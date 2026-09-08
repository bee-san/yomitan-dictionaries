#!/usr/bin/env python3
"""Convert the supplied Onomanabi note schema, without Anki history or scripts."""
import argparse
from collections import Counter
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import zipfile

FIELDS = ('WordId Word Reading Romaji Pitch AccentLabel AccentKind Meaning Notes '
          'ExampleJa ExampleEn Level Category Subcategory MotionLabel Anim '
          'AccentLight AccentDark DecoClass SourceTag').split()
SMALL_KANA = set('ぁぃぅぇぉゃゅょゎァィゥェォャュョヮ')
TITLE = 'Onomanabi - Japanese Onomatopoeia'
SOURCE_WARNINGS = {
    'source::verified': 'Source deck marks this entry as verified; not independently rechecked during conversion.',
    'source::generated': 'Source deck marks this gloss as generated; not independently verified.',
    'source::pending': 'No definition supplied in the source deck.',
    'pitch::dictionary': 'Source deck labels this pitch dictionary-sourced; the underlying dictionary is not identified.',
    'pitch::rule': 'Pitch is rule-derived in the source deck, not dictionary-verified.',
}


class InlineContent(HTMLParser):
    """Convert the deck's only inline markup, bold, to safe structured content."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.content = []
        self.stack = [self.content]

    def handle_data(self, data):
        self.stack[-1].append(data)

    def handle_starttag(self, tag, attrs):
        if tag != 'b' or attrs:
            raise ValueError(f'unsupported HTML tag or attributes: {tag}')
        node = {'tag': 'span', 'style': {'fontWeight': 'bold'}, 'content': []}
        self.stack[-1].append(node)
        self.stack.append(node['content'])

    def handle_endtag(self, tag):
        if tag != 'b' or len(self.stack) == 1:
            raise ValueError(f'unsupported HTML tag or unbalanced close: {tag}')
        self.stack.pop()

    def handle_comment(self, data):
        raise ValueError('unsupported HTML comment')

    def handle_decl(self, decl):
        raise ValueError('unsupported HTML declaration')


def inline(text):
    parser = InlineContent()
    parser.feed(text)
    parser.close()
    if len(parser.stack) != 1:
        raise ValueError('unclosed HTML tag')
    return parser.content


def load_notes(source):
    with zipfile.ZipFile(source) as archive, tempfile.TemporaryDirectory() as temp:
        if archive.testzip() is not None:
            raise ValueError('APKG CRC failure')
        if json.loads(archive.read('media')):
            raise ValueError('unexpected media: this converter requires the text-only Onomanabi deck')
        db = Path(temp) / 'collection.anki2'
        db.write_bytes(archive.read('collection.anki2'))
        connection = sqlite3.connect(f'{db.as_uri()}?mode=ro', uri=True)
        try:
            if connection.execute('pragma integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Anki database integrity failure')
            models = json.loads(connection.execute('select models from col').fetchone()[0])
            notes = []
            for mid, fields, tags in connection.execute('select mid,flds,tags from notes order by id'):
                names = [field['name'] for field in sorted(models[str(mid)]['flds'], key=lambda f: f['ord'])]
                values = fields.split('\x1f')
                if set(names) != set(FIELDS) or len(names) != len(FIELDS) or len(values) != len(names):
                    raise ValueError('unexpected Onomanabi field schema')
                notes.append(dict(zip(names, values), AnkiTags=tags.split()))
        finally:
            connection.close()
    if not notes:
        raise ValueError('empty source deck')
    ids = [note['WordId'] for note in notes]
    if len(ids) != len(set(ids)):
        raise ValueError('duplicate WordId')
    keys = [(note['Word'], note['Reading']) for note in notes]
    if len(keys) != len(set(keys)):
        raise ValueError('duplicate word and reading')
    return notes


def pitch_metadata(note):
    primary = re.fullmatch(r'\[(\d+)\] .+', note['AccentLabel'])
    if primary is None:
        raise ValueError('unrecognised pitch label')
    positions = list(dict.fromkeys([int(primary[1])] + [int(n) for n in re.findall(r'\[(\d+)\]', note['AccentKind'])]))
    mora_count = sum(char not in SMALL_KANA for char in note['Reading'])
    if any(position > mora_count for position in positions):
        raise ValueError(f"pitch position exceeds mora count: {note['WordId']}")
    tags = [tag for tag in note['AnkiTags'] if tag.startswith('pitch::')]
    if len(tags) != 1 or tags[0] not in ('pitch::rule', 'pitch::dictionary'):
        raise ValueError('missing or ambiguous pitch provenance')
    return [note['Word'], 'pitch', {'reading': note['Reading'],
            'pitches': [{'position': position, 'tags': tags} for position in positions]}]


def term_entry(note, sequence):
    if not note['Word'] or not note['Reading']:
        raise ValueError('empty word or reading')
    content = []
    if note['Meaning']:
        content.append({'tag': 'div', 'content': [{'tag': 'span', 'style': {'fontWeight': 'bold'}, 'content': inline(note['Meaning'])}]})
    else:
        content.append({'tag': 'div', 'content': 'No definition supplied in the source deck.'})
    for field, label in [('Notes', ''), ('ExampleJa', 'Example: '), ('ExampleEn', 'Translation: '),
                         ('Romaji', 'Romanisation: '), ('AccentLabel', 'Pitch: '),
                         ('AccentKind', 'Pitch type: '), ('Level', 'Deck level: '),
                         ('Category', 'Category: '), ('Subcategory', 'Subcategory: '),
                         ('MotionLabel', ''), ('SourceTag', 'Source label: '), ('WordId', 'Source entry: ')]:
        if note[field]:
            content.append({'tag': 'div', 'content': [label, *inline(note[field])]})
    for tag in note['AnkiTags']:
        if tag in SOURCE_WARNINGS:
            content.append({'tag': 'div', 'content': SOURCE_WARNINGS[tag]})
    return [note['Word'], note['Reading'], ' '.join(note['AnkiTags']), '', 0,
            [{'type': 'structured-content', 'content': {'tag': 'div', 'content': content}}], sequence, '']


def convert(source, output, revision, source_owner=None):
    notes = load_notes(source)
    terms = [term_entry(note, sequence) for sequence, note in enumerate(notes, 1)]
    pitches = [pitch_metadata(note) for note in notes]
    counts = Counter(tag for note in notes for tag in note['AnkiTags'])
    summary = {
        'source_notes': len(notes), 'lookup_entries': len(terms), 'pitch_entries': len(pitches),
        'pitch_variants': sum(len(row[2]['pitches']) for row in pitches),
        'with_definitions': sum(bool(note['Meaning']) for note in notes),
        'without_definitions': sum(not note['Meaning'] for note in notes),
        'with_bilingual_examples': sum(bool(note['ExampleJa'] and note['ExampleEn']) for note in notes),
        'deck_marked_verified': counts['source::verified'], 'generated_glosses': counts['source::generated'],
        'dictionary_labelled_pitch': counts['pitch::dictionary'], 'rule_derived_pitch': counts['pitch::rule'],
    }
    description = (
        f"{len(terms):,} Japanese onomatopoeia entries converted from Onomanabi_1.apkg. "
        f"{summary['with_definitions']:,} include definitions and bilingual examples; "
        f"{summary['without_definitions']:,} retain the source's missing-definition warning. "
        'Includes readings, romanisation, notes, categories, source status and native pitch metadata. '
        'Generated glosses and rule-derived pitches are explicitly labelled. '
        'Anki animations, CSS and device TTS are not included.'
    )
    index = {'title': TITLE, 'format': 3, 'revision': revision, 'sequenced': True,
             'description': description, 'sourceLanguage': 'ja', 'targetLanguage': 'en',
             'attribution': 'Converted from the user-supplied Onomanabi_1.apkg. '
             'No author, source bibliography or redistribution licence was embedded in the deck. '
             'No ownership or new licence over source content is claimed. See SOURCE.json and RIGHTS.txt.'}
    source_info = {
        'source_file': 'Onomanabi_1.apkg', 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
        'conversion': 'Onomanabi note fields to Yomitan v3 structured content and pitch metadata',
        'summary': summary, 'source_tags': dict(sorted(counts.items())),
        'preserved': ['WordId', 'Word', 'Reading', 'Romaji', 'AccentLabel', 'AccentKind',
                      'Meaning', 'Notes', 'ExampleJa', 'ExampleEn', 'Level', 'Category',
                      'Subcategory', 'MotionLabel', 'SourceTag', 'AnkiTags'],
        'transformed': {'Pitch': 'Native pitch metadata from AccentLabel and AccentKind, including alternate positions.'},
        'not_ported': ['Anim', 'AccentLight', 'AccentDark', 'DecoClass', 'Anki CSS/JavaScript', 'device TTS'],
        'privacy': 'No Anki note IDs, GUIDs, review history, scheduling, local paths or account information included.',
        'rights': 'Source author and redistribution licence are not recorded in the supplied deck.',
        'quality': 'Verified/dictionary labels reproduce source claims, not independent verification. '
                   'No missing definitions or examples were invented.'}
    rights = ('Source: user-supplied Onomanabi_1.apkg.\n'
              'No author, source bibliography or redistribution licence was embedded in the deck.\n'
              'Conversion does not establish rights over the source text or pitch data.\n'
              'No blanket reuse licence is granted by this archive. Preserve these notices.\n')
    if source_owner:
        confirmation = (f'{source_owner} confirmed ownership and authorised publication. '
                        'No blanket reuse licence is granted by this archive. Preserve source and pitch notices.')
        index['author'] = source_owner
        index['attribution'] = ('Converted from the user-supplied Onomanabi_1.apkg. ' + confirmation +
                                ' Source verification and pitch labels remain source-deck claims. See SOURCE.json and RIGHTS.txt.')
        source_info['rights'] = confirmation
        rights = 'Source: user-supplied Onomanabi_1.apkg.\n' + confirmation + '\n'
    members: dict[str, object] = {'index.json': index, 'SOURCE.json': source_info}
    for prefix, rows in [('term_bank', terms), ('term_meta_bank', pitches)]:
        for start in range(0, len(rows), 500):
            members[f'{prefix}_{start // 500 + 1}.json'] = rows[start:start + 500]
    members['tag_bank_1.json'] = [
        [tag, 'misc', 0, SOURCE_WARNINGS.get(tag, f'Source deck tag: {tag}'), 0]
        for tag in sorted(counts)]
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'w') as archive:
        for name, data in members.items():
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, json.dumps(data, ensure_ascii=False, separators=(',', ':')) + '\n')
        info = zipfile.ZipInfo('RIGHTS.txt', (1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(info, rights)
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None:
            raise ValueError('output CRC failure')
    return {'file': output.name, 'title': TITLE, 'revision': revision, 'description': description,
            'lookup_entries': len(terms), 'bytes': output.stat().st_size,
            'sha256': hashlib.sha256(output.read_bytes()).hexdigest(), 'summary': summary}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--source-owner', help='Only set after the named owner confirms ownership and publication permission')
    args = parser.parse_args()
    print(json.dumps(convert(args.source, args.output, args.revision, args.source_owner), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

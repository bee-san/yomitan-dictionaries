#!/usr/bin/env python3
"""Convert modern Anki grammar APKGs to deterministic, inert Yomitan v3 ZIPs.

Python 3.14 uses compression.zstd. Python 3.12/3.13 requires zstandard.
No note templates, JavaScript, CSS, SQL scripts or remote resources are executed.
"""
import argparse
from collections import Counter
import hashlib
from html.parser import HTMLParser
import io
import json
from pathlib import Path, PurePosixPath
import re
import sqlite3
import zipfile

try:
    from compression import zstd
except ImportError:
    zstd = None
    import zstandard

LIMIT = 64 * 1024 * 1024
REVISION = '2026.09.08-v1'
ALLOWED_TAGS = {'N1', 'N2', 'N3', 'N4', 'N5', '文法', 'JLPTに出ない'}
AI_WARNING = 'AI-generated material from the source deck. Not independently verified; the original AI field labels are retained.'
RIGHTS = 'Source deck authorship and redistribution rights are unverified. Source fields contain links to 日本語NET (nihongokyoshi-net.com); these links are retained. No licence is asserted for the deck content or its images.'


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def _decompress(data):
    stream = zstd.ZstdFile(io.BytesIO(data)) if zstd else zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data))
    with stream:
        result = stream.read(LIMIT + 1)
    if len(result) > LIMIT:
        raise ValueError('decompressed member exceeds 64 MiB limit')
    return result


def _safe_name(name):
    if not name or name in {'.', '..'} or '/' in name or '\\' in name or ':' in name or any(ord(c) < 32 for c in name):
        raise ValueError('unsafe media filename')
    return name


def _protobuf(data):
    offset = 0

    def integer():
        nonlocal offset
        result = 0
        for shift in range(0, 70, 7):
            if offset >= len(data):
                raise ValueError('truncated protobuf varint')
            byte = data[offset]
            offset += 1
            result |= (byte & 127) << shift
            if byte < 128:
                return result
        raise ValueError('oversized protobuf varint')

    result = []
    while offset < len(data):
        key = integer()
        field, wire = key >> 3, key & 7
        if field == 0:
            raise ValueError('invalid protobuf field')
        if wire == 0:
            value = integer()
        elif wire in (1, 2, 5):
            length = integer() if wire == 2 else (8 if wire == 1 else 4)
            if offset + length > len(data):
                raise ValueError('truncated protobuf value')
            value = data[offset:offset + length]
            offset += length
        else:
            raise ValueError('unsupported protobuf wire type')
        result.append((field, wire, value))
    return result


def decode_media_manifest(data):
    result = []
    for field, wire, entry in _protobuf(data):
        if field != 1:
            continue
        if wire != 2:
            raise ValueError('invalid media entry')
        parts = {number: value for number, _, value in _protobuf(entry)}
        if not isinstance(parts.get(1), bytes) or not isinstance(parts.get(2), int) or not isinstance(parts.get(3), bytes) or len(parts[3]) != 20:
            raise ValueError('incomplete media entry')
        result.append({'name': _safe_name(parts[1].decode('utf-8')), 'size': parts[2], 'sha1': parts[3].hex()})
    if len({entry['name'] for entry in result}) != len(result):
        raise ValueError('duplicate media name')
    return result


def read_apkg(source):
    source = Path(source)
    with zipfile.ZipFile(source) as archive:
        infos = archive.infolist()
        if len({info.filename for info in infos}) != len(infos):
            raise ValueError('duplicate archive member')
        for info in infos:
            _safe_name(info.filename)
            if info.file_size > LIMIT:
                raise ValueError('archive member too large')
        if archive.testzip() is not None:
            raise ValueError('APKG CRC failure')
        if 'collection.anki21b' not in archive.namelist():
            raise ValueError('requires modern APKG collection.anki21b, not the compatibility dummy')
        database = _decompress(archive.read('collection.anki21b'))
        media = decode_media_manifest(_decompress(archive.read('media')))
    if not database.startswith(b'SQLite format 3\0'):
        raise ValueError('not a SQLite database')
    # APKG exports are self-contained snapshots. A checkpointed WAL-mode
    # header cannot be deserialised in-memory by SQLite. Set read/write
    # format bytes to rollback mode in this private byte copy only.
    if database[18:20] == b'\x02\x02':
        database = database[:18] + b'\x01\x01' + database[20:]
    conn = sqlite3.connect(':memory:')
    try:
        conn.deserialize(database)
        conn.create_collation('unicase', lambda a, b: (a.casefold() > b.casefold()) - (a.casefold() < b.casefold()))
        conn.execute('PRAGMA trusted_schema=OFF')
        conn.execute('PRAGMA query_only=ON')
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'fields', 'notes'} <= tables:
            raise ValueError('unsupported Anki database schema')
        field_names = {}
        for model, ordinal, name in conn.execute('SELECT ntid, ord, name FROM fields ORDER BY ntid, ord'):
            names = field_names.setdefault(model, [])
            if ordinal != len(names) or name in names:
                raise ValueError('invalid field order or duplicate name')
            names.append(name)
        notes = []
        excluded_tags = Counter()
        for model, raw, tags in conn.execute('SELECT mid, flds, tags FROM notes'):
            values = raw.split('\x1f')
            names = field_names.get(model, [])
            if len(names) != len(values):
                raise ValueError('note field count mismatch')
            safe_tags = sorted(set(tags.split()) & ALLOWED_TAGS)
            excluded_tags['occurrences'] += sum(tag not in ALLOWED_TAGS for tag in tags.split())
            notes.append({'fields': dict(zip(names, values)), 'tags': safe_tags})
        counts = {table: conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] for table in ['notes', 'cards', 'revlog'] if table in tables}
    finally:
        conn.close()
    # Source IDs and GUIDs do not determine the exported sequence or appear in output.
    notes.sort(key=lambda note: _json(note))
    return {'notes': notes, 'media': media, 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(), 'source_counts': counts, 'excluded_tag_occurrences': excluded_tags['occurrences']}


class _SafeHTML(HTMLParser):
    BLOCKED = {'script', 'style', 'iframe', 'object', 'embed', 'svg', 'math', 'template'}
    CONTAINERS = {'ruby', 'rt', 'rp', 'table', 'thead', 'tbody', 'tfoot', 'tr', 'td', 'th', 'span', 'div', 'ol', 'ul', 'li', 'details', 'summary'}

    def __init__(self, media):
        super().__init__(convert_charrefs=True)
        self.root = []
        self.stack = [(None, self.root)]
        self.blocked = []
        self.media = media

    def handle_starttag(self, tag, attrs):
        if self.blocked:
            if tag in self.BLOCKED:
                self.blocked.append(tag)
            return
        if tag in self.BLOCKED:
            self.blocked.append(tag)
            return
        attrs = dict(attrs)
        if tag == 'img':
            name = _safe_name(attrs.get('src', ''))
            if name not in self.media:
                raise ValueError('image not present in supported local media: ' + name)
            node = {'tag': 'img', 'path': self.media[name]}
            if attrs.get('alt'):
                node['alt'] = attrs['alt']
            self.stack[-1][1].append(node)
            return
        if tag in {'br', 'hr'}:
            self.stack[-1][1].append({'tag': 'br'})
            return
        children = []
        mapped = tag if tag in self.CONTAINERS else ('div' if tag in {'p', 'h1', 'h2', 'h3', 'h4'} else 'span')
        node = {'tag': mapped, 'content': children}
        if tag == 'a' and re.match(r'^https?://[^\s/]+', attrs.get('href', ''), re.I):
            node = {'tag': 'a', 'href': attrs['href'], 'content': children}
        style = {}
        if tag in {'b', 'strong', 'h1', 'h2', 'h3', 'h4'} or re.search(r'font-weight\s*:\s*(?:700|bold)', attrs.get('style', '')):
            style['fontWeight'] = 'bold'
        if tag == 'u' or re.search(r'text-decoration(?:-line)?\s*:\s*underline', attrs.get('style', '')):
            style['textDecorationLine'] = 'underline'
        if style and mapped in {'span', 'div', 'td', 'th'} and node['tag'] != 'a':
            node['style'] = style
        self.stack[-1][1].append(node)
        self.stack.append((tag, children))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.blocked:
            if tag == self.blocked[-1]:
                self.blocked.pop()
            return
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        if not self.blocked:
            self.stack[-1][1].append(data)


def safe_content(raw, media):
    # Reveal the answer, not the cue. Preserve any ruby markup inside the answer.
    pattern = r'\{\{c\d+::((?:(?!\{\{|\}\}).)*?)\}\}'
    for _ in range(20):
        rendered, count = re.subn(pattern, lambda match: '<b>' + match[1].split('::', 1)[0] + '</b>', raw, flags=re.S)
        raw = rendered
        if count == 0:
            break
    if re.search(r'\{\{c\d+::', raw):
        raise ValueError('malformed or excessive nested cloze')
    parser = _SafeHTML(media)
    parser.feed(raw)
    parser.close()
    return parser.root


def _text(content, boundaries=False):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return ''.join(_text(item, boundaries) for item in content)
    body = _text(content.get('content', ''), boundaries)
    if boundaries and content.get('tag') in {'div', 'br', 'li'}:
        body += '\n'
    return body


def headwords(raw):
    rendered = _text(safe_content(raw, {}), True)
    original = ' '.join(rendered.split())
    if not original:
        raise ValueError('empty headword')
    result = [{'term': original, 'kind': 'source', 'reason': 'Original 文型 text, HTML removed and whitespace collapsed'}]
    seen = {original}

    def add(term, reason):
        term = ' '.join(term.split())
        if term and term not in seen:
            seen.add(term)
            result.append({'term': term, 'kind': 'alias', 'reason': reason})

    # These are literal readings printed in this deck, not dictionary guesses.
    readings = {'方（かた）': 'かた', '手前（てまえ）': 'てまえ', '至り（いたり）': 'いたり', 'に則って（〜にのっとって）': 'にのっとって'}
    candidates = []
    for line in rendered.splitlines():
        if not line.strip():
            continue
        line = re.sub(r'^文型[０-９0-9]+[：:]\s*', '', line.strip())
        line = re.sub(r'[（(]誤用(?:例)?[）)]', '', line)
        # Comparison headings expose only the explicitly quoted term as an alias.
        if '「' in line:
            candidates.extend(re.findall(r'「([^」]+)」', line))
        if not ('判別方法' in line or '類似文型' in line):
            candidates.extend(re.split(r'\s*[/／]\s*', line))
    for candidate in candidates:
        forms = [candidate]
        for notation, reading in readings.items():
            if notation in candidate:
                forms = [candidate.replace(notation, notation.split('（')[0]), candidate.replace(notation, reading)]
                break
        else:
            # Only the deck's explicit parenthesised optional particles are expanded.
            match = re.search(r'[（(](に|も|は|で|を|として)[）)]', candidate)
            if match:
                forms = [candidate[:match.start()] + candidate[match.end():], candidate[:match.start()] + match[1] + candidate[match.end():]]
        for form in forms:
            add(form, 'Explicit heading/slash alternative, annotation removed, or printed optional form/reading')
            # Never concatenate internal gaps or erase A/B/N/V placeholders.
            add(re.sub(r'^[〜～~]+', '', form), 'Leading grammar attachment marker removed; internal notation unchanged')
    return result


def _media_paths(value):
    if isinstance(value, list):
        return set().union(*(_media_paths(item) for item in value)) if value else set()
    if isinstance(value, dict):
        return ({value['path']} if value.get('tag') == 'img' else set()) | _media_paths(value.get('content', []))
    return set()


def convert(source, output, revision=REVISION):
    data = read_apkg(source)
    media_map = {}
    manifest = {}
    for ordinal, item in enumerate(data['media']):
        suffix = Path(item['name']).suffix.lower()
        if suffix in {'.jpg', '.jpeg', '.png', '.gif', '.webp'}:
            path = 'media/' + item['sha1'] + suffix
            media_map[item['name']] = path
            manifest[path] = (ordinal, item)
    rows, coverage, aliases = [], [], []
    field_counts, blank_counts = Counter(), Counter()
    paths = set()
    tags = set()
    for sequence, note in enumerate(data['notes'], 1):
        fields = note['fields']
        if not fields.get('文型', '').strip():
            raise ValueError('note missing 文型')
        terms = headwords(fields['文型'])
        sections = []
        ai_seen = False
        for name, raw in fields.items():
            if not raw.strip():
                blank_counts[name] += 1
                continue
            if name.startswith('AI') and not ai_seen:
                sections.append({'tag': 'div', 'content': AI_WARNING, 'style': {'fontWeight': 'bold'}})
                ai_seen = True
            content = safe_content(raw, media_map)
            sections.append({'tag': 'div', 'data': {'field': name}, 'content': [{'tag': 'div', 'style': {'fontWeight': 'bold'}, 'content': name}, {'tag': 'div', 'content': content}]})
            paths |= _media_paths(content)
            field_counts[name] += 1
            coverage.append({'sequence': sequence, 'field': name, 'source_sha256': hashlib.sha256(raw.encode()).hexdigest(), 'text_sha256': hashlib.sha256(_text(content).encode()).hexdigest()})
        glossary = [{'type': 'structured-content', 'content': {'tag': 'div', 'content': sections}}]
        note_tags = list(note['tags']) + (['AI'] if ai_seen else [])
        tags.update(note_tags)
        for term in terms:
            rows.append([term['term'], '', ' '.join(note_tags), '', 0, glossary, sequence, ''])
        aliases.append({'sequence': sequence, 'source_headword': terms[0]['term'], 'terms': terms})
    if not rows:
        raise ValueError('no source notes')
    index = {'title': '文法', 'revision': revision, 'format': 3, 'sequenced': True, 'sourceLanguage': 'ja', 'targetLanguage': 'ja', 'description': 'Japanese grammar with original Japanese explanations/examples and separately labelled source AI English/Japanese material. All non-empty note fields retained. Readings are left empty; aliases use source notation only. Source APKG SHA-256: ' + data['source_sha256'], 'attribution': RIGHTS}
    files = {'index.json': _json(index)}
    for start in range(0, len(rows), 500):
        files[f'term_bank_{start // 500 + 1}.json'] = _json(rows[start:start + 500])
    files['tag_bank_1.json'] = _json([[tag, 'misc', 0, ('Source deck AI-generated material, not independently verified' if tag == 'AI' else 'Source Anki tag: ' + tag), 0] for tag in sorted(tags)])
    retained = []
    with zipfile.ZipFile(source) as archive:
        for path in sorted(paths):
            ordinal, item = manifest[path]
            content = _decompress(archive.read(str(ordinal)))
            if len(content) != item['size'] or hashlib.sha1(content).hexdigest() != item['sha1']:
                raise ValueError('media integrity mismatch')
            suffix = Path(path).suffix
            valid = (suffix in {'.jpg', '.jpeg'} and content.startswith(b'\xff\xd8\xff')) or (suffix == '.png' and content.startswith(b'\x89PNG\r\n\x1a\n')) or (suffix == '.gif' and content.startswith((b'GIF87a', b'GIF89a'))) or (suffix == '.webp' and content.startswith(b'RIFF') and content[8:12] == b'WEBP')
            if not valid:
                raise ValueError('media signature mismatch')
            files[path] = content
            retained.append(item['name'])
    files['SOURCE.json'] = _json({
        'source_file': '文法.apkg', 'source_sha256': data['source_sha256'],
        'source_notes': len(data['notes']), 'lookup_entries': len(rows),
        'alias_rows': len(rows) - len(aliases),
        'nonempty_fields': sum(field_counts.values()), 'field_counts': dict(field_counts),
        'preserved': 'Every non-empty note field, Japanese furigana, source examples, AI labels and local referenced images.',
        'alias_policy': 'Original notation retained; explicit heading/slash alternatives, printed readings and optional particles indexed. Leading attachment markers removed in additional aliases; internal gaps and placeholders retained.',
        'transformed': 'HTML to safe structured content. Anki clozes show their answers in bold. No meanings or readings were invented.',
        'retained_images': sorted(retained),
        'omitted_media': sorted(item['name'] for item in data['media'] if item['name'] not in retained),
        'not_ported': ['Anki templates, JavaScript and CSS', 'device TTS', 'Anki review history and scheduling', 'Anki note IDs and GUIDs', 'unrecognised private tags'],
        'quality': AI_WARNING, 'rights': RIGHTS,
    })
    files['RIGHTS.txt'] = (RIGHTS + '\nPublication requested by the collection owner. This does not grant downstream reuse rights. Preserve original source links and AI labels.\n').encode('utf-8')
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, content in sorted(files.items()):
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, content, compresslevel=9)
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None:
            raise ValueError('output CRC failure')
    source_heads = {item['source_headword'] for item in aliases}
    all_heads = {row[0] for row in rows}
    return {'revision': revision, 'source_sha256': data['source_sha256'], 'output_sha256': hashlib.sha256(output.read_bytes()).hexdigest(), 'output_bytes': output.stat().st_size, 'notes': len(data['notes']), 'source_counts': data['source_counts'], 'source_rows': len(aliases), 'unique_source_headwords': len(source_heads), 'term_rows': len(rows), 'alias_rows': len(rows) - len(aliases), 'unique_headwords': len(all_heads), 'unique_alias_only_headwords': len(all_heads - source_heads), 'nonempty_fields': sum(field_counts.values()), 'covered_fields': len(coverage), 'omitted_nonempty_fields': 0, 'field_counts': dict(field_counts), 'blank_field_counts': dict(blank_counts), 'retained_media': len(retained), 'retained_media_names': sorted(retained), 'omitted_media': sorted(item['name'] for item in data['media'] if item['name'] not in retained), 'excluded_tag_occurrences': data['excluded_tag_occurrences'], 'coverage': coverage, 'headwords': aliases, 'rights': RIGHTS}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--revision', default=REVISION)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    report = convert(args.source, args.output, args.revision)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: value for key, value in report.items() if key not in {'coverage', 'headwords'}}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

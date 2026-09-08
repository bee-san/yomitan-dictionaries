"""Regression tests. Synthetic fixtures contain no source-deck data."""
import hashlib
import importlib.util
import json
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
try:
    from compression import zstd
except ImportError:
    import zstandard

    class zstd:
        compress = staticmethod(lambda data: zstandard.ZstdCompressor().compress(data))

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
try:
    import convert_bunpo as converter
except ImportError:
    converter = None


def strings(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ''.join(strings(item) for item in value)
    return strings(value.get('content', '')) if isinstance(value, dict) else ''


def varint(n):
    result = bytearray()
    while n > 127:
        result.append((n & 127) | 128)
        n >>= 7
    return bytes(result + bytes([n]))


def blob(field, data):
    return varint(field << 3 | 2) + varint(len(data)) + data


def manifest_item(name, data):
    return blob(1, blob(1, name.encode()) + varint(16) + varint(len(data)) + blob(3, hashlib.sha1(data).digest()))


def fixture(folder, fields=None, media=None):
    fields = fields or {'文型': '<h2>〜ないで</h2>', '意味': 'Without doing', '例文1': '食べ<strong>ないで</strong>寝た。', 'AI例文1': '食べ{{c1::ないで}}寝た。', 'AI英訳1': 'Slept <b>without</b> eating.', 'AI意味': 'Generated explanation', '備考': ''}
    media = media or {}
    db = pathlib.Path(folder) / 'fixture.sqlite'
    db.unlink(missing_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript('CREATE TABLE fields (ntid INTEGER, ord INTEGER, name TEXT); CREATE TABLE notes (id INTEGER, guid TEXT, mid INTEGER, flds TEXT, tags TEXT); CREATE TABLE cards (nid INTEGER); CREATE TABLE revlog (id INTEGER);')
    conn.executemany('INSERT INTO fields VALUES (7,?,?)', enumerate(fields))
    conn.execute('INSERT INTO notes VALUES (1739999000000,?,7,?,?)', ('PRIVATE-GUID', '\x1f'.join(fields.values()), ' N5 文法 private-test-tag '))
    conn.execute('INSERT INTO cards VALUES (1739999000000)')
    conn.commit()
    conn.close()
    path = pathlib.Path(folder) / 'fixture.apkg'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('meta', b'\x08\x03')
        archive.writestr('collection.anki21b', zstd.compress(db.read_bytes()))
        archive.writestr('collection.anki2', b'DUMMY, NOT SQLITE')
        archive.writestr('media', zstd.compress(b''.join(manifest_item(name, data) for name, data in media.items())))
        for i, data in enumerate(media.values()):
            archive.writestr(str(i), zstd.compress(data))
    return path


class ConverterTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(converter, 'the converter implementation is missing')
        self.temp = tempfile.TemporaryDirectory(prefix='bunpo-test-', dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.folder = pathlib.Path(self.temp.name)

    def test_reads_real_modern_database_not_dummy(self):
        data = converter.read_apkg(fixture(self.folder))
        self.assertEqual(len(data['notes']), 1)
        self.assertEqual(data['notes'][0]['fields']['意味'], 'Without doing')
        self.assertNotIn('PRIVATE-GUID', repr(data['notes']))
        self.assertNotIn('1739999000000', repr(data['notes']))

    def test_reads_wal_mode_export_without_a_sidecar_file(self):
        source = fixture(self.folder)
        with zipfile.ZipFile(source) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        # A checkpointed WAL-mode file keeps WAL bytes in its header.
        db = self.folder / 'fixture.sqlite'
        conn = sqlite3.connect(db)
        self.assertEqual(conn.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
        conn.close()
        members['collection.anki21b'] = zstd.compress(db.read_bytes())
        with zipfile.ZipFile(source, 'w') as archive:
            for name, content in members.items():
                archive.writestr(name, content)
        self.assertEqual(len(converter.read_apkg(source)['notes']), 1)

    def test_decodes_protobuf_media_with_hash_and_size(self):
        data = b'abc'
        self.assertEqual(converter.decode_media_manifest(manifest_item('a.jpg', data)), [{'name': 'a.jpg', 'size': 3, 'sha1': hashlib.sha1(data).hexdigest()}])

    def test_rejects_truncated_protobuf(self):
        with self.assertRaises(ValueError):
            converter.decode_media_manifest(b'\x0a\x7fshort')

    def test_cloze_is_revealed_bold_including_ruby_and_hint(self):
        content = converter.safe_content('{{c1::<ruby>猫<rt>ねこ</rt></ruby>::animal}}と{{c2::犬}}', {})
        self.assertEqual(strings(content), '猫ねこと犬')
        self.assertIn('ruby', repr(content))
        self.assertEqual(repr(content).count("'fontWeight': 'bold'"), 2)
        self.assertNotIn('{{', repr(content))

    def test_safe_html_retains_text_ruby_bold_underline_and_link(self):
        content = converter.safe_content('<div>A<br>B <strong>bold</strong><u>under</u><ruby>語<rt>ご</rt></ruby><a href="https://example.org/a">source</a></div>', {})
        self.assertEqual(strings(content), 'AB boldunder語ごsource')
        self.assertIn('https://example.org/a', repr(content))
        self.assertIn('underline', repr(content))
        self.assertIn("'tag': 'br'", repr(content))

    def test_scripts_events_styles_and_unsafe_links_cannot_survive(self):
        content = converter.safe_content('<script>BAD</script><style>EVIL</style><iframe>EVIL</iframe><div onclick="BAD" style="background:url(https://evil)"><a href="javascript:BAD">keep</a><span hidden>visible</span></div>', {})
        self.assertEqual(strings(content), 'keepvisible')
        for forbidden in ['BAD', 'EVIL', 'onclick', 'javascript', 'background']:
            self.assertNotIn(forbidden, repr(content))

    def test_missing_or_remote_image_fails_instead_of_silent_omission(self):
        for name in ['missing.jpg', 'https://example.org/tracker.jpg', '../escape.jpg']:
            with self.subTest(name=name), self.assertRaises(ValueError):
                converter.safe_content(f'<img src="{name}">', {})

    def test_manifest_unsafe_path_is_rejected(self):
        with self.assertRaises(ValueError):
            converter.decode_media_manifest(manifest_item('../a.jpg', b'abc'))

    def test_source_notation_and_literal_alias_are_both_searchable(self):
        terms = converter.headwords('<h2>～ないで</h2>')
        self.assertEqual(terms[0]['term'], '～ないで')
        self.assertIn('ないで', [x['term'] for x in terms])

    def test_slash_and_optional_particle_aliases(self):
        terms = [x['term'] for x in converter.headwords('〜際(に) / 〜際の')]
        for term in ['際', '際に', '際の']:
            self.assertIn(term, terms)
        self.assertNotIn('際に際の', terms)

    def test_explicit_reading_annotation_is_not_optional_kana(self):
        terms = [x['term'] for x in converter.headwords('〜の至り（いたり）')]
        self.assertIn('の至り', terms)
        self.assertIn('のいたり', terms)
        self.assertNotIn('の至りいたり', terms)

    def test_discontinuous_templates_are_not_falsely_concatenated(self):
        terms = [x['term'] for x in converter.headwords('とても〜ない')]
        self.assertEqual(terms, ['とても〜ない'])
        self.assertNotIn('とてもない', terms)

    def test_source_placeholders_and_misuse_annotation_stay_in_gloss_not_alias(self):
        terms = [x['term'] for x in converter.headwords('AながらB（誤用例）')]
        self.assertIn('AながらB', terms)
        self.assertNotIn('ながら', terms)
        self.assertEqual(terms[0], 'AながらB（誤用例）')
        self.assertIn('きり', [x['term'] for x in converter.headwords('文型１：〜きり')])

    def test_both_headings_are_preserved_with_searchable_main_heading(self):
        terms = [x['term'] for x in converter.headwords('<h2>〜向けに<br></h2><h2>類似文型「〜向き」との違い</h2>')]
        self.assertIn('向けに', terms)
        self.assertIn('向き', terms)

    def test_convert_covers_every_nonempty_field_and_no_private_tags(self):
        source = fixture(self.folder)
        report = converter.convert(source, self.folder / 'out.zip', revision='test-v1')
        with zipfile.ZipFile(self.folder / 'out.zip') as archive:
            terms = json.loads(archive.read('term_bank_1.json'))
            text = archive.read('term_bank_1.json').decode()
            self.assertNotIn('PRIVATE-GUID', text)
            self.assertNotIn('1739999000000', text)
            self.assertNotIn('private-test-tag', text)
            self.assertNotIn('{{c', text)
            self.assertIn('AI-generated', text)
            self.assertEqual({x[6] for x in terms}, {1})
            self.assertEqual({x[1] for x in terms}, {''})
            self.assertEqual(archive.testzip(), None)
            self.assertEqual(json.loads(archive.read('index.json'))['format'], 3)
        self.assertEqual(report['notes'], 1)
        self.assertEqual(report['nonempty_fields'], 6)
        self.assertEqual(report['covered_fields'], 6)
        self.assertEqual(report['omitted_nonempty_fields'], 0)

    def test_referenced_media_only_is_retained_and_integrity_checked(self):
        jpeg = b'\xff\xd8\xff\xe0IMAGE\xff\xd9'
        source = fixture(self.folder, {'文型': '語', '接続': '<img src="chart.jpg">'}, {'chart.jpg': jpeg, 'unused.otf': b'FONT'})
        report = converter.convert(source, self.folder / 'out.zip')
        with zipfile.ZipFile(self.folder / 'out.zip') as archive:
            images = [name for name in archive.namelist() if name.startswith('media/')]
            self.assertEqual(len(images), 1)
            self.assertEqual(archive.read(images[0]), jpeg)
            self.assertNotIn('unused.otf', archive.namelist())
        self.assertEqual(report['retained_media'], 1)
        self.assertEqual(report['omitted_media'], ['unused.otf'])

    def test_output_is_deterministic_and_source_hash_is_recorded(self):
        source = fixture(self.folder)
        for name in ['a.zip', 'b.zip']:
            converter.convert(source, self.folder / name)
        self.assertEqual((self.folder / 'a.zip').read_bytes(), (self.folder / 'b.zip').read_bytes())
        with zipfile.ZipFile(self.folder / 'a.zip') as archive:
            index = json.loads(archive.read('index.json'))
            self.assertIn(hashlib.sha256(source.read_bytes()).hexdigest(), index['description'])
            self.assertNotIn('author', index)
            self.assertNotIn('/Users/', json.dumps(index))
            self.assertIn('unverified', index['attribution'])

    def test_embeds_source_counts_omissions_and_rights_without_private_history(self):
        source = fixture(self.folder)
        converter.convert(source, self.folder / 'out.zip')
        with zipfile.ZipFile(self.folder / 'out.zip') as archive:
            self.assertIn('SOURCE.json', archive.namelist())
            self.assertIn('RIGHTS.txt', archive.namelist())
            provenance = json.loads(archive.read('SOURCE.json'))
            rights = archive.read('RIGHTS.txt').decode()
        self.assertEqual(provenance['source_sha256'], hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertEqual(provenance['source_notes'], 1)
        self.assertEqual(provenance['nonempty_fields'], 6)
        self.assertIn('Anki review history and scheduling', provenance['not_ported'])
        self.assertIn('AI-generated', provenance['quality'])
        self.assertIn('unverified', rights)
        for value in ('PRIVATE-GUID', '1739999000000', 'private-test-tag', str(self.folder)):
            self.assertNotIn(value, json.dumps(provenance))

    def test_unrecognised_semantic_field_is_not_dropped(self):
        source = fixture(self.folder, {'文型': '語', 'Future semantic field': 'must keep'})
        report = converter.convert(source, self.folder / 'out.zip')
        self.assertEqual(report['covered_fields'], 2)
        with zipfile.ZipFile(self.folder / 'out.zip') as archive:
            self.assertIn('must keep', archive.read('term_bank_1.json').decode())

    def test_missing_headword_fails(self):
        source = fixture(self.folder, {'意味': 'no headword'})
        with self.assertRaises(ValueError):
            converter.convert(source, self.folder / 'out.zip')

    def test_cli_writes_zip_and_report(self):
        source = fixture(self.folder)
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/convert_bunpo.py'), str(source), str(self.folder / 'cli.zip'), '--report', str(self.folder / 'report.json')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.folder / 'report.json').read_text())['notes'], 1)
        self.assertTrue((self.folder / 'cli.zip').is_file())


if __name__ == '__main__':
    unittest.main()

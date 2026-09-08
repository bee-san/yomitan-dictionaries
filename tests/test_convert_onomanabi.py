"""Synthetic fixtures for the Onomanabi APKG converter, not dictionary data."""
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'convert_onomanabi.py'
FIELDS = ('WordId Word Reading Romaji Pitch AccentLabel AccentKind Meaning Notes '
          'ExampleJa ExampleEn Level Category Subcategory MotionLabel Anim '
          'AccentLight AccentDark DecoClass SourceTag').split()


class ConversionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.note: dict[str, str] = {name: '' for name in FIELDS}
        self.note.update(WordId='ono_0001', Word='イライラ', Reading='いらいら',
                         Romaji='iraira', AccentLabel='[0] 平板',
                         AccentKind='heiban · also [1]', Meaning='irritated',
                         Notes='A test note & explanation.',
                         ExampleJa='私は<b>イライラ</b>する。', ExampleEn='I feel irritated.',
                         Level='1', Category='emotion', Subcategory='anger',
                         MotionLabel='motion: irritate', SourceTag='gloss: generated')

    def run_conversion(self, notes=None, extra_args=()):
        notes = notes or [(self.note, 'source::generated pitch::dictionary Lv1')]
        db = self.root / 'collection.anki2'
        if db.exists():
            db.unlink()
        with sqlite3.connect(db) as conn:
            conn.execute('create table col (models text)')
            conn.execute('insert into col values (?)', (json.dumps({
                '1': {'flds': [{'name': name, 'ord': i} for i, name in enumerate(FIELDS)]}
            }),))
            conn.execute('create table notes (id integer, mid integer, flds text, tags text)')
            for i, (note, tags) in enumerate(notes):
                conn.execute('insert into notes values (?,1,?,?)',
                             (i, '\x1f'.join(note[name] for name in FIELDS), tags))
        source = self.root / 'input.apkg'
        with zipfile.ZipFile(source, 'w') as archive:
            archive.write(db, 'collection.anki2')
            archive.writestr('media', '{}')
        output = self.root / 'output.zip'
        result = subprocess.run([sys.executable, str(SCRIPT), str(source), str(output),
                                 '--revision', 'test-v1', *extra_args], capture_output=True, text=True)
        return result, output

    def read_success(self, notes=None):
        result, output = self.run_conversion(notes)
        self.assertEqual(result.returncode, 0, result.stderr)
        with zipfile.ZipFile(output) as archive:
            self.assertIsNone(archive.testzip())
            return {name: json.loads(archive.read(name)) for name in archive.namelist()
                    if name.endswith('.json')}, output.read_bytes()

    def test_preserves_words_examples_alternate_pitch_and_provenance(self):
        data, _ = self.read_success()
        row = data['term_bank_1.json'][0]
        self.assertEqual(row[:2], ['イライラ', 'いらいら'])
        self.assertIn('source::generated', row[2].split())
        glossary = json.dumps(row[5], ensure_ascii=False)
        for text in ['irritated', 'A test note & explanation.', '私は', 'I feel irritated.',
                     'gloss: generated', 'ono_0001']:
            self.assertIn(text, glossary)
        self.assertNotIn('"tag": "b"', glossary)
        self.assertIn('"fontWeight": "bold"', glossary)
        meta = data['term_meta_bank_1.json'][0]
        self.assertEqual(meta[:2], ['イライラ', 'pitch'])
        self.assertEqual(meta[2]['reading'], 'いらいら')
        self.assertEqual([p['position'] for p in meta[2]['pitches']], [0, 1])
        self.assertTrue(all(p['tags'] == ['pitch::dictionary'] for p in meta[2]['pitches']))
        self.assertEqual(data['index.json']['format'], 3)
        self.assertNotIn(str(self.root), json.dumps(data))

    def test_retains_undefined_entries_and_rule_derived_warning(self):
        pending = dict(self.note, WordId='ono_0002', Word='チリン', Reading='ちりん',
                       Meaning='', Notes='', ExampleJa='', ExampleEn='', SourceTag='',
                       AccentLabel='[2] 中高', AccentKind='nakadaka · rule-derived')
        data, _ = self.read_success([(pending, 'source::pending pitch::rule')])
        self.assertEqual(len(data['term_bank_1.json']), 1)
        text = json.dumps(data['term_bank_1.json'], ensure_ascii=False)
        self.assertIn('No definition supplied in the source deck.', text)
        self.assertIn('rule-derived', text)
        self.assertEqual(data['term_meta_bank_1.json'][0][2]['pitches'][0]['tags'],
                         ['pitch::rule'])

    def test_deterministic_bytes(self):
        _, first = self.read_success()
        _, second = self.read_success()
        self.assertEqual(hashlib.sha256(first).digest(), hashlib.sha256(second).digest())

    def test_explicit_owner_confirmation_is_recorded_without_inventing_licence(self):
        result, output = self.run_conversion(extra_args=('--source-owner', 'bee-san'))
        self.assertEqual(result.returncode, 0, result.stderr)
        with zipfile.ZipFile(output) as archive:
            index = json.loads(archive.read('index.json'))
            source = json.loads(archive.read('SOURCE.json'))
            rights = archive.read('RIGHTS.txt').decode()
        self.assertEqual(index['author'], 'bee-san')
        for text in (index['attribution'], source['rights'], rights):
            self.assertIn('bee-san confirmed ownership and authorised publication', text)
            self.assertIn('No blanket reuse licence', text)

    def test_duplicate_word_ids_rejected(self):
        result, _ = self.run_conversion([(self.note, 'pitch::dictionary'),
                                         (self.note, 'pitch::dictionary')])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('duplicate WordId', result.stderr)

    def test_script_markup_rejected_not_executed_or_silently_discarded(self):
        self.note['Meaning'] = '<script>alert(1)</script>'
        result, _ = self.run_conversion()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unsupported HTML tag', result.stderr)

    def test_out_of_range_pitch_rejected(self):
        self.note['AccentLabel'] = '[9] 中高'
        result, _ = self.run_conversion()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('pitch position exceeds mora count', result.stderr)


if __name__ == '__main__':
    unittest.main()

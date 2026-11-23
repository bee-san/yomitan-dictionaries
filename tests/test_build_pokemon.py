"""Synthetic fixtures for the Pokémon dictionary builder, not dictionary data."""
import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

from PIL import Image

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'build_pokemon.py'

TABLES = {
    'pokemon_species': [
        'id,identifier,generation_id,evolves_from_species_id,is_baby,is_legendary,is_mythical',
        '1,testmon,1,,0,0,0', '2,evomon,1,1,0,1,0', '3,digitmon,4,,0,0,0'],
    'pokemon_species_names': [
        'pokemon_species_id,local_language_id,name,genus',
        '1,11,テストン,たねポケモン', '1,1,テストン,たねポケモン', '1,2,Tesuton,', '1,9,Testmon,Seed Pokémon',
        '2,11,シンカン,はなポケモン', '2,9,Evomon,Flower Pokémon',
        '3,11,デジタル２,すうじポケモン', '3,9,Digitmon,Digit Pokémon'],
    'pokemon_species_flavor_text': [
        'species_id,version_id,language_id,flavor_text',
        '1,1,11,古い　説明。', '1,2,11,"新しい　説明\nだ。"', '1,2,1,あたらしい　せつめいだ。',
        '1,2,9,"A new\nentry."', '1,3,9,English only.'],
    'pokemon': [
        'id,identifier,species_id,height,weight,is_default',
        '1,testmon,1,7,69,1', '2,evomon,2,20,1000,1', '3,digitmon,3,5,10,1',
        '10001,evomon-mega-x,2,25,1100,0', '10002,evomon-regional,2,20,900,0',
        '10003,evomon-unnamed,2,20,900,0', '10004,evomon-no-art,2,20,900,0',
        '10005,evomon-mega-x-alt,2,26,1200,0'],
    'pokemon_types': [
        'pokemon_id,type_id,slot',
        '1,12,1', '1,4,2', '2,12,1', '3,1,1', '10001,10,1', '10002,15,1', '10003,12,1',
        '10004,12,1', '10005,11,1'],
    'types': ['id,identifier', '1,normal', '4,poison', '10,fire', '11,water', '12,grass', '15,ice'],
    'type_names': [
        'type_id,local_language_id,name',
        '1,11,ノーマル', '1,9,Normal', '4,11,どく', '4,9,Poison', '10,11,ほのお', '10,9,Fire',
        '11,11,みず', '11,9,Water', '12,11,くさ', '12,9,Grass', '15,11,こおり', '15,9,Ice'],
    'pokemon_forms': [
        'id,identifier,pokemon_id,is_default',
        '1,testmon,1,1', '2,evomon,2,1', '3,digitmon,3,1', '10001,evomon-mega-x,10001,1',
        '10002,evomon-regional,10002,1', '10003,evomon-unnamed,10003,1',
        '10004,evomon-no-art,10004,1', '10005,evomon-mega-x-alt,10005,1'],
    'pokemon_form_names': [
        'pokemon_form_id,local_language_id,form_name,pokemon_name',
        '10001,11,メガシンカンX,', '10001,9,Mega Evomon X,Mega Evomon X',
        '10005,11,メガシンカンX,', '10005,9,Mega Evomon X,Mega Alt Evomon X',
        '10002,11,ちほうのすがた,', '10002,9,Regional Form,Regional Evomon',
        '10004,11,メガシンカンＹ,'],
    'generations': ['id,identifier', '1,generation-i', '4,generation-iv'],
    'version_names': ['version_id,local_language_id,name', '1,9,Old', '2,9,New', '3,9,Newest'],
    'abilities': ['id,identifier', '65,overgrow', '34,chlorophyll'],
    'ability_names': [
        'ability_id,local_language_id,name', '65,11,しんりょく', '65,9,Overgrow', '34,9,Chlorophyll'],
    'pokemon_abilities': ['pokemon_id,ability_id,is_hidden,slot', '1,65,0,1', '1,34,1,3'],
}
ARTWORK = ('1', '2', '3', '10001', '10002', '10003', '10005')


def texts(node):
    if isinstance(node, str):
        return [node]
    if isinstance(node, list):
        return [text for item in node for text in texts(item)]
    if isinstance(node, dict):
        return texts(node.get('content', []))
    return []


def nodes(node):
    if isinstance(node, list):
        for item in node:
            yield from nodes(item)
    elif isinstance(node, dict):
        yield node
        yield from nodes(node.get('content', []))


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'csv').mkdir()
        (self.root / 'artwork').mkdir()
        for table, lines in TABLES.items():
            (self.root / 'csv' / f'{table}.csv').write_text('\n'.join(lines) + '\n', encoding='utf-8')
        for index, pokemon_id in enumerate(ARTWORK):
            Image.new('RGBA', (475, 475), (20 * index, 80, 160, 255)).save(
                self.root / 'artwork' / f'{pokemon_id}.png')

    def run_build(self, name='output.zip'):
        output = self.root / name
        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(self.root), str(output), '--revision', 'test-v1',
             '--pokeapi-commit', 'aaa', '--sprites-commit', 'bbb'], capture_output=True, text=True)
        return result, output

    def read_success(self):
        result, output = self.run_build()
        self.assertEqual(result.returncode, 0, result.stderr)
        with zipfile.ZipFile(output) as archive:
            self.assertIsNone(archive.testzip())
            names = set(archive.namelist())
            rows = json.loads(archive.read('term_bank_1.json'))
            index = json.loads(archive.read('index.json'))
            source = json.loads(archive.read('SOURCE.json'))
            image = Image.open(archive.open('img/1.webp'))
            image.load()
        return json.loads(result.stdout), names, {row[0]: row for row in rows}, index, source, image

    def test_species_entry_contents(self):
        report, names, rows, index, source, image = self.read_success()
        self.assertEqual(report['lookup_entries'], 4)
        self.assertEqual(index['title'], report['title'])
        self.assertEqual(index['revision'], 'test-v1')
        self.assertEqual(source['data']['commit'], 'aaa')
        self.assertEqual(source['artwork']['commit'], 'bbb')
        self.assertEqual((image.format, image.size), ('WEBP', (400, 400)))
        self.assertIn('styles.css', names)
        self.assertIn('RIGHTS.txt', names)

        row = rows['テストン']
        self.assertEqual(len(row), 8)
        self.assertEqual((row[1], row[2], row[6]), ('テストン', 'ポケモン', 1))
        text = ''.join(texts(row[5]))
        for expected in ('No.0001', 'Testmon', '[Tesuton]', 'たねポケモン — Seed Pokémon', 'くさ Grass',
                         'どく Poison', '0.7 m', '6.9 kg', '第1世代・カントー地方 (Generation I, Kanto)',
                         'しんりょく (Overgrow)', 'Chlorophyll［隠れ特性 / Hidden］',
                         '新しい説明だ。', 'あたらしい せつめいだ。', 'A new entry.', 'Pokémon New'):
            self.assertIn(expected, text)
        self.assertNotIn('古い', text)
        self.assertNotIn('English only.', text)
        links = [node['href'] for node in nodes(row[5]) if node.get('tag') == 'a']
        self.assertEqual(links, ['?query=シンカン&wildcards=off'])

    def test_forms_readings_and_media(self):
        _, names, rows, _, source, _ = self.read_success()
        self.assertEqual(rows['デジタル２'][1], 'デジタルツー')
        self.assertIn('伝説 Legendary', ''.join(texts(rows['シンカン'][5])))

        species_images = [node['path'] for node in nodes(rows['シンカン'][5]) if node.get('tag') == 'img']
        self.assertEqual(species_images, ['img/2.webp', 'img/10001.webp', 'img/10002.webp', 'img/10005.webp'])
        self.assertIn('ちほうのすがた', ''.join(texts(rows['シンカン'][5])))

        mega = rows['メガシンカンＸ']
        self.assertEqual((mega[1], mega[2], mega[6]), ('メガシンカンエックス', 'フォルム', 10001))
        mega_text = ''.join(texts(mega[5]))
        for expected in ('Mega Evomon X', 'Mega Alt Evomon X', 'ほのお Fire', 'みず Water', '2.5 m', '2.6 m'):
            self.assertIn(expected, mega_text)
        self.assertNotIn('メガシンカンＹ', rows)

        referenced = {node['path'] for row in rows.values() for node in nodes(row[5]) if node.get('tag') == 'img'}
        self.assertEqual(referenced, {name for name in names if name.startswith('img/')})
        self.assertEqual(referenced, {f'img/{i}.webp' for i in ('1', '2', '3', '10001', '10002', '10005')})
        self.assertEqual(source['summary']['alternate_forms_without_artwork'], ['evomon-no-art'])
        self.assertEqual(source['summary']['named_form_entries'], 1)

    def test_output_is_reproducible(self):
        first, first_output = self.run_build('first.zip')
        second, second_output = self.run_build('second.zip')
        self.assertEqual((first.returncode, second.returncode), (0, 0), first.stderr + second.stderr)
        self.assertEqual(first_output.read_bytes(), second_output.read_bytes())

    def test_missing_species_artwork_fails(self):
        (self.root / 'artwork' / '3.png').unlink()
        result, output = self.run_build()
        self.assertEqual(result.returncode, 1)
        self.assertIn('missing artwork for デジタル２', result.stderr)
        self.assertFalse(output.exists())

    def test_underivable_reading_fails(self):
        path = self.root / 'csv' / 'pokemon_species_names.csv'
        with path.open(encoding='utf-8', newline='') as source:
            rows = list(csv.reader(source))
        rows = [['3', '11', '漢字モン', row[3]] if row[:2] == ['3', '11'] else row for row in rows]
        with path.open('w', encoding='utf-8', newline='') as target:
            csv.writer(target).writerows(rows)
        result, _ = self.run_build()
        self.assertEqual(result.returncode, 1)
        self.assertIn('cannot derive a kana reading for 漢字モン', result.stderr)


if __name__ == '__main__':
    unittest.main()

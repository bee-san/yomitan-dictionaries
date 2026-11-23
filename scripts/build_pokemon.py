#!/usr/bin/env python3
"""Build the illustrated Pokémon Yomitan dictionary from PokeAPI data and artwork.

The data directory must contain ``csv/<table>.csv`` (PokeAPI ``data/v2/csv``)
and ``artwork/<pokemon id>.png`` (PokeAPI sprites ``official-artwork``).
``--fetch`` downloads whatever is missing at the pinned commits.
"""
import argparse
from collections import defaultdict
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import urllib.error
import urllib.request
import zipfile

from PIL import Image

TITLE = 'Pokémon Zukan (Illustrated)'
FILE_NAME = 'pokemon-zukan-yomitan.zip'
TABLES = ('pokemon_species pokemon_species_names pokemon_species_flavor_text pokemon '
          'pokemon_types types type_names pokemon_forms pokemon_form_names generations '
          'version_names abilities ability_names pokemon_abilities').split()
CSV_URL = 'https://raw.githubusercontent.com/PokeAPI/pokeapi/{commit}/data/v2/csv/{table}.csv'
ARTWORK_URL = ('https://raw.githubusercontent.com/PokeAPI/sprites/{commit}'
               '/sprites/pokemon/other/official-artwork/{pokemon_id}.png')
JA, JA_KANA, ROMAJI, EN = '11', '1', '2', '9'
IMAGE_PIXELS = 400
BANK_SIZE = 250
ZIP_DATE = (1980, 1, 1, 0, 0, 0)

TYPE_COLOURS = {
    'normal': '#8a8a59', 'fighting': '#a82c26', 'flying': '#8a75d8', 'poison': '#8f3a8f',
    'ground': '#a8873a', 'rock': '#9c8a30', 'bug': '#7d8c1a', 'ghost': '#634e86',
    'steel': '#77778f', 'fire': '#d2691e', 'water': '#3a6fd8', 'grass': '#4a9a34',
    'electric': '#b8960a', 'psychic': '#d83a68', 'ice': '#3f9a96', 'dragon': '#5a2ee6',
    'dark': '#5f4a3d', 'fairy': '#c2577a',
}
REGIONS = {
    '1': ('カントー地方', 'Kanto'), '2': ('ジョウト地方', 'Johto'), '3': ('ホウエン地方', 'Hoenn'),
    '4': ('シンオウ地方', 'Sinnoh'), '5': ('イッシュ地方', 'Unova'), '6': ('カロス地方', 'Kalos'),
    '7': ('アローラ地方', 'Alola'), '8': ('ガラル地方', 'Galar'), '9': ('パルデア地方', 'Paldea'),
}
READING_REPLACEMENTS = {
    '♀': 'メス', '♂': 'オス', '２': 'ツー', 'Ｘ': 'エックス', 'Ｙ': 'ワイ', 'Ｚ': 'ゼット', '：': '',
}
FULL_WIDTH = str.maketrans(
    {chr(code): chr(code + 0xFEE0) for code in [*range(0x30, 0x3A), *range(0x41, 0x5B), *range(0x61, 0x7B)]})
READING_PATTERN = re.compile(r'[ぁ-ゖァ-ヺー・]+')

STYLES = '''div[data-sc-content="pokemon"] { line-height: 1.5; }
div[data-sc-content="title"] { font-size: 1.15em; }
span[data-sc-content="number"], span[data-sc-content="romaji"], span[data-sc-content="label"],
div[data-sc-content="flavor-source"] { opacity: 0.7; font-size: 0.85em; }
span[data-sc-content="type"], span[data-sc-content="rarity"] { display: inline-block; white-space: nowrap; }
div[data-sc-content="flavor"] { border-left: 0.2em solid rgba(128, 128, 128, 0.5); padding-left: 0.6em; margin-top: 0.4em; }
div[data-sc-content="forms"] { margin-top: 0.4em; }
div[data-sc-content="form"] { display: inline-block; vertical-align: top; width: 7em; margin: 0.2em 0.4em 0.2em 0; text-align: center; font-size: 0.85em; }
'''


class BuildError(RuntimeError):
    pass


def fetch(data_dir, pokeapi_commit, sprites_commit):
    """Download missing CSV tables, then missing artwork for every listed Pokémon."""
    def download(url, target, optional=False):
        if target.exists():
            return
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                payload = response.read()
        except urllib.error.HTTPError as error:
            if optional and error.code == 404:
                return
            raise BuildError(f'cannot download {url}: {error}') from error
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)

    for table in TABLES:
        download(CSV_URL.format(commit=pokeapi_commit, table=table), data_dir / 'csv' / f'{table}.csv')
    for row in read_table(data_dir, 'pokemon'):
        # Alternate forms without official artwork are omitted; species artwork is checked later.
        download(ARTWORK_URL.format(commit=sprites_commit, pokemon_id=row['id']),
                 data_dir / 'artwork' / f"{row['id']}.png", optional=row['is_default'] != '1')


def read_table(data_dir, table):
    path = data_dir / 'csv' / f'{table}.csv'
    try:
        with path.open(encoding='utf-8', newline='') as source:
            return list(csv.DictReader(source))
    except OSError as error:
        raise BuildError(f'cannot read {path}: {error}') from error


def clean_japanese(text):
    return re.sub(r'\s+', '', text)


def clean_spaced(text):
    return re.sub(r'\s+', ' ', text.replace('　', ' ')).strip()


def reading_for(name):
    reading = name
    for source, replacement in READING_REPLACEMENTS.items():
        reading = reading.replace(source, replacement)
    if not READING_PATTERN.fullmatch(reading):
        raise BuildError(f'cannot derive a kana reading for {name}')
    return reading


def span(content, kind=None, style=None):
    node = {'tag': 'span', 'content': content}
    if kind:
        node['data'] = {'content': kind}
    if style:
        node['style'] = style
    return node


def div(content, kind=None):
    node = {'tag': 'div', 'content': content}
    if kind:
        node['data'] = {'content': kind}
    return node


def label(japanese, english):
    return span(f'{japanese} ({english}): ', 'label')


def lookup_link(name):
    return {'tag': 'a', 'href': f'?query={name}&wildcards=off', 'content': name}


def image(path, alt, size):
    return {'tag': 'img', 'path': path, 'width': size, 'height': size, 'sizeUnits': 'em',
            'alt': alt, 'title': alt, 'collapsible': False, 'collapsed': False, 'background': False}


class Dataset:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.species = {row['id']: row for row in read_table(data_dir, 'pokemon_species')}
        if not self.species:
            raise BuildError('no Pokémon species in source data')
        self.pokemon = {row['id']: row for row in read_table(data_dir, 'pokemon')}
        self.varieties = defaultdict(list)
        for row in self.pokemon.values():
            self.varieties[row['species_id']].append(row)

        self.species_names = defaultdict(dict)
        for row in read_table(data_dir, 'pokemon_species_names'):
            self.species_names[row['pokemon_species_id']][row['local_language_id']] = row

        self.type_identifiers = {row['id']: row['identifier'] for row in read_table(data_dir, 'types')}
        self.type_names = self._names('type_names', 'type_id')
        self.ability_names = self._names('ability_names', 'ability_id')
        self.version_names = self._names('version_names', 'version_id')
        self.generations = {row['id']: row['identifier'] for row in read_table(data_dir, 'generations')}

        self.types = defaultdict(list)
        for row in sorted(read_table(data_dir, 'pokemon_types'), key=lambda row: int(row['slot'])):
            self.types[row['pokemon_id']].append(row['type_id'])
        self.abilities = defaultdict(list)
        for row in sorted(read_table(data_dir, 'pokemon_abilities'), key=lambda row: int(row['slot'])):
            self.abilities[row['pokemon_id']].append((row['ability_id'], row['is_hidden'] == '1'))

        default_forms = {}
        for row in read_table(data_dir, 'pokemon_forms'):
            if row['is_default'] == '1':
                default_forms[row['id']] = row['pokemon_id']
        self.form_names = defaultdict(dict)
        for row in read_table(data_dir, 'pokemon_form_names'):
            pokemon_id = default_forms.get(row['pokemon_form_id'])
            if pokemon_id:
                self.form_names[pokemon_id][row['local_language_id']] = row

        self.flavor = defaultdict(lambda: defaultdict(dict))
        for row in read_table(data_dir, 'pokemon_species_flavor_text'):
            if row['language_id'] in (JA, JA_KANA, EN):
                self.flavor[row['species_id']][int(row['version_id'])][row['language_id']] = row['flavor_text']

        self.children = defaultdict(list)
        for row in self.species.values():
            if row['evolves_from_species_id']:
                self.children[row['evolves_from_species_id']].append(row['id'])

    def _names(self, table, key):
        names = defaultdict(dict)
        for row in read_table(self.data_dir, table):
            names[row[key]][row['local_language_id']] = row['name']
        return names

    def species_name(self, species_id, language=JA):
        try:
            return self.species_names[species_id][language]['name']
        except KeyError:
            raise BuildError(f'species {species_id} has no name in language {language}') from None

    def artwork(self, pokemon_id):
        return self.data_dir / 'artwork' / f'{pokemon_id}.png'

    def form_name(self, pokemon_id):
        """Japanese name of a non-default variety, or None when the source leaves it unnamed."""
        names = self.form_names.get(pokemon_id, {})
        row = names.get(JA) or names.get(JA_KANA)
        return row['form_name'].translate(FULL_WIDTH) if row and row['form_name'] else None

    def form_english(self, pokemon_id):
        row = self.form_names.get(pokemon_id, {}).get(EN)
        if not row:
            return ''
        return row['pokemon_name'] or row['form_name']

    def is_standalone_form(self, pokemon_id):
        """True when the form name is a full name in its own right, such as メガリザードンＸ."""
        name = self.form_name(pokemon_id)
        species = self.species_name(self.pokemon[pokemon_id]['species_id'])
        return bool(name) and species in name and name != species and not name.endswith('のすがた')

    def evolution_levels(self, species_id):
        root = species_id
        while self.species[root]['evolves_from_species_id']:
            root = self.species[root]['evolves_from_species_id']
        levels, current = [], [root]
        while current:
            levels.append(current)
            current = [child for parent in current
                       for child in sorted(self.children[parent], key=int)]
        return levels


def type_badges(data, pokemon_id):
    badges = []
    for type_id in data.types[pokemon_id]:
        names = data.type_names[type_id]
        colour = TYPE_COLOURS.get(data.type_identifiers[type_id], '#666666')
        badges.append(span(f"{names[JA]} {names[EN]}", 'type', {
            'color': '#ffffff', 'backgroundColor': colour, 'borderRadius': '0.3em',
            'padding': '0 0.45em', 'marginRight': '0.3em', 'fontSize': '0.85em', 'fontWeight': 'bold'}))
    if not badges:
        raise BuildError(f'Pokémon {pokemon_id} has no types')
    return badges


def measurements(data, pokemon_id, species):
    row = data.pokemon[pokemon_id]
    region_ja, region_en = REGIONS.get(species['generation_id'], ('', ''))
    numeral = data.generations[species['generation_id']].rpartition('-')[2].upper()
    generation = f"第{species['generation_id']}世代"
    return [
        div([label('高さ', 'Height'), f"{int(row['height']) / 10:.1f} m　",
             label('重さ', 'Weight'), f"{int(row['weight']) / 10:.1f} kg"]),
        div([label('初登場', 'Debut'),
             f"{generation}・{region_ja} (Generation {numeral}, {region_en})" if region_ja
             else f'{generation} (Generation {numeral})']),
    ]


def ability_line(data, pokemon_id):
    parts = []
    for ability_id, hidden in data.abilities[pokemon_id]:
        names = data.ability_names[ability_id]
        japanese = names.get(JA) or names.get(JA_KANA)
        english = names.get(EN)
        text = f'{japanese} ({english})' if japanese and english else japanese or english
        if text:
            parts.append(text + ('［隠れ特性 / Hidden］' if hidden else ''))
    return div([label('特性', 'Abilities'), '、'.join(parts)]) if parts else None


def flavor_block(data, species_id):
    versions = data.flavor[species_id]
    candidates = [version for version, texts in versions.items() if JA in texts]
    if not candidates:
        return None
    version = max(candidates)
    texts = versions[version]
    content = [div(clean_japanese(texts[JA]), 'flavor-ja')]
    if JA_KANA in texts:
        content.append(div(clean_spaced(texts[JA_KANA]), 'flavor-kana'))
    if EN in texts:
        content.append(div(clean_spaced(texts[EN]), 'flavor-en'))
    game = data.version_names[str(version)].get(EN)
    content.append(div('ポケモン図鑑 (Pokédex entry' + (f', Pokémon {game})' if game else ')'), 'flavor-source'))
    return div(content, 'flavor')


def evolution_line(data, species_id):
    levels = data.evolution_levels(species_id)
    if len(levels) < 2:
        return None
    content = [label('進化', 'Evolution')]
    for index, level in enumerate(levels):
        if index:
            content.append(' → ')
        for position, member in enumerate(level):
            if position:
                content.append('・')
            name = data.species_name(member)
            content.append(span(name, style={'fontWeight': 'bold'}) if member == species_id
                           else lookup_link(name))
    return div(content, 'evolution')


def species_entry(data, species_id, media):
    species = data.species[species_id]
    default = [row for row in data.varieties[species_id] if row['is_default'] == '1']
    if len(default) != 1:
        raise BuildError(f'species {species_id} needs exactly one default Pokémon')
    pokemon_id = default[0]['id']
    name = data.species_name(species_id)
    english = data.species_name(species_id, EN)
    romaji = data.species_names[species_id].get(ROMAJI, {}).get('name', '')
    if not data.artwork(pokemon_id).exists():
        raise BuildError(f'missing artwork for {name} ({pokemon_id})')
    media.add(pokemon_id)

    title = [span(f'No.{int(species_id):04d} ', 'number'), span(english, style={'fontWeight': 'bold'})]
    if romaji and romaji != english:
        title.append(span(f' [{romaji}]', 'romaji'))
    genus = [data.species_names[species_id][language]['genus']
             for language in (JA, EN) if data.species_names[species_id].get(language, {}).get('genus')]
    badges = type_badges(data, pokemon_id)
    for flag, text in (('is_legendary', '伝説 Legendary'), ('is_mythical', '幻 Mythical'), ('is_baby', 'ベイビィ Baby')):
        if species.get(flag) == '1':
            badges.append(span(text, 'rarity', {
                'borderStyle': 'solid', 'borderWidth': '1px', 'borderRadius': '0.3em',
                'padding': '0 0.45em', 'marginRight': '0.3em', 'fontSize': '0.85em'}))

    content = [div(image(f'img/{pokemon_id}.webp', f'{name} ({english})', 11), 'artwork'), div(title, 'title')]
    if genus:
        content.append(div(' — '.join(genus), 'genus'))
    content.append(div(badges, 'types'))
    content.extend(measurements(data, pokemon_id, species))
    content.extend(filter(None, [ability_line(data, pokemon_id), evolution_line(data, species_id),
                                 flavor_block(data, species_id)]))

    forms = []
    for variety in sorted(data.varieties[species_id], key=lambda row: int(row['id'])):
        form_name = data.form_name(variety['id'])
        if variety['is_default'] == '1' or not form_name or not data.artwork(variety['id']).exists():
            continue
        media.add(variety['id'])
        types = '・'.join(data.type_names[type_id][JA] for type_id in data.types[variety['id']])
        caption = [lookup_link(form_name) if data.is_standalone_form(variety['id']) else form_name]
        if types:
            caption.extend([{'tag': 'br'}, span(types, 'label')])
        forms.append(div([image(f"img/{variety['id']}.webp", f'{name} — {form_name}', 6),
                          div(caption)], 'form'))
    if forms:
        content.append(div([div(label('すがた', 'Forms')), *forms], 'forms'))

    glossary = [{'type': 'structured-content', 'content': div(content, 'pokemon')}]
    return [name, reading_for(name), 'ポケモン', '', 0, glossary, int(species_id), '']


def form_entry(data, pokemon_ids, media):
    """One lookup row per form name; forms sharing a name (メガシャリタツ) share the row."""
    species_id = data.pokemon[pokemon_ids[0]]['species_id']
    species = data.species[species_id]
    name = data.form_name(pokemon_ids[0])
    content = []
    for pokemon_id in pokemon_ids:
        english = data.form_english(pokemon_id)
        media.add(pokemon_id)
        title = [span(f'No.{int(species_id):04d} ', 'number')]
        if english:
            title.append(span(english, style={'fontWeight': 'bold'}))
        content.extend([
            div(image(f'img/{pokemon_id}.webp', f'{name} ({english})' if english else name, 11), 'artwork'),
            div(title, 'title'), div(type_badges(data, pokemon_id), 'types'),
            measurements(data, pokemon_id, species)[0]])
        content.extend(filter(None, [ability_line(data, pokemon_id)]))
    content.append(div([label('元のポケモン', 'Form of'), lookup_link(data.species_name(species_id)),
                        f' ({data.species_name(species_id, EN)})'], 'base'))
    content.extend(filter(None, [flavor_block(data, species_id)]))
    glossary = [{'type': 'structured-content', 'content': div(content, 'pokemon')}]
    return [name, reading_for(name), 'フォルム', '', 0, glossary, int(pokemon_ids[0]), '']


def encode_artwork(path):
    try:
        with Image.open(path) as source:
            picture = source.convert('RGBA')
    except (OSError, ValueError) as error:
        raise BuildError(f'cannot read artwork {path.name}: {error}') from error
    picture.thumbnail((IMAGE_PIXELS, IMAGE_PIXELS), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    picture.save(buffer, 'WEBP', quality=82, method=4)
    return buffer.getvalue()


def build(data_dir, output, revision, pokeapi_commit, sprites_commit):
    data = Dataset(data_dir)
    media = set()
    species_ids = sorted(data.species, key=int)
    terms = [species_entry(data, species_id, media) for species_id in species_ids]
    named_forms = defaultdict(list)
    for pokemon_id in sorted(data.pokemon, key=int):
        if (data.pokemon[pokemon_id]['is_default'] != '1' and data.is_standalone_form(pokemon_id)
                and data.artwork(pokemon_id).exists()):
            named_forms[(data.pokemon[pokemon_id]['species_id'], data.form_name(pokemon_id))].append(pokemon_id)
    terms.extend(form_entry(data, pokemon_ids, media) for pokemon_ids in named_forms.values())

    keys = [(row[0], row[1]) for row in terms]
    if len(keys) != len(set(keys)):
        raise BuildError('duplicate lookup term')

    varieties = [row for row in data.pokemon.values() if row['is_default'] != '1']
    summary = {
        'species': len(species_ids), 'named_form_entries': len(named_forms), 'lookup_entries': len(terms),
        'images': len(media), 'alternate_forms_in_source': len(varieties),
        'alternate_forms_illustrated': len(media) - len(species_ids),
        'alternate_forms_without_artwork': sorted(
            row['identifier'] for row in varieties if not data.artwork(row['id']).exists()),
        'alternate_forms_without_japanese_name': sum(not data.form_name(row['id']) for row in varieties),
    }
    description = (
        f"All {len(species_ids):,} Pokémon species by Japanese name, each with official artwork, English name, "
        'official romanisation, category, types, height, weight, abilities, evolution line and a Pokédex entry '
        f"in kanji, kana and English. {len(named_forms):,} separately named forms such as Mega Evolutions have "
        f"their own entries, and {summary['alternate_forms_illustrated']:,} alternate forms are illustrated."
    )
    index = {
        'title': TITLE, 'format': 3, 'revision': revision, 'sequenced': True, 'author': 'bee-san',
        'url': 'https://github.com/bee-san/yomitan-dictionaries', 'description': description,
        'sourceLanguage': 'ja', 'targetLanguage': 'en',
        'attribution': (
            'Unofficial fan-made reference. Data and artwork files from PokeAPI (https://pokeapi.co). '
            'Pokémon names, Pokédex text and artwork are © Nintendo / Creatures Inc. / GAME FREAK inc.; '
            'Pokémon is a trademark of Nintendo. No reuse licence is granted. See SOURCE.json and RIGHTS.txt.'),
    }
    source = {
        'data': {'repository': 'https://github.com/PokeAPI/pokeapi', 'commit': pokeapi_commit,
                 'path': 'data/v2/csv', 'tables': list(TABLES)},
        'artwork': {'repository': 'https://github.com/PokeAPI/sprites', 'commit': sprites_commit,
                    'path': 'sprites/pokemon/other/official-artwork',
                    'transformation': f'Resized to fit {IMAGE_PIXELS}x{IMAGE_PIXELS} px and re-encoded as lossy WebP.'},
        'summary': summary,
        'rules': {
            'headwords': 'Official Japanese species names. Latin letters and digits use full-width forms.',
            'readings': 'The Japanese name with ♀, ♂, ２, Ｘ, Ｙ, Ｚ spelled out in kana and ： removed.',
            'named_form_entries': 'Alternate forms whose Japanese form name contains the species name.',
            'pokedex_text': 'The most recent game version with a Japanese kanji entry; kana and English '
                            'text come from the same version. Kana spacing is as published by PokeAPI.',
            'omitted_forms': 'Alternate forms with no Japanese form name or no official artwork.',
        },
    }
    rights = (
        'Pokémon Zukan (Illustrated) is an unofficial, fan-made Yomitan dictionary.\n\n'
        'Pokémon and all related names, Pokédex text and artwork are\n'
        '© Nintendo / Creatures Inc. / GAME FREAK inc. Pokémon is a trademark of Nintendo.\n'
        'This archive is not affiliated with, sponsored or endorsed by any of them.\n\n'
        'The data tables and artwork files were obtained from the PokeAPI project\n'
        '(https://github.com/PokeAPI/pokeapi and https://github.com/PokeAPI/sprites).\n'
        'PokeAPI does not own the underlying Pokémon material either.\n\n'
        'No licence over the names, text or artwork is granted by this archive.\n'
        'It is provided for personal language study. Preserve this notice.\n'
    )
    tags = [['ポケモン', 'name', 0, 'Pokémon species', 0],
            ['フォルム', 'name', 0, 'Separately named form of a Pokémon, such as a Mega Evolution', 0]]

    members = {'index.json': index, 'SOURCE.json': source, 'tag_bank_1.json': tags}
    for start in range(0, len(terms), BANK_SIZE):
        members[f'term_bank_{start // BANK_SIZE + 1}.json'] = terms[start:start + BANK_SIZE]

    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'w') as archive:
        def write(name, payload, compression=zipfile.ZIP_DEFLATED):
            info = zipfile.ZipInfo(name, ZIP_DATE)
            info.compress_type = compression
            archive.writestr(info, payload)

        for name, payload in members.items():
            write(name, json.dumps(payload, ensure_ascii=False, separators=(',', ':')) + '\n')
        write('styles.css', STYLES)
        write('RIGHTS.txt', rights)
        for pokemon_id in sorted(media, key=int):
            write(f'img/{pokemon_id}.webp', encode_artwork(data.artwork(pokemon_id)), zipfile.ZIP_STORED)
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None:
            raise BuildError('output CRC failure')
    return {'file': output.name, 'title': TITLE, 'revision': revision, 'description': description,
            'lookup_entries': len(terms), 'bytes': output.stat().st_size,
            'sha256': hashlib.sha256(output.read_bytes()).hexdigest(), 'summary': summary}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('data_dir', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--pokeapi-commit', required=True, help='PokeAPI/pokeapi commit of the CSV tables')
    parser.add_argument('--sprites-commit', required=True, help='PokeAPI/sprites commit of the artwork')
    parser.add_argument('--fetch', action='store_true', help='download missing source files first')
    args = parser.parse_args(argv)
    try:
        if args.fetch:
            fetch(args.data_dir, args.pokeapi_commit, args.sprites_commit)
        report = build(args.data_dir, args.output, args.revision, args.pokeapi_commit, args.sprites_commit)
    except BuildError as error:
        parser.exit(1, f'ERROR: {error}\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

# Bee's Yomitan Dictionaries

Custom Japanese dictionaries for Yomitan, covering places, culture, food, folklore, poetry and grammar.

[![Latest release](https://img.shields.io/github/v/release/bee-san/yomitan-dictionaries?label=download)](https://github.com/bee-san/yomitan-dictionaries/releases/latest)
[![Dictionaries](https://img.shields.io/badge/dictionaries-7-6f42c1)](https://github.com/bee-san/yomitan-dictionaries/releases)
[![Archive tests](https://img.shields.io/badge/ZIP%20tests-passing-brightgreen)](CHECKSUMS.sha256)

The ZIP files are published as [GitHub release assets](https://github.com/bee-san/yomitan-dictionaries/releases/latest), not committed to Git. Each archive is ready to import directly into Yomitan and includes its own source and attribution metadata.

## Quick start

1. Download one or more ZIP files from the [latest release](https://github.com/bee-san/yomitan-dictionaries/releases/latest).
2. Do not unzip them.
3. Open Yomitan's settings, go to **Dictionaries**, choose **Import**, and select the ZIP.
4. Wait for the import to finish. The illustrated dictionaries can take a while, especially on Android or low-memory devices.

Downloading or syncing a ZIP does not automatically import it into Yomitan.

## Dictionary catalogue

| Dictionary | What it contains | Lookup entries | Download size | Download |
|---|---|---:|---:|---|
| **Japanese Cities & Wards** | 815 Japanese cities and wards with readings, prefectures, populations, maps and attributed lead images | 815 | 125.3 MB | [ZIP](https://github.com/bee-san/yomitan-dictionaries/releases/download/2026.09.01/japanese-cities-yomitan.zip) |
| **Illustrated Ogura Hyakunin Isshu** | All 100 poems with Japanese text, historical kana, poets, source anthologies, public-domain English translations, notes and historical cards | 900 | 3.7 MB | [ZIP](https://github.com/bee-san/yomitan-dictionaries/releases/download/2026.09.01/japanese-hyakunin-isshu-yomitan.zip) |
| **Japanese Railway Stations** | 11,439 current and historical stations and stops, including readings, lines, operators, prefectures, addresses, dates and coordinates | 12,750 | 2.5 MB | [ZIP](https://github.com/bee-san/yomitan-dictionaries/releases/download/2026.09.01/japanese-railway-stations-yomitan.zip) |
| **Japanese Regional Cuisine** | 1,015 illustrated regional dishes with readings, prefectures, traditional areas, ingredients, history and Japanese/English descriptions | 1,139 | 156.7 MB | [ZIP](https://github.com/bee-san/yomitan-dictionaries/releases/download/2026.09.01/japanese-regional-cuisine-yomitan.zip) |
| **Japanese Traditional Colours** | 227 traditional colour names with readings, romanisation, English meanings, exact RGB/hex values and generated swatches | 228 | 754 KB | [ZIP](https://github.com/bee-san/yomitan-dictionaries/releases/download/2026.09.01/japanese-traditional-colors-yomitan.zip) |
| **Japanese Yōkai Encyclopedia** | 387 yōkai and supernatural phenomena with readings, variants, Japanese descriptions, classifications and 279 open or public-domain illustrations | 715 | 41.2 MB | [ZIP](https://github.com/bee-san/yomitan-dictionaries/releases/download/2026.09.01/japanese-yokai-encyclopedia-yomitan.zip) |
| **日本語文型バンク (NINJAL 2026.01)** | 2,394 Japanese grammar patterns with official explanations, connection rules, levels, categories and examples | 2,394 | 2.7 MB | [ZIP](https://github.com/bee-san/yomitan-dictionaries/releases/download/2026.09.01/ninjal-bunkei-yomitan-2026.01.33.zip) |

The complete release is 332.8 MB. `CATALOGUE.json` provides machine-readable titles, revisions, sizes and SHA-256 hashes.

## A little more about each dictionary

### Japanese Cities & Wards

A visual place-name reference for all 815 included cities and wards. It is useful when a novel, game, news article or train announcement drops a municipality name without explaining where it is. The underlying city deck is MIT licensed. Every Wikipedia/Wikimedia image has its creator, source page and licence recorded in `wikipedia_image_attribution.json` inside the archive. The release copy also bundles the GFDL and Free Art License texts referenced by that manifest.

### Illustrated Ogura Hyakunin Isshu

A lookup-friendly edition of the classical anthology rather than a flat list of poems. It indexes poems through several useful forms, which is why 100 poems produce 900 lookup entries. Japanese Wikisource transcription is CC BY-SA 4.0. William N. Porter's 1909 translation and the historical card images are public domain.

### Japanese Railway Stations

A large proper-noun dictionary built from `station_database` snapshot `v20260731`. It covers 8,989 open and 2,450 closed stations and stops. Original station names remain the primary terms, while disambiguated aliases make homonymous stations easier to inspect. The transformed compilation follows the upstream CC BY-SA 4.0 licence.

### Japanese Regional Cuisine

An illustrated food dictionary based on the Japanese Ministry of Agriculture, Forestry and Fisheries collection *うちの郷土料理*. It includes regional context, ingredients, history and bilingual descriptions. Text is adapted under Japan's Public Data License 1.0. Images are limited to recipe pages that explicitly provide permission-free downloadable image ZIPs, with providers and source pages recorded inside the archive.

### Japanese Traditional Colours

A compact visual reference for traditional colour vocabulary. Each entry includes its exact numeric colour values, a readable swatch and contrast guidance. The data comes from an attributed English Wikipedia revision and is distributed under CC BY-SA 4.0.

### Japanese Yōkai Encyclopedia

A monolingual illustrated reference assembled from Wikidata, Japanese Wikipedia and freely reusable Wikimedia Commons media. It is useful for folklore-heavy games and visual novels where ordinary dictionaries often provide only a one-line gloss. Exact article revisions, creators, media licences and transformations are bundled in the attribution manifests.

### 日本語文型バンク (NINJAL 2026.01)

A Yomitan build of the National Institute for Japanese Language and Linguistics' 日本語文型データベース. It keeps the official Japanese explanations, connection patterns, levels, categories and examples. Source DOI: [`10.15084/0002000610`](https://doi.org/10.15084/0002000610). Source licence: CC BY 4.0.

## Why some custom dictionaries are not published

The local collection contains ten more custom builds, but their current archives are not safe to redistribute as-is:

- **Former Provinces of Japan**: converted from a local Anki deck whose archive says it is not prepared for redistribution.
- **Japanese Kanji Phonetic Families**: converted from a local Anki deck whose archive says it is not prepared for redistribution.
- **Japan's Lakes**: based partly on a personal-use source deck with unresolved provenance.
- **Japanese Proper Nouns Mk II**: the original deck author and distribution terms are unidentified.
- **Japanese Public Holidays**: based on a local deck whose redistribution terms are unresolved.
- **Illustrated Shinto Kami & Japanese Mythology**: the archive says image terms must be checked before redistribution.
- **Japan's METI Traditional Crafts**: the archive explicitly says the images are private-study only and not to redistribute it.
- **Japan's 100 Famous Mountains**: the image manifest links to source pages but does not bundle each photograph's exact credit and licence.
- **Japanese Kamon Encyclopedia**: some GFDL media still needs complete author and licence packaging.
- **National Parks of Japan**: some GFDL media still needs complete licence and creator metadata.

They are deliberately excluded rather than being published with vague or missing rights information.

## Integrity

Every published asset passed Python's complete ZIP CRC test, contains a parseable Yomitan `index.json`, and was scanned for accidental local paths and private identifiers.

After downloading the assets and `CHECKSUMS.sha256` into the same directory, verify them with:

```bash
shasum -a 256 -c CHECKSUMS.sha256
```

## Attribution and licences

These are custom dictionary compilations, not claims of ownership over the underlying source material. There is no blanket licence covering every archive. Each ZIP contains its own source, attribution and licence records, and those embedded notices take precedence.

If you redistribute an archive or reuse its contents, preserve its attribution files and follow every upstream licence named inside it.

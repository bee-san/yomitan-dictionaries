# Grammar source contracts

This document is the public-safe contract between source adapters, the canonical model, the merger, and the final Yomitan packager. It records source decisions, not source content or permission beyond the cited source.

## Policy boundary

Registry version 1 separates three questions:

1. Access: how bytes may lawfully enter a local build (`local-file`, `public-http`, `metadata-only`, or `unavailable`).
2. Import: whether source content may enter Bee's private canonical build (`content`, `metadata-only`, or `denied`).
3. Publication: whether copied source content is cleared for redistribution (`allowed`, `denied`, or `uncleared`).

These fields are independent. In particular, `bee-bunpo` and the five selected community archives allow local content import but deny publication. A code licence, a public web page, or an available archive never upgrades content publication policy. Only registry version changes backed by new evidence can relax policy. A source manifest or adapter may be more restrictive than the registry, never more permissive.

A metadata selection includes only this registry's source name, homepage, status, and attribution note. It does not include source lesson bodies or copied explanations. Publication policy applies to copied content; public-safe metadata/link records do not launder that content.

## Version 1 source registry

| Source ID | Source | Access / import | Content publication | Selection decision |
| --- | --- | --- | --- | --- |
| `bee-bunpo` | Bee 文法 | local file / content | denied | required private source |
| `bunpro` | Bunpro grammar points | public HTTP / content | uncleared | required private source |
| `bunpro-public-metadata` | Bunpro identity/formation metadata | metadata only | uncleared | public canary metadata only |
| `bunpro-josh-cache` | J-O-S-H-L Bunpro cache | public HTTP / content | uncleared | secondary private cache |
| `ninjal-bunkei` | NINJAL 日本語文型データベース | public HTTP / content | allowed, CC BY 4.0 | selected open source |
| `nihongo-kyoshi` | Nihongo Kyoshi / 日本語NET | local file / content | denied | selected private archive |
| `donna-toki` | Donna Toki | local file / content | denied | selected private archive |
| `e-de-wakaru` | E de wakaru | local file / content | denied | selected private archive |
| `dojg` | Dictionary of Japanese Grammar | lawful local file / content | denied | selected private archive |
| `nihongo-no-sensei` | Nihongo no sensei | local file / content | denied | selected private archive |
| `imabi` | IMABI | metadata only | denied | link-only; lesson content deferred |
| `tae-kim` | Tae Kim's Guide | metadata only | uncleared | deferred/link-only |
| `jlpt-sensei` | JLPT Sensei | metadata only | uncleared | deferred/link-only |
| `maggie-sensei` | Maggie Sensei | metadata only | uncleared | deferred/link-only |
| `japanese-wikibooks` | Japanese Wikibooks | metadata only | allowed under CC BY-SA 4.0 | open but deferred as too textbook-like |
| `nihongo-kyoshi-n1et` | 日本語教師のN1et | metadata only | uncleared | deferred backlink source |
| `nihon5-bunka` | 日本の言葉と文化 | metadata only | uncleared | deferred backlink source |
| `japanese-bank` | 日本語教師キャリア マガジン | metadata only | uncleared | deferred backlink source |
| `yokubi` | Yokubi | public HTTP / content | allowed, CC BY 4.0 | selected open, unfinished snapshot |

The executable definitions, exact host allowlists, source-family relationships, and attribution requirements live in `scripts/grammar/registry.py`. Unknown source IDs and non-allowlisted remote hosts fail closed.

## Reconciled source evidence

### Bee 文法

The pinned source archive has SHA-256 `67f82da8316e0c9c33346bc0480b7b15c5ebb6b85c8c41bdf9ca487629f931f6`, 534 source-note sequences, 1,324 lookup rows, 790 alias rows, 14,975 non-empty field sections, 534 AI-warning sections, and two images. Source-note count and lookup-row count are different measures and must remain separate.

Bee states that the supplied work is their own. That later declaration must be preserved as separate provenance from the archive's older unverified-rights wording. It is not a licence for 日本語NET, Bunpro, images, or any other third-party material. Existing AI fields remain generated and visibly labelled.

### Bunpro

The private ingestion source of truth is the official grammar-point HTML and sitemap. Sampled N5/N3/N1 pages carry substantive explanations, formation information, examples, level labels, and source links. Badge-only generators are insufficient. The J-O-S-H-L cache at commit `010fb773945993217f3f2426f996d9c251341254` contains 913 cached grammar pages and four term banks, but is under construction and has no located content licence. Bunpro-derived content therefore remains private and publication-uncleared.

### NINJAL

The official DOI is `10.15084/0002000610`, audited at Version 2026.01 under CC BY 4.0. Evidence reports 800 source headwords and 2,394 rows in the existing first Yomitan term bank. Adapters must report source records and output lookup rows separately, preserve NINJAL's own level/category system, and must not relabel it as JLPT.

### Selected community archives

The measured archive row counts are Nihongo Kyoshi 170, Donna Toki 1,082, E de wakaru 309, DoJG 535, and Nihongo no sensei 478. These are local/private import candidates, not redistribution-cleared corpora. Nihongo Kyoshi overlaps the 日本語NET source family already linked by Bee 文法 and is not independent corroboration. Donna Toki's former live page returned 404 during audit. The Nihongo no sensei domain returned unrelated content, so the archive is provenance evidence and the live domain is not an acquisition authority. DoJG is copyrighted book-derived material and requires a lawful user-supplied local copy.

### IMABI

IMABI is metadata/link-only. The public site says no offline format is available, its terms reserve rights and prohibit text/data mining and web scraping, and no permitted export route was found. Store lesson ID, title, canonical URL, table-of-contents section, and modern/classical label only. Do not copy lesson bodies. A later content adapter requires a lawful local export or explicit permission and a registry policy revision.

### Yokubi

Yokubi is a first-class selected source pinned by adapter manifests to an exact repository revision; the audited starting commit is `b1c0938b0bda58e20c6ccd21288b46711b438239`. The repository is CC BY 4.0. Preserve Morgawr and contributor attribution, credited Sakubi origins, file-level notices, and chapter/section anchors. Treat the snapshot as an unfinished rewrite. Do not infer grammar-point boundaries from editorial chapters, and do not present shared upstream passages as independent evidence.

## Canonical source format

A source adapter emits one UTF-8 JSON object:

- `format`: `ugd-canonical-source`
- `format_version`: `1`
- `source_revision`: one `SourceRevision`
- `records`: source-ordered `SourceRecord` objects
- `senses`: source-qualified `SourceSense` objects
- `media`: `MediaRecord` metadata; media bytes remain in ignored local storage until packaging

The typed records in `scripts/grammar/model.py` define the exact fields:

- `SourceRevision`: immutable source/revision IDs, exact accepted-source SHA-256, languages, attribution, licence, access/import/publication modes, source-level generated flag, and provenance note.
- `SourceRecord`: immutable source-qualified record ID, exact raw expression, source order, and exact raw-field digests.
- `SourceSense`: immutable source-qualified sense ID, record ID, partition status, optional curated concept reference, ordered blocks/examples, source-scoped level/register labels, and source links.
- `ContentBlock`: stable block ID, kind, BCP-47 language, JSON content, source order, exact provenance, and generated flag.
- `ExamplePair`: stable example ID, Japanese content, optional source-supplied translation with its own language, source order, provenance, and generated flag. Never pair unequal lists by position.
- `MediaRecord`: stable media ID, safe relative source path, content hash, MIME type, byte count, and provenance.
- `CanonicalSense`: one grammar sense identity, display expression, exact source-sense memberships, optional broader concept identity, and ambiguity state.

IDs must be source qualified, for example:

- record: `bee-bunpo:2026.09.08-v1:record:110`
- sense: `bee-bunpo:2026.09.08-v1:record:110:sense:1`

When an upstream has a stable ID, adapters use it instead of mutable row order. Otherwise they derive a stable local record key from source evidence and document the derivation. `canonical_sense_id()` hashes exact source-sense memberships only; it never hashes spelling, gloss text, vector similarity, or mutable ordering.

## Non-negotiable model invariants

- Equal spelling does not merge senses. Cause/purpose `〜ために`, the three baseline `ものだ` records, and distinct `ように` records retain distinct identities.
- NFC comparison keys may be derived, but raw strings and U+301C/U+FF5E distinctions are preserved. NFKC is not an identity operation.
- Internal gap markers remain gaps. `〜ば〜ほど` cannot produce `ばほど`, `ば`, or `ほど`, and scanning remains manual-only until real Yomitan evidence supports a reviewed anchor.
- One-hiragana generated aliases fail closed unless an exact reviewed override cites real scanning evidence.
- Source level/register labels remain source scoped. Conflicts are displayed, not silently resolved.
- Generated blocks and examples retain `generated: true` and visible warnings. They cannot supply an unlabelled canonical explanation or translation.
- Different-language blocks remain separate. No new grammar explanation or translation is generated by default.
- Every source record, sense, block, example, and media identity must survive into coverage reports. Aggregate counts are not a substitute for exact ID-set containment.
- Record, sense, block, example, and media IDs are unique within a source bundle. Every sense and media provenance reference resolves to a record in that same bundle.

## Adapter responsibilities

Each adapter must:

1. acquire only through the registry's permitted mode and exact host allowlist;
2. pin upstream revision and accepted-source SHA-256;
3. emit the versioned canonical source format with complete provenance;
4. preserve every eligible source field or record an explicit, reversible exclusion reason;
5. keep source bytes and private canonical outputs under `.grammar/`, which Git ignores;
6. use public tests containing only synthetic text, short public metadata, IDs, and hashes;
7. report expected/imported/rejected source ID sets and content versus metadata status; and
8. avoid claiming a full source from a sample fixture or link-only record.

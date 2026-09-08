# Add a grammar source

Community adapters emit the canonical source format defined in `source-contracts.md`. Raw ZIPs, checked-out source trees, canonical JSON, coverage reports, and media bytes stay under `.grammar/`, which Git ignores.

## Safety and policy boundary

A source must already exist in `scripts/grammar/registry.py`. The registry independently controls access, private import, and content publication. An adapter may preserve or tighten those decisions; it must not relax them. In particular:

- a downloadable dictionary does not establish redistribution permission;
- a converter's code licence does not license copied dictionary text;
- DoJG, Donna Toki, and the other selected community archives remain private/local inputs even when their format is supported;
- unknown source IDs and unrelated general dictionaries are refused;
- no adapter logs in, bypasses a gate, or silently follows a moving source revision.

## Generic Yomitan v3 adapter

`scripts.grammar.sources.yomitan.adapt_yomitan_archive()` accepts one local Yomitan v3 ZIP and an allowlisted source ID. It:

1. reads a regular, non-symlink file into a bounded snapshot;
2. rejects unsafe/duplicate/encrypted/symlink members, CRC errors, oversized expansion, malformed JSON, non-contiguous term banks, and malformed eight-field rows;
3. maps every term row to one source-qualified record and source sense;
4. preserves the complete original row, index metadata, tag-bank hashes, bank/row locator, field hashes, and immutable archive hash;
5. resolves every referenced local image/audio member and assigns a source-namespaced, content-addressed target path; and
6. reports auxiliary archive members explicitly instead of treating them as imported grammar content.

Revision identity is `yomitan-<full archive SHA-256>`, not the mutable dictionary title. Two archives with the same title but different bytes therefore cannot collide. Exact-byte mirrors are detected before batch import, and only the first reviewed source is imported. A `sequenced: true` flag is not trusted when every multi-row entry uses the same sentinel sequence; those rows remain `needs-review` instead of being falsely grouped into one concept.

Example for an already-authorized local input:

```python
from pathlib import Path
from scripts.grammar.model import canonical_json_bytes
from scripts.grammar.sources.yomitan import adapt_yomitan_archive

adapted = adapt_yomitan_archive(
    Path(".grammar/inputs/source.zip"),
    "donna-toki",
    expected_archive_sha256="<previously recorded lowercase SHA-256>",
    expected_rows=1082,
    expected_revision="donna_v1.04;2022-04-30(completed arrow internal links)",
)
Path(".grammar/donna-toki.canonical.json").write_bytes(
    canonical_json_bytes(adapted.bundle.to_dict())
)
```

A digest or count mismatch is fatal. The archive digest must come from a separately reviewed manifest or evidence record; computing it from the candidate in the same invocation would not authenticate the input. Counts accompany exact source-record IDs and hashes; they do not replace identity coverage.

## Vetted community batch

`adapt_community_sources(local_inputs, archive_digest_pins=..., yokubi_root=...)` accepts only the five reviewed local archive IDs. Every available archive needs a separately recorded digest pin. The batch never downloads missing private inputs. Each candidate gets exactly one `imported`, `link-only`, `unavailable`, or `excluded` outcome with evidence and a reason.

Current source-by-source result from the real bounded verification on 8 September 2026:

| Source | Result | Content count | Evidence / limitation |
| --- | --- | ---: | --- |
| Nihongo Kyoshi / 日本語NET | unavailable | 0 / 170 rows | Adapter contract is ready; lawful local `日本語NET(nihongo_kyoushi)_v1_03.zip` was absent. It overlaps 日本語NET links already present in Bee's deck. |
| Donna Toki | imported | 1,082 / 1,082 rows | The retained local private archive passed CRC, revision, row, and exact-ID coverage. Archive SHA-256: `b91845b2a565bce855b3eca593ae1441eb6b30c49486e0a163e2ec1e570d21ff`. Publication remains denied. |
| E de wakaru | unavailable | 0 / 309 rows | Adapter contract is ready; lawful local `edewakaru_v_1_03.zip` was absent. |
| DoJG | unavailable | 0 / 535 rows | Adapter contract is ready, but requires Bee's lawful local `dojg-consolidated-v1_01.zip`. Format availability is not redistribution clearance. |
| Nihongo no sensei | unavailable | 0 / 478 rows | Adapter contract is ready; lawful local `nihongo_no_sensei_1_04.zip` was absent. The formerly relevant live domain is not an acquisition authority. |
| Yokubi | imported | 50 / 64 lessons | Pinned open snapshot described below; 14 lessons were conservatively skipped rather than given invented grammar-point boundaries. |
| Tae Kim | link-only | 0 | No selected maintained Yomitan archive or open-content grant. |
| JLPT Sensei | link-only | 0 | No selected standalone permitted export; member-gated content was not acquired. |
| Maggie Sensei | link-only | 0 | No selected standalone permitted export or open-content grant. |
| Japanese Wikibooks | excluded | 0 | CC BY-SA 4.0 candidate, but deliberately deferred as broad textbook-like content. |
| 日本語教師のN1et | excluded | 0 | Backlink source; Donna Toki-derived level labels are not independent coverage. |
| 日本の言葉と文化 | excluded | 0 | General grammar index, not a selected dictionary input. |
| 日本語教師キャリア マガジン | excluded | 0 | General grammar list, not a selected dictionary input. |
| NINJAL 日本語文型データベース | excluded here | 0 | Selected CC BY 4.0 input belongs to the dedicated NINJAL adapter, so it is not duplicated by this batch. |

“Unavailable” describes this adapter workspace, not a claim that the work cannot ever be imported. Supplying a lawful local file with the exact audited revision enables a full validation run. No small test fixture is counted as source coverage.

## Yokubi

Yokubi is pinned to commit `b1c0938b0bda58e20c6ccd21288b46711b438239`. The adapter verifies the checkout revision when Git metadata is present, the repository and Credits CC BY 4.0 notices, `book.toml`, `src/SUMMARY.md`, and an exhaustive reviewed map of all 64 lesson files.

The source contains no grammar-point subheadings beneath each lesson. To avoid inventing boundaries, the adapter keeps one canonical sense per imported lesson and records every title-backed grammar concept on that lesson. Editorial or overly broad lessons are skipped with a per-path reason. Navigation, section overviews, Credits, and other non-lesson Markdown are `not-applicable`, not silently dropped.

Pinned full-source verification produced:

- source-tree SHA-256 `dc77d2fd54623cae7dbc9ecab52369c2d83d440c33827933deefdb1b2f14909b` across 78 pinned input files;
- 64 lessons exhaustively accounted for: 50 imported and 14 skipped;
- 12 additional Markdown files marked not applicable;
- 132 explicit lesson-to-concept mappings;
- 569 exact source-text blocks, including 106 formation-classified blocks; and
- 394 conservative source-provided example/translation pairs. Preformatted tables, paradigms, and English-only contrasts remain source blocks instead of being mislabeled as examples.

The snapshot is explicitly unfinished. Attribution retains Morgawr/Yokubi contributors and credited Sakubi origins. Source links use commit-pinned GitHub lesson URLs. The adapter does not claim each concept is an independently delimited upstream entry and does not generate explanations, translations, or point boundaries.

## Adding another source

1. Add evidence-backed policy to the versioned registry. Record access, private import, publication, licence evidence, attribution, source family, and exact allowed hosts separately.
2. Add a failing public-safe synthetic regression. Exercise malformed input, identity, media namespace, source-family duplicates, and expected source-ID coverage as applicable.
3. Use an immutable revision and SHA-256. For local restricted content, accept an explicit `.grammar/` path; do not add an implicit downloader.
4. Map every eligible source record and preserve raw content, source locators, notices, IDs, and per-field/content hashes. Report every exclusion with a reversible reason.
5. Run the focused adapter tests and `python3 -m unittest discover -s tests -v`.
6. Commit only adapter code, synthetic fixtures, and public-safe documentation. Never commit the source bytes, private canonical output, generated combined archive, or a machine-local path.

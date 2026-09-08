# Reproducible grammar build foundation

`scripts/grammar/build.py` combines explicit, adapter-produced canonical source documents into one deterministic canonical snapshot. It is not the final Yomitan packager; merging, rendering, and ZIP packaging are later pipeline stages.

Private mode is the default. A publishable build is a stricter policy check, not permission to publish anything.

## Local directories

Keep manifests, source bytes, caches, canonical adapter outputs, reports, and built dictionaries out of Git:

```sh
mkdir -p .grammar/inputs .grammar/cache .grammar/build
```

The repository ignores `.grammar/` and all ZIP archives. Do not put private source content in tests, documentation, commits, workflow logs, or public Actions artifacts.

## Two hashes per content source

Every content selection has two independent pins:

- `source_content_sha256`: hash of the exact upstream source accepted by the adapter. This must equal `source_revision.content_sha256` inside the canonical source document.
- `input.sha256`: hash of the exact canonical JSON document consumed by this build.

This keeps upstream provenance distinct from an adapter-output hash. A revision ID is also mandatory; a newer source cannot silently replace an older revision with the same ID.

## Source manifest version 1

Create `.grammar/manifest.json`:

```json
{
  "format": "ugd-source-manifest",
  "format_version": 1,
  "registry_version": 1,
  "build_mode": "private",
  "sources": [
    {
      "source_id": "bee-bunpo",
      "revision_id": "2026.09.08-v1",
      "selection": "content",
      "source_content_sha256": "67f82da8316e0c9c33346bc0480b7b15c5ebb6b85c8c41bdf9ca487629f931f6",
      "input": {
        "path": "inputs/bee-bunpo.canonical.json",
        "sha256": "REPLACE_WITH_CANONICAL_JSON_SHA256"
      }
    },
    {
      "source_id": "imabi",
      "revision_id": "metadata-current",
      "selection": "metadata"
    }
  ]
}
```

Paths are relative to the manifest directory and cannot escape it through `..` or symlinks. Absolute input paths are rejected. This prevents local machine paths from entering output provenance. Copy or generate inputs below `.grammar/inputs/` rather than committing a machine-specific path.

Each source ID may appear once. The source ID must exist in registry version 1. `selection` is exactly `content` or `metadata`. A metadata selection has no input or source-content hash and copies no source body.

### Remote canonical input

A remote canonical input must use HTTPS, end on an allowlisted source host after redirects, and have an explicit relative cache path:

```json
"input": {
  "url": "https://raw.githubusercontent.com/Morgawr/yokubi/REVISION/path/to/pinned-canonical.json",
  "cache": "cache/yokubi-REVISION.canonical.json",
  "sha256": "REPLACE_WITH_CANONICAL_JSON_SHA256",
  "max_bytes": 67108864
}
```

Use this shape only when that exact canonical document actually exists at the pinned URL and the registry declares the source `public-http`. A `local-file` source cannot be upgraded to remote acquisition by a manifest, even when the host appears in its link allowlist. Normally a source adapter acquires the raw pinned source and writes a local canonical JSON file, then the build uses `path`.

Downloads use a 20-second request timeout, at most three attempts, 1 MiB streaming chunks, a 64 MiB default input limit, and a 256 MiB hard limit. Cached bytes are reused only after SHA-256 verification. A bad cache is retained for diagnosis and replaced atomically only after a verified refresh succeeds. Every redirect target is checked against the source allowlist before it is followed, then the final URL is checked again. Local files are opened component by component from a root directory descriptor with no-follow checks, so replacing a validated path component with a symlink cannot redirect the read outside the manifest tree. Authentication embedded in URLs, HTTP, custom ports, unknown hosts, missing pins, oversized input, and hash mismatches fail closed.

## Canonical source document

Adapters emit the schema described in `docs/grammar/source-contracts.md`. A minimal synthetic shape is:

```json
{
  "format": "ugd-canonical-source",
  "format_version": 1,
  "source_revision": {
    "source_id": "bee-bunpo",
    "revision_id": "fixture-r1",
    "content_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "languages": ["ja", "en"],
    "attribution": "Synthetic fixture attribution",
    "license": {
      "identifier": null,
      "notice": "Synthetic fixture; no third-party source text.",
      "evidence_url": null
    },
    "access_mode": "local-file",
    "import_mode": "content",
    "publication_mode": "denied",
    "generated": false,
    "provenance_note": "Synthetic fixture only."
  },
  "records": [],
  "senses": [],
  "media": []
}
```

Production adapters must not emit empty coverage merely because this schema permits an empty intermediate source document. Their own source-count and ID-set tests define eligibility and completeness.

## Run the build

```sh
python scripts/grammar/build.py \
  --source-manifest .grammar/manifest.json \
  --output .grammar/build/combined.canonical.json \
  --report .grammar/build/combined.report.json
```

The required interface is:

```text
python scripts/grammar/build.py --source-manifest PATH --output PATH --report PATH
```

`--mode private|publishable` optionally overrides `build_mode`. If neither is supplied, mode is `private`.

The builder validates the complete manifest and every selected input before writing. Duplicate JSON object keys, boolean version values, and output/report paths that collide with the manifest, a pinned local input, a remote cache, or the reserved lock namespace fail closed. A remote cache is preflighted before any download and cannot replace the manifest, output, or report. The builder then writes canonical JSON with sorted keys, compact separators, UTF-8 text, a trailing newline, registry/source ordering by immutable IDs, and source-internal ordering by explicit order and stable IDs. Output and report replacement is a paired transaction: hashed per-destination advisory locks in an exclusive OS temporary-directory namespace serialize every overlapping writer, and if either install fails, prior files are restored. Manifests, inputs, caches, outputs, and reports are refused inside that lock namespace so another build cannot replace a live lock. Reports contain hashes, counts, source IDs, and the registry access/import/publication policy for every selection, but no input or output filesystem paths.

The output reports these counts separately:

- selected sources;
- content sources and metadata-only sources;
- source records;
- source senses;
- blocks;
- examples; and
- media records.

A report count does not prove preservation. Adapter and integration reports must also compare exact source ID sets.

## Publication checks

`private` permits content only when the registry and source revision both permit local content import. It does not inspect or weaken publication status.

`publishable` permits content only when both the registry and the exact source revision say `publication_mode: allowed`. `denied` and `uncleared` both fail. The revision must also match the registry licence identifier, include the registry's attribution contract in its source attribution, and provide an HTTPS licence-evidence URL. This means a private build may include `bee-bunpo`, Bunpro, or selected local archives while a publishable build containing any of them is rejected before output is written.

Metadata-only selections never include source bodies. Their public-safe registry fields can remain in a publishable build without claiming source-content inclusion.

No successful `publishable` build authorizes a GitHub release. Public distribution still requires explicit user approval and the later release gate.

## Adapter branch contract

All parallel adapter cards start from the exact remote foundation SHA recorded in the UGD-06 Kanban handoff:

```sh
git fetch origin ugd/foundation
git switch --detach FOUNDATION_SHA
git switch -c ugd/SOURCE-adapter
```

Before push, each adapter records:

- foundation SHA and adapter branch;
- one or more exact adapter commit SHAs;
- source ID and immutable upstream revision/hash;
- changed paths;
- targeted and full test commands/results;
- expected/imported/rejected identity sets and separate lookup counts; and
- rights/access limitations.

Do not merge adapter branches into `main`. UGD-12 owns integration.

## Deterministic UGD-12 integration

UGD-12 creates `ugd/integration` from the UGD-06 foundation SHA, verifies every adapter descends from that exact base, and cherry-picks exact handed-off commits in this fixed source order:

1. Bee 文法 (`bee-bunpo`)
2. Bunpro (`bunpro`)
3. NINJAL (`ninjal-bunkei`)
4. community inputs sorted by registry source ID, including Yokubi
5. IMABI metadata (`imabi`)
6. any separately linked, completed source-adapter cards sorted by task ID

Example verification and integration skeleton:

```sh
git fetch origin
git merge-base --is-ancestor FOUNDATION_SHA ADAPTER_SHA
git switch --detach FOUNDATION_SHA
git switch -c ugd/integration
git cherry-pick UGD07_COMMIT_SHA UGD08_COMMIT_SHA UGD09_COMMIT_SHA UGD10_COMMIT_SHA UGD11_COMMIT_SHA
python -m unittest discover -s tests -v
```

Use exact commit SHAs, not moving branch tips. If an adapter has multiple commits, preserve that card's listed order. Resolve sibling additions without dropping files, then compare the complete integrated source-ID set with every handoff. New source-specific work must be a real dependency of UGD-12, not a prose-only TODO.

## Verification commands

```sh
python -m unittest tests.test_ultimate_model -v
python -m unittest discover -s tests -v
python scripts/grammar/build.py --help
```

Fixture tests contain synthetic content only. Full private datasets must never be uploaded to public CI.

# IMABI integration status

## Delivered mode

The IMABI adapter delivers two deliberately separate interfaces:

- `audited_reference_index()` retains the six public-safe, link-only references
  produced by the bounded UGD-04 audit; and
- the `fetch` and `convert` commands acquire and convert the complete official
  lesson index for a permission-approved private build.

Full-content conversion emits canonical records with stable source-qualified
lesson IDs, source-ordered sections, explanations and formations, paired
Japanese/English examples, modern/classical labels, canonical source links,
and exact field provenance. Site navigation, advertisements, scripts, styles,
forms, comments, and footer chrome are excluded. Remote images remain attributed
HTTPS links in their source content; the adapter does not download or repackage
them as media.

## Permission and current scope

The original UGD-04 audit selected link-only mode because:

- the [IMABI homepage](https://imabi.org/) says that offline formats are not
  available;
- the [terms of use](https://www.imabi.com/terms-of-use) reserve rights and
  prohibit text/data mining and web scraping; and
- no permitted offline export or open content licence was found at that time.

Bee subsequently reported project-specific approval from the IMABI authors on
8 September 2026. This is recorded as user-reported permission, not an
independently verified licence, an open licence, or a blanket redistribution
grant. It supersedes the earlier absence-of-permission restriction for this
private project, so the registry permits substantive IMABI content in private
builds.

No agent sent a permission request. The report does not establish agreement
terms, an acquisition method, expiry, sublicensing rights, or permission to
publish a release. Private/local delivery remains the scope.

## Inclusion and exclusion contract

The fetcher accepts one exact unauthenticated HTTPS table-of-contents URL on
`imabi.org`, limits the accepted index and response sizes, requires every lesson
URL to be canonical and same-origin, uses a configurable delay, and stops rather
than emitting a partial snapshot if any eligible lesson fails. Cached responses
are regular files read without following symlinks. Snapshots contain a manifest,
the accepted index HTML, and one hashed HTML document for every eligible lesson;
ZIP paths, member types, duplicate members, member sizes, total size, and hashes
are checked before conversion.

The converter rejects missing or unaccounted lesson identities. Every accepted
source content unit must be represented by a canonical content block or a fully
extracted example pair, and the report includes both counts for an exact
preservation check. Content keeps safe semantic elements and safe HTTP(S) source links
while removing active, presentational, or site-chrome fields. The adapter does
not claim that presentation-only HTML attributes are canonical fields.

The registry permits `imabi` content selection only for private builds.
`publication_mode: denied` remains fail-closed for copied content, and the
permission basis remains `user-reported-project-permission` rather than an open
licence or independently verified redistribution grant.

## Deterministic integration API

Use `audited_reference_index()` for a fresh public-safe Python dictionary or
`audited_reference_json()` for canonical UTF-8 reference JSON. These legacy
reference functions remain offline and do not imply full-content coverage.

The audited source index is the official
[IMABI table of contents](https://imabi.org/table-of-contents-%E7%9B%AE%E6%AC%A1).

Keep source snapshots, cache files, reports, and canonical output under the
ignored `.grammar/` directory:

```sh
python scripts/grammar/sources/imabi.py fetch \
  --toc-url 'https://imabi.org/table-of-contents-%E7%9B%AE%E6%AC%A1/' \
  --snapshot .grammar/imabi-official.zip \
  --cache-dir .grammar/imabi-cache \
  --report .grammar/imabi-fetch-report.json \
  --request-delay 0.5 \
  --max-pages 600

python scripts/grammar/sources/imabi.py convert \
  --snapshot .grammar/imabi-official.zip \
  --revision official-SNAPSHOT_SHA_PREFIX \
  --output .grammar/imabi-canonical.json \
  --report .grammar/imabi-import-report.json
```

The permitted acquisition must remain bounded and rate-limited and must not use
login, paywall, or CAPTCHA bypass. An access failure is a specific blocker, not
permission to substitute partial or synthetic production content.

## Verified 2026-09-08 acquisition

The real-input validation snapshot has SHA-256
`517ecc89d0d2e1c9132e310a334c996f820b32c91161b20b92831da0f21a581f`.
Its reports account for 487 index entries: 486 eligible lessons imported, one
Okinawan lesson marked not applicable to the selected modern/classical scope, and zero rejected or
unaccounted lessons. The canonical output contains 486 records, 3,134 sections,
19,261 content blocks, 276 conservatively classified formation blocks, 16,051
example pairs, 568 linked images, and zero downloaded media. The canonical JSON
SHA-256 is `3e52e8c1c8b96cc9540e3c7dbe35875c0181fa920e0513a4460cd99b8422a03b`.

The converter found 30,240 lesson content units and represented all 30,240;
the report confirms that equality for every individual lesson as well as for
the whole snapshot. It separately reports 972 excluded site-chrome units and
retains modern/classical counts of 450 and 36. These counts characterize the
pinned snapshot and must be regenerated rather than copied when the live source
changes.
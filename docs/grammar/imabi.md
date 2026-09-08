# IMABI integration status

## Delivered mode

This bounded adapter branch delivers **metadata/link-only** IMABI references.
It does not yet supply IMABI content to the combined dictionary; a dedicated
follow-up owns the substantive content adapter. The implementation in
`scripts/grammar/sources/imabi.py` returns six lesson-level references from the
bounded UGD-04 audit:

- three modern and three classical references;
- four `link-only` lessons and two `reference-only` overview pages; and
- stable IDs in the form `imabi:{modern|classical}:lesson:{number}`.

The emitted index identifies itself as `partial-audited` coverage and reports
zero lesson bodies, content blocks, examples, and media. These six references
are useful audited links; they are not a complete IMABI lesson index and must
not be counted as substantive IMABI grammar inclusion or as the final
project-wide IMABI result.

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
private project, so substantive content is required from the dedicated
follow-up rather than permanently deferred.

No agent sent a permission request. The report does not establish agreement
terms, an acquisition method, expiry, sublicensing rights, or permission to
publish a release. Private/local delivery remains the scope.

## Inclusion and exclusion contract

Each emitted reference contains only:

- source lesson ID;
- lesson number and title;
- canonical IMABI lesson URL;
- table-of-contents section;
- `modern` or `classical` material kind; and
- `link-only` or `reference-only` status.

The adapter has no network acquisition path. It emits no explanations,
formation rules, headings, grammar-anchor text, surrounding lesson context,
examples, translations, images, navigation, advertisements, footers, or
scripts. Metadata values reject markup and Unicode controls, and lesson URLs
must exactly match one of the six audited, canonical, unauthenticated HTTPS
links on `imabi.org`.

Downstream integration should include the registry's `imabi` metadata
selection and may render these records as visibly partial external source
references. It must preserve `mode`, `coverage`, and all zero-content counts;
it must not convert a lesson reference into a canonical grammar sense or claim
that an IMABI lesson was imported from this branch. It must also wait for and
inspect the substantive follow-up rather than accepting this superseded
link-only slice as complete project coverage.

## Deterministic integration API

Use `audited_reference_index()` for a fresh Python dictionary or
`audited_reference_json()` for canonical UTF-8 JSON bytes. The latter is
deterministically serialized by the foundation model. Both functions are
offline and require no cache or rate limiter because the approved mode performs
no acquisition.

The audited source index is the official
[IMABI table of contents](https://imabi.org/table-of-contents-%E7%9B%AE%E6%AC%A1).
The six records are intentionally limited to the UGD-04 sample rather than an
automated crawl.

## Full-content continuation contract

Substantive content is intentionally implemented in the already-planned
follow-up instead of rewriting this nearly complete bounded branch. That work
must preserve the permission basis as user-reported project approval, keep the
private permission record outside public Git and CI, and avoid presenting it as
an independently verified or open licence. It must then:

1. reconcile the IMABI registry policy without weakening policy for other
   sources or authorising public release;
2. use a hash-pinned local/export snapshot or a bounded permitted acquisition
   route, keeping source bytes in ignored private storage;
3. use stable lesson IDs and verified heading/grammar anchors;
4. split many-concept lessons by those source anchors while preserving nearby
   explanatory context;
5. keep each Japanese example paired with its source translation under the
   smallest matching grammar section;
6. preserve separate modern and classical labels and author/source
   attribution; and
7. strip site navigation, advertisements, footers, scripts, and other chrome.

Any remote route must use the foundation's bounded, rate-limited, hash-pinned
cache path without login, paywall, or CAPTCHA bypass. An actual access failure
is a separate blocker and must be reported specifically; it cannot be inferred
from the superseded permission finding. None of these rules authorises a public
release or changes the zero-content status of this branch.
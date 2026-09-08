# IMABI integration status

## Delivered mode

IMABI is **metadata/link-only**. It is not a content source in the combined
dictionary. The implementation in `scripts/grammar/sources/imabi.py` returns
six lesson-level references from the bounded UGD-04 audit:

- three modern and three classical references;
- four `link-only` lessons and two `reference-only` overview pages; and
- stable IDs in the form `imabi:{modern|classical}:lesson:{number}`.

The emitted index identifies itself as `partial-audited` coverage and reports
zero lesson bodies, content blocks, examples, and media. These six references
are useful audited links; they are not a complete IMABI lesson index and must
not be counted as substantive IMABI grammar inclusion.

## Why content import is deferred

The UGD-04 audit selected this mode because:

- the [IMABI homepage](https://imabi.org/) says that offline formats are not
  available;
- the [terms of use](https://www.imabi.com/terms-of-use) reserve rights and
  prohibit text/data mining and web scraping; and
- no permitted offline export or open content licence was found.

No permission request was sent. Public availability is not treated as
permission to copy lesson bodies.

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
that an IMABI lesson was imported.

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

## Contract for a future content mode

A future content adapter is a policy change, not an automatic upgrade of this
module. It may begin only after Bee supplies a lawful local export or explicit
permission and the source registry is revised with that evidence. The new
implementation must then:

1. hash-pin the local export and keep its bytes in ignored private storage;
2. use stable lesson IDs and verified heading/grammar anchors;
3. split many-concept lessons by those source anchors while preserving nearby
   explanatory context;
4. keep each Japanese example paired with its source translation under the
   smallest matching grammar section;
5. preserve separate modern and classical labels; and
6. strip site navigation, advertisements, footers, scripts, and other chrome.

If an authorised remote route is ever established, it must additionally use
the foundation's bounded, rate-limited, hash-pinned cache path. None of these
future rules grants permission or changes today's link-only status.
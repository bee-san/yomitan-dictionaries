"""Versioned, fail-closed source registry and selection policy."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from urllib.parse import urlsplit

from .model import (
    AccessMode,
    BuildMode,
    ImportMode,
    PublicationMode,
    SelectionMode,
)


REGISTRY_VERSION = 1


class PolicyError(ValueError):
    """A source selection exceeds the audited registry policy."""


@dataclass(frozen=True, slots=True)
class SourceDefinition:
    source_id: str
    name: str
    homepage: str
    allowed_hosts: tuple[str, ...]
    access_mode: AccessMode
    import_mode: ImportMode
    publication_mode: PublicationMode
    license_identifier: str | None
    attribution: str
    selection_status: str
    source_family: str

    def public_metadata(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "name": self.name,
            "homepage": self.homepage,
            "access_mode": self.access_mode.value,
            "import_mode": self.import_mode.value,
            "publication_mode": self.publication_mode.value,
            "license_identifier": self.license_identifier,
            "attribution": self.attribution,
            "selection_status": self.selection_status,
            "source_family": self.source_family,
        }


def _source(
    source_id: str,
    name: str,
    homepage: str,
    allowed_hosts: tuple[str, ...],
    access_mode: AccessMode,
    import_mode: ImportMode,
    publication_mode: PublicationMode,
    license_identifier: str | None,
    attribution: str,
    selection_status: str,
    source_family: str | None = None,
) -> SourceDefinition:
    return SourceDefinition(
        source_id=source_id,
        name=name,
        homepage=homepage,
        allowed_hosts=allowed_hosts,
        access_mode=access_mode,
        import_mode=import_mode,
        publication_mode=publication_mode,
        license_identifier=license_identifier,
        attribution=attribution,
        selection_status=selection_status,
        source_family=source_family or source_id,
    )


# Policy is deliberately conservative. PublicationMode.ALLOWED means the
# audited source permits redistribution under the recorded attribution terms;
# it does not authorize a release action.
_SOURCES = (
    _source(
        "bee-bunpo",
        "Bee 文法",
        "https://github.com/bee-san/yomitan-dictionaries/releases/tag/2026.09.01",
        ("github.com", "objects.githubusercontent.com"),
        AccessMode.LOCAL_FILE,
        ImportMode.CONTENT,
        PublicationMode.DENIED,
        None,
        "Preserve the archive wording and record Bee's later own-work declaration separately.",
        "required-private",
    ),
    _source(
        "bunpro",
        "Bunpro grammar points",
        "https://bunpro.jp/grammar_points",
        ("bunpro.jp",),
        AccessMode.PUBLIC_HTTP,
        ImportMode.CONTENT,
        PublicationMode.UNCLEARED,
        None,
        "Bunpro; preserve grammar-point URLs and source identity.",
        "required-private",
    ),
    _source(
        "bunpro-public-metadata",
        "Bunpro public grammar metadata",
        "https://bunpro.jp/grammar_points",
        ("bunpro.jp",),
        AccessMode.METADATA_ONLY,
        ImportMode.METADATA_ONLY,
        PublicationMode.UNCLEARED,
        None,
        "Bunpro grammar-point identity, level, formation metadata and links only.",
        "canary-metadata",
        "bunpro",
    ),
    _source(
        "bunpro-josh-cache",
        "J-O-S-H-L Bunpro cache",
        "https://github.com/J-O-S-H-L/grammar_dict",
        ("github.com", "raw.githubusercontent.com"),
        AccessMode.PUBLIC_HTTP,
        ImportMode.CONTENT,
        PublicationMode.UNCLEARED,
        None,
        "Mirror provenance must retain Bunpro as the content source and J-O-S-H-L as the cache provider.",
        "secondary-private-cache",
        "bunpro",
    ),
    _source(
        "ninjal-bunkei",
        "NINJAL 日本語文型データベース",
        "https://doi.org/10.15084/0002000610",
        ("doi.org", "www2.ninjal.ac.jp", "repository.ninjal.ac.jp", "github.com"),
        AccessMode.PUBLIC_HTTP,
        ImportMode.CONTENT,
        PublicationMode.ALLOWED,
        "CC-BY-4.0",
        "National Institute for Japanese Language and Linguistics (NINJAL); preserve DOI, version and attribution.",
        "selected-open",
    ),
    _source(
        "nihongo-kyoshi",
        "Nihongo Kyoshi / 日本語NET",
        "https://nihongokyoshi-net.com/jlpt-grammars/",
        ("nihongokyoshi-net.com",),
        AccessMode.LOCAL_FILE,
        ImportMode.CONTENT,
        PublicationMode.DENIED,
        None,
        "Preserve 日本語NET links and archive author metadata; local/private use only.",
        "selected-private",
        "nihongokyoshi-net",
    ),
    _source(
        "donna-toki",
        "Donna Toki",
        "https://itazuraneko.neocities.org/grammar/donnatoki.html",
        ("itazuraneko.neocities.org",),
        AccessMode.LOCAL_FILE,
        ImportMode.CONTENT,
        PublicationMode.DENIED,
        None,
        "Preserve the archive's book, converter and source attribution; local/private use only.",
        "selected-private",
    ),
    _source(
        "e-de-wakaru",
        "E de wakaru",
        "https://www.edewakaru.com/archives/cat_179055.html",
        ("www.edewakaru.com", "edewakaru.com"),
        AccessMode.LOCAL_FILE,
        ImportMode.CONTENT,
        PublicationMode.DENIED,
        None,
        "Preserve source links, image provenance and archive author metadata; local/private use only.",
        "selected-private",
    ),
    _source(
        "dojg",
        "Dictionary of Japanese Grammar",
        "https://bookclub.japantimes.co.jp/jp/book/b309577.html",
        ("bookclub.japantimes.co.jp",),
        AccessMode.LOCAL_FILE,
        ImportMode.CONTENT,
        PublicationMode.DENIED,
        None,
        "Copyrighted book-derived material; accept only a lawful local input and do not redistribute.",
        "selected-private",
    ),
    _source(
        "nihongo-no-sensei",
        "Nihongo no sensei",
        "https://nihongonosensei.net/?page_id=10246",
        ("nihongonosensei.net",),
        AccessMode.LOCAL_FILE,
        ImportMode.CONTENT,
        PublicationMode.DENIED,
        None,
        "Preserve archive provenance; the formerly relevant live domain is not acquisition authority.",
        "selected-private",
    ),
    _source(
        "imabi",
        "IMABI",
        "https://imabi.org/",
        ("imabi.org", "www.imabi.com"),
        AccessMode.METADATA_ONLY,
        ImportMode.METADATA_ONLY,
        PublicationMode.DENIED,
        None,
        "IMABI lesson metadata and links only; lesson-body import is deferred without a lawful export or permission.",
        "link-only",
    ),
    _source(
        "tae-kim",
        "Tae Kim's Guide to Japanese",
        "https://guidetojapanese.org/learn/",
        ("guidetojapanese.org",),
        AccessMode.METADATA_ONLY,
        ImportMode.METADATA_ONLY,
        PublicationMode.UNCLEARED,
        None,
        "Link-only candidate; no content redistribution grant was established.",
        "deferred-link-only",
    ),
    _source(
        "jlpt-sensei",
        "JLPT Sensei",
        "https://jlptsensei.com/",
        ("jlptsensei.com",),
        AccessMode.METADATA_ONLY,
        ImportMode.METADATA_ONLY,
        PublicationMode.UNCLEARED,
        None,
        "Link-only candidate; member gating and no open content licence were observed.",
        "deferred-link-only",
    ),
    _source(
        "maggie-sensei",
        "Maggie Sensei",
        "https://maggiesensei.com/",
        ("maggiesensei.com",),
        AccessMode.METADATA_ONLY,
        ImportMode.METADATA_ONLY,
        PublicationMode.UNCLEARED,
        None,
        "Link-only lesson source; no open content licence was established.",
        "deferred-link-only",
    ),
    _source(
        "japanese-wikibooks",
        "Japanese Wikibooks",
        "https://ja.wikibooks.org/wiki/日本語",
        ("ja.wikibooks.org",),
        AccessMode.METADATA_ONLY,
        ImportMode.METADATA_ONLY,
        PublicationMode.ALLOWED,
        "CC-BY-SA-4.0",
        "Wikibooks contributors; preserve revision, attribution and share-alike terms if a later adapter is approved.",
        "deferred-open",
    ),
    _source(
        "nihongo-kyoshi-n1et",
        "日本語教師のN1et",
        "https://jn1et.com/jlpt/",
        ("jn1et.com",),
        AccessMode.METADATA_ONLY,
        ImportMode.METADATA_ONLY,
        PublicationMode.UNCLEARED,
        None,
        "Backlink metadata only; Donna Toki-derived level references are not independent corroboration.",
        "deferred-backlink",
        "donna-toki",
    ),
    _source(
        "nihon5-bunka",
        "日本の言葉と文化",
        "https://nihon5-bunka.net/japanese-grammars/",
        ("nihon5-bunka.net",),
        AccessMode.METADATA_ONLY,
        ImportMode.METADATA_ONLY,
        PublicationMode.UNCLEARED,
        None,
        "Backlink metadata only; no open content licence was established.",
        "deferred-backlink",
    ),
    _source(
        "japanese-bank",
        "日本語教師キャリア マガジン",
        "https://japanese-bank.com/jlpt-grammar-all/",
        ("japanese-bank.com",),
        AccessMode.METADATA_ONLY,
        ImportMode.METADATA_ONLY,
        PublicationMode.UNCLEARED,
        None,
        "Backlink metadata only; no open content licence was established.",
        "deferred-backlink",
    ),
    _source(
        "yokubi",
        "Yokubi",
        "https://yoku.bi/",
        ("yoku.bi", "github.com", "raw.githubusercontent.com"),
        AccessMode.PUBLIC_HTTP,
        ImportMode.CONTENT,
        PublicationMode.ALLOWED,
        "CC-BY-4.0",
        "Morgawr and Yokubi contributors; preserve credited Sakubi origins, repository revision, and chapter/section anchors.",
        "selected-open-unfinished",
    ),
)

SOURCE_REGISTRY = MappingProxyType({source.source_id: source for source in _SOURCES})
if len(SOURCE_REGISTRY) != len(_SOURCES):
    raise RuntimeError("duplicate source_id in grammar registry")


def get_source(source_id: str) -> SourceDefinition:
    try:
        return SOURCE_REGISTRY[source_id]
    except KeyError as error:
        raise PolicyError(f"source {source_id!r} is not in registry version {REGISTRY_VERSION}") from error


def validate_selection(
    source_id: str,
    build_mode: BuildMode | str,
    selection_mode: SelectionMode | str,
) -> SourceDefinition:
    source = get_source(source_id)
    build_mode = BuildMode(build_mode)
    selection_mode = SelectionMode(selection_mode)
    if selection_mode is SelectionMode.METADATA:
        return source
    if source.import_mode is not ImportMode.CONTENT:
        raise PolicyError(f"{source_id} is not cleared for content import")
    if source.access_mode in {AccessMode.METADATA_ONLY, AccessMode.UNAVAILABLE}:
        raise PolicyError(f"{source_id} has no permitted content acquisition mode")
    if build_mode is BuildMode.PUBLISHABLE and source.publication_mode is not PublicationMode.ALLOWED:
        raise PolicyError(
            f"{source_id} content is not cleared to publish ({source.publication_mode.value})"
        )
    return source


def validate_remote_url(source_id: str, url: str) -> str:
    source = get_source(source_id)
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise PolicyError(f"{source_id} remote input must be an unauthenticated HTTPS URL")
    host = parsed.hostname.lower().rstrip(".")
    if host not in source.allowed_hosts:
        raise PolicyError(f"{source_id} remote host {host!r} is not allowlisted")
    try:
        port = parsed.port
    except ValueError as error:
        raise PolicyError(f"{source_id} remote input has an invalid port") from error
    if port not in (None, 443):
        raise PolicyError(f"{source_id} remote input must use the default HTTPS port")
    return url

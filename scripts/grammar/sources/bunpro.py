"""Acquire and convert official Bunpro grammar-point HTML snapshots.

Bunpro-derived content is for private/local builds. No open content
redistribution licence was established by the source audit.
"""

from __future__ import annotations

import argparse
from collections import Counter
from html.parser import HTMLParser
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
import time
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from xml.etree import ElementTree
import zipfile

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from scripts.grammar.model import (  # type: ignore[import-not-found]
        AccessMode,
        BlockKind,
        ContentBlock,
        ExamplePair,
        ImportMode,
        LicenseInfo,
        PartitionStatus,
        Provenance,
        PublicationMode,
        SourceBundle,
        SourceLink,
        SourceRecord,
        SourceRevision,
        SourceSense,
        canonical_json_bytes,
    )
    from scripts.grammar.registry import (  # type: ignore[import-not-found]
        PolicyError,
        validate_remote_url,
    )
else:
    from ..model import (
        AccessMode,
        BlockKind,
        ContentBlock,
        ExamplePair,
        ImportMode,
        LicenseInfo,
        PartitionStatus,
        Provenance,
        PublicationMode,
        SourceBundle,
        SourceLink,
        SourceRecord,
        SourceRevision,
        SourceSense,
        canonical_json_bytes,
    )
    from ..registry import PolicyError, validate_remote_url


SOURCE_ID = "bunpro"
SNAPSHOT_FORMAT = "bunpro-html-snapshot"
SNAPSHOT_FORMAT_VERSION = 1
REPORT_FORMAT_VERSION = 1
DEFAULT_SITEMAP_URL = "https://bunpro.jp/sitemap.xml"
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_RETRIES = 2
DEFAULT_REQUEST_DELAY_SECONDS = 0.05
MAX_SITEMAPS = 100
MAX_PAGES = 2_000
MAX_SITEMAP_BYTES = 10 * 1024 * 1024
MAX_PAGE_BYTES = 10 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 256 * 1024 * 1024
MAX_UNCOMPRESSED_SNAPSHOT_BYTES = 512 * 1024 * 1024
MAX_HTML_DEPTH = 128
MAX_HTML_NODES = 100_000
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LEVELS = frozenset({"JLPT1", "JLPT2", "JLPT3", "JLPT4", "JLPT5", "Non-JLPT", "関西弁"})

FetchBytes = Callable[[str, float], bytes]
Delay = Callable[[float], None]


class BunproError(ValueError):
    """The Bunpro acquisition or conversion contract failed."""


class BunproImportError(BunproError):
    """A complete snapshot could not be converted without data loss."""

    def __init__(self, message: str, report: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.report = dict(report or {})


class BunproFetchError(BunproError):
    """A complete official HTML snapshot could not be acquired."""

    def __init__(self, message: str, report: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.report = dict(report or {})


class _BunproAggregateSizeError(BunproFetchError):
    """The acquisition crossed the importer's aggregate size boundary."""


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _json_value_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _json_hash(value: Any) -> str:
    return _sha256(_json_value_bytes(value))


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BunproError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _decode_json(content: bytes, context: str) -> Any:
    try:
        return json.loads(content, object_pairs_hook=_unique_object)
    except BunproError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BunproError(f"invalid {context} JSON: {error}") from error


def _required_object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BunproError(f"{context} must be an object")
    return value


def _required_list(value: Any, context: str) -> list[Any]:
    if not isinstance(value, list):
        raise BunproError(f"{context} must be a list")
    return value


def _required_string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BunproError(f"{context} must be a non-empty string")
    return value


def _optional_string(value: Any, context: str) -> str | None:
    if value is None or value == "":
        return None
    return _required_string(value, context)


def _normalized_optional_string(value: Any, context: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise BunproError(f"{context} must be a string or null")
    normalized = value.strip()
    return normalized or None


def _required_integer(value: Any, context: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise BunproError(f"{context} must be a non-negative integer")
    return value


def _safe_member_name(name: Any, context: str) -> str:
    if not isinstance(name, str) or not name:
        raise BunproError(f"{context} must be a non-empty path")
    path = PurePosixPath(name)
    if (
        "\\" in name
        or path.is_absolute()
        or path.as_posix() != name
        or ".." in path.parts
        or not path.parts
    ):
        raise BunproError(f"{context} must be a safe POSIX-relative path")
    return name


def _validate_bunpro_url(url: Any, *, grammar_page: bool = False) -> str:
    if not isinstance(url, str):
        raise BunproError("Bunpro URL must be a string")
    try:
        validate_remote_url(SOURCE_ID, url)
        parsed = urlsplit(url)
    except (PolicyError, ValueError) as error:
        raise BunproError(str(error)) from error
    if parsed.query or parsed.fragment:
        raise BunproError("Bunpro snapshot URLs must not contain query strings or fragments")
    if grammar_page:
        prefix = "/grammar_points/"
        if not parsed.path.startswith(prefix) or not parsed.path[len(prefix) :]:
            raise BunproError(f"not a Bunpro grammar-point URL: {url}")
    return url


def _grammar_slug_from_url(url: str) -> str:
    parsed = urlsplit(_validate_bunpro_url(url, grammar_page=True))
    slug = unquote(parsed.path.removeprefix("/grammar_points/"))
    if not slug or "/" in slug:
        raise BunproError(f"grammar-point URL has an invalid slug: {url}")
    return slug


class _NextDataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._active = False
        self.payloads: list[list[str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script" and dict(attrs).get("id") == "__NEXT_DATA__":
            self._active = True
            self.payloads.append([])

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._active = False

    def handle_data(self, data: str) -> None:
        if self._active:
            self.payloads[-1].append(data)


class _SafeHTMLParser(HTMLParser):
    _BLOCKED = {"script", "style", "iframe", "object", "embed", "svg", "math", "template"}
    _CONTAINERS = {
        "ruby",
        "rt",
        "rp",
        "span",
        "div",
        "ol",
        "ul",
        "li",
        "table",
        "thead",
        "tbody",
        "tfoot",
        "tr",
        "td",
        "th",
        "details",
        "summary",
    }
    _BLOCK_LIKE = {"p", "section", "article", "header", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root: list[Any] = []
        self.stack: list[tuple[str | None, list[Any]]] = [(None, self.root)]
        self.blocked: list[str] = []
        self.node_count = 0

    def _add_node(self) -> None:
        self.node_count += 1
        if self.node_count > MAX_HTML_NODES:
            raise BunproError(f"HTML exceeds {MAX_HTML_NODES} nodes")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self.blocked:
            if tag in self._BLOCKED:
                self.blocked.append(tag)
            return
        if tag in self._BLOCKED:
            self.blocked.append(tag)
            return
        if tag in {"br", "hr"}:
            self._add_node()
            self.stack[-1][1].append({"tag": "br"})
            return

        if len(self.stack) >= MAX_HTML_DEPTH:
            raise BunproError(f"HTML nesting exceeds {MAX_HTML_DEPTH} levels")
        self._add_node()

        attributes = dict(attrs)
        children: list[Any] = []
        mapped = tag if tag in self._CONTAINERS else "div" if tag in self._BLOCK_LIKE else "span"
        node: dict[str, Any] = {"tag": mapped, "content": children}
        if tag == "a":
            href = attributes.get("href")
            if href is not None:
                try:
                    link = SourceLink("inline", href)
                except ValueError:
                    pass
                else:
                    node = {"tag": "a", "href": link.url, "content": children}
        if tag in {"b", "strong"}:
            node["style"] = {"fontWeight": "bold"}
        elif tag in {"i", "em"}:
            node["style"] = {"fontStyle": "italic"}
        self.stack[-1][1].append(node)
        self.stack.append((tag, children))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.blocked:
            if tag == self.blocked[-1]:
                self.blocked.pop()
            return
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if not self.blocked and data:
            self._add_node()
            self.stack[-1][1].append(data)


def _safe_html(value: str, context: str) -> list[Any]:
    parser = _SafeHTMLParser()
    try:
        parser.feed(value)
        parser.close()
    except Exception as error:
        raise BunproError(f"invalid HTML in {context}: {error}") from error
    return parser.root


def _visible_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_visible_text(item) for item in value)
    if isinstance(value, dict):
        return _visible_text(value.get("content", []))
    return ""


def _extract_page_data(content: bytes, url: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if not content or len(content) > MAX_PAGE_BYTES:
        raise BunproError(f"Bunpro page is empty or exceeds {MAX_PAGE_BYTES} bytes")
    try:
        document = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise BunproError("Bunpro page is not UTF-8") from error
    parser = _NextDataParser()
    parser.feed(document)
    parser.close()
    if len(parser.payloads) != 1:
        raise BunproError("page must contain exactly one __NEXT_DATA__ script")
    payload = "".join(parser.payloads[0]).encode("utf-8")
    next_data = _required_object(_decode_json(payload, "__NEXT_DATA__"), "__NEXT_DATA__")
    if next_data.get("page") != "/grammar_points/[slug]":
        raise BunproError("unexpected Next.js page route")
    query = _required_object(next_data.get("query"), "__NEXT_DATA__.query")
    slug = _required_string(query.get("slug"), "__NEXT_DATA__.query.slug")
    if slug != _grammar_slug_from_url(url):
        raise BunproError("page slug does not match its grammar-point URL")
    props = _required_object(next_data.get("props"), "__NEXT_DATA__.props")
    page_props = _required_object(props.get("pageProps"), "__NEXT_DATA__.props.pageProps")
    if page_props.get("error") not in (None, False):
        raise BunproError(f"Bunpro page reports an error: {page_props['error']!r}")
    reviewable = _required_object(page_props.get("reviewable"), "pageProps.reviewable")
    included = _required_object(page_props.get("included"), "pageProps.included")

    grammar_point_id = _required_integer(reviewable.get("id"), "reviewable.id")
    if grammar_point_id == 0:
        raise BunproError("reviewable.id must be positive")
    if reviewable.get("type_snake") != "grammar_point":
        raise BunproError("reviewable is not a grammar point")
    if _required_string(reviewable.get("slug"), "reviewable.slug") != slug:
        raise BunproError("reviewable.slug does not match the page slug")
    _required_string(reviewable.get("title"), "reviewable.title")
    _required_string(reviewable.get("furigana"), "reviewable.furigana")
    _required_string(reviewable.get("meaning"), "reviewable.meaning")
    level = _required_string(reviewable.get("level"), "reviewable.level")
    if level not in _LEVELS:
        raise BunproError(f"unsupported Bunpro level: {level!r}")
    _required_integer(reviewable.get("grammar_order"), "reviewable.grammar_order")
    for name in (
        "writeups",
        "studyQuestions",
        "offlineResources",
        "supplementalLinks",
        "relatedContents",
        "attachedReviewables",
        "articles",
    ):
        _required_list(included.get(name), f"included.{name}")
    return page_props, reviewable, included


def _page_identity(content: bytes, url: str) -> int:
    _, reviewable, _ = _extract_page_data(content, url)
    return _required_integer(reviewable["id"], "reviewable.id")


def _parse_sitemap(content: bytes, url: str) -> tuple[str, list[str]]:
    if not content or len(content) > MAX_SITEMAP_BYTES:
        raise BunproError(f"sitemap is empty or exceeds {MAX_SITEMAP_BYTES} bytes")
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as error:
        raise BunproError(f"invalid sitemap XML at {url}: {error}") from error
    kind = root.tag.rsplit("}", 1)[-1]
    if kind not in {"urlset", "sitemapindex"}:
        raise BunproError(f"unsupported sitemap root {kind!r} at {url}")
    child_name = "url" if kind == "urlset" else "sitemap"
    locations: list[str] = []
    for child in root:
        if child.tag.rsplit("}", 1)[-1] != child_name:
            continue
        location = next(
            (
                item.text.strip()
                for item in child
                if item.tag.rsplit("}", 1)[-1] == "loc" and item.text and item.text.strip()
            ),
            None,
        )
        if location is None:
            raise BunproError(f"sitemap {url} contains an entry without loc")
        locations.append(_validate_bunpro_url(location))
    if not locations:
        raise BunproError(f"sitemap {url} contains no locations")
    if len(locations) != len(set(locations)):
        raise BunproError(f"sitemap {url} contains duplicate locations")
    return kind, locations


class _RestrictedRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request | None:
        _validate_bunpro_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _request_url(url: str) -> str:
    """Encode a Unicode sitemap URL without double-encoding existing escapes."""
    parsed = urlsplit(_validate_bunpro_url(url))
    path = quote(parsed.path, safe="/%:@-._~!$&'()*+,;=")
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))


def _download_bytes(url: str, timeout: float) -> bytes:
    request = Request(_request_url(url), headers={"User-Agent": "bee-grammar-builder/1"})
    opener = build_opener(_RestrictedRedirect())
    with opener.open(request, timeout=timeout) as response:
        _validate_bunpro_url(response.geturl())
        declared = response.headers.get("Content-Length")
        if declared is not None:
            try:
                if int(declared) > MAX_SITEMAP_BYTES:
                    raise BunproError("remote Bunpro response exceeds the hard byte limit")
            except ValueError as error:
                raise BunproError("remote Bunpro response has invalid Content-Length") from error
        content = response.read(MAX_SITEMAP_BYTES + 1)
    if len(content) > MAX_SITEMAP_BYTES:
        raise BunproError("remote Bunpro response exceeds the hard byte limit")
    return content


def _read_regular_nofollow(path: Path, limit: int, context: str) -> bytes:
    try:
        path = path.parent.resolve(strict=True) / path.name
    except OSError as error:
        raise BunproError(f"cannot safely resolve {context} parent: {error}") from error
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_descriptor: int | None = None
    descriptor: int | None = None
    try:
        directory_descriptor = os.open(path.anchor, directory_flags)
        for component in path.parts[1:-1]:
            next_descriptor = os.open(component, directory_flags, dir_fd=directory_descriptor)
            os.close(directory_descriptor)
            directory_descriptor = next_descriptor
        descriptor = os.open(path.name, file_flags, dir_fd=directory_descriptor)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise BunproError(f"{context} must be a regular file")
        with os.fdopen(descriptor, "rb") as source:
            descriptor = None
            content = source.read(limit + 1)
    except OSError as error:
        raise BunproError(f"cannot safely open {context}: {error}") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory_descriptor is not None:
            os.close(directory_descriptor)
    if len(content) > limit:
        raise BunproError(f"{context} exceeds its byte limit")
    return content


def _read_cache(path: Path, limit: int) -> bytes:
    return _read_regular_nofollow(path, limit, "cache entry")


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temporary.write(content)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    try:
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _cache_member(cache_directory: Path, kind: str, url: str, suffix: str) -> Path:
    return cache_directory / kind / f"{_sha256(url.encode('utf-8'))}{suffix}"


def _cached_or_fetch(
    url: str,
    cache_path: Path,
    *,
    limit: int,
    validate: Callable[[bytes], Any],
    fetch_bytes: FetchBytes,
    timeout: float,
    retries: int,
    retry_delay: Delay,
    refresh: bool,
) -> tuple[bytes, str]:
    if not refresh and cache_path.exists():
        try:
            cached = _read_cache(cache_path, limit)
            validate(cached)
        except (BunproError, OSError, UnicodeError):
            pass
        else:
            return cached, "cached"

    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            content = fetch_bytes(url, timeout)
            if not isinstance(content, bytes):
                raise BunproError("fetcher returned non-byte content")
            if not content or len(content) > limit:
                raise BunproError(f"response is empty or exceeds {limit} bytes")
            validate(content)
            _atomic_write(cache_path, content)
            return content, "fetched"
        except (BunproError, HTTPError, URLError, TimeoutError, OSError) as error:
            last_error = error
            if attempt < retries:
                retry_delay(float(2**attempt))
    raise BunproFetchError(f"failed to fetch {url} after {retries + 1} attempts: {last_error}")


def _snapshot_member(kind: str, url: str, suffix: str) -> str:
    return f"{kind}/{_sha256(url.encode('utf-8'))}{suffix}"


def _write_snapshot(path: Path, members: Mapping[str, bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temporary_path = Path(temporary.name)
    try:
        with zipfile.ZipFile(temporary_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in sorted(members.items()):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, content, compresslevel=9)
        if temporary_path.stat().st_size > MAX_SNAPSHOT_BYTES:
            raise BunproFetchError(f"snapshot exceeds {MAX_SNAPSHOT_BYTES} bytes")
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def fetch_snapshot(
    sitemap_url: str | Path,
    snapshot_path: str | Path,
    cache_directory: str | Path,
    *,
    fetch_bytes: FetchBytes = _download_bytes,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    retries: int = DEFAULT_RETRIES,
    retry_delay: Delay = time.sleep,
    refresh: bool = False,
    request_delay: float = 0.0,
    max_pages: int = MAX_PAGES,
) -> dict[str, Any]:
    """Fetch a complete sitemap-bounded official HTML snapshot.

    Sitemap indexes are traversed as bounded pagination. Existing cache entries
    are reused only after XML or page-schema validation.
    """
    if isinstance(sitemap_url, Path):
        sitemap_url = str(sitemap_url)
    root_url = _validate_bunpro_url(sitemap_url)
    if not isinstance(retries, int) or isinstance(retries, bool) or not 0 <= retries <= 5:
        raise BunproError("retries must be between 0 and 5")
    if timeout <= 0 or timeout > 120:
        raise BunproError("timeout must be between 0 and 120 seconds")
    if request_delay < 0 or request_delay > 10:
        raise BunproError("request delay must be between 0 and 10 seconds")
    if not isinstance(max_pages, int) or isinstance(max_pages, bool) or not 1 <= max_pages <= MAX_PAGES:
        raise BunproError(f"max_pages must be between 1 and {MAX_PAGES}")

    snapshot_path = Path(snapshot_path)
    cache_directory = Path(cache_directory)
    sitemap_documents: dict[str, bytes] = {}
    aggregate_bytes = 0
    sitemap_status = Counter()
    pending = [root_url]
    page_urls: list[str] = []
    while pending:
        url = pending.pop(0)
        if url in sitemap_documents:
            raise BunproFetchError(f"sitemap cycle or duplicate discovered at {url}")
        if len(sitemap_documents) >= MAX_SITEMAPS:
            raise BunproFetchError(f"sitemap traversal exceeds {MAX_SITEMAPS} documents")
        cache = _cache_member(cache_directory, "sitemaps", url, ".xml")
        content, status = _cached_or_fetch(
            url,
            cache,
            limit=MAX_SITEMAP_BYTES,
            validate=lambda value, current=url: _parse_sitemap(value, current),
            fetch_bytes=fetch_bytes,
            timeout=timeout,
            retries=retries,
            retry_delay=retry_delay,
            refresh=refresh,
        )
        aggregate_bytes += len(content)
        if aggregate_bytes > MAX_UNCOMPRESSED_SNAPSHOT_BYTES:
            raise _BunproAggregateSizeError(
                "snapshot aggregate uncompressed content exceeds "
                f"{MAX_UNCOMPRESSED_SNAPSHOT_BYTES} bytes"
            )
        sitemap_documents[url] = content
        sitemap_status[status] += 1
        kind, locations = _parse_sitemap(content, url)
        if kind == "sitemapindex":
            pending.extend(locations)
        else:
            for location in locations:
                path = urlsplit(location).path
                if path.startswith("/grammar_points/") and path != "/grammar_points/":
                    page_urls.append(_validate_bunpro_url(location, grammar_page=True))
    if not page_urls:
        raise BunproFetchError("sitemap traversal found no grammar-point URLs")
    if len(page_urls) != len(set(page_urls)):
        raise BunproFetchError("sitemap traversal found duplicate grammar-point URLs")
    if len(page_urls) > max_pages:
        raise BunproFetchError(
            f"sitemap exposes {len(page_urls)} grammar pages, above configured max_pages {max_pages}"
        )

    page_documents: dict[str, bytes] = {}
    page_entries: list[dict[str, Any]] = []
    page_status = Counter()
    failures: list[dict[str, str]] = []
    grammar_ids: set[int] = set()
    for url in page_urls:
        cache = _cache_member(cache_directory, "pages", url, ".html")
        status: str | None = None
        try:
            content, status = _cached_or_fetch(
                url,
                cache,
                limit=MAX_PAGE_BYTES,
                validate=lambda value, current=url: _page_identity(value, current),
                fetch_bytes=fetch_bytes,
                timeout=timeout,
                retries=retries,
                retry_delay=retry_delay,
                refresh=refresh,
            )
            grammar_point_id = _page_identity(content, url)
            if grammar_point_id in grammar_ids:
                raise BunproError(f"duplicate grammar point ID {grammar_point_id}")
            if aggregate_bytes + len(content) > MAX_UNCOMPRESSED_SNAPSHOT_BYTES:
                raise _BunproAggregateSizeError(
                    "snapshot aggregate uncompressed content exceeds "
                    f"{MAX_UNCOMPRESSED_SNAPSHOT_BYTES} bytes"
                )
            grammar_ids.add(grammar_point_id)
            page_documents[url] = content
            aggregate_bytes += len(content)
            page_status[status] += 1
            page_entries.append(
                {
                    "url": url,
                    "path": _snapshot_member("pages", url, ".html"),
                    "sha256": _sha256(content),
                    "grammar_point_id": grammar_point_id,
                }
            )
        except _BunproAggregateSizeError:
            raise
        except (BunproError, BunproFetchError) as error:
            failures.append({"url": url, "reason": str(error)})
        if request_delay and status == "fetched":
            retry_delay(request_delay)

    report: dict[str, Any] = {
        "format": "ugd-bunpro-fetch-report",
        "format_version": REPORT_FORMAT_VERSION,
        "source_id": SOURCE_ID,
        "root_sitemap_url": root_url,
        "sitemap_count": len(sitemap_documents),
        "fetched_sitemap_count": sitemap_status["fetched"],
        "cached_sitemap_count": sitemap_status["cached"],
        "expected_page_count": len(page_urls),
        "fetched_page_count": page_status["fetched"],
        "cached_page_count": page_status["cached"],
        "failed_page_count": len(failures),
        "failed_pages": failures,
        "expected_urls": page_urls,
        "captured_urls": [entry["url"] for entry in page_entries],
        "expected_grammar_point_ids": sorted(grammar_ids),
    }
    if failures:
        raise BunproFetchError(
            f"refusing partial snapshot: {len(failures)} of {len(page_urls)} pages failed",
            report,
        )

    manifest = {
        "format": SNAPSHOT_FORMAT,
        "format_version": SNAPSHOT_FORMAT_VERSION,
        "source_id": SOURCE_ID,
        "root_sitemap_url": root_url,
        "sitemaps": [
            {
                "url": url,
                "path": _snapshot_member("sitemaps", url, ".xml"),
                "sha256": _sha256(content),
            }
            for url, content in sorted(sitemap_documents.items())
        ],
        "pages": sorted(page_entries, key=lambda item: (item["grammar_point_id"], item["url"])),
    }
    members = {"snapshot.json": _json_value_bytes(manifest)}
    members.update(
        {
            _snapshot_member("sitemaps", url, ".xml"): content
            for url, content in sitemap_documents.items()
        }
    )
    members.update(
        {
            _snapshot_member("pages", url, ".html"): content
            for url, content in page_documents.items()
        }
    )
    if sum(len(content) for content in members.values()) > MAX_UNCOMPRESSED_SNAPSHOT_BYTES:
        raise _BunproAggregateSizeError(
            f"snapshot aggregate uncompressed content exceeds {MAX_UNCOMPRESSED_SNAPSHOT_BYTES} bytes"
        )
    _write_snapshot(snapshot_path, members)
    report["snapshot_sha256"] = _sha256(snapshot_path.read_bytes())
    report["snapshot_bytes"] = snapshot_path.stat().st_size
    return report


def _strict_snapshot_entry(
    entry: Any,
    context: str,
    *,
    kind: str,
    suffix: str,
    grammar_page: bool,
) -> tuple[str, str, str, int | None]:
    value = _required_object(entry, context)
    allowed = {"url", "path", "sha256"} | ({"grammar_point_id"} if grammar_page else set())
    unknown = set(value) - allowed
    missing = allowed - set(value)
    if unknown or missing:
        raise BunproImportError(
            f"{context} fields differ from snapshot schema; missing={sorted(missing)}, unknown={sorted(unknown)}"
        )
    url = _validate_bunpro_url(value["url"], grammar_page=grammar_page)
    path = _safe_member_name(value["path"], f"{context}.path")
    if path != _snapshot_member(kind, url, suffix):
        raise BunproImportError(f"{context}.path does not match its URL-derived path")
    digest = value["sha256"]
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise BunproImportError(f"{context}.sha256 must be a lowercase SHA-256 digest")
    grammar_point_id = None
    if grammar_page:
        grammar_point_id = _required_integer(value.get("grammar_point_id"), f"{context}.grammar_point_id")
        if grammar_point_id == 0:
            raise BunproImportError(f"{context}.grammar_point_id must be positive")
    return url, path, digest, grammar_point_id


def _load_snapshot(path: Path) -> tuple[dict[str, Any], dict[str, bytes], str]:
    try:
        snapshot_bytes = _read_regular_nofollow(path, MAX_SNAPSHOT_BYTES, "snapshot")
    except BunproError as error:
        raise BunproImportError(str(error)) from error
    try:
        archive = zipfile.ZipFile(io.BytesIO(snapshot_bytes))
    except zipfile.BadZipFile as error:
        raise BunproImportError(f"invalid snapshot ZIP: {error}") from error
    with archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            raise BunproImportError("snapshot contains duplicate archive members")
        total = 0
        for info in infos:
            _safe_member_name(info.filename, "snapshot member")
            if info.is_dir() or info.flag_bits & 0x1:
                raise BunproImportError("snapshot directories and encrypted members are not allowed")
            total += info.file_size
            if info.file_size > MAX_PAGE_BYTES and info.filename != "snapshot.json":
                raise BunproImportError(f"snapshot member exceeds byte limit: {info.filename}")
        if total > MAX_UNCOMPRESSED_SNAPSHOT_BYTES:
            raise BunproImportError("snapshot uncompressed content exceeds the hard byte limit")
        if "snapshot.json" not in names:
            raise BunproImportError("snapshot.json is missing")
        manifest = _required_object(
            _decode_json(archive.read("snapshot.json"), "snapshot manifest"),
            "snapshot manifest",
        )
        allowed = {
            "format",
            "format_version",
            "source_id",
            "root_sitemap_url",
            "sitemaps",
            "pages",
        }
        if set(manifest) != allowed:
            raise BunproImportError("snapshot manifest fields differ from version 1 schema")
        if (
            manifest.get("format") != SNAPSHOT_FORMAT
            or manifest.get("format_version") != SNAPSHOT_FORMAT_VERSION
            or type(manifest.get("format_version")) is not int
            or manifest.get("source_id") != SOURCE_ID
        ):
            raise BunproImportError("unsupported Bunpro snapshot format, version, or source")
        root_url = _validate_bunpro_url(manifest.get("root_sitemap_url"))
        sitemap_values = _required_list(manifest.get("sitemaps"), "snapshot.sitemaps")
        page_values = _required_list(manifest.get("pages"), "snapshot.pages")
        if not sitemap_values or not page_values:
            raise BunproImportError("snapshot must contain at least one sitemap and grammar page")
        sitemap_entries = [
            _strict_snapshot_entry(
                value,
                f"snapshot.sitemaps[{index}]",
                kind="sitemaps",
                suffix=".xml",
                grammar_page=False,
            )
            for index, value in enumerate(sitemap_values)
        ]
        page_entries = [
            _strict_snapshot_entry(
                value,
                f"snapshot.pages[{index}]",
                kind="pages",
                suffix=".html",
                grammar_page=True,
            )
            for index, value in enumerate(page_values)
        ]
        sitemap_urls = [entry[0] for entry in sitemap_entries]
        page_urls = [entry[0] for entry in page_entries]
        grammar_ids = [entry[3] for entry in page_entries]
        if root_url not in sitemap_urls:
            raise BunproImportError("root_sitemap_url is not present in snapshot.sitemaps")
        if len(sitemap_urls) != len(set(sitemap_urls)):
            raise BunproImportError("snapshot contains duplicate sitemap URLs")
        if len(page_urls) != len(set(page_urls)):
            raise BunproImportError("snapshot contains duplicate grammar-page URLs")
        if len(grammar_ids) != len(set(grammar_ids)):
            raise BunproImportError("snapshot contains duplicate grammar-point IDs")
        expected_members = {"snapshot.json"} | {entry[1] for entry in sitemap_entries + page_entries}
        unexpected = set(names) - expected_members
        missing_members = expected_members - set(names)
        if unexpected:
            raise BunproImportError(f"unexpected archive members: {sorted(unexpected)}")
        if missing_members:
            raise BunproImportError(f"missing archive members: {sorted(missing_members)}")
        documents: dict[str, bytes] = {}
        for url, member, digest, _ in sitemap_entries + page_entries:
            content = archive.read(member)
            if _sha256(content) != digest:
                raise BunproImportError(f"snapshot member SHA-256 mismatch: {member}")
            documents[url] = content

    sitemap_map = {entry[0]: documents[entry[0]] for entry in sitemap_entries}
    discovered_pages: list[str] = []
    pending = [root_url]
    visited: set[str] = set()
    while pending:
        url = pending.pop(0)
        if url in visited:
            raise BunproImportError(f"snapshot sitemap cycle or duplicate at {url}")
        visited.add(url)
        if url not in sitemap_map:
            raise BunproImportError(f"snapshot omits referenced sitemap {url}")
        kind, locations = _parse_sitemap(sitemap_map[url], url)
        if kind == "sitemapindex":
            pending.extend(locations)
        else:
            discovered_pages.extend(
                location
                for location in locations
                if urlsplit(location).path.startswith("/grammar_points/")
                and urlsplit(location).path != "/grammar_points/"
            )
    if set(visited) != set(sitemap_urls):
        raise BunproImportError("snapshot contains unreachable sitemap documents")
    discovered_page_counts = Counter(discovered_pages)
    manifest_page_counts = Counter(page_urls)
    if discovered_page_counts != manifest_page_counts:
        missing = sorted((discovered_page_counts - manifest_page_counts).elements())
        extra = sorted((manifest_page_counts - discovered_page_counts).elements())
        raise BunproImportError(
            f"snapshot page manifest differs from sitemap; missing={missing}, extra={extra}"
        )
    manifest["pages"] = [
        {
            "url": url,
            "path": member,
            "sha256": digest,
            "grammar_point_id": grammar_point_id,
        }
        for url, member, digest, grammar_point_id in page_entries
    ]
    return manifest, documents, _sha256(snapshot_bytes)


def _provenance(
    revision_id: str,
    source_record_id: str,
    url: str,
    locator: str,
    raw_value: Any,
) -> Provenance:
    return Provenance(
        source_id=SOURCE_ID,
        revision_id=revision_id,
        source_record_id=source_record_id,
        locator=f"{url}#__NEXT_DATA__/{locator}",
        content_sha256=_json_hash(raw_value),
    )


def _safe_values(values: Any, context: str) -> list[list[Any]]:
    if values is None:
        return []
    result = []
    for index, value in enumerate(_required_list(values, context)):
        text = _normalized_optional_string(value, f"{context}[{index}]")
        if text is None:
            continue
        result.append(_safe_html(text, f"{context}[{index}]"))
    return result


def _convert_page(
    content: bytes,
    url: str,
    revision_id: str,
    expected_grammar_point_id: int,
) -> tuple[SourceRecord, SourceSense, dict[str, Any]]:
    page_props, reviewable, included = _extract_page_data(content, url)
    grammar_point_id = _required_integer(reviewable["id"], "reviewable.id")
    if grammar_point_id != expected_grammar_point_id:
        raise BunproError(
            f"snapshot expected grammar point {expected_grammar_point_id}, page contains {grammar_point_id}"
        )
    source_record_id = f"{SOURCE_ID}:{revision_id}:record:{grammar_point_id}"
    source_sense_id = f"{source_record_id}:sense:1"
    field_hashes: dict[str, str] = {}
    for field in ("title", "level", "register", "register_translation", "grammar_order"):
        raw_field = reviewable.get(field)
        if raw_field not in (None, ""):
            field_hashes[field.replace("_", "-")] = _json_hash(raw_field)
    blocks: list[ContentBlock] = []
    examples: list[ExamplePair] = []
    links: list[SourceLink] = [SourceLink("Bunpro grammar point", url)]
    missing_sections: list[str] = []
    block_order = 0

    def add_block(
        key: str,
        raw: Any,
        kind: BlockKind,
        language: str,
        locator: str,
        *,
        structured: Any | None = None,
    ) -> None:
        nonlocal block_order
        if raw is None or raw == "" or raw == []:
            return
        if structured is None:
            text = _required_string(raw, key)
            value: Any = {"field": key, "content": _safe_html(text, key)}
            if not _visible_text(value).strip():
                raise BunproError(f"{key} becomes empty after HTML sanitation")
        else:
            value = structured
        provenance = _provenance(revision_id, source_record_id, url, locator, raw)
        blocks.append(
            ContentBlock(
                block_id=f"{source_sense_id}:block:{key}",
                kind=kind,
                language=language,
                content=value,
                order=block_order,
                provenance=provenance,
            )
        )
        field_hashes[key] = provenance.content_sha256
        block_order += 1

    add_block("meaning", reviewable["meaning"], BlockKind.MEANING, "en", "reviewable/meaning")
    add_block("furigana", reviewable["furigana"], BlockKind.OTHER, "ja", "reviewable/furigana")
    add_block("nuance-ja", reviewable.get("nuance"), BlockKind.MEANING, "ja", "reviewable/nuance")
    add_block(
        "nuance-en",
        reviewable.get("nuance_translation"),
        BlockKind.MEANING,
        "en",
        "reviewable/nuance_translation",
    )
    add_block(
        "formation-casual",
        reviewable.get("casual_structure"),
        BlockKind.FORMATION,
        "ja",
        "reviewable/casual_structure",
    )
    add_block(
        "formation-polite",
        reviewable.get("polite_structure"),
        BlockKind.FORMATION,
        "ja",
        "reviewable/polite_structure",
    )
    if not any(block.kind is BlockKind.FORMATION for block in blocks):
        missing_sections.append("formation")
    add_block("caution", reviewable.get("caution"), BlockKind.RESTRICTION, "en", "reviewable/caution")
    add_block(
        "rare-kanji-warning",
        reviewable.get("rare_kanji_warning"),
        BlockKind.RESTRICTION,
        "en",
        "reviewable/rare_kanji_warning",
    )
    for key, language in (
        ("part_of_speech", "ja"),
        ("part_of_speech_translation", "en"),
        ("word_type", "ja"),
        ("word_type_translation", "en"),
        ("metadata", "en"),
    ):
        add_block(key.replace("_", "-"), reviewable.get(key), BlockKind.OTHER, language, f"reviewable/{key}")

    writeups = _required_list(included["writeups"], "included.writeups")
    writeup_ids: set[int] = set()
    substantive_writeups = 0
    for index, raw_writeup in enumerate(writeups):
        writeup = _required_object(raw_writeup, f"included.writeups[{index}]")
        writeup_id = _required_integer(writeup.get("id"), f"writeups[{index}].id")
        if writeup_id in writeup_ids:
            raise BunproError(f"duplicate writeup ID {writeup_id}")
        writeup_ids.add(writeup_id)
        if _required_integer(
            writeup.get("grammar_point_id"), f"writeups[{index}].grammar_point_id"
        ) != grammar_point_id:
            raise BunproError(f"writeup {writeup_id} belongs to another grammar point")
        for field, language in (("body", "en"), ("body_ja", "ja")):
            value = _optional_string(writeup.get(field), f"writeups[{index}].{field}")
            if value:
                add_block(
                    f"writeup-{writeup_id}-{language}",
                    value,
                    BlockKind.MEANING,
                    language,
                    f"included/writeups/{writeup_id}/{field}",
                )
                substantive_writeups += 1
    if substantive_writeups == 0:
        missing_sections.append("writeup")

    questions = _required_list(included["studyQuestions"], "included.studyQuestions")
    question_ids: set[int] = set()
    excluded_example_rows = 0
    excluded_question_fields = 0
    for index, raw_question in enumerate(questions):
        question = _required_object(raw_question, f"included.studyQuestions[{index}]")
        sentenceable_type = question.get("sentenceable_type")
        sentenceable_id = question.get("sentenceable_id")
        if sentenceable_type == "Vocab":
            excluded_example_rows += 1
            continue
        belongs = (sentenceable_type == "GrammarPoint" and sentenceable_id == grammar_point_id) or (
            sentenceable_type == "Writeup" and sentenceable_id in writeup_ids
        )
        if not belongs:
            raise BunproError(
                f"study question {question.get('id')!r} has unexpected ownership "
                f"{sentenceable_type!r}/{sentenceable_id!r}"
            )
        validation_status = _required_string(
            question.get("validation_status"),
            f"studyQuestions[{index}].validation_status",
        )
        if validation_status not in {"validated", "unvalidated"}:
            raise BunproError(
                f"unsupported study question validation status: {validation_status!r}"
            )
        question_type = _required_string(
            question.get("question_type"), f"studyQuestions[{index}].question_type"
        )
        if question_type not in {"cloze", "readonly", "writeups", "draft"}:
            raise BunproError(f"unsupported study question type: {question_type!r}")
        if question_type == "draft":
            excluded_example_rows += 1
            continue
        question_id = _required_integer(question.get("id"), f"studyQuestions[{index}].id")
        if question_id in question_ids:
            raise BunproError(f"duplicate study question ID {question_id}")
        question_ids.add(question_id)
        question_content = _required_string(
            question.get("content"), f"studyQuestions[{index}].content"
        )
        answer = _optional_string(question.get("answer"), f"studyQuestions[{index}].answer")
        kanji_answer = _optional_string(
            question.get("kanji_answer"), f"studyQuestions[{index}].kanji_answer"
        )
        sanitized_answer = (
            _safe_html(answer, f"study question {question_id} answer") if answer else None
        )
        if sanitized_answer is not None and not _visible_text(sanitized_answer).strip():
            raise BunproError(
                f"study question {question_id} answer becomes empty after HTML sanitation"
            )
        sanitized_kanji_answer = (
            _safe_html(kanji_answer, f"study question {question_id} kanji answer")
            if kanji_answer
            else None
        )
        if sanitized_kanji_answer is not None and not _visible_text(
            sanitized_kanji_answer
        ).strip():
            raise BunproError(
                f"study question {question_id} kanji answer becomes empty after HTML sanitation"
            )
        if answer is None and kanji_answer is None:
            if question_type != "readonly" or "____" in question_content:
                raise BunproError(f"study question {question_id} has no answer")
        sanitized_question_content = _safe_html(
            question_content,
            f"study question {question_id} content",
        )
        if not _visible_text(sanitized_question_content).strip():
            raise BunproError(
                f"study question {question_id} content becomes empty after HTML sanitation"
            )
        japanese: dict[str, Any] = {
            "content": sanitized_question_content,
            "answer": sanitized_answer,
            "kanji_answer": sanitized_kanji_answer,
            "alternate_grammar": _safe_values(
                question.get("alternate_grammar", []),
                f"studyQuestions[{index}].alternate_grammar",
            ),
            "kanji_alt_grammar": _safe_values(
                question.get("kanji_alt_grammar", []),
                f"studyQuestions[{index}].kanji_alt_grammar",
            ),
            "source_validation_status": validation_status,
        }
        word_prompt = _normalized_optional_string(
            question.get("word_prompt"), f"studyQuestions[{index}].word_prompt"
        )
        if word_prompt:
            japanese["word_prompt"] = _safe_html(
                word_prompt, f"study question {question_id} word_prompt"
            )
        tense = _optional_string(question.get("tense"), f"studyQuestions[{index}].tense")
        if tense:
            japanese["source_label"] = tense
        translation_raw = _optional_string(
            question.get("translation"), f"studyQuestions[{index}].translation"
        )
        translation = (
            _safe_html(translation_raw, f"study question {question_id} translation")
            if translation_raw
            else None
        )
        provenance = _provenance(
            revision_id,
            source_record_id,
            url,
            f"included/studyQuestions/{question_id}",
            question,
        )
        examples.append(
            ExamplePair(
                example_id=f"{source_sense_id}:example:{question_id}",
                japanese=japanese,
                translation=translation,
                translation_language="en" if translation is not None else None,
                order=_required_integer(
                    question.get("sentence_order", index),
                    f"studyQuestions[{index}].sentence_order",
                ),
                provenance=provenance,
            )
        )
        field_hashes[f"study-question-{question_id}"] = provenance.content_sha256
        extra_info = _optional_string(
            question.get("extra_info"), f"studyQuestions[{index}].extra_info"
        )
        if extra_info:
            add_block(
                f"example-note-{question_id}",
                extra_info,
                BlockKind.EXAMPLE_NOTE,
                "en",
                f"included/studyQuestions/{question_id}/extra_info",
            )
        imported_question_fields = {
            "id",
            "content",
            "answer",
            "alternate_grammar",
            "kanji_answer",
            "kanji_alt_grammar",
            "translation",
            "word_prompt",
            "tense",
            "extra_info",
            "sentence_order",
            "sentenceable_type",
            "sentenceable_id",
            "validation_status",
        }
        excluded_question_fields += len(set(question) - imported_question_fields)
    if not examples:
        missing_sections.append("examples")
    if all(section in missing_sections for section in ("formation", "writeup", "examples")):
        raise BunproError("grammar page contains no substantive formation, writeup, or example content")

    offline_resources = _required_list(included["offlineResources"], "included.offlineResources")
    if offline_resources:
        normalized_resources = []
        for index, raw_resource in enumerate(offline_resources):
            resource = _required_object(raw_resource, f"offlineResources[{index}]")
            normalized_resources.append(
                {
                    "source": _required_string(resource.get("source"), f"offlineResources[{index}].source"),
                    "location": _optional_string(
                        resource.get("location"), f"offlineResources[{index}].location"
                    ),
                }
            )
        add_block(
            "offline-resources",
            offline_resources,
            BlockKind.OTHER,
            "en",
            "included/offlineResources",
            structured={"field": "offline-resources", "items": normalized_resources},
        )

    supplemental_links = _required_list(included["supplementalLinks"], "included.supplementalLinks")
    field_hashes["supplemental-links"] = _json_hash(supplemental_links)
    for index, raw_link in enumerate(supplemental_links):
        link = _required_object(raw_link, f"supplementalLinks[{index}]")
        site = _normalized_optional_string(link.get("site"), f"supplementalLinks[{index}].site")
        description = _normalized_optional_string(
            link.get("description"), f"supplementalLinks[{index}].description"
        )
        label = "Reference" + (f": {site}" if site else "") + (f" — {description}" if description else "")
        links.append(SourceLink(label, _required_string(link.get("link"), f"supplementalLinks[{index}].link")))

    discourse = _optional_string(reviewable.get("discourse_link"), "reviewable.discourse_link")
    if discourse:
        links.append(SourceLink("Bunpro community discussion", discourse))
        field_hashes["discourse-link"] = _json_hash(discourse)

    related_contents = _required_list(included["relatedContents"], "included.relatedContents")
    field_hashes["related-links"] = _json_hash(related_contents)
    excluded_related_rows = 0
    for index, raw_relation in enumerate(related_contents):
        relation = _required_object(raw_relation, f"relatedContents[{index}]")
        relation_id = _required_integer(relation.get("id"), f"relatedContents[{index}].id")
        endpoints = [
            _required_object(relation.get("first_relatable"), f"relatedContents[{index}].first"),
            _required_object(relation.get("second_relatable"), f"relatedContents[{index}].second"),
        ]
        grammar_endpoints = [item for item in endpoints if item.get("type_snake") == "grammar_point"]
        current = [item for item in grammar_endpoints if item.get("id") == grammar_point_id]
        others = [item for item in grammar_endpoints if item.get("id") != grammar_point_id]
        if grammar_endpoints and not current:
            raise BunproError(
                f"related content {relation_id} does not belong to grammar point {grammar_point_id}"
            )
        if len(grammar_endpoints) == 2 and (len(current) != 1 or len(others) != 1):
            raise BunproError(
                f"related content {relation_id} does not belong to grammar point {grammar_point_id}"
            )
        if len(grammar_endpoints) != 2:
            excluded_related_rows += 1
            continue
        other = others[0]
        other_slug = _required_string(other.get("slug"), f"relatedContents[{index}].slug").strip()
        other_title = _required_string(other.get("title"), f"relatedContents[{index}].title").strip()
        related_url = f"https://bunpro.jp/grammar_points/{quote(other_slug, safe='')}"
        relationship = _required_string(
            relation.get("relationship_type"), f"relatedContents[{index}].relationship_type"
        ).strip()
        links.append(SourceLink(f"{relationship.title()}: {other_title}", related_url))
        relation_body = _optional_string(relation.get("body"), f"relatedContents[{index}].body")
        if relation_body:
            add_block(
                f"related-{relation_id}",
                relation_body,
                BlockKind.OTHER,
                "en",
                f"included/relatedContents/{relation_id}/body",
            )

    seen_links: set[tuple[str, str]] = set()
    unique_links = []
    for link in links:
        identity = (link.label, link.url)
        if identity not in seen_links:
            seen_links.add(identity)
            unique_links.append(link)

    attached = _required_list(included["attachedReviewables"], "included.attachedReviewables")
    excluded_vocabulary_rows = sum(
        1 for item in attached if isinstance(item, dict) and item.get("type_snake") == "vocab"
    )
    excluded_non_content_fields = (
        len(set(page_props) - {"reviewable", "included", "error"})
        + len(
            set(reviewable)
            - {
                "id",
                "level",
                "part_of_speech",
                "register",
                "word_type",
                "slug",
                "nuance",
                "grammar_order",
                "discourse_link",
                "type_snake",
                "title",
                "furigana",
                "meaning",
                "nuance_translation",
                "rare_kanji_warning",
                "caution",
                "polite_structure",
                "casual_structure",
                "part_of_speech_translation",
                "register_translation",
                "word_type_translation",
                "metadata",
            }
        )
        + excluded_question_fields
        + len(_required_list(included["articles"], "included.articles"))
    )

    register_labels = []
    for raw_register in (reviewable.get("register"), reviewable.get("register_translation")):
        value = _optional_string(raw_register, "reviewable register")
        if value and value not in register_labels:
            register_labels.append(value)
    level = _required_string(reviewable["level"], "reviewable.level")
    record = SourceRecord(
        source_record_id=source_record_id,
        raw_expression=_required_string(reviewable["title"], "reviewable.title").strip(),
        order=_required_integer(reviewable["grammar_order"], "reviewable.grammar_order"),
        field_hashes=field_hashes,
    )
    sense = SourceSense(
        source_sense_id=source_sense_id,
        source_record_id=source_record_id,
        partition_status=PartitionStatus.SOURCE_EXPLICIT,
        blocks=tuple(blocks),
        examples=tuple(examples),
        level_labels=(level,),
        register_labels=tuple(register_labels),
        links=tuple(unique_links),
    )
    metrics = {
        "level": level,
        "blocks": len(blocks),
        "examples": len(examples),
        "links": len(unique_links),
        "missing_sections": missing_sections,
        "excluded_vocabulary_rows": excluded_vocabulary_rows,
        "excluded_example_rows": excluded_example_rows,
        "excluded_related_rows": excluded_related_rows,
        "excluded_non_content_fields": excluded_non_content_fields,
    }
    return record, sense, metrics


def convert_snapshot(
    snapshot_path: str | Path,
    revision_id: str,
) -> tuple[SourceBundle, dict[str, Any]]:
    """Convert one exact local snapshot, refusing any partial record import."""
    revision_id = _required_string(revision_id, "revision_id")
    snapshot_path = Path(snapshot_path)
    manifest, documents, snapshot_sha256 = _load_snapshot(snapshot_path)
    page_entries = manifest["pages"]
    expected_source_record_ids = [
        f"{SOURCE_ID}:{revision_id}:record:{entry['grammar_point_id']}" for entry in page_entries
    ]
    records: list[SourceRecord] = []
    senses: list[SourceSense] = []
    rejected: list[dict[str, Any]] = []
    metrics = Counter()
    level_counts = Counter()
    missing_sections: dict[str, list[str]] = {}
    for entry in page_entries:
        url = entry["url"]
        grammar_point_id = entry["grammar_point_id"]
        try:
            record, sense, page_metrics = _convert_page(
                documents[url], url, revision_id, grammar_point_id
            )
        except (BunproError, KeyError, TypeError, ValueError) as error:
            rejected.append(
                {
                    "grammar_point_id": grammar_point_id,
                    "url": url,
                    "reason": str(error),
                }
            )
            continue
        records.append(record)
        senses.append(sense)
        level_counts[page_metrics["level"]] += 1
        for name in (
            "blocks",
            "examples",
            "links",
            "excluded_vocabulary_rows",
            "excluded_example_rows",
            "excluded_related_rows",
            "excluded_non_content_fields",
        ):
            metrics[name] += page_metrics[name]
        if page_metrics["missing_sections"]:
            missing_sections[record.source_record_id] = page_metrics["missing_sections"]

    report: dict[str, Any] = {
        "format": "ugd-bunpro-import-report",
        "format_version": REPORT_FORMAT_VERSION,
        "source_id": SOURCE_ID,
        "revision_id": revision_id,
        "snapshot_sha256": snapshot_sha256,
        "content_status": "substantive-official-html",
        "publication_mode": PublicationMode.UNCLEARED.value,
        "expected_record_count": len(page_entries),
        "imported_record_count": len(records),
        "rejected_record_count": len(rejected),
        "expected_source_record_ids": expected_source_record_ids,
        "imported_source_record_ids": [record.source_record_id for record in records],
        "rejected_records": rejected,
        "levels": dict(sorted(level_counts.items())),
        "blocks": metrics["blocks"],
        "examples": metrics["examples"],
        "links": metrics["links"],
        "records_missing_optional_sections": missing_sections,
        "excluded_vocabulary_rows": metrics["excluded_vocabulary_rows"],
        "excluded_example_rows": metrics["excluded_example_rows"],
        "excluded_related_rows": metrics["excluded_related_rows"],
        "excluded_non_content_fields": metrics["excluded_non_content_fields"],
    }
    if rejected:
        raise BunproImportError(
            f"refusing partial Bunpro import: {len(rejected)} of {len(page_entries)} records rejected",
            report,
        )
    if set(expected_source_record_ids) != {record.source_record_id for record in records}:
        raise BunproImportError("exact Bunpro source-record identity coverage failed", report)

    source_revision = SourceRevision(
        source_id=SOURCE_ID,
        revision_id=revision_id,
        content_sha256=snapshot_sha256,
        languages=("ja", "en"),
        attribution="Bunpro; preserve grammar-point URLs and source identity.",
        license=LicenseInfo(
            identifier=None,
            notice=(
                "No open redistribution licence was established for Bunpro-derived content; "
                "private/local import only until separately cleared."
            ),
            evidence_url=None,
        ),
        access_mode=AccessMode.PUBLIC_HTTP,
        import_mode=ImportMode.CONTENT,
        publication_mode=PublicationMode.UNCLEARED,
        generated=False,
        provenance_note=(
            "Official public grammar-point HTML enumerated through the pinned Bunpro sitemap; "
            "account/SRS state, audio, vocab coverage rows, and answer feedback are excluded."
        ),
    )
    bundle = SourceBundle(
        source_revision=source_revision,
        records=tuple(records),
        senses=tuple(senses),
        media=(),
    )
    bundle.validate()
    return bundle, report


def _write_json(path: Path, value: Any) -> None:
    _atomic_write(path, canonical_json_bytes(value))


def _error_report(error: Exception) -> dict[str, Any] | None:
    report = getattr(error, "report", None)
    return report if isinstance(report, dict) and report else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch a bounded official Bunpro HTML snapshot or convert an exact local snapshot "
            "to the canonical private grammar model."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch", help="fetch a complete sitemap-bounded local snapshot")
    fetch.add_argument("--sitemap-url", default=DEFAULT_SITEMAP_URL)
    fetch.add_argument("--snapshot", type=Path, required=True)
    fetch.add_argument("--cache-dir", type=Path, required=True)
    fetch.add_argument("--report", type=Path, required=True)
    fetch.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    fetch.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    fetch.add_argument("--request-delay", type=float, default=DEFAULT_REQUEST_DELAY_SECONDS)
    fetch.add_argument("--max-pages", type=int, default=MAX_PAGES)
    fetch.add_argument("--refresh", action="store_true")

    convert = commands.add_parser("convert", help="convert an explicit local snapshot")
    convert.add_argument("--snapshot", type=Path, required=True)
    convert.add_argument("--revision", required=True)
    convert.add_argument("--output", type=Path, required=True)
    convert.add_argument("--report", type=Path, required=True)

    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "fetch":
            report = fetch_snapshot(
                arguments.sitemap_url,
                arguments.snapshot,
                arguments.cache_dir,
                timeout=arguments.timeout,
                retries=arguments.retries,
                refresh=arguments.refresh,
                request_delay=arguments.request_delay,
                max_pages=arguments.max_pages,
            )
            _write_json(arguments.report, report)
        else:
            bundle, report = convert_snapshot(arguments.snapshot, arguments.revision)
            _write_json(arguments.output, bundle.to_dict())
            _write_json(arguments.report, report)
    except (BunproError, OSError, zipfile.BadZipFile) as error:
        report = _error_report(error)
        if report is not None and hasattr(arguments, "report"):
            try:
                _write_json(arguments.report, report)
            except OSError:
                pass
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

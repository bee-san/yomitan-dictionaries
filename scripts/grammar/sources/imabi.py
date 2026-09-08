"""Public-safe IMABI references and private full-content ingestion."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
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
import unicodedata
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
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
    from scripts.grammar.registry import PolicyError, validate_remote_url  # type: ignore[import-not-found]
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


REFERENCE_FORMAT_VERSION = 1
REFERENCE_REVISION_ID = "ugd04-2026-09-08"
TABLE_OF_CONTENTS_URL = "https://imabi.org/table-of-contents-%E7%9B%AE%E6%AC%A1"
_MATERIAL_KINDS = frozenset({"modern", "classical"})
_REFERENCE_KINDS = frozenset({"link-only", "reference-only"})
_AUDITED_RECORDS = (
    (
        0,
        "Introduction to Japanese",
        "https://imabi.org/what-is-japanese/",
        "Beginners 1",
        "modern",
        "reference-only",
    ),
    (
        4,
        "Hiragana",
        "https://imabi.org/hiragana%E3%80%80%E3%81%B2%E3%82%89%E3%81%8C%E3%81%AA",
        "Beginners 1",
        "modern",
        "link-only",
    ),
    (
        260,
        "No Doubt that",
        "https://imabi.org/no-doubt-that/",
        "Advanced I",
        "modern",
        "link-only",
    ),
    (
        12,
        "The Auxiliary Verb ～ず II",
        "https://imabi.org/the-auxiliary-verb-%EF%BD%9E%E3%81%9A-ii/",
        "Classical Japanese",
        "classical",
        "link-only",
    ),
    (
        1,
        "Introduction to Classical Japanese",
        "https://imabi.org/intro-to-classical/",
        "Classical Japanese",
        "classical",
        "reference-only",
    ),
    (
        18,
        "Classical Adverbs",
        "https://imabi.org/classical-adverbs/",
        "Classical Japanese",
        "classical",
        "link-only",
    ),
)
_AUDITED_URLS = frozenset(record[2] for record in _AUDITED_RECORDS)


class ImabiReferenceError(ValueError):
    """An IMABI link-only metadata record exceeds the audited contract."""


def _plain_metadata(value: str, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ImabiReferenceError(f"{field} must be non-empty, unpadded plain metadata")
    if "<" in value or ">" in value or any(
        unicodedata.category(character).startswith("C") for character in value
    ):
        raise ImabiReferenceError(f"{field} must be plain metadata without markup")
    return value


def _canonical_lesson_url(value: str) -> str:
    if not isinstance(value, str):
        raise ImabiReferenceError("canonical_url must be a string")
    _plain_metadata(value, "canonical_url")
    if any(character.isspace() for character in value):
        raise ImabiReferenceError("canonical_url must not contain whitespace")
    parsed = urlsplit(value)
    if parsed.scheme != "https":
        raise ImabiReferenceError("canonical_url must use HTTPS")
    if parsed.username or parsed.password:
        raise ImabiReferenceError("canonical_url must not contain credentials")
    try:
        parsed.port
    except ValueError as error:
        raise ImabiReferenceError("canonical_url has an invalid port") from error
    if parsed.netloc != "imabi.org":
        raise ImabiReferenceError("canonical_url must use the canonical imabi.org host")
    if value not in _AUDITED_URLS:
        raise ImabiReferenceError("canonical_url is not one of the audited IMABI lesson URLs")
    return value


def source_lesson_id(material_kind: str, lesson_number: int) -> str:
    """Build a stable ID in a namespace that separates reused lesson numbers."""
    if not isinstance(material_kind, str) or material_kind not in _MATERIAL_KINDS:
        raise ImabiReferenceError("material_kind must be modern or classical")
    if (
        not isinstance(lesson_number, int)
        or isinstance(lesson_number, bool)
        or lesson_number < 0
    ):
        raise ImabiReferenceError("lesson_number must be a non-negative integer")
    return f"imabi:{material_kind}:lesson:{lesson_number}"


@dataclass(frozen=True, slots=True)
class ImabiLessonReference:
    """One lesson-level link; it deliberately has no lesson-content fields."""

    lesson_number: int
    title: str
    canonical_url: str
    toc_section: str
    material_kind: str
    reference_kind: str

    def __post_init__(self) -> None:
        source_lesson_id(self.material_kind, self.lesson_number)
        _plain_metadata(self.title, "title")
        _plain_metadata(self.toc_section, "toc_section")
        _canonical_lesson_url(self.canonical_url)
        if not isinstance(self.reference_kind, str) or self.reference_kind not in _REFERENCE_KINDS:
            raise ImabiReferenceError(
                "reference_kind must be link-only or reference-only"
            )
        if (
            self.lesson_number,
            self.title,
            self.canonical_url,
            self.toc_section,
            self.material_kind,
            self.reference_kind,
        ) not in _AUDITED_RECORDS:
            raise ImabiReferenceError("reference is not one of the audited metadata records")

    @property
    def source_lesson_id(self) -> str:
        return source_lesson_id(self.material_kind, self.lesson_number)

    def to_dict(self) -> dict[str, object]:
        return {
            "source_lesson_id": self.source_lesson_id,
            "lesson_number": self.lesson_number,
            "title": self.title,
            "canonical_url": self.canonical_url,
            "toc_section": self.toc_section,
            "material_kind": self.material_kind,
            "reference_kind": self.reference_kind,
        }


# These six lesson-level records are the exact bounded sample verified by UGD-04.
# They are intentionally not represented as complete IMABI coverage.
_AUDITED_REFERENCES = tuple(ImabiLessonReference(*record) for record in _AUDITED_RECORDS)


def audited_reference_index() -> dict[str, object]:
    """Return fresh, deterministic link-only metadata for the audited sample."""
    references = [reference.to_dict() for reference in _AUDITED_REFERENCES]
    source_lesson_ids = [reference["source_lesson_id"] for reference in references]
    if len(source_lesson_ids) != len(set(source_lesson_ids)):
        raise RuntimeError("duplicate IMABI source lesson ID")
    return {
        "format": "ugd-source-reference-index",
        "format_version": REFERENCE_FORMAT_VERSION,
        "source_id": "imabi",
        "revision_id": REFERENCE_REVISION_ID,
        "mode": "metadata-link-only",
        "coverage": "partial-audited",
        "content_status": "not-included-pending-follow-up",
        "permission_basis": "user-reported-project-specific-author-approval",
        "publication_status": "not-authorized",
        "table_of_contents_url": TABLE_OF_CONTENTS_URL,
        "reference_count": len(references),
        "lesson_body_count": 0,
        "content_block_count": 0,
        "example_count": 0,
        "media_count": 0,
        "references": references,
    }


def audited_reference_json() -> bytes:
    """Serialize the audited metadata deterministically for downstream adapters."""
    return canonical_json_bytes(audited_reference_index())


SOURCE_ID = "imabi"
SNAPSHOT_FORMAT = "imabi-html-snapshot"
SNAPSHOT_FORMAT_VERSION = 1
CONTENT_REPORT_FORMAT_VERSION = 1
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_REQUEST_DELAY_SECONDS = 0.1
MAX_INDEXED_LESSONS = 600
MAX_PAGE_BYTES = 5 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 256 * 1024 * 1024
MAX_UNCOMPRESSED_SNAPSHOT_BYTES = 512 * 1024 * 1024
MAX_HTML_NODES = 250_000
MAX_HTML_DEPTH = 192
_LESSON_NUMBER = re.compile(r"第\s*0*(\d+)\s*課")
_EXAMPLE_MARKER = re.compile(r"^(?:\d{1,3}|[ivxlcdm]+|[①-⑳])(?:[.)．])?\s+", re.I)
_JAPANESE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
_LATIN = re.compile(r"[A-Za-z]")
_VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})
_BLOCKED_TAGS = frozenset({"script", "style", "iframe", "object", "form", "nav", "footer", "aside", "noscript", "template"})
_SAFE_TAGS = frozenset(
    {
        "a",
        "abbr",
        "b",
        "blockquote",
        "br",
        "code",
        "dd",
        "del",
        "details",
        "div",
        "dl",
        "dt",
        "em",
        "figcaption",
        "figure",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "hr",
        "i",
        "img",
        "ins",
        "kbd",
        "li",
        "mark",
        "ol",
        "p",
        "pre",
        "q",
        "rp",
        "rt",
        "ruby",
        "s",
        "small",
        "span",
        "strong",
        "sub",
        "summary",
        "sup",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "u",
        "ul",
        "var",
    }
)
_CONTENT_UNIT_TAGS = frozenset(
    {"h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "blockquote", "pre", "figure", "table", "dl"}
)
_CHROME_CLASSES = frozenset(
    {
        "sharedaddy",
        "sd-sharing-enabled",
        "sd-like",
        "jetpack-likes-widget-wrapper",
        "jp-relatedposts",
        "post-likes-widget",
    }
)

FetchBytes = Callable[[str, float], bytes]
Delay = Callable[[float], None]


class ImabiContentError(ValueError):
    """The permission-aware IMABI acquisition or conversion contract failed."""


class ImabiFetchError(ImabiContentError):
    """A complete bounded IMABI snapshot could not be acquired."""

    def __init__(self, message: str, report: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.report = dict(report or {})


class ImabiImportError(ImabiContentError):
    """A complete IMABI snapshot could not be converted without data loss."""

    def __init__(self, message: str, report: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.report = dict(report or {})


@dataclass(slots=True)
class _Node:
    tag: str
    attrs: dict[str, str | None]
    children: list[Any]


class _TreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("document", {}, [])
        self.stack = [self.root]
        self.node_count = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        names = [name.lower() for name, _value in attrs]
        if len(names) != len(set(names)):
            raise ImabiContentError(f"duplicate HTML attribute on <{tag}>")
        self.node_count += 1
        if self.node_count > MAX_HTML_NODES:
            raise ImabiContentError(f"HTML exceeds {MAX_HTML_NODES} nodes")
        node = _Node(tag, {name.lower(): value for name, value in attrs}, [])
        self.stack[-1].children.append(node)
        if tag not in _VOID_TAGS:
            self.stack.append(node)
            if len(self.stack) > MAX_HTML_DEPTH:
                raise ImabiContentError(f"HTML exceeds nesting depth {MAX_HTML_DEPTH}")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _json_hash(value: Any) -> str:
    return _sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ImabiContentError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _decode_json(content: bytes, context: str) -> Any:
    try:
        return json.loads(content, object_pairs_hook=_unique_object)
    except ImabiContentError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ImabiContentError(f"invalid {context} JSON: {error}") from error


def _required_string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ImabiContentError(f"{context} must be a non-empty string")
    return value


def _required_integer(value: Any, context: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ImabiContentError(f"{context} must be a non-negative integer")
    return value


def _required_list(value: Any, context: str) -> list[Any]:
    if not isinstance(value, list):
        raise ImabiContentError(f"{context} must be a list")
    return value


def _required_object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ImabiContentError(f"{context} must be an object")
    return value


def _clean_text(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())


def _node_text(node: _Node, *, include_rt: bool = True) -> str:
    if node.tag in _BLOCKED_TAGS or _is_chrome(node):
        return ""
    if node.tag == "rt" and not include_rt:
        return ""
    return _clean_text(
        "".join(
            child if isinstance(child, str) else _node_text(child, include_rt=include_rt)
            for child in node.children
        )
    )


def _walk(node: _Node):
    yield node
    for child in node.children:
        if isinstance(child, _Node):
            yield from _walk(child)


def _parse_html(content: bytes, context: str) -> _Node:
    if not content or len(content) > MAX_PAGE_BYTES:
        raise ImabiContentError(f"{context} is empty or exceeds {MAX_PAGE_BYTES} bytes")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ImabiContentError(f"{context} is not valid UTF-8") from error
    parser = _TreeParser()
    try:
        parser.feed(text)
        parser.close()
    except (ImabiContentError, AssertionError) as error:
        raise ImabiContentError(f"invalid {context}: {error}") from error
    return parser.root


def _class_tokens(node: _Node) -> set[str]:
    value = node.attrs.get("class")
    return set(value.split()) if isinstance(value, str) else set()


def _is_chrome(node: _Node) -> bool:
    classes = _class_tokens(node)
    return bool(classes & _CHROME_CLASSES) or any(
        value.startswith(("sharedaddy", "jetpack-likes", "sd-sharing", "jp-relatedposts"))
        for value in classes
    )


def _entry_content(root: _Node, context: str) -> _Node:
    matches = [
        node
        for node in _walk(root)
        if node.tag == "div" and "entry-content" in _class_tokens(node)
    ]
    if len(matches) != 1:
        raise ImabiContentError(f"{context} must contain exactly one entry-content element")
    return matches[0]


def _canonical_content_url(value: Any) -> str:
    value = _required_string(value, "IMABI URL")
    if value != value.strip() or any(
        character.isspace() or unicodedata.category(character).startswith("C")
        for character in value
    ):
        raise ImabiContentError("IMABI URL must be unpadded and contain no whitespace or controls")
    try:
        validate_remote_url(SOURCE_ID, value)
        parsed = urlsplit(value)
    except (PolicyError, ValueError) as error:
        raise ImabiContentError(str(error)) from error
    if parsed.hostname != "imabi.org" or parsed.netloc != "imabi.org":
        raise ImabiContentError("IMABI content URL must use canonical imabi.org without a port")
    if parsed.query or parsed.fragment:
        raise ImabiContentError("IMABI content URL must not contain a query or fragment")
    decoded_path = unquote(parsed.path)
    if not decoded_path.startswith("/") or ".." in PurePosixPath(decoded_path).parts:
        raise ImabiContentError("IMABI content URL has an unsafe path")
    path = quote(decoded_path, safe="/:@-._~!$&'()*+,;=")
    if not path.endswith("/"):
        path += "/"
    return urlunsplit(("https", "imabi.org", path, "", ""))


def _material_kind(toc_section: str) -> str:
    lower = toc_section.lower()
    if "classical" in lower or "古典" in toc_section:
        return "classical"
    if "okinawan" in lower or "琉球" in toc_section:
        return "okinawan"
    return "modern"


def _toc_items(content: bytes, toc_url: str) -> list[dict[str, Any]]:
    root = _parse_html(content, "IMABI table of contents")
    entry = _entry_content(root, "IMABI table of contents")
    section = ""
    pending = ""
    raw_items: list[dict[str, Any]] = []

    def visit(value: Any) -> None:
        nonlocal section, pending
        if isinstance(value, str):
            pending += value.replace("\xa0", " ")
            return
        if value.tag in _BLOCKED_TAGS or _is_chrome(value):
            return
        if value.tag in {"h2", "h3"}:
            section = _node_text(value)
            pending = ""
            return
        if value.tag == "a":
            title = _node_text(value)
            context = _clean_text(pending + " " + title)
            numbers = _LESSON_NUMBER.findall(context)
            href = value.attrs.get("href")
            pending = ""
            if not section or not title or not numbers or not isinstance(href, str):
                return
            try:
                canonical_url = _canonical_content_url(urljoin(toc_url, href))
            except ImabiContentError:
                return
            kind = _material_kind(section)
            raw_items.append(
                {
                    "index": len(raw_items),
                    "lesson_number": int(numbers[-1]),
                    "title": title,
                    "canonical_url": canonical_url,
                    "toc_section": section,
                    "material_kind": kind,
                    "status": "eligible" if kind in {"modern", "classical"} else "not-applicable",
                    "reason": None
                    if kind in {"modern", "classical"}
                    else "outside the selected modern/classical Japanese grammar scope",
                }
            )
            return
        for child in value.children:
            visit(child)

    visit(entry)
    if not raw_items:
        raise ImabiContentError("IMABI table of contents contains no numbered lesson links")
    urls = [item["canonical_url"] for item in raw_items]
    if len(urls) != len(set(urls)):
        raise ImabiContentError("IMABI table of contents contains duplicate canonical lesson URLs")
    counts = Counter((item["material_kind"], item["lesson_number"]) for item in raw_items)
    for item in raw_items:
        if item["material_kind"] in _MATERIAL_KINDS:
            lesson_id = source_lesson_id(item["material_kind"], item["lesson_number"])
        else:
            lesson_id = f"imabi:{item['material_kind']}:lesson:{item['lesson_number']}"
        if counts[(item["material_kind"], item["lesson_number"])] > 1:
            lesson_id += f":variant:{_sha256(item['canonical_url'].encode())[:12]}"
        item["source_lesson_id"] = lesson_id
    lesson_ids = [item["source_lesson_id"] for item in raw_items]
    if len(lesson_ids) != len(set(lesson_ids)):
        raise ImabiContentError("IMABI table of contents produces duplicate lesson identities")
    return raw_items


def _safe_member_name(value: Any, context: str) -> str:
    value = _required_string(value, context)
    path = PurePosixPath(value)
    if (
        "\\" in value
        or path.is_absolute()
        or path.as_posix() != value
        or not path.parts
        or ".." in path.parts
    ):
        raise ImabiContentError(f"{context} must be a safe POSIX-relative path")
    return value


def _read_regular_nofollow(path: Path, limit: int, context: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ImabiContentError(f"cannot open {context}: {error}") from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ImabiContentError(f"{context} must be a regular file")
        if info.st_size > limit:
            raise ImabiContentError(f"{context} exceeds {limit} bytes")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) > limit:
            raise ImabiContentError(f"{context} exceeds {limit} bytes")
        return content
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


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
        _canonical_content_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download_bytes(url: str, timeout: float) -> bytes:
    url = _canonical_content_url(url)
    request = Request(url, headers={"User-Agent": "bee-grammar-builder/1"})
    with build_opener(_RestrictedRedirect()).open(request, timeout=timeout) as response:
        final_url = _canonical_content_url(response.geturl())
        if final_url != url:
            raise ImabiFetchError(f"IMABI page redirected away from its canonical URL: {url}")
        declared = response.headers.get("Content-Length")
        if declared is not None and int(declared) > MAX_PAGE_BYTES:
            raise ImabiFetchError(f"IMABI response exceeds {MAX_PAGE_BYTES} bytes: {url}")
        content = response.read(MAX_PAGE_BYTES + 1)
    if not content or len(content) > MAX_PAGE_BYTES:
        raise ImabiFetchError(f"IMABI response is empty or too large: {url}")
    return content


def _cache_path(cache_directory: Path, kind: str, url: str) -> Path:
    return cache_directory / kind / f"{_sha256(url.encode())}.html"


def _cached_or_fetch(
    cache_directory: Path,
    kind: str,
    url: str,
    timeout: float,
    fetch: FetchBytes,
    delay: Delay,
    request_delay: float,
) -> tuple[bytes, bool]:
    path = _cache_path(cache_directory, kind, url)
    if path.exists():
        content = _read_regular_nofollow(path, MAX_PAGE_BYTES, "IMABI cache entry")
        if content:
            return content, False
    if request_delay:
        delay(request_delay)
    content = fetch(url, timeout)
    if not isinstance(content, bytes) or not content or len(content) > MAX_PAGE_BYTES:
        raise ImabiFetchError(f"invalid bounded response for {url}")
    _atomic_write(path, content)
    return content, True


def _snapshot_member(url: str) -> str:
    return f"pages/{_sha256(url.encode())}.html"


def _write_snapshot(path: Path, members: Mapping[str, bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w+b") as handle:
            with zipfile.ZipFile(handle, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
                for name in sorted(members):
                    info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.create_system = 3
                    info.external_attr = (stat.S_IFREG | 0o644) << 16
                    archive.writestr(info, members[name])
            handle.flush()
            os.fsync(handle.fileno())
            handle.seek(0)
            content = handle.read(MAX_SNAPSHOT_BYTES + 1)
        if len(content) > MAX_SNAPSHOT_BYTES:
            raise ImabiContentError(f"temporary IMABI snapshot exceeds {MAX_SNAPSHOT_BYTES} bytes")
        _atomic_write(path, content)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _index_anomalies(items: list[dict[str, Any]]) -> dict[str, Any]:
    duplicate_numbers: list[dict[str, Any]] = []
    missing_numbers: dict[str, list[int]] = {}
    groups: dict[str, list[int]] = defaultdict(list)
    for item in items:
        groups[item["material_kind"]].append(item["lesson_number"])
    for kind, numbers in sorted(groups.items()):
        counts = Counter(numbers)
        for number, count in sorted(counts.items()):
            if count > 1:
                duplicate_numbers.append({"material_kind": kind, "lesson_number": number, "count": count})
        unique = set(numbers)
        if unique:
            missing_numbers[kind] = sorted(set(range(min(unique), max(unique) + 1)) - unique)
    return {"duplicate_lesson_numbers": duplicate_numbers, "missing_lesson_numbers": missing_numbers}


def fetch_snapshot(
    toc_url: str | Path,
    snapshot_path: str | Path,
    cache_directory: str | Path,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    request_delay: float = DEFAULT_REQUEST_DELAY_SECONDS,
    max_pages: int = MAX_INDEXED_LESSONS,
    fetch: FetchBytes = _download_bytes,
    delay: Delay = time.sleep,
) -> dict[str, Any]:
    """Fetch one TOC-bounded, deterministic private IMABI snapshot."""
    toc_url = _canonical_content_url(str(toc_url))
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 120:
        raise ImabiFetchError("timeout must be greater than zero and at most 120 seconds")
    if (
        not isinstance(request_delay, (int, float))
        or isinstance(request_delay, bool)
        or not 0 <= request_delay <= 10
    ):
        raise ImabiFetchError("request_delay must be between zero and 10 seconds")
    if not isinstance(max_pages, int) or isinstance(max_pages, bool) or not 1 <= max_pages <= MAX_INDEXED_LESSONS:
        raise ImabiFetchError(f"max_pages must be between 1 and {MAX_INDEXED_LESSONS}")
    cache_directory = Path(cache_directory)
    toc_content, toc_downloaded = _cached_or_fetch(
        cache_directory,
        "toc",
        toc_url,
        float(timeout),
        fetch,
        delay,
        float(request_delay),
    )
    try:
        items = _toc_items(toc_content, toc_url)
    except ImabiContentError as error:
        raise ImabiFetchError(str(error)) from error
    eligible = [item for item in items if item["status"] == "eligible"]
    if len(eligible) > max_pages:
        raise ImabiFetchError(
            f"IMABI table of contents has {len(eligible)} eligible lessons, above max_pages={max_pages}"
        )
    members: dict[str, bytes] = {"toc.html": toc_content}
    page_manifest: list[dict[str, Any]] = []
    downloaded_pages = 0
    aggregate_bytes = len(toc_content)
    failures: list[dict[str, str]] = []
    for item in eligible:
        url = item["canonical_url"]
        try:
            content, downloaded = _cached_or_fetch(
                cache_directory,
                "pages",
                url,
                float(timeout),
                fetch,
                delay,
                float(request_delay),
            )
        except Exception as error:
            failures.append(
                {"source_lesson_id": item["source_lesson_id"], "canonical_url": url, "reason": str(error)}
            )
            continue
        downloaded_pages += int(downloaded)
        aggregate_bytes += len(content)
        if aggregate_bytes > MAX_UNCOMPRESSED_SNAPSHOT_BYTES:
            raise ImabiFetchError(
                f"IMABI snapshot exceeds {MAX_UNCOMPRESSED_SNAPSHOT_BYTES} uncompressed bytes"
            )
        member = _snapshot_member(url)
        members[member] = content
        page_manifest.append(
            {
                "source_lesson_id": item["source_lesson_id"],
                "canonical_url": url,
                "path": member,
                "sha256": _sha256(content),
            }
        )
    outcomes = [
        {
            "source_lesson_id": item["source_lesson_id"],
            "canonical_url": item["canonical_url"],
            "status": item["status"],
            "reason": item["reason"],
        }
        for item in items
    ]
    base_report: dict[str, Any] = {
        "format": "ugd-imabi-fetch-report",
        "format_version": CONTENT_REPORT_FORMAT_VERSION,
        "source_id": SOURCE_ID,
        "toc_url": toc_url,
        "toc_sha256": _sha256(toc_content),
        "indexed_lesson_count": len(items),
        "eligible_lesson_count": len(eligible),
        "not_applicable_count": sum(item["status"] == "not-applicable" for item in items),
        "fetched_page_count": len(page_manifest),
        "downloaded_page_count": downloaded_pages,
        "reused_cached_page_count": len(page_manifest) - downloaded_pages,
        "toc_downloaded": toc_downloaded,
        "outcomes": outcomes,
        "index_anomalies": _index_anomalies(items),
        "failures": failures,
    }
    if failures:
        raise ImabiFetchError(
            f"failed to acquire {len(failures)} of {len(eligible)} eligible IMABI lessons",
            base_report,
        )
    manifest = {
        "format": SNAPSHOT_FORMAT,
        "format_version": SNAPSHOT_FORMAT_VERSION,
        "source_id": SOURCE_ID,
        "toc_url": toc_url,
        "toc_sha256": _sha256(toc_content),
        "indexed_lessons": items,
        "pages": page_manifest,
    }
    members["snapshot.json"] = canonical_json_bytes(manifest)
    snapshot_path = Path(snapshot_path)
    _write_snapshot(snapshot_path, members)
    snapshot_bytes = _read_regular_nofollow(snapshot_path, MAX_SNAPSHOT_BYTES, "IMABI snapshot")
    return base_report | {
        "snapshot_bytes": len(snapshot_bytes),
        "snapshot_uncompressed_bytes": aggregate_bytes,
        "snapshot_sha256": _sha256(snapshot_bytes),
    }


def _load_snapshot(path: Path) -> tuple[dict[str, Any], dict[str, bytes], str]:
    snapshot_bytes = _read_regular_nofollow(path, MAX_SNAPSHOT_BYTES, "IMABI snapshot")
    try:
        with zipfile.ZipFile(io.BytesIO(snapshot_bytes)) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise ImabiImportError("IMABI snapshot contains duplicate member names")
            if sum(info.file_size for info in infos) > MAX_UNCOMPRESSED_SNAPSHOT_BYTES:
                raise ImabiImportError("IMABI snapshot uncompressed size exceeds the configured limit")
            for info in infos:
                _safe_member_name(info.filename, "snapshot member")
                mode = info.external_attr >> 16
                if mode and not stat.S_ISREG(mode):
                    raise ImabiImportError(f"snapshot member is not a regular file: {info.filename}")
                if info.file_size > MAX_PAGE_BYTES:
                    raise ImabiImportError(
                        f"snapshot member exceeds {MAX_PAGE_BYTES} bytes: {info.filename}"
                    )
            if "snapshot.json" not in names or "toc.html" not in names:
                raise ImabiImportError("IMABI snapshot omits its manifest or table of contents")
            manifest = _required_object(_decode_json(archive.read("snapshot.json"), "snapshot"), "snapshot")
            if (
                manifest.get("format") != SNAPSHOT_FORMAT
                or type(manifest.get("format_version")) is not int
                or manifest.get("format_version") != SNAPSHOT_FORMAT_VERSION
                or manifest.get("source_id") != SOURCE_ID
            ):
                raise ImabiImportError("unsupported IMABI snapshot format, version, or source")
            toc_url = _canonical_content_url(manifest.get("toc_url"))
            toc_content = archive.read("toc.html")
            if _sha256(toc_content) != manifest.get("toc_sha256"):
                raise ImabiImportError("IMABI table-of-contents SHA-256 mismatch")
            parsed_items = _toc_items(toc_content, toc_url)
            if parsed_items != manifest.get("indexed_lessons"):
                raise ImabiImportError("IMABI snapshot index differs from its table of contents")
            pages = _required_list(manifest.get("pages"), "snapshot.pages")
            expected_eligible = {
                item["source_lesson_id"]: item
                for item in parsed_items
                if item["status"] == "eligible"
            }
            documents: dict[str, bytes] = {}
            expected_members = {"snapshot.json", "toc.html"}
            seen_ids: set[str] = set()
            for index, raw_page in enumerate(pages):
                page = _required_object(raw_page, f"snapshot.pages[{index}]")
                if set(page) != {"source_lesson_id", "canonical_url", "path", "sha256"}:
                    raise ImabiImportError(f"snapshot.pages[{index}] has unsupported fields")
                source_id = _required_string(page["source_lesson_id"], "page source_lesson_id")
                if source_id in seen_ids or source_id not in expected_eligible:
                    raise ImabiImportError("IMABI snapshot page identity is duplicate or not eligible")
                seen_ids.add(source_id)
                url = _canonical_content_url(page["canonical_url"])
                if url != expected_eligible[source_id]["canonical_url"]:
                    raise ImabiImportError("IMABI snapshot page URL differs from its index")
                member = _safe_member_name(page["path"], "page path")
                if member != _snapshot_member(url):
                    raise ImabiImportError("IMABI snapshot page path is not canonical")
                expected_members.add(member)
                if member not in names:
                    raise ImabiImportError(f"IMABI snapshot omits page member {member}")
                content = archive.read(member)
                if _sha256(content) != page["sha256"]:
                    raise ImabiImportError(f"IMABI snapshot page SHA-256 mismatch: {url}")
                documents[source_id] = content
            if seen_ids != set(expected_eligible):
                raise ImabiImportError("IMABI snapshot does not contain every eligible lesson identity")
            if set(names) != expected_members:
                raise ImabiImportError("IMABI snapshot contains missing or unexpected members")
    except zipfile.BadZipFile as error:
        raise ImabiImportError("invalid IMABI snapshot ZIP") from error
    manifest["indexed_lessons"] = parsed_items
    return manifest, documents, _sha256(snapshot_bytes)


def _safe_url(value: str, base_url: str) -> str:
    resolved = urljoin(base_url, value)
    parsed = urlsplit(resolved)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ImabiContentError(f"unsafe linked URL in IMABI content: {value!r}")
    if parsed.username or parsed.password:
        raise ImabiContentError("linked URL in IMABI content contains credentials")
    try:
        parsed.port
    except ValueError as error:
        raise ImabiContentError("linked URL in IMABI content has an invalid port") from error
    return resolved


def _text_fragment(value: str) -> str:
    collapsed = re.sub(r"\s+", " ", value.replace("\xa0", " "))
    if not collapsed.strip():
        return " " if collapsed else ""
    return collapsed


def _safe_children(node: _Node, base_url: str) -> list[Any]:
    values: list[Any] = []
    for child in node.children:
        if isinstance(child, str):
            text = _text_fragment(child)
            if text:
                if values and isinstance(values[-1], str):
                    values[-1] += text
                else:
                    values.append(text)
            continue
        safe = _safe_node(child, base_url)
        if safe is None:
            continue
        if isinstance(safe, list):
            values.extend(safe)
        else:
            values.append(safe)
    while values and isinstance(values[0], str) and not values[0].strip():
        values.pop(0)
    while values and isinstance(values[-1], str) and not values[-1].strip():
        values.pop()
    if values and isinstance(values[0], str):
        values[0] = values[0].lstrip()
    if values and isinstance(values[-1], str):
        values[-1] = values[-1].rstrip()
    return values


def _safe_node(node: _Node, base_url: str) -> Any:
    if node.tag in _BLOCKED_TAGS or _is_chrome(node):
        return None
    children = _safe_children(node, base_url)
    if node.tag not in _SAFE_TAGS:
        return children
    value: dict[str, Any] = {"tag": node.tag}
    attrs: dict[str, Any] = {}
    if node.tag == "a" and isinstance(node.attrs.get("href"), str):
        attrs["href"] = _safe_url(node.attrs["href"], base_url)
    elif node.tag == "img" and isinstance(node.attrs.get("src"), str):
        attrs["src"] = _safe_url(node.attrs["src"], base_url)
        if isinstance(node.attrs.get("alt"), str) and _clean_text(node.attrs["alt"]):
            attrs["alt"] = _clean_text(node.attrs["alt"])
    elif node.tag in {"td", "th"}:
        for name in ("colspan", "rowspan"):
            raw = node.attrs.get(name)
            if isinstance(raw, str) and raw.isdigit() and 1 <= int(raw) <= 100:
                attrs[name] = int(raw)
    if attrs:
        value["attrs"] = attrs
    if children:
        value["content"] = children
    return value


def _safe_fragment(children: list[Any], base_url: str) -> Any:
    wrapper = _Node("span", {}, list(children))
    values = _safe_children(wrapper, base_url)
    if len(values) == 1 and isinstance(values[0], str):
        return _clean_text(values[0])
    return values


def _content_units(entry: _Node) -> tuple[list[_Node], int]:
    units: list[_Node] = []
    excluded_chrome = 0

    def visit(node: _Node) -> None:
        nonlocal excluded_chrome
        if node.tag in _BLOCKED_TAGS or _is_chrome(node):
            excluded_chrome += 1
            return
        if node.tag in _CONTENT_UNIT_TAGS:
            if _node_text(node) or node.tag in {"figure", "table"}:
                units.append(node)
            return
        if node.tag in {"hr", "br"}:
            return
        child_nodes = [child for child in node.children if isinstance(child, _Node)]
        if child_nodes:
            for child in child_nodes:
                visit(child)
        elif _node_text(node):
            units.append(node)

    for child in entry.children:
        if isinstance(child, _Node):
            visit(child)
    return units, excluded_chrome


def _line_fragments(node: _Node) -> list[list[Any]]:
    lines: list[list[Any]] = [[]]
    for child in node.children:
        if isinstance(child, _Node) and child.tag == "br":
            if lines[-1]:
                lines.append([])
            continue
        lines[-1].append(child)
    return [line for line in lines if _clean_text(_fragment_visible_text(line))]


def _fragment_visible_text(children: list[Any]) -> str:
    return "".join(
        child if isinstance(child, str) else _node_text(child, include_rt=False)
        for child in children
    ).replace("\xa0", " ")


def _extract_examples(node: _Node, base_url: str) -> tuple[list[tuple[Any, Any, Any]], bool]:
    if node.tag != "p":
        return [], False
    lines = _line_fragments(node)
    texts = [_clean_text(_fragment_visible_text(line)) for line in lines]
    starts = [
        index
        for index, text in enumerate(texts)
        if _EXAMPLE_MARKER.match(text) and _JAPANESE.search(text)
    ]
    if not starts:
        return [], False
    examples: list[tuple[Any, Any, Any]] = []
    complete = starts[0] == 0
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        translation_index: int | None = None
        translation_text: str | None = None
        for index in range(start + 1, end):
            text = texts[index]
            if text.lower().startswith("translation:"):
                translation_index = index
                translation_text = text.split(":", 1)[1].strip()
                break
        if translation_index is None:
            for index in range(start + 1, end):
                text = texts[index]
                lower = text.lower()
                if _LATIN.search(text) and not lower.startswith(
                    ("gloss:", "grammar note:", "usage note:", "word note:", "culture note:")
                ):
                    translation_index = index
                    translation_text = text
                    break
        if translation_index is None or not translation_text:
            complete = False
            continue
        if any(index not in {start, translation_index} for index in range(start, end)):
            complete = False
        japanese = _safe_fragment(lines[start], base_url)
        if texts[translation_index].lower().startswith("translation:"):
            translation: Any = translation_text
        else:
            translation = _safe_fragment(lines[translation_index], base_url)
        raw = {
            "japanese": japanese,
            "translation": translation,
            "source_lines": texts[start:end],
        }
        examples.append((japanese, translation, raw))
    complete = complete and len(examples) == len(starts)
    return examples, complete


def _formation_kind(node: _Node, section_title: str) -> BlockKind:
    text = f"{section_title} {_node_text(node)}".lower()
    formation_markers = (
        "＋",
        " + ",
        "formation",
        "conjugation",
        "connects to",
        "attaches to",
        "活用",
        "接続",
    )
    if node.tag in {"table", "figure"} and any(marker in text for marker in formation_markers):
        return BlockKind.FORMATION
    if any(marker in section_title.lower() for marker in ("formation", "conjugation")):
        return BlockKind.FORMATION
    return BlockKind.MEANING


def _page_title(root: _Node, fallback: str) -> str:
    for node in _walk(root):
        if node.tag == "h1" and "entry-title" in _class_tokens(node):
            title = _node_text(node)
            if title:
                return title
    return fallback


def _record_suffix(item: Mapping[str, Any]) -> str:
    prefix = f"{item['material_kind']}:lesson:{item['lesson_number']}"
    marker = ":variant:"
    lesson_id = item["source_lesson_id"]
    return prefix + (lesson_id[lesson_id.index(marker) :] if marker in lesson_id else "")


def _provenance(
    revision_id: str,
    source_record_id: str,
    locator: str,
    raw: Any,
) -> Provenance:
    return Provenance(
        source_id=SOURCE_ID,
        revision_id=revision_id,
        source_record_id=source_record_id,
        locator=locator,
        content_sha256=_json_hash(raw),
    )


def _convert_lesson(
    item: Mapping[str, Any],
    content: bytes,
    revision_id: str,
) -> tuple[SourceRecord, list[SourceSense], dict[str, Any]]:
    url = item["canonical_url"]
    root = _parse_html(content, f"IMABI lesson {url}")
    entry = _entry_content(root, f"IMABI lesson {url}")
    units, excluded_chrome = _content_units(entry)
    if not units:
        raise ImabiContentError("lesson entry-content contains no substantive units")
    source_record_id = f"{SOURCE_ID}:{revision_id}:record:{_record_suffix(item)}"
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    global_block_count = 0
    global_example_count = 0
    formation_count = 0
    linked_images = 0
    preserved_content_units = 0
    example_source_units = 0
    fully_extracted_example_units = 0

    def new_section(title: str, anchor: str, heading: _Node | None, unit_index: int) -> dict[str, Any]:
        source_sense_id = f"{source_record_id}:sense:section:{len(sections) + 1}"
        section: dict[str, Any] = {
            "source_sense_id": source_sense_id,
            "title": title,
            "anchor": anchor,
            "blocks": [],
            "examples": [],
        }
        raw = {
            "section_title": title,
            "source_anchor": anchor,
            "heading_level": heading.tag if heading else None,
            "content": _safe_node(heading, url) if heading else None,
        }
        section["blocks"].append(
            ContentBlock(
                block_id=f"{source_sense_id}:block:section",
                kind=BlockKind.NOTE,
                language="mul",
                content=raw,
                order=0,
                provenance=_provenance(
                    revision_id,
                    source_record_id,
                    f"{url}#entry-content/{unit_index}/section",
                    raw,
                ),
            )
        )
        sections.append(section)
        return section

    for unit_index, unit in enumerate(units):
        linked_images += sum(1 for node in _walk(unit) if node.tag == "img")
        if unit.tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            title = _node_text(unit)
            native_id = unit.attrs.get("id")
            anchor = (
                f"#{native_id}"
                if isinstance(native_id, str)
                and native_id
                and not any(character.isspace() for character in native_id)
                else f"entry-content/{unit.tag}[{sum(1 for prior in units[: unit_index + 1] if prior.tag == unit.tag)}]"
            )
            current = new_section(title, anchor, unit, unit_index)
            global_block_count += 1
            preserved_content_units += 1
            continue
        if current is None:
            current = new_section("Lesson overview", "entry-content/overview", None, unit_index)
            global_block_count += 1
        extracted, fully_extracted = _extract_examples(unit, url)
        example_source_units += int(bool(extracted))
        for example_index, (japanese, translation, raw) in enumerate(extracted):
            source_sense_id = current["source_sense_id"]
            provenance = _provenance(
                revision_id,
                source_record_id,
                f"{url}#entry-content/{unit_index}/example/{example_index + 1}",
                raw,
            )
            current["examples"].append(
                ExamplePair(
                    example_id=f"{source_sense_id}:example:{unit_index + 1}-{example_index + 1}",
                    japanese=japanese,
                    translation=translation,
                    translation_language="en",
                    order=len(current["examples"]),
                    provenance=provenance,
                )
            )
            global_example_count += 1
        if fully_extracted:
            fully_extracted_example_units += 1
            preserved_content_units += 1
            continue
        safe = _safe_node(unit, url)
        if safe is None:
            raise ImabiContentError(f"content unit {unit_index} could not be represented safely")
        kind = _formation_kind(unit, current["title"])
        formation_count += int(kind is BlockKind.FORMATION)
        source_sense_id = current["source_sense_id"]
        current["blocks"].append(
            ContentBlock(
                block_id=f"{source_sense_id}:block:{unit_index + 1}",
                kind=kind,
                language="mul",
                content=safe,
                order=len(current["blocks"]),
                provenance=_provenance(
                    revision_id,
                    source_record_id,
                    f"{url}#entry-content/{unit_index}",
                    safe,
                ),
            )
        )
        global_block_count += 1
        preserved_content_units += 1
    if preserved_content_units != len(units):
        raise ImabiContentError(
            f"content-unit preservation failed: {preserved_content_units} of {len(units)} represented"
        )
    substantive_blocks = sum(
        1 for section in sections for block in section["blocks"] if block.kind is not BlockKind.NOTE
    )
    if substantive_blocks == 0 and global_example_count == 0:
        raise ImabiContentError("lesson has headings but no substantive explanation, formation, or examples")
    senses: list[SourceSense] = []
    labels = (item["toc_section"], f"IMABI:{item['material_kind']}")
    for section in sections:
        links = [SourceLink("IMABI lesson", url)]
        if section["anchor"].startswith("#"):
            links.append(SourceLink("IMABI source section", url + section["anchor"]))
        senses.append(
            SourceSense(
                source_sense_id=section["source_sense_id"],
                source_record_id=source_record_id,
                partition_status=PartitionStatus.SOURCE_EXPLICIT,
                blocks=tuple(section["blocks"]),
                examples=tuple(section["examples"]),
                level_labels=labels,
                register_labels=(),
                links=tuple(links),
            )
        )
    page_title = _page_title(root, item["title"])
    record = SourceRecord(
        source_record_id=source_record_id,
        raw_expression=item["title"],
        order=item["index"],
        field_hashes={
            "canonical-url": _json_hash(url),
            "material-kind": _json_hash(item["material_kind"]),
            "page-html": _sha256(content),
            "page-title": _json_hash(page_title),
            "toc-section": _json_hash(item["toc_section"]),
            "toc-title": _json_hash(item["title"]),
        },
    )
    return record, senses, {
        "source_lesson_id": item["source_lesson_id"],
        "source_record_id": source_record_id,
        "canonical_url": url,
        "toc_title": item["title"],
        "page_title": page_title,
        "material_kind": item["material_kind"],
        "section_count": len(senses),
        "content_block_count": global_block_count,
        "formation_block_count": formation_count,
        "example_count": global_example_count,
        "linked_image_count": linked_images,
        "excluded_chrome_unit_count": excluded_chrome,
        "source_content_unit_count": len(units),
        "preserved_content_unit_count": preserved_content_units,
        "example_source_unit_count": example_source_units,
        "fully_extracted_example_unit_count": fully_extracted_example_units,
    }


def convert_snapshot(
    snapshot_path: str | Path,
    revision_id: str,
) -> tuple[SourceBundle, dict[str, Any]]:
    """Convert every eligible lesson in one pinned private IMABI snapshot."""
    revision_id = _required_string(revision_id, "revision_id")
    manifest, documents, snapshot_sha256 = _load_snapshot(Path(snapshot_path))
    indexed = manifest["indexed_lessons"]
    eligible = [item for item in indexed if item["status"] == "eligible"]
    expected_source_record_ids = [
        f"{SOURCE_ID}:{revision_id}:record:{_record_suffix(item)}" for item in eligible
    ]
    records: list[SourceRecord] = []
    senses: list[SourceSense] = []
    imported: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    metrics = Counter()
    material_counts = Counter()
    for item in eligible:
        try:
            record, lesson_senses, lesson_metrics = _convert_lesson(
                item,
                documents[item["source_lesson_id"]],
                revision_id,
            )
        except (ImabiContentError, KeyError, TypeError, ValueError) as error:
            rejected.append(
                {
                    "source_lesson_id": item["source_lesson_id"],
                    "canonical_url": item["canonical_url"],
                    "reason": str(error),
                }
            )
            continue
        records.append(record)
        senses.extend(lesson_senses)
        imported.append(lesson_metrics)
        material_counts[item["material_kind"]] += 1
        for key in (
            "section_count",
            "content_block_count",
            "formation_block_count",
            "example_count",
            "linked_image_count",
            "excluded_chrome_unit_count",
            "source_content_unit_count",
            "preserved_content_unit_count",
            "example_source_unit_count",
            "fully_extracted_example_unit_count",
        ):
            metrics[key] += lesson_metrics[key]
    outcomes = []
    imported_ids = {item["source_lesson_id"] for item in imported}
    rejected_by_id = {item["source_lesson_id"]: item["reason"] for item in rejected}
    for item in indexed:
        lesson_id = item["source_lesson_id"]
        if item["status"] == "not-applicable":
            status = "not-applicable"
            reason = item["reason"]
        elif lesson_id in imported_ids:
            status = "imported"
            reason = None
        else:
            status = "skipped"
            reason = rejected_by_id.get(lesson_id, "eligible lesson was not converted")
        outcomes.append(
            {
                "source_lesson_id": lesson_id,
                "canonical_url": item["canonical_url"],
                "status": status,
                "reason": reason,
            }
        )
    report: dict[str, Any] = {
        "format": "ugd-imabi-import-report",
        "format_version": CONTENT_REPORT_FORMAT_VERSION,
        "source_id": SOURCE_ID,
        "revision_id": revision_id,
        "snapshot_sha256": snapshot_sha256,
        "content_status": "substantive-official-html",
        "permission_basis": "user-reported-project-permission",
        "publication_mode": PublicationMode.DENIED.value,
        "indexed_lesson_count": len(indexed),
        "eligible_lesson_count": len(eligible),
        "imported_lesson_count": len(records),
        "rejected_lesson_count": len(rejected),
        "not_applicable_count": sum(item["status"] == "not-applicable" for item in indexed),
        "expected_source_record_ids": expected_source_record_ids,
        "imported_source_record_ids": [record.source_record_id for record in records],
        "rejected_lessons": rejected,
        "outcomes": outcomes,
        "imported_lessons": imported,
        "material_counts": dict(sorted(material_counts.items())),
        "section_count": metrics["section_count"],
        "content_block_count": metrics["content_block_count"],
        "formation_block_count": metrics["formation_block_count"],
        "example_count": metrics["example_count"],
        "linked_image_count": metrics["linked_image_count"],
        "media_count": 0,
        "excluded_chrome_unit_count": metrics["excluded_chrome_unit_count"],
        "source_content_unit_count": metrics["source_content_unit_count"],
        "preserved_content_unit_count": metrics["preserved_content_unit_count"],
        "example_source_unit_count": metrics["example_source_unit_count"],
        "fully_extracted_example_unit_count": metrics["fully_extracted_example_unit_count"],
        "index_anomalies": _index_anomalies(indexed),
    }
    if rejected:
        raise ImabiImportError(
            f"refusing partial IMABI import: {len(rejected)} of {len(eligible)} eligible lessons rejected",
            report,
        )
    if set(expected_source_record_ids) != {record.source_record_id for record in records}:
        raise ImabiImportError("exact IMABI source-record identity coverage failed", report)
    source_revision = SourceRevision(
        source_id=SOURCE_ID,
        revision_id=revision_id,
        content_sha256=snapshot_sha256,
        languages=("ja", "en", "mul"),
        attribution=(
            "IMABI authors; preserve lesson URLs, source-defined section boundaries, and "
            "modern/classical labels."
        ),
        license=LicenseInfo(
            identifier=None,
            notice=(
                "Private project use is based on Bee's 2026-09-08 user-reported project permission; "
                "public redistribution terms were not independently verified."
            ),
            evidence_url=None,
        ),
        access_mode=AccessMode.PUBLIC_HTTP,
        import_mode=ImportMode.CONTENT,
        publication_mode=PublicationMode.DENIED,
        generated=False,
        provenance_note=(
            "Official unauthenticated IMABI lesson HTML enumerated by the pinned table of contents; "
            "site chrome is excluded and remote image links are retained without copying media bytes."
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch a bounded official IMABI snapshot or convert an exact local snapshot "
            "to the canonical private grammar model."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch", help="fetch the complete TOC-bounded private snapshot")
    fetch.add_argument("--toc-url", default=TABLE_OF_CONTENTS_URL)
    fetch.add_argument("--snapshot", type=Path, required=True)
    fetch.add_argument("--cache-dir", type=Path, required=True)
    fetch.add_argument("--report", type=Path, required=True)
    fetch.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    fetch.add_argument("--request-delay", type=float, default=DEFAULT_REQUEST_DELAY_SECONDS)
    fetch.add_argument("--max-pages", type=int, default=MAX_INDEXED_LESSONS)
    convert = commands.add_parser("convert", help="convert an explicit pinned IMABI snapshot")
    convert.add_argument("--snapshot", type=Path, required=True)
    convert.add_argument("--revision", required=True)
    convert.add_argument("--output", type=Path, required=True)
    convert.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "fetch":
            report = fetch_snapshot(
                args.toc_url,
                args.snapshot,
                args.cache_dir,
                timeout=args.timeout,
                request_delay=args.request_delay,
                max_pages=args.max_pages,
            )
            _write_json(args.report, report)
        else:
            bundle, report = convert_snapshot(args.snapshot, args.revision)
            _write_json(args.output, bundle.to_dict())
            _write_json(args.report, report)
    except (ImabiContentError, OSError, ValueError) as error:
        report = getattr(error, "report", None)
        if isinstance(report, dict) and report:
            _write_json(args.report, report)
        parser.exit(1, f"error: {error}\n")
    return 0


__all__ = [
    "ImabiContentError",
    "ImabiFetchError",
    "ImabiImportError",
    "ImabiLessonReference",
    "ImabiReferenceError",
    "audited_reference_index",
    "audited_reference_json",
    "convert_snapshot",
    "fetch_snapshot",
    "source_lesson_id",
]


if __name__ == "__main__":
    raise SystemExit(main())
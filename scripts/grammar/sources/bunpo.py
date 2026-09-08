#!/usr/bin/env python3
"""Import Bee's pinned Yomitan 文法 ZIP into the canonical grammar model."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
import sys
import tempfile
from types import MappingProxyType
from typing import Any, Mapping, Sequence
from urllib.parse import quote, urlsplit
import zipfile

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.grammar.model import (  # noqa: E402
    AccessMode,
    BlockKind,
    ContentBlock,
    ExamplePair,
    ImportMode,
    LicenseInfo,
    MediaRecord,
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


SOURCE_ID = "bee-bunpo"
FORMAT_VERSION = 1
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_MEMBER_BYTES = 32 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_MEMBERS = 1_000
AI_WARNING = (
    "AI-generated material from the source deck. Not independently verified; "
    "the original AI field labels are retained."
)
_OWN_WORK_NOTE = (
    "The original archive wording is retained without being upgraded into a licence. "
    "Bee later declared that the supplied work is their own; that declaration is "
    "recorded separately and does not grant rights to linked or embedded third-party material."
)
_TERM_BANK = re.compile(r"^term_bank_(\d+)\.json$")
_TAG_BANK = re.compile(r"^tag_bank_(\d+)\.json$")
_MEDIA = re.compile(r"^media/([^/]+)$")
_URL = re.compile(r"^https?://[^\s/]+", re.IGNORECASE)
_PATH_LEAK = re.compile(r"/Users/[^\s\"']+")
_SOURCE_EXAMPLE = re.compile(r"^例文([1-9][0-9]*)$")
_AI_EXAMPLE = re.compile(r"^AI例文([1-9][0-9]*)$")
_AI_TRANSLATION = re.compile(r"^AI英訳([1-9][0-9]*)$")
_ONE_HIRAGANA = re.compile(r"^[ぁ-ゖ]$")


class BunpoAdapterError(ValueError):
    """The source archive is unsafe, malformed, unpinned, or lossy."""


@dataclass(frozen=True, slots=True)
class BunpoAlias:
    """One exact lookup row retained separately from its source sense."""

    alias_id: str
    source_sense_id: str
    source_sequence: int
    position: int
    surface: str
    reading: str
    kind: str
    scan_mode: str
    evidence: str
    definition_tags: str
    rules: str
    score: int | float
    term_tags: str
    row_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "alias_id": self.alias_id,
            "source_sense_id": self.source_sense_id,
            "source_sequence": self.source_sequence,
            "position": self.position,
            "surface": self.surface,
            "reading": self.reading,
            "kind": self.kind,
            "scan_mode": self.scan_mode,
            "evidence": self.evidence,
            "definition_tags": self.definition_tags,
            "rules": self.rules,
            "score": self.score,
            "term_tags": self.term_tags,
            "row_sha256": self.row_sha256,
        }


@dataclass(frozen=True, slots=True)
class BunpoImport:
    """Canonical metadata plus private media bytes and exhaustive audit report."""

    source_path: Path
    bundle: SourceBundle
    aliases: tuple[BunpoAlias, ...]
    media_files: Mapping[str, bytes]
    report: Mapping[str, Any]

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.bundle.to_dict())

    def report_bytes(self) -> bytes:
        return canonical_json_bytes(self.report)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _stable_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BunpoAdapterError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _decode_json(content: bytes, context: str) -> Any:
    try:
        return json.loads(content, object_pairs_hook=_unique_object)
    except BunpoAdapterError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BunpoAdapterError(f"invalid {context} JSON: {error}") from error


def _read_archive(path: Path, expected_sha256: str) -> bytes:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise BunpoAdapterError("expected_sha256 must be a lowercase SHA-256 digest")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise BunpoAdapterError(f"cannot open source archive without following symlinks: {error}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise BunpoAdapterError("source archive must be a regular file")
        if metadata.st_size > MAX_ARCHIVE_BYTES:
            raise BunpoAdapterError("source archive exceeds 64 MiB limit")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            content = stream.read(MAX_ARCHIVE_BYTES + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(content) > MAX_ARCHIVE_BYTES:
        raise BunpoAdapterError("source archive exceeds 64 MiB limit")
    actual_sha256 = _sha256(content)
    if actual_sha256 != expected_sha256:
        raise BunpoAdapterError(
            f"source archive SHA-256 mismatch: {actual_sha256} != {expected_sha256}"
        )
    return content


def _safe_member_name(name: str) -> None:
    path = PurePosixPath(name)
    if (
        not name
        or name in {".", ".."}
        or path.is_absolute()
        or path.as_posix() != name
        or ".." in path.parts
        or "\\" in name
        or PureWindowsPath(name).drive
        or any(ord(character) < 32 for character in name)
    ):
        raise BunpoAdapterError(f"unsafe archive member: {name!r}")


def _validate_zip(archive: zipfile.ZipFile) -> tuple[zipfile.ZipInfo, ...]:
    infos = tuple(archive.infolist())
    names = [info.filename for info in infos]
    if len(infos) > MAX_MEMBERS:
        raise BunpoAdapterError("archive contains too many members")
    if len(names) != len(set(names)):
        raise BunpoAdapterError("duplicate archive member name")
    total = 0
    for info in infos:
        _safe_member_name(info.filename)
        if info.is_dir():
            raise BunpoAdapterError(f"directory archive member is not allowed: {info.filename}")
        unix_mode = info.external_attr >> 16
        file_type = stat.S_IFMT(unix_mode)
        if file_type not in {0, stat.S_IFREG}:
            raise BunpoAdapterError(f"non-regular archive member: {info.filename}")
        if info.flag_bits & 1:
            raise BunpoAdapterError(f"encrypted archive member: {info.filename}")
        if info.file_size > MAX_MEMBER_BYTES:
            raise BunpoAdapterError(f"archive member exceeds 32 MiB: {info.filename}")
        total += info.file_size
        if total > MAX_UNCOMPRESSED_BYTES:
            raise BunpoAdapterError("archive exceeds 64 MiB uncompressed limit")
    try:
        bad_member = archive.testzip()
    except (OSError, RuntimeError, zipfile.BadZipFile) as error:
        raise BunpoAdapterError(f"archive integrity check failed: {error}") from error
    if bad_member is not None:
        raise BunpoAdapterError(f"archive CRC failure in {bad_member}")
    return infos


def _read_member(archive: zipfile.ZipFile, name: str) -> bytes:
    try:
        return archive.read(name)
    except KeyError as error:
        raise BunpoAdapterError(f"required archive member is missing: {name}") from error


def _numbered_members(names: Sequence[str], pattern: re.Pattern[str], kind: str) -> list[str]:
    numbered = []
    for name in names:
        match = pattern.fullmatch(name)
        if match:
            numbered.append((int(match.group(1)), name))
    numbered.sort()
    numbers = [number for number, _ in numbered]
    if not numbers or numbers != list(range(1, len(numbers) + 1)):
        raise BunpoAdapterError(f"{kind} members must be contiguous starting at 1")
    return [name for _, name in numbered]


def _walk_nodes(value: Any):
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _walk_nodes(nested)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_nodes(item)


def _plain_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_plain_text(item) for item in value)
    if isinstance(value, dict):
        return _plain_text(value.get("content", ""))
    return ""


def _extract_root(glossary: Any) -> Any:
    if not isinstance(glossary, list) or len(glossary) != 1:
        raise BunpoAdapterError("each source sequence must have one structured-content glossary")
    item = glossary[0]
    if not isinstance(item, dict) or item.get("type") != "structured-content":
        raise BunpoAdapterError("source glossary is not structured content")
    if set(item) != {"type", "content"}:
        raise BunpoAdapterError("structured-content glossary has unsupported metadata")
    root = item.get("content")
    if not isinstance(root, dict) or root.get("tag") != "div":
        raise BunpoAdapterError("structured-content glossary root must be a div")
    sections = root.get("content")
    if not isinstance(sections, list):
        raise BunpoAdapterError("structured-content glossary sections must be a list")
    return root


_ALLOWED_TAGS = {
    "br",
    "ruby",
    "rt",
    "rp",
    "table",
    "thead",
    "tbody",
    "tfoot",
    "tr",
    "td",
    "th",
    "span",
    "div",
    "ol",
    "ul",
    "li",
    "details",
    "summary",
    "img",
    "a",
}
_ALLOWED_STYLES = {
    "fontWeight": {"normal", "bold"},
    "textDecorationLine": {"none", "underline"},
}


def _validate_structured_content(value: Any, context: str = "structured content") -> None:
    if isinstance(value, str):
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_structured_content(item, f"{context}[{index}]")
        return
    if not isinstance(value, dict):
        raise BunpoAdapterError(f"{context} contains a non-node value")
    tag = value.get("tag")
    if tag not in _ALLOWED_TAGS:
        raise BunpoAdapterError(f"unsupported structured-content tag: {tag!r}")
    allowed_keys = {"tag", "content", "style", "data", "lang"}
    if tag == "a":
        allowed_keys.add("href")
    elif tag == "img":
        allowed_keys = {"tag", "path", "alt"}
    elif tag in {"td", "th"}:
        allowed_keys.update({"colSpan", "rowSpan"})
    unknown = set(value) - allowed_keys
    if unknown:
        raise BunpoAdapterError(
            f"unsupported structured-content properties on {tag}: {sorted(unknown)}"
        )
    if tag not in {"br", "img"}:
        if "content" not in value:
            raise BunpoAdapterError(f"structured-content {tag} node requires content")
        _validate_structured_content(value["content"], f"{context}.{tag}.content")
    if "style" in value:
        style = value["style"]
        if not isinstance(style, dict) or set(style) - set(_ALLOWED_STYLES):
            raise BunpoAdapterError("unsupported structured-content style")
        for name, setting in style.items():
            if setting not in _ALLOWED_STYLES[name]:
                raise BunpoAdapterError(
                    f"unsupported structured-content style value: {name}={setting!r}"
                )
    if "data" in value:
        data = value["data"]
        if (
            tag != "div"
            or not isinstance(data, dict)
            or set(data) != {"field"}
            or not isinstance(data["field"], str)
            or not data["field"]
        ):
            raise BunpoAdapterError("unsupported structured-content data metadata")
    if "lang" in value and (
        not isinstance(value["lang"], str)
        or re.fullmatch(r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", value["lang"]) is None
    ):
        raise BunpoAdapterError("invalid structured-content language tag")
    if tag == "a":
        href = value.get("href")
        if not isinstance(href, str) or not _safe_source_link(href):
            raise BunpoAdapterError("unsafe structured-content link")
    if tag == "img":
        image_path = value.get("path")
        if not isinstance(image_path, str) or _MEDIA.fullmatch(image_path) is None:
            raise BunpoAdapterError("unsafe structured-content image path")
        if "alt" in value and not isinstance(value["alt"], str):
            raise BunpoAdapterError("structured-content image alt must be text")
    for name in ("colSpan", "rowSpan"):
        if name in value and (
            not isinstance(value[name], int)
            or isinstance(value[name], bool)
            or value[name] < 1
        ):
            raise BunpoAdapterError(f"structured-content {name} must be a positive integer")


def _section_payload(section: Any) -> tuple[str, str | None, Any]:
    if not isinstance(section, dict):
        raise BunpoAdapterError("source field section must be an object")
    if _plain_text(section) == AI_WARNING:
        return "ai_warning", None, section
    data = section.get("data")
    field_name = data.get("field") if isinstance(data, dict) else None
    if not isinstance(field_name, str) or not field_name:
        raise BunpoAdapterError("unrecognised source section would be dropped")
    children = section.get("content")
    if (
        not isinstance(children, list)
        or len(children) != 2
        or not isinstance(children[1], dict)
        or "content" not in children[1]
    ):
        raise BunpoAdapterError(f"source field {field_name!r} has malformed content")
    return "field", field_name, children[1]["content"]


def _collect_urls(value: Any) -> list[str]:
    return sorted(
        {
            node["href"]
            for node in _walk_nodes(value)
            if node.get("tag") == "a"
            and isinstance(node.get("href"), str)
            and _URL.match(node["href"])
        }
    )


def _safe_source_link(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            return False
        parsed.port
    except ValueError:
        return False
    return True


def _collect_image_paths(value: Any) -> list[str]:
    return sorted(
        {
            node["path"]
            for node in _walk_nodes(value)
            if node.get("tag") == "img" and isinstance(node.get("path"), str)
        }
    )


def _count_ruby_nodes(value: Any) -> int:
    return sum(1 for node in _walk_nodes(value) if node.get("tag") in {"ruby", "rt"})


def _content_type(path: str, content: bytes) -> str:
    suffix = Path(path).suffix.lower()
    if suffix in {".jpg", ".jpeg"} and content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if suffix == ".png" and content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if suffix == ".gif" and content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if (
        suffix == ".webp"
        and content.startswith(b"RIFF")
        and len(content) >= 12
        and content[8:12] == b"WEBP"
    ):
        return "image/webp"
    raise BunpoAdapterError(f"media extension/signature mismatch: {path}")


def _canonical_media_path(source_path: str, content: bytes) -> str:
    suffix = Path(source_path).suffix.lower()
    if suffix == ".jpeg":
        suffix = ".jpg"
    return f"media/{_sha256(content)}{suffix}"


def _transform_media_paths(value: Any, path_map: Mapping[str, str]) -> Any:
    if isinstance(value, list):
        return [_transform_media_paths(item, path_map) for item in value]
    if isinstance(value, dict):
        transformed = {
            key: _transform_media_paths(item, path_map) for key, item in value.items()
        }
        if value.get("tag") == "img":
            source_path = value.get("path")
            if not isinstance(source_path, str) or source_path not in path_map:
                raise BunpoAdapterError(
                    f"structured-content image is missing from the archive: {source_path!r}"
                )
            transformed["path"] = path_map[source_path]
        return transformed
    return value


def _block_kind(field_name: str) -> BlockKind:
    if field_name in {"意味", "AI意味"}:
        return BlockKind.MEANING
    if field_name in {"接続", "AI構造"}:
        return BlockKind.FORMATION
    if _SOURCE_EXAMPLE.fullmatch(field_name) or _AI_EXAMPLE.fullmatch(field_name) or _AI_TRANSLATION.fullmatch(field_name):
        return BlockKind.EXAMPLE_NOTE
    if field_name in {"AI丁寧度"}:
        return BlockKind.RESTRICTION
    if field_name in {"JLPTレベル", "備考", "AIニュアンス", "AI類似文法"}:
        return BlockKind.NOTE
    return BlockKind.OTHER


def _field_language(field_name: str) -> str:
    return "en" if _AI_TRANSLATION.fullmatch(field_name) else "ja"


def _alias_scan_mode(surface: str, position: int) -> str:
    marker_free = surface.lstrip("〜～~")
    if not marker_free:
        raise BunpoAdapterError("lookup row contains only attachment markers")
    if position > 1 and _ONE_HIRAGANA.fullmatch(marker_free):
        return "quarantined"
    if any(marker in marker_free for marker in "〜～~"):
        return "manual-search-only"
    return "automatic"


def _validate_row(row: Any, bank_name: str, row_index: int) -> list[Any]:
    if not isinstance(row, list) or len(row) != 8:
        raise BunpoAdapterError(f"invalid term row in {bank_name}[{row_index}]")
    term, reading, definition_tags, rules, score, glossary, sequence, term_tags = row
    if not isinstance(term, str) or not term or term != term.strip():
        raise BunpoAdapterError(f"term row in {bank_name}[{row_index}] has an invalid term")
    for name, value in (
        ("reading", reading),
        ("definition tags", definition_tags),
        ("rules", rules),
        ("term tags", term_tags),
    ):
        if not isinstance(value, str):
            raise BunpoAdapterError(f"term row in {bank_name}[{row_index}] has invalid {name}")
    if (
        not isinstance(score, (int, float))
        or isinstance(score, bool)
        or (isinstance(score, float) and not math.isfinite(score))
    ):
        raise BunpoAdapterError(f"term row in {bank_name}[{row_index}] has invalid score")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence <= 0:
        raise BunpoAdapterError(f"term row in {bank_name}[{row_index}] has invalid sequence")
    _extract_root(glossary)
    return row


def _record_id(revision_id: str, sequence: int) -> str:
    return f"{SOURCE_ID}:{revision_id}:record:sequence:{sequence}"


def _sense_id(revision_id: str, sequence: int) -> str:
    return f"{_record_id(revision_id, sequence)}:sense:1"


def _provenance(
    revision_id: str,
    sequence: int,
    locator: str,
    content_sha256: str,
) -> Provenance:
    return Provenance(
        source_id=SOURCE_ID,
        revision_id=revision_id,
        source_record_id=_record_id(revision_id, sequence),
        locator=locator,
        content_sha256=content_sha256,
    )


def _load_manifest(value: str | Path | Mapping[str, Any], context: str) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    path = Path(value)
    try:
        content = path.read_bytes()
    except OSError as error:
        raise BunpoAdapterError(f"cannot read {context}: {error}") from error
    decoded = _decode_json(content, context)
    if not isinstance(decoded, dict):
        raise BunpoAdapterError(f"{context} must be an object")
    return decoded


def _require_equal(actual: Any, expected: Any, context: str) -> None:
    if actual != expected:
        raise BunpoAdapterError(f"UGD-01 manifest mismatch: {context}")


def _verify_manifests(
    archive_report: Mapping[str, Any],
    coverage: Mapping[str, Any],
    baseline_manifest: str | Path | Mapping[str, Any],
    coverage_manifest: str | Path | Mapping[str, Any],
) -> dict[str, Any]:
    baseline = _load_manifest(baseline_manifest, "UGD-01 baseline manifest")
    expected_coverage = _load_manifest(coverage_manifest, "UGD-01 coverage manifest")
    baseline_archive = baseline.get("archive")
    declared = baseline.get("declared")
    if not isinstance(baseline_archive, Mapping) or not isinstance(declared, Mapping):
        raise BunpoAdapterError("UGD-01 baseline manifest has an invalid shape")
    _require_equal(archive_report["sha256"], baseline_archive.get("sha256"), "archive sha256")
    _require_equal(archive_report["member_names"], baseline_archive.get("member_names"), "member names")
    for key in ("index", "source", "rights_text", "member_hashes", "media_inventory"):
        if key in declared:
            _require_equal(archive_report[key], declared[key], key)
    for key in ("term_bank_manifests", "tag_bank_manifests"):
        if key in declared:
            _require_equal(archive_report[key], declared[key], key)
    for key in (
        "archive_sha256",
        "source_notes",
        "lookup_entries",
        "alias_rows",
        "sequence_summaries",
        "field_records",
        "media_inventory",
        "aggregate",
    ):
        _require_equal(coverage[key], expected_coverage.get(key), key)
    return {
        "exact_match": True,
        "archive_sha256": archive_report["sha256"],
        "source_record_ids": [
            f"{SOURCE_ID}:{archive_report['revision_id']}:record:sequence:{item['sequence']}"
            for item in coverage["sequence_summaries"]
        ],
        "lookup_rows": coverage["lookup_entries"],
        "field_section_hashes": [
            item["section_sha256"] for item in coverage["field_records"]
        ],
        "field_content_hashes": [
            item["content_sha256"] for item in coverage["field_records"]
        ],
        "media_hashes": [item["sha256"] for item in coverage["media_inventory"]],
    }


def adapt_archive(
    source: str | Path,
    *,
    expected_sha256: str,
    expected_revision: str | None = None,
    baseline_manifest: str | Path | Mapping[str, Any] | None = None,
    coverage_manifest: str | Path | Mapping[str, Any] | None = None,
) -> BunpoImport:
    """Validate and losslessly adapt one exact Bee 文法 Yomitan archive."""
    if (baseline_manifest is None) != (coverage_manifest is None):
        raise BunpoAdapterError("baseline_manifest and coverage_manifest must be supplied together")
    source_bytes = _read_archive(Path(source), expected_sha256)
    try:
        archive = zipfile.ZipFile(io.BytesIO(source_bytes))
    except zipfile.BadZipFile as error:
        raise BunpoAdapterError(f"source is not a ZIP archive: {error}") from error

    with archive:
        infos = _validate_zip(archive)
        names = [info.filename for info in infos]
        allowed_metadata = {"index.json", "SOURCE.json", "RIGHTS.txt"}
        term_banks = _numbered_members(names, _TERM_BANK, "term bank")
        tag_banks = _numbered_members(names, _TAG_BANK, "tag bank")
        media_names = sorted(name for name in names if _MEDIA.fullmatch(name))
        recognized = allowed_metadata | set(term_banks) | set(tag_banks) | set(media_names)
        unknown = set(names) - recognized
        if unknown:
            raise BunpoAdapterError(f"unsupported archive members: {sorted(unknown)}")
        missing = allowed_metadata - set(names)
        if missing:
            raise BunpoAdapterError(f"required archive members are missing: {sorted(missing)}")

        index = _decode_json(_read_member(archive, "index.json"), "index.json")
        declared_source = _decode_json(_read_member(archive, "SOURCE.json"), "SOURCE.json")
        try:
            rights_text = _read_member(archive, "RIGHTS.txt").decode("utf-8")
        except UnicodeDecodeError as error:
            raise BunpoAdapterError("RIGHTS.txt is not UTF-8") from error
        if not isinstance(index, dict) or not isinstance(declared_source, dict):
            raise BunpoAdapterError("index.json and SOURCE.json must be objects")
        revision_id = index.get("revision")
        if not isinstance(revision_id, str) or not revision_id:
            raise BunpoAdapterError("index.json requires an immutable revision")
        if expected_revision is not None and revision_id != expected_revision:
            raise BunpoAdapterError(
                f"source revision mismatch: {revision_id!r} != {expected_revision!r}"
            )
        if index.get("format") != 3 or index.get("sequenced") is not True:
            raise BunpoAdapterError("Bee 文法 input must be a sequenced Yomitan v3 archive")
        attribution = index.get("attribution")
        if not isinstance(attribution, str) or not attribution.strip():
            raise BunpoAdapterError("index.json requires source attribution")
        if not rights_text.strip():
            raise BunpoAdapterError("RIGHTS.txt must not be empty")

        member_hashes = {
            name: _sha256(_read_member(archive, name)) for name in names
        }
        term_bank_manifests = []
        rows_by_sequence: dict[int, list[tuple[int, list[Any]]]] = {}
        global_position = 0
        for bank_name in term_banks:
            content = _read_member(archive, bank_name)
            rows = _decode_json(content, bank_name)
            if not isinstance(rows, list):
                raise BunpoAdapterError(f"{bank_name} must be a JSON array")
            term_bank_manifests.append(
                {"file": bank_name, "sha256": _sha256(content), "rows": len(rows)}
            )
            for row_index, raw_row in enumerate(rows):
                row = _validate_row(raw_row, bank_name, row_index)
                global_position += 1
                rows_by_sequence.setdefault(row[6], []).append((global_position, row))

        tag_bank_manifests = []
        for bank_name in tag_banks:
            content = _read_member(archive, bank_name)
            rows = _decode_json(content, bank_name)
            if not isinstance(rows, list):
                raise BunpoAdapterError(f"{bank_name} must be a JSON array")
            tag_bank_manifests.append(
                {"file": bank_name, "sha256": _sha256(content), "rows": len(rows)}
            )

        if not rows_by_sequence:
            raise BunpoAdapterError("archive has no source sequences")
        for sequence, positioned_rows in rows_by_sequence.items():
            seen_aliases: set[tuple[str, str]] = set()
            representative = positioned_rows[0][1]
            representative_payload = _stable_json(
                representative[1:6] + representative[7:]
            )
            for _, row in positioned_rows:
                alias_key = (row[0], row[1])
                if alias_key in seen_aliases:
                    raise BunpoAdapterError(f"duplicate alias in sequence {sequence}: {row[0]!r}")
                seen_aliases.add(alias_key)
                if _stable_json(row[1:6] + row[7:]) != representative_payload:
                    raise BunpoAdapterError(
                        f"lookup rows for sequence {sequence} disagree on semantic content"
                    )

        media_inventory = []
        source_media: dict[str, bytes] = {}
        media_types: dict[str, str] = {}
        media_path_map: dict[str, str] = {}
        for name in media_names:
            content = _read_member(archive, name)
            media_type = _content_type(name, content)
            canonical_path = _canonical_media_path(name, content)
            existing = source_media.get(canonical_path)
            if existing is not None and existing != content:
                raise BunpoAdapterError(f"hashed media collision at {canonical_path}")
            source_media[canonical_path] = content
            media_types[name] = media_type
            media_path_map[name] = canonical_path
            info = archive.getinfo(name)
            media_inventory.append(
                {
                    "path": name,
                    "sha256": _sha256(content),
                    "size": len(content),
                    "crc32": f"{info.CRC:08x}",
                    "content_type": media_type,
                }
            )

    records: list[SourceRecord] = []
    senses: list[SourceSense] = []
    aliases: list[BunpoAlias] = []
    sequence_summaries = []
    field_records = []
    media_references: dict[str, list[str]] = {name: [] for name in media_names}
    field_counts: dict[str, int] = {}
    all_source_links: set[str] = set()
    all_media_paths: set[str] = set()
    furigana_sequences: set[int] = set()
    ai_warning_count = 0
    field_section_count = 0

    for sequence in sorted(rows_by_sequence):
        positioned_rows = rows_by_sequence[sequence]
        representative = positioned_rows[0][1]
        root = _extract_root(representative[5])
        _validate_structured_content(root)
        sections = root["content"]
        record_id = _record_id(revision_id, sequence)
        sense_id = _sense_id(revision_id, sequence)
        field_hashes: dict[str, str] = {}
        blocks: list[ContentBlock] = []
        sequence_aliases: list[BunpoAlias] = []
        fields: dict[str, tuple[Any, Any, int, str]] = {}
        sequence_links: set[str] = set()
        sequence_images: set[str] = set()
        sequence_field_names: list[str] = []
        sequence_field_hashes: list[str] = []
        sequence_furigana_fields = 0
        sequence_ai_warnings = 0

        for position, (_, row) in enumerate(positioned_rows, start=1):
            row_hash = _sha256(_stable_json(row))
            field_hashes[f"lookup-row:{position}"] = row_hash
            alias = BunpoAlias(
                alias_id=f"{sense_id}:alias:{position}",
                source_sense_id=sense_id,
                source_sequence=sequence,
                position=position,
                surface=row[0],
                reading=row[1],
                kind="source-expression" if position == 1 else "source-alternative",
                scan_mode=_alias_scan_mode(row[0], position),
                evidence=f"pinned Yomitan lookup row SHA-256 {row_hash}",
                definition_tags=row[2],
                rules=row[3],
                score=row[4],
                term_tags=row[7],
                row_sha256=row_hash,
            )
            aliases.append(alias)
            sequence_aliases.append(alias)

        alias_rows = [alias.to_dict() for alias in sequence_aliases]
        alias_rows_hash = _sha256(_stable_json(alias_rows))
        field_hashes["lookup-rows"] = alias_rows_hash
        blocks.append(
            ContentBlock(
                block_id=f"{sense_id}:block:lookup-aliases",
                kind=BlockKind.NOTE,
                language="ja",
                content={
                    "role": "source-lookup-aliases",
                    "rows": alias_rows,
                    "source_content_sha256": alias_rows_hash,
                },
                order=0,
                provenance=_provenance(
                    revision_id,
                    sequence,
                    f"term-sequence:{sequence}:lookup-rows",
                    alias_rows_hash,
                ),
                generated=False,
            )
        )

        for section_index, section in enumerate(sections, start=1):
            kind, field_name, payload = _section_payload(section)
            section_hash = _sha256(_stable_json(section))
            content_hash = _sha256(_stable_json(payload))
            text_hash = _sha256(_plain_text(payload).encode("utf-8"))
            record: dict[str, Any] = {
                "sequence": sequence,
                "section_index": section_index,
                "kind": kind,
                "section_sha256": section_hash,
                "text_sha256": text_hash,
                "content_sha256": content_hash,
            }
            if kind == "ai_warning":
                ai_warning_count += 1
                sequence_ai_warnings += 1
                field_hashes[f"ai-warning:{section_index}"] = content_hash
                blocks.append(
                    ContentBlock(
                        block_id=f"{sense_id}:block:section-{section_index}",
                        kind=BlockKind.NOTE,
                        language="en",
                        content={
                            "role": "ai-warning",
                            "source_structured_content": section,
                            "source_content_sha256": content_hash,
                        },
                        order=section_index,
                        provenance=_provenance(
                            revision_id,
                            sequence,
                            f"term-sequence:{sequence}:section:{section_index}:ai-warning",
                            content_hash,
                        ),
                        generated=False,
                    )
                )
                field_records.append(record)
                continue

            assert field_name is not None
            if field_name in fields:
                raise BunpoAdapterError(
                    f"sequence {sequence} contains duplicate source field {field_name!r}"
                )
            transformed_payload = _transform_media_paths(payload, media_path_map)
            fields[field_name] = (
                payload,
                transformed_payload,
                section_index,
                content_hash,
            )
            field_hashes[field_name] = content_hash
            sequence_field_names.append(field_name)
            sequence_field_hashes.append(content_hash)
            field_counts[field_name] = field_counts.get(field_name, 0) + 1
            field_section_count += 1
            urls = _collect_urls(payload)
            image_paths = _collect_image_paths(payload)
            ruby_nodes = _count_ruby_nodes(payload)
            if ruby_nodes:
                sequence_furigana_fields += 1
                furigana_sequences.add(sequence)
            sequence_links.update(urls)
            sequence_images.update(image_paths)
            all_source_links.update(urls)
            all_media_paths.update(image_paths)
            for image_path in image_paths:
                if image_path not in media_references:
                    raise BunpoAdapterError(
                        f"structured-content image is missing from the archive: {image_path!r}"
                    )
                media_references[image_path].append(record_id)
            record.update(
                {
                    "field": field_name,
                    "has_ruby": ruby_nodes > 0,
                    "ruby_node_count": ruby_nodes,
                    "source_links": urls,
                    "image_paths": image_paths,
                }
            )
            field_records.append(record)
            blocks.append(
                ContentBlock(
                    block_id=f"{sense_id}:block:section-{section_index}",
                    kind=_block_kind(field_name),
                    language=_field_language(field_name),
                    content={
                        "field": field_name,
                        "source_structured_content": transformed_payload,
                        "source_content_sha256": content_hash,
                    },
                    order=section_index,
                    provenance=_provenance(
                        revision_id,
                        sequence,
                        (
                            f"term-sequence:{sequence}:section:{section_index}:field:"
                            f"{quote(field_name, safe='')}"
                        ),
                        content_hash,
                    ),
                    generated=field_name.startswith("AI"),
                )
            )

        generated_fields = [name for name in fields if name.startswith("AI")]
        if generated_fields and sequence_ai_warnings != 1:
            raise BunpoAdapterError(
                f"sequence {sequence} must preserve exactly one AI warning"
            )
        if not generated_fields and sequence_ai_warnings:
            raise BunpoAdapterError(
                f"sequence {sequence} has an AI warning without generated fields"
            )

        examples: list[ExamplePair] = []
        for field_name, (_, transformed, section_index, content_hash) in fields.items():
            source_match = _SOURCE_EXAMPLE.fullmatch(field_name)
            ai_match = _AI_EXAMPLE.fullmatch(field_name)
            if source_match:
                example_number = source_match.group(1)
                examples.append(
                    ExamplePair(
                        example_id=f"{sense_id}:example:source-{example_number}",
                        japanese=transformed,
                        translation=None,
                        translation_language=None,
                        order=section_index,
                        provenance=_provenance(
                            revision_id,
                            sequence,
                            f"term-sequence:{sequence}:field:{quote(field_name, safe='')}",
                            content_hash,
                        ),
                        generated=False,
                    )
                )
            elif ai_match:
                example_number = ai_match.group(1)
                translation_name = f"AI英訳{example_number}"
                translation = fields.get(translation_name)
                examples.append(
                    ExamplePair(
                        example_id=f"{sense_id}:example:ai-{example_number}",
                        japanese=transformed,
                        translation=None if translation is None else translation[1],
                        translation_language=None if translation is None else "en",
                        order=section_index,
                        provenance=_provenance(
                            revision_id,
                            sequence,
                            f"term-sequence:{sequence}:field:{quote(field_name, safe='')}",
                            content_hash,
                        ),
                        generated=True,
                    )
                )

        tags = representative[2].split()
        level_labels = tuple(
            dict.fromkeys(tag for tag in tags if re.fullmatch(r"N[1-5]", tag) or tag == "JLPTに出ない")
        )
        safe_links = tuple(
            SourceLink("Source link", url)
            for url in sorted(sequence_links)
            if _safe_source_link(url)
        )
        records.append(
            SourceRecord(
                source_record_id=record_id,
                raw_expression=representative[0],
                order=sequence,
                field_hashes=field_hashes,
            )
        )
        senses.append(
            SourceSense(
                source_sense_id=sense_id,
                source_record_id=record_id,
                partition_status=PartitionStatus.SOURCE_EXPLICIT,
                concept_ref=None,
                blocks=tuple(blocks),
                examples=tuple(examples),
                level_labels=level_labels,
                register_labels=(),
                links=safe_links,
            )
        )
        lookup_rows = [
            {
                "position": position,
                "term": row[0],
                "term_sha256": _sha256(row[0].encode("utf-8")),
                "role": "source_headword" if position == 1 else "alias",
            }
            for position, (_, row) in enumerate(positioned_rows, start=1)
        ]
        sequence_summaries.append(
            {
                "sequence": sequence,
                "source_headword": representative[0],
                "source_headword_sha256": _sha256(representative[0].encode("utf-8")),
                "lookup_row_count": len(positioned_rows),
                "alias_row_count": len(positioned_rows) - 1,
                "lookup_rows": lookup_rows,
                "semantic_hash": _sha256(_stable_json(root)),
                "field_section_count": len(sequence_field_names),
                "ai_warning_sections": sequence_ai_warnings,
                "furigana_field_sections": sequence_furigana_fields,
                "source_links": sorted(sequence_links),
                "image_paths": sorted(sequence_images),
                "field_names": sequence_field_names,
                "field_record_hashes": sequence_field_hashes,
            }
        )

    for source_path, references in media_references.items():
        if not references:
            raise BunpoAdapterError(f"unreferenced image member cannot be attributed: {source_path}")

    media_records: list[MediaRecord] = []
    seen_media_ids: set[str] = set()
    for source_path in media_names:
        canonical_path = media_path_map[source_path]
        content = source_media[canonical_path]
        digest = _sha256(content)
        media_id = f"{SOURCE_ID}:{revision_id}:media:{digest}"
        if media_id in seen_media_ids:
            continue
        seen_media_ids.add(media_id)
        owner_record = media_references[source_path][0]
        owner_sequence = int(owner_record.rsplit(":", 1)[1])
        media_records.append(
            MediaRecord(
                media_id=media_id,
                source_path=canonical_path,
                content_sha256=digest,
                media_type=media_types[source_path],
                byte_count=len(content),
                provenance=_provenance(
                    revision_id,
                    owner_sequence,
                    f"archive-member:{source_path}",
                    digest,
                ),
            )
        )

    lookup_count = sum(len(rows) for rows in rows_by_sequence.values())
    source_note_count = len(rows_by_sequence)
    alias_count = lookup_count - source_note_count
    declared_counts = {
        "source_notes": source_note_count,
        "lookup_entries": lookup_count,
        "alias_rows": alias_count,
        "nonempty_fields": field_section_count,
    }
    for name, observed in declared_counts.items():
        declared = declared_source.get(name)
        if declared != observed:
            raise BunpoAdapterError(
                f"SOURCE.json {name} mismatch: declared {declared!r}, observed {observed}"
            )

    path_leakage = bool(
        _PATH_LEAK.search(json.dumps(index, ensure_ascii=False))
        or _PATH_LEAK.search(json.dumps(declared_source, ensure_ascii=False))
        or _PATH_LEAK.search(rights_text)
    )
    coverage = {
        "archive_sha256": expected_sha256,
        "source_notes": source_note_count,
        "lookup_entries": lookup_count,
        "alias_rows": alias_count,
        "sequence_summaries": sequence_summaries,
        "field_records": field_records,
        "media_inventory": media_inventory,
        "aggregate": {
            "field_counts": dict(sorted(field_counts.items())),
            "field_section_count": field_section_count,
            "ai_warning_section_count": ai_warning_count,
            "furigana_sequence_count": len(furigana_sequences),
            "unique_source_links": sorted(all_source_links),
            "unique_source_link_count": len(all_source_links),
            "unique_media_paths": sorted(all_media_paths),
            "unique_media_path_count": len(all_media_paths),
            "path_leakage_detected": path_leakage,
        },
    }
    archive_report = {
        "sha256": expected_sha256,
        "revision_id": revision_id,
        "member_names": names,
        "member_hashes": member_hashes,
        "index": index,
        "source": declared_source,
        "rights_text": rights_text,
        "term_bank_manifests": term_bank_manifests,
        "tag_bank_manifests": tag_bank_manifests,
        "media_inventory": media_inventory,
    }
    verification: dict[str, Any] = {"exact_match": False, "reason": "not supplied"}
    if baseline_manifest is not None and coverage_manifest is not None:
        verification = _verify_manifests(
            archive_report,
            coverage,
            baseline_manifest,
            coverage_manifest,
        )

    revision = SourceRevision(
        source_id=SOURCE_ID,
        revision_id=revision_id,
        content_sha256=expected_sha256,
        languages=("ja", "en"),
        attribution=attribution,
        license=LicenseInfo(
            identifier=None,
            notice=" ".join(line.strip() for line in rights_text.splitlines() if line.strip()),
            evidence_url=None,
        ),
        access_mode=AccessMode.LOCAL_FILE,
        import_mode=ImportMode.CONTENT,
        publication_mode=PublicationMode.DENIED,
        generated=False,
        provenance_note=_OWN_WORK_NOTE,
    )
    bundle = SourceBundle(
        source_revision=revision,
        records=tuple(records),
        senses=tuple(senses),
        media=tuple(media_records),
    )
    bundle.validate()

    report: dict[str, Any] = {
        "format": "ugd-bunpo-adapter-report",
        "format_version": FORMAT_VERSION,
        "source_id": SOURCE_ID,
        "revision_id": revision_id,
        "source_content_sha256": expected_sha256,
        "counts": {
            "source_notes": source_note_count,
            "lookup_rows": lookup_count,
            "alias_rows": alias_count,
            "field_sections": field_section_count,
            "blocks": sum(len(sense.blocks) for sense in senses),
            "examples": sum(len(sense.examples) for sense in senses),
            "ai_warning_sections": ai_warning_count,
            "furigana_sequences": len(furigana_sequences),
            "source_links": len(all_source_links),
            "source_media": len(media_names),
            "media": len(media_records),
        },
        "source_record_ids": [record.source_record_id for record in records],
        "source_sense_ids": [sense.source_sense_id for sense in senses],
        "lookup_row_ids": [alias.alias_id for alias in aliases],
        "aliases": [alias.to_dict() for alias in aliases],
        "media_path_map": dict(sorted(media_path_map.items())),
        "media_references": {
            source_path: sorted(set(references))
            for source_path, references in sorted(media_references.items())
        },
        "transformations": [
            {
                "kind": "media-path-sha256-rename",
                "reversible": True,
                "mapping": dict(sorted(media_path_map.items())),
                "source_field_hashes_preserved": True,
            },
            {
                "kind": "field-section-extraction",
                "reversible": True,
                "source_section_and_content_hashes_preserved": True,
                "field_names_and_order_preserved": True,
            },
            {
                "kind": "rights-newline-normalization",
                "reversible": True,
                "model_field": "source_revision.license.notice",
                "exact_source_text": "report.archive.rights_text",
                "exact_source_member_sha256": member_hashes["RIGHTS.txt"],
            },
        ],
        "archive": archive_report,
        "coverage": coverage,
        "manifest_verification": verification,
        "limitations": [
            "Canonical model v1 has no Alias field, so exact lookup mappings are retained in source-lookup-aliases content blocks and the audit report.",
            "Hashed media bytes are returned separately and must remain in ignored private build storage until packaging.",
        ],
    }
    return BunpoImport(
        source_path=Path(source).resolve(),
        bundle=bundle,
        aliases=tuple(aliases),
        media_files=MappingProxyType(dict(sorted(source_media.items()))),
        report=MappingProxyType(report),
    )


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


def write_import(
    result: BunpoImport,
    canonical_path: str | Path,
    report_path: str | Path,
    media_directory: str | Path,
) -> None:
    """Write private adapter outputs without embedding host paths."""
    canonical_path = Path(canonical_path).resolve()
    report_path = Path(report_path).resolve()
    media_directory = Path(media_directory).resolve()
    if canonical_path == report_path:
        raise BunpoAdapterError("canonical output and report paths must differ")
    if result.source_path in {canonical_path, report_path}:
        raise BunpoAdapterError("adapter output must not overwrite the source archive")
    media_destinations = {
        media_directory.joinpath(*PurePosixPath(relative_path).parts).resolve()
        for relative_path in result.media_files
    }
    if result.source_path in media_destinations:
        raise BunpoAdapterError("adapter media must not overwrite the source archive")
    if canonical_path in media_destinations or report_path in media_destinations:
        raise BunpoAdapterError("canonical output or report collides with adapter media")
    for relative_path, content in result.media_files.items():
        path = PurePosixPath(relative_path)
        if path.is_absolute() or ".." in path.parts:
            raise BunpoAdapterError(f"unsafe canonical media path: {relative_path}")
        _atomic_write(media_directory.joinpath(*path.parts), content)
    _atomic_write(canonical_path, result.canonical_bytes())
    _atomic_write(report_path, result.report_bytes())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--expected-revision")
    parser.add_argument("--baseline-manifest", type=Path)
    parser.add_argument("--coverage-manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--media-dir", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        result = adapt_archive(
            arguments.source,
            expected_sha256=arguments.expected_sha256,
            expected_revision=arguments.expected_revision,
            baseline_manifest=arguments.baseline_manifest,
            coverage_manifest=arguments.coverage_manifest,
        )
        write_import(result, arguments.output, arguments.report, arguments.media_dir)
    except (BunpoAdapterError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    summary = {
        "source_id": SOURCE_ID,
        "revision_id": result.bundle.source_revision.revision_id,
        "source_content_sha256": result.bundle.source_revision.content_sha256,
        **result.report["counts"],
        "canonical_sha256": _sha256(result.canonical_bytes()),
        "manifest_exact_match": result.report["manifest_verification"]["exact_match"],
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

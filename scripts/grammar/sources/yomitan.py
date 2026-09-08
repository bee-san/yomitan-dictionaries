"""Validated, source-preserving adapter for Yomitan v3 grammar archives."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import io
import json
import mimetypes
from pathlib import Path, PurePosixPath
import re
import stat
from types import MappingProxyType
from typing import Any, Mapping
import zipfile

from ..model import (
    BlockKind,
    ContentBlock,
    LicenseInfo,
    MediaRecord,
    PartitionStatus,
    Provenance,
    SourceBundle,
    SourceLink,
    SourceRecord,
    SourceRevision,
    SourceSense,
    canonical_json_bytes,
)
from ..registry import get_source


MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_MEMBER_BYTES = 32 * 1024 * 1024
MAX_EXPANDED_BYTES = 256 * 1024 * 1024
MAX_MEMBERS = 10_000
_TERM_BANK = re.compile(r"^term_bank_([1-9][0-9]*)\.json$")
_TAG_BANK = re.compile(r"^tag_bank_([1-9][0-9]*)\.json$")
_LANGUAGE = re.compile(r"^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
_ROW_FIELDS = (
    "term",
    "reading",
    "definition_tags",
    "rules",
    "score",
    "glossary",
    "sequence",
    "term_tags",
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
YOMITAN_ARCHIVE_SOURCE_IDS = frozenset(
    {
        "bee-bunpo",
        "ninjal-bunkei",
        "nihongo-kyoshi",
        "donna-toki",
        "e-de-wakaru",
        "dojg",
        "nihongo-no-sensei",
    }
)


class AdapterError(ValueError):
    """An input archive cannot be mapped without ambiguity or data loss."""


@dataclass(frozen=True, slots=True)
class YomitanAdaptation:
    bundle: SourceBundle
    report: Mapping[str, Any]
    media_bytes: Mapping[str, bytes]


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise AdapterError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _json(content: bytes, context: str) -> Any:
    try:
        return json.loads(content, object_pairs_hook=_unique_object)
    except AdapterError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AdapterError(f"invalid {context} JSON: {error}") from error


def _safe_member_name(name: str) -> None:
    normalized = name[:-1] if name.endswith("/") else name
    path = PurePosixPath(normalized)
    if (
        not normalized
        or "\\" in name
        or path.is_absolute()
        or path.as_posix() != normalized
        or ".." in path.parts
        or any(ord(character) < 32 for character in name)
    ):
        raise AdapterError(f"unsafe archive member path: {name!r}")


def _validated_archive(path: str | Path) -> tuple[bytes, zipfile.ZipFile]:
    source = Path(path)
    try:
        metadata = source.lstat()
    except OSError as error:
        raise AdapterError(f"cannot read Yomitan archive: {error}") from error
    if not stat.S_ISREG(metadata.st_mode) or source.is_symlink():
        raise AdapterError("Yomitan input must be a regular non-symbolic-link file")
    if metadata.st_size > MAX_ARCHIVE_BYTES:
        raise AdapterError(f"Yomitan archive exceeds {MAX_ARCHIVE_BYTES} byte limit")
    try:
        content = source.read_bytes()
        archive = zipfile.ZipFile(io.BytesIO(content))
    except (OSError, zipfile.BadZipFile) as error:
        raise AdapterError(f"invalid Yomitan ZIP: {error}") from error
    infos = archive.infolist()
    if len(infos) > MAX_MEMBERS:
        archive.close()
        raise AdapterError(f"Yomitan archive exceeds {MAX_MEMBERS} member limit")
    names = [info.filename for info in infos]
    if len(names) != len(set(names)):
        archive.close()
        raise AdapterError("duplicate archive member")
    expanded = 0
    for info in infos:
        _safe_member_name(info.filename)
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            archive.close()
            raise AdapterError(
                f"symbolic-link archive member is not allowed: {info.filename}"
            )
        if info.flag_bits & 1:
            archive.close()
            raise AdapterError(
                f"encrypted archive member is not allowed: {info.filename}"
            )
        if info.file_size > MAX_MEMBER_BYTES:
            archive.close()
            raise AdapterError(f"archive member exceeds size limit: {info.filename}")
        expanded += info.file_size
        if expanded > MAX_EXPANDED_BYTES:
            archive.close()
            raise AdapterError("expanded Yomitan archive exceeds total size limit")
    if archive.testzip() is not None:
        archive.close()
        raise AdapterError("Yomitan archive CRC failure")
    return content, archive


def _required_text(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AdapterError(f"{context} must be a non-empty string")
    return value


def _validate_index(index: Any) -> dict[str, Any]:
    if not isinstance(index, dict):
        raise AdapterError("index.json must contain an object")
    if type(index.get("format")) is not int or index["format"] != 3:
        raise AdapterError("index.json must declare Yomitan format 3")
    _required_text(index.get("title"), "index title")
    _required_text(index.get("revision"), "index revision")
    if "sequenced" in index and not isinstance(index["sequenced"], bool):
        raise AdapterError("index sequenced must be boolean")
    return index


def _bank_names(
    archive: zipfile.ZipFile, pattern: re.Pattern[str], label: str
) -> list[str]:
    matched: list[tuple[int, str]] = []
    for name in archive.namelist():
        match = pattern.fullmatch(name)
        if match:
            matched.append((int(match.group(1)), name))
        elif name.startswith(label + "_") and name.endswith(".json"):
            raise AdapterError(f"malformed {label} bank filename: {name}")
    matched.sort()
    if label == "term_bank" and not matched:
        raise AdapterError("Yomitan grammar archive contains no term banks")
    if matched and [number for number, _ in matched] != list(
        range(1, len(matched) + 1)
    ):
        raise AdapterError(f"{label} banks must be contiguous from 1")
    return [name for _, name in matched]


def _validate_row(row: Any, context: str) -> list[Any]:
    if not isinstance(row, list) or len(row) != len(_ROW_FIELDS):
        raise AdapterError(f"{context} must contain exactly eight fields")
    term, reading, definition_tags, rules, score, glossary, sequence, term_tags = row
    _required_text(term, f"{context} term")
    for value, label in (
        (reading, "reading"),
        (definition_tags, "definition tags"),
        (rules, "rules"),
        (term_tags, "term tags"),
    ):
        if not isinstance(value, str):
            raise AdapterError(f"{context} {label} must be a string")
    if not isinstance(score, int) or isinstance(score, bool):
        raise AdapterError(f"{context} score must be an integer")
    if not isinstance(glossary, list) or not glossary:
        raise AdapterError(f"{context} glossary must be a non-empty list")
    if not isinstance(sequence, int) or isinstance(sequence, bool):
        raise AdapterError(f"{context} sequence must be an integer")
    try:
        canonical_json_bytes(row)
    except ValueError as error:
        raise AdapterError(f"{context} is not canonical JSON data: {error}") from error
    return row


def _media_references(value: Any) -> set[str]:
    result: set[str] = set()
    if isinstance(value, list):
        for item in value:
            result.update(_media_references(item))
    elif isinstance(value, dict):
        if value.get("tag") in {"img", "image", "audio"} and isinstance(
            value.get("path"), str
        ):
            _safe_member_name(value["path"])
            result.add(value["path"])
        for item in value.values():
            result.update(_media_references(item))
    return result


def _media_type(name: str, content: bytes) -> str:
    suffix = PurePosixPath(name).suffix.lower()
    signatures = {
        ".png": ("image/png", lambda value: value.startswith(b"\x89PNG\r\n\x1a\n")),
        ".jpg": ("image/jpeg", lambda value: value.startswith(b"\xff\xd8\xff")),
        ".jpeg": ("image/jpeg", lambda value: value.startswith(b"\xff\xd8\xff")),
        ".gif": ("image/gif", lambda value: value.startswith((b"GIF87a", b"GIF89a"))),
        ".webp": (
            "image/webp",
            lambda value: value.startswith(b"RIFF") and value[8:12] == b"WEBP",
        ),
        ".svg": ("image/svg+xml", lambda value: b"<svg" in value[:1024].lower()),
        ".mp3": (
            "audio/mpeg",
            lambda value: value.startswith(
                (b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")
            ),
        ),
        ".ogg": ("audio/ogg", lambda value: value.startswith(b"OggS")),
        ".wav": (
            "audio/wav",
            lambda value: value.startswith(b"RIFF") and value[8:12] == b"WAVE",
        ),
    }
    if suffix in signatures:
        media_type, valid = signatures[suffix]
        if not valid(content):
            raise AdapterError(f"media signature does not match filename: {name}")
        return media_type
    guessed, _ = mimetypes.guess_type(name)
    if guessed and guessed.startswith(("image/", "audio/")):
        return guessed
    raise AdapterError(f"unsupported referenced media type: {name}")


def _compact(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return " ".join(value.split())


def _languages(index: Mapping[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    for name in ("sourceLanguage", "targetLanguage"):
        value = index.get(name)
        if (
            isinstance(value, str)
            and _LANGUAGE.fullmatch(value)
            and value not in values
        ):
            values.append(value)
    return tuple(values or ["und"])


def adapt_yomitan_archive(
    path: str | Path,
    source_id: str,
    *,
    expected_archive_sha256: str | None = None,
    expected_rows: int | None = None,
    expected_revision: str | None = None,
) -> YomitanAdaptation:
    """Map every term row in one allowlisted Yomitan v3 archive."""
    if source_id not in YOMITAN_ARCHIVE_SOURCE_IDS:
        raise AdapterError(f"{source_id} is not registered as a Yomitan archive")
    if (
        not isinstance(expected_archive_sha256, str)
        or _SHA256.fullmatch(expected_archive_sha256) is None
    ):
        raise AdapterError("Yomitan input requires an external lowercase SHA-256 pin")
    definition = get_source(source_id)
    if definition.import_mode.value != "content":
        raise AdapterError(f"{source_id} is not a content source")
    archive_content, archive = _validated_archive(path)
    try:
        archive_sha256 = _sha256(archive_content)
        if archive_sha256 != expected_archive_sha256:
            raise AdapterError(
                f"{source_id} archive SHA-256 mismatch: expected "
                f"{expected_archive_sha256}, got {archive_sha256}"
            )
        if "index.json" not in archive.namelist():
            raise AdapterError("Yomitan archive is missing index.json")
        index = _validate_index(_json(archive.read("index.json"), "index.json"))
        if expected_revision is not None and index["revision"] != expected_revision:
            raise AdapterError(
                f"{source_id} index revision mismatch: expected {expected_revision!r}, "
                f"got {index['revision']!r}"
            )
        term_banks = _bank_names(archive, _TERM_BANK, "term_bank")
        tag_banks = _bank_names(archive, _TAG_BANK, "tag_bank")
        revision_id = f"yomitan-{archive_sha256}"
        prefix = f"{source_id}:{revision_id}"
        rows: list[tuple[str, int, list[Any]]] = []
        bank_reports: list[dict[str, Any]] = []
        for bank_name in term_banks:
            raw = archive.read(bank_name)
            bank = _json(raw, bank_name)
            if not isinstance(bank, list):
                raise AdapterError(f"{bank_name} must contain a list")
            bank_reports.append(
                {"path": bank_name, "rows": len(bank), "sha256": _sha256(raw)}
            )
            for row_index, raw_row in enumerate(bank):
                rows.append(
                    (
                        bank_name,
                        row_index,
                        _validate_row(raw_row, f"{bank_name} row {row_index}"),
                    )
                )
        if expected_rows is not None and len(rows) != expected_rows:
            raise AdapterError(
                f"{source_id} row count mismatch: expected {expected_rows}, got {len(rows)}"
            )
        sequence_counts = Counter(row[6] for _, _, row in rows)
        sequence_meaningful = index.get("sequenced") is True and (
            len(rows) <= 1 or len(sequence_counts) > 1
        )

        tag_reports: list[dict[str, Any]] = []
        for bank_name in tag_banks:
            raw = archive.read(bank_name)
            bank = _json(raw, bank_name)
            if not isinstance(bank, list):
                raise AdapterError(f"{bank_name} must contain a list")
            tag_reports.append(
                {"path": bank_name, "rows": len(bank), "sha256": _sha256(raw)}
            )

        records: list[SourceRecord] = []
        senses: list[SourceSense] = []
        media_uses: dict[str, list[tuple[str, str]]] = {}
        block_language = _languages(index)[-1]
        for order, (bank_name, row_index, row) in enumerate(rows):
            record_id = f"{prefix}:record:row-{order + 1:06d}"
            sense_id = f"{record_id}:sense:row"
            raw_row = canonical_json_bytes(row)
            field_hashes = {
                name: _sha256(canonical_json_bytes(value))
                for name, value in zip(_ROW_FIELDS, row, strict=True)
            }
            field_hashes["row"] = _sha256(raw_row)
            provenance = Provenance(
                source_id=source_id,
                revision_id=revision_id,
                source_record_id=record_id,
                locator=f"{bank_name}#/{row_index}",
                content_sha256=_sha256(raw_row),
            )
            records.append(SourceRecord(record_id, row[0], order, field_hashes))
            links: tuple[SourceLink, ...] = ()
            upstream_url = index.get("url")
            if isinstance(upstream_url, str) and upstream_url.strip():
                try:
                    links = (SourceLink("Upstream dictionary source", upstream_url),)
                except ValueError as error:
                    raise AdapterError(f"unsafe upstream index URL: {error}") from error
            concept_ref = (
                f"{source_id}:yomitan-sequence:{row[6]}"
                if sequence_meaningful
                else None
            )
            senses.append(
                SourceSense(
                    source_sense_id=sense_id,
                    source_record_id=record_id,
                    partition_status=(
                        PartitionStatus.SOURCE_GROUPED
                        if concept_ref is not None
                        else PartitionStatus.NEEDS_REVIEW
                    ),
                    concept_ref=concept_ref,
                    blocks=(
                        ContentBlock(
                            block_id=f"{sense_id}:block:yomitan-row",
                            kind=BlockKind.OTHER,
                            language=block_language,
                            content={
                                "bank": bank_name,
                                "row_index": row_index,
                                "row": row,
                            },
                            order=0,
                            provenance=provenance,
                        ),
                    ),
                    links=links,
                )
            )
            for media_path in sorted(_media_references(row[5])):
                media_uses.setdefault(media_path, []).append(
                    (record_id, provenance.locator)
                )

        media_records: list[MediaRecord] = []
        media_bytes: dict[str, bytes] = {}
        media_report: list[dict[str, Any]] = []
        media_by_digest: dict[str, MediaRecord] = {}
        for original_path, uses in sorted(media_uses.items()):
            if original_path not in archive.namelist():
                raise AdapterError(
                    f"referenced media is missing from archive: {original_path}"
                )
            content = archive.read(original_path)
            digest = _sha256(content)
            suffix = PurePosixPath(original_path).suffix.lower()
            target_path = f"media/{source_id}/{digest}{suffix}"
            if digest not in media_by_digest:
                record_id, _ = uses[0]
                media_provenance = Provenance(
                    source_id=source_id,
                    revision_id=revision_id,
                    source_record_id=record_id,
                    locator=f"archive-member:{original_path}",
                    content_sha256=digest,
                )
                media_record = MediaRecord(
                    media_id=f"{prefix}:media:{digest}",
                    source_path=target_path,
                    content_sha256=digest,
                    media_type=_media_type(original_path, content),
                    byte_count=len(content),
                    provenance=media_provenance,
                )
                media_by_digest[digest] = media_record
                media_records.append(media_record)
                media_bytes[target_path] = content
            media_report.append(
                {
                    "original_path": original_path,
                    "target_path": media_by_digest[digest].source_path,
                    "sha256": digest,
                    "references": [record_id for record_id, _ in uses],
                }
            )

        mapped_names = {"index.json", *term_banks, *tag_banks, *media_uses}
        auxiliary = []
        for name in sorted(set(archive.namelist()) - mapped_names):
            if name.endswith("/"):
                continue
            raw = archive.read(name)
            auxiliary.append(
                {
                    "path": name,
                    "sha256": _sha256(raw),
                    "bytes": len(raw),
                    "reason": "not referenced by a grammar term row; retained in immutable source archive",
                }
            )

        upstream_attribution = _compact(index.get("attribution"))
        attribution = definition.attribution
        if upstream_attribution:
            attribution += f" Upstream index attribution: {upstream_attribution}."
        index_json = json.dumps(
            index, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        revision = SourceRevision(
            source_id=source_id,
            revision_id=revision_id,
            content_sha256=archive_sha256,
            languages=_languages(index),
            attribution=attribution,
            license=LicenseInfo(
                definition.license_identifier,
                "Registry policy applies; original index metadata and immutable archive hash are preserved.",
                None,
            ),
            access_mode=definition.access_mode,
            import_mode=definition.import_mode,
            publication_mode=definition.publication_mode,
            generated=False,
            provenance_note=(
                f"Validated Yomitan v3 archive SHA-256 {archive_sha256}. "
                f"Original index.json: {index_json}"
            ),
        )
        bundle = SourceBundle(
            revision, tuple(records), tuple(senses), tuple(media_records)
        )
        bundle.validate()
        report = {
            "format": "ugd-yomitan-adapter-report",
            "format_version": 1,
            "source_id": source_id,
            "revision_id": revision_id,
            "archive_sha256": archive_sha256,
            "index": index,
            "expected_rows": expected_rows,
            "imported_rows": len(records),
            "rejected_rows": 0,
            "sequence_identity": {
                "declared_sequenced": index.get("sequenced") is True,
                "meaningful": sequence_meaningful,
                "unique_values": len(sequence_counts),
            },
            "source_record_ids": [record.source_record_id for record in records],
            "term_banks": bank_reports,
            "tag_banks": tag_reports,
            "media": media_report,
            "auxiliary_members": auxiliary,
        }
        return YomitanAdaptation(
            bundle=bundle,
            report=MappingProxyType(report),
            media_bytes=MappingProxyType(media_bytes),
        )
    finally:
        archive.close()

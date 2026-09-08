#!/usr/bin/env python3
"""Convert the pinned NINJAL 2026.01 XML dataset to canonical grammar records."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
from typing import Any, Iterable, Mapping
import xml.etree.ElementTree as ElementTree
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


SOURCE_ID = "ninjal-bunkei"
DOI = "10.15084/0002000610"
DOI_URL = f"https://doi.org/{DOI}"
SOURCE_ARCHIVE_URL = (
    "https://repository.ninjal.ac.jp/record/2000610/files/"
    "nihongo_bunkei_database20260126.zip"
)
HEADWORDS_URL = "https://repository.ninjal.ac.jp/record/2000610/files/headwords.txt"
LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"
REFERENCE_YOMITAN_URL = (
    "https://github.com/bee-san/yomitan-dictionaries/releases/download/2026.09.01/"
    "ninjal-bunkei-yomitan-2026.01.33.zip"
)
MAX_SOURCE_BYTES = 16 * 1024 * 1024
MAX_HEADWORDS_BYTES = 1024 * 1024
MAX_MEMBER_BYTES = 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 2_500
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RUBY = re.compile(r"〓([^〔〕]+)〔[^〔〕]+〕")

_ROOT_FIELDS = {"SentencePattern", "Reading", "Category", "GeneralExplanation", "Sense"}
_ROOT_SINGLE = ("SentencePattern", "Reading", "GeneralExplanation")
_SENSE_SINGLE = (
    "SenceCategory",
    "Level",
    "Usage",
    "UsageNotes",
    "Style",
    "CommonlyUsedWordsTogether",
    "Orthography",
    "AlternativeForm",
    "SimilarExpressions",
    "ContrastingExpression",
)
_SENSE_FIELDS = {*_SENSE_SINGLE, "Connection"}
_CONNECTION_FIELDS = {"ConnectionType", "ExampleSet"}
_EXAMPLE_FIELDS = {"SceneDescription", "Example", "ExampleNote"}


class NinjalAdapterError(RuntimeError):
    """The pinned input, XML schema, or coverage contract failed."""


@dataclass(frozen=True, slots=True)
class NinjalCounts:
    records: int
    senses: int
    connections: int
    examples: int

    def __post_init__(self) -> None:
        for name in ("records", "senses", "connections", "examples"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class NinjalPins:
    revision_id: str
    source_sha256: str
    headwords_sha256: str
    counts: NinjalCounts
    reference_yomitan_revision: str
    reference_yomitan_sha256: str

    def __post_init__(self) -> None:
        if not self.revision_id or not self.reference_yomitan_revision:
            raise ValueError("revision pins must be non-empty")
        for name in ("source_sha256", "headwords_sha256", "reference_yomitan_sha256"):
            if _SHA256.fullmatch(getattr(self, name)) is None:
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")
        if not isinstance(self.counts, NinjalCounts):
            raise ValueError("counts must be NinjalCounts")


@dataclass(frozen=True, slots=True)
class NinjalImportResult:
    bundle: SourceBundle
    report: Mapping[str, Any]


OFFICIAL_PINS = NinjalPins(
    revision_id="2026.01.26",
    source_sha256="21db3087c49c9e05eaf50f735d5c704b971df20d206b56a079dee1f50bf3a6a9",
    headwords_sha256="dafa092480006c2de09a4cfdff90ebcb1fdbc93a7347a677b206293a1cf588c5",
    counts=NinjalCounts(records=800, senses=958, connections=1988, examples=9552),
    reference_yomitan_revision="2026.01.33",
    reference_yomitan_sha256="c55469eaad15495012b05afb28817db50a96b6225ebf8f834428a78910c9c9ae",
)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _read_regular_file(path: str | Path, *, max_bytes: int, label: str) -> bytes:
    absolute = Path(os.path.abspath(path))
    resolved = absolute.parent.resolve() / absolute.name
    directory_flags = (
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        directory_descriptor = os.open(resolved.anchor, directory_flags)
        try:
            for component in resolved.parts[1:-1]:
                next_descriptor = os.open(
                    component,
                    directory_flags,
                    dir_fd=directory_descriptor,
                )
                os.close(directory_descriptor)
                directory_descriptor = next_descriptor
            descriptor = os.open(
                resolved.name,
                file_flags,
                dir_fd=directory_descriptor,
            )
        finally:
            os.close(directory_descriptor)
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise NinjalAdapterError(f"{label} must be a regular file")
        if details.st_size > max_bytes:
            raise NinjalAdapterError(f"{label} exceeds {max_bytes} bytes")
        with os.fdopen(descriptor, "rb") as source:
            descriptor = None
            content = source.read(max_bytes + 1)
    except NinjalAdapterError:
        raise
    except OSError as error:
        raise NinjalAdapterError(f"cannot read {label}: {error}") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(content) > max_bytes:
        raise NinjalAdapterError(f"{label} exceeds {max_bytes} bytes")
    return content


def _pinned_file(
    path: str | Path,
    expected_sha256: str,
    *,
    max_bytes: int,
    label: str,
) -> bytes:
    content = _read_regular_file(path, max_bytes=max_bytes, label=label)
    if _sha256(content) != expected_sha256:
        raise NinjalAdapterError(f"{label} SHA-256 mismatch")
    return content


def _safe_zip_members(
    content: bytes,
    *,
    label: str,
    max_member_bytes: int = MAX_MEMBER_BYTES,
) -> dict[str, bytes]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except (OSError, zipfile.BadZipFile) as error:
        raise NinjalAdapterError(f"invalid {label}: {error}") from error
    with archive:
        infos = archive.infolist()
        if not infos or len(infos) > MAX_ARCHIVE_MEMBERS:
            raise NinjalAdapterError(f"{label} has an invalid member count")
        names = [item.filename for item in infos]
        if len(names) != len(set(names)):
            raise NinjalAdapterError(f"{label} contains duplicate member names")
        total = 0
        members: dict[str, bytes] = {}
        for item in infos:
            path = PurePosixPath(item.filename)
            mode = item.external_attr >> 16
            if (
                not item.filename
                or "\\" in item.filename
                or path.is_absolute()
                or path.name != item.filename
                or ".." in path.parts
                or stat.S_ISLNK(mode)
                or item.flag_bits & 0x1
            ):
                raise NinjalAdapterError(
                    f"{label} contains an unsafe member: {item.filename!r}"
                )
            if item.file_size > max_member_bytes:
                raise NinjalAdapterError(
                    f"{label} member exceeds {max_member_bytes} bytes"
                )
            total += item.file_size
            if total > MAX_TOTAL_UNCOMPRESSED_BYTES:
                raise NinjalAdapterError(f"{label} exceeds the uncompressed size limit")
            try:
                member = archive.read(item)
            except (OSError, RuntimeError, zipfile.BadZipFile) as error:
                raise NinjalAdapterError(
                    f"cannot read {label} member {item.filename!r}: {error}"
                ) from error
            if len(member) != item.file_size:
                raise NinjalAdapterError(f"{label} member size changed while reading")
            members[item.filename] = member
        if archive.testzip() is not None:
            raise NinjalAdapterError(f"{label} failed CRC validation")
        return members


def _normalize_wave_dash(value: str) -> str:
    return value.replace("～", "〜")


def _plain_source_text(value: str) -> str:
    return _normalize_wave_dash(_RUBY.sub(lambda match: match.group(1), value)).strip()


def _text(element: ElementTree.Element, context: str) -> str:
    if list(element):
        raise NinjalAdapterError(f"{context} must contain text only")
    if element.attrib:
        raise NinjalAdapterError(f"{context} must not contain attributes")
    return element.text or ""


def _one(parent: ElementTree.Element, tag: str, context: str) -> ElementTree.Element:
    children = parent.findall(tag)
    if len(children) != 1:
        qualifier = "missing" if not children else "duplicate"
        raise NinjalAdapterError(f"{context} has {qualifier} {tag}")
    return children[0]


def _reject_unknown(
    parent: ElementTree.Element, allowed: set[str], context: str
) -> None:
    unknown = sorted({child.tag for child in parent if child.tag not in allowed})
    if unknown:
        raise NinjalAdapterError(f"{context} has unsupported fields: {unknown}")
    if parent.attrib:
        raise NinjalAdapterError(f"{context} must not contain attributes")


def _parse_xml(content: bytes, filename: str) -> ElementTree.Element:
    upper = content.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise NinjalAdapterError(f"{filename} contains a forbidden XML declaration")
    try:
        root = ElementTree.fromstring(content)
    except (ElementTree.ParseError, UnicodeDecodeError) as error:
        raise NinjalAdapterError(f"cannot parse {filename}: {error}") from error
    if root.tag != "Entry":
        raise NinjalAdapterError(f"{filename} root must be Entry")
    _reject_unknown(root, _ROOT_FIELDS, filename)
    for tag in _ROOT_SINGLE:
        _one(root, tag, filename)
    if len(root.findall("Category")) > 1:
        raise NinjalAdapterError(f"{filename} has duplicate Category")
    if not root.findall("Sense"):
        raise NinjalAdapterError(f"{filename} has no Sense")
    for sense_index, sense in enumerate(root.findall("Sense"), 1):
        sense_context = f"{filename} Sense[{sense_index}]"
        _reject_unknown(sense, _SENSE_FIELDS, sense_context)
        for tag in _SENSE_SINGLE:
            _one(sense, tag, sense_context)
        if not sense.findall("Connection"):
            raise NinjalAdapterError(f"{sense_context} has no Connection")
        for connection_index, connection in enumerate(sense.findall("Connection"), 1):
            connection_context = f"{sense_context} Connection[{connection_index}]"
            _reject_unknown(connection, _CONNECTION_FIELDS, connection_context)
            _one(connection, "ConnectionType", connection_context)
            if not connection.findall("ExampleSet"):
                raise NinjalAdapterError(f"{connection_context} has no ExampleSet")
            for example_index, example in enumerate(
                connection.findall("ExampleSet"), 1
            ):
                example_context = f"{connection_context} ExampleSet[{example_index}]"
                _reject_unknown(example, _EXAMPLE_FIELDS, example_context)
                for tag in _EXAMPLE_FIELDS:
                    _one(example, tag, example_context)
    return root


def _field_hashes(root: ElementTree.Element, xml_content: bytes) -> dict[str, str]:
    fields = {"xml": _sha256(xml_content)}
    for tag in ("SentencePattern", "Reading", "Category", "GeneralExplanation"):
        elements = root.findall(tag)
        if elements:
            fields[tag] = _sha256(_text(elements[0], tag).encode("utf-8"))
    for sense_index, sense in enumerate(root.findall("Sense"), 1):
        for tag in _SENSE_SINGLE:
            locator = f"Sense[{sense_index}]/{tag}"
            fields[locator] = _sha256(
                _text(_one(sense, tag, locator), locator).encode("utf-8")
            )
        for connection_index, connection in enumerate(sense.findall("Connection"), 1):
            prefix = f"Sense[{sense_index}]/Connection[{connection_index}]"
            connection_type = _one(connection, "ConnectionType", prefix)
            fields[f"{prefix}/ConnectionType"] = _sha256(
                _text(connection_type, f"{prefix}/ConnectionType").encode("utf-8")
            )
            for example_index, example in enumerate(
                connection.findall("ExampleSet"), 1
            ):
                example_prefix = f"{prefix}/ExampleSet[{example_index}]"
                for tag in ("SceneDescription", "Example", "ExampleNote"):
                    locator = f"{example_prefix}/{tag}"
                    fields[locator] = _sha256(
                        _text(_one(example, tag, locator), locator).encode("utf-8")
                    )
    return fields


def _provenance(
    record_id: str, revision_id: str, locator: str, digest: str
) -> Provenance:
    return Provenance(
        source_id=SOURCE_ID,
        revision_id=revision_id,
        source_record_id=record_id,
        locator=locator,
        content_sha256=digest,
    )


def _block(
    *,
    block_id: str,
    kind: BlockKind,
    content: Mapping[str, Any],
    order: int,
    record_id: str,
    revision_id: str,
    locator: str,
    source_text: str,
) -> ContentBlock:
    return ContentBlock(
        block_id=block_id,
        kind=kind,
        language="ja",
        content=content,
        order=order,
        provenance=_provenance(
            record_id,
            revision_id,
            locator,
            _sha256(source_text.encode("utf-8")),
        ),
    )


def _record_and_senses(
    *,
    root: ElementTree.Element,
    xml_content: bytes,
    filename: str,
    order: int,
    revision_id: str,
) -> tuple[SourceRecord, list[SourceSense], int, int]:
    record_id = f"{SOURCE_ID}:{revision_id}:record:{order + 1:04d}"
    sentence_pattern = _text(
        _one(root, "SentencePattern", filename), f"{filename} SentencePattern"
    )
    record = SourceRecord(
        source_record_id=record_id,
        raw_expression=sentence_pattern,
        order=order,
        field_hashes=_field_hashes(root, xml_content),
    )
    source_wide: list[tuple[str, str, BlockKind]] = []
    category = root.find("Category")
    if category is not None and _text(category, f"{filename} Category").strip():
        source_wide.append(
            ("Category", _text(category, f"{filename} Category"), BlockKind.OTHER)
        )
    general = _text(
        _one(root, "GeneralExplanation", filename),
        f"{filename} GeneralExplanation",
    )
    if general.strip():
        source_wide.append(("GeneralExplanation", general, BlockKind.MEANING))

    senses: list[SourceSense] = []
    connection_total = 0
    example_total = 0
    field_kinds = (
        ("SenceCategory", BlockKind.MEANING),
        ("Usage", BlockKind.MEANING),
        ("UsageNotes", BlockKind.NOTE),
        ("Style", BlockKind.RESTRICTION),
        ("CommonlyUsedWordsTogether", BlockKind.NOTE),
        ("Orthography", BlockKind.FORMATION),
        ("AlternativeForm", BlockKind.FORMATION),
        ("SimilarExpressions", BlockKind.NOTE),
        ("ContrastingExpression", BlockKind.NOTE),
    )
    for sense_index, sense in enumerate(root.findall("Sense"), 1):
        sense_id = f"{record_id}:sense:{sense_index:03d}"
        blocks: list[ContentBlock] = []
        block_order = 0
        if sense_index == 1:
            for field_name, value, kind in source_wide:
                blocks.append(
                    _block(
                        block_id=f"{sense_id}:block:record-{field_name.lower()}",
                        kind=kind,
                        content={
                            "field": field_name,
                            "scope": "record",
                            "value": value,
                        },
                        order=block_order,
                        record_id=record_id,
                        revision_id=revision_id,
                        locator=f"{filename}#{field_name}",
                        source_text=value,
                    )
                )
                block_order += 1
        values: dict[str, str] = {}
        for field_name, kind in field_kinds:
            value = _text(
                _one(sense, field_name, sense_id),
                f"{filename} Sense[{sense_index}]/{field_name}",
            )
            values[field_name] = value
            if not value.strip():
                continue
            blocks.append(
                _block(
                    block_id=f"{sense_id}:block:{field_name.lower()}",
                    kind=kind,
                    content={"field": field_name, "value": value},
                    order=block_order,
                    record_id=record_id,
                    revision_id=revision_id,
                    locator=f"{filename}#Sense[{sense_index}]/{field_name}",
                    source_text=value,
                )
            )
            block_order += 1

        examples: list[ExamplePair] = []
        for connection_index, connection in enumerate(sense.findall("Connection"), 1):
            connection_total += 1
            connection_type = _text(
                _one(connection, "ConnectionType", sense_id),
                f"{filename} Sense[{sense_index}]/Connection[{connection_index}]/ConnectionType",
            )
            blocks.append(
                _block(
                    block_id=f"{sense_id}:block:connection-{connection_index:03d}",
                    kind=BlockKind.FORMATION,
                    content={
                        "field": "Connection",
                        "connection_index": connection_index,
                        "connection_type": connection_type,
                    },
                    order=block_order,
                    record_id=record_id,
                    revision_id=revision_id,
                    locator=(
                        f"{filename}#Sense[{sense_index}]/Connection[{connection_index}]"
                        "/ConnectionType"
                    ),
                    source_text=connection_type,
                )
            )
            block_order += 1
            for example_index, example in enumerate(
                connection.findall("ExampleSet"), 1
            ):
                example_total += 1
                payload = {
                    "connection_index": connection_index,
                    "example_index": example_index,
                    "scene_description": _text(
                        _one(example, "SceneDescription", sense_id),
                        "SceneDescription",
                    ),
                    "text": _text(_one(example, "Example", sense_id), "Example"),
                    "note": _text(
                        _one(example, "ExampleNote", sense_id), "ExampleNote"
                    ),
                }
                locator = (
                    f"{filename}#Sense[{sense_index}]/Connection[{connection_index}]"
                    f"/ExampleSet[{example_index}]"
                )
                examples.append(
                    ExamplePair(
                        example_id=(
                            f"{sense_id}:example:{connection_index:03d}-{example_index:03d}"
                        ),
                        japanese=payload,
                        translation=None,
                        translation_language=None,
                        order=len(examples),
                        provenance=_provenance(
                            record_id,
                            revision_id,
                            locator,
                            _sha256(canonical_json_bytes(payload)),
                        ),
                    )
                )

        level = _text(
            _one(sense, "Level", sense_id), f"{filename} Sense[{sense_index}]/Level"
        ).strip()
        levels = (f"NINJAL Level {level}",) if level else ()
        style = values["Style"].strip()
        registers = (_plain_source_text(style),) if style else ()
        senses.append(
            SourceSense(
                source_sense_id=sense_id,
                source_record_id=record_id,
                partition_status=PartitionStatus.SOURCE_EXPLICIT,
                blocks=tuple(blocks),
                examples=tuple(examples),
                level_labels=levels,
                register_labels=registers,
                links=(
                    SourceLink("NINJAL dataset DOI", DOI_URL),
                    SourceLink(
                        "NINJAL Bunkei Bank", "https://www2.ninjal.ac.jp/bunkeibank/"
                    ),
                ),
            )
        )
    return record, senses, connection_total, example_total


def _ordered_xml(
    xml_members: Mapping[str, bytes],
    headwords: list[str],
) -> tuple[list[tuple[str, bytes]], dict[str, Any]]:
    positions: dict[str, list[int]] = defaultdict(list)
    for index, headword in enumerate(headwords, 1):
        positions[_normalize_wave_dash(headword)].append(index)
    stems: dict[str, str] = {}
    for filename in xml_members:
        normalized = _normalize_wave_dash(Path(filename).stem)
        if normalized in stems:
            raise NinjalAdapterError(f"duplicate normalized XML headword: {normalized}")
        stems[normalized] = filename
    rank = {headword: indices[-1] for headword, indices in positions.items()}
    ordered_names = sorted(
        xml_members,
        key=lambda filename: (
            rank.get(_normalize_wave_dash(Path(filename).stem), len(headwords) + 1),
            _normalize_wave_dash(Path(filename).stem),
        ),
    )
    anomalies = {
        "duplicate_headwords": {
            headword: indices
            for headword, indices in sorted(positions.items())
            if len(indices) > 1
        },
        "headwords_without_xml": sorted(set(positions) - set(stems)),
        "xml_without_headword": sorted(set(stems) - set(positions)),
    }
    return [(name, xml_members[name]) for name in ordered_names], anomalies


def _json_member(members: Mapping[str, bytes], name: str, label: str) -> Any:
    if name not in members:
        raise NinjalAdapterError(f"{label} is missing {name}")
    try:
        return json.loads(members[name])
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise NinjalAdapterError(f"{label} has invalid {name}: {error}") from error


def _verify_reference_yomitan(
    path: str | Path,
    *,
    pins: NinjalPins,
    display_expressions: list[str],
) -> dict[str, Any]:
    content = _pinned_file(
        path,
        pins.reference_yomitan_sha256,
        max_bytes=MAX_SOURCE_BYTES,
        label="reference Yomitan ZIP",
    )
    members = _safe_zip_members(
        content,
        label="reference Yomitan ZIP",
        max_member_bytes=MAX_TOTAL_UNCOMPRESSED_BYTES,
    )
    index = _json_member(members, "index.json", "reference Yomitan ZIP")
    if (
        not isinstance(index, dict)
        or index.get("revision") != pins.reference_yomitan_revision
    ):
        raise NinjalAdapterError("reference Yomitan revision mismatch")
    attribution = index.get("attribution")
    if (
        not isinstance(attribution, str)
        or DOI not in attribution
        or "CC BY 4.0" not in attribution
    ):
        raise NinjalAdapterError(
            "reference Yomitan attribution does not preserve DOI and licence"
        )
    bank_names: list[tuple[int, str]] = []
    for name in members:
        match = re.fullmatch(r"term_bank_(\d+)\.json", name)
        if match:
            bank_names.append((int(match.group(1)), name))
    bank_names.sort()
    if not bank_names or [number for number, _ in bank_names] != list(
        range(1, len(bank_names) + 1)
    ):
        raise NinjalAdapterError("reference Yomitan term banks are not contiguous")
    rows: list[list[Any]] = []
    for _, name in bank_names:
        value = _json_member(members, name, "reference Yomitan ZIP")
        if not isinstance(value, list):
            raise NinjalAdapterError(f"reference Yomitan {name} must contain a list")
        for row in value:
            if (
                not isinstance(row, list)
                or len(row) < 8
                or not isinstance(row[0], str)
                or not isinstance(row[1], str)
                or not isinstance(row[6], int)
                or isinstance(row[6], bool)
            ):
                raise NinjalAdapterError(
                    f"reference Yomitan {name} contains an invalid term row"
                )
            rows.append(row)
    expected_sequences = set(range(1, len(display_expressions) + 1))
    sequences = {row[6] for row in rows}
    if sequences != expected_sequences:
        raise NinjalAdapterError("reference Yomitan sequence set mismatch")
    terms_by_sequence: dict[int, set[str]] = defaultdict(set)
    for row in rows:
        terms_by_sequence[row[6]].add(_normalize_wave_dash(row[0]))
    for sequence, expression in enumerate(display_expressions, 1):
        if _normalize_wave_dash(expression) not in terms_by_sequence[sequence]:
            raise NinjalAdapterError(
                f"reference Yomitan sequence {sequence} has no official headword lookup row"
            )
    row_hashes = sorted(_sha256(canonical_json_bytes(row)) for row in rows)
    return {
        "archive_sha256": pins.reference_yomitan_sha256,
        "revision": pins.reference_yomitan_revision,
        "lookup_row_count": len(rows),
        "sequence_count": len(sequences),
        "lookup_identity_set_sha256": _sha256(canonical_json_bytes(row_hashes)),
        "source_family": SOURCE_ID,
        "independent_corroboration": False,
        "derivation_note": (
            "This reference archive is derived from the same pinned NINJAL revision; "
            "it verifies lookup coverage but is not independent source corroboration."
        ),
    }


def adapt_ninjal_archive(
    source_zip: str | Path,
    headwords_path: str | Path,
    *,
    pins: NinjalPins = OFFICIAL_PINS,
    reference_yomitan_zip: str | Path | None = None,
    overlap_expressions: Iterable[str] = (),
) -> NinjalImportResult:
    """Return a lossless canonical bundle and exact-set coverage report."""
    source_content = _pinned_file(
        source_zip,
        pins.source_sha256,
        max_bytes=MAX_SOURCE_BYTES,
        label="source ZIP",
    )
    headword_content = _pinned_file(
        headwords_path,
        pins.headwords_sha256,
        max_bytes=MAX_HEADWORDS_BYTES,
        label="headwords file",
    )
    try:
        headwords = [
            line.strip()
            for line in headword_content.decode("utf-8-sig").splitlines()
            if line.strip()
        ]
    except UnicodeDecodeError as error:
        raise NinjalAdapterError(f"headwords file is not UTF-8: {error}") from error
    members = _safe_zip_members(source_content, label="source ZIP")
    non_xml = sorted(name for name in members if not name.endswith(".xml"))
    if non_xml:
        raise NinjalAdapterError(f"source ZIP contains non-XML members: {non_xml}")
    if len(members) != pins.counts.records:
        raise NinjalAdapterError(
            f"expected records={pins.counts.records}, got {len(members)}"
        )
    ordered, anomalies = _ordered_xml(members, headwords)

    records: list[SourceRecord] = []
    senses: list[SourceSense] = []
    connection_count = 0
    example_count = 0
    display_expressions: list[str] = []
    level_counts: Counter[str] = Counter()
    categories: set[str] = set()
    for order, (filename, xml_content) in enumerate(ordered):
        root = _parse_xml(xml_content, filename)
        record, record_senses, connections, examples = _record_and_senses(
            root=root,
            xml_content=xml_content,
            filename=filename,
            order=order,
            revision_id=pins.revision_id,
        )
        records.append(record)
        senses.extend(record_senses)
        connection_count += connections
        example_count += examples
        display_expressions.append(_normalize_wave_dash(Path(filename).stem))
        for sense in root.findall("Sense"):
            level = _text(_one(sense, "Level", filename), "Level").strip()
            level_counts[level or "unassigned"] += 1
            category = _text(_one(sense, "SenceCategory", filename), "SenceCategory")
            categories.add(category)

    actual = NinjalCounts(
        records=len(records),
        senses=len(senses),
        connections=connection_count,
        examples=example_count,
    )
    if actual != pins.counts:
        raise NinjalAdapterError(
            f"pinned source count mismatch: expected {pins.counts}, got {actual}"
        )

    registry_attribution = (
        "National Institute for Japanese Language and Linguistics (NINJAL); "
        "preserve DOI, version and attribution."
    )
    revision = SourceRevision(
        source_id=SOURCE_ID,
        revision_id=pins.revision_id,
        content_sha256=pins.source_sha256,
        languages=("ja",),
        attribution=(
            f"{registry_attribution} Editors: Prashant Pardeshi and Yuriko Sunakawa. "
            f"Dataset: 日本語文型データベース (Version {pins.revision_id}). DOI: {DOI}."
        ),
        license=LicenseInfo(
            identifier="CC-BY-4.0",
            notice=(
                "日本語文型データベース is licensed CC BY 4.0; retain NINJAL, "
                "editor, version, and DOI attribution."
            ),
            evidence_url=LICENSE_URL,
        ),
        access_mode=AccessMode.PUBLIC_HTTP,
        import_mode=ImportMode.CONTENT,
        publication_mode=PublicationMode.ALLOWED,
        generated=False,
        provenance_note=(
            f"Official NINJAL dataset at {DOI_URL}; source archive {SOURCE_ARCHIVE_URL}; "
            f"headword order {HEADWORDS_URL}."
        ),
    )
    bundle = SourceBundle(revision, tuple(records), tuple(senses), ())
    bundle.validate()

    expected_ids = [record.source_record_id for record in records]
    imported_ids = [sense.source_record_id for sense in senses]
    imported_record_ids = [
        record_id for record_id in expected_ids if record_id in set(imported_ids)
    ]
    rejected_ids = sorted(set(expected_ids) - set(imported_record_ids))
    if imported_record_ids != expected_ids or rejected_ids:
        raise NinjalAdapterError(
            "canonical record identity set is not exactly preserved"
        )

    overlap_keys = {
        _normalize_wave_dash(value).strip()
        for value in overlap_expressions
        if value.strip()
    }
    overlap_ids: list[str] = []
    additional_ids: list[str] = []
    for record, display in zip(records, display_expressions, strict=True):
        candidates = {display, _plain_source_text(record.raw_expression)}
        target = overlap_ids if candidates & overlap_keys else additional_ids
        target.append(record.source_record_id)

    reference: dict[str, Any] | None = None
    if reference_yomitan_zip is not None:
        reference = _verify_reference_yomitan(
            reference_yomitan_zip,
            pins=pins,
            display_expressions=display_expressions,
        )
    report: dict[str, Any] = {
        "format": "ugd-ninjal-adapter-report",
        "format_version": 1,
        "source_id": SOURCE_ID,
        "revision_id": pins.revision_id,
        "doi": DOI,
        "source_archive_url": SOURCE_ARCHIVE_URL,
        "source_archive_sha256": pins.source_sha256,
        "headwords_url": HEADWORDS_URL,
        "headwords_sha256": pins.headwords_sha256,
        "license": "CC-BY-4.0",
        "source_record_count": len(records),
        "imported_record_count": len(imported_record_ids),
        "source_sense_count": len(senses),
        "connection_count": connection_count,
        "example_count": example_count,
        "expected_record_ids": expected_ids,
        "imported_record_ids": imported_record_ids,
        "rejected_record_ids": rejected_ids,
        "record_identity_set_sha256": _sha256(
            canonical_json_bytes(sorted(expected_ids))
        ),
        "level_counts": dict(sorted(level_counts.items())),
        "category_value_count": len(categories),
        "headword_line_count": len(headwords),
        "headword_anomalies": anomalies,
        "overlap_basis": (
            "caller-provided exact expressions after U+FF5E to U+301C normalization"
            if overlap_keys
            else "not provided"
        ),
        "overlap_record_ids": overlap_ids,
        "additional_record_ids": additional_ids,
        "reference_yomitan_lookup_row_count": reference["lookup_row_count"]
        if reference
        else None,
        "reference_yomitan_sequence_count": reference["sequence_count"]
        if reference
        else None,
        "reference_yomitan": reference,
    }
    report["canonical_output_sha256"] = _sha256(canonical_json_bytes(bundle.to_dict()))
    return NinjalImportResult(bundle=bundle, report=report)


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
            temporary = Path(output.name)
        temporary.replace(path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _load_overlap_expressions(path: Path | None) -> tuple[str, ...]:
    if path is None:
        return ()
    content = _read_regular_file(
        path, max_bytes=MAX_HEADWORDS_BYTES, label="overlap expressions"
    )
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise NinjalAdapterError(
            f"invalid overlap expressions JSON: {error}"
        ) from error
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise NinjalAdapterError("overlap expressions JSON must be a list of strings")
    return tuple(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Convert the checksum-pinned official NINJAL 2026.01 XML archive into "
            "source-preserving UGD canonical JSON."
        )
    )
    parser.add_argument("--source-zip", type=Path, required=True)
    parser.add_argument("--headwords", type=Path, required=True)
    parser.add_argument("--reference-yomitan-zip", type=Path, required=True)
    parser.add_argument("--overlap-expressions", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        result = adapt_ninjal_archive(
            arguments.source_zip,
            arguments.headwords,
            pins=OFFICIAL_PINS,
            reference_yomitan_zip=arguments.reference_yomitan_zip,
            overlap_expressions=_load_overlap_expressions(
                arguments.overlap_expressions
            ),
        )
        output_bytes = canonical_json_bytes(result.bundle.to_dict())
        report_bytes = canonical_json_bytes(result.report)
        if arguments.output.resolve() == arguments.report.resolve():
            raise NinjalAdapterError("output and report paths must be distinct")
        _atomic_write(arguments.output.resolve(), output_bytes)
        _atomic_write(arguments.report.resolve(), report_bytes)
    except (NinjalAdapterError, OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result.report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

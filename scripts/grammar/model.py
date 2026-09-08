"""Typed, source-preserving records for the grammar pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import math
import re
from types import MappingProxyType
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit


MODEL_FORMAT_VERSION = 1
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_REVISION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_LANGUAGE = re.compile(r"^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")


class AccessMode(str, Enum):
    LOCAL_FILE = "local-file"
    PUBLIC_HTTP = "public-http"
    METADATA_ONLY = "metadata-only"
    UNAVAILABLE = "unavailable"


class ImportMode(str, Enum):
    CONTENT = "content"
    METADATA_ONLY = "metadata-only"
    DENIED = "denied"


class PublicationMode(str, Enum):
    ALLOWED = "allowed"
    DENIED = "denied"
    UNCLEARED = "uncleared"


class BuildMode(str, Enum):
    PRIVATE = "private"
    PUBLISHABLE = "publishable"


class SelectionMode(str, Enum):
    CONTENT = "content"
    METADATA = "metadata"


class PartitionStatus(str, Enum):
    SOURCE_EXPLICIT = "source-explicit"
    SOURCE_GROUPED = "source-grouped"
    NEEDS_REVIEW = "needs-review"


class BlockKind(str, Enum):
    MEANING = "meaning"
    FORMATION = "formation"
    EXAMPLE_NOTE = "example-note"
    RESTRICTION = "restriction"
    NOTE = "note"
    ATTRIBUTION = "attribution"
    SOURCE_LINK = "source-link"
    OTHER = "other"


def _nonempty(value: str, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty, unpadded string")
    if any(ord(character) < 32 for character in value):
        raise ValueError(f"{name} contains a control character")
    return value


def _sha256(value: str | None, name: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _jsonable(value: Any, name: str = "content") -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{name} contains a non-finite number")
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(item, name) for item in value]
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{name} contains a non-string object key")
            result[key] = _jsonable(item, name)
        return result
    raise ValueError(f"{name} is not JSON-compatible")


def _qualified_prefix(source_id: str, revision_id: str) -> str:
    return f"{source_id}:{revision_id}:"


def _strict_fields(value: Mapping[str, Any], allowed: set[str], context: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"{context} has unsupported fields: {sorted(unknown)}")


def canonical_sense_id(source_sense_ids: Sequence[str]) -> str:
    """Return an order-independent identity without using spelling or meaning."""
    members = sorted({_nonempty(value, "source_sense_id") for value in source_sense_ids})
    if not members:
        raise ValueError("canonical sense requires at least one source_sense_id")
    digest = hashlib.sha256("\x1f".join(members).encode("utf-8")).hexdigest()
    return f"ugd-sense-v1:{digest}"


@dataclass(frozen=True, slots=True)
class LicenseInfo:
    identifier: str | None
    notice: str
    evidence_url: str | None

    def __post_init__(self) -> None:
        if self.identifier is not None:
            _nonempty(self.identifier, "license identifier")
        _nonempty(self.notice, "license notice")
        if self.evidence_url is not None and not self.evidence_url.startswith("https://"):
            raise ValueError("license evidence_url must use HTTPS")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "LicenseInfo":
        _strict_fields(value, {"identifier", "notice", "evidence_url"}, "license")
        return cls(value.get("identifier"), value["notice"], value.get("evidence_url"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "identifier": self.identifier,
            "notice": self.notice,
            "evidence_url": self.evidence_url,
        }


@dataclass(frozen=True, slots=True)
class SourceRevision:
    source_id: str
    revision_id: str
    content_sha256: str
    languages: tuple[str, ...]
    attribution: str
    license: LicenseInfo
    access_mode: AccessMode
    import_mode: ImportMode
    publication_mode: PublicationMode
    generated: bool
    provenance_note: str

    def __post_init__(self) -> None:
        if _SOURCE_ID.fullmatch(self.source_id) is None:
            raise ValueError("source_id must be lowercase kebab-case")
        if _REVISION_ID.fullmatch(self.revision_id) is None:
            raise ValueError("revision_id contains unsupported characters")
        _sha256(self.content_sha256, "content_sha256")
        languages = tuple(self.languages)
        if not languages or len(set(languages)) != len(languages):
            raise ValueError("languages must contain unique language tags")
        for language in languages:
            if _LANGUAGE.fullmatch(language) is None:
                raise ValueError(f"invalid language tag: {language}")
        object.__setattr__(self, "languages", languages)
        _nonempty(self.attribution, "attribution")
        if not isinstance(self.license, LicenseInfo):
            raise ValueError("license must be LicenseInfo")
        object.__setattr__(self, "access_mode", AccessMode(self.access_mode))
        object.__setattr__(self, "import_mode", ImportMode(self.import_mode))
        object.__setattr__(self, "publication_mode", PublicationMode(self.publication_mode))
        if not isinstance(self.generated, bool):
            raise ValueError("generated must be boolean")
        _nonempty(self.provenance_note, "provenance_note")

    @property
    def qualified_prefix(self) -> str:
        return _qualified_prefix(self.source_id, self.revision_id)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SourceRevision":
        _strict_fields(
            value,
            {
                "source_id",
                "revision_id",
                "content_sha256",
                "languages",
                "attribution",
                "license",
                "access_mode",
                "import_mode",
                "publication_mode",
                "generated",
                "provenance_note",
            },
            "source_revision",
        )
        return cls(
            source_id=value["source_id"],
            revision_id=value["revision_id"],
            content_sha256=value["content_sha256"],
            languages=tuple(value["languages"]),
            attribution=value["attribution"],
            license=LicenseInfo.from_dict(value["license"]),
            access_mode=value["access_mode"],
            import_mode=value["import_mode"],
            publication_mode=value["publication_mode"],
            generated=value["generated"],
            provenance_note=value["provenance_note"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "revision_id": self.revision_id,
            "content_sha256": self.content_sha256,
            "languages": list(self.languages),
            "attribution": self.attribution,
            "license": self.license.to_dict(),
            "access_mode": self.access_mode.value,
            "import_mode": self.import_mode.value,
            "publication_mode": self.publication_mode.value,
            "generated": self.generated,
            "provenance_note": self.provenance_note,
        }


@dataclass(frozen=True, slots=True)
class Provenance:
    source_id: str
    revision_id: str
    source_record_id: str
    locator: str
    content_sha256: str | None = None

    def __post_init__(self) -> None:
        if not self.source_record_id.startswith(_qualified_prefix(self.source_id, self.revision_id)):
            raise ValueError("source_record_id is not qualified by source_id and revision_id")
        _nonempty(self.locator, "locator")
        _sha256(self.content_sha256, "provenance content_sha256", optional=True)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Provenance":
        _strict_fields(
            value,
            {"source_id", "revision_id", "source_record_id", "locator", "content_sha256"},
            "provenance",
        )
        return cls(
            source_id=value["source_id"],
            revision_id=value["revision_id"],
            source_record_id=value["source_record_id"],
            locator=value["locator"],
            content_sha256=value.get("content_sha256"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "revision_id": self.revision_id,
            "source_record_id": self.source_record_id,
            "locator": self.locator,
            "content_sha256": self.content_sha256,
        }


@dataclass(frozen=True, slots=True)
class SourceRecord:
    source_record_id: str
    raw_expression: str
    order: int
    field_hashes: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _nonempty(self.source_record_id, "source_record_id")
        _nonempty(self.raw_expression, "raw_expression")
        if not isinstance(self.order, int) or isinstance(self.order, bool) or self.order < 0:
            raise ValueError("source record order must be a non-negative integer")
        hashes = dict(self.field_hashes)
        for name, digest in hashes.items():
            _nonempty(name, "field name")
            _sha256(digest, f"field hash {name}")
        object.__setattr__(
            self,
            "field_hashes",
            MappingProxyType(dict(sorted(hashes.items()))),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SourceRecord":
        _strict_fields(
            value,
            {"source_record_id", "raw_expression", "order", "field_hashes"},
            "source record",
        )
        return cls(
            source_record_id=value["source_record_id"],
            raw_expression=value["raw_expression"],
            order=value["order"],
            field_hashes=value.get("field_hashes", {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_record_id": self.source_record_id,
            "raw_expression": self.raw_expression,
            "order": self.order,
            "field_hashes": dict(self.field_hashes),
        }


@dataclass(frozen=True, slots=True)
class SourceLink:
    label: str
    url: str

    def __post_init__(self) -> None:
        _nonempty(self.label, "link label")
        if not isinstance(self.url, str):
            raise ValueError("source link URL must be a string")
        parsed = urlsplit(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("source link must use HTTP or HTTPS with a host")
        if parsed.username or parsed.password:
            raise ValueError("source link must not contain credentials")
        try:
            parsed.port
        except ValueError as error:
            raise ValueError("source link has an invalid port") from error

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SourceLink":
        _strict_fields(value, {"label", "url"}, "source link")
        return cls(value["label"], value["url"])

    def to_dict(self) -> dict[str, str]:
        return {"label": self.label, "url": self.url}


@dataclass(frozen=True, slots=True)
class ContentBlock:
    block_id: str
    kind: BlockKind
    language: str
    content: Any
    order: int
    provenance: Provenance
    generated: bool = False

    def __post_init__(self) -> None:
        _nonempty(self.block_id, "block_id")
        object.__setattr__(self, "kind", BlockKind(self.kind))
        if _LANGUAGE.fullmatch(self.language) is None:
            raise ValueError(f"invalid content language: {self.language}")
        object.__setattr__(self, "content", _jsonable(self.content))
        if not isinstance(self.order, int) or isinstance(self.order, bool) or self.order < 0:
            raise ValueError("block order must be a non-negative integer")
        if not isinstance(self.provenance, Provenance):
            raise ValueError("block provenance must be Provenance")
        if not isinstance(self.generated, bool):
            raise ValueError("block generated must be boolean")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ContentBlock":
        _strict_fields(
            value,
            {"block_id", "kind", "language", "content", "order", "provenance", "generated"},
            "content block",
        )
        return cls(
            block_id=value["block_id"],
            kind=value["kind"],
            language=value["language"],
            content=value["content"],
            order=value["order"],
            provenance=Provenance.from_dict(value["provenance"]),
            generated=value.get("generated", False),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_id": self.block_id,
            "kind": self.kind.value,
            "language": self.language,
            "content": _jsonable(self.content),
            "order": self.order,
            "provenance": self.provenance.to_dict(),
            "generated": self.generated,
        }


@dataclass(frozen=True, slots=True)
class ExamplePair:
    example_id: str
    japanese: Any
    translation: Any | None
    translation_language: str | None
    order: int
    provenance: Provenance
    generated: bool = False

    def __post_init__(self) -> None:
        _nonempty(self.example_id, "example_id")
        object.__setattr__(self, "japanese", _jsonable(self.japanese, "japanese example"))
        object.__setattr__(self, "translation", _jsonable(self.translation, "translation"))
        if (self.translation is None) != (self.translation_language is None):
            raise ValueError("translation and translation_language must be supplied together")
        if self.translation_language is not None and _LANGUAGE.fullmatch(self.translation_language) is None:
            raise ValueError("invalid translation language")
        if not isinstance(self.order, int) or isinstance(self.order, bool) or self.order < 0:
            raise ValueError("example order must be a non-negative integer")
        if not isinstance(self.provenance, Provenance):
            raise ValueError("example provenance must be Provenance")
        if not isinstance(self.generated, bool):
            raise ValueError("example generated must be boolean")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExamplePair":
        _strict_fields(
            value,
            {
                "example_id",
                "japanese",
                "translation",
                "translation_language",
                "order",
                "provenance",
                "generated",
            },
            "example pair",
        )
        return cls(
            example_id=value["example_id"],
            japanese=value["japanese"],
            translation=value.get("translation"),
            translation_language=value.get("translation_language"),
            order=value["order"],
            provenance=Provenance.from_dict(value["provenance"]),
            generated=value.get("generated", False),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "example_id": self.example_id,
            "japanese": _jsonable(self.japanese),
            "translation": _jsonable(self.translation),
            "translation_language": self.translation_language,
            "order": self.order,
            "provenance": self.provenance.to_dict(),
            "generated": self.generated,
        }


@dataclass(frozen=True, slots=True)
class MediaRecord:
    media_id: str
    source_path: str
    content_sha256: str
    media_type: str
    byte_count: int
    provenance: Provenance

    def __post_init__(self) -> None:
        _nonempty(self.media_id, "media_id")
        _nonempty(self.source_path, "source_path")
        if self.source_path.startswith(("/", "\\")) or ".." in self.source_path.replace("\\", "/").split("/"):
            raise ValueError("media source_path must be safe and relative")
        _sha256(self.content_sha256, "media content_sha256")
        if not self.media_type.startswith(("image/", "audio/")):
            raise ValueError("media_type must be an image or audio MIME type")
        if not isinstance(self.byte_count, int) or isinstance(self.byte_count, bool) or self.byte_count < 0:
            raise ValueError("media byte_count must be a non-negative integer")
        if not isinstance(self.provenance, Provenance):
            raise ValueError("media provenance must be Provenance")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MediaRecord":
        _strict_fields(
            value,
            {"media_id", "source_path", "content_sha256", "media_type", "byte_count", "provenance"},
            "media record",
        )
        return cls(
            media_id=value["media_id"],
            source_path=value["source_path"],
            content_sha256=value["content_sha256"],
            media_type=value["media_type"],
            byte_count=value["byte_count"],
            provenance=Provenance.from_dict(value["provenance"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "media_id": self.media_id,
            "source_path": self.source_path,
            "content_sha256": self.content_sha256,
            "media_type": self.media_type,
            "byte_count": self.byte_count,
            "provenance": self.provenance.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class SourceSense:
    source_sense_id: str
    source_record_id: str
    partition_status: PartitionStatus
    concept_ref: str | None = None
    blocks: tuple[ContentBlock, ...] = ()
    examples: tuple[ExamplePair, ...] = ()
    level_labels: tuple[str, ...] = ()
    register_labels: tuple[str, ...] = ()
    links: tuple[SourceLink, ...] = ()

    def __post_init__(self) -> None:
        if not self.source_sense_id.startswith(f"{self.source_record_id}:sense:"):
            raise ValueError("source_sense_id must be qualified by source_record_id")
        object.__setattr__(self, "partition_status", PartitionStatus(self.partition_status))
        if self.concept_ref is not None:
            _nonempty(self.concept_ref, "concept_ref")
        object.__setattr__(self, "blocks", tuple(self.blocks))
        object.__setattr__(self, "examples", tuple(self.examples))
        object.__setattr__(self, "level_labels", tuple(self.level_labels))
        object.__setattr__(self, "register_labels", tuple(self.register_labels))
        object.__setattr__(self, "links", tuple(self.links))
        for label in self.level_labels + self.register_labels:
            _nonempty(label, "source-scoped label")
        if len(set(self.level_labels)) != len(self.level_labels) or len(set(self.register_labels)) != len(self.register_labels):
            raise ValueError("source-scoped labels must be unique")
        for block in self.blocks:
            if block.provenance.source_record_id != self.source_record_id:
                raise ValueError("block provenance points to another source record")
        for example in self.examples:
            if example.provenance.source_record_id != self.source_record_id:
                raise ValueError("example provenance points to another source record")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SourceSense":
        _strict_fields(
            value,
            {
                "source_sense_id",
                "source_record_id",
                "partition_status",
                "concept_ref",
                "blocks",
                "examples",
                "level_labels",
                "register_labels",
                "links",
            },
            "source sense",
        )
        return cls(
            source_sense_id=value["source_sense_id"],
            source_record_id=value["source_record_id"],
            partition_status=value["partition_status"],
            concept_ref=value.get("concept_ref"),
            blocks=tuple(ContentBlock.from_dict(item) for item in value.get("blocks", [])),
            examples=tuple(ExamplePair.from_dict(item) for item in value.get("examples", [])),
            level_labels=tuple(value.get("level_labels", [])),
            register_labels=tuple(value.get("register_labels", [])),
            links=tuple(SourceLink.from_dict(item) for item in value.get("links", [])),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_sense_id": self.source_sense_id,
            "source_record_id": self.source_record_id,
            "partition_status": self.partition_status.value,
            "concept_ref": self.concept_ref,
            "blocks": [item.to_dict() for item in sorted(self.blocks, key=lambda item: (item.order, item.block_id))],
            "examples": [item.to_dict() for item in sorted(self.examples, key=lambda item: (item.order, item.example_id))],
            "level_labels": list(self.level_labels),
            "register_labels": list(self.register_labels),
            "links": [item.to_dict() for item in self.links],
        }


@dataclass(frozen=True, slots=True)
class CanonicalSense:
    sense_id: str
    display_expression: str
    source_sense_ids: tuple[str, ...]
    concept_id: str | None = None
    ambiguity: str = "resolved"

    def __post_init__(self) -> None:
        _nonempty(self.sense_id, "sense_id")
        _nonempty(self.display_expression, "display_expression")
        members = tuple(self.source_sense_ids)
        if not members or len(set(members)) != len(members):
            raise ValueError("source_sense_ids must be non-empty and unique")
        object.__setattr__(self, "source_sense_ids", members)
        if self.concept_id is not None:
            _nonempty(self.concept_id, "concept_id")
        if self.ambiguity not in {"resolved", "needs-review"}:
            raise ValueError("ambiguity must be resolved or needs-review")


@dataclass(frozen=True, slots=True)
class SourceBundle:
    source_revision: SourceRevision
    records: tuple[SourceRecord, ...]
    senses: tuple[SourceSense, ...]
    media: tuple[MediaRecord, ...]

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SourceBundle":
        _strict_fields(
            value,
            {"format", "format_version", "source_revision", "records", "senses", "media"},
            "canonical source",
        )
        format_version = value.get("format_version")
        if (
            value.get("format") != "ugd-canonical-source"
            or type(format_version) is not int
            or format_version != MODEL_FORMAT_VERSION
        ):
            raise ValueError("unsupported canonical source format or version")
        bundle = cls(
            source_revision=SourceRevision.from_dict(value["source_revision"]),
            records=tuple(SourceRecord.from_dict(item) for item in value.get("records", [])),
            senses=tuple(SourceSense.from_dict(item) for item in value.get("senses", [])),
            media=tuple(MediaRecord.from_dict(item) for item in value.get("media", [])),
        )
        bundle.validate()
        return bundle

    def validate(self) -> None:
        record_ids = [record.source_record_id for record in self.records]
        if len(record_ids) != len(set(record_ids)):
            raise ValueError("duplicate source_record_id")
        for record_id in record_ids:
            if not record_id.startswith(self.source_revision.qualified_prefix):
                raise ValueError("source_record_id belongs to another source revision")
        sense_ids = [sense.source_sense_id for sense in self.senses]
        if len(sense_ids) != len(set(sense_ids)):
            raise ValueError("duplicate source_sense_id")
        known_records = set(record_ids)
        for sense in self.senses:
            if sense.source_record_id not in known_records:
                raise ValueError("source sense references an unknown source record")
            for item in (*sense.blocks, *sense.examples):
                provenance = item.provenance
                if (provenance.source_id, provenance.revision_id) != (
                    self.source_revision.source_id,
                    self.source_revision.revision_id,
                ):
                    raise ValueError("content provenance belongs to another source revision")
        block_ids = [block.block_id for sense in self.senses for block in sense.blocks]
        if len(block_ids) != len(set(block_ids)):
            raise ValueError("duplicate block_id")
        example_ids = [example.example_id for sense in self.senses for example in sense.examples]
        if len(example_ids) != len(set(example_ids)):
            raise ValueError("duplicate example_id")
        media_ids = [item.media_id for item in self.media]
        if len(media_ids) != len(set(media_ids)):
            raise ValueError("duplicate media_id")
        for item in self.media:
            if item.provenance.source_record_id not in known_records:
                raise ValueError("media references an unknown source record")
            if (item.provenance.source_id, item.provenance.revision_id) != (
                self.source_revision.source_id,
                self.source_revision.revision_id,
            ):
                raise ValueError("media provenance belongs to another source revision")

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": "ugd-canonical-source",
            "format_version": MODEL_FORMAT_VERSION,
            "source_revision": self.source_revision.to_dict(),
            "records": [item.to_dict() for item in sorted(self.records, key=lambda item: (item.order, item.source_record_id))],
            "senses": [item.to_dict() for item in sorted(self.senses, key=lambda item: item.source_sense_id)],
            "media": [item.to_dict() for item in sorted(self.media, key=lambda item: item.media_id)],
        }


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize a validated JSON value deterministically."""
    return (json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")

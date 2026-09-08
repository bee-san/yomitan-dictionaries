"""Public-safe IMABI metadata references approved by the UGD-04 audit."""

from __future__ import annotations

from dataclasses import dataclass
import unicodedata
from urllib.parse import urlsplit

from ..model import canonical_json_bytes


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


__all__ = [
    "ImabiLessonReference",
    "ImabiReferenceError",
    "audited_reference_index",
    "audited_reference_json",
    "source_lesson_id",
]
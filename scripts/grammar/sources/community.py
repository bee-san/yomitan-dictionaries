"""Vetted community inputs, Yokubi mapping, and per-source outcomes."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import stat
from types import MappingProxyType
from typing import Any, Mapping

from ..model import (
    BlockKind,
    ContentBlock,
    ExamplePair,
    LicenseInfo,
    PartitionStatus,
    Provenance,
    SourceBundle,
    SourceLink,
    SourceRecord,
    SourceRevision,
    SourceSense,
)
from ..registry import get_source
from .yomitan import (
    MAX_ARCHIVE_BYTES,
    AdapterError,
    YomitanAdaptation,
    adapt_yomitan_archive,
)


PINNED_YOKUBI_REVISION = "b1c0938b0bda58e20c6ccd21288b46711b438239"
PINNED_YOKUBI_CONTENT_SHA256 = (
    "dc77d2fd54623cae7dbc9ecab52369c2d83d440c33827933deefdb1b2f14909b"
)
_JAPANESE_SCRIPT = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")

# Yokubi has no subheadings beneath each lesson. These source-backed mappings
# deliberately keep one source sense per lesson and list only grammar concepts
# explicitly named in the lesson title. Empty tuples are reviewed skips rather
# than invented grammar-point boundaries.
YOKUBI_LESSON_CONCEPTS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "Section1/Part1/Lesson0.md": (),
        "Section1/Part1/Lesson1.md": ("だ", "です"),
        "Section1/Part1/Lesson2.md": (),
        "Section1/Part1/Lesson3.md": (),
        "Section1/Part1/Lesson4.md": (),
        "Section1/Part1/Lesson5.md": ("の",),
        "Section1/Part1/Lesson6.md": ("に", "へ", "から"),
        "Section1/Part1/Lesson7.md": (),
        "Section1/Part1/Lesson8.md": ("〜い",),
        "Section1/Part1/Lesson9.md": (),
        "Section1/Part1/Lesson10.md": ("〜て",),
        "Section1/Part1/Lesson11.md": ("〜て",),
        "Section1/Part1/Lesson12.md": ("〜てください",),
        "Section1/Part1/Lesson13.md": ("で", "では", "じゃ"),
        "Section1/Part1/Lesson14.md": (),
        "Section1/Part1/Lesson15.md": ("〜な",),
        "Section1/Part1/Lesson16.md": ("する",),
        "Section1/Part1/Lesson17.md": ("〜ます",),
        "Section1/Part1/Lesson18.md": ("いる", "ある", "である"),
        "Section1/Part2/Lesson19.md": ("か",),
        "Section1/Part2/Lesson20.md": ("の", "のだ"),
        "Section1/Part2/Lesson21.md": ("も", "と"),
        "Section1/Part2/Lesson22.md": ("ている", "てある"),
        "Section1/Part2/Lesson23.md": ("こそあど",),
        "Section1/Part2/Lesson24.md": (),
        "Section1/Part2/Lesson25.md": ("できる",),
        "Section1/Part2/Lesson26.md": ("たい", "ほしい"),
        "Section1/Part2/Lesson27.md": (),
        "Section1/Part2/Lesson28.md": (),
        "Section2/Part3/Lesson29.md": ("ね", "な", "よ", "ぞ", "ぜ", "わ"),
        "Section2/Part3/Lesson30.md": ("と", "って", "という"),
        "Section2/Part3/Lesson31.md": (),
        "Section2/Part3/Lesson32.md": ("なさい", "な"),
        "Section2/Part3/Lesson33.md": ("なに", "だれ", "どれ", "いつ"),
        "Section2/Part3/Lesson34.md": ("か", "も", "でも"),
        "Section2/Part3/Lesson35.md": ("事", "物", "ところ", "の"),
        "Section2/Part3/Lesson36.md": ("や", "とか", "など", "と", "か", "に"),
        "Section2/Part3/Lesson37.md": ("が", "けど", "しかし", "ても", "でも"),
        "Section2/Part3/Lesson38.md": ("ながら", "あいだ", "うちに", "つつ"),
        "Section2/Part3/Lesson39.md": ("から", "そして", "ので", "で"),
        "Section2/Part3/Lesson40.md": ("より", "の方が"),
        "Section2/Part3/Lesson41.md": (),
        "Section2/Part3/Lesson42.md": (
            "のに",
            "ように",
            "ために",
            "せいで",
            "おかげで",
        ),
        "Section2/Part3/Lesson43.md": ("みたい", "らしい", "ぽい", "そう"),
        "Section2/Part3/Lesson44.md": ("だめ", "いけない", "ならない"),
        "Section2/Part4/Lesson45.md": ("〜たり〜たり", "ては"),
        "Section2/Part4/Lesson46.md": ("だけ", "のみ", "ばかり", "しか"),
        "Section2/Part4/Lesson47.md": ("もう", "まだ", "また"),
        "Section2/Part4/Lesson48.md": ("いい", "ませんか"),
        "Section2/Part4/Lesson49.md": ("とする",),
        "Section2/Part4/Lesson50.md": ("てみる",),
        "Section2/Part4/Lesson51.md": ("と思う", "と考える"),
        "Section2/Part4/Lesson52.md": (
            "っけ",
            "かな",
            "かい",
            "だい",
            "じゃない",
            "じゃん",
        ),
        "Section2/Part4/Lesson53.md": (),
        "Section2/Part4/Lesson54.md": ("なる", "する"),
        "Section2/Part4/Lesson55.md": ("自分",),
        "Section2/Part4/Lesson56.md": ("なくて", "ないで", "ず", "ずに"),
        "Section2/Part4/Lesson57.md": ("てしまう", "ておく"),
        "Section2/Part4/Lesson58.md": ("ていく", "てくる"),
        "Section2/Part4/Lesson59.md": ("だって",),
        "Section2/Part4/Lesson60.md": (
            "わけ",
            "はず",
            "べき",
            "ものだ",
            "かもしれない",
        ),
        "Section2/Part4/Lesson61.md": ("ころ", "くらい", "まで", "ほど", "すぎる"),
        "Section2/Part4/Lesson62.md": ("後", "前", "先", "時"),
        "Section2/Part4/Lesson63.md": (),
    }
)


@dataclass(frozen=True, slots=True)
class PrivateArchiveSpec:
    source_id: str
    filename: str
    expected_rows: int
    expected_revision: str
    evidence_url: str


PRIVATE_ARCHIVES = (
    PrivateArchiveSpec(
        "nihongo-kyoshi",
        "日本語NET(nihongo_kyoushi)_v1_03.zip",
        170,
        "nihongo_kyoshi_v1.03; 2022-05-27; p.o.s. info",
        "https://nihongokyoshi-net.com/jlpt-grammars/",
    ),
    PrivateArchiveSpec(
        "donna-toki",
        "どんなときどう使う 日本語表現文型辞典_1_05.zip",
        1082,
        "donna_v1.04;2022-04-30(completed arrow internal links)",
        "https://itazuraneko.neocities.org/grammar/donnatoki.html",
    ),
    PrivateArchiveSpec(
        "e-de-wakaru",
        "edewakaru_v_1_03.zip",
        309,
        "edewakaru_v1.03; 2022-09-01",
        "https://www.edewakaru.com/archives/cat_179055.html",
    ),
    PrivateArchiveSpec(
        "dojg",
        "dojg-consolidated-v1_01.zip",
        535,
        "DOJG_v1.01;2022-04-30;better formatting",
        "https://bookclub.japantimes.co.jp/jp/book/b309577.html",
    ),
    PrivateArchiveSpec(
        "nihongo-no-sensei",
        "nihongo_no_sensei_1_04.zip",
        478,
        "nihongo_no_sensei_v_1.04;2022-07-03;embedded urls, p of speech indicators(N5-N0)",
        "https://nihongonosensei.net/?page_id=10246",
    ),
)


@dataclass(frozen=True, slots=True)
class SourceOutcome:
    source_id: str
    status: str
    content_count: int
    expected_count: int | None
    evidence_url: str
    reason: str
    revision_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "status": self.status,
            "content_count": self.content_count,
            "expected_count": self.expected_count,
            "evidence_url": self.evidence_url,
            "reason": self.reason,
            "revision_id": self.revision_id,
        }


@dataclass(frozen=True, slots=True)
class CommunityAdaptation:
    bundles: tuple[SourceBundle, ...]
    outcomes: tuple[SourceOutcome, ...]
    adapter_reports: tuple[Mapping[str, Any], ...]
    media_bytes: Mapping[str, bytes]

    def outcome_by_source(self) -> dict[str, SourceOutcome]:
        return {item.source_id: item for item in self.outcomes}

    def report(self) -> dict[str, Any]:
        counts = Counter(item.status for item in self.outcomes)
        return {
            "format": "ugd-community-coverage-report",
            "format_version": 1,
            "status_counts": dict(sorted(counts.items())),
            "sources": [item.to_dict() for item in self.outcomes],
            "adapter_reports": [dict(item) for item in self.adapter_reports],
        }


@dataclass(frozen=True, slots=True)
class YokubiAdaptation:
    bundle: SourceBundle
    report: Mapping[str, Any]
    media_bytes: Mapping[str, bytes]


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _tree_digest(root: Path, paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _git_head(root: Path) -> str | None:
    head = root / ".git/HEAD"
    if not head.is_file():
        return None
    value = head.read_text(encoding="utf-8").strip()
    if value.startswith("ref: "):
        reference = root / ".git" / value[5:]
        if not reference.is_file():
            return None
        value = reference.read_text(encoding="utf-8").strip()
    return value if re.fullmatch(r"[0-9a-f]{40}", value) else None


def _lesson_number(path: str) -> int:
    match = re.search(r"/Lesson([0-9]+)\.md$", path)
    if match is None:
        raise AdapterError(f"invalid Yokubi lesson path: {path}")
    return int(match.group(1))


def _formation_paragraph(paragraph: str) -> bool:
    plain = re.sub(r"<[^>]+>", "", paragraph).lower()
    markers = (
        " is made by ",
        " form is ",
        " form of ",
        "to make ",
        "made by ",
        "by adding ",
        "we add ",
        "you add ",
        "attach",
        "conjugat",
        "turning the ",
        "becomes ",
        "followed by ",
    )
    padded = f" {plain} "
    return any(marker in padded for marker in markers)


def _lesson_parts(
    text: str,
) -> tuple[
    list[tuple[BlockKind, str, str]],
    list[tuple[str, str | None, str, str]],
]:
    blocks: list[tuple[BlockKind, str, str]] = []
    examples: list[tuple[str, str | None, str, str]] = []
    pieces = re.split(r"(<pre\b[^>]*>.*?</pre>)", text, flags=re.IGNORECASE | re.DOTALL)
    pre_number = 0
    paragraph_number = 0
    for piece in pieces:
        if not piece:
            continue
        if re.fullmatch(
            r"<pre\b[^>]*>.*?</pre>", piece, flags=re.IGNORECASE | re.DOTALL
        ):
            pre_number += 1
            inner = re.sub(
                r"^<pre\b[^>]*>|</pre>$", "", piece, flags=re.IGNORECASE | re.DOTALL
            )
            groups = [
                group.strip("\r\n")
                for group in re.split(r"\n\s*\n", inner)
                if group.strip()
            ]
            for group_number, group in enumerate(groups, 1):
                lines = [line.strip() for line in group.splitlines() if line.strip()]
                if not lines:
                    continue
                locator = f"pre:{pre_number}:group:{group_number}"
                if (
                    len(lines) == 2
                    and not any("\t" in line for line in lines)
                    and _JAPANESE_SCRIPT.search(lines[0])
                    and re.search(r"[A-Za-z]", lines[1])
                ):
                    examples.append((lines[0], lines[1], locator, group))
                else:
                    blocks.append(
                        (
                            BlockKind.OTHER,
                            group,
                            locator,
                        )
                    )
            continue
        for paragraph in re.split(r"\n\s*\n", piece):
            paragraph = paragraph.strip()
            if not paragraph:
                continue
            paragraph_number += 1
            kind = (
                BlockKind.FORMATION
                if _formation_paragraph(paragraph)
                else BlockKind.MEANING
            )
            blocks.append((kind, paragraph, f"paragraph:{paragraph_number}"))
    return blocks, examples


def _summary_lessons(summary: str) -> set[str]:
    result = set()
    for target in re.findall(r"\((?:\./)?([^)]*Lesson[0-9]+\.md)\)", summary):
        result.add(Path(target).as_posix())
    return result


def adapt_yokubi_tree(
    root: str | Path,
    *,
    revision_id: str = PINNED_YOKUBI_REVISION,
    lesson_concepts: Mapping[str, tuple[str, ...]] | None = None,
    expected_content_sha256: str | None = None,
) -> YokubiAdaptation:
    """Adapt a pinned Yokubi checkout at its real lesson boundaries."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise AdapterError("Yokubi root must be a non-symbolic-link directory")
    required = [
        root / "LICENSE",
        root / "book.toml",
        root / "src/SUMMARY.md",
        root / "src/Credits.md",
    ]
    missing = [
        path.relative_to(root).as_posix() for path in required if not path.is_file()
    ]
    if missing:
        raise AdapterError(f"Yokubi checkout is missing required files: {missing}")
    license_text = (root / "LICENSE").read_text(encoding="utf-8")
    credits_text = (root / "src/Credits.md").read_text(encoding="utf-8")
    if (
        "Attribution 4.0 International" not in license_text
        or "Attribution 4.0" not in credits_text
    ):
        raise AdapterError("Yokubi CC BY 4.0 notices are missing or changed")
    actual_head = _git_head(root)
    if actual_head is not None and actual_head != revision_id:
        raise AdapterError(
            f"Yokubi checkout revision mismatch: expected {revision_id}, got {actual_head}"
        )

    use_pinned_mapping = lesson_concepts is None
    mapping = dict(lesson_concepts or YOKUBI_LESSON_CONCEPTS)
    src = root / "src"
    lessons = {
        path.relative_to(src).as_posix(): path
        for path in src.glob("Section*/Part*/Lesson*.md")
        if path.is_file()
    }
    summary_lessons = _summary_lessons((src / "SUMMARY.md").read_text(encoding="utf-8"))
    if set(mapping) != set(lessons) or set(mapping) != summary_lessons:
        raise AdapterError(
            "Yokubi lesson coverage mismatch between source tree, SUMMARY.md, and reviewed mapping"
        )

    source_files = [root / "LICENSE", root / "book.toml", *src.rglob("*.md")]
    content_sha256 = _tree_digest(root, source_files)
    expected_digest = (
        PINNED_YOKUBI_CONTENT_SHA256
        if use_pinned_mapping and expected_content_sha256 is None
        else expected_content_sha256
    )
    if expected_digest is not None and content_sha256 != expected_digest:
        raise AdapterError(
            "Yokubi accepted content SHA-256 mismatch: "
            f"expected {expected_digest}, got {content_sha256}"
        )
    definition = get_source("yokubi")
    records: list[SourceRecord] = []
    senses: list[SourceSense] = []
    concept_map: list[dict[str, Any]] = []
    formation_blocks = 0
    example_count = 0
    imported_paths: set[str] = set()
    for source_path in sorted(mapping, key=lambda path: (_lesson_number(path), path)):
        concepts = tuple(mapping[source_path])
        if not concepts:
            continue
        if not all(
            isinstance(concept, str) and concept.strip() for concept in concepts
        ):
            raise AdapterError(
                f"Yokubi concepts must be non-empty strings: {source_path}"
            )
        if len(set(concepts)) != len(concepts):
            raise AdapterError(
                f"Yokubi concepts must be unique within a lesson: {source_path}"
            )
        path = lessons[source_path]
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        title_match = re.match(r"^# ([^\n]+)\n", text)
        if title_match is None:
            raise AdapterError(f"Yokubi lesson has no H1 title: {source_path}")
        lesson_number = _lesson_number(source_path)
        record_id = f"yokubi:{revision_id}:record:lesson-{lesson_number:02d}"
        sense_id = f"{record_id}:sense:lesson"
        source_url = (
            f"https://github.com/Morgawr/yokubi/blob/{revision_id}/src/{source_path}"
        )
        block_specs, example_specs = _lesson_parts(text)
        provenance_base = {
            "source_id": "yokubi",
            "revision_id": revision_id,
            "source_record_id": record_id,
        }
        blocks = []
        for block_order, (kind, content, locator) in enumerate(block_specs):
            provenance = Provenance(
                **provenance_base,
                locator=f"src/{source_path}#{locator}",
                content_sha256=_sha256(content.encode("utf-8")),
            )
            if kind is BlockKind.FORMATION:
                formation_blocks += 1
            blocks.append(
                ContentBlock(
                    block_id=f"{sense_id}:block:{block_order + 1:04d}",
                    kind=kind,
                    language="en",
                    content=content,
                    order=block_order,
                    provenance=provenance,
                )
            )
        examples = []
        for example_order, (japanese, translation, locator, raw_group) in enumerate(
            example_specs
        ):
            provenance = Provenance(
                **provenance_base,
                locator=f"src/{source_path}#{locator}",
                content_sha256=_sha256(raw_group.encode("utf-8")),
            )
            examples.append(
                ExamplePair(
                    example_id=f"{sense_id}:example:{example_order + 1:04d}",
                    japanese=japanese,
                    translation=translation,
                    translation_language="en" if translation is not None else None,
                    order=example_order,
                    provenance=provenance,
                )
            )
        example_count += len(examples)
        records.append(
            SourceRecord(
                source_record_id=record_id,
                raw_expression=" / ".join(concepts),
                order=lesson_number,
                field_hashes={"markdown": _sha256(raw)},
            )
        )
        senses.append(
            SourceSense(
                source_sense_id=sense_id,
                source_record_id=record_id,
                partition_status=PartitionStatus.SOURCE_GROUPED,
                concept_ref=f"yokubi:lesson:{lesson_number}",
                blocks=tuple(blocks),
                examples=tuple(examples),
                links=(SourceLink("Pinned Yokubi lesson", source_url),),
            )
        )
        concept_map.append(
            {
                "source_path": source_path,
                "title": title_match.group(1),
                "source_record_id": record_id,
                "source_sense_id": sense_id,
                "concepts": list(concepts),
                "boundary": "source lesson; concepts are not split into invented subsections",
            }
        )
        imported_paths.add(source_path)

    chapters = []
    for path in sorted(src.rglob("*.md")):
        source_path = path.relative_to(src).as_posix()
        if source_path in imported_paths:
            item = {
                "path": source_path,
                "status": "imported",
                "reason": "reviewed lesson title maps one or more explicit grammar concepts",
                "concepts": list(mapping[source_path]),
            }
        elif source_path in mapping:
            item = {
                "path": source_path,
                "status": "skipped",
                "reason": "no explicit dictionary grammar point can be mapped without inventing a subsection boundary",
                "concepts": [],
            }
        else:
            item = {
                "path": source_path,
                "status": "not-applicable",
                "reason": "navigation, editorial, section overview, credits, or other non-lesson material",
                "concepts": [],
            }
        chapters.append(item)
    status_counts = Counter(item["status"] for item in chapters)
    revision = SourceRevision(
        source_id="yokubi",
        revision_id=revision_id,
        content_sha256=content_sha256,
        languages=("en", "ja"),
        attribution=definition.attribution,
        license=LicenseInfo(
            "CC-BY-4.0",
            "Yokubi is CC BY 4.0; preserve Morgawr/contributor attribution and credited Sakubi origins.",
            f"https://github.com/Morgawr/yokubi/blob/{revision_id}/LICENSE",
        ),
        access_mode=definition.access_mode,
        import_mode=definition.import_mode,
        publication_mode=definition.publication_mode,
        generated=False,
        provenance_note=(
            f"Pinned Yokubi commit {revision_id}; accepted source-tree SHA-256 {content_sha256}. "
            "This is an unfinished rewrite. Lessons remain source-grouped because the files contain "
            "no grammar-point subheadings."
        ),
    )
    bundle = SourceBundle(revision, tuple(records), tuple(senses), ())
    bundle.validate()
    report = {
        "format": "ugd-yokubi-adapter-report",
        "format_version": 1,
        "source_id": "yokubi",
        "revision_id": revision_id,
        "source_content_sha256": content_sha256,
        "expected_source_content_sha256": expected_digest,
        "unfinished_snapshot": True,
        "source_file_count": len(source_files),
        "lesson_count": len(lessons),
        "imported_lessons": len(records),
        "skipped_lessons": len(lessons) - len(records),
        "mapped_concepts": sum(len(concepts) for concepts in mapping.values()),
        "formation_blocks": formation_blocks,
        "examples": example_count,
        "source_record_ids": [record.source_record_id for record in records],
        "concept_map": concept_map,
        "chapter_status_counts": dict(sorted(status_counts.items())),
        "chapters": chapters,
        "attribution": definition.attribution,
        "license": "CC-BY-4.0",
    }
    return YokubiAdaptation(bundle, MappingProxyType(report), MappingProxyType({}))


def find_duplicate_archives(inputs: Mapping[str, str | Path]) -> dict[str, str]:
    """Return later source IDs whose exact ZIP bytes duplicate an earlier input."""
    first_by_digest: dict[str, str] = {}
    duplicates: dict[str, str] = {}
    for source_id, raw_path in inputs.items():
        path = Path(raw_path)
        try:
            metadata = path.lstat()
        except OSError as error:
            raise AdapterError(
                f"community input for {source_id} is not a regular file"
            ) from error
        if not stat.S_ISREG(metadata.st_mode):
            raise AdapterError(f"community input for {source_id} is not a regular file")
        if metadata.st_size > MAX_ARCHIVE_BYTES:
            raise AdapterError(
                f"community input for {source_id} exceeds {MAX_ARCHIVE_BYTES} byte limit"
            )
        digest_builder = hashlib.sha256()
        total = 0
        try:
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    total += len(chunk)
                    if total > MAX_ARCHIVE_BYTES:
                        raise AdapterError(
                            f"community input for {source_id} exceeds {MAX_ARCHIVE_BYTES} byte limit"
                        )
                    digest_builder.update(chunk)
        except OSError as error:
            raise AdapterError(
                f"cannot read community input for {source_id}: {error}"
            ) from error
        digest = digest_builder.hexdigest()
        if digest in first_by_digest:
            duplicates[source_id] = first_by_digest[digest]
        else:
            first_by_digest[digest] = source_id
    return duplicates


def _static_outcomes() -> tuple[SourceOutcome, ...]:
    return (
        SourceOutcome(
            "tae-kim",
            "link-only",
            0,
            None,
            "https://guidetojapanese.org/learn/",
            "No maintained Yomitan archive or open content licence was selected; preserve the canonical link only.",
        ),
        SourceOutcome(
            "jlpt-sensei",
            "link-only",
            0,
            None,
            "https://jlptsensei.com/",
            "No standalone permitted dictionary export was selected; member-gated lesson content is not acquired.",
        ),
        SourceOutcome(
            "maggie-sensei",
            "link-only",
            0,
            None,
            "https://maggiesensei.com/",
            "No standalone permitted dictionary export or open content licence was selected.",
        ),
        SourceOutcome(
            "japanese-wikibooks",
            "excluded",
            0,
            None,
            "https://ja.wikibooks.org/wiki/日本語",
            "CC BY-SA 4.0 candidate, but deliberately excluded as broad textbook-like content for this bounded batch.",
        ),
        SourceOutcome(
            "nihongo-kyoshi-n1et",
            "excluded",
            0,
            None,
            "https://jn1et.com/jlpt/",
            "Backlink-only discovery; Donna Toki-derived level labels are not independent coverage.",
        ),
        SourceOutcome(
            "nihon5-bunka",
            "excluded",
            0,
            None,
            "https://nihon5-bunka.net/japanese-grammars/",
            "General grammar index with no selected open content licence or dictionary archive.",
        ),
        SourceOutcome(
            "japanese-bank",
            "excluded",
            0,
            None,
            "https://japanese-bank.com/jlpt-grammar-all/",
            "General grammar list with no selected open content licence or dictionary archive.",
        ),
        SourceOutcome(
            "ninjal-bunkei",
            "excluded",
            0,
            800,
            "https://doi.org/10.15084/0002000610",
            "Selected open source is implemented by the dedicated NINJAL adapter card, not duplicated here.",
        ),
    )


def adapt_community_sources(
    local_inputs: Mapping[str, str | Path] | None = None,
    *,
    archive_digest_pins: Mapping[str, str] | None = None,
    yokubi_root: str | Path | None = None,
    yokubi_revision: str = PINNED_YOKUBI_REVISION,
) -> CommunityAdaptation:
    """Adapt available selected inputs and account for every reviewed candidate."""
    local_inputs = dict(local_inputs or {})
    archive_digest_pins = dict(archive_digest_pins or {})
    selected_ids = {spec.source_id for spec in PRIVATE_ARCHIVES}
    unknown = (set(local_inputs) | set(archive_digest_pins)) - selected_ids
    if unknown:
        raise AdapterError(
            f"unselected community source inputs are refused: {sorted(unknown)}"
        )
    duplicates = find_duplicate_archives(local_inputs)
    bundles: list[SourceBundle] = []
    outcomes: list[SourceOutcome] = []
    reports: list[Mapping[str, Any]] = []
    media: dict[str, bytes] = {}
    for spec in PRIVATE_ARCHIVES:
        raw_path = local_inputs.get(spec.source_id)
        if raw_path is None:
            outcomes.append(
                SourceOutcome(
                    spec.source_id,
                    "unavailable",
                    0,
                    spec.expected_rows,
                    spec.evidence_url,
                    f"Requires a lawful local input named {spec.filename}; no download or crawl is attempted.",
                )
            )
            continue
        digest_pin = archive_digest_pins.get(spec.source_id)
        if digest_pin is None:
            raise AdapterError(
                f"{spec.source_id} available private archive requires an external digest pin"
            )
        if spec.source_id in duplicates:
            original = duplicates[spec.source_id]
            outcomes.append(
                SourceOutcome(
                    spec.source_id,
                    "excluded",
                    0,
                    spec.expected_rows,
                    spec.evidence_url,
                    f"Exact archive-byte duplicate of {original}; excluded as a duplicate mirror.",
                )
            )
            continue
        adapted: YomitanAdaptation = adapt_yomitan_archive(
            raw_path,
            spec.source_id,
            expected_archive_sha256=digest_pin,
            expected_rows=spec.expected_rows,
            expected_revision=spec.expected_revision,
        )
        bundles.append(adapted.bundle)
        reports.append(adapted.report)
        for target, content in adapted.media_bytes.items():
            if target in media and media[target] != content:
                raise AdapterError(f"media namespace collision: {target}")
            media[target] = content
        outcomes.append(
            SourceOutcome(
                spec.source_id,
                "imported",
                len(adapted.bundle.records),
                spec.expected_rows,
                spec.evidence_url,
                "Every validated term-bank row was imported from the local private archive; publication remains denied.",
                adapted.bundle.source_revision.revision_id,
            )
        )

    if yokubi_root is None:
        outcomes.append(
            SourceOutcome(
                "yokubi",
                "unavailable",
                0,
                len(YOKUBI_LESSON_CONCEPTS),
                "https://github.com/Morgawr/yokubi",
                f"Requires the pinned public checkout at commit {PINNED_YOKUBI_REVISION}; no implicit moving-main import is used.",
            )
        )
    else:
        adapted_yokubi = adapt_yokubi_tree(yokubi_root, revision_id=yokubi_revision)
        bundles.append(adapted_yokubi.bundle)
        reports.append(adapted_yokubi.report)
        outcomes.append(
            SourceOutcome(
                "yokubi",
                "imported",
                len(adapted_yokubi.bundle.records),
                len(YOKUBI_LESSON_CONCEPTS),
                "https://github.com/Morgawr/yokubi",
                "Pinned CC BY 4.0 unfinished snapshot imported at reviewed lesson boundaries; skipped/editorial files remain explicit in its report.",
                adapted_yokubi.bundle.source_revision.revision_id,
            )
        )

    outcomes.extend(_static_outcomes())
    return CommunityAdaptation(
        bundles=tuple(bundles),
        outcomes=tuple(outcomes),
        adapter_reports=tuple(reports),
        media_bytes=MappingProxyType(media),
    )

"""Community adapter regressions use synthetic content only."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import warnings
import zipfile

from scripts.grammar.sources.community import (
    PINNED_YOKUBI_REVISION,
    YOKUBI_LESSON_CONCEPTS,
    adapt_community_sources,
    adapt_yokubi_tree,
    find_duplicate_archives,
)
from scripts.grammar.sources.yomitan import AdapterError, adapt_yomitan_archive


INDEX_REVISION = "synthetic-v1"


def _write_yomitan(
    path: Path,
    *,
    title: str = "Synthetic grammar",
    revision: str = INDEX_REVISION,
    term: str = "〜synthetic",
    second_term: str | None = None,
    media: bytes | None = None,
    malformed_row: bool = False,
    directory_member: bool = False,
) -> Path:
    glossary: list[object] = ["Synthetic explanation"]
    if media is not None:
        glossary.append(
            {
                "type": "structured-content",
                "content": {"tag": "img", "path": "chart.png"},
            }
        )
    row: list[object] = [term, "", "N3", "", 0, glossary, 7, ""]
    if malformed_row:
        row.pop()
    rows = [row]
    if second_term is not None:
        second_row = list(row)
        second_row[0] = second_term
        rows.append(second_row)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "index.json",
            json.dumps(
                {
                    "title": title,
                    "revision": revision,
                    "format": 3,
                    "sequenced": True,
                    "attribution": "Synthetic fixture attribution",
                }
            ),
        )
        archive.writestr("term_bank_1.json", json.dumps(rows))
        if directory_member:
            archive.writestr("assets/", b"")
        if media is not None:
            archive.writestr("chart.png", media)
    return path


def _adapt(path: Path, source_id: str, **kwargs: object):
    return adapt_yomitan_archive(
        path,
        source_id,
        expected_archive_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        **kwargs,
    )


def _write_yokubi(root: Path) -> dict[str, tuple[str, ...]]:
    (root / "src/Section1/Part1").mkdir(parents=True)
    (root / "LICENSE").write_text("Attribution 4.0 International\n", encoding="utf-8")
    (root / "book.toml").write_text(
        '[book]\nauthors=["Morg"]\ntitle="Yokubi"\n', encoding="utf-8"
    )
    (root / "src/Credits.md").write_text(
        "# Credits\n\nYokubi revises Sakubi. Licensed under Creative Commons By-Attribution 4.0.\n",
        encoding="utf-8",
    )
    lessons = {
        "Section1/Part1/Lesson1.md": ("だ", "です"),
        "Section1/Part1/Lesson2.md": (),
    }
    (root / "src/SUMMARY.md").write_text(
        "# Summary\n\n- [Lesson 1](./Section1/Part1/Lesson1.md)\n"
        "- [Lesson 2](./Section1/Part1/Lesson2.md)\n",
        encoding="utf-8",
    )
    (root / "src/Section1/Part1/Lesson1.md").write_text(
        "# State of being with だ and です\n\n"
        "The form is made by adding だ after a noun.\n\n"
        "<pre>\n  猫だ。  \nIt is a cat.\n\n本です。\nIt is a book.\n</pre>\n",
        encoding="utf-8",
    )
    (root / "src/Section1/Part1/Lesson2.md").write_text(
        "# Editorial review\n\nThis chapter has no safely mapped grammar point.\n",
        encoding="utf-8",
    )
    return lessons


class GenericYomitanAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="community-adapter-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_preserves_every_term_row_and_upstream_index_metadata(self):
        archive = _write_yomitan(self.root / "one.zip")

        adapted = _adapt(archive, "donna-toki", expected_rows=1)

        self.assertEqual(len(adapted.bundle.records), 1)
        self.assertEqual(len(adapted.bundle.senses), 1)
        block = adapted.bundle.senses[0].blocks[0].to_dict()["content"]
        self.assertEqual(block["row"][0], "〜synthetic")
        self.assertEqual(block["row"][5], ["Synthetic explanation"])
        self.assertEqual(adapted.report["index"]["revision"], INDEX_REVISION)
        self.assertEqual(adapted.report["expected_rows"], 1)
        self.assertEqual(adapted.report["imported_rows"], 1)
        self.assertEqual(adapted.report["rejected_rows"], 0)

    def test_same_title_different_versions_have_distinct_source_identities(self):
        first = _write_yomitan(self.root / "first.zip", revision="v1")
        second = _write_yomitan(self.root / "second.zip", revision="v2")

        a = _adapt(first, "donna-toki")
        b = _adapt(second, "donna-toki")

        self.assertNotEqual(
            a.bundle.source_revision.revision_id,
            b.bundle.source_revision.revision_id,
        )
        self.assertNotEqual(
            a.bundle.records[0].source_record_id,
            b.bundle.records[0].source_record_id,
        )
        self.assertEqual(a.report["index"]["title"], b.report["index"]["title"])

    def test_constant_sequence_sentinel_does_not_group_unrelated_rows(self):
        archive = _write_yomitan(
            self.root / "sentinel.zip",
            second_term="〜another-synthetic",
        )

        adapted = _adapt(archive, "donna-toki")

        self.assertEqual(
            [sense.concept_ref for sense in adapted.bundle.senses],
            [None, None],
        )
        self.assertFalse(adapted.report["sequence_identity"]["meaningful"])

    def test_media_names_are_namespaced_and_content_addressed(self):
        first = _write_yomitan(self.root / "first.zip", media=b"\x89PNG\r\n\x1a\nFIRST")
        second = _write_yomitan(
            self.root / "second.zip", media=b"\x89PNG\r\n\x1a\nSECOND"
        )

        a = _adapt(first, "donna-toki")
        b = _adapt(second, "e-de-wakaru")

        self.assertNotEqual(
            a.bundle.media[0].source_path, b.bundle.media[0].source_path
        )
        self.assertTrue(a.bundle.media[0].source_path.startswith("media/donna-toki/"))
        self.assertTrue(b.bundle.media[0].source_path.startswith("media/e-de-wakaru/"))
        self.assertEqual(set(a.media_bytes), {a.bundle.media[0].source_path})

    def test_duplicate_archive_members_and_malformed_rows_fail_loudly(self):
        duplicate = self.root / "duplicate.zip"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(duplicate, "w") as archive:
                archive.writestr("index.json", "{}")
                archive.writestr("index.json", "{}")
        malformed = _write_yomitan(self.root / "malformed.zip", malformed_row=True)

        with self.assertRaisesRegex(AdapterError, "duplicate archive member"):
            _adapt(duplicate, "donna-toki")
        with self.assertRaisesRegex(AdapterError, "eight fields"):
            _adapt(malformed, "donna-toki")

    def test_duplicate_mirrors_are_detected_by_exact_archive_hash(self):
        archive = _write_yomitan(self.root / "one.zip")
        mirror = self.root / "mirror.zip"
        mirror.write_bytes(archive.read_bytes())

        duplicates = find_duplicate_archives(
            {"donna-toki": archive, "e-de-wakaru": mirror}
        )

        self.assertEqual(duplicates, {"e-de-wakaru": "donna-toki"})

    def test_safe_directory_members_do_not_make_a_valid_archive_fail(self):
        archive = _write_yomitan(self.root / "directory.zip", directory_member=True)

        adapted = _adapt(archive, "donna-toki")

        self.assertEqual(len(adapted.bundle.records), 1)

    def test_archive_requires_an_external_digest_pin(self):
        archive = _write_yomitan(self.root / "unpinned.zip")

        with self.assertRaisesRegex(AdapterError, "SHA-256 pin"):
            adapt_yomitan_archive(archive, "donna-toki")

    def test_non_yomitan_source_policy_cannot_be_claimed_by_a_zip(self):
        archive = _write_yomitan(self.root / "counterfeit-yokubi.zip")

        with self.assertRaisesRegex(
            AdapterError, "not registered as a Yomitan archive"
        ):
            _adapt(archive, "yokubi")


class YokubiAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="yokubi-adapter-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.lessons = _write_yokubi(self.root)

    def test_imports_mapped_lessons_without_inventing_editorial_entries(self):
        adapted = adapt_yokubi_tree(
            self.root,
            revision_id="fixture-r1",
            lesson_concepts=self.lessons,
        )

        self.assertEqual(len(adapted.bundle.records), 1)
        self.assertEqual(adapted.report["mapped_concepts"], 2)
        self.assertEqual(
            adapted.report["chapter_status_counts"],
            {
                "imported": 1,
                "not-applicable": 2,
                "skipped": 1,
            },
        )
        statuses = {item["path"]: item for item in adapted.report["chapters"]}
        self.assertEqual(statuses["Section1/Part1/Lesson2.md"]["status"], "skipped")
        self.assertIn("no explicit", statuses["Section1/Part1/Lesson2.md"]["reason"])
        self.assertEqual(adapted.bundle.records[0].raw_expression, "だ / です")

    def test_extracts_source_formation_and_example_pairs_with_provenance(self):
        adapted = adapt_yokubi_tree(
            self.root,
            revision_id="fixture-r1",
            lesson_concepts=self.lessons,
        )
        sense = adapted.bundle.senses[0]

        self.assertIn("formation", {block.kind.value for block in sense.blocks})
        self.assertEqual(len(sense.examples), 2)
        self.assertEqual(sense.examples[0].japanese, "猫だ。")
        self.assertEqual(sense.examples[0].translation, "It is a cat.")
        self.assertEqual(
            sense.examples[0].provenance.content_sha256,
            hashlib.sha256("  猫だ。  \nIt is a cat.".encode("utf-8")).hexdigest(),
        )
        lesson = self.root / "src/Section1/Part1/Lesson1.md"
        self.assertEqual(
            adapted.bundle.records[0].field_hashes["markdown"],
            hashlib.sha256(lesson.read_bytes()).hexdigest(),
        )

    def test_non_example_preformatted_groups_remain_source_blocks(self):
        lesson = self.root / "src/Section1/Part1/Lesson1.md"
        lesson.write_text(
            lesson.read_text(encoding="utf-8")
            + "\n<pre>\nする\tto do\nした\tdid\n</pre>\n"
            + '<pre>\n"I will eat," he said.\nHe said he would eat.\n</pre>\n',
            encoding="utf-8",
        )

        adapted = adapt_yokubi_tree(
            self.root,
            revision_id="fixture-r1",
            lesson_concepts=self.lessons,
        )
        sense = adapted.bundle.senses[0]

        self.assertEqual(len(sense.examples), 2)
        source_blocks = [block.to_dict()["content"] for block in sense.blocks]
        self.assertIn("する\tto do\nした\tdid", source_blocks)
        self.assertIn('"I will eat," he said.\nHe said he would eat.', source_blocks)
        self.assertIn(
            PINNED_YOKUBI_REVISION,
            adapt_community_sources().outcome_by_source()["yokubi"].reason,
        )

    def test_summary_and_mapping_must_account_for_the_same_lesson_set(self):
        del self.lessons["Section1/Part1/Lesson2.md"]

        with self.assertRaisesRegex(AdapterError, "lesson coverage mismatch"):
            adapt_yokubi_tree(
                self.root,
                revision_id="fixture-r1",
                lesson_concepts=self.lessons,
            )

    def test_expected_yokubi_content_digest_rejects_a_modified_checkout(self):
        with self.assertRaisesRegex(AdapterError, "content SHA-256 mismatch"):
            adapt_yokubi_tree(
                self.root,
                revision_id="fixture-r1",
                lesson_concepts=self.lessons,
                expected_content_sha256="0" * 64,
            )

    def test_repository_mapping_accounts_for_all_64_pinned_lessons(self):
        self.assertEqual(len(YOKUBI_LESSON_CONCEPTS), 64)


class CommunityCoverageTests(unittest.TestCase):
    def test_available_private_archive_requires_a_digest_pin(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = _write_yomitan(Path(directory) / "private.zip")

            with self.assertRaisesRegex(AdapterError, "digest pin"):
                adapt_community_sources({"donna-toki": archive})

    def test_every_selected_and_deferred_candidate_has_an_explicit_outcome(self):
        result = adapt_community_sources()
        outcomes = result.outcome_by_source()

        for source_id in (
            "nihongo-kyoshi",
            "donna-toki",
            "e-de-wakaru",
            "dojg",
            "nihongo-no-sensei",
            "yokubi",
            "tae-kim",
            "jlpt-sensei",
            "maggie-sensei",
            "japanese-wikibooks",
        ):
            self.assertIn(source_id, outcomes)
        self.assertEqual(outcomes["dojg"].status, "unavailable")
        self.assertEqual(outcomes["tae-kim"].status, "link-only")
        self.assertEqual(outcomes["japanese-wikibooks"].status, "excluded")
        self.assertEqual(outcomes["dojg"].content_count, 0)
        self.assertFalse(result.bundles)


if __name__ == "__main__":
    unittest.main()

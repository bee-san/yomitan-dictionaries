"""Regressions for IMABI references and private full-content ingestion."""

import json
from pathlib import Path
import tempfile
from typing import cast
import unittest
from unittest.mock import patch

import scripts.grammar.sources.imabi as imabi_module
from scripts.grammar.sources.imabi import (
    ImabiImportError,
    ImabiLessonReference,
    ImabiReferenceError,
    audited_reference_index,
    audited_reference_json,
    convert_snapshot,
    fetch_snapshot,
    source_lesson_id,
)


EXPECTED_SOURCE_LESSON_IDS = {
    "imabi:classical:lesson:1",
    "imabi:classical:lesson:12",
    "imabi:classical:lesson:18",
    "imabi:modern:lesson:0",
    "imabi:modern:lesson:4",
    "imabi:modern:lesson:260",
}


class ImabiLinkOnlyTests(unittest.TestCase):
    def test_audited_references_are_explicitly_partial_and_link_only(self):
        index = audited_reference_index()
        references = cast(list[dict[str, object]], index["references"])

        self.assertEqual(index["mode"], "metadata-link-only")
        self.assertEqual(index["coverage"], "partial-audited")
        self.assertEqual(index["content_status"], "not-included-pending-follow-up")
        self.assertEqual(
            index["permission_basis"],
            "user-reported-project-specific-author-approval",
        )
        self.assertEqual(index["publication_status"], "not-authorized")
        self.assertEqual(index["reference_count"], 6)
        self.assertEqual(index["lesson_body_count"], 0)
        self.assertEqual(index["content_block_count"], 0)
        self.assertEqual(index["example_count"], 0)
        self.assertEqual(index["media_count"], 0)
        self.assertEqual(
            {item["source_lesson_id"] for item in references},
            EXPECTED_SOURCE_LESSON_IDS,
        )
        self.assertEqual(
            {item["material_kind"] for item in references},
            {"modern", "classical"},
        )
        self.assertTrue(
            all(item["reference_kind"] in {"link-only", "reference-only"} for item in references)
        )
        self.assertEqual(
            {
                item["source_lesson_id"]: (
                    item["title"],
                    item["canonical_url"],
                    item["toc_section"],
                    item["reference_kind"],
                )
                for item in references
            },
            {
                "imabi:modern:lesson:0": (
                    "Introduction to Japanese",
                    "https://imabi.org/what-is-japanese/",
                    "Beginners 1",
                    "reference-only",
                ),
                "imabi:modern:lesson:4": (
                    "Hiragana",
                    "https://imabi.org/hiragana%E3%80%80%E3%81%B2%E3%82%89%E3%81%8C%E3%81%AA",
                    "Beginners 1",
                    "link-only",
                ),
                "imabi:modern:lesson:260": (
                    "No Doubt that",
                    "https://imabi.org/no-doubt-that/",
                    "Advanced I",
                    "link-only",
                ),
                "imabi:classical:lesson:12": (
                    "The Auxiliary Verb ～ず II",
                    "https://imabi.org/the-auxiliary-verb-%EF%BD%9E%E3%81%9A-ii/",
                    "Classical Japanese",
                    "link-only",
                ),
                "imabi:classical:lesson:1": (
                    "Introduction to Classical Japanese",
                    "https://imabi.org/intro-to-classical/",
                    "Classical Japanese",
                    "reference-only",
                ),
                "imabi:classical:lesson:18": (
                    "Classical Adverbs",
                    "https://imabi.org/classical-adverbs/",
                    "Classical Japanese",
                    "link-only",
                ),
            },
        )

    def test_reference_output_cannot_leak_lesson_content_or_site_chrome(self):
        blocked_keys = {
            "ads",
            "blocks",
            "body",
            "examples",
            "footer",
            "html",
            "media",
            "nav",
            "navigation",
            "scripts",
            "translation",
        }

        def assert_safe(value):
            if isinstance(value, dict):
                self.assertTrue(blocked_keys.isdisjoint(value))
                for child in value.values():
                    assert_safe(child)
            elif isinstance(value, list):
                for child in value:
                    assert_safe(child)

        index = audited_reference_index()
        assert_safe(index)
        self.assertEqual(
            set(index["references"][0]),
            {
                "source_lesson_id",
                "lesson_number",
                "title",
                "canonical_url",
                "toc_section",
                "material_kind",
                "reference_kind",
            },
        )

    def test_reference_validation_rejects_markup_and_noncanonical_urls(self):
        defaults = {
            "lesson_number": 0,
            "title": "Introduction to Japanese",
            "canonical_url": "https://imabi.org/what-is-japanese/",
            "toc_section": "Beginners 1",
            "material_kind": "modern",
            "reference_kind": "reference-only",
        }
        cases = (
            ({"title": "<script>alert(1)</script>"}, "plain metadata"),
            (
                {"canonical_url": "https://imabi.org/what-is-<script>/"},
                "plain metadata",
            ),
            ({"canonical_url": "https://imabi.org/what-is-japanese/\u0085"}, "plain metadata"),
            ({"canonical_url": "https://imabi.org/what-is-japanese/\n"}, "plain metadata"),
            ({"canonical_url": " https://imabi.org/what-is-japanese/"}, "plain metadata"),
            ({"canonical_url": "https://imabi.org/what is-japanese/"}, "whitespace"),
            ({"canonical_url": "https://example.org/what-is-japanese/"}, "imabi.org"),
            ({"canonical_url": "https://IMABI.org/what-is-japanese/"}, "imabi.org"),
            ({"canonical_url": "https://imabi.org:443/what-is-japanese/"}, "imabi.org"),
            ({"canonical_url": "http://imabi.org/what-is-japanese/"}, "HTTPS"),
            ({"canonical_url": "https://imabi.org/what-is-japanese/?tracking=1"}, "audited"),
            ({"canonical_url": "https://imabi.org/what-is-japanese/?"}, "audited"),
            ({"canonical_url": "https://imabi.org/what-is-japanese/#"}, "audited"),
            ({"canonical_url": "https://imabi.org//what-is-japanese/"}, "audited"),
            ({"canonical_url": "https://imabi.org/../what-is-japanese/"}, "audited"),
            ({"canonical_url": "https://imabi.org/what-is-japanese%2f"}, "audited"),
            ({"canonical_url": "https://imabi.org/what-is-%ZZ/"}, "audited"),
            ({"canonical_url": "https://imabi.org/synthetic-lesson/"}, "audited"),
            ({"title": "Synthetic lesson title"}, "audited metadata"),
        )

        for changes, message in cases:
            with self.subTest(changes=changes), self.assertRaisesRegex(ImabiReferenceError, message):
                ImabiLessonReference(**(defaults | changes))

    def test_modern_and_classical_numbering_have_distinct_stable_ids(self):
        modern = source_lesson_id("modern", 12)
        classical = source_lesson_id("classical", 12)

        self.assertEqual(modern, "imabi:modern:lesson:12")
        self.assertEqual(classical, "imabi:classical:lesson:12")
        self.assertNotEqual(modern, classical)

    def test_audited_index_is_deterministic_fresh_data_without_network_access(self):
        with patch("urllib.request.urlopen") as urlopen:
            first = audited_reference_json()
            second = audited_reference_json()

        urlopen.assert_not_called()
        self.assertEqual(first, second)
        self.assertEqual(json.loads(first), audited_reference_index())

        mutated = audited_reference_index()
        mutated["references"][0]["title"] = "mutated"
        self.assertNotEqual(mutated, audited_reference_index())


def _fixture_page(title: str, body: str) -> bytes:
    return f"""<!doctype html><html lang="ja"><body>
    <article><h1 class="entry-title">{title}</h1>
    <div class="entry-content">{body}
      <div class="sharedaddy">site chrome must not survive</div>
    </div></article></body></html>""".encode()


class ImabiContentTests(unittest.TestCase):
    TOC_URL = "https://imabi.org/table-of-contents-%E7%9B%AE%E6%AC%A1/"
    PAGES = {
        TOC_URL: """<!doctype html><div class="entry-content">
          <h3>Beginners 1</h3>
          <p>\u7b2c0\u8ab2: <a href="https://imabi.org/what-is-japanese/">Introduction to Japanese</a></p>
          <h2>Advanced I</h2>
          <p>\u7b2c260\u8ab2: <a href="https://imabi.org/no-doubt-that/">No Doubt that</a></p>
          <p>\u7b2c260\u8ab2: <a href="https://imabi.org/a-second-260/">A second source-labelled 260</a></p>
          <h2>\u53e4\u5178\u8a9e: Classical Japanese</h2>
          <p>\u7b2c12\u8ab2: <a href="https://imabi.org/the-auxiliary-verb-%EF%BD%9E%E3%81%9A-ii/">The Auxiliary Verb \uff5e\u305a II</a></p>
          <h2>\u7409\u7403\u8af8\u8a9e\uff08\u7409\u7403\u8a9e\u6d3e\uff09: Okinawan</h2>
          <p>\u7b2c1\u8ab2: <a href="https://imabi.org/okinawan-script/">Okinawan Script</a></p>
        </div>""".encode(),
        "https://imabi.org/what-is-japanese/": _fixture_page(
            "Introduction to Japanese",
            "<h2>Japanese Grammar</h2><p>Substantive introductory explanation.</p>",
        ),
        "https://imabi.org/no-doubt-that/": _fixture_page(
            "No Doubt that",
            """<h3 id="ni-chigainai"><img src="https://imabi.org/heading.png" alt="source diagram">\uff5e\u306b\u9055\u3044\u306a\u3044</h3>
            <p>This expression marks confident inference.</p>
            <figure class="wp-block-table"><table><tr><th>Nouns</th><td>N \uff0b \uff5e\u306b\u9055\u3044\u306a\u3044</td></tr></table></figure>
            <p>1. \u6ce5\u68d2\u304c\u5165\u3063\u305f\u306b\u9055\u3044\u306a\u3044\u3002<br>There is no doubt that a robber came in.</p>""",
        ),
        "https://imabi.org/a-second-260/": _fixture_page(
            "A second source-labelled 260",
            "<h3>Distinct page</h3><p>Distinct source content with the same displayed lesson number.</p>",
        ),
        "https://imabi.org/the-auxiliary-verb-%EF%BD%9E%E3%81%9A-ii/": _fixture_page(
            "The Auxiliary Verb \uff5e\u305a II",
            """<h3>\u7b2c012\u8ab2: \uff5e\u305a II</h3>
            <p>Classical explanation retained separately.</p>
            <p>1. \u6708\u306a\u898b\u7d66\u3072\u305d\u3002<br>Do not look at the moon.</p>""",
        ),
    }

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="imabi-content-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.snapshot = self.root / "imabi.zip"

    def _fetch(self, url: str, _timeout: float) -> bytes:
        return self.PAGES[url]

    def test_bounded_snapshot_is_complete_accounted_and_deterministic(self):
        calls: list[str] = []

        def fetch(url: str, timeout: float) -> bytes:
            calls.append(url)
            return self._fetch(url, timeout)

        first = fetch_snapshot(
            self.TOC_URL,
            self.snapshot,
            self.root / "cache",
            fetch=fetch,
            delay=lambda _seconds: None,
            request_delay=0,
            max_pages=10,
        )
        first_bytes = self.snapshot.read_bytes()
        second_snapshot = self.root / "imabi-second.zip"
        second = fetch_snapshot(
            self.TOC_URL,
            second_snapshot,
            self.root / "cache",
            fetch=lambda *_args: self.fail("validated cache should be reused"),
            delay=lambda _seconds: None,
            request_delay=0,
            max_pages=10,
        )

        self.assertEqual(first_bytes, second_snapshot.read_bytes())
        self.assertEqual(first["snapshot_sha256"], second["snapshot_sha256"])
        self.assertEqual(first["indexed_lesson_count"], 5)
        self.assertEqual(first["eligible_lesson_count"], 4)
        self.assertEqual(first["not_applicable_count"], 1)
        self.assertEqual(first["fetched_page_count"], 4)
        self.assertEqual(len(calls), 5)
        self.assertNotIn("https://imabi.org/okinawan-script/", calls)
        self.assertEqual(
            {item["status"] for item in first["outcomes"]},
            {"eligible", "not-applicable"},
        )

    def test_snapshot_conversion_preserves_sections_formations_examples_and_permission(self):
        fetch_snapshot(
            self.TOC_URL,
            self.snapshot,
            self.root / "cache",
            fetch=self._fetch,
            delay=lambda _seconds: None,
            request_delay=0,
            max_pages=10,
        )

        bundle, report = convert_snapshot(self.snapshot, "official-fixture")
        serialized = json.dumps(bundle.to_dict(), ensure_ascii=False)

        self.assertEqual(len(bundle.records), 4)
        self.assertEqual(len({record.source_record_id for record in bundle.records}), 4)
        self.assertEqual(
            set(report["expected_source_record_ids"]),
            {record.source_record_id for record in bundle.records},
        )
        self.assertEqual(report["indexed_lesson_count"], 5)
        self.assertEqual(report["imported_lesson_count"], 4)
        self.assertEqual(report["rejected_lesson_count"], 0)
        self.assertEqual(report["not_applicable_count"], 1)
        self.assertGreater(report["content_block_count"], 4)
        self.assertGreaterEqual(report["formation_block_count"], 1)
        self.assertEqual(report["example_count"], 2)
        self.assertEqual(report["linked_image_count"], 1)
        self.assertEqual(report["source_content_unit_count"], report["preserved_content_unit_count"])
        self.assertTrue(
            all(
                item["source_content_unit_count"] == item["preserved_content_unit_count"]
                for item in report["imported_lessons"]
            )
        )
        self.assertEqual(report["material_counts"], {"classical": 1, "modern": 3})
        self.assertNotIn("site chrome must not survive", serialized)
        self.assertIn("Substantive introductory explanation.", serialized)
        self.assertIn("N \uff0b \uff5e\u306b\u9055\u3044\u306a\u3044", serialized)
        self.assertIn("https://imabi.org/heading.png", serialized)

        examples = [example for sense in bundle.senses for example in sense.examples]
        self.assertEqual(
            {(example.japanese, example.translation) for example in examples},
            {
                ("1. \u6ce5\u68d2\u304c\u5165\u3063\u305f\u306b\u9055\u3044\u306a\u3044\u3002", "There is no doubt that a robber came in."),
                ("1. \u6708\u306a\u898b\u7d66\u3072\u305d\u3002", "Do not look at the moon."),
            },
        )
        labels = {label for sense in bundle.senses for label in sense.level_labels}
        self.assertIn("IMABI:modern", labels)
        self.assertIn("IMABI:classical", labels)
        self.assertTrue(all("entry-content" in block.provenance.locator for sense in bundle.senses for block in sense.blocks))
        self.assertEqual(bundle.source_revision.publication_mode.value, "denied")
        self.assertIn("user-reported project permission", bundle.source_revision.license.notice)
        self.assertNotIn("open licence", bundle.source_revision.license.notice.lower())

    def test_example_extraction_does_not_drop_adjacent_source_notes(self):
        pages = dict(self.PAGES)
        pages["https://imabi.org/no-doubt-that/"] = _fixture_page(
            "No Doubt that",
            """<h3>～に違いない</h3>
            <p>1. 泥棒が入ったに違いない。<br>
            There is no doubt that a robber came in.<br>
            Grammar Note: This source note must remain.</p>""",
        )
        fetch_snapshot(
            self.TOC_URL,
            self.snapshot,
            self.root / "cache",
            fetch=lambda url, _timeout: pages[url],
            delay=lambda _seconds: None,
            request_delay=0,
            max_pages=10,
        )

        bundle, _report = convert_snapshot(self.snapshot, "official-fixture")
        serialized = json.dumps(bundle.to_dict(), ensure_ascii=False)

        self.assertIn("Grammar Note: This source note must remain.", serialized)

    def test_missing_lesson_body_refuses_partial_import_with_identity_report(self):
        pages = dict(self.PAGES)
        pages["https://imabi.org/a-second-260/"] = b"<html><p>no entry content</p></html>"
        fetch_snapshot(
            self.TOC_URL,
            self.snapshot,
            self.root / "cache",
            fetch=lambda url, _timeout: pages[url],
            delay=lambda _seconds: None,
            request_delay=0,
            max_pages=10,
        )

        with self.assertRaisesRegex(ImabiImportError, "refusing partial IMABI import") as raised:
            convert_snapshot(self.snapshot, "official-fixture")

        self.assertEqual(raised.exception.report["rejected_lesson_count"], 1)
        self.assertEqual(
            raised.exception.report["rejected_lessons"][0]["canonical_url"],
            "https://imabi.org/a-second-260/",
        )

    def test_snapshot_read_cannot_be_redirected_after_regular_file_validation(self):
        fetch_snapshot(
            self.TOC_URL,
            self.snapshot,
            self.root / "cache",
            fetch=self._fetch,
            delay=lambda _seconds: None,
            request_delay=0,
            max_pages=10,
        )
        verified_bytes = self.snapshot.read_bytes()
        replacement = self.root / "replacement.zip"
        replacement.write_bytes(b"not the verified snapshot")
        original_reader = imabi_module._read_regular_nofollow

        def swap_after_read(path: Path, limit: int, context: str) -> bytes:
            content = original_reader(path, limit, context)
            if context == "IMABI snapshot":
                path.unlink()
                path.symlink_to(replacement)
            return content

        with patch.object(imabi_module, "_read_regular_nofollow", side_effect=swap_after_read):
            bundle, report = convert_snapshot(self.snapshot, "race-fixture")

        self.assertEqual(report["imported_lesson_count"], 4)
        self.assertEqual(bundle.source_revision.content_sha256, imabi_module._sha256(verified_bytes))

    def test_snapshot_rejects_an_individually_oversized_member_before_parsing(self):
        fetch_snapshot(
            self.TOC_URL,
            self.snapshot,
            self.root / "cache",
            fetch=self._fetch,
            delay=lambda _seconds: None,
            request_delay=0,
            max_pages=10,
        )
        with imabi_module.zipfile.ZipFile(self.snapshot) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        page_name = next(name for name in members if name.startswith("pages/"))
        members[page_name] = b"x" * (imabi_module.MAX_PAGE_BYTES + 1)
        with imabi_module.zipfile.ZipFile(
            self.snapshot,
            "w",
            compression=imabi_module.zipfile.ZIP_DEFLATED,
        ) as archive:
            for name, content in members.items():
                info = imabi_module.zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                info.compress_type = imabi_module.zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = (imabi_module.stat.S_IFREG | 0o644) << 16
                archive.writestr(info, content)

        with self.assertRaisesRegex(ImabiImportError, "snapshot member exceeds"):
            convert_snapshot(self.snapshot, "official-fixture")


if __name__ == "__main__":
    unittest.main()
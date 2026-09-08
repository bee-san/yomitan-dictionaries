"""IMABI regressions use only the metadata approved by the UGD-04 audit."""

import json
from typing import cast
import unittest
from unittest.mock import patch

from scripts.grammar.sources.imabi import (
    ImabiLessonReference,
    ImabiReferenceError,
    audited_reference_index,
    audited_reference_json,
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


if __name__ == "__main__":
    unittest.main()
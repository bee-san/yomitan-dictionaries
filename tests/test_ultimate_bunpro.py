"""Tests for the Bunpro HTML-to-canonical adapter.

Fixtures contain synthetic prose and public grammar-point metadata only.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import quote
import zipfile

from scripts.grammar.model import BlockKind, SourceBundle
from scripts.grammar.sources import bunpro


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _page(
    grammar_point_id: int,
    slug: str,
    level: str,
    *,
    body: str | None = None,
    extra_page_props: dict[str, object] | None = None,
) -> bytes:
    writeup_id = grammar_point_id + 10_000
    question_id = grammar_point_id + 20_000
    reviewable = {
        "id": grammar_point_id,
        "level": level,
        "lesson_id": grammar_point_id,
        "part_of_speech": "助詞",
        "register": "一般",
        "word_type": "文法",
        "slug": slug,
        "nuance": "<strong>合成</strong>の日本語説明。",
        "grammar_order": grammar_point_id,
        "discourse_link": f"https://community.bunpro.jp/t/{grammar_point_id}",
        "type_snake": "grammar_point",
        "type_pascal": "GrammarPoint",
        "title": slug,
        "furigana": f"<ruby>{slug}<rt>かな</rt></ruby>",
        "meaning": f"Synthetic meaning {grammar_point_id}",
        "nuance_translation": "<strong>Synthetic</strong> English nuance.",
        "rare_kanji_warning": "Synthetic rare-kanji note.",
        "caution": "Synthetic caution.",
        "polite_structure": "Noun + <strong>polite</strong>",
        "casual_structure": "Verb + <strong>casual</strong>",
        "part_of_speech_translation": "Particle",
        "register_translation": "Standard",
        "word_type_translation": "Grammar",
        "metadata": f"{slug}, synthetic alias",
        "next_grammar_point": None,
        "previous_grammar_point": None,
    }
    included = {
        "writeups": [
            {
                "id": writeup_id,
                "grammar_point_id": grammar_point_id,
                "body": body or "<p>Synthetic <strong>English explanation</strong>.</p>",
                "body_ja": "<p>合成の<ruby>説明<rt>せつめい</rt></ruby>。</p>",
                "has_been_translated": True,
            }
        ],
        "studyQuestions": [
            {
                "id": question_id,
                "content": "合成____例文。",
                "answer": slug,
                "alternate_grammar": [slug],
                "kanji_answer": f"<ruby>{slug}<rt>かな</rt></ruby>",
                "kanji_alt_grammar": [],
                "translation": "A <strong>synthetic</strong> translation.",
                "word_prompt": "Synthetic prompt",
                "tense": "Synthetic label",
                "extra_info": "Synthetic example note.",
                "sentenceable_type": "Writeup",
                "sentenceable_id": writeup_id,
                "validation_status": "validated",
                "question_type": "readonly",
                "wrong_answers": {"SECRET_SRS_SENTINEL": {"en": "ignored"}},
                "male_audio_url": "https://audio.invalid/account-sentinel.mp3",
            }
        ],
        "offlineResources": [
            {
                "id": grammar_point_id + 30_000,
                "source": "Synthetic reference",
                "location": "Page 1",
                "readable_type": "OfflineResource",
            }
        ],
        "supplementalLinks": [
            {
                "id": grammar_point_id + 40_000,
                "site": "Synthetic reference",
                "description": "Further reading",
                "link": "https://example.org/reference",
                "readable_type": "SupplementalLink",
            }
        ],
        "relatedContents": [
            {
                "id": grammar_point_id + 50_000,
                "relationship_type": "related",
                "body": "Synthetic relationship explanation.",
                "first_relatable": {
                    "id": grammar_point_id,
                    "slug": slug,
                    "title": slug,
                    "type_snake": "grammar_point",
                },
                "second_relatable": {
                    "id": grammar_point_id + 1,
                    "slug": f"related-{grammar_point_id}",
                    "title": f"Related {grammar_point_id}",
                    "type_snake": "grammar_point",
                },
            }
        ],
        "attachedReviewables": [
            {
                "id": grammar_point_id + 60_000,
                "slug": "VOCAB_ONLY_SENTINEL",
                "title": "VOCAB_ONLY_SENTINEL",
                "meaning": "must not be imported",
                "type_snake": "vocab",
            }
        ],
        "articles": [],
    }
    page_props: dict[str, object] = {
        "reviewable": reviewable,
        "included": included,
        "latestDiscourseReplies": {"ACCOUNT_HISTORY_SENTINEL": True},
        "error": None,
    }
    if extra_page_props:
        page_props.update(extra_page_props)
    data = {
        "page": "/grammar_points/[slug]",
        "query": {"slug": slug},
        "buildId": "fixture-build",
        "props": {"pageProps": page_props, "__N_SSG": True},
    }
    payload = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    return (
        "<!doctype html><html><body><script id=\"__NEXT_DATA__\" "
        f"type=\"application/json\">{payload}</script>"
        "</body></html>"
    ).encode("utf-8")


def _next_data(page: bytes) -> dict:
    payload = page.decode("utf-8").split('type="application/json">', 1)[1]
    return json.loads(payload.split("</script>", 1)[0])


def _sitemap(urls: list[str]) -> bytes:
    locations = "".join(f"<url><loc>{url}</loc></url>" for url in urls)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{locations}</urlset>"
    ).encode("utf-8")


def _sitemap_index(urls: list[str]) -> bytes:
    locations = "".join(f"<sitemap><loc>{url}</loc></sitemap>" for url in urls)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{locations}</sitemapindex>"
    ).encode("utf-8")


def _write_zip(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(members.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)


def _snapshot(path: Path, pages: list[tuple[int, str, bytes]]) -> Path:
    root_url = "https://bunpro.jp/sitemap.xml"
    page_entries = []
    members: dict[str, bytes] = {}
    urls = []
    for grammar_point_id, slug, content in pages:
        url = f"https://bunpro.jp/grammar_points/{quote(slug, safe='')}"
        member = f"pages/{hashlib.sha256(url.encode()).hexdigest()}.html"
        members[member] = content
        urls.append(url)
        page_entries.append(
            {
                "url": url,
                "path": member,
                "sha256": hashlib.sha256(content).hexdigest(),
                "grammar_point_id": grammar_point_id,
            }
        )
    sitemap = _sitemap(urls)
    sitemap_path = f"sitemaps/{hashlib.sha256(root_url.encode()).hexdigest()}.xml"
    members[sitemap_path] = sitemap
    manifest = {
        "format": "bunpro-html-snapshot",
        "format_version": 1,
        "source_id": "bunpro",
        "root_sitemap_url": root_url,
        "sitemaps": [
            {
                "url": root_url,
                "path": sitemap_path,
                "sha256": hashlib.sha256(sitemap).hexdigest(),
            }
        ],
        "pages": page_entries,
    }
    members["snapshot.json"] = _json_bytes(manifest)
    _write_zip(path, members)
    return path


class BunproAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="ultimate-bunpro-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def test_maps_substantive_n5_n3_n1_pages_to_canonical_records(self) -> None:
        snapshot = _snapshot(
            self.folder / "snapshot.zip",
            [
                (101, "〜です", _page(101, "〜です", "JLPT5")),
                (303, "〜なら", _page(303, "〜なら", "JLPT3")),
                (501, "〜に至って", _page(501, "〜に至って", "JLPT1")),
            ],
        )

        bundle, report = bunpro.convert_snapshot(snapshot, "official-fixture-r1")
        bundle.validate()

        self.assertEqual(len(bundle.records), 3)
        self.assertEqual(len(bundle.senses), 3)
        self.assertEqual(
            {sense.level_labels for sense in bundle.senses},
            {("JLPT5",), ("JLPT3",), ("JLPT1",)},
        )
        self.assertTrue(
            all(
                {BlockKind.MEANING, BlockKind.FORMATION} <= {block.kind for block in sense.blocks}
                for sense in bundle.senses
            )
        )
        self.assertTrue(all(sense.examples for sense in bundle.senses))
        self.assertTrue(
            all(
                example.japanese["source_validation_status"] == "validated"
                for sense in bundle.senses
                for example in sense.examples
            )
        )
        self.assertTrue(all(len(sense.links) >= 3 for sense in bundle.senses))
        self.assertEqual(report["expected_record_count"], 3)
        self.assertEqual(report["imported_record_count"], 3)
        self.assertEqual(report["rejected_record_count"], 0)
        self.assertEqual(
            set(report["expected_source_record_ids"]),
            set(report["imported_source_record_ids"]),
        )
        self.assertEqual(report["levels"], {"JLPT1": 1, "JLPT3": 1, "JLPT5": 1})
        for record in bundle.records:
            self.assertIn("level", record.field_hashes)
            self.assertIn("discourse-link", record.field_hashes)
            self.assertIn("supplemental-links", record.field_hashes)
            self.assertIn("related-links", record.field_hashes)
        serialized = json.dumps(bundle.to_dict(), ensure_ascii=False)
        self.assertIn("English explanation", serialized)
        self.assertIn("translation.", serialized)
        self.assertIn("ruby", serialized)
        self.assertIn("Synthetic reference", serialized)
        self.assertIn("Related 101", serialized)

    def test_sanitizes_html_and_drops_account_srs_and_vocab_rows(self) -> None:
        malicious = (
            "<p>Keep <ruby>語<rt>ご</rt></ruby>"
            "<script>HTML_SCRIPT_SENTINEL</script>"
            "<iframe>IFRAME_SENTINEL</iframe>"
            "<a href='javascript:ALERT_SENTINEL' onclick='BAD'>link text</a></p>"
        )
        snapshot = _snapshot(
            self.folder / "snapshot.zip",
            [(101, "〜です", _page(101, "〜です", "JLPT5", body=malicious))],
        )

        bundle, report = bunpro.convert_snapshot(snapshot, "official-fixture-r1")

        serialized = json.dumps(bundle.to_dict(), ensure_ascii=False)
        self.assertIn("Keep", serialized)
        self.assertIn("ruby", serialized)
        self.assertIn("link text", serialized)
        for forbidden in (
            "HTML_SCRIPT_SENTINEL",
            "IFRAME_SENTINEL",
            "ALERT_SENTINEL",
            "onclick",
            "ACCOUNT_HISTORY_SENTINEL",
            "SECRET_SRS_SENTINEL",
            "VOCAB_ONLY_SENTINEL",
            "account-sentinel",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(report["excluded_vocabulary_rows"], 1)
        self.assertGreater(report["excluded_non_content_fields"], 0)

    def test_rejects_required_example_text_emptied_by_html_sanitation(self) -> None:
        data = _next_data(_page(102, "〜空", "JLPT5"))
        question = data["props"]["pageProps"]["included"]["studyQuestions"][0]
        question["content"] = "<script>REMOVED</script>"
        payload = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
        page = (
            "<!doctype html><html><body><script id=\"__NEXT_DATA__\" "
            f"type=\"application/json\">{payload}</script></body></html>"
        ).encode()
        snapshot = _snapshot(self.folder / "snapshot.zip", [(102, "〜空", page)])

        with self.assertRaises(bunpro.BunproImportError) as raised:
            bunpro.convert_snapshot(snapshot, "official-fixture-r1")
        self.assertIn(
            "content becomes empty after HTML sanitation",
            raised.exception.report["rejected_records"][0]["reason"],
        )

    def test_rejects_required_cloze_answer_emptied_by_html_sanitation(self) -> None:
        data = _next_data(_page(103, "〜答", "JLPT5"))
        question = data["props"]["pageProps"]["included"]["studyQuestions"][0]
        question["question_type"] = "cloze"
        question["answer"] = "<script>REMOVED</script>"
        question["kanji_answer"] = ""
        payload = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
        page = (
            "<!doctype html><html><body><script id=\"__NEXT_DATA__\" "
            f"type=\"application/json\">{payload}</script></body></html>"
        ).encode()
        snapshot = _snapshot(self.folder / "snapshot.zip", [(103, "〜答", page)])

        with self.assertRaises(bunpro.BunproImportError) as raised:
            bunpro.convert_snapshot(snapshot, "official-fixture-r1")
        self.assertIn(
            "answer becomes empty after HTML sanitation",
            raised.exception.report["rejected_records"][0]["reason"],
        )

    def test_schema_drift_rejects_the_snapshot_instead_of_returning_partial_data(self) -> None:
        invalid = _next_data(_page(303, "〜なら", "JLPT3"))
        del invalid["props"]["pageProps"]["reviewable"]["meaning"]
        invalid_page = (
            "<script id=\"__NEXT_DATA__\" type=\"application/json\">"
            + json.dumps(invalid, ensure_ascii=False)
            + "</script>"
        ).encode()
        snapshot = _snapshot(
            self.folder / "snapshot.zip",
            [
                (101, "〜です", _page(101, "〜です", "JLPT5")),
                (303, "〜なら", invalid_page),
            ],
        )

        with self.assertRaises(bunpro.BunproImportError) as raised:
            bunpro.convert_snapshot(snapshot, "official-fixture-r1")

        report = raised.exception.report
        self.assertEqual(report["expected_record_count"], 2)
        self.assertEqual(report["imported_record_count"], 1)
        self.assertEqual(report["rejected_record_count"], 1)
        self.assertEqual(report["rejected_records"][0]["grammar_point_id"], 303)
        self.assertIn("meaning", report["rejected_records"][0]["reason"])

    def test_foreign_grammar_relation_is_rejected_as_schema_drift(self) -> None:
        data = _next_data(_page(304, "〜関係", "JLPT3"))
        relation = data["props"]["pageProps"]["included"]["relatedContents"][0]
        relation["first_relatable"]["id"] = 9001
        relation["second_relatable"]["id"] = 9002
        payload = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
        page = (
            "<!doctype html><html><body><script id=\"__NEXT_DATA__\" "
            f"type=\"application/json\">{payload}</script></body></html>"
        ).encode()
        snapshot = _snapshot(self.folder / "snapshot.zip", [(304, "〜関係", page)])

        with self.assertRaises(bunpro.BunproImportError) as raised:
            bunpro.convert_snapshot(snapshot, "official-fixture-r1")
        self.assertIn(
            "does not belong to grammar point 304",
            raised.exception.report["rejected_records"][0]["reason"],
        )

    def test_foreign_grammar_to_vocab_relation_is_not_silently_excluded(self) -> None:
        data = _next_data(_page(305, "〜所有", "JLPT3"))
        relation = data["props"]["pageProps"]["included"]["relatedContents"][0]
        relation["first_relatable"]["id"] = 9001
        relation["second_relatable"]["type_snake"] = "vocab"
        payload = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
        page = (
            "<!doctype html><html><body><script id=\"__NEXT_DATA__\" "
            f"type=\"application/json\">{payload}</script></body></html>"
        ).encode()
        snapshot = _snapshot(self.folder / "snapshot.zip", [(305, "〜所有", page)])

        with self.assertRaises(bunpro.BunproImportError) as raised:
            bunpro.convert_snapshot(snapshot, "official-fixture-r1")
        self.assertIn(
            "does not belong to grammar point 305",
            raised.exception.report["rejected_records"][0]["reason"],
        )

    def test_readonly_complete_example_does_not_require_a_separate_answer(self) -> None:
        data = _next_data(_page(303, "〜なら", "JLPT3"))
        question = data["props"]["pageProps"]["included"]["studyQuestions"][0]
        question["content"] = "これは答えを含む完成した例文です。"
        question["answer"] = ""
        question["kanji_answer"] = ""
        question["question_type"] = "readonly"
        payload = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
        page = (
            '<script id="__NEXT_DATA__" type="application/json">'
            + payload
            + "</script>"
        ).encode("utf-8")
        snapshot = _snapshot(self.folder / "snapshot.zip", [(303, "〜なら", page)])

        bundle, report = bunpro.convert_snapshot(snapshot, "official-fixture-r1")

        self.assertEqual(report["rejected_record_count"], 0)
        example = bundle.senses[0].examples[0].to_dict()
        self.assertIsNone(example["japanese"]["answer"])
        self.assertIsNone(example["japanese"]["kanji_answer"])

    def test_preserves_non_jlpt_and_kansai_source_level_labels(self) -> None:
        snapshot = _snapshot(
            self.folder / "snapshot.zip",
            [
                (601, "〜俗語", _page(601, "〜俗語", "Non-JLPT")),
                (602, "〜関西", _page(602, "〜関西", "関西弁")),
            ],
        )

        bundle, report = bunpro.convert_snapshot(snapshot, "official-fixture-r1")

        self.assertEqual(
            {sense.level_labels for sense in bundle.senses},
            {("Non-JLPT",), ("関西弁",)},
        )
        self.assertEqual(report["levels"], {"Non-JLPT": 1, "関西弁": 1})

    def test_normalizes_incidental_blanks_and_excludes_draft_examples(self) -> None:
        data = _next_data(_page(603, "〜空白", "JLPT2"))
        page_props = data["props"]["pageProps"]
        page_props["reviewable"]["title"] = " 〜空白 "
        question = page_props["included"]["studyQuestions"][0]
        question["alternate_grammar"] = ["", "〜空白"]
        question["word_prompt"] = "\u3000"
        page_props["included"]["studyQuestions"].append(
            {
                **question,
                "id": 99_603,
                "question_type": "draft",
                "content": "合成の未完成例文。",
                "answer": None,
                "kanji_answer": None,
            }
        )
        links = page_props["included"]["supplementalLinks"]
        links[0]["site"] = " Synthetic reference "
        links.append(
            {
                "id": 88_603,
                "site": "",
                "description": "",
                "link": "https://example.org/unlabelled",
                "readable_type": "SupplementalLink",
            }
        )
        payload = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
        page = (
            "<!doctype html><html><body><script id=\"__NEXT_DATA__\" "
            f"type=\"application/json\">{payload}</script></body></html>"
        ).encode()
        snapshot = _snapshot(self.folder / "snapshot.zip", [(603, "〜空白", page)])

        bundle, report = bunpro.convert_snapshot(snapshot, "official-fixture-r1")

        self.assertEqual(bundle.records[0].raw_expression, "〜空白")
        self.assertEqual(len(bundle.senses[0].examples), 1)
        self.assertEqual(report["excluded_example_rows"], 1)
        japanese = bundle.senses[0].examples[0].japanese
        self.assertEqual(japanese["alternate_grammar"], (("〜空白",),))
        self.assertNotIn("word_prompt", japanese)
        labels = {link.label for link in bundle.senses[0].links}
        self.assertIn("Reference: Synthetic reference — Further reading", labels)
        self.assertIn("Reference", labels)

    def test_unknown_example_validation_status_is_rejected_as_schema_drift(self) -> None:
        data = _next_data(_page(605, "〜状態", "JLPT2"))
        question = data["props"]["pageProps"]["included"]["studyQuestions"][0]
        question["validation_status"] = "draft"
        payload = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
        page = (
            "<!doctype html><html><body><script id=\"__NEXT_DATA__\" "
            f"type=\"application/json\">{payload}</script></body></html>"
        ).encode()
        snapshot = _snapshot(self.folder / "snapshot.zip", [(605, "〜状態", page)])

        with self.assertRaises(bunpro.BunproImportError) as raised:
            bunpro.convert_snapshot(snapshot, "official-fixture-r1")
        self.assertIn(
            "unsupported study question validation status",
            raised.exception.report["rejected_records"][0]["reason"],
        )

    def test_badge_only_page_is_rejected_as_non_substantive(self) -> None:
        data = _next_data(_page(604, "〜要約のみ", "JLPT2"))
        page_props = data["props"]["pageProps"]
        page_props["reviewable"]["casual_structure"] = None
        page_props["reviewable"]["polite_structure"] = None
        page_props["included"]["writeups"] = []
        page_props["included"]["studyQuestions"] = []
        payload = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
        page = (
            "<!doctype html><html><body><script id=\"__NEXT_DATA__\" "
            f"type=\"application/json\">{payload}</script></body></html>"
        ).encode()
        snapshot = _snapshot(self.folder / "snapshot.zip", [(604, "〜要約のみ", page)])

        with self.assertRaisesRegex(bunpro.BunproImportError, "partial Bunpro import"):
            bunpro.convert_snapshot(snapshot, "official-fixture-r1")

    def test_snapshot_member_hash_and_exact_member_set_are_verified(self) -> None:
        snapshot = _snapshot(
            self.folder / "snapshot.zip",
            [(101, "〜です", _page(101, "〜です", "JLPT5"))],
        )
        with zipfile.ZipFile(snapshot) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        page_name = next(name for name in members if name.startswith("pages/"))
        members[page_name] += b"tampered"
        members["unlisted-private.bin"] = b"must be rejected"
        _write_zip(snapshot, members)

        with self.assertRaisesRegex(bunpro.BunproImportError, "unexpected archive members"):
            bunpro.convert_snapshot(snapshot, "official-fixture-r1")

    def test_snapshot_read_cannot_be_redirected_after_regular_file_validation(self) -> None:
        snapshot = _snapshot(
            self.folder / "snapshot.zip",
            [(101, "〜安全", _page(101, "〜安全", "JLPT5"))],
        )
        replacement = _snapshot(
            self.folder / "replacement.zip",
            [(202, "〜置換", _page(202, "〜置換", "JLPT1"))],
        )
        original_open = bunpro.os.open
        swapped = False

        def swap_after_open(
            path: str,
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            nonlocal swapped
            descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
            if path == snapshot.name and dir_fd is not None:
                snapshot.unlink()
                snapshot.symlink_to(replacement)
                swapped = True
            return descriptor

        with patch.object(bunpro.os, "open", swap_after_open):
            bundle, _ = bunpro.convert_snapshot(snapshot, "official-fixture-r1")

        self.assertTrue(swapped)
        self.assertEqual(
            [record.source_record_id for record in bundle.records],
            ["bunpro:official-fixture-r1:record:101"],
        )

    def test_sitemap_index_pagination_fetch_retry_and_cache_reuse(self) -> None:
        root = "https://bunpro.jp/sitemap.xml"
        child_a = "https://bunpro.jp/sitemaps/a.xml"
        child_b = "https://bunpro.jp/sitemaps/b.xml"
        page_a = "https://bunpro.jp/grammar_points/a"
        page_b = "https://bunpro.jp/grammar_points/b"
        responses = {
            root: _sitemap_index([child_a, child_b]),
            child_a: _sitemap([page_a]),
            child_b: _sitemap([page_b]),
            page_a: _page(1, "a", "JLPT5"),
            page_b: _page(2, "b", "JLPT1"),
        }
        attempts: dict[str, int] = {}

        def flaky_fetch(url: str, _timeout: float) -> bytes:
            attempts[url] = attempts.get(url, 0) + 1
            if url == page_b and attempts[url] < 3:
                raise URLError("synthetic transient error")
            return responses[url]

        snapshot = self.folder / "fetched.zip"
        report = bunpro.fetch_snapshot(
            root,
            snapshot,
            self.folder / "cache",
            fetch_bytes=flaky_fetch,
            retries=2,
            retry_delay=lambda _seconds: None,
        )

        self.assertEqual(attempts[page_b], 3)
        self.assertEqual(report["sitemap_count"], 3)
        self.assertEqual(report["expected_page_count"], 2)
        self.assertEqual(report["fetched_page_count"], 2)
        self.assertEqual(report["cached_page_count"], 0)
        self.assertEqual(report["failed_pages"], [])
        bundle, import_report = bunpro.convert_snapshot(snapshot, "official-fixture-r1")
        self.assertEqual(len(bundle.records), 2)
        self.assertEqual(import_report["rejected_record_count"], 0)

        def no_network(_url: str, _timeout: float) -> bytes:
            raise AssertionError("valid cache should prevent a network request")

        cached_report = bunpro.fetch_snapshot(
            root,
            self.folder / "cached.zip",
            self.folder / "cache",
            fetch_bytes=no_network,
            retries=0,
            retry_delay=lambda _seconds: None,
        )
        self.assertEqual(cached_report["cached_page_count"], 2)
        self.assertEqual(cached_report["cached_sitemap_count"], 3)

    def test_snapshot_rejects_duplicate_page_discovery_across_sitemaps(self) -> None:
        root = "https://bunpro.jp/sitemap.xml"
        child_a = "https://bunpro.jp/sitemaps/a.xml"
        child_b = "https://bunpro.jp/sitemaps/b.xml"
        page_url = "https://bunpro.jp/grammar_points/a"
        documents = {
            root: _sitemap_index([child_a, child_b]),
            child_a: _sitemap([page_url]),
            child_b: _sitemap([page_url]),
        }
        page = _page(1, "a", "JLPT5")
        members: dict[str, bytes] = {}
        sitemap_entries = []
        for url, content in documents.items():
            member = f"sitemaps/{hashlib.sha256(url.encode()).hexdigest()}.xml"
            members[member] = content
            sitemap_entries.append(
                {"url": url, "path": member, "sha256": hashlib.sha256(content).hexdigest()}
            )
        page_member = f"pages/{hashlib.sha256(page_url.encode()).hexdigest()}.html"
        members[page_member] = page
        members["snapshot.json"] = _json_bytes(
            {
                "format": "bunpro-html-snapshot",
                "format_version": 1,
                "source_id": "bunpro",
                "root_sitemap_url": root,
                "sitemaps": sitemap_entries,
                "pages": [
                    {
                        "url": page_url,
                        "path": page_member,
                        "sha256": hashlib.sha256(page).hexdigest(),
                        "grammar_point_id": 1,
                    }
                ],
            }
        )
        snapshot = self.folder / "duplicate-discovery.zip"
        _write_zip(snapshot, members)

        with self.assertRaisesRegex(
            bunpro.BunproImportError,
            "page manifest differs from sitemap",
        ):
            bunpro.convert_snapshot(snapshot, "official-fixture-r1")

    def test_fetch_rejects_an_aggregate_snapshot_above_the_import_limit(self) -> None:
        root = "https://bunpro.jp/sitemap.xml"
        page_a = "https://bunpro.jp/grammar_points/a"
        page_b = "https://bunpro.jp/grammar_points/b"
        sitemap = _sitemap([page_a, page_b])
        first = _page(801, "a", "JLPT5")
        responses = {root: sitemap, page_a: first, page_b: _page(802, "b", "JLPT5")}
        snapshot = self.folder / "oversized.zip"

        with (
            patch.object(
                bunpro,
                "MAX_UNCOMPRESSED_SNAPSHOT_BYTES",
                len(sitemap) + len(first) + 1,
            ),
            self.assertRaisesRegex(bunpro.BunproFetchError, "aggregate"),
        ):
            bunpro.fetch_snapshot(
                root,
                snapshot,
                self.folder / "cache",
                fetch_bytes=lambda url, _timeout: responses[url],
                retries=0,
            )

        self.assertFalse(snapshot.exists())

    def test_malformed_url_and_excessive_html_nesting_fail_as_adapter_errors(self) -> None:
        with self.assertRaises(bunpro.BunproError):
            bunpro.fetch_snapshot(
                "https://[::1",
                self.folder / "bad-url.zip",
                self.folder / "cache",
                fetch_bytes=lambda _url, _timeout: b"unused",
                retries=0,
            )

        nested = "<div>" * 1_000 + "Synthetic text" + "</div>" * 1_000
        with self.assertRaisesRegex(bunpro.BunproError, "nesting"):
            bunpro._safe_html(nested, "deep fixture")

    def test_request_url_percent_encodes_unicode_without_double_encoding(self) -> None:
        self.assertEqual(
            bunpro._request_url("https://bunpro.jp/grammar_points/とは"),
            "https://bunpro.jp/grammar_points/%E3%81%A8%E3%81%AF",
        )
        self.assertEqual(
            bunpro._request_url("https://bunpro.jp/grammar_points/%E3%81%A8"),
            "https://bunpro.jp/grammar_points/%E3%81%A8",
        )

    def test_cli_converts_an_explicit_local_snapshot_and_writes_report(self) -> None:
        snapshot = _snapshot(
            self.folder / "snapshot.zip",
            [(101, "〜です", _page(101, "〜です", "JLPT5"))],
        )
        output = self.folder / "canonical.json"
        report = self.folder / "report.json"

        result = bunpro.main(
            [
                "convert",
                "--snapshot",
                str(snapshot),
                "--revision",
                "official-fixture-r1",
                "--output",
                str(output),
                "--report",
                str(report),
            ]
        )

        self.assertEqual(result, 0)
        SourceBundle.from_dict(json.loads(output.read_text(encoding="utf-8")))
        report_data = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(report_data["imported_record_count"], 1)
        self.assertEqual(
            report_data["snapshot_sha256"],
            hashlib.sha256(snapshot.read_bytes()).hexdigest(),
        )


if __name__ == "__main__":
    unittest.main()

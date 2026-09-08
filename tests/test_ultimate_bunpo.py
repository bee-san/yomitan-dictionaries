"""Bee 文法 adapter regressions use synthetic source text and media only."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import warnings
import zipfile

from scripts.grammar.model import SourceBundle
from scripts.grammar.sources.bunpo import (
    AI_WARNING,
    BunpoAdapterError,
    adapt_archive,
    write_import,
)


ROOT = Path(__file__).resolve().parents[1]
REVISION = "fixture-r1"
RIGHTS = (
    "Synthetic fixture; no redistribution licence is asserted.\n"
    "Preserve this second source-rights line exactly.\n"
)
JPEG_A = b"\xff\xd8\xff\xe0SYNTHETIC-A\xff\xd9"
JPEG_B = b"\xff\xd8\xff\xe0SYNTHETIC-B\xff\xd9"


def stable_json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def field(name, content):
    return {
        "tag": "div",
        "data": {"field": name},
        "content": [
            {"tag": "div", "style": {"fontWeight": "bold"}, "content": name},
            {"tag": "div", "content": content},
        ],
    }


def glossary(fields, *, include_warning=True):
    sections = []
    warning_added = False
    for name, content in fields:
        if include_warning and name.startswith("AI") and not warning_added:
            sections.append(
                {
                    "tag": "div",
                    "content": AI_WARNING,
                    "style": {"fontWeight": "bold"},
                }
            )
            warning_added = True
        sections.append(field(name, content))
    return [
        {
            "type": "structured-content",
            "content": {"tag": "div", "content": sections},
        }
    ]


def term_row(term, sequence, value, tags="N3 文法 AI"):
    return [term, "", tags, "", 0, value, sequence, ""]


def fixture_rows():
    first = glossary(
        [
            (
                "文型",
                [
                    {
                        "tag": "ruby",
                        "content": ["語", {"tag": "rt", "content": "ご"}],
                    },
                    "〜ば〜ほど",
                ],
            ),
            (
                "意味",
                [
                    "Synthetic meaning. ",
                    {
                        "tag": "a",
                        "href": "https://example.invalid/grammar/7",
                        "content": "Synthetic source",
                    },
                ],
            ),
            (
                "接続",
                [{"tag": "img", "path": "media/source-a.jpg", "alt": "Chart A"}],
            ),
            ("JLPTレベル", ["N3"]),
            ("例文1", ["Synthetic Japanese example."]),
            ("AI例文1", ["Synthetic generated Japanese example."]),
            ("AI英訳1", ["Synthetic generated English translation."]),
            ("AI意味", ["Synthetic generated meaning."]),
            (
                "Future semantic field",
                [{"tag": "img", "path": "media/source-b.jpg", "alt": "Chart B"}],
            ),
        ]
    )
    second = glossary(
        [
            ("文型", ["〜か"]),
            ("意味", ["Synthetic short-pattern meaning."]),
            ("AI意味", ["Synthetic generated short-pattern meaning."]),
        ]
    )
    return [
        term_row("〜ば〜ほど", 7, first),
        term_row("ば〜ほど", 7, first),
        term_row("〜か", 8, second, "N5 文法 AI"),
        term_row("か", 8, second, "N5 文法 AI"),
    ]


def write_archive(path, rows=None, *, extra_members=None):
    rows = rows or fixture_rows()
    sequences = {row[6] for row in rows}
    index = {
        "title": "Synthetic 文法",
        "revision": REVISION,
        "format": 3,
        "sequenced": True,
        "sourceLanguage": "ja",
        "targetLanguage": "ja",
        "description": "Synthetic fixture only.",
        "attribution": "Synthetic fixture attribution from the archive.",
    }
    source = {
        "source_file": "synthetic.apkg",
        "source_sha256": "0" * 64,
        "source_notes": len(sequences),
        "lookup_entries": len(rows),
        "alias_rows": len(rows) - len(sequences),
        "nonempty_fields": 12,
        "field_counts": {},
        "retained_images": ["source-a.jpg", "source-b.jpg"],
        "omitted_media": [],
        "rights": RIGHTS.strip(),
    }
    members = {
        "RIGHTS.txt": RIGHTS.encode("utf-8"),
        "SOURCE.json": stable_json(source),
        "index.json": stable_json(index),
        "media/source-a.jpg": JPEG_A,
        "media/source-b.jpg": JPEG_B,
        "tag_bank_1.json": stable_json([]),
        "term_bank_1.json": stable_json(rows),
    }
    members.update(extra_members or {})
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(members.items()):
            archive.writestr(name, content)
    return path


def archive_baseline(path):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        index = json.loads(archive.read("index.json"))
        source = json.loads(archive.read("SOURCE.json"))
        rights = archive.read("RIGHTS.txt").decode("utf-8")
        member_hashes = {
            name: hashlib.sha256(archive.read(name)).hexdigest() for name in names
        }
        media_inventory = []
        for name in sorted(item for item in names if item.startswith("media/")):
            info = archive.getinfo(name)
            content = archive.read(name)
            media_inventory.append(
                {
                    "path": name,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size": len(content),
                    "crc32": f"{info.CRC:08x}",
                    "content_type": "image/jpeg",
                }
            )
    return {
        "archive": {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "member_names": names,
        },
        "declared": {
            "index": index,
            "source": source,
            "rights_text": rights,
            "member_hashes": member_hashes,
            "media_inventory": media_inventory,
        },
    }


class BunpoAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ultimate-bunpo-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = write_archive(self.root / "bunpo.zip")
        self.archive_sha256 = hashlib.sha256(self.archive.read_bytes()).hexdigest()

    def adapt(self, path=None, **kwargs):
        return adapt_archive(
            path or self.archive,
            expected_sha256=kwargs.pop("expected_sha256", self.archive_sha256),
            expected_revision=kwargs.pop("expected_revision", REVISION),
            **kwargs,
        )

    def test_preserves_fields_aliases_links_furigana_ai_examples_and_media(self):
        original = self.archive.read_bytes()

        result = self.adapt()
        result.bundle.validate()

        self.assertEqual(self.archive.read_bytes(), original)
        self.assertEqual(len(result.bundle.records), 2)
        self.assertEqual(len(result.bundle.senses), 2)
        self.assertEqual(
            {record.source_record_id for record in result.bundle.records},
            {
                "bee-bunpo:fixture-r1:record:sequence:7",
                "bee-bunpo:fixture-r1:record:sequence:8",
            },
        )
        first_record = result.bundle.records[0]
        self.assertEqual(first_record.raw_expression, "〜ば〜ほど")
        self.assertIn("Future semantic field", first_record.field_hashes)
        self.assertIn("lookup-row:2", first_record.field_hashes)

        aliases = {(item.source_sequence, item.surface): item for item in result.aliases}
        self.assertEqual(len(aliases), 4)
        self.assertEqual(aliases[(7, "〜ば〜ほど")].kind, "source-expression")
        self.assertEqual(aliases[(7, "ば〜ほど")].scan_mode, "manual-search-only")
        self.assertEqual(aliases[(8, "か")].scan_mode, "quarantined")
        self.assertEqual(aliases[(8, "か")].definition_tags, "N5 文法 AI")
        canonical_alias_blocks = [
            block
            for sense in result.bundle.senses
            for block in sense.blocks
            if block.content.get("role") == "source-lookup-aliases"
        ]
        self.assertEqual(len(canonical_alias_blocks), 2)
        self.assertEqual(
            {
                row["surface"]
                for block in canonical_alias_blocks
                for row in block.content["rows"]
            },
            {"〜ば〜ほど", "ば〜ほど", "〜か", "か"},
        )

        first_sense = next(
            sense for sense in result.bundle.senses if sense.source_record_id.endswith(":7")
        )
        self.assertEqual(first_sense.level_labels, ("N3",))
        self.assertEqual(
            [link.url for link in first_sense.links],
            ["https://example.invalid/grammar/7"],
        )
        self.assertIn("ruby", repr([block.to_dict() for block in first_sense.blocks]))
        generated_blocks = [block for block in first_sense.blocks if block.generated]
        self.assertTrue(generated_blocks)
        self.assertTrue(
            all("field:AI" in block.provenance.locator for block in generated_blocks)
        )
        warning_blocks = [
            block
            for block in first_sense.blocks
            if block.provenance.locator.endswith(":ai-warning")
        ]
        self.assertEqual(len(warning_blocks), 1)
        self.assertIn(AI_WARNING, repr(warning_blocks[0].to_dict()))

        source_example = next(
            item for item in first_sense.examples if item.example_id.endswith(":source-1")
        )
        ai_example = next(
            item for item in first_sense.examples if item.example_id.endswith(":ai-1")
        )
        self.assertIsNone(source_example.translation)
        self.assertEqual(ai_example.translation_language, "en")
        self.assertTrue(ai_example.generated)

        expected_media = {
            hashlib.sha256(JPEG_A).hexdigest(): JPEG_A,
            hashlib.sha256(JPEG_B).hexdigest(): JPEG_B,
        }
        self.assertEqual(len(result.bundle.media), 2)
        self.assertEqual(
            {media.content_sha256 for media in result.bundle.media}, set(expected_media)
        )
        for path, content in result.media_files.items():
            digest = Path(path).stem
            self.assertEqual(content, expected_media[digest])
            self.assertEqual(path, f"media/{digest}.jpg")
        transformed = repr([block.to_dict() for block in first_sense.blocks])
        self.assertNotIn("media/source-a.jpg", transformed)
        self.assertIn(hashlib.sha256(JPEG_A).hexdigest(), transformed)
        self.assertEqual(
            result.report["media_path_map"]["media/source-a.jpg"],
            f"media/{hashlib.sha256(JPEG_A).hexdigest()}.jpg",
        )

        counts = result.report["counts"]
        self.assertEqual(counts["source_notes"], 2)
        self.assertEqual(counts["lookup_rows"], 4)
        self.assertEqual(counts["alias_rows"], 2)
        self.assertEqual(counts["media"], 2)
        self.assertEqual(counts["source_links"], 1)
        self.assertEqual(counts["furigana_sequences"], 1)
        self.assertEqual(counts["ai_warning_sections"], 2)
        self.assertIn("Bee later declared", result.bundle.source_revision.provenance_note)
        self.assertEqual(
            result.bundle.source_revision.license.notice,
            " ".join(RIGHTS.splitlines()),
        )
        self.assertEqual(result.report["archive"]["rights_text"], RIGHTS)
        self.assertIn(
            "rights-newline-normalization",
            {item["kind"] for item in result.report["transformations"]},
        )

    def test_duplicate_alias_within_one_sequence_is_rejected(self):
        rows = fixture_rows()
        rows.insert(2, copy.deepcopy(rows[1]))
        archive = write_archive(self.root / "duplicate-alias.zip", rows)

        with self.assertRaisesRegex(BunpoAdapterError, "duplicate alias"):
            self.adapt(
                archive,
                expected_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
            )

    def test_one_sequence_cannot_hide_different_semantic_payloads(self):
        rows = fixture_rows()
        rows[1] = copy.deepcopy(rows[1])
        rows[1][5] = glossary([("文型", ["different"]), ("意味", ["different"])])
        archive = write_archive(self.root / "divergent-sequence.zip", rows)

        with self.assertRaisesRegex(BunpoAdapterError, "sequence 7.*disagree"):
            self.adapt(
                archive,
                expected_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
            )

    def test_duplicate_or_unsafe_archive_members_are_rejected(self):
        duplicate = write_archive(self.root / "duplicate-member.zip")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(duplicate, "a") as archive:
                archive.writestr("index.json", b"{}")
        unsafe = write_archive(
            self.root / "unsafe-member.zip", extra_members={"../escape": b"bad"}
        )

        for archive, message in (
            (duplicate, "duplicate archive member"),
            (unsafe, "unsafe archive member"),
        ):
            with self.subTest(archive=archive.name), self.assertRaisesRegex(
                BunpoAdapterError, message
            ):
                self.adapt(
                    archive,
                    expected_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
                )

    def test_malicious_structured_content_is_rejected_not_copied(self):
        rows = fixture_rows()
        malicious_glossary = glossary(
            [
                ("文型", ["〜unsafe"]),
                ("意味", [{"tag": "script", "content": "run()"}]),
                ("AI意味", ["generated"]),
            ]
        )
        rows[0][5] = malicious_glossary
        rows[1][5] = malicious_glossary
        archive = write_archive(self.root / "malicious-content.zip", rows)

        with self.assertRaisesRegex(BunpoAdapterError, "unsupported structured-content tag"):
            self.adapt(
                archive,
                expected_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
            )

    def test_extra_field_children_are_rejected_instead_of_silently_dropped(self):
        rows = fixture_rows()
        glossary_value = copy.deepcopy(rows[0][5])
        glossary_value[0]["content"]["content"][0]["content"].append(
            {"tag": "span", "content": "silently dropped sentinel"}
        )
        rows[0][5] = glossary_value
        rows[1][5] = glossary_value
        archive = write_archive(self.root / "extra-field-child.zip", rows)

        with self.assertRaisesRegex(BunpoAdapterError, "malformed content"):
            self.adapt(
                archive,
                expected_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
            )

    def test_malformed_rows_and_missing_ai_warning_fail_loudly(self):
        malformed_rows = fixture_rows()
        malformed_rows[0] = malformed_rows[0][:-1]
        malformed = write_archive(self.root / "malformed-row.zip", malformed_rows)
        missing_warning_rows = fixture_rows()
        missing_warning_rows[2][5] = glossary(
            [("文型", ["〜か"]), ("AI意味", ["generated"])],
            include_warning=False,
        )
        missing_warning_rows[3][5] = missing_warning_rows[2][5]
        missing_warning = write_archive(
            self.root / "missing-warning.zip", missing_warning_rows
        )

        for archive, message in (
            (malformed, "term row"),
            (missing_warning, "AI warning"),
        ):
            with self.subTest(archive=archive.name), self.assertRaisesRegex(
                BunpoAdapterError, message
            ):
                self.adapt(
                    archive,
                    expected_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
                )

    def test_baseline_manifests_check_identity_and_digests_not_only_counts(self):
        first = self.adapt()
        baseline = archive_baseline(self.archive)
        coverage = copy.deepcopy(first.report["coverage"])
        baseline_path = self.root / "baseline.json"
        coverage_path = self.root / "coverage.json"
        baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
        coverage_path.write_text(json.dumps(coverage), encoding="utf-8")

        verified = self.adapt(
            baseline_manifest=baseline_path,
            coverage_manifest=coverage_path,
        )
        self.assertTrue(verified.report["manifest_verification"]["exact_match"])

        coverage["sequence_summaries"][0]["sequence"] = 999
        coverage_path.write_text(json.dumps(coverage), encoding="utf-8")
        with self.assertRaisesRegex(BunpoAdapterError, "sequence_summaries"):
            self.adapt(
                baseline_manifest=baseline_path,
                coverage_manifest=coverage_path,
            )

        coverage = copy.deepcopy(first.report["coverage"])
        coverage["field_records"][0]["content_sha256"] = "f" * 64
        coverage_path.write_text(json.dumps(coverage), encoding="utf-8")
        with self.assertRaisesRegex(BunpoAdapterError, "field_records"):
            self.adapt(
                baseline_manifest=baseline_path,
                coverage_manifest=coverage_path,
            )

    def test_write_import_outputs_valid_canonical_json_report_and_exact_media(self):
        result = self.adapt()
        canonical = self.root / "out" / "bee-bunpo.canonical.json"
        report = self.root / "out" / "bee-bunpo.report.json"
        media_root = self.root / "out" / "media-files"

        write_import(result, canonical, report, media_root)

        SourceBundle.from_dict(json.loads(canonical.read_text(encoding="utf-8")))
        report_value = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(report_value["source_content_sha256"], self.archive_sha256)
        self.assertNotIn(str(self.root), report.read_text(encoding="utf-8"))
        for relative_path, content in result.media_files.items():
            self.assertEqual((media_root / relative_path).read_bytes(), content)

    def test_write_import_refuses_to_overwrite_the_source_archive(self):
        result = self.adapt()
        original = self.archive.read_bytes()

        with self.assertRaisesRegex(BunpoAdapterError, "source archive"):
            write_import(
                result,
                self.archive,
                self.root / "report.json",
                self.root / "media",
            )

        self.assertEqual(self.archive.read_bytes(), original)

    def test_cli_emits_only_summary_and_keeps_private_payloads_in_requested_paths(self):
        canonical = self.root / "cli" / "canonical.json"
        report = self.root / "cli" / "report.json"
        media_root = self.root / "cli" / "media"
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/grammar/sources/bunpo.py"),
                str(self.archive),
                "--expected-sha256",
                self.archive_sha256,
                "--expected-revision",
                REVISION,
                "--output",
                str(canonical),
                "--report",
                str(report),
                "--media-dir",
                str(media_root),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["source_notes"], 2)
        self.assertEqual(summary["lookup_rows"], 4)
        self.assertNotIn("Synthetic meaning", result.stdout)
        self.assertTrue(canonical.is_file())
        self.assertTrue(report.is_file())


if __name__ == "__main__":
    unittest.main()

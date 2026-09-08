"""Foundation regressions use synthetic source payloads only."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.grammar.build import BuildError, build_from_manifest
from scripts.grammar.model import (
    BuildMode,
    CanonicalSense,
    ContentBlock,
    ImportMode,
    LicenseInfo,
    PartitionStatus,
    Provenance,
    PublicationMode,
    SelectionMode,
    SourceRevision,
    SourceSense,
    canonical_sense_id,
)
from scripts.grammar.registry import (
    REGISTRY_VERSION,
    PolicyError,
    get_source,
    validate_remote_url,
    validate_selection,
)


ROOT = Path(__file__).resolve().parents[1]
SHA_A = "a" * 64
SHA_B = "b" * 64


def revision_dict(*, publication_mode="denied"):
    return {
        "source_id": "bee-bunpo",
        "revision_id": "fixture-r1",
        "content_sha256": SHA_A,
        "languages": ["ja", "en"],
        "attribution": "Synthetic fixture attribution",
        "license": {
            "identifier": None,
            "notice": "Synthetic fixture; no third-party source text.",
            "evidence_url": None,
        },
        "access_mode": "local-file",
        "import_mode": "content",
        "publication_mode": publication_mode,
        "generated": False,
        "provenance_note": "Synthetic fixture only.",
    }


def source_bundle():
    record_id = "bee-bunpo:fixture-r1:record:1"
    sense_id = f"{record_id}:sense:1"
    provenance = {
        "source_id": "bee-bunpo",
        "revision_id": "fixture-r1",
        "source_record_id": record_id,
        "locator": "field:meaning",
        "content_sha256": SHA_B,
    }
    return {
        "format": "ugd-canonical-source",
        "format_version": 1,
        "source_revision": revision_dict(),
        "records": [
            {
                "source_record_id": record_id,
                "raw_expression": "〜ために",
                "order": 1,
                "field_hashes": {"meaning": SHA_B},
            }
        ],
        "senses": [
            {
                "source_sense_id": sense_id,
                "source_record_id": record_id,
                "partition_status": "source-explicit",
                "concept_ref": None,
                "blocks": [
                    {
                        "block_id": f"{sense_id}:block:meaning",
                        "kind": "meaning",
                        "language": "en",
                        "content": ["Synthetic explanation"],
                        "order": 1,
                        "provenance": provenance,
                        "generated": False,
                    }
                ],
                "examples": [],
                "level_labels": ["JLPT:N3"],
                "register_labels": [],
                "links": [],
            }
        ],
        "media": [],
    }


class CanonicalModelTests(unittest.TestCase):
    def test_source_qualified_senses_keep_same_expression_distinct(self):
        first = "bee-bunpo:fixture-r1:record:110:sense:1"
        second = "bee-bunpo:fixture-r1:record:111:sense:1"

        first_id = canonical_sense_id([first])
        second_id = canonical_sense_id([second])

        self.assertNotEqual(first_id, second_id)
        self.assertEqual(first_id, canonical_sense_id([first]))
        self.assertNotEqual(
            CanonicalSense(first_id, "〜ために", (first,), "cause").sense_id,
            CanonicalSense(second_id, "〜ために", (second,), "purpose").sense_id,
        )

    def test_generated_block_retains_language_and_exact_provenance(self):
        provenance = Provenance(
            source_id="bee-bunpo",
            revision_id="fixture-r1",
            source_record_id="bee-bunpo:fixture-r1:record:1",
            locator="field:AI意味",
            content_sha256=SHA_B,
        )
        block = ContentBlock(
            block_id="bee-bunpo:fixture-r1:record:1:sense:1:block:ai-meaning",
            kind="meaning",
            language="ja",
            content=("Synthetic generated text",),
            order=2,
            provenance=provenance,
            generated=True,
        )

        self.assertTrue(block.generated)
        self.assertEqual(block.language, "ja")
        self.assertEqual(block.provenance.locator, "field:AI意味")
        self.assertEqual(block.provenance.content_sha256, SHA_B)

    def test_revision_keeps_import_and_publication_policy_independent(self):
        revision = SourceRevision(
            source_id="bee-bunpo",
            revision_id="fixture-r1",
            content_sha256=SHA_A,
            languages=("ja",),
            attribution="Synthetic fixture attribution",
            license=LicenseInfo(None, "No redistribution grant", None),
            access_mode="local-file",
            import_mode=ImportMode.CONTENT,
            publication_mode=PublicationMode.DENIED,
            generated=False,
            provenance_note="Synthetic fixture only.",
        )

        self.assertEqual(revision.import_mode, ImportMode.CONTENT)
        self.assertEqual(revision.publication_mode, PublicationMode.DENIED)

    def test_sense_requires_source_qualified_identity(self):
        with self.assertRaisesRegex(ValueError, "source_sense_id"):
            SourceSense(
                source_sense_id="sense:1",
                source_record_id="bee-bunpo:fixture-r1:record:1",
                partition_status=PartitionStatus.SOURCE_EXPLICIT,
            )


class RegistryPolicyTests(unittest.TestCase):
    def test_policy_cross_product_fails_closed(self):
        cases = [
            ("bee-bunpo", BuildMode.PRIVATE, SelectionMode.CONTENT, True),
            ("bee-bunpo", BuildMode.PUBLISHABLE, SelectionMode.CONTENT, False),
            ("ninjal-bunkei", BuildMode.PRIVATE, SelectionMode.CONTENT, True),
            ("ninjal-bunkei", BuildMode.PUBLISHABLE, SelectionMode.CONTENT, True),
            ("imabi", BuildMode.PRIVATE, SelectionMode.CONTENT, False),
            ("imabi", BuildMode.PUBLISHABLE, SelectionMode.CONTENT, False),
            ("imabi", BuildMode.PRIVATE, SelectionMode.METADATA, True),
            ("imabi", BuildMode.PUBLISHABLE, SelectionMode.METADATA, True),
        ]
        for source_id, build_mode, selection_mode, allowed in cases:
            with self.subTest(source_id=source_id, build_mode=build_mode, selection_mode=selection_mode):
                if allowed:
                    validate_selection(source_id, build_mode, selection_mode)
                else:
                    with self.assertRaises(PolicyError):
                        validate_selection(source_id, build_mode, selection_mode)

    def test_unknown_source_is_never_selected(self):
        with self.assertRaisesRegex(PolicyError, "not in registry"):
            validate_selection("unknown-source", BuildMode.PRIVATE, SelectionMode.CONTENT)

    def test_yokubi_is_first_class_and_pinned_by_manifests(self):
        source = get_source("yokubi")
        self.assertEqual(source.license_identifier, "CC-BY-4.0")
        self.assertIn("raw.githubusercontent.com", source.allowed_hosts)
        self.assertIn("Sakubi", source.attribution)

    def test_remote_urls_require_https_and_an_allowlisted_host(self):
        validate_remote_url(
            "yokubi",
            "https://raw.githubusercontent.com/Morgawr/yokubi/main/src/SUMMARY.md",
        )
        for url in (
            "http://raw.githubusercontent.com/Morgawr/yokubi/main/src/SUMMARY.md",
            "https://example.org/yokubi.md",
        ):
            with self.subTest(url=url), self.assertRaises(PolicyError):
                validate_remote_url("yokubi", url)


class BuildCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="grammar-foundation-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.input_path = self.root / "source.json"
        self.input_path.write_text(
            json.dumps(source_bundle(), ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        self.input_sha256 = hashlib.sha256(self.input_path.read_bytes()).hexdigest()

    def write_manifest(self, *, build_mode=None, input_sha256=None, path="source.json"):
        manifest = {
            "format": "ugd-source-manifest",
            "format_version": 1,
            "registry_version": REGISTRY_VERSION,
            "sources": [
                {
                    "source_id": "bee-bunpo",
                    "revision_id": "fixture-r1",
                    "selection": "content",
                    "source_content_sha256": SHA_A,
                    "input": {
                        "path": path,
                        "sha256": input_sha256 or self.input_sha256,
                    },
                },
                {
                    "source_id": "imabi",
                    "revision_id": "metadata-current",
                    "selection": "metadata",
                },
            ],
        }
        if build_mode is not None:
            manifest["build_mode"] = build_mode
        path = self.root / "manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path

    def test_private_is_default_and_all_inputs_are_pinned(self):
        manifest = self.write_manifest()
        output = self.root / "build.json"
        report_path = self.root / "report.json"

        report = build_from_manifest(manifest, output, report_path)
        built = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(built["build_mode"], "private")
        self.assertEqual(built["registry_version"], REGISTRY_VERSION)
        self.assertEqual(built["content_source_count"], 1)
        self.assertEqual(built["metadata_source_count"], 1)
        self.assertEqual(built["sources"][0]["source_revision"]["content_sha256"], SHA_A)
        self.assertEqual(report["output_sha256"], hashlib.sha256(output.read_bytes()).hexdigest())
        self.assertNotIn(str(self.root), output.read_text(encoding="utf-8"))
        self.assertNotIn(str(self.root), report_path.read_text(encoding="utf-8"))

    def test_publishable_build_refuses_uncleared_content_before_writing(self):
        manifest = self.write_manifest(build_mode="publishable")
        output = self.root / "build.json"
        report = self.root / "report.json"

        with self.assertRaisesRegex(BuildError, "bee-bunpo.*publish"):
            build_from_manifest(manifest, output, report)

        self.assertFalse(output.exists())
        self.assertFalse(report.exists())

    def test_hash_mismatch_fails_before_writing(self):
        manifest = self.write_manifest(input_sha256="0" * 64)
        output = self.root / "build.json"

        with self.assertRaisesRegex(BuildError, "SHA-256 mismatch"):
            build_from_manifest(manifest, output, self.root / "report.json")
        self.assertFalse(output.exists())

    def test_relative_input_cannot_escape_manifest_directory(self):
        manifest = self.write_manifest(path="../source.json")
        with self.assertRaisesRegex(BuildError, "escape"):
            build_from_manifest(manifest, self.root / "build.json", self.root / "report.json")

    def test_build_output_is_deterministic(self):
        manifest = self.write_manifest()
        first = self.root / "first.json"
        second = self.root / "second.json"

        build_from_manifest(manifest, first, self.root / "first-report.json")
        build_from_manifest(manifest, second, self.root / "second-report.json")

        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_cli_help_documents_required_interface(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/grammar/build.py"), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for option in ("--source-manifest", "--output", "--report", "--mode"):
            self.assertIn(option, result.stdout)


if __name__ == "__main__":
    unittest.main()

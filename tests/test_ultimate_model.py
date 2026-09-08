"""Foundation regressions use synthetic source payloads only."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import URLError
from urllib.request import Request

import scripts.grammar.build as builder
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
    SourceBundle,
    SourceLink,
    SourceRecord,
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


def revision_dict(
    *,
    source_id="bee-bunpo",
    revision_id="fixture-r1",
    access_mode="local-file",
    publication_mode="denied",
    attribution="Synthetic fixture attribution",
    license_identifier=None,
    license_evidence_url=None,
):
    return {
        "source_id": source_id,
        "revision_id": revision_id,
        "content_sha256": SHA_A,
        "languages": ["ja", "en"],
        "attribution": attribution,
        "license": {
            "identifier": license_identifier,
            "notice": "Synthetic fixture; no third-party source text.",
            "evidence_url": license_evidence_url,
        },
        "access_mode": access_mode,
        "import_mode": "content",
        "publication_mode": publication_mode,
        "generated": False,
        "provenance_note": "Synthetic fixture only.",
    }


def source_bundle(
    *,
    source_id="bee-bunpo",
    revision_id="fixture-r1",
    access_mode="local-file",
    publication_mode="denied",
    attribution="Synthetic fixture attribution",
    license_identifier=None,
    license_evidence_url=None,
):
    record_id = f"{source_id}:{revision_id}:record:1"
    sense_id = f"{record_id}:sense:1"
    provenance = {
        "source_id": source_id,
        "revision_id": revision_id,
        "source_record_id": record_id,
        "locator": "field:meaning",
        "content_sha256": SHA_B,
    }
    return {
        "format": "ugd-canonical-source",
        "format_version": 1,
        "source_revision": revision_dict(
            source_id=source_id,
            revision_id=revision_id,
            access_mode=access_mode,
            publication_mode=publication_mode,
            attribution=attribution,
            license_identifier=license_identifier,
            license_evidence_url=license_evidence_url,
        ),
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
    def test_nested_canonical_content_is_immutable_after_validation(self):
        bundle = SourceBundle.from_dict(source_bundle())
        block = bundle.senses[0].blocks[0]

        with self.assertRaises((AttributeError, TypeError)):
            block.content.append("mutation")

        self.assertEqual(block.to_dict()["content"], ["Synthetic explanation"])

    def test_content_provenance_requires_an_exact_hash(self):
        value = source_bundle()
        del value["senses"][0]["blocks"][0]["provenance"]["content_sha256"]

        with self.assertRaisesRegex(ValueError, "content_sha256"):
            SourceBundle.from_dict(value)

    def test_block_id_must_be_qualified_by_its_source_sense(self):
        value = source_bundle()
        value["senses"][0]["blocks"][0]["block_id"] = "block:meaning"

        with self.assertRaisesRegex(ValueError, "block_id.*source_sense_id"):
            SourceBundle.from_dict(value)

    def test_example_id_must_be_qualified_by_its_source_sense(self):
        value = source_bundle()
        provenance = dict(value["senses"][0]["blocks"][0]["provenance"])
        value["senses"][0]["examples"] = [
            {
                "example_id": "example:1",
                "japanese": "例",
                "translation": "Synthetic example",
                "translation_language": "en",
                "order": 1,
                "provenance": provenance,
                "generated": False,
            }
        ]

        with self.assertRaisesRegex(ValueError, "example_id.*source_sense_id"):
            SourceBundle.from_dict(value)

    def test_media_id_must_be_qualified_by_its_source_revision(self):
        value = source_bundle()
        provenance = dict(value["senses"][0]["blocks"][0]["provenance"])
        value["media"] = [
            {
                "media_id": "media:chart",
                "source_path": "media/chart.png",
                "content_sha256": SHA_B,
                "media_type": "image/png",
                "byte_count": 1,
                "provenance": provenance,
            }
        ]

        with self.assertRaisesRegex(ValueError, "media_id.*source revision"):
            SourceBundle.from_dict(value)

    def test_media_path_rejects_windows_absolute_paths(self):
        value = source_bundle()
        provenance = dict(value["senses"][0]["blocks"][0]["provenance"])
        value["media"] = [
            {
                "media_id": "bee-bunpo:fixture-r1:media:chart",
                "source_path": r"C:\\Users\\alice\\private.png",
                "content_sha256": SHA_B,
                "media_type": "image/png",
                "byte_count": 1,
                "provenance": provenance,
            }
        ]

        with self.assertRaisesRegex(ValueError, "safe POSIX-relative"):
            SourceBundle.from_dict(value)

    def test_canonical_sense_id_must_match_exact_membership(self):
        source_sense_id = "bee-bunpo:fixture-r1:record:1:sense:1"

        with self.assertRaisesRegex(ValueError, "sense_id.*source_sense_ids"):
            CanonicalSense("arbitrary-id", "〜ために", (source_sense_id,), "purpose")

    def test_source_record_field_hashes_are_immutable(self):
        record = SourceRecord(
            "bee-bunpo:fixture-r1:record:1",
            "〜ために",
            1,
            {"meaning": SHA_B},
        )

        with self.assertRaises(TypeError):
            record.field_hashes["meaning"] = "0" * 64  # type: ignore[index]

    def test_duplicate_block_ids_are_rejected(self):
        value = source_bundle()
        value["senses"][0]["blocks"].append(dict(value["senses"][0]["blocks"][0]))

        with self.assertRaisesRegex(ValueError, "duplicate block_id"):
            SourceBundle.from_dict(value)

    def test_media_must_reference_a_known_source_record(self):
        value = source_bundle()
        value["media"].append(
            {
                "media_id": "bee-bunpo:fixture-r1:media:chart",
                "source_path": "media/chart.png",
                "content_sha256": SHA_B,
                "media_type": "image/png",
                "byte_count": 1,
                "provenance": {
                    "source_id": "bee-bunpo",
                    "revision_id": "fixture-r1",
                    "source_record_id": "bee-bunpo:fixture-r1:record:unknown",
                    "locator": "media:chart.png",
                    "content_sha256": SHA_B,
                },
            }
        )

        with self.assertRaisesRegex(ValueError, "media.*unknown source record"):
            SourceBundle.from_dict(value)

    def test_content_provenance_must_match_its_source_sense_record(self):
        value = source_bundle()
        value["senses"][0]["blocks"][0]["provenance"]["source_record_id"] = (
            "bee-bunpo:fixture-r1:record:unknown"
        )

        with self.assertRaisesRegex(ValueError, "provenance.*source record"):
            SourceBundle.from_dict(value)

    def test_source_links_preserve_safe_http_but_reject_credentials(self):
        self.assertEqual(
            SourceLink("Legacy source", "http://example.org/grammar").url,
            "http://example.org/grammar",
        )
        with self.assertRaisesRegex(ValueError, "credentials"):
            SourceLink("Unsafe source", "https://user:pass@example.org/grammar")

    def test_unknown_canonical_fields_fail_instead_of_disappearing(self):
        value = source_bundle()
        value["records"][0]["unmapped_field"] = "must not disappear"

        with self.assertRaisesRegex(ValueError, "unsupported.*unmapped_field"):
            SourceBundle.from_dict(value)

    def test_boolean_canonical_format_version_is_rejected(self):
        value = source_bundle()
        value["format_version"] = True

        with self.assertRaisesRegex(ValueError, "unsupported canonical source"):
            SourceBundle.from_dict(value)

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
            ("imabi", BuildMode.PRIVATE, SelectionMode.CONTENT, True),
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

    def test_user_reported_permission_only_enables_private_bunpro_and_imabi_content(self):
        bunpro = get_source("bunpro")
        imabi = get_source("imabi")

        self.assertIn("user-reported permission", bunpro.attribution)
        self.assertIn("user-reported permission", imabi.attribution)
        self.assertEqual(imabi.access_mode.value, "public-http")
        self.assertEqual(imabi.import_mode.value, "content")
        self.assertEqual(imabi.publication_mode.value, "denied")
        with self.assertRaises(PolicyError):
            validate_selection("bunpro", BuildMode.PUBLISHABLE, SelectionMode.CONTENT)
        with self.assertRaises(PolicyError):
            validate_selection("imabi", BuildMode.PUBLISHABLE, SelectionMode.CONTENT)

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

    def test_remote_url_with_invalid_port_fails_as_policy_error(self):
        with self.assertRaises(PolicyError):
            validate_remote_url(
                "yokubi",
                "https://raw.githubusercontent.com:not-a-port/Morgawr/yokubi",
            )


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

    def write_manifest(
        self,
        *,
        build_mode=None,
        input_sha256=None,
        path="source.json",
        source_id="bee-bunpo",
        revision_id="fixture-r1",
        input_spec=None,
    ):
        if input_spec is None:
            input_spec = {
                "path": path,
                "sha256": input_sha256 or self.input_sha256,
            }
        manifest = {
            "format": "ugd-source-manifest",
            "format_version": 1,
            "registry_version": REGISTRY_VERSION,
            "sources": [
                {
                    "source_id": source_id,
                    "revision_id": revision_id,
                    "selection": "content",
                    "source_content_sha256": SHA_A,
                    "input": input_spec,
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
        manifest_path = self.root / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        return manifest_path

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
        report_sources = {item["source_id"]: item for item in report["sources"]}
        self.assertEqual(report_sources["bee-bunpo"]["registry"]["access_mode"], "local-file")
        self.assertEqual(report_sources["bee-bunpo"]["registry"]["import_mode"], "content")
        self.assertEqual(report_sources["bee-bunpo"]["registry"]["publication_mode"], "denied")
        self.assertNotIn(str(self.root), output.read_text(encoding="utf-8"))
        self.assertNotIn(str(self.root), report_path.read_text(encoding="utf-8"))

    def test_duplicate_manifest_keys_are_rejected(self):
        manifest = self.root / "manifest.json"
        manifest.write_text(
            '{"format":"ugd-source-manifest","format_version":1,'
            '"registry_version":1,"registry_version":1,"sources":[]}',
            encoding="utf-8",
        )

        with self.assertRaisesRegex(BuildError, "duplicate JSON key"):
            build_from_manifest(manifest, self.root / "build.json", self.root / "report.json")

    def test_boolean_manifest_versions_are_rejected(self):
        for field in ("format_version", "registry_version"):
            with self.subTest(field=field):
                manifest = self.write_manifest()
                value = json.loads(manifest.read_text(encoding="utf-8"))
                value[field] = True
                manifest.write_text(json.dumps(value), encoding="utf-8")

                with self.assertRaises(BuildError):
                    build_from_manifest(
                        manifest,
                        self.root / "build.json",
                        self.root / "report.json",
                    )

    def test_malformed_nested_object_is_reported_as_build_error(self):
        value = source_bundle()
        value["source_revision"] = [
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
        ]
        self.input_path.write_text(json.dumps(value), encoding="utf-8")
        self.input_sha256 = hashlib.sha256(self.input_path.read_bytes()).hexdigest()
        manifest = self.write_manifest()

        with self.assertRaisesRegex(BuildError, "model validation"):
            build_from_manifest(manifest, self.root / "build.json", self.root / "report.json")

    def test_publishable_build_refuses_uncleared_content_before_writing(self):
        manifest = self.write_manifest(build_mode="publishable")
        output = self.root / "build.json"
        report = self.root / "report.json"

        with self.assertRaisesRegex(BuildError, "bee-bunpo.*publish"):
            build_from_manifest(manifest, output, report)

        self.assertFalse(output.exists())
        self.assertFalse(report.exists())

    def test_revision_cannot_loosen_registry_publication_policy(self):
        self.input_path.write_text(
            json.dumps(source_bundle(publication_mode="allowed"), sort_keys=True),
            encoding="utf-8",
        )
        self.input_sha256 = hashlib.sha256(self.input_path.read_bytes()).hexdigest()
        manifest = self.write_manifest()

        with self.assertRaisesRegex(BuildError, "exceeds registry publication policy"):
            build_from_manifest(
                manifest,
                self.root / "build.json",
                self.root / "report.json",
            )

    def test_publishable_build_requires_registry_attribution_contract(self):
        value = source_bundle(
            source_id="yokubi",
            access_mode="public-http",
            publication_mode="allowed",
            attribution="Wrong attribution",
            license_identifier="CC-BY-4.0",
            license_evidence_url="https://github.com/Morgawr/yokubi/blob/main/LICENSE",
        )
        self.input_path.write_text(json.dumps(value), encoding="utf-8")
        self.input_sha256 = hashlib.sha256(self.input_path.read_bytes()).hexdigest()
        manifest = self.write_manifest(source_id="yokubi", build_mode="publishable")

        with self.assertRaisesRegex(BuildError, "attribution"):
            build_from_manifest(manifest, self.root / "build.json", self.root / "report.json")

    def test_publishable_build_requires_licence_evidence(self):
        value = source_bundle(
            source_id="yokubi",
            access_mode="public-http",
            publication_mode="allowed",
            attribution=get_source("yokubi").attribution,
            license_identifier="CC-BY-4.0",
        )
        self.input_path.write_text(json.dumps(value), encoding="utf-8")
        self.input_sha256 = hashlib.sha256(self.input_path.read_bytes()).hexdigest()
        manifest = self.write_manifest(source_id="yokubi", build_mode="publishable")

        with self.assertRaisesRegex(BuildError, "licence evidence"):
            build_from_manifest(manifest, self.root / "build.json", self.root / "report.json")

    def test_publishable_build_accepts_fully_attributed_open_content(self):
        value = source_bundle(
            source_id="yokubi",
            access_mode="public-http",
            publication_mode="allowed",
            attribution=get_source("yokubi").attribution,
            license_identifier="CC-BY-4.0",
            license_evidence_url="https://github.com/Morgawr/yokubi/blob/main/LICENSE",
        )
        self.input_path.write_text(json.dumps(value), encoding="utf-8")
        self.input_sha256 = hashlib.sha256(self.input_path.read_bytes()).hexdigest()
        manifest = self.write_manifest(source_id="yokubi", build_mode="publishable")

        report = build_from_manifest(
            manifest,
            self.root / "build.json",
            self.root / "report.json",
        )

        self.assertEqual(report["build_mode"], "publishable")
        report_sources = {item["source_id"]: item for item in report["sources"]}
        self.assertEqual(report_sources["yokubi"]["registry"]["publication_mode"], "allowed")

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

    def test_local_reader_rejects_a_post_validation_symlink_swap(self):
        target = self.root / "outside.json"
        target.write_bytes(self.input_path.read_bytes())
        link = self.root / "swapped.json"
        link.symlink_to(target)

        with self.assertRaisesRegex(BuildError, "symbolic link"):
            builder._read_local_input(link, self.input_sha256, 1024 * 1024)

    def test_local_file_source_cannot_select_a_remote_input(self):
        manifest = self.write_manifest(
            input_spec={
                "url": "https://github.com/bee-san/yomitan-dictionaries/source.json",
                "cache": "cache.json",
                "sha256": self.input_sha256,
            }
        )

        with (
            patch.object(builder, "_download_once") as download,
            self.assertRaisesRegex(BuildError, "local-file.*remote"),
        ):
            build_from_manifest(manifest, self.root / "build.json", self.root / "report.json")

        download.assert_not_called()

    def test_remote_cache_cannot_overwrite_manifest_or_outputs(self):
        value = source_bundle(
            source_id="yokubi",
            access_mode="public-http",
            publication_mode="allowed",
            attribution=get_source("yokubi").attribution,
            license_identifier="CC-BY-4.0",
            license_evidence_url="https://github.com/Morgawr/yokubi/blob/main/LICENSE",
        )
        content = json.dumps(value).encode("utf-8")
        content_sha256 = hashlib.sha256(content).hexdigest()
        manifest = self.write_manifest(
            source_id="yokubi",
            input_spec={
                "url": "https://raw.githubusercontent.com/Morgawr/yokubi/main/source.json",
                "cache": "manifest.json",
                "sha256": content_sha256,
            },
        )
        original_manifest = manifest.read_bytes()

        with (
            patch.object(builder, "_download_once", return_value=content) as download,
            self.assertRaisesRegex(BuildError, "cache.*manifest|manifest.*cache"),
        ):
            build_from_manifest(manifest, self.root / "build.json", self.root / "report.json")

        download.assert_not_called()
        self.assertEqual(manifest.read_bytes(), original_manifest)

    def test_outputs_cannot_enter_the_reserved_build_lock_namespace(self):
        manifest = self.write_manifest()
        output = builder.LOCK_ROOT / "attacker-output.json"

        with self.assertRaisesRegex(BuildError, "reserved build lock namespace"):
            build_from_manifest(manifest, output, self.root / "report.json")

        self.assertFalse(output.exists())

    def test_output_cannot_overwrite_a_pinned_input(self):
        manifest = self.write_manifest()

        with self.assertRaisesRegex(BuildError, "pinned input"):
            build_from_manifest(manifest, self.input_path, self.root / "report.json")

    def test_failed_refresh_preserves_the_previous_bad_cache_for_diagnosis(self):
        cache = self.root / "cache.json"
        cache.write_bytes(b"corrupt cache")
        expected_sha256 = hashlib.sha256(b"expected content").hexdigest()
        spec = {
            "url": "https://raw.githubusercontent.com/Morgawr/yokubi/main/source.json",
            "cache": "cache.json",
            "sha256": expected_sha256,
            "max_bytes": 1024,
        }

        with (
            patch.object(builder, "_download_once", side_effect=URLError("offline")),
            patch.object(builder.time, "sleep"),
            self.assertRaises(BuildError),
        ):
            builder._materialize_input("yokubi", spec, self.root)

        self.assertEqual(cache.read_bytes(), b"corrupt cache")

    def test_redirect_policy_is_checked_before_following_the_redirect(self):
        handler = builder._PolicyRedirectHandler("yokubi")
        request = Request("https://raw.githubusercontent.com/Morgawr/yokubi/source.json")

        with self.assertRaises(PolicyError):
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "http://127.0.0.1/private",
            )

    def test_second_output_failure_restores_existing_output_and_report(self):
        manifest = self.write_manifest()
        output = self.root / "build.json"
        report = self.root / "report.json"
        output.write_bytes(b"previous output")
        report.write_bytes(b"previous report")
        original_replace = builder._replace_path
        failed = False

        def fail_report_install(source, destination):
            nonlocal failed
            if destination == report.resolve() and not failed:
                failed = True
                raise OSError("simulated report write failure")
            return original_replace(source, destination)

        with (
            patch.object(builder, "_replace_path", side_effect=fail_report_install),
            self.assertRaises(OSError),
        ):
            build_from_manifest(manifest, output, report)

        self.assertEqual(output.read_bytes(), b"previous output")
        self.assertEqual(report.read_bytes(), b"previous report")

    def test_rollback_restores_backups_without_unlinking_installed_files(self):
        manifest = self.write_manifest()
        output = self.root / "build.json"
        report = self.root / "report.json"
        output.write_bytes(b"previous output")
        report.write_bytes(b"previous report")
        original_replace = builder._replace_path
        original_unlink = Path.unlink
        failed = False

        def fail_report_install(source, destination):
            nonlocal failed
            if destination == report.resolve() and not failed:
                failed = True
                raise OSError("simulated report write failure")
            return original_replace(source, destination)

        def reject_output_unlink(path, *args, **kwargs):
            if path == output.resolve():
                raise PermissionError("installed output must be replaced from backup")
            return original_unlink(path, *args, **kwargs)

        with (
            patch.object(builder, "_replace_path", side_effect=fail_report_install),
            patch.object(Path, "unlink", reject_output_unlink),
            self.assertRaisesRegex(OSError, "simulated report write failure"),
        ):
            build_from_manifest(manifest, output, report)

        self.assertEqual(output.read_bytes(), b"previous output")
        self.assertEqual(report.read_bytes(), b"previous report")

    def test_concurrent_builds_cannot_mix_output_and_report_pairs(self):
        manifest_template = json.loads(self.write_manifest().read_text(encoding="utf-8"))
        manifests = []
        for label in ("a", "b"):
            value = source_bundle()
            value["records"][0]["raw_expression"] = f"synthetic-{label}"
            source_path = self.root / f"source-{label}.json"
            source_path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
            manifest_value = json.loads(json.dumps(manifest_template))
            manifest_value["sources"][0]["input"] = {
                "path": source_path.name,
                "sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
            }
            manifest_path = self.root / f"manifest-{label}.json"
            manifest_path.write_text(json.dumps(manifest_value), encoding="utf-8")
            manifests.append(manifest_path)

        output = self.root / "shared-build.json"
        report = self.root / "shared-report.json"
        first_output_installed = threading.Event()
        original_replace = builder._replace_path
        errors = []

        def delay_first_writer(source, destination):
            result = original_replace(source, destination)
            if destination == output.resolve() and threading.current_thread().name == "build-a":
                first_output_installed.set()
                time.sleep(0.2)
            return result

        def run_build(manifest):
            try:
                build_from_manifest(manifest, output, report)
            except Exception as error:  # pragma: no cover - asserted below
                errors.append(error)

        with patch.object(builder, "_replace_path", side_effect=delay_first_writer):
            first = threading.Thread(target=run_build, args=(manifests[0],), name="build-a")
            first.start()
            self.assertTrue(first_output_installed.wait(timeout=2))
            second = threading.Thread(target=run_build, args=(manifests[1],), name="build-b")
            second.start()
            first.join(timeout=3)
            second.join(timeout=3)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        report_value = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(report_value["output_sha256"], hashlib.sha256(output.read_bytes()).hexdigest())

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

    def test_cli_builds_a_synthetic_fixture(self):
        manifest = self.write_manifest()
        output = self.root / "cli-build.json"
        report = self.root / "cli-report.json"

        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/grammar/build.py"),
                "--source-manifest",
                str(manifest),
                "--output",
                str(output),
                "--report",
                str(report),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(output.read_text())["build_mode"], "private")
        self.assertEqual(json.loads(report.read_text())["content_source_count"], 1)


if __name__ == "__main__":
    unittest.main()

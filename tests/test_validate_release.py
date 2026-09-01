import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.validate_release import ValidationError, validate_collection


class ValidateReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.assets = self.root / "assets"
        self.assets.mkdir()
        self.archive = self.assets / "sample.zip"
        self.catalogue = self.root / "CATALOGUE.json"
        self.checksums = self.root / "CHECKSUMS.sha256"

    def tearDown(self):
        self.temp_dir.cleanup()

    def write_fixture(self, *, include_image=True, checksum_override=None):
        image_path = "media/example.png"
        term = [
            "見本",
            "みほん",
            "",
            "",
            0,
            [
                {
                    "type": "structured-content",
                    "content": {"tag": "img", "path": image_path},
                }
            ],
            1,
            "",
        ]
        with zipfile.ZipFile(self.archive, "w", compression=zipfile.ZIP_DEFLATED) as target:
            target.writestr(
                "index.json",
                json.dumps({"title": "Sample", "revision": "1", "format": 3}),
            )
            target.writestr("term_bank_1.json", json.dumps([term]))
            if include_image:
                target.writestr(image_path, b"image")

        digest = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.catalogue.write_text(
            json.dumps(
                {
                    "release": "test",
                    "release_url": "https://example.invalid/test",
                    "archives": [
                        {
                            "file": self.archive.name,
                            "title": "Sample",
                            "revision": "1",
                            "description": "fixture",
                            "lookup_entries": 1,
                            "bytes": self.archive.stat().st_size,
                            "sha256": digest,
                        }
                    ],
                }
            )
        )
        self.checksums.write_text(
            f"{checksum_override or digest}  {self.archive.name}\n"
        )

    def test_valid_collection_passes_and_reports_lookup_count(self):
        self.write_fixture()

        report = validate_collection(self.assets, self.catalogue, self.checksums)

        self.assertEqual(report["total"], 1)
        self.assertEqual(report["archives"][0]["lookupEntries"], 1)
        self.assertEqual(report["archives"][0]["mediaReferences"], 1)

    def test_missing_structured_content_image_fails(self):
        self.write_fixture(include_image=False)

        with self.assertRaisesRegex(ValidationError, "missing media/example.png"):
            validate_collection(self.assets, self.catalogue, self.checksums)

    def test_checksum_mismatch_fails(self):
        self.write_fixture(checksum_override="0" * 64)

        with self.assertRaisesRegex(ValidationError, "CHECKSUMS.sha256 mismatch"):
            validate_collection(self.assets, self.catalogue, self.checksums)


if __name__ == "__main__":
    unittest.main()

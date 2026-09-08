from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
import zipfile

from scripts.grammar.model import BlockKind
from scripts.grammar.sources.ninjal import (
    OFFICIAL_PINS,
    NinjalAdapterError,
    NinjalCounts,
    NinjalPins,
    adapt_ninjal_archive,
)


SAMPLE_FIRST = """<?xml version="1.0" encoding="UTF-8"?>
<Entry>
  <SentencePattern>～〓間〔あいだ〕</SentencePattern>
  <Reading>～あいだ</Reading>
  <Category></Category>
  <GeneralExplanation></GeneralExplanation>
  <Sense>
    <SenceCategory>〓時〔とき〕｜〓期間〔きかん〕</SenceCategory>
    <Level>4</Level>
    <Usage>～の期間中ずっと使います。</Usage>
    <UsageNotes>継続する状態に使います。</UsageNotes>
    <Style>硬い言い方</Style>
    <CommonlyUsedWordsTogether>ずっと</CommonlyUsedWordsTogether>
    <Orthography>〜〓間〔あいだ〕・〜あいだ</Orthography>
    <AlternativeForm></AlternativeForm>
    <SimilarExpressions>〜間に</SimilarExpressions>
    <ContrastingExpression></ContrastingExpression>
    <Connection>
      <ConnectionType>Vる〓間〔あいだ〕</ConnectionType>
      <ExampleSet>
        <SceneDescription>仕事の場面</SceneDescription>
        <Example>日本にいる｛〓間〔あいだ〕｝、働く。</Example>
        <ExampleNote>継続の例</ExampleNote>
      </ExampleSet>
    </Connection>
  </Sense>
  <Sense>
    <SenceCategory>時｜別の意味</SenceCategory>
    <Level></Level>
    <Usage>別の意味です。</Usage>
    <UsageNotes></UsageNotes>
    <Style></Style>
    <CommonlyUsedWordsTogether></CommonlyUsedWordsTogether>
    <Orthography></Orthography>
    <AlternativeForm></AlternativeForm>
    <SimilarExpressions></SimilarExpressions>
    <ContrastingExpression></ContrastingExpression>
    <Connection>
      <ConnectionType>Nの間</ConnectionType>
      <ExampleSet>
        <SceneDescription></SceneDescription>
        <Example>休みの間、読む。</Example>
        <ExampleNote></ExampleNote>
      </ExampleSet>
    </Connection>
  </Sense>
</Entry>
"""

SAMPLE_SECOND = """<?xml version="1.0" encoding="UTF-8"?>
<Entry>
  <SentencePattern>～に</SentencePattern>
  <Reading>～に</Reading>
  <GeneralExplanation></GeneralExplanation>
  <Sense>
    <SenceCategory>対象</SenceCategory>
    <Level>2</Level>
    <Usage>対象を示します。</Usage>
    <UsageNotes></UsageNotes>
    <Style></Style>
    <CommonlyUsedWordsTogether></CommonlyUsedWordsTogether>
    <Orthography></Orthography>
    <AlternativeForm></AlternativeForm>
    <SimilarExpressions></SimilarExpressions>
    <ContrastingExpression></ContrastingExpression>
    <Connection>
      <ConnectionType>Nに</ConnectionType>
      <ExampleSet>
        <SceneDescription></SceneDescription>
        <Example>学校に行く。</Example>
        <ExampleNote></ExampleNote>
      </ExampleSet>
    </Connection>
  </Sense>
</Entry>
"""


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def write_zip(path: Path, members: list[tuple[str, bytes]]) -> bytes:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in members:
            archive.writestr(name, content)
    return path.read_bytes()


class NinjalAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source_zip = self.root / "official.zip"
        self.source_bytes = write_zip(
            self.source_zip,
            [
                ("〜間.xml", SAMPLE_FIRST.encode("utf-8")),
                ("〜に.xml", SAMPLE_SECOND.encode("utf-8")),
            ],
        )
        self.headwords = self.root / "headwords.txt"
        self.headword_bytes = "～間\n～間\n".encode("utf-8")
        self.headwords.write_bytes(self.headword_bytes)
        self.reference = self.root / "reference.zip"
        self.reference_bytes = write_zip(
            self.reference,
            [
                (
                    "index.json",
                    json.dumps(
                        {
                            "title": "日本語文型バンク（NINJAL 2026.01）",
                            "revision": "2026.01.33",
                            "attribution": (
                                "日本語文型データベース DOI: 10.15084/0002000610 "
                                "License: CC BY 4.0"
                            ),
                        },
                        ensure_ascii=False,
                    ).encode("utf-8"),
                ),
                (
                    "term_bank_1.json",
                    json.dumps(
                        [
                            [
                                "〜間",
                                "〜あいだ",
                                "bunkei Lv4",
                                "",
                                30,
                                ["definition"],
                                1,
                                "bunkei",
                            ],
                            [
                                "間",
                                "あいだ",
                                "bunkei Lv4",
                                "",
                                25,
                                ["definition"],
                                1,
                                "bunkei",
                            ],
                            [
                                "〜に",
                                "〜に",
                                "bunkei Lv2",
                                "",
                                30,
                                ["definition"],
                                2,
                                "bunkei",
                            ],
                        ],
                        ensure_ascii=False,
                    ).encode("utf-8"),
                ),
            ],
        )
        self.pins = NinjalPins(
            revision_id="2026.01.26",
            source_sha256=sha256(self.source_bytes),
            headwords_sha256=sha256(self.headword_bytes),
            counts=NinjalCounts(records=2, senses=3, connections=3, examples=3),
            reference_yomitan_revision="2026.01.33",
            reference_yomitan_sha256=sha256(self.reference_bytes),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def adapt(self, **kwargs):
        return adapt_ninjal_archive(
            self.source_zip,
            self.headwords,
            pins=self.pins,
            reference_yomitan_zip=self.reference,
            overlap_expressions=("～間",),
            **kwargs,
        )

    def test_preserves_source_senses_fields_connections_and_example_groups(
        self,
    ) -> None:
        result = self.adapt()
        bundle = result.bundle
        bundle.validate()

        self.assertEqual(len(bundle.records), 2)
        self.assertEqual(len(bundle.senses), 3)
        first_record = bundle.records[0]
        self.assertEqual(first_record.raw_expression, "～〓間〔あいだ〕")
        self.assertIn("xml", first_record.field_hashes)
        self.assertIn(
            "Sense[1]/Connection[1]/ExampleSet[1]/Example", first_record.field_hashes
        )

        first_sense = bundle.senses[0]
        self.assertEqual(first_sense.level_labels, ("NINJAL Level 4",))
        self.assertEqual(first_sense.register_labels, ("硬い言い方",))
        self.assertFalse(any("JLPT" in label for label in first_sense.level_labels))
        by_field = {block.content["field"]: block for block in first_sense.blocks}
        self.assertEqual(by_field["SenceCategory"].kind, BlockKind.MEANING)
        self.assertEqual(
            by_field["SenceCategory"].content["value"], "〓時〔とき〕｜〓期間〔きかん〕"
        )
        self.assertEqual(by_field["Connection"].kind, BlockKind.FORMATION)
        self.assertEqual(
            by_field["Connection"].content["connection_type"], "Vる〓間〔あいだ〕"
        )

        example = first_sense.examples[0]
        self.assertEqual(example.translation, None)
        self.assertEqual(example.japanese["connection_index"], 1)
        self.assertEqual(example.japanese["scene_description"], "仕事の場面")
        self.assertEqual(
            example.japanese["text"], "日本にいる｛〓間〔あいだ〕｝、働く。"
        )
        self.assertEqual(example.japanese["note"], "継続の例")

        unassigned = bundle.senses[1]
        self.assertEqual(unassigned.level_labels, ())
        self.assertEqual(bundle.source_revision.content_sha256, self.pins.source_sha256)
        self.assertEqual(bundle.source_revision.license.identifier, "CC-BY-4.0")
        self.assertIn("10.15084/0002000610", bundle.source_revision.attribution)

    def test_reports_exact_identity_sets_and_separate_lookup_coverage(self) -> None:
        report = self.adapt().report

        json.dumps(report, ensure_ascii=False, sort_keys=True)

        self.assertEqual(report["source_record_count"], 2)
        self.assertEqual(report["imported_record_count"], 2)
        self.assertEqual(report["source_sense_count"], 3)
        self.assertEqual(report["connection_count"], 3)
        self.assertEqual(report["example_count"], 3)
        self.assertEqual(report["reference_yomitan_lookup_row_count"], 3)
        self.assertEqual(report["reference_yomitan_sequence_count"], 2)
        self.assertEqual(report["expected_record_ids"], report["imported_record_ids"])
        self.assertEqual(report["rejected_record_ids"], [])
        self.assertEqual(len(report["overlap_record_ids"]), 1)
        self.assertEqual(len(report["additional_record_ids"]), 1)
        self.assertEqual(
            set(report["overlap_record_ids"]) | set(report["additional_record_ids"]),
            set(report["expected_record_ids"]),
        )
        self.assertEqual(report["reference_yomitan"]["source_family"], "ninjal-bunkei")
        self.assertFalse(report["reference_yomitan"]["independent_corroboration"])
        self.assertEqual(
            report["headword_anomalies"]["duplicate_headwords"], {"〜間": [1, 2]}
        )
        self.assertEqual(report["headword_anomalies"]["xml_without_headword"], ["〜に"])

    def test_rejects_unpinned_or_structurally_incomplete_source(self) -> None:
        bad_pins = NinjalPins(
            revision_id=self.pins.revision_id,
            source_sha256="0" * 64,
            headwords_sha256=self.pins.headwords_sha256,
            counts=self.pins.counts,
            reference_yomitan_revision=self.pins.reference_yomitan_revision,
            reference_yomitan_sha256=self.pins.reference_yomitan_sha256,
        )
        with self.assertRaisesRegex(NinjalAdapterError, "source ZIP SHA-256 mismatch"):
            adapt_ninjal_archive(self.source_zip, self.headwords, pins=bad_pins)

        broken_zip = self.root / "broken.zip"
        broken = SAMPLE_SECOND.replace("<Usage>対象を示します。</Usage>", "")
        broken_bytes = write_zip(broken_zip, [("〜に.xml", broken.encode("utf-8"))])
        broken_pins = NinjalPins(
            revision_id=self.pins.revision_id,
            source_sha256=sha256(broken_bytes),
            headwords_sha256=self.pins.headwords_sha256,
            counts=NinjalCounts(records=1, senses=1, connections=1, examples=1),
            reference_yomitan_revision=self.pins.reference_yomitan_revision,
            reference_yomitan_sha256=self.pins.reference_yomitan_sha256,
        )
        with self.assertRaisesRegex(NinjalAdapterError, "missing Usage"):
            adapt_ninjal_archive(broken_zip, self.headwords, pins=broken_pins)

    def test_refuses_symbolic_link_inputs(self) -> None:
        linked_source = self.root / "linked-source.zip"
        linked_source.symlink_to(self.source_zip)
        with self.assertRaisesRegex(NinjalAdapterError, "cannot read source ZIP"):
            adapt_ninjal_archive(linked_source, self.headwords, pins=self.pins)

    def test_reference_yomitan_must_cover_every_canonical_sequence(self) -> None:
        incomplete = self.root / "incomplete.zip"
        incomplete_bytes = write_zip(
            incomplete,
            [
                (
                    "index.json",
                    json.dumps(
                        {
                            "revision": self.pins.reference_yomitan_revision,
                            "attribution": "DOI: 10.15084/0002000610; CC BY 4.0",
                        }
                    ).encode("utf-8"),
                ),
                (
                    "term_bank_1.json",
                    json.dumps([["〜間", "〜あいだ", "", "", 0, [], 1, ""]]).encode(
                        "utf-8"
                    ),
                ),
            ],
        )
        pins = NinjalPins(
            revision_id=self.pins.revision_id,
            source_sha256=self.pins.source_sha256,
            headwords_sha256=self.pins.headwords_sha256,
            counts=self.pins.counts,
            reference_yomitan_revision=self.pins.reference_yomitan_revision,
            reference_yomitan_sha256=sha256(incomplete_bytes),
        )
        with self.assertRaisesRegex(NinjalAdapterError, "sequence set mismatch"):
            adapt_ninjal_archive(
                self.source_zip,
                self.headwords,
                pins=pins,
                reference_yomitan_zip=incomplete,
            )

    @unittest.skipUnless(
        os.environ.get("NINJAL_TEST_SOURCE_ZIP")
        and os.environ.get("NINJAL_TEST_HEADWORDS")
        and os.environ.get("NINJAL_TEST_YOMITAN_ZIP"),
        "set NINJAL_TEST_SOURCE_ZIP, NINJAL_TEST_HEADWORDS and NINJAL_TEST_YOMITAN_ZIP",
    )
    def test_official_2026_01_snapshot_has_exhaustive_coverage(self) -> None:
        result = adapt_ninjal_archive(
            os.environ["NINJAL_TEST_SOURCE_ZIP"],
            os.environ["NINJAL_TEST_HEADWORDS"],
            pins=OFFICIAL_PINS,
            reference_yomitan_zip=os.environ["NINJAL_TEST_YOMITAN_ZIP"],
        )
        report = result.report
        self.assertEqual(report["source_record_count"], 800)
        self.assertEqual(report["imported_record_count"], 800)
        self.assertEqual(report["source_sense_count"], 958)
        self.assertEqual(report["connection_count"], 1988)
        self.assertEqual(report["example_count"], 9552)
        self.assertEqual(report["reference_yomitan_lookup_row_count"], 2394)
        self.assertEqual(report["expected_record_ids"], report["imported_record_ids"])
        self.assertEqual(report["rejected_record_ids"], [])


if __name__ == "__main__":
    unittest.main()

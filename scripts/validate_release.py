#!/usr/bin/env python3
import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path, PurePosixPath


BANK_PATTERN = re.compile(
    r"^(term_bank|term_meta_bank|kanji_bank|kanji_meta_bank|tag_bank)_(\d+)\.json$"
)


class ValidationError(RuntimeError):
    pass


def _sha256(path):
    value = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _load_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValidationError(f"cannot read {path.name}: {error}") from error


def _load_checksums(path):
    checksums = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise ValidationError(f"cannot read {path.name}: {error}") from error

    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        match = re.fullmatch(r"([0-9a-f]{64})  ([^\r\n]+)", line)
        if match is None:
            raise ValidationError(f"malformed {path.name} line {line_number}")
        digest, file_name = match.groups()
        if file_name in checksums:
            raise ValidationError(f"duplicate checksum entry for {file_name}")
        checksums[file_name] = digest
    return checksums


def _validate_member_name(name, archive_name):
    member = PurePosixPath(name)
    if member.is_absolute() or ".." in member.parts or "\\" in name:
        raise ValidationError(f"{archive_name}: unsafe ZIP member {name}")


def _read_archive_json(archive, name, archive_name):
    try:
        return json.loads(archive.read(name))
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValidationError(f"{archive_name}: invalid {name}: {error}") from error


def _collect_media_references(value, references):
    if isinstance(value, list):
        for item in value:
            _collect_media_references(item, references)
        return
    if not isinstance(value, dict):
        return

    if value.get("tag") == "img" or value.get("type") == "image":
        path = value.get("path")
        if isinstance(path, str) and path:
            references.add(path)
    for item in value.values():
        _collect_media_references(item, references)


def _validate_bank_sequences(names, archive_name):
    banks = {}
    for name in names:
        match = BANK_PATTERN.fullmatch(name)
        if match is None:
            continue
        bank_type, number_text = match.groups()
        banks.setdefault(bank_type, []).append(int(number_text))

    for bank_type, numbers in banks.items():
        ordered = sorted(numbers)
        expected = list(range(1, len(ordered) + 1))
        if ordered != expected:
            raise ValidationError(
                f"{archive_name}: non-contiguous {bank_type} numbering {ordered}"
            )
    return banks


def _validate_archive(path, metadata):
    archive_name = path.name
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as error:
        raise ValidationError(f"{archive_name}: cannot open ZIP: {error}") from error

    with archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValidationError(f"{archive_name}: duplicate ZIP member names")
        for name in names:
            _validate_member_name(name, archive_name)

        corrupt = archive.testzip()
        if corrupt is not None:
            raise ValidationError(f"{archive_name}: CRC failure in {corrupt}")
        if "index.json" not in names:
            raise ValidationError(f"{archive_name}: missing index.json")

        index = _read_archive_json(archive, "index.json", archive_name)
        if index.get("title") != metadata["title"]:
            raise ValidationError(
                f"{archive_name}: index title does not match CATALOGUE.json"
            )
        if index.get("revision") != metadata["revision"]:
            raise ValidationError(
                f"{archive_name}: index revision does not match CATALOGUE.json"
            )

        banks = _validate_bank_sequences(names, archive_name)
        term_bank_names = sorted(
            (
                name
                for name in names
                if name.startswith("term_bank_") and name.endswith(".json")
            ),
            key=lambda name: int(name[len("term_bank_") : -len(".json")]),
        )
        if not term_bank_names:
            raise ValidationError(f"{archive_name}: no term banks")

        lookup_entries = 0
        media_references = set()
        for bank_name in term_bank_names:
            rows = _read_archive_json(archive, bank_name, archive_name)
            if not isinstance(rows, list):
                raise ValidationError(f"{archive_name}: {bank_name} is not an array")
            for row_number, row in enumerate(rows):
                if not isinstance(row, list) or len(row) != 8:
                    raise ValidationError(
                        f"{archive_name}: invalid term row {bank_name}[{row_number}]"
                    )
                _collect_media_references(row[5], media_references)
            lookup_entries += len(rows)

        if lookup_entries != metadata["lookup_entries"]:
            raise ValidationError(
                f"{archive_name}: lookup count {lookup_entries} does not match "
                f"CATALOGUE.json value {metadata['lookup_entries']}"
            )

        missing_media = sorted(path for path in media_references if path not in names)
        if missing_media:
            raise ValidationError(
                f"{archive_name}: missing {missing_media[0]}"
            )

    return {
        "file": archive_name,
        "title": metadata["title"],
        "revision": metadata["revision"],
        "lookupEntries": lookup_entries,
        "mediaReferences": len(media_references),
        "bankCounts": {key: len(value) for key, value in sorted(banks.items())},
    }


def validate_collection(assets_dir, catalogue_path, checksums_path):
    assets_dir = Path(assets_dir)
    catalogue_path = Path(catalogue_path)
    checksums_path = Path(checksums_path)
    catalogue = _load_json(catalogue_path)
    checksums = _load_checksums(checksums_path)

    archives = catalogue.get("archives")
    if not isinstance(archives, list) or not archives:
        raise ValidationError("CATALOGUE.json has no archives")

    metadata_by_file = {}
    for metadata in archives:
        if not isinstance(metadata, dict) or not isinstance(metadata.get("file"), str):
            raise ValidationError("CATALOGUE.json contains an invalid archive entry")
        file_name = metadata["file"]
        if file_name in metadata_by_file:
            raise ValidationError(f"CATALOGUE.json repeats {file_name}")
        metadata_by_file[file_name] = metadata

    expected_files = set(metadata_by_file)
    actual_files = {path.name for path in assets_dir.glob("*.zip")}
    if actual_files != expected_files:
        missing = sorted(expected_files - actual_files)
        extra = sorted(actual_files - expected_files)
        raise ValidationError(f"release ZIP set mismatch: missing={missing}, extra={extra}")
    if set(checksums) != expected_files:
        raise ValidationError("CHECKSUMS.sha256 file set does not match CATALOGUE.json")

    report_archives = []
    for file_name in sorted(expected_files):
        metadata = metadata_by_file[file_name]
        path = assets_dir / file_name
        actual_bytes = path.stat().st_size
        if actual_bytes != metadata.get("bytes"):
            raise ValidationError(f"{file_name}: byte size does not match CATALOGUE.json")
        actual_digest = _sha256(path)
        if actual_digest != metadata.get("sha256"):
            raise ValidationError(f"{file_name}: CATALOGUE.json SHA-256 mismatch")
        if actual_digest != checksums[file_name]:
            raise ValidationError(f"{file_name}: CHECKSUMS.sha256 mismatch")
        report_archives.append(_validate_archive(path, metadata))

    return {
        "release": catalogue.get("release"),
        "total": len(report_archives),
        "archives": report_archives,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets-dir", type=Path, required=True)
    parser.add_argument("--catalogue", type=Path, default=Path("CATALOGUE.json"))
    parser.add_argument("--checksums", type=Path, default=Path("CHECKSUMS.sha256"))
    arguments = parser.parse_args(argv)
    try:
        report = validate_collection(
            arguments.assets_dir,
            arguments.catalogue,
            arguments.checksums,
        )
    except ValidationError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

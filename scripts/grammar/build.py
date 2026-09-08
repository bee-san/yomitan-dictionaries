#!/usr/bin/env python3
"""Verify pinned canonical inputs and build a deterministic grammar snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
from typing import Any, Mapping, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.grammar.model import (  # type: ignore[import-not-found]
        BuildMode,
        ImportMode,
        PublicationMode,
        SelectionMode,
        SourceBundle,
        canonical_json_bytes,
    )
    from scripts.grammar.registry import (  # type: ignore[import-not-found]
        REGISTRY_VERSION,
        PolicyError,
        validate_remote_url,
        validate_selection,
    )
else:
    from .model import (
        BuildMode,
        ImportMode,
        PublicationMode,
        SelectionMode,
        SourceBundle,
        canonical_json_bytes,
    )
    from .registry import (
        REGISTRY_VERSION,
        PolicyError,
        validate_remote_url,
        validate_selection,
    )


MANIFEST_FORMAT_VERSION = 1
BUILD_FORMAT_VERSION = 1
DEFAULT_MAX_INPUT_BYTES = 64 * 1024 * 1024
HARD_MAX_INPUT_BYTES = 256 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 20
DOWNLOAD_RETRIES = 2
CHUNK_BYTES = 1024 * 1024


class BuildError(RuntimeError):
    """A manifest, input, registry policy, or output contract failed."""


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _strict_keys(value: Mapping[str, Any], allowed: set[str], context: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise BuildError(f"{context} has unsupported keys: {sorted(unknown)}")


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        content = path.read_bytes()
    except OSError as error:
        raise BuildError(f"cannot read source manifest: {error}") from error
    if len(content) > 1024 * 1024:
        raise BuildError("source manifest exceeds 1 MiB limit")
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BuildError(f"invalid source manifest JSON: {error}") from error
    if not isinstance(value, dict):
        raise BuildError("source manifest must be a JSON object")
    _strict_keys(
        value,
        {"format", "format_version", "registry_version", "build_mode", "sources"},
        "source manifest",
    )
    if value.get("format") != "ugd-source-manifest" or value.get("format_version") != MANIFEST_FORMAT_VERSION:
        raise BuildError("unsupported source manifest format or version")
    if value.get("registry_version") != REGISTRY_VERSION:
        raise BuildError(
            f"manifest registry_version must be exactly {REGISTRY_VERSION}"
        )
    if not isinstance(value.get("sources"), list) or not value["sources"]:
        raise BuildError("source manifest must explicitly select at least one source")
    return value


def _safe_relative(root: Path, value: Any, context: str) -> Path:
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise BuildError(f"{context} must be a non-empty relative path")
    root = root.resolve()
    candidate = (root / value).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise BuildError(f"{context} cannot escape the manifest directory") from error
    return candidate


def _input_limit(spec: Mapping[str, Any]) -> int:
    value = spec.get("max_bytes", DEFAULT_MAX_INPUT_BYTES)
    if not isinstance(value, int) or isinstance(value, bool) or not 0 < value <= HARD_MAX_INPUT_BYTES:
        raise BuildError(
            f"input max_bytes must be between 1 and {HARD_MAX_INPUT_BYTES}"
        )
    return value


def _read_local_input(path: Path, expected_sha256: str, max_bytes: int) -> bytes:
    try:
        size = path.stat().st_size
    except OSError as error:
        raise BuildError(f"cannot stat pinned local input: {error}") from error
    if size > max_bytes:
        raise BuildError(f"pinned local input exceeds {max_bytes} byte limit")
    try:
        content = path.read_bytes()
    except OSError as error:
        raise BuildError(f"cannot read pinned local input: {error}") from error
    if _sha256_bytes(content) != expected_sha256:
        raise BuildError("pinned local input SHA-256 mismatch")
    return content


def _download_once(source_id: str, url: str, max_bytes: int) -> bytes:
    request = Request(url, headers={"User-Agent": "bee-grammar-builder/1"})
    with urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
        final_url = response.geturl()
        validate_remote_url(source_id, final_url)
        length = response.headers.get("Content-Length")
        if length is not None:
            try:
                declared = int(length)
            except ValueError as error:
                raise BuildError("remote input has an invalid Content-Length") from error
            if declared > max_bytes:
                raise BuildError(f"remote input exceeds {max_bytes} byte limit")
        chunks = []
        total = 0
        while True:
            chunk = response.read(min(CHUNK_BYTES, max_bytes - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise BuildError(f"remote input exceeds {max_bytes} byte limit")
            chunks.append(chunk)
        return b"".join(chunks)


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temporary.write(content)
        temporary.flush()
        temporary_path = Path(temporary.name)
    try:
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _materialize_input(
    source_id: str,
    spec: Mapping[str, Any],
    manifest_directory: Path,
) -> tuple[bytes, str]:
    _strict_keys(spec, {"path", "url", "cache", "sha256", "max_bytes"}, f"{source_id} input")
    expected_sha256 = spec.get("sha256")
    if not _valid_sha256(expected_sha256):
        raise BuildError(f"{source_id} input requires a lowercase SHA-256 pin")
    expected_sha256 = cast(str, expected_sha256)
    max_bytes = _input_limit(spec)
    has_path = "path" in spec
    has_url = "url" in spec
    if has_path == has_url:
        raise BuildError(f"{source_id} input requires exactly one of path or url")
    if has_path:
        if "cache" in spec:
            raise BuildError(f"{source_id} local input cannot declare cache")
        path = _safe_relative(manifest_directory, spec["path"], f"{source_id} input path")
        return _read_local_input(path, expected_sha256, max_bytes), expected_sha256

    if "cache" not in spec:
        raise BuildError(f"{source_id} remote input requires an explicit relative cache path")
    url = spec["url"]
    if not isinstance(url, str):
        raise BuildError(f"{source_id} remote input URL must be a string")
    try:
        validate_remote_url(source_id, url)
    except PolicyError as error:
        raise BuildError(str(error)) from error
    cache = _safe_relative(manifest_directory, spec["cache"], f"{source_id} cache path")
    if cache.is_file():
        try:
            return _read_local_input(cache, expected_sha256, max_bytes), expected_sha256
        except BuildError:
            cache.unlink(missing_ok=True)

    last_error: Exception | None = None
    for attempt in range(DOWNLOAD_RETRIES + 1):
        try:
            content = _download_once(source_id, url, max_bytes)
            if _sha256_bytes(content) != expected_sha256:
                raise BuildError(f"{source_id} downloaded input SHA-256 mismatch")
            _atomic_write(cache, content)
            return content, expected_sha256
        except (BuildError, HTTPError, URLError, TimeoutError, OSError, PolicyError) as error:
            last_error = error
            if attempt < DOWNLOAD_RETRIES:
                time.sleep(2**attempt)
    raise BuildError(
        f"failed to fetch pinned input for {source_id} after {DOWNLOAD_RETRIES + 1} attempts: {last_error}"
    ) from last_error


def _parse_bundle(content: bytes, source_id: str) -> SourceBundle:
    try:
        value = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BuildError(f"{source_id} canonical input is invalid JSON: {error}") from error
    if not isinstance(value, dict):
        raise BuildError(f"{source_id} canonical input must be a JSON object")
    try:
        return SourceBundle.from_dict(value)
    except (KeyError, TypeError, ValueError) as error:
        raise BuildError(f"{source_id} canonical input failed model validation: {error}") from error


def _effective_mode(manifest: Mapping[str, Any], override: BuildMode | str | None) -> BuildMode:
    raw = override if override is not None else manifest.get("build_mode", BuildMode.PRIVATE.value)
    try:
        return BuildMode(raw)
    except ValueError as error:
        raise BuildError("build_mode must be private or publishable") from error


def _validate_revision_policy(bundle: SourceBundle, build_mode: BuildMode) -> None:
    revision = bundle.source_revision
    if revision.import_mode is not ImportMode.CONTENT:
        raise BuildError(f"{revision.source_id} revision does not permit content import")
    if build_mode is BuildMode.PUBLISHABLE and revision.publication_mode is not PublicationMode.ALLOWED:
        raise BuildError(
            f"{revision.source_id} revision content is not cleared to publish ({revision.publication_mode.value})"
        )


def _source_counts(bundle: SourceBundle) -> dict[str, int]:
    return {
        "records": len(bundle.records),
        "senses": len(bundle.senses),
        "blocks": sum(len(sense.blocks) for sense in bundle.senses),
        "examples": sum(len(sense.examples) for sense in bundle.senses),
        "media": len(bundle.media),
    }


def build_from_manifest(
    source_manifest: str | Path,
    output: str | Path,
    report_path: str | Path,
    mode: BuildMode | str | None = None,
) -> dict[str, Any]:
    """Build one deterministic canonical snapshot after all checks succeed."""
    manifest_path = Path(source_manifest).resolve()
    manifest = _load_manifest(manifest_path)
    build_mode = _effective_mode(manifest, mode)
    manifest_directory = manifest_path.parent

    selections: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_selection in enumerate(manifest["sources"]):
        if not isinstance(raw_selection, dict):
            raise BuildError(f"source selection {index} must be an object")
        _strict_keys(
            raw_selection,
            {"source_id", "revision_id", "selection", "source_content_sha256", "input"},
            f"source selection {index}",
        )
        source_id = raw_selection.get("source_id")
        revision_id = raw_selection.get("revision_id")
        if not isinstance(source_id, str) or not source_id:
            raise BuildError(f"source selection {index} requires source_id")
        if source_id in seen:
            raise BuildError(f"source manifest selects {source_id} more than once")
        seen.add(source_id)
        if not isinstance(revision_id, str) or not revision_id:
            raise BuildError(f"{source_id} requires an immutable revision_id")
        try:
            selection_mode = SelectionMode(raw_selection.get("selection"))
            definition = validate_selection(source_id, build_mode, selection_mode)
        except (PolicyError, ValueError) as error:
            raise BuildError(str(error)) from error

        if selection_mode is SelectionMode.METADATA:
            if "input" in raw_selection or "source_content_sha256" in raw_selection:
                raise BuildError(f"{source_id} metadata selection cannot contain source content")
            selections.append(
                {
                    "source_id": source_id,
                    "revision_id": revision_id,
                    "selection": selection_mode.value,
                    "registry": definition.public_metadata(),
                }
            )
            continue

        source_content_sha256 = raw_selection.get("source_content_sha256")
        if not _valid_sha256(source_content_sha256):
            raise BuildError(f"{source_id} requires source_content_sha256")
        input_spec = raw_selection.get("input")
        if not isinstance(input_spec, dict):
            raise BuildError(f"{source_id} content selection requires a pinned input object")
        content, input_sha256 = _materialize_input(
            source_id,
            input_spec,
            manifest_directory,
        )
        bundle = _parse_bundle(content, source_id)
        revision = bundle.source_revision
        if (revision.source_id, revision.revision_id) != (source_id, revision_id):
            raise BuildError(f"{source_id} canonical input identity does not match its manifest pin")
        if revision.content_sha256 != source_content_sha256:
            raise BuildError(f"{source_id} source content SHA-256 does not match its manifest pin")
        _validate_revision_policy(bundle, build_mode)
        selections.append(
            {
                "source_id": source_id,
                "revision_id": revision_id,
                "selection": selection_mode.value,
                "input_sha256": input_sha256,
                **bundle.to_dict(),
            }
        )

    selections.sort(key=lambda item: (item["source_id"], item["revision_id"]))
    content_selections = [item for item in selections if item["selection"] == SelectionMode.CONTENT.value]
    metadata_selections = [item for item in selections if item["selection"] == SelectionMode.METADATA.value]
    counts = {"records": 0, "senses": 0, "blocks": 0, "examples": 0, "media": 0}
    source_reports = []
    for selection in selections:
        summary = {
            "source_id": selection["source_id"],
            "revision_id": selection["revision_id"],
            "selection": selection["selection"],
        }
        if selection["selection"] == SelectionMode.CONTENT.value:
            bundle = SourceBundle.from_dict(selection)
            source_counts = _source_counts(bundle)
            for name, value in source_counts.items():
                counts[name] += value
            summary.update(
                {
                    "source_content_sha256": bundle.source_revision.content_sha256,
                    "input_sha256": selection["input_sha256"],
                    **source_counts,
                }
            )
        source_reports.append(summary)

    build = {
        "format": "ugd-canonical-build",
        "format_version": BUILD_FORMAT_VERSION,
        "registry_version": REGISTRY_VERSION,
        "build_mode": build_mode.value,
        "source_count": len(selections),
        "content_source_count": len(content_selections),
        "metadata_source_count": len(metadata_selections),
        **counts,
        "sources": selections,
    }
    output_bytes = canonical_json_bytes(build)
    report = {
        "format": "ugd-build-report",
        "format_version": BUILD_FORMAT_VERSION,
        "registry_version": REGISTRY_VERSION,
        "build_mode": build_mode.value,
        "output_sha256": _sha256_bytes(output_bytes),
        "output_bytes": len(output_bytes),
        "source_count": len(selections),
        "content_source_count": len(content_selections),
        "metadata_source_count": len(metadata_selections),
        **counts,
        "sources": source_reports,
    }

    output_path = Path(output).resolve()
    report_output_path = Path(report_path).resolve()
    if output_path == report_output_path or manifest_path in {output_path, report_output_path}:
        raise BuildError("manifest, output and report paths must be distinct")
    _atomic_write(output_path, output_bytes)
    try:
        _atomic_write(report_output_path, canonical_json_bytes(report))
    except Exception:
        output_path.unlink(missing_ok=True)
        raise
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build a deterministic canonical grammar snapshot from an explicit, "
            "hash-pinned source manifest. Private mode is the default."
        )
    )
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--mode",
        choices=[mode.value for mode in BuildMode],
        help="override manifest build_mode; defaults to private when both are omitted",
    )
    arguments = parser.parse_args(argv)
    try:
        report = build_from_manifest(
            arguments.source_manifest,
            arguments.output,
            arguments.report,
            arguments.mode,
        )
    except (BuildError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

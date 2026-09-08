#!/usr/bin/env python3
"""Verify pinned canonical inputs and build a deterministic grammar snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Any, Mapping, cast
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

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
        SourceDefinition,
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
        SourceDefinition,
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


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BuildError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _decode_json(content: bytes, context: str) -> Any:
    try:
        return json.loads(content, object_pairs_hook=_unique_object)
    except BuildError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BuildError(f"invalid {context} JSON: {error}") from error


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as source:
            content = source.read(1024 * 1024 + 1)
    except OSError as error:
        raise BuildError(f"cannot read source manifest: {error}") from error
    if len(content) > 1024 * 1024:
        raise BuildError("source manifest exceeds 1 MiB limit")
    value = _decode_json(content, "source manifest")
    if not isinstance(value, dict):
        raise BuildError("source manifest must be a JSON object")
    _strict_keys(
        value,
        {"format", "format_version", "registry_version", "build_mode", "sources"},
        "source manifest",
    )
    format_version = value.get("format_version")
    if (
        value.get("format") != "ugd-source-manifest"
        or type(format_version) is not int
        or format_version != MANIFEST_FORMAT_VERSION
    ):
        raise BuildError("unsupported source manifest format or version")
    registry_version = value.get("registry_version")
    if type(registry_version) is not int or registry_version != REGISTRY_VERSION:
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
        with path.open("rb") as source:
            content = source.read(max_bytes + 1)
    except OSError as error:
        raise BuildError(f"cannot read pinned local input: {error}") from error
    if len(content) > max_bytes:
        raise BuildError(f"pinned local input exceeds {max_bytes} byte limit")
    if _sha256_bytes(content) != expected_sha256:
        raise BuildError("pinned local input SHA-256 mismatch")
    return content


class _PolicyRedirectHandler(HTTPRedirectHandler):
    def __init__(self, source_id: str) -> None:
        super().__init__()
        self.source_id = source_id

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request | None:
        validate_remote_url(self.source_id, newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download_once(source_id: str, url: str, max_bytes: int) -> bytes:
    request = Request(url, headers={"User-Agent": "bee-grammar-builder/1"})
    opener = build_opener(_PolicyRedirectHandler(source_id))
    with opener.open(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
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


def _prepare_temp_file(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temporary.write(content)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    return temporary_path


def _replace_path(source: Path, destination: Path) -> None:
    source.replace(destination)


def _atomic_write(path: Path, content: bytes) -> None:
    temporary_path = _prepare_temp_file(path, content)
    try:
        _replace_path(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _reserve_backup_path(path: Path) -> Path:
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as placeholder:
        backup = Path(placeholder.name)
    backup.unlink()
    return backup


def _atomic_write_pair(
    first_path: Path,
    first_content: bytes,
    second_path: Path,
    second_content: bytes,
) -> None:
    paths = (first_path, second_path)
    for path in paths:
        if path.exists() and not path.is_file():
            raise BuildError(f"output path is not a regular file: {path}")

    temporary_paths: dict[Path, Path] = {}
    backups: dict[Path, Path] = {}
    installed: set[Path] = set()
    try:
        temporary_paths[first_path] = _prepare_temp_file(first_path, first_content)
        temporary_paths[second_path] = _prepare_temp_file(second_path, second_content)
        for path in paths:
            if path.exists():
                backup = _reserve_backup_path(path)
                _replace_path(path, backup)
                backups[path] = backup
        for path in paths:
            _replace_path(temporary_paths[path], path)
            installed.add(path)
    except Exception as error:
        for path in reversed(paths):
            if path in installed:
                path.unlink(missing_ok=True)
        rollback_errors = []
        for path in reversed(paths):
            backup = backups.get(path)
            if backup is not None and backup.exists():
                try:
                    _replace_path(backup, path)
                except OSError as rollback_error:
                    rollback_errors.append(f"{path}: {rollback_error}")
        if rollback_errors:
            raise BuildError(
                "output write failed and rollback was incomplete: " + "; ".join(rollback_errors)
            ) from error
        raise
    else:
        for backup in backups.values():
            backup.unlink(missing_ok=True)
    finally:
        for temporary_path in temporary_paths.values():
            temporary_path.unlink(missing_ok=True)


def _materialize_input(
    source_id: str,
    spec: Mapping[str, Any],
    manifest_directory: Path,
) -> tuple[bytes, str, Path]:
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
        return _read_local_input(path, expected_sha256, max_bytes), expected_sha256, path

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
            return _read_local_input(cache, expected_sha256, max_bytes), expected_sha256, cache
        except BuildError:
            pass

    last_error: Exception | None = None
    for attempt in range(DOWNLOAD_RETRIES + 1):
        try:
            content = _download_once(source_id, url, max_bytes)
            if _sha256_bytes(content) != expected_sha256:
                raise BuildError(f"{source_id} downloaded input SHA-256 mismatch")
            _atomic_write(cache, content)
            return content, expected_sha256, cache
        except (BuildError, HTTPError, URLError, TimeoutError, OSError, PolicyError) as error:
            last_error = error
            if attempt < DOWNLOAD_RETRIES:
                time.sleep(2**attempt)
    raise BuildError(
        f"failed to fetch pinned input for {source_id} after {DOWNLOAD_RETRIES + 1} attempts: {last_error}"
    ) from last_error


def _parse_bundle(content: bytes, source_id: str) -> SourceBundle:
    value = _decode_json(content, f"{source_id} canonical input")
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


def _validate_revision_policy(
    bundle: SourceBundle,
    definition: SourceDefinition,
    build_mode: BuildMode,
) -> None:
    revision = bundle.source_revision
    if revision.access_mode is not definition.access_mode:
        raise BuildError(
            f"{revision.source_id} revision access mode does not match registry policy"
        )
    if revision.import_mode is not ImportMode.CONTENT:
        raise BuildError(f"{revision.source_id} revision does not permit content import")
    if (
        revision.publication_mode is PublicationMode.ALLOWED
        and definition.publication_mode is not PublicationMode.ALLOWED
    ):
        raise BuildError(
            f"{revision.source_id} revision exceeds registry publication policy"
        )
    if build_mode is BuildMode.PUBLISHABLE and revision.publication_mode is not PublicationMode.ALLOWED:
        raise BuildError(
            f"{revision.source_id} revision content is not cleared to publish ({revision.publication_mode.value})"
        )
    if (
        build_mode is BuildMode.PUBLISHABLE
        and definition.license_identifier is not None
        and revision.license.identifier != definition.license_identifier
    ):
        raise BuildError(
            f"{revision.source_id} revision licence does not match registry publication policy"
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
    loaded_bundles: dict[tuple[str, str], SourceBundle] = {}
    pinned_input_paths: set[Path] = set()
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
        content, input_sha256, pinned_input_path = _materialize_input(
            source_id,
            input_spec,
            manifest_directory,
        )
        pinned_input_paths.add(pinned_input_path)
        bundle = _parse_bundle(content, source_id)
        revision = bundle.source_revision
        if (revision.source_id, revision.revision_id) != (source_id, revision_id):
            raise BuildError(f"{source_id} canonical input identity does not match its manifest pin")
        if revision.content_sha256 != source_content_sha256:
            raise BuildError(f"{source_id} source content SHA-256 does not match its manifest pin")
        _validate_revision_policy(bundle, definition, build_mode)
        loaded_bundles[(source_id, revision_id)] = bundle
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
            bundle = loaded_bundles[(selection["source_id"], selection["revision_id"])]
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
    if output_path in pinned_input_paths or report_output_path in pinned_input_paths:
        raise BuildError("output and report cannot overwrite a pinned input or cache")
    _atomic_write_pair(
        output_path,
        output_bytes,
        report_output_path,
        canonical_json_bytes(report),
    )
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

"""Verify an installed Codex Rig package against its deterministic manifest.

## Purpose

Establish package identity, payload closure, hashes, and expected modes before an installed helper trusts its own
assets. The verification result is the package-level anchor for role-card and script integrity checks.

## Scope

Reads local package data through safe I/O and performs no installation, refresh, or lifecycle mutation. It compares the
manifest to the complete regular-file payload, leaving package creation and command execution to its callers.

## Usage

Import ``verify_package`` from package build/validation and installed helper startup paths. Call it against the package
root that will actually supply assets, then retain the returned identity facts while consuming those assets. The live
denial harness may instead invoke this file with ``--root`` and ``--expected-package-hash`` to compare the fully
verified candidate with an independently recorded manifest digest.

## Used by

``build_package.py``, installed role-link verification, the live local reviewer wave denial harness, and package identity tests
call this verifier. These callers use the same contract both before shipping a package and before trusting one selected
by local Codex discovery.

## Outputs

Returns a ``PackageIdentity`` with manifest records and verified root facts that callers can bind to their subsequent
operation. The records expose canonical version, file hashes, modes, and root identity without authorizing any mutation.

## Failure

Malformed manifests, unexpected files, links, hashes, modes, or references raise ``PackageIdentityError`` and fail
closed. Consumers must stop startup or release validation on this error because an unverified package may contain
substituted runtime content.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn

from _safe_package_io import inventory_package_files, read_safe_file


MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_PACKAGE_FILE_BYTES = 16 * 1024 * 1024
EXCLUDED_PARTS = frozenset({"__pycache__", ".pytest_cache", ".reports"})
EXCLUDED_FILES = frozenset({".coverage", "package-manifest.json"})
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
MODE_PATTERN = re.compile(r"0[0-7]{3}")


class PackageIdentityError(ValueError):
    """Report a package identity invariant that could not be established."""


@dataclass(frozen=True)
class PackageIdentity:
    """Summarize the verified immutable package identity."""

    version: str
    package_hash: str
    files_verified: int
    mode_status: str


def _reject_constant(value: str) -> NoReturn:
    """Reject non-finite JSON constants in identity inputs."""
    raise PackageIdentityError(f"invalid JSON constant: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON keys before they can shadow identity fields."""
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise PackageIdentityError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _json_object(payload: bytes, label: str) -> dict[str, Any]:
    """Decode one strict UTF-8 JSON object from verified bytes."""
    try:
        value = json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PackageIdentityError(f"invalid {label} JSON") from error
    if not isinstance(value, dict):
        raise PackageIdentityError(f"invalid {label} JSON")
    return value


def _safe_relative(value: object, label: str) -> str:
    """Require one canonical relative POSIX package path."""
    if not isinstance(value, str) or any(ord(character) < 32 for character in value):
        raise PackageIdentityError(f"invalid {label}")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or "\\" in value
        or any(part in {"", ".", ".."} or ":" in part for part in path.parts)
    ):
        raise PackageIdentityError(f"invalid {label}: {value}")
    if path.as_posix() != value:
        raise PackageIdentityError(f"invalid {label}: {value}")
    return value


def _digest(value: object, label: str) -> str:
    """Require one canonical lowercase SHA-256 digest."""
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise PackageIdentityError(f"invalid {label}")
    return value


def _file_records(manifest: dict[str, Any]) -> tuple[list[dict[str, str]], dict[str, dict[str, str]]]:
    """Validate and index the complete manifest file roster."""
    records = manifest.get("files")
    if not isinstance(records, list):
        raise PackageIdentityError("package file records missing")
    validated: list[dict[str, str]] = []
    indexed: dict[str, dict[str, str]] = {}
    folded: set[str] = set()
    for item in records:
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "mode"}:
            raise PackageIdentityError("invalid package file record")
        path = _safe_relative(item["path"], "package file path")
        digest = _digest(item["sha256"], f"package file hash: {path}")
        mode = item["mode"]
        if not isinstance(mode, str) or MODE_PATTERN.fullmatch(mode) is None:
            raise PackageIdentityError(f"invalid package file mode: {path}")
        if path in indexed or path.casefold() in folded:
            raise PackageIdentityError(f"duplicate package file record: {path}")
        record = {"path": path, "sha256": digest, "mode": mode}
        validated.append(record)
        indexed[path] = record
        folded.add(path.casefold())
    if [record["path"] for record in validated] != sorted(indexed):
        raise PackageIdentityError("package file records are not sorted")
    return validated, indexed


def _require_record_hash(
    value: object,
    *,
    path: str,
    indexed: dict[str, dict[str, str]],
    label: str,
) -> None:
    """Bind one manifest sub-record to its complete file-roster digest."""
    expected = _digest(value, label)
    record = indexed.get(path)
    if record is None or record["sha256"] != expected:
        raise PackageIdentityError(f"{label} mismatch")


def _validate_references(manifest: dict[str, Any], indexed: dict[str, dict[str, str]]) -> None:
    """Bind skills, roles, generator, and bootstrap records to verified files."""
    skills = manifest.get("skills")
    roles = manifest.get("roles")
    if not isinstance(skills, list) or not isinstance(roles, list):
        raise PackageIdentityError("package public roster missing")
    for skill in skills:
        if not isinstance(skill, dict) or set(skill) != {"id", "path"}:
            raise PackageIdentityError("invalid package skill record")
        path = _safe_relative(skill["path"], "skill path")
        if path not in indexed:
            raise PackageIdentityError(f"missing package file record: {path}")
    for role in roles:
        if not isinstance(role, dict) or set(role) != {"id", "path", "sha256", "runtime"}:
            raise PackageIdentityError("invalid package role record")
        path = _safe_relative(role["path"], "role path")
        _require_record_hash(role["sha256"], path=path, indexed=indexed, label=f"role hash: {role['id']}")

    bootstrap = manifest.get("bootstrap")
    generator = manifest.get("generator")
    if not isinstance(bootstrap, dict) or set(bootstrap) != {"protocol", "helper", "sha256"}:
        raise PackageIdentityError("invalid bootstrap manifest")
    if not isinstance(generator, dict) or set(generator) != {"version", "path", "sha256"}:
        raise PackageIdentityError("invalid generator manifest")
    helper_path = _safe_relative(bootstrap["helper"], "bootstrap helper path")
    generator_path = _safe_relative(generator["path"], "generator path")
    _require_record_hash(bootstrap["sha256"], path=helper_path, indexed=indexed, label="bootstrap hash")
    _require_record_hash(generator["sha256"], path=generator_path, indexed=indexed, label="generator hash")


def verify_package(root: Path | str, *, enforce_modes: bool | None = None) -> PackageIdentity:
    """Verify complete package bytes and native-applicable filesystem invariants.

    Args:
        root: Installed package root containing ``package-manifest.json``.
        enforce_modes: Override native mode enforcement for focused validation.
            Defaults to exact modes on POSIX and not-applicable on Windows.

    Returns:
        Immutable package identity and mode-check status.

    Raises:
        PackageIdentityError: If any identity, containment, or stability check fails.
    """
    package_root = Path(root)
    check_modes = os.name != "nt" if enforce_modes is None else enforce_modes
    try:
        manifest_file = read_safe_file(package_root, "package-manifest.json", maximum=MAX_MANIFEST_BYTES)
        manifest = _json_object(manifest_file.payload, "package manifest")
        if manifest.get("schema") != 1 or manifest.get("plugin") != "codex-rig":
            raise PackageIdentityError("package manifest identity mismatch")
        version = manifest.get("version")
        if not isinstance(version, str) or not version:
            raise PackageIdentityError("package version missing")
        records, indexed = _file_records(manifest)
        _validate_references(manifest, indexed)

        before = inventory_package_files(
            package_root,
            excluded_parts=EXCLUDED_PARTS,
            excluded_files=EXCLUDED_FILES,
        )
        expected_paths = tuple(record["path"] for record in records)
        if before != expected_paths:
            raise PackageIdentityError(
                f"package file closure mismatch: expected={list(expected_paths)} observed={list(before)}"
            )

        verified_payloads: dict[str, bytes] = {}
        for record in records:
            path = record["path"]
            safe_file = read_safe_file(package_root, path, maximum=MAX_PACKAGE_FILE_BYTES)
            if hashlib.sha256(safe_file.payload).hexdigest() != record["sha256"]:
                raise PackageIdentityError(f"hash mismatch: {path}")
            if check_modes and f"{safe_file.mode:04o}" != record["mode"]:
                raise PackageIdentityError(f"mode mismatch: {path}")
            verified_payloads[path] = safe_file.payload

        after = inventory_package_files(
            package_root,
            excluded_parts=EXCLUDED_PARTS,
            excluded_files=EXCLUDED_FILES,
        )
        if after != before:
            raise PackageIdentityError("package file closure changed during verification")
        final_manifest = read_safe_file(package_root, "package-manifest.json", maximum=MAX_MANIFEST_BYTES)
        if final_manifest.payload != manifest_file.payload:
            raise PackageIdentityError("package manifest changed during verification")

        plugin_payload = verified_payloads.get(".codex-plugin/plugin.json")
        if plugin_payload is None:
            raise PackageIdentityError("plugin manifest file record missing")
        plugin = _json_object(plugin_payload, "plugin manifest")
        if plugin.get("name") != "codex-rig" or plugin.get("version") != version:
            raise PackageIdentityError("plugin manifest identity mismatch")
        return PackageIdentity(
            version=version,
            package_hash=hashlib.sha256(manifest_file.payload).hexdigest(),
            files_verified=len(records),
            mode_status="pass" if check_modes else "not-applicable",
        )
    except PackageIdentityError:
        raise
    except OSError as error:
        raise PackageIdentityError(str(error)) from error


def main() -> int:
    """Verify one package against an independently supplied manifest digest."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--expected-package-hash", required=True)
    arguments = parser.parse_args()
    if SHA256_PATTERN.fullmatch(arguments.expected_package_hash) is None:
        return 2
    try:
        identity = verify_package(arguments.root)
    except (OSError, UnicodeError, PackageIdentityError):
        return 2
    return 0 if identity.package_hash == arguments.expected_package_hash else 2


if __name__ == "__main__":
    raise SystemExit(main())

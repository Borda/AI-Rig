#!/usr/bin/env python3
"""Build and verify Codex Rig's deterministic installed-package manifest.

## Purpose

Enumerate the complete shipped payload, record stable hashes/modes, and reject package drift before installation or
release. The generated manifest is the deterministic inventory consumed by package validation and installed-helper
identity checks.

## Scope

Creates or checks local manifest data for this plugin; it neither publishes a release nor changes a remote marketplace.
Its writes are limited to the repository's canonical ``package-manifest.json`` when the explicit update action is
selected.

## Usage

Run ``python scripts/build_package.py --update`` after shipped-file changes, then ``--check`` before delivery. Use
``--check`` in automated gates so a stale manifest fails without modifying the working tree.

## Used by

Package validation, release verification, and Codex Rig packaging tests call this builder. These callers use the
manifest as a reproducible package boundary rather than reconstructing file lists independently.

## Outputs

Prints whether the manifest is current and, with ``--update``, writes the canonical ``package-manifest.json`` bytes. The
check result identifies drift through a non-zero exit so release automation can stop before shipping inconsistent
assets.

## Failure

Missing role/skill metadata, invalid manifest structure, unsafe payload object, or a stale manifest produces a non-zero
error and names the local cause. The script does not silently regenerate during a check, which keeps accidental source
changes visible to the maintainer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

from _package_identity import verify_package


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = PACKAGE_ROOT / "package-manifest.json"
PLUGIN_MANIFEST_PATH = PACKAGE_ROOT / ".codex-plugin" / "plugin.json"
WORKFLOW_SKILLS = (
    "agent-shims",
    "assess",
    "audit",
    "calibrate",
    "code-remediate",
    "code-review",
    "implement",
    "investigate",
    "kaggle",
    "manage",
    "optimize",
    "release",
    "research",
    "sync",
)
ROLE_IDS = (
    "challenger",
    "cicd-steward",
    "curator",
    "data-steward",
    "delegation-lead",
    "doc-scribe",
    "linting-expert",
    "oss-shepherd",
    "qa-specialist",
    "scientist",
    "security-auditor",
    "solution-architect",
    "squeezer",
    "sw-engineer",
    "web-explorer",
)
RUNTIME_KEYS = ("model", "model_reasoning_effort", "approval_policy", "sandbox_mode")
EXCLUDED_PARTS = frozenset({"__pycache__", ".pytest_cache", ".reports"})
EXCLUDED_FILES = frozenset({".coverage", "package-manifest.json"})


def sha256(payload: bytes) -> str:
    """Return the lowercase SHA-256 digest for exact bytes."""
    return hashlib.sha256(payload).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    """Load one JSON object or raise a descriptive package error."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path.relative_to(PACKAGE_ROOT)}")
    return payload


def parse_role_frontmatter(path: Path) -> dict[str, str]:
    """Parse the flat canonical role-card frontmatter used by the package."""
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "---":
        raise ValueError(f"missing role frontmatter: {path.relative_to(PACKAGE_ROOT)}")
    try:
        end = lines.index("---", 1)
    except ValueError as error:
        raise ValueError(f"unterminated role frontmatter: {path.relative_to(PACKAGE_ROOT)}") from error
    values: dict[str, str] = {}
    for line in lines[1:end]:
        key, separator, value = line.partition(":")
        if not separator or not key or key in values:
            raise ValueError(f"invalid role frontmatter: {path.relative_to(PACKAGE_ROOT)}")
        values[key] = value.strip()
    return values


def iter_payload_files() -> list[Path]:
    """Return every contained regular payload file in canonical order."""
    discovered: list[Path] = []
    casefold_paths: set[str] = set()
    for path in sorted(PACKAGE_ROOT.rglob("*")):
        relative = path.relative_to(PACKAGE_ROOT)
        if path.name in EXCLUDED_FILES or any(part in EXCLUDED_PARTS for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError(f"symlink payload forbidden: {relative.as_posix()}")
        if not path.is_file():
            continue
        normalized = relative.as_posix()
        folded = normalized.casefold()
        if folded in casefold_paths:
            raise ValueError(f"case-colliding payload path: {normalized}")
        casefold_paths.add(folded)
        discovered.append(path)
    return discovered


def file_record(path: Path) -> dict[str, str]:
    """Build one stable file identity record."""
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"non-regular payload: {path.relative_to(PACKAGE_ROOT)}")
    return {
        "path": path.relative_to(PACKAGE_ROOT).as_posix(),
        "sha256": sha256(path.read_bytes()),
        "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
    }


def build_manifest() -> dict[str, Any]:
    """Build the complete role-card-injected package identity manifest."""
    plugin = load_json(PLUGIN_MANIFEST_PATH)
    if plugin.get("name") != "codex-rig" or not isinstance(plugin.get("version"), str):
        raise ValueError("plugin manifest identity is invalid")
    payload_files = iter_payload_files()

    skills = []
    for skill_id in WORKFLOW_SKILLS:
        path = PACKAGE_ROOT / "skills" / skill_id / "SKILL.md"
        if not path.is_file():
            raise ValueError(f"missing workflow skill: {skill_id}")
        skills.append({"id": skill_id, "path": f"skills/{skill_id}/SKILL.md"})

    roles = []
    for role_id in ROLE_IDS:
        path = PACKAGE_ROOT / "roles" / role_id / "ROLE.md"
        if not path.is_file():
            raise ValueError(f"missing canonical role card: {role_id}")
        frontmatter = parse_role_frontmatter(path)
        if frontmatter.get("role_id") != role_id or frontmatter.get("name") != f"codex-rig-{role_id}":
            raise ValueError(f"role identity mismatch: {role_id}")
        if frontmatter.get("fallback_modes") != "[shim, built-in-injected, inline]":
            raise ValueError(f"role fallback modes mismatch: {role_id}")
        runtime = {key: frontmatter.get(key) for key in RUNTIME_KEYS}
        if not all(isinstance(value, str) and value for value in runtime.values()):
            raise ValueError(f"role runtime defaults incomplete: {role_id}")
        roles.append(
            {
                "id": role_id,
                "path": f"roles/{role_id}/ROLE.md",
                "sha256": sha256(path.read_bytes()),
                "runtime": runtime,
            }
        )

    helper = PACKAGE_ROOT / "scripts" / "verify_role_link.py"
    if not helper.is_file():
        raise ValueError("missing linked-role verifier")
    generator = PACKAGE_ROOT / "scripts" / "generate_roles.py"
    if not generator.is_file():
        raise ValueError("missing thin-role generator")
    return {
        "schema": 1,
        "plugin": "codex-rig",
        "version": plugin["version"],
        "release_profile": "role-card-injected",
        "features": {"manager": True, "hooks": True, "mcp": False, "generated_shims": False},
        "skills": skills,
        "roles": roles,
        "bootstrap": {
            "protocol": 1,
            "helper": "scripts/verify_role_link.py",
            "sha256": sha256(helper.read_bytes()),
        },
        "generator": {
            "version": 1,
            "path": "scripts/generate_roles.py",
            "sha256": sha256(generator.read_bytes()),
        },
        "files": [file_record(path) for path in payload_files],
        "excluded": [
            "__pycache__",
            ".pytest_cache",
            ".reports",
            ".coverage",
            ".mcp.json",
            "hooks",
            "skills/agent-shims",
        ],
    }


def encode_manifest(manifest: dict[str, Any]) -> bytes:
    """Encode a stable human-readable manifest."""
    return (json.dumps(manifest, indent=2, ensure_ascii=True, sort_keys=True) + "\n").encode("utf-8")


def enforces_posix_modes() -> bool:
    """Return whether this host can authoritatively generate POSIX mode records."""
    return os.name != "nt"


def manifests_match(current: dict[str, Any], generated: dict[str, Any], *, enforce_modes: bool) -> bool:
    """Compare schema-1 manifests while optionally excluding filesystem mode values."""
    if enforce_modes:
        return current == generated

    def portable(value: dict[str, Any]) -> dict[str, Any]:
        normalized = json.loads(json.dumps(value))
        files = normalized.get("files")
        if isinstance(files, list):
            for record in files:
                if isinstance(record, dict):
                    record.pop("mode", None)
        return normalized

    return portable(current) == portable(generated)


def parse_args() -> argparse.Namespace:
    """Parse manifest generation arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="fail when package-manifest.json differs from generation")
    mode.add_argument("--update", action="store_true", help="write current hashes to package-manifest.json")
    return parser.parse_args()


def main() -> None:
    """Generate the manifest or check the committed bytes."""
    args = parse_args()
    enforce_modes = enforces_posix_modes()
    if args.update and not enforce_modes:
        raise SystemExit("package-manifest-update-requires-posix")
    if args.check and not enforce_modes:
        verify_package(PACKAGE_ROOT, enforce_modes=False)
        print("Package manifest is current.")
        return
    expected = encode_manifest(build_manifest())
    current = MANIFEST_PATH.read_bytes() if MANIFEST_PATH.is_file() else None
    if args.check:
        if current != expected:
            raise SystemExit("package-manifest-out-of-date")
        print("Package manifest is current.")
        return
    if current == expected:
        print("Package manifest is current.")
        return
    temporary = MANIFEST_PATH.with_suffix(".json.tmp")
    temporary.write_bytes(expected)
    os.replace(temporary, MANIFEST_PATH)
    print(f"Updated package manifest: {MANIFEST_PATH.relative_to(PACKAGE_ROOT)}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        print(f"package-build-error: {error}", file=sys.stderr)
        raise SystemExit(2) from error

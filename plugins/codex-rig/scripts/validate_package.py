#!/usr/bin/env python3
"""Validate Codex Rig's packaged role-card contract and publication payload.

## Purpose

Ensure a generated manifest and shipped files form an installable, safe, internally consistent plugin release. It checks
both the declared roster and the publication payload so local validation catches omissions and accidental private
material before handoff.

## Scope

Checks local payload semantics and publication exclusions; it does not write a manifest, upload artifacts, or publish
remotely. Manifest generation remains the responsibility of ``build_package.py``; this script serves as a release gate
that only reads existing state.

## Usage

Run ``python scripts/validate_package.py`` after ``build_package.py --check`` and before a release handoff. A maintainer
should resolve the named local failure and rerun the gate before treating the package as deliverable.

## Used by

Release/implement gates and package validation acceptance tests invoke this validator. These callers rely on a nonzero
exit status to block a package whose generated or shipped contents do not match the public contract.

## Outputs

Prints a passed validation confirmation after manifest, role, skill, and publication-payload checks all agree. The
message identifies the validated package root, while the subprocess and semantic checks remain visible through their
exit status.

## Failure

Stale manifest, missing required asset, leaked private material, malformed version data, or invalid package semantics
exits non-zero with a local reason. Validation intentionally stops before publication when any one of these checks
cannot establish a safe package boundary.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any

from _package_identity import verify_package


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = PACKAGE_ROOT / "scripts" / "build_package.py"
EXPECTED_SKILLS = {
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
}
EXPECTED_ROLES = {
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
}
PRIVATE_PATH_PATTERN = re.compile(
    rb"(?:/(?:Users|home)/[^/\\\s]+|(?i:[A-Z]:[\\/]+Users[\\/]+[^/\\\s]+))",
)
PRIVATE_KEY_MARKER = b"BEGIN " + b"PRIVATE KEY"


def load_json(path: Path) -> dict[str, Any]:
    """Load one JSON object for semantic validation."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected object: {path.relative_to(PACKAGE_ROOT)}")
    return payload


def validate_semantics() -> None:
    """Validate feature boundaries and exact public rosters."""
    manifest = load_json(PACKAGE_ROOT / "package-manifest.json")
    plugin = load_json(PACKAGE_ROOT / ".codex-plugin" / "plugin.json")
    skill_ids = {item["id"] for item in manifest.get("skills", [])}
    role_ids = {item["id"] for item in manifest.get("roles", [])}
    if skill_ids != EXPECTED_SKILLS or role_ids != EXPECTED_ROLES:
        raise ValueError("public roster mismatch")
    if manifest.get("version") != plugin.get("version") or manifest.get("release_profile") != "role-card-injected":
        raise ValueError("plugin identity mismatch")
    if manifest.get("features") != {"manager": True, "hooks": True, "mcp": False, "generated_shims": False}:
        raise ValueError("role-card-injected feature boundary mismatch")
    if (
        not (PACKAGE_ROOT / "hooks" / "hooks.json").is_file()
        or not (PACKAGE_ROOT / "hooks" / "session_start.py").is_file()
    ):
        raise ValueError("declared hook payload is incomplete")
    if (PACKAGE_ROOT / ".mcp.json").exists():
        raise ValueError("undeclared MCP payload present")
    if any((PACKAGE_ROOT / "skills" / name).exists() for name in ("review", "resolve")):
        raise ValueError("retired skill present")


def validate_publication_payload() -> None:
    """Reject machine-local paths and credentials in exact manifest-declared payload files."""
    records = load_json(PACKAGE_ROOT / "package-manifest.json").get("files")
    if not isinstance(records, list):
        raise ValueError("publication manifest files invalid")
    for record in records:
        relative_value = record.get("path") if isinstance(record, dict) else None
        relative = PurePosixPath(relative_value) if isinstance(relative_value, str) else PurePosixPath(".")
        if not relative_value or relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError("publication manifest path invalid")
        if "tests" in relative.parts:
            continue
        path = PACKAGE_ROOT.joinpath(*relative.parts)
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"publication payload missing: {relative.as_posix()}")
        payload = path.read_bytes()
        if PRIVATE_PATH_PATTERN.search(payload) or PRIVATE_KEY_MARKER in payload:
            raise ValueError(f"private material in payload: {relative.as_posix()}")


def parse_args() -> argparse.Namespace:
    """Parse package validation arguments."""
    return argparse.ArgumentParser(description=__doc__).parse_args()


def main() -> None:
    """Run deterministic and semantic package validation."""
    parse_args()
    generated = subprocess.run([sys.executable, str(BUILD_SCRIPT), "--check"], check=False)
    if generated.returncode != 0:
        raise SystemExit(generated.returncode)
    try:
        verify_package(PACKAGE_ROOT)
        validate_semantics()
        validate_publication_payload()
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        print(f"package-validation-error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
    print(f"Package validation passed: {PACKAGE_ROOT}")


if __name__ == "__main__":
    main()

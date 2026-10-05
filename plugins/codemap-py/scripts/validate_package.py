#!/usr/bin/env python3
"""Validate a built codemap-py package against its manifest and closure rules.

Given a directory produced by ``build_package.py`` (its ``package-manifest.json``
plus payload), assert that the package is a closed, self-contained, portable
artifact:

- inventory identity: every manifest entry exists on disk with a matching
  SHA-256, and no un-manifested file is present (no missing, extra, or modified
  payload);
- portability: no symlinks, no case-folding collisions, and no absolute or
  parent-escaping paths in the manifest;
- hygiene: no source-checkout / personal-home path bytes and no obvious secret
  material (private-key headers, provider token prefixes) in the payload;
- closure: both runtime manifests and all required product documents are
  present, and no default ``skills/`` directory or ``hooks/hooks.json`` leaks
  in;
- declared-component closure: every component the manifests declare exists on
  disk AND in the inventory — the Claude ``skills`` pointer directory, each
  package-manifest roster skill's ``SKILL.md`` (roster matches the on-disk skill
  dirs exactly), both runtime ``hooks`` pointer files, and every hook helper they
  reference; the Codex manifest declares ``skills: ./codex-skills/`` and ships
  ``codex-skills/`` with the same six-skill roster as Claude;
- executable modes: on POSIX, each file's on-disk executable bit matches its
  manifest ``exec`` flag (informational on Windows).

CLI::

    python plugins/codemap-py/scripts/validate_package.py --package <dir>

Exit ``0`` when clean; ``1`` with named findings on stderr; ``2`` on usage error
(missing manifest / directory).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

_MANIFEST = "package-manifest.json"
_REQUIRED_DOCS: tuple[str, ...] = ("README.md", "LICENSE", "NOTICE", "CHANGELOG.md")
_REQUIRED_MANIFESTS: tuple[str, ...] = (".claude-plugin/plugin.json", ".codex-plugin/plugin.json")
_FORBIDDEN_PATHS: tuple[str, ...] = ("skills", "hooks/hooks.json")

# Basic secret material — private-key headers and common token prefixes.
_SECRET_PATTERNS: tuple[re.Pattern[bytes], ...] = (
    re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(rb"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(rb"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(rb"AKIA[0-9A-Z]{16}"),
    re.compile(rb"xox[baprs]-[A-Za-z0-9-]{10,}"),
)


def _load_manifest(package: Path) -> dict:
    """Return the parsed manifest, or raise ``FileNotFoundError`` if absent."""
    path = package / _MANIFEST
    if not path.is_file():
        raise FileNotFoundError(f"missing {_MANIFEST} in {package}")
    return json.loads(path.read_text(encoding="utf-8"))


def _check_paths(manifest: dict) -> list[str]:
    """Reject absolute, drive-qualified, or parent-escaping manifest paths."""
    findings: list[str] = []
    for record in manifest.get("files", []):
        relative = record["path"]
        if relative.startswith("/") or re.match(r"^[A-Za-z]:", relative) or ".." in relative.split("/"):
            findings.append(f"non-relative manifest path: {relative}")
    return findings


def _check_inventory(package: Path, manifest: dict) -> list[str]:
    """Reject missing, modified, or extra (un-manifested) payload files."""
    findings: list[str] = []
    manifest_paths: set[str] = set()
    for record in manifest.get("files", []):
        relative = record["path"]
        manifest_paths.add(relative)
        disk = package / relative
        if not disk.is_file():
            findings.append(f"missing payload file: {relative}")
            continue
        if hashlib.sha256(disk.read_bytes()).hexdigest() != record["sha256"]:
            findings.append(f"modified payload file: {relative}")
    for path in sorted(package.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(package).as_posix()
        if relative != _MANIFEST and relative not in manifest_paths:
            findings.append(f"extra un-manifested file: {relative}")
    return findings


def _check_portability(package: Path) -> list[str]:
    """Reject symlinks and case-folding path collisions on disk."""
    findings: list[str] = []
    folded: dict[str, str] = {}
    for path in sorted(package.rglob("*")):
        relative = path.relative_to(package).as_posix()
        if path.is_symlink():
            findings.append(f"symlink in package: {relative}")
        if path.is_file():
            key = relative.casefold()
            if key in folded:
                findings.append(f"case collision: {relative} vs {folded[key]}")
            folded[key] = relative
    return findings


def _personal_roots() -> tuple[bytes, ...]:
    """Return the concrete build-host home path(s) as bytes to scan for.

    Scanning for the real home (rather than a generic ``/Users/`` pattern) avoids false positives on documented
    placeholder paths (``/Users/x/``) and on tools whose source legitimately names those prefixes, while still catching
    a payload file that baked in this checkout's absolute home.
    """
    candidates = {str(Path.home()), os.path.realpath(Path.home())}
    return tuple(sorted(root.encode("utf-8") for root in candidates if root not in ("", "/")))


def _check_bytes(package: Path, manifest: dict) -> list[str]:
    """Reject personal-path leaks and secret material in payload bytes."""
    findings: list[str] = []
    roots = _personal_roots()
    for record in manifest.get("files", []):
        disk = package / record["path"]
        if not disk.is_file():
            continue
        data = disk.read_bytes()
        if any(root in data for root in roots):
            findings.append(f"personal-path reference in payload: {record['path']}")
        if any(pattern.search(data) for pattern in _SECRET_PATTERNS):
            findings.append(f"secret-like material in payload: {record['path']}")
    return findings


def _check_closure(package: Path) -> list[str]:
    """Reject a package missing manifests/docs or leaking default runtime paths."""
    findings: list[str] = []
    for required in (*_REQUIRED_MANIFESTS, *_REQUIRED_DOCS):
        if not (package / required).is_file():
            findings.append(f"missing required member: {required}")
    for forbidden in _FORBIDDEN_PATHS:
        if (package / forbidden).exists():
            findings.append(f"forbidden default path present: {forbidden}")
    return findings


def _check_exec_modes(package: Path, manifest: dict) -> list[str]:
    """On POSIX, require each on-disk executable bit to match its manifest flag.

    On non-POSIX hosts the executable bit is unreliable, so the check is informational only; the manifest flag remains
    authoritative.
    """
    if os.name != "posix":
        return []
    findings: list[str] = []
    for record in manifest.get("files", []):
        disk = package / record["path"]
        if not disk.is_file():
            continue
        on_disk = bool(disk.stat().st_mode & 0o111)
        if on_disk != bool(record.get("exec", False)):
            findings.append(f"exec flag mismatch (disk={on_disk}, manifest={record.get('exec')}): {record['path']}")
    return findings


def _load_json(path: Path) -> dict:
    """Return a parsed JSON object, or ``{}`` when the file is absent/unreadable."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _pointer_path(pointer: str) -> str:
    """Normalize a ``"./dir/"`` manifest pointer to a package-relative path."""
    trimmed = pointer.removeprefix("./")
    return trimmed.rstrip("/")


def _check_claude_roster(package: Path, manifest: dict, inventory: set[str]) -> list[str]:
    """Require the Claude roster to match the on-disk skill dirs and its SKILL.md set."""
    findings: list[str] = []
    roster = set(manifest.get("skills", {}).get("claude", []))
    skills_dir = package / "claude-skills"
    disk_skills = {d.name for d in skills_dir.iterdir() if (d / "SKILL.md").is_file()} if skills_dir.is_dir() else set()
    if roster != disk_skills:
        findings.append(f"claude roster {sorted(roster)} != skill dirs with SKILL.md {sorted(disk_skills)}")
    for skill in sorted(roster):
        relative = f"claude-skills/{skill}/SKILL.md"
        if not (package / relative).is_file():
            findings.append(f"rostered skill missing SKILL.md: {relative}")
        elif relative not in inventory:
            findings.append(f"rostered skill not in inventory: {relative}")
    codex_roster = set(manifest.get("skills", {}).get("codex", []))
    if codex_roster != roster:
        findings.append(f"codex roster {sorted(codex_roster)} != claude roster {sorted(roster)}")
    return findings


def _check_hook_helpers(package: Path, hooks_relative: str, inventory: set[str]) -> list[str]:
    """Require every hook helper referenced by the wiring file to exist and be inventoried.

    ``hooks/_hookutil.py`` is checked unconditionally: every hook script imports it, but no wiring file names it, so a
    package shipping the hooks without it would otherwise validate clean while every hook crashes at import time.
    """
    findings: list[str] = []
    blob = json.dumps(_load_json(package / hooks_relative))
    referenced = set(re.findall(r"hooks/[A-Za-z0-9_.-]+\.py", blob)) | {"hooks/_hookutil.py"}
    for ref in sorted(referenced):
        if not (package / ref).is_file():
            findings.append(f"referenced hook helper missing: {ref}")
        elif ref not in inventory:
            findings.append(f"referenced hook helper not in inventory: {ref}")
    return findings


def _check_native_questions(package: Path, codex: dict, inventory: set[str]) -> list[str]:
    """Verify the declared native form configuration and provider belong to the closed package."""
    if "mcpServers" not in codex:
        return []
    findings: list[str] = []
    question_config_path = package / ".codex-mcp.json"
    if codex.get("mcpServers") != "./.codex-mcp.json":
        findings.append("codex manifest must declare native questions: ./.codex-mcp.json")
    if ".codex-mcp.json" not in inventory or not question_config_path.is_file():
        findings.append("native question configuration missing from inventory")
    else:
        expected_questions = {
            "mcpServers": {
                "codemap-input": {
                    "command": "python",
                    "cwd": ".",
                    "args": ["shared/user_questions_mcp.py", "--stdio"],
                    "tool_timeout_sec": 3600,
                }
            }
        }
        if _load_json(question_config_path) != expected_questions:
            findings.append("native question configuration differs from the packaged local provider")
    if "shared/user_questions_mcp.py" not in inventory or not (package / "shared/user_questions_mcp.py").is_file():
        findings.append("native question provider missing from inventory")
    return findings


def _check_declared_components(package: Path, manifest: dict, inventory: set[str]) -> list[str]:
    """Require every component both runtime manifests declare to exist in the inventory."""
    findings = _check_claude_roster(package, manifest, inventory)
    claude = _load_json(package / ".claude-plugin" / "plugin.json")
    skills_ptr = claude.get("skills")
    if skills_ptr and not (package / _pointer_path(skills_ptr)).is_dir():
        findings.append(f"claude skills pointer dir missing: {_pointer_path(skills_ptr)}")
    codex = _load_json(package / ".codex-plugin" / "plugin.json")
    findings += _check_native_questions(package, codex, inventory)
    if codex.get("hooks") != "./hooks/codex-hooks.json":
        findings.append(f"codex manifest must declare hooks: ./hooks/codex-hooks.json, got {codex.get('hooks')!r}")
    for runtime, runtime_manifest in (("claude", claude), ("codex", codex)):
        hooks_ptr = runtime_manifest.get("hooks")
        if not hooks_ptr:
            continue
        hooks_relative = _pointer_path(hooks_ptr)
        if not (package / hooks_relative).is_file():
            findings.append(f"{runtime} hooks pointer file missing: {hooks_relative}")
        elif hooks_relative not in inventory:
            findings.append(f"{runtime} hooks file not in inventory: {hooks_relative}")
        else:
            findings += _check_hook_helpers(package, hooks_relative, inventory)
    if codex.get("skills") != "./codex-skills/":
        findings.append(f"codex manifest must declare skills: ./codex-skills/, got {codex.get('skills')!r}")
    codex_roster = set(manifest.get("skills", {}).get("codex", []))
    if not (package / "codex-skills").is_dir() and codex_roster:
        findings.append("codex-skills directory missing (Codex ships the same six-skill roster as Claude)")
    for skill in sorted(codex_roster):
        relative = f"codex-skills/{skill}/SKILL.md"
        if not (package / relative).is_file():
            findings.append(f"rostered codex skill missing SKILL.md: {relative}")
        elif relative not in inventory:
            findings.append(f"rostered codex skill not in inventory: {relative}")
    return findings


def validate_package(package: Path) -> list[str]:
    """Return all validation findings for ``package`` (empty when clean).

    Args:
        package: A directory produced by ``build_package.py``.

    Returns:
        Named findings, one per rule violation.
    """
    manifest = _load_manifest(package)
    inventory = {record["path"] for record in manifest.get("files", [])}
    findings: list[str] = []
    findings += _check_paths(manifest)
    findings += _check_inventory(package, manifest)
    findings += _check_portability(package)
    findings += _check_bytes(package, manifest)
    findings += _check_closure(package)
    findings += _check_exec_modes(package, manifest)
    findings += _check_declared_components(package, manifest, inventory)
    return findings


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse the validator CLI arguments."""
    parser = argparse.ArgumentParser(description="Validate a built codemap-py package.")
    parser.add_argument("--package", required=True, type=Path, help="built package directory")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Validate the package directory; print findings and return an exit code."""
    args = _parse_args(argv)
    findings = validate_package(args.package)
    if findings:
        sys.stderr.write("package validation failed:\n" + "\n".join(f"- {item}" for item in findings) + "\n")
        return 1
    print(f"package validation passed: {args.package}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, json.JSONDecodeError, KeyError) as error:
        sys.stderr.write(f"validate-package-error: {error}\n")
        raise SystemExit(2) from error

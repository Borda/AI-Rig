#!/usr/bin/env python3
"""Manage the installed GitHub reader's reusable Codex command approval.

## Purpose

Keep GitHub evidence approval under the plugin's explicit setup and sync lifecycle. Regenerate the literal installed
reader path after upgrades so changing log destinations and job identifiers does not require new command approvals.

## Scope

Manage only ``rules/codex-rig-github-read.rules`` in the selected Codex home and migrate exact canonical legacy reader
allow entries from ``rules/default.rules``. Preserve unrelated default-rule bytes. The approval covers the reader's
existing GitHub reads, local PR checkout, and output writes; it does not grant arbitrary Python or GitHub CLI execution.

## Usage

Run with ``--plugin-root`` pointing to the installed Codex Rig cache and ``--codex-home`` after an explicitly approved
setup. Use ``--remove --codex-home`` for teardown. Direct plugin installation does not execute this helper.

## Outputs

Write one checksum-marked rule file, unique byte-exact backups under ``backups/codex-rig``, and concise per-file status.
Prepare all required backups before changing rules. Repeated setup is idempotent. Each changed file is replaced
atomically; the two-file migration is not transactional, and completed operations are reported before later failures.

## Failure

Reject missing or hash-invalid installed identity, linked paths, unowned or edited rules, and observable changes. Return
a nonzero exit with the failure; never silently replace unrecognized content. Checksums establish integrity of the owned
bytes, not author authentication or protection against a malicious process racing filesystem operations. Package
verification checks setup-time consistency only: persistent approval trusts the installed cache for its lifetime and
cannot stop a same-user process replacing code or rehashing a manifest after verification.

## Used by

The packaged ``sync_codex.py`` install and clear actions, documented explicit setup, and isolated-home regression tests
consume this lifecycle. Existing Codex sessions may need restarting to load the updated user-layer rules.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path, PurePath

from _package_identity import verify_package


CACHE_PARTS = ("plugins", "cache", "borda-ai-rig", "codex-rig")
RULE_NAME = "codex-rig-github-read.rules"
MARKER = b"# codex-rig:github-read sha256="
MAX_BYTES = 4 * 1024 * 1024
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?")
LEGACY_RULE = re.compile(rb'prefix_rule\(pattern=(\["python(?:3)?", "(?:[^"\\]|\\.)*"\]), decision="allow"\)')


class UnsafeRulesState(ValueError):
    """Report state that cannot be updated without risking unrelated permissions."""


def render_rules(reader: PurePath) -> bytes:
    """Render a literal reader prefix with both supported interpreter spellings.

    Preserve native Windows argument spelling as well as its POSIX representation. Neither argument position uses a
    wildcard or interprets shell syntax.
    """
    paths = list(dict.fromkeys((str(reader), reader.as_posix())))
    pattern = [["python", "python3"], paths[0] if len(paths) == 1 else paths]
    body = f'prefix_rule(pattern={json.dumps(pattern)}, decision="allow")\n'.encode("utf-8")
    return MARKER + hashlib.sha256(body).hexdigest().encode("ascii") + b"\n" + body


def strip_legacy_rules(existing: bytes, home: PurePath) -> bytes:
    """Remove only canonical UI-saved two-token allow rules for this home's reader cache."""
    kept = []
    for line in existing.splitlines(keepends=True):
        match = LEGACY_RULE.fullmatch(line.rstrip(b"\r\n"))
        if match is not None:
            try:
                _interpreter, script = json.loads(match.group(1))
                reader = type(home)(script)
            except (ValueError, UnicodeError):
                kept.append(line)
                continue
            if _is_reader_path(reader, home):
                continue
        kept.append(line)
    return b"".join(kept)


def _is_reader_path(reader: PurePath, home: PurePath) -> bool:
    """Recognize a versioned reader coordinate without requiring the old installation to exist."""
    try:
        relative = reader.relative_to(home.joinpath(*CACHE_PARTS))
    except ValueError:
        return False
    return (
        len(relative.parts) == 3
        and VERSION.fullmatch(relative.parts[0]) is not None
        and relative.parts[1:] == ("shared", "github_read.py")
    )


def _require_directory(path: Path) -> None:
    """Reject links or non-directory components before reading or creating an owned directory."""
    for part in (path, *path.parents):
        if part.is_symlink():
            raise UnsafeRulesState(f"linked path; refusing change: {path}")
        if part.exists() and not part.is_dir():
            raise UnsafeRulesState(f"not a directory: {part}")


def _read_regular(path: Path) -> bytes | None:
    """Read bounded ordinary bytes while rejecting linked path components."""
    _require_directory(path.parent)
    if path.is_symlink():
        raise UnsafeRulesState(f"linked path; refusing change: {path}")
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise UnsafeRulesState(f"not a bounded ordinary file: {path}")
    return path.read_bytes()


def _installed_reader(plugin_root: Path, home: Path) -> Path:
    """Resolve a reader only from this home's versioned Codex Rig installation."""
    try:
        relative = plugin_root.relative_to(home.joinpath(*CACHE_PARTS))
    except ValueError as error:
        raise UnsafeRulesState("plugin root is outside the selected Codex home cache") from error
    if len(relative.parts) != 1 or not VERSION.fullmatch(relative.name):
        raise UnsafeRulesState("plugin root is not a versioned Codex Rig cache directory")
    manifest = _read_regular(plugin_root / ".codex-plugin" / "plugin.json")
    if manifest is None:
        raise UnsafeRulesState("installed plugin manifest is missing")
    identity = json.loads(manifest)
    if (
        not isinstance(identity, dict)
        or identity.get("name") != "codex-rig"
        or identity.get("version") != relative.name
    ):
        raise UnsafeRulesState("installed plugin identity does not match its cache directory")
    reader = plugin_root / "shared" / "github_read.py"
    if _read_regular(reader) is None:
        raise UnsafeRulesState("installed GitHub reader is missing")
    verify_package(plugin_root)
    return reader


def _check_owned(existing: bytes | None, home: Path) -> None:
    """Require checksum integrity and one canonical rule for this home's reader cache."""
    if existing is None:
        return
    header, separator, body = existing.partition(b"\n")
    expected = MARKER + hashlib.sha256(body).hexdigest().encode("ascii")
    if not separator or header != expected:
        raise UnsafeRulesState("managed GitHub rules are unowned or modified; refusing change")
    try:
        pattern = json.loads(body.removeprefix(b"prefix_rule(pattern=").removesuffix(b', decision="allow")\n'))
        if not isinstance(pattern, list) or len(pattern) != 2:
            raise ValueError("not a two-position pattern")
        path = pattern[1][0] if isinstance(pattern[1], list) and pattern[1] else pattern[1]
        if not isinstance(path, str):
            raise ValueError("not a path string")
        reader = Path(path)
    except (ValueError, UnicodeError) as error:
        raise UnsafeRulesState("managed GitHub rules have an unrecognized body") from error
    if not _is_reader_path(reader, home) or existing != render_rules(reader):
        raise UnsafeRulesState("managed GitHub rules have an unrecognized body")


def _backup_change(target: Path, payload: bytes | None, existing: bytes | None, home: Path) -> Path | None:
    """Prepare a verified backup for one changed existing file before any permission mutation."""
    if existing is None or payload == existing:
        return None
    if _read_regular(target) != existing:
        raise UnsafeRulesState(f"target changed before backup: {target}")
    backup_root = home / "backups" / "codex-rig"
    _require_directory(backup_root)
    backup_root.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=backup_root, prefix=f"{target.name}.", delete=False) as stream:
        backup = Path(stream.name)
        stream.write(existing)
        stream.flush()
        os.fsync(stream.fileno())
    if backup.read_bytes() != existing:
        raise UnsafeRulesState(f"backup verification failed: {backup}")
    return backup


def _apply_change(target: Path, payload: bytes | None, existing: bytes | None) -> str:
    """Replace one backed-up rules file after checking observable drift."""
    if payload == existing:
        return "absent" if payload is None else "already current"
    _require_directory(target.parent)
    target.parent.mkdir(parents=True, exist_ok=True)
    if _read_regular(target) != existing:
        raise UnsafeRulesState(f"target changed before update: {target}")
    mode = stat.S_IMODE(target.stat().st_mode) if existing is not None else 0o600
    if payload is None:
        if _read_regular(target) != existing:
            raise UnsafeRulesState(f"target changed before removal: {target}")
        target.unlink()
        return "removed"

    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=f".{RULE_NAME}.", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        if _read_regular(target) != existing:
            raise UnsafeRulesState(f"target changed before replacement: {target}")
        os.replace(temporary, target)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return "created" if existing is None else "updated"


def sync_github_read_rules(home: Path, plugin_root: Path | None = None) -> Iterator[tuple[str, Path, Path | None]]:
    """Yield completed approval updates while migrating recognized legacy entries.

    Exhaust the iterator to complete the operation. A missing ``plugin_root`` selects removal. Verify inputs and prepare
    every required backup before modifying rules; report each completed update before another filesystem operation can
    fail.
    """
    home = Path(os.path.abspath(home))
    if home == Path(home.anchor):
        raise UnsafeRulesState("a filesystem root cannot be used as Codex home")
    _require_directory(home)
    desired = None if plugin_root is None else render_rules(_installed_reader(Path(os.path.abspath(plugin_root)), home))
    managed = home / "rules" / RULE_NAME
    default = home / "rules" / "default.rules"
    existing = _read_regular(managed)
    _check_owned(existing, home)
    legacy = _read_regular(default)
    cleaned = None if legacy is None else strip_legacy_rules(legacy, home)

    changes = [(managed, desired, existing)]
    if cleaned != legacy:
        changes.append((default, cleaned, legacy))
    # Stage backups for the entire migration before granting or removing approval.
    # Replacement remains per-file, so yield each effect before attempting the next.
    backups = [_backup_change(target, payload, original, home) for target, payload, original in changes]
    for (target, payload, original), backup in zip(changes, backups):
        action = _apply_change(target, payload, original)
        yield "migrated" if target == default else action, target, backup


def main(argv: list[str] | None = None) -> int:
    """Run the explicit rule lifecycle and report each resulting local file action."""
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--plugin-root", type=Path, help="installed Codex Rig cache root to approve")
    action.add_argument("--remove", action="store_true", help="remove owned and recognized legacy reader approvals")
    parser.add_argument("--codex-home", type=Path, required=True, help="Codex home receiving managed rules")
    args = parser.parse_args(argv)
    completed_changes = 0
    try:
        for status, target, backup in sync_github_read_rules(args.codex_home, args.plugin_root):
            print(f"  [ok] GitHub reader rules {status}: {target}", flush=True)
            if backup is not None:
                print(f"    backup: {backup}", flush=True)
            completed_changes += status not in {"absent", "already current"}
    except (OSError, ValueError) as error:
        print(f"github-read-rules-error: {error}", file=sys.stderr)
        if completed_changes:
            print(
                f"github-read-rules-partial: {completed_changes} completed update(s); no rollback performed",
                file=sys.stderr,
            )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

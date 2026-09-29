#!/usr/bin/env python3
"""Safely install, update, or remove Codex Rig's managed global-instruction block.

## Purpose

Maintain one integrity-marked Codex Rig section inside a user global instruction file while preserving surrounding user
content. The managed block gives session tooling a known instruction entry point without taking ownership of unrelated
user-authored text.

## Scope

Performs explicit local file mutation with backup and atomic-write checks; it does not manage role shims or remote
services. Its safety contract covers only the marked block and target-file replacement, not arbitrary edits to the rest
of the instruction file.

## Usage

Run the documented CLI only for an approved global-instruction lifecycle operation. Select the action through the
workflow for agent shims so diagnosis and approval occur before this script changes the target.

## Used by

The agent-shims workflow, session setup procedures, and global-instruction installer tests call this installer. These
callers depend on its marker and hash format to distinguish managed content from user content during update and removal.

## Outputs

Reports the target, backup when created, and lifecycle status after an atomic local update or removal. A successful
result identifies the resulting managed-block state so session setup can continue with an auditable local outcome.

## Failure

Malformed managed markers, hash mismatch, unsafe target state, or concurrent replacement raises
``UnsafeGlobalAgentsState`` and leaves user content untouched. The caller must surface that failure for review because
continuing could overwrite instructions that are no longer the approved file.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


BEGIN_PREFIX = b"<!-- codex-rig:global-agents begin sha256="
BEGIN_PATTERN = re.compile(rb"<!-- codex-rig:global-agents begin sha256=([0-9a-f]{64}) -->\n")
END_MARKER = b"<!-- codex-rig:global-agents end -->\n"


class UnsafeGlobalAgentsState(ValueError):
    """Report target state that cannot be changed without risking user content."""


def sha256(payload: bytes) -> str:
    """Return the lowercase SHA-256 digest for exact bytes."""
    return hashlib.sha256(payload).hexdigest()


def managed_block(template: bytes) -> bytes:
    """Wrap exact template bytes in authenticated ownership markers."""
    if not template:
        raise UnsafeGlobalAgentsState("global instruction template is empty")
    try:
        template.decode("utf-8")
    except UnicodeDecodeError as error:
        raise UnsafeGlobalAgentsState("global instruction template is not UTF-8") from error
    if BEGIN_PREFIX in template or END_MARKER.rstrip(b"\n") in template:
        raise UnsafeGlobalAgentsState("global instruction template contains ownership markers")
    body = template if template.endswith(b"\n") else template + b"\n"
    begin = BEGIN_PREFIX + sha256(body).encode("ascii") + b" -->\n"
    return begin + body + END_MARKER


def merged_payload(existing: bytes, block: bytes) -> tuple[bytes, str]:
    """Merge one trusted managed block while preserving all external bytes."""
    try:
        existing.decode("utf-8")
    except UnicodeDecodeError as error:
        raise UnsafeGlobalAgentsState("existing AGENTS.md is not UTF-8; refusing write") from error

    begin_count = existing.count(BEGIN_PREFIX)
    end_count = existing.count(END_MARKER.rstrip(b"\n"))
    if begin_count == 0 and end_count == 0:
        separator = b"" if not existing else (b"\n" if existing.endswith(b"\n") else b"\n\n")
        return existing + separator + block, "merged"
    if begin_count != 1 or end_count != 1:
        raise UnsafeGlobalAgentsState("managed markers are malformed or duplicated; refusing write")

    begin_match = BEGIN_PATTERN.search(existing)
    if begin_match is None:
        raise UnsafeGlobalAgentsState("managed begin marker is malformed; refusing write")
    end_index = existing.find(END_MARKER, begin_match.end())
    if end_index < 0:
        raise UnsafeGlobalAgentsState("managed end marker is malformed; refusing write")
    body = existing[begin_match.end() : end_index]
    if sha256(body) != begin_match.group(1).decode("ascii"):
        raise UnsafeGlobalAgentsState("managed block was modified; refusing write")

    block_end = end_index + len(END_MARKER)
    updated = existing[: begin_match.start()] + block + existing[block_end:]
    return updated, "already current" if updated == existing else "updated"


def stripped_payload(existing: bytes) -> tuple[bytes, str]:
    """Remove one authenticated managed block, preserving every external byte."""
    try:
        existing.decode("utf-8")
    except UnicodeDecodeError as error:
        raise UnsafeGlobalAgentsState("existing AGENTS.md is not UTF-8; refusing write") from error

    begin_count = existing.count(BEGIN_PREFIX)
    end_count = existing.count(END_MARKER.rstrip(b"\n"))
    if begin_count == 0 and end_count == 0:
        return existing, "absent"
    if begin_count != 1 or end_count != 1:
        raise UnsafeGlobalAgentsState("managed markers are malformed or duplicated; refusing write")

    begin_match = BEGIN_PATTERN.search(existing)
    if begin_match is None:
        raise UnsafeGlobalAgentsState("managed begin marker is malformed; refusing write")
    end_index = existing.find(END_MARKER, begin_match.end())
    if end_index < 0:
        raise UnsafeGlobalAgentsState("managed end marker is malformed; refusing write")
    body = existing[begin_match.end() : end_index]
    if sha256(body) != begin_match.group(1).decode("ascii"):
        raise UnsafeGlobalAgentsState("managed block was modified; refusing write")

    block_end = end_index + len(END_MARKER)
    updated = existing[: begin_match.start()] + existing[block_end:]
    # collapse the single separator install prepended so removal leaves no doubled blank line
    if updated.endswith(b"\n\n") and existing[: begin_match.start()].endswith(b"\n\n"):
        updated = updated[:-1]
    return updated, "removed"


def backup_target(target: Path, codex_home: Path, payload: bytes) -> Path:
    """Create and verify a unique backup before changing an existing target."""
    backup_root = codex_home / "backups" / "codex-rig"
    backup_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = backup_root / f"{timestamp}-{sha256(payload)[:12]}-AGENTS.md"
    shutil.copy2(target, backup, follow_symlinks=False)
    if backup.read_bytes() != payload:
        raise OSError(f"backup verification failed: {backup}")
    return backup


def atomic_write(target: Path, payload: bytes, mode: int, expected: bytes | None) -> None:
    """Replace target atomically after rejecting observable concurrent drift."""
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".AGENTS.md.codex-rig-", delete=False) as stream:
            temporary_path = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_path, mode)
        if target.is_symlink():
            raise UnsafeGlobalAgentsState(f"target became a symlink; refusing write: {target}")
        if expected is None:
            if target.exists():
                raise UnsafeGlobalAgentsState(f"target appeared during installation; refusing write: {target}")
        elif not target.is_file() or target.read_bytes() != expected:
            raise UnsafeGlobalAgentsState(f"target changed during installation; refusing write: {target}")
        os.replace(temporary_path, target)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def install_global_agents(source: Path, codex_home: Path) -> tuple[str, Path, Path | None]:
    """Install or update the template without overwriting user-owned instructions."""
    if source.is_symlink() or not source.is_file():
        raise UnsafeGlobalAgentsState(f"template must be an ordinary file: {source}")
    template = source.read_bytes()
    block = managed_block(template)
    codex_home.mkdir(parents=True, exist_ok=True)
    target = codex_home / "AGENTS.md"
    if target.is_symlink():
        raise UnsafeGlobalAgentsState(f"target is a symlink; refusing write: {target}")
    if target.exists() and not target.is_file():
        raise UnsafeGlobalAgentsState(f"target is not an ordinary file; refusing write: {target}")

    if not target.exists():
        atomic_write(target, block, 0o600, None)
        return "created", target, None

    existing = target.read_bytes()
    template_body = template if template.endswith(b"\n") else template + b"\n"
    if existing in {template, template_body}:
        desired, action = block, "adopted"
    else:
        desired, action = merged_payload(existing, block)
    unmanaged, _ = stripped_payload(desired)
    if b"# Global Agent Instructions" in unmanaged and b"# Global Agent Instructions" in template:
        print(
            "global-agents-overlap: unmanaged global policy overlaps the managed template. "
            "Review and migrate the unmanaged policy explicitly; user bytes were preserved.",
            file=sys.stderr,
        )
    if action == "already current":
        return action, target, None
    mode = stat.S_IMODE(target.stat().st_mode)
    backup = backup_target(target, codex_home, existing)
    atomic_write(target, desired, mode, existing)
    return action, target, backup


def remove_global_agents(codex_home: Path) -> tuple[str, Path, Path | None]:
    """Strip Codex Rig's managed block from AGENTS.md without touching user content."""
    target = codex_home / "AGENTS.md"
    if not target.exists():
        return "absent", target, None
    if target.is_symlink():
        raise UnsafeGlobalAgentsState(f"target is a symlink; refusing write: {target}")
    if not target.is_file():
        raise UnsafeGlobalAgentsState(f"target is not an ordinary file; refusing write: {target}")

    existing = target.read_bytes()
    updated, action = stripped_payload(existing)
    if action == "absent":
        return "absent", target, None

    backup = backup_target(target, codex_home, existing)
    if updated.strip() == b"":
        target.unlink()  # file held only our block — remove it entirely
        return "removed-file", target, backup
    mode = stat.S_IMODE(target.stat().st_mode)
    atomic_write(target, updated, mode, existing)
    return "removed-block", target, backup


def parse_args() -> argparse.Namespace:
    """Parse explicit source and Codex-home paths."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="packaged assets/AGENTS.md template (required unless --remove)")
    parser.add_argument("--codex-home", type=Path, required=True, help="target Codex home")
    parser.add_argument("--remove", action="store_true", help="strip the managed block instead of installing it")
    args = parser.parse_args()
    if not args.remove and args.source is None:
        parser.error("--source is required unless --remove is given")
    return args


def main() -> int:
    """Run one fail-closed global-instruction installation or removal."""
    args = parse_args()
    try:
        if args.remove:
            action, target, backup = remove_global_agents(args.codex_home)
        else:
            action, target, backup = install_global_agents(args.source, args.codex_home)
    except UnsafeGlobalAgentsState as error:
        print(f"global-agents-error: {error}", file=sys.stderr)
        return 4
    except OSError as error:
        print(f"global-agents-error: {error}", file=sys.stderr)
        return 2
    print(f"  [ok] global instructions {action}: {target}")
    if backup is not None:
        print(f"    backup: {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

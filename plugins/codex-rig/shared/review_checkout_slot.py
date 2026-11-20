#!/usr/bin/env python3
"""Serialize cooperating code reviews that share Git checkout metadata.

## Purpose

Give each review exclusive admission before creating its worktree or other run artifacts. Linked worktrees share an
owner record in their Git common directory, while separate clones admit independent reviews.

## Scope

An atomic rename publishes a complete owner marker across processes. An acquired slot survives interruption. The helper
does not reclaim based on age, clean up review artifacts, or grant execution authority.

## Usage

Use ``acquire --checkout PATH --owner RUN_ID [--wait-seconds N]`` at entry and keep the absolute slot locator and unique
owner ID for resume. A repeated acquire by the same run succeeds. Inspect with ``status --slot PATH`` or ``status
--checkout PATH``. Finish with ``release --slot PATH --owner RUN_ID`` even if running Git has stopped. After an
integrity stop use ``hold --slot PATH --owner RUN_ID`` instead. A waiting peer stays queued through inspection
and owner changes. An expired wait reports the current state. Only the owner may clear a held claim after inspection
with ``release --inspected``. After human inspection of a run that predates slot acquisition, use
``acquire --slot PATH --owner NEW_ID`` to claim a free slot without Git.
Derive PATH from the original authenticated Git common directory and SLOT_NAME. This form never adopts an existing
marker, even one bearing NEW_ID.

## Outputs

Each operation prints a JSON object with status, absolute slot path and current owner when known. An ordinary release
removes the matching marker and the empty slot directory. Empty or malformed slots never authorize entry.

## Failure

Exit 1 means another owner holds the slot or a release did not match. Exit 2 means invalid input or unreadable
coordination state. Neither permits entry. After interruption only the owning run can release through the helper.

## Used by

The Codex Rig code-review entry and closing steps use this cooperative checkout scheduler.
"""

from __future__ import annotations

import argparse
import base64
import errno
import json
import os
import re
import stat
import subprocess
import tempfile
import time
from pathlib import Path

SLOT_NAME = "codex-review-slot"
OWNER_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}")


def _checkout_slot(checkout: Path) -> Path:
    """Resolve the shared Git directory without inheriting repository-selection overrides."""
    result = subprocess.run(  # noqa: S603 - fixed Git operation, no shell
        ["git", "-C", str(checkout), "rev-parse", "--path-format=absolute", "--git-common-dir"],  # noqa: S607
        env={key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")},
        capture_output=True,
        check=True,
        timeout=10,
    )
    common = Path(os.fsdecode(result.stdout.rstrip(b"\r\n")))
    if not common.is_absolute() or not common.is_dir():
        raise ValueError("invalid Git common directory")
    return common.resolve(strict=True) / SLOT_NAME


def _validate_locator(slot: Path) -> None:
    """Validate a retained Git common-directory locator without running Git or reading Git configuration."""
    if not slot.is_absolute() or slot.name != SLOT_NAME or ".." in slot.parts:
        raise ValueError("invalid review slot locator")
    # Check each component before inspecting its descendants. Windows junctions are reparse points too.
    for path in (*reversed(slot.parents), slot):
        try:
            _mode_without_links(path)
        except FileNotFoundError:
            if path == slot:
                break
            raise
    for name in ("HEAD", "objects", "refs"):
        mode = _mode_without_links(slot.parent / name)
        if not (stat.S_ISREG(mode) if name == "HEAD" else stat.S_ISDIR(mode)):
            raise ValueError("slot parent is not a Git common directory")


def _mode_without_links(path: Path) -> int:
    """Inspect a path without following symbolic links or Windows junctions."""
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError("redirected review slot path")
    return info.st_mode


def _observe(slot: Path) -> tuple[str, str | None]:
    """Read only the marker name and distinguish an empty, interrupted release from an unclaimed slot."""
    _validate_locator(slot)
    try:
        entries = list(slot.iterdir())
    except FileNotFoundError:
        return "free", None
    if not entries:
        return "busy", None
    if len(entries) != 1:
        raise ValueError("invalid review slot marker")
    try:
        mode = _mode_without_links(entries[0])
    except FileNotFoundError:
        # A release between listing and inspection is still in progress.
        return "busy", None
    if not stat.S_ISDIR(mode):
        raise ValueError("invalid review slot marker")
    # Base32 makes case-sensitive owner IDs distinct even on case-insensitive filesystems.
    encoded = entries[0].name.removeprefix("inspection-")
    owner = base64.b32decode(encoded).decode("ascii")
    if not OWNER_PATTERN.fullmatch(owner) or _marker_name(owner) != encoded:
        raise ValueError("invalid review slot owner")
    return ("busy" if encoded == entries[0].name else "inspection-required"), owner


def _marker_name(owner: str) -> str:
    """Encode the run identity as one portable directory name."""
    return base64.b32encode(owner.encode("ascii")).decode("ascii")


def _operate(
    action: str, slot: Path, owner: str | None, inspected: bool, *, reuse_owner: bool = True
) -> tuple[str, str | None]:
    """Observe, atomically publish, or remove only the marker belonging to the caller."""
    state, current = _observe(slot)
    if action == "status":
        return state, current
    if owner is None:
        raise ValueError("missing review owner")
    marker = _marker_name(owner)
    if action in {"hold", "release"}:
        if current != owner:
            return "not-owner", current
        held_marker = "inspection-" + marker
        if action == "hold":
            if state != "inspection-required":
                (slot / marker).rename(slot / held_marker)
            return "inspection-required", current
        if state == "inspection-required" and not inspected:
            return state, current
        if inspected and state != "inspection-required":
            raise ValueError("inspected release requires an inspection hold")
        # Only the process removing this specific marker may continue to remove the empty directory.
        (slot / (held_marker if inspected else marker)).rmdir()
        try:
            slot.rmdir()
        except OSError as error:
            if error.errno not in (errno.EEXIST, errno.ENOTEMPTY) or _observe(slot)[1] is None:
                raise
        return "released", None
    if state == "free":
        candidate = Path(tempfile.mkdtemp(prefix="codex-review-claim-", dir=slot.parent))
        (candidate / marker).mkdir()
        try:
            # An occupied slot is a nonempty directory. No supported OS replaces it with a rename.
            candidate.rename(slot)
            return "acquired", owner
        except OSError as error:
            (candidate / marker).rmdir()
            candidate.rmdir()
            if error.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                raise
            state, current = _observe(slot)
    if state == "inspection-required":
        return state, current
    return ("acquired" if reuse_owner and current == owner else "busy"), current


def main() -> int:
    """Parse one scheduling request and emit its decision after publishing or releasing the claim."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="action", required=True)
    acquire = commands.add_parser("acquire")
    location = acquire.add_mutually_exclusive_group(required=True)
    location.add_argument("--checkout", type=Path)
    location.add_argument(
        "--slot", type=Path, help="Claim a free slot at an authenticated locator without running Git."
    )
    acquire.add_argument("--wait-seconds", type=int, default=0, metavar="0..60", help="Wait for the current holder.")
    release = commands.add_parser("release")
    release.add_argument("--inspected", action="store_true", help="Clear a hold after human-confirmed inspection.")
    hold = commands.add_parser("hold")
    for command in (hold, release):
        command.add_argument("--slot", type=Path, required=True)
    for command in (acquire, release, hold):
        command.add_argument("--owner", required=True, help="Unique review run ID, retained across resume.")
    status = commands.add_parser("status").add_mutually_exclusive_group(required=True)
    status.add_argument("--checkout", type=Path)
    status.add_argument("--slot", type=Path)
    args = parser.parse_args()
    owner = getattr(args, "owner", None)
    wait = getattr(args, "wait_seconds", 0)
    slot = getattr(args, "slot", None)
    try:
        if owner is not None and not OWNER_PATTERN.fullmatch(owner):
            raise ValueError("invalid owner ID")
        if not 0 <= wait <= 60:
            raise ValueError("wait must be between 0 and 60 seconds")
        if slot is None:
            slot = _checkout_slot(args.checkout)
        deadline = time.monotonic() + wait
        while True:
            state, current = _operate(
                args.action,
                slot,
                owner,
                getattr(args, "inspected", False),
                reuse_owner=getattr(args, "slot", None) is None,
            )
            remaining = deadline - time.monotonic()
            # The owning review handles inspection. A peer observes the claim until it is released or its wait expires.
            waiting_for_owner = state == "busy" or (state == "inspection-required" and current != owner)
            if args.action != "acquire" or not waiting_for_owner or remaining <= 0:
                break
            time.sleep(min(1, remaining))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(json.dumps({"status": "error", "owner": None, "slot": str(slot) if slot else None, "error": str(error)}))
        return 2
    print(json.dumps({"status": state, "owner": current, "slot": str(slot)}))
    if args.action in {"acquire", "release"}:
        return int(state not in {"acquired", "released"})
    return int(state == "not-owner")


if __name__ == "__main__":
    raise SystemExit(main())

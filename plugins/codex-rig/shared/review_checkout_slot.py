#!/usr/bin/env python3
"""Serialize cooperating code reviews that share Git checkout metadata.

## Purpose

Schedule ordinary PR work per target, serialize writers within one checkout, and reserve the shared Git directory for
protected operations. Different PRs can use linked worktrees concurrently.

## Scope

An atomic rename publishes a complete owner marker across processes. An acquired slot survives interruption. The helper
does not reclaim based on age, clean up review artifacts, or grant execution authority.

## Usage

Use ``acquire --checkout PATH --target PR_URL --owner RUN_ID --queue`` for ordinary PR work. Omit the target for
protected shared-state operations. In-place remediation also takes a checkout-local ``--writer`` claim. Retain the
absolute locator and owner ID for resume. A repeated checkout-bound acquire by the same run succeeds. Inspect with
``status --slot PATH`` or ``status --checkout PATH``. Finish with ``release --slot PATH --owner RUN_ID`` even if running
Git has stopped. After an integrity stop use ``hold --slot PATH --owner RUN_ID`` instead. A waiting peer stays queued
through inspection and owner changes until admitted or interrupted. Peer progress carries queue=waiting on stderr when
the blocking claim changes. The optional bounded ``--wait-seconds N`` form reports the current state when its interval
expires. Only the owner may clear a held claim after inspection with ``release --inspected``. The integrity helper uses
``recovery_claim`` to seal a proven restart and reactivate that owner's exclusive claim in place. After human inspection
of a run that predates slot acquisition, use ``acquire --slot PATH --owner NEW_ID`` to claim a free slot without Git.
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
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# Helpers also run directly from an installed plugin. Keep its payload read-only.
sys.dont_write_bytecode = True
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from create_run import PR_URL_PATTERN  # noqa: E402

SLOT_NAME = "codex-review-slot"
TARGETS = "codex-review-targets"
WRITERS = "codex-review-writers"
OWNER_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}")


def _checkout_slot(checkout: Path, target: str | None = None, *, writer: bool = False, local: bool = False) -> Path:
    """Resolve the shared Git directory without inheriting repository-selection overrides."""
    result = subprocess.run(  # noqa: S603 - fixed Git operation, no shell
        ["git", "-C", str(checkout), "rev-parse", "--path-format=absolute", "--git-common-dir", "--absolute-git-dir"],  # noqa: S607
        env={key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")},
        capture_output=True,
        check=True,
        timeout=10,
    )
    common, git_dir = (Path(os.fsdecode(line)) for line in result.stdout.splitlines())
    if not common.is_absolute() or not common.is_dir():
        raise ValueError("invalid Git common directory")
    common = common.resolve(strict=True)
    if writer or local:
        key = os.fsencode(os.path.normcase(str(git_dir.resolve(strict=True))))
        namespace = WRITERS if writer else TARGETS
        digest = hashlib.sha256(b"local:" + key).hexdigest()
        return common / namespace / digest / SLOT_NAME
    return _target_slot(common, target) if target else common / SLOT_NAME


def _target_slot(common: Path, target: str) -> Path:
    """Bind every spelling of a canonical PR URL to the same ordinary claim."""
    if not PR_URL_PATTERN.fullmatch(target):
        raise ValueError("expected a canonical GitHub PR URL")
    return common / TARGETS / hashlib.sha256(target.lower().encode("ascii")).hexdigest() / SLOT_NAME


def _common(slot: Path) -> Path:
    """Find the common directory for either a retained scoped or repository-wide locator."""
    if slot.parent.parent.name in {TARGETS, WRITERS}:
        if not re.fullmatch(r"[0-9a-f]{64}", slot.parent.name):
            raise ValueError("invalid scoped slot locator")
        return slot.parents[2]
    return slot.parent


def _validate_locator(slot: Path) -> None:
    """Validate a retained Git common-directory locator without running Git or reading Git configuration."""
    if not slot.is_absolute() or slot.name != SLOT_NAME or ".." in slot.parts:
        raise ValueError("invalid review slot locator")
    # Check each component before inspecting its descendants. Windows junctions are reparse points too.
    for path in (*reversed(slot.parents), slot):
        try:
            _mode_without_links(path)
        except FileNotFoundError:
            if _common(slot) in path.parents:
                break
            raise
    for name in ("HEAD", "objects", "refs"):
        mode = _mode_without_links(_common(slot) / name)
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


@contextmanager
def _admission_lock(common: Path) -> Iterator[None]:
    """Make checking competing scopes and publishing their marker one cross-process operation."""
    path = common / "codex-review-admission.lock"
    try:
        if not stat.S_ISREG(_mode_without_links(path)):
            raise ValueError("invalid admission lock")
    except FileNotFoundError:
        pass
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("invalid admission lock")
        if os.name == "nt":
            import msvcrt

            # The CRT stops waiting after ten seconds. A protected recovery can still be measuring the checkout.
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                    break
                except OSError as error:
                    if error.errno != errno.EDEADLK:
                        raise
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        # Closing the descriptor releases the kernel lock, including on process termination.
        os.close(fd)


@contextmanager
def recovery_claim(common: Path, slot: Path, owner: str) -> Iterator[None]:
    """Seal recovery evidence under an existing exclusive claim and reactivate its owner only on success."""
    if slot != common / SLOT_NAME or _common(slot) != common or not OWNER_PATTERN.fullmatch(owner):
        raise ValueError("recovery requires the original repository-wide slot and owner")
    _validate_locator(slot)
    with _admission_lock(common):
        state, holder = _observe(slot)
        if holder != owner or state not in {"busy", "inspection-required"}:
            raise ValueError("recovery requires the original owner's active or held exclusive claim")
        yield
        if state == "inspection-required":
            # Renaming only the marker keeps the slot occupied through the transition.
            marker = _marker_name(owner)
            (slot / ("inspection-" + marker)).rename(slot / marker)


def _schedule(
    action: str, slot: Path, owner: str | None, inspected: bool, *, reuse_owner: bool
) -> tuple[str, str | None]:
    """Admit independent targets together and exclude active targets during a protected interval."""
    _validate_locator(slot)
    if action == "status":
        return _observe(slot)
    common = _common(slot)
    with _admission_lock(common):
        if action == "acquire":
            if common == slot.parent:
                targets = common / TARGETS
                try:
                    if not stat.S_ISDIR(_mode_without_links(targets)):
                        raise ValueError("invalid review target directory")
                    peers = sorted(targets.iterdir())
                except FileNotFoundError:
                    peers = []
                for peer in peers:
                    state, holder = _observe(peer / SLOT_NAME)
                    if state != "free":
                        return state, holder
            else:
                if slot.parent.parent.name == TARGETS:
                    state, holder = _observe(common / SLOT_NAME)
                    if state != "free":
                        return state, holder
                # Local review and remediation must not read and switch the same checkout concurrently.
                other_scope = WRITERS if slot.parent.parent.name == TARGETS else TARGETS
                state, holder = _observe(common / other_scope / slot.parent.name / SLOT_NAME)
                if state != "free":
                    return state, holder
                slot.parent.mkdir(parents=True, exist_ok=True)
        result = _operate(action, slot, owner, inspected, reuse_owner=reuse_owner)
        if result[0] == "released" and common != slot.parent:
            slot.parent.rmdir()
        return result


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
    waiting = acquire.add_mutually_exclusive_group()
    waiting.add_argument("--wait-seconds", type=int, default=0, metavar="0..60", help="Wait for the current holder.")
    waiting.add_argument("--queue", action="store_true", help="Wait for admission until interrupted.")
    release = commands.add_parser("release")
    release.add_argument("--inspected", action="store_true", help="Clear a hold after human-confirmed inspection.")
    hold = commands.add_parser("hold")
    for command in (hold, release):
        command.add_argument("--slot", type=Path, required=True)
    for command in (acquire, release, hold):
        command.add_argument("--owner", required=True, help="Unique review run ID, retained across resume.")
    status_parser = commands.add_parser("status")
    for command in (acquire, status_parser):
        scope = command.add_mutually_exclusive_group()
        scope.add_argument("--target", help="Canonical URL for an ordinary PR claim.")
        scope.add_argument("--local", action="store_true", help="Claim ordinary local review of this checkout.")
        scope.add_argument("--writer", action="store_true", help="Serialize in-place writers to this checkout.")
    status = status_parser.add_mutually_exclusive_group(required=True)
    status.add_argument("--checkout", type=Path)
    status.add_argument("--slot", type=Path)
    args = parser.parse_args()
    owner = getattr(args, "owner", None)
    wait = getattr(args, "wait_seconds", 0)
    queue = getattr(args, "queue", False)
    slot = getattr(args, "slot", None)
    try:
        if owner is not None and not OWNER_PATTERN.fullmatch(owner):
            raise ValueError("invalid owner ID")
        if not 0 <= wait <= 60:
            raise ValueError("wait must be between 0 and 60 seconds")
        if slot is None:
            slot = _checkout_slot(args.checkout, args.target, writer=args.writer, local=args.local)
        elif any(getattr(args, flag, None) for flag in ("target", "writer", "local")):
            raise ValueError("scope is already bound to the retained locator")
        deadline = time.monotonic() + wait
        previous = None
        while True:
            state, current = _schedule(
                args.action,
                slot,
                owner,
                getattr(args, "inspected", False),
                reuse_owner=getattr(args, "slot", None) is None,
            )
            remaining = deadline - time.monotonic()
            # The owning review handles inspection. A peer observes the claim until it is released or its wait expires.
            waiting_for_owner = state == "busy" or (state == "inspection-required" and current != owner)
            if args.action != "acquire" or not waiting_for_owner:
                break
            if queue:
                observation = state, current
                # Give an in-progress release one interval to remove its empty directory.
                # An existing same-owner marker at a free-only locator requires explicit recovery.
                if current == owner or (current is None and observation == previous):
                    break
                if current is not None and observation != previous:
                    print(
                        json.dumps({"status": state, "owner": current, "slot": str(slot), "queue": "waiting"}),
                        file=sys.stderr,
                        flush=True,
                    )
                previous = observation
                time.sleep(1)
            elif remaining > 0:
                time.sleep(min(1, remaining))
            else:
                break
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(json.dumps({"status": "error", "owner": None, "slot": str(slot) if slot else None, "error": str(error)}))
        return 2
    print(json.dumps({"status": state, "owner": current, "slot": str(slot)}))
    if args.action in {"acquire", "release"}:
        return int(state not in {"acquired", "released"})
    return int(state == "not-owner")


if __name__ == "__main__":
    raise SystemExit(main())

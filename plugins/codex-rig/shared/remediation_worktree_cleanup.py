"""Verify and retire the specific linked checkout allocated for a published PR remediation.

## Purpose

Release a run-created PR checkout after its combined publication and local cleanup decision completes.

## Scope

Creation binds the locator and Git administration directory to the allocating run. Cleanup checks the promoted
handoff, exact offer and native decision, successful transport, audited readback, live branch, worktree contents
and checkout claim. The original branch and commit stay registered in the shared repository.

## Usage

Before offering cleanup, use --check-owner --run RUN. After promotion, invoke --run CHECKPOINT --slot SLOT
--owner OWNER from the original invoking checkout. CHECKPOINT may be RUN or its publication-request directory.

## Outputs

The command prints its JSON disposition. The publication checkpoint retains remediation-worktree-cleanup.json.
A create-only witness in the common Git directory authenticates the pre-removal receipt after Git removes its marker.

## Failure

Missing, stale or inconsistent evidence exits one and preserves the checkout. An interrupted journal can be
rechecked under the same publication authority. Report replay verifies the recorded removal without changing Git.

## Used by

Code Remediate's terminal publication checkpoint and the historical reported-delivery artifact validator.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

sys.pycache_prefix = str(Path(__file__).resolve())

import remediation_finalize as finalizer  # noqa: E402
import review_checkout_slot as slots  # noqa: E402
import worktree_publication as publication  # noqa: E402
from review_worktree_cleanup import _git, _require_clean, filesystem_content_digest  # noqa: E402

OWNER = "remediation-worktree-owner.json"
MARKER = "codex-remediation-owner"
JOURNAL = "remediation-worktree-cleanup.json"


@dataclass(frozen=True)
class Allocation:
    """Validated local coordinates and creation evidence kept across promotion and removal."""

    checkout: Path
    caller: Path
    common: Path
    admin: Path
    token: str
    owner_digest: str


def _allocation(original: Path) -> Allocation:
    """Require the creation sidecar and locator of this exact run directory, including after canonical renaming."""
    if original != original.resolve(strict=True) or os.path.lexists(original / "recovery-worktree.json"):
        raise ValueError("redirected or recovery worktree run")
    locator, locator_digest = finalizer._publication_record(original, "remediation-worktree.json")
    created, owner_digest = finalizer._publication_record(original, OWNER)
    checkout, caller, common, admin = (
        Path(p)
        for p in (locator["worktree"], locator["invoking_checkout"], locator["git_common_dir"], created["git_dir"])
    )
    identity = original.stat()
    if (
        type(created.get("schema_version")) is not int
        or created["schema_version"] != 1
        or locator.get("schema_version") != 1
        or created["locator_sha256"] != locator_digest
        or not identity.st_ino
        or created.get("run_directory_id") != [identity.st_dev, identity.st_ino]
        or not re.fullmatch("[0-9a-f]{32}", created["token"])
        or any(path != path.resolve() for path in (checkout, caller, common, admin))
        or admin.parent != common / "worktrees"
        or not checkout.parent.name.startswith("codex-remediate-")
        or checkout.name != "worktree"
        or any(path.is_relative_to(checkout) for path in (caller, common, original))
        or checkout.is_relative_to(common)
    ):
        raise ValueError("ownership or worktree coordinates changed")
    return Allocation(checkout, caller, common, admin, created["token"], owner_digest)


def _verify_registration(allocation: Allocation) -> None:
    """Authenticate both halves of the linked registration and the marker created by this run."""
    checkout, admin = allocation.checkout, allocation.admin
    root, actual_admin, common = _git(
        checkout, "rev-parse", "--path-format=absolute", "--show-toplevel", "--absolute-git-dir", "--git-common-dir"
    ).splitlines()
    pointer = checkout / ".git"
    if (
        (Path(os.fsdecode(root)), Path(os.fsdecode(actual_admin)), Path(os.fsdecode(common)))
        != (checkout, admin, allocation.common)
        or pointer.is_symlink()
        or not pointer.is_file()
        or (admin / "gitdir").is_symlink()
        or not (admin / "gitdir").is_file()
        or (admin / os.fsdecode((admin / "gitdir").read_bytes().rstrip(b"\r\n"))).resolve(strict=True) != pointer
        or hashlib.sha256(publication._read_record(admin, MARKER)).hexdigest() != allocation.owner_digest
    ):
        raise ValueError("original worktree registration or ownership marker changed")


def _evidence(checkpoint: Path, original: Path, allocation: Allocation) -> tuple[dict, dict]:
    """Require the exact combined decision and authoritative terminal push proof for the promoted delivery."""
    if checkpoint != checkpoint.resolve(strict=True) or os.path.lexists(original / "result.candidate.json"):
        raise ValueError("cleanup requires an unredirected promoted run")
    _, digest = finalizer._publication_record(checkpoint, publication.OFFER_FILE)
    offer = publication.read_offer(checkpoint, digest)
    approved = {
        "offer_digest": digest,
        "decision_id": publication.publication_decision_id(checkpoint, digest),
        "answer": "Approve",
    }
    answer, _ = finalizer._publication_record(checkpoint, "publication-answer.json")
    if offer.get("after_push") != "remove-owned-worktree" or answer != approved:
        raise ValueError("combined publication and removal approval missing")
    delivery = finalizer._publication_delivery(original)
    if checkpoint == original:
        result, handoff = publication._committed_result(original, "result.json")
        publication._offered_destination(original, offer, handoff)
    else:
        publication.requested_delivery(checkpoint, offer)
    binding = finalizer._publication_state(checkpoint, delivery)
    if (
        binding["status"] != "verified"
        or binding["evidence"] != "publication.json"
        or binding["observed_oid"] != delivery["head"]
        or (checkpoint == original and result["metadata"].get("worktree_publication") != binding)
    ):
        raise ValueError("verified publication of this promoted delivery required")
    journal = {
        "schema_version": 1,
        "run_dir": original.as_posix(),
        "checkpoint": checkpoint.as_posix(),
        "worktree": allocation.checkout.as_posix(),
        "git_dir": allocation.admin.as_posix(),
        "head": delivery["head"],
        "branch": delivery["branch"],
        "owner_sha256": allocation.owner_digest,
        "offer_digest": digest,
        "result_sha256": hashlib.sha256(
            publication._read_record(original, "result.json", limit=publication.REPORT_LIMIT)
        ).hexdigest(),
        "publication_sha256": binding["evidence_sha256"],
        "readback_sha256": binding["readback_sha256"],
    }
    return delivery, journal


def _write_journal(path: Path, payload: dict, *, complete: bool = False) -> None:
    """Atomically seal a new proof, reuse its exact bytes, or finish an already verified removal journal."""
    raw = (json.dumps(payload, sort_keys=True) + "\n").encode()
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        pending = Path(stream.name)
        try:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        except OSError:
            stream.close()
            pending.unlink(missing_ok=True)
            raise
    try:
        if complete:
            pending.replace(path)
        else:
            try:
                os.link(pending, path)
            except FileExistsError:
                if publication._read_record(path.parent, path.name) != raw:
                    raise ValueError("existing cleanup record differs") from None
    finally:
        pending.unlink(missing_ok=True)
    if os.name == "posix":
        # Journal and checkout may be on different filesystems. Persist the linked proof before removal starts.
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _verify_absence(checkpoint: Path, allocation: Allocation, journal: dict) -> bool:
    """Accept only this removed registration; Git can recycle its basename for future linked worktrees."""
    record, _ = finalizer._publication_record(checkpoint, JOURNAL)
    witness, _ = finalizer._publication_record(allocation.common, f"codex-remediation-cleanup-{allocation.token}.json")
    complete = record == journal | {"status": "removed"}
    if (
        witness != journal
        or (not complete and record != journal | {"status": "removal-authorized"})
        or os.path.lexists(allocation.checkout)
    ):
        raise ValueError("cleanup journal, witness or worktree absence unproven")
    if os.path.lexists(allocation.admin):
        if not complete:
            raise ValueError("original worktree registration is still present")
        # The recorded removal already finished. Accept a later independent registration only when its two
        # pointers identify one another at a different live checkout. Never modify that worktree or its marker.
        pointer = (
            allocation.admin / os.fsdecode(publication._read_record(allocation.admin, "gitdir").rstrip(b"\r\n"))
        ).resolve(strict=True)
        prefix, separator, admin = os.fsdecode(
            publication._read_record(pointer.parent, ".git").rstrip(b"\r\n")
        ).partition("gitdir: ")
        if (
            pointer.name != ".git"
            or pointer.parent == allocation.checkout
            or prefix
            or not separator
            or (pointer.parent / admin).resolve(strict=True) != allocation.admin
        ):
            raise ValueError("original worktree registration absence unproven")
    return complete


def verified_removed_delivery(run: Path) -> dict[str, str]:
    """Replay the archived checkout coordinates after a verified removal, independent of live branch movement."""
    try:
        checkpoints = [p for p in (run, run / publication.REQUEST_DIRECTORY) if os.path.lexists(p / JOURNAL)]
        if len(checkpoints) != 1:
            raise ValueError("no unique removal checkpoint")
        checkpoint = checkpoints[0]
        allocation = _allocation(run)
        delivery, journal = _evidence(checkpoint, run, allocation)
        _verify_absence(checkpoint, allocation, journal)
        return {key: delivery[key] for key in ("checkout", "common_dir", "head", "branch")}
    except (OSError, KeyError, TypeError, RuntimeError, finalizer.DeriveError) as error:
        raise ValueError(f"removed delivery unverified: {error}") from error


def _preserve_private_commits(allocation: Allocation) -> None:
    """Keep worktree-only commits that would lose their last ref or reflog entry on removal."""
    tip_ids = set()
    for name in ("logs/HEAD", "ORIG_HEAD", "FETCH_HEAD"):
        if not (allocation.admin / name).exists():
            continue
        contents = publication._read_record(allocation.admin, name, limit=publication.REPORT_LIMIT)
        for line in contents.splitlines():
            fields = line.split(maxsplit=2)[: 2 if name == "logs/HEAD" else 1]
            for field in fields:
                if not re.fullmatch(rb"[a-f0-9]{40}|[a-f0-9]{64}", field):
                    raise ValueError("worktree has unrecognized private ref evidence")
                if field.strip(b"0"):
                    tip_ids.add(field.decode("ascii"))
    if tip_ids and _git(allocation.checkout, "rev-list", "--max-count=1", *sorted(tip_ids), "--not", "--all", "--"):
        raise ValueError("worktree has commits reachable only from private administrative data")


def _clean_registration(allocation: Allocation, delivery: dict, run: Path) -> None:
    """Preserve unexpected administrative data, in-progress operations and user files on the offered commit."""
    _verify_registration(allocation)
    branch = _git(allocation.checkout, "symbolic-ref", "-q", "HEAD").rstrip(b"\r\n")
    if (
        branch != os.fsencode(f"refs/heads/{delivery['branch']}")
        or _git(allocation.checkout, "rev-parse", "HEAD").strip().decode() != delivery["head"]
    ):
        raise ValueError("worktree branch or commit changed")
    files = {"HEAD", "ORIG_HEAD", "commondir", "gitdir", "index", "logs/HEAD", MARKER, "COMMIT_EDITMSG", "FETCH_HEAD"}
    for entry in allocation.admin.rglob("*"):
        name = entry.relative_to(allocation.admin).as_posix()
        mode = slots._mode_without_links(entry)
        if (name not in files or not stat.S_ISREG(mode)) and (name not in {"logs", "refs"} or not stat.S_ISDIR(mode)):
            raise ValueError(f"worktree administration data retained: {name}")
    _preserve_private_commits(allocation)
    _require_clean(allocation.checkout)
    # Git status trusts its stat cache and normalizes working files. Compare the actual bytes and modes
    # against Git's checkout representation so even a clean-looking user edit survives.
    with tempfile.TemporaryDirectory(dir=run) as scratch:
        pristine = Path(scratch)
        _git(allocation.checkout, "checkout-index", "--all", f"--prefix={pristine.as_posix()}/")
        actual = filesystem_content_digest(allocation.checkout, exclude=frozenset({allocation.checkout / ".git"}))
        if filesystem_content_digest(pristine) != actual:
            raise ValueError("worktree contents differ from the committed checkout")


def _remove(checkpoint: Path, slot: Path, owner: str) -> dict:
    """Recheck custody under the scheduler's admission lock before the sole non-force removal."""
    original = checkpoint.parent if checkpoint.name == publication.REQUEST_DIRECTORY else checkpoint
    allocation = _allocation(original)
    caller_root = Path(os.fsdecode(_git(Path.cwd(), "rev-parse", "--show-toplevel").rstrip(b"\r\n")))
    if caller_root != allocation.caller or slots._checkout_slot(caller_root).parent != allocation.common:
        raise ValueError("cleanup must run from the recorded invoking checkout")
    delivery, journal = _evidence(checkpoint, original, allocation)
    pr, _ = finalizer._publication_record(original / "pr", "pr.json")
    if slot not in (allocation.common / slots.SLOT_NAME, slots._target_slot(allocation.common, pr["url"])):
        raise ValueError("claim is for another repository or PR")
    with slots._admission_lock(allocation.common):
        if slots._observe(slot) != ("busy", owner):
            raise ValueError("active checkout claim belongs to a different owner or is held for inspection")
        live = os.path.lexists(allocation.checkout)
        if live:
            for scoped in (
                slots._checkout_slot(allocation.checkout, writer=True),
                slots._checkout_slot(allocation.checkout, local=True),
            ):
                if slots._observe(scoped)[0] != "free":
                    raise ValueError("worktree has a concurrent checkout claim")
            _clean_registration(allocation, delivery, original)
            _write_journal(checkpoint / JOURNAL, journal | {"status": "removal-authorized"})
            _write_journal(allocation.common / f"codex-remediation-cleanup-{allocation.token}.json", journal)
            _git(allocation.caller, "worktree", "remove", "--", str(allocation.checkout))
        complete = _verify_absence(checkpoint, allocation, journal)
        removed = journal | {"status": "removed"}
        if not complete:
            _write_journal(checkpoint / JOURNAL, removed, complete=True)
            # Only the private, now-empty parent of the checkout removed by this invocation is disposable.
            if live:
                try:
                    allocation.checkout.parent.rmdir()
                except OSError:
                    pass
        return removed


def main(argv: list[str] | None = None) -> int:
    """Report the concrete retention reason or the verified local removal outcome."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--check-owner", action="store_true")
    parser.add_argument("--slot", type=Path)
    parser.add_argument("--owner")
    args = parser.parse_args(argv)
    try:
        run = args.run.absolute()
        if args.check_owner:
            allocation = _allocation(run)
            _verify_registration(allocation)
            result = {"status": "owned", "worktree": allocation.checkout.as_posix()}
        elif args.slot and args.owner:
            result = _remove(run, args.slot, args.owner)
        else:
            parser.error("removal requires --slot and --owner")
    except (
        ValueError,
        OSError,
        TypeError,
        KeyError,
        RuntimeError,
        subprocess.SubprocessError,
        finalizer.DeriveError,
    ) as error:
        print(json.dumps({"status": "retained", "reason": str(error)}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

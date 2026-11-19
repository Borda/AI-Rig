#!/usr/bin/env python3
"""Remove a fresh detached PR checkout after successful review completion.

## Purpose

Reclaim the specific checkout created for a completed review while preserving its report and all local work.

## Scope

The collector records a unique marker in the new Git worktree administration directory. The owning parent keeps the
checkout path and receipt digest before contributed code runs. Reuse, uncertain ownership, incomplete assessment,
integrity stops and additional files (including ignored files and empty directories) retain the checkout.

## Usage

Run with --run, --worktree, --checkout-sha256, --slot and --owner before releasing the slot. Supply the original
--integrity-sha256 after contributor execution, or --no-contributor-execution when no contributor code ran.
Before publishing a witness under an integrity anchor, the helper emits its exact OID to stderr. On interruption,
the parent can retry the same command with --retry-witness and the OID kept from that attempt.
If Git already removed the checkout, the same owner can repeat the command under its slot. The helper validates
the archive and registration, then finalizes an interrupted journal or returns the existing completion receipt.

## Outputs

A JSON disposition goes to stdout. One create-only private ref in the source repository retains a small validation
blob. The run stores its matching replay receipt before Git removes the worktree without force. A private temporary
parent is removed only if empty. Completed-report lookup and remediation do not invoke cleanup.

## Failure

An unsuccessful check preserves the worktree. An interrupted removal retains its pre-removal replay proof; a completed
removal remains replayable if the final bookkeeping write fails. Integrity errors require the normal inspection stop.

## Used by

PR collection records creation; the owning Code Review workflow invokes the terminal cleanup CLI.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

# Do not load helper bytecode left by a prior process before consulting any ownership evidence.
sys.pycache_prefix = str(Path(__file__).resolve())

import review_checkout_slot  # noqa: E402
import review_worktree_replay  # noqa: E402
from collect_diff import reject_executable_filters  # noqa: E402
from contributor_run_integrity import _canonical as integrity_digest  # noqa: E402

MARKER = "codex-review-owner"
RECEIPT = "review-worktree-cleanup.json"


def _regular(path: Path) -> bool:
    """Treat redirected paths, including junctions, as ineligible for ownership verification."""
    return path.resolve() == path and stat.S_ISREG(path.lstat().st_mode)


def _absent(path: Path) -> bool:
    """Distinguish a missing directory entry from a dangling link or an inaccessible path."""
    try:
        path.lstat()
    except FileNotFoundError:
        return True
    return False


def _admin_state(admin: Path) -> dict[str, str]:
    """Bind the detached HEAD, reflog and registration; Git may legitimately refresh index cache metadata."""
    records = {}
    for path in admin.rglob("*"):
        name = path.relative_to(admin).as_posix()
        if name in {"logs", "refs"} and path.resolve() == path and path.is_dir():
            continue
        if name not in {"HEAD", "ORIG_HEAD", "commondir", "gitdir", "index", "logs/HEAD", MARKER} or not _regular(path):
            raise ValueError("unexpected worktree administrative data")
        if name != "index":
            records[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return records


def filesystem_content_digest(root: Path, *, exclude: frozenset[Path] = frozenset()) -> str:
    """Bind original bytes and permission bits, including changes that Git clean filters normalize away."""
    digest = hashlib.sha256()
    pending = [root]
    while pending:
        directory = pending.pop()
        for path in sorted(directory.iterdir()):
            if path in exclude:
                continue
            mode = path.lstat().st_mode
            digest.update(os.fsencode(path.relative_to(root).as_posix()) + b"\0" + str(mode).encode() + b"\0")
            if stat.S_ISLNK(mode):
                digest.update(hashlib.sha256(os.fsencode(os.readlink(path))).digest())
            elif stat.S_ISDIR(mode):
                if path != path.resolve():
                    raise ValueError("redirected worktree directory")
                pending.append(path)
            elif stat.S_ISREG(mode):
                contents = hashlib.sha256()
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1 << 20), b""):
                        contents.update(chunk)
                digest.update(contents.digest())
            else:
                raise ValueError("unsupported worktree entry")
    return digest.hexdigest()


def recorded_content_digest(checkout: dict[str, Any]) -> str | None:
    """Read the pristine filesystem digest, accepting matching cleanup-era and current collection receipts."""
    created = checkout.get("created_worktree", {})
    if not isinstance(created, dict):
        raise ValueError("review-source-collection-digest-invalid")
    digests = []
    for record, field in ((checkout, "worktree_contents_sha256"), (created, "contents_sha256")):
        if field in record:
            value = record[field]
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError("review-source-collection-digest-invalid")
            digests.append(value)
    if len(set(digests)) > 1:
        raise ValueError("review-source-collection-digest-conflict")
    return digests[0] if digests else None


def record_creation(worktree: Path, common: Path) -> dict[str, Any]:
    """Tie a fresh checkout to its Git registration, so a later worktree at the same address is preserved."""
    slot_state, invocation = review_checkout_slot._observe(common / review_checkout_slot.SLOT_NAME)
    if slot_state != "busy" or not invocation:
        raise ValueError("no active review invocation owns this checkout")
    pointer = worktree / ".git"
    if not _regular(pointer):
        raise ValueError("missing worktree gitfile")
    prefix, separator, destination = os.fsdecode(pointer.read_bytes().rstrip(b"\r\n")).partition("gitdir: ")
    if prefix or not separator:
        raise ValueError("invalid worktree gitfile")
    admin = (worktree / destination).resolve(strict=True)
    if admin.parent != common / "worktrees" or not _regular(admin / "gitdir"):
        raise ValueError("unexpected worktree Git directory")
    back = os.fsdecode((admin / "gitdir").read_bytes().rstrip(b"\r\n"))
    if (admin / back).resolve(strict=True) != pointer:
        raise ValueError("worktree registration mismatch")
    token = secrets.token_hex(16)
    with (admin / MARKER).open("x", encoding="ascii") as stream:
        stream.write(token)
    return {
        "git_dir": admin.as_posix(),
        "owner": token,
        "slot_owner": invocation,
        "admin_state": _admin_state(admin),
        "contents_sha256": filesystem_content_digest(worktree),
    }


def _git(root: Path, *args: str) -> bytes:
    """Run one fixed local Git command without hooks, automatic fsmonitor or repository-selection overrides."""
    command = [
        "git",
        "--no-optional-locks",
        "-c",
        "core.fsmonitor=false",
        "-c",
        f"core.hooksPath={os.devnull}",
        "-C",
        str(root),
        *args,
    ]
    environment = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    return subprocess.run(command, env=environment, capture_output=True, check=True, timeout=60).stdout  # noqa: S603


def _verify_completion(run: Path, codex_home: Path | None) -> None:
    """Re-run the read-only producer boundary against the precise promoted artifacts before removal."""
    command = [sys.executable, str(Path(__file__).with_name("find-review-report.py")), "--complete-run", str(run)]
    if codex_home:
        command.extend(["--codex-home", str(codex_home)])
    subprocess.run(command, capture_output=True, check=True, timeout=300)  # noqa: S603


def _write_receipt(run: Path, data: dict[str, Any]) -> None:
    """Publish a complete replay receipt atomically even if the cleanup process is interrupted."""
    with tempfile.NamedTemporaryFile(mode="w", dir=run, encoding="utf-8", delete=False) as stream:
        json.dump(data, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    pending = Path(stream.name)
    destination = run / RECEIPT
    try:
        if data["status"] == "removal-authorized":
            # Preserve any unrelated or malformed pre-existing artifact. A retry can reuse its own identical journal.
            try:
                os.link(pending, destination)
            except FileExistsError:
                if not _regular(destination) or json.loads(destination.read_bytes()) != data:
                    raise ValueError("existing cleanup receipt belongs to different evidence") from None
        else:
            pending.replace(destination)
    finally:
        pending.unlink(missing_ok=True)
    if os.name == "posix":
        # Persist the published directory entry before removing the source on another filesystem.
        directory = os.open(run, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def _require_clean(worktree: Path) -> None:
    """Preserve untracked entries even when Git omits an empty directory or hides an index entry."""
    environment = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    reject_executable_filters(worktree, environment=environment)
    if _git(
        worktree,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignored=matching",
        "--ignore-submodules=none",
    ):
        raise ValueError("worktree contains changed, untracked or ignored data")
    flagged = _git(worktree, "ls-files", "-v", "-z").split(b"\0")
    if any(entry and not entry.startswith(b"H ") for entry in flagged):
        raise ValueError("worktree index masks local state")
    indexed = _git(worktree, "ls-files", "--stage", "-z").split(b"\0")
    allowed = {Path(".git")}
    for entry in filter(None, indexed):
        meta, name = entry.split(b"\t", 1)
        if not meta.startswith((b"100644 ", b"100755 ", b"120000 ")):
            raise ValueError("unsupported tracked entry")
        path = Path(os.fsdecode(name))
        allowed.update((path, *path.parents))

    def onerror(error: OSError) -> None:
        raise error

    for directory, directories, files in os.walk(worktree, onerror=onerror):
        if any((Path(directory) / name).relative_to(worktree) not in allowed for name in (*directories, *files)):
            raise ValueError("worktree contains additional filesystem entries")


def _finish_removed(
    args: argparse.Namespace, run: Path, source: Path, admin: Path, receipt: dict[str, Any]
) -> dict[str, Any]:
    """Validate both durable proofs before acknowledging a missing checkout or finalizing its journal."""
    journal = run / RECEIPT
    if admin.parent != args.slot.parent / "worktrees" or admin != admin.resolve() or not _absent(admin):
        raise ValueError("original worktree registration is not absent")
    if not _regular(journal):
        raise ValueError("missing original cleanup receipt")
    recorded = journal.read_bytes()
    saved = json.loads(recorded)
    removed = receipt | {"status": "removed"}
    if saved not in (receipt, removed):
        raise ValueError("existing cleanup receipt belongs to different evidence")
    if review_checkout_slot._checkout_slot(source) != args.slot:
        raise ValueError("source checkout belongs to a different slot")
    if (
        args.retry_witness
        and _git(source, "show-ref", "--verify", "--hash", review_worktree_replay.witness_ref(run)).strip().decode()
        != args.retry_witness
    ):
        raise ValueError("original cleanup witness does not match")
    # Completion checks the final bytes. Require its archived source to remain eligible before settling the journal.
    _verify_completion(run, args.codex_home)
    if (
        not _absent(admin)
        or not _absent(args.worktree)
        or journal.read_bytes() != recorded
        or review_checkout_slot._observe(args.slot) != ("busy", args.owner)
        or review_worktree_replay.verified_archive(run, run / "result.json") is None
    ):
        raise ValueError("cleanup state changed during archive verification")
    if saved != removed:
        _write_receipt(run, removed)
    return removed


def _cleanup(args: argparse.Namespace) -> dict[str, Any]:
    """Validate the caller's retained identities before allowing one native Git removal."""
    if review_checkout_slot._observe(args.slot) != ("busy", args.owner):
        raise ValueError("review slot is held for inspection or not owned by this run")
    run, worktree = args.run.absolute(), args.worktree
    path = run / "local-checkout.json"
    if run != run.resolve(strict=True) or not _regular(path):
        raise ValueError("redirected review run")
    original = path.read_bytes()
    if hashlib.sha256(original).hexdigest() != args.checkout_sha256:
        raise ValueError("checkout receipt differs from original digest")
    checkout = json.loads(original)
    source = Path(checkout["source_worktree"])
    head = checkout["expected_head"]
    created = checkout.get("created_worktree")
    if (
        not isinstance(created, dict)
        or created.get("slot_owner") != args.owner
        or checkout.get("checkout_mode") != "review"
        or checkout.get("checkout_method") != "git-detached-review-worktree"
        or checkout.get("local_branch") != ""
        or checkout.get("status") != "checked-out"
        or checkout.get("local_head") != head
        or not re.fullmatch("[0-9a-f]{40}", head)
        or checkout.get("command") != f"git worktree add --detach {worktree} {head}"
        or checkout.get("worktree") != worktree.as_posix()
        or not worktree.is_absolute()
        or worktree == source
        or source != source.resolve(strict=True)
        or worktree != worktree.resolve()
        or run.is_relative_to(worktree)
        or run.parent.name != f"pr-{checkout.get('pr_number')}"
        or not re.fullmatch(r"run-[0-9]{3,}", run.name)
    ):
        raise ValueError("missing fresh collector ownership or mismatched run/worktree")
    result_path = run / "result.json"
    if not _regular(result_path) or os.path.lexists(run / "result.candidate.json"):
        raise ValueError("review has pending or missing results")
    result = result_path.read_bytes()
    payload = json.loads(result)
    if payload.get("status") != "pass" or payload["metadata"].get("review_status") not in {None, "closed"}:
        raise ValueError("review did not complete successfully")
    already_absent = _absent(worktree)
    if args.integrity_sha256:
        snapshot = run / "integrity.json"
        body = json.loads(snapshot.read_bytes())
        if not body.get("watch_surface") or Path(body["repo"]) != worktree or Path(body["main_checkout"]) != source:
            raise ValueError("integrity snapshot belongs to another checkout")
        if already_absent and integrity_digest(body) != args.integrity_sha256:
            raise ValueError("original integrity digest does not match")
    elif os.path.lexists(run / "integrity.json"):
        raise ValueError("original integrity digest required")
    elif args.retry_witness:
        raise ValueError("cleanup retry requires the original integrity digest")
    if args.integrity_sha256 and not already_absent:
        verified = subprocess.run(  # noqa: S603 - trusted helper verifies the retained pre-execution digest
            [
                sys.executable,
                str(Path(__file__).with_name("contributor_run_integrity.py")),
                *(
                    ["verify-cleanup-retry", "--retry-witness", args.retry_witness]
                    if args.retry_witness
                    else ["verify"]
                ),
                "--snapshot",
                str(snapshot),
                "--expect",
                args.integrity_sha256,
            ],
            capture_output=True,
            check=False,
            timeout=300,
        )
        if verified.returncode:
            raise RuntimeError(
                "integrity stop: " + (verified.stdout + verified.stderr).decode(errors="replace").strip()
            )
    inputs = review_worktree_replay.verified_source_inputs(run)
    admin = Path(created["git_dir"])
    receipt = {
        "schema_version": 1,
        "status": "removal-authorized",
        "run_dir": run.as_posix(),
        "worktree": worktree.as_posix(),
        "source_worktree": source.as_posix(),
        "head": head,
        "result_sha256": hashlib.sha256(result).hexdigest(),
        "local_checkout_sha256": args.checkout_sha256,
        "source_inputs": inputs,
    }
    journal = run / RECEIPT
    if already_absent:
        return _finish_removed(args, run, source, admin, receipt)
    if review_checkout_slot._checkout_slot(source) != args.slot:
        raise ValueError("source checkout belongs to a different slot")
    _verify_completion(run, args.codex_home)
    if (
        admin.parent != args.slot.parent / "worktrees"
        or admin != admin.resolve(strict=True)
        or not _regular(admin / MARKER)
        or (admin / MARKER).read_text(encoding="ascii") != created["owner"]
        or _admin_state(admin) != created.get("admin_state")
        or Path(os.fsdecode(_git(worktree, "rev-parse", "--absolute-git-dir").rstrip(b"\r\n"))) != admin
        or Path(os.fsdecode(_git(worktree, "rev-parse", "--show-toplevel").rstrip(b"\r\n"))) != worktree
        or _git(worktree, "rev-parse", "HEAD").strip().decode() != head
        or _git(worktree, "branch", "--show-current").strip()
    ):
        raise ValueError("worktree registration, owner or detached head changed")

    _require_clean(worktree)
    if filesystem_content_digest(worktree) != created.get("contents_sha256"):
        raise ValueError("worktree contents differ from the collected checkout")
    if (
        inputs != review_worktree_replay.verified_source_inputs(run)
        or path.read_bytes() != original
        or result_path.read_bytes() != result
    ):
        raise ValueError("review evidence changed")
    if review_checkout_slot._observe(args.slot) != ("busy", args.owner):
        raise ValueError("review slot changed")
    if os.path.lexists(journal) and (not _regular(journal) or json.loads(journal.read_bytes()) != receipt):
        raise ValueError("existing cleanup receipt belongs to different evidence")

    def keep_retry_identity(oid: str) -> None:
        if args.retry_witness and args.retry_witness != oid:
            raise ValueError("current evidence differs from the interrupted cleanup witness")
        print(json.dumps({"retry_witness": oid}), file=sys.stderr, flush=True)

    review_worktree_replay.seal_source_witness(
        run, source, receipt, before_publish=keep_retry_identity if args.integrity_sha256 else None
    )
    _write_receipt(run, receipt)
    _git(source, "worktree", "remove", "--", str(worktree))
    if os.path.lexists(worktree) or os.path.lexists(admin):
        raise ValueError("Git did not finish removing the recorded worktree")
    receipt["status"] = "removed"
    _write_receipt(run, receipt)
    if checkout["worktree_location"] == "temporary-fallback" and worktree.name == "review-worktree":
        try:
            worktree.parent.rmdir()
        except OSError as error:
            receipt["retained_parent"] = str(error)
    return receipt


def main(argv: list[str] | None = None) -> int:
    """Run cleanup once and report retention when any prerequisite fails."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--worktree", type=Path, required=True)
    parser.add_argument("--slot", type=Path, required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--checkout-sha256", required=True)
    parser.add_argument(
        "--retry-witness", help="Exact witness OID kept by the parent immediately before an interrupted cleanup."
    )
    parser.add_argument("--codex-home", type=Path)
    execution = parser.add_mutually_exclusive_group(required=True)
    execution.add_argument("--integrity-sha256")
    execution.add_argument("--no-contributor-execution", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = _cleanup(args)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as error:
        detail = (
            error.stderr.decode(errors="replace").strip() if isinstance(error, subprocess.CalledProcessError) else ""
        )
        print(json.dumps({"status": "cleanup-incomplete", "reason": str(error), "detail": detail}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

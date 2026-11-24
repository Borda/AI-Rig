#!/usr/bin/env python3
"""Create owned local review checkouts and remove detached checkouts after successful review completion.

## Purpose

Reclaim the specific checkout created for a completed review while preserving its report and all local work.

## Scope

The collector records a unique marker in the new Git worktree administration directory. Local working-tree and path
mirrors also retain hashes for each original entry so a partial removal can safely resume. The owning parent keeps the
checkout path and receipt digest before contributed code runs. Reuse, uncertain ownership, incomplete assessment,
integrity stops and additional files (including ignored files and empty directories) retain the checkout.

## Usage

Run with --run, --worktree, --checkout-sha256, --slot and --owner before releasing the slot. These arguments suffice
for a verified same-repository checkout with no contributor integrity interval. For an ordinary assessed PR execution,
supply --ordinary-execution and the promoting finalizer’s --review-result-sha256. Supply the original --integrity-sha256
for a protected interval, or --no-contributor-execution when no contributor code ran. Supply --review-result-sha256
from the promoting finalizer to authenticate its chosen result for a later execution-safety decision.
After an inspected restart, also supply --inspected-snapshot and its parent-held --inspected-sha256. Use
--original-snapshot for an original baseline with a different name from integrity.json.
Before publishing a witness under an integrity anchor, the helper emits its exact OID to stderr. On interruption,
the parent can retry the same command with --retry-witness and the OID kept from that attempt.
If Git already removed the checkout, the same owner can repeat the command under its slot. The helper validates
the archive and registration, then finalizes an interrupted journal or returns the existing completion receipt.

## Outputs

A JSON disposition goes to stdout. One create-only private ref in the source repository retains a small validation
blob. The run stores its matching replay receipt before the helper removes the worktree without force.
A private temporary
parent is removed only if empty. Completed-report lookup and remediation do not invoke cleanup.

## Failure

An unsuccessful check preserves the worktree. An interrupted removal retains its pre-removal replay proof; a completed
removal remains replayable if the final bookkeeping write fails. Integrity errors require the normal inspection stop.

## Used by

Local review creation uses --create-local with --repository, --run, --owner and --slot; commit scope also takes --head.
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

import collect_diff  # noqa: E402
import review_checkout_slot  # noqa: E402
import review_worktree_replay  # noqa: E402
from collect_diff import reject_executable_filters  # noqa: E402
from contributor_run_integrity import _canonical as integrity_digest  # noqa: E402
from review_workspace import workspace_caller  # noqa: E402

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


def _entry_digest(path: Path, mode: int) -> bytes:
    """Identify file or symlink bytes, leaving directories to their individually verified children."""
    if stat.S_ISLNK(mode):
        return hashlib.sha256(os.fsencode(os.readlink(path))).digest()
    if stat.S_ISDIR(mode) and path == path.resolve():
        return b""
    if not stat.S_ISREG(mode):
        raise ValueError("unsupported worktree entry")
    contents = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            contents.update(chunk)
    return contents.digest()


def filesystem_content_digest(
    root: Path, *, exclude: frozenset[Path] = frozenset(), entries: dict[str, list[Any]] | None = None
) -> str:
    """Bind original bytes and permission bits, including changes that Git clean filters normalize away."""
    digest = hashlib.sha256()
    pending = [root]
    while pending:
        directory = pending.pop()
        for path in sorted(directory.iterdir()):
            if path in exclude:
                continue
            mode = path.lstat().st_mode
            name = path.relative_to(root).as_posix()
            content = _entry_digest(path, mode)
            digest.update(os.fsencode(name) + b"\0" + str(mode).encode() + b"\0" + content)
            if entries is not None:
                entries[name] = [mode, content.hex()]
            if stat.S_ISDIR(mode):
                pending.append(path)
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


def record_creation(
    worktree: Path,
    common: Path,
    target: str | None = None,
    *,
    local_slot: Path | None = None,
    capture_entries: bool = False,
) -> dict[str, Any]:
    """Tie a fresh checkout to its Git registration, so a later worktree at the same address is preserved."""
    slot = local_slot or common / review_checkout_slot.SLOT_NAME
    if target:
        scoped = review_checkout_slot._target_slot(common, target)
        if scoped.exists():
            slot = scoped
    slot_state, invocation = review_checkout_slot._observe(slot)
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
    entries = {} if capture_entries else None
    digest = filesystem_content_digest(worktree, entries=entries)
    return {
        "git_dir": admin.as_posix(),
        "owner": token,
        "slot_owner": invocation,
        "admin_state": _admin_state(admin),
        "contents_sha256": digest,
        **({"entries": entries} if entries is not None else {}),
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
    environment.update(GIT_NO_LAZY_FETCH="1", GIT_ALLOW_PROTOCOL="", GIT_NO_REPLACE_OBJECTS="1")
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
    _require_known_entries(worktree)


def _require_known_entries(worktree: Path, *, copied: tuple[bytes, ...] = ()) -> None:
    """Reject content outside the tracked checkout and the collector's verified untracked overlay."""
    indexed = _git(worktree, "ls-files", "--stage", "-z").split(b"\0")
    allowed = {Path(".git")}
    for name in filter(None, copied):
        path = Path(os.fsdecode(name))
        allowed.update((path, *path.parents))
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
    args: argparse.Namespace, run: Path, source: Path, common: Path, admin: Path, receipt: dict[str, Any]
) -> dict[str, Any]:
    """Validate both durable proofs before acknowledging a missing checkout or finalizing its journal."""
    journal = run / RECEIPT
    if admin.parent != common / "worktrees" or admin != admin.resolve() or not _absent(admin):
        raise ValueError("original worktree registration is not absent")
    if not _regular(journal):
        raise ValueError("missing original cleanup receipt")
    recorded = journal.read_bytes()
    saved = json.loads(recorded)
    removed = receipt | {"status": "removed"}
    if saved not in (receipt, removed):
        raise ValueError("existing cleanup receipt belongs to different evidence")
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


def has_integrity_interval(run: Path) -> bool:
    """Recognize retained contributor baselines and successors before choosing an execution route."""
    return (
        os.path.lexists(run / "integrity.json")
        or os.path.lexists(run / "integrity-retry.json")
        or any(run.glob("integrity-restart*.json"))
    )


def integrity_main_checkout(checkout: dict[str, Any]) -> Path:
    """Bind contributor monitoring to the original caller even when collection used a private repository."""
    source = Path(checkout["source_worktree"])
    caller = workspace_caller(source, str(checkout.get("pr_url") or ""))
    if checkout.get("integrity_main_checkout") != (caller.as_posix() if caller is not None else None):
        raise ValueError("private review integrity checkout mismatch")
    return caller if caller is not None else source


def _terminal_snapshot(args: argparse.Namespace, run: Path, source: Path) -> tuple[Path, str] | None:
    """Authenticate the original baseline and select the retained interval for terminal verification."""
    original = args.original_snapshot or run / "integrity.json"
    active = args.inspected_snapshot
    if not args.integrity_sha256:
        if (
            args.original_snapshot
            or active
            or args.inspected_sha256
            or args.retry_witness
            or has_integrity_interval(run)
        ):
            raise ValueError("original integrity digest required")
        return None

    if original.parent != run or not _regular(original):
        raise ValueError("original integrity snapshot must be a regular file in the review run")
    prior = json.loads(original.read_bytes())
    if integrity_digest(prior) != args.integrity_sha256:
        raise ValueError("original integrity digest does not match")
    if not prior.get("watch_surface") or Path(prior["repo"]) != args.worktree or Path(prior["main_checkout"]) != source:
        raise ValueError("integrity snapshot belongs to another checkout")
    if active is None and not args.inspected_sha256:
        if args.original_snapshot and original != run / "integrity.json":
            raise ValueError("alternate original snapshot requires an inspected successor")
        return original, args.integrity_sha256
    if (
        not active
        or not args.inspected_sha256
        or active.parent != run
        or active == original
        or not re.fullmatch(r"integrity-restart(?:-[1-9][0-9]*)?\.json", active.name)
        or not _regular(active)
    ):
        raise ValueError("inspected cleanup requires a distinct restart snapshot in the review run and its digest")
    current = json.loads(active.read_bytes())
    if integrity_digest(current) != args.inspected_sha256:
        raise ValueError("inspected integrity digest does not match")
    if any(prior[key] != current[key] for key in ("repo", "main_checkout", "dirs", "watch_surface", "exclude")):
        raise ValueError("inspected integrity snapshot changed the measurement scope")
    return active, args.inspected_sha256


def _create_local(args: argparse.Namespace) -> dict[str, Any]:
    """Create a fresh local mirror or commit checkout and bind its original bytes to the invoking owner."""
    run = args.run.resolve(strict=True)
    source = args.repository.resolve(strict=True)
    if args.slot != review_checkout_slot._checkout_slot(source, local=True) or review_checkout_slot._observe(
        args.slot
    ) != ("busy", args.owner):
        raise ValueError("local review slot is not owned by this invocation")
    if run == source or source.is_relative_to(run) or args.run.absolute() != run:
        raise ValueError("local review run overlaps caller or is redirected")
    path = run / review_worktree_replay.LOCAL_OWNER
    worktree = run / ("commit-review-worktree" if args.create_local == "commit" else "local-source-review-worktree")
    if any(not _absent(item) for item in (path, worktree, run / "local-source", run / "local-checkout.json")):
        raise ValueError("local review source or receipt already exists")
    if args.create_local == "commit":
        if not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", args.head or ""):
            raise ValueError("commit checkout requires the frozen full head")
        collect_diff._require_ignored_inside_repository(source, worktree)
        reject_executable_filters(source)
        _git(source, "worktree", "add", "--detach", str(worktree), args.head)
        head = args.head
    else:
        if args.head is not None:
            raise ValueError("only a commit checkout accepts a frozen head")
        mirror = collect_diff.collect_review_worktree(source, run / "local-source")
        head = mirror["revision"]
    common = review_checkout_slot._common(args.slot)
    created = record_creation(worktree, common, local_slot=args.slot, capture_entries=args.create_local != "commit")
    directory = worktree.stat()
    created["directory_identity"] = [directory.st_dev, directory.st_ino]
    checkout = {
        "schema_version": 1,
        "checkout_mode": "local-review",
        "scope": args.create_local,
        "worktree": worktree.as_posix(),
        "source_worktree": source.as_posix(),
        "expected_head": head,
        "created_worktree": created,
        "index_sha256": hashlib.sha256(_git(worktree, "ls-files", "-v", "--stage", "-z")).hexdigest(),
    }
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(checkout, stream, sort_keys=True)
        stream.write("\n")
    return {"worktree": worktree.as_posix(), "checkout_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _remove_mirror_files(worktree: Path, created: dict[str, Any]) -> None:
    """Reclaim only the authenticated original entries, including an interrupted removal's remaining subset."""
    if _absent(worktree):
        return
    directory = worktree.stat()
    if [directory.st_dev, directory.st_ino] != created["directory_identity"]:
        raise ValueError("original local mirror directory was replaced")
    expected = created["entries"]
    observed: dict[str, list[Any]] = {}
    filesystem_content_digest(worktree, entries=observed)
    if any(expected.get(name) != entry for name, entry in observed.items()):
        raise ValueError("local mirror contains changed or additional entries")
    # Git refuses to remove this intentionally dirty checkout. Delete each original entry individually.
    # New children survive rmdir, late edits fail the per-file test, and a retry can verify the remaining subset.
    # Keep the gitfile until last so an interrupted checkout remains recognizable while its files are present.
    for name in sorted(observed, key=lambda name: (name != ".git", len(Path(name).parts), name), reverse=True):
        path = worktree / name
        mode = path.lstat().st_mode
        if path.parent.resolve(strict=True) != path.parent or [mode, _entry_digest(path, mode).hex()] != expected[name]:
            raise ValueError("local mirror entry changed during cleanup")
        if stat.S_ISDIR(mode):
            path.rmdir()
        else:
            path.unlink()
    worktree.rmdir()


def _cleanup(args: argparse.Namespace) -> dict[str, Any]:
    """Validate the caller's retained identities before allowing one native Git removal."""
    if review_checkout_slot._observe(args.slot) != ("busy", args.owner):
        raise ValueError("review slot is held for inspection or not owned by this run")
    run, worktree = args.run.absolute(), args.worktree
    local = os.path.lexists(run / review_worktree_replay.LOCAL_OWNER)
    path = run / (review_worktree_replay.LOCAL_OWNER if local else "local-checkout.json")
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
        or not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", head)
        or checkout.get("worktree") != worktree.as_posix()
        or not worktree.is_absolute()
        or worktree == source
        or source != source.resolve(strict=True)
        or worktree != worktree.resolve()
        or run.is_relative_to(worktree)
    ):
        raise ValueError("missing fresh collector ownership or mismatched run/worktree")
    exclusive = review_checkout_slot._checkout_slot(source)
    common = exclusive.parent
    if local:
        scope = checkout.get("scope")
        name = "commit-review-worktree" if scope == "commit" else "local-source-review-worktree"
        if (
            scope not in {"working-tree", "path", "commit"}
            or checkout.get("checkout_mode") != "local-review"
            or worktree != run / name
            or os.path.lexists(run / "local-checkout.json")
            or args.slot != review_checkout_slot._checkout_slot(source, local=True)
            or args.ordinary_execution
            or args.no_contributor_execution
            or has_integrity_interval(run)
        ):
            raise ValueError("local review checkout, scope or slot mismatch")
    else:
        if (
            checkout.get("checkout_mode") != "review"
            or checkout.get("checkout_method") != "git-detached-review-worktree"
            or checkout.get("local_branch") != ""
            or checkout.get("status") != "checked-out"
            or checkout.get("local_head") != head
            or checkout.get("command") != f"git worktree add --detach {worktree} {head}"
            or run.parent.name != f"pr-{checkout.get('pr_number')}"
            or not re.fullmatch(r"run-[0-9]{3,}", run.name)
        ):
            raise ValueError("missing fresh collector ownership or mismatched run/worktree")
        target = checkout.get("pr_url")
        if args.slot != exclusive and (not target or args.slot != review_checkout_slot._target_slot(common, target)):
            raise ValueError("source checkout belongs to a different slot")
    result_path = run / "result.json"
    if not _regular(result_path) or os.path.lexists(run / "result.candidate.json"):
        raise ValueError("review has pending or missing results")
    result = result_path.read_bytes()
    result_digest = hashlib.sha256(result).hexdigest()
    if args.review_result_sha256 is not None and args.review_result_sha256 != result_digest:
        raise ValueError("review result differs from producer digest")
    payload = json.loads(result)
    assessed = payload["metadata"].get("review_status")
    if not (payload.get("status") == "pass" and assessed in {None, "closed"}) and not (
        payload.get("status") == "fail" and assessed is None and payload["metadata"].get("review_decision")
    ):
        raise ValueError("review did not complete successfully")
    if local and payload["metadata"].get("scope") != checkout["scope"]:
        raise ValueError("local review result belongs to a different scope")
    already_absent = _absent(worktree)
    if args.slot != exclusive and (args.integrity_sha256 or has_integrity_interval(run)):
        raise ValueError("protected cleanup requires the repository-wide review slot")
    interval = _terminal_snapshot(args, run, integrity_main_checkout(checkout))
    if args.ordinary_execution and args.review_result_sha256 is None:
        raise ValueError("ordinary execution requires the promoting finalizer's result digest")
    if (
        interval is None
        and not local
        and not args.no_contributor_execution
        and not args.ordinary_execution
        and checkout.get("verified_same_repository") is not True
    ):
        raise ValueError(
            "specify ordinary execution, contributor integrity digest or confirmation of no contributor execution"
        )
    if interval is not None and not already_absent:
        snapshot, anchor = interval
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
                anchor,
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
        "schema_version": 2,
        "producer_result_verified": args.review_result_sha256 is not None,
        "status": "removal-authorized",
        "run_dir": run.as_posix(),
        "worktree": worktree.as_posix(),
        "source_worktree": source.as_posix(),
        "head": head,
        "result_sha256": result_digest,
        "local_checkout_sha256": args.checkout_sha256,
        "source_inputs": inputs,
    }
    # Resume an already sealed historical cleanup with its original byte contract. Its evidence stays unverified
    # for execution safety, and the ordinary replay checks still require the exact existing Git witness.
    if args.review_result_sha256 is None:
        sealed = review_worktree_replay.existing_witness(run, source)
        if sealed is not None and sealed.get("schema_version") == 1:
            receipt["schema_version"] = 1
            del receipt["producer_result_verified"]
    journal = run / RECEIPT
    mirror = local and checkout["scope"] != "commit"
    retiring = mirror and os.path.lexists(journal) and _regular(journal)
    if already_absent and (not retiring or _absent(admin)):
        return _finish_removed(args, run, source, common, admin, receipt)
    if retiring:
        # Only the Git witness from the successful live validation can resume a partially deleted overlay.
        if json.loads(journal.read_bytes()) != receipt:
            raise ValueError("existing cleanup receipt belongs to different evidence")
        review_worktree_replay.verify_source_witness(run, source, receipt)
    else:
        _verify_completion(run, args.codex_home)
    if (
        admin.parent != common / "worktrees"
        or admin != admin.resolve(strict=True)
        or not _regular(admin / MARKER)
        or (admin / MARKER).read_text(encoding="ascii") != created["owner"]
        or _admin_state(admin) != created.get("admin_state")
        or (
            not retiring
            and (
                Path(os.fsdecode(_git(worktree, "rev-parse", "--absolute-git-dir").rstrip(b"\r\n"))) != admin
                or Path(os.fsdecode(_git(worktree, "rev-parse", "--show-toplevel").rstrip(b"\r\n"))) != worktree
                or _git(worktree, "rev-parse", "HEAD").strip().decode() != head
                or _git(worktree, "branch", "--show-current").strip()
            )
        )
    ):
        raise ValueError("worktree registration, owner or detached head changed")

    if mirror:
        index = _git(source, f"--git-dir={admin}", "ls-files", "-v", "--stage", "-z")
        if hashlib.sha256(index).hexdigest() != checkout["index_sha256"]:
            raise ValueError("local mirror index differs from collection")
        if not retiring:
            collect_diff.verify_review_worktree(run / "local-source")
            untracked = _git(worktree, "ls-files", "--others", "--exclude-standard", "-z")
            _require_known_entries(worktree, copied=tuple(untracked.split(b"\0")))
    else:
        _require_clean(worktree)
    if not retiring and filesystem_content_digest(worktree) != created.get("contents_sha256"):
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
    if mirror:
        _remove_mirror_files(worktree, created)
    _git(source, "worktree", "remove", "--", str(worktree))
    if os.path.lexists(worktree) or os.path.lexists(admin):
        raise ValueError("Git did not finish removing the recorded worktree")
    receipt["status"] = "removed"
    _write_receipt(run, receipt)
    if checkout.get("worktree_location") == "temporary-fallback" and worktree.name == "review-worktree":
        try:
            worktree.parent.rmdir()
        except OSError as error:
            receipt["retained_parent"] = str(error)
    return receipt


def main(argv: list[str] | None = None) -> int:
    """Run cleanup once and report retention when any prerequisite fails."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--worktree", type=Path)
    parser.add_argument("--create-local", choices=("working-tree", "path", "commit"))
    parser.add_argument("--repository", type=Path)
    parser.add_argument("--head", help="Frozen commit for a new local commit checkout.")
    parser.add_argument("--slot", type=Path, required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--checkout-sha256")
    parser.add_argument("--review-result-sha256", help="Digest retained from the trusted promoting review finalizer.")
    parser.add_argument(
        "--retry-witness", help="Exact witness OID kept by the parent immediately before an interrupted cleanup."
    )
    parser.add_argument("--original-snapshot", type=Path, help="Retained original baseline for an inspected restart.")
    parser.add_argument("--inspected-snapshot", type=Path, help="Absolute path of the retained inspected successor.")
    parser.add_argument("--inspected-sha256", help="Parent-held digest printed when the successor was created.")
    parser.add_argument("--codex-home", type=Path)
    execution = parser.add_mutually_exclusive_group()
    execution.add_argument("--integrity-sha256")
    execution.add_argument("--no-contributor-execution", action="store_true")
    execution.add_argument(
        "--ordinary-execution",
        action="store_true",
        help="Parent confirms assessed execution outside a protected interval.",
    )
    args = parser.parse_args(argv)
    if args.create_local and (not args.repository or args.worktree or args.checkout_sha256):
        parser.error("local creation requires --repository and an unused run")
    if not args.create_local and (not args.worktree or not args.checkout_sha256 or args.repository or args.head):
        parser.error("cleanup requires --worktree and --checkout-sha256")
    try:
        result = _create_local(args) if args.create_local else _cleanup(args)
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

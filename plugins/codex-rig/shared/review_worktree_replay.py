"""Bind completed PR review source evidence to the checkout removed by terminal cleanup.

## Purpose

Keep direct and batched reviews verifiable after their clean, detached worktree has been reclaimed. Their plans already
contain the exact reviewed file records. The cleanup receipt fingerprints those plans and the source inventories used
by the live validators before removal. Terminal closes bind their collection evidence. A create-only local Git ref seals
the hashes outside the report.

## Scope

Only the exact promoted result can select retained source. Preparation, candidate checks, local reviews, and any
remaining checkout continue to use live source. Role assignments, rollout provenance, coverage, and gates still go
through their respective ordinary validators.

## Usage

The cleanup writer calls verified_source_inputs around live completion. The review validator opens
completed_source_validation around promoted-result checks. Its nested plan validators consult retained_checkout.

## Outputs

The cleanup writer stores one small Git blob and ref. Historical validators only read the witness and report.

## Failure

Reject changed inputs or mismatched checkout, commit, report and result identities. An interrupted native removal
also requires proof that the worktree is no longer registered before its pre-removal receipt can supply source.

## Used by

Code Review terminal cleanup, completed-result validation and remediation's historical report admission.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

#: PR artifact files that must be present for a closed-PR review.
CLOSED_REQUIRED_PR_ARTIFACTS = {
    "pr.json",
    "pr-routing.json",
    "remote-selection.json",
    "target-branch.json",
    "local-checkout.json",
    "comments.json",
    "reviews.json",
    "review-threads.json",
    "unresolved-review-threads.json",
    "online-review-summary.json",
    "diff.patch",
}

# Validation visits nested direct/batch consumers; only the promoted-result invocation may select archived evidence.
_ARCHIVE: ContextVar[tuple[Path, dict[str, Any], dict[str, str]] | None] = ContextVar(
    "completed_pr_source", default=None
)


def _digest(path: Path) -> str:
    """Hash a contained regular artifact in bounded chunks and reject redirected paths."""
    if path.is_symlink() or not path.is_file() or path.resolve() != path:
        raise ValueError(f"review-cleanup-source-file-invalid:{path.name}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def verified_source_inputs(run: Path) -> dict[str, str]:
    """Fingerprint source records used by live completion, including optional plans and inventories."""
    run = run.resolve()
    result = json.loads((run / "result.json").read_bytes())
    if result.get("status") == "pass" and result.get("metadata", {}).get("review_status") == "closed":
        # A terminal close forbids a specialist manifest and rests instead on the collected proposal evidence.
        names = sorted(CLOSED_REQUIRED_PR_ARTIFACTS | {"pr-head-fetch.json", "worktree-preflight.json", "gates.json"})
    else:
        names = ["diff.patch", "specialist-manifest.json"]
    for name in ("files.txt", "untracked.txt", "direct-plan.json", "batch-inventory.json", "inspection-plan.json"):
        if os.path.lexists(run / name):
            names.append(name)
    return {name: _digest(run / name) for name in names}


def witness_ref(run: Path) -> str:
    """Name the private write-once ref from the actual promoted location, independently of the report contents."""
    identity = hashlib.sha256(run.resolve().as_posix().encode("utf-8")).hexdigest()
    return f"refs/codex-rig/review-archives/{identity}"


def _git(source: Path, *args: str, data: bytes | None = None, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    """Execute a fixed local plumbing operation without hooks, replacements, or ambient Git overrides."""
    command = [
        "git",
        "--no-pager",
        "--no-replace-objects",
        "-c",
        f"core.hooksPath={os.devnull}",
        "-C",
        str(source),
        *args,
    ]
    return subprocess.run(  # noqa: S603 - only fixed plumbing and validated path/object identifiers
        command,
        input=data,
        env={k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")},
        capture_output=True,
        check=check,
        timeout=30,
    )


def _report_repository(run: Path) -> Path | None:
    """Locate the repository ancestor independently of paths supplied by a canonical report."""
    if len(run.parents) >= 5 and run.parents[1].parts[-3:] == (".reports", "codex", "code-review"):
        return run.parents[4]
    return None


def _require_live_checkout_if_sealed(run: Path, checkout_path: Path, live: bool) -> None:
    """Prevent deleting the cleanup receipt or redirecting its worktree to bypass an existing canonical witness."""
    repository = _report_repository(run)
    if repository is None:
        return
    witness = _git(repository, "rev-parse", "--verify", witness_ref(run) + "^{blob}", check=False)
    if witness.returncode:
        return
    if not live:
        raise ValueError("review-cleanup-witness-missing")
    # A crash may leave a live checkout between publication of the witness and the cleanup journal.
    # Check that exact checkout before falling back to ordinary live validation.
    oid = witness.stdout.decode("ascii").strip()
    if (
        not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", oid)
        or int(_git(repository, "cat-file", "-s", oid).stdout) > 16384
    ):
        raise ValueError("review-cleanup-witness-object-invalid")
    sealed = json.loads(_git(repository, "cat-file", "blob", oid).stdout)
    if sealed.get("local_checkout_sha256") != _digest(checkout_path):
        raise ValueError("review-cleanup-witness-mismatch")


def _witness(
    run: Path, source: Path, receipt: dict[str, Any], *, seal: bool, before_publish: Callable[[str], None] | None = None
) -> None:
    """Authenticate live validation in the original repository, outside the report writer's control."""
    if (
        source != source.resolve(strict=True)
        or source.is_relative_to(run)
        or source.as_posix() != receipt["source_worktree"]
    ):
        raise ValueError("review-cleanup-witness-repository-mismatch")
    top = _git(source, "rev-parse", "--show-toplevel").stdout.removesuffix(b"\n")
    if Path(os.fsdecode(top)).resolve(strict=True) != source:
        raise ValueError("review-cleanup-witness-repository-mismatch")
    git_dir = _git(source, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.removesuffix(b"\n")
    common = Path(os.fsdecode(git_dir)).resolve(strict=True)
    if common.is_relative_to(run):
        raise ValueError("review-cleanup-witness-repository-mismatch")
    # The normal report hierarchy independently identifies the repository even if the report claims another checkout.
    # Explicit external report roots instead rely on the original repository's write-once, path-specific witness.
    repository = _report_repository(run)
    if repository is not None:
        owner = _git(repository, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False)
        if owner.returncode == 0 and Path(os.fsdecode(owner.stdout.removesuffix(b"\n"))).resolve(strict=True) != common:
            raise ValueError("review-cleanup-witness-repository-mismatch")
    # Status changes after Git removes the checkout. All identity and evidence fields stay byte-exact.
    fields = (
        "schema_version",
        "run_dir",
        "worktree",
        "source_worktree",
        "head",
        "result_sha256",
        "local_checkout_sha256",
        "source_inputs",
    )
    data = json.dumps({key: receipt[key] for key in fields}, sort_keys=True, separators=(",", ":")).encode()
    if len(data) > 16384:
        raise ValueError("review-cleanup-witness-oversized")
    ref = witness_ref(run)
    if _git(source, "symbolic-ref", "-q", ref, check=False).returncode == 0:
        raise ValueError("review-cleanup-witness-mismatch")
    oid = _git(source, "hash-object", "--stdin", data=data).stdout.decode("ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", oid):
        raise ValueError("review-cleanup-witness-object-invalid")
    if seal:
        if before_publish is not None:
            before_publish(oid)
        # Existing claims, including a ref whose object went missing, are verified without repairing their evidence.
        # Create-only publication also protects a conflicting ref that appears after this read.
        existing = _git(source, "rev-parse", "--verify", "--quiet", ref, check=False)
        if existing.returncode == 1:
            _git(source, "hash-object", "-w", "--stdin", data=data)
            _git(source, "update-ref", "--no-deref", ref, oid, "0" * len(oid), check=False)
        elif existing.returncode:
            raise ValueError("review-cleanup-witness-mismatch")
    actual = _git(source, "show-ref", "--verify", "--hash", ref, check=False)
    if (
        actual.returncode
        or actual.stdout.strip() != oid.encode()
        or _git(source, "cat-file", "-e", f"{oid}^{{blob}}", check=False).returncode
    ):
        raise ValueError("review-cleanup-witness-mismatch")


def seal_source_witness(
    run: Path, source: Path, receipt: dict[str, Any], *, before_publish: Callable[[str], None] | None = None
) -> None:
    """Seal the exact verified cleanup inputs after live completion and before native removal."""
    _witness(run.resolve(), source, receipt, seal=True, before_publish=before_publish)


def verified_archive(run: Path, result_path: Path) -> tuple[Path, dict[str, Any], dict[str, str]] | None:
    """Admit exact retained evidence only for a completed removal of a freshly collected detached PR worktree."""
    receipt_path = run / "review-worktree-cleanup.json"
    if result_path != run / "result.json":
        return None
    checkout_path = run / "local-checkout.json"
    journal = os.path.lexists(receipt_path)
    checkout = json.loads(checkout_path.read_bytes()) if checkout_path.is_file() else {}
    live = isinstance(checkout.get("worktree"), str) and os.path.lexists(checkout["worktree"])
    if live or not journal:
        _require_live_checkout_if_sealed(run, checkout_path, live)
        if not live and checkout.get("created_worktree"):
            raise ValueError("review-cleanup-witness-missing")
        return None
    if receipt_path.resolve() != receipt_path or not receipt_path.is_file():
        raise ValueError("review-cleanup-receipt-invalid")
    receipt = json.loads(receipt_path.read_bytes())
    if (
        not isinstance(receipt, dict)
        or type(receipt.get("schema_version")) is not int
        or receipt["schema_version"] != 1
        or receipt.get("status") not in {"removed", "removal-authorized"}
        or receipt.get("run_dir") != run.as_posix()
        or receipt.get("result_sha256") != _digest(result_path)
        or receipt.get("local_checkout_sha256") != _digest(checkout_path)
    ):
        raise ValueError("review-cleanup-receipt-invalid")
    if (
        checkout.get("checkout_mode") != "review"
        or checkout.get("checkout_method") != "git-detached-review-worktree"
        or not checkout.get("created_worktree")
        or receipt.get("worktree") != checkout["worktree"]
        or receipt.get("source_worktree") != checkout["source_worktree"]
        or receipt.get("head") != checkout["expected_head"]
    ):
        raise ValueError("review-cleanup-checkout-mismatch")
    inputs = verified_source_inputs(run)
    if inputs != receipt.get("source_inputs"):
        raise ValueError("review-cleanup-source-evidence-changed")
    worktree = Path(checkout["worktree"])
    _witness(run, Path(checkout["source_worktree"]), receipt, seal=False)
    # An interruption after native removal may leave the earlier receipt. Missing files alone cannot prove removal.
    registered = _git(Path(checkout["source_worktree"]), "worktree", "list", "--porcelain", "-z").stdout
    if any(
        Path(os.fsdecode(field[9:])) == worktree for field in registered.split(b"\0") if field.startswith(b"worktree ")
    ):
        raise ValueError("review-cleanup-worktree-still-registered")
    return run, checkout, inputs


@contextmanager
def completed_source_validation(run: Path, result_path: Path) -> Iterator[None]:
    """Limit archived source to this result validation, including nested batch and retry checks."""
    token = _ARCHIVE.set(verified_archive(run.resolve(), result_path.parent.resolve() / result_path.name))
    try:
        yield
    finally:
        _ARCHIVE.reset(token)


def retained_checkout(run: Path, plan_path: Path, source_root: Path) -> dict[str, Any] | None:
    """Return the checkout binding for this exact source plan during promoted-result validation."""
    archive = _ARCHIVE.get()
    if archive is None:
        return None
    directory, checkout, inputs = archive
    if (
        run.resolve() != directory
        or source_root.as_posix() != checkout["worktree"]
        or plan_path.resolve()
        not in {directory / name for name in inputs if name in {"direct-plan.json", "batch-inventory.json"}}
    ):
        raise ValueError("review-cleanup-source-plan-mismatch")
    return checkout

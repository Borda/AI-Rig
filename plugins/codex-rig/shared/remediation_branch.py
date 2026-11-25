#!/usr/bin/env python3
"""Record the verified original PR branch for subsequent remediation commits.

## Purpose

    Bind the collector's successful original-branch checkout to the branch used for
    editing, target integration, and optional local commits. Source identity is
    independent of publication configuration; this receipt never authorizes a push.
## Scope

    The optional worktree action creates a separate linked checkout for the collector.
    All other actions use local Git queries only. Preparation records the existing branch and verifies
    its PR source from tracking or, for a same-repository native checkout tracking the target,
    from the selected remote's PR branch ref. It never creates or switches branches, sets tracking,
    or repairs a changed revision. Only the explicitly requested receipt is written.
## Usage

    After an occupied-branch checkout failure, use ``locate-owner --pr-dir <collector-directory>``
    to locate the clean registered PR worktree. An optional receipt records the borrowed delivery location.
    For an explicitly requested separate PR checkout, run ``python remediation_branch.py
    worktree --receipt <run>/remediation-worktree.json`` from the invoking checkout,
    then execute the remediation-mode collector from the returned worktree.
    Run ``python remediation_branch.py prepare --pr-dir <collected-pr-directory>
    --receipt <branch.json>`` immediately after collection. Run ``check`` with that
    receipt and ``--expected-head <recorded-revision>`` before integration or each
    optional commit, and after a commit using its independently observed revision. If the native branch
    tracks the target, check reads the collector's sibling ``pr/`` directory or an explicit ``--pr-dir``.
    For a legacy run already on its original PR branch, use ``recover --legacy-receipt
    <old.json> --pr-dir <retained-pr-directory> --receipt <new.json> --expected-head
    <last-authorized-revision>`` before continuing with ordinary checks.
## Outputs

    The worktree action records the invoking and new checkout paths before calling Git,
    preserving the location on interruption. Preparation prints and exclusively writes a JSON receipt binding the PR,
    worktree, initial revision, PR source, and attached branch. Check prints that receipt
    only after the current branch and expected revision match. Recovery exclusively
    writes a separate schema-2 receipt with the legacy evidence digest and verified
    continuation revision; it does not claim a new collector checkout occurred.
## Failure

    Invalid source identity, unverified checkout route, tracking mismatch, operation
    state, existing receipt, or branch/head drift exits 2 with a diagnostic. Legacy
    receipts require explicit recovery verification on the original PR branch. Receipt files are evidence,
    not authorization, and a failed check never changes Git state.
## Used by

    Code Remediate before changes and its opt-in commit preflight, plus disposable
    real-Git tests covering collection through the next user action.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import subprocess
import sys
import tempfile
from functools import partial
from pathlib import Path
from typing import Any

# A source file cannot contain a bytecode cache directory.
sys.pycache_prefix = str(Path(__file__).resolve())

from collect_pr import (  # noqa: E402
    CollectionError,
    _github_remote_identity,
    _head_repository,
    pr_worktree_index_unmasked,
    pr_worktree_is_clean,
    verify_pr_tracking,
)
from review_workspace import _checkout_directories, workspace_namespace  # noqa: E402


def _git(*arguments: str, optional: bool = False, cwd: Path | None = None) -> str:
    """Run a local Git command and distinguish ordinary negative lookups from errors."""
    result = subprocess.run(["git", *arguments], cwd=cwd, capture_output=True, text=True, check=False, timeout=30)  # noqa: S603, S607 - argv list, no shell; tool resolved via PATH on purpose
    if optional and result.returncode == 1 and (arguments[:2] == ("config", "--get") or arguments[0] == "check-ignore"):
        return ""
    if result.returncode:
        raise ValueError(f"git-command-failed:{arguments[0]}:exit-{result.returncode}")
    return result.stdout.strip()


def locate_owner(pr_dir: Path, receipt: Path | None) -> dict[str, Any]:
    """Locate a clean attached checkout of the fetched PR branch before retrying collection there."""
    pr = _read(pr_dir / "pr.json")
    branch, head, repository = pr["headRefName"], pr["headRefOid"], _head_repository(pr)
    if pr.get("state") != "OPEN" or not re.fullmatch(r"[0-9a-f]{40}", head):
        raise ValueError("invalid-pr-source")
    fetched = _read(pr_dir / "pr-head-fetch.json")
    state = _read(pr_dir / "checkout-state.json")
    failure = state.get("gh_checkout_failure") or {}
    diagnostic = failure.get("diagnostics") if isinstance(failure, dict) else None
    terminal_error = (pr_dir / "pr-error.txt").read_text(encoding="utf-8").strip()
    if (
        terminal_error
        not in {
            "remediation-gh-checkout-recovery-required",
            "remediation-existing-branch-not-at-pr-head",
            "command-failed:remediation-original-branch-checkout",
        }
        or fetched.get("status") != "fetched"
        or fetched.get("head_matches_pr_metadata") is not True
        or any(fetched.get(key) != head for key in ("expected_head_oid", "local_head", "remote_ref"))
        or not isinstance(diagnostic, dict)
        or failure.get("command") != f"gh pr checkout {pr['url']}"
        or diagnostic.get("label") != "local-pr-checkout"
        or diagnostic.get("failure_class") != "github-command-failed"
        or diagnostic.get("failure_reason")
        not in {"unclassified", "local-branch-in-other-worktree", "local-branch-exists"}
    ):
        raise ValueError("pr-owner-requires-fetched-head-and-checkout-failure")
    caller = Path(_git("rev-parse", "--show-toplevel")).resolve()
    _, common = _checkout_directories(caller)
    branches = {f"branch refs/heads/{branch}"}
    if pr.get("isCrossRepository") is True:
        branches.add(f"branch refs/heads/{repository.partition('/')[0]}/{branch}")
    owners = []
    for entry in _git("worktree", "list", "--porcelain", "-z").split("\0\0"):
        location, *fields = entry.split("\0")
        if branches.intersection(fields):
            if any(field.partition(" ")[0] in {"locked", "prunable"} for field in fields):
                raise ValueError("pr-owner-registration-unavailable")
            owners.append(Path(location.removeprefix("worktree ")))
    if len(owners) != 1:
        raise ValueError("no-unique-pr-worktree-owner")
    owner = owners[0].resolve(strict=True)
    admin, owner_common = _checkout_directories(owner)
    if owner == caller or owner_common != common or workspace_namespace(owner) or workspace_namespace(common):
        raise ValueError("no-separate-pr-worktree-owner")
    git = partial(_git, cwd=owner)
    local_branch = git("branch", "--show-current")
    if (
        f"branch refs/heads/{local_branch}" not in branches
        or diagnostic.get("checkout_branch", local_branch) != local_branch
    ):
        raise ValueError("pr-owner-branch-changed")
    verify_pr_tracking(git, local_branch, branch, repository, pr["url"], pr["number"], allow_pull_ref=True)
    if git(
        "--no-optional-locks",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        "status",
        "--porcelain",
        "--untracked-files=all",
        "--ignore-submodules=none",
    ) or not pr_worktree_index_unmasked(owner):
        raise ValueError("pr-owner-has-local-changes")
    if any(
        (admin / operation).exists()
        for operation in (
            "MERGE_HEAD",
            "CHERRY_PICK_HEAD",
            "REVERT_HEAD",
            "rebase-merge",
            "rebase-apply",
            "sequencer",
            "BISECT_START",
        )
    ):
        raise ValueError("pr-owner-has-active-operation")
    local_head = git("rev-parse", "HEAD")
    if git("merge-base", local_head, head) != local_head:
        raise ValueError("pr-owner-has-local-only-commits")
    record = {
        "schema_version": 1,
        "invoking_checkout": caller.as_posix(),
        "git_common_dir": common.as_posix(),
        "initial_head": _git("rev-parse", "HEAD"),
        "worktree": owner.as_posix(),
    }
    if receipt is not None:
        # Borrow the delivery checkout. Only the allocator can issue a cleanup ownership marker.
        with receipt.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(record, indent=2) + "\n")
    return {**record, "branch": local_branch, "local_head": local_head, "needs_update": local_head != head}


def create_worktree(receipt: Path) -> dict[str, Any]:
    """Create a linked detached worktree for native PR checkout while preserving the invoking branch and index."""
    caller = Path(_git("rev-parse", "--show-toplevel")).resolve()
    common = Path(_git("rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    if workspace_namespace(common) is not None or workspace_namespace(caller) is not None:
        raise ValueError("retained-review-repository:use-original-checkout")
    head = _git("rev-parse", "--verify", "HEAD")
    # Keep the new checkout outside the run directory so canonical PR report promotion cannot move it.
    parent = receipt.parent.resolve(strict=True).parent
    if parent.is_relative_to(common) or (
        parent.is_relative_to(caller)
        and (parent == caller or not _git("check-ignore", "--", str(parent), optional=True))
    ):
        parent = Path(tempfile.gettempdir()).resolve()
        if parent.is_relative_to(caller) or parent.is_relative_to(common):
            raise ValueError("no-isolated-remediation-worktree-location")
    if receipt.is_symlink():
        raise ValueError("invalid-remediation-worktree-locator")
    fresh = not receipt.exists()
    if not fresh:
        record = _read(receipt)
        destination = record.get("worktree")
        if (
            record.get("schema_version") != 1
            or record.get("invoking_checkout") != caller.as_posix()
            or record.get("git_common_dir") != common.as_posix()
            or record.get("initial_head") != head
            or not isinstance(destination, str)
        ):
            raise ValueError("remediation-worktree-source-mismatch")
        directory = Path(destination)
        # Never claim an arbitrary directory named by a changed or partially written receipt.
        if (
            directory != directory.resolve()
            or directory.parent.parent != parent
            or not directory.parent.name.startswith("codex-remediate-")
            or not directory.parent.is_dir()
            or directory.name != "worktree"
        ):
            raise ValueError("invalid-remediation-worktree-location")
        # A prunable registration can survive a missing directory. Preserve it for diagnosis.
        registered = False
        for entry in _git("worktree", "list", "--porcelain", "-z").split("\0\0"):
            location, *attributes = entry.split("\0")
            if location.startswith("worktree ") and Path(location.removeprefix("worktree ")) == directory:
                registered = True
                if not directory.is_dir() or any(
                    field.partition(" ")[0] in {"locked", "prunable"} for field in attributes
                ):
                    raise ValueError("remediation-worktree-registration-unavailable")
                break
        if not registered and directory.exists():
            raise ValueError("remediation-worktree-location-occupied")
        if any(
            (receipt.parent / name).exists()
            for name in ("pr/local-checkout.json", "remediation-branch.json", "remediation-branch-recovered.json")
        ):
            raise ValueError("remediation-worktree-collected:resume-verified-pr-receipts")
    else:
        directory = Path(tempfile.mkdtemp(prefix="codex-remediate-", dir=parent)) / "worktree"
        record = {
            "schema_version": 1,
            "invoking_checkout": caller.as_posix(),
            "git_common_dir": common.as_posix(),
            "initial_head": head,
            "worktree": directory.as_posix(),
        }
        with receipt.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, indent=2) + "\n")
        registered = False
    if not registered:
        _git("worktree", "add", "--detach", str(directory), head)
    if (
        Path(_git("-C", str(directory), "rev-parse", "--show-toplevel")).resolve() != directory
        or Path(_git("-C", str(directory), "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
        != common
    ):
        raise ValueError("remediation-worktree-verification-failed")
    if _git("-C", str(directory), "branch", "--show-current"):
        raise ValueError("remediation-worktree-attached:resume-verified-pr-receipts")
    if _git("-C", str(directory), "rev-parse", "HEAD") != head:
        raise ValueError("remediation-worktree-head-mismatch")
    if registered and not pr_worktree_is_clean(directory):
        raise ValueError("remediation-worktree-has-local-changes")
    # Only this successful, fresh add may mint cleanup authority. Never upgrade a retained locator or registration.
    if fresh and receipt.name == "remediation-worktree.json":
        admin = Path(_git("-C", str(directory), "rev-parse", "--absolute-git-dir")).resolve(strict=True)
        if admin.parent != common / "worktrees":
            raise ValueError("remediation-worktree-registration-unavailable")
        run_identity = receipt.parent.stat()
        ownership = {
            "schema_version": 1,
            "git_dir": admin.as_posix(),
            "token": secrets.token_hex(16),
            "locator_sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
            # The provisional run may be renamed as one directory into its canonical PR sequence.
            "run_directory_id": [run_identity.st_dev, run_identity.st_ino],
        }
        encoded = json.dumps(ownership, sort_keys=True).encode() + b"\n"
        for path in (admin / "codex-remediation-owner", receipt.with_name("remediation-worktree-owner.json")):
            with path.open("xb") as stream:
                stream.write(encoded)
    return record


def _read(path: Path) -> dict[str, Any]:
    """Read an object-shaped local source receipt."""
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("invalid-receipt-object")
    return value


def _head(expected: str) -> None:
    """Require the caller's previously recorded full revision before continuing."""
    if not re.fullmatch(r"[0-9a-f]{40}", expected) or _git("rev-parse", "HEAD") != expected:
        raise ValueError("head-mismatch")


def _collected_revision(pr: dict[str, Any], checkout: dict[str, Any]) -> tuple[int, str, str]:
    """Require the collector's verified local checkout to agree with the PR identity and revision."""
    number, head, url = pr.get("number"), pr.get("headRefOid"), pr.get("url")
    if (
        type(number) is not int
        or number <= 0
        or not isinstance(head, str)
        or not isinstance(url, str)
        or not url.endswith(f"/pull/{number}")
        or checkout.get("pr_number") != number
        or checkout.get("pr_url") != url
        or checkout.get("local_head") != head
        or checkout.get("expected_head") != head
        or checkout.get("head_matches_pr") is not True
        or checkout.get("diff_source") != "verified-local-checkout"
    ):
        raise ValueError("unverified-pr-source")
    return number, head, url


def _destination(
    branch: str,
    head_ref: str,
    head_repository: str,
    pr_url: str,
    pr_number: int,
    *,
    allow_pull_ref: bool = False,
    pr_dir: Path | None = None,
    initial_head: str = "",
) -> None:
    """Verify source tracking or the exact original native branch on its selected same-repository remote."""
    try:
        verify_pr_tracking(_git, branch, head_ref, head_repository, pr_url, pr_number, allow_pull_ref=allow_pull_ref)
        return
    except ValueError as error:
        if (
            str(error) != "pr-upstream-mismatch:stop-without-changing-tracking"
            or pr_dir is None
            or branch != head_ref
            or "/".join(_github_remote_identity(pr_url.rsplit("/pull/", 1)[0]) or ()).casefold()
            != head_repository.casefold()
        ):
            raise

    # Native checkout can leave an existing branch tracking the target. Rebuild the source proof
    # from the collector on every check, including after local commits advance the attached branch.
    pr = _read(pr_dir / "pr.json")
    checkout = _read(pr_dir / "local-checkout.json")
    base_ref = pr.get("baseRefName")
    if (
        _collected_revision(pr, checkout) != (pr_number, initial_head, pr_url)
        or pr.get("headRefName") != head_ref
        or _head_repository(pr).casefold() != head_repository.casefold()
        or pr.get("isCrossRepository") is not False
        or not isinstance(base_ref, str)
        or not base_ref
        or checkout.get("status") != "checked-out"
        or checkout.get("checkout_mode") != "remediate"
        or checkout.get("checkout_method") != "gh-pr-checkout"
        or checkout.get("command") != f"gh pr checkout {pr_url}"
        or checkout.get("gh_checkout_failure") is not None
        or checkout.get("verified_same_repository") is not True
        or checkout.get("pr_head_fetch_artifact") != "pr-head-fetch.json"
        or checkout.get("local_branch") != branch
        or checkout.get("worktree") != Path(_git("rev-parse", "--show-toplevel")).resolve().as_posix()
    ):
        raise ValueError("unverified-native-same-repository-source")
    selection = _read(pr_dir / "remote-selection.json")
    remote, selected_url = selection.get("remote"), selection.get("remote_url")
    if (
        not remote
        or not isinstance(selected_url, str)
        or "/".join(_github_remote_identity(selected_url) or ()).casefold() != head_repository.casefold()
        or remote not in _git("remote").splitlines()
        or remote != _git("config", "--get", f"branch.{branch}.remote")
    ):
        raise ValueError("pr-remote-mismatch:stop-without-changing-tracking")
    verify_pr_tracking(_git, branch, base_ref, head_repository, pr_url, pr_number)
    fetched = _read(pr_dir / "pr-head-fetch.json")
    if (
        fetched.get("status") != "fetched"
        or fetched.get("remote") != remote
        or fetched.get("source_ref") != head_ref
        or fetched.get("head_ref") != head_ref
        or fetched.get("remote_ref") != initial_head
        or fetched.get("local_head") != initial_head
        or fetched.get("expected_head_oid") != initial_head
        or fetched.get("head_matches_pr_metadata") is not True
    ):
        raise ValueError("unverified-native-pr-head-fetch")
    # A symbolic remote-tracking ref could point back to the local branch, making the proof circular.
    source_ref = f"refs/remotes/{remote}/{head_ref}"
    direct_ref = _git("for-each-ref", "--format=%(refname) %(objectname) %(symref)", "--", source_ref)
    if direct_ref != f"{source_ref} {initial_head}":
        raise ValueError("unverified-native-pr-remote-tracking-ref")


def check(receipt: Path, expected_head: str, pr_dir: Path | None = None) -> dict[str, Any]:
    """Verify the recorded worktree and destination without switching or repairing it."""
    record = _read(receipt)
    _verify_record(record, expected_head, pr_dir or receipt.parent / "pr")
    return record


def _verify_record(record: dict[str, Any], expected_head: str, pr_dir: Path | None = None) -> None:
    """Check live branch identity and ancestry for prepared or recovered evidence."""
    if record.get("schema_version") != 2 or record.get("status") != "prepared":
        raise ValueError("invalid-branch-receipt:preserve-local-work-and-use-recover-for-schema-1")
    worktree = Path(_git("rev-parse", "--show-toplevel")).resolve()
    if record.get("worktree") != worktree.as_posix():
        raise ValueError("worktree-mismatch")
    if not pr_worktree_index_unmasked(worktree):
        raise ValueError("remediation-worktree-index-masks-local-state")
    branch = _git("branch", "--show-current")
    if not branch or branch != record.get("branch"):
        raise ValueError("branch-mismatch")
    _head(expected_head)
    initial = record.get("initial_head")
    if not isinstance(initial, str) or not re.fullmatch(r"[0-9a-f]{40}", initial):
        raise ValueError("invalid-initial-head")
    _git("merge-base", "--is-ancestor", initial, expected_head)
    head_ref, head_repository = record.get("head_ref"), record.get("head_repository")
    if not isinstance(head_ref, str) or not isinstance(head_repository, str):
        raise ValueError("invalid-pr-destination")
    _destination(
        branch,
        head_ref,
        head_repository,
        record.get("pr_url"),
        record.get("pr_number"),
        allow_pull_ref="recovery" not in record,
        pr_dir=pr_dir if "recovery" not in record else None,
        initial_head=initial,
    )


def recover(legacy_receipt: Path, pr_dir: Path, receipt: Path, expected_head: str) -> dict[str, Any]:
    """Verify a legacy run's current PR branch and write separate continuation evidence.

    Retained PR metadata must agree with the original receipt and collected checkout. The caller supplies the last
    recorded authorized revision, including local integration or commits. Recovery never replays checkout or rewrites
    the historical receipt.
    """
    if receipt.exists() or receipt.is_symlink():
        raise ValueError("receipt-already-exists:check-recorded-branch-before-resuming")
    legacy_bytes = legacy_receipt.read_bytes()
    legacy = json.loads(legacy_bytes)
    pr = _read(pr_dir / "pr.json")
    checkout = _read(pr_dir / "local-checkout.json")
    number, head, url = pr.get("number"), pr.get("headRefOid"), pr.get("url")
    if (
        not isinstance(legacy, dict)
        or type(legacy.get("schema_version")) is not int
        or legacy.get("schema_version") != 1
        or legacy.get("status") != "prepared"
        or not isinstance(legacy.get("branch"), str)
        or not legacy["branch"]
        or type(number) is not int
        or number <= 0
        or not isinstance(url, str)
        or not re.fullmatch(rf"(?i:https://github\.com)/[^/]+/[^/]+/pull/{number}", url)
        or legacy.get("pr_number") != number
        or legacy.get("pr_url") != url
        or legacy.get("initial_head") != head
        or checkout.get("pr_number") != number
        or checkout.get("pr_url") != url
        or checkout.get("local_head") != head
        or checkout.get("expected_head") != head
        or checkout.get("head_matches_pr") is not True
        or checkout.get("diff_source") != "verified-local-checkout"
        or checkout.get("worktree") != legacy.get("worktree")
    ):
        raise ValueError("unverified-legacy-pr-source")
    # Legacy branch names describe history; only the live original PR branch can resume.
    record = {
        "schema_version": 2,
        "status": "prepared",
        "pr_number": number,
        "pr_url": url,
        "initial_head": head,
        "branch": pr.get("headRefName"),
        "worktree": legacy.get("worktree"),
        "head_ref": pr.get("headRefName"),
        "head_repository": _head_repository(pr),
        "recovery": {
            "legacy_receipt": legacy_receipt.resolve().as_posix(),
            "legacy_sha256": hashlib.sha256(legacy_bytes).hexdigest(),
            "legacy_branch": legacy["branch"],
            "expected_head": expected_head,
        },
    }
    _verify_record(record, expected_head)
    if _git("ls-files", "--unmerged"):
        raise ValueError("unresolved-index")
    for operation in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply"):
        if Path(_git("rev-parse", "--git-path", operation)).exists():
            raise ValueError(f"operation-in-progress:{operation}")
    receipt.parent.mkdir(parents=True, exist_ok=True)
    with receipt.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, indent=2) + "\n")
    return record


def prepare(pr_dir: Path, receipt: Path) -> dict[str, Any]:
    """Record a verified PR checkout without changing branches or tracking."""
    if receipt.exists() or receipt.is_symlink():
        raise ValueError("receipt-already-exists:check-recorded-branch-before-resuming")
    pr = _read(pr_dir / "pr.json")
    checkout = _read(pr_dir / "local-checkout.json")
    number, head, url = _collected_revision(pr, checkout)
    head_ref, head_repository = pr.get("headRefName"), _head_repository(pr)
    if not isinstance(head_ref, str):
        raise ValueError("invalid-pr-destination")
    if checkout.get("checkout_mode") != "remediate":
        raise ValueError("remediation-requires-remediation-checkout")
    if checkout.get("checkout_method") == "git-original-branch-fallback":
        base_repository = _github_remote_identity(url.rsplit("/pull/", 1)[0])
        remote = _read(pr_dir / "remote-selection.json").get("remote")
        commands = (
            f"git checkout --no-guess {head_ref}",
            f"git checkout --track -b {head_ref} {remote}/{head_ref}",
        )
        # A direct branch checkout is authorized only for the positively identified base repository.
        if (
            not base_repository
            or pr.get("isCrossRepository") is not False
            or "/".join(base_repository).casefold() != head_repository.casefold()
            or not isinstance(remote, str)
            or not remote
            or checkout.get("local_branch") != head_ref
            or checkout.get("command") not in commands
            or not isinstance(checkout.get("gh_checkout_failure"), dict)
            or not checkout["gh_checkout_failure"].get("code")
            or checkout["gh_checkout_failure"].get("command") != f"gh pr checkout {url}"
        ):
            raise ValueError("unverified-same-repo-checkout-fallback")
    elif checkout.get("checkout_method") != "gh-pr-checkout" or checkout.get("command") != f"gh pr checkout {url}":
        raise ValueError("remediation-requires-gh-or-verified-same-repo-checkout")
    _head(head)
    worktree = Path(_git("rev-parse", "--show-toplevel")).resolve().as_posix()
    if checkout.get("worktree") != worktree:
        raise ValueError("collected-worktree-mismatch")
    if not pr_worktree_index_unmasked(Path(worktree)):
        raise ValueError("remediation-worktree-index-masks-local-state")
    locators = [
        path
        for path in (pr_dir.parent / "remediation-worktree.json", pr_dir.parent / "recovery-worktree.json")
        if path.exists() or path.is_symlink()
    ]
    if len(locators) > 1:
        raise ValueError("conflicting-remediation-worktree-locators")
    if locators:
        locator = locators[0]
        if locator.is_symlink() or not locator.is_file():
            raise ValueError("invalid-remediation-worktree-locator")
        selected = _read(locator)
        caller = selected.get("invoking_checkout")
        if (
            selected.get("schema_version") != 1
            or selected.get("worktree") != worktree
            or not isinstance(caller, str)
            or caller == worktree
            or not Path(caller).is_absolute()
            or Path(_git("-C", caller, "rev-parse", "--show-toplevel")).resolve().as_posix() != caller
        ):
            raise ValueError("remediation-worktree-mismatch")
        # A checkout at the right path must still belong to the invoking repository after report promotion.
        for directory in (caller, worktree):
            common = _git("-C", directory, "rev-parse", "--path-format=absolute", "--git-common-dir")
            if Path(common).resolve().as_posix() != selected.get("git_common_dir"):
                raise ValueError("remediation-worktree-mismatch")
    if _git("ls-files", "--unmerged"):
        raise ValueError("unresolved-index")
    for operation in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply"):
        if Path(_git("rev-parse", "--git-path", operation)).exists():
            raise ValueError(f"operation-in-progress:{operation}")
    branch = _git("branch", "--show-current")
    if not branch or branch != checkout.get("local_branch"):
        raise ValueError("branch-mismatch-after-pr-checkout")
    _destination(
        branch,
        head_ref,
        head_repository,
        url,
        number,
        allow_pull_ref=checkout.get("checkout_method") == "gh-pr-checkout",
        pr_dir=pr_dir if checkout.get("checkout_method") == "gh-pr-checkout" else None,
        initial_head=head,
    )
    record = {
        "schema_version": 2,
        "status": "prepared",
        "pr_number": number,
        "pr_url": url,
        "initial_head": head,
        "branch": branch,
        "worktree": worktree,
        "head_ref": head_ref,
        "head_repository": head_repository,
    }
    receipt.parent.mkdir(parents=True, exist_ok=True)
    with receipt.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, indent=2) + "\n")
    return record


def main() -> int:
    """Prepare, recover, or check a remediation branch with explicit failure diagnostics."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    parser.add_argument("action", choices=("prepare", "check", "recover", "worktree", "locate-owner"))
    parser.add_argument("--receipt", type=Path)
    parser.add_argument(
        "--pr-dir", type=Path, help="Collector evidence directory (check defaults to the receipt's sibling pr/)."
    )
    parser.add_argument("--expected-head")
    parser.add_argument("--legacy-receipt", type=Path)
    args = parser.parse_args()
    try:
        if args.action == "locate-owner":
            if args.pr_dir is None or args.expected_head is not None or args.legacy_receipt is not None:
                raise ValueError("locate-owner-requires-pr-dir")
            record = locate_owner(args.pr_dir, args.receipt)
        elif args.receipt is None:
            raise ValueError("receipt-required")
        elif args.action == "worktree":
            if args.pr_dir is not None or args.expected_head is not None or args.legacy_receipt is not None:
                raise ValueError("worktree-requires-receipt-only")
            record = create_worktree(args.receipt)
        elif args.action == "prepare":
            if args.pr_dir is None or args.expected_head is not None or args.legacy_receipt is not None:
                raise ValueError("prepare-requires-pr-dir-only")
            record = prepare(args.pr_dir, args.receipt)
        elif args.action == "recover":
            if args.pr_dir is None or args.expected_head is None or args.legacy_receipt is None:
                raise ValueError("recover-requires-pr-dir-legacy-receipt-and-expected-head")
            record = recover(args.legacy_receipt, args.pr_dir, args.receipt, args.expected_head)
        else:
            if args.expected_head is None or args.legacy_receipt is not None:
                raise ValueError("check-requires-expected-head")
            record = check(args.receipt, args.expected_head, args.pr_dir)
    except (CollectionError, KeyError, OSError, TypeError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"remediation-branch: {error}", file=sys.stderr)
        return 2
    print(json.dumps(record, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

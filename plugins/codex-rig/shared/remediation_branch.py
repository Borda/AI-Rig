#!/usr/bin/env python3
"""Record the verified original PR branch for subsequent remediation commits.

## Purpose

    Bind the collector's successful original-branch checkout to the branch used for
    editing, target integration, and optional commits. Equal commit hashes alone
    do not establish the original pull request's publication destination.
## Scope

    Local Git queries only. Preparation records the existing branch and verifies
    its tracking destination; it never creates or switches branches, sets tracking,
    or repairs a changed revision. Only the explicitly requested receipt is written.
## Usage

    Run ``python remediation_branch.py prepare --pr-dir <collected-pr-directory>
    --receipt <branch.json>`` immediately after collection. Run ``check`` with that
    receipt and ``--expected-head <recorded-revision>`` before integration or each
    optional commit, and after a commit using its independently observed revision.
    For a legacy run already on its original PR branch, use ``recover --legacy-receipt
    <old.json> --pr-dir <retained-pr-directory> --receipt <new.json> --expected-head
    <last-authorized-revision>`` before continuing with ordinary checks.
## Outputs

    Preparation prints and exclusively writes a JSON receipt binding the PR,
    worktree, initial revision, PR destination, and attached branch. Check prints that receipt
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
import subprocess
import sys
from pathlib import Path
from typing import Any

from collect_pr import _github_remote_identity, _head_repository


def _git(*arguments: str, optional: bool = False) -> str:
    """Run a local Git query, allowing absent optional configuration only."""
    result = subprocess.run(["git", *arguments], capture_output=True, text=True, check=False, timeout=30)
    if optional and result.returncode == 1 and arguments[:2] == ("config", "--get"):
        return ""
    if result.returncode:
        raise ValueError(f"git-command-failed:{arguments[0]}:exit-{result.returncode}")
    return result.stdout.strip()


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


def _destination(branch: str, head_ref: str, head_repository: str) -> None:
    """Verify tracking and effective push remotes still identify the original PR head."""
    expected = _github_remote_identity(f"https://github.com/{head_repository}.git")
    if expected is None or not head_ref:
        raise ValueError("invalid-pr-destination")
    if branch != head_ref:
        raise ValueError("pr-branch-name-mismatch:default-push-destination-unverified")
    _git("check-ref-format", f"refs/heads/{head_ref}")
    merge = _git("config", "--get", f"branch.{branch}.merge", optional=True)
    remote = _git("config", "--get", f"branch.{branch}.remote", optional=True)
    if merge != f"refs/heads/{head_ref}" or not remote:
        raise ValueError("pr-upstream-mismatch:stop-without-changing-tracking")
    push_remote = (
        _git("config", "--get", f"branch.{branch}.pushRemote", optional=True)
        or _git("config", "--get", "remote.pushDefault", optional=True)
        or remote
    )
    remotes = _git("remote").splitlines()
    # gh can configure a fork URL directly instead of adding a named local remote.
    for selected, direction in ((remote, []), (push_remote, ["--push"])):
        urls = (
            _git("remote", "get-url", *direction, "--all", selected).splitlines() if selected in remotes else [selected]
        )
        if not urls or any(
            tuple(part.casefold() for part in (_github_remote_identity(url) or ()))
            != tuple(part.casefold() for part in expected)
            for url in urls
        ):
            raise ValueError("pr-remote-mismatch:stop-without-changing-tracking")
    if push_remote in remotes and _git("config", "--get", f"remote.{push_remote}.push", optional=True):
        raise ValueError("custom-push-refspec:publication-destination-unverified")


def check(receipt: Path, expected_head: str) -> dict[str, Any]:
    """Verify the recorded worktree and destination without switching or repairing it."""
    record = _read(receipt)
    _verify_record(record, expected_head)
    return record


def _verify_record(record: dict[str, Any], expected_head: str) -> None:
    """Check live branch identity and ancestry for prepared or recovered evidence."""
    if record.get("schema_version") != 2 or record.get("status") != "prepared":
        raise ValueError("invalid-branch-receipt:preserve-local-work-and-use-recover-for-schema-1")
    if record.get("worktree") != Path(_git("rev-parse", "--show-toplevel")).resolve().as_posix():
        raise ValueError("worktree-mismatch")
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
    _destination(branch, head_ref, head_repository)


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
        or not re.fullmatch(rf"https://github\.com/[^/]+/[^/]+/pull/{number}", url)
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
    if _git("ls-files", "--unmerged"):
        raise ValueError("unresolved-index")
    for operation in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply"):
        if Path(_git("rev-parse", "--git-path", operation)).exists():
            raise ValueError(f"operation-in-progress:{operation}")
    branch = _git("branch", "--show-current")
    if not branch or branch != checkout.get("local_branch"):
        raise ValueError("branch-mismatch-after-pr-checkout")
    _destination(branch, head_ref, head_repository)
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
    parser.add_argument("action", choices=("prepare", "check", "recover"))
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--pr-dir", type=Path)
    parser.add_argument("--expected-head")
    parser.add_argument("--legacy-receipt", type=Path)
    args = parser.parse_args()
    try:
        if args.action == "prepare":
            if args.pr_dir is None or args.expected_head is not None or args.legacy_receipt is not None:
                raise ValueError("prepare-requires-pr-dir-only")
            record = prepare(args.pr_dir, args.receipt)
        elif args.action == "recover":
            if args.pr_dir is None or args.expected_head is None or args.legacy_receipt is None:
                raise ValueError("recover-requires-pr-dir-legacy-receipt-and-expected-head")
            record = recover(args.legacy_receipt, args.pr_dir, args.receipt, args.expected_head)
        else:
            if args.expected_head is None or args.pr_dir is not None or args.legacy_receipt is not None:
                raise ValueError("check-requires-expected-head-only")
            record = check(args.receipt, args.expected_head)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"remediation-branch: {error}", file=sys.stderr)
        return 2
    print(json.dumps(record, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

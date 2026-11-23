#!/usr/bin/env python3
"""Bind a review parent's safety judgment to a specific pull request and its verified execution inputs.

## Purpose

Code Review records its parent's explicit safe, unsafe or unassessed judgment in the authoritative review result. Code
Remediate can reuse a safe judgment only for the same PR, head and base, with a fresh whole-tree scan and clean
checkout. A static scan describes detected risk. The review outcome, findings count and prose supply no authority.

## Scope

The review producer measures the selected Git tree directly with the shipped guard. Reuse requires the promoted result,
both existing completion validators and either the original producer-held digest or a verified cleanup witness that
bound that digest before removal. The consumer repeats the measurement in the current checkout. An explicit sandbox
request wins immediately.

## Usage

Call ``check --review-run RUN --pr-url URL --head SHA --base SHA --checkout PATH``, optionally with ``--review-result-
sha256 DIGEST`` from trusted producer context or ``--sandbox``. Pass current PR identity from fresh collector evidence
and any explicit producer validation coordinates from admission. For fresh PR reviews and in-place remediation following
a review, use ``report-root --checkout PATH --pr-url URL`` for report allocation and promotion. Call the safety check
immediately before each executable operation, with all writers stopped in that interval.

## Outputs

Print one JSON decision with route and reason. Ordinary means the plugin-specific sandbox can be omitted for that exact
source. Inspect asks the remediation parent to assess the command and its current inputs. Sandbox retains an explicit
confinement request. All routes preserve host permissions and the command's existing authorization.

## Failure

Unavailable verification, untrusted or stale inputs, unreadable evidence, source edits and omitted or nonempty ignored
inputs require current command inspection. The helper reads the evidence without running project code or granting host
permissions.

## Used by

The shared result writer and review finalizer produce the record. Code Remediate consumes it before executing gates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.pycache_prefix = str(Path(__file__).resolve())
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import review_worktree_replay  # noqa: E402
import untrusted_code_guard as guard  # noqa: E402
from create_run import PR_URL_PATTERN, same_pr_url  # noqa: E402
from review_workspace import (  # noqa: E402
    _check_path,
    _checkout_directories,
    _git_environment,
    checkout_report_root,
    prepare_workspace,
    workspace_caller,
)

SCHEMA = 1
ASSESSMENTS = ("safe", "unsafe", "unassessed")
_OID = re.compile(r"(?:[a-f0-9]{40}|[a-f0-9]{64})")
_FINDER = Path(__file__).with_name("find-review-report.py")


def _identity(run: Path) -> dict[str, str]:
    """Read the canonical PR and full head/base identities that the collector recorded."""
    routing = json.loads((run / "pr-routing.json").read_bytes())
    url, head, base = (routing.get(key) for key in ("pr_url", "head_oid", "base_oid"))
    if not all(isinstance(item, str) for item in (url, head, base)) or not (
        PR_URL_PATTERN.fullmatch(url) and _OID.fullmatch(head) and _OID.fullmatch(base)
    ):
        raise ValueError("review-pr-identity-unavailable")
    return {"pr_url": url, "head": head, "base": base}


def _inspect(checkout: Path, head: str) -> dict[str, Any]:
    """Measure frozen Git bytes and check the actual checkout without executing its code or configuration."""
    repo = str(checkout.resolve(strict=True))
    if not _OID.fullmatch(head) or guard._resolve(repo, "HEAD") != head:
        raise ValueError("current-head-mismatch")
    top = Path(os.fsdecode(guard._git(repo, "rev-parse", "--show-toplevel").rstrip(b"\r\n"))).resolve(strict=True)
    if top != Path(repo):
        raise ValueError("checkout-is-not-repository-root")
    scan = guard.build_report(repo, head, None, True)
    # Direct execution also sees ignored files, special files and linked directories. Verify the actual directory
    # entries before applying the byte/mode check shared with the guard's disposable-copy execution path.
    tracked = set(guard._tree_entries(repo, head))
    parents = {parent.as_posix() for name in tracked for parent in Path(name).parents}

    def unreadable(error: OSError) -> None:
        """Reject a checkout whose directories could not be enumerated."""
        raise error

    for root, directories, files in os.walk(repo, onerror=unreadable):
        if root == repo and ".git" in directories:
            directories.remove(".git")
        for name in directories:
            path = Path(root, name)
            if path.relative_to(repo).as_posix() not in parents or path.resolve(strict=True) != path:
                raise ValueError("unreviewed-directory")
        for name in files:
            if root == repo and name == ".git":
                continue
            path = Path(root, name)
            if (
                path.relative_to(repo).as_posix() not in tracked
                or not path.is_file()
                or path.resolve(strict=True) != path
            ):
                raise ValueError("unreviewed-file")

    if guard._git(repo, "diff-index", "--cached", "--name-only", "-z", head, "--"):
        raise ValueError("index-differs-from-reviewed-head")
    guard.require_exact_head(repo, scan, Path(repo), guard.listed_files(repo))
    if guard._resolve(repo, "HEAD") != head:
        raise ValueError("current-head-changed")
    return {
        "whole_tree": True,
        "risk_level": scan["risk"]["level"],
        "digest": scan["digest"],
        "skipped": len(scan["skipped"]),
    }


def _scan(checkout: Path, head: str) -> dict[str, Any]:
    """Run the trusted scanner in a clean Git environment isolated from inherited repository-selection overrides."""
    done = subprocess.run(  # noqa: S603 - fixed installed helper, positional paths, no shell
        [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "_inspect", str(checkout), head],
        env=_git_environment() | {"GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0"},
        cwd=Path(__file__).resolve().parent,
        capture_output=True,
        check=False,
    )
    if done.returncode:
        raise ValueError("source-scan-or-clean-check-failed")
    return json.loads(done.stdout)


def review_record(run: Path, metadata: dict[str, Any], assessment: str) -> dict[str, Any]:
    """Produce the parent's selected assessment, binding positive claims to verified PR source and a new scan."""
    if assessment not in ASSESSMENTS:
        raise ValueError("invalid-code-safety-assessment")
    record: dict[str, Any] = {"schema_version": SCHEMA, "assessment": assessment}
    try:
        if metadata.get("scope") != "pr" or metadata.get("review_status") in {"closed", "unavailable"}:
            if assessment == "safe":
                raise ValueError("safe-requires-assessed-pr")
            return record
        identity = _identity(run)
        record.update(identity)
        if assessment == "unassessed":
            return record
        checkout = json.loads((run / "local-checkout.json").read_bytes())
        if not same_pr_url(identity["pr_url"], checkout.get("pr_url")) or (
            checkout.get("diff_base_oid"),
            checkout.get("diff_head_oid"),
            checkout.get("local_head"),
        ) != (identity["base"], identity["head"], identity["head"]):
            raise ValueError("review-checkout-identity-mismatch")
        scan = _scan(Path(checkout["worktree"]), identity["head"])
        record["scan"] = scan
        if assessment == "safe" and (scan["risk_level"] != "none" or scan["skipped"]):
            raise ValueError("whole-tree scan has findings or unread inputs")
    except (OSError, ValueError, TypeError, AttributeError, KeyError, RecursionError):
        if assessment == "safe":
            record.update(
                assessment="unassessed",
                requested_assessment="safe",
                reason="The verified PR identity, complete risk-none scan or clean execution inputs could not be established.",
            )
    return record


def _git_common(checkout: Path) -> Path:
    """Resolve a checkout's Git common directory independently of paths asserted in report metadata."""
    output = review_worktree_replay._git(checkout, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout
    return Path(os.fsdecode(output.rstrip(b"\r\n"))).resolve(strict=True)


def pr_report_root(checkout: Path, url: str) -> Path:
    """Prepare ordinary report storage for PR review and remediation under the invoking checkout’s Git directory."""
    caller = prepare_workspace(checkout, url)
    root = checkout_report_root(caller) or caller / ".reports" / "codex"
    for skill in ("code-review", "code-remediate"):
        _check_path(root / skill, root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    _check_path(root, root)
    return root


def _archived_review(run: Path, result: Path, checkout: Path, url: str, digest: str) -> bool:
    """Require a verified cleanup witness for a review produced on behalf of this checkout’s repository."""
    repository = review_worktree_replay._report_repository(run)
    if repository is None:
        return False
    caller = workspace_caller(repository, url) or repository
    _, common = _checkout_directories(checkout)
    if common != _git_common(caller):
        return False
    return review_worktree_replay.verified_archive(run, result, producer_result_sha256=digest) is not None


def decision(
    run: Path,
    url: str,
    head: str,
    base: str,
    checkout: Path,
    *,
    expect: str | None,
    sandbox: bool,
    codex_home: Path | None = None,
    parent_thread_id: str | None = None,
) -> dict[str, str]:
    """Decide whether this exact source can use ordinary execution, preserving any explicit confinement request."""
    if sandbox:
        return {"route": "sandbox", "reason": "explicit-sandbox"}
    try:
        if run.absolute() != run.resolve(strict=True):
            raise ValueError("redirected-review-run")
        checkout = checkout.absolute()
        if checkout != checkout.resolve():
            raise ValueError("redirected-execution-checkout")
        run = run.absolute()
        result = run / "result.json"
        if result.is_symlink() or not result.is_file():
            raise ValueError("review-not-promoted")
        raw = result.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if expect is not None:
            if digest != expect:
                raise ValueError("trusted-result-digest-mismatch")
        elif not _archived_review(run, result, checkout, url, digest):
            raise ValueError("missing-authoritative-result-anchor")
        metadata = json.loads(raw)["metadata"]
        if metadata.get("scope") != "pr" or metadata.get("review_status") in {"closed", "unavailable"}:
            raise ValueError("review-was-not-an-assessed-pr")
        record = metadata["code_safety"]
        if (
            not isinstance(record, dict)
            or type(record.get("schema_version")) is not int
            or record.get("schema_version") != SCHEMA
            or record.get("assessment") != "safe"
        ):
            raise ValueError("review-did-not-assess-source-as-safe")
        identity = _identity(run)
        if (
            not same_pr_url(url, identity["pr_url"])
            or head != identity["head"]
            or base != identity["base"]
            or not all(record.get(key) == value for key, value in identity.items())
        ):
            raise ValueError("review-pr-or-commit-mismatch")
        command = [sys.executable, "-B", str(_FINDER), "--complete-run", str(run)]
        if codex_home is not None:
            command.extend(["--codex-home", str(codex_home)])
        if parent_thread_id is not None:
            command.extend(["--parent-thread-id", parent_thread_id])
        completed = subprocess.run(  # noqa: S603 - packaged completion validates the producer and both artifact contracts
            command,
            env=_git_environment(),
            capture_output=True,
            check=False,
            timeout=300,
        )
        if completed.returncode or result.read_bytes() != raw:
            raise ValueError("review-completion-validation-failed")
        notes = (run / "review-notes.md").read_text(encoding="utf-8").splitlines()
        if notes.count("## Code Safety") != 1 or notes[notes.index("## Code Safety") + 1 :][:2] != [
            "",
            "Code safety: safe",
        ]:
            raise ValueError("review-notes-do-not-confirm-assessment")
        fresh = _scan(checkout, head)
        if fresh["risk_level"] != "none" or fresh["skipped"] or record.get("scan") != fresh:
            raise ValueError("fresh-scan-does-not-confirm-reviewed-inputs")
    except (
        KeyError,
        TypeError,
        AttributeError,
        RecursionError,
        ValueError,
        OSError,
        subprocess.SubprocessError,
        RuntimeError,
    ) as err:
        return {"route": "inspect", "reason": str(err) or type(err).__name__}
    return {"route": "ordinary", "reason": "validated-safe-review-and-fresh-matching-source"}


def main() -> int:
    """Run the selected safety check and print its measured result."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    actions = parser.add_subparsers(dest="action", required=True)
    check = actions.add_parser("check")
    check.add_argument("--review-run", type=Path, required=True)
    check.add_argument("--pr-url", required=True)
    check.add_argument("--head", required=True)
    check.add_argument("--base", required=True)
    check.add_argument("--checkout", type=Path, required=True)
    check.add_argument(
        "--codex-home", type=Path, help="Trusted producer evidence home from completed review admission."
    )
    check.add_argument("--parent-thread-id", help="Trusted producer thread from completed review admission.")
    check.add_argument("--review-result-sha256")
    check.add_argument("--sandbox", action="store_true")
    report_root = actions.add_parser(
        "report-root", help="Prepare report storage for fresh PR review or in-place remediation."
    )
    report_root.add_argument("--checkout", type=Path, required=True)
    report_root.add_argument("--pr-url", required=True)
    inspect = actions.add_parser("_inspect")
    inspect.add_argument("checkout", type=Path)
    inspect.add_argument("head")
    args = parser.parse_args()
    if args.action == "report-root":
        try:
            print(pr_report_root(args.checkout, args.pr_url))
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as err:
            print(str(err), file=sys.stderr)
            return 1
    elif args.action == "_inspect":
        try:
            measured = _inspect(args.checkout, args.head)
        except (OSError, ValueError, guard.GuardError, RuntimeError) as err:
            print(str(err), file=sys.stderr)
            return 1
        print(json.dumps(measured))
    else:
        print(
            json.dumps(
                decision(
                    args.review_run,
                    args.pr_url,
                    args.head,
                    args.base,
                    args.checkout,
                    expect=args.review_result_sha256,
                    sandbox=args.sandbox,
                    codex_home=args.codex_home,
                    parent_thread_id=args.parent_thread_id,
                )
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

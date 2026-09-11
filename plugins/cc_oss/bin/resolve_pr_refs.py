#!/usr/bin/env python
"""resolve_pr_refs.py — resolve the default branch and the PR's head/base/fork metadata before checkout.

Runs the branch-safety pre-check for ``oss:resolve`` Step 4: a PR whose head ref equals the repository's default branch
must never be checked out, because every later commit would then land on the default branch.

The PR fields are fetched once, here, rather than at Step 3b. The human approval between the two steps can take
minutes, and a stale ``headRefOid`` would poison the SHA-first checkout skip that follows. The resulting sentinels are
read by the post-checkout assertion, the Step 10 push gate, and ``conflict-resolution.md``.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_pr_refs.py" --pr "<PR#>"

Sentinels written to ``${TMPDIR:-/tmp}/<name>-${CSID}``:
    resolve-head-ref, resolve-base-ref, resolve-is-cross-repo, resolve-head-repo-owner, resolve-saved-branch,
    resolve-pr-head-oid, resolve-local-sha

Exit codes:
    0 — metadata resolved and persisted
    1 — the default branch is undeterminable, or the PR head ref is the default branch
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Final

_PR_FIELDS: Final = "headRefName,baseRefName,isCrossRepository,headRefOid,headRepositoryOwner"
_HEAD_BRANCH_RE: Final = re.compile(r"HEAD branch:\s*(\S+)")


def _sentinel_path(name: str) -> Path:
    """Build the session-scoped sentinel path for ``name``.

    Args:
        name: Sentinel base name, without the trailing session token.

    Returns:
        Path of the form ``<tmpdir>/<name>-<csid>``.

    Examples:
        >>> _sentinel_path("resolve-head-ref").name.startswith("resolve-head-ref-")
        True
    """
    csid = os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "shared"
    tmpdir = os.environ.get("TMPDIR") or tempfile.gettempdir()
    return Path(tmpdir) / f"{name}-{csid}"


#: Set by ``--dry-run``. Suppresses every sentinel write for the process.
_DRY_RUN = False


def _set_dry_run(enabled: bool) -> None:
    """Enable or disable dry-run mode for this process."""
    global _DRY_RUN  # noqa: PLW0603 — one process-wide switch, set once from argv
    _DRY_RUN = enabled


def _write_sentinel(name: str, value: str) -> None:
    """Write ``value`` plus a trailing newline to the sentinel named ``name``.

    Readers use ``IFS= read -r VAR < file``, which exits non-zero on a file with no final newline and silently falls
    back to its default; ``newline="\\n"`` stops Windows from appending a carriage return inside the value.


    Sentinels are live session state, not scratch output: they are named for the current ``CSID`` and are what the
    skill's later steps and its PreToolUse hooks read. Running this script by hand to inspect its output therefore
    forges state for whatever session is running — one observed case wrote an ``analyse-report-file`` naming a report
    that was never produced, and the resulting hook denial blocked an unrelated question. Pass ``--dry-run`` for any
    invocation that is not a real skill step.

    Args:
        name: Sentinel base name, without the trailing session token.
        value: Payload to persist.
    """
    if _DRY_RUN:
        print(f"[dry-run] would write {name}={value}")
        return
    _sentinel_path(name).write_text(f"{value}\n", encoding="utf-8", newline="\n")


def _run(cmd: list[str], timeout: int) -> str:
    """Run ``cmd`` and return its stripped stdout, or an empty string on any failure.

    Args:
        cmd: Argument vector to execute.
        timeout: Maximum wait in seconds.

    Returns:
        Stripped stdout on success, otherwise an empty string.
    """
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def parse_head_branch(remote_show: str) -> str:
    """Extract the default branch from ``git remote show origin`` output.

    Args:
        remote_show: Raw command output.

    Returns:
        The default branch name, or an empty string when the line is absent.

    Examples:
        >>> parse_head_branch("* remote origin\\n  HEAD branch: main\\n")
        'main'
        >>> parse_head_branch("* remote origin")
        ''
    """
    match = _HEAD_BRANCH_RE.search(remote_show)
    return match.group(1) if match else ""


def default_branch(timeout: int) -> str:
    """Resolve the repository's default branch, local ref first and the network only as a fallback.

    Args:
        timeout: Maximum wait in seconds per subprocess.

    Returns:
        The default branch name, or an empty string when neither source answers.
    """
    ref = _run(["git", "symbolic-ref", "refs/remotes/origin/HEAD"], timeout)
    if ref:
        return re.sub(r"^refs/remotes/origin/", "", ref)
    return parse_head_branch(_run(["git", "remote", "show", "origin"], timeout))


def fetch_pr_meta(pr_number: str, timeout: int) -> dict:
    """Fetch the PR fields needed for checkout safety and the later push gate.

    Args:
        pr_number: PR number or URL accepted by ``gh pr view``.
        timeout: Maximum wait in seconds.

    Returns:
        The decoded JSON object, or an empty dict when the fetch or parse fails.
    """
    raw = _run(["gh", "pr", "view", pr_number, "--json", _PR_FIELDS], timeout)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def head_repo_owner(meta: dict) -> str:
    """Read the fork owner's login from PR metadata.

    Args:
        meta: Decoded ``gh pr view`` payload.

    Returns:
        The owner login, or an empty string when absent.

    Examples:
        >>> head_repo_owner({"headRepositoryOwner": {"login": "contributor"}})
        'contributor'
        >>> head_repo_owner({"headRepositoryOwner": None})
        ''
    """
    owner = meta.get("headRepositoryOwner")
    if not isinstance(owner, dict):
        return ""
    return str(owner.get("login") or "")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument list override for testing. Defaults to ``sys.argv[1:]``.

    Returns:
        Exit code — 0 when the metadata was persisted, 1 on either branch-safety failure.
    """
    parser = argparse.ArgumentParser(
        prog="resolve_pr_refs.py",
        description="Resolve the default branch and PR head/base/fork metadata before checkout.",
    )
    parser.add_argument("--pr", default="", help="PR number or URL accepted by gh pr view.")
    parser.add_argument("--timeout", type=int, default=15, help="Max subprocess wait in seconds (default: 15).")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and print, but write no sentinels — use for any run that is not a real skill step.",
    )
    args = parser.parse_args(argv)
    _set_dry_run(args.dry_run)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]

    base_default = default_branch(args.timeout)
    if not base_default:
        print("! BLOCKED — cannot determine default branch; refusing to proceed")
        return 1

    meta = fetch_pr_meta(args.pr, args.timeout)
    head_ref = str(meta.get("headRefName") or "")
    base_ref = str(meta.get("baseRefName") or "") or base_default
    cross_repo = "true" if meta.get("isCrossRepository") else "false"
    head_oid = str(meta.get("headRefOid") or "")
    if head_ref == base_default:
        print(f"⛔ PR HEAD ref ({head_ref}) equals default branch — refusing to check out and commit on default branch")
        return 1

    saved_branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], args.timeout)
    local_sha = _run(["git", "rev-parse", "HEAD"], args.timeout)
    for name, value in (
        ("resolve-head-ref", head_ref),
        ("resolve-base-ref", base_ref),
        ("resolve-is-cross-repo", cross_repo),
        ("resolve-head-repo-owner", head_repo_owner(meta)),
        ("resolve-saved-branch", saved_branch),
        ("resolve-pr-head-oid", head_oid),
        ("resolve-local-sha", local_sha),
    ):
        _write_sentinel(name, value)

    # Reflog trace (cf. investigate 2026-06-13T11-00-00Z: pr195 alias, opaque state).
    print(
        f"→ Step 4 state: SAVED_BRANCH={saved_branch} PR_HEAD_REF={head_ref} "
        f"PR_HEAD_OID={head_oid or '<empty>'} LOCAL_SHA={local_sha or '<empty>'}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

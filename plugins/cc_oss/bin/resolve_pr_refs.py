#!/usr/bin/env python
"""resolve_pr_refs.py — resolve the default branch and the PR's head/base/fork metadata before checkout.

Runs the branch-safety pre-check for ``oss:resolve`` Step 4: a PR whose head ref equals the repository's default branch
must never be checked out, because every later commit would then land on the default branch.

The PR fields are fetched once, here, rather than at Step 3b. The human approval between the two steps can take
minutes, and a stale ``headRefOid`` would poison the SHA-first checkout skip that follows. The resulting sentinels are
read by the post-checkout assertion, the Step 10 push gate, and ``conflict-resolution.md``.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_pr_refs.py" --pr "<PR#>"
    python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_pr_refs.py" --local-base

``--local-base`` is the no-PR path (report mode, comment dispatch): it publishes only the repository's default branch to
``resolve-base-ref`` so Step 9 has a QA diff range, using the same origin/HEAD-then-``git remote show`` chain as the PR
path.

Sentinels written to ``${TMPDIR:-/tmp}/<name>-${CSID}``, each created fresh with mode 0600 (``_sentinel_io.py``):
    resolve-head-ref, resolve-base-ref, resolve-is-cross-repo, resolve-head-repo-owner, resolve-head-repo-name,
    resolve-saved-branch, resolve-pr-head-oid, resolve-local-sha

Exit codes:
    0 — metadata resolved and persisted
    1 — the default branch is undeterminable, the PR head ref is the default branch, the PR head or base ref is
        not a valid branch name, or ``--pr`` starts with ``-`` (argv-injection guard)
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

#: Sibling import: bin/ is not a package. Putting it on the path resolves the private writer whether this file runs as
#: a script, under the test conftest, or as a doctest module.
_BIN_DIR = str(Path(__file__).resolve().parent)
if _BIN_DIR not in sys.path:
    sys.path.insert(0, _BIN_DIR)

from _sentinel_io import write_private_text  # noqa: E402

#: Comma-separated JSON fields requested from ``gh pr view`` to resolve a PR's head and base refs.
_PR_FIELDS: Final = "headRefName,baseRefName,isCrossRepository,headRefOid,headRepositoryOwner,headRepository"
#: Extracts the default branch name from the ``HEAD branch:`` line of ``git remote show`` output.
_HEAD_BRANCH_RE: Final = re.compile(r"HEAD branch:\s*(\S+)")
#: Printable characters ``git check-ref-format`` forbids anywhere in a ref name (control bytes are checked separately).
_REF_FORBIDDEN_CHARS: Final = frozenset(" ~^:?*[\\")


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
    global _DRY_RUN
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
    write_private_text(_sentinel_path(name), f"{value}\n")


def _run(cmd: list[str], timeout: int) -> str:
    """Run ``cmd`` and return its stripped stdout, or an empty string on any failure.

    Args:
        cmd: Argument vector to execute.
        timeout: Maximum wait in seconds.

    Returns:
        Stripped stdout on success, otherwise an empty string.
    """
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)  # noqa: S603 - argv list, no shell
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


def is_valid_branch_name(name: str) -> bool:
    """Report whether ``name`` is a branch name git accepts and one that cannot be parsed as an option.

    Follows ``git check-ref-format --branch``: the PR author controls the head and base ref names, and the later
    steps pass them to ``git switch`` and ``gh pr checkout`` as bare arguments, so a name such as ``--detach`` would be
    read as a flag. ``git check-ref-format --branch`` rejects such a name, yet ``refs/heads/--detach`` passes as a full
    ref, so the hosting service is not guaranteed to refuse it.

    Args:
        name: Candidate branch name, as reported by ``gh pr view``.

    Returns:
        ``True`` when every ref-name rule holds and the name does not start with ``-``.

    Examples:
        >>> is_valid_branch_name("feature/x-1")
        True
        >>> is_valid_branch_name("--detach")
        False
        >>> is_valid_branch_name("a..b")
        False
        >>> is_valid_branch_name("")
        False
    """
    if not name or name.startswith("-") or name.endswith(".") or ".." in name or "@{" in name:
        return False
    if any(ch in _REF_FORBIDDEN_CHARS or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name):
        return False
    return all(part and not part.startswith(".") and not part.endswith(".lock") for part in name.split("/"))


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


def head_repo_name(meta: dict) -> str:
    """Read the head repository's name from PR metadata.

    A fork may be renamed, so its name can differ from the upstream one; Step 10 passes it to
    ``derive_fork_remote.py --fork-repo``, which validates it before it reaches a remote URL.

    Args:
        meta: Decoded ``gh pr view`` payload.

    Returns:
        The repository name, or an empty string when absent (the push then assumes the upstream name).

    Examples:
        >>> head_repo_name({"headRepository": {"id": "R_1", "name": "renamed-fork"}})
        'renamed-fork'
        >>> head_repo_name({"headRepository": None})
        ''
    """
    repo = meta.get("headRepository")
    if not isinstance(repo, dict):
        return ""
    return str(repo.get("name") or "")


def _collect_ref_metadata(args: argparse.Namespace) -> dict[str, str] | None:
    """Resolve the default branch, the PR's head/base/fork metadata, and the current branch/sha.

    Args:
        args: Parsed CLI namespace (``pr``, ``timeout``).

    Returns:
        The 8-key sentinel-name-to-value mapping, or ``None`` when the default branch could not be
        determined, the PR head or base ref is not a valid branch name, or the PR head ref equals the
        default branch — every case already printed its own failure message.
    """
    base_default = default_branch(args.timeout)
    if not base_default:
        print("! BLOCKED — cannot determine default branch; refusing to proceed")
        return None

    meta = fetch_pr_meta(args.pr, args.timeout)
    head_ref = str(meta.get("headRefName") or "")
    base_ref = str(meta.get("baseRefName") or "") or base_default
    cross_repo = "true" if meta.get("isCrossRepository") else "false"
    head_oid = str(meta.get("headRefOid") or "")
    for label, ref in (("head", head_ref), ("base", base_ref)):
        # empty head = failed PR fetch, which the later steps already handle; an invalid name never reaches a sentinel
        if ref and not is_valid_branch_name(ref):
            print(
                f"⛔ PR {label} ref {ref!r} is not a valid git branch name — refusing to persist it for later git commands"
            )
            return None
    if head_ref == base_default:
        print(f"⛔ PR HEAD ref ({head_ref}) equals default branch — refusing to check out and commit on default branch")
        return None

    saved_branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], args.timeout)
    local_sha = _run(["git", "rev-parse", "HEAD"], args.timeout)
    return {
        "resolve-head-ref": head_ref,
        "resolve-base-ref": base_ref,
        "resolve-is-cross-repo": cross_repo,
        "resolve-head-repo-owner": head_repo_owner(meta),
        "resolve-head-repo-name": head_repo_name(meta),
        "resolve-saved-branch": saved_branch,
        "resolve-pr-head-oid": head_oid,
        "resolve-local-sha": local_sha,
    }


def publish_local_base(timeout: int) -> int:
    """Publish the default branch as ``resolve-base-ref`` for runs that have no PR.

    Report mode without a PR number and comment dispatch skip the PR path, so Step 9 would otherwise find no base. The
    sentinel is emptied first: a failure must leave it empty rather than inherit a previous run's value.

    Args:
        timeout: Maximum wait in seconds per subprocess.

    Returns:
        0 when a valid default branch was published, 1 when it is undeterminable or not a valid branch name.
    """
    _write_sentinel("resolve-base-ref", "")
    base = default_branch(timeout)
    if not is_valid_branch_name(base):
        print(f"⛔ Cannot determine a valid origin default branch for QA (got {base!r})")
        return 1
    _write_sentinel("resolve-base-ref", base)
    print(f"→ resolve-base-ref={base}")
    return 0


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
        "--local-base",
        action="store_true",
        help="No-PR path: publish only the default branch as resolve-base-ref (report mode, comment dispatch).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and print, but write no sentinels — use for any run that is not a real skill step.",
    )
    args = parser.parse_args(argv)
    _set_dry_run(args.dry_run)

    if args.pr.startswith("-"):
        print(f"resolve_pr_refs: --pr must not start with '-': {args.pr!r}")
        return 1

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]

    if args.local_base:
        return publish_local_base(args.timeout)

    metadata = _collect_ref_metadata(args)
    if metadata is None:
        return 1

    for name, value in metadata.items():
        _write_sentinel(name, value)

    saved_branch = metadata["resolve-saved-branch"]
    head_ref = metadata["resolve-head-ref"]
    head_oid = metadata["resolve-pr-head-oid"]
    local_sha = metadata["resolve-local-sha"]
    # Reflog trace (cf. investigate 2026-06-13T11-00-00Z: pr195 alias, opaque state).
    print(
        f"→ Step 4 state: SAVED_BRANCH={saved_branch} PR_HEAD_REF={head_ref} "
        f"PR_HEAD_OID={head_oid or '<empty>'} LOCAL_SHA={local_sha or '<empty>'}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

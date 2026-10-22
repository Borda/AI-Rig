#!/usr/bin/env python
"""commit_lint_fixes.py — commit the lint auto-fixes, limited to the files the lint step can touch.

The lint step (``foundry:linting-expert``) reviews and fixes only the files the branch changed since its merge base with
the target branch, so the commit is restricted to the working-tree changes that also fall inside that set. Any other
tracked change — an unrelated local edit, a pre-staged file, a locally modified credentials file — stays out of a commit
made under the bot identity and is reported instead.

The target branch is read from the ``resolve-base-ref-<CSID>`` sentinel that ``oss:resolve`` publishes before its lint
gate. Without it the script refuses to commit anything rather than guess a scope.

No-ops when there are no changed files to stage. Extracted from oss:resolve
lint-qa-gate Step 9 auto-fix commit block.

Usage:
    commit_lint_fixes.py
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from shutil import which

#: Fixed commit message, including the co-author trailer, for the lint auto-fix commit.
_COMMIT_MESSAGE = "lint: auto-fix violations after resolve cycle\n\n---\nCo-authored-by: claude[bot] <209825114+claude[bot]@users.noreply.github.com>"
#: Sentinel base name that holds the target branch the lint step diffed against.
_BASE_REF_SENTINEL = "resolve-base-ref"
#: Seconds allowed for each cheap git plumbing call (diff, merge-base, add).
_PLUMBING_TIMEOUT = 3
#: Seconds allowed for ``git commit``: far above any normal pre-commit hook run, yet bounded so a hook or a signing
#: prompt waiting on input that never comes cannot hang the resolve run for ever.
_COMMIT_TIMEOUT = 600


class _CommitAbort(RuntimeError):
    """The commit cannot proceed safely; ``returncode`` is the exit code to forward."""

    def __init__(self, message: str, returncode: int = 1) -> None:
        """Store the operator-facing ``message`` and the ``returncode`` to exit with."""
        super().__init__(message)
        self.returncode = returncode


def _resolve(cmd: str) -> str:
    """Resolve a CLI tool to its absolute path.

    Args:
        cmd: Bare executable name (e.g. ``"git"``).

    Returns:
        Absolute path to the executable.

    Raises:
        FileNotFoundError: If ``cmd`` is not present on ``PATH``.

    Examples:
        >>> import shutil
        >>> _resolve("git") == shutil.which("git")
        True
    """
    p = which(cmd)
    if p is None:
        raise FileNotFoundError(f"executable not found on PATH: {cmd}")
    return p


def _parse_z(raw: bytes) -> list[str]:
    """Split NUL-terminated git output into path strings.

    ``-z`` output carries each path verbatim, so a name with a space, newline or quote survives; the bytes are decoded
    with the filesystem encoding so a non-UTF-8 name round-trips back into a ``git`` argument unchanged.

    Args:
        raw: Raw stdout of a ``git ... -z`` command.

    Returns:
        The paths in output order, without empty trailing entries.

    Examples:
        >>> _parse_z(b"a.py\\0dir/b c.py\\0")
        ['a.py', 'dir/b c.py']
        >>> _parse_z(b"")
        []
    """
    return [os.fsdecode(chunk) for chunk in raw.split(b"\0") if chunk]


def _git_output(git: str, *args: str) -> bytes:
    """Run a git plumbing command and return its stdout.

    Args:
        git: Absolute path to the ``git`` executable.
        *args: Arguments after the executable.

    Returns:
        The command's raw stdout.

    Raises:
        _CommitAbort: If git cannot run, times out, or exits non-zero; carries git's own exit code when it ran.
    """
    name = next((a for a in args if not a.startswith("--")), "git")
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, no shell
            [git, *args], capture_output=True, check=False, timeout=_PLUMBING_TIMEOUT
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise _CommitAbort(f"git {name} could not run: {exc}") from exc
    if proc.returncode != 0:
        lines = proc.stderr.decode(errors="replace").strip().splitlines()
        raise _CommitAbort(f"git {name} failed: {lines[0] if lines else 'no output'}", proc.returncode)
    return proc.stdout


def _read_sentinel(name: str) -> str:
    """Read the first line of the session-scoped sentinel ``name``.

    Args:
        name: Sentinel base name, without the trailing session token.

    Returns:
        The stripped first line, or an empty string when the sentinel is absent or empty.
    """
    csid = os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "shared"
    tmp_fallback = tempfile.gettempdir() if os.name == "nt" else "/tmp"  # noqa: S108 - shell default of ${TMPDIR:-/tmp}
    path = Path(os.environ.get("TMPDIR") or tmp_fallback) / f"{name}-{csid}"
    try:
        return next(iter(path.read_text(encoding="utf-8").splitlines()), "").strip()
    except OSError:
        return ""


def _lint_scope(git: str) -> set[str]:
    """Collect the files the branch changed since its merge base with the target branch.

    Args:
        git: Absolute path to the ``git`` executable.

    Returns:
        The set of repository-relative paths the lint step was allowed to touch.

    Raises:
        _CommitAbort: If the target-branch sentinel is missing or git cannot establish the merge base.
    """
    base_ref = _read_sentinel(_BASE_REF_SENTINEL)
    if not base_ref:
        raise _CommitAbort(
            f"sentinel {_BASE_REF_SENTINEL}-<CSID> is missing or empty — cannot bound the lint commit; "
            "refusing to stage unrelated changes"
        )
    merge_base = _git_output(git, "merge-base", "HEAD", f"origin/{base_ref}").decode().strip()
    return set(_parse_z(_git_output(git, "diff", "-z", "--name-only", "--no-renames", merge_base, "HEAD")))


def _commit_scoped(git: str, files: list[str]) -> int:
    """Stage ``files`` and commit exactly them.

    ``--literal-pathspecs`` stops a name containing ``*``, ``?`` or ``[`` from matching other files, and the trailing
    path list on ``git commit`` commits only those paths, leaving anything else already staged in the index.

    Args:
        git: Absolute path to the ``git`` executable.
        files: Repository-relative paths to commit.

    Returns:
        ``git commit``'s exit code.

    Raises:
        _CommitAbort: If ``git add`` fails or ``git commit`` exceeds its timeout.
    """
    _git_output(git, "--literal-pathspecs", "add", "--", *files)
    # Generous commit timeout: the commit runs user-controlled pre-commit hooks (ruff, mypy,
    # custom checks routinely exceed a few seconds), so a short timeout would kill git mid-hook.
    # The bound exists only for a hook or signing prompt that waits on input for ever.
    # Matches commit_action_item.py; short timeouts stay only on cheap plumbing calls.
    try:
        result = subprocess.run(  # noqa: S603
            [git, "--literal-pathspecs", "commit", "-m", _COMMIT_MESSAGE, "--", *files],
            check=False,
            timeout=_COMMIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise _CommitAbort(f"git commit timed out after {exc.timeout} s — no commit created") from exc
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    """Entry point — mirrors ``commit_lint_fixes.sh`` behaviour.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``); only ``-h/--help``
            is honoured, otherwise the script takes no positional arguments.

    Returns:
        Exit code: 0 if no changes or commit succeeds; git's exit code when a git call fails; 1 when the lint scope
        cannot be established.

    Examples:
        No doctest — requires subprocess; covered by pytest with monkeypatch.
    """
    # Honour only ``-h/--help`` via argparse; the script otherwise takes no arguments and
    # ignores argv entirely (legacy zero-arg contract — the sole call site passes none).
    # A broad parse_args would reject any stray token with exit 2; keep argv ignored.
    if list(sys.argv[1:] if argv is None else argv) in (["-h"], ["--help"]):
        argparse.ArgumentParser(
            prog="commit_lint_fixes.py",
            description="Commit the lint auto-fixes, limited to the files this branch changed.",
        ).parse_args(["-h"])

    sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]
    git = _resolve("git")
    try:
        changed = _parse_z(_git_output(git, "diff", "-z", "--name-only", "--no-renames", "HEAD"))
        if not changed:
            print("[lint] no changed files to commit")
            return 0
        scope = _lint_scope(git)
        files = [f for f in changed if f in scope]
        if len(files) < len(changed):
            print(
                "[lint] left uncommitted (outside the branch's changed files): "
                + ", ".join(f for f in changed if f not in scope)
            )
        if not files:
            print("[lint] no lint-scope changes to commit")
            return 0
        return _commit_scoped(git, files)
    except _CommitAbort as exc:
        print(f"[lint] {exc}", file=sys.stderr)
        return exc.returncode


if __name__ == "__main__":
    sys.exit(main())

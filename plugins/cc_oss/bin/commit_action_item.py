#!/usr/bin/env python
"""commit_action_item.py — sentinel-aware commit helper for /oss:resolve Step 8.

Touches the commit-auth sentinel for the current repo+branch (required by
git-commit.md Gate 1) immediately before ``git commit``, so the pre-commit
hook approves the commit. Cleans the sentinel afterwards regardless of exit
status.

Two message-source modes (mutually exclusive):

* ``--message-file <path>`` — caller supplies the fully-formed message (used by
  the ``grouped``/``all`` commit paths, which assemble bespoke bodies).
* ``--build`` plus fields — script assembles the canonical per-item ``each``-mode
  message (subject + ``[resolve No.<id>]`` attribution block + co-author trailers),
  so the ``each``-mode template lives in one place instead of being inlined in
  ``action-item-dispatch.md``.

Usage:
    commit_action_item.py --message-file <path> --files <file1> [<file2>...]
    commit_action_item.py --build --summary <s> --item-id <id> --author <a> \\
        --pr <n> --comment <text> --challenge <text> [--codex] \\
        --files <file1> [<file2>...]

Exit codes:
    0 — commit succeeded (or staging area was empty — no-op)
    1 — bad args, message file missing, or commit failed
"""

from __future__ import annotations

import argparse
import atexit
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from shutil import which

_SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class EachMessageFields:
    """Fields for the ``each``-mode per-item commit message.

    Attributes:
        summary: Imperative short subject for the change.
        item_id: Review action-item id.
        author: GitHub handle of the reviewer (without leading ``@`` — attribution only,
            not a live ping).
        pr: Pre-formatted PR reference to embed verbatim — ``#<N>`` when the commit
            lands in the same repo as the PR, or the full PR URL when it lands in a
            different repo (e.g. pushed to a contributor's fork, where bare ``#N``
            would resolve against the fork's own issues, not this PR). Caller resolves
            which form applies (see ``PR_REF`` in ``resolve/SKILL.md`` Step 4).
        comment: Full review comment text (truncated to 72 chars in the body).
        challenge: Challenge-outcome string (e.g. ``evidence=VALID suggestion=VALID resolution=as-suggested``).
        include_codex: Whether to add the OpenAI Codex co-author trailer.
    """

    summary: str
    item_id: str
    author: str
    pr: str
    comment: str
    challenge: str
    include_codex: bool = False


def _slug(text: str) -> str:
    """Convert text to a filesystem/path-safe slug.

    Lowercases, replaces non-alphanumeric runs with ``-``, strips trailing
    hyphens.

    Args:
        text: Input string.

    Returns:
        Slugified string.

    Examples:
        >>> _slug("My/Repo Name")
        'my-repo-name'
        >>> _slug("main")
        'main'
        >>> _slug("feature/add-thing!")
        'feature-add-thing'
    """
    return _SLUG_RE.sub("-", text.lower()).rstrip("-")


def build_each_message(fields: EachMessageFields) -> str:
    """Build the canonical ``each``-mode per-item commit message.

    Mirrors the heredoc previously inlined in ``action-item-dispatch.md``:
    subject line, ``[resolve No.<id>]`` attribution block
    quoting the first 72 chars of the review comment, the challenge-outcome line,
    then the Claude (and optional Codex) co-author trailers.

    Args:
        fields: Structured message fields (see :class:`EachMessageFields`).

    Returns:
        Full commit message string.

    Examples:
        >>> f = EachMessageFields(
        ...     "Fix typo", "3", "octocat", "#42", "Please fix the typo here",
        ...     "evidence=VALID suggestion=VALID resolution=as-suggested",
        ... )
        >>> msg = build_each_message(f)
        >>> msg.splitlines()[0]
        'Fix typo'
        >>> "[resolve No.3] Review by octocat (PR #42):" in msg
        True
        >>> "Co-authored-by: OpenAI Codex" in msg
        False
        >>> codex_fields = EachMessageFields("s", "1", "a", "9", "c", "evidence=VALID", True)
        >>> "Co-authored-by: OpenAI Codex" in build_each_message(codex_fields)
        True
    """
    quoted = fields.comment[:72]
    codex_trailer = "\nCo-authored-by: OpenAI Codex <codex@openai.com>" if fields.include_codex else ""
    return (
        f"{fields.summary}\n"
        f"\n"
        f"[resolve No.{fields.item_id}] Review by {fields.author} (PR {fields.pr}):\n"
        f'"{quoted}..."\n'
        f"Challenge: {fields.challenge}\n"
        f"\n---\n"
        f"Co-authored-by: claude[bot] <209825114+claude[bot]@users.noreply.github.com>"
        f"{codex_trailer}"
    )


_SINGLE_VALUE_FLAGS = frozenset(
    {"--message-file", "--summary", "--item-id", "--author", "--pr", "--comment", "--challenge"}
)


def _parse_args(args: list[str]) -> tuple[dict[str, str | bool], list[str], str | None]:
    """Parse CLI args into an options dict plus the ``--files`` list.

    Args:
        args: Raw argument list (``sys.argv[1:]``).

    Returns:
        ``(opts, files, error)`` — ``error`` is ``None`` on success or a message string.

    Examples:
        >>> opts, files, err = _parse_args(["--message-file", "m.txt", "--files", "a.py", "b.py"])
        >>> err is None and files == ["a.py", "b.py"]
        True
        >>> opts["--message-file"]
        'm.txt'
        >>> _parse_args(["--bogus"])[2]
        "commit_action_item: unknown arg '--bogus'"
    """
    opts: dict[str, str | bool] = {}
    files: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--codex":
            opts["--codex"] = True
            i += 1
        elif a == "--build":
            opts["--build"] = True
            i += 1
        elif a in _SINGLE_VALUE_FLAGS:
            opts[a] = args[i + 1] if i + 1 < len(args) else ""
            i += 2
        elif a == "--files":
            i += 1
            while i < len(args) and not args[i].startswith("--"):
                files.append(args[i])
                i += 1
        else:
            return opts, files, f"commit_action_item: unknown arg '{a}'"
    return opts, files, None


def _resolve_message_file(opts: dict[str, str | bool]) -> tuple[str, str | None]:
    """Resolve the commit message file from either ``--message-file`` or ``--build``.

    In ``--build`` mode the canonical ``each`` message is rendered and written to a
    NamedTemporaryFile whose path is returned (cleaned up at process exit).

    Args:
        opts: Parsed options dict from :func:`_parse_args`.

    Returns:
        ``(message_file_path, error)`` — ``error`` is ``None`` on success.

    Examples:
        No doctest — ``--build`` path writes a temp file; covered by pytest.
    """
    if opts.get("--build"):
        if "--message-file" in opts:
            return "", "commit_action_item: pass either --build or --message-file, not both"
        msg = build_each_message(
            EachMessageFields(
                summary=str(opts.get("--summary", "")),
                item_id=str(opts.get("--item-id", "")),
                author=str(opts.get("--author", "")),
                pr=str(opts.get("--pr", "")),
                comment=str(opts.get("--comment", "")),
                challenge=str(opts.get("--challenge", "")),
                include_codex=bool(opts.get("--codex", False)),
            )
        )
        handle = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", encoding="utf-8", newline="\n", delete=False)
        with handle:
            handle.write(msg)
        path = handle.name
        atexit.register(lambda: Path(path).unlink(missing_ok=True))
        return path, None

    msg_file = str(opts.get("--message-file", ""))
    if not msg_file:
        return "", "commit_action_item: --message-file required"
    if not Path(msg_file).is_file():
        return "", f"commit_action_item: message file not found: {msg_file}"
    return msg_file, None


def main(argv: list[str] | None = None) -> int:
    """Entry point — mirrors ``commit_action_item.sh`` behaviour.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Exit code: 1 on bad args or commit failure; 0 on success or empty stage.

    Examples:
        No doctest — subprocess-dependent; covered by pytest.
    """
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]
    args = list(sys.argv[1:] if argv is None else argv)

    # Honour only ``-h/--help`` via argparse; every other flag flows through the manual
    # _parse_args below, which is already reject-strict (unknown arg → exit 1) with a
    # bespoke "unknown arg '<a>'" message. argparse's native errors would change that
    # exit-1 contract to exit-2 — keep the manual parser as the sole argv authority.
    if args in (["-h"], ["--help"]):
        argparse.ArgumentParser(
            prog="commit_action_item.py",
            description="Sentinel-aware commit helper for /oss:resolve Step 8.",
        ).parse_args(["-h"])

    opts, files, err = _parse_args(args)
    if err is not None:
        print(err, file=sys.stderr)
        return 1

    msg_file, err = _resolve_message_file(opts)
    if err is not None:
        print(err, file=sys.stderr)
        return 1
    if not files:
        print("commit_action_item: --files requires at least one path", file=sys.stderr)
        return 1

    git = which("git")
    if git is None:
        raise FileNotFoundError("executable not found on PATH: git")

    # --- Compute Gate 1 sentinel path ----------------------------------------
    root_proc = subprocess.run(  # noqa: S603
        [git, "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    branch_proc = subprocess.run(  # noqa: S603
        [git, "branch", "--show-current"],
        capture_output=True,
        text=True,
        check=False,
    )
    repo_slug = _slug(Path(root_proc.stdout.strip()).name if root_proc.returncode == 0 else "repo")
    branch_slug = _slug(branch_proc.stdout.strip() if branch_proc.returncode == 0 else "main")

    # Prefer per-user temp dirs over a world-readable `/tmp` (macOS `/tmp`
    # is mode 1777 — sentinel name leaks branch metadata). Order: TMPDIR
    # (per-user on macOS) → XDG_RUNTIME_DIR (per-user on Linux) → fallback.
    # A candidate counts only when absolute for this host: Windows CI inherits a
    # POSIX-style TMPDIR with no native directory, and a drive-less path would
    # resolve against whatever drive the process happens to run on.
    _native = PureWindowsPath if sys.platform == "win32" else PurePosixPath
    _candidates = (os.environ.get("TMPDIR"), os.environ.get("XDG_RUNTIME_DIR"))
    _tmp = Path(next((c for c in _candidates if c and _native(c).is_absolute()), tempfile.gettempdir()))
    sentinel = _tmp / f"claude-commit-auth-{repo_slug}-{branch_slug}"

    # Touch sentinel and register cleanup (mirrors bash `trap EXIT INT TERM`).
    sentinel.touch()
    atexit.register(lambda: sentinel.unlink(missing_ok=True))

    try:
        # --- Stage files ------------------------------------------------------
        add_proc = subprocess.run([git, "add", "--", *files], check=False)  # noqa: S603
        if add_proc.returncode != 0:
            print(f"commit_action_item: git add failed (exit {add_proc.returncode})", file=sys.stderr)
            return add_proc.returncode

        # Empty staging area → nothing to commit.
        cached_proc = subprocess.run(  # noqa: S603
            [git, "diff", "--cached", "--quiet"],
            check=False,
        )
        if cached_proc.returncode == 0:
            print(
                "commit_action_item: staging area empty after add — no commit created",
                file=sys.stderr,
            )
            return 0

        result = subprocess.run([git, "commit", "-F", msg_file], check=False)  # noqa: S603
        return result.returncode
    finally:
        sentinel.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python
"""parse_analyse_args.py — classify the oss:analyse argument and resolve the vitality repository.

Two modes, one per Step-1 argument-parsing stage of ``oss:analyse``:

``--mode classify``
    Decide whether ``CLEAN_ARGS`` names a direct report path (``*.md`` that is neither a ``vitality``/``ecosystem``
    keyword nor a plan/todo file) and make sure the ``analyse-today`` sentinel exists so later steps cannot straddle a
    midnight rollover.

``--mode vitality`` (default)
    For a ``vitality`` invocation, resolve ``owner/repo`` from the explicit argument, from ``gh repo view``, or from the
    ``origin`` remote, then normalise ``CLEAN_ARGS`` to the bare ``vitality`` keyword for mode dispatch.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/parse_analyse_args.py" --mode classify --args "$CLEAN_ARGS"
    python "${CLAUDE_PLUGIN_ROOT}/bin/parse_analyse_args.py" --args "$CLEAN_ARGS"

Sentinels written to ``${TMPDIR:-/tmp}/<name>-${CSID}`` (classify mode):
    analyse-direct-path-mode, analyse-report-file, analyse-today (only when absent)

Sentinels written (vitality mode):
    analyse-clean-args, analyse-gh-owner, analyse-gh-repo

Exit codes:
    0 — sentinels written, or an unsupported-repository notice was printed and the workflow must stop
    1 — the argument names a plan/todo file, which is never a valid report path
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Final

_REPO_SLUG_RE: Final = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_URL_SCHEME_RE: Final = re.compile(r"^https?://")
_GITHUB_PREFIX_RE: Final = re.compile(r"https?://github\.com/")
# Greedy leading ``.*`` mirrors ``sed 's|.*github\.com[:/]||'`` — the last host occurrence wins.
_REMOTE_HOST_RE: Final = re.compile(r"^.*github\.com[:/]")
_VITALITY_KEYWORD: Final = "vitality"


class Mode(str, Enum):
    """Which Step-1 parsing stage to run.

    Subclasses ``str`` rather than ``enum.StrEnum`` because ``requires-python`` is ``>=3.10``.
    """

    CLASSIFY = "classify"
    VITALITY = "vitality"


class _Abort(Exception):
    """Terminate the run after printing operator-facing lines verbatim.

    The wrapped lines and exit code reproduce the ``echo`` + ``exit`` pairs of the bash block this script replaces; the
    calling skill branches on both.
    """

    def __init__(self, code: int, *lines: str) -> None:
        """Store the exit code and the lines to print before returning it."""
        super().__init__(lines[0] if lines else "")
        self.code = code
        self.lines = lines


def _sentinel_path(name: str) -> Path:
    """Build the session-scoped sentinel path for ``name``.

    Args:
        name: Sentinel base name, without the trailing session token.

    Returns:
        Path of the form ``<tmpdir>/<name>-<csid>``.

    Examples:
        >>> import os
        >>> os.environ["TMPDIR"] = os.environ.get("TMPDIR", "/tmp")
        >>> _sentinel_path("analyse-today").name.startswith("analyse-today-")
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

    The trailing newline is required: readers use ``IFS= read -r VAR < file``, which exits non-zero on a file with no
    final newline and silently falls through to its default. ``newline="\\n"`` keeps Windows from emitting ``\\r\\n``,
    which would leave a stray carriage return inside the shell variable.


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
        Stripped stdout when the command succeeds, otherwise an empty string.
    """
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def cut_field(value: str, field: int) -> str:
    """Return one ``/``-delimited field, mirroring ``cut -d'/' -fN``.

    ``cut`` without ``-s`` echoes a delimiter-free line unchanged for every requested field, so a bare ``owner`` yields
    ``owner`` for both field 1 and field 2. That quirk is reproduced deliberately — the skill's downstream behaviour
    depends on it.

    Args:
        value: String to split.
        field: 1-based field index.

    Returns:
        The requested field, or an empty string when the index is past the end.

    Examples:
        >>> cut_field("owner/repo", 1)
        'owner'
        >>> cut_field("owner/repo", 2)
        'repo'
        >>> cut_field("owner", 2)
        'owner'
    """
    if "/" not in value:
        return value
    parts = value.split("/")
    return parts[field - 1] if field <= len(parts) else ""


def classify_report_path(clean_args: str) -> tuple[bool, str]:
    """Decide whether the argument is a direct report path.

    Args:
        clean_args: Argument blob with flags already stripped.

    Returns:
        ``(direct_path_mode, report_file)`` — the second element is empty unless the first is ``True``.

    Raises:
        _Abort: when the argument names a plan or todo file, which is never a report path.

    Examples:
        >>> classify_report_path(".reports/analyse/thread/out.md")
        (True, '.reports/analyse/thread/out.md')
        >>> classify_report_path("vitality owner/repo.md")
        (False, '')
        >>> classify_report_path("42")
        (False, '')
    """
    if not clean_args.endswith(".md") or clean_args.startswith(("vitality", "ecosystem")):
        return False, ""
    if clean_args.startswith(".plans/") or "todo_" in clean_args:
        raise _Abort(
            1,
            f"! Invalid report path: '{clean_args}' — plan/todo files are not valid report paths.",
            "Usage: /oss:analyse <path/to/report.md> --reply  (use a .reports/ path)",
        )
    return True, clean_args


def ensure_today() -> str:
    """Return the analysis date, creating the ``analyse-today`` sentinel on first call.

    Pinning the date once keeps cache keys and report paths consistent across a run that spans midnight.

    Returns:
        The pinned ``YYYY-MM-DD`` date, or an empty string when an existing sentinel is unreadable.
    """
    path = _sentinel_path("analyse-today")
    if path.is_file():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return ""
        return lines[0] if lines else ""
    today = datetime.now().strftime("%Y-%m-%d")
    _write_sentinel("analyse-today", today)
    return today


def repo_from_argument(extra: str) -> str:
    """Resolve ``owner/repo`` from an explicit vitality argument.

    Args:
        extra: Text following the ``vitality`` keyword, with one leading space already removed.

    Returns:
        The ``owner/repo`` slug.

    Raises:
        _Abort: with exit code 0 for a non-GitHub URL or an unrecognised argument shape.

    Examples:
        >>> repo_from_argument("owner/repo")
        'owner/repo'
        >>> repo_from_argument("https://github.com/owner/repo/tree/main")
        'owner/repo'
    """
    if _URL_SCHEME_RE.match(extra):
        if "github.com" not in extra:
            raise _Abort(
                0,
                "⚠ Not a GitHub URL — this skill supports GitHub only.",
                "Other providers (GitLab, Bitbucket, Azure DevOps) are not supported.",
                "Usage: /oss:analyse vitality https://github.com/owner/repo",
            )
        stripped = _GITHUB_PREFIX_RE.sub("", extra, count=1)
        return "/".join(stripped.split("/")[:2]) if "/" in stripped else stripped
    if _REPO_SLUG_RE.match(extra):
        return extra
    raise _Abort(
        0,
        f"⚠ Unrecognised vitality argument: '{extra}'",
        "Usage: /oss:analyse vitality [owner/repo | https://github.com/owner/repo]",
    )


def repo_from_context(timeout: int) -> str:
    """Resolve ``owner/repo`` from the current checkout when no argument was given.

    Args:
        timeout: Maximum wait in seconds for each subprocess.

    Returns:
        The ``owner/repo`` slug.

    Raises:
        _Abort: with exit code 0 when the remote is not GitHub or no remote exists at all.
    """
    repo = _run(["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"], timeout)
    if repo:
        return repo
    remote = _run(["git", "remote", "get-url", "origin"], timeout)
    if "github.com" in remote:
        return re.sub(r"\.git$", "", _REMOTE_HOST_RE.sub("", remote))
    if remote:
        raise _Abort(
            0,
            f"⚠ Remote '{remote}' is not a GitHub repository.",
            "This skill supports GitHub only. Other providers are not supported.",
            "Tip: /oss:analyse vitality https://github.com/owner/repo",
        )
    raise _Abort(
        0,
        "⚠ No GitHub repository detected. Pass a URL:",
        "  /oss:analyse vitality https://github.com/owner/repo",
    )


def _run_classify(clean_args: str) -> int:
    """Persist direct-path-mode state and pin the analysis date."""
    direct_path_mode, report_file = classify_report_path(clean_args)
    _write_sentinel("analyse-direct-path-mode", "true" if direct_path_mode else "false")
    _write_sentinel("analyse-report-file", report_file)
    today = ensure_today()
    print(f"[args] direct_path={'true' if direct_path_mode else 'false'} report_file={report_file} today={today}")
    return 0


def _run_vitality(clean_args: str, timeout: int) -> int:
    """Resolve the vitality repository and normalise the argument for mode dispatch."""
    gh_owner = ""
    gh_repo = ""
    out_args = clean_args
    if clean_args.startswith(_VITALITY_KEYWORD):
        extra = clean_args[len(_VITALITY_KEYWORD) :]
        if extra.startswith(" "):
            extra = extra[1:]
        slug = repo_from_argument(extra) if extra else repo_from_context(timeout)
        gh_owner = cut_field(slug, 1)
        gh_repo = cut_field(slug, 2)
        out_args = _VITALITY_KEYWORD
    _write_sentinel("analyse-clean-args", out_args)
    _write_sentinel("analyse-gh-owner", gh_owner)
    _write_sentinel("analyse-gh-repo", gh_repo)
    print(f"[args] clean_args={out_args} owner={gh_owner} repo={gh_repo}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument list override for testing. Defaults to ``sys.argv[1:]``.

    Returns:
        Exit code — 0 on success or a soft stop, 1 on an invalid report path.
    """
    parser = argparse.ArgumentParser(
        prog="parse_analyse_args.py",
        description="Classify the oss:analyse argument and resolve the vitality repository.",
    )
    parser.add_argument("--args", default="", help="CLEAN_ARGS blob (flags already stripped).")
    parser.add_argument(
        "--mode",
        choices=[m.value for m in Mode],
        default=Mode.VITALITY.value,
        help="Parsing stage to run (default: vitality).",
    )
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

    try:
        if Mode(args.mode) is Mode.CLASSIFY:
            return _run_classify(args.args)
        return _run_vitality(args.args, args.timeout)
    except _Abort as abort:
        for line in abort.lines:
            print(line)
        return abort.code


if __name__ == "__main__":
    sys.exit(main())

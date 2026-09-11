#!/usr/bin/env python
"""build_analyse_paths.py — derive the oss:analyse report path or GitHub cache path for a thread.

Two modes:

``--mode report`` (default)
    Build the thread report path from the repository slug, pin the fast-path sentinels, and record the mtime of an
    existing report so Step 4 can run a type-aware drift check.

``--mode cache``
    Build the ``.cache/gh`` file path for the thread, create the cache directory, and stop the run when a numeric
    argument was given outside any GitHub repository — thread mode cannot resolve ``{owner}/{repo}`` without one.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/build_analyse_paths.py" --clean-args "$CLEAN_ARGS" --today "$TODAY"
    python "${CLAUDE_PLUGIN_ROOT}/bin/build_analyse_paths.py" --mode cache --clean-args "$CLEAN_ARGS" --today "$TODAY"

Sentinels written to ``${TMPDIR:-/tmp}/<name>-${CSID}``:
    report mode — analyse-drift, analyse-fast-path, analyse-fast-path-tentative, analyse-report-mtime
    cache mode  — analyse-cache-file

Exit codes:
    0 — sentinels written, or the no-repository-context notice was printed and the workflow must stop
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from enum import Enum
from pathlib import Path
from typing import Final

_CACHE_DIR: Final = ".cache/gh"
_DEFAULT_SUBDIR: Final = "thread"
_NUMERIC_RE: Final = re.compile(r"^[0-9]+$")
# Mirrors ``tr -cd '[:alnum:]-'`` — everything outside the class is dropped, not replaced.
_NON_SLUG_RE: Final = re.compile(r"[^A-Za-z0-9-]")


class Mode(str, Enum):
    """Which path the script derives.

    Subclasses ``str`` rather than ``enum.StrEnum`` because ``requires-python`` is ``>=3.10``.
    """

    REPORT = "report"
    CACHE = "cache"


def _sentinel_path(name: str) -> Path:
    """Build the session-scoped sentinel path for ``name``.

    Args:
        name: Sentinel base name, without the trailing session token.

    Returns:
        Path of the form ``<tmpdir>/<name>-<csid>``.

    Examples:
        >>> _sentinel_path("analyse-drift").name.startswith("analyse-drift-")
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

    Sentinels are live session state, not scratch output: they are named for the current
    ``CSID`` and are what the skill's later steps and its PreToolUse hooks read. Running this
    script by hand to inspect its output therefore forges state for whatever session is
    running — one observed case wrote an ``analyse-report-file`` pointing at a report that was
    never produced, and the resulting hook denial blocked an unrelated question. Pass
    ``--dry-run`` for any invocation that is not a real skill step.

    Args:
        name: Sentinel base name, without the trailing session token.
        value: Payload to persist.
    """
    if _DRY_RUN:
        print(f"[dry-run] would write {name}={value}")
        return
    _sentinel_path(name).write_text(f"{value}\n", encoding="utf-8", newline="\n")


def _gh_slug(timeout: int) -> str:
    """Return ``owner/repo`` for the current checkout, or an empty string when unavailable.

    Args:
        timeout: Maximum wait in seconds.

    Returns:
        The raw ``nameWithOwner`` value, or an empty string on any failure.
    """
    cmd = ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def report_slug(raw: str) -> str:
    """Sanitise a repository slug for use inside a report filename.

    Slashes become dashes and every remaining non-alphanumeric, non-dash character is dropped, so the slug is safe on
    every filesystem. An unresolvable repository falls back to ``local``.

    Args:
        raw: Raw ``owner/repo`` value, possibly empty.

    Returns:
        Filename-safe slug.

    Examples:
        >>> report_slug("Lightning-AI/pytorch-lightning")
        'Lightning-AI-pytorch-lightning'
        >>> report_slug("owner/repo.py")
        'owner-repopy'
        >>> report_slug("")
        'local'
    """
    slug = _NON_SLUG_RE.sub("", raw.replace("/", "-"))
    return slug or "local"


def cache_slug(raw: str) -> str:
    """Sanitise a repository slug for use inside a cache-file name.

    Only slashes are replaced — unlike :func:`report_slug`, dots and other characters are kept, because the cache key
    only has to be unique per repository, not portable as a report name.

    Args:
        raw: Raw ``owner/repo`` value, possibly empty.

    Returns:
        Cache-key slug; empty when the repository could not be resolved.

    Examples:
        >>> cache_slug("owner/repo.py")
        'owner-repo.py'
        >>> cache_slug("")
        ''
    """
    return raw.replace("/", "-")


def build_report_path(subdir: str, slug: str, clean_args: str, today: str) -> str:
    """Build the analyse report path.

    Args:
        subdir: Report sub-directory (``thread`` for numeric arguments).
        slug: Filename-safe repository slug.
        clean_args: Argument blob with flags already stripped.
        today: Pinned ``YYYY-MM-DD`` analysis date.

    Returns:
        Repository-relative report path.

    Examples:
        >>> build_report_path("thread", "owner-repo", "42", "2026-09-11")
        '.reports/analyse/thread/output-analyse-thread-owner-repo-42-2026-09-11.md'
    """
    return f".reports/analyse/{subdir}/output-analyse-{subdir}-{slug}-{clean_args}-{today}.md"


def build_cache_path(slug: str, clean_args: str, today: str) -> str:
    """Build the GitHub cache-file path for a thread.

    The repository slug is part of the key: without it the same issue number in two repositories would poison one
    another's cache entry.

    Args:
        slug: Cache-key repository slug; empty disables caching.
        clean_args: Argument blob with flags already stripped.
        today: Pinned ``YYYY-MM-DD`` analysis date.

    Returns:
        Cache-file path, or an empty string when the repository is unknown.

    Examples:
        >>> build_cache_path("owner-repo", "42", "2026-09-11")
        '.cache/gh/owner-repo-42-2026-09-11.json'
        >>> build_cache_path("", "42", "2026-09-11")
        ''
    """
    if not slug:
        return ""
    return f"{_CACHE_DIR}/{slug}-{clean_args}-{today}.json"


def _run_report(clean_args: str, today: str, subdir: str, timeout: int) -> int:
    """Derive the report path and pin the fast-path sentinels."""
    report_file = build_report_path(subdir, report_slug(_gh_slug(timeout)), clean_args, today)
    path = Path(report_file)
    tentative = path.is_file()
    # Drift is deferred to Step 4 — the thread type must be known before activity can be compared.
    report_mtime = int(path.stat().st_mtime) if tentative else 0
    _write_sentinel("analyse-drift", "false")
    _write_sentinel("analyse-fast-path", "false")
    _write_sentinel("analyse-fast-path-tentative", "true" if tentative else "false")
    _write_sentinel("analyse-report-mtime", str(report_mtime))
    print(f"[paths] report_file={report_file} fast_path_tentative={'true' if tentative else 'false'}")
    return 0


def _run_cache(clean_args: str, today: str, timeout: int) -> int:
    """Derive the cache-file path, create the cache directory, and guard thread mode."""
    slug = cache_slug(_gh_slug(timeout))
    cache_file = build_cache_path(slug, clean_args, today)
    _write_sentinel("analyse-cache-file", cache_file)
    Path(_CACHE_DIR).mkdir(parents=True, exist_ok=True)
    if not slug and _NUMERIC_RE.match(clean_args):
        print("⚠ No GitHub repository context — cannot resolve repository for thread mode.")
        print("Run from inside a git repository with a GitHub remote:")
        print(f"  cd /path/to/repo && /oss:analyse {clean_args}")
        return 0
    print(f"[paths] cache_file={cache_file}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument list override for testing. Defaults to ``sys.argv[1:]``.

    Returns:
        Exit code 0; argparse exits 2 on an unknown flag.
    """
    parser = argparse.ArgumentParser(
        prog="build_analyse_paths.py",
        description="Derive the oss:analyse report path or GitHub cache path for a thread.",
    )
    parser.add_argument("--clean-args", default="", help="CLEAN_ARGS blob (flags already stripped).")
    parser.add_argument("--today", default="", help="Pinned YYYY-MM-DD analysis date.")
    parser.add_argument("--subdir", default=_DEFAULT_SUBDIR, help="Report sub-directory (default: thread).")
    parser.add_argument(
        "--mode",
        choices=[m.value for m in Mode],
        default=Mode.REPORT.value,
        help="Which path to derive (default: report).",
    )
    parser.add_argument("--timeout", type=int, default=10, help="Max subprocess wait in seconds (default: 10).")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and print, but write no sentinels — use for any run that is not a real skill step.",
    )
    args = parser.parse_args(argv)
    _set_dry_run(args.dry_run)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]

    if Mode(args.mode) is Mode.CACHE:
        return _run_cache(args.clean_args, args.today, args.timeout)
    return _run_report(args.clean_args, args.today, args.subdir, args.timeout)


if __name__ == "__main__":
    sys.exit(main())

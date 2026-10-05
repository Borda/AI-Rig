#!/usr/bin/env python
"""detect_thread_type.py — auto-detect GitHub thread type and check for drift.

Given a GitHub issue/PR/discussion number (or thread URL), probes the GitHub
REST API (and GraphQL for discussions) to determine the thread type and the
``updatedAt`` timestamp. Optionally compares the timestamp against a report
``mtime`` to decide whether the cached report has drifted.

Output is KEY=VALUE lines on stdout suitable for ``eval`` in bash::

    TYPE=issue|pr|discussion|unknown
    UPDATED_AT=<iso8601>
    DRIFT=true|false

The caller is responsible for the downstream ``FAST_PATH`` and ``[resume]``
shell-side logic — this script only emits the three values above.

Usage:
    detect_thread_type.py --number <N|URL> [--report-mtime <epoch>] [--timeout SECS]

Exit codes:
    0 — type detected (including TYPE=unknown when the item is not found)
    1 — bad CLI arguments or unrecoverable subprocess error

Examples:
    python detect_thread_type.py --number 123
    python detect_thread_type.py --number https://github.com/owner/repo/issues/123
    python detect_thread_type.py --number 123 --report-mtime 1700000000
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from enum import Enum
from shutil import which


class ThreadType(str, Enum):
    """Kind of GitHub thread behind a number/URL.

    Subclasses ``str`` (not ``enum.StrEnum``) because ``requires-python`` is ``>=3.10``. ``UNKNOWN`` is the conservative
    result when neither the issues API nor the discussions GraphQL query returns a record.
    """

    ISSUE = "issue"
    PR = "pr"
    DISCUSSION = "discussion"
    UNKNOWN = "unknown"


# Match the number at the end of a GitHub thread URL:
# https://github.com/<owner>/<repo>/(issues|pull|discussions)/<N>
_URL_NUMBER_RE = re.compile(
    r"https?://github\.com/[^/]+/[^/]+/(?:issues|pull|discussions)/(\d+)",
)

_DISCUSSION_QUERY = (
    "query($owner:String!,$repo:String!,$number:Int!){"
    "repository(owner:$owner,name:$repo){"
    "discussion(number:$number){title updatedAt}"
    "}}"
)


def parse_number(raw: str) -> str | None:
    """Extract a plain issue/PR/discussion number from a raw arg.

    Accepts a bare integer (optionally prefixed with ``#``) or a GitHub
    thread URL of the form
    ``https://github.com/<owner>/<repo>/(issues|pull|discussions)/<N>``.

    Args:
        raw: Argument string from the CLI (or shell).

    Returns:
        Decimal-digit string on success; ``None`` when the input is neither
        a numeric token nor a recognised thread URL.

    Examples:
        >>> parse_number("123")
        '123'
        >>> parse_number("#42")
        '42'
        >>> parse_number("https://github.com/o/r/issues/7")
        '7'
        >>> parse_number("https://github.com/o/r/pull/9")
        '9'
        >>> parse_number("https://github.com/o/r/discussions/3")
        '3'
        >>> parse_number("not-a-number") is None
        True
        >>> parse_number("https://github.com/o/r/wiki/Home") is None
        True
    """
    stripped = raw.strip().lstrip("#")
    if stripped.isdigit():
        return stripped
    match = _URL_NUMBER_RE.search(raw.strip())
    return match.group(1) if match else None


def parse_iso_to_epoch(iso: str) -> int | None:
    """Convert an ISO 8601 timestamp (``YYYY-MM-DDTHH:MM:SSZ``) to epoch seconds.

    Args:
        iso: Timestamp string returned by the GitHub API.

    Returns:
        Integer epoch seconds on success; ``None`` on parse failure or
        empty input (caller should treat as drifted — conservative default).

    Examples:
        >>> parse_iso_to_epoch("2024-01-01T00:00:00Z")
        1704067200
        >>> parse_iso_to_epoch("") is None
        True
        >>> parse_iso_to_epoch("not-a-date") is None
        True
    """
    if not iso:
        return None
    try:
        dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return int(dt.timestamp())


def compute_drift(updated_at: str, report_mtime: int | None) -> bool:
    """Decide whether the report has drifted from the live thread.

    Args:
        updated_at: ISO 8601 timestamp from the GitHub API.
        report_mtime: Epoch seconds of the cached report's mtime, or
            ``None`` when no drift check is requested.

    Returns:
        ``True`` when the thread is newer than the report or the timestamp
        cannot be parsed (conservative default — bias toward refetch);
        ``False`` when the report is up to date.

    Examples:
        >>> compute_drift("2024-01-01T00:00:00Z", None)
        False
        >>> compute_drift("2024-01-01T00:00:00Z", 1704067100)
        True
        >>> compute_drift("2024-01-01T00:00:00Z", 1704067300)
        False
        >>> compute_drift("", 1704067200)
        True
        >>> compute_drift("bogus", 1704067200)
        True
    """
    if report_mtime is None:
        return False
    updated_ts = parse_iso_to_epoch(updated_at)
    if updated_ts is None:
        return True
    return updated_ts > report_mtime


def _resolve(cmd: str) -> str:
    """Resolve a CLI tool to its absolute path.

    Args:
        cmd: Bare executable name (e.g. ``"gh"``).

    Returns:
        Absolute path to the executable.

    Raises:
        FileNotFoundError: If ``cmd`` is not on ``PATH``.

    Examples:
        No doctest — environment-dependent; covered by pytest with monkeypatch.
    """
    p = which(cmd)
    if p is None:
        raise FileNotFoundError(f"executable not found on PATH: {cmd}")
    return p


def _gh_issue_lookup(gh: str, number: str, timeout: int) -> dict | None:
    """Fetch the issues-API record for ``number`` (covers issues and PRs).

    Returns the parsed JSON on success, ``None`` on not-found / failure.
    Uses ``{owner}/{repo}`` placeholders so gh resolves the repo from the
    current working directory's git context.

    Args:
        gh: Absolute path to the ``gh`` binary.
        number: Plain decimal-digit string.
        timeout: Subprocess timeout in seconds.

    Returns:
        Parsed JSON object on success; ``None`` otherwise.
    """
    try:
        result = subprocess.run(
            [gh, "api", f"repos/{{owner}}/{{repo}}/issues/{number}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _gh_discussion_lookup(gh: str, number: str, timeout: int) -> dict | None:
    """Fetch a discussion record via GraphQL.

    Args:
        gh: Absolute path to the ``gh`` binary.
        number: Plain decimal-digit string.
        timeout: Subprocess timeout in seconds.

    Returns:
        Dict with ``title`` and ``updatedAt`` on success; ``None`` otherwise.
    """
    try:
        result = subprocess.run(
            [
                gh,
                "api",
                "graphql",
                "-f",
                f"query={_DISCUSSION_QUERY}",
                "-f",
                "owner={owner}",
                "-f",
                "repo={repo}",
                "-F",
                f"number={number}",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    discussion = payload.get("data", {}).get("repository", {}).get("discussion")
    if not discussion or not discussion.get("title"):
        return None
    return discussion


def detect(number: str, report_mtime: int | None, gh: str, timeout: int) -> tuple[ThreadType, str, bool]:
    """Detect the thread type and the drift flag.

    Probes the issues API first (covers both issues and PRs); falls back
    to a GraphQL discussion lookup when the issues API returns nothing.

    Args:
        number: Plain decimal-digit string (already extracted from URL/``#N``).
        report_mtime: Epoch seconds of the cached report's mtime, or ``None``.
        gh: Absolute path to the ``gh`` binary.
        timeout: Subprocess timeout in seconds for every gh invocation.

    Returns:
        ``(type, updated_at, drift)`` tuple where ``type`` is a ``ThreadType``
        member and ``updated_at`` is the ISO 8601 timestamp (empty string for
        ``ThreadType.UNKNOWN``).
    """
    item = _gh_issue_lookup(gh, number, timeout)
    if item is not None:
        type_ = ThreadType.PR if item.get("pull_request") else ThreadType.ISSUE
        updated_at = item.get("updated_at", "") or ""
        return type_, updated_at, compute_drift(updated_at, report_mtime)
    disc = _gh_discussion_lookup(gh, number, timeout)
    if disc is not None:
        updated_at = disc.get("updatedAt", "") or ""
        return ThreadType.DISCUSSION, updated_at, compute_drift(updated_at, report_mtime)
    return ThreadType.UNKNOWN, "", False


def _emit(type_: ThreadType, updated_at: str, drift: bool) -> None:
    """Write TYPE, UPDATED_AT, DRIFT to ${TMPDIR:-/tmp}/oss-detect-*-<CSID> temp files.

    Callers read back with ``cat`` — avoids the ``eval "$(...)"`` anti-pattern.

    Args:
        type_: Detected ``ThreadType`` member; written out as its plain value.
        updated_at: ISO 8601 timestamp (empty when unknown).
        drift: ``True`` when the report should be refetched.
    """
    import os
    import tempfile

    csid = os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "shared"
    tmpdir = os.environ.get("TMPDIR") or tempfile.gettempdir()
    drift_str = "true" if drift else "false"
    for key, val in (("type", type_.value), ("updated-at", updated_at), ("drift", drift_str)):
        with open(f"{tmpdir}/oss-detect-{key}-{csid}", "w") as fh:
            fh.write(val)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Optional argv list (defaults to ``sys.argv[1:]``).

    Returns:
        Exit code — 0 on success (including ``TYPE=unknown``), 1 on bad args.
    """
    parser = argparse.ArgumentParser(
        prog="detect_thread_type.py",
        description="Detect GitHub thread type (issue|pr|discussion) and drift state.",
    )
    parser.add_argument(
        "--number",
        required=True,
        help="Issue/PR/discussion number, '#N', or GitHub thread URL.",
    )
    parser.add_argument(
        "--report-mtime",
        type=int,
        default=None,
        help="Epoch seconds of the cached report's mtime; omit to skip drift check.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=6,
        help="Subprocess timeout per gh invocation, in seconds (default: 6).",
    )
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse exits 2 on usage errors; normalise to 1 to match bin/ convention.
        return 1 if exc.code else 0

    number = parse_number(args.number)
    if number is None:
        print(f"⚠ detect_thread_type: cannot parse '{args.number}' as number or URL", file=sys.stderr)
        _emit(ThreadType.UNKNOWN, "", False)
        return 0

    try:
        gh = _resolve("gh")
    except FileNotFoundError as exc:
        print(f"⚠ detect_thread_type: {exc}", file=sys.stderr)
        _emit(ThreadType.UNKNOWN, "", False)
        return 0

    type_, updated_at, drift = detect(number, args.report_mtime, gh, args.timeout)
    _emit(type_, updated_at, drift)
    return 0


if __name__ == "__main__":
    sys.exit(main())

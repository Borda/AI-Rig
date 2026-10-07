#!/usr/bin/env python3
"""dev_issue_fetch_wrap.py — GitHub issue fetch shared by debug/feature/fix.

Consolidates the three near-identical issue-fetch blocks: read the cross-repo
REPO_NAME sentinel, branch on whether it is set, invoke issue_fetch.py, and tee the
body to this session's sentinel so the next block reuses it instead of re-running
the `gh` call.

Usage: python dev_issue_fetch_wrap.py <debug|feature|fix> "<arguments>"

Writes the issue body to BOTH the legacy shared sentinel (dev-issue-body-<CSID>,
which debug's next block reads) and a per-skill one, mirroring the dual-write
convention in dev_parse_args.py. Body is also echoed to stdout for the caller.

CSID is inherited from the caller's exported environment and never re-derived from
the parent process id: inside a script that id is the invoking shell's, which changes on
every Bash tool call, so a locally derived CSID would name a different sentinel each time.

Exit codes:
  0 — issue fetched
  1 — unknown skill or missing arguments
  * — issue_fetch.py's own exit code, so callers can warn and proceed without context
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

#: Skills that may fetch and cache a GitHub issue; any other skill name is rejected.
_KNOWN_SKILLS = ("debug", "feature", "fix")


def _tmp_dir() -> Path:
    """Session temp dir; never a hardcoded /tmp, which is absent on native Windows Python."""
    return Path(os.environ.get("TMPDIR") or tempfile.gettempdir())


def _csid() -> str:
    """Caller-exported session token, mirroring `${CSID:-${CLAUDE_CODE_SESSION_ID:-shared}}`."""
    return os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "shared"


def _first_line(path: Path) -> str:
    """First line of *path* minus its trailing newline; empty string when unreadable."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.readline().rstrip("\n")
    except OSError:
        return ""


def _fetch(bin_dir: Path, args: str, repo_name: str) -> tuple[str, int]:
    """Run issue_fetch.py, returning its (stripped stdout, exit code).

    stderr is discarded, matching the shell original's `2>/dev/null` — including the interpreter-missing case, whose
    message that redirect also swallowed (exit 127).
    """
    cmd = [sys.executable, str(bin_dir / "issue_fetch.py"), args]
    if repo_name:
        cmd += ["--repo", repo_name]
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, no shell; sibling script path
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False
        )
    except OSError:
        return "", 127
    return proc.stdout.rstrip("\n"), proc.returncode


def main(argv: list[str]) -> int:
    """Fetch a GitHub issue and cache it for debug/feature/fix."""
    skill = argv[1] if len(argv) > 1 else ""
    if skill not in _KNOWN_SKILLS:
        print(
            f"dev_issue_fetch_wrap.py: unknown skill '{skill}' (expected debug|feature|fix)",
            file=sys.stderr,
        )
        return 1

    args = argv[2] if len(argv) > 2 else ""
    if not args:
        print(
            f"dev_issue_fetch_wrap.py: missing arguments (issue reference) for '{skill}'",
            file=sys.stderr,
        )
        return 1

    bin_dir = Path(__file__).resolve().parent
    tmp, csid = _tmp_dir(), _csid()

    repo_name = _first_line(tmp / f"dev-upstream-{csid}")
    issue_body, fetch_exit = _fetch(bin_dir, args, repo_name)

    for sentinel in (tmp / f"dev-issue-body-{csid}", tmp / f"dev-{skill}-issue-body-{csid}"):
        try:
            sentinel.write_text(issue_body + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"dev_issue_fetch_wrap.py: cannot write {sentinel}: {exc}", file=sys.stderr)

    print(issue_body)
    return fetch_exit


if __name__ == "__main__":
    sys.exit(main(sys.argv))

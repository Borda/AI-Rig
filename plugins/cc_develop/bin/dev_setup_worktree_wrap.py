#!/usr/bin/env python3
"""dev_setup_worktree_wrap.py — team-mode run-dir setup shared by feature/fix/refactor.

Consolidates the three byte-similar team-mode setup blocks: setup_worktree.py
invocation (with the skill's sentinel name) plus persistence of the run timestamp
and run directory to this session's sentinels.

Usage: python dev_setup_worktree_wrap.py <feature|fix|refactor>

Output (two lines, same contract as setup_worktree.py):
  Line 1: UTC timestamp
  Line 2: run directory path

The caller registers its own `trap ... EXIT` — a trap set inside this script would
fire when the script exits (immediately), not when the caller's block ends.

CSID is inherited from the caller's exported environment and never re-derived from
the parent process id: inside a script that id is the invoking shell's, which changes on
every Bash tool call, so a locally derived CSID would name a different sentinel each time.

Exit codes:
  0 — run dir created and sentinels written
  1 — unknown skill, or setup_worktree.py produced no timestamp
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

# skill -> (setup_worktree.py ``--sentinel`` value, run-dir sentinel basename)
#: Per-skill worktree setup: the --sentinel value for setup_worktree.py and the run-dir sentinel name.
_SKILLS: dict[str, tuple[str, str]] = {
    "feature": ("", "dev-feature-team-dir"),
    "fix": ("fix-team-check", "dev-fix-run-dir"),
    "refactor": ("refactor-team-check", "dev-refactor-run-dir"),
}


def _tmp_dir() -> Path:
    """Session temp dir; never a hardcoded /tmp, which is absent on native Windows Python."""
    return Path(os.environ.get("TMPDIR") or tempfile.gettempdir())


def _csid() -> str:
    """Caller-exported session token, mirroring `${CSID:-${CLAUDE_CODE_SESSION_ID:-shared}}`."""
    return os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "shared"


def _run_setup(bin_dir: Path, sentinel: str) -> str:
    """Run setup_worktree.py and return its stdout with trailing newlines stripped.

    stderr is inherited and the exit code is deliberately ignored — the shell original checked only whether a timestamp
    came back.
    """
    cmd = [sys.executable, str(bin_dir / "setup_worktree.py")]
    if sentinel:
        cmd += ["--sentinel", sentinel]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, text=True, check=False)  # noqa: S603 - argv list, no shell
    except OSError as exc:
        print(f"dev_setup_worktree_wrap.py: cannot run setup_worktree.py: {exc}", file=sys.stderr)
        return ""
    return proc.stdout.rstrip("\n")


def main(argv: list[str]) -> int:
    """Set up a team-mode run directory shared by feature/fix/refactor."""
    skill = argv[1] if len(argv) > 1 else ""
    entry = _SKILLS.get(skill)
    if entry is None:
        print(
            f"dev_setup_worktree_wrap.py: unknown skill '{skill}' (expected feature|fix|refactor)",
            file=sys.stderr,
        )
        return 1
    sentinel, dir_file = entry

    bin_dir = Path(__file__).resolve().parent
    out = _run_setup(bin_dir, sentinel)

    # `head -1` / `tail -1` over the stripped capture: a one-line result puts the same
    # value in both, and an empty one leaves both empty and trips the guard below.
    lines = out.split("\n")
    ts, run_dir = lines[0], lines[-1]
    if not ts or not run_dir:
        print(
            f"dev_setup_worktree_wrap.py: setup_worktree.py returned no run dir for '{skill}'",
            file=sys.stderr,
        )
        return 1

    tmp, csid = _tmp_dir(), _csid()
    for path, value in (
        (tmp / f"dev-{skill}-team-ts-{csid}", ts),
        (tmp / f"{dir_file}-{csid}", run_dir),
    ):
        try:
            path.write_text(value + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"dev_setup_worktree_wrap.py: cannot write {path}: {exc}", file=sys.stderr)

    print(ts)
    print(run_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

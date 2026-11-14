"""Print how expensive process spawns and small file writes are on this host, for comparing CI operating systems.

Test time in this repository scales with the number of Git and Python processes tests start, so one line per spawn kind
per operating system shows whether a slow runner is slow at starting processes, at writing files, or at neither. Also
lists every Git filter that is configured outside the repository, because an ambient executable filter such as Git LFS
changes how many Git processes the diff collector runs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path


def _milliseconds_per_call(label: str, action: Callable[[], object], repeats: int) -> None:
    """Run ``action`` ``repeats`` times and print its mean wall time in milliseconds."""
    start = time.perf_counter()
    for _ in range(repeats):
        action()
    print(f"{label:34s} {(time.perf_counter() - start) / repeats * 1000:8.1f} ms", flush=True)


def _git(*arguments: str) -> subprocess.CompletedProcess[bytes]:
    """Run one Git command and capture its output."""
    return subprocess.run(["git", *arguments], capture_output=True, check=False)  # noqa: S603, S607 - fixed argv


def main() -> int:
    """Print spawn and file-write timings plus the ambient Git filter configuration."""
    print(f"{sys.platform} Python {sys.version.split()[0]} CPUs {os.cpu_count()}")
    filters = _git("config", "--show-origin", "--get-regexp", r"^filter\..*\.(clean|process|smudge)$")
    print("ambient executable Git filters:", filters.stdout.decode(errors="replace").strip() or "none")
    scratch = Path(tempfile.mkdtemp(prefix="spawn-cost-"))
    try:
        _git("init", "-q", os.fspath(scratch))
        _milliseconds_per_call(
            "python -c pass", lambda: subprocess.run([sys.executable, "-c", "pass"], check=False), 20
        )
        _milliseconds_per_call("git --version", lambda: _git("--version"), 100)
        _milliseconds_per_call(
            "git rev-parse --git-dir", lambda: _git("-C", os.fspath(scratch), "rev-parse", "--git-dir"), 100
        )
        _milliseconds_per_call("git ls-files", lambda: _git("-C", os.fspath(scratch), "ls-files"), 100)
        _milliseconds_per_call(
            "create small file",
            lambda: (scratch / f"f{time.perf_counter_ns()}").write_text("x", encoding="utf-8"),
            500,
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

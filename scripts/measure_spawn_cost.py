"""Print how expensive process spawns and small file writes are on this host, for comparing CI operating systems.

Test time in this repository scales with the number of Git and Python processes tests start, so one line per spawn kind
per operating system shows whether a slow runner is slow at starting processes, at writing files, or at neither. Also
lists every Git filter that is configured outside the repository, because an ambient executable filter such as Git LFS
changes how many Git processes the diff collector runs. On a GitHub Actions Windows runner it measures a second time
after excluding the workspace, temp directories, Git and Python from Defender scanning, showing how much of the spawn
cost scanning accounts for.
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


def _direct_git_executable() -> str | None:
    """Return the Git for Windows binary that the ``cmd`` launcher starts, or ``None`` when it cannot be located.

    Git for Windows puts a small launcher first on ``PATH`` that starts the real ``mingw64`` binary, so every Git call
    creates two processes. Timing the real binary directly shows how much of a Windows Git call is that extra hop.
    """
    if os.name != "nt":
        return None
    exec_path = _git("--exec-path").stdout.decode(errors="replace").strip()
    candidate = Path(exec_path).parent.parent / "bin" / "git.exe" if exec_path else None
    return os.fspath(candidate) if candidate is not None and candidate.is_file() else None


def _exclude_from_defender() -> bool:
    """Exclude the workspace, temp directories, ``git.exe`` and ``python.exe`` from Windows Defender scanning.

    Real-time scanning of every spawned process and written file is a candidate explanation for slow Windows process
    creation. This changes the host's security settings, so it runs only on a GitHub Actions Windows runner, never on a
    developer machine. A failure is reported and measurement continues without the exclusions.
    """
    if os.name != "nt" or os.environ.get("GITHUB_ACTIONS") != "true":
        return False
    candidates = (
        os.environ.get("GITHUB_WORKSPACE") or os.getcwd(),
        os.environ.get("RUNNER_TEMP"),
        tempfile.gettempdir(),
    )
    paths = ", ".join("'" + path.replace("'", "''") + "'" for path in dict.fromkeys(filter(None, candidates)))
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"Add-MpPreference -ExclusionPath {paths}; "
        "Add-MpPreference -ExclusionProcess 'git.exe', 'python.exe'"
    )
    completed = subprocess.run(  # noqa: S603 - fixed argv; the script text holds only quoted environment paths
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],  # noqa: S607 - Windows PowerShell via PATH
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        print(f"::warning::Defender exclusions not applied: {completed.stderr.strip() or completed.returncode}")
    return completed.returncode == 0


def _measure() -> None:
    """Print mean wall time for the spawn kinds and a small file write on this host."""
    scratch = Path(tempfile.mkdtemp(prefix="spawn-cost-"))
    try:
        _git("init", "-q", os.fspath(scratch))
        _milliseconds_per_call(
            "python -c pass", lambda: subprocess.run([sys.executable, "-c", "pass"], check=False), 20
        )
        _milliseconds_per_call("git --version", lambda: _git("--version"), 100)
        direct = _direct_git_executable()
        if direct is not None:
            _milliseconds_per_call(
                "git --version (direct binary)",
                lambda: subprocess.run([direct, "--version"], capture_output=True, check=False),  # noqa: S603
                100,
            )
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


def main() -> int:
    """Print the host, the ambient Git filters and spawn timings, then timings again after Defender exclusions."""
    print(f"{sys.platform} Python {sys.version.split()[0]} CPUs {os.cpu_count()}")
    print("git on PATH:", shutil.which("git"))
    filters = _git("config", "--show-origin", "--get-regexp", r"^filter\..*\.(clean|process|smudge)$")
    print("ambient executable Git filters:", filters.stdout.decode(errors="replace").strip() or "none")
    _measure()
    if _exclude_from_defender():
        print("--- after Defender exclusions", flush=True)
        _measure()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

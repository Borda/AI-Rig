"""Host-capability helpers shared by the foundry hook tests.

Deliberately a uniquely-named module rather than ``conftest``. Every test tree in this repo has its own ``conftest.py``,
and ``ini_options.testpaths`` spans ``benchmarks`` and ``plugins``; a bare ``from conftest import ...`` resolves to
whichever ``conftest`` module was registered under that name first, which under ``--import-mode=importlib`` is
``benchmarks/conftest.py``. Importing by a name that exists exactly once in the repo removes the ordering dependency
entirely.

Fixtures still belong in ``conftest.py`` — pytest injects those by name with no import at all. These two are not
fixtures: :func:`_hook_tmp_base` is called from plain module-level helpers that tests invoke directly, and
:func:`_bash_runs_posix_script` is evaluated inside a module-level ``skipif``, where a fixture cannot reach.
"""

from __future__ import annotations

import functools
import subprocess
import sys
import tempfile
from pathlib import Path


@functools.lru_cache(maxsize=1)
def _hook_tmp_base() -> Path:
    """Return the temp base the foundry hooks write sentinel state into.

    Mirrors the ``getSentinelDir()`` every hook defines identically —
    ``process.platform === "win32" ? os.tmpdir() : "/tmp"``. Tests that hardcoded
    ``/tmp`` agreed with the hook on POSIX only, which is why they carried a
    ``skipif(win32)`` marker; deriving the base the same way the hook does removes
    the need for the marker rather than papering over it.

    On Windows the value is asked of node rather than taken from
    :func:`tempfile.gettempdir`: ``os.tmpdir()`` resolves ``%TEMP%``, which can be
    an 8.3 short path and can differ from Python's answer in case and trailing
    separator. Falls back to :func:`tempfile.gettempdir` when node cannot be
    reached — every caller lives in a file that already skips without node.

    Lazy on purpose: called from fixtures and test bodies only, never at module
    scope, so collection on a node-less host still reaches the skip markers.

    Returns:
        Directory holding ``claude-state-<sid>`` and leftover ``claude-push-auth-*`` sentinels of the former push
        mechanism, which commit-guard deletes at session start.

    Examples:
        >>> _hook_tmp_base().is_absolute()
        True
    """
    if sys.platform != "win32":
        return Path("/tmp")
    try:
        proc = subprocess.run(
            ["node", "-p", "require('os').tmpdir()"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return Path(tempfile.gettempdir())
    if proc.returncode != 0 or not proc.stdout.strip():
        return Path(tempfile.gettempdir())
    return Path(proc.stdout.strip())


@functools.lru_cache(maxsize=1)
def _bash_runs_posix_script() -> bool:
    """Probe whether the ``bash`` on PATH actually executes a POSIX script.

    A capability probe, never a platform test. On Windows ``bash`` frequently
    resolves to ``C:\\Windows\\System32\\bash.exe``, the WSL launcher: with no
    distribution installed it prints a UTF-16 notice and exits 1 without running
    anything. A Git Bash on PATH satisfies the probe and keeps the tests running.

    Returns:
        True when ``bash -c`` ran and produced the expected stdout.

    Examples:
        >>> isinstance(_bash_runs_posix_script(), bool)
        True
    """
    try:
        proc = subprocess.run(
            ["bash", "-c", "printf ok"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "ok"

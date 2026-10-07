"""Cold-grep baseline execution and the wall-clock timing helpers built on it."""

from __future__ import annotations

import math
import statistics
import subprocess
import time
from pathlib import Path

from _bench_query.models import TimingStats
from _bench_query.sources import module_to_grep_pattern, module_to_package

# ---- COLD BASELINE ----


def _run(cmd: list[str], *, cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run a command with standard benchmark defaults (capture, text, 30s timeout)."""
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30, cwd=cwd)  # noqa: S603 - argv list, no shell


class CallCounter:
    def __init__(self) -> None:
        self.count = 0

    def run(self, cmd: list[str], *, cwd: str | None = None) -> subprocess.CompletedProcess[str]:
        self.count += 1
        return _run(cmd, cwd=cwd)


def cold_greps(repo_path: Path, *cmds: list[str]) -> int:
    """Execute each command via :class:`CallCounter` and return the total invocation count.

    All commands are run with ``cwd=repo_path`` and their output is discarded.
    The function is used only for counting — it measures how many subprocess
    calls a cold grep baseline requires, not what the commands return.

    The return value is therefore ``len(cmds)`` **by construction**: it reports the size
    of the planned grep plan, not search work observed to be necessary. Callers must
    label it as a planned-invocation count so it is not read as a measurement beside the
    genuinely measured codemap query counts.

    Args:
        repo_path: Working directory passed to each subprocess call.
        *cmds: Variable number of command lists, each formatted as a list of
            strings suitable for :func:`subprocess.run`.

    Returns:
        Total number of commands executed (always ``len(cmds)``).

    Examples:
        >>> cold_greps(Path("."), ["echo", "a"], ["echo", "b"])
        2
    """
    counter = CallCounter()
    for cmd in cmds:
        counter.run(cmd, cwd=str(repo_path))
    return counter.count


def count_cold_calls_centrality(repo_path: Path) -> int:
    """Return the number of grep/find calls needed to compute centrality without the index.

    Simulates the three-step cold grep pipeline: (1) enumerate all ``.py`` files,
    (2) extract import lines, (3) extract module names from import statements.

    Args:
        repo_path: Root of the repository to search.

    Returns:
        Number of subprocess invocations executed (always 3 for this query type).
    """
    repo = str(repo_path)
    return cold_greps(
        repo_path,
        ["find", repo, "-name", "*.py", "-not", "-path", "*/.git/*", "-not", "-path", "*/__pycache__/*"],
        ["grep", "-rn", r"^from \|^import ", repo, "--include=*.py", "-l"],
        ["grep", "-roh", r"from \([a-z_][a-z_.]*\) import\|^import \([a-z_][a-z_.]*\)", repo, "--include=*.py"],
    )


def count_cold_calls_rdeps(repo_path: Path, module: str) -> int:
    """Return the number of grep calls needed to find reverse-dependencies without the index.

    Simulates a two-pass cold grep: one for direct dotted-name imports and one
    for ``from <parent-package> import`` style imports.

    Args:
        repo_path: Root of the repository to search.
        module: Dotted module name whose importers are to be found.

    Returns:
        Number of subprocess invocations executed (always 2 for this query type).
    """
    repo = str(repo_path)
    pattern = module_to_grep_pattern(module)
    pkg = module_to_package(module)
    second_pattern = f"from {pkg} import" if pkg else f"import {module}"
    return cold_greps(
        repo_path,
        ["grep", "-rn", pattern, repo, "--include=*.py"],
        ["grep", "-rn", second_pattern, repo, "--include=*.py"],
    )


def count_cold_calls_deps(repo_path: Path, module: str) -> int:
    """Return the number of grep calls needed to find a module's direct imports without the index.

    Locates the module source file (checking ``src/`` layout and ``__init__.py``
    variants) and greps its import block.  Returns 1 even when the file is not
    found, because a real agent would still attempt the grep.

    Args:
        repo_path: Root of the repository to search.
        module: Dotted module name whose import block is to be inspected.

    Returns:
        Number of subprocess invocations executed (always 1 for this query type).
    """
    parts = module.replace(".", "/")
    candidates = [
        repo_path / "src" / (parts + ".py"),
        repo_path / (parts + ".py"),
        repo_path / "src" / parts / "__init__.py",
        repo_path / parts / "__init__.py",
    ]
    target = next((c for c in candidates if c.exists()), None)
    if target is None:
        return 1  # would still attempt one grep
    return cold_greps(repo_path, ["grep", "-n", r"^from \|^import ", str(target)])


def count_cold_calls_path(repo_path: Path, frm: str, to: str) -> int:
    """Return the number of grep calls needed to discover a 2-hop import path without the index.

    Assumes a 2-hop path (A → intermediate → B) requiring three grep passes:
    grep for ``frm``, grep for ``frm``'s parent package, and grep for ``to``.

    Args:
        repo_path: Root of the repository to search.
        frm: Dotted module name of the path source.
        to: Dotted module name of the path destination.

    Returns:
        Number of subprocess invocations executed (always 3 for this query type).
    """
    # BFS via grep: N+1 calls for N-hop path; use 2-hop assumption = 3 calls
    repo = str(repo_path)
    return cold_greps(
        repo_path,
        ["grep", "-rn", f"import {frm}", repo, "--include=*.py"],
        ["grep", "-rn", f"import {frm.rsplit('.', 1)[0]}", repo, "--include=*.py"],
        ["grep", "-rn", f"import {to}", repo, "--include=*.py"],
    )


# ---- LATENCY ----

#: Search tools that report "no lines selected" with exit status 1 — a completed search, not a failure.
_SEARCH_TOOLS = frozenset({"grep", "egrep", "fgrep", "rg", "ag", "ack"})


def _command_completed(cmd: list[str], returncode: int) -> bool:
    """Return whether *cmd* finished its work, treating an empty search result as completion.

    ``grep`` exits 1 when a pattern matches nothing and 2 only on a real error, so counting exit 1 as
    a failure discards the timing of a search that ran to completion. The cold-baseline sequences pair
    a broad grep with a narrow one, so one empty match dropped every repetition and left the baseline
    median undefined.

    Args:
        cmd: The command that was run, as a subprocess argument list.
        returncode: Exit status the command reported.

    Returns:
        True when the command completed its search or exited zero.

    Examples:
        >>> _command_completed(["grep", "-rn", "nothing", "."], 1)
        True
        >>> _command_completed(["grep", "-rn", "nothing", "/missing"], 2)
        False
        >>> _command_completed(["scan-query", "central"], 1)
        False
    """
    if returncode == 0:
        return True
    tool = Path(cmd[0]).name if cmd else ""
    return returncode == 1 and tool in _SEARCH_TOOLS


def time_command(cmd: list[str], n: int = 5, cwd: str | None = None) -> TimingStats:
    """Time a single command over ``n`` repeated runs and return sorted wall-clock statistics.

    Timings are sorted before computing the median to eliminate cold-start
    outliers.

    Only successful runs contribute a latency observation. A command that exits
    non-zero is discarded and counted in ``failed``: an instantly-failing command
    otherwise recorded an excellent latency, which is how a broken command could look
    like the fastest one measured. A timed-out run is discarded and counted in
    ``timed_out``: its true duration is only known to exceed the deadline, so feeding
    the deadline into the median treats censored data as an observation.

    Args:
        cmd: Command to run, formatted as a list of strings for
            :func:`subprocess.run`.
        n: Number of repetitions.  Default is 5.
        cwd: Working directory for the subprocess.  ``None`` inherits the current
            process directory.

    Returns:
        :class:`TimingStats` over the successful runs, with ``failed`` and
        ``timed_out`` counts. All statistics are ``nan`` when no run succeeded —
        never silently zero, which would read as an infinitely fast command.
    """
    timings: list[float] = []
    failed = timed_out = 0
    for _ in range(n):
        start = time.perf_counter()
        try:
            completed = _run(cmd, cwd=cwd)
        except subprocess.TimeoutExpired:
            timed_out += 1
            continue
        elapsed_ms = (time.perf_counter() - start) * 1000
        if not _command_completed(cmd, completed.returncode):
            failed += 1
            continue
        timings.append(elapsed_ms)
    if not timings:
        return TimingStats(
            min_ms=math.nan, median_ms=math.nan, max_ms=math.nan, n=n, failed=failed, timed_out=timed_out
        )
    timings.sort()
    return TimingStats(
        min_ms=round(timings[0], 2),
        median_ms=round(statistics.median(timings), 2),
        max_ms=round(timings[-1], 2),
        n=n,
        failed=failed,
        timed_out=timed_out,
    )


def time_commands(cmds: list[list[str]], n: int = 3, cwd: str | None = None) -> TimingStats:
    """Time a sequence of commands as one logical operation (e.g. a cold grep session).

    Runs all commands in ``cmds`` back-to-back per repetition, measuring total
    elapsed wall-clock time for the whole sequence.  Timed-out individual
    commands are skipped but their wall-clock contribution is still counted.
    Timings are sorted before computing the median to eliminate cold-start
    outliers.

    Args:
        cmds: Sequence of commands to run in order; each command is a list of
            strings suitable for :func:`subprocess.run`.
        n: Number of full repetitions of the command sequence.  Default is 3.
        cwd: Working directory for every subprocess call.  ``None`` inherits
            the current process directory.

    Returns:
        :class:`TimingStats` with ``min_ms``, ``median_ms``, ``max_ms`` (all
        in milliseconds, rounded to 2 decimal places), and ``n`` (repetition
        count).

    Examples:
        >>> import subprocess
        >>> stats = time_commands([["echo", "a"], ["echo", "b"]], n=3)
        >>> stats.n
        3
        >>> stats.median_ms >= 0
        True
    """
    timings: list[float] = []
    failed = timed_out = 0
    for _ in range(n):
        start = time.perf_counter()
        sequence_failed = sequence_timed_out = False
        for cmd in cmds:
            try:
                completed = _run(cmd, cwd=cwd)
            except subprocess.TimeoutExpired:
                sequence_timed_out = True
                continue
            if not _command_completed(cmd, completed.returncode):
                sequence_failed = True
        elapsed_ms = (time.perf_counter() - start) * 1000
        # A sequence whose commands failed or were censored is not a latency
        # observation for the work the sequence is supposed to represent.
        if sequence_timed_out:
            timed_out += 1
            continue
        if sequence_failed:
            failed += 1
            continue
        timings.append(elapsed_ms)
    if not timings:
        return TimingStats(
            min_ms=math.nan, median_ms=math.nan, max_ms=math.nan, n=n, failed=failed, timed_out=timed_out
        )
    timings.sort()
    return TimingStats(
        min_ms=round(timings[0], 2),
        median_ms=round(statistics.median(timings), 2),
        max_ms=round(timings[-1], 2),
        n=n,
        failed=failed,
        timed_out=timed_out,
    )

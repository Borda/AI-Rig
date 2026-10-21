#!/usr/bin/env python
"""dev_run_repro.py — reproduce a bug with the project's pytest runner and print the verdict.

Used by ``/develop:debug`` Step 1. Replaces two near-identical inline bash blocks that each rebuilt the pytest command
with ``eval``, a zsh-sensitive ``$TEST_PATH`` quoting dance and a ``tail -60`` pipe. The script does the same work
without a shell:

1. Read the runner from the ``dev-pytest-cmd`` sentinel (written by ``runner-detection.md``). Empty → print the
   unresolved notice and exit 1: an empty command would exit 127 and be misread as a reproduced bug.
2. Read the target from the ``dev-debug-test-path`` sentinel. Empty → no target argument, so pytest uses its own
   default collection (the full suite) rather than an explicit ``.``.
3. Run ``<runner> --tb=long [<target>] -v`` with stdout and stderr merged, keeping only the last 60 lines in memory.
4. Print those lines, write the runner's exit code to the ``dev-gate-exit`` sentinel, then print the verdict. Only
   pytest exit 1 (tests ran and failed) means the bug reproduced; any other non-zero code (2 collection or import
   error, 3 internal error, 4 usage or unknown node id, 5 nothing collected) is a gate error, not a reproduction.

The runner must be one of the commands ``run_pytest_short.py`` allows and the target must stay inside the working
directory, so neither a poisoned sentinel nor a path lifted from an issue body can run an arbitrary command or escape
the project.

Usage:
    python dev_run_repro.py

Exit codes:
    0 — the run finished with pytest exit 0 or 1; the verdict line says whether the bug reproduced (the pytest code is
        in ``dev-gate-exit``).
    1 — runner sentinel empty, the target resolved outside the working directory, or pytest exited with a code other
        than 0 or 1 (``! GATE ERROR`` line printed; the code is still recorded in ``dev-gate-exit``).
    2 — runner is not an allowlisted pytest command, or pytest was not found on PATH.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from collections import deque
from shutil import which

from dev_run_dir import read_sentinel, write_sentinel
from run_pytest_short import _PYTEST_ALLOWLIST, _validate_target_in_cwd

#: Sentinel (written by runner detection) holding the project's pytest invocation.
_PYTEST_CMD_SENTINEL = "dev-pytest-cmd"
#: Sentinel holding the failing test path or node id; empty means the full suite.
_TEST_PATH_SENTINEL = "dev-debug-test-path"
#: Sentinel that receives the runner's exit code for later blocks.
_GATE_EXIT_SENTINEL = "dev-gate-exit"
#: Number of trailing output lines printed.
_TAIL_LINES = 60
#: Hard wall-clock cap in seconds; the calling bash block sets ``# timeout: 600000`` to match.
_RUN_TIMEOUT_S = 600

_UNRESOLVED = (
    "! PYTEST_CMD unresolved — re-run §Project Detection (runner-detection.md); an empty command would exit 127 and be "
    "misread as a reproduced bug"
)
_REPRODUCED = "Bug reproduced — tests fail. Proceed to fix."
_GATE_ERROR = (
    "! GATE ERROR: pytest exit {code} (collection/import error, usage error or no tests collected) — "
    "not a reproduced bug; fix the test path or the collection error and re-run"
)
_NOT_REPRODUCED = "Tests pass — bug may not be reproducible via pytest; check symptom directly."


def run_tail(argv: list[str], tail_lines: int = _TAIL_LINES) -> tuple[int, list[str]]:
    """Run a command with merged output, returning its exit code and last ``tail_lines`` lines.

    Output is consumed line by line into a bounded buffer, so a test run that floods stdout cannot exhaust memory.

    Args:
        argv: Command and arguments, executed without a shell.
        tail_lines: How many trailing lines to keep.

    Returns:
        ``(returncode, lines)`` with line endings stripped.

    Examples:
        >>> code, lines = run_tail([sys.executable, "-c", "print('a'); print('b'); raise SystemExit(3)"], 1)
        >>> (code, lines)
        (3, ['b'])
    """
    with subprocess.Popen(  # noqa: S603 - argv list, no shell; the runner is allowlisted by the caller
        argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace"
    ) as proc:
        tail: deque[str] = deque(maxlen=tail_lines)
        if proc.stdout is not None:
            tail.extend(line.rstrip("\r\n") for line in proc.stdout)
        return proc.wait(timeout=_RUN_TIMEOUT_S), list(tail)


def main(argv: list[str] | None = None) -> int:
    """Run the repro and print the verdict.

    Args:
        argv: Command-line arguments; the script takes none, so any argument is a usage error.

    Returns:
        Process exit code (see module docstring).
    """
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]
    if sys.argv[1:] if argv is None else argv:
        print(
            f"usage: dev_run_repro.py (no arguments; reads the {_PYTEST_CMD_SENTINEL} and {_TEST_PATH_SENTINEL} sentinels)"
        )
        return 2
    pytest_cmd = read_sentinel(_PYTEST_CMD_SENTINEL)
    if not pytest_cmd:
        print(_UNRESOLVED)
        return 1
    if pytest_cmd not in _PYTEST_ALLOWLIST:
        print(f"dev_run_repro: rejected unsafe PYTEST_CMD: {pytest_cmd}", file=sys.stderr)
        return 2
    test_path = read_sentinel(_TEST_PATH_SENTINEL)
    if test_path:
        _validate_target_in_cwd(test_path)
    parts = shlex.split(pytest_cmd)
    resolved = which(parts[0])
    if resolved is None:
        print(f"dev_run_repro: executable not found on PATH: {parts[0]}", file=sys.stderr)
        return 2
    code, tail = run_tail([resolved, *parts[1:], "--tb=long", *([test_path] if test_path else []), "-v"])
    if tail:
        print("\n".join(tail))
    write_sentinel(_GATE_EXIT_SENTINEL, str(code))
    if code not in (0, 1):
        print(_GATE_ERROR.format(code=code))
        return 1
    print(_REPRODUCED if code else _NOT_REPRODUCED)
    return 0


if __name__ == "__main__":
    sys.exit(main())

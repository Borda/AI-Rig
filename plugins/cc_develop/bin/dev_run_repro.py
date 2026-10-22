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
   pytest exit 1 with a failure summary (tests ran and failed) means the bug reproduced; any other non-zero code (2
   collection or import error, 3 internal error, 4 usage or unknown node id, 5 nothing collected) is a gate error, not
   a reproduction.

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

import re
import shlex
import subprocess
import sys
import threading
import time
from collections import deque
from shutil import which
from typing import IO

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
_NO_SUMMARY = (
    "! GATE ERROR: pytest exited 1 without a failure summary (pytest missing or unable to start) — "
    "not a reproduced bug; fix the runner and re-run"
)
_NOT_REPRODUCED = "Tests pass — bug may not be reproducible via pytest; check symptom directly."


#: A pytest failure summary (``1 failed, 2 passed in 0.1s``, ``FAILED test_a.py::test_a - …``): proof that a test ran
#: and failed. A ``FAILED`` line must name a test node id (``path::name``); the stats line must be the whole line,
#: optionally framed by ``=`` rules and followed by the ``(0:01:05)`` clock pytest adds to runs of a minute or more.
#: Unrelated tool output such as ``1 error generated.``, a startup hook's ``FAILED preflight: …`` or an interpreter
#: diagnostic quoting a path that contains ``1 failed in 0.01s`` does not match.
_FAILURE_EVIDENCE = re.compile(
    r"^FAILED \S.*?::\S|^(?:=+ )?\d+ failed(?:, \d+ \w+)* in [\d.]+s(?: \(\d+:\d\d:\d\d\))?(?: =+)?$"
)
#: Terminal colour and style codes, which ``--color=yes`` puts inside the summary lines.
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def failure_reported(lines: list[str]) -> bool:
    """Report whether the output carries a pytest failure summary, so an exit status of 1 means a test failed.

    Pytest exits 1 only after tests ran and failed, and names the failures in its summary. A status of 1 without one
    comes from the runner failing to start: pytest not installed, a plugin that cannot load, a broken environment.

    Trust boundary: the project's ``conftest.py`` files and pytest plugins run inside the pytest process and can print
    any text, a counterfeit failure summary included. They are the project's own code, in the same trust domain as the
    tests themselves, so this check guards against a runner that never started, not against a project that fakes its
    own results.

    Examples:
        >>> failure_reported(["FAILED test_a.py::test_a - assert 0", "1 failed in 0.01s"])
        True
        >>> failure_reported(["=== 2 failed, 1 passed in 0.50s ==="])
        True
        >>> failure_reported(["/usr/bin/python3: No module named pytest"])
        False
        >>> failure_reported(["startup compiler: 1 error generated."])
        False
        >>> failure_reported(["FAILED preflight: required test environment is unavailable"])
        False
        >>> failure_reported(["Fatal Python error: PYTHONHOME = '/nonexistent/1 failed in 0.01s'"])
        False
        >>> failure_reported(["==== 1 failed, 300 passed in 65.12s (0:01:05) ===="])
        True
        >>> esc = chr(27)
        >>> failure_reported([f"{esc}[31mFAILED{esc}[0m t.py::{esc}[1mtest_a{esc}[0m - assert 0"])
        True
    """
    return any(_FAILURE_EVIDENCE.search(_ANSI_ESCAPE.sub("", line)) for line in lines)


def run_tail(argv: list[str], tail_lines: int = _TAIL_LINES) -> tuple[int, list[str]]:
    """Run a command with merged output, returning its exit code and last ``tail_lines`` lines.

    Output is consumed line by line into a bounded buffer, so a test run that floods stdout cannot exhaust memory.
    The output is read on a helper thread so the ``_RUN_TIMEOUT_S`` deadline covers the whole run: at the deadline
    the direct child is killed and this call raises, even when a grandchild that inherited stdout keeps the pipe open.
    Only the direct child is killed; such a grandchild (a test worker, say) runs on until it exits by itself. Killing
    the whole process group would need a new session for the child, which would stop the caller's own interrupt or
    timeout from reaching the tests.

    Args:
        argv: Command and arguments, executed without a shell.
        tail_lines: How many trailing lines to keep.

    Returns:
        ``(returncode, lines)`` with line endings stripped.

    Raises:
        subprocess.TimeoutExpired: When the run outlives ``_RUN_TIMEOUT_S``; the child has been killed.

    Examples:
        >>> code, lines = run_tail([sys.executable, "-c", "print('a'); print('b'); raise SystemExit(3)"], 1)
        >>> (code, lines)
        (3, ['b'])
    """
    proc = subprocess.Popen(  # noqa: S603 - argv list, no shell; the runner is allowlisted by the caller
        argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace"
    )
    deadline = time.monotonic() + _RUN_TIMEOUT_S
    tail: deque[str] = deque(maxlen=tail_lines)
    reader = threading.Thread(target=_drain, args=(proc.stdout, tail), daemon=True)
    reader.start()
    reader.join(_RUN_TIMEOUT_S)
    try:
        if reader.is_alive():
            raise subprocess.TimeoutExpired(argv, _RUN_TIMEOUT_S)
        code = proc.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        # Leave stdout open: closing it while the reader thread still holds its lock would block here instead.
        proc.kill()
        proc.wait()
        raise
    if proc.stdout is not None:
        proc.stdout.close()
    return code, list(tail)


def _drain(stream: IO[str] | None, tail: deque[str]) -> None:
    """Read ``stream`` to its end, keeping the last lines in ``tail`` with line endings stripped."""
    if stream is not None:
        tail.extend(line.rstrip("\r\n") for line in stream)


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
    if code == 1 and not failure_reported(tail):
        print(_NO_SUMMARY)
        return 1
    print(_REPRODUCED if code else _NOT_REPRODUCED)
    return 0


if __name__ == "__main__":
    sys.exit(main())

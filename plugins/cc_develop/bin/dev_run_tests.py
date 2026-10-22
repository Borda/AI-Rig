#!/usr/bin/env python
"""Run the project's sentinel-selected test command without interpreting shell source.

Used by ``/develop:fix``, ``/develop:feature`` and ``/develop:refactor``. The ``pytest`` mode reads
``dev-pytest-cmd`` and appends literal CLI arguments to an allowlisted pytest command. The ``suite`` mode reads
``dev-test-cmd`` and accepts only a closed set of complete project test commands, with no extra arguments.
Both sentinel reads refuse state owned by another user through ``dev_run_dir.read_sentinel``.

Commands run as argv lists after PATH resolution, with merged UTF-8 stdout and stderr. Output streams as it arrives
unless ``--tail N`` requests only the last N lines, using the bounded buffer shared with ``dev_run_repro``.

Usage:
    python dev_run_tests.py {pytest|suite} [--tail N] [--] [ARGS...]

Exit codes:
    0 — tests passed.
    64 — no test command ran: sentinel missing, empty or untrusted (an unresolved notice is printed), unsafe command,
        invalid arguments, executable missing, launch failure, a pytest run that exited 1 without printing a failure
        summary (pytest missing or unable to start) or a fault in this wrapper itself.
    Child status — propagated unchanged after a successful launch, including non-zero pytest or suite statuses; a
    child that itself exits 64 is reported as 2, which no gate reads as a pass (0), a red demo (1) or a refused run.
    Status 64 sits outside the codes pytest (0-5), make (2) and the other suite runners use, so a gate can tell a
    refused or unresolved command from a test run that failed (1) or was interrupted (2).

Verdict sentinel:
    ``dev-tests-verdict`` is written ``none`` when the wrapper starts and ``failed <nonce>`` only when a ``pytest`` run
    exits 1 with a failure summary, where ``<nonce>`` is ``$DEV_TESTS_VERDICT_NONCE``. A Python interpreter that dies
    before this script runs also exits 1, so a red gate exports a fresh nonce per run and accepts exit 1 only when the
    sentinel, a regular file it owns, reads ``failed <its nonce>``; a stale or planted file never carries that nonce.
    The write replaces the file without following a link planted at its name. No verdict is recorded when the session
    id is not already in the ``[A-Za-z0-9_-]`` form the shell and Python both use for the file name.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
from collections import deque
from shutil import which

from dev_run_dir import read_sentinel, write_sentinel
from dev_run_repro import failure_reported, run_tail
from run_pytest_short import _PYTEST_ALLOWLIST

#: Status for "no test command ran" (sysexits ``EX_USAGE``), distinct from every status a test runner returns.
EXIT_NO_RUN = 64

#: Sentinel naming the outcome this wrapper confirmed: ``failed <nonce>`` for a pytest run that failed with a summary,
#: else ``none``. Red gates require it beside exit 1, which an interpreter that never reached this script also returns.
VERDICT_SENTINEL = "dev-tests-verdict"

#: Environment variable carrying the per-run nonce a red gate expects back in the ``failed`` verdict.
VERDICT_NONCE_ENV = "DEV_TESTS_VERDICT_NONCE"


def _record_verdict(value: str) -> None:
    """Write ``value`` to the verdict sentinel when the session id names the same file for the shell and for Python.

    The shell gate builds the file name from the raw session id, while ``dev_run_dir`` strips it to ``[A-Za-z0-9_-]``.
    When the two differ no verdict is written, so the gate fails closed instead of reading a file that belongs to a
    session whose id strips to the same text.
    """
    raw = os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "shared"
    if re.fullmatch(r"[A-Za-z0-9_-]+", raw):
        write_sentinel(VERDICT_SENTINEL, value)


#: Status reported for a test command that itself exits :data:`EXIT_NO_RUN`: neither a pass, a reproduced failure
#: (1) nor a refused run, so no gate can mistake it for any of them.
EXIT_CHILD_RESERVED = 2

#: How many trailing output lines are kept to recognise a runner that could not start its tests.
_RECENT_LINES = 20


#: Complete project test commands supported in addition to pytest. Exact matching prevents shell syntax or extra
#: flags in a planted sentinel from turning a suite gate into arbitrary command execution.
_SUITE_COMMANDS: frozenset[str] = frozenset({"tox", "tox -q", "make test", "npm test", "cargo test", "go test ./..."})


class _UsageError(Exception):
    """Wrapper arguments were invalid; the parser has already printed why."""


class _Parser(argparse.ArgumentParser):
    """Argument parser that reports a usage error to the caller instead of exiting with argparse's status 2."""

    def error(self, message: str) -> None:  # type: ignore[override]
        """Print the usage error and signal it, so :func:`main` can return :data:`EXIT_NO_RUN`."""
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        raise _UsageError


def _parse_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    """Separate wrapper options from literal child arguments, honoring an explicit ``--`` boundary."""
    parser = _Parser(description=__doc__, add_help=False, allow_abbrev=False)
    parser.add_argument("mode", choices=("pytest", "suite"))
    parser.add_argument("--tail", type=int, default=None, metavar="N", help="Keep only the last N output lines.")
    boundary = argv.index("--") if "--" in argv else len(argv)
    options, extra = parser.parse_known_args(argv[:boundary])
    extra += argv[boundary + 1 :]
    if options.tail is not None and options.tail < 0:
        parser.error("--tail must be non-negative")
    if options.mode == "suite" and extra:
        parser.error("suite does not accept extra arguments")
    return options, extra


def _emit(text: str, *, end: str = "\n") -> None:
    """Print to stdout; once the reader has closed the pipe, discard output instead of failing the run."""
    try:
        print(text, end=end, flush=True)
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())


def _run_stream(argv: list[str]) -> tuple[int, list[str]]:
    """Stream merged child output, returning the child's status and its last few lines."""
    with subprocess.Popen(  # noqa: S603 - allowlisted, PATH-resolved argv, no shell
        argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace"
    ) as proc:
        recent: deque[str] = deque(maxlen=_RECENT_LINES)
        if proc.stdout is not None:
            for line in proc.stdout:
                _emit(line, end="")
                recent.append(line.rstrip("\r\n"))
        return proc.wait(), list(recent)


def main(argv: list[str] | None = None) -> int:
    """Validate the session command and execute the requested test gate.

    Args:
        argv: CLI arguments; defaults to ``sys.argv[1:]``.

    Returns:
        The child status, or :data:`EXIT_NO_RUN` when no test command ran. A fault anywhere in this wrapper, including
        before the command starts, also returns :data:`EXIT_NO_RUN`, never the status 1 of a failing test.

    Examples:
        No doctest — reads session files and starts subprocesses; covered by pytest with real child processes.
    """
    try:
        _record_verdict("none")
        return _run(list(sys.argv[1:] if argv is None else argv))
    except Exception as exc:
        print(f"dev_run_tests: wrapper error: {exc!r}", file=sys.stderr)
        return EXIT_NO_RUN


def _run(argv: list[str]) -> int:
    """Resolve the allowlisted command, run it, and map the outcome to the documented status."""
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", newline="\n")
    try:
        options, extra = _parse_args(argv)
    except _UsageError:
        return EXIT_NO_RUN
    name = "dev-pytest-cmd" if options.mode == "pytest" else "dev-test-cmd"
    command = read_sentinel(name)
    if not command:
        _emit(f"! {name} unresolved — re-run Project Detection (runner-detection.md)")
        return EXIT_NO_RUN
    allowed = _PYTEST_ALLOWLIST if options.mode == "pytest" else _PYTEST_ALLOWLIST | _SUITE_COMMANDS
    if command not in allowed:
        print(f"dev_run_tests: rejected unsafe {name}: {command}", file=sys.stderr)
        return EXIT_NO_RUN
    parts = shlex.split(command)
    resolved = which(parts[0])
    if resolved is None:
        print(f"dev_run_tests: executable not found on PATH: {parts[0]}", file=sys.stderr)
        return EXIT_NO_RUN
    try:
        child_argv = [resolved, *parts[1:], *extra]
        if options.tail is None:
            code, recent = _run_stream(child_argv)
        else:
            code, recent = run_tail(child_argv, tail_lines=max(options.tail, _RECENT_LINES))
            shown = recent[len(recent) - options.tail :] if options.tail else []
            if shown:
                _emit("\n".join(shown))
    except OSError as exc:
        print(f"dev_run_tests: could not launch {parts[0]}: {exc}", file=sys.stderr)
        return EXIT_NO_RUN
    if code == 1 and options.mode == "pytest" and not failure_reported(recent):
        print(f"dev_run_tests: {parts[0]} exited 1 without a pytest failure summary; no test ran", file=sys.stderr)
        return EXIT_NO_RUN
    if code == EXIT_NO_RUN:
        # a test command that itself exits 64 must stay distinguishable from "no test command ran"
        print(
            f"dev_run_tests: {parts[0]} exited {code}; reporting {EXIT_CHILD_RESERVED} so {EXIT_NO_RUN} keeps "
            "meaning no test ran",
            file=sys.stderr,
        )
        return EXIT_CHILD_RESERVED
    if code == 1 and options.mode == "pytest":
        _record_verdict(f"failed {os.environ.get(VERDICT_NONCE_ENV, '')}".strip())
    return code


if __name__ == "__main__":
    sys.exit(main())

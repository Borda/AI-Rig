#!/usr/bin/env python
"""Run pytest and emit only the last requested lines of output.

Purpose: Execute allowlisted pytest commands with bounded output and a ten-minute deadline.
Scope: Local pytest runs selected by Develop skills; no arbitrary shell commands.

Validates ``pytest_cmd`` against an allowlist
(``pytest``, ``uv run pytest``, ``python -m pytest``, ``poetry run pytest``,
``poetry run python -m pytest``) before any execution — the set must cover
every runner ``runner-detection.md`` can emit.
Captures combined stdout+stderr, then prints the last ``tail_n`` lines (default 20).
Bad/non-integer ``tail_n`` silently falls back to 20.

``pytest_cmd`` and ``target`` are sliced directly from ``argv`` rather than matched by
argparse, which is present only to supply ``-h/--help`` — both carry embedded spaces or
``::`` node-id tokens argparse would otherwise split. ``--tail-n`` is a named flag, extracted
from anywhere in argv before positional slicing. This keeps the allowlist-rejection contract
(exit 2), the target-containment guard (exit 64), and the silent ``tail_n`` fallback.

Usage:
    run_pytest_short.py [pytest_cmd] [target ...] [--tail-n N] [--log PATH]

Every target is containment-checked; several targets run as one pytest process (``dev_test_targets.py --run``
passes its whole selection this way). ``--log PATH`` also writes the full, untruncated-by-tail output there.
Logs stay inside the session's run directory, or ``.developments`` for standalone runs without a run sentinel.

Outputs: The output tail on stdout and optionally the full bounded log at ``--log``.
Failure: Runner failures remain distinct from pytest-reported failures.
Used by: Develop targeted verification, batch isolation, and baseline callers.

Exit codes:
    0 — pytest passed.
    1-5 — pytest reported failure/collection error (propagated unchanged).
    2 — pytest_cmd not in the allowlist (before pytest runs); also argparse's bad-argument exit.
    64 — target or log path rejected before pytest runs.
    124 — runner deadline expired.
    127 — executable missing; 70 — other runner or log I/O error.
"""

from __future__ import annotations

import argparse
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from shutil import which

from dev_run_dir import read_sentinel

#: The only pytest launch commands this runner will execute, so an arbitrary command cannot be passed through it.
_PYTEST_ALLOWLIST: frozenset[str] = frozenset(
    {
        "pytest",
        "uv run pytest",
        "python -m pytest",
        "poetry run pytest",
        "poetry run python -m pytest",
    }
)
#: Number of trailing output lines printed when the caller gives no valid count.
_DEFAULT_TAIL_N: int = 20


def _validate_target_in_cwd(target: str) -> None:
    """Reject option-shaped or response-file targets and paths outside the current working directory.

    A target starting with ``-`` is refused outright: ``Path("-p")`` resolves under the working directory, so
    containment alone lets an option-shaped token such as ``-p``, ``-c`` or ``--rootdir=...`` reach pytest's argv.
    A leading ``@`` is also refused because pytest expands response files into unchecked command-line arguments.

    Args:
        target: Raw target string from argv (file/dir path or pytest node id).

    Raises:
        SystemExit: With exit code 64 if the target starts with ``-`` or ``@``, or resolves outside ``Path.cwd()``.
            Pytest node ids (``path::nodeid``) have the path portion validated.
    """
    if target.startswith(("-", "@")):
        print(f"run-pytest-short: rejected option-shaped target: {target}", file=sys.stderr)
        sys.exit(64)
    path_part = target.split("::", 1)[0]
    if not path_part:
        return
    target_path = Path(path_part).resolve()
    cwd = Path.cwd().resolve()
    try:
        target_path.relative_to(cwd)
    except ValueError:
        print(
            f"run-pytest-short: rejected target outside project directory: {target_path}",
            file=sys.stderr,
        )
        sys.exit(64)


def is_allowed_pytest_cmd(cmd: str) -> bool:
    """Report whether ``cmd`` is one of the pytest launch commands this runner executes.

    Public so ``dev_test_targets.py`` applies the same allowlist to the command it only prints in plan mode.

    Examples:
        >>> is_allowed_pytest_cmd("uv run pytest"), is_allowed_pytest_cmd("pytest; rm -rf /")
        (True, False)
    """
    return cmd in _PYTEST_ALLOWLIST


def within_workspace(path: str) -> bool:
    """Report whether ``path`` resolves inside the working directory or the session temp dir.

    The batch Codemap writer, run logs and selection records use the narrower :func:`within_run_directory` boundary.

    Examples:
        >>> within_workspace("logs/run.log")
        True
    """
    resolved = Path(path).resolve()
    tmp_fallback = tempfile.gettempdir() if os.name == "nt" else "/tmp"  # noqa: S108 - shell default of ${TMPDIR:-/tmp}
    # Both temp spellings stay roots: the shell's ${TMPDIR:-/tmp} and the platform dir that TEMP/TMP may name.
    roots = (
        Path.cwd(),
        Path(os.environ.get("TMPDIR") or tmp_fallback),
        Path(os.environ.get("TMPDIR") or tempfile.gettempdir()),
    )
    return any(resolved.is_relative_to(root.resolve()) for root in roots)


def within_run_directory(path: str) -> bool:
    """Accept artifacts only under a session run directory or the standalone developments fallback.

    Session sentinels use the ownership-checked reader and may name an absolute directory in the original project while
    the caller runs in a worktree. Without one, standalone selection keeps its artifacts under ``.developments``.
    Resolved paths prevent an artifact symlink from reaching project sources or an unrelated temporary directory.
    """
    roots = [read_sentinel(f"dev-{skill}-dev-dir") for skill in ("feature", "fix", "refactor")]
    directories = [Path(root).resolve() for root in roots if root] or [(Path.cwd() / ".developments").resolve()]
    resolved = Path(path).resolve()
    return any(resolved.is_relative_to(root) for root in directories)


# Hard cap on captured subprocess output (50 MB) — guards against adversarial test floods.
#: Most bytes of pytest output captured before the rest is dropped and a truncation note is appended.
_MAX_OUTPUT_BYTES: int = 50 * 1024 * 1024


def _resolve(cmd: str) -> str:
    """Resolve ``cmd`` to an absolute path using ``shutil.which``.

    Args:
        cmd: Bare executable name (e.g. ``"pytest"``, ``"uv"``, ``"python"``).

    Returns:
        Absolute path to the executable.

    Raises:
        FileNotFoundError: If ``cmd`` is not present on ``PATH``.
    """
    resolved = which(cmd)
    if resolved is None:
        raise FileNotFoundError(f"executable not found on PATH: {cmd}")
    return resolved


def _parse_tail_n(raw: str) -> int:
    """Parse ``raw`` as a non-negative int; fall back to default on bad input.

    Args:
        raw: Tail-line-count argument as a string (possibly empty or non-numeric).

    Returns:
        Parsed positive int, or ``_DEFAULT_TAIL_N`` on any parse failure.

    Examples:
        >>> _parse_tail_n("5")
        5
        >>> _parse_tail_n("abc")
        20
        >>> _parse_tail_n("")
        20
        >>> _parse_tail_n("-3")
        20
    """
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_TAIL_N
    return n if n >= 0 else _DEFAULT_TAIL_N


def _extract_named_flags(args: list[str]) -> tuple[list[str], str, str]:
    """Pull ``--tail-n VALUE`` and ``--log PATH`` out of argv.

    Args:
        args: Raw argument list, flags and positionals interleaved in any order.

    Returns:
        Tuple of (remaining positional args in original order, raw tail_n string or ``""`` if absent, log path or
        ``""`` if absent).
    """
    positional: list[str] = []
    named = {"--tail-n": "", "--log": ""}
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in named and i + 1 < len(args):
            named[arg] = args[i + 1]
            i += 2
            continue
        positional.append(arg)
        i += 1
    return positional, named["--tail-n"], named["--log"]


_TIMEOUT_SECONDS = 600


def _stop_runner(proc: subprocess.Popen[str]) -> None:
    """Stop the runner's process tree so inherited output pipes cannot delay the deadline."""
    try:
        if os.name == "nt" and hasattr(proc, "pid"):
            subprocess.run(  # noqa: S603 - Windows native process-tree termination, fixed arguments
                [_resolve("taskkill"), "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=5,
                check=False,
            )
        elif hasattr(proc, "pid"):
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"run-pytest-short: process-tree cleanup unavailable: {exc}", file=sys.stderr)
    proc.kill()


def _capture(proc: subprocess.Popen[str]) -> tuple[int, str]:
    """Drain capped output while enforcing the deadline even during a blocking read."""
    expired = threading.Event()

    def expire() -> None:
        """Release a blocked read by stopping the expired runner."""
        expired.set()
        _stop_runner(proc)

    timer = threading.Timer(_TIMEOUT_SECONDS, expire)
    timer.daemon = True
    timer.start()
    chunks: list[str] = []
    total = 0
    truncated = False
    try:
        if proc.stdout is None:
            raise RuntimeError("proc.stdout must not be None")
        # Keep draining after the cap so output backpressure cannot prevent exit.
        with proc.stdout:
            for chunk in iter(lambda: proc.stdout.read(64 * 1024), ""):
                remaining = max(0, _MAX_OUTPUT_BYTES - total)
                if remaining:
                    chunks.append(chunk[:remaining])
                total += min(len(chunk), remaining)
                truncated = truncated or len(chunk) > remaining
        try:
            code = proc.wait(timeout=_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            expire()
            code = 124
    finally:
        timer.cancel()
    output = "".join(chunks)
    if truncated:
        output += f"\n[run-pytest-short: output truncated at {_MAX_OUTPUT_BYTES} bytes]"
    if expired.is_set():
        output += "\n[run-pytest-short: runner deadline expired]"
    return (124 if expired.is_set() else code), output


def main(argv: list[str] | None = None) -> int:
    """Entry point — mirrors ``run-pytest-short.sh`` behaviour.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Pytest's exit code or a distinct runner error (see module docstring).

    No doctest — spawns pytest via subprocess and reads argv; covered by pytest.
    """
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]
    args = list(sys.argv[1:] if argv is None else argv)

    # argparse supplies only -h/--help; the positional pytest_cmd/target carry spaces and
    # ``::`` node-id tokens that must be sliced directly, never matched by argparse.
    if args and args[0] in {"-h", "--help"}:
        parser = argparse.ArgumentParser(
            prog="run_pytest_short.py",
            description="Run pytest --tb=short -v and emit only the last N lines of combined output.",
        )
        parser.add_argument(
            "pytest_cmd", nargs="?", default="pytest", help="Allowlisted pytest runner (default: pytest)."
        )
        parser.add_argument("target", nargs="?", default=".", help="Test path or node id (default: current directory).")
        parser.add_argument(
            "--tail-n",
            dest="tail_n",
            default=str(_DEFAULT_TAIL_N),
            help=f"Number of trailing output lines to print (default: {_DEFAULT_TAIL_N}).",
        )
        parser.add_argument("--log", help="Also write the full combined output to this file.")
        parser.parse_args(args)  # exits 0 after printing help

    positional, tail_n_raw, log_path = _extract_named_flags(args)
    pytest_cmd = positional[0] if len(positional) >= 1 else "pytest"
    # Several targets are accepted so a targeted-test selection runs as one pytest process, not one per file.
    targets = positional[1:] or ["."]
    tail_n = _parse_tail_n(tail_n_raw)

    if pytest_cmd not in _PYTEST_ALLOWLIST:
        print(f"run-pytest-short: rejected unsafe PYTEST_CMD: {pytest_cmd}", file=sys.stderr)
        return 2

    for target in targets:
        _validate_target_in_cwd(target)
    if log_path and not within_run_directory(log_path):
        print(f"run-pytest-short: rejected --log outside run directory: {log_path}", file=sys.stderr)
        return 64

    parts = shlex.split(pytest_cmd)
    try:
        parts[0] = _resolve(parts[0])
        return _execute(parts, targets, tail_n, log_path)
    except FileNotFoundError as exc:
        print(f"run-pytest-short: {exc}", file=sys.stderr)
        return 127
    except OSError as exc:
        print(f"run-pytest-short: runner I/O error: {exc}", file=sys.stderr)
        return 70


def _execute(parts: list[str], targets: list[str], tail_n: int, log_path: str) -> int:
    """Launch pytest, preserve its bounded log, and print the requested tail."""
    # Capture combined stdout+stderr so we can tail it; mirrors `2>&1 | tail -N`.
    # Read incrementally with a byte cap so adversarial test output cannot exhaust memory
    # before tail_n truncation is applied.
    proc = subprocess.Popen(  # noqa: S603 — allowlisted cmd + resolved binary, no shell.
        [*parts, "--tb=short", *targets, "-v"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=os.name != "nt",
    )
    returncode, output = _capture(proc)  # 10-min hard cap; pytest_cmd callers set # timeout: 600000 in bash
    if log_path:
        # full output stays inspectable on disk while the terminal shows only the tail
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(log_path).write_text(output, encoding="utf-8", newline="\n")
    # splitlines drops the trailing newline if present; reattach to preserve newline semantics.
    lines = output.splitlines()
    tail = lines[-tail_n:] if tail_n > 0 else []
    if tail:
        print("\n".join(tail))
    return returncode


if __name__ == "__main__":
    sys.exit(main())

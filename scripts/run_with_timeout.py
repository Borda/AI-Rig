#!/usr/bin/env python3
"""Run one subprocess behind a portable hard timeout.

Purpose:
    Prevent a networked or third-party command from blocking repository maintenance indefinitely while preserving its
    normal output and exit status.
Scope:
    Launch exactly the argv supplied after ``--``. The helper does not use a shell, retry commands, inspect
    credentials, or interpret command output. On timeout it terminates the spawned process tree on POSIX and Windows.
Usage:
    ``python3 scripts/run_with_timeout.py --timeout-seconds 120 --label "external plugin update" -- command arg``.
Outputs:
    Inherit stdin, stdout, and stderr during normal execution. Timeout and launch failures add one concise diagnostic
    to stderr. Successful and ordinary failing commands keep their original exit status; timeout returns ``124`` and
    launch failure returns ``127``.
Failure:
    Reject a missing command or non-finite/non-positive timeout before launch with exit ``2``. Cleanup attempts are
    bounded and fall back to killing the direct child when process-tree facilities are unavailable.
Used by:
    The root ``Makefile`` wraps every external-plugin marketplace and installation command with this helper. Tests
    invoke it directly to prove successful transparency, failure propagation, timeout behavior, and descendant cleanup
    without contacting a marketplace.
"""

from __future__ import annotations

import argparse
import math
import os
import signal
import subprocess
import sys

#: Exit status returned when the command was stopped for exceeding its time limit.
TIMEOUT_EXIT_CODE = 124
#: Exit status returned when the command could not be started.
LAUNCH_FAILURE_EXIT_CODE = 127
#: Seconds allowed for the Windows process-tree kill and for the child to exit after it.
TERMINATION_GRACE_SECONDS = 2.0


def positive_timeout(value: str) -> float:
    """Parse a finite, strictly positive duration in seconds for argparse.

    Raise ``argparse.ArgumentTypeError`` for non-numeric input, zero, negative
    values, NaN, or infinity so the CLI reports invalid input before launch.

    Examples:
        >>> positive_timeout("0.5")
        0.5
        >>> positive_timeout("0")
        Traceback (most recent call last):
        ...
        argparse.ArgumentTypeError: timeout must be a finite positive number
    """
    try:
        timeout = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("timeout must be a finite positive number") from error
    if not math.isfinite(timeout) or timeout <= 0:
        raise argparse.ArgumentTypeError("timeout must be a finite positive number")
    return timeout


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the timeout, diagnostic label, and literal child argv."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout-seconds", required=True, type=positive_timeout)
    parser.add_argument("--label", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        parser.error("a command is required after --")
    return args


def terminate_process_tree(process: subprocess.Popen[bytes], platform: str) -> None:
    """Terminate the child process tree within a bounded cleanup window."""
    if platform == "win32":
        try:
            subprocess.run(  # noqa: S603 - fixed Windows process-tree utility and numeric PID.
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],  # noqa: S607 - Windows system utility resolved via PATH
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=TERMINATION_GRACE_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
        try:
            process.wait(timeout=TERMINATION_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=TERMINATION_GRACE_SECONDS)
        return

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=TERMINATION_GRACE_SECONDS)


def run(command: list[str], timeout_seconds: float, label: str, platform: str = sys.platform) -> int:
    """Run literal argv with inherited streams and return a diagnostic exit code.

    Preserve ordinary child exit statuses. Return ``127`` when launch fails, ``124`` after timeout cleanup, or ``130``
    after interrupt cleanup. The caller supplies a positive timeout; this function does not parse it.
    """
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if platform == "win32" else 0
    try:
        process = subprocess.Popen(  # noqa: S603 - caller intentionally supplies literal argv without a shell.
            command,
            creationflags=creationflags,
            start_new_session=platform != "win32",
        )
    except OSError as error:
        print(f"{label} failed to start: {error}", file=sys.stderr)
        return LAUNCH_FAILURE_EXIT_CODE

    try:
        return process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        terminate_process_tree(process, platform)
        print(f"{label} timed out after {timeout_seconds:g} seconds", file=sys.stderr)
        return TIMEOUT_EXIT_CODE
    except KeyboardInterrupt:
        terminate_process_tree(process, platform)
        print(f"{label} interrupted", file=sys.stderr)
        return 130


def main(argv: list[str] | None = None) -> int:
    """Run the parsed command behind its configured timeout."""
    args = parse_args(argv)
    return run(args.command, args.timeout_seconds, args.label)


if __name__ == "__main__":
    raise SystemExit(main())

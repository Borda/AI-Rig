#!/usr/bin/env python
"""run_pytest_short.py — run ``pytest --tb=short -v`` and emit only the last N lines of output.

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
(exit 2), the target-containment guard (exit 1), and the silent ``tail_n`` fallback exactly as
the legacy bash script defined them.

Usage:
    run_pytest_short.py [pytest_cmd] [target ...] [--tail-n N] [--log PATH]

Every target is containment-checked; several targets run as one pytest process (``dev_test_targets.py --run``
passes its whole selection this way). ``--log PATH`` also writes the full, untruncated-by-tail output there.

Exit codes:
    0 — pytest passed.
    1 — target resolved outside the project directory (containment guard).
    1-5 — pytest reported failure/collection error (propagated unchanged).
    2 — pytest_cmd not in the allowlist (before pytest runs); also argparse's bad-argument exit.
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from pathlib import Path
from shutil import which

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
    """Reject targets that resolve outside the current working directory.

    Args:
        target: Raw target string from argv (file/dir path or pytest node id).

    Raises:
        SystemExit: With exit code 1 if the resolved target escapes ``Path.cwd()``.
            Pytest node ids (``path::nodeid``) have the path portion validated.
    """
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
        sys.exit(1)


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


def main(argv: list[str] | None = None) -> int:
    """Entry point — mirrors ``run-pytest-short.sh`` behaviour.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Pytest's exit code, or 2 when ``pytest_cmd`` is rejected by the allowlist.

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

    parts = shlex.split(pytest_cmd)
    parts[0] = _resolve(parts[0])
    # Capture combined stdout+stderr so we can tail it; mirrors `2>&1 | tail -N`.
    # Read incrementally with a byte cap so adversarial test output cannot exhaust memory
    # before tail_n truncation is applied.
    proc = subprocess.Popen(  # noqa: S603 — allowlisted cmd + resolved binary, no shell.
        [*parts, "--tb=short", *targets, "-v"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    chunks: list[str] = []
    total = 0
    truncated = False
    # PIPE guarantees a stream.
    if proc.stdout is None:
        raise RuntimeError("proc.stdout must not be None")
    for chunk in iter(lambda: proc.stdout.read(64 * 1024), ""):
        remaining = _MAX_OUTPUT_BYTES - total
        if remaining <= 0:
            truncated = True
            # Drain remaining output without buffering so the child can exit cleanly.
            for _ in iter(lambda: proc.stdout.read(64 * 1024), ""):
                pass
            break
        if len(chunk) > remaining:
            chunks.append(chunk[:remaining])
            total += remaining
            truncated = True
            for _ in iter(lambda: proc.stdout.read(64 * 1024), ""):
                pass
            break
        chunks.append(chunk)
        total += len(chunk)
    returncode = proc.wait(timeout=600)  # 10-min hard cap; pytest_cmd callers set # timeout: 600000 in bash
    output = "".join(chunks)
    if truncated:
        output += f"\n[run-pytest-short: output truncated at {_MAX_OUTPUT_BYTES} bytes]"
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

#!/usr/bin/env python
"""pytest_gate.py — run ``pytest --tb=short -v`` on a target, surfacing the exit code unchanged.

Validates ``pytest_cmd`` against an allowlist
(``pytest``, ``uv run pytest``, ``python -m pytest``, ``poetry run pytest``,
``poetry run python -m pytest``) before any execution — the set must cover
every runner ``runner-detection.md`` can emit.
Streams full pytest output to stdout/stderr; this is the no-tail inner-loop variant.

The two positional arguments carry pytest node-id tokens (e.g. ``tests/foo.py::test_bar``)
and runner strings with embedded spaces; they are sliced directly from ``argv`` rather than
matched by argparse, which is present only to supply ``-h/--help``. This keeps the
allowlist-rejection contract (exit 2) and the target-containment guard (exit 1) exactly
as the legacy bash gate defined them.

Usage:
    pytest_gate.py [pytest_cmd] [target]

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

_PYTEST_ALLOWLIST: frozenset[str] = frozenset(
    {
        "pytest",
        "uv run pytest",
        "python -m pytest",
        "poetry run pytest",
        "poetry run python -m pytest",
    }
)


def _validate_target_in_cwd(target: str) -> None:
    """Reject targets that resolve outside the current working directory.

    Args:
        target: Raw target string from argv (file/dir path or pytest node id).

    Raises:
        SystemExit: With exit code 1 if the resolved target escapes ``Path.cwd()``.
            Pytest node ids (``path::nodeid``) have the path portion validated.
    """
    # Strip pytest node-id suffix (e.g. "tests/foo.py::test_bar") — only the
    # path portion is filesystem-resolvable; the nodeid is opaque to the OS.
    path_part = target.split("::", 1)[0]
    if not path_part:
        return
    target_path = Path(path_part).resolve()
    cwd = Path.cwd().resolve()
    try:
        target_path.relative_to(cwd)
    except ValueError:
        print(
            f"pytest-gate: rejected target outside project directory: {target_path}",
            file=sys.stderr,
        )
        sys.exit(1)


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


def main(argv: list[str] | None = None) -> int:
    """Entry point — mirrors ``pytest-gate.sh`` behaviour.

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
            prog="pytest_gate.py",
            description="Run pytest --tb=short -v on a target, surfacing the exit code unchanged.",
        )
        parser.add_argument(
            "pytest_cmd", nargs="?", default="pytest", help="Allowlisted pytest runner (default: pytest)."
        )
        parser.add_argument("target", nargs="?", default=".", help="Test path or node id (default: current directory).")
        parser.parse_args(args)  # exits 0 after printing help

    pytest_cmd = args[0] if len(args) >= 1 else "pytest"
    target = args[1] if len(args) >= 2 else "."

    if pytest_cmd not in _PYTEST_ALLOWLIST:
        print(f"pytest-gate: rejected unsafe PYTEST_CMD: {pytest_cmd}", file=sys.stderr)
        return 2

    _validate_target_in_cwd(target)

    parts = shlex.split(pytest_cmd)
    parts[0] = _resolve(parts[0])
    # stdout/stderr inherited from caller — full output streams as bash version did.
    result = subprocess.run(
        [*parts, "--tb=short", target, "-v"],
        check=False,
        timeout=120,  # 2-min cap; pytest_gate is the inner-loop fast-iteration variant
    )
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())

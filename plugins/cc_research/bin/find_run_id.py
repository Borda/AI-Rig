#!/usr/bin/env python
"""find_run_id.py — locate the latest completed run id under a state-dir base.

Scans ``<state_dir_base>/*/state.json`` files sorted by mtime descending (newest first)
and returns the basename of the first matching directory whose ``state.json`` has
``status`` in (``"completed"``, ``"goal-achieved"``). When ``--match-program <path>``
is supplied, the run's ``program_file`` field must additionally equal ``<path>``.

Extracted from the ``python -c`` inline blocks in
``plugins/cc_research/skills/fortify/SKILL.md`` (lines 99, 101) and
``plugins/cc_research/skills/retro/SKILL.md`` (line 72) — replaces them with a single
deterministic call satisfying the Check 23e policy on inline Python.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/find_run_id.py" <state_dir_base> [--match-program <path>]

Exit codes:
    0   matching run found; basename printed to stdout
    1   no matching run found
    2   argument error (missing/invalid args or unreadable state dir)

Examples:
    # Latest completed run anywhere under the state base
    python find_run_id.py .experiments/state

    # Latest completed run whose program_file matches the given path
    python find_run_id.py .experiments/fortify-state --match-program program.md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

#: Run state statuses that count as a finished run when choosing which run id to report.
_COMPLETED_STATUSES: frozenset[str] = frozenset({"completed", "goal-achieved"})


def _safe_mtime(path: Path) -> float:
    """Return ``path``'s mtime, or ``-inf`` when it is unavailable.

    Guards the sort key against a directory deleted between ``iterdir`` and ``stat``
    (concurrent /research:run cleanup or the 30-day artifact TTL sweep). A vanished
    entry sorts last (oldest) and is skipped by the later ``state.json`` read, so the
    function still returns ``None`` on no-match rather than raising (exit-1 contract).

    Args:
        path: Candidate run directory.

    Returns:
        The directory mtime, or ``float("-inf")`` on any ``OSError``.
    """
    try:
        return path.stat().st_mtime
    except OSError:
        return float("-inf")


def _load_state(state_file: Path) -> dict | None:
    """Return parsed JSON from ``state_file`` or ``None`` on any read/parse error.

    Args:
        state_file: Path to a ``state.json`` candidate.

    Returns:
        Parsed JSON object as a ``dict``, or ``None`` when the file is missing,
        unreadable, not valid JSON, or not a JSON object at the top level.
    """
    try:
        with state_file.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def find_run_id(state_dir: Path, match_program: str | None = None) -> str | None:
    """Return the basename of the latest completed run directory under ``state_dir``.

    Iterates immediate subdirectories of ``state_dir`` sorted by mtime descending
    (newest first), inspects each ``<subdir>/state.json``, and returns the first
    match. A match requires ``status`` in ``{"completed", "goal-achieved"}``; when
    ``match_program`` is supplied, the ``program_file`` field must also equal it
    exactly (no path normalisation — matches the original inline behaviour).

    Args:
        state_dir: Directory containing per-run subdirectories.
        match_program: Optional ``program_file`` value to filter by. ``None`` (the
            default) returns the latest completed run regardless of program file.

    Returns:
        The basename of the matching subdirectory, or ``None`` when no run matches
        or when ``state_dir`` is not a readable directory.
    """
    if not state_dir.is_dir():
        return None
    try:
        candidates = [p for p in state_dir.iterdir() if p.is_dir()]
    except OSError:
        return None
    # Sort by mtime descending — newest first; ties broken by name for determinism.
    # _safe_mtime guards against a dir removed between iterdir and stat (race/TTL sweep).
    candidates.sort(key=lambda p: (_safe_mtime(p), p.name), reverse=True)
    for run_dir in candidates:
        state = _load_state(run_dir / "state.json")
        if state is None:
            continue
        if state.get("status") not in _COMPLETED_STATUSES:
            continue
        if match_program is not None and state.get("program_file", "") != match_program:
            continue
        return run_dir.name
    return None


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Build the CLI argument parser (separate for testability)."""
    parser = argparse.ArgumentParser(
        prog="find_run_id",
        description="Locate the latest completed run id under a state-dir base.",
    )
    parser.add_argument("state_dir_base", help="Directory containing per-run subdirs (each with state.json).")
    parser.add_argument(
        "--match-program",
        default=None,
        metavar="PATH",
        help="Restrict matches to runs whose state.json program_file equals PATH (exact string match).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Find the latest completed run and return the process status.

    Prints the matching run ID to stdout.

    Args:
        argv: Optional argv list (for testing); defaults to ``sys.argv[1:]``.

    Returns:
        ``0`` when a match is printed, ``1`` when no match is found, or ``2`` on
        argument error.
    """
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    try:
        args = _parse_args(argv)
    except SystemExit as exc:
        # argparse exits 2 on bad args; preserve that contract.
        return int(exc.code) if isinstance(exc.code, int) else 2

    state_dir = Path(args.state_dir_base)
    run_id = find_run_id(state_dir, match_program=args.match_program)
    if run_id is None:
        return 1
    sys.stdout.write(run_id + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python
"""codex_state.py — read and bump the research:run Codex co-pilot counters.

The Codex co-pilot phase (``skills/run/modes/codex-copilot.md``) keeps two counters in a small JSON file,
``.experiments/state/<run-id>/codex-state``, because counters tracked in prose are lost to mid-run compaction and
silently re-open the whole Codex budget::

    {"codex_iter": 3, "noop_streak": 1}

``CODEX_DISABLED`` is derived on every read (``noop_streak >= 2``), never stored, so it cannot go stale.

Two subcommands replace the former one-or-two ``read_state_field.py`` forks plus ``printf`` rewrite:

* ``read`` prints ``CODEX_ITER=<n> CODEX_NOOP_STREAK=<n> CODEX_DISABLED=<true|false>`` — the gate check's one line.
* ``bump`` increments ``codex_iter`` and either grows the no-op streak (``--outcome noop``) or resets it to 0
  (``--outcome productive``), rewrites the file atomically, and prints the same line for the new state. It re-reads the
  file itself, so an increment survives a compaction that landed mid-iteration.

A missing file reads as 0/0, so an interrupted run resumes with a fresh budget rather than a hard stop. An unreadable
or malformed file does the same, with a warning on stderr (the former ``read_state_field.py ... || echo 0`` did so
silently).

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/codex_state.py" read --state "$_CODEX_STATE"
    python "${CLAUDE_PLUGIN_ROOT}/bin/codex_state.py" bump --state "$_CODEX_STATE" --outcome noop|productive

Exit codes:
    0   ok (state line printed)
    1   state file could not be written
    2   argument error, or ``--state`` outside the working directory and temp dir
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

#: Consecutive Codex passes without code changes after which Codex is disabled for the rest of the run.
NOOP_STREAK_LIMIT = 2


def _within(path: Path, roots: tuple[Path, ...]) -> bool:
    """Return whether ``path`` resolves under any of ``roots``."""
    resolved = path.resolve()
    return any(resolved.is_relative_to(root.resolve()) for root in roots)


def load_counters(state: Path) -> tuple[int, int]:
    """Return ``(codex_iter, noop_streak)`` from ``state``, ``(0, 0)`` when absent or unusable.

    Args:
        state: Path of the ``codex-state`` JSON file.

    Returns:
        The two counters. A missing file is the normal first-run case and stays silent; an unreadable file, invalid
        JSON, a non-object, or a non-integer counter also yields ``(0, 0)`` but warns on stderr.

    Examples:
        >>> import tempfile
        >>> d = Path(tempfile.mkdtemp())
        >>> load_counters(d / "absent")
        (0, 0)
        >>> _ = (d / "s").write_text('{"codex_iter": 3, "noop_streak": 1}')
        >>> load_counters(d / "s")
        (3, 1)
    """
    try:
        data = json.loads(state.read_text(encoding="utf-8"))
        return int(data.get("codex_iter", 0)), int(data.get("noop_streak", 0))
    except FileNotFoundError:
        return 0, 0
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        print(f"codex_state: {state} unusable ({exc}); reading as 0/0", file=sys.stderr)
        return 0, 0


def render(codex_iter: int, noop_streak: int) -> str:
    """Return the gate-check line for the given counters.

    Examples:
        >>> render(2, 2)
        'CODEX_ITER=2 CODEX_NOOP_STREAK=2 CODEX_DISABLED=true'
        >>> render(2, 1)
        'CODEX_ITER=2 CODEX_NOOP_STREAK=1 CODEX_DISABLED=false'
    """
    disabled = "true" if noop_streak >= NOOP_STREAK_LIMIT else "false"
    return f"CODEX_ITER={codex_iter} CODEX_NOOP_STREAK={noop_streak} CODEX_DISABLED={disabled}"


def bump(state: Path, outcome: str) -> tuple[int, int]:
    """Increment ``codex_iter``, update the streak for ``outcome``, and persist atomically.

    Args:
        state: Path of the ``codex-state`` JSON file; parent directories are created when missing.
        outcome: ``"noop"`` grows the streak, ``"productive"`` resets it to 0.

    Returns:
        The new ``(codex_iter, noop_streak)``.

    Raises:
        OSError: When the file cannot be written.

    Examples:
        >>> import tempfile
        >>> p = Path(tempfile.mkdtemp()) / "run" / "codex-state"
        >>> bump(p, "noop"), bump(p, "noop"), bump(p, "productive")
        ((1, 1), (2, 2), (3, 0))
    """
    codex_iter, noop_streak = load_counters(state)
    new = (codex_iter + 1, noop_streak + 1 if outcome == "noop" else 0)
    state.parent.mkdir(parents=True, exist_ok=True)
    # Same bytes the former printf wrote: json.dumps' default separators match `{"codex_iter": N, "noop_streak": M}`.
    tmp = state.with_name(state.name + ".tmp")
    tmp.write_text(json.dumps({"codex_iter": new[0], "noop_streak": new[1]}) + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, state)
    return new


def main(argv: list[str] | None = None) -> int:
    """Run the ``read`` or ``bump`` subcommand and return the process status.

    Args:
        argv: Optional argument list (tests); defaults to ``sys.argv[1:]``.

    Returns:
        ``0`` on success, ``1`` when the state file cannot be written, ``2`` on an argument or path error.
    """
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    parser = argparse.ArgumentParser(prog="codex_state", description="Read or bump the Codex co-pilot counters.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("read", help="Print the gate-check line.").add_argument("--state", required=True)
    bump_parser = sub.add_parser("bump", help="Increment codex_iter and update the no-op streak.")
    bump_parser.add_argument("--state", required=True)
    bump_parser.add_argument("--outcome", required=True, choices=("noop", "productive"))
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    # A write target, so confine it: the project the run executes in, or the session temp dir.
    state = Path(args.state)
    tmp_fallback = tempfile.gettempdir() if os.name == "nt" else "/tmp"  # noqa: S108 - shell default of ${TMPDIR:-/tmp}
    # Both temp spellings stay roots: the shell's ${TMPDIR:-/tmp} and the platform dir that TEMP/TMP may name.
    roots = (
        Path.cwd(),
        Path(os.environ.get("TMPDIR") or tmp_fallback),
        Path(os.environ.get("TMPDIR") or tempfile.gettempdir()),
    )
    if not _within(state, roots):
        print(f"codex_state: --state {state} is outside the working directory and temp dir", file=sys.stderr)
        return 2

    if args.command == "read":
        print(render(*load_counters(state)))
        return 0
    try:
        counters = bump(state, args.outcome)
    except OSError as exc:
        print(f"codex_state: cannot write {state}: {exc}", file=sys.stderr)
        return 1
    print(render(*counters))
    return 0


if __name__ == "__main__":
    sys.exit(main())

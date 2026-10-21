#!/usr/bin/env python
"""dev_batch_gate.py — decide whether a develop loop runs in batch mode, and record which skill asked.

``/develop:feature`` and ``/develop:refactor`` group non-overlapping edits into fewer test runs (``batch-mode.md``).
Batch mode is on by default; ``--no-batch`` turns it off. Both skills used to carry the same eight-line shell block for
that decision. This script is the block: it reads the skill's ``dev-<skill>-no-batch`` sentinel (written by
``dev_parse_args.py``), and when batch mode is on it writes ``dev-batch-skill``, the sentinel ``batch-mode.md``'s own
fences read back so they never need a literal ``<skill>`` placeholder.

Usage:
    if python dev_batch_gate.py feature; then cat "$_DEV_SHARED/batch-mode.md"; fi

The ``if`` form keeps the skill's Bash call at exit status 0 on the ``--no-batch`` path: a bare ``&&`` chain would end
with this script's status 1, which the Bash tool renders as an error for a normal opt-out.

Output: one verdict line, ``batch mode: on (<skill>)`` or ``batch mode: off (--no-batch)``.

Exit codes:
    0 — batch mode on; ``dev-batch-skill`` written (the caller then loads ``batch-mode.md``)
    1 — batch mode off; nothing written, so a no-batch run never leaves the sentinel behind
    2 — unknown skill name (argparse)
"""

from __future__ import annotations

import argparse
import sys

from dev_run_dir import read_sentinel, write_sentinel

#: Skills that run the batch-mode procedure.
BATCH_SKILLS = ("feature", "refactor")


def batch_enabled(skill: str) -> bool:
    """Return whether batch mode is on for ``skill``: anything but a literal ``false`` in its no-batch sentinel.

    A missing or unreadable sentinel means on — batch mode ships default-on.

    Args:
        skill: Skill name, one of :data:`BATCH_SKILLS`.

    Returns:
        ``False`` only when ``dev-<skill>-no-batch`` holds ``false``.

    Examples:
        >>> import os, tempfile
        >>> from unittest import mock
        >>> with tempfile.TemporaryDirectory() as scratch:
        ...     with mock.patch.dict(os.environ, {"TMPDIR": scratch, "CSID": "d"}):
        ...         batch_enabled("feature")
        True
    """
    return read_sentinel(f"dev-{skill}-no-batch") != "false"


def main(argv: list[str] | None = None) -> int:
    """Print the batch-mode verdict; record the calling skill when batch mode is on.

    Args:
        argv: Command-line arguments; ``None`` reads ``sys.argv``.

    Returns:
        ``0`` when batch mode is on, ``1`` when ``--no-batch`` turned it off.
    """
    parser = argparse.ArgumentParser(description="Decide whether this develop loop runs in batch mode.")
    parser.add_argument("skill", choices=BATCH_SKILLS, help="the calling skill")
    args = parser.parse_args(argv)
    if not batch_enabled(args.skill):
        print("batch mode: off (--no-batch)")
        return 1
    if write_sentinel("dev-batch-skill", args.skill) is None:
        print(
            "dev_batch_gate: could not write dev-batch-skill; batch-mode.md fences fall back to feature",
            file=sys.stderr,
        )
    print(f"batch mode: on ({args.skill})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

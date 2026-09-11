#!/usr/bin/env python
"""parse_kaggle_args.py — parse the kaggle skill's competition name and mode flags.

Extracted from ``skills/kaggle/SKILL.md`` Step 1. Python rather than a shell regex on purpose:
the inline twin used ``[[ =~ ]]`` + ``${BASH_REMATCH[1]}``, and zsh — the harness shell —
populates ``$match`` instead, so ``--type`` and ``--resume`` silently resolved to empty.

The ``--`` separator at the call site is load-bearing: ``$ARGUMENTS`` routinely starts with a
flag, which argparse would otherwise read as an unknown option.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/parse_kaggle_args.py" -- "$ARGUMENTS"

stdout is the parsed summary the skill reads. ``Resume:`` is new — the inline twin parsed the
flag and then never echoed it, so a resumed run gave no sign of which checkpoint it resumed:

    Competition: titanic
    Type: auto-detect
    Resume: none
    EDA only: false | Inference only: false | Offline setup: false

Side effects:
    creates ``.experiments/kaggle/``, clears a stale ``.temp/state/skill-contract.md`` left by a
    crashed prior run (compaction-contract.md §Lifecycle), and writes five sentinels:
    ``kaggle-{competition-name,eda-only,inference-only,offline-setup,keep-items}-${CSID}``.
    All paths are CWD-relative, matching the shell original — the contract and experiment dirs
    belong to the project the skill runs in, not to this script's location.

Exit codes:
    0 — parsed and persisted
    1 — directory creation or sentinel write failed
    2 — argument error (argparse default)
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path

#: Duplicated from ``extract-keep-flag.py`` rather than imported — that script's name is
#: hyphenated and so not importable, and a shared module would add import machinery to a
#: script five skills already depend on. Keep the two regexes in step.
_KEEP_RE = re.compile(r'--keep[ \t\r\f\v]+"([^"]+)"')
_TYPE_RE = re.compile(r"--type[ \t\r\f\v]+([a-z]+)")
_RESUME_RE = re.compile(r"--resume[ \t\r\f\v]+(\S+)")

_CONTRACT = Path(".temp/state/skill-contract.md")
_EXPERIMENTS_DIR = Path(".experiments/kaggle")


def parse_modes(arguments: str) -> tuple[bool, bool, bool]:
    """Resolve the three mutually influencing mode flags.

    Inference is always offline; EDA is always online and therefore overrides ``--offline-setup``.

    Args:
        arguments: Raw ``$ARGUMENTS`` string.

    Returns:
        ``(eda_only, inference_only, offline_setup)``.

    Examples:
        >>> parse_modes("comp --eda-only --offline-setup")
        (True, False, False)
        >>> parse_modes("comp --inference-only")
        (False, True, True)
        >>> parse_modes("comp")
        (False, False, False)
    """
    eda_only = "--eda-only" in arguments
    inference_only = "--inference-only" in arguments
    offline_setup = "--offline-setup" in arguments
    if inference_only:
        offline_setup = True
    if eda_only:
        offline_setup = False
    return eda_only, inference_only, offline_setup


def _first_token(arguments: str) -> str:
    """Return the first whitespace-delimited token, as ``awk '{print $1}'`` did.

    Args:
        arguments: Raw ``$ARGUMENTS`` string.

    Returns:
        The first token, or an empty string.

    Examples:
        >>> _first_token("  titanic --eda-only ")
        'titanic'
        >>> _first_token("")
        ''
    """
    parts = arguments.split()
    return parts[0] if parts else ""


def _search(pattern: re.Pattern[str], arguments: str) -> str:
    """Return the first capture group of ``pattern`` in ``arguments``, or an empty string."""
    match = pattern.search(arguments)
    return match.group(1) if match else ""


def _sentinel_dir() -> Path:
    """Return the session temp directory (never a hardcoded ``/tmp`` — absent on native Windows)."""
    return Path(os.environ.get("TMPDIR") or tempfile.gettempdir())


def _session_token() -> str:
    """Return the sentinel session suffix; ``os.getppid()`` would name the calling shell, not the session."""
    return os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "shared"


def main(argv: list[str] | None = None) -> int:
    """Parse the kaggle arguments, report them, and persist what Steps 3-4 reload.

    Args:
        argv: Optional argv override (defaults to ``sys.argv[1:]``).

    Returns:
        ``0`` on success, ``1`` when a directory or sentinel write fails.
    """
    parser = argparse.ArgumentParser(
        prog="parse_kaggle_args.py",
        description="Parse the kaggle skill's competition name and mode flags.",
    )
    parser.add_argument("arguments", nargs="?", default="", help="Raw $ARGUMENTS string.")
    args = parser.parse_args(argv)
    raw = args.arguments

    competition = _first_token(raw)
    eda_only, inference_only, offline_setup = parse_modes(raw)
    problem_type = _search(_TYPE_RE, raw)
    resume = _search(_RESUME_RE, raw)
    keep_items = _search(_KEEP_RE, raw)

    print(
        f"Competition: {competition}\n"
        f"Type: {problem_type or 'auto-detect'}\n"
        f"Resume: {resume or 'none'}\n"
        f"EDA only: {str(eda_only).lower()} | Inference only: {str(inference_only).lower()} "
        f"| Offline setup: {str(offline_setup).lower()}"
    )

    tmp = _sentinel_dir()
    csid = _session_token()
    payload = {
        "competition-name": competition,
        "eda-only": str(eda_only).lower(),
        "inference-only": str(inference_only).lower(),
        "offline-setup": str(offline_setup).lower(),
        "keep-items": keep_items,
    }
    try:
        _EXPERIMENTS_DIR.mkdir(parents=True, exist_ok=True)
        _CONTRACT.unlink(missing_ok=True)
        for name, value in payload.items():
            # Trailing newline is the `IFS= read -r` reload contract; without it read exits
            # non-zero and the `|| VAR=...` fallback wipes the value.
            (tmp / f"kaggle-{name}-{csid}").write_text(value + "\n", encoding="utf-8", newline="\n")
    except OSError as exc:
        print(f"parse_kaggle_args: cannot persist state: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

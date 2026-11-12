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
    crashed prior run (compaction-contract.md §Lifecycle), and writes seven sentinels:
    ``kaggle-{competition-name,eda-only,inference-only,offline-setup,explicit-offline-setup,resume,keep-items}-${CSID}``.
    ``resume`` holds the ``--resume`` path (empty when absent) so Step 3 can tell the generating
    agent to extend that script instead of overwriting it.
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
import shlex
import sys
import tempfile
from pathlib import Path

#: Sibling import: bin/ is not a package. Putting it on the path resolves the private writer whether this file runs as
#: a script, under the test conftest, or as a doctest module.
_BIN_DIR = str(Path(__file__).resolve().parent)
if _BIN_DIR not in sys.path:
    sys.path.insert(0, _BIN_DIR)

from _sentinel_io import write_private_text  # noqa: E402

#: Compaction-contract file that is removed when the skill starts a fresh run.
_CONTRACT = Path(".temp/state/skill-contract.md")
#: Directory holding Kaggle experiment runs, created if missing when a run starts.
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
    options = _options(arguments)
    eda_only = "--eda-only" in options
    inference_only = "--inference-only" in options
    offline_setup = "--offline-setup" in options
    if inference_only:
        offline_setup = True
    if eda_only:
        offline_setup = False
    return eda_only, inference_only, offline_setup


def _options(arguments: str) -> dict[str, str]:
    """Extract standalone options while preserving quoted descriptions as data."""
    lexer = shlex.shlex(arguments, posix=False)
    lexer.whitespace_split = True
    lexer.commenters = ""
    tokens = iter(lexer)
    options = {}
    for argument in tokens:
        if argument == "--":
            break
        if argument in {"--type", "--resume", "--keep"}:
            value = next(tokens, "")
            options[argument] = value[1:-1] if value[:1] in {"'", '"'} and value[-1:] == value[:1] else value
        elif argument in {"--eda-only", "--inference-only", "--offline-setup"}:
            options[argument] = "true"
    return options


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


def _sentinel_dir() -> Path:
    """Return the session temp directory (never a hardcoded ``/tmp`` — absent on native Windows)."""
    tmp_fallback = tempfile.gettempdir() if os.name == "nt" else "/tmp"  # noqa: S108 - shell default of ${TMPDIR:-/tmp}
    return Path(os.environ.get("TMPDIR") or tmp_fallback)


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
    csid = _session_token()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", csid) or ".." in csid:
        print("parse_kaggle_args: invalid sentinel session identity", file=sys.stderr)
        return 2
    raw = args.arguments

    competition = _first_token(raw)
    if (
        not re.fullmatch(r"[A-Za-z0-9._-]+", competition)
        or competition.startswith("-")
        or competition == "."
        or ".." in competition
    ):
        parser.error("competition must be a name using letters, digits, '.', '_' or '-' without '..'")
    try:
        options = _options(raw)
        eda_only, inference_only, offline_setup = parse_modes(raw)
    except ValueError as exc:
        parser.error(str(exc))
    problem_type = options.get("--type", "")
    resume = options.get("--resume", "")
    if resume:
        try:
            resume = Path(resume).expanduser().resolve().relative_to(Path.cwd().resolve()).as_posix()
        except (OSError, ValueError, RuntimeError):
            parser.error("--resume must stay within the project tree")
    keep_items = options.get("--keep", "")

    print(
        f"Competition: {competition}\n"
        f"Type: {problem_type or 'auto-detect'}\n"
        f"Resume: {resume or 'none'}\n"
        f"EDA only: {str(eda_only).lower()} | Inference only: {str(inference_only).lower()} "
        f"| Offline setup: {str(offline_setup).lower()}"
    )

    tmp = _sentinel_dir()
    payload = {
        "competition-name": competition,
        "eda-only": str(eda_only).lower(),
        "inference-only": str(inference_only).lower(),
        "offline-setup": str(offline_setup).lower(),
        "explicit-offline-setup": str("--offline-setup" in options).lower(),
        "resume": resume,
        "keep-items": keep_items,
    }
    try:
        _EXPERIMENTS_DIR.mkdir(parents=True, exist_ok=True)
        _CONTRACT.unlink(missing_ok=True)
        for name, value in payload.items():
            # Trailing newline is the `IFS= read -r` reload contract; without it read exits
            # non-zero and the `|| VAR=...` fallback wipes the value.
            write_private_text(tmp / f"kaggle-{name}-{csid}", value + "\n")
    except OSError as exc:
        print(f"parse_kaggle_args: cannot persist state: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

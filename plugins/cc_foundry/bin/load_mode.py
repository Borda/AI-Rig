#!/usr/bin/env python3
"""load_mode.py — resolve a skill's ``modes/``/``templates/`` dir and emit one file from it.

Collapses the two-line skill idiom

.. code-block:: bash

    X=$(python "$ROOT/bin/resolve_skill_subdir.py" <skill> <subdir>) || { printf "! BREAKING: ...\\n"; exit 1; }
    cat "$X/<file>.md"

into a single call. Resolution is **not** reimplemented here — the cascade is
imported from :mod:`resolve_skill_subdir` so the two can never drift.

Failure behaviour mirrors the inline form it replaces: a ``! BREAKING`` line on
**stdout** (the inline ``printf`` had no redirect) and exit 1.

Usage:
    python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/load_mode.py" \
        <skill> <subdir> <file.md> [--local] [--fallback-source]

Arguments:
    skill   Skill directory name (e.g. ``audit``, ``calibrate``, ``distill``).
    subdir  Subdirectory to resolve — ``modes`` or ``templates``.
    file    File name inside that subdirectory (plain name; no path separators).

Options:
    --local             Prefer the local source tree (plugin-dev workflow); forwarded verbatim
                        to the ``resolve_skill_subdir`` cascade.
    --fallback-source   Add this script's own sibling ``skills/<skill>/<subdir>`` as a final
                        tier when every cascade tier misses. Reproduces the softer inline
                        variant ``... 2>/dev/null || echo "plugins/cc_foundry/skills/<skill>/<subdir>"``.
                        Omit it at sites that must fail loudly.

Exit codes:
    0  Success — file content written to stdout
    1  Subdirectory or file not found
    2  Argument error
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from resolve_skill_subdir import _validate_token, resolve

_BREAKING_HINT = "run /foundry:setup first"


def validate_file_name(name: str) -> None:
    """Reject path separators and traversal segments in the file argument.

    Only plain file names are accepted; the resolved directory is the security
    boundary, so anything that could escape it is refused outright rather than
    silently sanitised.

    Args:
        name: File name supplied on the command line.

    Raises:
        ValueError: When ``name`` is empty, contains a path separator, or is a
            traversal segment.

    Examples:
        >>> validate_file_name("breakdown.md")
        >>> validate_file_name("")
        Traceback (most recent call last):
            ...
        ValueError: file must not be empty
        >>> validate_file_name("../modes/x.md")
        Traceback (most recent call last):
            ...
        ValueError: file '../modes/x.md' must be a plain file name (no path separators)
        >>> validate_file_name("..")
        Traceback (most recent call last):
            ...
        ValueError: file '..' must be a plain file name (no path separators)
    """
    if not name:
        raise ValueError("file must not be empty")
    if "/" in name or "\\" in name or name in {".", ".."}:
        raise ValueError(f"file {name!r} must be a plain file name (no path separators)")


def source_fallback_dir(skill: str, subdir: str) -> Path | None:
    """Return this install's own ``skills/<skill>/<subdir>``, when it exists.

    Derived from ``__file__`` rather than a literal ``plugins/cc_foundry`` path so
    it resolves identically from an installed plugin cache, a source checkout, or
    any working directory.

    Args:
        skill: Skill directory name.
        subdir: Subdirectory under the skill.

    Returns:
        The sibling directory when present, else ``None``.

    Examples:
        >>> source_fallback_dir("audit", "templates") is not None
        True
        >>> source_fallback_dir("__nonexistent__", "modes") is None
        True
    """
    candidate = Path(__file__).resolve().parent.parent / "skills" / skill / subdir
    return candidate if candidate.is_dir() else None


def locate(skill: str, subdir: str, *, local: bool, fallback_source: bool) -> Path | None:
    """Resolve the skill subdirectory, optionally falling back to the source tree.

    Args:
        skill: Skill directory name.
        subdir: Subdirectory under the skill (``modes`` or ``templates``).
        local: Forwarded to the cascade — prefer the local source tree first.
        fallback_source: Append the ``__file__``-derived sibling directory as a
            final tier when the cascade misses.

    Returns:
        The resolved directory, or ``None`` when nothing matched.

    Examples:
        >>> locate("__nonexistent__", "modes", local=False, fallback_source=True) is None
        True
    """
    hit = resolve(skill, subdir, local=local)
    if hit is not None:
        return hit
    return source_fallback_dir(skill, subdir) if fallback_source else None


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser.

    Returns:
        Configured :class:`argparse.ArgumentParser`.

    Examples:
        >>> _build_parser().parse_args(["audit", "modes", "fix.md"]).subdir
        'modes'
    """
    parser = argparse.ArgumentParser(
        prog="load_mode",
        description="Resolve a skill's modes/templates dir and emit one file from it.",
    )
    parser.add_argument("skill", help="Skill directory name (e.g. audit, calibrate).")
    parser.add_argument("subdir", help="Subdirectory to resolve (modes or templates).")
    parser.add_argument("file", help="File name inside that subdirectory (plain name).")
    parser.add_argument("--local", action="store_true", help="Prefer the local source tree (plugin-dev workflow).")
    parser.add_argument(
        "--fallback-source",
        action="store_true",
        help="Fall back to this install's own skills/<skill>/<subdir> when every cascade tier misses.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Optional argv override (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code (0 success, 1 not found, 2 argument error).
    """
    args = _build_parser().parse_args(argv)

    try:
        _validate_token(args.skill, "skill")
        _validate_token(args.subdir, "subdir")
        validate_file_name(args.file)
    except ValueError as exc:
        print(f"load_mode: {exc}", file=sys.stderr)
        return 2

    resolved = locate(args.skill, args.subdir, local=args.local, fallback_source=args.fallback_source)
    if resolved is None:
        print(f"! BREAKING: {args.skill}/{args.subdir} not found — {_BREAKING_HINT}")
        return 1

    target = resolved / args.file
    if not target.is_file():
        print(f"! BREAKING: {args.file} not found in {resolved.as_posix()} — {_BREAKING_HINT}")
        return 1

    # Byte-for-byte passthrough — matches `cat`, no encoding or newline rewriting.
    sys.stdout.buffer.write(target.read_bytes())
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""derive_codemap_target.py — read a skill goal and name the codemap target it points at.

Emits shell assignments for eval, following the shape of ``parse-skill-flags.py``::

    eval "$(python derive_codemap_target.py "$ARGUMENTS")"

Two spellings are recognised in the goal text, in this order:

- ``module.path::function`` — sets ``TARGET_MODULE`` to the part before ``::`` and
  ``TARGET_FN`` to the bare function name. ``codemap-context.md`` rebuilds ``module::fn``
  from the pair, so the function name is emitted without its module prefix.
- ``module.path`` — a dotted name of two or more segments. Sets ``TARGET_MODULE`` and
  leaves ``TARGET_FN`` empty.

Neither present leaves both empty, which the callers read as "affected surface unknown" and
run only the central baseline query. That is the correct reading: a goal like ``fix the
login timeout`` names no target, and guessing one would send caller-impact queries after
a symbol the user never mentioned.

Replaces an inline ``[[ =~ ]]`` + ``${BASH_REMATCH[1]}`` pair duplicated in ``plan`` and
``feature``. That idiom captured nothing under zsh, which populates ``match`` rather than
``BASH_REMATCH``, so the dotted-module branch silently produced an empty target there.

Exit codes: 0 — assignments emitted (always; an unrecognised goal is not an error)
"""

from __future__ import annotations

import argparse
import re
import shlex
import sys
from typing import Final

# A dotted Python-ish path, then `::`, then a bare function name. The module half allows
# dots so `pkg.mod::fn` keeps `pkg.mod` together; the function half does not, so a trailing
# `.` in prose cannot be pulled into the name.
_QUALIFIED: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*::[A-Za-z_][A-Za-z0-9_]*")

# Two or more dot-joined identifier segments. Requiring the second segment keeps ordinary
# prose ending in a period from reading as a module path.
_DOTTED: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")


def derive_target(goal: str) -> tuple[str, str]:
    """Return the ``(module, function)`` pair named by ``goal``.

    Args:
        goal: Free-form goal text, typically a skill's argument blob.

    Returns:
        Tuple of module path and bare function name; either may be empty.

    Examples:
        >>> derive_target("extend auth.tokens::refresh for rotation")
        ('auth.tokens', 'refresh')
        >>> derive_target("speed up pkg.mod.submodule")
        ('pkg.mod.submodule', '')
        >>> derive_target("fix the login timeout")
        ('', '')
        >>> derive_target("rewrite this. and that.")
        ('', '')
    """
    qualified = _QUALIFIED.search(goal)
    if qualified:
        module, _, function = qualified.group(0).partition("::")
        return module, function

    dotted = _DOTTED.search(goal)
    return (dotted.group(0), "") if dotted else ("", "")


def main(argv: list[str]) -> int:
    """Emit ``TARGET_MODULE`` and ``TARGET_FN`` assignments for the goal in ``argv``.

    Args:
        argv: Raw argv tokens (``sys.argv[1:]``); the goal blob may start with ``-``.

    Returns:
        Always ``0``.

    Examples:
        No doctest — writes to stdout; covered by pytest via subprocess.
    """
    parser = argparse.ArgumentParser(
        prog="derive_codemap_target.py",
        description="Derive TARGET_MODULE/TARGET_FN from a skill goal, emit shell assignments.",
        # No -h/--help: callers run this inside eval "$(...)", so argparse's help would be
        # printed to stdout and then executed as shell source, leaving TARGET_MODULE and
        # TARGET_FN unset. A goal that is exactly `--help` is goal text like any other.
        add_help=False,
    )
    # parse_known_args so a goal beginning with a dash is never read as an option of this
    # script, matching parse-skill-flags.py.
    _, extra = parser.parse_known_args(argv)
    module, function = derive_target(" ".join(extra))
    print(f"TARGET_MODULE={shlex.quote(module)}")
    print(f"TARGET_FN={shlex.quote(function)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

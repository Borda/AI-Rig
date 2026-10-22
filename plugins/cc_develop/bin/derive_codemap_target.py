#!/usr/bin/env python3
"""derive_codemap_target.py — read a skill goal and name the codemap target it points at.

Emits shell assignments by default, or writes private per-session values with ``--keep-state``::

    python derive_codemap_target.py --keep-state dev-target "$ARGUMENTS"

State mode writes ``<prefix>-target-module-<CSID>`` and ``<prefix>-target-fn-<CSID>`` in ``TMPDIR`` (or the
platform temp directory), including empty values to clear stale state. Callers use ``IFS= read -r``; stdout stays
empty. The existing develop atomic writer creates private files and replaces planted links without following them.

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

Exit codes:
    0 — assignments emitted or state written; an unrecognised goal is not an error.
    1 — invalid state identity or state write failure.
    2 — missing option value (argparse usage error).
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import sys
from typing import Final

from dev_run_dir import sentinel_path, write_atomic

# A dotted Python-ish path, then `::`, then a bare function name. The module half allows
# dots so `pkg.mod::fn` keeps `pkg.mod` together; the function half does not, so a trailing
# `.` in prose cannot be pulled into the name.
#: Matches a qualified function reference of the form module.path::function in free-text goal text.
_QUALIFIED: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*::[A-Za-z_][A-Za-z0-9_]*")

# Two or more dot-joined identifier segments. Requiring the second segment keeps ordinary
# prose ending in a period from reading as a module path.
#: Matches a dotted module path of two or more identifier segments in free-text goal text.
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
    """Emit codemap target assignments or persist the two target values for shell readers.

    Args:
        argv: Raw argv tokens (``sys.argv[1:]``); the goal blob may start with ``-``.

    Returns:
        ``0`` on success, ``1`` on a state validation/write error.

    Examples:
        No doctest — writes to stdout; covered by pytest via subprocess.
    """
    parser = argparse.ArgumentParser(
        prog="derive_codemap_target.py",
        description="Derive TARGET_MODULE/TARGET_FN from a skill goal, emit assignments or session state.",
        # Preserve the legacy argument contract: a goal exactly `--help` remains goal text.
        add_help=False,
        # no prefix matching: a goal starting `--keep x` must stay goal text, not become `--keep-state`
        allow_abbrev=False,
    )
    parser.add_argument("--keep-state", metavar="PREFIX", help="Write private session values instead of assignments.")
    # parse_known_args so a goal beginning with a dash is never read as an option of this
    # script, matching parse-skill-flags.py.
    args, extra = parser.parse_known_args(argv)
    module, function = derive_target(" ".join(extra))
    if args.keep_state is not None:
        csid = os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "shared"
        # sentinel_path strips unsupported session characters; reject them instead so the shell reads the same name.
        if any(not re.fullmatch(r"[A-Za-z0-9_-]+", value) for value in (args.keep_state, csid)):
            print("derive_codemap_target: --keep-state prefix and CSID must match [A-Za-z0-9_-]+", file=sys.stderr)
            return 1
        try:
            for name, value in (("target-module", module), ("target-fn", function)):
                write_atomic(sentinel_path(f"{args.keep_state}-{name}"), value)
        except OSError as exc:
            print(f"derive_codemap_target: cannot write state: {exc}", file=sys.stderr)
            return 1
    else:
        print(f"TARGET_MODULE={shlex.quote(module)}")
        print(f"TARGET_FN={shlex.quote(function)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

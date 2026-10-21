#!/usr/bin/env python
"""codemap_changed_modules.py — name the codemap modules behind the Python files a branch changed.

Agents that ask ``codemap-py`` structural questions about a branch's work all begin the same way: find what changed
since the branch point, turn those files into the module names the index knows, and run one query per module. This
script is that shared first half. The per-module query loop stays in each agent, because the queries differ.

Usage:
    python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/codemap_changed_modules.py" [--base REF] [--cap N]

    --base REF   Ref to diff against. Default: the remote's default branch (``origin/HEAD``), else ``origin/main``.
    --cap N      Most module names to print. Default 10.

Output (stdout): one module name per line, in changed-file order, de-duplicated. Nothing when ``codemap-py`` is not
installed, nothing changed, the index does not know the changed files, or the index query fails. Always exit 0, so a
caller can wrap it in an unquoted ``$(...)`` loop without a guard; argument errors exit 2.

Module names come from the index's own ``name`` field via ``resolve_centrality``, never from a path transform: a
``sed`` that turns ``pkg/__init__.py`` into ``pkg.__init__`` names a module the index never had, and a query for it is
a guaranteed error. A changed file the index does not know (new, or not Python) contributes nothing rather than a
guessed name.

The diff is ``<merge-base>..HEAD``: committed changes only, as the inline blocks this replaces. When there is no merge
base (no remote ref, unrelated history) it falls back to ``HEAD~1``. Paths are read NUL-separated, so a name that git
would otherwise quote or that contains a comma still resolves.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from resolve_centrality import build_maps, ordered_modules

#: Fallback comparison ref when the remote does not advertise a default branch.
FALLBACK_BASE = "origin/main"
#: Default number of modules printed; the per-module query loop in each agent is bounded by it.
DEFAULT_CAP = 10
#: Upper bounds for the child processes, so a stuck git or index query never holds an agent's pre-flight.
_GIT_TIMEOUT_S = 30
_CODEMAP_TIMEOUT_S = 60


def _run(command: list[str], timeout: float) -> str | None:
    """Run a command and return its stdout; None when it cannot start, times out, or exits non-zero."""
    try:
        result = subprocess.run(  # noqa: S603 - argv list, no shell; git or the resolved codemap-py executable
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def default_base() -> str:
    """Return the remote's default branch ref, or ``origin/main`` when git does not know one."""
    ref = _run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], _GIT_TIMEOUT_S)
    return ref.strip() if ref and ref.strip() else FALLBACK_BASE


def comparison_point(base: str) -> str | None:
    """Return the commit to diff from: the merge base with ``base``, else ``HEAD~1``, else None."""
    for command in (["git", "merge-base", "HEAD", base], ["git", "rev-parse", "HEAD~1"]):
        out = _run(command, _GIT_TIMEOUT_S)
        if out and out.strip():
            return out.strip()
    return None


def changed_python_files(point: str) -> list[str]:
    """Return the ``.py`` paths changed between ``point`` and ``HEAD``, in git's order."""
    out = _run(["git", "diff", "-z", "--name-only", f"{point}..HEAD"], _GIT_TIMEOUT_S)
    return [path for path in (out or "").split("\0") if path.endswith(".py")]


def _central_payload(codemap: str) -> object:
    """Return the parsed ``codemap-py query central`` payload covering every indexed module; None on any failure."""
    out = _run([codemap, "query", "--timeout", "10", "central", "--top", "100000"], _CODEMAP_TIMEOUT_S)
    try:
        return json.loads(out) if out else None
    except (ValueError, RecursionError):
        return None


def changed_modules(base: str | None = None, cap: int = DEFAULT_CAP) -> list[str]:
    """Return the indexed module names for the Python files changed since the branch point.

    Args:
        base: Ref to diff against; None detects the remote's default branch.
        cap: Most names to return.

    Returns:
        Module names in changed-file order, at most ``cap``; empty when anything needed is unavailable.
    """
    codemap = shutil.which("codemap-py")
    if codemap is None:
        return []
    point = comparison_point(base or default_base())
    files = changed_python_files(point) if point else []
    if not files:
        return []
    central = _central_payload(codemap)
    if not isinstance(central, dict):
        return []
    return ordered_modules(build_maps(central, files))[:cap]


def _positive_int(raw: str) -> int:
    """Parse a strictly positive integer for argparse."""
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {raw}")
    return value


def main(argv: list[str] | None = None) -> int:
    """Print the changed modules, one per line.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        0 always once the arguments parse.

    Examples:
        No doctest — spawns git and codemap-py; covered by pytest against a temporary repository.
    """
    parser = argparse.ArgumentParser(prog="codemap_changed_modules.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", help="Ref to diff against (default: the remote's default branch, else origin/main).")
    parser.add_argument("--cap", type=_positive_int, default=DEFAULT_CAP, help="Most module names to print.")
    args = parser.parse_args(argv)
    for name in changed_modules(args.base, args.cap):
        print(name)
    return 0


if __name__ == "__main__":
    sys.exit(main())

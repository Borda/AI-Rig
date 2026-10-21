#!/usr/bin/env python
"""compaction_boundary.py — print a skill's compaction Boundary step, then run the optional foundry compaction gate.

Every phased skill that refreshes its compaction contract ends the boundary's Bash block the same way: show the
``## Boundary step`` section of its own ``compaction-contract.md``, then ask the foundry plugin's ``compact_gate.py``
whether compacting now would pay off. The skill follows the printed section and acts on the single
``compact-gate:`` decision line that follows it.

Usage:
    python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_<plugin>}/bin/compaction_boundary.py" ["$_SHARED_DIR"]

``SHARED_DIR`` is the plugin's resolved ``skills/_shared`` directory (the caller's sentinel read). It is optional and
may be empty: the contract is then taken from this script's own plugin, ``<plugin>/skills/_shared`` beside ``bin/``.
This script is propagated byte-identical into each plugin's ``bin/``, so that fallback names the owning plugin without
a per-plugin literal and keeps working from an installed copy, not only from a repository checkout.

Output (stdout):
    The ``## Boundary step`` section of the contract (heading included, ``## Acceptance criteria`` excluded), then the
    gate's own ``compact-gate:`` decision line (``continue``, ``advise`` or ``stop``). When the provider is missing, or
    the gate exits non-zero, this script prints ``compact-gate: continue (foundry gate unavailable)`` as the last line.

Optional-provider contract (``plugins/CLAUDE.md`` §Self-Contained ``_shared``, instance 2):
    The foundry gate is resolved through the active install record with ``resolve_shared_path.py foundry bin``, the
    sibling resolver in this directory. The consumer's feature is a no-op without the provider, and absence degrades to
    the fallback line above. Nothing is read from another plugin's tree except the provider's ``compact_gate.py``,
    which is executed, never imported.

Exit codes:
    0 — always, including a missing contract (a note goes to stderr) and an unavailable gate; 2 — bad arguments.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

#: Name of the contract file every plugin ships in its own ``skills/_shared``.
_CONTRACT_NAME = "compaction-contract.md"
#: Heading that opens the section to print, and the heading that closes it (excluded from the output).
_START_HEADING = "## Boundary step"
_END_HEADING = "## Acceptance criteria"
#: Decision line printed when the provider or its gate cannot run; mirrors the gate's own ``continue`` decision.
_FALLBACK_LINE = "compact-gate: continue (foundry gate unavailable)"
#: This plugin's root; the fallback contract lives under ``skills/_shared`` beside ``bin/``.
_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
#: Sibling resolver, propagated into every plugin's ``bin/``.
_RESOLVER = Path(__file__).resolve().with_name("resolve_shared_path.py")
#: Upper bounds for the two child processes, so a hung provider can never hold a skill at its boundary.
_RESOLVE_TIMEOUT_S = 30
_GATE_TIMEOUT_S = 60


def boundary_excerpt(text: str) -> str:
    """Return the ``## Boundary step`` section of a compaction contract.

    The section runs from its heading line (included) to the line before ``## Acceptance criteria`` (excluded), or to
    the end of the text when that heading is missing. A repeated ``## Boundary step`` heading after the section ends
    opens another one, exactly as the ``sed`` range this replaces behaves. Only ``\\n`` separates lines, and a trailing
    ``\\r`` does not stop a heading from matching, so a CRLF checkout still works.

    Args:
        text: Full contract text.

    Returns:
        The selected lines, newline-terminated as in ``text``; empty when the contract has no such heading.

    Examples:
        >>> boundary_excerpt("## A\\nx\\n## Boundary step\\n1. go\\n## Acceptance criteria\\nz\\n")
        '## Boundary step\\n1. go\\n'
        >>> boundary_excerpt("## Boundary step\\nlast line")
        '## Boundary step\\nlast line'
        >>> boundary_excerpt("no section\\n")
        ''
    """
    kept: list[str] = []
    inside = False
    for line in re.findall(r"[^\n]*\n|[^\n]+", text):
        heading = line.rstrip("\r\n")
        if not inside:
            inside = heading == _START_HEADING
            if inside:
                kept.append(line)
        elif heading == _END_HEADING:
            inside = False
        else:
            kept.append(line)
    return "".join(kept)


def find_contract(shared_dir: str) -> Path | None:
    """Return the compaction contract to print: the caller's shared dir first, then this plugin's own.

    Args:
        shared_dir: The caller's resolved ``skills/_shared`` directory; empty when its sentinel was missing.

    Returns:
        Path of the first existing candidate, or None when neither holds the contract.
    """
    candidates = [_PLUGIN_ROOT / "skills" / "_shared" / _CONTRACT_NAME]
    if shared_dir:
        candidates.insert(0, Path(shared_dir) / _CONTRACT_NAME)
    return next((path for path in candidates if path.is_file()), None)


def provider_gate() -> Path | None:
    """Return the foundry provider's ``compact_gate.py`` when the active install record resolves one.

    Returns:
        The gate script path, or None when the resolver is missing, fails, times out, or names a directory without
        ``compact_gate.py``.
    """
    if not _RESOLVER.is_file():
        return None
    try:
        resolved = subprocess.run(  # noqa: S603 - argv list, no shell; fixed sibling resolver
            [sys.executable, str(_RESOLVER), "foundry", "bin"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_RESOLVE_TIMEOUT_S,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    directory = resolved.stdout.rstrip("\r\n")
    if resolved.returncode != 0 or not directory:
        return None
    gate = Path(directory) / "compact_gate.py"
    return gate if gate.is_file() else None


def run_gate() -> bool:
    """Run the provider's gate with inherited streams, so its decision line and any crash stay visible.

    Returns:
        True when the gate ran and exited 0; False when the provider is unavailable, the gate timed out, or it failed.
    """
    gate = provider_gate()
    if gate is None:
        return False
    sys.stdout.flush()
    try:
        completed = subprocess.run(  # noqa: S603 - argv list, no shell; gate path resolved through the install record
            [sys.executable, str(gate)], check=False, timeout=_GATE_TIMEOUT_S
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def main(argv: list[str] | None = None) -> int:
    """Print the Boundary step, then the compaction gate decision.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        0 in every case that parses; the process is a no-op for the skill when the contract or gate is missing.

    Examples:
        No doctest — prints and spawns the provider's gate; covered by pytest with a stubbed provider.
    """
    parser = argparse.ArgumentParser(prog="compaction_boundary.py", description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "shared_dir",
        nargs="?",
        default="",
        help="The plugin's resolved skills/_shared directory; empty or omitted uses this plugin's own.",
    )
    args = parser.parse_args(argv)

    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", newline="\n")  # the contract carries non-ASCII punctuation
    contract = find_contract(args.shared_dir)
    try:
        sys.stdout.write(boundary_excerpt(contract.read_text(encoding="utf-8")) if contract else "")
    except (OSError, UnicodeDecodeError):
        contract = None
    if contract is None:
        print(f"! {_CONTRACT_NAME} not found or unreadable — Boundary step not printed", file=sys.stderr)
    if not run_gate():
        print(_FALLBACK_LINE)
    return 0


if __name__ == "__main__":
    sys.exit(main())

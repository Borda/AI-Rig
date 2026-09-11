#!/usr/bin/env python
"""audit_preflight.py — parse /foundry:audit arguments and prepare its run state.

Replaces the inline pre-flight block. It does four things:

1. Splits ``--keep "<items>"`` and the mode flags out of the raw argument string,
   leaving the scope tokens.
2. Rejects ``--upgrade`` combined with ``--adversarial`` or ``--efficiency``.
3. Probes ``jq``, ``git`` and ``node``, memoising each result for four hours
   under ``.temp/state/preflight/`` so repeat invocations skip the lookup.
4. Resolves the audit ``templates/`` directory and writes the session sentinels
   the later steps read back (``local-mode``, ``audit-tpl``, ``keep-items``),
   then clears any stale skill contract. The scope tokens are printed rather than
   persisted, as in the shell original — nothing re-derives scope across blocks.

Flags are parsed by exact token, not substring, so a scope token containing a
flag name is not mistaken for the flag. The shell original used ``grep``/``sed``
rather than ``[[ =~ ]]`` because zsh leaves ``BASH_REMATCH`` empty on a match;
that trap does not exist here.

stdout is the state summary the skill reads:

    local-mode=false adversarial=false efficiency=true upgrade=false skip-gate=false fast=false
    tools: jq=yes git=yes node=no
    audit-tpl=/path/to/templates
    keep-items=
    scope=plugins

Usage:
    audit_preflight.py --arguments "<raw $ARGUMENTS>"

Exit codes:
    0   pre-flight complete
    1   mutually exclusive flags, no .claude/ directory, or templates unresolvable
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

from resolve_skill_subdir import resolve as resolve_subdir

#: How long a successful tool probe stays valid, in seconds.
PREFLIGHT_TTL = 4 * 60 * 60

_KEEP = re.compile(r'--keep\s+"([^"]*)"')

#: Flag token → the state key it sets. `--challenge` is an alias of `--adversarial`.
FLAGS: dict[str, str] = {
    "--local": "local-mode",
    "--adversarial": "adversarial",
    "--challenge": "adversarial",
    "--efficiency": "efficiency",
    "--upgrade": "upgrade",
    "--skip-gate": "skip-gate",
    "--fast": "fast",
}

STATE_KEYS = ("local-mode", "adversarial", "efficiency", "upgrade", "skip-gate", "fast")


def _temp_base() -> Path:
    return Path(os.environ.get("TMPDIR") or tempfile.gettempdir())


def _session_token() -> str:
    return os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "shared"


def parse_arguments(raw: str) -> tuple[dict[str, bool], str, str]:
    """Split a raw argument string into (flag state, keep items, scope tokens)."""
    keep_match = _KEEP.search(raw)
    keep_items = keep_match.group(1) if keep_match else ""
    without_keep = _KEEP.sub(" ", raw)

    state = dict.fromkeys(STATE_KEYS, False)
    scope: list[str] = []
    for token in without_keep.split():
        key = FLAGS.get(token)
        if key:
            state[key] = True
        else:
            scope.append(token)
    return state, keep_items, " ".join(scope)


def probe_tool(name: str, *, state_dir: Path) -> bool:
    """Report whether a tool is available, memoising a hit for PREFLIGHT_TTL."""
    marker = state_dir / f"{name}.ok"
    try:
        stamp = int(marker.read_text(encoding="utf-8").strip())
        if time.time() - stamp < PREFLIGHT_TTL:
            return True
    except (OSError, ValueError):
        pass
    if shutil.which(name) is None:
        return False
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(int(time.time())), encoding="utf-8", newline="\n")
    except OSError:
        pass
    return True


def write_state(values: dict[str, str]) -> Path:
    """Write the session sentinels the later audit steps read back."""
    state_dir = _temp_base() / f"audit-state-{_session_token()}"
    state_dir.mkdir(parents=True, exist_ok=True)
    for name, value in values.items():
        (state_dir / name).write_text(f"{value}\n", encoding="utf-8", newline="\n")
    return state_dir


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--arguments", default="")
    parser.add_argument("--claude-dir", default=".claude")
    args = parser.parse_args()

    state, keep_items, scope = parse_arguments(args.arguments)

    if state["upgrade"] and (state["adversarial"] or state["efficiency"]):
        print("! --upgrade is mutually exclusive with --adversarial and --efficiency")
        return 1
    if not Path(args.claude_dir).is_dir():
        print("! BREAKING: .claude/ directory not found — nothing to audit")
        return 1

    preflight_dir = Path(".temp/state/preflight")
    tools = {name: probe_tool(name, state_dir=preflight_dir) for name in ("jq", "git", "node")}
    if not tools["jq"]:
        print("⚠ MISSING: jq not found — Check 4 (permissions-guide drift) will be skipped")
    if not tools["git"]:
        print("⚠ MISSING: git not found — path portability check may miss repo-root references")
    if not tools["node"]:
        print("⚠ MISSING: node not found — the upgrade-mode hook syntax check will be skipped")

    templates = resolve_subdir("audit", "templates", local=state["local-mode"])
    if templates is None:
        print("! BREAKING: audit/templates not found — run /foundry:setup first")
        return 1

    Path(".temp/state/skill-contract.md").unlink(missing_ok=True)
    write_state(
        {
            "local-mode": str(state["local-mode"]).lower(),
            "audit-tpl": str(templates),
            "keep-items": keep_items,
        }
    )

    print(" ".join(f"{key}={str(state[key]).lower()}" for key in STATE_KEYS))
    print("tools: " + " ".join(f"{name}={'yes' if ok else 'no'}" for name, ok in tools.items()))
    print(f"audit-tpl={templates}")
    print(f"keep-items={keep_items}")
    print(f"scope={scope}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

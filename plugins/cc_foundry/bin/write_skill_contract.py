#!/usr/bin/env python
"""write_skill_contract.py — write the compaction-boundary contract the PreCompact hook appends verbatim.

Only the skeleton is shared. `preserve` and `next` stay bespoke per boundary by design
(compaction.md §Verbatim-file principle) — this script parameterises the mkdir + write
around them, never the prose itself.

Argparse is deliberately not used: `preserve` and `next` routinely begin with a dash or
carry flag-like text, which argparse would read as options. `-h`/`--help` is handled
explicitly instead, so the bin/ help contract still holds.

Usage: python write_skill_contract.py <skill> <phase> <run-dir> <preserve> <next>
       python write_skill_contract.py <skill> <phase> <run-dir> <preserve> <next> <label> <items>
  Callers keep their own sentinel reads: the `${_OUT}`-style values must expand in the
  caller's shell, while `<REFINE_ITER>`-style placeholders are literal fill-in tokens the
  orchestrator substitutes as prose. Both survive as-is through a double-quoted argument.
  The 7-argument form appends one labelled list — `<items>` is a newline-separated blob,
  each line indented under `- <label>:`. It carries the ledgers a `next:` string refers to
  ("skip any candidate marked refuted/ruled-out above"); without it, a post-compaction
  resume is told to consult a list that was never written, and re-probes what it ruled out.
  An empty `<items>` omits the block, so a caller need not branch.
Exit codes: 0 = written · 1 = contract file unwritable · 2 = wrong argument count or empty <skill>
"""

from __future__ import annotations

import sys
from pathlib import Path

# CWD-relative, exactly as the shell original: the contract belongs to the project the
# skill is running in, which is the caller's working directory, not this script's location.
_CONTRACT = Path(".temp/state/skill-contract.md")

_USAGE = (
    "usage: write_skill_contract.py <skill> <phase> <run-dir> <preserve> <next>\n"
    "       write_skill_contract.py <skill> <phase> <run-dir> <preserve> <next> <label> <items>"
)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "-h" in args or "--help" in args:
        print(_USAGE)
        print()
        print("Write .temp/state/skill-contract.md for the current compaction boundary.")
        sys.exit(0)

    if len(args) not in (5, 7):
        print(
            f"write_skill_contract: expected 5 args (skill phase run-dir preserve next) "
            f"or 7 (… label items), got {len(args)}",
            file=sys.stderr,
        )
        return 2

    skill, phase, run_dir, preserve, next_step = args[:5]
    label, items = args[5:] if len(args) == 7 else ("", "")
    if not skill:
        print("write_skill_contract: <skill> must not be empty", file=sys.stderr)
        return 2

    contract = (
        "## Active Skill Contract\n"
        f"- skill: {skill} · phase: {phase}\n"
        f"- run-dir: {run_dir}\n"
        f"- preserve: {preserve}\n"
        f"- next: {next_step}\n"
    )
    listed = [line for line in items.splitlines() if line.strip()]
    if listed:
        contract += f"- {label}:\n" + "".join(f"    - {line.strip()}\n" for line in listed)
    try:
        _CONTRACT.parent.mkdir(parents=True, exist_ok=True)
        _CONTRACT.write_text(contract, encoding="utf-8", newline="\n")
    except OSError as exc:
        print(f"write_skill_contract: cannot write {_CONTRACT}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

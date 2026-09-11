#!/usr/bin/env python
"""measure_config_size.py — size the config Claude loads, for /foundry:audit.

Two report modes, both replacing inline shell blocks:

``--mode inventory`` (Check 17 size table)
    One row per ``.claude/agents/*.md``, ``.claude/skills/*/SKILL.md`` and
    ``.claude/rules/*.md`` with its estimated tokens and line count, flagging any
    file over its per-kind budget.

``--mode overhead`` (Check 34)
    Total bytes of the always-loaded config — project ``CLAUDE.md``, the rules
    directory, and the global ``~/.claude/`` markdown — against the 50 KB warn
    and 100 KB fail thresholds, then each rules file against 5 KB / 10 KB.

Token estimate is ``bytes / 3``, the convention in `plugins/CLAUDE.md`
§Length Unit Convention; ``/4`` predates the current tokenizer and under-reports
by roughly 30%.

Usage:
    measure_config_size.py --mode inventory [--claude-dir <dir>]
    measure_config_size.py --mode overhead [--claude-dir <dir>] [--project-claude <file>]
                                           [--global-dir <dir>]

Exit codes:
    0   always — findings are reported on stdout with their severity markers,
        matching the inline blocks this replaced. A non-zero status would read as
        "the check failed to run", not "the check found something".
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

#: Per-kind token budget for the inventory report, in estimated tokens.
BUDGETS: dict[str, int] = {"agents": 4000, "skills": 8000, "rules": 2500}

#: Budget labels as they appear in the inventory findings.
BUDGET_LABELS: dict[str, str] = {"agents": "~4 k", "skills": "~8 k", "rules": "~2.5 k"}

OVERHEAD_WARN_BYTES = 51200
OVERHEAD_FAIL_BYTES = 102400
RULES_WARN_BYTES = 5120
RULES_FAIL_BYTES = 10240


def _measure(path: Path) -> tuple[int, int]:
    """Return (estimated tokens, line count) for one file."""
    data = path.read_bytes()
    return len(data) // 3, data.count(b"\n")


def _inventory_targets(claude_dir: Path) -> list[tuple[str, str, Path]]:
    """Return (kind, display label, path) for every file the inventory covers."""
    targets: list[tuple[str, str, Path]] = []
    for path in sorted((claude_dir / "agents").glob("*.md")):
        targets.append(("agents", f"agents/{path.name}", path))
    for path in sorted((claude_dir / "skills").glob("*/SKILL.md")):
        targets.append(("skills", f"skills/{path.parent.name}/SKILL.md", path))
    for path in sorted((claude_dir / "rules").glob("*.md")):
        targets.append(("rules", f"rules/{path.name}", path))
    return targets


def report_inventory(claude_dir: Path) -> None:
    """Print the per-file size table and flag files over their per-kind budget."""
    print(f"{'FILE':<52} {'~TOKENS':>8} {'LINES':>8}")
    for kind, label, path in _inventory_targets(claude_dir):
        if not path.is_file():
            continue
        tokens, lines = _measure(path)
        if tokens > BUDGETS[kind]:
            print(f"⚠ OVER BUDGET: {label} — ~{tokens} tokens / {lines} lines (limit: {BUDGET_LABELS[kind]})")
        else:
            print(f"  {label:<50} {tokens:>8} {lines:>8}")


def _byte_size(path: Path) -> int:
    try:
        return path.stat().st_size if path.is_file() else 0
    except OSError:
        return 0


def _rules_files(rules_dir: Path) -> list[Path]:
    """Return every rules Markdown file, nested ones included.

    ``rglob`` matches the ``find .claude/rules -name '*.md'`` original. A flat glob would silently stop counting and
    stop flagging ``rules/<subdir>/*.md``.
    """
    if not rules_dir.is_dir():
        return []
    return sorted(p for p in rules_dir.rglob("*.md") if p.is_file())


def _dir_bytes(directory: Path, pattern: str = "*.md") -> int:
    if not directory.is_dir():
        return 0
    return sum(_byte_size(p) for p in sorted(directory.glob(pattern)))


def report_overhead(claude_dir: Path, project_claude: Path, global_dir: Path) -> None:
    """Print always-loaded config totals and flag anything over threshold."""
    print("--- Check 34: Config token overhead ---")
    project_bytes = _byte_size(project_claude)
    rules_files = _rules_files(claude_dir / "rules")
    rules_bytes = sum(_byte_size(p) for p in rules_files)
    # `*.md` already covers CLAUDE.md; the shell original named it twice and
    # double-counted it into the total.
    global_bytes = _dir_bytes(global_dir)
    total = project_bytes + rules_bytes + global_bytes

    print(f"  Project CLAUDE.md:  {project_bytes} bytes")
    print(f"  Rules dir total:    {rules_bytes} bytes")
    print(f"  Global ~/.claude/:  {global_bytes} bytes")
    print(f"  Total always-loaded: {total} bytes (~{total // 3} tokens)")

    if total > OVERHEAD_FAIL_BYTES:
        print(f"! FAIL Check 34a — total always-loaded config {total} bytes (> 100 KB)")
    elif total > OVERHEAD_WARN_BYTES:
        print(f"⚠ WARN Check 34a — total always-loaded config {total} bytes (> 50 KB)")
    else:
        print(f"✓ OK Check 34 — config overhead {total} bytes (~{total // 3} tokens)")

    for path in rules_files:
        size = _byte_size(path)
        if size > RULES_FAIL_BYTES:
            print(f"! FAIL Check 34b — rules file {path.as_posix()} is {size} bytes (> 10 KB)")
        elif size > RULES_WARN_BYTES:
            print(f"⚠ WARN Check 34b — rules file {path.as_posix()} is {size} bytes (> 5 KB)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=("inventory", "overhead"), required=True)
    parser.add_argument("--claude-dir", default=".claude")
    parser.add_argument("--project-claude", default="CLAUDE.md")
    parser.add_argument("--global-dir", default=str(Path.home() / ".claude"))
    args = parser.parse_args()

    claude_dir = Path(args.claude_dir)
    if args.mode == "inventory":
        report_inventory(claude_dir)
    else:
        report_overhead(claude_dir, Path(args.project_claude), Path(args.global_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())

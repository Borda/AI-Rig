#!/usr/bin/env python3
"""Check that Codemap's Claude and Codex skill trees stay in step with each other and with the shared contract.

Codemap ships one skill roster per runtime (``claude-skills/`` and ``codex-skills/``) plus a shared integration
contract. Three copy-sync facts must hold, and no behaviour depends on them: a drifted copy changes no output, it only
leaves one runtime describing a different product than the other.

* Roster: each runtime holds exactly the canonical skills, so the two rosters are equal.
* Mode table: the pinned ``| Mode | ... | Exit |`` table of the ``integration`` skill is identical in both runtimes and
  in ``shared/integration-contract.md``.
* Not-for references: for every skill, the ``NOT for:`` / ``Skip for:`` / ``SKIP:`` clauses defer to the same other
  skills in both runtimes (only which skill is named counts, never the wording), name at least one skill, and name only
  canonical ones.

This is a repository-only hook, not a test: it needs both runtime trees side by side, which an installed plugin does
not carry. Usage: ``check_codemap_skill_parity.py [--root DIR]``; exit 1 on any finding, one line per finding.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

#: Plugin directory below the repository root.
PLUGIN_DIR = Path("plugins") / "codemap-py"
#: Every skill each runtime must expose, and nothing else.
CANONICAL_SKILLS = frozenset(
    {"scan-codebase", "query-code", "test-impact", "rename-refs", "integration", "debrief-coding"}
)
#: Skill roster directory per runtime, relative to the plugin directory.
RUNTIME_DIRS = {"claude": Path("claude-skills"), "codex": Path("codex-skills")}
#: Shared source of truth for the integration mode table, relative to the plugin directory.
INTEGRATION_CONTRACT = Path("shared") / "integration-contract.md"

#: Marker that opens a not-for clause; the clause runs from the colon to the next blank line.
_TRIGGER = re.compile(r"(?:NOT for|Skip for|SKIP)\s*:", re.IGNORECASE)
_CLAUSE_END_TAG = "</objective>"
_SKILL_REF = re.compile(r"[/$]codemap-py:([a-z][a-z-]*)")


def skill_roster(skills_dir: Path) -> set[str]:
    """Return the skill names that have a ``SKILL.md`` directly under ``skills_dir``.

    Examples:
        >>> skill_roster(Path("/nonexistent-roster-dir"))
        set()
    """
    if not skills_dir.is_dir():
        return set()
    return {p.name for p in skills_dir.iterdir() if p.is_dir() and (p / "SKILL.md").is_file()}


def mode_table(text: str) -> tuple[int, list[str]] | None:
    """Return the 1-based start line and rows of the pinned integration mode table, or ``None`` when absent.

    The table starts at the first row whose first cell is ``Mode`` and whose header names ``Exit``, and runs until
    the first line that is not a table row.

    Examples:
        >>> mode_table("x\\n| Mode | Exit |\\n| --- | --- |\\n| plan | 0 |\\nend")
        (2, ['| Mode | Exit |', '| --- | --- |', '| plan | 0 |'])
        >>> mode_table("no table here") is None
        True
    """
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip().startswith("| Mode") and "Exit" in line), None)
    if start is None:
        return None
    rows = []
    for line in lines[start:]:
        if not line.strip().startswith("|"):
            break
        rows.append(line.strip())
    return start + 1, rows


def not_for_clauses(text: str) -> list[str]:
    """Return the text of every ``NOT for:`` / ``Skip for:`` / ``SKIP:`` clause in ``text``.

    A clause starts after the colon and ends at the first blank line, at ``</objective>``, or at the end of the text.

    Examples:
        >>> not_for_clauses("NOT for: a\\n  b\\n\\nunrelated")
        [' a   b']
        >>> not_for_clauses("x SKIP: y</objective> z")
        [' y']
    """
    clauses: list[str] = []
    current: list[str] | None = None
    for line in text.splitlines():
        if current is not None:
            if not line.strip():
                clauses.append(" ".join(current))
                current = None
                continue
            end = line.find(_CLAUSE_END_TAG)
            if end >= 0:
                clauses.append(" ".join([*current, line[:end]]))
                current = None
                continue
            current.append(line)
            continue
        match = _TRIGGER.search(line)
        if match is None:
            continue
        rest = line[match.end() :]
        end = rest.find(_CLAUSE_END_TAG)
        if end >= 0:
            clauses.append(rest[:end])
        else:
            current = [rest]
    if current is not None:
        clauses.append(" ".join(current))
    return clauses


def not_for_skill_refs(text: str) -> set[str]:
    """Return the union of skill names referenced by every not-for clause in ``text``.

    Examples:
        >>> sorted(not_for_skill_refs("NOT for: $codemap-py:scan-codebase, /codemap-py:query-code"))
        ['query-code', 'scan-codebase']
    """
    return {ref for clause in not_for_clauses(text) for ref in _SKILL_REF.findall(clause)}


def roster_findings(plugin: Path, root: Path) -> list[str]:
    """Return findings for runtime rosters that miss or add a canonical skill, or differ from each other."""
    findings = []
    rosters = {label: skill_roster(plugin / rel) for label, rel in RUNTIME_DIRS.items()}
    for label, roster in rosters.items():
        where = (plugin / RUNTIME_DIRS[label]).relative_to(root).as_posix()
        if CANONICAL_SKILLS - roster:
            findings.append(
                f"{where}: {label} roster missing skill(s) {sorted(CANONICAL_SKILLS - roster)}; "
                "add the SKILL.md or update CANONICAL_SKILLS in scripts/check_codemap_skill_parity.py"
            )
        if roster - CANONICAL_SKILLS:
            findings.append(
                f"{where}: {label} roster has undocumented extra skill(s) {sorted(roster - CANONICAL_SKILLS)}; "
                "remove it or add it to CANONICAL_SKILLS in scripts/check_codemap_skill_parity.py"
            )
    if rosters["claude"] != rosters["codex"]:
        findings.append(
            f"{PLUGIN_DIR.as_posix()}: roster mismatch claude={sorted(rosters['claude'])} "
            f"codex={sorted(rosters['codex'])}; give both runtimes the same skills"
        )
    return findings


def mode_table_findings(plugin: Path, root: Path) -> list[str]:
    """Return findings for an integration mode table that is missing or differs from the shared contract."""
    sources = [
        ("shared integration contract", plugin / INTEGRATION_CONTRACT),
        *(
            (f"{label} integration skill", plugin / rel / "integration" / "SKILL.md")
            for label, rel in RUNTIME_DIRS.items()
        ),
    ]
    tables = {}
    findings = []
    for label, path in sources:
        shown = path.relative_to(root).as_posix()
        if not path.is_file():
            findings.append(f"{shown}: {label} not found; restore it so the mode table can be compared")
            continue
        found = mode_table(path.read_text(encoding="utf-8"))
        if found is None:
            findings.append(f"{shown}: {label} has no `| Mode ... | Exit |` table; restore the pinned mode table")
            continue
        tables[shown] = found
    reference = tables.get(plugin.joinpath(INTEGRATION_CONTRACT).relative_to(root).as_posix())
    findings.extend(
        f"{shown}:{line}: mode table differs from the shared contract; make the rows identical to "
        f"{INTEGRATION_CONTRACT.as_posix()}"
        for shown, (line, rows) in tables.items()
        if reference is not None and rows != reference[1]
    )
    return findings


def not_for_findings(plugin: Path, root: Path) -> list[str]:
    """Return findings for skills whose not-for clauses cite different, no, or unknown skills across runtimes."""
    findings = []
    for skill in sorted(CANONICAL_SKILLS):
        refs = {}
        for label, rel in RUNTIME_DIRS.items():
            path = plugin / rel / skill / "SKILL.md"
            shown = path.relative_to(root).as_posix()
            if not path.is_file():
                continue
            refs[label] = not_for_skill_refs(path.read_text(encoding="utf-8"))
            if not refs[label]:
                findings.append(f"{shown}: no NOT-for/Skip-for clause names a skill; add the skill it defers to")
            unknown = refs[label] - CANONICAL_SKILLS
            if unknown:
                findings.append(f"{shown}: NOT-for clause references unknown skill(s) {sorted(unknown)}; fix the name")
        if len(refs) == len(RUNTIME_DIRS) and refs["claude"] != refs["codex"]:
            findings.append(
                f"{(plugin / RUNTIME_DIRS['codex'] / skill / 'SKILL.md').relative_to(root).as_posix()}: "
                f"NOT-for references differ between runtimes claude={sorted(refs['claude'])} "
                f"codex={sorted(refs['codex'])}; defer to the same skills in both"
            )
    return findings


def check(root: Path) -> list[str]:
    """Return every skill-parity finding for the Codemap plugin below ``root``."""
    plugin = root / PLUGIN_DIR
    return [*roster_findings(plugin, root), *mode_table_findings(plugin, root), *not_for_findings(plugin, root)]


def main(argv: list[str] | None = None) -> int:
    """Check the repository and print one line per finding; exit 1 when any exist."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1], help="repository root")
    args = parser.parse_args(argv)
    findings = check(args.root.resolve())
    for finding in findings:
        print(finding)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())

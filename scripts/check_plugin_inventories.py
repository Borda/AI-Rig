#!/usr/bin/env python3
"""Check shipped-file inventories against reviewed tables: investigator spawn sites and standalone plugin payloads.

Two inventories are checked, both against a declarative table below:

* Every skill Markdown file that spawns ``sw-engineer`` hypothesis investigators is a reviewed read-only site. A new
  investigator site that is not in the table would reopen the shared-tree hazard unreviewed; add it to
  ``INVESTIGATOR_SITES`` after giving its spawn prompt the read-only clause (pinned by the kept pytest).
* The standalone ``cc_oss`` and ``cc_research`` payloads hold exactly their own hook modules, the ``audit-log.js``
  helper and the four permission manifests, so no sibling plugin's hook is copied in by accident.

This is a hook rather than a test because a stray or unlisted file breaks nothing at run time: a missing registered hook
is caught by the kept registration tests, and the investigator clause itself is pinned by pytest.

Usage: ``check_plugin_inventories.py [--root DIR]``. Always scans the whole tree. Exit 1 on any finding.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

#: Skill files that spawn ``sw-engineer`` hypothesis investigators, each reviewed for the read-only clause.
INVESTIGATOR_SITES = frozenset(
    {
        "plugins/cc_develop/skills/_shared/preflight-helpers.md",
        "plugins/cc_develop/skills/fix/modes/team-mode.md",
        "plugins/cc_develop/skills/debug/SKILL.md",
        "plugins/cc_oss/skills/review/SKILL.md",
        "plugins/cc_oss/skills/review/modes/scope-context.md",
    }
)
_INVESTIGATOR_LINE = re.compile(r"sw-engineer.*hypothes|hypothes.*sw-engineer", re.IGNORECASE)

#: Permission manifests every standalone plugin ships beside its ``plugin.json``.
_MANIFESTS = frozenset(
    {"plugin.json", "permissions-allow.json", "permissions-allow-retired.json", "permissions-deny.json"}
)
_LIB_HOOKS = frozenset({"audit-log.js"})
_COMMON_HOOKS = {
    "agent-router.js",
    "allow-dispatch.js",
    "audit-close.js",
    "blueprint-allow.js",
    "report-header-table.js",
    "sentinel-read-allow.js",
    "stale-plugin-check.js",
    "write-guard.js",
}
#: Plugin directory -> (hook modules, ``hooks/lib`` modules, ``.claude-plugin`` JSON files) the payload must hold.
PAYLOADS = {
    "cc_oss": (
        frozenset(
            _COMMON_HOOKS | {"enforce-analyse-header.js", "enforce-resolve-table.js", "enforce-review-header.js"}
        ),
        _LIB_HOOKS,
        _MANIFESTS,
    ),
    "cc_research": (frozenset(_COMMON_HOOKS | {"enforce-topic-header.js"}), _LIB_HOOKS, _MANIFESTS),
}


def investigator_findings(root: Path) -> list[str]:
    """Return one finding per skill file that names ``sw-engineer`` hypothesis investigators outside the table."""
    findings = []
    for path in sorted(root.glob("plugins/*/skills/**/*.md")):
        relative = path.relative_to(root)
        if "tests" in relative.parts or relative.as_posix() in INVESTIGATOR_SITES:
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        hit = next((number for number, line in enumerate(lines, 1) if _INVESTIGATOR_LINE.search(line)), None)
        if hit is not None:
            findings.append(
                f"{relative.as_posix()}:{hit}: spawns sw-engineer hypothesis investigators but is not a reviewed "
                "read-only site; give the prompt the read-only clause, then add the file to INVESTIGATOR_SITES in "
                "scripts/check_plugin_inventories.py and READ_ONLY_INVESTIGATOR_SITES in "
                "tests/instructions/test_agent_worktree_isolation_contract.py"
            )
    return findings


def _inventory_finding(directory: Path, pattern: str, expected: frozenset[str], root: Path) -> list[str]:
    """Return a finding when the files matching ``pattern`` in ``directory`` differ from ``expected``."""
    found = {path.name for path in directory.glob(pattern)}
    if found == expected:
        return []
    relative = directory.relative_to(root).as_posix()
    return [
        f"{relative}: {pattern} files differ from the expected inventory; unexpected {sorted(found - expected)}, "
        f"missing {sorted(expected - found)}; update PAYLOADS in scripts/check_plugin_inventories.py or the payload"
    ]


def payload_findings(root: Path) -> list[str]:
    """Return findings for every standalone plugin whose hook or manifest files differ from its table row."""
    findings: list[str] = []
    for plugin, (hooks, lib_hooks, manifests) in PAYLOADS.items():
        base = root / "plugins" / plugin
        findings.extend(_inventory_finding(base / "hooks", "*.js", hooks, root))
        findings.extend(_inventory_finding(base / "hooks" / "lib", "*.js", lib_hooks, root))
        findings.extend(_inventory_finding(base / ".claude-plugin", "*.json", manifests, root))
    return findings


def main(argv: list[str] | None = None) -> int:
    """Run both inventory checks under ``--root`` and print findings; exit 1 when any exist."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="repository root (default: current directory)")
    root = parser.parse_args(argv).root.resolve()
    findings = [*investigator_findings(root), *payload_findings(root)]
    for finding in findings:
        print(finding)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())

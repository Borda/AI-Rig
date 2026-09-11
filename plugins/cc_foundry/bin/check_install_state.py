#!/usr/bin/env python
"""check_install_state.py — validate post-install state under ~/.claude for /foundry:audit.

Replaces the three inline Install Check blocks. Each sub-check keeps the wording
the audit severity table maps onto:

``--check I1``  the foundry plugin is registered in ``installed_plugins.json``
                and its install cache directory exists
``--check I2``  ``~/.claude/settings.json`` carries what ``/foundry:setup`` merges:
                a statusline command, a populated allow list, the bridge plugin
                enabled, and no stale top-level ``hooks`` block
``--check I3``  ``~/.claude/agents`` and ``~/.claude/skills`` hold no foundry
                symlinks, and the ``rules``/``TEAM_PROTOCOL.md`` symlinks that
                should exist still resolve

These read the home directory, not the project ``.claude/``. ``--home`` exists so
tests can point the checks at a fixture tree.

Usage:
    check_install_state.py --check {I1,I2,I3} [--home <dir>]

Exit codes:
    0   always — findings are reported on stdout with their severity markers,
        matching the inline blocks this replaced. A non-zero status would read as
        "the check failed to run", not "the check found something".
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_SETUP_FIX = "  Fix: run /foundry:setup"


def _load_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _foundry_entry(registry: dict) -> dict | None:
    """Return the registry record for the foundry plugin, or None.

    Claude Code nests the records under a top-level ``plugins`` key and stores a list of install records per plugin id.
    The shell original read the top level directly, so it never matched and Check I1 could only ever report "not found";
    both shapes are accepted here.
    """
    entries = registry.get("plugins") if isinstance(registry.get("plugins"), dict) else registry
    for key, value in entries.items():
        if "foundry" not in key.lower():
            continue
        if isinstance(value, dict):
            return value
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value[0]
    return None


def check_cache(home: Path) -> int:
    """Check I1 — the plugin is registered and its cache directory is present."""
    print("=== Check I1: foundry plugin cache ===")
    registry_path = home / ".claude" / "plugins" / "installed_plugins.json"
    if not registry_path.is_file():
        print("! HIGH: Check I1 — installed_plugins.json not found; plugin may not be installed")
        return 1
    registry = _load_json(registry_path)
    entry = _foundry_entry(registry) if registry else None
    install_path = (entry or {}).get("installPath") or ""
    if not install_path:
        print("! HIGH: Check I1 — foundry not found in installed_plugins.json")
        print("  Fix: claude plugin marketplace add Borda/AI-Rig && claude plugin install foundry@borda-ai-rig")
        return 1
    if not Path(install_path).is_dir():
        print(f"! HIGH: Check I1 — install cache missing: {install_path}")
        print("  Fix: claude plugin install foundry@borda-ai-rig  (reinstall to rebuild cache)")
        return 1
    version = (entry or {}).get("version") or "unknown"
    print(f"✓: Check I1 — foundry cache intact at {install_path} (version: {version})")
    return 0


def _settings_findings(settings: dict) -> list[tuple[str, str]]:
    """Return (label, message) for each I2 sub-check that fails."""
    findings: list[tuple[str, str]] = []
    if "statusline.js" not in str((settings.get("statusLine") or {}).get("command") or ""):
        findings.append(("I2a", "statusLine not set to statusline.js"))
    if len((settings.get("permissions") or {}).get("allow") or []) <= 10:
        findings.append(
            ("I2b", "permissions.allow appears empty or very short; foundry entries may not have been merged")
        )
    if (settings.get("enabledPlugins") or {}).get("bridge@borda-ai-rig") is not True:
        findings.append(("I2c", "enabledPlugins.bridge@borda-ai-rig not set to true"))
    if "hooks" in settings:
        findings.append(
            (
                "I2d",
                "'hooks' key present in ~/.claude/settings.json; stale block from before plugin "
                "migration will cause double-firing",
            )
        )
    return findings


_I2_PASS = {
    "I2a": "✓: Check I2a — statusLine set",
    "I2b": "✓: Check I2b — permissions.allow populated",
    "I2c": "✓: Check I2c — enabledPlugins.bridge@borda-ai-rig enabled",
    "I2d": "✓: Check I2d — no stale hooks block",
}


def check_settings(home: Path) -> int:
    """Check I2 — the settings merge /foundry:setup performs is present."""
    print("=== Check I2: ~/.claude/settings.json merge ===")
    settings_path = home / ".claude" / "settings.json"
    if not settings_path.is_file():
        print("! HIGH: Check I2 — ~/.claude/settings.json not found")
        return 1
    settings = _load_json(settings_path) or {}
    findings = _settings_findings(settings)
    failed = {label for label, _ in findings}
    for label, message in findings:
        print(f"⚠ MEDIUM: Check {label} — {message}")
        fix = _SETUP_FIX
        if label == "I2d":
            fix = "  Fix: run /foundry:setup — it will offer to remove the stale hooks block"
        print(fix)
    for label in ("I2a", "I2b", "I2c", "I2d"):
        if label not in failed:
            print(_I2_PASS[label])
    if not findings:
        print("✓: Check I2 — settings merge complete")
        return 0
    return 1


def _foundry_symlinks(home: Path) -> list[tuple[Path, str]]:
    """Return (path, target) for foundry symlinks that must not exist."""
    claude = home / ".claude"
    candidates = list((claude / "agents").glob("*.md")) + list((claude / "skills").glob("*"))
    found: list[tuple[Path, str]] = []
    for path in sorted(candidates):
        if not path.is_symlink():
            continue
        target = str(Path(path).readlink())
        if "borda-ai-rig/foundry/" in target:
            found.append((path, target))
    return found


def _rule_links(home: Path) -> list[tuple[Path, str, bool]]:
    """Return (path, target, resolves) for the symlinks that should exist."""
    claude = home / ".claude"
    candidates = list((claude / "rules").glob("*.md")) + [claude / "TEAM_PROTOCOL.md"]
    links: list[tuple[Path, str, bool]] = []
    for path in sorted(candidates):
        if path.is_symlink():
            links.append((path, str(Path(path).readlink()), path.exists()))
    return links


def check_links(home: Path) -> int:
    """Check I3 — no foundry agent/skill links, and rules links still resolve."""
    print("=== Check I3: ~/.claude/ link health ===")
    forbidden = _foundry_symlinks(home)
    for path, target in forbidden:
        print(f"! HIGH: Check I3 — foundry symlink must not exist: {path.as_posix()} -> {target}")
    if forbidden:
        print("  Fix: re-run /foundry:setup — Step 10 Phase 1 purges agent and skill symlinks")

    links = _rule_links(home)
    stale = [(path, target) for path, target, resolves in links if not resolves]
    for path, target in stale:
        print(f"! HIGH: Check I3 — broken symlink: {path.as_posix()} -> {target}")

    if not links:
        print("✓: Check I3 — no rules symlinks in ~/.claude/ (foundry:setup not run; skipping staleness check)")
    elif not stale:
        print(f"✓: Check I3 — {len(links)} rules/TEAM_PROTOCOL symlink(s) all resolve correctly")
    else:
        print(
            f"! HIGH: Check I3 — {len(stale)} of {len(links)} symlink(s) broken "
            "(likely stale after plugin version upgrade)"
        )
        print("  Fix: re-run /foundry:setup — Phase 4 replaces stale symlinks with the current cache path")
    return 1 if forbidden or stale else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", choices=("I1", "I2", "I3"), required=True)
    parser.add_argument("--home", default=str(Path.home()))
    args = parser.parse_args()

    home = Path(args.home)
    runner = {"I1": check_cache, "I2": check_settings, "I3": check_links}[args.check]
    runner(home)
    return 0


if __name__ == "__main__":
    sys.exit(main())

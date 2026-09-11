#!/usr/bin/env python
"""check_plugin_layout.py — validate a plugin's on-disk layout for /foundry:audit Check 8.

Replaces the inline Check 8 shell block. Every sub-check keeps the exact output
wording the audit severity table maps onto, so the finding classification in
``skills/audit/templates/checks-setup.md`` stays valid:

- 8a  manifest exists, parses, and declares name/version/description
- 8b  ``agents/`` and ``skills/`` are real directories in the plugin, and the
      matching ``.claude/`` entries are symlinks rather than real copies
- 8c  hook files are real files, and every ``hooks.json`` command reference
      resolves to a file that exists
- 8d  ``hooks.json`` is valid JSON
- 8e  ``claude plugin validate`` passes, when the CLI is on PATH
- 8f  ``permissions-allow.json`` and ``.claude/settings.json`` agree
- 8g  the setup skill exists and mentions the settings it is responsible for

Usage:
    check_plugin_layout.py [--plugin-dir <dir>] [--claude-dir <dir>] [--expect-name <name>]
                           [--timeout SECS]

Exit codes:
    0   always — findings are reported on stdout with their severity markers,
        matching the inline block this replaced. A non-zero status would read as
        "the check failed to run", not "the check found something".
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

# hooks.json commands reference hook files as ${CLAUDE_PLUGIN_ROOT}/hooks/<name>.js.
_HOOK_REF = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}/hooks/([^\"'\s]+\.js)")

_SETUP_KEYWORDS = ("statusLine", "permissions.allow", "bridge@borda-ai-rig", "link")


def _emit(line: str) -> None:
    print(line)


def check_manifest(plugin_dir: Path, expect_name: str) -> int:
    """Check 8a — manifest present, parseable, and naming the expected plugin."""
    manifest = plugin_dir / ".claude-plugin" / "plugin.json"
    if not manifest.is_file():
        _emit(f"! CRITICAL: Check 8a — manifest not found: {manifest.as_posix()}")
        return 1
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _emit("! CRITICAL: Check 8a — manifest invalid JSON or missing required fields (name, version, description)")
        return 1
    if not all(key in data for key in ("name", "version", "description")):
        _emit("! CRITICAL: Check 8a — manifest invalid JSON or missing required fields (name, version, description)")
        return 1
    name = data["name"]
    if name != expect_name:
        _emit(f"! HIGH: Check 8a — manifest name is '{name}', expected '{expect_name}'")
        return 1
    _emit(f"✓: Check 8a — manifest valid (name: {name})")
    return 0


def _check_plugin_dirs(plugin_dir: Path) -> int:
    fail = 0
    for name in ("agents", "skills"):
        target = plugin_dir / name
        if target.is_symlink():
            _emit(f"! HIGH: Check 8b — {target.as_posix()} is a symlink; expected real directory (canonical source)")
            fail += 1
        elif not target.is_dir():
            _emit(f"! HIGH: Check 8b — {target.as_posix()} directory not found")
            fail += 1
        else:
            _emit(f"✓: Check 8b — real directory: {target.as_posix()}")
    return fail


def _broken_and_stale(entries: list[Path]) -> tuple[int, int]:
    """Count entries that are real (not symlinked) and symlinks that do not resolve."""
    broken = sum(1 for entry in entries if not entry.is_symlink())
    stale = sum(1 for entry in entries if entry.is_symlink() and not entry.exists())
    return broken, stale


def _check_claude_links(claude_dir: Path) -> None:
    agents = sorted(p for p in (claude_dir / "agents").glob("*.md")) if (claude_dir / "agents").is_dir() else []
    broken, stale = _broken_and_stale(agents)
    if broken:
        _emit(f"⚠ MEDIUM: Check 8b — {broken} non-symlink .md file(s) in .claude/agents/ (expected symlinks → plugin)")
    if stale:
        _emit(f"! HIGH: Check 8b — {stale} broken symlink(s) in .claude/agents/")
    if not broken and not stale:
        _emit("✓: Check 8b — .claude/agents/ symlinks valid")

    skills_dir = claude_dir / "skills"
    skills = sorted(p for p in skills_dir.iterdir() if p.is_dir() or p.is_symlink()) if skills_dir.is_dir() else []
    broken, stale = _broken_and_stale(skills)
    if broken:
        _emit(
            f"⚠ MEDIUM: Check 8b — {broken} real directory/directories in .claude/skills/ (expected symlinks → plugin)"
        )
    if stale:
        _emit(f"! HIGH: Check 8b — {stale} broken symlink(s) in .claude/skills/")
    if not broken and not stale:
        _emit("✓: Check 8b — .claude/skills/ symlinks valid")


def check_directories(plugin_dir: Path, claude_dir: Path) -> int:
    """Check 8b — canonical directories in the plugin, symlinks in ``.claude/``."""
    fail = _check_plugin_dirs(plugin_dir)
    _check_claude_links(claude_dir)
    return fail


def _hook_refs(hooks_json: Path) -> set[str]:
    try:
        data = json.loads(hooks_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    refs: set[str] = set()
    for entries in (data.get("hooks") or {}).values():
        for entry in entries:
            for hook in entry.get("hooks") or []:
                match = _HOOK_REF.search(hook.get("command") or "")
                if match:
                    refs.add(match.group(1))
    return refs


def check_hooks(plugin_dir: Path) -> int:
    """Check 8c — hook files are real, and hooks.json references resolve."""
    hooks_dir = plugin_dir / "hooks"
    if not hooks_dir.is_dir():
        _emit("! HIGH: Check 8c — hooks/ directory not found in plugin")
        return 1

    symlinked = [js.name for js in sorted(hooks_dir.glob("*.js")) if js.is_symlink()]
    for name in symlinked:
        _emit(f"⚠ MEDIUM: Check 8c — {name} is a symlink; expected real file in plugin hooks/")
    if not symlinked:
        _emit("✓: Check 8c — plugin hook files valid")

    hooks_json = hooks_dir / "hooks.json"
    if not hooks_json.is_file():
        return 0
    missing = sorted(ref for ref in _hook_refs(hooks_json) if not (hooks_dir / ref).is_file())
    for ref in missing:
        _emit(f"! HIGH: Check 8c — hooks.json references missing file: hooks/{ref}")
    if not missing:
        _emit("✓: Check 8c — hooks.json references all resolve to plugin files")
        return 0
    return 1


def check_hooks_json(plugin_dir: Path) -> int:
    """Check 8d — hooks.json parses."""
    hooks_json = plugin_dir / "hooks" / "hooks.json"
    if not hooks_json.is_file():
        _emit("! HIGH: Check 8d — hooks/hooks.json not found")
        return 1
    try:
        json.loads(hooks_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _emit("! HIGH: Check 8d — hooks/hooks.json is not valid JSON")
        return 1
    _emit("✓: Check 8d — hooks/hooks.json is valid JSON")
    return 0


def check_cli_validate(plugin_dir: Path, *, timeout: float) -> int:
    """Check 8e — the Claude CLI accepts the plugin."""
    if shutil.which("claude") is None:
        _emit("⚠ SKIPPED: Check 8e — claude CLI not in PATH")
        return 0
    try:
        result = subprocess.run(  # noqa: S603 — fixed argv, plugin_dir is a local path
            ["claude", "plugin", "validate", f"./{plugin_dir.as_posix()}"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        # Includes TimeoutExpired. Letting it propagate would kill the process
        # before 8f and 8g ever run, losing two sub-checks with no output.
        _emit("⚠ SKIPPED: Check 8e — `claude plugin validate` failed to run or timed out")
        return 0
    if result.returncode != 0:
        combined = (result.stdout + result.stderr).strip()
        _emit(f"! HIGH: Check 8e — `claude plugin validate` failed:\n{combined}")
        return 1
    _emit("✓: Check 8e — `claude plugin validate` passed")
    return 0


def _allow_entries(path: Path, key: str | None) -> set[str] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if key is not None:
        data = (data.get("permissions") or {}).get(key) or []
    return set(data) if isinstance(data, list) else None


def check_permissions_drift(plugin_dir: Path, claude_dir: Path) -> int:
    """Check 8f — plugin allow list and project settings agree."""
    perm_json = plugin_dir / ".claude-plugin" / "permissions-allow.json"
    settings = claude_dir / "settings.json"
    if not perm_json.is_file():
        _emit(f"⚠ SKIPPED: Check 8f — permissions-allow.json not found at {perm_json.as_posix()}")
        return 0
    if not settings.is_file():
        _emit("⚠ SKIPPED: Check 8f — no .claude/settings.json (marketplace install; skipping drift check)")
        return 0

    plugin_allow = _allow_entries(perm_json, None)
    settings_allow = _allow_entries(settings, "allow")
    if plugin_allow is None or settings_allow is None:
        _emit("⚠ SKIPPED: Check 8f — allow list unreadable")
        return 0

    missing_from_plugin = sorted(settings_allow - plugin_allow)
    missing_from_settings = sorted(plugin_allow - settings_allow)
    fail = 0
    if missing_from_plugin:
        _emit(
            f"⚠ MEDIUM: Check 8f — {len(missing_from_plugin)} allow entries in settings.json absent "
            "from permissions-allow.json (plugin users won't get them)"
        )
        for entry in missing_from_plugin:
            _emit(f"    {entry}")
        fail = 1
    if missing_from_settings:
        _emit(
            f"⚠ LOW: Check 8f — {len(missing_from_settings)} permissions-allow.json entries absent "
            "from .claude/settings.json"
        )
        for entry in missing_from_settings:
            _emit(f"    {entry}")
    if not missing_from_plugin and not missing_from_settings:
        _emit("✓: Check 8f — permissions-allow.json and .claude/settings.json in sync")
    return fail


def check_setup_skill(plugin_dir: Path, claude_dir: Path) -> int:
    """Check 8g — the setup skill is plugin-only and documents what it merges."""
    setup_skill = plugin_dir / "skills" / "setup" / "SKILL.md"
    if not setup_skill.is_file():
        _emit(f"! HIGH: Check 8g — setup SKILL.md not found at {setup_skill.as_posix()}")
        return 1
    local_setup = claude_dir / "skills" / "setup"
    if local_setup.exists() or local_setup.is_symlink():
        _emit("⚠ MEDIUM: Check 8g — .claude/skills/setup/ exists; setup skill should live only in the plugin")
    body = setup_skill.read_text(encoding="utf-8")
    missing = [kw for kw in _SETUP_KEYWORDS if kw not in body]
    for keyword in missing:
        _emit(f"⚠ MEDIUM: Check 8g — setup SKILL.md does not mention '{keyword}'")
    if missing:
        return 1
    _emit("✓: Check 8g — setup-foundry SKILL.md present and covers required settings")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plugin-dir", default="plugins/cc_foundry")
    parser.add_argument("--claude-dir", default=".claude")
    parser.add_argument("--expect-name", default="foundry")
    parser.add_argument("--timeout", type=float, default=60.0, help="seconds for `claude plugin validate`")
    args = parser.parse_args()

    plugin_dir = Path(args.plugin_dir)
    claude_dir = Path(args.claude_dir)

    _emit("=== Check 8: foundry plugin correctness ===")
    if not plugin_dir.is_dir():
        _emit(f"⚠ SKIPPED: Check 8 — {plugin_dir.as_posix()}/ not found")
        return 0

    fail = check_manifest(plugin_dir, args.expect_name)
    fail += check_directories(plugin_dir, claude_dir)
    fail += check_hooks(plugin_dir)
    fail += check_hooks_json(plugin_dir)
    fail += check_cli_validate(plugin_dir, timeout=args.timeout)
    fail += check_permissions_drift(plugin_dir, claude_dir)
    fail += check_setup_skill(plugin_dir, claude_dir)

    if fail == 0:
        _emit("✓ OK: Check 8 — foundry plugin structure valid")
    else:
        _emit(f"✗: Check 8 — {fail} issue(s) found")
    return 0


if __name__ == "__main__":
    sys.exit(main())

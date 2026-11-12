"""Prepare real cache-owned helper payloads for executable Markdown contract tests."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

_PLUGIN = Path(__file__).resolve().parents[1]


def install_foundry(home: Path) -> Path:
    """Copy foundry's helper payload into an isolated, resolver-valid installed cache root."""
    root = home / ".claude/plugins/cache/borda-ai-rig/foundry/1.0.0"
    (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (root / ".claude-plugin/plugin.json").write_text(json.dumps({"name": "foundry"}), encoding="utf-8")
    shutil.copytree(
        _PLUGIN / "bin", root / "bin", dirs_exist_ok=True, ignore=shutil.ignore_patterns(".claude", "__pycache__")
    )
    return root

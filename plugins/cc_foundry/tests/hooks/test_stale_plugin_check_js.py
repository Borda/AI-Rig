"""Subprocess tests for ``hooks/stale-plugin-check.js``.

The hook fires on every ``SessionStart``. It compares the foundry root this session loaded with the install record in
``<config>/plugins/installed_plugins.json`` and prints a warning when the loaded copy was replaced (``.orphaned_at``) or
is no longer the installed path. Every case builds a fake config dir and passes it through ``CLAUDE_CONFIG_DIR``;
``CLAUDE_PLUGIN_ROOT`` names the loaded root.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[2] / "hooks" / "stale-plugin-check.js"
_requires_node = pytest.mark.skipif(shutil.which("node") is None, reason="node executable not available")


def _install(config: Path, version: str) -> Path:
    """Create one cached foundry version dir and return it."""
    root = config / "plugins" / "cache" / "borda-ai-rig" / "foundry" / version
    root.mkdir(parents=True)
    return root


def _record(config: Path, *installed: Path) -> None:
    """Write installed_plugins.json naming the given foundry roots as installed."""
    entries = [{"scope": "user", "installPath": str(root), "version": root.name} for root in installed]
    path = config / "plugins" / "installed_plugins.json"
    path.write_text(json.dumps({"version": 2, "plugins": {"foundry@borda-ai-rig": entries}}), encoding="utf-8")


def _run(config: Path, root: Path, payload: str = '{"hook_event_name":"SessionStart","source":"clear"}') -> str:
    """Run the hook for one loaded root and return its stdout."""
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(config), "CLAUDE_PLUGIN_ROOT": str(root)}
    result = subprocess.run(
        ["node", str(HOOK)], input=payload, env=env, capture_output=True, text=True, encoding="utf-8", timeout=15
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@_requires_node
class TestStalePluginCheck:
    """Warn only when the loaded foundry copy is not the installed one."""

    def test_silent_when_loaded_root_is_installed(self, tmp_path: Path) -> None:
        """A session running the installed version prints nothing."""
        root = _install(tmp_path, "0.63.0")
        _record(tmp_path, root)

        assert _run(tmp_path, root) == ""

    def test_warns_when_loaded_root_was_replaced(self, tmp_path: Path) -> None:
        """An older loaded root that a newer install replaced is reported with both versions.

        This is the observed failure: a long-running session kept executing an old copy after a newer one was
        installed, with no sign anything was stale.
        """
        old = _install(tmp_path, "0.62.1")
        (old / ".orphaned_at").write_text("1790681924819", encoding="utf-8")
        new = _install(tmp_path, "0.63.0")
        _record(tmp_path, new)

        out = _run(tmp_path, old)

        assert "STALE PLUGIN SESSION" in out
        assert "foundry 0.62.1 (replaced — marked orphaned)" in out
        assert "installed version is 0.63.0" in out
        assert "restart Claude Code" in out

    def test_warns_when_orphan_marker_alone_flags_root(self, tmp_path: Path) -> None:
        """A root still named in the record but marked orphaned is stale."""
        root = _install(tmp_path, "0.63.0")
        (root / ".orphaned_at").write_text("1", encoding="utf-8")
        _record(tmp_path, root)

        assert "STALE PLUGIN SESSION" in _run(tmp_path, root)

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param("", id="no-stdin"),
            pytest.param("not json", id="malformed-stdin"),
        ],
    )
    def test_checks_without_a_usable_payload(self, tmp_path: Path, payload: str) -> None:
        """The check never depends on the payload, so a missing or broken one still warns."""
        old = _install(tmp_path, "0.62.1")
        _record(tmp_path, _install(tmp_path, "0.63.0"))

        assert "STALE PLUGIN SESSION" in _run(tmp_path, old, payload)

    def test_silent_for_root_outside_the_plugin_cache(self, tmp_path: Path) -> None:
        """A source-tree root has no install record to compare, so it stays silent."""
        _record(tmp_path, _install(tmp_path, "0.63.0"))
        source = tmp_path / "workspace" / "plugins" / "cc_foundry"
        source.mkdir(parents=True)

        assert _run(tmp_path, source) == ""

    def test_silent_without_install_record(self, tmp_path: Path) -> None:
        """A missing installed_plugins.json cannot prove staleness, so the hook stays silent."""
        root = _install(tmp_path, "0.62.1")

        assert _run(tmp_path, root) == ""

    def test_silent_for_other_hook_events(self, tmp_path: Path) -> None:
        """An explicit non-SessionStart event is ignored even when the root is stale."""
        old = _install(tmp_path, "0.62.1")
        _record(tmp_path, _install(tmp_path, "0.63.0"))

        assert _run(tmp_path, old, '{"hook_event_name":"Stop"}') == ""

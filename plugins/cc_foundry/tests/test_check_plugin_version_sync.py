"""Tests for ``bin/check_plugin_version_sync.py``.

The checker walks a scan dir for plugins shipping BOTH ``.claude-plugin`` and ``.codex-plugin`` manifests and requires
the two ``version`` fields to agree — one release, two host manifests. Single-host plugins are out of scope.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


_MOD_PATH = Path(__file__).resolve().parent.parent / "bin" / "check_plugin_version_sync.py"
_spec = importlib.util.spec_from_file_location("check_plugin_version_sync", _MOD_PATH)
assert _spec and _spec.loader
vs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vs)


def _plugin(root: Path, name: str, claude: str | None, codex: str | None) -> Path:
    """Create a plugin dir with the requested host manifests (None = omit that host)."""
    plugin = root / name
    if claude is not None:
        (plugin / ".claude-plugin").mkdir(parents=True)
        (plugin / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": name, "version": claude}), encoding="utf-8"
        )
    if codex is not None:
        (plugin / ".codex-plugin").mkdir(parents=True)
        (plugin / ".codex-plugin" / "plugin.json").write_text(
            json.dumps({"name": name, "version": codex}), encoding="utf-8"
        )
    return plugin


@pytest.mark.packaging
class TestFindDesyncs:
    """Flag disagreeing dual-manifest pairs only."""

    def test_matching_pair_is_clean(self, tmp_path: Path) -> None:
        """A dual-host plugin whose manifests agree produces no findings.

        The everyday post-bump state: both manifests were bumped together, so
        the gate must stay silent.
        """
        _plugin(tmp_path, "dual", "1.2.3", "1.2.3")
        assert vs.find_desyncs(tmp_path) == []

    def test_mismatch_is_reported_with_both_versions(self, tmp_path: Path) -> None:
        """A version bump applied to one host manifest only is flagged, naming both values.

        This is the incident shape: codemap-py's Claude manifest was bumped to
        0.31.3 while the Codex manifest stayed 0.31.2 — two installs claiming
        different releases of identical code.
        """
        _plugin(tmp_path, "dual", "0.31.3", "0.31.2")
        findings = vs.find_desyncs(tmp_path)
        assert len(findings) == 1
        assert "0.31.3" in findings[0] and "0.31.2" in findings[0]

    def test_single_host_plugins_ignored(self, tmp_path: Path) -> None:
        """Plugins shipping only one host manifest are out of scope.

        Most plugins are Claude-only (or Codex-only, like codex-rig); they have no counterpart to desync from and must
        not produce noise.
        """
        _plugin(tmp_path, "claude-only", "9.9.9", None)
        _plugin(tmp_path, "codex-only", None, "8.8.8")
        assert vs.find_desyncs(tmp_path) == []

    def test_missing_version_field_is_flagged(self, tmp_path: Path) -> None:
        """A dual-host manifest without a ``version`` string is a finding, not a pass.

        Silently treating an absent field as matching would let a malformed manifest disable the gate exactly when it is
        needed.
        """
        plugin = _plugin(tmp_path, "dual", "1.0.0", "1.0.0")
        (plugin / ".codex-plugin" / "plugin.json").write_text(json.dumps({"name": "dual"}), encoding="utf-8")
        findings = vs.find_desyncs(tmp_path)
        assert len(findings) == 1
        assert ".codex-plugin" in findings[0]

    def test_unparseable_manifest_is_flagged(self, tmp_path: Path) -> None:
        """Invalid JSON in either manifest is a finding rather than a crash or a pass.

        The checker runs in pre-commit; a corrupt file must fail the gate with a location, never traceback.
        """
        plugin = _plugin(tmp_path, "dual", "1.0.0", "1.0.0")
        (plugin / ".claude-plugin" / "plugin.json").write_text("{not json", encoding="utf-8")
        findings = vs.find_desyncs(tmp_path)
        assert len(findings) == 1
        assert ".claude-plugin" in findings[0]


@pytest.mark.packaging
class TestMain:
    """CLI exit codes mirror the findings."""

    @pytest.mark.parametrize(
        ("claude", "codex", "expected"),
        [pytest.param("1.0.0", "1.0.0", 0, id="in-sync"), pytest.param("1.0.1", "1.0.0", 1, id="desynced")],
    )
    def test_exit_codes(self, tmp_path: Path, capsys, claude: str, codex: str, expected: int) -> None:
        """Exit 0 when every pair agrees, 1 on any desync (with a VERSION-DESYNC line).

        pre-commit keys purely on the exit code; the printed finding is what the committer acts on.
        """
        _plugin(tmp_path, "dual", claude, codex)
        rc = vs.main(["--scan-dir", str(tmp_path)])
        out = capsys.readouterr().out
        assert rc == expected
        assert ("VERSION-DESYNC" in out) == bool(expected)

    def test_real_repo_scan_is_clean(self) -> None:
        """The actual repository passes — bridge and codemap-py pairs agree.

        Guards the live tree: a merge that desyncs a real pair fails here even
        before the pre-commit hook runs.
        """
        repo_plugins = Path(__file__).resolve().parents[3] / "plugins"
        assert vs.find_desyncs(repo_plugins) == []

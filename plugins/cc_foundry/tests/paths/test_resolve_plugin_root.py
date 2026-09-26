"""Tests for ``bin/resolve_plugin_root.py``.

Doctests in the source cover the pure helpers (``lookup_registry``, ``scan_cache``). This file exercises the CLI surface
via ``main()`` with a ``monkeypatch``-ed HOME so the registry/cache cascade and the two security gates resolve against
an isolated tree.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from resolve_plugin_root import main  # noqa: E402


def _make_install(home: Path, version: str, *, name: str = "foundry") -> Path:
    """Create a cache install dir with a ``.claude-plugin/plugin.json`` manifest."""
    root = home / ".claude" / "plugins" / "cache" / "borda-ai-rig" / "foundry" / version
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": name}), encoding="utf-8")
    return root


def _write_registry(home: Path, install_path: str, *, installed_at: str = "2026-05-01T00:00:00Z") -> None:
    """Write a fake ``installed_plugins.json`` pointing foundry at ``install_path``."""
    reg = home / ".claude" / "plugins" / "installed_plugins.json"
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text(
        json.dumps({"plugins": {"foundry@borda-ai-rig": [{"installedAt": installed_at, "installPath": install_path}]}}),
        encoding="utf-8",
    )


class TestMain:
    """Main: CLI surface — stdout, stderr, exit codes."""

    def test_registry_hit_prints_validated_root(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Registry entry resolves and passes both security gates → exit 0."""
        install = _make_install(tmp_path, "0.27.8")
        _write_registry(tmp_path, str(install))
        monkeypatch.setattr("os.path.expanduser", lambda _p: str(tmp_path))

        rc = main(["--plugin-name", "foundry"])

        assert rc == 0
        assert capsys.readouterr().out.strip() == str(install)

    def test_cache_scan_fallback_picks_newest(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """No registry entry → cache scan returns newest non-orphaned version."""
        _make_install(tmp_path, "0.10.0")
        newest = _make_install(tmp_path, "0.27.8")
        monkeypatch.setattr("os.path.expanduser", lambda _p: str(tmp_path))

        rc = main(["--plugin-name", "foundry"])

        assert rc == 0
        assert capsys.readouterr().out.strip() == str(newest)

    def test_orphaned_version_skipped_by_scan(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A newer version carrying ``.orphaned_at`` is skipped in favour of an older valid one."""
        valid = _make_install(tmp_path, "0.10.0")
        orphaned = _make_install(tmp_path, "0.27.8")
        (orphaned / ".orphaned_at").write_text("2026-06-01", encoding="utf-8")
        monkeypatch.setattr("os.path.expanduser", lambda _p: str(tmp_path))

        rc = main(["--plugin-name", "foundry"])

        assert rc == 0
        assert capsys.readouterr().out.strip() == str(valid)

    def test_exits_1_when_not_found_anywhere(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Empty registry and empty cache → exit 1 with diagnostic."""
        monkeypatch.setattr("os.path.expanduser", lambda _p: str(tmp_path))

        rc = main(["--plugin-name", "foundry"])

        assert rc == 1
        assert "not found" in capsys.readouterr().err

    def test_exits_2_when_manifest_name_mismatches(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Resolved root whose manifest name != requested plugin → security abort, exit 2."""
        install = _make_install(tmp_path, "0.27.8", name="malicious")
        _write_registry(tmp_path, str(install))
        monkeypatch.setattr("os.path.expanduser", lambda _p: str(tmp_path))

        rc = main(["--plugin-name", "foundry"])

        assert rc == 2
        assert "SECURITY" in capsys.readouterr().err

    def test_exits_2_when_root_outside_cache(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Registry points outside the cache dir → security abort, exit 2."""
        outside = tmp_path / "evil"
        (outside / ".claude-plugin").mkdir(parents=True)
        (outside / ".claude-plugin" / "plugin.json").write_text('{"name": "foundry"}', encoding="utf-8")
        _write_registry(tmp_path, str(outside))
        monkeypatch.setattr("os.path.expanduser", lambda _p: str(tmp_path))

        rc = main(["--plugin-name", "foundry"])

        assert rc == 2
        assert "SECURITY" in capsys.readouterr().err

    @pytest.mark.parametrize("plugin", ["", "../etc", "foo/bar"])
    def test_exits_2_on_invalid_plugin_token(
        self,
        plugin: str,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Invalid plugin token → exit 2 with stderr message."""
        rc = main(["--plugin-name", plugin])

        assert rc == 2
        assert "error" in capsys.readouterr().err.lower()

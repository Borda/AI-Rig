"""Tests for ``bin/get_plugin_install_path.py``.

Doctests in the source cover the pure helpers (``pick_latest_install_path``, ``resolve_install_path``). This file
exercises the CLI surface via ``main()`` with ``capsys`` for stdout/stderr and ``--registry`` for filesystem isolation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from get_plugin_install_path import main, stale_root_warning  # noqa: E402


def _write_registry(path: Path, payload: dict) -> None:
    """Write a fake ``installed_plugins.json`` payload at ``path``."""
    path.write_text(json.dumps(payload), encoding="utf-8")


class TestMain:
    """Main: CLI surface — stdout, stderr, exit codes."""

    def test_returns_install_path_when_entry_found(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Single-entry plugin: installPath printed to stdout, exit 0."""
        reg = tmp_path / "installed_plugins.json"
        _write_registry(
            reg,
            {
                "plugins": {
                    "foundry@borda-ai-rig": [
                        {
                            "installedAt": "2026-05-01T00:00:00Z",
                            "installPath": "/cache/borda-ai-rig/foundry/0.18.0",
                        }
                    ]
                }
            },
        )

        rc = main(["borda-ai-rig", "foundry", "--registry", str(reg)])

        assert rc == 0
        out = capsys.readouterr().out.strip()
        assert out == "/cache/borda-ai-rig/foundry/0.18.0"

    def test_picks_most_recent_when_multiple_entries(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Multiple installs: latest installedAt wins regardless of array order."""
        reg = tmp_path / "installed_plugins.json"
        _write_registry(
            reg,
            {
                "plugins": {
                    "foundry@borda-ai-rig": [
                        {"installedAt": "2026-01-01T00:00:00Z", "installPath": "/old/0.10.0"},
                        {"installedAt": "2026-05-15T00:00:00Z", "installPath": "/new/0.20.0"},
                        {"installedAt": "2026-03-01T00:00:00Z", "installPath": "/mid/0.15.0"},
                    ]
                }
            },
        )

        rc = main(["borda-ai-rig", "foundry", "--registry", str(reg)])

        assert rc == 0
        assert capsys.readouterr().out.strip() == "/new/0.20.0"

    def test_exits_1_when_plugin_not_installed(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Lookup key absent from registry → exit 1 with stderr message."""
        reg = tmp_path / "installed_plugins.json"
        _write_registry(reg, {"plugins": {"other@market": []}})

        rc = main(["borda-ai-rig", "foundry", "--registry", str(reg)])

        assert rc == 1
        err = capsys.readouterr().err
        assert "not found" in err
        assert "foundry@borda-ai-rig" in err

    def test_exits_1_when_registry_file_missing(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Registry file absent → exit 1 with stderr message."""
        missing = tmp_path / "no-such-file.json"

        rc = main(["borda-ai-rig", "foundry", "--registry", str(missing)])

        assert rc == 1
        assert "not found" in capsys.readouterr().err

    def test_exits_1_when_registry_malformed(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Registry contains invalid JSON → exit 1 (no traceback to stderr)."""
        reg = tmp_path / "installed_plugins.json"
        reg.write_text("not json{", encoding="utf-8")

        rc = main(["borda-ai-rig", "foundry", "--registry", str(reg)])

        assert rc == 1
        assert "not found" in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("marketplace", "plugin"),
        [
            pytest.param("", "foundry", id="empty"),
            pytest.param("borda-ai-rig", "", id="borda-ai-rig-empty"),
            pytest.param("../etc", "foundry", id="..-etc"),
            pytest.param("borda-ai-rig", "../etc", id="borda-ai-rig-..-etc"),
            pytest.param("foo bar", "foundry", id="foo-bar"),
            pytest.param("borda-ai-rig", "foo/bar", id="borda-ai-rig-foo-bar"),
        ],
    )
    def test_exits_2_on_invalid_args(
        self,
        marketplace: str,
        plugin: str,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Invalid marketplace or plugin token → exit 2 with stderr message."""
        rc = main([marketplace, plugin, "--registry", "/tmp/x"])

        assert rc == 2
        assert "error" in capsys.readouterr().err.lower()

    def test_entry_without_install_path_skipped(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Entries lacking installPath are ignored; falls back to next candidate or exit 1."""
        reg = tmp_path / "installed_plugins.json"
        _write_registry(
            reg,
            {
                "plugins": {
                    "foundry@borda-ai-rig": [
                        {"installedAt": "2026-05-15T00:00:00Z"},
                        {"installedAt": "2026-01-01T00:00:00Z", "installPath": "/old/0.10.0"},
                    ]
                }
            },
        )

        rc = main(["borda-ai-rig", "foundry", "--registry", str(reg)])

        assert rc == 0
        # Latest-with-installPath wins — the newer entry without installPath was skipped.
        assert capsys.readouterr().out.strip() == "/old/0.10.0"


def _cached_root(config: Path, version: str, *, orphaned: bool = False) -> Path:
    """Create one cached foundry version dir, optionally carrying the orphan marker."""
    root = config / "plugins" / "cache" / "borda-ai-rig" / "foundry" / version
    root.mkdir(parents=True)
    if orphaned:
        (root / ".orphaned_at").write_text("1790681924819", encoding="utf-8")
    return root


class TestStaleRootWarning:
    """stale_root_warning: flag a loaded plugin root that is not the installed copy."""

    @pytest.mark.parametrize(
        ("loaded", "orphaned", "expected"),
        [
            pytest.param("0.63.0", False, "", id="installed-root-silent"),
            pytest.param("0.62.1", False, "running foundry 0.62.1, installed 0.63.0", id="older-root-warns"),
            pytest.param("0.62.1", True, "foundry 0.62.1 (replaced — marked orphaned)", id="orphaned-root-warns"),
            pytest.param("0.63.0", True, "foundry 0.63.0 (replaced — marked orphaned)", id="orphan-marker-alone-warns"),
        ],
    )
    def test_compares_loaded_root_with_registry(
        self, tmp_path: Path, loaded: str, orphaned: bool, expected: str
    ) -> None:
        """The loaded root warns exactly when it is marked orphaned or is not the registry's install path.

        A long-running session keeps executing the version it loaded at start; this is the check that makes that
        visible instead of silently running old skill steps.
        """
        installed = _cached_root(tmp_path, "0.63.0", orphaned=orphaned and loaded == "0.63.0")
        root = installed if loaded == "0.63.0" else _cached_root(tmp_path, loaded, orphaned=orphaned)
        _write_registry(
            tmp_path / "plugins" / "installed_plugins.json",
            {"plugins": {"foundry@borda-ai-rig": [{"installPath": str(installed), "version": "0.63.0"}]}},
        )

        warning = stale_root_warning(root, tmp_path)

        assert (expected in warning) if expected else warning == ""

    def test_silent_outside_the_plugin_cache(self, tmp_path: Path) -> None:
        """A source-tree root has no install record to compare against."""
        source = tmp_path / "workspace" / "plugins" / "cc_foundry"
        source.mkdir(parents=True)

        assert stale_root_warning(source, tmp_path) == ""

    def test_silent_without_registry(self, tmp_path: Path) -> None:
        """A missing registry cannot prove staleness."""
        assert stale_root_warning(_cached_root(tmp_path, "0.62.1"), tmp_path) == ""

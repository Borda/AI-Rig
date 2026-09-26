"""Tests for ``bin/get_plugin_install_path.py``.

Doctests in the source cover the pure helpers (``pick_latest_install_path``, ``resolve_install_path``). This file
exercises the CLI surface via ``main()`` with ``capsys`` for stdout/stderr and ``--registry`` for filesystem isolation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from get_plugin_install_path import main  # noqa: E402


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

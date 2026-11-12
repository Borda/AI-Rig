"""Tests for ``bin/resolve_plugin_root.py``.

Doctests in the source cover the pure helpers (``lookup_registry``, ``scan_cache``). This file exercises the CLI surface
via ``main()`` with a patched home directory so the registry/cache cascade and the two security gates resolve against an
isolated tree.
"""

from __future__ import annotations

import io
import json
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from resolve_plugin_root import lookup_registry, main


@pytest.mark.parametrize("timestamp", [None, 7, False, pytest.param([], id="list"), pytest.param({}, id="object")])
def test_registry_non_string_timestamp_sorts_as_undated(tmp_path: Path, timestamp: object) -> None:
    """Malformed timestamps are empty sort keys, leaving the dated installation authoritative."""
    registry = tmp_path / "installed_plugins.json"
    registry.write_text(
        json.dumps(
            {
                "plugins": {
                    "foundry@market": [
                        {"installPath": "undated", "installedAt": timestamp},
                        {"installPath": "dated", "installedAt": "2026-01-01"},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    assert lookup_registry(registry, "market", "foundry") == "dated"


def _make_install(home: Path, version: str, *, name: str = "foundry") -> Path:
    """Create a cache install dir with a ``.claude-plugin/plugin.json`` manifest."""
    root = home / ".claude" / "plugins" / "cache" / "borda-ai-rig" / "foundry" / version
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": name}), encoding="utf-8")
    (root / "bin").mkdir()
    (root / "bin/resolve_plugin_root.py").write_text("# resolver\n", encoding="utf-8")
    return root


def _write_registry(home: Path, install_path: str, *, installed_at: str = "2026-05-01T00:00:00Z") -> None:
    """Write a fake ``installed_plugins.json`` pointing foundry at ``install_path``."""
    reg = home / ".claude" / "plugins" / "installed_plugins.json"
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text(
        json.dumps({"plugins": {"foundry@borda-ai-rig": [{"installedAt": installed_at, "installPath": install_path}]}}),
        encoding="utf-8",
    )


def _registry_hit(home: Path) -> Path:
    """Register one valid install in the registry and return it as the expected answer."""
    install = _make_install(home, "0.27.8")
    _write_registry(home, str(install))
    return install


def _newest_cache_version(home: Path) -> Path:
    """Cache two versions without any registry entry; the newest is the expected answer."""
    _make_install(home, "0.10.0")
    return _make_install(home, "0.27.8")


def _newer_orphan_skipped(home: Path) -> Path:
    """Cache an older valid version and a newer orphaned one; the older is the expected answer."""
    valid = _make_install(home, "0.10.0")
    orphaned = _make_install(home, "0.27.8")
    (orphaned / ".orphaned_at").write_text("2026-06-01", encoding="utf-8")
    return valid


def _manifest_name_mismatch(home: Path) -> None:
    """Register an install whose manifest names a different plugin ("malicious")."""
    _write_registry(home, str(_make_install(home, "0.27.8", name="malicious")))


def _registry_outside_cache(home: Path) -> None:
    """Register an install located outside the plugin cache even though its manifest names foundry."""
    outside = home / "evil"
    (outside / ".claude-plugin").mkdir(parents=True)
    (outside / ".claude-plugin" / "plugin.json").write_text('{"name": "foundry"}', encoding="utf-8")
    _write_registry(home, str(outside))


@pytest.fixture(name="fake_home")
def _fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Make ``os.path.expanduser`` resolve every ``~`` to ``tmp_path`` and return that directory."""
    monkeypatch.setattr("os.path.expanduser", lambda _p: str(tmp_path))
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestMain:
    """Main: CLI surface — stdout, stderr, exit codes."""

    def test_root_output_uses_lf_with_a_windows_text_stream(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bash command substitution must receive a root without a trailing carriage return."""
        expected = _registry_hit(fake_home)
        buffer = io.BytesIO()
        with io.TextIOWrapper(buffer, encoding="utf-8", newline="\r\n") as output:
            with monkeypatch.context() as stream_patch:
                stream_patch.setattr(sys, "stdout", output)
                result = main(["--plugin-name", "foundry"])
                output.flush()
                emitted = buffer.getvalue()

        assert result == 0
        assert emitted == (expected.as_posix() + "\n").encode("utf-8")
        assert b"\r" not in emitted

    @pytest.mark.parametrize(
        "arrange",
        [
            pytest.param(_registry_hit, id="registry-entry"),
            pytest.param(_newest_cache_version, id="cache-scan-picks-newest"),
            pytest.param(_newer_orphan_skipped, id="cache-scan-skips-orphan"),
        ],
    )
    def test_prints_validated_root(
        self, fake_home: Path, capsys: pytest.CaptureFixture[str], arrange: Callable[[Path], Path]
    ) -> None:
        """A resolvable install prints its root on stdout and exits 0.

        Scenario: a registry entry that passes both security gates wins; with no registry entry the cache scan returns
        the newest version, skipping one that carries ``.orphaned_at``.
        """
        expected = arrange(fake_home)

        rc = main(["--plugin-name", "foundry"])

        assert rc == 0
        assert Path(capsys.readouterr().out.strip()) == expected

    def test_exits_1_when_not_found_anywhere(self, fake_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Empty registry and empty cache → exit 1 with diagnostic.

        Scenario: nothing is installed for the requested plugin, so the CLI must say so instead of guessing a path.
        """
        rc = main(["--plugin-name", "foundry"])

        assert rc == 1
        assert "not found" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "arrange",
        [
            pytest.param(_manifest_name_mismatch, id="manifest-name-mismatch"),
            pytest.param(_registry_outside_cache, id="root-outside-cache"),
        ],
    )
    def test_exits_2_on_security_violation(
        self, fake_home: Path, capsys: pytest.CaptureFixture[str], arrange: Callable[[Path], None]
    ) -> None:
        """A resolved root failing a security gate aborts with exit 2 and a SECURITY diagnostic.

        Scenario: the registry points at an install whose manifest names a different plugin, or at a directory outside
        the plugin cache. Skills run what this resolver returns, so neither may be accepted.
        """
        arrange(fake_home)

        rc = main(["--plugin-name", "foundry"])

        assert rc == 2
        assert "SECURITY" in capsys.readouterr().err

    @pytest.mark.parametrize("plugin", ["", "../etc", "foo/bar"])
    def test_exits_2_on_invalid_plugin_token(self, plugin: str, capsys: pytest.CaptureFixture[str]) -> None:
        """Invalid plugin token → exit 2 with stderr message.

        Scenario: an empty name, a path-traversing name and a name containing a separator are rejected before any
        lookup, so the plugin name can never steer the search outside the cache.
        """
        rc = main(["--plugin-name", plugin])

        assert rc == 2
        assert "error" in capsys.readouterr().err.lower()

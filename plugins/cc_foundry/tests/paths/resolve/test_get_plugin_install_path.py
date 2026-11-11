"""Tests for ``bin/get_plugin_install_path.py``.

Doctests in the source cover the pure helpers (``pick_latest_install_path``, ``resolve_install_path``). This file
exercises the CLI surface via ``main()`` with ``capsys`` for stdout/stderr and ``--registry`` for filesystem isolation.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from get_plugin_install_path import main, pick_latest_install_path, stale_root_warning


@pytest.mark.parametrize("timestamp", [None, 7, False, pytest.param([], id="list"), pytest.param({}, id="object")])
def test_non_string_timestamp_keeps_undated_install(timestamp: object) -> None:
    """An undated but usable installation remains available when its timestamp has the wrong type."""
    assert pick_latest_install_path([{"installPath": "available", "installedAt": timestamp}]) == "available"


@pytest.mark.parametrize("timestamp", [None, 7, False, pytest.param([], id="list"), pytest.param({}, id="object")])
def test_valid_timestamp_outranks_non_string_timestamp(timestamp: object) -> None:
    """A dated installation sorts ahead of a malformed timestamp rather than raising or shadowing it."""
    assert (
        pick_latest_install_path(
            [
                {"installPath": "undated", "installedAt": timestamp},
                {"installPath": "dated", "installedAt": "2026-01-01"},
            ]
        )
        == "dated"
    )


def _write_registry(path: Path, payload: dict) -> None:
    """Write a fake ``installed_plugins.json`` payload at ``path``."""
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def registry(tmp_path: Path) -> Callable[[list[dict]], Path]:
    """Return a factory writing a registry holding the given ``foundry@borda-ai-rig`` entries and returning its path."""

    def _make(entries: list[dict]) -> Path:
        path = tmp_path / "installed_plugins.json"
        _write_registry(path, {"plugins": {"foundry@borda-ai-rig": entries}})
        return path

    return _make


class TestMain:
    """CLI surface of ``main()``: stdout, stderr, exit codes."""

    @pytest.mark.parametrize(
        ("entries", "expected"),
        [
            pytest.param(
                [{"installedAt": "2026-05-01T00:00:00Z", "installPath": "/cache/borda-ai-rig/foundry/0.18.0"}],
                "/cache/borda-ai-rig/foundry/0.18.0",
                id="single-entry",
            ),
            pytest.param(
                [
                    {"installedAt": "2026-01-01T00:00:00Z", "installPath": "/old/0.10.0"},
                    {"installedAt": "2026-05-15T00:00:00Z", "installPath": "/new/0.20.0"},
                    {"installedAt": "2026-03-01T00:00:00Z", "installPath": "/mid/0.15.0"},
                ],
                "/new/0.20.0",
                id="latest-installed-at-wins-regardless-of-order",
            ),
            pytest.param(
                [
                    {"installedAt": "2026-05-15T00:00:00Z"},
                    {"installedAt": "2026-01-01T00:00:00Z", "installPath": "/old/0.10.0"},
                ],
                "/old/0.10.0",
                id="entry-without-install-path-skipped",
            ),
        ],
    )
    def test_prints_install_path(
        self,
        registry: Callable[[list[dict]], Path],
        capsys: pytest.CaptureFixture[str],
        entries: list[dict],
        expected: str,
    ) -> None:
        """The registry's most recent entry that carries an ``installPath`` is printed to stdout with exit 0.

        Skills resolve a plugin's installed directory through this CLI; a newer entry without a path must not shadow an
        older usable one, and array order must not matter.
        """
        rc = main(["borda-ai-rig", "foundry", "--registry", str(registry(entries))])

        assert rc == 0
        assert capsys.readouterr().out.strip() == expected

    def test_exits_1_when_plugin_not_installed(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A lookup key absent from the registry exits 1 with a stderr message naming the key.

        The registry lists another marketplace's plugin only, so the requested ``foundry@borda-ai-rig`` is unknown.
        """
        reg = tmp_path / "installed_plugins.json"
        _write_registry(reg, {"plugins": {"other@market": []}})

        rc = main(["borda-ai-rig", "foundry", "--registry", str(reg)])

        err = capsys.readouterr().err
        assert rc == 1
        assert "not found" in err
        assert "foundry@borda-ai-rig" in err

    @pytest.mark.parametrize(
        "prepare",
        [
            pytest.param(lambda path: None, id="registry-file-missing"),
            pytest.param(lambda path: path.write_text("not json{", encoding="utf-8"), id="registry-malformed"),
        ],
    )
    def test_exits_1_when_registry_unusable(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], prepare: Callable[[Path], object]
    ) -> None:
        """A missing or invalid-JSON registry exits 1 with a stderr message and no traceback.

        A fresh machine has no registry yet and a half-written one must not crash callers with a stack trace.
        """
        reg = tmp_path / "installed_plugins.json"
        prepare(reg)

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
        self, tmp_path: Path, marketplace: str, plugin: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An invalid marketplace or plugin token exits 2 with an error on stderr.

        Tokens become registry keys and path components, so empty values, separators and whitespace are refused before
        the registry is read.
        """
        rc = main([marketplace, plugin, "--registry", str(tmp_path / "x")])

        assert rc == 2
        assert "error" in capsys.readouterr().err.lower()


def _cached_root(config: Path, version: str, *, orphaned: bool = False) -> Path:
    """Create one cached foundry version dir, optionally carrying the orphan marker."""
    root = config / "plugins" / "cache" / "borda-ai-rig" / "foundry" / version
    root.mkdir(parents=True)
    if orphaned:
        (root / ".orphaned_at").write_text("1790681924819", encoding="utf-8")
    return root


class TestStaleRootWarning:
    """``stale_root_warning`` flags a loaded plugin root that is not the installed copy."""

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

        A long-running session keeps executing the version it loaded at start; this is the check that makes that visible
        instead of silently running old skill steps.
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
        """A source-tree root has no install record to compare against, so no warning is produced.

        Developers run plugins straight from the checkout; warning there would be noise on every session.
        """
        source = tmp_path / "workspace" / "plugins" / "cc_foundry"
        source.mkdir(parents=True)

        assert stale_root_warning(source, tmp_path) == ""

    def test_silent_without_registry(self, tmp_path: Path) -> None:
        """A missing registry cannot prove staleness, so a cached root stays silent.

        Without install records there is nothing to compare the loaded version against.
        """
        assert stale_root_warning(_cached_root(tmp_path, "0.62.1"), tmp_path) == ""

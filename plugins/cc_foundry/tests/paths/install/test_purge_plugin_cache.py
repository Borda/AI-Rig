"""Tests for ``bin/purge_plugin_cache.py``.

Doctests cover the pure helpers (``version_key``, ``age_hours``). This file exercises the guard predicates and the CLI
against a real temporary cache tree, because the whole point of the script is deciding what is safe to delete — mocks
would test the mock. Every test asserts on the filesystem after the run.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from purge_plugin_cache import main

_MARKET = "borda-ai-rig"
_HOUR_MS = 3_600_000


def _orphan_ms(hours_ago: float) -> int:
    """Return an epoch-ms stamp *hours_ago* hours in the past."""
    return int(time.time() * 1000) - int(hours_ago * _HOUR_MS)


def _make_version(cache: Path, plugin: str, version: str, *, orphan_hours: float | None = None) -> Path:
    """Create one cache version dir; mark it orphaned when *orphan_hours* is given."""
    vdir = cache / _MARKET / plugin / version
    (vdir / ".claude-plugin").mkdir(parents=True)
    (vdir / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": plugin, "version": version}))
    (vdir / "skills").mkdir()
    (vdir / "skills" / "note.md").write_text("payload\n")
    if orphan_hours is not None:
        (vdir / ".orphaned_at").write_text(f"{_orphan_ms(orphan_hours)}\n")
    return vdir


def _write_registry(path: Path, entries: dict[str, str]) -> Path:
    """Write an ``installed_plugins.json`` mapping ``plugin`` → current installPath."""
    payload = {
        "plugins": {
            f"{name}@{_MARKET}": [{"installedAt": "2026-01-01T00:00:00Z", "installPath": install_path}]
            for name, install_path in entries.items()
        }
    }
    path.write_text(json.dumps(payload))
    return path


@pytest.fixture(name="cache")
def _cache(tmp_path: Path) -> Path:
    """Empty cache root."""
    root = tmp_path / "cache"
    (root / _MARKET).mkdir(parents=True)
    return root


@pytest.fixture(name="registry")
def _registry(tmp_path: Path) -> Path:
    """Registry path with no plugins recorded (callers overwrite as needed)."""
    return _write_registry(tmp_path / "installed_plugins.json", {})


def _run(cache: Path, registry: Path, *extra: str) -> int:
    """Invoke the CLI against the fake tree."""
    return main(["--cache-dir", str(cache), "--registry", str(registry), "--marketplace", _MARKET, *extra])


def _live_version(cache: Path) -> tuple[Path, list[str]]:
    """Build a version dir with no ``.orphaned_at`` marker, purged with plain ``--apply``."""
    return _make_version(cache, "live", "0.3.0"), ["--apply"]


def _young_orphan(cache: Path) -> tuple[Path, list[str]]:
    """Build a version orphaned 2 hours ago, purged with a 24-hour age floor."""
    vdir = _make_version(cache, "ghost", "0.1.0", orphan_hours=2)
    return vdir, ["--apply", "--min-orphan-age-hours", "24"]


def _unparseable_marker(cache: Path) -> tuple[Path, list[str]]:
    """Build an old orphan whose ``.orphaned_at`` holds garbage."""
    vdir = _make_version(cache, "ghost", "0.1.0", orphan_hours=100)
    (vdir / ".orphaned_at").write_text("not-a-number\n")
    return vdir, ["--apply"]


def _manifest_less_dir(cache: Path) -> tuple[Path, list[str]]:
    """Build an old-orphan-marked directory that lacks ``.claude-plugin/plugin.json``."""
    stray = cache / _MARKET / "ghost" / "scratch"
    stray.mkdir(parents=True)
    (stray / ".orphaned_at").write_text(f"{_orphan_ms(100)}\n")
    return stray, ["--apply"]


def _protected_version(cache: Path) -> tuple[Path, list[str]]:
    """Build an old orphan shielded by an explicit ``--protect`` argument."""
    vdir = _make_version(cache, "ghost", "0.1.0", orphan_hours=100)
    return vdir, ["--apply", "--protect", str(vdir)]


class TestContract:
    """Normal use: an old orphan of an unregistered plugin is reclaimable."""

    def test_report_lists_candidate_and_deletes_nothing(
        self, cache: Path, registry: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Report mode names the candidate, exits 0, and leaves it on disk.

        Scenario: an old orphan of an unregistered plugin exists and no ``--apply`` is given. The default run is a
        dry report, so the user can review what would be deleted first.
        """
        vdir = _make_version(cache, "ghost", "0.1.0", orphan_hours=100)

        code = _run(cache, registry)

        assert code == 0
        assert "ghost/0.1.0" in capsys.readouterr().out
        assert vdir.is_dir()

    def test_apply_deletes_exactly_the_candidate(self, cache: Path, registry: Path) -> None:
        """``--apply`` removes the candidate and spares a non-orphaned sibling.

        Scenario: one orphaned ghost version and one live version of another plugin sit in the cache. Only the orphan
        goes.
        """
        doomed = _make_version(cache, "ghost", "0.1.0", orphan_hours=100)
        kept = _make_version(cache, "other", "0.2.0")

        code = _run(cache, registry, "--apply")

        assert code == 0
        assert not doomed.exists()
        assert kept.is_dir()

    def test_report_says_nothing_to_purge_when_clean(
        self, cache: Path, registry: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A cache with no orphans reports the no-op state explicitly.

        Scenario: only a live, non-orphaned version exists, so there is nothing to reclaim.
        """
        _make_version(cache, "live", "0.3.0")

        code = _run(cache, registry)

        assert code == 0
        assert "nothing to purge" in capsys.readouterr().out


class TestGuards:
    """Each guard must independently prevent deletion."""

    @pytest.mark.parametrize(
        "build",
        [
            pytest.param(_live_version, id="no-orphan-marker"),
            pytest.param(_young_orphan, id="younger-than-age-floor"),
            pytest.param(_unparseable_marker, id="unparseable-marker"),
            pytest.param(_manifest_less_dir, id="no-plugin-manifest"),
            pytest.param(_protected_version, id="protected-path"),
        ],
    )
    def test_keeps_directory(
        self, build: Callable[[Path], tuple[Path, list[str]]], cache: Path, registry: Path
    ) -> None:
        """A directory failing any single purge guard survives ``--apply``.

        Scenario: no orphan marker means a live version; a young orphan may still be used by a session; garbage in
        ``.orphaned_at`` fails safe; a dir without ``plugin.json`` is not a version dir; ``--protect`` shields a dir
        that would otherwise qualify.
        """
        vdir, args = build(cache)

        _run(cache, registry, *args)

        assert vdir.is_dir()

    def test_keeps_registry_install_path(self, cache: Path, tmp_path: Path) -> None:
        """The version the registry points at survives even when marked orphaned.

        Scenario: the registry records ``live/0.3.0`` as installed while a newer ``0.4.0`` also exists.
        """
        vdir = _make_version(cache, "live", "0.3.0", orphan_hours=100)
        _make_version(cache, "live", "0.4.0")
        reg = _write_registry(tmp_path / "reg.json", {"live": str(vdir)})

        _run(cache, reg, "--apply")

        assert vdir.is_dir()

    def test_keeps_newest_version_of_registered_plugin(self, cache: Path, tmp_path: Path) -> None:
        """Registry lag guard: newest cached version of a live plugin is never purged.

        Scenario: the registry record points outside the cache and both cached versions are old orphans. The older
        version goes, the newest stays so a plugin the registry still knows is never left without an install.
        """
        old = _make_version(cache, "live", "0.9.0", orphan_hours=100)
        newest = _make_version(cache, "live", "0.10.0", orphan_hours=100)
        reg = _write_registry(tmp_path / "reg.json", {"live": "/elsewhere/not-in-cache"})

        _run(cache, reg, "--apply")

        assert newest.is_dir()
        assert not old.exists()

    def test_purges_newest_version_of_unregistered_plugin(self, cache: Path, registry: Path) -> None:
        """A plugin absent from the registry has no live consumer — all versions go.

        This is the renamed/uninstalled-plugin case (e.g. ``codemap`` after the rename to ``codemap-py``); nothing else
        would ever reclaim its tree.
        """
        newest = _make_version(cache, "ghost", "0.10.0", orphan_hours=100)

        _run(cache, registry, "--apply")

        assert not newest.exists()


class TestErrors:
    """Argument and consistency failures must not delete anything."""

    def test_expect_count_mismatch_aborts_without_deleting(self, cache: Path, registry: Path) -> None:
        """A changed candidate count means the user's confirmation is stale.

        Scenario: the user confirmed five candidates but only one exists now. ``--apply`` exits 1 and deletes nothing.
        """
        vdir = _make_version(cache, "ghost", "0.1.0", orphan_hours=100)

        code = _run(cache, registry, "--apply", "--expect-count", "5")

        assert code == 1
        assert vdir.is_dir()

    def test_missing_cache_dir_exits_1(self, tmp_path: Path, registry: Path) -> None:
        """An absent cache root is an error, not an empty report.

        Scenario: ``--cache-dir`` names a path that does not exist, as after a mistyped path.
        """
        assert _run(tmp_path / "__absent__", registry) == 1

    @pytest.mark.parametrize(
        ("flag", "value"),
        [
            pytest.param("--marketplace", "../etc", id="marketplace-traversal"),
            pytest.param("--min-orphan-age-hours", "-1", id="negative-age"),
        ],
    )
    def test_bad_argument_exits_2(self, cache: Path, registry: Path, flag: str, value: str) -> None:
        """Rejected arguments exit 2 before any scan happens.

        Scenario: a path-traversing marketplace name or a negative age floor would widen what the purge could touch.
        """
        assert _run(cache, registry, flag, value) == 2

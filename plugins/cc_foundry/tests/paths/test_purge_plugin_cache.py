"""Tests for ``bin/purge_plugin_cache.py``.

Doctests cover the pure helpers (``version_key``, ``age_hours``). This file exercises the guard predicates and the CLI
against a real temporary cache tree, because the whole point of the script is deciding what is safe to delete — mocks
would test the mock. Every test asserts on the filesystem after the run.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from purge_plugin_cache import main  # noqa: E402

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
        },
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


class TestContract:
    """Normal use: an old orphan of an unregistered plugin is reclaimable."""

    def test_report_lists_candidate_and_deletes_nothing(
        self,
        cache: Path,
        registry: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Report mode names the candidate, exits 0, and leaves it on disk."""
        vdir = _make_version(cache, "ghost", "0.1.0", orphan_hours=100)

        code = _run(cache, registry)

        assert code == 0
        assert "ghost/0.1.0" in capsys.readouterr().out
        assert vdir.is_dir()

    def test_apply_deletes_exactly_the_candidate(self, cache: Path, registry: Path) -> None:
        """Verify command-line option behavior.

        ``--apply`` removes the candidate and spares a non-orphaned sibling.
        """
        doomed = _make_version(cache, "ghost", "0.1.0", orphan_hours=100)
        kept = _make_version(cache, "other", "0.2.0")

        code = _run(cache, registry, "--apply")

        assert code == 0
        assert not doomed.exists()
        assert kept.is_dir()

    def test_report_says_nothing_to_purge_when_clean(
        self,
        cache: Path,
        registry: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A cache with no orphans reports the no-op state explicitly."""
        _make_version(cache, "live", "0.3.0")

        code = _run(cache, registry)

        assert code == 0
        assert "nothing to purge" in capsys.readouterr().out


class TestGuards:
    """Each guard must independently prevent deletion."""

    def test_keeps_version_without_orphan_marker(self, cache: Path, registry: Path) -> None:
        """No ``.orphaned_at`` → live version, never touched."""
        vdir = _make_version(cache, "live", "0.3.0")

        _run(cache, registry, "--apply")

        assert vdir.is_dir()

    def test_keeps_orphan_younger_than_age_floor(self, cache: Path, registry: Path) -> None:
        """Recently-orphaned version is deferred — a live session may still use it."""
        vdir = _make_version(cache, "ghost", "0.1.0", orphan_hours=2)

        _run(cache, registry, "--apply", "--min-orphan-age-hours", "24")

        assert vdir.is_dir()

    def test_keeps_orphan_with_unparseable_marker(self, cache: Path, registry: Path) -> None:
        """Garbage in ``.orphaned_at`` fails safe — treated as not orphaned."""
        vdir = _make_version(cache, "ghost", "0.1.0", orphan_hours=100)
        (vdir / ".orphaned_at").write_text("not-a-number\n")

        _run(cache, registry, "--apply")

        assert vdir.is_dir()

    def test_keeps_dir_without_plugin_manifest(self, cache: Path, registry: Path) -> None:
        """A dir lacking ``.claude-plugin/plugin.json`` is not a version dir."""
        stray = cache / _MARKET / "ghost" / "scratch"
        stray.mkdir(parents=True)
        (stray / ".orphaned_at").write_text(f"{_orphan_ms(100)}\n")

        _run(cache, registry, "--apply")

        assert stray.is_dir()

    def test_keeps_registry_install_path(self, cache: Path, tmp_path: Path) -> None:
        """The version the registry points at survives even when marked orphaned."""
        vdir = _make_version(cache, "live", "0.3.0", orphan_hours=100)
        _make_version(cache, "live", "0.4.0")
        reg = _write_registry(tmp_path / "reg.json", {"live": str(vdir)})

        _run(cache, reg, "--apply")

        assert vdir.is_dir()

    def test_keeps_protected_path(self, cache: Path, registry: Path) -> None:
        """Verify command-line option behavior.

        ``--protect`` shields a dir that would otherwise qualify.
        """
        vdir = _make_version(cache, "ghost", "0.1.0", orphan_hours=100)

        _run(cache, registry, "--apply", "--protect", str(vdir))

        assert vdir.is_dir()

    def test_keeps_newest_version_of_registered_plugin(self, cache: Path, tmp_path: Path) -> None:
        """Registry lag guard: newest cached version of a live plugin is never purged."""
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
        """A changed candidate count means the user's confirmation is stale."""
        vdir = _make_version(cache, "ghost", "0.1.0", orphan_hours=100)

        code = _run(cache, registry, "--apply", "--expect-count", "5")

        assert code == 1
        assert vdir.is_dir()

    def test_missing_cache_dir_exits_1(self, tmp_path: Path, registry: Path) -> None:
        """An absent cache root is an error, not an empty report."""
        assert _run(tmp_path / "__absent__", registry) == 1

    @pytest.mark.parametrize(
        ("flag", "value"),
        [
            pytest.param("--marketplace", "../etc", id="marketplace-traversal"),
            pytest.param("--min-orphan-age-hours", "-1", id="negative-age"),
        ],
    )
    def test_bad_argument_exits_2(self, cache: Path, registry: Path, flag: str, value: str) -> None:
        """Rejected arguments exit 2 before any scan happens."""
        assert _run(cache, registry, flag, value) == 2

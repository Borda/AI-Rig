"""Tests for ``bin/locate_scan_query.py`` — three-tier scan-query resolver.

Tiers:
1. ``shutil.which("scan-query")`` on PATH (absolute hits only)
2. ``$CLAUDE_PLUGIN_ROOT/bin/scan-query``
3. ``~/.claude/plugins/cache/borda-ai-rig/codemap-py/*/bin/scan-query`` (newest semver)

Exits 0 with resolved path on stdout; exits 1 with stderr when not found.

Covers:
* Tier 2 resolution via CLAUDE_PLUGIN_ROOT
* Tier 3 resolution via cache glob
* Not found → FileNotFoundError / exit 1
* Tier 3 picks newest semver when multiple versions present
* Tier 1 PATH takes precedence over lower tiers
* Windows-portability invariants

``shutil.which`` and the home/plugin-root environment are the only boundaries replaced; every executable is a real file
in ``tmp_path``.
"""

from __future__ import annotations

import importlib.util
import shutil
import stat
from collections.abc import Callable
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent.parent / "bin" / "locate_scan_query.py"
_spec = importlib.util.spec_from_file_location("codemap_locate_scan_query", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

locate_scan_query = _mod.locate_scan_query
main = _mod.main


def _make_executable(path: Path) -> Path:
    """Create a minimal executable file at *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env sh\necho ok\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _cache_exe(home: Path, version: str, *, marketplace: str = "borda-ai-rig", plugin: str = "codemap-py") -> Path:
    """Create ``scan-query`` in the plugin cache layout under *home* for *version*."""
    return _make_executable(
        home / ".claude" / "plugins" / "cache" / marketplace / plugin / version / "bin" / "scan-query"
    )


@pytest.fixture
def configure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Return a setter that points the resolver at ``tmp_path`` as home, with chosen plugin root and PATH hit.

    ``plugin_root`` defaults to unset (empty). ``path_hit`` is what ``shutil.which`` returns; ``None`` simulates no PATH
    hit.
    """

    def _configure(*, plugin_root: Path | None = None, path_hit: Path | str | None = None) -> None:
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "" if plugin_root is None else str(plugin_root))
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows compat
        monkeypatch.setattr(shutil, "which", lambda cmd: None if path_hit is None else str(path_hit))
        monkeypatch.setattr(_mod, "__file__", str(tmp_path / "bin" / "locate_scan_query.py"))

    return _configure


class TestLocateScanQuery:
    """Unit tests for ``locate_scan_query()``."""

    def test_tier1_path_hit(self, tmp_path, configure):
        """Tier 1: an absolute executable found on PATH is used immediately."""
        exe = _make_executable(tmp_path / "scan-query")
        configure(path_hit=exe)

        assert locate_scan_query() == exe

    def test_tier2_claude_plugin_root(self, tmp_path, configure):
        """Tier 2: with no PATH hit, the executable at ``$CLAUDE_PLUGIN_ROOT/bin/scan-query`` is used."""
        exe = _make_executable(tmp_path / "bin" / "scan-query")
        configure(plugin_root=tmp_path)

        assert locate_scan_query() == exe

    def test_tier3_cache_glob(self, tmp_path, configure):
        """Tier 3: resolves via ``~/.claude/plugins/cache/borda-ai-rig/codemap-py/*/bin/scan-query``.

        No PATH hit and no plugin root, so only the cache layout under the fake home can answer.
        """
        cache_exe = _cache_exe(tmp_path, "0.3.0")
        configure()

        assert locate_scan_query() == cache_exe

    def test_tier3_picks_newest_semver(self, tmp_path, configure):
        """Tier 3: the highest semver wins when multiple cache versions are present.

        Versions 0.1.0, 0.2.1 and 0.3.2 exist; a lexical or first-found pick would not return 0.3.2 reliably.
        """
        _cache_exe(tmp_path, "0.1.0")
        _cache_exe(tmp_path, "0.2.1")
        newest = _cache_exe(tmp_path, "0.3.2")
        configure()

        assert locate_scan_query() == newest

    def test_raises_when_all_tiers_empty(self, configure):
        """With no PATH hit, plugin root or cache entry, ``FileNotFoundError`` is raised."""
        configure()

        with pytest.raises(FileNotFoundError):
            locate_scan_query()

    @pytest.mark.parametrize(
        ("marketplace", "plugin"),
        [
            pytest.param("other-marketplace", "codemap-py", id="foreign-marketplace"),
            pytest.param("borda-ai-rig", "codemap", id="legacy-plugin-name"),
        ],
    )
    def test_tier3_ignores_unpinned_cache_dirs(self, tmp_path, configure, marketplace, plugin):
        """Tier 3 does not accept a ``scan-query`` from another marketplace or from the pre-rename ``codemap`` name.

        Any marketplace able to ship a plugin directory with a matching name would otherwise supply an executable that
        the caller then runs.
        """
        _cache_exe(tmp_path, "0.3.0", marketplace=marketplace, plugin=plugin)
        configure()

        with pytest.raises(FileNotFoundError):
            locate_scan_query()

    def test_tier1_relative_path_hit_is_skipped(self, tmp_path, configure):
        """A relative PATH hit (from a ``.`` or empty PATH entry) is skipped in favour of the pinned tiers.

        Such a hit resolves against the working directory, which may be an untrusted checkout shipping its own ``scan-
        query``; tier 2 must win instead.
        """
        exe = _make_executable(tmp_path / "bin" / "scan-query")
        configure(plugin_root=tmp_path, path_hit="./scan-query")

        assert locate_scan_query() == exe

    def test_tier1_wins_over_tier2(self, tmp_path, configure):
        """Tier 1 (PATH) takes precedence over tier 2 (CLAUDE_PLUGIN_ROOT) when both exist."""
        path_exe = _make_executable(tmp_path / "path_bin" / "scan-query")
        _make_executable(tmp_path / "bin" / "scan-query")  # tier 2 candidate (should be ignored)
        configure(plugin_root=tmp_path, path_hit=path_exe)

        assert locate_scan_query() == path_exe


class TestMain:
    """Integration tests for ``main()``."""

    def test_found_exits_0_and_prints_path(self, tmp_path, configure, capsys):
        """Executable found → exit 0 and absolute path printed to stdout."""
        exe = _make_executable(tmp_path / "bin" / "scan-query")
        configure(plugin_root=tmp_path)

        rc = main([])

        assert rc == 0
        assert capsys.readouterr().out.strip() == str(exe)

    def test_not_found_exits_1(self, configure, capsys):
        """All tiers empty → exit 1 with message on stderr."""
        configure()

        rc = main([])

        assert rc == 1
        assert capsys.readouterr().err.strip() != ""

    def test_output_has_no_crlf(self, tmp_path, configure, capsys):
        """Stdout must not contain CRLF (Windows text-mode regression guard)."""
        _make_executable(tmp_path / "bin" / "scan-query")
        configure(plugin_root=tmp_path)

        main([])

        assert "\r" not in capsys.readouterr().out

    def test_help_exits_0(self, capsys):
        """Print command usage and exit successfully for a help request."""
        with pytest.raises(SystemExit) as exc:
            main(["--help"])

        assert exc.value.code == 0
        assert "usage" in capsys.readouterr().out.lower()

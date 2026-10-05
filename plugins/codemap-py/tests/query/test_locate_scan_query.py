"""Tests for ``bin/locate_scan_query.py`` — three-tier scan-query resolver.

Tiers:
1. ``shutil.which("scan-query")`` on PATH
2. ``$CLAUDE_PLUGIN_ROOT/bin/scan-query``
3. ``~/.claude/plugins/cache/*/codemap/*/bin/scan-query`` (newest semver)

Exits 0 with resolved path on stdout; exits 1 with stderr when not found.

Covers:
* Tier 2 resolution via CLAUDE_PLUGIN_ROOT
* Tier 3 resolution via cache glob
* Not found → FileNotFoundError / exit 1
* Tier 3 picks newest semver when multiple versions present
* Tier 1 PATH takes precedence over lower tiers
* Windows-portability invariants
"""

from __future__ import annotations

import importlib.util
import shutil
import stat
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


def _no_which(cmd: str) -> None:  # type: ignore[return]
    """Stub for ``shutil.which`` that always returns ``None`` (simulates no PATH hit)."""
    return


class TestLocateScanQuery:
    """Unit tests for ``locate_scan_query()``."""

    def test_tier1_path_hit(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tier 1: shutil.which returns executable → used immediately."""
        exe = _make_executable(tmp_path / "scan-query")
        monkeypatch.setattr(shutil, "which", lambda cmd: str(exe))
        result = locate_scan_query()
        assert result == exe

    def test_tier2_claude_plugin_root(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tier 2: executable at ``$CLAUDE_PLUGIN_ROOT/bin/scan-query``."""
        exe = _make_executable(tmp_path / "bin" / "scan-query")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows compat
        monkeypatch.setattr(shutil, "which", _no_which)
        result = locate_scan_query()
        assert result == exe

    def test_tier3_cache_glob(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tier 3: resolves via ``~/.claude/plugins/cache/*/codemap/*/bin/scan-query``."""
        cache_exe = _make_executable(
            tmp_path / ".claude" / "plugins" / "cache" / "borda-ai-rig" / "codemap" / "0.3.0" / "bin" / "scan-query"
        )
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "")
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.setattr(shutil, "which", _no_which)
        result = locate_scan_query()
        assert result == cache_exe

    def test_tier3_picks_newest_semver(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tier 3: highest semver wins when multiple cache versions present."""
        base = tmp_path / ".claude" / "plugins" / "cache" / "borda-ai-rig" / "codemap"
        _make_executable(base / "0.1.0" / "bin" / "scan-query")
        _make_executable(base / "0.2.1" / "bin" / "scan-query")
        newest = _make_executable(base / "0.3.2" / "bin" / "scan-query")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "")
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.setattr(shutil, "which", _no_which)
        result = locate_scan_query()
        assert result == newest

    def test_not_found_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """All tiers empty → ``FileNotFoundError`` raised."""
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "")
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.setattr(shutil, "which", _no_which)
        with pytest.raises(FileNotFoundError):
            locate_scan_query()

    def test_tier1_wins_over_tier2(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Tier 1 (PATH) takes precedence over tier 2 (CLAUDE_PLUGIN_ROOT)."""
        path_exe = _make_executable(tmp_path / "path_bin" / "scan-query")
        _make_executable(tmp_path / "bin" / "scan-query")  # tier 2 candidate (should be ignored)
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.setattr(shutil, "which", lambda cmd: str(path_exe))
        result = locate_scan_query()
        assert result == path_exe


class TestMain:
    """Integration tests for ``main()``."""

    def test_found_exits_0_and_prints_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Executable found → exit 0 and absolute path printed to stdout."""
        exe = _make_executable(tmp_path / "bin" / "scan-query")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.setattr(shutil, "which", _no_which)
        rc = main([])
        assert rc == 0
        assert capsys.readouterr().out.strip() == str(exe)

    def test_not_found_exits_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """All tiers empty → exit 1 with message on stderr."""
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "")
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.setattr(shutil, "which", _no_which)
        rc = main([])
        assert rc == 1
        assert capsys.readouterr().err.strip() != ""

    def test_output_has_no_crlf(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Stdout must not contain CRLF (Windows text-mode regression guard)."""
        _make_executable(tmp_path / "bin" / "scan-query")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.setattr(shutil, "which", _no_which)
        main([])
        assert "\r" not in capsys.readouterr().out

    def test_help_exits_0(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Print command usage and exit successfully for a help request."""
        with pytest.raises(SystemExit) as exc:
            main(["--help"])
        assert exc.value.code == 0
        assert "usage" in capsys.readouterr().out.lower()

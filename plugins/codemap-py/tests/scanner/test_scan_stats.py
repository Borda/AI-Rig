"""Tests for ``bin/scan-stats.py`` — codemap index summary printer.

Covers:
* ``_resolve_root`` precedence: --root arg → git toplevel → cwd
* ``_resolve_root`` directory-traversal guard (--root outside cwd → exit 2)
* ``_load_index`` happy path and missing-file exit
* ``main()`` output for typical index shapes: no modules, ok+degraded, calls
* ``main()`` with no call edges (Calls line absent)
* ``main()`` top-5 ranking by rdep_count
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "bin" / "scan-stats.py"
_spec = importlib.util.spec_from_file_location("codemap_scan_stats", _SCRIPT)
assert _spec and _spec.loader, "scan-stats.py not found in bin/"
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

_resolve_root = _mod._resolve_root
_load_index = _mod._load_index
main = _mod.main


# ---------------------------------------------------------------------------
# _resolve_root
# ---------------------------------------------------------------------------


class TestResolveRoot:
    """_resolve_root: root precedence and directory-traversal guard."""

    def test_returns_absolute_path_from_scan_args(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify command-line option behavior.

        --root within cwd resolves to its absolute form and is returned.
        """
        subdir = tmp_path / "proj"
        subdir.mkdir()
        monkeypatch.chdir(tmp_path)
        result = _resolve_root(f"--root {subdir.as_posix()}", timeout=5)
        assert result == str(subdir)

    def test_traversal_blocked_exits_2(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify command-line option behavior.

        --root outside cwd causes sys.exit(2) — directory traversal guard.
        """
        monkeypatch.chdir(tmp_path)
        outside = tmp_path.parent / "escaped"
        with pytest.raises(SystemExit) as exc_info:
            _resolve_root(f"--root {outside.as_posix()}", timeout=5)
        assert exc_info.value.code == 2

    def test_falls_back_to_git_toplevel(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without ``--root`` in SCAN_ARGS, falls back to ``git rev-parse --show-toplevel``."""
        monkeypatch.chdir(tmp_path)
        fake_root = "/some/git/root"
        with patch("subprocess.check_output", return_value=fake_root.encode()):
            result = _resolve_root("", timeout=5)
        assert result == fake_root

    def test_falls_back_to_cwd_when_git_unavailable(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When git rev-parse raises, cwd is returned as last resort."""
        monkeypatch.chdir(tmp_path)
        with patch("subprocess.check_output", side_effect=Exception("no git")):
            result = _resolve_root("", timeout=5)
        assert result == os.path.abspath(str(tmp_path))

    def test_empty_scan_args_skips_root_parsing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty SCAN_ARGS string does not cause ``--root`` lookup or crash."""
        monkeypatch.chdir(tmp_path)
        with patch("subprocess.check_output", return_value=str(tmp_path).encode()):
            result = _resolve_root("", timeout=5)
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# _load_index
# ---------------------------------------------------------------------------


class TestLoadIndex:
    """_load_index: JSON loading and missing-file error path."""

    def test_returns_parsed_json(self, tmp_path: Path) -> None:
        """Valid index JSON is loaded and returned as dict."""
        proj = tmp_path.name
        idx_dir = tmp_path / ".cache" / "codemap"
        idx_dir.mkdir(parents=True)
        payload: dict[str, Any] = {"modules": [{"name": "mod", "status": "ok", "symbols": []}]}
        (idx_dir / f"{proj}.json").write_text(json.dumps(payload))
        assert _load_index(str(tmp_path)) == payload

    def test_exits_1_when_file_missing(self, tmp_path: Path) -> None:
        """Absent index file causes sys.exit(1) (user hint: run /codemap-py:scan-codebase)."""
        with pytest.raises(SystemExit) as exc_info:
            _load_index(str(tmp_path))
        assert exc_info.value.code == 1

    def test_exits_1_when_index_exceeds_size_limit(self, tmp_path: Path) -> None:
        """Index file larger than MAX_INDEX_SIZE causes sys.exit(1) — DoS guard."""
        proj = tmp_path.name
        idx_dir = tmp_path / ".cache" / "codemap"
        idx_dir.mkdir(parents=True)
        idx_file = idx_dir / f"{proj}.json"
        idx_file.write_text("{}")
        oversized = _mod.MAX_INDEX_SIZE + 1
        with patch("os.path.getsize", return_value=oversized):
            with pytest.raises(SystemExit) as exc_info:
                _load_index(str(tmp_path))
        assert exc_info.value.code == 1

    def test_exits_1_on_race_condition_file_disappears(self, tmp_path: Path) -> None:
        """Handle an index disappearing between its size check and open operation."""
        proj = tmp_path.name
        idx_dir = tmp_path / ".cache" / "codemap"
        idx_dir.mkdir(parents=True)
        idx_file = idx_dir / f"{proj}.json"
        idx_file.write_text("{}")
        with patch("builtins.open", side_effect=FileNotFoundError("gone")):
            with pytest.raises(SystemExit) as exc_info:
                _load_index(str(tmp_path))
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_index(tmp_path: Path, modules: list[dict[str, Any]]) -> None:
    """Write a minimal scan index JSON at the canonical path under *tmp_path*."""
    proj = tmp_path.name
    idx_dir = tmp_path / ".cache" / "codemap"
    idx_dir.mkdir(parents=True, exist_ok=True)
    (idx_dir / f"{proj}.json").write_text(json.dumps({"modules": modules}))


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


class TestMain:
    """Report command-line statistics for every supported index shape."""

    def test_no_modules_prints_message_and_exits_0(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Empty module list emits 'No modules indexed.' and exits 0."""
        _write_index(tmp_path, [])
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SCAN_ARGS", "")
        with patch("subprocess.check_output", return_value=str(tmp_path).encode()):
            with pytest.raises(SystemExit) as exc_info:
                main([])
        assert exc_info.value.code == 0
        assert "No modules indexed." in capsys.readouterr().out

    def test_ok_and_degraded_counts_printed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Indexed and degraded module counts appear in output."""
        modules = [
            {"name": "a", "status": "ok", "rdep_count": 1, "symbols": []},
            {"name": "b", "status": "ok", "rdep_count": 2, "symbols": []},
            {"name": "c", "status": "degraded", "rdep_count": 0, "symbols": []},
        ]
        _write_index(tmp_path, modules)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SCAN_ARGS", "")
        with patch("subprocess.check_output", return_value=str(tmp_path).encode()):
            main([])
        out = capsys.readouterr().out
        assert "2 indexed" in out
        assert "1 degraded" in out

    def test_symbol_total_summed_across_modules(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Total symbol count is printed as sum of all ok-module symbol lists."""
        modules = [
            {"name": "x", "status": "ok", "rdep_count": 0, "symbols": [{}, {}]},
            {"name": "y", "status": "ok", "rdep_count": 0, "symbols": [{}]},
        ]
        _write_index(tmp_path, modules)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SCAN_ARGS", "")
        with patch("subprocess.check_output", return_value=str(tmp_path).encode()):
            main([])
        out = capsys.readouterr().out
        assert "Symbols: 3" in out

    def test_calls_line_present_for_v3_index(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """'Calls:' line is printed when at least one resolved call edge exists."""
        modules = [{"name": "m", "status": "ok", "rdep_count": 0, "symbols": [{"calls": [{"target": "n"}]}]}]
        _write_index(tmp_path, modules)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SCAN_ARGS", "")
        with patch("subprocess.check_output", return_value=str(tmp_path).encode()):
            main([])
        assert "Calls:" in capsys.readouterr().out

    def test_calls_line_absent_when_no_call_edges(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """'Calls:' line is omitted when all call lists are empty."""
        modules = [{"name": "m", "status": "ok", "rdep_count": 0, "symbols": [{"calls": []}]}]
        _write_index(tmp_path, modules)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SCAN_ARGS", "")
        with patch("subprocess.check_output", return_value=str(tmp_path).encode()):
            main([])
        assert "Calls:" not in capsys.readouterr().out

    def test_top_modules_ranked_by_rdep_count(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Module with highest rdep_count appears first in 'Most central' list."""
        modules = [
            {"name": "low", "status": "ok", "rdep_count": 1, "symbols": []},
            {"name": "high", "status": "ok", "rdep_count": 99, "symbols": []},
        ]
        _write_index(tmp_path, modules)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SCAN_ARGS", "")
        with patch("subprocess.check_output", return_value=str(tmp_path).encode()):
            main([])
        out = capsys.readouterr().out
        assert out.index("high") < out.index("low"), "highest rdep_count must appear first"

    def test_degraded_modules_excluded_from_symbol_count(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Degraded modules do not contribute to the symbol total."""
        modules = [
            {"name": "ok_mod", "status": "ok", "rdep_count": 0, "symbols": [{}]},
            {"name": "bad_mod", "status": "degraded", "rdep_count": 0, "symbols": [{}, {}]},
        ]
        _write_index(tmp_path, modules)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SCAN_ARGS", "")
        with patch("subprocess.check_output", return_value=str(tmp_path).encode()):
            main([])
        out = capsys.readouterr().out
        assert "Symbols: 1" in out


# ---------------------------------------------------------------------------
# argparse layer
# ---------------------------------------------------------------------------


class TestArgparse:
    """CLI argument handling: ``--help`` and the real SCAN_ARGS call-site shape."""

    def test_help_exits_0(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Print usage and exit 0 (argparse default)."""
        with pytest.raises(SystemExit) as exc_info:
            main(["--help"])
        assert exc_info.value.code == 0
        assert "scan-stats.py" in capsys.readouterr().out

    def test_golden_call_site_scan_args_env(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Real call site (``SCAN_ARGS`` env, no argv) still prints the summary."""
        modules = [{"name": "m", "status": "ok", "rdep_count": 1, "symbols": [{}]}]
        _write_index(tmp_path, modules)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SCAN_ARGS", "")
        with patch("subprocess.check_output", return_value=str(tmp_path).encode()):
            main([])
        out = capsys.readouterr().out
        assert "Modules: 1 indexed" in out


# ---------------------------------------------------------------------------
# Doctest hookup
# ---------------------------------------------------------------------------


def test_module_doctests_pass() -> None:
    """Doctest examples embedded in scan-stats.py must not regress."""
    import doctest

    results = doctest.testmod(_mod, verbose=False)
    assert results.failed == 0, f"{results.failed} doctest(s) failed"

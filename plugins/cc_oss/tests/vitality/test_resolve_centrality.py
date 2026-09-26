"""Tests for ``bin/resolve_centrality.py``.

Covers path-suffix module matching (exact, scan-root prefix on either side, ``__init__.py`` → package name, no-match),
the combined map build, and the CLI stdin→JSON path including the invalid-input exit.
"""

from __future__ import annotations

import io
import json

import pytest

import resolve_centrality as rc

_MODS = [("pkg", "src/pkg/__init__.py"), ("pkg.auth", "src/pkg/auth.py")]


class TestMatchModule:
    """match_module: resolve a repo-relative file to its canonical codemap module name."""

    @pytest.mark.parametrize(
        ("file", "expected"),
        [
            pytest.param("src/pkg/auth.py", "pkg.auth", id="exact"),
            pytest.param("src/pkg/__init__.py", "pkg", id="init-becomes-package"),
            pytest.param("proj-main/src/pkg/auth.py", "pkg.auth", id="prefix-on-query-side"),
            pytest.param("README.md", None, id="no-match-non-python"),
        ],
    )
    def test_resolution(self, file: str, expected: str | None) -> None:
        """Each file resolves to the expected module name (or None when unknown)."""
        assert rc.match_module(file, _MODS) == expected

    def test_prefix_on_index_side(self) -> None:
        """A scan-root prefix on the index path still matches a bare repo-relative file."""
        assert rc.match_module("src/pkg/auth.py", [("pkg.auth", "a/b/src/pkg/auth.py")]) == "pkg.auth"

    def test_longest_path_wins_on_multi_match(self) -> None:
        """When two module paths both suffix-match, the most specific (longest) one wins."""
        mods = [("pkg", "pkg.py"), ("sub.pkg", "src/sub/pkg.py")]
        assert rc.match_module("src/sub/pkg.py", mods) == "sub.pkg"


class TestBuildMaps:
    """build_maps: central payload + files → centrality map and file→module resolution."""

    def test_centrality_and_resolution(self) -> None:
        """Known file resolves to its module; unknown file maps to empty string."""
        payload = {"central": [{"name": "pkg.auth", "rdep_count": 9, "path": "src/pkg/auth.py"}]}
        result = rc.build_maps(payload, ["src/pkg/auth.py", "docs/x.md"])
        assert result == {
            "centrality": {"pkg.auth": 9},
            "file_module": {"src/pkg/auth.py": "pkg.auth", "docs/x.md": ""},
        }

    def test_empty_central_payload(self) -> None:
        """No modules → empty centrality, every file resolves to empty string."""
        result = rc.build_maps({"central": []}, ["a.py"])
        assert result == {"centrality": {}, "file_module": {"a.py": ""}}


class TestMainCli:
    """Transform centrality input into command-line JSON output."""

    def test_reads_stdin_and_resolves_files(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Resolve requested files against piped centrality data."""
        payload = '{"central": [{"name": "pkg.auth", "rdep_count": 5, "path": "src/pkg/auth.py"}]}'
        monkeypatch.setattr(rc.sys, "stdin", io.StringIO(payload))
        rc_code = rc.main(["--files", "src/pkg/auth.py"])
        out = json.loads(capsys.readouterr().out)
        assert rc_code == 0
        assert out["file_module"]["src/pkg/auth.py"] == "pkg.auth"
        assert out["centrality"]["pkg.auth"] == 5

    def test_invalid_stdin_exits_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Non-JSON stdin → exit 1, no crash."""
        monkeypatch.setattr(rc.sys, "stdin", io.StringIO("not json"))
        assert rc.main(["--files", "a.py"]) == 1


class TestNoFilesArg:
    """Emit centrality with an empty resolution map when no files are supplied."""

    def test_empty_files(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        """No ``--files`` → centrality present, file_module empty."""
        payload = '{"central": [{"name": "pkg", "rdep_count": 3, "path": "src/pkg/__init__.py"}]}'
        monkeypatch.setattr(rc.sys, "stdin", io.StringIO(payload))
        rc.main([])
        out = json.loads(capsys.readouterr().out)
        assert out == {"centrality": {"pkg": 3}, "file_module": {}}

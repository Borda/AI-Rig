"""Tests for ``bin/resolve_centrality.py``.

Covers path-suffix module matching (exact, scan-root prefix on either side, ``__init__.py`` → package name, no-match),
the combined map build, and the CLI stdin→JSON path. Malformed-input behaviour lives in
``test_resolve_centrality_input_shape.py``.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest
import resolve_centrality as rc

_MODS = [("pkg", "src/pkg/__init__.py"), ("pkg.auth", "src/pkg/auth.py")]


class TestMatchModule:
    """match_module: resolve a repo-relative file to its canonical codemap module name."""

    @pytest.mark.parametrize(
        ("file", "mods", "expected"),
        [
            pytest.param("src/pkg/auth.py", _MODS, "pkg.auth", id="exact"),
            pytest.param("src/pkg/__init__.py", _MODS, "pkg", id="init-becomes-package"),
            pytest.param("proj-main/src/pkg/auth.py", _MODS, "pkg.auth", id="prefix-on-query-side"),
            pytest.param("README.md", _MODS, None, id="no-match-non-python"),
            pytest.param(
                "src/pkg/auth.py", [("pkg.auth", "a/b/src/pkg/auth.py")], "pkg.auth", id="prefix-on-index-side"
            ),
            pytest.param(
                "src/sub/pkg.py",
                [("pkg", "pkg.py"), ("sub.pkg", "src/sub/pkg.py")],
                "sub.pkg",
                id="longest-path-wins-on-multi-match",
            ),
        ],
    )
    def test_resolves_file_to_module(self, file: str, mods: list[tuple[str, str]], expected: str | None) -> None:
        """Each file resolves to its module name (or ``None`` when unknown).

        A scan-root prefix on either side still matches, and when two indexed paths both suffix-match the most specific
        (longest) one wins, so ``src/sub/pkg.py`` is never mistaken for the top-level ``pkg.py``.
        """
        assert rc.match_module(file, mods) == expected


class TestBuildMaps:
    """build_maps: central payload + files → centrality map and file→module resolution."""

    @pytest.mark.parametrize(
        ("payload", "files", "expected"),
        [
            pytest.param(
                {"central": [{"name": "pkg.auth", "rdep_count": 9, "path": "src/pkg/auth.py"}]},
                ["src/pkg/auth.py", "docs/x.md"],
                {
                    "centrality": {"pkg.auth": 9},
                    "file_module": {"src/pkg/auth.py": "pkg.auth", "docs/x.md": ""},
                },
                id="known-file-resolves-unknown-file-is-empty",
            ),
            pytest.param(
                {"central": []},
                ["a.py"],
                {"centrality": {}, "file_module": {"a.py": ""}},
                id="no-modules-resolves-every-file-to-empty",
            ),
        ],
    )
    def test_builds_centrality_and_file_resolution(self, payload: dict, files: list[str], expected: dict) -> None:
        """A known file resolves to its module and an unknown file maps to an empty string.

        With no central modules at all the centrality map is empty and every requested file still gets an entry.
        """
        assert rc.build_maps(payload, files) == expected


class TestMainCli:
    """Main: transform piped centrality JSON into command-line JSON output."""

    @pytest.mark.parametrize(
        ("payload", "argv", "expected"),
        [
            pytest.param(
                '{"central": [{"name": "pkg.auth", "rdep_count": 5, "path": "src/pkg/auth.py"}]}',
                ["--files", "src/pkg/auth.py"],
                {"centrality": {"pkg.auth": 5}, "file_module": {"src/pkg/auth.py": "pkg.auth"}},
                id="files-resolved-against-piped-data",
            ),
            pytest.param(
                '{"central": [{"name": "pkg", "rdep_count": 3, "path": "src/pkg/__init__.py"}]}',
                [],
                {"centrality": {"pkg": 3}, "file_module": {}},
                id="no-files-gives-empty-resolution-map",
            ),
        ],
    )
    def test_emits_json_for_piped_data(
        self,
        feed_stdin: Callable[[str], None],
        capsys: pytest.CaptureFixture[str],
        payload: str,
        argv: list[str],
        expected: dict,
    ) -> None:
        """Piped centrality data is resolved against ``--files`` and printed as JSON; no files → empty resolution
        map."""
        feed_stdin(payload)
        code = rc.main(argv)
        assert (code, json.loads(capsys.readouterr().out)) == (0, expected)

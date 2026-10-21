"""Input-shape tests for ``bin/resolve_centrality.py``.

``--modules-only`` output is split by an unquoted shell ``$(...)`` in callers, and the payload comes from a tool reading
a repository the user may not control. These tests pin that unusual module names never reach the word split and that a
malformed payload degrades to an empty map or a clean exit code instead of a traceback.
"""

from __future__ import annotations

import io

import pytest
import resolve_centrality as rc


class TestOrderedModules:
    """ordered_modules: only shell-word-safe module names are emitted."""

    @pytest.mark.parametrize(
        "unsafe_name",
        [
            pytest.param("my pkg", id="space"),
            pytest.param("*", id="glob-star"),
            pytest.param("pkg[0]", id="glob-class"),
            pytest.param("-x", id="leading-dash-option"),
            pytest.param("a;b", id="semicolon"),
            pytest.param("a$(b)", id="command-substitution"),
        ],
    )
    def test_unsafe_name_is_dropped(self, unsafe_name: str) -> None:
        """A module name that a shell would split, glob or read as an option is omitted.

        An unusual repository path can become a module name verbatim; emitting it would let the caller's unquoted loop
        split or expand it, so the safe choice is to skip the name.
        """
        assert rc.ordered_modules({"file_module": {"f.py": unsafe_name}}) == []

    def test_hyphenated_and_dotted_names_are_kept(self) -> None:
        """Hyphens and dots inside a name stay valid, since script modules are indexed by their file name."""
        maps = {"file_module": {"a.py": "pkg.sub-mod", "b.py": "tool"}}
        assert rc.ordered_modules(maps) == ["pkg.sub-mod", "tool"]


class TestBuildMaps:
    """build_maps: a malformed central payload yields empty or partial maps, never an exception."""

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param({"central": [{"rdep_count": 3}]}, id="entry-without-name"),
            pytest.param({"central": ["junk", 7, None]}, id="entries-not-dicts"),
            pytest.param({"central": {"name": "pkg"}}, id="central-not-a-list"),
            pytest.param({}, id="central-missing"),
        ],
    )
    def test_malformed_payload_gives_empty_centrality(self, payload: dict) -> None:
        """Entries that cannot name a module are skipped rather than raising ``KeyError``."""
        assert rc.build_maps(payload, ["src/a.py"]) == {"centrality": {}, "file_module": {"src/a.py": ""}}

    def test_malformed_rdep_count_counts_as_zero(self) -> None:
        """A non-numeric ``rdep_count`` scores 0 so the module still resolves."""
        payload = {"central": [{"name": "pkg", "path": "src/pkg.py", "rdep_count": "many"}]}
        assert rc.build_maps(payload, [])["centrality"] == {"pkg": 0}


class TestMainExitCodes:
    """Main: stdin that is not a central JSON object exits 1."""

    @pytest.mark.parametrize(
        "stdin_text",
        [
            pytest.param("[1, 2]", id="json-list"),
            pytest.param('"text"', id="json-string"),
            pytest.param("not json", id="invalid-json"),
        ],
    )
    def test_non_object_stdin_returns_one(self, monkeypatch: pytest.MonkeyPatch, stdin_text: str) -> None:
        """A payload that is not a JSON object is rejected with exit code 1."""
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin_text))
        assert rc.main(["--files", "a.py"]) == 1

    def test_oversized_stdin_returns_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Input beyond the size cap is refused instead of being parsed whole."""
        monkeypatch.setattr(rc, "_MAX_STDIN_CHARS", 8)
        monkeypatch.setattr("sys.stdin", io.StringIO('{"central": []}'))
        assert rc.main(["--files", "a.py"]) == 1

"""Incremental scans reuse per-file subprocess and fixture results without changing what a full scan produces.

Subprocess edges, fixture exports and fixture uses are extracted from each file's AST. An incremental scan keeps the
stored values for unchanged files and re-extracts only what its cross-file lookups invalidate, so every case here
compares the incremental index with a fresh full scan of the same tree, and the parse-count cases pin the saving.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from _scan_helpers import IGNORE_RUNNER_THREAD_FORK_WARNING
from codemap_py.graph import incremental_scan, scan

_SOURCES = {
    "pkg/__init__.py": "",
    "pkg/runner.py": (
        "import subprocess\nimport sys\n\n\ndef go():\n    subprocess.run([sys.executable, 'tool.py'])\n"
    ),
    "pkg/other.py": "def helper():\n    return 1\n",
    "pkg/third.py": "def third():\n    return 3\n",
    "tests/conftest.py": ("import pytest\n\n\n@pytest.fixture\ndef shared():\n    return 1\n"),
    "tests/test_a.py": "def test_a(shared):\n    assert shared == 1\n",
    "tests/test_b.py": "def test_b(shared, extra):\n    assert shared == extra\n",
}


def _write(root: Path, sources: dict[str, str]) -> None:
    for rel, text in sources.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def _by_path(index: dict) -> dict[str, dict]:
    return {m["path"]: m for m in index["modules"]}


@contextmanager
def _parsed_files(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Record the ``filename`` of every ``ast.parse`` call made inside the block."""
    seen: list[str] = []
    real_parse = ast.parse

    def counting_parse(source, filename="<unknown>", *args, **kwargs):
        seen.append(str(filename))
        return real_parse(source, filename, *args, **kwargs)

    monkeypatch.setattr(ast, "parse", counting_parse)
    yield seen
    monkeypatch.setattr(ast, "parse", real_parse)


class TestIncrementalPerFilePhases:
    """Carried per-file results never diverge from a full scan."""

    @IGNORE_RUNNER_THREAD_FORK_WARNING
    def test_edit_matches_full_scan_and_skips_untouched_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Editing one plain module re-parses only that file and still equals a full scan."""
        _write(tmp_path, _SOURCES)
        old_index = scan(tmp_path)
        (tmp_path / "pkg" / "other.py").write_text("def helper():\n    return 2\n")

        with _parsed_files(monkeypatch) as parsed:
            incremental = incremental_scan(tmp_path, old_index)

        # The conftest alias sweep in ``incremental_scan`` parses conftests on every refresh; no other file may be read.
        assert {Path(name).name for name in parsed} - {"conftest.py"} == {"other.py"}
        assert _by_path(incremental) == _by_path(scan(tmp_path))
        assert incremental["subprocess_rdep_count"] == scan(tmp_path)["subprocess_rdep_count"]
        assert incremental["fixture_rdep_count"] == scan(tmp_path)["fixture_rdep_count"]

    @IGNORE_RUNNER_THREAD_FORK_WARNING
    def test_new_script_module_resolves_subprocess_call_in_unchanged_caller(self, tmp_path: Path) -> None:
        """Adding the module a caller launches updates the unchanged caller's edge, as a full scan does."""
        _write(tmp_path, _SOURCES)
        old_index = scan(tmp_path)
        assert _by_path(old_index)["pkg/runner.py"]["subprocess_calls"] == []
        (tmp_path / "pkg" / "tool.py").write_text("print('tool')\n")

        incremental = incremental_scan(tmp_path, old_index)
        full = scan(tmp_path)

        assert (
            _by_path(incremental)["pkg/runner.py"]["subprocess_calls"]
            == _by_path(full)["pkg/runner.py"]["subprocess_calls"]
        )
        assert _by_path(incremental)["pkg/runner.py"]["subprocess_calls"] != []
        assert _by_path(incremental) == _by_path(full)
        assert incremental["subprocess_rdep_count"] == full["subprocess_rdep_count"]

    @IGNORE_RUNNER_THREAD_FORK_WARNING
    def test_conftest_change_updates_fixture_uses_of_unchanged_tests(self, tmp_path: Path) -> None:
        """A conftest gaining a fixture changes the resolved uses of tests that did not change."""
        _write(tmp_path, _SOURCES)
        old_index = scan(tmp_path)
        (tmp_path / "tests" / "conftest.py").write_text(
            "import pytest\n\n\n@pytest.fixture\ndef shared():\n    return 1\n\n\n@pytest.fixture\ndef extra():\n"
            "    return 1\n"
        )

        incremental = incremental_scan(tmp_path, old_index)
        full = scan(tmp_path)

        assert (
            _by_path(incremental)["tests/test_b.py"]["fixture_uses"]
            == _by_path(full)["tests/test_b.py"]["fixture_uses"]
        )
        assert _by_path(incremental) == _by_path(full)
        assert incremental["fixture_rdep_count"] == full["fixture_rdep_count"]

    @IGNORE_RUNNER_THREAD_FORK_WARNING
    def test_deleted_script_module_drops_edge_in_unchanged_caller(self, tmp_path: Path) -> None:
        """Deleting a launched module removes the edge from the unchanged caller."""
        _write(tmp_path, {**_SOURCES, "pkg/tool.py": "print('tool')\n"})
        old_index = scan(tmp_path)
        assert _by_path(old_index)["pkg/runner.py"]["subprocess_calls"] != []
        (tmp_path / "pkg" / "tool.py").unlink()

        incremental = incremental_scan(tmp_path, old_index)

        assert _by_path(incremental)["pkg/runner.py"]["subprocess_calls"] == []
        assert _by_path(incremental) == _by_path(scan(tmp_path))

    @IGNORE_RUNNER_THREAD_FORK_WARNING
    def test_legacy_index_without_carried_keys_is_filled_in(self, tmp_path: Path) -> None:
        """Stored modules lacking the per-file keys are computed rather than skipped."""
        _write(tmp_path, _SOURCES)
        old_index = scan(tmp_path)
        for module in old_index["modules"]:
            for key in ("subprocess_calls", "fixture_exports", "fixture_uses"):
                module.pop(key, None)
        (tmp_path / "pkg" / "other.py").write_text("def helper():\n    return 2\n")

        incremental = incremental_scan(tmp_path, old_index)

        assert _by_path(incremental) == _by_path(scan(tmp_path))

    @IGNORE_RUNNER_THREAD_FORK_WARNING
    def test_moved_project_root_recomputes_subprocess_calls(self, tmp_path: Path) -> None:
        """A different recorded scan root invalidates carried subprocess calls, whose paths are absolute."""
        _write(tmp_path, {**_SOURCES, "pkg/tool.py": "print('tool')\n"})
        old_index = scan(tmp_path)
        old_index["scan_root"] = str(tmp_path / "elsewhere")
        (tmp_path / "pkg" / "other.py").write_text("def helper():\n    return 2\n")

        incremental = incremental_scan(tmp_path, old_index)

        assert _by_path(incremental) == _by_path(scan(tmp_path))

    @IGNORE_RUNNER_THREAD_FORK_WARNING
    def test_full_scan_parses_each_test_file_once_in_per_file_phases(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A full scan no longer re-parses a test module separately for subprocess, exports and uses.

        The three per-file phases share one parse. The bound is two, not one, because the module-extraction parse is
        also counted when it runs in this process (no fork context, or a refused process pool) and is invisible when a
        worker does it. Three separate phase parses would give three or four.
        """
        _write(tmp_path, _SOURCES)

        with _parsed_files(monkeypatch) as parsed:
            scan(tmp_path)

        per_file = [name for name in parsed if name.endswith("test_a.py")]
        assert 1 <= len(per_file) <= 2, per_file

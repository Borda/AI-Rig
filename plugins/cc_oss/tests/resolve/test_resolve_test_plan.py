"""Tests for ``bin/resolve_test_plan.py``.

Targeted selection and full-command discovery run against throwaway repositories in ``tmp_path``; codemap is simulated
through ``shutil.which`` and ``subprocess.run`` so both the codemap path and every fallback are exercised.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import resolve_test_plan as rtp

_skip_no_git = pytest.mark.skipif(shutil.which("git") is None, reason="git CLI not available")


def _write(root: Path, relative: str, text: str) -> None:
    """Write one fixture file, creating parent directories."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Build a git repository with a package, a matching test, an importing test and an unrelated test."""
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "src/pkg/core.py", "def run():\n    return 1\n")
    _write(root, "src/pkg/other.py", "X = 1\n")
    _write(root, "tests/test_core.py", "def test_run():\n    assert True\n")
    _write(root, "tests/test_uses_core.py", "from pkg.core import run\n")
    _write(root, "tests/test_unrelated.py", "def test_x():\n    assert True\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    return root


class TestModuleNames:
    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            pytest.param("src/pkg/core.py", "pkg.core", id="src-layout"),
            pytest.param("pkg/core.py", "pkg.core", id="flat-layout"),
            pytest.param("pkg/__init__.py", "pkg", id="package-init"),
        ],
    )
    def test_maps_paths_to_dotted_modules(self, path: str, expected: str) -> None:
        """Paths become the dotted names codemap indexes, with layout prefixes and ``__init__`` removed."""
        assert rtp.module_name(path) == expected


@_skip_no_git
class TestPlanTargeted:
    def test_heuristics_find_named_and_importing_tests(self, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without codemap, a source change selects its namesake test and every test that imports it — nothing else."""
        monkeypatch.setattr(rtp.shutil, "which", lambda _name: None)
        plan = rtp.plan_targeted(repo, ["src/pkg/core.py"])
        assert plan["tests"] == ["tests/test_core.py", "tests/test_uses_core.py"]
        assert set(plan["sources"].values()) == {"heuristic"}

    def test_changed_test_file_selects_itself(self, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """An edited test module is always part of its own targeted run."""
        monkeypatch.setattr(rtp.shutil, "which", lambda _name: None)
        assert rtp.plan_targeted(repo, ["tests/test_unrelated.py"])["sources"] == {"tests/test_unrelated.py": "changed"}

    def test_codemap_answer_is_used_when_complete(self, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A complete codemap answer is taken as is, so impact through indirect imports is not missed."""
        monkeypatch.setattr(rtp.shutil, "which", lambda _name: "/fake/codemap-py")
        monkeypatch.setattr(rtp, "codemap_tests", lambda _module, _repo: ["tests/test_unrelated.py"])
        plan = rtp.plan_targeted(repo, ["src/pkg/core.py"])
        assert (plan["tests"], plan["codemap"]) == (["tests/test_unrelated.py"], {"used": 1, "fell_back": 0})

    def test_codemap_failure_falls_back_instead_of_empty(self, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A failed codemap query is never read as "no affected tests"; that module falls back to heuristics."""
        monkeypatch.setattr(rtp.shutil, "which", lambda _name: "/fake/codemap-py")
        monkeypatch.setattr(rtp, "codemap_tests", lambda _module, _repo: None)
        plan = rtp.plan_targeted(repo, ["src/pkg/core.py"])
        assert (plan["tests"], plan["codemap"]["fell_back"]) == (["tests/test_core.py", "tests/test_uses_core.py"], 1)

    def test_no_match_says_so(self, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """An empty selection carries a note forbidding a full-suite substitute."""
        monkeypatch.setattr(rtp.shutil, "which", lambda _name: None)
        plan = rtp.plan_targeted(repo, ["src/pkg/other.py"])
        assert (plan["tests"], "never substitute the full suite" in plan["note"]) == ([], True)


#: The nested ``index`` block real ``codemap-py query test-impact`` output carries for a fresh, complete index.
_FRESH = {"total_modules": 12, "degraded": 0, "stale": False, "query_complete": True, "has_call_graph": True}


class TestCodemapTests:
    @pytest.mark.parametrize(
        ("returncode", "stdout", "expected"),
        [
            pytest.param(0, json.dumps({"test_files": ["tests/a.py"], "index": _FRESH}), ["tests/a.py"], id="fresh"),
            pytest.param(
                0,
                json.dumps({"test_files": ["tests/a.py"], "index": {**_FRESH, "stale": True}}),
                None,
                id="stale-index",
            ),
            pytest.param(
                0,
                json.dumps({"test_files": ["tests/a.py"], "index": {**_FRESH, "stale_undetermined": True}}),
                None,
                id="stale-undetermined",
            ),
            pytest.param(
                0,
                json.dumps({"test_files": ["tests/a.py"], "index": {**_FRESH, "query_complete": False}}),
                None,
                id="incomplete",
            ),
            pytest.param(
                0,
                json.dumps({"test_files": ["tests/a.py"], "index": {**_FRESH, "degraded": 1}}),
                None,
                id="degraded-modules",
            ),
            pytest.param(0, json.dumps({"test_files": ["tests/a.py"], "stale": False}), None, id="no-index-block"),
            pytest.param(2, "", None, id="query-failed"),
            pytest.param(0, "not json", None, id="garbage"),
        ],
    )
    def test_only_a_complete_answer_counts(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, returncode: int, stdout: str, expected: list[str] | None
    ) -> None:
        """Only a fresh, complete index vouches for a test list; anything else returns ``None`` so the caller falls
        back.

        Completeness lives in the nested ``index`` block of real codemap output — a top-level flag never counts.
        """
        monkeypatch.setattr(rtp.shutil, "which", lambda _name: "/fake/codemap-py")
        monkeypatch.setattr(rtp, "_run", lambda *_a, **_k: (returncode, stdout))
        assert rtp.codemap_tests("pkg.core", tmp_path) == expected


class TestFullCommand:
    @pytest.mark.parametrize(
        ("files", "expected_command", "expected_source"),
        [
            pytest.param(
                {"AGENTS.md": "Run tests:\n\n```bash\nuv run pytest -n 1 tests/\n```\n"},
                "uv run pytest -n 1 tests/",
                "AGENTS.md:4",
                id="agents-fence",
            ),
            pytest.param(
                {"CLAUDE.md": "Use `python -m pytest -x` before pushing.\n"},
                "python -m pytest -x",
                "CLAUDE.md:1",
                id="claude-inline",
            ),
            pytest.param({"Makefile": "test:\n\tpytest\n"}, "make test", "Makefile:test", id="makefile-target"),
            pytest.param({}, "python -m pytest", "fallback (no documented command)", id="fallback"),
        ],
    )
    def test_prefers_documented_command(
        self, tmp_path: Path, files: dict[str, str], expected_command: str, expected_source: str
    ) -> None:
        """The contributor docs win over a Makefile, which wins over the bare fallback."""
        for name, text in files.items():
            _write(tmp_path, name, text)
        found = rtp.full_command(tmp_path)
        assert (found["command"], found["source"]) == (expected_command, expected_source)

    def test_runner_drops_path_arguments(self, tmp_path: Path) -> None:
        """The targeted runner keeps the repository's flags but not its suite path, so test files can be appended."""
        (tmp_path / "tests").mkdir()
        assert rtp.runner_prefix("uv run pytest -n 1 tests", tmp_path) == "uv run pytest -n 1"


@_skip_no_git
class TestMain:
    def test_targeted_prints_runnable_command(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """The CLI prints one JSON plan whose command runs only the selected tests."""
        monkeypatch.chdir(repo)
        monkeypatch.setattr(rtp.shutil, "which", lambda _name: None)
        assert rtp.main(["targeted", "--files", "src/pkg/core.py"]) == 0
        plan = json.loads(capsys.readouterr().out)
        assert plan["command"] == "python -m pytest tests/test_core.py tests/test_uses_core.py"

    def test_rejects_option_like_base(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """A base ref git would parse as an option is refused."""
        monkeypatch.chdir(repo)
        assert rtp.main(["targeted", "--base=-evil"]) == 1
        assert "starts with '-'" in json.loads(capsys.readouterr().out)["error"]

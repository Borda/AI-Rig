"""Tests for ``dev_test_targets.py`` — the targeted-test selector fix and refactor loops run instead of the full suite.

Each scenario builds a small real git repository so selection reads the same ``git`` output it reads in a user's
project; codemap is either removed from ``PATH`` or replaced by a fixed answer, never left to whatever is installed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import dev_test_targets as dtt
import pytest

_skip_no_git = pytest.mark.skipif(shutil.which("git") is None, reason="selector reads git output")


def _write(root: Path, relative: str, text: str) -> None:
    """Create one file under the fixture repository."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build a committed src-layout package with a name-matched test, an importing test and an unrelated test."""
    _write(tmp_path, "src/pkg/mod.py", "def f():\n    return 1\n")
    _write(tmp_path, "src/pkg/other.py", "def g():\n    return 2\n")
    _write(tmp_path, "tests/test_mod.py", "def test_f():\n    pass\n")
    _write(tmp_path, "tests/test_uses.py", "from pkg.mod import f\n\n\ndef test_uses():\n    assert f()\n")
    _write(tmp_path, "tests/test_other.py", "from pkg.other import g\n\n\ndef test_g():\n    assert g()\n")
    for args in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"]):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def no_codemap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make codemap-py look uninstalled so only the path heuristics select."""
    real_which = shutil.which
    monkeypatch.setattr(dtt.shutil, "which", lambda name: None if name == "codemap-py" else real_which(name))


@_skip_no_git
class TestPlanTargeted:
    def test_heuristics_select_named_and_importing_tests(self, repo: Path, no_codemap: None) -> None:
        """Without codemap, a source change selects its name-matched test and every test importing it.

        The unrelated test that imports a sibling module stays out — selecting it would drift back toward the full suite
        the loops are meant to avoid.
        """
        plan = dtt.plan_targeted(repo, ["src/pkg/mod.py"])
        assert plan.sources == {"tests/test_mod.py": "heuristic", "tests/test_uses.py": "heuristic"}

    def test_changed_test_file_selects_itself(self, repo: Path, no_codemap: None) -> None:
        """An edited test file is always run, labelled as changed rather than inferred."""
        plan = dtt.plan_targeted(repo, ["tests/test_other.py"])
        assert plan.sources == {"tests/test_other.py": "changed"}

    def test_trusted_codemap_answer_stands_alone(self, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A complete, fresh codemap answer is used as is, without heuristic additions."""
        monkeypatch.setattr(dtt, "codemap_tests", lambda module, root: dtt.CodemapAnswer(["tests/test_mod.py"], True))
        plan = dtt.plan_targeted(repo, ["src/pkg/mod.py"])
        assert (plan.sources, plan.codemap_used) == ({"tests/test_mod.py": "codemap"}, 1)

    def test_untrusted_codemap_answer_adds_heuristics(self, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A stale or incomplete codemap answer keeps its hits and gains the heuristic hits it may have missed."""
        monkeypatch.setattr(dtt, "codemap_tests", lambda module, root: dtt.CodemapAnswer(["tests/test_mod.py"], False))
        plan = dtt.plan_targeted(repo, ["src/pkg/mod.py"])
        assert plan.sources == {"tests/test_mod.py": "codemap", "tests/test_uses.py": "heuristic"}
        assert plan.codemap_partial == 1

    def test_tests_missing_on_disk_are_dropped(self, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A codemap hit naming a deleted test file never reaches the pytest command."""
        monkeypatch.setattr(dtt, "codemap_tests", lambda module, root: dtt.CodemapAnswer(["tests/test_gone.py"], True))
        assert dtt.plan_targeted(repo, ["src/pkg/mod.py"]).sources == {}


@_skip_no_git
def test_changed_files_include_untracked_work(repo: Path) -> None:
    """Uncommitted edits and new untracked files both count as changed — refactor and fix loops never commit first."""
    _write(repo, "src/pkg/mod.py", "def f():\n    return 3\n")
    _write(repo, "src/pkg/new.py", "X = 1\n")
    assert dtt.changed_files(repo, None) == ["src/pkg/mod.py", "src/pkg/new.py"]


class TestParseCodemap:
    @pytest.mark.parametrize(
        ("index", "trusted"),
        [
            pytest.param({"query_complete": True, "stale": []}, True, id="complete-fresh"),
            pytest.param({"query_complete": True, "stale": ["src/pkg/mod.py"]}, False, id="stale"),
            pytest.param({"query_complete": False}, False, id="incomplete"),
            pytest.param({"query_complete": True, "stale_undetermined": True}, False, id="stale-undetermined"),
            pytest.param({"query_complete": True, "degraded": 2}, False, id="degraded"),
            pytest.param({"query_complete": True, "degraded": 0}, True, id="zero-degraded"),
            pytest.param(None, False, id="no-index-block"),
        ],
    )
    def test_trust_reads_the_nested_index_block(self, index: dict | None, trusted: bool) -> None:
        """Trust comes from the nested ``index`` coverage block, where codemap actually reports staleness."""
        payload = {"test_files": ["tests/test_mod.py"], **({"index": index} if index is not None else {})}
        assert dtt.parse_codemap(json.dumps(payload)).trusted is trusted

    def test_not_covered_caveat_is_kept(self) -> None:
        """Codemap's ``not_covered`` caveat survives parsing so the skill can still warn about unmapped code."""
        payload = {"test_files": [], "index": {"query_complete": True, "not_covered": ["dynamic dispatch"]}}
        assert dtt.parse_codemap(json.dumps(payload)).not_covered == ["dynamic dispatch"]

    @pytest.mark.parametrize("stdout", ["", "not json", '{"error": "no index"}', "[]"])
    def test_unusable_output_is_a_failure_not_an_empty_answer(self, stdout: str) -> None:
        """Unparsable or error output returns ``None`` so the module falls back instead of selecting nothing."""
        assert dtt.parse_codemap(stdout) is None


class TestRender:
    def test_command_appends_tests_to_repo_runner(self) -> None:
        """The command reuses the repository's own pytest invocation with the selected files appended."""
        plan = dtt.TargetPlan(changed=["src/pkg/mod.py"], sources={"tests/test_mod.py": "heuristic"})
        assert dtt.render(plan, "uv run pytest")["command"] == "uv run pytest tests/test_mod.py"

    def test_empty_selection_has_no_command_and_forbids_full_suite(self) -> None:
        """Nothing selected yields no command and a note that keeps the full suite at the final gate."""
        payload = dtt.render(dtt.TargetPlan(changed=["README.md"]), "python -m pytest")
        assert payload["command"] is None
        assert "final gate" in payload["note"]


@_skip_no_git
class TestRunFlag:
    def test_run_hands_the_whole_selection_to_one_runner_call(
        self, repo: Path, no_codemap: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--run`` passes every selected test to a single ``run_pytest_short`` call and returns its exit code.

        One call per loop cycle is the point of the flag; pytest's exit code must surface unchanged so a failing
        targeted run is never read as a pass.
        """
        import run_pytest_short

        calls: list[list[str]] = []
        monkeypatch.setattr(run_pytest_short, "main", lambda argv: calls.append(argv) or 1)
        _write(repo, "src/pkg/mod.py", "def f():\n    return 3\n")
        assert dtt.main(["--pytest-cmd", "uv run pytest", "--run"]) == 1
        assert calls == [["uv run pytest", "tests/test_mod.py", "tests/test_uses.py"]]

    def test_run_with_empty_selection_runs_nothing(
        self, repo: Path, no_codemap: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """With nothing changed, ``--run`` starts no pytest and says the full suite waits for the final gate."""
        import run_pytest_short

        monkeypatch.setattr(run_pytest_short, "main", lambda argv: pytest.fail("pytest must not start"))
        assert dtt.main(["--pytest-cmd", "pytest", "--run"]) == 0
        assert "final gate" in json.loads(capsys.readouterr().out)["note"]


@_skip_no_git
class TestRecordDir:
    def test_run_records_selection_reasons_and_keeps_full_log(
        self, repo: Path, no_codemap: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--record-dir`` appends why each test was picked and the exit code, and asks the runner for a full log.

        The terminal shows only the runner's tail, so the selection reasons and the complete output must survive on disk
        for anyone inspecting the run afterwards.
        """
        import run_pytest_short

        calls: list[list[str]] = []
        monkeypatch.setattr(run_pytest_short, "main", lambda argv: calls.append(argv) or 0)
        _write(repo, "src/pkg/mod.py", "def f():\n    return 3\n")
        assert dtt.main(["--pytest-cmd", "pytest", "--run", "--record-dir", str(repo / "run")]) == 0
        entry = json.loads((repo / "run" / "test-targets.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        assert entry["sources"] == {"tests/test_mod.py": "heuristic", "tests/test_uses.py": "heuristic"}
        assert (entry["exit"], entry["codemap"]["fell_back"]) == (0, 0)
        assert calls[0][-4:] == ["--tail-n", "40", "--log", str(Path(entry["log"]))]

    def test_plan_only_call_is_recorded_without_a_log(self, repo: Path, no_codemap: None) -> None:
        """A selection that runs nothing is still recorded, with no exit code and no log."""
        assert dtt.main(["--pytest-cmd", "pytest", "--record-dir", str(repo / "run")]) == 0
        entry = json.loads((repo / "run" / "test-targets.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        assert (entry["exit"], entry["log"], entry["tests"]) == (None, None, [])


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["--pytest-cmd", "  "], id="empty-runner"),
        pytest.param(["--pytest-cmd", "pytest", "--base=--evil"], id="dash-ref"),
    ],
)
def test_main_rejects_unusable_arguments(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    """An empty runner or a dash-prefixed ref exits 1 with a JSON error instead of selecting anything."""
    assert dtt.main(argv) == 1
    assert "error" in json.loads(capsys.readouterr().out)

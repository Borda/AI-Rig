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
from dev_run_dir import write_sentinel

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
    @pytest.mark.parametrize(
        ("changed", "expected_sources"),
        [
            # the unrelated test that imports a sibling module stays out — selecting it would drift back toward the
            # full suite the loops are meant to avoid
            pytest.param(
                ["src/pkg/mod.py"],
                {"tests/test_mod.py": "heuristic", "tests/test_uses.py": "heuristic"},
                id="source-change-selects-named-and-importing-tests",
            ),
            pytest.param(
                ["tests/test_other.py"], {"tests/test_other.py": "changed"}, id="changed-test-file-selects-itself"
            ),
        ],
    )
    def test_heuristics_select_tests(
        self, repo: Path, no_codemap: None, changed: list[str], expected_sources: dict[str, str]
    ) -> None:
        """Without codemap, a source change selects its name-matched test and every test importing it.

        An edited test file is always run, labelled as changed rather than inferred.
        """
        plan = dtt.plan_targeted(repo, changed)
        assert plan.sources == expected_sources

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
    def test_query_runs_resolved_executable(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """An executable found through Windows PATHEXT is passed to the subprocess by its full name."""
        executable = str(tmp_path / "codemap-py.cmd")
        monkeypatch.setattr(dtt.shutil, "which", lambda name: executable)
        calls = []
        monkeypatch.setattr(dtt, "_run", lambda args, cwd, timeout: calls.append(args) or (0, "{}"))
        dtt.codemap_tests("pkg.mod", tmp_path)
        assert calls == [[executable, "query", "test-impact", "pkg.mod"]]

    def test_batch_query_rejects_shell_metacharacters(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A filename-derived module cannot inject commands into a Windows batch launcher."""
        monkeypatch.setattr(dtt.shutil, "which", lambda name: r"C:\Tools\codemap-py.cmd")
        calls = []
        monkeypatch.setattr(dtt, "_run", lambda args, cwd, timeout: calls.append(args) or (0, "{}"))
        assert dtt.codemap_tests("pkg.x&echo injected", tmp_path) is None
        assert calls == []

    def test_relative_launcher_is_anchored_before_changing_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A PATH entry relative to the caller remains valid when the query runs at the repo root."""
        caller = tmp_path / "subdir"
        caller.mkdir()
        monkeypatch.chdir(caller)
        monkeypatch.setattr(dtt.shutil, "which", lambda name: "bin/codemap-py")
        calls = []
        monkeypatch.setattr(dtt, "_run", lambda args, cwd, timeout: calls.append(args) or (0, "{}"))
        dtt.codemap_tests("pkg.mod", tmp_path)
        assert calls[0][0] == str(caller / "bin" / "codemap-py")

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
        pytest.param(["--pytest-cmd", "pytest; rm -rf /"], id="shell-text-runner"),
        pytest.param(["--pytest-cmd", "python -m pytest -p evil"], id="runner-with-extra-option"),
    ],
)
def test_main_rejects_unusable_arguments(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    """An empty, non-allowlisted or shell-shaped runner, or a dash-prefixed ref, exits 1 with a JSON error.

    Plan mode only prints ``command``, but a caller may run it verbatim, so the runner faces the same allowlist the
    ``--run`` path applies.
    """
    assert dtt.main(argv) == 1
    assert "error" in json.loads(capsys.readouterr().out)


def test_main_rejects_record_dir_outside_project_and_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--record-dir`` outside the working directory and ``$TMPDIR`` exits 1 and creates nothing there."""
    project, scratch, elsewhere = tmp_path / "project", tmp_path / "scratch", tmp_path / "elsewhere"
    project.mkdir()
    scratch.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setenv("TMPDIR", str(scratch))

    assert dtt.main(["--pytest-cmd", "pytest", "--record-dir", str(elsewhere)]) == 1
    assert "--record-dir" in json.loads(capsys.readouterr().out)["error"]
    assert not elsewhere.exists()


@_skip_no_git
class TestSkillSentinels:
    """``--skill`` reads the pytest command and the run directory from the sentinels the skills used to read by hand."""

    @pytest.fixture(autouse=True)
    def _isolate_sentinels(self, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
        """Keep sentinels outside the fixture repository, so they never show up as untracked changed files."""
        monkeypatch.setenv("TMPDIR", str(tmp_path_factory.mktemp("sentinels")))
        monkeypatch.setenv("CSID", "targets-test")

    def test_records_into_the_skill_dev_dir_sentinel(
        self, repo: Path, no_codemap: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The selection record lands in the directory ``dev-<skill>-dev-dir`` names, and the runner gets its pytest.

        This is the contract the six skill blocks relied on when they passed ``--pytest-cmd "$PYTEST_CMD"`` and
        ``--record-dir "${DEV_DIR:-.developments}"``: runner detection's command runs the selected tests and the run
        record sits beside the skill's checkpoint.
        """
        import run_pytest_short

        calls: list[list[str]] = []
        monkeypatch.setattr(run_pytest_short, "main", lambda argv: calls.append(argv) or 0)
        write_sentinel("dev-pytest-cmd", "uv run pytest")
        write_sentinel("dev-fix-dev-dir", str(repo / "run"))
        _write(repo, "src/pkg/mod.py", "def f():\n    return 3\n")

        assert dtt.main(["--skill", "fix", "--run"]) == 0

        assert calls[0][:3] == ["uv run pytest", "tests/test_mod.py", "tests/test_uses.py"]
        assert (repo / "run" / "test-targets.jsonl").is_file()

    def test_missing_dev_dir_sentinel_records_under_developments(
        self, repo: Path, no_codemap: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without a run-dir sentinel the record goes to ``.developments``, the old shell default."""
        import run_pytest_short

        monkeypatch.setattr(run_pytest_short, "main", lambda argv: 0)
        write_sentinel("dev-pytest-cmd", "pytest")
        _write(repo, "src/pkg/mod.py", "def f():\n    return 3\n")

        assert dtt.main(["--skill", "refactor", "--run"]) == 0

        assert (repo / ".developments" / "test-targets.jsonl").is_file()

    def test_explicit_record_dir_wins_over_the_sentinel(
        self, repo: Path, no_codemap: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A caller that passes ``--record-dir`` keeps control of where the record goes."""
        import run_pytest_short

        monkeypatch.setattr(run_pytest_short, "main", lambda argv: 0)
        write_sentinel("dev-pytest-cmd", "pytest")
        write_sentinel("dev-feature-dev-dir", str(repo / "from-sentinel"))
        _write(repo, "src/pkg/mod.py", "def f():\n    return 3\n")

        assert dtt.main(["--skill", "feature", "--run", "--record-dir", str(repo / "explicit")]) == 0

        assert (repo / "explicit" / "test-targets.jsonl").is_file()
        assert not (repo / "from-sentinel").exists()

    def test_empty_pytest_sentinel_points_at_project_detection(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An unresolved runner exits 1 naming the step that sets it, instead of running an empty command."""
        assert dtt.main(["--skill", "fix", "--run"]) == 1

        assert "! PYTEST_CMD unresolved — re-run §Project Detection" in json.loads(capsys.readouterr().out)["error"]

    def test_skill_and_pytest_cmd_are_mutually_exclusive(self) -> None:
        """Passing both sources is an argparse error (exit 2), never a silent preference."""
        with pytest.raises(SystemExit) as exit_info:
            dtt.main(["--skill", "fix", "--pytest-cmd", "pytest"])

        assert exit_info.value.code == 2

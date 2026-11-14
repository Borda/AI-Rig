"""Regression checks for changed-file test target selection and the sandbox-safe flag decision.

The helper under test is loaded by file path, as the skills invoke it. Codemap is mocked at its two adapter boundaries;
the Git change-set test keeps a real Git repository because the Git read is its contract.
"""

from __future__ import annotations

import subprocess
from collections import Counter
from pathlib import Path
from types import ModuleType
from unittest import mock

import pytest
from _modules import load_module

PLUGIN_ROOT = Path(__file__).resolve().parents[2]


def _load_helper() -> ModuleType:
    """Load the target helper by file path, as skills invoke it."""
    return load_module("codex_rig_test_targets", PLUGIN_ROOT / "shared" / "test_targets.py")


HELPER = _load_helper()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Write a small src-layout project with one named test, one importing test, and one unrelated test."""
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "pkg" / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "src" / "pkg" / "orphan.py").write_text("OTHER = 2\n", encoding="utf-8")
    (tmp_path / "tests" / "test_mod.py").write_text("def test_value():\n    assert True\n", encoding="utf-8")
    (tmp_path / "tests" / "test_usage.py").write_text("from pkg.mod import VALUE\n", encoding="utf-8")
    (tmp_path / "tests" / "test_unrelated.py").write_text("def test_other():\n    pass\n", encoding="utf-8")
    return tmp_path


def test_heuristics_pick_named_and_importing_tests_and_report_unmapped(project: Path) -> None:
    """Select the tests named after or importing a changed module and flag a source with no test.

    The unrelated test must stay out of the loop run, and the unmapped source must be reported so the agent adds a
    focused regression test instead of widening the run to the whole suite.
    """
    report = HELPER.choose(project, ["src/pkg/mod.py", "src/pkg/orphan.py"], "HEAD", False, None)

    assert report["targets"] == ["tests/test_mod.py", "tests/test_usage.py"]
    assert report["unmapped"] == ["src/pkg/orphan.py"]
    assert report["codemap"] == "disabled"
    assert report["sandbox_safe"] is True
    assert report["pytest_args"] == ["-p", "no:xdist", "--", "tests/test_mod.py", "tests/test_usage.py"]


def test_changed_tests_and_config_are_reported(project: Path) -> None:
    """Run a changed test directly and leave changed test configuration to the canonical gate.

    A changed test file is selected as-is, while a changed ``conftest.py`` is only reported so the agent runs the full
    canonical gate instead of a narrowed loop that the configuration change could invalidate.
    """
    (project / "conftest.py").write_text("\n", encoding="utf-8")

    report = HELPER.choose(project, ["tests/test_unrelated.py", "conftest.py"], "HEAD", False, None)

    assert report["targets"] == ["tests/test_unrelated.py"]
    assert report["sources"] == {"tests/test_unrelated.py": "changed-test"}
    assert report["config_changed"] == ["conftest.py"]


@mock.patch.object(Path, "read_text", autospec=True, side_effect=Path.read_text)
def test_choose_reads_each_test_once_for_multiple_changed_sources(read_text: mock.MagicMock, project: Path) -> None:
    """Reuse imported-test text across source matching and sandbox checks without selecting unrelated tests."""
    report = HELPER.choose(project, ["src/pkg/mod.py", "src/pkg/orphan.py", "src/pkg/unused.py"], "HEAD", False, None)

    assert report == {
        "targets": ["tests/test_mod.py", "tests/test_usage.py"],
        "sources": {"tests/test_mod.py": "heuristic", "tests/test_usage.py": "heuristic"},
        "unmapped": ["src/pkg/orphan.py", "src/pkg/unused.py"],
        "config_changed": [],
        "codemap": "disabled",
        "sandbox_safe": True,
        "sandbox_reason": "no parallel option or xdist fixture found",
        "pytest_args": ["-p", "no:xdist", "--", "tests/test_mod.py", "tests/test_usage.py"],
    }
    assert Counter(call.args[0].relative_to(project).as_posix() for call in read_text.call_args_list) == {
        "tests/test_mod.py": 1,
        "tests/test_usage.py": 1,
        "tests/test_unrelated.py": 1,
    }


def test_choose_refreshes_test_text_after_file_changes(project: Path) -> None:
    """Observe changed imports and fixture dependencies on each invocation, including removed dependencies."""
    initial = HELPER.choose(project, ["src/pkg/mod.py", "src/pkg/orphan.py"], "HEAD", False, None)
    (project / "tests/test_usage.py").write_text(
        "from pkg.orphan import OTHER\ndef f(worker_id): pass\n", encoding="utf-8"
    )
    changed = HELPER.choose(project, ["src/pkg/mod.py", "src/pkg/orphan.py"], "HEAD", False, None)
    (project / "tests/test_usage.py").write_text("def f(): pass\n", encoding="utf-8")
    cleared = HELPER.choose(project, ["src/pkg/mod.py", "src/pkg/orphan.py"], "HEAD", False, None)

    assert initial["unmapped"] == ["src/pkg/orphan.py"]
    assert initial["sandbox_safe"] is True
    assert changed["unmapped"] == []
    assert changed["targets"] == ["tests/test_mod.py", "tests/test_usage.py"]
    assert changed["sandbox_safe"] is False
    assert changed["sandbox_reason"] == "tests/test_usage.py uses xdist fixtures"
    assert cleared["targets"] == ["tests/test_mod.py"]
    assert cleared["unmapped"] == ["src/pkg/orphan.py"]
    assert cleared["sandbox_safe"] is True


@mock.patch.object(Path, "read_text", autospec=True, side_effect=Path.read_text)
@mock.patch.object(HELPER.codemap_adapter, "_run_json", autospec=True)
@mock.patch.object(HELPER.codemap_adapter, "_resolve_codemap_executable", autospec=True)
@mock.patch.object(HELPER.subprocess, "run", autospec=True)
def test_complete_codemap_reads_only_selected_tests(
    git: mock.MagicMock,
    resolve: mock.MagicMock,
    run_json: mock.MagicMock,
    read_text: mock.MagicMock,
    project: Path,
) -> None:
    """Leave unrelated test contents unread when a complete provider result bypasses heuristics."""
    resolve.return_value = HELPER.codemap_adapter.LauncherResolution("codemap-py", "available", "test launcher")
    run_json.return_value = (
        0,
        {"index": {"stale": False, "query_complete": True}, "test_impact": {"test_files": ["tests/test_mod.py"]}},
        None,
    )
    git.return_value = subprocess.CompletedProcess([], 0, "a" * 40, "")

    report = HELPER.choose(project, ["src/pkg/mod.py", "src/pkg/orphan.py"], "HEAD", True, None)

    assert report["targets"] == ["tests/test_mod.py"]
    assert report["sources"] == {"tests/test_mod.py": "codemap"}
    assert report["unmapped"] == []
    assert [call.args[0].relative_to(project).as_posix() for call in read_text.call_args_list] == ["tests/test_mod.py"]


@pytest.fixture
def unreadable_project(project: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Make the importing test unreadable without relying on host file permission enforcement."""
    original_read = Path.read_text

    def read(path: Path, *args: object, **kwargs: object) -> str:
        """Simulate an unreadable test without depending on host file permission enforcement."""
        if path == project / "tests/test_usage.py":
            raise PermissionError("test read denied")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    return project


def test_unreadable_import_candidate_is_ignored(unreadable_project: Path) -> None:
    """Skip unreadable import candidates while retaining named tests and unmapped sources."""
    report = HELPER.choose(unreadable_project, ["src/pkg/mod.py", "src/pkg/orphan.py"], "HEAD", False, None)

    assert report["targets"] == ["tests/test_mod.py"]
    assert report["unmapped"] == ["src/pkg/orphan.py"]


def test_unreadable_selected_test_propagates_sandbox_error(unreadable_project: Path) -> None:
    """Propagate an unreadable selected test's error when the sandbox must inspect its fixture dependencies."""
    with pytest.raises(PermissionError, match="test read denied"):
        HELPER.choose(
            unreadable_project, ["src/pkg/mod.py", "src/pkg/orphan.py", "tests/test_usage.py"], "HEAD", False, None
        )


@pytest.mark.parametrize(
    ("filename", "content", "reason"),
    [
        pytest.param(
            "pyproject.toml",
            '[tool.pytest.ini_options]\naddopts = "-n auto"\n',
            "pyproject.toml addopts requests parallelism",
            id="addopts-numprocesses",
        ),
        pytest.param(
            "pytest.ini", "[pytest]\naddopts = --dist loadfile\n", "pytest.ini addopts requests parallelism", id="dist"
        ),
        pytest.param(
            "tests/conftest.py", "def f(worker_id):\n    pass\n", "tests/conftest.py uses xdist fixtures", id="fixture"
        ),
    ],
)
def test_sandbox_flag_is_withheld_when_parallelism_is_configured(
    project: Path, filename: str, content: str, reason: str
) -> None:
    """Never add the xdist-disabling flag when configuration or fixtures depend on xdist.

    Each case plants one parallelism signal (``-n``/``--dist`` addopts or a ``worker_id`` fixture); disabling xdist
    there would change what the project's own configuration asks for, so the flag is withheld with a named reason.
    """
    (project / filename).write_text(content, encoding="utf-8")

    report = HELPER.choose(project, ["src/pkg/mod.py"], "HEAD", False, None)

    assert (report["sandbox_safe"], report["sandbox_reason"]) == (False, reason)
    assert "no:xdist" not in report["pytest_args"]


def test_environment_addopts_withhold_the_sandbox_flag(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Honor parallel options supplied through ``PYTEST_ADDOPTS``.

    Parallelism requested only through the environment has no file to inspect, so the helper must read the variable
    itself before deciding that disabling xdist is safe.
    """
    monkeypatch.setenv("PYTEST_ADDOPTS", "-n 4")

    report = HELPER.choose(project, ["src/pkg/mod.py"], "HEAD", False, None)

    assert report["sandbox_safe"] is False


def test_git_change_set_includes_untracked_files(project: Path) -> None:
    """Read tracked edits against the base and untracked new files from Git.

    A real repository with one commit gains an edited tracked file and an untracked new one; both must appear, sorted,
    because a change set that misses untracked files would skip their tests.
    """
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run(["git", "-C", str(project), "add", "-A"], check=True)
    commit = ["git", "-C", str(project), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q"]
    subprocess.run([*commit, "-m", "init"], check=True)
    (project / "src" / "pkg" / "mod.py").write_text("VALUE = 3\n", encoding="utf-8")
    (project / "src" / "pkg" / "new.py").write_text("NEW = 1\n", encoding="utf-8")

    changed = HELPER.changed_files(project, "HEAD")

    assert changed == ["src/pkg/mod.py", "src/pkg/new.py"]


def test_missing_codemap_falls_back_to_heuristics(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Treat an absent codemap-py launcher as a fallback, never as a failure.

    With no launcher on ``PATH`` and no ``CODEMAP_BIN``, the helper reports ``absent`` and still selects tests through
    the filename and import heuristics.
    """
    monkeypatch.delenv("CODEMAP_BIN", raising=False)
    monkeypatch.setenv("PATH", str(project))

    report = HELPER.choose(project, ["src/pkg/mod.py"], "HEAD", True, None)

    assert report["codemap"] == "absent"
    assert report["targets"] == ["tests/test_mod.py", "tests/test_usage.py"]


@pytest.mark.parametrize(
    ("payload", "status", "targets"),
    [
        pytest.param(
            {"stale": False, "index": {"stale": True, "query_complete": True}, "test_impact": {"test_files": []}},
            "stale-index",
            ["tests/test_mod.py", "tests/test_usage.py"],
            id="nested-stale-overrides-top-level",
        ),
        pytest.param(
            {"test_impact": {"test_files": ["tests/test_unrelated.py"]}},
            "stale-index",
            ["tests/test_mod.py", "tests/test_usage.py"],
            id="missing-index-block",
        ),
        pytest.param(
            {"index": {"stale": False, "query_complete": True}, "test_impact": {"test_files": ["tests/test_mod.py"]}},
            "available",
            ["tests/test_mod.py"],
            id="fresh-complete-index",
        ),
        pytest.param(
            {
                "index": {"stale": False, "query_complete": False},
                "test_impact": {"test_files": ["tests/test_unrelated.py"]},
            },
            "incomplete",
            ["tests/test_mod.py", "tests/test_unrelated.py", "tests/test_usage.py"],
            id="incomplete-index-unions-heuristics",
        ),
    ],
)
@mock.patch.object(HELPER.codemap_adapter, "_run_json", autospec=True)
@mock.patch.object(HELPER.codemap_adapter, "_resolve_codemap_executable", autospec=True)
@mock.patch.object(HELPER.subprocess, "run", autospec=True)
def test_codemap_completeness_is_read_from_the_nested_index_block(
    git: mock.MagicMock,
    resolve: mock.MagicMock,
    run_json: mock.MagicMock,
    project: Path,
    payload: dict[str, object],
    status: str,
    targets: list[str],
) -> None:
    """Trust codemap test impact only when the nested ``index`` block says it is fresh.

    codemap-py reports ``stale`` and ``query_complete`` under ``payload["index"]``; reading them at the top level once
    let a stale index through. A stale or missing block falls back to heuristics, and an incomplete one adds them.
    """
    resolve.return_value = HELPER.codemap_adapter.LauncherResolution("codemap-py", "available", "test launcher")
    run_json.return_value = (0, payload, None)
    git.return_value = subprocess.CompletedProcess([], 0, "a" * 40, "")

    report = HELPER.choose(project, ["src/pkg/mod.py"], "HEAD", True, None)

    assert (report["codemap"], report["targets"]) == (status, targets)

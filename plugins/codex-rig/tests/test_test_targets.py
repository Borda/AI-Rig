"""Regression checks for changed-file test target selection and the sandbox-safe flag decision."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _load_helper() -> ModuleType:
    """Load the target helper by file path, as skills invoke it."""
    spec = importlib.util.spec_from_file_location("codex_rig_test_targets", PLUGIN_ROOT / "shared" / "test_targets.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    assert report["pytest_args"] == ["tests/test_mod.py", "tests/test_usage.py", "-p", "no:xdist"]


def test_changed_tests_and_config_are_reported(project: Path) -> None:
    """Run a changed test directly and leave changed test configuration to the canonical gate."""
    (project / "conftest.py").write_text("\n", encoding="utf-8")

    report = HELPER.choose(project, ["tests/test_unrelated.py", "conftest.py"], "HEAD", False, None)

    assert report["targets"] == ["tests/test_unrelated.py"]
    assert report["sources"] == {"tests/test_unrelated.py": "changed-test"}
    assert report["config_changed"] == ["conftest.py"]


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
    """Never add the xdist-disabling flag when configuration or fixtures depend on xdist."""
    (project / filename).write_text(content, encoding="utf-8")

    report = HELPER.choose(project, ["src/pkg/mod.py"], "HEAD", False, None)

    assert (report["sandbox_safe"], report["sandbox_reason"]) == (False, reason)
    assert "no:xdist" not in report["pytest_args"]


def test_environment_addopts_withhold_the_sandbox_flag(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Honor parallel options supplied through ``PYTEST_ADDOPTS``."""
    monkeypatch.setenv("PYTEST_ADDOPTS", "-n 4")

    report = HELPER.choose(project, ["src/pkg/mod.py"], "HEAD", False, None)

    assert report["sandbox_safe"] is False


def test_git_change_set_includes_untracked_files(project: Path) -> None:
    """Read tracked edits against the base and untracked new files from Git."""
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run(["git", "-C", str(project), "add", "-A"], check=True)
    commit = ["git", "-C", str(project), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q"]
    subprocess.run([*commit, "-m", "init"], check=True)
    (project / "src" / "pkg" / "mod.py").write_text("VALUE = 3\n", encoding="utf-8")
    (project / "src" / "pkg" / "new.py").write_text("NEW = 1\n", encoding="utf-8")

    changed = HELPER.changed_files(project, "HEAD")

    assert changed == ["src/pkg/mod.py", "src/pkg/new.py"]


def test_missing_codemap_falls_back_to_heuristics(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Treat an absent codemap-py launcher as a fallback, never as a failure."""
    monkeypatch.delenv("CODEMAP_BIN", raising=False)
    monkeypatch.setenv("PATH", str(project))

    report = HELPER.choose(project, ["src/pkg/mod.py"], "HEAD", True, None)

    assert report["codemap"] == "absent"
    assert report["targets"] == ["tests/test_mod.py", "tests/test_usage.py"]


def _fake_codemap(monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]) -> None:
    """Make the helper see an available codemap-py launcher that returns ``payload`` for ``diff-impact``."""
    resolution = HELPER.codemap_adapter.LauncherResolution("codemap-py", "available", "test launcher")
    monkeypatch.setattr(HELPER.codemap_adapter, "_resolve_codemap_executable", lambda provider_root=None: resolution)
    monkeypatch.setattr(HELPER.codemap_adapter, "_run_json", lambda argv, timeout: (0, payload, None))


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
def test_codemap_completeness_is_read_from_the_nested_index_block(
    project: Path, monkeypatch: pytest.MonkeyPatch, payload: dict[str, object], status: str, targets: list[str]
) -> None:
    """Trust codemap test impact only when the nested ``index`` block says it is fresh.

    codemap-py reports ``stale`` and ``query_complete`` under ``payload["index"]``; reading them at the top level once
    let a stale index through. A stale or missing block falls back to heuristics, and an incomplete one adds them.
    """
    _fake_codemap(monkeypatch, payload)

    report = HELPER.choose(project, ["src/pkg/mod.py"], "HEAD", True, None)

    assert (report["codemap"], report["targets"]) == (status, targets)

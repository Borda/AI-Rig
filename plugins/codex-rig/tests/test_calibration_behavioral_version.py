"""Exercise calibration's behavioral fixture version gate against real HEAD files."""

from __future__ import annotations

from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


_RUNNER = Path(__file__).resolve().parents[1] / "runtime" / "calibration" / "run.py"


def _load_runner() -> object:
    """Load the shipped runner without adding a plugin path to sys.path."""
    spec = importlib.util.spec_from_file_location("codex_rig_behavioral_version_runner", _RUNNER)
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = runner
    spec.loader.exec_module(runner)
    return runner


def _committed_version_repo(tmp_path: Path, *, tracked_runner: bool = True) -> tuple[Path, Path, Path]:
    """Create a real checkout with HEAD cases at version 2 and working cases at version 1."""
    repo = tmp_path / "repo"
    cases = repo / "plugins" / "codex-rig" / "runtime" / "calibration" / "behavioral-cases.json"
    run_py = cases.with_name("run.py")
    cases.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    cases.write_text('{"schema_version": 2, "cases": []}', encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", str(cases.relative_to(repo))], check=True)
    if tracked_runner:
        run_py.write_text("# tracked calibration runner\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", str(run_py.relative_to(repo))], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    if not tracked_runner:
        run_py.write_text("# untracked calibration runner\n", encoding="utf-8")
    cases.write_text('{"schema_version": 1, "cases": []}', encoding="utf-8")
    return repo, cases, run_py


@pytest.mark.parametrize(
    ("head_version", "current_version", "accepted"),
    [
        pytest.param(None, 1, True, id="new-family-starts-at-one"),
        pytest.param(None, 3, False, id="new-family-cannot-start-at-three"),
        pytest.param(1, 1, True, id="unchanged-from-head"),
        pytest.param(1, 2, True, id="one-step-from-head"),
        pytest.param(1, 3, False, id="cannot-skip-head-version"),
        pytest.param(2, 1, False, id="cannot-downgrade"),
    ],
)
def test_shipped_behavioral_cases_version_uses_head(
    tmp_path: Path, head_version: int | None, current_version: int, accepted: bool
) -> None:
    """A tracked source plugin must check the actual shipped fixture path at HEAD."""
    runner = _load_runner()
    repo = tmp_path / "repo"
    asset_root = repo / "plugins" / "codex-rig"
    cases = asset_root / "runtime" / "calibration" / "behavioral-cases.json"
    run_py = cases.with_name("run.py")
    cases.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    run_py.write_text("# tracked calibration runner\n", encoding="utf-8")
    head_payload: dict[str, object] = {"cases": []}
    if head_version is not None:
        head_payload["schema_version"] = head_version
    cases.write_text(json.dumps(head_payload), encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "plugins/codex-rig/runtime/calibration"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    cases.write_text(json.dumps({"schema_version": current_version, "cases": []}), encoding="utf-8")
    paths = replace(runner.Paths.create("plugin", repo), asset_root=asset_root, behavioral_cases=cases, run_py=run_py)
    run = runner.CalibrationRun(paths=paths)

    runner.check_behavioral_cases_version(run)

    checks = paths.checks.read_text(encoding="utf-8") if paths.checks.exists() else ""
    assert ("behavioral-version=ok:" in checks) is accepted
    assert ("behavioral-version-policy" in run.checks_failed) is not accepted
    assert "skipped" not in checks
    if not accepted:
        assert "behavioral-version-gap:" in paths.leaks.read_text(encoding="utf-8")


def test_head_read_error_cannot_masquerade_as_new_family(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed read of an existing HEAD fixture must block even when current version is 1."""
    runner = _load_runner()
    repo = tmp_path / "repo"
    cases = repo / "plugins" / "codex-rig" / "runtime" / "calibration" / "behavioral-cases.json"
    run_py = cases.with_name("run.py")
    cases.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    run_py.write_text("# tracked calibration runner\n", encoding="utf-8")
    cases.write_text('{"schema_version": 2, "cases": []}', encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "plugins/codex-rig/runtime/calibration"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    cases.write_text('{"schema_version": 1, "cases": []}', encoding="utf-8")
    paths = replace(runner.Paths.create("plugin", repo), behavioral_cases=cases, run_py=run_py)
    run = runner.CalibrationRun(paths=paths)
    real_run = subprocess.run

    def fail_head_read(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Simulate one Git blob read failure after the real HEAD is committed."""
        if args[:2] == ["git", "show"]:
            return subprocess.CompletedProcess(args, 128, "", "simulated read failure")
        return real_run(args, **kwargs)

    monkeypatch.setattr(runner.subprocess, "run", fail_head_read)
    runner.check_behavioral_cases_version(run)

    assert run.checks_failed == ["behavioral-version-policy"]
    assert "behavioral-version=ok:" not in (paths.checks.read_text(encoding="utf-8") if paths.checks.exists() else "")
    assert "HEAD fixture read failed" in paths.leaks.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("operation", "failure"),
    [
        pytest.param("rev-parse", "exit", id="checkout-discovery-exit"),
        pytest.param("rev-parse", "oserror", id="git-executable-unavailable"),
        pytest.param("runner-lookup", "exit", id="runner-tracking-probe-exit"),
        pytest.param("runner-lookup", "oserror", id="runner-tracking-probe-oserror"),
        pytest.param("case-lookup", "exit", id="case-tracking-probe-exit"),
        pytest.param("case-lookup", "oserror", id="case-tracking-probe-oserror"),
    ],
)
def test_git_probe_errors_inside_checkout_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, failure: str
) -> None:
    """Git failures in a known checkout cannot turn a version downgrade into a skip."""
    runner = _load_runner()
    repo, cases, run_py = _committed_version_repo(tmp_path)
    paths = replace(runner.Paths.create("plugin", repo), behavioral_cases=cases, run_py=run_py)
    run = runner.CalibrationRun(paths=paths)
    real_run = subprocess.run

    def fail_probe(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Fail only the selected Git boundary while leaving the real checkout intact."""
        is_runner_lookup = args[:2] == ["git", "cat-file"] or (
            args[:2] == ["git", "ls-tree"] and str(run_py.relative_to(repo)) in args
        )
        is_case_lookup = args[:2] == ["git", "ls-tree"] and str(cases.relative_to(repo)) in args
        if (
            (operation == "rev-parse" and args[:2] == ["git", "rev-parse"])
            or (operation == "runner-lookup" and is_runner_lookup)
            or (operation == "case-lookup" and is_case_lookup)
        ):
            if failure == "oserror":
                raise OSError("simulated Git executable error")
            return subprocess.CompletedProcess(args, 128, "", "simulated Git probe failure")
        return real_run(args, **kwargs)

    monkeypatch.setattr(runner.subprocess, "run", fail_probe)
    runner.check_behavioral_cases_version(run)

    assert run.checks_failed == ["behavioral-version-policy"]
    assert run.behavioral_version_skip_reason is None
    assert "behavioral-version-gap:" in paths.leaks.read_text(encoding="utf-8")


def test_untracked_runner_inside_checkout_is_explicit_skip(tmp_path: Path) -> None:
    """An actually absent HEAD runner path remains distinct from a failing Git probe."""
    runner = _load_runner()
    repo, cases, run_py = _committed_version_repo(tmp_path, tracked_runner=False)
    paths = replace(runner.Paths.create("plugin", repo), behavioral_cases=cases, run_py=run_py)
    run = runner.CalibrationRun(paths=paths)

    runner.check_behavioral_cases_version(run)

    assert run.checks_failed == []
    assert run.behavioral_version_skip_reason == "untracked-runner"
    assert "behavioral-version=skipped:untracked-runner" in paths.checks.read_text(encoding="utf-8")


def test_git_worktree_file_marks_checkout_when_probe_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A linked worktree's .git file keeps Git failures from becoming cache skips."""
    runner = _load_runner()
    repo, _, _ = _committed_version_repo(tmp_path)
    worktree = tmp_path / "linked"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(worktree), "HEAD"], check=True)
    assert (worktree / ".git").is_file()
    cases = worktree / "plugins" / "codex-rig" / "runtime" / "calibration" / "behavioral-cases.json"
    cases.write_text('{"schema_version": 1, "cases": []}', encoding="utf-8")
    paths = replace(runner.Paths.create("plugin", worktree), behavioral_cases=cases, run_py=cases.with_name("run.py"))
    run = runner.CalibrationRun(paths=paths)
    real_run = subprocess.run

    def fail_checkout_lookup(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Simulate rev-parse failing after a real linked worktree is present."""
        if args[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(args, 128, "", "simulated failure")
        return real_run(args, **kwargs)

    monkeypatch.setattr(runner.subprocess, "run", fail_checkout_lookup)
    runner.check_behavioral_cases_version(run)

    assert run.checks_failed == ["behavioral-version-policy"]
    assert run.behavioral_version_skip_reason is None


def test_missing_plugin_cases_inside_checkout_fails_version_gate(tmp_path: Path) -> None:
    """A removed shipped case file in source is a defect, not an installed-cache skip."""
    runner = _load_runner()
    repo, cases, run_py = _committed_version_repo(tmp_path)
    cases.unlink()
    paths = replace(runner.Paths.create("plugin", repo), behavioral_cases=cases, run_py=run_py)
    run = runner.CalibrationRun(paths=paths)

    runner.check_behavioral_cases_version(run)

    assert run.checks_failed == ["behavioral-version-policy"]
    assert run.behavioral_version_skip_reason is None


def test_source_fixture_outside_git_checkout_is_no_git_skip(tmp_path: Path) -> None:
    """A local source fixture without repository metadata has no HEAD to compare."""
    runner = _load_runner()
    paths = runner.Paths.create("source", tmp_path)
    paths.behavioral_cases.parent.mkdir(parents=True)
    paths.behavioral_cases.write_text('{"schema_version": 1, "cases": []}', encoding="utf-8")
    run = runner.CalibrationRun(paths=paths)

    runner.check_behavioral_cases_version(run)

    assert run.checks_failed == []
    assert run.behavioral_version_skip_reason == "no-git"
    assert "behavioral-version=skipped:no-git" in paths.checks.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("current_version", "accepted"),
    [pytest.param(1, True, id="new-path-starts-at-one"), pytest.param(3, False, id="new-path-cannot-skip")],
)
def test_absent_head_case_path_starts_new_family(tmp_path: Path, current_version: int, accepted: bool) -> None:
    """An actual missing HEAD path permits only the first schema version."""
    runner = _load_runner()
    repo = tmp_path / "repo"
    cases = repo / "plugins" / "codex-rig" / "runtime" / "calibration" / "behavioral-cases.json"
    run_py = cases.with_name("run.py")
    cases.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    run_py.write_text("# tracked calibration runner\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "plugins/codex-rig/runtime/calibration/run.py"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "runner",
        ],
        check=True,
    )
    cases.write_text(json.dumps({"schema_version": current_version, "cases": []}), encoding="utf-8")
    paths = replace(runner.Paths.create("plugin", repo), behavioral_cases=cases, run_py=run_py)
    run = runner.CalibrationRun(paths=paths)

    runner.check_behavioral_cases_version(run)

    assert (run.checks_failed == []) is accepted
    if accepted:
        assert "behavioral-version=ok:HEAD=0:current=1" in paths.checks.read_text(encoding="utf-8")
    else:
        assert "behavioral-version-gap:" in paths.leaks.read_text(encoding="utf-8")


def test_missing_source_layout_fixture_is_explicit_skip(tmp_path: Path) -> None:
    """The absent legacy .codex fixture must not be reported as a completed version check."""
    runner = _load_runner()
    paths = runner.Paths.create("source", tmp_path)
    run = runner.CalibrationRun(paths=paths)

    runner.check_behavioral_cases_version(run)
    runner.write_result(run)

    assert "behavioral-version=skipped:missing-source-fixture" in paths.checks.read_text(encoding="utf-8")
    assert run.checks_failed == []
    result = json.loads(paths.result.read_text(encoding="utf-8"))
    assert "behavioral-version-policy" not in result["checks_run"]
    assert result["metadata"]["checks_skipped"] == [
        {"id": "behavioral-version-policy", "reason": "missing-source-fixture"}
    ]


def test_installed_cache_reports_immutable_fixture_skip(tmp_path: Path) -> None:
    """A packaged fixture outside tracked source must not claim a HEAD comparison."""
    runner = _load_runner()
    cache = tmp_path / "cache" / "runtime" / "calibration"
    cache.mkdir(parents=True)
    cases = cache / "behavioral-cases.json"
    cases.write_text('{"schema_version": 1, "cases": []}', encoding="utf-8")
    paths = replace(runner.Paths.create("plugin", tmp_path), behavioral_cases=cases, run_py=cache / "run.py")
    run = runner.CalibrationRun(paths=paths)

    runner.check_behavioral_cases_version(run)
    runner.write_result(run)

    assert "behavioral-version=skipped:immutable-plugin-fixture" in paths.checks.read_text(encoding="utf-8")
    result = json.loads(paths.result.read_text(encoding="utf-8"))
    assert "behavioral-version-policy" not in result["checks_run"]
    assert result["metadata"]["checks_skipped"] == [
        {"id": "behavioral-version-policy", "reason": "immutable-plugin-fixture"}
    ]

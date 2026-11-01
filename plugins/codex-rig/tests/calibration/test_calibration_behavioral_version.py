"""Exercise calibration's behavioral fixture version gate against real HEAD files.

The gate compares the working behavioral-case schema version with the version committed at HEAD, so these tests build
real throwaway Git checkouts. Only Git failures themselves are simulated, never the HEAD reading under test.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pytest
from _modules import load_module_scoped

_RUNNER = Path(__file__).resolve().parents[2] / "runtime" / "calibration" / "run.py"
_CASES_REL = "plugins/codex-rig/runtime/calibration/behavioral-cases.json"
_RUNNER_REL = "plugins/codex-rig/runtime/calibration/run.py"
_HEAD_V2: dict[str, object] = {"schema_version": 2, "cases": []}


@pytest.fixture(scope="module", name="runner")
def _runner() -> Iterator[ModuleType]:
    """Load the shipped runner once without adding a plugin path to sys.path."""
    with pytest.MonkeyPatch.context() as patch:
        yield load_module_scoped("codex_rig_behavioral_version_runner", _RUNNER, patch)


def _git(repo: Path, *args: str) -> None:
    """Run one Git command inside the throwaway checkout."""
    subprocess.run(["git", "-C", str(repo), *args], check=True)


def _committed_version_repo(
    tmp_path: Path,
    *,
    head_cases: dict[str, object] | None = _HEAD_V2,
    tracked_runner: bool = True,
    current_version: int = 1,
) -> tuple[Path, Path, Path]:
    """Create a real checkout whose HEAD holds `head_cases` and whose working cases carry `current_version`.

    `head_cases=None` leaves the cases path out of HEAD; `tracked_runner=False` leaves `run.py` untracked.
    """
    repo = tmp_path / "repo"
    cases = repo / "plugins" / "codex-rig" / "runtime" / "calibration" / "behavioral-cases.json"
    run_py = cases.with_name("run.py")
    cases.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    if head_cases is not None:
        cases.write_text(json.dumps(head_cases), encoding="utf-8")
        _git(repo, "add", _CASES_REL)
    if tracked_runner:
        run_py.write_text("# tracked calibration runner\n", encoding="utf-8")
        _git(repo, "add", _RUNNER_REL)
    _git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture")
    if not tracked_runner:
        run_py.write_text("# untracked calibration runner\n", encoding="utf-8")
    cases.write_text(json.dumps({"schema_version": current_version, "cases": []}), encoding="utf-8")
    return repo, cases, run_py


def _checkout_paths(runner: ModuleType, repo: Path, cases: Path, run_py: Path) -> object:
    """Return plugin-layout runner paths pointing at the checkout's fixture and runner files."""
    return replace(runner.Paths.create("plugin", repo), behavioral_cases=cases, run_py=run_py)


def _text(path: Path) -> str:
    """Return the file's text, or an empty string when the run never wrote it."""
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _failing_git(matches: Callable[[list[str]], bool], *, oserror: bool = False) -> Callable[..., object]:
    """Build a `subprocess.run` stand-in failing only Git calls selected by `matches`, passing the rest through."""
    real_run = subprocess.run

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Fail the selected Git boundary while leaving the real checkout intact."""
        if matches(args):
            if oserror:
                raise OSError("simulated Git executable error")
            return subprocess.CompletedProcess(args, 128, "", "simulated Git failure")
        return real_run(args, **kwargs)

    return run


def _is_rev_parse(args: list[str]) -> bool:
    """Select the checkout-discovery Git call."""
    return args[:2] == ["git", "rev-parse"]


def _is_runner_lookup(args: list[str]) -> bool:
    """Select the Git calls probing whether `run.py` is tracked at HEAD."""
    return args[:2] == ["git", "cat-file"] or (args[:2] == ["git", "ls-tree"] and _RUNNER_REL in args)


def _is_case_lookup(args: list[str]) -> bool:
    """Select the Git call probing whether the behavioral cases are tracked at HEAD."""
    return args[:2] == ["git", "ls-tree"] and _CASES_REL in args


def _is_head_read(args: list[str]) -> bool:
    """Select the Git call reading the HEAD blob of the cases fixture."""
    return args[:2] == ["git", "show"]


class TestVersionAgainstHead:
    """The working schema version may equal or follow HEAD by exactly one step, and a new family starts at one."""

    @pytest.mark.parametrize(
        ("head_cases", "current_version", "ok_line"),
        [
            pytest.param({"cases": []}, 1, "behavioral-version=ok:", id="new-family-starts-at-one"),
            pytest.param({"schema_version": 1, "cases": []}, 1, "behavioral-version=ok:", id="unchanged-from-head"),
            pytest.param({"schema_version": 1, "cases": []}, 2, "behavioral-version=ok:", id="one-step-from-head"),
            pytest.param(None, 1, "behavioral-version=ok:HEAD=0:current=1", id="new-path-starts-at-one"),
        ],
    )
    def test_accepts_first_or_next_version(
        self,
        runner: ModuleType,
        tmp_path: Path,
        head_cases: dict[str, object] | None,
        current_version: int,
        ok_line: str,
    ) -> None:
        """A tracked source plugin passes when the shipped fixture path at HEAD permits the working version.

        HEAD holds a legacy unversioned file, the same version, the previous version, or no cases path at all.
        """
        repo, cases, run_py = _committed_version_repo(tmp_path, head_cases=head_cases, current_version=current_version)
        paths = _checkout_paths(runner, repo, cases, run_py)
        run = runner.CalibrationRun(paths=paths)

        runner.check_behavioral_cases_version(run)

        checks = _text(paths.checks)
        assert ok_line in checks
        assert "behavioral-version-policy" not in run.checks_failed
        assert "skipped" not in checks

    @pytest.mark.parametrize(
        ("head_cases", "current_version"),
        [
            pytest.param({"cases": []}, 3, id="new-family-cannot-start-at-three"),
            pytest.param({"schema_version": 1, "cases": []}, 3, id="cannot-skip-head-version"),
            pytest.param({"schema_version": 2, "cases": []}, 1, id="cannot-downgrade"),
            pytest.param(None, 3, id="new-path-cannot-skip"),
        ],
    )
    def test_rejects_skipped_or_downgraded_version(
        self, runner: ModuleType, tmp_path: Path, head_cases: dict[str, object] | None, current_version: int
    ) -> None:
        """A version that skips ahead of HEAD or goes below it fails the policy gate with a version gap.

        A new family may only start at one and an existing family advances one step, so three over nothing, three over
        one, and one under two are all refused and never reported as an ok check.
        """
        repo, cases, run_py = _committed_version_repo(tmp_path, head_cases=head_cases, current_version=current_version)
        paths = _checkout_paths(runner, repo, cases, run_py)
        run = runner.CalibrationRun(paths=paths)

        runner.check_behavioral_cases_version(run)

        checks = _text(paths.checks)
        assert "behavioral-version-policy" in run.checks_failed
        assert "behavioral-version=ok:" not in checks
        assert "skipped" not in checks
        assert "behavioral-version-gap:" in _text(paths.leaks)


class TestGitFailuresFailClosed:
    """Git failures inside a known checkout must block the gate instead of becoming a skip."""

    @pytest.mark.parametrize(
        ("matches", "oserror"),
        [
            pytest.param(_is_rev_parse, False, id="checkout-discovery-exit"),
            pytest.param(_is_rev_parse, True, id="git-executable-unavailable"),
            pytest.param(_is_runner_lookup, False, id="runner-tracking-probe-exit"),
            pytest.param(_is_runner_lookup, True, id="runner-tracking-probe-oserror"),
            pytest.param(_is_case_lookup, False, id="case-tracking-probe-exit"),
            pytest.param(_is_case_lookup, True, id="case-tracking-probe-oserror"),
        ],
    )
    def test_probe_errors_fail_the_policy(
        self,
        runner: ModuleType,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        matches: Callable[[list[str]], bool],
        oserror: bool,
    ) -> None:
        """A failing Git probe cannot turn a version downgrade into a skip.

        HEAD holds version 2 and the working file version 1, a downgrade, so a probe that fails (nonzero exit or a
        missing executable) must surface a policy failure with no skip reason.
        """
        repo, cases, run_py = _committed_version_repo(tmp_path)
        paths = _checkout_paths(runner, repo, cases, run_py)
        run = runner.CalibrationRun(paths=paths)
        monkeypatch.setattr(runner.subprocess, "run", _failing_git(matches, oserror=oserror))

        runner.check_behavioral_cases_version(run)

        assert run.checks_failed == ["behavioral-version-policy"]
        assert run.behavioral_version_skip_reason is None
        assert "behavioral-version-gap:" in _text(paths.leaks)

    def test_head_read_error_does_not_masquerade_as_new_family(
        self, runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed read of an existing HEAD fixture blocks even though the current version is 1.

        Treating the unreadable blob as absent would let version 1 pass as a new family and hide a real downgrade.
        """
        repo, cases, run_py = _committed_version_repo(tmp_path)
        paths = _checkout_paths(runner, repo, cases, run_py)
        run = runner.CalibrationRun(paths=paths)
        monkeypatch.setattr(runner.subprocess, "run", _failing_git(_is_head_read))

        runner.check_behavioral_cases_version(run)

        assert run.checks_failed == ["behavioral-version-policy"]
        assert "behavioral-version=ok:" not in _text(paths.checks)
        assert "HEAD fixture read failed" in _text(paths.leaks)

    def test_linked_worktree_git_file_marks_checkout(
        self, runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A linked worktree's `.git` file still marks a checkout, so a failing probe fails instead of skipping.

        The worktree is real; only `git rev-parse` is made to fail, which would look like an installed cache if the gate
        recognized only a `.git` directory.
        """
        repo, _, _ = _committed_version_repo(tmp_path)
        worktree = tmp_path / "linked"
        _git(repo, "worktree", "add", "--detach", str(worktree), "HEAD")
        cases = worktree / "plugins" / "codex-rig" / "runtime" / "calibration" / "behavioral-cases.json"
        cases.write_text('{"schema_version": 1, "cases": []}', encoding="utf-8")
        paths = _checkout_paths(runner, worktree, cases, cases.with_name("run.py"))
        run = runner.CalibrationRun(paths=paths)
        monkeypatch.setattr(runner.subprocess, "run", _failing_git(_is_rev_parse))

        runner.check_behavioral_cases_version(run)

        assert (worktree / ".git").is_file()
        assert run.checks_failed == ["behavioral-version-policy"]
        assert run.behavioral_version_skip_reason is None

    def test_missing_plugin_cases_in_checkout_fail_the_gate(self, runner: ModuleType, tmp_path: Path) -> None:
        """A removed shipped case file in source is a defect, not an installed-cache skip."""
        repo, cases, run_py = _committed_version_repo(tmp_path)
        cases.unlink()
        run = runner.CalibrationRun(paths=_checkout_paths(runner, repo, cases, run_py))

        runner.check_behavioral_cases_version(run)

        assert run.checks_failed == ["behavioral-version-policy"]
        assert run.behavioral_version_skip_reason is None


def _paths_untracked_runner(runner: ModuleType, tmp_path: Path) -> object:
    """Return paths for a checkout whose HEAD does not track the calibration runner."""
    repo, cases, run_py = _committed_version_repo(tmp_path, tracked_runner=False)
    return _checkout_paths(runner, repo, cases, run_py)


def _paths_source_without_git(runner: ModuleType, tmp_path: Path) -> object:
    """Return source-layout paths for a local fixture outside any Git checkout."""
    paths = runner.Paths.create("source", tmp_path)
    paths.behavioral_cases.parent.mkdir(parents=True)
    paths.behavioral_cases.write_text('{"schema_version": 1, "cases": []}', encoding="utf-8")
    return paths


def _paths_missing_source_fixture(runner: ModuleType, tmp_path: Path) -> object:
    """Return source-layout paths where the legacy `.codex` fixture is absent."""
    return runner.Paths.create("source", tmp_path)


def _paths_installed_cache(runner: ModuleType, tmp_path: Path) -> object:
    """Return plugin-layout paths for a packaged fixture outside any tracked source checkout."""
    cache = tmp_path / "cache" / "runtime" / "calibration"
    cache.mkdir(parents=True)
    cases = cache / "behavioral-cases.json"
    cases.write_text('{"schema_version": 1, "cases": []}', encoding="utf-8")
    return replace(runner.Paths.create("plugin", tmp_path), behavioral_cases=cases, run_py=cache / "run.py")


@pytest.mark.parametrize(
    ("build_paths", "reason"),
    [
        pytest.param(_paths_untracked_runner, "untracked-runner", id="untracked-runner-in-checkout"),
        pytest.param(_paths_source_without_git, "no-git", id="source-fixture-outside-checkout"),
        pytest.param(_paths_missing_source_fixture, "missing-source-fixture", id="missing-source-layout-fixture"),
        pytest.param(_paths_installed_cache, "immutable-plugin-fixture", id="installed-cache-fixture"),
    ],
)
def test_comparison_impossible_is_an_explicit_skip(
    runner: ModuleType, tmp_path: Path, build_paths: Callable[[ModuleType, Path], object], reason: str
) -> None:
    """When there is no HEAD to compare, the gate records a named skip and never reports a completed check.

    An untracked runner, a source fixture outside Git, an absent legacy fixture and an installed cache are distinct
    from a failing Git probe: each leaves a `skipped:<reason>` check and a `checks_skipped` entry in the result.
    """
    paths = build_paths(runner, tmp_path)
    run = runner.CalibrationRun(paths=paths)

    runner.check_behavioral_cases_version(run)
    runner.write_result(run)

    result = json.loads(paths.result.read_text(encoding="utf-8"))
    assert run.checks_failed == []
    assert run.behavioral_version_skip_reason == reason
    assert f"behavioral-version=skipped:{reason}" in _text(paths.checks)
    assert "behavioral-version-policy" not in result["checks_run"]
    assert result["metadata"]["checks_skipped"] == [{"id": "behavioral-version-policy", "reason": reason}]


@pytest.fixture(name="present_evidence")
def _present_evidence(tmp_path: Path) -> Path:
    """Write an accepted live-route evidence file and return its path."""
    path = tmp_path / "present-route-evidence.json"
    path.write_text("{}", encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("current", "evidence_name", "score", "route_gaps"),
    [
        pytest.param(True, "present-route-evidence.json", 0.95, [], id="current-route-acceptance"),
        pytest.param(
            False,
            "present-route-evidence.json",
            0.9,
            ["(-0.05) accepted live-route evidence predates the current skill identity"],
            id="historical-route-evidence",
        ),
        pytest.param(
            False,
            "absent-route-evidence.json",
            0.9,
            ["(-0.05) current live-route acceptance evidence is unavailable"],
            id="missing-route-evidence",
        ),
    ],
)
@pytest.mark.usefixtures("present_evidence")
def test_calibration_result_accounts_for_score_shortfall(
    runner: ModuleType, tmp_path: Path, current: bool, evidence_name: str, score: float, route_gaps: list[str]
) -> None:
    """Each score shortfall appears as a named gap whose amounts sum to `1.00 - score`.

    Current acceptance evidence scores 0.95 with only the fixture-heavy gap; historical or missing live-route evidence
    scores 0.9 and adds one route gap. Every gap must keep its closure identity and a zero-impact remaining limit.
    """
    paths = replace(runner.Paths.create("plugin", tmp_path), accepted_route_evidence=tmp_path / evidence_name)
    run = runner.CalibrationRun(paths=paths)
    run.accepted_route_evidence_current = current

    runner.write_result(run)

    result = json.loads(paths.result.read_text(encoding="utf-8"))
    metadata = result["metadata"]
    expected_gaps = ["(-0.05) fixture-heavy calibration does not fully prove live model behavior", *route_gaps]
    amounts = [Decimal(re.match(r"^\(-([0-9]+\.[0-9]{2})\) ", gap)[1]) for gap in expected_gaps]
    limits = metadata["confidence_recovery"]["remaining_limits"]
    assert result["confidence"] == score
    assert metadata["confidence_gaps"] == expected_gaps
    assert [entry["gap"] for entry in metadata["confidence_gap_closures"]] == expected_gaps
    assert sum(amounts) == Decimal("1.00") - Decimal(str(score))
    assert len(limits) == len(expected_gaps)
    assert all(limit.startswith("(-0.00) ") for limit in limits)
    assert all(f"accounted by gap: {gap}" in limit for gap, limit in zip(expected_gaps, limits, strict=True))
    assert "not an empirically calibrated probability" in metadata["confidence_gap_closures"][0]["rationale"]

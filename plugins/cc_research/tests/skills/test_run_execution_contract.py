"""Exercise run's scope, measurement, queue, and terminal-state instructions."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_RUN = Path(__file__).resolve().parents[2] / "skills" / "run"
_BASH = shutil.which("bash")
_JQ = shutil.which("jq")


def _section(start: str, end: str) -> list[str]:
    """Read a run section using heading boundaries rather than prose wrapping."""
    lines = (_RUN / "SKILL.md").read_text(encoding="utf-8").splitlines()
    first = next(index for index, line in enumerate(lines) if line.startswith(start))
    last = next(index for index in range(first + 1, len(lines)) if lines[index].startswith(end))
    return lines[first:last]


def _bash_block(lines: list[str], marker: str) -> str:
    """Extract the Bash fence containing a distinctive executable line."""
    current: list[str] | None = None
    for line in lines:
        if line == "```bash":
            current = []
        elif line == "```" and current is not None:
            if any(marker in item for item in current):
                return "\n".join(current)
            current = None
        elif current is not None:
            current.append(line)
    raise AssertionError(f"No Bash block contains {marker!r}")


def test_bare_goal_redirects_before_config_is_built() -> None:
    """A goal cannot enter a run with an inferred guard and no enforceable scope."""
    lines = _section("### Step R1:", "### Step R2:")
    assert any("**Auto-detect**" in line and "! UNSUPPORTED" in line and "/research:sweep" in line for line in lines)
    assert not any(line.startswith("**If argument is text**") for line in lines)
    assert any("program_file" in line and "must" in line and "existing" in line for line in lines)


def test_missing_program_is_not_an_unchecked_success() -> None:
    """An old text-goal resume cannot commit evaluator edits without a scope check."""
    lines = _section("#### Phase 3", "#### Phase 4")
    assert any("checked: false" in line and "stop" in line for line in lines)
    assert not any("checked: false" in line and "proceed" in line for line in lines)
    assert all("checked: true" in line for line in lines if line.startswith("- Exit 0,") and "no-op" in line)


def test_scope_cleanup_clears_agent_commits_from_the_index() -> None:
    """A soft-reset agent commit cannot survive scope-violation cleanup in the index."""
    lines = _section("#### Phase 3", "#### Phase 4")
    violation = next(line for line in lines if line.startswith("- Exit 3"))
    assert "git restore --staged --worktree --" in violation
    assert "scope-violation" in violation


def test_guard_rework_checks_scope_before_amending() -> None:
    """A rework cannot amend evaluator edits into an otherwise valid experiment."""
    lines = _section("#### Phase 7 —", "#### Phase 7a")
    row = next(line for line in lines if line.startswith("| metric improved AND guard fail"))
    assert row.index("rework scope gate") < row.index("git commit --amend")
    assert any("check_iteration_scope.py" in line for line in lines)
    assert any("Exit 3" in line and "scope-violation" in line and "revert" in line for line in lines)
    assert any("never" in line and "Phase 0" in line and "SHA" in line for line in lines)


def test_ml_specialist_proposes_and_engineer_applies() -> None:
    """A scientist's non-paper ML proposal is applied by an implementation-capable agent."""
    lines = _section("#### Phase 2 —", "#### Phase 2a")
    assert any("`ml`" in line and "propose-only" in line and "foundry:sw-engineer" in line for line in lines)


def test_local_proposal_application_does_not_load_docker_only_mode() -> None:
    """Apply the selected proposal without replacing its analysis or asking for a new proposal."""
    lines = _section("#### Phase 2 —", "#### Phase 2a")
    assert any("For local compute" in line and "inline apply prompt" in line for line in lines)
    assert any("ideation-<i>.md" in line and "read-only" in line for line in lines)
    assert any("Apply exactly that change" in line for line in lines)
    assert any("Do not propose an alternative" in line for line in lines)
    assert any("Never run `git add`/`git commit`" in line for line in lines)
    assert any("apply-<i>.md" in line for line in lines)
    assert any('Return ONLY: {"files_modified":[...]}' in line for line in lines)
    log = _section("#### Phase 8", "#### Phase 9")
    assert any("proposer" in line and "description" in line and "confidence" in line for line in log)


def test_step_delta_reference_is_saved_before_the_keep_decision() -> None:
    """A kept metric cannot become its own reference after state updates or compaction."""
    phase_zero = _section("#### Phase 0", "#### Phase 1")
    assert any("previous_best_metric" in line and "state.json" in line for line in phase_zero)
    log = _section("#### Phase 8", "#### Phase 9")
    assert any("previous_best_metric" in line and "Phase 0" in line for line in log)


@pytest.mark.integration
@pytest.mark.skipif(not _BASH or not _JQ, reason="Timeout initialization requires Bash and jq")
@pytest.mark.parametrize(
    ("compute", "flag_compute", "override", "expected"),
    [
        pytest.param("local", None, None, "120000", id="local-default"),
        pytest.param("colab", None, None, "300000", id="colab-default"),
        pytest.param("docker", None, None, "120000", id="docker-default"),
        pytest.param("colab", None, "9", "9000", id="explicit-timeout"),
        pytest.param("local", "colab", None, "300000", id="resume-colab-override"),
        pytest.param("colab", "docker", None, "120000", id="resume-docker-override"),
    ],
)
def test_fresh_shell_uses_persisted_compute_timeout(
    tmp_path: Path, compute: str, flag_compute: str | None, override: str | None, expected: str
) -> None:
    """The resolved compute route survives a fresh shell and retains environment overrides."""
    state_dir = tmp_path / ".experiments" / "state" / "run-test"
    state_dir.mkdir(parents=True)
    state = {"config": {"compute": compute}, "flags": {"compute": flag_compute}}
    (state_dir / "state.json").write_text(json.dumps(state), encoding="utf-8", newline="\n")
    (tmp_path / "research-run-id-test-session").write_text("run-test\n", encoding="utf-8", newline="\n")
    env = {key: value for key, value in os.environ.items() if key not in {"compute", "VERIFY_TIMEOUT_SEC"}}
    env.update({"TMPDIR": tmp_path.as_posix(), "CLAUDE_CODE_SESSION_ID": "test-session"})
    env.update({"VERIFY_TIMEOUT_SEC": override} if override is not None else {})
    block = _bash_block(_section("### Step R2:", "### Step R3:"), "VERIFY_TIMEOUT_MS=$((")
    result = subprocess.run(
        [_BASH, "-c", block], cwd=tmp_path, env=env, capture_output=True, text=True, encoding="utf-8", check=False
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "research-run-verify-timeout-ms-test-session").read_text().strip() == expected


def test_docker_rejection_is_reported_before_float_parsing() -> None:
    """An unsupported shell pipeline is distinguished from a nonnumeric metric."""
    lines = _section("### Step R2:", "### Step R3:")
    metric = next(line for line in lines if line.startswith("03."))
    assert metric.index("exit 2") < metric.index("parse stdout")
    assert "! UNSUPPORTED" in metric
    notes = _section("<notes>", "</notes>")
    assert any("Docker" in line and "single command" in line and "script entry point" in line for line in notes)


def test_metric_exit_two_does_not_imply_a_wrapper_rejection() -> None:
    """A nonzero metric fails without being confused with a wrapper preparation rejection."""
    lines = _section("### Step R2:", "### Step R3:")
    metric = next(line for line in lines if line.startswith("03."))
    assert "docker_sandbox_run.py:" in metric
    assert "container command's own exit 2" in metric
    assert "usable float" in metric
    assert "failed metric" in metric
    assert "Require exit 0" in metric


def test_raw_step_delta_is_distinct_from_baseline_percentage() -> None:
    """Retro receives a signed raw step while diary percentages have a named baseline reference."""
    lines = _section("#### Phase 8", "#### Phase 9")
    assert any("`delta = metric - previous_best_metric`" in line and "raw" in line for line in lines)
    assert any('"delta_pct":' in line for line in lines)
    assert any('"baseline_delta_pct":' in line for line in lines)
    assert any("null" in line and "zero" in line and "percentage" in line for line in lines)
    diary = _section("#### Phase 7a", "#### Phase 8")
    assert any("baseline_delta_pct" in line and "vs baseline" in line for line in diary)


def test_report_percentages_use_baseline_delta() -> None:
    """Best and history displays cannot label a raw-unit delta as a percentage."""
    lines = (_RUN / "modes/report.md").read_text(encoding="utf-8").splitlines()
    displays = [line for line in lines if line.startswith(("Best:", "**Best**:", "| N"))]
    assert len(displays) == 4
    assert all("[baseline_delta_pct]%" in line for line in displays)
    assert any("direction: lower" in line and "sign reversed" in line for line in lines)


def test_docker_proposals_use_the_implementation_agent() -> None:
    """Loading Docker instructions cannot route an architect or scientist into source implementation."""
    lines = (_RUN / "modes/compute-docker.md").read_text(encoding="utf-8").splitlines()
    spawn = next(line for line in lines if line.startswith("Spawn"))
    assert "R3 specialist" in spawn
    assert "foundry:sw-engineer" in spawn
    assert "`arch`/`ml`" in spawn


def test_retro_counts_unmeasured_iterations_without_computing_their_jumps() -> None:
    """No-measurement rows remain dead-window members but cannot enter jump arithmetic."""
    lines = (_RUN.parent / "retro/SKILL.md").read_text(encoding="utf-8").splitlines()
    first = next(i for i, line in enumerate(lines) if line.startswith("### Step T3:"))
    last = next(i for i, line in enumerate(lines) if line.startswith("### Step T4:"))
    t3 = lines[first:last]
    assert any("delta: null" in line and "dead iteration" in line and "excluded from T4" in line for line in t3)
    assert any("abs(delta) < threshold" in line for line in t3)
    assert not any("metric_delta" in line for line in t3)


def test_initial_state_and_header_name_persisted_references() -> None:
    """Resume state declares the references used by baseline, step-delta and stuck reporting."""
    lines = _section("### Step R1:", "### Step R2:")
    start = lines.index("```json") + 1
    end = lines.index("```", start)
    state = json.loads("\n".join(lines[start:end]))
    assert {key: state[key] for key in ("baseline_commit", "previous_best_metric", "stop_reason")} == {
        "baseline_commit": None,
        "previous_best_metric": None,
        "stop_reason": None,
    }
    header = _section("#### Phase 0", "#### Phase 1")
    assert any("<best record's baseline_delta_pct>" in line for line in header)


def test_timeout_config_read_has_an_execution_budget() -> None:
    """The persisted route lookup must retain its own bounded Bash execution budget."""
    lines = _section("### Step R2:", "### Step R3:")
    lookup = next(line for line in lines if line.startswith("compute=$(jq"))
    assert "# timeout: 5000" in lookup


def test_rejected_target_crossing_cannot_achieve_goal() -> None:
    """A guard-failed measurement cannot stop a campaign whose retained best misses target."""
    lines = _section("#### Phase 9", "### Step R6:")
    early = next(line for line in lines if "**Early stop**" in line)
    assert "best_metric" in early
    assert "kept" in early
    assert "direction: higher" in early
    assert "direction: lower" in early


def test_wrapup_preserves_goal_achievement() -> None:
    """Report generation does not erase the terminal result set by the progress check."""
    lines = _section("### Step R6:", "### Step R7:")
    state = next(line for line in lines if line.startswith("`state.json`:"))
    assert "goal-achieved" in state
    assert "preserve" in state
    assert "otherwise" in state
    assert "completed" in state


def test_stuck_run_uses_discoverable_terminal_status_and_clears_contract() -> None:
    """A stopped campaign reaches reporting and remains visible to existing completed-run readers."""
    lines = _section("<constants>", "</constants>")
    outcome = next(line for line in lines if line.startswith("3. Single stuck outcome"))
    assert 'stop_reason = "stuck"' in outcome
    assert "R6" in outcome
    assert "contract" in outcome
    assert "completed" in outcome
    report = _section("### Step R6:", "### Step R7:")
    assert any("If R5 ended before Phase D" in line and "write the report here" in line for line in report)


def test_codex_noop_counts_toward_discard_streak() -> None:
    """An empty Codex iteration cannot reset the stuck detector."""
    lines = _section("#### Phase 9", "### Step R6:")
    assert any("**Stuck detection**" in line and "codex-no-op" in line for line in lines)


def test_paper_verification_suggestion_is_conditional() -> None:
    """A campaign with no paper receives only the applicable retrospective suggestion."""
    lines = _section("### Step R6:", "### Step R7:")
    assert any("Only" in line and "paper" in line and "/research:verify" in line for line in lines)


def test_baseline_diff_has_a_persistent_commit_source() -> None:
    """Optional delegation can locate its baseline in both new and legacy runs."""
    baseline = _section("### Step R4:", "### Step R5:")
    assert any("baseline_commit = <HEAD sha>" in line for line in baseline)
    delegation = _section("### Step R7:", "## Resume Mode")
    assert any("state.json.baseline_commit" in line and "iteration 0" in line for line in delegation)


def test_architect_only_output_becomes_the_active_queue() -> None:
    """An architect-only oracle cannot silently discard every generated hypothesis."""
    lines = (_RUN / "modes" / "hypothesis-pipeline.md").read_text(encoding="utf-8").splitlines()
    merge = next(line for line in lines if "**Collect oracle outputs**" in line)
    assert "whichever" in merge
    assert "hypotheses-arch.jsonl" in merge
    assert "architect-only" in merge
    assert "hypotheses.jsonl" in merge
    assert "missing" in merge
    assert "warn" in merge

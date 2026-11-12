"""Exercise fresh-shell metric execution and the run recovery instructions."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
from _research_shell_plugin import render_shell

_PLUGIN = Path(__file__).resolve().parents[2]
_RUN = _PLUGIN / "skills/run"
_BASH = shutil.which("bash")
_JQ = shutil.which("jq")
_shell_available = pytest.mark.skipif(not _BASH or not _JQ, reason="run shell blocks require Bash and jq")


def _lines(relative: str) -> list[str]:
    """Read a run file without making contract checks depend on prose wrapping."""
    return (_RUN / relative).read_text(encoding="utf-8").splitlines()


def _block(relative: str, language: str, marker: str) -> str:
    """Select one fenced block by a distinctive line in its body."""
    blocks: list[str] = []
    current: list[str] | None = None
    for line in _lines(relative):
        if line.strip() == f"```{language}":
            current = []
        elif line.strip() == "```" and current is not None:
            blocks.append(textwrap.dedent("\n".join(current)))
            current = None
        elif current is not None:
            current.append(line)
    return next(block for block in blocks if marker in block)


@pytest.mark.integration
@_shell_available
@pytest.mark.parametrize("metric", ["python measure.py --label 'validation score'", "", None])
def test_docker_metric_reloads_config_in_a_fresh_shell(tmp_path: Path, metric: str | None) -> None:
    """The wrapper gets the saved command as one argument; missing commands never reach it."""
    state_dir = tmp_path / ".experiments/state/recovery-run"
    state_dir.mkdir(parents=True)
    config = {} if metric is None else {"metric_cmd": metric}
    (state_dir / "state.json").write_text(json.dumps({"config": config}), encoding="utf-8", newline="\n")
    (tmp_path / "research-run-id-fresh-shell").write_text("recovery-run\n", encoding="utf-8", newline="\n")
    (tmp_path / "research-run-verify-timeout-ms-fresh-shell").write_text("120000\n", encoding="utf-8", newline="\n")
    captured = tmp_path / "wrapper-args"
    env = {key: value for key, value in os.environ.items() if key not in {"METRIC_CMD", "RUN_ID", "CSID"}}
    env.update(
        TMPDIR=tmp_path.as_posix(),
        CLAUDE_CODE_SESSION_ID="fresh-shell",
        CLAUDE_PLUGIN_ROOT=_PLUGIN.as_posix(),
        CAPTURE=captured.as_posix(),
    )
    script = 'python() { printf "%s\\n" "$@" > "$CAPTURE"; }\n' + _block(
        "modes/phase5-metric.md", "bash", "docker_sandbox_run.py"
    )

    result = subprocess.run(
        [_BASH, "-c", render_shell(script, Path(env.get("CLAUDE_PLUGIN_ROOT", str(_PLUGIN))), "research")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert result.returncode == (0 if metric else 1), (result.stdout, result.stderr)
    assert captured.exists() == bool(metric)
    assert (captured.read_text(encoding="utf-8").splitlines()[-3:] if metric else result.stdout.strip()) == (
        ["--mode", "verify", metric] if metric else "! BLOCKED — Phase 5: config.metric_cmd missing from state.json"
    )


def test_single_metric_failure_cannot_reach_guard_or_comparison() -> None:
    """A crash or missing measurement must use guarded rollback before evaluation."""
    lines = _lines("modes/phase5-metric.md")
    rule = next((line for line in lines if line.startswith("**`metric_repeats` = 1**")), "")

    assert "non-zero" in rule
    assert "Any non-zero exit fails even when stdout contains a float" in rule
    assert "finite" in rule
    assert "metric-failed" in rule
    assert "status: reverted" in rule
    assert "skip Phases 6–7" in rule
    assert "revert block below" in rule
    assert "metric: null" in rule
    assert "stray" in rule


def test_resume_recovers_unlogged_commit_before_preconditions_and_team_dispatch() -> None:
    """Resume must not adopt an unmeasured experiment as the next iteration's baseline."""
    lines = _lines("modes/resume.md")
    recovery = next((i for i, line in enumerate(lines) if "**Recover interrupted iteration**" in line), -1)
    preconditions = next(i for i, line in enumerate(lines) if "**Re-run R2**" in line)
    dispatch = next((i for i, line in enumerate(lines) if "**Continue restored mode**" in line), -1)

    assert 0 <= recovery < preconditions < dispatch
    assert any("experiment(optimize/i<N>):" in line and "no row" in line for line in lines)
    assert any("revert_iteration_commit.py" in line and "--phase 5" in line for line in lines)
    assert any("delete only" in line and ".rec" in line and "before" in line.casefold() for line in lines)
    assert any("interrupted" in line and "metric: null" in line and "status: reverted" in line for line in lines)
    assert any("team-results.jsonl" in line and "current_hypothesis" in line for line in lines)


def test_resume_hypothesis_queue_uses_saved_path_and_checkpoint_ids() -> None:
    """A hypothesis-only run must restore its read-only queue and skip completed entries."""
    lines = _lines("modes/resume.md")
    rebuild = next(line for line in lines if "rebuild `RESEARCH_QUEUE`" in line)

    assert "`--hypothesis`" in rebuild
    assert "stored path" in rebuild
    assert "checkpoint" in rebuild


def test_team_resume_reconciles_logged_iteration_before_next_iteration() -> None:
    """A crash after experiment logging cannot cause a completed hypothesis to be applied twice."""
    resume = _lines("modes/resume.md")
    reconciliation = next(i for i, line in enumerate(resume) if "reconcile `team_mode.active_iteration`" in line)
    recovery = next(i for i, line in enumerate(resume) if "**Recover interrupted iteration**" in line)
    preconditions = next(i for i, line in enumerate(resume) if "**Re-run R2**" in line)
    next_iteration = next(i for i, line in enumerate(resume) if "**Continue restored mode**" in line)
    team = _lines("modes/team.md")
    bind = next(i for i, line in enumerate(team) if "persist `team_mode.active_iteration" in line)
    spawn = next(i for i, line in enumerate(team) if "**Spawn specialist agent**" in line)
    finish = next(line for line in team if "remove `team_mode.active_iteration`" in line)

    assert recovery < reconciliation < preconditions < next_iteration
    assert "even when Phase 8 already advanced" in resume[reconciliation]
    assert "not appended again" in resume[reconciliation]
    assert "active_queue_position" in resume[reconciliation]
    assert bind < spawn
    assert "same state write" in finish
    assert "append succeeds" in finish


def test_team_prompt_preserves_both_standard_sandbox_variants() -> None:
    """Team agents must receive the same source-write and commit restrictions as standard agents."""
    standard = _block("SKILL.md", "markdown", 'sandbox_mode = "local"').splitlines()
    team = _block("modes/team.md", "markdown", 'sandbox_mode = "local"').splitlines()

    assert all(line in team for line in standard)
    assert any("Focus this iteration" in line for line in team)
    assert any("Phases 2a–2c" in line for line in _lines("modes/team.md"))


def test_team_result_statuses_cover_iteration_failures() -> None:
    """A scope, commit or sandbox failure must remain representable in team results."""
    status = next(line for line in _lines("modes/team.md") if '"status": "kept|' in line)
    values = status.split('"')[3].split("|")

    assert {"scope-violation", "commit-failed", "sandbox-failed"} <= set(values)


def test_logged_iteration_finishes_pending_flush_before_team_reconciliation() -> None:
    """A logged iteration must finish its pending records before any mode can advance."""
    lines = _lines("modes/resume.md")
    recovery = next(i for i, line in enumerate(lines) if "**Recover interrupted iteration**" in line)
    logged = next((i for i, line in enumerate(lines) if "**Logged outcome (any mode)**" in line), -1)
    cleanup = next(i for i, line in enumerate(lines) if "delete only stale" in line)
    team = next(i for i, line in enumerate(lines) if "reconcile `team_mode.active_iteration`" in line)

    assert recovery < logged < cleanup < team
    assert "experiments.jsonl" in lines[logged]
    assert "Phase 8 flush block once" in lines[logged]
    assert "never delete" in lines[logged]
    assert "never stage" in lines[logged]
    assert "set `state.json.iteration = <N>`" in lines[logged]
    assert "Never revert, re-measure or rerun `<N>`" in lines[logged]


def test_already_reverted_iteration_is_recovered_for_every_mode() -> None:
    """A timeout revert still needs a logged outcome even outside team mode."""
    lines = _lines("modes/resume.md")
    reverted = next(i for i, line in enumerate(lines) if 'HEAD subject starting with `Revert "experiment' in line)
    team = next(i for i, line in enumerate(lines) if "reconcile `team_mode.active_iteration`" in line)

    assert reverted < team
    assert "any mode" in lines[reverted]
    assert "no experiment row" in lines[reverted]
    assert "never revert it again" in lines[reverted]


def test_resume_requires_existing_program_before_recovery() -> None:
    """Legacy text goals or deleted program files must stop before recovery changes history."""
    lines = _lines("modes/resume.md")
    program = next(line for line in lines if "**Re-parse program file**" in line)
    recovery = next(i for i, line in enumerate(lines) if "**Recover interrupted iteration**" in line)

    assert "! BLOCKED — an existing program file is required" in program
    assert "null" in program
    assert "missing" in program
    assert "keep the stored `config`" not in program
    assert lines.index(program) < recovery


def test_team_preserves_propose_only_routing_and_metric_reference() -> None:
    """Team iteration setup must preserve R5 routing and the pre-keep metric reference."""
    lines = _lines("modes/team.md")
    setup = next(line for line in lines if "**Print header**" in line)
    routing = next(line for line in lines if "**Spawn specialist agent**" in line)

    assert "Phase 0 (iteration block and `previous_best_metric` save)" in setup
    assert "`arch`/`ml` propose-only routing" in routing
    assert "run/SKILL.md §Phase 2 `arch` and `ml` strategies" in routing
    assert "Colab agent-selection rules" in routing


def test_team_prompt_requires_program_and_names_policy_sibling() -> None:
    """The shared ideation prompt must keep a required program and a discoverable sibling."""
    lines = _lines("modes/team.md")
    prompt = _block("modes/team.md", "markdown", 'sandbox_mode = "local"').splitlines()

    assert any("policy-sibling:" in line and "plugins/cc_research/skills/run/SKILL.md" in line for line in lines)
    assert not any("If program_file null" in line for line in prompt)
    assert any("Scope:" in line and "<program.md path>" in line for line in lines)
    assert not any("<program.md path or text goal>" in line for line in lines)


@pytest.mark.integration
@pytest.mark.skipif(not _BASH, reason="run shell blocks require Bash")
@pytest.mark.parametrize("relative", ["modes/phase5-metric.md", "modes/resume.md"])
def test_early_state_reads_report_missing_jq(tmp_path: Path, relative: str) -> None:
    """A missing jq must be diagnosed before state reads or any recovery side effects."""
    (tmp_path / "research-run-id-no-jq").write_text("recovery-run\n", encoding="utf-8", newline="\n")
    env = dict(os.environ, PATH="", TMPDIR=tmp_path.as_posix(), CLAUDE_CODE_SESSION_ID="no-jq")
    script = _block(relative, "bash", "METRIC_CMD=" if "metric" in relative else "revert_iteration_commit.py")

    result = subprocess.run(
        [_BASH, "-c", render_shell(script, Path(env.get("CLAUDE_PLUGIN_ROOT", str(_PLUGIN))), "research")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert result.returncode == 1
    assert result.stdout.strip() == "! MISSING — jq not on PATH; install jq and retry"
    assert result.stderr == ""


@pytest.mark.integration
@_shell_available
@pytest.mark.parametrize("team", [False, True])
@pytest.mark.parametrize("already_reverted", [False, True])
def test_unlogged_recovery_discards_only_staged_records(tmp_path: Path, team: bool, already_reverted: bool) -> None:
    """Unlogged recovery removes six staged records, preserves ledgers and verifies the revert."""
    state_dir = tmp_path / ".experiments/state/recovery-run"
    state_dir.mkdir(parents=True)
    run_dir = tmp_path / ("saved team directory" if team else ".experiments/recovery-run")
    run_dir.mkdir(parents=True)
    state = {"iteration": 2, **({"team_mode": {"run_dir": run_dir.as_posix()}} if team else {})}
    (state_dir / "state.json").write_text(json.dumps(state), encoding="utf-8", newline="\n")
    (tmp_path / "research-run-id-recovery").write_text("recovery-run\n", encoding="utf-8", newline="\n")
    ledgers = [state_dir / name for name in ("experiments.jsonl", "diary.md", "metric-runs.jsonl")] + [
        run_dir / name for name in ("checkpoint.jsonl", "journal.md", "team-results.jsonl")
    ]
    unrelated = state_dir / "other.rec"
    unrelated.write_text("preserved", encoding="utf-8", newline="\n")
    subject = tmp_path / "subject"
    subject.write_text(
        'Revert "experiment(optimize/i3): interrupted"' if already_reverted else "experiment(optimize/i3): interrupted",
        encoding="utf-8",
        newline="\n",
    )
    calls = tmp_path / "revert-call"
    env = dict(
        os.environ,
        TMPDIR=tmp_path.as_posix(),
        CLAUDE_CODE_SESSION_ID="recovery",
        HEAD_FILE=subject.as_posix(),
        CALLS=calls.as_posix(),
        CLAUDE_PLUGIN_ROOT=_PLUGIN.as_posix(),
    )
    _stage_recovery_ledgers(ledgers)
    script = """git() { cat "$HEAD_FILE"; }
python() { printf '%s\\n' "$*" > "$CALLS"; printf 'Revert "experiment(optimize/i3): interrupted"' > "$HEAD_FILE"; }
""" + _block("modes/resume.md", "bash", "revert_iteration_commit.py")

    result = subprocess.run(
        [_BASH, "-c", render_shell(script, Path(env.get("CLAUDE_PLUGIN_ROOT", str(_PLUGIN))), "research")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert not any(Path(f"{ledger}.rec").exists() for ledger in ledgers)
    assert all(ledger.read_text(encoding="utf-8") == "logged\n" for ledger in ledgers)
    assert unrelated.read_text(encoding="utf-8") == "preserved"
    assert calls.exists() == (not already_reverted)
    assert subject.read_text(encoding="utf-8").startswith('Revert "experiment(optimize/i3):')


def _stage_recovery_ledgers(ledgers: list[Path]) -> None:
    """Create distinct logged and staged contents for interrupted iteration recovery."""
    for ledger in ledgers:
        ledger.write_text("logged\n", encoding="utf-8", newline="\n")
        Path(f"{ledger}.rec").write_text("uncommitted\n", encoding="utf-8", newline="\n")


@pytest.mark.integration
@_shell_available
@pytest.mark.parametrize(
    ("result_subject", "revert_status"),
    [
        pytest.param("experiment(optimize/i3): interrupted", 0, id="revert-did-not-change-head"),
        pytest.param('Revert "experiment(optimize/i30): unrelated"', 0, id="wrong-iteration-prefix"),
        pytest.param('Revert "experiment(optimize/i3): interrupted"', 5, id="revert-failed"),
    ],
)
def test_unlogged_recovery_rejects_unverified_revert(tmp_path: Path, result_subject: str, revert_status: int) -> None:
    """Recovery cannot log success when rollback fails or leaves an unrelated HEAD."""
    state_dir = tmp_path / ".experiments/state/recovery-run"
    state_dir.mkdir(parents=True)
    (state_dir / "state.json").write_text('{"iteration":2}', encoding="utf-8", newline="\n")
    (tmp_path / "research-run-id-recovery").write_text("recovery-run\n", encoding="utf-8", newline="\n")
    subject = tmp_path / "subject"
    subject.write_text("experiment(optimize/i3): interrupted", encoding="utf-8", newline="\n")
    env = dict(
        os.environ,
        TMPDIR=tmp_path.as_posix(),
        CLAUDE_CODE_SESSION_ID="recovery",
        HEAD_FILE=subject.as_posix(),
        RESULT_SUBJECT=result_subject,
        REVERT_STATUS=str(revert_status),
    )
    script = """git() { cat "$HEAD_FILE"; }
python() { printf '%s' "$RESULT_SUBJECT" > "$HEAD_FILE"; return "$REVERT_STATUS"; }
""" + _block("modes/resume.md", "bash", "revert_iteration_commit.py")

    result = subprocess.run(
        [_BASH, "-c", render_shell(script, Path(env.get("CLAUDE_PLUGIN_ROOT", str(_PLUGIN))), "research")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert result.returncode == 1, (result.stdout, result.stderr)
    assert subject.read_text(encoding="utf-8") == result_subject

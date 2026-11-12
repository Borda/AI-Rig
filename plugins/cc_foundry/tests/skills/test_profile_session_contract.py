"""Exercise profile failure delivery and session handover retention recipes."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from _hook_env import _bash_runs_posix_script

_PLUGIN = Path(__file__).resolve().parents[2]
_BASH = pytest.mark.skipif(not _bash_runs_posix_script(), reason="POSIX bash unavailable")


def _lines(skill: str) -> list[str]:
    """Read instructions without depending on paragraph wrapping."""
    return (_PLUGIN / "skills" / skill).read_text(encoding="utf-8").splitlines()


def _bash_block(skill: str, heading: str) -> str:
    """Extract the first executable block following a named step."""
    lines = _lines(skill)
    start = lines.index("```bash", lines.index(heading)) + 1
    end = lines.index("```", start)
    return "\n".join(lines[start:end])


def _run(script: str, tmp_path: Path, **values: str) -> subprocess.CompletedProcess[str]:
    """Execute the documented recipe with isolated state and the active interpreter."""
    return subprocess.run(
        ["bash", "-c", script.replace("python ", shlex.quote(sys.executable) + " ")],
        cwd=tmp_path,
        env={**os.environ, "CLAUDE_PLUGIN_ROOT": str(_PLUGIN), "TMPDIR": str(tmp_path), **values},
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )


def test_scaffold_grants_required_task_tools_and_has_one_trigger_home() -> None:
    """New skills can triage tasks without conflicting trigger placement instructions."""
    lines = _lines("manage/templates/skill-scaffold.md")
    grant = next(line for line in lines if "Default `allowed-tools`" in line)
    assert "TaskList" in grant
    assert "TaskUpdate" in grant
    assert any("TRIGGER/SKIP required" in line and "description" in line for line in lines)
    assert not any("every skill body must include" in line for line in lines)


def test_profile_task_name_and_dump_collision_contract() -> None:
    """The report task maps to a real step and repeated dumps preserve earlier output."""
    lines = _lines("profile/SKILL.md")
    tracking = next(line for line in lines if line.startswith("**Task tracking**"))
    assert '"Step 4: Print report header"' in tracking
    assert any("never overwrite" in line and "-2.md" in line for line in lines)


def test_profile_validates_only_its_public_flags_before_execution() -> None:
    """Unknown switches, including the parser placeholder, require the standard user choice."""
    lines = _lines("profile/SKILL.md")
    check = next(line for line in lines if line.startswith("**Unsupported flag check**"))
    assert "`--since`" in check
    assert "`--session-id`" in check
    assert "`--top-n`" in check
    assert "`--noop`" in check
    assert "**Abort**" in check
    assert "**Continue ignoring**" in check
    assert lines.index(check) < lines.index("## Step 2: Run analyzers — clock time, then tokens/cost")


@_BASH
@pytest.mark.parametrize("exit_code", [0, 1, 2])
def test_profile_timing_errors_stop_the_shell(tmp_path: Path, exit_code: int) -> None:
    """A failed timing analyzer cannot return a successful recipe or reach the next action."""
    script = _bash_block("profile/SKILL.md", "## Step 2: Run analyzers — clock time, then tokens/cost")
    script = script.replace('python "$PLUGIN_ROOT/bin/timing_analyzer.py"', "timing_stub")
    result = _run(
        'timing_stub() { return "$TIMING_EXIT"; }\n' + script + '\nprintf "REACHED_NEXT_STEP\\n"',
        tmp_path,
        ARGUMENTS="",
        CLAUDE_CODE_SESSION_ID="timing-test",
        TIMING_EXIT=str(exit_code),
    )
    assert result.returncode == (0 if exit_code == 0 else 1)
    assert ("REACHED_NEXT_STEP" in result.stdout) is (exit_code == 0)


@_BASH
@pytest.mark.parametrize(
    ("exit_code", "status"),
    [
        pytest.param(0, "complete", id="cost-present"),
        pytest.param(1, "complete", id="no-transcripts"),
        pytest.param(2, "partial", id="cost-crash"),
    ],
)
def test_profile_cost_result_records_failure_without_losing_clock_report(
    tmp_path: Path, exit_code: int, status: str
) -> None:
    """Best-effort cost failures stay visible in the result while the clock report survives."""
    report = tmp_path / "report.md"
    report.write_text("Clock report\n", encoding="utf-8")
    (tmp_path / "foundry-profile-state-cost-test").write_text(
        f"REPORT_DIR={tmp_path.as_posix()}\nSINCE=24h\nSESSION_ID=\nTOP_N=5\n", encoding="utf-8"
    )
    script = _bash_block("profile/SKILL.md", "## Step 2b: Run analyzer — tokens/cost (best-effort, appended)")
    script = script.replace('python "$PLUGIN_ROOT/bin/cost_analyzer.py"', "cost_stub")
    result = _run(
        'cost_stub() { printf "Cost report\\n" > "$REPORT_DIR/cost.md"; return "$COST_STATUS"; }\n' + script,
        tmp_path,
        CLAUDE_CODE_SESSION_ID="cost-test",
        COST_STATUS=str(exit_code),
    )
    assert result.returncode == 0, result.stderr
    row = json.loads((tmp_path / "result.jsonl").read_text(encoding="utf-8"))
    assert row["cost_exit"] == exit_code
    assert row["status"] == status
    assert report.read_text(encoding="utf-8") == (
        "Clock report\n\nCost report\n" if exit_code == 0 else "Clock report\n"
    )


def test_session_reads_pointer_and_grants_triage_tool() -> None:
    """A repeated dump reads its existing pointer before Write and can triage live tasks."""
    lines = _lines("session/SKILL.md")
    grant = next(line for line in lines if line.startswith("allowed-tools:"))
    assert "TaskUpdate" in grant
    pointer = next(line for line in lines if line.startswith("2. `.claude/state/session/LATEST` — slug"))
    assert "Read" in pointer
    assert "exists" in pointer
    assert "before" in pointer


def test_session_commit_window_is_candidate_evidence_only() -> None:
    """The eight-hour log cannot claim unrelated earlier commits belong to this conversation."""
    lines = _lines("session/SKILL.md")
    log = next(line for line in lines if line.startswith("git branch"))
    assert "committed since session start" not in log
    assert "candidate commits" in log
    source = next(line for line in lines if line.startswith("3. `git log"))
    assert "candidate" in source
    assert "conversation" in source


@_BASH
@pytest.mark.parametrize(
    ("created", "consumed", "keep"),
    [
        pytest.param("old", "true", False, id="expired-consumed"),
        pytest.param("old", "false", True, id="expired-unconsumed"),
        pytest.param("recent-quoted", "true", True, id="recent-double-quoted"),
        pytest.param("recent-single-quoted", "true", True, id="recent-single-quoted"),
        pytest.param("old-quoted", "true", False, id="expired-quoted"),
        pytest.param("invalid", "true", True, id="malformed-date"),
        pytest.param("", "true", True, id="missing-date"),
    ],
)
def test_session_expiry_preserves_pending_and_invalid_handovers(
    tmp_path: Path, created: str, consumed: str, keep: bool
) -> None:
    """Expiry removes only consumed old docs after normalizing and validating their timestamp."""
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    values = {
        "old": old,
        "old-quoted": f'"{old}"',
        "recent-quoted": f'"{recent}"',
        "recent-single-quoted": f"'{recent}'",
        "invalid": "0000-invalid",
        "": "",
    }
    store = tmp_path / ".claude/state/session"
    store.mkdir(parents=True)
    doc = store / "named-plan.md"
    doc.write_text(f"---\ncreated: {values[created]}\nconsumed: {consumed}\n---\nGoal\n", encoding="utf-8")
    parked = store / "PARKED.md"
    parked.write_text(f"---\ncreated: {old}\nconsumed: true\n---\nParked\n", encoding="utf-8")
    result = _run(_bash_block("session/SKILL.md", "### Substep 3a: Sweep expired handover docs (≥ 30 days)"), tmp_path)
    assert result.returncode == 0, result.stderr
    assert doc.exists() is keep
    assert parked.exists()


@_BASH
def test_session_drop_rejects_unresolved_placeholder(tmp_path: Path) -> None:
    """A copied drop recipe cannot log the literal placeholder as a real closure."""
    store = tmp_path / ".claude/state/session"
    store.mkdir(parents=True)
    result = _run(_bash_block("session/SKILL.md", "### Substep 6b: Remove the bullet and log the closure"), tmp_path)
    assert result.returncode != 0
    assert not (store / "dropped.jsonl").exists()


@_BASH
@pytest.mark.skipif(shutil.which("jq") is None, reason="jq unavailable")
def test_session_drop_creates_missing_log_parent(tmp_path: Path) -> None:
    """An explicit drop recipe can append its closure even when the store is absent."""
    script = _bash_block("session/SKILL.md", "### Substep 6b: Remove the bullet and log the closure")
    result = _run(script.replace("<matched slug>", "retry-backoff"), tmp_path)
    assert result.returncode == 0, result.stderr
    row = json.loads((tmp_path / ".claude/state/session/dropped.jsonl").read_text(encoding="utf-8"))
    assert row["item"] == "retry-backoff"
    assert row["action"] == "dropped"

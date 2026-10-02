"""Pin everything a user or tool could see from the touched foundry skills at the committed baseline.

The efficiency rollout merged bash blocks, moved task updates into the responses that do real work, and replaced agent
polling with per-agent deadlines. None of that may hide anything the baseline showed: printed status lines, artifacts
written to disk, task progress, ⏱ timeout reports, and the telemetry the hooks record. Each row is a marker that existed
at the baseline and must still be present, in the same file, after the change.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_FOUNDRY = Path(__file__).resolve().parents[2]

#: (foundry-relative file, marker that keeps a baseline-visible status line, artifact, progress or timeout report)
_VISIBLE = [
    # investigate — merged Step 2 and Step 4 blocks keep every printed line and every artifact
    pytest.param(
        "skills/investigate/SKILL.md", 'echo "INVESTIGATE_RUN=$INVESTIGATE_RUN"', id="investigate-run-dir-line"
    ),
    pytest.param("skills/investigate/SKILL.md", "bridge@borda-ai-rig: ", id="investigate-bridge-status-line"),
    pytest.param("skills/investigate/SKILL.md", "skipping bridge review", id="investigate-bridge-skip-line"),
    pytest.param("skills/investigate/SKILL.md", 'echo "CODEX_OUT=', id="investigate-codex-out-line"),
    pytest.param(
        "skills/investigate/SKILL.md", 'echo "CODEX_AVAILABLE=$CODEX_AVAILABLE"', id="investigate-codex-available-line"
    ),
    pytest.param("skills/investigate/SKILL.md", "which python && python --version", id="investigate-tool-versions"),
    pytest.param(
        "skills/investigate/SKILL.md",
        "env | grep -E 'PATH|VIRTUAL_ENV|UV_|CLAUDE|HOME|SHELL|NODE'",
        id="investigate-env",
    ),
    pytest.param("skills/investigate/SKILL.md", "git log --oneline -10", id="investigate-recent-commits"),
    pytest.param("skills/investigate/SKILL.md", "git diff HEAD~3..HEAD --stat", id="investigate-recent-diffstat"),
    pytest.param("skills/investigate/SKILL.md", "<INVESTIGATE_RUN>/symptom.txt", id="investigate-symptom-artifact"),
    pytest.param("skills/investigate/SKILL.md", "<INVESTIGATE_RUN>/signals.md", id="investigate-signals-artifact"),
    pytest.param(
        "skills/investigate/SKILL.md", "<INVESTIGATE_RUN>/hypotheses.md", id="investigate-hypotheses-artifact"
    ),
    pytest.param("skills/investigate/SKILL.md", "challenger-review.md", id="investigate-review-artifact"),
    pytest.param("skills/investigate/SKILL.md", "investigate-verdicts-", id="investigate-probe-ledger"),
    pytest.param(
        "skills/investigate/SKILL.md",
        "TaskCreate tasks for Gather, Hypothesise, Probe, Report",
        id="investigate-task-progress",
    ),
    # manage
    pytest.param(
        "skills/manage/SKILL.md",
        "Schema file: $MANAGE_SCHEMA_FILE (cached: $MANAGE_SCHEMA_CACHED)",
        id="manage-schema-line",
    ),
    pytest.param("skills/manage/SKILL.md", "## Step 10: Summary report", id="manage-summary-report"),
    pytest.param("skills/manage/SKILL.md", ".temp/manage-challenger-", id="manage-challenger-artifact"),
    pytest.param(
        "skills/manage/SKILL.md", "Print challenger's `findings` count and confidence", id="manage-challenger-result"
    ),
    # calibrate
    pytest.param("skills/calibrate/SKILL.md", "mark target `⏱` in report", id="calibrate-timeout-report"),
    pytest.param("skills/calibrate/SKILL.md", "result.jsonl", id="calibrate-result-artifact"),
    pytest.param("skills/calibrate/SKILL.md", ".notes/logs/calibrations.jsonl", id="calibrate-log"),
    pytest.param("skills/calibrate/SKILL.md", "## Fix Apply — <date>", id="calibrate-apply-summary"),
    pytest.param("skills/calibrate/SKILL.md", 'TaskCreate "Analyse and report"', id="calibrate-task-progress"),
    # audit
    pytest.param("skills/audit/SKILL.md", "surface with ⏱ in final report", id="audit-timeout-report"),
    pytest.param("skills/audit/SKILL.md", "$RUN_DIR/report.md", id="audit-report-artifact"),
    pytest.param("skills/audit/SKILL.md", "Phase 6: print report header", id="audit-task-progress"),
    pytest.param(
        "skills/audit/modes/steps-4-5-7.md", "surface with ⏱ in final report", id="audit-lowconf-timeout-report"
    ),
    pytest.param("skills/audit/modes/steps-4-5-7.md", "<RUN_DIR>/summary.jsonl", id="audit-summary-artifact"),
    # brainstorm, create, distill
    pytest.param("skills/brainstorm/SKILL.md", "incomplete review noted", id="brainstorm-incomplete-review"),
    pytest.param(
        "skills/brainstorm/SKILL.md",
        'OUTPUT_PATH=".temp/brainstorm/$TS/curator-review.md"',
        id="brainstorm-review-artifact",
    ),
    pytest.param("skills/create/SKILL.md", "**Task tracking**: TaskCreate all steps", id="create-task-progress"),
    pytest.param(
        "skills/distill/modes/executables.md", "mark that plugin `timed_out`, surface with ⏱", id="distill-scan-timeout"
    ),
    pytest.param(
        "skills/distill/modes/executables.md",
        "mark that cluster `timed_out`, surface with ⏱",
        id="distill-extract-timeout",
    ),
    pytest.param("skills/distill/modes/prune.md", "Print consolidated summary:", id="distill-prune-summary"),
    pytest.param(
        "skills/distill/modes/memory.md", "Surface curator findings as advisory block", id="distill-memory-findings"
    ),
    pytest.param("skills/distill/modes/external.md", "⚠ Challenger review unavailable", id="distill-external-fallback"),
    # shared protocol and rules
    pytest.param("skills/_shared/agent-spawn-protocol.md", '{"verdict":"timed_out"}', id="protocol-timeout-record"),
    pytest.param("skills/_shared/agent-spawn-protocol.md", "never silently omit", id="protocol-never-omit"),
    pytest.param("skills/_shared/file-handoff-protocol.md", "surface with ⏱ in report", id="handoff-timeout-report"),
    pytest.param("skills/_shared/quality-stack.md", "⏱ in final report", id="quality-stack-timeout-report"),
    pytest.param("rules/task-lifecycle.md", "surface with ⏱", id="rule-timeout-report"),
    pytest.param(
        "agents/challenger.md", "**Do not silently skip** — surface failure in report", id="challenger-codex-failure"
    ),
    # telemetry: spawn/complete pairing stays on the Agent tool events the deadline protocol still uses
    pytest.param(
        "hooks/task-log.js", "Logs Task/Agent and Skill invocations to invocations.jsonl", id="telemetry-invocations"
    ),
    pytest.param(
        "hooks/task-log.js",
        "SubagentStop: delete the per-agent file; append completion entry",
        id="telemetry-completion",
    ),
]


@pytest.mark.parametrize(("relative", "marker"), _VISIBLE)
def test_baseline_visible_output_survives(relative: str, marker: str) -> None:
    """A status line, artifact, progress entry, timeout report or telemetry event visible at the baseline is still
    there.

    Every marker existed in the committed baseline; losing one means the rollout made something the user or tooling used
    to see invisible.
    """
    assert marker in (_FOUNDRY / relative).read_text(encoding="utf-8")


def test_deadline_check_reports_more_than_the_retired_probe() -> None:
    """The deadline helper prints a per-agent status for every agent, where the retired probe printed one file count.

    ``health_sentinel.py`` plus ``find -newer … | wc -l`` showed only "N new files"; ``agent_watch.py`` must keep
    showing per-agent status, the deliverable, and the time left, so the replacement is equivalent or better.
    """
    source = (_FOUNDRY / "bin" / "agent_watch.py").read_text(encoding="utf-8")
    assert all(field in source for field in ("status", "deliverable", "seconds_left", "timed_out"))

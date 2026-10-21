"""Pin everything the user or tooling could see at HEAD in files the turn-budget pass touched — no observability
regression.

data/observability_head_markers.json holds, per touched file, every HEAD line that produces visible output: ⏱ markers,
printed progress and report lines, report headers, checkpoint and plan-file writes, task updates, run-dir artifacts.
Each marker is the line's first ~100 characters, cut at a word boundary, still present verbatim. Lines the pass rewrote
are pinned separately by their visible equivalent. Removing an output deletes its marker and fails here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parents[1]
_MARKERS = json.loads((Path(__file__).parent / "data" / "observability_head_markers.json").read_text(encoding="utf-8"))
_ROWS = [
    pytest.param(
        relative, marker, id=f"{relative.split('/')[-2] if relative.endswith('SKILL.md') else relative}-{index}"
    )
    for relative, markers in sorted(_MARKERS.items())
    for index, marker in enumerate(markers, start=1)
]


# Authorized review contract changes retain these exact current observable equivalents.
# Keep the historical marker inventory intact and fail if any replacement signal disappears.
_REVIEW_VISIBLE_EQUIVALENTS = {
    "> - Add `Reviewers:` after `Agents:` in the report header, using readable `Role (rating)` or `Role:": (
        "> - Add `Reviewers:` after `Agents:` in the report header, using readable role names only and square-bracketed scores: `Role [rating]`, for example `Software engineer [2]`.",
    ),
    "`Legend: 1 = Approve · 2 = Minor changes · 3 = Changes required · 4 = Insufficient evidence": (
        "Legend: 1 = Approve · 2 = Minor changes · 3 = Major revision · 4 = Insufficient, but a good initiative · 5 = Block.",
    ),
    'echo "⚠ foundry--cross-validation-protocol.md not found at $_DEV_SHARED — Step 4 skipped; critical': (
        'echo "⚠ foundry--cross-validation-protocol.md not found at $_DEV_SHARED — Step 4 skipped; required validation is incomplete.',
        'echo "**Reason**: _DEV_SHARED unavailable — cross-validation protocol not executed."',
    ),
    "File present → follow cross-validation protocol printed above. File absent → skip Step 4 (warning": (
        "File present → parent validates all candidates using the printed protocol before consolidation.",
        "File absent → record incomplete process coverage, owner and reinstall recovery; no unconditional approval or unvalidated actionable findings.",
    ),
    "Use as full consolidator instructions. Prepend the [run-dir preamble": (
        "Read <CONSOLIDATOR_INSTRUCTIONS_LITERAL> with the Read tool and follow it in full.",
        "Pass only the reference, literal bindings, run-dir preamble and required return envelope.",
    ),
    "**Consolidator-unavailable fallback**: `Agent` tool deferred or consolidator times out → read each": (
        "**Consolidator-unavailable fallback**: `Agent` tool deferred or consolidator times out → read the same instruction file and each agent finding file from `$RUN_DIR/` directly,",
        "apply the full validation, precision gate, density and output rules, synthesize the report, and write `$REPORT_DIR/review-report.md` with the Write tool.",
        "Missing instruction/report-template files block with restore/reinstall recovery; never synthesize from absent rules.",
    ),
    'echo "$_REVIEW_TEMPLATE"': (
        'printf \'Consolidator instructions: %s\\nReport template: %s\\n\' "$_CONSOLIDATOR_INSTRUCTIONS" "$_REVIEW_TEMPLATE"',
    ),
    # The severity checklist ships with develop (templates/review-checklist.md) instead of being read from the oss
    # plugin, so the missing-checklist warning now names a broken develop install, not an absent oss plugin.
    'echo "⚠ oss plugin checklist unavailable — review will proceed without severity anchors; install': (
        'echo "⚠ review checklist unavailable — review will proceed without severity anchors; restore/reinstall this develop version for full coverage"',
    ),
}


# The team-mode liveness probe and its 15-minute cutoff were removed as forbidden polling; the ⏱ marking survives.
_DEBUG_VISIBLE_EQUIVALENTS = {
    "Hard cutoff 15 min no-file-activity per agent; mark timed-out agents ⏱ in synthesis.": (
        "Mark timed-out agents ⏱ in synthesis.",
    ),
    # The two inline pytest repro blocks became dev_run_repro.py, which prints these lines itself
    # (tests/test_dev_run_repro.py); the skill text states each one so the model knows what the script emits.
    'echo "$GATE_EXIT" > "${TMPDIR:-/tmp}/dev-gate-exit-${CSID}"': ("writes `dev-gate-exit`",),
    'echo "! PYTEST_CMD unresolved — re-run §Project Detection (runner-detection.md); an empty command': (
        "`! PYTEST_CMD unresolved — re-run §Project Detection (runner-detection.md)`",
    ),
    'echo "Bug reproduced — tests fail. Proceed to fix."': ("Bug reproduced — tests fail. Proceed to fix.",),
    'echo "Tests pass — bug may not be reproducible via pytest; check symptom directly."': (
        "Tests pass — bug may not be reproducible via pytest; check symptom directly.",
    ),
    # fix/SKILL.md ships in the same plugin as debug, so the partial-install warning could never print; the dead
    # check is gone and the handoff line the user copies is the live signal it guarded.
    'echo "⚠ /develop:fix not found — partial install detected; diagnosis file will be written but': (
        "Hand off: `-> /develop:fix --diagnosis $DIAG_FILE`",
    ),
}


# The plan slug is built from the flag-stripped goal (CLEAN_ARGS) instead of raw $ARGUMENTS, so flags and shell
# metacharacters cannot reach the filename or the pipeline.
_PLAN_VISIBLE_EQUIVALENTS = {
    "SLUG=$(printf '%s\\n' \"$ARGUMENTS\" | tr '[:upper:]' '[:lower:]' | grep -oE '[a-z0-9]+' | grep -vE": (
        "SLUG=$(printf '%s\\n' \"$CLEAN_ARGS\" | tr '[:upper:]' '[:lower:]' | grep -oE '[a-z0-9]+' | grep -vE",
    ),
    # /develop:debug is disable-model-invocation: the plan recommends it for the user to run, it never invokes it.
    'echo "→ invoking /develop:debug (depth remaining: $NEXT_DEPTH)"': (
        'echo "→ recommending /develop:debug (depth remaining: $NEXT_DEPTH)"',
    ),
}


def _run_dir_persist_equivalents(skill: str) -> dict[str, tuple[str, ...]]:
    """Map the retired per-skill sentinel echo to the ``--persist`` call that now writes the same sentinel."""
    return {
        f'echo "$DEV_DIR" > "${{TMPDIR:-/tmp}}/dev-{skill}-dev-dir-${{CSID}}"': (
            f'bin/dev_run_dir.py" --persist {skill})',
        ),
    }


# Every skill's cold-resolve block became one ``dev_shared_resolve.py --init`` call (tests/test_dev_shared_resolve.py
# covers what it writes); the call's own comment names the sentinel the retired ``echo`` line wrote.
_SHARED_INIT_EQUIVALENTS = {
    'echo "$_DEV_SHARED" > "${TMPDIR:-/tmp}/dev-shared-${CSID}"  # cold resolve — every later block': (
        'bin/dev_shared_resolve.py" --init',
        "writes dev-shared",
    ),
}


def _batch_gate_equivalents(skill: str) -> dict[str, tuple[str, ...]]:
    """Map the retired per-skill ``dev-batch-skill`` echo to the ``dev_batch_gate.py`` call that now writes it."""
    return {
        f'echo "{skill}" > "${{TMPDIR:-/tmp}}/dev-batch-skill-${{CSID}}"  # batch-mode.md\'s own fences read this': (
            f'bin/dev_batch_gate.py" {skill}; then python "${{CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}}/bin/dev_load_shared.py" batch-mode.md; fi',
            "writes dev-batch-skill",
        ),
    }


def _keep_sentinel_equivalents(skill: str) -> dict[str, tuple[str, ...]]:
    """Map the plain contract-write call to the ``--keep-sentinel`` form that now carries the user's ``--keep`` text."""
    return {
        f'python "${{CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}}/bin/write_skill_contract.py" "develop:{skill}"': (
            f'bin/write_skill_contract.py" --keep-sentinel dev-{skill}-keep-items "develop:{skill}"',
        ),
    }


#: fix's reproduction gates printed ``GATE OK`` for any non-zero pytest exit; now only exit 1 prints it and every other
#: exit prints ``GATE ERROR``, so a usage error or a missing runner is never reported as a reproduced bug.
_FIX_GATE_EQUIVALENTS = {
    f'[ $GATE_{gate} -eq 0 ] && echo "GATE FAIL (Path {path}): test passed — bug not captured" || echo "GATE OK': (
        f'case $GATE_{gate} in 0) echo "GATE FAIL (Path {path}): test passed — bug not captured";; 1) echo "GATE OK (Path {path}): failed as expected (exit 1)"',
    )
    for gate, path in (("P1", 1), ("P2", 2))
} | {
    '[ $GATE_EXIT -eq 0 ] && echo "GATE FAIL: fixed test still passes — weak test not corrected;': (
        'case $GATE_EXIT in 0) echo "GATE FAIL: fixed test still passes — weak test not corrected; revisit";; 1) echo "GATE OK: fixed test fails as expected (exit 1)"',
    )
}

_VISIBLE_EQUIVALENTS = {
    "skills/review/SKILL.md": {
        **_REVIEW_VISIBLE_EQUIVALENTS,
        **_SHARED_INIT_EQUIVALENTS,
        **_keep_sentinel_equivalents("review"),
    },
    "skills/debug/SKILL.md": {
        **_DEBUG_VISIBLE_EQUIVALENTS,
        **_SHARED_INIT_EQUIVALENTS,
        **_keep_sentinel_equivalents("debug"),
    },
    "skills/feature/SKILL.md": {
        **_run_dir_persist_equivalents("feature"),
        **_batch_gate_equivalents("feature"),
        **_SHARED_INIT_EQUIVALENTS,
        **_keep_sentinel_equivalents("feature"),
    },
    "skills/plan/SKILL.md": {**_PLAN_VISIBLE_EQUIVALENTS, **_SHARED_INIT_EQUIVALENTS},
    # Spawn prompts now open with the teammate's delta line (agent-spawn.md), so the role sentence is no longer line 1.
    "skills/fix/modes/team-mode.md": {
        '"You are a foundry:sw-engineer teammate investigating a bug fix. Read': (
            "You are a foundry:sw-engineer teammate investigating a bug fix. Read",
        ),
    },
    # The liveness sentinel touched before each spawn is retired: the armed `team` deadline is the only liveness check.
    "skills/refactor/modes/team-mode.md": {
        "Apply to each teammate independently — create sentinel `touch": (
            "Apply to each teammate independently — arm batch `team` in each spawn response",
        ),
    },
    "skills/fix/SKILL.md": {
        **_run_dir_persist_equivalents("fix"),
        **_FIX_GATE_EQUIVALENTS,
        **_SHARED_INIT_EQUIVALENTS,
        **_keep_sentinel_equivalents("fix"),
    },
    "skills/refactor/SKILL.md": {
        **_run_dir_persist_equivalents("refactor"),
        **_batch_gate_equivalents("refactor"),
        **_SHARED_INIT_EQUIVALENTS,
        **_keep_sentinel_equivalents("refactor"),
        # The quality-stack Codex pre-pass and delegation call Skill(bridge:review/implement),
        # so refactor now grants Skill like feature and fix.
        "allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Agent, TaskList, TaskCreate, TaskUpdate,": (
            "allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Agent, Skill, TaskList, TaskCreate, TaskUpdate,",
        ),
        # Scoped like feature's dev-feature-gate-exit: debug's dev_run_repro.py writes the unscoped dev-gate-exit.
        'echo "$GATE_EXIT" > ${TMPDIR:-/tmp}/dev-gate-exit-${CSID}': (
            'echo "$GATE_EXIT" > ${TMPDIR:-/tmp}/dev-refactor-gate-exit-${CSID}',
        ),
    },
}


@pytest.mark.parametrize(("relative", "marker"), _ROWS)
def test_head_visible_output_still_present(relative: str, marker: str) -> None:
    """Preserve each historical output verbatim or its explicitly authorized current equivalent."""
    equivalents = _VISIBLE_EQUIVALENTS.get(relative, {}).get(marker, (marker,))
    text = (_PLUGIN / relative).read_text(encoding="utf-8")
    assert all(equivalent in text for equivalent in equivalents)


@pytest.mark.parametrize(
    ("relative", "marker"),
    [
        pytest.param("skills/feature/SKILL.md", "baseline proven at cycle 0", id="feature-baseline-skip-line"),
        pytest.param(
            "skills/feature/SKILL.md", "⚠ not_covered — some changed code has no mapped test", id="feature-not-covered"
        ),
        pytest.param(
            "skills/fix/SKILL.md", "⚠ not_covered — some changed code has no mapped test", id="fix-not-covered"
        ),
        pytest.param(
            "skills/refactor/SKILL.md",
            "⚠ not_covered — some changed code has no mapped test",
            id="refactor-not-covered",
        ),
        pytest.param("skills/fix/SKILL.md", "regression test is sole verification", id="fix-no-suite-note"),
        pytest.param(
            "skills/refactor/SKILL.md", "note the message above in Final Report", id="refactor-stack-skipped-note"
        ),
        pytest.param(
            "skills/review/SKILL.md",
            "Mark agents that returned empty or error with ⏱ in final report",
            id="review-timeout-marker",
        ),
        pytest.param("skills/fix/modes/team-mode.md", "surface with ⏱; never omit", id="fix-team-timeout-marker"),
    ],
)
def test_replaced_output_has_a_visible_equivalent(relative: str, marker: str) -> None:
    """Every HEAD output this pass rewrote still has a visible equivalent — same signal, new wording."""
    assert marker in (_PLUGIN / relative).read_text(encoding="utf-8")


@pytest.mark.parametrize("skill", ["refactor", "fix", "feature"])
def test_targeted_runs_are_recorded_with_reasons_and_full_log(skill: str) -> None:
    """Targeted loop runs record which tests were picked and why, and keep the full runner log, in the run dir.

    ``--skill`` makes the selector read the skill's ``dev-<skill>-dev-dir`` sentinel and record there;
    ``tests/test_dev_test_targets.py::TestSkillSentinels`` covers the record itself.
    """
    text = (_PLUGIN / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
    assert f'bin/dev_test_targets.py" --skill {skill} --run' in text
    assert "$DEV_DIR/test-targets.jsonl" in text


def test_agent_wait_check_prints_in_flight_progress() -> None:
    """Each wake-up prints a status line per in-flight agent from the helper's pending list and elapsed time."""
    rule = (_PLUGIN / "skills" / "_shared" / "agent-resolution.md").read_text(encoding="utf-8")
    assert "top-level `pending` list" in rule
    assert "`elapsed_s` since spawn" in rule


@pytest.mark.parametrize("skill", ["refactor", "fix", "debug"])
def test_step_level_task_progress_is_kept(skill: str) -> None:
    """Step-level task progress the user saw at HEAD stays visible: one task per step, never collapsed."""
    assert "One task per step, never a bookkeeping-only turn." in (_PLUGIN / "skills" / skill / "SKILL.md").read_text(
        encoding="utf-8"
    )

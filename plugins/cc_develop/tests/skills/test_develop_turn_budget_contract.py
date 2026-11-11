"""Guard the develop turn-budget contracts: no-polling agent waits, run-level tasks, targeted tests in fix loops.

Measured refactor/fix/debug runs made one tool call per turn, spent up to 16 turns on task bookkeeping alone, re-ran the
full suite up to seven times per session, and left long agent waits for the user to notice ("finished?"). These tests
pin the instructions that remove each cost, so a later edit cannot quietly bring one back.

Skill text is compared with whitespace collapsed and sections are located by heading line or marker, so reflowing a
paragraph never breaks an assertion.
"""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path

import pytest
from _develop_text import between, flat

_SKILLS = Path(__file__).resolve().parents[2] / "skills"
_AGENT_RESOLUTION = "_shared/agent-resolution.md"
_CHALLENGER_GATE = "## Challenger gate"


@cache
def _raw(relative: str) -> str:
    """Read one develop skill file."""
    return (_SKILLS / relative).read_text(encoding="utf-8")


def _read(relative: str) -> str:
    """Return one develop skill file with whitespace collapsed."""
    return flat(_raw(relative))


def _section(relative: str, start: str, end: str) -> str:
    """Return the collapsed text between two markers of one skill file."""
    return between(_read(relative), start, end)


def _after(relative: str, marker: str) -> str:
    """Return the collapsed text following the first occurrence of ``marker``."""
    text = _read(relative)
    return text[text.index(marker) + len(marker) :]


def _heading_section(relative: str, heading: str, stop: str) -> str:
    """Return the collapsed lines after the ``heading`` line up to the first line starting with ``stop``."""
    lines = _raw(relative).splitlines()
    begin = lines.index(heading) + 1
    end = next(index for index in range(begin, len(lines)) if lines[index].startswith(stop))
    return flat(" ".join(lines[begin:end]))


def _helper_run(skill: str) -> str:
    """Return the selector call a loop skill runs: sentinels supply the pytest command and the run directory."""
    return f'bin/dev_test_targets.py" --skill {skill} --run'


def _deadline_batches(rule: str) -> dict[str, str]:
    """Map each batch named in the rule's deadline table to the last cell of its row (the deadline in seconds)."""
    rows = [[cell.strip() for cell in line.strip().strip("|").split("|")] for line in rule.splitlines()]
    return {row[0].strip("`"): row[-1] for row in rows if row[0].startswith("`") and row[0].endswith("`")}


class TestAgentResolution:
    """The shared agent-resolution rule forbids polling waits and arms a deadline per spawn batch."""

    @pytest.mark.parametrize(
        "phrase",
        [
            pytest.param(
                "never call `ScheduleWakeup`, `ListAgents`, or a `Monitor` loop to wait for agents",
                id="banned-wait-tools",
            ),
            pytest.param("the notification is the only resume signal", id="notification-resumes"),
            pytest.param(
                "End the spawning turn with one line naming each agent in flight", id="spawn-turn-names-agents"
            ),
            pytest.param("agent-watch-<batch>.tsv", id="one-tsv-per-batch"),
            pytest.param(
                'python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/agent_watch.py" --state-dir "$WATCH_DIR"',
                id="plugin-own-watch-helper",
            ),
        ],
    )
    def test_states_wait_rule(self, phrase: str) -> None:
        """The shared rule names every banned wait mechanism, the only resume signal, and how deadlines are armed.

        Spawning skills load this rule instead of repeating it, so a missing phrase silently weakens every one of them.
        """
        assert phrase in _read(_AGENT_RESOLUTION)

    def test_does_not_reach_into_oss_plugin(self) -> None:
        """The rule points at develop's own ``agent_watch.py``, never at the oss plugin's copy.

        Plugins are self-contained; a path into ``plugins/cc_oss/bin`` would break on a standalone install.
        """
        assert "plugins/cc_oss/bin" not in _read(_AGENT_RESOLUTION)

    @pytest.mark.parametrize(
        "option",
        [
            pytest.param("(a) retry the challenge once", id="retry"),
            pytest.param("(b) proceed unchallenged, ⏱ recorded in the Final Report", id="proceed-unchallenged"),
            pytest.param("(c) abort", id="abort"),
        ],
    )
    def test_timed_out_challenger_asks_instead_of_passing(self, option: str) -> None:
        """A timed-out challenger is never read as a clean pass; the user decides how to continue.

        The gate must ask through ``AskUserQuestion`` and offer retry, proceed-with-⏱ and abort.
        """
        rule = _after(_AGENT_RESOLUTION, "A timed-out `foundry:challenger`")
        assert ("AskUserQuestion" in rule, option in rule) == (True, True)

    @pytest.mark.parametrize("batch", ["scope", "challenge", "tests", "team", "review", "verify", "consolidate"])
    def test_every_batch_has_a_deadline(self, batch: str) -> None:
        """Each spawn batch named at a spawn site has a row in the deadline table, ending in a number of seconds.

        A batch without a row would never be reported as stalled.
        """
        deadlines = _deadline_batches(_raw(_AGENT_RESOLUTION))
        assert deadlines.get(batch, "").isdigit()


class TestAgentWaits:
    """Skills wait for agents by notification only, and arm their own deadline batches."""

    def test_banned_tools_appear_only_as_prohibitions(self) -> None:
        """Every develop skill line naming a banned wait tool forbids it; none instructs its use.

        Scans every Markdown file under the skills tree line by line, so a new skill cannot reintroduce polling.
        """
        offenders = [
            f"{path.relative_to(_SKILLS)}:{number}"
            for path in sorted(_SKILLS.rglob("*.md"))
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
            if re.search(r"\b(ScheduleWakeup|ListAgents)\b|`Monitor`", line) and "never" not in line.lower()
        ]
        assert offenders == []

    def test_refactor_team_mode_has_no_fixed_interval_poll(self) -> None:
        """Refactor team mode resumes on notifications; the former every-5-minutes check is gone."""
        team = _read("refactor/modes/team-mode.md")
        assert ("every 5 min" in team, "At most one liveness probe" in team, "never a liveness probe" in team) == (
            False,
            False,
            True,
        )

    @pytest.mark.parametrize("skill", ["refactor", "fix", "debug"])
    def test_skill_points_at_the_shared_wait_rule(self, skill: str) -> None:
        """Each spawning skill loads the shared wait rule and says it applies to its spawns."""
        text = _read(f"{skill}/SKILL.md")
        assert ('bin/dev_load_shared.py" agent-resolution.md' in text, "§Agent waits — no polling" in text) == (
            True,
            True,
        )


class TestAgentWatchDeadlines:
    """Every spawn site arms a deadline batch so a hung agent is reported at the next wake-up."""

    @pytest.mark.parametrize(
        ("relative", "batch"),
        [
            pytest.param("refactor/SKILL.md", "scope", id="refactor-scope"),
            pytest.param("refactor/SKILL.md", "challenge", id="refactor-challenge"),
            pytest.param("refactor/SKILL.md", "tests", id="refactor-tests"),
            pytest.param("fix/SKILL.md", "scope", id="fix-scope"),
            pytest.param("fix/SKILL.md", "challenge", id="fix-challenge"),
            pytest.param("fix/SKILL.md", "tests", id="fix-tests"),
            pytest.param("debug/SKILL.md", "scope", id="debug-scope"),
            pytest.param("debug/SKILL.md", "challenge", id="debug-challenge"),
            pytest.param("debug/SKILL.md", "team", id="debug-team"),
            pytest.param("review/SKILL.md", "review", id="review"),
            pytest.param("refactor/modes/team-mode.md", "team", id="refactor-team"),
            pytest.param("fix/modes/team-mode.md", "team", id="fix-team"),
        ],
    )
    def test_spawn_site_arms_its_batch(self, relative: str, batch: str) -> None:
        """Every spawn site arms its deadline batch, so a hung agent is reported at the next wake-up."""
        assert re.search(rf"[Aa]rm batch `{batch}`", _read(relative))

    @pytest.mark.parametrize("skill", ["refactor", "fix", "debug", "review", "feature", "plan"])
    def test_each_run_gets_a_fresh_watch_dir(self, skill: str) -> None:
        """Agent Resolution starts every run with a fresh watch dir, so a prior run's batches never report as stale."""
        text = _read(f"{skill}/SKILL.md")
        assert ('bin/dev_shared_resolve.py" --init' in text, "fresh dev-agent-watch-dir per run" in text) == (
            True,
            True,
        )


@pytest.mark.parametrize(
    ("skill", "first_step"),
    [
        pytest.param("refactor", "Step 1 Scope", id="refactor"),
        pytest.param("fix", "Step 1 Understand", id="fix"),
        pytest.param("debug", "Step 1 Symptom", id="debug"),
    ],
)
def test_step_level_tasks_ride_with_real_work(skill: str, first_step: str) -> None:
    """Step-level task visibility is kept while every create and update rides with a real call.

    The measured cost was standalone bookkeeping turns, not the number of tasks; collapsing to fewer tasks would lose
    progress the user saw at HEAD, so the rule keeps one task per step and only moves where the calls ride.
    """
    budget = _section(f"{skill}/SKILL.md", "**Turn budget**", "## ")
    assert "One task per step, never a bookkeeping-only turn." in budget
    assert first_step in budget
    assert "rides with the next step's first real tool call" in budget
    assert "The only standalone call is the final `completed` right before the Final Report" in budget
    assert "TASK_" not in budget


@pytest.mark.parametrize("skill", ["refactor", "fix", "debug"])
def test_call_batching_never_moves_a_question_later(skill: str) -> None:
    """Batching independent calls stops at every question, and the worktree entry stays ahead of the codemap gate.

    A batch that ran a block placed after a question before that question was answered would move the ask later — the
    flow regression the ask-mapping tests exist to prevent.
    """
    budget = _section(f"{skill}/SKILL.md", "**Turn budget**", "## ")
    assert "Independent calls share one response — never across a question." in budget
    assert "Every later preamble block keeps its order" in budget
    assert "the worktree entry, which must precede the codemap gate" in budget


@pytest.mark.parametrize(
    ("skill", "start", "end"),
    [
        pytest.param("refactor", "## Step 4", "**Safety break**", id="refactor-step-4"),
        pytest.param("refactor", "## Step 5", "**Quality-stack load**", id="refactor-step-5"),
        pytest.param("fix", "## Step 3", "## Step 4", id="fix-step-3"),
        pytest.param("fix", "## Step 4", "## Final Report", id="fix-step-4"),
    ],
)
def test_loops_run_targeted_tests_only(skill: str, start: str, end: str) -> None:
    """Fix and refactor loops select tests per change and never fall back to the whole test directory.

    Measured runs re-ran the full suite up to seven times per session; the loop must call the selector and carry none of
    the old full-suite fallback wording.
    """
    loop = _section(f"{skill}/SKILL.md", start, end)
    assert _helper_run(skill) in loop
    assert "Full suite fallback" not in loop
    assert "full suite fallback" not in loop
    assert "Re-run full test suite" not in loop


@pytest.mark.parametrize("skill", ["refactor", "fix", "feature"])
def test_full_suite_wording_never_forbids_the_rerun(skill: str) -> None:
    """No "runs once" phrasing survives that could be read as forbidding the rerun after a full-suite failure.

    The skill must instead say the gate reruns after any fix to a full-suite failure.
    """
    text = _read(f"{skill}/SKILL.md")
    assert re.search(r"full suite (now )?runs once|only full-suite run|one full run of the whole", text) is None
    assert "again after any fix to a full-suite failure (§Final gate)" in text


@pytest.mark.parametrize(
    ("skill", "start"),
    [
        pytest.param("refactor", "**Final gate — full suite", id="refactor"),
        pytest.param("fix", "**Final gate rerun — full suite", id="fix"),
        pytest.param("feature", "**Final gate rerun — full suite", id="feature"),
    ],
)
def test_final_gate_reruns_in_full_after_any_fix(skill: str, start: str) -> None:
    """The full-suite gate reruns in full after any fix to a full-suite failure, within three iterations, ending green.

    The quality stack's post-fix re-checks can be scoped, so a fix made after its wide run would otherwise ship without
    a full run; a green full suite must be the last test evidence, and the cap never reports green.
    """
    gate = _section(f"{skill}/SKILL.md", start, "## Final Report")
    assert gate.count('python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_run_tests.py" suite') == 1
    assert 'eval "$TEST_CMD"' not in gate
    assert "any fix to a full-suite failure" in gate
    assert "then rerun this block in full" in gate
    assert "Max 3 gate iterations" in gate
    assert "never report the suite green" in gate
    assert "A green full suite is the last test evidence before the Final Report" in gate
    assert "`run_in_background: true`" in gate


def test_batch_mode_union_run_uses_the_selector() -> None:
    """Batch mode's single union run is the selector's run, and its line-number references are gone.

    Line-number references to ``SKILL.md`` go stale on every edit, so none may remain in the shared batch-mode doc.
    """
    batch = _read("_shared/batch-mode.md")
    assert '`dev_test_targets.py --skill "$_SKILL" --run` is that invocation' in batch
    assert 'dev_test_targets.py" --skill "$_SKILL" --run --record-dir "${DEV_DIR:-.developments}"' in batch
    assert re.search(r"SKILL\.md:\d+", batch) is None


def test_review_small_diff_never_reviewed_inline() -> None:
    """The review spawn-count gate never lets diff size replace the specialist fan-out with an inline review.

    The ~73-call inline rule is for work-displacement spawns. Review specialists are role-isolated, and a small change
    can carry the highest risk, so the gate keeps a "never zero" floor instead of an inline escape.
    """
    gate = _section("review/SKILL.md", "**Spawn-count gate", "Dimensions dropped by the cap")
    assert "do it inline, spawn nothing" not in gate
    assert "**never zero**" in gate
    assert "**Diff size never licenses inline review.**" in gate


def test_review_depth_follows_impact_not_diff_size() -> None:
    """The challenger runs at any diff size and the FIX trim waits for a LIGHT impact tier.

    A small diff on the main user story is the riskiest case, so neither the challenger nor the perf and architecture
    dimensions may be dropped on line count alone.
    """
    skill = _read("review/SKILL.md")
    assert "Small-diff challenger skip" not in skill
    assert "**Challenger at any diff size**" in skill
    assert "FIX → **only when `IMPACT_TIER=LIGHT`**" in skill
    assert "review_impact_tier.py" in skill


@pytest.mark.parametrize("skill", ["feature", "fix", "refactor"])
def test_implementation_challenge_is_independent_at_every_size(skill: str) -> None:
    """Small work retains concise planning and self-challenge before accepted execution.

    The plan gate covers small work with a parent self-challenge (not independent) and a bounded post-implementation
    challenge; the result gate then requires one independent reviewer over the scoped diff and never claims a pass
    without it.
    """
    plan_gate = _heading_section(f"{skill}/SKILL.md", _CHALLENGER_GATE, "## Step 2")
    result_gate = _after(f"{skill}/SKILL.md", "**Bounded independent implementation challenge**")
    assert "Small work" in plan_gate
    assert "parent self-challenge" in plan_gate
    assert "accept the challenged plan" in plan_gate
    assert "waives only the supplied plan artifact" in plan_gate
    assert "not independent" in plan_gate
    assert "bounded post-implementation challenge" in plan_gate
    assert "one independent reviewer" in result_gate
    assert "current scoped diff, task acceptance and relevant evidence" in result_gate
    assert "do not claim the challenge passed" in result_gate
    assert "Do not start another challenge or convergence loop automatically" in result_gate


def test_debug_challenger_retains_its_diagnosis_gate() -> None:
    """The read-only debugging workflow retains its independent hypothesis challenge.

    Size never skips the diagnosis gate; the old auto-skip wording must not return.
    """
    text = _read("debug/SKILL.md")
    assert "size never skips the gate" in text
    assert "auto-skip when" not in text


def test_prepass_preserves_required_challenge_and_explicit_loops() -> None:
    """Formatting and an unavailable bridge cannot silently satisfy a required independent challenge.

    The Codex pre-pass doc keeps a formatting-only skip from applying to a required challenge and runs iterative loops
    only on explicit request.
    """
    text = _read("_shared/codex-prepass.md")
    assert "formatting-only skip does not apply" in text
    assert "another permitted independent route" in text
    assert "only when the caller explicitly invokes an iterative loop" in text


def test_feature_report_records_incomplete_single_pass() -> None:
    """Unresolved findings or missing independent coverage cannot become a clean feature report.

    The report template carries placeholders for each unresolved gap and for the actual stop reason with completed
    passes.
    """
    text = _read("feature/templates/report-templates.md")
    assert "unresolved substantive issues or missing required independent coverage" in text
    assert "[each unresolved substantive gap and missing evidence]" in text
    assert "[actual stop reason and completed passes]" in text


@pytest.mark.parametrize("skill", ["feature", "fix", "refactor"])
def test_plan_challenge_resolves_authorized_findings_and_refreshes_hash(skill: str) -> None:
    """Routine plan revisions must not create false drift or trigger automatic user approval.

    Feasible authorized blockers are resolved without asking and the revised plan's hash is re-recorded; an unexplained
    or unauthorized change is never re-recorded.
    """
    gate = _heading_section(f"{skill}/SKILL.md", _CHALLENGER_GATE, "## Step 2")
    assert "Resolve feasible authorized blockers without asking" in gate
    assert "protected decision or authority" in gate
    assert "record the revised plan's hash" in gate
    assert 'bin/plan_hash.py" record "$PLAN_FILE"' in gate
    assert "Never re-record an unexplained or unauthorized change" in gate


def test_standalone_plan_resolves_feasible_blockers_before_asking() -> None:
    """Plan challenges ask only when they encounter a real decision or authority boundary."""
    text = _heading_section("plan/SKILL.md", "## Step 4: Challenger gate", "## Step 5")
    assert "Resolve feasible authorized blockers without asking" in text
    assert "protected decision or authority" in text
    assert "explicit user wait" in text


def test_debug_classification_hands_off_before_plan_and_challenge() -> None:
    """Unknown causes route to diagnosis without creating a speculative implementation plan.

    The handoff sends the user to ``/develop:debug`` and skips Steps 2-5 without consuming a user-loop counter.
    """
    route = _section("plan/SKILL.md", "**Debug classification handoff**", "## Step 2: Structured plan")
    assert "when the cause is unknown" in route
    assert "do not write or challenge an implementation plan" in route
    assert "skip Steps 2–5" in route
    assert "/develop:debug" in route
    assert "clear the skill contract" in route


def test_plan_summary_reports_actual_review_coverage() -> None:
    """A waived challenge or skipped feasibility review cannot appear as completed agent coverage."""
    summary = _after("plan/SKILL.md", "## Step 5: Final output")
    assert "report skipped or unavailable coverage explicitly" in summary
    assert "✓ agents ready" not in summary


def test_fix_diagnosis_uses_the_proportional_challenge_decision() -> None:
    """A diagnosis reuses root-cause evidence while retaining planning and explicit challenge waivers."""
    diagnosis = _section("fix/SKILL.md", "## Fix Mode", "## Flag parsing")
    assert "apply the challenger gate's proportional decision" in diagnosis
    assert "Do NOT skip challenger gate" not in diagnosis
    assert "explicit user waivers" in diagnosis
    assert "retaining concise planning and preimplementation challenge" in diagnosis
    assert "Do not repeat still-valid root-cause investigation" in diagnosis


def test_refactor_missing_stack_blocks_before_acceptance() -> None:
    """A broken quality-stack install must stop instead of silently skipping required checks."""
    branch = _section("refactor/SKILL.md", "**Quality-stack load**", "**Final gate")
    assert "the shared loader resolves warm state or the installed cache and fails visibly" in branch
    assert "Missing file → stop the skill" in branch
    assert "do not run the final gate or report completion" in branch
    assert "Not found → skip quality stack entirely" not in branch
    assert "**Bounded independent implementation challenge**" in branch
    assert "unless the user explicitly passed `--no-challenge`" in branch
    assert "If no independent route is available, report the gap" in branch

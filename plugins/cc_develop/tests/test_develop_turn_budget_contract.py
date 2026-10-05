"""Guard the develop turn-budget contracts: no-polling agent waits, run-level tasks, targeted tests in fix loops.

Measured refactor/fix/debug runs made one tool call per turn, spent up to 16 turns on task bookkeeping alone, re-ran the
full suite up to seven times per session, and left long agent waits for the user to notice ("finished?"). These tests
pin the instructions that remove each cost, so a later edit cannot quietly bring one back.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_SKILLS = Path(__file__).resolve().parents[1] / "skills"
_AGENT_RESOLUTION = _SKILLS / "_shared" / "agent-resolution.md"
_HELPER_RUN = 'bin/dev_test_targets.py" --pytest-cmd "$PYTEST_CMD" --run'


def _read(relative: str) -> str:
    """Read one develop skill file."""
    return (_SKILLS / relative).read_text(encoding="utf-8")


def _section(relative: str, start: str, end: str) -> str:
    """Return the text between two unique markers of one skill file."""
    text = _read(relative)
    begin = text.index(start)
    return text[begin : text.index(end, begin)]


class TestAgentWaits:
    def test_banned_wait_tools_are_named_as_forbidden(self) -> None:
        """The shared wait rule names every banned wait mechanism and the notification as the only resume signal."""
        rule = _AGENT_RESOLUTION.read_text(encoding="utf-8")
        assert "never call `ScheduleWakeup`, `ListAgents`, or a `Monitor` loop to wait for agents" in rule
        assert "the notification is the only resume signal" in rule

    def test_spawn_turn_names_what_is_in_flight(self) -> None:
        """The spawning turn ends by naming each agent in flight, so the user never has to ask whether it is waiting."""
        assert "End the spawning turn with one line naming each agent in flight" in _AGENT_RESOLUTION.read_text(
            encoding="utf-8"
        )

    @pytest.mark.parametrize(
        "option",
        [
            "(a) retry the challenge once",
            "(b) proceed unchallenged, ⏱ recorded in the Final Report",
            "(c) abort",
        ],
    )
    def test_timed_out_challenger_asks_instead_of_passing(self, option: str) -> None:
        """A timed-out challenger is never read as a clean pass; the user decides how to continue."""
        rule = _AGENT_RESOLUTION.read_text(encoding="utf-8")
        gate = rule[rule.index("A timed-out `foundry:challenger`") :]
        assert "AskUserQuestion" in gate
        assert option in gate

    def test_banned_tools_appear_only_as_prohibitions(self) -> None:
        """Every develop skill line naming a banned wait tool forbids it; none instructs its use."""
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
        assert "every 5 min" not in team
        assert "At most one liveness probe per wake-up" in team

    @pytest.mark.parametrize("skill", ["refactor", "fix", "debug"])
    def test_skill_points_at_the_shared_wait_rule(self, skill: str) -> None:
        """Each spawning skill loads the shared wait rule and says it applies to its spawns."""
        text = _read(f"{skill}/SKILL.md")
        assert 'cat "$_DEV_SHARED/agent-resolution.md"' in text
        assert "§Agent waits — no polling" in text


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
        pytest.param("refactor", "## Step 5", "**Foundry availability check**", id="refactor-step-5"),
        pytest.param("fix", "## Step 3", "## Step 4", id="fix-step-3"),
        pytest.param("fix", "## Step 4", "## Final Report", id="fix-step-4"),
    ],
)
def test_loops_run_targeted_tests_only(skill: str, start: str, end: str) -> None:
    """Fix and refactor loops select tests per change and never fall back to the whole test directory."""
    loop = _section(f"{skill}/SKILL.md", start, end)
    assert _HELPER_RUN in loop
    assert "Full suite fallback" not in loop
    assert "full suite fallback" not in loop
    assert "Re-run full test suite" not in loop


@pytest.mark.parametrize("skill", ["refactor", "fix", "feature"])
def test_full_suite_wording_never_forbids_the_rerun(skill: str) -> None:
    """No "runs once" phrasing survives that could be read as forbidding the rerun after a full-suite failure."""
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
    assert 'IFS= read -r TEST_CMD < "${TMPDIR:-/tmp}/dev-test-cmd-${CSID}"' in gate
    assert gate.count('eval "$TEST_CMD"') == 1
    assert "any fix to a full-suite failure" in gate
    assert "then rerun this block in full" in gate
    assert "Max 3 gate iterations" in gate
    assert "never report the suite green" in gate
    assert "A green full suite is the last test evidence before the Final Report" in gate
    assert "`run_in_background: true`" in gate


def test_batch_mode_union_run_uses_the_selector() -> None:
    """Batch mode's single union run is the selector's run, and its line-number references are gone."""
    batch = _read("_shared/batch-mode.md")
    assert '`dev_test_targets.py --pytest-cmd "$PYTEST_CMD" --run` is that invocation' in batch
    assert re.search(r"SKILL\.md:\d+", batch) is None


class TestAgentWatchDeadlines:
    def test_develop_ships_the_watch_helper_byte_identical_to_resolve(self) -> None:
        """Develop carries its own agent_watch.py (plugins are self-contained), identical to the oss:resolve
        canonical."""
        develop = _SKILLS.parent / "bin" / "agent_watch.py"
        canonical = _SKILLS.parents[1] / "cc_oss" / "bin" / "agent_watch.py"
        assert develop.read_bytes() == canonical.read_bytes()

    def test_shared_rule_arms_and_checks_with_the_helper(self) -> None:
        """The shared rule arms one TSV per spawn batch and runs the plugin's own agent_watch.py once per wake-up."""
        rule = _AGENT_RESOLUTION.read_text(encoding="utf-8")
        assert "agent-watch-<batch>.tsv" in rule
        assert 'python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/agent_watch.py" --state-dir "$WATCH_DIR"' in rule
        assert "plugins/cc_oss/bin" not in rule

    @pytest.mark.parametrize("batch", ["scope", "challenge", "tests", "team", "review", "verify", "consolidate"])
    def test_every_batch_has_a_deadline(self, batch: str) -> None:
        """Each spawn batch named at a spawn site has a row in the deadline table."""
        assert re.search(rf"^\| `{batch}` \|.*\| \d+ \|$", _AGENT_RESOLUTION.read_text(encoding="utf-8"), re.MULTILINE)

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
        assert '> "${TMPDIR:-/tmp}/dev-agent-watch-dir-${CSID}"  # fresh per run' in _read(f"{skill}/SKILL.md")


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


@pytest.mark.parametrize("skill", ["feature", "fix", "refactor", "debug"])
def test_challenger_gate_ignores_change_size(skill: str) -> None:
    """The pre-implementation challenger runs at any change size unless the user passes --no-challenge.

    Each gate once auto-skipped single-file changes under ~50 lines, which are exactly the small main-path edits that
    carry the most hidden risk.
    """
    text = _read(f"{skill}/SKILL.md")
    assert "size never skips the gate" in text
    assert "auto-skip when" not in text

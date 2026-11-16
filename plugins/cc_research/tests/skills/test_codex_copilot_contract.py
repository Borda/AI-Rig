"""Check the co-pilot's combined-change-set exception and its quota-stop contract."""

from pathlib import Path

import pytest
from _research_text import list_after as _list_after

_PLUGIN = Path(__file__).resolve().parents[2]
_COPILOT = _PLUGIN / "skills/run/modes/codex-copilot.md"
_RUN_SKILL = _PLUGIN / "skills/run/SKILL.md"
_TEAM = _PLUGIN / "skills/run/modes/team.md"
_RESUME = _PLUGIN / "skills/run/modes/resume.md"


def _lines(path: Path) -> list[str]:
    """Return the Markdown file's lines without their trailing newlines."""
    return path.read_text(encoding="utf-8").splitlines()


def _bash_blocks(path: Path) -> list[list[str]]:
    """Return every fenced Bash block in ``path`` as its list of lines."""
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in _lines(path):
        stripped = line.strip()
        if stripped == "```bash":
            current = []
        elif stripped == "```" and current is not None:
            blocks.append(current)
            current = None
        elif current is not None:
            current.append(stripped)
    return blocks


def _quota_rule() -> str:
    """Return the outcome bullet that classifies a harness quota stop."""
    return next(line for line in _lines(_COPILOT) if line.startswith("- Codex stopped on a **harness-classified quota"))


def _unclassified_rule() -> str:
    """Return the outcome bullet that keeps every other stop on today's path."""
    return next(line for line in _lines(_COPILOT) if line.startswith("- Not classified"))


def _r7_section() -> list[str]:
    """Return the lines of run Step R7, up to the next top-level heading."""
    lines = _lines(_RUN_SKILL)
    start = next(index for index, line in enumerate(lines) if line.startswith("### Step R7:"))
    end = next(index for index in range(start + 1, len(lines)) if lines[index].startswith("## "))
    return lines[start:end]


def _r7_quota_rule() -> str:
    """Return the R7 lead line that ends delegation on a harness quota fault."""
    return next(
        line
        for line in _r7_section()
        if line.startswith("A delegation call that ends on a harness-classified quota stop")
    )


def _r7_quota_steps() -> list[str]:
    """Return the ordered numbered steps that follow the R7 quota lead line, in document order."""
    section = _r7_section()
    steps: list[str] = []
    for line in section[section.index(_r7_quota_rule()) + 1 :]:
        if not line.strip():
            if steps:
                break
            continue
        if not line[:1].isdigit():
            break
        steps.append(line)
    return steps


def _r7_quota_step(literal: str) -> int:
    """Return the position of the R7 quota step containing ``literal``."""
    return next(index for index, step in enumerate(_r7_quota_steps()) if literal in step)


def _resume_lift_index() -> int:
    """Return the index of the resume-mode lead line that lifts a stored quota stop."""
    return next(
        i for i, line in enumerate(_lines(_RESUME)) if line.strip().startswith("**Lift a stored Codex quota stop**")
    )


def _resume_lift_bullets() -> list[str]:
    """Return the bullets under the resume-mode quota-lift lead line."""
    return _list_after(_lines(_RESUME), _resume_lift_index())


def _approval_items() -> list[str]:
    """Return the co-pilot dispatch approval bullets: the first line naming a recorded user decision and its list."""
    lines = _lines(_COPILOT)
    start = next(i for i, line in enumerate(lines) if "recorded user decision" in line)
    return [lines[start].strip(), *_list_after(lines, start)]


def test_phase2c_names_the_clean_tree_checklist_exception() -> None:
    """The second pass explicitly permits Claude's pending edit for combined review."""
    lines = (_PLUGIN / "skills/run/modes/codex-copilot.md").read_text(encoding="utf-8").splitlines()
    exception = next(line for line in lines if line.startswith("> **Clean-tree exception.**"))
    assert "sanctioned exception to the clean-tree step of the Write-Delegation Checklist" in exception
    assert "`bridge:implement`" in exception
    assert "Claude's uncommitted Phase 2 edit" in exception
    assert "Claude and Codex edits are reviewed together as one combined change set" in exception


def test_clean_tree_exception_preserves_existing_safeguards() -> None:
    """The exception retains path limits, the snapshot, review, proof, and commit ownership."""
    lines = (_PLUGIN / "skills/run/modes/codex-copilot.md").read_text(encoding="utf-8").splitlines()
    exception = next(line for line in lines if line.startswith("> **Clean-tree exception.**"))
    assert "All other checklist steps remain in force" in exception
    assert "owned paths" in exception
    assert "`scope_files`" in exception
    assert "iteration-start SHA snapshot" in exception
    assert "Phase 3 scope check" in exception
    assert "read the full combined diff" in exception
    assert "run the actual proof commands" in exception
    assert "the bridge never commits" in exception


def test_readme_explains_the_combined_change_set_exception() -> None:
    """Users can discover the clean-tree exception beside the co-pilot integration flag."""
    lines = (_PLUGIN / "README.md").read_text(encoding="utf-8").splitlines()
    assert any(
        line.startswith("- `--codex` co-pilot")
        and "clean-tree step of the Write-Delegation Checklist" in line
        and "one combined change set" in line
        for line in lines
    )


class TestQuotaStop:
    """A harness-classified quota stop disables Codex for the rest of the run and the loop continues in-house."""

    def test_decides_on_the_incident_fault(self) -> None:
        """The quota outcome is keyed on a non-null incident whose file names ``quota-exhausted``.

        Synthetic harness envelope case: ``status`` blocked or timeout, ``incident`` linking a file with
        ``fault: quota-exhausted``. The rule must name every part of that provenance check, not the verdict prose.
        """
        rule = _quota_rule()
        assert "`status` is `blocked` or `timeout`" in rule
        assert "`incident` is non-null" in rule
        assert "`fault` equal to `quota-exhausted`" in rule

    def test_records_blocked_outcome_not_noop(self) -> None:
        """The quota stop is recorded as ``codex-blocked:quota`` through the ``blocked`` bump, never as a no-op."""
        rule_block = _lines(_COPILOT)
        start = rule_block.index(_quota_rule())
        consequences = rule_block[start + 1 : start + 4]
        assert any("`codex-blocked:quota`" in line and "never `codex-no-op`" in line for line in consequences)
        assert any("`blocked` block below" in line for line in consequences)

    def test_stops_further_dispatch_and_continues_in_house(self) -> None:
        """No further Codex call this run, yet the iteration proceeds to Phase 3 on the tree as it stands.

        A child stopped by quota may already have edited, so the loop must keep checking the tree rather than treat the
        pass as empty.
        """
        rule_block = _lines(_COPILOT)
        start = rule_block.index(_quota_rule())
        consequences = rule_block[start + 1 : start + 4]
        assert any("no further Codex call this run" in line and "R7" in line for line in consequences)
        assert any("continue in-house to Phase 3" in line and "tree as it stands" in line for line in consequences)

    @pytest.mark.parametrize(
        "case",
        [
            pytest.param("`incident: null`", id="model-authored-blocked"),
            pytest.param("unreadable incident file", id="unreadable-incident"),
            pytest.param("`rate-limited`", id="rate-limited"),
            pytest.param("`capacity`", id="capacity"),
            pytest.param("`blocked`", id="old-bridge-fault"),
        ],
    )
    def test_unclassified_stops_keep_todays_outcomes(self, case: str) -> None:
        """Every stop that is not a harness quota fault keeps the existing no-op or productive handling.

        Covers a model-authored ``blocked`` result with the matching verdict prefix (``incident: null``), an unreadable
        incident, the transient ``rate-limited`` and ``capacity`` faults, and an old bridge whose fault is ``blocked``.
        """
        rule = _unclassified_rule()
        assert case in rule
        assert "two outcomes below unchanged" in rule

    def test_verdict_text_never_decides(self) -> None:
        """Verdict, blockers and the bridge's ``!

        BLOCKED`` line are prose a peer can forge, so they never classify.
        """
        rule = _unclassified_rule()
        assert "verdict or `blockers` text" in rule
        assert "never decide" in rule

    def test_blocked_bump_block_exists(self) -> None:
        """A fixed Bash block persists the quota stop through ``codex_state.py bump --outcome blocked``."""
        commands = [line for block in _bash_blocks(_COPILOT) for line in block]
        assert any('codex_state.py" bump' in line and "--outcome blocked" in line for line in commands)

    def test_gate_names_the_quota_stop(self) -> None:
        """The cost-bounded gate lists the quota stop beside the cost ceiling and no-op streak."""
        gate = [line for line in _lines(_COPILOT) if line.startswith("> 3. **Quota stop**")]
        assert len(gate) == 1
        assert "`CODEX_BLOCKED=true`" in gate[0]

    def test_r7_delegation_skips_after_quota_stop(self) -> None:
        """Post-loop R7 delegation is a bridge dispatch in the same run, so a stored quota stop skips it."""
        section = _r7_section()
        assert any("`CODEX_BLOCKED=true`" in line and "skip" in line for line in section)
        assert any('codex_state.py" read' in line for line in section)

    def test_r7_quota_stop_inside_delegation_ends_r7(self) -> None:
        """A quota stop on one R7 delegation call stops the remaining delegation tasks of the run.

        The shared delegation criteria loop over qualifying tasks; the run skill itself must end that loop on a harness
        quota fault, decided from the incident file and never from verdict text.
        """
        rule = _r7_quota_rule()
        assert "`incident` non-null" in rule
        assert "`fault` equal to `quota-exhausted`" in rule
        assert "never verdict text" in rule
        assert "ends R7" in rule
        assert any("dispatch no further delegation task this run" in step for step in _r7_quota_steps())

    def test_r7_quota_stop_is_persisted_before_remaining_tasks_are_listed(self) -> None:
        """An R7 quota stop is written to codex-state first, so compaction or re-entry cannot lose it.

        The first quota fault can occur inside R7 (run without ``--codex``, or a co-pilot never blocked); the in-context
        stop alone would let a re-entered R7 pass its pre-check and dispatch again. The rule must order the persist step
        before listing the remaining tasks, and a ``blocked`` bump block on the run's codex-state must follow the rule.
        """
        section = _r7_section()
        after_steps = section[section.index(_r7_quota_steps()[-1]) + 1 :]
        block = next(line for line in after_steps if 'codex_state.py" bump' in line)
        persist = _r7_quota_step("persist it with the block below")
        assert persist < _r7_quota_step("dispatch no further delegation task")
        assert persist < _r7_quota_step("List the remaining tasks as not delegated")
        assert '--state ".experiments/state/${RUN_ID}/codex-state" --outcome blocked' in block


class TestStatusEnums:
    """The quota marker is diary-only: never an iteration record status, never a stuck-detector discard."""

    @pytest.mark.parametrize(
        "path",
        [pytest.param(_RUN_SKILL, id="run-skill"), pytest.param(_TEAM, id="team-mode")],
    )
    def test_status_enum_omits_diary_only_quota_marker(self, path: Path) -> None:
        """Phase 8 and team-results status enums keep their values and never offer ``codex-blocked:quota``.

        No record can carry it: the iteration status is the Phases 3–7 outcome. Offering it would also let a record
        outside the stuck list reset the discard streak.
        """
        enums = [line for line in _lines(path) if line.strip().startswith('"status": "kept|')]
        assert enums
        assert all("codex-no-op" in line and "codex-blocked" not in line for line in enums)

    def test_quota_marker_rule_keeps_record_status(self) -> None:
        """The quota outcome states that the marker is diary-only and the record status stays the iteration outcome."""
        lines = _lines(_COPILOT)
        start = lines.index(_quota_rule())
        marker = next(line for line in lines[start + 1 : start + 4] if "`codex-blocked:quota`" in line)
        assert "diary-only" in marker
        assert "Phase 8 `status` stays the Phases 3–7 outcome" in marker

    def test_stuck_detection_excludes_quota_block(self) -> None:
        """A quota stop continues the loop in-house, so it is not one of the stuck-detection discard statuses."""
        stuck = next(line for line in _lines(_RUN_SKILL) if line.startswith("- **Stuck detection**"))
        assert "codex-no-op" in stuck
        assert "codex-blocked" not in stuck


class TestQuotaLift:
    """A stored quota stop is lifted only by the user's own ``--codex`` on a resume of the same run."""

    def test_resume_lifts_only_on_this_invocations_codex(self) -> None:
        """The lift is keyed on ``--codex`` typed in this resume request, which is a new user approval.

        A user who added credits resumes with ``/research:run --resume <program.md> --codex``; that fresh request, not
        the stored flag, authorizes Codex again, and the helper reads it from the request's own arguments.
        """
        bullets = _resume_lift_bullets()
        assert any(
            "this invocation's own arguments carry a typed `--codex`" in item
            and "new approval" in item
            and "`codex_state.py arm`" in item
            for item in bullets
        )

    def test_block_runs_on_every_resume(self) -> None:
        """The lift block runs unconditionally and the helper decides, so no model judgment gates the lift.

        Changed contract: the block used to run only when the model judged the arguments to carry ``--codex``; the
        decision now lives in ``codex_state.py``, which a model cannot talk into lifting.
        """
        assert any(
            item.startswith("- Run the block below on every resume") and "`codex_state.py unblock` decides" in item
            for item in _resume_lift_bullets()
        )

    @pytest.mark.parametrize(
        ("case", "outcome"),
        [
            pytest.param("restored from `flags`", "never approves the lift", id="restored-flag"),
            pytest.param("resume without it", "never approves the lift", id="resume-without-codex"),
            pytest.param("another skill's or agent's resume", "never passes `--codex`", id="internal-invocation"),
        ],
    )
    def test_other_resumes_keep_the_stop(self, case: str, outcome: str) -> None:
        """A restored flag and a resume without ``--codex`` never approve; skills never type it for the user.

        These carry no fresh user decision, so the stored quota stop must stay and Codex stays off for the run. Code
        cannot tell a skill's resume from the user's, so that case stays an instruction.
        """
        assert any(case in item and outcome in item for item in _resume_lift_bullets())

    def test_later_stop_spends_the_approval(self) -> None:
        """The lift happens before R2 and any Codex call; a stop recorded later in the session is never lifted.

        Changed contract: "never re-run it after a compaction" was an instruction; a replay of the block now finds the
        approval spent by that later stop and keeps it.
        """
        lead = _lines(_RESUME)[_resume_lift_index()]
        assert "before R2" in lead
        assert any(
            "recorded later in this session spends that approval" in item
            and "re-running the block after a compaction" in item
            for item in _resume_lift_bullets()
        )

    def test_arguments_are_read_in_the_run_skill(self) -> None:
        """The ``arm`` call carrying ``$ARGUMENTS`` lives in ``run/SKILL.md`` Resume Mode, before ``resume.md`` loads.

        The host substitutes ``$ARGUMENTS`` only in the skill file itself; inside a mode file loaded by a command it is
        an unset shell variable, so an argument check there would refuse every lift.
        """
        lines = _lines(_RUN_SKILL)
        start = lines.index("## Resume Mode")
        end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("## "))
        arm = next(i for i in range(start, end) if 'codex_state.py" arm -- "$ARGUMENTS"' in lines[i])
        loader = next(i for i in range(start, end) if 'modes/resume.md"' in lines[i])
        assert arm < loader
        assert not any("$ARGUMENTS" in line for line in _lines(_RESUME) if "codex_state.py" in line)

    def test_lift_block_precedes_r2_and_the_loop(self) -> None:
        """A fixed ``codex_state.py unblock`` block on the run's codex-state follows the lead, before R2 and the
        loop."""
        lines = _lines(_RESUME)
        lead = _resume_lift_index()
        block = next(line for line in lines[lead:] if 'codex_state.py" unblock' in line)
        rerun_r2 = next(i for i, line in enumerate(lines) if "**Re-run R2**" in line)
        loop = next(i for i, line in enumerate(lines) if "**Continue restored mode**" in line)
        assert '--state ".experiments/state/${RUN_ID}/codex-state"' in block
        assert lead < lines.index(block) < rerun_r2 < loop

    def test_approval_names_the_lift(self) -> None:
        """The co-pilot approval list says the same direct request lifts a stored stop and a restored flag does not."""
        items = _approval_items()
        assert any("lifts a stored quota stop" in item and "restored flag" in item for item in items)

    def test_gate_names_the_lift(self) -> None:
        """The quota gate item says the stop holds until the user's own resume with ``--codex`` lifts it."""
        gate = next(line for line in _lines(_COPILOT) if line.startswith("> 3. **Quota stop**"))
        assert "until" in gate
        assert "lifts it" in gate


def test_readme_explains_the_quota_stop() -> None:
    """Users learn that a Codex quota stop ends the co-pilot for the run while the loop continues."""
    lines = _lines(_PLUGIN / "README.md")
    assert any(line.startswith("- `--codex` co-pilot") and "`quota-exhausted`" in line for line in lines)


def test_readme_explains_the_quota_lift() -> None:
    """Users learn how to re-enable Codex after topping up: resume the same run with ``--codex`` typed again."""
    lines = _lines(_PLUGIN / "README.md")
    start = next(
        i for i, line in enumerate(lines) if line.startswith("- `--codex` co-pilot") and "`quota-exhausted`" in line
    )
    sub_items = _list_after(lines, start)
    assert any("`--resume`" in item and "`--codex`" in item and "lifts" in item for item in sub_items)

"""Tests for ``bin/check_agent_waits.py`` and the repository-wide no-polling, no-bookkeeping-turn contract.

The checker is the enforcement point for the rule that waiting on a spawned agent is never a tool call and that task
bookkeeping never takes a turn of its own. The last tests run it over every plugin in the repository, so a skill in any
plugin that starts prescribing ``ScheduleWakeup``, a fixed-interval poll, or a standalone ``TaskUpdate`` turn fails CI.
"""

from __future__ import annotations

from pathlib import Path

import check_agent_waits
import pytest


@pytest.mark.timeout(3)
@pytest.mark.parametrize("prefix", ["every 1 min ", "-newer "])
def test_repeated_poll_prefixes_have_bounded_classification_time(prefix: str) -> None:
    """Repeated incomplete polling clauses cannot force a fresh suffix scan at every prefix."""
    assert check_agent_waits.classify(prefix * 20000) is None


REPO_ROOT = Path(__file__).resolve().parents[5]
FOUNDRY = REPO_ROOT / "plugins" / "cc_foundry"


class TestClassify:
    """Line classification separates instructions from prohibitions."""

    @pytest.mark.parametrize(
        ("line", "kind"),
        [
            pytest.param("Then call ScheduleWakeup(300) and look again.", "wait-tool", id="schedule-wakeup"),
            pytest.param("Use ListAgents to see whether the curator finished.", "wait-tool", id="list-agents"),
            pytest.param("One bounded `Monitor` call per turn is allowed.", "wait-tool", id="monitor-allowance"),
            pytest.param("Poll every 60s until the file appears.", "fixed-poll", id="poll-every"),
            pytest.param("Every 5 min while waiting: count new partial files.", "fixed-poll", id="every-five-min"),
            pytest.param('eval "$(python health_sentinel.py start x)"', "fixed-poll", id="health-sentinel"),
            pytest.param('find "$RUN" -newer "$S" -type f | wc -l', "fixed-poll", id="find-newer-probe"),
            pytest.param("Send TaskUpdate in a separate response.", "bookkeeping-turn", id="separate-response"),
        ],
    )
    def test_flags_instruction(self, line: str, kind: str) -> None:
        """Each prescriptive waiting or bookkeeping-turn line is reported under its own kind.

        These are the shapes observed in real skill text before the rule changed — the allowance wording included.
        """
        assert check_agent_waits.classify(line) == kind

    @pytest.mark.parametrize(
        "line",
        [
            pytest.param("Never call ScheduleWakeup, ListAgents or Monitor to wait on an agent.", id="never"),
            pytest.param("No `ScheduleWakeup`, no poll loop, no sleep.", id="no"),
            pytest.param("TaskUpdate(completed) as its own response before the long output block.", id="sanctioned"),
            pytest.param("grep -c 'MONITOR_INTERVAL\\|poll every' \"$f\"", id="detector-source"),
            pytest.param("Monitor(...)  <!-- wait-check: allow -->", id="explicit-allow"),
            pytest.param("TaskUpdate rides with the next substantive tool call.", id="rides-along"),
        ],
    )
    def test_skips_prohibition_or_exemption(self, line: str) -> None:
        """Lines that forbid the pattern, detect it, or carry a reviewed exemption are not findings.

        Without this the rule text itself, and the audit detectors that enforce it, would fail their own check.
        """
        assert check_agent_waits.classify(line) is None


class TestMain:
    """CLI exit codes and output over a scanned tree."""

    def test_reports_finding_and_exits_one(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A skill under the scan dir that prescribes ListAgents fails with a path:line finding.

        The scan covers ``skills/**`` so mode and template files are reached, not only ``SKILL.md``.
        """
        mode = tmp_path / "demo" / "skills" / "run" / "modes" / "wait.md"
        mode.parent.mkdir(parents=True)
        mode.write_text("# Wait\n\nCall ListAgents until done.\n", encoding="utf-8")
        assert check_agent_waits.main(["--scan-dir", str(tmp_path)]) == 1
        assert f"wait-tool: {mode.as_posix()}:3:" in capsys.readouterr().out

    def test_clean_tree_exits_zero(self, tmp_path: Path) -> None:
        """A tree whose agent text only forbids waiting tools passes.

        The agent file names ``ScheduleWakeup`` only inside a prohibition, which the classifier must not report.
        """
        agent = tmp_path / "demo" / "agents" / "a.md"
        agent.parent.mkdir(parents=True)
        agent.write_text("Never use ScheduleWakeup.\n", encoding="utf-8")
        assert check_agent_waits.main(["--scan-dir", str(tmp_path)]) == 0


def test_every_plugin_is_free_of_agent_polling_and_bookkeeping_turns(capsys: pytest.CaptureFixture[str]) -> None:
    """No skill or agent in any plugin prescribes a waiting tool, a fixed-interval poll, or a bookkeeping-only turn.

    This is the standing enforcement of the user's decision: a new instruction anywhere under ``plugins/`` that
    reintroduces polling fails here, with the offending path and line in the output.
    """
    code = check_agent_waits.main(["--scan-dir", str(REPO_ROOT / "plugins")])
    assert code == 0, capsys.readouterr().out


@pytest.mark.parametrize(
    "relative",
    [
        "skills/audit/SKILL.md",
        "skills/brainstorm/SKILL.md",
        "skills/calibrate/SKILL.md",
        "skills/create/SKILL.md",
        "skills/distill/modes/executables.md",
        "skills/distill/modes/external.md",
        "skills/distill/modes/memory.md",
        "skills/distill/modes/prune.md",
        "skills/investigate/SKILL.md",
        "skills/manage/SKILL.md",
    ],
)
def test_foundry_spawn_sites_use_the_shared_deadline_protocol(relative: str) -> None:
    """Every foundry file that spawns agents arms deadlines through the shared protocol, not its own wait logic.

    Each listed skill or mode file spawns agents; it must name the shared protocol document and the ``agent-watch-``
    deadline file it writes, so a stalled agent is surfaced by the same mechanism everywhere.
    """
    text = (FOUNDRY / relative).read_text(encoding="utf-8")
    assert ("agent-spawn-protocol.md" in text, "agent-watch-" in text) == (True, True)


def test_scoped_rerun_reads_freshness_from_nested_index() -> None:
    """The quality stack trusts a test-impact scope only via the nested ``index`` freshness fields, then reruns in full.

    ``codemap-py query test-impact`` puts ``stale`` and ``query_complete`` under ``payload["index"]``; reading them at
    the top level finds nothing and trusts a stale index. A clean scoped cycle must still end with one full run. The
    prose is compared with whitespace collapsed, so reflowing a paragraph never breaks the assertion.
    """
    path = FOUNDRY / "skills" / "_shared" / "quality-stack.md"
    text = " ".join(path.read_text(encoding="utf-8").split())
    assert "`index.stale` is false and `index.query_complete` is true" in text
    assert "never at the top level" in text
    assert "run the full quality stack (its directory-wide pytest line, the repository's own settings) once" in text


class TestSilentTimeouts:
    """A skill that spawns agents must show the user any agent that never delivered."""

    def test_flags_spawning_skill_without_timeout_report(self, tmp_path: Path) -> None:
        """A skill whose files spawn an agent but never mention ⏱ or timed_out is reported once, by directory.

        Replacing polling with notifications must not turn a stalled agent into a silent one.
        """
        skill = tmp_path / "demo" / "skills" / "run"
        (skill / "modes").mkdir(parents=True)
        (skill / "SKILL.md").write_text("Spawn it.\n", encoding="utf-8")
        (skill / "modes" / "a.md").write_text('Agent(subagent_type="x:y", prompt="go")\n', encoding="utf-8")
        findings = check_agent_waits.silent_timeouts(sorted(skill.rglob("*.md")))
        assert [(f.kind, f.path) for f in findings] == [("silent-timeout", skill.as_posix())]

    def test_timeout_report_anywhere_in_skill_dir_satisfies(self, tmp_path: Path) -> None:
        """A ⏱ report in any file of the same skill directory covers a spawn in another file of it."""
        skill = tmp_path / "demo" / "skills" / "run"
        (skill / "modes").mkdir(parents=True)
        (skill / "SKILL.md").write_text("Missing output → ⏱ timed_out, surfaced.\n", encoding="utf-8")
        (skill / "modes" / "a.md").write_text('Agent(subagent_type="x:y", prompt="go")\n', encoding="utf-8")
        assert check_agent_waits.silent_timeouts(sorted(skill.rglob("*.md"))) == []


def _task_skill(tmp_path: Path, text: str) -> Path:
    """Write ``text`` as the ``SKILL.md`` of a demo skill and return the file."""
    skill = tmp_path / "demo" / "skills" / "run" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(text, encoding="utf-8")
    return skill


class TestTaskBlocks:
    """Task-only text blocks must ride with real work, never open a step with ``in_progress``."""

    def test_flags_standalone_in_progress_block(self, tmp_path: Path) -> None:
        """A text block holding only a ``TaskUpdate`` to ``in_progress`` is reported at its fence line.

        This is the per-step start-of-step update that costs a full-context turn for one bookkeeping record.
        """
        skill = _task_skill(tmp_path, "Rides with the next call.\n\n```text\nTaskUpdate(1, in_progress)\n```\n")

        findings = check_agent_waits.task_block_findings(skill)

        assert [(f.kind, f.line) for f in findings] == [("standalone-in-progress", 3)]

    def test_flags_task_block_that_never_says_how_it_rides(self, tmp_path: Path) -> None:
        """A task-only block with no riding or long-output explanation nearby is reported as unexplained.

        The explanation may sit in the 400 characters before the block or in the block itself.
        """
        skill = _task_skill(tmp_path, "Mark the step.\n\n```text\nTaskUpdate(1, completed)\n```\n")

        findings = check_agent_waits.task_block_findings(skill)

        assert [(f.kind, f.line) for f in findings] == [("unexplained-task-block", 3)]

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("Rides with the next call.\n```text\nTaskUpdate(1, completed)\n```\n", id="rides-before"),
            pytest.param("Done, before the long output.\n```text\nTaskUpdate(1, completed)\n```\n", id="long-output"),
            pytest.param(
                "Same response as the work.\n```text\nTaskCreate(a)\nTaskCreate(b)\n```\n", id="same-response"
            ),
            pytest.param("```text\nTaskUpdate(1, in_progress)\nplain output line\n```\n", id="mixed-block-ignored"),
            pytest.param("```text\nsome output\n```\n", id="no-task-calls"),
        ],
    )
    def test_accepts_explained_or_non_task_blocks(self, tmp_path: Path, text: str) -> None:
        """Blocks that state how they ride, or that hold more than task calls, are not findings.

        A block mixing a task call with other output is real work, so it is never a task-only block.
        """
        assert check_agent_waits.task_block_findings(_task_skill(tmp_path, text)) == []

    def test_block_explanation_survives_prose_reflow(self, tmp_path: Path) -> None:
        """A riding explanation wrapped across lines still counts: the window is flattened before matching."""
        skill = _task_skill(tmp_path, "Each call rides\nwith the next call.\n```text\nTaskUpdate(1, completed)\n```\n")

        assert check_agent_waits.task_block_findings(skill) == []


class TestMissingRideRules:
    """A skill that drives tasks must state that task calls ride with real work."""

    def test_flags_task_skill_without_ride_rule(self, tmp_path: Path) -> None:
        """A skill calling ``TaskCreate(`` with no bookkeeping-only or rides-with wording is reported by directory."""
        skill = _task_skill(tmp_path, "Call TaskCreate(step) first.\n")

        findings = check_agent_waits.missing_ride_rules([skill])

        assert [(f.kind, f.path) for f in findings] == [("missing-ride-rule", skill.parent.as_posix())]

    def test_ride_rule_anywhere_in_skill_dir_satisfies(self, tmp_path: Path) -> None:
        """The rule in a mode file covers task calls in ``SKILL.md`` of the same skill directory."""
        skill = _task_skill(tmp_path, "Call TaskCreate(step) first.\n")
        mode = skill.parent / "modes" / "a.md"
        mode.parent.mkdir()
        mode.write_text("Never spend a bookkeeping-only turn.\n", encoding="utf-8")

        assert check_agent_waits.missing_ride_rules([skill, mode]) == []

    def test_skill_without_task_calls_needs_no_rule(self, tmp_path: Path) -> None:
        """A skill that never calls the task tools is not asked for the rule."""
        assert check_agent_waits.missing_ride_rules([_task_skill(tmp_path, "Just prose.\n")]) == []

    def test_main_exits_one_for_task_block_violation(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """The command line reports a task-block finding as ``kind: path:line`` and exits 1."""
        skill = _task_skill(tmp_path, "Rides with the next call.\n```text\nTaskUpdate(1, in_progress)\n```\n")

        assert check_agent_waits.main(["--scan-dir", str(tmp_path)]) == 1
        assert f"standalone-in-progress: {skill.as_posix()}:2:" in capsys.readouterr().out

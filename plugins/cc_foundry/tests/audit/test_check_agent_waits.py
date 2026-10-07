"""Tests for ``bin/check_agent_waits.py`` and the repository-wide no-polling, no-bookkeeping-turn contract.

The checker is the enforcement point for the rule that waiting on a spawned agent is never a tool call and that task
bookkeeping never takes a turn of its own. The last tests run it over every plugin in the repository, so a skill in any
plugin that starts prescribing ``ScheduleWakeup``, a fixed-interval poll, or a standalone ``TaskUpdate`` turn fails CI.
"""

from __future__ import annotations

from pathlib import Path

import check_agent_waits
import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
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
        """A tree whose agent text only forbids waiting tools passes."""
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
    """Every foundry file that spawns agents arms deadlines through the shared protocol rather than its own wait
    logic."""
    text = (FOUNDRY / relative).read_text(encoding="utf-8")
    assert "agent-spawn-protocol.md" in text
    assert "agent-watch-" in text


def test_scoped_rerun_reads_freshness_from_nested_index() -> None:
    """The quality stack trusts a test-impact scope only via the nested ``index`` freshness fields, then reruns in full.

    ``codemap-py query test-impact`` puts ``stale`` and ``query_complete`` under ``payload["index"]``; reading them at
    the top level finds nothing and trusts a stale index. A clean scoped cycle must still end with one full run.
    """
    text = (FOUNDRY / "skills" / "_shared" / "quality-stack.md").read_text(encoding="utf-8")
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

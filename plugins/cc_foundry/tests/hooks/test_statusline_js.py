"""Subprocess tests for ``hooks/statusline.js``.

The hook is a status-line renderer: it consumes a JSON payload on stdin and writes a two-line ANSI string to stdout.
State for the segments lives under ``<hook temp base>/claude-state-<session_id>/`` and is written by ``task-log.js`` at
runtime. These tests seed that state directly, then assert against ``statusline.js`` stdout.

Line 2 format: ``⚡ <skills> │ 🤖 <agents> │ [🧠 <codex runs> │] 🛠️ <tools>``.

Codex bridge runs appear in their own ``🧠`` segment, only while one is active. Assertions strip ANSI escape sequences
before substring matching so the rendered marker and label co-occurrence is testable irrespective of color wrapping.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from _hook_env import _hook_state_base

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(s: str) -> str:
    """Remove ANSI CSI sequences so substring assertions are color-agnostic.

    Examples:
        >>> _strip_ansi("\\x1b[31mred\\x1b[0m")
        'red'
    """
    return _ANSI.sub("", s)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(name="sid")
def _sid() -> Iterator[str]:
    """Yield a unique session id; clean its ``claude-state-<id>`` dir on teardown.

    Base resolved via ``_hook_state_base()`` so teardown targets the directory the hook's ``getSentinelDir()`` writes on
    this platform. The hook's state base is machine-global, so the id is random: a name derived from the test would
    collide with a concurrent pytest session running the same test.
    """
    s = f"pytest-{uuid.uuid4().hex}"
    yield s
    shutil.rmtree(_hook_state_base() / f"claude-state-{s}", ignore_errors=True)


@pytest.fixture(name="tmp_home")
def _tmp_home(tmp_path: Path) -> Path:
    """Return an isolated HOME with an empty subscription.json so reads succeed."""
    h = tmp_path / "home"
    sub_dir = h / ".claude" / "state"
    sub_dir.mkdir(parents=True)
    (sub_dir / "subscription.json").write_text("{}", encoding="utf-8")
    return h


@pytest.fixture
def render(sid: str, tmp_home: Path, run_hook) -> Callable[[], str]:
    """Return a callable that runs ``statusline.js`` for the session and gives back its ANSI-stripped stdout."""

    def _render() -> str:
        result = run_hook("statusline.js", _payload(sid), home=tmp_home)
        assert result.returncode == 0, result.stderr
        return _strip_ansi(result.stdout)

    return _render


# ── Payload helper ───────────────────────────────────────────────────────────


def _payload(sid: str) -> dict:
    """Return a minimal valid statusline stdin payload for the given session id.

    Examples:
        >>> _payload("s")["session_id"]
        's'
    """
    return {
        "model": {"id": "claude-sonnet-4-6", "display_name": "Sonnet 4.6"},
        "workspace": {"current_dir": "/tmp/test"},
        "context_window": {"tokens_used": 1000, "tokens_remaining": 9000, "remaining_percentage": 90},
        "cost": {"total_cost_usd": 0.1},
        "session_id": sid,
        "effort": {"level": "normal"},
    }


def _write_agent(
    sid: str,
    agent_id: str,
    *,
    since: str,
    agent_type: str = "foundry:sw-engineer",
    last_active: str | None = None,
) -> None:
    """Write an agents/<id>.json file under the per-session state dir.

    ``last_active`` is included only when provided so tests can exercise both the legacy (since-only) records and the
    refreshed (last_active-bearing) ones.
    """
    d = _hook_state_base() / f"claude-state-{sid}" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    record: dict = {"id": agent_id, "type": agent_type, "model": "opus", "color": None, "since": since}
    if last_active is not None:
        record["last_active"] = last_active
    (d / f"{agent_id}.json").write_text(json.dumps(record), encoding="utf-8")


def _write_tool(
    sid: str,
    tool: str,
    *,
    since: str,
    count: int = 1,
    last: str | None = None,
    inflight: int | None = None,
) -> None:
    """Write a tools/<tool>.json file under the per-session state dir.

    ``last`` and ``inflight`` are written only when provided, so tests can exercise both records written by an older
    hook version (``since`` only) and current ones.
    """
    d = _hook_state_base() / f"claude-state-{sid}" / "tools"
    d.mkdir(parents=True, exist_ok=True)
    record: dict = {"tool": tool, "since": since, "count": count}
    if last is not None:
        record["last"] = last
    if inflight is not None:
        record["inflight"] = inflight
    (d / f"{tool}.json").write_text(json.dumps(record), encoding="utf-8")


def _write_codex(sid: str, tool_use_id: str, *, since: str, verb: str = "review", **fields: object) -> None:
    """Write a codex/<id>.json run record under the per-session state dir, as task-log.js does.

    ``fields`` override or extend the default record (``bg``, ``detached``, ``workspace``, ``job_id``, ``timeout_s``, or
    ``kind`` to write a legacy Skill-level entry).
    """
    d = _hook_state_base() / f"claude-state-{sid}" / "codex"
    d.mkdir(parents=True, exist_ok=True)
    record: dict = {"id": tool_use_id, "kind": "run", "since": since, "type": verb, "bg": False, "detached": False}
    record.update(fields)
    record = {k: v for k, v in record.items() if v is not None}
    (d / f"{tool_use_id}.json").write_text(json.dumps(record), encoding="utf-8")


_JOB_ID = "0b1f6c4e-5d2a-4c8e-9a41-7d3e2f1a8b90"


def _write_report(
    workspace: Path, verb: str, *, started_ago_s: float, run_id: str | None = None, job_id: str | None = None
) -> None:
    """Write the final report the bridge leaves when a run ends: ``result-<finish ns>-<id>.json`` with its start time.

    ``job_id`` marks the report of a detached job, as the bridge does.
    """
    bridge = workspace / ".temp" / "bridge"
    bridge.mkdir(parents=True, exist_ok=True)
    finished_ns = time.time_ns()
    record = {"verb": verb, "run_id": run_id or uuid.uuid4().hex, "started_at": time.time() - started_ago_s}
    if job_id:
        record["job_id"] = job_id
    (bridge / f"result-{finished_ns}-{uuid.uuid4().hex[:8]}.json").write_text(json.dumps(record), encoding="utf-8")


def _write_consumed(sid: str, run_id: str) -> None:
    """Record a finished foreground run's id under codex/, as task-log.js does at its PostToolUse."""
    d = _hook_state_base() / f"claude-state-{sid}" / "codex"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"consumed-{run_id}.json").write_text(json.dumps({"kind": "consumed", "run_id": run_id}), encoding="utf-8")


def _write_job(workspace: Path, status: str) -> None:
    """Write the bridge job record ``<workspace>/.temp/bridge/jobs/<job id>.json`` with the given status."""
    jobs = workspace / ".temp" / "bridge" / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    (jobs / f"{_JOB_ID}.json").write_text(json.dumps({"job_id": _JOB_ID, "status": status}), encoding="utf-8")


def _ago(seconds: float | None) -> str | None:
    """Return the ISO timestamp ``seconds`` before now, or None when no timestamp is wanted.

    Examples:
        >>> (_ago(None), _ago(0) is not None)
        (None, True)
    """
    return None if seconds is None else (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestAgentDisplay:
    """statusline.js: 🤖 segment rendering across empty, active, and stale agents."""

    def test_no_agents_shows_none(self, render) -> None:
        """Empty state directory renders the ``🤖 none`` marker for the agent segment."""
        rendered = render()

        assert "🤖 none" in rendered

    @pytest.mark.parametrize(
        ("agent_id", "agent_type", "expected_label"),
        [
            pytest.param("a1", "foundry:sw-engineer", "sw-engineer", id="a1"),
            pytest.param("audit-17", "oss:shepherd", "shepherd", id="audit-17"),
            pytest.param("tu-cdx-1", "codex:rescue", "rescue", id="tu-cdx-1"),
        ],
    )
    def test_active_agent_shows_type_label(
        self, sid: str, render, agent_id: str, agent_type: str, expected_label: str
    ) -> None:
        """One fresh agent entry renders its short type label in the 🤖 segment."""
        _write_agent(sid, agent_id, since=datetime.now(timezone.utc).isoformat(), agent_type=agent_type)

        rendered = render()

        assert "🤖" in rendered
        assert expected_label in rendered
        assert "🤖 none" not in rendered

    def test_multiple_active_agents_show_total_and_group_counts(self, sid: str, render) -> None:
        """Multiple fresh agents render total count and per-label grouped counts."""
        now = datetime.now(timezone.utc).isoformat()
        _write_agent(sid, "a1", since=now, agent_type="foundry:sw-engineer")
        _write_agent(sid, "a2", since=now, agent_type="foundry:sw-engineer")
        _write_agent(sid, "a3", since=now, agent_type="codex:rescue")

        rendered = render()

        assert "🤖 3" in rendered
        assert "sw-engineer(2)" in rendered
        assert "rescue" in rendered
        assert "🤖 none" not in rendered

    @pytest.mark.parametrize(
        ("since_s", "last_active_s"),
        [
            # Non-worktree agents (plain ``Agent()`` calls, the common case) get no per-agent liveness signal — the tool
            # event payload carries no agent_id, so ``last_active`` is never refreshed for them. They use the longer
            # 30-minute backstop instead of the worktree-only 10-min one, so a genuinely still-working 20-min background
            # task (e.g. a multi-file refactor) is not hidden.
            pytest.param(20 * 60, None, id="non-worktree-past-10-min-cutoff"),
            # Reproduces the reported bug: a 20-min-old dispatch (``since``) would drop under the 10-min filter, but
            # ongoing tool activity keeps ``last_active`` current so the agent must remain in the 🤖 segment.
            pytest.param(20 * 60, 0, id="fresh-last-active-beats-stale-since"),
        ],
    )
    def test_long_running_agent_stays_visible(
        self, sid: str, render, since_s: float, last_active_s: float | None
    ) -> None:
        """An agent older than the old 10-min cutoff stays visible while it is within its liveness backstop.

        Scenario: a non-worktree agent (since-only, no ``last_active``) past 10 min stays visible under the 30-minute
        backstop; a long-running agent with a stale ``since`` but a fresh ``last_active`` also stays visible.
        """
        _write_agent(sid, "a-longrun", since=_ago(since_s), last_active=_ago(last_active_s))

        rendered = render()

        assert "sw-engineer" in rendered
        assert "🤖 none" not in rendered

    @pytest.mark.parametrize(
        ("since_s", "last_active_s"),
        [
            pytest.param(70 * 60, None, id="non-worktree-past-30-min-backstop"),
            # Confirms the refresh path does not defeat the staleness net: a crashed/hung agent that stopped emitting
            # activity has a stale ``last_active`` (older than 10 min) and is still reaped.
            pytest.param(20 * 60, 20 * 60, id="idle-agent-with-stale-last-active"),
        ],
    )
    def test_stale_agent_dropped(self, sid: str, render, since_s: float, last_active_s: float | None) -> None:
        """An agent past its staleness backstop is dropped → ``🤖 none`` rendered.

        Scenario: a non-worktree agent past the 30-min backstop is dropped; an agent whose ``last_active`` is also older
        than 10 min ages out.
        """
        _write_agent(sid, "a-stale", since=_ago(since_s), last_active=_ago(last_active_s))

        rendered = render()

        assert "🤖 none" in rendered


class TestCodexDisplay:
    """statusline.js: 🧠 segment for running Codex bridge calls, separate from agents and tools."""

    def test_no_codex_run_hides_segment(self, render) -> None:
        """With no Codex run active the 🧠 segment is absent, so users without the bridge see no change."""
        assert "🧠" not in render()

    @pytest.mark.parametrize("verb", ["advise", "review", "implement"])
    def test_active_run_shows_verb_label(self, sid: str, render, verb: str) -> None:
        """A fresh foreground run renders ``codex:<verb>`` in 🧠, leaving the 🤖 segment on ``none``.

        A Codex run is neither a subagent nor a native tool, so it must not count as an agent.
        """
        _write_codex(sid, "tu-cdx-1", since=_ago(1), verb=verb)

        rendered = render()

        assert f"🧠 codex:{verb}" in rendered
        assert f"codex:{verb}(bg)" not in rendered
        assert "🤖 none" in rendered

    def test_background_run_is_marked(self, sid: str, render) -> None:
        """A background or detached run renders with a ``(bg)`` suffix."""
        _write_codex(sid, "tu-cdx-bg", since=_ago(1), bg=True)

        assert "codex:review(bg)" in render()

    def test_same_kind_runs_group_with_count(self, sid: str, render) -> None:
        """Two concurrent runs of one verb render once with a multiplier; another verb renders beside it."""
        _write_codex(sid, "tu-cdx-a", since=_ago(1))
        _write_codex(sid, "tu-cdx-b", since=_ago(1))
        _write_codex(sid, "tu-cdx-c", since=_ago(1), verb="advise")

        rendered = render()

        assert "codex:advise" in rendered
        assert "codex:review×2" in rendered

    def test_legacy_skill_level_entry_is_ignored(self, sid: str, render) -> None:
        """A codex file without ``kind: run`` (written by an older hook for a Skill call) never renders.

        Such an entry marked instruction delivery, not a running process, and would otherwise sit until its age cap.
        """
        _write_codex(sid, "tu-cdx-old", since=_ago(1), kind=None)

        assert "🧠" not in render()

    @pytest.mark.parametrize(
        ("verb", "age_s", "timeout_s", "visible"),
        [
            # Without --timeout-seconds the bridge's own default applies:
            # review 300 s → 2 x (1.2 x 300 + 9) + 30 = 768 s, implement 600 s → 1.2 x 600 + 9 + 30 = 759 s,
            # advise 120 s → 2 x (1.2 x 120 + 9) + 30 = 336 s.
            pytest.param("review", 700, None, True, id="review-default-cap-within"),
            pytest.param("review", 800, None, False, id="review-default-cap-exceeded"),
            pytest.param("implement", 700, None, True, id="implement-default-cap-within"),
            pytest.param("implement", 800, None, False, id="implement-default-cap-exceeded"),
            pytest.param("advise", 300, None, True, id="advise-default-cap-within"),
            pytest.param("advise", 400, None, False, id="advise-default-cap-exceeded"),
            # review with --timeout-seconds 60: 2 x (1.2 x 60 + 9) + 30 = 192 s
            pytest.param("review", 150, 60, True, id="timeout-cap-within"),
            pytest.param("review", 300, 60, False, id="timeout-cap-exceeded"),
            pytest.param("review", 30 * 60, 3600, True, id="long-timeout-extends-cap"),
        ],
    )
    def test_background_run_drops_after_age_cap(
        self, sid: str, render, verb: str, age_s: int, timeout_s: int | None, visible: bool
    ) -> None:
        """A background run past the cap derived from its ``--timeout-seconds`` or the verb default is dropped.

        Background Bash has no completion hook, so this cap is what clears a finished background run; it must not cut a
        still-running default-budget run short.
        """
        _write_codex(sid, "tu-cdx-age", since=_ago(age_s), verb=verb, bg=True, timeout_s=timeout_s)

        assert ("🧠" in render()) is visible

    @pytest.mark.parametrize(
        ("age_s", "visible"),
        [
            pytest.param(500, True, id="within-foreground-ceiling"),
            pytest.param(700, False, id="past-foreground-ceiling"),
        ],
    )
    def test_foreground_run_is_bounded_by_the_foreground_bash_ceiling(
        self, sid: str, render, age_s: int, visible: bool
    ) -> None:
        """A foreground run older than the 10-min foreground Bash ceiling is dropped even with a longer timeout.

        A call denied or interrupted before PostToolUse leaves its entry behind; no live foreground call lasts longer
        than the ceiling, so the entry cannot show for the full worst-case cap of its timeout.
        """
        _write_codex(sid, "tu-cdx-fg", since=_ago(age_s), timeout_s=350)

        assert ("🧠" in render()) is visible

    def test_detached_run_past_default_cap_stays_while_job_is_running(self, sid: str, render, tmp_path: Path) -> None:
        """A detached run whose job record still says ``running`` stays visible at its default-budget worst case."""
        workspace = tmp_path / "ws"
        _write_job(workspace, "running")
        _write_codex(
            sid,
            "tu-cdx-long",
            since=_ago(11 * 60),
            verb="implement",
            bg=True,
            detached=True,
            workspace=str(workspace),
            job_id=_JOB_ID,
        )

        assert "codex:implement(bg)" in render()

    @pytest.mark.parametrize(
        ("status", "visible"),
        [
            pytest.param("queued", True, id="queued"),
            pytest.param("running", True, id="running"),
            pytest.param("cancel_requested", True, id="cancel-requested"),
            pytest.param("finished", False, id="finished"),
            pytest.param("failed", False, id="failed"),
            pytest.param("cancelled", False, id="cancelled"),
        ],
    )
    def test_detached_run_follows_job_status(
        self, sid: str, render, tmp_path: Path, status: str, visible: bool
    ) -> None:
        """A detached run stays visible while its job record is live and drops once the record is terminal.

        The job record is written by the bridge supervisor, so it ends the entry well before the age cap.
        """
        workspace = tmp_path / "ws"
        _write_job(workspace, status)
        _write_codex(sid, "tu-cdx-job", since=_ago(5), bg=True, detached=True, workspace=str(workspace), job_id=_JOB_ID)

        assert ("🧠" in render()) is visible

    @pytest.mark.parametrize(
        ("report_verb", "report_started_ago_s", "report_run_id", "run_id", "visible"),
        [
            pytest.param("review", 6, None, None, False, id="report-after-start-drops-run"),
            pytest.param("advise", 6, None, None, True, id="other-verb-report-keeps-run"),
            pytest.param("review", 60, None, None, True, id="report-of-an-earlier-run-keeps-run"),
            pytest.param("review", 6, "run-a", "run-a", False, id="matching-run-id-drops-run"),
            pytest.param("review", 6, "run-b", "run-a", True, id="other-run-id-keeps-run"),
        ],
    )
    def test_background_run_drops_when_the_bridge_report_appears(
        self,
        sid: str,
        render,
        tmp_path: Path,
        report_verb: str,
        report_started_ago_s: int,
        report_run_id: str | None,
        run_id: str | None,
        visible: bool,
    ) -> None:
        """A background run drops once the bridge's final report for it lands in the run's workspace.

        Background Bash has no completion hook; the bridge writes ``result-<finish ns>-<id>.json`` when Codex ends, so
        the report (matched by verb and start time, or exactly by ``--run-id``) is the end-of-run signal.
        """
        workspace = tmp_path / "ws"
        _write_report(workspace, report_verb, started_ago_s=report_started_ago_s, run_id=report_run_id)
        _write_codex(sid, "tu-cdx-rep", since=_ago(10), bg=True, workspace=str(workspace), run_id=run_id)

        assert ("🧠" in render()) is visible

    def test_one_report_ends_only_one_of_two_concurrent_runs(self, sid: str, render, tmp_path: Path) -> None:
        """Two background runs of one verb in a workspace with one final report leave one run showing."""
        workspace = tmp_path / "ws"
        _write_report(workspace, "review", started_ago_s=6)
        _write_codex(sid, "tu-cdx-r1", since=_ago(10), bg=True, workspace=str(workspace))
        _write_codex(sid, "tu-cdx-r2", since=_ago(9), bg=True, workspace=str(workspace))

        assert "codex:review(bg)" in render()
        assert "×2" not in render()

    def test_report_of_a_finished_foreground_run_does_not_end_a_background_run(
        self, sid: str, render, tmp_path: Path
    ) -> None:
        """A foreground run's own report, left in the shared workspace, never ends a still-running background run.

        task-log.js records the finished foreground run's id; its report started after the background launch, so
        matching on verb and start time alone would drop the background run at once.
        """
        workspace = tmp_path / "ws"
        _write_report(workspace, "review", started_ago_s=8, run_id="fg-run-1")
        _write_consumed(sid, "fg-run-1")
        _write_codex(sid, "tu-cdx-bgrun", since=_ago(20), bg=True, workspace=str(workspace))

        assert "codex:review(bg)" in render()

    def test_report_of_a_detached_job_does_not_end_a_background_run(self, sid: str, render, tmp_path: Path) -> None:
        """A report carrying ``job_id`` belongs to a detached job, so it cannot end a background Bash run."""
        workspace = tmp_path / "ws"
        _write_report(workspace, "implement", started_ago_s=8, job_id=_JOB_ID)
        _write_codex(sid, "tu-cdx-bgimp", since=_ago(20), verb="implement", bg=True, workspace=str(workspace))

        assert "codex:implement(bg)" in render()

    def test_expired_run_keeps_its_own_report(self, sid: str, render, tmp_path: Path) -> None:
        """A run past its age cap still owns its report, so a longer-lived run is not charged for it.

        Two background reviews share a workspace; the short-timeout one is long over and its report started right after
        its launch, so the long-timeout run must stay visible.
        """
        workspace = tmp_path / "ws"
        _write_report(workspace, "review", started_ago_s=128)
        _write_codex(sid, "tu-cdx-long", since=_ago(130), bg=True, timeout_s=300, workspace=str(workspace))
        _write_codex(sid, "tu-cdx-short", since=_ago(129), bg=True, timeout_s=30, workspace=str(workspace))

        assert "codex:review(bg)" in render()

    @pytest.mark.parametrize(
        ("content", "visible"),
        [
            pytest.param(None, True, id="no-output-file-yet"),
            pytest.param("", True, id="empty-output-file"),
            pytest.param("Warning: retention maintenance skipped\n", True, id="early-stderr-keeps-run"),
            pytest.param(
                "usage: bridge_call.py: error: unrecognized arguments: .\n\n[exited with code 2]\n",
                False,
                id="launch-error-exit-marker",
            ),
            pytest.param('{"run_id": "r"}\n\n[exited with code 0]\n', False, id="envelope-and-exit-marker"),
            pytest.param("\n[killed]\n", False, id="stopped-task-marker"),
            pytest.param(
                '{"findings": ["[killed] and [exited with code 1] quoted"]}', True, id="marker-text-inside-output"
            ),
        ],
    )
    def test_background_run_follows_the_host_task_output_file(
        self, sid: str, render, tmp_path: Path, content: str | None, visible: bool
    ) -> None:
        """A background run drops once the host's task output file carries its exit marker, even with no report.

        The host creates the file at launch and ends it with ``[exited with code N]``; a launch that died on an argument
        error never writes a report, so the marker is the only end signal, and other output (a stderr warning at start)
        must not end the run.
        """
        output = tmp_path / "tasks" / "bt1.output"
        output.parent.mkdir()
        if content is not None:
            output.write_text(content, encoding="utf-8")
        _write_codex(
            sid, "tu-cdx-out", since=_ago(10), bg=True, workspace=str(tmp_path / "ws"), output_files=[str(output)]
        )

        assert ("codex:review(bg)" in render()) is visible

    def test_report_never_ends_a_run_whose_output_file_is_still_open(self, sid: str, render, tmp_path: Path) -> None:
        """A run whose host output file has no exit marker keeps showing, whatever reports sit in the workspace.

        Another session's report of the same verb, started just after this launch, would otherwise end a live run.
        """
        workspace = tmp_path / "ws"
        output = tmp_path / "tasks" / "bt2.output"
        output.parent.mkdir()
        output.write_text("", encoding="utf-8")
        _write_report(workspace, "review", started_ago_s=9.5)
        _write_codex(sid, "tu-cdx-live", since=_ago(10), bg=True, workspace=str(workspace), output_files=[str(output)])

        assert "codex:review(bg)" in render()

    def test_ended_run_marks_its_report_as_consumed(self, sid: str, render, tmp_path: Path) -> None:
        """The run id in a finished run's output file keeps its report away from a run with no output file.

        The unlocated run launched next to it must keep showing; the finished run's own report is not its end signal.
        """
        workspace = tmp_path / "ws"
        output = tmp_path / "tasks" / "bt3.output"
        output.parent.mkdir()
        run_id = "38479f58-97c3-4b0e-8f61-2a1d5c7e9b04"
        output.write_text(json.dumps({"run_id": run_id}) + "\n\n[exited with code 0]\n", encoding="utf-8")
        _write_report(workspace, "review", started_ago_s=9.8, run_id=run_id)
        _write_codex(sid, "tu-cdx-done", since=_ago(10), bg=True, workspace=str(workspace), output_files=[str(output)])
        _write_codex(sid, "tu-cdx-unlocated", since=_ago(10), bg=True, workspace=str(workspace))

        line = render()
        assert "codex:review(bg)" in line
        assert "×2" not in line

    def test_report_that_started_before_the_launch_stamp_goes_to_its_own_run(
        self, sid: str, render, tmp_path: Path
    ) -> None:
        """A report is charged to the run whose launch stamp is nearest, even when the report started just before it.

        The launch is stamped at PostToolUse, after the bridge has already started, so the owner's lag is slightly
        negative. The earlier, still-running run is 0.6 s away and must not take the report.
        """
        workspace = tmp_path / "ws"
        _write_report(workspace, "review", started_ago_s=128)
        _write_codex(sid, "tu-cdx-run-a", since=_ago(129.2), bg=True, timeout_s=300, workspace=str(workspace))
        _write_codex(sid, "tu-cdx-run-b", since=_ago(127.95), bg=True, timeout_s=30, workspace=str(workspace))

        assert "codex:review(bg)" in render()

    def test_launches_too_close_to_tell_apart_are_not_ended_by_one_report(
        self, sid: str, render, tmp_path: Path
    ) -> None:
        """Two same-verb runs launched within half a second of a report's start both keep showing.

        Charging the report to either one could hide the run that is still going; each then waits for its age cap.
        """
        workspace = tmp_path / "ws"
        _write_report(workspace, "review", started_ago_s=9.9)
        _write_codex(sid, "tu-cdx-twin-a", since=_ago(10), bg=True, workspace=str(workspace))
        _write_codex(sid, "tu-cdx-twin-b", since=_ago(9.8), bg=True, workspace=str(workspace))

        assert "codex:review(bg)×2" in render()

    @pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt", reason="needs unprivileged symlinks")
    def test_one_workspace_spelled_two_ways_is_one_group(self, sid: str, render, tmp_path: Path) -> None:
        """Runs recorded under a workspace and under a symlink to it share one set of reports.

        Grouping by the raw path would let a single report end one run in each group.
        """
        workspace = tmp_path / "ws"
        workspace.mkdir()
        alias = tmp_path / "alias"
        alias.symlink_to(workspace, target_is_directory=True)
        _write_report(workspace, "review", started_ago_s=9.9)
        _write_codex(sid, "tu-cdx-real", since=_ago(10), bg=True, workspace=str(workspace))
        _write_codex(sid, "tu-cdx-alias", since=_ago(5), bg=True, workspace=str(alias))

        line = render()
        assert "codex:review(bg)" in line
        assert "×2" not in line

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX named pipes")
    @pytest.mark.parametrize("name", ["report", "job", "output"])
    def test_symlinked_fifo_never_blocks_the_renderer(self, sid: str, render, tmp_path: Path, name: str) -> None:
        """A symlink to a named pipe where a report, job record or output file is expected is skipped, not read.

        Workspace files come from the project; opening a pipe for reading would block the status line forever.
        """
        fifo = tmp_path / "pipe"
        os.mkfifo(fifo)
        workspace = tmp_path / "ws"
        bridge = workspace / ".temp" / "bridge"
        (bridge / "jobs").mkdir(parents=True)
        fields: dict = {"workspace": str(workspace)}
        if name == "report":
            (bridge / "result-99999999999999999999-abcdef12.json").symlink_to(fifo)
        elif name == "job":
            (bridge / "jobs" / f"{_JOB_ID}.json").symlink_to(fifo)
            fields.update(detached=True, job_id=_JOB_ID)
        else:
            (tmp_path / "tasks").mkdir()
            (tmp_path / "tasks" / "bt4.output").symlink_to(fifo)
            fields["output_files"] = [str(tmp_path / "tasks" / "bt4.output")]
        _write_codex(sid, "tu-cdx-pipe", since=_ago(10), bg=True, **fields)

        assert "🧠" in render()

    @pytest.mark.parametrize(
        ("age_s", "visible"),
        [pytest.param(600, True, id="past-derived-cap"), pytest.param(3700, False, id="past-ceiling")],
    )
    def test_run_with_an_open_output_file_outlives_its_derived_cap(
        self, sid: str, render, tmp_path: Path, age_s: int, visible: bool
    ) -> None:
        """A run whose output file has no closing marker is bounded by the 60-minute ceiling, not its derived cap.

        The host closes the file when the task exits or is stopped, so the cap is only a backstop for a missing file.
        """
        output = tmp_path / "tasks" / "bt5.output"
        output.parent.mkdir()
        output.write_text("", encoding="utf-8")
        _write_codex(
            sid,
            "tu-cdx-open",
            since=_ago(age_s),
            bg=True,
            timeout_s=30,
            workspace=str(tmp_path / "ws"),
            output_files=[str(output)],
        )

        assert ("codex:review(bg)" in render()) is visible

    def test_detached_run_with_a_live_job_survives_the_supervisor_start_slack(
        self, sid: str, render, tmp_path: Path
    ) -> None:
        """A detached run whose job is still running stays visible a little past its derived age cap.

        The supervisor can take up to a minute to start before the budget begins, so the cap alone would hide a full-
        budget run just before it finishes.
        """
        workspace = tmp_path / "ws"
        _write_job(workspace, "running")
        _write_codex(
            sid,
            "tu-cdx-slack",
            since=_ago(100),
            verb="implement",
            bg=True,
            detached=True,
            timeout_s=10,
            workspace=str(workspace),
            job_id=_JOB_ID,
        )

        assert "codex:implement(bg)" in render()

    def test_detached_run_with_pruned_job_record_is_dropped(self, sid: str, render, tmp_path: Path) -> None:
        """A detached run whose job record no longer exists is over, so it is dropped."""
        _write_codex(
            sid, "tu-cdx-gone", since=_ago(5), bg=True, detached=True, workspace=str(tmp_path / "ws"), job_id=_JOB_ID
        )

        assert "🧠" not in render()

    def test_detached_run_without_job_id_uses_age_cap(self, sid: str, render, tmp_path: Path) -> None:
        """A detached run whose launcher output gave no job id stays visible until the age cap."""
        _write_codex(sid, "tu-cdx-nojob", since=_ago(5), bg=True, detached=True, workspace=str(tmp_path / "ws"))

        assert "codex:review(bg)" in render()


class TestToolDisplay:
    """statusline.js: 🛠️ segment freshness — ``last`` beats ``since``, inflight survives the 30s window."""

    def test_no_tools_shows_none(self, render) -> None:
        """Empty state directory renders ``🛠️ none`` — the correct display at an idle prompt.

        task-log.js clears ``tools/`` at Stop and UserPromptSubmit, so the segment is per-turn by design.
        """
        assert "🛠️ none" in render()

    @pytest.mark.parametrize(
        ("tool", "since_s", "last_s", "count", "inflight", "expected"),
        [
            pytest.param("Bash", 0, 0, 3, 0, "Bash:3x", id="recent-call"),
            # Regression guard: filtering on ``since`` (a FIXED window start) blanked the segment during active work.
            pytest.param("Bash", 40, 5, 7, 0, "Bash:7x", id="fresh-last-survives-stale-window-start"),
            pytest.param("Bash", 4 * 60, 4 * 60, 1, 1, "Bash:1x", id="inflight-call-past-window"),
            pytest.param("Read", 0, None, 4, None, "Read:4x", id="legacy-record-without-last-uses-since"),
        ],
    )
    def test_visible_call_shows_type_and_count(
        self,
        sid: str,
        render,
        tool: str,
        since_s: float,
        last_s: float | None,
        count: int,
        inflight: int | None,
        expected: str,
    ) -> None:
        """A tool still within its freshness rule renders ``<tool>:<count>x``.

        Scenario: a tool called just now renders; a call 5 s ago inside a window opened 40 s ago stays visible; a single
        slow call still in flight after 4 min keeps its type displayed; a record written by an older hook version (no
        ``last``) still renders off ``since``.
        """
        _write_tool(sid, tool, since=_ago(since_s), last=_ago(last_s), count=count, inflight=inflight)

        assert expected in render()

    @pytest.mark.parametrize(
        ("age_s", "count", "inflight"),
        [
            pytest.param(90, 2, 0, id="finished-call-past-30s-window"),
            pytest.param(20 * 60, 1, 1, id="leaked-inflight-past-10-min-cap"),
        ],
    )
    def test_expired_call_dropped(self, sid: str, render, age_s: float, count: int, inflight: int) -> None:
        """A returned call older than 30 s, or an inflight slot leaked past the 10-min cap, is dropped.

        Scenario: a returned call (inflight 0) older than 30 s is dropped; an inflight slot leaked by a missing
        PostToolUse is dropped past the 10-min cap.
        """
        _write_tool(sid, "Bash", since=_ago(age_s), last=_ago(age_s), count=count, inflight=inflight)

        assert "Bash" not in render()

    def test_task_log_write_renders_end_to_end(self, sid: str, tmp_home: Path, run_hook, render) -> None:
        """State written by the real ``task-log.js`` renders in the real ``statusline.js``.

        Closes the writer/reader seam: every other test in this class seeds ``tools/`` by hand, so a field renamed on
        one side only would pass them all.
        """
        (tmp_home / ".claude" / "logs").mkdir(parents=True, exist_ok=True)
        pre = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "echo hi"},
            "tool_use_id": "tu-e2e",
            "session_id": sid,
        }
        assert run_hook("task-log.js", pre, home=tmp_home).returncode == 0

        assert "Bash:1x" in render()

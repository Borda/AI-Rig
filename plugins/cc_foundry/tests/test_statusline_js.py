"""Subprocess tests for ``hooks/statusline.js``.

The hook is a status-line renderer: it consumes a JSON payload on stdin and writes a two-line ANSI string to stdout.
State for the segments lives under ``<hook temp base>/claude-state-<session_id>/`` and is written by ``task-log.js`` at
runtime. These tests seed that state directly, then assert against ``statusline.js`` stdout.

Line 2 format: ``⚡ <skills> │ 🤖 <agents> │ 🛠️ <tools>``. Agents (including codex:* types) appear in the ``🤖`` segment.
Assertions strip ANSI escape sequences before substring matching so the rendered marker and label co-occurrence is
testable irrespective of color wrapping.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from _hook_env import _hook_tmp_base

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
def _sid(tmp_path: Path) -> Iterator[str]:
    """Yield a unique session id; clean its ``claude-state-<id>`` dir on teardown.

    Base resolved via ``_hook_tmp_base()`` so teardown targets the same directory the hook's ``getSentinelDir()`` writes
    on this platform.
    """
    s = f"pytest-{tmp_path.name}"
    yield s
    shutil.rmtree(_hook_tmp_base() / f"claude-state-{s}", ignore_errors=True)


@pytest.fixture(name="tmp_home")
def _tmp_home(tmp_path: Path) -> Path:
    """Return an isolated HOME with an empty subscription.json so reads succeed."""
    h = tmp_path / "home"
    sub_dir = h / ".claude" / "state"
    sub_dir.mkdir(parents=True)
    (sub_dir / "subscription.json").write_text("{}", encoding="utf-8")
    return h


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
    d = _hook_tmp_base() / f"claude-state-{sid}" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    record: dict = {"id": agent_id, "type": agent_type, "model": "opus", "color": None, "since": since}
    if last_active is not None:
        record["last_active"] = last_active
    (d / f"{agent_id}.json").write_text(json.dumps(record), encoding="utf-8")


def _write_codex(sid: str, tool_use_id: str, *, since: str) -> None:
    """Write a codex/<id>.json file under the per-session state dir."""
    d = _hook_tmp_base() / f"claude-state-{sid}" / "codex"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{tool_use_id}.json").write_text(
        json.dumps({"id": tool_use_id, "since": since, "type": "rescue"}),
        encoding="utf-8",
    )


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestAgentDisplay:
    """statusline.js: 🤖 segment rendering across empty, active, and stale agents."""

    def test_no_agents_shows_none(self, sid: str, tmp_home: Path, run_hook) -> None:
        """Empty state directory renders the ``🤖 none`` marker for the agent segment."""
        result = run_hook("statusline.js", _payload(sid), home=tmp_home)

        assert result.returncode == 0, result.stderr
        rendered = _strip_ansi(result.stdout)
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
        self, sid: str, tmp_home: Path, run_hook, agent_id: str, agent_type: str, expected_label: str
    ) -> None:
        """One fresh agent entry renders its short type label in the 🤖 segment."""
        _write_agent(sid, agent_id, since=datetime.now(timezone.utc).isoformat(), agent_type=agent_type)

        result = run_hook("statusline.js", _payload(sid), home=tmp_home)

        assert result.returncode == 0, result.stderr
        rendered = _strip_ansi(result.stdout)
        assert "🤖" in rendered
        assert expected_label in rendered
        assert "🤖 none" not in rendered

    def test_multiple_active_agents_show_total_and_group_counts(self, sid: str, tmp_home: Path, run_hook) -> None:
        """Multiple fresh agents render total count and per-label grouped counts."""
        now = datetime.now(timezone.utc).isoformat()
        _write_agent(sid, "a1", since=now, agent_type="foundry:sw-engineer")
        _write_agent(sid, "a2", since=now, agent_type="foundry:sw-engineer")
        _write_agent(sid, "a3", since=now, agent_type="codex:rescue")

        result = run_hook("statusline.js", _payload(sid), home=tmp_home)

        assert result.returncode == 0, result.stderr
        rendered = _strip_ansi(result.stdout)
        assert "🤖 3" in rendered
        assert "sw-engineer(2)" in rendered
        assert "rescue" in rendered
        assert "🤖 none" not in rendered

    def test_non_worktree_agent_past_10_min_stays_visible(self, sid: str, tmp_home: Path, run_hook) -> None:
        """A non-worktree agent (since-only, no ``last_active``) past the old 10-min cutoff stays visible.

        Non-worktree agents (plain ``Agent()`` calls, the common case) get no per-agent liveness signal — the tool event
        payload carries no agent_id, so ``last_active`` is never refreshed for them. They use the longer 30-minute
        backstop instead of the worktree-only 10-min one, so a genuinely still-working 20-min background task (e.g. a
        multi-file refactor) is not hidden.
        """
        stale = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        _write_agent(sid, "a-longrun", since=stale)

        result = run_hook("statusline.js", _payload(sid), home=tmp_home)

        assert result.returncode == 0, result.stderr
        rendered = _strip_ansi(result.stdout)
        assert "sw-engineer" in rendered
        assert "🤖 none" not in rendered

    def test_non_worktree_agent_dropped_after_30_min(self, sid: str, tmp_home: Path, run_hook) -> None:
        """A non-worktree agent past the 30-min backstop is dropped → ``🤖 none`` rendered."""
        stale = (datetime.now(timezone.utc) - timedelta(minutes=70)).isoformat()
        _write_agent(sid, "a-stale", since=stale)

        result = run_hook("statusline.js", _payload(sid), home=tmp_home)

        assert result.returncode == 0, result.stderr
        rendered = _strip_ansi(result.stdout)
        assert "🤖 none" in rendered

    def test_long_running_agent_kept_visible_by_last_active(self, sid: str, tmp_home: Path, run_hook) -> None:
        """A long-running agent with a stale ``since`` but a fresh ``last_active`` stays visible.

        Reproduces the reported bug: a 20-min-old dispatch (``since``) would drop under the
        10-min filter, but ongoing tool activity keeps ``last_active`` current so the agent
        must remain in the 🤖 segment.
        """
        stale_since = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        fresh_active = datetime.now(timezone.utc).isoformat()
        _write_agent(sid, "a-longrun", since=stale_since, last_active=fresh_active)

        result = run_hook("statusline.js", _payload(sid), home=tmp_home)

        assert result.returncode == 0, result.stderr
        rendered = _strip_ansi(result.stdout)
        assert "sw-engineer" in rendered
        assert "🤖 none" not in rendered

    def test_idle_agent_with_stale_last_active_dropped(self, sid: str, tmp_home: Path, run_hook) -> None:
        """An agent whose ``last_active`` is also older than 10 min ages out (backstop preserved).

        Confirms the refresh path does not defeat the staleness net: a crashed/hung agent that
        stopped emitting activity has a stale ``last_active`` and is still reaped.
        """
        stale = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        _write_agent(sid, "a-dead", since=stale, last_active=stale)

        result = run_hook("statusline.js", _payload(sid), home=tmp_home)

        assert result.returncode == 0, result.stderr
        rendered = _strip_ansi(result.stdout)
        assert "🤖 none" in rendered


class TestCodexDisplay:
    """statusline.js: codex:* agents shown in 🤖 segment via agents/ dir."""

    def test_no_codex_shows_none(self, sid: str, tmp_home: Path, run_hook) -> None:
        """Empty agents directory renders the ``🤖 none`` marker."""
        result = run_hook("statusline.js", _payload(sid), home=tmp_home)

        assert result.returncode == 0, result.stderr
        rendered = _strip_ansi(result.stdout)
        assert "🤖 none" in rendered

    def test_active_codex_agent_shows_type_label(self, sid: str, tmp_home: Path, run_hook) -> None:
        """Codex:rescue agent in agents/ renders its short type label in the 🤖 segment."""
        _write_agent(sid, "tu-cdx-1", since=datetime.now(timezone.utc).isoformat(), agent_type="codex:rescue")

        result = run_hook("statusline.js", _payload(sid), home=tmp_home)

        assert result.returncode == 0, result.stderr
        rendered = _strip_ansi(result.stdout)
        assert "🤖" in rendered
        assert "rescue" in rendered

    def test_active_codex_dir_agent_shows_label(self, sid: str, tmp_home: Path, run_hook) -> None:
        """Codex agent tracked in the codex/ dir (not agents/) still renders in the 🤖 segment."""
        _write_codex(sid, "tu-cdx-dir", since=datetime.now(timezone.utc).isoformat())

        result = run_hook("statusline.js", _payload(sid), home=tmp_home)

        assert result.returncode == 0, result.stderr
        rendered = _strip_ansi(result.stdout)
        assert "🤖" in rendered
        assert "rescue" in rendered
        assert "🤖 none" not in rendered

    def test_stale_codex_dir_agent_dropped(self, sid: str, tmp_home: Path, run_hook) -> None:
        """Codex/ entry (non-worktree, since-only) older than the 30-min backstop is dropped → ``🤖 none`` rendered."""
        stale = (datetime.now(timezone.utc) - timedelta(minutes=70)).isoformat()
        _write_codex(sid, "tu-cdx-stale", since=stale)

        result = run_hook("statusline.js", _payload(sid), home=tmp_home)

        assert result.returncode == 0, result.stderr
        rendered = _strip_ansi(result.stdout)
        assert "🤖 none" in rendered

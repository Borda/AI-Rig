"""Subprocess tests for ``hooks/task-log.js``.

The hook is invoked as a Node.js child process with a JSON payload on
stdin and asserted against the per-session state directory written
under ``<hook temp base>/claude-state-<session_id>``. No JS is mocked — each test
spawns the real script via ``node`` and inspects filesystem effects.

Three behavioural areas are covered:

* **Agent lifecycle** — ``PreToolUse`` / ``PostToolUse`` for ``Agent()``
  plus ``SubagentStart`` / ``SubagentStop`` events.
* **Codex tracking** — ``Skill(bridge:*)`` creates a codex file consumed
  by the status line and cleaned up on ``PostToolUse``.
* **Tool counting** — non-Agent ``PreToolUse`` increments a per-tool
  counter file used by the status line's tool-activity segment.

Contract notes
--------------
This hook version writes the ``pending/<tool_use_id>.json`` cache on
``PreToolUse(Agent)`` and **consumes** it on ``SubagentStart`` (or
leaves it for ``SessionEnd`` to wipe). ``PostToolUse(Agent)`` deletes
**both** the ``agents/`` tracking file **and** the ``pending/`` marker so
that ``SubagentStart`` (which may fire after ``PostToolUse`` for
background agents) always creates a fresh ``agents/<agent_id>.json``
rather than silently skipping the write because it found a stale pending
entry.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest
from _hook_env import _hook_tmp_base

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
    """Return an isolated HOME so audit-log writes don't pollute the real user dir."""
    h = tmp_path / "home"
    (h / ".claude" / "logs").mkdir(parents=True)
    return h


# ── Payload helpers (module-level, not fixtures) ─────────────────────────────


def _pre_agent(
    sid: str,
    tool_use_id: str,
    subagent_type: str = "foundry:sw-engineer",
    run_in_background: bool = False,
    name: str | None = None,
) -> dict:
    """Build a ``PreToolUse`` payload for an ``Agent()`` tool call.

    Examples:
        >>> _pre_agent("s", "u", run_in_background=True)["tool_input"]["run_in_background"]
        True
    """
    tool_input: dict = {"subagent_type": subagent_type, "description": "x", "prompt": "p"}
    if run_in_background:
        tool_input["run_in_background"] = True
    if name is not None:
        tool_input["name"] = name
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Agent",
        "tool_input": tool_input,
        "tool_use_id": tool_use_id,
        "session_id": sid,
    }


def _post_agent(sid: str, tool_use_id: str) -> dict:
    """Build a ``PostToolUse`` payload for an ``Agent()`` tool call.

    Mirrors the live payload where PostToolUse's ``tool_input`` omits ``run_in_background`` — the hook must recover
    background-ness from the ``pending/`` marker written at PreToolUse.

    Examples:
        >>> "run_in_background" in _post_agent("s", "u")["tool_input"]
        False
    """
    return {
        "hook_event_name": "PostToolUse",
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "foundry:sw-engineer"},
        "tool_use_id": tool_use_id,
        "session_id": sid,
    }


def _subagent_start(
    sid: str,
    agent_id: str,
    agent_type: str = "foundry:sw-engineer",
    tool_use_id: str | None = None,
) -> dict:
    """Build a ``SubagentStart`` payload.

    Examples:
        >>> _subagent_start("s", "a", tool_use_id="u")["tool_use_id"]
        'u'
    """
    payload: dict = {
        "hook_event_name": "SubagentStart",
        "agent_id": agent_id,
        "agent_type": agent_type,
        "session_id": sid,
    }
    if tool_use_id is not None:
        payload["tool_use_id"] = tool_use_id
    return payload


def _subagent_stop(sid: str, agent_id: str) -> dict:
    """Build a ``SubagentStop`` payload.

    Examples:
        >>> _subagent_stop("s", "a")["hook_event_name"]
        'SubagentStop'
    """
    return {
        "hook_event_name": "SubagentStop",
        "agent_id": agent_id,
        "session_id": sid,
    }


def _pre_skill(sid: str, tool_use_id: str, skill: str) -> dict:
    """Build a ``PreToolUse`` payload for a ``Skill()`` call.

    Examples:
        >>> _pre_skill("s", "u", "audit")["tool_input"]["skill"]
        'audit'
    """
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Skill",
        "tool_input": {"skill": skill, "args": ""},
        "tool_use_id": tool_use_id,
        "session_id": sid,
    }


def _post_skill(sid: str, tool_use_id: str, skill: str) -> dict:
    """Build a ``PostToolUse`` payload for a ``Skill()`` call.

    Examples:
        >>> _post_skill("s", "u", "audit")["hook_event_name"]
        'PostToolUse'
    """
    return {
        "hook_event_name": "PostToolUse",
        "tool_name": "Skill",
        "tool_input": {"skill": skill, "args": ""},
        "tool_use_id": tool_use_id,
        "session_id": sid,
    }


def _pre_bash(sid: str, tool_use_id: str) -> dict:
    """Build a ``PreToolUse`` payload for a ``Bash`` call.

    Examples:
        >>> _pre_bash("s", "u")["tool_name"]
        'Bash'
    """
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "echo hi"},
        "tool_use_id": tool_use_id,
        "session_id": sid,
    }


def _post_bash(sid: str, tool_use_id: str, *, failed: bool = False) -> dict:
    """Build a ``PostToolUse`` (or ``PostToolUseFailure``) payload for a ``Bash`` call.

    Examples:
        >>> _post_bash("s", "u", failed=True)["hook_event_name"]
        'PostToolUseFailure'
    """
    return {
        "hook_event_name": "PostToolUseFailure" if failed else "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "echo hi"},
        "tool_use_id": tool_use_id,
        "session_id": sid,
    }


def _pre_tool_with_cwd(sid: str, tool_use_id: str, cwd: str, tool_name: str = "Read") -> dict:
    """Build a PreToolUse payload carrying a ``cwd`` (a subagent's worktree working dir).

    Mirrors the live CC hook contract where PreToolUse payloads include ``cwd`` but no ``agent_id`` — the field
    ``touchAgentLastActive`` uses to attribute worktree activity.

    Examples:
        >>> _pre_tool_with_cwd("s", "u", "/work")["cwd"]
        '/work'
    """
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": {"file_path": "/x"} if tool_name == "Read" else {"command": "echo hi"},
        "tool_use_id": tool_use_id,
        "session_id": sid,
        "cwd": cwd,
    }


def _pre_compact(sid: str, transcript_path: str) -> dict:
    """Build a ``PreCompact`` payload pointing at a transcript file.

    Examples:
        >>> _pre_compact("s", "t.jsonl")["transcript_path"]
        't.jsonl'
    """
    return {
        "hook_event_name": "PreCompact",
        "transcript_path": transcript_path,
        "session_id": sid,
    }


def _write_agent_file(state_dir, sid: str, agent_id: str) -> None:
    """Seed an agents/<agent_id>.json record with a fixed dispatch time and no last_active.

    The fixed ``since`` (2000-01-01) lets a test assert it is preserved untouched by a refresh.
    """
    d = state_dir(sid) / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{agent_id}.json").write_text(
        json.dumps(
            {
                "id": agent_id,
                "type": "foundry:sw-engineer",
                "model": "inherit",
                "color": None,
                "since": "2000-01-01T00:00:00.000Z",
            }
        ),
        encoding="utf-8",
    )


def _write_transcript(path: Path, file_paths: list[str]) -> None:
    """Write a minimal JSONL transcript with Write tool_use blocks for ``file_paths``."""
    lines = []
    for fp in file_paths:
        block = {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "Write", "input": {"file_path": fp}}]},
        }
        lines.append(json.dumps(block))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestAgentLifecycle:
    """task-log.js: PreToolUse / PostToolUse / SubagentStart / SubagentStop for Agent()."""

    def test_pre_tool_use_creates_agents_and_pending(self, sid: str, tmp_home: Path, run_hook, state_dir) -> None:
        """PreToolUse for Agent() writes both agents/<tool_use_id>.json and pending/<tool_use_id>.json."""
        tool_use_id = "tu-create"

        result = run_hook("task-log.js", _pre_agent(sid, tool_use_id), home=tmp_home)

        assert result.returncode == 0, result.stderr
        assert (state_dir(sid) / "agents" / f"{tool_use_id}.json").exists()
        assert (state_dir(sid) / "pending" / f"{tool_use_id}.json").exists()

    def test_post_tool_use_deletes_agents_and_pending(self, sid: str, tmp_home: Path, run_hook, state_dir) -> None:
        """PostToolUse for Agent() deletes both agents/ and pending/ markers.

        Both files must be removed so that a later SubagentStart (which fires after PostToolUse for background agents)
        does not find a stale pending entry and silently skip writing its own agents/<agent_id>.json.
        """
        tool_use_id = "tu-delete"
        run_hook("task-log.js", _pre_agent(sid, tool_use_id), home=tmp_home)
        assert (state_dir(sid) / "agents" / f"{tool_use_id}.json").exists()
        assert (state_dir(sid) / "pending" / f"{tool_use_id}.json").exists()

        result = run_hook("task-log.js", _post_agent(sid, tool_use_id), home=tmp_home)

        assert result.returncode == 0, result.stderr
        assert not (state_dir(sid) / "agents" / f"{tool_use_id}.json").exists()
        assert not (state_dir(sid) / "pending" / f"{tool_use_id}.json").exists()

    def test_background_agent_tracked_after_post_then_subagent_start(
        self, sid: str, tmp_home: Path, run_hook, state_dir
    ) -> None:
        """Background agents are tracked even when PostToolUse fires before SubagentStart.

        Regression test for the race condition where PostToolUse fires immediately after launch (before the agent
        starts), leaving a stale pending/ marker. SubagentStart must fall through to write a fresh
        agents/<agent_id>.json.
        """
        tool_use_id = "tu-bg"
        agent_id = "agent-bg"
        run_hook("task-log.js", _pre_agent(sid, tool_use_id), home=tmp_home)
        run_hook("task-log.js", _post_agent(sid, tool_use_id), home=tmp_home)
        assert not (state_dir(sid) / "pending" / f"{tool_use_id}.json").exists()

        result = run_hook(
            "task-log.js",
            _subagent_start(sid, agent_id, tool_use_id=tool_use_id),
            home=tmp_home,
        )

        assert result.returncode == 0, result.stderr
        assert (state_dir(sid) / "agents" / f"{agent_id}.json").exists()

    def test_background_agent_survives_post_tool_use(self, sid: str, tmp_home: Path, run_hook, state_dir) -> None:
        """run_in_background agents stay tracked after PostToolUse, which fires at dispatch.

        Regression: a background agent is still running when its Agent() tool call returns, so
        PostToolUse must NOT delete the agents/ entry — the 🤖 badge would otherwise show "none"
        for the agent's whole runtime. Background-ness is recovered from the pending/ marker,
        since PostToolUse's tool_input omits run_in_background.
        """
        tool_use_id = "tu-bgkeep"
        run_hook("task-log.js", _pre_agent(sid, tool_use_id, run_in_background=True), home=tmp_home)
        assert (state_dir(sid) / "agents" / f"{tool_use_id}.json").exists()

        result = run_hook("task-log.js", _post_agent(sid, tool_use_id), home=tmp_home)

        assert result.returncode == 0, result.stderr
        assert (state_dir(sid) / "agents" / f"{tool_use_id}.json").exists()
        assert (state_dir(sid) / "pending" / f"{tool_use_id}.json").exists()

    def test_foreground_agent_subagent_start_consumes_pending(
        self, sid: str, tmp_home: Path, run_hook, state_dir
    ) -> None:
        """SubagentStart with a matching pending entry re-keys agents/<tool_use_id>.json to agents/<agent_id>.json.

        Without the re-key, SubagentStop's later unlink (keyed by agent_id) can never find the record — it stays keyed
        by tool_use_id forever, leaking until the statusline's staleness backstop reaps it instead of being removed on
        actual completion.
        """
        tool_use_id = "tu-fg"
        agent_id = "agent-fg"
        run_hook("task-log.js", _pre_agent(sid, tool_use_id), home=tmp_home)
        assert (state_dir(sid) / "pending" / f"{tool_use_id}.json").exists()

        result = run_hook(
            "task-log.js",
            _subagent_start(sid, agent_id, tool_use_id=tool_use_id),
            home=tmp_home,
        )

        assert result.returncode == 0, result.stderr
        assert not (state_dir(sid) / "agents" / f"{tool_use_id}.json").exists()
        assert (state_dir(sid) / "agents" / f"{agent_id}.json").exists()
        assert not (state_dir(sid) / "pending" / f"{tool_use_id}.json").exists()

    def test_subagent_start_rekey_lets_subagent_stop_remove_it(
        self, sid: str, tmp_home: Path, run_hook, state_dir
    ) -> None:
        """End-to-end: Pre(Agent) → SubagentStart(re-key) → SubagentStop actually removes the record.

        Regression test for the leak the challenger found: before renameAgentFile, SubagentStop's
        unlink (agentsDir/<agent_id>.json) silently no-op'd because the record was still keyed by
        tool_use_id, so a finished agent's entry lingered until the statusline's staleness filter
        aged it out — up to an hour, not on actual completion.
        """
        tool_use_id = "tu-e2e"
        agent_id = "agent-e2e"
        run_hook("task-log.js", _pre_agent(sid, tool_use_id, run_in_background=True), home=tmp_home)
        run_hook("task-log.js", _subagent_start(sid, agent_id, tool_use_id=tool_use_id), home=tmp_home)
        assert (state_dir(sid) / "agents" / f"{agent_id}.json").exists()

        result = run_hook("task-log.js", _subagent_stop(sid, agent_id), home=tmp_home)

        assert result.returncode == 0, result.stderr
        assert not (state_dir(sid) / "agents" / f"{agent_id}.json").exists()

    @pytest.mark.parametrize(
        ("pre_kwargs", "start_agent_type"),
        [
            # Some SubagentStart payloads carry agent_type but not tool_use_id — the hook falls back to scanning
            # pending/ for the most recent entry matching agent_type.
            pytest.param(
                {"subagent_type": "foundry:qa-specialist"}, "foundry:qa-specialist", id="matches-by-agent-type"
            ),
            # Live-observed shape: SubagentStart's ``agent_type`` field carries the assigned *name* for a named Agent()
            # call, not its subagent_type. Matching only on subagent_type would miss it.
            pytest.param(
                {"subagent_type": "foundry:challenger", "name": "post-fix-challenger-2"},
                "post-fix-challenger-2",
                id="named-agent-matches-by-name-not-type",
            ),
        ],
    )
    def test_subagent_start_without_tool_use_id_rekeys_matched_pending(
        self, sid: str, tmp_home: Path, run_hook, state_dir, pre_kwargs: dict, start_agent_type: str
    ) -> None:
        """SubagentStart payload omitting tool_use_id still re-keys the matched pending agent file.

        Scenario: the hook falls back to scanning pending/ for the most recent entry matching agent_type, and for a
        named agent it matches via name rather than subagent_type. Either fallback must re-key
        agents/<tool_use_id>.json to agents/<agent_id>.json, the same as the direct tool_use_id match path, so no
        un-re-keyed file leaks.
        """
        tool_use_id = "tu-pending"
        agent_id = "agent-rekeyed"
        run_hook("task-log.js", _pre_agent(sid, tool_use_id, **pre_kwargs), home=tmp_home)

        result = run_hook(
            "task-log.js",
            _subagent_start(sid, agent_id, agent_type=start_agent_type),
            home=tmp_home,
        )

        assert result.returncode == 0, result.stderr
        assert not (state_dir(sid) / "agents" / f"{tool_use_id}.json").exists()
        assert (state_dir(sid) / "agents" / f"{agent_id}.json").exists()

    def test_subagent_start_no_pending_writes_agent_file(self, sid: str, tmp_home: Path, run_hook, state_dir) -> None:
        """Team-mode SubagentStart (no pending entry) writes agents/<agent_id>.json directly."""
        agent_id = "agent-team"

        result = run_hook(
            "task-log.js",
            _subagent_start(sid, agent_id, agent_type="foundry:qa-specialist"),
            home=tmp_home,
        )

        assert result.returncode == 0, result.stderr
        assert (state_dir(sid) / "agents" / f"{agent_id}.json").exists()

    def test_subagent_stop_removes_agent_file(self, sid: str, tmp_home: Path, run_hook, state_dir) -> None:
        """SubagentStop deletes the per-agent file written by SubagentStart."""
        agent_id = "agent-stop"
        run_hook(
            "task-log.js",
            _subagent_start(sid, agent_id, agent_type="foundry:qa-specialist"),
            home=tmp_home,
        )
        assert (state_dir(sid) / "agents" / f"{agent_id}.json").exists()

        result = run_hook("task-log.js", _subagent_stop(sid, agent_id), home=tmp_home)

        assert result.returncode == 0, result.stderr
        assert not (state_dir(sid) / "agents" / f"{agent_id}.json").exists()


class TestCodexTracking:
    """task-log.js: bridge Skill tracking under state/codex/."""

    def test_codex_skill_pre_creates_codex_file(self, sid: str, tmp_home: Path, run_hook, state_dir) -> None:
        """PreToolUse for bridge Skill writes codex/<tool_use_id>.json."""
        tool_use_id = "tu-cdx-pre"

        result = run_hook(
            "task-log.js",
            _pre_skill(sid, tool_use_id, skill="bridge:review"),
            home=tmp_home,
        )

        assert result.returncode == 0, result.stderr
        assert (state_dir(sid) / "codex" / f"{tool_use_id}.json").exists()

    def test_codex_skill_post_removes_codex_file(self, sid: str, tmp_home: Path, run_hook, state_dir) -> None:
        """PostToolUse for bridge Skill removes the codex tracking file."""
        tool_use_id = "tu-cdx-post"
        run_hook("task-log.js", _pre_skill(sid, tool_use_id, skill="bridge:review"), home=tmp_home)
        assert (state_dir(sid) / "codex" / f"{tool_use_id}.json").exists()

        result = run_hook(
            "task-log.js",
            _post_skill(sid, tool_use_id, skill="bridge:review"),
            home=tmp_home,
        )

        assert result.returncode == 0, result.stderr
        assert not (state_dir(sid) / "codex" / f"{tool_use_id}.json").exists()


class TestToolCounting:
    """task-log.js: per-tool counter under state/tools/."""

    def test_first_call_writes_counter_record(self, sid: str, tmp_home: Path, run_hook, state_dir) -> None:
        """First Bash PreToolUse creates tools/Bash.json with count=1, stamps ``last`` and opens one ``inflight`` slot.

        ``since`` is a fixed window start and goes stale while calls keep landing inside the window, so the status line
        needs a separate per-call stamp (``last``) to decide freshness.
        """
        result = run_hook("task-log.js", _pre_bash(sid, "tu-bash-1"), home=tmp_home)

        assert result.returncode == 0, result.stderr
        counter = state_dir(sid) / "tools" / "Bash.json"
        assert counter.exists()
        data = json.loads(counter.read_text(encoding="utf-8"))
        assert data["count"] == 1
        assert data["tool"] == "Bash"
        assert data["last"] >= data["since"]
        assert data["inflight"] == 1

    def test_second_call_advances_last_but_not_since(self, sid: str, tmp_home: Path, run_hook, state_dir) -> None:
        """A second call inside the 30s window increments ``count``, keeps ``since`` fixed, moves ``last`` forward."""
        run_hook("task-log.js", _pre_bash(sid, "tu-bash-l2a"), home=tmp_home)
        first = json.loads((state_dir(sid) / "tools" / "Bash.json").read_text(encoding="utf-8"))
        run_hook("task-log.js", _pre_bash(sid, "tu-bash-l2b"), home=tmp_home)

        second = json.loads((state_dir(sid) / "tools" / "Bash.json").read_text(encoding="utf-8"))
        assert second["count"] == 2
        assert second["since"] == first["since"]
        assert second["last"] >= first["last"]
        assert second["inflight"] == 2

    @pytest.mark.parametrize("failed", [pytest.param(False, id="ok"), pytest.param(True, id="error")])
    def test_post_tool_use_releases_inflight(self, sid: str, tmp_home: Path, run_hook, state_dir, failed: bool) -> None:
        """PostToolUse and PostToolUseFailure both close the inflight slot opened at PreToolUse."""
        run_hook("task-log.js", _pre_bash(sid, "tu-bash-r1"), home=tmp_home)
        result = run_hook("task-log.js", _post_bash(sid, "tu-bash-r1", failed=failed), home=tmp_home)

        assert result.returncode == 0, result.stderr
        data = json.loads((state_dir(sid) / "tools" / "Bash.json").read_text(encoding="utf-8"))
        assert data["inflight"] == 0
        assert data["count"] == 1  # release must not disturb the per-window call count

    def test_inflight_never_goes_negative(self, sid: str, tmp_home: Path, run_hook, state_dir) -> None:
        """A duplicate PostToolUse (both settings files register the hook) floors inflight at 0."""
        run_hook("task-log.js", _pre_bash(sid, "tu-bash-r2"), home=tmp_home)
        run_hook("task-log.js", _post_bash(sid, "tu-bash-r2"), home=tmp_home)
        run_hook("task-log.js", _post_bash(sid, "tu-bash-r2"), home=tmp_home)

        data = json.loads((state_dir(sid) / "tools" / "Bash.json").read_text(encoding="utf-8"))
        assert data["inflight"] == 0


class TestAgentLivenessRefresh:
    """task-log.js: touchAgentLastActive refreshes a worktree subagent's last_active on tool activity."""

    def test_worktree_tool_event_stamps_last_active(self, sid: str, tmp_home: Path, run_hook, state_dir) -> None:
        """A tool event with cwd = .claude/worktrees/agent-<id> stamps last_active on agents/<id>.json.

        The agent file is keyed by agent_id (as SubagentStart writes it); the worktree cwd basename
        (agent-<id>) resolves back to that key, so the still-running agent's freshness is renewed.
        """
        agent_id = "a0abc123"
        _write_agent_file(state_dir, sid, agent_id)
        cwd = f"/repo/.claude/worktrees/agent-{agent_id}"

        result = run_hook("task-log.js", _pre_tool_with_cwd(sid, "tu-r1", cwd), home=tmp_home)

        assert result.returncode == 0, result.stderr
        rec = json.loads((state_dir(sid) / "agents" / f"{agent_id}.json").read_text(encoding="utf-8"))
        assert "last_active" in rec
        assert rec["since"] == "2000-01-01T00:00:00.000Z"  # dispatch time preserved, not overwritten

    def test_non_worktree_cwd_does_not_stamp(self, sid: str, tmp_home: Path, run_hook, state_dir) -> None:
        """A tool event whose cwd is the project root (not a worktree) leaves last_active unset.

        Non-worktree agents share the parent's cwd, so their activity can't be attributed to a specific agent — the
        record must stay unchanged and rely on the staleness backstop.
        """
        agent_id = "a0def456"
        _write_agent_file(state_dir, sid, agent_id)
        cwd = "/repo"  # project root, not a worktree

        result = run_hook("task-log.js", _pre_tool_with_cwd(sid, "tu-r2", cwd), home=tmp_home)

        assert result.returncode == 0, result.stderr
        rec = json.loads((state_dir(sid) / "agents" / f"{agent_id}.json").read_text(encoding="utf-8"))
        assert "last_active" not in rec

    def test_worktree_cwd_no_matching_agent_is_noop(self, sid: str, tmp_home: Path, run_hook, state_dir) -> None:
        """A worktree cwd whose agent file is absent (agent finished) is a silent no-op, not an error."""
        cwd = "/repo/.claude/worktrees/agent-a0gone999"

        result = run_hook("task-log.js", _pre_tool_with_cwd(sid, "tu-r3", cwd), home=tmp_home)

        assert result.returncode == 0, result.stderr
        assert not (state_dir(sid) / "agents" / "a0gone999.json").exists()


class TestCompactionLogs:
    """Compaction telemetry is append-only, private and independent of context preservation."""

    @pytest.mark.parametrize("instructions", ["private instructions", "", None])
    def test_precompact_row(self, sid: str, tmp_home: Path, run_hook, instructions: str | None) -> None:
        """Record only whether instructions exist, even without a transcript to snapshot."""
        payload = {"hook_event_name": "PreCompact", "session_id": sid, "trigger": "manual"}
        if instructions is not None:
            payload["custom_instructions"] = instructions
        before = datetime.now(timezone.utc)
        result = run_hook("task-log.js", payload, home=tmp_home, cwd=tmp_home)
        after = datetime.now(timezone.utc)

        assert result.returncode == 0, result.stderr
        assert result.stdout == result.stderr == ""
        raw = (tmp_home / ".claude" / "logs" / "compactions.jsonl").read_bytes()
        assert raw.endswith(b"\n")
        assert len(raw.splitlines()) == 1
        row = json.loads(raw)
        ts = row.pop("ts")
        assert row == {
            "event": "precompact",
            "session_id": sid,
            "trigger": "manual",
            "has_custom_instructions": bool(instructions),
        }
        assert isinstance(row["has_custom_instructions"], bool)
        assert b"private instructions" not in raw
        parsed = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
        assert before.replace(microsecond=before.microsecond // 1000 * 1000) <= parsed <= after
        assert parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z") == ts

    @pytest.mark.parametrize("summary", ["private summary", "résumé 📝", "", None])
    def test_postcompact_row(self, sid: str, tmp_home: Path, run_hook, summary: str | None) -> None:
        """Log summary character count without retaining the summary or assuming precompact fields."""
        payload = {"hook_event_name": "PostCompact", "session_id": sid, "trigger": "auto"}
        if summary is not None:
            payload["compact_summary"] = summary
        result = run_hook("task-log.js", payload, home=tmp_home, cwd=tmp_home)

        assert result.returncode == 0, result.stderr
        assert result.stdout == result.stderr == ""
        raw = (tmp_home / ".claude" / "logs" / "compactions.jsonl").read_bytes()
        assert raw.endswith(b"\n")
        assert len(raw.splitlines()) == 1
        row = json.loads(raw)
        ts = row.pop("ts")
        assert row == {"event": "postcompact", "session_id": sid, "trigger": "auto", "summary_len": len(summary or "")}
        assert b"private summary" not in raw
        parsed = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
        assert parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z") == ts

    @pytest.mark.parametrize("log_state", ["missing", "blocked-directory", "blocked-file"])
    def test_log_failure_preserves_snapshot(
        self, sid: str, tmp_path: Path, tmp_home: Path, run_hook, log_state: str
    ) -> None:
        """Missing directories are created; deterministic write failures cannot suppress the snapshot or contract."""
        logs = tmp_home / ".claude" / "logs"
        if log_state == "blocked-file":
            (logs / "compactions.jsonl").mkdir()
        else:
            logs.rmdir()
            if log_state == "blocked-directory":
                logs.write_text("not a directory", encoding="utf-8")
        project = tmp_path / "project"
        contract_dir = project / ".temp" / "state"
        contract_dir.mkdir(parents=True)
        contract = b"Keep the current contract verbatim.\r\n"
        (contract_dir / "skill-contract.md").write_bytes(contract)
        transcript = tmp_path / "transcript.jsonl"
        _write_transcript(transcript, ["src/result.py"])

        result = run_hook("task-log.js", _pre_compact(sid, str(transcript)), home=tmp_home, cwd=project)

        assert result.returncode == 0, result.stderr
        assert result.stdout == result.stderr == ""
        snapshot = (project / ".claude" / "state" / "session-context.md").read_bytes()
        assert b"- src/result.py\n" in snapshot
        assert snapshot.endswith(b"## Skill Compaction Contract\nunverified at PreCompact\n" + contract)
        if log_state == "missing":
            assert json.loads((logs / "compactions.jsonl").read_bytes())["event"] == "precompact"
        elif log_state == "blocked-directory":
            assert logs.read_text(encoding="utf-8") == "not a directory"
        else:
            assert (logs / "compactions.jsonl").is_dir()
            assert list((logs / "compactions.jsonl").iterdir()) == []
        post = run_hook(
            "task-log.js",
            {"hook_event_name": "PostCompact", "session_id": sid, "trigger": "auto"},
            home=tmp_home,
            cwd=project,
        )
        assert post.returncode == 0, post.stderr
        assert post.stdout == post.stderr == ""
        assert (project / ".claude" / "state" / "session-context.md").read_bytes() == snapshot

    def test_compactions_append(self, sid: str, tmp_home: Path, run_hook) -> None:
        """Successive pre/post events preserve earlier bytes and append exactly one row each."""
        log = tmp_home / ".claude" / "logs" / "compactions.jsonl"
        previous = b""
        for event in ("PreCompact", "PostCompact", "PreCompact", "PostCompact"):
            result = run_hook(
                "task-log.js",
                {"hook_event_name": event, "session_id": sid, "trigger": "auto"},
                home=tmp_home,
                cwd=tmp_home,
            )
            assert result.returncode == 0, result.stderr
            assert result.stdout == result.stderr == ""
            current = log.read_bytes()
            assert current.startswith(previous)
            appended = current[len(previous) :]
            assert appended.endswith(b"\n")
            assert len(appended.splitlines()) == 1
            assert json.loads(appended)["event"] == event.lower()
            previous = current
        assert len(previous.splitlines()) == 4

    def test_postcompact_registration(self) -> None:
        """PostCompact uses exactly the PreCompact task logger's node probe, without a matcher."""
        hooks = json.loads((Path(__file__).parents[2] / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
        pre = next(group for group in hooks["PreCompact"] if "task-log.js" in group["hooks"][0]["command"])
        assert hooks["PostCompact"] == [pre]
        assert "matcher" not in hooks["PostCompact"][0]


class TestLogMasking:
    """task-log.js masks credentials in free-text log fields and leaves parsed fields intact."""

    @staticmethod
    def _timing_args(home: Path, sid: str, tool: str, tool_input: dict) -> str:
        """Run PreToolUse then PostToolUse for one call and return the logged ``args`` summary."""
        base = {"session_id": sid, "tool_name": tool, "tool_input": tool_input, "tool_use_id": f"tu-{tool}"}
        for event in ("PreToolUse", "PostToolUse"):
            result = subprocess.run(
                ["node", str(Path(__file__).resolve().parents[2] / "hooks" / "task-log.js")],
                input=json.dumps({**base, "hook_event_name": event}),
                capture_output=True,
                text=True,
                env={**os.environ, "HOME": str(home), "USERPROFILE": str(home)},
                cwd=home,
                check=False,
            )
            assert result.returncode == 0, result.stderr
        rows = [
            json.loads(line) for line in (home / ".claude" / "logs" / "timings.jsonl").read_text("utf-8").splitlines()
        ]
        return next(r["args"] for r in rows if r["tool"] == tool)

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            pytest.param("TOKEN=secret", "command=TOKEN=[masked]", id="assignment-at-end"),
            pytest.param('GH_TOKEN="a\\" b" run', "command=GH_TOKEN=[masked] run", id="escaped-quote"),
            pytest.param("X=$(cat secret) run", "command=X=[masked] run", id="command-substitution"),
            pytest.param(
                "export AWS_SECRET_ACCESS_KEY=abc", "command=export AWS_SECRET_ACCESS_KEY=[masked]", id="export"
            ),
            pytest.param("cd x && API_KEY=zzz curl", "command=cd x && API_KEY=[masked] curl", id="after-and"),
            pytest.param(
                'curl -H "Authorization: Bearer abc123def456"',
                'command=curl -H "Authorization: Bearer [masked]"',
                id="bearer",
            ),
            pytest.param(
                "git clone https://u:pw@example.com/r",
                "command=git clone https://u:[masked]@example.com/r",
                id="url-credentials",
            ),
            pytest.param("run --token abc123 now", "command=run --token [masked] now", id="token-flag"),
            pytest.param("echo glpat-abcdefghijklmnop", "command=echo [redacted]", id="token-prefix"),
            pytest.param(
                "git -C /a/very/long/path/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa log",
                "command=git -C /a/very/long/path/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa log",
                id="long-path-kept",
            ),
        ],
    )
    def test_bash_command_masked(self, sid: str, tmp_home: Path, command: str, expected: str) -> None:
        """Credentials in a Bash command never reach timings.jsonl; plain paths and words survive."""
        assert self._timing_args(tmp_home, sid, "Bash", {"command": command}) == expected

    def test_edit_path_with_secret_lookalikes_is_untouched(self, sid: str, tmp_home: Path) -> None:
        """A path containing ``=`` or ``sk-`` stays byte-identical: PreCompact and the profiler parse it."""
        path = "/x/GH_TOKEN=abc def/sk-abcdefghijkl.py"
        assert self._timing_args(tmp_home, sid, "Edit", {"file_path": path}) == f"file_path={path}"

    def test_skill_args_and_default_branch_masked(self, sid: str, tmp_home: Path) -> None:
        """Skill arguments and the generic JSON summary mask credentials after the parsed ``skill=`` prefix."""
        skill = self._timing_args(tmp_home, sid, "Skill", {"skill": "x:y", "args": "--token abc123 go"})
        assert skill == "skill=x:y args=--token [masked] go"
        other = self._timing_args(tmp_home, sid, "WebFetch", {"url": "https://h/?access_token=abc"})
        assert "abc" not in other

    @pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
    def test_existing_logs_dir_and_files_tightened(self, sid: str, tmp_home: Path) -> None:
        """A pre-existing 0755 logs dir and 0644 log file are tightened to 0700 / 0600."""
        logs = tmp_home / ".claude" / "logs"
        logs.chmod(0o755)
        log = logs / "timings.jsonl"
        log.write_text("", encoding="utf-8")
        log.chmod(0o644)
        self._timing_args(tmp_home, sid, "Bash", {"command": "echo hi"})
        assert logs.stat().st_mode & 0o777 == 0o700
        assert log.stat().st_mode & 0o777 == 0o600


class TestPreCompactContract:
    """task-log.js: PreCompact writes session-context.md and preserves the skill contract verbatim.

    PreCompact writes ``<cwd>/.claude/state/session-context.md`` (cwd = project root),
    not the ephemeral ``claude-state-<sid>`` dir — so these tests pass ``cwd`` and
    read the context file from that project-local state dir. The skill contract is
    staged by skills at ``<cwd>/.temp/state/skill-contract.md`` (relocated out of
    ``.claude/state/`` to dodge the sensitive-file gate), which is where the hook reads it.
    """

    def test_precompact_appends_contract_verbatim(self, sid: str, tmp_home: Path, tmp_path: Path, run_hook) -> None:
        """A staged skill-contract.md is appended verbatim under its own section alongside Files Modified."""
        proj = tmp_path / "proj"
        state = proj / ".claude" / "state"
        state.mkdir(parents=True)
        contract_dir = proj / ".temp" / "state"
        contract_dir.mkdir(parents=True)
        transcript = tmp_path / "transcript.jsonl"
        _write_transcript(transcript, ["/proj/src/foo.py"])
        contract = "CONTRACT: keep phase 3 in-progress; do NOT re-run steps 1-2.\n- next: verify tests"
        (contract_dir / "skill-contract.md").write_text(contract, encoding="utf-8")

        result = run_hook("task-log.js", _pre_compact(sid, str(transcript)), home=tmp_home, cwd=proj)

        assert result.returncode == 0, result.stderr
        written = (state / "session-context.md").read_text(encoding="utf-8")
        assert "## Files Modified This Session" in written
        assert "- /proj/src/foo.py" in written
        assert "## Skill Compaction Contract" in written
        assert contract in written

    def test_precompact_no_contract_writes_files_only(self, sid: str, tmp_home: Path, tmp_path: Path, run_hook) -> None:
        """With no skill-contract.md, PreCompact writes Files Modified and no contract section, no error."""
        proj = tmp_path / "proj"
        (proj / ".claude" / "state").mkdir(parents=True)
        transcript = tmp_path / "transcript.jsonl"
        _write_transcript(transcript, ["/proj/src/bar.py"])

        result = run_hook("task-log.js", _pre_compact(sid, str(transcript)), home=tmp_home, cwd=proj)

        assert result.returncode == 0, result.stderr
        written = (proj / ".claude" / "state" / "session-context.md").read_text(encoding="utf-8")
        assert "## Files Modified This Session" in written
        assert "- /proj/src/bar.py" in written
        assert "## Skill Compaction Contract" not in written

    def test_precompact_lists_edits_beyond_transcript_tail(
        self, sid: str, tmp_home: Path, tmp_path: Path, run_hook
    ) -> None:
        """Edits older than the 50KB transcript tail are listed from timings.jsonl, scoped to this session."""
        proj = tmp_path / "proj"
        (proj / ".claude" / "state").mkdir(parents=True)
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text("x" * (200 * 1024) + "\n", encoding="utf-8")
        records = [
            {"session_id": sid, "tool": "Write", "args": "file_path=/proj/old one.py"},
            {"session_id": sid, "tool": "MultiEdit", "args": "file_path=/proj/multi.py"},
            {"session_id": sid, "tool": "NotebookEdit", "args": "file_path=/proj/nb.ipynb"},
            {"session_id": sid, "tool": "Read", "args": "file_path=/proj/read_only.py"},
            {"session_id": "other-session", "tool": "Edit", "args": "file_path=/proj/foreign.py"},
        ]
        log = tmp_home / ".claude" / "logs" / "timings.jsonl"
        log.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")

        result = run_hook("task-log.js", _pre_compact(sid, str(transcript)), home=tmp_home, cwd=proj)

        assert result.returncode == 0, result.stderr
        written = (proj / ".claude" / "state" / "session-context.md").read_text(encoding="utf-8")
        assert "- /proj/old one.py" in written
        assert "- /proj/multi.py" in written
        assert "- /proj/nb.ipynb" in written
        assert "read_only.py" not in written
        assert "foreign.py" not in written

    @pytest.mark.parametrize(
        ("tool", "tool_input", "expected_args"),
        [
            pytest.param("MultiEdit", {"file_path": "/p/a.py", "edits": []}, "file_path=/p/a.py", id="multiedit"),
            pytest.param("NotebookEdit", {"notebook_path": "/p/n.ipynb"}, "file_path=/p/n.ipynb", id="notebookedit"),
        ],
    )
    def test_timing_records_edit_tool_path(
        self, sid: str, tmp_home: Path, run_hook, tool: str, tool_input: dict, expected_args: str
    ) -> None:
        """MultiEdit and NotebookEdit write a ``file_path=`` args summary so PreCompact can find them."""
        base = {"session_id": sid, "tool_name": tool, "tool_input": tool_input, "tool_use_id": f"tu-{tool}"}
        run_hook("task-log.js", {**base, "hook_event_name": "PreToolUse"}, home=tmp_home)
        run_hook("task-log.js", {**base, "hook_event_name": "PostToolUse"}, home=tmp_home)

        rows = [
            json.loads(line)
            for line in (tmp_home / ".claude" / "logs" / "timings.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert [r["args"] for r in rows if r["tool"] == tool] == [expected_args]

    @pytest.mark.parametrize(
        ("stamp", "expected"),
        [
            pytest.param("verified", "verified", id="same-session"),
            pytest.param("absent", "unverified", id="no-owner"),
            pytest.param("stale", "unverified", id="sha-mismatch"),
            pytest.param("foreign", None, id="matching-sha-foreign-session"),
            pytest.param("malformed", "unverified", id="malformed-owner"),
            pytest.param("null", "unverified", id="null-owner"),
            pytest.param("missing-session", "unverified", id="incomplete-owner"),
            pytest.param("unreadable", "unverified", id="owner-is-directory"),
        ],
    )
    def test_precompact_owner_states(
        self, sid: str, tmp_home: Path, tmp_path: Path, run_hook, stamp: str, expected: str | None
    ) -> None:
        """Save and restore share provenance states; PreCompact retains every byte of the contract text."""
        project = tmp_path / "project"
        state = project / ".temp" / "state"
        state.mkdir(parents=True)
        raw = b"  CONTRACT: keep whitespace\r\nnext: resume\n\n"
        (state / "skill-contract.md").write_bytes(raw)
        owner = state / "skill-contract.owner"
        if stamp == "unreadable":
            owner.mkdir()
        elif stamp != "absent":
            data = {
                "session_id": "foreign-session" if stamp == "foreign" else sid,
                "sha256": "0" * 64 if stamp == "stale" else hashlib.sha256(raw).hexdigest(),
                "written_at": "2000-01-01T00:00:00Z",
            }
            if stamp == "missing-session":
                del data["session_id"]
            owner.write_text("{broken" if stamp == "malformed" else json.dumps(None if stamp == "null" else data))
        transcript = tmp_path / "transcript.jsonl"
        _write_transcript(transcript, ["src/result.py"])
        result = run_hook("task-log.js", _pre_compact(sid, str(transcript)), home=tmp_home, cwd=project)
        assert result.returncode == 0, result.stderr
        saved = (project / ".claude" / "state" / "session-context.md").read_bytes()
        assert b"## Files Modified This Session" in saved
        if expected is None:
            assert b"## Skill Compaction Contract" not in saved
            assert raw not in saved
        else:
            status = (
                f"verified for session {sid} at PreCompact" if expected == "verified" else "unverified at PreCompact"
            )
            assert saved.count(b"## Skill Compaction Contract") == 1
            assert b"## Skill Compaction Contract\n" + status.encode() + b"\n" + raw in saved
            assert saved.endswith(raw)


_requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git executable not available")

_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def _git(cwd: Path, *args: str) -> str:
    """Run one git command in ``cwd`` isolated from user/system config, failing the test on error."""
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        env=_GIT_ENV,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _age_worktree(tree: Path, seconds: float) -> None:
    """Backdate a worktree dir and its git admin files, which the sweep reads as last activity."""
    stamp = time.time() - seconds
    gitdir = Path(_git(tree, "rev-parse", "--absolute-git-dir").strip())
    for p in (tree, gitdir / "index", gitdir / "HEAD", gitdir / "logs" / "HEAD"):
        if p.exists():
            os.utime(p, (stamp, stamp))


@pytest.mark.integration
@_requires_git
class TestSessionEndWorktreeCleanup:
    """task-log.js SessionEnd: stale-worktree sweep never destroys user deliverables or pending work."""

    def test_sweep_removes_only_idle_clean_managed_trees(
        self, sid: str, tmp_home: Path, tmp_path: Path, run_hook
    ) -> None:
        """Only an idle, clean, registered agent-*/oss-* tree is removed; every other kind survives."""
        proj = tmp_path / "proj"
        proj.mkdir()
        _git(proj, "init", "-q")
        (proj / ".gitignore").write_text(".temp/\n", encoding="utf-8")
        (proj / "a.txt").write_text("a\n", encoding="utf-8")
        _git(proj, "add", ".gitignore", "a.txt")
        _git(proj, "commit", "-q", "-m", "init")
        wt_root = proj / ".claude" / "worktrees"
        registered = ["agent-old", "oss-old", "dev-old", "manual-old", "agent-dirty", "agent-output", "agent-fresh"]
        for name in registered:
            _git(proj, "worktree", "add", "-q", "-b", name, str(wt_root / name), "HEAD")
        (wt_root / "agent-dirty" / "a.txt").write_text("edited\n", encoding="utf-8")
        (wt_root / "agent-output" / ".temp").mkdir()
        (wt_root / "agent-output" / ".temp" / "extract.md").write_text("result\n", encoding="utf-8")
        (wt_root / "agent-orphan").mkdir()
        for name in registered:
            if name != "agent-fresh":
                _age_worktree(wt_root / name, 3 * 60 * 60)
        stamp = time.time() - 3 * 60 * 60
        os.utime(wt_root / "agent-orphan", (stamp, stamp))

        result = run_hook("task-log.js", {"hook_event_name": "SessionEnd", "session_id": sid}, home=tmp_home, cwd=proj)

        assert result.returncode == 0, result.stderr
        assert not (wt_root / "agent-old").exists()
        assert not (wt_root / "oss-old").exists()
        assert (wt_root / "dev-old").is_dir()
        assert (wt_root / "manual-old").is_dir()
        assert (wt_root / "agent-dirty" / "a.txt").read_text(encoding="utf-8") == "edited\n"
        assert (wt_root / "agent-output" / ".temp" / "extract.md").is_file()
        assert (wt_root / "agent-fresh").is_dir()
        assert (wt_root / "agent-orphan").is_dir()


def _contract_path_capability(kind: str) -> bool:
    """Probe special-file creation before collection, including Windows restrictions."""
    with tempfile.TemporaryDirectory() as directory:
        candidate = Path(directory) / "candidate"
        try:
            if kind == "fifo":
                if not hasattr(os, "mkfifo"):
                    return False
                os.mkfifo(candidate)
            else:
                target = Path(directory) / "target"
                target.write_bytes(b"test")
                candidate.symlink_to(target)
        except OSError:
            return False
    return True


@pytest.mark.skipif(shutil.which("node") is None, reason="node executable not available")
@pytest.mark.parametrize("target", ["contract", "owner"])
@pytest.mark.parametrize(
    "kind",
    [
        "oversize",
        pytest.param(
            "symlink",
            marks=pytest.mark.skipif(not _contract_path_capability("symlink"), reason="symlink creation unavailable"),
        ),
        pytest.param(
            "fifo", marks=pytest.mark.skipif(not _contract_path_capability("fifo"), reason="FIFO creation unavailable")
        ),
    ],
)
def test_unsafe_contract_paths_fail_open(tmp_path: Path, target: str, kind: str) -> None:
    """Reject unsafe contract paths and retain advisory text for unsafe owner paths without blocking."""
    project = tmp_path / "project"
    state = project / ".temp" / "state"
    state.mkdir(parents=True)
    raw = b"CONTRACT: continue safely\n"
    contract = state / "skill-contract.md"
    owner = state / "skill-contract.owner"
    stamp = json.dumps({"session_id": "safe-session", "sha256": hashlib.sha256(raw).hexdigest()}).encode()
    contract.write_bytes(raw)
    owner.write_bytes(stamp)
    candidate = contract if target == "contract" else owner
    content = raw if target == "contract" else stamp
    candidate.unlink()
    if kind == "symlink":
        backing = state / "backing"
        backing.write_bytes(content)
        candidate.symlink_to(backing)
    elif kind == "fifo":
        os.mkfifo(candidate)
    else:
        cap = 256 * 1024 if target == "contract" else 4 * 1024
        candidate.write_bytes(content + b" " * (cap + 1 - len(content)))
    payload = {"session_id": "safe-session", "hook_event_name": "PreCompact", "source": "compact"}
    transcript = project / "transcript.jsonl"
    transcript.write_bytes(b'{"type":"user","message":{"content":"hello"}}\n')
    payload["transcript_path"] = str(transcript)
    result = subprocess.run(
        ["node", str(Path(__file__).resolve().parents[2] / "hooks" / "task-log.js")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=project,
        env={**os.environ, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path), "TMPDIR": str(tmp_path)},
        # Include Node startup on loaded CI runners while retaining a finite deadline for FIFO regressions.
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    output = (project / ".claude" / "state" / "session-context.md").read_text(encoding="utf-8")
    if target == "contract":
        assert "CONTRACT:" not in output
        assert "Skill contract," not in output
        assert "## Skill Compaction Contract" not in output
    else:
        assert "CONTRACT: continue safely" in output
        assert "unverified at PreCompact" in output
        assert "Skill contract, verified:" not in output
        assert "verified for session" not in output

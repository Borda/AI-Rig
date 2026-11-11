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
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import pytest
from _hook_env import _contract_path_capability

# ── Fixtures ──────────────────────────────────────────────────────────────────


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
        """PreToolUse for Agent() writes both agents/<tool_use_id>.json and pending/<tool_use_id>.json.

        The agents/ record feeds the status badge immediately; the pending/ record lets SubagentStart re-key it to the
        real agent id later.
        """
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
        """Team-mode SubagentStart (no pending entry) writes agents/<agent_id>.json directly.

        Team agents are started without a preceding Agent() tool call, so there is no pending record to re-key.
        """
        agent_id = "agent-team"

        result = run_hook(
            "task-log.js",
            _subagent_start(sid, agent_id, agent_type="foundry:qa-specialist"),
            home=tmp_home,
        )

        assert result.returncode == 0, result.stderr
        assert (state_dir(sid) / "agents" / f"{agent_id}.json").exists()

    def test_subagent_stop_removes_agent_file(self, sid: str, tmp_home: Path, run_hook, state_dir) -> None:
        """SubagentStop deletes the per-agent file written by SubagentStart.

        A finished agent must leave the status badge as soon as it stops, not when a staleness backstop ages it out.
        """
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
        """PreToolUse for bridge Skill writes codex/<tool_use_id>.json.

        The status line reads this file to show that a Codex run is active while the bridge skill executes.
        """
        tool_use_id = "tu-cdx-pre"

        result = run_hook(
            "task-log.js",
            _pre_skill(sid, tool_use_id, skill="bridge:review"),
            home=tmp_home,
        )

        assert result.returncode == 0, result.stderr
        assert (state_dir(sid) / "codex" / f"{tool_use_id}.json").exists()

    def test_codex_skill_post_removes_codex_file(self, sid: str, tmp_home: Path, run_hook, state_dir) -> None:
        """PostToolUse for bridge Skill removes the codex tracking file.

        Once the bridge skill returns, the Codex activity indicator must disappear instead of lingering.
        """
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
        """A second call inside the 30s window increments ``count``, keeps ``since`` fixed, moves ``last`` forward.

        Two Bash PreToolUse events land back to back; the window start stays put while the per-call stamp advances and
        both calls count as in flight.
        """
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
        """PostToolUse and PostToolUseFailure both close the inflight slot opened at PreToolUse.

        A failed tool call must not leave a phantom running tool on the status line.
        """
        run_hook("task-log.js", _pre_bash(sid, "tu-bash-r1"), home=tmp_home)
        result = run_hook("task-log.js", _post_bash(sid, "tu-bash-r1", failed=failed), home=tmp_home)

        assert result.returncode == 0, result.stderr
        data = json.loads((state_dir(sid) / "tools" / "Bash.json").read_text(encoding="utf-8"))
        assert data["inflight"] == 0
        assert data["count"] == 1  # release must not disturb the per-window call count

    def test_inflight_never_goes_negative(self, sid: str, tmp_home: Path, run_hook, state_dir) -> None:
        """A duplicate PostToolUse (both settings files register the hook) floors inflight at 0.

        One Pre and two Post events for the same call: the second release must not drive the counter below zero.
        """
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
        """A worktree cwd whose agent file is absent (agent finished) is a silent no-op, not an error.

        Late tool events can arrive after SubagentStop removed the record; refreshing must not recreate it or fail.
        """
        cwd = "/repo/.claude/worktrees/agent-a0gone999"

        result = run_hook("task-log.js", _pre_tool_with_cwd(sid, "tu-r3", cwd), home=tmp_home)

        assert result.returncode == 0, result.stderr
        assert not (state_dir(sid) / "agents" / "a0gone999.json").exists()


def _compaction_events(run_hook, sid: str, home: Path, events: list[str]) -> tuple[list, list[bytes]]:
    """Fire each compaction event in order; return the hook results and the log bytes seen after every event."""
    log = home / ".claude" / "logs" / "compactions.jsonl"
    results, snapshots = [], []
    for event in events:
        results.append(
            run_hook(
                "task-log.js", {"hook_event_name": event, "session_id": sid, "trigger": "auto"}, home=home, cwd=home
            )
        )
        snapshots.append(log.read_bytes())
    return results, snapshots


def _remove_logs_dir(logs: Path) -> None:
    """Delete the empty logs directory, so the hook has to create it."""
    logs.rmdir()


def _replace_logs_dir_with_file(logs: Path) -> None:
    """Put a regular file where the logs directory belongs, so no log can be created."""
    logs.rmdir()
    logs.write_text("not a directory", encoding="utf-8")


def _block_log_file_with_directory(logs: Path) -> None:
    """Put a directory where the log file belongs, so opening it for append fails."""
    (logs / "compactions.jsonl").mkdir()


def _assert_row_written(logs: Path) -> None:
    """Assert the hook recreated the logs directory and logged the precompact row."""
    assert json.loads((logs / "compactions.jsonl").read_bytes())["event"] == "precompact"


def _assert_logs_file_untouched(logs: Path) -> None:
    """Assert the blocking file stayed exactly as planted."""
    assert logs.read_text(encoding="utf-8") == "not a directory"


def _assert_blocking_directory_empty(logs: Path) -> None:
    """Assert the blocking directory is still a directory and nothing was written into it."""
    assert (logs / "compactions.jsonl").is_dir()
    assert list((logs / "compactions.jsonl").iterdir()) == []


def _stage_contract(tmp_path: Path, raw: bytes) -> tuple[Path, Path, Path]:
    """Stage a skill contract and a one-file transcript in a project; return project, state dir and transcript."""
    project = tmp_path / "project"
    state = project / ".temp" / "state"
    state.mkdir(parents=True)
    (state / "skill-contract.md").write_bytes(raw)
    transcript = tmp_path / "transcript.jsonl"
    _write_transcript(transcript, ["src/result.py"])
    return project, state, transcript


def _owner_record(sid: str, raw: bytes, **override: object) -> dict:
    """Return a valid ownership stamp for ``raw`` written by session ``sid``, with fields optionally overridden."""
    return {
        "session_id": sid,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "written_at": "2000-01-01T00:00:00Z",
        **override,
    }


def _owner_without_session(sid: str, raw: bytes) -> str:
    """Return an ownership stamp whose ``session_id`` field is absent."""
    return json.dumps({k: v for k, v in _owner_record(sid, raw).items() if k != "session_id"})


def _stage_text(path: Path, text: str) -> None:
    """Write ``text`` at the owner path."""
    path.write_text(text, encoding="utf-8")


def _stage_nothing(path: Path, text: str) -> None:
    """Leave the owner path absent."""


def _stage_directory(path: Path, text: str) -> None:
    """Make the owner path a directory, which cannot be read as a stamp."""
    path.mkdir()


class TestCompactionLogs:
    """Compaction telemetry is append-only, private and independent of context preservation."""

    @pytest.mark.parametrize(
        "extra",
        [
            pytest.param({"custom_instructions": "private instructions"}, id="instructions"),
            pytest.param({"custom_instructions": ""}, id="empty-instructions"),
            pytest.param({}, id="no-instructions"),
        ],
    )
    def test_precompact_writes_row(self, sid: str, tmp_home: Path, run_hook, extra: dict) -> None:
        """PreCompact logs one row that records only whether custom instructions exist, never their text.

        No transcript is supplied, so this also proves the row is written when there is nothing to snapshot; the
        instructions themselves are private and must not reach the log.
        """
        payload = {"hook_event_name": "PreCompact", "session_id": sid, "trigger": "manual", **extra}
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
            "has_custom_instructions": bool(extra.get("custom_instructions")),
        }
        assert b"private instructions" not in raw
        parsed = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
        assert before.replace(microsecond=before.microsecond // 1000 * 1000) <= parsed <= after
        assert parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z") == ts

    @pytest.mark.parametrize(
        "extra",
        [
            pytest.param({"compact_summary": "private summary"}, id="summary"),
            pytest.param({"compact_summary": "résumé 📝"}, id="non-ascii-summary"),
            pytest.param({"compact_summary": ""}, id="empty-summary"),
            pytest.param({}, id="no-summary"),
        ],
    )
    def test_postcompact_writes_row(self, sid: str, tmp_home: Path, run_hook, extra: dict) -> None:
        """PostCompact logs the summary's character count without retaining the summary text.

        The payload carries no precompact fields, so the row must not assume them; non-ASCII text is counted in
        characters, not bytes.
        """
        payload = {"hook_event_name": "PostCompact", "session_id": sid, "trigger": "auto", **extra}

        result = run_hook("task-log.js", payload, home=tmp_home, cwd=tmp_home)

        assert result.returncode == 0, result.stderr
        assert result.stdout == result.stderr == ""
        raw = (tmp_home / ".claude" / "logs" / "compactions.jsonl").read_bytes()
        assert raw.endswith(b"\n")
        assert len(raw.splitlines()) == 1
        row = json.loads(raw)
        ts = row.pop("ts")
        expected_len = len(extra.get("compact_summary", ""))
        assert row == {"event": "postcompact", "session_id": sid, "trigger": "auto", "summary_len": expected_len}
        assert b"private summary" not in raw
        parsed = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
        assert parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z") == ts

    @pytest.mark.parametrize(
        ("break_logs", "check_logs"),
        [
            pytest.param(_remove_logs_dir, _assert_row_written, id="missing"),
            pytest.param(_replace_logs_dir_with_file, _assert_logs_file_untouched, id="blocked-directory"),
            pytest.param(_block_log_file_with_directory, _assert_blocking_directory_empty, id="blocked-file"),
        ],
    )
    def test_log_failure_preserves_snapshot(
        self,
        sid: str,
        tmp_path: Path,
        tmp_home: Path,
        run_hook,
        break_logs: Callable[[Path], None],
        check_logs: Callable[[Path], None],
    ) -> None:
        """A missing log directory is created; deterministic write failures cannot suppress the snapshot or contract.

        PreCompact must still write session-context.md with the staged contract verbatim when its log cannot be written,
        and a following PostCompact must leave that snapshot byte-identical.
        """
        logs = tmp_home / ".claude" / "logs"
        break_logs(logs)
        contract = b"Keep the current contract verbatim.\r\n"
        project, state, transcript = _stage_contract(tmp_path, contract)
        (state / "skill-contract.owner").write_text(json.dumps(_owner_record(sid, contract)), encoding="utf-8")

        result = run_hook("task-log.js", _pre_compact(sid, str(transcript)), home=tmp_home, cwd=project)

        assert result.returncode == 0, result.stderr
        assert result.stdout == result.stderr == ""
        snapshot = (project / ".claude" / "state" / "session-context.md").read_bytes()
        assert b"- src/result.py\n" in snapshot
        assert (
            b"## Skill Compaction Contract\n" + f"verified for session {sid} at PreCompact\n".encode() + contract
            in snapshot
        )
        assert re.search(rb"<untrusted-[a-f0-9]+>\n.*</untrusted-[a-f0-9]+>", snapshot, re.DOTALL)
        check_logs(logs)
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
        """Successive pre/post events preserve earlier bytes and append exactly one row each.

        Four alternating events share one log; each must extend the file by a single newline-terminated JSON row of its
        own event type, never rewrite what came before.
        """
        events = ["PreCompact", "PostCompact", "PreCompact", "PostCompact"]

        results, snapshots = _compaction_events(run_hook, sid, tmp_home, events)

        pairs = list(zip([b"", *snapshots], snapshots))
        appended = [current[len(previous) :] for previous, current in pairs]
        assert all(r.returncode == 0 and r.stdout == r.stderr == "" for r in results)
        assert all(current.startswith(previous) for previous, current in pairs)
        assert all(rows.endswith(b"\n") and len(rows.splitlines()) == 1 for rows in appended)
        assert [json.loads(rows)["event"] for rows in appended] == [event.lower() for event in events]
        assert len(snapshots[-1].splitlines()) == 4

    def test_postcompact_registration(self) -> None:
        """PostCompact uses exactly the PreCompact task logger's node probe, without a matcher.

        Reads the shipped hooks.json: PostCompact must register the same group as PreCompact's task-log entry so both
        events reach the one logger.
        """
        hooks = json.loads((Path(__file__).parents[3] / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
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
                ["node", str(Path(__file__).resolve().parents[3] / "hooks" / "task-log.js")],
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
        """Credentials in a Bash command never reach timings.jsonl; plain paths and words survive.

        Each case is a distinct place a secret hides (assignments, exports, headers, URLs, flags, known token prefixes);
        the long-path case guards against masking harmless long strings.
        """
        assert self._timing_args(tmp_home, sid, "Bash", {"command": command}) == expected

    def test_edit_path_with_secret_lookalikes_is_untouched(self, sid: str, tmp_home: Path) -> None:
        """A path containing ``=`` or ``sk-`` stays byte-identical: PreCompact and the profiler parse it.

        Masking the parsed ``file_path`` field would corrupt the file list that PreCompact rebuilds from the log.
        """
        path = "/x/GH_TOKEN=abc def/sk-abcdefghijkl.py"
        assert self._timing_args(tmp_home, sid, "Edit", {"file_path": path}) == f"file_path={path}"

    def test_skill_args_masked_after_parsed_prefix(self, sid: str, tmp_home: Path) -> None:
        """Skill arguments are masked while the parsed ``skill=`` prefix stays intact.

        The skill name is a parsed field that other tools read back; only the free-text ``args`` part may be masked.
        """
        skill = self._timing_args(tmp_home, sid, "Skill", {"skill": "x:y", "args": "--token abc123 go"})
        assert skill == "skill=x:y args=--token [masked] go"

    def test_generic_json_summary_masked(self, sid: str, tmp_home: Path) -> None:
        """A tool without a dedicated summary falls back to JSON, and credentials inside it are masked too.

        A URL query parameter ``access_token`` in a WebFetch input must not appear in the logged summary.
        """
        other = self._timing_args(tmp_home, sid, "WebFetch", {"url": "https://h/?access_token=abc"})
        assert "abc" not in other

    @pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
    def test_existing_logs_dir_and_files_tightened(self, sid: str, tmp_home: Path) -> None:
        """A pre-existing 0755 logs dir and 0644 log file are tightened to 0700 / 0600.

        Logs from an older version may be world-readable; the next hook run must lock them down rather than only
        protecting newly created files.
        """
        logs = tmp_home / ".claude" / "logs"
        logs.chmod(0o755)
        log = logs / "timings.jsonl"
        log.write_text("", encoding="utf-8")
        log.chmod(0o644)

        self._timing_args(tmp_home, sid, "Bash", {"command": "echo hi"})

        assert logs.stat().st_mode & 0o777 == 0o700
        assert log.stat().st_mode & 0o777 == 0o600


def _assert_saved_contract(saved: bytes, raw: bytes, status: str) -> None:
    """Require exact owned bytes inside an untrusted boundary and omit all unverified bodies."""
    if status == "unverified at PreCompact":
        assert raw not in saved
        assert b"## Skill Compaction Contract" not in saved
        return
    assert saved.count(b"## Skill Compaction Contract") == 1
    assert b"## Skill Compaction Contract\n" + status.encode() + b"\n" + raw in saved
    match = re.search(rb"<(untrusted-[a-f0-9]+)>\n(.*?)\n</\1>\n$", saved, re.DOTALL)
    assert match is not None
    assert match[2].endswith(raw)


class TestPreCompactContract:
    """task-log.js: PreCompact writes session-context.md and preserves the skill contract verbatim.

    PreCompact writes ``<cwd>/.claude/state/session-context.md`` (cwd = project root),
    not the ephemeral ``claude-state-<sid>`` dir — so these tests pass ``cwd`` and
    read the context file from that project-local state dir. The skill contract is
    staged by skills at ``<cwd>/.temp/state/skill-contract.md`` (relocated out of
    ``.claude/state/`` to dodge the sensitive-file gate), which is where the hook reads it.
    """

    def test_precompact_appends_contract_verbatim(self, sid: str, tmp_home: Path, tmp_path: Path, run_hook) -> None:
        """A staged skill-contract.md is appended verbatim under its own section alongside Files Modified.

        Skills stage phase state before a compaction; the summary prose is lossy, so the contract must survive as-is.
        """
        proj = tmp_path / "proj"
        state = proj / ".claude" / "state"
        state.mkdir(parents=True)
        contract_dir = proj / ".temp" / "state"
        contract_dir.mkdir(parents=True)
        transcript = tmp_path / "transcript.jsonl"
        _write_transcript(transcript, ["/proj/src/foo.py"])
        contract = "CONTRACT: keep phase 3 in-progress; do NOT re-run steps 1-2.\n- next: verify tests"
        (contract_dir / "skill-contract.md").write_text(contract, encoding="utf-8")
        (contract_dir / "skill-contract.owner").write_text(
            json.dumps(_owner_record(sid, contract.encode())), encoding="utf-8"
        )

        result = run_hook("task-log.js", _pre_compact(sid, str(transcript)), home=tmp_home, cwd=proj)

        assert result.returncode == 0, result.stderr
        written = (state / "session-context.md").read_text(encoding="utf-8")
        assert "## Files Modified This Session" in written
        assert "- /proj/src/foo.py" in written
        assert "## Skill Compaction Contract" in written
        assert contract in written

    def test_precompact_no_contract_writes_files_only(self, sid: str, tmp_home: Path, tmp_path: Path, run_hook) -> None:
        """With no skill-contract.md, PreCompact writes Files Modified and no contract section, no error.

        Most compactions happen outside any skill run; the absent contract must be a silent omission.
        """
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
        """Edits older than the 50KB transcript tail are listed from timings.jsonl, scoped to this session.

        The transcript is 200KB of filler, so its tail holds no edits; the file list must come from the timing log, with
        read-only tools and other sessions' edits left out.
        """
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
        """MultiEdit and NotebookEdit write a ``file_path=`` args summary so PreCompact can find them.

        NotebookEdit's input names the file ``notebook_path``; the log must normalise it to the ``file_path=`` form the
        compaction file list parses.
        """
        base = {"session_id": sid, "tool_name": tool, "tool_input": tool_input, "tool_use_id": f"tu-{tool}"}
        run_hook("task-log.js", {**base, "hook_event_name": "PreToolUse"}, home=tmp_home)
        run_hook("task-log.js", {**base, "hook_event_name": "PostToolUse"}, home=tmp_home)

        rows = [
            json.loads(line)
            for line in (tmp_home / ".claude" / "logs" / "timings.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert [r["args"] for r in rows if r["tool"] == tool] == [expected_args]

    @pytest.mark.parametrize(
        ("stage_owner", "owner_text", "status"),
        [
            pytest.param(
                _stage_text,
                lambda sid, raw: json.dumps(_owner_record(sid, raw)),
                "verified for session {sid} at PreCompact",
                id="same-session",
            ),
            pytest.param(_stage_nothing, lambda sid, raw: "", "unverified at PreCompact", id="no-owner"),
            pytest.param(
                _stage_text,
                lambda sid, raw: json.dumps(_owner_record(sid, raw, sha256="0" * 64)),
                "unverified at PreCompact",
                id="sha-mismatch",
            ),
            pytest.param(_stage_text, lambda sid, raw: "{broken", "unverified at PreCompact", id="malformed-owner"),
            pytest.param(_stage_text, lambda sid, raw: json.dumps(None), "unverified at PreCompact", id="null-owner"),
            pytest.param(
                _stage_text,
                _owner_without_session,
                "unverified at PreCompact",
                id="incomplete-owner",
            ),
            pytest.param(_stage_directory, lambda sid, raw: "", "unverified at PreCompact", id="owner-is-directory"),
        ],
    )
    def test_precompact_owner_states(
        self,
        sid: str,
        tmp_home: Path,
        tmp_path: Path,
        run_hook,
        stage_owner: Callable[[Path, str], None],
        owner_text: Callable[[str, bytes], str],
        status: str,
    ) -> None:
        """PreCompact preserves matching session-owned bytes and omits contracts without valid ownership.

        A missing, stale, malformed, incomplete or unreadable owner stamp cannot carry repository instructions forward.
        """
        raw = b"  CONTRACT: keep whitespace\r\nnext: resume\n\n"
        project, state, transcript = _stage_contract(tmp_path, raw)
        stage_owner(state / "skill-contract.owner", owner_text(sid, raw))

        result = run_hook("task-log.js", _pre_compact(sid, str(transcript)), home=tmp_home, cwd=project)

        assert result.returncode == 0, result.stderr
        saved = (project / ".claude" / "state" / "session-context.md").read_bytes()
        assert b"## Files Modified This Session" in saved
        _assert_saved_contract(saved, raw, status.format(sid=sid))

    def test_precompact_foreign_owner_omits_contract(self, sid: str, tmp_home: Path, tmp_path: Path, run_hook) -> None:
        """A contract whose owner stamp matches its bytes but names another session is not carried over.

        Another session's phase state must never be injected into this session's compaction snapshot.
        """
        raw = b"  CONTRACT: keep whitespace\r\nnext: resume\n\n"
        project, state, transcript = _stage_contract(tmp_path, raw)
        owner = json.dumps(_owner_record("foreign-session", raw))
        (state / "skill-contract.owner").write_text(owner, encoding="utf-8")

        result = run_hook("task-log.js", _pre_compact(sid, str(transcript)), home=tmp_home, cwd=project)

        assert result.returncode == 0, result.stderr
        saved = (project / ".claude" / "state" / "session-context.md").read_bytes()
        assert b"## Files Modified This Session" in saved
        assert b"## Skill Compaction Contract" not in saved
        assert raw not in saved


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


@pytest.fixture
def managed_worktrees(tmp_path: Path) -> Path:
    """Build a git project with one worktree of each kind the sweep must tell apart; return the project root.

    Old clean agent-/oss- trees are idle; dev-/manual- trees are not managed; agent-dirty has an edit; agent-output
    holds an untracked deliverable; agent-fresh was just used; agent-orphan is a plain directory unknown to git.
    """
    proj = tmp_path / "proj"
    proj.mkdir()
    _git(proj, "init", "-q")
    (proj / ".gitignore").write_text(".temp/\n", encoding="utf-8")
    (proj / "a.txt").write_text("a\n", encoding="utf-8")
    _git(proj, "add", ".gitignore", "a.txt")
    _git(proj, "commit", "-q", "-m", "init")
    wt_root = proj / ".claude" / "worktrees"
    idle = ["agent-old", "oss-old", "dev-old", "manual-old", "agent-dirty", "agent-output"]
    for name in [*idle, "agent-fresh"]:
        _git(proj, "worktree", "add", "-q", "-b", name, str(wt_root / name), "HEAD")
    (wt_root / "agent-dirty" / "a.txt").write_text("edited\n", encoding="utf-8")
    (wt_root / "agent-output" / ".temp").mkdir()
    (wt_root / "agent-output" / ".temp" / "extract.md").write_text("result\n", encoding="utf-8")
    (wt_root / "agent-orphan").mkdir()
    for name in idle:
        _age_worktree(wt_root / name, 3 * 60 * 60)
    stamp = time.time() - 3 * 60 * 60
    os.utime(wt_root / "agent-orphan", (stamp, stamp))
    return proj


@pytest.mark.integration
@_requires_git
class TestSessionEndWorktreeCleanup:
    """task-log.js SessionEnd: stale-worktree sweep never destroys user deliverables or pending work."""

    def test_sweep_removes_only_idle_clean_managed_trees(
        self, sid: str, tmp_home: Path, managed_worktrees: Path, run_hook
    ) -> None:
        """Only an idle, clean, registered agent-*/oss-* tree is removed; every other kind survives.

        Real git worktrees are used because the sweep consults git's own registry and status; deleting a dirty tree, an
        untracked deliverable, a fresh tree or an unmanaged name would destroy someone's work.
        """
        wt_root = managed_worktrees / ".claude" / "worktrees"

        result = run_hook(
            "task-log.js", {"hook_event_name": "SessionEnd", "session_id": sid}, home=tmp_home, cwd=managed_worktrees
        )

        assert result.returncode == 0, result.stderr
        assert not (wt_root / "agent-old").exists()
        assert not (wt_root / "oss-old").exists()
        assert (wt_root / "dev-old").is_dir()
        assert (wt_root / "manual-old").is_dir()
        assert (wt_root / "agent-dirty" / "a.txt").read_text(encoding="utf-8") == "edited\n"
        assert (wt_root / "agent-output" / ".temp" / "extract.md").is_file()
        assert (wt_root / "agent-fresh").is_dir()
        assert (wt_root / "agent-orphan").is_dir()


def _plant_oversize(candidate: Path, content: bytes, cap: int) -> None:
    """Replace ``candidate`` with a regular file one byte over ``cap``."""
    candidate.write_bytes(content + b" " * (cap + 1 - len(content)))


def _plant_symlink(candidate: Path, content: bytes, cap: int) -> None:
    """Replace ``candidate`` with a symlink to a sibling file holding ``content``."""
    backing = candidate.parent / "backing"
    backing.write_bytes(content)
    candidate.symlink_to(backing)


def _plant_fifo(candidate: Path, content: bytes, cap: int) -> None:
    """Replace ``candidate`` with a named pipe, which blocks a naive reader forever."""
    os.mkfifo(candidate)


_UNSAFE_KINDS = [
    pytest.param(_plant_oversize, id="oversize"),
    pytest.param(
        _plant_symlink,
        marks=pytest.mark.skipif(not _contract_path_capability("symlink"), reason="symlink creation unavailable"),
        id="symlink",
    ),
    pytest.param(
        _plant_fifo,
        marks=pytest.mark.skipif(not _contract_path_capability("fifo"), reason="FIFO creation unavailable"),
        id="fifo",
    ),
]

_requires_node = pytest.mark.skipif(shutil.which("node") is None, reason="node executable not available")


def _precompact_session_context(tmp_path: Path, project: Path) -> str:
    """Run PreCompact through real node with a finite deadline and return the session-context.md text it wrote."""
    transcript = project / "transcript.jsonl"
    transcript.write_bytes(b'{"type":"user","message":{"content":"hello"}}\n')
    payload = {
        "session_id": "safe-session",
        "hook_event_name": "PreCompact",
        "source": "compact",
        "transcript_path": str(transcript),
    }
    result = subprocess.run(
        ["node", str(Path(__file__).resolve().parents[3] / "hooks" / "task-log.js")],
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
    return (project / ".claude" / "state" / "session-context.md").read_text(encoding="utf-8")


@pytest.fixture
def safe_contract(tmp_path: Path) -> tuple[Path, Path, Path, bytes, bytes]:
    """Stage a valid contract and matching owner stamp; return project, contract path, owner path, contract, stamp."""
    project = tmp_path / "project"
    state = project / ".temp" / "state"
    state.mkdir(parents=True)
    raw = b"CONTRACT: continue safely\n"
    stamp = json.dumps({"session_id": "safe-session", "sha256": hashlib.sha256(raw).hexdigest()}).encode()
    contract = state / "skill-contract.md"
    owner = state / "skill-contract.owner"
    contract.write_bytes(raw)
    owner.write_bytes(stamp)
    return project, contract, owner, raw, stamp


@_requires_node
class TestUnsafeContractPaths:
    """PreCompact fails open on oversized, linked or FIFO contract and owner files instead of blocking compaction."""

    @pytest.mark.parametrize("plant", _UNSAFE_KINDS)
    def test_unsafe_contract_is_dropped(self, tmp_path: Path, safe_contract: tuple, plant: Callable) -> None:
        """An oversized, symlinked or FIFO skill-contract.md is rejected and nothing of it reaches the snapshot.

        The hook must neither read an unbounded or blocking file nor fail the compaction; the snapshot simply carries no
        contract section.
        """
        project, contract, _owner, raw, _stamp = safe_contract
        contract.unlink()
        plant(contract, raw, 256 * 1024)

        output = _precompact_session_context(tmp_path, project)

        assert "CONTRACT:" not in output
        assert "Skill contract," not in output
        assert "## Skill Compaction Contract" not in output

    @pytest.mark.parametrize("plant", _UNSAFE_KINDS)
    def test_unsafe_owner_omits_contract(self, tmp_path: Path, safe_contract: tuple, plant: Callable) -> None:
        """An oversized, symlinked or FIFO owner stamp cannot authorize carrying a contract into context.

        The stamp is advisory provenance; an unsafe stamp path must not block compaction nor pass for a verified owner.
        """
        project, _contract, owner, _raw, stamp = safe_contract
        owner.unlink()
        plant(owner, stamp, 4 * 1024)

        output = _precompact_session_context(tmp_path, project)

        assert "CONTRACT: continue safely" not in output
        assert "## Skill Compaction Contract" not in output
        assert "Skill contract, verified:" not in output
        assert "verified for session" not in output

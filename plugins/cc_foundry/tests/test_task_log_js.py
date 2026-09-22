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

import json
import shutil
from collections.abc import Iterator
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

    def test_subagent_start_without_tool_use_id_rekeys_matched_pending(
        self, sid: str, tmp_home: Path, run_hook, state_dir
    ) -> None:
        """SubagentStart payload omitting tool_use_id still re-keys via the agent_type pending scan.

        Some SubagentStart payloads carry agent_type but not tool_use_id — the hook falls back to scanning pending/ for
        the most recent entry matching agent_type. That fallback must re-key the matched agents/<tool_use_id>.json to
        agents/<agent_id>.json too, the same as the direct tool_use_id match path.
        """
        tool_use_id = "tu-typematch"
        agent_id = "agent-typematch"
        run_hook("task-log.js", _pre_agent(sid, tool_use_id, subagent_type="foundry:qa-specialist"), home=tmp_home)

        result = run_hook(
            "task-log.js",
            _subagent_start(sid, agent_id, agent_type="foundry:qa-specialist"),
            home=tmp_home,
        )

        assert result.returncode == 0, result.stderr
        assert not (state_dir(sid) / "agents" / f"{tool_use_id}.json").exists()
        assert (state_dir(sid) / "agents" / f"{agent_id}.json").exists()

    def test_subagent_start_named_agent_matches_by_name_not_type(
        self, sid: str, tmp_home: Path, run_hook, state_dir
    ) -> None:
        """A named agent's tool_use_id-less SubagentStart matches pending/ via name, not subagent_type.

        Live-observed shape: SubagentStart's ``agent_type`` field carries the assigned *name* for a
        named Agent() call, not its subagent_type. Matching only on subagent_type would miss it,
        leaving agents/<tool_use_id>.json un-re-keyed — the exact leak renameAgentFile exists to close.
        """
        tool_use_id = "tu-named"
        agent_id = "agent-named"
        run_hook(
            "task-log.js",
            _pre_agent(sid, tool_use_id, subagent_type="foundry:challenger", name="post-fix-challenger-2"),
            home=tmp_home,
        )

        result = run_hook(
            "task-log.js",
            _subagent_start(sid, agent_id, agent_type="post-fix-challenger-2"),
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

    def test_pre_tool_use_bash_creates_counter(self, sid: str, tmp_home: Path, run_hook, state_dir) -> None:
        """First Bash PreToolUse creates tools/Bash.json with count=1."""
        result = run_hook("task-log.js", _pre_bash(sid, "tu-bash-1"), home=tmp_home)

        assert result.returncode == 0, result.stderr
        counter = state_dir(sid) / "tools" / "Bash.json"
        assert counter.exists()
        data = json.loads(counter.read_text(encoding="utf-8"))
        assert data["count"] == 1
        assert data["tool"] == "Bash"

    def test_pre_tool_use_bash_increments_counter(self, sid: str, tmp_home: Path, run_hook, state_dir) -> None:
        """Second Bash PreToolUse increments count to 2 (within 30s window)."""
        run_hook("task-log.js", _pre_bash(sid, "tu-bash-a"), home=tmp_home)
        result = run_hook("task-log.js", _pre_bash(sid, "tu-bash-b"), home=tmp_home)

        assert result.returncode == 0, result.stderr
        data = json.loads((state_dir(sid) / "tools" / "Bash.json").read_text(encoding="utf-8"))
        assert data["count"] == 2

    def test_counter_stamps_last_and_inflight(self, sid: str, tmp_home: Path, run_hook, state_dir) -> None:
        """PreToolUse records ``last`` (this call's time) and opens one ``inflight`` slot.

        ``since`` is a fixed window start and goes stale while calls keep landing inside the window, so the status line
        needs a separate per-call stamp to decide freshness.
        """
        run_hook("task-log.js", _pre_bash(sid, "tu-bash-l1"), home=tmp_home)

        data = json.loads((state_dir(sid) / "tools" / "Bash.json").read_text(encoding="utf-8"))
        assert data["last"] >= data["since"]
        assert data["inflight"] == 1

    def test_second_call_advances_last_but_not_since(self, sid: str, tmp_home: Path, run_hook, state_dir) -> None:
        """A second call inside the 30s window keeps ``since`` fixed and moves ``last`` forward."""
        run_hook("task-log.js", _pre_bash(sid, "tu-bash-l2a"), home=tmp_home)
        first = json.loads((state_dir(sid) / "tools" / "Bash.json").read_text(encoding="utf-8"))
        run_hook("task-log.js", _pre_bash(sid, "tu-bash-l2b"), home=tmp_home)

        second = json.loads((state_dir(sid) / "tools" / "Bash.json").read_text(encoding="utf-8"))
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

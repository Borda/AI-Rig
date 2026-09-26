"""Subprocess tests for ``hooks/agent-router.js``.

The router implements three-tier fallback for ``Agent()`` calls:

* **Tier 1** — exact name match against either the plugin agent cache
  (``~/.claude/plugins/cache/<vendor>/<namespace>/<version>/agents/``)
  or the local agents directory → passthrough (no stdout).
* **Tier 2** — semantic match via OpenAI embeddings or Anthropic LLM
  pick. Both API keys are stripped by the ``run_hook`` fixture so this
  tier is unreachable in the suite, ensuring deterministic fallthrough.
* **Tier 3** — no fit → reroute to ``general-purpose`` via a JSON
  ``hookSpecificOutput`` block on stdout.

A separate ``SessionStart`` event builds the in-tmp routing index;
that is also exercised here.
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
    """Return an isolated HOME with a stubbed foundry plugin cache containing one agent.

    Writes ``~/.claude/plugins/cache/borda-ai-rig/foundry/0.0.1/agents/sw-engineer.md``
    so the router's tier-1 lookup recognises ``foundry:sw-engineer`` as a
    known plugin agent and passes the call through unchanged.
    """
    h = tmp_path / "home"
    (h / ".claude" / "agents").mkdir(parents=True)
    plugin_agents = h / ".claude" / "plugins" / "cache" / "borda-ai-rig" / "foundry" / "0.0.1" / "agents"
    plugin_agents.mkdir(parents=True)
    (plugin_agents / "sw-engineer.md").write_text(
        "---\nname: sw-engineer\ndescription: stub agent for tests\n---\n",
        encoding="utf-8",
    )
    return h


# ── Payload helpers ──────────────────────────────────────────────────────────


def _pre_agent(subagent_type: str, session_id: str) -> dict:
    """Build a ``PreToolUse(Agent)`` payload for the given subagent_type.

    Examples:
        >>> _pre_agent("foundry:sw-engineer", "s")["tool_input"]["subagent_type"]
        'foundry:sw-engineer'
    """
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Agent",
        "tool_input": {"subagent_type": subagent_type, "description": "test", "prompt": "p"},
        "session_id": session_id,
    }


def _session_start(session_id: str) -> dict:
    """Build a ``SessionStart`` payload.

    Examples:
        >>> _session_start("s") == {"hook_event_name": "SessionStart", "session_id": "s"}
        True
    """
    return {"hook_event_name": "SessionStart", "session_id": session_id}


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestAgentRouting:
    """agent-router.js: tier-1 passthrough vs tier-3 fallback to general-purpose."""

    def test_builtin_agent_passes_through(self, sid: str, tmp_home: Path, run_hook) -> None:
        """Built-in 'general-purpose' is recognised; hook exits with empty stdout."""
        result = run_hook("agent-router.js", _pre_agent("general-purpose", sid), home=tmp_home)

        assert result.returncode == 0, result.stderr
        assert result.stdout == ""

    def test_claude_catchall_passes_through(self, sid: str, tmp_home: Path, run_hook) -> None:
        """Built-in 'claude' catch-all is recognised; no silent reroute to general-purpose."""
        result = run_hook("agent-router.js", _pre_agent("claude", sid), home=tmp_home)

        assert result.returncode == 0, result.stderr
        assert result.stdout == ""

    def test_known_plugin_agent_passes_through(self, sid: str, tmp_home: Path, run_hook) -> None:
        """Known plugin agent 'foundry:sw-engineer' resolves via tier 1; stdout empty."""
        result = run_hook("agent-router.js", _pre_agent("foundry:sw-engineer", sid), home=tmp_home)

        assert result.returncode == 0, result.stderr
        assert result.stdout == ""

    def test_unknown_agent_rerouted_to_general_purpose(self, sid: str, tmp_home: Path, run_hook) -> None:
        """Unknown agent triggers tier-3 fallback; stdout JSON sets subagent_type=general-purpose."""
        result = run_hook(
            "agent-router.js",
            _pre_agent("nonexistent:agent-xyz", sid),
            home=tmp_home,
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout, "tier-3 fallback must emit JSON"
        envelope = json.loads(result.stdout)
        updated = envelope["hookSpecificOutput"]["updatedInput"]
        assert updated["subagent_type"] == "general-purpose"

    def test_session_start_builds_index(self, sid: str, tmp_home: Path, run_hook, state_dir) -> None:
        """SessionStart writes agent-router-index.json with plugin agents enumerated."""
        result = run_hook("agent-router.js", _session_start(sid), home=tmp_home)

        assert result.returncode == 0, result.stderr
        index_path = state_dir(sid) / "agent-router-index.json"
        assert index_path.exists()
        index = json.loads(index_path.read_text(encoding="utf-8"))
        assert "plugin_agents" in index
        assert "local_agents" in index
        assert "foundry:sw-engineer" in index["plugin_agents"]

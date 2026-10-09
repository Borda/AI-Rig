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

The tier-2B request builder and response parser are pure helpers, exercised
by requiring the hook as a module under ``node -e`` — no network, no API key.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from _hook_env import _hook_tmp_base

HOOK = Path(__file__).resolve().parent.parent.parent / "hooks" / "agent-router.js"

NODE_UNAVAILABLE = shutil.which("node") is None
_skip_node_unavailable = pytest.mark.skipif(NODE_UNAVAILABLE, reason="requires node to execute the hook")

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

    @pytest.mark.parametrize(
        "subagent_type",
        [
            pytest.param("general-purpose", id="builtin-general-purpose"),
            pytest.param("claude", id="builtin-claude-catchall"),
            pytest.param("foundry:sw-engineer", id="known-plugin-agent"),
        ],
    )
    def test_recognised_agent_passes_through(self, sid: str, tmp_home: Path, run_hook, subagent_type: str) -> None:
        """A recognised agent resolves via tier 1; the hook exits 0 with empty stdout.

        Scenario: built-in 'general-purpose', the built-in 'claude' catch-all (no silent reroute to general-purpose) and
        the known plugin agent 'foundry:sw-engineer' are all recognised.
        """
        result = run_hook("agent-router.js", _pre_agent(subagent_type, sid), home=tmp_home)

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


# ── Tier-2B LLM pick helpers ──────────────────────────────────────────────────


def _node_call(fn: str, *args: object) -> dict:
    """Call one exported hook helper under node; return ``{"ok": value}`` or ``{"error": message}``."""
    call_args = ", ".join(json.dumps(a) for a in args)
    script = (
        f"const h = require({json.dumps(str(HOOK))});"
        f"let out; try {{ out = {{ ok: h.{fn}({call_args}) }}; }} catch (e) {{ out = {{ error: e.message }}; }}"
        "process.stdout.write(JSON.stringify(out));"
    )
    proc = subprocess.run(["node", "-e", script], capture_output=True, encoding="utf-8", timeout=10, check=False)
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout)


def _node_constant(name: str) -> object:
    """Read one exported hook constant under node."""
    script = f"process.stdout.write(JSON.stringify(require({json.dumps(str(HOOK))}).{name}));"
    proc = subprocess.run(["node", "-e", script], capture_output=True, encoding="utf-8", timeout=10, check=False)
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout)


def _node_resolve(env: dict[str, str], fetch_body: str, now_ms: str = "Date.now()") -> dict:
    """Run ``resolveHaikuModel`` under node with an injected Models API stub; return its result plus logs and calls.

    ``fetch_body`` is the JavaScript body of the stub ``fetchModels(apiKey)`` — it may return a ``{statusCode, raw}``
    object or throw. ``env`` replaces ``process.env`` for the resolver, so ``TMPDIR`` points the cache at ``tmp_path``.
    """
    script = (
        f"const h = require({json.dumps(str(HOOK))});"
        "const logs = []; let calls = 0;"
        f"const fetchModels = async (apiKey) => {{ calls += 1; {fetch_body} }};"
        f"h.resolveHaikuModel({{ env: {json.dumps(env)}, apiKey: 'k', nowMs: {now_ms}, fetchModels,"
        " log: (m) => logs.push(m) })"
        ".then((r) => process.stdout.write(JSON.stringify({ ...r, logs, calls })));"
    )
    proc = subprocess.run(["node", "-e", script], capture_output=True, encoding="utf-8", timeout=10, check=False)
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout)


def _models_response(*entries: tuple[str, str | None]) -> str:
    """Build a JavaScript stub body returning a 200 Models API page listing ``(id, line)`` entries in order."""
    raw = json.dumps({"data": [{"type": "model", "id": i, "line": line} for i, line in entries], "has_more": False})
    return f"return {{ statusCode: 200, raw: {json.dumps(raw)} }};"


_MODELS_MUST_NOT_BE_CALLED = "throw new Error('Models API must not be called');"


@_skip_node_unavailable
class TestLlmRequest:
    """BuildLlmRequest: the Messages API body sent for the tier-2B pick."""

    def test_uses_resolved_model_id_with_thinking_disabled(self) -> None:
        """The body carries the runtime-resolved model id, disables adaptive thinking and leaves room past a reply.

        The Claude Code alias ``haiku`` is not an API model ID: every request with it failed, and the old parser turned
        that failure into a silent "none". Thinking tokens count toward ``max_tokens`` on Haiku 5.5, so thinking stays
        disabled and the budget sits well above the reply length.
        """
        got = _node_call("buildLlmRequest", [{"name": "a", "description": "does a"}], "query", "claude-haiku-9")

        body = got["ok"]
        assert body["model"] == "claude-haiku-9"
        assert body["thinking"] == {"type": "disabled"}
        assert body["max_tokens"] >= 256
        assert "- a: does a" in body["messages"][0]["content"]


@_skip_node_unavailable
class TestHaikuModelResolution:
    """ResolveHaikuModel: env override, then 24h cache, then the Models API by ``line``, then a logged fallback."""

    def test_env_override_wins_without_lookup(self, tmp_path: Path) -> None:
        """``ANTHROPIC_DEFAULT_HAIKU_MODEL`` is used as-is; neither the cache nor the Models API is consulted.

        The variable is Claude Code's own override for the ``haiku`` alias, so honouring it keeps the router on the same
        model the user pinned for every other haiku-tier call.
        """
        env = {"ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-haiku-9", "TMPDIR": str(tmp_path)}

        got = _node_resolve(env, _MODELS_MUST_NOT_BE_CALLED)

        assert (got["model"], got["source"], got["calls"]) == ("claude-haiku-9", "env", 0)

    def test_fresh_cache_hit_skips_models_api(self, tmp_path: Path) -> None:
        """A cache file younger than 24h answers without a network call."""
        (tmp_path / "agent-router-haiku-model-shared").write_text("claude-haiku-7-1\n", encoding="utf-8")

        got = _node_resolve({"TMPDIR": str(tmp_path)}, _MODELS_MUST_NOT_BE_CALLED)

        assert (got["model"], got["source"], got["calls"]) == ("claude-haiku-7-1", "cache", 0)

    def test_stale_cache_refreshed_from_models_api(self, tmp_path: Path) -> None:
        """A cache file older than 24h is ignored, the Models API answers, and the cache is rewritten.

        The TTL is what lets a newer Haiku release replace the cached id within a day without any code change.
        """
        cache = tmp_path / "agent-router-haiku-model-shared"
        cache.write_text("claude-haiku-old\n", encoding="utf-8")
        day_and_a_bit_later = f"{cache.stat().st_mtime * 1000} + 25 * 3600 * 1000"

        got = _node_resolve(
            {"TMPDIR": str(tmp_path)}, _models_response(("claude-haiku-8", "haiku")), now_ms=day_and_a_bit_later
        )

        assert (got["model"], got["source"]) == ("claude-haiku-8", "models-api")
        assert cache.read_text(encoding="utf-8").strip() == "claude-haiku-8"

    def test_models_api_pick_uses_line_not_id(self, tmp_path: Path) -> None:
        """The first listed model whose ``line`` is ``haiku`` wins; an id that only mentions haiku does not qualify.

        The API lists newest releases first and documents ``line`` as the family signal — the id must not be parsed.
        """
        page = _models_response(
            ("claude-sonnet-6", "sonnet"),
            ("claude-haiku-preview", None),
            ("claude-haiku-6", "haiku"),
            ("claude-haiku-5-5", "haiku"),
        )

        got = _node_resolve({"TMPDIR": str(tmp_path)}, page)

        assert (got["model"], got["source"], got["calls"]) == ("claude-haiku-6", "models-api", 1)
        assert (tmp_path / "agent-router-haiku-model-shared").read_text(encoding="utf-8").strip() == "claude-haiku-6"

    @pytest.mark.parametrize(
        "fetch_body",
        [
            pytest.param("throw new Error('ECONNRESET');", id="network-error"),
            pytest.param(
                "return { statusCode: 401, raw: JSON.stringify({ type: 'error', error: { type: 'authentication_error' } }) };",
                id="http-401",
            ),
            pytest.param("return { statusCode: 200, raw: JSON.stringify({ data: [] }) };", id="no-haiku-line"),
        ],
    )
    def test_lookup_failure_falls_back_and_logs(self, tmp_path: Path, fetch_body: str) -> None:
        """Every lookup failure lands on the latest-known constant, logs why, and leaves no cache behind.

        Caching the fallback would pin it for a day; leaving the cache empty retries the lookup on the next call.
        """
        fallback = _node_constant("FALLBACK_HAIKU_MODEL")

        got = _node_resolve({"TMPDIR": str(tmp_path)}, fetch_body)

        assert (got["model"], got["source"]) == (fallback, "fallback")
        assert any(f"using fallback {fallback}" in line for line in got["logs"])
        assert not (tmp_path / "agent-router-haiku-model-shared").exists()

    @pytest.mark.parametrize(
        ("env", "suffix"),
        [
            pytest.param({"CSID": "abc-123"}, "abc-123", id="csid"),
            pytest.param({"CLAUDE_CODE_SESSION_ID": "s_9"}, "s_9", id="session-id"),
            pytest.param({"CSID": "../escape"}, "shared", id="unsafe-token"),
            pytest.param({}, "shared", id="no-token"),
        ],
    )
    def test_cache_path_is_session_scoped(self, tmp_path: Path, env: dict[str, str], suffix: str) -> None:
        """The cache file sits in ``TMPDIR`` with a session suffix; a token unsafe for a filename becomes ``shared``."""
        got = _node_call("haikuModelCachePath", {**env, "TMPDIR": str(tmp_path)})

        assert Path(got["ok"]) == tmp_path / f"agent-router-haiku-model-{suffix}"


@_skip_node_unavailable
class TestLlmResponse:
    """ParseLlmResponse: picked name on success, a thrown error on every failure shape."""

    def test_selects_first_text_block_after_thinking(self) -> None:
        """A response opening with a thinking block still yields the text block's answer, trimmed and lowercased.

        Position-based selection (``content[0].text``) reads the thinking block, finds no ``text`` and returned "none".
        """
        raw = json.dumps(
            {
                "type": "message",
                "content": [{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "  Code-Reviewer \n"}],
            }
        )

        got = _node_call("parseLlmResponse", 200, raw)

        assert got == {"ok": "code-reviewer"}

    @pytest.mark.parametrize(
        ("status", "raw", "fragment"),
        [
            pytest.param(
                400,
                json.dumps(
                    {
                        "type": "error",
                        "error": {"type": "invalid_request_error", "message": "model: haiku not found"},
                    }
                ),
                "HTTP 400 invalid_request_error: model: haiku not found",
                id="http-400-error-body",
            ),
            pytest.param(
                200,
                json.dumps({"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}),
                "overloaded_error",
                id="http-200-error-body",
            ),
            pytest.param(503, "<html>gateway</html>", "not JSON", id="non-json-body"),
            pytest.param(
                200,
                json.dumps({"type": "message", "content": [{"type": "thinking"}], "stop_reason": "max_tokens"}),
                "no text block (stop_reason: max_tokens)",
                id="no-text-block",
            ),
            pytest.param(200, "null", "HTTP 200", id="null-body"),
        ],
    )
    def test_failure_raises_instead_of_none(self, status: int, raw: str, fragment: str) -> None:
        """Every failed request throws with status and error detail rather than returning a silent "none".

        "none" is a legitimate model answer (no agent fits); a broken request must stay distinguishable from it so the
        hook can log the failure before failing open to tier 3.
        """
        got = _node_call("parseLlmResponse", status, raw)

        assert "ok" not in got
        assert fragment in got["error"]

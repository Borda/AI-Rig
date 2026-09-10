"""Acceptance checks for the bridge's shipped contracts."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCHEMAS_ROOT = PLUGIN_ROOT / "schemas"

CORE_SCHEMA_PATH = SCHEMAS_ROOT / "envelope.schema.json"
HARNESS_SCHEMA_PATH = SCHEMAS_ROOT / "harness-envelope.schema.json"
MCP_SCHEMA_PATH = SCHEMAS_ROOT / "mcp-tools.schema.json"
SETUP_SCHEMA_PATH = SCHEMAS_ROOT / "setup-result.schema.json"
MCP_CONFIG_PATH = PLUGIN_ROOT / ".mcp.json"

CORE_FIELDS = {"status", "verdict", "findings", "files_touched", "remaining", "blockers"}
PEER_FIELDS = CORE_FIELDS | {"details"}
HARNESS_ONLY_FIELDS = {
    "model",
    "effort",
    "effort_substituted",
    "cost",
    "tokens",
    "duration_seconds",
    "depth",
    "run_id",
    "incident",
    "session_id",
    "transcript_path",
    "verb",
    "direction",
}

CODEX_SKILL_CONTRACTS = {
    "advise": (
        "bridge_advise",
        "required `task`",
        "`model`, `effort`, `timeout_seconds`, `depth`, and `run_id`",
        "Never replace supplied effort",
        "launch workspace",
        "compact envelope",
        "`transcript_path`",
        "`incident`",
        "never copy transcript-only peer `details`",
        "fresh call, never session resumption",
    ),
    "implement": (
        "bridge_implement",
        "required `task`",
        "`model`, `effort`, `timeout_seconds`, `depth`, and `run_id`",
        "Never replace supplied choices",
        "launch workspace",
        "model-controlled workspace, background, and session fields",
        "`verdict`, `findings`, `files_touched`, `remaining`, and `blockers`",
        "`transcript_path`",
        "never inline `details`",
        "reread reported files and run relevant project checks",
        "trusted inherited depth one",
    ),
    "review": (
        "bridge_review",
        "required `task`",
        "`model`, `effort`, `timeout_seconds`, `depth`, and `run_id`",
        "Never replace supplied effort",
        "launch workspace",
        "compact envelope",
        "workspace-relative transcript",
        "`incident`",
        "never inline peer `details`",
    ),
    "setup": (
        "action=all target=peer scope=auto live=prompt",
        "bridge_setup.py",
        '`--approve "<approval_digest>"`',
        "action-bound, expires, and is consumed",
        "`--action authenticate`",
        "`--action verify-live`",
        "provider-owned interactive login",
        "Never accept, request, pipe, echo, inspect, or store",
        "bridge_status",
        "paid provider call",
        "To prepare both integrations",
        "Never equate static readiness",
    ),
}

CLAUDE_SKILL_CONTRACTS = {
    "advise": (
        'bridge_call.py" advise --task "<question>"',
        "`--task-file <path>`",
        "mutually exclusive",
        "120 seconds",
        "Never resume advice",
        "`transcript_path`",
        "`incident`",
        "Preserve caller-supplied level",
    ),
    "implement": (
        'bridge_call.py" implement --task "<task>"',
        "Never interpolate task shell syntax",
        "`--task-file <path>`",
        "600 seconds",
        "hard cutoff",
        "write-capable",
        "Never auto-retry after timeout",
        "`verdict`, `findings`, `files_touched`, `remaining`, and `blockers`",
        "`transcript_path`",
        "do not edit task-named paths",
        "re-read every `files_touched` path",
    ),
    "review": (
        'bridge_call.py" review --task "<instructions>"',
        "`--task-file <path>`",
        "adversarial-review prompt",
        "300 seconds",
        "Never resume review",
        "Preserve caller-supplied level",
    ),
    "cancel": (
        'bridge_call.py" cancel --job-id "<job-id>"',
        "`--workspace` only when explicitly supplied",
        "do not claim termination complete",
        "`/bridge:status` or `/bridge:result`",
    ),
    "result": (
        'bridge_call.py" result --job-id "<job-id>"',
        "`--workspace` only when explicitly supplied",
        "never inline raw transcript",
    ),
    "status": (
        'bridge_call.py" status --job-id "<job-id>"',
        "`--workspace` only when explicitly supplied",
        "Return JSON status unchanged",
    ),
    "setup": (
        "bridge_setup.py",
        "Python 3.10",
        "action=all target=peer scope=auto live=prompt",
        '`--approve "<approval_digest>"`',
        "action-bound, expires, and is consumed",
        "`--action authenticate`",
        "`--action verify-live`",
        "provider-owned interactive login",
        "Never accept, request, pipe, echo, inspect, or store",
        "paid provider call",
        "To prepare both integrations",
        "Never equate static readiness",
    ),
}


def _read_json(path: Path) -> dict[str, object]:
    """Read one JSON contract object with an exact top-level object assertion.

    Examples:
        >>> path = getfixture("tmp_path") / "contract.json"
        >>> _ = path.write_text('{"type": "object"}')
        >>> _read_json(path)["type"]
        'object'
    """
    parsed = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict), f"{path} must contain a JSON object"
    return parsed


def _assert_value_matches_contract(schema: Mapping[str, object], value: object) -> None:
    """Validate fixtures against every JSON-Schema keyword used by bridge contracts."""
    allowed_types = schema.get("type")
    if isinstance(allowed_types, str):
        allowed_types = [allowed_types]
    if allowed_types is not None:
        assert isinstance(allowed_types, list)
        type_matches = {
            "array": isinstance(value, list),
            "boolean": isinstance(value, bool),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "null": value is None,
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "object": isinstance(value, dict),
            "string": isinstance(value, str),
        }
        assert any(type_matches.get(item, False) for item in allowed_types)

    if "enum" in schema:
        assert value in schema["enum"]
    if "minLength" in schema:
        assert isinstance(value, str)
        assert len(value) >= schema["minLength"]
    if "minimum" in schema and isinstance(value, (int, float)) and not isinstance(value, bool):
        assert value >= schema["minimum"]
    if "exclusiveMinimum" in schema and isinstance(value, (int, float)) and not isinstance(value, bool):
        assert value > schema["exclusiveMinimum"]

    if isinstance(value, list):
        if "minItems" in schema:
            assert len(value) >= schema["minItems"]
        if "items" in schema:
            assert isinstance(schema["items"], Mapping)
            for item in value:
                _assert_value_matches_contract(schema["items"], item)

    if not isinstance(value, dict):
        return

    required = schema.get("required", [])
    assert isinstance(required, list)
    assert set(required).issubset(value)
    properties = schema.get("properties", {})
    assert isinstance(properties, Mapping)
    if schema.get("additionalProperties") is False:
        assert set(value).issubset(properties)
    for key, item in value.items():
        if key in properties:
            assert isinstance(properties[key], Mapping)
            _assert_value_matches_contract(properties[key], item)
        elif isinstance(schema.get("additionalProperties"), Mapping):
            _assert_value_matches_contract(schema["additionalProperties"], item)


@pytest.mark.parametrize("name", sorted(CODEX_SKILL_CONTRACTS))
def test_codex_skills_retain_the_runtime_safety_contract(name: str) -> None:
    """Prevent prompt compression from dropping Bridge's caller and safety boundaries."""
    skills_root = PLUGIN_ROOT / "codex-skills"
    skill = (skills_root / name / "SKILL.md").read_text(encoding="utf-8")
    requirements = CODEX_SKILL_CONTRACTS[name]
    assert not [requirement for requirement in requirements if requirement not in skill], name


@pytest.mark.parametrize("name", sorted(CLAUDE_SKILL_CONTRACTS))
def test_claude_skills_retain_the_runtime_safety_contract(name: str) -> None:
    """Prevent prompt compression from dropping Bridge's caller and safety boundaries."""
    skills_root = PLUGIN_ROOT / "claude-skills"
    skill = (skills_root / name / "SKILL.md").read_text(encoding="utf-8")
    requirements = CLAUDE_SKILL_CONTRACTS[name]
    assert not [requirement for requirement in requirements if requirement not in skill], name


@pytest.mark.parametrize(
    "relative_path",
    (
        "rules/escalation-policy.md",
        "rules/self-healing.md",
        "rules/envelope.md",
        "rules/recursion-guard.md",
        "rules/prompting.md",
        "schemas/envelope.schema.json",
        "schemas/harness-envelope.schema.json",
        "schemas/mcp-tools.schema.json",
        "schemas/setup-result.schema.json",
    ),
)
def test_contract_artifacts_exist(relative_path: str) -> None:
    """Prevent runtime dispatch without every declared source-of-truth contract."""
    assert (PLUGIN_ROOT / relative_path).is_file(), relative_path


def test_model_core_schema_accepts_only_model_authored_result() -> None:
    """Prevent model output from claiming harness-observed lifecycle metadata."""
    schema = _read_json(CORE_SCHEMA_PATH)

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == PEER_FIELDS
    assert set(schema["properties"]) == PEER_FIELDS
    assert schema["properties"]["status"]["enum"] == ["complete", "partial", "blocked"]
    assert PEER_FIELDS.isdisjoint(HARNESS_ONLY_FIELDS)
    assert schema["properties"]["verdict"]["maxLength"] == 500
    for field in ("findings", "files_touched", "remaining", "blockers"):
        assert schema["properties"][field]["maxItems"] == 8
        assert schema["properties"][field]["items"]["maxLength"] == 500
    assert schema["properties"]["details"]["maxItems"] == 32
    assert schema["properties"]["details"]["items"]["maxLength"] == 2000

    _assert_value_matches_contract(
        schema,
        {
            "status": "partial",
            "verdict": "The bounded result is usable.",
            "findings": ["one finding"],
            "files_touched": [],
            "remaining": ["one follow-up"],
            "blockers": [],
            "details": ["one transcript-only detail"],
        },
    )

    with pytest.raises(AssertionError):
        _assert_value_matches_contract(
            schema,
            {
                "status": "timeout",
                "verdict": "wrong layer",
                "findings": [],
                "files_touched": [],
                "remaining": [],
                "blockers": [],
                "details": [],
            },
        )
    with pytest.raises(AssertionError):
        _assert_value_matches_contract(
            schema,
            {
                "status": "complete",
                "verdict": "wrong layer",
                "findings": [],
                "files_touched": [],
                "remaining": [],
                "blockers": [],
                "details": [],
                "cost": 1.0,
            },
        )


def test_harness_schema_adds_observed_metadata_and_terminal_statuses() -> None:
    """Prevent timeout/refusal and telemetry from leaking into the model-core schema."""
    schema = _read_json(HARNESS_SCHEMA_PATH)

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == CORE_FIELDS | HARNESS_ONLY_FIELDS
    assert set(schema["properties"]) == CORE_FIELDS | HARNESS_ONLY_FIELDS
    assert schema["properties"]["status"]["enum"] == ["complete", "partial", "blocked", "timeout", "refused"]

    _assert_value_matches_contract(
        schema,
        {
            "status": "refused",
            "verdict": "Recursion was refused.",
            "findings": [],
            "files_touched": [],
            "remaining": [],
            "blockers": ["recursion-depth"],
            "model": "test-model",
            "effort": "low",
            "effort_substituted": None,
            "cost": None,
            "tokens": {"input": 0, "output": 0},
            "duration_seconds": 0.0,
            "depth": 1,
            "run_id": "run-123",
            "incident": None,
            "session_id": None,
            "transcript_path": ".temp/bridge/raw.txt",
            "verb": "advise",
            "direction": "codex_to_claude",
        },
    )

    with pytest.raises(AssertionError):
        _assert_value_matches_contract(
            schema,
            {
                "status": "complete",
                "verdict": "wrong token count",
                "findings": [],
                "files_touched": [],
                "remaining": [],
                "blockers": [],
                "model": "test-model",
                "effort": "low",
                "effort_substituted": None,
                "cost": 0.0,
                "tokens": {"input": -1},
                "duration_seconds": 0.0,
                "depth": 0,
                "run_id": "run-123",
                "incident": None,
                "session_id": None,
                "transcript_path": ".temp/bridge/raw.txt",
                "verb": "advise",
                "direction": "claude_to_codex",
            },
        )


def test_setup_result_schema_cannot_impersonate_a_model_or_provider_result() -> None:
    """Keep setup lifecycle evidence separate from inference, transcript, token, cost, and verb claims."""
    schema = _read_json(SETUP_SCHEMA_PATH)
    properties = schema["properties"]
    forbidden = {"model", "effort", "cost", "tokens", "transcript_path", "incident", "verb", "findings"}

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["additionalProperties"] is False
    assert forbidden.isdisjoint(properties)
    assert properties["status"]["enum"] == ["ready", "partial", "blocked", "manual", "unsupported", "denied", "failed"]
    assert properties["authentication"]["enum"] == [
        "not-checked",
        "auth-flow-launched",
        "host-authenticated",
        "inference-unverified",
        "live-verified",
    ]
    assert properties["provider_call"] == {"type": "boolean"}

    _assert_value_matches_contract(
        schema,
        {
            "status": "partial",
            "current_host": "codex",
            "target": "claude",
            "direction": "codex_to_claude",
            "requested": {"action": "all", "target": "peer", "scope": "auto", "live": "prompt"},
            "canonical_workspace": "/workspace",
            "workspace_fingerprint": "a" * 64,
            "resolved_scope": "user",
            "approval_digest": "approval",
            "state_fingerprint": "b" * 64,
            "operations": [],
            "classification": "static-ready",
            "authentication": "host-authenticated",
            "verification_level": "host-authenticated",
            "state_changed": False,
            "provider_call": False,
            "ready_to_use": False,
            "remaining": ["session-workspace-verification", "live-verification"],
            "manual_next_action": "Verify the loaded session and workspace.",
            "confidence": "high",
            "limits": ["No provider call made."],
        },
    )


def test_mcp_input_contract_rejects_unknown_or_incomplete_request_fields() -> None:
    """Prevent an MCP tool from receiving a request the bridge cannot safely route."""
    schema = _read_json(MCP_SCHEMA_PATH)
    definitions = schema["$defs"]
    assert set(definitions) == {"bridge_implement", "bridge_advise", "bridge_review", "bridge_status"}
    request = definitions["bridge_advise"]
    assert isinstance(request, Mapping)
    assert request["additionalProperties"] is False
    assert set(request["required"]) == {"task"}
    for name, definition in definitions.items():
        if name == "bridge_status":
            assert definition == {"additionalProperties": False, "properties": {}, "type": "object"}
            continue
        assert "workspace" not in definition["properties"]
        assert "background" not in definition["properties"]
        assert "session_id" not in definition["properties"]
        assert definition["properties"]["task"]["pattern"] == "\\S"
        assert definition["properties"]["task"]["maxLength"] == 16_384
        assert {"trivial", "none"}.issubset(definition["properties"]["effort"]["enum"])

    _assert_value_matches_contract(
        request,
        {
            "task": "Summarize the local diff.",
            "model": "test-model",
            "effort": "low",
            "depth": 0,
            "run_id": "run-123",
            "timeout_seconds": 120,
            "supported_efforts": ["low", "medium"],
        },
    )
    _assert_value_matches_contract(request, {"task": "Use bridge-owned defaults."})
    _assert_value_matches_contract(request, {"task": "Normalize a documented alias.", "effort": "none"})
    with pytest.raises(AssertionError):
        _assert_value_matches_contract(
            request,
            {
                "task": "unknown field",
                "model": "test-model",
                "effort": "low",
                "depth": 0,
                "run_id": "run-123",
                "surprise": True,
            },
        )


@pytest.mark.parametrize("name", ["bridge_implement", "bridge_advise", "bridge_review"])
def test_reverse_timeout_limit_leaves_a_response_margin_before_the_mcp_deadline(name: str) -> None:
    """Prevent the complete retry policy from outliving the MCP host that returns the envelope."""
    schema = _read_json(MCP_SCHEMA_PATH)
    config = _read_json(MCP_CONFIG_PATH)
    definitions = schema["$defs"]
    timeout_limits = {
        name: definitions[name]["properties"]["timeout_seconds"]["maximum"]
        for name in ("bridge_implement", "bridge_advise", "bridge_review")
    }
    maximum_attempts = {"bridge_implement": 1, "bridge_advise": 2, "bridge_review": 2}

    assert timeout_limits == {
        "bridge_implement": 700,
        "bridge_advise": 350,
        "bridge_review": 350,
    }
    timeout_seconds = timeout_limits[name]
    worst_case = maximum_attempts[name] * (timeout_seconds * 1.2 + 9)
    assert worst_case + 30 < config["mcpServers"]["bridge"]["tool_timeout_sec"]


@pytest.mark.parametrize("name", ["bridge_implement", "bridge_advise", "bridge_review"])
def test_mcp_python_constants_match_the_shipped_transport_config(name: str) -> None:
    """Prevent the server's deadline model from drifting away from the declared MCP config."""
    import sys

    bin_root = PLUGIN_ROOT / "bin"
    if str(bin_root) not in sys.path:
        sys.path.insert(0, str(bin_root))
    import bridge_mcp

    config = _read_json(MCP_CONFIG_PATH)
    schema = _read_json(MCP_SCHEMA_PATH)

    assert bridge_mcp.MCP_HOST_DEADLINE_SECONDS == config["mcpServers"]["bridge"]["tool_timeout_sec"]
    assert set(bridge_mcp.TOOL_NAMES) == {"bridge_implement", "bridge_advise", "bridge_review"}
    verb = bridge_mcp.TOOL_NAMES[name]
    schema_maximum = schema["$defs"][name]["properties"]["timeout_seconds"]["maximum"]
    assert bridge_mcp.MAX_MCP_TIMEOUT_SECONDS_BY_VERB[verb] == schema_maximum

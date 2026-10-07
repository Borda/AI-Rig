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
MCP_CONFIG_PATH = PLUGIN_ROOT / ".codex-mcp.json"

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
        "current `binding_id`",
        "bridge_bind_workspace",
        "project binding grants no editing approval",
        "compact envelope",
        "`transcript_path`",
        "`incident`",
        "never copy transcript-only peer `details`",
        "fresh call, never session resumption",
        "open the JSON file referenced by `incident`",
        "inspect its `fault`",
        "`output-limit`",
        "name unanswered work",
    ),
    "implement": (
        "bridge_implement",
        "required `task`",
        "`model`, `effort`, `timeout_seconds`, `depth`, and `run_id`",
        "Never replace supplied choices",
        "current `binding_id`",
        "bridge_bind_workspace",
        "project binding grants no editing approval",
        "model-controlled workspace, background, and session fields",
        "`verdict`, `findings`, `files_touched`, `remaining`, and `blockers`",
        "`transcript_path`",
        "never inline `details`",
        "reread reported files and run relevant project checks",
        "trusted inherited depth one",
        "open the JSON file referenced by `incident`",
        "inspect its `fault`",
        "`output-limit`",
        "never replay the original write-capable task",
    ),
    "review": (
        "bridge_review",
        "required `task`",
        "`model`, `effort`, `timeout_seconds`, `depth`, and `run_id`",
        "Never replace supplied effort",
        "current `binding_id`",
        "bridge_bind_workspace",
        "project binding grants no editing approval",
        "compact envelope",
        "workspace-relative transcript",
        "`incident`",
        "never inline peer `details`",
        "open the JSON file referenced by `incident`",
        "inspect its `fault`",
        "`output-limit`",
        "no review verdict",
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
        "open the JSON file referenced by `incident`",
        "inspect its `fault`",
        "`output-limit`",
        "name unanswered work",
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
        "Never wait on a detached job with a loop",
        "`run_in_background: true`",
        "re-read every `files_touched` path",
        "open the JSON file referenced by `incident`",
        "inspect its `fault`",
        "`output-limit`",
        "never replay the original write-capable task",
    ),
    "review": (
        'bridge_call.py" review --task "<instructions>"',
        "`--task-file <path>`",
        "adversarial-review prompt",
        "300 seconds",
        "Never resume review",
        "Preserve caller-supplied level",
        "open the JSON file referenced by `incident`",
        "inspect its `fault`",
        "`output-limit`",
        "no review verdict",
    ),
    "cancel": (
        'bridge_call.py" cancel --job-id "<job-id>"',
        "same `--workspace` value the originating detached call used",
        "do not claim termination complete",
        "`/bridge:status` or `/bridge:result`",
    ),
    "result": (
        'bridge_call.py" result --job-id "<job-id>"',
        "same `--workspace` value the originating detached call used",
        "never inline the bounded transcript",
        "open the JSON file referenced by `incident`",
        "inspect its `fault`",
        "`output-limit`",
    ),
    "status": (
        'bridge_call.py" status --job-id "<job-id>"',
        "same `--workspace` value the originating detached call used",
        "Return JSON status unchanged",
        "never wrapped in a `sleep`, `ScheduleWakeup`, `ListAgents`, or `Monitor` loop",
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
    [
        "rules/escalation-policy.md",
        "rules/self-healing.md",
        "rules/envelope.md",
        "rules/recursion-guard.md",
        "rules/prompting.md",
        "schemas/envelope.schema.json",
        "schemas/harness-envelope.schema.json",
        "schemas/mcp-tools.schema.json",
        "schemas/setup-result.schema.json",
    ],
)
def test_contract_artifacts_exist(relative_path: str) -> None:
    """Prevent runtime dispatch without every declared source-of-truth contract."""
    assert (PLUGIN_ROOT / relative_path).is_file(), relative_path


class TestModelCoreSchema:
    """Contract checks for the model-authored core envelope schema."""

    @pytest.fixture
    def schema(self) -> dict[str, object]:
        """Load the core envelope schema shared by every test in this class."""
        return _read_json(CORE_SCHEMA_PATH)

    def test_declares_peer_fields_and_excludes_harness_only_fields(self, schema: dict[str, object]) -> None:
        """Core schema exposes only peer fields and keeps them disjoint from harness-only ones.

        Prevents model output from claiming harness-observed lifecycle metadata (e.g. cost, tokens, transcript_path)
        that only the harness may attach after the model call returns.
        """
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

    def test_accepts_a_well_formed_peer_result(self, schema: dict[str, object]) -> None:
        """A partial-status result carrying only peer fields, including bounded details, validates.

        Confirms the schema's positive path independently of the shape rules asserted above.
        """
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

    @pytest.mark.parametrize(
        "result",
        [
            pytest.param(
                {
                    "status": "timeout",
                    "verdict": "wrong layer",
                    "findings": [],
                    "files_touched": [],
                    "remaining": [],
                    "blockers": [],
                    "details": [],
                },
                id="status-outside-model-authored-enum",
            ),
            pytest.param(
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
                id="harness-only-field-cost",
            ),
        ],
    )
    def test_rejects_a_result_claiming_harness_authority(
        self, schema: dict[str, object], result: dict[str, object]
    ) -> None:
        """A result claiming a harness-only status or field is rejected at the model layer.

        A status value only the harness may assign ("timeout") fails the enum, and a result adding a harness-only field
        ("cost") fails additionalProperties. Guards against a model claiming a harness-observed lifecycle outcome or
        impersonating harness-attached telemetry it never computed.
        """
        with pytest.raises(AssertionError):
            _assert_value_matches_contract(schema, result)


class TestHarnessSchema:
    """Contract checks for the harness-observed envelope schema."""

    @pytest.fixture
    def schema(self) -> dict[str, object]:
        """Load the harness envelope schema shared by every test in this class."""
        return _read_json(HARNESS_SCHEMA_PATH)

    def test_declares_core_and_harness_only_fields_with_terminal_statuses(self, schema: dict[str, object]) -> None:
        """Harness schema layers observed metadata and terminal statuses onto the core fields.

        Prevents timeout/refusal handling and telemetry from leaking into the model-authored core schema instead.
        """
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == CORE_FIELDS | HARNESS_ONLY_FIELDS
        assert set(schema["properties"]) == CORE_FIELDS | HARNESS_ONLY_FIELDS
        assert schema["properties"]["status"]["enum"] == ["complete", "partial", "blocked", "timeout", "refused"]

    def test_accepts_a_refused_result_with_full_harness_metadata(self, schema: dict[str, object]) -> None:
        """A refused-status result carrying every harness-only field validates end to end.

        Exercises the recursion-refusal path together with the full set of harness telemetry fields (model, effort,
        cost, tokens, duration, depth, run_id, incident, ...).
        """
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

    def test_rejects_a_negative_token_count(self, schema: dict[str, object]) -> None:
        """A negative token count is rejected even though every other field is well-formed.

        Guards the `minimum: 0` constraint on the nested `tokens` object, distinct from the enum/shape checks covered
        elsewhere.
        """
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
    assert set(definitions) == {
        "bridge_implement",
        "bridge_advise",
        "bridge_review",
        "bridge_status",
        "bridge_bind_workspace",
    }
    request = definitions["bridge_advise"]
    assert isinstance(request, Mapping)
    assert request["additionalProperties"] is False
    assert set(request["required"]) == {"task", "binding_id"}
    for name, definition in definitions.items():
        if name in {"bridge_status", "bridge_bind_workspace"}:
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
            "binding_id": "confirmed-binding",
            "model": "test-model",
            "effort": "low",
            "depth": 0,
            "run_id": "run-123",
            "timeout_seconds": 120,
            "supported_efforts": ["low", "medium"],
        },
    )
    _assert_value_matches_contract(request, {"task": "Use bridge-owned defaults.", "binding_id": "confirmed-binding"})
    _assert_value_matches_contract(
        request, {"task": "Normalize a documented alias.", "effort": "none", "binding_id": "confirmed-binding"}
    )
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


def test_codex_setup_uses_portable_helper_launcher() -> None:
    """Allow setup diagnosis without confusing its launcher with the MCP prerequisite."""
    text = (PLUGIN_ROOT / "codex-skills/setup/SKILL.md").read_text(encoding="utf-8")
    assert '"PLUGIN_ROOT/bin/python" "PLUGIN_ROOT/bin/bridge_setup.py"' in text
    assert 'python "${PLUGIN_ROOT}/bin/bridge_setup.py"' not in text
    assert "bin/python.cmd" in text
    assert "MCP startup still requires" in text

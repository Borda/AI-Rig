"""Acceptance checks for the bridge's shipped contracts: schemas, MCP config, and skill and rule wording.

Schemas are checked through a small keyword validator; skill and rule prose is checked by required phrases after
whitespace is collapsed, so reflowing a paragraph never breaks a check.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest
from _bridge_prose import read_prose

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT / "bin") not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT / "bin"))

import bridge_mcp  # noqa: E402  (loaded from the installed-plugin-equivalent bin directory)

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
        "Do not edit task-named paths",
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

PROVIDER_CONSENT_RULE_PHRASES = (
    "Before every",
    "An absent, invalid, unreadable or unsupported result stops dispatch",
    "Never run interactive `grant` or `revoke` for them",
    "session_effective=unverified",
    "never treat cached consent as current",
    "Do not ask a duplicate native question first",
)
SETUP_CONSENT_FLAG_SKILL_PHRASES = (
    "explicitly supplied in the current user invocation",
    "Reject it with `check`, `authenticate`, or `verify-live` before any setup operation",
    "do not pass it to `bridge_setup.py`",
    "grant --approve",
    "otherwise use the operator-terminal approval",
    "Follow either grant with a read-only recheck",
)
SETUP_CONSENT_FLAG_RULE_PHRASES = (
    "Never invent the flag",
    "request to implement this feature",
    "Do not use it to bypass a host denial",
    "`--approve <digest>` does not imply provider consent",
)

_SKILL_CONTRACT_CASES = [
    pytest.param(skills_dir, name, requirements, id=f"{skills_dir}-{name}")
    for skills_dir, contracts in (("codex-skills", CODEX_SKILL_CONTRACTS), ("claude-skills", CLAUDE_SKILL_CONTRACTS))
    for name, requirements in sorted(contracts.items())
]


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


def _missing_phrases(text: str, phrases: tuple[str, ...]) -> list[str]:
    """Return the required phrases absent from the text, in declaration order."""
    return [phrase for phrase in phrases if phrase not in text]


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


def _core_result(**overrides: object) -> dict[str, object]:
    """Build a minimal valid model-authored envelope, with the given fields replaced."""
    result = {
        "status": "complete",
        "verdict": "wrong layer",
        "findings": [],
        "files_touched": [],
        "remaining": [],
        "blockers": [],
        "details": [],
    }
    return {**result, **overrides}


def _harness_result(**overrides: object) -> dict[str, object]:
    """Build a minimal valid harness-observed envelope, with the given fields replaced."""
    result = {
        "status": "complete",
        "verdict": "Harness result.",
        "findings": [],
        "files_touched": [],
        "remaining": [],
        "blockers": [],
        "model": "test-model",
        "effort": "low",
        "effort_substituted": None,
        "cost": 0.0,
        "tokens": {"input": 0, "output": 0},
        "duration_seconds": 0.0,
        "depth": 0,
        "run_id": "run-123",
        "incident": None,
        "session_id": None,
        "transcript_path": ".temp/bridge/raw.txt",
        "verb": "advise",
        "direction": "claude_to_codex",
    }
    return {**result, **overrides}


@pytest.fixture
def core_schema() -> dict[str, object]:
    """Load the model-authored core envelope schema."""
    return _read_json(CORE_SCHEMA_PATH)


@pytest.fixture
def harness_schema() -> dict[str, object]:
    """Load the harness-observed envelope schema."""
    return _read_json(HARNESS_SCHEMA_PATH)


@pytest.fixture
def mcp_schema() -> dict[str, object]:
    """Load the MCP tool-input schema."""
    return _read_json(MCP_SCHEMA_PATH)


@pytest.fixture
def mcp_config() -> dict[str, object]:
    """Load the shipped Codex MCP transport config."""
    return _read_json(MCP_CONFIG_PATH)


@pytest.mark.parametrize(("skills_dir", "name", "requirements"), _SKILL_CONTRACT_CASES)
def test_skill_retains_safety_contract(skills_dir: str, name: str, requirements: tuple[str, ...]) -> None:
    """A host skill still carries every caller and safety boundary the bridge depends on.

    Each skill file is the only instruction the peer host reads before dispatching. Compressing or rewording it must not
    drop a boundary such as approval handling, incident inspection, or the no-replay rule for write-capable tasks.
    """
    missing = _missing_phrases(read_prose(PLUGIN_ROOT / skills_dir / name / "SKILL.md"), requirements)
    assert not missing, f"{skills_dir}/{name} lost: {missing}"


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
def test_contract_artifact_exists(relative_path: str) -> None:
    """Every declared source-of-truth contract file ships in the plugin.

    Runtime dispatch reads these rules and schemas by path, so a missing one would only surface when a peer call runs.
    """
    assert (PLUGIN_ROOT / relative_path).is_file(), relative_path


class TestModelCoreSchema:
    """Contract checks for the model-authored core envelope schema."""

    def test_declares_peer_fields_and_excludes_harness_only_fields(self, core_schema: dict[str, object]) -> None:
        """Core schema exposes only peer fields and keeps them disjoint from harness-only ones.

        Prevents model output from claiming harness-observed lifecycle metadata (e.g. cost, tokens, transcript_path)
        that only the harness may attach after the model call returns.
        """
        properties = core_schema["properties"]
        assert core_schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert core_schema["type"] == "object"
        assert core_schema["additionalProperties"] is False
        assert set(core_schema["required"]) == PEER_FIELDS
        assert set(properties) == PEER_FIELDS
        assert properties["status"]["enum"] == ["complete", "partial", "blocked"]
        assert PEER_FIELDS.isdisjoint(HARNESS_ONLY_FIELDS)

    def test_leaves_verdict_and_list_fields_unbounded(self, core_schema: dict[str, object]) -> None:
        """Verdict length and list sizes are not capped by the schema; list entries are plain strings.

        The harness bounds output after the call, so a schema-level cap would reject legitimate long peer answers.
        """
        properties = core_schema["properties"]
        list_fields = ("findings", "files_touched", "remaining", "blockers", "details")
        assert "maxLength" not in properties["verdict"]
        assert all("maxItems" not in properties[field] for field in list_fields)
        assert all(properties[field]["items"] == {"type": "string"} for field in list_fields)

    def test_accepts_a_well_formed_peer_result(self, core_schema: dict[str, object]) -> None:
        """A partial-status result carrying only peer fields, including bounded details, validates.

        Confirms the schema's positive path independently of the shape rules asserted above.
        """
        result = _core_result(
            status="partial",
            verdict="The bounded result is usable.",
            findings=["one finding"],
            remaining=["one follow-up"],
            details=["one transcript-only detail"],
        )
        _assert_value_matches_contract(core_schema, result)

    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param({"status": "timeout"}, id="status-outside-model-authored-enum"),
            pytest.param({"cost": 1.0}, id="harness-only-field-cost"),
        ],
    )
    def test_rejects_a_result_claiming_harness_authority(
        self, core_schema: dict[str, object], overrides: dict[str, object]
    ) -> None:
        """A result claiming a harness-only status or field is rejected at the model layer.

        A status value only the harness may assign ("timeout") fails the enum, and a result adding a harness-only field
        ("cost") fails additionalProperties. Guards against a model claiming a harness-observed lifecycle outcome or
        impersonating harness-attached telemetry it never computed.
        """
        with pytest.raises(AssertionError):
            _assert_value_matches_contract(core_schema, _core_result(**overrides))


class TestHarnessSchema:
    """Contract checks for the harness-observed envelope schema."""

    def test_declares_core_and_harness_only_fields_with_terminal_statuses(
        self, harness_schema: dict[str, object]
    ) -> None:
        """Harness schema layers observed metadata and terminal statuses onto the core fields.

        Prevents timeout/refusal handling and telemetry from leaking into the model-authored core schema instead.
        """
        properties = harness_schema["properties"]
        assert harness_schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert harness_schema["type"] == "object"
        assert harness_schema["additionalProperties"] is False
        assert set(harness_schema["required"]) == CORE_FIELDS | HARNESS_ONLY_FIELDS
        assert set(properties) == CORE_FIELDS | HARNESS_ONLY_FIELDS | {"partial_output"}
        assert "partial_output" not in harness_schema["required"]
        assert properties["partial_output"]["type"] == "string"
        assert properties["partial_output"]["maxLength"] == 4096
        assert properties["status"]["enum"] == ["complete", "partial", "blocked", "timeout", "refused"]

    def test_partial_output_requires_a_blocked_codex_child(self, harness_schema: dict[str, object]) -> None:
        """Require schema routing guards and the consumer-enforced UTF-8 byte bound.

        The local fixture validator does not implement conditionals, so inspect their exact schema contract.
        """
        description = harness_schema["properties"]["partial_output"]["description"]
        assert harness_schema["if"] == {"required": ["partial_output"]}
        assert harness_schema["then"] == {
            "properties": {"status": {"const": "blocked"}, "direction": {"const": "claude_to_codex"}}
        }
        assert "consumers must also enforce the 4096-byte UTF-8 bound" in description

    def test_accepts_a_refused_result_with_full_harness_metadata(self, harness_schema: dict[str, object]) -> None:
        """A refused-status result carrying every harness-only field validates end to end.

        Exercises the recursion-refusal path together with the full set of harness telemetry fields (model, effort,
        cost, tokens, duration, depth, run_id, incident, ...).
        """
        result = _harness_result(
            status="refused",
            verdict="Recursion was refused.",
            blockers=["recursion-depth"],
            cost=None,
            depth=1,
            direction="codex_to_claude",
        )
        _assert_value_matches_contract(harness_schema, result)

    def test_rejects_a_negative_token_count(self, harness_schema: dict[str, object]) -> None:
        """A negative token count is rejected even though every other field is well-formed.

        Guards the `minimum: 0` constraint on the nested `tokens` object, distinct from the enum/shape checks covered
        elsewhere.
        """
        with pytest.raises(AssertionError):
            _assert_value_matches_contract(harness_schema, _harness_result(tokens={"input": -1}))


class TestSetupResultSchema:
    """Contract checks for the setup lifecycle result schema."""

    @pytest.fixture
    def schema(self) -> dict[str, object]:
        """Load the setup result schema."""
        return _read_json(SETUP_SCHEMA_PATH)

    def test_cannot_impersonate_a_model_or_provider_result(self, schema: dict[str, object]) -> None:
        """Setup lifecycle evidence stays separate from inference, transcript, token, cost, and verb claims.

        A setup result that could carry model or provider fields would let static readiness be mistaken for a live
        provider call.
        """
        properties = schema["properties"]
        forbidden = {"model", "effort", "cost", "tokens", "transcript_path", "incident", "verb", "findings"}
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["additionalProperties"] is False
        assert forbidden.isdisjoint(properties)
        assert properties["provider_call"] == {"type": "boolean"}

    def test_pins_status_and_authentication_vocabulary(self, schema: dict[str, object]) -> None:
        """Setup status and authentication levels are closed sets in a fixed order.

        Skills branch on these exact words, so an added or renamed level must be a deliberate contract change.
        """
        properties = schema["properties"]
        assert properties["status"]["enum"] == [
            "ready",
            "partial",
            "blocked",
            "manual",
            "unsupported",
            "denied",
            "failed",
        ]
        assert properties["authentication"]["enum"] == [
            "not-checked",
            "auth-flow-launched",
            "host-authenticated",
            "inference-unverified",
            "live-verified",
        ]

    def test_accepts_a_static_ready_partial_result(self, schema: dict[str, object]) -> None:
        """A partial result that made no provider call and still lists remaining verification validates."""
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


class TestMcpToolSchema:
    """Contract checks for the MCP tool-input schema and its transport deadlines."""

    @pytest.fixture
    def definitions(self, mcp_schema: dict[str, object]) -> dict[str, dict[str, object]]:
        """Return the per-tool request definitions."""
        return mcp_schema["$defs"]

    def test_declares_exactly_the_bridge_tools(self, definitions: dict[str, dict[str, object]]) -> None:
        """The schema defines the five bridge tools and no others.

        An extra or missing tool definition means the server would route a request the contract never reviewed.
        """
        assert set(definitions) == {
            "bridge_implement",
            "bridge_advise",
            "bridge_review",
            "bridge_status",
            "bridge_bind_workspace",
        }

    def test_status_takes_no_arguments(self, definitions: dict[str, dict[str, object]]) -> None:
        """The status tool accepts an empty object only."""
        assert definitions["bridge_status"] == {"additionalProperties": False, "properties": {}, "type": "object"}

    def test_bind_workspace_accepts_only_a_proposed_workspace_string(
        self, definitions: dict[str, dict[str, object]]
    ) -> None:
        """Binding takes one proposed workspace path and nothing else, so the model cannot widen the binding."""
        definition = definitions["bridge_bind_workspace"]
        assert definition["additionalProperties"] is False
        assert set(definition["properties"]) == {"proposed_workspace"}
        assert definition["properties"]["proposed_workspace"]["type"] == "string"

    @pytest.mark.parametrize("name", ["bridge_implement", "bridge_advise", "bridge_review"])
    def test_verb_tool_hides_host_controlled_fields_and_bounds_the_task(
        self, definitions: dict[str, dict[str, object]], name: str
    ) -> None:
        """Verb tools expose no workspace, background or session field, and bound a non-blank task.

        Those fields are chosen by the host binding, never by model-controlled input; the task pattern rejects blank
        text and the length bound keeps one request inside the MCP payload budget.
        """
        properties = definitions[name]["properties"]
        assert {"workspace", "background", "session_id"}.isdisjoint(properties)
        assert properties["task"]["pattern"] == "\\S"
        assert properties["task"]["maxLength"] == 16_384
        assert {"trivial", "none"}.issubset(properties["effort"]["enum"])

    def test_advise_request_requires_task_and_binding_and_forbids_extras(
        self, definitions: dict[str, dict[str, object]]
    ) -> None:
        """An advise request must name a task and a confirmed binding, and may carry no other field."""
        request = definitions["bridge_advise"]
        assert request["additionalProperties"] is False
        assert set(request["required"]) == {"task", "binding_id"}

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param(
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
                id="every-optional-field",
            ),
            pytest.param({"task": "Use bridge-owned defaults.", "binding_id": "confirmed-binding"}, id="defaults-only"),
            pytest.param(
                {"task": "Normalize a documented alias.", "effort": "none", "binding_id": "confirmed-binding"},
                id="documented-effort-alias",
            ),
        ],
    )
    def test_advise_request_accepts_valid_payloads(
        self, definitions: dict[str, dict[str, object]], payload: dict[str, object]
    ) -> None:
        """Documented advise payloads, from bare defaults to every optional field, validate."""
        _assert_value_matches_contract(definitions["bridge_advise"], payload)

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param({"task": "unknown field", "binding_id": "confirmed-binding", "surprise": True}, id="unknown"),
            pytest.param({"task": "no binding", "model": "test-model", "depth": 0}, id="missing-binding-id"),
        ],
    )
    def test_advise_request_rejects_unknown_or_incomplete_fields(
        self, definitions: dict[str, dict[str, object]], payload: dict[str, object]
    ) -> None:
        """An unknown field, or a request without a confirmed binding, is rejected.

        Each case isolates one cause so a regression in `additionalProperties` and one in `required` cannot mask each
        other.
        """
        with pytest.raises(AssertionError):
            _assert_value_matches_contract(definitions["bridge_advise"], payload)

    @pytest.mark.parametrize(
        ("name", "maximum", "attempts"),
        [
            pytest.param("bridge_implement", 700, 1, id="implement"),
            pytest.param("bridge_advise", 350, 2, id="advise"),
            pytest.param("bridge_review", 350, 2, id="review"),
        ],
    )
    def test_reverse_timeout_limit_leaves_response_margin_before_mcp_deadline(
        self, mcp_schema: dict[str, object], mcp_config: dict[str, object], name: str, maximum: int, attempts: int
    ) -> None:
        """The longest retry policy still finishes, plus a 30 s margin, before the MCP host gives up.

        Each attempt may run 1.2x its timeout plus 9 s of overhead; if the host deadline were shorter, the host would
        discard an envelope the bridge was still about to return.
        """
        schema_maximum = mcp_schema["$defs"][name]["properties"]["timeout_seconds"]["maximum"]
        worst_case = attempts * (maximum * 1.2 + 9)
        assert schema_maximum == maximum
        assert worst_case + 30 < mcp_config["mcpServers"]["bridge"]["tool_timeout_sec"]


class TestBridgeMcpConstants:
    """The server module's deadline model matches the shipped transport config and schema."""

    def test_host_deadline_matches_transport_config(self, mcp_config: dict[str, object]) -> None:
        """The module's host deadline equals the `tool_timeout_sec` shipped in the MCP config."""
        assert bridge_mcp.MCP_HOST_DEADLINE_SECONDS == mcp_config["mcpServers"]["bridge"]["tool_timeout_sec"]

    def test_tool_names_are_the_three_verbs(self) -> None:
        """The server routes exactly the implement, advise and review tools."""
        assert set(bridge_mcp.TOOL_NAMES) == {"bridge_implement", "bridge_advise", "bridge_review"}

    @pytest.mark.parametrize("name", ["bridge_implement", "bridge_advise", "bridge_review"])
    def test_timeout_maximum_matches_schema(self, mcp_schema: dict[str, object], name: str) -> None:
        """The server's per-verb timeout cap equals the schema's `timeout_seconds` maximum for that tool."""
        schema_maximum = mcp_schema["$defs"][name]["properties"]["timeout_seconds"]["maximum"]
        assert bridge_mcp.MAX_MCP_TIMEOUT_SECONDS_BY_VERB[bridge_mcp.TOOL_NAMES[name]] == schema_maximum


class TestCodexSkillProse:
    """Wording contracts of the Codex setup skill and the provider-consent rule it shares."""

    def test_setup_uses_portable_helper_launcher(self) -> None:
        """Setup diagnosis launches its helper through the portable launcher, not a bare `python` call.

        The launcher is distinct from the MCP server's own interpreter prerequisite, which the skill must still state.
        """
        text = read_prose(PLUGIN_ROOT / "codex-skills/setup/SKILL.md")
        assert '"PLUGIN_ROOT/bin/python" "PLUGIN_ROOT/bin/bridge_setup.py"' in text
        assert 'python "${PLUGIN_ROOT}/bin/bridge_setup.py"' not in text
        assert "bin/python.cmd" in text
        assert "MCP startup still requires" in text

    @pytest.mark.parametrize("skill", ["setup", "advise", "review", "implement"])
    def test_skill_links_the_setup_owned_consent_rule(self, skill: str) -> None:
        """Setup and every dispatching skill point at the one installed provider-consent rule."""
        assert "../../rules/provider-consent.md" in read_prose(PLUGIN_ROOT / "codex-skills" / skill / "SKILL.md")

    @pytest.mark.parametrize(
        ("relative_path", "phrases"),
        [
            pytest.param("rules/provider-consent.md", PROVIDER_CONSENT_RULE_PHRASES, id="rule-gates-dispatch"),
            pytest.param(
                "codex-skills/setup/SKILL.md", SETUP_CONSENT_FLAG_SKILL_PHRASES, id="setup-skill-consent-flag"
            ),
            pytest.param("rules/provider-consent.md", SETUP_CONSENT_FLAG_RULE_PHRASES, id="rule-consent-flag"),
        ],
    )
    def test_retains_consent_boundaries(self, relative_path: str, phrases: tuple[str, ...]) -> None:
        """Consent stays opt-in, current-invocation only, and separate from the action-bound setup approvals.

        Dropping one of these sentences would let cached or inferred consent authorize a paid provider call.
        """
        missing = _missing_phrases(read_prose(PLUGIN_ROOT / relative_path), phrases)
        assert not missing, f"{relative_path} lost: {missing}"

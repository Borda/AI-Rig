"""Regression checks for deterministic code-review routing artifacts."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CODE_REVIEW_SKILL = PLUGIN_ROOT / "skills" / "code-review" / "SKILL.md"
CODE_REVIEW_RESULT_TEMPLATE = PLUGIN_ROOT / "skills" / "code-review" / "result-template.json"
HELPER_CLI_CONTRACT = PLUGIN_ROOT / "shared" / "helper-cli-contract.md"
REVIEW_VALIDATOR = PLUGIN_ROOT / "skills" / "code-review" / "validate_artifacts.py"
ROUTING_HELPER = PLUGIN_ROOT / "skills" / "code-review" / "review_routing.py"
PARALLEL_EXECUTION_TESTS = Path(__file__).with_name("test_parallel_execution.py")


class TestPrArtifactPathContract:
    """Keep PR identity promotion ordered, safe, and topology-neutral downstream."""

    def test_promotes_after_authoritative_collection(self) -> None:
        """Prevent path promotion before current-branch PR identity is known."""
        skill = CODE_REVIEW_SKILL.read_text(encoding="utf-8")
        collection = "For PR scope, inspect `python PLUGIN_ROOT/shared/collect_pr.py --help`"
        promotion = "create_run.py --skill code-review --promote-pr-run <run-directory>"

        assert skill.index(collection) < skill.index(promotion)
        assert ".reports/codex/code-review/pr-<number>/run-<NNN>/" in skill
        assert "Use the printed promoted path literally for every later helper" in skill
        assert "It is not an assessed PR review and must not be promoted." in skill

    def test_rejects_raw_argument_directory_names(self) -> None:
        """Prevent prompts, paths, URLs, or credentials from leaking into artifact paths."""
        contract = HELPER_CLI_CONTRACT.read_text(encoding="utf-8")

        assert ".reports/codex/<skill>/<canonical-safe-identity>/run-<NNN>/" in contract
        assert "Never serialize raw prompts, paths, URLs, refs, credentials, or arbitrary arguments" in contract
        assert "otherwise they use the generated timestamp" in contract

    def test_result_template_uses_selected_run_directory(self) -> None:
        """Keep local and promoted PR paths compatible with one result template."""
        template = json.loads(CODE_REVIEW_RESULT_TEMPLATE.read_text(encoding="utf-8"))

        assert template["artifact_path"] == "<run-directory>/result.json"
        assert template["metadata"]["final_handoff"]["handoff_path"] == "<run-directory>/final-handoff.json"
        assert template["metadata"]["specialist_manifest"] == "<run-directory>/specialist-manifest.json"


def _load_validator() -> ModuleType:
    """Load the shipped standalone validator from its installed-package path."""
    specification = importlib.util.spec_from_file_location("code_review_routing_validator", REVIEW_VALIDATOR)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _load_parallel_execution_tests() -> ModuleType:
    """Load the shared rollout fixture without making production code depend on tests."""
    specification = importlib.util.spec_from_file_location(
        "codex_rig_parallel_execution_tests", PARALLEL_EXECUTION_TESTS
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_action_table_parser_accepts_the_canonical_section_and_stops_at_the_next_heading() -> None:
    """Prevent regex overescaping from rejecting a valid remediation table."""
    notes = """# Review

## Review Findings and Merge Blocks

| Finding / area | Required change | Evidence | Status |
| --- | --- | --- | --- |
| Parser | Accept the canonical table. | `review-notes.md` | Required |

## Confidence Calibration

| This | is | not | a finding |
"""

    rows = _load_validator()._action_table_rows(notes)

    assert rows == [
        ["Finding / area", "Required change", "Evidence", "Status"],
        ["---", "---", "---", "---"],
        ["Parser", "Accept the canonical table.", "`review-notes.md`", "Required"],
    ]


def test_parent_activity_reader_normalizes_the_current_item_completed_shape() -> None:
    """Keep review provenance compatible with the current parent rollout activity record."""
    validator = _load_validator()
    rows = [
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "started_at_ms": 1234,
                "completed_at_ms": 1250,
                "item": {
                    "type": "SubAgentActivity",
                    "id": "activity-1",
                    "kind": "started",
                    "agent_path": "/root/review_qa",
                    "agent_thread_id": "child-1",
                },
            },
        }
    ]

    assert validator._event_payloads(rows, "sub_agent_activity") == [
        {
            "event_id": "activity-1",
            "kind": "started",
            "agent_path": "/root/review_qa",
            "agent_thread_id": "child-1",
            "started_at_ms": 1234,
            "completed_at_ms": 1250,
        }
    ]


def test_routing_helper_replaces_manual_mechanical_evidence_idempotently(tmp_path: Path) -> None:
    """Keep file and line arithmetic derived from collected evidence instead of model-authored JSON."""
    validator = _load_validator()
    signals = {name: False for name in validator.ROUTING_SIGNALS}
    signals.update({"bug_fix": True, "test_or_error_path": True})
    (tmp_path / "files.txt").write_text("src/widget.py\ntests/test_widget.py\n", encoding="utf-8")
    (tmp_path / "untracked.txt").write_text("", encoding="utf-8")
    (tmp_path / "numstat.txt").write_text(
        "51\t14\tsrc/widget.py\n278\t0\ttests/test_widget.py\n",
        encoding="utf-8",
    )
    routing_path = tmp_path / "review-routing.json"
    routing_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "risk_tier": "LOCAL",
                "mechanical_risk_tier": "TRIVIAL",
                "mechanical_risk_evidence": ["files=2", "changed_lines=329", "unknown_size_rows=0"],
                "signals": signals,
                "signal_evidence": {
                    name: ["Fixture requires this signal."] if value else ["Fixture does not require this signal."]
                    for name, value in signals.items()
                },
                "triggered_roles": ["qa-specialist"],
                "trigger_reasons": {"qa-specialist": ["Bug-fix and test-path evidence require QA."]},
            }
        ),
        encoding="utf-8",
    )

    first = subprocess.run(
        [sys.executable, str(ROUTING_HELPER), "--out", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert first.returncode == 0, first.stderr
    first_bytes = routing_path.read_bytes()
    second = subprocess.run(
        [sys.executable, str(ROUTING_HELPER), "--out", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert second.returncode == 0, second.stderr
    assert routing_path.read_bytes() == first_bytes

    routing = json.loads(first_bytes)
    assert routing["mechanical_risk_tier"] == "LOCAL"
    assert routing["mechanical_risk_evidence"] == ["files=2", "changed_lines=343", "unknown_size_rows=0"]
    assert routing["signals"] == signals
    assert validator._validate_routing(tmp_path, "LOCAL") == {"qa-specialist"}


@pytest.mark.parametrize("reasons", ["one bare reason", pytest.param(["valid reason", 3], id="non-string-member")])
def test_routing_rejects_trigger_reasons_that_are_not_nonempty_string_lists(tmp_path: Path, reasons: object) -> None:
    """Prevent malformed reason collections from passing the routing preflight."""
    validator = _load_validator()
    signals = {name: name == "behavior_change" for name in validator.ROUTING_SIGNALS}
    (tmp_path / "files.txt").write_text("src/widget.py\n", encoding="utf-8")
    (tmp_path / "untracked.txt").write_text("", encoding="utf-8")
    (tmp_path / "numstat.txt").write_text("1\t1\tsrc/widget.py\n", encoding="utf-8")
    (tmp_path / "review-routing.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "risk_tier": "LOCAL",
                "mechanical_risk_tier": "TRIVIAL",
                "mechanical_risk_evidence": ["files=1", "changed_lines=2", "unknown_size_rows=0"],
                "signals": signals,
                "signal_evidence": {name: ["fixture evidence"] for name in signals},
                "triggered_roles": ["qa-specialist"],
                "trigger_reasons": {"qa-specialist": reasons},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit, match="review-routing-trigger-reason-values-invalid"):
        validator._validate_routing(tmp_path, "LOCAL")


def test_packaged_role_card_supplies_runtime_contract_without_source_agent_config() -> None:
    """Keep plugin-only provenance grounded in the shipped role card."""
    validator = _load_validator()
    role_card = PLUGIN_ROOT / "roles" / "qa-specialist" / "ROLE.md"

    contract = validator._load_role_card(PLUGIN_ROOT / "roles", "qa-specialist")

    assert contract == {
        "approval_policy": "on-request",
        "model": "gpt-5.6-terra",
        "model_reasoning_effort": "high",
        "role_card_sha256": hashlib.sha256(role_card.read_bytes()).hexdigest(),
        "role_id": "qa-specialist",
        "sandbox_mode": "workspace-write",
    }
    assert 'project_root / ".codex" / "agents"' not in REVIEW_VALIDATOR.read_text(encoding="utf-8")


def test_sol_axis_cannot_route_without_explicit_selection_evidence(tmp_path: Path) -> None:
    """Prevent an architecture/security label from automatically selecting a Sol role."""
    validator = _load_validator()
    signals = {name: name == "axis_solution_architect" for name in validator.ROUTING_SIGNALS}
    (tmp_path / "files.txt").write_text("src/widget.py\n", encoding="utf-8")
    (tmp_path / "untracked.txt").write_text("", encoding="utf-8")
    (tmp_path / "numstat.txt").write_text("1\t1\tsrc/widget.py\n", encoding="utf-8")
    (tmp_path / "review-routing.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "risk_tier": "LOCAL",
                "mechanical_risk_tier": "TRIVIAL",
                "mechanical_risk_evidence": ["files=1", "changed_lines=2", "unknown_size_rows=0"],
                "signals": signals,
                "signal_evidence": {name: ["fixture evidence"] for name in signals},
                "triggered_roles": ["solution-architect"],
                "trigger_reasons": {"solution-architect": ["architecture axis"]},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit, match="review-routing-sol-selection-missing:solution-architect"):
        validator._validate_routing(tmp_path, "LOCAL")


def _substituted_manifest(tmp_path: Path, *roles: str) -> tuple[dict[str, object], list[dict[str, object]]]:
    """Create a minimal historical manifest for provenance-hardening regressions."""
    review_input = b"diff --git a/widget.py b/widget.py\n"
    (tmp_path / "diff.patch").write_bytes(review_input)
    passes: list[dict[str, object]] = []
    for role in roles:
        output = tmp_path / "specialists" / f"{role}.md"
        output.parent.mkdir(exist_ok=True)
        output.write_text(f"# {role}\n\nBounded {role} evidence.\n", encoding="utf-8")
        passes.append(
            {
                "role": role,
                "axis": "tests",
                "mode": "substituted",
                "trigger": "fixture trigger",
                "confidence": 0.9,
                "blocking_findings": 0,
                "output_path": str(output),
            }
        )
    manifest: dict[str, object] = {
        "schema_version": 2,
        "review_run_id": "review-run",
        "parent_thread_id": "parent-thread",
        "review_input_sha256": hashlib.sha256(review_input).hexdigest(),
        "passes": passes,
    }
    return manifest, passes


def test_manifest_rejects_reused_specialist_output_paths(tmp_path: Path) -> None:
    """Prevent two roles from claiming the same evidence file."""
    validator = _load_validator()
    manifest, passes = _substituted_manifest(tmp_path, "qa-specialist", "challenger")
    passes[1]["output_path"] = passes[0]["output_path"]

    with pytest.raises(SystemExit, match="manifest-reused-output-path"):
        validator._validate_manifest_entries(
            tmp_path,
            manifest,
            passes,
            {"qa-specialist", "challenger"},
            tmp_path,
            "parent-thread",
            tmp_path,
        )


def test_manifest_rejects_weak_substitute_output(tmp_path: Path) -> None:
    """Require a substitute to identify its role and contain substantive evidence."""
    validator = _load_validator()
    manifest, passes = _substituted_manifest(tmp_path, "qa-specialist")
    Path(str(passes[0]["output_path"])).write_text("generic note\n", encoding="utf-8")

    with pytest.raises(SystemExit, match="manifest-substitute-output-not-role-bound:qa-specialist"):
        validator._validate_manifest_entries(
            tmp_path,
            manifest,
            passes,
            {"qa-specialist"},
            tmp_path,
            "parent-thread",
            tmp_path,
        )


@pytest.mark.parametrize("role", ["solution-architect", "security-auditor"])
def test_manifest_requires_explicit_selection_evidence_for_sol_roles(tmp_path: Path, role: str) -> None:
    """Block a Sol-pinned pass that lacks immutable explicit-selection evidence."""
    validator = _load_validator()
    manifest, passes = _substituted_manifest(tmp_path, role)

    with pytest.raises(SystemExit, match=f"manifest-sol-selection-missing:{role}"):
        validator._validate_manifest_entries(
            tmp_path,
            manifest,
            passes,
            {role},
            tmp_path,
            "parent-thread",
            tmp_path,
        )


def test_manifest_rejects_unstructured_sol_selection_evidence(tmp_path: Path) -> None:
    """Require a source, event identity, and digest instead of a self-authored label."""
    validator = _load_validator()
    manifest, passes = _substituted_manifest(tmp_path, "solution-architect")
    manifest["sol_selection"] = {"solution-architect": "yes"}

    with pytest.raises(SystemExit, match="manifest-sol-selection-invalid:solution-architect"):
        validator._validate_manifest_entries(
            tmp_path,
            manifest,
            passes,
            {"solution-architect"},
            tmp_path,
            "parent-thread",
            tmp_path,
        )


def test_schema_three_binds_pass_to_the_exact_packaged_role_card(tmp_path: Path) -> None:
    """Make the new manifest schema reject a substituted role card."""
    validator = _load_validator()
    manifest, passes = _substituted_manifest(tmp_path, "qa-specialist")
    manifest["schema_version"] = 3
    passes[0]["role_card_sha256"] = "0" * 64

    with pytest.raises(SystemExit, match="manifest-role-card-hash-mismatch:qa-specialist"):
        validator._validate_manifest_entries(
            tmp_path,
            manifest,
            passes,
            {"qa-specialist"},
            tmp_path,
            "parent-thread",
            tmp_path,
        )

    passes[0]["role_card_sha256"] = hashlib.sha256(
        (PLUGIN_ROOT / "roles" / "qa-specialist" / "ROLE.md").read_bytes()
    ).hexdigest()
    assert (
        validator._validate_manifest_entries(
            tmp_path,
            manifest,
            passes,
            {"qa-specialist"},
            tmp_path,
            "parent-thread",
            tmp_path,
        )["qa-specialist"]
        is passes[0]
    )


def test_skill_requires_deterministic_routing_synchronization_before_specialists() -> None:
    """Keep the producer workflow bound to the same mechanical evidence used by validation."""
    skill = CODE_REVIEW_SKILL.read_text(encoding="utf-8")
    invocation = "python PLUGIN_ROOT/skills/code-review/review_routing.py --out <run-directory>"

    assert invocation in skill
    manifest_path = "`<run-directory>/specialist-manifest.json`"
    assert skill.index(invocation) < skill.index(manifest_path)


def test_skill_requires_list_valued_routing_evidence_and_reasons() -> None:
    """Prevent a valid-looking string value from stranding a review as an unpromoted candidate."""
    skill = CODE_REVIEW_SKILL.read_text(encoding="utf-8")

    assert "non-empty JSON `list[str]` value for each true/false decision" in skill
    assert "non-empty JSON `list[str]` value" in skill
    assert "Bare strings are invalid." in skill


def test_skill_and_result_template_require_schema_three_role_and_sol_provenance() -> None:
    """Keep the producer contract aligned with installed-card and explicit-selection validation."""
    skill = CODE_REVIEW_SKILL.read_text(encoding="utf-8")
    template = json.loads((PLUGIN_ROOT / "skills" / "code-review" / "result-template.json").read_text())

    assert "`specialist-manifest.json` uses schema version 3" in skill
    assert "`role_card_sha256`" in skill
    assert "`sol_selection`" in skill
    assert "`source=explicit-user-selection`" in skill
    assert "`parent_event_id`" in skill
    assert "`selection_sha256`" in skill
    assert template["metadata"]["specialist_passes"][0]["role_card_sha256"] == "sha256 of installed ROLE.md"


def test_manifest_preflight_rejects_spawned_pass_without_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catch malformed specialist cardinality before a result candidate exists."""
    validator = _load_validator()
    review_input = b"diff --git a/widget.py b/widget.py\n"
    (tmp_path / "diff.patch").write_bytes(review_input)
    specialists = tmp_path / "specialists"
    specialists.mkdir()
    (specialists / "qa-specialist.md").write_text("QA evidence.\n", encoding="utf-8")
    (tmp_path / "review-routing.json").write_text(json.dumps({"risk_tier": "LOCAL"}), encoding="utf-8")
    (tmp_path / "specialist-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "review_run_id": "review-run",
                "parent_thread_id": "parent-thread",
                "review_input_sha256": hashlib.sha256(review_input).hexdigest(),
                "passes": [
                    {
                        "role": "qa-specialist",
                        "axis": "tests",
                        "mode": "spawned",
                        "trigger": "bug fix",
                        "confidence": 0.9,
                        "blocking_findings": 1,
                        "output_path": "specialists/qa-specialist.md",
                        "attempts": [],
                        "selected_attempt": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(validator, "_validate_routing", lambda _out, _tier: {"qa-specialist"})
    monkeypatch.setattr(validator, "_find_rollout", lambda _home, _thread: tmp_path / "rollout.jsonl")
    monkeypatch.setattr(validator, "_read_jsonl", lambda _path: [])

    with pytest.raises(SystemExit, match="manifest-invalid-attempt-count:qa-specialist"):
        validator._validate_manifest_preflight(tmp_path, tmp_path, "parent-thread", tmp_path)


def test_review_validator_exposes_manifest_only_preflight() -> None:
    """Keep the executable preflight available to the code-review workflow."""
    completed = subprocess.run(
        [sys.executable, str(REVIEW_VALIDATOR), "--help"], capture_output=True, text=True, check=False
    )

    assert completed.returncode == 0
    assert "--manifest-only" in completed.stdout


def test_review_runtime_consumer_binds_spawned_roles_to_the_shared_validator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prevent code review from claiming a spawned mode without the shared runtime gate."""
    validator = _load_validator()
    plan_path = tmp_path / "execution-plan.json"
    plan_path.write_text('{"run_id":"review-run"}\n', encoding="utf-8")
    execution_path = tmp_path / "execution-manifest.json"
    execution = {
        "schema_version": 1,
        "stages": [
            {
                "stage_id": "review",
                "nodes": [
                    {
                        "node_id": "qa",
                        "role_id": "qa-specialist",
                        "context_path": "specialists/qa-context.md",
                        "attempts": [
                            {
                                "output_path": "specialists/qa.md",
                            }
                        ],
                        "selected_attempt": 1,
                    }
                ],
            }
        ],
    }
    execution_path.write_text(json.dumps(execution), encoding="utf-8")
    parent_rollout = tmp_path / "rollout-parent.jsonl"
    parent_rollout.write_text("{}\n", encoding="utf-8")
    pass_record = {
        "role": "qa-specialist",
        "mode": "spawned",
        "output_path": "specialists/qa.md",
        "attempts": [
            {
                "context_path": "specialists/qa-context.md",
                "output_path": "specialists/qa.md",
            }
        ],
        "selected_attempt": 1,
    }
    manifest = {
        "runtime_execution": {
            "manifest_path": execution_path.name,
            "manifest_sha256": hashlib.sha256(execution_path.read_bytes()).hexdigest(),
            "plan_path": plan_path.name,
        }
    }
    captured: dict[str, object] = {}

    def _validate_runtime(payload: dict[str, object], **kwargs: object) -> dict[str, object]:
        """Capture runtime-validator inputs and return the accepted portable summary."""
        captured.update({"payload": payload, **kwargs})
        return {
            "actual_mode": "serial",
            "evidence_level": "portable-read-restricted",
            "network_mode": "restricted",
            "approval_policy": "never",
            "filesystem_credential_isolation": "unverified",
            "runtime_promotion_eligible": True,
            "write_parallel_eligible": False,
            "consumer_id": "code-review",
        }

    monkeypatch.setattr(validator, "validate_read_only_runtime", _validate_runtime)
    monkeypatch.setattr(validator, "_find_rollout", lambda _home, _thread: parent_rollout)

    summary = validator._validate_review_runtime(
        tmp_path,
        manifest,
        [pass_record],
        tmp_path / "codex-home",
        "parent-thread",
    )

    assert summary["actual_mode"] == "serial"
    assert summary["evidence_level"] == "portable-read-restricted"
    assert summary["network_mode"] == "restricted"
    assert summary["approval_policy"] == "never"
    assert "network_guarantee" not in summary
    assert captured["payload"] == execution
    assert captured["manifest_path"] == execution_path
    assert captured["plan_path"] == plan_path
    assert captured["parent_rollout"] == parent_rollout
    assert captured["sessions_dir"] == tmp_path / "codex-home" / "sessions"
    assert captured["roles_dir"] == PLUGIN_ROOT / "roles"
    assert captured["expected_consumer_id"] == "code-review"


@pytest.fixture(name="real_schema_v2_review_runtime")
def _real_schema_v2_review_runtime(tmp_path: Path) -> dict[str, Any]:
    """Build consumer-bound rollout evidence for review runtime checks."""
    validator = _load_validator()
    fixture_module = _load_parallel_execution_tests()
    manifest, execution_path, plan_path, parent_rollout, sessions_dir, _roles_dir = (
        fixture_module._schema_v2_runtime_fixture(tmp_path)
    )
    fixture_module._bind_portable_read_consumer_policy(
        manifest,
        execution_path,
        plan_path,
        consumer_id="code-review",
    )
    out_dir = execution_path.parent
    codex_home = tmp_path / "codex-home"
    copied_sessions = codex_home / "sessions"
    copied_sessions.mkdir(parents=True)
    for rollout in sessions_dir.glob("*.jsonl"):
        (copied_sessions / rollout.name).write_bytes(rollout.read_bytes())
    (copied_sessions / parent_rollout.name).write_bytes(parent_rollout.read_bytes())

    nodes = manifest["stages"][0]["nodes"]
    for node in nodes:
        role_id = str(node["role_id"])
        role_card = PLUGIN_ROOT / "roles" / role_id / "ROLE.md"
        node["role_card_sha256"] = hashlib.sha256(role_card.read_bytes()).hexdigest()
    execution_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")

    selected_passes = [
        {
            "role": node["role_id"],
            "mode": "spawned",
            "output_path": node["attempts"][0]["output_path"],
            "attempts": [
                {
                    "context_path": node["context_path"],
                    "output_path": node["attempts"][0]["output_path"],
                }
            ],
            "selected_attempt": 1,
        }
        for node in nodes
    ]
    review_manifest = {
        "runtime_execution": {
            "manifest_path": execution_path.name,
            "manifest_sha256": hashlib.sha256(execution_path.read_bytes()).hexdigest(),
            "plan_path": plan_path.name,
        }
    }
    return {
        "validator": validator,
        "manifest": manifest,
        "execution_path": execution_path,
        "out_dir": out_dir,
        "codex_home": codex_home,
        "nodes": nodes,
        "selected_passes": selected_passes,
        "review_manifest": review_manifest,
    }


class TestReviewRuntimeConsumer:
    """Protect review's consumer-bound portable runtime contract."""

    def test_accepts_real_schema_v2_evidence(self, real_schema_v2_review_runtime: dict[str, Any]) -> None:
        """Promote complete restricted-network evidence from the shared validator."""
        runtime = real_schema_v2_review_runtime
        summary = runtime["validator"]._validate_review_runtime(
            runtime["out_dir"],
            runtime["review_manifest"],
            runtime["selected_passes"],
            runtime["codex_home"],
            "parent-thread",
        )

        assert summary["evidence_level"] == "portable-read-restricted"
        assert summary["network_mode"] == "restricted"
        assert summary["approval_policy"] == "never"
        assert summary["filesystem_credential_isolation"] == "unverified"
        assert "network_guarantee" not in summary
        assert summary["write_parallel_eligible"] is False

    def test_rejects_legacy_control_tampering(self, real_schema_v2_review_runtime: dict[str, Any]) -> None:
        """Reject a legacy Boolean projected as a portable network restriction."""
        runtime = real_schema_v2_review_runtime
        runtime["nodes"][0]["observed_controls"]["network"] = False
        runtime["execution_path"].write_text(
            json.dumps(runtime["manifest"], indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        runtime["review_manifest"]["runtime_execution"]["manifest_sha256"] = hashlib.sha256(
            runtime["execution_path"].read_bytes()
        ).hexdigest()

        with pytest.raises(SystemExit, match="review-runtime-execution-invalid:runtime-node-capability-mismatch:N1"):
            runtime["validator"]._validate_review_runtime(
                runtime["out_dir"],
                runtime["review_manifest"],
                runtime["selected_passes"],
                runtime["codex_home"],
                "parent-thread",
            )

    def test_rejects_spawn_without_manifest(self, tmp_path: Path) -> None:
        """Require authoritative runtime evidence whenever a pass records a child spawn."""
        validator = _load_validator()

        with pytest.raises(SystemExit, match="review-runtime-execution-missing"):
            validator._validate_review_runtime(
                tmp_path,
                {},
                [{"role": "qa-specialist", "mode": "spawned"}],
                tmp_path,
                "parent-thread",
            )


def test_skill_requires_shared_runtime_gate_and_truthful_execution_labels() -> None:
    """Keep the producer from treating planned fan-out as runtime parallelism."""
    skill = CODE_REVIEW_SKILL.read_text(encoding="utf-8")

    assert "`<run-directory>/execution-plan.json`" in skill
    assert "`<run-directory>/execution-manifest.json`" in skill
    assert "`consumer_id=code-review`" in skill
    assert "`parallel` only when" in skill
    assert "`independent-spawned`" in skill
    assert "`serial-fallback`" in skill


def test_skill_rebuilds_a_compact_pr_snapshot_before_reporting_findings() -> None:
    """Keep assessed PR handoffs grounded in current review artifacts, not stale chat context."""
    skill = CODE_REVIEW_SKILL.read_text(encoding="utf-8")

    assert "`PR Snapshot` for every assessed `scope=pr` review" in skill
    assert "`pr.json`, `pr-routing.json`, and `gates.json`" in skill
    assert "`pr.json.statusCheckRollup`" in skill
    assert "An absent or empty rollup is `unavailable`, never `passing`" in skill
    assert "`fix`, `feat`, `refactor`, `perf`, `docs`, `ci`, `chore`, `test`, or `mixed`" in skill
    assert "`approve`, `minor changes`, `needs work`, `reject`, or `not aligned`" in skill
    assert "before any findings" in skill

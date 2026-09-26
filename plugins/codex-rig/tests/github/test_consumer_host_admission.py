"""Check shared host compatibility before promoted consumers dispatch read work."""

import hashlib
import json
from pathlib import Path

import pytest

from test_parallel_execution import _load_validator


@pytest.mark.parametrize("consumer", ["implement", "manage", "code-review"])
@pytest.mark.parametrize("host_case", ["absent", "role-default", "workspace-write", "compatible"])
@pytest.mark.parametrize("mode", ["serial", "auto", "parallel-read"])
def test_consumer_host_admission(tmp_path: Path, consumer: str, host_case: str, mode: str) -> None:
    """Reject incompatible explicit dispatch and keep automatic fallback non-independent."""
    plan = {
        "consumer_policy": {
            "consumer_id": consumer,
            "capability": "portable-read-only",
            "promotion_status": "promoted",
            "parent_mutations": "serial",
            "canonical_gates": "serial",
        },
        "write_policy": {"parent_writes": "none", "approval_requirement": "not-required"},
    }
    if host_case != "absent":
        plan["read_host"] = {
            "source": "role-default" if host_case == "role-default" else "runtime-tool-contract",
            "sandbox_mode": "workspace-write" if host_case == "workspace-write" else "read-only",
            "approval_policy": "never",
        }
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    validator = _load_validator()
    if mode == "parallel-read" and host_case != "compatible":
        with pytest.raises(ValueError, match="host-controls-unavailable-before-dispatch"):
            validator.resolve_consumer_execution_mode(
                consumer,
                f"--execution={mode}",
                environment={},
                plan_path=path,
                approval_path=None,
            )
        return
    result = validator.resolve_consumer_execution_mode(
        consumer,
        f"--execution={mode}",
        environment={},
        plan_path=path,
        approval_path=None,
    )
    expected = "parallel-read" if host_case == "compatible" and mode != "serial" else "serial"
    assert result["effective_mode"] == expected
    assert "runtime_promotion_eligible" not in result
    assert "evidence_level" not in result
    if mode == "auto" and host_case != "compatible":
        assert result["fallback_reason"] == "host-controls-unavailable-before-dispatch"


@pytest.mark.parametrize("mode", ["serial", "auto", "parallel-read"])
def test_review_inspection_admission_needs_no_child_permission_attestation(tmp_path: Path, mode: str) -> None:
    """Admit supplied-context review without claiming enforced child isolation."""
    plan = {
        "consumer_policy": {
            "consumer_id": "code-review",
            "capability": "instruction-bounded-review",
            "promotion_status": "promoted",
            "parent_mutations": "serial",
            "canonical_gates": "serial",
        },
        "review_operation": "inspection-only",
        "source_sensitivity": "non-sensitive",
        "contexts": [],
        "write_policy": {"parent_writes": "none", "approval_requirement": "not-required"},
    }
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    result = _load_validator().resolve_consumer_execution_mode(
        "code-review", f"--execution={mode}", environment={}, plan_path=path, approval_path=None
    )
    assert result["effective_mode"] == ("serial" if mode == "serial" else "parallel-read")
    assert result["capability"] == "instruction-bounded-review"
    assert result["write_approval_required"] is False
    assert "evidence_level" not in result
    assert "sandbox_mode" not in result
    assert "runtime_promotion_eligible" not in result


@pytest.mark.parametrize(
    ("consumer", "operation", "parent_writes", "host_key", "error"),
    [
        pytest.param("implement", "inspection-only", "none", None, "capability-invalid", id="implementation-excluded"),
        pytest.param("manage", "inspection-only", "none", None, "capability-invalid", id="management-excluded"),
        pytest.param("code-review", None, "none", None, "operation-invalid", id="operation-required"),
        pytest.param("code-review", "execute-tests", "none", None, "operation-invalid", id="execution-excluded"),
        pytest.param("code-review", "inspection-only", "planned", None, "writes-forbidden", id="write-plan-excluded"),
        pytest.param("code-review", "inspection-only", "none", "read_host", "host-claim-forbidden", id="no-host-claim"),
        pytest.param(
            "code-review", "inspection-only", "none", "review_host", "host-claim-forbidden", id="no-legacy-host-claim"
        ),
    ],
)
def test_review_inspection_cannot_authorize_other_operations(
    tmp_path: Path, consumer: str, operation: str | None, parent_writes: str, host_key: str | None, error: str
) -> None:
    """Keep the inspection exception scoped to non-executing code-review dispatch."""
    plan = {
        "consumer_policy": {
            "consumer_id": consumer,
            "capability": "instruction-bounded-review",
            "promotion_status": "promoted",
            "parent_mutations": "serial",
            "canonical_gates": "serial",
        },
        "review_operation": operation,
        "source_sensitivity": "non-sensitive",
        "contexts": [],
        "write_policy": {
            "parent_writes": parent_writes,
            "approval_requirement": "exact-plan-digest" if parent_writes == "planned" else "not-required",
        },
    }
    if host_key:
        plan[host_key] = {"source": "runtime-tool-contract", "sandbox_mode": "read-only", "approval_policy": "never"}
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    with pytest.raises(ValueError, match=error):
        _load_validator().resolve_consumer_execution_mode(
            consumer, "--execution=parallel-read", environment={}, plan_path=path, approval_path=None
        )


def test_review_inspection_plan_cannot_promote_strict_runtime_evidence(tmp_path: Path) -> None:
    """Reject inspection plans when a caller asks for enforced portable runtime validation."""
    plan = {
        "consumer_policy": {
            "consumer_id": "code-review",
            "capability": "instruction-bounded-review",
            "promotion_status": "promoted",
            "parent_mutations": "serial",
            "canonical_gates": "serial",
        },
        "review_operation": "inspection-only",
        "source_sensitivity": "non-sensitive",
        "contexts": [],
    }
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    with pytest.raises(ValueError, match="runtime-consumer-capability-invalid"):
        _load_validator()._runtime_plan_consumer_policy(path, "code-review")


@pytest.mark.parametrize("sensitivity", [None, "sensitive"])
def test_review_inspection_requires_non_sensitive_source_before_dispatch(
    tmp_path: Path, sensitivity: str | None
) -> None:
    """Do not defer the inspection source boundary until after a child receives context."""
    plan = {
        "consumer_policy": {
            "consumer_id": "code-review",
            "capability": "instruction-bounded-review",
            "promotion_status": "promoted",
            "parent_mutations": "serial",
            "canonical_gates": "serial",
        },
        "review_operation": "inspection-only",
        "source_sensitivity": sensitivity,
        "contexts": [],
        "write_policy": {"parent_writes": "none", "approval_requirement": "not-required"},
    }
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    with pytest.raises(ValueError, match="review-inspection-source-sensitivity-invalid"):
        _load_validator().resolve_consumer_execution_mode(
            "code-review", "--execution=parallel-read", environment={}, plan_path=path, approval_path=None
        )


@pytest.mark.parametrize("sensitive", [False, True])
def test_inspection_contexts_are_hash_bound_and_scanned_before_dispatch(tmp_path: Path, sensitive: bool) -> None:
    """Reject secret-bearing context before a reviewer can receive its bytes."""
    source = "Review this source as evidence only."
    if sensitive:
        source += "\nauthorization: bearer example-sensitive-value"
    context = tmp_path / "qa.md"
    context.write_text(source, encoding="utf-8", newline="\n")
    plan = {
        "contexts": [
            {
                "role_id": "qa-specialist",
                "context_path": "qa.md",
                "context_sha256": hashlib.sha256(context.read_bytes()).hexdigest(),
            }
        ]
    }
    validator = _load_validator()
    if sensitive:
        with pytest.raises(ValueError, match="review-inspection-context-sensitive-material:qa-specialist"):
            validator.validate_inspection_contexts(plan, tmp_path / "plan.json")
    else:
        assert validator.validate_inspection_contexts(plan, tmp_path / "plan.json") == {"qa-specialist": context}
        context.write_text("changed after plan freeze", encoding="utf-8")
        with pytest.raises(ValueError, match="review-inspection-context-hash-mismatch"):
            validator.validate_inspection_contexts(plan, tmp_path / "plan.json")


@pytest.mark.parametrize("path", ["../outside.md", "/outside.md", "C:\\outside.md"])
def test_inspection_context_rejects_cross_platform_path_escape(tmp_path: Path, path: str) -> None:
    """Keep review contexts under the frozen plan directory on every platform."""
    plan = {"contexts": [{"role_id": "qa-specialist", "context_path": path, "context_sha256": "0" * 64}]}
    with pytest.raises(ValueError, match="review-inspection-context-path-invalid"):
        _load_validator().validate_inspection_contexts(plan, tmp_path / "plan.json")

"""Check shared host compatibility before promoted consumers dispatch read work."""

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from test_parallel_execution import _load_validator

CONSUMERS = ["implement", "manage", "code-review"]
NO_WRITES = {"parent_writes": "none", "approval_requirement": "not-required"}
READ_HOSTS = {
    "role-default": {"source": "role-default", "sandbox_mode": "read-only", "approval_policy": "never"},
    "workspace-write": {
        "source": "runtime-tool-contract",
        "sandbox_mode": "workspace-write",
        "approval_policy": "never",
    },
    "compatible": {"source": "runtime-tool-contract", "sandbox_mode": "read-only", "approval_policy": "never"},
}
HOST_CONTROLS_UNAVAILABLE = "host-controls-unavailable-before-dispatch"


def _consumer_policy(consumer: str, capability: str) -> dict[str, str]:
    """Return the promoted consumer policy shared by every admission plan."""
    return {
        "consumer_id": consumer,
        "capability": capability,
        "promotion_status": "promoted",
        "parent_mutations": "serial",
        "canonical_gates": "serial",
    }


def _inspection_plan(**overrides: Any) -> dict[str, Any]:
    """Return a valid supplied-context code-review inspection plan with the given top-level overrides."""
    plan = {
        "consumer_policy": _consumer_policy("code-review", "instruction-bounded-review"),
        "review_operation": "inspection-only",
        "source_sensitivity": "non-sensitive",
        "contexts": [],
        "write_policy": NO_WRITES,
    }
    return plan | overrides


@pytest.fixture
def validator() -> ModuleType:
    """Return the parallel-execution validator module."""
    return _load_validator()


@pytest.fixture
def write_plan(tmp_path: Path) -> Callable[[dict[str, Any]], Path]:
    """Return a factory that freezes a plan dictionary to ``plan.json`` and returns its path."""

    def write(plan: dict[str, Any]) -> Path:
        """Serialize one plan beside the other test artifacts."""
        path = tmp_path / "plan.json"
        path.write_text(json.dumps(plan), encoding="utf-8")
        return path

    return write


def _read_plan(consumer: str, host_case: str) -> dict[str, Any]:
    """Return a portable read-only plan, with a host claim unless the case is ``absent``."""
    plan: dict[str, Any] = {
        "consumer_policy": _consumer_policy(consumer, "portable-read-only"),
        "write_policy": NO_WRITES,
    }
    if host_case != "absent":
        plan["read_host"] = READ_HOSTS[host_case]
    return plan


class TestConsumerHostAdmission:
    """Explicit and automatic dispatch against absent, mismatched and compatible host claims."""

    @pytest.mark.parametrize("consumer", CONSUMERS)
    @pytest.mark.parametrize(
        ("host_case", "mode", "effective_mode", "fallback_reason"),
        [
            pytest.param("absent", "serial", "serial", None, id="absent-serial"),
            pytest.param("absent", "auto", "serial", HOST_CONTROLS_UNAVAILABLE, id="absent-auto"),
            pytest.param("role-default", "serial", "serial", None, id="role-default-serial"),
            pytest.param("role-default", "auto", "serial", HOST_CONTROLS_UNAVAILABLE, id="role-default-auto"),
            pytest.param("workspace-write", "serial", "serial", None, id="workspace-write-serial"),
            pytest.param("workspace-write", "auto", "serial", HOST_CONTROLS_UNAVAILABLE, id="workspace-write-auto"),
            pytest.param("compatible", "serial", "serial", None, id="compatible-serial"),
            pytest.param("compatible", "auto", "parallel-read", None, id="compatible-auto"),
            pytest.param("compatible", "parallel-read", "parallel-read", None, id="compatible-parallel-read"),
        ],
    )
    def test_resolves_effective_mode(
        self,
        validator: ModuleType,
        write_plan: Callable[[dict[str, Any]], Path],
        consumer: str,
        host_case: str,
        mode: str,
        effective_mode: str,
        fallback_reason: str | None,
    ) -> None:
        """Automatic dispatch falls back to serial with a reason unless the host claim is compatible.

        Only a runtime-attested read-only host admits parallel reads. Every other claim keeps serial dispatch, an
        automatic request records why, and no resolution invents promotion or evidence-level claims.
        """
        path = write_plan(_read_plan(consumer, host_case))

        result = validator.resolve_consumer_execution_mode(
            consumer, f"--execution={mode}", environment={}, plan_path=path, approval_path=None
        )

        assert result["effective_mode"] == effective_mode
        assert result.get("fallback_reason") == fallback_reason
        assert "runtime_promotion_eligible" not in result
        assert "evidence_level" not in result

    @pytest.mark.parametrize("consumer", CONSUMERS)
    @pytest.mark.parametrize("host_case", ["absent", "role-default", "workspace-write"])
    def test_rejects_explicit_parallel_read_without_compatible_host(
        self, validator: ModuleType, write_plan: Callable[[dict[str, Any]], Path], consumer: str, host_case: str
    ) -> None:
        """An explicit parallel-read request fails before dispatch when host controls are incompatible.

        Silently downgrading an explicit request would hide that read work never got its isolation.
        """
        path = write_plan(_read_plan(consumer, host_case))

        with pytest.raises(ValueError, match=HOST_CONTROLS_UNAVAILABLE):
            validator.resolve_consumer_execution_mode(
                consumer, "--execution=parallel-read", environment={}, plan_path=path, approval_path=None
            )


class TestReviewInspection:
    """Supplied-context review admission without enforced child isolation."""

    @pytest.mark.parametrize("mode", ["serial", "auto", "parallel-read"])
    def test_admission_needs_no_child_permission_attestation(
        self, validator: ModuleType, write_plan: Callable[[dict[str, Any]], Path], mode: str
    ) -> None:
        """Supplied-context review is admitted without claiming enforced child isolation.

        An inspection-only plan carries no host claim; parallel dispatch is still allowed, but the result must not
        report sandbox or evidence-level facts it never verified.
        """
        path = write_plan(_inspection_plan())

        result = validator.resolve_consumer_execution_mode(
            "code-review", f"--execution={mode}", environment={}, plan_path=path, approval_path=None
        )

        assert result["effective_mode"] == ("serial" if mode == "serial" else "parallel-read")
        assert result["capability"] == "instruction-bounded-review"
        assert result["write_approval_required"] is False
        assert "evidence_level" not in result
        assert "sandbox_mode" not in result
        assert "runtime_promotion_eligible" not in result

    @pytest.mark.parametrize(
        ("plan", "consumer", "error"),
        [
            pytest.param(
                _inspection_plan(consumer_policy=_consumer_policy("implement", "instruction-bounded-review")),
                "implement",
                "capability-invalid",
                id="implementation-excluded",
            ),
            pytest.param(
                _inspection_plan(consumer_policy=_consumer_policy("manage", "instruction-bounded-review")),
                "manage",
                "capability-invalid",
                id="management-excluded",
            ),
            pytest.param(
                _inspection_plan(review_operation=None), "code-review", "operation-invalid", id="operation-required"
            ),
            pytest.param(
                _inspection_plan(review_operation="execute-tests"),
                "code-review",
                "operation-invalid",
                id="execution-excluded",
            ),
            pytest.param(
                _inspection_plan(
                    write_policy={"parent_writes": "planned", "approval_requirement": "exact-plan-digest"}
                ),
                "code-review",
                "writes-forbidden",
                id="write-plan-excluded",
            ),
            pytest.param(
                _inspection_plan(read_host=READ_HOSTS["compatible"]),
                "code-review",
                "host-claim-forbidden",
                id="no-host-claim",
            ),
            pytest.param(
                _inspection_plan(review_host=READ_HOSTS["compatible"]),
                "code-review",
                "host-claim-forbidden",
                id="no-legacy-host-claim",
            ),
        ],
    )
    def test_cannot_authorize_other_operations(
        self,
        validator: ModuleType,
        write_plan: Callable[[dict[str, Any]], Path],
        plan: dict[str, Any],
        consumer: str,
        error: str,
    ) -> None:
        """The inspection exception stays scoped to non-executing code-review dispatch.

        Each plan is valid inspection input except for one widening attempt: another consumer, a missing or
        executing operation, planned writes, or an attached host claim.
        """
        path = write_plan(plan)

        with pytest.raises(ValueError, match=error):
            validator.resolve_consumer_execution_mode(
                consumer, "--execution=parallel-read", environment={}, plan_path=path, approval_path=None
            )

    def test_plan_cannot_promote_strict_runtime_evidence(
        self, validator: ModuleType, write_plan: Callable[[dict[str, Any]], Path]
    ) -> None:
        """Inspection plans are rejected when a caller asks for enforced portable runtime validation.

        The plan has no write policy, which the strict runtime path requires; the inspection exception must not satisfy
        it.
        """
        plan = _inspection_plan()
        del plan["write_policy"]
        path = write_plan(plan)

        with pytest.raises(ValueError, match="runtime-consumer-capability-invalid"):
            validator._runtime_plan_consumer_policy(path, "code-review")

    @pytest.mark.parametrize("sensitivity", [None, "sensitive"])
    def test_requires_non_sensitive_source_before_dispatch(
        self, validator: ModuleType, write_plan: Callable[[dict[str, Any]], Path], sensitivity: str | None
    ) -> None:
        """The source-sensitivity boundary is enforced before any child receives context."""
        path = write_plan(_inspection_plan(source_sensitivity=sensitivity))

        with pytest.raises(ValueError, match="review-inspection-source-sensitivity-invalid"):
            validator.resolve_consumer_execution_mode(
                "code-review", "--execution=parallel-read", environment={}, plan_path=path, approval_path=None
            )


class TestInspectionContexts:
    """Hash binding, secret scanning and path confinement of frozen review contexts."""

    @staticmethod
    def _plan_for(context: Path) -> dict[str, Any]:
        """Return a one-context plan whose digest matches the file currently on disk."""
        digest = hashlib.sha256(context.read_bytes()).hexdigest()
        return {"contexts": [{"role_id": "qa-specialist", "context_path": context.name, "context_sha256": digest}]}

    def test_accepts_hash_bound_context(self, validator: ModuleType, tmp_path: Path) -> None:
        """A context whose bytes match the frozen digest is admitted under its role id."""
        context = tmp_path / "qa.md"
        context.write_text("Review this source as evidence only.", encoding="utf-8", newline="\n")

        admitted = validator.validate_inspection_contexts(self._plan_for(context), tmp_path / "plan.json")

        assert admitted == {"qa-specialist": context}

    def test_rejects_context_changed_after_plan_freeze(self, validator: ModuleType, tmp_path: Path) -> None:
        """Editing a context after the plan froze its digest is detected before dispatch."""
        context = tmp_path / "qa.md"
        context.write_text("Review this source as evidence only.", encoding="utf-8", newline="\n")
        plan = self._plan_for(context)
        context.write_text("changed after plan freeze", encoding="utf-8")

        with pytest.raises(ValueError, match="review-inspection-context-hash-mismatch"):
            validator.validate_inspection_contexts(plan, tmp_path / "plan.json")

    def test_rejects_secret_bearing_context(self, validator: ModuleType, tmp_path: Path) -> None:
        """A context carrying credential material is rejected before a reviewer can receive its bytes."""
        context = tmp_path / "qa.md"
        context.write_text(
            "Review this source as evidence only.\nauthorization: bearer example-sensitive-value",
            encoding="utf-8",
            newline="\n",
        )

        with pytest.raises(ValueError, match="review-inspection-context-sensitive-material:qa-specialist"):
            validator.validate_inspection_contexts(self._plan_for(context), tmp_path / "plan.json")

    @pytest.mark.parametrize("path", ["../outside.md", "/outside.md", "C:\\outside.md"])
    def test_rejects_cross_platform_path_escape(self, validator: ModuleType, tmp_path: Path, path: str) -> None:
        """Review contexts stay under the frozen plan directory on every platform."""
        plan = {"contexts": [{"role_id": "qa-specialist", "context_path": path, "context_sha256": "0" * 64}]}

        with pytest.raises(ValueError, match="review-inspection-context-path-invalid"):
            validator.validate_inspection_contexts(plan, tmp_path / "plan.json")

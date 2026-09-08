"""Check shared host compatibility before promoted consumers dispatch read work."""

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

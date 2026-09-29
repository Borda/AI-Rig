"""Regression checks for the bounded reasoning-progress escalation ledger."""

from __future__ import annotations

import importlib.util
from copy import deepcopy
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[2]
LEDGER_PATH = PLUGIN_ROOT / "shared" / "escalation_ledger.py"


def _load_ledger_module() -> ModuleType:
    """Load the standalone ledger helper without package installation."""
    specification = importlib.util.spec_from_file_location("codex_rig_escalation_ledger", LEDGER_PATH)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _cycle(index: int, material_progress: bool) -> dict[str, Any]:
    """Build one valid cycle record with evidence when progress is claimed.

    Example:
        >>> _cycle(1, True)["evidence"]
        ['evidence-1']
    """
    return {
        "index": index,
        "objective": "close failing acceptance check",
        "operation": f"attempt-{index}",
        "outcome": "acceptance condition remains open",
        "next_decision": "record the observed result",
        "material_progress": material_progress,
        "work_kind": "primary",
        "evidence": [f"evidence-{index}"] if material_progress else [],
    }


def _ledger(*cycles: dict[str, Any]) -> dict[str, Any]:
    """Build a minimally valid in-progress ledger for one open condition.

    Example:
        >>> _ledger()["closure_condition"]["status"]
        'open'
    """
    return {
        "schema_version": 2,
        "primary_goal": "Fix the observed runtime defect.",
        "workstream_id": "fixture-workstream",
        "closure_condition": {"id": "acceptance-check", "status": "open"},
        "cycles": list(cycles),
        "outcome": "working",
    }


def _advisory() -> dict[str, Any]:
    """Build an observed read-only advisory record.

    Example:
        >>> _advisory()["observed_sandbox"]
        'read-only'
    """
    return {
        "requested_model": "gpt-5.6-terra",
        "requested_effort": "high",
        "observed_model": "gpt-5.6-terra",
        "observed_effort": "high",
        "observed_sandbox": "read-only",
        "mode": "advice-only",
        "state_changes": [],
        "recommendation": "run one bounded recovery check",
        "stop_condition": "handoff if acceptance remains open",
    }


def _handoff() -> dict[str, Any]:
    """Build the required evidence-backed human handoff record.

    Example:
        >>> _handoff()["alternatives"]
        ['defer the workstream']
    """
    return {
        "summary": "The bounded recovery did not close the condition.",
        "recommended_next_step": "Choose the proposed recovery direction.",
        "alternatives": ["defer the workstream"],
    }


def test_two_no_progress_cycles_require_escalation() -> None:
    """Prevent silent retries after the no-material-progress trigger."""
    module = _load_ledger_module()
    stalled = _ledger(_cycle(1, False), _cycle(2, False))

    with pytest.raises(ValueError, match="escalation-required-after-stall-trigger"):
        module.validate_ledger(stalled)

    stalled["outcome"] = "advisory"
    stalled["advisory"] = _advisory()
    module.validate_ledger(stalled)


def test_auxiliary_receipt_repairs_cannot_count_as_primary_progress() -> None:
    """Prevent report repairs from resetting a stalled implementation's progress counter."""
    module = _load_ledger_module()
    active = _ledger(_cycle(1, False), _cycle(2, True))
    active["primary_goal"] = "Fix the observed runtime defect."
    active["cycles"][1].update(
        work_kind="auxiliary",
        required_for="independent review evidence",
        operation="repair reviewer metadata",
        outcome="receipt validator passes; requested code remains unchanged",
    )

    with pytest.raises(ValueError, match="auxiliary-work-is-not-primary-progress"):
        module.validate_ledger(active)


def test_auxiliary_setup_does_not_reset_the_primary_stall() -> None:
    """Count a correctly labeled auxiliary success as no progress toward the requested fix."""
    module = _load_ledger_module()
    stalled = _ledger(_cycle(1, False), _cycle(2, False))
    stalled["cycles"][1].update(work_kind="auxiliary", required_for="independent review evidence")

    with pytest.raises(ValueError, match="escalation-required-after-stall-trigger"):
        module.validate_ledger(stalled)


def test_current_progress_ledger_requires_primary_goal() -> None:
    """Prevent auxiliary closure conditions from replacing an omitted user objective."""
    module = _load_ledger_module()
    active = _ledger(_cycle(1, True))
    del active["primary_goal"]

    with pytest.raises(ValueError, match="primary-goal-required"):
        module.validate_ledger(active)


def test_old_schema_cannot_bypass_primary_progress_contract() -> None:
    """Require active owners to upgrade old ledgers rather than weaken current stall checks."""
    module = _load_ledger_module()
    active = _ledger(_cycle(1, True))
    active["schema_version"] = 1

    with pytest.raises(ValueError, match="unsupported-schema-version"):
        module.validate_ledger(active)


def test_user_directed_progress_does_not_count_as_evidence_free() -> None:
    """Keep a recorded user decision from falsely triggering advisory escalation."""
    module = _load_ledger_module()
    active = _ledger(_cycle(1, False), _cycle(2, True))
    active["cycles"][1]["evidence"] = ["user approved narrowed scope"]

    module.validate_ledger(active)


def test_three_nonclosing_evidence_backed_cycles_require_advisory() -> None:
    """Escalate productive but non-closing attempts against one open condition."""
    module = _load_ledger_module()
    stalled = _ledger(_cycle(1, True), _cycle(2, True), _cycle(3, True))

    with pytest.raises(ValueError, match="escalation-required-after-stall-trigger"):
        module.validate_ledger(stalled)

    stalled["outcome"] = "advisory"
    stalled["advisory"] = _advisory()
    module.validate_ledger(stalled)


@pytest.mark.parametrize("auxiliary", [False, True])
def test_mixed_progress_attempts_cannot_bypass_nonclosing_stop(auxiliary: bool) -> None:
    """Count observed primary attempts even when one yields no material progress."""
    module = _load_ledger_module()
    cycles = [_cycle(1, True), _cycle(2, False), _cycle(3, True)]
    cycles[1]["evidence"] = ["Acceptance check still fails after the second attempted fix."]
    if auxiliary:
        repair = _cycle(3, False)
        repair.update(work_kind="auxiliary", required_for="independent review evidence", evidence=["receipt repair"])
        cycles.insert(2, repair)
        cycles[-1]["index"] = 4
    stalled = _ledger(*cycles)

    with pytest.raises(ValueError, match="escalation-required-after-stall-trigger"):
        module.validate_ledger(stalled)


def test_two_observed_attempts_and_auxiliary_work_do_not_become_three_attempts() -> None:
    """Keep setup evidence separate from attempts at the primary acceptance condition."""
    module = _load_ledger_module()
    cycles = [_cycle(1, True), _cycle(2, False), _cycle(3, True)]
    cycles[1].update(work_kind="auxiliary", required_for="independent review evidence", evidence=["receipt repair"])

    module.validate_ledger(_ledger(*cycles))


def test_replaced_condition_requires_recorded_authority() -> None:
    """Reject a bare replacement claim that bypasses the primary attempt bound."""
    module = _load_ledger_module()
    stalled = _ledger(_cycle(1, True), _cycle(2, True), _cycle(3, True))
    stalled["closure_condition"]["status"] = "replaced"

    with pytest.raises(ValueError, match="closure-replacement-object-required"):
        module.validate_ledger(stalled)


@pytest.mark.parametrize("source", ["user-direction", "external-state"])
def test_evidenced_condition_replacement_preserves_authorized_continuation(source: str) -> None:
    """Allow a distinct replacement supported by a recorded decision or state change."""
    module = _load_ledger_module()
    active = _ledger(_cycle(1, True), _cycle(2, True), _cycle(3, True))
    active["closure_condition"].update(
        status="replaced",
        replacement={"id": "revised-acceptance", "source": source, "evidence": ["Retained decision or state change."]},
    )

    module.validate_ledger(active)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        pytest.param("id", "", "closure-replacement-id-required", id="missing-new-condition"),
        pytest.param("id", "acceptance-check", "closure-replacement-must-change-condition", id="unchanged-condition"),
        pytest.param("source", "agent-setup", "closure-replacement-source-invalid", id="auxiliary-claim"),
        pytest.param("evidence", [], "closure-replacement-evidence-required", id="missing-evidence"),
        pytest.param("evidence", [True], "closure-replacement-evidence-required", id="nontext-evidence"),
    ],
)
def test_condition_replacement_rejects_incomplete_proof(field: str, value: object, error: str) -> None:
    """Keep replacement authority and a distinct new criterion observable."""
    module = _load_ledger_module()
    active = _ledger(_cycle(1, True), _cycle(2, True), _cycle(3, True))
    replacement = {"id": "revised-acceptance", "source": "user-direction", "evidence": ["Retained user decision."]}
    replacement[field] = value
    active["closure_condition"].update(status="replaced", replacement=replacement)

    with pytest.raises(ValueError, match=error):
        module.validate_ledger(active)


def test_advisory_requires_observed_read_only_route() -> None:
    """Reject advisory claims that lack an observed read-only sandbox."""
    module = _load_ledger_module()
    stalled = _ledger(_cycle(1, False), _cycle(2, False))
    stalled.update({"outcome": "advisory", "advisory": _advisory()})
    unsafe = deepcopy(stalled)
    unsafe["advisory"]["observed_sandbox"] = "workspace-write"

    with pytest.raises(ValueError, match="advisory-read-only-sandbox-required"):
        module.validate_ledger(unsafe)


def test_unsuccessful_recovery_requires_complete_human_handoff() -> None:
    """Prevent second advisors or retries after the bounded recovery action."""
    module = _load_ledger_module()
    stalled = _ledger(_cycle(1, False), _cycle(2, False))
    stalled.update(
        {
            "outcome": "recovery",
            "advisory": _advisory(),
            "recovery": {"action": "run one diagnostic", "material_progress": False, "closure_met": False},
        }
    )

    with pytest.raises(ValueError, match="unsuccessful-recovery-requires-human-handoff"):
        module.validate_ledger(stalled)

    stalled["outcome"] = "human_handoff"
    stalled["human_handoff"] = _handoff()
    module.validate_ledger(stalled)

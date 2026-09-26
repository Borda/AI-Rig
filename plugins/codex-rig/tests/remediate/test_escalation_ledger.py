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
        "evidence": [f"evidence-{index}"] if material_progress else [],
    }


def _ledger(*cycles: dict[str, Any]) -> dict[str, Any]:
    """Build a minimally valid in-progress ledger for one open condition.

    Example:
        >>> _ledger()["closure_condition"]["status"]
        'open'
    """
    return {
        "schema_version": 1,
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

"""Regression checks for the bounded reasoning-progress escalation ledger."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
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
    assert specification is not None
    assert specification.loader is not None
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
        "schema_version": 3,
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
    stalled = _ledger(_cycle(1, False), _cycle(2, False), _cycle(3, False))
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


@pytest.mark.parametrize("version", [1, 2])
def test_old_schema_cannot_bypass_primary_progress_contract(version: int) -> None:
    """Require active owners to upgrade old ledgers rather than weaken current stall checks."""
    module = _load_ledger_module()
    active = _ledger(_cycle(1, True))
    active["schema_version"] = version

    with pytest.raises(ValueError, match="unsupported-schema-version"):
        module.validate_ledger(active)


def test_historical_inline_ledger_is_read_as_an_archive_under_current_rules() -> None:
    """Validate a schema-2 archive with inline cycles only when explicitly read as history.

    The archive still meets every stall rule, so a stalled historical ledger stays invalid and a historical read never
    relaxes the contract; it only accepts the old single-file shape.
    """
    module = _load_ledger_module()
    archive = _ledger(_cycle(1, True)) | {"schema_version": 2}
    stalled = _ledger(_cycle(1, False), _cycle(2, False)) | {"schema_version": 2}

    module.validate_ledger(archive, historical=True)
    with pytest.raises(ValueError, match="escalation-required-after-stall-trigger"):
        module.validate_ledger(stalled, historical=True)
    with pytest.raises(ValueError, match="unsupported-schema-version"):
        module.validate_ledger(_ledger(_cycle(1, True)), historical=True)


def _run_cli(ledger_path: Path, *flags: str) -> subprocess.CompletedProcess[str]:
    """Run the standalone escalation-ledger CLI as a lifecycle owner does."""
    command = [sys.executable, str(LEDGER_PATH), "--ledger", str(ledger_path), *flags]
    return subprocess.run(command, capture_output=True, text=True, check=False)


def _stage(run: Path, cycle: dict[str, Any]) -> None:
    """Stage one work cycle beside the header with the file tool."""
    (run / "reasoning-cycles.jsonl.rec").write_text(json.dumps(cycle, indent=2), encoding="utf-8")


def _write_header(path: Path) -> None:
    """Write a current schema-3 header holding every field except the appended cycles."""
    header = {key: value for key, value in _ledger().items() if key != "cycles"}
    path.write_text(json.dumps(header), encoding="utf-8", newline="\n")


def test_append_records_cycles_without_rewriting_earlier_lines(tmp_path: Path) -> None:
    """Grow the cycle log one staged cycle at a time and validate the assembled ledger.

    The second cycle stalls the workstream: it is still appended as an observed fact, the CLI then fails until the
    header records the escalation, and the first cycle's line stays byte-identical throughout.
    """
    module = _load_ledger_module()
    ledger_path = tmp_path / "reasoning-progress.json"
    _write_header(ledger_path)
    _stage(tmp_path, _cycle(1, False))
    first = _run_cli(ledger_path, "--append")
    first_line = (tmp_path / "reasoning-cycles.jsonl").read_bytes()
    _stage(tmp_path, _cycle(2, False))
    stalled = _run_cli(ledger_path, "--append")
    header = json.loads(ledger_path.read_text(encoding="utf-8")) | {"outcome": "advisory", "advisory": _advisory()}
    ledger_path.write_text(json.dumps(header), encoding="utf-8", newline="\n")
    escalated = _run_cli(ledger_path)

    log = (tmp_path / "reasoning-cycles.jsonl").read_bytes()
    assert (first.returncode, first.stdout.strip()) == (0, "escalation-ledger-valid")
    assert (stalled.returncode, stalled.stdout.strip()) == (
        2,
        "escalation-ledger-invalid:escalation-required-after-stall-trigger",
    )
    assert (escalated.returncode, escalated.stdout.strip()) == (0, "escalation-ledger-valid")
    assert log.startswith(first_line)
    assert len(log.splitlines()) == 2
    assert not (tmp_path / "reasoning-cycles.jsonl.rec").exists()
    assert module.load_ledger(ledger_path)["cycles"] == [_cycle(1, False), _cycle(2, False)]


@pytest.mark.parametrize(
    ("staged", "error"),
    [
        pytest.param(_cycle(3, True), "cycle-index-must-be-contiguous", id="skipped-index"),
        pytest.param({**_cycle(2, True), "evidence": []}, "material-progress-evidence-required", id="unproven"),
    ],
)
def test_append_refuses_invalid_cycle_and_keeps_staged_record(
    tmp_path: Path, staged: dict[str, Any], error: str
) -> None:
    """Reject a staged cycle that breaks the sequence or cycle rules, appending nothing."""
    ledger_path = tmp_path / "reasoning-progress.json"
    _write_header(ledger_path)
    _stage(tmp_path, _cycle(1, True))
    assert _run_cli(ledger_path, "--append").returncode == 0
    before = (tmp_path / "reasoning-cycles.jsonl").read_bytes()
    _stage(tmp_path, staged)

    rejected = _run_cli(ledger_path, "--append")

    assert (rejected.returncode, rejected.stdout.strip()) == (2, f"escalation-ledger-invalid:{error}")
    assert (tmp_path / "reasoning-cycles.jsonl").read_bytes() == before
    assert (tmp_path / "reasoning-cycles.jsonl.rec").is_file()


@pytest.mark.parametrize(
    ("payload", "flags", "expected"),
    [
        pytest.param(
            _ledger(_cycle(1, True)), (), "escalation-ledger-invalid:ledger-header-inline-cycles-forbidden", id="dup"
        ),
        pytest.param(
            _ledger(_cycle(1, True)) | {"schema_version": 2},
            (),
            "escalation-ledger-invalid:unsupported-schema-version",
            id="historical-as-active",
        ),
        pytest.param(
            _ledger(_cycle(1, True)) | {"schema_version": 2},
            ("--historical",),
            "escalation-ledger-historical-valid",
            id="historical-archive",
        ),
        pytest.param(
            _ledger(_cycle(1, True)) | {"schema_version": 2},
            ("--append",),
            "escalation-ledger-invalid:append-requires-current-schema",
            id="historical-append",
        ),
    ],
)
def test_cli_reads_only_the_declared_ledger_shape(
    tmp_path: Path, payload: dict[str, Any], flags: tuple[str, ...], expected: str
) -> None:
    """Keep one ledger shape per schema: inline cycles are an archive, never a current or growing ledger."""
    ledger_path = tmp_path / "reasoning-progress.json"
    ledger_path.write_text(json.dumps(payload), encoding="utf-8", newline="\n")
    _stage(tmp_path, _cycle(2, True))

    result = _run_cli(ledger_path, *flags)

    assert result.stdout.strip() == expected


def test_user_directed_progress_does_not_count_as_evidence_free() -> None:
    """Keep a recorded user decision from falsely triggering advisory escalation."""
    module = _load_ledger_module()
    active = _ledger(_cycle(1, False), _cycle(2, True))
    active["cycles"][1]["evidence"] = ["user approved narrowed scope"]

    module.validate_ledger(active)


def test_three_productive_nonclosing_cycles_remain_working() -> None:
    """Keep evidence-backed diagnostics working while their acceptance condition remains open."""
    module = _load_ledger_module()
    stalled = _ledger(_cycle(1, True), _cycle(2, True), _cycle(3, True))

    module.validate_ledger(stalled)


@pytest.mark.parametrize("auxiliary", [False, True])
def test_mixed_productive_cycles_do_not_trigger_failed_attempt_stop(auxiliary: bool) -> None:
    """Distinguish one failed diagnostic from three failed attempts, including setup rows."""
    module = _load_ledger_module()
    cycles = [_cycle(1, True), _cycle(2, False), _cycle(3, True)]
    cycles[1]["evidence"] = ["Acceptance check still fails after the second attempted fix."]
    if auxiliary:
        repair = _cycle(3, False)
        repair.update(work_kind="auxiliary", required_for="independent review evidence", evidence=["receipt repair"])
        cycles.insert(2, repair)
        cycles[-1]["index"] = 4
    stalled = _ledger(*cycles)

    module.validate_ledger(stalled)


def test_productive_primary_cycles_ignore_auxiliary_rows() -> None:
    """Administrative rows cannot turn productive primary work into a two-cycle stall."""
    cycles = [_cycle(1, True), _cycle(2, False), _cycle(3, False), _cycle(4, True), _cycle(5, True)]
    for cycle in cycles[1:3]:
        cycle.update(work_kind="auxiliary", required_for="independent review evidence")
    _load_ledger_module().validate_ledger(_ledger(*cycles))


def test_primary_progress_starts_new_failed_attempt_sequence() -> None:
    """Observed primary progress separates old failures from the current diagnostic sequence."""
    cycles = [_cycle(index, index in {2, 5}) for index in range(1, 7)]
    for cycle in cycles:
        cycle["evidence"] = [f"Observed diagnostic result {cycle['index']}."]
    cycles[3].update(work_kind="auxiliary", required_for="independent review evidence")
    _load_ledger_module().validate_ledger(_ledger(*cycles))


def test_useful_unfinished_recovery_resumes_working() -> None:
    """Resume from observed recovery progress without pretending the condition is closed."""
    active = _ledger(_cycle(1, False), _cycle(2, False), _cycle(3, True))
    active["cycles"][-1]["operation"] = "run one diagnostic"
    active.update(
        advisory=_advisory(), recovery={"action": "run one diagnostic", "material_progress": True, "closure_met": False}
    )
    _load_ledger_module().validate_ledger(active)
    active["cycles"].append(_cycle(4, True))
    _load_ledger_module().validate_ledger(active)


def test_one_ordinary_failure_after_productive_recovery_remains_working() -> None:
    """Recovery history cannot impose a stricter threshold on later ordinary diagnostics."""
    active = _ledger(_cycle(1, True), _cycle(2, False))
    active.update(
        advisory=_advisory(), recovery={"action": "attempt-1", "material_progress": True, "closure_met": False}
    )
    _load_ledger_module().validate_ledger(active)
    active["cycles"].append(_cycle(3, False))
    with pytest.raises(ValueError, match="escalation-required-after-stall-trigger"):
        _load_ledger_module().validate_ledger(active)


@pytest.mark.parametrize("mismatch", ["progress", "operation", "evidence"])
def test_recovery_progress_requires_current_observed_cycle(mismatch: str) -> None:
    """Reject a recovery boolean unsupported by the latest primary operation and evidence."""
    active = _ledger(_cycle(1, True))
    active.update(
        advisory=_advisory(), recovery={"action": "attempt-1", "material_progress": True, "closure_met": False}
    )
    if mismatch == "progress":
        active["cycles"][-1]["material_progress"] = False
    elif mismatch == "operation":
        active["recovery"]["action"] = "unobserved action"
    else:
        active["cycles"][-1]["evidence"] = []
    with pytest.raises(ValueError, match=r"recovery-progress-evidence-required|material-progress-evidence-required"):
        _load_ledger_module().validate_ledger(active)


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


@pytest.mark.parametrize("terminal", ["closed", "human_handoff"])
def test_productive_recovery_keeps_original_evidence_after_later_terminal_state(terminal: str) -> None:
    """Preserve the recovery outcome when subsequent work closes or genuinely stalls."""
    active = _ledger(_cycle(1, True))
    active.update(
        advisory=_advisory(), recovery={"action": "attempt-1", "material_progress": True, "closure_met": False}
    )
    if terminal == "closed":
        active["cycles"].append(_cycle(2, True))
        active["closure_condition"]["status"] = "closed"
    else:
        active["cycles"].extend([_cycle(2, False), _cycle(3, False)])
        active["human_handoff"] = _handoff()
    active["outcome"] = terminal
    _load_ledger_module().validate_ledger(active)

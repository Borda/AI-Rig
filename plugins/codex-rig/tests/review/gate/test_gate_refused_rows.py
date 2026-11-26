"""Contract tests for gate rows the source guard refused before execution.

A refused gate never ran, so it records ``status: refused`` with exit code 0 and a reason, the run records one top-level
``refusal`` and an exact ``checks_refused`` list, and the gate run fails without listing the refused gates as failed
checks. A result over such gates must name ``source-refused`` and can never pass.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _modules import load_module

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
VALIDATOR = load_module("gate_refusal_validator", PLUGIN_ROOT / "shared" / "validate-artifacts.py")
GATE_IDS = ["lint", "format", "types", "tests", "review"]
REASON = "source refused before execution; see refusal"


def _refused_gates(tmp_path: Path) -> dict:
    """Write a gates.json whose five rows were all refused by the source guard, with their logs."""
    checks = []
    for gate_id in GATE_IDS:
        logs = {key: f"checks/{gate_id}.{key}.txt" for key in ("command_path", "stdout", "stderr")}
        for relative in logs.values():
            path = tmp_path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")
        checks.append(
            {"id": gate_id, "status": "refused", "exit_code": 0, "duration_seconds": 0.0, "reason": REASON, **logs}
        )
    gates = {
        "status": "fail",
        "checks_failed": [],
        "checks_refused": GATE_IDS,
        "refusal": {"cause": "source-dirty", "reason": "tracked edits in the review worktree"},
        "checks": checks,
    }
    (tmp_path / "gates.json").write_text(json.dumps(gates), encoding="utf-8")
    return gates


def _rewrite(tmp_path: Path, gates: dict) -> None:
    """Persist a mutated gate record."""
    (tmp_path / "gates.json").write_text(json.dumps(gates), encoding="utf-8")


def test_refused_rows_validate_as_a_failed_run_without_failed_checks(tmp_path: Path) -> None:
    """Refused gates make the run fail while the failed-check list stays reserved for executed failures."""
    gates = _refused_gates(tmp_path)
    assert VALIDATOR._validate_gates(tmp_path) == gates


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        pytest.param(lambda gates: gates.pop("refusal"), "gate-refused-without-refusal", id="refusal-missing"),
        pytest.param(
            lambda gates: gates["refusal"].update(cause="network-unavailable"), "gate-refusal-invalid", id="bad-cause"
        ),
        pytest.param(lambda gates: gates.update(checks_refused=["lint"]), "gate-refused-list-mismatch", id="list"),
        pytest.param(lambda gates: gates.update(status="pass"), "gates-status-mismatch", id="status-pass"),
        pytest.param(
            lambda gates: gates["checks"][0].update(exit_code=1), "gate-check-exit-status-mismatch:0", id="exit-code"
        ),
        pytest.param(lambda gates: gates["checks"][0].pop("reason"), "gate-check-missing-reason:0", id="reason"),
    ],
)
def test_inconsistent_refusal_records_are_rejected(tmp_path: Path, mutate, code: str) -> None:
    """Every refusal fact must agree: record, cause, refused list, run status, exit code and reason."""
    gates = _refused_gates(tmp_path)
    mutate(gates)
    _rewrite(tmp_path, gates)
    with pytest.raises(SystemExit) as error:
        VALIDATOR._validate_gates(tmp_path)
    assert error.value.code == code


def test_result_over_refused_gates_must_name_the_refusal(tmp_path: Path) -> None:
    """A result reconciled with refused gates fails unless it names ``source-refused``."""
    gates = _refused_gates(tmp_path)
    result = {"status": "fail", "checks_run": GATE_IDS, "checks_failed": ["verification-readiness"]}
    with pytest.raises(SystemExit) as error:
        VALIDATOR._reconcile_result_with_gates(result, gates, "code-review")
    assert error.value.code == "result-refused-gates-unnamed"
    VALIDATOR._reconcile_result_with_gates({**result, "checks_failed": ["source-refused"]}, gates, "code-review")
    with pytest.raises(SystemExit) as passing:
        VALIDATOR._reconcile_result_with_gates({**result, "status": "pass", "checks_failed": []}, gates, "code-review")
    assert passing.value.code == "result-pass-with-failed-gates"


def test_refusal_record_without_refused_rows_stays_valid(tmp_path: Path) -> None:
    """A producer that predates refused rows may attach the refusal record to its ordinary rows."""
    gates = _refused_gates(tmp_path)
    for check in gates["checks"]:
        check.update(status="not-applicable", reason="source refused before execution")
    gates.update(status="pass")
    gates.pop("checks_refused")
    _rewrite(tmp_path, gates)
    assert VALIDATOR._validate_gates(tmp_path) == gates

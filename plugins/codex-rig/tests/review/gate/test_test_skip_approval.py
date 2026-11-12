"""Require passing implementation results to execute tests or retain a bound user skip decision."""

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from _modules import load_module

SHARED = Path(__file__).resolve().parents[3] / "shared"
VALIDATOR = load_module("skip_approval_validator", SHARED / "validate-artifacts.py")
WRITER = load_module("skip_approval_writer", SHARED / "write-result.py")
GATE_IDS = ["lint", "format", "types", "tests", "review"]
DOCS_ONLY_REASON = "documentation-only change"


def _gates(reason: str = "text-only change"):
    """Build a review-only run with an explicitly unexecuted test gate."""
    return {
        "status": "pass",
        "checks_failed": [],
        "checks": [
            {"id": name, "status": "pass" if name == "review" else "not-applicable", "reason": reason}
            for name in GATE_IDS
        ],
    }


def test_remediation_accepts_documentation_only_test_skip():
    """A documentation-only remediation passes without running tests or retaining a user skip decision.

    Nothing executable changed, so the shared contract skips the full suite and records the exact reason instead; the
    validator must admit that record rather than force a suite run or a fabricated approval.
    """
    result = {"status": "pass", "checks_run": GATE_IDS, "checks_failed": [], "metadata": {}}
    VALIDATOR._reconcile_result_with_gates(result, _gates(DOCS_ONLY_REASON), "code-remediate")


def test_runner_documentation_only_skip_passes_remediation_validation(tmp_path):
    """The real gate CLI's approval-free documentation-only skip yields a record Code Remediate validation accepts.

    The shared contract names `run_gates.py --skip-tests "documentation-only change"` with no approval file; only an
    executed runner proves that flag produces the gate entry the validator admits.
    """
    output = tmp_path / "run"
    completed = subprocess.run(
        [
            sys.executable,
            str(SHARED / "run_gates.py"),
            "--out",
            str(output),
            "--skip-lint",
            DOCS_ONLY_REASON,
            "--skip-format",
            DOCS_ONLY_REASON,
            "--skip-types",
            DOCS_ONLY_REASON,
            "--skip-tests",
            DOCS_ONLY_REASON,
            "--review",
            "exit 0",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    gates = VALIDATOR._validate_gates(output)
    result = {"status": "pass", "checks_run": GATE_IDS, "checks_failed": [], "metadata": {}}
    VALIDATOR._reconcile_result_with_gates(result, gates, "code-remediate")


@pytest.mark.parametrize(
    ("reason", "skill"),
    [
        pytest.param(DOCS_ONLY_REASON, "implement", id="docs-reason-outside-remediation"),
        pytest.param("text-only change", "code-remediate", id="other-reason-in-remediation"),
        pytest.param(DOCS_ONLY_REASON.upper(), "code-remediate", id="inexact-docs-reason"),
    ],
)
def test_documentation_only_exemption_stays_narrow(reason, skill):
    """Only Code Remediate's exact documentation-only reason replaces executed tests or an approved skip.

    Other implementation workflows and any other skip wording keep the original executed-or-approved requirement.
    """
    result = {"status": "pass", "checks_run": GATE_IDS, "checks_failed": [], "metadata": {}}
    with pytest.raises(SystemExit, match="tests-pass-or-approved-skip-required"):
        VALIDATOR._reconcile_result_with_gates(result, _gates(reason), skill)


def test_implementation_cannot_pass_with_only_review():
    """A passing review command cannot substitute for tests or user approval."""
    result = {"status": "pass", "checks_run": GATE_IDS, "checks_failed": [], "metadata": {}}
    with pytest.raises(SystemExit, match="tests-pass-or-approved-skip-required"):
        VALIDATOR._reconcile_result_with_gates(result, _gates())


def test_skill_validator_rejects_unapproved_implementation_skip(tmp_path):
    """A generic review-only candidate cannot pass implementation validation without approval."""
    path = tmp_path / "gates.json"
    path.write_text(json.dumps(_gates()), encoding="utf-8")
    args = argparse.Namespace(gates=path, status="pass", metadata="{}")
    WRITER.validate_gate_evidence(args, GATE_IDS, [])
    with pytest.raises(SystemExit, match="tests-pass-or-approved-skip-required"):
        VALIDATOR._reconcile_result_with_gates(
            {"status": "pass", "checks_run": GATE_IDS, "checks_failed": [], "metadata": {}}, _gates(), "implement"
        )


def test_validator_accepts_recorded_closed_review():
    """A terminal close assessment requires its review evidence but no implementation tests."""
    result = {"status": "pass", "checks_run": GATE_IDS, "checks_failed": [], "metadata": {"review_status": "closed"}}
    VALIDATOR._reconcile_result_with_gates(result, _gates(), "code-review")


def test_closed_metadata_cannot_exempt_implementation_tests():
    """A closed-review label in implementation metadata does not change the owning workflow."""
    result = {"status": "pass", "checks_run": GATE_IDS, "checks_failed": [], "metadata": {"review_status": "closed"}}
    with pytest.raises(SystemExit, match="tests-pass-or-approved-skip-required"):
        VALIDATOR._reconcile_result_with_gates(result, _gates(), "implement")


def test_skip_decision_is_bound_to_reason_and_retained_bytes(tmp_path):
    """An approved skip is admitted only while its retained decision and reason match."""
    approval = load_module("test_approval_binding", SHARED / "gate_test_approval.py")
    path = tmp_path / "checks/tests-skip-approval.json"
    path.parent.mkdir()
    raw = json.dumps({"decision": "skip-tests", "reason": "text-only change", "evidence": "User: skip tests."}).encode()
    path.write_bytes(raw)
    gates = _gates()
    gates["checks"][3]["approval"] = {
        "path": "checks/tests-skip-approval.json",
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    approval.validate_tests(gates, {}, tmp_path)
    path.write_bytes(raw + b" ")
    with pytest.raises(SystemExit, match="test-skip-approval-digest-mismatch"):
        approval.validate_tests(gates, {}, tmp_path)


def test_runner_retains_user_skip_decision(tmp_path):
    """The real gate CLI copies the approval and binds it to the skipped tests entry."""
    decision = tmp_path / "user-decision.json"
    decision.write_text(
        json.dumps({"decision": "skip-tests", "reason": "text-only change", "evidence": "User: skip tests."}),
        encoding="utf-8",
    )
    output = tmp_path / "run"
    completed = subprocess.run(
        [
            sys.executable,
            str(SHARED / "run_gates.py"),
            "--out",
            str(output),
            "--skip-lint",
            "text-only change",
            "--skip-format",
            "text-only change",
            "--skip-types",
            "text-only change",
            "--skip-tests",
            "text-only change",
            "--test-skip-approval",
            str(decision),
            "--review",
            "exit 0",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    gates = VALIDATOR._validate_gates(output)
    assert gates["checks"][3]["approval"]["sha256"] == hashlib.sha256(decision.read_bytes()).hexdigest()
    WRITER.validate_gate_evidence(
        argparse.Namespace(gates=output / "gates.json", status="pass", metadata="{}"), GATE_IDS, []
    )

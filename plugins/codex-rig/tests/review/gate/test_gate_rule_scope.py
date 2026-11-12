"""Match passing-result evidence to each workflow's mandatory executed gate."""

import argparse
import json
from pathlib import Path

import pytest
from _modules import load_module

SHARED = Path(__file__).resolve().parents[3] / "shared"
VALIDATOR = load_module("gate_scope_validator", SHARED / "validate-artifacts.py")
WRITER = load_module("gate_scope_writer", SHARED / "write-result.py")
GATE_IDS = ["lint", "format", "types", "tests", "review"]
REVIEW_SKILLS = [
    "code-review",
    "audit",
    "investigate",
    "challenge-resolve",
    "assess",
    "change-analysis",
    "research",
    "profile",
    "calibrate",
    "release",
    "sync",
    "manage",
]


def _gates(executed):
    """Describe executed gates separately from explicitly inapplicable checks."""
    return {
        "status": "pass",
        "checks_failed": [],
        "checks": [
            {"id": name, "status": "pass" if name in executed else "not-applicable", "reason": "No executable target"}
            for name in GATE_IDS
        ],
    }


def _result():
    """Build the common passing-result fields without a review-specific exemption."""
    return {"status": "pass", "checks_run": GATE_IDS, "checks_failed": [], "metadata": {}}


@pytest.mark.parametrize("skill", REVIEW_SKILLS)
def test_review_evidence_accepts_inapplicable_tests(skill):
    """Source-only workflows need an executed review rather than an approved implementation-test skip."""
    VALIDATOR._reconcile_result_with_gates(_result(), _gates({"review"}), skill)


@pytest.mark.parametrize("skill", REVIEW_SKILLS)
@pytest.mark.parametrize(
    "executed", [pytest.param(set(), id="all-inapplicable"), pytest.param({"tests"}, id="tests-only")]
)
def test_read_only_pass_requires_its_review_gate(skill, executed):
    """Neither an empty gate record nor unrelated tests can certify the mandatory review."""
    with pytest.raises(SystemExit, match=f"{skill}-review-gate-required"):
        VALIDATOR._reconcile_result_with_gates(_result(), _gates(executed), skill)


@pytest.mark.parametrize("skill", ["implement", "code-remediate", "optimize", "kaggle"])
def test_implementation_pass_rejects_all_inapplicable_gates(skill):
    """Every implementation-class result still refuses an unapproved record without executed evidence."""
    with pytest.raises(SystemExit, match="result-pass-without-executed-gates"):
        VALIDATOR._reconcile_result_with_gates(_result(), _gates(set()), skill)


@pytest.mark.parametrize("skill", ["implement", "code-remediate", "optimize", "kaggle"])
def test_implementation_review_does_not_replace_tests(skill):
    """The read-only rule cannot exempt a mutating workflow's unapproved skipped tests."""
    with pytest.raises(SystemExit, match="tests-pass-or-approved-skip-required"):
        VALIDATOR._reconcile_result_with_gates(_result(), _gates({"review"}), skill)


def test_writer_accepts_source_only_review_candidate(tmp_path):
    """The generic writer leaves skill-specific requirements to the authoritative skill validator."""
    path = tmp_path / "gates.json"
    path.write_text(json.dumps(_gates({"review"})), encoding="utf-8")
    WRITER.validate_gate_evidence(argparse.Namespace(gates=path, status="pass", metadata="{}"), GATE_IDS, [])


def test_writer_rejects_all_inapplicable_candidate(tmp_path):
    """Generic result production still refuses a pass with no executed gate."""
    path = tmp_path / "gates.json"
    path.write_text(json.dumps(_gates(set())), encoding="utf-8")
    with pytest.raises(SystemExit, match="result-pass-without-executed-gates"):
        WRITER.validate_gate_evidence(argparse.Namespace(gates=path, status="pass", metadata="{}"), GATE_IDS, [])

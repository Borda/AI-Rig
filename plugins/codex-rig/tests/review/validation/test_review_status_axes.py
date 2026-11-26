"""Contract tests for the separated Code Review status axes and their closed failed-check set.

Every truth-table row shipped in ``shared/review-status-axes.json`` is a fixture: valid rows must pass the pure check
and impossible rows must fail with their recorded code. The writer and both validators are exercised on the axis and
confidence rules they now share, and results saved before the axes existed keep their original reader.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
from _modules import load_module_registered

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
SHARED = PLUGIN_ROOT / "shared"
AXES = load_module_registered("review_status_axes", SHARED / "review_status_axes.py")
TABLE = json.loads((SHARED / "review-status-axes.json").read_text(encoding="utf-8"))
#: Free-form coverage identifiers saved runs used for one condition; current results must use the closed id instead.
LEGACY_COVERAGE_IDS = (
    "specialist-coverage",
    "review-coverage",
    "specialist-review",
    "independent-review-evidence",
    "specialist-independence",
    "OP-SPECIALIST-COVERAGE",
    "multi-role-specialist-coverage",
    "specialist-provenance",
    "reviewer-provenance",
    "independent-specialist-coverage-unavailable",
    "independent-review-unvalidated",
    "specialist-coverage-unvalidated",
)


@pytest.fixture(scope="module")
def writer() -> Iterator[ModuleType]:
    """Load the shared result writer from its shipped path, with its sibling helpers importable.

    The writer imports its sibling modules by bare name, as when it runs as a script from ``shared/``. A worker that
    collected no other shared helper has no ``shared/`` on ``sys.path``, so the fixture adds it for the module's tests.
    """
    with pytest.MonkeyPatch.context() as patch:
        patch.syspath_prepend(str(SHARED))
        yield load_module_registered("codex_rig_write_result_axes", SHARED / "write-result.py")


@pytest.fixture(scope="module")
def review_validator() -> ModuleType:
    """Load the Code Review skill validator from its shipped path."""
    return load_module_registered(
        "code_review_validate_artifacts_axes", PLUGIN_ROOT / "skills" / "code-review" / "validate_artifacts.py"
    )


def _axes(row: dict) -> dict:
    """Build the stored block for one truth-table row."""
    return {"schema_version": 1, **row["axes"]}


def _check(row: dict) -> None:
    """Run the pure axis check for one truth-table row."""
    AXES.check_status_axes(
        _axes(row),
        status=row["status"],
        checks_failed=row["checks_failed"],
        readiness=row["readiness"],
        critical=row["critical"],
        coverage=row.get("coverage", AXES.CoverageState.UNKNOWN.value),
    )


def test_truth_table_is_versioned_and_covers_valid_and_impossible_states() -> None:
    """The shipped table carries its own version family and at least one fixture per side."""
    assert TABLE["schema_version"] == AXES.STATUS_AXES_SCHEMA_VERSION == 1
    assert set(TABLE["check_ids"]) == AXES.CHECK_IDS
    assert set(TABLE["gate_check_ids"]) == AXES.GATE_CHECK_IDS <= AXES.CHECK_IDS
    assert AXES.truth_table() == TABLE["truth_table"]
    assert any(row["valid"] for row in TABLE["truth_table"])
    assert any(not row["valid"] for row in TABLE["truth_table"])
    assert len({row["id"] for row in TABLE["truth_table"]}) == len(TABLE["truth_table"])


@pytest.mark.parametrize("row", [pytest.param(row, id=row["id"]) for row in TABLE["truth_table"] if row["valid"]])
def test_valid_state_passes(row: dict) -> None:
    """Each valid combination of axes, status and named checks is accepted."""
    _check(row)


@pytest.mark.parametrize("row", [pytest.param(row, id=row["id"]) for row in TABLE["truth_table"] if not row["valid"]])
def test_impossible_state_fails_with_its_code(row: dict) -> None:
    """Each impossible combination fails with the code its fixture records."""
    with pytest.raises(SystemExit) as error:
        _check(row)
    assert error.value.code == row["error"]


@pytest.mark.parametrize("legacy", [pytest.param(name, id=name) for name in LEGACY_COVERAGE_IDS])
def test_free_form_coverage_ids_are_replaced_by_one_closed_id(legacy: str) -> None:
    """A failing review names missing independent coverage only with the closed identifier."""
    axes = {"schema_version": 1, "gates_status": "pass", "review_status": "assessed", "review_decision": "REJECT"}
    with pytest.raises(SystemExit) as error:
        AXES.check_status_axes(axes, status="fail", checks_failed=[legacy], readiness="complete", critical=0)
    assert error.value.code == f"status-axes-unknown-check:{legacy}"
    AXES.check_status_axes(
        axes,
        status="fail",
        checks_failed=["independent-review-coverage"],
        readiness="complete",
        critical=0,
        coverage="not-established",
    )


@pytest.mark.parametrize(
    ("independence", "named", "expected"),
    [
        pytest.param(
            {"independence_required": False, "independence_satisfied": False, "fanout_substituted": True},
            ["independent-review-coverage"],
            None,
            id="substituted-named",
        ),
        pytest.param(
            {"independence_required": True, "independence_satisfied": False, "fanout_substituted": False},
            ["independent-review-coverage"],
            None,
            id="required-unmet-named",
        ),
        pytest.param(
            {"independence_required": False, "independence_satisfied": True, "fanout_substituted": False},
            ["independent-review-coverage"],
            None,
            id="satisfied-roles-named-left-to-the-review-validator",
        ),
        pytest.param({}, ["independent-review-coverage"], "status-axes-coverage-evidence-missing", id="no-evidence"),
        pytest.param(
            {"independence_required": True, "independence_satisfied": False, "fanout_substituted": False},
            ["critical-findings"],
            "status-axes-coverage-unnamed",
            id="required-unmet-unnamed",
        ),
    ],
)
def test_named_coverage_check_is_cross_checked_against_result_metadata(
    independence: dict, named: list[str], expected: str | None
) -> None:
    """A named coverage check needs coverage evidence, and a failing review must name an unmet explicit requirement."""
    axes = AXES.build_status_axes("pass", {"review_decision": {"recommendation": "MAJOR_REVISION"}})
    result = _result("fail", named, axes)
    result["metadata"].update(independence)
    if "critical-findings" in named:
        result["findings"]["critical"] = 1
    if expected is None:
        AXES.check_result_status_axes(result, "pass")
        return
    with pytest.raises(SystemExit) as error:
        AXES.check_result_status_axes(result, "pass")
    assert error.value.code == expected


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        pytest.param(
            {"review_decision": {"recommendation": "MAJOR_REVISION"}},
            ("assessed", "MAJOR_REVISION"),
            id="assessed",
        ),
        pytest.param({"review_status": "unavailable"}, ("unavailable", None), id="unavailable"),
        pytest.param(
            {"review_status": "closed", "review_decision": {"recommendation": "APPROVE"}},
            ("closed", None),
            id="closed-ignores-stale-decision",
        ),
    ],
)
def test_axes_are_derived_from_metadata(metadata: dict, expected: tuple) -> None:
    """The writer derives the review and decision axes instead of trusting an authored copy."""
    axes = AXES.build_status_axes("timeout", metadata)
    assert axes == {
        "schema_version": 1,
        "gates_status": "timeout",
        "review_status": expected[0],
        "review_decision": expected[1],
    }


def _result(status: str, checks_failed: list[str], axes: dict | None) -> dict:
    """Return a minimal assessed result payload for axis reconciliation."""
    metadata: dict = {
        "review_decision": {"recommendation": "MAJOR_REVISION"},
        "review_validation": {"verification_readiness": "complete"},
    }
    if axes is not None:
        metadata["status_axes"] = axes
    return {
        "status": status,
        "checks_failed": checks_failed,
        "findings": {"critical": 0, "high": 1, "medium": 0, "low": 0},
        "metadata": metadata,
    }


def test_stored_axes_must_match_gate_evidence() -> None:
    """A stored gate axis that disagrees with gates.json is rejected even when the status is self-consistent."""
    axes = AXES.build_status_axes("pass", {"review_decision": {"recommendation": "MAJOR_REVISION"}})
    AXES.check_result_status_axes(_result("pass", [], axes), "pass")
    with pytest.raises(SystemExit) as error:
        AXES.check_result_status_axes(_result("pass", [], axes), "fail")
    assert error.value.code == "status-axes-evidence-mismatch"


def test_skill_validator_reads_axes_only_when_present(review_validator: ModuleType, tmp_path: Path) -> None:
    """Saved results without axes keep their reader; a current result must agree with its gate evidence."""
    (tmp_path / "gates.json").write_text(json.dumps({"status": "pass"}), encoding="utf-8")
    review_validator._validate_status_axes(tmp_path, _result("fail", [], None), _result("fail", [], None)["metadata"])
    axes = AXES.build_status_axes("pass", {"review_decision": {"recommendation": "MAJOR_REVISION"}})
    current = _result("fail", [], axes)
    with pytest.raises(SystemExit) as error:
        review_validator._validate_status_axes(tmp_path, current, current["metadata"])
    assert error.value.code == "status-axes-fail-without-named-check"


@pytest.mark.parametrize("review_status", [None, "closed", "unavailable"])
@pytest.mark.parametrize("location", ["result.candidate.json", "other.json", "external/result.json", "result.json"])
def test_only_the_promoted_review_may_omit_axes(
    review_validator: ModuleType, tmp_path: Path, review_status: str | None, location: str
) -> None:
    """Both validator entry points require axes for every candidate shape and preserve historical results."""
    shared = load_module_registered("validate_shared_review_axes", SHARED / "validate-artifacts.py")
    result = _result("fail", [], None)
    result.update(confidence=0.95, checks_run=[], artifact_path="result.json")
    result["metadata"].update(scope="working-tree", risk_tier="LOCAL", review_status=review_status)
    path = tmp_path / location
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(result), encoding="utf-8")
    review = review_validator.collect_result_errors(tmp_path, path, tmp_path, "parent", tmp_path)
    shared_report = shared.collect_errors("code-review", tmp_path, path)
    required = location != "result.json"
    assert ("status-axes-required" in {error["code"] for error in review["errors"]}) == required
    assert ("status-axes-required" in {error["code"] for error in shared_report["errors"]}) == required


@pytest.mark.parametrize(
    ("named", "failed", "refused", "error"),
    [
        pytest.param(["lint", "tests"], ["lint"], [], "status-axes-gate-failures-mismatch", id="spurious-executed"),
        pytest.param(["lint"], ["lint", "tests"], [], "status-axes-gate-failures-mismatch", id="omitted-executed"),
        pytest.param(["source-refused"], ["lint"], [], "status-axes-gate-failures-mismatch", id="swapped-cause"),
        pytest.param(["source-refused"], [], [], "status-axes-source-refusal-mismatch", id="spurious-refusal"),
        pytest.param(["lint"], ["lint"], ["tests"], "status-axes-source-refusal-mismatch", id="omitted-refusal"),
        pytest.param(["lint", "source-refused"], ["lint"], ["tests"], "status-axes-refusal-with-executed", id="mixed"),
    ],
)
def test_review_gate_causes_agree_with_receipt(
    review_validator: ModuleType,
    tmp_path: Path,
    named: list[str],
    failed: list[str],
    refused: list[str],
    error: str,
) -> None:
    """The skill validator rejects added, missing, swapped or incompatible reasons for failed gates."""
    gates = {"status": "fail", "checks_failed": failed, "checks_refused": refused}
    (tmp_path / "gates.json").write_text(json.dumps(gates), encoding="utf-8")
    axes = AXES.build_status_axes("fail", {"review_decision": {"recommendation": "MAJOR_REVISION"}})
    result = _result("fail", named, axes)
    with pytest.raises(SystemExit, match=error):
        review_validator._validate_status_axes(tmp_path, result, result["metadata"])


@pytest.mark.parametrize(
    ("status", "named", "failed", "refused"),
    [
        pytest.param("pass", [], [], [], id="passing-gates"),
        pytest.param("fail", ["tests", "lint"], ["lint", "tests"], [], id="executed-order-independent"),
        pytest.param("timeout", ["tests"], ["tests"], [], id="executed-timeout"),
        pytest.param("fail", ["source-refused"], [], ["lint", "tests"], id="source-refusal"),
    ],
)
def test_gate_receipt_accepts_its_exact_causes(
    status: str, named: list[str], failed: list[str], refused: list[str]
) -> None:
    """Executed failure IDs are compared as a set and source refusal has its own receipt evidence."""
    gates = {"status": status, "checks_failed": failed, "checks_refused": refused}
    axes = AXES.build_status_axes(status, {"review_decision": {"recommendation": "MAJOR_REVISION"}})
    AXES.check_result_status_axes(_result(status, named, axes), gates)


@pytest.mark.parametrize("review_status", ["unavailable", "closed"])
def test_terminal_review_cannot_name_absent_critical_findings(review_status: str) -> None:
    """A terminal review still needs a positive finding count to name critical findings."""
    metadata = {"review_status": review_status}
    result = _result("fail", ["critical-findings"], AXES.build_status_axes("pass", metadata))
    result["metadata"].update(metadata)
    with pytest.raises(SystemExit, match="status-axes-critical-findings-absent"):
        AXES.check_result_status_axes(result, "pass")


def _recovery(confidence: float, band: str) -> dict:
    """Return confidence metadata for one score and band."""
    return {
        "confidence_recovery": {
            "initial_confidence": confidence,
            "final_confidence": confidence,
            "status": band,
            "evidence": ["evidence"],
            "recovery_actions": ["action"],
            "remaining_limits": ["limit"],
        }
    }


@pytest.mark.parametrize(
    ("confidence", "band"),
    [pytest.param(0.7, "not-acceptable-failed", id="not-acceptable"), pytest.param(0.82, "very-questionable", id="vq")],
)
def test_confidence_alone_never_fails_a_current_review(writer: ModuleType, confidence: float, band: str) -> None:
    """The writer keeps the band label but drops the forced failure when the status axes name the causes."""
    metadata = _recovery(confidence, band)
    writer.validate_confidence_recovery(
        metadata, confidence, writer.ResultStatus.PASS, [], confidence_gates_status=False
    )
    with pytest.raises(SystemExit):
        writer.validate_confidence_recovery(metadata, confidence, writer.ResultStatus.PASS, [])
    with pytest.raises(SystemExit) as error:
        writer.validate_confidence_recovery(
            _recovery(confidence, "fair"), confidence, writer.ResultStatus.PASS, [], confidence_gates_status=False
        )
    assert str(error.value.code).startswith("confidence-status-should")


def test_skill_validator_confidence_gate_depends_on_axes(review_validator: ModuleType) -> None:
    """A saved result without axes keeps the historical confidence gate; a current one does not."""
    saved = {"status": "pass", "checks_failed": [], "confidence": 0.7}
    metadata = _recovery(0.7, "not-acceptable-failed")
    with pytest.raises(SystemExit) as error:
        review_validator._validate_confidence_recovery(saved, metadata)
    assert error.value.code == "review-pass-confidence-not-acceptable"
    current = {**metadata, "status_axes": AXES.build_status_axes("pass", {"review_decision": {}})}
    review_validator._validate_confidence_recovery(saved, current)

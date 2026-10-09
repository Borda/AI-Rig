"""Pin how the behavioral scorer treats negative-control cases that expect no findings.

A case whose expected finding list is empty is answered correctly by reporting nothing. The scorer used to compute F1
0.0 for that correct answer because precision and recall are undefined at zero counts, so every clean case added its
whole confidence to ``mean_overconfidence`` and adding more clean cases pushed the gate toward failure.

The opposite failure must still fail the gate: a reporter that flags every negative control passed precision and
overconfidence, because dozens of positive cases dilute both means. Negative-control specificity is gated on its own.
"""

from __future__ import annotations

import importlib.util
import json
import math
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CALIBRATION_DIR = PLUGIN_ROOT / "runtime" / "calibration"
CASES_PATH = CALIBRATION_DIR / "behavioral-cases.json"
OBSERVATIONS_PATH = CALIBRATION_DIR / "behavioral-observations.jsonl"
#: Finding ID no shipped case expects, injected into negative-control observations as a false alarm.
SPURIOUS_FINDING = "spurious-negative-control-finding"


@pytest.fixture
def scorer(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Load the shipped behavioral scorer beside its sibling ``live_contract`` module."""
    monkeypatch.syspath_prepend(str(CALIBRATION_DIR))
    spec = importlib.util.spec_from_file_location(
        "codex_rig_behavioral_scoring_negative_controls", CALIBRATION_DIR / "score_behavioral.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.mark.installed_plugin
@pytest.mark.parametrize(
    ("expected", "reported", "f1"),
    [
        pytest.param(set(), set(), 1.0, id="negative-control-answered-clean"),
        pytest.param(set(), {"spurious"}, 0.0, id="negative-control-false-positive"),
        pytest.param({"a"}, set(), 0.0, id="missed-finding"),
        pytest.param({"a", "b"}, {"a", "b"}, 1.0, id="exact-match"),
    ],
)
def test_case_quality_scores_a_clean_negative_control_as_correct(
    scorer: ModuleType, expected: set[str], reported: set[str], f1: float
) -> None:
    """A case expecting no findings scores 1.0 only when nothing is reported.

    A spurious report on the same case still scores 0.0, so the rule cannot hide false positives.
    """
    assert scorer._case_quality(expected, reported)["f1"] == f1


@pytest.mark.installed_plugin
def test_shipped_negative_controls_add_no_overconfidence(scorer: ModuleType) -> None:
    """Every correctly answered shipped negative-control row carries zero overconfidence and F1 1.0.

    The shipped observation set holds dozens of clean negative controls; scoring them as failures had lifted
    ``mean_overconfidence`` to within a few cases of its threshold.
    """
    result = scorer._score(
        CALIBRATION_DIR / "behavioral-cases.json",
        CALIBRATION_DIR / "behavioral-observations.jsonl",
        CALIBRATION_DIR / "live-route-policy.json",
        CALIBRATION_DIR / "live-ab-tasks.json",
        PLUGIN_ROOT,
        layout="plugin",
    )

    clean_rows = [row for row in result["case_results"] if (row["tp"], row["fp"], row["fn"]) == (0, 0, 0)]
    assert clean_rows
    assert {(row["f1"], row["overconfidence"]) for row in clean_rows} == {(1.0, 0.0)}


def _score_observations(scorer: ModuleType, observations: Path) -> dict:
    """Score one observation file against the shipped cases, thresholds, and route contracts."""
    return scorer._score(
        CASES_PATH,
        observations,
        CALIBRATION_DIR / "live-route-policy.json",
        CALIBRATION_DIR / "live-ab-tasks.json",
        PLUGIN_ROOT,
        layout="plugin",
    )


@pytest.fixture
def spurious_negatives(tmp_path: Path) -> Callable[[float], Path]:
    """Return a factory writing the shipped observations with a share of negative controls reporting a false alarm.

    The share is applied to the current negative-control count, so the fixture keeps its meaning when cases are added.
    Each altered observation keeps its original confidence, as an overconfident false alarm would.
    """
    cases = {case["id"]: case for case in json.loads(CASES_PATH.read_text(encoding="utf-8"))["cases"]}
    records = [json.loads(line) for line in OBSERVATIONS_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    negative_rows = [index for index, record in enumerate(records) if not cases[record["case_id"]]["expected_findings"]]

    def write(share: float) -> Path:
        altered = set(negative_rows[: math.floor(len(negative_rows) * share)])
        rows = [
            {**record, "reported_findings": [SPURIOUS_FINDING]} if index in altered else record
            for index, record in enumerate(records)
        ]
        path = tmp_path / f"observations-{share}.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8", newline="\n")
        return path

    return write


@pytest.mark.installed_plugin
@pytest.mark.parametrize("share", [pytest.param(0.2, id="one-in-five"), pytest.param(1.0, id="every-negative")])
def test_spurious_findings_on_negative_controls_fail_the_gate(
    scorer: ModuleType, spurious_negatives: Callable[[float], Path], share: float
) -> None:
    """False alarms on clean cases fail calibration through the negative-control specificity gate.

    With every shipped negative control flagged at its original high confidence, aggregate precision stayed near 0.94
    and mean overconfidence just under 0.15, so the five original gates passed a reporter that never answers clean.
    """
    result = _score_observations(scorer, spurious_negatives(share))

    assert result["status"] == "fail"
    assert "behavioral-negative-specificity" in result["checks_failed"]


@pytest.mark.installed_plugin
def test_isolated_negative_control_false_alarms_stay_within_tolerance(
    scorer: ModuleType, spurious_negatives: Callable[[float], Path]
) -> None:
    """A few false alarms below the specificity threshold's tolerance leave the shipped gate passing.

    The threshold bounds the false-alarm rate; it does not demand a perfect clean record on every negative control.
    """
    result = _score_observations(scorer, spurious_negatives(0.05))

    assert (result["status"], result["checks_failed"]) == ("pass", [])


@pytest.mark.installed_plugin
@pytest.mark.parametrize(
    ("specificity", "failures"),
    [
        pytest.param(0.9, [], id="at-threshold-passes"),
        pytest.param(0.89, ["behavioral-negative-specificity"], id="below-threshold-fails"),
    ],
)
def test_negative_specificity_threshold_boundary(scorer: ModuleType, specificity: float, failures: list[str]) -> None:
    """The specificity gate fails strictly below its threshold, like the other minimum gates."""
    overall = {
        "observations": 100,
        "recall": 1.0,
        "precision": 1.0,
        "confidence_mae": 0.0,
        "mean_overconfidence": 0.0,
        "negative_specificity": specificity,
    }
    thresholds = scorer._load_cases(CASES_PATH)[1]

    assert scorer._threshold_failures(overall, {**thresholds, "min_negative_specificity": 0.9}) == failures


#: Observation source standing in for a new partial campaign; neither fixture nor live, so no live metadata applies.
PARTIAL_SOURCE = "partial-campaign"


@pytest.fixture
def partial_source_false_alarms(tmp_path: Path) -> Callable[[int], Path]:
    """Return a factory writing the shipped observations plus N new-source negative controls, each a false alarm.

    Each added row answers one shipped negative-control case with a spurious finding at 0.9 confidence, as a new
    campaign whose reporter never answers clean would.
    """
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))["cases"]
    negatives = [case for case in cases if not case["expected_findings"]]
    shipped = OBSERVATIONS_PATH.read_text(encoding="utf-8")

    def write(count: int) -> Path:
        rows = [
            {
                "case_id": case["id"],
                "target": case["target"],
                "source": PARTIAL_SOURCE,
                "run_id": "partial-campaign-run",
                "observed_at": "2026-10-08T00:00:00Z",
                "reported_findings": [SPURIOUS_FINDING],
                "confidence": 0.9,
            }
            for case in negatives[:count]
        ]
        path = tmp_path / f"observations-partial-{count}.jsonl"
        path.write_text(shipped + "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8", newline="\n")
        return path

    return write


@pytest.mark.installed_plugin
@pytest.mark.parametrize("count", [5, 6, 7])
def test_source_false_alarms_fail_despite_clean_fixture_negatives(
    scorer: ModuleType, partial_source_false_alarms: Callable[[int], Path], count: int
) -> None:
    """A new source that flags every clean case fails the gate even while the aggregate ratio stays high.

    With five or six such rows beside the shipped fixture negatives, aggregate specificity stayed above 0.9 and the
    whole calibration passed at 0% specificity for the new source; the per-source gate closes that dilution.
    """
    result = _score_observations(scorer, partial_source_false_alarms(count))

    assert result["by_source"][PARTIAL_SOURCE]["negative_specificity"] == 0.0
    assert result["status"] == "fail"
    assert "behavioral-source-negative-specificity" in result["checks_failed"]
    assert result["negative_specificity_failing_sources"] == [PARTIAL_SOURCE]


@pytest.mark.installed_plugin
def test_shipped_observations_pass_every_source_specificity_gate(scorer: ModuleType) -> None:
    """The shipped ledger alone passes, so the per-source gate fails only on a source's own false alarms."""
    result = _score_observations(scorer, OBSERVATIONS_PATH)

    assert (result["status"], result["negative_specificity_failing_sources"]) == ("pass", [])


@pytest.mark.installed_plugin
def test_shipped_cases_gate_negative_specificity() -> None:
    """The shipped case set declares the negative-control specificity threshold at 0.9 or stricter."""
    thresholds = json.loads(CASES_PATH.read_text(encoding="utf-8"))["thresholds"]

    assert thresholds["min_negative_specificity"] >= 0.9

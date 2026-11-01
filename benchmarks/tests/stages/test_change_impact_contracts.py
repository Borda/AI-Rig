"""Contracts for controlled read-only change-impact prediction tasks."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from _bench_common import change_impact_contracts as contracts

ROOT = Path(__file__).resolve().parents[3]
FIXTURE_ROOT = ROOT / "benchmarks/fixtures/change-impact/v1/repo"
SUITE_PATH = ROOT / "benchmarks/suites/tasks-change-impact.json"
_LOCATION_FIELDS = ("must_update_callsites", "compatible_callsites", "must_update_tests", "compatible_tests")


def _answer(oracle: Any) -> dict[str, Any]:
    """Copy an oracle answer into the response shape required by the public scorer."""
    return {
        field: dict(value) if isinstance(value, Mapping) else list(value) for field, value in oracle.expected.items()
    }


def _envelope(answer: dict[str, Any]) -> str:
    """Wrap one JSON answer in the strict response markers."""
    return f"BEGIN_CHANGE_IMPACT_JSON\n{json.dumps(answer, sort_keys=True)}\nEND_CHANGE_IMPACT_JSON"


@pytest.fixture(name="tasks")
def _tasks() -> list[dict[str, Any]]:
    """Load the committed controlled suite through its public validation API."""
    return contracts.load_change_impact_tasks(SUITE_PATH)


@pytest.fixture(name="oracles")
def _oracles(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    """Build all source-derived oracles and verify their hidden behavioral labels."""
    built = {task["id"]: contracts.build_change_impact_oracle(task, FIXTURE_ROOT) for task in tasks}
    for task in tasks:
        contracts.verify_change_impact_oracle(task, FIXTURE_ROOT, built[task["id"]])
    return built


def test_fixture_suite_has_five_unlabelled_prediction_tasks(tasks: list[dict[str, Any]]) -> None:
    """The suite is a five-task prospective input without embedded oracle answers.

    Tasks CI-01..CI-05 load through the public validator; none carries an ``expected`` key and every rendered prompt
    names the strict answer envelope, so the model is asked to predict rather than copy.
    """
    assert [task["id"] for task in tasks] == ["CI-01", "CI-02", "CI-03", "CI-04", "CI-05"]
    assert all("expected" not in task for task in tasks)
    assert all("BEGIN_CHANGE_IMPACT_JSON" in contracts.materialize_change_impact_prompt(task) for task in tasks)


@pytest.mark.parametrize(
    ("task_id", "expected_reason"),
    [
        pytest.param("CI-01", "positional-after-keyword-only", id="positional-after-keyword-only-first"),
        pytest.param("CI-02", "renamed-keyword", id="renamed-keyword-first"),
        pytest.param("CI-03", "positional-after-keyword-only", id="positional-after-keyword-only-second"),
        pytest.param("CI-04", "missing-required-argument", id="missing-required-argument"),
        pytest.param("CI-05", "renamed-keyword", id="renamed-keyword-second"),
    ],
)
def test_oracle_has_four_balanced_sets_and_distinct_reason_codes(
    oracles: dict[str, Any], task_id: str, expected_reason: str
) -> None:
    """Each task has four two-element location sets and its own distinct compatibility reason code.

    Balanced sets mean both a false positive and a false negative cost credit; the reason code per task confirms the
    oracle derived each breaking change from its fixture source rather than reusing one label.
    """
    oracle = oracles[task_id]

    assert {field: len(oracle.expected[field]) for field in _LOCATION_FIELDS} == dict.fromkeys(_LOCATION_FIELDS, 2)
    assert expected_reason in oracle.expected["reasons"].values()


def test_aliases_are_credited_and_same_name_decoy_is_excluded(oracles: dict[str, Any]) -> None:
    """Qualified import resolution credits aliased calls and excludes a local same-name function.

    CI-01 calls the target through an alias and also defines an unrelated ``local_preview``: the alias call is a must-
    update site and the decoy appears in no expected set.
    """
    quota = oracles["CI-01"].expected
    assert any("ledger_snapshot" in location for location in quota["must_update_callsites"])
    assert not any(
        "local_preview" in location for field, values in quota.items() if field != "reasons" for location in values
    )


def test_perfect_response_is_pooling_eligible_and_scores_fully(oracles: dict[str, Any]) -> None:
    """A complete strict envelope is pooling-eligible and scores fully.

    Echoing the oracle's own answer for CI-04 through the strict envelope must validate, qualify for pooling and earn
    full credit, with the evidence-only metrics left unset.
    """
    oracle = oracles["CI-04"]
    assessment = contracts.assess_change_impact_response({"id": "CI-04"}, _envelope(_answer(oracle)))
    score = contracts.score_change_impact_answer(oracle, assessment.answer)

    assert assessment.valid is True
    assert assessment.strict_envelope_valid is True
    assert assessment.pooling_eligible is True
    assert score.scored is True
    assert score.correct is True
    assert score.quality_score == 1.0
    assert score.graded_score == 1.0
    assert score.erec is None
    assert score.rrec is None
    assert score.deff is None


@pytest.mark.parametrize("field", ["must_update_callsites", "must_update_tests"])
def test_all_affected_response_loses_compatible_f1(oracles: dict[str, Any], field: str) -> None:
    """Declaring every candidate affected zeroes the compatible-set credit.

    Moving all compatible locations into the must-update list keeps recall perfect on the affected set but leaves the
    compatible set empty, so the answer cannot reach full quality by over-predicting.
    """
    oracle = oracles["CI-01"]
    answer = _answer(oracle)
    compatible_field = field.replace("must_update", "compatible")
    answer[field].extend(answer[compatible_field])
    answer[compatible_field] = []
    score = contracts.score_change_impact_answer(oracle, answer)

    assert score.components[compatible_field] == 0.0
    assert score.quality_score < 1.0


@pytest.mark.parametrize("field", ["must_update_callsites", "must_update_tests"])
def test_all_unaffected_response_loses_affected_f1(oracles: dict[str, Any], field: str) -> None:
    """Declaring every candidate compatible zeroes the affected-set credit.

    Moving all must-update locations into the compatible list leaves the affected set empty, so the answer cannot reach
    full quality by under-predicting.
    """
    oracle = oracles["CI-03"]
    answer = _answer(oracle)
    compatible_field = field.replace("must_update", "compatible")
    answer[compatible_field].extend(answer[field])
    answer[field] = []
    score = contracts.score_change_impact_answer(oracle, answer)

    assert score.components[field] == 0.0
    assert score.quality_score < 1.0


def test_reason_error_has_distinct_diagnostic_loss(oracles: dict[str, Any]) -> None:
    """A single wrong compatibility reason costs reason credit and is reported as its own failure.

    CI-05 with correct location sets but one compatible call labelled with another task's reason code keeps the set
    components intact while the reasons component drops and a ``wrong_reasons`` detail appears.
    """
    oracle = oracles["CI-05"]
    answer = _answer(oracle)
    location = answer["compatible_callsites"][0]
    answer["reasons"][location] = "renamed-keyword"
    score = contracts.score_change_impact_answer(oracle, answer)
    details = contracts.change_impact_failure_details(oracle, answer)

    assert score.components["reasons"] < 1.0
    assert any(detail["category"] == "wrong_reasons" for detail in details)


def test_invalid_envelopes_and_unknown_reasons_receive_zero_credit(oracles: dict[str, Any]) -> None:
    """Malformed envelopes and unknown reason codes are invalid, answerless and score zero.

    A bare ``{}`` and an otherwise perfect CI-02 answer using the reason ``unknown`` are both rejected, so neither is
    promoted into a partially scored diagnostic answer.
    """
    oracle = oracles["CI-02"]
    malformed = contracts.assess_change_impact_response({"id": "CI-02"}, "{}")
    answer = _answer(oracle)
    answer["reasons"][answer["must_update_callsites"][0]] = "unknown"
    invalid_reason = contracts.assess_change_impact_response({"id": "CI-02"}, _envelope(answer))

    assert malformed.answer is None
    assert malformed.valid is False
    assert invalid_reason.answer is None
    assert invalid_reason.valid is False
    assert contracts.score_change_impact_answer(oracle, malformed.answer).quality_score == 0.0


def test_failure_details_report_missing_and_unexpected_facts(oracles: dict[str, Any]) -> None:
    """Failure details report both an omitted location and a hallucinated one.

    Replacing one real CI-01 must-update call with the local decoy ``local_preview@999`` yields a missing fact and an
    unexpected fact, so a grader can tell omission from invention.
    """
    oracle = oracles["CI-01"]
    answer = _answer(oracle)
    removed = answer["must_update_callsites"][1]
    replacement = "app.quota_calls::local_preview@999"
    answer["must_update_callsites"] = [*answer["must_update_callsites"][:1], replacement]
    answer["reasons"].pop(removed)
    answer["reasons"][replacement] = "positional-after-keyword-only"
    details = contracts.change_impact_failure_details(oracle, answer)

    categories = {detail["category"] for detail in details}
    assert {"missing_facts", "unexpected_facts"} <= categories


def test_source_fingerprint_detects_source_drift(tmp_path: Path, tasks: list[dict[str, Any]]) -> None:
    """A locked fixture identity fails after any Python source byte changes.

    A private copy of the fixture repository is fingerprinted, then one module gains a comment line: both the direct
    fingerprint check and oracle verification must raise a drift error.
    """
    copy_root = tmp_path / "repo"
    for source in FIXTURE_ROOT.rglob("*.py"):
        destination = copy_root / source.relative_to(FIXTURE_ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
    expected = contracts.source_fingerprint(copy_root)
    task = tasks[0]
    oracle = contracts.build_change_impact_oracle(task, copy_root)
    target = copy_root / "impactlib/quota.py"
    target.write_text(f"{target.read_text(encoding='utf-8')}\n# drift\n", encoding="utf-8")

    with pytest.raises(ValueError, match="source fingerprint drift"):
        contracts.verify_source_fingerprint(copy_root, expected)
    with pytest.raises(ValueError, match="source fingerprint drift"):
        contracts.verify_change_impact_oracle(task, copy_root, oracle)

"""Verify scoped interaction claims against authenticated review evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from test_loop_review_evidence import (
    _loop_evidence_run,
    _read_ledger,
    _rewrite_native_outputs,
    _validator,
    _write_ledger,
)


def _write_json(path: Path, value: object) -> None:
    """Write one fixture artifact with portable newline behavior."""
    path.write_text(json.dumps(value), encoding="utf-8", newline="\n")


def _write_authenticated_verdict(fixture: dict[str, Any], verdicts: list[dict[str, Any]]) -> None:
    """Rebind the actual child and parent messages to a synthetic reviewer verdict."""
    review = fixture["run"]
    attempt = fixture["manifest"]["passes"][0]["attempts"][0]
    output = review / attempt["output_path"]
    message = output.read_text(encoding="utf-8").rstrip("\n")
    prefix, block = message.split("```adversarial-loop\n", 1)
    body, suffix = block.split("\n```", 1)
    report = json.loads(body)
    report["interactions"] = verdicts
    message = prefix + "```adversarial-loop\n" + json.dumps(report, sort_keys=True) + "\n```" + suffix
    output.write_text(message + "\n", encoding="utf-8", newline="\n")
    attempt["output_sha256"] = hashlib.sha256(output.read_bytes()).hexdigest()
    _write_json(review / "specialist-manifest.json", fixture["manifest"])

    sessions = fixture["sessions"] / "sessions"
    child_path = sessions / f"rollout-{attempt['agent_thread_id']}.jsonl"
    child = [json.loads(line) for line in child_path.read_text(encoding="utf-8").splitlines()]
    child[-1]["payload"]["last_agent_message"] = message
    child_path.write_text("".join(json.dumps(row) + "\n" for row in child), encoding="utf-8", newline="\n")

    parent_path = sessions / "rollout-parent-thread.jsonl"
    parent = [json.loads(line) for line in parent_path.read_text(encoding="utf-8").splitlines()]
    joined = False
    for row in parent:
        payload = row.get("payload", {})
        if payload.get("type") == "agent_message" and payload.get("author") == attempt["agent_path"]:
            payload["content"][0]["text"] = f"Message Type: FINAL_ANSWER\nPayload:\n{message}"
            joined = True
    assert joined
    parent_path.write_text("".join(json.dumps(row) + "\n" for row in parent), encoding="utf-8", newline="\n")
    (review.parent / "review-1.md").write_bytes(output.read_bytes())


def _scoped_fixture(
    tmp_path: Path,
    *,
    include_consumer: bool = True,
    signed_request: bool = True,
    requirements: list[dict[str, Any]] | None = None,
    findings: list[dict[str, object]] | None = None,
) -> dict[str, Any]:
    """Build real native artifacts for one producer-to-consumer interaction."""
    fixture = _loop_evidence_run(tmp_path, current_contract=True, findings=findings)
    run, repository, validator, native = (fixture[key] for key in ("run", "repository", "validator", "fixture"))
    (repository / "consumer.py").write_text("from widget import VALUE\n", encoding="utf-8", newline="\n")
    if requirements is None:
        requirements = [
            {
                "id": "widget-consumer",
                "source_paths": ["consumer.py", "widget.py"],
                "claim": "The consumer reads the produced value.",
            }
        ]
    evidence = json.loads((run / "loop-evidence.json").read_bytes())
    evidence["schema_version"] = 3
    evidence["interaction_requirements"] = requirements
    request = dict(evidence["request"])
    request["specification"] += "\nInteraction assessment:\n" + json.dumps(
        requirements, sort_keys=True, ensure_ascii=True
    )
    evidence["request"] = request
    supporting_bytes = None
    if include_consumer:
        supporting = validator.capture_source_snapshot(repository, ["consumer.py"])
        supporting_bytes = validator._canonical_source_bytes(supporting)
        for name in ("current-supporting.json", "supporting-1.json"):
            (run / name).write_bytes(supporting_bytes)
        evidence["supporting_paths"] = ["consumer.py"]
        evidence["current_supporting_source_path"] = "current-supporting.json"
        evidence["rounds"][0]["supporting_source_path"] = "supporting-1.json"
    _write_json(run / "loop-evidence.json", evidence)
    _rewrite_native_outputs(
        native,
        (run / "source-1.json").read_bytes(),
        (run / "round-1.diff").read_bytes(),
        findings or [],
        full=True,
        request=request if signed_request else {**request, "specification": "The interaction was not assigned."},
        supporting_bytes=supporting_bytes,
        coverage_paths=["consumer.py", "widget.py"] if include_consumer else ["widget.py"],
    )
    verdicts = [
        {"id": requirement["id"], "verdict": "reviewed", "evidence": ["The producer and consumer use the same value."]}
        for requirement in requirements
    ]
    for requirement, verdict in zip(requirements, verdicts, strict=True):
        if requirement["id"] == "interaction-inventory-audit":
            verdict["unit_verdicts"] = [{"id": unit, "verdict": "reviewed"} for unit in requirement["unit_ids"]]
    _write_authenticated_verdict(native, verdicts)
    return fixture


def test_scoped_interaction_accepts_authenticated_assessment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Accept a clean interaction only after exact endpoints and reviewer verdict are bound."""
    monkeypatch.setenv("CODEX_THREAD_ID", "parent-thread")
    fixture = _scoped_fixture(tmp_path)

    fixture["validator"].validate_loop_evidence(fixture["run"], fixture["codex_home"])


@pytest.mark.parametrize(
    "verdicts",
    [
        pytest.param([], id="missing-verdict"),
        pytest.param([{"id": "other", "verdict": "reviewed", "evidence": ["Observed."]}], id="unassigned-id"),
        pytest.param(
            [
                {"id": "widget-consumer", "verdict": "reviewed", "evidence": ["Observed."]},
                {"id": "widget-consumer", "verdict": "reviewed", "evidence": ["Observed."]},
            ],
            id="duplicate-id",
        ),
        pytest.param(
            [{"id": "widget-consumer", "verdict": "unresolved", "evidence": ["Unknown consumer."]}],
            id="unresolved-cannot-be-clean",
        ),
        pytest.param(
            [{"id": "widget-consumer", "verdict": "unknown", "evidence": ["Unknown consumer."]}], id="unknown-verdict"
        ),
        pytest.param([{"id": "widget-consumer", "verdict": "reviewed", "evidence": []}], id="empty-evidence"),
    ],
)
def test_scoped_interaction_rejects_invalid_authenticated_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verdicts: list[dict[str, Any]]
) -> None:
    """A clean loop cannot replace, omit, duplicate, or defer its assigned interaction."""
    monkeypatch.setenv("CODEX_THREAD_ID", "parent-thread")
    fixture = _scoped_fixture(tmp_path)
    validator, run, codex_home = (fixture[key] for key in ("validator", "run", "codex_home"))
    validator.validate_loop_evidence(run, codex_home)
    _write_authenticated_verdict(fixture["fixture"], verdicts)

    with pytest.raises(ValueError, match="interaction"):
        validator.validate_loop_evidence(run, codex_home)


@pytest.mark.parametrize(
    ("damage", "error"),
    [
        pytest.param("unsigned-request", "interaction|review-request", id="unsigned-request"),
        pytest.param("stale-consumer", "current-supporting-mismatch", id="stale-consumer"),
        pytest.param("forged-reviewer", "reviewer-identity-mismatch", id="forged-reviewer"),
        pytest.param("tampered-output", "report-bytes-mismatch", id="tampered-output"),
    ],
)
def test_scoped_interaction_rejects_unbound_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str, error: str
) -> None:
    """The verdict requires delivered, current endpoints and an authentic reviewer."""
    monkeypatch.setenv("CODEX_THREAD_ID", "parent-thread")
    fixture = _scoped_fixture(tmp_path)
    validator, run, codex_home = (fixture[key] for key in ("validator", "run", "codex_home"))
    validator.validate_loop_evidence(run, codex_home)
    if damage == "unsigned-request":
        evidence = json.loads((run / "loop-evidence.json").read_bytes())
        evidence["request"]["specification"] = "The interaction was not assigned."
        _write_json(run / "loop-evidence.json", evidence)
    elif damage == "stale-consumer":
        (fixture["repository"] / "consumer.py").write_text("VALUE = 9\n", encoding="utf-8", newline="\n")
    elif damage == "forged-reviewer":
        ledger = _read_ledger(run / "loop-ledger.json")
        ledger["rounds"][0]["reviewer"]["identity"] = "unrelated-child"
        _write_ledger(run / "loop-ledger.json", ledger)
    else:
        (run / "review-1.md").write_text("unrelated text\n", encoding="utf-8", newline="\n")

    with pytest.raises(ValueError, match=error):
        validator.validate_loop_evidence(run, codex_home)


def test_scoped_interaction_rejects_undelivered_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A correctly assigned interaction still fails if its consumer was not delivered."""
    monkeypatch.setenv("CODEX_THREAD_ID", "parent-thread")
    fixture = _scoped_fixture(tmp_path, include_consumer=False)

    with pytest.raises(ValueError, match="interaction"):
        fixture["validator"].validate_loop_evidence(fixture["run"], fixture["codex_home"])


@pytest.mark.parametrize(
    "audit",
    [pytest.param(False, id="material-interaction"), pytest.param(True, id="inventory-unit")],
)
def test_non_clean_round_retains_authenticated_unresolved_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, audit: bool
) -> None:
    """An open finding permits a reviewer to preserve an unresolved assessment without claiming clean."""
    monkeypatch.setenv("CODEX_THREAD_ID", "parent-thread")
    finding = {
        "signature": "missing-guard",
        "tier": "high",
        "structural": False,
        "disposition": "open",
        "evidence": ["The widget source lacks the required guard."],
    }
    requirement = (
        {
            "id": "interaction-inventory-audit",
            "source_paths": [],
            "claim": "Review the changed-unit index for overlooked interactions.",
            "unit_ids": ["unit-001"],
        }
        if audit
        else {"id": "widget-consumer", "source_paths": ["consumer.py", "widget.py"], "claim": "Check the value."}
    )
    fixture = _scoped_fixture(tmp_path, requirements=[requirement], findings=[finding])
    verdict: dict[str, Any] = {
        "id": requirement["id"],
        "verdict": "unresolved",
        "evidence": ["The missing guard prevents a complete assessment."],
    }
    if audit:
        verdict["unit_verdicts"] = [{"id": "unit-001", "verdict": "unresolved"}]
    _write_authenticated_verdict(fixture["fixture"], [verdict])

    ledger = _read_ledger(fixture["run"] / "loop-ledger.json")
    assert fixture["validator"].summarize_ledger(ledger)["reason"] != "clean"
    fixture["validator"].validate_loop_evidence(fixture["run"], fixture["codex_home"])


def test_scoped_interaction_rejects_forged_source_assignment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A signed reviewer claim cannot turn an absent source file into delivered evidence."""
    monkeypatch.setenv("CODEX_THREAD_ID", "parent-thread")
    requirement = {"id": "missing-source", "source_paths": ["not-delivered.py"], "claim": "Check this source."}
    fixture = _scoped_fixture(tmp_path, requirements=[requirement])

    with pytest.raises(ValueError, match="interaction-source-missing"):
        fixture["validator"].validate_loop_evidence(fixture["run"], fixture["codex_home"])


def test_scoped_interaction_accepts_no_interactions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An independently reviewed assignment can explicitly have no interaction claims."""
    monkeypatch.setenv("CODEX_THREAD_ID", "parent-thread")
    fixture = _scoped_fixture(tmp_path, requirements=[])

    fixture["validator"].validate_loop_evidence(fixture["run"], fixture["codex_home"])


def test_inventory_audit_requires_authentic_verdicts_for_assigned_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bind the audit's exact units to the reviewer, not parent-provided metadata."""
    monkeypatch.setenv("CODEX_THREAD_ID", "parent-thread")
    requirement = {
        "id": "interaction-inventory-audit",
        "source_paths": [],
        "claim": "Review the changed-unit index for overlooked interactions.",
        "unit_ids": ["unit-001", "unit-002"],
    }
    fixture = _scoped_fixture(tmp_path, requirements=[requirement])

    fixture["validator"].validate_loop_evidence(fixture["run"], fixture["codex_home"])


@pytest.mark.parametrize(
    "unit_verdicts",
    [
        pytest.param([{"id": "unit-001", "verdict": "reviewed"}], id="missing-unit"),
        pytest.param(
            [{"id": "unit-001", "verdict": "reviewed"}, {"id": "unit-001", "verdict": "reviewed"}], id="duplicate-unit"
        ),
        pytest.param(
            [{"id": "unit-001", "verdict": "reviewed"}, {"id": "unit-003", "verdict": "reviewed"}], id="extra-unit"
        ),
        pytest.param(
            [{"id": "unit-001", "verdict": "reviewed"}, {"id": "unit-002", "verdict": "unresolved"}],
            id="unresolved-unit",
        ),
        pytest.param(
            [{"id": "unit-002", "verdict": "reviewed"}, {"id": "unit-001", "verdict": "reviewed"}], id="unsorted-units"
        ),
    ],
)
def test_inventory_audit_rejects_incomplete_or_unresolved_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unit_verdicts: list[dict[str, str]]
) -> None:
    """An auditor cannot certify unassigned, missing, duplicated, or unresolved units."""
    monkeypatch.setenv("CODEX_THREAD_ID", "parent-thread")
    requirement = {
        "id": "interaction-inventory-audit",
        "source_paths": [],
        "claim": "Review the changed-unit index for overlooked interactions.",
        "unit_ids": ["unit-001", "unit-002"],
    }
    fixture = _scoped_fixture(tmp_path, requirements=[requirement])
    validator, run, codex_home = (fixture[key] for key in ("validator", "run", "codex_home"))
    validator.validate_loop_evidence(run, codex_home)
    _write_authenticated_verdict(
        fixture["fixture"],
        [
            {
                "id": requirement["id"],
                "verdict": "reviewed",
                "evidence": ["Checked the assigned units."],
                "unit_verdicts": unit_verdicts,
            }
        ],
    )

    with pytest.raises(ValueError, match="interaction"):
        validator.validate_loop_evidence(run, codex_home)


@pytest.mark.parametrize(
    "requirements",
    [
        pytest.param(
            [{"id": "widget-consumer", "source_paths": [], "claim": "The consumer reads the value."}],
            id="material-claim-without-source",
        ),
        pytest.param([{"id": "widget-consumer", "source_paths": ["widget.py"], "claim": ""}], id="empty-claim"),
        pytest.param(
            [
                {"id": "same", "source_paths": ["widget.py"], "claim": "First."},
                {"id": "same", "source_paths": ["widget.py"], "claim": "Second."},
            ],
            id="duplicate-requirement",
        ),
        pytest.param(
            [
                {"id": "z-last", "source_paths": ["widget.py"], "claim": "First."},
                {"id": "a-first", "source_paths": ["widget.py"], "claim": "Second."},
            ],
            id="unsorted-requirements",
        ),
    ],
)
def test_scoped_interaction_rejects_invalid_requirements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, requirements: list[dict[str, Any]]
) -> None:
    """Material requirements must be source-backed, unique, sorted, and descriptive."""
    monkeypatch.setenv("CODEX_THREAD_ID", "parent-thread")
    fixture = _scoped_fixture(tmp_path, requirements=requirements)

    with pytest.raises(ValueError, match="interaction"):
        fixture["validator"].validate_loop_evidence(fixture["run"], fixture["codex_home"])


def test_schema_two_remains_valid_without_interactions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Preserve existing completed reviews under their historical evidence contract."""
    monkeypatch.setenv("CODEX_THREAD_ID", "parent-thread")
    fixture = _loop_evidence_run(tmp_path, current_contract=True)

    fixture["validator"].validate_loop_evidence(fixture["run"], fixture["codex_home"])


@pytest.mark.parametrize("kind", [pytest.param([], id="list-kind"), pytest.param({}, id="object-kind")])
def test_primary_source_rejects_non_string_kind_as_validation_error(kind: object) -> None:
    """Untrusted malformed record types must produce the stable source validation error."""
    with pytest.raises(ValueError, match="loop-evidence-primary-source-incomplete"):
        _validator()._primary_source_paths({"files": [{"path": "widget.py", "kind": kind}]}, ["widget.py"])

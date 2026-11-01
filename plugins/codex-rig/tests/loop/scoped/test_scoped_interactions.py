"""Verify scoped interaction claims against authenticated review evidence.

Each test builds real native reviewer artifacts for one producer-to-consumer interaction, then rewrites exactly one
piece (verdict, request, endpoint, ledger or output) to prove the loop-evidence validator accepts only evidence whose
assigned interactions are delivered, current and resolved by the authentic reviewer.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from _artifact_files import write_json
from test_loop_review_evidence import (
    _loop_evidence_run,
    _read_ledger,
    _rewrite_native_outputs,
    _validator,
    _write_ledger,
)

AUDIT_ID = "interaction-inventory-audit"


@pytest.fixture(autouse=True)
def _parent_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """Identify the parent Codex thread whose session records the validator authenticates against."""
    monkeypatch.setenv("CODEX_THREAD_ID", "parent-thread")


def _audit_requirement(*unit_ids: str) -> dict[str, Any]:
    """Return the changed-unit inventory audit requirement over ``unit_ids``."""
    return {
        "id": AUDIT_ID,
        "source_paths": [],
        "claim": "Review the changed-unit index for overlooked interactions.",
        "unit_ids": list(unit_ids),
    }


def _validate(fixture: dict[str, Any]) -> None:
    """Run the loop-evidence validator over a scoped fixture."""
    fixture["validator"].validate_loop_evidence(fixture["run"], fixture["codex_home"])


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
    write_json(review / "specialist-manifest.json", fixture["manifest"])

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
    write_json(run / "loop-evidence.json", evidence)
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
        if requirement["id"] == AUDIT_ID:
            verdict["unit_verdicts"] = [{"id": unit, "verdict": "reviewed"} for unit in requirement["unit_ids"]]
    _write_authenticated_verdict(native, verdicts)
    return fixture


def _damage_unsigned_request(fixture: dict[str, Any]) -> None:
    """Replace the signed request specification so the interaction assessment is not assigned."""
    path = fixture["run"] / "loop-evidence.json"
    evidence = json.loads(path.read_bytes())
    evidence["request"]["specification"] = "The interaction was not assigned."
    write_json(path, evidence)


def _damage_stale_consumer(fixture: dict[str, Any]) -> None:
    """Edit the delivered consumer after the review."""
    (fixture["repository"] / "consumer.py").write_text("VALUE = 9\n", encoding="utf-8", newline="\n")


def _damage_forged_reviewer(fixture: dict[str, Any]) -> None:
    """Rewrite the ledger reviewer identity to an unrelated child."""
    path = fixture["run"] / "loop-ledger.json"
    ledger = _read_ledger(path)
    ledger["rounds"][0]["reviewer"]["identity"] = "unrelated-child"
    _write_ledger(path, ledger)


def _damage_tampered_output(fixture: dict[str, Any]) -> None:
    """Replace the saved reviewer output with unrelated text."""
    (fixture["run"] / "review-1.md").write_text("unrelated text\n", encoding="utf-8", newline="\n")


class TestScopedInteraction:
    """A producer-to-consumer interaction assigned to the independent reviewer."""

    @pytest.mark.parametrize(
        "options",
        [
            pytest.param({}, id="default-interaction"),
            pytest.param({"requirements": []}, id="no-interactions"),
            pytest.param({"requirements": [_audit_requirement("unit-001", "unit-002")]}, id="inventory-audit-units"),
        ],
    )
    def test_accepts_authenticated_assessment(self, tmp_path: Path, options: dict[str, Any]) -> None:
        """Clean evidence is accepted when exact endpoints and the reviewer verdict are bound.

        Covers the default consumer interaction, an assignment that explicitly has no interactions, and the audit whose
        per-unit verdicts come from the reviewer rather than parent-provided metadata.
        """
        fixture = _scoped_fixture(tmp_path, **options)

        _validate(fixture)

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
                [{"id": "widget-consumer", "verdict": "unknown", "evidence": ["Unknown consumer."]}],
                id="unknown-verdict",
            ),
            pytest.param([{"id": "widget-consumer", "verdict": "reviewed", "evidence": []}], id="empty-evidence"),
        ],
    )
    def test_rejects_invalid_authenticated_verdict(self, tmp_path: Path, verdicts: list[dict[str, Any]]) -> None:
        """A clean loop cannot replace, omit, duplicate or defer its assigned interaction.

        The evidence validates first; only the reviewer verdict is then rewritten to the invalid shape.
        """
        fixture = _scoped_fixture(tmp_path)
        _validate(fixture)
        _write_authenticated_verdict(fixture["fixture"], verdicts)

        with pytest.raises(ValueError, match="interaction"):
            _validate(fixture)

    @pytest.mark.parametrize(
        ("damage", "error"),
        [
            pytest.param(_damage_unsigned_request, "interaction|review-request", id="unsigned-request"),
            pytest.param(_damage_stale_consumer, "current-supporting-mismatch", id="stale-consumer"),
            pytest.param(_damage_forged_reviewer, "reviewer-identity-mismatch", id="forged-reviewer"),
            pytest.param(_damage_tampered_output, "report-bytes-mismatch", id="tampered-output"),
        ],
    )
    def test_rejects_unbound_evidence(
        self, tmp_path: Path, damage: Callable[[dict[str, Any]], None], error: str
    ) -> None:
        """The verdict requires delivered, current endpoints and an authentic reviewer.

        Valid evidence is damaged in one way (unsigned request, stale consumer, forged reviewer, tampered output).
        """
        fixture = _scoped_fixture(tmp_path)
        _validate(fixture)
        damage(fixture)

        with pytest.raises(ValueError, match=error):
            _validate(fixture)

    def test_rejects_undelivered_endpoint(self, tmp_path: Path) -> None:
        """A correctly assigned interaction still fails if its consumer was not delivered to the reviewer."""
        fixture = _scoped_fixture(tmp_path, include_consumer=False)

        with pytest.raises(ValueError, match="interaction"):
            _validate(fixture)

    def test_rejects_forged_source_assignment(self, tmp_path: Path) -> None:
        """A signed reviewer claim cannot turn an absent source file into delivered evidence."""
        requirement = {"id": "missing-source", "source_paths": ["not-delivered.py"], "claim": "Check this source."}
        fixture = _scoped_fixture(tmp_path, requirements=[requirement])

        with pytest.raises(ValueError, match="interaction-source-missing"):
            _validate(fixture)

    @pytest.mark.parametrize(
        ("requirement", "extra"),
        [
            pytest.param(
                {"id": "widget-consumer", "source_paths": ["consumer.py", "widget.py"], "claim": "Check the value."},
                {},
                id="material-interaction",
            ),
            pytest.param(
                _audit_requirement("unit-001"),
                {"unit_verdicts": [{"id": "unit-001", "verdict": "unresolved"}]},
                id="inventory-unit",
            ),
        ],
    )
    def test_non_clean_round_keeps_unresolved_verdict(
        self, tmp_path: Path, requirement: dict[str, Any], extra: dict[str, Any]
    ) -> None:
        """An open finding lets the reviewer preserve an unresolved assessment without claiming the round is clean."""
        finding = {
            "signature": "missing-guard",
            "tier": "high",
            "structural": False,
            "disposition": "open",
            "evidence": ["The widget source lacks the required guard."],
        }
        fixture = _scoped_fixture(tmp_path, requirements=[requirement], findings=[finding])
        verdict = {
            "id": requirement["id"],
            "verdict": "unresolved",
            "evidence": ["The missing guard prevents a complete assessment."],
            **extra,
        }
        _write_authenticated_verdict(fixture["fixture"], [verdict])
        ledger = _read_ledger(fixture["run"] / "loop-ledger.json")

        assert fixture["validator"].summarize_ledger(ledger)["reason"] != "clean"
        _validate(fixture)

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
    def test_rejects_invalid_requirements(self, tmp_path: Path, requirements: list[dict[str, Any]]) -> None:
        """Material requirements must be source-backed, unique, sorted and described by a claim."""
        fixture = _scoped_fixture(tmp_path, requirements=requirements)

        with pytest.raises(ValueError, match="interaction"):
            _validate(fixture)


class TestInventoryAudit:
    """The changed-unit inventory audit and its per-unit verdicts."""

    @pytest.mark.parametrize(
        "unit_verdicts",
        [
            pytest.param([{"id": "unit-001", "verdict": "reviewed"}], id="missing-unit"),
            pytest.param(
                [{"id": "unit-001", "verdict": "reviewed"}, {"id": "unit-001", "verdict": "reviewed"}],
                id="duplicate-unit",
            ),
            pytest.param(
                [{"id": "unit-001", "verdict": "reviewed"}, {"id": "unit-003", "verdict": "reviewed"}],
                id="extra-unit",
            ),
            pytest.param(
                [{"id": "unit-001", "verdict": "reviewed"}, {"id": "unit-002", "verdict": "unresolved"}],
                id="unresolved-unit",
            ),
            pytest.param(
                [{"id": "unit-002", "verdict": "reviewed"}, {"id": "unit-001", "verdict": "reviewed"}],
                id="unsorted-units",
            ),
        ],
    )
    def test_rejects_incomplete_or_unresolved_units(self, tmp_path: Path, unit_verdicts: list[dict[str, str]]) -> None:
        """An auditor cannot certify unassigned, missing, duplicated, unsorted or unresolved units."""
        fixture = _scoped_fixture(tmp_path, requirements=[_audit_requirement("unit-001", "unit-002")])
        _validate(fixture)
        verdict = {
            "id": AUDIT_ID,
            "verdict": "reviewed",
            "evidence": ["Checked the assigned units."],
            "unit_verdicts": unit_verdicts,
        }
        _write_authenticated_verdict(fixture["fixture"], [verdict])

        with pytest.raises(ValueError, match="interaction"):
            _validate(fixture)


def test_schema_two_remains_valid_without_interactions(tmp_path: Path) -> None:
    """Completed reviews recorded under the historical schema-two contract still validate.

    Schema two predates interaction requirements; requiring them retroactively would invalidate existing reviews.
    """
    fixture = _loop_evidence_run(tmp_path, current_contract=True)

    _validate(fixture)


@pytest.mark.parametrize("kind", [pytest.param([], id="list-kind"), pytest.param({}, id="object-kind")])
def test_primary_source_rejects_non_string_kind(kind: object) -> None:
    """Untrusted malformed record types produce the stable source validation error, not a raw exception."""
    with pytest.raises(ValueError, match="loop-evidence-primary-source-incomplete"):
        _validator()._primary_source_paths({"files": [{"path": "widget.py", "kind": kind}]}, ["widget.py"])

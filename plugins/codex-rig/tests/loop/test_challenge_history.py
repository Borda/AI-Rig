"""Keep descriptive review history visible without granting canonical admission."""

import copy
import json
from pathlib import Path

import pytest
from test_adversarial_loop_artifacts import _write_loop
from test_final_handoff import _handoff_payload, _load_finalizer, _load_shared_validator
from test_loop_review_evidence import _read_ledger, _write_ledger


def _history(tmp_path: Path) -> dict:
    """Build two completed advisory checkpoints with retained local reports."""
    payload = _handoff_payload()
    payload.update(skill="challenge-resolve", presentation_version=3)
    payload["tables"] = [
        {
            "heading": "Results",
            "layout": "challenge-history",
            "columns": list(_load_finalizer().CHALLENGE_SEVERITY_COLUMNS),
            "rows": [
                {
                    "id": "CR-1",
                    "admission": "advisory",
                    "admission_reason": "Reviewer provenance unavailable",
                    "cells": [
                        "Advisory 1",
                        "0 + 0",
                        "0 + 0",
                        "0 + 0",
                        "0 + 1",
                        "0 + 0",
                        "0 + 0",
                        "0 + 4",
                        "baseline",
                        "first review.md",
                    ],
                    "source_ids": ["report:CR-1"],
                },
                {
                    "id": "CR-2",
                    "admission": "advisory",
                    "admission_reason": "Reviewer provenance unavailable",
                    "cells": [
                        "Checkpoint 2",
                        "0 + 0",
                        "0 + 0",
                        "0 + 0",
                        "0 + 0",
                        "0 + 1",
                        "0 + 0",
                        "0 + 2",
                        "decreasing",
                        "checkpoint.md",
                    ],
                    "source_ids": ["report:CR-2"],
                },
            ],
        }
    ]
    (tmp_path / "first review.md").write_text("Reviewer A: one medium finding.\n", encoding="utf-8")
    (tmp_path / "checkpoint.md").write_text("Constituent reviewers: A and B. One new low finding.\n", encoding="utf-8")
    return payload


def _render(tmp_path: Path, payload: dict) -> str:
    """Exercise the public file renderer and its digest-bound verification."""
    path = tmp_path / "final-handoff.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    finalizer = _load_finalizer()
    finalizer.render_files(path, tmp_path / "final.md", tmp_path / "final-handoff.validation.json")
    finalizer.check_files(path, tmp_path / "final.md", tmp_path / "final-handoff.validation.json")
    return (tmp_path / "final.md").read_text(encoding="utf-8")


def test_advisory_history_and_resources_are_visible(tmp_path: Path) -> None:
    """Expose every checkpoint, its limitation and chat-resolvable resources outside cells."""
    rendered = _render(tmp_path, _history(tmp_path))
    table_lines = [line for line in rendered.splitlines() if line.startswith("|")]
    assert len(table_lines) == 4
    assert "Advisory 1" in table_lines[2]
    assert "Checkpoint 2" in table_lines[3]
    assert "advisory" in table_lines[2].lower()
    assert "[R1]" in table_lines[2]
    assert "[R2]" in table_lines[3]
    assert all("](" not in line and ".md" not in line for line in table_lines)
    assert "No validated rounds" in rendered
    assert "Reviewer provenance unavailable" in rendered
    assert "first%20review.md" in rendered
    assert tmp_path.as_posix() in rendered
    assert rendered.index("**Resources**") > rendered.index(table_lines[-1])


@pytest.mark.parametrize("mutation", ["reason", "admission", "negative", "weight", "legacy"])
def test_history_rejects_misleading_rows(tmp_path: Path, mutation: str) -> None:
    """Reject hidden admission and fabricated arithmetic before rendering."""
    payload = _history(tmp_path)
    row = payload["tables"][0]["rows"][0]
    if mutation == "reason":
        row.pop("admission_reason")
    elif mutation == "admission":
        row["admission"] = "certified-ish"
    elif mutation == "negative":
        row["cells"][4] = "-1 + 2"
    elif mutation == "weight":
        row["cells"][7] = "0 + 0"
    else:
        payload["tables"][0].pop("layout")
    with pytest.raises(ValueError, match="challenge-history"):
        _render(tmp_path, payload)


@pytest.mark.parametrize("report", ["missing.md", "../outside.md", "", "https://example.com/review.md"])
def test_history_rejects_unretained_reports(tmp_path: Path, report: str) -> None:
    """Evidence resources must be nonempty local reports within the owning run."""
    payload = _history(tmp_path)
    payload["tables"][0]["rows"][0]["cells"][-1] = report
    with pytest.raises(ValueError, match=r"challenge-history|table-row-cells"):
        _render(tmp_path, payload)


@pytest.mark.parametrize("forge", [False, True])
def test_mixed_history_preserves_canonical_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forge: bool
) -> None:
    """An advisory row cannot substitute for or change an authenticated round."""
    result_path = _write_loop(tmp_path, monkeypatch)
    payload = json.loads((tmp_path / "final-handoff.json").read_text())
    payload["tables"][0]["layout"] = "challenge-history"
    payload["tables"][0]["rows"][0]["admission"] = "validated"
    advisory = _history(tmp_path)["tables"][0]["rows"][0]
    payload["tables"][0]["rows"].append(advisory)
    payload["source_records"].append({"id": "report:CR-1", "evidence": "first review.md"})
    payload["source_coverage"] = {
        "source_records_total": 2,
        "represented_source_records_total": 2,
        "omitted_source_records_total": 0,
    }
    if forge:
        advisory["admission"] = "validated"
        advisory.pop("admission_reason")
        advisory["cells"][0] = "2"
    _render(tmp_path, payload)
    result = json.loads(result_path.read_text())
    result["metadata"]["challenge_table_contract_version"] = 2
    binding = json.loads((tmp_path / "final-handoff.validation.json").read_text())
    result["metadata"]["final_handoff"].update({key: binding[key] for key in ("handoff_sha256", "rendered_sha256")})
    result_path.write_text(json.dumps(result))
    if forge:
        with pytest.raises(SystemExit, match="adversarial-loop-table-mismatch"):
            _load_shared_validator().validate("challenge-resolve", tmp_path, result_path)
    else:
        _load_shared_validator().validate("challenge-resolve", tmp_path, result_path)


@pytest.mark.parametrize("claim_pass", [False, True])
def test_advisory_only_history_cannot_pass_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, claim_pass: bool
) -> None:
    """Completed advisory reviews remain visible while all canonical gates stay failed."""
    result_path = _write_loop(tmp_path, monkeypatch)
    ledger = _read_ledger(tmp_path / "loop-ledger.json")
    ledger["rounds"] = []
    _write_ledger(tmp_path / "loop-ledger.json", ledger)
    (tmp_path / "loop-actions.json").write_text(json.dumps({"schema_version": 2, "rounds": []}))
    evidence = json.loads((tmp_path / "loop-evidence.json").read_text())
    evidence["rounds"] = []
    (tmp_path / "loop-evidence.json").write_text(json.dumps(evidence))
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    gates = json.loads((tmp_path / "gates.json").read_text())
    gates.update(status="fail", checks_failed=["review"])
    next(check for check in gates["checks"] if check["id"] == "review").update(status="fail", exit_code=1)
    (tmp_path / "gates.json").write_text(json.dumps(gates))
    original = json.loads((tmp_path / "final-handoff.json").read_text())
    payload = _history(tmp_path)
    for key in ("verification", "confidence", "artifacts"):
        payload[key] = copy.deepcopy(original[key])
    payload["verification"][-1]["status"] = "fail"
    _render(tmp_path, payload)
    result = json.loads(result_path.read_text())
    result.update(status="pass" if claim_pass else "fail", checks_failed=["review"])
    result["metadata"].update(
        challenge_table_contract_version=2,
        adversarial_loop={"status": "stopped", "reason": "independence-unavailable", "rounds": [], "scores": []},
    )
    binding = json.loads((tmp_path / "final-handoff.validation.json").read_text())
    result["metadata"]["final_handoff"].update({key: binding[key] for key in ("handoff_sha256", "rendered_sha256")})
    result_path.write_text(json.dumps(result))
    if claim_pass:
        with pytest.raises(SystemExit):
            _load_shared_validator().validate("challenge-resolve", tmp_path, result_path)
    else:
        _load_shared_validator().validate("challenge-resolve", tmp_path, result_path)


def test_no_review_history_keeps_unavailable_placeholder(tmp_path: Path) -> None:
    """Distinguish never-reviewed work from a completed but unadmitted review."""
    payload = _history(tmp_path)
    table = payload["tables"][0]
    table["rows"] = [
        {
            "id": "CR-2",
            "admission": "unavailable",
            "cells": ["not-run", *(["N/A"] * 7), "independence-unavailable", "checkpoint.md"],
            "source_ids": ["report:CR-1", "report:CR-2"],
        }
    ]
    rendered = _render(tmp_path, payload)
    assert "| not-run | N/A |" in rendered
    assert "No validated rounds" in rendered


def test_resource_targets_are_deduplicated(tmp_path: Path) -> None:
    """Two checkpoints citing one retained report share a resource without hiding rows."""
    payload = _history(tmp_path)
    payload["tables"][0]["rows"][1]["cells"][-1] = "first review.md"
    rendered = _render(tmp_path, payload)
    assert "[R2]" not in rendered
    assert rendered.count("- [R1]") == 1
    assert "Checkpoint 2" in rendered


def test_empty_report_cannot_supply_advisory_evidence(tmp_path: Path) -> None:
    """An existing filename alone is not retained review evidence."""
    payload = _history(tmp_path)
    (tmp_path / "first review.md").write_text("  ", encoding="utf-8")
    with pytest.raises(ValueError, match="challenge-history-report-empty"):
        _render(tmp_path, payload)


def test_chunk_coordinator_contract_two_keeps_child_routing() -> None:
    """The presentation upgrade cannot make a coordinator require parent-ledger artifacts."""
    assert _load_shared_validator()._is_chunked_coordinator(
        "challenge-resolve",
        {"metadata": {"challenge_table_contract_version": 2, "challenge_scope": {"mode": "chunked"}}},
    )

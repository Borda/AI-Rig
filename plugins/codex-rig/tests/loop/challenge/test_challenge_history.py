"""Keep descriptive review history visible without granting canonical admission."""

import copy
import json
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import pytest
from _final_handoff_support import handoff_payload, load_finalizer, load_shared_validator
from test_adversarial_loop_artifacts import _write_loop
from test_loop_review_evidence import _read_ledger, _write_ledger

BINDING_KEYS = ("handoff_sha256", "rendered_sha256")


def _history(tmp_path: Path) -> dict:
    """Build two completed advisory checkpoints with retained local reports."""
    payload = handoff_payload()
    payload.update(skill="challenge-resolve", presentation_version=8)
    payload["tables"] = [
        {
            "heading": "Results",
            "layout": "challenge-history",
            "columns": list(load_finalizer().CHALLENGE_SEVERITY_COLUMNS),
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
    finalizer = load_finalizer()
    finalizer.render_files(path, tmp_path / "final.md", tmp_path / "final-handoff.validation.json")
    finalizer.check_files(path, tmp_path / "final.md", tmp_path / "final-handoff.validation.json")
    return (tmp_path / "final.md").read_text(encoding="utf-8")


def _bind_rendered_handoff(tmp_path: Path, result_path: Path, **updates: Any) -> None:
    """Point the result at the rendered handoff digests and the history table contract, plus any extra updates.

    A `metadata` update is merged into the result metadata; every other keyword replaces a top-level result field.
    """
    result = json.loads(result_path.read_text())
    result["metadata"]["challenge_table_contract_version"] = 2
    binding = json.loads((tmp_path / "final-handoff.validation.json").read_text())
    result["metadata"]["final_handoff"].update({key: binding[key] for key in BINDING_KEYS})
    result["metadata"].update(updates.pop("metadata", {}))
    result.update(updates)
    result_path.write_text(json.dumps(result))


def _keep_advisory(advisory: dict) -> None:
    """Leave the advisory row as honestly marked."""


def _forge_validated(advisory: dict) -> None:
    """Relabel the advisory row as a validated round with a fabricated challenge number."""
    advisory["admission"] = "validated"
    advisory.pop("admission_reason")
    advisory["cells"][0] = "2"


def _drop_reason(table: dict) -> None:
    """Remove the explanation of why the first row is not canonical."""
    table["rows"][0].pop("admission_reason")


def _invent_admission(table: dict) -> None:
    """Replace the first row's admission class with an unknown value."""
    table["rows"][0]["admission"] = "certified-ish"


def _negative_count(table: dict) -> None:
    """Show a negative carried-plus-new finding count in the first row."""
    table["rows"][0]["cells"][4] = "-1 + 2"


def _wrong_weight(table: dict) -> None:
    """Show a weighted score that disagrees with the first row's severity counts."""
    table["rows"][0]["cells"][7] = "0 + 0"


def _drop_layout(table: dict) -> None:
    """Remove the history layout so the table reads as a legacy table."""
    table.pop("layout")


def test_shows_checkpoints_and_resources_outside_cells(tmp_path: Path) -> None:
    """Expose every checkpoint, its limitation and chat-resolvable resources outside cells.

    Two advisory checkpoints with retained local reports render as table rows with no links inside cells; the
    limitation, each checkpoint's resource path and a `Resources` list follow the table.
    """
    rendered = _render(tmp_path, _history(tmp_path))
    table_lines = [line for line in rendered.splitlines() if line.startswith("|")]
    assert len(table_lines) == 4
    assert "Advisory 1" in table_lines[2]
    assert "Checkpoint 2" in table_lines[3]
    assert "advisory" in table_lines[2].lower()
    assert all("[R" not in line for line in table_lines)
    assert all("](" not in line and ".md" not in line for line in table_lines)
    assert "No validated rounds" in rendered
    assert "Reviewer provenance unavailable" in rendered
    assert "first%20review.md" in rendered
    assert tmp_path.as_posix() in rendered
    assert "- Advisory 1:" in rendered
    assert "- Checkpoint 2:" in rendered
    assert rendered.index("- Advisory 1:") > rendered.index(table_lines[-1])
    assert rendered.index("**Resources**") > rendered.index(table_lines[-1])


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(_drop_reason, id="reason"),
        pytest.param(_invent_admission, id="admission"),
        pytest.param(_negative_count, id="negative"),
        pytest.param(_wrong_weight, id="weight"),
        pytest.param(_drop_layout, id="legacy"),
    ],
)
def test_rejects_misleading_rows(tmp_path: Path, mutate: Callable[[dict], None]) -> None:
    """Reject hidden admission and fabricated arithmetic before rendering.

    Each mutation leaves a table that would present an advisory checkpoint as canonical or show impossible counts; the
    `legacy` case drops the history layout, so the renderer cannot treat the table as history at all.
    """
    payload = _history(tmp_path)
    mutate(payload["tables"][0])

    with pytest.raises(ValueError, match="challenge-history"):
        _render(tmp_path, payload)


@pytest.mark.parametrize("report", ["missing.md", "../outside.md", "", "https://example.com/review.md"])
def test_rejects_unretained_reports(tmp_path: Path, report: str) -> None:
    """Evidence resources must be nonempty local reports within the owning run.

    A report that does not exist, escapes the run directory, is blank or is a remote URL cannot back an advisory row.
    """
    payload = _history(tmp_path)
    payload["tables"][0]["rows"][0]["cells"][-1] = report

    with pytest.raises(ValueError, match=r"challenge-history|table-row-cells"):
        _render(tmp_path, payload)


@pytest.mark.parametrize(
    ("mutate", "outcome"),
    [
        pytest.param(_keep_advisory, nullcontext(), id="honest-advisory-row"),
        pytest.param(
            _forge_validated,
            pytest.raises(SystemExit, match="adversarial-loop-table-mismatch"),
            id="forged-validated-row",
        ),
    ],
)
def test_mixed_history_preserves_canonical_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutate: Callable[[dict], None], outcome: Any
) -> None:
    """An advisory row cannot substitute for or change an authenticated round.

    A validated loop gains one advisory row beside its canonical round: honestly marked, canonical reconciliation still
    passes; relabelled as validated, the table no longer matches the authenticated ledger and is rejected.
    """
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
    mutate(advisory)
    _render(tmp_path, payload)
    _bind_rendered_handoff(tmp_path, result_path)

    with outcome:
        load_shared_validator().validate("challenge-resolve", tmp_path, result_path)


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        pytest.param("fail", nullcontext(), id="claims-failure"),
        pytest.param("pass", pytest.raises(SystemExit), id="claims-pass"),
    ],
)
def test_advisory_only_history_cannot_pass_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str, outcome: Any
) -> None:
    """Completed advisory reviews remain visible while all canonical gates stay failed.

    A loop whose only reviews are advisory has no authenticated round and a failed review gate: reporting that honestly
    as a failure validates, while claiming a pass is rejected.
    """
    result_path = _write_loop(tmp_path, monkeypatch)
    ledger = _read_ledger(tmp_path / "loop-ledger.json")
    ledger["rounds"] = []
    _write_ledger(tmp_path / "loop-ledger.json", ledger)
    (tmp_path / "loop-actions.json").write_text(json.dumps({"schema_version": 3, "rounds": []}))
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
    payload |= {key: copy.deepcopy(original[key]) for key in ("verification", "confidence", "artifacts")}
    payload["verification"][-1]["status"] = "fail"
    _render(tmp_path, payload)
    stopped = {"status": "stopped", "reason": "independence-unavailable", "rounds": [], "scores": []}
    _bind_rendered_handoff(
        tmp_path, result_path, status=status, checks_failed=["review"], metadata={"adversarial_loop": stopped}
    )

    with outcome:
        load_shared_validator().validate("challenge-resolve", tmp_path, result_path)


def test_no_review_history_keeps_unavailable_placeholder(tmp_path: Path) -> None:
    """Distinguish never-reviewed work from a completed but unadmitted review.

    A single `unavailable` row with `N/A` counts renders as a not-run placeholder, not as a zero-finding review.
    """
    payload = _history(tmp_path)
    payload["tables"][0]["rows"] = [
        {
            "id": "CR-2",
            "admission": "unavailable",
            "cells": ["not-run", *(["N/A"] * 7), "independence-unavailable", "checkpoint.md"],
            "source_ids": ["report:CR-1", "report:CR-2"],
        }
    ]
    rendered = _render(tmp_path, payload)
    assert "independence-unavailable" in rendered
    assert "- not-run:" in rendered
    assert "No validated rounds" in rendered


def test_resource_targets_are_deduplicated(tmp_path: Path) -> None:
    """List one shared report path for each history row without duplicating either row's item entry.

    Both checkpoints point at `first review.md`; each still gets its own resource line, once, with no numbered link.
    """
    payload = _history(tmp_path)
    payload["tables"][0]["rows"][1]["cells"][-1] = "first review.md"
    rendered = _render(tmp_path, payload)
    assert "[R2]" not in rendered
    shared_path = f"{tmp_path.as_posix()}/first%20review.md"
    assert rendered.count(f"- Advisory 1: {shared_path}") == 1
    assert rendered.count(f"- Checkpoint 2: {shared_path}") == 1
    assert "Checkpoint 2" in rendered


def test_empty_report_cannot_supply_advisory_evidence(tmp_path: Path) -> None:
    """An existing filename alone is not retained review evidence.

    A whitespace-only report file exists on disk but carries no review content, so the row it backs is rejected.
    """
    payload = _history(tmp_path)
    (tmp_path / "first review.md").write_text("  ", encoding="utf-8")

    with pytest.raises(ValueError, match="challenge-history-report-empty"):
        _render(tmp_path, payload)


def test_chunk_coordinator_contract_two_keeps_child_routing() -> None:
    """The presentation upgrade cannot make a coordinator require parent-ledger artifacts.

    A chunked-scope result at table contract 2 must still be routed as a child-backed coordinator.
    """
    assert load_shared_validator()._is_chunked_coordinator(
        "challenge-resolve",
        {"metadata": {"challenge_table_contract_version": 2, "challenge_scope": {"mode": "chunked"}}},
    )

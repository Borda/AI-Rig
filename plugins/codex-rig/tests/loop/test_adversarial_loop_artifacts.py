"""Check convergence evidence through the public workflow artifact validator."""

import copy
import hashlib
import itertools
import json
import shutil
import subprocess
import sys
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from _artifact_files import read_json
from _final_handoff_support import load_finalizer, load_shared_validator, write_schema_v2_assess
from test_loop_review_evidence import _loop_evidence_run, _read_ledger, _rewrite_native_outputs, _write_ledger
from test_loop_review_evidence import _validator as _load_loop_validator

BINDING_KEYS = ("handoff_sha256", "rendered_sha256")
CHUNK_CHECKER = Path(__file__).resolve().parents[2] / "skills" / "challenge-resolve" / "chunk_diff.py"
RESULT_FILENAMES = ["result.candidate.json", "result.json"]
FIRST_RUN_ORIGIN = {"kind": "first-run", "caller_run": None}


def _dump(path: Path, value: object) -> None:
    """Write one JSON artifact with LF endings."""
    path.write_text(json.dumps(value), encoding="utf-8", newline="\n")


def _load_handoff(run: Path) -> dict[str, Any]:
    """Read the run's final handoff payload."""
    return read_json(run / "final-handoff.json")


def _render_handoff(run: Path, handoff: dict[str, Any]) -> dict[str, Any]:
    """Write and render a handoff, returning the digests a result must carry for it."""
    handoff_path = run / "final-handoff.json"
    _dump(handoff_path, handoff)
    return load_finalizer().render_files(handoff_path, run / "final.md", run / "final-handoff.validation.json")


def _bind_handoff(run: Path, result_path: Path, result: dict[str, Any], handoff: dict[str, Any]) -> None:
    """Render a changed handoff and write the result bound to its digests."""
    validation = _render_handoff(run, handoff)
    result["metadata"]["final_handoff"].update({key: validation[key] for key in BINDING_KEYS})
    _dump(result_path, result)


def _validate(run: Path, result_path: Path) -> None:
    """Run the public challenge-resolve artifact validator on one result."""
    load_shared_validator().validate("challenge-resolve", run, result_path)


def _assert_rejected(run: Path, result_path: Path, match: str) -> None:
    """Assert the public validator refuses the result with an error matching `match`."""
    with pytest.raises(SystemExit, match=match):
        _validate(run, result_path)


def _named_result(run: Path, candidate: Path, filename: str) -> Path:
    """Return the result stored under `filename`, a byte copy of the candidate."""
    result_path = run / filename
    result_path.write_bytes(candidate.read_bytes())
    return result_path


def _set_review_gate(run: Path, status: str, exit_code: int, checks_failed: list[str]) -> dict[str, Any]:
    """Record the review gate outcome in `gates.json` and return the gates."""
    gates = read_json(run / "gates.json")
    next(check for check in gates["checks"] if check["id"] == "review").update(status=status, exit_code=exit_code)
    gates.update(status=status, checks_failed=checks_failed)
    _dump(run / "gates.json", gates)
    return gates


def _fail_review_gate(run: Path) -> dict[str, Any]:
    """Record a failed review gate and return the gates."""
    return _set_review_gate(run, "fail", 1, ["review"])


def _record_remaining(handoff: dict[str, Any], item: str, next_action: str, row_id: str = "1") -> None:
    """Add one open item owned by the parent and make it the only next step."""
    handoff["remaining"] = [{"row_id": row_id, "item": item, "owner": "parent", "next_action": next_action}]
    handoff["next_steps"] = [row_id]


def _set_chunk_only(handoff: dict[str, Any], child_runs: list[dict[str, Any]]) -> None:
    """Present the handoff as a coordinator whose only evidence is its child runs."""
    handoff["presentation_version"] = 8
    handoff["tables"][0]["rows"][0]["cells"] = ["not-run", *(["N/A"] * 7), "chunk-only", "chunks/results.json"]
    handoff["child_runs"] = child_runs


def _set_chunk_review_outcome(handoff: dict[str, Any], summary: str) -> None:
    """Title the handoff outcome as a chunk review with the given summary."""
    handoff["outcome"] = {"title": "Chunk review", "summary": summary}


def _chunked_scope() -> dict[str, str]:
    """Return the challenge scope of a coordinator that delegates to `chunks/`."""
    return {"mode": "chunked", "manifest_path": "chunks/chunks.json", "results_path": "chunks/results.json"}


def _chunked_result(result_path: Path, summary: dict[str, Any], origin: dict[str, Any]) -> dict[str, Any]:
    """Turn a loop result into a chunked coordinator result that reports `summary` and has no parent review."""
    result = read_json(result_path)
    result["metadata"].pop("adversarial_loop", None)
    result["metadata"]["challenge_scope"] = _chunked_scope()
    result["metadata"]["chunked_review"] = copy.deepcopy(summary)
    result["metadata"]["challenge_origin"] = dict(origin)
    return result


def _drop_parent_ledger(run: Path) -> None:
    """Delete the parent ledger, which a child-backed coordinator does not own."""
    (run / "loop-ledger.json").unlink()
    (run / "loop-rounds.jsonl").unlink()


def _write_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    findings: list[dict[str, object]] | None = None,
    evidence_out: dict[str, object] | None = None,
) -> Path:
    """Create a complete independently clean loop using the common handoff fixture."""
    result_path = write_schema_v2_assess(tmp_path)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    evidence = _loop_evidence_run(tmp_path, run_dir=tmp_path, findings=findings, current_contract=True)
    if evidence_out is not None:
        evidence_out.update(evidence)
    monkeypatch.setenv("CODEX_HOME", str(evidence["codex_home"]))
    monkeypatch.setenv("CODEX_THREAD_ID", "parent-thread")
    (tmp_path / "current.diff").write_bytes((tmp_path / "round-1.diff").read_bytes())
    (tmp_path / "loop-report.md").write_text(
        "# Scope\nParser.\n# Rounds\nOne.\n# Findings\nNone.\n# Recovery\nNone.\n# Verification\nPassed.\n",
        encoding="utf-8",
    )
    counts = {"security": 0, "critical": 0, "high": 0, "medium": 0, "low": 0, "nit": 0}
    result["metadata"]["adversarial_loop"] = {
        "status": "stopped",
        "reason": "clean",
        "scores": [0],
        "rounds": [{"index": 1, "score": 0, "counts": counts, "decision": "clean"}],
    }
    result["metadata"]["action_contract_version"] = 3
    result["metadata"]["challenge_table_contract_version"] = 1
    result["metadata"]["challenge_scope"] = {"mode": "single"}
    result["metadata"]["challenge_origin"] = {
        "kind": "first-run",
        "caller_run": None,
        "prior_ledger_sha256": None,
    }
    open_findings = [
        finding for finding in findings or [] if finding["disposition"] in {"open", "fixed-pending-verification"}
    ]
    (tmp_path / "loop-actions.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "rounds": [
                    {
                        "index": 1,
                        "actions": [
                            {
                                "signature": finding["signature"],
                                "decision": (
                                    "escalate" if finding["tier"] in {"security", "critical", "high"} else "defer"
                                ),
                                "evidence": [
                                    f"invariant: {finding['signature']} holds",
                                    "The finding remains open pending the next scoped action.",
                                ],
                                "owner": "parent",
                                "next_action": "Request the needed scope or independent verification.",
                                "root_cause": None,
                            }
                            for finding in open_findings
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    handoff = _load_handoff(tmp_path)
    handoff["skill"] = "challenge-resolve"
    handoff["presentation_version"] = 8
    handoff["outcome"] = {"title": "Parser review", "summary": "Parser review is clean; no code fixes were needed."}
    handoff["tables"] = [
        {
            "heading": "Iterations",
            "columns": [
                "Challenge",
                "Security",
                "Critical",
                "High",
                "Medium",
                "Low",
                "Nits",
                "Weighted score",
                "Decision",
                "Evidence",
            ],
            "rows": [
                {
                    "id": "1",
                    "cells": [
                        "1",
                        *(f"0 + {counts[tier]}" for tier in ("security", "critical", "high", "medium", "low", "nit")),
                        "0 + 0",
                        "clean",
                        "review-1.md",
                    ],
                    "source_ids": ["analysis:CA-1"],
                }
            ],
        }
    ]
    _bind_handoff(tmp_path, result_path, result, handoff)
    return result_path


def _downgrade_evidence_to_schema_one(evidence: dict[str, Any]) -> None:
    """Rewrite loop evidence as a schema-1 record that predates request delivery."""
    evidence["schema_version"] = 1
    for key in ("request", "supporting_paths", "current_supporting_source_path", "origin"):
        evidence.pop(key)
    for round_record in evidence["rounds"]:
        round_record.pop("supporting_source_path")
        round_record.pop("triage")


def _drop_round_triage(evidence: dict[str, Any]) -> None:
    """Remove the parent triage from every round of a schema-2 evidence record."""
    for round_record in evidence["rounds"]:
        del round_record["triage"]


class TestCurrentResultContract:
    """The current result contract: what a clean loop must carry and which historical shapes are refused."""

    def test_rejects_historical_inline_ledger(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Refuse a current challenge result whose ledger still keeps every round inline in one rewritten file.

        The same rounds that validate in the current header-plus-round-log layout are rewritten as a schema-1 file: the
        standalone checker still reads that archive shape, but a current result must record rounds append-only.
        """
        result_path = _write_loop(tmp_path, monkeypatch)
        historical = _read_ledger(tmp_path / "loop-ledger.json") | {"schema_version": 1}
        (tmp_path / "loop-rounds.jsonl").unlink()
        _dump(tmp_path / "loop-ledger.json", historical)

        _assert_rejected(tmp_path, result_path, "adversarial-loop-ledger-schema-v2-required")

    @pytest.mark.parametrize("filename", RESULT_FILENAMES)
    def test_accepts_clean_loop_and_rejects_changed_snapshot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str
    ) -> None:
        """Accept complete current evidence and reject a changed snapshot for either filename.

        A clean loop validates as `result.candidate.json` and as the promoted `result.json`; replacing the frozen
        `current.diff` afterwards must break the snapshot digest, so a later edit cannot ride on an earlier review.
        """
        result_path = _named_result(tmp_path, _write_loop(tmp_path, monkeypatch), filename)
        _validate(tmp_path, result_path)
        (tmp_path / "current.diff").write_bytes(b"unreviewed change")

        _assert_rejected(tmp_path, result_path, r"adversarial-loop-snapshot-digest-mismatch:current.diff")

    @pytest.mark.parametrize(
        ("old", "new", "outcome"),
        [
            pytest.param("# Recovery", "# Remediation", nullcontext(), id="remediation-heading"),
            pytest.param(
                "# Recovery\nNone.\n",
                "",
                pytest.raises(SystemExit, match=r"missing-artifact-section:loop-report.md:Remediation"),
                id="action-section-omitted",
            ),
            pytest.param(
                "# Recovery\nNone.\n",
                "Remediation is mentioned here but has no heading.\n",
                pytest.raises(SystemExit, match=r"missing-artifact-section:loop-report.md:Remediation"),
                id="word-in-body-only",
            ),
        ],
    )
    def test_review_action_report_section(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, old: str, new: str, outcome: Any
    ) -> None:
        """The report needs a review-action heading, current or historical, and a body mention never counts.

        The generated `loop-report.md` is edited: renaming the historical `Recovery` heading to the new `Remediation`
        wording is accepted, while omitting the section or only mentioning the word in a body paragraph is refused.
        """
        result_path = _write_loop(tmp_path, monkeypatch)
        report_path = tmp_path / "loop-report.md"
        report_path.write_text(report_path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")

        with outcome:
            _validate(tmp_path, result_path)

    @pytest.mark.parametrize("filename", RESULT_FILENAMES)
    @pytest.mark.parametrize(
        ("key", "error"),
        [
            pytest.param("challenge_scope", "adversarial-loop-challenge-scope-required", id="challenge-scope"),
            pytest.param(
                "challenge_table_contract_version", "adversarial-loop-table-contract-required", id="table-contract"
            ),
        ],
    )
    def test_requires_current_metadata(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str, key: str, error: str
    ) -> None:
        """Neither result filename can omit the explicit challenge scope or the ranked table contract version.

        An omitted scope would bypass chunk coverage and an omitted table version would bypass the ranked severity
        columns, so each is required on a current result under both filenames.
        """
        result_path = _named_result(tmp_path, _write_loop(tmp_path, monkeypatch), filename)
        result = read_json(result_path)
        del result["metadata"][key]
        _dump(result_path, result)

        _assert_rejected(tmp_path, result_path, error)

    @pytest.mark.parametrize("filename", RESULT_FILENAMES)
    @pytest.mark.parametrize(
        ("error", "mutate"),
        [
            pytest.param(
                "adversarial-loop-evidence-contract-required",
                _downgrade_evidence_to_schema_one,
                id="schema-one-evidence",
            ),
            pytest.param(
                "adversarial-loop-evidence-invalid:loop-evidence-round-shape-invalid",
                _drop_round_triage,
                id="untriaged-schema-two",
            ),
        ],
    )
    def test_rejects_evidence_without_current_contract(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        filename: str,
        error: str,
        mutate: Callable[[dict[str, Any]], None],
    ) -> None:
        """Old evidence stays readable but cannot validate a current result under either filename.

        Schema-1 evidence lacks request delivery and an earlier untriaged schema-2 record lacks parent triage; both must
        be refused so a current loop cannot be certified by evidence that predates the contract.
        """
        candidate = _write_loop(tmp_path, monkeypatch)
        evidence_path = tmp_path / "loop-evidence.json"
        evidence = read_json(evidence_path)
        mutate(evidence)
        _dump(evidence_path, evidence)
        result_path = _named_result(tmp_path, candidate, filename)

        _assert_rejected(tmp_path, result_path, error)

    @pytest.mark.parametrize("old_contract", [1, 2])
    def test_rejects_old_action_contract(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, old_contract: int
    ) -> None:
        """Keep schema-1 and schema-2 action records readable without using them to certify a current loop.

        A current result that declares action contract 1 or 2 predates bound parent actions and must be refused.
        """
        result_path = _write_loop(tmp_path, monkeypatch)
        result = read_json(result_path)
        result["metadata"]["action_contract_version"] = old_contract
        _dump(result_path, result)

        _assert_rejected(tmp_path, result_path, "adversarial-loop-action-contract-required")

    def test_requires_actions_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Reject a new result whose finding actions file is missing."""
        result_path = _write_loop(tmp_path, monkeypatch)
        (tmp_path / "loop-actions.json").unlink()

        _assert_rejected(tmp_path, result_path, "adversarial-loop-invalid-actions:")

    def test_requires_action_contract_version(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Reject a new result that omits its action contract version, as candidate and as promoted result.

        Without the version a result could bypass the parent-action checks entirely, so the same version-less result is
        refused under both filenames.
        """
        candidate = _write_loop(tmp_path, monkeypatch)
        result = read_json(candidate)
        result["metadata"].pop("action_contract_version")
        _dump(candidate, result)
        promoted = tmp_path / "result.json"
        _dump(promoted, result)

        _assert_rejected(tmp_path, candidate, "adversarial-loop-action-contract-required")
        _assert_rejected(tmp_path, promoted, "adversarial-loop-action-contract-required")

    def test_historical_result_keeps_its_compact_table(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Keep pre-change canonical results readable without weakening new candidates.

        A result with the compact five-column table and no table or scope metadata is the historical shape. Both the
        promoted file and a candidate that adds back the scope still require the table contract version.
        """
        candidate = _write_loop(tmp_path, monkeypatch)
        handoff = _load_handoff(tmp_path)
        handoff["tables"][0]["columns"] = ["Iteration", "Open findings", "Weighted score", "Decision", "Evidence"]
        handoff["tables"][0]["rows"][0]["cells"] = [
            "1",
            "security=0, critical=0, high=0, medium=0, low=0, nit=0",
            "0",
            "clean",
            "review-1.md",
        ]
        result = read_json(candidate)
        del result["metadata"]["challenge_table_contract_version"]
        del result["metadata"]["challenge_scope"]
        canonical = tmp_path / "result.json"
        _bind_handoff(tmp_path, canonical, result, handoff)
        result["metadata"]["challenge_scope"] = {"mode": "single"}
        _dump(candidate, result)

        assert read_json(canonical)["metadata"]["adversarial_loop"]["scores"] == [0]
        _assert_rejected(tmp_path, canonical, "adversarial-loop-table-contract-required")
        _assert_rejected(tmp_path, candidate, "adversarial-loop-table-contract-required")

    def test_historical_ranked_totals_remain_readable(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Read a prior version-2 result while requiring splits for new candidates.

        Table contract 2 showed one total per severity instead of `carried + new`; that file stays readable, but the
        same table must be refused as a promoted result and as a new candidate once splits are required.
        """
        candidate = _write_loop(tmp_path, monkeypatch)
        handoff = _load_handoff(tmp_path)
        handoff["tables"][0]["rows"][0]["cells"][1:8] = ["0"] * 7
        result = read_json(candidate)
        result["metadata"]["challenge_table_contract_version"] = 2
        historical = tmp_path / "result.json"
        _bind_handoff(tmp_path, historical, result, handoff)
        _dump(candidate, result)

        assert read_json(historical)["metadata"]["challenge_table_contract_version"] == 2
        _assert_rejected(tmp_path, historical, "adversarial-loop-table-contract-version-required")
        _assert_rejected(tmp_path, candidate, "adversarial-loop-table-contract-version-required")


class TestChunkedCoordinator:
    """Chunked scope: coordinators are validated through their declared children, never a parent review."""

    def test_single_scope_cannot_claim_child_scores(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Reject a digest-bound child score list when no chunk checker owns it.

        The scope is `single`, yet the handoff lists child runs with scores; nothing validates those scores, so the
        validator must refuse to render them as evidence.
        """
        candidate = _write_loop(tmp_path, monkeypatch)
        handoff = _load_handoff(tmp_path)
        _set_chunk_only(
            handoff, [{"result_path": "chunks/runs/001/result.json", "scores": [6, 0], "decision": "clean"}]
        )
        _bind_handoff(tmp_path, candidate, read_json(candidate), handoff)

        _assert_rejected(tmp_path, candidate, "adversarial-loop-child-scores-scope-invalid")

    def test_candidate_rechecks_coverage_before_promotion(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reject a clean parent candidate when its declared chunk coverage is invalid.

        The manifest and results files exist but describe no chunks, so a clean-looking parent cannot be promoted.
        """
        result_path = _write_loop(tmp_path, monkeypatch)
        chunks = tmp_path / "chunks"
        chunks.mkdir()
        (chunks / "chunks.json").write_text("{}", encoding="utf-8")
        (chunks / "results.json").write_text('{"schema_version":1,"chunks":[]}', encoding="utf-8")
        result = read_json(result_path)
        result["metadata"]["challenge_scope"] = _chunked_scope()
        _dump(result_path, result)

        _assert_rejected(tmp_path, result_path, "adversarial-loop-chunk-coverage-invalid")

    def test_rejects_unanchored_coverage_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A candidate cannot redirect chunk evidence outside its declared run.

        A manifest path that climbs out of the run directory with `..` is not anchored to the run.
        """
        result_path = _write_loop(tmp_path, monkeypatch)
        result = read_json(result_path)
        result["metadata"]["challenge_scope"] = {
            "mode": "chunked",
            "manifest_path": "../chunks/chunks.json",
            "results_path": "chunks/results.json",
        }
        _dump(result_path, result)

        _assert_rejected(tmp_path, result_path, "adversarial-loop-challenge-scope-invalid")

    def test_promoted_result_rejects_old_table_contract(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A promoted result cannot use the historical table contract as current evidence.

        A chunked result at table contract 2 with unsplit severity totals is promoted to `result.json`, where current
        evidence requires the split columns.
        """
        candidate = _write_loop(tmp_path, monkeypatch)
        chunks = tmp_path / "chunks"
        chunks.mkdir()
        _dump(chunks / "chunks.json", {"schema_version": 1, "origin": FIRST_RUN_ORIGIN})
        (chunks / "results.json").write_text("current", encoding="utf-8")
        result = read_json(candidate)
        result["metadata"]["challenge_scope"] = _chunked_scope()
        result["metadata"]["challenge_table_contract_version"] = 2
        handoff = _load_handoff(tmp_path)
        handoff["tables"][0]["rows"][0]["cells"][1:8] = ["0"] * 7
        promoted = tmp_path / "result.json"
        _bind_handoff(tmp_path, promoted, result, handoff)

        _assert_rejected(tmp_path, promoted, "adversarial-loop-table-contract-version-required")

    def test_candidate_cannot_call_chunk_artifacts_single_scope(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Visible chunk artifacts prevent relabeling a chunked candidate as single scope.

        A `chunks/chunks.json` exists beside a result that declares `single` scope.
        """
        result_path = _write_loop(tmp_path, monkeypatch)
        chunks = tmp_path / "chunks"
        chunks.mkdir()
        (chunks / "chunks.json").write_text("{}", encoding="utf-8")

        _assert_rejected(tmp_path, result_path, "adversarial-loop-challenge-scope-mismatch")

    @pytest.mark.parametrize(
        ("table_version", "table_patch", "row_patch"),
        [
            pytest.param(1, {}, {}, id="table-contract-1"),
            pytest.param(2, {"layout": "challenge-history"}, {"admission": "unavailable"}, id="table-contract-2"),
        ],
    )
    def test_reports_children_without_a_combined_score(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        table_version: int,
        table_patch: dict[str, Any],
        row_patch: dict[str, Any],
    ) -> None:
        """Validate a stopped child-backed coordinator without inventing a parent review.

        The coordinator has a failed review gate, no parent ledger and one clean child summary. It validates, then stops
        validating when the handoff scores, the recorded summary or the presentation version disagree with the checker's
        summary. The phases share one built state, so they run in one test per table contract.
        """
        candidate = _write_loop(tmp_path, monkeypatch)
        chunks = tmp_path / "chunks"
        chunks.mkdir()
        _dump(chunks / "chunks.json", {"schema_version": 1, "origin": FIRST_RUN_ORIGIN})
        (chunks / "results.json").write_text("results", encoding="utf-8")
        summary = {
            "schema_version": 1,
            "manifest_sha256": "a" * 64,
            "results_sha256": "b" * 64,
            "runs": [{"result_path": "chunks/runs/001/result.json", "scores": [6, 0], "decision": "clean"}],
        }
        gates = _fail_review_gate(tmp_path)
        handoff = _load_handoff(tmp_path)
        _set_chunk_only(handoff, copy.deepcopy(summary["runs"]))
        _set_chunk_review_outcome(handoff, "Child reviews passed; overall review is incomplete.")
        handoff["tables"][0].update(table_patch)
        handoff["tables"][0]["rows"][0].update(row_patch)
        handoff["verification"][-1] = {"check": "review", "status": "fail", "evidence": gates["checks"][-1]["stdout"]}
        _record_remaining(
            handoff, "Full scope has no independent review.", "Seek a full scope review when capacity permits."
        )
        result = _chunked_result(candidate, summary, FIRST_RUN_ORIGIN)
        result.update(status="fail", checks_failed=["review"])
        result["metadata"]["challenge_table_contract_version"] = table_version
        _bind_handoff(tmp_path, candidate, result, handoff)
        _drop_parent_ledger(tmp_path)
        monkeypatch.setattr(load_shared_validator().subprocess, "run", _chunk_diff_summary_stub(summary))

        _validate(tmp_path, candidate)

        handoff["child_runs"][0]["scores"] = [0]
        _bind_handoff(tmp_path, candidate, result, handoff)
        _assert_rejected(tmp_path, candidate, "adversarial-loop-child-scores-mismatch")

        handoff["child_runs"] = copy.deepcopy(summary["runs"])
        result["metadata"]["chunked_review"]["runs"][0]["scores"] = [0]
        _bind_handoff(tmp_path, candidate, result, handoff)
        _assert_rejected(tmp_path, candidate, "adversarial-loop-chunk-summary-mismatch")

        result["metadata"]["chunked_review"] = copy.deepcopy(summary)
        handoff["presentation_version"] = 2
        del handoff["child_runs"]
        _bind_handoff(tmp_path, candidate, result, handoff)
        _assert_rejected(tmp_path, candidate, "current-handoff-presentation-v8-required")
        _dump(tmp_path / "result.json", result)
        _assert_rejected(tmp_path, tmp_path / "result.json", "adversarial-loop-child-scores-presentation-required")

    @pytest.fixture
    def clean_chunk_coordinator(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        """Stub chunk-scope validation with a complete clean child summary and return the validator inputs."""
        validator = load_shared_validator()
        summary = {
            "schema_version": 1,
            "manifest_sha256": "a" * 64,
            "results_sha256": "b" * 64,
            "runs": [{"result_path": "chunks/runs/001/result.json", "scores": [0], "decision": "clean"}],
        }
        handoff = {
            "presentation_version": 8,
            "child_runs": summary["runs"],
            "tables": [{"rows": [{"cells": ["not-run", *(["N/A"] * 7), "chunk-only", "chunks/results.json"]}]}],
            "remaining": [],
            "next_steps": [],
        }
        monkeypatch.setattr(validator, "_validate_challenge_scope", lambda *_, **__: summary)
        monkeypatch.setattr(
            validator,
            "_load_json",
            lambda path: json.loads(path.read_text()) if path.name == "chunks.json" else handoff,
        )
        return {
            "validator": validator,
            "result": {
                "status": "pass",
                "checks_failed": [],
                "findings": {"critical": 0, "high": 0, "medium": 0, "low": 0},
                "metadata": {"chunked_review": summary},
            },
            "gates": {"checks": [{"id": "review", "status": "pass"}]},
        }

    def test_complete_clean_chunk_coverage_permits_success(
        self, tmp_path: Path, clean_chunk_coordinator: dict[str, Any]
    ) -> None:
        """A complete clean child summary must have a truthful coordinator success form.

        With every child clean and the handoff showing only the child run, the coordinator may pass without a combined
        parent score.
        """
        fixture = clean_chunk_coordinator

        fixture["validator"]._validate_chunked_coordinator(
            fixture["result"], tmp_path, fixture["gates"], candidate=True
        )

    def test_current_chunk_manifest_requires_origin_contract(
        self, tmp_path: Path, clean_chunk_coordinator: dict[str, Any]
    ) -> None:
        """A current coordinator is refused when its manifest lacks the origin contract.

        The on-disk manifest declares schema 3 with prior findings but no origin, which a current result must carry.
        """
        fixture = clean_chunk_coordinator
        chunks = tmp_path / "chunks"
        chunks.mkdir()
        (chunks / "chunks.json").write_text('{"schema_version":3,"prior_findings":{"ledger_path":null}}')

        with pytest.raises(SystemExit, match="adversarial-loop-chunk-origin-contract-required"):
            fixture["validator"]._validate_chunked_coordinator(
                fixture["result"], tmp_path, fixture["gates"], candidate=True, current_result=True
            )

    @pytest.mark.packaging
    def test_validates_real_children_and_interaction(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A coordinator accepts complete native child reviews and rejects changed child evidence.

        Real `chunk_diff.py` plans three chunks and checks four native child reviews. The coordinator validates, then is
        refused when the interaction assessment, origin, manifest schema, a child gate, child request or child source is
        altered. It is one test because the phases tamper with and restore the same ~20s real setup.
        """
        review = _build_real_chunked_review(tmp_path, monkeypatch)
        chunks, manifest, summary = review.chunks, review.manifest, review.summary
        coordinator = _write_loop(tmp_path, monkeypatch)
        handoff = _load_handoff(tmp_path)
        _set_chunk_only(handoff, summary["runs"])
        _set_chunk_review_outcome(handoff, "All declared child and interaction reviews are clean.")
        result = _chunked_result(coordinator, summary, manifest["origin"])
        _bind_handoff(tmp_path, coordinator, result, handoff)
        _drop_parent_ledger(tmp_path)
        monkeypatch.setenv("CODEX_HOME", str(review.session_home))
        canonical = tmp_path / "result.json"
        _validate(tmp_path, coordinator)
        canonical.write_bytes(coordinator.read_bytes())
        _validate(tmp_path, canonical)

        _assert_interaction_assessment_enforced(tmp_path, review, canonical)
        _assert_origin_mismatch_rejected(tmp_path, [coordinator, canonical], result)
        _assert_archive_manifests_rejected(tmp_path, chunks, canonical)

        _fail_child_tests_gate(chunks / "runs" / manifest["chunks"][0]["id"])
        _dump(
            chunks / "results.json",
            review.result_map | {"chunks": [review.result_map["chunks"][0]], "interactions": [], "groups": []},
        )
        stopped = _run_checker(
            "check-stopped",
            "--manifest",
            str(chunks / "chunks.json"),
            "--results",
            str(chunks / "results.json"),
            "--summary",
        )
        assert stopped.returncode == 0, stopped.stderr
        stopped_summary = json.loads(stopped.stdout)
        assert stopped_summary["status"] == "stopped"
        assert stopped_summary["pending_chunks"] == [chunk["id"] for chunk in manifest["chunks"][1:]]
        assert stopped_summary["pending_interactions"] == [
            item["chunk_ids"] for item in review.result_map["interactions"]
        ]
        assert stopped_summary["pending_groups"] == []

        gates = _fail_review_gate(tmp_path)
        handoff = _load_handoff(tmp_path)
        _set_chunk_only(handoff, stopped_summary["runs"])
        _set_chunk_review_outcome(handoff, "Child review stopped; full review remains open.")
        handoff["verification"][-1] = {"check": "review", "status": "fail", "evidence": gates["checks"][-1]["stdout"]}
        _record_remaining(handoff, "Full scope has no independent review.", "Seek a full scope review.")
        result = _chunked_result(coordinator, stopped_summary, manifest["origin"])
        result.update(status="fail", checks_failed=["review"])
        _bind_handoff(tmp_path, coordinator, result, handoff)
        rendered = (tmp_path / "final.md").read_text(encoding="utf-8")
        assert all(
            f"{run['result_path']}: {' → '.join(map(str, run['scores']))}" in rendered
            for run in stopped_summary["runs"]
        )
        assert "combined score" not in rendered.casefold()
        _validate(tmp_path, coordinator)

        _assert_child_tampering_rejected(tmp_path, review, coordinator)

    @pytest.mark.packaging
    def test_recovery_requires_prior_signature_in_real_review(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reject a clean recovery when its authenticated current reviewer omits an earlier finding.

        A caller run left one open high finding. The chunk that verifies it fixed passes the real checker; a chunk whose
        review omits the signature is reported unresolved, shows as pending when stopped, and a downgraded manifest is
        refused.
        """
        repository = tmp_path / "repository"
        _init_repository(repository, {"alpha.py": "VALUE = 1\n"})
        finding = {
            "signature": "caller-origin",
            "tier": "high",
            "structural": False,
            "disposition": "open",
            "evidence": ["Caller accepts the invalid value."],
        }
        snapshot = {"revision": "earlier", "diff_digest": "0" * 64}
        prior = {
            "schema_version": 1,
            "implementation_author": "parent-thread",
            "current_snapshot": snapshot,
            "rounds": [
                {
                    "index": 1,
                    "reviewer": {"identity": "earlier-child", "independent": True},
                    "snapshot": snapshot,
                    "report_path": "review-1.md",
                    "findings": [finding],
                }
            ],
        }
        prior_path = tmp_path / "caller-run" / "loop-ledger.json"
        prior_path.parent.mkdir()
        _dump(prior_path, prior)
        coverage_path = tmp_path / "coverage.json"
        _dump(
            coverage_path,
            [
                {
                    "signature": "caller-origin",
                    "source_paths": ["alpha.py"],
                    "review_kind": "chunk",
                    "chunk_ids": ["chunk-001"],
                }
            ],
        )
        chunks = tmp_path / "chunks"
        planned = _run_checker(
            "plan",
            "--repository",
            str(repository),
            "--out",
            str(chunks),
            "--scope-mode",
            "all",
            "--goal",
            "Review caller behavior",
            "--specification",
            "Invalid values must be rejected",
            "--done-when",
            "Independent review resolves every prior finding",
            "--prior-ledger",
            str(prior_path),
            "--prior-coverage",
            str(coverage_path),
            "--caller-run",
            str(prior_path.parent),
        )
        assert planned.returncode == 0, planned.stderr
        manifest = read_json(chunks / "chunks.json")
        chunk = manifest["chunks"][0]
        request = manifest["request"]
        assert "Caller accepts the invalid value." in request["specification"]
        source, diff = _planned_inputs(chunks, chunk)
        resolved = finding | {
            "disposition": "verified-fixed",
            "evidence": ["Caller now rejects the original invalid value."],
        }
        _write_bound_child(
            chunks / "runs" / "chunk-001",
            monkeypatch,
            repository,
            chunk["scope_paths"],
            (source, diff),
            request,
            "recovery",
            findings=[resolved],
        )
        result_map = {
            "schema_version": 1,
            "chunks": [{"id": "chunk-001", "result_path": "runs/chunk-001/result.json"}],
            "interactions": [],
            "groups": [],
        }
        _dump(chunks / "results.json", result_map)
        checked = _run_check(chunks, "check")
        assert checked.returncode == 0, checked.stderr

        omitted_run = chunks / "runs" / "omitted-finding"
        _write_bound_child(
            omitted_run, monkeypatch, repository, chunk["scope_paths"], (source, diff), request, "omitted"
        )
        result_map["chunks"][0]["result_path"] = "runs/omitted-finding/result.json"
        _dump(chunks / "results.json", result_map)
        assert "prior-finding-not-resolved:caller-origin" in _run_check(chunks, "check").stderr

        _fail_review_gate(omitted_run)
        omitted_handoff = _load_handoff(omitted_run)
        omitted_handoff["verification"][-1]["status"] = "fail"
        _record_remaining(
            omitted_handoff,
            "Prior finding remains unresolved.",
            "Resolve the prior finding in a fresh independent review.",
        )
        omitted_result = read_json(omitted_run / "result.json")
        omitted_result.update(status="fail", checks_failed=["review"])
        _bind_handoff(omitted_run, omitted_run / "result.json", omitted_result, omitted_handoff)
        stopped = _run_check(chunks, "check-stopped", "--summary")
        assert stopped.returncode == 0, stopped.stderr
        assert json.loads(stopped.stdout)["pending_prior_signatures"] == ["caller-origin"]

        result_map["chunks"][0]["result_path"] = "runs/chunk-001/result.json"
        _dump(chunks / "results.json", result_map)
        manifest.pop("prior_findings")
        manifest.pop("origin")
        manifest["schema_version"] = 2
        _dump(chunks / "chunks.json", manifest)
        assert "chunk-manifest-invalid" in _run_check(chunks, "check").stderr


def _forge_reviewer(run: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Record an invented reviewer identity in the ledger."""
    ledger_path = run / "loop-ledger.json"
    ledger = _read_ledger(ledger_path)
    ledger["rounds"][0]["reviewer"]["identity"] = "invented-reviewer"
    _write_ledger(ledger_path, ledger)


def _change_source(run: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Edit the reviewed source file after the review."""
    (Path(read_json(run / "loop-evidence.json")["repository"]) / "widget.py").write_text(
        "VALUE = 2\n", encoding="utf-8"
    )


def _remove_provenance(run: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Delete the specialist manifest that proves who reviewed."""
    (run / "review" / "specialist-manifest.json").unlink()


def _replace_owner_thread(run: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run validation from a different parent thread than the one that owns the review receipts."""
    monkeypatch.setenv("CODEX_THREAD_ID", "another-parent-thread")


def _drop_review_report(run: Path, result_path: Path) -> None:
    """Delete the local review report the ledger points at."""
    (run / "review-1.md").unlink()


def _claim_clean_without_rounds(run: Path, result_path: Path) -> None:
    """Empty the ledger rounds while the result still claims a clean review."""
    ledger_path = run / "loop-ledger.json"
    ledger = _read_ledger(ledger_path)
    ledger["rounds"] = []
    _write_ledger(ledger_path, ledger)


def _escape_report_path(run: Path, result_path: Path) -> None:
    """Point the ledger's report path outside the run directory."""
    ledger_path = run / "loop-ledger.json"
    ledger = _read_ledger(ledger_path)
    ledger["rounds"][0]["report_path"] = "../review-1.md"
    _write_ledger(ledger_path, ledger)


def _change_table_decision(run: Path, result_path: Path) -> None:
    """Show a decision in the handoff table that the ledger does not support."""
    handoff = _load_handoff(run)
    handoff["tables"][0]["rows"][0]["cells"][8] = "converging"
    _bind_handoff(run, result_path, read_json(result_path), handoff)


class TestLoopClosure:
    """A loop is only closed by a bound ledger, local reports, truthful gates and matching visible decisions."""

    @pytest.mark.parametrize("column_index", [1, 2, 3, 4, 5, 6])
    def test_rejects_wrong_ranked_severity_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, column_index: int
    ) -> None:
        """Reject a displayed severity count that disagrees with the reviewed ledger.

        The ledger is clean, so a handoff row claiming `1` in any one severity column contradicts it.
        """
        result_path = _write_loop(tmp_path, monkeypatch)
        handoff = _load_handoff(tmp_path)
        handoff["tables"][0]["rows"][0]["cells"][column_index] = "1"
        _bind_handoff(tmp_path, result_path, read_json(result_path), handoff)

        _assert_rejected(tmp_path, result_path, "adversarial-loop-table-mismatch")

    def test_rows_show_carried_and_new_findings_in_each_round(self, tmp_path: Path) -> None:
        """Keep newly found defects visible even when a later round scores worse.

        Round two keeps three high findings from round one and adds a fourth, so its row must read `3 + 1`; showing a
        plain `4` hides the new defect and is refused.
        """
        first = [{"signature": name, "tier": "high", "disposition": "open"} for name in ("first", "second", "third")]
        ledger = {
            "rounds": [
                {"report_path": "review-1.json", "findings": first},
                {
                    "report_path": "review-2.json",
                    "findings": [*first, {"signature": "fourth", "tier": "high", "disposition": "open"}],
                },
            ]
        }
        empty = {tier: 0 for tier in ("security", "critical", "high", "medium", "low", "nit")}
        summary = {
            "reason": "nonconverging",
            "rounds": [
                {"index": 1, "counts": {**empty, "high": 3}, "score": 18, "decision": "baseline"},
                {"index": 2, "counts": {**empty, "high": 4}, "score": 24, "decision": "nonconverging"},
            ],
        }
        zero = ["0 + 0"] * 6
        handoff = {
            "tables": [
                {
                    "rows": [
                        {"cells": ["1", *zero[:2], "0 + 3", *zero[3:], "0 + 18", "baseline", "review-1.json"]},
                        {"cells": ["2", *zero[:2], "3 + 1", *zero[3:], "18 + 6", "nonconverging", "review-2.json"]},
                    ]
                }
            ],
            "remaining": ["four open findings"],
            "next_steps": ["resolve them"],
        }
        _dump(tmp_path / "final-handoff.json", handoff)
        validator = load_shared_validator()
        validator._validate_adversarial_loop_rows(tmp_path, ledger, summary, table_version=1)
        handoff["tables"][0]["rows"][1]["cells"][3] = "4"
        _dump(tmp_path / "final-handoff.json", handoff)

        with pytest.raises(SystemExit, match="adversarial-loop-table-mismatch"):
            validator._validate_adversarial_loop_rows(tmp_path, ledger, summary, table_version=1)

    def test_accepts_verified_structural_carryover(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Keep independently verified structural closure visible in a separately approved correction run.

        A structural high finding verified fixed in this review is closed, so the clean loop validates and the ledger
        keeps the finding exactly as reviewed.
        """
        findings = [
            {
                "signature": "contract-change",
                "tier": "high",
                "structural": True,
                "disposition": "verified-fixed",
                "evidence": ["The separately approved contract correction is independently verified in this review."],
            }
        ]
        result_path = _write_loop(tmp_path, monkeypatch, findings=findings)

        _validate(tmp_path, result_path)
        assert _read_ledger(tmp_path / "loop-ledger.json")["rounds"][0]["findings"] == findings

    def test_scored_review_remains_valid_with_failed_review_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Keep a scored review visible when its review gate stops completion.

        The review gate fails after a clean scored round, so the result is a failure that still carries its score.
        """
        result_path = _write_loop(tmp_path, monkeypatch)
        _fail_review_gate(tmp_path)
        handoff = _load_handoff(tmp_path)
        handoff["verification"][-1]["status"] = "fail"
        _record_remaining(handoff, "Independent review has stopped.", "Resolve the review gate before completion.")
        result = read_json(result_path)
        result.update(status="fail", checks_failed=["review"])
        _bind_handoff(tmp_path, result_path, result, handoff)

        _validate(tmp_path, result_path)
        assert result["metadata"]["adversarial_loop"]["scores"] == [0]

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(_forge_reviewer, id="forged-reviewer"),
            pytest.param(_change_source, id="changed-source"),
            pytest.param(_remove_provenance, id="missing-provenance"),
            pytest.param(_replace_owner_thread, id="active-owner"),
        ],
    )
    def test_rejects_unbound_review_evidence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutate: Callable[[Path, pytest.MonkeyPatch], None]
    ) -> None:
        """Reject clean-looking handoffs when actual source or independent provenance is invalid.

        The result looks clean, but the reviewer identity is invented, the reviewed source changed, the specialist
        manifest is gone, or the running thread is the reviewer's own parent: none may be accepted.
        """
        result_path = _write_loop(tmp_path, monkeypatch)
        mutate(tmp_path, monkeypatch)

        _assert_rejected(tmp_path, result_path, "adversarial-loop-evidence-invalid:")

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(_drop_review_report, id="drop-review"),
            pytest.param(_claim_clean_without_rounds, id="claim-clean"),
            pytest.param(_escape_report_path, id="escape-report"),
            pytest.param(_change_table_decision, id="change-table"),
        ],
    )
    def test_rejects_false_loop_closure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutate: Callable[[Path, Path], None]
    ) -> None:
        """Keep actual ledger, local reports, and visible decisions bound to completion.

        A deleted review report, a ledger with no rounds, a report path escaping the run, or a table decision that
        disagrees with the ledger each break the binding between the claimed closure and its evidence.
        """
        result_path = _write_loop(tmp_path, monkeypatch)
        mutate(tmp_path, result_path)

        _assert_rejected(tmp_path, result_path, "adversarial-loop-")

    def test_accepts_stopped_loop_with_failed_review_and_folded_findings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Keep an authority-blocked structural finding and folded result counts visible.

        A structural flag alone leaves the score active; the parent still fails acceptance when the required contract-
        change authority is absent, without losing security and nit findings in common result counts.
        """
        findings = [
            {
                "signature": "shared-contract-boundary",
                "tier": "security",
                "structural": True,
                "disposition": "open",
                "evidence": [
                    "review-1.md identifies a shared contract change.",
                    "invariant: shared-contract-boundary holds",
                ],
            },
            {
                "signature": "spelling",
                "tier": "nit",
                "structural": False,
                "disposition": "open",
                "evidence": ["review-1.md identifies a spelling correction.", "invariant: spelling holds"],
            },
        ]
        result_path = _write_loop(tmp_path, monkeypatch, findings=findings)
        _fail_review_gate(tmp_path)
        result = read_json(result_path)
        result.update(
            status="fail",
            checks_failed=["review"],
            findings={"critical": 1, "high": 0, "medium": 0, "low": 1},
            metadata={
                **result["metadata"],
                "adversarial_loop": {
                    "status": "active",
                    "reason": "baseline",
                    "scores": [21],
                    "rounds": [
                        {
                            "index": 1,
                            "score": 21,
                            "counts": {"security": 1, "critical": 0, "high": 0, "medium": 0, "low": 0, "nit": 1},
                            "decision": "baseline",
                        }
                    ],
                },
            },
        )
        handoff = _load_handoff(tmp_path)
        handoff["outcome"] = {
            "title": "Parser review",
            "summary": "Parser review cannot fix the shared contract without approval.",
        }
        handoff["tables"][0]["rows"][0]["cells"] = [
            "1",
            "0 + 1",
            "0 + 0",
            "0 + 0",
            "0 + 0",
            "0 + 0",
            "0 + 1",
            "0 + 21",
            "baseline",
            "review-1.md",
        ]
        handoff["verification"][-1] = {"check": "review", "status": "fail", "evidence": "review.stdout.txt"}
        _record_remaining(
            handoff,
            "Shared contract boundary remains open.",
            "Approve a separately scoped contract change or reject the finding with evidence.",
        )
        _bind_handoff(tmp_path, result_path, result, handoff)

        _validate(tmp_path, result_path)

    def test_rejects_nonclean_loop_hidden_as_pass(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Reject an accepted result when its real review gate recorded a failure.

        Prevents a lifecycle owner from hiding a failed convergence review by clearing the result failure list while
        retaining a passing status.
        """
        result_path = _write_loop(tmp_path, monkeypatch)
        _fail_review_gate(tmp_path)
        result = read_json(result_path)
        result.update(status="pass", checks_failed=[])
        handoff = _load_handoff(tmp_path)
        handoff["verification"][-1] = {"check": "review", "status": "fail", "evidence": "review.stdout.txt"}
        _bind_handoff(tmp_path, result_path, result, handoff)

        _assert_rejected(tmp_path, result_path, "result-checks-failed-gate-mismatch")

    @pytest.mark.parametrize(
        ("review_status", "exit_code", "checks_failed", "outcome"),
        [
            pytest.param("fail", 1, ["review"], nullcontext(), id="failed-review-gate"),
            pytest.param(
                "pass",
                0,
                [],
                pytest.raises(SystemExit, match="adversarial-loop-nonclean-review-gate"),
                id="passing-review-gate",
            ),
        ],
    )
    def test_unavailable_loop_needs_not_run_row(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        review_status: str,
        exit_code: int,
        checks_failed: list[str],
        outcome: Any,
    ) -> None:
        """Preserve unavailable independent review as a failed handoff without fake clean coverage.

        Prevents an empty ledger from being presented with a clean iteration row instead of the contract-required not-
        run recovery decision. A passing review gate beside no review is refused; the rendered handoff shows only the
        not-run row either way.
        """
        result_path = _write_loop(tmp_path, monkeypatch)
        ledger = _read_ledger(tmp_path / "loop-ledger.json")
        ledger["rounds"] = []
        _write_ledger(tmp_path / "loop-ledger.json", ledger)
        _dump(tmp_path / "loop-actions.json", {"schema_version": 3, "rounds": []})
        evidence = read_json(tmp_path / "loop-evidence.json")
        evidence["rounds"] = []
        _dump(tmp_path / "loop-evidence.json", evidence)
        monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
        _set_review_gate(tmp_path, review_status, exit_code, checks_failed)
        result = read_json(result_path)
        result.update(
            status="fail",
            checks_failed=checks_failed,
            metadata={
                **result["metadata"],
                "adversarial_loop": {
                    "status": "stopped",
                    "reason": "independence-unavailable",
                    "scores": [],
                    "rounds": [],
                },
            },
        )
        handoff = _load_handoff(tmp_path)
        handoff["outcome"] = {
            "title": "Parser review",
            "summary": "Parser review stopped because independent review was unavailable.",
        }
        handoff["tables"][0]["rows"][0].update(
            id="not-run", cells=["not-run", *(["N/A"] * 7), "independence-unavailable", "loop-report.md"]
        )
        handoff["verification"][-1] = {"check": "review", "status": review_status, "evidence": "review.stdout.txt"}
        _record_remaining(
            handoff,
            "Independent review was unavailable.",
            "Obtain an allowed independent review route before claiming closure.",
            row_id="not-run",
        )
        _bind_handoff(tmp_path, result_path, result, handoff)

        with outcome:
            _validate(tmp_path, result_path)
        rendered = (tmp_path / "final.md").read_text(encoding="utf-8")
        assert "independence-unavailable" in rendered
        assert "- not-run: loop-report.md" in rendered
        assert "| 1 |" not in rendered


def _chunk_diff_summary_stub(summary: dict[str, Any]) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Build a `subprocess.run` stand-in that returns the validated child summary for `chunk_diff.py` only."""
    real_run = subprocess.run

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Supply the child summary from the checker boundary; run every other command for real."""
        if Path(command[1]).name == "chunk_diff.py":
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(summary), stderr="")
        return real_run(command, **kwargs)

    return run


def _init_repository(repository: Path, files: dict[str, str]) -> None:
    """Create a Git repository holding `files` in one initial commit."""
    repository.mkdir()
    for name, text in files.items():
        (repository / name).write_text(text, encoding="utf-8", newline="\n")
    for arguments in (
        ("init", "-q"),
        ("add", "."),
        ("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "initial"),
    ):
        subprocess.run(["git", *arguments], cwd=repository, capture_output=True, check=True)


def _run_checker(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the real chunk checker with the given arguments."""
    return subprocess.run([sys.executable, str(CHUNK_CHECKER), *arguments], capture_output=True, text=True, check=False)


def _run_check(chunks: Path, command: str, *extra: str) -> subprocess.CompletedProcess[str]:
    """Run a chunk checker command against the manifest and results of `chunks`."""
    manifest = read_json(chunks / "chunks.json")
    return _run_checker(
        command,
        "--manifest",
        str(chunks / "chunks.json"),
        "--results",
        str(chunks / "results.json"),
        "--repository",
        manifest["repository"],
        *extra,
    )


def _planned_inputs(chunks: Path, chunk: dict[str, Any]) -> tuple[bytes, bytes]:
    """Read the frozen source and diff the planner wrote for one chunk."""
    return (chunks / chunk["source_path"]).read_bytes(), (chunks / chunk["diff_path"]).read_bytes()


def _write_bound_child(
    run: Path,
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    paths: list[str],
    inputs: tuple[bytes, bytes],
    request: dict[str, str],
    identity: str,
    findings: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Create a clean native-reviewed child run bound to its planned source and diff, and return its evidence."""
    run.mkdir(parents=True)
    evidence: dict[str, object] = {}
    candidate = _write_loop(run, monkeypatch, findings=findings, evidence_out=evidence)
    _bind_real_chunk_run(run, evidence, repository, paths, *inputs, request, identity, findings=findings)
    (run / "result.json").write_bytes(candidate.read_bytes())
    return evidence


@dataclass(frozen=True)
class _ChunkedReview:
    """A planned three-chunk review with four complete native child runs and its checker summary."""

    repository: Path
    chunks: Path
    manifest: dict[str, Any]
    request: dict[str, str]
    result_map: dict[str, Any]
    session_home: Path
    summary: dict[str, Any]


def _build_real_chunked_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _ChunkedReview:
    """Plan three chunks with the real checker, review each plus their interactions, and check the whole set."""
    repository = tmp_path / "repository"
    _init_repository(repository, {name: f"VALUE = '{name}'\n" for name in ("alpha.py", "beta.py", "gamma.py")})
    monkeypatch.chdir(repository)
    chunks = tmp_path / "chunks"
    request = {
        "goal": "Review all three files",
        "specification": "All three files retain their values",
        "done_when": "Each child and their interactions are independently clean",
    }
    planned = _run_checker(
        "plan",
        "--repository",
        str(repository),
        "--out",
        str(chunks),
        "--scope-mode",
        "all",
        "--budget-bytes",
        "1",
        "--goal",
        request["goal"],
        "--specification",
        request["specification"],
        "--done-when",
        request["done_when"],
    )
    assert planned.returncode == 0, planned.stderr
    manifest = read_json(chunks / "chunks.json")
    assert len(manifest["chunks"]) == 3
    session_home = tmp_path / "session-home"
    sessions = session_home / "sessions"
    sessions.mkdir(parents=True)
    interactions = [
        {
            "chunk_ids": [left["id"], right["id"]],
            "decision": "reviewed",
            "evidence": f"{left['id']} and {right['id']} jointly define the fixture behavior",
            "result_path": "runs/interaction/result.json",
        }
        for left, right in itertools.combinations(manifest["chunks"], 2)
    ]
    groups = [
        {
            "chunk_ids": [chunk["id"] for chunk in manifest["chunks"]],
            "evidence": "All three chunks jointly define the fixture contract",
            "result_path": "runs/interaction/result.json",
        }
    ]
    result_map = {"schema_version": 1, "chunks": [], "interactions": interactions, "groups": groups}
    for index, chunk in enumerate(manifest["chunks"], 1):
        result_map["chunks"].append({"id": chunk["id"], "result_path": f"runs/{chunk['id']}/result.json"})
        evidence = _write_bound_child(
            chunks / "runs" / chunk["id"],
            monkeypatch,
            repository,
            chunk["scope_paths"],
            _planned_inputs(chunks, chunk),
            request,
            f"chunk-{index}",
        )
        _collect_chunk_sessions(evidence, sessions)

    decisions = [{key: item[key] for key in ("chunk_ids", "decision", "evidence")} for item in interactions] + [
        {"chunk_ids": group["chunk_ids"], "decision": "reviewed", "evidence": group["evidence"]} for group in groups
    ]
    assessment = "Interaction assessment:\n" + json.dumps(decisions, sort_keys=True, ensure_ascii=True)
    interaction_request = request | {"specification": request["specification"] + "\n" + assessment}
    chunker_source = _load_loop_validator().capture_source_snapshot(repository, manifest["inventory"])
    source = (json.dumps(chunker_source, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode()
    evidence = _write_bound_child(
        chunks / "runs" / "interaction",
        monkeypatch,
        repository,
        manifest["inventory"],
        (source, b""),
        interaction_request,
        "interaction",
    )
    _collect_chunk_sessions(evidence, sessions)
    _dump(chunks / "results.json", result_map)
    monkeypatch.setenv("CODEX_HOME", str(session_home))
    checked = _run_check(chunks, "check", "--summary")
    assert checked.returncode == 0, checked.stderr
    summary = json.loads(checked.stdout)
    assert len(summary["runs"]) == 4
    return _ChunkedReview(repository, chunks, manifest, request, result_map, session_home, summary)


def _assert_interaction_assessment_enforced(run: Path, review: _ChunkedReview, canonical: Path) -> None:
    """Substitute a group conclusion in the results map and assert the coordinator no longer validates, then restore."""
    results_path = review.chunks / "results.json"
    original_results = results_path.read_bytes()
    original_canonical = canonical.read_bytes()
    substituted = copy.deepcopy(review.result_map)
    substituted["groups"][0]["evidence"] = "Substituted group conclusion"
    _dump(results_path, substituted)
    tampered = json.loads(original_canonical)
    tampered["metadata"]["chunked_review"]["results_sha256"] = hashlib.sha256(results_path.read_bytes()).hexdigest()
    _dump(canonical, tampered)
    _assert_rejected(run, canonical, r"adversarial-loop-chunk-coverage-invalid:.*chunk-interaction-assessment-missing")
    canonical.write_bytes(original_canonical)
    results_path.write_bytes(original_results)


def _assert_origin_mismatch_rejected(run: Path, result_paths: list[Path], result: dict[str, Any]) -> None:
    """Relabel each result as a continuation of the run itself and assert refusal, restoring `result` afterwards."""
    for result_path in result_paths:
        tampered = read_json(result_path)
        tampered["metadata"]["challenge_origin"] = {"kind": "continuation", "caller_run": str(run)}
        _dump(result_path, tampered)
        _assert_rejected(run, result_path, "adversarial-loop-chunk-origin-mismatch")
        _dump(result_path, result)


def _assert_archive_manifests_rejected(run: Path, chunks: Path, canonical: Path) -> None:
    """Downgrade the manifest to archive schemas 3 and 4, asserting both checker and validator refuse each."""
    manifest_path = chunks / "chunks.json"
    original = manifest_path.read_bytes()
    for archive_version in (3, 4):
        _dump(manifest_path, json.loads(original) | {"schema_version": archive_version})
        archived = _run_checker("check", "--manifest", str(manifest_path))
        assert archived.returncode != 0
        assert "chunk-manifest-invalid" in archived.stderr
        _assert_rejected(
            run, canonical, "adversarial-loop-chunk-coverage-invalid:chunk-diff-error:chunk-manifest-invalid"
        )
    manifest_path.write_bytes(original)


def _fail_child_tests_gate(failed_run: Path) -> None:
    """Make a child run independently clean but with a failed test gate, with its handoff and result rebound."""
    gates = read_json(failed_run / "gates.json")
    next(check for check in gates["checks"] if check["id"] == "tests").update(status="fail", exit_code=1)
    gates.update(status="fail", checks_failed=["tests"])
    _dump(failed_run / "gates.json", gates)
    handoff = _load_handoff(failed_run)
    next(check for check in handoff["verification"] if check["check"] == "tests")["status"] = "fail"
    handoff["outcome"]["summary"] = "Independent review is clean; test gate failed."
    result = read_json(failed_run / "result.json")
    result.update(status="fail", checks_failed=["tests"])
    _bind_handoff(failed_run, failed_run / "result.json", result, handoff)


def _assert_child_tampering_rejected(run: Path, review: _ChunkedReview, coordinator: Path) -> None:
    """Replace a child's request goal, then its frozen source, asserting the coordinator is refused after each."""
    first = review.manifest["chunks"][0]
    child_evidence_path = review.chunks / "runs" / first["id"] / "loop-evidence.json"
    child_evidence = read_json(child_evidence_path)
    child_evidence["request"]["goal"] = "Unreviewed replacement goal"
    _dump(child_evidence_path, child_evidence)
    _assert_rejected(run, coordinator, "adversarial-loop-chunk-coverage-invalid")
    child_evidence["request"]["goal"] = review.request["goal"]
    _dump(child_evidence_path, child_evidence)
    child_source = review.chunks / first["source_path"]
    child_source.write_bytes(child_source.read_bytes() + b" ")
    _assert_rejected(run, coordinator, "adversarial-loop-chunk-coverage-invalid")


def _bind_real_chunk_run(
    run: Path,
    evidence: dict[str, object],
    repository: Path,
    paths: list[str],
    source: bytes,
    diff: bytes,
    request: dict[str, str],
    identity: str,
    findings: list[dict[str, object]] | None = None,
) -> None:
    """Bind a complete native review fixture to one planned child or interaction."""
    fixture = evidence["fixture"]
    assert isinstance(fixture, dict)
    review = run / "review"
    (review / "diff.patch").write_bytes(diff)
    for record in (fixture["manifest"], fixture["plan"]):
        record["review_input_sha256"] = hashlib.sha256(diff).hexdigest()
    _rewrite_native_outputs(fixture, source, diff, findings or [], full=True, request=request, coverage_paths=paths)
    selected = fixture["manifest"]["passes"][0]["attempts"][0]
    (run / "review-1.md").write_bytes((review / selected["output_path"]).read_bytes())
    for name in ("current-source.json", "source-1.json"):
        (run / name).write_bytes(source)
    for name in ("current.diff", "round-1.diff"):
        (run / name).write_bytes(diff)
    snapshot = json.loads(source)
    ledger_path = run / "loop-ledger.json"
    ledger = _read_ledger(ledger_path)
    ledger["current_snapshot"] = {"revision": snapshot["revision"], "diff_digest": hashlib.sha256(diff).hexdigest()}
    ledger["rounds"][0]["snapshot"] = ledger["current_snapshot"]
    _write_ledger(ledger_path, ledger)
    loop_path = run / "loop-evidence.json"
    loop = json.loads(loop_path.read_text(encoding="utf-8"))
    loop.update(repository=repository.as_posix(), scope_paths=paths, request=request)
    loop_path.write_text(json.dumps(loop), encoding="utf-8")

    # Rollout fixture identities must be unique in the shared native session store.
    home = evidence["codex_home"]
    assert isinstance(home, Path)
    for file in [*review.rglob("*.json"), *home.rglob("*.jsonl")]:
        content = file.read_text(encoding="utf-8")
        for old in ("child-1", "child-2", "spawn-1", "spawn-2"):
            content = content.replace(old, f"{identity}-{old}")
        file.write_text(content, encoding="utf-8", newline="\n")
    for file in (home / "sessions").glob("rollout-*.jsonl"):
        file.rename(file.with_name(file.name.replace("child-", f"{identity}-child-")))
    manifest_path = review / "specialist-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["inspection_execution"]["plan_sha256"] = hashlib.sha256(
        (review / "inspection-plan.json").read_bytes()
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    ledger = _read_ledger(ledger_path)
    ledger["rounds"][0]["reviewer"]["identity"] = f"{identity}-child-2"
    _write_ledger(ledger_path, ledger)


def _collect_chunk_sessions(evidence: dict[str, object], sessions: Path) -> None:
    """Keep one active parent rollout containing each independent child spawn."""
    home = evidence["codex_home"]
    assert isinstance(home, Path)
    for file in (home / "sessions").glob("*.jsonl"):
        target = sessions / file.name
        if file.name == "rollout-parent-thread.jsonl" and target.exists():
            rows = file.read_text(encoding="utf-8").splitlines(keepends=True)
            with target.open("a", encoding="utf-8", newline="\n") as stream:
                stream.writelines(rows[1:])
        else:
            shutil.copy2(file, target)

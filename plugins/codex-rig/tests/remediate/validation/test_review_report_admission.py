"""Review report admission for remediation: clean claims are rejected while source-bound incomplete intake is kept."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from _modules import load_module
from test_candidate_validation_contract import _candidate
from test_code_remediate_final_outcome_validation import _metadata, _status_counts, _write_action_items
from test_code_remediate_work_bucket_validation import _parallel_metadata, _write_workplan
from test_review_completion_gate import _assessed_pr
from test_review_remediation_handoff import (
    test_native_assembly_finalization_and_separate_intake_preserve_proof as build_native_review_evidence,
)

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
USER_BODY = "Please add the missing boundary guard."
REVIEW_BODY = "Use the existing review evidence for these fixes."
OBLIGATION_NOTICE = "not a confirmed defect or automatic edit request"
PENDING = pytest.param(None, id="pending")


VALIDATOR = load_module("remediation_admission_validator", PLUGIN_ROOT / "shared/validate-artifacts.py")
FINALIZER = load_module("user_finding_finalizer", PLUGIN_ROOT / "shared/final_handoff.py")


def _validate_intake(result: dict, directory: Path) -> None:
    """Run the current-contract report intake validation over a remediation result."""
    VALIDATOR._validate_code_remediate_report_intake(result, directory, current_contract=True)


def _run(*args: str | Path, **kwargs: object) -> subprocess.CompletedProcess[str]:
    """Run a command without raising on a non-zero exit, capturing text output."""
    return subprocess.run([str(arg) for arg in args], capture_output=True, text=True, check=False, **kwargs)


def _file_bytes(*roots: Path) -> dict[Path, bytes]:
    """Snapshot the bytes of every file under the given roots to prove a validation left them untouched."""
    return {path: path.read_bytes() for root in roots for path in root.rglob("*") if path.is_file()}


def _sha256(data: bytes) -> str:
    """Return the hex SHA-256 of ``data``."""
    return hashlib.sha256(data).hexdigest()


def _direct_origin_report(disposition: str | None) -> dict:
    """Build the current direct producer's list relations with two distinct online originals for one candidate."""
    candidate = _candidate(disposition)
    candidate.update(
        id="C_INPUT",
        claim="The empty input bypasses the guard.",
        owner="input maintainer",
        next_check="Run the empty-input regression on the native backend.",
        closure_condition="Both empty-input paths reject unchecked values.",
        missing_prerequisite="Native backend unavailable." if disposition == "unverified" else "",
        feasible_checks_complete=disposition is not None,
    )
    origins = [
        {
            "id": f"review-comment-{index}",
            "parent_id": f"review-thread-{index}",
            "path": "widget.py",
            "line": index + 6,
            "body": body,
            "artifact": "review-threads.json",
            "candidate_id": candidate["id"],
            "disposition": disposition,
        }
        for index, body in enumerate(("Guard the empty input.", "Guard the repeated empty input."), 1)
    ]
    return {
        "schema_version": 3,
        "artifact_path": "review/result.json",
        "checks_failed": [],
        "follow_up": [],
        "metadata": {
            "review_status": None,
            "review_decision": {},
            "confidence_gaps": [],
            "confidence_recovery": {},
            "review_validation": {"schema_version": 1, "candidates": [candidate]},
            "source_findings": origins,
            "source_finding_mapping": [
                {"source_id": origin["id"], "candidate_id": candidate["id"]} for origin in origins
            ],
        },
    }


def _keep_origins(metadata: dict) -> None:
    """Leave the supplemental source relations as built."""


def _drop_origin_keys(metadata: dict) -> None:
    """Remove the supplemental source relations entirely."""
    del metadata["source_findings"], metadata["source_finding_mapping"]


def _empty_origins(metadata: dict) -> None:
    """Replace the supplemental source relations with empty lists."""
    metadata.update(source_findings=[], source_finding_mapping=[])


def _lose_relation(metadata: dict) -> None:
    """Drop the relation of the last original to its candidate."""
    metadata["source_finding_mapping"].pop()


def _contradict_relation(metadata: dict) -> None:
    """Map an original to a candidate that does not exist."""
    metadata["source_finding_mapping"].append({"source_id": "review-comment-1", "candidate_id": "C_OTHER"})


def _lose_origin(metadata: dict) -> None:
    """Drop the last original while its relation remains."""
    metadata["source_findings"].pop()


class TestDirectOriginObligations:
    """Obligation sources built from a direct producer report's candidates and their original online claims."""

    @pytest.mark.parametrize("disposition", [PENDING, "unverified"])
    @pytest.mark.parametrize(
        ("supplemental", "origin_count"),
        [
            pytest.param(_keep_origins, 2, id="two-origins"),
            pytest.param(_drop_origin_keys, 0, id="absent"),
            pytest.param(_empty_origins, 0, id="empty"),
        ],
    )
    def test_open_candidate_keeps_all_source_context(self, disposition, supplemental, origin_count: int):
        """A pending or unverified concern keeps every original online claim without becoming a confirmed fix.

        The report carries two originals for one candidate, or none when the relations are absent or empty; the single
        obligation must embed the candidate and exactly the originals present, and must not change the report.
        """
        report = _direct_origin_report(disposition)
        metadata = report["metadata"]
        candidate = metadata["review_validation"]["candidates"][0]
        origins = metadata["source_findings"][:origin_count]
        supplemental(metadata)
        before = json.dumps(report, sort_keys=True)

        sources = VALIDATOR.report_obligation_sources(report)

        assert len(sources) == 1
        source = sources[0]
        assert source["source_id"] == "review/result.json#/metadata/review_validation/candidates/0"
        assert source["location"] == source["source_id"]
        assert source["evidence"] == "findings-input.txt#/metadata/review_validation/candidates/0"
        assert OBLIGATION_NOTICE in source["body"]
        assert json.dumps(candidate, sort_keys=True) in source["body"]
        assert all(json.dumps(origin, sort_keys=True) in source["body"] for origin in origins)
        assert json.dumps(report, sort_keys=True) == before

    @pytest.mark.parametrize("disposition", ["confirmed", "refuted", "already-fixed", "optional-out-of-scope"])
    def test_closed_candidates_create_no_context_obligations(self, disposition: str):
        """Completed direct reports are accepted and resolved concerns are not recreated as remediation work.

        A closed disposition means the review already settled the concern, so no obligation source is produced.
        """
        assert VALIDATOR.report_obligation_sources(_direct_origin_report(disposition)) == []

    @pytest.mark.parametrize(
        "damage",
        [
            pytest.param(_lose_relation, id="missing-relation"),
            pytest.param(_contradict_relation, id="conflicting-relation"),
            pytest.param(_lose_origin, id="missing-origin"),
        ],
    )
    def test_open_obligation_rejects_lost_source_relations(self, damage: Callable[[dict], None]):
        """An open concern fails deliberately when a relation loses or contradicts an original claim.

        Silently accepting a report whose originals no longer map to the candidate would drop reviewer evidence.
        """
        report = _direct_origin_report("unverified")
        damage(report["metadata"])

        with pytest.raises(SystemExit):
            VALIDATOR.report_obligation_sources(report)

    @pytest.mark.parametrize("disposition", [PENDING, "unverified"])
    def test_batched_open_obligation_keeps_each_original(self, disposition: str | None):
        """Existing batched source obligations stay intact when direct producer relations are accepted.

        The two direct originals are rewritten into the batched `finding_id` form, and the one obligation must still
        embed each batched original verbatim.
        """
        report = _direct_origin_report(disposition)
        metadata = report["metadata"]
        origins = [
            {
                "finding_id": f"source.sw-engineer.origin-{index}",
                "original": {"id": f"origin-{index}", "summary": origin["body"]},
                "original_text": json.dumps(origin),
                "manifest_path": "source/specialist-manifest.json",
                "manifest_sha256": "a" * 64,
                "output_sha256": "b" * 64,
                "pass": {"role": "sw-engineer", "output_path": "specialists/engineer.md"},
                "disposition": "unresolved",
            }
            for index, origin in enumerate(metadata["source_findings"], 1)
        ]
        metadata.update(
            source_findings=origins, source_finding_mapping={origin["finding_id"]: ["C_INPUT"] for origin in origins}
        )

        sources = VALIDATOR.report_obligation_sources(report)

        assert len(sources) == 1
        assert sources[0]["source_id"] == "review/result.json#/metadata/review_validation/candidates/0"
        assert all(json.dumps(origin, sort_keys=True) in sources[0]["body"] for origin in origins)


@pytest.fixture
def completed_report(tmp_path: Path) -> tuple[dict, Path, Path]:
    """Retain exact producer bytes and a lossless consumer inventory of its obligations.

    Returns the remediation result, the consumer directory and the real producer directory.
    """
    producer = _assessed_pr.__wrapped__(tmp_path)
    report_path = producer / "result.json"
    report = json.loads(report_path.read_bytes())
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    (consumer / "findings-input.txt").write_bytes(report_path.read_bytes())
    (consumer / "action-items.md").write_text("## Review Report Intake\n", encoding="utf-8")
    (consumer / "resolution-scope.md").write_text("Retain review obligations.\n", encoding="utf-8")
    metadata = {
        "mode": "report",
        "resolution_scope": {"presentation_version": 4},
        "final_resolution_table": {
            "items": [
                {
                    "input_item_id": "G1",
                    "item_type": "confidence-gap",
                    "selectable": True,
                    "sources": VALIDATOR.report_obligation_sources(report),
                }
            ]
        },
        "review_report_intake": {
            "obligation_records_version": 1,
            "schema_version": 1,
            "admission_status": "completed",
            "requested_report": True,
            "report_items_total": 1,
            "review_gate_items_total": 1,
            "review_gate_items_selectable": 1,
            "report_items_marked_out_of_scope": 0,
        },
    }
    return {"status": "fail", "metadata": metadata}, consumer, producer


def _unpromote(producer: Path, consumer: Path) -> None:
    """Leave the producer result as an unpromoted candidate file."""
    (producer / "result.json").rename(producer / "result.candidate.json")


def _forge_proof(producer: Path, consumer: Path) -> None:
    """Overwrite the producer's final-handoff validation proof with an empty object."""
    (producer / "final-handoff.validation.json").write_text("{}", encoding="utf-8")


def _drift_source(producer: Path, consumer: Path) -> None:
    """Change the producer's source diff after the review."""
    (producer / "diff.patch").write_bytes(b"changed source\n")


def _alter_copied_bytes(producer: Path, consumer: Path) -> None:
    """Make the consumer's copy of the report differ from the producer's bytes."""
    (consumer / "findings-input.txt").write_bytes((producer / "result.json").read_bytes() + b"\n")


class TestCompletedIntake:
    """Completed review intake against a real, validated producer directory."""

    @pytest.mark.integration
    def test_admitted_direct_report_reaches_obligation_cli_and_context_coverage(
        self, completed_report: tuple[dict, Path, Path]
    ):
        """Unchanged bytes already admitted by the real producer gates are not rejected by the obligation consumer.

        A refuted direct candidate with two originals is written into the producer result; the real finder admits it,
        the real obligations CLI builds the context, and the consumer validators accept it without touching the
        producer.
        """
        result, consumer, producer = completed_report
        producer_path = producer / "result.json"
        report = json.loads(producer_path.read_bytes())
        direct = _direct_origin_report("refuted")["metadata"]
        routing = json.loads((producer / "pr-routing.json").read_text(encoding="utf-8"))
        direct["review_validation"]["candidates"][0]["source_revision"] = "head:" + routing["head_oid"]
        report["metadata"].update(
            source_findings=direct["source_findings"], source_finding_mapping=direct["source_finding_mapping"]
        )
        report["metadata"]["review_validation"]["candidates"] = direct["review_validation"]["candidates"]
        producer_path.write_text(json.dumps(report), encoding="utf-8", newline="\n")
        copied = consumer / "findings-input.txt"
        copied.write_bytes(producer_path.read_bytes())
        before = _file_bytes(producer)
        expected_context = result["metadata"]["final_resolution_table"]["items"][0]["sources"]

        admitted = _run(sys.executable, PLUGIN_ROOT / "shared/find-review-report.py", "--result", producer_path)
        assert admitted.returncode == 0, admitted.stderr
        assert admitted.stdout.strip() == str(producer_path)
        obligations = _run(
            sys.executable,
            PLUGIN_ROOT / "shared/remediation_finalize.py",
            "obligations",
            "--run",
            consumer,
            "--producer-result-path",
            producer_path,
        )

        assert obligations.returncode == 0, obligations.stdout + obligations.stderr
        context = json.loads((consumer / "report-obligations.json").read_bytes())
        assert context == {"schema_version": 1, "sources": expected_context}
        metadata = {
            "final_resolution_table": {"items": []},
            "review_report_intake": {"obligation_records_version": 2, "context_sources": context["sources"]},
        }
        VALIDATOR._validate_code_remediate_report_coverage(metadata, consumer)
        _validate_intake(result, consumer)
        assert copied.read_bytes() == producer_path.read_bytes()
        assert _file_bytes(producer) == before

    @pytest.mark.integration
    @pytest.mark.parametrize(
        "damage",
        [
            pytest.param(_unpromote, id="unpromoted"),
            pytest.param(_forge_proof, id="forged-proof"),
            pytest.param(_drift_source, id="source-drift"),
            pytest.param(_alter_copied_bytes, id="copied-bytes"),
        ],
    )
    def test_rejects_unvalidated_producer(
        self, completed_report: tuple[dict, Path, Path], damage: Callable[[Path, Path], None]
    ):
        """A mirrored inventory cannot turn a candidate, forged proof, stale source or altered copy into completion.

        Each mutator damages the real producer or the consumer's copy of its bytes after a valid admission.
        """
        result, consumer, producer = completed_report
        damage(producer, consumer)

        with pytest.raises(SystemExit, match="code-remediate-report-producer-"):
            _validate_intake(result, consumer)

    @pytest.mark.integration
    def test_admits_validated_immutable_producer(self, completed_report: tuple[dict, Path, Path]):
        """Promoted producer bytes are revalidated without changing either producer or consumer artifacts.

        The artifact path is the absolute producer path, as the real producer wrote it.
        """
        result, consumer, producer = completed_report
        before = _file_bytes(producer, consumer)

        _validate_intake(result, consumer)

        assert _file_bytes(producer, consumer) == before

    @pytest.mark.integration
    def test_admits_producer_relative_artifact_with_recorded_origin(self, completed_report: tuple[dict, Path, Path]):
        """A producer-relative artifact path is revalidated against the recorded producer result path.

        The result and its final handoff are rewritten to the relative filename and re-rendered by the real finalizer;
        the intake then records where the producer result lives, so validation can find it without guessing.
        """
        result, consumer, producer = completed_report
        report_path = producer / "result.json"
        report = json.loads(report_path.read_bytes())
        report["artifact_path"] = "result.json"
        handoff_path = producer / "final-handoff.json"
        handoff = json.loads(handoff_path.read_bytes())
        handoff["artifacts"][0]["path"] = "result.json"
        handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
        proof = FINALIZER.render_files(handoff_path, producer / "final.md", producer / "final-handoff.validation.json")
        report["metadata"]["final_handoff"].update(
            handoff_sha256=proof["handoff_sha256"], rendered_sha256=proof["rendered_sha256"]
        )
        report_path.write_text(json.dumps(report), encoding="utf-8")
        (consumer / "findings-input.txt").write_bytes(report_path.read_bytes())
        result["metadata"]["review_report_intake"]["admission_evidence"] = {"producer_result_path": str(report_path)}
        before = _file_bytes(producer, consumer)

        _validate_intake(result, consumer)

        assert _file_bytes(producer, consumer) == before

    def test_requires_origin_for_producer_relative_artifact(self, completed_report: tuple[dict, Path, Path]):
        """A copied producer-relative filename is rejected rather than resolved against a guessed directory.

        With no recorded producer result path, a relative `artifact_path` has no origin to resolve from.
        """
        result, consumer, _producer = completed_report
        copied = consumer / "findings-input.txt"
        report = json.loads(copied.read_bytes())
        report["artifact_path"] = "result.json"
        copied.write_text(json.dumps(report), encoding="utf-8")

        with pytest.raises(SystemExit, match="code-remediate-report-producer-origin-missing"):
            _validate_intake(result, consumer)


@pytest.fixture
def native_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict, Path, Path, Path]:
    """Reuse real native assembly and finalization evidence for a later consumer session.

    Returns the remediation result, the consumer directory, the producer review directory and the producer Codex home.
    The environment then describes an unrelated consumer thread whose Codex home is the consumer directory.
    """
    producer_root = tmp_path / "producer"
    producer_root.mkdir()
    build_native_review_evidence(producer_root, "none")
    producer = producer_root / "review"
    home = producer_root / "codex-home"
    manifest = json.loads((producer / "specialist-manifest.json").read_bytes())
    report = json.loads((producer / "result.json").read_bytes())
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    (consumer / "findings-input.txt").write_bytes((producer / "result.json").read_bytes())
    (consumer / "action-items.md").write_text("## Review Report Intake\n", encoding="utf-8")
    (consumer / "resolution-scope.md").write_text("Retain native proof obligations.\n", encoding="utf-8")
    metadata = {
        "mode": "report",
        "resolution_scope": {"presentation_version": 4},
        "final_resolution_table": {
            "items": [
                {
                    "input_item_id": "G1",
                    "item_type": "confidence-gap",
                    "selectable": True,
                    "sources": VALIDATOR.report_obligation_sources(report),
                }
            ]
        },
        "review_report_intake": {
            "obligation_records_version": 1,
            "schema_version": 1,
            "admission_status": "completed",
            "requested_report": True,
            "report_items_total": 1,
            "review_gate_items_total": 1,
            "review_gate_items_selectable": 1,
            "report_items_marked_out_of_scope": 0,
            "admission_evidence": {
                "producer_result_path": str(producer / "result.json"),
                "producer_codex_home": str(home),
                "producer_parent_thread_id": manifest["parent_thread_id"],
            },
        },
    }
    monkeypatch.setenv("CODEX_THREAD_ID", "unrelated-consumer")
    monkeypatch.setenv("CODEX_HOME", str(consumer))
    return {"status": "fail", "metadata": metadata}, consumer, producer, home


def _keep_thread_override(evidence: dict, consumer: Path, home: Path) -> None:
    """Keep the explicit producer thread id in the admission evidence."""


def _drop_thread_override(evidence: dict, consumer: Path, home: Path) -> None:
    """Remove the explicit producer thread id so it must be recovered from the manifest."""
    evidence.pop("producer_parent_thread_id")


def _wrong_home(evidence: dict, consumer: Path, home: Path) -> None:
    """Point the producer Codex home at the consumer's home."""
    evidence["producer_codex_home"] = str(consumer)


def _wrong_thread(evidence: dict, consumer: Path, home: Path) -> None:
    """Claim a producer thread id that is not the producer's."""
    evidence["producer_parent_thread_id"] = "unrelated-producer"


def _malformed_proof(evidence: dict, consumer: Path, home: Path) -> None:
    """Append unrequested execution text to the second recorded custom tool call of a child rollout."""
    child, rows = next(
        (path, records)
        for path in home.rglob("*.jsonl")
        if (records := [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()])
        and any(record.get("payload", {}).get("type") == "custom_tool_call" for record in records)
    )
    calls = [row for row in rows if row.get("payload", {}).get("type") == "custom_tool_call"]
    assert len(calls) >= 2
    calls[1]["payload"]["input"] += "\nUnrequested execution."
    child.write_bytes(("\n".join(json.dumps(row) for row in rows) + "\n").encode("utf-8"))


class TestNativeProducerIntake:
    """Intake of real native review proof by a later consumer session with different thread and home."""

    @pytest.mark.integration
    def test_local_finder_recovers_producer_thread_in_later_session(self, native_report: tuple[dict, Path, Path, Path]):
        """The local finder admits the result using the manifest's producer thread, not the consumer's thread.

        The environment names an unrelated consumer thread and home; the finder is given the producer Codex home.
        """
        _result, _consumer, producer, home = native_report

        completed = _run(
            sys.executable, PLUGIN_ROOT / "shared/find-review-report.py", "--result", producer / "result.json",
            "--codex-home", home,
        )  # fmt: skip

        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.strip() == str(producer / "result.json")

    @pytest.mark.integration
    @pytest.mark.parametrize(
        "adjust",
        [
            pytest.param(_keep_thread_override, id="with-thread-override"),
            pytest.param(_drop_thread_override, id="thread-from-manifest"),
        ],
    )
    def test_preserves_producer_evidence_location(
        self, native_report: tuple[dict, Path, Path, Path], adjust: Callable[[dict, Path, Path], None]
    ):
        """A later consumer admits real native proof at its original coordinates without rewriting artifacts.

        The explicit producer thread id is either kept or removed (then taken from the manifest); neither changes a byte
        of the producer, its Codex home or the consumer directory.
        """
        result, consumer, producer, home = native_report
        adjust(result["metadata"]["review_report_intake"]["admission_evidence"], consumer, home)
        before = _file_bytes(producer, home, consumer)

        _validate_intake(result, consumer)

        assert _file_bytes(producer, home, consumer) == before

    @pytest.mark.integration
    @pytest.mark.parametrize(
        "damage",
        [
            pytest.param(_wrong_home, id="wrong-home"),
            pytest.param(_wrong_thread, id="wrong-thread"),
            pytest.param(_malformed_proof, id="malformed-proof"),
        ],
    )
    def test_rejects_wrong_producer_coordinates(
        self, native_report: tuple[dict, Path, Path, Path], damage: Callable[[dict, Path, Path], None]
    ):
        """Explicit evidence coordinates never weaken thread, rollout-home or proof validation.

        A wrong home, a wrong thread or a tampered rollout proof is rejected, and the rejection leaves every artifact
        byte-identical.
        """
        result, consumer, producer, home = native_report
        damage(result["metadata"]["review_report_intake"]["admission_evidence"], consumer, home)
        before = _file_bytes(producer, home, consumer)

        with pytest.raises(SystemExit, match="code-remediate-report-producer-validation-failed"):
            _validate_intake(result, consumer)

        assert _file_bytes(producer, home, consumer) == before


@pytest.fixture
def admission(tmp_path: Path) -> tuple[dict, Path]:
    """Retain a real lookup diagnostic, source identity, and frozen two-item inventory."""
    pr = tmp_path / "pr"
    pr.mkdir()
    identity = {"pr_url": "https://github.com/example/project/pull/12", "head_oid": "a" * 40, "base_oid": "b" * 40}
    (pr / "pr-routing.json").write_text(json.dumps(identity), encoding="utf-8")
    (pr / "diff.patch").write_bytes(b"verified source diff\n")
    diagnostic = b"matching-review-incomplete:retained-run\n"
    (tmp_path / "review-lookup.txt").write_bytes(diagnostic)
    selection = b'{"selected_indexes": [1], "items": ["fix", "missing-review"]}\n'
    (tmp_path / "selection.json").write_bytes(selection)
    items = [
        {
            "input_item_id": "R1",
            "item_type": "code",
            "sources": [{"kind": "online"}],
            "selectable": True,
            "triage_status": "valid",
            "resolution_status": "implemented",
            "owner_status": "fixed",
        },
        {
            "input_item_id": "R2",
            "item_type": "review-gate",
            "sources": [{"kind": "online"}],
            "selectable": True,
            "triage_status": "valid",
            "resolution_status": "unresolved",
            "owner_status": "not-selected",
        },
    ]
    evidence = {
        **identity,
        "diagnostic_path": "review-lookup.txt",
        "diagnostic_sha256": _sha256(diagnostic),
        "selection_sha256": _sha256(selection),
        "open_item_id": "R2",
    }
    intake = {
        "schema_version": 1,
        "requested_report": True,
        "admission_status": "unavailable",
        "admission_evidence": evidence,
        "report_items_total": 0,
        "review_gate_items_total": 0,
        "review_gate_items_selectable": 0,
        "report_items_marked_out_of_scope": 0,
    }
    metadata = {
        "mode": "pr",
        "resolution_scope": {"presentation_version": 4, "selected_indexes": [1]},
        "final_resolution_table": {"items": items},
        "review_report_intake": intake,
    }
    return {"status": "fail", "metadata": metadata}, tmp_path


def _remove_open_item(items: list[dict]) -> None:
    """Drop the open review-gate item from the inventory."""
    items.pop()


def _close_open_item(items: list[dict]) -> None:
    """Mark the open review-gate item resolved."""
    items[1].update(resolution_status="resolved", owner_status="resolved")


def _retype_open_item(items: list[dict]) -> None:
    """Relabel the open review-gate item as ordinary code work."""
    items[1]["item_type"] = "code"


@pytest.fixture
def declined_review(admission: tuple[dict, Path]) -> tuple[dict, Path]:
    """Build an admission where the user declined the extra review and a limitation replaces the open item."""
    result, directory = admission
    metadata = result["metadata"]
    intake = metadata["review_report_intake"]
    intake["schema_version"] = 2
    evidence = intake["admission_evidence"]
    evidence.pop("open_item_id")
    evidence["limitation"] = "(-0.03) Completed review unavailable; additional review declined."
    metadata["confidence_gaps"] = [evidence["limitation"]]
    metadata["final_resolution_table"]["items"].pop()
    result["status"] = "pass"
    return result, directory


class TestUnavailableAdmission:
    """Admission of a requested review that is unavailable: the evidence gap stays open and visible."""

    def test_missing_review_keeps_fixed_work_and_unselected_evidence_gap(self, admission: tuple[dict, Path]):
        """Reviewer bookkeeping need not be selected before the actual fix is reported.

        The fix is implemented while the missing-review gate stays unselected; the intake is accepted as it stands.
        """
        result, directory = admission

        _validate_intake(result, directory)

        assert result["metadata"]["final_resolution_table"]["items"][0]["resolution_status"] == "implemented"
        assert result["metadata"]["review_report_intake"]["requested_report"] is True

    def test_declined_review_discloses_limitation_without_synthetic_finding(self, declined_review: tuple[dict, Path]):
        """A declined extra review leaves its evidence limitation, not an unresolved fix task.

        The limitation is recorded as a confidence gap and the result may pass without an open review-gate item.
        """
        result, directory = declined_review

        _validate_intake(result, directory)

    def test_declined_review_requires_limitation_as_confidence_gap(self, declined_review: tuple[dict, Path]):
        """Removing the limitation from the confidence gaps is rejected.

        A declined review with no recorded limitation would silently hide the missing evidence.
        """
        result, directory = declined_review
        result["metadata"]["confidence_gaps"] = []

        with pytest.raises(SystemExit, match="report-admission-limitation-missing"):
            _validate_intake(result, directory)

    @pytest.mark.parametrize("status", ["pass", "timeout"])
    def test_noncompleted_admission_never_certifies_clean_result(self, admission: tuple[dict, Path], status: str):
        """A missing requested review is retained only in an explicitly failed remediation.

        Neither a `pass` nor a `timeout` result may claim a clean outcome for an unavailable review.
        """
        result, directory = admission
        result["status"] = status

        with pytest.raises(SystemExit, match="code-remediate-report-admission-requires-fail"):
            _validate_intake(result, directory)

    @pytest.mark.parametrize(
        "change",
        [
            pytest.param(_remove_open_item, id="missing"),
            pytest.param(_close_open_item, id="closed"),
            pytest.param(_retype_open_item, id="wrong-kind"),
        ],
    )
    def test_rejects_missing_or_closed_review_obligation(
        self, admission: tuple[dict, Path], change: Callable[[list[dict]], None]
    ):
        """A diagnostic cannot replace an honest open obligation in the final inventory.

        The open review-gate item is removed, closed or retyped, and the admission must name the missing obligation.
        """
        result, directory = admission
        change(result["metadata"]["final_resolution_table"]["items"])

        with pytest.raises(SystemExit, match="code-remediate-report-admission-open-obligation-missing"):
            _validate_intake(result, directory)

    @pytest.mark.parametrize("field", ["pr_url", "head_oid", "base_oid", "selection_sha256", "diagnostic_sha256"])
    def test_cannot_rebind_source_or_evidence(self, admission: tuple[dict, Path], field: str):
        """A diagnostic reused for a different PR, source, selection or byte stream is rejected.

        Each identity field of the admission evidence is changed in turn and must break the binding.
        """
        result, directory = admission
        result["metadata"]["review_report_intake"]["admission_evidence"][field] = "changed"

        with pytest.raises(SystemExit, match="code-remediate-report-admission-evidence-mismatch"):
            _validate_intake(result, directory)

    def test_cannot_hide_report_origin_findings(self, admission: tuple[dict, Path]):
        """No-report admission must not relabel preliminary report records as verified intake.

        An item whose source kind is `report` contradicts an `unavailable` admission.
        """
        result, directory = admission
        result["metadata"]["final_resolution_table"]["items"][0]["sources"][0]["kind"] = "report"
        result["metadata"]["review_report_intake"]["report_items_total"] = 1

        with pytest.raises(SystemExit, match="code-remediate-report-admission-unavailable-report-sources"):
            _validate_intake(result, directory)

    def test_omitted_status_keeps_completed_report_contract(self, admission: tuple[dict, Path]):
        """Backward compatibility does not silently downgrade an absent completed input.

        With `admission_status` omitted the intake falls back to the completed-report contract and its input is missing.
        """
        result, directory = admission
        del result["metadata"]["review_report_intake"]["admission_status"]

        with pytest.raises(SystemExit, match="code-remediate-report-input-missing"):
            _validate_intake(result, directory)

    @pytest.mark.parametrize("version", [0, 3, True])
    def test_rejects_unknown_schema_versions(self, admission: tuple[dict, Path], version: object):
        """Version skipping and boolean aliases are rejected in the admission schema family.

        Only the declared versions are valid; `True` must not pass as version 1.
        """
        result, directory = admission
        result["metadata"]["review_report_intake"]["schema_version"] = version

        with pytest.raises(SystemExit, match="code-remediate-report-admission-schema-invalid"):
            _validate_intake(result, directory)


@pytest.fixture
def preliminary(admission: tuple[dict, Path]) -> tuple[dict, Path]:
    """Add the unchanged original structured finding and its exact PR identity to an admission."""
    result, directory = admission
    intake = result["metadata"]["review_report_intake"]
    intake.update(admission_status="preliminary", report_items_total=1)
    record = {
        "id": "F1",
        "severity": "high",
        "title": "Missing guard",
        "summary": "Input is unchecked.",
        "required_change": "Validate the input.",
        "closure_evidence": "Boundary regression passes.",
        "evidence": ["src/guard.py:12"],
        "authors": ["Main reviewer"],
    }
    original = {
        "schema_version": 3,
        "metadata": {
            "scope": "pr",
            "review_findings": [record],
            "review_input_sha256": _sha256((directory / "pr/diff.patch").read_bytes()),
        },
    }
    original_bytes = json.dumps(original).encode("utf-8")
    (directory / "findings-input.txt").write_bytes(original_bytes)
    evidence = intake["admission_evidence"]
    pr_bytes = json.dumps(
        {"url": evidence["pr_url"], "headRefOid": evidence["head_oid"], "baseRefOid": evidence["base_oid"]}
    ).encode("utf-8")
    (directory / "original-pr.json").write_bytes(pr_bytes)
    evidence.update(
        original_pr_path="original-pr.json",
        original_pr_sha256=_sha256(pr_bytes),
        original_input_sha256=_sha256(original_bytes),
    )
    item = result["metadata"]["final_resolution_table"]["items"][0]
    item["sources"] = [
        {
            "kind": "report",
            "source_id": "original.json#F1",
            "finding_id": "F1",
            "body": "Missing guard Input is unchecked. Validate the input. Boundary regression passes. src/guard.py:12",
        }
    ]
    (directory / "action-items.md").write_text("## Review Report Intake\n", encoding="utf-8")
    (directory / "resolution-scope.md").write_text("Selected source fix only.\n", encoding="utf-8")
    return result, directory


class TestPreliminaryAdmission:
    """Admission of a preliminary (incomplete) review whose original structured finding is retained."""

    def test_records_are_source_bound_and_lossless(self, preliminary: tuple[dict, Path]):
        """A retained candidate supplies hypotheses without certifying review completion.

        The original finding is carried verbatim into the source body and the result stays `fail`.
        """
        result, directory = preliminary

        _validate_intake(result, directory)

        assert result["status"] == "fail"
        assert result["metadata"]["review_report_intake"]["admission_status"] == "preliminary"

    def test_record_cannot_omit_resolution_contract(self, preliminary: tuple[dict, Path]):
        """An ID match alone must not erase the original closure requirements.

        The source body is cut to the title, dropping the summary the original finding carries.
        """
        result, directory = preliminary
        result["metadata"]["final_resolution_table"]["items"][0]["sources"][0]["body"] = "Missing guard"

        with pytest.raises(SystemExit, match="code-remediate-report-finding-detail-omitted:F1:summary"):
            _validate_intake(result, directory)

    def test_record_cannot_reuse_old_diff(self, preliminary: tuple[dict, Path]):
        """A matching PR number cannot hide a different source patch.

        The retained diff bytes change after the review hashed them.
        """
        result, directory = preliminary
        (directory / "pr/diff.patch").write_bytes(b"different source\n")

        with pytest.raises(SystemExit, match="code-remediate-report-admission-original-source-invalid"):
            _validate_intake(result, directory)

    def test_requires_enriched_canonical_records(self, preliminary: tuple[dict, Path]):
        """Unstructured or incomplete notes are not turned into canonical findings.

        The original finding loses its `required_change`, and its input hash is updated to match the edited bytes.
        """
        result, directory = preliminary
        path = directory / "findings-input.txt"
        original = json.loads(path.read_bytes())
        del original["metadata"]["review_findings"][0]["required_change"]
        changed = json.dumps(original).encode("utf-8")
        path.write_bytes(changed)
        result["metadata"]["review_report_intake"]["admission_evidence"]["original_input_sha256"] = _sha256(changed)

        with pytest.raises(SystemExit, match="code-remediate-report-admission-canonical-records-required"):
            _validate_intake(result, directory)


@dataclass
class UserFix:
    """A remediation result whose supplied user fix is implemented and whose requested review stays open."""

    result: dict
    directory: Path
    metadata: dict
    table: dict
    inventory: dict
    scope: dict
    first: dict
    second: dict
    pack: Path | None = None
    snapshot_bytes: bytes = b""


def _user_source(identity: str, body: str) -> list[dict]:
    """Build the single user-kind source of an inventory item."""
    return [
        {
            "kind": "user",
            "source_id": identity,
            "location": "src/guard.py:12",
            "body": body,
            "evidence": "user-findings.md",
        }
    ]


def _user_fix_base(admission: tuple[dict, Path]) -> UserFix:
    """Build the two-item inventory (implemented user fix, open review gate) and its selection inventory."""
    result, directory = admission
    metadata = _metadata()
    metadata.update(result["metadata"])
    table = _metadata()["final_resolution_table"]
    metadata["final_resolution_table"] = table
    (directory / "user-findings.md").write_text(USER_BODY + "\n" + REVIEW_BODY, encoding="utf-8")
    first, second = table["items"]
    first.update(
        resolved_how="Implemented: Added the missing guard.",
        summary=USER_BODY,
        closure_evidence="Boundary regression passes.",
        resolution_proposal="Add the boundary guard.",
    )
    second.update(
        item_name="Missing requested review",
        item_type="review-gate",
        selectable=True,
        triage_status="valid",
        resolution_status="unresolved",
        owner_status="not-selected",
        resolved_how="Blocked: Review admission unavailable.",
        evidence="review-lookup.txt",
        summary=REVIEW_BODY,
        closure_evidence="Valid review evidence admitted.",
        resolution_proposal="Retain missing review proof as open.",
    )
    first["sources"] = _user_source("user-request#finding-1", USER_BODY)
    second["sources"] = _user_source("user-request#finding-2", REVIEW_BODY)
    table.update(
        grouped_items_total=0,
        source_records_total=2,
        represented_source_records_total=2,
        selectable_rows_total=2,
        nonselectable_rows_total=0,
    )
    table["triage_status_counts"] = _status_counts(
        tuple(table["triage_status_counts"]), [i["triage_status"] for i in table["items"]]
    )
    table["resolution_status_counts"] = _status_counts(
        tuple(table["resolution_status_counts"]), [i["resolution_status"] for i in table["items"]]
    )
    scope = metadata["resolution_scope"]
    scope.update(
        selection_source="explicit-input",
        prompt_presented=False,
        selection_confirmed_by_user=True,
        deferred_indexes=[2],
        selected_severity_groups=[],
    )
    inventory = {"schema_version": 1, "presentation_version": 4, "selected_indexes": [1], "items": table["items"]}
    return UserFix(result, directory, metadata, table, inventory, scope, first, second)


def _git_checked(*args: str | Path) -> None:
    """Run a command that must succeed, discarding its output."""
    subprocess.run([str(arg) for arg in args], check=True, capture_output=True)


def _attach_local_source(fix: UserFix) -> None:
    """Bind the fix to a real one-file local git repository snapshot instead of a PR identity."""
    directory = fix.directory
    for item in fix.table["items"]:
        item["sources"][0]["location"] = "guard.py:1"
    repository = directory / "source"
    repository.mkdir()
    (repository / "guard.py").write_text("guard = False\n", encoding="utf-8", newline="\n")
    _git_checked("git", "init", repository)
    _git_checked("git", "-C", repository, "add", "guard.py")
    _git_checked(
        "git", "-C", repository, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "Baseline"
    )  # fmt: skip
    pack = directory / "local-source"
    collector = PLUGIN_ROOT / "shared/collect_diff.py"
    _git_checked(
        sys.executable, collector, "--snapshot", "--repository", repository, "--scope-path", "guard.py",
        "--out", pack / "source-snapshot.json",
    )  # fmt: skip
    subprocess.run(
        [sys.executable, str(collector), "--out", str(pack)], cwd=repository, check=True, capture_output=True
    )
    snapshot_bytes = (pack / "source-snapshot.json").read_bytes()
    snapshot = json.loads(snapshot_bytes)
    local_source = {
        "snapshot_path": "local-source/source-snapshot.json",
        "snapshot_sha256": _sha256(snapshot_bytes),
        "diff_path": "local-source/diff.patch",
        "diff_sha256": _sha256((pack / "diff.patch").read_bytes()),
        "repository": snapshot["repository"],
        "revision": snapshot["revision"],
        "scope_paths": snapshot["scope_paths"],
    }
    fix.inventory["local_source"] = local_source
    evidence = fix.metadata["review_report_intake"]["admission_evidence"]
    for key in ("pr_url", "head_oid", "base_oid"):
        del evidence[key]
    evidence["local_source"] = local_source
    fix.metadata["mode"] = "report"
    fix.pack = pack
    fix.snapshot_bytes = snapshot_bytes


def _handoff_rows(items: list[dict]) -> tuple[list[dict], list[dict]]:
    """Build the concise outcome-table rows and detail cells for user-sourced inventory items."""
    rows = [
        {
            "id": item["input_item_id"],
            "source_ids": [f"user:{item['sources'][0]['source_id']}"],
            "cells": [
                item["input_item_id"],
                item["severity"],
                item["item_name"],
                f"user [{item['sources'][0]['source_id']}]",
                f"{item['resolution_status']} — [O{position}]",
                f"[E{position}] — owner/status: {item['owner_status']}",
            ],
        }
        for position, item in enumerate(items, 1)
    ]
    details = [
        detail
        for position, item in enumerate(items, 1)
        for detail in (
            {"id": f"O{position}", "text": item["resolved_how"]},
            {"id": f"E{position}", "text": item["evidence"]},
        )
    ]
    return rows, details


def _finish_user_fix(fix: UserFix) -> None:
    """Freeze the selection, render scope and final handoff with the real finalizer, and validate each artifact."""
    directory, metadata, table = fix.directory, fix.metadata, fix.table
    selection_bytes = json.dumps(fix.inventory).encode("utf-8")
    (directory / "selection.json").write_bytes(selection_bytes)
    metadata["review_report_intake"]["admission_evidence"]["selection_sha256"] = _sha256(selection_bytes)
    rendered_scope = FINALIZER.render_selection(fix.inventory)
    (directory / "resolution-scope.md").write_text(rendered_scope, encoding="utf-8", newline="\n")
    assert "user ×1" in rendered_scope
    assert "report ×" not in rendered_scope
    assert "online ×" not in rendered_scope
    _write_action_items(metadata, directory)
    VALIDATOR._validate_code_remediate_scope_selection(metadata, directory)
    VALIDATOR._validate_code_remediate_final_resolution_table(metadata, directory)
    rows, details = _handoff_rows(table["items"])
    handoff = {
        "schema_version": 1,
        "presentation_version": 4,
        "skill": "code-remediate",
        "branch": "standard",
        "outcome": {
            "title": "Remediation Summary",
            "summary": "Supplied boundary fix implemented; requested review evidence unavailable.",
        },
        "tables": [
            {
                "heading": "Final Outcome Table",
                "layout": "concise",
                "overview_only": True,
                "columns": ["Item", "Severity", "Finding", "Sources", "Outcome", "Evidence / next action"],
                "rows": rows,
                "details": details,
            }
        ],
        "source_records": [{"id": row["source_ids"][0], "evidence": "user-findings.md"} for row in rows],
        "source_coverage": {
            "source_records_total": 2,
            "represented_source_records_total": 2,
            "omitted_source_records_total": 0,
        },
        "verification": [
            {"check": name, "status": "missing-command", "evidence": "Not executed in isolated component fixture."}
            for name in ("lint", "format", "types", "tests", "review")
        ],
        "remaining": [
            {
                "row_id": "R2",
                "item": "Requested review proof",
                "owner": "review producer",
                "next_action": "Retain admission as open; no fresh review dispatched.",
            }
        ],
        "next_steps": ["R2"],
        "confidence": {
            "score": 0.93,
            "band": "fair",
            "limits": ["Review evidence unavailable."],
            "gaps": [
                {"gap": "Review evidence unavailable.", "status": "unresolved", "rationale": "Lookup incomplete."}
            ],
        },
        "artifacts": [{"label": "Result", "path": "result.json"}, {"label": "Ledger", "path": "action-items.md"}],
        "caller_contract": None,
        "commit_disposition": {
            "status": "blocked",
            "reason": "Requested evidence remains open.",
            "evidence": "review-lookup.txt",
        },
    }
    handoff_path = directory / "final-handoff.json"
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
    FINALIZER.render_files(handoff_path, directory / "final.md", directory / "final-handoff.validation.json")
    final = (directory / "final.md").read_text(encoding="utf-8")
    assert "Supplied boundary fix implemented" in final
    assert "implemented" in final
    assert "Missing requested review" in final
    assert (
        json.loads(handoff_path.read_text(encoding="utf-8"))["source_records"][0]["id"] == "user:user-request#finding-1"
    )
    assert "Review handoff blocked" not in final
    fix.result.update(status="fail", metadata=metadata)


@pytest.fixture
def pr_user_fix(admission: tuple[dict, Path]) -> UserFix:
    """Build a failed PR remediation whose supplied user fix is implemented and whose requested review stays open."""
    fix = _user_fix_base(admission)
    _finish_user_fix(fix)
    return fix


@pytest.fixture
def local_user_fix(admission: tuple[dict, Path]) -> UserFix:
    """Bind the same remediation to a real local git snapshot, diff and selection instead of a PR."""
    fix = _user_fix_base(admission)
    _attach_local_source(fix)
    _finish_user_fix(fix)
    return fix


def _change_snapshot(fix: UserFix) -> None:
    """Append a byte to the retained source snapshot."""
    (fix.pack / "source-snapshot.json").write_bytes(fix.snapshot_bytes + b" ")


def _change_diff(fix: UserFix) -> None:
    """Replace the retained source diff."""
    (fix.pack / "diff.patch").write_bytes(b"different source diff")


def _rebind_selection(fix: UserFix) -> None:
    """Change the selection's local checkout and re-hash it in the admission evidence."""
    selection = json.loads((fix.directory / "selection.json").read_bytes())
    selection["local_source"]["repository"] = "changed-checkout"
    changed = json.dumps(selection).encode("utf-8")
    (fix.directory / "selection.json").write_bytes(changed)
    fix.metadata["review_report_intake"]["admission_evidence"]["selection_sha256"] = _sha256(changed)


def _assert_fix_retained(fix: UserFix) -> None:
    """Assert the selected user fix stays implemented and the unselected review gap stays open."""
    assert fix.scope["selected_indexes"] == [1]
    assert fix.second["owner_status"] == "not-selected"
    assert fix.first["resolution_status"] == "implemented"


@pytest.mark.integration
class TestUserFixSelection:
    """A supplied user fix and an unselected review gap across selection, handoff and partial intake."""

    def test_pr_partial_outcome_is_admitted(self, pr_user_fix: UserFix):
        """The actual supplied fix and the unselected review gap are retained for a failed PR remediation.

        Selection, final handoff and intake all agree that the fix is implemented and the review gate stays open.
        """
        _validate_intake(pr_user_fix.result, pr_user_fix.directory)

        _assert_fix_retained(pr_user_fix)

    def test_pr_false_clean_is_rejected(self, pr_user_fix: UserFix):
        """A PR remediation claiming `pass` while the requested review is missing is rejected.

        The unavailable review keeps the result failed, so a clean status must be refused.
        """
        pr_user_fix.result["status"] = "pass"

        with pytest.raises(SystemExit, match="code-remediate-report-admission-requires-fail"):
            _validate_intake(pr_user_fix.result, pr_user_fix.directory)

    def test_local_partial_outcome_is_admitted(self, local_user_fix: UserFix):
        """The supplied fix and unselected review gap are retained for a failed local-checkout remediation.

        The selection is bound to a real git snapshot and diff instead of a PR identity.
        """
        _validate_intake(local_user_fix.result, local_user_fix.directory)

        _assert_fix_retained(local_user_fix)

    def test_local_false_clean_is_rejected(self, local_user_fix: UserFix):
        """A local remediation claiming `pass` while the requested review is missing is rejected.

        The same false-clean rule applies when the source is a local snapshot.
        """
        local_user_fix.result["status"] = "pass"

        with pytest.raises(SystemExit, match="code-remediate-report-admission-requires-fail"):
            _validate_intake(local_user_fix.result, local_user_fix.directory)

    @pytest.mark.parametrize(
        "tamper",
        [
            pytest.param(_change_snapshot, id="changed-snapshot"),
            pytest.param(_change_diff, id="changed-diff"),
            pytest.param(_rebind_selection, id="rebound-selection"),
        ],
    )
    def test_local_source_tampering_is_rejected(self, local_user_fix: UserFix, tamper: Callable[[UserFix], None]):
        """A changed snapshot, diff or rebound selection breaks the local source binding.

        Each tamper happens after the admission recorded the local source hashes.
        """
        tamper(local_user_fix)

        with pytest.raises(SystemExit, match="code-remediate-report-admission-local-source-invalid"):
            _validate_intake(local_user_fix.result, local_user_fix.directory)


class TestUserSourceAndSchema:
    """Retained-body check for user-sourced inventory items."""

    @pytest.mark.parametrize("body", ["", "A finding the user never supplied."])
    def test_user_source_requires_retained_complete_body(self, admission: tuple[dict, Path], body: str):
        """A user tag cannot authenticate invented or missing request text.

        The retained request holds `Actual supplied finding.`; an empty or different body must be rejected.
        """
        _, directory = admission
        (directory / "user-findings.md").write_text("Actual supplied finding.", encoding="utf-8")
        source = {"kind": "user", "source_id": "user-request#finding-1", "body": body, "evidence": "user-findings.md"}

        with pytest.raises(SystemExit, match=r"code-remediate-user-source-(evidence-invalid|body-mismatch)"):
            VALIDATOR._validate_code_remediate_user_source(source, directory)


def _run_source_gates(directory: Path, failed_lint: bool) -> tuple[dict, subprocess.CompletedProcess[str]]:
    """Fix the local source, run the real gate runner and collector, and return the gates and runner result.

    The source regression fails before the edit and passes after it; the lint gate is forced to fail or pass so both
    admission paths run regardless of the host's lint and format tools.
    """
    shutil.rmtree(directory / "pr")
    repository = directory / "source"
    before = json.loads((directory / "local-source/source-snapshot.json").read_bytes())
    assert before["files"][0]["content"] == "guard = False\n"
    regression = directory / "check_guard.py"
    regression.write_text(
        "import runpy\nassert runpy.run_path(" + repr(str(repository / "guard.py")) + ")['guard'] is True\n",
        encoding="utf-8",
        newline="\n",
    )
    failed_before = subprocess.run([sys.executable, str(regression)], capture_output=True, text=True)
    assert failed_before.returncode == 1
    assert "AssertionError" in failed_before.stderr
    (repository / "guard.py").write_text("guard = True\n", encoding="utf-8", newline="\n")
    environment = dict(os.environ)
    for gate in ("LINT", "FORMAT", "TYPES", "TESTS", "REVIEW"):
        environment.pop(gate + "_CMD", None)
    environment["LINT_CMD"] = f"exit {int(failed_lint)}"
    environment["FORMAT_CMD"] = "exit 0"
    script_arguments = (sys.executable, str(regression))
    environment["TESTS_CMD"] = (
        "& " + " ".join("'" + argument.replace("'", "''") + "'" for argument in script_arguments)
        if sys.platform == "win32"
        else shlex.join(script_arguments)
    )
    executed = subprocess.run(
        [sys.executable, str(PLUGIN_ROOT / "shared/run_gates.py"), "--out", str(directory)],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
    )
    (directory / "source-regression-before.json").write_text(
        json.dumps(
            {
                "argv": [sys.executable, str(regression)],
                "returncode": failed_before.returncode,
                "stdout": failed_before.stdout,
                "stderr": failed_before.stderr,
            }
        ),
        encoding="utf-8",
        newline="\n",
    )
    _git_checked(
        sys.executable, PLUGIN_ROOT / "shared/collect_diff.py", "--snapshot", "--repository", repository,
        "--scope-path", "guard.py", "--out", directory / "local-source/source-after.json",
    )  # fmt: skip
    after = json.loads((directory / "local-source/source-after.json").read_bytes())
    assert after["files"][0]["content"] == "guard = True\n"
    return json.loads((directory / "gates.json").read_bytes()), executed


def _record_closure(fix: UserFix, gates: dict) -> None:
    """Write the closure evidence, work plan and summaries that make the implemented fix a complete selected item."""
    directory, metadata = fix.directory, fix.metadata
    fix.first["evidence"] = "checks/tests.stdout.txt; check_guard.py verifies actual source guard is True."
    _write_action_items(metadata, directory)
    workplan = _parallel_metadata()["resolution_workplan"]
    workplan.update(
        execution_mode="parent-owned",
        groups_total=1,
        parent_owned_groups=1,
        specialist_owned_groups=0,
        verifier_groups=1,
        parallel_eligible=False,
        parallel_approval_required=False,
        parallel_approval_source="workflow-default",
        parallel_approval_status="parent-only",
        work_buckets=[
            {
                "bucket_id": "B1",
                "selected_indexes": [1],
                "owner": "parent",
                "verifier": "parent",
                "context_pack_path": "resolution-workplan.md",
                "owned_paths": ["guard.py"],
                "execution_mode": "parent",
            }
        ],
    )
    metadata["resolution_workplan"] = workplan
    _write_workplan(metadata, directory)
    document = directory / "resolution-workplan.md"
    document.write_text(
        document.read_text(encoding="utf-8").replace(
            "## Parallel Approval\n",
            "## Parallel Approval\n\nIneligibility reason: One coherent selected source fix.\n",
        ),
        encoding="utf-8",
        newline="\n",
    )
    metadata["out_of_scope_confirmation"] = {"count": 0, "all_confirmed_by_user": True, "items": []}
    metadata["pr_relevance"] = {
        "evaluated": False,
        "connected_open_items_total": 0,
        "connected_selectable_items_total": 0,
        "connected_required_followup_total": 0,
        "connected_items_marked_out_of_scope": 0,
    }
    metadata["unresolved_summary"] = {
        "selected_items_total": 1,
        "selected_items_resolved": 1,
        "selected_items_unresolved": 0,
        "local_actionable_items_unresolved": 0,
        "process_gate_items_unresolved": 0,
        "environment_blocked_items": 0,
        "external_owner_items": 0,
        "user_deferred_items": 0,
        "all_local_actionable_items_closed": True,
        "unresolved_reason_groups": [],
    }
    (directory / "closure-log.md").write_text(
        "## Closure Evidence\n\nR1: guard.py changed False to True; actual check_guard.py passes via gates.json. "
        "Pre-edit bytes retained in local-source/source-snapshot.json; regression failed before fix in "
        "source-regression-before.json. Post-edit bytes retained in local-source/source-after.json. "
        "R2 requested review remains open and unselected.\n",
        encoding="utf-8",
        newline="\n",
    )
    (directory / "unresolved.txt").write_text(
        "Requested review R2 remains open, unselected. No new review dispatched.\n", encoding="utf-8", newline="\n"
    )


def _render_gate_handoff(fix: UserFix, gates: dict) -> None:
    """Rewrite the final handoff with the actual gate receipts, render it, and bind it into the result metadata."""
    directory, metadata = fix.directory, fix.metadata
    handoff_path = directory / "final-handoff.json"
    handoff = json.loads(handoff_path.read_bytes())
    rows, details = _handoff_rows(metadata["final_resolution_table"]["items"])
    handoff["tables"][0].update(rows=rows, details=details)
    handoff["verification"] = [
        {"check": check["id"], "status": check["status"], "evidence": check["stdout"]} for check in gates["checks"]
    ]
    limits = ["Requested independent review evidence unavailable; local diff check is not that evidence."]
    if gates["checks_failed"]:
        limits.append("Actual gate failures: " + ", ".join(gates["checks_failed"]))
    gaps = [{"gap": gap, "status": "unresolved", "rationale": gap} for gap in limits]
    handoff["confidence"].update(limits=limits, gaps=gaps)
    handoff["commit_disposition"]["reason"] = (
        "Requested independent review remains open; gate statuses retained from actual runner."
    )
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8", newline="\n")
    FINALIZER.render_files(handoff_path, directory / "final.md", directory / "final-handoff.validation.json")
    validation = json.loads((directory / "final-handoff.validation.json").read_bytes())
    metadata.update(
        confidence_gaps=limits,
        confidence_gap_closures=gaps,
        confidence_recovery={
            "initial_confidence": 0.93,
            "final_confidence": 0.93,
            "status": "fair",
            "evidence": ["Actual local collector, source regression and shared gate receipts."],
            "recovery_actions": ["Retain requested proof as open; report actual source fix separately."],
            "remaining_limits": limits,
        },
    )
    metadata["final_handoff"] = {
        "schema_version": 1,
        "branch": "standard",
        "handoff_path": "final-handoff.json",
        "handoff_sha256": validation["handoff_sha256"],
        "rendered_path": "final.md",
        "rendered_sha256": validation["rendered_sha256"],
        "validation_path": "final-handoff.validation.json",
    }


def _write_final_result(fix: UserFix, gates: dict) -> tuple[list[str], Path]:
    """Write the complete result.json and return the validator command line with the result path."""
    fix.result.update(
        schema_version=2,
        status="fail",
        checks_run=[check["id"] for check in gates["checks"]],
        checks_failed=gates["checks_failed"],
        findings={severity: 0 for severity in ("critical", "high", "medium", "low")},
        confidence=0.93,
        artifact_path="result.json",
    )
    result_path = fix.directory / "result.json"
    result_path.write_text(json.dumps(fix.result), encoding="utf-8", newline="\n")
    argv = [
        sys.executable,
        str(PLUGIN_ROOT / "shared/validate-artifacts.py"),
        "--skill",
        "code-remediate",
        "--out",
        str(fix.directory),
        "--result",
        str(result_path),
    ]
    return argv, result_path


def _validate_cli(argv: list[str], directory: Path, receipt: str) -> subprocess.CompletedProcess[str]:
    """Run the artifact validator CLI and keep its command, exit code and streams as a receipt file."""
    completed = subprocess.run(argv, capture_output=True, text=True)
    (directory / receipt).write_text(
        json.dumps(
            {"argv": argv, "returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}
        ),
        encoding="utf-8",
        newline="\n",
    )
    return completed


@pytest.mark.integration
@pytest.mark.parametrize(
    ("failed_lint", "rejection"),
    [
        pytest.param(False, "code-remediate-report-admission-requires-fail\n", id="gates-green"),
        pytest.param(True, "pass-with-failed-checks\n", id="lint-failed"),
    ],
)
def test_local_partial_outcome_passes_complete_cli_and_rejects_false_clean(
    local_user_fix: UserFix, failed_lint: bool, rejection: str
):
    """Actual source and gate receipts pass the complete validator CLI, while a clean claim is rejected.

    A real local repository is fixed, the real gate runner records each gate (lint forced to pass or fail), and the real
    validator CLI accepts the failed remediation. Flipping the same result to `pass` must then be refused, with `pass-
    with-failed-checks` when a gate failed and the missing-review reason otherwise.
    """
    fix = local_user_fix
    gates, executed = _run_source_gates(fix.directory, failed_lint)
    observed = {check["id"]: check["status"] for check in gates["checks"]}
    _record_closure(fix, gates)
    _render_gate_handoff(fix, gates)
    argv, result_path = _write_final_result(fix, gates)

    accepted = _validate_cli(argv, fix.directory, "complete-cli.accepted.json")
    fix.result["status"] = "pass"
    result_path.write_text(json.dumps(fix.result), encoding="utf-8", newline="\n")
    rejected = _validate_cli(argv, fix.directory, "complete-cli.false-clean.json")

    assert gates["checks_failed"] == (["lint"] if failed_lint else [])
    assert executed.returncode == (1 if failed_lint else 0)
    assert (observed["tests"], observed["types"], observed["review"]) == ("pass", "not-applicable", "pass")
    assert accepted.returncode == 0, accepted.stderr
    assert accepted.stderr == ""
    assert rejected.returncode == 1
    assert rejected.stderr == rejection

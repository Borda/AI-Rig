"""Reject clean claims while allowing source-bound incomplete review intake."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from test_code_remediate_final_outcome_validation import _metadata, _status_counts, _write_action_items
from test_code_remediate_work_bucket_validation import _parallel_metadata, _write_workplan
from test_review_remediation_handoff import (
    test_native_assembly_finalization_and_separate_intake_preserve_proof as build_native_review_evidence,
)

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "remediation_admission_validator", PLUGIN_ROOT / "shared/validate-artifacts.py"
)
assert SPEC is not None and SPEC.loader is not None
VALIDATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)


@pytest.fixture
def completed_report(tmp_path: Path) -> tuple[dict, Path, Path]:
    """Retain exact producer bytes and a lossless consumer inventory of its obligations."""
    fixture_path = PLUGIN_ROOT / "tests/review/test_review_completion_gate.py"
    spec = importlib.util.spec_from_file_location("intake_producer_fixture", fixture_path)
    assert spec is not None and spec.loader is not None
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    producer = fixture._assessed_pr.__wrapped__(tmp_path)
    report_path = producer / "result.json"
    report = json.loads(report_path.read_bytes())
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    (consumer / "findings-input.txt").write_bytes(report_path.read_bytes())
    (consumer / "action-items.md").write_text("## Review Report Intake\n", encoding="utf-8")
    (consumer / "resolution-scope.md").write_text("Retain review obligations.\n", encoding="utf-8")
    original = report["metadata"]
    obligations = [
        *report.get("checks_failed", []),
        *report.get("follow_up", []),
        *original["confidence_gaps"],
        *original["review_decision"].get("required_next_work", []),
        *original["confidence_recovery"]["remaining_limits"],
    ]
    metadata = {
        "mode": "report",
        "resolution_scope": {"presentation_version": 4},
        "final_resolution_table": {
            "items": [
                {
                    "input_item_id": "G1",
                    "item_type": "confidence-gap",
                    "selectable": True,
                    "sources": [{"kind": "report", "source_id": "result.json#limits", "body": "\n".join(obligations)}],
                }
            ]
        },
        "review_report_intake": {
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


@pytest.mark.integration
@pytest.mark.parametrize("damage", ["unpromoted", "forged-proof", "source-drift", "copied-bytes"])
def test_completed_intake_rejects_unvalidated_producer(completed_report: tuple[dict, Path, Path], damage: str) -> None:
    """A mirrored inventory cannot turn a candidate, forged proof, or stale source into completion."""
    result, consumer, producer = completed_report
    if damage == "unpromoted":
        (producer / "result.json").rename(producer / "result.candidate.json")
    elif damage == "forged-proof":
        (producer / "final-handoff.validation.json").write_text("{}", encoding="utf-8")
    elif damage == "source-drift":
        (producer / "diff.patch").write_bytes(b"changed source\n")
    else:
        (consumer / "findings-input.txt").write_bytes((producer / "result.json").read_bytes() + b"\n")
    with pytest.raises(SystemExit, match="code-remediate-report-producer-"):
        VALIDATOR._validate_code_remediate_report_intake(result, consumer, current_contract=True)


@pytest.mark.integration
@pytest.mark.parametrize("path_style", ["absolute-artifact", "producer-relative-artifact"])
def test_completed_intake_admits_validated_immutable_producer(
    completed_report: tuple[dict, Path, Path], path_style: str
) -> None:
    """Revalidate promoted producer bytes without changing either producer or consumer artifacts."""
    result, consumer, producer = completed_report
    if path_style == "producer-relative-artifact":
        report_path = producer / "result.json"
        report = json.loads(report_path.read_bytes())
        report["artifact_path"] = "result.json"
        handoff_path = producer / "final-handoff.json"
        handoff = json.loads(handoff_path.read_bytes())
        handoff["artifacts"][0]["path"] = "result.json"
        handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
        spec = importlib.util.spec_from_file_location(
            "intake_relative_handoff", PLUGIN_ROOT / "shared/final_handoff.py"
        )
        assert spec is not None and spec.loader is not None
        renderer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(renderer)
        proof = renderer.render_files(handoff_path, producer / "final.md", producer / "final-handoff.validation.json")
        for field in ("handoff_sha256", "rendered_sha256"):
            report["metadata"]["final_handoff"][field] = proof[field]
        report_path.write_text(json.dumps(report), encoding="utf-8")
        (consumer / "findings-input.txt").write_bytes(report_path.read_bytes())
        result["metadata"]["review_report_intake"]["admission_evidence"] = {"producer_result_path": str(report_path)}
    before = {path: path.read_bytes() for path in producer.iterdir() if path.is_file()}
    copied = (consumer / "findings-input.txt").read_bytes()
    VALIDATOR._validate_code_remediate_report_intake(result, consumer, current_contract=True)
    assert (consumer / "findings-input.txt").read_bytes() == copied
    assert {path: path.read_bytes() for path in before} == before


def test_completed_intake_requires_origin_for_producer_relative_artifact(
    completed_report: tuple[dict, Path, Path],
) -> None:
    """Reject a copied producer-relative filename rather than guessing its original directory."""
    result, consumer, _producer = completed_report
    copied = consumer / "findings-input.txt"
    report = json.loads(copied.read_bytes())
    report["artifact_path"] = "result.json"
    copied.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(SystemExit, match="code-remediate-report-producer-origin-missing"):
        VALIDATOR._validate_code_remediate_report_intake(result, consumer, current_contract=True)


@pytest.fixture
def native_report(tmp_path: Path) -> tuple[dict, Path, Path, Path]:
    """Reuse real native assembly and finalization evidence for a later consumer session."""
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
                    "sources": [
                        {
                            "kind": "report",
                            "source_id": "result.json#limits",
                            "body": "\n".join(report["metadata"]["confidence_gaps"]),
                        }
                    ],
                }
            ]
        },
        "review_report_intake": {
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
    return {"status": "fail", "metadata": metadata}, consumer, producer, home


@pytest.mark.integration
def test_local_finder_recovers_producer_thread_in_later_session(
    native_report: tuple[dict, Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A local manifest retains the actual producer thread rather than the consumer's current thread."""
    _result, consumer, producer, home = native_report
    monkeypatch.setenv("CODEX_THREAD_ID", "unrelated-consumer")
    monkeypatch.setenv("CODEX_HOME", str(consumer))
    completed = subprocess.run(
        [
            sys.executable,
            str(PLUGIN_ROOT / "shared/find-review-report.py"),
            "--result",
            str(producer / "result.json"),
            "--codex-home",
            str(home),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == str(producer / "result.json")


@pytest.mark.integration
@pytest.mark.parametrize("thread_override", [True, False])
def test_completed_native_intake_preserves_producer_evidence_location(
    native_report: tuple[dict, Path, Path, Path], monkeypatch: pytest.MonkeyPatch, thread_override: bool
) -> None:
    """A later consumer admits real native proof using original coordinates without rewriting artifacts."""
    result, consumer, producer, home = native_report
    monkeypatch.setenv("CODEX_THREAD_ID", "unrelated-consumer")
    monkeypatch.setenv("CODEX_HOME", str(consumer))
    if not thread_override:
        result["metadata"]["review_report_intake"]["admission_evidence"].pop("producer_parent_thread_id")
    paths = [path for root in (producer, home, consumer) for path in root.rglob("*") if path.is_file()]
    before = {path: path.read_bytes() for path in paths}
    VALIDATOR._validate_code_remediate_report_intake(result, consumer, current_contract=True)
    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.integration
@pytest.mark.parametrize("damage", ["wrong-home", "wrong-thread", "malformed-proof"])
def test_completed_native_intake_rejects_wrong_producer_coordinates(
    native_report: tuple[dict, Path, Path, Path], monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    """Explicit evidence coordinates never weaken thread, rollout-home, or proof validation."""
    result, consumer, producer, home = native_report
    monkeypatch.setenv("CODEX_THREAD_ID", "unrelated-consumer")
    monkeypatch.setenv("CODEX_HOME", str(consumer))
    evidence = result["metadata"]["review_report_intake"]["admission_evidence"]
    if damage == "wrong-home":
        evidence["producer_codex_home"] = str(consumer)
    elif damage == "wrong-thread":
        evidence["producer_parent_thread_id"] = "unrelated-producer"
    else:
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
    paths = [path for root in (producer, home, consumer) for path in root.rglob("*") if path.is_file()]
    before = {path: path.read_bytes() for path in paths}
    with pytest.raises(SystemExit, match="code-remediate-report-producer-validation-failed"):
        VALIDATOR._validate_code_remediate_report_intake(result, consumer, current_contract=True)
    assert {path: path.read_bytes() for path in before} == before


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
        "diagnostic_sha256": hashlib.sha256(diagnostic).hexdigest(),
        "selection_sha256": hashlib.sha256(selection).hexdigest(),
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


def test_missing_review_keeps_fixed_work_and_unselected_evidence_gap(admission: tuple[dict, Path]) -> None:
    """Do not require selecting reviewer bookkeeping before reporting the actual fix."""
    result, directory = admission
    VALIDATOR._validate_code_remediate_report_intake(result, directory, current_contract=True)
    assert result["metadata"]["final_resolution_table"]["items"][0]["resolution_status"] == "implemented"
    assert result["metadata"]["review_report_intake"]["requested_report"] is True


@pytest.mark.parametrize("status", ["pass", "timeout"])
def test_noncompleted_admission_never_certifies_clean_result(admission: tuple[dict, Path], status: str) -> None:
    """A missing requested review is retained only in an explicitly failed remediation."""
    result, directory = admission
    result["status"] = status
    with pytest.raises(SystemExit, match="code-remediate-report-admission-requires-fail"):
        VALIDATOR._validate_code_remediate_report_intake(result, directory, current_contract=True)


@pytest.mark.parametrize("change", ["missing", "closed", "wrong-kind"])
def test_missing_or_closed_review_obligation_is_rejected(admission: tuple[dict, Path], change: str) -> None:
    """A diagnostic cannot replace an honest open obligation in the final inventory."""
    result, directory = admission
    item = result["metadata"]["final_resolution_table"]["items"][1]
    if change == "missing":
        result["metadata"]["final_resolution_table"]["items"].pop()
    elif change == "closed":
        item.update(resolution_status="resolved", owner_status="resolved")
    else:
        item["item_type"] = "code"
    with pytest.raises(SystemExit, match="code-remediate-report-admission-open-obligation-missing"):
        VALIDATOR._validate_code_remediate_report_intake(result, directory, current_contract=True)


@pytest.mark.parametrize("field", ["pr_url", "head_oid", "base_oid", "selection_sha256", "diagnostic_sha256"])
def test_admission_cannot_rebind_source_or_evidence(admission: tuple[dict, Path], field: str) -> None:
    """Reject a reused diagnostic for a different PR, source, selection, or byte stream."""
    result, directory = admission
    result["metadata"]["review_report_intake"]["admission_evidence"][field] = "changed"
    with pytest.raises(SystemExit, match="code-remediate-report-admission-evidence-mismatch"):
        VALIDATOR._validate_code_remediate_report_intake(result, directory, current_contract=True)


def test_unavailable_cannot_hide_report_origin_findings(admission: tuple[dict, Path]) -> None:
    """No-report admission must not relabel preliminary report records as verified intake."""
    result, directory = admission
    result["metadata"]["final_resolution_table"]["items"][0]["sources"][0]["kind"] = "report"
    intake = result["metadata"]["review_report_intake"]
    intake["report_items_total"] = 1
    with pytest.raises(SystemExit, match="code-remediate-report-admission-unavailable-report-sources"):
        VALIDATOR._validate_code_remediate_report_intake(result, directory, current_contract=True)


def test_omitted_admission_status_keeps_completed_report_contract(admission: tuple[dict, Path]) -> None:
    """Backward compatibility does not silently downgrade absent completed input."""
    result, directory = admission
    del result["metadata"]["review_report_intake"]["admission_status"]
    with pytest.raises(SystemExit, match="code-remediate-report-input-missing"):
        VALIDATOR._validate_code_remediate_report_intake(result, directory, current_contract=True)


@pytest.fixture
def preliminary(admission: tuple[dict, Path]) -> tuple[dict, Path]:
    """Add the unchanged original structured finding and its exact PR identity."""
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
            "review_input_sha256": hashlib.sha256((directory / "pr/diff.patch").read_bytes()).hexdigest(),
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
        original_pr_sha256=hashlib.sha256(pr_bytes).hexdigest(),
        original_input_sha256=hashlib.sha256(original_bytes).hexdigest(),
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


def test_preliminary_records_are_source_bound_and_lossless(preliminary: tuple[dict, Path]) -> None:
    """A retained candidate supplies hypotheses without certifying review completion."""
    result, directory = preliminary
    VALIDATOR._validate_code_remediate_report_intake(result, directory, current_contract=True)
    assert result["status"] == "fail"
    assert result["metadata"]["review_report_intake"]["admission_status"] == "preliminary"


def test_preliminary_record_cannot_omit_resolution_contract(preliminary: tuple[dict, Path]) -> None:
    """An ID match alone must not erase original closure requirements."""
    result, directory = preliminary
    result["metadata"]["final_resolution_table"]["items"][0]["sources"][0]["body"] = "Missing guard"
    with pytest.raises(SystemExit, match="code-remediate-report-finding-detail-omitted:F1:summary"):
        VALIDATOR._validate_code_remediate_report_intake(result, directory, current_contract=True)


def test_preliminary_record_cannot_reuse_old_diff(preliminary: tuple[dict, Path]) -> None:
    """A matching PR number cannot hide a different source patch."""
    result, directory = preliminary
    (directory / "pr/diff.patch").write_bytes(b"different source\n")
    with pytest.raises(SystemExit, match="code-remediate-report-admission-original-source-invalid"):
        VALIDATOR._validate_code_remediate_report_intake(result, directory, current_contract=True)


def test_preliminary_requires_enriched_canonical_records(preliminary: tuple[dict, Path]) -> None:
    """Do not turn unstructured or incomplete notes into canonical findings."""
    result, directory = preliminary
    path = directory / "findings-input.txt"
    original = json.loads(path.read_bytes())
    del original["metadata"]["review_findings"][0]["required_change"]
    changed = json.dumps(original).encode("utf-8")
    path.write_bytes(changed)
    result["metadata"]["review_report_intake"]["admission_evidence"]["original_input_sha256"] = hashlib.sha256(
        changed
    ).hexdigest()
    with pytest.raises(SystemExit, match="code-remediate-report-admission-canonical-records-required"):
        VALIDATOR._validate_code_remediate_report_intake(result, directory, current_contract=True)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("status", "mode"),
    [
        pytest.param("fail", "pr", id="pr-partial"),
        pytest.param("pass", "pr", id="pr-false-clean"),
        pytest.param("fail", "report", id="local-partial"),
        pytest.param("pass", "report", id="local-false-clean"),
        pytest.param("fail", "report-snapshot", id="local-changed-snapshot"),
        pytest.param("fail", "report-diff", id="local-changed-diff"),
        pytest.param("fail", "report-selection", id="local-rebound-selection"),
    ],
)
def test_user_fix_selection_finalizer_and_partial_consumer(
    admission: tuple[dict, Path], status: str, mode: str
) -> None:
    """Retain the actual supplied fix and unselected review gap across selection, handoff, and intake."""
    result, directory = admission
    original_metadata = result["metadata"]
    metadata = _metadata()
    metadata.update(original_metadata)
    table = _metadata()["final_resolution_table"]
    metadata["final_resolution_table"] = table
    user_body = "Please add the missing boundary guard."
    review_body = "Use the existing review evidence for these fixes."
    (directory / "user-findings.md").write_text(user_body + "\n" + review_body, encoding="utf-8")
    first, second = table["items"]
    first.update(
        resolved_how="Implemented: Added the missing guard.",
        summary=user_body,
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
        summary=review_body,
        closure_evidence="Valid review evidence admitted.",
        resolution_proposal="Retain missing review proof as open.",
    )
    for item, identity, body in (
        (first, "user-request#finding-1", user_body),
        (second, "user-request#finding-2", review_body),
    ):
        item["sources"] = [
            {
                "kind": "user",
                "source_id": identity,
                "location": "src/guard.py:12",
                "body": body,
                "evidence": "user-findings.md",
            }
        ]
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
    if mode.startswith("report"):
        for item in table["items"]:
            item["sources"][0]["location"] = "guard.py:1"
        repository = directory / "source"
        repository.mkdir()
        (repository / "guard.py").write_text("guard = False\n", encoding="utf-8", newline="\n")
        subprocess.run(["git", "init", str(repository)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repository), "add", "guard.py"], check=True, capture_output=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-m",
                "Baseline",
            ],
            check=True,
            capture_output=True,
        )
        pack = directory / "local-source"
        subprocess.run(
            [
                sys.executable,
                str(PLUGIN_ROOT / "shared/collect_diff.py"),
                "--snapshot",
                "--repository",
                str(repository),
                "--scope-path",
                "guard.py",
                "--out",
                str(pack / "source-snapshot.json"),
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [sys.executable, str(PLUGIN_ROOT / "shared/collect_diff.py"), "--out", str(pack)],
            cwd=repository,
            check=True,
            capture_output=True,
        )
        snapshot_bytes = (pack / "source-snapshot.json").read_bytes()
        snapshot = json.loads(snapshot_bytes)
        local_source = {
            "snapshot_path": "local-source/source-snapshot.json",
            "snapshot_sha256": hashlib.sha256(snapshot_bytes).hexdigest(),
            "diff_path": "local-source/diff.patch",
            "diff_sha256": hashlib.sha256((pack / "diff.patch").read_bytes()).hexdigest(),
            "repository": snapshot["repository"],
            "revision": snapshot["revision"],
            "scope_paths": snapshot["scope_paths"],
        }
        inventory["local_source"] = local_source
        evidence = metadata["review_report_intake"]["admission_evidence"]
        for key in ("pr_url", "head_oid", "base_oid"):
            del evidence[key]
        evidence["local_source"] = local_source
        metadata["mode"] = "report"
    selection_bytes = json.dumps(inventory).encode("utf-8")
    (directory / "selection.json").write_bytes(selection_bytes)
    metadata["review_report_intake"]["admission_evidence"]["selection_sha256"] = hashlib.sha256(
        selection_bytes
    ).hexdigest()
    spec = importlib.util.spec_from_file_location("user_finding_finalizer", PLUGIN_ROOT / "shared/final_handoff.py")
    assert spec is not None and spec.loader is not None
    finalizer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(finalizer)
    rendered_scope = finalizer.render_selection(inventory)
    (directory / "resolution-scope.md").write_text(rendered_scope, encoding="utf-8", newline="\n")
    assert "user ×1" in rendered_scope
    assert "report ×" not in rendered_scope and "online ×" not in rendered_scope
    _write_action_items(metadata, directory)
    VALIDATOR._validate_code_remediate_scope_selection(metadata, directory)
    VALIDATOR._validate_code_remediate_final_resolution_table(metadata, directory)
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
        for position, item in enumerate(table["items"], 1)
    ]
    details = [
        detail
        for position, item in enumerate(table["items"], 1)
        for detail in (
            {"id": f"O{position}", "text": item["resolved_how"]},
            {"id": f"E{position}", "text": item["evidence"]},
        )
    ]
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
    finalizer.render_files(handoff_path, directory / "final.md", directory / "final-handoff.validation.json")
    final = (directory / "final.md").read_text(encoding="utf-8")
    assert "Supplied boundary fix implemented" in final
    assert "implemented" in final and "Missing requested review" in final
    assert (
        json.loads(handoff_path.read_text(encoding="utf-8"))["source_records"][0]["id"] == "user:user-request#finding-1"
    )
    assert "Review handoff blocked" not in final
    result.update(status=status, metadata=metadata)
    if mode == "report-snapshot":
        (pack / "source-snapshot.json").write_bytes(snapshot_bytes + b" ")
    elif mode == "report-diff":
        (pack / "diff.patch").write_bytes(b"different source diff")
    elif mode == "report-selection":
        changed_selection = json.loads((directory / "selection.json").read_bytes())
        changed_selection["local_source"]["repository"] = "changed-checkout"
        changed_bytes = json.dumps(changed_selection).encode("utf-8")
        (directory / "selection.json").write_bytes(changed_bytes)
        metadata["review_report_intake"]["admission_evidence"]["selection_sha256"] = hashlib.sha256(
            changed_bytes
        ).hexdigest()
    if mode in {"report-snapshot", "report-diff", "report-selection"}:
        with pytest.raises(SystemExit, match="code-remediate-report-admission-local-source-invalid"):
            VALIDATOR._validate_code_remediate_report_intake(result, directory, current_contract=True)
    elif status == "pass":
        with pytest.raises(SystemExit, match="code-remediate-report-admission-requires-fail"):
            VALIDATOR._validate_code_remediate_report_intake(result, directory, current_contract=True)
    else:
        VALIDATOR._validate_code_remediate_report_intake(result, directory, current_contract=True)
        assert scope["selected_indexes"] == [1]
        assert second["owner_status"] == "not-selected"
        assert first["resolution_status"] == "implemented"


@pytest.mark.parametrize("body", ["", "A finding the user never supplied."])
def test_user_source_requires_retained_complete_body(admission: tuple[dict, Path], body: str) -> None:
    """A user tag cannot authenticate invented or missing request text."""
    _, directory = admission
    (directory / "user-findings.md").write_text("Actual supplied finding.", encoding="utf-8")
    source = {"kind": "user", "source_id": "user-request#finding-1", "body": body, "evidence": "user-findings.md"}
    with pytest.raises(SystemExit, match="code-remediate-user-source-(evidence-invalid|body-mismatch)"):
        VALIDATOR._validate_code_remediate_user_source(source, directory)


@pytest.mark.parametrize("version", [0, 2, True])
def test_admission_schema_is_its_own_first_version(admission: tuple[dict, Path], version: object) -> None:
    """Prevent version skipping and boolean aliases in the new admission family."""
    result, directory = admission
    result["metadata"]["review_report_intake"]["schema_version"] = version
    with pytest.raises(SystemExit, match="code-remediate-report-admission-schema-invalid"):
        VALIDATOR._validate_code_remediate_report_intake(result, directory, current_contract=True)


@pytest.mark.integration
@pytest.mark.parametrize("failed_lint", [False, True])
def test_local_partial_outcome_passes_complete_cli_and_rejects_false_clean(
    admission: tuple[dict, Path], failed_lint: bool
) -> None:
    """Accept actual source and gate receipts while rejecting a clean claim with missing review proof."""
    result, directory = admission
    test_user_fix_selection_finalizer_and_partial_consumer(admission, "fail", "report")
    for path in (directory / "pr").iterdir():
        path.unlink()
    (directory / "pr").rmdir()
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
    # Exercise both admission paths regardless of the host's lint/format tools; the real runner records each exit.
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
    subprocess.run(
        [
            sys.executable,
            str(PLUGIN_ROOT / "shared/collect_diff.py"),
            "--snapshot",
            "--repository",
            str(repository),
            "--scope-path",
            "guard.py",
            "--out",
            str(directory / "local-source/source-after.json"),
        ],
        check=True,
        capture_output=True,
    )
    assert (
        json.loads((directory / "local-source/source-after.json").read_bytes())["files"][0]["content"]
        == "guard = True\n"
    )
    gates = json.loads((directory / "gates.json").read_bytes())
    assert gates["checks_failed"] == (["lint"] if failed_lint else [])
    assert executed.returncode == (1 if gates["checks_failed"] else 0)
    observed = {check["id"]: check["status"] for check in gates["checks"]}
    assert observed["tests"] == "pass"
    assert observed["types"] == "not-applicable"
    assert observed["review"] == "pass"
    metadata = result["metadata"]
    first = metadata["final_resolution_table"]["items"][0]
    first["evidence"] = "checks/tests.stdout.txt; check_guard.py verifies actual source guard is True."
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
        "Pre-edit bytes retained in local-source/source-snapshot.json; regression failed before fix in source-regression-before.json. "
        "Post-edit bytes retained in local-source/source-after.json. R2 requested review remains open and unselected.\n",
        encoding="utf-8",
        newline="\n",
    )
    (directory / "unresolved.txt").write_text(
        "Requested review R2 remains open, unselected. No new review dispatched.\n", encoding="utf-8", newline="\n"
    )
    handoff_path = directory / "final-handoff.json"
    handoff = json.loads(handoff_path.read_bytes())
    rows, details = [], []
    for position, item in enumerate(metadata["final_resolution_table"]["items"], 1):
        source = item["sources"][0]
        rows.append(
            {
                "id": item["input_item_id"],
                "source_ids": [f"user:{source['source_id']}"],
                "cells": [
                    item["input_item_id"],
                    item["severity"],
                    item["item_name"],
                    f"user [{source['source_id']}]",
                    f"{item['resolution_status']} — [O{position}]",
                    f"[E{position}] — owner/status: {item['owner_status']}",
                ],
            }
        )
        details.extend(
            [{"id": f"O{position}", "text": item["resolved_how"]}, {"id": f"E{position}", "text": item["evidence"]}]
        )
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
    spec = importlib.util.spec_from_file_location("complete_partial_finalizer", PLUGIN_ROOT / "shared/final_handoff.py")
    assert spec is not None and spec.loader is not None
    finalizer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(finalizer)
    finalizer.render_files(handoff_path, directory / "final.md", directory / "final-handoff.validation.json")
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
    result.update(
        schema_version=2,
        status="fail",
        checks_run=[check["id"] for check in gates["checks"]],
        checks_failed=gates["checks_failed"],
        findings={severity: 0 for severity in ("critical", "high", "medium", "low")},
        confidence=0.93,
        artifact_path="result.json",
    )
    result_path = directory / "result.json"
    result_path.write_text(json.dumps(result), encoding="utf-8", newline="\n")
    argv = [
        sys.executable,
        str(PLUGIN_ROOT / "shared/validate-artifacts.py"),
        "--skill",
        "code-remediate",
        "--out",
        str(directory),
        "--result",
        str(result_path),
    ]
    accepted = subprocess.run(argv, capture_output=True, text=True)
    (directory / "complete-cli.accepted.json").write_text(
        json.dumps(
            {"argv": argv, "returncode": accepted.returncode, "stdout": accepted.stdout, "stderr": accepted.stderr}
        ),
        encoding="utf-8",
        newline="\n",
    )
    assert accepted.returncode == 0, accepted.stderr
    assert accepted.stderr == ""
    result["status"] = "pass"
    result_path.write_text(json.dumps(result), encoding="utf-8", newline="\n")
    rejected = subprocess.run(argv, capture_output=True, text=True)
    (directory / "complete-cli.false-clean.json").write_text(
        json.dumps(
            {"argv": argv, "returncode": rejected.returncode, "stdout": rejected.stdout, "stderr": rejected.stderr}
        ),
        encoding="utf-8",
        newline="\n",
    )
    assert rejected.returncode == 1
    assert (
        "pass-with-failed-checks\n" if failed_lint else "code-remediate-report-admission-requires-fail\n"
    ) == rejected.stderr

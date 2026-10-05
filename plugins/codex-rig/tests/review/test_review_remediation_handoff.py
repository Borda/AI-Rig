"""Exercise immutable review completion and separate remediation intake boundaries."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import test_review_batches as batches
import test_review_prepare as preparation
from test_review_completion_gate import FINDER, PLUGIN_ROOT, _assessed_pr, _finalize_parent_fallback, _module


@pytest.mark.integration
def test_parent_fallback_promotes_and_preserves_failed_gate_obligations_at_intake(tmp_path: Path) -> None:
    """Carry a failed parent review through finalization, discovery, and lossless consumer admission."""
    run = _assessed_pr.__wrapped__(tmp_path)
    finalized = _finalize_parent_fallback(run)
    assert finalized.returncode == 0, finalized.stdout + finalized.stderr
    assert json.loads(finalized.stdout)["promoted"] is True
    result_path = run / "result.json"
    result = json.loads(result_path.read_bytes())
    assert result["status"] == "fail"
    assert result["checks_failed"] == ["types"]
    assert result["metadata"]["execution_mode"] == "serial-fallback"
    assert result["metadata"]["independence_required"] is False
    assert result["metadata"]["independence_satisfied"] is False
    assert {item["role"] for item in result["metadata"]["specialist_passes"]} == {"challenger", "qa-specialist"}
    assert all(
        item["mode"] == "substituted" and not item.get("attempts") for item in result["metadata"]["specialist_passes"]
    )
    before = {path.relative_to(run): path.read_bytes() for path in run.rglob("*") if path.is_file()}
    completed = subprocess.run(
        [sys.executable, str(FINDER), "--complete-run", str(run), "--parent-thread-id", "thread"],
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode()
    assert completed.stdout == before[Path("final.md")]
    for arguments in (
        ["--target", "https://github.com/acme/widgets/pull/123", "--reports-dir", str(run.parent.parent)],
        ["--result", str(result_path)],
    ):
        admitted = subprocess.run(
            [sys.executable, str(FINDER), *arguments], capture_output=True, text=True, check=False
        )
        assert admitted.returncode == 0, admitted.stderr
        assert admitted.stdout == str(result_path) + "\n"
        assert admitted.stderr == ""

    consumer = tmp_path / "remediation"
    consumer.mkdir()
    (consumer / "findings-input.txt").write_bytes(result_path.read_bytes())
    original = result["metadata"]
    blocker = original["operational_blockers"][0]
    blocker_body = "\n".join([blocker["title"], blocker["required_change"], *blocker["evidence"], "types"])
    items = [
        {
            "input_item_id": blocker["id"],
            "item_type": "review-gate",
            "selectable": True,
            "triage_status": "valid",
            "sources": [{"kind": "report", "source_id": f"{result_path}#{blocker['id']}", "body": blocker_body}],
        },
        {
            "input_item_id": "INDEPENDENT-COVERAGE",
            "item_type": "confidence-gap",
            "selectable": True,
            "triage_status": "valid",
            "sources": [
                {
                    "kind": "report",
                    "source_id": f"{result_path}#confidence",
                    "body": "\n".join(
                        original["confidence_gaps"] + original["confidence_recovery"]["remaining_limits"]
                    ),
                }
            ],
        },
    ]
    metadata = {
        "resolution_scope": {"presentation_version": 4},
        "final_resolution_table": {"items": items},
        "review_report_intake": {
            "schema_version": 1,
            "requested_report": True,
            "admission_status": "completed",
            "report_items_total": 2,
            "review_gate_items_total": 2,
            "review_gate_items_selectable": 2,
            "report_items_marked_out_of_scope": 0,
        },
    }
    (consumer / "action-items.md").write_text("## Review Report Intake\n", encoding="utf-8")
    (consumer / "resolution-scope.md").write_text("Review-gate obligations remain selectable.\n", encoding="utf-8")
    validator = _module(PLUGIN_ROOT / "shared/validate-artifacts.py")
    validator._validate_code_remediate_report_intake(
        {"status": "fail", "metadata": metadata}, consumer, current_contract=True
    )
    assert (consumer / "findings-input.txt").read_bytes() == before[Path("result.json")]
    assert {path.relative_to(run): path.read_bytes() for path in run.rglob("*") if path.is_file()} == before

    items[0]["sources"][0]["body"] = blocker_body.removesuffix("\ntypes")
    with pytest.raises(SystemExit, match="code-remediate-report-obligation-omitted:checks_failed"):
        validator._validate_code_remediate_report_coverage(metadata, consumer)
    items[0]["sources"][0]["body"] = blocker_body
    items.pop()
    with pytest.raises(SystemExit, match="code-remediate-report-obligation-omitted:confidence_gaps"):
        validator._validate_code_remediate_report_coverage(metadata, consumer)


@pytest.mark.integration
def test_completed_review_is_admitted_without_rewriting_producer_evidence(tmp_path: Path) -> None:
    """Keep producer proof byte-exact when a separate consumer validates its result."""
    run = _assessed_pr.__wrapped__(tmp_path)
    before = {path.relative_to(run): path.read_bytes() for path in run.rglob("*") if path.is_file()}
    completed = subprocess.run(
        [sys.executable, str(FINDER), "--complete-run", str(run), "--parent-thread-id", "thread"],
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr.decode()
    assert completed.stdout == before[Path("final.md")]

    admitted = subprocess.run(
        [sys.executable, str(FINDER), "--result", str(run / "result.json")], capture_output=True, text=True
    )
    assert admitted.returncode == 0, admitted.stderr
    assert admitted.stdout == str(run / "result.json") + "\n"
    assert admitted.stderr == ""
    assert {path.relative_to(run): path.read_bytes() for path in run.rglob("*") if path.is_file()} == before


@pytest.mark.integration
def test_malformed_closure_proof_blocks_completion_and_consumer_with_same_diagnostic(tmp_path: Path) -> None:
    """Retain a protocol failure without normalizing or admitting malformed source findings."""
    run = _assessed_pr.__wrapped__(tmp_path)
    result_path = run / "result.json"
    result = json.loads(result_path.read_bytes())
    result["metadata"]["review_findings"] = [
        {
            "id": "F1",
            "severity": "high",
            "title": "Missing input guard",
            "summary": "The boundary accepts unchecked input.",
            "required_change": "Validate the boundary input.",
            "evidence": ["widget.txt:1"],
            "closure_evidence": ["A boundary regression proves rejection."],
            "authors": ["Main reviewer"],
        }
    ]
    result_path.write_text(json.dumps(result), encoding="utf-8", newline="\n")
    before = {path.relative_to(run): path.read_bytes() for path in run.rglob("*") if path.is_file()}
    diagnostic = "review-validation-failed:review-finding-closure_evidence-invalid:1"
    for arguments, expected_stderr in (
        (
            ["--complete-run", str(run), "--parent-thread-id", "thread"],
            f"Review handoff blocked: {diagnostic}\nRetained evidence: {run}. Review not complete.\n",
        ),
        (["--result", str(result_path)], diagnostic + "\n"),
    ):
        rejected = subprocess.run([sys.executable, str(FINDER), *arguments], capture_output=True, text=True)
        assert rejected.returncode == 1
        assert rejected.stdout == ""
        assert rejected.stderr == expected_stderr
    assert {path.relative_to(run): path.read_bytes() for path in run.rglob("*") if path.is_file()} == before


@pytest.mark.integration
@pytest.mark.parametrize("failure", ["none", "missing-home", "malformed-proof"])
def test_native_assembly_finalization_and_separate_intake_preserve_proof(tmp_path: Path, failure: str) -> None:
    """Carry nonempty native producer output through promotion and intake without reconstructing its bindings."""
    run = preparation._review_inputs(tmp_path)
    routing_path = run / "review-routing.json"
    routing = json.loads(routing_path.read_bytes())
    routing.update(independent_review_required=False, independence_requirement_evidence=None)
    routing_path.write_text(json.dumps(routing), encoding="utf-8", newline="\n")
    prepared = preparation._prepare(run)
    assert prepared.returncode == 0, prepared.stderr
    run, home, children = preparation._assembly_evidence(tmp_path, prepared_run=run)
    assembled = preparation._assemble(run, home)
    assert assembled.returncode == 0, assembled.stderr
    _finalize_native_review(tmp_path, run, home, children, failure)


def _finalize_native_review(
    tmp_path: Path, run: Path, home: Path, children: dict[str, Path], failure: str = "none"
) -> None:
    """Prove promotion, complete discovery, and intake preserve assembled native reviewer evidence."""
    manifest_path = run / "specialist-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    summary = json.loads((run / "inspection-summary.json").read_bytes())
    plan = (
        json.loads((run / "batch-inventory.json").read_bytes())["plan"]
        if manifest.get("manifest_kind") == "batched-review"
        else json.loads((run / "inspection-plan.json").read_bytes())
    )
    assert manifest["passes"] and all(item["mode"] == "inspection" for item in manifest["passes"])
    assert {item["role"] for item in manifest["passes"]} == set(children)
    validator = _module(PLUGIN_ROOT / "skills/code-review/validate_artifacts.py")
    gap = "Synthetic offline native receipts; no live reviewer launched."
    metadata = {
        "scope": "working-tree",
        "risk_tier": json.loads((run / "review-routing.json").read_bytes())["risk_tier"],
        "finding_records_version": 1,
        "review_findings": [],
        "operational_blockers": [],
        "review_decision": {
            "recommendation": "accept-as-is",
            "summary": "Frozen fixture source inspected.",
            "rationale": "No findings in retained fixture responses.",
        },
        "reviewer_assessments": [
            {
                "role": validator._readable_review_role(item["role"]),
                "rating": validator._retained_reviewer_rating(
                    run / item["output_path"], local_reviewer_wave=False, main=False, role=item["role"]
                ),
                "evidence": item["output_path"],
            }
            for item in manifest["passes"]
        ],
        "specialist_manifest": "specialist-manifest.json",
        "specialist_passes": manifest["passes"],
        "review_run_id": manifest["review_run_id"],
        "review_input_sha256": manifest["review_input_sha256"],
        "execution_mode": summary["actual_mode"],
        "execution_evidence_level": summary["evidence_level"],
        "execution_observed_controls": summary["observed_controls"],
        "write_parallel_eligible": False,
        "independence_required": plan["independent_review_required"],
        "independence_requirement_evidence": plan["independence_requirement_evidence"],
        "independence_satisfied": summary["independence_satisfied"],
        "fanout_substituted": False,
        "confidence_gaps": [gap],
        "confidence_gap_closures": [{"gap": gap, "status": "unresolved", "rationale": "Offline regression boundary."}],
        "confidence_recovery": {
            "initial_confidence": 0.95,
            "final_confidence": 0.95,
            "status": "fair",
            "evidence": ["Retained fixture wave assembled."],
            "recovery_actions": ["Revalidate native proof at finalization and intake."],
            "remaining_limits": [gap],
        },
    }
    if manifest.get("manifest_kind") == "batched-review":
        metadata["source_findings"] = manifest["source_findings"]
    (run / "review-notes.md").write_text(
        "\n\n".join(f"## {section}\n\nFrozen fixture source inspected." for section in validator.REQUIRED_SECTIONS),
        encoding="utf-8",
        newline="\n",
    )
    checks = []
    for gate in ("lint", "format", "types", "tests", "review"):
        for suffix in ("command", "stdout", "stderr"):
            (run / f"{gate}.{suffix}.txt").write_bytes(b"")
        check = {
            "id": gate,
            "status": "not-applicable",
            "exit_code": 0,
            "duration_seconds": 0.0,
            "command_path": f"{gate}.command.txt",
            "stdout": f"{gate}.stdout.txt",
            "stderr": f"{gate}.stderr.txt",
            "reason": "Minimal fixture source declares no command for this gate.",
        }
        if gate == "review":
            check.update(status="pass")
            check.pop("reason")
            (run / check["stdout"]).write_bytes(b"Retained fixture responses inspected.\n")
        checks.append(check)
    (run / "gates.json").write_text(
        json.dumps({"status": "pass", "checks_failed": [], "checks": checks}), encoding="utf-8", newline="\n"
    )
    fields = [
        ("Scope", "working-tree"),
        ("Revision", "diff sha256:" + manifest["review_input_sha256"]),
        ("CI", "unavailable"),
        ("Type", "fix"),
        ("Suggestion", "approve"),
    ]
    handoff = {
        "schema_version": 1,
        "presentation_version": 3,
        "skill": "code-review",
        "branch": "assessed",
        "outcome": {"title": "Review Decision", "summary": "Recommendation: accept-as-is."},
        "tables": [
            {
                "heading": "Review Snapshot",
                "columns": ["Field", "Value"],
                "reviewers": metadata["reviewer_assessments"],
                "summary": metadata["review_decision"]["summary"],
                "rows": [
                    {"id": f"S{index}", "cells": list(pair), "source_ids": ["snapshot:" + pair[0]]}
                    for index, pair in enumerate(fields)
                ],
            }
        ],
        "source_records": [{"id": "snapshot:" + field, "evidence": "review-notes.md"} for field, _ in fields],
        "source_coverage": {
            "source_records_total": 5,
            "represented_source_records_total": 5,
            "omitted_source_records_total": 0,
        },
        "remaining": [],
        "next_steps": [],
        "caller_contract": None,
    }
    drafts = tmp_path / "drafts"
    drafts.mkdir()
    metadata_path, handoff_path = drafts / "metadata.json", drafts / "handoff.json"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8", newline="\n")
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8", newline="\n")
    unrelated_home = tmp_path / "unrelated-codex-home"
    unrelated_home.mkdir()
    runtime_env = {**os.environ, "CODEX_HOME": str(unrelated_home)}
    finalizer_path = PLUGIN_ROOT / "shared/remediation_finalize.py"
    if failure == "missing-home":
        # The copied faulty helper preserves every operation except forwarding the required evidence location.
        original = finalizer_path.read_text(encoding="utf-8")
        forwarding = '    if arguments.codex_home:\n        command += ["--codex-home", str(arguments.codex_home)]\n'
        assert original.count(forwarding) == 1
        altered = original.replace(forwarding, "")
        altered = altered.replace(
            "SHARED_DIRECTORY = Path(__file__).resolve().parent",
            f"SHARED_DIRECTORY = Path({str(finalizer_path.parent)!r})",
        )
        finalizer_path = tmp_path / "faulty_finalizer.py"
        finalizer_path.write_text(altered, encoding="utf-8", newline="\n")
    elif failure == "malformed-proof":
        child = children["challenger"]
        rows = [json.loads(line) for line in child.read_text(encoding="utf-8").splitlines()]
        calls = [row for row in rows if row.get("payload", {}).get("type") == "custom_tool_call"]
        calls[1]["payload"]["input"] += "\nUnrequested execution."
        preparation._write_jsonl(child, rows)
    producer_files = [path for path in run.rglob("*") if path.is_file()]
    producer_files += [path for path in home.rglob("*") if path.is_file()]
    before = {path: path.read_bytes() for path in producer_files}
    result_path = run / "result.json"
    assert not result_path.exists() and not (run / "result.candidate.json").exists()
    finalized = subprocess.run(
        [
            sys.executable,
            str(finalizer_path),
            "finalize",
            "--skill",
            "code-review",
            "--run",
            str(run),
            "--metadata",
            str(metadata_path),
            "--handoff",
            str(handoff_path),
            "--status",
            "pass",
            "--confidence",
            "0.95",
            "--artifact-path",
            str(result_path),
            "--parent-thread-id",
            manifest["parent_thread_id"],
            "--codex-home",
            str(home),
            "--promote",
        ],
        env=runtime_env,
        capture_output=True,
        text=True,
        check=False,
    )
    outcome = json.loads(finalized.stdout)
    completed = subprocess.run(
        [
            sys.executable,
            str(FINDER),
            "--complete-run",
            str(run),
            "--parent-thread-id",
            manifest["parent_thread_id"],
            "--codex-home",
            str(home),
        ],
        capture_output=True,
        check=False,
    )
    if failure != "none":
        assert finalized.returncode == 1 and outcome["promoted"] is False, outcome
        assert outcome["steps"][-1]["step"] == "review-validate", outcome
        expected = {
            "malformed-proof": "review-inspection-context-read-call-mismatch:challenger:2",
            "missing-home": "provenance-rollout-count:parent:0",
            "conditional-requirement": "independent-review-required-for-pass:",
        }[failure]
        assert expected in json.dumps(outcome), outcome
        assert not result_path.exists()
        assert completed.returncode == 1 and completed.stdout == b""
    else:
        assert finalized.returncode == 0 and outcome["promoted"] is True, outcome
        assert outcome["result"] == str(result_path)
        result = json.loads(result_path.read_bytes())
        assert result["metadata"]["specialist_passes"] == manifest["passes"]
        assert completed.returncode == 0, completed.stderr.decode()
        assert completed.stdout == (run / "final.md").read_bytes()
        after_finalization = {path: path.read_bytes() for path in run.rglob("*") if path.is_file()}
        admitted = subprocess.run(
            [
                sys.executable,
                str(FINDER),
                "--result",
                str(result_path),
                "--parent-thread-id",
                manifest["parent_thread_id"],
                "--codex-home",
                str(home),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert admitted.returncode == 0, admitted.stderr
        assert admitted.stdout == str(result_path) + "\n"
        assert {path: path.read_bytes() for path in after_finalization} == after_finalization
    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.integration
@pytest.mark.parametrize("batched", [False, True])
def test_fast_native_review_completes_and_enters_intake(tmp_path: Path, batched: bool) -> None:
    """Carry authentic fast independent children through every completion and consumer gate."""
    if batched:
        run, home = batches._completed_batches(tmp_path, fast_reviewers=True, independent_required=True)
        assembled = batches._batch_command(run, "assemble-batches", home)
        final = json.loads((run / "batches/interactions/specialist-manifest.json").read_bytes())
        children = {
            item["role"]: home / "sessions" / f"rollout-{item['attempts'][0]['agent_thread_id']}.jsonl"
            for item in final["passes"]
        }
    else:
        run = preparation._review_inputs(tmp_path)
        routing_path = run / "review-routing.json"
        routing = json.loads(routing_path.read_bytes())
        routing.update(
            independent_review_required=True,
            independence_requirement_evidence="The request requires independent source inspection.",
        )
        routing_path.write_text(json.dumps(routing), encoding="utf-8", newline="\n")
        prepared = preparation._prepare(run)
        assert prepared.returncode == 0, prepared.stderr
        run, home, children = preparation._assembly_evidence(tmp_path, prepared_run=run, active_limit=1)
        assembled = preparation._assemble(run, home)
    assert assembled.returncode == 0, assembled.stderr
    summary = json.loads((run / "inspection-summary.json").read_bytes())
    assert summary["actual_mode"] == "independent-spawned"
    assert summary["capacity_limited"] is False
    assert summary["independence_required"] is True and summary["independence_satisfied"] is True
    _finalize_native_review(tmp_path, run, home, children)


@pytest.mark.integration
@pytest.mark.parametrize("batched", [False, True])
def test_fast_conditional_review_completes_with_existing_policy(tmp_path: Path, batched: bool) -> None:
    """Complete conditional-only native exposure and later batch waves without rewriting independence policy."""
    if batched:
        run, home = batches._completed_batches(
            tmp_path, fast_reviewers=True, independent_required=True, conditional_tail=True
        )
        schedule = json.loads((run / "batch-dispatch.json").read_bytes())
        conditional_waves = []
        for wave in schedule["waves"]:
            directory = run / wave["directory"]
            manifest = json.loads((directory / "specialist-manifest.json").read_bytes())
            if {item["role"] for item in manifest["passes"]} == {"doc-scribe", "web-explorer"}:
                summary = json.loads((directory / "inspection-summary.json").read_bytes())
                assert summary["actual_mode"] == "independent-spawned"
                assert summary["independence_satisfied"] is False
                conditional_waves.append(wave["wave"])
        assert conditional_waves and min(conditional_waves) > 1
        assembled = batches._batch_command(run, "assemble-batches", home)
        final = json.loads((run / "batches/interactions/specialist-manifest.json").read_bytes())
        validator = _module(PLUGIN_ROOT / "skills/code-review/validate_artifacts.py")
        children = {
            item["role"]: validator._find_rollout(home, item["attempts"][0]["agent_thread_id"])
            for item in final["passes"]
        }
    else:
        run = preparation._conditional_review_inputs(tmp_path)
        prepared = preparation._prepare(run)
        assert prepared.returncode == 0, prepared.stderr
        run, home, children = preparation._assembly_evidence(tmp_path, prepared_run=run, active_limit=1)
        assembled = preparation._assemble(run, home)
    assert assembled.returncode == 0, assembled.stderr
    summary = json.loads((run / "inspection-summary.json").read_bytes())
    assert summary["actual_mode"] == "independent-spawned"
    assert summary["independence_required"] is batched and summary["independence_satisfied"] is batched
    _finalize_native_review(tmp_path, run, home, children)


@pytest.mark.integration
def test_conditional_native_roster_does_not_satisfy_explicit_core_independence(tmp_path: Path) -> None:
    """Keep the existing explicit independence closure requirement separate from native roster admission."""
    run = preparation._conditional_review_inputs(tmp_path)
    routing_path = run / "review-routing.json"
    routing = json.loads(routing_path.read_bytes())
    routing.update(
        independent_review_required=True,
        independence_requirement_evidence="The request explicitly requires independent source inspection.",
    )
    routing_path.write_text(json.dumps(routing), encoding="utf-8", newline="\n")
    prepared = preparation._prepare(run)
    assert prepared.returncode == 0, prepared.stderr
    run, home, children = preparation._assembly_evidence(tmp_path, prepared_run=run, active_limit=1)
    assembled = preparation._assemble(run, home)
    assert assembled.returncode == 0, assembled.stderr
    summary = json.loads((run / "inspection-summary.json").read_bytes())
    assert summary["independence_required"] is True and summary["independence_satisfied"] is False
    _finalize_native_review(tmp_path, run, home, children, "conditional-requirement")


def test_review_and_remediation_share_evidence_only_recovery_contract() -> None:
    """Prevent a needs-work review from turning into a no-op for non-code obligations."""
    review = (PLUGIN_ROOT / "skills/code-review/SKILL.md").read_text(encoding="utf-8")
    remediate = (PLUGIN_ROOT / "skills/code-remediate/SKILL.md").read_text(encoding="utf-8")
    for contract in (review, remediate):
        assert "No source changes identified; required verification remains" in contract
    assert "concrete recovery action, responsible owner, and observable closure evidence" in review
    assert "do not request impossible retroactive proof" in review
    assert "instead of ending with `nothing to implement`" in remediate
    assert "Parent substitutes do not establish independence" in remediate
    assert "If original-run provenance itself is required" in remediate
    assert "Original-head CI does not prove a new merge commit passed hosted CI" in remediate
    assert "Count fresh validation as evidence-only closure, never `implemented`" in remediate

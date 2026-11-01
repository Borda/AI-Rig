"""Exercise immutable review completion and separate remediation intake boundaries."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

import pytest
import test_review_batches as batches
import test_review_prepare as preparation
from _skill_text import read_with_references
from test_review_completion_gate import FINDER, PLUGIN_ROOT, _assessed_pr, _finalize_parent_fallback, _module

_REMEDIATION_FINALIZER = PLUGIN_ROOT / "shared/remediation_finalize.py"
_PR_URL = "https://github.com/acme/widgets/pull/123"


def _script(script: Path, *arguments: object, text: bool = True, **options: object) -> subprocess.CompletedProcess:
    """Run a shipped helper script with the test interpreter, capturing output and never raising on failure."""
    command = [sys.executable, str(script), *map(str, arguments)]
    return subprocess.run(command, capture_output=True, text=text, check=False, **options)


def _remediation_finalize(
    run: Path, metadata: Path, handoff: Path, status: str, result: Path, *extra: object, **options: object
) -> subprocess.CompletedProcess:
    """Finalize a code-review run through the remediation finalizer with the standard 0.95 confidence."""
    arguments = ["finalize", "--skill", "code-review", "--run", run, "--metadata", metadata, "--handoff", handoff]
    arguments += ["--status", status, "--confidence", "0.95", "--artifact-path", result, *extra]
    return _script(options.pop("finalizer", _REMEDIATION_FINALIZER), *arguments, **options)


def _file_bytes(root: Path) -> dict[Path, bytes]:
    """Return the bytes of every file below a directory, keyed by its path relative to that directory."""
    return {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}


@pytest.mark.integration
def test_coverage_waiver_completes_separate_fallback_without_synthetic_actions(tmp_path: Path) -> None:
    """A waived-coverage revision completes as a limited, action-free review without touching the original.

    The original run requires independent review and is refused at completion. A revised run over the same diff records
    the user's waiver, drops the type-check source record and still reports the failed type check.
    """
    original = _assessed_pr.__wrapped__(tmp_path / "original")
    rejected = _finalize_parent_fallback(original, independent_required=True)
    frozen = _file_bytes(original)
    fallback = _assessed_pr.__wrapped__(tmp_path / "revised")
    assert (fallback / "diff.patch").read_bytes() == (original / "diff.patch").read_bytes()
    assert _finalize_parent_fallback(fallback).returncode == 0
    metadata_path = fallback / "fallback-drafts/metadata.json"
    handoff_path = fallback / "fallback-drafts/handoff.json"
    metadata = json.loads(metadata_path.read_bytes())
    handoff = json.loads(handoff_path.read_bytes())
    metadata["operational_blockers"] = []
    metadata["review_decision"]["summary"] = "No source changes identified; verification limited"
    metadata["review_decision"]["rationale"] = (
        "The user waived independent coverage; configured checks retain their failures."
    )
    handoff["tables"] = handoff["tables"][:1]
    handoff["tables"][0]["summary"] = metadata["review_decision"]["summary"]
    handoff["source_records"] = [record for record in handoff["source_records"] if record["id"] != "gate:types"]
    total = len(handoff["source_records"])
    handoff["source_coverage"].update(source_records_total=total, represented_source_records_total=total)
    notes = fallback / "review-notes.md"
    notes.write_text(
        notes.read_text(encoding="utf-8").split("## Review Findings and Merge Blocks")[0]
        + "\n## Revised scope\n\nUser decision: skip independent reviewers for this unchanged revision. "
        + "Retain parent inspection, source identity, configured checks and every artifact/completion gate. "
        + f"Original plan retained: {original / 'parent-inspection-plan.json'}. "
        + "Independent coverage was not performed; the type check remains failed.\n",
        encoding="utf-8",
        newline="\n",
    )
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8")

    finalized = _remediation_finalize(
        fallback,
        metadata_path,
        handoff_path,
        "fail",
        fallback / "result.json",
        "--parent-thread-id",
        "thread",
        "--promote",
    )
    completed = _script(FINDER, "--complete-run", fallback, "--parent-thread-id", "thread")
    discovered = _script(FINDER, "--target", _PR_URL, "--reports-dir", fallback.parent.parent)

    assert rejected.returncode != 0
    assert "independent-review-required-for-completion" in rejected.stdout + rejected.stderr
    assert finalized.returncode == 0, finalized.stdout + finalized.stderr
    result = json.loads((fallback / "result.json").read_bytes())
    assert result["metadata"]["review_decision"]["recommendation"] is None
    assert result["metadata"]["independence_required"] is False
    assert result["metadata"]["independence_satisfied"] is False
    assert result["metadata"]["operational_blockers"] == []
    assert result["checks_failed"] == ["types"]
    assert completed.returncode == 0, completed.stderr
    assert "Review Findings and Merge Blocks" not in completed.stdout
    assert discovered.returncode == 0, discovered.stderr
    assert discovered.stdout.strip() == str(fallback / "result.json")
    assert _file_bytes(original) == frozen


@pytest.mark.integration
def test_parent_fallback_promotes_and_preserves_failed_gate_obligations_at_intake(tmp_path: Path) -> None:
    """A failed parent review carries its gate obligations through finalization, discovery and intake.

    A parent-only review with a failing type check is promoted. The remediation consumer must then admit the result with
    both the failed gate and the unmet independent-coverage gap selectable, and must refuse a report whose items drop
    either obligation.
    """
    run = _assessed_pr.__wrapped__(tmp_path)
    finalized = _finalize_parent_fallback(run)
    assert finalized.returncode == 0, finalized.stdout + finalized.stderr
    result_path = run / "result.json"
    result = json.loads(result_path.read_bytes())
    before = _file_bytes(run)

    completed = _script(FINDER, "--complete-run", run, "--parent-thread-id", "thread", text=False)
    admissions = [
        _script(FINDER, "--target", _PR_URL, "--reports-dir", run.parent.parent),
        _script(FINDER, "--result", result_path),
    ]

    assert json.loads(finalized.stdout)["promoted"] is True
    assert result["status"] == "fail"
    assert result["checks_failed"] == ["types"]
    assert result["metadata"]["execution_mode"] == "serial-fallback"
    assert result["metadata"]["independence_required"] is False
    assert result["metadata"]["independence_satisfied"] is False
    passes = result["metadata"]["specialist_passes"]
    assert {item["role"] for item in passes} == {"challenger", "qa-specialist"}
    assert all(item["mode"] == "substituted" and not item.get("attempts") for item in passes)
    assert completed.returncode == 0, completed.stderr.decode()
    assert completed.stdout == before[Path("final.md")]
    assert [(item.returncode, item.stdout, item.stderr) for item in admissions] == [(0, f"{result_path}\n", "")] * 2

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
    assert _file_bytes(run) == before

    items[0]["sources"][0]["body"] = blocker_body.removesuffix("\ntypes")
    with pytest.raises(SystemExit, match="code-remediate-report-obligation-omitted:checks_failed"):
        validator._validate_code_remediate_report_coverage(metadata, consumer)
    items[0]["sources"][0]["body"] = blocker_body
    items.pop()
    with pytest.raises(SystemExit, match="code-remediate-report-obligation-omitted:confidence_gaps"):
        validator._validate_code_remediate_report_coverage(metadata, consumer)


@pytest.mark.integration
def test_completed_review_is_admitted_without_rewriting_producer_evidence(tmp_path: Path) -> None:
    """Producer proof stays byte-exact when a separate consumer validates its result.

    An assessed review is completed and then admitted by result path; neither step may change any file in the run
    directory, and the completion output must be exactly the retained final handoff.
    """
    run = _assessed_pr.__wrapped__(tmp_path)
    before = _file_bytes(run)

    completed = _script(FINDER, "--complete-run", run, "--parent-thread-id", "thread", text=False)
    admitted = _script(FINDER, "--result", run / "result.json")

    assert completed.returncode == 0, completed.stderr.decode()
    assert completed.stdout == before[Path("final.md")]
    assert admitted.returncode == 0, admitted.stderr
    assert admitted.stdout == str(run / "result.json") + "\n"
    assert admitted.stderr == ""
    assert _file_bytes(run) == before


@pytest.mark.integration
def test_malformed_closure_proof_blocks_completion_and_consumer_with_same_diagnostic(tmp_path: Path) -> None:
    """A protocol failure is retained and reported identically by completion and by the result consumer.

    A finding whose closure evidence has the wrong shape is written into an assessed review's result. Completion and
    result admission must both exit 1 with the same diagnostic, print nothing to stdout and leave the run intact.
    """
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
    result["metadata"]["review_validation"]["candidates"] = [
        {
            "id": "F1",
            "severity": "high",
            "claim": "The boundary accepts unchecked input.",
            "source_revision": "head:" + json.loads((run / "pr-routing.json").read_bytes())["head_oid"],
            "contract": "Reject unchecked input.",
            "trigger": "Pass unchecked input to the boundary.",
            "confirmation_evidence": "widget.txt:1",
            "falsification_evidence": "Inspect existing input guards.",
            "counterevidence": [],
            "check": "Inspected the frozen boundary.",
            "result": "The guard is absent.",
            "environment": "Frozen source inspection",
            "limits": [],
            "disposition": "confirmed",
            "reason": "The input must be validated.",
            "feasible_checks_complete": True,
            "missing_prerequisite": "",
            "owner": "review owner",
            "next_check": "Run the boundary regression.",
            "closure_condition": "The boundary regression passes.",
        }
    ]
    result_path.write_text(json.dumps(result), encoding="utf-8", newline="\n")
    before = _file_bytes(run)
    diagnostic = f"review-validation-failed:{result_path}:review-finding-closure_evidence-invalid:1"

    completion = _script(FINDER, "--complete-run", run, "--parent-thread-id", "thread")
    admission = _script(FINDER, "--result", result_path)

    blocked = f"Review handoff blocked: {diagnostic}\nRetained evidence: {run}. Review not complete.\n"
    assert [(item.returncode, item.stdout, item.stderr) for item in (completion, admission)] == [
        (1, "", blocked),
        (1, "", diagnostic + "\n"),
    ]
    assert _file_bytes(run) == before


@dataclass
class _Finalization:
    """A fully assembled native review run staged for remediation finalization."""

    tmp_path: Path
    run: Path
    home: Path
    children: dict[str, Path]
    manifest: dict[str, object]
    metadata_path: Path
    handoff_path: Path
    finalizer: Path = _REMEDIATION_FINALIZER
    before: dict[Path, bytes] = field(default_factory=dict)

    @property
    def result_path(self) -> Path:
        """Location where finalization is expected to promote the review result."""
        return self.run / "result.json"


def _set_routing(run: Path, **changes: object) -> None:
    """Update the frozen review routing of a prepared run in place."""
    routing_path = run / "review-routing.json"
    routing = json.loads(routing_path.read_bytes())
    routing.update(changes)
    routing_path.write_text(json.dumps(routing), encoding="utf-8", newline="\n")


def _prepare_and_assemble(tmp_path: Path, run: Path, **evidence_options: object) -> tuple[Path, Path, dict[str, Path]]:
    """Prepare a routed run, record native reviewer evidence for it and assemble the specialist manifest."""
    prepared = preparation._prepare(run)
    assert prepared.returncode == 0, prepared.stderr
    run, home, children = preparation._assembly_evidence(tmp_path, prepared_run=run, **evidence_options)
    assembled = preparation._assemble(run, home)
    assert assembled.returncode == 0, assembled.stderr
    return run, home, children


def _stage_finalization(tmp_path: Path, run: Path, home: Path, children: dict[str, Path]) -> _Finalization:
    """Write the review metadata, notes, gates and handoff drafts for an assembled native review run."""
    manifest = json.loads((run / "specialist-manifest.json").read_bytes())
    summary = json.loads((run / "inspection-summary.json").read_bytes())
    batched = manifest.get("manifest_kind") == "batched-review"
    plan_path = run / "batch-inventory.json" if batched else run / "inspection-plan.json"
    plan = json.loads(plan_path.read_bytes())["plan"] if batched else json.loads(plan_path.read_bytes())
    assert manifest["passes"]
    assert all(item["mode"] == "inspection" for item in manifest["passes"])
    assert {item["role"] for item in manifest["passes"]} == set(children)
    validator = _module(PLUGIN_ROOT / "skills/code-review/validate_artifacts.py")
    gap = "Synthetic offline native receipts; no live reviewer launched."
    metadata = {
        "scope": "working-tree",
        "risk_tier": json.loads((run / "review-routing.json").read_bytes())["risk_tier"],
        "finding_records_version": 2,
        "review_validation": {
            "schema_version": 1,
            "source_rating_version": 1,
            "source_verdict": "accept-as-is",
            "verification_readiness": "complete",
            "candidates": [],
        },
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
            for item in sorted(manifest["passes"], key=lambda item: item["role"] != "sw-engineer")
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
    metadata |= {"source_findings": manifest["source_findings"]} if batched else {}
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
        checks.append(check)
    checks[-1].update(status="pass")
    checks[-1].pop("reason")
    (run / checks[-1]["stdout"]).write_bytes(b"Retained fixture responses inspected.\n")
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
                "source_rating_version": 1,
                "verification_readiness": "complete",
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
    # An empty PATH hides any codemap binary, so finalization records the context as absent on every host.
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    probe_env = {key: value for key, value in os.environ.items() if key != "CODEMAP_BIN"} | {"PATH": str(empty_path)}
    probe = _script(
        PLUGIN_ROOT / "shared/codemap_adapter.py",
        *["context", "--category", "review", "--diff-file", run / "diff.patch"],
        *["--out", run / "codemap-context.json"],
        env=probe_env,
    )
    assert probe.returncode == 0, probe.stderr
    assert json.loads((run / "codemap-context.json").read_bytes())["status"] == "absent"
    return _Finalization(tmp_path, run, home, children, manifest, metadata_path, handoff_path)


def _finalize(
    staged: _Finalization,
) -> tuple[subprocess.CompletedProcess, dict[str, object], subprocess.CompletedProcess]:
    """Snapshot producer files, finalize and promote the staged run, then ask the finder to complete it."""
    run, result_path = staged.run, staged.result_path
    assert not result_path.exists()
    assert not (run / "result.candidate.json").exists()
    producer_files = [path for root in (run, staged.home) for path in root.rglob("*") if path.is_file()]
    staged.before = {path: path.read_bytes() for path in producer_files}
    unrelated_home = staged.tmp_path / "unrelated-codex-home"
    unrelated_home.mkdir()
    parent = staged.manifest["parent_thread_id"]
    finalized = _remediation_finalize(
        run,
        staged.metadata_path,
        staged.handoff_path,
        "pass",
        result_path,
        *["--parent-thread-id", parent, "--codex-home", staged.home, "--promote"],
        finalizer=staged.finalizer,
        env={**os.environ, "CODEX_HOME": str(unrelated_home)},
    )
    outcome = json.loads(finalized.stdout)
    completed = _script(
        FINDER, "--complete-run", run, "--parent-thread-id", parent, "--codex-home", staged.home, text=False
    )
    return finalized, outcome, completed


def _assert_producer_evidence_preserved(staged: _Finalization) -> None:
    """Assert that finalization left every producer file untouched except the appended reviewed-source note."""
    source = json.loads((staged.run / "local-source/source-snapshot.json").read_bytes())
    expected = dict(staged.before)
    expected[staged.run / "review-notes.md"] += (
        f"\n\n## Reviewed Source\n\nReviewed commit: {source['revision']} (working-tree base)\n".encode()
    )
    assert {path: path.read_bytes() for path in expected} == expected


def _assert_accepted(staged: _Finalization, finalized: object, outcome: dict[str, object], completed: object) -> None:
    """Assert promotion, complete-run discovery and separate intake all succeeded on the staged run."""
    result_path = staged.result_path
    assert finalized.returncode == 0, outcome
    assert outcome["promoted"] is True, outcome
    assert outcome["result"] == str(result_path)
    assert json.loads(result_path.read_bytes())["metadata"]["specialist_passes"] == staged.manifest["passes"]
    assert completed.returncode == 0, completed.stderr.decode()
    assert completed.stdout == (staged.run / "final.md").read_bytes()
    after_finalization = {path: path.read_bytes() for path in staged.run.rglob("*") if path.is_file()}
    admitted = _script(
        FINDER,
        *["--result", result_path, "--parent-thread-id", staged.manifest["parent_thread_id"]],
        *["--codex-home", staged.home],
    )
    assert admitted.returncode == 0, admitted.stderr
    assert admitted.stdout == str(result_path) + "\n"
    assert {path: path.read_bytes() for path in after_finalization} == after_finalization
    _assert_producer_evidence_preserved(staged)


def _assert_refused(
    staged: _Finalization, finalized: object, outcome: dict[str, object], completed: object, *, diagnostic: str
) -> None:
    """Assert finalization stopped at review validation with the diagnostic and promoted nothing."""
    assert finalized.returncode == 1, outcome
    assert outcome["promoted"] is False, outcome
    assert outcome["steps"][-1]["step"] == "review-validate", outcome
    assert diagnostic in json.dumps(outcome), outcome
    assert not staged.result_path.exists()
    assert completed.returncode == 1
    assert completed.stdout == b""
    _assert_producer_evidence_preserved(staged)


def _keep_finalizer(staged: _Finalization) -> None:
    """Leave the staged run untouched so finalization runs against intact evidence."""


def _drop_codex_home_forwarding(staged: _Finalization) -> None:
    """Swap in a copy of the finalizer that forgets to forward `--codex-home` to the review validator."""
    original = _REMEDIATION_FINALIZER.read_text(encoding="utf-8")
    forwarding = '    if arguments.codex_home:\n        command += ["--codex-home", str(arguments.codex_home)]\n'
    assert original.count(forwarding) == 1
    altered = original.replace(forwarding, "")
    altered = altered.replace(
        "SHARED_DIRECTORY = Path(__file__).resolve().parent",
        f"SHARED_DIRECTORY = Path({str(_REMEDIATION_FINALIZER.parent)!r})",
    )
    staged.finalizer = staged.tmp_path / "faulty_finalizer.py"
    staged.finalizer.write_text(altered, encoding="utf-8", newline="\n")


def _malform_challenger_proof(staged: _Finalization) -> None:
    """Append an unrequested execution to the challenger child's second custom tool call."""
    child = staged.children["challenger"]
    rows = [json.loads(line) for line in child.read_text(encoding="utf-8").splitlines()]
    calls = [row for row in rows if row.get("payload", {}).get("type") == "custom_tool_call"]
    calls[1]["payload"]["input"] += "\nUnrequested execution."
    preparation._write_jsonl(child, rows)


_NATIVE_DAMAGE = {
    "none": _keep_finalizer,
    "missing-home": _drop_codex_home_forwarding,
    "malformed-proof": _malform_challenger_proof,
}
_NATIVE_CHECK = {
    "none": _assert_accepted,
    "missing-home": partial(_assert_refused, diagnostic="provenance-rollout-count:parent:0"),
    "malformed-proof": partial(_assert_refused, diagnostic="review-inspection-context-read-call-mismatch:challenger:2"),
}


@pytest.mark.integration
@pytest.mark.parametrize("failure", ["none", "missing-home", "malformed-proof"])
def test_native_assembly_finalization_and_separate_intake_preserve_proof(tmp_path: Path, failure: str) -> None:
    """Native producer output is promoted and admitted unchanged, and broken proof is refused at finalization.

    A nonempty native review is assembled without reconstructing its bindings. Intact evidence must promote and be
    admitted by the finder; a finalizer that drops `--codex-home`, or a child transcript with an unrequested execution,
    must stop at review validation with a specific diagnostic. Other suites build this producer run by calling this
    function directly with `(tmp_path, "none")`.
    """
    run = preparation._review_inputs(tmp_path)
    _set_routing(run, independent_review_required=False, independence_requirement_evidence=None)
    staged = _stage_finalization(tmp_path, *_prepare_and_assemble(tmp_path, run))
    _NATIVE_DAMAGE[failure](staged)

    finalized, outcome, completed = _finalize(staged)

    _NATIVE_CHECK[failure](staged, finalized, outcome, completed)


def _fast_single_run(tmp_path: Path) -> tuple[Path, Path, dict[str, Path]]:
    """Assemble an unbatched review whose fast reviewers satisfy an explicit independence requirement."""
    run = preparation._review_inputs(tmp_path)
    _set_routing(
        run,
        independent_review_required=True,
        independence_requirement_evidence="The request requires independent source inspection.",
    )
    return _prepare_and_assemble(tmp_path, run, active_limit=1)


def _batched_children(run: Path, home: Path, locate: Callable[[str], Path]) -> dict[str, Path]:
    """Assemble all batches and map each final reviewer role to its retained rollout transcript."""
    assembled = batches._batch_command(run, "assemble-batches", home)
    assert assembled.returncode == 0, assembled.stderr
    final = json.loads((run / "batches/interactions/specialist-manifest.json").read_bytes())
    return {item["role"]: locate(item["attempts"][0]["agent_thread_id"]) for item in final["passes"]}


def _fast_batched_run(tmp_path: Path) -> tuple[Path, Path, dict[str, Path]]:
    """Assemble a batched review whose fast reviewers satisfy an explicit independence requirement."""
    run, home = batches._completed_batches(tmp_path, fast_reviewers=True, independent_required=True)
    children = _batched_children(run, home, lambda thread: home / "sessions" / f"rollout-{thread}.jsonl")
    return run, home, children


def _conditional_single_run(tmp_path: Path) -> tuple[Path, Path, dict[str, Path]]:
    """Assemble an unbatched review whose roster is conditional-only, with no independence requirement."""
    return _prepare_and_assemble(tmp_path, preparation._conditional_review_inputs(tmp_path), active_limit=1)


def _conditional_batched_run(tmp_path: Path) -> tuple[Path, Path, dict[str, Path]]:
    """Assemble a batched review whose later waves hold only conditional roles that cannot satisfy independence."""
    run, home = batches._completed_batches(
        tmp_path, fast_reviewers=True, independent_required=True, conditional_tail=True
    )
    conditional_waves = []
    for wave in json.loads((run / "batch-dispatch.json").read_bytes())["waves"]:
        directory = run / wave["directory"]
        manifest = json.loads((directory / "specialist-manifest.json").read_bytes())
        summary = json.loads((directory / "inspection-summary.json").read_bytes())
        if {item["role"] for item in manifest["passes"]} == {"doc-scribe", "web-explorer"}:
            assert summary["actual_mode"] == "independent-spawned"
            assert summary["independence_satisfied"] is False
            conditional_waves.append(wave["wave"])
    assert conditional_waves
    assert min(conditional_waves) > 1
    validator = _module(PLUGIN_ROOT / "skills/code-review/validate_artifacts.py")
    children = _batched_children(run, home, lambda thread: validator._find_rollout(home, thread))
    return run, home, children


@pytest.mark.integration
@pytest.mark.parametrize(
    "assemble_run",
    [pytest.param(_fast_single_run, id="single"), pytest.param(_fast_batched_run, id="batched")],
)
def test_fast_native_review_completes_and_enters_intake(
    tmp_path: Path, assemble_run: Callable[[Path], tuple[Path, Path, dict[str, Path]]]
) -> None:
    """Authentic fast independent children pass every completion and consumer gate.

    A review that explicitly requires independent inspection is answered by fast native children, once unbatched and
    once batched. Its summary must report satisfied independence, and the result must promote and be admitted.
    """
    run, home, children = assemble_run(tmp_path)
    summary = json.loads((run / "inspection-summary.json").read_bytes())
    staged = _stage_finalization(tmp_path, run, home, children)

    finalized, outcome, completed = _finalize(staged)

    assert summary["actual_mode"] == "independent-spawned"
    assert summary["capacity_limited"] is False
    assert summary["independence_required"] is True
    assert summary["independence_satisfied"] is True
    _assert_accepted(staged, finalized, outcome, completed)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("assemble_run", "independent"),
    [
        pytest.param(_conditional_single_run, False, id="single"),
        pytest.param(_conditional_batched_run, True, id="batched"),
    ],
)
def test_fast_conditional_review_completes_with_existing_policy(
    tmp_path: Path, assemble_run: Callable[[Path], tuple[Path, Path, dict[str, Path]]], independent: bool
) -> None:
    """Conditional-only native exposure and later batch waves complete without rewriting independence policy.

    A single conditional review keeps independence unrequired and unsatisfied. A batch whose first wave holds the core
    reviewers satisfies the requirement even though later waves are conditional-only; both must finalize.
    """
    run, home, children = assemble_run(tmp_path)
    summary = json.loads((run / "inspection-summary.json").read_bytes())
    staged = _stage_finalization(tmp_path, run, home, children)

    finalized, outcome, completed = _finalize(staged)

    assert summary["actual_mode"] == "independent-spawned"
    assert summary["independence_required"] is independent
    assert summary["independence_satisfied"] is independent
    _assert_accepted(staged, finalized, outcome, completed)


@pytest.mark.integration
def test_conditional_native_roster_does_not_satisfy_explicit_core_independence(tmp_path: Path) -> None:
    """The explicit independence requirement stays separate from native roster admission.

    A conditional-only roster answers a request that explicitly requires independent core inspection. The summary
    reports the requirement unsatisfied, so finalization must refuse the pass instead of promoting it.
    """
    run = preparation._conditional_review_inputs(tmp_path)
    _set_routing(
        run,
        independent_review_required=True,
        independence_requirement_evidence="The request explicitly requires independent source inspection.",
    )
    run, home, children = _prepare_and_assemble(tmp_path, run, active_limit=1)
    summary = json.loads((run / "inspection-summary.json").read_bytes())
    staged = _stage_finalization(tmp_path, run, home, children)

    finalized, outcome, completed = _finalize(staged)

    assert summary["independence_required"] is True
    assert summary["independence_satisfied"] is False
    _assert_refused(staged, finalized, outcome, completed, diagnostic="independent-review-required-for-pass:")


@pytest.fixture(scope="module")
def contracts() -> dict[str, str]:
    """Read the review, remediation and shared quality-gate contracts once, with wrapping collapsed to spaces."""
    review = read_with_references(PLUGIN_ROOT / "skills/code-review/SKILL.md", "findings-and-completion.md")
    remediate = read_with_references(
        PLUGIN_ROOT / "skills/code-remediate/SKILL.md",
        "intake.md",
        "selection.md",
        "implementation.md",
        "completion.md",
    )
    shared = (PLUGIN_ROOT / "shared/quality-gates.md").read_text(encoding="utf-8")
    return {
        name: " ".join(text.split())
        for name, text in {"review": review, "remediate": remediate, "shared": shared}.items()
    }


class TestProcessLimitContracts:
    """Review producers and consumers must not regenerate declined review requests as work."""

    @pytest.mark.parametrize(
        ("document", "phrase"),
        [
            pytest.param("review", "Review limitations and additional review", id="review-limitations-section"),
            pytest.param("remediate", "Review limitations and additional review", id="remediate-limitations-section"),
            pytest.param("review", "never canonical findings, operational blockers", id="review-not-findings"),
            pytest.param("shared", "does not authorize another pass or a review/fix loop", id="no-review-loop"),
            pytest.param("shared", "declined or dismissed optional review is skipped", id="declined-review-skipped"),
            pytest.param("remediate", "selection.json.review_limitations", id="remediate-selection-field"),
            pytest.param(
                "remediate",
                "Omit it from `items`, severity counts, scope selection and final action tables",
                id="omit-from-action-tables",
            ),
            pytest.param(
                "remediate", "Never use this classification to conceal a real source defect", id="no-concealed-defect"
            ),
            pytest.param(
                "remediate",
                "Original-head CI does not prove a changed merge commit passed",
                id="original-head-ci-limit",
            ),
            pytest.param(
                "remediate",
                "fresh verification is evidence-only closure, never `implemented`",
                id="fresh-verification-not-implemented",
            ),
        ],
    )
    def test_contract_states_phrase(self, contracts: dict[str, str], document: str, phrase: str) -> None:
        """Each skill or shared gate document states the rule that keeps review limits out of findings.

        The checks read prose with wrapping collapsed, so they hold whether or not the Markdown is hard-wrapped.
        """
        assert phrase in contracts[document]

    def test_review_contract_drops_blanket_finding_requirement(self, contracts: dict[str, str]) -> None:
        """The review contract no longer demands a canonical finding for every blocking gap.

        That wording would turn declined review limits back into findings, which the limitations section forbids.
        """
        assert "Every blocking gap must have one canonical finding" not in contracts["review"]

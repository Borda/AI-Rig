"""Exercise the executable boundary between review evidence and completed final text.

Covers the finder's completion CLI, the review and shared artifact validators, the result writer and the parallel
execution preflight against an assessed pull-request run built through the real render and validation boundaries.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
FINDER = PLUGIN_ROOT / "shared" / "find-review-report.py"
PARALLEL = PLUGIN_ROOT / "shared" / "parallel_execution.py"
FINAL_HANDOFF = PLUGIN_ROOT / "shared" / "final_handoff.py"
WRITE_RESULT = PLUGIN_ROOT / "shared" / "write-result.py"
VALIDATE_SHARED = PLUGIN_ROOT / "shared" / "validate-artifacts.py"
VALIDATE_REVIEW = PLUGIN_ROOT / "skills" / "code-review" / "validate_artifacts.py"
_TERMINAL_FINAL = b"Arbitrary terminal final.\n"


def _module(path: Path) -> ModuleType:
    """Load existing fixture builders and installed helpers without import-path changes.

    The module is registered under a private name only while it executes, because dataclass and annotation resolution
    look the defining module up in ``sys.modules`` and Python 3.14 raises when it is absent.
    """
    name = f"_review_gate_{path.stem.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


@pytest.fixture(name="assessed_pr")
def _assessed_pr(tmp_path: Path) -> Path:
    """Create a complete trivial PR review through real render and validation boundaries."""
    run = tmp_path / "reports" / "pr-123" / "run-001"
    run.mkdir(parents=True)
    # Reuse collector/gate construction; keep assessed behavior visible here.
    fixture = _module(Path(__file__).with_name("test_code_review_close_contract.py"))
    result_path = fixture._write_closed_artifact(run)
    (run / "diff.patch").write_text("diff --git a/widget.txt b/widget.txt\n", encoding="utf-8", newline="\n")
    (run / "files.txt").write_text("widget.txt\n", encoding="utf-8")
    (run / "numstat.txt").write_text("1\t1\twidget.txt\n", encoding="utf-8")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    pr_path = run / "pr.json"
    pr = json.loads(pr_path.read_text(encoding="utf-8"))
    pr.update(
        title="Correct widget spelling",
        author={"login": "contributor"},
        statusCheckRollup=[{"__typename": "CheckRun", "name": "tests", "status": "COMPLETED", "conclusion": "SUCCESS"}],
    )
    pr_path.write_text(json.dumps(pr), encoding="utf-8")
    base, head = fixture.BASE_OID, fixture.HEAD_OID
    url = "https://github.com/acme/widgets/pull/123"
    remote_url = "https://github.com/acme/widgets.git"
    files = {
        "pr-routing.json": {
            "pr_number": 123,
            "pr_url": url,
            "base_identity_source": "pr_url",
            "pr_state": "OPEN",
            "base_host": "github.com",
            "base_repo": "acme/widgets",
            "local_checkout_required": True,
            "local_checkout_command": f"git checkout --detach {head}",
            "force_policy": "forbidden",
            "base_oid": base,
            "head_oid": head,
        },
        "remote-selection.json": {
            "expected": {"host": "github.com", "repository": "acme/widgets"},
            "remote": "origin",
            "remote_url": remote_url,
            "remote_ref": base,
        },
        "target-branch.json": {
            "status": "fetched",
            "remote": "origin",
            "remote_url": remote_url,
            "remote_ref": base,
            "expected_base_oid": base,
            "local_head": base,
            "expected_base_is_ancestor": True,
            "base_matches_pr_metadata": True,
            "base_relation": "matches-pr-metadata",
        },
        "pr-head-fetch.json": {
            "status": "fetched",
            "remote_ref": "FETCH_HEAD",
            "local_head": head,
            "expected_head_oid": head,
            "head_matches_pr_metadata": True,
        },
        "local-checkout.json": {
            "status": "checked-out",
            "pr_url": url,
            "command": f"git checkout --detach {head}",
            "force_policy": "forbidden",
            "head_matches_pr": True,
            "expected_head": head,
            "local_head": head,
            "diff_source": "verified-local-checkout",
            "diff_base_oid": base,
            "diff_head_oid": head,
            "diff_command": f"git diff --binary {base}...{head} --",
        },
        "worktree-preflight.json": {
            "phase": "after-checkout",
            "status": "clean",
            "current_head": head,
            "expected_head": head,
            "dirty_paths": [],
            "unmerged_paths": [],
            "pr_paths": ["widget.txt"],
            "checkout_paths": [],
            "overlapping_paths": [],
            "overlapping_pr_paths": [],
        },
        "online-review-summary.json": {"review_threads_status": "available", "review_threads_error": None},
    }
    validator = _module(PLUGIN_ROOT / "skills/code-review/validate_artifacts.py")
    signals = {name: False for name in validator.ROUTING_SIGNALS}
    tier, evidence, _ = validator.derive_mechanical_risk(run)
    files["review-routing.json"] = {
        "schema_version": 1,
        "risk_tier": tier,
        "mechanical_risk_tier": tier,
        "mechanical_risk_evidence": evidence,
        "signals": signals,
        "signal_evidence": {name: ["Trivial spelling correction."] for name in signals},
        "triggered_roles": [],
        "trigger_reasons": {},
    }
    input_hash = hashlib.sha256((run / "diff.patch").read_bytes()).hexdigest()
    files["specialist-manifest.json"] = {
        "schema_version": 3,
        "review_run_id": "test-review",
        "parent_thread_id": "thread",
        "review_input_sha256": input_hash,
        "passes": [],
    }
    for name, payload in files.items():
        (run / name).write_text(json.dumps(payload), encoding="utf-8")
    (run / "review-notes.md").write_text(
        "\n\n".join(
            f"## {section}\n\nTrivial spelling correction reviewed."
            for section in (*validator.REQUIRED_SECTIONS, "Online Review Triage")
        ),
        encoding="utf-8",
    )
    with (run / "review-notes.md").open("a", encoding="utf-8") as notes:
        notes.write(
            "\n\n## Main Reviewer Assessment\n\nSource rating version: 1\nRating: 1\nVerification readiness: complete\nRationale: The spelling change is safe.\n"
        )
    metadata = result["metadata"]
    del metadata["review_status"], metadata["close_decision"]
    gates_path = run / "gates.json"
    gates = json.loads(gates_path.read_text(encoding="utf-8"))
    tests_gate = next(check for check in gates["checks"] if check["id"] == "tests")
    approval_path = run / "tests-skip-decision.json"
    approval_path.write_text(
        json.dumps(
            {
                "decision": "skip-tests",
                "reason": tests_gate["reason"],
                "evidence": "Fixture user approves skipping code tests for this spelling-only review.",
            }
        ),
        encoding="utf-8",
    )
    (run / "checks").mkdir(exist_ok=True)
    approval = _module(PLUGIN_ROOT / "shared/gate_test_approval.py")
    tests_gate["approval"] = approval.retain_approval(approval_path, run, tests_gate["reason"])
    gates_path.write_text(json.dumps(gates), encoding="utf-8")
    metadata.update(
        {
            "review_decision": {
                "recommendation": "accept-as-is",
                "summary": "Spelling corrected.",
                "rationale": "No behavior change.",
            },
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
            "reviewer_assessments": [{"role": "Main reviewer", "rating": 1, "evidence": "review-notes.md"}],
            "specialist_manifest": str(run / "specialist-manifest.json"),
            "specialist_passes": [],
            "review_run_id": "test-review",
            "review_input_sha256": input_hash,
            "reviewed_commit_sha": head,
            "fanout_substituted": False,
            "independence_required": False,
            "independence_satisfied": False,
            "confidence_gaps": ["Synthetic offline collector evidence."],
            "confidence_gap_closures": [
                {
                    "gap": "Synthetic offline collector evidence.",
                    "status": "unresolved",
                    "rationale": "No live PR was collected.",
                }
            ],
        }
    )
    metadata["confidence_recovery"]["remaining_limits"] = ["Synthetic offline collector evidence."]
    gates = json.loads((run / "gates.json").read_text(encoding="utf-8"))
    snapshot = [
        ("PR", "[#123 — Correct widget spelling](https://github.com/acme/widgets/pull/123)"),
        ("Author", "@contributor"),
        ("CI", "passing"),
        ("Type", "docs"),
        ("Commit", head),
        ("Suggestion", "approve"),
    ]
    handoff = {
        "schema_version": 1,
        "presentation_version": 8,
        "skill": "code-review",
        "branch": "assessed",
        "outcome": {"title": "Review Decision", "summary": "Recommendation: accept-as-is."},
        "tables": [
            {
                "heading": "PR Snapshot",
                "columns": ["Field", "Value"],
                "reviewers": metadata["reviewer_assessments"],
                "summary": metadata["review_decision"]["summary"],
                "source_rating_version": 1,
                "verification_readiness": "complete",
                "rows": [
                    {"id": f"S{index}", "cells": list(row), "source_ids": [f"pr:{row[0]}"]}
                    for index, row in enumerate(snapshot)
                ],
            }
        ],
        "source_records": [{"id": f"pr:{field}", "evidence": f"pr.json#{field}"} for field, _ in snapshot],
        "source_coverage": {
            "source_records_total": len(snapshot),
            "represented_source_records_total": len(snapshot),
            "omitted_source_records_total": 0,
        },
        "verification": [
            {"check": check["id"], "status": check["status"], "evidence": check["stdout"]} for check in gates["checks"]
        ],
        "remaining": [],
        "next_steps": [],
        "confidence": {
            "score": 0.95,
            "band": "fair",
            "limits": metadata["confidence_recovery"]["remaining_limits"],
            "gaps": metadata["confidence_gap_closures"],
        },
        "artifacts": [{"label": "Result", "path": str(result_path)}],
        "caller_contract": None,
    }
    handoff_path = run / "final-handoff.json"
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
    validation = _module(PLUGIN_ROOT / "shared/final_handoff.py").render_files(
        handoff_path, run / "final.md", run / "final-handoff.validation.json"
    )
    metadata["final_handoff"] = {
        "schema_version": 1,
        "handoff_path": str(handoff_path),
        "handoff_sha256": validation["handoff_sha256"],
        "rendered_path": str(run / "final.md"),
        "rendered_sha256": validation["rendered_sha256"],
        "validation_path": str(run / "final-handoff.validation.json"),
        "branch": "assessed",
    }
    result["schema_version"] = 3
    result_path.write_text(json.dumps(result), encoding="utf-8")
    return run


def _add_substituted_broad_passes(assessed_pr: Path, tier: str, *, independent_required: bool | None = None) -> None:
    """Retain complete role-card proof for a broad parent-substitute review."""
    result_path = assessed_pr / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    routing_path = assessed_pr / "review-routing.json"
    routing = json.loads(routing_path.read_text(encoding="utf-8"))
    manifest_path = assessed_pr / "specialist-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    roles = ["challenger", "qa-specialist"]
    routing.update(risk_tier=tier, triggered_roles=roles, trigger_reasons={role: ["Broad review"] for role in roles})
    passes = []
    for role in roles:
        card = (PLUGIN_ROOT / "roles" / role / "ROLE.md").read_bytes()
        retained = assessed_pr / "role-cards" / role / "ROLE.md"
        retained.parent.mkdir(parents=True)
        retained.write_bytes(card)
        output = assessed_pr / f"{role}.md"
        output.write_text(
            f"role_id: {role}\n## Reviewer Assessment\n\nSource rating version: 1\nRating: 3\nVerification readiness: complete\n"
            "Rationale: Bounded inline review cannot establish independence.\n",
            encoding="utf-8",
        )
        passes.append(
            {
                "role": role,
                "axis": "tests",
                "mode": "substituted",
                "trigger": "Broad review",
                "confidence": 0.94,
                "blocking_findings": 0,
                "output_path": str(output),
                "role_card_sha256": hashlib.sha256(card).hexdigest(),
            }
        )
    manifest["passes"] = passes
    result["metadata"].update(risk_tier=tier, specialist_passes=passes, fanout_substituted=True)
    result["metadata"]["reviewer_assessments"].extend(
        {
            "role": "Challenger (parent substitute)" if role == "challenger" else "QA specialist (parent substitute)",
            "rating": 3,
            "evidence": f"{role}.md",
        }
        for role in roles
    )
    if independent_required is not None:
        requirement = "User explicitly required independent review." if independent_required else None
        routing.update(independent_review_required=independent_required, independence_requirement_evidence=requirement)
        plan = {
            "consumer_policy": {
                "consumer_id": "code-review",
                "capability": "instruction-bounded-review",
                "promotion_status": "promoted",
                "parent_mutations": "serial",
                "canonical_gates": "serial",
            },
            "review_operation": "inspection-only",
            "write_policy": {"parent_writes": "none", "approval_requirement": "not-required"},
            "source_sensitivity": "non-sensitive",
            **{key: manifest[key] for key in ("review_run_id", "parent_thread_id", "review_input_sha256")},
            "contexts": [],
            "independent_review_required": independent_required,
            "independence_requirement_evidence": requirement,
        }
        plan_bytes = (json.dumps(plan) + "\n").encode()
        (assessed_pr / "parent-inspection-plan.json").write_bytes(plan_bytes)
        manifest.update(
            schema_version=5,
            inspection_execution={
                "plan_path": "parent-inspection-plan.json",
                "plan_sha256": hashlib.sha256(plan_bytes).hexdigest(),
            },
        )
        result["metadata"].update(
            independence_required=independent_required,
            independence_requirement_evidence=requirement,
            independence_satisfied=False,
            execution_mode="serial-fallback",
            execution_evidence_level="instruction-bounded-review",
            execution_observed_controls={},
            write_parallel_eligible=False,
        )
    for path, payload in ((result_path, result), (routing_path, routing), (manifest_path, manifest)):
        path.write_text(json.dumps(payload), encoding="utf-8")


def _finalize_parent_fallback(run: Path, *, independent_required: bool = False) -> subprocess.CompletedProcess[str]:
    """Publish genuine parent axis coverage with a retained missing configured type checker."""
    _add_substituted_broad_passes(run, "HIGH_RISK", independent_required=independent_required)
    result_path = run / "result.json"
    result = json.loads(result_path.read_bytes())
    metadata = result["metadata"]
    gap = "Parent substitutes did not provide independent coverage."
    metadata["confidence_gaps"].append(gap)
    metadata["confidence_gap_closures"].append(
        {"gap": gap, "status": "unresolved", "rationale": "Every selected axis was inspected by the main agent."}
    )
    metadata["confidence_recovery"]["remaining_limits"].append(gap)
    metadata["review_validation"].update(source_verdict=None, verification_readiness="blocked")
    for assessment in metadata["reviewer_assessments"]:
        assessment["rating"] = None
        response_path = run / assessment["evidence"]
        content = response_path.read_text(encoding="utf-8")
        content = content.replace(
            "Rating: 1\nVerification readiness: complete", "Rating: null\nVerification readiness: blocked"
        )
        content = content.replace(
            "Rating: 3\nVerification readiness: complete", "Rating: null\nVerification readiness: blocked"
        )
        response_path.write_text(content, encoding="utf-8")
    metadata["review_decision"].update(
        recommendation=None,
        summary="Parent inspection completed; configured type checking remains unavailable.",
        rationale="The required checker could not execute, and parent coverage is not independent.",
    )
    blocker = {
        "id": "G-TYPES",
        "title": "Configured type checker is unavailable",
        "required_change": "Run the configured mypy check in the project environment.",
        "evidence": ["configured-check.command.txt", "configured-check.stderr.txt"],
        "authors": ["QA specialist (parent substitute)"],
    }
    metadata["operational_blockers"] = [blocker]
    gates_path = run / "gates.json"
    gates = json.loads(gates_path.read_bytes())
    check = next(check for check in gates["checks"] if check["id"] == "types")
    check.update(
        status="missing-command",
        exit_code=127,
        reason="mypy: command not found",
        command_path=blocker["evidence"][0],
        stderr=blocker["evidence"][1],
    )
    (run / check["command_path"]).write_text("mypy widget.py\n", encoding="utf-8", newline="\n")
    (run / check["stderr"]).write_text("mypy: command not found\n", encoding="utf-8", newline="\n")
    gates.update(status="fail", checks_failed=["types"], failed_count=1)
    gates_path.write_text(json.dumps(gates), encoding="utf-8", newline="\n")
    notes = run / "review-notes.md"
    notes.write_text(
        notes.read_text(encoding="utf-8") + "\n\n## Review Findings and Merge Blocks\n\n"
        "| Finding / area | Author | Required change | Evidence | Status |\n"
        "| --- | --- | --- | --- | --- |\n"
        f"| {blocker['id']} | {', '.join(blocker['authors'])} | {blocker['required_change']} | "
        f"{'; '.join(blocker['evidence'])} | Required |\n",
        encoding="utf-8",
        newline="\n",
    )
    handoff = json.loads((run / "final-handoff.json").read_bytes())
    handoff["outcome"]["summary"] = "Recommendation: None."
    snapshot = handoff["tables"][0]
    snapshot["verification_readiness"] = "blocked"
    snapshot.update(reviewers=metadata["reviewer_assessments"], summary=metadata["review_decision"]["summary"])
    snapshot["rows"][-1]["cells"][1] = "no source judgment"
    handoff["tables"].append(
        {
            "heading": "Review Findings and Merge Blocks",
            "layout": "concise",
            "overview_only": True,
            "columns": ["Finding / area", "Required change", "Evidence", "Status"],
            "rows": [
                {
                    "id": blocker["id"],
                    "title": blocker["title"],
                    "authors": blocker["authors"],
                    "cells": [blocker["id"], blocker["required_change"], "; ".join(blocker["evidence"]), "Required"],
                    "source_ids": ["gate:types"],
                }
            ],
        }
    )
    handoff["source_records"].append({"id": "gate:types", "evidence": "gates.json"})
    total = len(handoff["source_records"])
    handoff["source_coverage"].update(source_records_total=total, represented_source_records_total=total)
    handoff["remaining"] = [
        {
            "row_id": blocker["id"],
            "item": blocker["title"],
            "owner": "reviewer",
            "next_action": blocker["required_change"],
        }
    ]
    handoff["next_steps"] = [blocker["id"]]
    drafts = run / "fallback-drafts"
    drafts.mkdir()
    (drafts / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8", newline="\n")
    (drafts / "handoff.json").write_text(json.dumps(handoff), encoding="utf-8", newline="\n")
    result_path.unlink()
    return subprocess.run(
        [
            sys.executable,
            str(PLUGIN_ROOT / "shared/remediation_finalize.py"),
            "finalize",
            "--skill",
            "code-review",
            "--run",
            str(run),
            "--metadata",
            str(drafts / "metadata.json"),
            "--handoff",
            str(drafts / "handoff.json"),
            "--status",
            "fail",
            "--confidence",
            "0.95",
            "--artifact-path",
            str(result_path),
            "--parent-thread-id",
            "thread",
            "--promote",
        ],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def finder() -> ModuleType:
    """Fresh copy of the review-report finder module."""
    return _module(FINDER)


@pytest.fixture
def validator() -> ModuleType:
    """Fresh copy of the code-review artifact validator module."""
    return _module(VALIDATE_REVIEW)


@pytest.fixture
def shared_validator() -> ModuleType:
    """Fresh copy of the shared artifact validator module."""
    return _module(VALIDATE_SHARED)


def _load_json(path: Path) -> Any:
    """Parse one UTF-8 JSON file."""
    return json.loads(path.read_text(encoding="utf-8"))


def _save_json(path: Path, payload: object) -> None:
    """Write one JSON document with LF newlines."""
    path.write_text(json.dumps(payload), encoding="utf-8", newline="\n")


def _update_json(path: Path, **fields: object) -> dict[str, Any]:
    """Merge top-level fields into one JSON file and return the merged payload."""
    payload = _load_json(path)
    payload.update(fields)
    _save_json(path, payload)
    return payload


def _rerender(run: Path, handoff: dict[str, Any], result: dict[str, Any]) -> None:
    """Write the handoff, render its final text and bind the new digests into the in-memory result."""
    handoff_path = run / "final-handoff.json"
    _save_json(handoff_path, handoff)
    validation = _module(FINAL_HANDOFF).render_files(
        handoff_path, run / "final.md", run / "final-handoff.validation.json"
    )
    result["metadata"]["final_handoff"].update(
        handoff_sha256=validation["handoff_sha256"], rendered_sha256=validation["rendered_sha256"]
    )


def _complete_run(
    run: Path, *, parent_thread_id: str | None = "thread", text: bool = True
) -> subprocess.CompletedProcess[Any]:
    """Run the finder completion CLI for one review run in a real subprocess."""
    argv = [sys.executable, str(FINDER), "--complete-run", str(run)]
    if parent_thread_id:
        argv += ["--parent-thread-id", parent_thread_id]
    return subprocess.run(argv, capture_output=True, text=text, encoding="utf-8" if text else None, check=False)


def _find_target(reports_dir: Path) -> subprocess.CompletedProcess[str]:
    """Run the finder lookup CLI for pull request 123 below a reports directory."""
    argv = [sys.executable, str(FINDER), "--target", "#123", "--reports-dir", str(reports_dir)]
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def _validate_assessed(validator: ModuleType, run: Path, result_name: str = "result.json") -> None:
    """Run the review validator over an assessed run's named result file."""
    validator._validate_result(run, run / result_name, run, "thread", run)


def _complete_attempt(finder: ModuleType, run: Path, _result_path: Path) -> None:
    """Attempt promoted-result completion through the finder module."""
    finder.complete_review_run(run, parent_thread_id="thread")


def _validate_attempt(finder: ModuleType, _run: Path, result_path: Path) -> None:
    """Attempt candidate validation through the finder module."""
    finder.validate_review_result(result_path, parent_thread_id="thread")


def _promote_through_writer(
    run: Path, result_path: Path, *, status: str, checks_run: str, confidence: float, metadata: dict[str, Any]
) -> None:
    """Replace a result by a writer candidate that passes both artifact validators, then promote it."""
    result_path.unlink()
    candidate = run / "result.candidate.json"
    written = subprocess.run(
        [
            sys.executable,
            str(WRITE_RESULT),
            "--out",
            str(candidate),
            "--gates",
            str(run / "gates.json"),
            "--status",
            status,
            "--checks-run",
            checks_run,
            "--confidence",
            str(confidence),
            "--artifact-path",
            str(result_path),
            "--metadata",
            json.dumps(metadata),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert written.returncode == 0, written.stderr
    assert _load_json(candidate)["schema_version"] == 3
    review_check = [str(VALIDATE_REVIEW), "--out", str(run), "--result", str(candidate), "--parent-thread-id", "thread"]
    shared_check = [str(VALIDATE_SHARED), "--skill", "code-review", "--out", str(run), "--result", str(candidate)]
    for argv in (review_check, shared_check):
        checked = subprocess.run([sys.executable, *argv], capture_output=True, text=True, check=False)
        assert checked.returncode == 0, checked.stderr
    candidate.replace(result_path)


def _current_unavailable_terminal(root: Path) -> tuple[Path, dict[str, Any]]:
    """Build a current unavailable result whose confidence deductions match its rendered final handoff."""
    fixture = _module(Path(__file__).with_name("test_code_review_final_handoff_validation.py"))
    result_path = fixture._write_complete_unavailable_v2_artifact(
        root, {"status": "checkout-command-started", "local_state": "changed-or-unknown"}
    )
    result = _load_json(result_path)
    metadata = result["metadata"]
    metadata["confidence_gap_closures"][0]["rationale"] = (
        "(-0.10) " + metadata["confidence_gap_closures"][0]["rationale"]
    )
    previous_limit = metadata["confidence_recovery"]["remaining_limits"][0]
    metadata["confidence_recovery"]["remaining_limits"] = [
        f"(-0.00) {previous_limit} No additional deduction; "
        "the canonical source-verification gap accounts for this limitation."
    ]
    handoff = _load_json(root / "final-handoff.json")
    handoff["presentation_version"] = 8
    handoff["confidence"]["gaps"] = metadata["confidence_gap_closures"]
    handoff["confidence"]["limits"] = metadata["confidence_recovery"]["remaining_limits"]
    _rerender(root, handoff, result)
    return result_path, result


def _current_closed_terminal(root: Path) -> tuple[Path, dict[str, Any]]:
    """Build a current closed result carrying its writer-rendered final handoff."""
    fixture = _module(Path(__file__).with_name("test_code_review_close_contract.py"))
    result_path = fixture._write_closed_artifact(root)
    result = _load_json(result_path)
    result["metadata"]["final_handoff"] = fixture._write_closed_final_handoff(root, result_path, result["metadata"])
    _save_json(result_path, result)
    return result_path, result


def _historical_unavailable_terminal(root: Path) -> Path:
    """Write a historical unavailable result and return its path."""
    fixture = _module(Path(__file__).parents[1] / "pr" / "test_code_review_unavailable_artifact_contract.py")
    return fixture._write_unavailable_artifact(root)


def _historical_closed_terminal(root: Path) -> Path:
    """Write a historical closed result and return its path."""
    fixture = _module(Path(__file__).with_name("test_code_review_close_contract.py"))
    return fixture._write_closed_artifact(root)


def _add_newer_candidate(run: Path) -> None:
    """Add a newer same-PR run that holds only an unpromoted candidate."""
    newer = run.parent / "run-002"
    newer.mkdir()
    (newer / "pr.json").write_bytes((run / "pr.json").read_bytes())
    candidate = {"metadata": {"scope": "pr", "review_decision": {"recommendation": "needs-more-work"}}}
    _save_json(newer / "result.candidate.json", candidate)


def _add_newer_incomplete(run: Path) -> None:
    """Add a newer same-PR run that holds only review notes."""
    newer = run.parent / "run-002"
    newer.mkdir()
    (newer / "pr.json").write_bytes((run / "pr.json").read_bytes())
    (newer / "review-notes.md").write_text("Incomplete newer review.", encoding="utf-8")


def _write_checkout_receipts(
    run: Path, command: str, method: str, mode: str, checkout_overrides: dict[str, str] | None = None
) -> None:
    """Record the same checkout command, method and mode in routing and checkout receipts."""
    _update_json(run / "pr-routing.json", local_checkout_command=command, checkout_method=method, checkout_mode=mode)
    receipt = {"command": command, "checkout_method": method, "checkout_mode": mode, **(checkout_overrides or {})}
    _update_json(run / "local-checkout.json", **receipt)


def _strip_checkout_mode(run: Path) -> None:
    """Make both receipts claim a native checkout while omitting the explicit review mode."""
    command = "gh pr checkout https://github.com/acme/widgets/pull/123"
    for filename, command_field in (("pr-routing.json", "local_checkout_command"), ("local-checkout.json", "command")):
        payload = _load_json(run / filename)
        payload.update(checkout_method="gh-pr-checkout", **{command_field: command})
        payload.pop("checkout_mode", None)
        _save_json(run / filename, payload)


def _as_legacy_v1_result(run: Path, command: str) -> Path:
    """Strip the schema version from the result and set the routing checkout command."""
    result_path = run / "result.json"
    result = _load_json(result_path)
    result.pop("schema_version")
    _save_json(result_path, result)
    _update_json(run / "pr-routing.json", local_checkout_command=command)
    return result_path


def _mark_local_multi_role(run: Path) -> None:
    """Trigger multiple review axes under LOCAL risk by raising two routing signals."""
    routing = _load_json(run / "review-routing.json")
    routing["signals"].update(behavior_change=True, explicit_adversarial=True)
    _save_json(run / "review-routing.json", routing)


def _fail_tests_gate(run: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Mark the tests gate failed in gates, result and handoff; return the result and handoff for further edits."""
    result = _load_json(run / "result.json")
    result.update(status="fail", checks_failed=["tests"])
    gates_path = run / "gates.json"
    gates = _load_json(gates_path)
    gates.update(status="fail", checks_failed=["tests"], failed_count=1)
    next(check for check in gates["checks"] if check["id"] == "tests").update(status="fail", exit_code=1)
    _save_json(gates_path, gates)
    handoff = _load_json(run / "final-handoff.json")
    next(check for check in handoff["verification"] if check["check"] == "tests")["status"] = "fail"
    return result, handoff


def _add_test_failure_merge_block(run: Path, result: dict[str, Any], handoff: dict[str, Any]) -> None:
    """Turn the failed tests gate into a needs-more-work decision backed by a blocking finding."""
    recommendation = "needs-more-work"
    result["metadata"]["review_decision"]["recommendation"] = recommendation
    result["metadata"]["review_validation"]["source_verdict"] = recommendation
    result["metadata"]["operational_blockers"] = [{"id": "G-1", "authors": ["Main reviewer"]}]
    handoff["outcome"]["summary"] = "Recommendation: needs-more-work."
    next(row for row in handoff["tables"][0]["rows"] if row["cells"][0] == "Suggestion")["cells"][1] = "major revision"
    block = {
        "heading": "Review Findings and Merge Blocks",
        "layout": "concise",
        "overview_only": True,
        "columns": ["Finding / area", "Required change", "Evidence", "Status"],
        "rows": [
            {
                "id": "G-1",
                "title": "G-1",
                "authors": ["Main reviewer"],
                "cells": ["G-1", "Resolve test execution failure.", "gates.json", "Required"],
                "source_ids": ["gate:tests"],
            }
        ],
    }
    handoff["tables"].append(block)
    handoff["source_records"].append({"id": "gate:tests", "evidence": "gates.json"})
    total = len(handoff["source_records"])
    handoff["source_coverage"].update(source_records_total=total, represented_source_records_total=total)
    notes = run / "review-notes.md"
    table = (
        "\n\n## Review Findings and Merge Blocks\n\n"
        "| Finding / area | Author | Required change | Evidence | Status |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| G-1 | Main reviewer | Resolve test execution failure. | gates.json | Required |\n"
    )
    details = _module(FINAL_HANDOFF).render_review_report_details(handoff)
    notes.write_text(notes.read_text(encoding="utf-8") + table + "\n" + details, encoding="utf-8")


def _preflight_plan(**host: object) -> dict[str, Any]:
    """Return a promoted portable read-only plan, adding the given ``review_host`` entry when supplied."""
    return {
        "consumer_policy": {
            "consumer_id": "code-review",
            "capability": "portable-read-only",
            "promotion_status": "promoted",
            "parent_mutations": "serial",
            "canonical_gates": "serial",
        },
        "write_policy": {"parent_writes": "none", "approval_requirement": "not-required"},
        **host,
    }


def _run_preflight(tmp_path: Path, plan: dict[str, Any], *flags: str) -> subprocess.CompletedProcess[str]:
    """Run the parallel-execution preflight CLI for a plan file in a real subprocess."""
    path = tmp_path / "plan.json"
    _save_json(path, plan)
    argv = [sys.executable, str(PARALLEL), "preflight", "--consumer", "code-review", "--plan", str(path), *flags]
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def _independence_evidence(roles: set[str], modes: tuple[str, ...]) -> SimpleNamespace:
    """Build the minimal inspection evidence the independence rule reads."""
    return SimpleNamespace(
        manifest={"schema_version": 5},
        triggered_roles=roles,
        runtime_summary={"actual_mode": "serial-fallback"},
        passes=[{"mode": mode} for mode in modes],
    )


def _prepare_role_card_upgrade(
    run: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, validator: ModuleType
) -> tuple[Path, dict[str, Any], Any]:
    """Retain role cards in a LOCAL review, install changed cards, and return the result path, result and
    environment."""
    _add_substituted_broad_passes(run, "BROAD")
    installed = tmp_path / "changed-plugin" / "roles"
    for role in ("challenger", "qa-specialist"):
        destination = installed / role / "ROLE.md"
        destination.parent.mkdir(parents=True)
        destination.write_bytes((PLUGIN_ROOT / "roles" / role / "ROLE.md").read_bytes() + b"\nUpdated installation.\n")
    monkeypatch.setattr(validator, "PLUGIN_ROOT", installed.parent)
    _mark_local_multi_role(run)
    _update_json(run / "review-routing.json", risk_tier="LOCAL")
    result_path = run / "result.json"
    result = _load_json(result_path)
    result["metadata"].update(risk_tier="LOCAL", independence_required=True)
    _save_json(result_path, result)
    env = validator._ReviewEnvironment(codex_home=run, parent_thread_id="thread", project_root=run)
    return result_path, result, env


def _remove_card(card: Path) -> None:
    """Delete the retained role card."""
    card.unlink()


def _alter_card(card: Path) -> None:
    """Append bytes to the retained role card."""
    card.write_bytes(card.read_bytes() + b"\nAltered archive.\n")


class TestCompleteRun:
    """Completion CLI: emits the bound final text only for validated, promoted results."""

    def test_emits_bound_final_and_finder_locates_it(self, assessed_pr: Path) -> None:
        """Completion prints the bound final text and a separate lookup finds the promoted result.

        An assessed PR run is completed through the real CLI, then discovered by target in a second process, so no
        session memory can stand in for the retained artifacts.
        """
        completed = _complete_run(assessed_pr, text=False)
        lookup = _find_target(assessed_pr.parent.parent)

        assert completed.returncode == 0, completed.stderr
        assert completed.stdout == (assessed_pr / "final.md").read_bytes()
        assert lookup.returncode == 0, lookup.stderr
        assert Path(lookup.stdout.strip()) == assessed_pr / "result.json"

    def test_reuses_producer_thread_and_rejects_override(self, assessed_pr: Path) -> None:
        """Completion authenticates the retained producer thread and rejects a different explicit thread.

        A later session omitting the thread id must still complete; one naming another session must be refused so the
        current thread cannot be substituted for the producer.
        """
        reused = _complete_run(assessed_pr, parent_thread_id=None, text=False)
        wrong_producer = _complete_run(assessed_pr, parent_thread_id="other-session", text=False)

        assert reused.returncode == 0, reused.stderr
        assert reused.stdout == (assessed_pr / "final.md").read_bytes()
        assert wrong_producer.returncode != 0
        assert wrong_producer.stdout == b""
        assert b"manifest-parent-thread-mismatch" in wrong_producer.stderr

    @pytest.mark.parametrize(
        "terminal",
        [
            pytest.param(_historical_unavailable_terminal, id="unavailable"),
            pytest.param(_historical_closed_terminal, id="closed"),
        ],
    )
    @pytest.mark.parametrize(
        "final_handoff",
        [
            pytest.param(
                {"final_handoff": {"rendered_sha256": hashlib.sha256(_TERMINAL_FINAL).hexdigest()}},
                id="self-declared-digest",
            ),
            pytest.param({}, id="digest-missing"),
        ],
    )
    def test_historical_terminal_result_rejects_unbound_final(
        self, tmp_path: Path, finder: ModuleType, terminal: Callable[[Path], Path], final_handoff: dict[str, Any]
    ) -> None:
        """A historical terminal result cannot complete using a self-declared or missing final digest.

        The result stays readable by lookup, but completion needs the schema-three binding between result and rendered
        final text, so an arbitrary final.md must be refused with a diagnostic instead of a traceback.
        """
        result_path = terminal(tmp_path)
        (tmp_path / "final.md").write_bytes(_TERMINAL_FINAL)
        result = _load_json(result_path)
        result["metadata"].update(final_handoff)
        _save_json(result_path, result)
        finder.validate_review_result(result_path, parent_thread_id="thread")

        completed = _complete_run(tmp_path)

        assert completed.returncode == 1
        assert completed.stdout == ""
        assert "Review handoff blocked: review-completion-requires-bound-schema-v3" in completed.stderr
        assert "Traceback" not in completed.stderr

    @pytest.mark.parametrize(
        ("terminal", "fragments"),
        [
            pytest.param(
                _current_unavailable_terminal,
                (
                    b"0.90 (fair).",
                    b"(-0.10) A local checkout command may have changed state",
                    b"Limits: (-0.00) PR correctness was not assessed",
                ),
                id="unavailable",
            ),
            pytest.param(_current_closed_terminal, (), id="closed"),
        ],
    )
    def test_current_terminal_writer_promotes_and_completes(
        self, tmp_path: Path, terminal: Callable[[Path], tuple[Path, dict[str, Any]]], fragments: tuple[bytes, ...]
    ) -> None:
        """A current terminal result passes both validators through the writer and then completes.

        Covers the unavailable and closed dispositions: the writer-produced candidate must validate and promote, and the
        completion CLI must print the rendered final text, including any disclosed confidence deductions.
        """
        result_path, result = terminal(tmp_path)
        _promote_through_writer(
            tmp_path,
            result_path,
            status=result["status"],
            checks_run=",".join(result["checks_run"]),
            confidence=result["confidence"],
            metadata=result["metadata"],
        )

        completed = _complete_run(tmp_path, text=False)

        assert completed.returncode == 0, completed.stderr
        assert completed.stdout == (tmp_path / "final.md").read_bytes()
        assert all(fragment in completed.stdout for fragment in fragments)

    def test_writer_promotes_assessed_candidate(self, assessed_pr: Path) -> None:
        """An assessed result passes both validators through the writer and then completes.

        The writer must emit a schema-three candidate from the assessed metadata; promoting it must leave a run the
        completion CLI accepts and prints.
        """
        result = _load_json(assessed_pr / "result.json")
        _promote_through_writer(
            assessed_pr,
            assessed_pr / "result.json",
            status="pass",
            checks_run="lint,format,types,tests,review",
            confidence=0.95,
            metadata=result["metadata"],
        )

        completed = _complete_run(assessed_pr, text=False)

        assert completed.returncode == 0, completed.stderr
        assert completed.stdout == (assessed_pr / "final.md").read_bytes()

    def test_incomplete_review_withholds_prepared_final(self, tmp_path: Path) -> None:
        """A prepared verdict is not completion when no canonical result was promoted.

        Only final.md and review notes exist: completion must refuse, print nothing, name the retained evidence and
        leave the prepared text untouched.
        """
        (tmp_path / "final.md").write_text("Recommendation: accept-as-is.\n", encoding="utf-8")
        (tmp_path / "review-notes.md").write_text("Retained findings.\n", encoding="utf-8")

        completed = _complete_run(tmp_path, parent_thread_id=None)

        assert completed.returncode == 1
        assert completed.stdout == ""
        assert completed.stderr.startswith("Review handoff blocked: review-result-not-promoted:")
        assert "Retained evidence:" in completed.stderr
        assert (tmp_path / "final.md").read_text(encoding="utf-8") == "Recommendation: accept-as-is.\n"

    def test_forged_result_withholds_final(self, tmp_path: Path) -> None:
        """A canonical filename alone cannot bypass either artifact validator.

        A minimal result.json plus an unvalidated final.md must be rejected with a validation diagnostic and no output.
        """
        _save_json(tmp_path / "result.json", {"metadata": {"scope": "pr"}})
        (tmp_path / "final.md").write_text("Unvalidated verdict.\n", encoding="utf-8")

        completed = _complete_run(tmp_path)

        assert completed.returncode == 1
        assert completed.stdout == ""
        assert completed.stderr.startswith("Review handoff blocked: review-validation-failed:")
        assert "invalid-status" in completed.stderr

    def test_stale_rendered_digest_withholds_final(self, assessed_pr: Path) -> None:
        """Passing review-specific validation cannot hide a stale rendered digest.

        Tampering with final.md after rendering must make the shared validator fail completion without printing it.
        """
        (assessed_pr / "final.md").write_text("Tampered verdict.\n", encoding="utf-8")

        completed = _complete_run(assessed_pr)

        assert completed.returncode == 1
        assert completed.stdout == ""
        assert "review-validation-failed:" in completed.stderr

    def test_accepted_review_with_failed_gate_is_refused(self, assessed_pr: Path) -> None:
        """Approval cannot be published while a quality gate failed.

        The tests gate is marked failed but the decision remains accept-as-is, so completion must refuse.
        """
        result, handoff = _fail_tests_gate(assessed_pr)
        _rerender(assessed_pr, handoff, result)
        _save_json(assessed_pr / "result.json", result)

        completed = _complete_run(assessed_pr)

        assert completed.returncode == 1
        assert completed.stdout == ""
        assert "review-approval-with-failed-gates" in completed.stderr

    def test_failed_gate_publishes_with_nonapproval_handoff(self, assessed_pr: Path) -> None:
        """A failed quality gate completes only with a validated nonapproval handoff the finder can discover.

        The tests gate fails and the decision becomes needs-more-work with a blocking finding; completion must print the
        rendered text and the lookup must still find the result.
        """
        result, handoff = _fail_tests_gate(assessed_pr)
        _add_test_failure_merge_block(assessed_pr, result, handoff)
        _rerender(assessed_pr, handoff, result)
        _save_json(assessed_pr / "result.json", result)

        completed = _complete_run(assessed_pr)
        lookup = _find_target(assessed_pr.parent.parent)

        assert completed.returncode == 0, completed.stderr
        assert completed.stdout == (assessed_pr / "final.md").read_text(encoding="utf-8")
        assert lookup.returncode == 0, lookup.stderr
        assert Path(lookup.stdout.strip()) == assessed_pr / "result.json"

    @pytest.mark.parametrize(
        ("add_newer", "diagnostic"),
        [
            pytest.param(_add_newer_incomplete, "matching-review-incomplete:", id="incomplete"),
            pytest.param(_add_newer_candidate, "matching-review-candidate-unpromoted:", id="candidate"),
        ],
    )
    def test_delivers_named_run_while_newer_pending_blocks_intake(
        self, assessed_pr: Path, finder: ModuleType, add_newer: Callable[[Path], None], diagnostic: str
    ) -> None:
        """Completion delivers the named report while newer pending work stays out of remediation intake.

        A newer same-PR run that is incomplete or an unpromoted candidate must not block printing the named completed
        run, yet latest-report lookup and assessed-result intake must still reject it.
        """
        add_newer(assessed_pr)

        completed = _complete_run(assessed_pr, text=False)

        assert completed.returncode == 0, completed.stderr
        assert completed.stdout == (assessed_pr / "final.md").read_bytes()
        with pytest.raises(LookupError, match=diagnostic):
            finder.find_latest_review_report("123", [assessed_pr.parent.parent])
        with pytest.raises(LookupError, match=diagnostic):
            finder.require_assessed_review_result(assessed_pr / "result.json", parent_thread_id="thread")

    def test_explicit_result_rejects_newer_unpromoted_run(self, assessed_pr: Path, finder: ModuleType) -> None:
        """Explicit remediation intake honors a newer same-PR candidate.

        Naming the older promoted result must still fail when a newer run only has an unpromoted candidate.
        """
        _add_newer_candidate(assessed_pr)

        with pytest.raises(LookupError, match="matching-review-candidate-unpromoted"):
            finder.require_assessed_review_result(assessed_pr / "result.json", parent_thread_id="thread")


class TestHandoffContract:
    """Assessed results bind to current handoff presentation, branch and reviewer ratings."""

    def test_archived_schema_two_is_valid_but_cannot_complete(self, assessed_pr: Path, finder: ModuleType) -> None:
        """Old assessed reports stay readable without the new producer completion contract.

        A schema-two result with a presentation-two handoff validates for lookup, yet completion requires the bound
        schema-three binding and must raise.
        """
        result_path = assessed_pr / "result.json"
        result = _load_json(result_path)
        result["schema_version"] = 2
        handoff = _load_json(assessed_pr / "final-handoff.json")
        handoff["presentation_version"] = 2
        _rerender(assessed_pr, handoff, result)
        _save_json(result_path, result)

        finder.validate_review_result(result_path, parent_thread_id="thread")
        with pytest.raises(LookupError, match="review-completion-requires-bound-schema-v3"):
            finder.complete_review_run(assessed_pr, parent_thread_id="thread")

    @pytest.mark.parametrize(
        ("filename", "attempt", "error"),
        [
            pytest.param(
                "result.candidate.json",
                _validate_attempt,
                "review-current-handoff-presentation-v8-required",
                id="candidate-validation",
            ),
            pytest.param(
                "result.json", _complete_attempt, "review-current-handoff-presentation-invalid", id="completion"
            ),
        ],
    )
    def test_current_result_rejects_presentation_two(
        self, assessed_pr: Path, finder: ModuleType, filename: str, attempt: Callable[..., None], error: str
    ) -> None:
        """A current assessed result cannot use a row-ID-free version-two handoff.

        Both the candidate validator and promoted-result completion must refuse the older presentation.
        """
        handoff = _load_json(assessed_pr / "final-handoff.json")
        handoff["presentation_version"] = 2
        result = _load_json(assessed_pr / "result.json")
        _rerender(assessed_pr, handoff, result)
        result_path = assessed_pr / filename
        _save_json(result_path, result)

        with pytest.raises(LookupError, match=error):
            attempt(finder, assessed_pr, result_path)

    @pytest.mark.parametrize(
        ("filename", "attempt"),
        [
            pytest.param("result.candidate.json", _validate_attempt, id="candidate-validation"),
            pytest.param("result.json", _complete_attempt, id="completion"),
        ],
    )
    def test_current_result_rejects_caller_contract_branch(
        self, assessed_pr: Path, finder: ModuleType, filename: str, attempt: Callable[..., None]
    ) -> None:
        """A result with an assessed decision cannot choose the tableless caller-contract branch.

        The handoff is switched to the caller-contract branch with a consistent digest; both candidate validation and
        completion must still report a branch mismatch.
        """
        result = _apply_caller_contract_branch(assessed_pr)
        result_path = assessed_pr / filename
        _save_json(result_path, result)

        with pytest.raises(LookupError, match="code-review-final-handoff-branch-mismatch"):
            attempt(finder, assessed_pr, result_path)

    def test_archived_schema_two_accepts_caller_contract_branch(self, assessed_pr: Path, finder: ModuleType) -> None:
        """Archived schema-two results keep their caller-contract branch readable.

        The same caller-contract handoff that current results reject must still validate on a schema-two result.
        """
        result = _apply_caller_contract_branch(assessed_pr)
        result["schema_version"] = 2
        result_path = assessed_pr / "result.json"
        _save_json(result_path, result)

        finder.validate_review_result(result_path, parent_thread_id="thread")

    def test_promoted_schema_three_rejects_changed_reviewer_rating(self, assessed_pr: Path, finder: ModuleType) -> None:
        """Revalidation rejects a rating modified after candidate promotion.

        Changing the main reviewer rating in the promoted result must be reported as a rating mismatch.
        """
        result_path = assessed_pr / "result.json"
        result = _load_json(result_path)
        result["metadata"]["reviewer_assessments"][0]["rating"] = 2
        _save_json(result_path, result)

        with pytest.raises(LookupError, match="review-assessment-rating-mismatch:main reviewer"):
            finder.complete_review_run(assessed_pr, parent_thread_id="thread")


def _apply_caller_contract_branch(run: Path) -> dict[str, Any]:
    """Rewrite the handoff to the caller-contract branch and return the result bound to it."""
    handoff = _load_json(run / "final-handoff.json")
    handoff.update(
        presentation_version=3,
        branch="caller-contract",
        tables=[],
        source_records=[],
        source_coverage={
            "source_records_total": 0,
            "represented_source_records_total": 0,
            "omitted_source_records_total": 0,
        },
        caller_contract={
            "format": "application/json",
            "evidence": "Declared exact output",
            "output": '{"status":"ok"}\n',
        },
    )
    result = _load_json(run / "result.json")
    result["metadata"]["final_handoff"]["branch"] = "caller-contract"
    _rerender(run, handoff, result)
    return result


class TestSourceProvenance:
    """PR source receipts (OIDs, checkout command, specialist manifest) must agree with PR metadata."""

    def test_target_advanced_past_pr_metadata_is_accepted(
        self, assessed_pr: Path, validator: ModuleType, shared_validator: ModuleType
    ) -> None:
        """A verified immutable target fetch may advance beyond the PR's metadata base.

        The target receipt moves to a newer base with a documented relation; both validators must accept it.
        """
        advanced_target = "c" * 40
        target = _update_json(
            assessed_pr / "target-branch.json",
            remote_ref=advanced_target,
            local_head=advanced_target,
            base_matches_pr_metadata=False,
            base_relation="advanced",
        )

        _validate_assessed(validator, assessed_pr)
        shared_validator._validate_code_remediate_pr_source(
            assessed_pr,
            _load_json(assessed_pr / "pr-routing.json"),
            target,
            _load_json(assessed_pr / "local-checkout.json"),
        )

    def test_routing_oids_mismatching_pr_metadata_are_rejected(
        self, assessed_pr: Path, validator: ModuleType, shared_validator: ModuleType
    ) -> None:
        """A self-consistent fetched source whose OIDs differ from PR metadata is rejected by both validators.

        Every receipt is rewritten to forged base and head OIDs that agree with each other but not with pr.json.
        """
        base, head = "d" * 40, "e" * 40
        routing = _update_json(
            assessed_pr / "pr-routing.json",
            base_oid=base,
            head_oid=head,
            local_checkout_command=f"git checkout --detach {head}",
        )
        target = _update_json(
            assessed_pr / "target-branch.json", remote_ref=base, local_head=base, expected_base_oid=base
        )
        _update_json(assessed_pr / "pr-head-fetch.json", local_head=head, expected_head_oid=head)
        checkout = _update_json(
            assessed_pr / "local-checkout.json",
            command=f"git checkout --detach {head}",
            expected_head=head,
            local_head=head,
            diff_base_oid=base,
            diff_head_oid=head,
            diff_command=f"git diff --binary {base}...{head} --",
        )
        _update_json(assessed_pr / "worktree-preflight.json", current_head=head, expected_head=head)

        with pytest.raises(SystemExit, match="pr-source-oid-provenance-invalid"):
            validator._validate_verified_pr_source(assessed_pr, routing, target, checkout)
        with pytest.raises(SystemExit, match="code-remediate-pr-source-oid-provenance-invalid"):
            shared_validator._validate_code_remediate_pr_source(assessed_pr, routing, target, checkout)

    @pytest.mark.parametrize(
        ("method", "command"),
        [
            pytest.param(
                "gh-pr-checkout", "gh pr checkout https://github.com/acme/widgets/pull/123", id="native-checkout"
            ),
            pytest.param("already-at-head", "not-run: already at expected PR head", id="verified-existing-head"),
            pytest.param("git-detached-review-fallback", "git checkout --detach {head}", id="detached-fallback"),
        ],
    )
    def test_collector_checkout_receipts_are_accepted(
        self, assessed_pr: Path, validator: ModuleType, method: str, command: str
    ) -> None:
        """Supported collector checkout outcomes pass the complete review handoff validator.

        Native checkout, an already-correct head and the detached fallback each record a truthful command and method.
        """
        head = _load_json(assessed_pr / "pr-routing.json")["head_oid"]
        _write_checkout_receipts(assessed_pr, command.format(head=head), method, "review")

        _validate_assessed(validator, assessed_pr)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            pytest.param("command", "gh pr checkout https://github.com/acme/other/pull/123", id="wrong-repository"),
            pytest.param("checkout_method", "already-at-head", id="method-disagreement"),
            pytest.param("checkout_mode", "remediate", id="mode-disagreement"),
            pytest.param("command", "git checkout --force main", id="forced-command"),
        ],
    )
    def test_checkout_receipt_disagreement_is_rejected(
        self, assessed_pr: Path, validator: ModuleType, field: str, value: str
    ) -> None:
        """Contradictory checkout evidence is rejected even when the recorded commit IDs match.

        Routing records a native checkout while the checkout receipt differs in repository, method, mode or command.
        """
        command = f"gh pr checkout {_load_json(assessed_pr / 'pr-routing.json')['pr_url']}"
        _write_checkout_receipts(assessed_pr, command, "gh-pr-checkout", "review", {field: value})

        with pytest.raises(SystemExit, match=r"^pr-routing-checkout-command-invalid$"):
            _validate_assessed(validator, assessed_pr)

    def test_modern_receipt_requires_explicit_mode(self, assessed_pr: Path, validator: ModuleType) -> None:
        """Matching modern receipts that both omit the review mode are rejected.

        Both routing and checkout receipts claim a native checkout but carry no checkout_mode.
        """
        _strip_checkout_mode(assessed_pr)

        with pytest.raises(SystemExit, match=r"^pr-routing-checkout-command-invalid$"):
            _validate_assessed(validator, assessed_pr)

    def test_v1_accepts_legacy_checkout_command(self, assessed_pr: Path, validator: ModuleType) -> None:
        """A version-one result keeps accepting the historical checkout receipt.

        The legacy ``gh pr checkout 123`` form predates the structured receipt and must stay readable.
        """
        result_path = _as_legacy_v1_result(assessed_pr, "gh pr checkout 123")

        validator._validate_result(assessed_pr, result_path, assessed_pr, "thread", assessed_pr)

    def test_v1_rejects_arbitrary_checkout_command(self, assessed_pr: Path, validator: ModuleType) -> None:
        """A version-one result still rejects an arbitrary checkout command.

        Legacy tolerance covers only the historical receipt, not any command string.
        """
        result_path = _as_legacy_v1_result(assessed_pr, "git checkout arbitrary")

        with pytest.raises(SystemExit, match="pr-routing-checkout-command-invalid"):
            validator._validate_result(assessed_pr, result_path, assessed_pr, "thread", assessed_pr)

    def test_current_review_rejects_legacy_specialist_manifest(self, assessed_pr: Path, validator: ModuleType) -> None:
        """A schema-three assessed result cannot use a manifest without role-card binding.

        Downgrading the specialist manifest to schema two must be rejected for a current result.
        """
        _update_json(assessed_pr / "specialist-manifest.json", schema_version=2)

        with pytest.raises(SystemExit, match="current-review-specialist-manifest-schema"):
            _validate_assessed(validator, assessed_pr)


class TestPreflight:
    """Parallel-execution preflight gates child dispatch on a usable review host."""

    @pytest.mark.parametrize(
        ("mode", "host"),
        [
            pytest.param("serial", {}, id="serial-without-host"),
            pytest.param(
                "parallel-read",
                {
                    "review_host": {
                        "source": "runtime-tool-contract",
                        "sandbox_mode": "read-only",
                        "approval_policy": "never",
                    }
                },
                id="parallel-with-runtime-host",
            ),
        ],
    )
    def test_compatible_planning_is_admitted_without_claiming_runtime(
        self, tmp_path: Path, mode: str, host: dict[str, object]
    ) -> None:
        """A compatible launcher declaration admits planning but never certifies child execution.

        The output reports the effective mode and carries no runtime-promotion or evidence-level claim.
        """
        completed = _run_preflight(tmp_path, _preflight_plan(**host), "--execution", mode)

        assert completed.returncode == 0, completed.stderr
        output = json.loads(completed.stdout)
        assert output["effective_mode"] == mode
        assert "runtime_promotion_eligible" not in output
        assert "evidence_level" not in output

    @pytest.mark.parametrize(
        "host",
        [
            pytest.param(None, id="missing-contract"),
            pytest.param(
                {"source": "role-card", "sandbox_mode": "read-only", "approval_policy": "never"},
                id="requested-role-only",
            ),
            pytest.param(
                {"source": "runtime-tool-contract", "sandbox_mode": "workspace-write", "approval_policy": "on-request"},
                id="incompatible-host",
            ),
        ],
    )
    def test_unusable_host_is_rejected_before_dispatch(self, tmp_path: Path, host: object) -> None:
        """Promotion policy cannot imply that the current launcher supports required child controls.

        A missing, role-card-only or write-capable host must be refused with exit code 2 before any dispatch.
        """
        completed = _run_preflight(tmp_path, _preflight_plan(review_host=host), "--execution=parallel-read")

        assert completed.returncode == 2
        assert completed.stdout == ""
        assert "review-host-controls-unavailable-before-dispatch" in completed.stderr


class TestIndependence:
    """Parent-substitute review cannot satisfy independence or parallel-review requirements."""

    @pytest.mark.integration
    def test_required_independence_blocks_failed_parent_fallback(self, assessed_pr: Path) -> None:
        """A failed nonapproval result cannot complete an expressly required independent review.

        The parent covers every axis itself while the user required independence; finalization or completion must report
        the independence requirement.
        """
        finalized = _finalize_parent_fallback(assessed_pr, independent_required=True)

        completed = _complete_run(assessed_pr)

        assert completed.returncode == 1, finalized.stdout + completed.stdout
        assert completed.stdout == ""
        assert "independent-review-required" in finalized.stdout + completed.stderr

    @pytest.mark.integration
    def test_failed_parent_fallback_requires_every_triggered_axis(self, assessed_pr: Path) -> None:
        """A failed verdict still requires genuine coverage of every triggered review axis.

        After a valid parent-fallback finalization one specialist pass is dropped from the manifest; completion must
        report the triggered-role mismatch.
        """
        finalized = _finalize_parent_fallback(assessed_pr)
        assert finalized.returncode == 0, finalized.stdout + finalized.stderr
        manifest_path = assessed_pr / "specialist-manifest.json"
        manifest = _load_json(manifest_path)
        manifest["passes"].pop()
        _save_json(manifest_path, manifest)

        completed = _complete_run(assessed_pr)

        assert completed.returncode == 1
        assert completed.stdout == ""
        assert "manifest-triggered-role-set-mismatch" in completed.stderr

    @pytest.mark.parametrize("tier", ["BROAD", "HIGH_RISK"])
    def test_serial_substitutes_cannot_pass_broad_review(
        self, assessed_pr: Path, validator: ModuleType, tier: str
    ) -> None:
        """A passing broad or high-risk verdict is rejected even when both inline role outputs are complete.

        Serial parent substitutes cannot establish the independence these tiers require.
        """
        _add_substituted_broad_passes(assessed_pr, tier)

        with pytest.raises(SystemExit, match="independent-review-required-for-pass:challenger,qa-specialist"):
            _validate_assessed(validator, assessed_pr)

    def test_local_multi_role_review_requires_parallel_specialists(
        self, assessed_pr: Path, validator: ModuleType
    ) -> None:
        """Observed parallel specialists are required even when LOCAL risk permits substitutes.

        Two routing signals trigger several axes and independence is required, so parent substitutes cannot pass.
        """
        _add_substituted_broad_passes(assessed_pr, "LOCAL")
        _mark_local_multi_role(assessed_pr)
        result_path = assessed_pr / "result.json"
        result = _load_json(result_path)
        result["metadata"]["independence_required"] = True
        _save_json(result_path, result)

        with pytest.raises(SystemExit, match="parallel-review-required-for-pass"):
            _validate_assessed(validator, assessed_pr)

    @pytest.mark.integration
    def test_local_parent_review_completes_without_claiming_independence(self, assessed_pr: Path) -> None:
        """A low-risk parent-only result reaches the real completion gate with its coverage disclosed.

        Independence is not requested, so completion prints a summary stating that no independent specialists took part.
        """
        _add_substituted_broad_passes(assessed_pr, "LOCAL", independent_required=False)
        _mark_local_multi_role(assessed_pr)
        result = _load_json(assessed_pr / "result.json")
        summary = "The parent inspected every selected axis; no independent specialists participated."
        result["metadata"]["review_decision"]["summary"] = summary
        handoff = _load_json(assessed_pr / "final-handoff.json")
        handoff["tables"][0]["reviewers"] = result["metadata"]["reviewer_assessments"]
        handoff["tables"][0]["summary"] = summary
        _rerender(assessed_pr, handoff, result)
        _save_json(assessed_pr / "result.json", result)

        completed = _complete_run(assessed_pr)

        assert completed.returncode == 0, completed.stderr
        assert "no independent specialists participated" in completed.stdout
        assert result["metadata"]["independence_satisfied"] is False

    def test_local_parent_review_passes_without_requested_independence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, validator: ModuleType
    ) -> None:
        """Fully covered low-risk parent review passes without inventing independent specialists.

        Three substituted passes under LOCAL risk with independence not requested must not raise.
        """
        evidence = _independence_evidence({"sw-engineer", "qa-specialist", "doc-scribe"}, ("substituted",) * 3)
        monkeypatch.setattr(validator, "_validate_inspection_independence", lambda *args: (False, False))
        metadata = {"fanout_substituted": True, "independence_required": False, "independence_satisfied": False}

        validator._validate_independence_requirement(tmp_path, evidence, metadata, "pass", "LOCAL", None)

    @pytest.mark.parametrize(
        ("tier", "modes"),
        [
            pytest.param("BROAD", ("substituted", "substituted"), id="broad-parent-review"),
            pytest.param("LOCAL", ("substituted", "inspection"), id="mixed-parent-and-child"),
        ],
    )
    def test_parent_review_exception_excludes_other_routes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, validator: ModuleType, tier: str, modes: tuple[str, ...]
    ) -> None:
        """Higher-risk and mixed multi-role routes stay subject to the parallel-review evidence gate.

        The low-risk parent exception must not extend to BROAD parent-only review or LOCAL mixed parent-and-child
        passes.
        """
        evidence = _independence_evidence({"sw-engineer", "qa-specialist"}, modes)
        monkeypatch.setattr(validator, "_validate_inspection_independence", lambda *args: (False, False))

        with pytest.raises(SystemExit, match="parallel-review-required-for-pass"):
            validator._validate_independence_requirement(tmp_path, evidence, {}, "pass", tier, None)


class TestRetainedRoleCards:
    """Promoted reviews bind to the role card retained at review time, not the currently installed one."""

    def test_retained_card_survives_installed_change_until_candidate_check(
        self, assessed_pr: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, validator: ModuleType
    ) -> None:
        """The original card keeps the promoted manifest valid after an installation upgrade.

        Manifest validation passes against the retained card, while validating a fresh candidate against the changed
        installed card must report a hash mismatch.
        """
        result_path, result, env = _prepare_role_card_upgrade(assessed_pr, tmp_path, monkeypatch, validator)

        validator._validate_specialist_manifest(assessed_pr, result, result_path, result["metadata"], "LOCAL", env)
        candidate_path = assessed_pr / "result.candidate.json"
        candidate_path.write_bytes(result_path.read_bytes())
        with pytest.raises(SystemExit, match="manifest-role-card-hash-mismatch:challenger"):
            validator._validate_result(assessed_pr, candidate_path, assessed_pr, "thread", assessed_pr)

    @pytest.mark.parametrize(
        ("damage", "expected"),
        [
            pytest.param(_remove_card, "role-card-missing:challenger", id="missing"),
            pytest.param(_alter_card, "manifest-role-card-hash-mismatch:challenger", id="altered"),
        ],
    )
    def test_missing_or_altered_retained_card_is_rejected(
        self,
        assessed_pr: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        validator: ModuleType,
        damage: Callable[[Path], None],
        expected: str,
    ) -> None:
        """Manifest validation rejects absent or altered retained role-card proof.

        After an installation upgrade the retained card is the only proof; removing or changing it must fail.
        """
        result_path, result, env = _prepare_role_card_upgrade(assessed_pr, tmp_path, monkeypatch, validator)
        damage(assessed_pr / "role-cards" / "challenger" / "ROLE.md")

        with pytest.raises(SystemExit, match=expected):
            validator._validate_specialist_manifest(assessed_pr, result, result_path, result["metadata"], "LOCAL", env)

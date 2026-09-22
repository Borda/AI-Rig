"""Exercise the executable boundary between review evidence and completed final text."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
FINDER = PLUGIN_ROOT / "shared" / "find-review-report.py"
PARALLEL = PLUGIN_ROOT / "shared" / "parallel_execution.py"


def _module(path: Path):
    """Load existing fixture builders and installed helpers without import-path changes."""
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(name="assessed_pr")
def _assessed_pr(tmp_path: Path) -> Path:
    """Create a complete trivial PR review through real render and validation boundaries."""
    run = tmp_path / "reports" / "pr-123" / "run-001"
    run.mkdir(parents=True)
    # Reuse collector/gate construction; keep assessed behavior visible here.
    fixture = _module(Path(__file__).with_name("test_code_review_close_contract.py"))
    result_path = fixture._write_closed_artifact(run)
    result = json.loads(result_path.read_text(encoding="utf-8"))
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
            "pr_paths": ["widget.py"],
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
    metadata = result["metadata"]
    del metadata["review_status"], metadata["close_decision"]
    metadata.update(
        {
            "review_decision": {
                "recommendation": "accept-as-is",
                "summary": "Spelling corrected.",
                "rationale": "No behavior change.",
            },
            "finding_records_version": 1,
            "review_findings": [],
            "operational_blockers": [],
            "specialist_manifest": str(run / "specialist-manifest.json"),
            "specialist_passes": [],
            "review_run_id": "test-review",
            "review_input_sha256": input_hash,
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
        ("PR", "[#123](https://github.com/acme/widgets/pull/123)"),
        ("Author", "@contributor"),
        ("CI", "passing"),
        ("Type", "docs"),
        ("Suggestion", "approve"),
    ]
    handoff = {
        "schema_version": 1,
        "skill": "code-review",
        "branch": "assessed",
        "outcome": {"title": "Review Decision", "summary": "Recommendation: accept-as-is."},
        "tables": [
            {
                "heading": "PR Snapshot",
                "columns": ["Field", "Value"],
                "rows": [
                    {"id": f"S{index}", "cells": list(row), "source_ids": [f"pr:{row[0]}"]}
                    for index, row in enumerate(snapshot)
                ],
            }
        ],
        "source_records": [{"id": f"pr:{field}", "evidence": f"pr.json#{field}"} for field, _ in snapshot],
        "source_coverage": {
            "source_records_total": 5,
            "represented_source_records_total": 5,
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
    result["schema_version"] = 2
    result_path.write_text(json.dumps(result), encoding="utf-8")
    return run


def test_completed_pr_emits_bound_final_then_separate_finder_finds_it(assessed_pr: Path) -> None:
    """Exercise both real validators plus discovery without a session-memory shortcut."""
    completed = subprocess.run(
        [sys.executable, str(FINDER), "--complete-run", str(assessed_pr), "--parent-thread-id", "thread"],
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr.decode()
    assert completed.stdout == (assessed_pr / "final.md").read_bytes()
    lookup = subprocess.run(
        [sys.executable, str(FINDER), "--target", "#123", "--reports-dir", str(assessed_pr.parent.parent)],
        capture_output=True,
        text=True,
    )
    assert lookup.returncode == 0, lookup.stderr
    assert Path(lookup.stdout.strip()) == assessed_pr / "result.json"


def test_v2_source_validation_accepts_a_target_advanced_past_pr_metadata(assessed_pr: Path) -> None:
    """Allow a verified immutable target fetch to advance beyond the PR's metadata base."""
    advanced_target = "c" * 40
    target_path = assessed_pr / "target-branch.json"
    target = json.loads(target_path.read_text(encoding="utf-8"))
    target.update(
        remote_ref=advanced_target,
        local_head=advanced_target,
        base_matches_pr_metadata=False,
        base_relation="advanced",
    )
    target_path.write_text(json.dumps(target), encoding="utf-8")

    validator = _module(PLUGIN_ROOT / "skills/code-review/validate_artifacts.py")
    validator._validate_result(assessed_pr, assessed_pr / "result.json", assessed_pr, "thread", assessed_pr)
    shared_validator = _module(PLUGIN_ROOT / "shared/validate-artifacts.py")
    shared_validator._validate_code_remediate_pr_source(
        assessed_pr,
        json.loads((assessed_pr / "pr-routing.json").read_text(encoding="utf-8")),
        target,
        json.loads((assessed_pr / "local-checkout.json").read_text(encoding="utf-8")),
    )


def test_v2_source_validation_rejects_routing_oids_mismatching_pr_metadata(assessed_pr: Path) -> None:
    """Reject a self-consistent fetched source whose OIDs differ from PR metadata."""
    forged_base, forged_head = "d" * 40, "e" * 40
    routing_path = assessed_pr / "pr-routing.json"
    routing = json.loads(routing_path.read_text(encoding="utf-8"))
    routing.update(
        base_oid=forged_base,
        head_oid=forged_head,
        local_checkout_command=f"git checkout --detach {forged_head}",
    )
    routing_path.write_text(json.dumps(routing), encoding="utf-8")

    target_path = assessed_pr / "target-branch.json"
    target = json.loads(target_path.read_text(encoding="utf-8"))
    target.update(remote_ref=forged_base, local_head=forged_base, expected_base_oid=forged_base)
    target_path.write_text(json.dumps(target), encoding="utf-8")

    head_fetch_path = assessed_pr / "pr-head-fetch.json"
    head_fetch = json.loads(head_fetch_path.read_text(encoding="utf-8"))
    head_fetch.update(local_head=forged_head, expected_head_oid=forged_head)
    head_fetch_path.write_text(json.dumps(head_fetch), encoding="utf-8")

    checkout_path = assessed_pr / "local-checkout.json"
    checkout = json.loads(checkout_path.read_text(encoding="utf-8"))
    checkout.update(
        command=f"git checkout --detach {forged_head}",
        expected_head=forged_head,
        local_head=forged_head,
        diff_base_oid=forged_base,
        diff_head_oid=forged_head,
        diff_command=f"git diff --binary {forged_base}...{forged_head} --",
    )
    checkout_path.write_text(json.dumps(checkout), encoding="utf-8")

    preflight_path = assessed_pr / "worktree-preflight.json"
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    preflight.update(current_head=forged_head, expected_head=forged_head)
    preflight_path.write_text(json.dumps(preflight), encoding="utf-8")

    validator = _module(PLUGIN_ROOT / "skills/code-review/validate_artifacts.py")
    with pytest.raises(SystemExit, match="pr-source-oid-provenance-invalid"):
        validator._validate_verified_pr_source(assessed_pr, routing, target, checkout)
    shared_validator = _module(PLUGIN_ROOT / "shared/validate-artifacts.py")
    with pytest.raises(SystemExit, match="code-remediate-pr-source-oid-provenance-invalid"):
        shared_validator._validate_code_remediate_pr_source(assessed_pr, routing, target, checkout)


@pytest.mark.parametrize(
    "method, command",
    [
        pytest.param("gh-pr-checkout", "gh pr checkout https://github.com/acme/widgets/pull/123", id="native-checkout"),
        pytest.param("already-at-head", "not-run: already at expected PR head", id="verified-existing-head"),
        pytest.param("git-detached-review-fallback", "git checkout --detach {head}", id="detached-fallback"),
    ],
)
def test_v2_review_accepts_truthful_collector_checkout_receipts(assessed_pr: Path, method: str, command: str) -> None:
    """Allow supported collector outcomes through the complete review handoff validator."""
    routing_path = assessed_pr / "pr-routing.json"
    routing = json.loads(routing_path.read_text(encoding="utf-8"))
    command = command.format(head=routing["head_oid"])
    routing.update(local_checkout_command=command, checkout_method=method, checkout_mode="review")
    routing_path.write_text(json.dumps(routing), encoding="utf-8")
    checkout_path = assessed_pr / "local-checkout.json"
    checkout = json.loads(checkout_path.read_text(encoding="utf-8"))
    checkout.update(command=command, checkout_method=method, checkout_mode="review")
    checkout_path.write_text(json.dumps(checkout), encoding="utf-8")
    validator = _module(PLUGIN_ROOT / "skills/code-review/validate_artifacts.py")
    validator._validate_result(assessed_pr, assessed_pr / "result.json", assessed_pr, "thread", assessed_pr)


@pytest.mark.parametrize(
    "field, value",
    [
        pytest.param("command", "gh pr checkout https://github.com/acme/other/pull/123", id="wrong-repository"),
        pytest.param("checkout_method", "already-at-head", id="method-disagreement"),
        pytest.param("checkout_mode", "remediate", id="mode-disagreement"),
        pytest.param("command", "git checkout --force main", id="forced-command"),
    ],
)
def test_v2_review_rejects_checkout_receipt_disagreement(assessed_pr: Path, field: str, value: str) -> None:
    """Reject contradictory checkout evidence even when the recorded commit IDs match."""
    routing_path = assessed_pr / "pr-routing.json"
    routing = json.loads(routing_path.read_text(encoding="utf-8"))
    command = f"gh pr checkout {routing['pr_url']}"
    routing.update(local_checkout_command=command, checkout_method="gh-pr-checkout", checkout_mode="review")
    routing_path.write_text(json.dumps(routing), encoding="utf-8")
    checkout_path = assessed_pr / "local-checkout.json"
    checkout = json.loads(checkout_path.read_text(encoding="utf-8"))
    checkout.update(command=command, checkout_method="gh-pr-checkout", checkout_mode="review")
    checkout[field] = value
    checkout_path.write_text(json.dumps(checkout), encoding="utf-8")
    validator = _module(PLUGIN_ROOT / "skills/code-review/validate_artifacts.py")
    with pytest.raises(SystemExit, match="^pr-routing-checkout-command-invalid$"):
        validator._validate_result(assessed_pr, assessed_pr / "result.json", assessed_pr, "thread", assessed_pr)


def test_modern_review_receipt_requires_explicit_mode(assessed_pr: Path) -> None:
    """Reject matching modern receipts that both omit the required review mode."""
    for filename, command_field in (("pr-routing.json", "local_checkout_command"), ("local-checkout.json", "command")):
        path = assessed_pr / filename
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.update(checkout_method="gh-pr-checkout")
        payload[command_field] = "gh pr checkout https://github.com/acme/widgets/pull/123"
        payload.pop("checkout_mode", None)
        path.write_text(json.dumps(payload), encoding="utf-8")
    validator = _module(PLUGIN_ROOT / "skills/code-review/validate_artifacts.py")
    with pytest.raises(SystemExit, match="^pr-routing-checkout-command-invalid$"):
        validator._validate_result(assessed_pr, assessed_pr / "result.json", assessed_pr, "thread", assessed_pr)


def test_v1_review_validation_preserves_legacy_checkout_command_contract(assessed_pr: Path) -> None:
    """Accept the historical checkout receipt but reject an arbitrary v1 command."""
    result_path = assessed_pr / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result.pop("schema_version")
    result_path.write_text(json.dumps(result), encoding="utf-8")
    routing_path = assessed_pr / "pr-routing.json"
    routing = json.loads(routing_path.read_text(encoding="utf-8"))
    routing["local_checkout_command"] = "gh pr checkout 123"
    routing_path.write_text(json.dumps(routing), encoding="utf-8")

    validator = _module(PLUGIN_ROOT / "skills/code-review/validate_artifacts.py")
    validator._validate_result(assessed_pr, result_path, assessed_pr, "thread", assessed_pr)

    routing["local_checkout_command"] = "git checkout arbitrary"
    routing_path.write_text(json.dumps(routing), encoding="utf-8")
    with pytest.raises(SystemExit, match="pr-routing-checkout-command-invalid"):
        validator._validate_result(assessed_pr, result_path, assessed_pr, "thread", assessed_pr)


def test_completion_rejects_a_valid_but_superseded_result(assessed_pr: Path) -> None:
    """Validation success is insufficient when the consumer would use a newer run."""
    newer = assessed_pr.parent / "run-002"
    newer.mkdir()
    (newer / "pr.json").write_bytes((assessed_pr / "pr.json").read_bytes())
    (newer / "review-notes.md").write_text("Incomplete newer review.", encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(FINDER), "--complete-run", str(assessed_pr), "--parent-thread-id", "thread"],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 1
    assert completed.stdout == ""
    assert "matching-review-incomplete:" in completed.stderr


def test_incomplete_review_never_emits_prepared_assessed_final(tmp_path: Path) -> None:
    """A prepared verdict is not completion when no canonical result was promoted."""
    (tmp_path / "final.md").write_text("Recommendation: accept-as-is.\n", encoding="utf-8")
    (tmp_path / "review-notes.md").write_text("Retained findings.\n", encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, str(FINDER), "--complete-run", str(tmp_path)], capture_output=True, text=True
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr.startswith("Review handoff blocked: review-result-not-promoted:")
    assert "Retained evidence:" in completed.stderr
    assert (tmp_path / "final.md").read_text(encoding="utf-8") == "Recommendation: accept-as-is.\n"


def test_forged_promoted_review_cannot_emit_final_text(tmp_path: Path) -> None:
    """A canonical filename alone cannot bypass either artifact validator."""
    (tmp_path / "result.json").write_text(json.dumps({"metadata": {"scope": "pr"}}), encoding="utf-8")
    (tmp_path / "final.md").write_text("Unvalidated verdict.\n", encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, str(FINDER), "--complete-run", str(tmp_path), "--parent-thread-id", "thread"],
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr.startswith("Review handoff blocked: review-validation-failed:")
    assert "invalid-status" in completed.stderr


@pytest.mark.parametrize("mode", ["serial", "parallel-read"])
def test_review_preflight_accepts_compatible_planning_without_claiming_runtime(tmp_path: Path, mode: str) -> None:
    """A compatible launcher declaration admits planning, never certifies child execution."""
    plan = {
        "consumer_policy": {
            "consumer_id": "code-review",
            "capability": "portable-read-only",
            "promotion_status": "promoted",
            "parent_mutations": "serial",
            "canonical_gates": "serial",
        },
        "write_policy": {"parent_writes": "none", "approval_requirement": "not-required"},
        "review_host": {"source": "runtime-tool-contract", "sandbox_mode": "read-only", "approval_policy": "never"},
    }
    if mode == "serial":
        del plan["review_host"]
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            str(PARALLEL),
            "preflight",
            "--consumer",
            "code-review",
            "--plan",
            str(path),
            "--execution",
            mode,
        ],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    output = json.loads(completed.stdout)
    assert output["effective_mode"] == mode
    assert "runtime_promotion_eligible" not in output
    assert "evidence_level" not in output


def test_shared_validator_failure_suppresses_final_text(assessed_pr: Path) -> None:
    """Passing review-specific validation cannot hide a stale rendered digest."""
    (assessed_pr / "final.md").write_text("Tampered verdict.\n", encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(FINDER), "--complete-run", str(assessed_pr), "--parent-thread-id", "thread"],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 1
    assert completed.stdout == ""
    assert "review-validation-failed:" in completed.stderr


@pytest.mark.parametrize("recommendation", ["accept-as-is", "needs-more-work"])
def test_failed_quality_gate_requires_nonapproval_handoff(assessed_pr: Path, recommendation: str) -> None:
    """Publish failed checks only with a validated, discoverable nonapproval handoff."""
    result_path = assessed_pr / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result.update(status="fail", checks_failed=["tests"])
    gates_path = assessed_pr / "gates.json"
    gates = json.loads(gates_path.read_text(encoding="utf-8"))
    gates.update(status="fail", checks_failed=["tests"], failed_count=1)
    next(check for check in gates["checks"] if check["id"] == "tests").update(status="fail", exit_code=1)
    gates_path.write_text(json.dumps(gates), encoding="utf-8")
    handoff_path = assessed_pr / "final-handoff.json"
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    next(check for check in handoff["verification"] if check["check"] == "tests")["status"] = "fail"
    if recommendation == "needs-more-work":
        result["metadata"]["review_decision"]["recommendation"] = recommendation
        result["metadata"]["operational_blockers"] = [{"id": "G-1"}]
        handoff["outcome"]["summary"] = "Recommendation: needs-more-work."
        next(row for row in handoff["tables"][0]["rows"] if row["cells"][0] == "Suggestion")["cells"][1] = "needs work"
        handoff["tables"].append(
            {
                "heading": "Review Findings and Merge Blocks",
                "layout": "grouped",
                "columns": ["Finding / area", "Required change", "Evidence", "Status"],
                "rows": [
                    {
                        "id": "G-1",
                        "title": "G-1",
                        "cells": ["G-1", "Resolve test execution failure.", "gates.json", "Required"],
                        "source_ids": ["gate:tests"],
                    }
                ],
            }
        )
        handoff["source_records"].append({"id": "gate:tests", "evidence": "gates.json"})
        handoff["source_coverage"].update(source_records_total=6, represented_source_records_total=6)
        notes = assessed_pr / "review-notes.md"
        notes.write_text(
            notes.read_text(encoding="utf-8") + "\n\n## Review Findings and Merge Blocks\n\n"
            "| Finding / area | Required change | Evidence | Status |\n"
            "| --- | --- | --- | --- |\n"
            "| G-1 | Resolve test execution failure. | gates.json | Required |\n",
            encoding="utf-8",
        )
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
    validation = _module(PLUGIN_ROOT / "shared/final_handoff.py").render_files(
        handoff_path, assessed_pr / "final.md", assessed_pr / "final-handoff.validation.json"
    )
    result["metadata"]["final_handoff"].update(
        handoff_sha256=validation["handoff_sha256"], rendered_sha256=validation["rendered_sha256"]
    )
    result_path.write_text(json.dumps(result), encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, str(FINDER), "--complete-run", str(assessed_pr), "--parent-thread-id", "thread"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    if recommendation == "accept-as-is":
        assert completed.returncode == 1
        assert completed.stdout == ""
        assert "review-approval-with-failed-gates" in completed.stderr
    else:
        assert completed.returncode == 0, completed.stderr
        assert completed.stdout == (assessed_pr / "final.md").read_text(encoding="utf-8")
        lookup = subprocess.run(
            [sys.executable, str(FINDER), "--target", "#123", "--reports-dir", str(assessed_pr.parent.parent)],
            capture_output=True,
            text=True,
        )
        assert lookup.returncode == 0, lookup.stderr
        assert Path(lookup.stdout.strip()) == result_path


@pytest.mark.parametrize(
    "host",
    [
        pytest.param(None, id="missing-contract"),
        pytest.param(
            {"source": "role-card", "sandbox_mode": "read-only", "approval_policy": "never"}, id="requested-role-only"
        ),
        pytest.param(
            {"source": "runtime-tool-contract", "sandbox_mode": "workspace-write", "approval_policy": "on-request"},
            id="incompatible-host",
        ),
    ],
)
def test_review_preflight_rejects_unusable_host_before_dispatch(tmp_path: Path, host: object) -> None:
    """Promotion policy cannot imply that the current launcher supports required child controls."""
    plan = {
        "consumer_policy": {
            "consumer_id": "code-review",
            "capability": "portable-read-only",
            "promotion_status": "promoted",
            "parent_mutations": "serial",
            "canonical_gates": "serial",
        },
        "write_policy": {"parent_writes": "none", "approval_requirement": "not-required"},
        "review_host": host,
    }
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            str(PARALLEL),
            "preflight",
            "--consumer",
            "code-review",
            "--plan",
            str(path),
            "--execution=parallel-read",
        ],
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "review-host-controls-unavailable-before-dispatch" in completed.stderr


@pytest.mark.parametrize("tier", ["BROAD", "HIGH_RISK"])
def test_serial_substitutes_cannot_complete_independent_review(assessed_pr: Path, tier: str) -> None:
    """Reject a passing high-risk verdict even when both inline role outputs are complete."""
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
        output = assessed_pr / f"{role}.md"
        output.write_text(
            f"role_id: {role}\nBounded inline review found no additional issue; independence remains unavailable.\n",
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
                "role_card_sha256": hashlib.sha256((PLUGIN_ROOT / "roles" / role / "ROLE.md").read_bytes()).hexdigest(),
            }
        )
    manifest["passes"] = passes
    result["metadata"].update(risk_tier=tier, specialist_passes=passes, fanout_substituted=True)
    for path, payload in ((result_path, result), (routing_path, routing), (manifest_path, manifest)):
        path.write_text(json.dumps(payload), encoding="utf-8")
    validator = _module(PLUGIN_ROOT / "skills/code-review/validate_artifacts.py")
    with pytest.raises(SystemExit, match="independent-review-required-for-pass:challenger,qa-specialist"):
        validator._validate_result(assessed_pr, result_path, assessed_pr, "thread", assessed_pr)

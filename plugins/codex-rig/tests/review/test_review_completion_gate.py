"""Exercise the executable boundary between review evidence and completed final text."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[2]
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
        notes.write("\n\n## Main Reviewer Assessment\n\nRating: 1\nRationale: The spelling change is safe.\n")
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
            "reviewer_assessments": [{"role": "Main reviewer", "rating": 1, "evidence": "review-notes.md"}],
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
        ("PR", "[#123 — Correct widget spelling](https://github.com/acme/widgets/pull/123)"),
        ("Author", "@contributor"),
        ("CI", "passing"),
        ("Type", "docs"),
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
                "heading": "PR Snapshot",
                "columns": ["Field", "Value"],
                "reviewers": metadata["reviewer_assessments"],
                "summary": metadata["review_decision"]["summary"],
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
    result["schema_version"] = 3
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


@pytest.mark.parametrize("disposition", ["unavailable", "closed"])
@pytest.mark.parametrize("digest_present", [True, False])
def test_historical_terminal_result_cannot_complete_with_unbound_final(
    tmp_path: Path, disposition: str, digest_present: bool
) -> None:
    """Historical terminal lookup must not certify self-declared or missing final digests."""
    if disposition == "unavailable":
        fixture = _module(Path(__file__).with_name("test_code_review_unavailable_artifact_contract.py"))
        result_path = fixture._write_unavailable_artifact(tmp_path)
    else:
        fixture = _module(Path(__file__).with_name("test_code_review_close_contract.py"))
        result_path = fixture._write_closed_artifact(tmp_path)
    final_bytes = b"Arbitrary terminal final.\n"
    (tmp_path / "final.md").write_bytes(final_bytes)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if digest_present:
        result["metadata"]["final_handoff"] = {"rendered_sha256": hashlib.sha256(final_bytes).hexdigest()}
    result_path.write_text(json.dumps(result), encoding="utf-8")

    finder = _module(FINDER)
    finder.validate_review_result(result_path, parent_thread_id="thread")
    completed = subprocess.run(
        [sys.executable, str(FINDER), "--complete-run", str(tmp_path), "--parent-thread-id", "thread"],
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert "Review handoff blocked: review-completion-requires-bound-schema-v3" in completed.stderr
    assert "Traceback" not in completed.stderr


@pytest.mark.parametrize("disposition", ["unavailable", "closed"])
def test_current_terminal_writer_promotes_and_completes(tmp_path: Path, disposition: str) -> None:
    """A terminal result must pass both validators and completion through the normal writer."""
    if disposition == "unavailable":
        fixture = _module(Path(__file__).with_name("test_code_review_final_handoff_validation.py"))
        result_path = fixture._write_complete_unavailable_v2_artifact(
            tmp_path, {"status": "checkout-command-started", "local_state": "changed-or-unknown"}
        )
    else:
        fixture = _module(Path(__file__).with_name("test_code_review_close_contract.py"))
        result_path = fixture._write_closed_artifact(tmp_path)
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["metadata"]["final_handoff"] = fixture._write_closed_final_handoff(
            tmp_path, result_path, result["metadata"]
        )
        result_path.write_text(json.dumps(result), encoding="utf-8")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if disposition == "unavailable":
        metadata = result["metadata"]
        metadata["confidence_gap_closures"][0]["rationale"] = (
            "(-0.10) " + metadata["confidence_gap_closures"][0]["rationale"]
        )
        previous_limit = metadata["confidence_recovery"]["remaining_limits"][0]
        metadata["confidence_recovery"]["remaining_limits"] = [
            f"(-0.00) {previous_limit} No additional deduction; "
            "the canonical source-verification gap accounts for this limitation."
        ]
        handoff_path = tmp_path / "final-handoff.json"
        handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
        handoff["confidence"]["gaps"] = metadata["confidence_gap_closures"]
        handoff["confidence"]["limits"] = metadata["confidence_recovery"]["remaining_limits"]
        handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
        validation = _module(PLUGIN_ROOT / "shared/final_handoff.py").render_files(
            handoff_path, tmp_path / "final.md", tmp_path / "final-handoff.validation.json"
        )
        metadata["final_handoff"]["handoff_sha256"] = validation["handoff_sha256"]
        metadata["final_handoff"]["rendered_sha256"] = validation["rendered_sha256"]
    result_path.unlink()
    candidate = tmp_path / "result.candidate.json"
    written = subprocess.run(
        [
            sys.executable,
            str(PLUGIN_ROOT / "shared/write-result.py"),
            "--out",
            str(candidate),
            "--gates",
            str(tmp_path / "gates.json"),
            "--status",
            result["status"],
            "--checks-run",
            ",".join(result["checks_run"]),
            "--confidence",
            str(result["confidence"]),
            "--artifact-path",
            str(result_path),
            "--metadata",
            json.dumps(result["metadata"]),
        ],
        capture_output=True,
        text=True,
    )
    assert written.returncode == 0, written.stderr
    assert json.loads(candidate.read_text(encoding="utf-8"))["schema_version"] == 3
    for validator in (
        [
            sys.executable,
            str(PLUGIN_ROOT / "skills/code-review/validate_artifacts.py"),
            "--out",
            str(tmp_path),
            "--result",
            str(candidate),
            "--parent-thread-id",
            "thread",
        ],
        [
            sys.executable,
            str(PLUGIN_ROOT / "shared/validate-artifacts.py"),
            "--skill",
            "code-review",
            "--out",
            str(tmp_path),
            "--result",
            str(candidate),
        ],
    ):
        checked = subprocess.run(validator, capture_output=True, text=True)
        assert checked.returncode == 0, checked.stderr
    candidate.replace(result_path)

    completed = subprocess.run(
        [sys.executable, str(FINDER), "--complete-run", str(tmp_path), "--parent-thread-id", "thread"],
        capture_output=True,
    )

    assert completed.returncode == 0, completed.stderr.decode()
    assert completed.stdout == (tmp_path / "final.md").read_bytes()
    if disposition == "unavailable":
        assert b"0.90 (fair)." in completed.stdout
        assert b"(-0.10) A local checkout command may have changed state" in completed.stdout
        assert b"Limits: (-0.00) PR correctness was not assessed" in completed.stdout


def test_writer_emits_validated_schema_three_assessed_candidate(assessed_pr: Path) -> None:
    """An assessed result must pass both validators and completion through the normal writer."""
    result_path = assessed_pr / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result_path.unlink()
    candidate = assessed_pr / "result.candidate.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(PLUGIN_ROOT / "shared/write-result.py"),
            "--out",
            str(candidate),
            "--gates",
            str(assessed_pr / "gates.json"),
            "--status",
            "pass",
            "--checks-run",
            "lint,format,types,tests,review",
            "--confidence",
            "0.95",
            "--artifact-path",
            str(result_path),
            "--metadata",
            json.dumps(result["metadata"]),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(candidate.read_text(encoding="utf-8"))["schema_version"] == 3
    for validator in (
        [
            sys.executable,
            str(PLUGIN_ROOT / "skills/code-review/validate_artifacts.py"),
            "--out",
            str(assessed_pr),
            "--result",
            str(candidate),
            "--parent-thread-id",
            "thread",
        ],
        [
            sys.executable,
            str(PLUGIN_ROOT / "shared/validate-artifacts.py"),
            "--skill",
            "code-review",
            "--out",
            str(assessed_pr),
            "--result",
            str(candidate),
        ],
    ):
        checked = subprocess.run(validator, capture_output=True, text=True)
        assert checked.returncode == 0, checked.stderr
    candidate.replace(result_path)
    final = subprocess.run(
        [sys.executable, str(FINDER), "--complete-run", str(assessed_pr), "--parent-thread-id", "thread"],
        capture_output=True,
    )
    assert final.returncode == 0, final.stderr.decode()
    assert final.stdout == (assessed_pr / "final.md").read_bytes()


def test_archived_schema_two_remains_valid_but_cannot_certify_current_completion(assessed_pr: Path) -> None:
    """Keep old assessed reports readable without granting the new producer completion contract."""
    result_path = assessed_pr / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["schema_version"] = 2
    handoff_path = assessed_pr / "final-handoff.json"
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    handoff["presentation_version"] = 2
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8", newline="\n")
    validation = _module(PLUGIN_ROOT / "shared/final_handoff.py").render_files(
        handoff_path, assessed_pr / "final.md", assessed_pr / "final-handoff.validation.json"
    )
    result["metadata"]["final_handoff"].update(
        handoff_sha256=validation["handoff_sha256"], rendered_sha256=validation["rendered_sha256"]
    )
    result_path.write_text(json.dumps(result), encoding="utf-8")
    finder = _module(FINDER)

    finder.validate_review_result(result_path, parent_thread_id="thread")
    with pytest.raises(LookupError, match="review-completion-requires-bound-schema-v3"):
        finder.complete_review_run(assessed_pr, parent_thread_id="thread")


@pytest.mark.parametrize("filename", ["result.candidate.json", "result.json"])
def test_current_assessed_completion_requires_presentation_three(assessed_pr: Path, filename: str) -> None:
    """A current assessed result cannot complete with a row-ID-free version-two handoff."""
    handoff_path = assessed_pr / "final-handoff.json"
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    handoff["presentation_version"] = 2
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8", newline="\n")
    validation = _module(PLUGIN_ROOT / "shared/final_handoff.py").render_files(
        handoff_path, assessed_pr / "final.md", assessed_pr / "final-handoff.validation.json"
    )
    result = json.loads((assessed_pr / "result.json").read_text(encoding="utf-8"))
    result["metadata"]["final_handoff"].update(
        handoff_sha256=validation["handoff_sha256"], rendered_sha256=validation["rendered_sha256"]
    )
    result_path = assessed_pr / filename
    result_path.write_text(json.dumps(result), encoding="utf-8", newline="\n")
    finder = _module(FINDER)

    with pytest.raises(LookupError, match="review-current-handoff-presentation-v3-required"):
        if filename == "result.json":
            finder.complete_review_run(assessed_pr, parent_thread_id="thread")
        else:
            finder.validate_review_result(result_path, parent_thread_id="thread")


@pytest.mark.parametrize("filename", ["result.candidate.json", "result.json"])
def test_current_assessed_completion_rejects_caller_contract_branch(assessed_pr: Path, filename: str) -> None:
    """A result with an assessed decision cannot choose the tableless caller-contract branch."""
    handoff_path = assessed_pr / "final-handoff.json"
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    handoff["branch"] = "caller-contract"
    handoff["tables"] = []
    handoff["source_records"] = []
    handoff["source_coverage"] = {
        "source_records_total": 0,
        "represented_source_records_total": 0,
        "omitted_source_records_total": 0,
    }
    handoff["caller_contract"] = {
        "format": "application/json",
        "evidence": "Declared exact output",
        "output": '{"status":"ok"}\n',
    }
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8", newline="\n")
    validation = _module(PLUGIN_ROOT / "shared/final_handoff.py").render_files(
        handoff_path, assessed_pr / "final.md", assessed_pr / "final-handoff.validation.json"
    )
    result = json.loads((assessed_pr / "result.json").read_text(encoding="utf-8"))
    result["metadata"]["final_handoff"].update(
        branch="caller-contract",
        handoff_sha256=validation["handoff_sha256"],
        rendered_sha256=validation["rendered_sha256"],
    )
    result_path = assessed_pr / filename
    result_path.write_text(json.dumps(result), encoding="utf-8", newline="\n")
    finder = _module(FINDER)

    with pytest.raises(LookupError, match="code-review-final-handoff-branch-mismatch"):
        if filename == "result.json":
            finder.complete_review_run(assessed_pr, parent_thread_id="thread")
        else:
            finder.validate_review_result(result_path, parent_thread_id="thread")
    if filename == "result.json":
        result["schema_version"] = 2
        result_path.write_text(json.dumps(result), encoding="utf-8", newline="\n")
        finder.validate_review_result(result_path, parent_thread_id="thread")


def test_promoted_schema_three_rejects_changed_reviewer_rating(assessed_pr: Path) -> None:
    """Revalidation must reject a modified rating after candidate promotion."""
    result_path = assessed_pr / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["metadata"]["reviewer_assessments"][0]["rating"] = 2
    result_path.write_text(json.dumps(result), encoding="utf-8")
    finder = _module(FINDER)

    with pytest.raises(LookupError, match="review-assessment-rating-mismatch:main reviewer"):
        finder.complete_review_run(assessed_pr, parent_thread_id="thread")


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


def test_explicit_pr_result_rejects_newer_unpromoted_run(assessed_pr: Path) -> None:
    """Explicit remediation intake must honor a newer same-PR candidate."""
    newer = assessed_pr.parent / "run-002"
    newer.mkdir()
    (newer / "pr.json").write_bytes((assessed_pr / "pr.json").read_bytes())
    (newer / "result.candidate.json").write_text(
        json.dumps({"metadata": {"scope": "pr", "review_decision": {"recommendation": "needs-more-work"}}}),
        encoding="utf-8",
    )
    finder = _module(FINDER)

    with pytest.raises(LookupError, match="matching-review-candidate-unpromoted"):
        finder.require_assessed_review_result(assessed_pr / "result.json", parent_thread_id="thread")


def test_current_review_rejects_legacy_specialist_manifest(assessed_pr: Path) -> None:
    """A schema-three assessed result must not use a manifest without role-card binding."""
    manifest_path = assessed_pr / "specialist-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = 2
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    validator = _module(PLUGIN_ROOT / "skills/code-review/validate_artifacts.py")

    with pytest.raises(SystemExit, match="current-review-specialist-manifest-schema"):
        validator._validate_result(assessed_pr, assessed_pr / "result.json", assessed_pr, "thread", assessed_pr)


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
        result["metadata"]["operational_blockers"] = [{"id": "G-1", "authors": ["Main reviewer"]}]
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
                        "authors": ["Main reviewer"],
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
            "| Finding / area | Author | Required change | Evidence | Status |\n"
            "| --- | --- | --- | --- | --- |\n"
            "| G-1 | Main reviewer | Resolve test execution failure. | gates.json | Required |\n",
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
            f"role_id: {role}\n## Reviewer Assessment\n\nRating: 3\n"
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
    metadata["review_decision"].update(
        recommendation="needs-more-work",
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
    handoff["outcome"]["summary"] = "Recommendation: needs-more-work."
    snapshot = handoff["tables"][0]
    snapshot.update(reviewers=metadata["reviewer_assessments"], summary=metadata["review_decision"]["summary"])
    snapshot["rows"][-1]["cells"][1] = "needs work"
    handoff["tables"].append(
        {
            "heading": "Review Findings and Merge Blocks",
            "layout": "grouped",
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
    handoff["source_coverage"].update(source_records_total=6, represented_source_records_total=6)
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


@pytest.mark.integration
def test_required_independence_prevents_failed_parent_fallback_completion(assessed_pr: Path) -> None:
    """A failed nonapproval result must not complete expressly required independent review."""
    finalized = _finalize_parent_fallback(assessed_pr, independent_required=True)
    completed = subprocess.run(
        [sys.executable, str(FINDER), "--complete-run", str(assessed_pr), "--parent-thread-id", "thread"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 1, finalized.stdout + completed.stdout
    assert completed.stdout == ""
    assert "independent-review-required" in finalized.stdout + completed.stderr


@pytest.mark.integration
def test_failed_parent_fallback_cannot_complete_with_an_omitted_axis(assessed_pr: Path) -> None:
    """A failed verdict still requires genuine coverage of every triggered review axis."""
    finalized = _finalize_parent_fallback(assessed_pr)
    assert finalized.returncode == 0, finalized.stdout + finalized.stderr
    manifest_path = assessed_pr / "specialist-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["passes"].pop()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
    completed = subprocess.run(
        [sys.executable, str(FINDER), "--complete-run", str(assessed_pr), "--parent-thread-id", "thread"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 1
    assert completed.stdout == ""
    assert "manifest-triggered-role-set-mismatch" in completed.stderr


@pytest.mark.parametrize("tier", ["BROAD", "HIGH_RISK"])
def test_serial_substitutes_cannot_complete_independent_review(assessed_pr: Path, tier: str) -> None:
    """Reject a passing high-risk verdict even when both inline role outputs are complete."""
    _add_substituted_broad_passes(assessed_pr, tier)
    validator = _module(PLUGIN_ROOT / "skills/code-review/validate_artifacts.py")
    with pytest.raises(SystemExit, match="independent-review-required-for-pass:challenger,qa-specialist"):
        validator._validate_result(assessed_pr, assessed_pr / "result.json", assessed_pr, "thread", assessed_pr)


def test_local_multi_role_review_cannot_pass_with_parent_substitutes(assessed_pr: Path) -> None:
    """Require observed parallel specialists even when LOCAL risk permits independent-review substitutes."""
    _add_substituted_broad_passes(assessed_pr, "LOCAL")
    routing_path = assessed_pr / "review-routing.json"
    routing = json.loads(routing_path.read_text(encoding="utf-8"))
    routing["signals"].update(behavior_change=True, explicit_adversarial=True)
    routing_path.write_text(json.dumps(routing), encoding="utf-8")
    result_path = assessed_pr / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["metadata"]["independence_required"] = True
    result_path.write_text(json.dumps(result), encoding="utf-8")

    validator = _module(PLUGIN_ROOT / "skills/code-review/validate_artifacts.py")
    with pytest.raises(SystemExit, match="parallel-review-required-for-pass"):
        validator._validate_result(assessed_pr, result_path, assessed_pr, "thread", assessed_pr)


@pytest.mark.parametrize("proof", ["retained", "missing", "altered"])
def test_promoted_review_binds_retained_role_card_after_installed_change(
    assessed_pr: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, proof: str
) -> None:
    """Revalidate the original card after an installation upgrade, rejecting absent or altered proof."""
    _add_substituted_broad_passes(assessed_pr, "BROAD")
    installed = tmp_path / "changed-plugin" / "roles"
    for role in ("challenger", "qa-specialist"):
        destination = installed / role / "ROLE.md"
        destination.parent.mkdir(parents=True)
        destination.write_bytes((PLUGIN_ROOT / "roles" / role / "ROLE.md").read_bytes() + b"\nUpdated installation.\n")
    retained = assessed_pr / "role-cards" / "challenger" / "ROLE.md"
    if proof == "missing":
        retained.unlink()
    elif proof == "altered":
        retained.write_bytes(retained.read_bytes() + b"\nAltered archive.\n")
    validator = _module(PLUGIN_ROOT / "skills/code-review/validate_artifacts.py")
    monkeypatch.setattr(validator, "PLUGIN_ROOT", installed.parent)
    routing_path = assessed_pr / "review-routing.json"
    routing = json.loads(routing_path.read_text(encoding="utf-8"))
    routing["risk_tier"] = "LOCAL"
    routing["signals"]["behavior_change"] = True
    routing["signals"]["explicit_adversarial"] = True
    routing_path.write_text(json.dumps(routing), encoding="utf-8")
    result_path = assessed_pr / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["metadata"].update(risk_tier="LOCAL", independence_required=True)
    result_path.write_text(json.dumps(result), encoding="utf-8")
    env = validator._ReviewEnvironment(codex_home=assessed_pr, parent_thread_id="thread", project_root=assessed_pr)
    if proof != "retained":
        expected = (
            "role-card-missing:challenger" if proof == "missing" else "manifest-role-card-hash-mismatch:challenger"
        )
        with pytest.raises(SystemExit, match=expected):
            validator._validate_specialist_manifest(assessed_pr, result, result_path, result["metadata"], "LOCAL", env)
        return

    validator._validate_specialist_manifest(assessed_pr, result, result_path, result["metadata"], "LOCAL", env)
    candidate_path = assessed_pr / "result.candidate.json"
    candidate_path.write_bytes(result_path.read_bytes())
    with pytest.raises(SystemExit, match="manifest-role-card-hash-mismatch:challenger"):
        validator._validate_result(assessed_pr, candidate_path, assessed_pr, "thread", assessed_pr)

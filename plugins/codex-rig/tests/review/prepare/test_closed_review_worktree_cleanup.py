"""Exercise closed PR completion and its durable evidence after detached review checkout cleanup."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from _artifact_files import read_json, write_json
from _git_fixtures import git_bytes, git_text
from _modules import load_module

SHARED = Path(__file__).resolve().parents[3] / "shared"
VALIDATION = Path(__file__).resolve().parents[1] / "validation"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Create two committed versions whose verified comparison backs the screening decision."""
    project = tmp_path / "project"
    project.mkdir()
    git_text(project, "init", "-q")
    for version in ("before", "after"):
        (project / "widget.py").write_text(f"value = '{version}'\n", encoding="utf-8")
        git_text(project, "add", "widget.py")
        git_text(project, "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", version)
    return project


@pytest.mark.integration
def test_terminal_close_cleanup_preserves_completion_and_remediation_refusal(
    tmp_path: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source-verified close completes without specialists, replays after removal, and stays ineligible for fixes."""
    monkeypatch.chdir(project)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.syspath_prepend(str(SHARED))
    slot_helper = load_module("close_cleanup_slot", SHARED / "review_checkout_slot.py")
    slot = slot_helper._checkout_slot(project)
    slot_helper._operate("acquire", slot, "close-review", False)

    run = tmp_path / "reports/pr-123/run-001"
    run.mkdir(parents=True)
    close = load_module("close_cleanup_artifact", VALIDATION / "test_code_review_close_contract.py")
    close.BASE_OID = git_text(project, "rev-parse", "HEAD^")
    close.HEAD_OID = git_text(project, "rev-parse", "HEAD")
    result_path = close._write_closed_artifact(run)
    routing = read_json(run / "pr-routing.json")
    routing.update(pr_number=123, pr_url="https://github.com/acme/widgets/pull/123")
    collector = load_module("close_cleanup_collector", SHARED / "collect_pr.py")
    checkout = collector._review_worktree_checkout(
        collector.DEFAULT_RUN_COMMAND,
        30,
        run,
        routing,
        base_oid=close.BASE_OID,
        head_oid=close.HEAD_OID,
        prior_checkout={},
    )
    worktree = Path(checkout["worktree"])
    comparison = f"{close.BASE_OID}...{close.HEAD_OID}"
    (run / "diff.patch").write_bytes(
        git_bytes(worktree, *collector.canonical_diff_arguments(("diff", "--binary", comparison, "--")))
    )
    routing.update(checkout_method=checkout["checkout_method"])
    write_json(run / "pr-routing.json", routing)
    gates = read_json(run / "gates.json")
    binding = load_module("close_cleanup_gates", VALIDATION / "test_code_review_routing_contract.py")
    binding._bind_gates_to_worktree(gates, worktree.as_posix(), close.HEAD_OID)
    write_json(run / "gates.json", gates)
    (run / "review.command.txt").write_text(f"git diff --check {close.BASE_OID}...{close.HEAD_OID}\n", encoding="utf-8")
    git_text(worktree, "diff", "--check", f"{close.BASE_OID}...{close.HEAD_OID}")
    result = read_json(result_path)
    metadata = result["metadata"]
    metadata["final_handoff"] = close._write_closed_final_handoff(run, result_path, metadata)
    completion = load_module("close_cleanup_writer", VALIDATION / "test_review_completion_gate.py")
    completion._promote_through_writer(
        run,
        result_path,
        status=result["status"],
        checks_run=",".join(result["checks_run"]),
        confidence=result["confidence"],
        metadata=metadata,
    )

    finder = [sys.executable, str(SHARED / "find-review-report.py")]
    complete = [*finder, "--complete-run", str(run)]
    before = subprocess.run(complete, capture_output=True, check=False)
    assert before.returncode == 0, before.stderr.decode()
    assert not (run / "specialist-manifest.json").exists()
    cleanup = subprocess.run(
        [
            sys.executable,
            str(SHARED / "review_worktree_cleanup.py"),
            "--run",
            str(run),
            "--worktree",
            str(worktree),
            "--checkout-sha256",
            hashlib.sha256((run / "local-checkout.json").read_bytes()).hexdigest(),
            "--slot",
            str(slot),
            "--owner",
            "close-review",
            "--no-contributor-execution",
        ],
        capture_output=True,
        check=False,
    )
    assert cleanup.returncode == 0, cleanup.stdout.decode()
    assert json.loads(cleanup.stdout)["status"] == "removed"
    assert not worktree.exists()
    assert worktree.as_posix() not in git_text(project, "worktree", "list", "--porcelain")
    after = subprocess.run(complete, capture_output=True, check=False)
    assert after.returncode == 0, after.stderr.decode()
    assert before.stdout == after.stdout == (run / "final.md").read_bytes()
    intake = subprocess.run([*finder, "--result", str(result_path)], capture_output=True, check=False)
    assert intake.returncode != 0
    assert "matching-review-closed-not-remediable" in intake.stderr.decode()
    original_comments = (run / "comments.json").read_bytes()
    write_json(run / "comments.json", [{"body": "Changed after screening"}])
    altered = subprocess.run(complete, capture_output=True, check=False)
    assert altered.returncode != 0
    assert "review-cleanup-source-evidence-changed" in altered.stderr.decode()
    (run / "comments.json").write_bytes(original_comments)
    slot_helper._operate("release", slot, "close-review", False)

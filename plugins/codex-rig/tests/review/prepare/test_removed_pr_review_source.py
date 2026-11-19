"""Exercise detached PR review source validation after terminal cleanup through the direct and batched consumers."""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import test_review_direct as direct
from _artifact_files import read_json, write_json
from _git_fixtures import git_text
from test_review_batches import _batch_command, _batch_producer, _completed_source_only, _in_process_step
from test_review_prepare import _pr_fixture_receipt, _prepare


def _collect_fixture_pr(run: Path) -> Path:
    """Retain the fixture's selected source as a real committed comparison in a fresh detached checkout."""
    repo = run.parent / "repository"
    base = git_text(repo, "rev-parse", "HEAD")
    git_text(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qam", "reviewed tip")
    head = git_text(repo, "rev-parse", "HEAD")
    worktree = run.parent / "collection-review-worktree"
    git_text(repo, "worktree", "add", "--detach", str(worktree), head)
    (run / "local-source" / "review-worktree.json").unlink()
    checkout = {
        "checkout_mode": "review",
        "checkout_method": "git-detached-review-worktree",
        "created_worktree": {"owner": "a" * 32},
        **_pr_fixture_receipt(worktree, head, base),
        "source_worktree": repo.as_posix(),
    }
    write_json(run / "local-checkout.json", checkout)
    return worktree


def _direct_pr_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Build full native direct rollout evidence using the PR collector fixture instead of the local mirror."""

    def prepare(run: Path) -> subprocess.CompletedProcess[str]:
        """Freeze the detached comparison using ordinary direct preparation."""
        return _prepare(run, source_root=_collect_fixture_pr(run), paged=False)

    monkeypatch.setattr(direct, "_direct_prepare", prepare)
    run, home = direct._direct_evidence(tmp_path)
    assembled = direct._assemble_direct(run, home)
    assert assembled.returncode == 0, assembled.stderr
    return run, home


def _batched_pr_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Assemble every native source wave from a real PR-bound batch inventory."""

    def prepare(run: Path, operation: str, home: Path | None = None, *, source_only: bool = False) -> None:
        """Use detached PR source during preparation and the ordinary native lifecycle afterwards."""
        if operation == "prepare":
            result = _prepare(run, source_root=_collect_fixture_pr(run), paged=False, batches=True)
            assert result.returncode == 0, result.stderr
        else:
            _in_process_step(run, operation, home, source_only=source_only)

    run, home = _completed_source_only(tmp_path, step=prepare)
    assembled = _batch_command(run, "assemble-batches", home)
    assert assembled.returncode == 0, assembled.stderr
    return run, home


@pytest.mark.integration
@pytest.mark.parametrize(
    ("arrange", "plan_name"),
    [
        pytest.param(_direct_pr_evidence, "direct-plan.json", id="direct"),
        pytest.param(_batched_pr_evidence, "batch-inventory.json", id="batched"),
    ],
)
def test_removed_pr_plan_replays_only_during_bound_completed_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arrange: Callable[[Path, pytest.MonkeyPatch], tuple[Path, Path]],
    plan_name: str,
) -> None:
    """Native direct and aggregate validation survive removal; candidates and edited inputs remain rejected."""
    run, home = arrange(tmp_path, monkeypatch)
    checkout = read_json(run / "local-checkout.json")
    repo = Path(checkout["source_worktree"])
    worktree = Path(checkout["worktree"])
    head = checkout["expected_head"]
    plan_path = run / plan_name
    plan = read_json(plan_path)
    manifest = read_json(run / "specialist-manifest.json")
    producer, _ = _batch_producer()
    write_json(run / "result.json", {"status": "pass", "metadata": {}})
    from review_worktree_replay import completed_source_validation, seal_source_witness, verified_source_inputs

    receipt = {
        "schema_version": 1,
        "status": "removal-authorized",
        "worktree": worktree.as_posix(),
        "source_worktree": repo.as_posix(),
        "head": head,
        "run_dir": run.as_posix(),
        "local_checkout_sha256": hashlib.sha256((run / "local-checkout.json").read_bytes()).hexdigest(),
        "result_sha256": hashlib.sha256((run / "result.json").read_bytes()).hexdigest(),
        "source_inputs": verified_source_inputs(run),
    }

    def validate() -> dict[str, Any]:
        """Exercise all native execution validation, including reviewer histories, source selection, and coverage."""
        return producer.validator._validate_review_runtime(run, manifest, manifest["passes"], home, "parent")

    live = validate()
    seal_source_witness(run, repo, receipt)
    write_json(run / "review-worktree-cleanup.json", receipt)
    git_text(repo, "worktree", "remove", str(worktree))

    with completed_source_validation(run, run / "result.json"):
        assert validate() == live
    with (
        completed_source_validation(run, run / "result.candidate.json"),
        pytest.raises((FileNotFoundError, RuntimeError, SystemExit, ValueError)),
    ):
        validate()
    receipt["status"] = "removed"
    write_json(run / "review-worktree-cleanup.json", receipt)
    changed_paths = (run / "files.txt").read_bytes()
    (run / "files.txt").write_text("altered-inventory.py\n", encoding="utf-8")
    with (
        pytest.raises(ValueError, match="review-cleanup-source-evidence-changed"),
        completed_source_validation(run, run / "result.json"),
    ):
        validate()

    # A report writer can hash its own changes, but cannot replace a create-only witness in the original Git repository.
    (run / "files.txt").write_bytes(changed_paths)
    old_plan_digest = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    plan["source_snapshot"]["files"][0].update(
        content="forged source", sha256=hashlib.sha256(b"forged source").hexdigest()
    )
    write_json(plan_path, plan)
    manifest_path = run / "specialist-manifest.json"
    new_plan_digest = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    manifest_path.write_bytes(manifest_path.read_bytes().replace(old_plan_digest.encode(), new_plan_digest.encode()))
    write_json(run / "result.json", {"status": "pass", "metadata": {}, "summary": "Altered assessment."})
    receipt["result_sha256"] = hashlib.sha256((run / "result.json").read_bytes()).hexdigest()
    receipt["source_inputs"] = verified_source_inputs(run)
    write_json(run / "review-worktree-cleanup.json", receipt)
    with (
        pytest.raises(ValueError, match="review-cleanup-witness-mismatch"),
        completed_source_validation(run, run / "result.json"),
    ):
        validate()
    with pytest.raises(ValueError, match="review-cleanup-witness-mismatch"):
        seal_source_witness(run, repo, receipt)


@pytest.mark.integration
@pytest.mark.parametrize("location", ["inside-report", "unrelated-checkout"])
def test_canonical_report_refuses_another_witness_repository(tmp_path: Path, location: str) -> None:
    """A report cannot redirect its write-once witness into its own files or an unrelated Git repository."""
    producer, _ = _batch_producer()
    owner = tmp_path / "owner"
    run = owner / ".reports/codex/code-review/pr-7/run-001"
    run.mkdir(parents=True)
    other = (run if location == "inside-report" else tmp_path) / "other"
    other.mkdir()
    git_text(owner, "init", "-q")
    git_text(other, "init", "-q")
    with pytest.raises(ValueError, match="review-cleanup-witness-repository-mismatch"):
        producer.review_worktree_replay.seal_source_witness(run, other, {"source_worktree": other.as_posix()})
    assert git_text(other, "for-each-ref", "refs/codex-rig").strip() == ""


@pytest.mark.integration
def test_canonical_witness_rejects_missing_cleanup_and_creation_records(tmp_path: Path) -> None:
    """Dropping both in-report markers cannot turn a sealed review into an ordinary historical pre-cleanup run."""
    producer, _ = _batch_producer()
    repo = tmp_path / "project"
    run = repo / ".reports/codex/code-review/pr-9/run-001"
    run.mkdir(parents=True)
    git_text(repo, "init", "-q")
    receipt = {
        "schema_version": 1,
        "run_dir": run.as_posix(),
        "worktree": (repo / "removed").as_posix(),
        "source_worktree": repo.as_posix(),
        "head": "a" * 40,
        "result_sha256": "b" * 64,
        "local_checkout_sha256": "c" * 64,
        "source_inputs": {},
    }
    producer.review_worktree_replay.seal_source_witness(run, repo, receipt)
    with (
        pytest.raises(ValueError, match="review-cleanup-witness-missing"),
        producer.review_worktree_replay.completed_source_validation(run, run / "result.json"),
    ):
        pytest.fail("A sealed canonical report must retain its cleanup identity.")

"""Lifecycle tests for the disposable single-file patch executor."""

from __future__ import annotations

import difflib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from _frozen_repo import BENCHMARKS, FROZEN_REPO, requires_frozen_repo

SUITE_PATH = Path(__file__).resolve().parents[2] / "suites" / "tasks-fix-single.json"
sys.path.insert(0, str(BENCHMARKS))

from _bench_codex.stage_fix import build_fix_single_contract, execute_fix_single_patch  # noqa: E402

_PATIENCE_ASSIGNMENT = "        self.patience = patience\n"
_PATIENCE_GUARD = (
    "        if patience < 1:\n"
    '            raise MisconfigurationException(f"patience must be >= 1, got {patience}")\n'
    "        self.patience = patience\n"
)


def _contract() -> object:
    """Return the first deterministic pilot contract.

    Example:
        >>> _contract().task_id
        'FS-01'
    """
    tasks = json.loads(SUITE_PATH.read_text(encoding="utf-8"))
    return build_fix_single_contract(next(task for task in tasks if task["id"] == "FS-01"))


def _patience_guard_patch(contract: object) -> str:
    """Return the ordinary unified diff that adds the FS-01 patience guard to the frozen source."""
    path = contract.expected_paths[0]
    before = (FROZEN_REPO / path).read_text(encoding="utf-8")
    after = before.replace(_PATIENCE_ASSIGNMENT, _PATIENCE_GUARD, 1)
    assert after != before
    body = difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True), fromfile=f"a/{path}", tofile=f"b/{path}"
    )
    return f"diff --git a/{path} b/{path}\n{''.join(body)}"


def _worktree_list(repo: Path) -> str:
    """Return the porcelain worktree listing of a checkout."""
    return subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        capture_output=True,
        check=True,
        text=True,
        timeout=10,
    ).stdout


class TestExecuteFixSinglePatch:
    """Applying, scoring, and cleaning up one candidate patch in a disposable worktree."""

    @requires_frozen_repo
    def test_applies_scores_and_cleans_candidate_patch(self) -> None:
        """A known behavioral patch proves baseline, path boundary, oracle, and rollback evidence together.

        The FS-01 patience guard is applied to the frozen checkout; the result must show a failing baseline, an applied
        patch limited to the expected path, a passing oracle, and a verified cleanup.
        """
        contract = _contract()

        result = execute_fix_single_patch(FROZEN_REPO, contract, _patience_guard_patch(contract))

        assert result.baseline_failed is True
        assert result.patch_applied is True
        assert result.changed_paths == contract.expected_paths
        assert result.targeted_test_passed is True
        assert result.recount_recoverable is False
        assert result.recount_oracle_passed is None
        assert result.cleanup_verified is True
        assert result.error is None

    def test_rejects_missing_checkout(self, tmp_path: Path) -> None:
        """An empty directory is rejected as a source before any worktree cell is created.

        The source is a plain directory without a Git checkout, so baseline admission must fail with a Git-checkout
        error instead of creating anything.
        """
        source = tmp_path / "source"
        source.mkdir()

        with pytest.raises(ValueError, match="Git checkout"):
            execute_fix_single_patch(source, _contract(), "diff --git a/a b/a\n")

    def test_rejects_wrong_baseline_checkout_without_new_worktrees(self) -> None:
        """A checkout at the wrong commit is rejected and its worktree list stays unchanged.

        The repository root is a real Git checkout whose HEAD is not the frozen baseline, so the executor must refuse it
        before adding a worktree; the real listing before and after is the contract.
        """
        worktrees_before = _worktree_list(BENCHMARKS.parent)

        with pytest.raises(ValueError, match="does not match"):
            execute_fix_single_patch(BENCHMARKS.parent, _contract(), "diff --git a/a b/a\n")

        assert _worktree_list(BENCHMARKS.parent) == worktrees_before

    @requires_frozen_repo
    def test_reports_verified_cleanup_for_rejected_patch(self) -> None:
        """A failed apply cannot erase the worktree-cleanup evidence from the result.

        A patch that cannot apply is submitted against the frozen checkout; the result must still carry the failing
        baseline and a verified cleanup of the disposable worktree.
        """
        result = execute_fix_single_patch(FROZEN_REPO, _contract(), "diff --git a/a b/a\n")

        assert result.baseline_failed is True
        assert result.patch_applied is False
        assert result.recount_recoverable is False
        assert result.recount_oracle_passed is None
        assert result.cleanup_verified is True

    @requires_frozen_repo
    def test_hunk_count_error_is_diagnostic_only(self) -> None:
        """A recount-valid candidate stays primary-ineligible when ordinary apply rejects it.

        The correct FS-01 patch gets one hunk header line count inflated; ordinary apply must reject it while the
        recount diagnostic still shows it would have passed the oracle.
        """
        contract = _contract()
        malformed = re.sub(
            r"@@ -(\d+),(\d+) \+(\d+),(\d+) @@",
            lambda m: f"@@ -{m.group(1)},{int(m.group(2)) + 1} +{m.group(3)},{int(m.group(4)) + 1} @@",
            _patience_guard_patch(contract),
            count=1,
        )

        result = execute_fix_single_patch(FROZEN_REPO, contract, malformed)

        assert result.patch_applied is False
        assert result.targeted_test_passed is False
        assert result.recount_recoverable is True
        assert result.recount_oracle_passed is True
        assert result.cleanup_verified is True

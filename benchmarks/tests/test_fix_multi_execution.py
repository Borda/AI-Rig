"""Disposable-worktree lifecycle tests for complete-caller patches."""

from __future__ import annotations

import difflib
import json
import os
from pathlib import Path
import sys

import pytest

from _launcher_capability import _pinned_frozen_checkout_is_available

SUITE_PATH = Path(__file__).resolve().parents[1] / "suites" / "tasks-fix-multi.json"
BENCHMARKS = Path(__file__).resolve().parents[1]
FROZEN_REPO = Path(os.environ.get("PL_REPO_PATH", str(BENCHMARKS.parent / ".sandbox" / "pytorch-lightning")))
FROZEN_REPO_COMMIT = "be98784a1a03581b7051a355ae1084fd352d7cea"
sys.path.insert(0, str(BENCHMARKS))


_requires_frozen_repo = pytest.mark.skipif(
    not _pinned_frozen_checkout_is_available(FROZEN_REPO, FROZEN_REPO_COMMIT),
    reason=f"pinned frozen benchmark checkout is unavailable at {FROZEN_REPO}",
)


def _runner() -> object:
    """Return the private Fix stage without invoking its CLI.

    Example:
        >>> _runner().__name__
        '_bench_codex.stage_fix'
    """
    from _bench_codex import stage_fix

    return stage_fix


_RUNNER = _runner()


build_fix_multi_contract = _RUNNER.build_fix_multi_contract
execute_fix_multi_patch = _RUNNER.execute_fix_multi_patch


def _contract() -> object:
    """Return the deterministic FM-01 contract used for lifecycle admission.

    Example:
        >>> _contract().task_id
        'FM-01'
    """
    tasks = json.loads(SUITE_PATH.read_text(encoding="utf-8"))
    return build_fix_multi_contract(next(task for task in tasks if task["id"] == "FM-01"))


def _contract_for(task_id: str) -> object:
    """Return the requested immutable multi-file contract from the canonical suite.

    Example:
        >>> _contract_for("not-a-real-task")
        Traceback (most recent call last):
            ...
        StopIteration
    """
    tasks = json.loads(SUITE_PATH.read_text(encoding="utf-8"))
    return build_fix_multi_contract(next(task for task in tasks if task["id"] == task_id))


def _complete_early_stopping_source(source: str) -> str:
    """Return the FM-01 source with every caller and its observe-only branch updated.

    Example:
        >>> _complete_early_stopping_source("self._run_early_stopping_check(trainer)")
        'self._run_early_stopping_check(trainer, dry_run=False)'
    """
    return (
        source.replace(
            'def _run_early_stopping_check(self, trainer: "pl.Trainer") -> None:',
            'def _run_early_stopping_check(self, trainer: "pl.Trainer", dry_run: bool = False) -> None:',
        )
        .replace("self._run_early_stopping_check(trainer)", "self._run_early_stopping_check(trainer, dry_run=False)")
        .replace(
            "        trainer.should_stop = trainer.should_stop or should_stop\n",
            "        if dry_run:\n"
            '            log.info(f"dry run: should_stop={should_stop}, reason={reason}")\n'
            "            return\n"
            "        trainer.should_stop = trainer.should_stop or should_stop\n",
        )
    )


def _complete_strategy_environment_source(source: str, *, is_base: bool) -> str:
    """Return one FM-03 source with cooperative verbose environment propagation.

    Example:
        >>> _complete_strategy_environment_source("super().setup_environment()", is_base=False)
        'super().setup_environment(verbose=verbose)'
        >>> _complete_strategy_environment_source("super().setup_environment()", is_base=True)
        'super().setup_environment()'
    """
    source = source.replace(
        "def setup_environment(self) -> None:",
        "def setup_environment(self, verbose: bool = False) -> None:",
        1,
    )
    if is_base:
        return source.replace(
            "        assert self.accelerator is not None\n",
            "        if verbose:\n"
            '            log.debug("setting up strategy environment")\n'
            "        assert self.accelerator is not None\n",
            1,
        )
    return source.replace(
        "super().setup_environment()",
        "super().setup_environment(verbose=verbose)",
        1,
    )


def _complete_model_checkpoint_source(source: str) -> str:
    """Return the FM-02 source with exact persistence-boundary provenance labels.

    Example:
        >>> source = "; ".join(["self._save_checkpoint(trainer, filepath)"] * 2)
        >>> first, second = _complete_model_checkpoint_source(source).split("; ")
        >>> first
        "self._save_checkpoint(trainer, filepath, reason='exception')"
        >>> second
        "self._save_checkpoint(trainer, filepath, reason='last')"
    """
    source = source.replace(
        'def _save_checkpoint(self, trainer: "pl.Trainer", filepath: str) -> None:',
        'def _save_checkpoint(self, trainer: "pl.Trainer", filepath: str, reason: str = "") -> None:',
    ).replace(
        "        trainer.save_checkpoint(filepath, self.save_weights_only)\n",
        "        if reason:\n"
        '            rank_zero_info(f"{reason}: saving checkpoint to {filepath}")\n'
        "        trainer.save_checkpoint(filepath, self.save_weights_only)\n",
        1,
    )
    for reason in ("exception", "last", "none", "top_k"):
        source = source.replace(
            "self._save_checkpoint(trainer, filepath)",
            f"self._save_checkpoint(trainer, filepath, reason={reason!r})",
            1,
        )
    return source


def _patch_for_sources(before: dict[str, str], after: dict[str, str]) -> str:
    """Build one ordinary multi-file unified diff from deterministic candidate sources.

    Example:
        >>> patch = _patch_for_sources({"example.py": "old" + chr(10)}, {"example.py": "new" + chr(10)})
        >>> patch.splitlines()[-2:]
        ['-old', '+new']
    """
    return "".join(
        f"diff --git a/{path} b/{path}\n"
        + "".join(
            difflib.unified_diff(
                before[path].splitlines(keepends=True),
                after[path].splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
            )
        )
        for path in before
    )


@_requires_frozen_repo
def test_complete_multi_file_candidate_is_applied_scored_and_cleaned() -> None:
    """A known complete patch proves apply, caller completeness, and rollback evidence together."""
    contract = _contract()
    source_path = FROZEN_REPO / contract.expected_paths[0]
    before = source_path.read_text(encoding="utf-8")
    after = _complete_early_stopping_source(before)
    diff = _patch_for_sources({contract.expected_paths[0]: before}, {contract.expected_paths[0]: after})

    result = execute_fix_multi_patch(FROZEN_REPO, contract, diff)

    assert result.baseline_failed is True
    assert result.patch_applied is True
    assert set(result.changed_paths) == set(contract.expected_paths)
    assert result.targeted_test_passed is True
    assert result.cleanup_verified is True


@_requires_frozen_repo
def test_incomplete_early_stopping_patch_is_scored_false_and_cleaned() -> None:
    """FM-01 lifecycle scoring rejects a patch with one stale internal caller."""
    contract = _contract()
    source_path = FROZEN_REPO / contract.expected_paths[0]
    before = source_path.read_text(encoding="utf-8")
    after = _complete_early_stopping_source(before).replace("trainer, dry_run=False)", "trainer)", 1)
    diff = _patch_for_sources({contract.expected_paths[0]: before}, {contract.expected_paths[0]: after})

    result = execute_fix_multi_patch(FROZEN_REPO, contract, diff)

    assert result.baseline_failed is True
    assert result.patch_applied is True
    assert set(result.changed_paths) == set(contract.expected_paths)
    assert result.targeted_test_passed is False
    assert result.cleanup_verified is True


@_requires_frozen_repo
def test_complete_strategy_environment_patch_is_applied_scored_and_cleaned() -> None:
    """A six-file FM-03 patch proves lifecycle scoring requires every cooperative override."""
    contract = _contract_for("FM-03")
    before = {
        relative_path: (FROZEN_REPO / relative_path).read_text(encoding="utf-8")
        for relative_path in contract.expected_paths
    }
    after = {
        relative_path: _complete_strategy_environment_source(
            source,
            is_base=relative_path.endswith("strategies/strategy.py"),
        )
        for relative_path, source in before.items()
    }

    result = execute_fix_multi_patch(FROZEN_REPO, contract, _patch_for_sources(before, after))

    assert result.baseline_failed is True
    assert result.patch_applied is True
    assert set(result.changed_paths) == set(contract.expected_paths)
    assert result.targeted_test_passed is True
    assert result.cleanup_verified is True


@_requires_frozen_repo
def test_incomplete_strategy_environment_patch_is_scored_false_and_cleaned() -> None:
    """FM-03 lifecycle scoring rejects one stale cooperative override."""
    contract = _contract_for("FM-03")
    before = {
        relative_path: (FROZEN_REPO / relative_path).read_text(encoding="utf-8")
        for relative_path in contract.expected_paths
    }
    after = {
        relative_path: _complete_strategy_environment_source(
            source,
            is_base=relative_path.endswith("strategies/strategy.py"),
        )
        for relative_path, source in before.items()
    }
    missing_forward = contract.expected_paths[-1]
    after[missing_forward] = after[missing_forward].replace(
        "super().setup_environment(verbose=verbose)", "super().setup_environment()", 1
    )

    result = execute_fix_multi_patch(FROZEN_REPO, contract, _patch_for_sources(before, after))

    assert result.baseline_failed is True
    assert result.patch_applied is True
    assert set(result.changed_paths) == set(contract.expected_paths)
    assert result.targeted_test_passed is False
    assert result.cleanup_verified is True


@_requires_frozen_repo
def test_complete_model_checkpoint_patch_is_applied_scored_and_cleaned() -> None:
    """A complete FM-02 patch proves four exact labels reach the pre-save log boundary."""
    contract = _contract_for("FM-02")
    source_path = FROZEN_REPO / contract.expected_paths[0]
    before = source_path.read_text(encoding="utf-8")
    after = _complete_model_checkpoint_source(before)

    result = execute_fix_multi_patch(
        FROZEN_REPO,
        contract,
        _patch_for_sources({contract.expected_paths[0]: before}, {contract.expected_paths[0]: after}),
    )

    assert result.baseline_failed is True
    assert result.patch_applied is True
    assert set(result.changed_paths) == set(contract.expected_paths)
    assert result.targeted_test_passed is True
    assert result.cleanup_verified is True

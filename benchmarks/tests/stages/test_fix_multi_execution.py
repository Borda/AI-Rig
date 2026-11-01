"""Disposable-worktree lifecycle tests for complete-caller patches."""

from __future__ import annotations

import difflib
import json
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from _fix_multi_sources import (
    FORWARDED_SETUP,
    complete_early_stopping_source,
    complete_model_checkpoint_source,
    complete_strategy_environment_source,
)
from _frozen_repo import BENCHMARKS, FROZEN_REPO, requires_frozen_repo

SUITE_PATH = Path(__file__).resolve().parents[2] / "suites" / "tasks-fix-multi.json"
sys.path.insert(0, str(BENCHMARKS))

from _bench_codex.stage_fix import build_fix_multi_contract, execute_fix_multi_patch  # noqa: E402


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


def _early_stopping_complete(before: dict[str, str]) -> dict[str, str]:
    """Return the complete FM-01 candidate sources."""
    return {path: complete_early_stopping_source(source) for path, source in before.items()}


def _early_stopping_stale_caller(before: dict[str, str]) -> dict[str, str]:
    """Return FM-01 candidate sources where one internal caller still uses the old signature."""
    return {
        path: complete_early_stopping_source(source).replace("trainer, dry_run=False)", "trainer)", 1)
        for path, source in before.items()
    }


def _strategy_complete(before: dict[str, str]) -> dict[str, str]:
    """Return the complete FM-03 candidate sources for the base strategy and every override."""
    return {
        path: complete_strategy_environment_source(source, is_base=path.endswith("strategies/strategy.py"))
        for path, source in before.items()
    }


def _strategy_stale_override(before: dict[str, str]) -> dict[str, str]:
    """Return FM-03 candidate sources where the last override stops forwarding verbose."""
    after = _strategy_complete(before)
    last_path = list(before)[-1]
    after[last_path] = after[last_path].replace(FORWARDED_SETUP, "super().setup_environment()", 1)
    return after


def _model_checkpoint_complete(before: dict[str, str]) -> dict[str, str]:
    """Return the complete FM-02 candidate sources."""
    return {path: complete_model_checkpoint_source(source) for path, source in before.items()}


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


@requires_frozen_repo
class TestExecuteFixMultiPatch:
    """Applying, scoring, and cleaning up one multi-file candidate patch in a disposable worktree."""

    @pytest.mark.parametrize(
        ("task_id", "rewrite", "targeted_test_passed"),
        [
            pytest.param("FM-01", _early_stopping_complete, True, id="early-stopping-complete"),
            pytest.param("FM-01", _early_stopping_stale_caller, False, id="early-stopping-stale-caller"),
            pytest.param("FM-03", _strategy_complete, True, id="strategy-environment-complete"),
            pytest.param("FM-03", _strategy_stale_override, False, id="strategy-environment-stale-override"),
            pytest.param("FM-02", _model_checkpoint_complete, True, id="model-checkpoint-complete"),
        ],
    )
    def test_applies_scores_and_cleans_candidate(
        self, task_id: str, rewrite: Callable[[dict[str, str]], dict[str, str]], targeted_test_passed: bool
    ) -> None:
        """A candidate patch is applied, scored by caller completeness, and rolled back with evidence.

        Each candidate rewrites the contract's frozen sources: complete ones must pass the targeted oracle and
        incomplete ones (a stale caller or override) must fail it, while the patch always applies, stays inside the
        expected paths, and leaves a verified cleanup.
        """
        contract = _contract_for(task_id)
        before = {path: (FROZEN_REPO / path).read_text(encoding="utf-8") for path in contract.expected_paths}

        result = execute_fix_multi_patch(FROZEN_REPO, contract, _patch_for_sources(before, rewrite(before)))

        assert result.baseline_failed is True
        assert result.patch_applied is True
        assert set(result.changed_paths) == set(contract.expected_paths)
        assert result.targeted_test_passed is targeted_test_passed
        assert result.cleanup_verified is True

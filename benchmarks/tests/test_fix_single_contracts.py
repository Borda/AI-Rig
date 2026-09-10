"""Executable-oracle tests for the single-file fix pilot."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import pytest

from _launcher_capability import _pinned_frozen_checkout_is_available

SUITE_PATH = Path(__file__).resolve().parents[1] / "suites" / "tasks-fix-single.json"
BENCHMARKS = Path(__file__).resolve().parents[1]
FROZEN_REPO = Path(os.environ.get("PL_REPO_PATH", str(BENCHMARKS.parent / ".sandbox" / "pytorch-lightning")))
FROZEN_REPO_COMMIT = "be98784a1a03581b7051a355ae1084fd352d7cea"
sys.path.insert(0, str(BENCHMARKS))

from _bench_common.edit_patch_contracts import (  # noqa: E402
    build_fix_single_contract,
    run_fix_single_oracle,
    validate_fix_single_binding,
)


_requires_frozen_repo = pytest.mark.skipif(
    not _pinned_frozen_checkout_is_available(FROZEN_REPO, FROZEN_REPO_COMMIT),
    reason=f"pinned frozen benchmark checkout is unavailable at {FROZEN_REPO}",
)


def _tasks() -> dict[str, dict[str, object]]:
    """Load the committed scaffold task bytes once per test.

    Example:
        >>> tasks = _tasks()
        >>> all(key == task["id"] for key, task in tasks.items()) and bool(tasks)
        True
    """
    return {task["id"]: task for task in json.loads(SUITE_PATH.read_text(encoding="utf-8"))}


def test_pinned_frozen_checkout_rejects_git_directory_without_head(tmp_path: Path) -> None:
    """A partial Git directory cannot admit source-dependent contract coverage."""
    source = tmp_path / "source"
    (source / ".git").mkdir(parents=True)

    assert source.is_dir()
    assert _pinned_frozen_checkout_is_available(source, FROZEN_REPO_COMMIT) is False


@_requires_frozen_repo
@pytest.mark.parametrize("task_id", ("FS-01", "FS-02", "FS-03", "FS-04"))
def test_oracle_rejects_the_frozen_unfixed_source(task_id: str) -> None:
    """Each selected task has a real failing baseline at the locked revision."""
    contract = build_fix_single_contract(_tasks()[task_id])

    assert run_fix_single_oracle(FROZEN_REPO, contract) is False


@_requires_frozen_repo
def test_patience_oracle_accepts_a_behavioral_fix(tmp_path: Path) -> None:
    """A guard must reject zero while retaining a legal positive value."""
    task = _tasks()["FS-01"]
    contract = build_fix_single_contract(task)
    source_path = FROZEN_REPO / contract.expected_paths[0]
    candidate_path = tmp_path / contract.expected_paths[0]
    candidate_path.parent.mkdir(parents=True)
    candidate_path.write_text(
        source_path.read_text(encoding="utf-8").replace(
            "        self.patience = patience\n",
            "        if patience < 1:\n"
            '            raise MisconfigurationException(f"patience must be >= 1, got {patience}")\n'
            "        self.patience = patience\n",
            1,
        ),
        encoding="utf-8",
    )

    assert run_fix_single_oracle(tmp_path, contract) is True


@_requires_frozen_repo
@pytest.mark.parametrize(
    ("task_id", "old", "new"),
    (
        pytest.param(
            "FS-02",
            "        self.min_delta = min_delta\n",
            "        if min_delta < 0:\n"
            '            raise MisconfigurationException(f"min_delta must be >= 0, got {min_delta}")\n'
            "        self.min_delta = min_delta\n",
            id="fs-02",
        ),
        pytest.param(
            "FS-03",
            "        trainer.save_checkpoint(filepath, self.save_weights_only)\n",
            "        if trainer.global_step == self._last_global_step_saved:\n"
            '            rank_zero_info("Skipping duplicate checkpoint save")\n'
            "            return\n"
            "        trainer.save_checkpoint(filepath, self.save_weights_only)\n",
            id="fs-03",
        ),
        pytest.param(
            "FS-04",
            "        if self.save_top_k < -1:\n",
            "        if self.save_top_k == 0:\n"
            '            rank_zero_warn("ModelCheckpoint(save_top_k=0) is set: no checkpoints will be saved. '
            'Pass save_top_k=-1 to save all checkpoints.")\n'
            "        if self.save_top_k < -1:\n",
            id="fs-04",
        ),
    ),
)
def test_each_remaining_oracle_accepts_its_minimal_behavioral_fix(
    tmp_path: Path, task_id: str, old: str, new: str
) -> None:
    """Every expansion task has positive executable evidence, not just a failing baseline."""
    contract = build_fix_single_contract(_tasks()[task_id])
    source_path = FROZEN_REPO / contract.expected_paths[0]
    candidate_path = tmp_path / contract.expected_paths[0]
    candidate_path.parent.mkdir(parents=True)
    candidate = source_path.read_text(encoding="utf-8").replace(old, new, 1)
    assert candidate != source_path.read_text(encoding="utf-8")
    candidate_path.write_text(candidate, encoding="utf-8")

    assert run_fix_single_oracle(tmp_path, contract) is True


def test_provider_binding_rejects_a_changed_oracle() -> None:
    """A transport cannot substitute a provider-specific execution oracle."""
    contract = build_fix_single_contract(_tasks()["FS-01"])
    binding = dict(contract.provider_binding())
    validate_fix_single_binding(contract, binding)
    binding["oracle_sha256"] = "0" * 64

    with pytest.raises(ValueError, match="scientific fields"):
        validate_fix_single_binding(contract, binding)

"""Executable-oracle tests for the single-file fix pilot."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from _frozen_repo import BENCHMARKS, FROZEN_REPO, requires_frozen_repo

SUITE_PATH = Path(__file__).resolve().parents[2] / "suites" / "tasks-fix-single.json"
sys.path.insert(0, str(BENCHMARKS))

from _bench_common.edit_patch_contracts import (  # noqa: E402
    build_fix_single_contract,
    run_fix_single_oracle,
    validate_fix_single_binding,
)


def _tasks() -> dict[str, dict[str, object]]:
    """Load the committed scaffold task bytes once per test.

    Example:
        >>> tasks = _tasks()
        >>> all(key == task["id"] for key, task in tasks.items()) and bool(tasks)
        True
    """
    return {task["id"]: task for task in json.loads(SUITE_PATH.read_text(encoding="utf-8"))}


@requires_frozen_repo
class TestFrozenOracle:
    """Fix-single oracles scored against the pinned frozen checkout."""

    @pytest.mark.parametrize("task_id", ["FS-01", "FS-02", "FS-03", "FS-04"])
    def test_rejects_unfixed_source(self, task_id: str) -> None:
        """Each selected task has a real failing baseline at the locked revision.

        The unmodified frozen checkout is scored by the task's oracle; it must fail, otherwise the task cannot tell a
        fix from the original bug.
        """
        contract = build_fix_single_contract(_tasks()[task_id])

        assert run_fix_single_oracle(FROZEN_REPO, contract) is False

    @pytest.mark.parametrize(
        ("task_id", "old", "new"),
        [
            pytest.param(
                "FS-01",
                "        self.patience = patience\n",
                "        if patience < 1:\n"
                '            raise MisconfigurationException(f"patience must be >= 1, got {patience}")\n'
                "        self.patience = patience\n",
                id="patience-guard-rejects-zero",
            ),
            pytest.param(
                "FS-02",
                "        self.min_delta = min_delta\n",
                "        if min_delta < 0:\n"
                '            raise MisconfigurationException(f"min_delta must be >= 0, got {min_delta}")\n'
                "        self.min_delta = min_delta\n",
                id="min-delta-guard-rejects-negative",
            ),
            pytest.param(
                "FS-03",
                "        trainer.save_checkpoint(filepath, self.save_weights_only)\n",
                "        if trainer.global_step == self._last_global_step_saved:\n"
                '            rank_zero_info("Skipping duplicate checkpoint save")\n'
                "            return\n"
                "        trainer.save_checkpoint(filepath, self.save_weights_only)\n",
                id="duplicate-save-skipped",
            ),
            pytest.param(
                "FS-04",
                "        if self.save_top_k < -1:\n",
                "        if self.save_top_k == 0:\n"
                '            rank_zero_warn("ModelCheckpoint(save_top_k=0) is set: no checkpoints will be saved. '
                'Pass save_top_k=-1 to save all checkpoints.")\n'
                "        if self.save_top_k < -1:\n",
                id="save-top-k-zero-warns",
            ),
        ],
    )
    def test_accepts_minimal_behavioral_fix(self, tmp_path: Path, task_id: str, old: str, new: str) -> None:
        """Every task has positive executable evidence, not just a failing baseline.

        The smallest behavioral fix for the task is spliced into a copy of the frozen source; the oracle must accept it,
        proving the task is solvable and the oracle measures behavior (FS-01 keeps a legal positive patience while
        rejecting zero).
        """
        contract = build_fix_single_contract(_tasks()[task_id])
        source = (FROZEN_REPO / contract.expected_paths[0]).read_text(encoding="utf-8")
        candidate = source.replace(old, new, 1)
        assert candidate != source
        candidate_path = tmp_path / contract.expected_paths[0]
        candidate_path.parent.mkdir(parents=True)
        candidate_path.write_text(candidate, encoding="utf-8")

        assert run_fix_single_oracle(tmp_path, contract) is True


def test_provider_binding_rejects_changed_oracle() -> None:
    """A transport cannot substitute a provider-specific execution oracle.

    The FS-01 provider binding validates as issued; replacing its oracle digest with zeros must be refused as a change
    to the scientific fields.
    """
    contract = build_fix_single_contract(_tasks()["FS-01"])
    binding = dict(contract.provider_binding())
    validate_fix_single_binding(contract, binding)
    binding["oracle_sha256"] = "0" * 64

    with pytest.raises(ValueError, match="scientific fields"):
        validate_fix_single_binding(contract, binding)

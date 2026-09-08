"""Executable-oracle tests for the single-file fix pilot."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import pytest

SUITE_PATH = Path(__file__).resolve().parents[1] / "suites" / "tasks-fix-single.json"
# Same canonical resolution the Fix stage itself uses: the root temp directory (which is
# `/private/tmp` once resolved on the canonical macOS host), overridable for a host whose
# root temp directory differs. The oracles read real frozen sources, so every test that
# needs them is guarded on the repository being materialized rather than assuming a path.
FROZEN_REPO = Path(
    os.environ.get("CODEMAP_PARITY_REPO") or f"{os.sep}tmp{os.sep}codemap-provider-parity-pl-2.6.5"
).resolve()
requires_frozen_repo = pytest.mark.skipif(not FROZEN_REPO.is_dir(), reason="frozen benchmark repository is unavailable")
BENCHMARKS = Path(__file__).resolve().parents[1]
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
@pytest.mark.parametrize("task_id", ("FS-01", "FS-02", "FS-03", "FS-04"))
def test_oracle_rejects_the_frozen_unfixed_source(task_id: str) -> None:
    """Each selected task has a real failing baseline at the locked revision."""
    contract = build_fix_single_contract(_tasks()[task_id])

    assert run_fix_single_oracle(FROZEN_REPO, contract) is False


@requires_frozen_repo
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


@requires_frozen_repo
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

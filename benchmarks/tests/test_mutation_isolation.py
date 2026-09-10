"""Tests for provider-neutral mutable-cell lifecycle behavior."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

try:
    from _bench_common.mutation_isolation import (
        IsolatedMutationCell,
        MutationCleanupError,
        relocate_frozen_index_for_worktree,
        verify_index_relocation,
    )
except ModuleNotFoundError:
    from benchmarks._bench_common.mutation_isolation import (
        IsolatedMutationCell,
        MutationCleanupError,
        relocate_frozen_index_for_worktree,
        verify_index_relocation,
    )


@pytest.mark.parametrize("failure", [None, RuntimeError("model failure"), TimeoutError("cell timeout")])
def test_isolated_cell_restores_baseline_after_success_error_and_timeout(
    tmp_path: Path, failure: Exception | None
) -> None:
    """Success, action errors, and timeouts all restore the disposable baseline."""
    created: list[Path] = []
    restored: list[Path] = []

    def _create() -> Path:
        """Create and record a uniquely numbered disposable cell directory."""
        worktree = tmp_path / f"cell-{len(created)}"
        worktree.mkdir()
        created.append(worktree)
        return worktree

    def _restore(worktree: Path) -> None:
        """Record the worktree handed to the restoration callback."""
        restored.append(worktree)

    cell = IsolatedMutationCell(_create, _restore)
    if failure is None:
        assert cell.run(lambda worktree: worktree.name) == "cell-0"
    else:
        with pytest.raises(type(failure), match=str(failure)):
            cell.run(lambda _worktree: (_ for _ in ()).throw(failure))

    assert restored == created
    assert cell.last_evidence.restored is True


@pytest.mark.parametrize(
    ("error_type", "message"),
    [
        pytest.param(AssertionError, "target test failed", id="target-test-failure"),
        pytest.param(ValueError, "scorer failed", id="scorer-failure"),
    ],
)
def test_retry_and_test_or_scorer_failure_each_receive_a_fresh_baseline(
    tmp_path: Path, error_type: type[Exception], message: str
) -> None:
    """Retries and downstream failures cannot reuse a mutated previous cell."""
    created: list[Path] = []
    restored: list[Path] = []

    def _create() -> Path:
        """Create and record a uniquely numbered disposable cell directory."""
        worktree = tmp_path / f"cell-{len(created)}"
        worktree.mkdir()
        created.append(worktree)
        return worktree

    cell = IsolatedMutationCell(_create, restored.append)
    with pytest.raises(error_type, match=message):
        cell.run(lambda _worktree: (_ for _ in ()).throw(error_type(message)))
    with pytest.raises(error_type, match=message):
        cell.run(lambda _worktree: (_ for _ in ()).throw(error_type(message)))

    assert created == restored
    assert created[0] != created[1]


def test_cleanup_failure_preserves_action_failure_and_fails_closed(tmp_path: Path) -> None:
    """Cleanup failure must override a tempting action result with evidence."""
    worktree = tmp_path / "cell"
    worktree.mkdir()
    cell = IsolatedMutationCell(lambda: worktree, lambda _worktree: (_ for _ in ()).throw(OSError("locked")))

    with pytest.raises(MutationCleanupError, match="model failure"):
        cell.run(lambda _worktree: (_ for _ in ()).throw(RuntimeError("model failure")))

    assert cell.last_evidence.action_error == "RuntimeError: model failure"
    assert cell.last_evidence.cleanup_error == "OSError: locked"
    assert cell.last_evidence.restored is False


def test_relocating_an_already_relocated_index_keeps_naming_the_locked_origin(tmp_path: Path) -> None:
    """A second relocation records the locked index it descends from, not the copy it was cut from.

    Scenario: an isolated run already holds the locked graph with its scan root moved, and every executable stage then
    cuts its own worktree from that copy. Admission compares the recorded origin against the manifest lock, so a stage
    that recorded the intermediate copy would be refused even though its graph content never changed.
    """
    locked_root = tmp_path / "canonical"
    run_root = tmp_path / "run"
    stage_root = tmp_path / "stage"
    locked_payload = {
        "scan_root": str(locked_root.resolve()),
        "scan_version": 13,
        "modules": {"pkg.module": {"symbols": ["target"]}},
    }
    locked_bytes = json.dumps(locked_payload, indent=2, sort_keys=True).encode("utf-8")
    locked_sha256 = hashlib.sha256(locked_bytes).hexdigest()
    run_bytes, run_relocation = relocate_frozen_index_for_worktree(
        locked_bytes,
        source_root=locked_root,
        worktree_root=run_root,
    )

    stage_bytes, stage_relocation = relocate_frozen_index_for_worktree(
        run_bytes,
        source_root=run_root,
        worktree_root=stage_root,
        frozen_index_sha256=run_relocation["frozen_index_sha256"],
    )

    assert stage_relocation["frozen_index_sha256"] == locked_sha256
    assert stage_relocation["derived_index_sha256"] == hashlib.sha256(stage_bytes).hexdigest()
    assert stage_relocation["non_root_content_sha256"] == run_relocation["non_root_content_sha256"]
    verify_index_relocation(
        stage_relocation,
        metadata=json.loads(stage_bytes),
        index_sha256=hashlib.sha256(stage_bytes).hexdigest(),
        repo_path=stage_root,
        frozen_index_sha256=locked_sha256,
    )

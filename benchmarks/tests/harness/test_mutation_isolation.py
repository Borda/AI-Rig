"""Tests for provider-neutral mutable-cell lifecycle behavior."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
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


def _raising(error: Exception) -> Callable[[Path], None]:
    """Return a cell action that raises *error* instead of producing a result."""

    def _action(_worktree: Path) -> None:
        raise error

    return _action


@pytest.fixture
def numbered_cells(tmp_path: Path) -> tuple[Callable[[], Path], list[Path]]:
    """Provide a cell factory creating `cell-0`, `cell-1`, ...

    plus the list of every directory it created.
    """
    created: list[Path] = []

    def _create() -> Path:
        """Create and record a uniquely numbered disposable cell directory."""
        worktree = tmp_path / f"cell-{len(created)}"
        worktree.mkdir()
        created.append(worktree)
        return worktree

    return _create, created


class TestIsolatedMutationCell:
    """Lifecycle of one disposable mutation cell: create, run, restore, and report cleanup failure."""

    def test_restores_baseline_after_success(self, numbered_cells: tuple[Callable[[], Path], list[Path]]) -> None:
        """A successful action returns its result and still restores the disposable baseline.

        Restoration is not reserved for failures: the cell is always handed back to the restore callback, so the next
        run starts from the frozen baseline.
        """
        create, created = numbered_cells
        restored: list[Path] = []
        cell = IsolatedMutationCell(create, restored.append)

        result = cell.run(lambda worktree: worktree.name)

        assert result == "cell-0"
        assert restored == created
        assert cell.last_evidence.restored is True

    @pytest.mark.parametrize(
        "failure",
        [
            pytest.param(RuntimeError("model failure"), id="action-error"),
            pytest.param(TimeoutError("cell timeout"), id="timeout"),
        ],
    )
    def test_restores_baseline_after_action_failure(
        self, numbered_cells: tuple[Callable[[], Path], list[Path]], failure: Exception
    ) -> None:
        """Action errors and timeouts propagate unchanged and still restore the disposable baseline.

        A model failure or a cell timeout leaves the worktree mutated; the cell must restore it before the exception
        reaches the caller, or the next attempt would inherit the half-applied edits.
        """
        create, created = numbered_cells
        restored: list[Path] = []
        cell = IsolatedMutationCell(create, restored.append)

        with pytest.raises(type(failure), match=str(failure)):
            cell.run(_raising(failure))

        assert restored == created
        assert cell.last_evidence.restored is True

    @pytest.mark.parametrize(
        ("error_type", "message"),
        [
            pytest.param(AssertionError, "target test failed", id="target-test-failure"),
            pytest.param(ValueError, "scorer failed", id="scorer-failure"),
        ],
    )
    def test_each_retry_receives_a_fresh_baseline(
        self, numbered_cells: tuple[Callable[[], Path], list[Path]], error_type: type[Exception], message: str
    ) -> None:
        """Retries and downstream test or scorer failures cannot reuse a mutated previous cell.

        Two consecutive failing runs through one cell object must each create and restore their own directory, so a
        retry never starts inside the previous attempt's mutated worktree.
        """
        create, created = numbered_cells
        restored: list[Path] = []
        cell = IsolatedMutationCell(create, restored.append)

        with pytest.raises(error_type, match=message):
            cell.run(_raising(error_type(message)))
        with pytest.raises(error_type, match=message):
            cell.run(_raising(error_type(message)))

        assert created == restored
        assert created[0] != created[1]

    def test_cleanup_failure_preserves_action_failure_and_fails_closed(self, tmp_path: Path) -> None:
        """Cleanup failure must override a tempting action result with evidence.

        When the action fails and the restore callback then fails too, the run is reported as a cleanup error that still
        names the original action failure, and the evidence records the cell as not restored.
        """
        worktree = tmp_path / "cell"
        worktree.mkdir()
        cell = IsolatedMutationCell(lambda: worktree, _raising(OSError("locked")))

        with pytest.raises(MutationCleanupError, match="model failure"):
            cell.run(_raising(RuntimeError("model failure")))

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
        locked_bytes, source_root=locked_root, worktree_root=run_root
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

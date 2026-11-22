"""Acceptance checks for resuming a ``code-remediate`` cleanup that a crash cut short inside a worktree removal.

Each test drives the real lifecycle module (``shared/parallel_worktrees.py``) against throwaway Git repositories built
from committed templates. ``git worktree remove`` checks that the worktree is clean, deletes its directory entry by
entry and drops Git's registration last, so a crash in the middle leaves the pending worktree partly deleted: Git then
refuses a non-force removal, because its ``.git`` file is gone or because the deleted files count as changes. A resumed
cleanup must finish that removal under Git's own non-force guarantees and refuse whenever it would delete anything Git
does not show as an unmodified tracked or ignored file. Resume after a crash that left the directory fully present or
fully gone is checked in ``test_parallel_worktrees.py``.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
from _artifact_files import read_json
from _parallel_worktrees_support import (
    Pilot,
    _assert_completed_lifecycle,
    _cleanup_targets,
    _crash_inside_worktree_removal,
    _git,
    _read,
    _registered_worktrees,
    _SimulatedCrash,
    _write,
)


class _PartialRemoval:
    """A cleanup a crash cut short inside the removal of one worktree, which Git left partly deleted."""

    def __init__(self, pilot: Pilot, workspace: Path, victim: Path) -> None:
        """Keep the run, its cleanup targets and the partly removed worktree."""
        self.pilot = pilot
        self.victim = victim
        self.relatives = [
            target.relative_to(workspace).as_posix()
            for target in _cleanup_targets(workspace, pilot.state_path, pilot.state)
        ]


def _crash_partway_through_removal(
    pilot: Pilot,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    target_index: int,
    deleted: tuple[str, ...],
    before: Callable[[Path], None] | None = None,
) -> _PartialRemoval:
    """Apply the run, then crash its cleanup inside ``git worktree remove`` of one target after ``deleted`` are gone.

    ``before`` runs on the target ahead of the cleanup, while the worktree is still whole, for a file Git's clean check
    accepts (an ignored one).
    """
    lifecycle, state_path, state, _ = pilot
    lifecycle.apply_code_remediate_source(state_path=state_path)
    victim = _cleanup_targets(workspace, state_path, state)[target_index]
    if before is not None:
        before(victim)
    with monkeypatch.context() as crash:
        _crash_inside_worktree_removal(lifecycle, crash, victim, deleted)
        with pytest.raises(_SimulatedCrash):
            lifecycle.cleanup_code_remediate_pilot(state_path=state_path)
    assert victim.is_dir()
    assert victim in _registered_worktrees(pilot.repository)
    assert read_json(state_path)["cleanup"]["removing"] == victim.relative_to(workspace).as_posix()
    return _PartialRemoval(pilot, workspace, victim)


def _leave_ignored_cache(victim: Path) -> None:
    """Exclude ``cache/`` for the repository and leave an ignored build file in the still-whole worktree."""
    common = Path(_git(victim, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())
    with (common / "info" / "exclude").open("a", encoding="utf-8", newline="\n") as stream:
        stream.write("\ncache/\n")
    (victim / "cache").mkdir()
    _write(victim / "cache" / "build.txt", "generated\n")


def _add_user_file(partial: _PartialRemoval) -> Path:
    """Write a file Git does not track into the partly removed worktree, as a user would."""
    _write(partial.victim / "notes.txt", "user notes\n")
    return partial.victim / "notes.txt"


def _edit_remaining_tracked_file(partial: _PartialRemoval) -> Path:
    """Change a tracked file Git has not deleted yet."""
    _write(partial.victim / "bucket-b.txt", "user edit after the crash\n")
    return partial.victim / "bucket-b.txt"


def _orphan_user_worktree(partial: _PartialRemoval) -> Path:
    """Add a user's own worktree of the same repository and delete its directory, leaving it prunable."""
    other = partial.victim.parent.parent / "user-worktree"
    _git(partial.pilot.repository, "worktree", "add", "-q", "--detach", str(other), "HEAD")
    shutil.rmtree(other)
    return partial.victim / "bucket-b.txt"


def _lock_target(partial: _PartialRemoval) -> Path:
    """Lock the partly removed worktree, which Git never lets a non-force removal or a prune touch."""
    _git(partial.pilot.repository, "worktree", "lock", str(partial.victim))
    return partial.victim / "bucket-b.txt"


def _commit_populated_submodule(partial: _PartialRemoval) -> Path:
    """Commit a nested repository into the partly removed worktree, so Git tracks it as a populated submodule.

    The nested repository's own history lives only in its directory; ``git worktree remove`` refuses any worktree with a
    populated submodule even when every file is clean.
    """
    nested = partial.victim / "nested"
    nested.mkdir()
    _git(nested, "init", "-q")
    _write(nested / "kept.txt", "nested history\n")
    _git(nested, "add", "kept.txt")
    _git(nested, "-c", "user.name=User", "-c", "user.email=user@example.invalid", "commit", "-q", "-m", "Nested")
    _git(partial.victim, "add", "nested")
    _git(partial.victim, "-c", "user.name=User", "-c", "user.email=user@example.invalid", "commit", "-q", "-m", "Sub")
    return nested / "kept.txt"


@pytest.mark.installed_plugin
@pytest.mark.integration
class TestCleanupResumeAfterPartialRemoval:
    """A resumed cleanup finishes a removal a crash cut short part-way, and keeps anything Git does not show clean."""

    @pytest.mark.parametrize(
        ("target_index", "deleted", "before"),
        [
            pytest.param(0, (".git", "bucket-a.txt"), None, id="child-gitfile-and-tracked-file-gone"),
            pytest.param(2, ("bucket-a.txt",), None, id="integration-tracked-file-gone-gitfile-kept"),
            pytest.param(1, (".git",), _leave_ignored_cache, id="child-gitfile-gone-ignored-file-left"),
        ],
    )
    def test_finishes_a_removal_git_left_partly_done(
        self,
        integrated_remediation: Pilot,
        tmp_path: Path,
        validator: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        target_index: int,
        deleted: tuple[str, ...],
        before: Callable[[Path], None] | None,
    ) -> None:
        """Delete the rest of the pending worktree, drop its registration and record it removed, then finish cleanup.

        Without this, the resumed non-force removal fails on the missing ``.git`` file or the deleted files, saves the
        cleanup ``failed`` and drops the pending mark, so every later resume ends the same way.
        """
        partial = _crash_partway_through_removal(
            integrated_remediation, tmp_path, monkeypatch, target_index=target_index, deleted=deleted, before=before
        )
        lifecycle, state_path, _, repository = integrated_remediation

        cleanup = lifecycle.cleanup_code_remediate_pilot(state_path=state_path)

        assert cleanup == {"cleanup_status": "removed", "force": False}
        assert read_json(state_path)["cleanup"] == {
            "status": "removed",
            "force": False,
            "worktrees": partial.relatives,
        }
        assert not partial.victim.exists()
        assert partial.victim not in _registered_worktrees(repository)
        _assert_completed_lifecycle(validator, state_path)

    def test_retries_a_partial_removal_whose_own_delete_failed(
        self, integrated_remediation: Pilot, tmp_path: Path, validator: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Keep the pending mark when deleting the rest fails part-way, so the next resume finishes the same removal."""
        partial = _crash_partway_through_removal(
            integrated_remediation, tmp_path, monkeypatch, target_index=0, deleted=("bucket-a.txt",)
        )
        lifecycle, state_path, _, repository = integrated_remediation

        def _delete_gitfile_then_fail(path: Path, *arguments: object, **options: object) -> None:
            """Delete only the ``.git`` file, as an interrupted recursive delete can, then fail."""
            assert Path(path) == partial.victim
            (partial.victim / ".git").unlink()
            raise PermissionError("delete refused")

        with monkeypatch.context() as failing:
            failing.setattr(lifecycle.shutil, "rmtree", _delete_gitfile_then_fail)
            with pytest.raises(lifecycle.PilotError, match=r"^cleanup-partial-worktree-removal-failed$"):
                lifecycle.cleanup_code_remediate_pilot(state_path=state_path)

        assert read_json(state_path)["cleanup"]["removing"] == partial.relatives[0]
        assert lifecycle.cleanup_code_remediate_pilot(state_path=state_path)["cleanup_status"] == "removed"
        assert not partial.victim.exists()
        assert partial.victim not in _registered_worktrees(repository)
        _assert_completed_lifecycle(validator, state_path)

    @pytest.mark.parametrize(
        ("deleted", "tamper", "error"),
        [
            pytest.param((".git",), _add_user_file, "cleanup-partial-worktree-unclean", id="untracked-user-file"),
            pytest.param(
                (".git", "bucket-a.txt"),
                _edit_remaining_tracked_file,
                "cleanup-partial-worktree-unclean",
                id="modified-tracked-file",
            ),
            pytest.param(
                (".git",), _orphan_user_worktree, "cleanup-unrecorded-worktree-removal", id="another-worktree-prunable"
            ),
            pytest.param(("bucket-a.txt",), _lock_target, "cleanup-unrecorded-worktree-removal", id="locked-target"),
            pytest.param(
                ("bucket-a.txt",),
                _commit_populated_submodule,
                "cleanup-partial-worktree-unclean",
                id="populated-submodule",
            ),
        ],
    )
    def test_keeps_a_partial_worktree_git_does_not_show_clean(
        self,
        integrated_remediation: Pilot,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        deleted: tuple[str, ...],
        tamper: Callable[[_PartialRemoval], Path],
        error: str,
    ) -> None:
        """Refuse before deleting anything, keeping the directory, the user's file, the registration and the state.

        A partial directory holding content Git does not show as an unmodified tracked or ignored file, a locked target,
        or another prunable worktree (which the following prune would also drop) is left for the operator; the pending
        mark stays, so a resume after the operator resolves it finishes the removal.
        """
        partial = _crash_partway_through_removal(
            integrated_remediation, tmp_path, monkeypatch, target_index=0, deleted=deleted
        )
        lifecycle, state_path, _, repository = integrated_remediation
        kept = tamper(partial)
        kept_text = _read(kept)
        state_bytes = state_path.read_bytes()

        with pytest.raises(lifecycle.PilotError, match=f"^{error}$"):
            lifecycle.cleanup_code_remediate_pilot(state_path=state_path)

        assert _read(kept) == kept_text
        assert partial.victim in _registered_worktrees(repository)
        assert state_path.read_bytes() == state_bytes
        assert all((tmp_path / relative).is_dir() for relative in partial.relatives)

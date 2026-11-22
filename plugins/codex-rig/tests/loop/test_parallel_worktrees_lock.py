"""Acceptance checks for the parallel worktree apply lock, CLI lifecycle and generated-fixture pilot route.

Each test drives the real lifecycle module (``shared/parallel_worktrees.py``) against throwaway Git repositories built
from committed templates, so Git behaviour is part of the contract; only a single failing ``_git`` step or an OS error
is ever replaced. This module covers the exclusive per-run apply lock and lifecycle state durability, the ``code-
remediate`` CLI lifecycle and its receipts, the generated-fixture ``develop`` pilot (``prepare_write_pilot``) from
preparation through handovers, join, collection, integration and cleanup, grouped ``code-remediate`` delivery, and the
lifecycle's Git helpers. Plan admission, source apply and crash recovery of the ``code-remediate`` plan are checked in
``test_parallel_worktrees.py``.
"""

from __future__ import annotations

import errno
import functools
import hashlib
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest import mock

import pytest
from _artifact_files import read_json
from _parallel_worktrees_support import (
    _DELIVERY_MODES,
    CHILD_EDITS,
    LIFECYCLE_PATH,
    PLUGIN_ROOT,
    GatedGroupedRemediation,
    Pilot,
    RepositoryTemplates,
    _apply_statuses,
    _approved_code_remediate_plan,
    _approved_plan,
    _assert_completed_lifecycle,
    _assert_completed_lifecycle_after_cleanup,
    _cleanup_targets,
    _collect_all,
    _crash_after_patch_output,
    _crash_around_git,
    _create_handovers,
    _delivery_route,
    _DeliveryRoute,
    _draft_commit,
    _edit_owned,
    _fixture_repository,
    _git,
    _hook_after_source_apply,
    _is_source_apply,
    _is_source_merge,
    _is_worktree_command,
    _lifecycle_reference,
    _load_lifecycle,
    _node_worktrees,
    _overlapping_code_remediate_plan,
    _prepare_remediation,
    _raise,
    _read,
    _rewrite_plan,
    _run_grouped_gates,
    _sha256,
    _SimulatedCrash,
    _skip_no_symlinks,
    _use_grouped_plan,
    _write,
    _write_json,
)


def _child_handover(workspace: Path, node: dict[str, object]) -> dict[str, object]:
    """Build the five-field child result from the exact detached worktree diff."""
    worktree = workspace / str(node["worktree_path"])
    owned_paths = [str(path) for path in node["owned_paths"]]
    patch = subprocess.run(
        ["git", "-C", str(worktree), "diff", "--binary", "--full-index", "HEAD", "--", *owned_paths],
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
    ).stdout
    return {
        "node_id": node["node_id"],
        "status": "completed",
        "summary": f"Completed {node['node_id']}.",
        "changed_paths": owned_paths,
        "patch_sha256": hashlib.sha256(patch).hexdigest(),
    }


def _prepare_generated(
    lifecycle: ModuleType, workspace: Path, plan_path: Path, approval_path: Path
) -> dict[str, object]:
    """Prepare a generated-fixture pilot with the conventional state file."""
    return lifecycle.prepare_write_pilot(
        plan_path=plan_path,
        approval_path=approval_path,
        workspace_root=workspace,
        state_path=workspace / ".reports" / "codex" / "develop" / "state.json",
    )


def _join(lifecycle: ModuleType, state_path: Path, state: dict[str, object], workspace: Path) -> dict[str, object]:
    """Join both valid child handovers for the prepared fixture."""
    return lifecycle.join_child_handovers(
        state_path=state_path, handovers=[_child_handover(workspace, node) for node in state["nodes"]]
    )


def _write_other_children(workspace: Path, state: dict[str, object], excluded_node_id: str) -> None:
    """Give every non-target child one valid owned-path edit."""
    for node in state["nodes"]:
        if node["node_id"] == excluded_node_id:
            continue
        _write(workspace / str(node["worktree_path"]) / str(node["owned_paths"][0]), "other-child\n")


def _record_lifecycle_git(
    lifecycle: ModuleType, monkeypatch: pytest.MonkeyPatch, *, refuse_batches: bool = False
) -> list[tuple[str, ...]]:
    """Record every lifecycle Git invocation, optionally failing any batched ``--git-path`` query."""
    real_git = lifecycle._git
    calls: list[tuple[str, ...]] = []

    def _record(target: Path, *arguments: str) -> str:
        """Run the real Git helper after noting its arguments."""
        calls.append(arguments)
        if refuse_batches and arguments.count("--git-path") > 1:
            raise lifecycle.PilotError("git-command-failed:rev-parse")
        return real_git(target, *arguments)

    monkeypatch.setattr(lifecycle, "_git", _record)
    return calls


def _join_arguments(state_path: Path, handover_paths: Sequence[Path]) -> list[str]:
    """Build the ``join`` command line for the given handover files."""
    handovers = [argument for path in handover_paths for argument in ("--handover", str(path))]
    return ["join", "--state", str(state_path), *handovers]


@pytest.fixture
def edited_pilot(pilot: Pilot, tmp_path: Path) -> Pilot:
    """Have each child edit the path it owns (``CHILD_EDITS``) in the prepared pilot."""
    _edit_owned(tmp_path, pilot.state, CHILD_EDITS)
    return pilot


@pytest.fixture
def joined_pilot(edited_pilot: Pilot, tmp_path: Path) -> Pilot:
    """Join both verified child handovers of the edited pilot."""
    _join(edited_pilot.lifecycle, edited_pilot.state_path, edited_pilot.state, tmp_path)
    return edited_pilot


@pytest.fixture
def collected_pilot(joined_pilot: Pilot) -> Pilot:
    """Collect both hash-bound patches of the joined pilot."""
    _collect_all(joined_pilot.lifecycle, joined_pilot.state_path, joined_pilot.state)
    return joined_pilot


#: Error a swapped gate log raises when delivery re-binds the gate evidence.
_GATE_LOG_DRIFT = r"^grouped-gate-artifacts-invalid:gate-evidence-digest-mismatch$"
#: Error a swapped worker output raises when delivery re-checks the first worker's advisory receipt.
_WORKER_OUTPUT_DRIFT = r"^child-advisory-receipt-invalid:WRITE-A$"


def _swap_after_delivery_intent(lifecycle: ModuleType, monkeypatch: pytest.MonkeyPatch, swapped: Path) -> None:
    """Replace one referenced log right after the lifecycle records delivery as ``source-applying``."""
    real_persist = lifecycle._persist

    def _swap_after_intent(path: Path, payload: dict[str, object]) -> None:
        """Persist, then replace the log once delivery has been recorded as in progress."""
        real_persist(path, payload)
        if payload["status"] == "source-applying":
            swapped.write_bytes(b"swapped after validation\n")

    monkeypatch.setattr(lifecycle, "_persist", _swap_after_intent)


def _write_fabricated_receipt(directory: Path, worktree: Path, node: dict[str, object]) -> Path:
    """Write a passing-looking worker receipt for the frozen command without running anything."""
    output = directory / f"{node['node_id']}-fabricated.stdout.txt"
    _write(output, "this command never ran\n")
    receipt = directory / f"{node['node_id']}-fabricated.json"
    _write_json(
        receipt,
        {
            "node_id": node["node_id"],
            "commit_oid": _git(worktree, "rev-parse", "HEAD").stdout.strip(),
            "checks": [
                {
                    "command": "git diff --check HEAD^ HEAD",
                    "exit_code": 0,
                    "stdout": output.name,
                    "stdout_sha256": _sha256(output),
                }
            ],
        },
    )
    return receipt


def _contained_output_parent(evidence: Path) -> Path:
    """Create a real output directory inside the evidence root."""
    parent = evidence / "handover-output"
    parent.mkdir()
    return parent


def _symlinked_output_parent(evidence: Path) -> Path:
    """Create an output directory name that is a symlink to another directory inside the evidence root."""
    target = evidence / "contained-target"
    target.mkdir()
    parent = evidence / "handover-output"
    parent.symlink_to(target, target_is_directory=True)
    return parent


# --- code-remediate: exclusive per-run apply lock -------------------------------------------------------------------


def _apply_lock_path(state_path: Path) -> Path:
    """Return the run's exclusive apply lock, which exists next to the lifecycle state only while an apply holds it."""
    return state_path.with_name(f".{state_path.name}.apply-lock")


def _reclaim_lock_path(state_path: Path) -> Path:
    """Return the file that serializes taking over an apply lock whose holder died."""
    return state_path.with_name(f".{state_path.name}.apply-lock.reclaim")


def _dead_pid() -> int:
    """Return the process ID of a child that has already exited and been reaped."""
    child = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=True,
    )
    return int(child.stdout)


def _sleeping_child() -> subprocess.Popen[bytes]:
    """Start a child that stays alive until the test stops it, to stand in for a live lock holder."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], stdin=subprocess.DEVNULL)


def _stop(child: subprocess.Popen[bytes]) -> None:
    """Stop and reap a child started with ``_sleeping_child``."""
    child.kill()
    child.wait()


def _plant_lock(lock: Path, *, pid: int, host: str) -> bytes:
    """Write a lock record as a holder with ``pid`` on ``host`` would have left it, and return its exact bytes."""
    record = {"host": host, "pid": pid, "started_at": "2026-01-01T00:00:00+00:00", "token": f"planted-{pid}"}
    lock.write_bytes((json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
    return lock.read_bytes()


def _run_cli_before_git(
    lifecycle: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    matches: Callable[[Path, tuple[str, ...]], bool],
    arguments: Sequence[str],
) -> list[subprocess.CompletedProcess[str]]:
    """Run the lifecycle CLI once in another process right before this process runs the first matching Git command.

    Returns:
        A list that receives the other process's completed run.
    """
    real_git = lifecycle._git
    contenders: list[subprocess.CompletedProcess[str]] = []

    def _git_after_contender(target: Path, *git_arguments: str) -> str:
        """Let the other process run its whole command once, then run this process's Git command."""
        if not contenders and matches(target, git_arguments):
            contenders.append(
                subprocess.run(
                    [sys.executable, str(LIFECYCLE_PATH), *arguments],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    check=False,
                )
            )
        return real_git(target, *git_arguments)

    monkeypatch.setattr(lifecycle, "_git", _git_after_contender)
    return contenders


def _contend_from_another_process(
    route: _DeliveryRoute, monkeypatch: pytest.MonkeyPatch
) -> list[subprocess.CompletedProcess[str]]:
    """Run a second apply of the same run through the CLI in another process right before this apply writes the source.

    Returns:
        A list that receives the other process's completed run.
    """
    lifecycle, state_path, _, repository = route.pilot
    matches = _is_source_merge(repository) if route.gates is not None else _is_source_apply(repository)
    gates = [] if route.gates is None else ["--gates", str(route.gates)]
    return _run_cli_before_git(lifecycle, monkeypatch, matches, ["apply-source", "--state", str(state_path), *gates])


def _finish_other_cleanup_after_read(
    lifecycle: ModuleType, monkeypatch: pytest.MonkeyPatch, state_path: Path
) -> list[bytes]:
    """Run another cleanup of the run to completion right after this cleanup read the state, before it takes the lock.

    Returns:
        A list that receives the state file bytes the other cleanup left behind.
    """
    real_read_state = lifecycle._read_state
    recorded: list[bytes] = []

    def _read_then_other_cleanup(path: Path) -> tuple[dict[str, Any], Path]:
        """Return this cleanup's read, after the other cleanup removed every worktree and recorded it."""
        loaded = real_read_state(path)
        if not recorded:
            recorded.append(b"")
            lifecycle.cleanup_code_remediate_pilot(state_path=state_path)
            recorded[0] = state_path.read_bytes()
        return loaded

    monkeypatch.setattr(lifecycle, "_read_state", _read_then_other_cleanup)
    return recorded


def _deliver_bypassing_the_lock_before_source_apply(pilot: Pilot, monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    """Run another apply of the run to completion right before this one's source ``git apply``, as if no lock existed.

    Models a writer that never saw this apply's lock, for example one removed by hand: it delivers and records the run
    while this apply holds ``source-applying``, so this apply's own ``git apply`` then fails on the delivered paths.

    Returns:
        A list that receives the state file bytes the other apply left behind.
    """
    lifecycle, state_path, _, repository = pilot
    real_git = lifecycle._git
    lock = _apply_lock_path(state_path)
    fired: list[bool] = []
    recorded: list[bytes] = []

    def _git_after_bypass(target: Path, *arguments: str) -> str:
        """Hide this apply's lock, let the other apply finish, then put the lock back and run this apply's write."""
        if not fired and _is_source_apply(repository)(target, arguments):
            fired.append(True)
            held = lock.read_bytes() if lock.exists() else None
            lock.unlink(missing_ok=True)
            lifecycle.apply_code_remediate_source(state_path=state_path)
            recorded.append(state_path.read_bytes())
            if held is not None:
                lock.write_bytes(held)
        return real_git(target, *arguments)

    monkeypatch.setattr(lifecycle, "_git", _git_after_bypass)
    return recorded


_REFUSED_HOLDERS = [
    pytest.param("live-process", id="live-holder-on-this-host"),
    pytest.param("other-host", id="exited-holder-on-another-host"),
    pytest.param("unreadable", id="unreadable-lock"),
]


class _FakeKernel32:
    """Answer the three Win32 calls the Windows liveness probe makes, recording every handle it closes."""

    def __init__(self, handle: int | None, exit_code: int, *, exit_code_read: bool = True) -> None:
        """Fix the handle ``OpenProcess`` returns and the exit code ``GetExitCodeProcess`` reports."""
        self.handle = handle
        self.exit_code = exit_code
        self.exit_code_read = exit_code_read
        self.closed: list[int] = []

    def OpenProcess(self, access: int, inherit: bool, pid: int) -> int | None:
        """Return the fixed handle, or ``None`` as Win32 does when it cannot open the process."""
        return self.handle

    def GetExitCodeProcess(self, handle: int, code: Any) -> int:
        """Write the fixed exit code through the ``byref`` pointer and report whether the read succeeded."""
        code._obj.value = self.exit_code
        return int(self.exit_code_read)

    def CloseHandle(self, handle: int) -> int:
        """Record the closed handle."""
        self.closed.append(handle)
        return 1


@pytest.mark.installed_plugin
@pytest.mark.integration
class TestCodeRemediateApplyLock:
    """One apply of a run holds an exclusive lock across its whole source write, rollback and record."""

    @pytest.mark.parametrize("mode", _DELIVERY_MODES)
    def test_refuses_a_second_apply_while_the_first_writes_the_source(
        self, request: pytest.FixtureRequest, validator: ModuleType, monkeypatch: pytest.MonkeyPatch, mode: str
    ) -> None:
        """Refuse another process's apply of the same run while this one is about to write, and finish this one.

        Without the lock the other apply resumes the ``source-applying`` record, delivers and records the run, and this
        apply's own write then fails on the moved source and stops on its save, after a patch rollback may already have
        put the delivered paths back to the baseline.
        """
        route = _delivery_route(request, mode)
        lifecycle, state_path, _, repository = route.pilot
        contenders = _contend_from_another_process(route, monkeypatch)

        application = lifecycle.apply_code_remediate_source(state_path=state_path, gates_path=route.gates)
        monkeypatch.undo()

        assert [contender.returncode for contender in contenders] == [2]
        assert "lifecycle-apply-locked" in contenders[0].stderr
        assert application["status"] == "applied"
        assert _apply_statuses(state_path) == ("source-applied", "applied")
        assert _git(repository, "rev-parse", "HEAD").stdout.strip() == route.delivered_head
        assert _read(repository / "bucket-a.txt") == "child-a\n"
        assert not _apply_lock_path(state_path).exists()
        _assert_completed_lifecycle_after_cleanup(lifecycle, validator, state_path)

    def test_rollback_keeps_paths_another_apply_delivered_and_recorded(
        self, integrated_remediation: Pilot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stop before restoring anything when the state file no longer holds this apply's record.

        Another apply that never saw this one's lock delivers and records the run, so this apply's ``git apply`` fails.
        Its rollback must reread the state right before touching the working tree; restoring first would put the
        delivered paths back to the baseline under a record that says they are applied.
        """
        lifecycle, state_path, _, repository = integrated_remediation
        recorded = _deliver_bypassing_the_lock_before_source_apply(integrated_remediation, monkeypatch)

        with pytest.raises(lifecycle.PilotError, match=r"^lifecycle-state-concurrent-update$"):
            lifecycle.apply_code_remediate_source(state_path=state_path)
        monkeypatch.undo()

        assert state_path.read_bytes() == recorded[0]
        assert _apply_statuses(state_path) == ("source-applied", "applied")
        assert _read(repository / "bucket-a.txt") == "child-a\n"
        assert _read(repository / "bucket-b.txt") == "child-b\n"
        assert not _apply_lock_path(state_path).exists()
        assert lifecycle.cleanup_code_remediate_pilot(state_path=state_path)["cleanup_status"] == "removed"

    @pytest.mark.parametrize("mode", _DELIVERY_MODES)
    def test_reclaims_a_lock_its_exited_holder_left_and_resumes_the_delivery(
        self, request: pytest.FixtureRequest, validator: ModuleType, monkeypatch: pytest.MonkeyPatch, mode: str
    ) -> None:
        """Take over the lock of a holder that died mid-delivery on this host, then finish the delivery from Git."""
        route = _delivery_route(request, mode)
        lifecycle, state_path, _, repository = route.pilot
        matches = _is_source_merge(repository) if route.gates is not None else _is_source_apply(repository)
        _crash_around_git(lifecycle, monkeypatch, matches, after_effect=True)
        with pytest.raises(_SimulatedCrash):
            lifecycle.apply_code_remediate_source(state_path=state_path, gates_path=route.gates)
        monkeypatch.undo()
        _plant_lock(_apply_lock_path(state_path), pid=_dead_pid(), host=socket.gethostname())

        application = lifecycle.apply_code_remediate_source(state_path=state_path, gates_path=route.gates)

        assert application["status"] == "applied"
        assert _apply_statuses(state_path) == ("source-applied", "applied")
        assert not _apply_lock_path(state_path).exists()
        assert not _reclaim_lock_path(state_path).exists()
        _assert_completed_lifecycle_after_cleanup(lifecycle, validator, state_path)

    @pytest.mark.parametrize("holder", _REFUSED_HOLDERS)
    def test_refuses_a_lock_it_cannot_prove_abandoned(self, integrated_remediation: Pilot, holder: str) -> None:
        """Leave the run, the source and the lock untouched while a live, foreign or unreadable holder owns the lock.

        Only a holder on this host whose process no longer exists is provably gone; any other lock may belong to a
        running apply, so taking it over could let two applies write the same source.
        """
        lifecycle, state_path, _, repository = integrated_remediation
        lock = _apply_lock_path(state_path)
        sleeper = _sleeping_child()
        expected = "lifecycle-apply-locked"
        try:
            if holder == "live-process":
                held = _plant_lock(lock, pid=sleeper.pid, host=socket.gethostname())
            elif holder == "other-host":
                pid, host = _dead_pid(), f"other-{socket.gethostname()}"
                held = _plant_lock(lock, pid=pid, host=host)
                holder_record = {"host": host, "pid": pid, "started_at": "2026-01-01T00:00:00+00:00"}
                expected = f"lifecycle-apply-locked-foreign-host:{json.dumps(holder_record, sort_keys=True)}"
            else:
                lock.write_bytes(b"")
                held = b""
            state_bytes = state_path.read_bytes()

            with pytest.raises(lifecycle.PilotError) as refused:
                lifecycle.apply_code_remediate_source(state_path=state_path)
        finally:
            _stop(sleeper)

        assert str(refused.value) == expected
        assert lock.read_bytes() == held
        assert state_path.read_bytes() == state_bytes
        assert not (state_path.parent / "source-application.patch").exists()
        assert _read(repository / "bucket-a.txt") == "baseline-a\n"

    def test_reclaim_keeps_a_lock_a_live_holder_took_after_the_exited_one(
        self, integrated_remediation: Pilot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Re-judge the lock right before removing it, so a live holder that took it in between keeps it.

        The exited holder's lock is replaced by a live one after this apply judged it abandoned, as when another apply
        reclaimed it first. Removing the lock on the earlier judgement would steal it from a running apply.
        """
        lifecycle, state_path, _, _ = integrated_remediation
        lock = _apply_lock_path(state_path)
        _plant_lock(lock, pid=_dead_pid(), host=socket.gethostname())
        state_bytes = state_path.read_bytes()
        real_alive = lifecycle._process_alive
        sleeper = _sleeping_child()
        live: list[bytes] = []

        def _alive_then_taken_over(pid: int) -> bool:
            """Answer for the exited holder, then let a live holder take the lock over before the removal."""
            alive = real_alive(pid)
            if not live:
                live.append(_plant_lock(lock, pid=sleeper.pid, host=socket.gethostname()))
            return alive

        monkeypatch.setattr(lifecycle, "_process_alive", _alive_then_taken_over)
        try:
            with pytest.raises(lifecycle.PilotError, match=r"^lifecycle-apply-locked$"):
                lifecycle.apply_code_remediate_source(state_path=state_path)
        finally:
            _stop(sleeper)

        assert lock.read_bytes() == live[0]
        assert not _reclaim_lock_path(state_path).exists()
        assert state_path.read_bytes() == state_bytes

    def test_release_leaves_a_lock_another_holder_now_owns(
        self, integrated_remediation: Pilot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Remove only this apply's own lock on release, keeping one a live holder took after this lock was removed.

        The lock is removed by hand mid-delivery and a live holder takes it; when this apply finishes, deleting the file
        regardless of its owner would leave that holder writing the source unprotected.
        """
        lifecycle, state_path, _, repository = integrated_remediation
        lock = _apply_lock_path(state_path)
        sleeper = _sleeping_child()
        taken: list[bytes] = []

        def _taken_over() -> None:
            """Remove this apply's lock by hand and let a live holder take the run."""
            lock.unlink()
            taken.append(_plant_lock(lock, pid=sleeper.pid, host=socket.gethostname()))

        _hook_after_source_apply(lifecycle, monkeypatch, repository, _taken_over)
        try:
            application = lifecycle.apply_code_remediate_source(state_path=state_path)
        finally:
            _stop(sleeper)

        assert application["status"] == "applied"
        assert lock.read_bytes() == taken[0]

    def test_refuses_a_second_cleanup_while_the_first_removes_worktrees(
        self, integrated_remediation: Pilot, tmp_path: Path, validator: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Refuse another process's cleanup of the same run while this one removes worktrees, and finish this one.

        Without the run lock the other cleanup removes the pending worktree and records the run completed, this one's
        own removal then fails, and its ``failed`` receipt replaces the completed record, so a later resume stops on a
        removal it can no longer prove.
        """
        lifecycle, state_path, state, _ = integrated_remediation
        lifecycle.apply_code_remediate_source(state_path=state_path)
        last = _cleanup_targets(tmp_path, state_path, state)[-1]
        contenders = _run_cli_before_git(
            lifecycle, monkeypatch, _is_worktree_command("remove", last), ["cleanup", "--state", str(state_path)]
        )

        cleanup = lifecycle.cleanup_code_remediate_pilot(state_path=state_path)
        monkeypatch.undo()

        assert [contender.returncode for contender in contenders] == [2]
        assert "lifecycle-apply-locked" in contenders[0].stderr
        assert cleanup["cleanup_status"] == "removed"
        assert read_json(state_path)["status"] == "completed"
        assert not _apply_lock_path(state_path).exists()
        _assert_completed_lifecycle(validator, state_path)

    def test_cleanup_stops_when_another_cleanup_finished_after_it_read_the_state(
        self, integrated_remediation: Pilot, validator: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reread the state right after taking the lock and stop when another cleanup recorded the run meanwhile."""
        lifecycle, state_path, _, _ = integrated_remediation
        lifecycle.apply_code_remediate_source(state_path=state_path)
        recorded = _finish_other_cleanup_after_read(lifecycle, monkeypatch, state_path)

        with pytest.raises(lifecycle.PilotError, match=r"^lifecycle-state-concurrent-update$"):
            lifecycle.cleanup_code_remediate_pilot(state_path=state_path)
        monkeypatch.undo()

        assert state_path.read_bytes() == recorded[0]
        assert not _apply_lock_path(state_path).exists()
        _assert_completed_lifecycle(validator, state_path)

    def test_refuses_while_another_process_reclaims_the_lock(self, integrated_remediation: Pilot) -> None:
        """Refuse while a live process is taking over the same abandoned lock, leaving both lock files as they were."""
        lifecycle, state_path, _, _ = integrated_remediation
        lock = _apply_lock_path(state_path)
        abandoned = _plant_lock(lock, pid=_dead_pid(), host=socket.gethostname())
        sleeper = _sleeping_child()
        try:
            reclaiming = _plant_lock(_reclaim_lock_path(state_path), pid=sleeper.pid, host=socket.gethostname())
            with pytest.raises(lifecycle.PilotError, match=r"^lifecycle-apply-locked$"):
                lifecycle.apply_code_remediate_source(state_path=state_path)
        finally:
            _stop(sleeper)

        assert lock.read_bytes() == abandoned
        assert _reclaim_lock_path(state_path).read_bytes() == reclaiming
        assert read_json(state_path)["status"] == "integrated"

    @pytest.mark.parametrize(
        "occupant",
        [
            pytest.param("link-to-an-exited-holder", marks=_skip_no_symlinks, id="link-to-an-exited-holder-record"),
            pytest.param("dangling-link", marks=_skip_no_symlinks, id="dangling-link"),
            pytest.param("directory", id="directory"),
        ],
    )
    def test_refuses_a_lock_path_that_is_not_a_regular_file(self, integrated_remediation: Pilot, occupant: str) -> None:
        """Stop with ``lifecycle-apply-lock-invalid`` and touch nothing while the lock path holds a link or a directory.

        Reading through a link would judge a record kept somewhere else, here an exited holder's that would otherwise be
        reclaimed by removing the link; a dangling link and a directory hold no record at all. The occupant is left for
        the operator to inspect.
        """
        lifecycle, state_path, _, repository = integrated_remediation
        lock = _apply_lock_path(state_path)
        record = state_path.parent / "lock-record-elsewhere"
        if occupant == "directory":
            lock.mkdir()
        else:
            if occupant == "link-to-an-exited-holder":
                _plant_lock(record, pid=_dead_pid(), host=socket.gethostname())
            lock.symlink_to(record)
        state_bytes = state_path.read_bytes()

        with pytest.raises(lifecycle.PilotError, match=r"^lifecycle-apply-lock-invalid$"):
            lifecycle.apply_code_remediate_source(state_path=state_path)

        assert lock.is_symlink() if occupant != "directory" else lock.is_dir()
        assert state_path.read_bytes() == state_bytes
        assert not _reclaim_lock_path(state_path).exists()
        assert _read(repository / "bucket-a.txt") == "baseline-a\n"

    @pytest.mark.parametrize("failure", [pytest.param("create-refused"), pytest.param("record-write-failed")])
    def test_stops_when_the_lock_cannot_be_created(
        self, integrated_remediation: Pilot, monkeypatch: pytest.MonkeyPatch, failure: str
    ) -> None:
        """Stop with ``lifecycle-apply-lock-unavailable``, leave no lock behind, and let a later apply take the lock.

        A refused create (an unwritable run directory) and a record that cannot be written into a created file are both
        reported as an unavailable lock rather than a raw ``OSError``; a partly written lock is removed again, since an
        empty or torn record would otherwise block every later apply as an unreadable holder.
        """
        lifecycle, state_path, _, repository = integrated_remediation
        lock = _apply_lock_path(state_path)
        real_open = os.open

        def _open_the_lock(path: Any, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
            """Refuse the lock create, or create it and hand back a descriptor the record cannot be written through."""
            if os.fspath(path) != str(lock):
                return real_open(path, flags, mode, dir_fd=dir_fd)
            if failure == "create-refused":
                raise PermissionError(errno.EACCES, "lock create refused", str(path))
            os.close(real_open(path, flags, mode))
            return real_open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))

        state_bytes = state_path.read_bytes()
        with monkeypatch.context() as refusing:
            refusing.setattr(lifecycle.os, "open", _open_the_lock)
            with pytest.raises(lifecycle.PilotError, match=r"^lifecycle-apply-lock-unavailable$"):
                lifecycle.apply_code_remediate_source(state_path=state_path)

        assert not lock.exists()
        assert state_path.read_bytes() == state_bytes
        assert _read(repository / "bucket-a.txt") == "baseline-a\n"
        assert lifecycle.apply_code_remediate_source(state_path=state_path)["status"] == "applied"
        assert not lock.exists()

    @pytest.mark.parametrize(
        ("handle", "exit_code", "exit_code_read", "last_error", "alive"),
        [
            pytest.param(7, 259, True, 0, True, id="running"),
            pytest.param(7, 0, True, 0, False, id="exited-handle-still-open"),
            pytest.param(7, 0, False, 0, True, id="exit-code-unreadable"),
            pytest.param(None, 0, True, 87, False, id="no-such-process"),
            pytest.param(None, 0, True, 5, True, id="access-denied"),
            pytest.param(None, 0, True, 6, True, id="unknown-open-failure"),
        ],
    )
    def test_windows_probe_treats_only_a_provably_ended_process_as_gone(
        self,
        lifecycle: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        handle: int | None,
        exit_code: int,
        exit_code_read: bool,
        last_error: int,
        alive: bool,
    ) -> None:
        """Answer from ``OpenProcess`` and ``GetExitCodeProcess`` without ``os.kill``, closing every opened handle.

        ``os.kill(pid, 0)`` on Windows sends a console control event or terminates the target, so the probe must use the
        Win32 query calls; any answer it cannot read keeps the holder alive.
        """
        kernel32 = _FakeKernel32(handle, exit_code, exit_code_read=exit_code_read)
        monkeypatch.setattr(lifecycle, "_windows_kernel32", lambda: kernel32)
        monkeypatch.setattr(lifecycle.ctypes, "get_last_error", lambda: last_error, raising=False)
        monkeypatch.setattr(lifecycle.os, "kill", functools.partial(_raise, AssertionError("os.kill used")))

        assert lifecycle._windows_process_alive(4321) is alive
        assert kernel32.closed == ([] if handle is None else [handle])


@pytest.mark.installed_plugin
class TestLifecycleStateDurability:
    """A lifecycle state save reaches the disk before it replaces the previous state, and its rename is flushed too."""

    def test_flushes_the_new_state_before_replacing_it_and_the_directory_after(
        self, lifecycle: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Flush the temporary file before the atomic replace, then the directory entry where the host supports it.

        Without the first flush a crash right after the replace can leave an empty or partial state file; without the
        second the replace itself can be lost. Native Windows cannot open a directory for flushing, so it skips that.
        """
        state_path = tmp_path / "run" / "production-lifecycle.json"
        events: list[str] = []
        real_fsync, real_replace = os.fsync, os.replace

        def _record_fsync(descriptor: int) -> None:
            """Note whether a directory or a file is flushed, then flush it."""
            events.append("directory-fsync" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file-fsync")
            real_fsync(descriptor)

        def _record_replace(source: Any, target: Any, **options: Any) -> None:
            """Note the atomic replace, then perform it."""
            events.append("replace")
            real_replace(source, target, **options)

        monkeypatch.setattr(os, "fsync", _record_fsync)
        monkeypatch.setattr(os, "replace", _record_replace)
        lifecycle._persist(state_path, {"status": "integrated"})
        monkeypatch.undo()

        assert events == ["file-fsync", "replace", *(["directory-fsync"] if os.name == "posix" else [])]
        assert read_json(state_path) == {"status": "integrated"}
        assert not state_path.with_name(f".{state_path.name}.tmp").exists()


# --- code-remediate: CLI lifecycle and receipt validation -----------------------------------------------------------


def _create_run(*arguments: str) -> Path:
    """Run the shipped run-directory creator for ``code-remediate`` and return the directory it printed."""
    created = subprocess.run(
        [sys.executable, str(PLUGIN_ROOT / "shared" / "create_run.py"), "--skill", "code-remediate", *arguments],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(created.stdout.strip())


def _keep_plan_location(repository: Path, plan_path: Path, approval_path: Path) -> tuple[Path, Path]:
    """Leave the plan and approval where the fixture wrote them."""
    return plan_path, approval_path


def _promote_to_pr_run(repository: Path, plan_path: Path, approval_path: Path) -> tuple[Path, Path]:
    """Move the plan and approval into a promoted ``pr-41/run-001`` evidence directory."""
    reports_root = repository / ".reports" / "codex"
    initial_run = _create_run("--root", str(reports_root))
    shutil.copytree(plan_path.parent, initial_run, dirs_exist_ok=True)
    pr_directory = initial_run / "pr"
    pr_directory.mkdir()
    (pr_directory / "pr.json").write_text(json.dumps({"number": 41}), encoding="utf-8")
    run = _create_run("--root", str(reports_root), "--promote-pr-run", str(initial_run))
    assert run.relative_to(repository).as_posix() == ".reports/codex/code-remediate/pr-41/run-001"
    return run / plan_path.name, run / approval_path.name


def _drive_cli_lifecycle(
    lifecycle: ModuleType, workspace: Path, plan_path: Path, approval_path: Path, state_path: Path
) -> tuple[list[int], dict[str, object]]:
    """Run every parent step through the CLI entry point; return each exit code and the prepared state."""
    state_arguments = ["--state", str(state_path)]
    prepare = ["prepare", "--plan", str(plan_path), "--approval", str(approval_path), "--workspace", str(workspace)]
    codes = [lifecycle.main([*prepare, *state_arguments])]
    state = read_json(state_path)
    for worktree, node, content in zip(_node_worktrees(workspace, state), state["nodes"], CHILD_EDITS, strict=True):
        for owned_path in node["owned_paths"]:
            _write(worktree / str(owned_path), content)
    handover_paths = [state_path.parent / f"{node['node_id']}-handover.json" for node in state["nodes"]]
    for node, handover_path in zip(state["nodes"], handover_paths, strict=True):
        summary = f"Completed {node['node_id']}."
        create = ["create-handover", *state_arguments, "--node", str(node["node_id"]), "--summary", summary]
        codes.append(lifecycle.main([*create, "--output", str(handover_path)]))
    codes.append(lifecycle.main(_join_arguments(state_path, handover_paths)))
    codes.extend(
        lifecycle.main(["collect", *state_arguments, "--node", str(node["node_id"])]) for node in state["nodes"]
    )
    codes.extend(lifecycle.main([step, *state_arguments]) for step in ("integrate", "apply-source", "cleanup"))
    return codes, state


@dataclass
class CompletedRemediation:
    """A remediation fixture driven through every CLI step to a completed lifecycle."""

    exit_codes: list[int]
    prepared_owned_paths: list[str]
    expected_owned_paths: list[str]
    plan_path: Path
    state_path: Path
    persisted: dict[str, object]
    bucket_plan: dict[str, object]


@pytest.fixture(
    scope="module",
    params=[
        pytest.param(
            (_keep_plan_location, None, ["bucket-a.txt"]),
            id="sorted-plan",
            marks=pytest.mark.xdist_group(name="remediation-sorted-plan"),
        ),
        pytest.param(
            (_promote_to_pr_run, None, ["bucket-a.txt"]),
            id="promoted-sorted-plan",
            marks=pytest.mark.xdist_group(name="remediation-promoted-plan"),
        ),
        pytest.param(
            (_keep_plan_location, ["bucket-c.txt", "bucket-a.txt"], ["bucket-a.txt", "bucket-c.txt"]),
            id="authored-unsorted-paths",
            marks=pytest.mark.xdist_group(name="remediation-unsorted-paths"),
        ),
        pytest.param(
            (_keep_plan_location, [".\\bucket-c.txt", ".//bucket-a.txt"], ["bucket-a.txt", "bucket-c.txt"]),
            id="portable-authored-spellings",
            marks=pytest.mark.xdist_group(name="remediation-portable-paths"),
        ),
    ],
)
def completed_remediation(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory, repository_templates: RepositoryTemplates
) -> CompletedRemediation:
    """Drive one remediation fixture through the whole CLI once per authored-path variant and share the evidence.

    Driving the lifecycle takes several real Git processes, so the receipt-validation tests reuse one completed run per
    variant. Tests that rewrite its state file put the original bytes back (see ``restored_state``).
    """
    relocate, authored_owned_paths, expected_owned_paths = request.param
    lifecycle = _load_lifecycle()
    workspace = tmp_path_factory.mktemp("remediation-cli")
    plan_path, approval_path, repository = _approved_code_remediate_plan(
        workspace, repository_templates, authored_owned_paths=authored_owned_paths
    )
    plan_path, approval_path = relocate(repository, plan_path, approval_path)
    state_path = plan_path.parent / "production-lifecycle.json"
    exit_codes, prepared = _drive_cli_lifecycle(lifecycle, workspace, plan_path, approval_path, state_path)
    return CompletedRemediation(
        exit_codes=exit_codes,
        prepared_owned_paths=prepared["nodes"][0]["owned_paths"],
        expected_owned_paths=expected_owned_paths,
        plan_path=plan_path,
        state_path=state_path,
        persisted=read_json(state_path),
        bucket_plan=read_json(plan_path),
    )


@pytest.fixture
def restored_state(completed_remediation: CompletedRemediation) -> None:
    """Put the completed lifecycle state file back after a test rewrote it."""
    original = completed_remediation.state_path.read_bytes()
    yield
    completed_remediation.state_path.write_bytes(original)


def _unowned_paths(expected: list[str]) -> object:
    """Receipt paths naming a file no bucket owns."""
    return ["unowned.txt"]


def _duplicated_first_path(expected: list[str]) -> object:
    """Receipt paths repeating the first owned path."""
    return [*expected, expected[0]]


def _uppercased_first_path(expected: list[str]) -> object:
    """Receipt paths spelling the first owned path in a different case."""
    return [expected[0].upper(), *expected[1:]]


def _bare_string_path(expected: list[str]) -> object:
    """Receipt paths given as one string instead of a list."""
    return expected[0]


@pytest.mark.installed_plugin
@pytest.mark.integration
class TestCodeRemediateCli:
    """The supported parent lifecycle runs end to end through the CLI and its receipts validate."""

    def test_join_rejects_handover_outside_evidence_root(
        self, lifecycle: ModuleType, tmp_path: Path, repository_templates: RepositoryTemplates
    ) -> None:
        """Reject an otherwise valid external handover before it can satisfy a parent join.

        One of the two valid handover files is written beside the workspace instead of inside the evidence directory;
        the join must exit with status 2 and leave the lifecycle ``prepared``.
        """
        plan_path, approval_path, _ = _approved_code_remediate_plan(tmp_path, repository_templates)
        state_path = plan_path.parent / "production-lifecycle.json"
        state = _prepare_remediation(lifecycle, tmp_path, plan_path, approval_path)
        _edit_owned(tmp_path, state, CHILD_EDITS)
        outside_handover = tmp_path / "WRITE-A-handover.json"
        inside_handover = state_path.parent / "WRITE-B-handover.json"
        first, second = _create_handovers(lifecycle, state_path, state)
        _write_json(outside_handover, first)
        _write_json(inside_handover, second)

        exit_code = lifecycle.main(_join_arguments(state_path, [outside_handover, inside_handover]))

        assert exit_code == 2
        assert read_json(state_path)["status"] == "prepared"

    @_skip_no_symlinks
    @pytest.mark.parametrize(
        ("make_output_parent", "exit_code", "written"),
        [
            pytest.param(_contained_output_parent, 0, True, id="contained"),
            pytest.param(_symlinked_output_parent, 2, False, id="symlinked-component"),
        ],
    )
    def test_handover_output_rejects_intermediate_symlink(
        self,
        lifecycle: ModuleType,
        tmp_path: Path,
        repository_templates: RepositoryTemplates,
        make_output_parent: Callable[[Path], Path],
        exit_code: int,
        written: bool,
    ) -> None:
        """Allow a contained output but reject a symlinked parent even before its terminal exists.

        The handover is written to ``handover-output/WRITE-A.json``; when that directory name is a symlink the CLI must
        refuse with status 2 and write nothing, while a real directory is accepted.
        """
        plan_path, approval_path, _ = _approved_code_remediate_plan(tmp_path, repository_templates)
        state_path = plan_path.parent / "production-lifecycle.json"
        state = _prepare_remediation(lifecycle, tmp_path, plan_path, approval_path)
        _edit_owned(tmp_path, state, CHILD_EDITS)
        output_path = make_output_parent(state_path.parent) / "WRITE-A.json"

        result = lifecycle.main(
            [
                "create-handover",
                "--state",
                str(state_path),
                "--node",
                "WRITE-A",
                "--summary",
                "One valid child handover.",
                "--output",
                str(output_path),
            ]
        )

        assert result == exit_code
        assert output_path.exists() is written

    def test_drives_supported_parent_lifecycle(
        self, completed_remediation: CompletedRemediation, validator: ModuleType
    ) -> None:
        """Accept producer-sorted portable receipts without weakening exact recorded ownership checks.

        Four plan variants (sorted, PR-promoted, authored unsorted paths, portable authored spellings) are driven
        through prepare, handovers, join, collect, integrate, apply and cleanup; every step exits 0, the lifecycle
        completes, and the validator accepts the receipt against the authored plan.
        """
        completed = completed_remediation
        reference = _lifecycle_reference(completed.state_path)

        validator._validate_code_remediate_production_lifecycle(
            reference, completed.bucket_plan, completed.state_path.parent, _sha256(completed.plan_path)
        )

        assert completed.exit_codes == [0] * 9
        assert completed.prepared_owned_paths == completed.expected_owned_paths
        assert completed.persisted["status"] == "completed"
        assert completed.persisted["cleanup"] == {
            "force": False,
            "status": "removed",
            "worktrees": [
                ".codex-rig-worktrees/fixture/WRITE-A",
                ".codex-rig-worktrees/fixture/WRITE-B",
                ".codex-rig-worktrees/fixture/integration",
            ],
        }

    @pytest.mark.usefixtures("restored_state")
    @pytest.mark.parametrize(
        ("surface", "error"),
        [
            pytest.param(
                "nodes", "code-remediate-production-lifecycle-child-patch-path-invalid", id="child-patch-path"
            ),
            pytest.param("joined_nodes", "code-remediate-production-lifecycle-node-mismatch", id="joined-node"),
        ],
    )
    @pytest.mark.parametrize(
        "invalid_paths",
        [
            pytest.param(_unowned_paths, id="unowned"),
            pytest.param(_duplicated_first_path, id="duplicate"),
            pytest.param(_uppercased_first_path, id="case-altered"),
            pytest.param(_bare_string_path, id="bare-string"),
        ],
    )
    def test_validator_rejects_mutated_receipt_paths(
        self,
        completed_remediation: CompletedRemediation,
        validator: ModuleType,
        surface: str,
        error: str,
        invalid_paths: Callable[[list[str]], object],
    ) -> None:
        """Never normalize a mutated receipt into acceptance.

        The completed run's state file is rewritten so one node's owned paths are unowned, duplicated, re-cased or a
        bare string; the validator must reject it with the surface-specific error.
        """
        completed = completed_remediation
        mutated = json.loads(json.dumps(completed.persisted))
        mutated[surface][0]["owned_paths"] = invalid_paths(completed.expected_owned_paths)
        _write_json(completed.state_path, mutated)
        reference = _lifecycle_reference(completed.state_path)

        with pytest.raises(SystemExit, match=rf"^{error}$"):
            validator._validate_code_remediate_production_lifecycle(
                reference, completed.bucket_plan, completed.state_path.parent, _sha256(completed.plan_path)
            )

    @pytest.mark.parametrize(
        ("invalid_path", "error"),
        [
            pytest.param("bucket-a.txt:stream", "owned-path-invalid", id="alternate-data-stream"),
            pytest.param("bucket-a.txt\x00", "owned-path-invalid", id="nul-byte"),
            pytest.param("bucket-a.txt.", "owned-path-nonportable", id="trailing-dot"),
            pytest.param("bucket-a.txt ", "owned-path-nonportable", id="trailing-space"),
        ],
    )
    def test_producer_rejects_invalid_owned_path(
        self,
        lifecycle: ModuleType,
        tmp_path: Path,
        repository_templates: RepositoryTemplates,
        invalid_path: str,
        error: str,
    ) -> None:
        """Refuse to build lifecycle nodes for an owned path that is invalid or nonportable.

        Alternate data streams and NUL bytes are invalid outright; trailing dots and spaces name a different file on
        Windows, so they are nonportable.
        """
        plan_path, _, repository = _approved_code_remediate_plan(tmp_path, repository_templates)
        plan = read_json(plan_path)
        plan["work_buckets"][0]["owned_paths"] = [invalid_path]

        with pytest.raises(lifecycle.PilotError, match=rf"^{error}$"):
            lifecycle._code_remediate_nodes(plan, repository, plan_path.parent.relative_to(repository).as_posix())

    @pytest.mark.parametrize(
        "invalid_path",
        [
            pytest.param("bucket-a.txt:stream", id="alternate-data-stream"),
            pytest.param("bucket-a.txt\x00", id="nul-byte"),
            pytest.param("bucket-a.txt.", id="trailing-dot"),
            pytest.param("bucket-a.txt ", id="trailing-space"),
        ],
    )
    def test_validator_rejects_invalid_owned_path(
        self, completed_remediation: CompletedRemediation, validator: ModuleType, invalid_path: str
    ) -> None:
        """Reject a plan whose owned path the producer would refuse, even against a completed receipt.

        The validator re-derives the producer's portability verdict from the authored plan, so a plan edited after the
        run cannot be reconciled with the completed lifecycle.
        """
        completed = completed_remediation
        invalid_plan = json.loads(json.dumps(completed.bucket_plan))
        invalid_plan["work_buckets"][0]["owned_paths"] = [invalid_path]
        reference = _lifecycle_reference(completed.state_path)

        with pytest.raises(SystemExit, match=r"^code-remediate-production-lifecycle-owned-path-invalid$"):
            validator._validate_code_remediate_production_lifecycle(
                reference, invalid_plan, completed.state_path.parent, _sha256(completed.plan_path)
            )


# --- generated-fixture pilot: preparation ---------------------------------------------------------------------------


@pytest.mark.installed_plugin
@pytest.mark.integration
class TestPrepareWritePilot:
    """A generated-fixture plan is admitted only when approved, portable, disjoint and cleanly based."""

    def test_rejects_plan_changed_after_approval(
        self, lifecycle: ModuleType, tmp_path: Path, repository_templates: RepositoryTemplates
    ) -> None:
        """Prevent a stale approval from authorizing changed nodes or scope.

        One appended newline changes the plan bytes after approval; the stored digest no longer matches and no worktree
        may be created.
        """
        plan_path, approval_path, _ = _approved_plan(tmp_path, repository_templates)
        _write(plan_path, _read(plan_path) + "\n")

        with pytest.raises(lifecycle.PilotError, match=r"^plan-approval-digest-mismatch$"):
            _prepare_generated(lifecycle, tmp_path, plan_path, approval_path)

        assert not (tmp_path / ".reports" / "codex" / "develop" / "fixture" / "worktrees").exists()

    @pytest.mark.parametrize(
        ("owned_path", "error"),
        [
            pytest.param("../escape.txt", "owned-path-invalid", id="parent-escape"),
            pytest.param("/absolute.txt", "owned-path-invalid", id="posix-absolute"),
            pytest.param("C:\\absolute.txt", "owned-path-invalid", id="drive-absolute"),
            pytest.param("C:relative.txt", "owned-path-invalid", id="drive-relative"),
            pytest.param("\\\\server\\share.txt", "owned-path-invalid", id="unc-share"),
            pytest.param("bucket-a.txt.", "owned-path-nonportable", id="trailing-dot"),
            pytest.param("BUCKET-A.TXT", "owned-path-alias", id="case-alias"),
            pytest.param("bucket-a.txt/child", "owned-path-overlap", id="child-of-owned-file"),
        ],
    )
    def test_rejects_nonportable_or_aliased_owned_paths(
        self,
        lifecycle: ModuleType,
        tmp_path: Path,
        repository_templates: RepositoryTemplates,
        owned_path: str,
        error: str,
    ) -> None:
        """Block traversal, Windows aliases, and nonportable bucket ownership.

        The second node claims a path that escapes the repository, is absolute on some OS, differs only in case or
        trailing dot from the first node's file, or sits beneath it; each is refused with its own error code.
        """
        plan_path, approval_path, _ = _approved_plan(tmp_path, repository_templates)
        plan = read_json(plan_path)
        plan["stages"][0]["nodes"][1]["owned_paths"] = [owned_path]
        _rewrite_plan(plan_path, approval_path, plan)

        with pytest.raises(lifecycle.PilotError, match=rf"^{error}"):
            _prepare_generated(lifecycle, tmp_path, plan_path, approval_path)

    def test_rejects_overlapping_resource_locks(
        self, lifecycle: ModuleType, tmp_path: Path, repository_templates: RepositoryTemplates
    ) -> None:
        """Stop two otherwise disjoint packages from sharing one exclusive resource.

        Both nodes own different files but declare the same ``git-index`` lock, so they cannot run in parallel.
        """
        plan_path, approval_path, _ = _approved_plan(tmp_path, repository_templates)
        plan = read_json(plan_path)
        first, second = plan["stages"][0]["nodes"]
        first["resource_locks"] = ["git-index"]
        second["resource_locks"] = ["git-index"]
        _rewrite_plan(plan_path, approval_path, plan)

        with pytest.raises(lifecycle.PilotError, match=r"^resource-lock-overlap:FIXTURE-WRITE-B$"):
            _prepare_generated(lifecycle, tmp_path, plan_path, approval_path)

    @pytest.mark.parametrize(
        "dirty_file",
        [pytest.param("bucket-a.txt", id="tracked"), pytest.param("untracked.txt", id="untracked")],
    )
    def test_rejects_dirty_fixture_repository(
        self, lifecycle: ModuleType, tmp_path: Path, repository_templates: RepositoryTemplates, dirty_file: str
    ) -> None:
        """Prevent dispatch from a baseline with hidden tracked or untracked drift.

        The fixture repository gains one modified tracked file or one new untracked file after approval; either makes
        the approved baseline stale.
        """
        plan_path, approval_path, repository = _approved_plan(tmp_path, repository_templates)
        _write(repository / dirty_file, "dirty\n")

        with pytest.raises(lifecycle.PilotError, match=r"^fixture-repository-dirty$"):
            _prepare_generated(lifecycle, tmp_path, plan_path, approval_path)

    @_skip_no_symlinks
    def test_rejects_symlinked_generated_root(
        self, lifecycle: ModuleType, tmp_path: Path, repository_templates: RepositoryTemplates
    ) -> None:
        """Prevent a managed worktree path from escaping through a symlink component.

        The generated ``worktrees`` directory is a symlink to a directory outside the workspace.
        """
        plan_path, approval_path, _ = _approved_plan(tmp_path, repository_templates)
        outside = tmp_path / "outside"
        outside.mkdir()
        (tmp_path / ".reports" / "codex" / "develop" / "fixture" / "worktrees").symlink_to(
            outside, target_is_directory=True
        )

        with pytest.raises(lifecycle.PilotError, match=r"^managed-path-symlink-forbidden$"):
            _prepare_generated(lifecycle, tmp_path, plan_path, approval_path)

    def test_creates_detached_worktrees_at_exact_frozen_head(self, pilot: Pilot, tmp_path: Path) -> None:
        """Create isolated children without mutating the generated source checkout.

        Each child worktree must sit on the frozen baseline commit with a detached HEAD, and the source checkout must
        stay completely clean.
        """
        _, _, state, repository = pilot
        worktrees = _node_worktrees(tmp_path, state)

        heads = [_git(worktree, "rev-parse", "HEAD").stdout.strip() for worktree in worktrees]
        branch_lookups = [
            _git(worktree, "symbolic-ref", "-q", "HEAD", check=False).returncode for worktree in worktrees
        ]

        assert heads == [str(state["baseline_head"])] * 2
        assert branch_lookups == [1, 1]
        assert _git(repository, "status", "--porcelain=v1", "--untracked-files=all").stdout == ""

    def test_canonicalizes_multi_path_package_order(
        self, lifecycle: ModuleType, tmp_path: Path, repository_templates: RepositoryTemplates
    ) -> None:
        """Freeze multi-path ownership in the same lexical order used by parent verification.

        The first node is authored with ``bucket-c.txt`` before ``bucket-a.txt``; the state must list them sorted so
        parent verification compares like with like.
        """
        plan_path, approval_path, repository = _approved_plan(tmp_path, repository_templates)
        _write(repository / "bucket-c.txt", "baseline-c\n")
        _git(repository, "add", "bucket-c.txt")
        _git(repository, "commit", "-q", "-m", "add third fixture bucket")
        plan = read_json(plan_path)
        plan["stages"][0]["nodes"][0]["owned_paths"] = ["bucket-c.txt", "bucket-a.txt"]
        _rewrite_plan(plan_path, approval_path, plan)

        state = _prepare_generated(lifecycle, tmp_path, plan_path, approval_path)

        assert state["nodes"][0]["owned_paths"] == ["bucket-a.txt", "bucket-c.txt"]


# --- generated-fixture pilot: handovers and join --------------------------------------------------------------------


def _drop_last_handover(handovers: list[dict[str, object]]) -> None:
    """Omit the last child's handover."""
    handovers.pop()


def _duplicate_first_handover(handovers: list[dict[str, object]]) -> None:
    """Report the first child's handover twice instead of the second child's."""
    handovers[1] = dict(handovers[0])


def _set_first_handover_field(key: str, value: object, handovers: list[dict[str, object]]) -> None:
    """Set one field of the first child's handover."""
    handovers[0][key] = value


@pytest.mark.installed_plugin
@pytest.mark.integration
class TestChildHandover:
    """Children report through handovers that the parent verifies against the worktrees before joining."""

    @pytest.mark.parametrize("as_state_path", [pytest.param(Path, id="path"), pytest.param(str, id="string")])
    def test_accepts_canonical_state_path_forms(
        self, pilot: Pilot, tmp_path: Path, as_state_path: Callable[[Path], object]
    ) -> None:
        """Return the canonical report for supported lifecycle-state path forms.

        A ``Path`` and its string form must give the same five-field report that an independent diff of the child
        worktree produces, without advancing the lifecycle past ``prepared``.
        """
        lifecycle, state_path, state, _ = pilot
        node = state["nodes"][1]
        _write(_node_worktrees(tmp_path, state)[1] / "bucket-b.txt", "child-b\n")
        expected = _child_handover(tmp_path, node)
        expected["summary"] = "Updated bucket B."

        report = lifecycle.create_completed_child_handover(
            state_path=as_state_path(state_path), node_id="FIXTURE-WRITE-B", summary="Updated bucket B."
        )

        assert report == expected
        assert read_json(state_path)["status"] == "prepared"

    def test_rejects_invalid_state_path_type(self, pilot: Pilot) -> None:
        """Return the stable pilot error for an unsupported state-path object.

        Anything that is neither a ``Path`` nor a string must fail with the documented error code, not an arbitrary
        ``TypeError`` from deep inside the lifecycle.
        """
        lifecycle, _, _, _ = pilot

        with pytest.raises(lifecycle.PilotError, match=r"^lifecycle-state-path-invalid$"):
            lifecycle.create_completed_child_handover(
                state_path=object(), node_id="FIXTURE-WRITE-B", summary="Updated bucket B."
            )

    def test_rejects_relative_state_path(self, pilot: Pilot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Fail closed when a child resolves the parent state relative to its worktree.

        A child runs from inside its own worktree and passes the state path relative to the workspace; that would
        resolve to a different, child-controlled location, so a relative path is refused.
        """
        lifecycle, state_path, state, _ = pilot
        monkeypatch.chdir(_node_worktrees(tmp_path, state)[1])

        with pytest.raises(lifecycle.PilotError, match=r"^lifecycle-state-invalid$"):
            lifecycle.create_completed_child_handover(
                state_path=state_path.relative_to(tmp_path), node_id="FIXTURE-WRITE-B", summary="Updated bucket B."
            )

    def test_parent_authoritative_handovers_gate_patch_collection_and_integration(
        self, edited_pilot: Pilot, tmp_path: Path
    ) -> None:
        """Require both child reports and verify them against worktrees before integration.

        Both children edit their bucket and report; the parent joins the reports, collects patches whose digests match
        the reported ones, and integrates them into a separate worktree while the source stays at baseline.
        """
        lifecycle, state_path, state, repository = edited_pilot

        handovers = _create_handovers(lifecycle, state_path, state)
        joined = lifecycle.join_child_handovers(state_path=state_path, handovers=handovers)
        patches = _collect_all(lifecycle, state_path, state)
        result = lifecycle.integrate_write_pilot(state_path=state_path)
        integration = tmp_path / str(result["integration_worktree"])

        assert joined["status"] == "joined"
        assert [node["handover"]["status"] for node in joined["nodes"]] == ["completed", "completed"]
        assert [node["handover"]["patch_sha256"] for node in joined["nodes"]] == [
            handover["patch_sha256"] for handover in handovers
        ]
        assert [patch["patch_sha256"] for patch in patches] == [
            node["handover"]["patch_sha256"] for node in joined["nodes"]
        ]
        assert _read(integration / "bucket-a.txt") == "child-a\n"
        assert _read(integration / "bucket-b.txt") == "child-b\n"
        assert _read(repository / "bucket-a.txt") == "baseline-a\n"

    @pytest.mark.parametrize(
        ("change_handovers", "error"),
        [
            pytest.param(_drop_last_handover, "child-handovers-incomplete", id="missing"),
            pytest.param(_duplicate_first_handover, "child-handover-invalid", id="duplicate"),
            pytest.param(
                functools.partial(_set_first_handover_field, "unexpected", True),
                "child-handover-invalid",
                id="extra-field",
            ),
            pytest.param(
                functools.partial(_set_first_handover_field, "summary", ""),
                "child-handover-summary-invalid:FIXTURE-WRITE-A",
                id="empty-summary",
            ),
            pytest.param(
                functools.partial(_set_first_handover_field, "changed_paths", ["bucket-b.txt"]),
                "child-handover-paths-mismatch:FIXTURE-WRITE-A",
                id="wrong-path",
            ),
            pytest.param(
                functools.partial(_set_first_handover_field, "patch_sha256", "0" * 64),
                "child-handover-patch-mismatch:FIXTURE-WRITE-A",
                id="wrong-hash",
            ),
        ],
    )
    def test_join_rejects_incomplete_or_mismatched_report(
        self,
        edited_pilot: Pilot,
        tmp_path: Path,
        change_handovers: Callable[[list[dict[str, object]]], None],
        error: str,
    ) -> None:
        """Reject partial, empty, or Git-inconsistent child reports.

        Valid reports are built from the worktrees and then damaged one way (missing, duplicated, extra field, empty
        summary, wrong paths, wrong patch hash). The join must fail with that reason, write no patch and keep the
        worktrees for diagnosis.
        """
        lifecycle, state_path, state, _ = edited_pilot
        handovers = [_child_handover(tmp_path, node) for node in state["nodes"]]
        change_handovers(handovers)

        with pytest.raises(lifecycle.PilotError, match=rf"^{error}$"):
            lifecycle.join_child_handovers(state_path=state_path, handovers=handovers)

        failed_state = read_json(state_path)
        assert failed_state["status"] == "join-failed"
        assert failed_state["join_failure"]["reason"] == error
        assert not (state_path.parent / "patch-a.diff").exists()
        assert all(worktree.exists() for worktree in _node_worktrees(tmp_path, state))

    @pytest.mark.parametrize("status", ["failed", "cancelled"])
    def test_join_records_unsuccessful_child_report(self, edited_pilot: Pilot, tmp_path: Path, status: str) -> None:
        """Reject a child that reports failure or cancellation, and keep what it reported.

        The first child's otherwise valid report says ``failed`` or ``cancelled``; the join stops with ``child-handover-
        not-completed`` and the state records which node and exactly what it sent.
        """
        lifecycle, state_path, state, _ = edited_pilot
        handovers = [_child_handover(tmp_path, node) for node in state["nodes"]]
        handovers[0]["status"] = status
        error = "child-handover-not-completed:FIXTURE-WRITE-A"

        with pytest.raises(lifecycle.PilotError, match=rf"^{error}$"):
            lifecycle.join_child_handovers(state_path=state_path, handovers=handovers)

        failed_state = read_json(state_path)
        assert failed_state["status"] == "join-failed"
        assert failed_state["join_failure"]["reason"] == error
        assert failed_state["join_failure"]["failed_node"] == "FIXTURE-WRITE-A"
        assert failed_state["join_failure"]["received_reports"][0]["status"] == status
        assert failed_state["join_failure"]["received_reports"][0]["summary"] == "Completed FIXTURE-WRITE-A."
        assert not (state_path.parent / "patch-a.diff").exists()
        assert all(worktree.exists() for worktree in _node_worktrees(tmp_path, state))


def _fail_after_fast_forward(
    gated: GatedGroupedRemediation, monkeypatch: pytest.MonkeyPatch, *hooks: Callable[[], None]
) -> None:
    """Complete the source fast-forward, run ``hooks``, then fail as a later postcondition would."""
    lifecycle, _, _, repository = gated.pilot
    real_git = lifecycle._git

    def _fail_after_moving(target: Path, *arguments: str) -> str:
        """Let Git move the branch, then report the step as failed."""
        if target == repository and arguments[:1] == ("merge",):
            real_git(target, *arguments)
            for hook in hooks:
                hook()
            raise lifecycle.PilotError("git-command-failed:merge")
        return real_git(target, *arguments)

    monkeypatch.setattr(lifecycle, "_git", _fail_after_moving)


def _commit_on_the_tip(repository: Path) -> None:
    """Add a user commit on top of the fast-forwarded tip, leaving the branch past the recorded integration head."""
    _git(repository, "commit", "-q", "--allow-empty", "-m", "User commit after the fast-forward")


def _rewind_to_first_integrated_commit(repository: Path) -> None:
    """Leave the branch at the first integrated commit, which sits on the baseline but is not the integration tip."""
    _git(repository, "reset", "-q", "--hard", "HEAD~1")


def _edit_first_approved_path(repository: Path) -> None:
    """Change an approved path at the fast-forwarded tip, so Git no longer shows the exact delivery."""
    _write(repository / "bucket-a.txt", "user edit after the fast-forward\n")


#: Ways a branch can end up after a fast-forward that moved it without showing this run's exact delivery.
_MOVED_ELSEWHERE = [
    pytest.param(_commit_on_the_tip, id="user-commit-on-the-tip"),
    pytest.param(_rewind_to_first_integrated_commit, id="first-integrated-commit-only"),
    pytest.param(_edit_first_approved_path, id="tip-with-an-edited-approved-path"),
]


@pytest.mark.installed_plugin
@pytest.mark.integration
class TestGroupedCodeRemediate:
    """Authoritative source advances only to the verified ordered sequence of worker fixes."""

    def test_validates_pr_target_integration_after_grouped_delivery(
        self, gated_grouped_remediation: GatedGroupedRemediation, validator: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Validate the collected checkout even when the calling shell has an unrelated Git directory."""
        lifecycle, state_path, _, repository = gated_grouped_remediation.pilot
        gates = gated_grouped_remediation.gates
        baseline = gated_grouped_remediation.baseline
        head = gated_grouped_remediation.head
        assert lifecycle.apply_code_remediate_source(state_path=state_path, gates_path=gates)["status"] == "applied"
        _assert_completed_lifecycle_after_cleanup(lifecycle, validator, state_path)

        run = state_path.parent
        pr = run / "pr"
        pr.mkdir()
        branch = _git(repository, "branch", "--show-current").stdout.strip()
        checkout_path = pr / "local-checkout.json"
        checkout = {"worktree": repository.resolve().as_posix(), "local_branch": branch}
        _write_json(checkout_path, checkout)
        resolution = pr / "merge-resolution.json"
        _write_json(
            resolution,
            {
                "schema_version": 2,
                "conflicts_detected": False,
                "status": "not-needed",
                "authorization": "not-required",
                "base_remote_ref": "refs/heads/main",
                "target_oid": baseline,
                "pre_merge_head": baseline,
                "post_merge_head": baseline,
                "merge_commit": None,
                "resolved_paths": [],
                "unmerged_paths": [],
                "evidence": ["target already included"],
            },
        )
        metadata = {
            "mode": "pr",
            "resolution_workplan": {
                **_lifecycle_reference(state_path),
                "bucket_plan_sha256": _sha256(run / "work-bucket-plan.json"),
            },
            "merge_resolution": {
                "artifact_path": resolution.as_posix(),
                "conflicts_detected": False,
                "authorization": "not-required",
                "status": "not-needed",
            },
        }

        def validate() -> None:
            """Run the target integration check used by final PR artifact validation."""
            validator._validate_code_remediate_merge_resolution(metadata, pr, {"local_head": baseline}, baseline)

        assert _git(repository, "rev-parse", "HEAD").stdout.strip() == head
        assert not gated_grouped_remediation.integration.exists()
        with monkeypatch.context() as shell:
            shell.setenv("GIT_DIR", str(repository / "missing-git-directory"))
            validate()

        current_resolution = read_json(resolution)
        _write_json(resolution, {**current_resolution, "schema_version": 1})
        with pytest.raises(SystemExit, match="grouped-merge-resolution-current-required"):
            validate()
        _write_json(resolution, current_resolution)

        _write_json(checkout_path, {**checkout, "worktree": repository.parent.as_posix()})
        with pytest.raises(SystemExit, match="target-integration-source-mismatch"):
            validate()
        _write_json(checkout_path, {**checkout, "local_branch": "wrong-branch"})
        with pytest.raises(SystemExit, match="target-integration-source-mismatch"):
            validate()
        _write_json(checkout_path, checkout)

        original_gates = gates.read_bytes()
        gates.write_bytes(original_gates + b"\n")
        with pytest.raises(SystemExit, match="production-lifecycle-gates-mismatch"):
            validate()
        gates.write_bytes(original_gates)

        # Keep the tested commit in the object database but remove it from the collected branch.
        _git(repository, "update-ref", f"refs/heads/{branch}", baseline, head)
        with pytest.raises(SystemExit, match="target-integration-grouped-delivery-mismatch"):
            validate()

    def test_dispatch_approval_cannot_authorize_topic_commits(
        self, lifecycle: ModuleType, tmp_path: Path, repository_templates: RepositoryTemplates
    ) -> None:
        """A valid dispatch digest alone cannot select the grouped commit route."""
        plan_path, approval_path, repository = _approved_code_remediate_plan(tmp_path, repository_templates)
        _use_grouped_plan(plan_path, approval_path, repository)
        plan = read_json(plan_path)
        del plan["commit_choice"]
        _rewrite_plan(plan_path, approval_path, plan)

        with pytest.raises(lifecycle.PilotError, match="grouped-commit-choice-required"):
            _prepare_remediation(lifecycle, tmp_path, plan_path, approval_path)

        assert not (tmp_path / ".codex-rig-worktrees").exists()

    @pytest.mark.parametrize(
        ("field", "replacement", "error"),
        [
            pytest.param(
                "commit_choice",
                {"mode": "grouped", "source": "user-prompt"},
                "grouped-commit-choice-required",
                id="contradictory-consent",
            ),
            pytest.param(
                "tests_command",
                "pytest tests/test_single.py",
                "grouped-full-test-command-mismatch",
                id="narrower-suite",
            ),
        ],
    )
    def test_grouped_plan_cannot_override_upfront_decisions(
        self,
        lifecycle: ModuleType,
        tmp_path: Path,
        repository_templates: RepositoryTemplates,
        field: str,
        replacement: object,
        error: str,
    ) -> None:
        """Reject a refrozen plan that changes the recorded commit choice or full test invocation."""
        plan_path, approval_path, repository = _approved_code_remediate_plan(tmp_path, repository_templates)
        _use_grouped_plan(plan_path, approval_path, repository)
        plan = read_json(plan_path)
        plan[field] = replacement
        _rewrite_plan(plan_path, approval_path, plan)

        with pytest.raises(lifecycle.PilotError, match=error):
            _prepare_remediation(lifecycle, tmp_path, plan_path, approval_path)

    def test_grouped_plan_cannot_override_leave_unstaged(
        self, lifecycle: ModuleType, tmp_path: Path, repository_templates: RepositoryTemplates
    ) -> None:
        """Keep the recorded denial controlling despite a contradictory grouped plan."""
        plan_path, approval_path, repository = _approved_code_remediate_plan(tmp_path, repository_templates)
        _use_grouped_plan(plan_path, approval_path, repository)
        workplan = plan_path.parent / "resolution-workplan.md"
        workplan.write_text(
            _read(workplan).replace("group findings by topic (explicit-input)", "leave unstaged"),
            encoding="utf-8",
        )

        with pytest.raises(lifecycle.PilotError, match="grouped-commit-choice-required"):
            _prepare_remediation(lifecycle, tmp_path, plan_path, approval_path)

    def test_requires_worker_verification_receipt(
        self, grouped_remediation: Pilot, draft_commits: tuple[str, str]
    ) -> None:
        """A clean draft commit cannot substitute for its successful targeted checks."""
        lifecycle, state_path, _, _ = grouped_remediation
        assert draft_commits
        with pytest.raises(lifecycle.PilotError, match=r"^child-advisory-receipt-required:WRITE-A$"):
            lifecycle.create_completed_child_handover(state_path=state_path, node_id="WRITE-A", summary="Done")

    def test_join_rejects_replaced_worker_check_output(
        self, grouped_remediation: Pilot, draft_commits: tuple[str, str]
    ) -> None:
        """Recheck targeted output bytes before accepting an otherwise valid handover."""
        lifecycle, state_path, state, _ = grouped_remediation
        assert draft_commits
        handovers = _create_handovers(lifecycle, state_path, state)
        (state_path.parent / "WRITE-A-targeted.stdout.txt").write_bytes(b"different output\n")

        with pytest.raises(lifecycle.PilotError, match=r"^child-advisory-receipt-invalid:WRITE-A$"):
            lifecycle.join_child_handovers(state_path=state_path, handovers=handovers)

    def test_worker_receipt_is_advisory_until_integration_gates_pass(
        self, grouped_remediation: Pilot, draft_commits: tuple[str, str], tmp_path: Path
    ) -> None:
        """A receipt the worker wrote without running anything still joins; only integration-tip gates can deliver.

        The worker authors both the receipt and its output file, so the parent can prove only that they agree with each
        other. Delivery must therefore still demand the full gates run on the integration tip.
        """
        lifecycle, state_path, state, repository = grouped_remediation
        assert draft_commits
        baseline = _git(repository, "rev-parse", "HEAD").stdout.strip()
        handovers = [
            lifecycle.create_completed_child_handover(
                state_path=state_path,
                node_id=str(node["node_id"]),
                summary="Done",
                verification_path=_write_fabricated_receipt(state_path.parent, worktree, node),
            )
            for node, worktree in zip(state["nodes"], _node_worktrees(tmp_path, state), strict=True)
        ]
        lifecycle.join_child_handovers(state_path=state_path, handovers=handovers)
        _collect_all(lifecycle, state_path, state)
        lifecycle.integrate_write_pilot(state_path=state_path)

        with pytest.raises(lifecycle.PilotError, match=r"^grouped-gate-evidence-required$"):
            lifecycle.apply_code_remediate_source(state_path=state_path)

        assert _git(repository, "rev-parse", "HEAD").stdout.strip() == baseline

    @pytest.mark.parametrize(
        ("artifact", "error"),
        [
            pytest.param("checks/tests.stdout.txt", _GATE_LOG_DRIFT, id="gate-log"),
            pytest.param("WRITE-A-targeted.stdout.txt", _WORKER_OUTPUT_DRIFT, id="worker-output"),
        ],
    )
    def test_rebinds_evidence_right_before_fast_forward(
        self,
        gated_grouped_remediation: GatedGroupedRemediation,
        monkeypatch: pytest.MonkeyPatch,
        artifact: str,
        error: str,
    ) -> None:
        """Refuse to move the branch when a referenced log is swapped after the first validation pass.

        The swap lands after the lifecycle records ``source-applying``, so only a re-check inside the last pre-merge
        block can see it. The branch must stay at the baseline, and because nothing reached the source the run reopens
        as ``integrated`` with the attempt recorded as ``preflight-failed``.
        """
        lifecycle, state_path, _, repository = gated_grouped_remediation.pilot
        _swap_after_delivery_intent(lifecycle, monkeypatch, state_path.parent / artifact)
        with pytest.raises(lifecycle.PilotError, match=error):
            lifecycle.apply_code_remediate_source(state_path=state_path, gates_path=gated_grouped_remediation.gates)

        assert _git(repository, "rev-parse", "HEAD").stdout.strip() == gated_grouped_remediation.baseline
        assert _apply_statuses(state_path) == ("integrated", "preflight-failed")

    def test_reopens_the_run_when_git_refuses_the_fast_forward_untouched(
        self, gated_grouped_remediation: GatedGroupedRemediation, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Keep the run retryable when Git refuses the grouped fast-forward before changing the branch or any path."""
        lifecycle, state_path, _, repository = gated_grouped_remediation.pilot
        gates, head = gated_grouped_remediation.gates, gated_grouped_remediation.head
        real_git = lifecycle._git

        def _refuse_merge(target: Path, *arguments: str) -> str:
            """Fail the fast-forward the way a leftover index lock or a signature policy does, changing nothing."""
            if target == repository and arguments[:1] == ("merge",):
                raise lifecycle.PilotError("git-command-failed:merge")
            return real_git(target, *arguments)

        monkeypatch.setattr(lifecycle, "_git", _refuse_merge)
        with pytest.raises(lifecycle.PilotError, match=r"^git-command-failed:merge$"):
            lifecycle.apply_code_remediate_source(state_path=state_path, gates_path=gates)
        monkeypatch.undo()

        assert _apply_statuses(state_path) == ("integrated", "preflight-failed")
        assert _git(repository, "rev-parse", "HEAD").stdout.strip() == gated_grouped_remediation.baseline
        application = lifecycle.apply_code_remediate_source(state_path=state_path, gates_path=gates)
        assert application["status"] == "applied"
        assert _git(repository, "rev-parse", "HEAD").stdout.strip() == head

    def test_restores_paths_and_reopens_the_run_when_the_fast_forward_fails_part_way(
        self, gated_grouped_remediation: GatedGroupedRemediation, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Undo a fast-forward that wrote approved paths and then failed before moving the branch, so a retry works."""
        lifecycle, state_path, _, repository = gated_grouped_remediation.pilot
        gates, baseline = gated_grouped_remediation.gates, gated_grouped_remediation.baseline
        head = gated_grouped_remediation.head
        real_git = lifecycle._git

        def _fail_after_writing(target: Path, *arguments: str) -> str:
            """Let Git write the files, then leave the branch and index at the baseline and fail like a locked file."""
            if target == repository and arguments[:1] == ("merge",):
                real_git(target, *arguments)
                real_git(target, "reset", "-q", baseline)
                raise lifecycle.PilotError("git-command-failed:merge")
            return real_git(target, *arguments)

        monkeypatch.setattr(lifecycle, "_git", _fail_after_writing)
        with pytest.raises(lifecycle.PilotError, match=r"^git-command-failed:merge$"):
            lifecycle.apply_code_remediate_source(state_path=state_path, gates_path=gates)
        monkeypatch.undo()

        assert _apply_statuses(state_path) == ("integrated", "preflight-failed")
        assert _git(repository, "status", "--porcelain").stdout == ""
        assert _git(repository, "rev-parse", "HEAD").stdout.strip() == baseline
        application = lifecycle.apply_code_remediate_source(state_path=state_path, gates_path=gates)
        assert application["status"] == "applied"
        assert _git(repository, "rev-parse", "HEAD").stdout.strip() == head

    def test_records_a_fast_forward_git_shows_landed_when_a_later_step_failed(
        self,
        gated_grouped_remediation: GatedGroupedRemediation,
        validator: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Record the delivery as applied when the fast-forward moved the branch and Git shows it landed exactly.

        The branch is never moved back. Recording ``failed`` instead would strand a delivered run: no later apply,
        resume or cleanup accepts a ``failed`` run whose source already holds the integration tip.
        """
        lifecycle, state_path, _, repository = gated_grouped_remediation.pilot
        _fail_after_fast_forward(gated_grouped_remediation, monkeypatch)

        application = lifecycle.apply_code_remediate_source(
            state_path=state_path, gates_path=gated_grouped_remediation.gates
        )
        monkeypatch.undo()

        assert application["status"] == "applied"
        assert _apply_statuses(state_path) == ("source-applied", "applied")
        assert read_json(state_path)["source"]["applied_head"] == gated_grouped_remediation.head
        assert _git(repository, "rev-parse", "HEAD").stdout.strip() == gated_grouped_remediation.head
        _assert_completed_lifecycle_after_cleanup(lifecycle, validator, state_path)

    @pytest.mark.parametrize("move_elsewhere", _MOVED_ELSEWHERE)
    def test_keeps_the_run_failed_when_the_branch_moved_before_the_fast_forward_failed(
        self,
        gated_grouped_remediation: GatedGroupedRemediation,
        monkeypatch: pytest.MonkeyPatch,
        move_elsewhere: Callable[[Path], None],
    ) -> None:
        """A branch Git already moved is never rolled back: the run records ``failed`` with the observed head.

        Only the recorded integration tip on the baseline with every delivered path counts as landed; a branch left at
        any other commit, or at the tip with an approved path changed, is not this delivery.
        """
        lifecycle, state_path, _, repository = gated_grouped_remediation.pilot
        _fail_after_fast_forward(gated_grouped_remediation, monkeypatch, functools.partial(move_elsewhere, repository))

        with pytest.raises(lifecycle.PilotError, match=r"^git-command-failed:merge$"):
            lifecycle.apply_code_remediate_source(state_path=state_path, gates_path=gated_grouped_remediation.gates)
        observed = _git(repository, "rev-parse", "HEAD").stdout.strip()

        assert _apply_statuses(state_path) == ("failed", "failed")
        assert read_json(state_path)["source_application"]["observed_head"] == observed
        assert observed != gated_grouped_remediation.baseline

    @pytest.mark.parametrize(
        ("artifact", "error", "repair"),
        [
            pytest.param("checks/tests.stdout.txt", _GATE_LOG_DRIFT, "restore-bytes", id="gate-log-restored"),
            pytest.param("checks/tests.stdout.txt", _GATE_LOG_DRIFT, "rerun-gates", id="gate-log-regenerated"),
            pytest.param("WRITE-A-targeted.stdout.txt", _WORKER_OUTPUT_DRIFT, "restore-bytes", id="worker-output"),
        ],
    )
    def test_retries_delivery_after_preflight_drift_is_repaired(
        self,
        gated_grouped_remediation: GatedGroupedRemediation,
        tmp_path: Path,
        validator: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        artifact: str,
        error: str,
        repair: str,
    ) -> None:
        """Deliver on a later apply once the drift that stopped preflight is repaired; refuse while it persists.

        Preflight wrote nothing to the source, so the reopened run must stay retryable, including when the operator
        repairs the evidence by re-running the gates, which records a new ``gates.json`` digest.
        """
        lifecycle, state_path, state, repository = gated_grouped_remediation.pilot
        gates, head = gated_grouped_remediation.gates, gated_grouped_remediation.head
        swapped = state_path.parent / artifact
        original = swapped.read_bytes()
        _swap_after_delivery_intent(lifecycle, monkeypatch, swapped)
        with pytest.raises(lifecycle.PilotError, match=error):
            lifecycle.apply_code_remediate_source(state_path=state_path, gates_path=gates)
        monkeypatch.undo()
        with pytest.raises(lifecycle.PilotError, match=error):
            lifecycle.apply_code_remediate_source(state_path=state_path, gates_path=gates)
        assert _git(repository, "rev-parse", "HEAD").stdout.strip() == gated_grouped_remediation.baseline
        assert read_json(state_path)["status"] == "integrated"

        if repair == "rerun-gates":
            original_manifest = gates.read_bytes()
            damaged_output = swapped.read_bytes()
            _run_grouped_gates(state_path.parent, gated_grouped_remediation.integration, head)
            diagnostic = state_path.parent / "gate-diagnostics/001"
            assert (diagnostic / "gates.json").read_bytes() == original_manifest
            assert (diagnostic / artifact).read_bytes() == damaged_output
            assert read_json(diagnostic / "gate-log-errors.json") == {
                artifact: hashlib.sha256(damaged_output).hexdigest()
            }
            assert not (state_path.parent / "gate-attempts").exists()
            with pytest.raises(SystemExit, match="gate-evidence-damaged-gate-log"):
                validator._validate_gates(diagnostic)
        else:
            swapped.write_bytes(original)
        application = lifecycle.apply_code_remediate_source(state_path=state_path, gates_path=gates)

        assert application["status"] == "applied"
        assert application["gates_sha256"] == _sha256(gates)
        assert _git(repository, "rev-parse", "HEAD").stdout.strip() == head
        assert lifecycle.cleanup_code_remediate_pilot(state_path=state_path)["cleanup_status"] == "removed"
        assert all(not worktree.exists() for worktree in _node_worktrees(tmp_path, state))
        _assert_completed_lifecycle(validator, state_path)

    def test_rejects_shared_path_before_creating_grouped_workers(
        self, lifecycle: ModuleType, tmp_path: Path, repository_templates: RepositoryTemplates
    ) -> None:
        """Require conflicting path ownership to be resolved before grouped plans dispatch."""
        plan_path, approval_path, repository = _overlapping_code_remediate_plan(
            tmp_path, repository_templates, "baseline-a\n"
        )
        _use_grouped_plan(plan_path, approval_path, repository)

        with pytest.raises(lifecycle.PilotError, match=r"^owned-path-overlap:WRITE-B$"):
            _prepare_remediation(lifecycle, tmp_path, plan_path, approval_path)

        assert not (tmp_path / ".codex-rig-worktrees").exists()

    def test_integrates_worker_commits_and_delivers_exact_gated_head(
        self, grouped_remediation: Pilot, draft_commits: tuple[str, str], tmp_path: Path, validator: ModuleType
    ) -> None:
        """Retain both topic commits in order, then fast-forward the attached source to the gated head."""
        lifecycle, state_path, state, repository = grouped_remediation
        baseline = _git(repository, "rev-parse", "HEAD").stdout.strip()
        source_branch = _git(repository, "symbolic-ref", "HEAD").stdout.strip()
        handovers = _create_handovers(lifecycle, state_path, state)
        assert [handover["commit_oid"] for handover in handovers] == list(draft_commits)
        assert handovers[0].keys() == {
            "node_id",
            "status",
            "summary",
            "changed_paths",
            "patch_sha256",
            "commit_oid",
            "verification",
        }
        assert [handover["changed_paths"] for handover in handovers] == [["bucket-a.txt"], ["bucket-b.txt"]]
        lifecycle.join_child_handovers(state_path=state_path, handovers=list(reversed(handovers)))
        collected = _collect_all(lifecycle, state_path, state)
        assert [node["commit_oid"] for node in collected] == list(draft_commits)

        result = lifecycle.integrate_write_pilot(state_path=state_path)

        integration = tmp_path / str(result["integration_worktree"])
        head = _git(integration, "rev-parse", "HEAD").stdout.strip()
        history = _git(integration, "rev-list", "--reverse", f"{baseline}..HEAD").stdout.splitlines()
        assert result["integration_head"] == head
        assert result["integrated_commits"] == history
        assert len(history) == 2
        assert _git(integration, "log", "--reverse", "--format=%s", f"{baseline}..HEAD").stdout.splitlines() == [
            "Edit bucket A",
            "Edit bucket B",
        ]
        assert _git(integration, "symbolic-ref", "-q", "HEAD", check=False).returncode == 1
        assert _git(integration, "status", "--porcelain").stdout == ""
        assert _git(repository, "rev-parse", "HEAD").stdout.strip() == baseline
        assert _read(repository / "bucket-a.txt") == "baseline-a\n"
        assert _read(repository / "bucket-b.txt") == "baseline-b\n"
        with pytest.raises(lifecycle.PilotError, match="gate"):
            lifecycle.apply_code_remediate_source(state_path=state_path)
        assert _git(repository, "rev-parse", "HEAD").stdout.strip() == baseline

        gates = _run_grouped_gates(state_path.parent, integration, head)
        assert not (integration / ".pytest_cache").exists()
        assert not (integration / "__pycache__").exists()
        assert "1 passed" in _read(state_path.parent / "checks" / "tests.stdout.txt")
        accepted = gates.read_bytes()
        stale = read_json(gates)
        assert stale["source"] == {"worktree": integration.as_posix(), "expected_head": head}
        stale["source"]["expected_head"] = baseline
        _write_json(gates, stale)
        with pytest.raises(lifecycle.PilotError, match="gate"):
            lifecycle.apply_code_remediate_source(state_path=state_path, gates_path=gates)
        assert _git(repository, "rev-parse", "HEAD").stdout.strip() == baseline

        unbound = json.loads(accepted)
        tests_check = next(check for check in unbound["checks"] if check["id"] == "tests")
        summary = gates.with_name("gates.checks.jsonl")
        original_summary = summary.read_bytes()

        def write_gate_rows(manifest: dict[str, Any]) -> None:
            """Keep the summary consistent so the grouped check sees the changed evidence."""
            _write_json(gates, manifest)
            _write(summary, "".join(json.dumps(row, sort_keys=True) + "\n" for row in manifest["checks"]))

        del tests_check["source"]["worktree"]
        write_gate_rows(unbound)
        with pytest.raises(lifecycle.PilotError, match=r"^grouped-gate-source-mismatch$"):
            lifecycle.apply_code_remediate_source(state_path=state_path, gates_path=gates)
        assert _git(repository, "rev-parse", "HEAD").stdout.strip() == baseline
        gates.write_bytes(accepted)
        summary.write_bytes(original_summary)

        command_path = state_path.parent / tests_check["command_path"]
        command = command_path.read_bytes()
        command_path.unlink()
        with pytest.raises(lifecycle.PilotError, match=r"^grouped-gate-artifacts-invalid:gate-check-missing-log:"):
            lifecycle.apply_code_remediate_source(state_path=state_path, gates_path=gates)
        assert _git(repository, "rev-parse", "HEAD").stdout.strip() == baseline
        command_path.write_bytes(command)

        stdout = state_path.parent / tests_check["stdout"]
        original_stdout = stdout.read_bytes()
        stdout.write_bytes(b"replacement output\n")
        with pytest.raises(
            lifecycle.PilotError, match=r"^grouped-gate-artifacts-invalid:gate-evidence-digest-mismatch$"
        ):
            lifecycle.apply_code_remediate_source(state_path=state_path, gates_path=gates)
        stdout.write_bytes(original_stdout)
        assert _git(repository, "rev-parse", "HEAD").stdout.strip() == baseline

        rewritten = json.loads(accepted)
        rewritten_test = next(check for check in rewritten["checks"] if check["id"] == "tests")
        command_path.write_bytes(b"git --version\n")
        rewritten_test["source"]["artifacts"][rewritten_test["command_path"]] = _sha256(command_path)
        write_gate_rows(rewritten)
        with pytest.raises(lifecycle.PilotError, match=r"^grouped-gate-tests-command-mismatch$"):
            lifecycle.apply_code_remediate_source(state_path=state_path, gates_path=gates)
        assert _git(repository, "rev-parse", "HEAD").stdout.strip() == baseline
        command_path.write_bytes(command)
        gates.write_bytes(accepted)
        summary.write_bytes(original_summary)

        _git(repository, "switch", "-c", "unrelated-branch")
        with pytest.raises(lifecycle.PilotError, match="branch"):
            lifecycle.apply_code_remediate_source(state_path=state_path, gates_path=gates)
        assert _git(repository, "rev-parse", "HEAD").stdout.strip() == baseline
        _git(repository, "switch", source_branch.removeprefix("refs/heads/"))

        application = lifecycle.apply_code_remediate_source(state_path=state_path, gates_path=gates)

        assert application["status"] == "applied"
        assert read_json(state_path)["source"]["applied_head"] == head
        assert _git(repository, "symbolic-ref", "HEAD").stdout.strip() == source_branch
        assert _git(repository, "rev-parse", "HEAD").stdout.strip() == head
        assert _git(repository, "status", "--porcelain").stdout == ""
        assert _read(repository / "bucket-a.txt") == "child-a\n"
        assert _read(repository / "bucket-b.txt") == "child-b\n"
        cleanup = lifecycle.cleanup_code_remediate_pilot(state_path=state_path)
        assert cleanup["cleanup_status"] == "removed"
        assert cleanup["force"] is False
        assert not integration.exists()
        assert all(not worktree.exists() for worktree in _node_worktrees(tmp_path, state))

        plan = state_path.parent / "work-bucket-plan.json"
        validator._validate_code_remediate_production_lifecycle(
            _lifecycle_reference(state_path), read_json(plan), state_path.parent, _sha256(plan)
        )

    @pytest.mark.parametrize(
        ("relative", "extra_relative", "reason"),
        [
            pytest.param("bucket-a.txt", "bucket-a.txt", "child-commit-parent-mismatch", id="two-commits"),
            pytest.param("bucket-b.txt", None, "child-owned-path-mismatch", id="path-outside-owner"),
            pytest.param("bucket-a.txt", "dirty.txt", "child-untracked-path-forbidden", id="dirty-after-commit"),
        ],
    )
    def test_rejects_invalid_grouped_draft(
        self, grouped_remediation: Pilot, tmp_path: Path, relative: str, extra_relative: str | None, reason: str
    ) -> None:
        """Reject extra history, undeclared paths, and postcommit output before producing a handover."""
        lifecycle, state_path, state, _ = grouped_remediation
        first, _ = _node_worktrees(tmp_path, state)
        _draft_commit(first, relative, "first edit\n", "First topic commit")
        _finish_invalid_grouped_draft(first, extra_relative)
        node_id = str(state["nodes"][0]["node_id"])

        with pytest.raises(lifecycle.PilotError, match=rf"^{reason}:{node_id}$"):
            lifecycle.create_completed_child_handover(state_path=state_path, node_id=node_id, summary="Done")

        assert read_json(state_path)["status"] == "prepared"
        assert first.is_dir()
        assert not (state_path.parent / "patch-a.diff").exists()

    def test_rejects_grouped_draft_without_codex_attribution(
        self, grouped_remediation: Pilot, draft_commits: tuple[str, str], tmp_path: Path
    ) -> None:
        """Reject the worker's exact authorized edit when its draft misses the required attribution."""
        lifecycle, state_path, state, _ = grouped_remediation
        first, _ = _node_worktrees(tmp_path, state)
        _git(first, "commit", "--amend", "-q", "-m", "Unattributed edit")
        assert _git(first, "diff", "--exit-code", draft_commits[0], "HEAD").returncode == 0
        node_id = str(state["nodes"][0]["node_id"])

        with pytest.raises(lifecycle.PilotError, match=rf"^child-commit-attribution-invalid:{node_id}$"):
            lifecycle.create_completed_child_handover(state_path=state_path, node_id=node_id, summary="Done")

    def test_collect_rejects_replaced_commit_even_when_patch_is_identical(
        self, grouped_remediation: Pilot, draft_commits: tuple[str, str], tmp_path: Path
    ) -> None:
        """After joins, reject a replacement commit despite an unchanged parent and tree."""
        lifecycle, state_path, state, _ = grouped_remediation
        first, _ = _node_worktrees(tmp_path, state)
        handovers = _create_handovers(lifecycle, state_path, state)
        lifecycle.join_child_handovers(state_path=state_path, handovers=handovers)
        _git(
            first,
            "commit",
            "--amend",
            "-q",
            "-m",
            "Different commit message",
            "-m",
            "Co-authored-by: Codex <codex@openai.com>",
        )
        assert _git(first, "rev-parse", "HEAD").stdout.strip() != draft_commits[0]
        assert _git(first, "diff", "--exit-code", draft_commits[0], "HEAD").returncode == 0
        node_id = str(state["nodes"][0]["node_id"])

        with pytest.raises(lifecycle.PilotError, match=node_id):
            lifecycle.collect_write_patch(state_path=state_path, node_id=node_id)

        assert not (state_path.parent / "patch-a.diff").exists()

    def test_join_rejects_report_for_another_commit(
        self, grouped_remediation: Pilot, draft_commits: tuple[str, str]
    ) -> None:
        """Reject a reported commit ID that differs from the worker's actual head despite a valid patch hash."""
        lifecycle, state_path, state, _ = grouped_remediation
        handovers = _create_handovers(lifecycle, state_path, state)
        handovers[0]["commit_oid"] = draft_commits[1]

        with pytest.raises(lifecycle.PilotError, match=str(state["nodes"][0]["node_id"])):
            lifecycle.join_child_handovers(state_path=state_path, handovers=handovers)

        assert not (state_path.parent / "patch-a.diff").exists()

    def test_patch_plan_still_rejects_worker_commit(
        self, lifecycle: ModuleType, tmp_path: Path, repository_templates: RepositoryTemplates
    ) -> None:
        """Keep the schema-2 remediation plan patch-only even when the child commits its exact owned edit."""
        plan_path, approval_path, _ = _approved_code_remediate_plan(tmp_path, repository_templates)
        state_path = plan_path.parent / "production-lifecycle.json"
        state = _prepare_remediation(lifecycle, tmp_path, plan_path, approval_path)
        first, _ = _node_worktrees(tmp_path, state)
        _draft_commit(first, "bucket-a.txt", "first edit\n", "Bucket A")
        node_id = str(state["nodes"][0]["node_id"])

        with pytest.raises(lifecycle.PilotError, match=rf"^child-commit-forbidden:{node_id}$"):
            lifecycle.create_completed_child_handover(state_path=state_path, node_id=node_id, summary="Done")


#: Plan inputs a grouped run needs beside its frozen plan; everything else in a run directory is the run's own output.
_GROUPED_PLAN_INPUTS = (
    "work-bucket-plan.json",
    "parallel-approval.json",
    "WRITE-A-context.md",
    "WRITE-B-context.md",
    "resolution-workplan.md",
)


def _second_grouped_run(first: GatedGroupedRemediation, workspace: Path) -> GatedGroupedRemediation:
    """Prepare, draft, integrate and gate a second grouped run on the first run's source checkout and baseline.

    Its plan is a copy of the first run's with its own run directory, state file and worktree root. Its commits carry
    the same edits under other subjects, so they are distinct commits that still pass the fixture's full test suite.
    """
    lifecycle, first_state_path, _, repository = first.pilot
    run = first_state_path.parent.with_name(f"{first_state_path.parent.name}-second")
    run.mkdir()
    for name in _GROUPED_PLAN_INPUTS:
        shutil.copyfile(first_state_path.parent / name, run / name)
    plan_path, approval_path = run / "work-bucket-plan.json", run / "parallel-approval.json"
    plan = read_json(plan_path)
    plan["worktree_root"] = f"{plan['worktree_root']}-second"
    _rewrite_plan(plan_path, approval_path, plan)
    state_path = run / "production-lifecycle.json"
    state = _prepare_remediation(lifecycle, workspace, plan_path, approval_path)
    for worktree, bucket, contents in zip(_node_worktrees(workspace, state), ("a", "b"), CHILD_EDITS, strict=True):
        _draft_commit(worktree, f"bucket-{bucket}.txt", contents, f"Edit bucket {bucket.upper()} in the second run")
    lifecycle.join_child_handovers(state_path=state_path, handovers=_create_handovers(lifecycle, state_path, state))
    _collect_all(lifecycle, state_path, state)
    result = lifecycle.integrate_write_pilot(state_path=state_path)
    integration = workspace / str(result["integration_worktree"])
    head = str(result["integration_head"])
    gates = _run_grouped_gates(run, integration, head)
    return GatedGroupedRemediation(
        Pilot(lifecycle, state_path, state, repository), integration, first.baseline, head, gates
    )


def _land_first_run(first: GatedGroupedRemediation) -> None:
    """Deliver the first run to the shared source branch."""
    lifecycle, state_path, _, _ = first.pilot
    assert lifecycle.apply_code_remediate_source(state_path=state_path, gates_path=first.gates)["status"] == "applied"


def _land_first_run_after_intent(
    first: GatedGroupedRemediation, second: GatedGroupedRemediation, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deliver the first run right after the second one saved its ``source-applying`` intent, before its preflight."""
    lifecycle = second.pilot.lifecycle
    real_save = lifecycle._save_delivery_intent
    fired: list[bool] = []

    def _save_then_first_lands(*arguments: Any) -> Path:
        """Save the second run's intent, then let the first run land once; the first run's own save runs unchanged."""
        patch_path = real_save(*arguments)
        if not fired:
            fired.append(True)
            _land_first_run(first)
        return patch_path

    monkeypatch.setattr(lifecycle, "_save_delivery_intent", _save_then_first_lands)


def _land_first_run_before_fast_forward(
    first: GatedGroupedRemediation, second: GatedGroupedRemediation, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deliver the first run right before the second one's fast-forward, after its preflight passed."""
    lifecycle, _, _, repository = second.pilot
    real_git = lifecycle._git
    fired: list[bool] = []

    def _first_lands_then_git(target: Path, *arguments: str) -> str:
        """Let the first run land once, right before the second run's own fast-forward runs."""
        if not fired and target == repository and arguments[:1] == ("merge",) and second.head in arguments:
            fired.append(True)
            _land_first_run(first)
        return real_git(target, *arguments)

    monkeypatch.setattr(lifecycle, "_git", _first_lands_then_git)


@pytest.mark.installed_plugin
@pytest.mark.integration
class TestConcurrentRunsOnOneSource:
    """Two runs on one source repository are unsupported: the apply lock is per run, and the later run never lands."""

    @pytest.mark.parametrize(
        ("first_lands", "error", "recorded"),
        [
            pytest.param(None, "source-repository-head-drift", None, id="before-the-second-apply"),
            pytest.param(
                _land_first_run_after_intent,
                "source-repository-head-drift",
                ("integrated", "preflight-failed"),
                id="after-the-second-run-saved-its-intent",
            ),
            pytest.param(
                _land_first_run_before_fast_forward,
                "git-command-failed:merge",
                ("failed", "failed"),
                id="during-the-second-run-fast-forward",
            ),
        ],
    )
    def test_the_later_run_stops_without_landing_anything(
        self,
        gated_grouped_remediation: GatedGroupedRemediation,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        first_lands: Callable[[GatedGroupedRemediation, GatedGroupedRemediation, pytest.MonkeyPatch], None] | None,
        error: str,
        recorded: tuple[str, str] | None,
    ) -> None:
        """Stop the second run with the first run's delivery intact and none of its own commits on the branch.

        Each run takes only its own lock, so nothing serializes the two. A second apply after the first landed stops
        before it saves or writes anything, one whose intent was already saved reopens as ``preflight-failed``, and one
        whose fast-forward the first run's beat is saved ``failed`` with the head it found.
        """
        first = gated_grouped_remediation
        second = _second_grouped_run(first, tmp_path)
        lifecycle, state_path, _, repository = second.pilot
        if first_lands is None:
            _land_first_run(first)
        else:
            first_lands(first, second, monkeypatch)
        state_bytes = state_path.read_bytes()

        with pytest.raises(lifecycle.PilotError, match=f"^{error}$"):
            lifecycle.apply_code_remediate_source(state_path=state_path, gates_path=second.gates)
        monkeypatch.undo()

        if recorded is None:
            assert state_path.read_bytes() == state_bytes
        else:
            assert _apply_statuses(state_path) == recorded
        if recorded == ("failed", "failed"):
            assert read_json(state_path)["source_application"]["observed_head"] == first.head
        assert _git(repository, "rev-parse", "HEAD").stdout.strip() == first.head
        assert _git(repository, "merge-base", "--is-ancestor", second.head, "HEAD", check=False).returncode == 1
        assert _git(repository, "status", "--porcelain").stdout == ""
        assert _apply_statuses(first.pilot.state_path) == ("source-applied", "applied")


def _finish_invalid_grouped_draft(worktree: Path, extra_relative: str | None) -> None:
    """Add the selected postcommit mutation after the first fixture draft."""
    if extra_relative == "bucket-a.txt":
        _draft_commit(worktree, extra_relative, "second edit\n", "Second topic commit")
    elif extra_relative is not None:
        _write(worktree / extra_relative, "untracked output\n")


# --- generated-fixture pilot: collection ----------------------------------------------------------------------------


def _commit_owned_edit(worktree: Path) -> None:
    """Hide an owned edit in a child commit so the worktree looks clean."""
    _write(worktree / "bucket-a.txt", "child-a\n")
    _git(worktree, "add", "bucket-a.txt")
    _git(worktree, "commit", "-q", "-m", "forbidden child commit")


def _edit_unowned_file(worktree: Path) -> None:
    """Edit a tracked file that belongs to the other child."""
    _write(worktree / "bucket-b.txt", "forbidden\n")


def _create_untracked_file(worktree: Path) -> None:
    """Create a file that no bucket owns."""
    _write(worktree / "new.txt", "forbidden\n")


def _stage_child_owned_edit(worktree: Path) -> None:
    """Stage a valid owned edit, bypassing the patch-only index boundary."""
    _write(worktree / "bucket-a.txt", "staged\n")
    _git(worktree, "add", "bucket-a.txt")


def _delete_owned_file(worktree: Path) -> None:
    """Delete an owned file, which a content patch cannot express."""
    (worktree / "bucket-a.txt").unlink()


@pytest.mark.installed_plugin
@pytest.mark.integration
class TestCollect:
    """The parent collects only exact, owned, content-only child changes."""

    @pytest.mark.parametrize(
        ("damage_child", "error"),
        [
            pytest.param(_commit_owned_edit, "child-commit-forbidden", id="child-commit"),
            pytest.param(_edit_unowned_file, "child-owned-path-mismatch", id="outside-owned"),
            pytest.param(_create_untracked_file, "child-untracked-path-forbidden", id="untracked"),
            pytest.param(_stage_child_owned_edit, "child-index-change-forbidden", id="staged-owned"),
            pytest.param(_delete_owned_file, "child-patch-shape-forbidden", id="deletion"),
        ],
    )
    def test_join_rejects_forbidden_child_change(
        self,
        pilot: Pilot,
        tmp_path: Path,
        damage_child: Callable[[Path], None],
        error: str,
    ) -> None:
        """Prevent a child from smuggling commits, undeclared files, staged edits or deletions into a patch.

        The first child alters its worktree in a way the patch-only contract forbids while the second child behaves; the
        join must name the first child in the error, write no patch and retain the worktree.
        """
        lifecycle, state_path, state, _ = pilot
        worktree = _node_worktrees(tmp_path, state)[0]
        damage_child(worktree)
        _write_other_children(tmp_path, state, "FIXTURE-WRITE-A")

        with pytest.raises(lifecycle.PilotError, match=rf"^{error}:FIXTURE-WRITE-A$"):
            _join(lifecycle, state_path, state, tmp_path)

        assert worktree.exists()
        assert not (state_path.parent / "patch-a.diff").exists()

    def test_mode_only_change_rejected_without_filemode_capability(
        self, pilot: Pilot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reject a Git-reported mode-only patch without relying on chmod tracking.

        Git is made to report a pure mode change (100644 to 100755) for an owned file, so the shape check is exercised
        on hosts where chmod is not tracked.
        """
        lifecycle, _, state, _ = pilot
        node = state["nodes"][0]
        worktree = _node_worktrees(tmp_path, state)[0]
        real_git_bytes = lifecycle._git_bytes

        def _mode_only_raw(target: Path, *arguments: str) -> bytes:
            """Return mode-only raw diff evidence for patch-shape rejection."""
            if target == worktree and arguments[:3] == ("diff", "--raw", "--no-renames"):
                return b":100644 100755 deadbeef deadbeef M\0bucket-a.txt\0"
            return real_git_bytes(target, *arguments)

        monkeypatch.setattr(lifecycle, "_git_bytes", _mode_only_raw)

        with pytest.raises(lifecycle.PilotError, match=r"^child-patch-shape-forbidden:FIXTURE-WRITE-A$"):
            lifecycle._raw_content_updates(worktree, node)

    def test_generated_pilot_collect_resumes_after_patch_write(
        self, joined_pilot: Pilot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Adopt an identical crash-left patch through the generated-fixture collector."""
        lifecycle, state_path, _, _ = joined_pilot
        node_id, patch_path, patch = _crash_after_patch_output(joined_pilot, monkeypatch)
        written_at = patch_path.stat().st_mtime_ns

        collected = lifecycle.collect_write_patch(state_path=state_path, node_id=node_id)

        assert collected["patch_status"] == "collected"
        assert collected["patch_sha256"] == hashlib.sha256(patch).hexdigest()
        assert patch_path.stat().st_mtime_ns == written_at

    def test_rejects_worktree_drift_after_parent_join(self, joined_pilot: Pilot, tmp_path: Path) -> None:
        """Recheck the reported patch hash immediately before parent collection.

        A child edits its owned file again after the parent joined its handover; collection must notice the patch no
        longer matches the reported hash and write nothing.
        """
        lifecycle, state_path, state, _ = joined_pilot
        _write(_node_worktrees(tmp_path, state)[0] / "bucket-a.txt", "drifted-after-join\n")

        with pytest.raises(lifecycle.PilotError, match=r"^child-handover-drift:FIXTURE-WRITE-A$"):
            lifecycle.collect_write_patch(state_path=state_path, node_id="FIXTURE-WRITE-A")

        assert not (state_path.parent / "patch-a.diff").exists()

    def test_rejects_fabricated_operation_api(self, pilot: Pilot, tmp_path: Path) -> None:
        """Keep caller-created operation dictionaries outside the collector API.

        The collector derives its own Git evidence, so passing a caller-built ``operation`` must be a ``TypeError`` for
        an unknown keyword rather than being trusted.
        """
        lifecycle, state_path, state, _ = pilot
        _write(_node_worktrees(tmp_path, state)[0] / "bucket-a.txt", "child-a\n")

        with pytest.raises(TypeError, match="unexpected keyword argument 'operation'"):
            lifecycle.collect_write_patch(state_path=state_path, node_id="FIXTURE-WRITE-A", operation={})

    def test_prior_attempt_fingerprint_blocks_retained_worktree_mutation(
        self, lifecycle: ModuleType, tmp_path: Path, repository_templates: RepositoryTemplates
    ) -> None:
        """Freeze retained diagnostics and reject their mutation before current patch collection.

        A failed earlier attempt left detached worktrees that the plan records by fingerprint. One of them is edited
        after preparation, so joining the current attempt must stop and write no patch.
        """
        plan_path, approval_path, repository = _approved_plan(tmp_path, repository_templates)
        prior_root = tmp_path / ".reports" / "codex" / "develop" / "prior" / "worktrees"
        prior_root.mkdir(parents=True)
        _git(repository, "worktree", "add", "--detach", str(prior_root / "FIXTURE-WRITE-A"), "HEAD")
        _git(repository, "worktree", "add", "--detach", str(prior_root / "FIXTURE-WRITE-B"), "HEAD")
        runtime = tmp_path / ".reports" / "codex" / "develop" / "prior" / "runtime.json"
        _write(runtime, '{"status":"failed"}\n')
        plan = read_json(plan_path)
        plan["prior_attempt"] = {
            "runtime_record": runtime.relative_to(tmp_path).as_posix(),
            "status": "failed-retained",
            "retry_count": 0,
            "retained_worktree_root": prior_root.relative_to(tmp_path).as_posix(),
        }
        _rewrite_plan(plan_path, approval_path, plan)
        state_path = tmp_path / ".reports" / "codex" / "develop" / "fixture" / "lifecycle.json"
        state = lifecycle.prepare_write_pilot(
            plan_path=plan_path, approval_path=approval_path, workspace_root=tmp_path, state_path=state_path
        )
        _write(prior_root / "FIXTURE-WRITE-B" / "bucket-b.txt", "mutated\n")
        _write(_node_worktrees(tmp_path, state)[0] / "bucket-a.txt", "child-a\n")
        _write_other_children(tmp_path, state, "FIXTURE-WRITE-A")

        with pytest.raises(lifecycle.PilotError, match=r"^prior-attempt-fingerprint-mismatch$"):
            _join(lifecycle, state_path, state, tmp_path)

        assert not (state_path.parent / "patch-a.diff").exists()

    @mock.patch(
        "subprocess.run", autospec=True, side_effect=AssertionError("Git ran before state authority validation")
    )
    def test_state_authority_tamper_fails_before_git(self, mock_run: mock.Mock, pilot: Pilot) -> None:
        """Rederive managed roots before executing Git from mutable lifecycle state.

        The persisted repository path is redirected; collection must raise the root-drift error without starting any
        subprocess, so a tampered state file can never steer a Git command.
        """
        lifecycle, state_path, _, _ = pilot
        persisted = read_json(state_path)
        persisted["repository"] = ".reports/codex/develop/redirected"
        _write_json(state_path, persisted)

        with pytest.raises(lifecycle.PilotError, match=r"^lifecycle-state-root-drift$"):
            lifecycle.collect_write_patch(state_path=state_path, node_id="FIXTURE-WRITE-A")

        mock_run.assert_not_called()


# --- generated-fixture pilot: integration and cleanup ---------------------------------------------------------------


def _drift_approval(workspace: Path, state: dict[str, object], repository: Path) -> None:
    """Change the approval file bytes after the join."""
    approval = workspace / str(state["approval_path"])
    _write(approval, _read(approval) + "\n")


def _drift_source(workspace: Path, state: dict[str, object], repository: Path) -> None:
    """Dirty an approved path of the generated source repository after the join."""
    _write(repository / "bucket-a.txt", "source-drift\n")


@pytest.mark.installed_plugin
@pytest.mark.integration
class TestIntegrateAndCleanup:
    """Integration applies verified patches to an isolated worktree; cleanup removes only durable successes."""

    def test_requires_every_completed_joined_patch(self, pilot: Pilot) -> None:
        """Prevent integration before both child reports and patches are verified.

        A freshly prepared pilot has no joined children, so integration must refuse and leave the source untouched.
        """
        lifecycle, state_path, _, repository = pilot

        with pytest.raises(lifecycle.PilotError, match=r"^pilot-children-not-joined$"):
            lifecycle.integrate_write_pilot(state_path=state_path)

        assert _read(repository / "bucket-a.txt") == "baseline-a\n"

    def test_applies_patches_in_lexical_order(self, joined_pilot: Pilot, tmp_path: Path) -> None:
        """Apply both hash-bound patches to an isolated integration worktree in stable order.

        After collection each patch file must match its recorded digest; integration then applies A before B into its
        own worktree and the source repository keeps its baseline bytes.
        """
        lifecycle, state_path, state, repository = joined_pilot
        patches = _collect_all(lifecycle, state_path, state)

        result = lifecycle.integrate_write_pilot(state_path=state_path)

        integration = tmp_path / str(result["integration_worktree"])
        assert [_sha256(tmp_path / str(patch["patch_path"])) for patch in patches] == [
            patch["patch_sha256"] for patch in patches
        ]
        assert result["integration_order"] == ["FIXTURE-WRITE-A", "FIXTURE-WRITE-B"]
        assert _read(integration / "bucket-a.txt") == "child-a\n"
        assert _read(integration / "bucket-b.txt") == "child-b\n"
        assert _read(repository / "bucket-a.txt") == "baseline-a\n"

    def test_plan_drift_after_join_blocks_integration(self, collected_pilot: Pilot, tmp_path: Path) -> None:
        """Rehash immutable authority after joins before applying any child patch, and retain the worktrees.

        The approved plan gains a byte after patches were collected; integration must fail on the digest and leave all
        child worktrees in place.
        """
        lifecycle, state_path, state, _ = collected_pilot
        plan_path = tmp_path / str(state["plan_path"])
        _write(plan_path, _read(plan_path) + "\n")

        with pytest.raises(lifecycle.PilotError, match=r"^plan-approval-digest-mismatch$"):
            lifecycle.integrate_write_pilot(state_path=state_path)

        assert all(worktree.exists() for worktree in _node_worktrees(tmp_path, state))

    @pytest.mark.parametrize(
        ("drift", "expected"),
        [
            pytest.param(_drift_approval, "approval-drift-before-transition", id="approval"),
            pytest.param(_drift_source, "fixture-repository-dirty", id="source"),
        ],
    )
    def test_authority_or_source_drift_blocks_transition(
        self,
        joined_pilot: Pilot,
        tmp_path: Path,
        drift: Callable[[Path, dict[str, object], Path], None],
        expected: str,
    ) -> None:
        """Reject approval-byte or generated-source drift before another Git transition.

        After the join, either the approval file changes or the generated source repository becomes dirty; collecting
        the next patch must fail with the matching error and write nothing.
        """
        lifecycle, state_path, state, repository = joined_pilot
        drift(tmp_path, state, repository)

        with pytest.raises(lifecycle.PilotError, match=rf"^{expected}$"):
            lifecycle.collect_write_patch(state_path=state_path, node_id="FIXTURE-WRITE-A")

        assert not (state_path.parent / "patch-a.diff").exists()

    def test_patch_digest_tamper_blocks_integration(self, collected_pilot: Pilot, tmp_path: Path) -> None:
        """Reject changed patch bytes before creating an integration worktree.

        A newline is appended to the collected patch for the first child, so its digest no longer matches the state.
        """
        lifecycle, state_path, state, _ = collected_pilot
        patch = state_path.parent / "patch-a.diff"
        patch.write_bytes(patch.read_bytes() + b"\n")

        with pytest.raises(lifecycle.PilotError, match=r"^patch-digest-mismatch:FIXTURE-WRITE-A$"):
            lifecycle.integrate_write_pilot(state_path=state_path)

        assert not (tmp_path / str(state["worktree_root"]) / "integration").exists()

    def test_conflicting_patch_retains_failed_integration(self, collected_pilot: Pilot, tmp_path: Path) -> None:
        """Retain the integration worktree when a hash-bound later patch conflicts.

        The second child's patch is replaced by a copy of the first (with its recorded digest updated), so applying it
        conflicts; the failed integration worktree and all children must stay for inspection.
        """
        lifecycle, state_path, _, repository = collected_pilot
        persisted = read_json(state_path)
        patch_b = state_path.parent / "patch-b.diff"
        patch_b.write_bytes((state_path.parent / "patch-a.diff").read_bytes())
        persisted["nodes"][1]["patch_sha256"] = _sha256(patch_b)
        _write_json(state_path, persisted)

        with pytest.raises(lifecycle.PilotError, match=r"^patch-integration-failed:FIXTURE-WRITE-B$"):
            lifecycle.integrate_write_pilot(state_path=state_path)

        retained = read_json(state_path)
        assert retained["integration_status"] == "failed"
        assert (tmp_path / str(retained["integration_worktree"])).exists()
        assert all(worktree.exists() for worktree in _node_worktrees(tmp_path, retained))
        assert _read(repository / "bucket-a.txt") == "baseline-a\n"

    def test_cleanup_removes_only_persisted_successful_worktrees(self, collected_pilot: Pilot, tmp_path: Path) -> None:
        """Remove generated worktrees only after integration evidence is durable.

        After a successful integration, cleanup removes both child worktrees and the integration worktree and records
        each as absent.
        """
        lifecycle, state_path, state, _ = collected_pilot
        result = lifecycle.integrate_write_pilot(state_path=state_path)

        cleanup = lifecycle.cleanup_write_pilot(state_path=state_path)

        paths = [*_node_worktrees(tmp_path, state), tmp_path / str(result["integration_worktree"])]
        persisted = read_json(state_path)
        assert cleanup["cleanup_status"] == "removed"
        assert all(not path.exists() for path in paths)
        assert persisted["cleanup_status"] == "removed"
        assert all(item["postcondition"] == "absent" for item in persisted["cleanup"])

    def test_cleanup_failure_is_retained_and_blocks_success(
        self, collected_pilot: Pilot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Record non-force cleanup failure and retain every remaining diagnostic path.

        Git refuses to remove the first worktree (a lock, for example); cleanup must report the failing node, persist
        ``failed`` and leave that worktree in place instead of forcing removal.
        """
        lifecycle, state_path, state, _ = collected_pilot
        lifecycle.integrate_write_pilot(state_path=state_path)
        real_git = lifecycle._git

        def _fail_remove(repository: Path, *arguments: str) -> str:
            """Inject cleanup failure at worktree removal and delegate other Git commands."""
            if arguments[:2] == ("worktree", "remove"):
                raise lifecycle.PilotError("simulated-cleanup-lock")
            return real_git(repository, *arguments)

        monkeypatch.setattr(lifecycle, "_git", _fail_remove)
        with pytest.raises(lifecycle.PilotError, match=r"^cleanup-failed:FIXTURE-WRITE-A$"):
            lifecycle.cleanup_write_pilot(state_path=state_path)

        assert read_json(state_path)["cleanup_status"] == "failed"
        assert _node_worktrees(tmp_path, state)[0].exists()


# --- Git helper behaviour -------------------------------------------------------------------------------------------


def _generated_route(
    lifecycle: ModuleType, workspace: Path, repository_templates: RepositoryTemplates
) -> tuple[Path, list[dict[str, object]], Callable[[], list[object]], str]:
    """Build the generated-pilot route: repository, mutable buckets, a node validator and the second bucket's id."""
    plan_path, _, repository = _approved_plan(workspace, repository_templates)
    plan = read_json(plan_path)
    validate = functools.partial(lifecycle._validated_nodes, plan["stages"][0], repository)
    return repository, plan["stages"][0]["nodes"], validate, "FIXTURE-WRITE-B"


def _remediation_route(
    lifecycle: ModuleType, workspace: Path, repository_templates: RepositoryTemplates
) -> tuple[Path, list[dict[str, object]], Callable[[], list[object]], str]:
    """Build the code-remediate route: repository, mutable buckets, a node validator and the second bucket's id."""
    plan_path, _, repository = _approved_code_remediate_plan(workspace, repository_templates)
    plan = read_json(plan_path)
    evidence = plan_path.parent.relative_to(repository).as_posix()
    validate = functools.partial(lifecycle._code_remediate_nodes, plan, repository, evidence)
    return repository, plan["work_buckets"], validate, "WRITE-B"


def _keep_first_path(repository: Path) -> str:
    """Leave the first bucket's file where it is."""
    return "bucket-a.txt"


def _rename_first_path(repository: Path) -> str:
    """Move the first bucket's file to a name with spaces and a non-ASCII letter."""
    _git(repository, "mv", "bucket-a.txt", "owned path é.txt")
    return "owned path é.txt"


def _oversized_posix_batch() -> OSError:
    """Build the error POSIX raises when a command line is too long."""
    return OSError(errno.E2BIG, "probe")


def _oversized_windows_batch() -> OSError:
    """Build the error Windows raises when a command line is too long (``winerror`` 206)."""
    error = OSError(errno.EACCES, "probe")
    error.winerror = 206
    return error


def _fail_batched_launches(
    lifecycle: ModuleType, monkeypatch: pytest.MonkeyPatch, error: OSError
) -> list[tuple[str, ...]]:
    """Raise ``error`` for every multi-path Git launch, run single-path ones for real, and record all calls."""
    real_git = lifecycle._git
    calls: list[tuple[str, ...]] = []

    def _batch_limit(target: Path, *arguments: str) -> str:
        """Simulate only batch launch failure; individual checks execute Git."""
        calls.append(arguments)
        if len(arguments) > 4:
            raise error
        return real_git(target, *arguments)

    monkeypatch.setattr(lifecycle, "_git", _batch_limit)
    return calls


@pytest.mark.installed_plugin
@pytest.mark.integration
class TestGitHelpers:
    """The lifecycle's Git wrappers sanitize their environment and batch their queries without changing answers."""

    @mock.patch(
        "subprocess.run",
        autospec=True,
        return_value=subprocess.CompletedProcess(args=["git"], returncode=0, stdout="head\n", stderr=""),
    )
    def test_runner_sanitizes_git_environment_without_shell(
        self, mock_run: mock.Mock, lifecycle: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Remove Git overrides while preserving Windows-required child variables.

        ``GIT_*`` overrides (in any letter case) are dropped from the child environment, ``SYSTEMROOT`` and ``COMSPEC``
        survive because Windows children need them, and no shell is involved.
        """
        monkeypatch.setenv("GIT_DIR", "redirected")
        monkeypatch.setenv("git_work_tree", "redirected")
        monkeypatch.setenv("SYSTEMROOT", "C:\\Windows")
        monkeypatch.setenv("COMSPEC", "cmd.exe")

        lifecycle._git(tmp_path, "rev-parse", "HEAD")

        assert mock_run.called
        launch = mock_run.call_args_list[0].kwargs
        environment = launch["env"]
        assert isinstance(environment, dict)
        assert not any(key.upper().startswith("GIT_") for key in environment)
        assert environment["SYSTEMROOT"] == "C:\\Windows"
        assert environment["COMSPEC"] == "cmd.exe"
        assert launch["shell"] is False
        assert os.environ is not environment

    @pytest.mark.parametrize(
        ("creator", "name", "expected"),
        [
            pytest.param("touch", "MERGE_HEAD", "merging", id="merge"),
            pytest.param("touch", "CHERRY_PICK_HEAD", "cherry-picking", id="cherry-pick"),
            pytest.param("touch", "REVERT_HEAD", "reverting", id="revert"),
            pytest.param("mkdir", "rebase-merge", "rebasing", id="rebase-merge"),
            pytest.param("mkdir", "rebase-apply", "rebasing", id="rebase-apply"),
            pytest.param("touch", "UNRELATED_MARKER", None, id="idle"),
        ],
    )
    def test_pending_operation_is_read_with_one_git_process(
        self,
        lifecycle: ModuleType,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        creator: str,
        name: str,
        expected: str | None,
    ) -> None:
        """Report each pending operation by its own label from a single marker-path query.

        A repository gains exactly one operation marker; the batched answer must map every line back to the marker it
        was asked for, so swapping two answers would report the wrong operation or none.
        """
        _git(tmp_path, "init", "-q")
        getattr(tmp_path / ".git" / name, creator)()
        calls = _record_lifecycle_git(lifecycle, monkeypatch)

        assert lifecycle._git_operation(tmp_path) == expected
        assert calls == [
            (
                "rev-parse",
                "--git-path",
                "MERGE_HEAD",
                "--git-path",
                "CHERRY_PICK_HEAD",
                "--git-path",
                "REVERT_HEAD",
                "--git-dir",
            )
        ]

    def test_pending_operation_uses_the_linked_worktree_git_directory(
        self,
        lifecycle: ModuleType,
        tmp_path: Path,
        repository_templates: RepositoryTemplates,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Find a marker in a linked worktree's private Git directory and not in the main checkout.

        A linked worktree keeps MERGE_HEAD beside its own HEAD under the shared Git directory, where Git answers with an
        absolute path; the main checkout must stay idle while the linked one reports the merge.
        """
        repository = _fixture_repository(tmp_path, repository_templates)
        linked = tmp_path / "linked"
        _git(repository, "worktree", "add", "--detach", str(linked))
        Path(_git(linked, "rev-parse", "--git-path", "MERGE_HEAD").stdout.strip()).touch()
        calls = _record_lifecycle_git(lifecycle, monkeypatch)

        assert (lifecycle._git_operation(linked), lifecycle._git_operation(repository)) == ("merging", None)
        assert len(calls) == 2

    def test_pending_operation_falls_back_to_one_query_per_marker(
        self, lifecycle: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fall back to the unbatched queries, with the same answer, when the batched query is refused.

        A refused or ambiguous batched answer must never change the result: each marker and the Git directory are asked
        on their own, in reporting order, stopping at the first marker that exists.
        """
        _git(tmp_path, "init", "-q")
        (tmp_path / ".git" / "CHERRY_PICK_HEAD").touch()
        calls = _record_lifecycle_git(lifecycle, monkeypatch, refuse_batches=True)

        assert lifecycle._git_operation(tmp_path) == "cherry-picking"
        assert calls[1:] == [
            ("rev-parse", "--git-path", "MERGE_HEAD"),
            ("rev-parse", "--git-path", "CHERRY_PICK_HEAD"),
        ]

    def test_head_and_tree_are_read_with_one_git_process(
        self,
        lifecycle: ModuleType,
        tmp_path: Path,
        repository_templates: RepositoryTemplates,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Return the commit and tree identifiers a pair of single queries would, from one process.

        The combined query must give exactly the values two separate ``rev-parse`` calls give, using one Git invocation.
        """
        repository = _fixture_repository(tmp_path, repository_templates)
        expected = (
            _git(repository, "rev-parse", "HEAD").stdout.strip(),
            _git(repository, "rev-parse", "HEAD^{tree}").stdout.strip(),
        )
        calls = _record_lifecycle_git(lifecycle, monkeypatch)

        assert lifecycle._git_head_and_tree(repository) == expected
        assert calls == [("rev-parse", "HEAD", "HEAD^{tree}")]

    def test_head_and_tree_of_unborn_branch_keep_the_single_query_error(
        self, lifecycle: ModuleType, tmp_path: Path
    ) -> None:
        """Raise the same git-command-failed code a lone ``rev-parse HEAD`` raises when no commit exists.

        A repository with no commit has no HEAD; batching the two queries must not change the error callers see.
        """
        _git(tmp_path, "init", "-q")

        with pytest.raises(lifecycle.PilotError, match=r"^git-command-failed:rev-parse$"):
            lifecycle._git_head_and_tree(tmp_path)

    @pytest.mark.parametrize(
        "build_route",
        [pytest.param(_generated_route, id="generated"), pytest.param(_remediation_route, id="remediation")],
    )
    @pytest.mark.parametrize(
        "rename_first",
        [pytest.param(_keep_first_path, id="plain-path"), pytest.param(_rename_first_path, id="unicode-path")],
    )
    def test_owned_paths_batch_tracking_checks_and_recheck_index_drift(
        self,
        lifecycle: ModuleType,
        tmp_path: Path,
        repository_templates: RepositoryTemplates,
        monkeypatch: pytest.MonkeyPatch,
        build_route: Callable[..., tuple[Path, list[dict[str, object]], Callable[[], list[object]], str]],
        rename_first: Callable[[Path], str],
    ) -> None:
        """Batch real tracked-path reads without accepting a later untracked owned file.

        Both plan routes check every owned path with one ``ls-files --error-unmatch`` call (also for a path with spaces
        and a non-ASCII letter); removing a path from the index afterwards must make the next validation fail.
        """
        repository, buckets, validate, owner = build_route(lifecycle, tmp_path, repository_templates)
        first_path = rename_first(repository)
        buckets[0]["owned_paths"] = [first_path]
        calls = _record_lifecycle_git(lifecycle, monkeypatch)

        assert len(validate()) == 2
        assert calls == [("ls-files", "--error-unmatch", "--", first_path, "bucket-b.txt")]

        _git(repository, "rm", "--cached", "bucket-b.txt")
        with pytest.raises(lifecycle.PilotError, match=rf"^owned-path-not-tracked:{owner}$"):
            validate()

    @pytest.mark.parametrize(
        "make_error",
        [
            pytest.param(_oversized_posix_batch, id="posix-argv-limit"),
            pytest.param(_oversized_windows_batch, id="windows-argv-limit"),
        ],
    )
    def test_batch_launch_limit_falls_back_to_per_path_checks(
        self,
        lifecycle: ModuleType,
        tmp_path: Path,
        repository_templates: RepositoryTemplates,
        monkeypatch: pytest.MonkeyPatch,
        make_error: Callable[[], OSError],
    ) -> None:
        """Fall back to real per-path Git checks when an oversized batch cannot be launched.

        The batch of both owned paths hits the OS command-line limit (POSIX ``E2BIG`` or Windows error 206), so each
        path is checked on its own and validation still succeeds.
        """
        plan_path, _, repository = _approved_plan(tmp_path, repository_templates)
        stage = read_json(plan_path)["stages"][0]
        calls = _fail_batched_launches(lifecycle, monkeypatch, make_error())

        nodes = lifecycle._validated_nodes(stage, repository)

        assert len(nodes) == 2
        assert calls == [
            ("ls-files", "--error-unmatch", "--", "bucket-a.txt", "bucket-b.txt"),
            ("ls-files", "--error-unmatch", "--", "bucket-a.txt"),
            ("ls-files", "--error-unmatch", "--", "bucket-b.txt"),
        ]

    def test_unrelated_os_error_from_batch_launch_propagates(
        self,
        lifecycle: ModuleType,
        tmp_path: Path,
        repository_templates: RepositoryTemplates,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Let an OS error that is not an argument-limit failure escape instead of being retried.

        A permission error while launching the batch is not recoverable by splitting the batch, so it must reach the
        caller unchanged after exactly one attempt.
        """
        plan_path, _, repository = _approved_plan(tmp_path, repository_templates)
        stage = read_json(plan_path)["stages"][0]
        error = OSError(errno.EACCES, "probe")
        calls = _fail_batched_launches(lifecycle, monkeypatch, error)

        with pytest.raises(OSError, match="probe") as raised:
            lifecycle._validated_nodes(stage, repository)

        assert raised.value is error
        assert len(calls) == 1

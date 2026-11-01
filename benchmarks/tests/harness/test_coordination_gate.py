"""Teardown contract of the Codemap read/write coordination gate.

Admission-side tests for the same directory live beside the Codex runner that performs admission. What is covered here
is the other half: what a measured cell is allowed to leave behind. The sandbox profile grants the cell write access to
the gate and to nothing else, so files it writes there are permitted output; a paid run scored a correct answer as an
execution failure because teardown treated that permitted output as a fault, and these tests hold the corrected line.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from _bench_common.coordination_gate import (
    assert_coordination_root_idle,
    cleanup_coordination_root,
    prepare_coordination_root,
)

BENCHMARKS_DIR = Path(__file__).resolve().parent.parent.parent

#: Child program that takes a real lease lock and holds it until its stdin closes. It borrows the gate module's own
#: locking primitive rather than calling ``fcntl`` directly, so the lease is held the same way on every host.
_LEASE_HOLDER_SOURCE = (
    "import os, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from _bench_common.coordination_gate import try_lock_coordination_file\n"
    "fd = os.open(sys.argv[2], os.O_RDWR)\n"
    "assert try_lock_coordination_file(fd)\n"
    "print('ready', flush=True)\n"
    "sys.stdin.readline()\n"
)


@pytest.fixture(name="gate")
def _gate(tmp_path: Path) -> Path:
    """Return a freshly prepared gate skeleton in its own directory."""
    return prepare_coordination_root(tmp_path / "index-dir")


@pytest.fixture(name="outside")
def _outside(tmp_path: Path) -> Path:
    """Return a directory beside the gate holding one file that teardown must never reach."""
    directory = tmp_path / "outside"
    directory.mkdir()
    (directory / "keep.txt").write_text("keep", encoding="utf-8")
    return directory


@pytest.fixture(name="hold_lease")
def _hold_lease() -> Iterator[Callable[[Path], None]]:
    """Return a function that makes a separate live process hold the lease lock on a path until teardown."""
    children: list[subprocess.Popen[str]] = []

    def _hold(path: Path) -> None:
        """Start the holder child and wait until it reports the lock is taken."""
        child = subprocess.Popen(
            [sys.executable, "-c", _LEASE_HOLDER_SOURCE, str(BENCHMARKS_DIR), str(path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        children.append(child)
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "ready"

    yield _hold
    for child in children:
        child.communicate("\n", timeout=10)


class TestCleanupCoordinationRoot:
    """Tearing down a gate removes whatever a finished cell left and refuses only what is unsafe."""

    def test_removes_and_reports_scratch_left_behind(self, gate: Path, outside: Path) -> None:
        """Scratch written into the gate is removed and reported rather than raised as a cleanup failure.

        A cell running under the measured profile can write nowhere else, so a model that drops an analysis script or a
        working directory into the gate is doing what the profile permits. Reporting keeps that visible as an
        observation about how the cell worked; raising would score a correct answer as an execution failure, which is
        what happened.
        """
        (gate / "analyze_imports.py").write_text("print(1)\n", encoding="utf-8")
        probe = gate / "probe"
        probe.mkdir()
        (probe / "out.json").write_text("{}", encoding="utf-8")
        (gate / "escape").symlink_to(outside, target_is_directory=True)

        residue = cleanup_coordination_root(gate)

        assert residue == ["analyze_imports.py", "escape", "probe"]
        assert not gate.exists()
        assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"

    def test_tolerates_overwritten_skeleton(self, gate: Path) -> None:
        """A cell that deleted or replaced the gate's own files is still torn down without failing.

        Nothing reuses this directory — it belongs to one cell and is destroyed with it — so the identity of the
        registry and readers entries stops carrying meaning once the cell has ended. Only what is still alive in it
        does.
        """
        (gate / "registry.lock").unlink()
        (gate / "readers" / "scratch").mkdir()

        residue = cleanup_coordination_root(gate)

        assert residue == ["readers/scratch"]
        assert not gate.exists()

    def test_reports_no_residue_for_untouched_gate(self, gate: Path) -> None:
        """The ordinary case stays silent: a cell that wrote nothing leaves nothing to report.

        A pristine skeleton is the common outcome of a run, so any residue reported here would be noise on every cell.
        """
        residue = cleanup_coordination_root(gate)

        assert residue == []
        assert not gate.exists()

    def test_rejects_root_reached_through_symlink(self, tmp_path: Path, outside: Path) -> None:
        """A redirected gate path fails closed instead of deleting through the link.

        Tolerating residue must not become tolerating an arbitrary deletion target: if the root itself is a link, the
        directory that would be emptied is not the one the runner created.
        """
        redirected = tmp_path / ".index-rw"
        redirected.symlink_to(outside, target_is_directory=True)

        with pytest.raises(ValueError, match="symlink"):
            cleanup_coordination_root(redirected)

        assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"

    def test_rejects_already_removed_root(self, gate: Path) -> None:
        """A gate that is gone before teardown stays an explicit lifecycle error.

        Residue tolerance covers what a cell wrote inside the directory, not the disappearance of the directory the
        runner is responsible for; a silent success there would hide a lost or relocated gate.
        """
        cleanup_coordination_root(gate)

        with pytest.raises(ValueError, match="coordination root is unavailable"):
            cleanup_coordination_root(gate)


class TestLiveLease:
    """A lease lock held by a living process is the one teardown condition that stays fail-closed."""

    @pytest.mark.integration
    @pytest.mark.parametrize("held_entry", ["registry.lock", "readers/live.json"])
    @pytest.mark.parametrize(
        "teardown_check",
        [
            pytest.param(assert_coordination_root_idle, id="idle-assertion"),
            pytest.param(cleanup_coordination_root, id="removal"),
        ],
    )
    def test_refuses_teardown(
        self, gate: Path, hold_lease: Callable[[Path], None], held_entry: str, teardown_check: Callable[[Path], object]
    ) -> None:
        """Both the liveness assertion and the removal path refuse while another process holds a lease.

        Dead residue is the cell's permitted output, but a held lock names a reader that outlived its cell and is still
        reading the frozen index. The lock is taken by a real child process, since the contract is that the operating-
        system lock, not a file's mere presence, decides.
        """
        held = gate / held_entry
        held.parent.mkdir(parents=True, exist_ok=True)
        held.touch()
        hold_lease(held)

        with pytest.raises(ValueError, match=r"busy|live reader tokens"):
            teardown_check(gate)

        assert gate.is_dir()

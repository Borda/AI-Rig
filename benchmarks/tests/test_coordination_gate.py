"""Teardown contract of the Codemap read/write coordination gate.

Admission-side tests for the same directory live beside the Codex runner that performs admission. What is covered here
is the other half: what a measured cell is allowed to leave behind. The sandbox profile grants the cell write access to
the gate and to nothing else, so files it writes there are permitted output; a paid run scored a correct answer as an
execution failure because teardown treated that permitted output as a fault, and these tests hold the corrected line.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

BENCHMARKS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BENCHMARKS_DIR))

from _bench_common.coordination_gate import (  # noqa: E402
    assert_coordination_root_idle,
    cleanup_coordination_root,
    prepare_coordination_root,
)

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


def test_cleanup_removes_and_reports_the_scratch_a_cell_left_behind(gate: Path, tmp_path: Path) -> None:
    """Scratch written into the gate is removed and reported rather than raised as a cleanup failure.

    A cell running under the measured profile can write nowhere else, so a model that drops an analysis script or a
    working directory into the gate is doing what the profile permits. Reporting keeps that visible as an observation
    about how the cell worked; raising would score a correct answer as an execution failure, which is what happened.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    (gate / "analyze_imports.py").write_text("print(1)\n", encoding="utf-8")
    probe = gate / "probe"
    probe.mkdir()
    (probe / "out.json").write_text("{}", encoding="utf-8")
    (gate / "escape").symlink_to(outside, target_is_directory=True)

    residue = cleanup_coordination_root(gate)

    assert residue == ["analyze_imports.py", "escape", "probe"]
    assert not gate.exists()
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_cleanup_tolerates_a_skeleton_the_cell_overwrote(gate: Path) -> None:
    """A cell that deleted or replaced the gate's own files is still torn down without failing.

    Nothing reuses this directory — it belongs to one cell and is destroyed with it — so the identity of the registry
    and readers entries stops carrying meaning once the cell has ended. Only what is still alive in it does.
    """
    (gate / "registry.lock").unlink()
    (gate / "readers" / "scratch").mkdir()

    residue = cleanup_coordination_root(gate)

    assert residue == ["readers/scratch"]
    assert not gate.exists()


def test_cleanup_rejects_a_root_reached_through_a_symlink(tmp_path: Path) -> None:
    """A redirected gate path fails closed instead of deleting through the link.

    Tolerating residue must not become tolerating an arbitrary deletion target: if the root itself is a link, the
    directory that would be emptied is not the one the runner created.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    redirected = tmp_path / ".index-rw"
    redirected.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        cleanup_coordination_root(redirected)

    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_cleanup_rejects_an_already_removed_root(gate: Path) -> None:
    """A gate that is gone before teardown stays an explicit lifecycle error.

    Residue tolerance covers what a cell wrote inside the directory, not the disappearance of the directory the runner
    is responsible for; a silent success there would hide a lost or relocated gate.
    """
    cleanup_coordination_root(gate)

    with pytest.raises(ValueError, match="coordination root is unavailable"):
        cleanup_coordination_root(gate)


@pytest.mark.integration
@pytest.mark.parametrize("held_entry", ["registry.lock", "readers/live.json"])
def test_a_lease_held_by_a_living_process_fails_both_teardown_checks(gate: Path, held_entry: str) -> None:
    """A lock another process still holds is the one teardown condition that stays fail-closed.

    Dead residue is the cell's permitted output, but a held lock names a reader that outlived its cell and is still
    reading the frozen index. Both the liveness assertion and the removal path must refuse while it is held.
    """
    held = gate / held_entry
    held.parent.mkdir(parents=True, exist_ok=True)
    held.touch()
    child = subprocess.Popen(
        [sys.executable, "-c", _LEASE_HOLDER_SOURCE, str(BENCHMARKS_DIR), str(held)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "ready"
        with pytest.raises(ValueError, match="busy|live reader tokens"):
            assert_coordination_root_idle(gate)
        with pytest.raises(ValueError, match="busy|live reader tokens"):
            cleanup_coordination_root(gate)
    finally:
        child.communicate("\n", timeout=10)

    assert gate.is_dir()


def test_an_untouched_gate_reports_no_residue(gate: Path) -> None:
    """The ordinary case stays silent: a cell that wrote nothing leaves nothing to report."""
    residue = cleanup_coordination_root(gate)

    assert residue == []
    assert not gate.exists()

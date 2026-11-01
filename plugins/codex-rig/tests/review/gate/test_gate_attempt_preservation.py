"""Protect failed gate evidence across resumed workflow executions."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

RUNNER = Path(__file__).resolve().parents[3] / "shared/run_gates.py"
SKIP_REASON = "Outside this execution probe."
OTHER_GATES = ("lint", "format", "types", "review")
ALL_GATES = ("lint", "format", "types", "tests", "review")


def _gates_command(out: Path, skipped: tuple[str, ...]) -> list[str]:
    """Build a run_gates.py invocation that skips every gate named in ``skipped``."""
    skips = [arg for gate in skipped for arg in (f"--skip-{gate}", SKIP_REASON)]
    return [sys.executable, str(RUNNER), "--out", str(out), *skips]


def _run(command: list[str], *extra: str) -> subprocess.CompletedProcess[str]:
    """Run the gates command with extra arguments and capture its text output."""
    return subprocess.run([*command, *extra], capture_output=True, text=True, check=False)


def _snapshot(root: Path) -> dict[Path, bytes]:
    """Map every file under ``root`` to its bytes, keyed by relative path."""
    return {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}


@pytest.fixture
def gates_command(tmp_path: Path) -> list[str]:
    """Return a run_gates.py invocation that skips every gate except tests."""
    return _gates_command(tmp_path, OTHER_GATES)


@pytest.mark.parametrize("prior_exit", [1, 124, 127])
def test_failed_gate_cannot_be_reclassified_as_skipped(
    gates_command: list[str], tmp_path: Path, prior_exit: int
) -> None:
    """Reject skip recovery before any old evidence or unrelated gate gets overwritten.

    A tests gate failed (exit 1, timeout 124 or missing command 127). Resuming with ``--skip-tests`` must not turn that
    failure into a skip, and must leave every byte of the earlier evidence untouched.
    """
    failed = _run(gates_command, "--tests", f"exit {prior_exit}")
    assert failed.returncode in (1, 124)
    before = _snapshot(tmp_path)

    skipped = _run(gates_command, "--skip-tests", "Launcher failed; direct check passed.")

    assert skipped.returncode == 2
    assert "cannot-skip-failed-gate:tests" in skipped.stderr
    assert _snapshot(tmp_path) == before


def test_first_failed_run_reports_gate_failure(gates_command: list[str]) -> None:
    """A failing tests gate returns the documented failure exit code.

    Fresh output directory, tests probe exits 1: the runner must surface exit code 1 rather than swallow it.
    """
    failed = _run(gates_command, "--tests", "exit 1")

    assert failed.returncode == 1


def test_successful_rerun_archives_prior_failed_attempt(gates_command: list[str], tmp_path: Path) -> None:
    """A passing rerun after a failure archives the failed attempt's evidence instead of destroying it.

    The first run fails and writes gates.json plus the recorded command. After the passing rerun, the failed attempt
    must live byte-for-byte under ``gate-attempts/001`` while the top-level receipt reports a pass.
    """
    _run(gates_command, "--tests", "exit 1")
    old_gates = (tmp_path / "gates.json").read_bytes()
    old_command = (tmp_path / "checks/tests.command.txt").read_bytes()

    passed = _run(gates_command, "--tests", "exit 0")

    assert passed.returncode == 0, passed.stderr
    assert json.loads((tmp_path / "gates.json").read_text())["status"] == "pass"
    archive = tmp_path / "gate-attempts" / "001"
    assert (archive / "gates.json").read_bytes() == old_gates
    assert (archive / "checks/tests.command.txt").read_bytes() == old_command


def test_repeated_success_creates_new_archive_without_overwriting_prior(
    gates_command: list[str], tmp_path: Path
) -> None:
    """A second successful rerun archives again under a new slot, leaving the first archive untouched.

    Sequence fail, pass, pass: slot 001 holds the failed attempt, slot 002 must receive the first pass, and 001 must not
    be rewritten by the later archive.
    """
    _run(gates_command, "--tests", "exit 1")
    old_gates = (tmp_path / "gates.json").read_bytes()
    _run(gates_command, "--tests", "exit 0")
    archive = tmp_path / "gate-attempts" / "001"

    repeated = _run(gates_command, "--tests", "exit 0")

    assert repeated.returncode == 0, repeated.stderr
    assert (archive / "gates.json").read_bytes() == old_gates
    assert json.loads((tmp_path / "gate-attempts/002/gates.json").read_text())["status"] == "pass"


@pytest.mark.parametrize("invalid_field", ["id", "status"])
def test_malformed_previous_receipt_is_preserved(tmp_path: Path, invalid_field: str) -> None:
    """Invalid historical JSON shapes produce a recovery diagnostic, never a traceback or overwrite.

    A prior gates.json carries a list where ``id`` or ``status`` must be a string. The runner must refuse with exit code
    2 and a recovery marker, keep the receipt bytes, and create no ``gate-attempts`` archive.
    """
    checks = [{"id": gate, "status": "pass"} for gate in ALL_GATES]
    checks[0][invalid_field] = []
    prior = json.dumps({"checks": checks}).encode()
    (tmp_path / "gates.json").write_bytes(prior)

    result = _run(_gates_command(tmp_path, ALL_GATES))

    assert result.returncode == 2
    assert "gate-recovery-blocked:invalid-previous-gates" in result.stderr
    assert "Traceback" not in result.stderr
    assert (tmp_path / "gates.json").read_bytes() == prior
    assert not (tmp_path / "gate-attempts").exists()

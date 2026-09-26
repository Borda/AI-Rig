"""Protect failed gate evidence across resumed workflow executions."""

import json
import subprocess
import sys
from pathlib import Path

import pytest


RUNNER = Path(__file__).resolve().parents[2] / "shared/run_gates.py"


@pytest.mark.parametrize("prior_exit", [1, 124, 127])
def test_failed_gate_cannot_be_reclassified_as_skipped(tmp_path: Path, prior_exit: int) -> None:
    """Reject skip recovery before any old evidence or unrelated gate gets overwritten."""
    command = [sys.executable, str(RUNNER), "--out", str(tmp_path)]
    for gate in ("lint", "format", "types", "review"):
        command.extend([f"--skip-{gate}", "Outside this execution probe."])
    failed = subprocess.run(command + ["--tests", f"exit {prior_exit}"], capture_output=True, text=True, check=False)
    assert failed.returncode in (1, 124)
    before = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    skipped = subprocess.run(
        command + ["--skip-tests", "Launcher failed; direct check passed."], capture_output=True, text=True, check=False
    )

    assert skipped.returncode == 2
    assert "cannot-skip-failed-gate:tests" in skipped.stderr
    assert {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


@pytest.fixture
def gates_command(tmp_path: Path) -> list[str]:
    """Build the base run_gates.py invocation, skipping every gate but tests."""
    command = [sys.executable, str(RUNNER), "--out", str(tmp_path)]
    for gate in ("lint", "format", "types", "review"):
        command.extend([f"--skip-{gate}", "Outside this execution probe."])
    return command


def _run_tests_gate(command: list[str], tests_arg: str) -> subprocess.CompletedProcess[str]:
    """Invoke the gates command with a given --tests probe argument."""
    return subprocess.run(command + ["--tests", tests_arg], capture_output=True, text=True, check=False)


def test_first_failed_run_reports_gate_failure(gates_command: list[str]) -> None:
    """A failing tests gate returns the documented failure exit code."""
    failed = _run_tests_gate(gates_command, "exit 1")
    assert failed.returncode == 1


def test_successful_rerun_archives_prior_failed_attempt(gates_command: list[str], tmp_path: Path) -> None:
    """A passing rerun after a failure archives the failed attempt's evidence instead of destroying it."""
    _run_tests_gate(gates_command, "exit 1")
    old_gates = (tmp_path / "gates.json").read_bytes()
    old_command = (tmp_path / "checks/tests.command.txt").read_bytes()

    passed = _run_tests_gate(gates_command, "exit 0")

    assert passed.returncode == 0, passed.stderr
    assert json.loads((tmp_path / "gates.json").read_text())["status"] == "pass"
    archive = tmp_path / "gate-attempts" / "001"
    assert (archive / "gates.json").read_bytes() == old_gates
    assert (archive / "checks/tests.command.txt").read_bytes() == old_command


def test_repeated_success_creates_new_archive_without_overwriting_prior(
    gates_command: list[str], tmp_path: Path
) -> None:
    """A second successful rerun archives again under a new slot, leaving the first archive untouched."""
    _run_tests_gate(gates_command, "exit 1")
    old_gates = (tmp_path / "gates.json").read_bytes()
    _run_tests_gate(gates_command, "exit 0")
    archive = tmp_path / "gate-attempts" / "001"

    repeated = _run_tests_gate(gates_command, "exit 0")

    assert repeated.returncode == 0, repeated.stderr
    assert (archive / "gates.json").read_bytes() == old_gates
    assert json.loads((tmp_path / "gate-attempts/002/gates.json").read_text())["status"] == "pass"


@pytest.mark.parametrize("invalid_field", ["id", "status"])
def test_malformed_previous_receipt_is_preserved(tmp_path: Path, invalid_field: str) -> None:
    """Invalid historical JSON shapes produce a recovery diagnostic, never a traceback or overwrite."""
    checks = [{"id": gate, "status": "pass"} for gate in ("lint", "format", "types", "tests", "review")]
    checks[0][invalid_field] = []
    prior = json.dumps({"checks": checks}).encode()
    (tmp_path / "gates.json").write_bytes(prior)
    command = [sys.executable, str(RUNNER), "--out", str(tmp_path)]
    for gate in ("lint", "format", "types", "tests", "review"):
        command.extend([f"--skip-{gate}", "Outside this execution probe."])

    result = subprocess.run(command, capture_output=True, text=True, check=False)

    assert result.returncode == 2
    assert "gate-recovery-blocked:invalid-previous-gates" in result.stderr
    assert "Traceback" not in result.stderr
    assert (tmp_path / "gates.json").read_bytes() == prior
    assert not (tmp_path / "gate-attempts").exists()

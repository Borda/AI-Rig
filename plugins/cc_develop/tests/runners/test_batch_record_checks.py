"""Verify completed-run checks and fixed bisect targets through the helper CLI."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parents[2] / "bin"


@pytest.mark.parametrize(
    ("tests", "code", "expected"),
    [
        pytest.param(["tests/test_a.py"], 1, 0, id="completed-red"),
        pytest.param([], 1, 2, id="empty-red"),
        pytest.param(["tests/test_a.py"], None, 2, id="plan-only"),
        pytest.param(["tests/test_a.py"], 2, 2, id="collection-error"),
    ],
)
def test_check_last_record(tmp_path: Path, tests: list[str], code: int | None, expected: int) -> None:
    """Only the last nonempty run with the requested exit establishes red."""
    record = tmp_path / "test-targets.jsonl"
    record.write_text(json.dumps({"tests": ["old"], "exit": 1}) + "\n" + json.dumps({"tests": tests, "exit": code}))
    result = subprocess.run(
        [sys.executable, str(_BIN / "dev_test_targets.py"), "--check-record", str(record), "--expect-exit", "1"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == expected, result.stdout + result.stderr


@pytest.mark.parametrize("message", ["AssertionError: wrong", "AssertionError: [1] != [2]"])
def test_save_failed_nodes_preserves_parameter_text(tmp_path: Path, message: str) -> None:
    """Full logs supply fixed nodes without losing spaces or delimiters in parameters."""
    node = "tests/test_a.py::test_value[a - b]"
    log = tmp_path / "run.log"
    log.write_text(f"\x1b[31mFAILED {node}\x1b[0m - {message}\n")
    record = tmp_path / "test-targets.jsonl"
    record.write_text(json.dumps({"tests": ["tests/test_a.py"], "exit": 1, "log": str(log)}))
    targets = tmp_path / "targets.json"
    result = subprocess.run(
        [
            sys.executable,
            str(_BIN / "dev_test_targets.py"),
            "--check-record",
            str(record),
            "--expect-exit",
            "1",
            "--save-failures",
            str(targets),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(targets.read_text()) == [node]


@pytest.mark.parametrize(
    "text",
    [
        "no summary",
        "FAILED malformed",
        "FAILED tests/test_a.py::test_a\n[run-pytest-short: output truncated at 10 bytes]",
    ],
)
def test_incomplete_failure_evidence_is_rejected(tmp_path: Path, text: str) -> None:
    """No failure extraction may silently accept missing or truncated evidence."""
    log = tmp_path / "run.log"
    log.write_text(text)
    record = tmp_path / "record.jsonl"
    record.write_text(json.dumps({"tests": ["tests/test_a.py"], "exit": 1, "log": str(log)}))
    targets = tmp_path / "targets.json"
    result = subprocess.run(
        [
            sys.executable,
            str(_BIN / "dev_test_targets.py"),
            "--check-record",
            str(record),
            "--expect-exit",
            "1",
            "--save-failures",
            str(targets),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert not targets.exists()

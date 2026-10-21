"""Verify the ledger and plan SHA streams consumed by resolve Phase 3."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "bin/extract_commit_shas.py"


@pytest.mark.integration
@pytest.mark.parametrize(
    ("mode", "source", "expected"),
    [
        pytest.param(
            "ledger", '\n{"sha":" abc123 "}\n \n{"sha":"def456"}\n', "abc123\ndef456\n", id="ledger-strips-blanks"
        ),
        pytest.param(
            "plan", '[{"sha":" abc123 "},{"sha":"def456"}]', " abc123 \ndef456\n", id="plan-preserves-sha-text"
        ),
        pytest.param("ledger", "\n \n", "", id="empty-ledger"),
        pytest.param("plan", "[]", "", id="empty-plan"),
    ],
)
def test_extract_shas(tmp_path: Path, mode: str, source: str, expected: str) -> None:
    """Print SHAs in source order without changing the two input formats' whitespace rules."""
    path = tmp_path / "commits.json"
    path.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), mode, str(path)], capture_output=True, text=True, check=False
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, expected, "")


@pytest.mark.integration
@pytest.mark.parametrize(
    ("mode", "source", "error", "partial_stdout"),
    [
        pytest.param(
            "ledger", '{"sha":"abc123"}\n{"sha":" "}\n', "empty sha in ledger row:", "abc123\n", id="empty-ledger-sha"
        ),
        pytest.param("ledger", '{"sha":"abc123"}\n{', "JSONDecodeError", "abc123\n", id="truncated-ledger"),
        pytest.param("plan", '[{"item_id":1}]', "KeyError", "", id="plan-missing-sha"),
        pytest.param("plan", "[", "JSONDecodeError", "", id="truncated-plan"),
    ],
)
def test_invalid_sha_source(tmp_path: Path, mode: str, source: str, error: str, partial_stdout: str) -> None:
    """Fail on malformed inputs so shell guards cannot interpret them as an empty successful stream."""
    path = tmp_path / "commits.json"
    path.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), mode, str(path)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 1
    assert result.stdout == partial_stdout
    assert error in result.stderr

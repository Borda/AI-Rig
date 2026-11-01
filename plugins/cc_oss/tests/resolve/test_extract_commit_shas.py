"""Verify the ledger and plan SHA streams consumed by resolve Phase 3."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "bin/extract_commit_shas.py"


def _extract(tmp_path: Path, mode: str, source: str) -> subprocess.CompletedProcess[str]:
    """Run the script in ``mode`` on a ``commits.json`` holding ``source``."""
    path = tmp_path / "commits.json"
    path.write_text(source, encoding="utf-8")
    return subprocess.run([sys.executable, str(_SCRIPT), mode, str(path)], capture_output=True, text=True, check=False)


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
def test_prints_shas_in_source_order(tmp_path: Path, mode: str, source: str, expected: str) -> None:
    """SHAs are printed one per line in source order, with each input format's own whitespace rule.

    A ledger is JSON lines whose SHA text is stripped and whose blank lines are skipped; a plan is one JSON array whose
    SHA text is kept verbatim. An empty source prints nothing and still exits 0.
    """
    result = _extract(tmp_path, mode, source)
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
def test_malformed_source_fails(tmp_path: Path, mode: str, source: str, error: str, partial_stdout: str) -> None:
    """A malformed ledger or plan exits 1 with the parse error rather than looking like an empty stream.

    Phase 3's shell guards treat empty output as "nothing to revert"; a truncated file, a missing ``sha`` key or a blank
    SHA must therefore fail loudly. SHAs already read before the bad row may still have been printed.
    """
    result = _extract(tmp_path, mode, source)
    assert result.returncode == 1
    assert result.stdout == partial_stdout
    assert error in result.stderr

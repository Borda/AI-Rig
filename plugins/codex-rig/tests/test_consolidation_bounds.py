"""Keep consolidation inputs within a bounded report and source byte budget."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from _modules import load_module_registered

HELPER = Path(__file__).resolve().parents[1] / "shared" / "audit_consolidation.py"
LIMIT = 16 * 1024 * 1024


@pytest.fixture
def consolidation() -> ModuleType:
    """Load the same settlement implementation used by report consumers."""
    return load_module_registered("consolidation_bounds", HELPER)


def test_oversized_source_is_rejected_before_digest_settlement(consolidation: ModuleType, tmp_path: Path) -> None:
    """A valid digest cannot authorize an unbounded source-file read."""
    source = tmp_path / "large.md"
    content = b"x" * (LIMIT + 1)
    source.write_bytes(content)
    report = {
        "schema_version": 1,
        "sources": [{"id": "source", "path": str(source.resolve()), "sha256": hashlib.sha256(content).hexdigest()}],
        "items": [],
        "coverage": [],
        "candidates": [],
        "excluded": [],
    }
    with pytest.raises(ValueError, match="consolidation-input-oversized"):
        consolidation.settle_consolidation(report)


def test_oversized_report_cli_returns_coded_error(tmp_path: Path) -> None:
    """The report path itself is bounded before JSON parsing allocates its object graph."""
    report = tmp_path / "report.json"
    report.write_bytes(b" " * (LIMIT + 1))
    result = subprocess.run([sys.executable, str(HELPER), "--report", str(report)], capture_output=True, check=False)
    assert result.returncode == 1
    assert b"consolidation-input-oversized" in result.stderr
    assert b"Traceback" not in result.stderr

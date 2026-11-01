"""Fixtures shared by the remediation suites."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_finding_presentation import _write_remediation_candidate


@pytest.fixture
def valid_run(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    """Write a selected, implemented remediation run that passes every validator step."""
    result_path = _write_remediation_candidate(tmp_path, "code", ("implemented", "Added the guard."))
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["metadata"]["unresolved_summary"].update(selected_items_total=1, selected_items_resolved=1)
    result_path.write_text(json.dumps(result), encoding="utf-8")
    return tmp_path, result

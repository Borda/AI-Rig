"""Protect telemetry, default-mode, and interpreter-failure routing in Claude skills."""

from __future__ import annotations

from pathlib import Path

import pytest

_SKILLS = Path(__file__).resolve().parents[2] / "claude-skills"


def test_debrief_distinguishes_crashes_from_absent_telemetry() -> None:
    """Exit one alone cannot justify telling the user that no telemetry exists."""
    lines = (_SKILLS / "debrief-coding" / "SKILL.md").read_text(encoding="utf-8").splitlines()
    route = next(line for line in lines if line.startswith("- Exit `1`"))

    assert "only when stderr starts with `No codemap telemetry found`" in route
    assert "Any other exit `1`" in route
    assert "crash" in route
    assert "relay stderr" in route


@pytest.mark.parametrize("clause", ["  Skip for:", "NOT for:"])
def test_integration_defers_usage_reports_to_debrief(clause: str) -> None:
    """Both discovery and execution boundaries route telemetry analysis to its owning skill."""
    lines = (_SKILLS / "integration" / "SKILL.md").read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(clause))
    end = next(i for i in range(start + 1, len(lines)) if not lines[i].startswith("  "))
    route = " ".join(lines[start:end])
    step = next(line for line in lines if line.startswith("First, if the request"))

    assert "telemetry" in route
    assert "/codemap-py:debrief-coding" in route
    assert "/codemap-py:debrief-coding" in step


def test_integration_accepts_flags_without_an_explicit_mode() -> None:
    """Flags-only requests select audit and retain their flags for execution."""
    lines = (_SKILLS / "integration" / "SKILL.md").read_text(encoding="utf-8").splitlines()
    route = next(line for line in lines if line.startswith("First, if the request"))

    assert "first token starts with `--` → audit" in route
    assert "preserve all flags" in route


def test_integration_stops_after_an_interpreter_probe_failure() -> None:
    """A launcher failure relays its actionable stderr and never retries the workload."""
    lines = (_SKILLS / "integration" / "SKILL.md").read_text(encoding="utf-8").splitlines()
    report = next(line for line in lines if line.startswith("Report exit meaning per mode"))

    assert "`127`" in report
    assert "no eligible CPython" in report
    assert "relay its stderr line" in report
    assert "no retry" in report

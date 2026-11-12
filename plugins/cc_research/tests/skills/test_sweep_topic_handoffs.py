"""Exercise sweep handoff guards and topic routing instructions."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parents[2]
_BASH = shutil.which("bash")
_needs_bash = pytest.mark.skipif(_BASH is None, reason="bash is not on PATH")


def _lines(relative: str) -> list[str]:
    """Read the shipped instructions without flattening their structure."""
    return (_PLUGIN / "skills" / relative).read_text(encoding="utf-8").splitlines()


def _block(lines: list[str], marker: str) -> str:
    """Extract the Bash fence containing an executable marker."""
    current: list[str] | None = None
    for line in lines:
        if line == "```bash":
            current = []
        elif line == "```" and current is not None:
            if any(marker in item for item in current):
                return "\n".join(current)
            current = None
        elif current is not None:
            current.append(line)
    raise AssertionError(f"No Bash block contains {marker!r}")


@_needs_bash
@pytest.mark.parametrize(
    ("arguments", "expected_code"),
    [
        pytest.param("<program path> <forwarded flags verbatim>", 1, id="all-placeholders"),
        pytest.param("program.md <forwarded flags verbatim>", 1, id="flags-placeholder"),
        pytest.param('"nested/program file.md" --team --keep "metric, guard"', 0, id="resolved-arguments"),
        pytest.param('program.md --keep "latency < 10ms and throughput > 50"', 0, id="comparison-prose"),
    ],
)
def test_run_handoff_blocks_unresolved_placeholders(tmp_path: Path, arguments: str, expected_code: int) -> None:
    """An unresolved handoff cannot reach R1; resolved quoted flags survive unchanged."""
    block = _block(_lines("sweep/SKILL.md"), 'cat > "${TMPDIR:-/tmp}/sweep-run-args-')
    block = block.replace("<program path> <forwarded flags verbatim>", arguments)
    result = subprocess.run(
        [_BASH, "-c", block],
        cwd=tmp_path,
        env={**os.environ, "TMPDIR": str(tmp_path), "CLAUDE_CODE_SESSION_ID": "handoff"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == expected_code
    assert ("! BLOCKED" in result.stdout) == bool(expected_code)
    assert (tmp_path / "sweep-run-args-handoff").exists() == (expected_code == 0)
    assert expected_code or (tmp_path / "sweep-run-args-handoff").read_text() == arguments + "\n"


@_needs_bash
def test_default_sweep_persists_inline_marker_before_overwrite_gate(tmp_path: Path) -> None:
    """A sweep without --out still supplies the marker protecting its judge contract."""
    lines = _lines("sweep/SKILL.md")
    directive = next(line for line in lines if line.startswith("**`--out` validation**"))
    assert "unconditionally" in directive
    assert "before" in directive
    assert "overwrite" in directive
    block = _block(lines, "check_output_within_root.py")
    result = subprocess.run(
        [_BASH, "-c", block],
        cwd=tmp_path,
        env={
            **os.environ,
            "TMPDIR": str(tmp_path),
            "CLAUDE_CODE_SESSION_ID": "default",
            "CLAUDE_PLUGIN_ROOT": str(_PLUGIN),
            "ARGUMENTS": '"Reduce latency"',
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "sweep-out-path-default").read_text().strip() == "program.md"


@_needs_bash
@pytest.mark.parametrize("marker", ["# sweep entry cleanup", "# sweep exit cleanup"])
def test_sweep_cleanup_removes_both_handoffs_and_contract(tmp_path: Path, marker: str) -> None:
    """Normal and early terminal paths cannot leave a stale inline contract or run flags."""
    (tmp_path / ".temp/state").mkdir(parents=True)
    contract = tmp_path / ".temp/state/skill-contract.md"
    contract.write_text("sweep contract")
    output = tmp_path / "sweep-out-path-cleanup"
    output.write_text("old-program.md")
    arguments = tmp_path / "sweep-run-args-cleanup"
    arguments.write_text('old-program.md --keep "old metric"')
    result = subprocess.run(
        [_BASH, "-c", _block(_lines("sweep/SKILL.md"), marker)],
        cwd=tmp_path,
        env={
            **os.environ,
            "TMPDIR": str(tmp_path),
            "CLAUDE_CODE_SESSION_ID": "cleanup",
            "CLAUDE_PLUGIN_ROOT": str(_PLUGIN),
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not contract.exists()
    assert not output.exists()
    assert not arguments.exists()
    assert any("every terminal exit" in line and "abort" in line for line in _lines("sweep/SKILL.md"))


def test_failed_plan_dry_run_stops_sweep_before_judge() -> None:
    """Sweep cannot silently accept or interactively waive failed evaluator commands."""
    lines = _lines("sweep/SKILL.md")
    rule = next(line for line in lines if line.startswith("- **P-P2 dry-run failure**"))
    assert "INCOMPLETE" in rule
    assert "full error" in rule
    assert "do NOT proceed to P-P2b, P-P3 or S3" in rule
    assert "no question" in rule


def test_ambiguous_metric_direction_uses_run_default() -> None:
    """Sweep's explicit direction cannot disagree with the run parser's default."""
    rule = next(line for line in _lines("sweep/SKILL.md") if line.startswith("- **Metric direction conventions**"))
    assert "run/SKILL.md" in rule
    assert "R1" in rule
    assert "default `direction: lower`" not in rule
    assert "direction: higher" in rule


def test_topic_search_fallback_has_one_inline_owner() -> None:
    """Missing foundry must not select both a general-purpose agent and inline search."""
    rule = next(line for line in _lines("topic/SKILL.md") if line.startswith("**Agent resolution**"))
    assert "Step 2a" in rule
    assert "inline" in rule
    assert "plan" in rule
    assert "general-purpose" in rule
    assert "substitute each" not in rule


def test_team_consolidator_declares_type_and_resolution_fallback() -> None:
    """The consolidator has a verifiable role and follows topic's missing-provider policy."""
    rule = next(line for line in _lines("topic/modes/team.md") if line.startswith("For 3 teammates,"))
    assert 'subagent_type="general-purpose"' in rule
    assert 'name="research-consolidator"' in rule
    assert 'description="' in rule
    assert "Agent Resolution" in rule
    assert "inline" in rule
    assert "partial: true" in rule


def test_plan_autodetect_filters_report_prefixes_instead_of_branch_names() -> None:
    """A report from a plan-v2 or codebase branch remains eligible for auto-detection."""
    rule = next(line for line in _lines("topic/modes/plan.md") if line.startswith("- No argument"))
    assert "basenames starting with `topic-plan-` or `topic-codebase-`" in rule
    assert "containing `-plan-`" not in rule

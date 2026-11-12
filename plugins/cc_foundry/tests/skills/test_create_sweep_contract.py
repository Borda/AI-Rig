"""Guard creator decision routing and session-specific investigation recovery."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from _hook_env import _bash_runs_posix_script

_SKILLS = Path(__file__).resolve().parents[2] / "skills"
_BASH = pytest.mark.skipif(not _bash_runs_posix_script(), reason="POSIX Bash execution unavailable")


def _lines(skill: str) -> list[str]:
    """Read a skill as independent instruction lines."""
    return (_SKILLS / skill / "SKILL.md").read_text(encoding="utf-8").splitlines()


def test_creator_decisions_return_to_the_user_before_respawn() -> None:
    """A decision envelope must stay distinct from a completed artifact notification."""
    lines = _lines("create")
    assert any("needs_decision" in line and "AskUserQuestion" in line and "re-spawn" in line for line in lines)
    assert any("blocked" in line and "reason" in line and "unsupported" in line for line in lines)
    assert any("partial" in line and "humanizer" in line for line in lines)


def test_create_offers_humanizer_only_after_a_complete_prose_artifact() -> None:
    """Create can dispatch the prose polish pass without rewriting code or metadata."""
    lines = _lines("create")
    assert "Skill" in next(line for line in lines if line.startswith("allowed-tools:")).split(":", 1)[1].split(", ")
    assert any("optional" in line and "foundry:humanizer" in line and "done" in line for line in lines)
    assert any("frontmatter" in line and "Marp" in line and "SVG" in line for line in lines)


@_BASH
def test_investigate_never_recovers_another_sessions_latest_run(tmp_path: Path) -> None:
    """A missing session sentinel must stop auxiliary review rather than select a sibling run."""
    lines = _lines("investigate")
    assert not any("find .temp/investigate" in line for line in lines)
    assert any("INVESTIGATE_RUN" in line and "BLOCKED" in line and "sentinel" in line for line in lines)
    section = lines[lines.index("## Step 4: Auxiliary review (optional)") :]
    start = section.index("```bash")
    end = section.index("```", start + 1)
    sibling = tmp_path / ".temp/investigate/other-session"
    sibling.mkdir(parents=True)
    result = subprocess.run(
        ["bash", "-c", "\n".join(section[start + 1 : end])],
        cwd=tmp_path,
        env={**os.environ, "TMPDIR": str(tmp_path), "CLAUDE_CODE_SESSION_ID": "missing-session"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert "sentinel missing" in result.stdout
    assert "CODEX_OUT=" not in result.stdout


@_BASH
@pytest.mark.parametrize("argument", ["", ".reports/audit/2026-11-12", ".reports/audit/2026-11-12/efficiency"])
def test_distill_discovers_check33_in_the_efficiency_subdirectory(tmp_path: Path, argument: str) -> None:
    """The extraction consumer finds reports produced by audit's nested efficiency route."""
    report = tmp_path / ".reports/audit/2026-11-12/efficiency/efficiency-check33-example.md"
    report.parent.mkdir(parents=True)
    report.write_text("Extraction candidate\n", encoding="utf-8")
    lines = (_SKILLS / "distill/modes/executables.md").read_text(encoding="utf-8").splitlines()
    start = lines.index("```bash")
    end = lines.index("```", start + 1)
    script = "\n".join(lines[start + 1 : end]).replace("<EXEC_PATH>", argument)
    result = subprocess.run(
        ["bash", "-c", 'python() { "$SWEEP_PYTHON" "$@"; }; ' + script],
        cwd=tmp_path,
        env={
            **os.environ,
            "TMPDIR": str(tmp_path),
            "CLAUDE_PLUGIN_ROOT": str(_SKILLS.parent),
            "CLAUDE_CODE_SESSION_ID": "nested-efficiency",
            "SWEEP_PYTHON": sys.executable,
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert report.relative_to(tmp_path).as_posix() in result.stdout.splitlines()

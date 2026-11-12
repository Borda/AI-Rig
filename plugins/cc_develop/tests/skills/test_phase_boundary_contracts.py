"""Exercise state recovery, compaction and stop behavior in the develop workflows."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from _develop_text import nested_bash_fences

_SKILLS = Path(__file__).resolve().parents[2] / "skills"
_BASH = shutil.which("bash")
_requires_bash = pytest.mark.skipif(_BASH is None, reason="bash unavailable")


@_requires_bash
@pytest.mark.parametrize("skill", ["feature", "fix", "refactor", "debug", "review"])
def test_worktree_gate_reads_the_persisted_calling_skill(tmp_path: Path, skill: str) -> None:
    """The shared gate must honor the caller's real flag instead of a placeholder sentinel."""
    text = (_SKILLS / "_shared/worktree-isolation.md").read_text(encoding="utf-8")
    gate = next(block for block in nested_bash_fences(text) if "WORKTREE_ENABLED" in block)
    (tmp_path / "dev-worktree-skill-boundary").write_text(skill + "\n", encoding="utf-8", newline="\n")
    (tmp_path / f"dev-{skill}-worktree-boundary").write_text("true", encoding="utf-8")
    result = subprocess.run(
        [_BASH, "-c", gate + '\nprintf "enabled=%s\\n" "$WORKTREE_ENABLED"'],
        env={**os.environ, "TMPDIR": str(tmp_path), "CLAUDE_CODE_SESSION_ID": "boundary"},
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "enabled=true" in result.stdout


@pytest.mark.parametrize("skill", ["feature", "fix", "refactor", "debug", "review"])
def test_worktree_caller_persists_its_skill_before_loading(skill: str) -> None:
    """Independent shell blocks can recover the caller identity before any worktree action."""
    lines = (_SKILLS / skill / "SKILL.md").read_text(encoding="utf-8").splitlines()
    load = next(i for i, line in enumerate(lines) if 'dev_load_shared.py" worktree-isolation.md' in line)
    saved = next(i for i, line in enumerate(lines) if "dev-worktree-skill-${CSID}" in line)
    assert saved < load
    assert skill in lines[saved]


@_requires_bash
@pytest.mark.parametrize(
    ("cycles", "elapsed"),
    [pytest.param(6, 0, id="cycle-cap"), pytest.param(0, 1801, id="wall-time-cap")],
)
def test_refactor_safety_caps_stop_the_shell(tmp_path: Path, cycles: int, elapsed: int) -> None:
    """A reached safety limit must prevent the next action in the same shell call."""
    text = (_SKILLS / "refactor/SKILL.md").read_text(encoding="utf-8")
    block = next(block for block in nested_bash_fences(text) if "INNER_CYCLE=$((" in block)
    (tmp_path / "dev-inner-cycle-boundary").write_text(str(cycles) + "\n", encoding="utf-8", newline="\n")
    (tmp_path / "dev-start-time-boundary").write_text(
        str(int(time.time()) - elapsed) + "\n", encoding="utf-8", newline="\n"
    )
    result = subprocess.run(
        [_BASH, "-c", block + '\necho "UNREACHABLE"'],
        env={**os.environ, "TMPDIR": str(tmp_path), "CLAUDE_CODE_SESSION_ID": "boundary"},
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "UNREACHABLE" not in result.stdout


@pytest.mark.parametrize("skill", ["feature", "refactor", "fix"])
def test_expanding_boundaries_preserve_metadata_and_invoke_compaction(skill: str) -> None:
    """Every phase transition carrying a saved contract must execute its compaction decision."""
    text = (_SKILLS / skill / "SKILL.md").read_text(encoding="utf-8")
    blocks = [block for block in nested_bash_fences(text) if "# boundary " in block]
    assert len(blocks) == 2
    assert all("calls_ahead=" in block and "drop=" in block for block in blocks)
    assert all('compaction_boundary.py" "$_DEV_SHARED"' in block for block in blocks)
    assert all('[ -z "$_DEV_SHARED" ]' in block for block in blocks)


@pytest.mark.parametrize("skill", ["feature", "fix", "refactor"])
def test_branch_guard_precedes_team_dispatch_and_source_edits(skill: str) -> None:
    """The source-editing guard runs after optional worktree entry and before either edit route."""
    lines = (_SKILLS / skill / "SKILL.md").read_text(encoding="utf-8").splitlines()
    entry = next(i for i, line in enumerate(lines) if "→ follow §Enter" in line)
    guard = next(i for i, line in enumerate(lines) if "CURRENT_BRANCH=$(git rev-parse" in line)
    first_step = next(i for i, line in enumerate(lines) if line.startswith("## Step 1"))
    assert entry < guard < first_step
    assert any("exit 1" in line for line in lines[guard : guard + 8])


@_requires_bash
def test_feature_team_refuses_a_self_reported_red_exit_without_proof(tmp_path: Path) -> None:
    """A teammate's exit number cannot substitute for the demo gate's fresh nonce receipt."""
    text = (_SKILLS / "feature/modes/team-mode.md").read_text(encoding="utf-8")
    block = next(block for block in nested_bash_fences(text) if 'WAVE1_FILE="' in block)
    run = tmp_path / ".temp/develop/round"
    run.mkdir(parents=True)
    (run / "feature-sw-engineer-round.md").write_text('{"demo_red_exit":1}\n', encoding="utf-8")
    (tmp_path / "dev-feature-team-ts-boundary").write_text("round\n", encoding="utf-8", newline="\n")
    result = subprocess.run(
        [_BASH, "-c", block],
        env={**os.environ, "TMPDIR": str(tmp_path), "CLAUDE_CODE_SESSION_ID": "boundary"},
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "proof" in result.stdout.lower()

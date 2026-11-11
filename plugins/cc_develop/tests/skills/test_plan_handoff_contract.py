"""Protect plan filename consistency, diagnostic handoffs and in-place revisions."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from _develop_text import bash_fences

_SKILLS = Path(__file__).resolve().parents[2] / "skills"
_PLAN = _SKILLS / "plan/SKILL.md"
_BASH = shutil.which("bash")
_skip_no_bash = pytest.mark.skipif(_BASH is None, reason="bash unavailable")


@_skip_no_bash
def test_plan_filename_matches_inline_scheme_and_never_overwrites(tmp_path: Path) -> None:
    """Two invocations with the same timestamp preserve both plans under the inline filename convention."""
    namespace = tmp_path / "namespace"
    namespace.mkdir()
    (namespace / "clean-args").write_text("improve caching in data loader\n", encoding="utf-8")
    (tmp_path / "dev-plan-ns-current-filename").write_text(namespace.as_posix() + "\n", encoding="utf-8")
    block = next(block for block in bash_fences(_PLAN.read_text(encoding="utf-8")) if "SLUG=" in block)
    env = {**os.environ, "TMPDIR": tmp_path.as_posix(), "CLAUDE_CODE_SESSION_ID": "filename"}
    block = 'date() { printf "2026-01-02T03-04-05Z\\n"; }\n' + block
    first_run = subprocess.run([_BASH, "-c", block], cwd=tmp_path, env=env, capture_output=True, text=True, check=False)
    assert first_run.returncode == 0, first_run.stderr
    first_path = (namespace / "plan-file").read_text().strip()
    assert first_path == ".plans/active/plan-improve-caching-data-loader-2026-01-02T03-04-05Z.md"
    (tmp_path / first_path).write_text("original plan", encoding="utf-8")
    second_run = subprocess.run(
        [_BASH, "-c", block], cwd=tmp_path, env=env, capture_output=True, text=True, check=False
    )
    assert second_run.returncode == 0, second_run.stderr
    assert (namespace / "plan-file").read_text().strip() == first_path.removesuffix(".md") + "-2.md"
    assert (tmp_path / first_path).read_text() == "original plan"


def test_refactor_handoff_carries_target_before_goal() -> None:
    """A refactor receives a real scoped target so discovery cannot improvise a path."""
    lines = _PLAN.read_text(encoding="utf-8").splitlines()
    handoff = next(line for line in lines if line.startswith("-> /develop:refactor"))
    assert "<target> <goal> --plan <PLAN_FILE>" in handoff
    assert any("common parent" in line and "Affected files" in line for line in lines)


def test_final_revision_does_not_repeat_review() -> None:
    """The final revise option updates the saved plan then returns to the final gate without a fresh challenge."""
    lines = _PLAN.read_text(encoding="utf-8").splitlines()
    gate = next(line for line in lines if line.startswith("Before invoking `AskUserQuestion`, print the saved brief"))
    assert "edit `PLAN_FILE` in place" in gate
    assert "Step 5 again" in gate
    assert "no Step 4 re-run unless requested" in gate
    assert "return to Step 2" not in gate


def test_unknown_cause_recommends_debug_without_goal_text_counter() -> None:
    """A user-controlled diagnostic handoff never consumes a counter tied to the exact goal wording."""
    lines = _PLAN.read_text(encoding="utf-8").splitlines()
    route = next(line for line in lines if line.startswith("**Debug classification handoff**"))
    assert "skip Steps 2–5" in route
    assert "/develop:debug <goal>" in route
    assert not any("GOAL_KEY=" in line or "DEPTH_FILE=" in line or "At depth 0:" in line for line in lines)


def test_challenger_policy_describes_its_own_default() -> None:
    """The plan policy does not claim other skills use a size-based challenge shortcut."""
    lines = _PLAN.read_text(encoding="utf-8").splitlines()
    policy = next(line for line in lines if line.startswith("**Two states**"))
    assert "unlike fix/feature/refactor/debug" not in policy
    assert "by **default** challenger always reviews plan design" in policy

"""Verify consolidator dispatch references installed instructions without loading their body in the parent."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
from _develop_text import bash_blocks

_PLUGIN = Path(__file__).resolve().parents[2]
_SKILL = _PLUGIN / "skills/review/SKILL.md"
_BASH = shutil.which("bash")
_skip_no_bash = pytest.mark.skipif(_BASH is None, reason="bash unavailable")

#: Fallback wording each plugin keeps for an unavailable agent; the review skill is shared between plugin copies.
_FALLBACK_MARKERS = {
    "cc_oss": [
        "Do NOT read agent finding files inline",
        "Prior resolution ledger:",
        "CI_FAILING_CHECKS=<resolved CI failing-check names or empty>",
    ],
    "cc_develop": [
        "read the same instruction file and each agent finding file",
        "Missing instruction/report-template files block",
        "REVIEW_TEMPLATE=<resolved report-template path>",
    ],
}

#: (earlier, later) marker pairs whose order matters per plugin.
_FALLBACK_ORDER = {
    "cc_oss": [],
    "cc_develop": [("_REVIEW_TEMPLATE=", "Read <CONSOLIDATOR_INSTRUCTIONS_LITERAL>")],
}


def _dispatch() -> tuple[str, str]:
    """Return Step 5 and its actual instruction-path preparation recipe."""
    text = _SKILL.read_text(encoding="utf-8").split("## Step 5: Consolidate findings", 1)[1]
    text = text.split('TaskUpdate "Step 5b:', 1)[0]
    blocks = bash_blocks(text)
    block = next(block for block in blocks if "_CONSOLIDATOR_INSTRUCTIONS=" in block)
    return text, block


@dataclass
class _Recipe:
    """The preparation block plus the installed-plugin environment it runs against."""

    block: str
    env: dict[str, str]
    instructions: Path

    def run(self) -> subprocess.CompletedProcess[str]:
        """Execute the preparation block in bash against the temp install."""
        return subprocess.run(
            [_BASH, "-c", self.block], capture_output=True, encoding="utf-8", env=self.env, check=False
        )


@pytest.fixture
def recipe(tmp_path: Path) -> _Recipe:
    """Installed plugin tree with consolidator instructions, plus the Step 5 recipe pointed at it."""
    plugin = tmp_path / "installed-plugin"
    templates = plugin / "skills/review/templates"
    templates.mkdir(parents=True)
    instructions = templates / "consolidator-prompt.md"
    instructions.write_text("PRIVATE BODY MUST STAY IN CHILD CONTEXT\n", encoding="utf-8", newline="\n")
    (templates / "review-report.md").write_text("---\nTitle: Template\n---\n", encoding="utf-8", newline="\n")
    skill_dir = tmp_path / "review-skill-dir-dispatch-test"
    skill_dir.write_text((plugin / "skills/review").as_posix() + "\n", encoding="utf-8", newline="\n")
    env = {
        **os.environ,
        "CLAUDE_PLUGIN_ROOT": plugin.as_posix(),
        "CLAUDE_CODE_SESSION_ID": "dispatch-test",
        "TMPDIR": tmp_path.as_posix(),
    }
    return _Recipe(block=_dispatch()[1], env=env, instructions=instructions)


class TestInstructionPathRecipe:
    """The shipped bash recipe that resolves the consolidator instruction path, run for real against a temp install."""

    @_skip_no_bash
    def test_prints_references_only(self, recipe: _Recipe) -> None:
        """The ordinary preparation emits the instruction path but never the template body.

        The parent context must hold only a reference; the body is read by the consolidator child, so a leak here re-
        inflates the orchestrator's context on every review.
        """
        result = recipe.run()
        assert result.returncode == 0, result.stdout + result.stderr
        assert recipe.instructions.as_posix() in result.stdout
        assert "PRIVATE BODY" not in result.stdout

    @_skip_no_bash
    def test_blocks_when_installed_instructions_are_missing(self, recipe: _Recipe) -> None:
        """Missing installed instructions stop the recipe before dispatch with a restore hint.

        Dispatching a consolidator with no instruction file would produce an unguided report, so the recipe must fail
        closed and tell the user to restore or reinstall.
        """
        recipe.instructions.unlink()
        result = recipe.run()
        assert result.returncode != 0
        assert "BLOCKED" in result.stdout
        assert "restore/reinstall" in result.stdout


class TestDispatchProse:
    """The Step 5 prose that hands the consolidator its inputs."""

    def test_passes_instruction_literal_and_envelope_contract(self) -> None:
        """Step 5 passes a literal instruction path to read and demands a compact envelope back.

        The parent must name the path for the child to Read, supply the resolved date, and forbid inlining the template.
        """
        text, _ = _dispatch()
        assert (
            "Read <CONSOLIDATOR_INSTRUCTIONS_LITERAL> with the Read tool" in text
            or "Read the instruction file at <CONSOLIDATOR_INSTRUCTIONS_LITERAL> with the Read tool" in text
        )
        assert "DATE=<resolved date>" in text
        assert "Return ONLY" in text
        assert "run-dir resolution preamble" in text
        assert "Do NOT cat or inline" in text or "never cat or inline" in text

    def test_fallback_retains_owning_workflow_boundary(self) -> None:
        """Unavailable-agent guidance preserves each plugin's established fallback rather than granting new execution.

        Each plugin copy of the review skill keeps its own fallback wording; none may widen what the parent is allowed
        to read or run when a dispatched agent is unavailable.
        """
        text, _ = _dispatch()
        missing = [marker for marker in _FALLBACK_MARKERS[_PLUGIN.name] if marker not in text]
        misordered = [pair for pair in _FALLBACK_ORDER[_PLUGIN.name] if text.index(pair[0]) >= text.index(pair[1])]
        assert (missing, misordered) == ([], [])

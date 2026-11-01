"""Verify consolidator dispatch references installed instructions without loading their body in the parent.

The review skill's Step 5 prepares the consolidator's instruction path in a bash block and tells the parent to pass only
the path to the child agent. These tests run that real block against a fake installed plugin and check the surrounding
prose without depending on where it wraps.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parents[2]
_SKILL = _PLUGIN / "skills/review/SKILL.md"
_BASH = shutil.which("bash")
_SESSION = "dispatch-test"


def _dispatch() -> tuple[str, str]:
    """Return Step 5 (whitespace-flattened, so line wraps do not matter) and its instruction-path recipe."""
    text = _SKILL.read_text(encoding="utf-8").split("## Step 5: Consolidate findings", 1)[1]
    text = text.split('TaskUpdate "Step 5b:', 1)[0]
    blocks = re.findall(r"```bash\n(.*?)```", text, re.DOTALL)
    block = next(block for block in blocks if "_CONSOLIDATOR_INSTRUCTIONS=" in block)
    return " ".join(text.split()), block


@pytest.fixture
def installed_plugin(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """Create a fake installed plugin with the consolidator template; return its instruction file and the bash env."""
    plugin = tmp_path / "installed-plugin"
    templates = plugin / "skills/review/templates"
    templates.mkdir(parents=True)
    instructions = templates / "consolidator-prompt.md"
    instructions.write_text("PRIVATE BODY MUST STAY IN CHILD CONTEXT\n", encoding="utf-8", newline="\n")
    (templates / "review-report.md").write_text("---\nTitle: Template\n---\n", encoding="utf-8", newline="\n")
    skill_dir = tmp_path / f"review-skill-dir-{_SESSION}"
    skill_dir.write_text((plugin / "skills/review").as_posix() + "\n", encoding="utf-8", newline="\n")
    env = {
        **os.environ,
        "CLAUDE_PLUGIN_ROOT": plugin.as_posix(),
        "CLAUDE_CODE_SESSION_ID": _SESSION,
        "TMPDIR": tmp_path.as_posix(),
    }
    return instructions, env


@pytest.mark.skipif(_BASH is None, reason="bash unavailable")
class TestInstructionPathRecipe:
    """The Step 5 bash block, executed against a fake installed plugin."""

    def test_emits_the_reference_and_never_the_template_body(
        self, installed_plugin: tuple[Path, dict[str, str]]
    ) -> None:
        """The ordinary preparation prints the instruction file's path and nothing from inside it.

        The parent agent must hand the child a reference; echoing the template body would load it into the parent's
        context on every review, which is the cost this recipe exists to avoid.
        """
        instructions, env = installed_plugin
        _, block = _dispatch()
        result = subprocess.run([_BASH, "-c", block], capture_output=True, encoding="utf-8", env=env, check=False)
        assert result.returncode == 0, result.stdout + result.stderr
        assert instructions.as_posix() in result.stdout
        assert "PRIVATE BODY" not in result.stdout

    def test_missing_instructions_block_before_dispatch(self, installed_plugin: tuple[Path, dict[str, str]]) -> None:
        """Missing installed instructions stop the recipe with a BLOCKED line instead of dispatching without them.

        Scenario: the consolidator template was deleted after install. The recipe must fail closed and tell the user to
        restore or reinstall, rather than letting the child run on an empty prompt.
        """
        instructions, env = installed_plugin
        _, block = _dispatch()
        instructions.unlink()
        blocked = subprocess.run([_BASH, "-c", block], capture_output=True, encoding="utf-8", env=env, check=False)
        assert blocked.returncode != 0
        assert "BLOCKED" in blocked.stdout
        assert "restore/reinstall" in blocked.stdout


class TestStepFiveWording:
    """Step 5's prose keeps the dispatch contract: reference not body, resolved inputs, envelope-only return."""

    def test_dispatch_prompt_passes_a_reference(self) -> None:
        """The dispatch prompt tells the child to read the instruction file itself and to return only the envelope.

        The parent never cats or inlines the template, passes the resolved date, and asks the child to re-run the run-
        dir resolution preamble, so each of these phrases pins one part of that hand-off.
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
        """Unavailable-agent guidance preserves this plugin's established fallback rather than granting new execution.

        The fallback must still forbid reading finding files inline, carry the prior resolution ledger, and pass the CI
        failing-check names to the consolidator.
        """
        text, _ = _dispatch()
        assert "Do NOT read agent finding files inline" in text
        assert "Prior resolution ledger:" in text
        assert "CI_FAILING_CHECKS=<resolved CI failing-check names or empty>" in text

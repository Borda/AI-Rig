"""Verify consolidator dispatch references installed instructions without loading their body in the parent."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parents[1]
_SKILL = _PLUGIN / "skills/review/SKILL.md"
_BASH = shutil.which("bash")


def _dispatch() -> tuple[str, str]:
    """Return Step 5 and its actual instruction-path preparation recipe."""
    text = _SKILL.read_text(encoding="utf-8").split("## Step 5: Consolidate findings", 1)[1]
    text = text.split('TaskUpdate "Step 5b:', 1)[0]
    blocks = re.findall(r"```bash\n(.*?)```", text, re.DOTALL)
    block = next(block for block in blocks if "_CONSOLIDATOR_INSTRUCTIONS=" in block)
    return text, block


@pytest.mark.skipif(_BASH is None, reason="bash unavailable")
def test_instruction_path_recipe_is_fail_closed_and_never_prints_template_body(tmp_path: Path) -> None:
    """The ordinary preparation emits references only, and missing installed instructions stop before dispatch."""
    text, block = _dispatch()
    plugin = tmp_path / "installed-plugin"
    templates = plugin / "skills/review/templates"
    templates.mkdir(parents=True)
    instructions = templates / "consolidator-prompt.md"
    instructions.write_text("PRIVATE BODY MUST STAY IN CHILD CONTEXT\n", encoding="utf-8", newline="\n")
    (templates / "review-report.md").write_text("---\nTitle: Template\n---\n", encoding="utf-8", newline="\n")
    (tmp_path / "review-skill-dir-dispatch-test").write_text(
        (plugin / "skills/review").as_posix() + "\n", encoding="utf-8", newline="\n"
    )
    env = {
        **os.environ,
        "CLAUDE_PLUGIN_ROOT": plugin.as_posix(),
        "CLAUDE_CODE_SESSION_ID": "dispatch-test",
        "TMPDIR": tmp_path.as_posix(),
    }
    result = subprocess.run([_BASH, "-c", block], capture_output=True, encoding="utf-8", env=env, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert instructions.as_posix() in result.stdout
    assert "PRIVATE BODY" not in result.stdout
    assert (
        "Read <CONSOLIDATOR_INSTRUCTIONS_LITERAL> with the Read tool" in text
        or "Read the instruction file at <CONSOLIDATOR_INSTRUCTIONS_LITERAL> with the Read tool" in text
    )
    assert "DATE=<resolved date>" in text
    assert "Return ONLY" in text
    assert "run-dir resolution preamble" in text
    assert "Do NOT cat or inline" in text or "never cat or inline" in text
    instructions.unlink()
    blocked = subprocess.run([_BASH, "-c", block], capture_output=True, encoding="utf-8", env=env, check=False)
    assert blocked.returncode != 0
    assert "BLOCKED" in blocked.stdout
    assert "restore/reinstall" in blocked.stdout


def test_fallback_retains_owning_workflow_boundary() -> None:
    """Unavailable-agent guidance preserves each plugin's established fallback rather than granting new execution."""
    text, _ = _dispatch()
    if _PLUGIN.name == "cc_oss":
        assert "Do NOT read agent finding files inline" in text
        assert "Prior resolution ledger:" in text
        assert "CI_FAILING_CHECKS=<resolved CI failing-check names or empty>" in text
    else:
        assert "read the same instruction file and each agent finding file" in text
        assert "Missing instruction/report-template files block" in text
        assert "REVIEW_TEMPLATE=<resolved report-template path>" in text
        assert text.index("_REVIEW_TEMPLATE=") < text.index("Read <CONSOLIDATOR_INSTRUCTIONS_LITERAL>")

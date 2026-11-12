"""Exercise shared plan extraction with the calling skill's literal invocation data."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from _develop_text import bash_blocks

_PLUGIN = Path(__file__).resolve().parents[2]
_BASH = shutil.which("bash")
_skip_no_bash = pytest.mark.skipif(_BASH is None, reason="bash unavailable")


def _preflight_block() -> str:
    """Return the installed shared plan extraction recipe."""
    text = (_PLUGIN / "skills/_shared/preflight-helpers.md").read_text(encoding="utf-8")
    return next(block for block in bash_blocks(text) if "parse-skill-flags.py" in block)


def _run(block: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Run the shipped recipe with the current project interpreter."""
    env = {
        **os.environ,
        "CLAUDE_PLUGIN_ROOT": str(_PLUGIN),
        "CLAUDE_CODE_SESSION_ID": "preflight-input",
        "TMPDIR": str(tmp_path),
        "TEST_PYTHON": sys.executable,
    }
    prefix = 'python() { command "$TEST_PYTHON" "$@"; }\n'
    return subprocess.run(
        [_BASH, "-c", prefix + block], cwd=tmp_path, env=env, capture_output=True, text=True, check=False, timeout=10
    )


@_skip_no_bash
@pytest.mark.parametrize("skill", ["feature", "fix", "refactor"])
def test_shared_plan_extraction_uses_callers_literal_input(tmp_path: Path, skill: str) -> None:
    """Plan filenames containing shell syntax stay data across the shared loader boundary."""
    plan = tmp_path / "plan $(touch escaped).md"
    plan.write_text("# Existing plan\n", encoding="utf-8")
    input_file = tmp_path / f"dev-{skill}-arguments-preflight-input.txt"
    input_file.write_text(f"--plan {shlex.quote(str(plan))}\n", encoding="utf-8")
    text = (_PLUGIN / f"skills/{skill}/SKILL.md").read_text(encoding="utf-8")
    parsing = next(block for block in bash_blocks(text) if "extract-keep-flag.py" in block)
    lines = parsing.splitlines(keepends=True)
    load_end = next(index for index, line in enumerate(lines) if line.startswith("ARGUMENT_BLOB=${ARGUMENT_BLOB%x}"))
    loader = "".join(lines[: load_end + 1])
    result = _run(loader + _preflight_block(), tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "dev-plan-file-preflight-input").read_text(encoding="utf-8").strip() == str(plan)
    assert not (tmp_path / "escaped").exists()


@_skip_no_bash
def test_shared_plan_extraction_requires_loaded_invocation(tmp_path: Path) -> None:
    """A separate Bash call cannot silently parse an unset invocation as an empty plan."""
    result = _run("unset ARGUMENT_BLOB\n" + _preflight_block(), tmp_path)
    assert result.returncode == 1
    assert "invocation input" in result.stdout
    assert not (tmp_path / "dev-plan-file-preflight-input").exists()

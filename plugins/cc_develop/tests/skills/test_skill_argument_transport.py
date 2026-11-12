"""Exercise invocation text as staged data across the develop skill shell boundary."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parents[2]
_BASH = shutil.which("bash")
_SESSION = "argument-transport"
_requires_bash = pytest.mark.skipif(_BASH is None, reason="documented shell recipes require bash")


def _blocks(skill: str) -> list[str]:
    """Read fenced shell commands line by line without depending on prose wrapping."""
    blocks: list[str] = []
    current: list[str] | None = None
    fence = ""
    for line in (_PLUGIN / "skills" / skill / "SKILL.md").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if current is None and stripped.startswith("```") and stripped.endswith("bash"):
            fence = stripped.removesuffix("bash")
            current = []
        elif current is not None and stripped == fence:
            blocks.append("\n".join(current))
            current = None
        elif current is not None:
            current.append(line)
    return blocks


def _run(skill: str, tmp_path: Path, payload: str, setup: str = "") -> subprocess.CompletedProcess[str]:
    """Run the shipped parser recipe with the same textual substitution an LLM host performs."""
    (tmp_path / f"dev-{skill}-arguments-{_SESSION}.txt").write_text(payload, encoding="utf-8", newline="\n")
    block = next(block for block in _blocks(skill) if "/bin/extract-keep-flag.py" in block)
    env = {
        **os.environ,
        "CSID": _SESSION,
        "CLAUDE_CODE_SESSION_ID": _SESSION,
        "CLAUDE_PLUGIN_ROOT": str(_PLUGIN),
        "TMPDIR": str(tmp_path),
        "PATH": os.pathsep.join((str(Path(sys.executable).parent), os.environ.get("PATH", ""))),
    }
    return subprocess.run(
        [_BASH or "bash", "-c", setup + block.replace("$ARGUMENTS", payload)],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )


@_requires_bash
@pytest.mark.parametrize("skill", ["debug", "feature", "fix", "refactor", "plan", "review"])
def test_host_argument_substitution_cannot_execute_a_shell_command(skill: str, tmp_path: Path) -> None:
    """A quote in user text cannot escape a documented parser invocation and execute another command."""
    result = _run(skill, tmp_path, '"; printf injected > injected; #')
    assert not (tmp_path / "injected").exists(), (result.stdout, result.stderr)
    assert result.returncode == 0, (result.stdout, result.stderr)


@_requires_bash
@pytest.mark.parametrize("skill", ["debug", "feature", "fix", "refactor", "plan", "review"])
def test_parser_receives_literal_text_and_trailing_newlines(skill: str, tmp_path: Path) -> None:
    """Reading staged input preserves shell syntax as data, including every trailing newline."""
    payload = '`printf backtick` $(printf dollar) "quoted"\nnext line\n\n'
    result = _run(skill, tmp_path, payload, 'python() { printf "%s\\0" "$@" > parser-argv; }\n')
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert (tmp_path / "parser-argv").read_bytes().split(b"\0")[-2] == payload.encode()


@pytest.mark.parametrize("skill", ["debug", "feature", "fix", "refactor", "plan", "review"])
def test_all_executable_recipes_keep_host_argument_tokens_out_of_shell_text(skill: str) -> None:
    """Every executable consumer uses the staged runtime value, including late Codemap routes."""
    assert not any("$ARGUMENTS" in line for block in _blocks(skill) for line in block.splitlines())

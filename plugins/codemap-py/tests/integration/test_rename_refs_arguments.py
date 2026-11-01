"""Regression tests for ``rename-refs`` Step 0: documented argument forms survive the skill's shell wrapper.

Claude Code pastes ``$ARGUMENTS`` as text into the Step 0 bash block, inside the double-quoted word handed to
``parse_rename_args.py``. A documented ``--deprecate="@deprecated_class(...)"`` form ended that word early, and the
``(`` after it made bash stop on a syntax error before the helper ran, so the documented form could never work. These
tests run the real Step 0 block with the arguments substituted the same way.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from _host_skips import needs_git_and_bash

_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_SKILL = _PLUGIN_ROOT / "claude-skills" / "rename-refs" / "SKILL.md"
_HINT_PREFIX = "argument-hint: "
_SINGLE_QUOTED_DECORATOR = re.compile(r"'(@[^']*)'")
_CSID = "sess1"


def _bash_blocks(text: str) -> list[str]:
    """Return the body of every fenced ``bash`` block, scanning line by line so prose wrapping is irrelevant."""
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in text.splitlines(keepends=True):
        if current is None:
            if line.strip() == "```bash":
                current = []
        elif line.strip() == "```":
            blocks.append(current)
            current = None
        else:
            current.append(line)
    return ["".join(block) for block in blocks]


def _argument_hint() -> str:
    """Return the ``argument-hint`` front-matter value of the skill."""
    lines = _SKILL.read_text(encoding="utf-8").splitlines()
    return next(line[len(_HINT_PREFIX) :] for line in lines if line.startswith(_HINT_PREFIX))


def _hint_decorators() -> list[str]:
    """Return the single-quoted decorator examples shown in the argument hint."""
    return _SINGLE_QUOTED_DECORATOR.findall(_argument_hint())


@pytest.fixture
def run_step0(tmp_path: Path):
    """Return a runner that substitutes arguments into the Step 0 block and runs it in a git project ``proj``."""
    repo = tmp_path / "proj"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, timeout=30)
    sentinels = tmp_path / "tmp"
    sentinels.mkdir()
    block = next(block for block in _bash_blocks(_SKILL.read_text(encoding="utf-8")) if "parse_rename_args.py" in block)
    env = {
        **os.environ,
        "TMPDIR": str(sentinels),
        "CLAUDE_CODE_SESSION_ID": _CSID,
        "CLAUDE_PLUGIN_ROOT": str(_PLUGIN_ROOT),
        # the block calls bare ``python``; put this interpreter first so the run never depends on the host PATH
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}",
    }

    def run(arguments: str) -> tuple[subprocess.CompletedProcess[str], Path]:
        script = block.replace("$ARGUMENTS", arguments)
        result = subprocess.run(["bash", "-c", script], cwd=repo, env=env, capture_output=True, text=True, timeout=60)
        return result, sentinels

    return run


def test_argument_hint_documents_single_quoted_decorators() -> None:
    """The hint shows both decorator forms in single quotes and lists ``--no-deprecate``.

    A double-quoted value in the hint is the form users copy; it always failed inside the skill's wrapper, so the hint
    must only teach the single-quoted forms.
    """
    hint = _argument_hint()

    assert _hint_decorators() == ["@deprecated(...)", "@deprecated_class(...)"]
    assert '="' not in hint
    assert "--no-deprecate" in hint


@needs_git_and_bash
class TestStep0:
    """The skill's real Step 0 bash block run with ``$ARGUMENTS`` pasted in as text."""

    @pytest.mark.parametrize(
        "decorator",
        [
            *[pytest.param(value, id=f"hint-{value.split('(')[0].lstrip('@')}") for value in _hint_decorators()],
            pytest.param("@deprecated_class(target=NewName, deprecated_in=1.0)", id="spaces-and-parens"),
        ],
    )
    def test_parses_single_quoted_decorator(self, run_step0, decorator: str) -> None:
        """A single-quoted ``--deprecate`` value reaches ``parse_rename_args.py`` intact and lands in its sentinel.

        Each decorator form shown in the hint, plus one with spaces and parentheses, is pasted as ``$ARGUMENTS``; the
        helper must write the value back verbatim, proving the wrapper quoting did not split or mangle it.
        """
        result, sentinels = run_step0(f"symbol Old New --deprecate='{decorator}' --since 1.0")

        assert result.returncode == 0, result.stderr
        sentinel = sentinels / f"codemap-proj-rename-DEPRECATE_DECORATOR-{_CSID}"
        assert sentinel.read_text(encoding="utf-8") == f"{decorator}\n"

    def test_double_quoted_decorator_fails_before_the_helper(self, run_step0) -> None:
        """A double-quoted decorator with parentheses is a bash syntax error, so no sentinel is ever written.

        This is the failure the Step 0 quoting guard exists to stop before the block runs: the pasted ``"`` closes the
        wrapper's own double-quoted word and the ``(`` that follows is a syntax error.
        """
        result, sentinels = run_step0('symbol Old New --deprecate="@deprecated_class(target=New)"')

        assert result.returncode != 0
        assert list(sentinels.iterdir()) == []

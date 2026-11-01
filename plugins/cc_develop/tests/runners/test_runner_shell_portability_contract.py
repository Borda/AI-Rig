"""Contract tests pinning the zsh shell-portability fix across develop skills.

Claude Code's Bash tool runs under the user's login shell — zsh on macOS by default. Confirmed empirically this session:
zsh does not word-split a bare ``"$VAR"`` (only unquoted command substitution), and has no bash-style ``PIPESTATUS``
(only a differently-indexed lowercase ``$pipestatus``). A bare ``$PYTEST_CMD``/``$TEST_CMD`` (e.g. "uv run pytest")
invoked as a command prefix fails with "command not found: uv run pytest" under zsh; a bare ``${PIPESTATUS[0]}`` after a
pipe reads empty.

These are static assertions over the markdown source, not a bash-execution harness — see
``test_quality_stack_retry_contract.py`` in the foundry plugin for the same trade-off and its rationale.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from _develop_text import bash_fences

_DEVELOP = Path(__file__).resolve().parents[2]
_SKILLS = {
    "feature": _DEVELOP / "skills" / "feature" / "SKILL.md",
    "fix": _DEVELOP / "skills" / "fix" / "SKILL.md",
    "refactor": _DEVELOP / "skills" / "refactor" / "SKILL.md",
    "debug": _DEVELOP / "skills" / "debug" / "SKILL.md",
    "_shared/runner-detection.md": _DEVELOP / "skills" / "_shared" / "runner-detection.md",
}

# Matches only the bracket clause itself (e.g. `[ -n "$PYTEST_CMD" ]`), not the whole line — a
# line combining an emptiness check with a bare invocation (`[ -n "$X" ] && $X ...`) must still
# be caught for the invocation half once the check half is stripped out.
_EMPTINESS_CLAUSE = re.compile(r'\[\s+-[zn]\s+"\$(?:PYTEST_CMD|TEST_CMD)"\s*\]')

# A var only reads as a *command prefix* — the zsh word-splitting hazard — at the start of a
# command: line start, after a command separator (&&, ||, ;, a single pipe |), after a
# command-substitution/subshell/group open ($(, (, {, a backtick), after if/while/elif/then/
# do/else, after a leading !, or after `time`. Any other position (inside `echo "$VAR"`,
# inside an already-`eval`'d string) is a value, not an invocation, and is not flagged. The
# previous whole-line substring scan couldn't distinguish the two. Round-4 review (R3) named
# `time`/backtick as latent gaps (`$( )` was caught, backticks were not) — closed here. A
# `case`-branch `)` (e.g. `a) $PYTEST_CMD …`) stays a documented, undosed nit: a bare `)`
# trigger risks a false positive on `$(cmd) $PYTEST_CMD` (an argument position, not a command
# start), and no live occurrence of the case-branch form exists in any scanned file.
_COMMAND_PREFIX = re.compile(
    r"(?:^|&&|\|\||;|\||\$\(|\(|\{|`|!|\btime\b|\bif\b|\bwhile\b|\belif\b|\bthen\b|\bdo\b|\belse\b)"
    r"\s*(\$PYTEST_CMD|\$TEST_CMD)\b"
)


def _text(skill: str) -> str:
    """Read a skill's SKILL.md (or shared doc) content once per call."""
    return _SKILLS[skill].read_text(encoding="utf-8")


def _bash_blocks(skill: str) -> list[str]:
    """Return the fenced ``bash`` blocks of one skill file."""
    return bash_fences(_text(skill))


def _bare_invocations(skill: str) -> list[str]:
    """Return the bash lines that invoke ``$PYTEST_CMD``/``$TEST_CMD`` bare as a command prefix.

    Comment lines and the ``run_pytest_short.py``/``pytest_gate.py`` call shapes are skipped, and the emptiness clause
    is stripped before scanning. A match is exempt only when ``eval "`` immediately precedes that match's own offset, so
    an earlier eval-wrapped use on the same line cannot launder a later bare one.
    """
    lines = [line for block in _bash_blocks(skill) for line in block.splitlines()]
    scannable = [
        line
        for line in lines
        if not line.strip().startswith("#") and "run_pytest_short.py" not in line and "pytest_gate.py" not in line
    ]
    return [
        line
        for line in scannable
        for match in _COMMAND_PREFIX.finditer(_EMPTINESS_CLAUSE.sub("", line))
        if not _EMPTINESS_CLAUSE.sub("", line)[: match.start(1)].endswith('eval "')
    ]


def _blocks_missing_pipefail(skill: str) -> list[str]:
    """Return the bash blocks that pipe into an exit-code check without ``set -o pipefail``.

    A rough proxy: a block combining a pipe with a same-or-next-line ``*_EXIT=$?`` assignment must contain
    ``set -o pipefail``.
    """
    return [
        block
        for block in _bash_blocks(skill)
        if (re.search(r"\|[^\n]*\n?[^\n]*=\$\?", block) or re.search(r"\|.*;\s*\w+_EXIT=\$\?", block))
        and "set -o pipefail" not in block
    ]


@pytest.mark.parametrize("skill", list(_SKILLS))
def test_no_bare_pytest_cmd_or_test_cmd_command_prefix(skill: str) -> None:
    """``$PYTEST_CMD``/``$TEST_CMD`` is never invoked bare as a command prefix.

    The exceptions are the ``run_pytest_short.py "$PYTEST_CMD" <target>`` and ``pytest_gate.py "$PYTEST_CMD" <target>``
    call shapes, where ``$PYTEST_CMD`` is a single quoted argv string handed to a Python script that does its own
    ``shlex.split`` — zsh-safe by construction, excluded here. Only lines inside fenced ``bash`` blocks are checked —
    prose mentioning the variable name (e.g. "Sets `$PYTEST_CMD` (pytest flags)") is not an invocation, nor is a comment
    line. The emptiness clause is stripped from each line, not the whole line skipped, so a line combining an emptiness
    check with a bare invocation (`` [ -n "$PYTEST_CMD" ] && $PYTEST_CMD --help `` ) still fails on the invocation half;
    only a command-start occurrence of the var counts as an invocation at all — `` echo "$VAR" `` or a use already
    inside an `` eval "..." `` string is a value, not a bare prefix.
    """
    assert _bare_invocations(skill) == []


@pytest.mark.parametrize("skill", list(_SKILLS))
def test_no_pipestatus_array_syntax(skill: str) -> None:
    """``${PIPESTATUS[...]}`` is gone — replaced by ``set -o pipefail`` + ``$?``.

    zsh has no bash-style ``PIPESTATUS``, so any remaining array read would come back empty on macOS.
    """
    assert "${PIPESTATUS" not in _text(skill)


@pytest.mark.parametrize("skill", list(_SKILLS))
def test_pipefail_present_where_pipes_check_exit_code(skill: str) -> None:
    """Every pipe-then-check-exit-code block sets ``pipefail`` first.

    Without ``pipefail`` the exit code after a pipe is the last stage's, so a failing pytest piped into ``tail`` would
    read as success.
    """
    assert _blocks_missing_pipefail(skill) == []

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

_DEVELOP = Path(__file__).resolve().parent.parent
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
    text = _text(skill)
    blocks = re.findall(r"```bash\n(.*?)\n```", text, re.DOTALL)
    for block in blocks:
        for line in block.splitlines():
            if line.strip().startswith("#"):
                continue
            if "run_pytest_short.py" in line or "pytest_gate.py" in line:
                continue
            scan_line = _EMPTINESS_CLAUSE.sub("", line)
            for match in _COMMAND_PREFIX.finditer(scan_line):
                var = match.group(1)
                # Match-scoped, not line-scoped: an earlier eval-wrapped occurrence on the same
                # line must not launder a later bare one (`eval "$PYTEST_CMD --help" &&
                # $PYTEST_CMD --version` — the second use is still bare). The var only reads as
                # eval-wrapped when `eval "` immediately precedes *this* match's own offset.
                assert scan_line[: match.start(1)].endswith('eval "'), (
                    f"{skill}: bare {var} used as a command prefix without eval: {line!r}"
                )


@pytest.mark.parametrize("skill", list(_SKILLS))
def test_no_pipestatus_array_syntax(skill: str) -> None:
    """``${PIPESTATUS[...]}`` is gone — replaced by ``set -o pipefail`` + ``$?``."""
    text = _text(skill)
    assert "${PIPESTATUS" not in text


@pytest.mark.parametrize("skill", list(_SKILLS))
def test_pipefail_present_where_pipes_check_exit_code(skill: str) -> None:
    """Every pipe-then-check-exit-code block sets ``pipefail`` first.

    A rough proxy: any line combining a pipe with a same-or-next-line
    ``GATE_EXIT=$?``/``PYTEST_EXIT=$?``/``COLLECT_EXIT=$?`` assignment must be
    preceded (within the same fenced block) by ``set -o pipefail``.
    """
    text = _text(skill)
    blocks = re.findall(r"```bash\n(.*?)\n```", text, re.DOTALL)
    for block in blocks:
        has_pipe_then_exit_check = re.search(r"\|[^\n]*\n?[^\n]*=\$\?", block) or re.search(
            r"\|.*;\s*\w+_EXIT=\$\?", block
        )
        if has_pipe_then_exit_check:
            assert "set -o pipefail" in block, f"{skill}: pipe+exit-check block missing pipefail:\n{block}"

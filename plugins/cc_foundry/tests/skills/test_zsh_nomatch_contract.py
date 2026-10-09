"""Shipped bash blocks must not lose a whole command to zsh's NOMATCH abort.

Claude Code runs Bash tool commands in the user's login shell, which is zsh on a default macOS install. zsh aborts a
command whose filename pattern matches nothing (``no matches found``) instead of passing the pattern through as bash
does. When one command lists a glob under an optional directory (``.claude/skills/*/SKILL.md`` in a plugin-only
checkout) beside operands that do exist, that abort drops the existing operands too: the check scans nothing and reports
clean. The static scan below rejects that shape in every shipped bash fence; the run test executes the audit's Check 23c
block under each available shell in a plugin-only tree and requires the finding it exists to report.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

_PLUGINS = Path(__file__).resolve().parents[3]
_CHECKS_SKILLS = _PLUGINS / "cc_foundry" / "skills" / "audit" / "templates" / "checks-skills.md"
# Claude-loaded plugin markdown; Codex Rig ships no bash blocks run through the Claude Bash tool.
_SHIPPED_DIRS = ("skills", "agents", "rules")
_FENCE_OPEN = re.compile(r"^\s*(`{3,})(?:bash|sh|shell|zsh)\s*$")
# Directories a normal checkout may lack: a project's own `.claude/` tree, and the optional Codex manifest of a plugin.
_OPTIONAL_GLOB = re.compile(r"^(?:\.claude/\S*[*?[]|\S*/\.codex-plugin/\S*|\S*[*?[]\S*/\.codex-plugin/)")
_PATH_OPERAND = re.compile(r"^(?:\.claude/|plugins/|\.claude$)")


def _code_outside_quotes(line: str) -> str:
    """Return ``line`` with quoted spans, ``$(...)`` substitutions and a trailing comment blanked out.

    A pattern inside quotes is never expanded, and a NOMATCH abort inside a substitution ends only that substitution.
    """
    out: list[str] = []
    quote = ""
    depth = 0
    index = 0
    while index < len(line):
        char = line[index]
        if quote:
            quote = "" if char == quote else quote
            out.append(" ")
        elif char in "'\"":
            quote = char
            out.append(" ")
        elif line.startswith("$(", index):
            depth += 1
            out.append("  ")
            index += 1
        elif char == ")" and depth:
            depth -= 1
            out.append(" ")
        elif depth:
            out.append(" ")
        elif char == "#" and (index == 0 or line[index - 1].isspace()):
            break
        else:
            out.append(char)
        index += 1
    return "".join(out)


def _fenced_bash_lines(markdown: Path) -> list[tuple[int, str]]:
    """Return ``(line number, text)`` for every line inside a bash fence of ``markdown``."""
    lines: list[tuple[int, str]] = []
    fence = ""
    for number, text in enumerate(markdown.read_text(encoding="utf-8").splitlines(), 1):
        opened = _FENCE_OPEN.match(text)
        if not fence and opened:
            fence = opened.group(1)
        elif fence and text.strip().startswith(fence) and not text.strip().strip("`"):
            fence = ""
        elif fence:
            lines.append((number, text))
    return lines


def _mixed_operand_globs() -> list[str]:
    """Return ``file:line`` for each fenced command mixing an optional-directory glob with other path operands."""
    hits: list[str] = []
    for plugin in sorted(_PLUGINS.glob("cc_*")):
        for subdir in _SHIPPED_DIRS:
            for markdown in sorted((plugin / subdir).rglob("*.md")):
                for number, text in _fenced_bash_lines(markdown):
                    words = [word.strip(";|&<>") for word in _code_outside_quotes(text).split()]
                    optional = [word for word in words if _OPTIONAL_GLOB.match(word)]
                    operands = [word for word in words if _PATH_OPERAND.match(word)]
                    if optional and len(operands) >= 2:
                        hits.append(f"{markdown.relative_to(_PLUGINS).as_posix()}:{number}: {' '.join(operands)}")
    return hits


def test_no_command_mixes_an_optional_glob_with_other_operands() -> None:
    """No shipped bash command lists a glob under an optional directory beside other path operands.

    Under zsh the unmatched glob aborts the whole command, so the operands that do exist are never read and the check
    reports clean. A ``find`` over the existing directories reads every present file under both shells.
    """
    assert _mixed_operand_globs() == []


def _check_23c_block() -> str:
    """Return the bash block of the audit's Sub-check 23c."""
    text = _CHECKS_SKILLS.read_text(encoding="utf-8")
    section = text.split("### Sub-check 23c", 1)[1]
    return section.split("```bash\n", 1)[1].split("\n```", 1)[0]


@pytest.fixture(name="plugin_only_tree")
def _plugin_only_tree(tmp_path: Path) -> Path:
    """Create a checkout holding one plugin skill that evals a script's output and no ``.claude/skills/`` directory."""
    skill = tmp_path / "plugins" / "demo" / "skills" / "probe" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text('VALUES="$(true)"\neval "$(python "$ROOT/bin/emit_values.py")"\n', encoding="utf-8")
    (tmp_path / ".claude").mkdir()
    return tmp_path


@pytest.mark.parametrize(
    "shell",
    [
        pytest.param(
            "bash", marks=pytest.mark.skipif(shutil.which("bash") is None, reason="requires bash to run the block")
        ),
        pytest.param(
            "zsh", marks=pytest.mark.skipif(shutil.which("zsh") is None, reason="requires zsh to run the block")
        ),
    ],
)
def test_check_23c_reports_plugin_finding_without_project_skills(shell: str, plugin_only_tree: Path) -> None:
    """Check 23c reports a plugin skill's eval finding in a checkout that has no ``.claude/skills/`` directory.

    This is the normal plugin-only case. Before the fix the block's ``.claude/skills/*/SKILL.md`` glob matched nothing,
    zsh aborted the grep, and the check reported clean while the plugin skill held the finding.
    """
    proc = subprocess.run(
        [shell, "-c", _check_23c_block()],
        cwd=plugin_only_tree,
        capture_output=True,
        encoding="utf-8",
        timeout=30,
    )
    assert "plugins/demo/skills/probe/SKILL.md:2:" in proc.stdout, proc.stderr

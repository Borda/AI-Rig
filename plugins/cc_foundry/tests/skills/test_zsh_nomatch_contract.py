"""Shipped bash blocks must not lose a whole command to zsh's NOMATCH abort.

Claude Code runs Bash tool commands in the user's login shell, which is zsh on a default macOS install. zsh aborts a
command whose filename pattern matches nothing (``no matches found``) instead of passing the pattern through as bash
does. When one command lists a glob under an optional directory (``.claude/skills/*/SKILL.md`` in a plugin-only
checkout) beside operands that do exist, that abort drops the existing operands too: the check scans nothing and reports
clean. The static scan below rejects that shape in every shipped bash fence; the run tests execute the audit's Check 23c
and 23e blocks under each available shell in a plugin-only tree and require the findings they exist to report.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from _hook_env import _bash_runs_posix_script

_PLUGINS = Path(__file__).resolve().parents[3]
_CHECKS_SKILLS_BASH = _PLUGINS / "cc_foundry" / "skills" / "audit" / "templates" / "checks-skills-bash.md"
# Claude-loaded plugin markdown; Codex Rig ships no bash blocks run through the Claude Bash tool.
_SHIPPED_DIRS = ("skills", "agents", "rules")
_FENCE_OPEN = re.compile(r"^\s*(`{3,})(?:bash|sh|shell|zsh)\s*$")
# Directories a normal checkout may lack: a project's own `.claude/` tree, and the optional Codex manifest of a plugin.
_OPTIONAL_GLOB = re.compile(r"^(?:\.claude/\S*[*?[]|\S*/\.codex-plugin/\S*|\S*[*?[]\S*/\.codex-plugin/)")
_PATH_OPERAND = re.compile(r"^(?:\.claude/|plugins/|\.claude$)")

_both_shells = pytest.mark.parametrize(
    "shell",
    [
        pytest.param(
            "bash",
            marks=pytest.mark.skipif(not _bash_runs_posix_script(), reason="requires Bash executing POSIX scripts"),
        ),
        pytest.param(
            "zsh", marks=pytest.mark.skipif(shutil.which("zsh") is None, reason="requires zsh to run the block")
        ),
    ],
)


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


def _check_block(sub_check: str) -> str:
    """Return the bash block of the audit's ``Sub-check <sub_check>`` section."""
    lines = _CHECKS_SKILLS_BASH.read_text(encoding="utf-8").splitlines()
    start = lines.index(next(line for line in lines if line.startswith(f"### Sub-check {sub_check}")))
    body = lines[start + 1 :]
    first = body.index("```bash") + 1
    return "\n".join(body[first : first + body[first:].index("```")])


@pytest.fixture
def probe_tree(tmp_path: Path) -> Callable[[str, str], Path]:
    """Return a factory placing one file under ``plugins/demo/skills/probe/`` in a plugin-only checkout.

    The checkout has an empty ``.claude/`` directory and no ``.claude/skills/``, the normal plugin-only layout.
    """

    def _place(relative: str, text: str) -> Path:
        """Write ``text`` to the demo skill's ``relative`` path and return the checkout root."""
        target = tmp_path / "plugins" / "demo" / "skills" / "probe" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        (tmp_path / ".claude").mkdir(exist_ok=True)
        return tmp_path

    return _place


def _run_block(shell: str, block: str, cwd: Path, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    """Run an audit check ``block`` in ``shell`` from ``cwd`` and capture its text output."""
    return subprocess.run([shell, "-c", block], cwd=cwd, capture_output=True, encoding="utf-8", timeout=timeout)


def _flagged_lines(proc: subprocess.CompletedProcess[str]) -> list[int]:
    """Return the sorted line numbers of every ``plugins/...:<line>:`` finding the block printed."""
    return sorted(int(line.split(":")[1]) for line in proc.stdout.splitlines() if line.startswith("plugins/"))


def test_no_optional_glob_beside_other_operands() -> None:
    """No shipped bash command lists a glob under an optional directory beside other path operands.

    Under zsh the unmatched glob aborts the whole command, so the operands that do exist are never read and the check
    reports clean. A ``find`` over the existing directories reads every present file under both shells.
    """
    assert _mixed_operand_globs() == []


class TestCheck23c:
    """Audit Sub-check 23c reports ``eval "$(...)"`` lines found in fenced code of plugin skills."""

    @_both_shells
    @pytest.mark.parametrize(
        ("skill_text", "expected"),
        [
            pytest.param(
                'Prose: `eval "$(python /bin/prose.py)"`\n'
                '~~~bash\neval "$(python /bin/tilde.py)"\n~~~\n'
                '> ```bash\n> eval "$(python /bin/quoted.py)"\n> ```\n',
                ["tilde.py", "quoted.py"],
                # Prose quoting eval stays out of the findings; a parser knowing only line-start backtick fences would
                # miss the other two valid code blocks entirely.
                id="tilde-and-blockquoted-fences-not-prose",
            ),
            pytest.param(
                '````bash\nNOTE="\n```\n"\neval "$(python /bin/long.py)"\n````\n'
                '> ```bash\n> # eval "$(python /bin/comment.py)"\n> ```\n',
                ["long.py"],
                # A four-backtick block may hold a bare three-backtick line inside a quoted string; ending the fence
                # there would hide every later line. A comment inside a blockquoted fence is still a comment.
                id="long-fence-stays-open-and-quoted-comment-skipped",
            ),
        ],
    )
    def test_reads_code_fences_not_prose(
        self, shell: str, probe_tree: Callable[[str, str], Path], skill_text: str, expected: list[str]
    ) -> None:
        """Check 23c scans fenced code of every valid fence style and ignores prose and comments.

        Scanning only fenced code keeps prose such as ``Skill runs `eval "$(…)"` `` out of the findings, while a fence
        parser that is too narrow would miss valid code blocks and report clean.
        """
        tree = probe_tree("SKILL.md", skill_text)
        proc = _run_block(shell, _check_block("23c"), tree)
        scanned = ["tilde.py", "quoted.py", "prose.py", "long.py", "comment.py"]
        assert [name for name in scanned if name in proc.stdout] == expected, proc.stderr

    @_both_shells
    def test_reports_plugin_finding_without_project_skills(
        self, shell: str, probe_tree: Callable[[str, str], Path]
    ) -> None:
        """Check 23c reports a plugin skill's eval finding in a checkout that has no ``.claude/skills/`` directory.

        This is the normal plugin-only case. Before the fix the block's ``.claude/skills/*/SKILL.md`` glob matched
        nothing, zsh aborted the grep, and the check reported clean while the plugin skill held the finding.
        """
        # Check 23c scans only fenced code, where a skill's executable lines live
        tree = probe_tree("SKILL.md", '```bash\nVALUES="$(true)"\neval "$(python "$ROOT/bin/emit_values.py")"\n```\n')
        proc = _run_block(shell, _check_block("23c"), tree)
        assert "plugins/demo/skills/probe/SKILL.md:3:" in proc.stdout, proc.stderr


class TestCheck23e:
    """Audit Sub-check 23e reports command-name rebinds in shell fences."""

    @_both_shells
    @pytest.mark.parametrize(
        ("mode_text", "flagged"),
        [
            pytest.param(
                "```bash\ngrep -E '(^|/)(skills|commands|hooks)/' paths.txt\n"
                "'hash' -p /bin/echo probe\ntypeset 'commands[probe]=/bin/echo'\n"
                "set -Acommands probe /bin/echo\ngrep -E 'commands'\"[probe]=/bin/echo\"\n"
                "grep -E $'commands'\ntypeset prefix=\"| grep -E \" 'commands[probe]=/bin/echo'\n"
                "grep -E 'commands'(e:'PATH=/tmp':)\n```\n",
                [3, 4, 5, 6, 7, 8, 9],
                id="quoted-grep-data-preserves-live-and-concatenated-operands",
            ),
            pytest.param(
                "```bash\nset -Acommands ruff /bin/echo\nx=1; hash -p /bin/rm ruff\n: ${(P)n::=/bin/rm}\n"
                "typeset -A aliases\necho hi  # hash later\ncommand -v uv\nls docs/commands.md\n```\n"
                "```text\nfunctions to change\n```\n"
                "> ```zsh\n> alias ruff=rm\n> ```\n",
                [2, 3, 4, 5, 14],
                # Every rebind spelling the blueprint danger filter denies by name must surface here too, so an author
                # learns why the block prompts; `command -v`, a `commands.md` path, a trailing comment and a `text`
                # fence must stay quiet.
                id="rebinds-in-shell-fences-only",
            ),
            pytest.param(
                "```bash\nset -A names x y\nread -a names\nprintf -v target %s value\ndeclare -n ref=target\n"
                'typeset "$n=value"\ncommand hash -p /bin/echo probe\n\\hash -p /bin/echo probe\n'
                'FOO=1 hash -p /bin/echo probe\necho " # literal"; hash -p /bin/echo probe\n'
                "timeout 5 set -A x y\n2>/dev/null set -A x y\nprint -u 2 -v x y\n"
                'printf "  Python: %s\\n" "$(python --version 2>&1)"\n'
                'IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/r-${CSID}" 2>/dev/null || RUN_DIR=""\n'
                'local x="$1"\nset -euo pipefail\ninteger -n ref\necho "a \\" # literal"; hash -p /bin/echo probe\n'
                "print -f '%s %s' -v target /bin/echo\nh$'\\x61'sh -p /bin/echo probe\nha\\\nsh -p /bin/echo probe\n"
                "printf '%s\\n' -v\nX=a$b; echo hi\n"
                '2>"a echo" set -A "$n" ruff /bin/echo\nread `echo n`\n```\n',
                [*range(2, 14), 18, 19, 20, 21, 23, 26, 27],
                # The danger filter refuses lines 2-13, 18-21, 23, 26 and 27 (an array or variable named by an option,
                # a command word behind `command`, a backslash, an assignment, a wrapper, a redirection, a `hash`
                # after a quoted string holding an escaped quote and a `#`, `print -v` after a quoted format, a command
                # word built with `$'...'`, one continued across lines and reported where it ends, `set -A` behind a
                # quoted redirection target, or a `read` operand built by a command substitution); lines 14-17, 24 and
                # 25 are ordinary shapes it admits, `-v` passed to `printf` as data among them.
                id="option-forms-and-prefixed-commands",
            ),
            pytest.param(
                '```bash\nread 2>/dev/null "$n"\nhash>/dev/null -p /bin/echo probe\n'
                '{fd}>/dev/null set -A "$n" ruff /bin/echo\nread -r x < "$file"\n'
                'IFS= read -r y < "$(dirname "$R")/sha.txt" || y=\n'
                'IFS= read -r z < "$F" 2>/dev/null; z="${z:-a}"\n'
                "command -- hash -p /bin/echo probe\nsudo -u root hash -p /bin/echo probe\n"
                "command -v hash >/dev/null\n```\n",
                [2, 3, 4, 8, 9],
                # Lines 2-4 and 8-9 are refused by the danger filter (a `read` operand after `2>/dev/null`, a
                # redirection glued to `hash`, `set -A` behind an allocated descriptor `{fd}>`, `hash` behind
                # `command --` or `sudo -u root`); lines 5-7 are ordinary reads whose redirection targets hold a `$`, a
                # command substitution with a quoted space, or a `;` right after the target, and line 10 is a
                # `command -v` lookup.
                id="operands-read-past-redirections",
            ),
        ],
    )
    def test_flags_rebind_lines(
        self, shell: str, probe_tree: Callable[[str, str], Path], mode_text: str, flagged: list[int]
    ) -> None:
        """Check 23e flags exactly the lines that rebind a command name and nothing else.

        The block drops whole redirections, prefixes and wrapper options before matching, so a rebind hidden behind one
        is still reported while lookups, data operands and prose fences stay quiet.
        """
        tree = probe_tree("modes/rebind.md", mode_text)
        proc = _run_block(shell, _check_block("23e"), tree)
        assert _flagged_lines(proc) == flagged, proc.stdout + proc.stderr

    @_both_shells
    def test_finds_no_rebind_in_shipped_skills(self, shell: str) -> None:
        """No shipped skill block rebinds a command name, and Check 23e does not match its own pattern text.

        The blueprint danger filter denies these spellings by name on the evidence that no shipped block needs them;
        this keeps that premise true as skills change.
        """
        proc = _run_block(shell, _check_block("23e"), _PLUGINS.parent, timeout=60)
        assert [line for line in proc.stdout.splitlines() if not line.startswith("===")] == [], proc.stderr

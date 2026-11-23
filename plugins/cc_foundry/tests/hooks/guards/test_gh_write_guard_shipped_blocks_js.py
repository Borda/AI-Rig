"""Run ``hooks/gh-write-guard.js`` over every shell block the plugins ship in Markdown.

A shipped block that holds no GitHub write must get no verdict from the guard: no ``ask`` and no exit 2. A block it
stops costs the user a prompt on every run (``ask``), or a block the model can never get approved (exit 2: an approvable
command may not hold ``$``). The shapes that did this were a ``gh`` word inside interpreter arguments, a variable used
as the ``gh`` subcommand, an ``=~`` test after ``&&`` or ``||``, and an unquoted ``(`` in an ``=~`` regex.

Every block runs through the real guard under ``node``; nothing is mocked and no case runs ``gh``. A block is fed to the
hook when the guard's own lexer (``namesProgram`` in ``hooks/lib/shell-git.js``, asked once through ``node``) reads it
as naming ``gh``, whatever the spelling (case, quoting, ``.exe``). For any other block the guard asks only when an
executable word cannot be read, which this gate does not cover. Blocks inside blockquotes are scanned too.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PLUGINS_DIR = Path(__file__).resolve().parents[4]
GUARD = PLUGINS_DIR / "cc_foundry" / "hooks" / "gh-write-guard.js"
SHELL_LEXER = PLUGINS_DIR / "cc_foundry" / "hooks" / "lib" / "shell-git.js"

_SHELL_LANGS = frozenset({"bash", "sh", "shell", "zsh"})
_SKIPPED_PARTS = frozenset({"tests", "node_modules", "__pycache__"})
_FENCE_OPEN = re.compile(r"^(\s*(?:>\s*)*)(`{3,}|~{3,})\s*([\w-]*)")
_QUOTE_PREFIX = re.compile(r"^\s*(?:>\s?)+")
# Fails toward feeding the hook: a body the lexer throws on counts as naming gh.
_NAMES_GH_SCRIPT = (
    "const {namesProgram} = require(process.argv[1]); let raw = '';"
    "process.stdin.on('data', (c) => (raw += c)).on('end', () => {"
    "const out = JSON.parse(raw).map((b) => { try { return namesProgram(b, 'gh'); } catch { return true; } });"
    "process.stdout.write(JSON.stringify(out)); });"
)

_skip_node_unavailable = pytest.mark.skipif(shutil.which("node") is None, reason="requires node to run the hook")


def _closes_fence(line: str, fence: str, quoted: bool) -> bool:
    """Say whether ``line`` closes a fence opened with ``fence``, looking past a blockquote prefix when ``quoted``."""
    text = (_QUOTE_PREFIX.sub("", line) if quoted else line).strip()
    return text.startswith(fence) and not text.strip(fence[0])


def _fenced_shell_blocks(markdown: Path) -> list[tuple[int, str]]:
    """Return ``(first body line, body)`` for each shell-language fenced block of one Markdown file.

    Examples:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as tmp:
        ...     doc = Path(tmp) / "a.md"
        ...     _ = doc.write_text("x\\n```bash\\ngh pr list\\n```\\n> ```zsh\\n> gh pr view 1\\n> ```\\n")
        ...     _fenced_shell_blocks(doc)
        [(3, 'gh pr list'), (6, 'gh pr view 1')]
    """
    lines = markdown.read_text(encoding="utf-8", errors="replace").splitlines()
    blocks: list[tuple[int, str]] = []
    i = 0
    while i < len(lines):
        opened = _FENCE_OPEN.match(lines[i])
        if not opened:
            i += 1
            continue
        quoted = ">" in opened.group(1)
        fence, language = opened.group(2), opened.group(3).lower()
        end = i + 1
        while end < len(lines) and not _closes_fence(lines[end], fence, quoted):
            end += 1
        if language in _SHELL_LANGS:
            body = [_QUOTE_PREFIX.sub("", line) if quoted else line for line in lines[i + 1 : end]]
            blocks.append((i + 2, "\n".join(body)))
        i = end + 1
    return blocks


def _guard_names_gh(bodies: list[str]) -> list[bool]:
    """Ask the guard's own lexer, in one ``node`` call, which bodies name ``gh``."""
    done = subprocess.run(
        ["node", "-e", _NAMES_GH_SCRIPT, str(SHELL_LEXER)],
        input=json.dumps(bodies),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return json.loads(done.stdout) if done.returncode == 0 else [True] * len(bodies)


def _shipped_gh_blocks() -> list:
    """Collect every shipped shell block the guard reads as naming ``gh``, one ``pytest.param`` per block."""
    if shutil.which("node") is None:
        return []
    found: list[tuple[str, str]] = []
    for markdown in sorted(PLUGINS_DIR.rglob("*.md")):
        relative = markdown.relative_to(PLUGINS_DIR)
        if any(part in _SKIPPED_PARTS or part.startswith(".") for part in relative.parts):
            continue
        found.extend((f"{relative.as_posix()}:{start}", body) for start, body in _fenced_shell_blocks(markdown))
    names_gh = _guard_names_gh([body for _, body in found])
    return [pytest.param(body, id=block_id) for (block_id, body), named in zip(found, names_gh) if named]


SHIPPED_GH_BLOCKS = _shipped_gh_blocks()


def _guard_output(command: str, cwd: Path) -> tuple[int, str]:
    """Run the real guard on one Bash command and return its exit code with stdout and stderr joined."""
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(cwd),
    }
    env = {**os.environ, "GH_TOKEN": "dummy", "GH_HOST": "x.invalid"}
    done = subprocess.run(
        ["node", str(GUARD)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        cwd=cwd,
        check=False,
    )
    return done.returncode, (done.stdout + done.stderr).strip()


@_skip_node_unavailable
def test_shipped_blocks_naming_gh_are_found() -> None:
    """The scan sees shell blocks that name gh, so a layout change cannot turn the gate below into an empty pass."""
    assert SHIPPED_GH_BLOCKS


@_skip_node_unavailable
@pytest.mark.parametrize("body", SHIPPED_GH_BLOCKS)
def test_shipped_shell_block_gets_no_gh_write_guard_verdict(body: str, tmp_path: Path) -> None:
    """A shipped block holding no GitHub write gets neither an ``ask`` nor a block from the guard."""
    code, output = _guard_output(body, tmp_path)

    assert (code, output) == (0, ""), f"gh-write-guard stopped a shipped block (exit {code}): {output[:300]}"

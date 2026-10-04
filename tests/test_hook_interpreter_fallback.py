"""Keep every Claude plugin hook runnable on hosts whose PATH lacks the bare interpreter name.

A hook registered as ``node "…/x.js"`` or ``python "…/x.py"`` fails with ``/bin/sh: node: command not found`` on a host
without that command. Stock macOS has no ``python``, and Claude Code launched outside a login shell (the desktop app,
started from Finder) inherits a PATH without ``/opt/homebrew/bin``. Each failing hook prints one error per event, so a
missing interpreter floods every session.

The contract has three parts:

* Node hooks probe ``node`` on PATH, then ``/opt/homebrew/bin/node`` and ``/usr/local/bin/node``, and exit 0 when none
  exists. That exit is not a weakening: Claude Code blocks only on exit 2, so a hook that died with 127 was already
  non-blocking.
* Each plugin with node hooks registers one ``SessionStart`` check that reports a missing Node.js once per session, so
  the silent skip never hides that the guards are inactive.
* Python hooks prefer ``python`` (a Windows ``python3`` may be the Microsoft Store stub) and fall back to ``python3``.

Codex hook files (``codex-hooks.json``, Codex-only plugins) are out of scope: Codex runs ``python3`` on POSIX and a
separate ``commandWindows`` form.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_DIR = REPO_ROOT / "plugins"

_NODE_PROBE = 'for n in node /opt/homebrew/bin/node /usr/local/bin/node; do command -v "$n" >/dev/null 2>&1 && '
_NODE_HOOK = re.compile(
    re.escape(_NODE_PROBE) + r'exec "\$n" "\$\{CLAUDE_PLUGIN_ROOT\}/hooks/[^"\s]+\.js"; done; exit 0'
)
_PYTHON_HOOK = re.compile(
    r'if command -v python >/dev/null 2>&1; then exec python (?P<s>"\$\{CLAUDE_PLUGIN_ROOT\}/hooks/[^"\s]+\.py"); '
    r"else exec python3 (?P=s); fi"
)
_NODE_CHECK = re.compile(
    re.escape(_NODE_PROBE)
    + r'exit 0; done; echo "[\w-]+ plugin hooks are inactive: Node\.js not found[^"]*" >&2; exit 1'
)


def _claude_hook_files() -> list[Path]:
    """Return the hook file each Claude plugin manifest registers."""
    files = []
    for manifest_path in sorted(PLUGINS_DIR.glob("*/.claude-plugin/plugin.json")):
        plugin_dir = manifest_path.parents[1]
        pointer = json.loads(manifest_path.read_text(encoding="utf-8")).get("hooks", "./hooks/hooks.json")
        hook_file = plugin_dir / pointer
        if hook_file.is_file():
            files.append(hook_file)
    return files


def _commands(hook_file: Path) -> list[tuple[str, str]]:
    """Return ``(event, command)`` for every command hook in one file."""
    data = json.loads(hook_file.read_text(encoding="utf-8"))
    return [
        (event, hook["command"])
        for event, entries in data["hooks"].items()
        for entry in entries
        for hook in entry["hooks"]
    ]


HOOK_FILES = _claude_hook_files()


def test_claude_hook_files_are_discovered() -> None:
    """The scan must cover the node-hook plugins and the python-hook plugin, or every check below is vacuous."""
    names = {path.relative_to(PLUGINS_DIR).parts[0] for path in HOOK_FILES}
    assert {"cc_foundry", "cc_oss", "cc_develop", "cc_research", "codemap-py"} <= names


@pytest.mark.parametrize(
    "hook_file", [pytest.param(path, id=path.relative_to(PLUGINS_DIR).parts[0]) for path in HOOK_FILES]
)
def test_every_script_hook_uses_an_interpreter_fallback(hook_file: Path) -> None:
    """A command that runs a shipped hook script must use the node or python fallback form, never a bare interpreter."""
    offenders = [
        command
        for _, command in _commands(hook_file)
        if "${CLAUDE_PLUGIN_ROOT}/hooks/" in command
        and not (_NODE_HOOK.fullmatch(command) or _PYTHON_HOOK.fullmatch(command))
    ]
    assert offenders == []


@pytest.mark.parametrize(
    "hook_file",
    [
        pytest.param(path, id=path.relative_to(PLUGINS_DIR).parts[0])
        for path in HOOK_FILES
        if any(_NODE_HOOK.fullmatch(command) for _, command in _commands(path))
    ],
)
def test_node_hook_plugins_report_missing_node_once_per_session(hook_file: Path) -> None:
    """A plugin whose node hooks skip silently must still say, once per session, that they are inactive."""
    commands = _commands(hook_file)
    checks = [(event, command) for event, command in commands if _NODE_CHECK.fullmatch(command)]
    assert [event for event, _ in checks] == ["SessionStart"]


@pytest.mark.skipif(shutil.which("sh") is None or shutil.which("node") is None, reason="needs a POSIX sh and node")
def test_node_hook_form_passes_stdin_and_exit_code_through(tmp_path: Path) -> None:
    """The wrapper must hand the event payload to the script and keep a guard's blocking exit 2."""
    (tmp_path / "hooks").mkdir()
    script = 'let d="";process.stdin.on("data",c=>d+=c).on("end",()=>{process.stdout.write(d);process.exit(2)})\n'
    (tmp_path / "hooks" / "guard.js").write_text(script, encoding="utf-8", newline="\n")
    command = _NODE_PROBE + 'exec "$n" "${CLAUDE_PLUGIN_ROOT}/hooks/guard.js"; done; exit 0'
    assert _NODE_HOOK.fullmatch(command)

    result = subprocess.run(
        [shutil.which("sh"), "-c", command],
        input='{"event": 1}',
        capture_output=True,
        text=True,
        env={**os.environ, "CLAUDE_PLUGIN_ROOT": str(tmp_path)},
        check=False,
    )

    assert (result.returncode, result.stdout) == (2, '{"event": 1}')


_FORBIDDEN_SELECTION = (
    "command -v python || command -v python3",
    "Host Python selection",
    "Host interpreter contract",
    "resolve `python` first",
    "resolve `python` on PATH first",
)


def test_shipped_text_never_selects_the_interpreter_per_call() -> None:
    """A missing ``python`` is fixed at the launch layer, so no shipped file may select an interpreter per call.

    Rewritten call sites miss ``Bash(python:*)`` allow rules and blueprint digests, and per-file selection prose repeats
    on every skill load (``CLAUDE.md`` §Interpreter Commands).
    """
    offenders = [
        f"{path.relative_to(PLUGINS_DIR)}: {needle}"
        for path in sorted(PLUGINS_DIR.rglob("*"))
        if path.is_file()
        and "tests" not in path.relative_to(PLUGINS_DIR).parts
        and path.suffix in {".md", ".py", ".json", ".toml", ""}
        for needle in _FORBIDDEN_SELECTION
        if needle in path.read_text(encoding="utf-8", errors="ignore")
    ]
    assert offenders == []


def test_every_claude_plugin_ships_the_python_fallback() -> None:
    """Each Claude plugin stands alone, so each ships its own executable ``bin/python``."""
    for hook_file in HOOK_FILES:
        shim = hook_file.parents[1] / "bin" / "python"
        assert shim.is_file(), shim
        assert os.name == "nt" or shim.stat().st_mode & 0o111, shim


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required to read checkout attributes")
def test_python_fallback_is_checked_out_with_lf_on_every_host() -> None:
    """Windows checkouts convert extensionless text to CRLF unless attributes pin LF; CRLF breaks the shebang."""
    # repo-relative POSIX paths: git C-quotes paths containing backslashes, so absolute Windows paths never match
    shims = [(hook_file.parents[1] / "bin" / "python").relative_to(REPO_ROOT).as_posix() for hook_file in HOOK_FILES]
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "check-attr", "eol", "--", *shims], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        pytest.skip("not a git checkout")
    assert result.stdout.splitlines() == [f"{shim}: eol: lf" for shim in shims]

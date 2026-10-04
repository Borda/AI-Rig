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
* Codemap hooks share its validated CPython launchers; native Windows uses ``codemap-py.cmd``.
* Standalone plugin ``bin/python`` and ``bin/python.cmd`` validate Python 3.10+ before executing the workload once.

Codex hook registrations use separate POSIX and native Windows forms. Codemap config coverage lives in its plugin tests;
the standalone launchers include the Codex Rig copy here.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_DIR = REPO_ROOT / "plugins"
_POSIX_SHELL = shutil.which("sh")

_NODE_PROBE = 'for n in node /opt/homebrew/bin/node /usr/local/bin/node; do command -v "$n" >/dev/null 2>&1 && '
_NODE_HOOK = re.compile(
    re.escape(_NODE_PROBE) + r'exec "\$n" "\$\{CLAUDE_PLUGIN_ROOT\}/hooks/[^"\s]+\.js"; done; exit 0'
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
        if ("${CLAUDE_PLUGIN_ROOT}/hooks/" in command or "${CLAUDE_PLUGIN_ROOT}/bin/codemap-py" in command)
        and not (
            _NODE_HOOK.fullmatch(command)
            or re.fullmatch(r'exec "\$\{CLAUDE_PLUGIN_ROOT\}/bin/codemap-py" --run-hook [a-z-]+\.py', command)
        )
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


@pytest.mark.skipif(_POSIX_SHELL is None, reason="needs a POSIX shell")
@pytest.mark.parametrize(
    "plugin", ["cc_foundry", "cc_oss", "cc_develop", "cc_research", "codemap-py", "bridge_cc-codex", "codex-rig"]
)
def test_python_fallback_finds_versioned_only_runtime(plugin: str, tmp_path: Path) -> None:
    """An installed shim starts without bare python3 and rejects a lying candidate name."""
    runtime = tmp_path / "python3.12"
    runtime.write_text(f'#!/bin/sh\nexec "{Path(sys.executable).as_posix()}" "$@"\n', encoding="utf-8", newline="\n")
    runtime.chmod(0o755)
    old = tmp_path / "python3.20"
    old.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8", newline="\n")
    old.chmod(0o755)
    result = subprocess.run(
        [
            _POSIX_SHELL,
            (PLUGINS_DIR / plugin / "bin" / "python").as_posix(),
            "-c",
            "import sys; print(sys.argv[1]); print(sys.stdin.read()); print('error', file=sys.stderr); sys.exit(23)",
            "space argument",
        ],
        input="payload",
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PATH": tmp_path.as_posix()},
    )
    assert result.returncode == 23, result.stderr
    assert result.stdout == "space argument\npayload\n"
    assert result.stderr == "error\n"


@pytest.mark.skipif(_POSIX_SHELL is None, reason="needs a POSIX shell")
def test_python_fallback_skips_self_and_does_not_leak_probe_guard(tmp_path: Path) -> None:
    """Self discovery terminates, and private probe state never reaches the workload."""
    shutil.copy2(PLUGINS_DIR / "cc_foundry" / "bin" / "python", tmp_path / "python")
    runtime = tmp_path / "python3.12"
    runtime.write_text(f'#!/bin/sh\nexec "{Path(sys.executable).as_posix()}" "$@"\n', encoding="utf-8", newline="\n")
    runtime.chmod(0o755)
    result = subprocess.run(
        [
            _POSIX_SHELL,
            (tmp_path / "python").as_posix(),
            "-c",
            "import os; print(os.environ.get('AI_RIG_PYTHON_PROBE', 'absent'))",
        ],
        env={**os.environ, "PATH": tmp_path.as_posix()},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, "absent\n", "")


@pytest.mark.skipif(_POSIX_SHELL is None, reason="needs a POSIX shell")
def test_python_fallback_no_runtime_has_bounded_diagnostic(tmp_path: Path) -> None:
    """An empty PATH fails without a bootstrap traceback or repeated workload."""
    result = subprocess.run(
        [
            _POSIX_SHELL,
            (PLUGINS_DIR / "cc_foundry" / "bin" / "python").as_posix(),
            "-c",
            "raise AssertionError('must not run')",
        ],
        env={**os.environ, "PATH": tmp_path.as_posix()},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 127
    assert result.stdout == ""
    assert result.stderr == "python: no Python 3.10+ found on PATH (python3, python3.10-python3.20); install one\n"


@pytest.mark.skipif(shutil.which("cmd") is None, reason="needs native Windows cmd")
def test_native_windows_python_launcher_preserves_workload(tmp_path: Path) -> None:
    """A real cmd process runs the installed launcher without any POSIX shell."""
    script = tmp_path / "work load.py"
    script.write_text(
        "import sys\nprint(sys.argv[1]); print(sys.stdin.read()); print('error', file=sys.stderr); sys.exit(23)\n",
        encoding="utf-8",
        newline="\n",
    )
    launcher = tmp_path / "python.cmd"
    shutil.copy2(PLUGINS_DIR / "codex-rig" / "bin" / "python.cmd", launcher)
    cmd = shutil.which("cmd")
    # cmd parses its own quotes; argv-list serialization adds incompatible CRT escapes.
    command = f'"{cmd}" /d /s /c ""{launcher}" "{script}" "space argument""'
    result = subprocess.run(
        command,
        executable=cmd,
        input="payload",
        env={**os.environ, "PATH": str(Path(sys.executable).parent)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode, result.stdout, result.stderr) == (23, "space argument\npayload\n", "error\n")


@pytest.mark.integration
def test_windows_checkout_preserves_shared_launcher_bytes(tmp_path: Path) -> None:
    """Windows-style Git checkout must preserve shared bytes and package hash identities."""
    repository = tmp_path / "checkout"
    repository.mkdir()
    shutil.copy2(REPO_ROOT / ".gitattributes", repository / ".gitattributes")
    launchers = sorted(PLUGINS_DIR.glob("*/bin/python.cmd"))
    assert len(launchers) >= 2
    expected = (PLUGINS_DIR / "cc_foundry/bin/python.cmd").read_bytes()
    assert b"\r\n" not in expected
    for source in launchers:
        target = repository / source.relative_to(REPO_ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(expected)
    for arguments in (["init", "-q"], ["add", "."], ["checkout-index", "--all", "--prefix=export/"]):
        subprocess.run(
            ["git", "-c", "core.autocrlf=true", *arguments],
            cwd=repository,
            capture_output=True,
            check=True,
        )
    for source in launchers:
        exported = repository / "export" / source.relative_to(REPO_ROOT)
        assert exported.read_bytes() == expected, source.relative_to(REPO_ROOT).as_posix()

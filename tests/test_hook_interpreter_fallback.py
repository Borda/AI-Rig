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
_requires_posix_shell = pytest.mark.skipif(_POSIX_SHELL is None, reason="needs a POSIX shell")

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
    """The scan covers the node-hook plugins and the python-hook plugin.

    A manifest layout change that hid a plugin's hook file would make every parametrized check below pass vacuously.
    """
    names = {path.relative_to(PLUGINS_DIR).parts[0] for path in HOOK_FILES}
    assert {"cc_foundry", "cc_oss", "cc_develop", "cc_research", "codemap-py"} <= names


@pytest.mark.parametrize(
    "hook_file", [pytest.param(path, id=path.relative_to(PLUGINS_DIR).parts[0]) for path in HOOK_FILES]
)
def test_every_script_hook_uses_an_interpreter_fallback(hook_file: Path) -> None:
    """A command that runs a shipped hook script uses the node or python fallback form, never a bare interpreter.

    A bare ``node "…/x.js"`` fails with ``command not found`` on a host without Node on PATH, once per hook event.
    """
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
    """A plugin whose node hooks skip silently still says, once per session, that they are inactive.

    The silent exit 0 on a missing Node is only acceptable when exactly one ``SessionStart`` check reports it.
    """
    commands = _commands(hook_file)
    checks = [(event, command) for event, command in commands if _NODE_CHECK.fullmatch(command)]
    assert [event for event, _ in checks] == ["SessionStart"]


@pytest.mark.skipif(_POSIX_SHELL is None or shutil.which("node") is None, reason="needs a POSIX sh and node")
def test_node_hook_form_passes_stdin_and_exit_code_through(tmp_path: Path) -> None:
    """The wrapper hands the event payload to the script and keeps a guard's blocking exit 2.

    A real ``sh`` runs the registered command form against a stub guard that echoes stdin and exits 2, the only exit
    code Claude Code treats as blocking.
    """
    (tmp_path / "hooks").mkdir()
    script = 'let d="";process.stdin.on("data",c=>d+=c).on("end",()=>{process.stdout.write(d);process.exit(2)})\n'
    (tmp_path / "hooks" / "guard.js").write_text(script, encoding="utf-8", newline="\n")
    command = _NODE_PROBE + 'exec "$n" "${CLAUDE_PLUGIN_ROOT}/hooks/guard.js"; done; exit 0'

    result = subprocess.run(
        [_POSIX_SHELL, "-c", command],
        input='{"event": 1}',
        capture_output=True,
        text=True,
        env={**os.environ, "CLAUDE_PLUGIN_ROOT": str(tmp_path)},
        check=False,
    )

    assert _NODE_HOOK.fullmatch(command)
    assert (result.returncode, result.stdout) == (2, '{"event": 1}')


_FORBIDDEN_SELECTION = (
    "command -v python || command -v python3",
    "Host Python selection",
    "Host interpreter contract",
    "resolve `python` first",
    "resolve `python` on PATH first",
)
_SHIPPED_SUFFIXES = frozenset({".md", ".py", ".json", ".toml", ""})
_GIT = shutil.which("git")
_IN_GIT_CHECKOUT = (
    _GIT is not None
    and subprocess.run(
        [_GIT, "-C", str(REPO_ROOT), "rev-parse", "--git-dir"], capture_output=True, check=False
    ).returncode
    == 0
)


def _shipped_files() -> list[Path]:
    """Return every shipped (non-test) plugin file of a text-like type."""
    return [
        path
        for path in sorted(PLUGINS_DIR.rglob("*"))
        if path.is_file() and "tests" not in path.relative_to(PLUGINS_DIR).parts and path.suffix in _SHIPPED_SUFFIXES
    ]


def _selection_offenders(path: Path) -> list[str]:
    """Return one ``plugin-relative path: needle`` entry per forbidden interpreter-selection phrase in ``path``."""
    text = path.read_text(encoding="utf-8", errors="ignore")
    return [f"{path.relative_to(PLUGINS_DIR)}: {needle}" for needle in _FORBIDDEN_SELECTION if needle in text]


def _write_runtime(directory: Path, name: str, body: str) -> Path:
    """Write an executable POSIX ``name`` script with ``body`` into ``directory``."""
    runtime = directory / name
    runtime.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8", newline="\n")
    runtime.chmod(0o755)
    return runtime


def _write_versioned_python(directory: Path, name: str = "python3.12") -> Path:
    """Write a shim named ``name`` that execs the interpreter running the tests."""
    return _write_runtime(directory, name, f'exec "{Path(sys.executable).as_posix()}" "$@"')


def _run_launcher(launcher: Path, path_dir: Path, *args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
    """Run a POSIX ``bin/python`` launcher under ``sh`` with ``path_dir`` as the whole PATH."""
    return subprocess.run(
        [_POSIX_SHELL, launcher.as_posix(), *args],
        env={**os.environ, "PATH": path_dir.as_posix()},
        capture_output=True,
        text=True,
        check=False,
        **kwargs,
    )


def _export_with_autocrlf(repository: Path, launchers: list[Path], content: bytes) -> None:
    """Commit ``content`` at every launcher path and export it with ``core.autocrlf=true`` like a Windows checkout."""
    repository.mkdir()
    shutil.copy2(REPO_ROOT / ".gitattributes", repository / ".gitattributes")
    for source in launchers:
        target = repository / source.relative_to(REPO_ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    for arguments in (["init", "-q"], ["add", "."], ["checkout-index", "--all", "--prefix=export/"]):
        subprocess.run(["git", "-c", "core.autocrlf=true", *arguments], cwd=repository, capture_output=True, check=True)


def test_shipped_text_never_selects_the_interpreter_per_call() -> None:
    """A missing ``python`` is fixed at the launch layer, so no shipped file selects an interpreter per call.

    Rewritten call sites miss ``Bash(python:*)`` allow rules and blueprint digests, and per-file selection prose repeats
    on every skill load (``CLAUDE.md`` §Interpreter Commands).
    """
    offenders = [offender for path in _shipped_files() for offender in _selection_offenders(path)]

    assert offenders == []


class TestPythonLauncher:
    """The standalone ``bin/python`` and ``bin/python.cmd`` launchers shipped by each plugin."""

    @pytest.mark.parametrize(
        "hook_file", [pytest.param(path, id=path.relative_to(PLUGINS_DIR).parts[0]) for path in HOOK_FILES]
    )
    def test_every_claude_plugin_ships_an_executable_fallback(self, hook_file: Path) -> None:
        """Each Claude plugin stands alone, so each ships its own executable ``bin/python``.

        A plugin installed without its siblings has no other launcher to borrow; on POSIX the file must be executable.
        """
        shim = hook_file.parents[1] / "bin" / "python"

        assert shim.is_file(), shim
        assert os.name == "nt" or shim.stat().st_mode & 0o111, shim

    @pytest.mark.skipif(not _IN_GIT_CHECKOUT, reason="checkout attributes need a git checkout")
    def test_fallback_is_checked_out_with_lf_on_every_host(self) -> None:
        """Windows checkouts convert extensionless text to CRLF unless attributes pin LF; CRLF breaks the shebang.

        ``git check-attr`` reports the end-of-line attribute of every plugin's shim; each must be ``lf``.
        """
        # repo-relative POSIX paths: git C-quotes paths containing backslashes, so absolute Windows paths never match
        shims = [(hook.parents[1] / "bin" / "python").relative_to(REPO_ROOT).as_posix() for hook in HOOK_FILES]

        result = subprocess.run(
            [_GIT, "-C", str(REPO_ROOT), "check-attr", "eol", "--", *shims], capture_output=True, text=True, check=False
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == [f"{shim}: eol: lf" for shim in shims]

    @_requires_posix_shell
    @pytest.mark.parametrize(
        "plugin", ["cc_foundry", "cc_oss", "cc_develop", "cc_research", "codemap-py", "bridge_cc-codex", "codex-rig"]
    )
    def test_finds_versioned_only_runtime(self, plugin: str, tmp_path: Path) -> None:
        """An installed shim starts without bare python3, rejects a lying candidate name and preserves the workload.

        PATH holds only ``python3.12`` (a working shim) and ``python3.20`` (exits 1). Arguments with spaces, stdin,
        stderr and the exit status of the workload must pass through the launcher unchanged.
        """
        _write_versioned_python(tmp_path)
        _write_runtime(tmp_path, "python3.20", "exit 1")
        workload = (
            "import sys; print(sys.argv[1]); print(sys.stdin.read()); print('error', file=sys.stderr); sys.exit(23)"
        )

        result = _run_launcher(
            PLUGINS_DIR / plugin / "bin" / "python", tmp_path, "-c", workload, "space argument", input="payload"
        )

        assert result.returncode == 23, result.stderr
        assert result.stdout == "space argument\npayload\n"
        assert result.stderr == "error\n"

    @_requires_posix_shell
    def test_skips_itself_and_hides_probe_guard_from_workload(self, tmp_path: Path) -> None:
        """Self discovery terminates, and private probe state never reaches the workload.

        The launcher is copied onto PATH under its own name beside a real runtime; it must skip itself while probing and
        unset its recursion guard before executing the workload.
        """
        shutil.copy2(PLUGINS_DIR / "cc_foundry" / "bin" / "python", tmp_path / "python")
        _write_versioned_python(tmp_path)

        result = _run_launcher(
            tmp_path / "python",
            tmp_path,
            "-c",
            "import os; print(os.environ.get('AI_RIG_PYTHON_PROBE', 'absent'))",
            timeout=10,
        )

        assert (result.returncode, result.stdout, result.stderr) == (0, "absent\n", "")

    @_requires_posix_shell
    def test_without_runtime_fails_with_bounded_diagnostic(self, tmp_path: Path) -> None:
        """An empty PATH fails without a bootstrap traceback or repeated workload.

        With no Python 3.10+ on PATH the launcher exits 127 with one diagnostic line and never runs the workload.
        """
        launcher = PLUGINS_DIR / "cc_foundry" / "bin" / "python"

        result = _run_launcher(launcher, tmp_path, "-c", "raise AssertionError('must not run')", timeout=10)

        assert result.returncode == 127
        assert result.stdout == ""
        assert result.stderr == "python: no Python 3.10+ found on PATH (python3, python3.10-python3.20); install one\n"

    def test_windows_launcher_disables_implicit_current_directory_lookup(self) -> None:
        """The cmd search guard is set inside ``setlocal`` before any interpreter is probed.

        Without ``NoDefaultCurrentDirectoryInExePath`` cmd would resolve ``python.exe`` from the current directory.
        """
        source = (PLUGINS_DIR / "cc_foundry/bin/python.cmd").read_text(encoding="utf-8")

        guard = source.index('set "NoDefaultCurrentDirectoryInExePath=1"')

        assert source.index("setlocal") < guard < source.index("python.exe -c")

    @pytest.mark.skipif(shutil.which("cmd") is None, reason="needs native Windows cmd")
    def test_native_windows_launcher_preserves_workload(self, tmp_path: Path) -> None:
        """A real cmd process runs the installed launcher without any POSIX shell.

        The workload script path and an argument both contain spaces; stdin, stderr and the exit status must survive.
        """
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
    def test_windows_checkout_preserves_shared_launcher_bytes(self, tmp_path: Path) -> None:
        """Windows-style Git checkout must preserve shared bytes and package hash identities.

        Every plugin's ``python.cmd`` is a byte-identical copy; exporting them through real Git with ``core.autocrlf``
        and the repository's ``.gitattributes`` must not turn any copy into CRLF.
        """
        launchers = sorted(PLUGINS_DIR.glob("*/bin/python.cmd"))
        expected = (PLUGINS_DIR / "cc_foundry/bin/python.cmd").read_bytes()
        repository = tmp_path / "checkout"

        _export_with_autocrlf(repository, launchers, expected)

        exported = {
            src.relative_to(REPO_ROOT).as_posix(): (repository / "export" / src.relative_to(REPO_ROOT)).read_bytes()
            for src in launchers
        }
        assert len(launchers) >= 2
        assert b"\r\n" not in expected
        assert [name for name, content in exported.items() if content != expected] == []

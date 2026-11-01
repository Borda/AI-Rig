"""Interpreter probe, entry gate, and doctor contract.

Covers interpreter candidate precedence, the CPython ``>=3.11,<3.15`` bound, the invalid-``CODEMAP_PYTHON`` hard fail,
the exit-127 rejection contract, and the ``doctor --json`` schema. Version-gated behaviour branches on the running
interpreter so every assertion runs (never skips) on the 3.10 matrix cell.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _PLUGIN_ROOT / "scripts"
_ENTRY = _SCRIPTS / "codemap_py_entry.py"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import codemap_py_cli as cli  # noqa: E402  (needs the scripts/ path insert above)

_RUNNING_SUPPORTED = cli.is_supported(sys.implementation.name, sys.version_info.major, sys.version_info.minor)
_NO_INTERPRETER_EXIT = 127  # exit status of the "no eligible interpreter" rejection
_POSIX_SHELL = shutil.which("sh")
_LAUNCHER = _PLUGIN_ROOT / "bin" / ("codemap-py.cmd" if sys.platform == "win32" else "codemap-py")
_POSIX_CANDIDATES = [["python3"], ["python"], ["python3.14"], ["python3.13"], ["python3.12"], ["python3.11"]]
_SUPPORTED_CPYTHON = ("cpython", 3, 12)

_skip_unsupported_runtime = pytest.mark.skipif(
    not _RUNNING_SUPPORTED, reason="wrapper delegates to this interpreter; must be supported"
)


def _run_entry(args: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Invoke the entry script under the current interpreter."""
    return subprocess.run(
        [sys.executable, str(_ENTRY), *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=env,
    )


def _run_launcher(*args: str, codemap_python: Path) -> subprocess.CompletedProcess[str]:
    """Run the shipped launcher with ``CODEMAP_PYTHON`` set; PATH is inherited so the shell/cmd interpreter is found."""
    return subprocess.run(
        [str(_LAUNCHER), *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={**os.environ, "CODEMAP_PYTHON": str(codemap_python)},
    )


def _write_cmd_wrapper(wrapper: Path) -> None:
    """Write a Windows ``.cmd`` wrapper that delegates to the running interpreter."""
    wrapper.write_text(f'@echo off\n"{sys.executable}" %*\n', encoding="utf-8", newline="\r\n")


def _write_sh_wrapper(wrapper: Path) -> None:
    """Write an executable POSIX ``sh`` wrapper that delegates to the running interpreter."""
    wrapper.write_text(f'#!/bin/sh\nexec "{Path(sys.executable).as_posix()}" "$@"\n', encoding="utf-8", newline="\n")
    wrapper.chmod(0o755)


_write_python_wrapper = _write_cmd_wrapper if sys.platform == "win32" else _write_sh_wrapper


def _probe_table(table: dict[tuple[str, ...], tuple[str, int, int] | None], default: tuple[str, int, int] | None):
    """Return a fake interpreter probe answering from *table* by candidate argv, else *default*."""

    def probe(candidate: list[str]) -> tuple[str, int, int] | None:
        """Report the scripted probe result for one candidate argv."""
        return table.get(tuple(candidate), default)

    return probe


def _hook_commands(installed: Path, config: str) -> list[str]:
    """Return every hook command registered in one shipped hooks config."""
    hooks = json.loads((installed / "hooks" / config).read_text())["hooks"]
    return [hook["command"] for entries in hooks.values() for entry in entries for hook in entry["hooks"]]


def _failing_hook_runs(installed: Path, env: dict[str, str], config: str) -> list[tuple[str, int, str]]:
    """Run each hook command of one config; return ``(command, returncode, stderr)`` of those failing to start."""
    runs = [
        (
            command,
            subprocess.run(
                [_POSIX_SHELL, "-c", command], input="{}", env=env, capture_output=True, text=True, check=False
            ),
        )
        for command in _hook_commands(installed, config)
    ]
    return [
        (command, run.returncode, run.stderr)
        for command, run in runs
        if run.returncode == _NO_INTERPRETER_EXIT or "Traceback" in run.stderr
    ]


@pytest.fixture(name="old_default_install", scope="module")
def _old_default_install(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, str]]:
    """Copy the plugin (without tests) to a spaced path whose PATH holds failing old defaults and one 3.12 runtime.

    Returns:
        The installed plugin root and an env whose ``PATH`` is only that directory (read-only for every consumer).
    """
    base = tmp_path_factory.mktemp("old-defaults")
    installed = base / "installed plugin"
    shutil.copytree(_PLUGIN_ROOT, installed, ignore=shutil.ignore_patterns("tests", "__pycache__", ".pytest_cache"))
    path = base / "path"
    path.mkdir()
    for name in ("python", "python3", "python3.14"):
        target = path / name
        target.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8", newline="\n")
        target.chmod(0o755)
    _write_sh_wrapper(path / "python3.12")
    env = {
        **os.environ,
        "PATH": path.as_posix(),
        "CLAUDE_PLUGIN_ROOT": installed.as_posix(),
        "PLUGIN_ROOT": installed.as_posix(),
    }
    env.pop("CODEMAP_PYTHON", None)
    return installed, env


class TestVersionBound:
    """Only CPython 3.11 through 3.14 is a supported runtime."""

    @pytest.mark.parametrize(
        ("impl", "major", "minor", "expected"),
        [
            pytest.param("cpython", 3, 11, True, id="cpython-3-11"),
            pytest.param("cpython", 3, 12, True, id="cpython-3-12"),
            pytest.param("cpython", 3, 14, True, id="cpython-3-14"),
            pytest.param("cpython", 3, 10, False, id="cpython-3-10"),
            pytest.param("cpython", 3, 15, False, id="cpython-3-15"),
            pytest.param("cpython", 4, 0, False, id="cpython-4"),
            pytest.param("pypy", 3, 12, False, id="pypy"),
        ],
    )
    def test_is_supported(self, impl: str, major: int, minor: int, expected: bool) -> None:
        """Interpreters inside ``>=3.11,<3.15`` on CPython are supported; every other combination is rejected.

        The edges (3.10, 3.15, a future 4.x) and a non-CPython implementation pin both ends of the supported window.
        """
        assert cli.is_supported(impl, major, minor) is expected


class TestCandidateOrder:
    """The probe tries a fixed, platform-specific list of interpreter candidates."""

    @pytest.mark.parametrize("platform", ["linux", "darwin"])
    def test_posix_order(self, platform: str) -> None:
        """POSIX hosts try ``python3``, ``python``, then versioned names from newest to oldest.

        Linux and macOS share one precedence, so a regression in either platform branch shows up here.
        """
        assert cli.candidate_interpreters({}, platform) == _POSIX_CANDIDATES

    def test_simulated_windows_order(self) -> None:
        """Windows tries versioned ``py`` selections first, then the unversioned ``py -3`` and ``.exe`` names.

        Run on every host with a simulated ``win32`` platform string, so the Windows ordering is checked everywhere.
        """
        assert cli.candidate_interpreters({}, "win32") == [["py", f"-3.{minor}"] for minor in range(14, 10, -1)] + [
            ["py", "-3"],
            ["python.exe"],
            ["python3.exe"],
            *[[f"python3.{minor}.exe"] for minor in range(14, 10, -1)],
        ]

    @pytest.mark.parametrize("platform", ["linux", "win32"])
    def test_override_is_sole_candidate(self, platform: str) -> None:
        """``CODEMAP_PYTHON`` replaces the whole candidate list on every platform.

        An explicit override is authoritative, so no default candidate may be tried beside it.
        """
        assert cli.candidate_interpreters({"CODEMAP_PYTHON": "/opt/py"}, platform) == [["/opt/py"]]


class TestResolveInterpreter:
    """Candidate resolution picks the first supported interpreter and treats an override as authoritative."""

    @pytest.mark.parametrize(
        ("platform", "table", "default", "expected"),
        [
            pytest.param("linux", {}, _SUPPORTED_CPYTHON, ["python3"], id="python3-preferred-over-python"),
            pytest.param(
                "linux",
                {("python3",): ("cpython", 3, 10)},
                _SUPPORTED_CPYTHON,
                ["python"],
                id="unsupported-first-candidate-skipped",
            ),
            pytest.param(
                "win32",
                {("py", "-3.12"): _SUPPORTED_CPYTHON, ("py", "-3"): ("cpython", 3, 9)},
                None,
                ["py", "-3.12"],
                id="windows-eligible-version-behind-bad-default",
            ),
        ],
    )
    def test_first_supported_candidate_wins(
        self,
        platform: str,
        table: dict[tuple[str, ...], tuple[str, int, int] | None],
        default: tuple[str, int, int] | None,
        expected: list[str],
    ) -> None:
        """The first candidate that probes as supported is chosen and no diagnostic is reported.

        An unsupported earlier candidate, including the unsupported bare ``py -3`` default on a simulated Windows host,
        must not hide a supported interpreter later in the list.
        """
        resolved, diag = cli.resolve_interpreter({}, platform, probe=_probe_table(table, default))

        assert (resolved, diag) == (expected, None)

    @pytest.mark.parametrize(
        ("override", "table"),
        [
            pytest.param("/no/such/py", {("/no/such/py",): None}, id="missing-binary"),
            pytest.param("python3.10", {("python3.10",): ("cpython", 3, 10)}, id="unsupported-version"),
        ],
    )
    def test_invalid_override_hard_fails(
        self, override: str, table: dict[tuple[str, ...], tuple[str, int, int] | None]
    ) -> None:
        """An unusable ``CODEMAP_PYTHON`` resolves to nothing, with a diagnostic naming the variable.

        The override is missing or unsupported while the default candidates would all be valid; resolution must not fall
        through to them, because silently ignoring an explicit override hides a misconfiguration.
        """
        resolved, diag = cli.resolve_interpreter(
            {"CODEMAP_PYTHON": override}, "linux", probe=_probe_table(table, _SUPPORTED_CPYTHON)
        )

        assert resolved is None
        assert diag is not None
        assert "CODEMAP_PYTHON" in diag


class TestLauncher:
    """The shipped launcher script selects the runtime, honouring ``CODEMAP_PYTHON``."""

    def test_invalid_override_exits_127_with_empty_stdout(self) -> None:
        """A nonexistent ``CODEMAP_PYTHON`` makes the real launcher exit 127 with a stderr message and no stdout.

        Runs the actual launcher so the exit-code contract is checked through the shell or cmd entry point, not only
        through the Python resolver.
        """
        completed = _run_launcher("doctor", codemap_python=_PLUGIN_ROOT / "bin" / "does-not-exist")

        assert completed.returncode == _NO_INTERPRETER_EXIT
        assert completed.stdout == ""
        assert completed.stderr.strip() != ""

    @_skip_unsupported_runtime
    def test_codemap_python_with_space_is_one_argument(self, tmp_path: Path) -> None:
        """A ``CODEMAP_PYTHON`` path containing a space reaches the interpreter as a single argv element.

        The wrapper lives at ``py wrap`` under a temp dir and delegates to the running interpreter; if the launcher
        split on whitespace, ``doctor --json`` would fail instead of reporting a supported runtime.
        """
        wrapper = tmp_path / ("py wrap.cmd" if sys.platform == "win32" else "py wrap")
        _write_python_wrapper(wrapper)

        completed = _run_launcher("doctor", "--json", codemap_python=wrapper)

        assert completed.returncode == 0, completed.stderr
        assert json.loads(completed.stdout)["supported"] is True

    @pytest.mark.skipif(
        _POSIX_SHELL is None or not _RUNNING_SUPPORTED, reason="needs POSIX shell and supported runtime"
    )
    def test_installed_launcher_finds_versioned_runtime_behind_old_defaults(
        self, old_default_install: tuple[Path, dict[str, str]]
    ) -> None:
        """A copied install resolves ``python3.12`` even though ``python``, ``python3`` and ``python3.14`` all fail.

        PATH holds only failing stubs for the old defaults plus one working versioned runtime, so the launcher must keep
        searching versioned names instead of stopping at the first stub.
        """
        installed, env = old_default_install

        result = subprocess.run(
            [_POSIX_SHELL, (installed / "bin" / "codemap-py").as_posix(), "doctor", "--json"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["supported"] is True

    @pytest.mark.skipif(
        _POSIX_SHELL is None or not _RUNNING_SUPPORTED, reason="needs POSIX shell and supported runtime"
    )
    @pytest.mark.parametrize("config", ["claude-hooks.json", "codex-hooks.json"])
    def test_installed_hooks_validate_runtime_before_importing_scanner(
        self, old_default_install: tuple[Path, dict[str, str]], config: str
    ) -> None:
        """Every registered hook command runs under the old-defaults PATH without exit 127 or a traceback.

        Hooks launch the scanner through the same interpreter search as the launcher, so they must validate the runtime
        before importing scanner code rather than crash on the first failing stub.
        """
        installed, env = old_default_install

        failures = _failing_hook_runs(installed, env, config)

        assert failures == []


class TestEntryGate:
    """The Python entry script accepts supported interpreters and rejects the rest with exit 127."""

    def test_matches_running_interpreter(self) -> None:
        """``doctor --json`` succeeds on a supported interpreter and is rejected with exit 127 on any other.

        An unsupported runtime (the 3.10 matrix cell) must exit 127 with empty stdout and a stderr diagnostic; a
        supported one must print the report on stdout and exit 0.
        """
        result = _run_entry(["doctor", "--json"])

        assert result.returncode == (0 if _RUNNING_SUPPORTED else _NO_INTERPRETER_EXIT)
        assert (result.stdout == "") is (not _RUNNING_SUPPORTED)
        assert _RUNNING_SUPPORTED or result.stderr.strip() != ""

    @_skip_unsupported_runtime
    def test_doctor_json_schema(self) -> None:
        """``doctor --json`` reports exactly the documented keys for a CPython plugin install.

        The schema is only defined for a supported interpreter, so the test is gated on the running runtime.
        """
        result = _run_entry(["doctor", "--json"])

        report = json.loads(result.stdout)
        assert result.returncode == 0
        assert set(report) == {"python", "version", "implementation", "supported", "plugin_root", "index_path"}
        assert report["implementation"] == "cpython"
        assert report["supported"] is True
        assert Path(report["plugin_root"]).name == "codemap-py"

    def test_hook_entry_rejects_unshipped_script(self) -> None:
        """The shared ``--run-hook`` path refuses a script outside the shipped hook set, without a traceback.

        A path-traversal argument naming the entry script itself must not execute; the exit is 2 on a supported runtime
        and 127 otherwise.
        """
        result = _run_entry(["--run-hook", "../scripts/codemap_py_entry.py"])

        assert result.returncode == (2 if _RUNNING_SUPPORTED else _NO_INTERPRETER_EXIT)
        assert result.stdout == ""
        assert "Traceback" not in result.stderr


class TestWindowsLauncher:
    """The Windows ``.cmd`` launcher keeps an explicit override ahead of default discovery."""

    def test_override_branch_precedes_default_candidates(self) -> None:
        """The default jump resolves, while an explicit override cannot enter default discovery.

        Text-level check of ``codemap-py.cmd`` so the ordering is verified on every host, not only on native Windows.
        """
        content = (_PLUGIN_ROOT / "bin" / "codemap-py.cmd").read_text()
        assert "\n:defaults\n" in content

        override, defaults = content.split("\n:defaults\n", 1)

        assert "if not defined CODEMAP_PYTHON goto :defaults" in override
        assert '%OVERRIDE_CALL% "%CODEMAP_PYTHON%" -c "%PROBE%"' in override
        assert "if errorlevel 1 goto :nointerp" in override
        assert '%OVERRIDE_CALL% "%CODEMAP_PYTHON%" "%ENTRY%" %*\nexit /b %errorlevel%' in override
        assert "for %%V in (3.14 3.13 3.12 3.11)" in defaults
        assert "%CODEMAP_PYTHON%" not in defaults

    @pytest.mark.skipif(shutil.which("cmd") is None or not _RUNNING_SUPPORTED, reason="needs cmd and supported runtime")
    @pytest.mark.parametrize("override", ["supported", "missing", "unset"])
    def test_native_installed_launcher_override_and_defaults(self, override: str, tmp_path: Path) -> None:
        """Native cmd preserves authoritative overrides and the default branch in a copied install.

        A supported spaced wrapper and an unset override must run ``doctor``; a missing override must exit 127 with the
        no-eligible-CPython message. Skipped off native Windows, so the body is deliberately kept as originally written.
        """
        installed = tmp_path / "installed plugin"
        shutil.copytree(_PLUGIN_ROOT, installed)
        env = {**os.environ, "PATH": str(Path(sys.executable).parent)}
        env.pop("CODEMAP_PYTHON", None)
        if override == "supported":
            wrapper = tmp_path / "runtime with space.cmd"
            _write_cmd_wrapper(wrapper)
            env["CODEMAP_PYTHON"] = str(wrapper)
        elif override == "missing":
            env["CODEMAP_PYTHON"] = str(tmp_path / "does-not-exist.exe")
        launcher = installed / "bin" / "codemap-py.cmd"
        cmd = shutil.which("cmd")
        # cmd parses its own quotes; argv-list serialization adds incompatible CRT escapes.
        result = subprocess.run(
            f'"{cmd}" /d /s /c ""{launcher}" doctor --json"',
            executable=cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == (127 if override == "missing" else 0), result.stderr
        if override == "missing":
            assert result.stdout == ""
            assert "no eligible CPython" in result.stderr
        else:
            assert json.loads(result.stdout)["supported"] is True
        assert "Traceback" not in result.stderr

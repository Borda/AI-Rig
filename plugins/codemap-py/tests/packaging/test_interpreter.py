"""Interpreter probe, entry gate, and doctor contract.

Covers interpreter candidate precedence, the CPython ``>=3.11,<3.15`` bound, the invalid-``CODEMAP_PYTHON`` hard fail,
the exit-127 rejection contract, and the ``doctor --json`` schema. Version-gated behaviour branches on the running
interpreter so every assertion runs (never skips) on the 3.10 matrix cell.
"""

from __future__ import annotations

import json
import os
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
_NO_INTERPRETER_EXIT = 127  #


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


# --- version bound ---------------------------------------------------------


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
def test_version_bound(impl: str, major: int, minor: int, expected: bool) -> None:
    assert cli.is_supported(impl, major, minor) is expected


# --- candidate precedence --------------------------------------


def test_posix_candidate_order() -> None:
    assert cli.candidate_interpreters({}, "linux") == [["python3"], ["python"]]
    assert cli.candidate_interpreters({}, "darwin") == [["python3"], ["python"]]


def test_simulated_windows_candidate_order() -> None:
    assert cli.candidate_interpreters({}, "win32") == [["py", "-3"], ["python.exe"], ["python3.exe"]]


def test_override_is_sole_candidate() -> None:
    assert cli.candidate_interpreters({"CODEMAP_PYTHON": "/opt/py"}, "linux") == [["/opt/py"]]
    assert cli.candidate_interpreters({"CODEMAP_PYTHON": "/opt/py"}, "win32") == [["/opt/py"]]


def test_resolve_prefers_python3_over_python() -> None:
    def _probe(candidate: list[str]) -> tuple[str, int, int] | None:
        """Report a supported interpreter for every candidate."""
        return ("cpython", 3, 12)

    resolved, diag = cli.resolve_interpreter({}, "linux", probe=_probe)
    assert resolved == ["python3"]
    assert diag is None


def test_resolve_skips_unsupported_then_takes_next() -> None:
    def _probe(candidate: list[str]) -> tuple[str, int, int] | None:
        """Reject Python 3.10 and accept the later fallback candidate."""
        return ("cpython", 3, 10) if candidate == ["python3"] else ("cpython", 3, 12)

    resolved, _ = cli.resolve_interpreter({}, "linux", probe=_probe)
    assert resolved == ["python"]


# --- invalid CODEMAP_PYTHON hard fail --------------------------


def test_invalid_override_missing_binary_hard_fails() -> None:
    def _probe(candidate: list[str]) -> tuple[str, int, int] | None:
        """Report the override candidate as unavailable and defaults as valid."""
        return None if candidate == ["/no/such/py"] else ("cpython", 3, 12)

    resolved, diag = cli.resolve_interpreter({"CODEMAP_PYTHON": "/no/such/py"}, "linux", probe=_probe)
    assert resolved is None
    assert diag is not None and "CODEMAP_PYTHON" in diag


def test_invalid_override_wrong_version_does_not_fall_through() -> None:
    def _probe(candidate: list[str]) -> tuple[str, int, int] | None:
        """Report the override as unsupported despite a valid default."""
        # Override is a real but unsupported CPython; defaults would be valid.
        return ("cpython", 3, 10) if candidate == ["python3.10"] else ("cpython", 3, 12)

    resolved, diag = cli.resolve_interpreter({"CODEMAP_PYTHON": "python3.10"}, "linux", probe=_probe)
    assert resolved is None
    assert diag is not None


def test_launcher_invalid_override_returns_127_empty_stdout() -> None:
    launcher = _PLUGIN_ROOT / "bin" / ("codemap-py.cmd" if sys.platform == "win32" else "codemap-py")
    # Inherit PATH so the shell/cmd interpreter itself is found.
    merged = {**os.environ, "CODEMAP_PYTHON": str(_PLUGIN_ROOT / "bin" / "does-not-exist")}
    completed = subprocess.run(
        [str(launcher), "doctor"], capture_output=True, text=True, timeout=30, check=False, env=merged
    )
    assert completed.returncode == _NO_INTERPRETER_EXIT
    assert completed.stdout == ""
    assert completed.stderr.strip() != ""


@pytest.mark.skipif(not _RUNNING_SUPPORTED, reason="wrapper delegates to this interpreter; must be supported")
def test_launcher_accepts_codemap_python_with_space(tmp_path: Path) -> None:
    """A CODEMAP_PYTHON path containing a space is a single argv element."""
    wrapper = tmp_path / ("py wrap.cmd" if sys.platform == "win32" else "py wrap")
    if sys.platform == "win32":
        wrapper.write_text(f'@echo off\n"{sys.executable}" %*\n', encoding="utf-8", newline="\r\n")
    else:
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
        wrapper.chmod(0o755)
    launcher = _PLUGIN_ROOT / "bin" / ("codemap-py.cmd" if sys.platform == "win32" else "codemap-py")
    merged = {**os.environ, "CODEMAP_PYTHON": str(wrapper)}
    completed = subprocess.run(
        [str(launcher), "doctor", "--json"], capture_output=True, text=True, timeout=30, check=False, env=merged
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["supported"] is True


# --- entry-gate rejection contract -----------------------------


def test_entry_gate_matches_running_interpreter() -> None:
    result = _run_entry(["doctor", "--json"])
    if _RUNNING_SUPPORTED:
        assert result.returncode == 0
    else:
        # 3.10 (or other unsupported) cell: exit 127, empty stdout, stderr diagnostic.
        assert result.returncode == _NO_INTERPRETER_EXIT
        assert result.stdout == ""
        assert result.stderr.strip() != ""


# --- ``doctor --json`` schema --------------------------------------------------


@pytest.mark.skipif(not _RUNNING_SUPPORTED, reason="doctor JSON schema is only defined on a supported interpreter")
def test_doctor_json_schema() -> None:
    result = _run_entry(["doctor", "--json"])
    assert result.returncode == 0
    report = json.loads(result.stdout)
    assert set(report) == {"python", "version", "implementation", "supported", "plugin_root", "index_path"}
    assert report["implementation"] == "cpython"
    assert report["supported"] is True
    assert Path(report["plugin_root"]).name == "codemap-py"

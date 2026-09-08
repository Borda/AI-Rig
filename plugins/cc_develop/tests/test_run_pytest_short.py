"""Tests for ``bin/run_pytest_short.py``.

The script validates ``PYTEST_CMD`` against the same allowlist as ``pytest_gate``, captures combined stdout+stderr from
pytest via incremental ``Popen`` reads (byte-capped at ``_MAX_OUTPUT_BYTES``), then prints only the last ``tail_n``
lines (default 20). Bad ``tail_n`` falls back to 20. ``subprocess.Popen`` is monkeypatched so no real pytest runs.
"""

from __future__ import annotations

import io
import subprocess
from pathlib import Path
from typing import Any

import pytest

import run_pytest_short  # type: ignore[import-not-found]


class _FakePopen:
    """Stand-in for ``subprocess.Popen`` exposing a readable ``stdout`` stream."""

    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        """Provide a return code and readable output stream for the runner."""
        self.returncode = returncode
        self.stdout = io.StringIO(stdout)

    def wait(self, timeout: float | None = None) -> int:
        """Return stored returncode; accepts optional timeout kwarg to match real Popen."""
        return self.returncode


def _patch_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    *,
    returncode: int = 0,
    stdout: str = "",
) -> list[dict[str, Any]]:
    """Patch ``subprocess.Popen`` to return a fake process; record call kwargs."""
    recorded: list[dict[str, Any]] = []

    def _fake_popen(cmd: list[str], **kwargs: Any) -> _FakePopen:
        """Record process argv/kwargs and return the configured fake process."""
        recorded.append({"cmd": list(cmd), **kwargs})
        return _FakePopen(returncode=returncode, stdout=stdout)

    monkeypatch.setattr(run_pytest_short.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(run_pytest_short, "which", lambda name: f"/fake/{name}")
    return recorded


def _make_lines(n: int) -> str:
    """Build a string with ``n`` numbered lines (``line-1`` … ``line-n``).

    Examples:
        >>> _make_lines(3)
        'line-1\\nline-2\\nline-3'
    """
    return "\n".join(f"line-{i + 1}" for i in range(n))


def test_default_tail_20(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """30 lines of output, default tail (20) → only lines 11..30 printed."""
    _patch_subprocess(monkeypatch, returncode=0, stdout=_make_lines(30))
    rc = run_pytest_short.main(["pytest", "."])
    assert rc == 0
    out_lines = capsys.readouterr().out.splitlines()
    assert out_lines == [f"line-{i}" for i in range(11, 31)]
    assert len(out_lines) == 20


def test_custom_tail_n(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Print only the requested number of trailing lines."""
    _patch_subprocess(monkeypatch, returncode=0, stdout=_make_lines(30))
    rc = run_pytest_short.main(["pytest", ".", "5"])
    assert rc == 0
    out_lines = capsys.readouterr().out.splitlines()
    assert out_lines == [f"line-{i}" for i in range(26, 31)]
    assert len(out_lines) == 5


@pytest.mark.parametrize("tail_n", ["abc", "-1"])
def test_bad_tail_n_falls_back_to_20(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tail_n: str,
) -> None:
    """Bad ``tail_n`` values silently use default 20."""
    _patch_subprocess(monkeypatch, returncode=0, stdout=_make_lines(25))
    rc = run_pytest_short.main(["pytest", ".", tail_n])
    assert rc == 0
    out_lines = capsys.readouterr().out.splitlines()
    assert len(out_lines) == 20
    assert out_lines == [f"line-{i}" for i in range(6, 26)]


def test_tail_n_larger_than_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Print all output when the requested tail exceeds its length."""
    _patch_subprocess(monkeypatch, returncode=0, stdout=_make_lines(3))
    rc = run_pytest_short.main(["pytest", ".", "100"])
    assert rc == 0
    out_lines = capsys.readouterr().out.splitlines()
    assert out_lines == ["line-1", "line-2", "line-3"]


@pytest.mark.parametrize(
    "tail_n,expected", [pytest.param("0", [], id="zero-lines"), pytest.param("1", ["line-3"], id="last-line")]
)
def test_numeric_tail_n_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tail_n: str,
    expected: list[str],
) -> None:
    """Numeric tail values at boundaries behave explicitly."""
    _patch_subprocess(monkeypatch, returncode=0, stdout=_make_lines(3))
    rc = run_pytest_short.main(["pytest", ".", tail_n])
    assert rc == 0
    assert capsys.readouterr().out.splitlines() == expected


def test_passes_through_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pytest exits 4 → ``main`` returns 4 unchanged."""
    _patch_subprocess(monkeypatch, returncode=4, stdout=_make_lines(5))
    assert run_pytest_short.main(["pytest"]) == 4


@pytest.mark.parametrize(
    "command",
    ["rm -rf /", "pytest; rm -rf /", "pytest && echo x", "uv run pytest; echo x", "python -m pytest -q"],
)
def test_rejects_unsafe_cmd(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    """Non-allowlisted cmd → exit 2; subprocess never invoked; stderr contains "rejected"."""
    recorded = _patch_subprocess(monkeypatch, returncode=0, stdout="")
    rc = run_pytest_short.main([command, "."])
    assert rc == 2
    assert recorded == []
    assert "rejected" in capsys.readouterr().err


def test_combined_stdout_stderr_captured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Subprocess invoked with ``stdout=PIPE`` and ``stderr=STDOUT`` for merged tailing."""
    recorded = _patch_subprocess(monkeypatch, returncode=0, stdout=_make_lines(5))
    run_pytest_short.main(["pytest"])
    assert len(recorded) == 1
    call = recorded[0]
    assert call["stdout"] == subprocess.PIPE
    assert call["stderr"] == subprocess.STDOUT
    assert call.get("text") is True


def test_rejects_target_outside_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Resolved target path outside ``Path.cwd()`` → exit 1; Popen never invoked."""
    recorded = _patch_subprocess(monkeypatch, returncode=0, stdout="")
    cwd_dir = tmp_path / "project"
    cwd_dir.mkdir()
    monkeypatch.chdir(cwd_dir)
    outside = tmp_path / "outside"
    outside.mkdir()
    rogue = outside / "leak"
    rogue.mkdir()
    with pytest.raises(SystemExit) as exc:
        run_pytest_short.main(["pytest", str(rogue)])
    assert exc.value.code == 1
    assert recorded == []
    assert "outside project directory" in capsys.readouterr().err


def test_rejects_relative_parent_target_outside_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Relative traversal targets are rejected after resolution."""
    recorded = _patch_subprocess(monkeypatch, returncode=0, stdout="")
    cwd_dir = tmp_path / "project"
    cwd_dir.mkdir()
    monkeypatch.chdir(cwd_dir)
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(SystemExit) as exc:
        run_pytest_short.main(["pytest", "../outside"])
    assert exc.value.code == 1
    assert recorded == []
    assert "outside project directory" in capsys.readouterr().err


def test_rejects_symlink_target_outside_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Symlink targets are rejected based on their resolved location."""
    recorded = _patch_subprocess(monkeypatch, returncode=0, stdout="")
    cwd_dir = tmp_path / "project"
    cwd_dir.mkdir()
    monkeypatch.chdir(cwd_dir)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = cwd_dir / "linked"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(SystemExit) as exc:
        run_pytest_short.main(["pytest", "linked"])
    assert exc.value.code == 1
    assert recorded == []
    assert "outside project directory" in capsys.readouterr().err


def test_resolves_first_token_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve only the executable in a multi-token test command."""
    recorded = _patch_subprocess(monkeypatch, returncode=0, stdout="")
    run_pytest_short.main(["uv run pytest", "tests/"])
    cmd = recorded[0]["cmd"]
    assert cmd[0] == "/fake/uv"
    assert cmd[1] == "run"
    assert cmd[2] == "pytest"


def test_output_byte_cap_truncates_buffer(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Oversized output is capped and annotated before tailing."""
    monkeypatch.setattr(run_pytest_short, "_MAX_OUTPUT_BYTES", 10)
    _patch_subprocess(monkeypatch, returncode=0, stdout="0123456789ABCDEFGHIJ")
    rc = run_pytest_short.main(["pytest", ".", "20"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "0123456789" in out
    assert "output truncated at 10 bytes" in out

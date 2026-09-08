"""Tests for ``bin/pytest_gate.py``.

The script validates ``PYTEST_CMD`` against an allowlist (``pytest``, ``uv run pytest``, ``python -m pytest``), then
execs pytest with ``--tb=short -v``. ``subprocess.run`` is monkeypatched so no real test runs; ``shutil.which`` is
patched so ``_resolve`` succeeds without the binary present.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

import pytest_gate  # type: ignore[import-not-found]

_RUNNER_DETECTION = Path(__file__).parents[1] / "skills" / "_shared" / "runner-detection.md"


def _runner_detection_pytest_cmds() -> list[str]:
    """Extract every pytest-invoking runner ``runner-detection.md`` can emit.

    Returns:
        Deduplicated list of ``TEST_CMD``/``PYTEST_CMD`` string values that contain
        ``pytest`` — these are exactly the commands that reach the gate allowlist.

    Examples:
        >>> "python -m pytest" in _runner_detection_pytest_cmds()
        True
    """
    text = _RUNNER_DETECTION.read_text(encoding="utf-8")
    values = re.findall(r'(?:TEST_CMD|PYTEST_CMD)="([^"]+)"', text)
    return sorted({v for v in values if "pytest" in v})


class _FakeCompleted:
    """Minimal stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode: int = 0) -> None:
        """Store the subprocess status exposed to the gate under test."""
        self.returncode = returncode


@pytest.fixture(name="captured_argv")
def _captured_argv(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Patch ``subprocess.run`` and ``shutil.which`` inside the script."""
    recorded: list[list[str]] = []

    def _fake_run(cmd: list[str], **_kwargs: Any) -> _FakeCompleted:
        """Record pytest argv and return success."""
        recorded.append(list(cmd))
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(pytest_gate.subprocess, "run", _fake_run)
    monkeypatch.setattr(pytest_gate, "which", lambda name: f"/fake/bin/{name}")
    return recorded


def test_runner_detection_outputs_are_allowlisted() -> None:
    """Every pytest runner ``runner-detection.md`` emits must pass the gate allowlist."""
    emitted = _runner_detection_pytest_cmds()
    assert emitted, "runner-detection.md yielded no pytest commands — parse regression"
    missing = [cmd for cmd in emitted if cmd not in pytest_gate._PYTEST_ALLOWLIST]
    assert not missing, f"runner-detection emits commands not in allowlist: {missing}"


def test_allowlisted_poetry_run(captured_argv: list[list[str]]) -> None:
    """Accept an allowlisted Poetry command and pass its fully resolved arguments."""
    rc = pytest_gate.main(["poetry run pytest", "tests/"])
    assert rc == 0
    assert captured_argv[0] == ["/fake/bin/poetry", "run", "pytest", "--tb=short", "tests/", "-v"]


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    """Print usage and exit 0 before any pytest run."""
    with pytest.raises(SystemExit) as exc:
        pytest_gate.main(["--help"])
    assert exc.value.code == 0
    assert "pytest_gate.py" in capsys.readouterr().out


def test_golden_invocation(captured_argv: list[list[str]]) -> None:
    """Golden call site ``pytest_gate.py "$PYTEST_CMD" <node_id>`` preserves argv shape."""
    rc = pytest_gate.main(["pytest", "tests/foo.py::test_bar"])
    assert rc == 0
    assert captured_argv[0] == ["/fake/bin/pytest", "--tb=short", "tests/foo.py::test_bar", "-v"]


def test_default_cmd_and_target(captured_argv: list[list[str]]) -> None:
    """Confirm no arguments invoke pytest with short tracebacks and verbose output."""
    rc = pytest_gate.main([])
    assert rc == 0
    assert len(captured_argv) == 1
    assert captured_argv[0] == ["/fake/bin/pytest", "--tb=short", ".", "-v"]


def test_allowlisted_uv_run(captured_argv: list[list[str]]) -> None:
    """Split a uv-based test command while preserving its complete argument vector."""
    rc = pytest_gate.main(["uv run pytest", "tests/"])
    assert rc == 0
    assert captured_argv[0] == ["/fake/bin/uv", "run", "pytest", "--tb=short", "tests/", "-v"]


def test_allowlisted_python_m_pytest(captured_argv: list[list[str]]) -> None:
    """Split a module-based Python test command and resolve its executable."""
    rc = pytest_gate.main(["python -m pytest", "tests/foo.py"])
    assert rc == 0
    assert captured_argv[0] == ["/fake/bin/python", "-m", "pytest", "--tb=short", "tests/foo.py", "-v"]


@pytest.mark.parametrize(
    "command",
    ["rm -rf /", "pytest; rm -rf /", "pytest && echo x", "uv run pytest; echo x", "python -m pytest -q"],
)
def test_rejects_unsafe_cmd(
    captured_argv: list[list[str]],
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    """Non-allowlisted cmd → exit 2; subprocess never invoked; stderr contains "rejected"."""
    rc = pytest_gate.main([command, "."])
    assert rc == 2
    assert captured_argv == []
    assert "rejected" in capsys.readouterr().err


def test_rejects_injection_payload(
    captured_argv: list[list[str]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Shell-injection-style payload not in allowlist → exit 2."""
    rc = pytest_gate.main(["python3 -c 'os.system(\"x\")'", "."])
    assert rc == 2
    assert captured_argv == []
    assert "rejected" in capsys.readouterr().err


def test_passes_through_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pytest exits 1 → ``main`` returns 1 unchanged."""

    def _fake_run(_cmd: list[str], **_kwargs: Any) -> _FakeCompleted:
        """Return pytest's ordinary test-failure exit status."""
        return _FakeCompleted(returncode=1)

    monkeypatch.setattr(pytest_gate.subprocess, "run", _fake_run)
    monkeypatch.setattr(pytest_gate, "which", lambda name: f"/fake/{name}")
    assert pytest_gate.main(["pytest", "tests/"]) == 1


def test_passes_through_collection_error_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pytest exits 5 (no tests collected) → ``main`` returns 5."""

    def _fake_run(_cmd: list[str], **_kwargs: Any) -> _FakeCompleted:
        """Return pytest's no-tests-collected exit status."""
        return _FakeCompleted(returncode=5)

    monkeypatch.setattr(pytest_gate.subprocess, "run", _fake_run)
    monkeypatch.setattr(pytest_gate, "which", lambda name: f"/fake/{name}")
    assert pytest_gate.main(["pytest"]) == 5


def test_rejects_target_outside_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_argv: list[list[str]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Resolved target path outside ``Path.cwd()`` → exit 1; pytest never invoked."""
    cwd_dir = tmp_path / "project"
    cwd_dir.mkdir()
    monkeypatch.chdir(cwd_dir)
    outside = tmp_path / "outside"
    outside.mkdir()
    rogue = outside / "leak"
    rogue.mkdir()
    with pytest.raises(SystemExit) as exc:
        pytest_gate.main(["pytest", str(rogue)])
    assert exc.value.code == 1
    assert captured_argv == []
    assert "outside project directory" in capsys.readouterr().err


def test_rejects_relative_parent_target_outside_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_argv: list[list[str]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Relative traversal targets are rejected after resolution."""
    cwd_dir = tmp_path / "project"
    cwd_dir.mkdir()
    monkeypatch.chdir(cwd_dir)
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(SystemExit) as exc:
        pytest_gate.main(["pytest", "../outside"])
    assert exc.value.code == 1
    assert captured_argv == []
    assert "outside project directory" in capsys.readouterr().err


def test_rejects_symlink_target_outside_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_argv: list[list[str]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Symlink targets are rejected based on their resolved location."""
    cwd_dir = tmp_path / "project"
    cwd_dir.mkdir()
    monkeypatch.chdir(cwd_dir)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = cwd_dir / "linked"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(SystemExit) as exc:
        pytest_gate.main(["pytest", "linked"])
    assert exc.value.code == 1
    assert captured_argv == []
    assert "outside project directory" in capsys.readouterr().err


def test_subprocess_called_without_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inherits stdout/stderr — no capture flags passed (full output streams through)."""
    recorded_kwargs: dict[str, Any] = {}

    def _fake_run(_cmd: list[str], **kwargs: Any) -> _FakeCompleted:
        """Capture subprocess keyword arguments while returning success."""
        recorded_kwargs.update(kwargs)
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(pytest_gate.subprocess, "run", _fake_run)
    monkeypatch.setattr(pytest_gate, "which", lambda name: f"/fake/{name}")
    pytest_gate.main(["pytest"])
    assert recorded_kwargs.get("capture_output") in (None, False)
    assert "stdout" not in recorded_kwargs or recorded_kwargs["stdout"] is None
    assert "stderr" not in recorded_kwargs or recorded_kwargs["stderr"] is None

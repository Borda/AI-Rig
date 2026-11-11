"""Health sentinels use the active temp root and session without adopting pre-existing files."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import health_monitor_start
import pytest


def test_health_sentinel_uses_tmpdir_and_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Session-scoped sentinel metadata names the active TMPDIR rather than a global path."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "health-session")
    monkeypatch.setattr(health_monitor_start.time, "time", lambda: 1234567890)
    assert health_monitor_start.main(["run"]) == 0
    sentinel = tmp_path / "research-run-check-1234567890-health-session"
    assert sentinel.is_file()
    assert capsys.readouterr().out == f"LAUNCH_AT=1234567890\nSENTINEL={sentinel.as_posix()}\n"


def test_existing_health_sentinel_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An existing session sentinel must not be silently accepted as a newly created health check."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "health-session")
    monkeypatch.setattr(health_monitor_start.time, "time", lambda: 1234567890)
    sentinel = tmp_path / "research-run-check-1234567890-health-session"
    sentinel.write_text("keep", encoding="utf-8")
    assert health_monitor_start.main(["run"]) == 1
    assert "cannot create sentinel" in capsys.readouterr().err
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_invalid_health_session_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A session identity cannot inject another path component into the health sentinel."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "../escape")
    assert health_monitor_start.main(["run"]) == 2
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("platform_name", ["posix", "nt"])
def test_unset_tmpdir_uses_the_hook_platform_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform_name: str
) -> None:
    """Health monitoring and hooks select the same temp root when TMPDIR is unset."""
    monkeypatch.setattr(health_monitor_start, "os", SimpleNamespace(name=platform_name, environ={}))
    monkeypatch.setattr(health_monitor_start.tempfile, "gettempdir", lambda: str(tmp_path))
    assert health_monitor_start._sentinel_dir() == (tmp_path if platform_name == "nt" else Path("/tmp"))


def test_absent_session_uses_shared_suffix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A process without a session identity uses the shared hook suffix."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.delenv("CSID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr(health_monitor_start.time, "time", lambda: 1234567890)
    health_monitor_start.main(["run"])
    assert (tmp_path / "research-run-check-1234567890-shared").is_file()

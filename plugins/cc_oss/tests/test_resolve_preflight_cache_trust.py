"""Cache-trust tests for ``bin/resolve_preflight.py``.

The preflight cache lives in the working tree (``.temp/state/preflight``), so a checked-out repository can ship its own
marker file. A marker must only count when its timestamp is in the past and within the TTL, and when the current user
owns it.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
import resolve_preflight as rp


def _write_marker(state_dir: Path, name: str, timestamp: int) -> None:
    """Write a ``<name>.ok`` preflight marker holding the given epoch seconds."""
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / f"{name}.ok").write_text(str(timestamp), encoding="utf-8")


class TestPreflightOk:
    """_preflight_ok: a marker is trusted only when recent, not from the future, and owned by the current user."""

    def test_recent_marker_is_trusted(self, tmp_path: Path) -> None:
        """A marker written a minute ago passes."""
        _write_marker(tmp_path, "gh", int(time.time()) - 60)
        assert rp._preflight_ok("gh", tmp_path) is True

    def test_future_timestamp_is_rejected(self, tmp_path: Path) -> None:
        """A marker dated in the future gives a negative age and must not pass for ever."""
        _write_marker(tmp_path, "gh", int(time.time()) + 10_000_000)
        assert rp._preflight_ok("gh", tmp_path) is False

    def test_expired_marker_is_rejected(self, tmp_path: Path) -> None:
        """A marker older than the TTL fails."""
        _write_marker(tmp_path, "gh", int(time.time()) - rp._PREFLIGHT_TTL - 10)
        assert rp._preflight_ok("gh", tmp_path) is False

    def test_marker_owned_by_another_user_is_rejected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A marker whose owner differs from the current uid is ignored, even when fresh."""
        _write_marker(tmp_path, "gh", int(time.time()) - 60)
        monkeypatch.setattr(rp.os, "getuid", lambda: -1, raising=False)
        assert rp._preflight_ok("gh", tmp_path) is False


class TestPreflightDir:
    """_preflight_dir: the cache is anchored at the repository root, not at wherever the process was started."""

    def test_subdirectory_uses_repository_root_cache(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Run from a nested directory, the cache still resolves to the root that holds the repository marker.

        A cwd-relative path would create a second, unread cache under the subdirectory on every such invocation.
        """
        (tmp_path / ".git").mkdir()
        nested = tmp_path / "pkg" / "sub"
        nested.mkdir(parents=True)
        monkeypatch.chdir(nested)
        assert rp._preflight_dir().resolve() == (tmp_path / ".temp" / "state" / "preflight").resolve()

    def test_outside_repository_falls_back_to_working_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no repository marker on the way up, the cache stays under the working directory."""
        monkeypatch.chdir(tmp_path)
        assert rp._preflight_dir().resolve() == (tmp_path / ".temp" / "state" / "preflight").resolve()


class TestFailedPreflightWritesNoSentinel:
    """A failed gh check must leave no GH_OK sentinel behind for the caller's default to mask."""

    def test_unauthenticated_gh_writes_no_gh_ok(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Failed auth returns 1 before any output sentinel is written.

        The caller defaults a missing ``GH_OK`` to true, so the failure path must exit before the sentinel is written.
        """
        failed = type("Completed", (), {"returncode": 1, "stdout": "", "stderr": ""})()
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.setenv("CSID", "failed-gh")
        monkeypatch.setattr(rp, "which", lambda cmd: "/fake/" + cmd)
        monkeypatch.setattr(rp.subprocess, "run", lambda *_args, **_kwargs: failed)
        monkeypatch.chdir(tmp_path)
        assert rp.main([]) == 1
        assert not (tmp_path / "resolve-preflight-GH_OK-failed-gh").exists()

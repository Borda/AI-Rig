"""Tests for ``bin/health_sentinel.py``.

Both subcommands are filesystem-bound; covered via ``tmp_path``, ``monkeypatch`` (to inject deterministic timestamps)
and ``capsys`` to assert the eval-able stdout contract for ``start``.
"""

from __future__ import annotations

import os
import shlex
import time
from pathlib import Path

import pytest


import health_sentinel  # noqa: E402


class TestCreateSentinel:
    """create_sentinel: file creation + return tuple."""

    def test_creates_file_with_expected_name(self, tmp_path: Path) -> None:
        """Sentinel filename embeds skill_id and timestamp."""
        launch_at, sentinel = health_sentinel.create_sentinel("audit", tmp_dir=tmp_path, now=1700000000)
        assert launch_at == 1700000000
        assert sentinel == tmp_path / "foundry-audit-check-1700000000"
        assert sentinel.exists()

    def test_now_defaults_to_current_time(self, tmp_path: Path) -> None:
        """When ``now`` is omitted, an integer near time.time() is used."""
        before = int(time.time())
        launch_at, sentinel = health_sentinel.create_sentinel("calibrate", tmp_dir=tmp_path)
        after = int(time.time())
        assert before <= launch_at <= after
        assert sentinel.exists()


class TestHasNewFiles:
    """has_new_files: detect activity newer than sentinel mtime."""

    def test_missing_sentinel_returns_false(self, tmp_path: Path) -> None:
        """Sentinel absent → considered stalled (False)."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        assert health_sentinel.has_new_files(tmp_path / "ghost", run_dir) is False

    def test_missing_run_dir_returns_false(self, tmp_path: Path) -> None:
        """Run dir absent → considered stalled (False)."""
        sentinel = tmp_path / "sentinel"
        sentinel.touch()
        assert health_sentinel.has_new_files(sentinel, tmp_path / "ghost") is False

    def test_no_newer_files(self, tmp_path: Path) -> None:
        """All files older than sentinel → False."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        old = run_dir / "old.txt"
        old.write_text("", encoding="utf-8")
        old_mtime = old.stat().st_mtime - 60
        os.utime(old, (old_mtime, old_mtime))

        sentinel = tmp_path / "sentinel"
        sentinel.touch()  # sentinel created AFTER the old file
        assert health_sentinel.has_new_files(sentinel, run_dir) is False

    def test_finds_newer_file(self, tmp_path: Path) -> None:
        """File mtime > sentinel mtime → True."""
        sentinel = tmp_path / "sentinel"
        sentinel.touch()
        sentinel_mtime = sentinel.stat().st_mtime

        run_dir = tmp_path / "run"
        run_dir.mkdir()
        new_file = run_dir / "fresh.txt"
        new_file.write_text("", encoding="utf-8")
        future = sentinel_mtime + 60
        os.utime(new_file, (future, future))

        assert health_sentinel.has_new_files(sentinel, run_dir) is True

    def test_recursive_descent(self, tmp_path: Path) -> None:
        """Nested file newer than sentinel still detected."""
        sentinel = tmp_path / "sentinel"
        sentinel.touch()
        sentinel_mtime = sentinel.stat().st_mtime

        nested = tmp_path / "run" / "deep" / "deeper" / "file.txt"
        nested.parent.mkdir(parents=True)
        nested.write_text("", encoding="utf-8")
        future = sentinel_mtime + 60
        os.utime(nested, (future, future))

        assert health_sentinel.has_new_files(sentinel, tmp_path / "run") is True

    def test_empty_run_dir(self, tmp_path: Path) -> None:
        """Run dir exists but empty → False."""
        sentinel = tmp_path / "sentinel"
        sentinel.touch()
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        assert health_sentinel.has_new_files(sentinel, run_dir) is False


class TestStartCommand:
    """Emit the two-line shell contract when starting a health sentinel."""

    def test_prints_two_eval_able_lines(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Output is exactly two lines: ``LAUNCH_AT=<int>`` (bare) then ``SENTINEL=<shlex-quoted-path>``."""
        # Capture real create_sentinel BEFORE patching so the closure does not recurse.
        real_create = health_sentinel.create_sentinel

        def _fixed_create(skill_id: str) -> tuple[int, Path]:
            """Create a sentinel with this test's fixed clock and temp directory."""
            return real_create(skill_id, tmp_dir=tmp_path, now=1700000000)

        monkeypatch.setattr(health_sentinel, "create_sentinel", _fixed_create)

        rc = health_sentinel.main(["start", "audit"])
        assert rc == 0

        out = capsys.readouterr().out
        lines = out.splitlines()
        assert len(lines) == 2
        # LAUNCH_AT is a bare integer — no quoting needed; shlex.quote handles SENTINEL path.
        assert lines[0] == "LAUNCH_AT=1700000000"
        expected_path = tmp_path / "foundry-audit-check-1700000000"
        assert lines[1] == f"SENTINEL={shlex.quote(expected_path.as_posix())}"
        assert expected_path.exists()

    def test_start_requires_skill_id(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Missing ``<skill-id>`` → argparse exits 2."""
        with pytest.raises(SystemExit) as exc:
            health_sentinel.main(["start"])
        assert exc.value.code == 2
        # argparse error goes to stderr; capsys returns empty when test runs in isolation.
        assert capsys.readouterr().err is not None


class TestCheckCommand:
    """Report sentinel health through the documented exit codes."""

    def test_exit_0_when_files_newer(self, tmp_path: Path) -> None:
        """File newer than sentinel → exit 0 (alive)."""
        sentinel = tmp_path / "sentinel"
        sentinel.touch()
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        new = run_dir / "fresh"
        new.write_text("", encoding="utf-8")
        future = sentinel.stat().st_mtime + 60
        os.utime(new, (future, future))

        assert health_sentinel.main(["check", str(sentinel), str(run_dir)]) == 0

    def test_exit_1_when_stalled(self, tmp_path: Path) -> None:
        """No newer files → exit 1 (stalled)."""
        sentinel = tmp_path / "sentinel"
        sentinel.touch()
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        assert health_sentinel.main(["check", str(sentinel), str(run_dir)]) == 1

    def test_exit_1_when_sentinel_missing(self, tmp_path: Path) -> None:
        """Missing sentinel path → exit 1."""
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        assert health_sentinel.main(["check", str(tmp_path / "ghost"), str(run_dir)]) == 1

    def test_exit_1_when_run_dir_missing(self, tmp_path: Path) -> None:
        """Missing run dir → exit 1."""
        sentinel = tmp_path / "sentinel"
        sentinel.touch()
        assert health_sentinel.main(["check", str(sentinel), str(tmp_path / "ghost")]) == 1


class TestMainBadArgs:
    """Main: argparse-driven error reporting."""

    def test_no_subcommand(self) -> None:
        """No subcommand → argparse exits 2."""
        with pytest.raises(SystemExit) as exc:
            health_sentinel.main([])
        assert exc.value.code == 2

    def test_unknown_subcommand(self) -> None:
        """Unknown subcommand → argparse exits 2."""
        with pytest.raises(SystemExit) as exc:
            health_sentinel.main(["frobnicate"])
        assert exc.value.code == 2


class TestMainSkillIdValidation:
    """Reject unsafe skill identifiers when starting a health sentinel."""

    @pytest.mark.parametrize("skill_id", ["a;b", "../traverse", "rm -rf /", "$(cmd)", "", " spaces"])
    def test_invalid_skill_id_returns_2(
        self,
        skill_id: str,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Disallowed chars in skill_id → exit 2, 'invalid skill_id' in stderr."""
        rc = health_sentinel.main(["start", skill_id])
        assert rc == 2
        assert "invalid skill_id" in capsys.readouterr().err

    def test_valid_skill_id_accepted(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Valid skill_id (alphanum + _ -) passes validation and creates sentinel."""
        real_create = health_sentinel.create_sentinel

        def _fixed_create(sid: str) -> tuple[int, Path]:
            """Create a sentinel with this test's fixed clock and temp directory."""
            return real_create(sid, tmp_dir=tmp_path, now=1700000000)

        monkeypatch.setattr(health_sentinel, "create_sentinel", _fixed_create)
        rc = health_sentinel.main(["start", "valid-skill_1"])
        assert rc == 0

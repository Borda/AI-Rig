"""Tests for ``bin/dev_run_dir.py`` — develop timestamped run-dir creator.

Covers:
* Happy-path ``.developments/<ts>/`` creation
* ``--sentinel <name>`` touches sentinel file in the ``$TMPDIR`` sentinel dir
* Sentinel name sanitization (path traversal stripped)
* ``--sentinel`` alone (no name) creates no sentinel
* Sentinel creation refuses to follow a pre-planted symlink
* Portability invariant: no ``datetime.utcnow()`` (deprecated in 3.12)
"""

from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import dev_run_dir
import pytest

SCRIPT = Path(dev_run_dir.__file__)
TIMESTAMP_RE = re.compile(r"\.developments/\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z$")
FROZEN_TS = "2026-05-22T10-00-00Z"


@pytest.fixture(name="sentinel_dir")
def _sentinel_dir_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``_sentinel_dir()`` at an isolated dir via ``$TMPDIR``, kept apart from the CWD run dir.

    >>> getfixture("sentinel_dir").is_dir()
    True
    """
    base = tmp_path / "sentinels"
    base.mkdir()
    monkeypatch.setenv("TMPDIR", str(base))
    return base


@pytest.fixture(name="frozen_clock")
def _frozen_clock(monkeypatch: pytest.MonkeyPatch) -> str:
    """Pin ``main()``'s timestamp to ``FROZEN_TS`` so a sentinel path can be predicted exactly.

    >>> getfixture("frozen_clock")
    '2026-05-22T10-00-00Z'
    """
    frozen = datetime(2026, 5, 22, 10, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(dev_run_dir, "datetime", SimpleNamespace(now=lambda tz: frozen.astimezone(tz)))
    return FROZEN_TS


class TestSentinelDirResolution:
    """Resolve sentinel storage from the same temporary-directory setting as shell callers."""

    def test_honors_tmpdir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Use the configured temporary directory instead of a hardcoded path."""
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        assert dev_run_dir._sentinel_dir() == tmp_path

    def test_falls_back_when_tmpdir_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Use the platform temporary directory when no override is configured."""
        monkeypatch.delenv("TMPDIR", raising=False)
        assert dev_run_dir._sentinel_dir() == Path(tempfile.gettempdir())

    def test_tracks_tmpdir_change_after_first_call(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Env is read live — a later ``$TMPDIR`` change is observed despite gettempdir() caching."""
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        dev_run_dir._sentinel_dir()
        second = tmp_path / "second"
        second.mkdir()
        monkeypatch.setenv("TMPDIR", str(second))
        assert dev_run_dir._sentinel_dir() == second


class TestRunDirCreation:
    """Tests for ``.developments/<ts>/`` creation."""

    def test_creates_developments_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Create ``.developments/<ts>/`` under CWD."""
        monkeypatch.chdir(tmp_path)
        rc = dev_run_dir.main([])
        assert rc == 0
        dev_dirs = list((tmp_path / ".developments").iterdir())
        assert len(dev_dirs) == 1
        assert dev_dirs[0].is_dir()

    def test_timestamp_format(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Output path matches ``.developments/YYYY-MM-DDTHH-MM-SSZ`` pattern."""
        monkeypatch.chdir(tmp_path)
        dev_run_dir.main([])
        out = capsys.readouterr().out.strip()
        assert TIMESTAMP_RE.search(out)

    def test_output_has_no_crlf(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Stdout must not contain CRLF (Windows text-mode regression guard)."""
        monkeypatch.chdir(tmp_path)
        dev_run_dir.main([])
        out = capsys.readouterr().out
        assert "\r" not in out

    def test_sentinel_path_absent_from_stdout(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        sentinel_dir: Path,
    ) -> None:
        """Stdout stays a single run-dir line — callers capture it whole via ``DEV_DIR=$(...)``."""
        monkeypatch.chdir(tmp_path)
        dev_run_dir.main(["--sentinel", "dev-quiet"])
        lines = capsys.readouterr().out.strip().splitlines()
        assert len(lines) == 1
        assert str(sentinel_dir) not in lines[0]


class TestSentinelFlag:
    """Tests for ``--sentinel <name>`` behaviour."""

    def test_sentinel_created(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        sentinel_dir: Path,
    ) -> None:
        """Touch sentinel in the ``$TMPDIR`` sentinel dir."""
        monkeypatch.chdir(tmp_path)
        sentinel_name = f"dev-py-test-{os.getpid()}"
        rc = dev_run_dir.main(["--sentinel", sentinel_name])
        assert rc == 0
        ts = capsys.readouterr().out.strip().split("/")[-1]
        assert (sentinel_dir / f"{sentinel_name}-{ts}").exists()

    @pytest.mark.parametrize(
        "raw_name,expected_sanitized",
        [
            pytest.param("../evil-{pid}", "evil-{pid}", id="posix-traversal"),
            pytest.param(r"..\evil-{pid}", "evil-{pid}", id="windows-traversal"),
            pytest.param("name with spaces-{pid}", "namewithspaces-{pid}", id="spaces"),
            pytest.param("safe_MIX-123-{pid}", "safe_MIX-123-{pid}", id="already-safe"),
        ],
    )
    def test_sentinel_name_sanitized(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        sentinel_dir: Path,
        raw_name: str,
        expected_sanitized: str,
    ) -> None:
        """Unsafe sentinel characters are stripped before touching the sentinel file."""
        monkeypatch.chdir(tmp_path)
        pid = os.getpid()
        raw_name = raw_name.format(pid=pid)
        sanitized = expected_sanitized.format(pid=pid)
        rc = dev_run_dir.main(["--sentinel", raw_name])
        assert rc == 0
        ts = capsys.readouterr().out.strip().split("/")[-1]
        assert (sentinel_dir / f"{sanitized}-{ts}").exists()
        if raw_name != sanitized:
            assert not (sentinel_dir / f"{raw_name}-{ts}").exists()

    def test_all_unsafe_sentinel_name_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sentinel_dir: Path
    ) -> None:
        """A sentinel name that sanitizes to empty does not create a broad timestamp file."""
        monkeypatch.chdir(tmp_path)
        dev_run_dir.main(["--sentinel", "!!!"])
        assert list(sentinel_dir.iterdir()) == []

    def test_sentinel_without_name_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sentinel_dir: Path
    ) -> None:
        """Avoid creating a sentinel when its name is omitted."""
        monkeypatch.chdir(tmp_path)
        dev_run_dir.main(["--sentinel"])
        assert list(sentinel_dir.iterdir()) == []


class TestSentinelSymlinkHardening:
    """A hostile symlink pre-planted in the temp dir must not be followed, nor break the caller."""

    def test_symlink_swap_before_hard_link_is_not_followed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sentinel_dir: Path, frozen_clock: str
    ) -> None:
        """A symlink installed immediately before the atomic final creation cannot create its target."""
        monkeypatch.chdir(tmp_path)
        victim = tmp_path / "victim"
        sentinel_path = sentinel_dir / f"dev-evil-{frozen_clock}"
        original_link = dev_run_dir.os.link
        link_destinations: list[Path] = []

        def _link_after_symlink_swap(
            source: str | bytes | os.PathLike[str], destination: str | bytes | os.PathLike[str]
        ) -> None:
            """Install the attacker link at the last possible point before final-path creation."""
            assert Path(destination) == sentinel_path
            link_destinations.append(Path(destination))
            sentinel_path.symlink_to(victim)
            original_link(source, destination)

        monkeypatch.setattr(dev_run_dir.os, "link", _link_after_symlink_swap)

        rc = dev_run_dir.main(["--sentinel", "dev-evil"])

        assert rc == 0
        assert link_destinations == [sentinel_path]
        assert sentinel_path.is_symlink()
        assert not victim.exists()

    def test_symlink_target_not_created(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sentinel_dir: Path, frozen_clock: str
    ) -> None:
        """Sentinel path pre-planted as a symlink — its target must not be created through it."""
        monkeypatch.chdir(tmp_path)
        victim = tmp_path / "victim"
        (sentinel_dir / f"dev-evil-{frozen_clock}").symlink_to(victim)
        dev_run_dir.main(["--sentinel", "dev-evil"])
        assert not victim.exists()

    def test_existing_symlink_target_mtime_preserved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sentinel_dir: Path, frozen_clock: str
    ) -> None:
        """An existing symlink target keeps its mtime — ``touch()`` would have clobbered it."""
        monkeypatch.chdir(tmp_path)
        victim = tmp_path / "victim"
        victim.write_text("original", encoding="utf-8")
        os.utime(victim, (0, 0))
        (sentinel_dir / f"dev-evil-{frozen_clock}").symlink_to(victim)
        dev_run_dir.main(["--sentinel", "dev-evil"])
        assert victim.stat().st_mtime == 0

    def test_exit_zero_contract_survives_hostile_symlink(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        sentinel_dir: Path,
        frozen_clock: str,
    ) -> None:
        """Refused sentinel still returns 0 and still emits the single-line stdout contract."""
        monkeypatch.chdir(tmp_path)
        (sentinel_dir / f"dev-evil-{frozen_clock}").symlink_to(tmp_path / "victim")
        rc = dev_run_dir.main(["--sentinel", "dev-evil"])
        assert rc == 0
        assert len(capsys.readouterr().out.strip().splitlines()) == 1


class TestHelp:
    """Short-circuit before any run-dir side effects."""

    def test_help_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Print usage to stdout and exit 0 (argparse default)."""
        with pytest.raises(SystemExit) as exc:
            dev_run_dir.main(["--help"])
        assert exc.value.code == 0
        assert "usage" in capsys.readouterr().out.lower()

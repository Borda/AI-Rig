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


@pytest.fixture(name="workdir")
def _workdir_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run from an empty temp directory so ``.developments/`` lands under the test's own tree.

    >>> getfixture("workdir").is_dir()
    True
    """
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture(name="sentinel_dir")
def _sentinel_dir_fixture(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``_sentinel_dir()`` at an isolated dir via ``$TMPDIR``, kept apart from the CWD run dir.

    >>> getfixture("sentinel_dir").is_dir()
    True
    """
    base = workdir / "sentinels"
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

    @pytest.mark.skipif(os.name == "nt", reason="native Windows has no shell /tmp; it keeps tempfile.gettempdir()")
    def test_falls_back_to_shell_tmp_when_tmpdir_unset(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Use ``/tmp``, the shell's ``${TMPDIR:-/tmp}`` default, even when ``TEMP`` or ``TMP`` is set.

        ``tempfile.gettempdir()`` honours ``TEMP`` and ``TMP`` before ``/tmp``; a skill block that wrote the runner
        sentinel under ``/tmp`` would then never be found by this script, and the test wrapper would report no run.
        """
        monkeypatch.delenv("TMPDIR", raising=False)
        monkeypatch.setenv("TEMP", str(tmp_path))
        monkeypatch.setenv("TMP", str(tmp_path))
        assert dev_run_dir._sentinel_dir() == Path("/tmp")

    @pytest.mark.skipif(os.name != "nt", reason="the gettempdir() fallback applies on native Windows only")
    def test_falls_back_to_platform_temp_on_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Use the platform temporary directory on native Windows when no override is configured."""
        monkeypatch.delenv("TMPDIR", raising=False)
        assert dev_run_dir._sentinel_dir() == Path(tempfile.gettempdir())

    def test_tracks_tmpdir_change_after_first_call(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Env is read live — a later ``$TMPDIR`` change is observed despite gettempdir() caching.

        A skill block may export a new ``$TMPDIR`` between calls; a cached first answer would send sentinels to a
        directory the shell no longer reads.
        """
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        dev_run_dir._sentinel_dir()
        second = tmp_path / "second"
        second.mkdir()
        monkeypatch.setenv("TMPDIR", str(second))
        assert dev_run_dir._sentinel_dir() == second


class TestRunDirCreation:
    """Creation of ``.developments/<ts>/`` and the single-line stdout contract."""

    def test_creates_timestamped_dir_and_prints_its_path(
        self, workdir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Create one ``.developments/<ts>/`` under the CWD and print its relative path without CRLF.

        Callers capture stdout whole via ``DEV_DIR=$(...)``, so the line must match ``YYYY-MM-DDTHH-MM-SSZ`` exactly and
        carry no carriage return (Windows text-mode regression guard).
        """
        rc = dev_run_dir.main([])
        out = capsys.readouterr().out
        created = [p.is_dir() for p in (workdir / ".developments").iterdir()]
        assert (rc, created, bool(TIMESTAMP_RE.search(out.strip())), "\r" in out) == (0, [True], True, False)

    def test_sentinel_path_absent_from_stdout(self, capsys: pytest.CaptureFixture[str], sentinel_dir: Path) -> None:
        """Stdout stays a single run-dir line — callers capture it whole via ``DEV_DIR=$(...)``."""
        dev_run_dir.main(["--sentinel", "dev-quiet"])
        lines = capsys.readouterr().out.strip().splitlines()
        assert (len(lines), str(sentinel_dir) in lines[0]) == (1, False)


class TestSentinelFlag:
    """Behaviour of ``--sentinel <name>``."""

    @pytest.mark.parametrize(
        ("raw_name", "sanitized"),
        [
            pytest.param("dev-py-test", "dev-py-test", id="plain-name"),
            pytest.param("safe_MIX-123", "safe_MIX-123", id="already-safe"),
            pytest.param("../evil", "evil", id="posix-traversal"),
            pytest.param(r"..\evil", "evil", id="windows-traversal"),
            pytest.param("name with spaces", "namewithspaces", id="spaces"),
        ],
    )
    def test_touches_sanitized_sentinel(
        self, capsys: pytest.CaptureFixture[str], sentinel_dir: Path, raw_name: str, sanitized: str
    ) -> None:
        """Unsafe sentinel characters are stripped and exactly one ``<name>-<ts>`` file appears in the sentinel dir.

        Sentinel names come from skill blocks; a traversal sequence must never steer the touch out of the temp dir, and
        a safe name must pass through unchanged.
        """
        rc = dev_run_dir.main(["--sentinel", raw_name])
        ts = capsys.readouterr().out.strip().split("/")[-1]
        assert (rc, [p.name for p in sentinel_dir.iterdir()]) == (0, [f"{sanitized}-{ts}"])

    @pytest.mark.parametrize(
        "argv",
        [
            # a name that sanitizes to empty must not create a broad timestamp file
            pytest.param(["--sentinel", "!!!"], id="all-unsafe-name"),
            pytest.param(["--sentinel"], id="name-omitted"),
        ],
    )
    def test_skips_sentinel_without_usable_name(self, sentinel_dir: Path, argv: list[str]) -> None:
        """No sentinel is created when its name is all unsafe characters or omitted entirely."""
        dev_run_dir.main(argv)
        assert list(sentinel_dir.iterdir()) == []


class TestSentinelSymlinkHardening:
    """A hostile symlink pre-planted in the temp dir must not be followed, nor break the caller."""

    def test_symlink_swap_before_hard_link_is_not_followed(
        self, workdir: Path, monkeypatch: pytest.MonkeyPatch, sentinel_dir: Path, frozen_clock: str
    ) -> None:
        """A symlink installed immediately before the atomic final creation cannot create its target.

        The attacker wins the race between the existence check and ``os.link``; the final creation must still refuse to
        write through the link, leaving the victim path absent.
        """
        victim = workdir / "victim"
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

        assert (rc, link_destinations, sentinel_path.is_symlink(), victim.exists()) == (
            0,
            [sentinel_path],
            True,
            False,
        )

    def test_planted_symlink_target_not_created(self, workdir: Path, sentinel_dir: Path, frozen_clock: str) -> None:
        """Sentinel path pre-planted as a symlink — its target must not be created through it."""
        victim = workdir / "victim"
        (sentinel_dir / f"dev-evil-{frozen_clock}").symlink_to(victim)
        dev_run_dir.main(["--sentinel", "dev-evil"])
        assert not victim.exists()

    def test_planted_symlink_target_mtime_preserved(self, workdir: Path, sentinel_dir: Path, frozen_clock: str) -> None:
        """An existing symlink target keeps its mtime — ``touch()`` would have clobbered it."""
        victim = workdir / "victim"
        victim.write_text("original", encoding="utf-8")
        os.utime(victim, (0, 0))
        (sentinel_dir / f"dev-evil-{frozen_clock}").symlink_to(victim)
        dev_run_dir.main(["--sentinel", "dev-evil"])
        assert victim.stat().st_mtime == 0

    def test_exit_zero_contract_survives_hostile_symlink(
        self, workdir: Path, capsys: pytest.CaptureFixture[str], sentinel_dir: Path, frozen_clock: str
    ) -> None:
        """Refused sentinel still returns 0 and still emits the single-line stdout contract.

        The sentinel is advisory; a hostile link must not turn a successful run-dir creation into a failure the skill
        would have to handle.
        """
        (sentinel_dir / f"dev-evil-{frozen_clock}").symlink_to(workdir / "victim")
        rc = dev_run_dir.main(["--sentinel", "dev-evil"])
        assert (rc, len(capsys.readouterr().out.strip().splitlines())) == (0, 1)


class TestHelp:
    """Short-circuit before any run-dir side effects."""

    def test_exits_zero_with_usage(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Print usage to stdout and exit 0 (argparse default)."""
        with pytest.raises(SystemExit) as exc:
            dev_run_dir.main(["--help"])
        assert (exc.value.code, "usage" in capsys.readouterr().out.lower()) == (0, True)

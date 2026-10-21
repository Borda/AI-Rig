"""Tests for ``dev_run_dir.py --persist <skill>`` — the dev-dir sentinel the develop skills read back.

Covers:
* The sentinel holds the same path the script prints, under the name later blocks read
* ``CSID`` falls back to ``CLAUDE_CODE_SESSION_ID`` and then ``shared``
* Unsafe skill slugs are stripped; an empty slug writes nothing and warns without failing the caller
* An existing sentinel (regular file or link) is replaced, never followed
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import dev_run_dir
import pytest


def _symlinks_supported() -> bool:
    """Probe whether this host lets the current user create symlinks (native Windows often does not)."""
    with tempfile.TemporaryDirectory() as scratch:
        try:
            os.symlink(Path(scratch) / "target", Path(scratch) / "link")
        except OSError:
            return False
    return True


_skip_no_symlinks = pytest.mark.skipif(not _symlinks_supported(), reason="host cannot create symlinks")


@pytest.fixture(name="sentinel_dir")
def _sentinel_dir_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate ``$TMPDIR`` and the CWD so each test sees only its own sentinels and ``.developments/``.

    >>> getfixture("sentinel_dir").is_dir()
    True
    """
    base = tmp_path / "sentinels"
    base.mkdir()
    monkeypatch.setenv("TMPDIR", str(base))
    monkeypatch.chdir(tmp_path)
    return base


class TestPersistSentinel:
    """The sentinel carries the printed run-dir path under ``dev-<skill>-dev-dir-<CSID>``."""

    def test_sentinel_matches_printed_path(
        self, sentinel_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A later ``IFS= read -r DEV_DIR`` gets exactly the path the caller captured from stdout.

        Feature, fix and refactor each used to echo this value themselves; the sentinel must stay byte-identical to that
        echo (path plus one trailing newline) so every downstream read keeps working unchanged.
        """
        monkeypatch.setenv("CSID", "abc123")
        rc = dev_run_dir.main(["--persist", "fix"])
        printed = capsys.readouterr().out
        assert rc == 0
        assert (sentinel_dir / "dev-fix-dev-dir-abc123").read_bytes() == printed.encode("utf-8")

    @pytest.mark.parametrize(
        ("csid", "session", "expected_suffix"),
        [
            pytest.param("explicit", "from-session", "explicit", id="csid-wins"),
            pytest.param(None, "from-session", "from-session", id="session-id-fallback"),
            pytest.param(None, None, "shared", id="shared-fallback"),
        ],
    )
    def test_csid_resolution_order(
        self,
        sentinel_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        csid: str | None,
        session: str | None,
        expected_suffix: str,
    ) -> None:
        """Session token follows ``CSID``, then ``CLAUDE_CODE_SESSION_ID``, then ``shared``."""
        for name, value in (("CSID", csid), ("CLAUDE_CODE_SESSION_ID", session)):
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        dev_run_dir.main(["--persist", "refactor"])
        assert [p.name for p in sentinel_dir.iterdir()] == [f"dev-refactor-dev-dir-{expected_suffix}"]

    def test_no_persist_flag_writes_no_sentinel(self, sentinel_dir: Path) -> None:
        """Without ``--persist`` the script touches nothing in the sentinel dir (legacy contract)."""
        dev_run_dir.main([])
        assert list(sentinel_dir.iterdir()) == []

    def test_unsafe_slug_is_stripped(self, sentinel_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Path separators and dots in the skill slug cannot steer the write out of the sentinel dir."""
        monkeypatch.setenv("CSID", "s1")
        dev_run_dir.main(["--persist", "../feature"])
        assert [p.name for p in sentinel_dir.iterdir()] == ["dev-feature-dev-dir-s1"]

    def test_empty_slug_warns_and_still_succeeds(self, sentinel_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A slug that sanitizes to nothing writes no sentinel, warns on stderr, and keeps the run dir and exit 0."""
        rc = dev_run_dir.main(["--persist", "///"])
        captured = capsys.readouterr()
        assert (rc, list(sentinel_dir.iterdir())) == (0, [])
        assert "could not persist" in captured.err
        assert captured.out.startswith(".developments/")


class TestPersistReplacesExistingEntry:
    """A leftover or hostile entry at the predictable sentinel name is replaced, never written through."""

    def test_stale_sentinel_is_overwritten(self, sentinel_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A sentinel from an earlier run in the same session is replaced by the new run dir."""
        monkeypatch.setenv("CSID", "s2")
        target = sentinel_dir / "dev-feature-dev-dir-s2"
        target.write_text("stale\n", encoding="utf-8")
        dev_run_dir.main(["--persist", "feature"])
        assert target.read_text(encoding="utf-8").startswith(".developments/")

    @_skip_no_symlinks
    def test_planted_symlink_is_replaced_not_followed(
        self, sentinel_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A symlink planted at the sentinel name leaves its target untouched and is itself replaced."""
        monkeypatch.setenv("CSID", "s3")
        victim = tmp_path / "victim"
        victim.write_text("keep\n", encoding="utf-8")
        target = sentinel_dir / "dev-fix-dev-dir-s3"
        target.symlink_to(victim)
        dev_run_dir.main(["--persist", "fix"])
        assert (victim.read_text(encoding="utf-8"), target.is_symlink()) == ("keep\n", False)

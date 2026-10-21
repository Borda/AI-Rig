"""Tests for ``bin/setup_worktree.py`` — team-mode subagent handoff dir creator.

Covers:
* Two-line output: timestamp on line 1, ``.temp/develop/<ts>`` on line 2
* Directory created on disk
* The legacy ``--sentinel`` option is gone: no sentinel file is created, and the extra tokens are ignored with exit 0
* Portability invariant: no ``datetime.utcnow()`` (deprecated in 3.12)
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import setup_worktree

SCRIPT = Path(setup_worktree.__file__)
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z$")


class TestRunDirCreation:
    """Tests for ``.temp/develop/<ts>/`` creation and two-line output."""

    def test_creates_run_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Create ``.temp/develop/<ts>/`` under CWD."""
        monkeypatch.chdir(tmp_path)
        rc = setup_worktree.main([])
        assert rc == 0
        assert (tmp_path / ".temp" / "develop").is_dir()

    def test_output_two_lines(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Output is exactly two non-empty lines."""
        monkeypatch.chdir(tmp_path)
        setup_worktree.main([])
        lines = capsys.readouterr().out.strip().splitlines()
        assert len(lines) == 2

    def test_line1_is_timestamp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Line 1 matches ``YYYY-MM-DDTHH-MM-SSZ`` UTC timestamp pattern."""
        monkeypatch.chdir(tmp_path)
        setup_worktree.main([])
        ts = capsys.readouterr().out.strip().splitlines()[0]
        assert TIMESTAMP_RE.match(ts)

    def test_line2_is_run_dir_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Line 2 is ``.temp/develop/<ts>`` matching the timestamp on line 1."""
        monkeypatch.chdir(tmp_path)
        setup_worktree.main([])
        ts, run_dir = capsys.readouterr().out.strip().splitlines()
        assert run_dir == f".temp/develop/{ts}"
        assert (tmp_path / run_dir).is_dir()

    def test_output_has_no_crlf(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Stdout must not contain CRLF (Windows text-mode regression guard)."""
        monkeypatch.chdir(tmp_path)
        setup_worktree.main([])
        out = capsys.readouterr().out
        assert "\r" not in out

    def test_retired_sentinel_option_is_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A caller still passing ``--sentinel NAME`` gets the two-line contract, exit 0 and no temp-dir file.

        The option was retired with the liveness sentinel it created; failing instead of ignoring it would break a stale
        caller that the always-exit-0 contract exists to protect.
        """
        sentinel_dir = tmp_path / "sentinels"
        sentinel_dir.mkdir()
        monkeypatch.setenv("TMPDIR", str(sentinel_dir))
        monkeypatch.chdir(tmp_path)

        rc = setup_worktree.main(["--sentinel", "swt-quiet"])

        assert rc == 0
        assert len(capsys.readouterr().out.strip().splitlines()) == 2
        assert list(sentinel_dir.iterdir()) == []


class TestArgparse:
    """Argparse-layer contract: ``--help`` exits 0; golden invocations preserve behaviour."""

    def test_help_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Print usage and exit 0 without creating a run dir."""
        with pytest.raises(SystemExit) as exc:
            setup_worktree.main(["--help"])
        assert exc.value.code == 0
        assert "setup_worktree.py" in capsys.readouterr().out

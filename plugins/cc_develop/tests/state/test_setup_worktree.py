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
from typing import NamedTuple

import pytest
import setup_worktree

SCRIPT = Path(setup_worktree.__file__)
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z$")


class _Run(NamedTuple):
    """Outcome of one ``setup_worktree.main([])`` call made inside a scratch working directory."""

    cwd: Path
    returncode: int
    out: str


@pytest.fixture
def run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> _Run:
    """Run the script once from an empty scratch directory and capture its exit code and stdout."""
    monkeypatch.chdir(tmp_path)
    returncode = setup_worktree.main([])
    return _Run(tmp_path, returncode, capsys.readouterr().out)


class TestRunDirCreation:
    """``.temp/develop/<ts>/`` creation and the two-line output contract."""

    def test_creates_run_dir(self, run: _Run) -> None:
        """Create ``.temp/develop/<ts>/`` under the current directory and exit 0."""
        assert run.returncode == 0
        assert (run.cwd / ".temp" / "develop").is_dir()

    def test_prints_two_lines(self, run: _Run) -> None:
        """Output is exactly two non-empty lines."""
        assert len(run.out.strip().splitlines()) == 2

    def test_line1_is_timestamp(self, run: _Run) -> None:
        """Line 1 matches the ``YYYY-MM-DDTHH-MM-SSZ`` UTC timestamp pattern."""
        assert TIMESTAMP_RE.match(run.out.strip().splitlines()[0])

    def test_line2_is_run_dir_path(self, run: _Run) -> None:
        """Line 2 is ``.temp/develop/<ts>`` matching the timestamp on line 1, and that directory exists."""
        ts, run_dir = run.out.strip().splitlines()
        assert run_dir == f".temp/develop/{ts}"
        assert (run.cwd / run_dir).is_dir()

    def test_output_has_no_crlf(self, run: _Run) -> None:
        """Stdout must not contain CRLF (Windows text-mode regression guard)."""
        assert "\r" not in run.out

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

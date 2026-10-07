"""Tests for ``bin/find_run_id.py`` — locate the latest completed run id.

Covers:
* No program filter: latest completed run wins by mtime
* Program filter: only matching ``program_file`` is returned
* No completed run anywhere → ``None`` / exit 1
* Program filter with no matching completed run → ``None`` / exit 1
* CLI exit codes: 0 on found, 1 on not-found, 2 on argument error
"""

from __future__ import annotations

import importlib.util
import json
import os
import time
from pathlib import Path

import pytest

# Load via explicit path to avoid sys.path collisions with other plugins.
_SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "find_run_id.py"
_spec = importlib.util.spec_from_file_location("research_find_run_id", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

find_run_id = _mod.find_run_id
main = _mod.main


def _make_run(
    parent: Path, run_id: str, status: str, program_file: str | None = None, mtime: float | None = None
) -> Path:
    """Create a fake ``<parent>/<run_id>/state.json`` and return the run dir.

    Args:
        parent: State-dir base.
        run_id: Subdirectory name.
        status: Value for the ``status`` field.
        program_file: Optional ``program_file`` value (omitted when ``None``).
        mtime: Optional explicit mtime applied to the run directory.

    Returns:
        Path to the created run directory.

    Examples:
        >>> run_dir = _make_run(getfixture("tmp_path"), "demo", "completed")
        >>> json.loads((run_dir / "state.json").read_text())["status"]
        'completed'
    """
    run_dir = parent / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {"status": status}
    if program_file is not None:
        payload["program_file"] = program_file
    (run_dir / "state.json").write_text(json.dumps(payload), encoding="utf-8")
    if mtime is not None:
        os.utime(run_dir, (mtime, mtime))
    return run_dir


class TestFindRunIdNoFilter:
    """Verify run discovery without a program filter."""

    def test_returns_latest_completed(self, tmp_path: Path) -> None:
        """Latest completed run wins by mtime even when older runs also completed."""
        now = time.time()
        _make_run(tmp_path, "old-run", "completed", mtime=now - 100)
        _make_run(tmp_path, "new-run", "completed", mtime=now)
        assert find_run_id(tmp_path) == "new-run"

    def test_skips_incomplete_runs(self, tmp_path: Path) -> None:
        """Runs without a terminal status are skipped even when newest."""
        now = time.time()
        _make_run(tmp_path, "old-completed", "completed", mtime=now - 100)
        _make_run(tmp_path, "newer-running", "running", mtime=now)
        assert find_run_id(tmp_path) == "old-completed"

    def test_accepts_goal_achieved_status(self, tmp_path: Path) -> None:
        """Treat a goal-achieved run as completed."""
        _make_run(tmp_path, "winner", "goal-achieved")
        assert find_run_id(tmp_path) == "winner"

    def test_returns_none_when_no_completed_run(self, tmp_path: Path) -> None:
        """Empty result when no run has a terminal status."""
        _make_run(tmp_path, "r1", "running")
        _make_run(tmp_path, "r2", "failed")
        assert find_run_id(tmp_path) is None

    def test_returns_none_for_empty_directory(self, tmp_path: Path) -> None:
        """Empty state dir yields ``None``."""
        assert find_run_id(tmp_path) is None

    def test_returns_none_for_missing_directory(self, tmp_path: Path) -> None:
        """Non-existent state dir yields ``None`` (no exception)."""
        assert find_run_id(tmp_path / "does-not-exist") is None

    def test_ignores_malformed_state_json(self, tmp_path: Path) -> None:
        """Runs with unreadable / malformed ``state.json`` are silently skipped."""
        now = time.time()
        bad = tmp_path / "bad-run"
        bad.mkdir()
        (bad / "state.json").write_text("{not json", encoding="utf-8")
        os.utime(bad, (now, now))
        _make_run(tmp_path, "good-run", "completed", mtime=now - 50)
        assert find_run_id(tmp_path) == "good-run"

    def test_safe_mtime_returns_neg_inf_on_missing_dir(self, tmp_path: Path) -> None:
        """The sort-key helper degrades a vanished dir to -inf instead of raising."""
        assert _mod._safe_mtime(tmp_path / "gone") == float("-inf")

    def test_dir_vanishing_during_sort_does_not_crash(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A dir removed after is_dir() but before the sort's stat must not raise.

        Faithfully models the race: is_dir() during enumeration succeeds (dir present),
        then the sort key's stat raises FileNotFoundError for the vanished dir.
        """
        _make_run(tmp_path, "survivor", "completed")
        vanished = _make_run(tmp_path, "vanished", "completed")
        real_stat = Path.stat
        real_is_dir = Path.is_dir

        def _flaky_stat(self: Path, *args: object, **kwargs: object) -> os.stat_result:
            """Expose the vanished directory only during the later sort-key stat."""
            if self == vanished:
                raise FileNotFoundError(self)
            return real_stat(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "is_dir", lambda self: True if self == vanished else real_is_dir(self))
        monkeypatch.setattr(Path, "stat", _flaky_stat)
        # No exception; the surviving completed run is still resolved.
        assert find_run_id(tmp_path) == "survivor"


class TestFindRunIdProgramFilter:
    """Filter completed runs by program identity."""

    def test_returns_run_matching_program(self, tmp_path: Path) -> None:
        """Return the latest completed run whose program_file matches."""
        now = time.time()
        _make_run(tmp_path, "older-other", "completed", program_file="other.md", mtime=now - 50)
        _make_run(tmp_path, "match", "completed", program_file="target.md", mtime=now - 25)
        _make_run(tmp_path, "newer-other", "completed", program_file="other.md", mtime=now)
        assert find_run_id(tmp_path, match_program="target.md") == "match"

    def test_returns_none_when_program_does_not_match(self, tmp_path: Path) -> None:
        """No completed run with matching program_file → ``None``."""
        _make_run(tmp_path, "r1", "completed", program_file="a.md")
        _make_run(tmp_path, "r2", "completed", program_file="b.md")
        assert find_run_id(tmp_path, match_program="c.md") is None

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            pytest.param("running", None, id="non-terminal-status-skipped"),
            pytest.param("goal-achieved", "r1", id="goal-achieved-matches"),
        ],
    )
    def test_program_filter_honours_terminal_status(self, tmp_path: Path, status: str, expected: str | None) -> None:
        """A run matching the program is returned only with a terminal status.

        A matching program_file with a non-terminal status is skipped; a completed goal (``goal-achieved``) is accepted.
        """
        _make_run(tmp_path, "r1", status, program_file="target.md")
        assert find_run_id(tmp_path, match_program="target.md") == expected


class TestMain:
    """CLI entry point — exit codes and stdout."""

    def test_exit_zero_on_found(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Match → exit 0; basename printed on stdout."""
        _make_run(tmp_path, "winner", "completed")
        rc = main([str(tmp_path)])
        assert rc == 0
        assert capsys.readouterr().out.strip() == "winner"

    def test_exit_one_on_not_found(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """No match → exit 1; no stdout output."""
        rc = main([str(tmp_path)])
        assert rc == 1
        assert capsys.readouterr().out == ""

    def test_exit_two_on_missing_arg(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Argparse rejects missing positional with exit 2."""
        rc = main([])
        assert rc == 2

    def test_program_filter_via_cli(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Verify command-line option behavior.

        ``--match-program`` filters by program_file.
        """
        _make_run(tmp_path, "wrong", "completed", program_file="other.md")
        _make_run(tmp_path, "right", "completed", program_file="target.md")
        rc = main([str(tmp_path), "--match-program", "target.md"])
        assert rc == 0
        assert capsys.readouterr().out.strip() == "right"

    def test_output_has_no_crlf(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Stdout must use LF only (Windows text-mode regression guard)."""
        _make_run(tmp_path, "winner", "completed")
        main([str(tmp_path)])
        assert "\r" not in capsys.readouterr().out

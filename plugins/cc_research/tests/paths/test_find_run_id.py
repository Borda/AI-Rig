"""Tests for ``bin/find_run_id.py`` — locate the latest completed run id.

Covers:
* No program filter: latest completed run wins by mtime
* Program filter: only matching ``program_file`` is returned
* No completed run anywhere → ``None`` / exit 1
* Program filter with no matching completed run → ``None`` / exit 1
* CLI exit codes: 0 on found, 1 on not-found, 2 on argument error

Run recency is set with fixed mtimes, never the wall clock, so ordering is deterministic.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from _research_text import load_module

# Load via explicit path to avoid sys.path collisions with other plugins.
_mod = load_module(Path(__file__).resolve().parents[2] / "bin" / "find_run_id.py", "research_find_run_id")

find_run_id = _mod.find_run_id
main = _mod.main

_NOW = 1_700_000_000.0


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


def _make_runs(parent: Path, statuses: dict[str, str]) -> None:
    """Create one run directory per ``run_id: status`` entry under ``parent``."""
    for run_id, status in statuses.items():
        _make_run(parent, run_id, status)


class TestFindRunIdNoFilter:
    """Verify run discovery without a program filter."""

    def test_latest_completed_wins(self, tmp_path: Path) -> None:
        """Latest completed run wins by mtime even when older runs also completed."""
        _make_run(tmp_path, "old-run", "completed", mtime=_NOW - 100)
        _make_run(tmp_path, "new-run", "completed", mtime=_NOW)

        assert find_run_id(tmp_path) == "new-run"

    def test_skips_incomplete_runs(self, tmp_path: Path) -> None:
        """Runs without a terminal status are skipped even when newest.

        A run still in progress must never be resumed as if it had finished.
        """
        _make_run(tmp_path, "old-completed", "completed", mtime=_NOW - 100)
        _make_run(tmp_path, "newer-running", "running", mtime=_NOW)

        assert find_run_id(tmp_path) == "old-completed"

    def test_accepts_goal_achieved_status(self, tmp_path: Path) -> None:
        """A goal-achieved run counts as completed."""
        _make_run(tmp_path, "winner", "goal-achieved")

        assert find_run_id(tmp_path) == "winner"

    @pytest.mark.parametrize(
        "statuses",
        [
            pytest.param({"r1": "running", "r2": "failed"}, id="no-terminal-status"),
            pytest.param({}, id="empty-directory"),
        ],
    )
    def test_none_without_completed_run(self, tmp_path: Path, statuses: dict[str, str]) -> None:
        """No run with a terminal status — or no run at all — yields ``None``."""
        _make_runs(tmp_path, statuses)

        assert find_run_id(tmp_path) is None

    def test_none_for_missing_directory(self, tmp_path: Path) -> None:
        """Non-existent state dir yields ``None`` (no exception)."""
        assert find_run_id(tmp_path / "does-not-exist") is None

    def test_ignores_malformed_state_json(self, tmp_path: Path) -> None:
        """Runs with unreadable / malformed ``state.json`` are silently skipped, even when newest."""
        bad = tmp_path / "bad-run"
        bad.mkdir()
        (bad / "state.json").write_text("{not json", encoding="utf-8")
        os.utime(bad, (_NOW, _NOW))
        _make_run(tmp_path, "good-run", "completed", mtime=_NOW - 50)

        assert find_run_id(tmp_path) == "good-run"

    def test_safe_mtime_is_neg_inf_for_missing_dir(self, tmp_path: Path) -> None:
        """The sort-key helper degrades a vanished dir to -inf instead of raising."""
        assert _mod._safe_mtime(tmp_path / "gone") == float("-inf")

    def test_dir_vanishing_during_sort_does_not_crash(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A dir removed after is_dir() but before the sort's stat must not raise.

        Faithfully models the race: is_dir() during enumeration succeeds (dir present), then the sort key's stat raises
        FileNotFoundError for the vanished dir. The surviving completed run is still resolved.
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

        assert find_run_id(tmp_path) == "survivor"


class TestFindRunIdProgramFilter:
    """Filter completed runs by program identity."""

    def test_latest_run_matching_program(self, tmp_path: Path) -> None:
        """Return the latest completed run whose program_file matches, ignoring newer runs of other programs."""
        _make_run(tmp_path, "older-other", "completed", program_file="other.md", mtime=_NOW - 50)
        _make_run(tmp_path, "match", "completed", program_file="target.md", mtime=_NOW - 25)
        _make_run(tmp_path, "newer-other", "completed", program_file="other.md", mtime=_NOW)

        assert find_run_id(tmp_path, match_program="target.md") == "match"

    def test_none_when_program_does_not_match(self, tmp_path: Path) -> None:
        """No completed run with matching program_file → ``None``."""
        _make_run(tmp_path, "r1", "completed", program_file="a.md")
        _make_run(tmp_path, "r2", "completed", program_file="b.md")

        assert find_run_id(tmp_path, match_program="c.md") is None

    def test_relative_argument_matches_absolute_record(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A relative ``program.md`` argument matches the absolute ``program_file`` that /research:run records.

        /research:fortify passes the path as typed while state.json stores ``realpath``; an exact string compare never
        matched, so fortify of ``program.md`` reported no completed run.
        """
        state_dir, proj = tmp_path / "state", tmp_path / "proj"
        proj.mkdir()
        (proj / "program.md").write_text("x", encoding="utf-8")
        _make_run(state_dir, "run", "completed", program_file=os.path.realpath(proj / "program.md"))
        monkeypatch.chdir(proj)

        assert find_run_id(state_dir, match_program="program.md") == "run"

    def test_text_goal_run_never_matches_program(self, tmp_path: Path) -> None:
        """A text-goal run (``program_file: null``) is never matched by a program filter.

        Guards the normalisation step: ``realpath`` must not be applied to a JSON null.
        """
        (tmp_path / "run").mkdir()
        (tmp_path / "run" / "state.json").write_text('{"status": "completed", "program_file": null}', encoding="utf-8")

        assert find_run_id(tmp_path, match_program="program.md") is None

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            pytest.param("running", None, id="non-terminal-status-skipped"),
            pytest.param("goal-achieved", "r1", id="goal-achieved-matches"),
        ],
    )
    def test_honours_terminal_status(self, tmp_path: Path, status: str, expected: str | None) -> None:
        """A run matching the program is returned only with a terminal status.

        A matching program_file with a non-terminal status is skipped; a completed goal (``goal-achieved``) is accepted.
        """
        _make_run(tmp_path, "r1", status, program_file="target.md")

        assert find_run_id(tmp_path, match_program="target.md") == expected


class TestMain:
    """CLI entry point — exit codes and stdout."""

    def test_found_exits_zero_and_prints_run_id(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Match → exit 0; the run id (directory basename) is printed on stdout."""
        _make_run(tmp_path, "winner", "completed")

        assert main([str(tmp_path)]) == 0
        assert capsys.readouterr().out.strip() == "winner"

    def test_not_found_exits_one_without_output(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """No match → exit 1; nothing on stdout, so callers capturing it get an empty value."""
        assert main([str(tmp_path)]) == 1
        assert capsys.readouterr().out == ""

    def test_missing_argument_exits_two(self) -> None:
        """Argparse rejects a missing state-dir positional with exit 2."""
        assert main([]) == 2

    def test_program_filter_via_cli(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """``--match-program`` filters runs by their recorded ``program_file``."""
        _make_run(tmp_path, "wrong", "completed", program_file="other.md")
        _make_run(tmp_path, "right", "completed", program_file="target.md")

        assert main([str(tmp_path), "--match-program", "target.md"]) == 0
        assert capsys.readouterr().out.strip() == "right"

    def test_output_has_no_crlf(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Stdout must use LF only (Windows text-mode regression guard)."""
        _make_run(tmp_path, "winner", "completed")

        main([str(tmp_path)])

        assert "\r" not in capsys.readouterr().out

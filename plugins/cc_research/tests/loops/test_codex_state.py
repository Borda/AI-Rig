"""Tests for ``bin/codex_state.py`` — Codex co-pilot counters for research:run."""

from __future__ import annotations

import json
from pathlib import Path

import codex_state
import pytest

_ZERO_LINE = "CODEX_ITER=0 CODEX_NOOP_STREAK=0 CODEX_DISABLED=false"


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Path of a ``codex-state`` file under a run dir that does not exist yet, with cwd at ``tmp_path``."""
    monkeypatch.chdir(tmp_path)
    return Path(".experiments/state/run-1/codex-state")


def _bump(state: Path, outcome: str, capsys: pytest.CaptureFixture[str]) -> str:
    """Run one successful ``bump`` and return the printed counter line."""
    assert codex_state.main(["bump", "--state", str(state), "--outcome", outcome]) == 0
    return capsys.readouterr().out.strip()


class TestRead:
    """``read`` prints the counters as ``KEY=value`` pairs the skill shell can ``eval``."""

    def test_missing_file_reads_as_zero(self, state: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A run with no state file starts a fresh budget, silently.

        The first read of a new run has nothing to load; it must print zeros without any warning on stderr.
        """
        assert codex_state.main(["read", "--state", str(state)]) == 0

        captured = capsys.readouterr()
        assert captured.out.strip() == _ZERO_LINE
        assert captured.err == ""

    def test_reports_disabled_at_streak_two(self, state: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """``CODEX_DISABLED`` is derived from the no-op streak on read.

        A stored streak of two crosses the threshold, so the printed line says disabled although the file holds only the
        two counters.
        """
        state.parent.mkdir(parents=True)
        state.write_text(json.dumps({"codex_iter": 5, "noop_streak": 2}), encoding="utf-8")

        assert codex_state.main(["read", "--state", str(state)]) == 0
        assert capsys.readouterr().out.strip() == "CODEX_ITER=5 CODEX_NOOP_STREAK=2 CODEX_DISABLED=true"

    @pytest.mark.parametrize("content", ["not json", "[1, 2]", '{"codex_iter": "x"}'])
    def test_malformed_file_reads_as_zero_with_warning(
        self, state: Path, capsys: pytest.CaptureFixture[str], content: str
    ) -> None:
        """Unusable content resets to 0/0 (as the former ``|| echo 0`` did) but now says so on stderr.

        Invalid JSON, a non-object value and a non-integer counter are all unusable; the run continues on a fresh budget
        and the warning keeps the reset visible.
        """
        state.parent.mkdir(parents=True)
        state.write_text(content, encoding="utf-8")

        assert codex_state.main(["read", "--state", str(state)]) == 0

        captured = capsys.readouterr()
        assert captured.out.strip() == _ZERO_LINE
        assert "unusable" in captured.err


class TestBump:
    """``bump`` records one co-pilot pass and its outcome."""

    def test_noop_then_productive_sequence(self, state: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """No-ops grow the streak, a productive pass resets it, and the file keeps the printf-compatible bytes.

        Two consecutive no-ops disable the co-pilot; the following productive pass clears the streak and re-enables it.
        """
        lines = [_bump(state, outcome, capsys) for outcome in ("noop", "noop", "productive")]

        assert lines == [
            "CODEX_ITER=1 CODEX_NOOP_STREAK=1 CODEX_DISABLED=false",
            "CODEX_ITER=2 CODEX_NOOP_STREAK=2 CODEX_DISABLED=true",
            "CODEX_ITER=3 CODEX_NOOP_STREAK=0 CODEX_DISABLED=false",
        ]
        assert state.read_text(encoding="utf-8") == '{"codex_iter": 3, "noop_streak": 0}\n'

    def test_state_outside_cwd_and_tmp_is_rejected(self, state: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """The write target is confined to the project and the session temp dir.

        A state path at the filesystem root is refused with exit 2 and nothing is written there.
        """
        outside = Path(Path.home().anchor) / "codex-state-outside"

        assert codex_state.main(["bump", "--state", str(outside), "--outcome", "noop"]) == 2
        assert "outside the working directory" in capsys.readouterr().err
        assert not outside.exists()

    def test_bad_outcome_is_argument_error(self, state: Path) -> None:
        """Only ``noop`` and ``productive`` are outcomes; anything else exits 2."""
        assert codex_state.main(["bump", "--state", str(state), "--outcome", "maybe"]) == 2

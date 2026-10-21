"""Tests for ``bin/codex_state.py`` — Codex co-pilot counters for research:run."""

from __future__ import annotations

import json
from pathlib import Path

import codex_state
import pytest


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Path of a ``codex-state`` file under a run dir that does not exist yet, with cwd at ``tmp_path``."""
    monkeypatch.chdir(tmp_path)
    return Path(".experiments/state/run-1/codex-state")


def test_missing_file_reads_as_zero(state: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A run with no state file starts a fresh budget, silently."""
    assert codex_state.main(["read", "--state", str(state)]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "CODEX_ITER=0 CODEX_NOOP_STREAK=0 CODEX_DISABLED=false"
    assert captured.err == ""


def test_noop_then_productive_sequence(state: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """No-ops grow the streak, a productive pass resets it, and the file keeps the printf-compatible bytes."""
    lines = []
    for outcome in ("noop", "noop", "productive"):
        assert codex_state.main(["bump", "--state", str(state), "--outcome", outcome]) == 0
        lines.append(capsys.readouterr().out.strip())
    assert lines == [
        "CODEX_ITER=1 CODEX_NOOP_STREAK=1 CODEX_DISABLED=false",
        "CODEX_ITER=2 CODEX_NOOP_STREAK=2 CODEX_DISABLED=true",
        "CODEX_ITER=3 CODEX_NOOP_STREAK=0 CODEX_DISABLED=false",
    ]
    assert state.read_text(encoding="utf-8") == '{"codex_iter": 3, "noop_streak": 0}\n'


def test_read_reports_disabled_at_streak_two(state: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``CODEX_DISABLED`` is derived from the streak on read."""
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"codex_iter": 5, "noop_streak": 2}), encoding="utf-8")
    assert codex_state.main(["read", "--state", str(state)]) == 0
    assert capsys.readouterr().out.strip() == "CODEX_ITER=5 CODEX_NOOP_STREAK=2 CODEX_DISABLED=true"


@pytest.mark.parametrize("content", ["not json", "[1, 2]", '{"codex_iter": "x"}'])
def test_malformed_file_reads_as_zero_with_warning(
    state: Path, capsys: pytest.CaptureFixture[str], content: str
) -> None:
    """Unusable content resets to 0/0 (as the former ``|| echo 0`` did) but now says so on stderr."""
    state.parent.mkdir(parents=True)
    state.write_text(content, encoding="utf-8")
    assert codex_state.main(["read", "--state", str(state)]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "CODEX_ITER=0 CODEX_NOOP_STREAK=0 CODEX_DISABLED=false"
    assert "unusable" in captured.err


def test_state_outside_cwd_and_tmp_is_rejected(state: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The write target is confined to the project and the session temp dir."""
    outside = Path(Path.home().anchor) / "codex-state-outside"
    assert codex_state.main(["bump", "--state", str(outside), "--outcome", "noop"]) == 2
    assert "outside the working directory" in capsys.readouterr().err
    assert not outside.exists()


def test_bad_outcome_is_an_argument_error(state: Path) -> None:
    """Only ``noop`` and ``productive`` are outcomes."""
    assert codex_state.main(["bump", "--state", str(state), "--outcome", "maybe"]) == 2

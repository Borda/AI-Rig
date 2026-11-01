"""Tests for ``dev_batch_gate.py`` — the batch-mode gate ``/develop:feature`` and ``/develop:refactor`` run.

Covers:
* Batch mode is on by default and records the calling skill in ``dev-batch-skill``
* ``--no-batch`` (a ``false`` in ``dev-<skill>-no-batch``) turns it off and writes nothing — a no-batch run never leaves
  the sentinel behind for ``batch-mode.md``'s fences
* One skill's opt-out never leaks into the other
* The exit code is what the skill's ``if ...; then cat batch-mode.md; fi`` condition depends on; the ``if`` keeps the
  call itself at exit 0 on the opt-out path
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import dev_batch_gate
import pytest
from dev_run_dir import read_sentinel, write_sentinel

_SCRIPT = Path(__file__).resolve().parents[2] / "bin" / "dev_batch_gate.py"
_SKILLS = ["feature", "refactor"]


@pytest.fixture(autouse=True)
def _isolate_sentinels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point ``$TMPDIR`` at an empty directory and fix the session token, so only this test's sentinels exist."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "gate-test")


def _gate_exit_status(tmp_path: Path) -> int:
    """Run ``dev_batch_gate.py feature`` as the skills do, in a minimal environment, and return its exit status."""
    env = {"TMPDIR": str(tmp_path), "CSID": "gate-test", "PATH": "", "PYTHONUTF8": "1"}
    env.update(
        (key, os.environ[key])
        for key in ("SystemRoot", "SYSTEMROOT", "COMSPEC", "PATHEXT", "TEMP", "TMP")
        if key in os.environ
    )
    return subprocess.run(
        [sys.executable, str(_SCRIPT), "feature"], env=env, capture_output=True, check=False
    ).returncode


class TestBatchOn:
    """Batch mode is on unless the skill's own opt-out sentinel says ``false``."""

    @pytest.mark.parametrize("skill", _SKILLS)
    def test_on_by_default_and_records_skill(self, skill: str, capsys: pytest.CaptureFixture[str]) -> None:
        """With no ``--no-batch`` sentinel the gate exits 0, prints the verdict and writes ``dev-batch-skill``.

        Batch mode ships default-on, so a fresh session — where ``dev_parse_args.py`` wrote nothing for this flag — must
        load ``batch-mode.md``; the sentinel it reads back names the skill whose size counter it feeds.
        """
        assert dev_batch_gate.main([skill]) == 0

        assert capsys.readouterr().out == f"batch mode: on ({skill})\n"
        assert read_sentinel("dev-batch-skill") == skill

    def test_true_in_opt_out_sentinel_is_still_on(self) -> None:
        """Only a literal ``false`` disables batch mode, matching the shell test the skills used to carry."""
        write_sentinel("dev-feature-no-batch", "true")

        assert dev_batch_gate.main(["feature"]) == 0
        assert read_sentinel("dev-batch-skill") == "feature"


class TestBatchOff:
    """``--no-batch`` turns batch mode off for that skill only and leaves no sentinel behind."""

    @pytest.mark.parametrize("skill", _SKILLS)
    def test_no_batch_exits_one_and_writes_nothing(self, skill: str, capsys: pytest.CaptureFixture[str]) -> None:
        """``--no-batch`` prints the off verdict, exits 1 and leaves ``dev-batch-skill`` unwritten.

        A stale sentinel from a batch-on run would make a later ``batch-mode.md`` fence believe it was invoked by the
        wrong skill; staying unwritten is the contract the original N9 fix established.
        """
        write_sentinel(f"dev-{skill}-no-batch", "false")

        assert dev_batch_gate.main([skill]) == 1

        assert capsys.readouterr().out == "batch mode: off (--no-batch)\n"
        assert read_sentinel("dev-batch-skill") == ""

    def test_opt_out_of_one_skill_does_not_disable_the_other(self) -> None:
        """Each skill reads only its own ``dev-<skill>-no-batch`` sentinel."""
        write_sentinel("dev-feature-no-batch", "false")

        assert dev_batch_gate.main(["refactor"]) == 0
        assert read_sentinel("dev-batch-skill") == "refactor"


def test_unknown_skill_is_rejected() -> None:
    """A skill outside the batch-mode set exits 2 instead of writing a sentinel for it."""
    with pytest.raises(SystemExit) as exit_info:
        dev_batch_gate.main(["debug"])

    assert exit_info.value.code == 2
    assert read_sentinel("dev-batch-skill") == ""


class TestExitStatusAcrossProcessBoundary:
    """The skills run ``if dev_batch_gate.py feature; then cat batch-mode.md; fi`` — a real process boundary."""

    def test_exit_status_zero_when_batch_mode_on(self, tmp_path: Path) -> None:
        """With no opt-out sentinel the script exits 0, so the skill's ``if`` loads ``batch-mode.md``."""
        assert _gate_exit_status(tmp_path) == 0

    def test_exit_status_one_when_no_batch_set(self, tmp_path: Path) -> None:
        """With ``--no-batch`` recorded the script exits 1, so the skill's ``if`` skips ``batch-mode.md``.

        The sentinel is written the way ``dev_parse_args.py`` writes it, before the child process starts.
        """
        write_sentinel("dev-feature-no-batch", "false")

        assert _gate_exit_status(tmp_path) == 1

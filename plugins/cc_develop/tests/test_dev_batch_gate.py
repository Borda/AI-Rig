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

_SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "dev_batch_gate.py"


@pytest.fixture(autouse=True)
def _isolate_sentinels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point ``$TMPDIR`` at an empty directory and fix the session token, so only this test's sentinels exist."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "gate-test")


class TestBatchOn:
    @pytest.mark.parametrize("skill", [pytest.param("feature", id="feature"), pytest.param("refactor", id="refactor")])
    def test_missing_opt_out_means_on_and_records_the_skill(
        self, skill: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """With no ``--no-batch`` sentinel the gate exits 0, prints the verdict and writes ``dev-batch-skill``.

        Batch mode ships default-on, so a fresh session — where ``dev_parse_args.py`` wrote nothing for this flag — must
        load ``batch-mode.md``; the sentinel it reads back names the skill whose size counter it feeds.
        """
        assert dev_batch_gate.main([skill]) == 0

        assert capsys.readouterr().out == f"batch mode: on ({skill})\n"
        assert read_sentinel("dev-batch-skill") == skill

    def test_true_in_the_opt_out_sentinel_is_still_on(self) -> None:
        """Only a literal ``false`` disables batch mode, matching the shell test the skills used to carry."""
        write_sentinel("dev-feature-no-batch", "true")

        assert dev_batch_gate.main(["feature"]) == 0
        assert read_sentinel("dev-batch-skill") == "feature"


class TestBatchOff:
    @pytest.mark.parametrize("skill", [pytest.param("feature", id="feature"), pytest.param("refactor", id="refactor")])
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


def test_script_exit_status_drives_the_load_condition(tmp_path: Path) -> None:
    """Run as the skills do, the exit status is 0 when batch mode is on and 1 when ``--no-batch`` is set.

    The skill runs ``if dev_batch_gate.py feature; then cat batch-mode.md; fi``; this is the real process boundary that
    condition crosses, with the sentinel written the way ``dev_parse_args.py`` writes it.
    """
    env = {"TMPDIR": str(tmp_path), "CSID": "gate-test", "PATH": "", "PYTHONUTF8": "1"}
    env.update(
        (key, os.environ[key])
        for key in ("SystemRoot", "SYSTEMROOT", "COMSPEC", "PATHEXT", "TEMP", "TMP")
        if key in os.environ
    )

    on = subprocess.run([sys.executable, str(_SCRIPT), "feature"], env=env, capture_output=True, text=True, check=False)
    write_sentinel("dev-feature-no-batch", "false")
    off = subprocess.run(
        [sys.executable, str(_SCRIPT), "feature"], env=env, capture_output=True, text=True, check=False
    )

    assert (on.returncode, off.returncode) == (0, 1)

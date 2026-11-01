"""Tests for ``bin/plan_hash.py`` — detect a plan file edited during a develop build.

Covers:
* unchanged plan prints nothing
* edited or deleted plan prints the ``! BLOCKED`` line naming the path
* ``record`` with an empty path clears a stale sentinel
* ``check`` without a sentinel is silent and exits 0
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import plan_hash
import pytest


@pytest.fixture(autouse=True)
def _session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the sentinel at a private TMPDIR and session id."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "t")


@pytest.fixture
def recorded_plan(tmp_path: Path) -> Path:
    """Write ``plan.md`` containing ``steps`` and record its hash, as a build start would."""
    plan = tmp_path / "plan.md"
    plan.write_text("steps\n")
    plan_hash.main(["record", str(plan)])
    return plan


class TestPlanHash:
    """``record`` stores the plan's hash; ``check`` reports a plan that changed since."""

    def test_unchanged_plan_is_silent(self, recorded_plan: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Re-hashing an untouched plan prints nothing.

        The check runs after every build step, so noise on the unchanged path would bury the one real warning.
        """
        plan_hash.main(["check"])
        assert capsys.readouterr().out == ""

    @pytest.mark.parametrize(
        "mutation",
        [
            pytest.param(lambda plan: plan.write_text("rewritten to match the build\n"), id="edited"),
            pytest.param(lambda plan: plan.unlink(), id="deleted"),
        ],
    )
    def test_changed_plan_is_blocked(
        self, recorded_plan: Path, capsys: pytest.CaptureFixture[str], mutation: Callable[[Path], object]
    ) -> None:
        """An edited or deleted plan yields the BLOCKED line with its path.

        A plan rewritten to match what was built destroys its value as a verification target; deleting it does too.
        """
        mutation(recorded_plan)
        rc = plan_hash.main(["check"])
        assert (rc, capsys.readouterr().out) == (0, f"! BLOCKED — plan changed during build: {recorded_plan}\n")

    def test_empty_record_clears_stale_sentinel(self, recorded_plan: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A run without --plan must not inherit an earlier run's hash.

        Recording an empty path after a real one drops the sentinel, so a later edit of the old plan stays unreported.
        """
        plan_hash.main(["record", ""])
        recorded_plan.write_text("changed\n")
        plan_hash.main(["check"])
        assert capsys.readouterr().out == ""

    def test_check_without_sentinel_is_silent(self, capsys: pytest.CaptureFixture[str]) -> None:
        """No recorded plan means nothing to compare.

        Runs started without ``--plan`` still call ``check``; it must exit 0 without output.
        """
        rc = plan_hash.main(["check"])
        assert (rc, capsys.readouterr().out) == (0, "")

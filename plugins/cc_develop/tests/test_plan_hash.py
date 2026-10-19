"""Tests for ``bin/plan_hash.py`` — detect a plan file edited during a develop build.

Covers:
* unchanged plan prints nothing
* edited or deleted plan prints the ``! BLOCKED`` line naming the path
* ``record`` with an empty path clears a stale sentinel
* ``check`` without a sentinel is silent and exits 0
"""

from __future__ import annotations

from pathlib import Path

import plan_hash
import pytest


@pytest.fixture(autouse=True)
def _session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the sentinel at a private TMPDIR and session id."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "t")


def test_unchanged_plan_is_silent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Re-hashing an untouched plan prints nothing."""
    plan = tmp_path / "plan.md"
    plan.write_text("steps\n")
    plan_hash.main(["record", str(plan)])
    plan_hash.main(["check"])
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("mutation", ["edit", "delete"])
def test_changed_plan_is_blocked(mutation: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """An edited or deleted plan yields the BLOCKED line with its path."""
    plan = tmp_path / "plan.md"
    plan.write_text("steps\n")
    plan_hash.main(["record", str(plan)])
    if mutation == "edit":
        plan.write_text("rewritten to match the build\n")
    else:
        plan.unlink()
    assert plan_hash.main(["check"]) == 0
    assert capsys.readouterr().out == f"! BLOCKED — plan changed during build: {plan}\n"


def test_empty_record_clears_stale_sentinel(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A run without --plan must not inherit an earlier run's hash."""
    plan = tmp_path / "plan.md"
    plan.write_text("steps\n")
    plan_hash.main(["record", str(plan)])
    plan_hash.main(["record", ""])
    plan.write_text("changed\n")
    plan_hash.main(["check"])
    assert capsys.readouterr().out == ""


def test_check_without_sentinel_is_silent(capsys: pytest.CaptureFixture[str]) -> None:
    """No recorded plan means nothing to compare."""
    assert plan_hash.main(["check"]) == 0
    assert capsys.readouterr().out == ""

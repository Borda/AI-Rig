"""Verify that plan challenge reuse is bound to the reviewed bytes and verdict."""

from __future__ import annotations

import hashlib
from pathlib import Path

import plan_hash
import pytest


@pytest.fixture
def plan(tmp_path: Path) -> Path:
    """Create the exact plan revision a reviewer receives."""
    path = tmp_path / "plan.md"
    path.write_bytes(b"# Plan\n\n## Implementation\nChange the parser.\n")
    return path


def test_passed_challenge_reuses_exact_plan(plan: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The stored header does not change its own digest or prevent valid reuse."""
    digest = hashlib.sha256(plan.read_bytes()).hexdigest()
    assert plan_hash.main(["challenge-digest", str(plan)]) == 0
    assert capsys.readouterr().out == digest + "\n"
    assert plan_hash.main(["challenge-record", str(plan), "--status", "passed", "--sha256", digest]) == 0
    assert plan.read_text().startswith(f"Challenge: passed sha256={digest}\n")
    assert plan_hash.main(["challenge-check", str(plan)]) == 0


def test_plan_edit_invalidates_prior_challenge(plan: Path) -> None:
    """A changed implementation cannot inherit the earlier revision's approval."""
    digest = hashlib.sha256(plan.read_bytes()).hexdigest()
    assert plan_hash.main(["challenge-record", str(plan), "--status", "passed", "--sha256", digest]) == 0
    plan.write_bytes(plan.read_bytes().replace(b"parser", b"writer"))
    assert plan_hash.main(["challenge-check", str(plan)]) == 1


def test_record_refuses_revision_changed_during_review(plan: Path) -> None:
    """Recording the verdict after a concurrent plan edit must preserve the unapproved revision."""
    digest = hashlib.sha256(plan.read_bytes()).hexdigest()
    plan.write_bytes(b"# Revised plan\n")
    assert plan_hash.main(["challenge-record", str(plan), "--status", "passed", "--sha256", digest]) == 1
    assert plan.read_bytes() == b"# Revised plan\n"


@pytest.mark.parametrize("status", ["blocked", "skipped"])
def test_nonpassing_challenge_cannot_be_reused(plan: Path, status: str) -> None:
    """A digest match alone never upgrades a skipped or blocked review to approval."""
    digest = hashlib.sha256(plan.read_bytes()).hexdigest()
    assert plan_hash.main(["challenge-record", str(plan), "--status", status, "--sha256", digest]) == 0
    assert plan_hash.main(["challenge-check", str(plan)]) == 1


@pytest.mark.parametrize("header", ["", "Challenge: passed sha256=invalid\n", "Challenge: passed\n"])
def test_missing_or_malformed_challenge_cannot_be_reused(plan: Path, header: str) -> None:
    """Legacy prose or malformed metadata forces a new challenge."""
    plan.write_bytes(header.encode() + plan.read_bytes())
    assert plan_hash.main(["challenge-check", str(plan)]) == 1


def test_duplicate_challenge_headers_are_rejected(plan: Path) -> None:
    """Conflicting approval fields are not silently resolved by taking the first."""
    digest = hashlib.sha256(plan.read_bytes()).hexdigest()
    header = f"Challenge: passed sha256={digest}\n".encode()
    plan.write_bytes(header + header + plan.read_bytes())
    assert plan_hash.main(["challenge-check", str(plan)]) == 1


def test_missing_plan_cannot_be_challenged(tmp_path: Path) -> None:
    """An unreadable plan yields a failed check instead of a fabricated digest."""
    assert plan_hash.main(["challenge-check", str(tmp_path / "missing.md")]) == 1

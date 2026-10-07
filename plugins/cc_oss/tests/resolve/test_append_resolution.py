"""Tests for ``bin/append_resolution.py`` — resolve reporting outcomes back to the review directory.

Run records live under ``tmp_path``; the commit lookup is monkeypatched so no real git history is needed.
"""

from __future__ import annotations

import json
from pathlib import Path

import append_resolution as ar
import pytest


def _setup_run(tmp_path: Path) -> tuple[Path, Path]:
    """Create a resolve run with four review-sourced items and one GitHub-only item."""
    impl = tmp_path / "impl-run"
    review = tmp_path / "review" / "pr-7" / "run-001"
    impl.mkdir()
    review.mkdir(parents=True)
    items = [
        {"id": 1, "finding_id": "critical-aaaa1111"},
        {"id": 2, "finding_id": "performance-concerns-bbbb2222"},
        {"id": 3, "finding_id": "test-coverage-gaps-cccc3333"},
        {"id": 4, "finding_id": "api-design-dddd4444"},
        {"id": 5},
    ]
    (impl / "action-items.jsonl").write_text("".join(json.dumps(i) + "\n" for i in items), encoding="utf-8")
    (impl / "selected-items.txt").write_text("1 2 3 4 5\n", encoding="utf-8")
    log = [
        "id=1 resolution=as-suggested evidence=VALID suggestion=VALID finding=f evidence_why=e suggestion_why=s detail=pending-impl:1",
        "id=2 resolution=rejected evidence=REJECT suggestion=— finding=x=1 evidence_why=already guarded upstream suggestion_why=— detail=d",
        "id=3 resolution=self-resolved evidence=VALID suggestion=REJECT finding=f evidence_why=e suggestion_why=use fixture detail=alt",
    ]
    (impl / "challenge-log.txt").write_text("\n".join(log) + "\n", encoding="utf-8")
    (impl / "skipped-items.txt").write_text("4\tconflicts with item 1\n", encoding="utf-8")
    return impl, review


def _no_commits(item_id: int, cwd: Path, base_sha: str = "") -> str:
    """Stand in for the git lookup when no item carries a commit tag."""
    return ""


def _rows(review: Path) -> dict[int, dict]:
    """Read the resolution ledger keyed by item id."""
    return {r["item_id"]: r for r in map(json.loads, (review / "resolution.jsonl").read_text().splitlines())}


def test_records_cover_each_review_item_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fixed, rejected, self-resolved and skipped items each get one record; GitHub-only items get none."""
    shas = {1: "abc1234", 3: "def5678"}
    monkeypatch.setattr(ar, "commit_for", lambda item_id, cwd, base_sha="": shas.get(item_id, ""))
    impl, review = _setup_run(tmp_path)
    assert ar.main(["--impl-dir", str(impl), "--review-dir", str(review)]) == 0
    rows = _rows(review)
    assert set(rows) == {1, 2, 3, 4}
    assert rows[1]["verdict"] == "fixed"
    assert rows[1]["sha"] == "abc1234"
    assert rows[2]["verdict"] == "rejected"
    assert rows[2]["why"] == "already guarded upstream"
    assert rows[2]["sha"] == ""
    assert rows[3]["verdict"] == "self-resolved"
    assert rows[3]["why"] == "use fixture"
    assert rows[4]["verdict"] == "skipped"
    assert rows[4]["why"] == "conflicts with item 1"
    assert {r["run"] for r in rows.values()} == {"impl-run"}


def test_rerun_for_same_run_appends_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The ledger is append-only and idempotent per resolve run."""
    monkeypatch.setattr(ar, "commit_for", _no_commits)
    impl, review = _setup_run(tmp_path)
    assert ar.main(["--impl-dir", str(impl), "--review-dir", str(review)]) == 0
    first = (review / "resolution.jsonl").read_text()
    assert ar.main(["--impl-dir", str(impl), "--review-dir", str(review)]) == 0
    assert (review / "resolution.jsonl").read_text() == first


def test_no_challenge_entry_without_commit_is_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An item with no challenge verdict (e.g. --no-challenge) and no commit is reported pending, not fixed."""
    monkeypatch.setattr(ar, "commit_for", _no_commits)
    impl, review = _setup_run(tmp_path)
    (impl / "challenge-log.txt").write_text("", encoding="utf-8")
    assert ar.main(["--impl-dir", str(impl), "--review-dir", str(review)]) == 0
    assert _rows(review)[1]["verdict"] == "pending"


def test_missing_review_dir_blocks(tmp_path: Path) -> None:
    """Writing beside a report that no longer exists would orphan the ledger."""
    impl, _ = _setup_run(tmp_path)
    assert ar.main(["--impl-dir", str(impl), "--review-dir", str(tmp_path / "gone")]) == 1


@pytest.mark.parametrize(
    "item_id",
    [
        pytest.param(1, id="accepted-but-unimplemented"),
        pytest.param(3, id="self-resolved-without-implementation"),
    ],
)
def test_item_without_implementation_record_is_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, item_id: int
) -> None:
    """An accepted or self-resolved item with no commit or implementation record is still pending.

    A challenge that accepted the fix is not a fix, and self-resolved means resolve implemented an alternative; with no
    record of that the item stays pending.
    """
    monkeypatch.setattr(ar, "commit_for", _no_commits)
    impl, review = _setup_run(tmp_path)
    assert ar.main(["--impl-dir", str(impl), "--review-dir", str(review)]) == 0
    assert _rows(review)[item_id]["verdict"] == "pending"


@pytest.mark.parametrize(
    ("merge_result", "verdict"),
    [
        pytest.param({"applied": ["x"], "conflict": None, "remaining": []}, "fixed", id="merged-cleanly"),
        pytest.param(
            {"applied": [], "conflict": {"item_id": 1, "sha": "s", "files": []}, "remaining": []},
            "pending",
            id="merge-conflict",
        ),
    ],
)
def test_phase2_commit_counts_only_once_merged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, merge_result: dict, verdict: str
) -> None:
    """A Phase 2 commit proves the fix only when the merge-back did not stop on that item."""
    monkeypatch.setattr(ar, "commit_for", _no_commits)
    impl, review = _setup_run(tmp_path)
    (impl / "phase2-commits.jsonl").write_text(json.dumps({"item_id": 1, "sha": "aaa", "group": "g"}) + "\n")
    (impl / "merge-result.json").write_text(json.dumps(merge_result))
    assert ar.main(["--impl-dir", str(impl), "--review-dir", str(review)]) == 0
    assert _rows(review)[1]["verdict"] == verdict


def test_records_carry_title_and_location_from_findings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A later review matches outcomes by location and claim, so each record names the finding, not just its id."""
    monkeypatch.setattr(ar, "commit_for", _no_commits)
    impl, review = _setup_run(tmp_path)
    finding = {
        "id": "performance-concerns-bbbb2222",
        "title": "Slow lookup",
        "section": "### Performance Concerns",
        "file": "src/app.py",
        "line": 12,
    }
    (review / "findings.jsonl").write_text(json.dumps(finding) + "\n")
    assert ar.main(["--impl-dir", str(impl), "--review-dir", str(review)]) == 0
    row = _rows(review)[2]
    assert (row["title"], row["file"], row["line"]) == ("Slow lookup", "src/app.py", 12)


def test_commit_lookup_is_scoped_to_this_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Item ids restart every run, so the tag search starts at the run's base head when it is known."""
    calls = []
    monkeypatch.setattr(
        ar.subprocess, "run", lambda argv, **kw: calls.append(argv) or ar.subprocess.CompletedProcess(argv, 0, "", "")
    )
    ar.commit_for(3, tmp_path, "base123")
    assert calls[0][-1] == "base123..HEAD"


def test_no_records_leaves_no_empty_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A run with no review-sourced items creates no resolution.jsonl beside the review."""
    monkeypatch.setattr(ar, "commit_for", _no_commits)
    impl, review = _setup_run(tmp_path)
    (impl / "selected-items.txt").write_text("5\n", encoding="utf-8")
    assert ar.main(["--impl-dir", str(impl), "--review-dir", str(review)]) == 0
    assert not (review / "resolution.jsonl").exists()

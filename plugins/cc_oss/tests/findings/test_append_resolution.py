"""Tests for ``bin/append_resolution.py`` — resolve reporting outcomes back to the review directory.

Run records live under ``tmp_path``. The commit lookup (``commit_for``, a ``git log`` search) is the only boundary
replaced, so no real git history is needed; one test keeps the real lookup and replaces only ``subprocess.run`` to pin
the revision range it asks for.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import append_resolution as ar
import pytest

_SHAS = {1: "abc1234", 3: "def5678"}
_FOLDED_FINDINGS = [
    {"id": "survivor", "title": "Missing cleanup", "section": "Architecture", "file": "src/app.py", "line": 12},
    {"id": "folded-a", "title": "Capacity leak", "section": "Performance", "file": "src/pool.py", "line": 40},
    {"id": "folded-b", "title": "Wrong count", "section": "Tests", "file": "tests/test_pool.py", "line": 21},
]
_CHALLENGE_LOG = [
    "id=1 resolution=as-suggested evidence=VALID suggestion=VALID finding=f evidence_why=e suggestion_why=s detail=pending-impl:1",
    "id=2 resolution=rejected evidence=REJECT suggestion=— finding=x=1 evidence_why=already guarded upstream suggestion_why=— detail=d",
    "id=3 resolution=self-resolved evidence=VALID suggestion=REJECT finding=f evidence_why=e suggestion_why=use fixture detail=alt",
]


@pytest.fixture
def run_dirs(tmp_path: Path) -> tuple[Path, Path]:
    """Create a resolve run with four review-sourced items and one GitHub-only item; return ``(impl, review)``."""
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
    (impl / "challenge-log.txt").write_text("\n".join(_CHALLENGE_LOG) + "\n", encoding="utf-8")
    (impl / "skipped-items.txt").write_text("4\tconflicts with item 1\n", encoding="utf-8")
    return impl, review


def _commit_lookup(**replacement: object) -> Iterator[mock.Mock]:
    """Replace the ``git log`` commit lookup used by ``append_resolution`` for the duration of one test."""
    with mock.patch("append_resolution.commit_for", autospec=True, **replacement) as patched:
        yield patched


@pytest.fixture
def no_commits() -> Iterator[mock.Mock]:
    """Make the commit lookup find no commit tag for any item."""
    yield from _commit_lookup(return_value="")


@pytest.fixture
def tagged_commits() -> Iterator[mock.Mock]:
    """Make the commit lookup find a commit for items 1 and 3 only."""
    yield from _commit_lookup(side_effect=lambda item_id, cwd, base_sha="": _SHAS.get(item_id, ""))


@pytest.fixture
def single_commit() -> Iterator[mock.Mock]:
    """Make the commit lookup find commit ``abc1234`` for every item."""
    yield from _commit_lookup(return_value="abc1234")


@pytest.fixture
def folded_run(run_dirs: tuple[Path, Path]) -> tuple[Path, Path, Path]:
    """Narrow the run to the selected item ``survivor`` and write its findings; return ``(impl, review, findings)``.

    The folded findings are not attached yet: tests call ``_fold`` to add ``merged_finding_ids`` to the survivor.
    """
    impl, review = run_dirs
    (impl / "action-items.jsonl").write_text(_survivor_item() + "\n", encoding="utf-8")
    (impl / "selected-items.txt").write_text("1\n", encoding="utf-8")
    findings_path = review / "findings.jsonl"
    findings_path.write_text("".join(json.dumps(row) + "\n" for row in _FOLDED_FINDINGS), encoding="utf-8")
    return impl, review, findings_path


def _survivor_item(**extra: object) -> str:
    """Return the JSON line of the selected ``survivor`` action item, plus any ``extra`` fields."""
    return json.dumps({"id": 1, "finding_id": "survivor", "location": "report", **extra})


def _fold(impl: Path) -> None:
    """Mark ``folded-a`` and ``folded-b`` as merged into the survivor item."""
    merged = _survivor_item(merged_finding_ids=["folded-a", "folded-b"])
    (impl / "action-items.jsonl").write_text(merged + "\n", encoding="utf-8")


def _resolve(impl: Path, review: Path) -> int:
    """Run the command-line entry point over ``impl`` and ``review`` and return its exit code."""
    return ar.main(["--impl-dir", str(impl), "--review-dir", str(review)])


def _rows(review: Path) -> dict[int, dict]:
    """Read the resolution ledger keyed by item id."""
    return {r["item_id"]: r for r in map(json.loads, (review / "resolution.jsonl").read_text().splitlines())}


class TestResolutionLedger:
    """The resolution ledger written beside the review: one record per review-sourced item, append-only."""

    @pytest.mark.usefixtures("tagged_commits")
    def test_records_cover_each_review_item_once(self, run_dirs: tuple[Path, Path]) -> None:
        """Fixed, rejected, self-resolved and skipped items each get one record; GitHub-only items get none.

        The run has five selected items: items 1 and 3 carry a commit tag, item 2 was rejected by the challenge, item 4
        was skipped for a conflict, and item 5 has no review finding behind it.
        """
        impl, review = run_dirs

        rc = _resolve(impl, review)

        rows = _rows(review)
        assert rc == 0
        assert set(rows) == {1, 2, 3, 4}
        assert [(rows[i]["verdict"], rows[i]["sha"]) for i in (1, 2, 3, 4)] == [
            ("fixed", "abc1234"),
            ("rejected", ""),
            ("self-resolved", "def5678"),
            ("skipped", ""),
        ]
        assert (rows[2]["why"], rows[3]["why"], rows[4]["why"]) == (
            "already guarded upstream",
            "use fixture",
            "conflicts with item 1",
        )
        assert {r["run"] for r in rows.values()} == {"impl-run"}

    @pytest.mark.usefixtures("no_commits")
    def test_rerun_for_same_run_appends_nothing(self, run_dirs: tuple[Path, Path]) -> None:
        """The ledger is append-only and idempotent per resolve run.

        A second invocation over the same resolve run must leave the ledger byte for byte as the first wrote it.
        """
        impl, review = run_dirs
        _resolve(impl, review)
        first = (review / "resolution.jsonl").read_text()

        rc = _resolve(impl, review)

        assert (rc, (review / "resolution.jsonl").read_text()) == (0, first)

    @pytest.mark.usefixtures("no_commits")
    def test_missing_review_dir_blocks(self, tmp_path: Path, run_dirs: tuple[Path, Path]) -> None:
        """Writing beside a report that no longer exists would orphan the ledger.

        The review directory was removed after the run started, so the command must exit 1 instead of recreating it.
        """
        impl, _ = run_dirs

        assert _resolve(impl, tmp_path / "gone") == 1

    @pytest.mark.usefixtures("no_commits")
    def test_no_records_leaves_no_empty_ledger(self, run_dirs: tuple[Path, Path]) -> None:
        """A run with no review-sourced items creates no resolution.jsonl beside the review.

        Only the GitHub-only item 5 is selected, so there is nothing to record and no empty file should appear.
        """
        impl, review = run_dirs
        (impl / "selected-items.txt").write_text("5\n", encoding="utf-8")

        rc = _resolve(impl, review)

        assert (rc, (review / "resolution.jsonl").exists()) == (0, False)

    @pytest.mark.usefixtures("no_commits")
    def test_records_carry_title_and_location_from_findings(self, run_dirs: tuple[Path, Path]) -> None:
        """A later review matches outcomes by location and claim, so each record names the finding, not just its id.

        The review's ``findings.jsonl`` supplies the title, section, file and line copied onto the matching record.
        """
        impl, review = run_dirs
        finding = {
            "id": "performance-concerns-bbbb2222",
            "title": "Slow lookup",
            "section": "### Performance Concerns",
            "file": "src/app.py",
            "line": 12,
        }
        (review / "findings.jsonl").write_text(json.dumps(finding) + "\n", encoding="utf-8")

        rc = _resolve(impl, review)

        row = _rows(review)[2]
        assert (rc, row["title"], row["file"], row["line"]) == (0, "Slow lookup", "src/app.py", 12)


class TestVerdicts:
    """How an item's challenge entry, commit tag and merge result decide its verdict."""

    @pytest.mark.usefixtures("no_commits")
    def test_no_challenge_entry_without_commit_is_pending(self, run_dirs: tuple[Path, Path]) -> None:
        """An item with no challenge verdict (e.g. --no-challenge) and no commit is reported pending, not fixed.

        The challenge log is empty and no commit tag is found for item 1.
        """
        impl, review = run_dirs
        (impl / "challenge-log.txt").write_text("", encoding="utf-8")

        rc = _resolve(impl, review)

        assert (rc, _rows(review)[1]["verdict"]) == (0, "pending")

    @pytest.mark.usefixtures("no_commits")
    @pytest.mark.parametrize(
        "item_id",
        [
            pytest.param(1, id="accepted-but-unimplemented"),
            pytest.param(3, id="self-resolved-without-implementation"),
        ],
    )
    def test_item_without_implementation_record_is_pending(self, run_dirs: tuple[Path, Path], item_id: int) -> None:
        """An accepted or self-resolved item with no commit or implementation record is still pending.

        A challenge that accepted the fix is not a fix, and self-resolved means resolve implemented an alternative; with
        no record of that the item stays pending.
        """
        impl, review = run_dirs

        rc = _resolve(impl, review)

        assert (rc, _rows(review)[item_id]["verdict"]) == (0, "pending")

    @pytest.mark.usefixtures("no_commits")
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
        self, run_dirs: tuple[Path, Path], merge_result: dict, verdict: str
    ) -> None:
        """A Phase 2 commit proves the fix only when the merge-back did not stop on that item.

        Item 1 has a Phase 2 commit; it is fixed after a clean merge and still pending when the merge stopped on it.
        """
        impl, review = run_dirs
        (impl / "phase2-commits.jsonl").write_text(
            json.dumps({"item_id": 1, "sha": "aaa", "group": "g"}) + "\n", encoding="utf-8"
        )
        (impl / "merge-result.json").write_text(json.dumps(merge_result), encoding="utf-8")

        rc = _resolve(impl, review)

        assert (rc, _rows(review)[1]["verdict"]) == (0, verdict)


class TestFoldedFindings:
    """Findings folded into a selected item inherit its outcome."""

    @pytest.mark.usefixtures("single_commit")
    def test_records_include_each_folded_finding_without_changing_survivor(
        self, folded_run: tuple[Path, Path, Path], tmp_path: Path
    ) -> None:
        """A selected item's folded findings inherit its outcome and retain their own claims and symptom locations.

        The survivor's record must stay byte for byte what it was before anything was folded into it; each folded
        finding adds a record carrying the survivor's verdict, sha and reason plus its own title, section and location.
        """
        impl, _, findings_path = folded_run
        (original,) = ar.build_records(impl, "impl-run", tmp_path, findings=findings_path)
        _fold(impl)

        records = ar.build_records(impl, "impl-run", tmp_path, findings=findings_path)

        keys = ("title", "section", "file", "line")
        folded = [
            {**original, "finding_id": f["id"], **{key: f[key] for key in keys}, "folded_into": "survivor"}
            for f in _FOLDED_FINDINGS[1:]
        ]
        assert (original["verdict"], original["sha"], original["why"]) == ("fixed", "abc1234", "s")
        assert len(records) == 3
        assert json.dumps(records[0], separators=(",", ":")) == json.dumps(original, separators=(",", ":"))
        assert records[1:] == folded

    @pytest.mark.usefixtures("single_commit")
    def test_folded_records_are_written_once(self, folded_run: tuple[Path, Path, Path], tmp_path: Path) -> None:
        """The ledger holds the survivor and folded records, and a rerun leaves it byte for byte unchanged.

        Folded findings are part of the same idempotent append as the survivor, so rerunning resolve must not add them a
        second time.
        """
        impl, review, findings_path = folded_run
        _fold(impl)
        expected = ar.build_records(impl, "impl-run", tmp_path, findings=findings_path)
        _resolve(impl, review)
        ledger = (review / "resolution.jsonl").read_bytes()

        rerun_rc = _resolve(impl, review)

        assert [json.loads(line) for line in ledger.splitlines()] == expected
        assert (rerun_rc, (review / "resolution.jsonl").read_bytes()) == (0, ledger)


class TestCommitFor:
    """The commit lookup that maps an item id to the commit tagged with it."""

    def test_option_shaped_base_stops_ledger_write(self, run_dirs: tuple[Path, Path]) -> None:
        """Invalid command-line revision input never produces resolution records."""
        impl, review = run_dirs

        result = ar.main(["--impl-dir", str(impl), "--review-dir", str(review), "--base-sha=--output=unexpected"])

        assert result == 1
        assert not (review / "resolution.jsonl").exists()

    @mock.patch("append_resolution.subprocess.run", autospec=True)
    def test_option_shaped_base_is_rejected(self, mock_run: mock.Mock, tmp_path: Path) -> None:
        """An option-shaped base never reaches the Git command line."""
        assert ar.commit_for(3, tmp_path, "--output=unexpected") == ""
        mock_run.assert_not_called()

    @mock.patch("append_resolution.subprocess.run", autospec=True)
    def test_lookup_is_scoped_to_this_run(self, mock_run: mock.Mock, tmp_path: Path) -> None:
        """Item ids restart every run, so the tag search starts at the run's base head when it is known.

        With base ``base123`` the ``git log`` revision range must be ``base123..HEAD``, never the whole history.
        """
        mock_run.return_value = subprocess.CompletedProcess([], 0, "", "")

        ar.commit_for(3, tmp_path, "base123")

        assert mock_run.call_args.args[0][-1] == "base123..HEAD"

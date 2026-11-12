"""Exercise the PR drift grading and measurement against a stubbed GitHub CLI boundary."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "bin"))
import pr_drift

_PR = {
    "number": 7, "url": "https://github.com/o/r/pull/7", "state": "OPEN", "baseRefName": "main", "headRefOid": "abc",
    "mergeable": "UNKNOWN", "createdAt": "2020-01-01T00:00:00Z", "changedFiles": 3,
    "commits": [{"committedDate": "2020-01-02T00:00:00Z"}],
    "closingIssuesReferences": [{"number": 3, "url": "https://github.com/x/y/issues/3"}],
}  # fmt: skip
_MB = "mbsha000"


@pytest.mark.parametrize(
    ("days", "share", "count", "removed", "expected"),
    [
        pytest.param(2, 0.0, 0, 0, ("fresh", "none"), id="fresh-undrifted"),
        pytest.param(45, 0.0, 0, 0, ("aging", "none"), id="aging-by-age"),
        pytest.param(200, 0.0, 0, 0, ("stale", "none"), id="stale-by-age-only"),
        pytest.param(1, 0.5, 1, 0, ("fresh", "light"), id="drift-never-ages-a-fresh-push"),
        pytest.param(1, 1.0, 2, 0, ("fresh", "light"), id="two-file-pr-is-never-heavy-by-share"),
        pytest.param(1, 0.3, 3, 0, ("fresh", "heavy"), id="three-files-and-share-is-heavy"),
        pytest.param(1, 0.05, 1, 1, ("fresh", "heavy"), id="removed-file-is-heavy"),
        pytest.param(None, None, 0, 0, ("unknown", "unknown"), id="unknown-stays-unknown"),
        pytest.param(5, None, 0, 0, ("fresh", "unknown"), id="unmeasured-drift-is-not-none"),
    ],
)
def test_grade(days: int | None, share: float | None, count: int, removed: int, expected: tuple) -> None:
    """Grading follows the thresholds, keeps age and drift separate and never turns unknown into fresh or none."""
    assert pr_drift.grade(days, share, count, removed) == expected


def _fake_run(responses: dict[str, str | tuple[int, str]]):
    """Build a ``_run`` stub answering by the first key found in the joined arguments; unmatched calls fail."""

    def run(args: list[str], timeout: int) -> tuple[int, str, str]:
        joined = " ".join(args)
        for key, value in responses.items():
            if key in joined:
                if isinstance(value, tuple):
                    return value[0], "", value[1]
                return 0, value, ""
        return 1, "", "boom"

    return run


def _base_responses(**extra: str | tuple[int, str]) -> dict[str, str | tuple[int, str]]:
    """Responses shared by the measurement tests; ``extra`` entries take precedence over the defaults."""
    defaults: dict[str, str | tuple[int, str]] = {
        "pr view": json.dumps(_PR),
        "commits/abc": "2020-01-02T00:00:00Z",
        "compare/main...abc": f"1\t300\t{_MB}\t2020-01-01T00:00:00Z",
        "pulls/7/files": "a.py\t10\nmoved.py\t4\ncalm.py\t1",
    }
    return {**defaults, **extra}


class TestMeasure:
    """Drift measurement from the base's change list and from the per-file fallback."""

    def test_merge_base_commit_is_not_counted_and_rename_is_not_removal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A file the base moved is ``renamed`` and an untouched file stays out of the drift list.

        The base change list is read once from the merge base, so a commit at the merge base never counts as drift and a
        mechanical move never reads as a removal.
        """
        changes = "a.py\tmodified\t\t12\nsrc/moved.py\trenamed\tmoved.py\t0"
        monkeypatch.setattr(pr_drift, "_run", _fake_run(_base_responses(**{f"compare/{_MB}...main": changes})))
        out = pr_drift.measure(7, "o/r", 40, 5)
        kinds = {e["path"]: (e["kind"], e["renamed_to"]) for e in out["drifted_files"]}
        assert kinds == {"a.py": ("changed", None), "moved.py": ("renamed", "src/moved.py")}
        assert out["source"] == "compare"
        assert out["removed_count"] == 0
        assert out["drift"] == "light"
        assert out["behind_by"] == 300

    def test_removed_file_makes_drift_heavy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A touched file the base deleted is reported removed and grades the drift heavy."""
        changes = "calm.py\tremoved\t\t9"
        monkeypatch.setattr(pr_drift, "_run", _fake_run(_base_responses(**{f"compare/{_MB}...main": changes})))
        out = pr_drift.measure(7, "o/r", 40, 5)
        assert out["removed_count"] == 1
        assert out["drift"] == "heavy"

    def test_cut_off_compare_falls_back_to_per_file_probes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A full 300-file base list is treated as possibly cut off; per-file probes run and the gap is disclosed.

        The probe ignores the merge-base commit, and only a 404 from the contents API marks a path absent.
        """
        full = "\n".join(f"f{i}.py\tmodified\t\t1" for i in range(pr_drift.COMPARE_FILE_CAP))
        responses = _base_responses(
            **{
                f"compare/{_MB}...main": full,
                "path=a.py": f"{_MB}\nnew1\nnew2",
                "contents/a.py": "",
                "path=moved.py": (1, "boom"),
                "path=calm.py": _MB,
            }
        )
        monkeypatch.setattr(pr_drift, "_run", _fake_run(responses))
        out = pr_drift.measure(7, "o/r", 40, 5)
        assert out["source"] == "per-file"
        assert [(e["path"], e["weight"]) for e in out["drifted_files"]] == [("a.py", 2)]
        assert any("per-file probes" in note for note in out["incomplete"])
        assert any("1 file probe(s) failed" in note for note in out["incomplete"])

    def test_contents_error_other_than_404_is_a_failed_probe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A rate-limit style error on the existence check never marks a file absent."""
        responses = {"path=a.py": "new1", "contents/a.py": (1, "HTTP 403: rate limit")}
        monkeypatch.setattr(pr_drift, "_run", _fake_run(responses))
        assert pr_drift._probe_file("o/r", "main", (_MB, "d"), "a.py", 5) is None

    def test_missing_path_is_absent_on_404(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A 404 on the existence check marks the path absent, not removed: the base may have moved it."""
        responses = {"path=a.py": "new1", "contents/a.py": (1, "HTTP 404: Not Found")}
        monkeypatch.setattr(pr_drift, "_run", _fake_run(responses))
        assert pr_drift._probe_file("o/r", "main", (_MB, "d"), "a.py", 5)["kind"] == "absent"

    def test_failed_compare_leaves_drift_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When the compare call fails nothing is graded fresh or none, and the gap is disclosed."""
        responses = {"pr view": json.dumps(_PR), "commits/abc": "2999-01-01T00:00:00Z"}
        monkeypatch.setattr(pr_drift, "_run", _fake_run(responses))
        out = pr_drift.measure(7, "o/r", 40, 5)
        assert out["drift"] == "unknown"
        assert out["behind_by"] is None
        assert any("compare unavailable" in note for note in out["incomplete"])

    def test_closed_pr_is_not_graded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A merged or closed PR gets n/a grades and no drift calls."""
        monkeypatch.setattr(pr_drift, "_run", _fake_run({"pr view": json.dumps({**_PR, "state": "MERGED"})}))
        out = pr_drift.measure(7, "o/r", 40, 5)
        assert (out["staleness"], out["drift"]) == ("n/a", "n/a")

    def test_cut_file_list_is_disclosed_with_the_real_total(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A PR file list shorter than the PR's own changed-file count is flagged with both numbers."""
        pr = {**_PR, "changedFiles": 1619}
        responses = _base_responses(**{"pr view": json.dumps(pr), f"compare/{_MB}...main": "x\tmodified\t\t1"})
        monkeypatch.setattr(pr_drift, "_run", _fake_run(responses))
        out = pr_drift.measure(7, "o/r", 40, 5)
        assert out["files_total"] == 1619
        assert "PR file list cut: 3 of 1619 files" in out["incomplete"]

    def test_recent_base_commits_and_linked_issue_urls_are_returned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Recent base commits come back as data for the verdict, and linked issues keep their repository."""
        changes = "a.py\tmodified\t\t12"
        responses = _base_responses(
            **{f"compare/{_MB}...main": changes, "path=a.py": "deadbeef1\t2021-05-05T00:00:00Z\tRework parser"}
        )
        monkeypatch.setattr(pr_drift, "_run", _fake_run(responses))
        out = pr_drift.measure(7, "o/r", 40, 5)
        assert out["recent_base_commits"] == [
            {"path": "a.py", "sha": "deadbeef1", "date": "2021-05-05T00:00:00Z", "subject": "Rework parser"}
        ]
        assert out["linked_issues"] == ["https://github.com/x/y/issues/3"]

    def test_unreadable_pr_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unreadable PR is a hard error so the caller can report an unclear verdict."""
        monkeypatch.setattr(pr_drift, "_run", _fake_run({}))
        with pytest.raises(RuntimeError, match="cannot fetch PR #7"):
            pr_drift.measure(7, "o/r", 40, 5)

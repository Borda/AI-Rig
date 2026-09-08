"""Tests for ``bin/merge_specialist_batch.py``.

``subprocess.run`` and module-level ``which`` are monkeypatched — no real ``git`` invocations. Covers plan parsing, the
each-mode passthrough (no soft-reset), the non-each soft-reset behaviour, and conflict handling that stops the plan and
reports remaining entries.
"""

from __future__ import annotations

from typing import Any

import pytest

import merge_specialist_batch as msb


class _FakeCompleted:
    """Minimal stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        """Store the status and output returned by a fake Git command."""
        self.returncode = returncode
        self.stdout = stdout


def _patch_git(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cherry_pick_rc_by_sha: dict[str, int] | None = None,
    conflicted_files_out: str = "",
) -> list[list[str]]:
    """Register subprocess.run fake dispatching on git subcommand; return recorded commands."""
    recorded: list[list[str]] = []
    rc_by_sha = cherry_pick_rc_by_sha or {}

    def _fake_run(cmd: list[str], **_kwargs: Any) -> _FakeCompleted:
        """Record Git calls and return configured cherry-pick/conflict output."""
        recorded.append(list(cmd))
        if cmd[1] == "cherry-pick":
            sha = cmd[3]  # [git, "cherry-pick", "--end-of-options", sha]
            return _FakeCompleted(returncode=rc_by_sha.get(sha, 0))
        if cmd[1] == "diff":
            return _FakeCompleted(stdout=conflicted_files_out)
        return _FakeCompleted()

    monkeypatch.setattr(msb.subprocess, "run", _fake_run)
    monkeypatch.setattr(msb, "which", lambda _: "/fake/git")
    return recorded


class TestParsePlan:
    """parse_plan: JSON array of {item_id, sha} → ordered PlanEntry list."""

    def test_preserves_order(self) -> None:
        """Multiple entries parse in the exact input order."""
        raw = '[{"item_id": "3", "sha": "aaa1111"}, {"item_id": "6", "sha": "bbb2222"}]'
        result = msb.parse_plan(raw)
        assert result == [msb.PlanEntry(item_id="3", sha="aaa1111"), msb.PlanEntry(item_id="6", sha="bbb2222")]

    def test_empty_plan(self) -> None:
        """Empty JSON array parses to an empty list."""
        assert msb.parse_plan("[]") == []

    def test_optional_group_and_module_fields(self) -> None:
        """Group/module are read when present and default to empty strings when absent."""
        raw = (
            '[{"item_id": "1", "sha": "aaa1111", "group": "sw", "module": "pkg.core"},'
            ' {"item_id": "2", "sha": "bbb2222"}]'
        )
        result = msb.parse_plan(raw)
        assert result == [
            msb.PlanEntry(item_id="1", sha="aaa1111", group="sw", module="pkg.core"),
            msb.PlanEntry(item_id="2", sha="bbb2222", group="", module=""),
        ]

    def test_invalid_sha_hard_fails(self) -> None:
        """A sha failing _SHA_RE (e.g. leading '-') raises, never silently skips."""
        raw = '[{"item_id": "1", "sha": "--strategy=evil"}]'
        with pytest.raises(ValueError, match="invalid sha"):
            msb.parse_plan(raw)


class TestOrderPlan:
    """order_plan: reorder whole worktree groups most-central-first, intra-group order kept."""

    def test_most_central_group_lands_first(self) -> None:
        """Group with the higher max-centrality module is emitted before the lower one."""
        plan = [
            msb.PlanEntry("1", "aa", group="docs", module="pkg.readme"),
            msb.PlanEntry("2", "bb", group="sw", module="pkg.core"),
            msb.PlanEntry("3", "cc", group="sw", module="pkg.util"),
        ]
        result = msb.order_plan(plan, {"pkg.core": 9.0, "pkg.readme": 1.0})
        assert [e.item_id for e in result] == ["2", "3", "1"]

    def test_intra_group_order_preserved(self) -> None:
        """Commit order within a single group is never reshuffled by centrality."""
        plan = [
            msb.PlanEntry("1", "aa", group="sw", module="pkg.util"),
            msb.PlanEntry("2", "bb", group="sw", module="pkg.core"),
        ]
        result = msb.order_plan(plan, {"pkg.core": 9.0, "pkg.util": 1.0})
        assert [e.item_id for e in result] == ["1", "2"]

    def test_empty_centrality_keeps_input_order(self) -> None:
        """No scores → every group weighs 0 → stable order equals the input order."""
        plan = [
            msb.PlanEntry("1", "aa", group="docs", module="pkg.readme"),
            msb.PlanEntry("2", "bb", group="sw", module="pkg.core"),
        ]
        result = msb.order_plan(plan, {})
        assert [e.item_id for e in result] == ["1", "2"]


class TestRunPlanEachMode:
    """run_plan with commit_mode='each': cherry-pick lands, no soft-reset."""

    def test_all_entries_applied_no_reset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Two clean cherry-picks in each mode → both applied, no reset call issued."""
        recorded = _patch_git(monkeypatch)
        entries = [msb.PlanEntry(item_id="1", sha="aaa"), msb.PlanEntry(item_id="2", sha="bbb")]
        result = msb.run_plan(entries, msb.CommitMode.EACH)
        assert result == {"applied": ["1", "2"], "conflict": None, "remaining": []}
        reset_calls = [c for c in recorded if c[1] == "reset"]
        assert reset_calls == []


class TestRunPlanNonEachMode:
    """run_plan with commit_mode in {grouped, all, stage}: soft-reset after each pick."""

    @pytest.mark.parametrize(
        "mode",
        [
            pytest.param(msb.CommitMode.GROUPED, id="grouped"),
            pytest.param(msb.CommitMode.ALL, id="all"),
            pytest.param(msb.CommitMode.STAGE, id="stage"),
        ],
    )
    def test_soft_reset_after_each_pick(self, monkeypatch: pytest.MonkeyPatch, mode: msb.CommitMode) -> None:
        """Each successful cherry-pick is immediately soft-reset in non-each modes."""
        recorded = _patch_git(monkeypatch)
        entries = [msb.PlanEntry(item_id="1", sha="aaa"), msb.PlanEntry(item_id="2", sha="bbb")]
        result = msb.run_plan(entries, mode)
        assert result["applied"] == ["1", "2"]
        reset_calls = [c for c in recorded if c[1] == "reset" and c[2:] == ["--soft", "HEAD~1"]]
        assert len(reset_calls) == 2


class TestRunPlanConflict:
    """run_plan: a failing cherry-pick stops the plan and reports remaining entries."""

    def test_conflict_stops_plan_and_reports_remaining(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Second entry conflicts → first stays applied, conflict entry reported, third stays remaining."""
        recorded = _patch_git(monkeypatch, cherry_pick_rc_by_sha={"bbb": 1}, conflicted_files_out="src/foo.py\n")
        entries = [
            msb.PlanEntry(item_id="1", sha="aaa"),
            msb.PlanEntry(item_id="2", sha="bbb"),
            msb.PlanEntry(item_id="3", sha="ccc"),
        ]
        result = msb.run_plan(entries, msb.CommitMode.EACH)
        assert result["applied"] == ["1"]
        assert result["conflict"] == {"item_id": "2", "sha": "bbb", "files": ["src/foo.py"]}
        assert result["remaining"] == ["3"]
        pick_shas = [c[3] for c in recorded if c[1] == "cherry-pick"]
        assert pick_shas == ["aaa", "bbb"]

    def test_conflict_no_reset_issued_for_conflicted_entry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A conflicting cherry-pick never reaches the soft-reset call, even in non-each mode."""
        recorded = _patch_git(monkeypatch, cherry_pick_rc_by_sha={"aaa": 1})
        entries = [msb.PlanEntry(item_id="1", sha="aaa")]
        msb.run_plan(entries, msb.CommitMode.GROUPED)
        reset_calls = [c for c in recorded if c[1] == "reset"]
        assert reset_calls == []


class TestMainCli:
    """Read a merge plan and report its conflict state through the command line."""

    def test_clean_plan_exits_0(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """All entries apply cleanly → exit 0, JSON result printed to stdout."""
        _patch_git(monkeypatch)
        plan_file = tmp_path / "plan.json"
        plan_file.write_text('[{"item_id": "1", "sha": "aaa1111"}]', encoding="utf-8")
        rc = msb.main(["--plan", str(plan_file), "--commit-mode", "each"])
        assert rc == 0
        assert '"applied": ["1"]' in capsys.readouterr().out

    def test_conflict_exits_1(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A conflicting entry → exit 1, conflict details printed to stdout."""
        _patch_git(monkeypatch, cherry_pick_rc_by_sha={"aaa1111": 1})
        plan_file = tmp_path / "plan.json"
        plan_file.write_text('[{"item_id": "1", "sha": "aaa1111"}]', encoding="utf-8")
        rc = msb.main(["--plan", str(plan_file), "--commit-mode", "each"])
        assert rc == 1
        assert '"conflict"' in capsys.readouterr().out

    def test_invalid_plan_sha_exits_2(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An invalid sha in the plan file → exit 2, JSON error printed, no cherry-pick attempted."""
        recorded = _patch_git(monkeypatch)
        plan_file = tmp_path / "plan.json"
        plan_file.write_text('[{"item_id": "1", "sha": "-evil"}]', encoding="utf-8")
        rc = msb.main(["--plan", str(plan_file), "--commit-mode", "each"])
        assert rc == 2
        assert '"error"' in capsys.readouterr().out
        assert recorded == []

    def test_centrality_file_reorders_before_apply(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Verify command-line option behavior.

        ``--centrality-file`` reorders whole groups so the most-central group's shas are picked first.
        """
        recorded = _patch_git(monkeypatch)
        plan_file = tmp_path / "plan.json"
        plan_file.write_text(
            '[{"item_id": "1", "sha": "aaa1111", "group": "docs", "module": "pkg.readme"},'
            ' {"item_id": "2", "sha": "bbb2222", "group": "sw", "module": "pkg.core"}]',
            encoding="utf-8",
        )
        cent_file = tmp_path / "cent.json"
        cent_file.write_text('{"pkg.core": 9.0, "pkg.readme": 1.0}', encoding="utf-8")
        rc = msb.main(["--plan", str(plan_file), "--commit-mode", "each", "--centrality-file", str(cent_file)])
        assert rc == 0
        pick_shas = [c[3] for c in recorded if c[1] == "cherry-pick"]
        assert pick_shas == ["bbb2222", "aaa1111"]

    def test_missing_required_args_exits_2(self) -> None:
        """Neither ``--plan`` nor ``--commit-mode`` supplied → argparse exits 2."""
        with pytest.raises(SystemExit) as exc:
            msb.main([])
        assert exc.value.code == 2

"""Tests for ``bin/merge_specialist_batch.py``.

``subprocess.run`` and module-level ``which`` are monkeypatched — no real ``git`` invocations. Covers plan parsing, the
each-mode passthrough (no soft-reset), the non-each deferred combined soft-reset, and conflict handling that stops the
plan and reports remaining entries without resetting any already-applied commit.
"""

from __future__ import annotations

from typing import Any

import merge_specialist_batch as msb
import pytest


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

    @pytest.mark.parametrize(
        ("plan", "centrality", "expected_order"),
        [
            pytest.param(
                [
                    msb.PlanEntry("1", "aa", group="docs", module="pkg.readme"),
                    msb.PlanEntry("2", "bb", group="sw", module="pkg.core"),
                    msb.PlanEntry("3", "cc", group="sw", module="pkg.util"),
                ],
                {"pkg.core": 9.0, "pkg.readme": 1.0},
                ["2", "3", "1"],
                id="most-central-group-lands-first",
            ),
            pytest.param(
                [
                    msb.PlanEntry("1", "aa", group="sw", module="pkg.util"),
                    msb.PlanEntry("2", "bb", group="sw", module="pkg.core"),
                ],
                {"pkg.core": 9.0, "pkg.util": 1.0},
                ["1", "2"],
                id="intra-group-order-preserved",
            ),
            pytest.param(
                [
                    msb.PlanEntry("1", "aa", group="docs", module="pkg.readme"),
                    msb.PlanEntry("2", "bb", group="sw", module="pkg.core"),
                ],
                {},
                ["1", "2"],
                id="empty-centrality-keeps-input-order",
            ),
        ],
    )
    def test_groups_ordered_by_centrality_with_stable_order_inside(
        self, plan: list[msb.PlanEntry], centrality: dict[str, float], expected_order: list[str]
    ) -> None:
        """The group with the higher max-centrality module is emitted first; commit order inside a group is kept.

        Commit order within a single group is never reshuffled by centrality. With no scores every group weighs 0, so
        the stable order equals the input order.
        """
        result = msb.order_plan(plan, centrality)
        assert [e.item_id for e in result] == expected_order


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
    """run_plan with commit_mode in {grouped, all, stage}: one combined soft-reset after the whole plan lands."""

    @pytest.mark.parametrize(
        "mode",
        [
            pytest.param(msb.CommitMode.GROUPED, id="grouped"),
            pytest.param(msb.CommitMode.ALL, id="all"),
            pytest.param(msb.CommitMode.STAGE, id="stage"),
        ],
    )
    def test_single_combined_reset_after_full_plan(self, monkeypatch: pytest.MonkeyPatch, mode: msb.CommitMode) -> None:
        """Two clean cherry-picks in non-each mode land as real commits, then collapse via one HEAD~2 reset.

        Resetting per entry would leave the index non-clean for the next cherry-pick — RF14: a real ``git cherry-pick``
        against an index carrying a prior entry's staged-but-uncommitted diff exits 128 even when the two entries touch
        disjoint files. Each entry must land as a real commit during the loop; only the fully-applied plan collapses, in
        one combined reset sized by entry count.
        """
        recorded = _patch_git(monkeypatch)
        entries = [msb.PlanEntry(item_id="1", sha="aaa"), msb.PlanEntry(item_id="2", sha="bbb")]
        result = msb.run_plan(entries, mode)
        assert result["applied"] == ["1", "2"]
        pick_shas = [c[3] for c in recorded if c[1] == "cherry-pick"]
        assert pick_shas == ["aaa", "bbb"]  # both picked as real commits, no reset between them
        reset_calls = [c for c in recorded if c[1] == "reset"]
        assert reset_calls == [["/fake/git", "reset", "--soft", "--end-of-options", "HEAD~2"]]

    @pytest.mark.parametrize(
        ("entries", "mode", "base_sha", "expected_applied", "expected_reset_calls"),
        [
            pytest.param(
                [msb.PlanEntry(item_id="1", sha="aaa")],
                msb.CommitMode.GROUPED,
                None,
                ["1"],
                [["/fake/git", "reset", "--soft", "--end-of-options", "HEAD~1"]],
                id="single-entry-reset-sized-head-1",
            ),
            pytest.param(
                [msb.PlanEntry(item_id="3", sha="ccc")],
                msb.CommitMode.STAGE,
                "deadbeef",
                ["3"],
                [["/fake/git", "reset", "--soft", "--end-of-options", "deadbeef"]],
                id="base-sha-resets-to-fixed-target-not-head-count",
            ),
            pytest.param(
                [msb.PlanEntry(item_id="1", sha="aaa")],
                msb.CommitMode.STAGE,
                None,
                ["1"],
                [["/fake/git", "reset", "--soft", "--end-of-options", "HEAD~1"]],
                id="no-base-sha-falls-back-to-head-count",
            ),
            pytest.param(
                [],
                msb.CommitMode.STAGE,
                "deadbeef",
                [],
                [["/fake/git", "reset", "--soft", "--end-of-options", "deadbeef"]],
                id="base-sha-collapses-even-with-empty-plan",
            ),
            pytest.param([], msb.CommitMode.STAGE, None, [], [], id="no-base-sha-empty-plan-never-resets"),
        ],
    )
    def test_reset_target_follows_base_sha_or_entry_count(
        self,
        monkeypatch: pytest.MonkeyPatch,
        entries: list[msb.PlanEntry],
        mode: msb.CommitMode,
        base_sha: str | None,
        expected_applied: list[str],
        expected_reset_calls: list[list[str]],
    ) -> None:
        """The combined soft-reset is sized by entry count unless a ``base_sha`` gives a fixed target.

        A one-entry plan resets by exactly HEAD~1, not a fixed constant reused for any count. A ``base_sha`` reset
        targets that fixed commit, not ``HEAD~<len(applied)>`` (W2): a resumed call after a conflict only re-applies the
        ``remaining`` entries, so sizing the reset off this call's own ``len(applied)`` collapses only those entries and
        leaves every entry applied in an earlier call as a permanent real commit. An empty plan with ``base_sha`` still
        resets (W2's 3rd-round gap): the conflict landed on the LAST entry, so the resumed plan is empty, yet earlier
        entries still sit above ``base_sha`` as uncollapsed commits. An empty plan with no ``base_sha`` issues no reset
        — there is nothing to size ``HEAD~0`` against.
        """
        recorded = _patch_git(monkeypatch)
        result = msb.run_plan(entries, mode, base_sha=base_sha)
        assert result == {"applied": expected_applied, "conflict": None, "remaining": []}
        reset_calls = [c for c in recorded if c[1] == "reset"]
        assert reset_calls == expected_reset_calls


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

    def test_partial_success_then_conflict_leaves_applied_entries_uncollapsed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An earlier successful entry is never reset when a later entry in the same non-each plan conflicts.

        RF14: resetting entry 1 before entry 2 is attempted is exactly the per-entry-reset bug — the
        partial-progress state (real commit for entry 1, in-progress conflicted cherry-pick for entry 2)
        must match ``git cherry-pick``'s own partial-progress state, with no reset ever in flight.
        """
        recorded = _patch_git(monkeypatch, cherry_pick_rc_by_sha={"bbb": 1}, conflicted_files_out="src/foo.py\n")
        entries = [msb.PlanEntry(item_id="1", sha="aaa"), msb.PlanEntry(item_id="2", sha="bbb")]
        result = msb.run_plan(entries, msb.CommitMode.GROUPED)
        assert result["applied"] == ["1"]
        assert result["conflict"]["item_id"] == "2"
        reset_calls = [c for c in recorded if c[1] == "reset"]
        assert reset_calls == []


class TestMainCli:
    """Read a merge plan and report its conflict state through the command line."""

    @pytest.mark.parametrize(
        ("cherry_pick_rc_by_sha", "expected_rc", "expected_output"),
        [
            pytest.param({}, 0, '"applied": ["1"]', id="clean-plan-exits-0-with-applied-json"),
            pytest.param({"aaa1111": 1}, 1, '"conflict"', id="conflicting-entry-exits-1-with-conflict-json"),
        ],
    )
    def test_exit_code_and_json_follow_cherry_pick_outcome(
        self,
        tmp_path: Any,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        cherry_pick_rc_by_sha: dict[str, int],
        expected_rc: int,
        expected_output: str,
    ) -> None:
        """All entries apply cleanly → exit 0, JSON result on stdout; a conflicting entry → exit 1, conflict details."""
        _patch_git(monkeypatch, cherry_pick_rc_by_sha=cherry_pick_rc_by_sha)
        plan_file = tmp_path / "plan.json"
        plan_file.write_text('[{"item_id": "1", "sha": "aaa1111"}]', encoding="utf-8")
        rc = msb.main(["--plan", str(plan_file), "--commit-mode", "each"])
        assert rc == expected_rc
        assert expected_output in capsys.readouterr().out

    def test_base_sha_reaches_run_plan_as_reset_target(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``--base-sha`` on the CLI reaches ``run_plan`` and is used as the combined-reset target."""
        recorded = _patch_git(monkeypatch)
        plan_file = tmp_path / "plan.json"
        plan_file.write_text('[{"item_id": "1", "sha": "aaa1111"}]', encoding="utf-8")
        rc = msb.main(["--plan", str(plan_file), "--commit-mode", "stage", "--base-sha", "deadbeef"])
        assert rc == 0
        reset_calls = [c for c in recorded if c[1] == "reset"]
        assert reset_calls == [["/fake/git", "reset", "--soft", "--end-of-options", "deadbeef"]]

    @pytest.mark.parametrize(
        ("plan_sha", "mode_args"),
        [
            pytest.param("aaa1111", ["--commit-mode", "stage", "--base-sha", "not-a-sha!"], id="invalid-base-sha"),
            pytest.param("-evil", ["--commit-mode", "each"], id="invalid-sha-in-plan-file"),
        ],
    )
    def test_invalid_sha_exits_2_without_git_calls(
        self,
        tmp_path: Any,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        plan_sha: str,
        mode_args: list[str],
    ) -> None:
        """A ``--base-sha`` or a plan-file sha failing the sha-shape guard → exit 2 with a JSON error.

        No git call is attempted.
        """
        recorded = _patch_git(monkeypatch)
        plan_file = tmp_path / "plan.json"
        plan_file.write_text(f'[{{"item_id": "1", "sha": "{plan_sha}"}}]', encoding="utf-8")
        rc = msb.main(["--plan", str(plan_file), *mode_args])
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

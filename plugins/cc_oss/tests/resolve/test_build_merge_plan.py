"""Tests for ``bin/build_merge_plan.py``.

Covers the pure ``build_plan`` join logic (priority order, module resolution, omission of never-committed ids) and the
CLI's handling of missing/malformed input files.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import build_merge_plan as bmp


class TestBuildPlan:
    """build_plan: join Phase 2's commit ledger against priority order and codemap module map."""

    def test_orders_by_priority_not_commit_order(self) -> None:
        """Plan entries land in priority-order sequence, not the ledger's own (arrival) order."""
        commits = [{"item_id": 2, "sha": "bb", "group": "sw"}, {"item_id": 1, "sha": "aa", "group": "docs"}]
        items = [{"id": 1, "file": "readme.md"}, {"id": 2, "file": "core.py"}]
        plan = bmp.build_plan(commits, items, {"core.py": "pkg.core"}, ["1", "2"])
        assert [e["item_id"] for e in plan] == ["1", "2"]

    def test_resolves_module_from_action_items_file(self) -> None:
        """Each entry's ``module`` comes from the codemap file_module map, keyed by its action-item file."""
        commits = [{"item_id": 2, "sha": "bb", "group": "sw"}]
        items = [{"id": 2, "file": "core.py"}]
        plan = bmp.build_plan(commits, items, {"core.py": "pkg.core"}, ["2"])
        assert plan == [{"item_id": "2", "sha": "bb", "group": "sw", "module": "pkg.core"}]

    def test_id_absent_from_commits_is_omitted_not_emitted_empty(self) -> None:
        """A priority-order id with no matching commit (rejected/skipped) is dropped, never a placeholder entry."""
        commits = [{"item_id": 1, "sha": "aa", "group": "docs"}]
        items = [{"id": 1, "file": "readme.md"}]
        plan = bmp.build_plan(commits, items, {}, ["1", "3"])
        assert plan == [{"item_id": "1", "sha": "aa", "group": "docs", "module": ""}]

    def test_unresolved_module_defaults_to_empty_string(self) -> None:
        """A file absent from the codemap map (no index, or non-Python item) resolves to an empty module."""
        commits = [{"item_id": 1, "sha": "aa", "group": "docs"}]
        items = [{"id": 1, "file": "readme.md"}]
        plan = bmp.build_plan(commits, items, {}, ["1"])
        assert plan[0]["module"] == ""

    def test_missing_group_defaults_to_empty_string(self) -> None:
        """A commit ledger row without a ``group`` key (defensive) resolves to an empty group, not a KeyError."""
        commits = [{"item_id": 1, "sha": "aa"}]
        items = [{"id": 1, "file": "readme.md"}]
        plan = bmp.build_plan(commits, items, {}, ["1"])
        assert plan[0]["group"] == ""

    def test_duplicate_item_id_in_commits_raises(self) -> None:
        """Two commits rows sharing an item_id raise instead of the second silently overwriting the first.

        N4: a silent last-wins would drop one Phase 2 group's commit from the plan while the item still
        reads as "landed" everywhere else — the all-mode close-out flips its task regardless.
        """
        commits = [{"item_id": 1, "sha": "aa", "group": "sw"}, {"item_id": 1, "sha": "bb", "group": "docs"}]
        with pytest.raises(ValueError, match="duplicate item_id"):
            bmp.build_plan(commits, [], {}, ["1"])

    def test_commits_row_missing_sha_raises(self) -> None:
        """A commits row without ``sha`` raises a named error, not an uncaught KeyError."""
        with pytest.raises(ValueError, match="missing item_id/sha"):
            bmp.build_plan([{"item_id": 1}], [], {}, ["1"])

    def test_action_item_missing_id_raises(self) -> None:
        """An action-items row without ``id`` raises a named error, not an uncaught KeyError."""
        with pytest.raises(ValueError, match="missing id"):
            bmp.build_plan([], [{"file": "a.py"}], {}, ["1"])

    def test_duplicate_priority_order_id_deduplicated(self) -> None:
        """A repeated id in priority_order emits one plan entry, not two identical cherry-pick targets."""
        commits = [{"item_id": 1, "sha": "aa", "group": "sw"}]
        plan = bmp.build_plan(commits, [], {}, ["1", "1"])
        assert [e["item_id"] for e in plan] == ["1"]

    def test_empty_priority_order_yields_empty_plan(self) -> None:
        """No selected items → empty plan, not an error."""
        assert bmp.build_plan([], [], {}, []) == []


class TestMainCli:
    """Read the three input files and write the assembled plan."""

    def test_writes_ordered_plan_json(self, tmp_path: Path) -> None:
        """A full happy-path run writes the plan array to --out in priority order."""
        commits_file = tmp_path / "commits.jsonl"
        commits_file.write_text(
            '{"item_id": 2, "sha": "bb", "group": "sw"}\n{"item_id": 1, "sha": "aa", "group": "docs"}\n',
            encoding="utf-8",
        )
        items_file = tmp_path / "items.jsonl"
        items_file.write_text('{"id": 1, "file": "readme.md"}\n{"id": 2, "file": "core.py"}\n', encoding="utf-8")
        out_file = tmp_path / "plan.json"
        rc = bmp.main(
            [
                "--commits",
                str(commits_file),
                "--action-items",
                str(items_file),
                "--priority-order",
                "1 2",
                "--out",
                str(out_file),
            ]
        )
        assert rc == 0
        plan = json.loads(out_file.read_text(encoding="utf-8"))
        assert [e["item_id"] for e in plan] == ["1", "2"]

    def test_missing_commits_file_yields_empty_plan(self, tmp_path: Path) -> None:
        """No commits ledger (e.g. every selected item was rejected in Phase 1) → empty plan, not an error.

        Distinct from a malformed file: the prelude initializes ``phase2-commits.jsonl`` empty, so a run
        where Phase 2 never dispatched anything still has a readable (empty) file, but this path also
        covers the file being absent entirely.
        """
        items_file = tmp_path / "items.jsonl"
        items_file.write_text("", encoding="utf-8")
        out_file = tmp_path / "plan.json"
        rc = bmp.main(
            [
                "--commits",
                str(tmp_path / "missing.jsonl"),
                "--action-items",
                str(items_file),
                "--priority-order",
                "1",
                "--out",
                str(out_file),
            ]
        )
        assert rc == 0
        assert json.loads(out_file.read_text(encoding="utf-8")) == []

    def test_codemap_maps_resolves_module(self, tmp_path: Path) -> None:
        """--codemap-maps, when present, supplies the file_module map used for module resolution."""
        commits_file = tmp_path / "commits.jsonl"
        commits_file.write_text('{"item_id": 1, "sha": "aa", "group": "sw"}\n', encoding="utf-8")
        items_file = tmp_path / "items.jsonl"
        items_file.write_text('{"id": 1, "file": "core.py"}\n', encoding="utf-8")
        maps_file = tmp_path / "codemap-maps.json"
        maps_file.write_text(json.dumps({"file_module": {"core.py": "pkg.core"}, "centrality": {}}), encoding="utf-8")
        out_file = tmp_path / "plan.json"
        rc = bmp.main(
            [
                "--commits",
                str(commits_file),
                "--action-items",
                str(items_file),
                "--priority-order",
                "1",
                "--out",
                str(out_file),
                "--codemap-maps",
                str(maps_file),
            ]
        )
        assert rc == 0
        assert json.loads(out_file.read_text(encoding="utf-8"))[0]["module"] == "pkg.core"

    def test_missing_codemap_maps_file_degrades_to_empty_module(self, tmp_path: Path) -> None:
        """A --codemap-maps path that doesn't exist on disk degrades silently — every module resolves empty."""
        commits_file = tmp_path / "commits.jsonl"
        commits_file.write_text('{"item_id": 1, "sha": "aa", "group": "sw"}\n', encoding="utf-8")
        items_file = tmp_path / "items.jsonl"
        items_file.write_text('{"id": 1, "file": "core.py"}\n', encoding="utf-8")
        out_file = tmp_path / "plan.json"
        rc = bmp.main(
            [
                "--commits",
                str(commits_file),
                "--action-items",
                str(items_file),
                "--priority-order",
                "1",
                "--out",
                str(out_file),
                "--codemap-maps",
                str(tmp_path / "absent.json"),
            ]
        )
        assert rc == 0
        assert json.loads(out_file.read_text(encoding="utf-8"))[0]["module"] == ""

    def test_empty_codemap_maps_file_degrades_to_empty_module(self, tmp_path: Path) -> None:
        """A 0-byte --codemap-maps file (the NORMAL state Structural prep leaves when codemap-py is.

        absent or its query fails — the file is created unconditionally, then re-emptied on any failure) degrades to an
        empty map, exit 0, plan still written — never the raw ``json.JSONDecodeError`` traceback N2 found escaping past
        ``Path.is_file()``.
        """
        commits_file = tmp_path / "commits.jsonl"
        commits_file.write_text('{"item_id": 1, "sha": "aa", "group": "sw"}\n', encoding="utf-8")
        items_file = tmp_path / "items.jsonl"
        items_file.write_text('{"id": 1, "file": "core.py"}\n', encoding="utf-8")
        maps_file = tmp_path / "codemap-maps.json"
        maps_file.write_text("", encoding="utf-8")
        out_file = tmp_path / "plan.json"
        rc = bmp.main(
            [
                "--commits",
                str(commits_file),
                "--action-items",
                str(items_file),
                "--priority-order",
                "1",
                "--out",
                str(out_file),
                "--codemap-maps",
                str(maps_file),
            ]
        )
        assert rc == 0
        assert json.loads(out_file.read_text(encoding="utf-8"))[0]["module"] == ""

    def test_codemap_maps_non_dict_top_level_degrades_to_empty_module(self, tmp_path: Path) -> None:
        """A --codemap-maps file whose top-level JSON value isn't an object (e.g. a bare array) degrades safely.

        F8: ``raw.get("file_module", {})`` would raise ``AttributeError`` on a list — the pre-fix code
        assumed every valid-JSON payload was a dict. Exercises the shape guard rather than the
        OSError/JSONDecodeError path already covered by the empty-file and missing-file cases above.
        """
        commits_file = tmp_path / "commits.jsonl"
        commits_file.write_text('{"item_id": 1, "sha": "aa", "group": "sw"}\n', encoding="utf-8")
        items_file = tmp_path / "items.jsonl"
        items_file.write_text('{"id": 1, "file": "core.py"}\n', encoding="utf-8")
        maps_file = tmp_path / "codemap-maps.json"
        maps_file.write_text("[]", encoding="utf-8")
        out_file = tmp_path / "plan.json"
        rc = bmp.main(
            [
                "--commits",
                str(commits_file),
                "--action-items",
                str(items_file),
                "--priority-order",
                "1",
                "--out",
                str(out_file),
                "--codemap-maps",
                str(maps_file),
            ]
        )
        assert rc == 0
        assert json.loads(out_file.read_text(encoding="utf-8"))[0]["module"] == ""

    def test_codemap_maps_non_dict_file_module_degrades_to_empty_module(self, tmp_path: Path) -> None:
        """A --codemap-maps file whose ``file_module`` value isn't an object degrades safely.

        F8: a valid top-level dict with a wrong-shaped ``file_module`` (e.g. a string) would otherwise
        propagate a non-dict into ``build_plan``'s ``file_module.get(...)`` call and raise.
        """
        commits_file = tmp_path / "commits.jsonl"
        commits_file.write_text('{"item_id": 1, "sha": "aa", "group": "sw"}\n', encoding="utf-8")
        items_file = tmp_path / "items.jsonl"
        items_file.write_text('{"id": 1, "file": "core.py"}\n', encoding="utf-8")
        maps_file = tmp_path / "codemap-maps.json"
        maps_file.write_text('{"file_module": "not-a-dict"}', encoding="utf-8")
        out_file = tmp_path / "plan.json"
        rc = bmp.main(
            [
                "--commits",
                str(commits_file),
                "--action-items",
                str(items_file),
                "--priority-order",
                "1",
                "--out",
                str(out_file),
                "--codemap-maps",
                str(maps_file),
            ]
        )
        assert rc == 0
        assert json.loads(out_file.read_text(encoding="utf-8"))[0]["module"] == ""

    def test_ledger_row_outside_priority_order_warns_on_stderr(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A commits-ledger row whose item_id never appears in priority-order prints a stderr warning.

        F9: such a row has no path to the plan (build_plan only ever iterates priority_order), so it
        silently vanishes with no trace unless something calls it out. The plan itself still omits it —
        this only adds observability, not a behavior change.
        """
        commits_file = tmp_path / "commits.jsonl"
        commits_file.write_text(
            '{"item_id": 1, "sha": "aa", "group": "sw"}\n{"item_id": 99, "sha": "zz", "group": "sw"}\n',
            encoding="utf-8",
        )
        items_file = tmp_path / "items.jsonl"
        items_file.write_text('{"id": 1, "file": "readme.md"}\n', encoding="utf-8")
        out_file = tmp_path / "plan.json"
        rc = bmp.main(
            [
                "--commits",
                str(commits_file),
                "--action-items",
                str(items_file),
                "--priority-order",
                "1",
                "--out",
                str(out_file),
            ]
        )
        assert rc == 0
        assert [e["item_id"] for e in json.loads(out_file.read_text(encoding="utf-8"))] == ["1"]
        assert "dropped" in capsys.readouterr().err

    def test_all_ledger_rows_in_priority_order_no_warning(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """No stderr warning fires when every commits-ledger row is covered by priority-order."""
        commits_file = tmp_path / "commits.jsonl"
        commits_file.write_text('{"item_id": 1, "sha": "aa", "group": "sw"}\n', encoding="utf-8")
        items_file = tmp_path / "items.jsonl"
        items_file.write_text('{"id": 1, "file": "readme.md"}\n', encoding="utf-8")
        out_file = tmp_path / "plan.json"
        rc = bmp.main(
            [
                "--commits",
                str(commits_file),
                "--action-items",
                str(items_file),
                "--priority-order",
                "1",
                "--out",
                str(out_file),
            ]
        )
        assert rc == 0
        assert capsys.readouterr().err == ""

    def test_malformed_commits_jsonl_exits_1(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A commits ledger line that isn't valid JSON → exit 1, error on stderr, nothing written."""
        commits_file = tmp_path / "commits.jsonl"
        commits_file.write_text("not json\n", encoding="utf-8")
        items_file = tmp_path / "items.jsonl"
        items_file.write_text("", encoding="utf-8")
        out_file = tmp_path / "plan.json"
        rc = bmp.main(
            [
                "--commits",
                str(commits_file),
                "--action-items",
                str(items_file),
                "--priority-order",
                "1",
                "--out",
                str(out_file),
            ]
        )
        assert rc == 1
        assert "failed to read input" in capsys.readouterr().err
        assert not out_file.exists()

    def test_missing_required_args_exits_2(self) -> None:
        """A required flag omitted → argparse exits 2."""
        with pytest.raises(SystemExit) as exc:
            bmp.main([])
        assert exc.value.code == 2

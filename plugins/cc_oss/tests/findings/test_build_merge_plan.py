"""Tests for ``bin/build_merge_plan.py``.

Covers the pure ``build_plan`` join logic (priority order, module resolution, omission of never-committed ids) and the
CLI's handling of missing/malformed input files. Nothing is mocked: the script only reads and writes small files below
``tmp_path``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import build_merge_plan as bmp
import pytest

_COMMIT = {"item_id": 1, "sha": "aa", "group": "sw"}
_ITEM = {"id": 1, "file": "core.py"}


def _jsonl(*rows: dict[str, object]) -> str:
    """Serialise ``rows`` as JSON Lines text."""
    return "".join(json.dumps(row) + "\n" for row in rows)


@pytest.fixture
def cli(tmp_path: Path) -> Callable[..., list[str]]:
    """Return a function that writes the ledger and action-items files and builds the CLI argument list.

    ``commits`` and ``items`` are the raw file texts (default: one ledger commit for item 1 and its action item);
    ``extra`` flags are appended after ``--out``, which always targets ``tmp_path / "plan.json"``.
    """

    def build(*extra: str, commits: str = _jsonl(_COMMIT), items: str = _jsonl(_ITEM)) -> list[str]:
        """Write the input files and return the argument list for ``bmp.main``."""
        (tmp_path / "commits.jsonl").write_text(commits, encoding="utf-8")
        (tmp_path / "items.jsonl").write_text(items, encoding="utf-8")
        return [
            *("--commits", str(tmp_path / "commits.jsonl")),
            *("--action-items", str(tmp_path / "items.jsonl")),
            *("--out", str(tmp_path / "plan.json")),
            *extra,
        ]

    return build


def _plan(tmp_path: Path) -> list[dict[str, object]]:
    """Read the plan the CLI wrote."""
    return json.loads((tmp_path / "plan.json").read_text(encoding="utf-8"))


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

    @pytest.mark.parametrize(
        ("commits", "field"),
        [
            pytest.param([{"item_id": 1, "sha": "aa", "group": "docs"}], "module", id="unresolved-module"),
            pytest.param([{"item_id": 1, "sha": "aa"}], "group", id="missing-group"),
        ],
    )
    def test_unresolved_module_or_missing_group_defaults_to_empty_string(
        self, commits: list[dict[str, object]], field: str
    ) -> None:
        """An unresolvable module or an absent group resolves to an empty string, not a KeyError.

        A file absent from the codemap map (no index, or non-Python item) resolves to an empty module; a commit ledger
        row without a ``group`` key (defensive) resolves to an empty group.
        """
        items = [{"id": 1, "file": "readme.md"}]

        plan = bmp.build_plan(commits, items, {}, ["1"])

        assert plan[0][field] == ""

    def test_duplicate_item_id_in_commits_raises(self) -> None:
        """Two commits rows sharing an item_id raise instead of the second silently overwriting the first.

        A silent last-wins would drop one Phase 2 group's commit from the plan while the item still reads as "landed"
        everywhere else — the all-mode close-out flips its task regardless.
        """
        commits = [{"item_id": 1, "sha": "aa", "group": "sw"}, {"item_id": 1, "sha": "bb", "group": "docs"}]

        with pytest.raises(ValueError, match="duplicate item_id"):
            bmp.build_plan(commits, [], {}, ["1"])

    @pytest.mark.parametrize(
        ("commits", "items", "message"),
        [
            pytest.param([{"item_id": 1}], [], "missing item_id/sha", id="commits-row-missing-sha"),
            pytest.param([], [{"file": "a.py"}], "missing id", id="action-item-missing-id"),
        ],
    )
    def test_malformed_input_row_raises_named_error(
        self, commits: list[dict[str, object]], items: list[dict[str, object]], message: str
    ) -> None:
        """A commits row without ``sha`` or an action-items row without ``id`` raises a named error.

        The failure is a ``ValueError`` naming the missing field, never an uncaught ``KeyError``.
        """
        with pytest.raises(ValueError, match=message):
            bmp.build_plan(commits, items, {}, ["1"])

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

    def test_writes_ordered_plan_json(self, tmp_path: Path, cli: Callable[..., list[str]]) -> None:
        """A full happy-path run writes the plan array to --out in priority order.

        The ledger lists item 2 before item 1; the plan follows ``--priority-order``, not the ledger.
        """
        commits = _jsonl({"item_id": 2, "sha": "bb", "group": "sw"}, {"item_id": 1, "sha": "aa", "group": "docs"})
        items = _jsonl({"id": 1, "file": "readme.md"}, {"id": 2, "file": "core.py"})

        rc = bmp.main(cli("--priority-order", "1 2", commits=commits, items=items))

        assert rc == 0
        assert [e["item_id"] for e in _plan(tmp_path)] == ["1", "2"]

    def test_missing_commits_file_yields_empty_plan(self, tmp_path: Path, cli: Callable[..., list[str]]) -> None:
        """No commits ledger (e.g. every selected item was rejected in Phase 1) → empty plan, not an error.

        Distinct from a malformed file: the prelude initializes ``phase2-commits.jsonl`` empty, so a run where Phase 2
        never dispatched anything still has a readable (empty) file, but this path also covers the file being absent
        entirely.
        """
        args = cli("--priority-order", "1", items="")
        (tmp_path / "commits.jsonl").unlink()

        rc = bmp.main(args)

        assert rc == 0
        assert _plan(tmp_path) == []

    @pytest.mark.parametrize(
        ("maps_content", "module"),
        [
            pytest.param(
                json.dumps({"file_module": {"core.py": "pkg.core"}, "centrality": {}}), "pkg.core", id="valid"
            ),
            pytest.param("", "", id="empty-file"),
            pytest.param("[]", "", id="non-dict-top-level"),
            pytest.param('{"file_module": "not-a-dict"}', "", id="non-dict-file-module"),
        ],
    )
    def test_codemap_maps_file_supplies_module(
        self, tmp_path: Path, cli: Callable[..., list[str]], maps_content: str, module: str
    ) -> None:
        """--codemap-maps supplies the file_module map; an empty or wrong-shaped file degrades to an empty module.

        A 0-byte file is the NORMAL state Structural prep leaves when codemap-py is absent or its query fails (the file
        is created unconditionally, then re-emptied on any failure); it must never surface as a raw
        ``json.JSONDecodeError`` traceback. A top-level JSON value that is not an object (a bare array) or a
        ``file_module`` value that is not an object (a string) would otherwise raise in ``raw.get("file_module", {})``
        or ``file_module.get(...)``, so the shape guard degrades them too: exit 0, plan still written.
        """
        maps_file = tmp_path / "codemap-maps.json"
        maps_file.write_text(maps_content, encoding="utf-8")

        rc = bmp.main(cli("--priority-order", "1", "--codemap-maps", str(maps_file)))

        assert rc == 0
        assert _plan(tmp_path)[0]["module"] == module

    def test_missing_codemap_maps_file_degrades_to_empty_module(
        self, tmp_path: Path, cli: Callable[..., list[str]]
    ) -> None:
        """A --codemap-maps path that doesn't exist on disk degrades silently — every module resolves empty."""
        rc = bmp.main(cli("--priority-order", "1", "--codemap-maps", str(tmp_path / "absent.json")))

        assert rc == 0
        assert _plan(tmp_path)[0]["module"] == ""

    @pytest.mark.parametrize(
        ("remaining", "expected_ids"),
        [
            pytest.param(["3", "1"], ["3", "1"], id="remaining-subset-keeps-result-order"),
            pytest.param([], [], id="empty-remaining-writes-empty-plan"),
            pytest.param([3], ["3"], id="integer-ids-coerced"),
        ],
    )
    def test_resume_from_uses_remaining_as_priority_order(
        self, tmp_path: Path, cli: Callable[..., list[str]], remaining: list[object], expected_ids: list[str]
    ) -> None:
        """--resume-from rebuilds the plan from merge-result.json's ``remaining`` ids, in that order.

        After a cherry-pick conflict the resumed plan must hold only the still-unapplied entries, so an applied item
        that is also in the full ledger is left out, and an empty ``remaining`` still writes a valid empty plan.
        """
        commits = _jsonl(*({"item_id": i, "sha": f"s{i}", "group": "sw"} for i in (1, 2, 3)))
        items = _jsonl(*({"id": i, "file": f"f{i}.py"} for i in (1, 2, 3)))
        result_file = tmp_path / "merge-result.json"
        result_file.write_text(
            json.dumps({"applied": ["2"], "conflict": "3", "remaining": remaining}), encoding="utf-8"
        )

        rc = bmp.main(cli("--resume-from", str(result_file), commits=commits, items=items))

        assert rc == 0
        assert [e["item_id"] for e in _plan(tmp_path)] == expected_ids

    @pytest.mark.parametrize(
        "result_text",
        [
            pytest.param("{not json", id="malformed-json"),
            pytest.param('{"applied": []}', id="no-remaining-key"),
            pytest.param('{"remaining": "3 1"}', id="remaining-not-a-list"),
            pytest.param("[1, 2]", id="result-not-an-object"),
        ],
    )
    def test_resume_from_unusable_result_fails_without_writing_a_plan(
        self, tmp_path: Path, cli: Callable[..., list[str]], result_text: str
    ) -> None:
        """A malformed result file stops the resume with the dedicated exit 3 and leaves no plan to be merged.

        A silently empty plan here would let the resumed merge fence collapse a branch while skipping every unapplied
        entry, which is the failure the old inline reader guarded with its own BLOCKED line.
        """
        result_file = tmp_path / "merge-result.json"
        result_file.write_text(result_text, encoding="utf-8")

        rc = bmp.main(cli("--resume-from", str(result_file)))

        assert (rc, (tmp_path / "plan.json").exists()) == (bmp.EXIT_RESUME_UNUSABLE, False)

    def test_resume_from_absent_result_file_fails_without_writing_a_plan(
        self, tmp_path: Path, cli: Callable[..., list[str]]
    ) -> None:
        """A merge-result.json that was never written (the merge fence did not run) is exit 3, not an empty plan."""
        rc = bmp.main(cli("--resume-from", str(tmp_path / "merge-result.json")))

        assert (rc, (tmp_path / "plan.json").exists()) == (bmp.EXIT_RESUME_UNUSABLE, False)

    @pytest.mark.parametrize(
        "args",
        [
            pytest.param([], id="no-arguments"),
            pytest.param(
                ["--commits", "c.jsonl", "--action-items", "i.jsonl", "--out", "plan.json"], id="neither-order-source"
            ),
            pytest.param(
                [
                    *("--commits", "c.jsonl", "--action-items", "i.jsonl", "--out", "plan.json"),
                    *("--priority-order", "1", "--resume-from", "merge-result.json"),
                ],
                id="both-order-sources",
            ),
        ],
    )
    def test_usage_error_exits_2(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, args: list[str]) -> None:
        """A missing required flag, or neither/both of --priority-order and --resume-from, is an argparse error (exit
        2).

        The relative paths are never opened because parsing fails first; the run happens in ``tmp_path`` regardless.
        """
        monkeypatch.chdir(tmp_path)

        with pytest.raises(SystemExit) as excinfo:
            bmp.main(args)

        assert excinfo.value.code == 2

    @pytest.mark.parametrize(
        ("commits", "warns"),
        [
            pytest.param(
                _jsonl(_COMMIT, {"item_id": 99, "sha": "zz", "group": "sw"}), True, id="row-outside-priority-order"
            ),
            pytest.param(_jsonl(_COMMIT), False, id="all-rows-covered"),
        ],
    )
    def test_warns_on_stderr_only_for_ledger_rows_outside_priority_order(
        self,
        tmp_path: Path,
        cli: Callable[..., list[str]],
        capsys: pytest.CaptureFixture[str],
        commits: str,
        warns: bool,
    ) -> None:
        """A commits-ledger row whose item_id never appears in priority-order prints a stderr warning, others stay
        quiet.

        Such a row has no path to the plan (build_plan only ever iterates priority_order), so it silently vanishes with
        no trace unless something calls it out. The plan itself still omits it — this only adds observability, not a
        behavior change — and a fully covered ledger prints nothing.
        """
        rc = bmp.main(cli("--priority-order", "1", commits=commits))

        err = capsys.readouterr().err
        assert (rc, [e["item_id"] for e in _plan(tmp_path)], err != "", "dropped" in err) == (0, ["1"], warns, warns)

    def test_malformed_commits_jsonl_exits_1(
        self, tmp_path: Path, cli: Callable[..., list[str]], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A commits ledger line that isn't valid JSON → exit 1, error on stderr, nothing written."""
        rc = bmp.main(cli("--priority-order", "1", commits="not json\n", items=""))

        assert (rc, (tmp_path / "plan.json").exists()) == (1, False)
        assert "failed to read input" in capsys.readouterr().err

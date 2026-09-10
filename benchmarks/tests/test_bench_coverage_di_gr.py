"""Tests for the diff-impact (DI) and graph (GR) benchmark coverage extension.

Covers:
  - Task schema for the new DI/GR series in tasks-bench.json.
  - Generator validators / oracles for diff_impact, graph_central, graph_path, graph_fn_blast
    on tiny fixture repos (deterministic AST — no scan-query, no LLM).
  - DiffImpactStager stage/revert safety, including dirty-tree refusal.
  - Batch counter parsing + used_batch field.
  - The new runner evaluators (caller recall, set overlap, ordered path, transitive recall).

No LLM calls, no benchmark execution — every test is pure AST / regex / git-porcelain.
"""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

SUITE = Path(__file__).resolve().parent.parent / "suites" / "tasks-bench.json"
TASK_ROWS: tuple[dict[str, Any], ...] = tuple(json.loads(SUITE.read_text(encoding="utf-8"))["tasks"])
_MB_TASK_CASES = tuple(pytest.param(task, id=task["id"]) for task in TASK_ROWS if task["id"].startswith("MB-"))
_DI_TASK_CASES = tuple(pytest.param(task, id=task["id"]) for task in TASK_ROWS if task["id"].startswith("DI-"))
_GRAPH_PATH_TASK_CASES = tuple(pytest.param(task, id=task["id"]) for task in TASK_ROWS if task["type"] == "graph_path")
_DI_GR_TASK_CASES = tuple(
    pytest.param(task, id=task["id"]) for task in TASK_ROWS if task["id"].startswith(("DI-", "GR-"))
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(name="mini_repo")
def _mini_repo(tmp_path: Path) -> Path:
    """Create a small repository for oracle unit tests.

    Graph: c imports b imports a; b.caller calls a.target; tests/test_a.py imports+calls a.

    >>> root = getfixture("mini_repo")
    >>> (root / "c.py").read_text().strip(), (root / "tests/test_a.py").is_file()
    ('import b', True)
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def target():\n    pass\n")
    (repo / "b.py").write_text("import a\n\n\ndef caller():\n    a.target()\n")
    (repo / "c.py").write_text("import b\n")
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_a.py").write_text("import a\n\n\ndef test_it():\n    a.target()\n")
    return repo


@pytest.fixture(name="git_repo")
def _git_repo(tmp_path: Path) -> Path:
    """Create a committed repository for staging and restoration tests."""
    repo = tmp_path / "gitrepo"
    repo.mkdir()
    (repo / "src.py").write_text("def widely_used():\n    return 1\n")
    for argv in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@t.t"],
        ["git", "config", "user.name", "t"],
        ["git", "add", "src.py"],
        ["git", "commit", "-q", "-m", "init"],
    ):
        subprocess.run(argv, cwd=repo, check=True, capture_output=True)
    return repo


# ---------------------------------------------------------------------------
# Task schema
# ---------------------------------------------------------------------------


class TestDiGrTaskSchema:
    """Schema invariants for the new DI/GR tasks in tasks-bench.json."""

    @pytest.fixture(name="tasks", scope="class")
    @staticmethod
    def _tasks() -> list[dict[str, Any]]:
        """Provide the task collection used by this selection or coverage scenario."""
        return list(TASK_ROWS)

    def test_suite_is_valid_json_object(self) -> None:
        """tasks-bench.json is an object with repo header + tasks list."""
        data = json.loads(SUITE.read_text())
        assert set(data) >= {"repo", "tasks"}
        assert isinstance(data["tasks"], list)

    def test_has_six_di_tasks(self, tasks: list[dict[str, Any]]) -> None:
        """DI roster contains exactly six diff-impact records."""
        di = [t for t in tasks if t["id"].startswith("DI-")]
        assert len(di) == 6
        assert all(t["type"] == "diff_impact" for t in di)

    def test_has_four_gr_tasks(self, tasks: list[dict[str, Any]]) -> None:
        """GR roster contains declared graph types and its graph-path record."""
        gr = [t for t in tasks if t["id"].startswith("GR-")]
        assert len(gr) == 4
        assert {t["type"] for t in gr} == {"graph_central", "graph_path", "graph_fn_blast"}
        assert [task["id"] for task in gr if task["type"] == "graph_path"] == ["GR-02"]

    def test_has_five_mb_tasks(self, tasks: list[dict[str, Any]]) -> None:
        """MB roster contains exactly five module-blast-radius records."""
        mb = [t for t in tasks if t["id"].startswith("MB-")]
        assert len(mb) == 5
        assert all(t["type"] == "module_blast_radius" for t in mb)

    @pytest.mark.parametrize("task", _MB_TASK_CASES)
    def test_mb_tasks_have_materialized_importer_gt(self, task: dict[str, Any]) -> None:
        """MB tasks ship materialized (non-pending, non-empty) importer ground truth."""
        gt = task["ground_truth"]
        assert gt.get("gt_pending") is False, task["id"]
        assert gt["importers"], task["id"]
        assert gt["importer_count"] == len(gt["importers"]), task["id"]
        assert task.get("workflow_type") == "query", task["id"]
        assert task["primary_module"], task["id"]
        assert not any(module.startswith("tests.") for module in gt["importers"]), task["id"]

    @pytest.mark.parametrize("task", _DI_TASK_CASES)
    def test_di_tasks_have_stage_spec_and_primary_fn(self, task: dict[str, Any]) -> None:
        """Each DI task exposes a callable target and executable stage specification."""
        assert "::" in task["primary_fn"], task["id"]
        assert isinstance(task.get("stage"), list) and task["stage"], task["id"]
        for edit in task["stage"]:
            assert edit.get("file"), task["id"]
            assert ("append" in edit) or ("find" in edit and "replace" in edit), task["id"]

    def test_di_gr_tasks_have_materialized_gt(self, tasks: list[dict]) -> None:
        """The locked target materializes every DI/GR independent-oracle answer."""
        new = [t for t in tasks if t["id"].startswith(("DI-", "GR-"))]
        assert new
        assert all(t["ground_truth"].get("gt_pending") is False for t in new)

    @pytest.mark.parametrize("task", _GRAPH_PATH_TASK_CASES)
    def test_graph_path_task_declares_source_and_target(self, task: dict[str, Any]) -> None:
        """Each graph-path task declares both ground-truth endpoints."""
        assert task["ground_truth"]["source"]
        assert task["ground_truth"]["target"]

    @pytest.mark.parametrize("task", _DI_GR_TASK_CASES)
    def test_di_gr_tasks_are_scoreable(self, task: dict[str, Any]) -> None:
        """Each DI/GR task is eligible for benchmark scoring."""
        assert task.get("scoreable") is True, task["id"]


# ---------------------------------------------------------------------------
# Generator validators / oracles (tiny fixture repos)
# ---------------------------------------------------------------------------


class TestDiffImpactValidator:
    """_validate_diff_impact computes AST caller + test-import GT independent of scan-query."""

    def test_computes_callers_and_test_modules(self, script_gen_bench: Any, mini_repo: Path) -> None:
        task = {
            "type": "diff_impact",
            "id": "DI-x",
            "primary_fn": "a::target",
            "primary_module": "a",
            "ground_truth": {"gt_pending": True, "fn_callers": [], "test_modules": []},
        }
        ok, live, reason = script_gen_bench._validate_diff_impact(task, None, None, mini_repo)
        assert ok is False  # gt_pending → not-ok, but oracle GT computed
        assert live["fn_callers"] == ["b::caller"]
        assert live["test_modules"] == ["tests.test_a"]
        assert live["gt_pending"] is False
        assert "computed" in reason

    def test_pending_flag_cleared_when_gt_matches(self, script_gen_bench: Any, mini_repo: Path) -> None:
        """Once GT is populated (gt_pending cleared), a matching task validates ok."""
        task = {
            "type": "diff_impact",
            "id": "DI-x",
            "primary_fn": "a::target",
            "primary_module": "a",
            "ground_truth": {
                "gt_pending": False,
                "fn_callers": ["b::caller"],
                "test_modules": ["tests.test_a"],
            },
        }
        ok, _live, reason = script_gen_bench._validate_diff_impact(task, None, None, mini_repo)
        assert ok is True, reason

    def test_rejects_malformed_primary_fn(self, script_gen_bench: Any, mini_repo: Path) -> None:
        task = {"type": "diff_impact", "id": "DI-x", "primary_fn": "no_colons", "ground_truth": {}}
        ok, live, reason = script_gen_bench._validate_diff_impact(task, None, None, mini_repo)
        assert ok is False and live is None and "primary_fn" in reason


class TestGraphValidators:
    """graph_central / graph_path / graph_fn_blast validators on a tiny fixture repo."""

    def test_central_ranks_by_in_degree(self, script_gen_bench: Any, mini_repo: Path) -> None:
        task = {"type": "graph_central", "id": "GR-x", "ground_truth": {"gt_pending": True, "top": 2}}
        ok, live, _reason = script_gen_bench._validate_graph_central(task, None, None, mini_repo)
        assert ok is False  # pending
        # a is imported by b; b is imported by c → both have in-degree 1, c has 0.
        assert set(live["central_modules"]) == {"a", "b"}

    def test_path_returns_unique_chain(self, script_gen_bench: Any, mini_repo: Path) -> None:
        task = {
            "type": "graph_path",
            "id": "GR-p",
            "ground_truth": {"gt_pending": True, "source": "c", "target": "a"},
        }
        ok, live, _reason = script_gen_bench._validate_graph_path(task, None, None, mini_repo)
        assert ok is False  # pending
        assert live["import_path"] == ["c", "b", "a"]
        assert live["path_is_unique"] is True

    def test_path_requires_source_and_target(self, script_gen_bench: Any, mini_repo: Path) -> None:
        task = {"type": "graph_path", "id": "GR-p", "ground_truth": {"gt_pending": True}}
        ok, live, reason = script_gen_bench._validate_graph_path(task, None, None, mini_repo)
        assert ok is False and live is None and "source" in reason

    def test_fn_blast_depth2_closure(self, script_gen_bench: Any, mini_repo: Path) -> None:
        task = {
            "type": "graph_fn_blast",
            "id": "GR-fb",
            "primary_fn": "a::target",
            "ground_truth": {"gt_pending": True, "depth": 2},
        }
        ok, live, _reason = script_gen_bench._validate_graph_fn_blast(task, None, None, mini_repo)
        assert ok is False  # pending
        assert live["blast_callers"] == ["b::caller"]


class TestImportGraphPrimitives:
    """AST import-graph oracle primitives — GT-critical, must mirror scan-index semantics.

    ``_module_imports`` and ``_build_import_graph`` feed every graph_central / graph_path / graph_fn_blast ground truth.
    They intentionally reproduce scan-index ``extract_imports`` (scanner.py:723) rather than resolve relatives properly,
    so the AST oracle agrees with what scan-query returns for the same repo. These lock that contract before any
    refactor.
    """

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            pytest.param("import a.b\n", {"a.b"}, id="import-dotted"),
            pytest.param("import a.b as c\n", {"a.b"}, id="import-alias-keeps-real-name"),
            pytest.param("from c.d import e\n", {"c.d"}, id="from-import-module"),
            pytest.param("import a\nimport b\n", {"a", "b"}, id="two-imports"),
            pytest.param("from . import f\n", set(), id="bare-relative-skipped"),
            pytest.param("from .foo import x\n", {"foo"}, id="level1-relative-kept-as-absolute"),
            pytest.param("from ..pkg.sub import y\n", {"pkg.sub"}, id="multidot-relative-level-ignored"),
        ],
    )
    def test_module_imports_mirrors_scan_index(self, script_gen_bench: Any, source: str, expected: set[str]) -> None:
        """_module_imports collects import targets on node.module truthiness, ignoring node.level."""
        assert script_gen_bench._module_imports(ast.parse(source)) == expected

    def test_module_imports_not_interchangeable_with_shared_helper(
        self, script_gen_bench: Any, script_python_source: Any
    ) -> None:
        """Guard: the shared helper resolves relatives and so DROPS multi-dot targets the oracle keeps.

        Pins why _module_imports cannot be replaced by python_source.extract_import_targets — swapping would silently
        diverge the AST oracle from scan-index on any multi-dot relative import.
        """
        tree = ast.parse("from ..pkg import x\n")
        assert script_gen_bench._module_imports(tree) == {"pkg"}
        shared = script_python_source.extract_import_targets(tree, package="", credit_submodules=False)
        assert shared == set()
        assert script_gen_bench._module_imports(tree) != shared

    def test_build_import_graph_edges_and_test_exclusion(self, script_gen_bench: Any, mini_repo: Path) -> None:
        """Edges point to in-repo import targets; test modules are excluded by default."""
        graph = script_gen_bench._build_import_graph(mini_repo)
        assert graph["a"] == set()
        assert graph["b"] == {"a"}
        assert graph["c"] == {"b"}
        assert not any("test_a" in key for key in graph)

    def test_build_import_graph_drops_external_targets(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Stdlib / third-party import targets are filtered out — only in-repo modules remain as edges."""
        repo = tmp_path / "extrepo"
        repo.mkdir()
        (repo / "m.py").write_text("import os\nimport json\nimport a\n")
        (repo / "a.py").write_text("x = 1\n")
        graph = script_gen_bench._build_import_graph(repo)
        assert graph["m"] == {"a"}

    def test_build_import_graph_include_tests(self, script_gen_bench: Any, mini_repo: Path) -> None:
        """Keep test modules as graph nodes when test exclusion is disabled."""
        graph = script_gen_bench._build_import_graph(mini_repo, exclude_tests=False)
        assert any("test_a" in key for key in graph)


class TestGtPending:
    """gt_is_pending helper + oracle-backed registration."""

    def testgt_is_pending_true_false(self, script_gen_bench: Any) -> None:
        assert script_gen_bench.gt_is_pending({"ground_truth": {"gt_pending": True}}) is True
        assert script_gen_bench.gt_is_pending({"ground_truth": {"gt_pending": False}}) is False
        assert script_gen_bench.gt_is_pending({"ground_truth": {}}) is False

    @pytest.mark.parametrize(
        "ttype", ("diff_impact", "graph_central", "graph_path", "graph_fn_blast", "module_blast_radius")
    )
    def test_new_types_are_oracle_backed(self, script_gen_bench: Any, ttype: str) -> None:
        """Each new benchmark task type uses an oracle-backed update path."""
        assert script_gen_bench._update_is_oracle_backed({"type": ttype}) is True

    def test_new_types_registered_in_validators(self, script_gen_bench: Any) -> None:
        for ttype in ("diff_impact", "graph_central", "graph_path", "graph_fn_blast", "module_blast_radius"):
            assert ttype in script_gen_bench.VALIDATORS


# ---------------------------------------------------------------------------
# DiffImpactStager stage / revert / dirty-tree refusal
# ---------------------------------------------------------------------------


class TestDiffImpactStager:
    """Stage-then-revert safety, including dirty-tree refusal."""

    def _spec(self) -> list[dict]:
        """Describe a signature edit that introduces a new optional parameter."""
        return [
            {"file": "src.py", "find": "def widely_used(", "replace": "def widely_used(new_arg=None,  # staged\n    "}
        ]

    def test_stage_applies_and_revert_restores(self, script_run_bench: Any, git_repo: Path) -> None:
        original = (git_repo / "src.py").read_text()
        stager = script_run_bench.DiffImpactStager(git_repo, self._spec())
        with stager:
            staged = (git_repo / "src.py").read_text()
            assert "new_arg=None" in staged
        # On block exit the change is reverted via git checkout --.
        assert (git_repo / "src.py").read_text() == original

    def test_revert_runs_even_on_exception(self, script_run_bench: Any, git_repo: Path) -> None:
        original = (git_repo / "src.py").read_text()
        stager = script_run_bench.DiffImpactStager(git_repo, self._spec())
        with pytest.raises(RuntimeError):
            with stager:
                assert "new_arg=None" in (git_repo / "src.py").read_text()
                raise RuntimeError("arm blew up mid-task")
        assert (git_repo / "src.py").read_text() == original

    def test_refuses_dirty_tree(self, script_run_bench: Any, git_repo: Path) -> None:
        # Dirty the staged path BEFORE staging — the stager must refuse.
        (git_repo / "src.py").write_text("def widely_used():\n    return 999  # user edit\n")
        stager = script_run_bench.DiffImpactStager(git_repo, self._spec())
        with pytest.raises(script_run_bench.DirtyTreeError):
            stager.__enter__()

    def test_refuses_dirty_tree_leaves_user_edit_intact(self, script_run_bench: Any, git_repo: Path) -> None:
        dirty = "def widely_used():\n    return 999  # user edit\n"
        (git_repo / "src.py").write_text(dirty)
        stager = script_run_bench.DiffImpactStager(git_repo, self._spec())
        with pytest.raises(script_run_bench.DirtyTreeError):
            stager.__enter__()
        # Refusal must not have touched or reverted the user's own edit.
        assert (git_repo / "src.py").read_text() == dirty

    def test_missing_find_text_aborts(self, script_run_bench: Any, git_repo: Path) -> None:
        bad = [{"file": "src.py", "find": "NONEXISTENT_TOKEN", "replace": "x"}]
        stager = script_run_bench.DiffImpactStager(git_repo, bad)
        with pytest.raises(script_run_bench.DirtyTreeError):
            stager.__enter__()
        stager.revert()  # best-effort cleanup

    def test_append_edit_form(self, script_run_bench: Any, git_repo: Path) -> None:
        original = (git_repo / "src.py").read_text()
        stager = script_run_bench.DiffImpactStager(git_repo, [{"file": "src.py", "append": "\n# appended\n"}])
        with stager:
            assert (git_repo / "src.py").read_text().endswith("# appended\n")
        assert (git_repo / "src.py").read_text() == original


# ---------------------------------------------------------------------------
# Batch counter parsing + used_batch
# ---------------------------------------------------------------------------


class TestBatchParsing:
    """_parse_batch_subcommands + used_batch counter wiring."""

    def test_parses_inner_cmds(self, script_run_bench: Any) -> None:
        cmd = 'scan-query batch <<< \'[{"cmd": "fn-rdeps", "args": ["m::f"]}, {"cmd": "rdeps", "args": ["m"]}]\''
        assert script_run_bench._parse_batch_subcommands(cmd) == ["fn-rdeps", "rdeps"]

    def test_non_batch_returns_empty(self, script_run_bench: Any) -> None:
        assert script_run_bench._parse_batch_subcommands("scan-query symbol Trainer") == []

    def test_unknown_inner_cmd_dropped(self, script_run_bench: Any) -> None:
        assert script_run_bench._parse_batch_subcommands('scan-query batch <<< \'[{"cmd": "bogus"}]\'') == []

    def test_malformed_json_returns_empty(self, script_run_bench: Any) -> None:
        assert script_run_bench._parse_batch_subcommands("scan-query batch <<< '[not json'") == []

    def test_batch_and_diff_impact_are_subcommands(self, script_run_bench: Any) -> None:
        assert "batch" in script_run_bench._SCAN_QUERY_SUBCOMMANDS
        assert "diff-impact" in script_run_bench._SCAN_QUERY_SUBCOMMANDS
        assert script_run_bench._parse_scan_query_subcommand("scan-query diff-impact --base HEAD~1") == "diff-impact"

    def test_record_tool_use_sets_used_batch_and_attributes_inner(self, script_run_bench: Any) -> None:
        run = script_run_bench.BenchRun(
            arm="codemap", task_id="X", task_type="diff_impact", model="haiku", success=False
        )
        cmd = 'scan-query batch <<< \'[{"cmd": "fn-rdeps", "args": ["m::f"]}, {"cmd": "rdeps", "args": ["m"]}]\''
        script_run_bench.BenchRunner._record_tool_use("Bash", {"command": cmd}, run)
        assert run.used_batch is True
        # Outer `batch` counted once; each inner cmd attributed to its own counter.
        assert run.scan_query_subcommands.get("batch") == 1
        assert run.scan_query_subcommands.get("fn-rdeps") == 1
        assert run.scan_query_subcommands.get("rdeps") == 1

    def test_used_batch_defaults_false(self, script_run_bench: Any) -> None:
        run = script_run_bench.BenchRun(
            arm="codemap", task_id="X", task_type="symbol_extraction", model="haiku", success=False
        )
        assert run.used_batch is False

    def test_used_batch_serialised_to_jsonl(self, script_run_bench: Any) -> None:
        from dataclasses import asdict

        run = script_run_bench.BenchRun(
            arm="codemap", task_id="X", task_type="diff_impact", model="haiku", success=False
        )
        run.used_batch = True
        assert asdict(run)["used_batch"] is True


# ---------------------------------------------------------------------------
# Runner evaluators
# ---------------------------------------------------------------------------


class TestDiffImpactEvaluator:
    """_evaluate_diff_impact: caller recall AND test-file recall both ≥ 0.70."""

    def _task(self) -> dict:
        """Build the fixed task and ground truth consumed by this evaluator test group."""
        return {
            "type": "diff_impact",
            "id": "DI-x",
            "ground_truth": {
                "fn_callers": ["lightning.a::Foo.bar", "lightning.b::Baz.qux"],
                "unique_caller_count": 2,
                "test_modules": ["tests.test_a", "tests.test_b"],
            },
        }

    def test_both_recalls_high_is_correct(self, script_run_bench: Any) -> None:
        out = "## Callers\nlightning.a::Foo.bar\nlightning.b::Baz.qux\n## Tests\ntests.test_a\ntests.test_b\n"
        q = script_run_bench._evaluate_diff_impact(self._task(), out)
        assert q.correct is True
        assert q.scoring_detail["caller_recall"] == 1.0
        assert q.scoring_detail["test_recall"] == 1.0

    def test_missing_tests_fails_even_with_all_callers(self, script_run_bench: Any) -> None:
        out = "## Callers\nlightning.a::Foo.bar\nlightning.b::Baz.qux\n"
        q = script_run_bench._evaluate_diff_impact(self._task(), out)
        assert q.correct is False
        assert q.scoring_detail["caller_recall"] == 1.0
        assert q.scoring_detail["test_recall"] == 0.0

    def test_empty_gt_not_scored(self, script_run_bench: Any) -> None:
        task = {"type": "diff_impact", "id": "DI-x", "ground_truth": {"fn_callers": [], "test_modules": []}}
        assert script_run_bench._evaluate_diff_impact(task, "anything").scored is False


class TestGraphEvaluators:
    """Central set-overlap, path ordered-chain, fn-blast recall."""

    def test_central_set_overlap(self, script_run_bench: Any) -> None:
        task = {
            "type": "graph_central",
            "id": "GR-c",
            "ground_truth": {"central_modules": ["lightning.a.b", "lightning.c.d", "lightning.e.f"]},
        }
        out = "## Modules\nlightning.a.b\nlightning.c.d\n"
        q = script_run_bench._evaluate_graph_central(task, out)
        assert q.recall == pytest.approx(2 / 3, abs=0.01)
        assert q.correct is False  # 0.67 < 0.70

    def test_path_ordered_chain_correct(self, script_run_bench: Any) -> None:
        task = {
            "type": "graph_path",
            "id": "GR-p",
            "ground_truth": {"import_path": ["lightning.x.a", "lightning.x.b", "lightning.x.c"]},
        }
        out = "## Path\nlightning.x.a\nlightning.x.b\nlightning.x.c\n"
        q = script_run_bench._evaluate_graph_path(task, out)
        assert q.correct is True

    def test_path_out_of_order_incorrect(self, script_run_bench: Any) -> None:
        task = {
            "type": "graph_path",
            "id": "GR-p",
            "ground_truth": {"import_path": ["lightning.x.a", "lightning.x.b", "lightning.x.c"]},
        }
        out = "lightning.x.c came from lightning.x.b came from lightning.x.a"
        q = script_run_bench._evaluate_graph_path(task, out)
        assert q.correct is False  # reversed order

    def test_fn_blast_recall(self, script_run_bench: Any) -> None:
        task = {
            "type": "graph_fn_blast",
            "id": "GR-fb",
            "ground_truth": {"blast_callers": ["lightning.a::Foo.mid", "lightning.b::Bar.top"]},
        }
        out = "## Callers\nlightning.a::Foo.mid\nlightning.b::Bar.top\n"
        q = script_run_bench._evaluate_graph_fn_blast(task, out)
        assert q.correct is True
        assert q.recall == 1.0


class TestModuleMatchHelpers:
    """_module_mentioned / _module_first_pos matching semantics."""

    def test_exact_and_suffix_match(self, script_run_bench: Any) -> None:
        assert script_run_bench._module_mentioned("lightning.pytorch.loops.eval_loop", "see loops.eval_loop") is True
        assert script_run_bench._module_mentioned("a.b.c", "unrelated") is False

    def test_single_component_tail_not_matched(self, script_run_bench: Any) -> None:
        # A bare single-component tail is too weak to count on its own.
        assert script_run_bench._module_mentioned("lightning.pytorch.trainer", "the trainer runs") is False

    def test_first_pos_returns_earliest(self, script_run_bench: Any) -> None:
        assert script_run_bench._module_first_pos("a.b.c", "xx a.b.c yy") == 3
        assert script_run_bench._module_first_pos("a.b.c", "nope") is None


# ---------------------------------------------------------------------------
# Registration wiring in the runner
# ---------------------------------------------------------------------------


class TestRunnerRegistration:
    """New evaluators + diff_impact type are wired into the runner."""

    def test_evaluators_registered(self, script_run_bench: Any) -> None:
        for ttype in ("diff_impact", "graph_central", "graph_path", "graph_fn_blast", "module_blast_radius"):
            assert ttype in script_run_bench._EVALUATORS

    def test_diff_impact_type_constant(self, script_run_bench: Any) -> None:
        assert script_run_bench._DIFF_IMPACT_TYPE == "diff_impact"

    def test_codemap_tools_document_new_subcommands(self, script_run_bench: Any) -> None:
        prompt = script_run_bench._build_system_prompt("codemap", "r", "/repo", "/idx.json")
        for token in ("central", "path <source>", "fn-blast", "diff-impact", "batch"):
            assert token in prompt, token


# ---------------------------------------------------------------------------
# Module blast radius (MB) — import fan-in oracle / validator / evaluator
# ---------------------------------------------------------------------------


class TestModuleImportersOracle:
    """_module_importers_via_ast enumerates the reverse-import relation (importers of a module)."""

    def test_importers_of_a_module(self, script_gen_bench: Any, mini_repo: Path) -> None:
        # Graph: c imports b imports a → a's importer is b; b's importer is c.
        importers, err = script_gen_bench._module_importers_via_ast(mini_repo, "a")
        assert err is None
        assert importers == {"b"}

    def test_importers_exclude_tests_by_default(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """A test module importing the target is dropped when exclude_tests is on (rdeps parity)."""
        repo = tmp_path / "r"
        repo.mkdir()
        (repo / "hub.py").write_text("x = 1\n")
        (repo / "a.py").write_text("import hub\n")
        tests = repo / "tests"
        tests.mkdir()
        (tests / "test_hub.py").write_text("import hub\n")
        importers, _err = script_gen_bench._module_importers_via_ast(repo, "hub", exclude_tests=True)
        assert importers == {"a"}
        with_tests, _e2 = script_gen_bench._module_importers_via_ast(repo, "hub", exclude_tests=False)
        assert any("test_hub" in m for m in with_tests)

    def test_empty_when_nothing_imports(self, script_gen_bench: Any, mini_repo: Path) -> None:
        """A module nothing imports yields an empty set (valid answer, not an error)."""
        importers, err = script_gen_bench._module_importers_via_ast(mini_repo, "c")
        assert err is None
        assert importers == set()


class TestModuleBlastRadiusValidator:
    """_validate_module_blast_radius computes AST importer GT independent of scan-query."""

    def test_computes_importers(self, script_gen_bench: Any, mini_repo: Path) -> None:
        task = {
            "type": "module_blast_radius",
            "id": "MB-x",
            "primary_module": "a",
            "ground_truth": {"gt_pending": True, "importers": [], "importer_count": 0},
        }
        ok, live, reason = script_gen_bench._validate_module_blast_radius(task, None, None, mini_repo)
        assert ok is False  # gt_pending → not-ok, but oracle GT computed
        assert live["importers"] == ["b"]
        assert live["importer_count"] == 1
        assert live["gt_pending"] is False
        assert "computed" in reason

    def test_pending_flag_cleared_when_gt_matches(self, script_gen_bench: Any, mini_repo: Path) -> None:
        task = {
            "type": "module_blast_radius",
            "id": "MB-x",
            "primary_module": "a",
            "ground_truth": {"gt_pending": False, "importers": ["b"], "importer_count": 1},
        }
        ok, _live, reason = script_gen_bench._validate_module_blast_radius(task, None, None, mini_repo)
        assert ok is True, reason

    def test_rejects_missing_primary_module(self, script_gen_bench: Any, mini_repo: Path) -> None:
        task = {"type": "module_blast_radius", "id": "MB-x", "ground_truth": {}}
        ok, live, reason = script_gen_bench._validate_module_blast_radius(task, None, None, mini_repo)
        assert ok is False and live is None and "primary_module" in reason


class TestModuleBlastRadiusEvaluator:
    """_evaluate_module_blast_radius: importer recall ≥ 0.70, ≥2-component match, extraction-fail."""

    def _task(self) -> dict:
        """Build the fixed task and ground truth consumed by this evaluator test group."""
        return {
            "type": "module_blast_radius",
            "id": "MB-x",
            "ground_truth": {
                "importers": ["lightning.pytorch.a", "lightning.pytorch.b", "lightning.fabric.c"],
                "importer_count": 3,
            },
        }

    def test_full_recall_is_correct(self, script_run_bench: Any) -> None:
        out = "## Modules\nlightning.pytorch.a\nlightning.pytorch.b\nlightning.fabric.c\n"
        q = script_run_bench._evaluate_module_blast_radius(self._task(), out)
        assert q.correct is True
        assert q.recall == 1.0
        assert q.scoring_detail["threshold"] == 0.70

    def test_partial_recall_below_threshold_fails(self, script_run_bench: Any) -> None:
        out = "## Modules\nlightning.pytorch.a\n"  # 1/3 = 0.33
        q = script_run_bench._evaluate_module_blast_radius(self._task(), out)
        assert q.correct is False
        assert q.recall == pytest.approx(1 / 3, abs=0.01)

    def test_extraction_failure_when_none_named(self, script_run_bench: Any) -> None:
        q = script_run_bench._evaluate_module_blast_radius(self._task(), "no modules named here")
        assert q.extraction_failed is True
        assert q.recall == 0.0

    def test_empty_gt_not_scored(self, script_run_bench: Any) -> None:
        task = {"type": "module_blast_radius", "id": "MB-x", "ground_truth": {"importers": []}}
        assert script_run_bench._evaluate_module_blast_radius(task, "anything").scored is False

    def test_bare_leaf_does_not_match(self, script_run_bench: Any) -> None:
        """A bare single-component leaf (`states`) must NOT count — only ≥2-component dotted forms."""
        task = {
            "type": "module_blast_radius",
            "id": "MB-x",
            "ground_truth": {"importers": ["lightning.pytorch.trainer.states"], "importer_count": 1},
        }
        # Only the bare leaf appears — must be rejected by the ≥2-component matcher.
        q_leaf = script_run_bench._evaluate_module_blast_radius(task, "it uses states internally")
        assert q_leaf.recall == 0.0
        # A ≥2-component dotted suffix is accepted.
        q_suffix = script_run_bench._evaluate_module_blast_radius(task, "imported by trainer.states")
        assert q_suffix.recall == 1.0

    def test_matched_missed_importers_populated(self, script_run_bench: Any) -> None:
        out = "## Modules\nlightning.pytorch.a\nlightning.fabric.c\n"  # missing lightning.pytorch.b
        q = script_run_bench._evaluate_module_blast_radius(self._task(), out)
        assert q.scoring_detail["matched_importers"] == ["lightning.fabric.c", "lightning.pytorch.a"]
        assert q.scoring_detail["missed_importers"] == ["lightning.pytorch.b"]


class TestDevelopBrTailRecall:
    """_evaluate_develop_br records matched/missed caller lists without changing the recall scalar."""

    def _task(self) -> dict:
        """Build the fixed task and ground truth consumed by this evaluator test group."""
        return {
            "type": "develop_blast_radius",
            "id": "BR-x",
            "ground_truth": {
                "fn_callers": ["lightning.a::Foo.bar", "lightning.b::Baz.qux"],
                "unique_caller_count": 2,
            },
        }

    def test_matched_and_missed_populated(self, script_run_bench: Any) -> None:
        out = "## Callers\nlightning.a::Foo.bar\n"  # names only the first caller
        q = script_run_bench._evaluate_develop_br(self._task(), out)
        assert q.scoring_detail["matched_callers"] == ["lightning.a::Foo.bar"]
        assert q.scoring_detail["missed_callers"] == ["lightning.b::Baz.qux"]

    def test_recall_scalar_unchanged_by_instrumentation(self, script_run_bench: Any) -> None:
        """Tail-recall lists are additive: the recall value still reflects true positives / expected."""
        out = "## Callers\nlightning.a::Foo.bar\nlightning.b::Baz.qux\n"
        q = script_run_bench._evaluate_develop_br(self._task(), out)
        assert q.recall == 1.0
        assert q.scoring_detail["threshold"] == 0.70

    def test_scoring_detail_serialises_via_asdict(self, script_run_bench: Any) -> None:
        """Matched/missed lists survive asdict serialisation into the JSONL result line."""
        from dataclasses import asdict

        out = "## Callers\nlightning.a::Foo.bar\n"
        q = script_run_bench._evaluate_develop_br(self._task(), out)
        detail = asdict(q)["scoring_detail"]
        assert "matched_callers" in detail and "missed_callers" in detail

    def test_turn_count_serialises(self, script_run_bench: Any) -> None:
        """BenchRun.turn_count already serialises via asdict (tail-recall diagnostics companion)."""
        from dataclasses import asdict

        run = script_run_bench.BenchRun(
            arm="codemap", task_id="BR-x", task_type="develop_blast_radius", model="haiku", success=True
        )
        run.turn_count = 7
        assert asdict(run)["turn_count"] == 7

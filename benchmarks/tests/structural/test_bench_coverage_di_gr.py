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
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

SUITE = Path(__file__).resolve().parent.parent.parent / "suites" / "tasks-bench.json"
TASK_ROWS: tuple[dict[str, Any], ...] = tuple(json.loads(SUITE.read_text(encoding="utf-8"))["tasks"])
_MB_TASK_CASES = tuple(pytest.param(task, id=task["id"]) for task in TASK_ROWS if task["id"].startswith("MB-"))
_DI_TASK_CASES = tuple(pytest.param(task, id=task["id"]) for task in TASK_ROWS if task["id"].startswith("DI-"))
_GRAPH_PATH_TASK_CASES = tuple(pytest.param(task, id=task["id"]) for task in TASK_ROWS if task["type"] == "graph_path")
_DI_GR_TASK_CASES = tuple(
    pytest.param(task, id=task["id"]) for task in TASK_ROWS if task["id"].startswith(("DI-", "GR-"))
)
_NEW_TASK_TYPES = ("diff_impact", "graph_central", "graph_path", "graph_fn_blast", "module_blast_radius")
_BATCH_COMMAND = 'scan-query batch <<< \'[{"cmd": "fn-rdeps", "args": ["m::f"]}, {"cmd": "rdeps", "args": ["m"]}]\''


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


@pytest.fixture(name="tasks")
def _tasks() -> list[dict[str, Any]]:
    """Provide the shipped benchmark task rows as a fresh list."""
    return list(TASK_ROWS)


# ---------------------------------------------------------------------------
# Task schema
# ---------------------------------------------------------------------------


class TestDiGrTaskSchema:
    """Schema invariants for the new DI/GR tasks in tasks-bench.json."""

    def test_suite_is_valid_json_object(self) -> None:
        """tasks-bench.json is an object with repo header + tasks list.

        The suite file is parsed as shipped, so a truncated or re-shaped file fails before any task check runs.
        """
        data = json.loads(SUITE.read_text())
        assert set(data) >= {"repo", "tasks"}
        assert isinstance(data["tasks"], list)

    @pytest.mark.parametrize(
        ("prefix", "expected_count", "expected_type"),
        [
            pytest.param("DI-", 6, "diff_impact", id="diff-impact-roster"),
            pytest.param("MB-", 5, "module_blast_radius", id="module-blast-radius-roster"),
        ],
    )
    def test_roster_has_exact_count_of_typed_records(
        self, tasks: list[dict[str, Any]], prefix: str, expected_count: int, expected_type: str
    ) -> None:
        """The DI roster has exactly six diff-impact records; the MB roster exactly five module-blast-radius records.

        Adding or dropping a task of either series must be a deliberate edit of this expectation.
        """
        records = [t for t in tasks if t["id"].startswith(prefix)]
        assert len(records) == expected_count
        assert all(t["type"] == expected_type for t in records)

    def test_gr_roster_has_four_graph_tasks(self, tasks: list[dict[str, Any]]) -> None:
        """GR roster contains the declared graph types and its single graph-path record.

        Four GR tasks cover central, path and function-blast types, with GR-02 the only path task.
        """
        gr = [t for t in tasks if t["id"].startswith("GR-")]
        assert len(gr) == 4
        assert {t["type"] for t in gr} == {"graph_central", "graph_path", "graph_fn_blast"}
        assert [task["id"] for task in gr if task["type"] == "graph_path"] == ["GR-02"]

    @pytest.mark.parametrize("task", _MB_TASK_CASES)
    def test_mb_tasks_have_materialized_importer_gt(self, task: dict[str, Any]) -> None:
        """MB tasks ship materialized (non-pending, non-empty) importer ground truth.

        The importer list must be non-empty, agree with its count, exclude test modules and belong to a query workflow
        with a primary module.
        """
        gt = task["ground_truth"]
        assert gt.get("gt_pending") is False, task["id"]
        assert gt["importers"], task["id"]
        assert gt["importer_count"] == len(gt["importers"]), task["id"]
        assert task.get("workflow_type") == "query", task["id"]
        assert task["primary_module"], task["id"]
        assert not any(module.startswith("tests.") for module in gt["importers"]), task["id"]

    @pytest.mark.parametrize("task", _DI_TASK_CASES)
    def test_di_tasks_have_stage_spec_and_primary_fn(self, task: dict[str, Any]) -> None:
        """Each DI task exposes a callable target and executable stage specification.

        The primary function is a ``module::name`` reference and every staged edit names a file plus either an
        append or a find/replace pair, which the stager needs to apply it.
        """
        assert "::" in task["primary_fn"], task["id"]
        assert isinstance(task.get("stage"), list), task["id"]
        assert task["stage"], task["id"]
        assert all(
            edit.get("file") and ("append" in edit or ("find" in edit and "replace" in edit)) for edit in task["stage"]
        ), task["id"]

    def test_di_gr_tasks_have_materialized_gt(self, tasks: list[dict]) -> None:
        """The locked target materializes every DI/GR independent-oracle answer.

        No DI or GR task may still be marked ground-truth pending.
        """
        new = [t for t in tasks if t["id"].startswith(("DI-", "GR-"))]
        assert new
        assert all(t["ground_truth"].get("gt_pending") is False for t in new)

    @pytest.mark.parametrize("task", _GRAPH_PATH_TASK_CASES)
    def test_graph_path_task_declares_source_and_target(self, task: dict[str, Any]) -> None:
        """Each graph-path task declares both ground-truth endpoints.

        A path question without a source or target cannot be answered or scored.
        """
        assert task["ground_truth"]["source"]
        assert task["ground_truth"]["target"]

    @pytest.mark.parametrize("task", _DI_GR_TASK_CASES)
    def test_di_gr_tasks_are_scoreable(self, task: dict[str, Any]) -> None:
        """Each DI/GR task is eligible for benchmark scoring.

        A task flagged unscoreable would silently drop out of the accuracy aggregate.
        """
        assert task.get("scoreable") is True, task["id"]


# ---------------------------------------------------------------------------
# Generator validators / oracles (tiny fixture repos)
# ---------------------------------------------------------------------------


class TestDiffImpactValidator:
    """_validate_diff_impact computes AST caller + test-import GT independent of scan-query."""

    def test_computes_callers_and_test_modules(self, script_gen_bench: Any, mini_repo: Path) -> None:
        """A pending task is reported not-ok while the oracle computes its callers and test modules.

        On the mini repository ``b.caller`` is the only caller of ``a.target`` and ``tests.test_a`` the only test
        importing ``a``; the live ground truth drops its pending flag.
        """
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
        """Once GT is populated (gt_pending cleared), a matching task validates ok.

        The stored callers and test modules equal the oracle's, so validation passes with no recomputation notice.
        """
        task = {
            "type": "diff_impact",
            "id": "DI-x",
            "primary_fn": "a::target",
            "primary_module": "a",
            "ground_truth": {"gt_pending": False, "fn_callers": ["b::caller"], "test_modules": ["tests.test_a"]},
        }
        ok, _live, reason = script_gen_bench._validate_diff_impact(task, None, None, mini_repo)
        assert ok is True, reason

    def test_rejects_malformed_primary_fn(self, script_gen_bench: Any, mini_repo: Path) -> None:
        """A primary function without the ``module::name`` separator is rejected with a clear reason.

        The validator returns no live ground truth and names ``primary_fn`` so the task author knows what to fix.
        """
        task = {"type": "diff_impact", "id": "DI-x", "primary_fn": "no_colons", "ground_truth": {}}
        ok, live, reason = script_gen_bench._validate_diff_impact(task, None, None, mini_repo)
        assert ok is False
        assert live is None
        assert "primary_fn" in reason


class TestGraphValidators:
    """graph_central / graph_path / graph_fn_blast validators on a tiny fixture repo."""

    def test_central_ranks_by_in_degree(self, script_gen_bench: Any, mini_repo: Path) -> None:
        """The two most-imported modules are reported as central.

        On the mini repository a is imported by b and b by c, so both have in-degree 1 and c has 0.
        """
        task = {"type": "graph_central", "id": "GR-x", "ground_truth": {"gt_pending": True, "top": 2}}
        ok, live, _reason = script_gen_bench._validate_graph_central(task, None, None, mini_repo)
        assert ok is False  # pending
        assert set(live["central_modules"]) == {"a", "b"}

    def test_path_returns_unique_chain(self, script_gen_bench: Any, mini_repo: Path) -> None:
        """A path between two modules is computed in import order and flagged unique.

        Module c reaches a only through b, so the single import chain is c, b, a.
        """
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
        """A path task without endpoints is rejected with a reason naming ``source``.

        Without both endpoints no chain can be computed, so no live ground truth is produced.
        """
        task = {"type": "graph_path", "id": "GR-p", "ground_truth": {"gt_pending": True}}
        ok, live, reason = script_gen_bench._validate_graph_path(task, None, None, mini_repo)
        assert ok is False
        assert live is None
        assert "source" in reason

    def test_fn_blast_depth2_closure(self, script_gen_bench: Any, mini_repo: Path) -> None:
        """The transitive caller closure of a function is computed to the requested depth.

        With depth 2 the callers of ``a::target`` reduce to ``b::caller``, the only function calling it.
        """
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
        """_module_imports collects import targets on node.module truthiness, ignoring node.level.

        Each source snippet pins one import form (dotted, aliased, from-import, bare or multi-dot relative) to the
        target set scan-index would produce.
        """
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
        """Edges point to in-repo import targets; test modules are excluded by default.

        The mini repository yields a no-edge, b to a and c to b, with no node for ``tests/test_a.py``.
        """
        graph = script_gen_bench._build_import_graph(mini_repo)
        assert graph["a"] == set()
        assert graph["b"] == {"a"}
        assert graph["c"] == {"b"}
        assert not any("test_a" in key for key in graph)

    def test_build_import_graph_drops_external_targets(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Stdlib / third-party import targets are filtered out — only in-repo modules remain as edges.

        A module importing ``os``, ``json`` and the in-repo ``a`` keeps only the edge to ``a``.
        """
        repo = tmp_path / "extrepo"
        repo.mkdir()
        (repo / "m.py").write_text("import os\nimport json\nimport a\n")
        (repo / "a.py").write_text("x = 1\n")
        graph = script_gen_bench._build_import_graph(repo)
        assert graph["m"] == {"a"}

    def test_build_import_graph_include_tests(self, script_gen_bench: Any, mini_repo: Path) -> None:
        """Keep test modules as graph nodes when test exclusion is disabled.

        The same mini repository that excludes ``test_a`` by default exposes it when ``exclude_tests=False``.
        """
        graph = script_gen_bench._build_import_graph(mini_repo, exclude_tests=False)
        assert any("test_a" in key for key in graph)


class TestGtPending:
    """gt_is_pending helper + oracle-backed registration."""

    @pytest.mark.parametrize(
        ("task", "pending"),
        [
            pytest.param({"ground_truth": {"gt_pending": True}}, True, id="flag-true"),
            pytest.param({"ground_truth": {"gt_pending": False}}, False, id="flag-false"),
            pytest.param({"ground_truth": {}}, False, id="flag-absent"),
        ],
    )
    def test_gt_is_pending_follows_flag(self, script_gen_bench: Any, task: dict, pending: bool) -> None:
        """A task is pending only when its ground truth sets ``gt_pending`` to true.

        An absent flag counts as materialized, so older tasks without the key are not treated as unfinished.
        """
        assert script_gen_bench.gt_is_pending(task) is pending

    @pytest.mark.parametrize("ttype", _NEW_TASK_TYPES)
    def test_new_types_are_oracle_backed(self, script_gen_bench: Any, ttype: str) -> None:
        """Each new benchmark task type uses an oracle-backed update path.

        Ground truth for these types is recomputed from source rather than hand-maintained.
        """
        assert script_gen_bench._update_is_oracle_backed({"type": ttype}) is True

    @pytest.mark.parametrize("ttype", _NEW_TASK_TYPES)
    def test_new_types_registered_in_validators(self, script_gen_bench: Any, ttype: str) -> None:
        """Each new task type has a validator registered in the generator.

        A type missing from the registry could not be validated or regenerated.
        """
        assert ttype in script_gen_bench.VALIDATORS


# ---------------------------------------------------------------------------
# DiffImpactStager stage / revert / dirty-tree refusal
# ---------------------------------------------------------------------------


class TestDiffImpactStager:
    """Stage-then-revert safety, including dirty-tree refusal.

    Kept on a real throwaway git repository: the stager applies and reverts edits through git, so the real tool is the
    contract.
    """

    def _spec(self) -> list[dict]:
        """Describe a signature edit that introduces a new optional parameter."""
        return [
            {"file": "src.py", "find": "def widely_used(", "replace": "def widely_used(new_arg=None,  # staged\n    "}
        ]

    def test_stage_applies_and_revert_restores(self, script_run_bench: Any, git_repo: Path) -> None:
        """Staged edits are visible inside the block and reverted on exit.

        The signature edit adds ``new_arg`` while the context is open; leaving it restores the committed file.
        """
        original = (git_repo / "src.py").read_text()
        stager = script_run_bench.DiffImpactStager(git_repo, self._spec())
        with stager:
            staged = (git_repo / "src.py").read_text()
            assert "new_arg=None" in staged
        # On block exit the change is reverted via git checkout --.
        assert (git_repo / "src.py").read_text() == original

    def test_revert_runs_even_on_exception(self, script_run_bench: Any, git_repo: Path) -> None:
        """A failure inside the staged block still restores the committed file.

        An arm crashing mid-task must not leave the benchmark repository modified for the next arm.
        """
        original = (git_repo / "src.py").read_text()
        stager = script_run_bench.DiffImpactStager(git_repo, self._spec())

        def _run_expected_failure() -> None:
            """Run the statements expected to fail as one callable."""
            with stager:
                assert "new_arg=None" in (git_repo / "src.py").read_text()
                raise RuntimeError("arm blew up mid-task")

        with pytest.raises(RuntimeError):
            _run_expected_failure()
        assert (git_repo / "src.py").read_text() == original

    def test_refuses_dirty_tree_and_leaves_user_edit_intact(self, script_run_bench: Any, git_repo: Path) -> None:
        """A tree with uncommitted edits is refused and the user's own edit is neither touched nor reverted.

        The staged path is dirtied before staging; entering the stager must raise instead of overwriting work the user
        has not committed.
        """
        dirty = "def widely_used():\n    return 999  # user edit\n"
        (git_repo / "src.py").write_text(dirty)
        stager = script_run_bench.DiffImpactStager(git_repo, self._spec())
        with pytest.raises(script_run_bench.DirtyTreeError):
            stager.__enter__()
        assert (git_repo / "src.py").read_text() == dirty

    def test_missing_find_text_aborts(self, script_run_bench: Any, git_repo: Path) -> None:
        """A find/replace edit whose find text is absent aborts the staging.

        Staging a stale spec must fail loudly rather than benchmark an unmodified repository.
        """
        bad = [{"file": "src.py", "find": "NONEXISTENT_TOKEN", "replace": "x"}]
        stager = script_run_bench.DiffImpactStager(git_repo, bad)
        with pytest.raises(script_run_bench.DirtyTreeError):
            stager.__enter__()
        stager.revert()  # best-effort cleanup

    def test_append_edit_form(self, script_run_bench: Any, git_repo: Path) -> None:
        """The append edit form adds text at the end of the file and is reverted on exit.

        Appending a comment must show at the file end inside the block and disappear afterwards.
        """
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
        """A batch command's inner subcommands are returned in order.

        The heredoc JSON names ``fn-rdeps`` then ``rdeps``; both are recognized scan-query subcommands.
        """
        assert script_run_bench._parse_batch_subcommands(_BATCH_COMMAND) == ["fn-rdeps", "rdeps"]

    @pytest.mark.parametrize(
        "cmd",
        [
            pytest.param("scan-query symbol Trainer", id="non-batch"),
            pytest.param('scan-query batch <<< \'[{"cmd": "bogus"}]\'', id="unknown-inner-cmd-dropped"),
            pytest.param("scan-query batch <<< '[not json'", id="malformed-json"),
        ],
    )
    def test_unparseable_batch_returns_empty(self, script_run_bench: Any, cmd: str) -> None:
        """A non-batch command, an unknown inner subcommand, or malformed JSON yields no inner subcommands.

        Bad input is ignored rather than raising, so a model's odd command never crashes telemetry.
        """
        assert script_run_bench._parse_batch_subcommands(cmd) == []

    def test_batch_and_diff_impact_are_subcommands(self, script_run_bench: Any) -> None:
        """``batch`` and ``diff-impact`` are recognized scan-query subcommands.

        The subcommand parser also resolves a ``diff-impact --base`` invocation to its subcommand name.
        """
        assert "batch" in script_run_bench._SCAN_QUERY_SUBCOMMANDS
        assert "diff-impact" in script_run_bench._SCAN_QUERY_SUBCOMMANDS
        assert script_run_bench._parse_scan_query_subcommand("scan-query diff-impact --base HEAD~1") == "diff-impact"

    def test_record_tool_use_sets_used_batch_and_attributes_inner(self, script_run_bench: Any) -> None:
        """Recording a batch tool call sets ``used_batch`` and counts the outer and inner commands.

        The outer ``batch`` is counted once and each inner command is attributed to its own counter.
        """
        run = script_run_bench.BenchRun(
            arm="codemap", task_id="X", task_type="diff_impact", model="haiku", success=False
        )
        script_run_bench.BenchRunner._record_tool_use("Bash", {"command": _BATCH_COMMAND}, run)
        assert run.used_batch is True
        assert run.scan_query_subcommands.get("batch") == 1
        assert run.scan_query_subcommands.get("fn-rdeps") == 1
        assert run.scan_query_subcommands.get("rdeps") == 1

    def test_used_batch_defaults_false(self, script_run_bench: Any) -> None:
        """A fresh run reports no batch use.

        The flag only turns on when a batch command is recorded.
        """
        run = script_run_bench.BenchRun(
            arm="codemap", task_id="X", task_type="symbol_extraction", model="haiku", success=False
        )
        assert run.used_batch is False

    def test_used_batch_serialised_to_jsonl(self, script_run_bench: Any) -> None:
        """The ``used_batch`` flag survives ``asdict`` serialisation into the result line.

        Result lines are written from the dataclass, so a field missing here would vanish from telemetry.
        """
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

    @pytest.mark.parametrize(
        ("out", "expected_correct", "expected_test_recall"),
        [
            pytest.param(
                "## Callers\nlightning.a::Foo.bar\nlightning.b::Baz.qux\n## Tests\ntests.test_a\ntests.test_b\n",
                True,
                1.0,
                id="both-recalls-high",
            ),
            pytest.param(
                "## Callers\nlightning.a::Foo.bar\nlightning.b::Baz.qux\n",
                False,
                0.0,
                id="missing-tests-fails-even-with-all-callers",
            ),
        ],
    )
    def test_correct_requires_caller_and_test_recall(
        self, script_run_bench: Any, out: str, expected_correct: bool, expected_test_recall: float
    ) -> None:
        """An answer is correct only when both caller recall and test-file recall clear the threshold.

        Naming every caller but no test file leaves caller recall at 1.0 and test recall at 0.0, which must fail.
        """
        q = script_run_bench._evaluate_diff_impact(self._task(), out)
        assert q.correct is expected_correct
        assert q.scoring_detail["caller_recall"] == 1.0
        assert q.scoring_detail["test_recall"] == expected_test_recall


class TestGraphEvaluators:
    """Central set-overlap, path ordered-chain, fn-blast recall."""

    def test_central_set_overlap(self, script_run_bench: Any) -> None:
        """Naming two of three central modules gives recall 2/3, below the 0.70 threshold.

        Set overlap decides central-module answers, so a partial answer must not be marked correct.
        """
        task = {
            "type": "graph_central",
            "id": "GR-c",
            "ground_truth": {"central_modules": ["lightning.a.b", "lightning.c.d", "lightning.e.f"]},
        }
        out = "## Modules\nlightning.a.b\nlightning.c.d\n"
        q = script_run_bench._evaluate_graph_central(task, out)
        assert q.recall == pytest.approx(2 / 3, abs=0.01)
        assert q.correct is False  # 0.67 < 0.70

    @pytest.mark.parametrize(
        ("out", "expected_correct"),
        [
            pytest.param("## Path\nlightning.x.a\nlightning.x.b\nlightning.x.c\n", True, id="ordered-chain-correct"),
            pytest.param(
                "lightning.x.c came from lightning.x.b came from lightning.x.a", False, id="reversed-order-incorrect"
            ),
        ],
    )
    def test_path_scores_the_ordered_chain(self, script_run_bench: Any, out: str, expected_correct: bool) -> None:
        """A path answer is correct only when its modules appear in the ground-truth import order.

        The same three modules listed in reverse describe the opposite direction and must not score.
        """
        task = {
            "type": "graph_path",
            "id": "GR-p",
            "ground_truth": {"import_path": ["lightning.x.a", "lightning.x.b", "lightning.x.c"]},
        }
        q = script_run_bench._evaluate_graph_path(task, out)
        assert q.correct is expected_correct

    def test_fn_blast_recall(self, script_run_bench: Any) -> None:
        """Naming every transitive caller scores full recall and a correct answer.

        Both expected blast callers appear in the answer, so recall is 1.0.
        """
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

    @pytest.mark.parametrize(
        ("module", "text", "mentioned"),
        [
            pytest.param("lightning.pytorch.loops.eval_loop", "see loops.eval_loop", True, id="dotted-suffix"),
            pytest.param("a.b.c", "unrelated", False, id="unrelated-text"),
            pytest.param("lightning.pytorch.trainer", "the trainer runs", False, id="bare-single-component-tail"),
        ],
    )
    def test_module_mentioned(self, script_run_bench: Any, module: str, text: str, mentioned: bool) -> None:
        """A module counts as mentioned by its full name or a dotted suffix, never by a bare one-word tail.

        A single-component tail such as ``trainer`` is too weak to count on its own.
        """
        assert script_run_bench._module_mentioned(module, text) is mentioned

    @pytest.mark.parametrize(
        ("text", "position"),
        [
            pytest.param("xx a.b.c yy", 3, id="found-at-offset"),
            pytest.param("nope", None, id="absent"),
        ],
    )
    def test_first_pos_returns_earliest(self, script_run_bench: Any, text: str, position: int | None) -> None:
        """The first-position helper returns the earliest offset of the module, or None when absent.

        Position drives answer-ordering checks, so a missing module must not yield a numeric position.
        """
        assert script_run_bench._module_first_pos("a.b.c", text) == position


# ---------------------------------------------------------------------------
# Registration wiring in the runner
# ---------------------------------------------------------------------------


class TestRunnerRegistration:
    """New evaluators + diff_impact type are wired into the runner."""

    @pytest.mark.parametrize("ttype", _NEW_TASK_TYPES)
    def test_evaluators_registered(self, script_run_bench: Any, ttype: str) -> None:
        """Every new task type has a runner evaluator.

        A task type with no evaluator could never be scored.
        """
        assert ttype in script_run_bench._EVALUATORS

    def test_diff_impact_type_constant(self, script_run_bench: Any) -> None:
        """The runner's diff-impact type constant equals the suite's task type string.

        Staging is keyed on this constant, so a mismatch would skip staging for every diff-impact task.
        """
        assert script_run_bench._DIFF_IMPACT_TYPE == "diff_impact"

    @pytest.mark.parametrize("token", ["central", "path <source>", "fn-blast", "diff-impact", "batch"])
    def test_codemap_tools_document_new_subcommands(self, script_run_bench: Any, token: str) -> None:
        """The codemap-arm system prompt documents each new scan-query subcommand.

        An undocumented subcommand would never be used by the model, hiding the capability being benchmarked.
        """
        prompt = script_run_bench._build_system_prompt("codemap", "r", "/repo", "/idx.json")
        assert token in prompt


# ---------------------------------------------------------------------------
# Module blast radius (MB) — import fan-in oracle / validator / evaluator
# ---------------------------------------------------------------------------


class TestModuleImportersOracle:
    """_module_importers_via_ast enumerates the reverse-import relation (importers of a module)."""

    def test_importers_of_a_module(self, script_gen_bench: Any, mini_repo: Path) -> None:
        """The importers of a module are the modules that import it directly.

        Graph: c imports b imports a, so a's only importer is b.
        """
        importers, err = script_gen_bench._module_importers_via_ast(mini_repo, "a")
        assert err is None
        assert importers == {"b"}

    @pytest.mark.parametrize(
        ("exclude_tests", "has_test_importer"),
        [
            pytest.param(True, False, id="tests-excluded-by-default"),
            pytest.param(False, True, id="tests-included-on-request"),
        ],
    )
    def test_importers_respect_exclude_tests(
        self, script_gen_bench: Any, tmp_path: Path, exclude_tests: bool, has_test_importer: bool
    ) -> None:
        """A test module importing the target is dropped when exclude_tests is on (rdeps parity).

        ``hub`` is imported by ``a`` and by ``tests/test_hub.py``; the test importer appears only when requested.
        """
        repo = tmp_path / "r"
        repo.mkdir()
        (repo / "hub.py").write_text("x = 1\n")
        (repo / "a.py").write_text("import hub\n")
        tests = repo / "tests"
        tests.mkdir()
        (tests / "test_hub.py").write_text("import hub\n")
        importers, _err = script_gen_bench._module_importers_via_ast(repo, "hub", exclude_tests=exclude_tests)
        assert "a" in importers
        assert any("test_hub" in m for m in importers) is has_test_importer

    def test_empty_when_nothing_imports(self, script_gen_bench: Any, mini_repo: Path) -> None:
        """A module nothing imports yields an empty set (valid answer, not an error).

        Module c is at the top of the mini repository's import chain.
        """
        importers, err = script_gen_bench._module_importers_via_ast(mini_repo, "c")
        assert err is None
        assert importers == set()


class TestModuleBlastRadiusValidator:
    """_validate_module_blast_radius computes AST importer GT independent of scan-query."""

    def test_computes_importers(self, script_gen_bench: Any, mini_repo: Path) -> None:
        """A pending task is reported not-ok while the oracle computes its importers and count.

        Module a is imported only by b, so the live ground truth lists b with count 1 and drops the pending flag.
        """
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
        """Once GT is populated (gt_pending cleared), a matching task validates ok.

        The stored importer list and count equal the oracle's, so validation passes.
        """
        task = {
            "type": "module_blast_radius",
            "id": "MB-x",
            "primary_module": "a",
            "ground_truth": {"gt_pending": False, "importers": ["b"], "importer_count": 1},
        }
        ok, _live, reason = script_gen_bench._validate_module_blast_radius(task, None, None, mini_repo)
        assert ok is True, reason

    def test_rejects_missing_primary_module(self, script_gen_bench: Any, mini_repo: Path) -> None:
        """A task without a primary module is rejected with a reason naming ``primary_module``.

        No target means no importers can be computed, so no live ground truth is produced.
        """
        task = {"type": "module_blast_radius", "id": "MB-x", "ground_truth": {}}
        ok, live, reason = script_gen_bench._validate_module_blast_radius(task, None, None, mini_repo)
        assert ok is False
        assert live is None
        assert "primary_module" in reason


class TestEmptyGroundTruthEvaluators:
    """Evaluators never score a task whose ground truth is empty."""

    @pytest.mark.parametrize(
        ("evaluator_name", "task"),
        [
            pytest.param(
                "_evaluate_diff_impact",
                {"type": "diff_impact", "id": "DI-x", "ground_truth": {"fn_callers": [], "test_modules": []}},
                id="diff-impact",
            ),
            pytest.param(
                "_evaluate_module_blast_radius",
                {"type": "module_blast_radius", "id": "MB-x", "ground_truth": {"importers": []}},
                id="module-blast-radius",
            ),
        ],
    )
    def test_empty_gt_not_scored(self, script_run_bench: Any, evaluator_name: str, task: dict[str, Any]) -> None:
        """An empty ground-truth set leaves the result unscored regardless of the answer text.

        Scoring against nothing would make any answer vacuously correct.
        """
        assert getattr(script_run_bench, evaluator_name)(task, "anything").scored is False


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
        """Naming every importer scores full recall and a correct answer at the 0.70 threshold.

        All three expected importers appear in the answer, so recall is 1.0.
        """
        out = "## Modules\nlightning.pytorch.a\nlightning.pytorch.b\nlightning.fabric.c\n"
        q = script_run_bench._evaluate_module_blast_radius(self._task(), out)
        assert q.correct is True
        assert q.recall == 1.0
        assert q.scoring_detail["threshold"] == 0.70

    def test_partial_recall_below_threshold_fails(self, script_run_bench: Any) -> None:
        """Naming one of three importers (recall 0.33) is below the threshold and incorrect.

        A single listed importer out of three must not pass.
        """
        out = "## Modules\nlightning.pytorch.a\n"  # 1/3 = 0.33
        q = script_run_bench._evaluate_module_blast_radius(self._task(), out)
        assert q.correct is False
        assert q.recall == pytest.approx(1 / 3, abs=0.01)

    def test_extraction_failure_when_none_named(self, script_run_bench: Any) -> None:
        """An answer naming no modules is flagged as an extraction failure with zero recall.

        This separates a model that gave no usable answer from one that answered wrongly.
        """
        q = script_run_bench._evaluate_module_blast_radius(self._task(), "no modules named here")
        assert q.extraction_failed is True
        assert q.recall == 0.0

    @pytest.mark.parametrize(
        ("out", "expected_recall"),
        [
            pytest.param("it uses states internally", 0.0, id="bare-leaf-rejected"),
            pytest.param("imported by trainer.states", 1.0, id="two-component-suffix-accepted"),
        ],
    )
    def test_matching_requires_two_component_form(
        self, script_run_bench: Any, out: str, expected_recall: float
    ) -> None:
        """A bare single-component leaf (``states``) does not count; a dotted suffix of two components does.

        The expected importer is ``lightning.pytorch.trainer.states``; only the dotted ``trainer.states`` form is
        specific enough to match.
        """
        task = {
            "type": "module_blast_radius",
            "id": "MB-x",
            "ground_truth": {"importers": ["lightning.pytorch.trainer.states"], "importer_count": 1},
        }
        q = script_run_bench._evaluate_module_blast_radius(task, out)
        assert q.recall == expected_recall

    def test_matched_missed_importers_populated(self, script_run_bench: Any) -> None:
        """The scoring detail lists the matched and the missed importers in sorted order.

        Naming two of the three importers leaves ``lightning.pytorch.b`` in the missed list.
        """
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
        """The scoring detail lists the callers the answer named and the ones it missed.

        An answer naming only the first caller reports the second as missed.
        """
        out = "## Callers\nlightning.a::Foo.bar\n"  # names only the first caller
        q = script_run_bench._evaluate_develop_br(self._task(), out)
        assert q.scoring_detail["matched_callers"] == ["lightning.a::Foo.bar"]
        assert q.scoring_detail["missed_callers"] == ["lightning.b::Baz.qux"]

    def test_recall_scalar_unchanged_by_instrumentation(self, script_run_bench: Any) -> None:
        """Tail-recall lists are additive: the recall value still reflects true positives / expected.

        Naming both callers keeps recall at 1.0 and the 0.70 threshold in the detail.
        """
        out = "## Callers\nlightning.a::Foo.bar\nlightning.b::Baz.qux\n"
        q = script_run_bench._evaluate_develop_br(self._task(), out)
        assert q.recall == 1.0
        assert q.scoring_detail["threshold"] == 0.70

    def test_scoring_detail_serialises_via_asdict(self, script_run_bench: Any) -> None:
        """Matched/missed lists survive asdict serialisation into the JSONL result line.

        Result lines are written from the dataclass, so both lists must be present in the serialised detail.
        """
        out = "## Callers\nlightning.a::Foo.bar\n"
        q = script_run_bench._evaluate_develop_br(self._task(), out)
        detail = asdict(q)["scoring_detail"]
        assert "matched_callers" in detail
        assert "missed_callers" in detail

    def test_turn_count_serialises(self, script_run_bench: Any) -> None:
        """BenchRun.turn_count already serialises via asdict (tail-recall diagnostics companion).

        Setting seven turns must appear unchanged in the serialised run.
        """
        run = script_run_bench.BenchRun(
            arm="codemap", task_id="BR-x", task_type="develop_blast_radius", model="haiku", success=True
        )
        run.turn_count = 7
        assert asdict(run)["turn_count"] == 7

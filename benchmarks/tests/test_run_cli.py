"""Tests for benchmarks/run-codemap-cli.py.

Covers public API surface:
  - Dataclass defaults and construction (Task, ScenarioResult, TimingStats,
    AccuracyStats, SuiteStats, ValidationResult, Query)
  - Task loading (load_tasks, load_oss_tasks) with and without filters
  - Pure helper logic (path_to_module, module_to_grep_pattern, module_to_package)
  - Accuracy scoring (compute_precision_recall)
  - Verdict computation (compute_verdict)
  - JSON structure validators (validate_central_json, validate_rdeps_json,
    validate_deps_json)
  - scan-query subprocess wrapper (run_scan_query) with mocked subprocess
  - Index-path resolution (resolve_index_path)
  - Repo-path resolution (resolve_repo_path) with env-var override
  - load_oss_tasks absent-file skip behaviour
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from types import SimpleNamespace
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from _launcher_capability import _raw_codemap_launchers_are_runnable

REPO_ROOT = Path(__file__).parent.parent.parent
PYTORCH_LIGHTNING_REPO = Path(os.environ.get("PL_REPO_PATH", str(REPO_ROOT / ".sandbox" / "pytorch-lightning")))
PYTORCH_LIGHTNING_INDEXES = (
    tuple(sorted(PYTORCH_LIGHTNING_REPO.rglob(".cache/codemap/*.json"))) if PYTORCH_LIGHTNING_REPO.exists() else ()
)
RAW_CODEMAP_LAUNCHERS = pytest.mark.skipif(
    not _raw_codemap_launchers_are_runnable(REPO_ROOT),
    reason="raw scan-index/scan-query launchers cannot execute on this host",
)
NETWORK_INTEGRATION = pytest.mark.skipif(
    os.environ.get("CODEMAP_NETWORK_TESTS") != "1",
    reason="set CODEMAP_NETWORK_TESTS=1 to run network integration tests",
)
PYTORCH_LIGHTNING_CHECKOUT = pytest.mark.skipif(
    not PYTORCH_LIGHTNING_REPO.is_dir(),
    reason=f"pytorch-lightning repo not found at {PYTORCH_LIGHTNING_REPO}",
)
PYTORCH_LIGHTNING_INDEX = pytest.mark.skipif(
    not PYTORCH_LIGHTNING_INDEXES,
    reason=f"No codemap index found under {PYTORCH_LIGHTNING_REPO}/.cache/codemap/",
)


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture(name="real_tasks", scope="session")
def _real_tasks(script_run_cli: Any) -> list:
    """Load tasks-code.json once for the whole session.

    Returns:
        All benchmark tasks parsed from the real tasks-code.json file.

    Example:
        >>> tasks = getfixture("real_tasks")
        >>> bool(tasks), len({task.id for task in tasks}) == len(tasks)
        (True, True)
    """
    return script_run_cli.load_tasks()


@pytest.fixture(name="minimal_task_dict")
def _minimal_task_dict() -> dict:
    """Return a minimal valid raw task dict matching the tasks-code.json schema.

    Returns:
        Dict with all required keys populated with well-formed values.

    Example:
        >>> task = getfixture("minimal_task_dict")
        >>> task["queries"], task["ground_truth_keys"]
        ([{'cmd': 'rdeps', 'args': ['pkg.mod']}], ['imported_by'])
    """
    return {
        "id": "T-99",
        "skill": "fix",
        "prompt": "A test prompt.",
        "primary_module": "pkg.mod",
        "risk_tier": "high",
        "queries": [{"cmd": "rdeps", "args": ["pkg.mod"]}],
        "ground_truth_keys": ["imported_by"],
    }


def _make_scenario(script_cli: Any, passed: bool, suite: str = "calls") -> Any:
    """Build a ScenarioResult with the minimum required fields.

    Args:
        script_cli: Loaded scan-query module fixture.
        passed: Whether the scenario passed.
        suite: Suite name string.

    Returns:
        ScenarioResult instance.

    Example:
        >>> scenario = _make_scenario(getfixture("script_run_cli"), False, suite="example")
        >>> scenario.passed, scenario.suite, scenario.result
        (False, 'example', {'coverage_gap': 0.5})
    """
    return script_cli.ScenarioResult(
        scenario="C1",
        name="coverage-gap",
        suite=suite,
        passed=passed,
        result={"coverage_gap": 0.5},
        threshold=script_cli.THRESHOLDS["C1"],
    )


# ===========================================================================
# Dataclass construction
# ===========================================================================


class TestDataclasses:
    """Verify dataclass fields and defaults behave as documented."""

    def test_query_stores_cmd_and_args(self, script_run_cli: Any) -> None:
        """Query created from raw dict has correct cmd and args."""
        q = script_run_cli.Query(cmd="rdeps", args=["foo.bar"])
        assert q.cmd == "rdeps"
        assert q.args == ["foo.bar"]

    def test_task_from_dict_roundtrip(self, script_run_cli: Any, minimal_task_dict: dict) -> None:
        """Task.from_dict parses every field correctly."""
        t = script_run_cli.Task.from_dict(minimal_task_dict)
        assert t.id == "T-99"
        assert t.skill == "fix"
        assert t.primary_module == "pkg.mod"
        assert t.risk_tier == "high"
        assert len(t.queries) == 1
        assert t.queries[0].cmd == "rdeps"

    def test_task_from_dict_empty_queries(self, script_run_cli: Any, minimal_task_dict: dict) -> None:
        """Task.from_dict with empty queries list produces empty list."""
        minimal_task_dict["queries"] = []
        t = script_run_cli.Task.from_dict(minimal_task_dict)
        assert t.queries == []

    def test_scenario_result_notes_default_empty(self, script_run_cli: Any) -> None:
        """ScenarioResult notes field defaults to empty string."""
        r = script_run_cli.ScenarioResult(
            scenario="C1",
            name="x",
            suite="calls",
            passed=True,
            result={},
            threshold={},
        )
        assert r.notes == ""

    def test_timing_stats_stores_all_fields(self, script_run_cli: Any) -> None:
        """TimingStats round-trips all timing fields."""
        ts = script_run_cli.TimingStats(min_ms=1.0, median_ms=2.5, max_ms=10.0, n=5)
        assert ts.min_ms == 1.0
        assert ts.median_ms == 2.5
        assert ts.max_ms == 10.0
        assert ts.n == 5

    def test_accuracy_stats_stores_all_fields(self, script_run_cli: Any) -> None:
        """AccuracyStats round-trips precision/recall and TP/FP/FN."""
        a = script_run_cli.AccuracyStats(
            precision=0.9,
            recall=0.8,
            tp=9,
            fp=1,
            fn=2,
            fp_modules=["a.b"],
            fn_modules=["c.d"],
        )
        assert a.precision == pytest.approx(0.9)
        assert a.recall == pytest.approx(0.8)
        assert a.tp == 9
        assert a.fp_modules == ["a.b"]

    def test_suite_stats_defaults_zero(self, script_run_cli: Any) -> None:
        """SuiteStats initialises all counters to zero."""
        s = script_run_cli.SuiteStats()
        assert s.total == 0
        assert s.passed == 0
        assert s.failed == 0

    def test_validation_result_ok_has_empty_reason(self, script_run_cli: Any) -> None:
        """ValidationResult ok=True carries an empty reason string."""
        vr = script_run_cli.ValidationResult(ok=True, reason="")
        assert vr.ok is True
        assert vr.reason == ""

    def test_validation_result_fail_has_reason(self, script_run_cli: Any) -> None:
        """ValidationResult ok=False carries a non-empty reason."""
        vr = script_run_cli.ValidationResult(ok=False, reason="missing key")
        assert vr.ok is False
        assert "missing" in vr.reason


# ===========================================================================
# Task loading — load_tasks
# ===========================================================================


class TestLoadTasks:
    """Validate load_tasks contract against the real tasks-code.json file."""

    def test_load_tasks_returns_list_of_task_objects(self, script_run_cli: Any, real_tasks: list) -> None:
        """load_tasks with no filter returns Task instances for all records."""
        assert len(real_tasks) > 0
        assert all(isinstance(t, script_run_cli.Task) for t in real_tasks)

    def test_load_tasks_total_count_matches_file(self, script_run_cli: Any, real_tasks: list) -> None:
        """Task count matches number of objects in tasks-code.json."""
        with script_run_cli.TASKS_FILE.open() as f:
            raw = json.load(f)
        assert len(real_tasks) == len(raw)

    @pytest.mark.parametrize("skill", ["fix", "feature", "refactor"])
    def test_load_tasks_skill_filter_returns_matching_only(self, script_run_cli: Any, skill: str) -> None:
        """skill_filter keeps only tasks whose skill field matches.

        Args:
            skill: One of the three documented skill values.
        """
        filtered = script_run_cli.load_tasks(skill_filter=skill)
        assert len(filtered) > 0, f"Expected at least one '{skill}' task"
        assert all(t.skill == skill for t in filtered)

    def test_load_tasks_unknown_skill_filter_returns_empty(self, script_run_cli: Any) -> None:
        """skill_filter with an unknown value produces an empty list."""
        result = script_run_cli.load_tasks(skill_filter="nonexistent_skill")
        assert result == []

    def test_load_tasks_no_filter_includes_all_skills(self, script_run_cli: Any, real_tasks: list) -> None:
        """Unfiltered load contains tasks from all three skill groups."""
        skills = {t.skill for t in real_tasks}
        assert skills >= {"fix", "feature", "refactor"}

    def test_load_tasks_queries_are_query_objects(self, script_run_cli: Any, real_tasks: list) -> None:
        """Every query within every task is a Query dataclass instance."""
        for task in real_tasks:
            for q in task.queries:
                assert isinstance(q, script_run_cli.Query), f"Task {task.id}: expected Query, got {type(q)}"

    def test_load_tasks_primary_module_is_dotted_string(self, script_run_cli: Any, real_tasks: list) -> None:
        """primary_module for every task is a non-empty dotted string."""
        for task in real_tasks:
            assert "." in task.primary_module, f"Task {task.id} module not dotted: {task.primary_module}"

    def test_load_tasks_with_nonexistent_file_raises(self, script_run_cli: Any, tmp_path: Path) -> None:
        """TASKS_FILE pointing at a missing file raises FileNotFoundError."""
        with patch.object(script_run_cli, "TASKS_FILE", tmp_path / "missing.json"):
            with pytest.raises(FileNotFoundError):
                script_run_cli.load_tasks()

    def test_load_tasks_with_malformed_json_raises(self, script_run_cli: Any, tmp_path: Path) -> None:
        """TASKS_FILE containing invalid JSON raises json.JSONDecodeError."""
        bad = tmp_path / "bad.json"
        bad.write_text("{not valid json", encoding="utf-8")
        with patch.object(script_run_cli, "TASKS_FILE", bad):
            with pytest.raises(json.JSONDecodeError):
                script_run_cli.load_tasks()


# ===========================================================================
# Task loading — load_oss_tasks
# ===========================================================================


@pytest.fixture(name="oss_tasks_file")
def _oss_tasks_file(tmp_path: Path) -> Path:
    """Write a minimal flat-list tasks-bench.json for load_oss_tasks tests.

    The load_oss_tasks function documents that it returns ``list[dict]`` and
    calls ``json.load`` expecting a list directly.  This fixture provides a
    controlled flat-list file so tests are independent of the real file layout.

    Returns:
        Path to a temporary tasks-bench.json containing a flat list of tasks.

    Example:
        >>> tasks = json.loads(getfixture("oss_tasks_file").read_text(encoding="utf-8"))
        >>> [task["type"] for task in tasks]
        ['symbol_extraction', 'code_quality', 'real_issue', 'debug_from_trace']
    """
    tasks = [
        {"id": "SE-01", "type": "symbol_extraction", "ground_truth": {"qualified_name": "Foo.bar"}},
        {"id": "CQ-01", "type": "code_quality", "ground_truth": {"check": "undocumented"}},
        {"id": "RI-01", "type": "real_issue"},
        {"id": "DB-01", "type": "debug_from_trace"},
    ]
    p = tmp_path / "tasks-bench.json"
    p.write_text(json.dumps(tasks), encoding="utf-8")
    return p


class TestLoadOssTasks:
    """Validate load_oss_tasks contract (expects flat list JSON)."""

    def test_load_oss_tasks_returns_list(self, script_run_cli: Any, oss_tasks_file: Path) -> None:
        """load_oss_tasks returns a list when the file contains a flat list."""
        with patch.object(script_run_cli, "OSS_TASKS_FILE", oss_tasks_file):
            result = script_run_cli.load_oss_tasks()
        assert isinstance(result, list)

    def test_load_oss_tasks_absent_file_returns_empty(self, script_run_cli: Any, tmp_path: Path) -> None:
        """load_oss_tasks returns [] when OSS_TASKS_FILE does not exist."""
        with patch.object(script_run_cli, "OSS_TASKS_FILE", tmp_path / "missing.json"):
            result = script_run_cli.load_oss_tasks()
        assert result == []

    @pytest.mark.parametrize("type_filter", ["symbol_extraction", "code_quality", "real_issue"])
    def test_load_oss_tasks_type_filter_matches_only(
        self, script_run_cli: Any, type_filter: str, oss_tasks_file: Path
    ) -> None:
        """type_filter keeps only tasks whose 'type' field matches.

        Args:
            type_filter: One of the documented task type values.
            oss_tasks_file: Path to fixture tasks file.
        """
        with patch.object(script_run_cli, "OSS_TASKS_FILE", oss_tasks_file):
            result = script_run_cli.load_oss_tasks(type_filter=type_filter)
        assert len(result) >= 1
        assert all(t.get("type") == type_filter for t in result)

    def test_load_oss_tasks_unknown_type_returns_empty(self, script_run_cli: Any, oss_tasks_file: Path) -> None:
        """type_filter with an unknown value produces an empty list."""
        with patch.object(script_run_cli, "OSS_TASKS_FILE", oss_tasks_file):
            result = script_run_cli.load_oss_tasks(type_filter="does_not_exist")
        assert result == []

    def test_load_oss_tasks_no_filter_includes_all_types(self, script_run_cli: Any, oss_tasks_file: Path) -> None:
        """Unfiltered load returns all tasks across types."""
        with patch.object(script_run_cli, "OSS_TASKS_FILE", oss_tasks_file):
            all_tasks = script_run_cli.load_oss_tasks()
        types = {t.get("type") for t in all_tasks}
        assert "symbol_extraction" in types
        assert "code_quality" in types

    def test_load_oss_tasks_result_dicts_have_id_field(self, script_run_cli: Any, oss_tasks_file: Path) -> None:
        """Every returned dict has a non-empty 'id' field."""
        with patch.object(script_run_cli, "OSS_TASKS_FILE", oss_tasks_file):
            tasks = script_run_cli.load_oss_tasks()
        for t in tasks:
            assert t.get("id"), f"Task missing 'id': {t}"

    def test_load_oss_tasks_with_flat_list_json(self, script_run_cli: Any, tmp_path: Path) -> None:
        """Flat-list JSON is returned as-is without wrapping.

        Args:
            tmp_path: Pytest temporary directory.
        """
        flat = [{"id": "X-01", "type": "code_quality"}]
        f = tmp_path / "flat.json"
        f.write_text(json.dumps(flat), encoding="utf-8")
        with patch.object(script_run_cli, "OSS_TASKS_FILE", f):
            result = script_run_cli.load_oss_tasks()
        assert result == flat


# ===========================================================================
# Path helpers
# ===========================================================================


class TestPathToModule:
    """Validate path_to_module conversion logic."""

    @pytest.mark.parametrize(
        "path,repo_root,expected",
        [
            # Standard layout
            pytest.param("/repo/pkg/mod.py", "/repo", "pkg.mod", id="repo-pkg-mod.py"),
            # src/ layout — strip prefix
            pytest.param("/repo/src/pkg/mod.py", "/repo", "pkg.mod", id="repo-src-pkg-mod.py"),
            # __init__.py collapses to package
            pytest.param("/repo/pkg/__init__.py", "/repo", "pkg", id="repo-pkg-__init__.py"),
            # Nested __init__.py
            pytest.param("/repo/pkg/sub/__init__.py", "/repo", "pkg.sub", id="repo-pkg-sub-__init__.py"),
            # src/ layout with __init__.py
            pytest.param("/repo/src/pkg/__init__.py", "/repo", "pkg", id="repo-src-pkg-__init__.py"),
        ],
    )
    def test_converts_py_path_to_dotted_module(
        self, script_run_cli: Any, path: str, repo_root: str, expected: str
    ) -> None:
        """.py path is converted to the expected dotted module name.

        Args:
            path: Filesystem path to a Python source file.
            repo_root: Repository root used as os.path.relpath base.
            expected: Expected dotted module name.
        """
        assert script_run_cli.path_to_module(path, repo_root) == expected

    @pytest.mark.parametrize("path", ["/repo/README.md", "/repo/data.csv", "/repo/LICENSE"])
    def test_returns_none_for_non_py_files(self, script_run_cli: Any, path: str) -> None:
        """non-.py paths return None.

        Args:
            path: Path to a non-Python file.
        """
        assert script_run_cli.path_to_module(path, "/repo") is None


class TestModuleToGrepPattern:
    """Validate module_to_grep_pattern output contract."""

    @pytest.mark.parametrize(
        "module,expected",
        [
            pytest.param("foo.bar", r"from foo.bar import\|import foo.bar", id="foo.bar"),
            pytest.param("pkg", r"from pkg import\|import pkg", id="pkg"),
            pytest.param(
                "lightning.pytorch.trainer.trainer",
                r"from lightning.pytorch.trainer.trainer import\|import lightning.pytorch.trainer.trainer",
                id="lightning.pytorch.trainer.trainer",
            ),
        ],
    )
    def test_pattern_contains_both_import_forms(self, script_run_cli: Any, module: str, expected: str) -> None:
        r"""Pattern contains 'from X import' and 'import X' forms joined by \|.

        Args:
            module: Dotted module name.
            expected: Expected grep alternation string.
        """
        assert script_run_cli.module_to_grep_pattern(module) == expected


class TestModuleToPackage:
    """Validate module_to_package parent extraction."""

    @pytest.mark.parametrize(
        "module,expected",
        [
            pytest.param("foo.bar.baz", "foo.bar", id="foo.bar.baz"),
            pytest.param("foo.bar", "foo", id="foo.bar"),
            pytest.param("foo", None, id="foo"),
        ],
    )
    def test_returns_parent_package_or_none(self, script_run_cli: Any, module: str, expected: str | None) -> None:
        """Parent package extracted correctly for nested and top-level modules.

        Args:
            module: Dotted module name.
            expected: Expected parent package, or None for top-level.
        """
        assert script_run_cli.module_to_package(module) == expected


# ===========================================================================
# Accuracy scoring — compute_precision_recall
# ===========================================================================


class TestComputePrecisionRecall:
    """Validate compute_precision_recall contract from the docs."""

    @pytest.mark.parametrize(
        "codemap_set,grep_set,expected_precision,expected_recall,expected_tp,expected_fp,expected_fn",
        [
            # Perfect agreement
            pytest.param({"a", "b", "c"}, {"a", "b", "c"}, 1.0, 1.0, 3, 0, 0, id="a-b-c"),
            # Codemap finds extra (FP)
            pytest.param({"a", "b", "x"}, {"a", "b"}, 2 / 3, 1.0, 2, 1, 0, id="a-b-x"),
            # Codemap misses some (FN)
            pytest.param({"a"}, {"a", "b"}, 1.0, 0.5, 1, 0, 1, id="a-a-b"),
            # No overlap at all
            pytest.param({"x"}, {"y"}, 0.0, 0.0, 0, 1, 1, id="x"),
            # Single matching element
            pytest.param({"a"}, {"a"}, 1.0, 1.0, 1, 0, 0, id="a-a"),
        ],
    )
    def test_precision_recall_values(
        self,
        script_run_cli: Any,
        codemap_set: set,
        grep_set: set,
        expected_precision: float,
        expected_recall: float,
        expected_tp: int,
        expected_fp: int,
        expected_fn: int,
    ) -> None:
        """Precision/recall computed correctly for documented TP/FP/FN formula.

        Args:
            codemap_set: Modules returned by scan-query rdeps.
            grep_set: Modules found by the grep baseline.
            expected_precision: Expected precision value.
            expected_recall: Expected recall value.
            expected_tp: Expected true positive count.
            expected_fp: Expected false positive count.
            expected_fn: Expected false negative count.
        """
        stats = script_run_cli.compute_precision_recall(codemap_set, grep_set)
        assert stats.precision == pytest.approx(expected_precision, abs=1e-4)
        assert stats.recall == pytest.approx(expected_recall, abs=1e-4)
        assert stats.tp == expected_tp
        assert stats.fp == expected_fp
        assert stats.fn == expected_fn

    def test_empty_codemap_set_precision_defaults_to_one(self, script_run_cli: Any) -> None:
        """Empty codemap set yields precision=1.0 per documented default."""
        stats = script_run_cli.compute_precision_recall(set(), {"a", "b"})
        assert stats.precision == pytest.approx(1.0)

    def test_empty_grep_set_recall_defaults_to_one(self, script_run_cli: Any) -> None:
        """Empty grep set yields recall=1.0 per documented default."""
        stats = script_run_cli.compute_precision_recall({"a", "b"}, set())
        assert stats.recall == pytest.approx(1.0)

    def test_both_empty_returns_perfect_scores(self, script_run_cli: Any) -> None:
        """Both sets empty yields precision=1.0 and recall=1.0."""
        stats = script_run_cli.compute_precision_recall(set(), set())
        assert stats.precision == pytest.approx(1.0)
        assert stats.recall == pytest.approx(1.0)

    def test_fp_modules_sorted(self, script_run_cli: Any) -> None:
        """fp_modules list is sorted alphabetically."""
        stats = script_run_cli.compute_precision_recall({"z", "a", "m"}, set())
        assert stats.fp_modules == sorted(stats.fp_modules)

    def test_fn_modules_sorted(self, script_run_cli: Any) -> None:
        """fn_modules list is sorted alphabetically."""
        stats = script_run_cli.compute_precision_recall(set(), {"z", "a", "m"})
        assert stats.fn_modules == sorted(stats.fn_modules)

    def test_precision_rounded_to_four_places(self, script_run_cli: Any) -> None:
        """Precision is rounded to 4 decimal places as documented."""
        # 2/3 = 0.6667
        stats = script_run_cli.compute_precision_recall({"a", "b", "c"}, {"a", "b"})
        assert stats.precision == round(stats.precision, 4)


# ===========================================================================
# Verdict computation — compute_verdict
# ===========================================================================


class TestComputeVerdict:
    """Validate compute_verdict against documented thresholds."""

    def test_all_pass_returns_pass(self, script_run_cli: Any) -> None:
        """All scenarios passed → verdict is PASS."""
        results = [_make_scenario(script_run_cli, True) for _ in range(3)]
        assert script_run_cli.compute_verdict(results) == "PASS"

    def test_all_fail_returns_fail(self, script_run_cli: Any) -> None:
        """All scenarios failed → verdict is FAIL."""
        results = [_make_scenario(script_run_cli, False) for _ in range(4)]
        assert script_run_cli.compute_verdict(results) == "FAIL"

    def test_empty_results_returns_fail(self, script_run_cli: Any) -> None:
        """Empty results list → FAIL per documented behaviour."""
        assert script_run_cli.compute_verdict([]) == "FAIL"

    @pytest.mark.parametrize(
        "n_pass,n_total,expected",
        [
            pytest.param(3, 4, "PARTIAL", id="3"),  # 75% >= 50% but not 100%
            pytest.param(1, 2, "PARTIAL", id="1-2"),  # exactly 50%
            pytest.param(1, 3, "FAIL", id="1-3"),  # 33% < 50%
            pytest.param(0, 3, "FAIL", id="0"),  # 0%
        ],
    )
    def test_partial_and_fail_boundary(self, script_run_cli: Any, n_pass: int, n_total: int, expected: str) -> None:
        """Verdict boundary at 50% pass rate per documented thresholds.

        Args:
            n_pass: Number of passing scenarios.
            n_total: Total number of scenarios.
            expected: Expected verdict string.
        """
        results = [_make_scenario(script_run_cli, i < n_pass) for i in range(n_total)]
        assert script_run_cli.compute_verdict(results) == expected


# ===========================================================================
# JSON validators
# ===========================================================================


class TestValidateCentralJson:
    """Validate validate_central_json contract."""

    def test_valid_central_response_returns_ok(self, script_run_cli: Any) -> None:
        """Well-formed central response with rdep_count → ok=True."""
        data = {"central": [{"name": "foo.bar", "rdep_count": 5}]}
        result = script_run_cli.validate_central_json(data)
        assert result.ok is True
        assert result.reason == ""

    def test_missing_central_key_returns_not_ok(self, script_run_cli: Any) -> None:
        """Response missing 'central' key → ok=False with reason."""
        result = script_run_cli.validate_central_json({"other_key": []})
        assert result.ok is False
        assert "central" in result.reason

    def test_empty_central_list_returns_not_ok(self, script_run_cli: Any) -> None:
        """'central' key present but empty list → ok=False."""
        result = script_run_cli.validate_central_json({"central": []})
        assert result.ok is False

    def test_central_not_a_list_returns_not_ok(self, script_run_cli: Any) -> None:
        """'central' value is not a list → ok=False."""
        result = script_run_cli.validate_central_json({"central": "not a list"})
        assert result.ok is False

    def test_item_missing_rdep_count_returns_not_ok(self, script_run_cli: Any) -> None:
        """Central item without rdep_count → ok=False with reason."""
        data = {"central": [{"name": "foo", "other": 1}]}
        result = script_run_cli.validate_central_json(data)
        assert result.ok is False
        assert "rdep_count" in result.reason

    def test_multiple_valid_items_returns_ok(self, script_run_cli: Any) -> None:
        """Multiple items all having rdep_count → ok=True."""
        data = {"central": [{"rdep_count": i} for i in range(5)]}
        result = script_run_cli.validate_central_json(data)
        assert result.ok is True

    @pytest.mark.parametrize(
        "data,reason_fragment",
        [
            pytest.param([], "object", id="punctuation"),
            pytest.param({"central": [42]}, "object", id="central-42"),
            pytest.param({"central": [{"rdep_count": "5"}]}, "int", id="central-rdep_count-5"),
        ],
    )
    def test_wrong_type_payloads_return_not_ok(self, script_run_cli: Any, data: Any, reason_fragment: str) -> None:
        """Wrong JSON shapes fail validation instead of passing structurally."""
        result = script_run_cli.validate_central_json(data)
        assert result.ok is False
        assert reason_fragment in result.reason


class TestValidateRdepsJson:
    """Validate validate_rdeps_json contract."""

    def test_valid_rdeps_response_returns_ok(self, script_run_cli: Any) -> None:
        """Response with imported_by and module keys → ok=True."""
        data = {"imported_by": ["a.b", "c.d"], "module": "foo.bar"}
        result = script_run_cli.validate_rdeps_json(data)
        assert result.ok is True

    @pytest.mark.parametrize(
        "data,expected_reason_fragment",
        [
            pytest.param({"module": "foo"}, "imported_by", id="module-foo"),  # missing imported_by
            pytest.param({"imported_by": []}, "module", id="imported_by"),  # missing module
            pytest.param({}, "imported_by", id="punctuation"),  # both missing — first check wins
        ],
    )
    def test_missing_keys_return_not_ok(self, script_run_cli: Any, data: dict, expected_reason_fragment: str) -> None:
        """Missing required keys produce ok=False with the key name in reason.

        Args:
            data: Incomplete response dict.
            expected_reason_fragment: Substring expected in the reason string.
        """
        result = script_run_cli.validate_rdeps_json(data)
        assert result.ok is False
        assert expected_reason_fragment in result.reason

    @pytest.mark.parametrize(
        "data,expected_reason_fragment",
        [
            pytest.param([], "object", id="punctuation"),
            pytest.param({"imported_by": "a.b", "module": "foo"}, "list", id="imported_by-a.b-module-foo"),
            pytest.param({"imported_by": [], "module": 42}, "string", id="imported_by-module-42"),
        ],
    )
    def test_wrong_type_payloads_return_not_ok(
        self, script_run_cli: Any, data: Any, expected_reason_fragment: str
    ) -> None:
        """Wrong JSON value types produce ok=False with a concrete reason."""
        result = script_run_cli.validate_rdeps_json(data)
        assert result.ok is False
        assert expected_reason_fragment in result.reason


class TestValidateDepsJson:
    """Validate validate_deps_json contract."""

    def test_valid_deps_response_returns_ok(self, script_run_cli: Any) -> None:
        """Response with direct_imports and module keys → ok=True."""
        data = {"direct_imports": ["x.y"], "module": "foo.bar"}
        result = script_run_cli.validate_deps_json(data)
        assert result.ok is True

    @pytest.mark.parametrize(
        "data,expected_reason_fragment",
        [
            pytest.param({"module": "foo"}, "direct_imports", id="module-foo"),
            pytest.param({"direct_imports": []}, "module", id="direct_imports"),
            pytest.param({}, "direct_imports", id="punctuation"),
        ],
    )
    def test_missing_keys_return_not_ok(self, script_run_cli: Any, data: dict, expected_reason_fragment: str) -> None:
        """Missing required keys produce ok=False with the key name in reason.

        Args:
            data: Incomplete response dict.
            expected_reason_fragment: Substring expected in the reason string.
        """
        result = script_run_cli.validate_deps_json(data)
        assert result.ok is False
        assert expected_reason_fragment in result.reason

    @pytest.mark.parametrize(
        "data,expected_reason_fragment",
        [
            pytest.param([], "object", id="punctuation"),
            pytest.param({"direct_imports": "x.y", "module": "foo"}, "list", id="direct_imports-x.y-module-foo"),
            pytest.param({"direct_imports": [], "module": 42}, "string", id="direct_imports-module-42"),
        ],
    )
    def test_wrong_type_payloads_return_not_ok(
        self, script_run_cli: Any, data: Any, expected_reason_fragment: str
    ) -> None:
        """Wrong JSON value types produce ok=False with a concrete reason."""
        result = script_run_cli.validate_deps_json(data)
        assert result.ok is False
        assert expected_reason_fragment in result.reason


# ===========================================================================
# run_scan_query — subprocess wrapper
# ===========================================================================


class TestRunScanQuery:
    """Validate run_scan_query subprocess contract via mocked subprocess.run."""

    def _fake_bin(self, tmp_path: Path) -> Path:
        """Create a placeholder binary file for path resolution.

        Args:
            tmp_path: Pytest temporary directory.

        Returns:
            Path to a file named 'scan-query'.
        """
        p = tmp_path / "scan-query"
        p.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        return p

    def test_returns_parsed_json_on_success(self, script_run_cli: Any, tmp_path: Path) -> None:
        """Zero-exit scan-query with valid JSON stdout returns parsed dict."""
        payload = {"imported_by": ["a.b"], "module": "foo"}
        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = json.dumps(payload)

        with patch.object(script_run_cli, "_run", return_value=fake_result):
            result = script_run_cli.run_scan_query(
                self._fake_bin(tmp_path),
                ["rdeps", "foo"],
                tmp_path / "index.json",
                tmp_path,
            )
        assert result == payload

    def test_returns_none_on_nonzero_exit(self, script_run_cli: Any, tmp_path: Path) -> None:
        """Non-zero returncode from scan-query yields None."""
        fake_result = MagicMock()
        fake_result.returncode = 1
        fake_result.stdout = ""

        with patch.object(script_run_cli, "_run", return_value=fake_result):
            result = script_run_cli.run_scan_query(
                self._fake_bin(tmp_path),
                ["rdeps", "missing"],
                tmp_path / "index.json",
                tmp_path,
            )
        assert result is None

    def test_returns_none_on_json_decode_error(self, script_run_cli: Any, tmp_path: Path) -> None:
        """Scan-query returns non-JSON stdout → None, no exception raised."""
        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = "not json at all"

        with patch.object(script_run_cli, "_run", return_value=fake_result):
            result = script_run_cli.run_scan_query(
                self._fake_bin(tmp_path),
                ["central", "--top", "5"],
                tmp_path / "index.json",
                tmp_path,
            )
        assert result is None

    def test_returns_none_on_timeout(self, script_run_cli: Any, tmp_path: Path) -> None:
        """Handle a command timeout without raising an exception."""
        with patch.object(script_run_cli, "_run", side_effect=subprocess.TimeoutExpired(cmd=[], timeout=30)):
            result = script_run_cli.run_scan_query(
                self._fake_bin(tmp_path),
                ["central"],
                tmp_path / "index.json",
                tmp_path,
            )
        assert result is None

    def test_returns_none_on_os_error(self, script_run_cli: Any, tmp_path: Path) -> None:
        """Handle an unavailable command binary without raising an exception."""
        with patch.object(script_run_cli, "_run", side_effect=OSError("file not found")):
            result = script_run_cli.run_scan_query(
                self._fake_bin(tmp_path),
                ["rdeps", "foo"],
                tmp_path / "index.json",
                tmp_path,
            )
        assert result is None

    def test_passes_index_flag_to_subprocess(self, script_run_cli: Any, tmp_path: Path) -> None:
        """run_scan_query always injects ``--index <path>`` into the command."""
        index_path = tmp_path / "my-index.json"
        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = json.dumps({"ok": True})

        captured: list[list[str]] = []

        def _capture_run(cmd: list[str], **kwargs: Any) -> MagicMock:
            """Record argv and return the configured subprocess result."""
            captured.append(cmd)
            return fake_result

        with patch.object(script_run_cli, "_run", side_effect=_capture_run):
            script_run_cli.run_scan_query(
                self._fake_bin(tmp_path),
                ["rdeps", "foo.bar"],
                index_path,
                tmp_path,
            )

        assert len(captured) == 1
        cmd = captured[0]
        assert "--index" in cmd
        idx = cmd.index("--index")
        assert str(index_path.resolve()) in cmd[idx + 1]

    def test_subcommand_appended_after_index(self, script_run_cli: Any, tmp_path: Path) -> None:
        """Subcommand args appear after the ``--index`` flag in the assembled command."""
        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = json.dumps({})
        captured: list[list[str]] = []

        def _capture(cmd: list[str], **kwargs: Any) -> MagicMock:
            """Record the subprocess invocation and return the fixture result."""
            captured.append(cmd)
            return fake_result

        with patch.object(script_run_cli, "_run", side_effect=_capture):
            script_run_cli.run_scan_query(
                self._fake_bin(tmp_path),
                ["deps", "some.module"],
                tmp_path / "idx.json",
                tmp_path,
            )

        cmd = captured[0]
        assert "deps" in cmd
        assert "some.module" in cmd

    def test_exact_command_order_and_cwd_propagated(self, script_run_cli: Any, tmp_path: Path) -> None:
        """Subprocess command order and cwd are stable for scan-query invocations."""
        scan_query_bin = self._fake_bin(tmp_path)
        index_path = tmp_path / "idx.json"
        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = json.dumps({})
        captured: list[tuple[list[str], dict[str, Any]]] = []

        def _capture(cmd: list[str], **kwargs: Any) -> MagicMock:
            """Record the subprocess invocation and return the fixture result."""
            captured.append((cmd, kwargs))
            return fake_result

        with patch.object(script_run_cli, "_run", side_effect=_capture):
            script_run_cli.run_scan_query(scan_query_bin, ["rdeps", "pkg.mod"], index_path, tmp_path)

        assert captured == [
            (
                ["python3", str(scan_query_bin.resolve()), "--index", str(index_path.resolve()), "rdeps", "pkg.mod"],
                {"cwd": str(tmp_path)},
            )
        ]

    @pytest.mark.parametrize(
        "side_effect,expected_error",
        [
            pytest.param(
                subprocess.TimeoutExpired(cmd=[], timeout=30), "timeout", id="subprocess.timeoutexpired-cmd-timeout-30"
            ),
            pytest.param(OSError("missing executable"), "os error", id="oserror-missing-executable"),
        ],
    )
    def test_result_wrapper_reports_timeout_and_os_error(
        self, script_run_cli: Any, tmp_path: Path, side_effect: Exception, expected_error: str
    ) -> None:
        """Result wrapper preserves failure reasons that run_scan_query collapses to None."""
        with patch.object(script_run_cli, "_run", side_effect=side_effect):
            result = script_run_cli.run_scan_query_result(
                self._fake_bin(tmp_path),
                ["rdeps", "foo"],
                tmp_path / "index.json",
                tmp_path,
            )

        assert result.data is None
        assert result.error is not None
        assert expected_error in result.error


# ===========================================================================
# resolve_index_path
# ===========================================================================


class TestResolveIndexPath:
    """Validate resolve_index_path resolution order."""

    def test_explicit_arg_returned_directly(self, script_run_cli: Any, tmp_path: Path) -> None:
        """When arg is provided it is returned without further resolution."""
        result = script_run_cli.resolve_index_path("/custom/path/index.json", tmp_path)
        assert result == Path("/custom/path/index.json")

    def test_finds_index_in_cache_codemap_by_repo_name(self, script_run_cli: Any, tmp_path: Path) -> None:
        """Index at <repo>/.cache/codemap/<name>.json is discovered automatically."""
        # Create the directory structure matching the lookup logic.
        cache_dir = tmp_path / ".cache" / "codemap"
        cache_dir.mkdir(parents=True)
        index_file = cache_dir / f"{tmp_path.name}.json"
        index_file.write_text("{}", encoding="utf-8")

        result = script_run_cli.resolve_index_path(None, tmp_path)
        assert result == index_file

    def test_finds_index_after_stripping_master_suffix(self, script_run_cli: Any, tmp_path: Path) -> None:
        """Repo named '<name>-master' resolves to '<name>.json' in cache."""
        # Simulate a repo dir named pytorch-lightning-master
        repo = tmp_path / "pytorch-lightning-master"
        repo.mkdir()
        cache_dir = repo / ".cache" / "codemap"
        cache_dir.mkdir(parents=True)
        index_file = cache_dir / "pytorch-lightning.json"
        index_file.write_text("{}", encoding="utf-8")

        result = script_run_cli.resolve_index_path(None, repo)
        assert result == index_file

    def test_falls_back_to_first_json_in_cache_codemap(self, script_run_cli: Any, tmp_path: Path) -> None:
        """When name-based lookup fails, first *.json in .cache/codemap/ is used."""
        cache_dir = tmp_path / ".cache" / "codemap"
        cache_dir.mkdir(parents=True)
        fallback = cache_dir / "anything.json"
        fallback.write_text("{}", encoding="utf-8")

        # repo name won't match 'anything'
        result = script_run_cli.resolve_index_path(None, tmp_path)
        # either the name-based default path or 'anything.json' — both are valid
        # but the file 'anything.json' exists so it should be found first
        assert result.exists()

    def test_returns_default_path_when_no_index_found(self, script_run_cli: Any, tmp_path: Path) -> None:
        """When no index exists the function returns the canonical default path."""
        # No .cache/ dir created — lookup finds nothing.
        result = script_run_cli.resolve_index_path(None, tmp_path)
        # Should be a path under tmp_path/.cache/codemap/
        assert ".cache" in str(result)
        assert result.suffix == ".json"


# ===========================================================================
# resolve_repo_path
# ===========================================================================


class TestResolveRepoPath:
    """Validate resolve_repo_path resolution order."""

    def test_explicit_existing_dir_returns_path(self, script_run_cli: Any, tmp_path: Path) -> None:
        """Valid existing directory arg is returned as Path."""
        result = script_run_cli.resolve_repo_path(str(tmp_path))
        assert result == tmp_path

    def test_explicit_nonexistent_dir_returns_none(self, script_run_cli: Any, tmp_path: Path) -> None:
        """Arg pointing to a non-existent directory returns None."""
        missing = str(tmp_path / "no_such_dir")
        result = script_run_cli.resolve_repo_path(missing)
        assert result is None

    def test_env_var_used_when_arg_absent(
        self, script_run_cli: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PYTORCH_LIGHTNING_PATH env var is consulted when arg is None."""
        monkeypatch.setenv("PYTORCH_LIGHTNING_PATH", str(tmp_path))
        result = script_run_cli.resolve_repo_path(None)
        assert result == tmp_path

    def test_env_var_nonexistent_dir_falls_through(
        self, script_run_cli: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PYTORCH_LIGHTNING_PATH pointing to missing dir falls through to local check."""
        monkeypatch.setenv("PYTORCH_LIGHTNING_PATH", str(tmp_path / "no_such"))
        # Patch cwd so ./pytorch-lightning also doesn't exist.
        with patch("pathlib.Path.is_dir", return_value=False):
            result = script_run_cli.resolve_repo_path(None)
        assert result is None

    def test_no_arg_no_env_no_local_returns_none(self, script_run_cli: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """All three resolution sources absent → None returned."""
        monkeypatch.delenv("PYTORCH_LIGHTNING_PATH", raising=False)
        # Make Path.is_dir always False to suppress ./pytorch-lightning fallback.
        with patch("pathlib.Path.is_dir", return_value=False):
            result = script_run_cli.resolve_repo_path(None)
        assert result is None


# ===========================================================================
# find_codemap_bin
# ===========================================================================


class TestFindCodemapBin:
    """Validate find_codemap_bin resolution logic."""

    def test_returns_none_when_not_on_path_and_no_plugin_root(
        self, script_run_cli: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Binary absent from PATH and no plugin_root → None."""
        with patch("shutil.which", return_value=None):
            result = script_run_cli.find_codemap_bin("scan-query", plugin_root=None)
        assert result is None

    def test_finds_binary_via_shutil_which(self, script_run_cli: Any) -> None:
        """Binary present on PATH is returned as a Path."""
        with patch("shutil.which", return_value="/usr/local/bin/scan-query"):
            result = script_run_cli.find_codemap_bin("scan-query", plugin_root=None)
        assert result == Path("/usr/local/bin/scan-query")

    def test_finds_binary_in_plugin_root_when_not_on_path(self, script_run_cli: Any, tmp_path: Path) -> None:
        """Binary not on PATH but present under plugin_root/plugins/codemap-py/bin/."""
        bin_dir = tmp_path / "plugins" / "codemap-py" / "bin"
        bin_dir.mkdir(parents=True)
        binary = bin_dir / "scan-query"
        binary.write_text("#!/usr/bin/env python3\n")

        with patch("shutil.which", return_value=None):
            result = script_run_cli.find_codemap_bin("scan-query", plugin_root=tmp_path)
        assert result == binary

    def test_returns_none_when_plugin_root_lacks_binary(self, script_run_cli: Any, tmp_path: Path) -> None:
        """plugin_root provided but binary file absent → None."""
        with patch("shutil.which", return_value=None):
            result = script_run_cli.find_codemap_bin("scan-query", plugin_root=tmp_path)
        assert result is None

    def test_legacy_codemap_path_never_selected(self, script_run_cli: Any, tmp_path: Path) -> None:
        """Only the retired plugins/codemap/bin/ holds the binary → None.

        The resolver targets the renamed ``codemap-py`` identity exclusively, so a stale pre-rename checkout must not
        yield a false-green binary path.
        """
        legacy = tmp_path / "plugins" / "codemap" / "bin"
        legacy.mkdir(parents=True)
        (legacy / "scan-query").write_text("#!/usr/bin/env python3\n")

        with patch("shutil.which", return_value=None):
            result = script_run_cli.find_codemap_bin("scan-query", plugin_root=tmp_path)
        assert result is None


# ===========================================================================
# THRESHOLDS config sanity
# ===========================================================================


class TestThresholdsConfig:
    """Smoke-check that THRESHOLDS dict has expected keys and numeric values."""

    @pytest.mark.parametrize(
        "key,sub_key,lo,hi",
        [
            pytest.param("C1", "coverage_gap_min", 0.0, 1.0, id="c1"),
            pytest.param("C2", "infeasible_path_fraction_min", 0.0, 1.0, id="c2"),
            pytest.param("C3", "leverage_ratio_min", 1.0, 100.0, id="c3"),
            pytest.param("A1", "precision_min", 0.0, 1.0, id="a1-precision_min"),
            pytest.param("A1", "recall_min", 0.0, 1.0, id="a1-recall_min"),
            pytest.param("A2", "precision_min", 0.0, 1.0, id="a2"),
            pytest.param("A3", "fp_rate_max", 0.0, 1.0, id="a3"),
            pytest.param("L1", "median_ms_max", 1.0, 10_000.0, id="l1"),
            pytest.param("L2", "median_ms_max", 1.0, 10_000.0, id="l2"),
            pytest.param("L3", "amortized_ms_max", 1.0, 10_000.0, id="l3"),
            pytest.param("L4", "speedup_min", 1.0, 1_000.0, id="l4"),
        ],
    )
    def test_threshold_value_in_expected_range(
        self, script_run_cli: Any, key: str, sub_key: str, lo: float, hi: float
    ) -> None:
        """Each threshold value is a float within a plausible range.

        Args:
            key: Top-level THRESHOLDS key (e.g. 'C1').
            sub_key: Sub-key within the threshold dict.
            lo: Lower bound for the plausible range.
            hi: Upper bound for the plausible range.
        """
        value = script_run_cli.THRESHOLDS[key][sub_key]
        assert isinstance(value, (int, float)), f"THRESHOLDS[{key!r}][{sub_key!r}] is not numeric"
        assert lo <= value <= hi, f"THRESHOLDS[{key!r}][{sub_key!r}]={value} not in [{lo}, {hi}]"

    def test_query_shape_thresholds_have_boolean_flags(self, script_run_cli: Any) -> None:
        """Query-shape thresholds have block_present and json_valid booleans."""
        for key in ("Q_fix", "Q_feature", "Q_refactor"):
            assert script_run_cli.THRESHOLDS[key]["block_present"] is True
            assert script_run_cli.THRESHOLDS[key]["json_valid"] is True


# ===========================================================================
# Integration — scan-query binary against a real repo (psf/requests)
# ===========================================================================


@pytest.mark.integration
@pytest.mark.live
@NETWORK_INTEGRATION
@RAW_CODEMAP_LAUNCHERS
class TestIntegrationScanQuery:
    """Run the scan-query binary against a cloned psf/requests repo.

    Requires runnable raw launchers and opt-in network access to clone the fixture repository.
    """

    def test_central_returns_valid_structure(
        self, script_run_cli: Any, scan_query_binary: Any, sample_repo: Any
    ) -> None:
        """Central subcommand returns a dict that passes validate_central_json."""
        repo, index = sample_repo
        data = script_run_cli.run_scan_query(scan_query_binary, ["central"], index, repo)
        assert data is not None, "run_scan_query returned None for 'central'"
        result = script_run_cli.validate_central_json(data)
        assert result.ok, f"validate_central_json failed: {result.reason}"

    def test_rdeps_known_module_returns_valid_structure(
        self, script_run_cli: Any, scan_query_binary: Any, sample_repo: Any
    ) -> None:
        """Rdeps for requests.api returns a dict that passes validate_rdeps_json."""
        repo, index = sample_repo
        data = script_run_cli.run_scan_query(scan_query_binary, ["rdeps", "requests.api"], index, repo)
        assert data is not None, "run_scan_query returned None for 'rdeps requests.api'"
        result = script_run_cli.validate_rdeps_json(data)
        assert result.ok, f"validate_rdeps_json failed: {result.reason}"

    def test_deps_known_module_returns_valid_structure(
        self, script_run_cli: Any, scan_query_binary: Any, sample_repo: Any
    ) -> None:
        """Deps for requests.api returns a dict that passes validate_deps_json."""
        repo, index = sample_repo
        data = script_run_cli.run_scan_query(scan_query_binary, ["deps", "requests.api"], index, repo)
        assert data is not None, "run_scan_query returned None for 'deps requests.api'"
        result = script_run_cli.validate_deps_json(data)
        assert result.ok, f"validate_deps_json failed: {result.reason}"

    def test_unknown_subcommand_returns_none(
        self, script_run_cli: Any, scan_query_binary: Any, sample_repo: Any
    ) -> None:
        """Unrecognised subcommand causes run_scan_query to return None."""
        repo, index = sample_repo
        data = script_run_cli.run_scan_query(scan_query_binary, ["not-a-real-subcommand"], index, repo)
        assert data is None

    def test_central_entries_have_name_and_rdep_count(
        self, script_run_cli: Any, scan_query_binary: Any, sample_repo: Any
    ) -> None:
        """Every entry in central list has both 'name' and 'rdep_count' fields."""
        repo, index = sample_repo
        data = script_run_cli.run_scan_query(scan_query_binary, ["central"], index, repo)
        assert data is not None
        for entry in data["central"]:
            assert "name" in entry, f"central entry missing 'name': {entry}"
            assert "rdep_count" in entry, f"central entry missing 'rdep_count': {entry}"


# ===========================================================================
# Integration — full suites against pytorch-lightning repo
# Requires a checked-out, indexed pytorch-lightning repository.
# ===========================================================================


@pytest.mark.integration
@RAW_CODEMAP_LAUNCHERS
@PYTORCH_LIGHTNING_CHECKOUT
@PYTORCH_LIGHTNING_INDEX
class TestIntegrationSuiteC:
    """Suite C: call-savings measurement against pytorch-lightning."""

    def test_returns_three_results(
        self, script_run_cli: Any, pytorch_lightning_repo: Any, scan_query_binary: Any, pytorch_lightning_index: Any
    ) -> None:
        """run_measure_calls returns exactly 3 ScenarioResult objects."""
        results = script_run_cli.run_measure_calls(pytorch_lightning_repo, scan_query_binary, pytorch_lightning_index)
        assert len(results) == 3

    def test_scenario_ids(
        self, script_run_cli: Any, pytorch_lightning_repo: Any, scan_query_binary: Any, pytorch_lightning_index: Any
    ) -> None:
        """The three results have scenario IDs C1, C2, C3."""
        results = script_run_cli.run_measure_calls(pytorch_lightning_repo, scan_query_binary, pytorch_lightning_index)
        assert {r.scenario for r in results} == {"C1", "C2", "C3"}

    def test_results_are_scenario_result_instances(
        self, script_run_cli: Any, pytorch_lightning_repo: Any, scan_query_binary: Any, pytorch_lightning_index: Any
    ) -> None:
        """Every item returned is a ScenarioResult dataclass."""
        results = script_run_cli.run_measure_calls(pytorch_lightning_repo, scan_query_binary, pytorch_lightning_index)
        for r in results:
            assert isinstance(r, script_run_cli.ScenarioResult)


@pytest.mark.integration
@RAW_CODEMAP_LAUNCHERS
@PYTORCH_LIGHTNING_CHECKOUT
@PYTORCH_LIGHTNING_INDEX
class TestIntegrationSuiteA:
    """Suite A: accuracy measurement against pytorch-lightning."""

    def test_returns_three_results(
        self, script_run_cli: Any, pytorch_lightning_repo: Any, scan_query_binary: Any, pytorch_lightning_index: Any
    ) -> None:
        """run_measure_accuracy returns exactly 3 ScenarioResult objects."""
        results = script_run_cli.run_measure_accuracy(
            pytorch_lightning_repo, scan_query_binary, pytorch_lightning_index
        )
        assert len(results) == 3

    def test_scenario_ids(
        self, script_run_cli: Any, pytorch_lightning_repo: Any, scan_query_binary: Any, pytorch_lightning_index: Any
    ) -> None:
        """The three results have scenario IDs A1, A2, A3."""
        results = script_run_cli.run_measure_accuracy(
            pytorch_lightning_repo, scan_query_binary, pytorch_lightning_index
        )
        assert {r.scenario for r in results} == {"A1", "A2", "A3"}


@pytest.mark.integration
@RAW_CODEMAP_LAUNCHERS
@PYTORCH_LIGHTNING_CHECKOUT
@PYTORCH_LIGHTNING_INDEX
class TestIntegrationSuiteL:
    """Suite L: latency measurement against pytorch-lightning."""

    def test_returns_four_results(
        self,
        script_run_cli: Any,
        pytorch_lightning_repo: Any,
        scan_query_binary: Any,
        pytorch_lightning_index: Any,
        scan_index_binary: Any,
    ) -> None:
        """run_measure_latency returns exactly 4 ScenarioResult objects."""
        results = script_run_cli.run_measure_latency(
            pytorch_lightning_repo, scan_query_binary, pytorch_lightning_index, scan_index_binary
        )
        assert len(results) == 4

    def test_scenario_ids(
        self,
        script_run_cli: Any,
        pytorch_lightning_repo: Any,
        scan_query_binary: Any,
        pytorch_lightning_index: Any,
        scan_index_binary: Any,
    ) -> None:
        """The four results have scenario IDs L1, L2, L3, L4."""
        results = script_run_cli.run_measure_latency(
            pytorch_lightning_repo, scan_query_binary, pytorch_lightning_index, scan_index_binary
        )
        assert {r.scenario for r in results} == {"L1", "L2", "L3", "L4"}


@pytest.mark.parametrize("times_out", [False, True])
def test_latency_index_build_restores_prebuilt_index_bytes(
    script_run_cli: Any,
    tmp_path: Path,
    times_out: bool,
) -> None:
    """Prevent completed or timed-out L3 scans from invalidating a frozen index."""
    index_path = tmp_path / "repo.json"
    index_path.write_bytes(b"locked-index")
    task = script_run_cli.Task(
        id="T-01",
        skill="fix",
        prompt="Inspect the target.",
        primary_module="pkg.mod",
        risk_tier="high",
        queries=[],
        ground_truth_keys=[],
    )
    timing = script_run_cli.TimingStats(min_ms=1.0, median_ms=1.0, max_ms=1.0, n=1)

    def _mutate_index(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Replace fixture index bytes and optionally simulate an indexing timeout."""
        index_path.write_bytes(b"rebuilt-index")
        if times_out:
            raise subprocess.TimeoutExpired(["scan-index"], 120)
        return subprocess.CompletedProcess([], 0, "", "")

    with (
        patch.object(script_run_cli, "load_tasks", return_value=[task]),
        patch.object(script_run_cli, "time_command", return_value=timing),
        patch.object(script_run_cli, "time_commands", return_value=timing),
        patch.object(script_run_cli.subprocess, "run", side_effect=_mutate_index),
    ):
        script_run_cli.run_measure_latency(
            tmp_path,
            tmp_path / "scan-query",
            index_path,
            tmp_path / "scan-index",
        )

    assert index_path.read_bytes() == b"locked-index"


@pytest.mark.integration
@RAW_CODEMAP_LAUNCHERS
@PYTORCH_LIGHTNING_CHECKOUT
@PYTORCH_LIGHTNING_INDEX
class TestIntegrationSuiteQ:
    """Suite Q: query-shape validation against pytorch-lightning."""

    def test_returns_three_results(
        self,
        script_run_cli: Any,
        pytorch_lightning_repo: Any,
        scan_query_binary: Any,
        pytorch_lightning_index: Any,
    ) -> None:
        """run_measure_query_shape returns exactly 3 ScenarioResult objects."""
        results = script_run_cli.run_measure_query_shape(
            REPO_ROOT, pytorch_lightning_repo, scan_query_binary, pytorch_lightning_index
        )
        assert len(results) == 3

    def test_scenario_ids(
        self,
        script_run_cli: Any,
        pytorch_lightning_repo: Any,
        scan_query_binary: Any,
        pytorch_lightning_index: Any,
    ) -> None:
        """The three results have scenario IDs Q_fix, Q_feature, Q_refactor."""
        results = script_run_cli.run_measure_query_shape(
            REPO_ROOT, pytorch_lightning_repo, scan_query_binary, pytorch_lightning_index
        )
        assert {r.scenario for r in results} == {"Q_fix", "Q_feature", "Q_refactor"}


# ===========================================================================
# Coverage-gap helpers — grep + AST importer verification
# ===========================================================================


@pytest.fixture(name="sample_pkg")
def _sample_pkg(tmp_path: Path) -> Path:
    """Build a package exercising literal, aliased, relative, and decoy imports of ``pkg.target``.

    Layout (all importers target ``pkg.target``):
      pkg/imp_from.py   from pkg.target import Thing   (literal — grep-visible)
      pkg/imp_plain.py  import pkg.target              (literal — grep-visible)
      pkg/imp_alias.py  import pkg.target as t         (aliased — grep-visible)
      pkg/imp_decoy.py  import pkg.target_helper       (decoy — must NOT match)
      pkg/unrelated.py  import os                      (no relation)
      pkg/bad.py        <syntax error>                 (unparsable)
      pkg/rel/rel_from.py  from ..target import Thing  (relative — grep-invisible)
      pkg/rel/rel_bare.py  from .. import target       (relative — grep-invisible)

    Returns:
        Path to the repository root containing the ``pkg`` package.

    Example:
        >>> root = getfixture("sample_pkg")
        >>> (root / "pkg/imp_decoy.py").read_text(encoding="utf-8").strip()
        'import pkg.target_helper'
    """
    pkg = tmp_path / "pkg"
    (pkg / "rel").mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "target.py").write_text("", encoding="utf-8")
    (pkg / "target_helper.py").write_text("", encoding="utf-8")
    (pkg / "imp_from.py").write_text("from pkg.target import Thing\n", encoding="utf-8")
    (pkg / "imp_plain.py").write_text("import pkg.target\n", encoding="utf-8")
    (pkg / "imp_alias.py").write_text("import pkg.target as t\n", encoding="utf-8")
    (pkg / "imp_decoy.py").write_text("import pkg.target_helper\n", encoding="utf-8")
    (pkg / "unrelated.py").write_text("import os\n", encoding="utf-8")
    (pkg / "bad.py").write_text("def (:\n", encoding="utf-8")
    (pkg / "rel" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "rel" / "rel_from.py").write_text("from ..target import Thing\n", encoding="utf-8")
    (pkg / "rel" / "rel_bare.py").write_text("from .. import target\n", encoding="utf-8")
    return tmp_path


class TestGrepImportersBoundary:
    """Validate grep_importers_boundary anchors to import statements only."""

    def test_uses_regular_package_chain_for_test_importer(self, script_run_cli: Any, tmp_path: Path) -> None:
        """A test package below a non-package directory keeps its import identity."""
        package = tmp_path / "tests" / "tests_fabric"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "test_target.py").write_text("from pkg.target import Thing\n", encoding="utf-8")

        grep_floor = script_run_cli.grep_importers_boundary(tmp_path, "pkg.target")
        stats = script_run_cli.score_rdeps_accuracy({"tests_fabric.test_target"}, grep_floor, "pkg.target", tmp_path)

        assert grep_floor == {"tests_fabric.test_target"}
        assert stats.precision == pytest.approx(1.0)
        assert stats.recall == pytest.approx(1.0)

    def test_matches_literal_imports_only(self, script_run_cli: Any, sample_pkg: Path) -> None:
        """Only literal, boundary-anchored importers are returned; relatives/decoys excluded."""
        result = script_run_cli.grep_importers_boundary(sample_pkg, "pkg.target")
        assert result == {"pkg.imp_from", "pkg.imp_plain", "pkg.imp_alias"}

    def test_excludes_substring_sibling(self, script_run_cli: Any, sample_pkg: Path) -> None:
        """'import pkg.target_helper' does not match a search for 'pkg.target'."""
        result = script_run_cli.grep_importers_boundary(sample_pkg, "pkg.target")
        assert "pkg.imp_decoy" not in result

    def test_no_match_returns_empty_set(self, script_run_cli: Any, sample_pkg: Path) -> None:
        """A module with no importers yields an empty set."""
        result = script_run_cli.grep_importers_boundary(sample_pkg, "pkg.nonexistent")
        assert result == set()


class TestModuleToSourceFile:
    """Validate module_to_source_file resolution."""

    def test_resolves_regular_module(self, script_run_cli: Any, sample_pkg: Path) -> None:
        """A dotted module resolves to its .py file."""
        assert script_run_cli.module_to_source_file("pkg.target", sample_pkg) == sample_pkg / "pkg" / "target.py"

    def test_resolves_package_init(self, script_run_cli: Any, sample_pkg: Path) -> None:
        """A package resolves to its __init__.py."""
        assert script_run_cli.module_to_source_file("pkg", sample_pkg) == sample_pkg / "pkg" / "__init__.py"

    def test_missing_module_returns_none(self, script_run_cli: Any, sample_pkg: Path) -> None:
        """An unknown module resolves to None."""
        assert script_run_cli.module_to_source_file("pkg.nope.here", sample_pkg) is None


class TestResolveRelative:
    """Validate _resolve_relative dotted-base resolution."""

    @pytest.mark.parametrize(
        "base,level,module,expected",
        [
            pytest.param("pkg.rel", 2, "target", "pkg.target", id="up-one-with-module"),
            pytest.param("pkg.rel", 1, None, "pkg.rel", id="current-package-bare"),
            pytest.param("pkg.rel", 1, "x", "pkg.rel.x", id="current-package-with-module"),
            pytest.param("pkg", 1, "target", "pkg.target", id="top-level-package"),
            pytest.param("a.b.c", 3, "d", "a.d", id="up-two"),
        ],
    )
    def test_resolution(self, script_run_cli: Any, base: str, level: int, module: str | None, expected: str) -> None:
        """Relative import base resolves per level and module suffix.

        Args:
            base: Base package of the importing file.
            level: Relative-import level (leading dots).
            module: Dotted suffix after the dots, or None.
            expected: Expected absolute dotted base.
        """
        assert script_run_cli._resolve_relative(base, level, module) == expected


class TestFileImportsModule:
    """Validate file_imports_module AST verification across import forms."""

    @pytest.mark.parametrize(
        "relpath",
        ["pkg/imp_from.py", "pkg/imp_plain.py", "pkg/imp_alias.py", "pkg/rel/rel_from.py", "pkg/rel/rel_bare.py"],
    )
    def test_true_importers(self, script_run_cli: Any, sample_pkg: Path, relpath: str) -> None:
        """Every genuine importer of pkg.target is confirmed by AST.

        Args:
            relpath: Path (relative to repo root) of the importing file.
        """
        assert script_run_cli.file_imports_module(sample_pkg / relpath, "pkg.target", sample_pkg) is True

    @pytest.mark.parametrize("relpath", ["pkg/imp_decoy.py", "pkg/unrelated.py", "pkg/bad.py"])
    def test_non_importers(self, script_run_cli: Any, sample_pkg: Path, relpath: str) -> None:
        """Non-importers, decoys, and unparsable files are rejected.

        Args:
            relpath: Path (relative to repo root) of the file that must not match.
        """
        assert script_run_cli.file_imports_module(sample_pkg / relpath, "pkg.target", sample_pkg) is False


class TestVerifyImporter:
    """Validate verify_importer end-to-end (module name → file → AST check)."""

    def test_verifies_relative_extra(self, script_run_cli: Any, sample_pkg: Path) -> None:
        """A grep-missed relative importer is verified as a true importer."""
        assert script_run_cli.verify_importer("pkg.rel.rel_from", "pkg.target", sample_pkg) is True

    def test_rejects_decoy(self, script_run_cli: Any, sample_pkg: Path) -> None:
        """A decoy that imports a sibling module is not verified."""
        assert script_run_cli.verify_importer("pkg.imp_decoy", "pkg.target", sample_pkg) is False

    def test_missing_candidate_module_returns_false(self, script_run_cli: Any, sample_pkg: Path) -> None:
        """A candidate module with no source file is rejected."""
        assert script_run_cli.verify_importer("pkg.ghost", "pkg.target", sample_pkg) is False


# ===========================================================================
# Infeasible-path measurement
# ===========================================================================


class TestMeasureInfeasiblePaths:
    """Validate _measure_infeasible_paths direct-edge detection."""

    def _task(self, script_cli: Any, frm: str, to: str) -> Any:
        """Build a single-path task.

        Args:
            script_cli: Loaded module fixture.
            frm: Path source module.
            to: Path destination module.

        Returns:
            Task with one path query.
        """
        return script_cli.Task.from_dict(
            {
                "id": "P-01",
                "skill": "fix",
                "prompt": "p",
                "primary_module": frm,
                "risk_tier": "high",
                "queries": [{"cmd": "path", "args": [frm, to]}],
                "ground_truth_keys": [],
            }
        )

    def test_direct_edge_is_feasible(self, script_run_cli: Any, sample_pkg: Path) -> None:
        """When the source is a direct importer of the target, the path is feasible."""
        # pkg.imp_from imports pkg.target directly → direct edge → not infeasible.
        task = self._task(script_run_cli, "pkg.imp_from", "pkg.target")
        fraction, infeasible, total, detail = script_run_cli._measure_infeasible_paths([task], sample_pkg)
        assert total == 1
        assert infeasible == 0
        assert fraction == 0.0
        assert detail[0]["direct_edge"] is True

    def test_missing_edge_is_infeasible(self, script_run_cli: Any, sample_pkg: Path) -> None:
        """When the source does not directly import the target, the path is infeasible."""
        # pkg.unrelated does not import pkg.target → no direct edge → infeasible.
        task = self._task(script_run_cli, "pkg.unrelated", "pkg.target")
        fraction, infeasible, total, detail = script_run_cli._measure_infeasible_paths([task], sample_pkg)
        assert total == 1
        assert infeasible == 1
        assert fraction == 1.0
        assert detail[0]["direct_edge"] is False


# ===========================================================================
# JSON output — emit + summary envelope
# ===========================================================================


class TestEmit:
    """Validate emit prints one compact JSON line with the dataclass fields."""

    def test_emits_single_json_line_with_all_fields(self, script_run_cli: Any, capsys: pytest.CaptureFixture) -> None:
        """Emit outputs one line whose keys mirror the ScenarioResult fields."""
        r = script_run_cli.ScenarioResult(
            scenario="C1",
            name="coverage-gap",
            suite="calls",
            passed=True,
            result={"coverage_gap": 0.5},
            threshold={"coverage_gap_min": 0.1},
            notes="n",
        )
        script_run_cli.emit(r)
        out = capsys.readouterr().out
        assert out.count("\n") == 1  # exactly one line
        obj = json.loads(out)
        assert set(obj) == {"scenario", "name", "suite", "passed", "result", "threshold", "notes"}
        assert obj["scenario"] == "C1"
        assert obj["suite"] == "calls"
        assert obj["passed"] is True
        assert obj["result"]["coverage_gap"] == 0.5


class TestBuildSummaryEnvelope:
    """Validate build_summary_envelope aggregation and fields."""

    def test_aggregates_suites_and_totals(self, script_run_cli: Any, tmp_path: Path) -> None:
        """Envelope reports per-suite pass/total and overall scenario counts."""
        results = [
            script_run_cli.ScenarioResult("C1", "x", "calls", True, {}, {}),
            script_run_cli.ScenarioResult("C2", "x", "calls", False, {}, {}),
            script_run_cli.ScenarioResult("A1", "x", "accuracy", True, {}, {}),
        ]
        env = script_run_cli.build_summary_envelope(results, tmp_path, tmp_path / "i.json", "PARTIAL")
        assert env["verdict"] == "PARTIAL"
        assert env["scenarios_passed"] == 2
        assert env["scenarios_total"] == 3
        assert env["suites"]["calls"] == {"passed": 1, "total": 2}
        assert env["suites"]["accuracy"] == {"passed": 1, "total": 1}
        assert env["repo"] == str(tmp_path)
        assert env["index"] == str(tmp_path / "i.json")
        assert env["date"] == date.today().isoformat()

    def test_envelope_is_json_serializable(self, script_run_cli: Any, tmp_path: Path) -> None:
        """The envelope round-trips through json.dumps/loads unchanged in structure."""
        results = [script_run_cli.ScenarioResult("C1", "x", "calls", True, {}, {})]
        env = script_run_cli.build_summary_envelope(results, tmp_path, tmp_path / "i.json", "PASS")
        assert json.loads(json.dumps(env))["suites"]["calls"]["passed"] == 1


# ===========================================================================
# Report path — single-resolve regression
# ===========================================================================


class TestWriteReportFile:
    """Validate that the printed report path equals the file actually written."""

    def test_returned_path_is_the_written_file(
        self, script_run_cli: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """write_report_file returns the exact path render_report wrote (no -2 drift)."""
        monkeypatch.chdir(tmp_path)
        rendered: dict[str, Path] = {}

        def _fake_render(results: Any, repo: Path, index: Path, path: Path) -> None:
            """Write a minimal report at the requested output path."""
            Path(path).write_text("report", encoding="utf-8")
            rendered["path"] = Path(path)

        monkeypatch.setattr(script_run_cli, "render_report", _fake_render)
        returned = script_run_cli.write_report_file([], tmp_path, tmp_path / "i.json")
        assert Path(returned) == rendered["path"]
        assert Path(returned).exists()
        assert Path(returned).name == f"code-{date.today().isoformat()}.md"

    def test_second_call_uses_counter_suffix(
        self, script_run_cli: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A second run writes a distinct code-<date>-2.md that also exists."""
        monkeypatch.chdir(tmp_path)

        def _fake_render(results: Any, repo: Path, index: Path, path: Path) -> None:
            """Write a minimal report at the requested output path."""
            Path(path).write_text("report", encoding="utf-8")

        monkeypatch.setattr(script_run_cli, "render_report", _fake_render)
        first = script_run_cli.write_report_file([], tmp_path, tmp_path / "i.json")
        second = script_run_cli.write_report_file([], tmp_path, tmp_path / "i.json")
        assert first != second
        assert Path(first).exists()
        assert Path(second).exists()
        assert Path(second).name == f"code-{date.today().isoformat()}-2.md"

    def test_resolve_report_path_has_no_filesystem_side_effect(
        self, script_run_cli: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """resolve_report_path only computes a path — it never creates benchmarks/results/."""
        monkeypatch.chdir(tmp_path)
        path = script_run_cli.resolve_report_path()
        assert path.name == f"code-{date.today().isoformat()}.md"
        assert not (tmp_path / "benchmarks" / "results").exists()
        assert not path.exists()


# ===========================================================================
# Error sentinel — run_scan_query_result distinguishes failure from empty
# ===========================================================================


class TestRunScanQueryResult:
    """Validate run_scan_query_result reports errors distinctly from empty results."""

    def _fake_bin(self, tmp_path: Path) -> Path:
        """Create a placeholder scan-query binary file.

        Args:
            tmp_path: Pytest temporary directory.

        Returns:
            Path to a file named 'scan-query'.
        """
        p = tmp_path / "scan-query"
        p.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        return p

    def test_success_sets_data_and_ok(self, script_run_cli: Any, tmp_path: Path) -> None:
        """A zero-exit JSON response yields ok=True with parsed data and no error."""
        fake = MagicMock()
        fake.returncode = 0
        fake.stdout = json.dumps({"imported_by": []})
        with patch.object(script_run_cli, "_run", return_value=fake):
            res = script_run_cli.run_scan_query_result(
                self._fake_bin(tmp_path), ["rdeps", "x"], tmp_path / "i.json", tmp_path
            )
        assert res.ok is True
        assert res.error is None
        assert res.data == {"imported_by": []}

    def test_nonzero_exit_carries_error(self, script_run_cli: Any, tmp_path: Path) -> None:
        """A non-zero exit yields ok=False with a stderr-derived error reason."""
        fake = MagicMock()
        fake.returncode = 2
        fake.stdout = ""
        fake.stderr = "module not found: foo\n"
        with patch.object(script_run_cli, "_run", return_value=fake):
            res = script_run_cli.run_scan_query_result(
                self._fake_bin(tmp_path), ["rdeps", "foo"], tmp_path / "i.json", tmp_path
            )
        assert res.ok is False
        assert res.data is None
        assert "module not found" in res.error

    def test_timeout_carries_error(self, script_run_cli: Any, tmp_path: Path) -> None:
        """A subprocess timeout yields ok=False with a timeout error reason."""
        with patch.object(script_run_cli, "_run", side_effect=subprocess.TimeoutExpired(cmd=[], timeout=30)):
            res = script_run_cli.run_scan_query_result(
                self._fake_bin(tmp_path), ["central"], tmp_path / "i.json", tmp_path
            )
        assert res.ok is False
        assert "timeout" in res.error

    def test_wrapper_returns_none_on_error(self, script_run_cli: Any, tmp_path: Path) -> None:
        """The run_scan_query wrapper still returns None on failure (back-compat)."""
        fake = MagicMock()
        fake.returncode = 1
        fake.stdout = ""
        fake.stderr = "boom"
        with patch.object(script_run_cli, "_run", return_value=fake):
            data = script_run_cli.run_scan_query(
                self._fake_bin(tmp_path), ["rdeps", "x"], tmp_path / "i.json", tmp_path
            )
        assert data is None

    def test_codemap_rdeps_result_reports_error(self, script_run_cli: Any, tmp_path: Path) -> None:
        """codemap_rdeps_result returns an error reason instead of a silent empty set."""
        fake = MagicMock()
        fake.returncode = 1
        fake.stdout = ""
        fake.stderr = "no such module"
        with patch.object(script_run_cli, "_run", return_value=fake):
            importers, err = script_run_cli.codemap_rdeps_result(
                self._fake_bin(tmp_path), tmp_path / "i.json", tmp_path, "foo"
            )
        assert importers == set()
        assert err is not None and "no such module" in err


# ===========================================================================
# AST-oracle accuracy scoring — precision oracle + grep recall floor
# ===========================================================================


class TestScoreRdepsAccuracy:
    """Validate score_rdeps_accuracy uses AST precision and a grep recall floor."""

    def test_grep_invisible_true_importer_not_penalised(self, script_run_cli: Any, sample_pkg: Path) -> None:
        """An AST-verified relative importer grep cannot see counts as precise, not a FP."""
        grep_floor = script_run_cli.grep_importers_boundary(sample_pkg, "pkg.target")
        codemap = grep_floor | {"pkg.rel.rel_from"}  # relative importer grep misses
        stats = script_run_cli.score_rdeps_accuracy(codemap, grep_floor, "pkg.target", sample_pkg)
        assert stats.precision == pytest.approx(1.0)
        assert stats.fp == 0
        assert stats.recall == pytest.approx(1.0)

    def test_ast_rejected_member_is_false_positive(self, script_run_cli: Any, sample_pkg: Path) -> None:
        """A codemap member the AST oracle rejects is scored as a false positive."""
        grep_floor = script_run_cli.grep_importers_boundary(sample_pkg, "pkg.target")
        codemap = {"pkg.imp_from", "pkg.imp_decoy"}  # decoy imports pkg.target_helper, not pkg.target
        stats = script_run_cli.score_rdeps_accuracy(codemap, grep_floor, "pkg.target", sample_pkg)
        assert stats.precision == pytest.approx(0.5)
        assert stats.fp == 1
        assert stats.fp_modules == ["pkg.imp_decoy"]

    def test_recall_floor_penalises_missed_grep_importer(self, script_run_cli: Any, sample_pkg: Path) -> None:
        """A boundary-grep importer codemap omits lowers the recall floor and is a FN."""
        grep_floor = script_run_cli.grep_importers_boundary(sample_pkg, "pkg.target")
        codemap = grep_floor - {"pkg.imp_plain"}  # codemap missed one grep-visible importer
        stats = script_run_cli.score_rdeps_accuracy(codemap, grep_floor, "pkg.target", sample_pkg)
        assert stats.recall < 1.0
        assert "pkg.imp_plain" in stats.fn_modules


# ===========================================================================
# Accuracy error handling — a scan-query failure never scores precision 1.0
# ===========================================================================


class TestAccuracyErrorHandling:
    """Validate accuracy scoring fails (not passes) when scan-query errors."""

    def _task(self, script_cli: Any) -> Any:
        """Build a single high-risk rdeps task.

        Args:
            script_cli: Loaded module fixture.

        Returns:
            Task with one rdeps query at high risk tier.
        """
        return script_cli.Task.from_dict(
            {
                "id": "B-01",
                "skill": "fix",
                "prompt": "p",
                "primary_module": "pkg.mod",
                "risk_tier": "high",
                "queries": [{"cmd": "rdeps", "args": ["pkg.mod"]}],
                "ground_truth_keys": [],
            }
        )

    def test_errored_task_not_scored_as_pass(self, script_run_cli: Any, tmp_path: Path) -> None:
        """A scan-query error marks the task errored and fails A1 (no precision=1.0 pass)."""
        task = self._task(script_run_cli)
        with patch.object(script_run_cli, "codemap_rdeps_result", return_value=(set(), "exit 1: boom")):
            rows = script_run_cli._score_accuracy_tasks([task], tmp_path / "sq", tmp_path / "i.json", tmp_path)
        assert rows[0]["errored"] is True
        a1 = script_run_cli._a1_scenario(rows)
        assert a1.passed is False
        assert "pkg.mod" in a1.result["errored"]


# ===========================================================================
# A2 vacuous empty result — precision-only scenario must not free-pass on empty
# ===========================================================================


class TestA2VacuousEmpty:
    """Validate a non-errored EMPTY low-risk codemap result is N/A for A2, never a precision=1.0 pass."""

    def test_empty_codemap_is_not_a_free_a2_pass(self, script_run_cli: Any, tmp_path: Path) -> None:
        """An empty non-errored low-risk result is excluded as vacuous and fails A2 alone."""
        task = script_run_cli.Task.from_dict(
            {
                "id": "B-04",
                "skill": "fix",
                "prompt": "p",
                "primary_module": "pkg.mod",
                "risk_tier": "low",
                "queries": [{"cmd": "rdeps", "args": ["pkg.mod"]}],
                "ground_truth_keys": [],
            }
        )
        with (
            patch.object(script_run_cli, "codemap_rdeps_result", return_value=(set(), None)),
            patch.object(script_run_cli, "grep_importers_boundary", return_value=set()),
        ):
            rows = script_run_cli._score_accuracy_tasks([task], tmp_path / "sq", tmp_path / "i.json", tmp_path)
        assert rows[0]["codemap_count"] == 0
        assert rows[0]["errored"] is False
        a2 = script_run_cli._a2_scenario(rows)
        assert a2.passed is False
        assert a2.result["vacuous"] == ["pkg.mod"]

    def test_a2_passes_on_real_module_despite_empty_sibling(self, script_run_cli: Any) -> None:
        """A2 passes on a real precision=1.0 module while an empty sibling is N/A, not a fail."""
        rows = [
            {"module": "pkg.real", "risk_tier": "low", "errored": False, "codemap_count": 2, "precision": 1.0},
            {"module": "pkg.empty", "risk_tier": "low", "errored": False, "codemap_count": 0, "precision": 1.0},
        ]
        a2 = script_run_cli._a2_scenario(rows)
        assert a2.passed is True
        assert a2.result["vacuous"] == ["pkg.empty"]


# ===========================================================================
# Verdict split — primary correctness vs self-consistency track
# ===========================================================================


class TestVerdictSplit:
    """Validate self-consistency suites are excluded from the primary verdict."""

    def test_self_consistency_excluded_from_verdict(self, script_run_cli: Any) -> None:
        """A failing self-consistency scenario does not drag the primary verdict."""
        results = [
            script_run_cli.ScenarioResult("C1", "x", "calls", True, {}, {}),
            script_run_cli.ScenarioResult("S2", "x", "symbol", False, {}, {}),
        ]
        assert script_run_cli.compute_verdict(results) == "PASS"

    def test_verdict_fails_on_primary_failure(self, script_run_cli: Any) -> None:
        """A failing primary scenario below 50% yields FAIL regardless of self-consistency."""
        results = [
            script_run_cli.ScenarioResult("C1", "x", "calls", False, {}, {}),
            script_run_cli.ScenarioResult("A1", "x", "accuracy", False, {}, {}),
            script_run_cli.ScenarioResult("S2", "x", "symbol", True, {}, {}),
        ]
        assert script_run_cli.compute_verdict(results) == "FAIL"

    @pytest.mark.parametrize(
        "passed_flags,expected",
        [
            pytest.param([True, True, True], "CONSISTENT", id="all-pass"),
            pytest.param([True, False], "PARTIAL", id="half"),
            pytest.param([False, False], "INCONSISTENT", id="all-fail"),
        ],
    )
    def test_self_consistency_verdict(self, script_run_cli: Any, passed_flags: list, expected: str) -> None:
        """Self-consistency verdict reflects the pass ratio of symbol/health/xrefs.

        Args:
            passed_flags: Pass/fail flags for the self-consistency scenarios.
            expected: Expected self-consistency verdict string.
        """
        results = [
            script_run_cli.ScenarioResult(f"S_{i}", "x", "symbol", flag, {}, {}) for i, flag in enumerate(passed_flags)
        ]
        assert script_run_cli.compute_self_consistency(results)["verdict"] == expected

    def test_self_consistency_skipped_when_absent(self, script_run_cli: Any) -> None:
        """With no self-consistency scenarios the track verdict is SKIPPED."""
        results = [script_run_cli.ScenarioResult("C1", "x", "calls", True, {}, {})]
        assert script_run_cli.compute_self_consistency(results)["verdict"] == "SKIPPED"

    def test_envelope_reports_primary_and_self_consistency(self, script_run_cli: Any, tmp_path: Path) -> None:
        """The summary envelope carries separate primary and self_consistency breakdowns."""
        results = [
            script_run_cli.ScenarioResult("C1", "x", "calls", True, {}, {}),
            script_run_cli.ScenarioResult("S2", "x", "symbol", False, {}, {}),
        ]
        env = script_run_cli.build_summary_envelope(results, tmp_path, tmp_path / "i.json", "PASS")
        assert env["primary"] == {"passed": 1, "total": 1}
        assert env["self_consistency"] == {"verdict": "INCONSISTENT", "passed": 0, "total": 1}


# ===========================================================================
# Accuracy tier coverage — every task graded, none silently dropped
# ===========================================================================


class TestAccuracyTierCoverage:
    """Validate A1 and A2 partition every risk tier so no accuracy task is silently ungraded."""

    def test_a1_and_a2_partition_all_tiers(self, script_run_cli: Any) -> None:
        """One row per known tier lands in exactly one of A1 or A2 (union all, no overlap)."""
        tiers = ["high", "very-high", "moderate-high", "moderate", "low", "low-moderate"]
        rows = [
            {
                "module": f"pkg.{t}",
                "risk_tier": t,
                "errored": False,
                "codemap_count": 1,
                "precision": 1.0,
                "recall": 1.0,
            }
            for t in tiers
        ]
        a1_mods = {m["module"] for m in script_run_cli._a1_scenario(rows).result["per_module"]}
        a2_mods = {m["module"] for m in script_run_cli._a2_scenario(rows).result["per_module"]}
        assert a1_mods | a2_mods == {r["module"] for r in rows}
        assert a1_mods & a2_mods == set()

    def test_previously_ungraded_tiers_now_graded_by_a2(self, script_run_cli: Any) -> None:
        """Moderate / low-moderate tiers (formerly ungraded) are now scored under A2."""
        rows = [
            {"module": "pkg.mod", "risk_tier": "moderate", "errored": False, "codemap_count": 1, "precision": 1.0},
            {"module": "pkg.lm", "risk_tier": "low-moderate", "errored": False, "codemap_count": 1, "precision": 1.0},
        ]
        graded = {m["module"] for m in script_run_cli._a2_scenario(rows).result["per_module"]}
        assert graded == {"pkg.mod", "pkg.lm"}


# ===========================================================================
# A1 per-module gating — a high group mean cannot mask a failing module
# ===========================================================================


class TestA1PerModuleGating:
    """Validate A1 PASS requires every module, not just a passing group mean."""

    def test_one_module_below_threshold_fails_despite_passing_mean(self, script_run_cli: Any) -> None:
        """Three perfect modules + one at recall 0.5 keep the mean above threshold, yet A1 fails."""
        rows = [
            {"module": "pkg.a", "risk_tier": "high", "errored": False, "precision": 1.0, "recall": 1.0},
            {"module": "pkg.b", "risk_tier": "high", "errored": False, "precision": 1.0, "recall": 1.0},
            {"module": "pkg.c", "risk_tier": "high", "errored": False, "precision": 1.0, "recall": 1.0},
            {"module": "pkg.bad", "risk_tier": "high", "errored": False, "precision": 1.0, "recall": 0.5},
        ]
        a1 = script_run_cli._a1_scenario(rows)
        assert a1.passed is False
        assert a1.result["avg_recall"] >= script_run_cli.THRESHOLDS["A1"]["recall_min"]
        assert "pkg.bad" in a1.result["failing_modules"]

    def test_all_modules_meeting_threshold_pass(self, script_run_cli: Any) -> None:
        """Every high-risk module at/above both thresholds → A1 passes with no failing modules."""
        rows = [
            {"module": "pkg.a", "risk_tier": "high", "errored": False, "precision": 1.0, "recall": 1.0},
            {"module": "pkg.b", "risk_tier": "high", "errored": False, "precision": 0.95, "recall": 0.9},
        ]
        a1 = script_run_cli._a1_scenario(rows)
        assert a1.passed is True
        assert a1.result["failing_modules"] == []


# ===========================================================================
# Hardware capture — latency gates are hardware-bound, host must be recorded
# ===========================================================================


class TestHardwareCapture:
    """Validate the report header and JSON envelope record the host for the hardware-calibrated gates."""

    def test_envelope_includes_platform_and_cpu_fields(self, script_run_cli: Any, tmp_path: Path) -> None:
        """The summary envelope carries a hardware dict with platform, cpu_count, and python."""
        results = [script_run_cli.ScenarioResult("C1", "x", "calls", True, {}, {})]
        env = script_run_cli.build_summary_envelope(results, tmp_path, tmp_path / "i.json", "PASS")
        assert set(env["hardware"]) >= {"platform", "processor", "cpu_count", "python"}

    def test_report_header_records_hardware(self, script_run_cli: Any, tmp_path: Path) -> None:
        """The rendered report header names the hardware host for the latency thresholds."""
        results = [script_run_cli.ScenarioResult("L1", "central", "latency", True, {}, {})]
        report = tmp_path / "r.md"
        script_run_cli.render_report(results, tmp_path, tmp_path / "i.json", report)
        assert "**Hardware**" in report.read_text()


# ===========================================================================
# Report reconciliation — S/H/X visible and header counts reconcile
# ===========================================================================


class TestReportReconciliation:
    """Validate every scenario counted in the header is visible in a rendered table (primary + S/H/X)."""

    def test_shx_rendered_and_header_counts_reconcile(self, script_run_cli: Any, tmp_path: Path) -> None:
        """S/H/X render in the self-consistency table and header counts match each track's size."""
        sr = script_run_cli.ScenarioResult
        results = [
            sr("C1", "cov", "calls", True, {}, {}),
            sr("C2", "cov2", "calls", False, {}, {}),
            sr("A1", "acc", "accuracy", True, {}, {}),
            sr("L1", "lat", "latency", True, {}, {}),
            sr("Q_fix", "qs", "query-shape", True, {}, {}),
            sr("S2", "sym", "symbol", True, {}, {}),
            sr("H1", "hea", "health", False, {}, {}),
            sr("X1", "xrf", "xrefs", True, {}, {}),
        ]
        report = tmp_path / "r.md"
        script_run_cli.render_report(results, tmp_path, tmp_path / "i.json", report)
        text = report.read_text()
        primary_total = len([r for r in results if r.suite in script_run_cli._PRIMARY_SUITES])
        sc = [r for r in results if r.suite in script_run_cli._SELF_CONSISTENCY_SUITES]
        sc_passed = len([r for r in sc if r.passed])
        assert "Symbol (S)" in text and "Health (H)" in text and "Xrefs (X)" in text
        assert f"/{primary_total} primary scenarios" in text
        assert f"{sc_passed}/{len(sc)}" in text
        assert primary_total + len(sc) == len(results)

    def test_skipped_self_consistency_noted_not_silent(self, script_run_cli: Any, tmp_path: Path) -> None:
        """With no S/H/X results the report explicitly flags the skipped track, not silence."""
        results = [script_run_cli.ScenarioResult("C1", "cov", "calls", True, {}, {})]
        report = tmp_path / "r.md"
        script_run_cli.render_report(results, tmp_path, tmp_path / "i.json", report)
        text = report.read_text()
        assert "skipped (no ground truth)" in text
        assert "SKIPPED" in text


# ===========================================================================
# Stale-index guard — self-consistency suites skip on an old scan_version
# ===========================================================================


class TestIndexScanVersion:
    """Validate the index scan_version reader that gates the self-consistency track."""

    def test_reads_recorded_scan_version(self, script_run_cli: Any, tmp_path: Path) -> None:
        """The recorded integer scan_version is returned from the index JSON."""
        index = tmp_path / "i.json"
        index.write_text(json.dumps({"scan_version": 7, "modules": []}), encoding="utf-8")
        assert script_run_cli._index_scan_version(index) == 7

    def test_missing_field_returns_zero(self, script_run_cli: Any, tmp_path: Path) -> None:
        """An index without scan_version yields 0, gating the self-consistency suites off."""
        index = tmp_path / "i.json"
        index.write_text(json.dumps({"modules": []}), encoding="utf-8")
        assert script_run_cli._index_scan_version(index) == 0

    def test_unreadable_index_returns_zero(self, script_run_cli: Any, tmp_path: Path) -> None:
        """An absent or unparsable index returns 0 rather than raising."""
        assert script_run_cli._index_scan_version(tmp_path / "nope.json") == 0

    def test_min_ver_constant_is_positive(self, script_run_cli: Any) -> None:
        """The self-consistency minimum version is a positive gate value."""
        assert script_run_cli._SELF_CONSISTENCY_MIN_VER >= 1


class TestTimingCensoring:
    """Failed and timed-out runs must not enter the latency statistics."""

    def test_failed_runs_are_discarded_and_counted(self, script_run_cli: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """A command that fails instantly otherwise records an excellent latency."""
        monkeypatch.setattr(
            script_run_cli, "_run", lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout="", stderr="boom")
        )

        stats = script_run_cli.time_command(["false"], n=3)

        assert stats.failed == 3
        assert stats.measured == 0
        assert math.isnan(stats.median_ms)

    def test_timed_out_runs_are_reported_as_censored_not_observed(
        self, script_run_cli: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The deadline is a lower bound on the true duration, not a measurement.

        Appending 30_000 ms as if it were an observation dragged the median toward the timeout value.
        """

        def _timeout(*_args: Any, **_kwargs: Any) -> None:
            """Raise a subprocess timeout without launching the requested command."""
            raise subprocess.TimeoutExpired(["sleep"], 30)

        monkeypatch.setattr(script_run_cli, "_run", _timeout)

        stats = script_run_cli.time_command(["sleep", "60"], n=3)

        assert stats.timed_out == 3
        assert stats.measured == 0
        assert math.isnan(stats.median_ms)
        assert stats.median_ms != 30_000.0

    def test_successful_runs_still_produce_statistics(
        self, script_run_cli: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ordinary path is unchanged."""
        monkeypatch.setattr(
            script_run_cli, "_run", lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr="")
        )

        stats = script_run_cli.time_command(["true"], n=4)

        assert stats.failed == 0
        assert stats.timed_out == 0
        assert stats.measured == 4
        assert stats.median_ms >= 0

    def test_a_mixed_run_keeps_only_the_successful_observations(
        self, script_run_cli: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Partial failure must reduce the observation count, not the reported speed."""
        codes = iter([0, 1, 0])

        monkeypatch.setattr(
            script_run_cli,
            "_run",
            lambda *_args, **_kwargs: SimpleNamespace(returncode=next(codes), stdout="", stderr=""),
        )

        stats = script_run_cli.time_command(["maybe"], n=3)

        assert stats.failed == 1
        assert stats.measured == 2

    def test_command_sequence_discards_a_failed_sequence(
        self, script_run_cli: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same rule applies to the cold-grep sequence timer."""
        monkeypatch.setattr(
            script_run_cli, "_run", lambda *_args, **_kwargs: SimpleNamespace(returncode=2, stdout="", stderr="")
        )

        stats = script_run_cli.time_commands([["a"], ["b"]], n=2)

        assert stats.failed == 2
        assert stats.measured == 0


# ===========================================================================
# Optional-rich fallback
# ===========================================================================


def _load_cli_without_rich(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Import the CLI runner afresh with ``rich`` made unimportable.

    Blocking the package in ``sys.modules`` is what makes the runner's guarded import fail the way a
    host without rich installed makes it fail, and ``monkeypatch`` restores the entry afterwards.
    The module is loaded under a throwaway name so the session-scoped ``script_run_cli`` fixture,
    which every other test shares, keeps its own rich-enabled instance.

    Args:
        monkeypatch: Fixture used to block the import and restore it at teardown.

    Returns:
        The freshly executed module object.
    """
    import importlib.util

    monkeypatch.syspath_prepend(str(REPO_ROOT / "benchmarks"))
    monkeypatch.setitem(sys.modules, "rich", None)
    monkeypatch.setitem(sys.modules, "rich.console", None)
    name = "run_cli_without_rich"
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "benchmarks" / "run-codemap-cli.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


class TestRichAbsentFallback:
    """The CLI runner stays usable on a host where rich is not installed."""

    def test_import_degrades_to_the_console_free_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A missing rich leaves no console behind instead of aborting the import.

        Scenario: the runner's console now comes from the shared ``benchmark_console`` helper, which
        imports rich lazily inside the function rather than at the top of the module. That import
        still has to raise through the runner's own guard, or a host without rich would lose the
        whole benchmark instead of just its progress bar.
        """
        module = _load_cli_without_rich(monkeypatch)

        assert module._IS_RICH_AVAILABLE is False
        assert module._console is None

    def test_progress_messages_fall_back_to_stderr(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Without a console, narration goes to stderr and leaves stdout clean for machine output.

        Scenario: stdout carries scenario JSONL and the summary envelope, so the fallback branch
        must never write narration there — a downstream JSON consumer would choke on it.
        """
        module = _load_cli_without_rich(monkeypatch)

        module.log("[verify] Checking task modules against index...")

        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == "[verify] Checking task modules against index...\n"

    def test_suites_run_without_a_progress_bar(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Every suite still runs and reports when the progress display is unavailable.

        Scenario: ``_run_all_suites`` asks for a bar whenever the caller wants one, so the guard on
        console availability is the only thing keeping a rich-free host from failing mid-benchmark.
        """
        module = _load_cli_without_rich(monkeypatch)
        run_labels: list[str] = []

        def _suite(label: str) -> Any:
            """Build a zero-argument suite that records its own execution and yields no results."""

            def _run() -> list:
                run_labels.append(label)
                return []

            return _run

        results = module._run_all_suites([("first", _suite("first")), ("second", _suite("second"))], True)

        assert run_labels == ["first", "second"]
        assert results == []

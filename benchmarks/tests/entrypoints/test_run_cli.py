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

import importlib.util
import json
import math
import os
import subprocess
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

# Patch seams live in the package modules the runner shim re-exports from: patching the shim
# would leave each package module's own global binding untouched.
from _bench_query import tasks as query_tasks
from _launcher_capability import _raw_codemap_launchers_are_runnable

REPO_ROOT = Path(__file__).parent.parent.parent.parent
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

# Subprocess boundary of the scan-query wrapper: patched where run_scan_query_result looks it up.
RUN_COMMAND = "_bench_query.cold._run"


# ===========================================================================
# Fixtures and helpers
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


@pytest.fixture(name="make_task")
def _make_task(script_run_cli: Any) -> Callable[..., Any]:
    """Return a factory building a high-risk rdeps ``Task``, with any field overridden by keyword.

    Returns:
        Callable taking raw task-dict overrides and returning the parsed ``Task``.

    Example:
        >>> task = getfixture("make_task")(risk_tier="low")
        >>> task.id, task.risk_tier, task.queries[0].cmd
        ('T-01', 'low', 'rdeps')
    """

    def build(**overrides: Any) -> Any:
        """Parse a default task dict merged with ``overrides``."""
        raw = {
            "id": "T-01",
            "skill": "fix",
            "prompt": "p",
            "primary_module": "pkg.mod",
            "risk_tier": "high",
            "queries": [{"cmd": "rdeps", "args": ["pkg.mod"]}],
            "ground_truth_keys": [],
        }
        return script_run_cli.Task.from_dict({**raw, **overrides})

    return build


@pytest.fixture(name="make_result")
def _make_result(script_run_cli: Any) -> Callable[..., Any]:
    """Return a factory building a ``ScenarioResult`` with empty result and threshold payloads.

    Returns:
        Callable taking ``(scenario, suite, passed=True)`` and returning the ``ScenarioResult``.

    Example:
        >>> r = getfixture("make_result")("S2", "symbol", passed=False)
        >>> r.scenario, r.suite, r.passed
        ('S2', 'symbol', False)
    """

    def build(scenario: str, suite: str, passed: bool = True) -> Any:
        """Build one scenario result with the given identity and verdict."""
        return script_run_cli.ScenarioResult(scenario, "x", suite, passed, {}, {})

    return build


@pytest.fixture(name="fake_scan_query")
def _fake_scan_query(tmp_path: Path) -> Path:
    """Create a placeholder scan-query script so ``Path.resolve()`` has a real file to point at.

    Returns:
        Path to a file named ``scan-query`` inside ``tmp_path``.

    Example:
        >>> getfixture("fake_scan_query").name
        'scan-query'
    """
    path = tmp_path / "scan-query"
    path.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    return path


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    """Build the slice of a ``CompletedProcess`` that the scan-query wrapper reads.

    Example:
        >>> _proc(2, stderr="boom").returncode
        2
    """
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _accuracy_row(module: str, risk_tier: str = "high", **fields: Any) -> dict:
    """Build one per-module accuracy row, defaulting to a non-errored module.

    Example:
        >>> _accuracy_row("pkg.a", precision=1.0)["errored"]
        False
    """
    return {"module": module, "risk_tier": risk_tier, "errored": False, **fields}


# ===========================================================================
# Dataclass construction
# ===========================================================================


class TestDataclasses:
    """Verify dataclass fields and defaults behave as documented."""

    def test_query_stores_cmd_and_args(self, script_run_cli: Any) -> None:
        """Query keeps the command and argument list it was built with.

        Scenario: a rdeps query for one module; the runner later replays cmd and args verbatim against scan-query.
        """
        q = script_run_cli.Query(cmd="rdeps", args=["foo.bar"])
        assert q.cmd == "rdeps"
        assert q.args == ["foo.bar"]

    def test_task_from_dict_roundtrip(self, script_run_cli: Any, minimal_task_dict: dict) -> None:
        """Task.from_dict parses every field of a raw task record.

        Scenario: a record with the full tasks-code.json schema; each field and the nested query must survive parsing.
        """
        t = script_run_cli.Task.from_dict(minimal_task_dict)
        assert t.id == "T-99"
        assert t.skill == "fix"
        assert t.primary_module == "pkg.mod"
        assert t.risk_tier == "high"
        assert len(t.queries) == 1
        assert t.queries[0].cmd == "rdeps"

    def test_task_from_dict_empty_queries(self, script_run_cli: Any, minimal_task_dict: dict) -> None:
        """Task.from_dict turns an empty queries list into an empty list.

        Scenario: a task that declares no queries must parse instead of failing on the missing first element.
        """
        minimal_task_dict["queries"] = []
        t = script_run_cli.Task.from_dict(minimal_task_dict)
        assert t.queries == []

    def test_scenario_result_notes_default_empty(self, script_run_cli: Any) -> None:
        """ScenarioResult.notes defaults to an empty string.

        Scenario: result built without notes; the report renderer prints notes unconditionally, so None would leak.
        """
        r = script_run_cli.ScenarioResult(scenario="C1", name="x", suite="calls", passed=True, result={}, threshold={})
        assert r.notes == ""

    def test_timing_stats_stores_all_fields(self, script_run_cli: Any) -> None:
        """TimingStats keeps the min, median, max and sample count it was built with.

        Scenario: a five-sample timing summary round-tripped through the dataclass constructor.
        """
        ts = script_run_cli.TimingStats(min_ms=1.0, median_ms=2.5, max_ms=10.0, n=5)
        assert ts.min_ms == 1.0
        assert ts.median_ms == 2.5
        assert ts.max_ms == 10.0
        assert ts.n == 5

    def test_accuracy_stats_stores_all_fields(self, script_run_cli: Any) -> None:
        """AccuracyStats keeps precision, recall, the TP/FP/FN counts and offending module lists.

        Scenario: a 9/1/2 TP/FP/FN summary with one false-positive and one false-negative module recorded.
        """
        a = script_run_cli.AccuracyStats(
            precision=0.9, recall=0.8, tp=9, fp=1, fn=2, fp_modules=["a.b"], fn_modules=["c.d"]
        )
        assert a.precision == pytest.approx(0.9)
        assert a.recall == pytest.approx(0.8)
        assert a.tp == 9
        assert a.fp_modules == ["a.b"]

    def test_suite_stats_defaults_zero(self, script_run_cli: Any) -> None:
        """SuiteStats starts every counter at zero.

        Scenario: a fresh accumulator before any scenario is recorded; non-zero defaults would inflate suite totals.
        """
        s = script_run_cli.SuiteStats()
        assert s.total == 0
        assert s.passed == 0
        assert s.failed == 0

    @pytest.mark.parametrize(
        ("ok", "reason"),
        [pytest.param(True, "", id="ok-has-empty-reason"), pytest.param(False, "missing key", id="failure-has-reason")],
    )
    def test_validation_result_keeps_verdict_and_reason(self, script_run_cli: Any, ok: bool, reason: str) -> None:
        """ValidationResult keeps the verdict and reason it was built with.

        Scenario: a passing result carries an empty reason; a failing one carries the human-readable cause.
        """
        vr = script_run_cli.ValidationResult(ok=ok, reason=reason)
        assert vr.ok is ok
        assert vr.reason == reason


# ===========================================================================
# Task loading — load_tasks
# ===========================================================================


class TestLoadTasks:
    """Validate load_tasks contract against the real tasks-code.json file."""

    def test_returns_task_objects(self, script_run_cli: Any, real_tasks: list) -> None:
        """Unfiltered load returns a non-empty list of Task instances.

        Scenario: the shipped tasks-code.json is parsed once per session; every record must become a Task.
        """
        assert len(real_tasks) > 0
        assert all(isinstance(t, script_run_cli.Task) for t in real_tasks)

    def test_total_count_matches_file(self, script_run_cli: Any, real_tasks: list) -> None:
        """Task count equals the number of objects in tasks-code.json.

        Scenario: guards against records silently dropped by the loader when the task file grows.
        """
        with script_run_cli.TASKS_FILE.open() as f:
            raw = json.load(f)
        assert len(real_tasks) == len(raw)

    @pytest.mark.parametrize("skill", ["fix", "feature", "refactor"])
    def test_skill_filter_returns_matching_only(self, script_run_cli: Any, skill: str) -> None:
        """skill_filter keeps only tasks whose skill field matches.

        Scenario: each of the three documented skills must select at least one task and nothing else.
        """
        filtered = script_run_cli.load_tasks(skill_filter=skill)
        assert len(filtered) > 0, f"Expected at least one '{skill}' task"
        assert all(t.skill == skill for t in filtered)

    def test_unknown_skill_filter_returns_empty(self, script_run_cli: Any) -> None:
        """skill_filter with an unknown value produces an empty list.

        Scenario: a mistyped skill name selects nothing rather than raising or falling back to all tasks.
        """
        result = script_run_cli.load_tasks(skill_filter="nonexistent_skill")
        assert result == []

    def test_no_filter_includes_all_skills(self, real_tasks: list) -> None:
        """Unfiltered load contains tasks from all three skill groups.

        Scenario: a benchmark run without --skill must cover fix, feature and refactor tasks alike.
        """
        skills = {t.skill for t in real_tasks}
        assert skills >= {"fix", "feature", "refactor"}

    def test_queries_are_query_objects(self, script_run_cli: Any, real_tasks: list) -> None:
        """Every query within every task is a Query dataclass instance.

        Scenario: nested raw dicts left unparsed would crash the suites later with attribute errors.
        """
        offenders = [t.id for t in real_tasks if not all(isinstance(q, script_run_cli.Query) for q in t.queries)]
        assert offenders == []

    def test_primary_module_is_dotted_string(self, real_tasks: list) -> None:
        """primary_module of every task is a dotted module name.

        Scenario: a bare file path or single-segment name would break module lookups against the index.
        """
        offenders = [t.id for t in real_tasks if "." not in t.primary_module]
        assert offenders == []

    def test_missing_file_raises(self, script_run_cli: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """TASKS_FILE pointing at a missing file raises FileNotFoundError.

        Scenario: the task file is absent; the failure must surface instead of yielding an empty benchmark.
        """
        monkeypatch.setattr(query_tasks, "TASKS_FILE", tmp_path / "missing.json")
        with pytest.raises(FileNotFoundError):
            script_run_cli.load_tasks()

    def test_malformed_json_raises(self, script_run_cli: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """TASKS_FILE containing invalid JSON raises json.JSONDecodeError.

        Scenario: a truncated or hand-edited task file must fail loudly rather than parse to partial data.
        """
        bad = tmp_path / "bad.json"
        bad.write_text("{not valid json", encoding="utf-8")
        monkeypatch.setattr(query_tasks, "TASKS_FILE", bad)
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

    def test_absent_file_returns_empty(
        self, script_run_cli: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """load_oss_tasks returns [] when OSS_TASKS_FILE does not exist.

        Scenario: a checkout without the optional OSS task file must skip that suite instead of crashing.
        """
        monkeypatch.setattr(query_tasks, "OSS_TASKS_FILE", tmp_path / "missing.json")
        assert script_run_cli.load_oss_tasks() == []

    @pytest.mark.parametrize("type_filter", ["symbol_extraction", "code_quality", "real_issue"])
    def test_type_filter_matches_only(
        self, script_run_cli: Any, type_filter: str, oss_tasks_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """type_filter keeps only tasks whose 'type' field matches.

        Scenario: each documented task type selects its own records from a file holding four types.
        """
        monkeypatch.setattr(query_tasks, "OSS_TASKS_FILE", oss_tasks_file)
        result = script_run_cli.load_oss_tasks(type_filter=type_filter)
        assert len(result) >= 1
        assert all(t.get("type") == type_filter for t in result)

    def test_unknown_type_returns_empty(
        self, script_run_cli: Any, oss_tasks_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """type_filter with an unknown value produces an empty list.

        Scenario: a mistyped task type selects nothing rather than raising or returning every task.
        """
        monkeypatch.setattr(query_tasks, "OSS_TASKS_FILE", oss_tasks_file)
        assert script_run_cli.load_oss_tasks(type_filter="does_not_exist") == []

    def test_no_filter_includes_all_types(
        self, script_run_cli: Any, oss_tasks_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unfiltered load returns tasks across types.

        Scenario: without a type filter the symbol-extraction and code-quality records must both be present.
        """
        monkeypatch.setattr(query_tasks, "OSS_TASKS_FILE", oss_tasks_file)
        types = {t.get("type") for t in script_run_cli.load_oss_tasks()}
        assert {"symbol_extraction", "code_quality"} <= types

    def test_result_dicts_have_id_field(
        self, script_run_cli: Any, oss_tasks_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every returned dict has a non-empty 'id' field.

        Scenario: downstream reporting keys scenarios by task id, so an id-less record would be unreportable.
        """
        monkeypatch.setattr(query_tasks, "OSS_TASKS_FILE", oss_tasks_file)
        tasks = script_run_cli.load_oss_tasks()
        assert [t for t in tasks if not t.get("id")] == []

    def test_flat_list_json_returned_as_is(
        self, script_run_cli: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Flat-list JSON is returned as a list without wrapping.

        Scenario: the documented file layout is a bare list; the loader must hand back exactly those records.
        """
        flat = [{"id": "X-01", "type": "code_quality"}]
        f = tmp_path / "flat.json"
        f.write_text(json.dumps(flat), encoding="utf-8")
        monkeypatch.setattr(query_tasks, "OSS_TASKS_FILE", f)
        assert script_run_cli.load_oss_tasks() == flat


# ===========================================================================
# Path helpers
# ===========================================================================


class TestPathToModule:
    """Validate path_to_module conversion logic."""

    @pytest.mark.parametrize(
        ("path", "repo_root", "expected"),
        [
            # Standard layout
            pytest.param("/repo/pkg/mod.py", "/repo", "pkg.mod", id="plain-layout"),
            # src/ layout — strip prefix
            pytest.param("/repo/src/pkg/mod.py", "/repo", "pkg.mod", id="src-layout-strips-prefix"),
            # __init__.py collapses to package
            pytest.param("/repo/pkg/__init__.py", "/repo", "pkg", id="init-collapses-to-package"),
            # Nested __init__.py
            pytest.param("/repo/pkg/sub/__init__.py", "/repo", "pkg.sub", id="nested-init-collapses"),
            # src/ layout with __init__.py
            pytest.param("/repo/src/pkg/__init__.py", "/repo", "pkg", id="src-layout-init"),
        ],
    )
    def test_converts_py_path_to_dotted_module(
        self, script_run_cli: Any, path: str, repo_root: str, expected: str
    ) -> None:
        """A .py path is converted to its dotted module name relative to the repo root.

        Scenario: plain, src-layout and __init__ paths must all land on the importable module name.
        """
        assert script_run_cli.path_to_module(path, repo_root) == expected

    @pytest.mark.parametrize("path", ["/repo/README.md", "/repo/data.csv", "/repo/LICENSE"])
    def test_returns_none_for_non_py_files(self, script_run_cli: Any, path: str) -> None:
        """Non-.py paths return None.

        Scenario: docs, data and extension-less files in a diff are not modules and must be skipped.
        """
        assert script_run_cli.path_to_module(path, "/repo") is None


class TestModuleToGrepPattern:
    """Validate module_to_grep_pattern output contract."""

    @pytest.mark.parametrize(
        ("module", "expected"),
        [
            pytest.param("foo.bar", r"from foo.bar import\|import foo.bar", id="dotted-module"),
            pytest.param("pkg", r"from pkg import\|import pkg", id="top-level-module"),
            pytest.param(
                "lightning.pytorch.trainer.trainer",
                r"from lightning.pytorch.trainer.trainer import\|import lightning.pytorch.trainer.trainer",
                id="deeply-nested-module",
            ),
        ],
    )
    def test_pattern_contains_both_import_forms(self, script_run_cli: Any, module: str, expected: str) -> None:
        r"""Pattern joins the 'from X import' and 'import X' forms with \|.

        Scenario: the grep baseline must find both import styles with one alternation pattern.
        """
        assert script_run_cli.module_to_grep_pattern(module) == expected


class TestModuleToPackage:
    """Validate module_to_package parent extraction."""

    @pytest.mark.parametrize(
        ("module", "expected"),
        [
            pytest.param("foo.bar.baz", "foo.bar", id="three-levels"),
            pytest.param("foo.bar", "foo", id="two-levels"),
            pytest.param("foo", None, id="top-level-has-no-parent"),
        ],
    )
    def test_returns_parent_package_or_none(self, script_run_cli: Any, module: str, expected: str | None) -> None:
        """The parent package is extracted for nested modules and None for top-level ones.

        Scenario: package-level fallbacks stop at the top-level name instead of producing an empty string.
        """
        assert script_run_cli.module_to_package(module) == expected


# ===========================================================================
# Accuracy scoring — compute_precision_recall
# ===========================================================================


class TestComputePrecisionRecall:
    """Validate compute_precision_recall contract from the docs."""

    @pytest.mark.parametrize(
        (
            "codemap_set",
            "grep_set",
            "expected_precision",
            "expected_recall",
            "expected_tp",
            "expected_fp",
            "expected_fn",
        ),
        [
            # Perfect agreement
            pytest.param({"a", "b", "c"}, {"a", "b", "c"}, 1.0, 1.0, 3, 0, 0, id="perfect-agreement"),
            # Codemap finds extra (FP)
            pytest.param({"a", "b", "x"}, {"a", "b"}, 2 / 3, 1.0, 2, 1, 0, id="codemap-extra-is-fp"),
            # Codemap misses some (FN)
            pytest.param({"a"}, {"a", "b"}, 1.0, 0.5, 1, 0, 1, id="codemap-miss-is-fn"),
            # No overlap at all
            pytest.param({"x"}, {"y"}, 0.0, 0.0, 0, 1, 1, id="no-overlap"),
            # Single matching element
            pytest.param({"a"}, {"a"}, 1.0, 1.0, 1, 0, 0, id="single-match"),
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
        """Precision, recall and TP/FP/FN follow the documented set formulas.

        Scenario: codemap rdeps are scored against the grep baseline across agreement, extras, misses and disjoint sets.
        """
        stats = script_run_cli.compute_precision_recall(codemap_set, grep_set)
        assert stats.precision == pytest.approx(expected_precision, abs=1e-4)
        assert stats.recall == pytest.approx(expected_recall, abs=1e-4)
        assert stats.tp == expected_tp
        assert stats.fp == expected_fp
        assert stats.fn == expected_fn

    @pytest.mark.parametrize(
        ("codemap_set", "grep_set", "metric"),
        [
            pytest.param(set(), {"a", "b"}, "precision", id="empty-codemap-precision"),
            pytest.param({"a", "b"}, set(), "recall", id="empty-grep-recall"),
            pytest.param(set(), set(), "precision", id="both-empty-precision"),
            pytest.param(set(), set(), "recall", id="both-empty-recall"),
        ],
    )
    def test_empty_denominator_defaults_to_one(
        self, script_run_cli: Any, codemap_set: set, grep_set: set, metric: str
    ) -> None:
        """A metric with an empty denominator defaults to 1.0 as documented.

        Scenario: no codemap results leaves precision undefined, no grep results leaves recall undefined.
        """
        stats = script_run_cli.compute_precision_recall(codemap_set, grep_set)
        assert getattr(stats, metric) == pytest.approx(1.0)

    @pytest.mark.parametrize(
        ("codemap_set", "grep_set", "field"),
        [
            pytest.param({"z", "a", "m"}, set(), "fp_modules", id="false-positives"),
            pytest.param(set(), {"z", "a", "m"}, "fn_modules", id="false-negatives"),
        ],
    )
    def test_offending_modules_sorted(self, script_run_cli: Any, codemap_set: set, grep_set: set, field: str) -> None:
        """fp_modules and fn_modules are listed in alphabetical order.

        Scenario: sets carry no order, so sorted output keeps reports and diffs deterministic.
        """
        stats = script_run_cli.compute_precision_recall(codemap_set, grep_set)
        assert getattr(stats, field) == ["a", "m", "z"]

    def test_precision_rounded_to_four_places(self, script_run_cli: Any) -> None:
        """Precision is rounded to 4 decimal places as documented.

        Scenario: 2 of 3 codemap hits are correct, so the raw ratio 0.666... must be reported as 0.6667.
        """
        stats = script_run_cli.compute_precision_recall({"a", "b", "c"}, {"a", "b"})
        assert stats.precision == 0.6667


# ===========================================================================
# Verdict computation — compute_verdict
# ===========================================================================


class TestComputeVerdict:
    """Validate compute_verdict against documented thresholds."""

    @pytest.mark.parametrize(
        ("n_pass", "n_total", "expected"),
        [
            pytest.param(0, 0, "FAIL", id="no-scenarios"),  # documented behaviour: no scenarios → FAIL
            pytest.param(3, 3, "PASS", id="all-pass"),  # every scenario passed
            pytest.param(3, 4, "PARTIAL", id="three-of-four"),  # 75% >= 50% but not 100%
            pytest.param(1, 2, "PARTIAL", id="exactly-half"),  # exactly 50%
            pytest.param(1, 3, "FAIL", id="one-of-three"),  # 33% < 50%
            pytest.param(0, 3, "FAIL", id="none-of-three"),  # 0%
            pytest.param(0, 4, "FAIL", id="all-fail"),  # every scenario failed
        ],
    )
    def test_verdict_by_pass_ratio(
        self, make_result: Any, script_run_cli: Any, n_pass: int, n_total: int, expected: str
    ) -> None:
        """Verdict is PASS when all pass, FAIL for none or no scenarios, and splits PARTIAL/FAIL at 50%.

        Scenario: n_total primary scenarios of which the first n_pass passed; thresholds sit on the 50% boundary.
        """
        results = [make_result("C1", "calls", i < n_pass) for i in range(n_total)]
        assert script_run_cli.compute_verdict(results) == expected


# ===========================================================================
# JSON validators
# ===========================================================================


class TestValidateCentralJson:
    """Validate validate_central_json contract."""

    @pytest.mark.parametrize(
        "data",
        [
            pytest.param({"central": [{"name": "foo.bar", "rdep_count": 5}]}, id="single-item"),
            pytest.param({"central": [{"rdep_count": i} for i in range(5)]}, id="multiple-items"),
        ],
    )
    def test_valid_response_returns_ok(self, script_run_cli: Any, data: dict) -> None:
        """A central response whose items all carry rdep_count is accepted.

        Scenario: one named item and five bare items must both validate with an empty reason.
        """
        result = script_run_cli.validate_central_json(data)
        assert result.ok is True
        assert result.reason == ""

    @pytest.mark.parametrize(
        ("data", "reason_fragment"),
        [
            pytest.param({"other_key": []}, "central", id="missing-central-key"),
            pytest.param({"central": [{"name": "foo", "other": 1}]}, "rdep_count", id="item-missing-rdep_count"),
        ],
    )
    def test_missing_required_field_returns_not_ok_with_reason(
        self, script_run_cli: Any, data: dict, reason_fragment: str
    ) -> None:
        """A response missing the 'central' key, or an item missing rdep_count, is rejected naming the field.

        Scenario: a truncated scan-query payload must fail with a reason pointing at the absent key.
        """
        result = script_run_cli.validate_central_json(data)
        assert result.ok is False
        assert reason_fragment in result.reason

    @pytest.mark.parametrize(
        "data",
        [
            pytest.param({"central": []}, id="empty-central-list"),
            pytest.param({"central": "not a list"}, id="central-not-a-list"),
        ],
    )
    def test_empty_or_non_list_central_returns_not_ok(self, script_run_cli: Any, data: dict) -> None:
        """'central' present but empty, or holding a non-list, is rejected.

        Scenario: an empty ranking would let downstream suites pass vacuously, so it must not validate.
        """
        assert script_run_cli.validate_central_json(data).ok is False

    @pytest.mark.parametrize(
        ("data", "reason_fragment"),
        [
            pytest.param([], "object", id="payload-not-an-object"),
            pytest.param({"central": [42]}, "object", id="item-not-an-object"),
            pytest.param({"central": [{"rdep_count": "5"}]}, "int", id="rdep_count-not-an-int"),
        ],
    )
    def test_wrong_type_payloads_return_not_ok(self, script_run_cli: Any, data: Any, reason_fragment: str) -> None:
        """Wrong JSON shapes fail validation with a reason naming the expected type.

        Scenario: payloads that are structurally JSON but not the documented shape must not pass structurally.
        """
        result = script_run_cli.validate_central_json(data)
        assert result.ok is False
        assert reason_fragment in result.reason


class TestValidateRdepsAndDepsJson:
    """Validate the validate_rdeps_json and validate_deps_json contracts, which share one payload shape."""

    @pytest.mark.parametrize(
        ("validator", "data"),
        [
            pytest.param(
                "validate_rdeps_json", {"imported_by": ["a.b", "c.d"], "module": "foo.bar"}, id="rdeps-response"
            ),
            pytest.param("validate_deps_json", {"direct_imports": ["x.y"], "module": "foo.bar"}, id="deps-response"),
        ],
    )
    def test_valid_response_returns_ok(self, script_run_cli: Any, validator: str, data: dict) -> None:
        """Rdeps with imported_by + module, or deps with direct_imports + module, validates.

        Scenario: the two payload shapes differ only in the list key name.
        """
        assert getattr(script_run_cli, validator)(data).ok is True

    @pytest.mark.parametrize(
        ("validator", "data", "expected_reason_fragment"),
        [
            pytest.param("validate_rdeps_json", {"module": "foo"}, "imported_by", id="rdeps-missing-imported_by"),
            pytest.param("validate_rdeps_json", {"imported_by": []}, "module", id="rdeps-missing-module"),
            pytest.param("validate_rdeps_json", {}, "imported_by", id="rdeps-both-missing-first-check-wins"),
            pytest.param("validate_deps_json", {"module": "foo"}, "direct_imports", id="deps-missing-direct_imports"),
            pytest.param("validate_deps_json", {"direct_imports": []}, "module", id="deps-missing-module"),
            pytest.param("validate_deps_json", {}, "direct_imports", id="deps-both-missing-first-check-wins"),
        ],
    )
    def test_missing_keys_return_not_ok(
        self, script_run_cli: Any, validator: str, data: dict, expected_reason_fragment: str
    ) -> None:
        """Missing required keys produce ok=False with the key name in the reason.

        Scenario: when both keys are absent the list key is reported first, so reasons stay deterministic.
        """
        result = getattr(script_run_cli, validator)(data)
        assert result.ok is False
        assert expected_reason_fragment in result.reason

    @pytest.mark.parametrize(
        ("validator", "data", "expected_reason_fragment"),
        [
            pytest.param("validate_rdeps_json", [], "object", id="rdeps-not-an-object"),
            pytest.param(
                "validate_rdeps_json", {"imported_by": "a.b", "module": "foo"}, "list", id="rdeps-imported_by-not-list"
            ),
            pytest.param(
                "validate_rdeps_json", {"imported_by": [], "module": 42}, "string", id="rdeps-module-not-string"
            ),
            pytest.param("validate_deps_json", [], "object", id="deps-not-an-object"),
            pytest.param(
                "validate_deps_json",
                {"direct_imports": "x.y", "module": "foo"},
                "list",
                id="deps-direct_imports-not-list",
            ),
            pytest.param(
                "validate_deps_json", {"direct_imports": [], "module": 42}, "string", id="deps-module-not-string"
            ),
        ],
    )
    def test_wrong_type_payloads_return_not_ok(
        self, script_run_cli: Any, validator: str, data: Any, expected_reason_fragment: str
    ) -> None:
        """Wrong JSON value types produce ok=False with a concrete reason.

        Scenario: right keys holding the wrong types (string for list, int for string) must not pass structurally.
        """
        result = getattr(script_run_cli, validator)(data)
        assert result.ok is False
        assert expected_reason_fragment in result.reason


# ===========================================================================
# run_scan_query — subprocess wrapper
# ===========================================================================


class TestRunScanQuery:
    """Validate run_scan_query subprocess contract via a mocked subprocess boundary."""

    @mock.patch(RUN_COMMAND, autospec=True)
    def test_returns_parsed_json_on_success(
        self, mock_run: mock.Mock, script_run_cli: Any, fake_scan_query: Path, tmp_path: Path
    ) -> None:
        """A zero-exit scan-query with valid JSON stdout returns the parsed dict.

        Scenario: the happy path of the wrapper; the real scan-query binary is not under test here.
        """
        payload = {"imported_by": ["a.b"], "module": "foo"}
        mock_run.return_value = _proc(stdout=json.dumps(payload))
        result = script_run_cli.run_scan_query(fake_scan_query, ["rdeps", "foo"], tmp_path / "index.json", tmp_path)
        assert result == payload

    @pytest.mark.parametrize(
        "outcome",
        [
            pytest.param(_proc(returncode=1), id="nonzero-exit"),
            pytest.param(_proc(stdout="not json at all"), id="non-json-stdout"),
            pytest.param(subprocess.TimeoutExpired(cmd=[], timeout=30), id="timeout"),
            pytest.param(OSError("file not found"), id="unavailable-binary"),
        ],
    )
    @mock.patch(RUN_COMMAND, autospec=True)
    def test_returns_none_on_failure(
        self, mock_run: mock.Mock, script_run_cli: Any, fake_scan_query: Path, tmp_path: Path, outcome: Any
    ) -> None:
        """Every failure mode collapses to None without raising.

        Scenario: a non-zero exit, zero-exit non-JSON output, a timeout or a missing binary must each be survivable
        by callers that only check for None.
        """
        mock_run.side_effect = [outcome]
        result = script_run_cli.run_scan_query(fake_scan_query, ["rdeps", "foo"], tmp_path / "index.json", tmp_path)
        assert result is None

    @pytest.mark.parametrize(
        "args",
        [pytest.param(["rdeps", "pkg.mod"], id="rdeps"), pytest.param(["deps", "some.module"], id="deps")],
    )
    @mock.patch(RUN_COMMAND, autospec=True)
    def test_command_order_and_cwd(
        self, mock_run: mock.Mock, script_run_cli: Any, fake_scan_query: Path, tmp_path: Path, args: list[str]
    ) -> None:
        """The subprocess gets python3, the script, --index <path>, then the subcommand, with cwd set to the repo.

        Scenario: the argv layout is the contract with scan-query; --index must precede the subcommand so the
        index is honoured regardless of cwd or git availability.
        """
        index_path = tmp_path / "idx.json"
        mock_run.return_value = _proc(stdout="{}")
        script_run_cli.run_scan_query(fake_scan_query, args, index_path, tmp_path)
        expected = ["python3", str(fake_scan_query.resolve()), "--index", str(index_path.resolve()), *args]
        mock_run.assert_called_once_with(expected, cwd=str(tmp_path))


class TestRunScanQueryResult:
    """Validate run_scan_query_result reports errors distinctly from empty results."""

    @mock.patch(RUN_COMMAND, autospec=True)
    def test_success_sets_data_and_ok(
        self, mock_run: mock.Mock, script_run_cli: Any, fake_scan_query: Path, tmp_path: Path
    ) -> None:
        """A zero-exit JSON response yields ok=True with parsed data and no error.

        Scenario: an empty-but-valid payload must stay distinguishable from a failed query.
        """
        mock_run.return_value = _proc(stdout=json.dumps({"imported_by": []}))
        res = script_run_cli.run_scan_query_result(fake_scan_query, ["rdeps", "x"], tmp_path / "i.json", tmp_path)
        assert res.ok is True
        assert res.error is None
        assert res.data == {"imported_by": []}

    @pytest.mark.parametrize(
        ("outcome", "expected_error"),
        [
            pytest.param(_proc(returncode=2, stderr="module not found: foo\n"), "module not found", id="stderr-reason"),
            pytest.param(subprocess.TimeoutExpired(cmd=[], timeout=30), "timeout", id="timeout"),
            pytest.param(OSError("missing executable"), "os error", id="os-error"),
        ],
    )
    @mock.patch(RUN_COMMAND, autospec=True)
    def test_failure_carries_error_reason(
        self,
        mock_run: mock.Mock,
        script_run_cli: Any,
        fake_scan_query: Path,
        tmp_path: Path,
        outcome: Any,
        expected_error: str,
    ) -> None:
        """A non-zero exit, timeout or OS error yields ok=False with no data and the reason that run_scan_query drops.

        Scenario: the wrapper collapses all failures to None; this result type keeps the cause for diagnostics.
        """
        mock_run.side_effect = [outcome]
        res = script_run_cli.run_scan_query_result(fake_scan_query, ["rdeps", "foo"], tmp_path / "i.json", tmp_path)
        assert res.ok is False
        assert res.data is None
        assert expected_error in res.error

    @mock.patch(RUN_COMMAND, autospec=True)
    def test_codemap_rdeps_result_reports_error(
        self, mock_run: mock.Mock, script_run_cli: Any, fake_scan_query: Path, tmp_path: Path
    ) -> None:
        """codemap_rdeps_result returns an error reason instead of a silent empty set.

        Scenario: a failing rdeps query must not look like a module with no importers, or accuracy would score 1.0.
        """
        mock_run.return_value = _proc(returncode=1, stderr="no such module")
        importers, err = script_run_cli.codemap_rdeps_result(fake_scan_query, tmp_path / "i.json", tmp_path, "foo")
        assert importers == set()
        assert err is not None
        assert "no such module" in err


# ===========================================================================
# resolve_index_path
# ===========================================================================


class TestResolveIndexPath:
    """Validate resolve_index_path resolution order."""

    def test_explicit_arg_returned_directly(self, script_run_cli: Any, tmp_path: Path) -> None:
        """An explicit arg is returned as a Path without further resolution.

        Scenario: --index-path points at a file that does not exist; the user's choice must win untouched.
        """
        result = script_run_cli.resolve_index_path("/custom/path/index.json", tmp_path)
        assert result == Path("/custom/path/index.json")

    def test_finds_index_in_cache_codemap_by_repo_name(self, script_run_cli: Any, tmp_path: Path) -> None:
        """Index at <repo>/.cache/codemap/<name>.json is discovered automatically.

        Scenario: a repo indexed by scan-index with the default cache layout needs no flag to be found.
        """
        cache_dir = tmp_path / ".cache" / "codemap"
        cache_dir.mkdir(parents=True)
        index_file = cache_dir / f"{tmp_path.name}.json"
        index_file.write_text("{}", encoding="utf-8")

        result = script_run_cli.resolve_index_path(None, tmp_path)
        assert result == index_file

    def test_finds_index_after_stripping_master_suffix(self, script_run_cli: Any, tmp_path: Path) -> None:
        """A repo dir named '<name>-master' resolves to '<name>.json' in the cache.

        Scenario: GitHub zip downloads name the directory pytorch-lightning-master while the index uses the bare name.
        """
        repo = tmp_path / "pytorch-lightning-master"
        repo.mkdir()
        cache_dir = repo / ".cache" / "codemap"
        cache_dir.mkdir(parents=True)
        index_file = cache_dir / "pytorch-lightning.json"
        index_file.write_text("{}", encoding="utf-8")

        result = script_run_cli.resolve_index_path(None, repo)
        assert result == index_file

    def test_falls_back_to_first_json_in_cache_codemap(self, script_run_cli: Any, tmp_path: Path) -> None:
        """When name-based lookup fails, the first *.json in .cache/codemap/ is used.

        Scenario: the index file name does not match the repo directory name, so only the fallback can find it.
        """
        cache_dir = tmp_path / ".cache" / "codemap"
        cache_dir.mkdir(parents=True)
        fallback = cache_dir / "anything.json"
        fallback.write_text("{}", encoding="utf-8")

        result = script_run_cli.resolve_index_path(None, tmp_path)
        assert result == fallback

    def test_returns_default_path_when_no_index_found(self, script_run_cli: Any, tmp_path: Path) -> None:
        """When no index exists the canonical default path under .cache/codemap/ is returned.

        Scenario: no cache directory at all; callers get a well-formed path to report or build at, never None.
        """
        result = script_run_cli.resolve_index_path(None, tmp_path)
        assert result == tmp_path / ".cache" / "codemap" / f"{tmp_path.name}.json"


# ===========================================================================
# resolve_repo_path
# ===========================================================================


class TestResolveRepoPath:
    """Validate resolve_repo_path resolution order."""

    def test_explicit_existing_dir_returns_path(self, script_run_cli: Any, tmp_path: Path) -> None:
        """A valid existing directory arg is returned as a Path.

        Scenario: --repo-path pointing at a real directory is used as-is.
        """
        assert script_run_cli.resolve_repo_path(str(tmp_path)) == tmp_path

    def test_explicit_nonexistent_dir_returns_none(self, script_run_cli: Any, tmp_path: Path) -> None:
        """An arg pointing to a non-existent directory returns None.

        Scenario: a mistyped --repo-path is reported as unresolved, never silently replaced by another source.
        """
        assert script_run_cli.resolve_repo_path(str(tmp_path / "no_such_dir")) is None

    def test_env_var_used_when_arg_absent(
        self, script_run_cli: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PYTORCH_LIGHTNING_PATH is consulted when no arg is given.

        Scenario: CI sets the env var instead of passing a flag.
        """
        monkeypatch.setenv("PYTORCH_LIGHTNING_PATH", str(tmp_path))
        assert script_run_cli.resolve_repo_path(None) == tmp_path

    def test_env_var_nonexistent_dir_falls_through(
        self, script_run_cli: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PYTORCH_LIGHTNING_PATH pointing at a missing dir falls through to the local clone check.

        Scenario: run from an empty cwd, so the ./.sandbox/pytorch-lightning fallback is absent too.
        """
        monkeypatch.setenv("PYTORCH_LIGHTNING_PATH", str(tmp_path / "no_such"))
        monkeypatch.chdir(tmp_path)
        assert script_run_cli.resolve_repo_path(None) is None

    def test_no_arg_no_env_no_local_returns_none(
        self, script_run_cli: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no arg, no env var and no local clone, None is returned.

        Scenario: all three resolution sources are absent; the cwd is an empty directory.
        """
        monkeypatch.delenv("PYTORCH_LIGHTNING_PATH", raising=False)
        monkeypatch.chdir(tmp_path)
        assert script_run_cli.resolve_repo_path(None) is None


# ===========================================================================
# find_codemap_bin
# ===========================================================================


class TestFindCodemapBin:
    """Validate find_codemap_bin resolution logic."""

    @mock.patch("shutil.which", autospec=True, return_value=None)
    def test_returns_none_when_not_on_path_and_no_plugin_root(self, mock_which: mock.Mock, script_run_cli: Any) -> None:
        """A binary absent from PATH with no plugin_root resolves to None.

        Scenario: neither lookup source exists, so the suites must skip instead of guessing a path.
        """
        assert script_run_cli.find_codemap_bin("scan-query", plugin_root=None) is None

    @mock.patch("shutil.which", autospec=True, return_value="/usr/local/bin/scan-query")
    def test_finds_binary_via_shutil_which(self, mock_which: mock.Mock, script_run_cli: Any) -> None:
        """A binary present on PATH is returned as a Path.

        Scenario: an installed plugin exposes scan-query on PATH.
        """
        assert script_run_cli.find_codemap_bin("scan-query", plugin_root=None) == Path("/usr/local/bin/scan-query")

    @mock.patch("shutil.which", autospec=True, return_value=None)
    def test_finds_binary_in_plugin_root_when_not_on_path(
        self, mock_which: mock.Mock, script_run_cli: Any, tmp_path: Path
    ) -> None:
        """A binary missing from PATH is found under plugin_root/plugins/codemap-py/bin/.

        Scenario: running from a source checkout, where the plugin's bin directory is not on PATH.
        """
        bin_dir = tmp_path / "plugins" / "codemap-py" / "bin"
        bin_dir.mkdir(parents=True)
        binary = bin_dir / "scan-query"
        binary.write_text("#!/usr/bin/env python3\n")

        assert script_run_cli.find_codemap_bin("scan-query", plugin_root=tmp_path) == binary

    @mock.patch("shutil.which", autospec=True, return_value=None)
    def test_returns_none_when_plugin_root_lacks_binary(
        self, mock_which: mock.Mock, script_run_cli: Any, tmp_path: Path
    ) -> None:
        """A plugin_root without the binary file resolves to None.

        Scenario: a checkout that does not ship the codemap-py plugin must not yield a path to a missing file.
        """
        assert script_run_cli.find_codemap_bin("scan-query", plugin_root=tmp_path) is None

    @mock.patch("shutil.which", autospec=True, return_value=None)
    def test_legacy_codemap_path_never_selected(
        self, mock_which: mock.Mock, script_run_cli: Any, tmp_path: Path
    ) -> None:
        """Only the retired plugins/codemap/bin/ holding the binary resolves to None.

        The resolver targets the renamed ``codemap-py`` identity exclusively, so a stale pre-rename checkout must not
        yield a false-green binary path.
        """
        legacy = tmp_path / "plugins" / "codemap" / "bin"
        legacy.mkdir(parents=True)
        (legacy / "scan-query").write_text("#!/usr/bin/env python3\n")

        assert script_run_cli.find_codemap_bin("scan-query", plugin_root=tmp_path) is None


# ===========================================================================
# THRESHOLDS config sanity
# ===========================================================================


class TestThresholdsConfig:
    """Smoke-check that THRESHOLDS dict has expected keys and numeric values."""

    @pytest.mark.parametrize(
        ("key", "sub_key", "lo", "hi"),
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
        """Each threshold is a number inside a plausible range.

        Scenario: a unit slip (seconds for milliseconds, percent for fraction) lands outside the range and is caught.
        """
        value = script_run_cli.THRESHOLDS[key][sub_key]
        assert isinstance(value, (int, float)), f"THRESHOLDS[{key!r}][{sub_key!r}] is not numeric"
        assert lo <= value <= hi, f"THRESHOLDS[{key!r}][{sub_key!r}]={value} not in [{lo}, {hi}]"

    @pytest.mark.parametrize("key", ["Q_fix", "Q_feature", "Q_refactor"])
    def test_query_shape_thresholds_have_boolean_flags(self, script_run_cli: Any, key: str) -> None:
        """Query-shape thresholds require block_present and json_valid to be True.

        Scenario: the three query-shape scenarios gate on the same two structural flags.
        """
        thresholds = script_run_cli.THRESHOLDS[key]
        assert (thresholds["block_present"], thresholds["json_valid"]) == (True, True)


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
        """The central subcommand returns a dict that passes validate_central_json.

        Scenario: real scan-query against a cloned requests repo; skipped unless network tests are opted in.
        """
        repo, index = sample_repo
        data = script_run_cli.run_scan_query(scan_query_binary, ["central"], index, repo)
        assert data is not None, "run_scan_query returned None for 'central'"
        result = script_run_cli.validate_central_json(data)
        assert result.ok, f"validate_central_json failed: {result.reason}"

    def test_rdeps_known_module_returns_valid_structure(
        self, script_run_cli: Any, scan_query_binary: Any, sample_repo: Any
    ) -> None:
        """Rdeps for requests.api returns a dict that passes validate_rdeps_json.

        Scenario: real scan-query against a cloned requests repo; skipped unless network tests are opted in.
        """
        repo, index = sample_repo
        data = script_run_cli.run_scan_query(scan_query_binary, ["rdeps", "requests.api"], index, repo)
        assert data is not None, "run_scan_query returned None for 'rdeps requests.api'"
        result = script_run_cli.validate_rdeps_json(data)
        assert result.ok, f"validate_rdeps_json failed: {result.reason}"

    def test_deps_known_module_returns_valid_structure(
        self, script_run_cli: Any, scan_query_binary: Any, sample_repo: Any
    ) -> None:
        """Deps for requests.api returns a dict that passes validate_deps_json.

        Scenario: real scan-query against a cloned requests repo; skipped unless network tests are opted in.
        """
        repo, index = sample_repo
        data = script_run_cli.run_scan_query(scan_query_binary, ["deps", "requests.api"], index, repo)
        assert data is not None, "run_scan_query returned None for 'deps requests.api'"
        result = script_run_cli.validate_deps_json(data)
        assert result.ok, f"validate_deps_json failed: {result.reason}"

    def test_unknown_subcommand_returns_none(
        self, script_run_cli: Any, scan_query_binary: Any, sample_repo: Any
    ) -> None:
        """An unrecognised subcommand makes run_scan_query return None.

        Scenario: real scan-query rejects the subcommand with a non-zero exit; needs opt-in network tests.
        """
        repo, index = sample_repo
        data = script_run_cli.run_scan_query(scan_query_binary, ["not-a-real-subcommand"], index, repo)
        assert data is None

    def test_central_entries_have_name_and_rdep_count(
        self, script_run_cli: Any, scan_query_binary: Any, sample_repo: Any
    ) -> None:
        """Every entry in the central list has both 'name' and 'rdep_count' fields.

        Scenario: real scan-query against a cloned requests repo; skipped unless network tests are opted in.
        """
        repo, index = sample_repo
        data = script_run_cli.run_scan_query(scan_query_binary, ["central"], index, repo)
        assert data is not None
        assert [e for e in data["central"] if "name" not in e or "rdep_count" not in e] == []


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
        """run_measure_calls returns exactly 3 ScenarioResult objects.

        Scenario: needs an indexed pytorch-lightning checkout and skips without one.
        """
        results = script_run_cli.run_measure_calls(pytorch_lightning_repo, scan_query_binary, pytorch_lightning_index)
        assert len(results) == 3

    def test_scenario_ids(
        self, script_run_cli: Any, pytorch_lightning_repo: Any, scan_query_binary: Any, pytorch_lightning_index: Any
    ) -> None:
        """The three results have scenario IDs C1, C2, C3.

        Scenario: needs an indexed pytorch-lightning checkout and skips without one.
        """
        results = script_run_cli.run_measure_calls(pytorch_lightning_repo, scan_query_binary, pytorch_lightning_index)
        assert {r.scenario for r in results} == {"C1", "C2", "C3"}

    def test_results_are_scenario_result_instances(
        self, script_run_cli: Any, pytorch_lightning_repo: Any, scan_query_binary: Any, pytorch_lightning_index: Any
    ) -> None:
        """Every item returned is a ScenarioResult dataclass.

        Scenario: needs an indexed pytorch-lightning checkout and skips without one.
        """
        results = script_run_cli.run_measure_calls(pytorch_lightning_repo, scan_query_binary, pytorch_lightning_index)
        assert all(isinstance(r, script_run_cli.ScenarioResult) for r in results)


@pytest.mark.integration
@RAW_CODEMAP_LAUNCHERS
@PYTORCH_LIGHTNING_CHECKOUT
@PYTORCH_LIGHTNING_INDEX
class TestIntegrationSuiteA:
    """Suite A: accuracy measurement against pytorch-lightning."""

    def test_returns_three_results(
        self, script_run_cli: Any, pytorch_lightning_repo: Any, scan_query_binary: Any, pytorch_lightning_index: Any
    ) -> None:
        """run_measure_accuracy returns exactly 3 ScenarioResult objects.

        Scenario: needs an indexed pytorch-lightning checkout and skips without one.
        """
        results = script_run_cli.run_measure_accuracy(
            pytorch_lightning_repo, scan_query_binary, pytorch_lightning_index
        )
        assert len(results) == 3

    def test_scenario_ids(
        self, script_run_cli: Any, pytorch_lightning_repo: Any, scan_query_binary: Any, pytorch_lightning_index: Any
    ) -> None:
        """The three results have scenario IDs A1, A2, A3.

        Scenario: needs an indexed pytorch-lightning checkout and skips without one.
        """
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
        """run_measure_latency returns exactly 4 ScenarioResult objects.

        Scenario: needs an indexed pytorch-lightning checkout and skips without one.
        """
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
        """The four results have scenario IDs L1, L2, L3, L4.

        Scenario: needs an indexed pytorch-lightning checkout and skips without one.
        """
        results = script_run_cli.run_measure_latency(
            pytorch_lightning_repo, scan_query_binary, pytorch_lightning_index, scan_index_binary
        )
        assert {r.scenario for r in results} == {"L1", "L2", "L3", "L4"}


class TestMeasureLatencyIndexBuild:
    """Validate the latency suite's index-rebuild scenario leaves a pre-built index untouched."""

    @pytest.mark.parametrize(
        "times_out",
        [pytest.param(False, id="completed-scan"), pytest.param(True, id="timed-out-scan")],
    )
    @mock.patch("_bench_query.suites.subprocess.run", autospec=True)
    @mock.patch("_bench_query.suites.time_commands", autospec=True)
    @mock.patch("_bench_query.suites.time_command", autospec=True)
    @mock.patch("_bench_query.suites.load_tasks", autospec=True)
    def test_restores_prebuilt_index_bytes(
        self,
        mock_load_tasks: mock.Mock,
        mock_time_command: mock.Mock,
        mock_time_commands: mock.Mock,
        mock_run: mock.Mock,
        script_run_cli: Any,
        make_task: Callable[..., Any],
        tmp_path: Path,
        times_out: bool,
    ) -> None:
        """Completed or timed-out L3 scans never leave a frozen index rewritten.

        Scenario: the L3 index build overwrites the index file in place (and may time out half way); the suite must
        put the original bytes back. Timing and the scan-index subprocess are stubbed; only the restore is under test.
        """
        index_path = tmp_path / "repo.json"
        index_path.write_bytes(b"locked-index")
        timing = script_run_cli.TimingStats(min_ms=1.0, median_ms=1.0, max_ms=1.0, n=1)
        mock_load_tasks.return_value = [make_task(queries=[])]
        mock_time_command.return_value = timing
        mock_time_commands.return_value = timing

        def _mutate_index(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            """Replace fixture index bytes and optionally simulate an indexing timeout."""
            index_path.write_bytes(b"rebuilt-index")
            if times_out:
                raise subprocess.TimeoutExpired(["scan-index"], 120)
            return subprocess.CompletedProcess([], 0, "", "")

        mock_run.side_effect = _mutate_index

        script_run_cli.run_measure_latency(tmp_path, tmp_path / "scan-query", index_path, tmp_path / "scan-index")

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
        """run_measure_query_shape returns exactly 3 ScenarioResult objects.

        Scenario: needs an indexed pytorch-lightning checkout and skips without one.
        """
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
        """The three results have scenario IDs Q_fix, Q_feature, Q_refactor.

        Scenario: needs an indexed pytorch-lightning checkout and skips without one.
        """
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
        """A test package below a non-package directory keeps its import identity.

        Scenario: tests/tests_fabric/ is a package while tests/ is not, so the importer is named
        tests_fabric.test_target and scoring it against the grep floor must give full precision and recall.
        """
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
        """Only literal, boundary-anchored importers are returned; relatives and substring decoys are excluded.

        Scenario: 'import pkg.target_helper' must not match a search for 'pkg.target', and relative imports are
        invisible to grep by design (the AST oracle recovers them).
        """
        result = script_run_cli.grep_importers_boundary(sample_pkg, "pkg.target")
        assert result == {"pkg.imp_from", "pkg.imp_plain", "pkg.imp_alias"}

    def test_no_match_returns_empty_set(self, script_run_cli: Any, sample_pkg: Path) -> None:
        """A module with no importers yields an empty set.

        Scenario: searching for a module that nothing imports must return set(), not None or an error.
        """
        assert script_run_cli.grep_importers_boundary(sample_pkg, "pkg.nonexistent") == set()


class TestModuleToSourceFile:
    """Validate module_to_source_file resolution."""

    @pytest.mark.parametrize(
        ("module", "relpath"),
        [
            pytest.param("pkg.target", "pkg/target.py", id="regular-module"),
            pytest.param("pkg", "pkg/__init__.py", id="package-init"),
        ],
    )
    def test_resolves_module_to_source_file(
        self, script_run_cli: Any, sample_pkg: Path, module: str, relpath: str
    ) -> None:
        """A dotted module resolves to its .py file, and a package resolves to its __init__.py.

        Scenario: the AST oracle needs the real file behind a module name before it can parse the imports.
        """
        assert script_run_cli.module_to_source_file(module, sample_pkg) == sample_pkg / relpath

    def test_missing_module_returns_none(self, script_run_cli: Any, sample_pkg: Path) -> None:
        """An unknown module resolves to None.

        Scenario: a candidate module with no file must be reported as unresolved, not crash the oracle.
        """
        assert script_run_cli.module_to_source_file("pkg.nope.here", sample_pkg) is None


class TestResolveRelative:
    """Validate _resolve_relative dotted-base resolution."""

    @pytest.mark.parametrize(
        ("base", "level", "module", "expected"),
        [
            pytest.param("pkg.rel", 2, "target", "pkg.target", id="up-one-with-module"),
            pytest.param("pkg.rel", 1, None, "pkg.rel", id="current-package-bare"),
            pytest.param("pkg.rel", 1, "x", "pkg.rel.x", id="current-package-with-module"),
            pytest.param("pkg", 1, "target", "pkg.target", id="top-level-package"),
            pytest.param("a.b.c", 3, "d", "a.d", id="up-two"),
        ],
    )
    def test_resolution(self, script_run_cli: Any, base: str, level: int, module: str | None, expected: str) -> None:
        """A relative-import base resolves per level (leading dots) and module suffix.

        Scenario: the arithmetic behind 'from ..target import X' in a nested package, from level 1 up to level 3.
        """
        assert script_run_cli._resolve_relative(base, level, module) == expected


class TestFileImportsModule:
    """Validate file_imports_module AST verification across import forms."""

    @pytest.mark.parametrize(
        ("relpath", "expected"),
        [
            pytest.param("pkg/imp_from.py", True, id="from-import-importer"),
            pytest.param("pkg/imp_plain.py", True, id="plain-import-importer"),
            pytest.param("pkg/imp_alias.py", True, id="aliased-import-importer"),
            pytest.param("pkg/rel/rel_from.py", True, id="relative-from-importer"),
            pytest.param("pkg/rel/rel_bare.py", True, id="relative-bare-importer"),
            pytest.param("pkg/imp_decoy.py", False, id="decoy-importing-sibling"),
            pytest.param("pkg/unrelated.py", False, id="unrelated-file"),
            pytest.param("pkg/bad.py", False, id="unparsable-file"),
        ],
    )
    def test_confirms_only_genuine_importers(
        self, script_run_cli: Any, sample_pkg: Path, relpath: str, expected: bool
    ) -> None:
        """Every genuine importer of pkg.target is confirmed by AST; non-importers, decoys, unparsable files are not.

        Scenario: the sample package mixes literal, aliased and relative importers with a decoy and a syntax error.
        """
        assert script_run_cli.file_imports_module(sample_pkg / relpath, "pkg.target", sample_pkg) is expected


class TestVerifyImporter:
    """Validate verify_importer end-to-end (module name → file → AST check)."""

    @pytest.mark.parametrize(
        ("candidate", "expected"),
        [
            pytest.param("pkg.rel.rel_from", True, id="verifies-relative-extra"),
            pytest.param("pkg.imp_decoy", False, id="rejects-decoy"),
            pytest.param("pkg.ghost", False, id="missing-candidate-module"),
        ],
    )
    def test_verifies_only_true_importers(
        self, script_run_cli: Any, sample_pkg: Path, candidate: str, expected: bool
    ) -> None:
        """A grep-missed relative importer is verified; a decoy or a module with no file is not.

        Scenario: candidate names reported by codemap are mapped to files and AST-checked against pkg.target.
        """
        assert script_run_cli.verify_importer(candidate, "pkg.target", sample_pkg) is expected


# ===========================================================================
# Infeasible-path measurement
# ===========================================================================


class TestMeasureInfeasiblePaths:
    """Validate _measure_infeasible_paths direct-edge detection."""

    @pytest.mark.parametrize(
        ("source", "infeasible", "direct_edge"),
        [
            pytest.param("pkg.imp_from", 0, True, id="direct-edge-is-feasible"),  # imports pkg.target directly
            pytest.param("pkg.unrelated", 1, False, id="missing-edge-is-infeasible"),  # no import of pkg.target
        ],
    )
    def test_path_feasibility_follows_direct_edge(
        self,
        script_run_cli: Any,
        make_task: Callable[..., Any],
        sample_pkg: Path,
        source: str,
        infeasible: int,
        direct_edge: bool,
    ) -> None:
        """A path is feasible when the source directly imports the target, and infeasible when it does not.

        Scenario: one path query from the source module to pkg.target, checked against real import edges on disk.
        """
        task = make_task(primary_module=source, queries=[{"cmd": "path", "args": [source, "pkg.target"]}])
        fraction, infeasible_count, total, detail = script_run_cli._measure_infeasible_paths([task], sample_pkg)
        assert total == 1
        assert infeasible_count == infeasible
        assert fraction == float(infeasible)
        assert detail[0]["direct_edge"] is direct_edge


# ===========================================================================
# JSON output — emit + summary envelope
# ===========================================================================


class TestEmit:
    """Validate emit prints one compact JSON line with the dataclass fields."""

    def test_emits_single_json_line_with_all_fields(self, script_run_cli: Any, capsys: pytest.CaptureFixture) -> None:
        """Emit prints one JSON line whose keys mirror the ScenarioResult fields.

        Scenario: stdout is consumed as JSON lines, so exactly one newline and the full field set are required.
        """
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

    def test_aggregates_suites_and_totals(
        self, script_run_cli: Any, make_result: Callable[..., Any], tmp_path: Path
    ) -> None:
        """The envelope reports per-suite pass/total and overall scenario counts.

        Scenario: two calls scenarios (one failed) and one passing accuracy scenario under a PARTIAL verdict.
        """
        results = [make_result("C1", "calls"), make_result("C2", "calls", False), make_result("A1", "accuracy")]
        env = script_run_cli.build_summary_envelope(results, tmp_path, tmp_path / "i.json", "PARTIAL")
        assert env["verdict"] == "PARTIAL"
        assert env["scenarios_passed"] == 2
        assert env["scenarios_total"] == 3
        assert env["suites"]["calls"] == {"passed": 1, "total": 2}
        assert env["suites"]["accuracy"] == {"passed": 1, "total": 1}
        assert env["repo"] == str(tmp_path)
        assert env["index"] == str(tmp_path / "i.json")
        assert env["date"] == date.today().isoformat()

    def test_is_json_serializable(self, script_run_cli: Any, make_result: Callable[..., Any], tmp_path: Path) -> None:
        """The envelope round-trips through json.dumps/loads with its structure intact.

        Scenario: the envelope is printed as JSON for machine consumers, so paths and counts must be plain types.
        """
        env = script_run_cli.build_summary_envelope([make_result("C1", "calls")], tmp_path, tmp_path / "i.json", "PASS")
        assert json.loads(json.dumps(env))["suites"]["calls"]["passed"] == 1


# ===========================================================================
# Report path — single-resolve regression
# ===========================================================================


def _write_stub_report(results: Any, repo: Path, index: Path, path: Path) -> None:
    """Write a minimal report at the requested output path, standing in for the real renderer."""
    Path(path).write_text("report", encoding="utf-8")


class TestWriteReportFile:
    """Validate that the printed report path equals the file actually written."""

    @mock.patch("_bench_query.report.render_report", autospec=True, side_effect=_write_stub_report)
    def test_returned_path_is_the_written_file(
        self, mock_render: mock.Mock, script_run_cli: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """write_report_file returns the exact path render_report wrote, with no -2 drift.

        Scenario: the destination is resolved once before rendering; resolving again after the file exists would
        print a -2 sibling that was never created.
        """
        monkeypatch.chdir(tmp_path)
        returned = script_run_cli.write_report_file([], tmp_path, tmp_path / "i.json")
        assert Path(returned) == mock_render.call_args.args[3]
        assert Path(returned).exists()
        assert Path(returned).name == f"code-{date.today().isoformat()}.md"

    @mock.patch("_bench_query.report.render_report", autospec=True, side_effect=_write_stub_report)
    def test_second_call_uses_counter_suffix(
        self, mock_render: mock.Mock, script_run_cli: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A second run on the same day writes a distinct code-<date>-2.md that also exists.

        Scenario: two runs on one day must not overwrite each other's report.
        """
        monkeypatch.chdir(tmp_path)
        first = script_run_cli.write_report_file([], tmp_path, tmp_path / "i.json")
        second = script_run_cli.write_report_file([], tmp_path, tmp_path / "i.json")
        assert first != second
        assert Path(first).exists()
        assert Path(second).exists()
        assert Path(second).name == f"code-{date.today().isoformat()}-2.md"

    def test_resolve_report_path_has_no_filesystem_side_effect(
        self, script_run_cli: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """resolve_report_path only computes a path and never creates benchmarks/results/.

        Scenario: resolution is pure so callers decide when to create directories.
        """
        monkeypatch.chdir(tmp_path)
        path = script_run_cli.resolve_report_path()
        assert path.name == f"code-{date.today().isoformat()}.md"
        assert not (tmp_path / "benchmarks" / "results").exists()
        assert not path.exists()


# ===========================================================================
# AST-oracle accuracy scoring — precision oracle + grep recall floor
# ===========================================================================


class TestScoreRdepsAccuracy:
    """Validate score_rdeps_accuracy uses AST precision and a grep recall floor."""

    def test_grep_invisible_true_importer_not_penalised(self, script_run_cli: Any, sample_pkg: Path) -> None:
        """An AST-verified relative importer that grep cannot see counts as precise, not a false positive.

        Scenario: codemap reports pkg.rel.rel_from on top of the grep floor; the oracle confirms it really imports.
        """
        grep_floor = script_run_cli.grep_importers_boundary(sample_pkg, "pkg.target")
        codemap = grep_floor | {"pkg.rel.rel_from"}  # relative importer grep misses
        stats = script_run_cli.score_rdeps_accuracy(codemap, grep_floor, "pkg.target", sample_pkg)
        assert stats.precision == pytest.approx(1.0)
        assert stats.fp == 0
        assert stats.recall == pytest.approx(1.0)

    def test_ast_rejected_member_is_false_positive(self, script_run_cli: Any, sample_pkg: Path) -> None:
        """A codemap member the AST oracle rejects is scored as a false positive.

        Scenario: codemap lists a decoy that imports pkg.target_helper, so half of its answer is wrong.
        """
        grep_floor = script_run_cli.grep_importers_boundary(sample_pkg, "pkg.target")
        codemap = {"pkg.imp_from", "pkg.imp_decoy"}  # decoy imports pkg.target_helper, not pkg.target
        stats = script_run_cli.score_rdeps_accuracy(codemap, grep_floor, "pkg.target", sample_pkg)
        assert stats.precision == pytest.approx(0.5)
        assert stats.fp == 1
        assert stats.fp_modules == ["pkg.imp_decoy"]

    def test_recall_floor_penalises_missed_grep_importer(self, script_run_cli: Any, sample_pkg: Path) -> None:
        """A boundary-grep importer that codemap omits lowers recall and is listed as a false negative.

        Scenario: grep is the recall floor, so anything it finds that codemap misses is a real miss.
        """
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

    @mock.patch("_bench_query.suites.codemap_rdeps_result", autospec=True, return_value=(set(), "exit 1: boom"))
    def test_errored_task_not_scored_as_pass(
        self, mock_rdeps: mock.Mock, script_run_cli: Any, make_task: Callable[..., Any], tmp_path: Path
    ) -> None:
        """A scan-query error marks the task errored and fails A1 instead of passing with precision 1.0.

        Scenario: a high-risk module whose rdeps query crashed; an empty importer set must not read as a clean score.
        """
        rows = script_run_cli._score_accuracy_tasks([make_task()], tmp_path / "sq", tmp_path / "i.json", tmp_path)
        assert rows[0]["errored"] is True
        a1 = script_run_cli._a1_scenario(rows)
        assert a1.passed is False
        assert "pkg.mod" in a1.result["errored"]


# ===========================================================================
# A2 vacuous empty result — precision-only scenario must not free-pass on empty
# ===========================================================================


class TestA2VacuousEmpty:
    """Validate a non-errored EMPTY low-risk codemap result is N/A for A2, never a precision=1.0 pass."""

    @mock.patch("_bench_query.suites.grep_importers_boundary", autospec=True, return_value=set())
    @mock.patch("_bench_query.suites.codemap_rdeps_result", autospec=True, return_value=(set(), None))
    def test_empty_codemap_is_not_a_free_a2_pass(
        self,
        mock_rdeps: mock.Mock,
        mock_grep: mock.Mock,
        script_run_cli: Any,
        make_task: Callable[..., Any],
        tmp_path: Path,
    ) -> None:
        """An empty non-errored low-risk result is excluded as vacuous and fails A2 on its own.

        Scenario: a low-risk module with no importers scores precision 1.0 trivially; that must not count as a pass.
        """
        task = make_task(id="B-04", risk_tier="low")
        rows = script_run_cli._score_accuracy_tasks([task], tmp_path / "sq", tmp_path / "i.json", tmp_path)
        assert rows[0]["codemap_count"] == 0
        assert rows[0]["errored"] is False
        a2 = script_run_cli._a2_scenario(rows)
        assert a2.passed is False
        assert a2.result["vacuous"] == ["pkg.mod"]

    def test_a2_passes_on_real_module_despite_empty_sibling(self, script_run_cli: Any) -> None:
        """A2 passes on a real precision=1.0 module while an empty sibling is N/A rather than a fail.

        Scenario: one module with two importers and one with none, both low risk and both at precision 1.0.
        """
        rows = [
            _accuracy_row("pkg.real", "low", codemap_count=2, precision=1.0),
            _accuracy_row("pkg.empty", "low", codemap_count=0, precision=1.0),
        ]
        a2 = script_run_cli._a2_scenario(rows)
        assert a2.passed is True
        assert a2.result["vacuous"] == ["pkg.empty"]


# ===========================================================================
# Verdict split — primary correctness vs self-consistency track
# ===========================================================================


class TestVerdictSplit:
    """Validate self-consistency suites are excluded from the primary verdict."""

    @pytest.mark.parametrize(
        ("scenarios", "expected"),
        [
            pytest.param(
                [("C1", "calls", True), ("S2", "symbol", False)], "PASS", id="failing-self-consistency-is-ignored"
            ),
            pytest.param(
                [("C1", "calls", False), ("A1", "accuracy", False), ("S2", "symbol", True)],
                "FAIL",
                id="passing-self-consistency-does-not-rescue",
            ),
        ],
    )
    def test_primary_verdict_ignores_self_consistency(
        self, script_run_cli: Any, make_result: Callable[..., Any], scenarios: list, expected: str
    ) -> None:
        """The primary verdict counts only primary suites, whatever the self-consistency track did.

        Scenario: a failing S scenario must not drag a passing primary run down, and a passing one must not lift a
        run whose primary scenarios all failed.
        """
        results = [make_result(*scenario) for scenario in scenarios]
        assert script_run_cli.compute_verdict(results) == expected

    @pytest.mark.parametrize(
        ("passed_flags", "expected"),
        [
            pytest.param([True, True, True], "CONSISTENT", id="all-pass"),
            pytest.param([True, False], "PARTIAL", id="half"),
            pytest.param([False, False], "INCONSISTENT", id="all-fail"),
        ],
    )
    def test_self_consistency_verdict(
        self, script_run_cli: Any, make_result: Callable[..., Any], passed_flags: list, expected: str
    ) -> None:
        """The self-consistency verdict reflects the pass ratio of the symbol/health/xrefs scenarios.

        Scenario: all pass is CONSISTENT, a mixed track PARTIAL, and an all-failing track INCONSISTENT.
        """
        results = [make_result(f"S_{i}", "symbol", flag) for i, flag in enumerate(passed_flags)]
        assert script_run_cli.compute_self_consistency(results)["verdict"] == expected

    def test_self_consistency_skipped_when_absent(self, script_run_cli: Any, make_result: Callable[..., Any]) -> None:
        """With no self-consistency scenarios the track verdict is SKIPPED.

        Scenario: a run with only a calls scenario has nothing to grade on the S/H/X track.
        """
        assert script_run_cli.compute_self_consistency([make_result("C1", "calls")])["verdict"] == "SKIPPED"

    def test_envelope_reports_primary_and_self_consistency(
        self, script_run_cli: Any, make_result: Callable[..., Any], tmp_path: Path
    ) -> None:
        """The summary envelope carries separate primary and self_consistency breakdowns.

        Scenario: one passing primary scenario and one failing symbol scenario land in different envelope keys.
        """
        results = [make_result("C1", "calls"), make_result("S2", "symbol", False)]
        env = script_run_cli.build_summary_envelope(results, tmp_path, tmp_path / "i.json", "PASS")
        assert env["primary"] == {"passed": 1, "total": 1}
        assert env["self_consistency"] == {"verdict": "INCONSISTENT", "passed": 0, "total": 1}


# ===========================================================================
# Accuracy tier coverage — every task graded, none silently dropped
# ===========================================================================


class TestAccuracyTierCoverage:
    """Validate A1 and A2 partition every risk tier so no accuracy task is silently ungraded."""

    def test_a1_and_a2_partition_all_tiers(self, script_run_cli: Any) -> None:
        """One row per known tier lands in exactly one of A1 or A2 (union is all, no overlap).

        Scenario: all six risk tiers at once; a tier in neither scenario goes ungraded, in both is double-counted.
        """
        tiers = ["high", "very-high", "moderate-high", "moderate", "low", "low-moderate"]
        rows = [_accuracy_row(f"pkg.{t}", t, codemap_count=1, precision=1.0, recall=1.0) for t in tiers]
        a1_mods = {m["module"] for m in script_run_cli._a1_scenario(rows).result["per_module"]}
        a2_mods = {m["module"] for m in script_run_cli._a2_scenario(rows).result["per_module"]}
        assert a1_mods | a2_mods == {r["module"] for r in rows}
        assert a1_mods & a2_mods == set()

    def test_previously_ungraded_tiers_now_graded_by_a2(self, script_run_cli: Any) -> None:
        """Moderate and low-moderate tiers, formerly ungraded, are now scored under A2.

        Scenario: regression guard for the gap where these two tiers fell between the A1 and A2 definitions.
        """
        rows = [
            _accuracy_row("pkg.mod", "moderate", codemap_count=1, precision=1.0),
            _accuracy_row("pkg.lm", "low-moderate", codemap_count=1, precision=1.0),
        ]
        graded = {m["module"] for m in script_run_cli._a2_scenario(rows).result["per_module"]}
        assert graded == {"pkg.mod", "pkg.lm"}


# ===========================================================================
# A1 per-module gating — a high group mean cannot mask a failing module
# ===========================================================================


class TestA1PerModuleGating:
    """Validate A1 PASS requires every module, not just a passing group mean."""

    def test_one_module_below_threshold_fails_despite_passing_mean(self, script_run_cli: Any) -> None:
        """Three perfect modules plus one at recall 0.5 keep the mean above threshold, yet A1 fails.

        Scenario: a high group average must not hide one module that misses its own recall threshold.
        """
        rows = [
            _accuracy_row("pkg.a", precision=1.0, recall=1.0),
            _accuracy_row("pkg.b", precision=1.0, recall=1.0),
            _accuracy_row("pkg.c", precision=1.0, recall=1.0),
            _accuracy_row("pkg.bad", precision=1.0, recall=0.5),
        ]
        a1 = script_run_cli._a1_scenario(rows)
        assert a1.passed is False
        assert a1.result["avg_recall"] >= script_run_cli.THRESHOLDS["A1"]["recall_min"]
        assert "pkg.bad" in a1.result["failing_modules"]

    def test_all_modules_meeting_threshold_pass(self, script_run_cli: Any) -> None:
        """Every high-risk module at or above both thresholds passes A1 with no failing modules.

        Scenario: the positive control for the per-module gate, with one module just inside each threshold.
        """
        rows = [
            _accuracy_row("pkg.a", precision=1.0, recall=1.0),
            _accuracy_row("pkg.b", precision=0.95, recall=0.9),
        ]
        a1 = script_run_cli._a1_scenario(rows)
        assert a1.passed is True
        assert a1.result["failing_modules"] == []


# ===========================================================================
# Hardware capture — latency gates are hardware-bound, host must be recorded
# ===========================================================================


class TestHardwareCapture:
    """Validate the report header and JSON envelope record the host for the hardware-calibrated gates."""

    def test_envelope_includes_platform_and_cpu_fields(
        self, script_run_cli: Any, make_result: Callable[..., Any], tmp_path: Path
    ) -> None:
        """The summary envelope carries a hardware dict with platform, processor, cpu_count and python.

        Scenario: latency thresholds are hardware-calibrated, so a result without its host is not comparable.
        """
        env = script_run_cli.build_summary_envelope([make_result("C1", "calls")], tmp_path, tmp_path / "i.json", "PASS")
        assert set(env["hardware"]) >= {"platform", "processor", "cpu_count", "python"}

    def test_report_header_records_hardware(
        self, script_run_cli: Any, make_result: Callable[..., Any], tmp_path: Path
    ) -> None:
        """The rendered report header names the hardware host for the latency thresholds.

        Scenario: a latency scenario is rendered to a real markdown file in tmp_path.
        """
        report = tmp_path / "r.md"
        script_run_cli.render_report([make_result("L1", "latency")], tmp_path, tmp_path / "i.json", report)
        assert "**Hardware**" in report.read_text()


# ===========================================================================
# Report reconciliation — S/H/X visible and header counts reconcile
# ===========================================================================


class TestReportReconciliation:
    """Validate every scenario counted in the header is visible in a rendered table (primary + S/H/X)."""

    def test_shx_rendered_and_header_counts_reconcile(
        self, script_run_cli: Any, make_result: Callable[..., Any], tmp_path: Path
    ) -> None:
        """S/H/X render in the self-consistency table and header counts match each track's size.

        Scenario: a mixed run of five primary and three self-consistency scenarios; no scenario may be counted in the
        header yet missing from every table.
        """
        results = [
            make_result("C1", "calls"),
            make_result("C2", "calls", False),
            make_result("A1", "accuracy"),
            make_result("L1", "latency"),
            make_result("Q_fix", "query-shape"),
            make_result("S2", "symbol"),
            make_result("H1", "health", False),
            make_result("X1", "xrefs"),
        ]
        report = tmp_path / "r.md"
        script_run_cli.render_report(results, tmp_path, tmp_path / "i.json", report)
        text = report.read_text()
        primary_total = len([r for r in results if r.suite in script_run_cli._PRIMARY_SUITES])
        sc = [r for r in results if r.suite in script_run_cli._SELF_CONSISTENCY_SUITES]
        sc_passed = len([r for r in sc if r.passed])
        assert "Symbol (S)" in text
        assert "Health (H)" in text
        assert "Xrefs (X)" in text
        assert f"/{primary_total} primary scenarios" in text
        assert f"{sc_passed}/{len(sc)}" in text
        assert primary_total + len(sc) == len(results)

    def test_skipped_self_consistency_noted_not_silent(
        self, script_run_cli: Any, make_result: Callable[..., Any], tmp_path: Path
    ) -> None:
        """With no S/H/X results the report explicitly flags the skipped track instead of staying silent.

        Scenario: a run with only a calls scenario; a reader must be told the track was skipped, not that it passed.
        """
        report = tmp_path / "r.md"
        script_run_cli.render_report([make_result("C1", "calls")], tmp_path, tmp_path / "i.json", report)
        text = report.read_text()
        assert "skipped (no ground truth)" in text
        assert "SKIPPED" in text


# ===========================================================================
# Stale-index guard — self-consistency suites skip on an old scan_version
# ===========================================================================


class TestIndexScanVersion:
    """Validate the index scan_version reader that gates the self-consistency track."""

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            pytest.param({"scan_version": 7, "modules": []}, 7, id="recorded-version"),
            pytest.param({"modules": []}, 0, id="missing-field-gates-suites-off"),
        ],
    )
    def test_reads_scan_version_from_index(
        self, script_run_cli: Any, tmp_path: Path, payload: dict, expected: int
    ) -> None:
        """The recorded integer scan_version is returned; an index without it yields 0, gating self-consistency off.

        Scenario: an index written by an older scan-index has no version field and must be treated as stale.
        """
        index = tmp_path / "i.json"
        index.write_text(json.dumps(payload), encoding="utf-8")
        assert script_run_cli._index_scan_version(index) == expected

    def test_unreadable_index_returns_zero(self, script_run_cli: Any, tmp_path: Path) -> None:
        """An absent index returns 0 rather than raising.

        Scenario: the index path does not exist, so the self-consistency track is gated off instead of crashing.
        """
        assert script_run_cli._index_scan_version(tmp_path / "nope.json") == 0

    def test_min_ver_constant_is_positive(self, script_run_cli: Any) -> None:
        """The self-consistency minimum version is a positive gate value.

        Scenario: a zero or negative minimum would let every index, including unversioned ones, through the guard.
        """
        assert script_run_cli._SELF_CONSISTENCY_MIN_VER >= 1


class TestTimingCensoring:
    """Failed and timed-out runs must not enter the latency statistics."""

    @pytest.mark.parametrize(
        ("outcomes", "failed", "measured", "has_median"),
        [
            pytest.param([_proc(returncode=1)] * 3, 3, 0, False, id="all-runs-fail"),
            pytest.param([_proc(), _proc(returncode=1), _proc()], 1, 2, True, id="partial-failure"),
            pytest.param([_proc()] * 4, 0, 4, True, id="all-runs-succeed"),
        ],
    )
    @mock.patch(RUN_COMMAND, autospec=True)
    def test_failed_runs_discarded_and_counted(
        self, mock_run: mock.Mock, script_run_cli: Any, outcomes: list, failed: int, measured: int, has_median: bool
    ) -> None:
        """Failing runs are dropped from the statistics and counted; the median is nan only when nothing succeeded.

        Scenario: a command that fails instantly otherwise records an excellent latency, and a partial failure must
        reduce the observation count rather than the reported speed.
        """
        mock_run.side_effect = outcomes
        stats = script_run_cli.time_command(["cmd"], n=len(outcomes))
        assert (stats.failed, stats.timed_out, stats.measured) == (failed, 0, measured)
        assert math.isnan(stats.median_ms) is not has_median

    @mock.patch(RUN_COMMAND, autospec=True)
    def test_timed_out_runs_reported_as_censored_not_observed(self, mock_run: mock.Mock, script_run_cli: Any) -> None:
        """The deadline is a lower bound on the true duration, not a measurement.

        Appending 30_000 ms as if it were an observation dragged the median toward the timeout value.
        """
        mock_run.side_effect = subprocess.TimeoutExpired(["sleep"], 30)

        stats = script_run_cli.time_command(["sleep", "60"], n=3)

        assert stats.timed_out == 3
        assert stats.measured == 0
        assert math.isnan(stats.median_ms)
        assert stats.median_ms != 30_000.0

    @mock.patch(RUN_COMMAND, autospec=True)
    def test_command_sequence_discards_a_failed_sequence(self, mock_run: mock.Mock, script_run_cli: Any) -> None:
        """The same discard rule applies to the cold-grep sequence timer.

        Scenario: two sequences of two commands that all exit non-zero leave no observation and two failures.
        """
        mock_run.return_value = _proc(returncode=2)

        stats = script_run_cli.time_commands([["a"], ["b"]], n=2)

        assert stats.failed == 2
        assert stats.measured == 0


# ===========================================================================
# Optional-rich fallback
# ===========================================================================


def _load_cli_without_rich(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Import the runner's console module afresh with ``rich`` made unimportable.

    Blocking the package in ``sys.modules`` is what makes the guarded import fail the way a host
    without rich installed makes it fail, and ``monkeypatch`` restores the entry afterwards. The
    target is ``_bench_query/output.py`` rather than the ``run-codemap-cli.py`` shim: re-executing
    the shim would re-bind its re-exports from the package modules already cached in
    ``sys.modules``, so the rich-enabled console would survive the block and the guard would never
    be exercised. The module is loaded under a throwaway name so the session-scoped
    ``script_run_cli`` fixture, which every other test shares, keeps its rich-enabled instance.

    Args:
        monkeypatch: Fixture used to block the import and restore it at teardown.

    Returns:
        The freshly executed module object.
    """
    monkeypatch.syspath_prepend(str(REPO_ROOT / "benchmarks"))
    monkeypatch.setitem(sys.modules, "rich", None)
    monkeypatch.setitem(sys.modules, "rich.console", None)
    name = "bench_query_output_without_rich"
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "benchmarks" / "_bench_query" / "output.py")
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

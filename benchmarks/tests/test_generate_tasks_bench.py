"""Tests for benchmarks/generate-tasks-bench.py.

Covers pure validation logic (validator functions, JSON parsing, ground-truth comparison, mode selection, task-type
routing) with mocked scan-query subprocess calls.  No real scan-query binary or codemap index is needed.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


class TestTaskContractValidation:
    """Validate executable query contracts declared by the active task suite."""

    def test_every_execution_task_has_a_supported_structured_expected_query(self, script_gen_bench: Any) -> None:
        """Every benchmark coordinate has a machine-checkable Codemap query contract.

        Prevents B/C preflight from discovering malformed or absent query metadata only after a paid benchmark has
        started.
        """
        data = json.loads(script_gen_bench.TASKS_FILE.read_text(encoding="utf-8"))
        tasks = [task for task in data["tasks"] if task["type"] != "real_issue"]

        assert len(tasks) == 55
        assert script_gen_bench._expected_query_contract_errors(tasks) == []

    def test_expected_query_commands_are_supported_by_scan_query(self, script_gen_bench: Any) -> None:
        """The benchmark's declared command allowlist stays within the live CLI surface.

        The launcher runs through ``sys.executable`` rather than by direct execution, which is how the script itself and
        the sibling lanes start it — so this contract is checked on Windows too, where an extension-less shebang script
        cannot be launched directly.
        """
        launcher = script_gen_bench.git_toplevel() / "plugins" / "codemap-py" / "bin" / "scan-query"
        result = subprocess.run([sys.executable, str(launcher), "--help"], capture_output=True, text=True, check=True)
        match = re.search(r"\{([^}]+)\}", result.stdout)

        assert match is not None
        supported = set(match.group(1).split(","))
        assert script_gen_bench._SUPPORTED_EXPECTED_QUERY_COMMANDS <= supported

    @pytest.mark.parametrize(
        ("task", "expected_error"),
        [
            pytest.param(
                {"id": "Q-01", "type": "query"},
                "Q-01: expected_queries must be a non-empty list",
                id="missing-query-list",
            ),
            pytest.param(
                {"id": "Q-02", "type": "query", "expected_queries": ["symbol module::name"]},
                "Q-02: expected query 1 must be an object",
                id="legacy-string-entry",
            ),
            pytest.param(
                {"id": "Q-03", "type": "query", "expected_queries": [{"cmd": "unknown", "args": []}]},
                "Q-03: expected query 1 has unsupported cmd 'unknown'",
                id="unsupported-command",
            ),
            pytest.param(
                {
                    "id": "Q-04",
                    "type": "query",
                    "expected_queries": [{"cmd": "symbol", "args": ["pkg.name"]}],
                    "expected_query_policy": "every_query",
                },
                "Q-04: expected_query_policy must be one of ['all_required', 'any_match']",
                id="unsupported-query-policy",
            ),
            pytest.param(
                {
                    "id": "Q-05",
                    "type": "query",
                    "expected_queries": [{"cmd": "symbol", "args": ["pkg.name"]}],
                    "expected_query_policy": ["all_required"],
                },
                "Q-05: expected_query_policy must be one of ['all_required', 'any_match']",
                id="non-string-query-policy",
            ),
        ],
    )
    def test_expected_query_contract_rejects_unrepresentable_execution_task(
        self, script_gen_bench: Any, task: dict[str, Any], expected_error: str
    ) -> None:
        """Malformed execution metadata fails before any benchmark coordinate runs."""
        assert script_gen_bench._expected_query_contract_errors([task]) == [expected_error]

    @pytest.mark.parametrize("task_id", ("DI-01", "DI-02", "DI-03", "DI-04", "DI-05", "DI-06"))
    def test_every_diff_impact_task_requires_both_direct_query_components(
        self, script_gen_bench: Any, task_id: str
    ) -> None:
        """Diff-impact compliance must prove callers and direct test importers.

        A nearby module or a generic symbol search can look useful while failing to establish the requested blast-radius
        evidence.
        """
        tasks = json.loads(script_gen_bench.TASKS_FILE.read_text(encoding="utf-8"))["tasks"]
        task = next(task for task in tasks if task["id"] == task_id)
        assert task["expected_query_policy"] == "all_required"
        assert {
            "cmd": "fn-rdeps",
            "args": [task["primary_fn"], "--exclude-tests"],
        } in task["expected_queries"]
        assert {"cmd": "rdeps", "args": [task["primary_module"]]} in task["expected_queries"]


# ===========================================================================
# find_codemap_bin
# ===========================================================================


class TestFindCodemapBin:
    """Resolve binary by PATH lookup then plugin-dir fallback."""

    def test_returns_none_when_not_found(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Return None when binary absent from PATH and plugin root.

        Scenario: clean environment; binary not installed.
        """
        with patch("shutil.which", return_value=None):
            result = script_gen_bench.find_codemap_bin("scan-query", tmp_path)
        assert result is None

    def test_returns_path_when_on_path(self, script_gen_bench: Any) -> None:
        """Return Path object when binary is found via shutil.which.

        Scenario: binary installed globally; PATH lookup succeeds.
        """
        with patch("shutil.which", return_value="/usr/local/bin/scan-query"):
            result = script_gen_bench.find_codemap_bin("scan-query")
        assert result == Path("/usr/local/bin/scan-query")

    def test_falls_back_to_plugin_dir(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Falls back to plugins/codemap-py/bin/<name> when PATH lookup fails.

        Scenario: binary not on PATH but present in plugin directory structure.
        """
        bin_dir = tmp_path / "plugins" / "codemap-py" / "bin"
        bin_dir.mkdir(parents=True)
        binary = bin_dir / "scan-query"
        binary.write_text("#!/bin/sh")

        with patch("shutil.which", return_value=None):
            result = script_gen_bench.find_codemap_bin("scan-query", tmp_path)
        assert result == binary

    def test_plugin_fallback_returns_none_when_file_absent(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Return None when plugin dir exists but binary file is absent.

        Scenario: partial plugin install; bin/ dir created but binary not
        present.
        """
        bin_dir = tmp_path / "plugins" / "codemap-py" / "bin"
        bin_dir.mkdir(parents=True)
        # binary file deliberately NOT created

        with patch("shutil.which", return_value=None):
            result = script_gen_bench.find_codemap_bin("scan-query", tmp_path)
        assert result is None

    def test_no_plugin_root_returns_none_without_which(self, script_gen_bench: Any) -> None:
        """Return None when plugin_root omitted and PATH lookup fails.

        Scenario: no plugin_root provided; which() returns None.
        """
        with patch("shutil.which", return_value=None):
            result = script_gen_bench.find_codemap_bin("scan-query")
        assert result is None

    def test_legacy_codemap_path_never_selected(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """The pre-rename plugins/codemap/bin/ folder is never resolved.

        Scenario: only the retired ``codemap`` identity holds the binary. The
        resolver targets ``codemap-py`` exclusively, so a stale checkout must
        yield None rather than a false-green fixture pointing at the old tree.
        """
        legacy = tmp_path / "plugins" / "codemap" / "bin"
        legacy.mkdir(parents=True)
        (legacy / "scan-query").write_text("#!/bin/sh")

        with patch("shutil.which", return_value=None):
            result = script_gen_bench.find_codemap_bin("scan-query", tmp_path)
        assert result is None


# ===========================================================================
# resolve_index_path
# ===========================================================================


class TestResolveIndexPath:
    """Return explicit arg as-is; otherwise discover index file."""

    def test_returns_explicit_arg_unchanged(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Return the explicit ``--index-path`` argument verbatim as a Path.

        Scenario: user passes ``--index-path``; resolution must not alter it.
        """
        explicit = "/some/custom/index.json"
        result = script_gen_bench.resolve_index_path(explicit, tmp_path)
        assert result == Path(explicit)

    def test_finds_index_in_cache_codemap(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Discovers index JSON at <repo>/.cache/codemap/<name>.json.

        Scenario: default layout; index built into .cache/codemap/.
        """
        repo_name = tmp_path.name
        cache_dir = tmp_path / ".cache" / "codemap"
        cache_dir.mkdir(parents=True)
        index_file = cache_dir / f"{repo_name}.json"
        index_file.write_text("{}")

        result = script_gen_bench.resolve_index_path(None, tmp_path)
        assert result == index_file

    def test_finds_index_in_cache_scan(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Falls back to .cache/scan/ when .cache/codemap/<name>.json absent.

        Scenario: older layout uses .cache/scan/ instead of .cache/codemap/.
        """
        repo_name = tmp_path.name
        cache_dir = tmp_path / ".cache" / "scan"
        cache_dir.mkdir(parents=True)
        index_file = cache_dir / f"{repo_name}.json"
        index_file.write_text("{}")

        result = script_gen_bench.resolve_index_path(None, tmp_path)
        assert result == index_file

    def test_strips_master_suffix_from_repo_name(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Strips -master suffix when looking up index file.

        Scenario: repo cloned as pytorch-lightning-master; index stored as
        pytorch-lightning.json.
        """
        # Use a temp dir whose name ends in -master by creating a subdir
        repo_dir = tmp_path / "myrepo-master"
        repo_dir.mkdir()
        cache_dir = repo_dir / ".cache" / "codemap"
        cache_dir.mkdir(parents=True)
        # file uses stem without -master suffix
        (cache_dir / "myrepo.json").write_text("{}")

        result = script_gen_bench.resolve_index_path(None, repo_dir)
        assert result == cache_dir / "myrepo.json"

    def test_falls_back_to_first_json_glob(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Falls back to first JSON found via glob when name-based lookup fails.

        Scenario: index named differently from repo; glob returns first match.
        """
        cache_dir = tmp_path / ".cache" / "codemap"
        cache_dir.mkdir(parents=True)
        index_file = cache_dir / "other-name.json"
        index_file.write_text("{}")

        result = script_gen_bench.resolve_index_path(None, tmp_path)
        assert result == index_file

    def test_returns_fallback_path_when_no_index_found(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Return a constructed path (that may not exist) when no index found.

        Scenario: empty repo; no .cache/ directory; returns computed fallback.
        """
        result = script_gen_bench.resolve_index_path(None, tmp_path)
        # Must be a Path object under .cache/codemap/
        assert isinstance(result, Path)
        assert ".cache" in result.parts


# ===========================================================================
# run_scan_query
# ===========================================================================


class TestRunScanQuery:
    """Subprocess wrapper returns parsed dict or None on failure."""

    def _make_proc(self, returncode: int, stdout: str, stderr: str = "") -> MagicMock:
        """Build a MagicMock simulating subprocess.CompletedProcess."""
        proc = MagicMock()
        proc.returncode = returncode
        proc.stdout = stdout
        proc.stderr = stderr
        return proc

    def test_returns_dict_on_success(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Return parsed JSON dict when subprocess exits 0 with valid JSON.

        Scenario: happy path; scan-query returns well-formed JSON.
        """
        payload = {"symbols": [{"qualified_name": "Foo.bar"}]}
        proc = self._make_proc(0, json.dumps(payload))
        sq = tmp_path / "scan-query"
        index = tmp_path / "index.json"

        with patch("subprocess.run", return_value=proc):
            result = script_gen_bench.run_scan_query(sq, ["symbol", "Foo.bar"], index, tmp_path)

        assert result == payload

    def test_returns_none_on_nonzero_exit(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Return None when subprocess exits non-zero.

        Scenario: scan-query fails (e.g., index not found); caller gets None.
        """
        proc = self._make_proc(1, "")
        sq = tmp_path / "scan-query"
        index = tmp_path / "index.json"

        with patch("subprocess.run", return_value=proc):
            result = script_gen_bench.run_scan_query(sq, ["symbol", "X"], index, tmp_path)

        assert result is None

    def test_returns_none_on_json_decode_error(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Return None when subprocess output is not valid JSON.

        Scenario: scan-query prints human-readable error; JSON parse fails.
        """
        proc = self._make_proc(0, "not json output")
        sq = tmp_path / "scan-query"
        index = tmp_path / "index.json"

        with patch("subprocess.run", return_value=proc):
            result = script_gen_bench.run_scan_query(sq, ["symbol", "X"], index, tmp_path)

        assert result is None

    def test_returns_none_on_timeout(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Return None when subprocess times out.

        Scenario: scan-query hangs; TimeoutExpired is caught and swallowed.
        """
        sq = tmp_path / "scan-query"
        index = tmp_path / "index.json"

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="sq", timeout=30)):
            result = script_gen_bench.run_scan_query(sq, ["symbol", "X"], index, tmp_path)

        assert result is None

    def test_returns_none_on_os_error(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Return None when the binary cannot be executed (OSError).

        Scenario: scan-query path does not exist or permissions denied.
        """
        sq = tmp_path / "scan-query"
        index = tmp_path / "index.json"

        with patch("subprocess.run", side_effect=OSError("no such file")):
            result = script_gen_bench.run_scan_query(sq, ["symbol", "X"], index, tmp_path)

        assert result is None


# ===========================================================================
# _validate_symbol
# ===========================================================================


class TestValidateSymbol:
    """Symbol_extraction validator compares scan-query output to gt."""

    def _task(self, module: str, qualified_name: str, start: int, end: int) -> dict:
        """Build a minimal symbol_extraction task dict."""
        return {
            "type": "symbol_extraction",
            "ground_truth": {
                "module": module,
                "qualified_name": qualified_name,
                "start_line": start,
                "end_line": end,
            },
        }

    def _sq_response(self, symbols: list[dict]) -> dict:
        """Build a minimal scan-query symbol response."""
        return {"symbols": symbols}

    def test_passes_when_all_fields_match(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Return (True, live_gt, '') when scan-query result matches ground truth exactly.

        Scenario: symbol found with correct module, qname, start_line, end_line.
        """
        task = self._task("pkg.mod", "MyClass.method", 10, 20)
        payload = self._sq_response(
            [{"module": "pkg.mod", "qualified_name": "MyClass.method", "start_line": 10, "end_line": 20}]
        )

        with patch.object(script_gen_bench, "run_scan_query", return_value=payload):
            ok, live_gt, reason = script_gen_bench._validate_symbol(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert ok is True
        assert reason == ""
        assert live_gt["start_line"] == 10
        assert live_gt["end_line"] == 20

    def test_fails_when_start_line_differs(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Return (False, live_gt, reason) when start_line does not match ground truth.

        Scenario: index rebuilt after refactor shifted function up one line.
        """
        task = self._task("pkg.mod", "MyClass.method", 10, 20)
        payload = self._sq_response(
            [{"module": "pkg.mod", "qualified_name": "MyClass.method", "start_line": 99, "end_line": 20}]
        )

        with patch.object(script_gen_bench, "run_scan_query", return_value=payload):
            ok, live_gt, reason = script_gen_bench._validate_symbol(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert ok is False
        assert "start_line" in reason

    def test_fails_when_symbol_not_found(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Return (False, None, reason) when symbol absent from scan-query results.

        Scenario: symbol renamed; query returns empty symbols list.
        """
        task = self._task("pkg.mod", "MissingFn", 1, 5)
        payload = self._sq_response([])

        with patch.object(script_gen_bench, "run_scan_query", return_value=payload):
            ok, live_gt, reason = script_gen_bench._validate_symbol(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert ok is False
        assert live_gt is None
        assert "not found" in reason

    def test_returns_none_on_scan_query_failure(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Report validation failure when the structural query fails.

        Scenario: binary error; subprocess call failed.
        """
        task = self._task("pkg.mod", "MyClass.method", 1, 5)

        with patch.object(script_gen_bench, "run_scan_query", return_value=None):
            ok, live_gt, reason = script_gen_bench._validate_symbol(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert ok is False
        assert live_gt is None

    def test_widens_to_qname_match_when_module_differs(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Falls back to matching on qualified_name alone when module differs.

        Scenario: symbol moved to different module but qname preserved; doc
        says 'Widen to any symbol with the right qname'.
        """
        task = self._task("pkg.old_mod", "MyClass.method", 10, 20)
        # Returned symbol has same qname but different module
        payload = self._sq_response(
            [{"module": "pkg.new_mod", "qualified_name": "MyClass.method", "start_line": 10, "end_line": 20}]
        )

        with patch.object(script_gen_bench, "run_scan_query", return_value=payload):
            ok, live_gt, reason = script_gen_bench._validate_symbol(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        # Symbol IS found via widened match; line numbers match so ok=True
        assert live_gt is not None  # widened match succeeded

    @pytest.mark.parametrize(
        "gt_field,live_value,expected_problem",
        [
            pytest.param("start_line", 99, "start_line", id="start_line"),
            pytest.param("end_line", 99, "end_line", id="end_line"),
            # module mismatch: symbol is found via widened qname match but module field differs
            pytest.param("module", "wrong.mod", "module", id="module"),
        ],
    )
    def test_reports_specific_field_mismatch(
        self, script_gen_bench: Any, tmp_path: Path, gt_field: str, live_value: Any, expected_problem: str
    ) -> None:
        """Failure reason names the specific field that differs from ground truth.

        Scenario: each ground-truth field individually diverges; the symbol IS
        found (qname matches) but the stored field value differs from live; the
        reason message must identify the offending field by name.
        Note: qualified_name cannot be tested this way — if qname differs, the
        symbol is not found at all rather than producing a field-mismatch message.
        """
        base = {
            "module": "pkg.mod",
            "qualified_name": "Cls.fn",
            "start_line": 10,
            "end_line": 20,
        }
        task = {"type": "symbol_extraction", "ground_truth": dict(base)}

        live = dict(base)
        live[gt_field] = live_value

        payload = self._sq_response([live])

        with patch.object(script_gen_bench, "run_scan_query", return_value=payload):
            ok, _, reason = script_gen_bench._validate_symbol(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert ok is False
        assert expected_problem in reason


# ===========================================================================
# _validate_fn
# ===========================================================================


class TestValidateFn:
    """Fn_call_graph validator checks caller counts and sets."""

    def _task(
        self,
        primary_fn: str,
        callers: list[str],
        unique_count: int,
        raw_count: int,
        exclude_tests: bool = False,
    ) -> dict:
        """Build a minimal fn_call_graph task dict."""
        return {
            "type": "fn_call_graph",
            "primary_fn": primary_fn,
            "ground_truth": {
                "fn_callers": callers,
                "unique_caller_count": unique_count,
                "raw_caller_count": raw_count,
                "exclude_tests": exclude_tests,
            },
        }

    def _sq_response(self, callers: list[str], count: int | None = None) -> dict:
        """Build a minimal fn-rdeps scan-query response."""
        called_by = [{"caller": c} for c in callers]
        result: dict = {"called_by": called_by}
        if count is not None:
            result["count"] = count
        return result

    def _write_callers(self, repo: Path, callers: list[str], target: str) -> None:
        """Write Python files so the AST oracle discovers exactly ``callers`` of ``target``.

        Each caller string is ``<module>::<scope>`` where scope is ``func`` or ``Class.method``.
        A file at ``<module-as-path>.py`` is created whose scope body calls ``target()`` — the
        AST oracle then attributes that call to ``<module>::<scope>``.
        """
        for caller in callers:
            module, scope = caller.split("::")
            fpath = repo / (module.replace(".", "/") + ".py")
            fpath.parent.mkdir(parents=True, exist_ok=True)
            if "." in scope:
                cls, meth = scope.split(".", 1)
                src = f"class {cls}:\n    def {meth}(self):\n        {target}()\n"
            else:
                src = f"def {scope}():\n    {target}()\n"
            fpath.write_text(src)

    def test_passes_when_callers_match_exactly(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Return (True, live_gt, '') when the AST oracle's callers match ground truth.

        Scenario: unchanged codebase; the independent AST oracle (now authoritative) agrees
        with the stored caller set.
        """
        callers = ["mod.a::fn_a", "mod.b::fn_b"]
        task = self._task("mod::target", callers, 2, 2)
        payload = self._sq_response(callers, 2)
        self._write_callers(tmp_path, callers, "target")

        with patch.object(script_gen_bench, "run_scan_query", return_value=payload):
            ok, live_gt, reason = script_gen_bench._validate_fn(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert ok is True
        assert reason == ""

    def test_fails_on_extra_caller(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Return failure when the AST oracle finds a caller not in ground truth.

        Scenario: new function added that calls target; GT is stale relative to the oracle.
        """
        gt_callers = ["mod.a::fn_a"]
        task = self._task("mod::target", gt_callers, 1, 1)
        oracle_callers = ["mod.a::fn_a", "mod.b::fn_b"]  # extra caller present in source
        payload = self._sq_response(oracle_callers, 2)
        self._write_callers(tmp_path, oracle_callers, "target")

        with patch.object(script_gen_bench, "run_scan_query", return_value=payload):
            ok, _, reason = script_gen_bench._validate_fn(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert ok is False
        assert "extra" in reason

    def test_fails_on_missing_caller(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Return failure when a ground-truth caller is absent from the AST oracle.

        Scenario: caller refactored away; the oracle no longer sees it in source.
        """
        gt_callers = ["mod.a::fn_a", "mod.b::fn_b"]
        task = self._task("mod::target", gt_callers, 2, 2)
        oracle_callers = ["mod.a::fn_a"]  # missing mod.b::fn_b in source
        payload = self._sq_response(oracle_callers, 1)
        self._write_callers(tmp_path, oracle_callers, "target")

        with patch.object(script_gen_bench, "run_scan_query", return_value=payload):
            ok, _, reason = script_gen_bench._validate_fn(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert ok is False
        assert "missing" in reason

    def test_fails_on_count_mismatch(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Return failure when unique_caller_count differs from ground truth.

        Scenario: count field diverges even if set comparison matches.
        """
        callers = ["mod.a::fn_a"]
        task = self._task("mod::target", callers, 99, 1)  # wrong expected unique_count
        payload = self._sq_response(callers, 1)

        with patch.object(script_gen_bench, "run_scan_query", return_value=payload):
            ok, _, reason = script_gen_bench._validate_fn(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert ok is False
        assert "unique_caller_count" in reason

    def test_returns_none_when_scan_query_fails(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Return (False, None, reason) when scan-query call fails.

        Scenario: binary not available; subprocess returns None.
        """
        task = self._task("mod::fn", [], 0, 0)

        with patch.object(script_gen_bench, "run_scan_query", return_value=None):
            ok, live_gt, reason = script_gen_bench._validate_fn(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert ok is False
        assert live_gt is None

    def test_excludes_tests_flag_added_to_args(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Appends the ``--exclude-tests`` flag when ground_truth.exclude_tests is True.

        Scenario: task configured to exclude test-file callers; flag must be
        forwarded to scan-query.
        """
        task = self._task("mod::fn", [], 0, 0, exclude_tests=True)

        captured_args: list[list] = []

        def _fake_sq(sq: Any, args: list, index: Any, repo: Any) -> dict:
            """Record query arguments and return an empty caller result."""
            captured_args.append(args)
            return {"called_by": [], "count": 0}

        with patch.object(script_gen_bench, "run_scan_query", side_effect=_fake_sq):
            script_gen_bench._validate_fn(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert captured_args, "run_scan_query must be called at least once"
        assert "--exclude-tests" in captured_args[0]

    def test_deduplicates_scan_callers_in_diagnostic(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Deduplicates repeated scan-query callers in the diagnostic scan_caller_count.

        Scenario: scan-query returns the same caller twice (raw > unique). The authoritative
        unique_caller_count comes from the AST oracle; the scan_caller_count diagnostic must
        still deduplicate the tool's output.
        """
        callers = ["mod.a::fn_a", "mod.a::fn_a"]  # duplicate scan output
        task = self._task("mod::target", ["mod.a::fn_a"], 1, 2)
        payload = self._sq_response(callers, 2)
        self._write_callers(tmp_path, ["mod.a::fn_a"], "target")

        with patch.object(script_gen_bench, "run_scan_query", return_value=payload):
            ok, live_gt, _ = script_gen_bench._validate_fn(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert live_gt["unique_caller_count"] == 1  # AST oracle (authoritative)
        assert live_gt["scan_caller_count"] == 1  # scan output deduplicated


# ===========================================================================
# _extract_rv_value
# ===========================================================================


class TestExtractRvValue:
    """Correct value extracted per (cmd, match_type) combination."""

    @pytest.mark.parametrize(
        "cmd,data,expected",
        [
            pytest.param("rdeps", {"imported_by": ["a", "b", "c"]}, 3, id="rdeps-imported_by-a-b-c"),
            pytest.param("rdeps", {"imported_by": []}, 0, id="rdeps-imported_by"),
            pytest.param("fn-rdeps", {"count": 7, "called_by": []}, 7, id="fn-rdeps-count-7-called_by"),
            pytest.param("fn-rdeps", {"called_by": [1, 2]}, 2, id="fn-rdeps-called_by-1-2"),  # fallback len(called_by)
            pytest.param("undocumented", {"total": 12}, 12, id="undocumented"),
            pytest.param("uncovered", {"total": 5}, 5, id="uncovered"),
            pytest.param("unknown-cmd", {}, 0, id="unknown-cmd"),
        ],
    )
    def test_integer_extract(self, script_gen_bench: Any, cmd: str, data: dict, expected: int) -> None:
        """Return integer count from scan-query output for integer_extract match type.

        Scenario: each supported subcommand returns count from different keys;
        unknown cmd falls through to 0.
        """
        result = script_gen_bench._extract_rv_value(cmd, data, "integer_extract")
        assert result == expected

    @pytest.mark.parametrize(
        "cmd,data,count_hint,expected",
        [
            pytest.param(
                "undocumented",
                {"undocumented": [{"qualified_name": "A"}, {"qualified_name": "B"}]},
                0,
                ["A", "B"],
                id="undocumented-undocumented-qualified_name-a-qualified_name-b",
            ),
            pytest.param(
                "undocumented",
                {"undocumented": [{"qualified_name": "A"}, {"qualified_name": "B"}, {"qualified_name": "C"}]},
                2,
                ["A", "B"],  # truncated to count_hint
                id="undocumented-undocumented-qualified_name-a-qualified_name-b-qualified_name-c",
            ),
            pytest.param("uncovered", {"uncovered": [{"qualified_name": "X"}]}, 0, ["X"], id="uncovered"),
            pytest.param(
                "unknown-cmd",
                {"anything": []},
                0,
                [],  # unknown cmd returns empty list for symbol_name_set
                id="unknown-cmd",
            ),
        ],
    )
    def test_symbol_name_set(
        self, script_gen_bench: Any, cmd: str, data: dict, count_hint: int, expected: list
    ) -> None:
        """Return qualified_name list for symbol_name_set match type.

        Scenario: undocumented and uncovered commands return symbol names;
        count_hint truncates the list; unknown cmd returns empty.
        """
        result = script_gen_bench._extract_rv_value(cmd, data, "symbol_name_set", count_hint=count_hint)
        assert result == expected

    def test_entry_falls_back_to_name_key(self, script_gen_bench: Any) -> None:
        """Uses 'name' key when 'qualified_name' absent from entry dict.

        Scenario: some scan-query versions use 'name' instead of
        'qualified_name'; fallback must be used.
        """
        data = {"undocumented": [{"name": "fallback_fn"}]}
        result = script_gen_bench._extract_rv_value("undocumented", data, "symbol_name_set")
        assert result == ["fallback_fn"]


# ===========================================================================
# _validate_rv
# ===========================================================================


class TestValidateRv:
    """Review_assistance validator checks sub-question ground truth."""

    def _task(self, sub_questions: list[dict], expected_queries: list[dict] | None = None) -> dict:
        """Build a minimal review_assistance task dict."""
        # Use explicit None sentinel so callers can pass [] to test the empty-query path.
        if expected_queries is None:
            expected_queries = [{"cmd": "fn-rdeps", "args": ["mod::fn"]}]
        return {
            "type": "review_assistance",
            "sub_questions": sub_questions,
            "expected_queries": expected_queries,
        }

    def test_passes_when_integer_count_matches(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Return (True, live_gt, '') when integer sub-question count matches GT.

        Scenario: fn-rdeps count matches stored expected count.
        """
        task = self._task([{"id": "sq1", "match": "integer_extract", "ground_truth": {"count": 3}}])
        payload = {"count": 3, "called_by": []}

        with (
            patch.object(script_gen_bench, "_rv_ast_value", return_value=(3, True, None)),
            patch.object(script_gen_bench, "run_scan_query", return_value=payload),
        ):
            ok, live_gt, reason = script_gen_bench._validate_rv(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert ok is True
        assert reason == ""

    def test_preserves_index_path_after_enumerating_review_subquestions(
        self, script_gen_bench: Any, tmp_path: Path
    ) -> None:
        """The scan-query call receives its `Path`, never a sub-question ordinal.

        Prevents the deterministic full-suite crash where `_validate_rv` shadows its `index` parameter with
        `enumerate(..., start=1)`.
        """
        index_path = tmp_path / "idx.json"
        task = self._task([{"id": "sq1", "match": "integer_extract", "ground_truth": {"count": 3}}])
        query = MagicMock(return_value={"count": 3, "called_by": []})

        with (
            patch.object(script_gen_bench, "_rv_ast_value", return_value=(3, True, None)),
            patch.object(script_gen_bench, "run_scan_query", query),
        ):
            ok, _live_gt, reason = script_gen_bench._validate_rv(task, MagicMock(), index_path, tmp_path)

        assert ok is True
        assert reason == ""
        assert query.call_args.args[2] == index_path

    def test_fails_when_integer_count_differs(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Return failure reason naming sub-question id when count differs.

        Scenario: refactor changed call graph; count in GT now stale.
        """
        task = self._task([{"id": "sq1", "match": "integer_extract", "ground_truth": {"count": 10}}])
        payload = {"count": 7, "called_by": []}

        with (
            patch.object(script_gen_bench, "_rv_ast_value", return_value=(7, True, None)),
            patch.object(script_gen_bench, "run_scan_query", return_value=payload),
        ):
            ok, _, reason = script_gen_bench._validate_rv(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert ok is False
        assert "sq1" in reason

    def test_passes_when_symbol_set_matches(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Return (True, ...) when symbol_name_set sub-question matches GT.

        Scenario: undocumented symbols match stored ground truth exactly.
        """
        task = self._task(
            [{"id": "sq2", "match": "symbol_name_set", "ground_truth": {"symbols": ["A", "B"]}}],
            expected_queries=[{"cmd": "undocumented", "args": []}],
        )
        payload = {"undocumented": [{"qualified_name": "A"}, {"qualified_name": "B"}]}

        with (
            patch.object(script_gen_bench, "_rv_ast_value", return_value=(["A", "B"], True, None)),
            patch.object(script_gen_bench, "run_scan_query", return_value=payload),
        ):
            ok, _, reason = script_gen_bench._validate_rv(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert ok is True

    def test_fails_when_symbol_set_has_missing(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Return failure describing missing symbols from expected set.

        Scenario: GT expects ['A','B'] but live returns only ['A'].
        """
        task = self._task(
            [{"id": "sq2", "match": "symbol_name_set", "ground_truth": {"symbols": ["A", "B"]}}],
            expected_queries=[{"cmd": "undocumented", "args": []}],
        )
        payload = {"undocumented": [{"qualified_name": "A"}]}

        with (
            patch.object(script_gen_bench, "_rv_ast_value", return_value=(["A"], True, None)),
            patch.object(script_gen_bench, "run_scan_query", return_value=payload),
        ):
            ok, _, reason = script_gen_bench._validate_rv(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert ok is False
        assert "missing" in reason

    def test_fails_when_expected_queries_empty(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Return (False, None, reason) when expected_queries list is empty.

        Scenario: malformed task; no query defined.
        """
        task = self._task([], expected_queries=[])

        ok, live_gt, reason = script_gen_bench._validate_rv(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert ok is False
        assert live_gt is None
        assert "expected_queries" in reason

    @pytest.mark.parametrize(
        "sub_questions",
        [
            pytest.param([{"id": "sq1", "match": "unknown", "ground_truth": {"count": 1}}], id="unsupported-match"),
            pytest.param(
                [
                    {"id": "sq1", "match": "integer_extract", "ground_truth": {"count": 1}},
                    {"id": "sq2", "match": "integer_extract", "ground_truth": {"count": 2}},
                ],
                id="ambiguous-multiple-counts",
            ),
            pytest.param(["not-an-object"], id="non-object-subquestion"),
        ],
    )
    def test_fails_closed_for_invalid_or_ambiguous_review_subquestions(
        self, script_gen_bench: Any, tmp_path: Path, sub_questions: list[object]
    ) -> None:
        """Generation refuses evaluator contracts that cannot produce one unambiguous answer mapping."""
        task = self._task(sub_questions)  # type: ignore[arg-type]

        ok, live_gt, reason = script_gen_bench._validate_rv(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert ok is False
        assert live_gt is None
        assert "review" in reason

    def test_passes_when_diagnostic_scan_query_fails(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Keeps independent validation usable when diagnostic scan-query fails.

        Scenario: AST derives the expected caller count while the queried tool is
        unavailable. The tool cannot become a ground-truth dependency.
        """
        task = self._task([{"id": "sq1", "match": "integer_extract", "ground_truth": {"count": 1}}])

        with (
            patch.object(script_gen_bench, "_rv_ast_value", return_value=(1, True, None)),
            patch.object(script_gen_bench, "run_scan_query", return_value=None),
        ):
            ok, live_gt, reason = script_gen_bench._validate_rv(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert ok is True
        assert live_gt == {"sq1": {"count": 1}}
        assert reason == ""

    def test_fails_closed_when_ast_source_is_unavailable(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Rejects tool-derived fallback when an independent RV source is absent."""
        task = self._task([{"id": "sq1", "match": "integer_extract", "ground_truth": {"count": 3}}])
        payload = {"count": 3, "called_by": []}

        with (
            patch.object(script_gen_bench, "_rv_ast_value", return_value=(None, False, None)),
            patch.object(script_gen_bench, "run_scan_query", return_value=payload),
        ):
            ok, live_gt, reason = script_gen_bench._validate_rv(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert ok is False
        assert live_gt is None
        assert "independent AST source is unavailable" in reason


# ===========================================================================
# _validate_oss
# ===========================================================================


class TestValidateOss:
    """Code_quality validator routes by gt['check'] field."""

    def _task_undocumented(self, count: int, syms: list[str]) -> dict:
        """Build a code_quality task with check='undocumented'."""
        return {
            "type": "code_quality",
            "expected_queries": [{"cmd": "undocumented", "args": []}],
            "ground_truth": {
                "check": "undocumented",
                "undocumented_count": count,
                "undocumented_symbols": syms,
            },
        }

    def _task_uncovered(self, count: int, syms: list[str]) -> dict:
        """Build a code_quality task with check='uncovered'."""
        return {
            "type": "code_quality",
            "expected_queries": [{"cmd": "uncovered", "args": []}],
            "ground_truth": {
                "check": "uncovered",
                "uncovered_count": count,
                "uncovered_symbols": syms,
            },
        }

    def _task_coupled(self, top_module: str, dep_count: int, internal: int) -> dict:
        """Build a code_quality task with check='coupled'."""
        return {
            "type": "code_quality",
            "expected_queries": [{"cmd": "coupled", "args": []}],
            "ground_truth": {
                "check": "coupled",
                "top_module": top_module,
                "top_dep_count": dep_count,
                "top_internal_dep_count": internal,
            },
        }

    def _task_coupled_ranking(self, ranking: list[dict[str, int | str]]) -> dict:
        """Build a CQ-03-style coupled task with its complete ordered oracle.

        ``coupled`` ranks by ``internal_dep_count``.  The benchmark prompt asks for all five displayed module names and
        their ``dep_count`` values, so ground truth must retain every row rather than treating rank one as a proxy for
        the requested ranking.
        """
        first = ranking[0]
        return {
            "type": "code_quality",
            "expected_queries": [{"cmd": "coupled", "args": ["--top", str(len(ranking))]}],
            "ground_truth": {
                "check": "coupled",
                "top_module": first["name"],
                "top_dep_count": first["dep_count"],
                "top_internal_dep_count": first["internal_dep_count"],
                "top_modules": ranking,
            },
        }

    def _coupled_ranking(self) -> list[dict[str, int | str]]:
        """Return a five-row oracle with different ranking and display metrics."""
        return [
            {"name": "pkg.alpha", "dep_count": 9, "internal_dep_count": 5},
            {"name": "pkg.bravo", "dep_count": 8, "internal_dep_count": 4},
            {"name": "pkg.charlie", "dep_count": 7, "internal_dep_count": 3},
            {"name": "pkg.delta", "dep_count": 6, "internal_dep_count": 2},
            {"name": "pkg.echo", "dep_count": 5, "internal_dep_count": 1},
        ]

    def _task_xrefs(self, broken_count: int, targets: list[dict]) -> dict:
        """Build a code_quality task with check='xrefs_broken'."""
        return {
            "type": "code_quality",
            "expected_queries": [{"cmd": "xrefs", "args": []}],
            "ground_truth": {
                "check": "xrefs_broken",
                "broken_count": broken_count,
                "broken_targets": targets,
            },
        }

    def _write_xref_source(self, repo: Path, filename: str, source: str) -> None:
        """Write *source* to ``repo/filename`` for the xrefs AST oracle (:func:`_xrefs_broken_via_ast`) to scan."""
        (repo / filename).write_text(source)

    def _write_undoc(self, repo: Path, names: list[str]) -> None:
        """Write a module whose public classes ``names`` all lack docstrings.

        The AST undocumented oracle (now authoritative) then reports exactly ``names`` as the undocumented public symbol
        set.
        """
        body = "\n\n".join(f"class {n}:\n    pass" for n in names) + "\n"
        (repo / "m.py").write_text(body)

    def test_undocumented_passes_on_match(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Return (True, ...) when the AST oracle's undocumented set matches GT.

        Scenario: check='undocumented'; the authoritative AST oracle agrees with stored GT.
        """
        task = self._task_undocumented(2, ["A", "B"])
        payload = {"total": 2, "undocumented": [{"qualified_name": "A"}, {"qualified_name": "B"}]}
        self._write_undoc(tmp_path, ["A", "B"])

        with patch.object(script_gen_bench, "run_scan_query", return_value=payload):
            ok, _, reason = script_gen_bench._validate_oss(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert ok is True
        assert reason == ""

    def test_undocumented_fails_on_count_mismatch(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Return failure when the AST oracle's undocumented count differs from GT.

        Scenario: symbols documented after GT was captured; the oracle now sees fewer.
        """
        task = self._task_undocumented(5, ["A", "B", "C", "D", "E"])
        payload = {
            "total": 3,
            "undocumented": [{"qualified_name": "A"}, {"qualified_name": "B"}, {"qualified_name": "C"}],
        }
        self._write_undoc(tmp_path, ["A", "B", "C"])

        with patch.object(script_gen_bench, "run_scan_query", return_value=payload):
            ok, _, reason = script_gen_bench._validate_oss(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert ok is False
        assert "undocumented_count" in reason

    def _write_uncovered(self, repo: Path, uncovered: list[str], covered: list[str]) -> None:
        """Write a source module of public functions; a test file references only ``covered``.

        The AST uncovered oracle (now authoritative) then reports exactly ``uncovered`` — the public functions no test
        module calls.
        """
        body = "\n\n".join(f"def {n}():\n    pass" for n in [*uncovered, *covered]) + "\n"
        (repo / "m.py").write_text(body)
        tests = repo / "tests"
        tests.mkdir(exist_ok=True)
        calls = "\n".join(f"    {n}()" for n in covered) or "    pass"
        (tests / "test_m.py").write_text(f"def test_all():\n{calls}\n")

    def test_uncovered_passes_on_match(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Return (True, ...) when the AST oracle's uncovered set matches GT.

        Scenario: check='uncovered'; the authoritative AST test-reference oracle agrees with stored GT.
        """
        task = self._task_uncovered(1, ["orphan"])
        payload = {"total": 1, "uncovered": [{"qualified_name": "orphan"}]}
        self._write_uncovered(tmp_path, ["orphan"], ["used"])

        with patch.object(script_gen_bench, "run_scan_query", return_value=payload):
            ok, _, reason = script_gen_bench._validate_oss(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert ok is True
        assert reason == ""

    def test_uncovered_allows_top_limited_scan_payload(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Keeps the AST oracle authoritative when scan-query returns a capped list.

        Scenario: ``uncovered --top 1`` reports the full total but returns one
        displayed symbol.  The payload is coherent when the requested top limit
        equals the returned list length; rejecting it prevents the independent
        oracle from validating otherwise valid benchmark ground truth.
        """
        task = {
            **self._task_uncovered(1, ["orphan"]),
            "expected_queries": [{"cmd": "uncovered", "args": ["--top", "1"]}],
        }
        self._write_uncovered(tmp_path, ["orphan"], ["used"])
        payload = {"total": 3, "uncovered": [{"qualified_name": "orphan"}]}

        with patch.object(script_gen_bench, "run_scan_query", return_value=payload):
            ok, live_gt, reason = script_gen_bench._validate_oss(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert ok is True
        assert reason == ""
        assert live_gt["uncovered_count"] == 1
        assert live_gt["uncovered_count_scan"] == 3

    def test_uncovered_rejects_incomplete_top_limited_scan_payload(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Rejects a capped response that returns fewer rows than its declared cap."""
        task = {
            **self._task_uncovered(1, ["orphan"]),
            "expected_queries": [{"cmd": "uncovered", "args": ["--top", "2"]}],
        }
        self._write_uncovered(tmp_path, ["orphan"], ["used"])
        payload = {"total": 3, "uncovered": [{"qualified_name": "orphan"}]}

        with patch.object(script_gen_bench, "run_scan_query", return_value=payload):
            ok, live_gt, reason = script_gen_bench._validate_oss(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert ok is False
        assert live_gt is None
        assert reason == "uncovered total conflicts with symbol count"

    def test_uncovered_fails_on_symbol_mismatch(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Return failure when the AST oracle's uncovered set differs from GT.

        Scenario: check='uncovered'; a symbol GT expected as uncovered is actually referenced by a test.
        """
        task = self._task_uncovered(2, ["a", "b"])
        payload = {"total": 2, "uncovered": [{"qualified_name": "a"}, {"qualified_name": "c"}]}
        self._write_uncovered(tmp_path, ["a"], ["b"])  # only 'a' is uncovered; 'b' is test-referenced

        with patch.object(script_gen_bench, "run_scan_query", return_value=payload):
            ok, _, reason = script_gen_bench._validate_oss(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert ok is False
        assert "uncovered" in reason

    def test_coupled_passes_on_match(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Return (True, ...) when top coupled module fields match GT.

        Scenario: check='coupled'; top entry matches GT top_module and counts.
        """
        task = self._task_coupled("my.module", 10, 5)
        payload = {"coupled": [{"name": "my.module", "dep_count": 10, "internal_dep_count": 5}]}

        with patch.object(script_gen_bench, "run_scan_query", return_value=payload):
            ok, _, reason = script_gen_bench._validate_oss(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert ok is True

    def test_coupled_fails_on_top_module_mismatch(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Return failure when top coupled module name differs from GT.

        Scenario: codebase restructured; most-coupled module changed.
        """
        task = self._task_coupled("expected.module", 10, 5)
        payload = {"coupled": [{"name": "actual.module", "dep_count": 10, "internal_dep_count": 5}]}

        with patch.object(script_gen_bench, "run_scan_query", return_value=payload):
            ok, _, reason = script_gen_bench._validate_oss(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert ok is False
        assert "top_module" in reason

    def test_coupled_fails_when_empty_result(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Return (False, None, reason) when coupled list is empty.

        Scenario: scan-query coupled returns no results (unexpected).
        """
        task = self._task_coupled("mod", 1, 0)
        payload = {"coupled": []}

        with patch.object(script_gen_bench, "run_scan_query", return_value=payload):
            ok, live_gt, reason = script_gen_bench._validate_oss(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert ok is False
        assert "empty" in reason

    @pytest.mark.parametrize(
        "live_ranking",
        [
            pytest.param(_coupled_ranking(None)[:4], id="missing-fifth-member"),
            pytest.param(
                [
                    _coupled_ranking(None)[0],
                    {"name": "pkg.unexpected", "dep_count": 8, "internal_dep_count": 4},
                    *_coupled_ranking(None)[2:],
                ],
                id="wrong-second-member",
            ),
            pytest.param(
                [
                    _coupled_ranking(None)[0],
                    _coupled_ranking(None)[2],
                    _coupled_ranking(None)[1],
                    *_coupled_ranking(None)[3:],
                ],
                id="second-and-third-reordered",
            ),
            pytest.param(
                [
                    _coupled_ranking(None)[0],
                    {"name": "pkg.bravo", "dep_count": 8, "internal_dep_count": 99},
                    *_coupled_ranking(None)[2:],
                ],
                id="wrong-internal-dependency-count",
            ),
        ],
    )
    def test_coupled_rejects_missing_or_misordered_top_five(
        self, script_gen_bench: Any, tmp_path: Path, live_ranking: list[dict[str, int | str]]
    ) -> None:
        """CQ-03 validation rejects every incorrect member of the five-row ranking.

        Prevents a false-green benchmark refresh where rank one still matches but the requested top-five answer is
        missing, substituted, reordered, or has a stale ``internal_dep_count`` value.
        """
        expected = self._coupled_ranking()
        task = self._task_coupled_ranking(expected)
        payload = {"coupled": live_ranking}

        with patch.object(script_gen_bench, "run_scan_query", return_value=payload):
            ok, live_gt, reason = script_gen_bench._validate_oss(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert ok is False
        assert live_gt is not None
        assert "top_modules" in reason

    def test_xrefs_passes_on_match(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Return (True, ...) when the AST oracle's broken cross-reference set matches GT.

        Scenario: check='xrefs_broken'; one module docstring holds a single unresolvable
        ``:func:`` role reference and the independent AST oracle agrees with the stored GT
        (and, incidentally, with the diagnostic scan-query snapshot).
        """
        source = 'def f():\n    """See :func:`mod.missing`."""\n    pass\n'
        self._write_xref_source(tmp_path, "mod.py", source)
        targets = [{"target": "mod::missing", "line": 2}]
        task = self._task_xrefs(1, targets)
        payload = {"broken": targets, "count": 1}

        with patch.object(script_gen_bench, "run_scan_query", return_value=payload):
            ok, _, reason = script_gen_bench._validate_oss(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert ok is True, reason

    def test_xrefs_fails_on_broken_count_mismatch(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Return failure when the AST oracle's broken count differs from GT.

        Scenario: a second unresolvable ``:func:`` reference was introduced after GT was
        captured; the oracle now finds two broken targets where GT still expects one.
        """
        source = (
            "def f():\n"
            '    """See :func:`mod.missing`."""\n'
            "    pass\n\n\n"
            "def g():\n"
            '    """See :func:`mod.also_missing`."""\n'
            "    pass\n"
        )
        self._write_xref_source(tmp_path, "mod.py", source)
        task = self._task_xrefs(1, [{"target": "mod::missing", "line": 2}])
        payload = {"broken": [{"target": "mod::missing", "line": 2}], "count": 1}

        with patch.object(script_gen_bench, "run_scan_query", return_value=payload):
            ok, _, reason = script_gen_bench._validate_oss(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert ok is False
        assert "broken_count" in reason

    def test_fails_when_no_expected_queries(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Return (False, None, reason) for check='undocumented' with no queries.

        Scenario: malformed task; expected_queries list absent.
        """
        task = {
            "type": "code_quality",
            "expected_queries": [],
            "ground_truth": {"check": "undocumented", "undocumented_count": 0, "undocumented_symbols": []},
        }

        ok, live_gt, reason = script_gen_bench._validate_oss(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert ok is False
        assert live_gt is None

    def test_scan_query_failure_returns_none(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Report validation failure when the structural query fails.

        Scenario: binary error on any check type; cascade to failure.
        """
        task = self._task_undocumented(0, [])

        with patch.object(script_gen_bench, "run_scan_query", return_value=None):
            ok, live_gt, _ = script_gen_bench._validate_oss(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert ok is False
        assert live_gt is None

    @pytest.mark.parametrize(
        "task,payload,reason_fragment",
        [
            pytest.param(
                _task_undocumented(None, 0, []), {"undocumented": []}, "total", id="_task_undocumented-none-0"
            ),
            pytest.param(
                _task_uncovered(None, 0, []),
                {"total": 0, "uncovered": "not a list"},
                "list",
                id="_task_uncovered-none-0",
            ),
            pytest.param(
                _task_undocumented(None, 2, ["A", "B"]),
                {"total": 2, "undocumented": [{"qualified_name": "A"}]},
                "conflicts",
                id="_task_undocumented-none-2-a-b",
            ),
            pytest.param(
                _task_coupled(None, "mod", 1, 0),
                {"coupled": "not a list"},
                "not a list",
                id="_task_coupled-none-mod-1-0-coupled-not-a-list",
            ),
            pytest.param(
                _task_coupled(None, "mod", 1, 0),
                {"coupled": [1]},
                "not an object",
                id="_task_coupled-none-mod-1-0-coupled-1",
            ),
            pytest.param(
                _task_xrefs(None, 2, [{"target": "mod::Fn", "line": 1}, {"target": "mod::Other", "line": 2}]),
                {"count": 2, "broken": [{"target": "mod::Fn", "line": 1}]},
                "conflicts",
                id="_task_xrefs-none-2-target-mod-fn-line-1-target-mod-other-line-2",
            ),
        ],
    )
    def test_malformed_scan_query_payloads_fail_cleanly(
        self,
        script_gen_bench: Any,
        tmp_path: Path,
        task: dict,
        payload: dict,
        reason_fragment: str,
    ) -> None:
        """Missing keys, wrong types, and count/list conflicts return failures, not crashes."""
        with patch.object(script_gen_bench, "run_scan_query", return_value=payload):
            ok, live_gt, reason = script_gen_bench._validate_oss(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert ok is False
        assert live_gt is None
        assert reason_fragment in reason

    def test_combined_health_validates_both_counts(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Validates both undocumented and uncovered fields for combined_health check.

        Scenario: check='combined_health'; A and B lack docstrings (undocumented) but are
        test-referenced (covered); C has a docstring (documented) but no test reference
        (uncovered). Both independent AST oracle slices must match GT for an overall pass —
        exercises the ``oracle_views`` nesting that keeps the two slices from overwriting
        each other (see ``_attach_oracle_views``).
        """
        (tmp_path / "m.py").write_text(
            "class A:\n    pass\n\n\nclass B:\n    pass\n\n\ndef C():\n    'Has a docstring.'\n    pass\n"
        )
        tests = tmp_path / "tests"
        tests.mkdir()
        (tests / "test_m.py").write_text("def test_it():\n    A()\n    B()\n")

        task = {
            "type": "code_quality",
            "expected_queries": [{"cmd": "undocumented", "args": []}, {"cmd": "uncovered", "args": []}],
            "ground_truth": {
                "check": "combined_health",
                "undocumented_count": 2,
                "undocumented_symbols": ["A", "B"],
                "uncovered_count": 1,
                "uncovered_symbols": ["C"],
            },
        }
        undoc_payload = {"total": 2, "undocumented": [{"qualified_name": "A"}, {"qualified_name": "B"}]}
        uncov_payload = {"total": 1, "uncovered": [{"qualified_name": "C"}]}

        call_counter = {"n": 0}

        def _sq_side_effect(sq: Any, args: list, index: Any, repo: Any) -> dict:
            """Count queries and return the matching undocumented or uncovered payload."""
            call_counter["n"] += 1
            if args[0] == "undocumented":
                return undoc_payload
            if args[0] == "uncovered":
                return uncov_payload
            return None  # type: ignore[return-value]

        with patch.object(script_gen_bench, "run_scan_query", side_effect=_sq_side_effect):
            ok, live_gt, reason = script_gen_bench._validate_oss(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert ok is True, reason
        assert call_counter["n"] == 2  # both queries fired
        assert live_gt["oracle_views"]["undocumented"]["independent_ast"]["count"] == 2
        assert live_gt["oracle_views"]["uncovered"]["independent_ast"]["count"] == 1


# ===========================================================================
# _build_updated_ground_truth
# ===========================================================================


class TestBuildUpdatedGroundTruth:
    """Merge live GT into existing GT correctly per task type."""

    @pytest.mark.parametrize(
        "task_type",
        ["SYMBOL_EXTRACTION", "FN_CALL_GRAPH", "DEVELOP_BLAST_RADIUS", "CODE_QUALITY"],
    )
    def test_merges_live_into_existing(self, script_gen_bench: Any, task_type: str) -> None:
        """Return merged dict with live values overriding existing for standard types.

        Scenario: validator computed live_gt; _build must integrate it into
        existing GT while preserving unrelated fields.
        """
        existing = {"preserved_field": "keep", "start_line": 99}
        live = {"start_line": 10, "end_line": 20}
        result = script_gen_bench._build_updated_ground_truth(script_gen_bench.TaskType[task_type], live, existing)
        assert result["start_line"] == 10  # live overrides
        assert result["preserved_field"] == "keep"  # existing preserved

    def test_review_assistance_returns_live_gt_only(self, script_gen_bench: Any) -> None:
        """Return live_gt unchanged for review_assistance (no merge with existing).

        Scenario: review_assistance stores per-sub_question entries; live_gt is
        the authoritative new structure; existing GT is discarded.
        """
        existing = {"old_key": "old_value"}
        live = {"sq1": {"count": 5}}
        result = script_gen_bench._build_updated_ground_truth(
            script_gen_bench.TaskType.REVIEW_ASSISTANCE, live, existing
        )
        assert result == live
        assert "old_key" not in result


# ===========================================================================
# VALIDATORS routing dict
# ===========================================================================


class TestTaskType:
    """Task types are a closed enum internally and plain strings in JSON."""

    def test_enum_members_match_authoritative_validator_keys(self, script_gen_bench: Any) -> None:
        """The enum and validator routing table declare exactly the same task types."""
        expected_values = {
            "symbol_extraction",
            "fn_call_graph",
            "review_assistance",
            "code_quality",
            "develop_blast_radius",
            "diff_impact",
            "graph_central",
            "graph_path",
            "graph_fn_blast",
            "module_blast_radius",
            "debug_from_trace",
            "feature_scaffolding",
            "real_issue",
        }
        assert {task_type.value for task_type in script_gen_bench.TaskType} == expected_values
        assert set(script_gen_bench.TaskType) == set(script_gen_bench.VALIDATORS)

    def test_main_update_serializes_only_plain_strings_and_preserves_json_bytes(
        self, script_gen_bench: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Task-type enum instances never cross the JSON write boundary.

        A no-op full-file update must keep canonical JSON bytes unchanged, so benchmark suite consumers continue to
        receive plain task-type strings.
        """
        payload = {
            "repo": {},
            "tasks": [
                {
                    "id": "A-01",
                    "type": "symbol_extraction",
                    "ground_truth": {},
                    "expected_queries": [{"cmd": "symbol", "args": ["first"]}],
                }
            ],
        }
        original = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        tasks_file = tmp_path / "tasks.json"
        # newline="\n": the fixture states the LF bytes the assertion compares against, instead of
        # letting the host's text mode seed the file with a different line ending than it expects.
        tasks_file.write_text(original, newline="\n")
        monkeypatch.setattr(script_gen_bench, "TASKS_FILE", tasks_file)

        fake_sq = tmp_path / "scan-query"
        fake_sq.write_text("#!/bin/sh")
        fake_index = tmp_path / "index.json"
        fake_index.write_text("{}")
        real_json_dump = json.dump
        serialized_payloads: list[object] = []

        def _dump_plain_json(value: object, file: Any, **kwargs: Any) -> None:
            """Reject enum instances recursively before recording and serializing the task payload."""

            def _contains_task_type(item: object) -> bool:
                """Detect task-type enum instances in nested dictionary keys, values, or lists."""
                if isinstance(item, script_gen_bench.TaskType):
                    return True
                if isinstance(item, dict):
                    return any(_contains_task_type(key) or _contains_task_type(value) for key, value in item.items())
                if isinstance(item, list):
                    return any(_contains_task_type(value) for value in item)
                return False

            assert not _contains_task_type(value)
            serialized_payloads.append(value)
            real_json_dump(value, file, **kwargs)

        with (
            patch.object(script_gen_bench, "find_codemap_bin", return_value=fake_sq),
            patch.object(script_gen_bench, "resolve_index_path", return_value=fake_index),
            patch.object(
                script_gen_bench, "VALIDATORS", {script_gen_bench.TaskType.SYMBOL_EXTRACTION: lambda *_: (True, {}, "")}
            ),
            patch.object(script_gen_bench.json, "dump", side_effect=_dump_plain_json),
        ):
            script_gen_bench.main(repo_path=str(tmp_path), update=True)

        assert serialized_payloads
        assert tasks_file.read_bytes() == original.encode()
        # The task file is a byte-compared artefact: the write-back must emit LF on every host,
        # never the host line ending that text mode would substitute.
        assert b"\r\n" not in tasks_file.read_bytes()


class TestValidatorsDict:
    """Map each documented task type to its validator."""

    @pytest.mark.parametrize(
        "task_type,expected_fn_name",
        [
            pytest.param("SYMBOL_EXTRACTION", "_validate_symbol", id="symbol_extraction"),
            pytest.param("FN_CALL_GRAPH", "_validate_fn", id="fn_call_graph"),
            pytest.param("DEVELOP_BLAST_RADIUS", "_validate_fn", id="develop_blast_radius"),
            pytest.param("REVIEW_ASSISTANCE", "_validate_rv", id="review_assistance"),
            pytest.param("CODE_QUALITY", "_validate_oss", id="code_quality"),
        ],
    )
    def test_task_type_routes_to_correct_validator(
        self, script_gen_bench: Any, task_type: str, expected_fn_name: str
    ) -> None:
        """Each task type key maps to its documented validator function.

        Scenario: main() looks up VALIDATORS[task_type]; mapping must be
        correct for all five documented task types.
        """
        actual_fn = script_gen_bench.VALIDATORS.get(script_gen_bench.TaskType[task_type])
        expected_fn = getattr(script_gen_bench, expected_fn_name)
        assert actual_fn is expected_fn

    def test_unknown_type_returns_none(self, script_gen_bench: Any) -> None:
        """VALIDATORS.get returns None for an unrecognised task type.

        Scenario: main() skips tasks with unknown type; VALIDATORS.get(type)
        must return None without raising.
        """
        assert script_gen_bench.VALIDATORS.get("nonexistent_type") is None


# ===========================================================================
# _CallerFinder / _callers_via_ast
# ===========================================================================


class TestCallFinderAst:
    """Record the enclosing scope of each matching call site."""

    def _parse_and_visit(self, script_gen_bench: Any, source: str, simple_name: str, rel_module: str) -> set:
        """Parse source and return the callers found by the call visitor."""
        callers: set = set()
        tree = ast.parse(source)
        script_gen_bench._CallFinder(simple_name, rel_module, callers).visit(tree)
        return callers

    def test_finds_direct_call(self, script_gen_bench: Any) -> None:
        """Records module::function when function directly calls the target.

        Scenario: simple function calling target by name.
        """
        source = "def caller():\n    target()\n"
        callers = self._parse_and_visit(script_gen_bench, source, "target", "mod")
        assert "mod::caller" in callers

    def test_finds_attribute_call(self, script_gen_bench: Any) -> None:
        """Records caller when target is called as attribute (obj.target()).

        Scenario: method call via attribute access; dot-call form.
        """
        source = "def caller():\n    obj.target()\n"
        callers = self._parse_and_visit(script_gen_bench, source, "target", "mod")
        assert "mod::caller" in callers

    def test_does_not_record_module_level_call(self, script_gen_bench: Any) -> None:
        """Does not record call at module level (no enclosing scope stack).

        Scenario: call outside any function/class is ignored per _scope_stack
        guard (``if matched and self._scope_stack``).
        """
        source = "target()\n"
        callers = self._parse_and_visit(script_gen_bench, source, "target", "mod")
        assert callers == set()

    def test_nested_class_method(self, script_gen_bench: Any) -> None:
        """Records nested class method as Module::Class.method scope string.

        Scenario: call inside a method; scope stack includes class and method.
        """
        source = "class MyClass:\n    def my_method(self):\n        target()\n"
        callers = self._parse_and_visit(script_gen_bench, source, "target", "mod")
        assert "mod::MyClass.my_method" in callers

    def test_does_not_match_different_name(self, script_gen_bench: Any) -> None:
        """Does not record callers of functions with different names.

        Scenario: false-positive guard; only exact simple name matches.
        """
        source = "def caller():\n    other_fn()\n"
        callers = self._parse_and_visit(script_gen_bench, source, "target", "mod")
        assert callers == set()

    def test_callers_via_ast_empty_repo(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Return empty caller set for repo with no Python files.

        Scenario: fresh empty repository; AST walker finds nothing.
        """
        callers, err = script_gen_bench._callers_via_ast("mod::target", tmp_path)
        assert callers == set()
        assert err is None

    def test_callers_via_ast_finds_caller_in_py_file(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Finds callers from Python files in the repo tree.

        Scenario: single Python file with a caller; walker returns that caller.
        """
        (tmp_path / "example.py").write_text("def do_thing():\n    target()\n")
        callers, err = script_gen_bench._callers_via_ast("mod::target", tmp_path)
        assert "example::do_thing" in callers
        assert err is None

    def test_callers_via_ast_skips_venv_dir(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Skips .venv directory when walking the repo.

        Scenario: virtualenv present; must not traverse into it.
        """
        venv = tmp_path / ".venv" / "lib"
        venv.mkdir(parents=True)
        (venv / "pkg.py").write_text("def hidden():\n    target()\n")

        (tmp_path / "real.py").write_text("# no calls here\n")
        callers, _ = script_gen_bench._callers_via_ast("mod::target", tmp_path)
        assert all(".venv" not in c for c in callers)

    def test_callers_via_ast_handles_syntax_error_gracefully(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Skips files with syntax errors without raising.

        Scenario: malformed Python file in repo; walker must continue.
        """
        (tmp_path / "broken.py").write_text("def bad syntax !!!\n")
        (tmp_path / "good.py").write_text("def ok():\n    target()\n")
        callers, err = script_gen_bench._callers_via_ast("any::target", tmp_path)
        # good.py must still be processed
        assert "good::ok" in callers


# ===========================================================================
# _QualifiedCallFinder / _walk_caller_sets
# ===========================================================================


class TestQualifiedCallerOracle:
    """Authoritative caller set resolves receivers; loose set over-approximates."""

    def test_same_named_method_in_unrelated_class_not_credited(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """A `self.bar()` call in an unrelated class is NOT credited as a caller of Foo.bar.

        Scenario: two classes define a `bar` method call; only the one whose enclosing class is the
        target class resolves. The loose (simple-name) set still contains both — kept as diagnostic.
        """
        src = (
            "class Foo:\n    def caller(self):\n        self.bar()\n\n"
            "class Baz:\n    def other(self):\n        self.bar()\n"
        )
        (tmp_path / "m.py").write_text(src)
        qualified, loose, err = script_gen_bench._walk_caller_sets("m::Foo.bar", tmp_path)
        assert err is None
        assert qualified == {"m::Foo.caller"}
        assert "m::Baz.other" in loose

    def test_direct_class_instantiation_credited(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """A `Foo().bar()` reference resolves to the target class and is credited.

        Scenario: a free function instantiates the class and calls the method directly.
        """
        (tmp_path / "m.py").write_text("def use():\n    Foo().bar()\n")
        qualified, _loose, _ = script_gen_bench._walk_caller_sets("m::Foo.bar", tmp_path)
        assert qualified == {"m::use"}

    def test_module_function_attribute_call_uses_target_module(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """A cross-module `foo.func()` is credited only when `foo` is the TARGET module, not the caller.

        Scenario: the module-level-function branch compares the receiver tail against the target
        module (`pkg.foo`), never the caller file's own module — so `foo.func()` from pkg.bar IS credited while
        `bar.func()` with the same name is NOT.
        """
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "foo.py").write_text("def func():\n    pass\n")
        (pkg / "bar.py").write_text("def use_target():\n    foo.func()\n\n\ndef use_other():\n    bar.func()\n")
        qualified, _loose, _ = script_gen_bench._walk_caller_sets("pkg.foo::func", tmp_path)
        assert qualified == {"pkg.bar::use_target"}

    def test_bare_call_imported_from_external_module_is_not_credited(
        self, script_gen_bench: Any, tmp_path: Path
    ) -> None:
        """Excludes a bare call whose import binding targets a different module.

        Scenario: a project calls an external ``is_overridden`` after importing
        it directly.  Matching its spelling alone would corrupt the canonical
        caller set for the in-repository function with the same name.
        """
        (tmp_path / "caller.py").write_text(
            "from external.helpers import is_overridden\n\n\ndef use_external():\n    is_overridden()\n"
        )

        qualified, loose, err = script_gen_bench._walk_caller_sets("pkg.model_helpers::is_overridden", tmp_path)

        assert err is None
        assert qualified == set()
        assert loose == {"caller::use_external"}

    def test_bare_call_imported_from_target_module_is_credited(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Credits a bare call when its import binding is the target module.

        Scenario: a direct ``from pkg.model_helpers import is_overridden``
        import is an unambiguous reference to the target despite not using a
        module-qualified receiver.
        """
        (tmp_path / "caller.py").write_text(
            "from pkg.model_helpers import is_overridden\n\n\ndef use_target():\n    is_overridden()\n"
        )

        qualified, _loose, err = script_gen_bench._walk_caller_sets("pkg.model_helpers::is_overridden", tmp_path)

        assert err is None
        assert qualified == {"caller::use_target"}

    def test_bare_call_imported_through_one_hop_target_reexport_is_credited(
        self, script_gen_bench: Any, tmp_path: Path
    ) -> None:
        """Credits a target re-export but excludes the same name from an external module.

        Scenario: a package re-exports its target function once, then consumers
        import that facade.  The source resolver must follow the in-repository
        re-export while preserving the external-import exclusion.
        """
        package = tmp_path / "pkg"
        package.mkdir()
        (package / "model_helpers.py").write_text("def is_overridden():\n    pass\n")
        (package / "exports.py").write_text("from pkg.model_helpers import is_overridden\n")
        (tmp_path / "target_user.py").write_text(
            "from pkg.exports import is_overridden\n\n\ndef use_target_reexport():\n    is_overridden()\n"
        )
        (tmp_path / "external_user.py").write_text(
            "from external.helpers import is_overridden\n\n\ndef use_external():\n    is_overridden()\n"
        )

        qualified, loose, err = script_gen_bench._walk_caller_sets("pkg.model_helpers::is_overridden", tmp_path)

        assert err is None
        assert qualified == {"target_user::use_target_reexport"}
        assert loose == {"external_user::use_external", "target_user::use_target_reexport"}

    def test_bare_call_imported_through_two_hop_target_reexport_is_credited(
        self, script_gen_bench: Any, tmp_path: Path
    ) -> None:
        """Credits a caller reached through two package re-export hops.

        Prevents the AST oracle from silently omitting real production callers when a public facade re-exports a symbol
        through an intermediate package, as ``lightning.pytorch.utilities`` does for ``move_data_to_device``.
        """
        package = tmp_path / "pkg"
        package.mkdir()
        (package / "target.py").write_text("def move_data_to_device():\n    pass\n")
        (package / "middle.py").write_text("from pkg.target import move_data_to_device\n")
        (package / "facade.py").write_text("from pkg.middle import move_data_to_device\n")
        (tmp_path / "consumer.py").write_text(
            "from pkg.facade import move_data_to_device\n\n\n"
            "class DataHooks:\n"
            "    def transfer_batch_to_device(self):\n"
            "        return move_data_to_device()\n"
        )

        qualified, _loose, err = script_gen_bench._walk_caller_sets("pkg.target::move_data_to_device", tmp_path)

        assert err is None
        assert qualified == {"consumer::DataHooks.transfer_batch_to_device"}

    def test_unbound_bare_call_preserves_local_fallback(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Preserves the conservative fallback for a bare call with no import binding.

        Scenario: local same-module calls are unresolved statically but remain
        valid caller candidates; import awareness must not erase them.
        """
        (tmp_path / "caller.py").write_text("def use_local():\n    is_overridden()\n")

        qualified, _loose, err = script_gen_bench._walk_caller_sets("pkg.model_helpers::is_overridden", tmp_path)

        assert err is None
        assert qualified == {"caller::use_local"}

    def test_function_local_external_import_does_not_suppress_sibling_local_call(
        self, script_gen_bench: Any, tmp_path: Path
    ) -> None:
        """Applies an external import binding only inside its lexical function."""
        (tmp_path / "caller.py").write_text(
            "def use_external():\n"
            "    from external.helpers import is_overridden\n"
            "    is_overridden()\n\n"
            "def use_local():\n"
            "    is_overridden()\n"
        )

        qualified, loose, err = script_gen_bench._walk_caller_sets("pkg.model_helpers::is_overridden", tmp_path)

        assert err is None
        assert qualified == {"caller::use_local"}
        assert loose == {"caller::use_external", "caller::use_local"}

    def test_alias_of_different_target_symbol_is_not_credited(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Rejects an alias whose module matches but original symbol does not."""
        (tmp_path / "caller.py").write_text(
            "from pkg.model_helpers import other as is_overridden\n\ndef use_other():\n    is_overridden()\n"
        )

        qualified, loose, err = script_gen_bench._walk_caller_sets("pkg.model_helpers::is_overridden", tmp_path)

        assert err is None
        assert qualified == set()
        assert loose == {"caller::use_other"}

    def test_emitted_module_paths_carry_no_src_prefix(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """A caller under a `src/` layout is emitted in the repo namespace, not with a `src.` prefix.

        Scenario: `--update` on a real src-layout clone must write GT in the repo's real namespace.
        """
        f = tmp_path / "src" / "pkg" / "mod.py"
        f.parent.mkdir(parents=True)
        f.write_text("def caller():\n    target()\n")
        qualified, _loose, _ = script_gen_bench._walk_caller_sets("pkg::target", tmp_path)
        assert qualified == {"pkg.mod::caller"}
        assert all(not c.startswith("src.") for c in qualified)

    def test_test_modules_excluded_from_callers(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Callers inside a `tests/` directory are excluded from the emitted namespace.

        Scenario: a test file calls the target; only the production caller is credited.
        """
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_mod.py").write_text("def test_x():\n    target()\n")
        (tmp_path / "prod.py").write_text("def caller():\n    target()\n")
        qualified, _loose, _ = script_gen_bench._walk_caller_sets("mod::target", tmp_path)
        assert qualified == {"prod::caller"}
        assert all("test" not in c for c in qualified)

    def test_src_and_flat_layouts_emit_repo_namespaces(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """A repo mixing src-layout and flat-layout packages emits each in its repo namespace (no `src.`).

        Scenario (parity): `src/pkg/sub/mod.py` (full __init__ chain under src/) and `flatpkg/mod.py`
        (flat __init__) both call the module-level target; the src caller must be credited as
        `pkg.sub.mod::src_caller` (structurally stripping the `src/` prefix) and the flat caller as
        `flatpkg.mod::flat_caller` — matching what scan-query/scan-index emit for the same clone.
        """
        src_mod = tmp_path / "src" / "pkg" / "sub" / "mod.py"
        src_mod.parent.mkdir(parents=True)
        (tmp_path / "src" / "pkg" / "__init__.py").write_text("")
        (tmp_path / "src" / "pkg" / "sub" / "__init__.py").write_text("")
        src_mod.write_text("def src_caller():\n    target()\n")
        flat_mod = tmp_path / "flatpkg" / "mod.py"
        flat_mod.parent.mkdir(parents=True)
        (tmp_path / "flatpkg" / "__init__.py").write_text("")
        flat_mod.write_text("def flat_caller():\n    target()\n")

        qualified, _loose, _ = script_gen_bench._walk_caller_sets("pkg.sub.mod::target", tmp_path)

        assert qualified == {"pkg.sub.mod::src_caller", "flatpkg.mod::flat_caller"}
        assert all(not c.startswith("src.") for c in qualified)


# ===========================================================================
# Provider-neutral generator validators added for B0 oracle remediation
# ===========================================================================


class TestValidateReviewAssistanceAst:
    """Supported review commands obtain ground truth from AST, not scan-query."""

    def _task(self, cmd: str, args: list[str], match: str, ground_truth: dict[str, Any]) -> dict[str, Any]:
        """Build one review task/sub-question for an AST oracle contract."""
        return {
            "type": "review_assistance",
            "expected_queries": [{"cmd": cmd, "args": args}],
            "sub_questions": [{"id": "sq", "match": match, "ground_truth": ground_truth}],
        }

    def test_undocumented_uses_ast_over_conflicting_scan_payload(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Uses the AST symbol set even when the scan diagnostic disagrees."""
        (tmp_path / "m.py").write_text("def public():\n    pass\n")
        task = self._task("undocumented", [], "symbol_name_set", {"symbols": ["public"]})

        with patch.object(script_gen_bench, "run_scan_query", return_value={"total": 0, "undocumented": []}):
            ok, live_gt, reason = script_gen_bench._validate_rv(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert ok is True
        assert reason == ""
        assert live_gt == {"sq": {"symbols": ["public"]}}

    def test_uncovered_uses_ast_over_conflicting_scan_payload(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Uses the AST uncovered set even when scan-query omits the symbol."""
        (tmp_path / "m.py").write_text("def orphan():\n    pass\n")
        task = self._task("uncovered", [], "symbol_name_set", {"symbols": ["orphan"]})

        with patch.object(script_gen_bench, "run_scan_query", return_value={"total": 0, "uncovered": []}):
            ok, live_gt, reason = script_gen_bench._validate_rv(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert ok is True
        assert reason == ""
        assert live_gt == {"sq": {"symbols": ["orphan"]}}

    def test_rdeps_uses_ast_over_conflicting_scan_payload(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Counts direct module importers from the AST rather than scan output."""
        (tmp_path / "hub.py").write_text("value = 1\n")
        (tmp_path / "consumer.py").write_text("import hub\n")
        task = self._task("rdeps", ["hub"], "integer_extract", {"count": 1})

        with patch.object(script_gen_bench, "run_scan_query", return_value={"imported_by": []}):
            ok, live_gt, reason = script_gen_bench._validate_rv(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert ok is True
        assert reason == ""
        assert live_gt == {"sq": {"count": 1}}

    def test_fn_rdeps_uses_ast_over_conflicting_scan_payload(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Counts qualified callers from the AST rather than scan output."""
        (tmp_path / "m.py").write_text("def target():\n    pass\n\n\ndef caller():\n    target()\n")
        task = self._task("fn-rdeps", ["m::target"], "integer_extract", {"count": 1})

        with patch.object(script_gen_bench, "run_scan_query", return_value={"count": 0, "called_by": []}):
            ok, live_gt, reason = script_gen_bench._validate_rv(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert ok is True
        assert reason == ""
        assert live_gt == {"sq": {"count": 1}}

    @pytest.mark.parametrize(
        "cmd,source,expected_symbols",
        [
            pytest.param(
                "undocumented", "def alpha():\n    pass\n\n\ndef beta():\n    pass\n", ["alpha"], id="undocumented"
            ),
            pytest.param("uncovered", "def alpha():\n    pass\n\n\ndef beta():\n    pass\n", ["alpha"], id="uncovered"),
        ],
    )
    def test_ast_symbol_commands_count_full_set_but_cap_symbol_results(
        self,
        script_gen_bench: Any,
        tmp_path: Path,
        cmd: str,
        source: str,
        expected_symbols: list[str],
    ) -> None:
        """Uses full AST cardinality for counts and requested cardinality for symbols.

        Scenario: the independent oracle finds two symbols.  A count question
        must receive ``2``; a symbol-set question with one stored symbol must
        receive only its requested first canonical symbol, matching the review
        task's intentional result cap.
        """
        (tmp_path / "m.py").write_text(source)
        conflicting_scan_payload = {cmd: [], "total": 0}
        count_task = self._task(cmd, [], "integer_extract", {"count": 2})
        symbols_task = self._task(cmd, [], "symbol_name_set", {"symbols": expected_symbols})

        with patch.object(script_gen_bench, "run_scan_query", return_value=conflicting_scan_payload):
            count_ok, count_gt, count_reason = script_gen_bench._validate_rv(
                count_task, MagicMock(), tmp_path / "idx.json", tmp_path
            )
            symbols_ok, symbols_gt, symbols_reason = script_gen_bench._validate_rv(
                symbols_task, MagicMock(), tmp_path / "idx.json", tmp_path
            )

        assert count_ok is True
        assert count_reason == ""
        assert count_gt == {"sq": {"count": 2}}
        assert symbols_ok is True
        assert symbols_reason == ""
        assert symbols_gt == {"sq": {"symbols": expected_symbols}}

    @pytest.mark.parametrize(
        "task,expected",
        [
            pytest.param(
                {
                    "type": "review_assistance",
                    "expected_queries": [{"cmd": "rdeps", "args": ["m"]}],
                },
                True,
                id="type-review_assistance-expected_queries-cmd-rdeps-args-m",
            ),
            pytest.param(
                {
                    "type": "review_assistance",
                    "expected_queries": [{"cmd": "unsupported", "args": []}],
                },
                False,
                id="type-review_assistance-expected_queries-cmd-unsupported-args",
            ),
            pytest.param(
                {
                    "type": "debug_from_trace",
                },
                True,
                id="type-debug_from_trace",
            ),
        ],
    )
    def test_refresh_is_oracle_backed_only_for_supported_review_commands(
        self, script_gen_bench: Any, task: dict[str, Any], expected: bool
    ) -> None:
        """Allows plain refresh only when every review answer has an AST oracle."""
        assert script_gen_bench._update_is_oracle_backed(task) is expected


class TestValidateWorkflowTaskFamilies:
    """Debug, feature, and real-issue tasks have offline canonical validators."""

    def test_debug_requires_exact_safe_path_qualified_symbol_and_line(
        self, script_gen_bench: Any, tmp_path: Path
    ) -> None:
        """Accepts only the exact AST anchor requested by a debug task."""
        source = tmp_path / "src" / "pkg" / "mod.py"
        source.parent.mkdir(parents=True)
        source.write_text("class Anchor:\n    def locate(self):\n        pass\n")
        task = {
            "type": "debug_from_trace",
            "primary_module": "pkg.mod",
            "symbol_name": "Anchor.locate",
            "ground_truth": {"file": "src/pkg/mod.py", "function": "locate", "start_line": 2},
        }

        ok, live_gt, reason = script_gen_bench._validate_debug(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert ok is True
        assert reason == ""
        assert live_gt == task["ground_truth"]

    @pytest.mark.parametrize(
        "ground_truth,reason_fragment",
        [
            pytest.param(
                {"file": "../outside.py", "function": "locate", "start_line": 2},
                "safe",
                id="file-..-outside.py-function-locate-start_line-2",
            )
        ],
    )
    def test_debug_rejects_noncanonical_anchor(
        self,
        script_gen_bench: Any,
        tmp_path: Path,
        ground_truth: dict[str, Any],
        reason_fragment: str,
    ) -> None:
        """Rejects an incorrect line or a path that escapes the target repository."""
        source = tmp_path / "src" / "pkg" / "mod.py"
        source.parent.mkdir(parents=True)
        source.write_text("class Anchor:\n    def locate(self):\n        pass\n")
        task = {
            "type": "debug_from_trace",
            "primary_module": "pkg.mod",
            "symbol_name": "Anchor.locate",
            "ground_truth": ground_truth,
        }

        ok, live_gt, reason = script_gen_bench._validate_debug(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert ok is False
        assert live_gt is None
        assert reason_fragment in reason

    def test_debug_wrong_line_returns_independent_live_gt_for_update(
        self, script_gen_bench: Any, tmp_path: Path
    ) -> None:
        """Return the AST line so a target-version relock can refresh stale debug GT."""
        source = tmp_path / "src" / "pkg" / "mod.py"
        source.parent.mkdir(parents=True)
        source.write_text("class Anchor:\n    def locate(self):\n        pass\n")
        task = {
            "type": "debug_from_trace",
            "primary_module": "pkg.mod",
            "symbol_name": "Anchor.locate",
            "ground_truth": {"file": "src/pkg/mod.py", "function": "locate", "start_line": 3},
        }

        ok, live_gt, reason = script_gen_bench._validate_debug(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert ok is False
        assert live_gt == {"file": "src/pkg/mod.py", "function": "locate", "start_line": 2}
        assert "start_line" in reason

    def test_feature_requires_exact_safe_file_and_entry_point(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Accepts a feature extension point only when its AST definition exists."""
        source = tmp_path / "src" / "pkg" / "feature.py"
        source.parent.mkdir(parents=True)
        source.write_text("class Extension:\n    def hook(self):\n        pass\n")
        task = {
            "type": "feature_scaffolding",
            "primary_module": "pkg.feature",
            "ground_truth": {"primary_file": "src/pkg/feature.py", "entry_point": "Extension.hook"},
        }

        ok, live_gt, reason = script_gen_bench._validate_feature(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert ok is True
        assert reason == ""
        assert live_gt == task["ground_truth"]

    def test_feature_rejects_missing_entry_point(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Rejects a feature task whose declared extension point does not exist."""
        source = tmp_path / "src" / "pkg" / "feature.py"
        source.parent.mkdir(parents=True)
        source.write_text("class Extension:\n    def hook(self):\n        pass\n")
        task = {
            "type": "feature_scaffolding",
            "primary_module": "pkg.feature",
            "ground_truth": {"primary_file": "src/pkg/feature.py", "entry_point": "Extension.missing"},
        }

        ok, live_gt, reason = script_gen_bench._validate_feature(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert ok is False
        assert live_gt is None
        assert "entry_point" in reason

    def _real_issue_task(self, scoreable: bool, files_changed: list[str]) -> dict[str, Any]:
        """Build a canonical real-issue task with immutable offline provenance."""
        return {
            "type": "real_issue",
            "issue_number": 42,
            "issue_url": "https://github.com/example/project/issues/42",
            "pr_number": 43,
            "pr_url": "https://github.com/example/project/pull/43",
            "scoreable": scoreable,
            "note": "legacy paths intentionally unscoreable" if not scoreable else None,
            "ground_truth": {"file_count": len(files_changed), "files_changed": files_changed},
            "provenance": {
                "source": "github_pull_request_files",
                "pr_head_sha": "a" * 40,
                "merge_commit_sha": "b" * 40,
                "changed_file_count": len(files_changed) + 1,
                "selection": "non_test_python_files",
            },
        }

    def test_real_issue_validates_scoreable_paths_and_locked_provenance(
        self, script_gen_bench: Any, tmp_path: Path
    ) -> None:
        """Accepts an applicable historical issue with safe paths and immutable provenance."""
        source = tmp_path / "src" / "pkg" / "fix.py"
        source.parent.mkdir(parents=True)
        source.write_text("value = 1\n")
        task = self._real_issue_task(True, ["src/pkg/fix.py"])

        ok, live_gt, reason = script_gen_bench._validate_real_issue(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert ok is True
        assert reason == ""
        assert live_gt == task["ground_truth"]

    def test_real_issue_preserves_explicitly_unscoreable_historical_paths(
        self, script_gen_bench: Any, tmp_path: Path
    ) -> None:
        """Allows a documented legacy task whose verified paths do not exist in this checkout."""
        task = self._real_issue_task(False, ["legacy/pkg/fix.py"])

        ok, live_gt, reason = script_gen_bench._validate_real_issue(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert ok is True
        assert reason == ""
        assert live_gt == task["ground_truth"]

    def test_real_issue_rejects_missing_scoreable_path(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Rejects a scoreable issue when its canonical changed path is absent locally."""
        task = self._real_issue_task(True, ["src/pkg/missing.py"])

        ok, live_gt, reason = script_gen_bench._validate_real_issue(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert ok is False
        assert live_gt is None
        assert "does not exist" in reason


# ===========================================================================
# main() unit + error paths
# ===========================================================================


class TestIsPublicQualname:
    """Public qualified name = no dotted component starts with underscore."""

    @pytest.mark.parametrize(
        "name,expected",
        [
            pytest.param("Trainer.fit", True, id="trainer.fit"),
            pytest.param("func", True, id="func"),
            pytest.param("_helper", False, id="_helper"),
            pytest.param("_Cache.get", False, id="_cache.get"),
            pytest.param("Trainer.__init__", False, id="trainer.__init__"),
            pytest.param("", False, id="empty"),
        ],
    )
    def test_public_rule(self, script_gen_bench: Any, name: str, expected: bool) -> None:
        """Mirrors scan-query _is_public_symbol: any leading-underscore component is private."""
        assert script_gen_bench._is_public_qualname(name) is expected


class TestUndocumentedViaAst:
    """Independent AST oracle lists public symbols lacking a docstring."""

    def test_finds_public_undocumented(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Public class/function without a docstring is reported."""
        (tmp_path / "m.py").write_text("class A:\n    pass\n\n\ndef pub():\n    pass\n")
        syms, err = script_gen_bench._undocumented_via_ast(tmp_path)
        assert err is None
        assert syms == {"A", "pub"}

    def test_documented_symbol_excluded(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """A symbol with a docstring is not reported as undocumented."""
        (tmp_path / "m.py").write_text('def pub():\n    """Doc."""\n    return 1\n')
        syms, _ = script_gen_bench._undocumented_via_ast(tmp_path)
        assert "pub" not in syms

    def test_private_symbol_excluded(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Private (leading-underscore) symbols are excluded even without a docstring."""
        (tmp_path / "m.py").write_text("def _helper():\n    pass\n")
        syms, _ = script_gen_bench._undocumented_via_ast(tmp_path)
        assert syms == set()

    def test_nested_method_qualified_name(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Methods are reported as Class.method module-relative qualified names."""
        (tmp_path / "m.py").write_text("class Cls:\n    def meth(self):\n        pass\n")
        syms, _ = script_gen_bench._undocumented_via_ast(tmp_path)
        assert "Cls.meth" in syms

    def test_test_modules_skipped(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Files named test_*.py are skipped (scan-query skips test modules)."""
        (tmp_path / "test_x.py").write_text("def pub():\n    pass\n")
        syms, _ = script_gen_bench._undocumented_via_ast(tmp_path)
        assert syms == set()

    def test_module_filter_unresolvable_returns_error(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """A module name that maps to no file yields an error reason, empty set."""
        syms, err = script_gen_bench._undocumented_via_ast(tmp_path, module="pkg.missing")
        assert syms == set()
        assert err is not None and "not resolvable" in err

    def test_module_filter_resolves_src_layout(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """A dotted module resolves against <repo>/src/<parts>.py."""
        f = tmp_path / "src" / "pkg" / "mod.py"
        f.parent.mkdir(parents=True)
        f.write_text("def pub():\n    pass\n")
        syms, err = script_gen_bench._undocumented_via_ast(tmp_path, module="pkg.mod")
        assert err is None
        assert syms == {"pub"}


class TestUncoveredViaAst:
    """Independent AST oracle lists public symbols with no test references."""

    def test_symbol_with_no_test_reference_is_uncovered(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """A public symbol absent from every configured test-AST reference is uncovered."""
        (tmp_path / "m.py").write_text("def orphan():\n    pass\n")
        syms, err = script_gen_bench._uncovered_via_ast(tmp_path)
        assert err is None
        assert syms == {"orphan"}

    def test_test_called_symbol_is_covered(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """A public symbol a test file calls is NOT reported as uncovered."""
        (tmp_path / "m.py").write_text("def used():\n    pass\n")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_m.py").write_text("def test_it():\n    used()\n")
        syms, _ = script_gen_bench._uncovered_via_ast(tmp_path)
        assert "used" not in syms

    @pytest.mark.parametrize("test_expression", ["mentioned", "fixture.mentioned"])
    def test_every_test_name_and_attribute_reference_counts_as_coverage(
        self, script_gen_bench: Any, tmp_path: Path, test_expression: str
    ) -> None:
        """The broad oracle records references even when they are not call targets."""
        (tmp_path / "m.py").write_text("def mentioned():\n    pass\n")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_m.py").write_text(f"def test_it():\n    {test_expression}\n")

        syms, _ = script_gen_bench._uncovered_via_ast(tmp_path)

        assert "mentioned" not in syms

    def test_mock_patched_symbol_is_covered(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """A symbol referenced only through a patch() string target counts as covered."""
        (tmp_path / "m.py").write_text("def mocked():\n    pass\n")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_m.py").write_text(
            "from unittest.mock import patch\n\n\n@patch('m.mocked')\ndef test_it():\n    pass\n"
        )
        syms, _ = script_gen_bench._uncovered_via_ast(tmp_path)
        assert "mocked" not in syms

    def test_every_patch_string_argument_tail_counts_as_coverage(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """The declared oracle records every string argument to a recognized patch call."""
        (tmp_path / "m.py").write_text("def first():\n    pass\n\n\ndef second():\n    pass\n")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_m.py").write_text(
            "from unittest.mock import patch\n\n\ndef test_it():\n    patch('m.first', 'm.second')\n"
        )

        syms, _ = script_gen_bench._uncovered_via_ast(tmp_path)

        assert syms == set()

    def test_private_symbol_excluded(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Private (leading-underscore) symbols are never reported (scan-query public-only rule)."""
        (tmp_path / "m.py").write_text("def _helper():\n    pass\n")
        syms, _ = script_gen_bench._uncovered_via_ast(tmp_path)
        assert syms == set()

    def test_nested_method_qualified_name(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Uncovered methods are reported as module-relative Class.method qualified names."""
        (tmp_path / "m.py").write_text("class Cls:\n    def orphan(self):\n        pass\n")
        syms, _ = script_gen_bench._uncovered_via_ast(tmp_path)
        assert "Cls.orphan" in syms

    def test_module_filter_scopes_scan(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """A module filter restricts the source scan to that module."""
        f = tmp_path / "src" / "pkg" / "mod.py"
        f.parent.mkdir(parents=True)
        f.write_text("def solo():\n    pass\n")
        syms, err = script_gen_bench._uncovered_via_ast(tmp_path, module="pkg.mod")
        assert err is None
        assert syms == {"solo"}


class TestXrefsBrokenViaAst:
    """Independent AST oracle for unresolved Sphinx/MkDocs cross-references (mirrors scan-index/scan-query)."""

    def test_bare_meth_role_broken_without_matching_top_level_function(
        self, script_gen_bench: Any, tmp_path: Path
    ) -> None:
        """A bare ``:meth:`name`` `` role with no matching top-level function is broken."""
        source = "class Cls:\n    def method(self):\n        'See :meth:`missing`.'\n        pass\n"
        (tmp_path / "mod.py").write_text(source)
        broken, err = script_gen_bench._xrefs_broken_via_ast(tmp_path)
        assert err is None
        assert [(b["target"], b["role"]) for b in broken] == [("mod::missing", "meth")]

    def test_bare_meth_role_resolves_to_matching_top_level_function(
        self, script_gen_bench: Any, tmp_path: Path
    ) -> None:
        """A bare ``:meth:`name`` `` role matching a real top-level function is NOT broken.

        Regression guard for the same name as :meth:`test_bare_meth_role_broken_without_matching_top_level_function`
        — proves brokenness depends on the symbol map, not on the role text alone.
        """
        source = (
            "class Cls:\n"
            "    def method(self):\n"
            "        'See :meth:`helper`.'\n"
            "        pass\n\n\n"
            "def helper():\n"
            "    pass\n"
        )
        (tmp_path / "mod.py").write_text(source)
        broken, err = script_gen_bench._xrefs_broken_via_ast(tmp_path)
        assert err is None
        assert broken == []

    def test_dotted_func_role_attributed_to_target_module(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """A dotted target is scoped by the TARGET's module, not the docstring's home module.

        Mirrors scan-query's own scoping (``query.py:3826-3828``): ``--broken <module>`` filters on the resolved
        target's prefix, so a broken reference living in one file's docstring is attributed to a different module
        entirely.
        """
        (tmp_path / "a.py").write_text("def f():\n    'See :func:`b.missing`.'\n    pass\n")
        (tmp_path / "b.py").write_text("def present():\n    pass\n")
        broken, err = script_gen_bench._xrefs_broken_via_ast(tmp_path, module="b")
        assert err is None
        assert [(b["target"], b["file"]) for b in broken] == [("b::missing", "a.py")]

    def test_module_level_assignment_target_stays_broken(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """A module-level assignment is outside the narrow symbol map, so a reference to it stays broken.

        Regression guard for the symbol-map-breadth risk: a broader map (module/class-level
        assignments included) would resolve this and silently under-report broken targets —
        verified empirically to flip a known-broken scan-query reference to resolvable.
        """
        source = "X = 1\n\n\ndef f():\n    'See :func:`mod.X`.'\n    pass\n"
        (tmp_path / "mod.py").write_text(source)
        broken, err = script_gen_bench._xrefs_broken_via_ast(tmp_path)
        assert err is None
        assert [(b["target"], b["role"]) for b in broken] == [("mod::X", "func")]

    @pytest.mark.parametrize("role", ["attr", "data", "mod"])
    def test_role_excluded_from_symbol_role_filter(self, script_gen_bench: Any, tmp_path: Path, role: str) -> None:
        """``attr``/``data``/``mod`` roles are extracted but never checked for brokenness.

        Mirrors ``query.py`` ``_SYMBOL_ROLES``, which deliberately omits these three roles.
        """
        source = f"def f():\n    'See :{role}:`mod.missing`.'\n    pass\n"
        (tmp_path / "mod.py").write_text(source)
        broken, err = script_gen_bench._xrefs_broken_via_ast(tmp_path)
        assert err is None
        assert broken == []

    def test_line_is_docstring_opening_line_for_multiline_docstring(
        self, script_gen_bench: Any, tmp_path: Path
    ) -> None:
        """The reported line is the docstring literal's opening line, not the matched-role line.

        Mirrors scan-index's documented approximation (``scanner.py:1866-1868``): AST docstring nodes only expose the
        opening literal's ``lineno``, so per-role-match line offsets are not tracked.
        """
        source = "def f():\n    '''First line.\n\n    See :func:`mod.missing` on a later line.\n    '''\n    pass\n"
        (tmp_path / "mod.py").write_text(source)
        broken, err = script_gen_bench._xrefs_broken_via_ast(tmp_path)
        assert err is None
        assert broken[0]["line"] == 2

    def test_rst_dotted_ref_to_existing_symbol_not_broken(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """A dotted ``.rst`` role resolving to a real symbol is not reported broken."""
        (tmp_path / "mod.py").write_text("def present():\n    pass\n")
        (tmp_path / "index.rst").write_text("See :func:`mod.present`.\n")
        broken, err = script_gen_bench._xrefs_broken_via_ast(tmp_path)
        assert err is None
        assert broken == []

    def test_rst_dotted_ref_to_missing_symbol_is_broken(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """A dotted ``.rst`` role with no matching symbol is reported broken, with a real line number."""
        (tmp_path / "index.rst").write_text("Intro.\nSee :func:`mod.missing`.\n")
        broken, err = script_gen_bench._xrefs_broken_via_ast(tmp_path)
        assert err is None
        assert [(b["target"], b["file"], b["line"]) for b in broken] == [("mod::missing", "index.rst", 2)]

    def test_rst_bare_ref_never_resolves(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """A bare (dotless) ``.rst`` role target has no ``::`` and can never match the symbol map.

        ``.rst`` files anchor to no Python module (``current_module=""``); a bare role target
        resolves to the raw string unchanged (scan-index's own docstring claims such targets are
        "dropped", but the code does not do that — verified directly against
        ``codemap_py.scanner._resolve_xref_target``). Reproducing the actual behavior, not the
        stale docstring, is the whole point of an independent oracle.
        """
        (tmp_path / "index.rst").write_text("See :func:`missing_bare`.\n")
        broken, err = script_gen_bench._xrefs_broken_via_ast(tmp_path)
        assert err is None
        assert [b["target"] for b in broken] == ["missing_bare"]

    def test_mkdocs_ref_under_docs_dir_counted(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """A dotted mkdocstrings autoref under ``docs/`` is scanned and can be broken."""
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "api.md").write_text("See [`mod.missing`][].\n")
        broken, err = script_gen_bench._xrefs_broken_via_ast(tmp_path)
        assert err is None
        assert [(b["target"], b["source"]) for b in broken] == [("mod::missing", "mkdocs")]

    def test_mkdocs_ref_outside_docs_dir_not_scanned(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """The identical autoref in a repo-root README.md is NOT scanned (scan-index restricts .md to docs/)."""
        (tmp_path / "README.md").write_text("See [`mod.missing`][].\n")
        broken, err = script_gen_bench._xrefs_broken_via_ast(tmp_path)
        assert err is None
        assert broken == []

    def test_unresolvable_module_argument_returns_error(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """An unresolvable module argument is a hard failure, not a silent zero-broken pass."""
        broken, err = script_gen_bench._xrefs_broken_via_ast(tmp_path, module="nope.missing")
        assert broken == []
        assert err is not None


class TestValidateFnAstAuthoritative:
    """Treat the independent syntax-tree oracle as authoritative during function validation."""

    def _task(self, primary_fn: str, callers: list[str]) -> dict:
        """Build an fn_call_graph task with the given expected callers."""
        return {
            "type": "fn_call_graph",
            "primary_fn": primary_fn,
            "ground_truth": {"fn_callers": callers, "unique_caller_count": len(callers), "raw_caller_count": 0},
        }

    def test_scan_output_stored_as_diagnostic(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Scan-query callers are preserved under fn_callers_scan, not as the authoritative set."""
        (tmp_path / "mod" / "a.py").parent.mkdir(parents=True)
        (tmp_path / "mod" / "a.py").write_text("def fn_a():\n    target()\n")
        task = self._task("mod::target", ["mod.a::fn_a"])
        payload = {"called_by": [{"caller": "other::ghost"}], "count": 1}

        with patch.object(script_gen_bench, "run_scan_query", return_value=payload):
            _, live_gt, _ = script_gen_bench._validate_fn(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        assert live_gt["fn_callers"] == ["mod.a::fn_a"]  # AST oracle authoritative
        assert live_gt["fn_callers_scan"] == ["other::ghost"]  # scan diagnostic
        assert live_gt["ast_divergence"]["scan_only"] == ["other::ghost"]
        assert live_gt["ast_divergence"]["ast_only"] == ["mod.a::fn_a"]

    def test_divergence_warning_printed(self, script_gen_bench: Any, tmp_path: Path, capsys: Any) -> None:
        """A loud divergence banner is printed when AST and scan disagree."""
        task = self._task("mod::target", [])
        payload = {"called_by": [{"caller": "other::ghost"}], "count": 1}

        with patch.object(script_gen_bench, "run_scan_query", return_value=payload):
            script_gen_bench._validate_fn(task, MagicMock(), tmp_path / "idx.json", tmp_path)

        out = capsys.readouterr().out
        assert "DIVERGENCE" in out


class TestUpdateGating:
    """Circular (scan-derived) refresh is gated behind ``--update-from-tool``."""

    @pytest.mark.parametrize(
        "task,expected",
        [
            pytest.param({"type": "fn_call_graph"}, True, id="type-fn_call_graph"),
            pytest.param({"type": "develop_blast_radius"}, True, id="type-develop_blast_radius"),
            pytest.param(
                {"type": "code_quality", "ground_truth": {"check": "undocumented"}},
                True,
                id="type-code_quality-ground_truth-check-undocumented",
            ),
            pytest.param(
                {"type": "code_quality", "ground_truth": {"check": "uncovered"}},
                True,
                id="type-code_quality-ground_truth-check-uncovered",
            ),
            pytest.param(
                {"type": "code_quality", "ground_truth": {"check": "combined_health"}},
                True,
                id="type-code_quality-ground_truth-check-combined_health",
            ),
            pytest.param(
                {"type": "code_quality", "ground_truth": {"check": "xrefs_broken"}},
                True,
                id="type-code_quality-ground_truth-check-xrefs_broken",
            ),
            pytest.param(
                {"type": "code_quality", "ground_truth": {"check": "coupled"}},
                False,
                id="type-code_quality-ground_truth-check-coupled",
            ),
            pytest.param({"type": "review_assistance"}, False, id="type-review_assistance"),
            pytest.param({"type": "symbol_extraction"}, False, id="type-symbol_extraction"),
        ],
    )
    def test_oracle_backed_classification(self, script_gen_bench: Any, task: dict, expected: bool) -> None:
        """Only AST-oracle-backed types are safe to refresh under a plain ``--update``."""
        assert script_gen_bench._update_is_oracle_backed(task) is expected

    def test_oracle_backed_type_refreshes_by_default(self, script_gen_bench: Any) -> None:
        """An fn_call_graph task refreshes under plain ``--update`` (update_from_tool=False)."""
        task = {"type": "fn_call_graph", "ground_truth": {"fn_callers": []}}
        live = {"fn_callers": ["m::a"], "unique_caller_count": 1}
        stored, status = script_gen_bench._refresh_task_gt(task, live, update_from_tool=False)
        assert status == "UPDATED"
        assert stored["ground_truth"]["fn_callers"] == ["m::a"]

    def test_tool_derived_type_skipped_without_flag(self, script_gen_bench: Any) -> None:
        """A scan-derived task (coupled) is NOT refreshed under plain ``--update``; original preserved."""
        task = {"type": "code_quality", "ground_truth": {"check": "coupled", "top_dep_count": 1}}
        live = {"check": "coupled", "top_dep_count": 99}
        stored, status = script_gen_bench._refresh_task_gt(task, live, update_from_tool=False)
        assert "SKIP UPDATE" in status
        assert stored["ground_truth"]["top_dep_count"] == 1  # unchanged

    def test_tool_derived_type_refreshes_with_flag_and_warns(self, script_gen_bench: Any, capsys: Any) -> None:
        """Verify command-line option behavior.

        ``--update-from-tool`` refreshes scan-derived (coupled) GT after a loud circularity warning.
        """
        task = {"type": "code_quality", "ground_truth": {"check": "coupled", "top_dep_count": 1}}
        live = {"check": "coupled", "top_dep_count": 99}
        stored, status = script_gen_bench._refresh_task_gt(task, live, update_from_tool=True)
        assert status == "UPDATED"
        assert stored["ground_truth"]["top_dep_count"] == 99
        assert "CIRCULAR UPDATE" in capsys.readouterr().out

    def test_review_assistance_merges_sub_questions_with_flag(self, script_gen_bench: Any) -> None:
        """review_assistance refresh (tool-derived) merges per-sub_question GT under the flag."""
        task = {
            "type": "review_assistance",
            "sub_questions": [{"id": "sq1", "ground_truth": {"count": 1}}],
        }
        live = {"sq1": {"count": 5}}
        stored, status = script_gen_bench._refresh_task_gt(task, live, update_from_tool=True)
        assert status == "UPDATED"
        assert stored["sub_questions"][0]["ground_truth"] == {"count": 5}

    def test_review_refresh_preserves_named_oracle_views(self, script_gen_bench: Any) -> None:
        """Regeneration cannot erase the AST/static semantic split from a review task."""
        views = {
            "independent_ast": {"count": 11, "semantics": "unique symbols"},
            "codemap_static": {"count": 25, "semantics": "static findings"},
        }
        task = {
            "type": "review_assistance",
            "ground_truth": {"oracle_views": views},
            "sub_questions": [{"id": "sq1", "ground_truth": {"count": 1}}],
        }

        stored, status = script_gen_bench._refresh_task_gt(task, {"sq1": {"count": 11}}, update_from_tool=True)

        assert status == "UPDATED"
        assert stored["ground_truth"]["oracle_views"] == views
        assert stored["sub_questions"][0]["ground_truth"] == {"count": 11}


class TestMainUnitBehavior:
    """Unit-level tests for main() error paths using mocked dependencies."""

    def test_main_exits_when_tasks_file_unreadable(
        self, script_gen_bench: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Report a missing task manifest and stop task generation.

        Scenario: tasks-bench.json deleted; main must exit with code 1.
        """
        broken_path = tmp_path / "nonexistent.json"
        monkeypatch.setattr(script_gen_bench, "TASKS_FILE", broken_path)

        with pytest.raises(SystemExit) as exc_info:
            script_gen_bench.main(repo_path=str(tmp_path))
        assert exc_info.value.code == 1

    def test_main_exits_when_no_repo_path(
        self, script_gen_bench: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """Exit 1 when no ``--repo-path`` and no local_path in tasks file.

        Scenario: tasks file has no repo.local_path; user forgot ``--repo-path``.
        """
        tasks_file = tmp_path / "tasks.json"
        tasks_file.write_text(json.dumps({"repo": {}, "tasks": []}))
        monkeypatch.setattr(script_gen_bench, "TASKS_FILE", tasks_file)

        with pytest.raises(SystemExit) as exc_info:
            script_gen_bench.main()  # no repo_path
        assert exc_info.value.code == 1

    def test_main_exits_when_repo_path_not_dir(
        self, script_gen_bench: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exit 1 when ``--repo-path`` exists but is a file, not a directory.

        Scenario: user passes a file path by mistake.
        """
        tasks_file = tmp_path / "tasks.json"
        tasks_file.write_text(json.dumps({"repo": {}, "tasks": []}))
        monkeypatch.setattr(script_gen_bench, "TASKS_FILE", tasks_file)

        file_path = tmp_path / "notadir.txt"
        file_path.write_text("content")

        with pytest.raises(SystemExit) as exc_info:
            script_gen_bench.main(repo_path=str(file_path))
        assert exc_info.value.code == 1

    def test_main_exits_when_scan_query_not_found(
        self, script_gen_bench: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exit 1 when scan-query binary cannot be found.

        Scenario: no binary on PATH and no plugin directory fallback.
        """
        tasks_file = tmp_path / "tasks.json"
        tasks_file.write_text(json.dumps({"repo": {}, "tasks": []}))
        monkeypatch.setattr(script_gen_bench, "TASKS_FILE", tasks_file)

        with patch.object(script_gen_bench, "find_codemap_bin", return_value=None):
            with pytest.raises(SystemExit) as exc_info:
                script_gen_bench.main(repo_path=str(tmp_path))
        assert exc_info.value.code == 1

    def test_main_exits_when_index_not_found(
        self, script_gen_bench: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exit 1 when resolved index path does not exist.

        Scenario: scan-query found but codemap index JSON missing.
        """
        tasks_file = tmp_path / "tasks.json"
        tasks_file.write_text(json.dumps({"repo": {}, "tasks": []}))
        monkeypatch.setattr(script_gen_bench, "TASKS_FILE", tasks_file)

        fake_sq = tmp_path / "scan-query"
        fake_sq.write_text("#!/bin/sh")
        nonexistent_index = tmp_path / "missing.json"

        with (
            patch.object(script_gen_bench, "find_codemap_bin", return_value=fake_sq),
            patch.object(script_gen_bench, "resolve_index_path", return_value=nonexistent_index),
        ):
            with pytest.raises(SystemExit) as exc_info:
                script_gen_bench.main(repo_path=str(tmp_path))
        assert exc_info.value.code == 1

    def test_main_fails_closed_on_unknown_task_type(
        self, script_gen_bench: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """Print SKIP and exit nonzero for an unrecognised type.

        Scenario: a missing validator must not produce a green canonical B0 run.
        """
        tasks_file = tmp_path / "tasks.json"
        tasks_file.write_text(
            json.dumps(
                {
                    "repo": {},
                    "tasks": [{"id": "X-01", "type": "unknown_type", "ground_truth": {}}],
                }
            )
        )
        monkeypatch.setattr(script_gen_bench, "TASKS_FILE", tasks_file)

        fake_sq = tmp_path / "scan-query"
        fake_sq.write_text("#!/bin/sh")
        fake_index = tmp_path / "index.json"
        fake_index.write_text("{}")

        with (
            patch.object(script_gen_bench, "find_codemap_bin", return_value=fake_sq),
            patch.object(script_gen_bench, "resolve_index_path", return_value=fake_index),
        ):
            with pytest.raises(SystemExit) as exc_info:
                script_gen_bench.main(repo_path=str(tmp_path))

        out = capsys.readouterr().out
        assert exc_info.value.code == 1
        assert "SKIP" in out
        assert "X-01" in out

    def test_main_update_aborts_on_unknown_task_type(
        self, script_gen_bench: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """Fails update mode without rewriting a suite that contains a SKIP."""
        tasks_file = tmp_path / "tasks.json"
        original = json.dumps(
            {
                "repo": {"name": "fixture"},
                "tasks": [{"id": "X-01", "type": "unknown_type", "ground_truth": {}}],
            }
        )
        tasks_file.write_text(original)
        monkeypatch.setattr(script_gen_bench, "TASKS_FILE", tasks_file)
        fake_sq = tmp_path / "scan-query"
        fake_sq.write_text("#!/bin/sh")
        fake_index = tmp_path / "index.json"
        fake_index.write_text("{}")

        with (
            patch.object(script_gen_bench, "find_codemap_bin", return_value=fake_sq),
            patch.object(script_gen_bench, "resolve_index_path", return_value=fake_index),
            pytest.raises(SystemExit) as exc_info,
        ):
            script_gen_bench.main(repo_path=str(tmp_path), update=True)

        assert exc_info.value.code == 1
        assert tasks_file.read_text() == original
        assert "Update aborted" in capsys.readouterr().out

    def test_main_reports_pass_fail_skip_counts_separately(
        self, script_gen_bench: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """Reports each terminal validation state without treating a skip as a pass.

        Scenario: one supported task passes, another fails, and an unknown task
        skips.  The human-facing summary must preserve the exact distribution
        rather than deriving passes from ``total - failures``.
        """
        tasks_file = tmp_path / "tasks.json"
        tasks_file.write_text(
            json.dumps(
                {
                    "repo": {},
                    "tasks": [
                        {
                            "id": "P-01",
                            "type": "symbol_extraction",
                            "ground_truth": {},
                            "expected_queries": [{"cmd": "symbol", "args": ["passing"]}],
                        },
                        {
                            "id": "F-01",
                            "type": "fn_call_graph",
                            "ground_truth": {},
                            "expected_queries": [{"cmd": "symbol", "args": ["failing"]}],
                        },
                        {"id": "S-01", "type": "unknown", "ground_truth": {}},
                    ],
                }
            )
        )
        monkeypatch.setattr(script_gen_bench, "TASKS_FILE", tasks_file)
        fake_sq = tmp_path / "scan-query"
        fake_sq.write_text("#!/bin/sh")
        fake_index = tmp_path / "index.json"
        fake_index.write_text("{}")

        with (
            patch.object(script_gen_bench, "find_codemap_bin", return_value=fake_sq),
            patch.object(script_gen_bench, "resolve_index_path", return_value=fake_index),
            patch.object(
                script_gen_bench,
                "VALIDATORS",
                {
                    script_gen_bench.TaskType.SYMBOL_EXTRACTION: lambda *_args: (True, {}, ""),
                    script_gen_bench.TaskType.FN_CALL_GRAPH: lambda *_args: (False, {}, "intentional failure"),
                },
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            script_gen_bench.main(repo_path=str(tmp_path))

        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "1/3 passed" in out
        assert "1 failed" in out
        assert "1 skipped" in out

    def test_main_filters_by_task_id(
        self, script_gen_bench: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Run only the task matching ``--task <id>`` when filter supplied.

        Scenario: user passes ``--task SE-01``; only that task is validated.
        """
        tasks_file = tmp_path / "tasks.json"
        tasks_file.write_text(
            json.dumps(
                {
                    "repo": {},
                    "tasks": [
                        {
                            "id": "SE-01",
                            "type": "symbol_extraction",
                            "ground_truth": {},
                            "expected_queries": [{"cmd": "symbol", "args": ["selected"]}],
                        },
                        {
                            "id": "SE-02",
                            "type": "symbol_extraction",
                            "ground_truth": {},
                            "expected_queries": [{"cmd": "symbol", "args": ["unselected"]}],
                        },
                    ],
                }
            )
        )
        monkeypatch.setattr(script_gen_bench, "TASKS_FILE", tasks_file)

        fake_sq = tmp_path / "scan-query"
        fake_sq.write_text("#!/bin/sh")
        fake_index = tmp_path / "index.json"
        fake_index.write_text("{}")

        processed_ids: list[str] = []

        def _tracking_validator(task: dict, sq: Any, index: Any, repo: Any) -> tuple:
            """Record the validated task identifier and accept its fixture contract."""
            processed_ids.append(task["id"])
            return True, {}, ""

        with (
            patch.object(script_gen_bench, "find_codemap_bin", return_value=fake_sq),
            patch.object(script_gen_bench, "resolve_index_path", return_value=fake_index),
            patch.object(
                script_gen_bench, "VALIDATORS", {script_gen_bench.TaskType.SYMBOL_EXTRACTION: _tracking_validator}
            ),
        ):
            script_gen_bench.main(repo_path=str(tmp_path), task="SE-01")

        assert processed_ids == ["SE-01"]

    def test_main_exits_when_task_id_not_found(
        self, script_gen_bench: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exit 1 when ``--task <id>`` does not match any task in file.

        Scenario: user passes a non-existent task ID.
        """
        tasks_file = tmp_path / "tasks.json"
        tasks_file.write_text(
            json.dumps(
                {
                    "repo": {},
                    "tasks": [{"id": "SE-01", "type": "symbol_extraction", "ground_truth": {}}],
                }
            )
        )
        monkeypatch.setattr(script_gen_bench, "TASKS_FILE", tasks_file)

        fake_sq = tmp_path / "scan-query"
        fake_sq.write_text("#!/bin/sh")
        fake_index = tmp_path / "index.json"
        fake_index.write_text("{}")

        with (
            patch.object(script_gen_bench, "find_codemap_bin", return_value=fake_sq),
            patch.object(script_gen_bench, "resolve_index_path", return_value=fake_index),
        ):
            with pytest.raises(SystemExit) as exc_info:
                script_gen_bench.main(repo_path=str(tmp_path), task="NO-SUCH")
        assert exc_info.value.code == 1

    def test_main_full_file_update_writes_back(
        self, script_gen_bench: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """Regression: full-file ``--update`` (no ``--task``) processes every task and writes GT back.

        The loop variable must not shadow the ``task`` filter parameter. If it did, the ``if task is None`` write-back
        guard would never fire and the file would never be written despite ``--update``.
        """
        tasks_file = tmp_path / "tasks.json"
        tasks_file.write_text(
            json.dumps(
                {
                    "repo": {},
                    "tasks": [
                        {
                            "id": "A-01",
                            "type": "symbol_extraction",
                            "ground_truth": {},
                            "expected_queries": [{"cmd": "symbol", "args": ["first"]}],
                        },
                        {
                            "id": "A-02",
                            "type": "symbol_extraction",
                            "ground_truth": {},
                            "expected_queries": [{"cmd": "symbol", "args": ["second"]}],
                        },
                    ],
                }
            )
        )
        monkeypatch.setattr(script_gen_bench, "TASKS_FILE", tasks_file)

        fake_sq = tmp_path / "scan-query"
        fake_sq.write_text("#!/bin/sh")
        fake_index = tmp_path / "index.json"
        fake_index.write_text("{}")

        processed: list[str] = []

        def _tracking_validator(task: dict, sq: Any, index: Any, repo: Any) -> tuple:
            """Record the validated task identifier and accept its fixture contract."""
            processed.append(task["id"])
            return True, {}, ""

        with (
            patch.object(script_gen_bench, "find_codemap_bin", return_value=fake_sq),
            patch.object(script_gen_bench, "resolve_index_path", return_value=fake_index),
            patch.object(
                script_gen_bench, "VALIDATORS", {script_gen_bench.TaskType.SYMBOL_EXTRACTION: _tracking_validator}
            ),
        ):
            script_gen_bench.main(repo_path=str(tmp_path), update=True)

        assert processed == ["A-01", "A-02"]  # loop iterated all tasks
        assert "Wrote updated ground truth" in capsys.readouterr().out  # write-back guard fired

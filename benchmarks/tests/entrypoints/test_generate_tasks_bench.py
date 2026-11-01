"""Tests for benchmarks/generate-tasks-bench.py.

Covers the per-type validators, ground-truth comparison, task-type routing, the independent AST oracles, update gating
and ``main`` orchestration. Scan-query subprocess calls are mocked at the ``run_scan_query`` boundary; one real ``scan-
query --help`` call and one real ``run_scan_query`` subprocess round trip prove the boundary itself.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from collections.abc import Callable
from enum import Enum
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

_MODULE = "generate_tasks_bench"
_RUN_SCAN_QUERY = f"{_MODULE}.run_scan_query"
_RV_AST_VALUE = f"{_MODULE}._rv_ast_value"
_REAL_JSON_DUMP = json.dump
#: Stand-in scan-query launcher path; validators only forward it to the (mocked) ``run_scan_query``.
_SQ = Path("scan-query")


def _write_files(root: Path, files: dict[str, str]) -> None:
    """Write each relative-path source in ``files`` under ``root``, creating parent directories."""
    for relative_path, text in files.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def _caller_files(callers: list[str], target: str) -> dict[str, str]:
    """Return sources whose scopes call ``target()`` so the AST oracle discovers exactly ``callers``.

    Each caller string is ``<module>::<scope>`` where scope is ``func`` or ``Class.method``; the source is placed at
    ``<module-as-path>.py``.
    """
    files = {}
    for caller in callers:
        module, scope = caller.split("::")
        if "." in scope:
            cls, meth = scope.split(".", 1)
            src = f"class {cls}:\n    def {meth}(self):\n        {target}()\n"
        else:
            src = f"def {scope}():\n    {target}()\n"
        files[module.replace(".", "/") + ".py"] = src
    return files


def _symbol_task(module: str, qualified_name: str, start: int, end: int) -> dict[str, Any]:
    """Build a minimal symbol_extraction task."""
    ground_truth = {"module": module, "qualified_name": qualified_name, "start_line": start, "end_line": end}
    return {"type": "symbol_extraction", "ground_truth": ground_truth}


def _fn_task(
    primary_fn: str, callers: list[str], unique_count: int, raw_count: int, exclude_tests: bool = False
) -> dict[str, Any]:
    """Build a minimal fn_call_graph task."""
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


def _fn_scan_response(callers: list[str], count: int) -> dict[str, Any]:
    """Build a minimal fn-rdeps scan-query response."""
    return {"called_by": [{"caller": c} for c in callers], "count": count}


def _rv_task(sub_questions: list[Any], expected_queries: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Build a minimal review_assistance task; pass ``[]`` as ``expected_queries`` for the empty-query path."""
    if expected_queries is None:
        expected_queries = [{"cmd": "fn-rdeps", "args": ["mod::fn"]}]
    return {"type": "review_assistance", "sub_questions": sub_questions, "expected_queries": expected_queries}


def _rv_count_question(count: int) -> dict[str, Any]:
    """Build one integer_extract review sub-question."""
    return {"id": "sq1", "match": "integer_extract", "ground_truth": {"count": count}}


def _rv_symbols_task(symbols: list[str]) -> dict[str, Any]:
    """Build a review task asking for the ``undocumented`` symbol set."""
    question = {"id": "sq2", "match": "symbol_name_set", "ground_truth": {"symbols": symbols}}
    return _rv_task([question], expected_queries=[{"cmd": "undocumented", "args": []}])


def _undocumented_task(count: int, syms: list[str]) -> dict[str, Any]:
    """Build a code_quality task with check='undocumented'."""
    return {
        "type": "code_quality",
        "expected_queries": [{"cmd": "undocumented", "args": []}],
        "ground_truth": {"check": "undocumented", "undocumented_count": count, "undocumented_symbols": syms},
    }


def _uncovered_task(count: int, syms: list[str], args: tuple[str, ...] = ()) -> dict[str, Any]:
    """Build a code_quality task with check='uncovered'."""
    return {
        "type": "code_quality",
        "expected_queries": [{"cmd": "uncovered", "args": list(args)}],
        "ground_truth": {"check": "uncovered", "uncovered_count": count, "uncovered_symbols": syms},
    }


def _coupled_task(top_module: str, dep_count: int, internal: int) -> dict[str, Any]:
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


#: Five-row coupled oracle whose ranking metric (``internal_dep_count``) differs from its display metric.
_COUPLED_RANKING = [
    {"name": "pkg.alpha", "dep_count": 9, "internal_dep_count": 5},
    {"name": "pkg.bravo", "dep_count": 8, "internal_dep_count": 4},
    {"name": "pkg.charlie", "dep_count": 7, "internal_dep_count": 3},
    {"name": "pkg.delta", "dep_count": 6, "internal_dep_count": 2},
    {"name": "pkg.echo", "dep_count": 5, "internal_dep_count": 1},
]


def _coupled_ranking_task(ranking: list[dict[str, int | str]]) -> dict[str, Any]:
    """Build a CQ-03-style coupled task with its complete ordered oracle.

    ``coupled`` ranks by ``internal_dep_count``.  The benchmark prompt asks for all five displayed module names and
    their ``dep_count`` values, so ground truth must retain every row rather than treating rank one as a proxy for the
    requested ranking.
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


def _xrefs_task(broken_count: int, targets: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a code_quality task with check='xrefs_broken'."""
    return {
        "type": "code_quality",
        "expected_queries": [{"cmd": "xrefs", "args": []}],
        "ground_truth": {"check": "xrefs_broken", "broken_count": broken_count, "broken_targets": targets},
    }


def _undocumented_files(names: list[str]) -> dict[str, str]:
    """Return a module whose public classes ``names`` all lack docstrings.

    The AST undocumented oracle (authoritative) then reports exactly ``names`` as the undocumented public symbols.
    """
    return {"m.py": "\n\n".join(f"class {n}:\n    pass" for n in names) + "\n"}


def _uncovered_files(uncovered: list[str], covered: list[str]) -> dict[str, str]:
    """Return a module of public functions where a test file calls only ``covered``.

    The AST uncovered oracle (authoritative) then reports exactly ``uncovered`` — the functions no test module calls.
    """
    body = "\n\n".join(f"def {n}():\n    pass" for n in [*uncovered, *covered]) + "\n"
    calls = "\n".join(f"    {n}()" for n in covered) or "    pass"
    return {"m.py": body, "tests/test_m.py": f"def test_all():\n{calls}\n"}


def _payload_by_command(payloads: dict[str, Any]) -> Callable[..., Any]:
    """Return a ``run_scan_query`` side effect that answers each query with the payload for its sub-command."""

    def _respond(_sq: Any, args: list[str], _index: Any, _repo: Any) -> Any:
        """Answer a scan-query call with the payload registered for ``args[0]``, or ``None`` when unknown."""
        return payloads.get(args[0])

    return _respond


def _completed(returncode: int, stdout: str) -> subprocess.CompletedProcess[str]:
    """Build the result object ``subprocess.run`` returns."""
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


_DEBUG_ANCHOR_SOURCE = "class Anchor:\n    def locate(self):\n        pass\n"


def _debug_task(ground_truth: dict[str, Any]) -> dict[str, Any]:
    """Build a debug_from_trace task anchored on ``Anchor.locate`` in ``pkg.mod``."""
    return {
        "type": "debug_from_trace",
        "primary_module": "pkg.mod",
        "symbol_name": "Anchor.locate",
        "ground_truth": ground_truth,
    }


def _feature_task(entry_point: str) -> dict[str, Any]:
    """Build a feature_scaffolding task whose extension point lives in ``pkg.feature``."""
    return {
        "type": "feature_scaffolding",
        "primary_module": "pkg.feature",
        "ground_truth": {"primary_file": "src/pkg/feature.py", "entry_point": entry_point},
    }


def _real_issue_task(scoreable: bool, files_changed: list[str]) -> dict[str, Any]:
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


@pytest.fixture
def write_tasks(script_gen_bench: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., Path]:
    """Return a writer that stores a task manifest under ``tmp_path`` and redirects ``TASKS_FILE`` to it."""

    def _write(tasks: list[dict[str, Any]], repo: dict[str, Any] | None = None) -> Path:
        """Write ``{"repo": ..., "tasks": ...}`` as ``tasks.json`` and redirect ``TASKS_FILE`` to it."""
        tasks_file = tmp_path / "tasks.json"
        tasks_file.write_text(json.dumps({"repo": repo or {}, "tasks": tasks}))
        monkeypatch.setattr(script_gen_bench, "TASKS_FILE", tasks_file)
        return tasks_file

    return _write


@pytest.fixture
def fake_discovery(script_gen_bench: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Stub scan-query and index discovery with files under ``tmp_path``; return ``(scan_query, index)``."""
    fake_sq = tmp_path / "scan-query"
    fake_sq.write_text("#!/bin/sh")
    fake_index = tmp_path / "index.json"
    fake_index.write_text("{}")
    find_bin = mock.create_autospec(script_gen_bench.find_codemap_bin, return_value=fake_sq)
    resolve_index = mock.create_autospec(script_gen_bench.resolve_index_path, return_value=fake_index)
    monkeypatch.setattr(script_gen_bench, "find_codemap_bin", find_bin)
    monkeypatch.setattr(script_gen_bench, "resolve_index_path", resolve_index)
    return fake_sq, fake_index


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
        cannot be launched directly. Read positional choices so option values such as ``--format {json,tsv}`` cannot be
        mistaken for query commands. This is the real-subprocess check behind the mocked ``run_scan_query`` tests.
        """
        launcher = script_gen_bench.git_toplevel() / "plugins" / "codemap-py" / "bin" / "scan-query"
        result = subprocess.run([sys.executable, str(launcher), "--help"], capture_output=True, text=True, check=True)
        match = re.search(r"positional arguments:\s+\{([^}]+)\}", result.stdout)

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
    def test_rejects_unrepresentable_execution_task(
        self, script_gen_bench: Any, task: dict[str, Any], expected_error: str
    ) -> None:
        """Malformed execution metadata fails before any benchmark coordinate runs.

        Scenario: each task omits, mistypes or mis-declares its query list or policy; the contract check reports one
        precise error naming the task.
        """
        assert script_gen_bench._expected_query_contract_errors([task]) == [expected_error]

    @pytest.mark.parametrize("task_id", ["DI-01", "DI-02", "DI-03", "DI-04", "DI-05", "DI-06"])
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
        assert {"cmd": "fn-rdeps", "args": [task["primary_fn"], "--exclude-tests"]} in task["expected_queries"]
        assert {"cmd": "rdeps", "args": [task["primary_module"]]} in task["expected_queries"]


class TestFindCodemapBin:
    """Resolve a codemap binary by PATH lookup, then by the plugin-dir fallback."""

    @mock.patch("shutil.which", autospec=True, return_value=None)
    @pytest.mark.parametrize(
        "files",
        [
            pytest.param({}, id="empty-plugin-root"),
            pytest.param({"plugins/codemap-py/bin/.keep": ""}, id="bin-dir-without-binary"),
            pytest.param({"plugins/codemap/bin/scan-query": "#!/bin/sh"}, id="retired-codemap-layout"),
        ],
    )
    def test_returns_none_when_binary_is_unresolvable(
        self, mock_which: mock.Mock, script_gen_bench: Any, tmp_path: Path, files: dict[str, str]
    ) -> None:
        """Return None when the binary is neither on PATH nor in ``plugins/codemap-py/bin``.

        Scenario: a clean environment, a partial plugin install whose bin/ holds no binary, and a stale checkout where
        only the retired ``codemap`` folder holds one. The resolver targets ``codemap-py`` exclusively, so none may
        yield a false-green path.
        """
        _write_files(tmp_path, files)

        assert script_gen_bench.find_codemap_bin("scan-query", tmp_path) is None

    @mock.patch("shutil.which", autospec=True, return_value=None)
    def test_returns_none_without_plugin_root(self, mock_which: mock.Mock, script_gen_bench: Any) -> None:
        """Return None when ``plugin_root`` is omitted and the PATH lookup fails.

        Scenario: caller supplies no plugin root, so no fallback directory exists to search.
        """
        assert script_gen_bench.find_codemap_bin("scan-query") is None

    @mock.patch("shutil.which", autospec=True, return_value="/usr/local/bin/scan-query")
    def test_returns_path_when_on_path(self, mock_which: mock.Mock, script_gen_bench: Any) -> None:
        """Return a Path object when the binary is found via ``shutil.which``.

        Scenario: binary installed globally; the PATH lookup succeeds.
        """
        assert script_gen_bench.find_codemap_bin("scan-query") == Path("/usr/local/bin/scan-query")

    @mock.patch("shutil.which", autospec=True, return_value=None)
    def test_falls_back_to_plugin_dir(self, mock_which: mock.Mock, script_gen_bench: Any, tmp_path: Path) -> None:
        """Fall back to ``plugins/codemap-py/bin/<name>`` when the PATH lookup fails.

        Scenario: binary not on PATH but present in the plugin directory structure.
        """
        _write_files(tmp_path, {"plugins/codemap-py/bin/scan-query": "#!/bin/sh"})

        result = script_gen_bench.find_codemap_bin("scan-query", tmp_path)

        assert result == tmp_path / "plugins" / "codemap-py" / "bin" / "scan-query"


class TestResolveIndexPath:
    """Return the explicit argument as-is; otherwise discover the index file."""

    def test_returns_explicit_arg_unchanged(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Return the explicit ``--index-path`` argument verbatim as a Path.

        Scenario: user passes ``--index-path``; resolution must not alter it.
        """
        explicit = "/some/custom/index.json"

        assert script_gen_bench.resolve_index_path(explicit, tmp_path) == Path(explicit)

    @pytest.mark.parametrize("cache_layout", [pytest.param("codemap", id="cache-codemap"), "scan"])
    def test_finds_index_in_cache_layout(self, script_gen_bench: Any, tmp_path: Path, cache_layout: str) -> None:
        """Discover the index JSON at ``<repo>/.cache/<layout>/<name>.json``.

        Scenario: the default layout builds the index into .cache/codemap/; an older layout uses .cache/scan/ and is
        found when .cache/codemap/<name>.json is absent.
        """
        index_file = tmp_path / ".cache" / cache_layout / f"{tmp_path.name}.json"
        _write_files(tmp_path, {str(index_file.relative_to(tmp_path)): "{}"})

        assert script_gen_bench.resolve_index_path(None, tmp_path) == index_file

    def test_strips_master_suffix_from_repo_name(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Strip the ``-master`` suffix when looking up the index file.

        Scenario: repo cloned as pytorch-lightning-master; the index is stored as pytorch-lightning.json.
        """
        repo_dir = tmp_path / "myrepo-master"
        _write_files(repo_dir, {".cache/codemap/myrepo.json": "{}"})

        result = script_gen_bench.resolve_index_path(None, repo_dir)

        assert result == repo_dir / ".cache" / "codemap" / "myrepo.json"

    def test_falls_back_to_first_json_glob(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Fall back to the first JSON found via glob when the name-based lookup fails.

        Scenario: index named differently from the repo; the glob returns the only match.
        """
        _write_files(tmp_path, {".cache/codemap/other-name.json": "{}"})

        result = script_gen_bench.resolve_index_path(None, tmp_path)

        assert result == tmp_path / ".cache" / "codemap" / "other-name.json"

    def test_returns_fallback_path_when_no_index_found(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Return a constructed path (that may not exist) when no index is found.

        Scenario: empty repo with no .cache/ directory; a computed fallback under .cache/ is returned.
        """
        result = script_gen_bench.resolve_index_path(None, tmp_path)

        assert isinstance(result, Path)
        assert ".cache" in result.parts


class TestRunScanQuery:
    """Subprocess wrapper returns the parsed dict, or None on failure."""

    @mock.patch("subprocess.run", autospec=True)
    def test_returns_dict_on_success(self, mock_run: mock.Mock, script_gen_bench: Any, tmp_path: Path) -> None:
        """Return the parsed JSON dict when the subprocess exits 0 with valid JSON.

        Scenario: happy path; scan-query returns well-formed JSON.
        """
        payload = {"symbols": [{"qualified_name": "Foo.bar"}]}
        mock_run.return_value = _completed(0, json.dumps(payload))

        result = script_gen_bench.run_scan_query(_SQ, ["symbol", "Foo.bar"], tmp_path / "index.json", tmp_path)

        assert result == payload

    @mock.patch("subprocess.run", autospec=True)
    @pytest.mark.parametrize(
        ("returncode", "stdout"),
        [pytest.param(1, "", id="nonzero-exit"), pytest.param(0, "not json output", id="json-decode-error")],
    )
    def test_returns_none_on_unusable_process_result(
        self, mock_run: mock.Mock, script_gen_bench: Any, tmp_path: Path, returncode: int, stdout: str
    ) -> None:
        """Return None when the subprocess exits non-zero or prints output that is not valid JSON.

        Scenario: scan-query fails (e.g., index not found) or prints a human-readable error so the JSON parse fails;
        the caller gets None.
        """
        mock_run.return_value = _completed(returncode, stdout)

        result = script_gen_bench.run_scan_query(_SQ, ["symbol", "X"], tmp_path / "index.json", tmp_path)

        assert result is None

    @mock.patch("subprocess.run", autospec=True)
    @pytest.mark.parametrize(
        "failure",
        [
            pytest.param(subprocess.TimeoutExpired(cmd="sq", timeout=30), id="timeout"),
            pytest.param(OSError("no such file"), id="os-error"),
        ],
    )
    def test_returns_none_when_subprocess_cannot_complete(
        self, mock_run: mock.Mock, script_gen_bench: Any, tmp_path: Path, failure: BaseException
    ) -> None:
        """Return None when the subprocess times out or the binary cannot be executed.

        Scenario: scan-query hangs (TimeoutExpired) or its path does not exist or permissions are denied (OSError);
        both are caught and swallowed.
        """
        mock_run.side_effect = failure

        result = script_gen_bench.run_scan_query(_SQ, ["symbol", "X"], tmp_path / "index.json", tmp_path)

        assert result is None

    def test_parses_stdout_of_real_subprocess(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Run a real launcher process and parse the JSON it prints.

        Scenario: a tiny Python script stands in for scan-query and echoes its arguments as JSON; this proves the
        ``sys.executable`` launch, ``--index`` forwarding and stdout parsing end to end without the real tool.
        """
        launcher = tmp_path / "scan-query"
        launcher.write_text("import json, sys\nprint(json.dumps({'argv': sys.argv[1:]}))\n")
        index = tmp_path / "index.json"

        result = script_gen_bench.run_scan_query(launcher, ["symbol", "Foo.bar"], index, tmp_path)

        assert result == {"argv": ["--index", str(index.resolve()), "symbol", "Foo.bar"]}


class TestValidateSymbol:
    """Symbol_extraction validator compares scan-query output to ground truth."""

    @mock.patch(_RUN_SCAN_QUERY, autospec=True)
    def test_passes_when_all_fields_match(self, mock_scan: mock.Mock, script_gen_bench: Any, tmp_path: Path) -> None:
        """Return (True, live_gt, '') when the scan-query result matches ground truth exactly.

        Scenario: symbol found with correct module, qualified name, start_line and end_line.
        """
        task = _symbol_task("pkg.mod", "MyClass.method", 10, 20)
        live = {"module": "pkg.mod", "qualified_name": "MyClass.method", "start_line": 10, "end_line": 20}
        mock_scan.return_value = {"symbols": [live]}

        ok, live_gt, reason = script_gen_bench._validate_symbol(task, _SQ, tmp_path / "idx.json", tmp_path)

        assert (ok, reason) == (True, "")
        assert live_gt["start_line"] == 10
        assert live_gt["end_line"] == 20

    @mock.patch(_RUN_SCAN_QUERY, autospec=True, return_value={"symbols": []})
    def test_fails_when_symbol_not_found(self, mock_scan: mock.Mock, script_gen_bench: Any, tmp_path: Path) -> None:
        """Return (False, None, reason) when the symbol is absent from the scan-query results.

        Scenario: symbol renamed; the query returns an empty symbols list.
        """
        task = _symbol_task("pkg.mod", "MissingFn", 1, 5)

        ok, live_gt, reason = script_gen_bench._validate_symbol(task, _SQ, tmp_path / "idx.json", tmp_path)

        assert ok is False
        assert live_gt is None
        assert "not found" in reason

    @mock.patch(_RUN_SCAN_QUERY, autospec=True, return_value=None)
    def test_fails_when_scan_query_fails(self, mock_scan: mock.Mock, script_gen_bench: Any, tmp_path: Path) -> None:
        """Report validation failure when the structural query fails.

        Scenario: the scan-query binary errors, so the wrapper returns None; validation must fail without live data.
        """
        task = _symbol_task("pkg.mod", "MyClass.method", 1, 5)

        ok, live_gt, _reason = script_gen_bench._validate_symbol(task, _SQ, tmp_path / "idx.json", tmp_path)

        assert ok is False
        assert live_gt is None

    @mock.patch(_RUN_SCAN_QUERY, autospec=True)
    def test_widens_to_qname_match_when_module_differs(
        self, mock_scan: mock.Mock, script_gen_bench: Any, tmp_path: Path
    ) -> None:
        """Fall back to matching on qualified_name alone when the module differs.

        Scenario: symbol moved to a different module but the qualified name is preserved; the documented behaviour is
        to widen to any symbol with the right qualified name, so live ground truth is still returned.
        """
        task = _symbol_task("pkg.old_mod", "MyClass.method", 10, 20)
        moved = {"module": "pkg.new_mod", "qualified_name": "MyClass.method", "start_line": 10, "end_line": 20}
        mock_scan.return_value = {"symbols": [moved]}

        _ok, live_gt, _reason = script_gen_bench._validate_symbol(task, _SQ, tmp_path / "idx.json", tmp_path)

        assert live_gt is not None

    @mock.patch(_RUN_SCAN_QUERY, autospec=True)
    @pytest.mark.parametrize(
        ("gt_field", "live_value"),
        [
            pytest.param("start_line", 99, id="start_line"),
            pytest.param("end_line", 99, id="end_line"),
            pytest.param("module", "wrong.mod", id="module-via-widened-qname-match"),
        ],
    )
    def test_reports_specific_field_mismatch(
        self, mock_scan: mock.Mock, script_gen_bench: Any, tmp_path: Path, gt_field: str, live_value: Any
    ) -> None:
        """Name the specific field that differs from ground truth in the failure reason.

        Scenario: each ground-truth field individually diverges; the symbol IS found (qualified name matches) but the
        stored field value differs from live, so the reason must identify the offending field by name. The qualified
        name itself cannot be tested this way — if it differs the symbol is not found at all.
        """
        base = {"module": "pkg.mod", "qualified_name": "Cls.fn", "start_line": 10, "end_line": 20}
        task = {"type": "symbol_extraction", "ground_truth": dict(base)}
        mock_scan.return_value = {"symbols": [{**base, gt_field: live_value}]}

        ok, _, reason = script_gen_bench._validate_symbol(task, _SQ, tmp_path / "idx.json", tmp_path)

        assert ok is False
        assert gt_field in reason


class TestValidateFn:
    """Fn_call_graph validator checks caller counts and sets."""

    @mock.patch(_RUN_SCAN_QUERY, autospec=True)
    def test_passes_when_callers_match_exactly(
        self, mock_scan: mock.Mock, script_gen_bench: Any, tmp_path: Path
    ) -> None:
        """Return (True, live_gt, '') when the AST oracle's callers match ground truth.

        Scenario: unchanged codebase; the independent AST oracle (authoritative) agrees with the stored caller set.
        """
        callers = ["mod.a::fn_a", "mod.b::fn_b"]
        task = _fn_task("mod::target", callers, 2, 2)
        mock_scan.return_value = _fn_scan_response(callers, 2)
        _write_files(tmp_path, _caller_files(callers, "target"))

        ok, _live_gt, reason = script_gen_bench._validate_fn(task, _SQ, tmp_path / "idx.json", tmp_path)

        assert (ok, reason) == (True, "")

    @mock.patch(_RUN_SCAN_QUERY, autospec=True)
    @pytest.mark.parametrize(
        ("gt_callers", "oracle_callers", "expected_fragment"),
        [
            pytest.param(["mod.a::fn_a"], ["mod.a::fn_a", "mod.b::fn_b"], "extra", id="extra-caller"),
            pytest.param(["mod.a::fn_a", "mod.b::fn_b"], ["mod.a::fn_a"], "missing", id="missing-caller"),
        ],
    )
    def test_fails_when_oracle_callers_diverge_from_ground_truth(
        self,
        mock_scan: mock.Mock,
        script_gen_bench: Any,
        tmp_path: Path,
        gt_callers: list[str],
        oracle_callers: list[str],
        expected_fragment: str,
    ) -> None:
        """Return failure when the AST oracle finds a caller not in ground truth or lacks a stored caller.

        Scenario: a new function calls target so ground truth is stale (extra caller), or a caller was refactored away
        so the oracle no longer sees it in source (missing caller).
        """
        task = _fn_task("mod::target", gt_callers, len(gt_callers), len(gt_callers))
        mock_scan.return_value = _fn_scan_response(oracle_callers, len(oracle_callers))
        _write_files(tmp_path, _caller_files(oracle_callers, "target"))

        ok, _, reason = script_gen_bench._validate_fn(task, _SQ, tmp_path / "idx.json", tmp_path)

        assert ok is False
        assert expected_fragment in reason

    @mock.patch(_RUN_SCAN_QUERY, autospec=True, return_value=_fn_scan_response(["mod.a::fn_a"], 1))
    def test_fails_on_count_mismatch(self, mock_scan: mock.Mock, script_gen_bench: Any, tmp_path: Path) -> None:
        """Return failure when unique_caller_count differs from ground truth.

        Scenario: the stored count field diverges even though the caller set comparison matches.
        """
        task = _fn_task("mod::target", ["mod.a::fn_a"], 99, 1)  # wrong expected unique_count

        ok, _, reason = script_gen_bench._validate_fn(task, _SQ, tmp_path / "idx.json", tmp_path)

        assert ok is False
        assert "unique_caller_count" in reason

    @mock.patch(_RUN_SCAN_QUERY, autospec=True, return_value=None)
    def test_fails_when_scan_query_fails(self, mock_scan: mock.Mock, script_gen_bench: Any, tmp_path: Path) -> None:
        """Return (False, None, reason) when the scan-query call fails.

        Scenario: binary not available; the subprocess wrapper returns None.
        """
        task = _fn_task("mod::fn", [], 0, 0)

        ok, live_gt, _reason = script_gen_bench._validate_fn(task, _SQ, tmp_path / "idx.json", tmp_path)

        assert ok is False
        assert live_gt is None

    @mock.patch(_RUN_SCAN_QUERY, autospec=True, return_value={"called_by": [], "count": 0})
    def test_forwards_exclude_tests_flag(self, mock_scan: mock.Mock, script_gen_bench: Any, tmp_path: Path) -> None:
        """Append the ``--exclude-tests`` flag when ground_truth.exclude_tests is True.

        Scenario: the task is configured to exclude test-file callers; the flag must be forwarded to scan-query.
        """
        task = _fn_task("mod::fn", [], 0, 0, exclude_tests=True)

        script_gen_bench._validate_fn(task, _SQ, tmp_path / "idx.json", tmp_path)

        assert "--exclude-tests" in mock_scan.call_args.args[1]

    @mock.patch(_RUN_SCAN_QUERY, autospec=True)
    def test_deduplicates_scan_callers_in_diagnostic(
        self, mock_scan: mock.Mock, script_gen_bench: Any, tmp_path: Path
    ) -> None:
        """Deduplicate repeated scan-query callers in the diagnostic scan_caller_count.

        Scenario: scan-query returns the same caller twice (raw > unique). The authoritative unique_caller_count comes
        from the AST oracle; the scan_caller_count diagnostic must still deduplicate the tool's output.
        """
        task = _fn_task("mod::target", ["mod.a::fn_a"], 1, 2)
        mock_scan.return_value = _fn_scan_response(["mod.a::fn_a", "mod.a::fn_a"], 2)  # duplicate scan output
        _write_files(tmp_path, _caller_files(["mod.a::fn_a"], "target"))

        _ok, live_gt, _ = script_gen_bench._validate_fn(task, _SQ, tmp_path / "idx.json", tmp_path)

        assert live_gt["unique_caller_count"] == 1  # AST oracle (authoritative)
        assert live_gt["scan_caller_count"] == 1  # scan output deduplicated


class TestExtractRvValue:
    """Extract the right value per (cmd, match_type) combination."""

    @pytest.mark.parametrize(
        ("cmd", "data", "expected"),
        [
            pytest.param("rdeps", {"imported_by": ["a", "b", "c"]}, 3, id="rdeps-counts-importers"),
            pytest.param("rdeps", {"imported_by": []}, 0, id="rdeps-no-importers"),
            pytest.param("fn-rdeps", {"count": 7, "called_by": []}, 7, id="fn-rdeps-count-key"),
            pytest.param("fn-rdeps", {"called_by": [1, 2]}, 2, id="fn-rdeps-called-by-length-fallback"),
            pytest.param("undocumented", {"total": 12}, 12, id="undocumented-total"),
            pytest.param("uncovered", {"total": 5}, 5, id="uncovered-total"),
            pytest.param("unknown-cmd", {}, 0, id="unknown-cmd-is-zero"),
        ],
    )
    def test_integer_extract(self, script_gen_bench: Any, cmd: str, data: dict, expected: int) -> None:
        """Return the integer count from scan-query output for the integer_extract match type.

        Scenario: each supported subcommand reports its count under a different key; an unknown cmd falls through to 0.
        """
        assert script_gen_bench._extract_rv_value(cmd, data, "integer_extract") == expected

    @pytest.mark.parametrize(
        ("cmd", "data", "count_hint", "expected"),
        [
            pytest.param(
                "undocumented",
                {"undocumented": [{"qualified_name": "A"}, {"qualified_name": "B"}]},
                0,
                ["A", "B"],
                id="undocumented-names",
            ),
            pytest.param(
                "undocumented",
                {"undocumented": [{"qualified_name": "A"}, {"qualified_name": "B"}, {"qualified_name": "C"}]},
                2,
                ["A", "B"],
                id="undocumented-truncated-to-count-hint",
            ),
            pytest.param("uncovered", {"uncovered": [{"qualified_name": "X"}]}, 0, ["X"], id="uncovered-names"),
            pytest.param("unknown-cmd", {"anything": []}, 0, [], id="unknown-cmd-is-empty"),
        ],
    )
    def test_symbol_name_set(
        self, script_gen_bench: Any, cmd: str, data: dict, count_hint: int, expected: list
    ) -> None:
        """Return the qualified_name list for the symbol_name_set match type.

        Scenario: undocumented and uncovered commands return symbol names; count_hint truncates the list; an unknown
        cmd returns an empty list.
        """
        result = script_gen_bench._extract_rv_value(cmd, data, "symbol_name_set", count_hint=count_hint)

        assert result == expected

    def test_entry_falls_back_to_name_key(self, script_gen_bench: Any) -> None:
        """Use the 'name' key when 'qualified_name' is absent from an entry dict.

        Scenario: some scan-query versions emit 'name' instead of 'qualified_name'; the fallback must be used.
        """
        data = {"undocumented": [{"name": "fallback_fn"}]}

        assert script_gen_bench._extract_rv_value("undocumented", data, "symbol_name_set") == ["fallback_fn"]


class TestValidateRv:
    """Review_assistance validator checks sub-question ground truth."""

    @mock.patch(_RUN_SCAN_QUERY, autospec=True, return_value={"count": 3, "called_by": []})
    @mock.patch(_RV_AST_VALUE, autospec=True, return_value=(3, True, None))
    def test_preserves_index_path_after_enumerating_review_subquestions(
        self, mock_ast: mock.Mock, mock_scan: mock.Mock, script_gen_bench: Any, tmp_path: Path
    ) -> None:
        """The scan-query call receives its `Path`, never a sub-question ordinal.

        Prevents the deterministic full-suite crash where `_validate_rv` shadows its `index` parameter with
        `enumerate(..., start=1)`.
        """
        index_path = tmp_path / "idx.json"
        task = _rv_task([_rv_count_question(3)])

        ok, _live_gt, reason = script_gen_bench._validate_rv(task, _SQ, index_path, tmp_path)

        assert (ok, reason) == (True, "")
        assert mock_scan.call_args.args[2] == index_path

    @mock.patch(_RUN_SCAN_QUERY, autospec=True, return_value={"count": 7, "called_by": []})
    @mock.patch(_RV_AST_VALUE, autospec=True, return_value=(7, True, None))
    def test_fails_when_integer_count_differs(
        self, mock_ast: mock.Mock, mock_scan: mock.Mock, script_gen_bench: Any, tmp_path: Path
    ) -> None:
        """Return a failure reason naming the sub-question id when the count differs.

        Scenario: a refactor changed the call graph; the count in ground truth (10) is now stale against live (7).
        """
        task = _rv_task([_rv_count_question(10)])

        ok, _, reason = script_gen_bench._validate_rv(task, _SQ, tmp_path / "idx.json", tmp_path)

        assert ok is False
        assert "sq1" in reason

    @mock.patch(_RUN_SCAN_QUERY, autospec=True)
    @mock.patch(_RV_AST_VALUE, autospec=True)
    @pytest.mark.parametrize(
        ("live_symbols", "expected_ok", "reason_fragment"),
        [
            pytest.param(["A", "B"], True, "", id="matches-ground-truth"),
            pytest.param(["A"], False, "missing", id="missing-symbol"),
        ],
    )
    def test_validates_symbol_set_against_ground_truth(
        self,
        mock_ast: mock.Mock,
        mock_scan: mock.Mock,
        script_gen_bench: Any,
        tmp_path: Path,
        live_symbols: list[str],
        expected_ok: bool,
        reason_fragment: str,
    ) -> None:
        """Pass a symbol_name_set sub-question that matches ground truth and describe missing symbols otherwise.

        Scenario: ground truth expects ['A', 'B']; live returns both (pass) or only ['A'] (failure naming 'missing').
        """
        task = _rv_symbols_task(["A", "B"])
        mock_ast.return_value = (live_symbols, True, None)
        mock_scan.return_value = {"undocumented": [{"qualified_name": name} for name in live_symbols]}

        ok, _, reason = script_gen_bench._validate_rv(task, _SQ, tmp_path / "idx.json", tmp_path)

        assert ok is expected_ok
        assert reason_fragment in reason

    def test_fails_when_expected_queries_empty(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Return (False, None, reason) when the expected_queries list is empty.

        Scenario: malformed task with no query defined; validation must fail before any query runs.
        """
        task = _rv_task([], expected_queries=[])

        ok, live_gt, reason = script_gen_bench._validate_rv(task, _SQ, tmp_path / "idx.json", tmp_path)

        assert ok is False
        assert live_gt is None
        assert "expected_queries" in reason

    @pytest.mark.parametrize(
        "sub_questions",
        [
            pytest.param([{"id": "sq1", "match": "unknown", "ground_truth": {"count": 1}}], id="unsupported-match"),
            pytest.param(
                [_rv_count_question(1), {**_rv_count_question(2), "id": "sq2"}], id="ambiguous-multiple-counts"
            ),
            pytest.param(["not-an-object"], id="non-object-subquestion"),
        ],
    )
    def test_fails_closed_for_invalid_or_ambiguous_review_subquestions(
        self, script_gen_bench: Any, tmp_path: Path, sub_questions: list[object]
    ) -> None:
        """Generation refuses evaluator contracts that cannot produce one unambiguous answer mapping.

        Scenario: an unsupported match type, two competing count questions, or a malformed sub-question entry each
        leave no single answer mapping, so validation fails closed instead of guessing.
        """
        task = _rv_task(sub_questions)

        ok, live_gt, reason = script_gen_bench._validate_rv(task, _SQ, tmp_path / "idx.json", tmp_path)

        assert ok is False
        assert live_gt is None
        assert "review" in reason

    @mock.patch(_RUN_SCAN_QUERY, autospec=True, return_value=None)
    @mock.patch(_RV_AST_VALUE, autospec=True, return_value=(1, True, None))
    def test_passes_when_diagnostic_scan_query_fails(
        self, mock_ast: mock.Mock, mock_scan: mock.Mock, script_gen_bench: Any, tmp_path: Path
    ) -> None:
        """Keep independent validation usable when the diagnostic scan-query fails.

        Scenario: the AST derives the expected caller count while the queried tool is unavailable. The tool cannot
        become a ground-truth dependency.
        """
        task = _rv_task([_rv_count_question(1)])

        ok, live_gt, reason = script_gen_bench._validate_rv(task, _SQ, tmp_path / "idx.json", tmp_path)

        assert ok is True
        assert live_gt == {"sq1": {"count": 1}}
        assert reason == ""

    @mock.patch(_RUN_SCAN_QUERY, autospec=True, return_value={"count": 3, "called_by": []})
    @mock.patch(_RV_AST_VALUE, autospec=True, return_value=(None, False, None))
    def test_fails_closed_when_ast_source_is_unavailable(
        self, mock_ast: mock.Mock, mock_scan: mock.Mock, script_gen_bench: Any, tmp_path: Path
    ) -> None:
        """Reject a tool-derived fallback when an independent review source is absent.

        Scenario: scan-query reports a count but the AST oracle cannot answer; the tool alone must not become the
        ground truth.
        """
        task = _rv_task([_rv_count_question(3)])

        ok, live_gt, reason = script_gen_bench._validate_rv(task, _SQ, tmp_path / "idx.json", tmp_path)

        assert ok is False
        assert live_gt is None
        assert "independent AST source is unavailable" in reason


class TestValidateOss:
    """Code_quality validator routes by the ground truth ``check`` field."""

    @mock.patch(_RUN_SCAN_QUERY, autospec=True)
    @pytest.mark.parametrize(
        ("task", "payload", "files"),
        [
            pytest.param(
                _undocumented_task(2, ["A", "B"]),
                {"total": 2, "undocumented": [{"qualified_name": "A"}, {"qualified_name": "B"}]},
                _undocumented_files(["A", "B"]),
                id="undocumented",
            ),
            pytest.param(
                _uncovered_task(1, ["orphan"]),
                {"total": 1, "uncovered": [{"qualified_name": "orphan"}]},
                _uncovered_files(["orphan"], ["used"]),
                id="uncovered",
            ),
            pytest.param(
                _coupled_task("my.module", 10, 5),
                {"coupled": [{"name": "my.module", "dep_count": 10, "internal_dep_count": 5}]},
                {},
                id="coupled",
            ),
            pytest.param(
                _xrefs_task(1, [{"target": "mod::missing", "line": 2}]),
                {"broken": [{"target": "mod::missing", "line": 2}], "count": 1},
                {"mod.py": 'def f():\n    """See :func:`mod.missing`."""\n    pass\n'},
                id="xrefs",
            ),
        ],
    )
    def test_passes_when_oracle_agrees_with_ground_truth(
        self,
        mock_scan: mock.Mock,
        script_gen_bench: Any,
        tmp_path: Path,
        task: dict[str, Any],
        payload: dict[str, Any],
        files: dict[str, str],
    ) -> None:
        """Return (True, ..., '') when the check's oracle output matches the stored ground truth.

        Scenario: one case per check type. Undocumented, uncovered and xrefs are validated by the authoritative AST
        oracle over a synthetic repo (the scan payload is a coherent diagnostic); coupled compares the top entry's
        module and counts directly.
        """
        _write_files(tmp_path, files)
        mock_scan.return_value = payload

        ok, _, reason = script_gen_bench._validate_oss(task, _SQ, tmp_path / "idx.json", tmp_path)

        assert (ok, reason) == (True, "")

    @mock.patch(_RUN_SCAN_QUERY, autospec=True)
    @pytest.mark.parametrize(
        ("task", "payload", "files", "reason_fragment"),
        [
            pytest.param(
                _undocumented_task(5, ["A", "B", "C", "D", "E"]),
                {"total": 3, "undocumented": [{"qualified_name": n} for n in "ABC"]},
                _undocumented_files(["A", "B", "C"]),
                "undocumented_count",
                id="undocumented-count-shrank",
            ),
            pytest.param(
                _uncovered_task(2, ["a", "b"]),
                {"total": 2, "uncovered": [{"qualified_name": "a"}, {"qualified_name": "c"}]},
                _uncovered_files(["a"], ["b"]),  # only 'a' is uncovered; 'b' is test-referenced
                "uncovered",
                id="uncovered-symbol-now-referenced",
            ),
            pytest.param(
                _coupled_task("expected.module", 10, 5),
                {"coupled": [{"name": "actual.module", "dep_count": 10, "internal_dep_count": 5}]},
                {},
                "top_module",
                id="coupled-top-module-changed",
            ),
            pytest.param(_coupled_task("mod", 1, 0), {"coupled": []}, {}, "empty", id="coupled-empty-result"),
            pytest.param(
                _xrefs_task(1, [{"target": "mod::missing", "line": 2}]),
                {"broken": [{"target": "mod::missing", "line": 2}], "count": 1},
                {
                    "mod.py": 'def f():\n    """See :func:`mod.missing`."""\n    pass\n\n\n'
                    'def g():\n    """See :func:`mod.also_missing`."""\n    pass\n'
                },
                "broken_count",
                id="xrefs-second-broken-reference",
            ),
        ],
    )
    def test_fails_when_oracle_diverges_from_ground_truth(
        self,
        mock_scan: mock.Mock,
        script_gen_bench: Any,
        tmp_path: Path,
        task: dict[str, Any],
        payload: dict[str, Any],
        files: dict[str, str],
        reason_fragment: str,
    ) -> None:
        """Return a failure naming the diverging field when live data no longer matches ground truth.

        Scenario: symbols documented or tested after ground truth was captured, the most-coupled module changed or
        vanished, or a second unresolvable cross-reference appeared; the reason must name what diverged.
        """
        _write_files(tmp_path, files)
        mock_scan.return_value = payload

        ok, _, reason = script_gen_bench._validate_oss(task, _SQ, tmp_path / "idx.json", tmp_path)

        assert ok is False
        assert reason_fragment in reason

    @mock.patch(_RUN_SCAN_QUERY, autospec=True)
    def test_uncovered_allows_top_limited_scan_payload(
        self, mock_scan: mock.Mock, script_gen_bench: Any, tmp_path: Path
    ) -> None:
        """Keep the AST oracle authoritative when scan-query returns a capped list.

        Scenario: ``uncovered --top 1`` reports the full total but returns one displayed symbol.  The payload is
        coherent when the requested top limit equals the returned list length; rejecting it prevents the independent
        oracle from validating otherwise valid benchmark ground truth.
        """
        task = _uncovered_task(1, ["orphan"], args=("--top", "1"))
        _write_files(tmp_path, _uncovered_files(["orphan"], ["used"]))
        mock_scan.return_value = {"total": 3, "uncovered": [{"qualified_name": "orphan"}]}

        ok, live_gt, reason = script_gen_bench._validate_oss(task, _SQ, tmp_path / "idx.json", tmp_path)

        assert (ok, reason) == (True, "")
        assert live_gt["uncovered_count"] == 1
        assert live_gt["uncovered_count_scan"] == 3

    @mock.patch(_RUN_SCAN_QUERY, autospec=True)
    def test_uncovered_rejects_incomplete_top_limited_scan_payload(
        self, mock_scan: mock.Mock, script_gen_bench: Any, tmp_path: Path
    ) -> None:
        """Reject a capped response that returns fewer rows than its declared cap.

        Scenario: ``uncovered --top 2`` returns a single row while reporting a larger total, so the payload is
        internally inconsistent and cannot be trusted as a diagnostic.
        """
        task = _uncovered_task(1, ["orphan"], args=("--top", "2"))
        _write_files(tmp_path, _uncovered_files(["orphan"], ["used"]))
        mock_scan.return_value = {"total": 3, "uncovered": [{"qualified_name": "orphan"}]}

        ok, live_gt, reason = script_gen_bench._validate_oss(task, _SQ, tmp_path / "idx.json", tmp_path)

        assert ok is False
        assert live_gt is None
        assert reason == "uncovered total conflicts with symbol count"

    @mock.patch(_RUN_SCAN_QUERY, autospec=True)
    @pytest.mark.parametrize(
        "live_ranking",
        [
            pytest.param(_COUPLED_RANKING[:4], id="missing-fifth-member"),
            pytest.param(
                [
                    _COUPLED_RANKING[0],
                    {"name": "pkg.unexpected", "dep_count": 8, "internal_dep_count": 4},
                    *_COUPLED_RANKING[2:],
                ],
                id="wrong-second-member",
            ),
            pytest.param(
                [_COUPLED_RANKING[0], _COUPLED_RANKING[2], _COUPLED_RANKING[1], *_COUPLED_RANKING[3:]],
                id="second-and-third-reordered",
            ),
            pytest.param(
                [
                    _COUPLED_RANKING[0],
                    {"name": "pkg.bravo", "dep_count": 8, "internal_dep_count": 99},
                    *_COUPLED_RANKING[2:],
                ],
                id="wrong-internal-dependency-count",
            ),
        ],
    )
    def test_coupled_rejects_missing_or_misordered_top_five(
        self, mock_scan: mock.Mock, script_gen_bench: Any, tmp_path: Path, live_ranking: list[dict[str, int | str]]
    ) -> None:
        """CQ-03 validation rejects every incorrect member of the five-row ranking.

        Prevents a false-green benchmark refresh where rank one still matches but the requested top-five answer is
        missing, substituted, reordered, or has a stale ``internal_dep_count`` value.
        """
        task = _coupled_ranking_task(_COUPLED_RANKING)
        mock_scan.return_value = {"coupled": live_ranking}

        ok, live_gt, reason = script_gen_bench._validate_oss(task, _SQ, tmp_path / "idx.json", tmp_path)

        assert ok is False
        assert live_gt is not None
        assert "top_modules" in reason

    def test_fails_when_no_expected_queries(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Return (False, None, reason) for check='undocumented' with no queries.

        Scenario: malformed task whose expected_queries list is empty; there is nothing to validate against.
        """
        task = {**_undocumented_task(0, []), "expected_queries": []}

        ok, live_gt, _reason = script_gen_bench._validate_oss(task, _SQ, tmp_path / "idx.json", tmp_path)

        assert ok is False
        assert live_gt is None

    @mock.patch(_RUN_SCAN_QUERY, autospec=True, return_value=None)
    def test_fails_when_scan_query_fails(self, mock_scan: mock.Mock, script_gen_bench: Any, tmp_path: Path) -> None:
        """Report validation failure when the structural query fails.

        Scenario: binary error on any check type; the failure cascades to a failed validation with no live data.
        """
        ok, live_gt, _ = script_gen_bench._validate_oss(_undocumented_task(0, []), _SQ, tmp_path / "idx.json", tmp_path)

        assert ok is False
        assert live_gt is None

    @mock.patch(_RUN_SCAN_QUERY, autospec=True)
    @pytest.mark.parametrize(
        ("task", "payload", "reason_fragment"),
        [
            pytest.param(_undocumented_task(0, []), {"undocumented": []}, "total", id="undocumented-missing-total"),
            pytest.param(
                _uncovered_task(0, []), {"total": 0, "uncovered": "not a list"}, "list", id="uncovered-not-a-list"
            ),
            pytest.param(
                _undocumented_task(2, ["A", "B"]),
                {"total": 2, "undocumented": [{"qualified_name": "A"}]},
                "conflicts",
                id="undocumented-total-conflicts-with-list",
            ),
            pytest.param(_coupled_task("mod", 1, 0), {"coupled": "not a list"}, "not a list", id="coupled-not-a-list"),
            pytest.param(_coupled_task("mod", 1, 0), {"coupled": [1]}, "not an object", id="coupled-entry-not-object"),
            pytest.param(
                _xrefs_task(2, [{"target": "mod::Fn", "line": 1}, {"target": "mod::Other", "line": 2}]),
                {"count": 2, "broken": [{"target": "mod::Fn", "line": 1}]},
                "conflicts",
                id="xrefs-count-conflicts-with-list",
            ),
        ],
    )
    def test_malformed_scan_query_payloads_fail_cleanly(
        self,
        mock_scan: mock.Mock,
        script_gen_bench: Any,
        tmp_path: Path,
        task: dict,
        payload: dict,
        reason_fragment: str,
    ) -> None:
        """Missing keys, wrong types, and count/list conflicts return failures, not crashes.

        Scenario: scan-query emits a structurally broken payload for each check type; validation reports a reason
        naming the problem and no live ground truth.
        """
        mock_scan.return_value = payload

        ok, live_gt, reason = script_gen_bench._validate_oss(task, _SQ, tmp_path / "idx.json", tmp_path)

        assert ok is False
        assert live_gt is None
        assert reason_fragment in reason

    @mock.patch(_RUN_SCAN_QUERY, autospec=True)
    def test_combined_health_validates_both_counts(
        self, mock_scan: mock.Mock, script_gen_bench: Any, tmp_path: Path
    ) -> None:
        """Validate both the undocumented and uncovered fields for the combined_health check.

        Scenario: A and B lack docstrings (undocumented) but are test-referenced (covered); C has a docstring
        (documented) but no test reference (uncovered). Both independent AST oracle slices must match ground truth for
        an overall pass — exercises the ``oracle_views`` nesting that keeps the two slices from overwriting each other
        (see ``_attach_oracle_views``).
        """
        _write_files(
            tmp_path,
            {
                "m.py": "class A:\n    pass\n\n\nclass B:\n    pass\n\n\ndef C():\n    'Has a docstring.'\n    pass\n",
                "tests/test_m.py": "def test_it():\n    A()\n    B()\n",
            },
        )
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
        mock_scan.side_effect = _payload_by_command(
            {
                "undocumented": {"total": 2, "undocumented": [{"qualified_name": "A"}, {"qualified_name": "B"}]},
                "uncovered": {"total": 1, "uncovered": [{"qualified_name": "C"}]},
            }
        )

        ok, live_gt, reason = script_gen_bench._validate_oss(task, _SQ, tmp_path / "idx.json", tmp_path)

        assert (ok, reason) == (True, "")
        assert mock_scan.call_count == 2  # both queries fired
        assert live_gt["oracle_views"]["undocumented"]["independent_ast"]["count"] == 2
        assert live_gt["oracle_views"]["uncovered"]["independent_ast"]["count"] == 1


class TestBuildUpdatedGroundTruth:
    """Merge live ground truth into existing ground truth per task type."""

    @pytest.mark.parametrize(
        "task_type", ["SYMBOL_EXTRACTION", "FN_CALL_GRAPH", "DEVELOP_BLAST_RADIUS", "CODE_QUALITY"]
    )
    def test_merges_live_into_existing(self, script_gen_bench: Any, task_type: str) -> None:
        """Return a merged dict with live values overriding existing ones for the standard types.

        Scenario: the validator computed live_gt; the builder must integrate it into the existing ground truth while
        preserving unrelated fields.
        """
        existing = {"preserved_field": "keep", "start_line": 99}
        live = {"start_line": 10, "end_line": 20}

        result = script_gen_bench._build_updated_ground_truth(script_gen_bench.TaskType[task_type], live, existing)

        assert result["start_line"] == 10  # live overrides
        assert result["preserved_field"] == "keep"  # existing preserved

    def test_review_assistance_returns_live_gt_only(self, script_gen_bench: Any) -> None:
        """Return live_gt unchanged for review_assistance (no merge with existing).

        Scenario: review_assistance stores per-sub_question entries; live_gt is the authoritative new structure and the
        existing ground truth is discarded.
        """
        task_type = script_gen_bench.TaskType.REVIEW_ASSISTANCE
        live = {"sq1": {"count": 5}}

        result = script_gen_bench._build_updated_ground_truth(task_type, live, {"old_key": "old_value"})

        assert result == live
        assert "old_key" not in result


def _contains_enum(item: object) -> bool:
    """Detect Enum instances in nested dictionary keys, values, or lists."""
    if isinstance(item, Enum):
        return True
    if isinstance(item, dict):
        return any(_contains_enum(key) or _contains_enum(value) for key, value in item.items())
    if isinstance(item, list):
        return any(_contains_enum(value) for value in item)
    return False


def _dump_plain_json(value: object, file: Any, **kwargs: Any) -> None:
    """Reject enum instances anywhere in the payload, then serialize it with the real ``json.dump``."""
    assert not _contains_enum(value)
    _REAL_JSON_DUMP(value, file, **kwargs)


class TestTaskType:
    """Task types are a closed enum internally and plain strings in JSON."""

    def test_enum_members_match_authoritative_validator_keys(self, script_gen_bench: Any) -> None:
        """The enum and validator routing table declare exactly the same task types.

        Scenario: a task type added to one side but not the other would silently skip validation or fail at routing.
        """
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

    @mock.patch("json.dump", autospec=True, side_effect=_dump_plain_json)
    def test_main_update_serializes_only_plain_strings_and_preserves_json_bytes(
        self,
        mock_dump: mock.Mock,
        script_gen_bench: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_discovery: tuple[Path, Path],
    ) -> None:
        """Task-type enum instances never cross the JSON write boundary.

        A no-op full-file update must keep canonical JSON bytes unchanged, so benchmark suite consumers continue to
        receive plain task-type strings. The task file is a byte-compared artefact: the write-back must emit LF on every
        host, never the host line ending that text mode would substitute.
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
        validators = {script_gen_bench.TaskType.SYMBOL_EXTRACTION: lambda *_: (True, {}, "")}
        monkeypatch.setattr(script_gen_bench, "VALIDATORS", validators)

        script_gen_bench.main(repo_path=str(tmp_path), update=True)

        assert mock_dump.called
        assert tasks_file.read_bytes() == original.encode()
        assert b"\r\n" not in tasks_file.read_bytes()


class TestValidatorsDict:
    """Map each documented task type to its validator."""

    @pytest.mark.parametrize(
        ("task_type", "expected_fn_name"),
        [
            pytest.param("SYMBOL_EXTRACTION", "_validate_symbol", id="symbol_extraction"),
            pytest.param("FN_CALL_GRAPH", "_validate_fn", id="fn_call_graph"),
            pytest.param("DEVELOP_BLAST_RADIUS", "_validate_fn", id="develop_blast_radius"),
            pytest.param("REVIEW_ASSISTANCE", "_validate_rv", id="review_assistance"),
            pytest.param("CODE_QUALITY", "_validate_oss", id="code_quality"),
        ],
    )
    def test_task_type_routes_to_validator(self, script_gen_bench: Any, task_type: str, expected_fn_name: str) -> None:
        """Each task type key maps to its documented validator function.

        Scenario: main() looks up VALIDATORS[task_type]; the mapping must be correct for all five documented
        validator-backed task types.
        """
        actual_fn = script_gen_bench.VALIDATORS.get(script_gen_bench.TaskType[task_type])

        assert actual_fn is getattr(script_gen_bench, expected_fn_name)

    def test_unknown_type_returns_none(self, script_gen_bench: Any) -> None:
        """VALIDATORS.get returns None for an unrecognised task type.

        Scenario: main() skips tasks with an unknown type; VALIDATORS.get(type) must return None without raising.
        """
        assert script_gen_bench.VALIDATORS.get("nonexistent_type") is None


class TestCallFinder:
    """Record the enclosing scope of each matching call site."""

    @staticmethod
    def _visit(script_gen_bench: Any, source: str) -> set:
        """Parse ``source`` and return the callers of ``target`` in module ``mod`` found by the call visitor."""
        callers: set = set()
        script_gen_bench._CallFinder("target", "mod", callers).visit(ast.parse(source))
        return callers

    @pytest.mark.parametrize(
        ("source", "expected_caller"),
        [
            pytest.param("def caller():\n    target()\n", "mod::caller", id="direct-call"),
            pytest.param("def caller():\n    obj.target()\n", "mod::caller", id="attribute-call"),
            pytest.param(
                "class MyClass:\n    def my_method(self):\n        target()\n",
                "mod::MyClass.my_method",
                id="nested-class-method",
            ),
        ],
    )
    def test_records_enclosing_scope_of_matching_call(
        self, script_gen_bench: Any, source: str, expected_caller: str
    ) -> None:
        """Record module::scope when a function or method calls the target by name or attribute.

        Scenario: a simple function calling the target by name, a method call via attribute access (obj.target()), and
        a call inside a class method whose scope stack includes class and method (Module::Class.method).
        """
        assert expected_caller in self._visit(script_gen_bench, source)

    @pytest.mark.parametrize(
        "source",
        [
            pytest.param("target()\n", id="module-level-call"),
            pytest.param("def caller():\n    other_fn()\n", id="different-name"),
        ],
    )
    def test_does_not_record_unmatched_calls(self, script_gen_bench: Any, source: str) -> None:
        """Record nothing for a call outside any scope or for a call with a different name.

        Scenario: a call at module level is ignored per the _scope_stack guard (``if matched and self._scope_stack``);
        a false-positive guard ensures only the exact simple name matches.
        """
        assert self._visit(script_gen_bench, source) == set()


class TestCallersViaAst:
    """Walk a repository tree and collect the callers of a target function."""

    @pytest.mark.parametrize(
        ("files", "expected_callers"),
        [
            pytest.param({}, set(), id="empty-repo"),
            pytest.param(
                {"example.py": "def do_thing():\n    target()\n"}, {"example::do_thing"}, id="caller-in-py-file"
            ),
            pytest.param(
                {".venv/lib/pkg.py": "def hidden():\n    target()\n", "real.py": "# no calls here\n"},
                set(),
                id="venv-dir-skipped",
            ),
            pytest.param(
                {"broken.py": "def bad syntax !!!\n", "good.py": "def ok():\n    target()\n"},
                {"good::ok"},
                id="syntax-error-file-skipped",
            ),
        ],
    )
    def test_collects_callers_from_python_files(
        self, script_gen_bench: Any, tmp_path: Path, files: dict[str, str], expected_callers: set[str]
    ) -> None:
        """Collect callers from the repo's Python files while skipping virtualenvs and unparsable files.

        Scenario: an empty repository finds nothing; a single caller file is found; a virtualenv directory is never
        traversed; a malformed file does not stop the walker from processing its well-formed sibling.
        """
        _write_files(tmp_path, files)

        callers, err = script_gen_bench._callers_via_ast("mod::target", tmp_path)

        assert callers == expected_callers
        assert err is None


class TestQualifiedCallerOracle:
    """Authoritative caller set resolves receivers; the loose set over-approximates."""

    def test_same_named_method_in_unrelated_class_not_credited(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """A `self.bar()` call in an unrelated class is NOT credited as a caller of Foo.bar.

        Scenario: two classes call a `bar` method; only the one whose enclosing class is the target class resolves. The
        loose (simple-name) set still contains both — kept as diagnostic.
        """
        src = (
            "class Foo:\n    def caller(self):\n        self.bar()\n\n"
            "class Baz:\n    def other(self):\n        self.bar()\n"
        )
        _write_files(tmp_path, {"m.py": src})

        qualified, loose, err = script_gen_bench._walk_caller_sets("m::Foo.bar", tmp_path)

        assert err is None
        assert qualified == {"m::Foo.caller"}
        assert "m::Baz.other" in loose

    def test_module_function_attribute_call_uses_target_module(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """A cross-module `foo.func()` is credited only when `foo` is the TARGET module, not the caller.

        Scenario: the module-level-function branch compares the receiver tail against the target module (`pkg.foo`),
        never the caller file's own module — so `foo.func()` from pkg.bar IS credited while `bar.func()` with the same
        name is NOT.
        """
        files = {
            "pkg/foo.py": "def func():\n    pass\n",
            "pkg/bar.py": "def use_target():\n    foo.func()\n\n\ndef use_other():\n    bar.func()\n",
        }
        _write_files(tmp_path, files)

        qualified, _loose, _ = script_gen_bench._walk_caller_sets("pkg.foo::func", tmp_path)

        assert qualified == {"pkg.bar::use_target"}

    @pytest.mark.parametrize(
        ("files", "expected_qualified", "expected_loose"),
        [
            pytest.param(
                {
                    "caller.py": "from external.helpers import is_overridden\n\n\ndef use_external():\n    is_overridden()\n"
                },
                set(),
                {"caller::use_external"},
                id="external-import-not-credited",
            ),
            pytest.param(
                {
                    "caller.py": (
                        "from pkg.model_helpers import other as is_overridden\n\ndef use_other():\n    is_overridden()\n"
                    )
                },
                set(),
                {"caller::use_other"},
                id="alias-of-different-target-symbol",
            ),
            pytest.param(
                {
                    "caller.py": (
                        "def use_external():\n"
                        "    from external.helpers import is_overridden\n"
                        "    is_overridden()\n\n"
                        "def use_local():\n"
                        "    is_overridden()\n"
                    )
                },
                {"caller::use_local"},
                {"caller::use_external", "caller::use_local"},
                id="function-local-external-import-keeps-sibling-local-call",
            ),
            pytest.param(
                {
                    "pkg/model_helpers.py": "def is_overridden():\n    pass\n",
                    "pkg/exports.py": "from pkg.model_helpers import is_overridden\n",
                    "target_user.py": (
                        "from pkg.exports import is_overridden\n\n\ndef use_target_reexport():\n    is_overridden()\n"
                    ),
                    "external_user.py": (
                        "from external.helpers import is_overridden\n\n\ndef use_external():\n    is_overridden()\n"
                    ),
                },
                {"target_user::use_target_reexport"},
                {"external_user::use_external", "target_user::use_target_reexport"},
                id="one-hop-reexport-credited-external-excluded",
            ),
        ],
    )
    def test_import_bindings_split_qualified_callers_from_loose_candidates(
        self,
        script_gen_bench: Any,
        tmp_path: Path,
        files: dict[str, str],
        expected_qualified: set[str],
        expected_loose: set[str],
    ) -> None:
        """Credit only callers whose import binding resolves to the target while the loose set over-approximates.

        Scenario: a project calls an external ``is_overridden`` after importing it directly, or an alias whose module
        matches but original symbol does not, and matching spelling alone would corrupt the canonical caller set for
        the in-repository function with the same name. An external import binding applies only inside its lexical
        function. A package that re-exports its target function once is followed by the source resolver while the
        external-import exclusion is preserved.
        """
        _write_files(tmp_path, files)

        qualified, loose, err = script_gen_bench._walk_caller_sets("pkg.model_helpers::is_overridden", tmp_path)

        assert err is None
        assert qualified == expected_qualified
        assert loose == expected_loose

    @pytest.mark.parametrize(
        ("target", "files", "expected_qualified"),
        [
            pytest.param(
                "m::Foo.bar",
                {"m.py": "def use():\n    Foo().bar()\n"},
                {"m::use"},
                id="direct-class-instantiation",
            ),
            pytest.param(
                "pkg.model_helpers::is_overridden",
                {
                    "caller.py": "from pkg.model_helpers import is_overridden\n\n\ndef use_target():\n    is_overridden()\n"
                },
                {"caller::use_target"},
                id="bare-call-imported-from-target-module",
            ),
            pytest.param(
                "pkg.model_helpers::is_overridden",
                {"caller.py": "def use_local():\n    is_overridden()\n"},
                {"caller::use_local"},
                id="unbound-bare-call-keeps-local-fallback",
            ),
            pytest.param(
                "pkg.target::move_data_to_device",
                {
                    "pkg/target.py": "def move_data_to_device():\n    pass\n",
                    "pkg/middle.py": "from pkg.target import move_data_to_device\n",
                    "pkg/facade.py": "from pkg.middle import move_data_to_device\n",
                    "consumer.py": (
                        "from pkg.facade import move_data_to_device\n\n\n"
                        "class DataHooks:\n"
                        "    def transfer_batch_to_device(self):\n"
                        "        return move_data_to_device()\n"
                    ),
                },
                {"consumer::DataHooks.transfer_batch_to_device"},
                id="two-hop-reexport-credited",
            ),
        ],
    )
    def test_caller_forms_resolving_to_the_target_are_credited(
        self, script_gen_bench: Any, tmp_path: Path, target: str, files: dict[str, str], expected_qualified: set[str]
    ) -> None:
        """Credit direct instantiation, import bindings to the target module, and local fallbacks.

        Scenario: a free function instantiates the class and calls the method directly; a direct
        ``from pkg.model_helpers import is_overridden`` import is an unambiguous reference to the target without a
        module-qualified receiver; local same-module bare calls are unresolved statically but remain valid caller
        candidates, so import awareness must not erase them; and a caller reached through two package re-export hops
        is credited, preventing the AST oracle from silently omitting real production callers when a public facade
        re-exports a symbol through an intermediate package.
        """
        _write_files(tmp_path, files)

        qualified, _loose, err = script_gen_bench._walk_caller_sets(target, tmp_path)

        assert err is None
        assert qualified == expected_qualified

    @pytest.mark.parametrize(
        ("target", "files", "expected_qualified"),
        [
            pytest.param(
                "pkg::target",
                {"src/pkg/mod.py": "def caller():\n    target()\n"},
                {"pkg.mod::caller"},
                id="src-layout",
            ),
            pytest.param(
                "pkg.sub.mod::target",
                {
                    "src/pkg/__init__.py": "",
                    "src/pkg/sub/__init__.py": "",
                    "src/pkg/sub/mod.py": "def src_caller():\n    target()\n",
                    "flatpkg/__init__.py": "",
                    "flatpkg/mod.py": "def flat_caller():\n    target()\n",
                },
                {"pkg.sub.mod::src_caller", "flatpkg.mod::flat_caller"},
                id="src-and-flat-layouts-mixed",
            ),
        ],
    )
    def test_emitted_module_paths_carry_no_src_prefix(
        self, script_gen_bench: Any, tmp_path: Path, target: str, files: dict[str, str], expected_qualified: set[str]
    ) -> None:
        """A caller under a `src/` layout is emitted in the repo namespace, not with a `src.` prefix.

        Scenario: `--update` on a real src-layout clone must write ground truth in the repo's real namespace. In the
        mixed case a src-layout package (full __init__ chain under src/) and a flat-layout package both call the
        target; the `src/` prefix is stripped structurally, matching what scan-query/scan-index emit for the same
        clone.
        """
        _write_files(tmp_path, files)

        qualified, _loose, _ = script_gen_bench._walk_caller_sets(target, tmp_path)

        assert qualified == expected_qualified

    def test_test_modules_excluded_from_callers(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Callers inside a `tests/` directory are excluded from the emitted namespace.

        Scenario: a test file calls the target; only the production caller is credited.
        """
        files = {"tests/test_mod.py": "def test_x():\n    target()\n", "prod.py": "def caller():\n    target()\n"}
        _write_files(tmp_path, files)

        qualified, _loose, _ = script_gen_bench._walk_caller_sets("mod::target", tmp_path)

        assert qualified == {"prod::caller"}


class TestValidateReviewAssistanceAst:
    """Supported review commands obtain ground truth from the AST, not scan-query."""

    @staticmethod
    def _task(cmd: str, args: list[str], match: str, ground_truth: dict[str, Any]) -> dict[str, Any]:
        """Build one review task/sub-question for an AST oracle contract."""
        return {
            "type": "review_assistance",
            "expected_queries": [{"cmd": cmd, "args": args}],
            "sub_questions": [{"id": "sq", "match": match, "ground_truth": ground_truth}],
        }

    @mock.patch(_RUN_SCAN_QUERY, autospec=True)
    @pytest.mark.parametrize(
        ("files", "cmd", "args", "match", "ground_truth", "scan_payload"),
        [
            pytest.param(
                {"m.py": "def public():\n    pass\n"},
                "undocumented",
                [],
                "symbol_name_set",
                {"symbols": ["public"]},
                {"total": 0, "undocumented": []},
                id="undocumented-symbol-set",
            ),
            pytest.param(
                {"m.py": "def orphan():\n    pass\n"},
                "uncovered",
                [],
                "symbol_name_set",
                {"symbols": ["orphan"]},
                {"total": 0, "uncovered": []},
                id="uncovered-symbol-set",
            ),
            pytest.param(
                {"hub.py": "value = 1\n", "consumer.py": "import hub\n"},
                "rdeps",
                ["hub"],
                "integer_extract",
                {"count": 1},
                {"imported_by": []},
                id="rdeps-importer-count",
            ),
            pytest.param(
                {"m.py": "def target():\n    pass\n\n\ndef caller():\n    target()\n"},
                "fn-rdeps",
                ["m::target"],
                "integer_extract",
                {"count": 1},
                {"count": 0, "called_by": []},
                id="fn-rdeps-caller-count",
            ),
        ],
    )
    def test_supported_commands_use_ast_over_conflicting_scan_payload(
        self,
        mock_scan: mock.Mock,
        script_gen_bench: Any,
        tmp_path: Path,
        files: dict[str, str],
        cmd: str,
        args: list[str],
        match: str,
        ground_truth: dict[str, Any],
        scan_payload: dict[str, Any],
    ) -> None:
        """Use the AST oracle result even when the scan diagnostic disagrees.

        Scenario: the AST symbol set is used for undocumented and uncovered symbols even when scan-query omits them;
        direct module importers and qualified callers are counted from the AST rather than from scan output.
        """
        _write_files(tmp_path, files)
        task = self._task(cmd, args, match, ground_truth)
        mock_scan.return_value = scan_payload

        ok, live_gt, reason = script_gen_bench._validate_rv(task, _SQ, tmp_path / "idx.json", tmp_path)

        assert (ok, reason) == (True, "")
        assert live_gt == {"sq": ground_truth}

    @mock.patch(_RUN_SCAN_QUERY, autospec=True)
    @pytest.mark.parametrize("cmd", ["undocumented", "uncovered"])
    def test_ast_symbol_commands_count_full_set_but_cap_symbol_results(
        self, mock_scan: mock.Mock, script_gen_bench: Any, tmp_path: Path, cmd: str
    ) -> None:
        """Use full AST cardinality for counts and requested cardinality for symbols.

        Scenario: the independent oracle finds two symbols.  A count question must receive ``2``; a symbol-set question
        with one stored symbol must receive only its requested first canonical symbol, matching the review task's
        intentional result cap.
        """
        _write_files(tmp_path, {"m.py": "def alpha():\n    pass\n\n\ndef beta():\n    pass\n"})
        mock_scan.return_value = {cmd: [], "total": 0}  # conflicting scan payload
        count_task = self._task(cmd, [], "integer_extract", {"count": 2})
        symbols_task = self._task(cmd, [], "symbol_name_set", {"symbols": ["alpha"]})

        count_result = script_gen_bench._validate_rv(count_task, _SQ, tmp_path / "idx.json", tmp_path)
        symbols_result = script_gen_bench._validate_rv(symbols_task, _SQ, tmp_path / "idx.json", tmp_path)

        assert count_result == (True, {"sq": {"count": 2}}, "")
        assert symbols_result == (True, {"sq": {"symbols": ["alpha"]}}, "")


class TestValidateWorkflowTaskFamilies:
    """Debug, feature, and real-issue tasks have offline canonical validators."""

    @pytest.mark.parametrize(
        ("validator", "task", "files"),
        [
            pytest.param(
                "_validate_debug",
                _debug_task({"file": "src/pkg/mod.py", "function": "locate", "start_line": 2}),
                {"src/pkg/mod.py": _DEBUG_ANCHOR_SOURCE},
                id="debug-exact-anchor",
            ),
            pytest.param(
                "_validate_feature",
                _feature_task("Extension.hook"),
                {"src/pkg/feature.py": "class Extension:\n    def hook(self):\n        pass\n"},
                id="feature-existing-entry-point",
            ),
            pytest.param(
                "_validate_real_issue",
                _real_issue_task(True, ["src/pkg/fix.py"]),
                {"src/pkg/fix.py": "value = 1\n"},
                id="real-issue-scoreable-paths",
            ),
            pytest.param(
                "_validate_real_issue",
                _real_issue_task(False, ["legacy/pkg/fix.py"]),
                {},
                id="real-issue-unscoreable-legacy-paths",
            ),
        ],
    )
    def test_accepts_canonical_task(
        self, script_gen_bench: Any, tmp_path: Path, validator: str, task: dict[str, Any], files: dict[str, str]
    ) -> None:
        """Accept a task whose offline anchors and provenance match the checkout.

        Scenario: a debug task needs the exact path, qualified symbol and line; a feature task needs its AST entry
        point; a scoreable real issue needs existing safe paths and locked provenance, while an explicitly unscoreable
        legacy task may reference paths absent from this checkout.
        """
        _write_files(tmp_path, files)

        ok, live_gt, reason = getattr(script_gen_bench, validator)(task, _SQ, tmp_path / "idx.json", tmp_path)

        assert (ok, reason) == (True, "")
        assert live_gt == task["ground_truth"]

    @pytest.mark.parametrize(
        ("validator", "task", "files", "reason_fragment"),
        [
            pytest.param(
                "_validate_debug",
                _debug_task({"file": "../outside.py", "function": "locate", "start_line": 2}),
                {"src/pkg/mod.py": _DEBUG_ANCHOR_SOURCE},
                "safe",
                id="debug-path-escapes-repository",
            ),
            pytest.param(
                "_validate_feature",
                _feature_task("Extension.missing"),
                {"src/pkg/feature.py": "class Extension:\n    def hook(self):\n        pass\n"},
                "entry_point",
                id="feature-missing-entry-point",
            ),
            pytest.param(
                "_validate_real_issue",
                _real_issue_task(True, ["src/pkg/missing.py"]),
                {},
                "does not exist",
                id="real-issue-missing-scoreable-path",
            ),
        ],
    )
    def test_rejects_noncanonical_task(
        self,
        script_gen_bench: Any,
        tmp_path: Path,
        validator: str,
        task: dict[str, Any],
        files: dict[str, str],
        reason_fragment: str,
    ) -> None:
        """Reject a task whose anchor escapes the repository or does not exist, without live ground truth.

        Scenario: a debug path leaving the repo, a feature extension point absent from its file, and a scoreable
        real-issue path missing locally must each fail with a reason naming the offending part.
        """
        _write_files(tmp_path, files)

        ok, live_gt, reason = getattr(script_gen_bench, validator)(task, _SQ, tmp_path / "idx.json", tmp_path)

        assert ok is False
        assert live_gt is None
        assert reason_fragment in reason

    def test_debug_wrong_line_returns_independent_live_gt_for_update(
        self, script_gen_bench: Any, tmp_path: Path
    ) -> None:
        """Return the AST line so a target-version relock can refresh stale debug ground truth.

        Scenario: the stored start_line (3) no longer matches the AST (2); validation fails but still hands back the
        independent live anchor for an authorized update.
        """
        _write_files(tmp_path, {"src/pkg/mod.py": _DEBUG_ANCHOR_SOURCE})
        task = _debug_task({"file": "src/pkg/mod.py", "function": "locate", "start_line": 3})

        ok, live_gt, reason = script_gen_bench._validate_debug(task, _SQ, tmp_path / "idx.json", tmp_path)

        assert ok is False
        assert live_gt == {"file": "src/pkg/mod.py", "function": "locate", "start_line": 2}
        assert "start_line" in reason


class TestIsPublicQualname:
    """Public qualified name = no dotted component starts with an underscore."""

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            pytest.param("Trainer.fit", True, id="public-method"),
            pytest.param("func", True, id="public-function"),
            pytest.param("_helper", False, id="private-function"),
            pytest.param("_Cache.get", False, id="method-of-private-class"),
            pytest.param("Trainer.__init__", False, id="dunder-method"),
            pytest.param("", False, id="empty"),
        ],
    )
    def test_public_rule(self, script_gen_bench: Any, name: str, expected: bool) -> None:
        """Mirror scan-query _is_public_symbol: any leading-underscore component is private.

        Scenario: public names, private names at any depth, dunders and the empty string are classified exactly as the
        scan-query rule does, so the AST oracles select the same symbols.
        """
        assert script_gen_bench._is_public_qualname(name) is expected


class TestUndocumentedViaAst:
    """Independent AST oracle lists public symbols lacking a docstring."""

    def test_finds_public_undocumented(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """Report a public class and function without a docstring.

        Scenario: one module with an undocumented class and function; both are reported with no error.
        """
        _write_files(tmp_path, {"m.py": "class A:\n    pass\n\n\ndef pub():\n    pass\n"})

        syms, err = script_gen_bench._undocumented_via_ast(tmp_path)

        assert err is None
        assert syms == {"A", "pub"}

    def test_documented_symbol_excluded(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """A symbol with a docstring is not reported as undocumented.

        Scenario: the only public function carries a docstring, so the undocumented set is empty.
        """
        _write_files(tmp_path, {"m.py": 'def pub():\n    """Doc."""\n    return 1\n'})

        syms, _ = script_gen_bench._undocumented_via_ast(tmp_path)

        assert syms == set()

    def test_module_filter_unresolvable_returns_error(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """A module name that maps to no file yields an error reason and an empty set.

        Scenario: the caller scopes the scan to ``pkg.missing`` in an empty repo; the oracle must say so, not report
        zero undocumented symbols as if the module were clean.
        """
        syms, err = script_gen_bench._undocumented_via_ast(tmp_path, module="pkg.missing")

        assert syms == set()
        assert err is not None
        assert "not resolvable" in err


class TestUncoveredViaAst:
    """Independent AST oracle lists public symbols with no test references."""

    def test_symbol_with_no_test_reference_is_uncovered(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """A public symbol absent from every configured test-AST reference is uncovered.

        Scenario: one public function and no test files at all.
        """
        _write_files(tmp_path, {"m.py": "def orphan():\n    pass\n"})

        syms, err = script_gen_bench._uncovered_via_ast(tmp_path)

        assert err is None
        assert syms == {"orphan"}

    @pytest.mark.parametrize(
        ("symbol", "test_source"),
        [
            pytest.param("used", "def test_it():\n    used()\n", id="symbol-called-by-test"),
            pytest.param("mentioned", "def test_it():\n    mentioned\n", id="bare-name-reference"),
            pytest.param("mentioned", "def test_it():\n    fixture.mentioned\n", id="attribute-reference"),
            pytest.param(
                "mocked",
                "from unittest.mock import patch\n\n\n@patch('m.mocked')\ndef test_it():\n    pass\n",
                id="patch-string-target",
            ),
        ],
    )
    def test_symbol_referenced_by_tests_is_not_uncovered(
        self, script_gen_bench: Any, tmp_path: Path, symbol: str, test_source: str
    ) -> None:
        """A public symbol that a test calls, mentions by name or attribute, or patches by string is covered.

        Scenario: the broad oracle records references even when they are not call targets, and a symbol referenced
        only through a patch() string target counts as covered.
        """
        _write_files(tmp_path, {"m.py": f"def {symbol}():\n    pass\n", "tests/test_m.py": test_source})

        syms, _ = script_gen_bench._uncovered_via_ast(tmp_path)

        assert symbol not in syms

    def test_every_patch_string_argument_tail_counts_as_coverage(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """The declared oracle records every string argument to a recognized patch call.

        Scenario: one ``patch('m.first', 'm.second')`` call names two symbols; both must count as covered.
        """
        files = {
            "m.py": "def first():\n    pass\n\n\ndef second():\n    pass\n",
            "tests/test_m.py": "from unittest.mock import patch\n\n\ndef test_it():\n    patch('m.first', 'm.second')\n",
        }
        _write_files(tmp_path, files)

        syms, _ = script_gen_bench._uncovered_via_ast(tmp_path)

        assert syms == set()


class TestAstSymbolOracleSharedRules:
    """The undocumented and uncovered AST oracles share their public-symbol selection rules."""

    @pytest.mark.parametrize(
        ("oracle_name", "files"),
        [
            pytest.param("_undocumented_via_ast", {"m.py": "def _helper():\n    pass\n"}, id="undocumented-private"),
            pytest.param("_uncovered_via_ast", {"m.py": "def _helper():\n    pass\n"}, id="uncovered-private"),
            pytest.param(
                "_undocumented_via_ast", {"test_x.py": "def pub():\n    pass\n"}, id="undocumented-test-module-skipped"
            ),
        ],
    )
    def test_non_reportable_symbols_are_excluded(
        self, script_gen_bench: Any, tmp_path: Path, oracle_name: str, files: dict[str, str]
    ) -> None:
        """Private symbols and test modules are never reported by the oracles.

        Scenario: private (leading-underscore) symbols are excluded even without a docstring or test reference,
        mirroring the scan-query public-only rule; files named test_*.py are skipped because scan-query skips test
        modules.
        """
        _write_files(tmp_path, files)

        syms, _ = getattr(script_gen_bench, oracle_name)(tmp_path)

        assert syms == set()

    @pytest.mark.parametrize(
        ("oracle_name", "source", "expected_name"),
        [
            pytest.param(
                "_undocumented_via_ast",
                "class Cls:\n    def meth(self):\n        pass\n",
                "Cls.meth",
                id="undocumented",
            ),
            pytest.param(
                "_uncovered_via_ast", "class Cls:\n    def orphan(self):\n        pass\n", "Cls.orphan", id="uncovered"
            ),
        ],
    )
    def test_nested_method_qualified_name(
        self, script_gen_bench: Any, tmp_path: Path, oracle_name: str, source: str, expected_name: str
    ) -> None:
        """Report methods as module-relative Class.method qualified names from both oracles.

        Scenario: a public class holding one undocumented, untested method.
        """
        _write_files(tmp_path, {"m.py": source})

        syms, _ = getattr(script_gen_bench, oracle_name)(tmp_path)

        assert expected_name in syms

    @pytest.mark.parametrize(
        ("oracle_name", "source", "expected_symbols"),
        [
            pytest.param("_undocumented_via_ast", "def pub():\n    pass\n", {"pub"}, id="undocumented"),
            pytest.param("_uncovered_via_ast", "def solo():\n    pass\n", {"solo"}, id="uncovered"),
        ],
    )
    def test_module_filter_resolves_src_layout(
        self, script_gen_bench: Any, tmp_path: Path, oracle_name: str, source: str, expected_symbols: set[str]
    ) -> None:
        """A dotted module filter resolves against <repo>/src/<parts>.py and scopes the scan to that module.

        Scenario: a src-layout repository where the filter ``pkg.mod`` must find ``src/pkg/mod.py``.
        """
        _write_files(tmp_path, {"src/pkg/mod.py": source})

        syms, err = getattr(script_gen_bench, oracle_name)(tmp_path, module="pkg.mod")

        assert err is None
        assert syms == expected_symbols


class TestXrefsBrokenViaAst:
    """Independent AST oracle for unresolved Sphinx/MkDocs cross-references (mirrors scan-index/scan-query)."""

    @pytest.mark.parametrize(
        ("source", "expected_broken"),
        [
            pytest.param(
                "class Cls:\n    def method(self):\n        'See :meth:`missing`.'\n        pass\n",
                [("mod::missing", "meth")],
                id="bare-meth-role-without-top-level-function",
            ),
            pytest.param(
                "X = 1\n\n\ndef f():\n    'See :func:`mod.X`.'\n    pass\n",
                [("mod::X", "func")],
                id="module-level-assignment-target",
            ),
        ],
    )
    def test_unresolved_symbol_roles_are_broken(
        self, script_gen_bench: Any, tmp_path: Path, source: str, expected_broken: list[tuple[str, str]]
    ) -> None:
        """A bare ``:meth:`name`` `` role with no matching top-level function is broken.

        A module-level assignment is outside the narrow symbol map, so a reference to it stays broken. Regression guard
        for the symbol-map-breadth risk: a broader map (module/class-level assignments included) would resolve this
        and silently under-report broken targets, verified empirically to flip a known-broken scan-query reference to
        resolvable.
        """
        _write_files(tmp_path, {"mod.py": source})

        broken, err = script_gen_bench._xrefs_broken_via_ast(tmp_path)

        assert err is None
        assert [(b["target"], b["role"]) for b in broken] == expected_broken

    @pytest.mark.parametrize(
        "files",
        [
            pytest.param(
                {
                    "mod.py": (
                        "class Cls:\n"
                        "    def method(self):\n"
                        "        'See :meth:`helper`.'\n"
                        "        pass\n\n\n"
                        "def helper():\n"
                        "    pass\n"
                    )
                },
                id="bare-meth-role-resolves-to-top-level-function",
            ),
            pytest.param({"mod.py": "def f():\n    'See :attr:`mod.missing`.'\n    pass\n"}, id="attr-role-unchecked"),
            pytest.param({"mod.py": "def f():\n    'See :data:`mod.missing`.'\n    pass\n"}, id="data-role-unchecked"),
            pytest.param({"mod.py": "def f():\n    'See :mod:`mod.missing`.'\n    pass\n"}, id="mod-role-unchecked"),
            pytest.param(
                {"mod.py": "def present():\n    pass\n", "index.rst": "See :func:`mod.present`.\n"},
                id="rst-dotted-ref-to-existing-symbol",
            ),
            pytest.param({"README.md": "See [`mod.missing`][].\n"}, id="mkdocs-ref-outside-docs-dir-not-scanned"),
        ],
    )
    def test_resolved_or_unchecked_references_are_not_broken(
        self, script_gen_bench: Any, tmp_path: Path, files: dict[str, str]
    ) -> None:
        """References that resolve to a real symbol or fall outside the checked scope are not reported broken.

        Scenario: a bare ``:meth:`name`` `` role matching a real top-level function proves brokenness depends on the
        symbol map, not on the role text alone. ``attr``/``data``/``mod`` roles are extracted but never checked
        (mirrors ``query.py`` ``_SYMBOL_ROLES``, which deliberately omits them). A dotted ``.rst`` role resolving to a
        real symbol is not broken. The identical mkdocstrings autoref in a repo-root README.md is not scanned
        (scan-index restricts ``.md`` to ``docs/``).
        """
        _write_files(tmp_path, files)

        broken, err = script_gen_bench._xrefs_broken_via_ast(tmp_path)

        assert err is None
        assert broken == []

    def test_dotted_func_role_attributed_to_target_module(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """A dotted target is scoped by the TARGET's module, not the docstring's home module.

        Mirrors scan-query's own scoping (``query.py:3826-3828``): ``--broken <module>`` filters on the resolved
        target's prefix, so a broken reference living in one file's docstring is attributed to a different module
        entirely.
        """
        files = {"a.py": "def f():\n    'See :func:`b.missing`.'\n    pass\n", "b.py": "def present():\n    pass\n"}
        _write_files(tmp_path, files)

        broken, err = script_gen_bench._xrefs_broken_via_ast(tmp_path, module="b")

        assert err is None
        assert [(b["target"], b["file"]) for b in broken] == [("b::missing", "a.py")]

    def test_line_is_docstring_opening_line_for_multiline_docstring(
        self, script_gen_bench: Any, tmp_path: Path
    ) -> None:
        """The reported line is the docstring literal's opening line, not the matched-role line.

        Mirrors scan-index's documented approximation (``scanner.py:1866-1868``): AST docstring nodes only expose the
        opening literal's ``lineno``, so per-role-match line offsets are not tracked.
        """
        source = "def f():\n    '''First line.\n\n    See :func:`mod.missing` on a later line.\n    '''\n    pass\n"
        _write_files(tmp_path, {"mod.py": source})

        broken, err = script_gen_bench._xrefs_broken_via_ast(tmp_path)

        assert err is None
        assert broken[0]["line"] == 2

    def test_rst_dotted_ref_to_missing_symbol_is_broken(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """A dotted ``.rst`` role with no matching symbol is reported broken, with a real line number.

        Scenario: the reference sits on the second line of an otherwise empty reStructuredText file.
        """
        _write_files(tmp_path, {"index.rst": "Intro.\nSee :func:`mod.missing`.\n"})

        broken, err = script_gen_bench._xrefs_broken_via_ast(tmp_path)

        assert err is None
        assert [(b["target"], b["file"], b["line"]) for b in broken] == [("mod::missing", "index.rst", 2)]

    def test_rst_bare_ref_never_resolves(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """A bare (dotless) ``.rst`` role target has no ``::`` and can never match the symbol map.

        ``.rst`` files anchor to no Python module (``current_module=""``); a bare role target resolves to the raw
        string unchanged (scan-index's own docstring claims such targets are "dropped", but the code does not do that —
        verified directly against ``codemap_py.scanner._resolve_xref_target``). Reproducing the actual behavior, not
        the stale docstring, is the whole point of an independent oracle.
        """
        _write_files(tmp_path, {"index.rst": "See :func:`missing_bare`.\n"})

        broken, err = script_gen_bench._xrefs_broken_via_ast(tmp_path)

        assert err is None
        assert [b["target"] for b in broken] == ["missing_bare"]

    def test_mkdocs_ref_under_docs_dir_counted(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """A dotted mkdocstrings autoref under ``docs/`` is scanned and can be broken.

        Scenario: the same autoref that is ignored in a repo-root README is reported when it lives in ``docs/api.md``.
        """
        _write_files(tmp_path, {"docs/api.md": "See [`mod.missing`][].\n"})

        broken, err = script_gen_bench._xrefs_broken_via_ast(tmp_path)

        assert err is None
        assert [(b["target"], b["source"]) for b in broken] == [("mod::missing", "mkdocs")]

    def test_unresolvable_module_argument_returns_error(self, script_gen_bench: Any, tmp_path: Path) -> None:
        """An unresolvable module argument is a hard failure, not a silent zero-broken pass.

        Scenario: the caller scopes the scan to a module that maps to no file in an empty repo.
        """
        broken, err = script_gen_bench._xrefs_broken_via_ast(tmp_path, module="nope.missing")

        assert broken == []
        assert err is not None


class TestValidateFnAstAuthoritative:
    """Treat the independent syntax-tree oracle as authoritative during function validation."""

    @mock.patch(_RUN_SCAN_QUERY, autospec=True, return_value={"called_by": [{"caller": "other::ghost"}], "count": 1})
    def test_scan_output_stored_as_diagnostic(
        self, mock_scan: mock.Mock, script_gen_bench: Any, tmp_path: Path
    ) -> None:
        """Preserve scan-query callers under fn_callers_scan, not as the authoritative set.

        Scenario: scan-query reports a ghost caller while the AST sees a different real one; ground truth follows the
        AST and the disagreement is recorded as divergence.
        """
        _write_files(tmp_path, {"mod/a.py": "def fn_a():\n    target()\n"})
        task = _fn_task("mod::target", ["mod.a::fn_a"], 1, 0)

        _, live_gt, _ = script_gen_bench._validate_fn(task, _SQ, tmp_path / "idx.json", tmp_path)

        assert live_gt["fn_callers"] == ["mod.a::fn_a"]  # AST oracle authoritative
        assert live_gt["fn_callers_scan"] == ["other::ghost"]  # scan diagnostic
        assert live_gt["ast_divergence"]["scan_only"] == ["other::ghost"]
        assert live_gt["ast_divergence"]["ast_only"] == ["mod.a::fn_a"]

    @mock.patch(_RUN_SCAN_QUERY, autospec=True, return_value={"called_by": [{"caller": "other::ghost"}], "count": 1})
    def test_divergence_warning_printed(
        self, mock_scan: mock.Mock, script_gen_bench: Any, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """Print a loud divergence banner when the AST and scan-query disagree.

        Scenario: the AST finds no callers while scan-query reports one, which a maintainer must not miss.
        """
        task = _fn_task("mod::target", [], 0, 0)

        script_gen_bench._validate_fn(task, _SQ, tmp_path / "idx.json", tmp_path)

        assert "DIVERGENCE" in capsys.readouterr().out


class TestUpdateGating:
    """Circular (scan-derived) refresh is gated behind ``--update-from-tool``."""

    @pytest.mark.parametrize(
        ("task", "expected"),
        [
            pytest.param({"type": "fn_call_graph"}, True, id="fn-call-graph"),
            pytest.param({"type": "develop_blast_radius"}, True, id="develop-blast-radius"),
            pytest.param(
                {"type": "code_quality", "ground_truth": {"check": "undocumented"}}, True, id="quality-undocumented"
            ),
            pytest.param(
                {"type": "code_quality", "ground_truth": {"check": "uncovered"}}, True, id="quality-uncovered"
            ),
            pytest.param(
                {"type": "code_quality", "ground_truth": {"check": "combined_health"}}, True, id="quality-combined"
            ),
            pytest.param({"type": "code_quality", "ground_truth": {"check": "xrefs_broken"}}, True, id="quality-xrefs"),
            pytest.param(
                {"type": "code_quality", "ground_truth": {"check": "coupled"}},
                False,
                id="quality-coupled-is-tool-derived",
            ),
            pytest.param({"type": "review_assistance"}, False, id="review-without-queries"),
            pytest.param({"type": "symbol_extraction"}, False, id="symbol-extraction"),
            pytest.param(
                {"type": "review_assistance", "expected_queries": [{"cmd": "rdeps", "args": ["m"]}]},
                True,
                id="review-assistance-supported-command",
            ),
            pytest.param(
                {"type": "review_assistance", "expected_queries": [{"cmd": "unsupported", "args": []}]},
                False,
                id="review-assistance-unsupported-command",
            ),
            pytest.param({"type": "debug_from_trace"}, True, id="debug-from-trace"),
        ],
    )
    def test_oracle_backed_classification(self, script_gen_bench: Any, task: dict, expected: bool) -> None:
        """Only AST-oracle-backed types are safe to refresh under a plain ``--update``.

        A review task is oracle-backed only when every review answer has an AST oracle, so a supported command allows a
        plain refresh while an unsupported command does not.
        """
        assert script_gen_bench._update_is_oracle_backed(task) is expected

    def test_oracle_backed_type_refreshes_by_default(self, script_gen_bench: Any) -> None:
        """An fn_call_graph task refreshes under plain ``--update`` (update_from_tool=False).

        Scenario: the AST oracle backs this type, so refreshing from live data is not circular.
        """
        task = {"type": "fn_call_graph", "ground_truth": {"fn_callers": []}}
        live = {"fn_callers": ["m::a"], "unique_caller_count": 1}

        stored, status = script_gen_bench._refresh_task_gt(task, live, update_from_tool=False)

        assert status == "UPDATED"
        assert stored["ground_truth"]["fn_callers"] == ["m::a"]

    def test_tool_derived_type_skipped_without_flag(self, script_gen_bench: Any) -> None:
        """A scan-derived task (coupled) is NOT refreshed under plain ``--update``; the original is preserved.

        Scenario: refreshing it from scan-query output would validate the tool against itself.
        """
        task = {"type": "code_quality", "ground_truth": {"check": "coupled", "top_dep_count": 1}}
        live = {"check": "coupled", "top_dep_count": 99}

        stored, status = script_gen_bench._refresh_task_gt(task, live, update_from_tool=False)

        assert "SKIP UPDATE" in status
        assert stored["ground_truth"]["top_dep_count"] == 1  # unchanged

    def test_tool_derived_type_refreshes_with_flag_and_warns(
        self, script_gen_bench: Any, capsys: pytest.CaptureFixture
    ) -> None:
        """``--update-from-tool`` refreshes scan-derived (coupled) ground truth after a loud circularity warning.

        Scenario: the maintainer explicitly opts into the circular refresh; it proceeds but prints the warning.
        """
        task = {"type": "code_quality", "ground_truth": {"check": "coupled", "top_dep_count": 1}}
        live = {"check": "coupled", "top_dep_count": 99}

        stored, status = script_gen_bench._refresh_task_gt(task, live, update_from_tool=True)

        assert status == "UPDATED"
        assert stored["ground_truth"]["top_dep_count"] == 99
        assert "CIRCULAR UPDATE" in capsys.readouterr().out

    def test_review_assistance_merges_sub_questions_with_flag(self, script_gen_bench: Any) -> None:
        """A review_assistance refresh (tool-derived) merges per-sub_question ground truth under the flag.

        Scenario: live per-question counts replace the stored sub-question ground truth.
        """
        task = {"type": "review_assistance", "sub_questions": [{"id": "sq1", "ground_truth": {"count": 1}}]}

        stored, status = script_gen_bench._refresh_task_gt(task, {"sq1": {"count": 5}}, update_from_tool=True)

        assert status == "UPDATED"
        assert stored["sub_questions"][0]["ground_truth"] == {"count": 5}

    def test_review_refresh_preserves_named_oracle_views(self, script_gen_bench: Any) -> None:
        """Regeneration cannot erase the AST/static semantic split from a review task.

        Scenario: a review task carries named oracle views next to its sub-questions; a refresh updates the counts
        but must leave the views untouched.
        """
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


class TestMain:
    """``main`` error paths and orchestration with discovery and validators stubbed."""

    def test_exits_when_tasks_file_unreadable(
        self, script_gen_bench: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Report a missing task manifest and stop task generation.

        Scenario: tasks-bench.json deleted; main must exit with code 1.
        """
        monkeypatch.setattr(script_gen_bench, "TASKS_FILE", tmp_path / "nonexistent.json")

        with pytest.raises(SystemExit) as exc_info:
            script_gen_bench.main(repo_path=str(tmp_path))

        assert exc_info.value.code == 1

    def test_exits_when_no_repo_path(self, script_gen_bench: Any, write_tasks: Callable[..., Path]) -> None:
        """Exit 1 when there is no ``--repo-path`` and no local_path in the tasks file.

        Scenario: the tasks file has no repo.local_path and the user forgot ``--repo-path``.
        """
        write_tasks([])

        with pytest.raises(SystemExit) as exc_info:
            script_gen_bench.main()

        assert exc_info.value.code == 1

    def test_exits_when_repo_path_not_dir(
        self, script_gen_bench: Any, tmp_path: Path, write_tasks: Callable[..., Path]
    ) -> None:
        """Exit 1 when ``--repo-path`` exists but is a file, not a directory.

        Scenario: the user passes a file path by mistake.
        """
        write_tasks([])
        file_path = tmp_path / "notadir.txt"
        file_path.write_text("content")

        with pytest.raises(SystemExit) as exc_info:
            script_gen_bench.main(repo_path=str(file_path))

        assert exc_info.value.code == 1

    @mock.patch(f"{_MODULE}.find_codemap_bin", autospec=True, return_value=None)
    def test_exits_when_scan_query_not_found(
        self, mock_find: mock.Mock, script_gen_bench: Any, tmp_path: Path, write_tasks: Callable[..., Path]
    ) -> None:
        """Exit 1 when the scan-query binary cannot be found.

        Scenario: no binary on PATH and no plugin directory fallback.
        """
        write_tasks([])

        with pytest.raises(SystemExit) as exc_info:
            script_gen_bench.main(repo_path=str(tmp_path))

        assert exc_info.value.code == 1

    def test_exits_when_index_not_found(
        self,
        script_gen_bench: Any,
        tmp_path: Path,
        write_tasks: Callable[..., Path],
        fake_discovery: tuple[Path, Path],
    ) -> None:
        """Exit 1 when the resolved index path does not exist.

        Scenario: scan-query is found but the codemap index JSON is missing.
        """
        write_tasks([])
        fake_discovery[1].unlink()

        with pytest.raises(SystemExit) as exc_info:
            script_gen_bench.main(repo_path=str(tmp_path))

        assert exc_info.value.code == 1

    @pytest.mark.usefixtures("fake_discovery")
    def test_fails_closed_on_unknown_task_type(
        self,
        script_gen_bench: Any,
        tmp_path: Path,
        write_tasks: Callable[..., Path],
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Print SKIP and exit nonzero for an unrecognised task type.

        Scenario: a missing validator must not produce a green canonical B0 run.
        """
        write_tasks([{"id": "X-01", "type": "unknown_type", "ground_truth": {}}])

        with pytest.raises(SystemExit) as exc_info:
            script_gen_bench.main(repo_path=str(tmp_path))

        out = capsys.readouterr().out
        assert exc_info.value.code == 1
        assert "SKIP" in out
        assert "X-01" in out

    @pytest.mark.usefixtures("fake_discovery")
    def test_update_aborts_on_unknown_task_type(
        self,
        script_gen_bench: Any,
        tmp_path: Path,
        write_tasks: Callable[..., Path],
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Fail update mode without rewriting a suite that contains a SKIP.

        Scenario: ``--update`` over a manifest with an unknown task type must leave the file byte-identical.
        """
        tasks_file = write_tasks([{"id": "X-01", "type": "unknown_type", "ground_truth": {}}], repo={"name": "fixture"})
        original = tasks_file.read_text()

        with pytest.raises(SystemExit) as exc_info:
            script_gen_bench.main(repo_path=str(tmp_path), update=True)

        assert exc_info.value.code == 1
        assert tasks_file.read_text() == original
        assert "Update aborted" in capsys.readouterr().out

    @pytest.mark.usefixtures("fake_discovery")
    def test_reports_pass_fail_skip_counts_separately(
        self,
        script_gen_bench: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        write_tasks: Callable[..., Path],
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Report each terminal validation state without treating a skip as a pass.

        Scenario: one supported task passes, another fails, and an unknown task skips.  The human-facing summary must
        preserve the exact distribution rather than deriving passes from ``total - failures``.
        """
        write_tasks(
            [
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
            ]
        )
        validators = {
            script_gen_bench.TaskType.SYMBOL_EXTRACTION: lambda *_args: (True, {}, ""),
            script_gen_bench.TaskType.FN_CALL_GRAPH: lambda *_args: (False, {}, "intentional failure"),
        }
        monkeypatch.setattr(script_gen_bench, "VALIDATORS", validators)

        with pytest.raises(SystemExit) as exc_info:
            script_gen_bench.main(repo_path=str(tmp_path))

        out = capsys.readouterr().out
        assert exc_info.value.code == 1
        assert "1/3 passed" in out
        assert "1 failed" in out
        assert "1 skipped" in out

    @pytest.mark.usefixtures("fake_discovery")
    def test_filters_by_task_id(
        self,
        script_gen_bench: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        write_tasks: Callable[..., Path],
    ) -> None:
        """Run only the task matching ``--task <id>`` when a filter is supplied.

        Scenario: the user passes ``--task SE-01``; only that task is validated.
        """
        write_tasks(
            [
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
            ]
        )
        validator = mock.Mock(return_value=(True, {}, ""))
        monkeypatch.setattr(script_gen_bench, "VALIDATORS", {script_gen_bench.TaskType.SYMBOL_EXTRACTION: validator})

        script_gen_bench.main(repo_path=str(tmp_path), task="SE-01")

        assert [call.args[0]["id"] for call in validator.call_args_list] == ["SE-01"]

    @pytest.mark.usefixtures("fake_discovery")
    def test_exits_when_task_id_not_found(
        self, script_gen_bench: Any, tmp_path: Path, write_tasks: Callable[..., Path]
    ) -> None:
        """Exit 1 when ``--task <id>`` does not match any task in the file.

        Scenario: the user passes a non-existent task ID.
        """
        write_tasks([{"id": "SE-01", "type": "symbol_extraction", "ground_truth": {}}])

        with pytest.raises(SystemExit) as exc_info:
            script_gen_bench.main(repo_path=str(tmp_path), task="NO-SUCH")

        assert exc_info.value.code == 1

    @pytest.mark.usefixtures("fake_discovery")
    def test_full_file_update_writes_back(
        self,
        script_gen_bench: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        write_tasks: Callable[..., Path],
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Regression: full-file ``--update`` (no ``--task``) processes every task and writes ground truth back.

        The loop variable must not shadow the ``task`` filter parameter. If it did, the ``if task is None`` write-back
        guard would never fire and the file would never be written despite ``--update``.
        """
        write_tasks(
            [
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
            ]
        )
        validator = mock.Mock(return_value=(True, {}, ""))
        monkeypatch.setattr(script_gen_bench, "VALIDATORS", {script_gen_bench.TaskType.SYMBOL_EXTRACTION: validator})

        script_gen_bench.main(repo_path=str(tmp_path), update=True)

        assert [call.args[0]["id"] for call in validator.call_args_list] == ["A-01", "A-02"]  # loop iterated all tasks
        assert "Wrote updated ground truth" in capsys.readouterr().out  # write-back guard fired

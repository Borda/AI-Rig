"""Regression contracts for frozen Codemap query execution.

``SCAN_NO_AUTOBUILD=1`` is the benchmark/CI opt-out for query-time index mutation. These tests keep its public contract
separate from the interactive self-heal tests: the existing index is answered exactly as stored, stale metadata remains
honest, and no incremental scan is launched.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_CODEMAP_CLI = Path(__file__).resolve().parents[2] / "bin" / "codemap-py"
_PYTHON_311 = shutil.which("python3.11")


def _git(root: Path, *args: str) -> None:
    """Run a git command in the disposable test repository."""
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def _scan(scan_index: Path, root: Path) -> Path:
    """Build and return the Codemap index for the disposable repository."""
    result = subprocess.run(
        [sys.executable, str(scan_index), "--root", str(root)],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return root / ".cache" / "codemap" / f"{root.name}.json"


def _supported_codemap_env() -> dict[str, str]:
    """Return an environment that makes the public launcher use CPython 3.11."""
    assert _PYTHON_311 is not None
    return {**os.environ, "CODEMAP_PYTHON": _PYTHON_311}


@pytest.fixture(name="stale_git_project")
def _stale_git_project(tmp_path: Path, scan_index: Path) -> tuple[Path, Path]:
    """Create a stale index whose stored graph lacks one committed importer."""
    root = tmp_path / "frozen-query"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Codemap test")
    (root / "leaf.py").write_text("def target(value):\n    return value\n")
    (root / "caller.py").write_text("import leaf\n\n\ndef call(value):\n    return leaf.target(value)\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial indexed graph")
    index_path = _scan(scan_index, root)

    (root / "new_caller.py").write_text("import leaf\n\n\ndef added_call(value):\n    return leaf.target(value)\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "new importer after index")
    return root, index_path


def test_frozen_query_preserves_stale_index_without_incremental_refresh(
    stale_git_project: tuple[Path, Path], scan_query: Path
) -> None:
    """The frozen opt-out answers the stored graph and never self-heals it.

    Prevents benchmark query time from silently including an index refresh. A plausibly wrong implementation that merely
    suppresses the completion message still fails because the newly committed importer must remain absent and the result
    must declare itself stale.
    """
    root, index_path = stale_git_project
    env = {**os.environ, "SCAN_NO_AUTOBUILD": "1"}

    result = subprocess.run(
        [sys.executable, str(scan_query), "--index", str(index_path), "rdeps", "leaf"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert "new_caller" not in data["imported_by"]
    assert data["index"]["stale"] is True
    assert "self-healed index" not in result.stderr
    assert "SCAN_NO_AUTOBUILD=1" not in result.stderr


def test_frozen_query_reports_a_missing_index_without_building(tmp_path: Path, scan_query: Path) -> None:
    """Frozen execution rejects a missing index with an explicit repair action.

    Prevents a future query launcher from hiding an index build behind the benchmark opt-out. A generic missing-file
    error is insufficient because it does not tell the caller that the opt-out caused the refusal.
    """
    root = tmp_path / "missing-frozen-index"
    root.mkdir()
    env = {**os.environ, "SCAN_NO_AUTOBUILD": "1"}
    missing_index = root / ".cache" / "codemap" / "missing-frozen-index.json"

    result = subprocess.run(
        [sys.executable, str(scan_query), "--index", str(missing_index), "list"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "SCAN_NO_AUTOBUILD=1" in result.stdout
    assert "scan-codebase" in result.stdout


@pytest.fixture(name="wide_reverse_graph")
def _wide_reverse_graph(tmp_path: Path, scan_index: Path) -> tuple[Path, Path, set[str], set[str]]:
    """Build a graph exceeding historical display limits in both reverse directions."""
    root = tmp_path / "wide-reverse-graph"
    root.mkdir()
    (root / "leaf.py").write_text("def target(value):\n    return value\n")
    expected_modules: set[str] = set()
    expected_callers: set[str] = set()
    for number in range(105):
        module = f"caller_{number:03d}"
        function = f"call_{number:03d}"
        expected_modules.add(module)
        expected_callers.add(f"{module}::{function}")
        (root / f"{module}.py").write_text(f"import leaf\n\n\ndef {function}(value):\n    return leaf.target(value)\n")
    return root, _scan(scan_index, root), expected_modules, expected_callers


def test_rdeps_limit_zero_preserves_the_exhaustive_result(
    wide_reverse_graph: tuple[Path, Path, set[str], set[str]],
    scan_query: Path,
) -> None:
    """Rdeps accepts ``--limit 0`` as the byte-identical exhaustive route.

    Prevents the preview option from changing the established default result when explicitly disabled. Exact output
    equality catches an omitted importer or any unrequested metadata change.
    """
    root, index_path, expected_modules, _expected_callers = wide_reverse_graph
    default = subprocess.run(
        [sys.executable, str(scan_query), "--index", str(index_path), "rdeps", "leaf"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    explicit = subprocess.run(
        [sys.executable, str(scan_query), "--index", str(index_path), "rdeps", "leaf", "--limit", "0"],
        cwd=root,
        capture_output=True,
        text=True,
    )

    assert default.returncode == explicit.returncode == 0, explicit.stderr
    assert explicit.stdout == default.stdout
    assert set(json.loads(explicit.stdout)["imported_by"]) == expected_modules


def test_fn_rdeps_rejects_unsupported_limit_argument(
    wide_reverse_graph: tuple[Path, Path, set[str], set[str]], scan_query: Path
) -> None:
    """Fn-rdeps rejects the rdeps-only preview option rather than ignoring it."""
    root, index_path, _expected_modules, _expected_callers = wide_reverse_graph
    result = subprocess.run(
        [sys.executable, str(scan_query), "--index", str(index_path), "fn-rdeps", "leaf::target", "--limit", "0"],
        cwd=root,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "unrecognized arguments: --limit 0" in result.stderr


def test_reverse_queries_return_all_names_without_a_limit(
    wide_reverse_graph: tuple[Path, Path, set[str], set[str]], scan_query: Path
) -> None:
    """Rdeps and fn-rdeps preserve every stored name above historical display caps.

    Prevents a compact-output optimization from silently truncating either reverse graph. Set equality catches both an
    omitted tail and an unexpected name, unlike a weak ``count >= 100`` assertion.
    """
    root, index_path, expected_modules, expected_callers = wide_reverse_graph

    rdeps = subprocess.run(
        [sys.executable, str(scan_query), "--index", str(index_path), "rdeps", "leaf"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    fn_rdeps = subprocess.run(
        [sys.executable, str(scan_query), "--index", str(index_path), "fn-rdeps", "leaf::target"],
        cwd=root,
        capture_output=True,
        text=True,
    )

    assert rdeps.returncode == 0, rdeps.stderr
    assert fn_rdeps.returncode == 0, fn_rdeps.stderr
    assert set(json.loads(rdeps.stdout)["imported_by"]) == expected_modules
    fn_payload = json.loads(fn_rdeps.stdout)
    assert {entry["caller"] for entry in fn_payload["called_by"]} == expected_callers
    assert fn_payload["count"] == len(expected_callers)


@pytest.mark.skipif(_PYTHON_311 is None, reason="codemap-py public-launcher test needs CPython 3.11 on PATH")
def test_compact_reverse_queries_only_reduce_metadata_not_result_arrays(
    wide_reverse_graph: tuple[Path, Path, set[str], set[str]],
) -> None:
    """The public compact mode preserves all reverse names and required honesty fields.

    Prevents a compact-output implementation from treating data arrays as disposable metadata. The explicit full-mode
    comparison protects the default output contract while the 105-entry graph detects a hidden display cap.
    """
    root, index_path, expected_modules, expected_callers = wide_reverse_graph
    env = _supported_codemap_env()
    compact_rdeps = subprocess.run(
        [str(_CODEMAP_CLI), "query", "--compact", "--index", str(index_path), "rdeps", "leaf"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )
    compact_fn_rdeps = subprocess.run(
        [str(_CODEMAP_CLI), "query", "--compact", "--index", str(index_path), "fn-rdeps", "leaf::target"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )
    default_rdeps = subprocess.run(
        [str(_CODEMAP_CLI), "query", "--index", str(index_path), "rdeps", "leaf"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )

    assert compact_rdeps.returncode == 0, compact_rdeps.stderr
    assert compact_fn_rdeps.returncode == 0, compact_fn_rdeps.stderr
    assert default_rdeps.returncode == 0, default_rdeps.stderr
    compact_rdeps_payload = json.loads(compact_rdeps.stdout)
    compact_fn_payload = json.loads(compact_fn_rdeps.stdout)
    default_rdeps_payload = json.loads(default_rdeps.stdout)
    assert set(compact_rdeps_payload["imported_by"]) == expected_modules
    assert {entry["caller"] for entry in compact_fn_payload["called_by"]} == expected_callers
    assert compact_fn_payload["count"] == len(expected_callers)
    for payload in (compact_rdeps_payload, compact_fn_payload):
        coverage = payload["index"]
        assert coverage["compact"] is True
        assert coverage["query_complete"] is True
        assert coverage["stale"] is False
        assert coverage["root_mismatch"] is False
        assert coverage["method"] in {"import-graph", "static-ast"}
    assert "compact" not in default_rdeps_payload["index"]
    assert "total_modules" in default_rdeps_payload["index"]
    assert set(default_rdeps_payload["imported_by"]) == expected_modules


@pytest.mark.skipif(_PYTHON_311 is None, reason="codemap-py public-launcher test needs CPython 3.11 on PATH")
def test_compact_stale_query_keeps_the_incompleteness_reason(stale_git_project: tuple[Path, Path]) -> None:
    """Compact metadata must retain the stale veto rather than imply a complete result.

    Prevents the opt-in diet from dropping the one field that explains why a frozen answer is incomplete. This is a
    distinct behavior from the complete reverse-result test above, so it uses one stale graph and one command.
    """
    root, index_path = stale_git_project
    env = {**_supported_codemap_env(), "SCAN_NO_AUTOBUILD": "1"}
    result = subprocess.run(
        [str(_CODEMAP_CLI), "query", "--compact", "--index", str(index_path), "rdeps", "leaf"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    coverage = payload["index"]
    assert "new_caller" not in payload["imported_by"]
    assert coverage["compact"] is True
    assert coverage["query_complete"] is False
    assert coverage["stale"] is True
    assert coverage["root_mismatch"] is False
    assert coverage["completeness_reason"] == "stale"
    assert coverage["method"] == "import-graph"
    assert "stale" in coverage["note"]

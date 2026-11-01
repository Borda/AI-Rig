"""Regression contracts for frozen Codemap query execution.

``SCAN_NO_AUTOBUILD=1`` is the benchmark/CI opt-out for query-time index mutation. These tests keep its public contract
separate from the interactive self-heal tests: the existing index is answered exactly as stored, stale metadata remains
honest, and no incremental scan is launched. They also pin reverse-query completeness (no display cap) and the compact
output contract. Every test drives the real ``scan-query`` script or public launcher in a subprocess: the CLI is the
contract, so nothing here is mocked.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

_CODEMAP_CLI = Path(__file__).resolve().parents[2] / "bin" / "codemap-py"
_PYTHON_311 = shutil.which("python3.11")
# Overrides only: merged over os.environ at call time so per-test monkeypatched variables (telemetry off) still apply.
_FROZEN_ENV = {"SCAN_NO_AUTOBUILD": "1"}


@dataclass(frozen=True)
class WideGraph:
    """A scanned project whose ``leaf::target`` has more importers and callers than any historical display cap."""

    root: Path
    index: Path
    modules: set[str]
    callers: set[str]


def _git(root: Path, *args: str) -> None:
    """Run a git command in the disposable test repository."""
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def _scan(scan_index: Path, root: Path) -> Path:
    """Build and return the Codemap index for the disposable repository."""
    result = subprocess.run(
        [sys.executable, str(scan_index), "--root", str(root)], cwd=root, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    return root / ".cache" / "codemap" / f"{root.name}.json"


def _scan_query(scan_query: Path, graph_root: Path, *args: str, env: dict[str, str] | None = None):
    """Run ``scan-query`` with ``args`` from ``graph_root`` and capture its output."""
    return subprocess.run(
        [sys.executable, str(scan_query), *args],
        cwd=graph_root,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
    )


def _launcher_query(graph_root: Path, *args: str, env: dict[str, str] | None = None):
    """Run the public ``codemap-py query`` launcher under CPython 3.11 from ``graph_root``."""
    assert _PYTHON_311 is not None
    launcher_env = {**os.environ, "CODEMAP_PYTHON": _PYTHON_311, **(env or {})}
    return subprocess.run(
        [str(_CODEMAP_CLI), "query", *args], cwd=graph_root, env=launcher_env, capture_output=True, text=True
    )


def _assert_compact_complete(coverage: dict) -> None:
    """Assert the compact ``index`` block marks a fresh, complete, root-matching reverse result."""
    assert coverage["compact"] is True
    assert coverage["query_complete"] is True
    assert coverage["stale"] is False
    assert coverage["root_mismatch"] is False
    assert coverage["method"] in {"import-graph", "static-ast"}


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


@pytest.fixture(name="wide_reverse_graph")
def _wide_reverse_graph(tmp_path: Path, scan_index: Path) -> WideGraph:
    """Build a graph exceeding historical display limits in both reverse directions."""
    root = tmp_path / "wide-reverse-graph"
    root.mkdir()
    (root / "leaf.py").write_text("def target(value):\n    return value\n")
    modules: set[str] = set()
    callers: set[str] = set()
    for number in range(105):
        module = f"caller_{number:03d}"
        function = f"call_{number:03d}"
        modules.add(module)
        callers.add(f"{module}::{function}")
        (root / f"{module}.py").write_text(f"import leaf\n\n\ndef {function}(value):\n    return leaf.target(value)\n")
    return WideGraph(root=root, index=_scan(scan_index, root), modules=modules, callers=callers)


class TestFrozenExecution:
    """``SCAN_NO_AUTOBUILD=1`` answers the stored index as-is and never builds or heals one."""

    def test_preserves_stale_index_without_incremental_refresh(self, stale_git_project, scan_query):
        """The frozen opt-out answers the stored graph and never self-heals it.

        Prevents benchmark query time from silently including an index refresh. A plausibly wrong implementation that
        merely suppresses the completion message still fails because the newly committed importer must remain absent and
        the result must declare itself stale.
        """
        root, index_path = stale_git_project

        result = _scan_query(scan_query, root, "--index", str(index_path), "rdeps", "leaf", env=_FROZEN_ENV)

        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert "new_caller" not in data["imported_by"]
        assert data["index"]["stale"] is True
        assert "self-healed index" not in result.stderr
        assert "SCAN_NO_AUTOBUILD=1" not in result.stderr

    def test_reports_missing_index_without_building(self, tmp_path, scan_query):
        """Frozen execution rejects a missing index with an explicit repair action.

        Prevents a future query launcher from hiding an index build behind the benchmark opt-out. A generic missing-file
        error is insufficient because it does not tell the caller that the opt-out caused the refusal.
        """
        root = tmp_path / "missing-frozen-index"
        root.mkdir()
        missing_index = root / ".cache" / "codemap" / "missing-frozen-index.json"

        result = _scan_query(scan_query, root, "--index", str(missing_index), "list", env=_FROZEN_ENV)

        assert result.returncode != 0
        assert "SCAN_NO_AUTOBUILD=1" in result.stdout
        assert "scan-codebase" in result.stdout


class TestReverseQueries:
    """Reverse queries over a 105-importer graph return every stored name and reject rdeps-only options."""

    def test_rdeps_limit_zero_preserves_exhaustive_result(self, wide_reverse_graph, scan_query):
        """Rdeps accepts ``--limit 0`` as the byte-identical exhaustive route.

        Prevents the preview option from changing the established default result when explicitly disabled. Exact output
        equality catches an omitted importer or any unrequested metadata change.
        """
        graph = wide_reverse_graph
        default = _scan_query(scan_query, graph.root, "--index", str(graph.index), "rdeps", "leaf")

        explicit = _scan_query(scan_query, graph.root, "--index", str(graph.index), "rdeps", "leaf", "--limit", "0")

        assert default.returncode == explicit.returncode == 0, explicit.stderr
        assert explicit.stdout == default.stdout
        assert set(json.loads(explicit.stdout)["imported_by"]) == graph.modules

    def test_fn_rdeps_rejects_unsupported_limit_argument(self, wide_reverse_graph, scan_query):
        """Fn-rdeps rejects the rdeps-only preview option rather than ignoring it."""
        graph = wide_reverse_graph

        result = _scan_query(
            scan_query, graph.root, "--index", str(graph.index), "fn-rdeps", "leaf::target", "--limit", "0"
        )

        assert result.returncode == 2
        assert "unrecognized arguments: --limit 0" in result.stderr

    def test_rdeps_returns_all_importers_without_limit(self, wide_reverse_graph, scan_query):
        """Rdeps preserves every stored importer above historical display caps.

        Prevents a compact-output optimization from silently truncating the module reverse graph. Set equality catches
        both an omitted tail and an unexpected name, unlike a weak ``count >= 100`` assertion.
        """
        graph = wide_reverse_graph

        result = _scan_query(scan_query, graph.root, "--index", str(graph.index), "rdeps", "leaf")

        assert result.returncode == 0, result.stderr
        assert set(json.loads(result.stdout)["imported_by"]) == graph.modules

    def test_fn_rdeps_returns_all_callers_without_limit(self, wide_reverse_graph, scan_query):
        """Fn-rdeps preserves every stored caller above historical display caps and counts them all.

        Same guard as the module reverse graph, for the function-level graph: the caller set and ``count`` must both
        equal the 105 generated callers.
        """
        graph = wide_reverse_graph

        result = _scan_query(scan_query, graph.root, "--index", str(graph.index), "fn-rdeps", "leaf::target")

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert {entry["caller"] for entry in payload["called_by"]} == graph.callers
        assert payload["count"] == len(graph.callers)


@pytest.mark.skipif(_PYTHON_311 is None, reason="codemap-py public-launcher test needs CPython 3.11 on PATH")
class TestPublicLauncher:
    """The public ``codemap-py query`` launcher keeps result arrays whole and honesty metadata present."""

    def test_compact_rdeps_reduces_only_metadata(self, wide_reverse_graph):
        """Compact ``rdeps`` preserves all importer names and required honesty fields.

        Prevents a compact-output implementation from treating data arrays as disposable metadata. The 105-entry graph
        detects a hidden display cap.
        """
        graph = wide_reverse_graph

        result = _launcher_query(graph.root, "--compact", "--index", str(graph.index), "rdeps", "leaf")

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert set(payload["imported_by"]) == graph.modules
        _assert_compact_complete(payload["index"])

    def test_compact_fn_rdeps_reduces_only_metadata(self, wide_reverse_graph):
        """Compact ``fn-rdeps`` preserves all caller names, their count and required honesty fields.

        Function-level counterpart of the compact ``rdeps`` case: the callers array and ``count`` are result data, not
        metadata the diet may drop.
        """
        graph = wide_reverse_graph

        result = _launcher_query(graph.root, "--compact", "--index", str(graph.index), "fn-rdeps", "leaf::target")

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert {entry["caller"] for entry in payload["called_by"]} == graph.callers
        assert payload["count"] == len(graph.callers)
        _assert_compact_complete(payload["index"])

    def test_default_rdeps_keeps_full_metadata(self, wide_reverse_graph):
        """Default reverse queries retain full metadata and module names after compact cases are split."""
        graph = wide_reverse_graph

        result = _launcher_query(graph.root, "--index", str(graph.index), "rdeps", "leaf")

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert "compact" not in payload["index"]
        assert "total_modules" in payload["index"]
        assert set(payload["imported_by"]) == graph.modules

    def test_compact_stale_query_keeps_incompleteness_reason(self, stale_git_project):
        """Compact metadata must retain the stale veto rather than imply a complete result.

        Prevents the opt-in diet from dropping the one field that explains why a frozen answer is incomplete. This is a
        distinct behavior from the complete reverse-result tests above, so it uses one stale graph and one command.
        """
        root, index_path = stale_git_project

        result = _launcher_query(root, "--compact", "--index", str(index_path), "rdeps", "leaf", env=_FROZEN_ENV)

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

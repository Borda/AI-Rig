"""Extraction parity: graph / coverage / test-impact — graph engine extraction.

Proves the cross-module graph-construction / coverage / scan-orchestration
move into :mod:`codemap_py.graph` preserved behavior exactly, by comparing the
pre-extraction monolithic ``bin/scan-index`` (checked out from ``HEAD``) against
the current thin ``bin/scan-index`` launcher (delegating to
:func:`codemap_py.graph.main`). Where ``test_extraction_parity_scanner_discovery.py`` asserts whole-index
parity as evidence for the scanner's per-file extraction, this file additionally
pins the *graph-layer aggregates* explicitly — the fields ``scan()``/
``incremental_scan()`` compute from already-parsed modules — so a regression
confined to graph construction (as opposed to per-file parsing) cannot hide
behind an incidental full-JSON match:

- ``fixture_rdep_count`` (pytest fixture dependency graph, built from
  ``conftest.py`` + a consuming test module) — the data ``scan-query test-impact`` reads to find tests affected by a
  change;
- ``subprocess_rdep_count`` (subprocess-call edges resolved to sibling
  modules);
- ``sphinx_xref_count`` / ``doc_xrefs`` (``.rst`` cross-reference scan);
- ``module_aliases`` and ``collisions``/dedup winner selection across a
  colliding module tree;
- per-symbol ``coverage_pct``/``covered_by`` annotation from a real
  ``.coverage`` SQLite file (``--with-coverage``).

SCOPE NOTE — test-impact: the ``scan-query test-impact`` *command* itself lives
in :mod:`codemap_py.query` (owned by a parallel Phase 3 agent; out of this
file's ownership boundary — see ``bin/scan-query``, ``query.py``, ``cli.py``
in the exclusion list). This file instead pins the graph-layer data that
command depends on (``fixture_rdep_count``, per-module ``mock_patches``,
``subprocess_rdep_count``) — full parity there is the correctness precondition
for the query-level command, and is squarely inside this file's scanner/graph
ownership.

The old-bin checkout, path classes, index normalisation, scan runner and feature-rich fixture project are shared with
``test_extraction_parity_scanner_discovery.py`` through ``tests/_parity_helpers.py``.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path

import pytest
from _parity_helpers import (
    NEW_SCAN_INDEX,
    PATH_CLASSES,
    ScanPair,
    assert_v13_index_delta,
    build_fixture_project,
    corrupt_fingerprint,
    corrupt_input_hash,
    corrupt_project,
    materialize_old_bin,
    run_scan,
    scan_old_and_new,
)


@pytest.fixture(name="old_scan_index", scope="module")
def _old_scan_index(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Session-stable path to the golden pre-extraction ``scan-index``."""
    return materialize_old_bin(tmp_path_factory.mktemp("old_bin_graph_coverage_testimpact"), "scan-index")


@pytest.mark.parametrize(
    "corrupt",
    [
        pytest.param(corrupt_project, id="legacy-content"),
        pytest.param(corrupt_input_hash, id="input-hash"),
        pytest.param(corrupt_fingerprint, id="fingerprint"),
    ],
)
def test_v13_normalization_rejects_unrelated_legacy_change(
    scan_pair: ScanPair, corrupt: Callable[[dict], None]
) -> None:
    """The declared metadata delta cannot hide content, input-hash or fingerprint corruption.

    A real old-vs-new index pair passes the delta check; corrupting one field of a copy of the new index must then make
    the same check fail, proving the normalisation is not so loose that it accepts unrelated drift.
    """
    assert_v13_index_delta(scan_pair.old_index, scan_pair.new_index)
    new = copy.deepcopy(scan_pair.new_index)
    corrupt(new)

    with pytest.raises(AssertionError):
        assert_v13_index_delta(scan_pair.old_index, new)


def _build_colliding_fixture(root: Path) -> None:
    """Root-level ``pkg/`` plus a nested ``wt/pkg/`` duplicate — forces a dedup collision.

    Mirrors ``conftest.py``'s proven ``_materialize_polluted_tree(with_collision=True)``
    shape: a canonical top-level ``pkg`` package and a second, non-excluded
    ``wt/pkg`` copy whose qualnames collide with it. Two *independent*
    top-level package dirs (e.g. ``a/pkg`` and ``wt/pkg``) do not collide —
    each is its own detected src root — so the nesting shape here matters.
    """
    pkg = root / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "leaf.py").write_text("def leaf_fn(x):\n    return x\n")

    wt_pkg = root / "wt" / "pkg"
    wt_pkg.mkdir(parents=True)
    (wt_pkg / "__init__.py").write_text("")
    (wt_pkg / "leaf.py").write_text("def leaf_fn(x):\n    return x\n")


@pytest.fixture(name="scan_pair", scope="module", params=PATH_CLASSES)
def _scan_pair(
    tmp_path_factory: pytest.TempPathFactory, old_scan_index: Path, request: pytest.FixtureRequest
) -> ScanPair:
    """Scan the feature-rich fixture once with the golden monolith and once with the current launcher.

    Both engines scan the identical root path into the identical index directory sequentially, so path-dependent output
    is truly comparable. The result is immutable evidence shared by every read-only comparison; tests that corrupt an
    index must deep-copy it first.
    """
    return scan_old_and_new(tmp_path_factory.mktemp("graph_pair"), request.param, old_scan_index)


_GRAPH_AGGREGATE_FIELDS = [
    "fixture_rdep_count",
    "subprocess_rdep_count",
    "sphinx_xref_count",
    "doc_xrefs",
    "module_aliases",
    "collisions",
    "excluded_roots",
    "src_layout",
    "src_roots",
]


# --- graph-layer aggregates: full scan across both path classes ------------


@pytest.mark.parametrize("field", _GRAPH_AGGREGATE_FIELDS)
def test_graph_aggregate_matches_old_vs_new(scan_pair: ScanPair, field: str) -> None:
    """One graph-layer aggregate field is identical between the golden monolith and the current engine.

    Whole-index parity is asserted separately; checking each graph-owned aggregate on its own means a regression
    confined to graph construction names the field that diverged instead of hiding behind an incidental full-JSON match.
    """
    assert scan_pair.old_index[field] == scan_pair.new_index[field], f"graph aggregate {field!r} diverged"


def test_whole_index_old_vs_new_agrees_apart_from_v13_delta(scan_pair: ScanPair) -> None:
    """A full scan of the feature-rich fixture agrees old-vs-new in exit code, stderr and the whole index.

    Runs for a plain and a non-ASCII project directory name; only the declared v12/v13 additions may differ.
    """
    assert scan_pair.old_result.returncode == scan_pair.new_result.returncode == 0
    assert scan_pair.old_result.stderr == scan_pair.new_result.stderr
    assert_v13_index_delta(scan_pair.old_index, scan_pair.new_index)


# --- dedup / collision resolution across a colliding module tree -----------


def test_dedup_collision_resolution_identical(tmp_path: Path, old_scan_index: Path) -> None:
    """Two colliding top-level ``pkg`` trees dedup to the same winner old-vs-new."""
    root = tmp_path / "proj"
    root.mkdir()
    _build_colliding_fixture(root)
    index_dir = tmp_path / "idx"

    old_result, old_index = run_scan(old_scan_index, root, index_dir)
    new_result, new_index = run_scan(NEW_SCAN_INDEX, root, index_dir)

    assert old_result.returncode == new_result.returncode == 0
    assert old_index is not None
    assert new_index is not None
    assert old_index["collisions"] == new_index["collisions"]
    assert len(old_index["collisions"]) > 0, "fixture must actually force a collision"
    old_names = sorted(m["name"] for m in old_index["modules"])
    new_names = sorted(m["name"] for m in new_index["modules"])
    assert old_names == new_names
    assert_v13_index_delta(old_index, new_index)


# --- coverage annotation (``--with-coverage``) ---------------------------------


def _record_coverage(module_path: Path, cov_path: Path) -> None:
    """Execute *module_path* under real ``coverage`` instrumentation, saving to *cov_path*."""
    import coverage

    cov = coverage.Coverage(data_file=str(cov_path))
    cov.start()
    src = module_path.read_text()
    code = compile(src, str(module_path), "exec")
    namespace = {"__file__": str(module_path), "__name__": "pkg.leaf"}
    exec(code, namespace)  # noqa: S102  (test-only, fixed local source, not user input)
    namespace["leaf_fn"](1)
    cov.stop()
    cov.save()


def test_coverage_annotation_identical_old_vs_new(tmp_path: Path, old_scan_index: Path) -> None:
    """Per-symbol coverage_pct/covered_by annotation from a real ``.coverage`` file agrees old-vs-new."""
    root = tmp_path / "proj"
    root.mkdir()
    build_fixture_project(root)
    cov_path = tmp_path / ".coverage"
    _record_coverage(root / "pkg" / "leaf.py", cov_path)
    index_dir = tmp_path / "idx"

    old_result, old_index = run_scan(old_scan_index, root, index_dir, "--with-coverage", str(cov_path))
    new_result, new_index = run_scan(NEW_SCAN_INDEX, root, index_dir, "--with-coverage", str(cov_path))

    assert old_result.returncode == new_result.returncode == 0
    assert old_index is not None
    assert new_index is not None
    old_leaf = next(m for m in old_index["modules"] if m["name"] == "pkg.leaf")
    new_leaf = next(m for m in new_index["modules"] if m["name"] == "pkg.leaf")
    assert old_leaf.get("symbols") == new_leaf.get("symbols")
    assert any("coverage_pct" in s for s in old_leaf.get("symbols", [])), (
        "fixture must actually exercise coverage annotation"
    )
    assert_v13_index_delta(old_index, new_index)


# --- test-impact's underlying graph data (fixture graph + mock patches) ----


def _mock_patches_by_module(index: dict) -> dict:
    """Map indexed module names to their recorded mock-patch metadata."""
    root = Path(index["scan_root"])
    return {
        m["name"]: [
            {**patch, "file": Path(patch["file"]).relative_to(root).as_posix()} for patch in m.get("mock_patches", [])
        ]
        for m in index["modules"]
    }


def test_fixture_graph_and_mock_patch_data_identical(scan_pair: ScanPair) -> None:
    """The pytest-fixture graph and per-module mock_patches feeding scan-query test-impact agree.

    See module SCOPE NOTE: the ``test-impact`` command itself is query.py's (out of ownership); this pins the
    graph-layer inputs it reads.
    """
    old_mocks = _mock_patches_by_module(scan_pair.old_index)

    assert scan_pair.old_result.returncode == scan_pair.new_result.returncode == 0
    assert scan_pair.old_index["fixture_rdep_count"] == scan_pair.new_index["fixture_rdep_count"]
    assert old_mocks == _mock_patches_by_module(scan_pair.new_index)
    assert old_mocks.get("tests.test_pkg"), "fixture must actually exercise mock-patch extraction"

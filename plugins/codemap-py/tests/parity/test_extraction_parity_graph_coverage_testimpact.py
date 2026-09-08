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

FILE OWNERSHIP NOTE: duplicates the old-bin checkout / path-classes / JSON
canonicalization helpers from ``test_extraction_parity_scanner_discovery.py`` rather than factoring
them into ``conftest.py`` — see that file's matching note for why.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parents[1]
_PLUGIN_ROOT = _TESTS_DIR.parent
_NEW_SCAN_INDEX = _PLUGIN_ROOT / "bin" / "scan-index"
_PARITY_GOLDEN_DIR = _TESTS_DIR / "data" / "parity_golden"

# Frozen as static fixtures under tests/data/parity_golden/ rather than read via
# `git show HEAD:...` — a HEAD-pinned checkout silently degrades to the
# post-extraction thin launcher once the extraction commit lands (HEAD then
# advances past the monolith), and is simply absent in a shallow CI clone that
# never fetched the old SHA. See _assert_is_monolith for the loud-failure guard
# against a golden fixture being accidentally overwritten in kind.
_OLD_BIN_FILES = ("scan-index", "_exclusions.py", "_schema.py", "_telemetry.py")
_VOLATILE_KEYS = ("scanned_at", "git_sha")
_V12_ROOT_ADDITIONS = frozenset({"symbol_aliases", "symbol_alias_limitations"})
_V12_MODULE_ADDITIONS = frozenset({"symbol_aliases", "symbol_alias_limitations"})
_V13_MODULE_ADDITIONS = frozenset({"unresolved_direct_imports", "from_import_submodules"})

# The monolith is roughly 10x-100x larger than its post-extraction thin
# launcher/shim counterpart; thresholds sit with headroom below the monolith
# line count and well above the current thin-file line count.
_MONOLITH_MIN_LINES = {
    "scan-index": 500,
    "_exclusions.py": 100,
    "_schema.py": 50,
    "_telemetry.py": 50,
}

# Substrings present in every current (post-extraction) bin/ shim or launcher
# docstring, absent from every pre-extraction monolith source — their presence
# in a golden fixture means it was accidentally overwritten with
# post-extraction content instead of holding the frozen monolith.
_POST_EXTRACTION_MARKERS = ("codemap_py", "thin launcher", "compatibility shim")

_PATH_CLASSES = ["proj", "proj café ünïcode dir"]


def _assert_is_monolith(name: str, content: str) -> None:
    """Fail loudly if *content* looks like the post-extraction shim, not the golden monolith."""
    lines = content.splitlines()
    assert len(lines) > _MONOLITH_MIN_LINES[name], (
        f"parity_golden/{name} has only {len(lines)} lines (expected > {_MONOLITH_MIN_LINES[name]}) "
        "— looks like the post-extraction thin bin/ file was committed in place of the "
        "pre-extraction monolith golden"
    )
    for marker in _POST_EXTRACTION_MARKERS:
        assert marker not in content, (
            f"parity_golden/{name} contains {marker!r}, which only appears in post-extraction "
            "bin/ sources — the golden fixture was accidentally overwritten with the thin "
            "launcher/shim instead of the monolith"
        )


def _materialize_old_bin(dest: Path) -> Path:
    """Copy the frozen pre-extraction monolithic ``scan-index`` + siblings into *dest*."""
    for name in _OLD_BIN_FILES:
        content = (_PARITY_GOLDEN_DIR / name).read_text()
        _assert_is_monolith(name, content)
        (dest / name).write_text(content)
    return dest / "scan-index"


@pytest.fixture(name="old_scan_index", scope="module")
def _old_scan_index(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Session-stable path to the golden pre-extraction ``scan-index``."""
    return _materialize_old_bin(tmp_path_factory.mktemp("old_bin_graph_coverage_testimpact"))


def _legacy_index(index: dict) -> dict:
    """Normalize v12/v13 additions while restoring legacy import metrics.

    >>> _legacy_index({"modules": []})
    {'modules': []}
    """
    modules = []
    for module in index["modules"]:
        normalized = dict(module)
        raw_imports = normalized.get("unresolved_direct_imports")
        if raw_imports is not None:
            normalized["direct_imports"] = raw_imports
            normalized["dep_count"] = len(raw_imports)
        if normalized.get("status") != "ok" and not normalized.get("direct_imports"):
            normalized.pop("direct_imports", None)
            normalized.pop("dep_count", None)
        normalized = {
            key: value for key, value in normalized.items() if key not in _V12_MODULE_ADDITIONS | _V13_MODULE_ADDITIONS
        }
        modules.append(normalized)
    legacy = {
        key: value
        for key, value in index.items()
        if key not in _VOLATILE_KEYS and key not in _V12_ROOT_ADDITIONS and key != "scan_version"
    }
    legacy["modules"] = modules
    return legacy


def _canonical(index: dict) -> str:
    """Serialize an index with stable key ordering for parity assertions.

    >>> _canonical({"b": 2, "a": 1})
    '{"a": 1, "b": 2}'
    """
    return json.dumps(index, sort_keys=True, ensure_ascii=False)


def _assert_v13_index_delta(old: dict, new: dict) -> None:
    """Prove frozen v11 content is identical except declared v12/v13 graph semantics."""
    assert old["scan_version"] == 11
    assert new["scan_version"] == 13
    assert set(new) == set(old) | _V12_ROOT_ADDITIONS
    assert len(old["modules"]) == len(new["modules"])
    for legacy_module, current_module in zip(old["modules"], new["modules"], strict=True):
        additions = _V12_MODULE_ADDITIONS | _V13_MODULE_ADDITIONS if current_module["status"] == "ok" else frozenset()
        current_keys = set(current_module)
        if (
            current_module["status"] != "ok"
            and current_module.get("direct_imports") == []
            and "direct_imports" not in legacy_module
        ):
            current_keys.remove("direct_imports")
        assert current_keys == set(legacy_module) | additions
        if additions:
            assert isinstance(current_module["symbol_aliases"], dict)
            assert isinstance(current_module["symbol_alias_limitations"], list)
    assert isinstance(new["symbol_aliases"], dict)
    assert isinstance(new["symbol_alias_limitations"], list)
    assert _canonical(_legacy_index(old)) == _canonical(_legacy_index(new))


def test_v13_normalization_rejects_unrelated_legacy_change() -> None:
    """The v13 allowance cannot mask a changed pre-v12 field."""
    old = {"scan_version": 11, "project": "legacy", "modules": [{"name": "mod", "status": "ok"}]}
    new = {
        "scan_version": 13,
        "project": "changed",
        "modules": [{"name": "mod", "status": "ok", "symbol_aliases": {}, "symbol_alias_limitations": []}],
        "symbol_aliases": {},
        "symbol_alias_limitations": [],
    }

    with pytest.raises(AssertionError):
        _assert_v13_index_delta(old, new)


def _run_scan(
    executable: Path, root: Path, index_dir: Path, *extra: str
) -> tuple[subprocess.CompletedProcess, dict | None]:
    """Run a ``scan-index``-shaped executable against *root*, return (result, index-or-None).

    ``PYTHONUTF8=1`` + ``encoding="utf-8"`` (rather than bare ``text=True``) pin stdio decoding to UTF-8 regardless of
    the host's console codepage — otherwise a non-ASCII *root* embedded in stderr decodes via the Windows console
    codepage and the two compared child processes can diverge (mojibake vs. clean UTF-8).
    """
    index_dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "CODEMAP_LOGGING": "false", "CODEMAP_INDEX_DIR": str(index_dir), "PYTHONUTF8": "1"}
    result = subprocess.run(
        [sys.executable, str(executable), "--root", str(root), *extra],
        cwd=str(root),
        env=env,
        capture_output=True,
        encoding="utf-8",
        timeout=90,
        check=False,
    )
    idx_path = index_dir / f"{root.name}.json"
    index = json.loads(idx_path.read_text()) if idx_path.exists() else None
    return result, index


def _build_fixture_project(root: Path) -> None:
    """Build the shared feature-rich parity fixture.

    This duplicates the fixture from ``test_extraction_parity_scanner_discovery.py`` as explained in the module
    docstring.
    """
    pkg = root / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "leaf.py").write_text("def leaf_fn(x):\n    return x + 1\n")
    (pkg / "leaf_runner.py").write_text(
        'from pkg.leaf import leaf_fn\n\nif __name__ == "__main__":\n    print(leaf_fn(1))\n'
    )
    (pkg / "consumer.py").write_text(
        "import pkg.leaf\n\n\nclass Widget:\n    def run(self, x):\n        return pkg.leaf.leaf_fn(x)\n"
    )
    (pkg / "dyn.py").write_text(
        "import importlib\n"
        "import subprocess\n"
        "from pathlib import Path\n\n\n"
        'def load():\n    return importlib.import_module("pkg.leaf")\n\n\n'
        'def run_script():\n    return subprocess.run(["python3", str(Path(__file__).parent / "leaf_runner.py")])\n'
    )
    (pkg / "broken.py").write_text("def oops(:\n    return\n")
    # conftest.py / test_*.py live under tests/, not project root — scanner.py's
    # _TEST_PATH_RE requires a "/" before "conftest.py"/"test_*.py" (or a "tests?/"
    # path segment), so a bare root-level conftest.py/test_pkg.py never matches and
    # is_test stays False, silently skipping fixture-graph and mock-patch extraction.
    tests_dir = root / "tests"
    tests_dir.mkdir()
    (tests_dir / "conftest.py").write_text(
        "import pytest\n\n\n@pytest.fixture\ndef widget():\n    from pkg.consumer import Widget\n\n    return Widget()\n"
    )
    (tests_dir / "test_pkg.py").write_text(
        "from unittest.mock import patch\n\n\n"
        '@patch("pkg.leaf.leaf_fn")\n'
        "def test_patched(mock_fn):\n"
        "    mock_fn.return_value = 99\n\n\n"
        "def test_widget(widget):\n"
        "    assert widget.run(1) == 2\n"
    )
    docs = root / "docs"
    docs.mkdir()
    (docs / "api.rst").write_text("API\n===\n\nSee :func:`pkg.leaf.leaf_fn` for details.\n")
    vendored = root / "vendored-lib"
    vendored.mkdir()
    (vendored / "vendored.py").write_text("def vendored_fn():\n    return 0\n")
    (root / ".codemapignore").write_text("# vendored third-party copy\nvendored-lib\n")


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


# --- graph-layer aggregates: full scan across both path classes ------------


@pytest.mark.parametrize("dirname", _PATH_CLASSES)
def test_graph_aggregates_byte_identical_old_vs_new(tmp_path: Path, old_scan_index: Path, dirname: str) -> None:
    """Full scan of the feature-rich fixture agrees on every graph-layer aggregate field.

    Asserts the whole-index parity (as in ``test_extraction_parity_scanner_discovery.py``) *and* each graph-owned
    aggregate individually, so a regression isolated to graph construction cannot hide behind an incidental full-JSON
    match.
    """
    root = tmp_path / dirname
    root.mkdir()
    _build_fixture_project(root)
    index_dir = tmp_path / "idx"

    old_result, old_index = _run_scan(old_scan_index, root, index_dir)
    new_result, new_index = _run_scan(_NEW_SCAN_INDEX, root, index_dir)

    assert old_result.returncode == new_result.returncode == 0
    assert old_result.stderr == new_result.stderr
    assert old_index is not None and new_index is not None

    for field in (
        "fixture_rdep_count",
        "subprocess_rdep_count",
        "sphinx_xref_count",
        "doc_xrefs",
        "module_aliases",
        "collisions",
        "excluded_roots",
        "src_layout",
        "src_roots",
    ):
        assert old_index[field] == new_index[field], f"graph aggregate {field!r} diverged"

    _assert_v13_index_delta(old_index, new_index)


# --- dedup / collision resolution across a colliding module tree -----------


def test_dedup_collision_resolution_identical(tmp_path: Path, old_scan_index: Path) -> None:
    """Two colliding top-level ``pkg`` trees dedup to the same winner old-vs-new."""
    root = tmp_path / "proj"
    root.mkdir()
    _build_colliding_fixture(root)
    index_dir = tmp_path / "idx"

    old_result, old_index = _run_scan(old_scan_index, root, index_dir)
    new_result, new_index = _run_scan(_NEW_SCAN_INDEX, root, index_dir)

    assert old_result.returncode == new_result.returncode == 0
    assert old_index is not None and new_index is not None
    assert old_index["collisions"] == new_index["collisions"]
    assert len(old_index["collisions"]) > 0, "fixture must actually force a collision"
    old_names = sorted(m["name"] for m in old_index["modules"])
    new_names = sorted(m["name"] for m in new_index["modules"])
    assert old_names == new_names
    _assert_v13_index_delta(old_index, new_index)


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
    _build_fixture_project(root)
    cov_path = tmp_path / ".coverage"
    _record_coverage(root / "pkg" / "leaf.py", cov_path)
    index_dir = tmp_path / "idx"

    old_result, old_index = _run_scan(old_scan_index, root, index_dir, "--with-coverage", str(cov_path))
    new_result, new_index = _run_scan(_NEW_SCAN_INDEX, root, index_dir, "--with-coverage", str(cov_path))

    assert old_result.returncode == new_result.returncode == 0
    assert old_index is not None and new_index is not None
    old_leaf = next(m for m in old_index["modules"] if m["name"] == "pkg.leaf")
    new_leaf = next(m for m in new_index["modules"] if m["name"] == "pkg.leaf")
    assert old_leaf.get("symbols") == new_leaf.get("symbols")
    assert any("coverage_pct" in s for s in old_leaf.get("symbols", [])), (
        "fixture must actually exercise coverage annotation"
    )
    _assert_v13_index_delta(old_index, new_index)


# --- test-impact's underlying graph data (fixture graph + mock patches) ----


def test_fixture_graph_and_mock_patch_data_identical(tmp_path: Path, old_scan_index: Path) -> None:
    """The pytest-fixture graph and per-module mock_patches feeding scan-query test-impact agree.

    See module SCOPE NOTE: the ``test-impact`` command itself is query.py's
    (out of ownership); this pins the graph-layer inputs it reads.
    """
    root = tmp_path / "proj"
    root.mkdir()
    _build_fixture_project(root)
    index_dir = tmp_path / "idx"

    old_result, old_index = _run_scan(old_scan_index, root, index_dir)
    new_result, new_index = _run_scan(_NEW_SCAN_INDEX, root, index_dir)

    assert old_result.returncode == new_result.returncode == 0
    assert old_index is not None and new_index is not None
    assert old_index["fixture_rdep_count"] == new_index["fixture_rdep_count"]

    def _mock_patches_by_module(index: dict) -> dict:
        """Map indexed module names to their recorded mock-patch metadata."""
        return {m["name"]: m.get("mock_patches") for m in index["modules"]}

    old_mocks = _mock_patches_by_module(old_index)
    new_mocks = _mock_patches_by_module(new_index)
    assert old_mocks == new_mocks
    assert old_mocks.get("tests.test_pkg"), "fixture must actually exercise mock-patch extraction"

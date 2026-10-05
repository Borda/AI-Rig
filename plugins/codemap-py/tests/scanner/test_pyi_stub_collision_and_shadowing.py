"""Test type-stub discovery, shadowing, and stub-only indexing.

Drives ``codemap_py.graph.scan`` over the committed ``tests/corpus_pyi/proj`` fixture (scenario map in that dir's
``README.md``) and asserts the module-collision matrix: a sibling ``.py`` is authoritative and its ``.pyi`` becomes a
``shadowed_stub``; a lone ``.pyi`` is indexed once as ``stub_only`` with declarations/imports but no call edges;
``__init__.py`` shadows ``__init__.pyi``; and case-fold collisions fail closed.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from codemap_py.graph import _resolve_stub_shadowing, scan
from codemap_py.scanner import _load_exclusions, get_file_hashes

# The five authoritative modules derived from .py files (stub-free module set).
_PY_MODULES = {"nested", "nested.core", "pkg", "pkg.shadowed", "typed"}
# The five stub-only modules derived from lone .pyi files.
_STUB_ONLY_MODULES = {"nested.proto", "pkg.stub_only", "stubpkg", "stubpkg.leaf", "typed.interfaces"}
# The three .pyi shadowed by an authoritative .py sibling.
_SHADOWED = ["nested/core.pyi", "pkg/__init__.pyi", "pkg/shadowed.pyi"]


@pytest.fixture(name="index", scope="module")
def _index(corpus_pyi_dir: Path) -> dict:
    """Full scan of the fixture project (``scan`` is pure — it writes no index file)."""
    return scan(corpus_pyi_dir)


def _module(index: dict, name: str) -> dict | None:
    """Find the indexed module with the requested dotted name.

    >>> _module({"modules": [{"name": "pkg.mod"}]}, "pkg.mod")
    {'name': 'pkg.mod'}
    """
    return next((m for m in index["modules"] if m.get("name") == name), None)


def _by_path(index: dict, path: str) -> dict | None:
    """Find the indexed module with the requested source path."""
    return next((m for m in index["modules"] if m.get("path") == path), None)


def _symbol_names(module: dict) -> set[str]:
    """Return declared symbol names from one indexed module.

    >>> _symbol_names({"symbols": [{"name": "one"}]})
    {'one'}
    """
    return {s.get("name") for s in module.get("symbols", [])}


# --- shadowing: sibling .py wins ------------------------------------------------


def test_shadowed_module_not_indexed_as_second_module(index: dict) -> None:
    """Prefer an implementation module over its shadowed type stub."""
    assert _by_path(index, "pkg/shadowed.pyi") is None
    assert _module(index, "pkg.shadowed")["path"] == "pkg/shadowed.py"


def test_shadowed_stubs_inventory_lists_every_shadowed_pyi(index: dict) -> None:
    """All three shadowed stubs are reported (never silently dropped), sorted."""
    assert index.get("shadowed_stubs") == _SHADOWED


def test_authoritative_modules_flagged_has_stub(index: dict) -> None:
    """Each ``.py`` that shadows a ``.pyi`` carries ``has_stub=True``."""
    assert _module(index, "pkg.shadowed").get("has_stub") is True
    assert _module(index, "nested.core").get("has_stub") is True
    assert _module(index, "pkg").get("has_stub") is True


def test_init_py_precedence_over_init_pyi(index: dict) -> None:
    """Prefer a package implementation initializer over its type stub."""
    assert _module(index, "pkg")["path"] == "pkg/__init__.py"
    assert "pkg/__init__.pyi" in index.get("shadowed_stubs", [])


def test_authoritative_private_symbol_survives_shadowing(index: dict) -> None:
    """A private symbol present only in the ``.py`` (not the stub) stays indexed."""
    assert "_double" in _symbol_names(_module(index, "pkg.shadowed"))


# --- stub-only: lone .pyi -------------------------------------------------------


def test_stub_only_modules_indexed_once_each(index: dict) -> None:
    """Every lone ``.pyi`` is indexed exactly once with ``stub_only=True``."""
    stub_only = {m["name"] for m in index["modules"] if m.get("stub_only") is True}
    assert stub_only == _STUB_ONLY_MODULES
    names = [m["name"] for m in index["modules"]]
    assert len(names) == len(set(names)), "no module name indexed twice"


def test_stub_only_modules_have_no_call_edges(index: dict) -> None:
    """Stub-only modules contribute no outgoing call edges (bodies are ``...``)."""
    for m in index["modules"]:
        if m.get("stub_only"):
            assert all(not s.get("calls") for s in m.get("symbols", [])), m["name"]


def test_stub_only_contributes_declarations_and_imports(index: dict) -> None:
    """A declarations-only stub records its symbols and its import edges."""
    stub = _module(index, "pkg.stub_only")
    assert "typing" in stub["direct_imports"]
    assert {"Marker", "describe"} <= _symbol_names(stub)
    interfaces = _module(index, "typed.interfaces")
    assert {"os", "collections.abc", "typing"} <= set(interfaces["direct_imports"])


def test_stub_only_package_init_indexed_once(index: dict) -> None:
    """A package whose only init is ``__init__.pyi`` is indexed once as stub-only."""
    pkg = _module(index, "stubpkg")
    assert pkg is not None and pkg.get("stub_only") is True
    assert pkg["path"] == "stubpkg/__init__.pyi"


def test_stub_only_module_inside_stub_package_resolves(index: dict) -> None:
    """A stub module inside a stub-only package resolves to a dotted name."""
    leaf = _module(index, "stubpkg.leaf")
    assert leaf is not None and leaf.get("stub_only") is True


# --- nested mix / invariants ----------------------------------------------------


def test_nested_mix_authoritative_edges_preserved(index: dict) -> None:
    """In a nested package: authoritative ``.py`` keeps its call edge, stub shadowed."""
    core = _module(index, "nested.core")
    calls = sum(len(s.get("calls", [])) for s in core.get("symbols", []))
    assert calls >= 1, "cross-package run()->impl edge must survive"
    assert "nested/core.pyi" in index.get("shadowed_stubs", [])
    assert _module(index, "nested.proto").get("stub_only") is True


def test_module_count_invariant_and_membership(index: dict) -> None:
    """Authoritative-module set is exactly the five .py modules; total = 5 + 5 stubs."""
    non_stub = {m["name"] for m in index["modules"] if not m.get("stub_only")}
    assert non_stub == _PY_MODULES
    assert len(index["modules"]) == len(_PY_MODULES) + len(_STUB_ONLY_MODULES)


def test_pyi_joins_the_freshness_hash_set(tmp_path: Path, corpus_pyi_dir: Path) -> None:
    """A ``.pyi`` edit must invalidate the index: stubs are in ``file_shas`` (MD5 path)."""
    dest = tmp_path / "proj"
    shutil.copytree(corpus_pyi_dir, dest)
    shas = get_file_hashes(dest, _load_exclusions(dest))
    assert any(k.endswith(".pyi") for k in shas), "stubs must be hashed for freshness"
    assert "pkg/stub_only.pyi" in shas


# --- case-fold collisions (fail closed, OS-independent) -------------------------


def test_case_fold_collision_drops_both_deterministically() -> None:
    """Two paths equal under casefold are dropped and reported, order-independent."""
    a = {"name": "pkg.Mod", "path": "pkg/Mod.py"}
    b = {"name": "pkg.mod", "path": "pkg/mod.py"}
    kept_ab, shadow_ab, cf_ab = _resolve_stub_shadowing([a, b])
    kept_ba, shadow_ba, cf_ba = _resolve_stub_shadowing([b, a])
    assert kept_ab == kept_ba == []
    assert shadow_ab == shadow_ba == []
    assert cf_ab == cf_ba == [{"paths": ["pkg/Mod.py", "pkg/mod.py"], "reason": "case_fold_collision"}]


def test_py_pyi_sibling_is_not_a_casefold_collision() -> None:
    """A real shadow pair (differing suffix) is resolved, never flagged case-fold."""
    py = {"name": "pkg.mod", "path": "pkg/mod.py"}
    pyi = {"name": "pkg.mod", "path": "pkg/mod.pyi"}
    kept, shadowed, casefold = _resolve_stub_shadowing([py, pyi])
    assert casefold == []
    assert shadowed == ["pkg/mod.pyi"]
    assert [m["path"] for m in kept] == ["pkg/mod.py"]
    assert kept[0].get("has_stub") is True


def test_malformed_stub_degrades_without_failing_scan(tmp_path: Path) -> None:
    """A syntactically broken lone ``.pyi`` degrades; the scan still completes."""
    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "good.py").write_text("def f() -> int:\n    return 1\n")
    (root / "pkg" / "broken.pyi").write_text("def f( ->:\n")  # invalid syntax
    result = scan(root)
    broken = _by_path(result, "pkg/broken.pyi")
    assert broken is not None and broken["status"] == "degraded"
    assert _module(result, "pkg.good")["status"] == "ok"

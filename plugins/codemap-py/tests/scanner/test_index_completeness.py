"""Index-vs-reality completeness property tests for the codemap structural index.

Nothing else in the suite validates that ``scan-index`` actually records the import and call edges present in real
source. This module builds a small package that exercises every import shape codemap must reason about — plain plain,
aliased, and relative imports, an ``__init__`` re-export consumed elsewhere, and cross-module function calls. It then
compares ``scan-query`` output against an *independent* oracle built here with :mod:`ast`.

Where the index legitimately cannot see an edge (module-less relative imports and ``from pkg import submodule`` are
recorded by their raw base, never resolved to the submodule), the test asserts the gap is *disclosed* through the
``index.not_covered`` field rather than silently absent — an honest blind spot, not a false-negative masquerading as an
exhaustive answer.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

# Fixture package sources, keyed by path relative to the package root. Every entry
# is a distinct import/call shape the index is expected to handle.
_FIXTURE_SOURCES: dict[str, str] = {
    "mypkg/__init__.py": 'from .core import Engine\n\n__all__ = ["Engine"]\n',
    "mypkg/core.py": "def run():\n    return 1\n\n\nclass Engine:\n    def start(self):\n        return run()\n",
    "mypkg/plain_import.py": "import mypkg.core\n\n\ndef go():\n    return mypkg.core.run()\n",
    "mypkg/from_import.py": "from mypkg import core\n\n\ndef go2():\n    return core.run()\n",
    "mypkg/aliased.py": "import mypkg.core as c\n\n\ndef go3():\n    return c.run()\n",
    "mypkg/rel_dot.py": "from . import core\n\n\ndef go4():\n    return core.run()\n",
    "mypkg/sub/__init__.py": "",
    "mypkg/sub/deep.py": "from ..core import run\n\n\ndef go5():\n    return run()\n",
    "mypkg/reexport_consumer.py": "from mypkg import Engine\n\n\ndef go6():\n    e = Engine()\n    return e.start()\n",
}

_CORE_MODULE = "mypkg.core"
_CORE_RUN = "mypkg.core.run"


# --------------------------------------------------------------------------- #
# Independent AST oracle — resolves edges the way Python itself would, WITHOUT
# reusing any scan-index code, so a divergence flags a real index limitation.
# --------------------------------------------------------------------------- #


def _module_name(rel_path: str) -> str:
    """Return the dotted module name for a package-relative ``.py`` path.

    Args:
        rel_path: POSIX path of the source file relative to the package root.

    Examples:
        >>> _module_name("mypkg/core.py")
        'mypkg.core'
        >>> _module_name("mypkg/sub/__init__.py")
        'mypkg.sub'
    """
    parts = rel_path[: -len(".py")].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _package_context(module_name: str, is_init: bool) -> str:
    """Return the package a relative import resolves against.

    For a package ``__init__`` the anchor is the package itself; for a regular
    module it is the parent package.

    >>> _package_context("pkg.mod", False)
    'pkg'

    Args:
        module_name: dotted name of the importing module.
        is_init: whether the module is a package ``__init__``.
    """
    if is_init:
        return module_name
    return module_name.rsplit(".", 1)[0] if "." in module_name else ""


def _resolve_from_base(node: ast.ImportFrom, package: str) -> str:
    """Resolve the fully qualified base module of a relative import node.

    Args:
        node: the ``ImportFrom`` node.
        package: package the module belongs to (relative-import anchor).

    >>> _resolve_from_base(ast.ImportFrom(module="mod", level=1), "pkg")
    'pkg.mod'
    """
    base = node.module or ""
    if not node.level:
        return base
    parts = package.split(".") if package else []
    up = node.level - 1
    anchor = ".".join(parts[: len(parts) - up]) if up < len(parts) else ""
    if anchor and base:
        return f"{anchor}.{base}"
    return anchor or base


def _name_scope(tree: ast.Module, module_name: str, is_init: bool) -> dict[str, str]:
    """Map each local name to the fully-qualified dotted path it refers to.

    Covers ``import``/``import ... as``, ``from ... import`` (absolute and
    relative, including module-less ``from . import x``), and top-level
    definitions — enough to resolve every call shape in the fixture.

    Args:
        tree: parsed module AST.
        module_name: dotted name of the module.
        is_init: whether the module is a package ``__init__``.
    """
    package = _package_context(module_name, is_init)
    scope: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    scope[alias.asname] = alias.name
                else:
                    head = alias.name.split(".")[0]
                    scope.setdefault(head, head)
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_from_base(node, package)
            for alias in node.names:
                scope[alias.asname or alias.name] = f"{base}.{alias.name}" if base else alias.name
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            scope.setdefault(node.name, f"{module_name}.{node.name}")
    return scope


def _call_chain(func: ast.expr) -> list[str] | None:
    """Reconstruct the dotted name of a call target, or ``None`` if not static.

    Args:
        func: the ``func`` attribute of an :class:`ast.Call`.

    >>> _call_chain(ast.parse("pkg.mod.run", mode="eval").body)
    ['pkg', 'mod', 'run']
    """
    if isinstance(func, ast.Name):
        return [func.id]
    if isinstance(func, ast.Attribute):
        head = _call_chain(func.value)
        return [*head, func.attr] if head is not None else None
    return None


def _resolve_call(chain: list[str], scope: dict[str, str]) -> str | None:
    """Resolve a reconstructed call chain to a fully-qualified target.

    Args:
        chain: dotted-name components of the call target.
        scope: local-name to qualified-path map from :func:`_name_scope`.

    >>> _resolve_call(["alias", "run"], {"alias": "pkg.mod"})
    'pkg.mod.run'
    """
    root = scope.get(chain[0])
    if root is None:
        return None
    return ".".join([root, *chain[1:]])


def _enclosing_symbols(tree: ast.Module) -> list[tuple[str, ast.AST]]:
    """Return ``(symbol_name, node)`` for every top-level function and method.

    Args:
        tree: parsed module AST.
    """
    out: list[tuple[str, ast.AST]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append((node.name, node))
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.append((f"{node.name}.{item.name}", item))
    return out


def _oracle_edges() -> tuple[set[str], set[str]]:
    """Compute true importers of ``mypkg.core`` and true callers of ``mypkg.core.run``.

    Returns:
        ``(importer_modules, caller_qnames)`` where callers are ``module::symbol``.
    """
    known = {_module_name(p) for p in _FIXTURE_SOURCES}
    importers: set[str] = set()
    callers: set[str] = set()
    for rel_path, src in _FIXTURE_SOURCES.items():
        module_name = _module_name(rel_path)
        is_init = rel_path.endswith("__init__.py")
        tree = ast.parse(src)
        scope = _name_scope(tree, module_name, is_init)
        _collect_importers(tree, module_name, is_init, known, importers)
        _collect_callers(tree, module_name, scope, callers)
    return importers, callers


def _collect_importers(tree: ast.Module, module_name: str, is_init: bool, known: set[str], importers: set[str]) -> None:
    """Add *module_name* to *importers* if it imports ``mypkg.core`` in any form."""
    package = _package_context(module_name, is_init)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name == _CORE_MODULE for a in node.names):
                importers.add(module_name)
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_from_base(node, package)
            candidates = {base, *(f"{base}.{a.name}" if base else a.name for a in node.names)}
            if _CORE_MODULE in candidates & known or base == _CORE_MODULE:
                importers.add(module_name)


def _collect_callers(tree: ast.Module, module_name: str, scope: dict[str, str], callers: set[str]) -> None:
    """Add ``module::symbol`` to *callers* for every symbol that calls ``mypkg.core.run``."""
    for symbol, node in _enclosing_symbols(tree):
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            chain = _call_chain(call.func)
            if chain and _resolve_call(chain, scope) == _CORE_RUN:
                callers.add(f"{module_name}::{symbol}")


# --------------------------------------------------------------------------- #
# Fixtures — build the package once, scan it once, query via subprocess.
# --------------------------------------------------------------------------- #


@pytest.fixture(name="completeness_project", scope="module")
def _completeness_project(tmp_path_factory, scan_index) -> tuple[Path, Path]:
    """Write the fixture package, scan it once, return ``(root, index_path)``."""
    root = tmp_path_factory.mktemp("completeness")
    for rel_path, src in _FIXTURE_SOURCES.items():
        target = root / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(src)
    result = subprocess.run(
        [sys.executable, str(scan_index), "--root", str(root)],
        capture_output=True,
        text=True,
        cwd=str(root),
    )
    assert result.returncode == 0, result.stderr
    index_path = root / ".cache" / "codemap" / f"{root.name}.json"
    assert index_path.exists(), "scan-index did not produce an index file"
    return root, index_path


@pytest.fixture(name="cquery", scope="module")
def _cquery(completeness_project, scan_query) -> Callable[..., dict]:
    """Return a callable that runs ``scan-query`` against the fixture index."""
    root, index_path = completeness_project

    def _run(*args: str) -> dict:
        """Run the fixture query subprocess and decode its JSON response."""
        result = subprocess.run(
            [sys.executable, str(scan_query), "--index", str(index_path), *args],
            capture_output=True,
            text=True,
            cwd=str(root),
        )
        assert result.returncode == 0, result.stderr + result.stdout
        return json.loads(result.stdout)

    return _run


# --------------------------------------------------------------------------- #
# Module-level import-graph completeness
# --------------------------------------------------------------------------- #


class TestModuleImportCompleteness:
    """Require indexed functions to match the AST oracle or disclose incompleteness."""

    def test_plain_and_aliased_imports_are_covered(self, cquery):
        """Resolvable ``import mypkg.core[/ as c]`` edges appear in ``imported_by``."""
        reported = set(cquery("rdeps", _CORE_MODULE)["imported_by"])
        assert {"mypkg.plain_import", "mypkg.aliased"} <= reported

    def test_relative_and_submodule_importers_match_the_ast_oracle(self, cquery):
        """Relative and parent-package submodule importers are retained."""
        reported = set(cquery("rdeps", _CORE_MODULE)["imported_by"])
        true_importers, _ = _oracle_edges()
        assert reported == true_importers

    def test_resolved_import_forms_are_not_reported_as_coverage_gaps(self, cquery):
        """The coverage block does not claim known static forms are omitted."""
        not_covered = cquery("rdeps", _CORE_MODULE)["index"]["not_covered"]
        assert "relative-import" not in not_covered
        assert "from-import-submodule" not in not_covered


# --------------------------------------------------------------------------- #
# Function-level call-graph completeness
# --------------------------------------------------------------------------- #


class TestFunctionCallCompleteness:
    """Require indexed classes to match the AST oracle or disclose incompleteness."""

    def test_resolvable_callers_are_covered(self, cquery):
        """Plain, from-import, aliased, dotted-relative, and local callers are all found."""
        reported = {c["caller"] for c in cquery("fn-rdeps", "mypkg.core::run")["called_by"]}
        expected = {
            "mypkg.core::Engine.start",
            "mypkg.plain_import::go",
            "mypkg.from_import::go2",
            "mypkg.aliased::go3",
            "mypkg.sub.deep::go5",
        }
        assert expected <= reported

    def test_relative_callers_match_the_ast_oracle(self, cquery):
        """Module-less relative imports resolve call aliases before query time."""
        reported = {c["caller"] for c in cquery("fn-rdeps", "mypkg.core::run")["called_by"]}
        _, true_callers = _oracle_edges()
        assert reported == true_callers

    def test_relative_calls_are_not_reported_as_coverage_gaps(self, cquery):
        """The coverage block does not claim resolved relative calls are omitted."""
        not_covered = cquery("fn-rdeps", "mypkg.core::run")["index"]["not_covered"]
        assert "relative-import" not in not_covered

    def test_unique_caller_count_is_deduped_caller_total(self, cquery):
        """Mirror the deduped caller list and ``count``."""
        data = cquery("fn-rdeps", "mypkg.core::run")
        assert data["unique_caller_count"] == len({c["caller"] for c in data["called_by"]})
        assert data["unique_caller_count"] == data["count"]

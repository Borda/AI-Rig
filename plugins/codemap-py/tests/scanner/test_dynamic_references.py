"""The scanner resolves literal dynamic references and counts the dynamic sites it cannot resolve.

Three forms name a module or attribute through a value the scanner can still read without running code: a ``getattr`` on
an imported module with a literal attribute name, an ``import_module`` call whose argument is a module-level string
constant, and a PEP 562 lazy-import map read by a module-level ``__getattr__``. Each form has a positive fixture and
false-positive fixtures (non-literal argument, shadowed name, a base that is not a module, an unknown module). Every
call of these forms whose argument stays unknown is recorded as a dynamic site, never dropped.
"""

from __future__ import annotations

import ast
import textwrap

import pytest
from codemap_py.graph import _stamp_dynamic_rdeps, _stamp_dynamic_refs
from codemap_py.scanner.imports import _extract_imports_and_scope, extract_dynamic_references


def _refs(source: str, module: str = "pkg.loader", *, is_package: bool = False) -> tuple[list[dict], list[dict]]:
    """Run the dynamic-reference pass over *source* with the import map the scanner builds for it."""
    tree = ast.parse(textwrap.dedent(source))
    _imports, _submodules, name_map, _module_map, _stars = _extract_imports_and_scope(tree, module, is_package)
    return extract_dynamic_references(tree, module, is_package, name_map)


class TestGetattrOnImportedModule:
    """``getattr(<imported module>, "name")`` is recorded with its base module and attribute name."""

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            pytest.param(
                'import pkg.mod as m\ngetattr(m, "run")\n',
                {"kind": "getattr", "module": "pkg.mod", "line": 2, "name": "run"},
                id="import-as",
            ),
            pytest.param(
                'from pkg import mod\ndef f():\n    return getattr(mod, "run", None)\n',
                {"kind": "getattr", "module": "pkg.mod", "line": 3, "name": "run"},
                id="from-import-with-default",
            ),
            pytest.param(
                'from pkg import mod\nNAME = "run"\ngetattr(mod, NAME)\n',
                {"kind": "getattr", "module": "pkg.mod", "line": 3, "name": "run"},
                id="constant-attribute-name",
            ),
            pytest.param(
                'import pkg.mod\ngetattr(pkg.mod, "run")\n',
                {"kind": "getattr", "module": "pkg.mod", "line": 2, "name": "run"},
                id="dotted-base",
            ),
        ],
    )
    def test_literal_attribute_is_recorded(self, source, expected):
        """A literal or constant attribute name on an imported module yields one named ``getattr`` record.

        The base is resolved through the module's import statements, so an alias, a from-import and a dotted path all
        name the same module.
        """
        assert _refs(source) == ([], [expected])

    def test_non_literal_attribute_is_a_dynamic_site(self):
        """A ``getattr`` on an imported module with a computed name is kept as an unnamed record.

        The record carries no ``name``; the graph phase counts it as a dynamic site once the base is an indexed module.
        """
        assert _refs("from pkg import mod\ndef f(attr):\n    return getattr(mod, attr)\n") == (
            [],
            [{"kind": "getattr", "module": "pkg.mod", "line": 3}],
        )

    @pytest.mark.parametrize(
        "source",
        [
            pytest.param('from pkg import mod\ndef f(mod):\n    return getattr(mod, "run")\n', id="parameter-shadows"),
            pytest.param(
                'from pkg import mod\ndef f():\n    mod = make()\n    return getattr(mod, "run")\n', id="local-shadows"
            ),
            pytest.param('from pkg import mod\n[getattr(mod, "run") for mod in items]\n', id="comprehension-shadows"),
            pytest.param('getattr(obj, "run")\n', id="base-not-imported"),
            pytest.param('import logging\ngetattr(logging, "INFO")\ngetattr(logging, level)\n', id="stdlib-base"),
            pytest.param(
                'from helpers import getattr\nfrom pkg import mod\ngetattr(mod, "run")\n', id="getattr-rebound"
            ),
        ],
    )
    def test_false_positive_shapes_are_not_recorded(self, source):
        """A name that is not the imported module at the call, or a standard-library base, records nothing.

        Shadowing by a parameter, a local assignment or a comprehension target hides the module binding; an object that
        was never imported and a rebound ``getattr`` are not module references at all.
        """
        assert _refs(source) == ([], [])


class TestImportModuleConstant:
    """``import_module(CONST)`` resolves a module-level string constant into a ``dynamic_imports`` entry."""

    @pytest.mark.parametrize(
        ("source", "module", "is_package", "expected"),
        [
            pytest.param(
                'import importlib\nTARGET = "pkg.plugins"\nimportlib.import_module(TARGET)\n',
                "pkg.loader",
                False,
                "pkg.plugins",
                id="attribute-call",
            ),
            pytest.param(
                'from importlib import import_module\nTARGET: str = "pkg.plugins"\ndef load():\n'
                "    return import_module(TARGET)\n",
                "pkg.loader",
                False,
                "pkg.plugins",
                id="bare-import-module-annotated",
            ),
            pytest.param(
                'import importlib\nSUB = ".plugins"\nimportlib.import_module(SUB, __name__)\n',
                "pkg",
                True,
                "pkg.plugins",
                id="relative-to-name-in-package",
            ),
            pytest.param(
                'import importlib\nSUB = ".plugins"\nimportlib.import_module(SUB, package=__package__)\n',
                "pkg.loader",
                False,
                "pkg.plugins",
                id="relative-to-package-keyword",
            ),
        ],
    )
    def test_constant_argument_resolves(self, source, module, is_package, expected):
        """The constant's value becomes a ``via: "constant"`` entry pointing at the constant's own line.

        The line is where the module path text lives, which is what a rename has to edit.
        """
        imports, refs = _refs(source, module, is_package=is_package)

        assert imports == [{"literal": expected, "line": 2, "via": "constant"}]
        assert refs == []

    def test_bare_import_module_literal_is_recorded_once(self):
        """``from importlib import import_module; import_module("x")`` is a literal entry without ``via``.

        The attribute spelling with a literal is already recorded by ``extract_dynamic_imports``, so it yields nothing
        here and is never duplicated.
        """
        bare = _refs('from importlib import import_module\nimport_module("pkg.plugins")\n')
        attribute = _refs('import importlib\nimportlib.import_module("pkg.plugins")\n')

        assert bare == ([{"literal": "pkg.plugins", "line": 2}], [])
        assert attribute == ([], [])

    @pytest.mark.parametrize(
        "source",
        [
            pytest.param("import importlib\nimportlib.import_module(name)\n", id="non-literal"),
            pytest.param('import importlib\nimportlib.import_module(f"pkg.{name}")\n', id="f-string"),
            pytest.param('import importlib\nT = "pkg.a"\nT = "pkg.b"\nimportlib.import_module(T)\n', id="rebound"),
            pytest.param(
                'import importlib\nif flag:\n    T = "pkg.a"\nimportlib.import_module(T)\n', id="conditional-constant"
            ),
            pytest.param(
                'import importlib\nT = "pkg.a"\ndef reset():\n    global T\n    T = "pkg.b"\nimportlib.import_module(T)\n',
                id="global-rebinding",
            ),
            pytest.param(
                'import importlib\nT = "pkg.a"\ndef load(T):\n    return importlib.import_module(T)\n', id="shadowed"
            ),
            pytest.param('import importlib\nT = "not a module"\nimportlib.import_module(T)\n', id="not-a-path"),
            pytest.param('import importlib\nT = ".plugins"\nimportlib.import_module(T)\n', id="relative-no-anchor"),
            pytest.param("__import__(name)\n", id="dunder-import"),
        ],
    )
    def test_unresolved_argument_is_a_dynamic_site(self, source):
        """An import call whose argument is not a visible, single, unconditional module-path constant is a site.

        It resolves to no entry and is recorded as an ``import`` site so the per-query count includes it.
        """
        imports, refs = _refs(source)

        assert imports == []
        assert [record["kind"] for record in refs] == ["import"]

    def test_unrelated_import_module_name_is_ignored(self):
        """A callable named ``import_module`` that does not come from importlib is not an import call.

        A project helper that happens to share the name must neither add an import entry nor inflate the site count.
        """
        assert _refs("from mylib import import_module\nimport_module(name)\n") == ([], [])


_LAZY_PACKAGE = """\
import importlib

_LAZY = {"Client": ".client", "Server": ("pkg.server", "Server")}


def __getattr__(name):
    if name in _LAZY:
        target = _LAZY[name]
        path = target if isinstance(target, str) else target[0]
        module = importlib.import_module(path, __name__)
        return getattr(module, name)
    raise AttributeError(name)
"""


class TestLazyGetattrMap:
    """A PEP 562 lazy-import map yields one ``via: "lazy_getattr"`` entry per value."""

    def test_map_values_resolve(self):
        """String and ``(module, attr)`` values resolve, relative ones against the package.

        The map's own import call inside ``__getattr__`` is resolved by the map, so it is not a dynamic site.
        """
        assert _refs(_LAZY_PACKAGE, "pkg", is_package=True) == (
            [
                {"literal": "pkg.client", "line": 3, "via": "lazy_getattr"},
                {"literal": "pkg.server", "line": 3, "via": "lazy_getattr"},
            ],
            [],
        )

    @pytest.mark.parametrize(
        "source",
        [
            pytest.param(
                'import warnings\n_OLD = {"old": "pkg.new"}\ndef __getattr__(name):\n'
                '    warnings.warn(f"{name} moved to {_OLD[name]}")\n    return globals()["_" + name]\n',
                id="deprecation-map-without-import",
            ),
            pytest.param(
                'import importlib\n_OLD = {"old": "pkg.new is deprecated"}\nimportlib.import_module("pkg.x")\n'
                "def __getattr__(name):\n    importlib.import_module('pkg.x')\n    raise AttributeError(_OLD[name])\n",
                id="value-not-a-module-path",
            ),
            pytest.param(
                'import importlib\n_MAP = {"Client": "pkg.client"}\nimportlib.import_module("pkg.x")\n',
                id="no-getattr-function",
            ),
            pytest.param(
                'import importlib\n_MAP = {"Client": "pkg.client"}\ndef __getattr__(name):\n'
                '    return importlib.import_module("pkg.other")\n',
                id="map-not-read-by-getattr",
            ),
        ],
    )
    def test_false_positive_maps_are_not_recorded(self, source):
        """A map is resolved only when ``__getattr__`` reads it and imports, and only for module-path values.

        A deprecation table, a free-text value, a dict with no ``__getattr__`` and a dict ``__getattr__`` never reads
        all leave ``dynamic_imports`` without a lazy entry.
        """
        imports, _refs_found = _refs(source, "pkg", is_package=True)

        assert [entry for entry in imports if entry.get("via") == "lazy_getattr"] == []

    def test_unresolved_map_leaves_the_import_call_counted(self):
        """A ``__getattr__`` whose map has no module-path value keeps its non-literal import call as a dynamic site.

        The transformers-style ``{"module": ["Name"]}`` structure maps the other way round and is not read.
        """
        source = (
            'import importlib\n_STRUCTURE = {"client": ["Client"]}\ndef __getattr__(name):\n'
            "    return importlib.import_module(_STRUCTURE[name][0], __name__)\n"
        )

        assert _refs(source, "pkg", is_package=True) == ([], [{"kind": "import", "line": 4}])


class TestGraphResolution:
    """The graph phase resolves ``getattr`` records against indexed modules and counts dynamic sites."""

    def _modules(self, refs: list[dict]) -> list[dict]:
        """Return a three-module set whose ``pkg.loader`` carries *refs*."""
        return [
            {"name": "pkg.loader", "status": "ok", "dynamic_refs": refs},
            {"name": "pkg.mod", "status": "ok"},
            {"name": "pkg.broken", "status": "degraded"},
        ]

    def test_indexed_base_resolves_and_others_drop(self):
        """Only a base that is an indexed module yields a reference or a counted site.

        A base that is a class in an indexed module, a degraded module or an unknown module is no module reference; an
        unresolved import call always counts.
        """
        modules = self._modules(
            [
                {"kind": "getattr", "module": "pkg.mod", "name": "run", "line": 3},
                {"kind": "getattr", "module": "pkg.mod", "line": 4},
                {"kind": "getattr", "module": "pkg.mod.Cls", "name": "x", "line": 5},
                {"kind": "getattr", "module": "pkg.mod.Cls", "line": 6},
                {"kind": "getattr", "module": "pkg.broken", "line": 7},
                {"kind": "getattr", "module": "other.lib", "name": "y", "line": 8},
                {"kind": "import", "line": 9},
            ]
        )

        _stamp_dynamic_refs(modules)

        assert modules[0]["getattr_refs"] == [{"target": "pkg.mod::run", "line": 3}]
        assert modules[0]["dynamic_sites"] == 2

    def test_empty_results_leave_no_key_and_clear_stale_ones(self):
        """A module without resolved references or sites carries neither key, even if a previous scan set them.

        Carried-over modules of an incremental scan must not keep a count or a reference that no longer holds.
        """
        modules = self._modules([{"kind": "getattr", "module": "other.lib", "name": "y", "line": 8}])
        modules[0].update({"getattr_refs": [{"target": "pkg.mod::old", "line": 1}], "dynamic_sites": 4})

        _stamp_dynamic_refs(modules)

        assert "getattr_refs" not in modules[0]
        assert "dynamic_sites" not in modules[0]
        assert all("dynamic_sites" not in m and "getattr_refs" not in m for m in modules[1:])

    def test_resolved_import_of_unknown_module_adds_no_importer(self):
        """A constant or lazy-map path that names no indexed module, nor a child of one, yields no importer row.

        Only the indexed target gains ``dynamic_imported_by``; the unknown path is dropped, so it can neither invent a
        module nor attach to an unrelated one.
        """
        modules = [
            {
                "name": "pkg.loader",
                "path": "pkg/loader.py",
                "status": "ok",
                "dynamic_imports": [
                    {"literal": "ghost.plugins", "line": 2, "via": "constant"},
                    {"literal": "pkg.mod", "line": 3, "via": "lazy_getattr"},
                ],
            },
            {"name": "pkg.mod", "path": "pkg/mod.py", "status": "ok"},
        ]

        _stamp_dynamic_rdeps(modules)

        assert "dynamic_imported_by" not in modules[0]
        assert modules[1]["dynamic_imported_by"] == [
            {"importer": "pkg.loader", "path": "pkg/loader.py", "line": 3, "literal": "pkg.mod", "via": "lazy_getattr"}
        ]

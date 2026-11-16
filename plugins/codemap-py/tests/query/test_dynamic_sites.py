"""Resolved dynamic references reach the index and every query reports its scoped ``dynamic_sites`` count.

A small project uses each resolvable form beside two calls the scanner cannot resolve: a ``getattr`` with a literal name
on an imported module, ``import_module`` of a module-level constant, and a PEP 562 lazy map in a package ``__init__``.
Each case scans it with the real ``scan-index`` and queries it with the real ``scan-query``. The resolved forms show up
as references, never as import edges. The unresolved calls are counted in the coverage block of a module-local query
(only the queried module) and of a whole-index query (every module), full and compact.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from _scan_helpers import index_path_for, run_query, write_tree

_LOADER = (
    "import importlib\n"
    "from pkg import client\n"
    'TARGET = "pkg.plugins"\n'
    "\n\ndef load():\n    return importlib.import_module(TARGET)\n"
    '\n\ndef connect():\n    return getattr(client, "connect")()\n'
    "\n\ndef pick(name):\n    return importlib.import_module(name)\n"
    "\n\ndef attribute(name):\n    return getattr(client, name)\n"
)
_INIT = (
    "import importlib\n"
    '_LAZY = {"Client": ".client"}\n'
    "\n\ndef __getattr__(name):\n"
    "    if name in _LAZY:\n        return importlib.import_module(_LAZY[name], __name__)\n"
    "    raise AttributeError(name)\n"
)
_SOURCES = {
    "pkg/__init__.py": _INIT,
    "pkg/client.py": "def connect():\n    return 1\n",
    "pkg/plugins.py": "def run():\n    return 2\n",
    "pkg/loader.py": _LOADER,
    "pkg/quiet.py": "from pkg import client\n\n\ndef nothing():\n    return client.connect()\n",
}
#: The loader with both unresolved calls removed and the constant import replaced by a plain function.
_LOADER_STATIC = 'from pkg import client\n\n\ndef connect():\n    return getattr(client, "connect")()\n'


def _scan(scan_index: Path, root: Path, *extra: str) -> None:
    """Run scan-index over *root* and assert it succeeds."""
    result = subprocess.run(
        [sys.executable, str(scan_index), "--root", str(root), *extra], capture_output=True, text=True, cwd=str(root)
    )
    assert result.returncode == 0, result.stderr


def _modules(root: Path) -> dict[str, dict]:
    """Return the project's index modules keyed by name."""
    index = json.loads(index_path_for(root).read_text(encoding="utf-8"))
    return {module["name"]: module for module in index["modules"]}


@pytest.fixture
def project(tmp_path: Path, scan_index: Path) -> Path:
    """Write and fully scan the fixture project, returning its root."""
    write_tree(tmp_path, _SOURCES)
    _scan(scan_index, tmp_path)
    return tmp_path


class TestIndexFields:
    """A full scan persists resolved references and per-module dynamic-site counts."""

    def test_resolved_forms_are_references_not_import_edges(self, project):
        """Constant and lazy-map imports land in ``dynamic_imported_by``; the literal ``getattr`` in ``getattr_refs``.

        ``pkg.plugins`` is reached only through ``import_module(TARGET)``: it gains a dynamic importer with ``via:
        "constant"`` but no direct importer, so the import graph and its ``rdep_count`` stay unchanged.
        """
        modules = _modules(project)

        assert modules["pkg.plugins"]["dynamic_imported_by"] == [
            {"importer": "pkg.loader", "path": "pkg/loader.py", "line": 3, "literal": "pkg.plugins", "via": "constant"}
        ]
        assert modules["pkg.client"]["dynamic_imported_by"] == [
            {"importer": "pkg", "path": "pkg/__init__.py", "line": 2, "literal": "pkg.client", "via": "lazy_getattr"}
        ]
        assert modules["pkg.loader"]["getattr_refs"] == [{"target": "pkg.client::connect", "line": 11}]
        assert modules["pkg.plugins"]["rdep_count"] == 0
        assert all("pkg.plugins" not in module["direct_imports"] for module in modules.values())

    def test_only_unresolved_calls_are_counted(self, project):
        """The loader's two non-literal calls are its dynamic sites; resolved and quiet modules carry no count.

        The lazy map resolves the import call inside the package ``__getattr__``, so the package has no site either.
        """
        counts = {name: module.get("dynamic_sites") for name, module in _modules(project).items()}

        assert counts == {"pkg": None, "pkg.client": None, "pkg.plugins": None, "pkg.loader": 2, "pkg.quiet": None}


class TestCoverageBlock:
    """``dynamic_sites`` is scoped to the query's search set in the full and the compact coverage block."""

    @pytest.mark.parametrize("mode", [pytest.param((), id="full"), pytest.param(("--compact",), id="compact")])
    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            pytest.param(("deps", "pkg.loader"), {"count": 2, "modules": 1, "scope": "module"}, id="local-target"),
            pytest.param(("rdeps", "pkg.plugins"), {"count": 2, "modules": 1, "scope": "index"}, id="wide"),
            pytest.param(("dead-symbols",), {"count": 2, "modules": 1, "scope": "index"}, id="wide-dead-symbols"),
        ],
    )
    def test_count_is_scoped_to_the_search_set(self, project, scan_query, mode, query, expected):
        """A module-local query counts its own module's sites; a whole-index query counts every module's.

        Both the full block and the compact block carry the record, with the same fixed check text and no name.
        """
        payload = run_query(scan_query, project, index_path_for(project), "--no-heal", *mode, *query)

        record = payload["index"]["dynamic_sites"]
        assert {key: record[key] for key in ("count", "modules", "scope")} == expected
        assert "pkg" not in record["check"]

    @pytest.mark.parametrize("mode", [pytest.param((), id="full"), pytest.param(("--compact",), id="compact")])
    def test_module_without_sites_adds_no_key(self, project, scan_query, mode):
        """A module-local query on a module without dynamic sites keeps the coverage key set it always had.

        Other modules of the index do hold sites, so this also proves the local scope ignores them.
        """
        payload = run_query(scan_query, project, index_path_for(project), "--no-heal", *mode, "deps", "pkg.quiet")

        assert "dynamic_sites" not in payload["index"]

    def test_rdeps_lists_resolved_dynamic_importers(self, project, scan_query):
        """``rdeps`` of a module reached only through a constant import shows that importer with its ``via``.

        Nothing imports ``pkg.plugins`` statically, so before constants were resolved ``rdeps`` showed no importer.
        """
        payload = run_query(scan_query, project, index_path_for(project), "--no-heal", "rdeps", "pkg.plugins")

        assert [(row["importer"], row["via"]) for row in payload["dynamic_imported_by"]] == [("pkg.loader", "constant")]


class TestIncrementalScan:
    """An incremental scan keeps the derived fields equal to a full scan of the same tree."""

    def test_removing_dynamic_calls_clears_carried_fields(self, project, scan_index, scan_query):
        """Editing only the loader drops its sites and the constant import's importer from the carried target.

        ``pkg.plugins`` is unchanged and carried over, so its ``dynamic_imported_by`` must be recomputed, not kept; the
        index-wide count in a query falls to zero and the key disappears.
        """
        (project / "pkg/loader.py").write_text(_LOADER_STATIC)

        _scan(scan_index, project, "--incremental")

        modules = _modules(project)
        payload = run_query(scan_query, project, index_path_for(project), "--no-heal", "rdeps", "pkg.client")
        assert "dynamic_imported_by" not in modules["pkg.plugins"]
        assert "dynamic_sites" not in modules["pkg.loader"]
        assert modules["pkg.loader"]["getattr_refs"] == [{"target": "pkg.client::connect", "line": 5}]
        assert "dynamic_sites" not in payload["index"]

    def test_removing_literal_dynamic_import_clears_carried_importer(self, tmp_path, scan_index):
        """Removing a string-literal ``import_module`` drops the importer row from the unchanged, carried target.

        Only the importer file changes, so an incremental scan carries ``pkg.plugins`` over; its old
        ``dynamic_imported_by`` row must not survive the rescan.
        """
        literal_loader = 'import importlib\n\n\ndef load():\n    return importlib.import_module("pkg.plugins")\n'
        write_tree(tmp_path, {**_SOURCES, "pkg/__init__.py": "", "pkg/loader.py": literal_loader})
        _scan(scan_index, tmp_path)
        (tmp_path / "pkg/loader.py").write_text(_LOADER_STATIC)

        _scan(scan_index, tmp_path, "--incremental")

        assert "dynamic_imported_by" not in _modules(tmp_path)["pkg.plugins"]

    def test_incremental_matches_full_scan(self, tmp_path, scan_index):
        """Adding the dynamic calls through an incremental scan yields the same derived fields as a full scan.

        The project starts with the static loader, is scanned, then gets the dynamic loader and an incremental scan; a
        second copy is scanned fully once. Every derived field must agree.
        """
        incremental, full = tmp_path / "incremental", tmp_path / "full"
        write_tree(incremental, {**_SOURCES, "pkg/loader.py": _LOADER_STATIC})
        write_tree(full, _SOURCES)
        _scan(scan_index, incremental)
        (incremental / "pkg/loader.py").write_text(_LOADER)

        _scan(scan_index, incremental, "--incremental")
        _scan(scan_index, full)

        fields = ("dynamic_imports", "dynamic_refs", "dynamic_imported_by", "getattr_refs", "dynamic_sites")
        derived = [
            {name: {key: module.get(key) for key in fields} for name, module in _modules(root).items()}
            for root in (incremental, full)
        ]
        assert derived[0] == derived[1]

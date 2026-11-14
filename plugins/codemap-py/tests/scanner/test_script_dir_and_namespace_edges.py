"""Regression tests for script-directory import edges and packages nested below a namespace directory.

Two resolver gaps found by scoring edges against a toolchain truth on real repositories:

* A file in a directory without ``__init__.py`` that does ``import helpers`` names the sibling ``helpers.py``, because
  its directory is first on ``sys.path`` (a script, or a test collected by pytest's default import mode). The dotted
  name carries no directory prefix, so name matching alone never linked the two.
* A regular package inside a tree of plain directories makes its parent the source root, so the module is named
  ``leaf.mod`` while code run from the project root imports it as ``ns.leaf.mod``; the two names never matched.

Fixture layouts are written per test with real ``scan-index`` runs; nothing is mocked.
"""

from __future__ import annotations

import json
from pathlib import Path

from _scan_helpers import index_path_for, run_scan, write_tree

_SCRIPT_DIR_FILES = {
    "tools/helper.py": "VALUE = 1\n",
    "tools/run.py": "import helper\n\nprint(helper.VALUE)\n",
}
_NAMESPACE_FILES = {
    "user.py": "from ns.leaf import mod\n",
    "ns/leaf/__init__.py": "",
    "ns/leaf/mod.py": "VALUE = 1\n",
}


def _scan(scan_index: Path, root: Path, *extra: str) -> dict[str, dict]:
    """Scan *root* and return the index modules keyed by their posix path."""
    run_scan(scan_index, root, *extra)
    index = json.loads(index_path_for(root).read_text())
    return {module["path"]: module for module in index["modules"]}


class TestScriptDirectoryImports:
    """Bare imports in a directory without ``__init__.py`` resolve against that directory."""

    def test_links_the_sibling_module_a_bare_import_names(self, scan_index, tmp_path):
        """A script importing ``helper`` gains an edge to the sibling ``tools/helper.py``.

        Two files sit in a directory with no package marker. Without sibling resolution the import stays an unresolved
        bare name and the script looks as though it depends on nothing in the project.
        """
        write_tree(tmp_path, _SCRIPT_DIR_FILES)

        modules = _scan(scan_index, tmp_path)

        assert modules["tools/helper.py"]["name"] in modules["tools/run.py"]["direct_imports"]

    def test_counts_the_script_as_a_reverse_dependency(self, scan_index, tmp_path):
        """The sibling module's reverse-dependency count includes the importing script.

        ``rdeps`` answers come from the same edge, so a missed sibling edge also hides the importer from blast-radius
        queries on the helper.
        """
        write_tree(tmp_path, _SCRIPT_DIR_FILES)

        modules = _scan(scan_index, tmp_path)

        assert modules["tools/helper.py"]["rdep_count"] == 1

    def test_leaves_a_file_inside_a_regular_package_alone(self, scan_index, tmp_path):
        """A bare import inside a regular package gains no sibling edge.

        Inside a package a sibling is reached by a relative or absolute import, so ``import b`` there names a top-level
        module, not ``pkgdir/b.py``. Linking it would invent an edge the interpreter would not follow.
        """
        write_tree(
            tmp_path,
            {"pkgdir/__init__.py": "", "pkgdir/a.py": "import b\n", "pkgdir/b.py": "VALUE = 1\n"},
        )

        modules = _scan(scan_index, tmp_path)

        assert modules["pkgdir/b.py"]["name"] not in modules["pkgdir/a.py"]["direct_imports"]

    def test_keeps_the_edge_across_an_incremental_scan(self, scan_index, tmp_path):
        """An incremental scan that re-parses only an unrelated file keeps the sibling edge.

        The edge is derived from stored fields on every scan; an incremental pass must recompute it instead of dropping
        it when the importing file itself was not re-parsed.
        """
        write_tree(tmp_path, {**_SCRIPT_DIR_FILES, "tools/other.py": "OTHER = 1\n"})
        _scan(scan_index, tmp_path)
        (tmp_path / "tools" / "other.py").write_text("OTHER = 2\n")

        modules = _scan(scan_index, tmp_path, "--incremental")

        assert modules["tools/helper.py"]["name"] in modules["tools/run.py"]["direct_imports"]


class TestRootQualifiedImports:
    """An import written with the project-root path of a module named from a nested source root resolves."""

    def test_links_the_module_named_from_a_nested_root(self, scan_index, tmp_path):
        """``from ns.leaf import mod`` links to ``ns/leaf/mod.py`` although that module is named ``leaf.mod``.

        ``leaf`` is the only regular package, in a plain directory ``ns``, so ``ns`` becomes the source root and the
        module's own name omits it. The importer runs from the project root and spells the full path.
        """
        write_tree(tmp_path, _NAMESPACE_FILES)

        modules = _scan(scan_index, tmp_path)

        assert modules["ns/leaf/mod.py"]["name"] in modules["user.py"]["direct_imports"]

    def test_counts_the_importer_as_a_reverse_dependency(self, scan_index, tmp_path):
        """The module's reverse-dependency count includes the importer that spelled the qualified path.

        ``rdeps`` reads the same edge, so a missed link also hides the importer from blast-radius queries.
        """
        write_tree(tmp_path, _NAMESPACE_FILES)

        modules = _scan(scan_index, tmp_path)

        assert modules["ns/leaf/mod.py"]["rdep_count"] == 1

    def test_importing_the_plain_directory_adds_no_edge(self, scan_index, tmp_path):
        """``import ns`` names a directory with no module file, so it links to nothing.

        The qualified-name map holds only real module files; a namespace directory must not match a module that happens
        to live below it.
        """
        write_tree(tmp_path, {**_NAMESPACE_FILES, "user.py": "import ns\n"})

        modules = _scan(scan_index, tmp_path)

        assert modules["ns/leaf/mod.py"]["rdep_count"] == 0

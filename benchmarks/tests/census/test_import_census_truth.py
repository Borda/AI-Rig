"""Truth rules of the import-edge census, one synthetic fixture repository per rule."""

from __future__ import annotations

from pathlib import Path

import pytest
from _bench_census.truth import build_truth, source_universe


def _write(root: Path, files: dict[str, str]) -> Path:
    """Create *files* (relative path to text) under *root* and return *root*."""
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def test_plain_package_edges_name_the_module_the_statement_names(tmp_path: Path) -> None:
    """``import a.b`` targets ``a/b.py``; ``from a import b`` targets the submodule; a symbol import targets ``a``."""
    root = _write(
        tmp_path,
        {
            "app/__init__.py": "VALUE = 1\n",
            "app/core.py": "",
            "app/util.py": "",
            "main.py": "import app.core\nfrom app import util\nfrom app import VALUE\n",
        },
    )
    truth = build_truth(root)
    assert truth.edges == {("main.py", "app/core.py"), ("main.py", "app/util.py"), ("main.py", "app/__init__.py")}
    assert truth.script_directory_only == frozenset()


def test_parent_packages_executed_on_the_way_are_not_edges(tmp_path: Path) -> None:
    """Importing ``pkg.sub.mod`` yields only the ``mod`` edge, never ``pkg/__init__.py`` or ``pkg/sub/__init__.py``."""
    root = _write(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/sub/__init__.py": "",
            "pkg/sub/mod.py": "",
            "pkg/user.py": "import pkg.sub.mod\nfrom .sub import mod\n",
        },
    )
    assert build_truth(root).edges == {("pkg/user.py", "pkg/sub/mod.py")}


def test_script_directory_resolves_bare_sibling_imports(tmp_path: Path) -> None:
    """A file outside every package searches its own directory first; those edges are recorded as script-only."""
    root = _write(
        tmp_path,
        {
            "tools/run.py": "import helper\nfrom common import thing\nimport lib.mod\n",
            "tools/helper.py": "",
            "tools/common.py": "thing = 1\n",
            "lib/__init__.py": "",
            "lib/mod.py": "",
        },
    )
    truth = build_truth(root)
    script_edges = {("tools/run.py", "tools/helper.py"), ("tools/run.py", "tools/common.py")}
    assert truth.edges == {*script_edges, ("tools/run.py", "lib/mod.py")}
    assert truth.script_directory_only == script_edges


def test_package_file_does_not_search_its_own_directory(tmp_path: Path) -> None:
    """A bare ``import sibling`` inside a package does not resolve to the sibling file (no implicit relative import)."""
    root = _write(tmp_path, {"pkg/__init__.py": "", "pkg/a.py": "import b\n", "pkg/b.py": ""})
    assert build_truth(root).edges == frozenset()


def test_nested_source_root_resolves_src_layout(tmp_path: Path) -> None:
    """``src/`` joins the search path, so ``import lib`` from the root reaches ``src/lib``."""
    root = _write(
        tmp_path,
        {
            "src/lib/__init__.py": "",
            "src/lib/core.py": "from lib import helpers\n",
            "src/lib/helpers.py": "",
            "tests/test_core.py": "from lib.core import thing\n",
        },
    )
    assert build_truth(root).edges == {
        ("src/lib/core.py", "src/lib/helpers.py"),
        ("tests/test_core.py", "src/lib/core.py"),
    }


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("def f():\n    import pkg.target\n", id="function-level"),
        pytest.param("try:\n    import pkg.target\nexcept ImportError:\n    pass\n", id="try-block"),
        pytest.param(
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from pkg import target\n", id="type-checking"
        ),
    ],
)
def test_every_import_node_counts(tmp_path: Path, body: str) -> None:
    """Imports inside functions, ``try`` blocks and ``TYPE_CHECKING`` blocks are edges."""
    root = _write(tmp_path, {"pkg/__init__.py": "", "pkg/target.py": "", "pkg/user.py": body})
    assert build_truth(root).edges == {("pkg/user.py", "pkg/target.py")}


def test_relative_import_outside_a_package_is_ignored(tmp_path: Path) -> None:
    """A relative import in a plain script has no package to anchor it and yields nothing."""
    root = _write(tmp_path, {"scripts/a.py": "from . import b\n", "scripts/b.py": ""})
    assert build_truth(root).edges == frozenset()


def test_star_import_and_self_import(tmp_path: Path) -> None:
    """``from pkg.mod import *`` targets ``pkg/mod.py``; a module importing itself yields no self-edge."""
    root = _write(
        tmp_path,
        {"pkg/__init__.py": "", "pkg/mod.py": "import pkg.mod\n", "pkg/user.py": "from pkg.mod import *\n"},
    )
    assert build_truth(root).edges == {("pkg/user.py", "pkg/mod.py")}


def test_universe_skips_build_venv_and_virtualenv_trees(tmp_path: Path) -> None:
    """Build, dist, venv, cache and any tree holding ``pyvenv.cfg`` stay outside the universe."""
    root = _write(
        tmp_path,
        {
            "keep/__init__.py": "",
            "build/lib/x.py": "",
            "dist/y.py": "",
            ".venv/z.py": "",
            "custom_env/pyvenv.cfg": "",
            "custom_env/lib/site.py": "",
            "node_modules/pkg/w.py": "",
        },
    )
    assert [path.relative_to(root.resolve()).as_posix() for path in source_universe(root.resolve())] == [
        "keep/__init__.py"
    ]


def test_unparsable_source_is_recorded_not_fatal(tmp_path: Path) -> None:
    """A syntax error lands in ``parse_failures`` and the rest of the repository still resolves."""
    root = _write(tmp_path, {"bad.py": "def (:\n", "good.py": "import other\n", "other.py": ""})
    truth = build_truth(root)
    assert truth.parse_failures == ("bad.py",)
    assert truth.edges == {("good.py", "other.py")}

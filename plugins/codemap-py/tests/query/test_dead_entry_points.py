"""Dead-code queries treat entry points declared in ``pyproject.toml`` as references.

A console script, GUI script or plugin-group target is loaded by packaging tools, never imported by project code, so
before the index recorded entry points ``dead-symbols`` listed every script target and ``dead-modules`` listed every
script module. Each case builds a small project, scans it with the real ``scan-index`` and queries it with the real
``scan-query``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from _scan_helpers import index_path_for, run_query, write_tree
from codemap_py.schema import SCAN_VERSION

_BODY = "    total = 0\n    for step in range(3):\n        total += step\n        total *= 2\n    total -= 1\n    return total\n"
_SOURCES = {
    "pkg/__init__.py": "",
    "pkg/helpers.py": f"def helper():\n{_BODY}",
    "pkg/cli.py": f"from pkg import helpers\n\n\ndef main():\n{_BODY}\n\ndef unused():\n{_BODY}",
    "pkg/tools.py": f"class Runner:\n    def run(self):\n{_BODY.replace('    ', '        ')}",
    "pkg/plugin.py": f"def pytest_configure(config):\n{_BODY}",
    "pkg/orphan.py": f"def orphan():\n{_BODY}",
}
_PROJECT_TABLE = '[project]\nname = "demo"\nversion = "0.1"\n'
_ENTRY_TABLES = (
    '\n[project.scripts]\ndemo-cli = "pkg.cli:main"  # console script\n'
    '\n[project.entry-points.pytest11]\ndemo = "pkg.plugin"\n'
    '\n[project.entry-points."demo.hooks"]\nrunner = "pkg.tools:Runner.run [fast]"\nghost = "missing.mod:main"\n'
    # Non-entry mentions of pkg.orphan: a Poetry script and a generic config reference must not keep it alive.
    '\n[tool.poetry.scripts]\norphan-cli = "pkg.orphan:orphan"\n\n[tool.demo]\nhelper_module = "pkg.orphan"\n'
)


def _scan(scan_index: Path, root: Path, *extra: str) -> str:
    """Run scan-index over *root*, assert success and return its stderr."""
    result = subprocess.run(
        [sys.executable, str(scan_index), "--root", str(root), *extra],
        capture_output=True,
        text=True,
        cwd=str(root),
    )
    assert result.returncode == 0, result.stderr
    return result.stderr


def _project(root: Path, *, with_entry_points: bool) -> Path:
    """Write the fixture project under *root*, with or without the entry-point tables, and return *root*."""
    write_tree(root, {**_SOURCES, "pyproject.toml": _PROJECT_TABLE + (_ENTRY_TABLES if with_entry_points else "")})
    return root


def _dead_symbols(scan_query: Path, root: Path) -> set[tuple[str, str]]:
    """Return ``(module, qualified_name)`` for every dead-symbols finding of the project's index."""
    payload = run_query(scan_query, root, index_path_for(root), "dead-symbols")
    return {(item["module"], item["qualified_name"]) for item in payload["dead"]}


def _dead_modules(scan_query: Path, root: Path) -> set[str]:
    """Return the module names dead-modules reports for the project's index."""
    payload = run_query(scan_query, root, index_path_for(root), "dead-modules")
    return {item["name"] for item in payload["dead_modules"]}


def _graph(root: Path) -> dict[str, tuple[list[str], int]]:
    """Return each module's ``(direct_imports, rdep_count)`` from the project's index."""
    index = json.loads(index_path_for(root).read_text(encoding="utf-8"))
    return {m["name"]: (m["direct_imports"], m["rdep_count"]) for m in index["modules"]}


class TestDeadQueriesHonourEntryPoints:
    """Entry-point targets leave the dead lists; everything else stays as before."""

    def test_dead_symbols_omit_script_and_nested_callable_targets(
        self, tmp_path: Path, scan_index: Path, scan_query: Path
    ) -> None:
        """Dead-symbols drops a console-script function and both parts of a ``Cls.method`` target.

        The project declares ``pkg.cli:main``, a module-only ``pytest11`` plugin and ``pkg.tools:Runner.run``. The
        unreferenced ``unused`` and ``orphan`` functions stay listed, and the module-only plugin target exempts no
        symbol, so ``pytest_configure`` stays listed too.
        """
        root = _project(tmp_path / "demo", with_entry_points=True)
        _scan(scan_index, root)
        assert _dead_symbols(scan_query, root) == {
            ("pkg.cli", "unused"),
            ("pkg.orphan", "orphan"),
            ("pkg.plugin", "pytest_configure"),
        }

    def test_dead_modules_omit_entry_point_modules(self, tmp_path: Path, scan_query: Path, scan_index: Path) -> None:
        """Dead-modules drops every module an entry point names and keeps an unreferenced one.

        ``pkg.cli``, ``pkg.tools`` and the ``pytest11`` plugin ``pkg.plugin`` have no importer but are named by entry
        points; ``pkg.orphan`` has neither and must remain a dead-module finding.
        """
        root = _project(tmp_path / "demo", with_entry_points=True)
        _scan(scan_index, root)
        dead = _dead_modules(scan_query, root)
        assert {"pkg.cli", "pkg.tools", "pkg.plugin"}.isdisjoint(dead)
        assert "pkg.orphan" in dead

    def test_entry_point_refs_leave_import_graph_unchanged(self, tmp_path: Path, scan_index: Path) -> None:
        """Declaring entry points changes no module's ``direct_imports`` or ``rdep_count``.

        The same sources are scanned once without and once with the entry-point tables; the import graph, which the
        import-edge census scores, must be identical, while the indexed ``pkg.cli`` gains its entry-point record and the
        unresolved ``missing.mod`` target attaches to nothing.
        """
        plain = _project(tmp_path / "plain" / "demo", with_entry_points=False)
        declared = _project(tmp_path / "declared" / "demo", with_entry_points=True)
        _scan(scan_index, plain)
        _scan(scan_index, declared)
        modules = json.loads(index_path_for(declared).read_text(encoding="utf-8"))["modules"]
        refs = {m["name"]: m["entry_point_refs"] for m in modules if "entry_point_refs" in m}
        assert _graph(plain) == _graph(declared)
        assert sorted(refs) == ["pkg.cli", "pkg.plugin", "pkg.tools"]
        assert refs["pkg.cli"] == [
            {"group": "console_scripts", "name": "demo-cli", "symbol": "main", "file": "pyproject.toml", "line": 6}
        ]


class TestIncrementalEntryPoints:
    """Incremental scans re-read entry points when only ``pyproject.toml`` changes."""

    @pytest.mark.parametrize(
        ("declared_before", "declared_after", "main_dead_after"),
        [
            pytest.param(False, True, False, id="adding-script-removes-finding"),
            pytest.param(True, False, True, id="removing-script-restores-finding"),
        ],
    )
    def test_pyproject_only_edit_updates_dead_symbols(
        self,
        tmp_path: Path,
        scan_index: Path,
        scan_query: Path,
        declared_before: bool,
        declared_after: bool,
        main_dead_after: bool,
    ) -> None:
        """Editing only ``pyproject.toml`` and scanning with ``--incremental`` updates the dead-symbols result.

        A full scan indexes the project, then only the entry-point tables are added or removed and an incremental
        scan runs. Its stderr must show the incremental path ran, and ``pkg.cli::main`` must be listed exactly when
        no console script names it, so a stale record carried on the unchanged module would fail the removal case.
        """
        root = _project(tmp_path / "demo", with_entry_points=declared_before)
        _scan(scan_index, root)
        _project(root, with_entry_points=declared_after)
        stderr = _scan(scan_index, root, "--incremental")
        assert "Incremental scan" in stderr
        assert (("pkg.cli", "main") in _dead_symbols(scan_query, root)) is main_dead_after

    def test_previous_generation_index_rebuilds_with_entry_point_refs(self, tmp_path: Path, scan_index: Path) -> None:
        """An index stamped one generation back is rebuilt by a full scan that records entry points.

        The fixture index is rewritten as the previous generation without any ``entry_point_refs``, as an older scanner
        wrote it. ``--incremental`` must refuse to reuse it, fall back to a full scan and write the current generation
        with the console-script record restored.
        """
        root = _project(tmp_path / "demo", with_entry_points=True)
        _scan(scan_index, root)
        index_file = index_path_for(root)
        index = json.loads(index_file.read_text(encoding="utf-8"))
        index["scan_version"] = SCAN_VERSION - 1
        index["modules"] = [{k: v for k, v in m.items() if k != "entry_point_refs"} for m in index["modules"]]
        index_file.write_text(json.dumps(index), encoding="utf-8")
        stderr = _scan(scan_index, root, "--incremental")
        rebuilt = json.loads(index_file.read_text(encoding="utf-8"))
        cli = next(m for m in rebuilt["modules"] if m["name"] == "pkg.cli")
        assert f"pre-v{SCAN_VERSION} index found" in stderr
        assert rebuilt["scan_version"] == SCAN_VERSION
        assert cli["entry_point_refs"][0]["symbol"] == "main"

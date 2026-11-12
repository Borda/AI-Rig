"""Integration tests for scan-query bin script.

Uses the shared `project` fixture from conftest.py (scan-index run once,
module-scoped). Tests module-level and symbol-level queries, call-graph
commands, and edge cases.

Fixture layout:
    gamma.py          — leaf module, no imports; defines func_gamma
    beta.py           — imports gamma; defines func_beta calling func_gamma
    alpha.py          — imports beta, gamma; defines func_alpha calling func_beta
    pkg/__init__.py   — empty
    pkg/delta.py      — imports alpha; defines func_delta calling func_alpha
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from codemap_py import index_paths


def _load_scan_query():
    """Import codemap_py.query for unit-level tests.

    ``bin/scan-query`` is a thin launcher with no module-level internals of its own — every ``cmd_*``/private helper
    this file reaches lives in :mod:`codemap_py.query`, so tests import the package module directly instead of loading
    the thin bin script via ``SourceFileLoader``.
    """
    src_dir = Path(__file__).resolve().parent.parent.parent / "src"
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))
    import codemap_py.query as mod

    return mod


_scan_query_mod = _load_scan_query()
_require_feature = _scan_query_mod._require_feature
_has_call_graph = _scan_query_mod._has_call_graph
_find_index = _scan_query_mod.find_index


def _load_scan_index():
    """Import codemap_py.scanner for unit-level tests (bin/scan-index is now a thin launcher).

    These particular helpers (``extract_conftest_syspath``, ``extract_subprocess_calls``, ``extract_fixtures``) moved
    into :mod:`codemap_py.scanner`; import the package module directly instead of loading the bin script.
    """
    src_dir = Path(__file__).resolve().parent.parent.parent / "src"
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))
    import codemap_py.scanner as mod

    return mod


def _write_files(root: Path, files: dict[str, str]) -> None:
    """Write each ``{relative name: content}`` pair under *root*.

    >>> import tempfile
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     _write_files(Path(tmp), {"a.py": "x = 1\\n"})
    ...     (Path(tmp) / "a.py").read_text()
    'x = 1\\n'
    """
    for name, content in files.items():
        (root / name).write_text(content)


_scan_index_mod = _load_scan_index()
_extract_conftest = _scan_index_mod.extract_conftest_syspath
_extract_subprocess = _scan_index_mod.extract_subprocess_calls
_extract_fixtures = _scan_index_mod.extract_fixtures


def _build_index(scan_index: Path, root: Path, *extra: str) -> Path:
    """Run scan-index over *root* (plus *extra* flags) and return the index path it wrote, requiring exit 0."""
    result = subprocess.run(
        [sys.executable, str(scan_index), "--root", str(root), *extra], capture_output=True, text=True, cwd=str(root)
    )
    assert result.returncode == 0, result.stderr
    return root / ".cache" / "codemap" / f"{root.name}.json"


def _run_query(scan_query: Path, index_path: Path, *args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run scan-query against *index_path* from *cwd*; callers assert on the returned process."""
    return subprocess.run(
        [sys.executable, str(scan_query), "--index", str(index_path), *args],
        capture_output=True,
        text=True,
        cwd=str(cwd),
    )


def _scan_and_query(root: Path, scan_index: Path, scan_query: Path, query: list[str]) -> tuple[int, dict, str]:
    """Run scan-index over *root*, then scan-query with *query*; return ``(rc, parsed_json, stderr)``."""
    result = _run_query(scan_query, _build_index(scan_index, root), *query, cwd=root)
    return result.returncode, json.loads(result.stdout), result.stderr


def _touch_json(root: Path, *relative: str) -> None:
    """Create each *relative* path under *root* as an empty JSON object, making parent directories."""
    for rel in relative:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")


class TestModuleQueries:
    """Module-level dependency queries: rdeps, deps, path, central, list."""

    @pytest.fixture(name="fan_in_index")
    def _fan_in_index(self, tmp_path) -> tuple[Path, Path, list[str]]:
        """Index of 1000 modules that all import ``target``; return ``(root, index_path, importers)``."""
        root = tmp_path / "rdeps_preview"
        root.mkdir()
        index_path = root / "index.json"
        importers = [f"caller_{number:04d}" for number in range(1000)]
        modules = [{"name": "target", "status": "ok", "path": "target.py", "direct_imports": []}]
        modules += [
            {"name": name, "status": "ok", "path": f"{name}.py", "direct_imports": ["target"]} for name in importers
        ]
        index_path.write_text(
            json.dumps({"scan_version": 11, "scan_root": str(root), "collisions": [], "modules": modules})
        )
        return root, index_path, importers

    @pytest.fixture(name="submodule_query", scope="module")
    @classmethod
    def _submodule_query(cls, tmp_path_factory, scan_index, scan_query) -> Callable[..., dict]:
        """Index a project whose ``consumer`` runs ``from package import child``; return a JSON query callable."""
        root = tmp_path_factory.mktemp("from_import_submodule")
        (root / "package").mkdir()
        _write_files(root, {"package/__init__.py": "", "package/child.py": "VALUE = 1\n"})
        (root / "consumer.py").write_text("from package import child\n")
        index_path = _build_index(scan_index, root)

        def run_query(*args: str) -> dict:
            """Run one query and decode its JSON stdout, requiring exit 0."""
            result = _run_query(scan_query, index_path, *args, cwd=root)
            assert result.returncode == 0, result.stderr + result.stdout
            return json.loads(result.stdout)

        return run_query

    def test_rdeps_lists_every_importer(self, query):
        """Rdeps of the leaf module lists every module that imports it.

        ``gamma`` is imported by both ``alpha`` and ``beta``; the answer must contain each, not just the first hit.
        """
        data = query("rdeps", "gamma")
        assert {"alpha", "beta"} <= set(data["imported_by"])

    def test_rdeps_excludes_non_importers(self, query):
        """Modules that do not import the target never appear in its rdeps.

        ``pkg.delta`` imports only ``alpha``, so it must not be listed as an importer of ``gamma``.
        """
        data = query("rdeps", "gamma")
        assert "pkg.delta" not in data["imported_by"]

    def test_rdeps_entity_filter_accepts_existing_cli_value(self, query):
        """The string ``--entity`` value still filters importers after enum conversion.

        The fixture project is queried with ``--entity pkg``, the spelling existing scripts use, and still returns both
        importers of ``gamma``; this guards the string-to-enum conversion of the option.
        """
        data = query("rdeps", "gamma", "--entity", "pkg")
        assert set(data["imported_by"]) == {"alpha", "beta"}

    def test_rdeps_limit_previews_bounded_slice_with_truthful_metadata(self, fan_in_index, scan_query):
        """An explicit limit previews a high-fan-in module without claiming exhaustion.

        With 1000 importers and ``--limit 20`` the first 20 come back, ``truncated`` is set, and ``total_available``
        reports the full count while the query itself stays complete.
        """
        root, index_path, importers = fan_in_index

        preview = _run_query(scan_query, index_path, "rdeps", "target", "--limit", "20", cwd=root)

        assert preview.returncode == 0, preview.stderr + preview.stdout
        preview_data = json.loads(preview.stdout)
        assert preview_data["imported_by"] == importers[:20]
        assert preview_data["index"]["truncated"] is True
        assert preview_data["index"]["total_available"] == len(importers)
        assert preview_data["index"]["query_complete"] is True

    def test_rdeps_limit_zero_returns_full_list(self, fan_in_index, scan_query):
        """``--limit 0`` disables the preview cap and drops the truncation metadata.

        The full 1000-importer list is returned, with neither ``truncated`` nor ``total_available`` in the index block.
        """
        root, index_path, importers = fan_in_index

        full = _run_query(scan_query, index_path, "rdeps", "target", "--limit", "0", cwd=root)

        assert full.returncode == 0, full.stderr + full.stdout
        full_data = json.loads(full.stdout)
        assert full_data["imported_by"] == importers
        assert "truncated" not in full_data["index"]
        assert "total_available" not in full_data["index"]

    def test_rdeps_rejects_negative_limit(self, project, scan_query):
        """Only zero and positive preview limits have defined output semantics.

        ``rdeps gamma --limit -1`` exits 2 with the limit message on stderr. A negative value has no preview meaning, so
        it is refused rather than slicing from the end of the list.
        """
        root, index_path = project

        result = _run_query(scan_query, index_path, "rdeps", "gamma", "--limit", "-1", cwd=root)

        assert result.returncode == 2
        assert "rdeps --limit must be 0 or a positive integer" in result.stderr

    def test_rdeps_unknown_module_exits_3(self, project, scan_query):
        """Rdeps on a module absent from the index errors (exit 3) instead of an empty imported_by list.

        An empty list would read as "nothing imports it"; the structured error carries suggestions instead.
        """
        root, index_path = project

        result = _run_query(scan_query, index_path, "rdeps", "nonexistent.module.xyz", cwd=root)

        assert result.returncode == 3, result.stderr + result.stdout
        data = json.loads(result.stdout)
        assert data["error"] == "module not indexed"
        assert data["module"] == "nonexistent.module.xyz"
        assert "suggestions" in data

    def test_deps_lists_direct_imports(self, query):
        """Deps of ``alpha`` lists both modules it imports, ``beta`` and ``gamma``.

        In the fixture project ``alpha`` imports ``beta`` and ``gamma``; ``deps`` must report both as direct imports.
        """
        data = query("deps", "alpha")
        assert {"beta", "gamma"} <= set(data["direct_imports"])

    def test_central_ranks_most_imported_module_above_unimported(self, query):
        """The most-imported module ranks above one nobody imports.

        ``gamma`` is imported by ``alpha`` and ``beta`` while ``pkg.delta`` has no importers, so its rank must be higher
        (a smaller index) in the central listing.
        """
        data = query("central", "--top", "10")
        names = [entry["name"] for entry in data["central"]]
        assert "gamma" in names
        assert names.index("gamma") < names.index("pkg.delta")

    def test_path_finds_three_module_import_chain(self, query):
        """``pkg.delta`` reaches ``gamma`` through a 3-module import chain via ``alpha``.

        ``pkg.delta`` imports ``alpha``, which imports ``gamma``, so the chain has three modules in order; an empty or
        longer answer means the import-graph walk broke.
        """
        data = query("path", "pkg.delta", "gamma")
        path = data["path"]
        assert path is not None, "expected a path, got null"
        assert path[0] == "pkg.delta"
        assert path[-1] == "gamma"
        assert len(path) == 3  # delta → alpha → gamma

    def test_path_reports_reason_when_no_import_path(self, query):
        """A legitimate no-path result reports ``reason`` (exit 0), never the ``error`` failure key.

        ``gamma`` imports nothing, so there is no ``gamma`` to ``alpha`` path; the answer is a null path with a reason.
        """
        data = query("path", "gamma", "alpha")
        assert data["path"] is None
        assert data["reason"] == "no-import-path"
        assert "error" not in data

    def test_path_same_module_reports_path_key(self, project, scan_query):
        """Path from a module to itself answers with a ``path`` key rather than crashing.

        The answer may be ``[A]`` or null; the contract is that the query completes (exit 0) and keeps the key.
        """
        root, index_path = project

        result = _run_query(scan_query, index_path, "path", "gamma", "gamma", cwd=root)

        assert result.returncode == 0
        assert "path" in json.loads(result.stdout)

    def test_deps_resolves_from_import_submodule(self, submodule_query):
        """``from package import child`` is a dependency edge to ``package.child`` in the dependency query.

        The module-scoped project has ``consumer.py`` doing ``from package import child`` where ``child`` is a
        submodule, so the edge must point at ``package.child`` and not only at the ``package`` namespace.
        """
        assert "package.child" in submodule_query("deps", "consumer")["direct_imports"]

    def test_rdeps_resolves_from_import_submodule(self, submodule_query):
        """``from package import child`` makes ``consumer`` the reverse dependency of ``package.child``.

        Same ``from package import child`` project, reverse view: the importer list of ``package.child`` is exactly
        ``consumer``.
        """
        assert submodule_query("rdeps", "package.child")["imported_by"] == ["consumer"]

    def test_path_resolves_from_import_submodule(self, submodule_query):
        """``from package import child`` yields a one-hop import path from ``consumer`` to ``package.child``.

        Same ``from package import child`` project, path view: the shortest path from ``consumer`` to ``package.child``
        is the single direct edge.
        """
        assert submodule_query("path", "consumer", "package.child")["path"] == ["consumer", "package.child"]

    def test_list_contains_all_modules(self, query):
        """List command returns all 5 modules.

        ``list`` over the five-module fixture project reports every module name, including the empty ``pkg`` package.
        """
        data = query("list")
        names = {m["name"] for m in data["modules"]}
        assert {"alpha", "beta", "gamma", "pkg", "pkg.delta"}.issubset(names)


class TestCentralExcludingTests:
    """Rank the production import graph."""

    @staticmethod
    def _module(
        name: str,
        *,
        direct_imports: list[str] | None = None,
        is_test: bool = False,
        rdep_count: int = 0,
    ) -> dict:
        """Build the minimum index module entry consumed by ``cmd_central``."""
        return {
            "name": name,
            "status": "ok",
            "path": f"{name.replace('.', '/')}.py",
            "direct_imports": direct_imports or [],
            "is_test": is_test,
            "rdep_count": rdep_count,
        }

    @staticmethod
    def _production_indegree(index: dict) -> dict[str, int]:
        """Compute production-only import counts without consulting stored metrics.

        >>> TestCentralExcludingTests._production_indegree(
        ...     {"modules": [{"is_test": False, "direct_imports": ["a"]}, {"is_test": True, "direct_imports": ["a"]}]}
        ... )
        {'a': 1}
        """
        aliases = index.get("module_aliases", {})
        counts: dict[str, int] = {}
        for module in index["modules"]:
            if module["is_test"]:
                continue
            for imported in module["direct_imports"]:
                target = aliases.get(imported, imported)
                counts[target] = counts.get(target, 0) + 1
        return counts

    @classmethod
    def _index(cls) -> dict:
        """Build an index with a test importer, an aliased production importer and stale stored counts."""
        return {
            "module_aliases": {"target_alias": "target"},
            "modules": [
                cls._module("target", rdep_count=40),
                cls._module("aaa", rdep_count=8),
                cls._module("zzz", rdep_count=8),
                cls._module("prod.one", direct_imports=["target", "aaa"]),
                cls._module("prod.two", direct_imports=["target_alias", "zzz"]),
                cls._module("tests.test_target", direct_imports=["target"], is_test=True, rdep_count=1),
            ],
        }

    def test_central_counts_all_importers_by_default(self, capsys):
        """Without ``--exclude-tests`` the ranking keeps the stored counts and the test importer.

        ``target`` carries a stored ``rdep_count`` of 40, so it tops the ranking regardless of the imports listed.
        """
        _scan_query_mod.cmd_central(self._index(), top=10, exclude_tests=False)

        central = json.loads(capsys.readouterr().out)["central"]

        assert central[0] == {"name": "target", "rdep_count": 40, "path": "target.py"}

    def test_central_excluding_tests_recomputes_production_importers(self, capsys):
        """The flag removes test candidates and recomputes incoming edges from production importers only.

        Aliased imports (``target_alias``) must count toward ``target``; stored ``rdep_count`` values are ignored.
        """
        index = self._index()

        _scan_query_mod.cmd_central(index, top=10, exclude_tests=True)

        central = json.loads(capsys.readouterr().out)["central"]
        production_indegree = self._production_indegree(index)
        expected = sorted(
            (
                {
                    "name": module["name"],
                    "rdep_count": production_indegree.get(module["name"], 0),
                    "path": module["path"],
                }
                for module in index["modules"]
                if not module["is_test"]
            ),
            key=lambda module: (-module["rdep_count"], module["name"]),
        )
        assert central == expected


class TestCoupledQueries:
    """Module coupling rankings."""

    def test_coupled_orders_equal_scores_by_module_name(self, capsys):
        """Equal coupling scores must not depend on index insertion order.

        Two modules import ``target`` and score equally, and the index lists ``zeta`` before ``alpha``. The output must
        still come back name-sorted so results do not depend on scan order.
        """
        index = {
            "modules": [
                {"name": "zeta", "status": "ok", "path": "zeta.py", "direct_imports": ["target"], "dep_count": 1},
                {"name": "alpha", "status": "ok", "path": "alpha.py", "direct_imports": ["target"], "dep_count": 1},
                {"name": "target", "status": "ok", "path": "target.py", "direct_imports": [], "dep_count": 0},
            ]
        }

        _scan_query_mod.cmd_coupled(index, top=2)

        coupled = json.loads(capsys.readouterr().out)["coupled"]
        assert [entry["name"] for entry in coupled] == ["alpha", "zeta"]


def _ranking_index() -> dict:
    """Build an index whose production in-degrees are known by construction.

    ``hub`` is imported by three production modules, ``mid`` by two, ``leaf`` by one, and ``ignored`` by a test module
    only, so a production ranking must order them hub, mid, leaf and score ``ignored`` at zero.
    """

    def module(name: str, imports: list[str] | None = None, *, is_test: bool = False) -> dict:
        return {
            "name": name,
            "status": "ok",
            "path": f"{name.replace('.', '/')}.py",
            "direct_imports": imports or [],
            "is_test": is_test,
            "rdep_count": 0,
        }

    return {
        "modules": [
            module("hub"),
            module("mid"),
            module("leaf"),
            module("ignored"),
            module("prod.one", ["hub", "mid", "leaf", "target"]),
            module("prod.two", ["hub", "mid", "target"]),
            module("prod.three", ["hub", "target"]),
            module("tests.test_target", ["ignored", "target"], is_test=True),
        ],
    }


class TestCentralAmong:
    """Rank a caller-supplied candidate set instead of the whole repository."""

    def test_ranks_only_the_requested_modules(self, capsys):
        """``--among`` restricts the ranking to the named modules, ordered by their own in-degree.

        This is the question left over after an ``rdeps`` call — order *these* importers by how exposed they are — which
        previously forced the caller to intersect a repository-wide ranking against its own list.
        """
        _scan_query_mod.cmd_central(_ranking_index(), top=None, exclude_tests=True, among=["leaf", "hub", "mid"])

        payload = json.loads(capsys.readouterr().out)

        assert [entry["name"] for entry in payload["central"]] == ["hub", "mid", "leaf"]
        assert [entry["rdep_count"] for entry in payload["central"]] == [3, 2, 1]

    def test_reports_requested_modules_the_ranking_could_not_cover(self, capsys):
        """Requested names that match no candidate come back under ``unmatched`` rather than vanishing.

        A typo, or a module the other filters removed, would otherwise leave the caller believing its whole candidate
        set was ranked — the silent-drop failure this field exists to make impossible.
        """
        _scan_query_mod.cmd_central(
            _ranking_index(), top=None, exclude_tests=True, among=["hub", "tests.test_target", "typo.module"]
        )

        payload = json.loads(capsys.readouterr().out)

        assert [entry["name"] for entry in payload["central"]] == ["hub"]
        assert payload["unmatched"] == ["tests.test_target", "typo.module"]
        assert payload["candidate_count"] == 1

    def test_ranks_every_candidate_when_no_top_is_given(self, capsys):
        """Without an explicit ``--top`` a scoped ranking returns the whole candidate set.

        The repository-wide default of ten would silently cut a longer candidate list short, which is the same silent
        truncation the ``unmatched`` field guards against from the other direction.
        """
        index = _ranking_index()
        candidates = [module["name"] for module in index["modules"] if not module["is_test"]]

        _scan_query_mod.cmd_central(index, top=None, exclude_tests=True, among=candidates)

        payload = json.loads(capsys.readouterr().out)

        assert len(payload["central"]) == len(candidates)

    def test_honours_an_explicit_top_within_the_candidate_set(self, capsys):
        """An explicit ``--top`` still caps a scoped ranking, and ``candidate_count`` exposes the full size.

        Three candidates with ``top=2`` return the two best-ranked, while ``candidate_count`` still reports all three so
        the caller can see what was cut.
        """
        _scan_query_mod.cmd_central(_ranking_index(), top=2, exclude_tests=True, among=["leaf", "hub", "mid"])

        payload = json.loads(capsys.readouterr().out)

        assert [entry["name"] for entry in payload["central"]] == ["hub", "mid"]
        assert payload["candidate_count"] == 3


class TestRdepsCounts:
    """Importer totals travel with the importer list."""

    def test_reports_the_importer_total_beside_the_list(self, capsys):
        """``importer_count`` states the total so the caller never counts the returned list by hand.

        ``target`` is imported by three production modules; the payload lists them in name order and states the count of
        three alongside.
        """
        _scan_query_mod.cmd_rdeps(_ranking_index(), "target", exclude_tests=True)

        payload = json.loads(capsys.readouterr().out)

        assert payload["imported_by"] == ["prod.one", "prod.three", "prod.two"]
        assert payload["importer_count"] == 3

    def test_reports_excluded_test_importers_in_the_same_call(self, capsys):
        """Production and test importer totals come from one call, never from subtracting two.

        Deriving the test count as "unfiltered call minus filtered call" is an arithmetic step outside the tool, and an
        off-by-one there is indistinguishable from a wrong graph.
        """
        _scan_query_mod.cmd_rdeps(_ranking_index(), "target", exclude_tests=True)

        payload = json.loads(capsys.readouterr().out)

        assert payload["excluded_test_importer_count"] == 1

    def test_omits_the_excluded_count_when_tests_are_included(self, capsys):
        """Without ``--exclude-tests`` nothing was excluded, so the field is absent rather than zero.

        Without ``exclude_tests`` all four importers (three production, one test) count, and no excluded-test field is
        emitted because nothing was excluded.
        """
        _scan_query_mod.cmd_rdeps(_ranking_index(), "target")

        payload = json.loads(capsys.readouterr().out)

        assert "excluded_test_importer_count" not in payload
        assert payload["importer_count"] == 4

    def test_importer_count_survives_limit_truncation(self, capsys):
        """A truncated preview still reports the full total, matching ``index.total_available``.

        With ``limit=1`` only one importer is listed, yet ``importer_count`` and ``index.total_available`` both stay 4
        so the caller can tell the list was cut.
        """
        _scan_query_mod.cmd_rdeps(_ranking_index(), "target", limit=1)

        payload = json.loads(capsys.readouterr().out)

        assert len(payload["imported_by"]) == 1
        assert payload["importer_count"] == 4
        assert payload["index"]["total_available"] == 4


class TestSymbolQueries:
    """Symbol-level queries: lookup by name, by module, and by regex."""

    def test_symbol_by_name(self, query):
        """Symbol query returns source for func_gamma.

        ``symbol func_gamma`` returns the function's source text from the fixture ``gamma.py``, which defines it as ``x
        + 1``.
        """
        data = query("symbol", "func_gamma")
        assert data.get("symbols"), "expected at least one symbol match"
        src = data["symbols"][0]["source"]
        assert "def func_gamma" in src
        assert "return x + 1" in src

    def test_symbols_in_module(self, query):
        """Symbols alpha lists func_alpha.

        ``symbols alpha`` lists the functions defined in the fixture module ``alpha``, which holds ``func_alpha``.
        """
        data = query("symbols", "alpha")
        names = {s["name"] for s in data["symbols"]}
        assert "func_alpha" in names

    def test_find_symbol_regex(self, query):
        """Find-symbol '^func_' matches all four functions.

        The anchored pattern ``^func_`` matches the qualified names of all four fixture functions, in ``gamma``,
        ``beta``, ``alpha`` and ``pkg.delta``.
        """
        data = query("find-symbol", "^func_")
        names = {m["qualified_name"] for m in data["matches"]}
        assert any("func_gamma" in n for n in names)
        assert any("func_alpha" in n for n in names)
        assert any("func_beta" in n for n in names)
        assert any("func_delta" in n for n in names)


class TestSymbolStaleAndImports:
    """Per-symbol stale field and optional ``--with-imports`` block."""

    @pytest.mark.parametrize(
        ("field", "expected"),
        [
            pytest.param("stale", False, id="stale-false"),
            pytest.param("stale_reason", None, id="stale-reason-is-json-null"),
            pytest.param("imports", None, id="imports-null-without-with-imports-flag"),
        ],
    )
    def test_symbol_fresh_index_field_defaults(self, query, field, expected):
        """A symbol from a fresh index reports stale=False, a JSON-null stale_reason, and null imports by default.

        ``stale_reason`` is the JSON null (Python None), not the string 'None'; without ``--with-imports`` the imports
        field is likewise JSON null.
        """
        data = query("symbol", "func_gamma")
        assert data["symbols"], "expected at least one symbol match"
        assert data["symbols"][0][field] is expected

    def test_symbol_with_imports_flag(self, query):
        """Verify command-line option behavior.

        --with-imports populates the imports field for a module that imports another.
        """
        data = query("symbol", "--with-imports", "func_beta")
        assert data["symbols"], "expected at least one symbol match"
        imports = data["symbols"][0]["imports"]
        assert imports is not None, "imports field should be populated when --with-imports is set"
        assert "import" in imports, f"expected an import statement in imports block, got: {imports!r}"

    def test_symbol_imports_empty_for_no_import_module(self, query):
        """Verify command-line option behavior.

        --with-imports on a module without imports returns an empty string (or None).
        """
        data = query("symbol", "--with-imports", "func_gamma")
        assert data["symbols"], "expected at least one symbol match"
        imports = data["symbols"][0]["imports"]
        assert imports in ("", None), f"expected empty import block for gamma, got: {imports!r}"

    @pytest.mark.parametrize(
        ("filename", "source", "mutate", "symbol", "expected"),
        [
            pytest.param(
                "myfunc.py",
                "def myfunc(x):\n    return x\n",
                lambda path: path.unlink(),
                "myfunc",
                {"stale": True, "stale_reason": "file deleted", "stale_category": "symbol_deleted"},
                id="file-deleted",
            ),
            pytest.param(
                "big.py",
                "def bigfunc(x):\n    return x * 2\n",
                lambda path: path.write_text("# truncated\n"),
                "bigfunc",
                {"stale": True, "stale_reason": "line range past EOF"},
                id="line-range-past-eof",
            ),
        ],
    )
    def test_symbol_marked_stale_when_source_changed_after_indexing(
        self, tmp_path, scan_index, scan_query, filename, source, mutate, symbol, expected
    ):
        """A symbol whose source was deleted or truncated after indexing is reported stale with the reason.

        The index is built first, then ``mutate`` removes the file or cuts it below the indexed end line; ``--no-heal``
        keeps the stale entry visible. A deleted file is the symbol being gone, not merely moved.
        """
        root = tmp_path / "stale"
        root.mkdir()
        (root / filename).write_text(source)
        index_path = _build_index(scan_index, root)
        mutate(root / filename)

        result = _run_query(scan_query, index_path, "--no-heal", "symbol", symbol, cwd=root)

        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["symbols"], "expected symbol still in index"
        sym = data["symbols"][0]
        assert {key: sym[key] for key in expected} == expected

    @pytest.mark.parametrize(
        ("project_dir", "module_file", "content", "symbol", "expected_fragments"),
        [
            pytest.param(
                "docstring",
                "ds.py",
                '"""Module docs."""\nimport os\nfrom pathlib import Path\n\ndef dsfunc(x):\n    return x\n',
                "dsfunc",
                ("import os", "pathlib"),
                id="one-line-module-docstring",
            ),
            pytest.param(
                "multiline",
                "ml.py",
                "from typing import (\n    Any,\n    Optional,\n)\n\ndef mlfunc(x: Any) -> Optional[int]:\n    return x\n",
                "mlfunc",
                ("Any", "Optional"),
                id="multiline-parenthesized-import",
            ),
        ],
    )
    def test_with_imports_extracts_import_block(
        self, tmp_path, scan_index, scan_query, project_dir, module_file, content, symbol, expected_fragments
    ):
        """``--with-imports`` extracts the module's import block, whatever shape its header takes.

        The import block must be found when the file opens with a one-line module docstring and must be captured whole
        when it is a multi-line parenthesized import.
        """
        root = tmp_path / project_dir
        root.mkdir()
        (root / module_file).write_text(content)
        index_path = _build_index(scan_index, root)

        result = _run_query(scan_query, index_path, "symbol", "--with-imports", symbol, cwd=root)

        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["symbols"]
        imports = data["symbols"][0]["imports"]
        assert imports is not None
        assert all(fragment in imports for fragment in expected_fragments), (
            f"expected {expected_fragments} in imports, got: {imports!r}"
        )


class TestScanRoot:
    """scan_root stored in index + ``--root`` flag for file-path resolution."""

    def test_scan_root_stored_in_index(self, project):
        """Scan-index stores a relative scan root with its path-free project identity.

        The saved JSON anchors later source lookups without retaining the resolved host path.
        """
        root, index_path = project
        data = json.loads(index_path.read_text())
        assert "scan_root" in data, "scan_root key missing from index"
        assert data["scan_root"] == "."
        assert data["scan_root_key"] == index_paths.root_key(root.resolve())

    def test_symbol_resolves_via_scan_root(self, tmp_path, scan_index, scan_query):
        """Scan-query resolves file paths via the stored scan_root even when the CWD differs.

        The index is built in ``scan_root_test`` but queried with the parent directory as CWD, so only the ``scan_root``
        recorded in the index can locate the source file and report it fresh.
        """
        root = tmp_path / "scan_root_test"
        root.mkdir()
        (root / "rootfunc.py").write_text("def rootfunc(x):\n    return x * 3\n")
        index_path = _build_index(scan_index, root)

        result = _run_query(scan_query, index_path, "symbol", "rootfunc", cwd=tmp_path)

        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["symbols"]
        sym = data["symbols"][0]
        assert sym["stale"] is False, f"expected stale=False via scan_root, got reason={sym['stale_reason']}"
        assert "def rootfunc" in sym["source"]

    def test_root_flag_overrides_scan_root(self, tmp_path, scan_index, scan_query):
        """The ``--root`` flag takes priority over the scan_root stored in the index.

        The index points at ``dir_a``; an identical copy of the file lives in ``dir_b`` and ``--root dir_b`` must
        redirect source lookup there.
        """
        dir_a = tmp_path / "dir_a"
        dir_b = tmp_path / "dir_b"
        dir_a.mkdir()
        dir_b.mkdir()
        _write_files(dir_a, {"afunc.py": "def afunc(x):\n    return x + 10\n"})
        _write_files(dir_b, {"afunc.py": "def afunc(x):\n    return x + 10\n"})
        index_path = _build_index(scan_index, dir_a)

        result = _run_query(scan_query, index_path, "--root", str(dir_b), "symbol", "afunc", cwd=tmp_path)

        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["symbols"]
        sym = data["symbols"][0]
        assert sym["stale"] is False, f"--root override failed: stale_reason={sym['stale_reason']}"
        assert "def afunc" in sym["source"]


class TestFunctionCallGraph:
    """Function-level call-graph queries (v3 index): fn-deps, fn-rdeps, fn-central, fn-blast."""

    @pytest.mark.parametrize("expected_target", ["beta::func_beta", "gamma::func_gamma"])
    def test_fn_deps_includes_exact_direct_callees(self, query, expected_target):
        """func_alpha calls the exact beta/gamma targets, not only similarly named functions.

        ``func_alpha`` calls ``func_beta`` and ``func_gamma``; the test is parametrized over each expected qualified
        target to prove both edges, not just one, are present.
        """
        data = query("fn-deps", "alpha::func_alpha")
        callees = {e["target"] for e in data.get("calls", [])}
        assert expected_target in callees

    @pytest.mark.parametrize(
        "unexpected_target",
        ["pkg.delta::func_delta", "alpha::func_alpha", "beta::not_func_beta"],
    )
    def test_fn_deps_excludes_unrelated_or_misqualified_callees(self, query, unexpected_target):
        """func_alpha's edge list excludes unrelated and wrongly qualified callees.

        Names a function it never calls, itself, and the near-miss ``beta::not_func_beta``; none may appear, which
        would indicate loose substring matching of targets.
        """
        data = query("fn-deps", "alpha::func_alpha")
        callees = {e["target"] for e in data.get("calls", [])}
        assert unexpected_target not in callees

    @pytest.mark.parametrize("expected_caller", ["alpha::func_alpha", "beta::func_beta"])
    def test_fn_rdeps_includes_exact_direct_callers(self, query, expected_caller):
        """func_gamma is called directly by alpha.func_alpha and beta.func_beta.

        ``func_gamma`` is called from ``alpha`` and ``beta``; each qualified caller is checked individually.
        """
        data = query("fn-rdeps", "gamma::func_gamma")
        callers = {e["caller"] for e in data.get("called_by", [])}
        assert expected_caller in callers

    def test_fn_rdeps_reports_no_callers_for_leaf_driver(self, query):
        """pkg.delta::func_delta has no caller in the fixture project.

        ``func_delta`` is the top of the fixture call chain, so ``called_by`` is empty and ``count`` is 0: an
        answer, not an error.
        """
        data = query("fn-rdeps", "pkg.delta::func_delta")
        assert data["called_by"] == []
        assert data["count"] == 0

    def test_fn_rdeps_dedupes_repeated_calls_from_same_caller(self, tmp_path, scan_index, scan_query):
        """A caller that invokes the same target twice is reported once.

        ``caller`` calls ``target.callee()`` on two lines; the answer lists one caller entry and counts one caller.
        """
        root = tmp_path / "repeat_calls"
        root.mkdir()
        _write_files(
            root,
            {
                "target.py": "def callee():\n    return 1\n",
                "caller.py": "import target\n\ndef caller():\n    target.callee()\n    return target.callee()\n",
            },
        )
        index_path = _build_index(scan_index, root)

        query_result = _run_query(scan_query, index_path, "fn-rdeps", "target::callee", cwd=root)

        assert query_result.returncode == 0, query_result.stderr
        data = json.loads(query_result.stdout)
        assert [e["caller"] for e in data["called_by"]] == ["caller::caller"]
        assert data["count"] == 1
        assert data["unique_caller_count"] == 1

    def test_fn_rdeps_unique_caller_count_matches_deduped_callers(self, query):
        """Report the deduplicated caller count consistently across aliases.

        For ``func_gamma`` the distinct caller names, ``unique_caller_count`` and ``count`` all agree, so the aliases of
        the same number cannot drift apart.
        """
        data = query("fn-rdeps", "gamma::func_gamma")
        distinct_callers = {e["caller"] for e in data["called_by"]}
        assert data["unique_caller_count"] == len(distinct_callers)
        assert data["unique_caller_count"] == data["count"]

    def test_fn_central_includes_func_gamma(self, query):
        """func_gamma called by multiple functions → appears in fn-central.

        ``func_gamma`` is called by two functions, the most in the fixture project, so it must show up in the top-10
        ``fn-central`` ranking.
        """
        data = query("fn-central", "--top", "10")
        names = [e["qname"] for e in data.get("fn_central", [])]
        assert any("func_gamma" in n for n in names)

    def test_fn_blast(self, query):
        """fn-blast gamma::func_gamma surfaces callers at depth >= 1.

        ``fn-blast`` on ``func_gamma`` walks the reverse call graph and must list at least one caller, including
        ``func_beta``.
        """
        data = query("fn-blast", "gamma::func_gamma")
        blast = data.get("blast_radius", [])
        assert len(blast) >= 1
        callers = {e["caller"] for e in blast}
        assert any("func_beta" in t for t in callers)


#: Project exercising constructor edges, qname normalization and instance-dispatch gaps.
#: ``convert`` is defined twice (ambiguous bare name); ``unique_helper`` once (unique bare name).
#: ``Meter.ratio`` is a property only ever read as ``meter.ratio``, never called.
#: ``Bag.__len__``/``Bag.__eq__`` are reached only implicitly, through ``len(left)`` and ``left == right``.
#: ``TestMeter.test_scale_is_kept`` and the unittest-style ``MeterChecks.test_ratio_is_read`` are reached only by the
#: test runner; ``TestMeter.meter_scale`` is a never-called helper in that test module, and ``Link.test_connection`` a
#: production method that merely carries a test-like name.
_RESOLUTION_PROJECT_FILES = {
    "shapes/__init__.py": "",
    "shapes/bag.py": (
        "class Bag:\n"
        "    def __len__(self):\n"
        "        return 0\n"
        "\n"
        "    def __eq__(self, other):\n"
        "        return True\n"
        "\n"
        "\n"
        "def size_and_match(left, right):\n"
        "    return len(left), left == right\n"
    ),
    "shapes/core.py": (
        "class Meter:\n"
        "    def __init__(self, scale):\n"
        "        self.scale = scale\n"
        "\n"
        "    def update(self, value):\n"
        "        return value * self.scale\n"
        "\n"
        "    @property\n"
        "    def ratio(self):\n"
        "        return self.scale\n"
        "\n"
        "\n"
        "class Wrapper:\n"
        "    def __init__(self):\n"
        "        self.meter = Meter(1)\n"
        "\n"
        "    def run(self):\n"
        "        return self.meter\n"
        "\n"
        "\n"
        "def convert(value):\n"
        "    return value\n"
        "\n"
        "\n"
        "def build():\n"
        "    return Meter(2)\n"
        "\n"
        "\n"
        "def make_wrapper():\n"
        "    return Wrapper()\n"
    ),
    "shapes/other.py": "def convert(value):\n    return str(value)\n",
    "app.py": (
        "from shapes.core import Meter, convert\n"
        "\n"
        "\n"
        "def run():\n"
        "    meter = Meter(3)\n"
        "    meter.update(1)\n"
        "    return convert(meter), meter.ratio\n"
        "\n"
        "\n"
        "def unique_helper():\n"
        "    return run()\n"
    ),
    "tests/test_app.py": (
        "from app import unique_helper\n"
        "\n"
        "\n"
        "class Probe:\n"
        "    def check(self):\n"
        "        return True\n"
        "\n"
        "\n"
        "def test_unique_helper():\n"
        "    assert unique_helper() is not None\n"
        "\n"
        "\n"
        "def test_probe():\n"
        "    assert Probe.check(Probe())\n"
        "\n"
        "\n"
        "class TestMeter:\n"
        "    def test_scale_is_kept(self):\n"
        "        assert True\n"
        "\n"
        "    def meter_scale(self):\n"
        "        return 1\n"
        "\n"
        "\n"
        "class MeterChecks:\n"
        "    def test_ratio_is_read(self):\n"
        "        assert True\n"
    ),
    "shapes/link.py": "class Link:\n    def test_connection(self):\n        return True\n",
    # A module whose last segment equals the unique function ``build`` in shapes.core.
    "tools/build.py": "def run_build():\n    return 1\n",
}


@pytest.fixture(name="resolution_project", scope="module")
def _resolution_project(tmp_path_factory, scan_index) -> tuple[Path, Path]:
    """Scan the constructor/normalization project once; return ``(root, index_path)``."""
    root = tmp_path_factory.mktemp("resolution")
    for rel, content in _RESOLUTION_PROJECT_FILES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(content)
    index_path = _build_index(scan_index, root)
    return root, index_path


@pytest.fixture(name="resolve_query", scope="module")
def _resolve_query(resolution_project, scan_query):
    """Return a callable running scan-query on the resolution project as ``(exit_code, payload)``."""
    root, index_path = resolution_project

    def _run(*args: str) -> tuple[int, dict]:
        """Run one query subprocess and decode its JSON stdout, success or error."""
        result = _run_query(scan_query, index_path, *args, cwd=root)
        return result.returncode, json.loads(result.stdout)

    return _run


class TestConstructorCallers:
    """Callers of ``Class()`` count as callers of ``Class.__init__``.

    The scanner records ``Meter(3)`` as an edge to ``shapes.core::Meter``, never to ``Meter.__init__``; without the
    union every constructor query reported zero callers while claiming a complete answer.
    """

    def test_fn_rdeps_on_init_reports_class_constructor_callers(self, resolve_query):
        """Fn-rdeps on ``Meter.__init__`` lists every function that instantiates ``Meter``.

        Three call sites construct ``Meter``: ``build``, ``Wrapper.__init__`` and ``app.run``. All of them must appear,
        and the payload flags that constructor edges were merged in.
        """
        _, data = resolve_query("fn-rdeps", "shapes.core::Meter.__init__")
        assert sorted(e["caller"] for e in data["called_by"]) == [
            "app::run",
            "shapes.core::Wrapper.__init__",
            "shapes.core::build",
        ]
        assert data["count"] == 3
        assert data["constructor_callers_merged"] is True

    def test_fn_rdeps_on_init_excludes_test_callers_after_merge(self, resolve_query):
        """``--exclude-tests`` still applies to callers reached through the class edge.

        The merged constructor callers still honour ``--exclude-tests``: the count stays at the three production callers
        and no returned path starts with ``tests/``.
        """
        _, data = resolve_query("fn-rdeps", "shapes.core::Meter.__init__", "--exclude-tests")
        assert data["count"] == 3
        assert all(not e["path"].startswith("tests/") for e in data["called_by"])

    def test_fn_blast_on_init_walks_past_the_constructor(self, resolve_query):
        """Fn-blast on ``Meter.__init__`` reaches ``unique_helper`` through ``run`` at depth 2.

        ``app.run`` constructs ``Meter`` (depth 1) and ``unique_helper`` calls ``run`` (depth 2), so the blast radius
        must continue past the constructor edge and flag that constructors were merged.
        """
        _, data = resolve_query("fn-blast", "shapes.core::Meter.__init__")
        depths = {e["caller"]: e["depth"] for e in data["blast_radius"]}
        assert depths["app::run"] == 1
        assert depths["app::unique_helper"] == 2
        assert data["constructor_callers_merged"] is True

    def test_fn_blast_crosses_an_intermediate_constructor(self, resolve_query):
        """A caller reached through ``Wrapper.__init__`` continues to whoever constructs ``Wrapper``.

        ``Meter`` is called from ``Wrapper.__init__``; the next hop must follow ``Wrapper()`` edges to ``make_wrapper``,
        otherwise the transitive walk stops at every constructor.
        """
        _, data = resolve_query("fn-blast", "shapes.core::Meter")
        depths = {e["caller"]: e["depth"] for e in data["blast_radius"]}
        assert depths["shapes.core::make_wrapper"] == 2

    def test_fn_blast_never_lists_the_class_itself(self, resolve_query):
        """The class seed is a walk start, not a caller row.

        The class qname is only used to start the reverse walk through ``Meter.__init__``; listing
        ``shapes.core::Meter`` as its own caller would be noise.
        """
        _, data = resolve_query("fn-blast", "shapes.core::Meter.__init__")
        assert "shapes.core::Meter" not in {e["caller"] for e in data["blast_radius"]}

    def test_test_impact_on_init_reaches_tests_through_the_class(self, resolve_query):
        """Test-impact on ``Meter.__init__`` selects the test that transitively constructs ``Meter``.

        ``test_unique_helper`` reaches ``Meter(3)`` through ``unique_helper`` and ``run``, so ``test-impact`` on the
        constructor must select ``tests/test_app.py``.
        """
        _, data = resolve_query("test-impact", "shapes.core::Meter.__init__")
        assert data["test_files"] == ["tests/test_app.py"]
        assert data["constructor_callers_merged"] is True

    @pytest.mark.parametrize(
        ("command", "target"),
        [
            pytest.param("fn-rdeps", "shapes.core::convert", id="fn-rdeps-plain-function"),
            pytest.param("fn-blast", "shapes.core::convert", id="fn-blast-plain-function"),
            pytest.param("test-impact", "app::run", id="test-impact-plain-function"),
        ],
    )
    def test_plain_function_payload_carries_no_new_keys(self, resolve_query, command, target):
        """Optional resolution keys stay absent when no merge, rewrite, or empty-method hint applied.

        Frozen parity contracts compare the payload key set byte-for-byte for ordinary functions.
        """
        _, data = resolve_query(command, target)
        assert not {"constructor_callers_merged", "normalized_from", "hint"} & set(data)
        assert "inherited-constructors" not in data["index"]["not_covered"]

    @pytest.mark.parametrize("command", ["fn-rdeps", "fn-blast", "test-impact"])
    def test_constructor_merge_names_the_inherited_constructor_gap(self, resolve_query, command):
        """A merged constructor answer says what it cannot see instead of reading as complete.

        The index records no class bases and no ``super().__init__()`` edge, so ``Sub()`` running ``Meter.__init__`` is
        never followed; ``not_covered`` names that limit beside the call-graph defaults.
        """
        _, data = resolve_query(command, "shapes.core::Meter.__init__")
        assert data["index"]["not_covered"][-1] == "inherited-constructors"


class TestQnameNormalization:
    """``fn-*`` targets accept dotted and unique bare names besides ``module::symbol``."""

    @pytest.mark.parametrize(
        ("command", "target", "expected_qname"),
        [
            pytest.param("fn-rdeps", "shapes.core.convert", "shapes.core::convert", id="fn-rdeps-dotted"),
            pytest.param("fn-rdeps", "shapes.core.Meter.update", "shapes.core::Meter.update", id="fn-rdeps-method"),
            pytest.param("fn-rdeps", "unique_helper", "app::unique_helper", id="fn-rdeps-bare-unique"),
            pytest.param("fn-rdeps", "Wrapper.__init__", "shapes.core::Wrapper.__init__", id="fn-rdeps-qualified"),
            pytest.param("fn-rdeps", "run", "app::run", id="fn-rdeps-bare-function-outranks-method"),
            pytest.param("fn-deps", "app.run", "app::run", id="fn-deps-dotted"),
            pytest.param("fn-blast", "app.run", "app::run", id="fn-blast-dotted"),
            pytest.param("test-impact", "app.run", "app::run", id="test-impact-dotted"),
            pytest.param("test-impact", "unique_helper", "app::unique_helper", id="test-impact-bare-unique"),
        ],
    )
    def test_resolves_to_module_symbol_and_reports_the_input(self, resolve_query, command, target, expected_qname):
        """A non-``::`` target resolves to one symbol; ``normalized_from`` records the caller's spelling.

        Dotted, method, bare-unique and qualified spellings of a target all resolve to their ``module::symbol``
        qname, and ``normalized_from`` echoes what the caller typed.
        """
        code, data = resolve_query(command, target)
        assert code == 0, data
        assert data["qname"] == expected_qname
        assert data["normalized_from"] == target

    def test_dotted_target_returns_the_same_callers_as_module_symbol(self, resolve_query):
        """The dotted spelling answers exactly what the ``module::symbol`` spelling answers.

        ``fn-rdeps`` runs with ``shapes.core.convert`` and ``shapes.core::convert``; both return the one caller
        ``app::run``, so normalization changes the spelling, not the answer.
        """
        _, dotted = resolve_query("fn-rdeps", "shapes.core.convert")
        _, canonical = resolve_query("fn-rdeps", "shapes.core::convert")
        assert (
            dotted["called_by"] == canonical["called_by"] == [{"caller": "app::run", "module": "app", "path": "app.py"}]
        )

    @pytest.mark.parametrize(
        ("target", "expected_candidates"),
        [
            pytest.param("convert", ["shapes.core::convert", "shapes.other::convert"], id="function-in-two-modules"),
            pytest.param(
                "__init__",
                ["shapes.core::Meter.__init__", "shapes.core::Wrapper.__init__"],
                id="method-leaf-in-two-classes",
            ),
            pytest.param("build", ["shapes.core::build", "tools.build"], id="function-also-a-module-suffix"),
        ],
    )
    @pytest.mark.parametrize("command", ["fn-rdeps", "test-impact"])
    def test_ambiguous_bare_name_lists_every_candidate(self, resolve_query, command, target, expected_candidates):
        """A bare name that could mean something else fails with every candidate instead of guessing.

        Covers several symbols, a method leaf in two classes, and a function name that is also the last segment of an
        indexed module: before, ``test-impact cli`` silently answered for an unrelated ``tools.cli::cli`` function. The
        list lets the caller re-run with one target instead of a ``find-symbol`` call.
        """
        code, data = resolve_query(command, target)
        assert code == 1
        assert data["candidates"] == expected_candidates
        assert data["candidate_count"] == len(expected_candidates)
        assert "ambiguous" in data["error"]

    @pytest.mark.parametrize("command", ["fn-rdeps", "test-impact"])
    def test_lone_method_leaf_names_the_qname_instead_of_ambiguity(self, resolve_query, command):
        """A bare method name matching one method is never auto-resolved, and never called "ambiguous: 1" either.

        Nobody named the class, so even a single method leaf is a guess the resolver refuses; reporting it as a choice
        among one told the caller to pick from a list of one instead of naming the qname to re-run with.
        """
        code, data = resolve_query(command, "update")
        assert (code, data["candidates"], data["candidate_count"]) == (1, ["shapes.core::Meter.update"], 1)
        assert data["error"].startswith(
            "Symbol 'update' is a bare method name matching only 'shapes.core::Meter.update'"
        )
        assert "ambiguous" not in data["error"]

    @pytest.mark.parametrize("command", ["fn-rdeps", "test-impact"])
    def test_lone_module_suffix_names_the_module_instead_of_ambiguity(self, resolve_query, command):
        """A bare name matching only one module's last segment names that module rather than "1 match" ambiguity.

        ``other`` is no symbol, only the tail of ``shapes.other``: calling a single module candidate ambiguous told the
        caller to choose from a list of one, while the module redirect says which command answers it.
        """
        code, data = resolve_query(command, "other")
        assert (code, data["candidates"], data["candidate_count"]) == (1, ["shapes.other"], 1)
        assert data["error"].startswith("Symbol 'other' is not an indexed symbol: it names module 'shapes.other'")
        assert "ambiguous" not in data["error"]

    @pytest.mark.parametrize(
        "target",
        [
            pytest.param("does_not_exist", id="bare"),
            pytest.param("shapes.core.does_not_exist", id="dotted"),
        ],
    )
    def test_unknown_target_keeps_the_not_found_error(self, resolve_query, target):
        """An unresolvable spelling still reports the historic not-found message.

        A bare and a dotted name that match nothing keep the historic ``not found`` message that points at ``find-
        symbol``; scripts match on that text.
        """
        code, data = resolve_query("fn-rdeps", target)
        assert code == 1
        assert data["error"] == f"Symbol '{target}' not found. Use 'find-symbol <pattern>' to search."

    def test_bare_module_name_keeps_the_rdeps_redirect(self, resolve_query):
        """A module name passed to fn-rdeps is redirected to ``rdeps``, never searched as a symbol.

        ``shapes.core`` is a module, so ``fn-rdeps`` exits 1 with a message saying so and pointing at ``rdeps`` instead
        of searching for a symbol.
        """
        code, data = resolve_query("fn-rdeps", "shapes.core")
        assert code == 1
        assert "is a module, not a function qname" in data["error"]

    def test_test_impact_module_target_stays_module_mode(self, resolve_query):
        """A dotted target naming a module keeps the import-graph mode and gains no ``normalized_from``.

        ``test-impact app`` names a module, so it answers in import-graph mode with the test file and does not add the
        ``normalized_from`` key reserved for symbol rewrites.
        """
        _, data = resolve_query("test-impact", "app")
        assert data["test_files"] == ["tests/test_app.py"]
        assert "normalized_from" not in data
        assert data["index"]["method"] == "import-graph"

    @pytest.mark.parametrize(
        ("command", "target", "expected_code"),
        [
            pytest.param("fn-rdeps", "does_not_exist", 1, id="fn-rdeps-not-found"),
            pytest.param("fn-blast", "shapes.core.does_not_exist", 1, id="fn-blast-dotted-not-found"),
            pytest.param("fn-rdeps", "shapes.core", 1, id="fn-rdeps-module-redirect"),
            pytest.param("fn-rdeps", "convert", 1, id="fn-rdeps-candidates"),
            pytest.param("test-impact", "shapes.nothere", 1, id="test-impact-module-not-found"),
            pytest.param("rdeps", "shapes.nothere", 3, id="rdeps-module-not-indexed"),
        ],
    )
    def test_target_rejection_names_the_rejected_target(self, resolve_query, command, target, expected_code):
        """Every target-resolution failure carries ``rejected_target`` beside its unchanged exit code.

        Exit 1 also covers an invalid index or a disabled feature, so a consumer that must keep "this target does not
        resolve" apart from "this provider cannot answer" — retry with a candidate, or stop querying — reads this key
        instead of the message text. A structured-context consumer recorded every such rejection as an unusable provider
        before the key existed.
        """
        code, data = resolve_query(command, target)
        assert (code, data["rejected_target"]) == (expected_code, target)


def _hint_search(hint: str) -> re.Pattern[str]:
    """Return the regular expression a zero-caller hint tells the reader to search with."""
    match = re.search(r'grep -rnE "(?P<pattern>[^"]+)"', hint)
    assert match is not None, hint
    return re.compile(match["pattern"])


class TestEmptyMethodHint:
    """A method with no static callers explains why instead of reading as dead code."""

    @pytest.mark.parametrize(
        ("command", "zero_key"),
        [
            pytest.param("fn-rdeps", "count", id="fn-rdeps"),
            pytest.param("fn-blast", "total_callers", id="fn-blast"),
            pytest.param("test-impact", "total", id="test-impact"),
        ],
    )
    def test_method_without_static_callers_gets_dispatch_hint(self, resolve_query, command, zero_key):
        """Every reverse-call answer for an instance-called method carries the hint, not only ``fn-rdeps``.

        ``test_unique_helper`` reaches ``meter.update(1)`` through ``run``, but the scanner cannot type ``meter``, so
        each command answers zero with ``query_complete`` true. A ``test-impact`` follow-up without the hint read as "no
        test covers this method" and could skip the one test that does.
        """
        _, data = resolve_query(command, "shapes.core::Meter.update")
        assert (data[zero_key], _hint_search(data["hint"]).pattern) == (0, r"\.update\b")
        assert data["hint"].startswith('Find references with grep -rnE "\\.update\\b". 0 static callers for method ')

    @pytest.mark.parametrize(
        ("target", "action"),
        [
            pytest.param("shapes.core::Meter.update", 'Find references with grep -rnE "\\.update\\b".', id="method"),
            pytest.param(
                "shapes.bag::Bag.__len__", "Never delete protocol method Bag.__len__ on zero callers.", id="dunder"
            ),
        ],
    )
    def test_hint_opens_with_its_action(self, resolve_query, target, action):
        """The search to run, or the instruction to keep a protocol method, comes first, the explanation after it.

        A consumer that bounds strings cuts the tail: the Codex structural context keeps 300 characters, and with the
        action last it lost the search of every method named longer than ``Report.render`` and the do-not-delete clause
        of every protocol method.
        """
        _, data = resolve_query("fn-rdeps", target)
        assert data["hint"][: len(action)] == action

    def test_hint_search_finds_a_property_read_and_skips_the_definition(self, resolve_query, resolution_project):
        """The hint's search is reference-shaped: it finds ``meter.ratio`` and not the ``def ratio(`` line.

        A property is read without a call, so the former ``.ratio(`` search matched only the definition and reported a
        property in active use as unreferenced.
        """
        root, _ = resolution_project
        _, data = resolve_query("fn-rdeps", "shapes.core::Meter.ratio")
        search = _hint_search(data["hint"])
        lines = [
            f"{rel}: {line.strip()}"
            for rel in ("app.py", "shapes/core.py")
            for line in (root / rel).read_text().splitlines()
            if search.search(line)
        ]
        assert (data["count"], lines) == (0, ["app.py: return convert(meter), meter.ratio"])

    @pytest.mark.parametrize(
        ("command", "target"),
        [
            pytest.param("test-impact", "shapes.core::Wrapper.__init__", id="test-impact-callers-but-no-tests"),
            pytest.param("fn-blast", "shapes.core::Meter.__init__", id="fn-blast-constructor-with-callers"),
            pytest.param("test-impact", "shapes.other", id="test-impact-module-mode"),
        ],
    )
    def test_reverse_walk_with_callers_or_module_mode_gets_no_hint(self, resolve_query, command, target):
        """Zero tests is not zero callers, and a module target is not a method, so neither gets the hint.

        ``Wrapper()`` is called from ``make_wrapper``, which no test reaches: ``test-impact`` reports no test file, yet
        "0 static callers" would be false. Module-mode ``test-impact`` walks imports, never call edges.
        """
        _, data = resolve_query(command, target)
        assert "hint" not in data

    @pytest.mark.parametrize("method", ["__len__", "__eq__"])
    def test_protocol_method_hint_names_implicit_calls_and_no_search(self, resolve_query, resolution_project, method):
        """A dunder's hint says Python calls it implicitly and offers no reference search.

        ``len(left)`` and ``left == right`` reach ``Bag.__len__``/``Bag.__eq__`` with no ``.__len__`` text anywhere, so
        the former ``\\.__len__\\b`` search came back empty — the very result that licensed a manual delete.
        """
        root, _ = resolution_project
        _, data = resolve_query("fn-rdeps", f"shapes.bag::Bag.{method}")
        uses = [line for line in (root / "shapes" / "bag.py").read_text().splitlines() if f".{method}" in line]
        assert (data["count"], uses) == (0, [])
        assert f"Python calls {method} implicitly" in data["hint"]
        assert "grep" not in data["hint"]

    def test_empty_method_hint_keeps_completeness_semantics(self, resolve_query):
        """The hint is advisory: ``query_complete`` is reported exactly as for any other query.

        A hinted ``Meter.update`` answer and an unhinted ``convert`` answer report the same ``query_complete``, so
        adding advice neither weakens nor inflates the completeness claim.
        """
        _, with_hint = resolve_query("fn-rdeps", "shapes.core::Meter.update")
        _, without_hint = resolve_query("fn-rdeps", "shapes.core::convert")
        assert with_hint["index"]["query_complete"] == without_hint["index"]["query_complete"]

    @pytest.mark.parametrize(
        ("command", "target", "deferred"),
        [
            pytest.param("fn-rdeps", "shapes.core::Meter.update", True, id="fn-rdeps-hinted"),
            pytest.param("fn-blast", "shapes.core::Meter.update", True, id="fn-blast-hinted"),
            pytest.param("test-impact", "shapes.core::Meter.update", True, id="test-impact-hinted"),
            pytest.param("fn-rdeps", "shapes.core::convert", False, id="fn-rdeps-plain"),
        ],
    )
    def test_complete_note_never_contradicts_the_hint(self, resolve_query, command, target, deferred):
        """A complete hinted answer defers to its hint; an unhinted one keeps "verification is not needed".

        The note used to say grep was not needed right beside a hint telling the reader to grep.
        """
        _, data = resolve_query(command, target)
        note = data["index"]["note"]
        assert (data["index"]["query_complete"], "not needed" in note, "follow the answer's hint" in note) == (
            True,
            not deferred,
            deferred,
        )

    @pytest.mark.parametrize(
        ("command", "zero_key"),
        [
            pytest.param("fn-rdeps", "count", id="fn-rdeps"),
            pytest.param("fn-blast", "total_callers", id="fn-blast"),
            pytest.param("test-impact", "total", id="test-impact"),
        ],
    )
    @pytest.mark.parametrize(
        "target",
        [
            pytest.param("tests.test_app::TestMeter.test_scale_is_kept", id="pytest-class"),
            pytest.param("tests.test_app::MeterChecks.test_ratio_is_read", id="unittest-style-class"),
        ],
    )
    def test_collected_test_method_gets_no_hint(self, resolve_query, command, zero_key, target):
        """A test method the runner collects has no static caller by design, so it gets no dispatch hint.

        The hint's premise, "zero static callers is unresolved, never unused", is false for a test: no instance call is
        missing, and its search finds nothing. Every changed test method in a review's diff carried it, and its blind
        spots turned a test-only change into a ``degraded`` structural answer.
        """
        _, data = resolve_query(command, target)
        assert (data[zero_key], "hint" in data) == (0, False)

    @pytest.mark.parametrize(
        ("target", "search"),
        [
            pytest.param("tests.test_app::TestMeter.meter_scale", r"\.meter_scale\b", id="helper-in-test-module"),
            pytest.param(
                "shapes.link::Link.test_connection", r"\.test_connection\b", id="test-named-production-method"
            ),
        ],
    )
    def test_uncollected_method_keeps_the_hint(self, resolve_query, target, search):
        """Only a ``test*`` method in a test module is exempt: a test helper or a test-named production method keeps it.

        Both are reached through an instance like any other method, so zero static callers there is still unresolved.
        """
        _, data = resolve_query("fn-rdeps", target)
        assert (data["count"], _hint_search(data["hint"]).pattern) == (0, search)

    def test_function_without_callers_gets_no_method_hint(self, resolve_query):
        """A module-level function with zero callers is not a method and gets no dispatch hint.

        ``make_wrapper`` is a plain function nobody calls: the count is 0 and no dispatch hint is attached, because the
        instance-call blind spot only applies to methods.
        """
        _, data = resolve_query("fn-rdeps", "shapes.core::make_wrapper")
        assert data["count"] == 0
        assert "hint" not in data

    def test_method_called_only_from_tests_gets_no_dispatch_hint(self, resolve_query):
        """Excluding test callers hides them; it does not make "0 static callers" true, so no hint is given.

        ``Probe.check`` is called only from a test: with tests included the count is 1, and with ``--exclude-tests`` it
        is 0 without a hint, since that zero only reflects the filter.
        """
        _, with_tests = resolve_query("fn-rdeps", "Probe.check")
        _, without_tests = resolve_query("fn-rdeps", "Probe.check", "--exclude-tests")
        assert with_tests["count"] == 1
        assert without_tests["count"] == 0
        assert "hint" not in without_tests


class TestRdepsNewFields:
    """Rdeps output includes dynamic_imported_by and config_refs fields."""

    @pytest.mark.parametrize("field", ["dynamic_imported_by", "config_refs"])
    def test_rdeps_carries_field_as_list(self, query, field):
        """Rdeps result always carries the field, even when empty, and it is a list.

        ``dynamic_imported_by`` may be empty for modules with no dynamic callers; ``config_refs`` may be empty when no
        config files reference the module.
        """
        data = query("rdeps", "gamma")
        assert field in data
        assert isinstance(data[field], list)

    def test_rdeps_populates_dynamic_imported_by_and_config_refs(self, tmp_path, scan_index, scan_query):
        """Dynamic import literals and root config references are exposed for rdeps.

        ``pkg.gamma`` is loaded by ``importlib.import_module`` and ``__import__`` string literals and named in a
        ``pyproject.toml`` plugin list; none is a static import, so each must surface through its own field.
        """
        root = tmp_path / "rdeps_fields"
        (root / "pkg").mkdir(parents=True)
        _write_files(
            root,
            {
                "pkg/__init__.py": "",
                "pkg/gamma.py": "VALUE = 1\n",
                "dyn_importlib.py": "import importlib\n\ndef load():\n    return importlib.import_module('pkg.gamma')\n",
                "dyn_dunder.py": "def load():\n    return __import__('pkg.gamma')\n",
                "pyproject.toml": "[tool.codemap]\nplugins = ['pkg.gamma']\n",
            },
        )
        index_path = _build_index(scan_index, root)

        query_result = _run_query(scan_query, index_path, "rdeps", "pkg.gamma", cwd=root)

        assert query_result.returncode == 0, query_result.stderr
        data = json.loads(query_result.stdout)
        assert {entry["importer"] for entry in data["dynamic_imported_by"]} == {"dyn_dunder", "dyn_importlib"}
        assert {entry["literal"] for entry in data["dynamic_imported_by"]} == {"pkg.gamma"}
        assert data["config_refs"] == [{"file": "pyproject.toml", "line": 2, "context": "plugins = ['pkg.gamma']"}]


class TestRequireFeature:
    """_require_feature: pass/fail/edge cases for per-feature version guard."""

    @pytest.fixture(name="index_v3")
    def _index_v3(self) -> dict:
        """Minimal index dict at scan_version=3 — the call-graph floor (CALL_GRAPH_MIN_VER)."""
        return {"scan_version": 3, "modules": []}

    def test_passes_when_version_meets_minimum(self, index_v3) -> None:
        """No raise/exit when scan_version >= min_ver.

        An index at scan_version 3 asked for a feature whose minimum is 3 returns normally; the guard fires only when
        the index is older.
        """
        _require_feature(index_v3, 3, "test_feature")

    @pytest.mark.parametrize(
        ("index", "min_ver", "feature"),
        [
            pytest.param({"scan_version": 3, "modules": []}, 4, "mock_patches", id="v3-index-below-v4-minimum"),
            pytest.param({}, 1, "test_feature", id="missing-version-key-treated-as-zero"),
        ],
    )
    def test_exits_when_version_unmet(self, index, min_ver, feature) -> None:
        """An index below the minimum version fails via SystemExit; a missing scan_version is treated as 0.

        A v3 index fails a v4 minimum with ``SystemExit``; an index with no ``scan_version`` key reads as version 0, so
        any minimum is unmet, instead of silently passing.
        """
        with pytest.raises(SystemExit):
            _require_feature(index, min_ver, feature)

    def test_error_message_names_feature(self, index_v3, capsys) -> None:
        """Error output must include the feature name so the user knows what failed.

        A v3 index asked for a v99 feature exits, and the message printed names ``my_feature``, so the user knows which
        command needs a rescan.
        """
        with pytest.raises(SystemExit):
            _require_feature(index_v3, 99, "my_feature")
        captured = capsys.readouterr()
        assert "my_feature" in captured.err or "my_feature" in captured.out

    def test_handles_string_version(self) -> None:
        """scan_version stored as a string (legacy indexes) must still parse cleanly.

        Older indexes stored ``scan_version`` as the text ``"3"``; it must compare as a number so those indexes are not
        wrongly rejected.
        """
        index = {"scan_version": "3"}
        _require_feature(index, 3, "test_feature")


class TestHasCallGraph:
    """_has_call_graph: gates fn-* commands on the fixed v3 floor, not live SCAN_VERSION.

    Regression guard for the bug where the check compared against the live
    ``SCAN_VERSION`` (11), so every pre-current index (v3–v10) was falsely rejected
    for fn-deps/fn-rdeps/fn-central/fn-blast. The floor must stay pinned to
    ``CALL_GRAPH_MIN_VER`` (3) so future ``SCAN_VERSION`` bumps never re-break it.
    """

    @pytest.mark.parametrize(
        "index",
        [
            pytest.param({"scan_version": 3}, id="v3-floor"),
            pytest.param({"scan_version": 10}, id="v10"),
            pytest.param({"scan_version": 11}, id="v11-current"),
            pytest.param({"scan_version": 99}, id="v99-future"),
            pytest.param({"scan_version": "3"}, id="legacy-string-version"),
        ],
    )
    def test_accepts_versions_at_or_above_floor(self, index: dict) -> None:
        """Any index at or above the v3 call-graph floor carries call edges, so it is accepted.

        Legacy indexes may serialise ``scan_version`` as a string; it is parsed rather than rejected.
        """
        assert _has_call_graph(index) is True

    @pytest.mark.parametrize(
        "index",
        [
            pytest.param({"scan_version": 2}, id="v2"),
            pytest.param({"scan_version": 1}, id="v1"),
            pytest.param({"scan_version": 0}, id="v0"),
            pytest.param({}, id="missing-version-key-defaults-to-zero"),
        ],
    )
    def test_rejects_versions_below_floor(self, index: dict) -> None:
        """Indexes below v3, or with no ``scan_version`` key at all, predate call edges and are rejected.

        Parametrized over v2, v1, v0 and an index without the key (read as 0); each predates the call edges that
        ``fn-*`` commands read.
        """
        assert _has_call_graph(index) is False

    def test_not_coupled_to_live_scan_version(self) -> None:
        """Guard against re-coupling: the call-graph floor constant and a v3 index both pass.

        If the check ever regresses to ``>= SCAN_VERSION``, the v3 case flips to False while the current-version case
        stays True — this asserts both hold.
        """
        assert _has_call_graph({"scan_version": _scan_query_mod.CALL_GRAPH_MIN_VER}) is True
        assert _has_call_graph({"scan_version": 3}) is True


class TestMockRdeps:
    """Mock-rdeps subcommand (v4.1): test files that patch a symbol via patch()."""

    def _scan_and_query(
        self, tmp_path: Path, scan_index: Path, scan_query: Path, test_source: str, query: list[str]
    ) -> tuple[int, dict, str]:
        """Write ``tests/test_target.py`` under *tmp_path*, scan it, run *query*; return ``(rc, json, stderr)``.

        The file lives in a ``tests/`` subdir because the scan-index test-path regex needs the ``tests/`` segment (or
        ``/test_<name>.py`` after a slash); a bare ``test_x.py`` at the project root is not flagged as a test module.
        """
        (tmp_path / "tests").mkdir(exist_ok=True)
        (tmp_path / "tests" / "test_target.py").write_text(test_source)
        return _scan_and_query(tmp_path, scan_index, scan_query, query)

    @pytest.mark.parametrize(
        ("src", "expected_form"),
        [
            pytest.param(
                "from unittest.mock import patch\n\n@patch('mypackage.x.fn')\ndef test_a(mock_fn):\n    pass\n",
                "decorator",
                id="decorator-form",
            ),
            pytest.param(
                "from unittest.mock import patch\n\ndef test_b():\n    with patch('mypackage.x.fn'):\n        pass\n",
                "call",
                id="call-form",
            ),
            pytest.param(
                "def test_c(mocker):\n    mocker.patch('mypackage.x.fn')\n",
                "mocker",
                id="mocker-form",
            ),
        ],
    )
    def test_patch_reference_form_indexed(self, tmp_path, scan_index, scan_query, src, expected_form):
        """Report decorator, call-in-body, and pytest-mock patch references with their specific reference form.

        Each patch spelling lands in ``mock-rdeps`` under its own ``form`` value, so tools can tell a decorator from a
        ``with patch(...)`` call and from ``mocker.patch``; every form also counts as at least one caller.
        """
        rc, data, _ = self._scan_and_query(tmp_path, scan_index, scan_query, src, ["mock-rdeps", "mypackage.x::fn"])
        assert rc == 0
        forms = {c["form"] for c in data["callers"]}
        assert expected_form in forms
        assert data["count"] >= 1

    def test_class_method_key_normalization(self, tmp_path, scan_index, scan_query):
        """Normalize to ``mypackage.x::MyClass.method``.

        A test patches ``mypackage.x.MyClass.method``; querying ``mypackage.x::MyClass.method`` finds exactly that
        one patch and reports the symbol and module split correctly.
        """
        src = (
            "from unittest.mock import patch\n"
            "\n"
            "@patch('mypackage.x.MyClass.method')\n"
            "def test_d(mock_method):\n"
            "    pass\n"
        )
        rc, data, _ = self._scan_and_query(
            tmp_path, scan_index, scan_query, src, ["mock-rdeps", "mypackage.x::MyClass.method"]
        )
        assert rc == 0, data
        assert data["count"] == 1
        assert data["symbol"] == "MyClass.method"
        assert data["module"] == "mypackage.x"

    @pytest.mark.parametrize(
        ("src", "query_args"),
        [
            pytest.param(
                "import pytest\n\n@pytest.fixture\ndef thing():\n    return 1\n",
                ["mock-rdeps", "pytest::fixture"],
                id="non-patch-decorator",
            ),
            pytest.param(
                "from unittest.mock import patch\n\n@patch('nodots')\ndef test_e(_):\n    pass\n",
                ["mock-rdeps", "nodots"],
                id="malformed-patch-string",
            ),
        ],
    )
    def test_non_patch_reference_not_captured(self, tmp_path, scan_index, scan_query, src, query_args):
        """Non-patch decorators (e.g. ``@pytest.fixture``) are not added to mock_patches.

        A malformed patch string must not crash: it is skipped (with a stderr warning) and yields no mock reference.
        """
        rc, data, _ = self._scan_and_query(tmp_path, scan_index, scan_query, src, query_args)
        assert rc == 0
        assert data["count"] == 0

    def test_bare_module_query_lists_all_targets(self, tmp_path, scan_index, scan_query):
        """Bare module query returns every mocked target in that module.

        One test stacks two ``@patch`` decorators on ``fn_a`` and ``fn_b`` in ``mypackage.x``; querying the bare module
        reports both mocked targets.
        """
        src = (
            "from unittest.mock import patch\n"
            "\n"
            "@patch('mypackage.x.fn_a')\n"
            "@patch('mypackage.x.fn_b')\n"
            "def test_f(mb, ma):\n"
            "    pass\n"
        )
        rc, data, _ = self._scan_and_query(tmp_path, scan_index, scan_query, src, ["mock-rdeps", "mypackage.x"])
        assert rc == 0, data
        targets = {c["target"] for c in data["callers"]}
        assert "mypackage.x::fn_a" in targets
        assert "mypackage.x::fn_b" in targets


class TestFindIndex:
    """find_index: .cache/codemap/ preferred over .cache/scan/ (D2 fix)."""

    @pytest.mark.parametrize(
        ("present", "expected"),
        [
            pytest.param(("codemap",), "codemap", id="codemap-only"),
            pytest.param(("scan",), "scan", id="legacy-scan-fallback"),
            pytest.param(("codemap", "scan"), "codemap", id="codemap-preferred-over-scan"),
        ],
    )
    def test_prefers_codemap_cache_dir(self, tmp_path, monkeypatch, present, expected) -> None:
        """An index in ``.cache/codemap/`` wins; ``.cache/scan/`` is only the fallback when it is absent.

        ``.cache/codemap/`` is the current location and ``.cache/scan/`` the legacy one; the lookup runs with no git
        root so only the cache directory decides the result.
        """
        _touch_json(tmp_path, *[f".cache/{subdir}/{tmp_path.name}.json" for subdir in present])
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(_scan_query_mod.index_io, "_get_git_root_cached", lambda: None)

        result = _find_index()

        assert result == tmp_path / ".cache" / expected / f"{tmp_path.name}.json"

    @pytest.mark.parametrize(
        "existing",
        [
            pytest.param((".cache/codemap/{name}.json", "custom-index-dir/{name}.json"), id="override-wins-over-cache"),
            pytest.param((), id="override-returned-before-writer-publishes"),
        ],
    )
    def test_index_dir_override_resolves_to_flat_file(self, tmp_path, monkeypatch, existing) -> None:
        """``CODEMAP_INDEX_DIR`` resolves to the writer's flat ``<override>/<root_name>.json``.

        The override wins even when a ``.cache`` index exists, and is returned even before the writer publishes, so a
        missing-index error surfaces at the writer's path.
        """
        _touch_json(tmp_path, *[path.format(name=tmp_path.name) for path in existing])
        override = tmp_path / "custom-index-dir"
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(_scan_query_mod.index_io, "_get_git_root_cached", lambda: tmp_path)
        monkeypatch.setenv("CODEMAP_INDEX_DIR", str(override))

        result = _find_index()

        assert result == override / f"{tmp_path.name}.json"


class TestImportClassification:
    """Import classification into stdlib / third_party / internal groups (v4.3)."""

    @pytest.mark.parametrize(
        ("project_dir", "files", "import_name", "group"),
        [
            pytest.param(
                "stdlib_proj",
                {"consumer.py": "import os\n\ndef use_os():\n    return os.getcwd()\n"},
                "os",
                "stdlib",
                id="stdlib",
            ),
            pytest.param(
                "third_party_proj",
                {"consumer.py": "import numpy\n\ndef use_np():\n    return numpy.array([])\n"},
                "numpy",
                "third_party",
                id="third_party",
            ),
            pytest.param(
                "internal_proj",
                {
                    "lib_a.py": "def thing():\n    return 1\n",
                    "consumer.py": "import lib_a\n\ndef use():\n    return lib_a.thing()\n",
                },
                "lib_a",
                "internal",
                id="internal-sibling-module",
            ),
        ],
    )
    def test_import_classified_into_single_group(
        self, tmp_path, scan_index, scan_query, project_dir, files, import_name, group
    ):
        """Place an import in exactly one ``import-types`` group: stdlib, third_party, or internal.

        ``os`` is stdlib; ``numpy`` is not stdlib and not indexed, so it is third-party; an import of a sibling indexed
        module lands in the ``internal`` group.
        """
        root = tmp_path / project_dir
        root.mkdir()
        _write_files(root, files)
        rc, data, _ = _scan_and_query(root, scan_index, scan_query, ["import-types", "consumer"])
        assert rc == 0, data
        assert import_name in data[group]
        assert all(import_name not in data[other] for other in {"stdlib", "third_party", "internal"} - {group})

    def test_import_types_returns_all_three_groups(self, tmp_path, scan_index, scan_query):
        """Return stdlib, third_party, and internal in a single payload.

        ``consumer.py`` imports ``os``, ``numpy`` and the indexed sibling ``lib_b``; the single ``import-types`` payload
        sorts them into stdlib, third_party and internal.
        """
        root = tmp_path / "all_three"
        root.mkdir()
        (root / "lib_b.py").write_text("def b():\n    return 1\n")
        (root / "consumer.py").write_text("import os\nimport numpy\nimport lib_b\n\ndef use():\n    return lib_b.b()\n")
        rc, data, _ = _scan_and_query(root, scan_index, scan_query, ["import-types", "consumer"])
        assert rc == 0, data
        assert "os" in data["stdlib"]
        assert "numpy" in data["third_party"]
        assert "lib_b" in data["internal"]

    def test_deps_third_party_filter_restricts_output(self, tmp_path, scan_index, scan_query):
        """Return only the third-party slice of direct_imports.

        With the same three kinds of import, ``deps --third-party`` keeps only ``numpy`` and drops the stdlib and
        internal ones.
        """
        root = tmp_path / "filter_third"
        root.mkdir()
        (root / "lib_c.py").write_text("def c():\n    return 1\n")
        (root / "consumer.py").write_text("import os\nimport numpy\nimport lib_c\n\ndef use():\n    return lib_c.c()\n")
        rc, data, _ = _scan_and_query(root, scan_index, scan_query, ["deps", "consumer", "--third-party"])
        assert rc == 0, data
        assert data["direct_imports"] == ["numpy"]

    def test_src_layout_internal_resolution(self, tmp_path, scan_index, scan_query):
        """Resolve as internal when indexed as ``src.mypackage.x``.

        The package lives under ``src/mypackage`` and is indexed as ``src.mypackage``; a bare ``import mypackage`` from
        outside ``src`` must still classify as internal, not third-party.
        """
        root = tmp_path / "src_layout"
        root.mkdir()
        src_dir = root / "src" / "mypackage"
        src_dir.mkdir(parents=True)
        (src_dir / "__init__.py").write_text("")
        (src_dir / "core.py").write_text("def core_fn():\n    return 42\n")
        # Consumer file outside src/ so that classify_imports sees a bare 'import mypackage'.
        (root / "consumer.py").write_text("import mypackage\n\ndef use():\n    return mypackage\n")
        rc, data, _ = _scan_and_query(root, scan_index, scan_query, ["import-types", "consumer"])
        assert rc == 0, data
        assert "mypackage" in data["internal"], (
            f"expected 'mypackage' to resolve internal via src.* prefix, got groups={data}"
        )

    @pytest.mark.parametrize(
        ("group", "import_name"),
        [
            pytest.param("stdlib", "os", id="stdlib-os"),
            pytest.param("stdlib", "collections", id="stdlib-collections"),
            pytest.param("third_party", "requests.sessions", id="third_party"),
            pytest.param("internal", "pkg", id="internal-pkg"),
            pytest.param("internal", "pkg.core", id="internal-pkg.core"),
        ],
    )
    def test_import_shapes_are_classified(self, tmp_path, scan_index, scan_query, group, import_name):
        """Aliases, ImportFrom, submodules, and local packages classify into the expected group.

        One consumer uses an aliased stdlib import, a ``from`` import, an aliased dotted third-party import and local
        package forms; each name lands in its expected group and in no other.
        """
        root = tmp_path / f"import_shape_{group}_{import_name.replace('.', '_')}"
        root.mkdir()
        pkg = root / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "core.py").write_text("VALUE = 1\n")
        (root / "consumer.py").write_text(
            "import os as operating_system\n"
            "from collections import deque\n"
            "import requests.sessions as sessions\n"
            "import pkg.core\n"
            "from pkg import core\n\n"
            "def use():\n"
            "    return operating_system.getcwd(), deque(), sessions, pkg.core.VALUE, core.VALUE\n"
        )
        rc, data, _ = _scan_and_query(root, scan_index, scan_query, ["import-types", "consumer"])
        assert rc == 0, data
        assert import_name in data[group]
        assert all(
            import_name not in data[other_group] for other_group in {"stdlib", "third_party", "internal"} - {group}
        )


class TestDocstringCoverage:
    """v4.4 — ``has_docstring`` / ``docstring_first_line`` per symbol and the ``undocumented`` query.

    Public symbol rule: a qualified_name component must not start with ``_`` — excludes
    dunders (``__init__``), private helpers (``_compute``), and private class names.
    """

    def _scan(self, root: Path, scan_index: Path) -> Path:
        """Run scan-index against *root* and return the produced index path."""
        return _build_index(scan_index, root)

    def _query(self, root: Path, index_path: Path, scan_query: Path, query: list[str]) -> tuple[int, dict, str]:
        """Run scan-query from *root* against *index_path* and return ``(rc, parsed_json, stderr)``."""
        result = _run_query(scan_query, index_path, *query, cwd=root)
        return result.returncode, json.loads(result.stdout), result.stderr

    def _load_index(self, index_path: Path) -> dict:
        """Load the raw index JSON for direct field inspection.

        Args:
            index_path: path to the index JSON file.
        """
        return json.loads(index_path.read_text())

    @pytest.mark.parametrize(
        ("project_dir", "source", "function_name", "expected_has_docstring", "expected_first_line"),
        [
            pytest.param(
                "doc_true",
                'def documented(x):\n    """Does something useful."""\n    return x\n',
                "documented",
                True,
                "Does something useful.",
                id="documented-function",
            ),
            pytest.param(
                "doc_missing",
                "def undocumented(x):\n    return x + 1\n",
                "undocumented",
                False,
                None,
                id="undocumented-function",
            ),
        ],
    )
    def test_function_docstring_fields_indexed(
        self, tmp_path, scan_index, project_dir, source, function_name, expected_has_docstring, expected_first_line
    ):
        """A function is indexed with explicit ``has_docstring`` and ``docstring_first_line`` fields.

        A documented function is flagged ``has_docstring=True`` with its first line; a function without a docstring gets
        explicit false/None fields.
        """
        root = tmp_path / project_dir
        root.mkdir()
        (root / "mymod.py").write_text(source)
        index_path = self._scan(root, scan_index)
        index = self._load_index(index_path)
        mod = next(m for m in index["modules"] if m["name"] == "mymod")
        sym = next(s for s in mod["symbols"] if s["name"] == function_name)
        assert sym["has_docstring"] is expected_has_docstring
        assert sym["docstring_first_line"] == expected_first_line

    @pytest.mark.parametrize(
        ("project_dir", "source", "function_name", "expected_listed", "expected_total"),
        [
            pytest.param(
                "doc_exclude",
                'def documented(x):\n    """Does something useful."""\n    return x\n',
                "documented",
                False,
                0,
                id="documented-function-excluded",
            ),
            pytest.param(
                "doc_missing_query",
                "def undocumented(x):\n    return x + 1\n",
                "undocumented",
                True,
                1,
                id="undocumented-function-returned",
            ),
        ],
    )
    def test_undocumented_query_lists_only_undocumented_function(
        self, tmp_path, scan_index, scan_query, project_dir, source, function_name, expected_listed, expected_total
    ):
        """A documented function is absent from the ``undocumented`` result; a public undocumented one surfaces.

        The same ``undocumented`` query is run on a one-function module: with a docstring the function is absent and the
        total is 0, without one it is listed and the total is 1.
        """
        root = tmp_path / project_dir
        root.mkdir()
        (root / "mymod.py").write_text(source)
        index_path = self._scan(root, scan_index)
        rc, data, _ = self._query(root, index_path, scan_query, ["undocumented", "mymod"])
        assert rc == 0, data
        names = {f["name"] for f in data["undocumented"]}
        assert (function_name in names) is expected_listed
        assert data["total"] == expected_total

    def test_async_decorated_class_and_blank_first_line_docstrings_indexed(self, tmp_path, scan_index):
        """Docstring fields are populated for async, decorated, class, and blank-first-line cases.

        Docstrings that start on the line after the opening quotes, on decorated async functions, classes and methods,
        still yield the first non-blank line as the summary.
        """
        root = tmp_path / "doc_shapes"
        root.mkdir()
        (root / "mymod.py").write_text(
            "def decorator(obj):\n"
            "    return obj\n"
            "\n"
            "@decorator\n"
            "async def async_documented():\n"
            '    """\n'
            "    Async summary.\n"
            '    """\n'
            "    return 1\n"
            "\n"
            "@decorator\n"
            "class Documented:\n"
            '    """\n'
            "    Class summary.\n"
            '    """\n'
            "\n"
            "    @decorator\n"
            "    def method(self):\n"
            '        """\n'
            "        Method summary.\n"
            '        """\n'
            "        return 2\n"
        )
        index = self._load_index(self._scan(root, scan_index))
        mod = next(m for m in index["modules"] if m["name"] == "mymod")
        by_qname = {s["qualified_name"]: s for s in mod["symbols"]}
        assert by_qname["async_documented"]["has_docstring"] is True
        assert by_qname["async_documented"]["docstring_first_line"] == "Async summary."
        assert by_qname["Documented"]["has_docstring"] is True
        assert by_qname["Documented"]["docstring_first_line"] == "Class summary."
        assert by_qname["Documented.method"]["has_docstring"] is True
        assert by_qname["Documented.method"]["docstring_first_line"] == "Method summary."

    @pytest.mark.parametrize(
        ("project_dir", "source", "expected_present", "expected_absent"),
        [
            pytest.param(
                "doc_public_shapes",
                "async def missing_async():\n"
                "    return 1\n"
                "\n"
                "class MissingClass:\n"
                "    def documented_method(self):\n"
                '        """Method docs do not document the class itself."""\n'
                "        return 2\n",
                {"missing_async", "MissingClass"},
                set(),
                id="public-async-function-and-class",
            ),
            pytest.param(
                "doc_method",
                "class MyClass:\n    def method_no_doc(self):\n        return 1\n",
                {"MyClass.method_no_doc"},
                set(),
                id="class-method-under-qualified-name",
            ),
            pytest.param(
                "doc_dunder",
                "class MyClass:\n    def __init__(self):\n        self.x = 0\n",
                {"MyClass"},
                {"MyClass.__init__"},
                id="dunder-init-excluded-but-class-public",
            ),
        ],
    )
    def test_undocumented_query_reports_public_symbols_by_qualified_name(
        self, tmp_path, scan_index, scan_query, project_dir, source, expected_present, expected_absent
    ):
        """Public async functions, classes, and class methods without docstrings are reported by qualified_name.

        Rule: ``qualified_name`` containing any component starting with ``_`` is considered non-public — covers dunders,
        private helpers, and private classes uniformly — so an undocumented ``__init__`` is excluded while the class
        itself, having no docstring, is public and appears.
        """
        root = tmp_path / project_dir
        root.mkdir()
        (root / "mymod.py").write_text(source)
        index_path = self._scan(root, scan_index)
        rc, data, _ = self._query(root, index_path, scan_query, ["undocumented", "mymod"])
        assert rc == 0, data
        qnames = {f["qualified_name"] for f in data["undocumented"]}
        assert expected_present <= qnames
        assert qnames.isdisjoint(expected_absent)

    def test_all_flag_returns_symbols_across_modules(self, tmp_path, scan_index, scan_query):
        """Return undocumented public symbols from every non-test module.

        ``undocumented --all`` scans every non-test module: ``fn_a`` and ``fn_c`` are listed, the documented ``fn_b`` is
        not, and the total is at least two.
        """
        root = tmp_path / "doc_all"
        root.mkdir()
        (root / "mod_a.py").write_text("def fn_a(x):\n    return x\n")
        (root / "mod_b.py").write_text('def fn_b(x):\n    """Documented."""\n    return x\n')
        (root / "mod_c.py").write_text("def fn_c(x):\n    return x\n")
        index_path = self._scan(root, scan_index)
        rc, data, _ = self._query(root, index_path, scan_query, ["undocumented", "--all"])
        assert rc == 0, data
        modules = {f["module"] for f in data["undocumented"]}
        names = {(f["module"], f["name"]) for f in data["undocumented"]}
        assert {"mod_a", "mod_c"}.issubset(modules)
        assert ("mod_b", "fn_b") not in names
        assert data["total"] >= 2

    def test_docstring_first_line_truncated_at_80_chars(self, tmp_path, scan_index):
        """A long single-line docstring is stored truncated to 80 characters exactly.

        A 200-character one-line docstring is cut to exactly its first 80 characters in the index, which bounds index
        size.
        """
        root = tmp_path / "doc_long"
        root.mkdir()
        long_line = "x" * 200
        (root / "mymod.py").write_text(f'def big(x):\n    """{long_line}"""\n    return x\n')
        index_path = self._scan(root, scan_index)
        index = self._load_index(index_path)
        mod = next(m for m in index["modules"] if m["name"] == "mymod")
        sym = next(s for s in mod["symbols"] if s["name"] == "big")
        assert sym["has_docstring"] is True
        assert sym["docstring_first_line"] is not None
        assert len(sym["docstring_first_line"]) == 80
        assert sym["docstring_first_line"] == "x" * 80

    def test_loc_sort_order_largest_first(self, tmp_path, scan_index, scan_query):
        """Findings are sorted by LOC descending — the biggest undocumented symbol comes first.

        ``big`` spans more lines than ``small`` but is defined after it; the result is sorted by size, largest first,
        not by source order.
        """
        root = tmp_path / "doc_sort"
        root.mkdir()
        # ``big`` spans 4 LOC (end_line − start_line = 5 − 1 = 4); ``small`` spans 1.
        src = "def small(x):\n    return x\ndef big(x):\n    a = 1\n    b = 2\n    c = 3\n    return x\n"
        (root / "mymod.py").write_text(src)
        index_path = self._scan(root, scan_index)
        rc, data, _ = self._query(root, index_path, scan_query, ["undocumented", "mymod"])
        assert rc == 0, data
        names_in_order = [f["name"] for f in data["undocumented"]]
        assert names_in_order.index("big") < names_in_order.index("small")


class TestUncovered:
    """v4.2 — ``uncovered`` query: public symbols with no test callers and no mocks.

    Uncovered = public ``qualified_name`` (no leading ``_`` in any component)
    AND ``fn_rdep_test_count == 0`` AND ``mock_rdep_count == 0``. Both counters
    are stored fields populated by ``scan-index`` (v4.1+), so the query reads
    them directly — no graph rebuild needed.
    """

    @staticmethod
    def _make_index(modules: list[dict], scan_version: int = 5) -> dict:
        """Return a minimal hand-crafted index dict for unit-level tests.

        >>> TestUncovered._make_index([], 7)["scan_version"]
        7
        """
        return {"scan_version": scan_version, "modules": modules}

    @staticmethod
    def _make_symbol(
        name: str,
        *,
        qualified_name: str | None = None,
        start_line: int = 1,
        end_line: int = 10,
        fn_rdep_test_count: int = 0,
        mock_rdep_count: int = 0,
    ) -> dict:
        """Return a stored-shape symbol dict matching the schema fields used by ``cmd_uncovered``.

        >>> TestUncovered._make_symbol("run")["qualified_name"]
        'run'
        """
        return {
            "name": name,
            "qualified_name": qualified_name or name,
            "type": "function",
            "start_line": start_line,
            "end_line": end_line,
            "fn_rdep_test_count": fn_rdep_test_count,
            "mock_rdep_count": mock_rdep_count,
        }

    @staticmethod
    def _ns(
        *,
        module: str | None,
        all_modules: bool = False,
        sort: str = "loc",
        top: int = 20,
    ):
        """Return an argparse.Namespace shim for direct ``cmd_uncovered`` invocation.

        >>> TestUncovered._ns(module="pkg").module
        'pkg'
        """
        return argparse.Namespace(module=module, all_modules=all_modules, sort=sort, top=top)

    def _run(self, capsys, index: dict, ns) -> dict:
        """Invoke ``cmd_uncovered`` and return the parsed JSON payload from stdout."""
        _scan_query_mod.cmd_uncovered(index, ns)
        captured = capsys.readouterr()
        return json.loads(captured.out)

    @staticmethod
    def _module(name: str, *symbols: dict, is_test: bool = False) -> dict:
        """Return a stored-shape module entry named *name* holding *symbols*."""
        return {"name": name, "status": "ok", "is_test": is_test, "symbols": list(symbols)}

    @classmethod
    def _index_with(cls, *symbols: dict) -> dict:
        """Return an index holding one non-test module, ``mymod``, with *symbols*."""
        return cls._make_index([cls._module("mymod", *symbols)])

    def test_public_uncovered_function_appears(self, capsys):
        """Public fn with both counters at 0 surfaces in the result.

        A single public function with no test callers and no mocks is the only module symbol; it is listed, with
        ``total``, ``showing`` and ``module`` filled in.
        """
        index = self._index_with(self._make_symbol("foo"))
        data = self._run(capsys, index, self._ns(module="mymod"))
        assert "foo" in {f["name"] for f in data["uncovered"]}
        assert data["total"] == 1
        assert data["showing"] == 1
        assert data["module"] == "mymod"

    @pytest.mark.parametrize(
        ("excluded_name", "excluded_counts", "kept_name"),
        [
            pytest.param(
                "covered", {"fn_rdep_test_count": 3, "mock_rdep_count": 0}, "uncovered", id="test-callers-exclude"
            ),
            pytest.param("mocked", {"fn_rdep_test_count": 0, "mock_rdep_count": 2}, "orphan", id="mock-excludes"),
        ],
    )
    def test_covered_function_excluded(self, capsys, excluded_name, excluded_counts, kept_name):
        """A public fn with ``fn_rdep_test_count >= 1`` or ``mock_rdep_count >= 1`` is filtered out.

        A mocked fn is excluded even when no direct test callers exist; the sibling with both counters at zero stays.
        """
        index = self._index_with(self._make_symbol(excluded_name, **excluded_counts), self._make_symbol(kept_name))
        data = self._run(capsys, index, self._ns(module="mymod"))
        names = {f["name"] for f in data["uncovered"]}
        assert excluded_name not in names
        assert kept_name in names

    def test_private_function_excluded(self, capsys):
        """Leading-underscore symbols never appear (public filter).

        A private component anywhere in ``qualified_name`` (``Klass._priv``) also hides the symbol, even when its own
        ``name`` looks public.
        """
        index = self._index_with(
            self._make_symbol("_helper"),
            self._make_symbol("__dunder__"),
            self._make_symbol("Public", qualified_name="Klass._priv"),
            self._make_symbol("public_fn"),
        )
        data = self._run(capsys, index, self._ns(module="mymod"))
        names = {f["name"] for f in data["uncovered"]}
        assert "_helper" not in names
        assert "__dunder__" not in names
        assert "Public" not in names  # private component in qualified_name → filtered
        assert "public_fn" in names

    def test_all_flag_spans_non_test_modules(self, capsys):
        """Scan every non-test module and excludes test modules.

        Symbols in a test module are never reported even when their own counters are zero.
        """
        index = self._make_index(
            [
                self._module("mod_a", self._make_symbol("a_fn")),
                self._module("mod_b", self._make_symbol("b_fn")),
                self._module("tests.test_mod", self._make_symbol("test_fn"), is_test=True),
            ]
        )
        data = self._run(capsys, index, self._ns(module=None, all_modules=True))
        assert {f["module"] for f in data["uncovered"]} == {"mod_a", "mod_b"}
        assert "test_fn" not in {f["name"] for f in data["uncovered"]}

    def test_top_caps_output(self, capsys):
        """Limit displayed results while preserving the complete result count.

        Five uncovered functions with ``top=2``: two are shown while ``total`` stays 5 so the truncation is visible.
        """
        index = self._index_with(*[self._make_symbol(f"fn_{i}", end_line=10 + i) for i in range(5)])
        data = self._run(capsys, index, self._ns(module="mymod", top=2))
        assert data["total"] == 5
        assert data["showing"] == 2
        assert len(data["uncovered"]) == 2

    @pytest.mark.parametrize(
        ("symbol_spans", "ns_kwargs", "expected_order"),
        [
            pytest.param(
                (("zeta", 1, 100), ("alpha", 1, 2), ("mu", 1, 50)),
                {"sort": _scan_query_mod.UncoveredSort.NAME},
                ["alpha", "mu", "zeta"],
                id="sort-name-alphabetical",
            ),
            pytest.param(
                (("small", 1, 3), ("big", 1, 100), ("medium", 1, 20)),
                {},
                ["big", "medium", "small"],
                id="default-sort-loc-biggest-first",
            ),
            pytest.param(
                (("highline", 196, 200), ("bigspan", 1, 100)),
                {},
                ["bigspan", "highline"],
                id="loc-uses-start-line-span",
            ),
        ],
    )
    def test_sort_orders_uncovered_symbols(self, capsys, symbol_spans, ns_kwargs, expected_order):
        """Findings are ordered alphabetically for ``--sort name`` and by LOC span, biggest first, by default.

        LOC sort ranks by ``end_line - start_line``, not by ``end_line`` alone: ``highline`` ends at line 200 but spans
        only 5 lines, while ``bigspan`` ends at 100 but spans 99 lines, so span-based sort puts ``bigspan`` first. A
        broken implementation that uses ``end_line`` alone would put ``highline`` first, so that case is a regression
        guard for that failure mode.
        """
        index = self._index_with(
            *[self._make_symbol(name, start_line=start, end_line=end) for name, start, end in symbol_spans]
        )
        data = self._run(capsys, index, self._ns(module="mymod", **ns_kwargs))
        assert [f["name"] for f in data["uncovered"]] == expected_order

    def test_sort_loc_missing_start_line_ranks_last(self, capsys):
        """Symbol without ``start_line`` gets loc=0 and sorts below symbols with a proper span.

        A symbol record lacking ``start_line`` is scored as zero lines and sorts after a normal 50-line one, rather than
        crashing or ranking first by its large ``end_line``.
        """
        sym_no_start = {
            "name": "no_start",
            "qualified_name": "no_start",
            "type": "function",
            "end_line": 999,
            "fn_rdep_test_count": 0,
            "mock_rdep_count": 0,
        }
        index = self._index_with(sym_no_start, self._make_symbol("normal", start_line=1, end_line=50))
        data = self._run(capsys, index, self._ns(module="mymod"))
        assert [f["name"] for f in data["uncovered"]][-1] == "no_start"

    @pytest.mark.parametrize(
        ("index_kwargs", "ns_kwargs", "expected_output"),
        [
            pytest.param({}, {"module": None, "all_modules": False}, "--all", id="neither-module-nor-all"),
            pytest.param(
                {"scan_version": 3},
                {"module": None, "all_modules": True},
                "fn_rdep_test_count",
                id="index-below-v4",
            ),
        ],
    )
    def test_unusable_invocation_exits_with_hint(self, capsys, index_kwargs, ns_kwargs, expected_output):
        """Missing both ``module`` and ``--all`` exits with a usage hint; a pre-v4 index names the feature.

        Calling ``uncovered`` with neither a module nor ``--all`` is a usage error that points at ``--all``; an index
        older than v4 lacks the counters the query reads, so the error names ``fn_rdep_test_count`` instead of answering
        wrongly.
        """
        index = self._make_index([], **index_kwargs)
        with pytest.raises(SystemExit):
            _scan_query_mod.cmd_uncovered(index, self._ns(**ns_kwargs))
        assert expected_output in capsys.readouterr().out

    def test_end_to_end_via_subprocess(self, tmp_path, scan_index, scan_query):
        """End-to-end: scan-index populates counters; scan-query uncovered surfaces only true orphans.

        Layout: ``mylib.py`` has public ``used_fn`` (called by a test) and ``orphan_fn`` (never reached);
        ``tests/test_mylib.py`` imports and calls ``used_fn``. ``orphan_fn`` must appear in the uncovered list and
        ``used_fn`` must not, which only holds if the scanner stored real counters.
        """
        root = tmp_path / "end_to_end"
        (root / "tests").mkdir(parents=True)
        _write_files(
            root,
            {
                "mylib.py": "def used_fn(x):\n    return x + 1\n\n\ndef orphan_fn(x):\n    return x * 2\n",
                "tests/test_mylib.py": "from mylib import used_fn\n\n\ndef test_used():\n    assert used_fn(1) == 2\n",
            },
        )
        index_path = _build_index(scan_index, root)

        query_result = _run_query(scan_query, index_path, "uncovered", "--all", cwd=root)

        assert query_result.returncode == 0, query_result.stderr
        data = json.loads(query_result.stdout)
        names = {(f["module"], f["name"]) for f in data["uncovered"]}
        assert ("mylib", "orphan_fn") in names, data
        assert ("mylib", "used_fn") not in names, data


class TestSphinxXrefs:
    """v4.5 — Sphinx + MkDocs cross-reference indexing and the ``xrefs`` query.

    Covers three input surfaces:
      * Python docstrings carrying ``:role:`target``` Sphinx roles
      * ``.rst`` files anywhere under the project
      * ``docs/**/*.md`` files with mkdocstrings ``[text][identifier]`` autorefs

    Each test scans a self-contained ``tmp_path`` project, then queries the
    ``xrefs`` subcommand and asserts on the parsed JSON.
    """

    def _scan(self, root: Path, scan_index: Path) -> Path:
        """Run ``scan-index`` against *root* and return the produced index path."""
        return _build_index(scan_index, root)

    def _query(self, root: Path, index_path: Path, scan_query: Path, query: list[str]) -> tuple[int, dict, str]:
        """Run ``scan-query`` against *index_path* and return ``(rc, parsed_json, stderr)``."""
        result = _run_query(scan_query, index_path, *query, cwd=root)
        return result.returncode, json.loads(result.stdout), result.stderr

    @pytest.mark.parametrize(
        "docstring_ref",
        [
            pytest.param("mymod.target_fn", id="plain-target"),
            pytest.param("~mymod.target_fn", id="tilde-prefix-stripped"),
        ],
    )
    def test_sphinx_func_role_in_python_docstring(self, tmp_path, scan_index, scan_query, docstring_ref):
        """Sphinx function references in Python docstrings are indexed against the qualified target.

        A leading ``~`` only shortens the rendered text, so the shortened spelling must normalize to the same
        ``mymod::target_fn`` symbol as the plain one.
        """
        root = tmp_path / "xref_func"
        root.mkdir()
        _write_files(
            root,
            {
                "mymod.py": "def target_fn():\n    return 1\n",
                "user.py": f'def user_fn():\n    """Calls :func:`{docstring_ref}` for its result."""\n    return 1\n',
            },
        )
        index_path = self._scan(root, scan_index)
        rc, data, _ = self._query(root, index_path, scan_query, ["xrefs", "mymod::target_fn"])
        assert rc == 0, data
        assert data["count"] >= 1, data
        assert "sphinx" in {ref["source"] for ref in data["refs"]}
        assert "func" in {ref["role"] for ref in data["refs"]}

    @pytest.mark.parametrize(
        ("project_dir", "module_source", "doc_file", "doc_text", "target", "expected_source"),
        [
            pytest.param(
                "xref_rst",
                "class MyCls:\n    pass\n",
                "api.rst",
                "See :class:`mymod.MyCls` for usage.\n",
                "mymod::MyCls",
                "sphinx",
                id="sphinx-class-role-in-rst",
            ),
            pytest.param(
                "xref_mkdocs",
                "def target_fn():\n    return 1\n",
                "api.md",
                "See [the function][mymod.target_fn] for details.\n",
                "mymod::target_fn",
                "mkdocs",
                id="mkdocs-named-link-in-md",
            ),
        ],
    )
    def test_doc_file_reference_indexed_with_its_source(
        self, tmp_path, scan_index, scan_query, project_dir, module_source, doc_file, doc_text, target, expected_source
    ):
        """Index Sphinx references from ``.rst`` files and MkDocs autorefs from ``.md`` files with their source.

        The ``source`` label records which documentation system produced the reference: ``sphinx`` for a role in an
        ``.rst`` file and ``mkdocs`` for a ``[text][identifier]`` autoref in a Markdown file under ``docs/``.
        """
        root = tmp_path / project_dir
        root.mkdir()
        (root / "mymod.py").write_text(module_source)
        docs_dir = root / "docs"
        docs_dir.mkdir()
        (docs_dir / doc_file).write_text(doc_text)
        index_path = self._scan(root, scan_index)
        rc, data, _ = self._query(root, index_path, scan_query, ["xrefs", target])
        assert rc == 0, data
        assert data["count"] >= 1, data
        sources = {ref["source"] for ref in data["refs"]}
        assert expected_source in sources

    def test_broken_ref_to_unknown_symbol(self, tmp_path, scan_index, scan_query):
        """Report unresolved Sphinx function references as broken.

        A docstring cites ``mymod.does_not_exist``; ``xrefs mymod --broken`` lists that unresolved target but not the
        real ``real_fn``.
        """
        root = tmp_path / "xref_broken"
        root.mkdir()
        (root / "mymod.py").write_text("def real_fn():\n    return 1\n")
        (root / "user.py").write_text(
            'def user_fn():\n    """Wrongly cites :func:`mymod.does_not_exist`."""\n    return 1\n'
        )
        index_path = self._scan(root, scan_index)
        rc, data, _ = self._query(root, index_path, scan_query, ["xrefs", "mymod", "--broken"])
        assert rc == 0, data
        targets = {ref["target"] for ref in data["broken"]}
        assert "mymod::does_not_exist" in targets, data
        # Real symbol must NOT be reported as broken.
        assert "mymod::real_fn" not in targets

    def test_xref_in_module_docstring_counted(self, tmp_path, scan_index, scan_query):
        """Count Sphinx function references found in module docstrings.

        A package ``__init__`` docstring references ``mymod.target_fn``; the reference is attributed to that
        ``__init__.py`` file and counted.
        """
        root = tmp_path / "xref_module_doc"
        root.mkdir()
        (root / "mymod.py").write_text("def target_fn():\n    return 1\n")
        (root / "pkg").mkdir()
        # Module-level docstring of the __init__ module.
        (root / "pkg" / "__init__.py").write_text(
            '"""Package overview — see :func:`mymod.target_fn` for the canonical helper."""\n',
            encoding="utf-8",
        )
        index_path = self._scan(root, scan_index)
        rc, data, _ = self._query(root, index_path, scan_query, ["xrefs", "mymod::target_fn"])
        assert rc == 0, data
        assert data["count"] >= 1, data
        files = {ref["file"] for ref in data["refs"]}
        assert any("__init__.py" in f for f in files), data

    def test_xref_count_top_level_field(self, tmp_path, scan_index):
        """The index carries a top-level ``sphinx_xref_count`` mapping for reverse lookups.

        Two modules cite ``mymod.target_fn``; the index stores the per-target count at the top level so reverse lookups
        need no extra scan.
        """
        root = tmp_path / "xref_count"
        root.mkdir()
        (root / "mymod.py").write_text("def target_fn():\n    return 1\n")
        (root / "user_a.py").write_text('def a():\n    """Calls :func:`mymod.target_fn`."""\n    return 1\n')
        (root / "user_b.py").write_text('def b():\n    """Also uses :func:`mymod.target_fn`."""\n    return 1\n')
        index_path = self._scan(root, scan_index)
        index = json.loads(index_path.read_text())
        assert "sphinx_xref_count" in index, "top-level sphinx_xref_count missing from index"
        assert index["sphinx_xref_count"].get("mymod::target_fn", 0) >= 2


#: Source of a never-imported module whose single public function spans 6 lines (above the default ``--min-loc``).
_LONELY_SRC = "def forgotten_fn(x):\n    a = 1\n    b = 2\n    c = 3\n    d = 4\n    return x + a + b + c + d\n"


class TestDeadSymbols:
    """v4.6 — ``dead-symbols`` / ``dead-modules`` queries.

    A symbol is dead when every static reachability signal is zero: the owning
    module has no importers, the symbol has no function-level callers (reverse
    call graph), no test mocks it, and no docs reference it. Modules with star
    imports are skipped wholesale because star imports defeat the call graph.

    Schema requirements (fail-closed):
      * ``scan_version >= 6`` — checked via ``_require_feature``
      * top-level ``sphinx_xref_count`` table present — never silently treat
        missing as zero (would mark documented symbols dead)
    """

    @staticmethod
    def _make_index(
        modules: list[dict],
        *,
        scan_version: int = 6,
        sphinx_xref_count: dict[str, int] | None = None,
        include_sphinx_table: bool = True,
    ) -> dict:
        """Return a minimal hand-crafted index dict for unit-level tests.

        Args:
            modules: list of module entries to embed.
            scan_version: scan_version field; default 6 (v4.6 minimum).
            sphinx_xref_count: reverse xref table; default empty.
            include_sphinx_table: when False, omit ``sphinx_xref_count`` entirely
                (used to assert fail-closed behaviour).
        """
        index: dict = {"scan_version": scan_version, "modules": modules}
        if include_sphinx_table:
            index["sphinx_xref_count"] = sphinx_xref_count or {}
        return index

    @staticmethod
    def _make_module(
        name: str,
        *,
        symbols: list[dict] | None = None,
        rdep_count: int = 0,
        is_entry_point: bool = False,
        is_test: bool = False,
        has_star_imports: bool = False,
        exports: list[str] | None = None,
        loc: int = 10,
    ) -> dict:
        """Return a stored-shape module dict matching the schema fields used by ``cmd_dead_symbols``.

        >>> TestDeadSymbols._make_module("pkg.mod")["status"]
        'ok'
        """
        return {
            "name": name,
            "status": "ok",
            "rdep_count": rdep_count,
            "is_entry_point": is_entry_point,
            "is_test": is_test,
            "has_star_imports": has_star_imports,
            "exports": exports,
            "loc": loc,
            "symbols": symbols or [],
        }

    @staticmethod
    def _make_symbol(
        name: str,
        *,
        qualified_name: str | None = None,
        start_line: int = 1,
        end_line: int = 10,
        mock_rdep_count: int = 0,
        sym_type: str = "function",
    ) -> dict:
        """Return a stored-shape symbol dict matching the schema fields used by ``cmd_dead_symbols``.

        >>> TestDeadSymbols._make_symbol("run")["type"]
        'function'
        """
        return {
            "name": name,
            "qualified_name": qualified_name or name,
            "type": sym_type,
            "start_line": start_line,
            "end_line": end_line,
            "mock_rdep_count": mock_rdep_count,
            "calls": [],
        }

    @staticmethod
    def _ns(*, min_loc: int = 5):
        """Return an argparse.Namespace shim for direct ``cmd_dead_symbols`` invocation.

        >>> TestDeadSymbols._ns(min_loc=8).min_loc
        8
        """
        return argparse.Namespace(min_loc=min_loc)

    @staticmethod
    def _ns_modules():
        """Return an argparse.Namespace shim for direct ``cmd_dead_modules`` invocation."""
        return argparse.Namespace()

    @classmethod
    def _index_with(cls, *symbols: dict, **module_kwargs) -> dict:
        """Return an index holding one module, ``mymod``, with *symbols* and the given module fields."""
        return cls._make_index([cls._make_module("mymod", symbols=list(symbols), **module_kwargs)])

    @pytest.fixture(autouse=True)
    def _reset_caches(self, monkeypatch):
        """Reset module-level caches before every test so each index is parsed fresh."""
        monkeypatch.setattr(_scan_query_mod.index_io, "_symbol_map_cache", None)
        monkeypatch.setattr(_scan_query_mod.index_io, "_rev_graph_cache", None)
        monkeypatch.setattr(_scan_query_mod.coverage, "_coverage_cache", None)

    def _run_dead_symbols(self, capsys, index: dict, ns) -> dict:
        """Invoke ``cmd_dead_symbols`` and return the parsed JSON payload from stdout."""
        _scan_query_mod.cmd_dead_symbols(index, ns)
        return json.loads(capsys.readouterr().out)

    def _run_dead_modules(self, capsys, index: dict, ns) -> dict:
        """Invoke ``cmd_dead_modules`` and return the parsed JSON payload from stdout."""
        _scan_query_mod.cmd_dead_modules(index, ns)
        return json.loads(capsys.readouterr().out)

    @pytest.fixture(name="orphan_project", scope="module")
    @classmethod
    def _orphan_project(cls, tmp_path_factory, scan_index) -> tuple[Path, Path]:
        """Index a project holding one module nobody imports; return ``(root, index_path)``.

        ``lonely.py`` has a public ``forgotten_fn`` spanning at least 5 LOC, with no importer, no docs and no tests.
        """
        root = tmp_path_factory.mktemp("dead_orphan")
        (root / "lonely.py").write_text(_LONELY_SRC)
        return root, _build_index(scan_index, root)

    def test_public_dead_fn_surfaces(self, capsys):
        """Public fn with all signals at zero appears in dead-symbols result.

        ``orphan_fn`` sits in a module with no importers, callers, mocks or doc references, so it is reported dead and
        ``total`` is 1.
        """
        index = self._index_with(self._make_symbol("orphan_fn"))
        data = self._run_dead_symbols(capsys, index, self._ns())
        assert "orphan_fn" in {f["name"] for f in data["dead"]}
        assert data["total"] == 1

    def test_fn_with_caller_excluded(self, capsys):
        """Fn with at least one non-test caller in the reverse call graph is not dead.

        ``uses_called`` in another module calls ``target_mod::called_fn``, so ``called_fn`` has a reverse-call edge
        and is not dead.
        """
        caller = self._make_symbol("uses_called", end_line=5)
        caller["calls"] = [{"target": "target_mod::called_fn", "resolution": "import"}]
        index = self._make_index(
            [
                self._make_module("target_mod", symbols=[self._make_symbol("called_fn")]),
                self._make_module("caller_mod", symbols=[caller]),
            ]
        )
        data = self._run_dead_symbols(capsys, index, self._ns())
        assert "called_fn" not in {f["name"] for f in data["dead"]}

    @pytest.mark.parametrize(
        ("module_name", "module_kwargs", "symbol_name", "symbol_kwargs", "index_kwargs"),
        [
            pytest.param(
                "scripts.runner",
                {"rdep_count": 0, "is_entry_point": True},
                "main_fn",
                {"start_line": 1, "end_line": 20},
                {},
                id="entry-point-module",
            ),
            pytest.param(
                "tests.test_thing",
                {"rdep_count": 0, "is_test": True},
                "test_helper",
                {"start_line": 1, "end_line": 20},
                {},
                id="test-module",
            ),
            pytest.param(
                "mymod",
                {"rdep_count": 0},
                "mocked_fn",
                {"start_line": 1, "end_line": 10, "mock_rdep_count": 2},
                {},
                id="mocked-by-tests",
            ),
            pytest.param(
                "mymod",
                {"rdep_count": 0},
                "documented_fn",
                {"start_line": 1, "end_line": 10},
                {"sphinx_xref_count": {"mymod::documented_fn": 3}},
                id="referenced-by-docs",
            ),
            pytest.param(
                "mymod",
                {"rdep_count": 2},
                "orphan_fn",
                {"start_line": 1, "end_line": 10},
                {},
                id="module-has-importers",
            ),
        ],
    )
    def test_alive_symbol_excluded(self, capsys, module_name, module_kwargs, symbol_name, symbol_kwargs, index_kwargs):
        """A symbol with any live reachability signal is never reported dead.

        Live signals: a symbol in an entry-point module (``__main__`` guard); a symbol in a test module (test files are
        not production surface); ``mock_rdep_count >= 1`` (tests reference it via patch()); a ``sphinx_xref_count``
        entry (docs cite it); a module ``rdep_count > 0``, which disqualifies every symbol the module owns.
        """
        index = self._make_index(
            [
                self._make_module(
                    module_name, **module_kwargs, symbols=[self._make_symbol(symbol_name, **symbol_kwargs)]
                )
            ],
            **index_kwargs,
        )
        data = self._run_dead_symbols(capsys, index, self._ns())
        assert symbol_name not in {f["name"] for f in data["dead"]}

    @pytest.mark.parametrize(
        ("module_kwargs", "symbol_spans", "ns_kwargs", "excluded_name", "kept_name"),
        [
            pytest.param(
                {"rdep_count": 0},
                (("_helper", 1, 10), ("public_fn", 11, 20)),
                {},
                "_helper",
                "public_fn",
                id="private-leading-underscore",
            ),
            pytest.param(
                {"rdep_count": 0, "exports": ["exported_fn"]},
                (("exported_fn", 1, 10), ("hidden_fn", 11, 20)),
                {},
                "exported_fn",
                "hidden_fn",
                id="listed-in-dunder-all",
            ),
            pytest.param(
                {"rdep_count": 0},
                (("trivial", 1, 2), ("big_enough", 10, 20)),
                {"min_loc": 5},
                "trivial",
                "big_enough",
                id="below-min-loc",
            ),
        ],
    )
    def test_filter_drops_candidate_and_keeps_dead_sibling(
        self, capsys, module_kwargs, symbol_spans, ns_kwargs, excluded_name, kept_name
    ):
        """Leading-underscore, ``__all__``-exported, and sub-5-line symbols are not dead candidates.

        The public sibling that none of those filters applies to is still reported dead, so each filter is proven to
        drop only its own symbol.
        """
        index = self._index_with(
            *[self._make_symbol(name, start_line=start, end_line=end) for name, start, end in symbol_spans],
            **module_kwargs,
        )
        data = self._run_dead_symbols(capsys, index, self._ns(**ns_kwargs))
        names = {f["name"] for f in data["dead"]}
        assert excluded_name not in names
        assert kept_name in names

    def test_exports_none_no_filter(self, capsys):
        """When ``exports is None`` (no ``__all__``) the export filter is inactive.

        Modules without ``__all__`` store ``exports: None``; with no list to filter on, a public function is still a
        dead candidate.
        """
        index = self._index_with(self._make_symbol("public_fn"), exports=None)
        data = self._run_dead_symbols(capsys, index, self._ns())
        assert "public_fn" in {f["name"] for f in data["dead"]}

    def test_star_import_module_skipped_and_warned(self, capsys):
        """Modules with ``has_star_imports=True`` are skipped wholesale; warning logged to stderr.

        ``would_look_dead`` lives in a module with star imports, which hide call edges; the module is skipped and named
        in ``skipped_star_import`` plus a stderr warning instead of guessing.
        """
        index = self._index_with(self._make_symbol("would_look_dead", end_line=20), has_star_imports=True)
        _scan_query_mod.cmd_dead_symbols(index, self._ns())
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "would_look_dead" not in {f["name"] for f in data["dead"]}
        assert "mymod" in data["skipped_star_import"]
        assert "mymod" in captured.err
        assert "star imports" in captured.err

    @pytest.mark.parametrize(
        ("command_name", "ns_kwargs", "index_kwargs", "expected_output"),
        [
            pytest.param("cmd_dead_symbols", {"min_loc": 5}, {"scan_version": 3}, "dead-symbol", id="symbols-v3-index"),
            pytest.param(
                "cmd_dead_symbols",
                {"min_loc": 5},
                {"include_sphinx_table": False},
                "sphinx_xref_count",
                id="symbols-missing-sphinx-xref-count",
            ),
            pytest.param("cmd_dead_modules", {}, {"scan_version": 3}, "dead-symbol", id="modules-v3-index"),
        ],
    )
    def test_unusable_index_fails_closed(self, capsys, command_name, ns_kwargs, index_kwargs, expected_output):
        """An index below v6 fails with the feature name in the error; a v6 index lacking ``sphinx_xref_count`` aborts.

        A missing ``sphinx_xref_count`` table is never silently treated as zero, which would mark documented symbols
        dead.
        """
        index = self._make_index([], **index_kwargs)
        with pytest.raises(SystemExit):
            getattr(_scan_query_mod, command_name)(index, argparse.Namespace(**ns_kwargs))
        assert expected_output in capsys.readouterr().out

    def test_sort_loc_descending(self, capsys):
        """Findings are returned biggest-LOC first.

        Three dead functions of 5, 100 and 20 lines come back as huge, medium, small.
        """
        index = self._index_with(
            self._make_symbol("small", start_line=1, end_line=6),
            self._make_symbol("huge", start_line=10, end_line=110),
            self._make_symbol("medium", start_line=120, end_line=140),
        )
        data = self._run_dead_symbols(capsys, index, self._ns())
        assert [f["name"] for f in data["dead"]] == ["huge", "medium", "small"]

    def test_dead_modules_reports_orphan_module(self, capsys):
        """A module with ``rdep_count == 0`` and not an entry point appears in dead-modules.

        ``orphan_mod`` has no importers and is not an entry point, so it is dead; ``used_mod`` has three importers and
        is not.
        """
        index = self._make_index(
            [self._make_module("orphan_mod", rdep_count=0, loc=42), self._make_module("used_mod", rdep_count=3, loc=10)]
        )
        data = self._run_dead_modules(capsys, index, self._ns_modules())
        names = {m["name"] for m in data["dead_modules"]}
        assert "orphan_mod" in names
        assert "used_mod" not in names
        assert data["total"] == 1

    def test_dead_modules_skips_entry_point_and_tests(self, capsys):
        """Entry-point and test modules are excluded from dead-modules regardless of rdep_count.

        ``runner`` is an entry point and ``tests.test_thing`` a test module; both have zero importers by design and must
        not be flagged, unlike ``orphan_lib``.
        """
        index = self._make_index(
            [
                self._make_module("runner", rdep_count=0, is_entry_point=True, loc=20),
                self._make_module("tests.test_thing", rdep_count=0, is_test=True, loc=15),
                self._make_module("orphan_lib", rdep_count=0, loc=30),
            ]
        )
        data = self._run_dead_modules(capsys, index, self._ns_modules())
        names = {m["name"] for m in data["dead_modules"]}
        assert "runner" not in names
        assert "tests.test_thing" not in names
        assert "orphan_lib" in names

    def test_dead_symbols_suppresses_imported_module_symbols(self, tmp_path, scan_index, scan_query):
        """Imported-module symbols are suppressed while an unimported module's symbol is reported.

        Layout: ``mylib.py`` has ``used_fn`` (called by ``user.py``) and ``orphan_fn`` (no caller, no docs, no test);
        ``user.py`` imports mylib and calls ``used_fn``; ``lonely.py`` is never imported, so its public function is
        dead. Every function spans at least 5 LOC so the default ``--min-loc`` threshold keeps all candidates.
        """
        root = tmp_path / "dead_e2e"
        root.mkdir()
        _write_files(
            root,
            {
                "mylib.py": (
                    "def used_fn(x):\n    a = 1\n    b = 2\n    c = 3\n    d = 4\n    return x + a + b + c + d\n"
                    "\n\ndef orphan_fn(x):\n    a = 1\n    b = 2\n    c = 3\n    d = 4\n    return x * (a + b + c + d)\n"
                ),
                "user.py": "import mylib\n\n\ndef driver():\n    return mylib.used_fn(1)\n",
                "lonely.py": _LONELY_SRC,
            },
        )
        index_path = _build_index(scan_index, root)

        query_result = _run_query(scan_query, index_path, "dead-symbols", cwd=root)

        assert query_result.returncode == 0, query_result.stderr
        data = json.loads(query_result.stdout)
        names = {(f["module"], f["name"]) for f in data["dead"]}
        assert ("lonely", "forgotten_fn") in names
        assert ("mylib", "used_fn") not in names
        assert ("mylib", "orphan_fn") not in names

    @pytest.mark.parametrize(
        ("command", "result_key", "expected_row"),
        [
            pytest.param("dead-symbols", "dead", {"module": "lonely", "name": "forgotten_fn"}, id="dead-symbols"),
            pytest.param("dead-modules", "dead_modules", {"name": "lonely"}, id="dead-modules"),
        ],
    )
    def test_end_to_end_orphan_module(self, orphan_project, scan_query, command, result_key, expected_row):
        """End-to-end: a module nobody imports is reported in both dead-modules and dead-symbols.

        ``lonely.py`` holds a public ``forgotten_fn`` spanning at least 5 LOC with no importer, docs or tests, so both
        queries must list it once the scanner has stored the real reachability fields.
        """
        root, index_path = orphan_project

        result = _run_query(scan_query, index_path, command, cwd=root)

        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert any(expected_row.items() <= row.items() for row in data[result_key]), data

    @pytest.mark.parametrize(
        ("project_dir", "source", "expected_exports"),
        [
            pytest.param(
                "exports_static",
                '__all__ = ["public_fn", "AnotherName"]\n'
                "\n"
                "\n"
                "def public_fn(x):\n"
                "    return x\n"
                "\n"
                "\n"
                "def AnotherName(x):\n"
                "    return x\n"
                "\n"
                "\n"
                "def hidden(x):\n"
                "    return x\n",
                ["public_fn", "AnotherName"],
                id="literal-all-list-extracted",
            ),
            pytest.param(
                "exports_missing",
                "def public_fn(x):\n    return x\n",
                None,
                id="absent-all-is-null",
            ),
            pytest.param(
                "exports_dynamic",
                "_names = ['a', 'b']\n"
                "__all__ = [n for n in _names]\n"
                "\n"
                "\n"
                "def a(x):\n"
                "    return x\n"
                "\n"
                "\n"
                "def b(x):\n"
                "    return x\n",
                None,
                id="dynamic-all-is-null",
            ),
        ],
    )
    def test_module_exports_field(self, tmp_path, scan_index, project_dir, source, expected_exports):
        """Scan-index parses literal ``__all__`` lists into ``exports``; absent or dynamic ``__all__`` yields null.

        A module without ``__all__`` reports ``exports: null`` (no export filter), and a dynamic ``__all__``
        (comprehension or non-literal) does too.
        """
        root = tmp_path / project_dir
        root.mkdir()
        (root / "mymod.py").write_text(source)
        index_path = _build_index(scan_index, root)

        index = json.loads(index_path.read_text())

        mod = next(m for m in index["modules"] if m["name"] == "mymod")
        assert mod["exports"] == expected_exports


class TestConftestSyspath:
    """v5.1 — ``extract_conftest_syspath`` static AST detection of conftest.py ``sys.path`` shims.

    Supported shapes:
      * ``sys.path.insert(N, "str_literal")``
      * ``sys.path.insert(N, str(Path(__file__).parent / "name"))``

    Unsupported shapes (multi-level ``.parent``, ``os.path.join``, variable
    indirection, ``.append``, ``.resolve()``) are skipped with a stderr warning.
    """

    @pytest.mark.parametrize(
        ("conftest_rel", "source", "expected_rel"),
        [
            pytest.param("conftest.py", 'import sys\nsys.path.insert(0, "bin")\n', ["bin"], id="string-literal-path"),
            pytest.param(
                "tests/conftest.py",
                'import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).parent / "bin"))\n',
                ["tests/bin"],
                id="path-file-parent-form",
            ),
            pytest.param("conftest.py", "import pytest\n", [], id="no-syspath-insert-yields-empty"),
            pytest.param(
                "conftest.py",
                'import sys\nsys.path.insert(0, "bin")\nsys.path.insert(1, "lib")\n',
                ["bin", "lib"],
                id="multiple-inserts-all-collected",
            ),
        ],
    )
    def test_extracts_supported_syspath_shims(self, tmp_path, conftest_rel, source, expected_rel):
        """Supported ``sys.path.insert`` shapes resolve to absolute paths; a conftest without one yields nothing.

        A string literal resolves against the project root, a ``Path(__file__).parent / name`` form against the
        conftest's own directory, and several inserts are all collected.
        """
        conftest = tmp_path / conftest_rel
        conftest.parent.mkdir(exist_ok=True)
        conftest.write_text(source)

        result = _extract_conftest(conftest, tmp_path)

        assert result == [tmp_path / rel for rel in expected_rel]

    def test_multi_level_parent_skipped(self, tmp_path, capsys):
        """2-level parent.parent is unsupported — skipped with warning.

        ``Path(__file__).parent.parent`` is outside the supported shapes; the extractor returns nothing and warns on
        stderr rather than guessing a path.
        """
        conftest = tmp_path / "tests" / "conftest.py"
        conftest.parent.mkdir()
        conftest.write_text(
            'import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).parent.parent / "bin"))\n'
        )
        result = _extract_conftest(conftest, tmp_path)
        assert result == []
        captured = capsys.readouterr()
        assert "unsupported" in captured.err or "skipped" in captured.err


class TestSubprocessDeps:
    """v5.2 — ``extract_subprocess_calls`` AST detection of subprocess edges + scan-query commands.

    Supported invocation shapes:
      * ``subprocess.run(["python", "<script>"])`` — bare interpreter token + string script.
      * ``subprocess.run([sys.executable, "<script>"])`` — sys.executable form.
      * ``subprocess.run(["python", str(Path(__file__).parent / "<script>.py")])`` — 1-level Path.
      * ``subprocess.Popen(...)`` — same arg shapes as run().
      * ``os.system("python <script> ...")`` — whitespace-split string form.

    Out of scope (silently ignored): ``runpy.run_path``, the ``sh`` library, shell
    strings without a ``python``/``python3`` token, multi-level ``parent.parent`` chains.
    """

    @staticmethod
    def _calls_from(tmp_path: Path, caller_source: str, indexed_files: dict[str, str]) -> tuple[Path, list[dict]]:
        """Write *caller_source* as ``caller.py`` and return ``(path, subprocess edges found)``."""
        src = tmp_path / "caller.py"
        src.write_text(caller_source)
        return src, _extract_subprocess(ast.parse(caller_source), src, tmp_path, indexed_files)

    @pytest.fixture(name="subprocess_query", scope="module")
    @classmethod
    def _subprocess_query(cls, tmp_path_factory, scan_index, scan_query) -> Callable[..., dict]:
        """Index ``caller.py`` launching ``target.py`` through ``subprocess.run``; return a JSON query callable."""
        root = tmp_path_factory.mktemp("sub_e2e")
        _write_files(
            root,
            {
                "target.py": "def main():\n    return 1\n",
                "caller.py": "import subprocess\ndef driver():\n    subprocess.run(['python', 'target.py'])\n",
            },
        )
        index_path = _build_index(scan_index, root)

        def run_query(*args: str) -> dict:
            """Run one query and decode its JSON stdout, requiring exit 0."""
            result = _run_query(scan_query, index_path, *args, cwd=root)
            assert result.returncode == 0, result.stderr
            return json.loads(result.stdout)

        return run_query

    @pytest.mark.parametrize(
        ("script_file", "caller_source", "expected_target"),
        [
            pytest.param(
                "script.py",
                "import subprocess\nsubprocess.run(['python', 'script.py'])\n",
                "script",
                id="run-bare-interpreter",
            ),
            pytest.param(
                "worker.py",
                "import subprocess\nsubprocess.Popen(['python', 'worker.py'])\n",
                "worker",
                id="popen",
            ),
            pytest.param(
                "script.py",
                "import subprocess, sys\nsubprocess.run([sys.executable, 'script.py'])\n",
                "script",
                id="sys-executable-form",
            ),
            pytest.param(
                "x.py",
                "import subprocess\n"
                "from pathlib import Path\n"
                "subprocess.run(['python', str(Path(__file__).parent / 'x.py')])\n",
                "x",
                id="path-file-parent-form",
            ),
            pytest.param("script.py", "import os\nos.system('python script.py')\n", "script", id="os-system"),
        ],
    )
    def test_subprocess_invocation_form_detected(self, tmp_path, script_file, caller_source, expected_target):
        """Resolve each supported invocation shape to exactly one edge targeting the indexed script's module.

        Bare ``python`` and ``Popen`` are detected alike; ``[sys.executable, 'script.py']`` resolves identically to bare
        ``python``; stringified source-relative ``Path(__file__).parent`` paths resolve against the caller; and
        ``os.system`` strings are split so the script token is resolved. The edge records the caller file and the launch
        line.
        """
        (tmp_path / script_file).write_text("# target\n")

        src, calls = self._calls_from(tmp_path, caller_source, {script_file: expected_target, "caller.py": "caller"})

        assert len(calls) == 1
        assert calls[0]["target_module"] == expected_target
        assert calls[0]["file"] == src.relative_to(tmp_path).as_posix()
        assert calls[0]["line"] == caller_source.count("\n")  # the launch is each snippet's last line

    def test_unresolvable_script_skipped(self, tmp_path, capsys):
        """Subprocess call referencing a non-indexed script emits a stderr warning and is skipped.

        The call names ``nonexistent_xyz_123.py``, which is not indexed; no edge is produced and the warning names both
        ``subprocess`` and the script.
        """
        caller_source = "import subprocess\nsubprocess.run(['python', 'nonexistent_xyz_123.py'])\n"

        _, calls = self._calls_from(tmp_path, caller_source, {"caller.py": "caller"})

        assert calls == []
        captured = capsys.readouterr()
        assert "subprocess" in captured.err
        assert "nonexistent_xyz_123.py" in captured.err

    @pytest.mark.parametrize(
        "caller_source",
        [
            pytest.param("def foo():\n    bar()\n    obj.method('python script.py')\n", id="unrelated-calls"),
            pytest.param("import os\nos.system('ls -la /tmp')\n", id="os-system-non-python"),
            pytest.param("import subprocess\nsubprocess.run(['echo', 'hi'])\n", id="subprocess-non-python-token"),
        ],
    )
    def test_non_python_invocation_ignored(self, tmp_path, caller_source):
        """Plain calls, unrelated attribute calls, and non-python commands yield no subprocess edge.

        Calls that merely look similar (an unrelated attribute call, a shell ``ls``, ``echo``) must not be mistaken for
        a Python script launch.
        """
        _, calls = self._calls_from(tmp_path, caller_source, {"caller.py": "caller"})
        assert calls == []

    def test_subprocess_deps_lists_launched_script(self, subprocess_query):
        """``subprocess-deps caller`` lists the script module the caller launches.

        Scan-index must record the ``subprocess.run(['python', 'target.py'])`` edge for the query to find it.
        """
        data = subprocess_query("subprocess-deps", "caller")
        assert "target" in {c["target_module"] for c in data["calls"]}

    def test_subprocess_rdeps_lists_launching_caller(self, subprocess_query):
        """``subprocess-rdeps target`` lists the one module that launches the target script.

        Reverse view of the same scan: ``target`` has exactly one launcher, ``caller``, and the count says 1.
        """
        data = subprocess_query("subprocess-rdeps", "target")
        assert "caller" in {c["caller"] for c in data["callers"]}
        assert data["count"] == 1

    def test_subprocess_rdep_count_at_root(self, tmp_path, scan_index):
        """Top-level subprocess_rdep_count is populated and counts callers per target.

        ``caller_a`` uses ``subprocess.run`` and ``caller_b`` ``subprocess.Popen`` against the same script, so the
        stored count for ``target`` is 2.
        """
        root = tmp_path / "sub_rdep_count"
        root.mkdir()
        _write_files(
            root,
            {
                "target.py": "def main():\n    return 1\n",
                "caller_a.py": "import subprocess\nsubprocess.run(['python', 'target.py'])\n",
                "caller_b.py": "import subprocess\nsubprocess.Popen(['python', 'target.py'])\n",
            },
        )

        index_path = _build_index(scan_index, root)

        index = json.loads(index_path.read_text())
        assert "subprocess_rdep_count" in index
        assert index["subprocess_rdep_count"].get("target") == 2


class TestFixtureGraph:
    """Tests for extract_fixtures() fixture extraction and fixture-graph queries."""

    @staticmethod
    def _fixtures_in(tmp_path: Path, source: str, filename: str = "conftest.py") -> list[dict]:
        """Write *source* as *filename* under *tmp_path* and return the fixtures ``extract_fixtures`` finds."""
        src = tmp_path / filename
        src.write_text(source)
        return _extract_fixtures(ast.parse(source), src)

    def test_basic_fixture_detected(self, tmp_path):
        """@pytest.fixture decorated function is detected.

        A conftest function decorated with ``@pytest.fixture`` yields a record named after the function.
        """
        fixtures = self._fixtures_in(tmp_path, "import pytest\n\n@pytest.fixture\ndef my_fixture():\n    return 42\n")
        assert any(f["name"] == "my_fixture" for f in fixtures)

    @pytest.mark.parametrize(
        ("source", "fixture_name", "attribute", "expected"),
        [
            pytest.param(
                "import pytest\n\n@pytest.fixture(scope='session')\ndef session_fixture():\n    yield {}\n",
                "session_fixture",
                "scope",
                "session",
                id="scope-from-keyword-argument",
            ),
            pytest.param(
                "import pytest\n\n@pytest.fixture\ndef fn_fixture():\n    return 1\n",
                "fn_fixture",
                "scope",
                "function",
                id="scope-defaults-to-function",
            ),
            pytest.param(
                "import pytest\n\n@pytest.fixture(scope='class')\ndef class_fixture():\n    return {}\n",
                "class_fixture",
                "scope",
                "class",
                id="class-scope",
            ),
            pytest.param(
                "import pytest\n\n@pytest.fixture\ndef yield_fixture():\n    yield 'value'\n",
                "yield_fixture",
                "yields",
                True,
                id="yield-in-body-sets-yields",
            ),
        ],
    )
    def test_fixture_attribute_extracted(self, tmp_path, source, fixture_name, attribute, expected):
        """Extract a fixture's scope (keyword, default 'function', class scope) and its yield flag.

        ``scope`` falls back to ``function`` when no keyword is given, and ``yields`` is a real boolean set by a
        ``yield`` in the fixture body; both feed the fixture-graph queries.
        """
        fixtures = self._fixtures_in(tmp_path, source)
        fix = next(f for f in fixtures if f["name"] == fixture_name)
        assert fix[attribute] == expected
        assert type(fix[attribute]) is type(expected)  # a bool flag must be a real bool, not a truthy stand-in

    def test_non_fixture_not_captured(self, tmp_path):
        """Regular functions without @pytest.fixture are ignored.

        A test module with an ordinary helper and test function contains no fixtures, so the extractor returns an empty
        list.
        """
        fixtures = self._fixtures_in(
            tmp_path, "def helper():\n    return 1\n\ndef test_something():\n    assert helper() == 1\n", "test_foo.py"
        )
        assert fixtures == []


# v5.4: shorthand handles for coverage integration tests below. These three moved
# into codemap_py.graph (cross-module coverage annotation), not codemap_py.scanner
# (per-file parsing) — a different package module than the extract_* helpers above.
import codemap_py.graph as _graph_mod  # noqa: E402  (needs the sys.path insert done by _load_scan_index above)

_read_coverage_data = _graph_mod._read_coverage_data
_compute_symbol_coverage = _graph_mod._compute_symbol_coverage
_parse_coverage_version = _graph_mod._parse_coverage_version


def _write_synthetic_coverage_file(
    coverage_path: Path,
    file_to_lines: dict[str, list[int]],
    file_to_contexts: dict[str, dict[int, list[str]]] | None = None,
) -> None:
    """Create a real ``.coverage`` SQLite file via the public CoverageData API.

    Uses ``add_lines`` plus ``set_context`` + ``add_lines`` per context so the
    file is byte-for-byte equivalent to one produced by a live coverage run.

    Args:
        coverage_path: target path for the SQLite file.
        file_to_lines: ``{abs_file_path: [lineno, ...]}`` lines measured
            without any context (always present).
        file_to_contexts: optional ``{abs_file_path: {context_name: [lineno, ...]}}``
            adding context-scoped line hits.
    """
    import coverage as cov_lib

    data = cov_lib.CoverageData(basename=str(coverage_path))
    data.add_lines({path: lines for path, lines in file_to_lines.items()})
    if file_to_contexts:
        for ctx_name, per_file in _invert_contexts(file_to_contexts).items():
            data.set_context(ctx_name)
            data.add_lines(per_file)
    data.write()


def _invert_contexts(
    file_to_contexts: dict[str, dict[int, list[str]]],
) -> dict[str, dict[str, list[int]]]:
    """Re-key context structure from ``{file: {line: [ctx]}}`` to ``{ctx: {file: [line]}}``.

    >>> _invert_contexts({"m.py": {4: ["test"]}})
    {'test': {'m.py': [4]}}
    """
    out: dict[str, dict[str, list[int]]] = {}
    for path, per_line in file_to_contexts.items():
        for line, ctxs in per_line.items():
            for ctx in ctxs:
                out.setdefault(ctx, {}).setdefault(path, []).append(line)
    return out


class TestCoverageComputation:
    """Unit tests for the pure coverage math helpers — no SQLite involved."""

    @pytest.mark.parametrize(
        ("start", "end", "measured", "expected_pct"),
        [
            pytest.param(10, 14, frozenset({10, 11, 12, 13, 14}), 1.0, id="every-line-measured"),
            pytest.param(1, 4, frozenset({1, 2}), 0.5, id="two-of-four-lines-measured"),
            pytest.param(10, 12, frozenset({1, 2, 3}), 0.0, id="no-lines-measured-in-range"),
            pytest.param(5, 5, frozenset({5}), 1.0, id="single-line-symbol-denominator-clamp"),
            pytest.param(10, 5, frozenset(), 0.0, id="inverted-range-clamps-denominator-to-one"),
        ],
    )
    def test_coverage_pct_over_measured_lines(self, start, end, measured, expected_pct):
        """Coverage_pct is the measured share of the symbol's line range, with a safe denominator and no contexts.

        Every line measured gives 1.0; two of four gives 0.5; no measured line in range gives 0.0 (not absent); a 1-line
        symbol with that line measured gives 1.0 (denominator clamp); and, defensively, ``end_line < start_line`` must
        not divide by zero or negative. Without contexts, ``covered_by`` stays None.
        """
        pct, covered_by = _compute_symbol_coverage(start, end, measured, {})
        assert pct == expected_pct
        assert covered_by is None

    def test_covered_by_collects_unique_contexts(self):
        """Multiple lines tagged with overlapping contexts → unique sorted list.

        Three lines carry contexts with ``test_a`` repeated; ``covered_by`` is the sorted union without duplicates.
        """
        contexts = {1: ["test_a"], 2: ["test_b", "test_a"], 3: ["test_c"]}
        _, covered_by = _compute_symbol_coverage(1, 3, frozenset({1, 2, 3}), contexts)
        assert covered_by == ["test_a", "test_b", "test_c"]

    def test_covered_by_ignores_empty_context_string(self):
        """Coverage emits empty string for 'no-context' hits — these must be dropped.

        Coverage records the empty string for hits made outside any test context; those would be noise in ``covered_by``
        and are dropped.
        """
        contexts = {1: [""], 2: ["test_real"]}
        _, covered_by = _compute_symbol_coverage(1, 2, frozenset({1, 2}), contexts)
        assert covered_by == ["test_real"]

    def test_covered_by_is_none_when_contexts_only_outside_range(self):
        """Contexts attached only to lines outside the symbol range are not surfaced.

        A context attached to line 99 does not belong to the symbol spanning lines 1-5, so ``covered_by`` is ``None``.
        """
        contexts = {99: ["test_unrelated"]}
        _, covered_by = _compute_symbol_coverage(1, 5, frozenset({1, 2}), contexts)
        assert covered_by is None


class TestParseCoverageVersion:
    """Unit tests for the small version-parsing helper."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param("7.4.1", (7, 4), id="full-triplet"),
            pytest.param("7.10", (7, 10), id="major-minor-only"),
            pytest.param("10.0.0a1", (10, 0), id="prerelease-suffix"),
        ],
    )
    def test_valid_versions_parsed(self, raw, expected):
        """Major.minor pair must be extracted from any well-formed dotted version.

        Full triplet, major.minor and a pre-release suffix all reduce to a ``(major, minor)`` tuple.
        """
        assert _parse_coverage_version(raw) == expected

    @pytest.mark.parametrize("raw", ["garbage", "7", ""])
    def test_invalid_versions_return_none(self, raw):
        """Anything that cannot be parsed must surface as None, never raise.

        ``garbage``, a single number and the empty string return ``None`` instead of raising, so a bad version string
        degrades quietly.
        """
        assert _parse_coverage_version(raw) is None


class TestReadCoverageData:
    """Tests for the _read_coverage_data filesystem boundary — graceful failure modes."""

    def test_missing_coverage_file_returns_none(self, tmp_path):
        """No file at coverage_path → graceful None + stderr warning, no crash.

        The path does not exist; the reader returns ``None`` and warns instead of crashing the scan.
        """
        result = _read_coverage_data(tmp_path / ".coverage_does_not_exist")
        assert result is None

    def test_real_coverage_file_returns_lines(self, tmp_path):
        """Synthetic .coverage file built via the public API surfaces measured lines.

        A real ``.coverage`` database written through the public coverage API marks lines 1-2 of ``target.py`` as
        measured.
        """
        src = tmp_path / "target.py"
        src.write_text("def foo():\n    return 1\n\ndef bar():\n    return 2\n")
        cov_path = tmp_path / ".coverage"
        _write_synthetic_coverage_file(cov_path, {str(src): [1, 2]})

        data = _read_coverage_data(cov_path)
        assert data is not None
        assert str(src) in data
        assert data[str(src)]["lines"] == frozenset({1, 2})

    def test_real_coverage_file_returns_contexts(self, tmp_path):
        """Context-scoped hits round-trip through the public API.

        A context-tagged hit written through the coverage API comes back under that line with its context name.
        """
        src = tmp_path / "target.py"
        src.write_text("def foo():\n    return 1\n")
        cov_path = tmp_path / ".coverage"
        _write_synthetic_coverage_file(
            cov_path,
            file_to_lines={str(src): [1, 2]},
            file_to_contexts={str(src): {1: ["tests/test_foo.py::test_one"]}},
        )

        data = _read_coverage_data(cov_path)
        assert data is not None
        contexts = data[str(src)]["contexts"]
        assert 1 in contexts
        assert "tests/test_foo.py::test_one" in contexts[1]


class TestCoverageScanIntegration:
    """End-to-end: scan-index ``--with-coverage`` attaches per-symbol fields."""

    def test_with_coverage_attaches_fields(self, tmp_path, scan_index):
        """Running scan-index ``--with-coverage`` stamps coverage_pct on every symbol.

        A synthetic ``.coverage`` file measures both lines of ``foo`` and none of ``bar``, so the index must carry 1.0
        and 0.0 plus the coverage mtime marker used for staleness checks.
        """
        root = tmp_path / "cov_e2e"
        root.mkdir()
        src = root / "mymod.py"
        src.write_text("def foo():\n    return 1\n\ndef bar():\n    return 2\n")
        cov_path = root / ".coverage"
        _write_synthetic_coverage_file(cov_path, {str(src): [1, 2]})

        index_path = _build_index(scan_index, root, "--with-coverage", str(cov_path))

        index = json.loads(index_path.read_text())
        assert index["scan_version"] == 14
        mod = next(m for m in index["modules"] if m["name"] == "mymod")
        foo = next(s for s in mod["symbols"] if s["qualified_name"] == "foo")
        bar = next(s for s in mod["symbols"] if s["qualified_name"] == "bar")
        assert foo["coverage_pct"] == 1.0
        assert bar["coverage_pct"] == 0.0
        assert "__coverage_mtime__" in index["file_shas"]

    def test_without_coverage_flag_no_fields(self, tmp_path, scan_index):
        """Default scan-index (no ``--with-coverage``) leaves coverage_pct absent.

        A plain scan stores no ``coverage_pct`` on any symbol and no coverage mtime marker, so coverage queries can tell
        the data is absent.
        """
        root = tmp_path / "no_cov"
        root.mkdir()
        (root / "plain.py").write_text("def x():\n    return 1\n")

        index_path = _build_index(scan_index, root)

        index = json.loads(index_path.read_text())
        mod = next(m for m in index["modules"] if m["name"] == "plain")
        assert all("coverage_pct" not in s for s in mod["symbols"])
        assert "__coverage_mtime__" not in index["file_shas"]


class TestCoverageQueryCommands:
    """Tests for `scan-query coverage` and `scan-query coverage-gap`."""

    @pytest.fixture(name="covered_project")
    def _covered_project(self, tmp_path, scan_index):
        """Build a small project with a real .coverage file and a v10 index."""
        root = tmp_path / "covered"
        root.mkdir()
        src = root / "mymod.py"
        src.write_text(
            "def full():\n"
            "    return 1\n"
            "\n"
            "def partial():\n"
            "    if True:\n"
            "        return 2\n"
            "    return 3\n"
            "\n"
            "def empty():\n"
            "    return 4\n"
        )
        cov_path = root / ".coverage"
        _write_synthetic_coverage_file(
            cov_path,
            file_to_lines={str(src): [1, 2, 4, 5, 6]},
            file_to_contexts={str(src): {1: ["tests/test_mymod.py::test_full"]}},
        )
        index_path = _build_index(scan_index, root, "--with-coverage", str(cov_path))
        return root, index_path

    def test_coverage_for_symbol_returns_pct(self, covered_project, scan_query):
        """Return coverage_pct == 1.0 and lists the test context.

        ``full`` is fully measured and tagged by one test context; the symbol query returns 1.0 and that context.
        """
        root, index_path = covered_project
        result = _run_query(scan_query, index_path, "coverage", "mymod::full", cwd=root)
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["qualified_name"] == "full"
        assert data["coverage_pct"] == 1.0
        assert data["covered_by"] == ["tests/test_mymod.py::test_full"]

    def test_coverage_for_module_returns_all_symbols(self, covered_project, scan_query):
        """Bare module query returns one entry per symbol with coverage data.

        Querying the bare module returns an entry for each of ``full``, ``partial`` and ``empty``.
        """
        root, index_path = covered_project
        result = _run_query(scan_query, index_path, "coverage", "mymod", cwd=root)
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        names = {row["qualified_name"] for row in data["symbols"]}
        assert {"full", "partial", "empty"}.issubset(names)

    def _coverage_gap_names(self, covered_project, scan_query, threshold: float) -> set[str]:
        """Run coverage-gap for *threshold* and return reported qualified names."""
        root, index_path = covered_project
        result = _run_query(scan_query, index_path, "coverage-gap", "--all", "--threshold", str(threshold), cwd=root)
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        return {row["qualified_name"] for row in data["coverage_gap"]}

    @pytest.mark.parametrize(
        ("threshold", "expected_names"),
        [
            pytest.param(0.0, set(), id="zero-threshold-excludes-zero-coverage"),
            pytest.param(0.75, {"empty"}, id="exact-threshold-excludes-equal-partial"),
            pytest.param(0.7501, {"empty", "partial"}, id="just-above-partial-includes-partial"),
            pytest.param(1.0, {"empty", "partial"}, id="full-threshold-excludes-full-coverage"),
        ],
    )
    def test_coverage_gap_threshold_boundaries(self, covered_project, scan_query, threshold, expected_names):
        """Use strict coverage_pct < threshold semantics.

        ``partial`` is covered at 0.75: the boundary cases show the comparison is strictly below the threshold, so equal
        and full coverage are excluded.
        """
        assert self._coverage_gap_names(covered_project, scan_query, threshold) == expected_names

    def test_coverage_gap_ignores_symbols_with_missing_coverage(self, capsys):
        """Symbols lacking coverage_pct are skipped instead of treated as zero coverage.

        One symbol has no ``coverage_pct`` at all and one is at 0.0; only the measured zero is reported as a gap, since
        unmeasured is unknown rather than zero.
        """
        index = {
            "scan_version": _scan_query_mod.COVERAGE_MIN_VER,
            "modules": [
                {
                    "name": "mymod",
                    "status": "ok",
                    "is_test": False,
                    "symbols": [
                        {
                            "qualified_name": "missing",
                            "type": "function",
                            "start_line": 1,
                            "end_line": 2,
                        },
                        {
                            "qualified_name": "empty",
                            "type": "function",
                            "coverage_pct": 0.0,
                            "start_line": 4,
                            "end_line": 5,
                        },
                    ],
                }
            ],
        }
        _scan_query_mod.cmd_coverage_gap(index, module=None, all_modules=True, threshold=0.5)
        data = json.loads(capsys.readouterr().out)
        assert {row["qualified_name"] for row in data["coverage_gap"]} == {"empty"}

    def test_coverage_gap_sorted_by_gap_desc(self, covered_project, scan_query):
        """The largest gap is reported first (gap = threshold − coverage_pct).

        At threshold 1.0 each row's gap is its shortfall, and rows come back from the largest gap to the smallest.
        """
        root, index_path = covered_project
        result = _run_query(scan_query, index_path, "coverage-gap", "--all", "--threshold", "1.0", cwd=root)
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        gaps = [row["gap"] for row in data["coverage_gap"]]
        assert gaps == sorted(gaps, reverse=True)

    def test_coverage_command_errors_on_index_without_coverage(self, tmp_path, scan_index, scan_query):
        """Reject coverage queries against an index built without coverage data.

        ``plain::hello`` exists in the index but carries no ``coverage_pct``, so the query must error explicitly
        instead of answering with zero coverage.
        """
        root = tmp_path / "no_cov_query"
        root.mkdir()
        (root / "plain.py").write_text("def hello():\n    return 1\n")
        index_path = _build_index(scan_index, root)

        result = _run_query(scan_query, index_path, "coverage", "plain::hello", cwd=root)

        assert result.returncode != 0
        data = json.loads(result.stdout)
        assert "no coverage data" in data.get("error", "")


class TestFeatureVersionGates:
    """Commands that need a newer index fail with a feature-gating error instead of answering from an old schema."""

    @pytest.mark.parametrize(
        ("scan_version", "query_args", "expected_error_fragment"),
        [
            pytest.param(4, ("xrefs", "mymod::fn"), "sphinx_xrefs", id="xrefs-needs-v5"),
            pytest.param(7, ("subprocess-deps", "anything"), "subprocess-deps", id="subprocess-deps-needs-v8"),
            pytest.param(9, ("coverage", "anything::x"), "coverage", id="coverage-needs-v10"),
        ],
    )
    def test_command_rejects_index_below_its_minimum_version(
        self, tmp_path, scan_query, scan_version, query_args, expected_error_fragment
    ):
        """An index older than a command's minimum scan_version exits non-zero with an error naming the feature.

        Each command guards its own feature with a minimum ``scan_version``; an older index must fail loudly with the
        feature named rather than return an answer computed from fields the old schema never recorded.
        """
        root = tmp_path / "gated"
        index_path = root / ".cache" / "codemap" / f"{root.name}.json"
        index_path.parent.mkdir(parents=True)
        index_path.write_text(json.dumps({"scan_version": scan_version, "modules": []}))

        result = _run_query(scan_query, index_path, *query_args, cwd=root)

        assert result.returncode != 0
        data = json.loads(result.stdout)
        assert expected_error_fragment in data.get("error", "")


class TestErrorSemantics:
    """Structured JSON errors + exit-code contract."""

    def test_unknown_module_errors_with_suggestions(self, project, scan_query):
        """Deps on a module absent from the index → exit 3 with difflib suggestions.

        ``gama`` is one edit from the indexed ``gamma``; the exit-3 JSON error carries ``gamma`` among its suggestions.
        """
        root, index_path = project
        result = _run_query(scan_query, index_path, "deps", "gama", cwd=root)
        assert result.returncode == 3, result.stderr + result.stdout
        data = json.loads(result.stdout)
        assert data["error"] == "module not indexed"
        assert data["module"] == "gama"
        # 'gama' is one edit from the indexed 'gamma' → difflib surfaces it.
        assert "gamma" in data["suggestions"]

    @pytest.mark.parametrize(
        ("args", "expected_rc", "expected_fields"),
        [
            pytest.param(
                ("symbols", "zzz.totally.absent"),
                3,
                {"error": "module not indexed", "suggestions": []},
                id="no-close-match-has-empty-suggestions",
            ),
            pytest.param(
                ("find-symbol", "(a+)+"),
                2,
                {"error": "pattern rejected", "reason": "redos"},
                id="catastrophic-backtracking-pattern",
            ),
            pytest.param(("find-symbol", "([unclosed"), 2, {"error": "invalid regex"}, id="invalid-regex"),
        ],
    )
    def test_rejected_query_emits_parseable_json_error(self, project, scan_query, args, expected_rc, expected_fields):
        """A rejected query exits with its documented code and still prints a parseable JSON error object.

        A wildly different module name yields empty suggestions rather than none; a catastrophic-backtracking pattern is
        rejected as ``redos`` and a syntactically invalid regex as ``invalid regex``, never as a traceback.
        """
        root, index_path = project

        result = _run_query(scan_query, index_path, *args, cwd=root)

        assert result.returncode == expected_rc, result.stderr + result.stdout
        assert expected_fields.items() <= json.loads(result.stdout).items()

    def test_rdeps_indexed_leaf_keeps_empty_list(self, project, scan_query):
        """An indexed module with no importers still returns imported_by:[] (not an error).

        ``pkg.delta`` exists but nothing imports it, so rdeps succeeds with an empty list, which distinguishes it from
        the not-indexed error.
        """
        root, index_path = project
        result = _run_query(scan_query, index_path, "rdeps", "pkg.delta", cwd=root)
        assert result.returncode == 0, result.stderr + result.stdout
        data = json.loads(result.stdout)
        assert data["module"] == "pkg.delta"
        assert data["imported_by"] == []

    def test_coords_stale_category(self, tmp_path, scan_index, scan_query):
        """A renamed symbol at the indexed lines → stale_category 'coords_stale' (moved, not gone).

        After indexing, ``mover`` is renamed on the same lines; the symbol is flagged stale in the ``coords_stale``
        category, not as deleted.
        """
        root = tmp_path / "coords_cat"
        root.mkdir()
        (root / "mover.py").write_text("def mover(x):\n    return x\n")
        index_path = _build_index(scan_index, root)
        (root / "mover.py").write_text("def mover_renamed(x):\n    return x\n")

        result = _run_query(scan_query, index_path, "--no-heal", "symbol", "mover", cwd=root)

        assert result.returncode == 0
        sym = json.loads(result.stdout)["symbols"][0]
        assert sym["stale"] is True
        assert sym["stale_category"] == "coords_stale"


class TestRootMismatch:
    """Index scan_root vs queried root — visible mismatch, not silent wrong answers."""

    def test_root_mismatch_flag_and_incomplete(self, tmp_path, scan_index, scan_query):
        """Querying with ``--root`` pointing elsewhere sets root_mismatch and forces query_complete=false.

        The index was scanned in ``scanned`` but queried against the unrelated ``other`` directory; the answer must say
        so in the index block and on stderr instead of presenting itself as complete.
        """
        scanned = tmp_path / "scanned"
        other = tmp_path / "other"
        scanned.mkdir()
        other.mkdir()
        (scanned / "mod.py").write_text("def fn(x):\n    return x\n")
        index_path = _build_index(scan_index, scanned)

        result = _run_query(scan_query, index_path, "--root", str(other), "central", cwd=scanned)

        assert result.returncode == 0, result.stderr + result.stdout
        cov = json.loads(result.stdout)["index"]
        assert cov["root_mismatch"] is True
        assert cov["query_complete"] is False
        assert "different project" in cov["note"]
        assert "differs from queried root" in result.stderr

    def test_matching_root_no_mismatch(self, project, scan_query):
        """Querying with ``--root`` equal to scan_root leaves root_mismatch false.

        Passing the project's own root explicitly must not raise the mismatch flag; it is the negative control for the
        mismatch test above.
        """
        root, index_path = project
        result = _run_query(scan_query, index_path, "--root", str(root), "central", cwd=root)
        assert result.returncode == 0, result.stderr + result.stdout
        cov = json.loads(result.stdout)["index"]
        assert cov["root_mismatch"] is False

    @pytest.mark.parametrize(
        ("env_rel", "index_rel"),
        [
            pytest.param({}, "selected-root/.cache/codemap/selected-root.json", id="default-cache-dir"),
            pytest.param({"CODEMAP_INDEX_DIR": "index-dir"}, "index-dir/selected-root.json", id="index-dir-override"),
        ],
    )
    def test_custom_root_index_is_selected_from_sibling_project(
        self, tmp_path, scan_index, scan_query, env_rel, index_rel
    ):
        """An emitted sibling-root index remains selectable from the invoking project.

        ``caller`` scans and queries the separate ``selected-root`` project, once with the default cache location and
        once with ``CODEMAP_INDEX_DIR`` redirecting the index; ``--root`` must keep both selectable and free of a root-
        mismatch flag.
        """
        caller = tmp_path / "caller"
        selected_root = tmp_path / "selected-root"
        caller.mkdir()
        selected_root.mkdir()
        (caller / "caller.py").write_text("VALUE = 'caller'\n")
        (selected_root / "inner.py").write_text("VALUE = 'selected'\n")
        scan_env = {k: v for k, v in os.environ.items() if k != "CODEMAP_INDEX_DIR"}
        scan_env.update({key: str(tmp_path / rel) for key, rel in env_rel.items()})
        selected_index = tmp_path / index_rel
        selected_scan = subprocess.run(
            [sys.executable, str(scan_index), "--root", str(selected_root)],
            capture_output=True,
            text=True,
            cwd=str(caller),
            env=scan_env,
        )
        assert selected_scan.returncode == 0, selected_scan.stderr
        assert selected_index.is_file()

        query_result = subprocess.run(
            [
                sys.executable,
                str(scan_query),
                "--index",
                str(selected_index),
                "--root",
                str(selected_root),
                "--no-heal",
                "rdeps",
                "inner",
            ],
            capture_output=True,
            text=True,
            cwd=str(caller),
            env=scan_env,
        )

        assert query_result.returncode == 0, query_result.stderr + query_result.stdout
        data = json.loads(query_result.stdout)
        assert data["module"] == "inner"
        assert data["index"]["index_path"] == str(selected_index)
        assert data["index"]["root_mismatch"] is False

    def test_index_guard_rejects_noncanonical_file_inside_explicit_sibling_root(self, tmp_path, scan_query):
        """An explicit external root admits its one derived index, not any file below it.

        Only the index file derived from the root's name is accepted; a differently named JSON under the same
        ``.cache/codemap`` is refused with exit 2.
        """
        caller = tmp_path / "caller"
        selected_root = tmp_path / "selected-root"
        caller.mkdir()
        selected_root.mkdir()
        unexpected = selected_root / ".cache" / "codemap" / "unrelated.json"
        unexpected.parent.mkdir(parents=True)
        unexpected.write_text(json.dumps({"scan_version": 11, "modules": [], "collisions": []}))

        result = _run_query(scan_query, unexpected, "--root", str(selected_root), "list", cwd=caller)

        assert result.returncode == 2
        assert json.loads(result.stdout)["error"] == "index path outside project root"

    def test_index_guard_rejection_emits_json(self, tmp_path, scan_query):
        """``--index`` pointing outside the project root yields a parseable JSON error on stdout, exit 2.

        The project is a git repo and the index lives outside it; the refusal is machine-readable so callers can react
        without parsing stderr.
        """
        root = tmp_path / "guarded"
        root.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=str(root), check=True)
        outside = tmp_path / "outside.json"
        outside.write_text(json.dumps({"scan_version": 10, "modules": []}))

        result = _run_query(scan_query, outside, "list", cwd=root)

        assert result.returncode == 2, result.stderr + result.stdout
        data = json.loads(result.stdout)
        assert data["error"] == "index path outside project root"

    def test_index_guard_rejects_nonmatching_file_in_configured_override(self, tmp_path, scan_query, monkeypatch):
        """Only the configured override's root-derived index filename may leave the project tree.

        ``CODEMAP_INDEX_DIR`` points at a directory holding ``other.json``; since that is not the root-derived filename,
        the guard still refuses it.
        """
        root = tmp_path / "guarded"
        root.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=str(root), check=True)
        override = tmp_path / "index-dir"
        override.mkdir()
        unexpected = override / "other.json"
        unexpected.write_text(json.dumps({"scan_version": 11, "modules": [], "collisions": []}))
        monkeypatch.setenv("CODEMAP_INDEX_DIR", str(override))

        result = _run_query(scan_query, unexpected, "--root", str(root), "list", cwd=root)

        assert result.returncode == 2
        assert json.loads(result.stdout)["error"] == "index path outside project root"


# ── list ``--limit`` cap + total/shown disclosure ──────────────────────────────


class TestListLimit:
    """Honor listing limits while reporting both total and displayed counts."""

    def test_list_default_reports_total_and_shown(self, query):
        """Default list emits total and shown counts alongside the module list.

        Without ``--limit`` every module is listed, so ``total`` and ``shown`` both equal the length of the returned
        list.
        """
        data = query("list")
        assert data["total"] == len(data["modules"])
        assert data["shown"] == len(data["modules"])

    def test_list_limit_caps_modules(self, query):
        """``--limit N`` returns at most N modules while total reflects the full, uncapped count.

        With ``--limit 2`` only two modules are returned and ``shown`` is 2, while ``total`` keeps counting all five-
        plus fixture modules.
        """
        data = query("list", "--limit", "2")
        assert len(data["modules"]) == 2
        assert data["shown"] == 2
        assert data["total"] >= 5  # fixture has 5+ modules; total is uncapped

    def test_list_limit_zero_returns_all(self, query):
        """``--limit 0`` disables the cap, so shown equals total.

        ``--limit 0`` removes the cap, so ``shown`` equals ``total`` and the list holds every module.
        """
        data = query("list", "--limit", "0")
        assert data["shown"] == data["total"]
        assert len(data["modules"]) == data["total"]


# ── session-scoped coverage diet ───────────────────────────────────────────


def _build_diet_repo(root: Path, scan_index: Path) -> Path:
    """Git-init *root*, write one module, scan it, return the index path.

    The diet reader resolves Claude's marker under ``<git-root>/.cache/codemap``, so the test tree must be a real git
    repo for the marker path to match.
    """
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=True)
    (root / "modx.py").write_text("def fx(x):\n    return x\n")
    return _build_index(scan_index, root)


def _write_marker(root: Path, session_id: str) -> None:
    """Write the hook-owned session marker matching the cross-agent contract."""
    marker = root / ".cache" / "codemap" / "current-session-claude.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"session_id": session_id, "ts": int(time.time() * 1000)}))


def _run_coverage_query(scan_query: Path, root: Path, index_path: Path, *extra: str) -> dict:
    """Run ``central --top 1`` and return its ``index`` coverage block."""
    result = subprocess.run(
        [sys.executable, str(scan_query), "--index", str(index_path), *extra, "central", "--top", "1"],
        capture_output=True,
        text=True,
        cwd=str(root),
        env={**os.environ, "CODEMAP_LOGGING": "false", "CODEMAP_RUNTIME": "claude"},
    )
    assert result.returncode == 0, result.stderr + result.stdout
    return json.loads(result.stdout)["index"]


class TestCoverageDiet:
    """First query per session emits the full block; subsequent emit a compact one."""

    @pytest.fixture(name="diet_session")
    def _diet_session(self, tmp_path, scan_index):
        """Git repo with a scanned module and a session marker; the per-session sentinel is removed afterwards."""
        index_path = _build_diet_repo(tmp_path, scan_index)
        session_id = f"diet-{tmp_path.name}-{uuid.uuid4().hex[:8]}"
        _write_marker(tmp_path, session_id)
        sentinel = Path(tempfile.gettempdir()) / f"codemap-coverage-{session_id}"
        sentinel.unlink(missing_ok=True)
        yield index_path
        sentinel.unlink(missing_ok=True)

    def test_first_query_full_then_second_compact(self, tmp_path, scan_query, diet_session):
        """Two sequential same-session queries: first full, second compact.

        The first query consumes the per-session sentinel and carries the full coverage block; the second carries only
        the compact block, which must still keep the per-query honesty signals.
        """
        first = _run_coverage_query(scan_query, tmp_path, diet_session)
        second = _run_coverage_query(scan_query, tmp_path, diet_session)
        assert not first.get("compact"), "first query must emit the full block"
        assert "total_modules" in first
        assert second.get("compact") is True, "second query must emit the compact block"
        assert "total_modules" not in second
        # Per-query honesty signals survive the diet.
        assert "query_complete" in second
        assert "stale" in second
        assert "root_mismatch" in second

    def test_missing_marker_stays_verbose(self, tmp_path, scan_index, scan_query):
        """No session marker → every query emits the full block (fail-verbose).

        A git repo without the hook-written session marker has no session to track, so both queries stay full rather
        than risking a compact block without a preceding full one.
        """
        index_path = _build_diet_repo(tmp_path, scan_index)
        first = _run_coverage_query(scan_query, tmp_path, index_path)
        second = _run_coverage_query(scan_query, tmp_path, index_path)
        assert not first.get("compact")
        assert not second.get("compact"), "without a marker the diet must never engage"

    def test_verbose_coverage_flag_forces_full(self, tmp_path, scan_query, diet_session):
        """``--verbose-coverage`` restores the full block even after the first query.

        An earlier same-session query consumes the sentinel, so without the flag the next answer would be compact.
        """
        _run_coverage_query(scan_query, tmp_path, diet_session)  # consumes the sentinel

        forced = _run_coverage_query(scan_query, tmp_path, diet_session, "--verbose-coverage")

        assert not forced.get("compact")
        assert "total_modules" in forced


# ── batch subcommand ───────────────────────────────────────────────────────


def _run_batch_raw(
    scan_query: Path, root: Path, index_path: Path, *args: str, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run scan-query with *args* (optionally feeding *stdin*) from *root*, logging off."""
    return subprocess.run(
        [sys.executable, str(scan_query), "--index", str(index_path), *args],
        input=stdin,
        capture_output=True,
        text=True,
        cwd=str(root),
        env={**os.environ, "CODEMAP_LOGGING": "false"},
    )


def _run_batch(scan_query: Path, root: Path, index_path: Path, items: list, *, compact: bool = False) -> dict:
    """Run `batch` feeding *items* via stdin; optionally compact coverage metadata."""
    flags = ["--compact"] if compact else []
    result = _run_batch_raw(scan_query, root, index_path, *flags, "batch", "-", stdin=json.dumps(items))
    assert result.returncode == 0, result.stderr + result.stdout
    return json.loads(result.stdout)


class TestBatch:
    """Share one process while preserving per-query coverage and failure evidence."""

    @pytest.mark.parametrize(
        "items",
        [pytest.param([], id="empty"), pytest.param([{"cmd": "not-a-command"}], id="all-failed")],
    )
    def test_batch_without_coverage_has_explicit_completion(self, project, scan_query, items):
        """Zero returned coverage blocks must not leave aggregate success ambiguous.

        An empty batch and an all-failed batch return no coverage blocks, so the aggregate states its own
        completeness: complete for empty, partial when items failed.
        """
        root, index_path = project
        batch = _run_batch(scan_query, root, index_path, items)
        assert batch["index"] == {
            "query_complete": not items,
            "truncated": False,
            "confidence": "partial" if items else "exact",
        }

    @pytest.mark.parametrize(
        ("items", "ok_args"),
        [
            pytest.param(
                [
                    {"cmd": "rdeps", "args": ["gamma"]},
                    {"cmd": "rdeps", "args": ["gamma", "--limit", "1"]},
                    {"cmd": "deps", "args": ["missing.module"]},
                ],
                [["gamma"], ["gamma", "--limit", "1"]],
                id="failure-last",
            ),
            pytest.param(
                [
                    {"cmd": "deps", "args": ["missing.module"]},
                    {"cmd": "rdeps", "args": ["gamma", "--limit", "1"]},
                    {"cmd": "rdeps", "args": ["gamma"]},
                ],
                [["gamma", "--limit", "1"], ["gamma"]],
                id="failure-first",
            ),
        ],
    )
    def test_batch_keeps_each_query_completeness(self, project, scan_query, query, items, ok_args):
        """A complete sibling must not erase a capped result's limits or a failed query.

        The batch mixes a complete ``rdeps``, a ``--limit 1`` preview and a query for a missing module, in both orders:
        each successful entry equals its standalone answer, only the preview is truncated, and the aggregate block stays
        partial rather than being promoted to complete by the healthy sibling.
        """
        root, index_path = project

        batch = _run_batch(scan_query, root, index_path, items)

        ok_entries = [entry for entry in batch["batch"] if entry["ok"]]
        failed = [entry for entry in batch["batch"] if not entry["ok"]]
        truncated = [entry["result"]["index"] for entry in ok_entries if entry["result"]["index"].get("truncated")]
        assert [entry["result"] for entry in ok_entries] == [query("rdeps", *args) for args in ok_args]
        assert all(entry["result"]["index"]["query_complete"] is True for entry in ok_entries)
        assert len(failed) == 1
        assert "error" in failed[0]
        assert len(truncated) == 1
        assert truncated[0]["total_available"] == 2
        assert truncated[0]["confidence"] == "partial"
        assert batch["index"]["query_complete"] is False
        assert batch["index"]["truncated"] is True
        assert batch["index"]["confidence"] == "partial"
        assert batch["index"].get("exhaustive") is not True
        assert "completeness_reason" not in batch["index"]
        assert "note" not in batch["index"]
        assert "total_available" not in batch["index"]

    def test_batch_compact_keeps_truncation_and_failed_item_evidence(self, project, scan_query):
        """Compact coverage cannot promote a capped or failed sibling to complete.

        ``--compact`` replaces the full coverage block with a short one; a ``--limit 1`` preview must still say
        truncated with ``total_available`` 2, and the failed sibling keeps the aggregate incomplete.
        """
        root, index_path = project
        items = [
            {"cmd": "rdeps", "args": ["gamma", "--limit", "1"]},
            {"cmd": "deps", "args": ["missing.module"]},
        ]

        batch = _run_batch(scan_query, root, index_path, items, compact=True)

        capped = batch["batch"][0]
        assert capped["ok"] is True
        assert (
            capped["result"]["index"].items()
            >= {
                "query_complete": True,
                "stale": False,
                "root_mismatch": False,
                "compact": True,
                "method": "import-graph",
                "not_covered": ["importlib.import_module", "__import__", "lazy-loading"],
                "confidence": "partial",
                "truncated": True,
                "total_available": 2,
            }.items()
        )
        assert batch["batch"][1]["ok"] is False
        assert "error" in batch["batch"][1]
        assert batch["index"].items() >= {"query_complete": False, "truncated": True, "confidence": "partial"}.items()

    def test_batch_matches_individual_results(self, project, scan_query, query):
        """A batch of four mixed queries preserves each complete standalone result.

        ``deps``, ``rdeps``, ``central`` and a limited ``list`` run in one process; every entry must succeed and equal
        what the same query returns on its own, in input order.
        """
        root, index_path = project
        items = [
            {"cmd": "deps", "args": ["alpha"]},
            {"cmd": "rdeps", "args": ["gamma"]},
            {"cmd": "central", "args": ["--top", "3"]},
            {"cmd": "list", "args": ["--limit", "2"]},
        ]

        batch = _run_batch(scan_query, root, index_path, items)

        assert batch["count"] == 4
        assert all(entry["ok"] is True for entry in batch["batch"])
        assert [entry["result"] for entry in batch["batch"]] == [query(item["cmd"], *item["args"]) for item in items]
        assert "index" in batch

    def test_batch_preserves_input_order(self, project, scan_query):
        """Results are keyed by input order via the ``index`` field.

        Two ``deps`` items come back with ``index`` 0 and 1 in the order sent, so callers can pair answers to requests.
        """
        root, index_path = project
        items = [{"cmd": "deps", "args": ["alpha"]}, {"cmd": "deps", "args": ["beta"]}]
        batch = _run_batch(scan_query, root, index_path, items)
        assert [e["index"] for e in batch["batch"]] == [0, 1]
        assert batch["batch"][0]["result"]["module"] == "alpha"
        assert batch["batch"][1]["result"]["module"] == "beta"

    def test_batch_failing_item_does_not_kill_batch(self, project, scan_query):
        """A failing query yields a per-item error object; sibling queries still succeed.

        The first item names a non-indexed module; it yields a per-item failure and the second item still succeeds.
        """
        root, index_path = project
        items = [{"cmd": "deps", "args": ["nonexistent.module.xyz"]}, {"cmd": "deps", "args": ["alpha"]}]
        batch = _run_batch(scan_query, root, index_path, items)
        assert batch["batch"][0]["ok"] is False
        assert batch["batch"][1]["ok"] is True
        assert batch["batch"][1]["result"]["module"] == "alpha"

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param({"cmd": "not-a-command"}, id="unknown-subcommand"),
            pytest.param({"cmd": "batch"}, id="nested-batch"),
        ],
    )
    def test_batch_invalid_command_is_per_item_error(self, project, scan_query, command):
        """An unknown subcommand or a nested ``batch`` surfaces as a per-item error, not a batch abort.

        An unknown subcommand and a nested ``batch`` each produce an error entry for that item while the rest of the
        batch is processed.
        """
        root, index_path = project
        batch = _run_batch(scan_query, root, index_path, [command])
        assert batch["batch"][0]["ok"] is False
        assert "error" in batch["batch"][0]

    def test_batch_non_array_input_exits_bad_input(self, project, scan_query):
        """A non-array top-level JSON value is a bad-input error (exit 2).

        ``batch`` expects a JSON array; an object on stdin exits 2 with an error object.
        """
        root, index_path = project
        result = _run_batch_raw(scan_query, root, index_path, "batch", "-", stdin=json.dumps({"cmd": "list"}))
        assert result.returncode == 2, result.stderr + result.stdout
        assert "error" in json.loads(result.stdout)

    def test_batch_accepts_the_json_array_as_the_argument(self, project, scan_query):
        """Passing the request array inline works the same as feeding it on stdin.

        A caller who writes the array straight onto the command line rather than saving it to a file used to have its
        own JSON reported as a missing filename, which reads as a broken command rather than a wrong argument form — so
        the caller falls back to one process per query.
        """
        root, index_path = project
        items = [{"cmd": "deps", "args": ["alpha"]}, {"cmd": "deps", "args": ["beta"]}]
        result = _run_batch_raw(scan_query, root, index_path, "batch", json.dumps(items))
        assert result.returncode == 0, result.stderr + result.stdout
        assert [entry["result"]["module"] for entry in json.loads(result.stdout)["batch"]] == ["alpha", "beta"]

    def test_batch_unreadable_path_error_names_every_accepted_form(self, project, scan_query):
        """An unreadable path reports what batch does accept, not only the filesystem failure.

        The bare errno told a caller that its argument was not a file without hinting that an inline array or stdin
        would have been taken, leaving the working form undiscoverable from the error.
        """
        root, index_path = project
        result = _run_batch_raw(scan_query, root, index_path, "batch", "no-such-batch-file.json")
        assert result.returncode == 2, result.stderr + result.stdout
        payload = json.loads(result.stdout)
        assert payload["error"] == "batch input unreadable"
        assert payload["accepts"] == "a JSON array, a path to a JSON file, or '-' for stdin"


class TestArgvHardening:
    """Malformed caller argv fails fast with an actionable message (2026-07 audit F1)."""

    @pytest.mark.parametrize(
        ("args", "expected_error_fragments"),
        [
            pytest.param(
                ("rdeps", "alpha\nbeta\ngamma"),
                ("3 names passed as ONE argument", "batch"),
                id="newline-joined-names-name-the-shell-cause",
            ),
            pytest.param(
                ("fn-rdeps", "gamma"),
                ("'gamma' is a module, not a function qname", "rdeps gamma"),
                id="module-given-to-fn-command-redirects-to-rdeps",
            ),
            pytest.param(
                ("fn-rdeps", "gamma::nope"),
                ("find-symbol",),
                id="unknown-symbol-keeps-generic-find-symbol-pointer",
            ),
        ],
    )
    def test_malformed_argv_exits_with_actionable_error(self, project, scan_query, args, expected_error_fragments):
        """Malformed caller argv exits 1 with an error that names the mistake and the working alternative.

        A newline-joined name list in ONE argument names the shell cause; ``fn-rdeps`` on a bare module points at
        ``rdeps``; a genuinely unknown symbol still gets the ``find-symbol`` pointer.
        """
        root, index_path = project

        result = _run_query(scan_query, index_path, *args, cwd=root)

        assert result.returncode == 1
        assert all(fragment in json.loads(result.stdout)["error"] for fragment in expected_error_fragments)


class TestInvalidCommandSuggestions:
    """Unknown public CLI commands receive only their explicit migration hint."""

    @pytest.mark.parametrize(
        ("command", "hint"),
        [
            pytest.param("search", "Hint: use 'find-symbol' to search symbols.", id="search"),
            pytest.param("callers", "Hint: use 'fn-rdeps' for function callers.", id="callers"),
            pytest.param("find-references", "Hint: use 'fn-rdeps' for function callers.", id="find-references"),
            pytest.param("imports", "Hint: use 'rdeps' for importers or 'deps' for imports.", id="imports"),
            pytest.param("help", "Hint: use '--help' to list commands.", id="help"),
        ],
    )
    def test_known_invalid_command_gets_actionable_hint(self, project, scan_query, command, hint):
        """Each observed invalid command exits normally while naming its canonical replacement.

        Each of five command names that callers have been seen to invent is rejected with exit 2 and a hint naming the
        real command.
        """
        root, index_path = project
        result = _run_query(scan_query, index_path, command, cwd=root)

        assert result.returncode == 2
        assert hint in result.stderr

    def test_unrelated_invalid_command_does_not_get_a_false_hint(self, project, scan_query):
        """Only observed invalid command names receive migration guidance.

        ``not-a-command`` is not in the migration table, so it is rejected with exit 2 and no ``Hint:`` line.
        """
        root, index_path = project
        result = _run_query(scan_query, index_path, "not-a-command", cwd=root)

        assert result.returncode == 2
        assert "Hint:" not in result.stderr

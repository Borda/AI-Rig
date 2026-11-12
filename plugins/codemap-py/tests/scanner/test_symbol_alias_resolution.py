"""Regression coverage for persisted, static symbol re-export aliases.

The call scanner faithfully records the spelling visible to a consumer.  Package ``__init__`` files can re-export that
spelling one or more times, however, so the reverse graph must normalize only aliases proven by a top-level
``ImportFrom``. These tests exercise the public index/query boundary and do not reuse scanner resolution helpers as an
oracle.
"""

from __future__ import annotations

import copy
import functools
import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from _scan_helpers import IGNORE_RUNNER_THREAD_FORK_WARNING
from codemap_py.graph import incremental_scan, scan
from codemap_py.query import _COMPACT_ALIAS_LIMITATION_LIMIT
from codemap_py.schema import SCAN_VERSION

_BIN = Path(__file__).parents[2] / "bin"

_SOURCES = {
    "pkg/__init__.py": "from .api import target\n",
    "pkg/impl.py": "def target():\n    return 1\n",
    "pkg/api.py": "from .impl import target\n",
    "pkg/consumer.py": "from pkg import target\n\n\ndef use():\n    return target()\n",
    "pkg/external.py": "from external_lib.api import external_target\n",
    "pkg/external_consumer.py": (
        "from .external import external_target\n\n\ndef use_external():\n    return external_target()\n"
    ),
    "pkg/cycle_a.py": "from .cycle_b import cycle_target\n",
    "pkg/cycle_b.py": "from .cycle_a import cycle_target\n",
    "pkg/cycle_consumer.py": ("from .cycle_a import cycle_target\n\n\ndef use_cycle():\n    return cycle_target()\n"),
    "pkg/relative_consumer.py": "from . import api\nfrom .api import target\n",
    "pkg/parent_consumer.py": "from pkg import api\n",
}

_AMBIGUOUS_SOURCES = {
    "pkg/conditional_import.py": "if enabled:\n    from .impl import target\n\n\ndef use():\n    return target()\n",
    "pkg/conditional_function.py": "from .impl import target\nif enabled:\n    def target():\n        return 2\n",
    "pkg/conditional_class.py": "from .impl import target\nif enabled:\n    class target:\n        pass\n",
    "pkg/conditional_import_binding.py": "from .impl import target\nif enabled:\n    import other as target\n",
    "pkg/conditional_for.py": "from .impl import target\nif enabled:\n    for target in candidates:\n        pass\n",
    "pkg/conditional_with.py": "from .impl import target\nif enabled:\n    with resource() as target:\n        pass\n",
    "pkg/rebound.py": "from .impl import target\ntarget = lambda: 2\n\n\ndef use_rebound():\n    return target()\n",
}


@dataclass(frozen=True)
class _Scanned:
    """A scanned alias project: its root, the persisted index file and the in-memory index."""

    root: Path
    index_path: Path
    index: dict

    def query(self, *args: str) -> dict:
        """Run scan-query against this project's index and return its JSON payload."""
        result = subprocess.run(
            [sys.executable, str(_BIN / "scan-query"), "--index", str(self.index_path), *args],
            cwd=self.root,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr + result.stdout
        return json.loads(result.stdout)


def _write_project(root: Path, *, ambiguous: bool = False) -> None:
    """Materialize the static alias fixture under *root*."""
    sources = {**_SOURCES, **(_AMBIGUOUS_SOURCES if ambiguous else {})}
    for relative, source in sources.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)


def _add_ambiguous_aliases(root: Path, count: int) -> None:
    """Add *count* conditional imports that each persist one target limitation."""
    for number in range(count):
        (root / "pkg" / f"ambiguous_{number:03}.py").write_text("if enabled:\n    from .impl import target\n")


def _scan_project(root: Path, *, ambiguous: bool = False, extra_ambiguous: int = 0) -> _Scanned:
    """Write the alias project under *root*, scan it and persist the index where scan-query can read it."""
    _write_project(root, ambiguous=ambiguous)
    _add_ambiguous_aliases(root, extra_ambiguous)
    index = scan(root)
    index_path = root / "index.json"
    index_path.write_text(json.dumps(index))
    return _Scanned(root, index_path, index)


def _downgrade_to_v11(index: dict) -> dict:
    """Strip every v13-only alias key from *index* and stamp it as a v11 index."""
    index["scan_version"] = 11
    index.pop("symbol_aliases")
    index.pop("symbol_alias_limitations")
    for module in index["modules"]:
        module.pop("symbol_aliases", None)
        module.pop("symbol_alias_limitations", None)
    return index


@pytest.fixture(name="clean_project", scope="module")
def _clean_project(tmp_path_factory: pytest.TempPathFactory) -> _Scanned:
    """Alias project with only proven re-exports, scanned once; tests only query it."""
    return _scan_project(tmp_path_factory.mktemp("clean"))


@pytest.fixture(name="ambiguous_project", scope="module")
def _ambiguous_project(tmp_path_factory: pytest.TempPathFactory) -> _Scanned:
    """Alias project that also holds conditional and rebound aliases, scanned once; tests only query it."""
    return _scan_project(tmp_path_factory.mktemp("ambiguous"), ambiguous=True)


@pytest.fixture(name="compact_central", scope="module")
def _compact_central(tmp_path_factory: pytest.TempPathFactory) -> Callable[[int], tuple[dict, _Scanned]]:
    """Return a memoized factory: scan a project with N ambiguous aliases and run compact ``fn-central`` on it."""

    @functools.cache
    def _build(limitation_count: int) -> tuple[dict, _Scanned]:
        project = _scan_project(tmp_path_factory.mktemp(f"central{limitation_count}"), extra_ambiguous=limitation_count)
        return project.query("--compact", "fn-central", "--top", "5"), project

    return _build


class TestReexportAliasQueries:
    """Reverse queries resolve proven package re-exports to the implementation symbol."""

    @IGNORE_RUNNER_THREAD_FORK_WARNING
    def test_reexport_aliases_canonicalize_reverse_call_queries(self, clean_project: _Scanned) -> None:
        """One- and two-hop package re-exports resolve to the implementation symbol.

        ``pkg.consumer`` calls ``target`` through ``pkg`` (re-exporting ``pkg.api``, re-exporting ``pkg.impl``); asking
        for callers of the public alias must give the same answer as asking for the implementation.
        """
        direct = clean_project.query("fn-rdeps", "pkg.impl::target")
        public = clean_project.query("fn-rdeps", "pkg::target")

        assert {entry["caller"] for entry in direct["called_by"]} == {"pkg.consumer::use"}
        assert public["called_by"] == direct["called_by"]
        assert direct["index"]["query_complete"] is True

    @IGNORE_RUNNER_THREAD_FORK_WARNING
    def test_external_terminal_alias_is_queryable_when_proven_by_import(self, clean_project: _Scanned) -> None:
        """A re-export terminating outside the scanned tree still has inbound callers.

        ``pkg.external`` re-exports a name from an unscanned library; its consumer's call must still be found by asking
        for callers of the re-exporting module's symbol.
        """
        data = clean_project.query("fn-rdeps", "pkg.external::external_target")

        assert {entry["caller"] for entry in data["called_by"]} == {"pkg.external_consumer::use_external"}
        assert data["index"]["query_complete"] is True

    @IGNORE_RUNNER_THREAD_FORK_WARNING
    def test_reverse_imports_include_resolved_relative_and_parent_package_submodules(
        self, clean_project: _Scanned
    ) -> None:
        """Reverse imports retain the parent edge and known submodule edges.

        ``from . import api`` and ``from pkg import api`` import a submodule through its package; ``rdeps`` must list
        those consumers for the submodule as well as for the package.
        """
        package = clean_project.query("rdeps", "pkg")
        api = clean_project.query("rdeps", "pkg.api")

        assert set(package["imported_by"]) == {"pkg.consumer", "pkg.parent_consumer", "pkg.relative_consumer"}
        assert set(api["imported_by"]) == {"pkg", "pkg.parent_consumer", "pkg.relative_consumer"}

    @IGNORE_RUNNER_THREAD_FORK_WARNING
    def test_clean_proven_alias_remains_complete_without_ambiguous_paths(self, clean_project: _Scanned) -> None:
        """A DI-style direct re-export is complete when no rejected alias targets it.

        With no conditional or rebound alias in the project every alias query, including the whole-graph ones, must
        claim a complete answer.
        """
        data = clean_project.query("fn-rdeps", "pkg.impl::target")
        central = clean_project.query("fn-central", "--top", "5")
        blast = clean_project.query("fn-blast", "pkg.impl::target")

        assert data["index"]["query_complete"] is True
        assert data["index"]["completeness_reason"] == "ok"
        assert central["index"]["query_complete"] is True
        assert blast["index"]["query_complete"] is True


class TestAmbiguousAliasCompleteness:
    """Conditional and rebound aliases veto completeness only for the queries they can affect."""

    @IGNORE_RUNNER_THREAD_FORK_WARNING
    def test_ambiguous_alias_paths_veto_target_completeness(self, ambiguous_project: _Scanned) -> None:
        """Known dynamic/rebound aliases make only their target query incomplete.

        Conditional imports, definitions and rebinding of ``target`` cannot be proven statically: the target query must
        report which reasons apply, while unproven or cyclic aliases are simply never persisted.
        """
        data = ambiguous_project.query("fn-rdeps", "pkg.impl::target")

        aliases = ambiguous_project.index["symbol_aliases"]
        assert {entry["caller"] for entry in data["called_by"]} == {"pkg.consumer::use"}
        assert data["index"]["query_complete"] is False
        assert data["index"]["completeness_reason"] == "symbol_alias_ambiguous"
        assert {entry["reason"] for entry in data["index"]["symbol_alias_limitations"]} == {
            "conditional_binding",
            "conditional_import",
            "top_level_rebinding",
        }
        assert "pkg.cycle_a::cycle_target" not in aliases
        assert "pkg.cycle_b::cycle_target" not in aliases
        assert "pkg.rebound::target" not in aliases
        assert "pkg.conditional_import::target" not in aliases

    @pytest.mark.parametrize(
        ("query", "args"),
        [
            pytest.param("fn-central", ("--top", "5"), id="central"),
            pytest.param("fn-blast", ("pkg.impl::target",), id="target-blast"),
        ],
    )
    @IGNORE_RUNNER_THREAD_FORK_WARNING
    def test_alias_limitations_veto_central_and_target_blast_queries(
        self, ambiguous_project: _Scanned, query: str, args: tuple[str, ...]
    ) -> None:
        """Centrality is whole-graph, while blast only sees its queried target's limits.

        Both queries must degrade to an incomplete answer naming the alias ambiguity when the project holds one.
        """
        data = ambiguous_project.query(query, *args)

        assert data["index"]["query_complete"] is False
        assert data["index"]["completeness_reason"] == "symbol_alias_ambiguous"
        assert data["index"]["symbol_alias_limitations"]


class TestCompactAliasLimitations:
    """``--compact`` output bounds alias-limitation evidence while the full output keeps every record."""

    @IGNORE_RUNNER_THREAD_FORK_WARNING
    def test_compact_central_omits_alias_limitations_when_none(self, compact_central) -> None:
        """Compact centrality with zero ambiguous aliases stays fully complete.

        A clean project has nothing to report, so the compact index block must not grow an empty limitations key.
        """
        compact, _ = compact_central(0)

        assert compact["index"]["query_complete"] is True
        assert "symbol_alias_limitations" not in compact["index"]

    @pytest.mark.parametrize(
        ("limitation_count", "truncated"),
        [
            pytest.param(_COMPACT_ALIAS_LIMITATION_LIMIT, False, id="at-limit"),
            pytest.param(128, True, id="above-limit"),
        ],
    )
    @IGNORE_RUNNER_THREAD_FORK_WARNING
    def test_compact_central_bounds_alias_limitations_without_losing_full_output(
        self, compact_central, limitation_count: int, truncated: bool
    ) -> None:
        """Compact centrality samples ambiguous aliases; full output retains every record.

        At the limit nothing is cut; above it the compact view keeps a bounded sample plus the true total, and the non-
        compact query still returns every persisted record sorted by alias, target and reason.
        """
        compact, project = compact_central(limitation_count)
        compact_index = compact["index"]

        full = project.query("fn-central", "--top", "5")

        assert compact_index["query_complete"] is False
        assert compact_index["completeness_reason"] == "symbol_alias_ambiguous"
        assert len(compact_index["symbol_alias_limitations"]) == min(limitation_count, _COMPACT_ALIAS_LIMITATION_LIMIT)
        assert compact_index["symbol_alias_limitations_total"] == limitation_count
        assert compact_index["symbol_alias_limitations_truncated"] is truncated
        expected_records = sorted(
            project.index["symbol_alias_limitations"],
            key=lambda record: (record["alias_qname"], record["target_qname"], record["reason"]),
        )
        assert full["index"]["symbol_alias_limitations"] == expected_records
        assert "symbol_alias_limitations_total" not in full["index"]
        assert "symbol_alias_limitations_truncated" not in full["index"]

    @IGNORE_RUNNER_THREAD_FORK_WARNING
    def test_compact_central_truncated_response_hints_and_shrinks_payload(self, compact_central) -> None:
        """A truncated compact response carries a hint and stays smaller than the full payload.

        The hint tells an agent that evidence was cut and how to get all of it; the point of truncating is a smaller
        response, which is checked on the serialized size.
        """
        compact, project = compact_central(128)

        full = project.query("fn-central", "--top", "5")

        assert "symbol_alias_limitations_hint" in compact["index"]
        assert "Run without --compact" in compact["index"]["note"]
        assert len(json.dumps(compact, separators=(",", ":"))) < len(json.dumps(full, separators=(",", ":")))

    @pytest.mark.parametrize(
        ("command", "result_key"),
        [pytest.param("fn-rdeps", "called_by", id="fn-rdeps"), pytest.param("fn-blast", "blast_radius", id="fn-blast")],
    )
    @IGNORE_RUNNER_THREAD_FORK_WARNING
    def test_target_queries_keep_small_relevant_evidence(
        self, ambiguous_project: _Scanned, command: str, result_key: str
    ) -> None:
        """Target-specific compact results keep all small, actionable ambiguity evidence.

        The handful of limitations that concern the queried target is below the sample limit, so nothing is truncated
        and no hint is emitted.
        """
        data = ambiguous_project.query("--compact", command, "pkg.impl::target")

        coverage = data["index"]
        assert data[result_key]
        assert coverage["query_complete"] is False
        assert coverage["completeness_reason"] == "symbol_alias_ambiguous"
        assert 0 < len(coverage["symbol_alias_limitations"]) <= _COMPACT_ALIAS_LIMITATION_LIMIT
        assert coverage["symbol_alias_limitations_total"] == len(coverage["symbol_alias_limitations"])
        assert coverage["symbol_alias_limitations_truncated"] is False
        assert "symbol_alias_limitations_hint" not in coverage


class TestIncrementalAliasScan:
    """Incremental scans keep alias and module data identical to a fresh full scan."""

    @IGNORE_RUNNER_THREAD_FORK_WARNING
    def test_incremental_alias_index_matches_fresh_full_scan(self, tmp_path: Path) -> None:
        """Incremental rebuild recomputes alias chains after a package re-export changes.

        The package ``__init__`` stops re-exporting ``target`` under its old spelling; aliases, limitations and modules
        from ``incremental_scan`` must equal a from-scratch scan of the same tree.
        """
        _write_project(tmp_path, ambiguous=True)
        old_index = scan(tmp_path)
        (tmp_path / "pkg" / "__init__.py").write_text("from .api import target as public_target\n")

        incremental = incremental_scan(tmp_path, old_index)
        full = scan(tmp_path)

        assert incremental["symbol_aliases"] == full["symbol_aliases"]
        assert incremental["symbol_alias_limitations"] == full["symbol_alias_limitations"]
        assert incremental["modules"] == full["modules"]

    @IGNORE_RUNNER_THREAD_FORK_WARNING
    def test_submodule_add_and_remove_matches_fresh_scan(self, tmp_path: Path) -> None:
        """Unchanged from-import candidates are re-resolved when submodules appear or disappear.

        ``from pkg import child`` is a submodule import only while ``pkg/child.py`` exists; the unchanged consumer must
        gain and later lose the ``pkg.child`` edge across two incremental scans, matching a fresh scan each time.
        """
        package = tmp_path / "pkg"
        package.mkdir()
        (package / "__init__.py").write_text("")
        (package / "consumer.py").write_text("from pkg import child\n")
        initial = scan(tmp_path)

        (package / "child.py").write_text("")
        after_add = incremental_scan(tmp_path, initial)
        fresh_after_add = scan(tmp_path)
        (package / "child.py").unlink()
        after_remove = incremental_scan(tmp_path, copy.deepcopy(after_add))
        fresh_after_remove = scan(tmp_path)

        assert after_add["modules"] == fresh_after_add["modules"]
        assert after_remove["modules"] == fresh_after_remove["modules"]
        imports_after_add = next(m for m in after_add["modules"] if m["name"] == "pkg.consumer")["direct_imports"]
        imports_after_remove = next(m for m in after_remove["modules"] if m["name"] == "pkg.consumer")["direct_imports"]
        assert (imports_after_add, imports_after_remove) == (["pkg", "pkg.child"], ["pkg"])

    @IGNORE_RUNNER_THREAD_FORK_WARNING
    def test_markdown_change_refreshes_xrefs_without_creating_a_module(self, tmp_path: Path) -> None:
        """A docs-only xref update must not enter the Python module/degradation set.

        Editing a Markdown page re-reads its cross references, but the page must never be indexed as a (degraded) Python
        module.
        """
        (tmp_path / "pkg.py").write_text("def public() -> int:\n    return 1\n")
        docs = tmp_path / "docs"
        docs.mkdir()
        reference = docs / "reference.md"
        reference.write_text("# Reference\n\n[`pkg.public`][]\n")
        initial = scan(tmp_path)

        reference.write_text("# Reference\n\n[`pkg.public`][]\n\nA plain-language — update.\n")
        incremental = incremental_scan(tmp_path, initial)

        assert incremental["file_shas"]["docs/reference.md"] != initial["file_shas"]["docs/reference.md"]
        assert incremental["doc_xrefs"] == [
            {"role": "mkdocs", "target": "pkg::public", "file": "docs/reference.md", "line": 3, "source": "mkdocs"}
        ]
        assert {module["path"] for module in incremental["modules"]} == {"pkg.py"}
        assert all(module["status"] == "ok" for module in incremental["modules"])

    @IGNORE_RUNNER_THREAD_FORK_WARNING
    def test_removes_legacy_markdown_entry_but_preserves_python_degradation(self, tmp_path: Path) -> None:
        """An incremental repair drops impossible docs modules without hiding a real broken Python source.

        An older index wrongly listed a Markdown file as a degraded Python module; repairing it must remove that entry
        yet keep the genuinely broken ``broken.py`` flagged as degraded.
        """
        (tmp_path / "healthy.py").write_text("VALUE = 1\n")
        (tmp_path / "broken.py").write_text("def broken(:\n")
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "guide.md").write_text("# Guide\n\nNormal Markdown.\n")
        contaminated = scan(tmp_path)
        contaminated["modules"].append(
            {
                "name": "docs.guide",
                "path": "docs/guide.md",
                "status": "degraded",
                "reason": "legacy Markdown parsed as Python",
            }
        )
        (tmp_path / "healthy.py").write_text("VALUE = 2\n")

        repaired = incremental_scan(tmp_path, contaminated)

        assert {module["path"] for module in repaired["modules"]} == {"broken.py", "healthy.py"}
        assert next(module for module in repaired["modules"] if module["path"] == "broken.py")["status"] == "degraded"
        assert not any(module["path"].endswith(".md") for module in repaired["modules"])

    @IGNORE_RUNNER_THREAD_FORK_WARNING
    def test_cli_rebuilds_v11_alias_index_to_v14(self, tmp_path: Path, scan_index: Path) -> None:
        """The scan-index entrypoint fully rebuilds persisted v11 data before serving aliases.

        A v11 index has no alias data, so ``--incremental`` must announce a fallback to a full scan and write a v14
        index whose alias map and limitations are populated.
        """
        _write_project(tmp_path, ambiguous=True)
        index_path = tmp_path / ".cache" / "codemap" / f"{tmp_path.name}.json"
        index_path.parent.mkdir(parents=True)
        index_path.write_text(json.dumps(_downgrade_to_v11(scan(tmp_path))))

        result = subprocess.run(
            [sys.executable, str(scan_index), "--root", str(tmp_path), "--incremental"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr
        assert f"pre-v{SCAN_VERSION} index found — falling back to full scan" in result.stderr
        rebuilt = json.loads(index_path.read_text())
        assert rebuilt["scan_version"] == SCAN_VERSION == 14
        assert rebuilt["symbol_aliases"]["pkg::target"] == "pkg.impl::target"
        assert rebuilt["symbol_alias_limitations"]

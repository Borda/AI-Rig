"""Regression coverage for persisted, static symbol re-export aliases.

The call scanner faithfully records the spelling visible to a consumer.  Package ``__init__`` files can re-export that
spelling one or more times, however, so the reverse graph must normalize only aliases proven by a top-level
``ImportFrom``. These tests exercise the public index/query boundary and do not reuse scanner resolution helpers as an
oracle.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from codemap_py.graph import incremental_scan, scan
from codemap_py.query import _COMPACT_ALIAS_LIMITATION_LIMIT
from codemap_py.schema import SCAN_VERSION


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


def _query(root: Path, index_path: Path, *args: str) -> dict:
    """Run scan-query and return its JSON payload."""
    scan_query = Path(__file__).parents[2] / "bin" / "scan-query"
    result = subprocess.run(
        [sys.executable, str(scan_query), "--index", str(index_path), *args],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    return json.loads(result.stdout)


def _write_index(root: Path, index: dict) -> Path:
    """Persist *index* where scan-query can consume it."""
    path = root / "index.json"
    path.write_text(json.dumps(index))
    return path


def test_reexport_aliases_canonicalize_reverse_call_queries(tmp_path: Path) -> None:
    """One- and two-hop package re-exports resolve to the implementation symbol."""
    _write_project(tmp_path)
    index_path = _write_index(tmp_path, scan(tmp_path))

    direct = _query(tmp_path, index_path, "fn-rdeps", "pkg.impl::target")
    public = _query(tmp_path, index_path, "fn-rdeps", "pkg::target")

    assert {entry["caller"] for entry in direct["called_by"]} == {"pkg.consumer::use"}
    assert public["called_by"] == direct["called_by"]
    assert direct["index"]["query_complete"] is True


def test_external_terminal_alias_is_queryable_when_proven_by_import(tmp_path: Path) -> None:
    """A re-export terminating outside the scanned tree still has inbound callers."""
    _write_project(tmp_path)
    index_path = _write_index(tmp_path, scan(tmp_path))

    data = _query(tmp_path, index_path, "fn-rdeps", "pkg.external::external_target")

    assert {entry["caller"] for entry in data["called_by"]} == {"pkg.external_consumer::use_external"}
    assert data["index"]["query_complete"] is True


def test_reverse_imports_include_resolved_relative_and_parent_package_submodules(tmp_path: Path) -> None:
    """Reverse imports retain the parent edge and known submodule edges."""
    _write_project(tmp_path)
    index_path = _write_index(tmp_path, scan(tmp_path))

    package = _query(tmp_path, index_path, "rdeps", "pkg")
    api = _query(tmp_path, index_path, "rdeps", "pkg.api")

    assert set(package["imported_by"]) == {"pkg.consumer", "pkg.parent_consumer", "pkg.relative_consumer"}
    assert set(api["imported_by"]) == {"pkg", "pkg.parent_consumer", "pkg.relative_consumer"}


def test_ambiguous_alias_paths_veto_target_completeness(tmp_path: Path) -> None:
    """Known dynamic/rebound aliases make only their target query incomplete."""
    _write_project(tmp_path, ambiguous=True)
    index = scan(tmp_path)
    index_path = _write_index(tmp_path, index)

    data = _query(tmp_path, index_path, "fn-rdeps", "pkg.impl::target")
    aliases = index["symbol_aliases"]

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


def test_clean_proven_alias_remains_complete_without_ambiguous_paths(tmp_path: Path) -> None:
    """A DI-style direct re-export is complete when no rejected alias targets it."""
    _write_project(tmp_path)
    index_path = _write_index(tmp_path, scan(tmp_path))

    data = _query(tmp_path, index_path, "fn-rdeps", "pkg.impl::target")
    central = _query(tmp_path, index_path, "fn-central", "--top", "5")
    blast = _query(tmp_path, index_path, "fn-blast", "pkg.impl::target")

    assert data["index"]["query_complete"] is True
    assert data["index"]["completeness_reason"] == "ok"
    assert central["index"]["query_complete"] is True
    assert blast["index"]["query_complete"] is True


@pytest.mark.parametrize(
    ("query", "args"),
    [
        pytest.param("fn-central", ("--top", "5"), id="central"),
        pytest.param("fn-blast", ("pkg.impl::target",), id="target-blast"),
    ],
)
def test_alias_limitations_veto_central_and_target_blast_queries(
    tmp_path: Path, query: str, args: tuple[str, ...]
) -> None:
    """Centrality is whole-graph, while blast only sees its queried target's limits."""
    _write_project(tmp_path, ambiguous=True)
    index_path = _write_index(tmp_path, scan(tmp_path))

    data = _query(tmp_path, index_path, query, *args)
    assert data["index"]["query_complete"] is False
    assert data["index"]["completeness_reason"] == "symbol_alias_ambiguous"
    assert data["index"]["symbol_alias_limitations"]


@pytest.mark.parametrize(
    ("limitation_count", "truncated"),
    [
        pytest.param(0, False, id="none"),
        pytest.param(_COMPACT_ALIAS_LIMITATION_LIMIT, False, id="at-limit"),
        pytest.param(128, True, id="above-limit"),
    ],
)
def test_compact_central_bounds_alias_limitations_without_losing_full_output(
    tmp_path: Path, limitation_count: int, truncated: bool
) -> None:
    """Compact centrality samples ambiguous aliases; full output retains every record."""
    _write_project(tmp_path)
    _add_ambiguous_aliases(tmp_path, limitation_count)
    index = scan(tmp_path)
    index_path = _write_index(tmp_path, index)

    compact = _query(tmp_path, index_path, "--compact", "fn-central", "--top", "5")
    compact_index = compact["index"]

    if limitation_count == 0:
        assert compact_index["query_complete"] is True
        assert "symbol_alias_limitations" not in compact_index
        return

    assert compact_index["query_complete"] is False
    assert compact_index["completeness_reason"] == "symbol_alias_ambiguous"
    assert len(compact_index["symbol_alias_limitations"]) == min(limitation_count, _COMPACT_ALIAS_LIMITATION_LIMIT)
    assert compact_index["symbol_alias_limitations_total"] == limitation_count
    assert compact_index["symbol_alias_limitations_truncated"] is truncated

    full = _query(tmp_path, index_path, "fn-central", "--top", "5")
    full_index = full["index"]
    expected_records = sorted(
        index["symbol_alias_limitations"],
        key=lambda record: (record["alias_qname"], record["target_qname"], record["reason"]),
    )
    assert full_index["symbol_alias_limitations"] == expected_records
    assert "symbol_alias_limitations_total" not in full_index
    assert "symbol_alias_limitations_truncated" not in full_index

    if truncated:
        assert "symbol_alias_limitations_hint" in compact_index
        assert "Run without --compact" in compact_index["note"]
        assert len(json.dumps(compact, separators=(",", ":"))) < len(json.dumps(full, separators=(",", ":")))


@pytest.mark.parametrize(
    ("command", "result_key"),
    [pytest.param("fn-rdeps", "called_by", id="fn-rdeps"), pytest.param("fn-blast", "blast_radius", id="fn-blast")],
)
def test_compact_target_alias_limitations_keep_small_relevant_evidence(
    tmp_path: Path, command: str, result_key: str
) -> None:
    """Target-specific compact results keep all small, actionable ambiguity evidence."""
    _write_project(tmp_path, ambiguous=True)
    index_path = _write_index(tmp_path, scan(tmp_path))

    data = _query(tmp_path, index_path, "--compact", command, "pkg.impl::target")
    coverage = data["index"]

    assert data[result_key]
    assert coverage["query_complete"] is False
    assert coverage["completeness_reason"] == "symbol_alias_ambiguous"
    assert 0 < len(coverage["symbol_alias_limitations"]) <= _COMPACT_ALIAS_LIMITATION_LIMIT
    assert coverage["symbol_alias_limitations_total"] == len(coverage["symbol_alias_limitations"])
    assert coverage["symbol_alias_limitations_truncated"] is False
    assert "symbol_alias_limitations_hint" not in coverage


def test_incremental_alias_index_matches_fresh_full_scan(tmp_path: Path) -> None:
    """Incremental rebuild recomputes alias chains after a package re-export changes."""
    _write_project(tmp_path, ambiguous=True)
    old_index = scan(tmp_path)
    (tmp_path / "pkg" / "__init__.py").write_text("from .api import target as public_target\n")

    incremental = incremental_scan(tmp_path, old_index)
    full = scan(tmp_path)

    assert incremental["symbol_aliases"] == full["symbol_aliases"]
    assert incremental["symbol_alias_limitations"] == full["symbol_alias_limitations"]
    assert incremental["modules"] == full["modules"]


def test_incremental_submodule_add_and_remove_matches_fresh_scan(tmp_path: Path) -> None:
    """Unchanged from-import candidates are re-resolved when submodules appear or disappear."""
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "consumer.py").write_text("from pkg import child\n")
    initial = scan(tmp_path)

    (package / "child.py").write_text("")
    after_add = incremental_scan(tmp_path, initial)
    fresh_after_add = scan(tmp_path)

    assert after_add["modules"] == fresh_after_add["modules"]
    added_consumer = next(module for module in after_add["modules"] if module["name"] == "pkg.consumer")
    assert added_consumer["direct_imports"] == ["pkg", "pkg.child"]

    (package / "child.py").unlink()
    after_remove = incremental_scan(tmp_path, after_add)
    fresh_after_remove = scan(tmp_path)

    assert after_remove["modules"] == fresh_after_remove["modules"]
    removed_consumer = next(module for module in after_remove["modules"] if module["name"] == "pkg.consumer")
    assert removed_consumer["direct_imports"] == ["pkg"]


def test_incremental_markdown_change_refreshes_xrefs_without_creating_a_module(tmp_path: Path) -> None:
    """A docs-only xref update must not enter the Python module/degradation set."""
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


def test_incremental_scan_removes_legacy_markdown_entry_but_preserves_python_degradation(tmp_path: Path) -> None:
    """An incremental repair drops impossible docs modules without hiding a real broken Python source."""
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


def test_incremental_cli_rebuilds_v11_alias_index_to_v13(tmp_path: Path) -> None:
    """The scan-index entrypoint fully rebuilds persisted v11 data before serving aliases."""
    _write_project(tmp_path, ambiguous=True)
    legacy = scan(tmp_path)
    legacy["scan_version"] = 11
    legacy.pop("symbol_aliases")
    legacy.pop("symbol_alias_limitations")
    for module in legacy["modules"]:
        module.pop("symbol_aliases", None)
        module.pop("symbol_alias_limitations", None)
    index_path = tmp_path / ".cache" / "codemap" / f"{tmp_path.name}.json"
    index_path.parent.mkdir(parents=True)
    index_path.write_text(json.dumps(legacy))
    scan_index = Path(__file__).parents[2] / "bin" / "scan-index"

    result = subprocess.run(
        [sys.executable, str(scan_index), "--root", str(tmp_path), "--incremental"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "pre-v13 index found — falling back to full scan" in result.stderr
    rebuilt = json.loads(index_path.read_text())
    assert rebuilt["scan_version"] == SCAN_VERSION == 13
    assert rebuilt["symbol_aliases"]["pkg::target"] == "pkg.impl::target"
    assert rebuilt["symbol_alias_limitations"]

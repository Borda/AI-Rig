"""Bound graph-query memory while preserving paths, cycles and fixture evidence."""

from __future__ import annotations

import json
import tracemalloc

import pytest
from codemap_py.query import modules, subprocess_fixtures


def _fixture_index(definitions: list[dict], uses: list[str]) -> dict:
    """Build the graph fields emitted for a conftest and its consuming test module."""
    return {
        "scan_version": 11,
        "modules": [
            {
                "name": "conftest",
                "path": "conftest.py",
                "status": "ok",
                "fixture_exports": [{"name": entry["name"], "params": entry["depends_on"]} for entry in definitions],
            },
            {
                "name": "tests.test_example",
                "path": "tests/test_example.py",
                "status": "ok",
                "is_test": True,
                "fixture_uses": [{"name": name} for name in uses],
            },
        ],
    }


def _nodes(roots: list[dict]) -> list[dict]:
    """Flatten emitted nodes for independent output-size and reference assertions."""
    result = []
    pending = list(roots)
    while pending:
        node = pending.pop()
        result.append(node)
        pending.extend(node["depends_on"])
    return result


def test_shared_fixture_graph_serializes_references(capsys):
    """Each consuming root expands a layered diamond once within the same shared budget."""
    definitions = [
        {"name": f"layer{level}_{branch}", "depends_on": [f"layer{level + 1}_{n}" for n in range(4)]}
        for level in range(6)
        for branch in range(4)
    ]
    index = _fixture_index(definitions, ["layer0_0", "layer0_1"])
    subprocess_fixtures.cmd_fixture_graph(index, "tests.test_example")
    output = capsys.readouterr().out
    roots = json.loads(output)["fixtures"]
    first_nodes = _nodes(roots[:1])
    second_nodes = _nodes(roots[1:])
    assert len(output) < 50_000
    assert len(first_nodes) <= 100
    assert len(second_nodes) <= 100
    assert any(node.get("reference") for node in first_nodes)
    assert any(node.get("reference") for node in second_nodes)
    assert sum(node["name"] == "layer1_0" and not node.get("reference") for node in first_nodes) == 1
    assert sum(node["name"] == "layer1_0" and not node.get("reference") for node in second_nodes) == 1


def test_fixture_graph_caps_nodes_across_roots(capsys):
    """A wide set of unresolved builtin fixtures shares the same output budget."""
    index = _fixture_index([], [f"builtin_{n}" for n in range(1500)])
    subprocess_fixtures.cmd_fixture_graph(index, "tests.test_example")
    output = json.loads(capsys.readouterr().out)
    assert len(_nodes(output["fixtures"])) <= 1000
    assert output["truncated"] is True


def test_fixture_graph_preserves_cycle_evidence(capsys):
    """References do not obscure a fixture dependency cycle on the active path."""
    index = _fixture_index([{"name": "a", "depends_on": ["b"]}, {"name": "b", "depends_on": ["a"]}], ["a"])
    subprocess_fixtures.cmd_fixture_graph(index, "tests.test_example")
    nodes = _nodes(json.loads(capsys.readouterr().out)["fixtures"])
    assert len(nodes) == 3
    assert next(node for node in nodes if node.get("cycle"))["name"] == "a"


def test_fixture_graph_reports_nested_budget_exhaustion(capsys):
    """A single root discloses a cutoff even when its nested child reaches the cap."""
    definitions = [
        {"name": "root", "depends_on": ["branch"]},
        {"name": "branch", "depends_on": [f"builtin_{n}" for n in range(1500)]},
    ]
    subprocess_fixtures.cmd_fixture_graph(_fixture_index(definitions, ["root"]), "tests.test_example")
    output = json.loads(capsys.readouterr().out)
    assert len(_nodes(output["fixtures"])) <= 1000
    assert output["truncated"] is True


def test_deep_wide_import_path_has_bounded_memory(capsys):
    """A deep chain with a wide final frontier must not retain copies of the prefix."""
    chain_length = 1500
    index = {
        "modules": [{"name": f"m{n}", "direct_imports": [f"m{n + 1}"], "status": "ok"} for n in range(chain_length)]
        + [{"name": f"m{chain_length}", "direct_imports": [f"leaf{n}" for n in range(1500)], "status": "ok"}]
        + [{"name": f"leaf{n}", "direct_imports": [], "status": "ok"} for n in range(1500)]
    }
    tracemalloc.start()
    try:
        modules.cmd_path(index, "m0", "leaf1499")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    output = json.loads(capsys.readouterr().out)
    assert output["path"] == [f"m{n}" for n in range(chain_length + 1)] + ["leaf1499"]
    assert peak < 5 * 1024 * 1024


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        pytest.param("a", "a", ["a"], id="self"),
        pytest.param("a", "d", ["a", "b", "d"], id="shortest-with-cycle"),
        pytest.param("d", "a", None, id="disconnected"),
    ],
)
def test_import_path_preserves_shortest_route(capsys, start, end, expected):
    """Path reconstruction retains BFS order, cycle guards and empty-path semantics."""
    index = {
        "modules": [
            {"name": "a", "direct_imports": ["b", "c"]},
            {"name": "b", "direct_imports": ["a", "d"]},
            {"name": "c", "direct_imports": ["b"]},
            {"name": "d", "direct_imports": []},
        ]
    }
    modules.cmd_path(index, start, end)
    assert json.loads(capsys.readouterr().out)["path"] == expected

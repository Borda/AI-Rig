"""Coverage availability remains distinct from zero findings and exact package scope."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from codemap_py import query


@pytest.mark.parametrize(
    ("symbols", "status", "measured"),
    [
        pytest.param([], "empty", 0, id="empty-module"),
        pytest.param([{"qualified_name": "run"}], "unavailable", 0, id="unmeasured"),
        pytest.param([{"qualified_name": "run", "coverage_pct": 0.0}], "available", 1, id="measured-zero"),
        pytest.param([{"qualified_name": "run", "coverage_pct": 100.0}], "available", 1, id="measured-covered"),
        pytest.param(
            [{"qualified_name": "run", "coverage_pct": 50.0}, {"qualified_name": "other"}],
            "partial",
            1,
            id="partially-measured",
        ),
    ],
)
@pytest.mark.parametrize("command", ["coverage", "coverage-gap"])
def test_coverage_measurement_state(symbols, status, measured, command, capsys):
    """A zero-length finding list cannot erase whether measurements exist."""
    index = {"scan_version": 13, "modules": [{"name": "pkg", "status": "ok", "symbols": symbols}]}
    if command == "coverage":
        query.cmd_coverage(index, "pkg")
    else:
        query.cmd_coverage_gap(index, "pkg", False, 80.0)
    result = json.loads(capsys.readouterr().out)
    assert result["measurement"] == {"status": status, "symbols": len(symbols), "measured_symbols": measured}
    assert result["selection"] == {"scope": "exact-module", "matched_modules": 1, "includes_descendants": False}
    if status == "available" and symbols[0]["coverage_pct"] == 0.0:
        assert result["total"] == 1


def test_package_coverage_does_not_claim_descendants(capsys):
    """An empty package module states that its measured child was not searched."""
    index = {
        "scan_version": 13,
        "modules": [
            {"name": "pkg", "status": "ok", "symbols": []},
            {"name": "pkg.child", "status": "ok", "symbols": [{"qualified_name": "run", "coverage_pct": 0.0}]},
        ],
    }
    query.cmd_coverage_gap(index, "pkg", False, 80.0)
    exact = json.loads(capsys.readouterr().out)
    query.cmd_coverage_gap(index, None, True, 80.0)
    whole = json.loads(capsys.readouterr().out)
    assert exact["total"] == 0
    assert exact["selection"]["includes_descendants"] is False
    assert whole["total"] == 1
    assert whole["coverage_gap"][0]["module"] == "pkg.child"


def test_missing_module_remains_distinct_from_empty_module(capsys):
    """An absent selection reports zero matched modules, not an indexed empty module."""
    index = {"scan_version": 13, "modules": [{"name": "pkg", "status": "ok", "symbols": []}]}
    query.cmd_coverage_gap(index, "absent", False, 80.0)
    assert json.loads(capsys.readouterr().out)["selection"]["matched_modules"] == 0


@pytest.mark.integration
@pytest.mark.parametrize("output_format", ["json", "tsv"])
def test_compact_rendering_preserves_measurement_and_scope(output_format, project, scan_query):
    """Compact output and TSV envelopes retain unavailable measurement evidence."""
    root, index = project
    result = subprocess.run(
        [
            sys.executable,
            str(scan_query),
            "--index",
            str(index),
            "--no-heal",
            "--compact",
            "--format",
            output_format,
            "coverage-gap",
            "alpha",
        ],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    stream = result.stdout if output_format == "json" else result.stderr
    payload = next(json.loads(line) for line in stream.splitlines() if line.startswith("{"))
    assert payload["measurement"]["status"] == "unavailable"
    assert payload["selection"]["scope"] == "exact-module"
    assert payload["index"]["compact"] is True


@pytest.mark.integration
def test_batch_keeps_child_availability_and_one_terminal_record(project, scan_query, tmp_path):
    """Successful child metadata and failed siblings coexist in one invocation log."""
    root, index = project
    logs = tmp_path / "logs"
    env = {**os.environ, "CODEMAP_LOGGING": "true", "CODEMAP_LOG_DIR": str(logs), "CODEMAP_RUNTIME": "direct"}
    items = [
        {"cmd": "coverage-gap", "args": ["pkg"]},
        {"cmd": "coverage-gap", "args": ["alpha"]},
        {"cmd": "symbol", "args": ["missing_symbol"]},
    ]
    result = subprocess.run(
        [sys.executable, str(scan_query), "--index", str(index), "--no-heal", "--compact", "batch", json.dumps(items)],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert [item["result"]["measurement"]["status"] for item in payload["batch"][:2]] == ["empty", "unavailable"]
    assert payload["batch"][2]["ok"] is False
    records = [json.loads(line) for path in logs.rglob("cli*.jsonl") for line in path.read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["cmd"] == "batch"
    assert records[0]["exit_code"] == 0
    assert records[0]["result"] == payload

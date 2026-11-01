"""Coverage availability remains distinct from zero findings and exact package scope."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from codemap_py import query

_SCAN_VERSION = 13


def _invoke_coverage(index):
    """Run the ``coverage`` command for module ``pkg``; the result is printed as JSON."""
    query.cmd_coverage(index, "pkg")


def _invoke_coverage_gap(index):
    """Run the ``coverage-gap`` command for module ``pkg`` at the 80% threshold; the result is printed as JSON."""
    query.cmd_coverage_gap(index, "pkg", False, 80.0)


_BOTH_COMMANDS = pytest.mark.parametrize(
    "invoke", [pytest.param(_invoke_coverage, id="coverage"), pytest.param(_invoke_coverage_gap, id="coverage-gap")]
)


def _single_module_index(symbols):
    """Build a one-module index named ``pkg`` holding ``symbols``."""
    return {"scan_version": _SCAN_VERSION, "modules": [{"name": "pkg", "status": "ok", "symbols": symbols}]}


@pytest.fixture
def package_with_measured_child():
    """Index with an empty ``pkg`` module and a ``pkg.child`` module whose symbol has 0% coverage."""
    return {
        "scan_version": _SCAN_VERSION,
        "modules": [
            {"name": "pkg", "status": "ok", "symbols": []},
            {"name": "pkg.child", "status": "ok", "symbols": [{"qualified_name": "run", "coverage_pct": 0.0}]},
        ],
    }


class TestMeasurementState:
    """Coverage results state whether measurements exist, never just how many findings there are."""

    @_BOTH_COMMANDS
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
    def test_state_and_exact_scope(self, invoke, symbols, status, measured, capsys):
        """A zero-length finding list cannot erase whether measurements exist.

        The module is empty, unmeasured, fully measured at 0% or 100%, or only partly measured. Both commands must
        report the measurement state and an exact-module selection with no descendants.
        """
        invoke(_single_module_index(symbols))

        result = json.loads(capsys.readouterr().out)

        assert result["measurement"] == {"status": status, "symbols": len(symbols), "measured_symbols": measured}
        assert result["selection"] == {"scope": "exact-module", "matched_modules": 1, "includes_descendants": False}

    @_BOTH_COMMANDS
    def test_measured_zero_counts_as_one_finding(self, invoke, capsys):
        """A symbol measured at 0% is a real finding, not an absent measurement.

        Distinguishes "measured and uncovered" (total 1) from "unmeasured" (unavailable, total 0).
        """
        invoke(_single_module_index([{"qualified_name": "run", "coverage_pct": 0.0}]))

        assert json.loads(capsys.readouterr().out)["total"] == 1

    def test_missing_module_reports_zero_matches(self, capsys):
        """An absent selection reports zero matched modules, not an indexed empty module.

        Guards against a typo'd module name looking like an empty module with nothing to cover.
        """
        query.cmd_coverage_gap(_single_module_index([]), "absent", False, 80.0)

        assert json.loads(capsys.readouterr().out)["selection"]["matched_modules"] == 0


class TestPackageScope:
    """Coverage for a package module covers exactly that module unless the whole graph is requested."""

    def test_exact_package_does_not_claim_descendants(self, package_with_measured_child, capsys):
        """An empty package module states that its measured child was not searched.

        Without the explicit ``includes_descendants: False`` an empty total would read as "the package is covered".
        """
        query.cmd_coverage_gap(package_with_measured_child, "pkg", False, 80.0)

        exact = json.loads(capsys.readouterr().out)

        assert exact["total"] == 0
        assert exact["selection"]["includes_descendants"] is False

    def test_whole_graph_reports_child_gap(self, package_with_measured_child, capsys):
        """The whole-graph query finds the uncovered child that the exact-package query leaves unsearched."""
        query.cmd_coverage_gap(package_with_measured_child, None, True, 80.0)

        whole = json.loads(capsys.readouterr().out)

        assert whole["total"] == 1
        assert whole["coverage_gap"][0]["module"] == "pkg.child"


class TestCliOutput:
    """The scan-query CLI keeps measurement and scope evidence in compact, TSV and batch output."""

    @pytest.mark.integration
    @pytest.mark.parametrize("output_format", ["json", "tsv"])
    def test_compact_rendering_preserves_measurement_and_scope(self, output_format, project, scan_query):
        """Compact output and TSV envelopes retain unavailable measurement evidence.

        Real CLI run against the fixture project: ``alpha`` has no coverage data, so the compact envelope must still
        say ``unavailable`` and name the exact-module scope on stdout (json) or stderr (tsv).
        """
        root, index = project
        args = ["--index", str(index), "--no-heal", "--compact", "--format", output_format, "coverage-gap", "alpha"]

        result = subprocess.run([sys.executable, str(scan_query), *args], cwd=root, capture_output=True, text=True)

        assert result.returncode == 0, result.stderr
        stream = result.stdout if output_format == "json" else result.stderr
        payload = next(json.loads(line) for line in stream.splitlines() if line.startswith("{"))
        assert payload["measurement"]["status"] == "unavailable"
        assert payload["selection"]["scope"] == "exact-module"
        assert payload["index"]["compact"] is True

    @pytest.mark.integration
    def test_batch_keeps_child_availability_and_one_terminal_record(self, project, scan_query, tmp_path):
        """Successful child metadata and failed siblings coexist in one invocation log.

        A batch of two coverage-gap queries and one failing symbol lookup runs through the real CLI with logging on.
        Each child keeps its own availability, and the log holds a single terminal record mirroring the payload.
        """
        root, index = project
        logs = tmp_path / "logs"
        env = {**os.environ, "CODEMAP_LOGGING": "true", "CODEMAP_LOG_DIR": str(logs), "CODEMAP_RUNTIME": "direct"}
        items = [
            {"cmd": "coverage-gap", "args": ["pkg"]},
            {"cmd": "coverage-gap", "args": ["alpha"]},
            {"cmd": "symbol", "args": ["missing_symbol"]},
        ]
        args = ["--index", str(index), "--no-heal", "--compact", "batch", json.dumps(items)]

        result = subprocess.run(
            [sys.executable, str(scan_query), *args], cwd=root, env=env, capture_output=True, text=True
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

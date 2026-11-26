"""Verify shared ID lookup, host-scoped registration and cross-harness rollup."""

import json
from pathlib import Path

import gap_ledger
import gap_rollup
import pytest


def _record(gap_id, gap_class, owner, harness="codex", **fields):
    """Construct a current ledger record for the same repository and pull request."""
    return {
        "schema": "gap-ledger",
        "schema_version": 1,
        "id": gap_id,
        "class": gap_class,
        "owner": owner,
        "summary": "Evidence was unavailable.",
        "closable_by": "Rerun with evidence.",
        "evidence": "report.md",
        "subject": {"repo": "example/project", "pr": 4, "head": "a" * 40},
        "run": {
            "run_id": harness,
            "kind": "review",
            "harness": harness,
            "plugin": harness,
            "plugin_version": "1.0.0",
            "route": "direct",
        },
        **fields,
    }


def _write_ledger(tmp_path: Path, name: str, record: dict) -> Path:
    """Write one run ledger for rollup."""
    path = tmp_path / name / "gaps.jsonl"
    path.parent.mkdir()
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return path


def test_lookup_and_rollup_count_a_shared_id_once(tmp_path, capsys):
    """The shared lookup supplies both harnesses the same countable identity."""
    gap_id = "provisioning-failed-opencv-python"
    assert gap_ledger.main(["lookup", gap_id]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "id": gap_id,
        "class": "environment-provisionable",
        "owner": "workflow",
    }
    claude = _write_ledger(tmp_path, "claude", _record(gap_id, "environment-provisionable", "workflow", "claude"))
    codex = _write_ledger(tmp_path, "codex", _record(gap_id, "environment-provisionable", "workflow"))
    result = gap_rollup.to_json(gap_rollup.build_rollup([claude, codex], []))
    assert (result["ledgers"], result["distinct_gaps"]) == (2, 1)
    assert {row["harness"] for row in result["routes"]} == {"claude", "codex"}


@pytest.mark.parametrize(
    "other_host",
    [pytest.param(("linux", "arm64"), id="other-os"), pytest.param(("darwin", "x86_64"), id="other-architecture")],
)
def test_known_limit_registration_and_rollup_are_host_scoped(tmp_path, monkeypatch, other_host):
    """Matching hardware on another OS or architecture stays an ordinary gap."""
    host = ("darwin", "arm64")
    monkeypatch.setattr(gap_ledger, "current_host_class", lambda: host)
    path = tmp_path / ".notes" / "known-limits.json"
    first = _record(
        "cuda-absent",
        "hardware-absent",
        "host",
        hardware_class="cuda",
        reason="No CUDA hardware.",
        host_class=gap_ledger.host_class_fields(host),
        deduction=0.05,
    )
    other = {**first, "host_class": gap_ledger.host_class_fields(other_host)}
    assert gap_ledger.register_known_limits(path, "example/project", [first]) == (["cuda-absent"], None)
    assert gap_ledger.register_known_limits(path, "example/project", [first]) == ([], None)
    repo, limits = gap_ledger.read_known_limits(path)
    assert (repo, limits[0].host_class, first["deduction"]) == ("example/project", host, 0.05)
    assert gap_ledger.apply_known_limit(first, limits)["deduction"] is None
    assert gap_ledger.apply_known_limit(other, limits) == other
    matching = _write_ledger(tmp_path, "same-host", first)
    distinct = _write_ledger(tmp_path, "different-host", other)
    result = gap_rollup.to_json(gap_rollup.build_rollup([matching, distinct], [path]))
    assert result["distinct_gaps"] == 1
    assert result["known_limits"][0]["runs"] == 1
    assert result["known_limits"][0]["host_class"] == first["host_class"]


def test_hardware_gap_and_known_limit_require_the_observing_host(tmp_path):
    """A hostless hardware observation cannot enter either version 1 artifact."""
    gap = _record("cuda-absent", "hardware-absent", "host", hardware_class="cuda", reason="No CUDA hardware.")
    assert gap_ledger.record_errors(gap) == ["hardware gaps require host_class with os and architecture"]

    path = tmp_path / "known-limits.json"
    entry = {key: gap[key] for key in ("id", "class", "hardware_class", "reason")}
    path.write_text(
        json.dumps({"schema": "known-limits", "schema_version": 1, "repo": "example/project", "limits": [entry]}),
        encoding="utf-8",
    )
    with pytest.raises(gap_ledger.LedgerInvalidError, match="host_class must contain os and architecture"):
        gap_ledger.read_known_limits(path)

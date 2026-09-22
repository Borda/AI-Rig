"""Public telemetry contracts preserve explicit project and terminal-outcome provenance."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import join_avoidance as ja


def _records(project: str) -> tuple[dict, dict]:
    """Return a terminal-success answer and later same-module tool event."""
    common = {"session": "shared-session", "v": "0.39.4", "project": project}
    return (
        common
        | {
            "layer": "cli",
            "ts": "2026-09-01T00:00:00Z",
            "cmd": "rdeps",
            "exit_code": 0,
            "result": {"module": "pkg.mod", "index": {"query_complete": True}},
        },
        common | {"layer": "tool", "ts": "2026-09-01T00:01:00Z", "tool": "Read", "target": "pkg/mod.py"},
    )


@pytest.mark.parametrize("project", ["/work/first", "C:\\work\\first"])
def test_shared_log_root_keeps_serialized_project_coordinates(tmp_path: Path, capsys, project: str) -> None:
    """A shared destination never replaces POSIX or Windows source coordinates."""
    runtime = tmp_path / "claude"
    runtime.mkdir()
    cli, tool = _records(project)
    (runtime / "cli_s.jsonl").write_text(json.dumps(cli) + "\n")
    (runtime / "tools_s.jsonl").write_text(json.dumps(tool | {"project": "/work/other"}) + "\n")
    assert ja.main(["--logs", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["avoidance_count"] == 0
    (runtime / "tools_s.jsonl").write_text(json.dumps(tool) + "\n")
    assert ja.main(["--logs", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["avoidance_count"] == 1
    assert payload["events"][0]["project"] == project


@pytest.mark.parametrize("field", ["project", "exit_code"])
def test_missing_provenance_is_counted_but_not_joined(tmp_path: Path, capsys, field: str) -> None:
    """Legacy records do not prove a terminal-success overlap in an identified project."""
    cli, tool = _records("/work/project")
    cli.pop(field)
    cli_path = tmp_path / "cli.jsonl"
    tool_path = tmp_path / "tools.jsonl"
    cli_path.write_text(json.dumps(cli) + "\n")
    tool_path.write_text(json.dumps(tool) + "\n")
    assert ja.main(["--cli", str(cli_path), "--tools", str(tool_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["avoidance_count"] == 0
    assert payload["record_counts"]["excluded_or_unjoinable_cli_records"] == 1
    if field == "exit_code":
        assert payload["record_counts"]["unverified_cli_outcomes"] == 1


def test_batch_child_accounting_partitions_every_child(tmp_path: Path, capsys) -> None:
    """Successful, failed, incomplete and malformed children remain visible separately."""
    cli, tool = _records("/work/project")
    complete = cli["result"]
    cli.update(
        cmd="batch",
        result={
            "batch": [
                {"cmd": "rdeps", "ok": True, "result": complete},
                {"cmd": "rdeps", "ok": False, "result": {"error": "missing"}},
                {"cmd": "rdeps", "ok": True, "result": {"module": "pkg.mod", "index": {"query_complete": False}}},
                None,
                {"cmd": "batch", "ok": True, "result": {"batch": [{"cmd": "rdeps", "ok": True, "result": complete}]}},
            ]
        },
    )
    cli_path = tmp_path / "cli.jsonl"
    tool_path = tmp_path / "tools.jsonl"
    cli_path.write_text(json.dumps(cli) + "\n")
    tool_path.write_text(json.dumps(tool) + "\n")
    assert ja.main(["--cli", str(cli_path), "--tools", str(tool_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    counts = payload["record_counts"]
    assert counts["eligible_cli_invocations"] == 1
    assert counts["logical_answers"] == 1
    assert counts["batch_children"] == 5
    assert counts["eligible_batch_children"] == 1
    assert counts["failed_batch_children"] == 1
    assert counts["unjoinable_batch_children"] == 3
    assert payload["avoidance_count"] == 1
    assert ja.main(["--cli", str(cli_path), "--tools", str(tool_path)]) == 0
    output = capsys.readouterr().out
    assert "failed_batch_children: 1" in output
    assert "unjoinable_batch_children: 3" in output


def test_skill_attribution_cannot_cross_project_or_version(tmp_path: Path, capsys) -> None:
    """An unrelated skill start cannot label another cohort's legitimate overlap."""
    cli, tool = _records("/work/project")
    (tmp_path / "cli.jsonl").write_text(json.dumps(cli) + "\n")
    (tmp_path / "tools.jsonl").write_text(json.dumps(tool) + "\n")
    skills = [
        cli | {"skill": "foreign-project", "project": "/work/other"},
        cli | {"skill": "foreign-version", "v": "0.39.3"},
        cli | {"skill": "matching-skill"},
    ]
    (tmp_path / "skills.jsonl").write_text("".join(json.dumps(record) + "\n" for record in skills))
    assert ja.main(["--logs", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["avoidance_count"] == 1
    assert payload["per_skill"] == {"matching-skill": 1}

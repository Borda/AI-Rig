"""Public telemetry contracts preserve explicit project and terminal-outcome provenance."""

from __future__ import annotations

import json
from pathlib import Path

import join_avoidance as ja
import pytest


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


def _write_pair(tmp_path: Path, cli: dict, tool: dict) -> list[str]:
    """Write one CLI record and one tool record; return the ``--cli``/``--tools`` argv."""
    cli_path = tmp_path / "cli.jsonl"
    tool_path = tmp_path / "tools.jsonl"
    cli_path.write_text(json.dumps(cli) + "\n")
    tool_path.write_text(json.dumps(tool) + "\n")
    return ["--cli", str(cli_path), "--tools", str(tool_path)]


def _run_json(capsys: pytest.CaptureFixture[str], argv: list[str]) -> dict:
    """Run ``join_avoidance.main`` with ``--json``, require exit 0, and return the parsed payload."""
    assert ja.main([*argv, "--json"]) == 0
    return json.loads(capsys.readouterr().out)


@pytest.fixture(name="batch_argv")
def _batch_argv(tmp_path: Path) -> list[str]:
    """Log argv for a batch answer holding good, failed, incomplete, malformed and nested children, plus a later
    read."""
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
    return _write_pair(tmp_path, cli, tool)


class TestProjectCoordinates:
    """Serialized project strings are cross-host coordinates compared exactly."""

    @pytest.mark.parametrize(
        ("project", "tool_project", "expected_events"),
        [
            pytest.param("/work/first", "/work/other", [], id="posix-foreign-tool-project"),
            pytest.param("C:\\work\\first", "/work/other", [], id="windows-foreign-tool-project"),
            pytest.param("/work/first", "/work/first", ["/work/first"], id="posix-matching-tool-project"),
            pytest.param("C:\\work\\first", "C:\\work\\first", ["C:\\work\\first"], id="windows-matching-tool-project"),
        ],
    )
    def test_shared_log_root_joins_only_same_project(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        project: str,
        tool_project: str,
        expected_events: list[str],
    ) -> None:
        """A shared log root joins a read to an answer only when both name the identical project string.

        POSIX and Windows coordinates both survive verbatim: a tool event from another project never counts as
        avoidance, and a matching one reports the original serialized project, not a host-normalised path.
        """
        runtime = tmp_path / "claude"
        runtime.mkdir()
        cli, tool = _records(project)
        (runtime / "cli_s.jsonl").write_text(json.dumps(cli) + "\n")
        (runtime / "tools_s.jsonl").write_text(json.dumps(tool | {"project": tool_project}) + "\n")

        payload = _run_json(capsys, ["--logs", str(tmp_path)])

        assert payload["avoidance_count"] == len(expected_events)
        assert [event["project"] for event in payload["events"]] == expected_events


class TestLegacyRecords:
    """CLI records missing provenance fields are counted but never joined."""

    @pytest.mark.parametrize("field", ["project", "exit_code"])
    def test_missing_provenance_is_excluded_from_join(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], field: str
    ) -> None:
        """A CLI record without ``project`` or ``exit_code`` yields no avoidance and is counted as unjoinable.

        Legacy records cannot prove a terminal-success overlap inside an identified project, so they stay visible in the
        counts instead of silently producing an avoidance event.
        """
        cli, tool = _records("/work/project")
        cli.pop(field)

        payload = _run_json(capsys, _write_pair(tmp_path, cli, tool))

        assert payload["avoidance_count"] == 0
        assert payload["record_counts"]["excluded_or_unjoinable_cli_records"] == 1

    def test_missing_exit_code_is_counted_unverified(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A CLI record without ``exit_code`` is reported as an unverified outcome.

        The absent status is the distinguishing signal: a missing ``project`` is unjoinable, but only a missing exit
        code means the success of the answer itself cannot be verified.
        """
        cli, tool = _records("/work/project")
        cli.pop("exit_code")

        payload = _run_json(capsys, _write_pair(tmp_path, cli, tool))

        assert payload["record_counts"]["unverified_cli_outcomes"] == 1


class TestBatchAccounting:
    """A batch answer partitions every child into joined, failed or unjoinable."""

    def test_json_counts_partition_every_child(self, capsys: pytest.CaptureFixture[str], batch_argv: list[str]) -> None:
        """Successful, failed, incomplete and malformed children are counted separately in the JSON payload.

        Five children (one good, one failed, one incomplete, one malformed, one nested batch) must all remain visible:
        only the single complete child joins the later read.
        """
        payload = _run_json(capsys, batch_argv)

        counts = payload["record_counts"]
        assert counts["eligible_cli_invocations"] == 1
        assert counts["logical_answers"] == 1
        assert counts["batch_children"] == 5
        assert counts["eligible_batch_children"] == 1
        assert counts["failed_batch_children"] == 1
        assert counts["unjoinable_batch_children"] == 3
        assert payload["avoidance_count"] == 1

    def test_text_output_reports_failed_and_unjoinable_children(
        self, capsys: pytest.CaptureFixture[str], batch_argv: list[str]
    ) -> None:
        """The human-readable report prints the failed and unjoinable batch child counts.

        The same five-child batch as the JSON case, read through the default text rendering a person sees.
        """
        assert ja.main(batch_argv) == 0

        output = capsys.readouterr().out
        assert "failed_batch_children: 1" in output
        assert "unjoinable_batch_children: 3" in output


class TestSkillAttribution:
    """Skill-start records label an overlap only inside the same project and version cohort."""

    def test_skill_attribution_cannot_cross_project_or_version(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An unrelated skill start cannot label another cohort's legitimate overlap.

        Two skill starts differ from the answer by project or by version; only the third matches both, so only it may be
        credited with the single overlap.
        """
        cli, tool = _records("/work/project")
        (tmp_path / "cli.jsonl").write_text(json.dumps(cli) + "\n")
        (tmp_path / "tools.jsonl").write_text(json.dumps(tool) + "\n")
        skills = [
            cli | {"skill": "foreign-project", "project": "/work/other"},
            cli | {"skill": "foreign-version", "v": "0.39.3"},
            cli | {"skill": "matching-skill"},
        ]
        (tmp_path / "skills.jsonl").write_text("".join(json.dumps(record) + "\n" for record in skills))

        payload = _run_json(capsys, ["--logs", str(tmp_path)])

        assert payload["avoidance_count"] == 1
        assert payload["per_skill"] == {"matching-skill": 1}

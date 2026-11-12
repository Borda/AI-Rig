"""Keep provider evidence and executed-attempt accounting separate from public answers."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import bridge_call


@pytest.mark.parametrize(
    ("host", "record", "expected"),
    [
        pytest.param(
            "claude",
            {"modelUsage": {"peer-a": {}, "peer-b": {}}, "result": "{}"},
            ("peer-a", "peer-b"),
            id="multiple-provider-models",
        ),
        pytest.param("codex", {"type": "thread.started", "model": "peer-c"}, ("peer-c",), id="codex-metadata"),
        pytest.param("claude", {"structured_output": {"model": "answer-not-evidence"}}, (), id="answer-excluded"),
        pytest.param("codex", {"type": "thread.started"}, (), id="unknown-model"),
    ],
)
def test_model_identity_comes_only_from_provider_metadata(host: str, record: dict, expected: tuple) -> None:
    """Do not fabricate the model from a request or peer answer when provider evidence is absent."""
    assert bridge_call._parse_output(json.dumps(record), host).observed_models == expected


def test_retry_keeps_ordered_observations_without_changing_public_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep failed and final usage, with one final report and no extra child execution."""
    core = dict(status="complete", verdict="Done", findings=[], files_touched=[], remaining=[], blockers=[], details=[])
    usage = dict(input_tokens=10, cached_input_tokens=4, output_tokens=3)
    response = "\n".join(
        json.dumps(r)
        for r in [
            {"type": "thread.started", "model": "observed-peer"},
            {"type": "item.completed", "item": {"text": json.dumps(core)}},
            {"type": "turn.completed", "usage": usage},
        ]
    )
    outcomes = iter(
        [
            bridge_call.ChildOutcome(response, "", 0, True, None),
            bridge_call.ChildOutcome(response, "", 0, False, None),
        ]
    )
    monkeypatch.setattr(bridge_call, "_run_child", lambda *args: next(outcomes))
    request = bridge_call.Request("review", "Review", None, "medium", 1, 0, "r", tmp_path, "claude_to_codex")
    result = bridge_call.run_request(request)
    health = json.loads((tmp_path / ".temp/bridge/health.jsonl").read_text())
    report = json.loads((tmp_path / health["report_path"]).read_text())
    attempts = health["telemetry"]["attempts"]
    assert len(attempts) == 2
    assert attempts[0]["attempt"] == 1
    assert attempts[1]["attempt"] == 2
    assert attempts[0]["status"] == "timeout"
    assert attempts[1]["status"] == "complete"
    assert attempts[0]["usage_complete"] is False
    assert attempts[1]["usage_complete"] is True
    assert attempts[0]["tokens"] == usage
    assert attempts[1]["tokens"] == usage
    assert attempts[0]["observed_models"] == ["observed-peer"]
    assert report["telemetry"] == health["telemetry"]
    assert "telemetry" not in result
    assert result["tokens"] == usage
    assert bridge_call.validate_envelope(result) == result
    assert next(outcomes, None) is None


def test_refusal_records_zero_executed_attempts(tmp_path: Path) -> None:
    """A depth refusal has no provider usage to count as missing or zero."""
    request = bridge_call.Request("review", "Review", None, "medium", 1, 1, "r", tmp_path, "claude_to_codex")
    result = bridge_call.run_request(request)
    health = json.loads((tmp_path / ".temp/bridge/health.jsonl").read_text())
    assert result["status"] == "refused"
    assert health["telemetry"]["attempts"] == []
    assert health["telemetry"]["runtime_sha256"] == hashlib.sha256(Path(bridge_call.__file__).read_bytes()).hexdigest()
    assert (
        health["telemetry"]["plugin_version"]
        == json.loads((Path(bridge_call.__file__).parents[1] / ".claude-plugin/plugin.json").read_text())["version"]
    )


def test_missing_executable_records_no_executed_attempt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed process launch must not create unknown provider spend for work that never started."""
    monkeypatch.setattr(bridge_call, "build_codex_argv", lambda *args: [str(tmp_path / "missing-peer")])
    request = bridge_call.Request("review", "Review", None, "medium", 1, 0, "r", tmp_path, "claude_to_codex")
    result = bridge_call.run_request(request)
    health = json.loads((tmp_path / ".temp/bridge/health.jsonl").read_text())
    assert result["status"] == "blocked"
    assert health["telemetry"]["attempts"] == []


def test_round_trip_fields_bracket_the_executed_attempt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Start and end times, byte sizes and process facts let one call be replayed as a request and a response.

    A real run with a fake child records when the call began, when the attempt ran, how large the outgoing task and the
    returned envelope were, and how the child exited, so a later timeline can order calls without guessing.
    """
    core = dict(status="complete", verdict="Done", findings=[], files_touched=[], remaining=[], blockers=[], details=[])
    response = "\n".join(
        json.dumps(r)
        for r in [
            {"type": "thread.started", "model": "observed-peer"},
            {"type": "item.completed", "item": {"text": json.dumps(core)}},
            {"type": "turn.completed", "usage": dict(input_tokens=10, cached_input_tokens=4, output_tokens=3)},
        ]
    )
    monkeypatch.setattr(
        bridge_call, "_run_child", lambda *args: bridge_call.ChildOutcome(response, "warn", 0, False, None)
    )
    request = bridge_call.Request("review", "Review é", None, "medium", 1, 0, "r", tmp_path, "claude_to_codex")
    result = bridge_call.run_request(request)
    health = json.loads((tmp_path / ".temp/bridge/health.jsonl").read_text())
    attempt = health["telemetry"]["attempts"][0]
    assert health["started_at"] <= attempt["started_at"] <= attempt["ended_at"] <= health["ts"]
    assert health["task_bytes"] == len("Review é".encode())
    assert health["response_bytes"] == len(json.dumps(result, sort_keys=True).encode())
    assert (attempt["returncode"], attempt["timed_out"]) == (0, False)
    assert (attempt["stdout_bytes"], attempt["stderr_bytes"]) == (len(response.encode()), len(b"warn"))
    assert "job_id" not in health


def test_refusal_still_records_start_and_sizes(tmp_path: Path) -> None:
    """A pre-dispatch refusal has no attempt but keeps its start time and message sizes."""
    request = bridge_call.Request("review", "Review", None, "medium", 1, 1, "r", tmp_path, "claude_to_codex")
    bridge_call.run_request(request)
    health = json.loads((tmp_path / ".temp/bridge/health.jsonl").read_text())
    assert health["started_at"] <= health["ts"]
    assert (health["task_bytes"], health["response_bytes"] > 0) == (len(b"Review"), True)


def test_diagnostic_probes_get_distinct_run_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Repeated live probes must not share a run id, or usage accounting reports them as conflicting duplicates."""
    import bridge_diagnose

    seen: list[str] = []

    def fake_run(request: bridge_call.Request, host: str) -> dict:
        """Record the probe's run id and return a minimal passing result."""
        seen.append(request.run_id)
        return {"status": "complete"}

    monkeypatch.setattr(bridge_diagnose, "run_request", fake_run)
    bridge_diagnose._live_result("codex", tmp_path)
    bridge_diagnose._live_result("codex", tmp_path)
    assert seen[0] != seen[1]
    assert all(run_id.startswith("diagnostic-") for run_id in seen)

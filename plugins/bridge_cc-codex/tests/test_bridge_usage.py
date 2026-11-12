"""Exercise offline accounting with retained bridge evidence and hostile artifact references."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from _bridge_link_probes import file_symlinks_supported, hardlinks_supported

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import bridge_usage


@pytest.fixture
def store(tmp_path: Path) -> Path:
    """Create the real on-disk artifact layout without starting a provider."""
    root = tmp_path / ".temp" / "bridge"
    (root / "incidents").mkdir(parents=True)
    return root


def _health(store: Path, rows: list[dict]) -> None:
    """Persist explicit retained accounting records."""
    (store / "health.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_provider_arithmetic_and_unknown_legacy_metrics(store: Path) -> None:
    """Keep provider cache semantics separate and expose missing observed spend."""
    _health(
        store,
        [
            {
                "direction": "claude_to_codex",
                "status": "partial",
                "tokens": {"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 20},
                "cost": None,
            },
            {
                "direction": "codex_to_claude",
                "status": "complete",
                "model": "requested",
                "tokens": {
                    "input_tokens": 10,
                    "cache_read_input_tokens": 40,
                    "cache_creation_input_tokens": 5,
                    "output_tokens": 7,
                },
                "cost": 0.25,
            },
            {"direction": "claude_to_codex", "status": "blocked", "tokens": {}, "cost": None},
        ],
    )
    result = bridge_usage.summarize([store.parents[1]])
    codex, claude = result["groups"]
    assert codex["provider"] == "codex"
    assert codex["normalized"] == {"input_tokens_total": 100, "output_tokens_total": 20}
    assert codex["raw_tokens"]["cached_input_tokens"] == 60
    assert codex["partial_calls"] == 1
    assert codex["statuses"] == {"partial": 1, "blocked": 1}
    assert codex["missing_token_attempts"] == 1
    assert codex["missing_cost_attempts"] == 2
    assert codex["unknown_spend"] is True
    assert codex["plugin_version"] == codex["model"] == "unknown"
    assert claude["normalized"] == {"input_tokens_total": 55, "output_tokens_total": 7}
    assert claude["known_cost"] == 0.25


def test_retry_incidents_are_counted_without_terminal_duplication(store: Path) -> None:
    """Follow explicit retry chains and count the final failed attempt once."""
    prior = ".temp/bridge/incidents/first.json"
    final = ".temp/bridge/incidents/final.json"
    (store / "incidents" / "first.json").write_text(
        json.dumps(
            {
                "tokens": {"input_tokens": 3, "output_tokens": 1},
                "cost": 0.1,
                "transcript_path": ".temp/bridge/raw-first.txt",
            }
        ),
        encoding="utf-8",
    )
    (store / "incidents" / "final.json").write_text(
        json.dumps(
            {
                "tokens": {"input_tokens": 8, "output_tokens": 2},
                "cost": 0.2,
                "transcript_path": ".temp/bridge/raw-last.txt",
                "prior_incident": prior,
            }
        ),
        encoding="utf-8",
    )
    _health(
        store,
        [
            {
                "direction": "claude_to_codex",
                "status": "blocked",
                "tokens": {"input_tokens": 8, "output_tokens": 2},
                "cost": 0.2,
                "incident": final,
                "transcript_path": ".temp/bridge/raw-last.txt",
            }
        ],
    )
    group = bridge_usage.summarize([store.parents[1]])["groups"][0]
    assert group["attempts"] == 2
    assert group["normalized"] == {"input_tokens_total": 11, "output_tokens_total": 3}
    assert group["known_cost"] == pytest.approx(0.3)


def test_report_attempts_override_health_and_incident_metrics(store: Path) -> None:
    """Prefer exact report attempt evidence instead of adding overlapping ledger totals."""
    report = {
        "telemetry": {
            "plugin_version": "1.2.3",
            "observed_models": ["actual"],
            "attempts": [
                {"tokens": {"input_tokens": 20, "output_tokens": 2}, "cost": 0.4},
                {"tokens": {"input_tokens": 30, "output_tokens": 4}, "cost": 0.5},
            ],
        }
    }
    (store / "result.json").write_text(json.dumps(report), encoding="utf-8")
    _health(
        store,
        [
            {
                "direction": "claude_to_codex",
                "status": "complete",
                "model": "requested",
                "tokens": {"input_tokens": 999},
                "report_path": ".temp/bridge/result.json",
                "incident": ".temp/bridge/incidents/missing.json",
            }
        ],
    )
    result = bridge_usage.summarize([store.parents[1]])
    group = result["groups"][0]
    assert group["attempts"] == 2
    assert group["model"] == "actual"
    assert group["plugin_version"] == "1.2.3"
    assert group["normalized"] == {"input_tokens_total": 50, "output_tokens_total": 6}
    assert group["known_cost"] == pytest.approx(0.9)
    assert "missing_linked_artifact" in {item["code"] for item in result["diagnostics"]}


def test_malformed_rows_and_unsafe_cross_host_refs_are_diagnostic(store: Path) -> None:
    """Reject malformed rows and cross-host paths while preserving valid accounting."""
    _health(
        store,
        [
            {
                "direction": "claude_to_codex",
                "status": "complete",
                "report_path": "C:\\outside\\report.json",
                "incident": "../../outside.json",
                "tokens": {"input_tokens": -1, "output_tokens": True},
                "cost": float("nan"),
            }
        ],
    )
    with (store / "health.jsonl").open("a", encoding="utf-8") as stream:
        stream.write("{broken\n[]\n")
    result = bridge_usage.summarize([store.parents[1]])
    assert {item["code"] for item in result["diagnostics"]} == {
        "unsafe_reference",
        "malformed_row",
        "invalid_metrics",
        "retained_history_only",
    }
    assert result["groups"][0]["missing_token_attempts"] == 1
    assert result["groups"][0]["missing_cost_attempts"] == 1


@pytest.mark.skipif(not file_symlinks_supported(), reason="Host cannot create file symlinks")
def test_symlinked_health_is_rejected(store: Path) -> None:
    """Refuse ledger aliases instead of reading their target."""
    target = store.parents[1] / "private.jsonl"
    target.write_text('{"direction":"claude_to_codex"}\n', encoding="utf-8")
    (store / "health.jsonl").symlink_to(target)
    result = bridge_usage.summarize([store.parents[1]])
    assert result["groups"] == []
    assert result["diagnostics"][0]["code"] == "unsafe_health"


@pytest.mark.skipif(not hardlinks_supported(), reason="Host cannot create hard links")
def test_hardlinked_report_is_rejected(store: Path) -> None:
    """Refuse linked result evidence even when its name is contained."""
    target = store.parents[1] / "private.json"
    target.write_text('{"telemetry":{"plugin_version":"secret"}}', encoding="utf-8")
    os.link(target, store / "report.json")
    _health(store, [{"report_path": ".temp/bridge/report.json"}])
    result = bridge_usage.summarize([store.parents[1]])
    assert result["groups"][0]["plugin_version"] == "unknown"
    assert "invalid_linked_artifact" in {item["code"] for item in result["diagnostics"]}


def test_cli_only_reads_explicit_workspaces_and_does_not_create_store(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Return JSON for a nonexistent store without mutation or workspace discovery."""
    assert bridge_usage.main(["--workspace", str(tmp_path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["groups"] == []
    assert result["workspaces"] == [str(tmp_path.resolve())]
    assert not (tmp_path / ".temp").exists()


def test_rotation_deduplicates_run_identity_and_preserves_source(store: Path) -> None:
    """Do not double charge exact duplicates or conflicting records across retained segments."""
    row = {
        "run_id": "same",
        "direction": "claude_to_codex",
        "tokens": {"input_tokens": 4, "output_tokens": 1},
        "cost": 0.1,
    }
    _health(store, [row, {**row, "cost": 9}, {"schema_version": 2}])
    (store / "health.jsonl.1").write_text(json.dumps(row) + "\n", encoding="utf-8")
    result = bridge_usage.summarize([store.parents[1]])
    assert result["groups"][0]["attempts"] == 1
    assert result["groups"][0]["known_cost"] == 0.1
    assert result["diagnostics"][1] == {
        "workspace": str(store.parents[1]),
        "source": "health.jsonl",
        "row": 1,
        "code": "duplicate_row",
    }
    assert {item["code"] for item in result["diagnostics"]} == {
        "retained_history_only",
        "duplicate_row",
        "conflicting_duplicate_run",
        "unsupported_health_schema",
    }


def test_empty_execution_and_incomplete_usage_remain_distinct(store: Path) -> None:
    """Do not invent missing spend for predispatch rejection; flag partial observed totals."""
    _health(
        store,
        [
            {"direction": "claude_to_codex", "telemetry": {"plugin_version": "1", "attempts": []}},
            {
                "direction": "codex_to_claude",
                "tokens": {"input_tokens": 2, "output_tokens": 1},
                "cost": 0.1,
                "usage_complete": False,
            },
        ],
    )
    codex, claude = bridge_usage.summarize([store.parents[1]])["groups"]
    assert codex["attempts"] == codex["missing_cost_attempts"] == 0
    assert codex["unknown_spend"] is False
    assert claude["usage_complete"] is False
    assert claude["missing_token_attempts"] == 1
    assert claude["normalized"]["input_tokens_total"] == 2


def test_identity_mismatch_and_missing_transcript_are_diagnostic(store: Path) -> None:
    """Reject evidence for a different call and expose absent direct transcript references."""
    (store / "report.json").write_text(
        json.dumps({"run_id": "other", "telemetry": {"plugin_version": "wrong", "attempts": []}}), encoding="utf-8"
    )
    _health(
        store,
        [
            {
                "run_id": "current",
                "report_path": ".temp/bridge/report.json",
                "transcript_path": ".temp/bridge/missing.txt",
            }
        ],
    )
    result = bridge_usage.summarize([store.parents[1]])
    assert result["groups"][0]["plugin_version"] == "unknown"
    assert {item["code"] for item in result["diagnostics"]} == {
        "retained_history_only",
        "linked_identity_mismatch",
        "missing_transcript",
    }


def test_missing_health_has_explicit_source_location(tmp_path: Path) -> None:
    """Expose absent retained evidence instead of implying zero historical consumption."""
    result = bridge_usage.summarize([tmp_path])
    assert result["diagnostics"] == [
        {"workspace": str(tmp_path), "source": "health.jsonl", "row": 0, "code": "missing_health"}
    ]


def test_oversized_linked_artifact_and_cyclic_incident_chain_are_bounded(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stop oversized reports and repeated direct references without losing terminal usage."""
    monkeypatch.setattr(bridge_usage, "MAX_ARTIFACT_BYTES", 150)
    (store / "report.json").write_text(" " * 151, encoding="utf-8")
    (store / "incidents" / "cycle.json").write_text(
        json.dumps({"prior_incident": ".temp/bridge/incidents/cycle.json", "tokens": {"input_tokens": 1}}),
        encoding="utf-8",
    )
    _health(
        store,
        [
            {
                "direction": "claude_to_codex",
                "report_path": ".temp/bridge/report.json",
                "incident": ".temp/bridge/incidents/cycle.json",
                "tokens": {"input_tokens": 2, "output_tokens": 1},
            }
        ],
    )
    result = bridge_usage.summarize([store.parents[1]])
    assert result["groups"][0]["attempts"] == 2
    assert result["groups"][0]["normalized"]["input_tokens_total"] == 3
    assert {item["code"] for item in result["diagnostics"]} == {
        "retained_history_only",
        "invalid_linked_artifact",
        "invalid_incident_chain",
    }


def test_latency_uses_whole_call_elapsed_without_retry_double_counting(store: Path) -> None:
    """Keep retry execution, whole-call latency, and unknown legacy elapsed distinct."""
    _health(
        store,
        [
            {
                "direction": "claude_to_codex",
                "elapsed_seconds": 10,
                "duration_seconds": 3,
                "telemetry": {"attempts": [{"duration_seconds": 4}, {"duration_seconds": 3}]},
            },
            {
                "direction": "claude_to_codex",
                "elapsed_seconds": 2,
                "duration_seconds": 1,
                "telemetry": {"attempts": [{"duration_seconds": 1}]},
            },
            {"direction": "claude_to_codex", "duration_seconds": 20},
        ],
    )
    group = bridge_usage.summarize([store.parents[1]])["groups"][0]
    assert group["observed_attempt_seconds"] == 28
    assert group["final_attempt_seconds"] == 24
    assert group["elapsed_seconds"] == 12
    assert group["missing_elapsed_calls"] == 1
    assert group["latency_median_seconds"] == 6
    assert group["latency_p95_seconds"] == 10


def test_unknown_legacy_elapsed_has_no_latency_estimate(store: Path) -> None:
    """Never use a legacy final-attempt duration as full-call latency."""
    _health(store, [{"duration_seconds": 9}])
    group = bridge_usage.summarize([store.parents[1]])["groups"][0]
    assert group["observed_attempt_seconds"] == group["final_attempt_seconds"] == 9
    assert group["elapsed_seconds"] == 0
    assert group["missing_elapsed_calls"] == 1
    assert group["latency_median_seconds"] is None
    assert group["latency_p95_seconds"] is None


def test_deeply_nested_report_yields_diagnostic(store: Path) -> None:
    """Treat JSON parser recursion exhaustion as unavailable linked evidence."""
    (store / "nested.json").write_text("[" * 2000 + "0" + "]" * 2000, encoding="utf-8")
    _health(store, [{"report_path": ".temp/bridge/nested.json"}])
    result = bridge_usage.summarize([store.parents[1]])
    assert "invalid_linked_artifact" in {item["code"] for item in result["diagnostics"]}
    assert result["groups"][0]["calls"] == 1


@pytest.mark.parametrize("schema", [True, 1.0])
def test_noninteger_schema_is_not_a_supported_version(store: Path, schema: object) -> None:
    """Boolean and floating schema values must not pass the integer version gate by equality."""
    _health(store, [{"schema_version": schema, "direction": "claude_to_codex"}])
    result = bridge_usage.summarize([store.parent.parent])
    assert result["groups"] == []
    assert any(item["code"] == "unsupported_health_schema" for item in result["diagnostics"])


def _current_row(run_id: str, **changes: object) -> dict:
    """Build a row shaped like the current writer, with every expected round-trip field present."""
    attempt = {
        "attempt": 1,
        "started_at": 100.0,
        "ended_at": 104.0,
        "duration_seconds": 4.0,
        "returncode": 0,
        "stdout_bytes": 10,
        "stderr_bytes": 0,
        "tokens": {"input_tokens": 5, "output_tokens": 2},
        "cost": None,
        "observed_models": ["peer"],
        "usage_complete": True,
        "status": "complete",
    }
    row = {
        "run_id": run_id,
        "direction": "claude_to_codex",
        "verb": "advise",
        "status": "complete",
        "model": "host-default",
        "started_at": 99.5,
        "ts": 105.0,
        "elapsed_seconds": 5.5,
        "task_bytes": 12,
        "response_bytes": 300,
        "tokens": attempt["tokens"],
        "telemetry": {"plugin_version": "0.13.0", "attempts": [attempt]},
    }
    return {**row, **changes}


class TestCallExport:
    """The per-call export lists each call with the expected fields it lacks, never filling them in."""

    def test_default_output_has_no_calls(self, store: Path) -> None:
        """Without the flag the summary keeps its original shape."""
        _health(store, [_current_row("a")])
        assert "calls" not in bridge_usage.summarize([store.parents[1]])

    def test_current_row_has_nothing_missing(self, store: Path) -> None:
        """A call from the current writer carries every field; Codex cost stays an explicit unknown."""
        _health(store, [_current_row("a")])
        [call] = bridge_usage.summarize([store.parents[1]], include_calls=True)["calls"]
        assert call["missing"] == []
        assert call["unknown"] == ["cost"]
        assert (call["started_at"], call["ended_at"], call["observed_models"]) == (99.5, 105.0, ["peer"])
        assert (call["task_bytes"], call["response_bytes"], call["plugin_version"]) == (12, 300, "0.13.0")

    def test_legacy_row_lists_what_it_lacks(self, store: Path) -> None:
        """A retained legacy row stays unattributed and names each absent field."""
        _health(
            store,
            [{"run_id": "old", "direction": "claude_to_codex", "status": "complete", "ts": 1.0, "tokens": {}}],
        )
        [call] = bridge_usage.summarize([store.parents[1]], include_calls=True)["calls"]
        assert {"elapsed_seconds", "started_at", "plugin_version", "attempt_1.started_at"} <= set(call["missing"])
        assert "observed_models" in call["unknown"]
        assert call["plugin_version"] is None

    @pytest.mark.parametrize(
        ("direction", "missing", "unknown"),
        [
            pytest.param("claude_to_codex", [], ["observed_models", "cost"], id="codex-never-reports-model"),
            pytest.param("codex_to_claude", ["observed_models"], ["cost"], id="claude-reports-model"),
        ],
    )
    def test_absent_observed_model_is_provider_specific(
        self, store: Path, direction: str, missing: list[str], unknown: list[str]
    ) -> None:
        """An unreported Codex model is an unknown, while a Claude call without one is a collection gap."""
        row = _current_row("m", direction=direction)
        row["telemetry"]["attempts"][0]["observed_models"] = []
        _health(store, [row])
        [call] = bridge_usage.summarize([store.parents[1]], include_calls=True)["calls"]
        assert (call["missing"], call["unknown"]) == (missing, unknown)

    def test_refusal_without_attempts_is_complete(self, store: Path) -> None:
        """A pre-dispatch refusal executed nothing, so absent attempt and model fields are not gaps."""
        refusal = _current_row("r", status="refused", telemetry={"plugin_version": "0.13.0", "attempts": []})
        _health(store, [refusal])
        [call] = bridge_usage.summarize([store.parents[1]], include_calls=True)["calls"]
        assert (call["missing"], call["attempts"]) == ([], [])

    def test_duplicate_run_is_listed_once(self, store: Path) -> None:
        """A repeated run id is reported as a diagnostic and does not create a second call."""
        _health(store, [_current_row("a"), _current_row("a", status="partial")])
        result = bridge_usage.summarize([store.parents[1]], include_calls=True)
        assert len(result["calls"]) == 1
        assert "conflicting_duplicate_run" in {item["code"] for item in result["diagnostics"]}

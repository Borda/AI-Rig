"""Exercise native session profiling without retaining private transcript content."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

HELPER = Path(__file__).resolve().parents[1] / "skills" / "profile" / "profile.py"


def usage(total: int) -> dict[str, int]:
    """Make internally consistent native counters with cached and reasoning subsets."""
    return {
        "input_tokens": total * 8 // 10,
        "cached_input_tokens": total * 2 // 10,
        "cache_write_input_tokens": 0,
        "output_tokens": total * 2 // 10,
        "reasoning_output_tokens": total // 10,
        "total_tokens": total,
    }


def rollout(path: Path, rows: list[dict[str, Any]]) -> Path:
    """Write a complete native log with source identity and observed model."""
    prefix = [
        {"type": "session_meta", "payload": {"id": path.stem, "base_instructions": "PRIVATE-SENTINEL"}},
        {"type": "turn_context", "payload": {"model": "example-model"}},
    ]
    path.write_bytes(b"".join((json.dumps(row) + "\n").encode() for row in prefix + rows))
    return path


def invoke(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the public installed-style CLI with ordinary process isolation."""
    return subprocess.run([sys.executable, str(HELPER), *arguments], text=True, capture_output=True, check=False)


@pytest.mark.installed_plugin
@pytest.mark.integration
def test_profile_keeps_latest_cumulative_usage_and_inclusive_timing(tmp_path: Path) -> None:
    """Multiple turns and duplicate events cannot inflate tokens or pretend timing is exclusive."""
    tool = {
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "started_at_ms": 1000,
            "completed_at_ms": 4000,
            "item": {
                "id": "tool-a",
                "type": "McpToolCall",
                "duration": {"secs": 3, "nanos": 0},
                "arguments": "PRIVATE-SENTINEL",
            },
        },
    }
    path = rollout(
        tmp_path / "parent.jsonl",
        [
            {"type": "token_usage_record", "payload": {"thread_token_usage": usage(100)}},
            {"type": "event_msg", "payload": {"type": "task_complete", "duration_ms": 5000}},
            tool,
            tool,
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "started_at_ms": 2000,
                    "completed_at_ms": 6000,
                    "item": {"id": "tool-b", "type": "FileChange", "changes": "PRIVATE-SENTINEL"},
                },
            },
            {"type": "token_usage_record", "payload": {"thread_token_usage": usage(200)}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": usage(200)}}},
            {"type": "future_event", "payload": {"private": "PRIVATE-SENTINEL"}},
        ],
    )
    before = path.read_bytes()
    result = invoke("--input", str(path))
    assert result.returncode == 0, result.stderr
    assert "PRIVATE-SENTINEL" not in result.stdout + result.stderr
    data = json.loads(result.stdout)
    session = data["sessions"][0]
    assert session["tokens"]["usage"] == usage(200)
    assert session["tools"] == [
        {"kind": "FileChange", "calls": 1, "timed_calls": 1, "inclusive_duration_ms": 4000.0},
        {"kind": "McpToolCall", "calls": 1, "timed_calls": 1, "inclusive_duration_ms": 3000.0},
    ]
    assert session["cost"]["estimate_usd"] is None
    assert session["capture"]["sha256"] == hashlib.sha256(before).hexdigest()
    assert session["capture"]["bytes"] == len(before)
    assert path.read_bytes() == before
    assert data["aggregation"] == "separate-sessions-no-grand-total"


@pytest.mark.installed_plugin
def test_profile_reports_missing_data_and_counter_resets(tmp_path: Path) -> None:
    """Absent counters stay unavailable, and a reset never becomes a misleading smaller total."""
    empty = rollout(tmp_path / "empty.jsonl", [])
    reset = rollout(
        tmp_path / "reset.jsonl",
        [
            {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": usage(n)}}}
            for n in (200, 100)
        ],
    )
    result = invoke("--input", str(empty), "--input", str(reset))
    assert result.returncode == 0, result.stderr
    first, second = json.loads(result.stdout)["sessions"]
    assert first["tokens"] == {"usage": None, "basis": "unavailable", "reason": "token-usage-not-observed"}
    assert second["tokens"] == {"usage": None, "basis": "unavailable", "reason": "cumulative-counter-decreased"}
    assert first["cost"]["estimate_usd"] is None
    assert second["cost"]["estimate_usd"] is None


@pytest.mark.installed_plugin
def test_profile_estimate_prices_cached_and_output_subsets_once(tmp_path: Path) -> None:
    """Public-rate estimates charge cached input separately and never add reasoning output twice."""
    path = rollout(
        tmp_path / "priced.jsonl",
        [{"type": "token_usage_record", "payload": {"thread_token_usage": usage(1000000), "usage": usage(1000000)}}],
    )
    rates = tmp_path / "rates.json"
    rates.write_text(
        json.dumps(
            {
                "as_of": "2026-10-20",
                "source": "https://example.invalid/pricing",
                "models": {
                    "example-model": {"input_per_million": 2, "cached_input_per_million": 0.5, "output_per_million": 8}
                },
            }
        ),
        encoding="utf-8",
    )
    result = invoke("--input", str(path), "--rates", str(rates))
    assert result.returncode == 0, result.stderr
    cost = json.loads(result.stdout)["sessions"][0]["cost"]
    assert cost["estimate_usd"] == "2.9"
    assert cost["basis"] == "supplied-rates-estimate-not-billing"
    assert cost["rates_sha256"] == hashlib.sha256(rates.read_bytes()).hexdigest()
    with path.open("ab") as handle:
        handle.write(b'{"type":"turn_context","payload":{"model":"another-model"}}\n')
    mixed = invoke("--input", str(path), "--rates", str(rates))
    assert mixed.returncode == 0, mixed.stderr
    assert json.loads(mixed.stdout)["sessions"][0]["cost"]["reason"] == "model-attribution-unavailable"


@pytest.mark.parametrize("counter", [-1, True, "10"])
@pytest.mark.installed_plugin
def test_profile_rejects_malformed_recognized_usage(tmp_path: Path, counter: Any) -> None:
    """Invalid native counters fail without echoing transcript content."""
    values: dict[str, Any] = usage(100)
    values["input_tokens"] = counter
    path = rollout(
        tmp_path / "bad.jsonl",
        [{"type": "token_usage_record", "payload": {"thread_token_usage": values, "private": "PRIVATE-SENTINEL"}}],
    )
    result = invoke("--input", str(path))
    assert result.returncode != 0
    assert "invalid-token-counter" in result.stderr
    assert "PRIVATE-SENTINEL" not in result.stdout + result.stderr


@pytest.mark.installed_plugin
def test_profile_distinguishes_live_tail_from_malformed_complete_row(tmp_path: Path) -> None:
    """Only an incomplete final row may degrade a live capture instead of failing."""
    path = rollout(
        tmp_path / "live.jsonl", [{"type": "token_usage_record", "payload": {"thread_token_usage": usage(100)}}]
    )
    with path.open("ab") as handle:
        handle.write(b'{"private":"PRIVATE-SENTINEL')
    result = invoke("--input", str(path))
    assert result.returncode == 0, result.stderr
    assert "incomplete-final-row" in json.loads(result.stdout)["sessions"][0]["limits"]
    assert "PRIVATE-SENTINEL" not in result.stdout
    with path.open("ab") as handle:
        handle.write(b"\n")
    bad = invoke("--input", str(path))
    assert bad.returncode != 0
    assert "invalid-json-row" in bad.stderr
    assert "PRIVATE-SENTINEL" not in bad.stderr


@pytest.mark.installed_plugin
def test_profile_capture_verification_accepts_growth_and_rejects_changed_prefix(tmp_path: Path) -> None:
    """A retained live prefix stays valid after append, but cannot survive replacement or truncation."""
    specification = importlib.util.spec_from_file_location("session_profile", HELPER)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    path = rollout(tmp_path / "capture.jsonl", [])
    before = path.read_bytes()
    capture = {"bytes": len(before), "sha256": hashlib.sha256(before).hexdigest()}
    with path.open("ab") as handle:
        handle.write(b'{"type":"future_event"}\n')
    assert module.verify_capture(path, capture) == len(b'{"type":"future_event"}\n')
    path.write_bytes(before.replace(b"example-model", b"another-model"))
    with pytest.raises(ValueError, match="capture-prefix-changed"):
        module.verify_capture(path, capture)
    path.write_bytes(before[:10])
    with pytest.raises(ValueError, match="capture-prefix-truncated"):
        module.verify_capture(path, capture)


@pytest.mark.parametrize(
    "rate", [-1, True, float("nan"), float("inf"), pytest.param(10**400, id="overflowing-integer")]
)
@pytest.mark.installed_plugin
def test_profile_rejects_invalid_rates_without_raw_diagnostics(tmp_path: Path, rate: Any) -> None:
    """Malformed or nonfinite pricing cannot produce an apparently valid dollar estimate."""
    path = rollout(tmp_path / "rates-source.jsonl", [])
    rates = tmp_path / "invalid-rates.json"
    rates.write_text(
        json.dumps(
            {
                "as_of": "2026-10-20",
                "source": "PRIVATE-SENTINEL",
                "models": {
                    "example-model": {"input_per_million": rate, "cached_input_per_million": 1, "output_per_million": 1}
                },
            }
        ),
        encoding="utf-8",
    )
    result = invoke("--input", str(path), "--rates", str(rates))
    assert result.returncode == 1
    assert result.stderr == "profile: invalid-rate-sheet\n"
    assert "PRIVATE-SENTINEL" not in result.stdout + result.stderr


@pytest.mark.installed_plugin
def test_profile_legacy_usage_and_missing_duration_remain_distinct(tmp_path: Path) -> None:
    """Legacy counters are supported, while a completed call lacking timing is not zero milliseconds."""
    path = rollout(
        tmp_path / "legacy.jsonl",
        [
            {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": usage(100)}}},
            {
                "type": "event_msg",
                "payload": {"type": "item_completed", "item": {"id": "untimed", "type": "CommandExecution"}},
            },
        ],
    )
    result = invoke("--input", str(path))
    assert result.returncode == 0, result.stderr
    session = json.loads(result.stdout)["sessions"][0]
    assert session["tokens"]["usage"] == usage(100)
    assert session["tokens"]["basis"] == "legacy_total_token_usage"
    assert session["tools"] == [
        {"kind": "CommandExecution", "calls": 1, "timed_calls": 0, "inclusive_duration_ms": None}
    ]


@pytest.mark.installed_plugin
def test_profile_does_not_count_duplicate_session_inputs(tmp_path: Path) -> None:
    """Two files claiming one session are rejected instead of presenting duplicate spend rows."""
    first = rollout(tmp_path / "first.jsonl", [])
    second = tmp_path / "second.jsonl"
    second.write_bytes(first.read_bytes())
    result = invoke("--input", str(first), "--input", str(second))
    assert result.returncode == 1
    assert result.stderr == "profile: duplicate-session-identity\n"


@pytest.mark.installed_plugin
def test_profile_report_checker_rejects_wrong_field_types_without_traceback(tmp_path: Path) -> None:
    """Untrusted retained metadata must fail with a bounded diagnostic rather than a parser traceback."""
    source = rollout(
        tmp_path / "source.jsonl", [{"type": "token_usage_record", "payload": {"thread_token_usage": usage(100)}}]
    )
    report = json.loads(invoke("--input", str(source)).stdout)
    report["sessions"][0]["tokens"]["basis"] = ["PRIVATE-SENTINEL"]
    retained = tmp_path / "wrong-types.json"
    retained.write_text(json.dumps(report), encoding="utf-8")
    checked = invoke("--check-report", str(retained))
    assert checked.returncode == 1
    assert checked.stderr == "profile: invalid-profile-report\n"
    assert "PRIVATE-SENTINEL" not in checked.stdout + checked.stderr


@pytest.mark.parametrize("coordinate", ["C:\\sessions\\selected.jsonl", "/srv/sessions/selected.jsonl"])
@pytest.mark.installed_plugin
def test_profile_report_coordinates_are_portable_data_not_reopened_paths(tmp_path: Path, coordinate: str) -> None:
    """A retained report remains verifiable across hosts without reading its external source coordinate."""
    source = rollout(
        tmp_path / "source.jsonl", [{"type": "token_usage_record", "payload": {"thread_token_usage": usage(100)}}]
    )
    report = json.loads(invoke("--input", str(source)).stdout)
    report["sessions"][0]["source"] = coordinate
    retained = tmp_path / "portable.json"
    encoded = json.dumps(report).encode()
    retained.write_bytes(encoded)
    source.unlink()
    checked = invoke("--check-report", str(retained))
    assert checked.returncode == 0, checked.stderr
    assert json.loads(checked.stdout)["rows"][0][1] == "100 total"
    assert retained.read_bytes() == encoded


@pytest.mark.parametrize("missing_coverage", ["unknown-model", "partial-prefix", "missing-increment"])
@pytest.mark.installed_plugin
def test_profile_never_prices_incompletely_attributed_counters(tmp_path: Path, missing_coverage: str) -> None:
    """A later known model cannot supply attribution for unknown or uncaptured earlier usage."""
    first = {"thread_token_usage": usage(100), "usage": usage(20 if missing_coverage == "partial-prefix" else 100)}
    if missing_coverage == "missing-increment":
        first.pop("usage")
    rows = []
    if missing_coverage == "unknown-model":
        rows.append({"type": "turn_context", "payload": {}})
    rows.extend(
        [
            {"type": "token_usage_record", "payload": first},
            {"type": "turn_context", "payload": {"model": "example-model"}},
            {"type": "token_usage_record", "payload": {"thread_token_usage": usage(200), "usage": usage(100)}},
        ]
    )
    source = rollout(tmp_path / "incomplete.jsonl", rows)
    rates = tmp_path / "rates.json"
    rates.write_text(
        json.dumps(
            {
                "as_of": "2026-10-20",
                "source": "https://example.invalid/pricing",
                "models": {
                    "example-model": {"input_per_million": 2, "cached_input_per_million": 0.5, "output_per_million": 8}
                },
            }
        ),
        encoding="utf-8",
    )
    result = invoke("--input", str(source), "--rates", str(rates))
    assert result.returncode == 0, result.stderr
    session = json.loads(result.stdout)["sessions"][0]
    assert session["tokens"]["usage"] == usage(200)
    assert session["cost"]["estimate_usd"] is None
    assert session["cost"]["reason"] == "model-attribution-unavailable"


@pytest.mark.installed_plugin
@pytest.mark.integration
@pytest.mark.parametrize(
    ("replacement", "branch"),
    [
        pytest.param("empty", "standard", id="standard-empty"),
        pytest.param("malformed", "standard", id="standard-malformed"),
        pytest.param("different-session", "standard", id="standard-contradictory-session"),
        pytest.param("unavailable", "standard", id="standard-unavailable"),
        pytest.param("result-schema", "standard", id="standard-result-schema"),
        pytest.param("cache-write", "standard", id="standard-invalid-cache-write"),
        pytest.param("empty", "caller-contract", id="exact-empty"),
        pytest.param("malformed", "caller-contract", id="exact-malformed"),
        pytest.param("unavailable", "caller-contract", id="exact-unavailable"),
        pytest.param("result-schema", "caller-contract", id="exact-result-schema"),
        pytest.param("cache-write", "caller-contract", id="exact-invalid-cache-write"),
    ],
)
def test_profile_report_reaches_canonical_artifact_promotion(tmp_path: Path, replacement: str, branch: str) -> None:
    """A reader report must render, bind, validate and promote through ordinary shared consumers."""
    fixture_path = HELPER.parents[2] / "tests" / "review" / "test_final_handoff.py"
    specification = importlib.util.spec_from_file_location("profile_handoff_fixture", fixture_path)
    assert specification is not None
    assert specification.loader is not None
    fixtures = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(fixtures)
    source = rollout(
        tmp_path / "selected.jsonl", [{"type": "token_usage_record", "payload": {"thread_token_usage": usage(200)}}]
    )
    observed = invoke("--input", str(source))
    assert observed.returncode == 0, observed.stderr
    (tmp_path / "profile.json").write_text(observed.stdout, encoding="utf-8")
    (tmp_path / "profile-report.md").write_text(
        "# Scope\nSelected log.\n# Observed usage\n200 total tokens.\n# Inclusive timing\nUnavailable.\n# Cost basis\nNo rates.\n# Coverage and limits\nSelected prefix only.\n",
        encoding="utf-8",
    )
    candidate = fixtures._write_schema_v2_assess(tmp_path)
    handoff_path = tmp_path / "final-handoff.json"
    payload = json.loads(handoff_path.read_text())
    payload["skill"] = "profile"
    payload["presentation_version"] = 10
    payload["outcome"] = {
        "title": "Profile",
        "summary": "Selected native usage captured; timing and rates unavailable.",
    }
    payload["tables"][0].update(
        heading="Results",
        columns=["Session / agent", "Observed tokens", "Inclusive tool time", "Cost estimate", "Coverage limit"],
    )
    payload["tables"][0]["rows"][0]["cells"] = [
        "session-1",
        "200 total",
        "Unavailable",
        "Unavailable",
        "Selected prefix only",
    ]
    payload["source_records"][0]["evidence"] = "profile.json"
    if branch == "caller-contract":
        payload["branch"] = branch
        payload["tables"] = []
        payload["source_records"] = []
        payload["source_coverage"] = {
            "source_records_total": 0,
            "represented_source_records_total": 0,
            "omitted_source_records_total": 0,
        }
        payload["caller_contract"] = {
            "format": "application/json",
            "evidence": "User explicitly requested JSON only.",
            "output": '{"status":"ok"}\n',
        }
    gap = "(-0.05) Selected prefix does not establish omitted agents."
    payload["confidence"]["limits"] = [gap]
    payload["confidence"]["gaps"][0].update(gap=gap, rationale="Only the selected native source was read.")
    handoff_path.write_text(json.dumps(payload), encoding="utf-8")
    binding = fixtures._load_finalizer().render_files(
        handoff_path, tmp_path / "final.md", tmp_path / "final-handoff.validation.json"
    )
    metadata = json.loads(candidate.read_text())["metadata"]
    metadata["confidence_gaps"] = [gap]
    metadata["confidence_gap_closures"][0].update(gap=gap, rationale="Only the selected native source was read.")
    metadata["confidence_recovery"]["remaining_limits"] = [gap]
    metadata["final_handoff"].update({key: binding[key] for key in ("handoff_sha256", "rendered_sha256")})
    metadata["final_handoff"]["branch"] = branch
    metadata["profile"] = {
        "schema_version": 1,
        "profile_sha256": hashlib.sha256((tmp_path / "profile.json").read_bytes()).hexdigest(),
    }
    writer = subprocess.run(
        [
            sys.executable,
            str(HELPER.parents[2] / "shared" / "write-result.py"),
            "--out",
            str(candidate),
            "--gates",
            str(tmp_path / "gates.json"),
            "--status",
            "pass",
            "--checks-run",
            "lint,format,types,tests,review",
            "--confidence",
            "0.95",
            "--artifact-path",
            str(tmp_path / "result.json"),
            "--metadata",
            json.dumps(metadata),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert writer.returncode == 0, writer.stderr
    assert json.loads(candidate.read_text())["schema_version"] == 2
    validator = fixtures._load_shared_validator()
    validator.validate("profile", tmp_path, candidate)
    canonical = tmp_path / "result.json"
    candidate.replace(canonical)
    validator.validate("profile", tmp_path, canonical)
    if branch == "caller-contract":
        assert (tmp_path / "final.md").read_text() == '{"status":"ok"}\n'
    else:
        assert "200 total" in (tmp_path / "final.md").read_text()
    if replacement == "result-schema":
        original_result = canonical.read_text()
        (tmp_path / "final.md").write_text("Unbound response\n", encoding="utf-8")
        (tmp_path / "final-handoff.validation.json").unlink()
        for name in ("result.candidate.json", "result.json"):
            for schema in (None, 1, True, 2.0):
                changed_result = json.loads(original_result)
                if schema is None:
                    changed_result.pop("schema_version")
                else:
                    changed_result["schema_version"] = schema
                changed_result["metadata"].pop("final_handoff")
                target = tmp_path / name
                target.write_text(json.dumps(changed_result), encoding="utf-8")
                with pytest.raises(SystemExit, match="profile-result-schema-version"):
                    validator.validate("profile", tmp_path, target)
        return
    original = (tmp_path / "profile.json").read_bytes()
    if replacement == "different-session":
        other = rollout(
            tmp_path / "other.jsonl", [{"type": "token_usage_record", "payload": {"thread_token_usage": usage(300)}}]
        )
        changed = invoke("--input", str(other)).stdout.encode()
    elif replacement == "cache-write":
        changed_report = json.loads(original)
        counters = changed_report["sessions"][0]["tokens"]["usage"]
        counters["cache_write_input_tokens"] = counters["input_tokens"] + 1
        changed = json.dumps(changed_report).encode()
    else:
        changed = {
            "empty": b"{}",
            "malformed": b"not JSON",
            "unavailable": b'{"schema_version":1,"aggregation":"separate-sessions-no-grand-total","sessions":[]}',
        }[replacement]
    (tmp_path / "profile.json").write_bytes(changed)
    with pytest.raises(SystemExit, match="profile-"):
        validator.validate("profile", tmp_path, canonical)
    # Rebinding changed bytes still cannot make a malformed report or contradictory published session valid.
    result = json.loads(canonical.read_text())
    result["metadata"]["profile"]["profile_sha256"] = hashlib.sha256(changed).hexdigest()
    for name in ("result.candidate.json", "result.json"):
        target = tmp_path / name
        target.write_text(json.dumps(result), encoding="utf-8")
        with pytest.raises(SystemExit, match="profile-"):
            validator.validate("profile", tmp_path, target)
    (tmp_path / "profile.json").write_bytes(original)
    (tmp_path / "profile.json").unlink()
    with pytest.raises(SystemExit, match=r"missing-artifact:.*[/\\]profile\.json$"):
        validator.validate("profile", tmp_path, canonical)


@pytest.mark.installed_plugin
@pytest.mark.parametrize("cache_write", [None, 0, 80, 81])
def test_profile_cache_write_subset_boundary(tmp_path: Path, cache_write: int | None) -> None:
    """Observed cache writes fit input tokens; absent writes remain unavailable rather than zero."""
    counters: dict[str, Any] = usage(100)
    counters["cache_write_input_tokens"] = cache_write
    source = rollout(
        tmp_path / "cache.jsonl", [{"type": "token_usage_record", "payload": {"thread_token_usage": counters}}]
    )
    before = source.read_bytes()
    result = invoke("--input", str(source))
    assert source.read_bytes() == before
    if cache_write == 81:
        assert result.returncode == 1
        assert result.stdout == ""
        assert "token-subset-exceeds-total" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["sessions"][0]["tokens"]["usage"]["cache_write_input_tokens"] == cache_write


@pytest.mark.installed_plugin
@pytest.mark.parametrize("surface", ["telemetry", "telemetry-no-newline", "rates", "report"])
def test_profile_deep_json_has_bounded_diagnostics(tmp_path: Path, surface: str) -> None:
    """Decoder depth failures on older supported Python versions cannot escape as tracebacks."""
    nested = "[" * 2000 + '"PRIVATE-SENTINEL"' + "]" * 2000
    source = tmp_path / "deep.json"
    if surface.startswith("telemetry"):
        raw = '{"type":"token_usage_record","payload":{"thread_token_usage":' + nested + "}}"
        if surface == "telemetry":
            raw += "\n"
        arguments = ("--input", str(source))
    elif surface == "rates":
        valid = rollout(tmp_path / "valid.jsonl", [])
        raw = '{"as_of":"2026-10-20","source":"fixture","models":' + nested + "}\n"
        arguments = ("--input", str(valid), "--rates", str(source))
    else:
        raw = '{"schema_version":1,"aggregation":"separate-sessions-no-grand-total","sessions":' + nested + "}\n"
        arguments = ("--check-report", str(source))
    source.write_bytes(raw.encode())
    before = source.read_bytes()
    result = invoke(*arguments)
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.startswith("profile: ")
    assert len(result.stderr) < 200
    assert "Traceback" not in result.stderr
    assert "PRIVATE-SENTINEL" not in result.stderr
    assert source.read_bytes() == before


@pytest.mark.installed_plugin
def test_profile_unavailable_report_does_not_invent_a_session_or_zero(tmp_path: Path) -> None:
    """Missing telemetry has a stable unavailable row, never a fabricated measured session."""
    retained = tmp_path / "unavailable.json"
    retained.write_bytes(b'{"schema_version":1,"aggregation":"separate-sessions-no-grand-total","sessions":[]}')
    checked = invoke("--check-report", str(retained))
    assert checked.returncode == 0, checked.stderr
    assert json.loads(checked.stdout) == {
        "observed": False,
        "rows": [["Unavailable", "Unavailable", "Unavailable", "Unavailable", "No selected telemetry"]],
    }

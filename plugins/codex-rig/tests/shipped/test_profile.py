"""Exercise native session profiling without retaining private transcript content."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from _modules import load_module

HELPER = Path(__file__).resolve().parents[2] / "skills" / "profile" / "profile.py"
SHARED = HELPER.parents[2] / "shared"
HANDOFF_FIXTURES = HELPER.parents[2] / "tests" / "review" / "handoff" / "test_final_handoff.py"
UNAVAILABLE_REPORT = b'{"schema_version":1,"aggregation":"separate-sessions-no-grand-total","sessions":[]}'
NESTED_JSON = "[" * 2000 + '"PRIVATE-SENTINEL"' + "]" * 2000
_MISSING = object()


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


def token_row(**payload: Any) -> dict[str, Any]:
    """Make a native token-usage record carrying the given payload fields."""
    return {"type": "token_usage_record", "payload": payload}


def rollout(path: Path, rows: list[dict[str, Any]]) -> Path:
    """Write a complete native log with source identity and observed model."""
    prefix = [
        {"type": "session_meta", "payload": {"id": path.stem, "base_instructions": "PRIVATE-SENTINEL"}},
        {"type": "turn_context", "payload": {"model": "example-model"}},
    ]
    path.write_bytes(b"".join((json.dumps(row) + "\n").encode() for row in prefix + rows))
    return path


def usage_source(tmp_path: Path, total: int = 100, name: str = "source.jsonl") -> Path:
    """Write a native log whose only usage record reports one cumulative total."""
    return rollout(tmp_path / name, [token_row(thread_token_usage=usage(total))])


def write_rates(path: Path, source: str = "https://example.invalid/pricing", **rates: Any) -> Path:
    """Write a public-rate sheet for the example model, with the given per-million rates overriding the defaults."""
    model = {"input_per_million": 2, "cached_input_per_million": 0.5, "output_per_million": 8, **rates}
    path.write_text(
        json.dumps({"as_of": "2026-10-20", "source": source, "models": {"example-model": model}}), encoding="utf-8"
    )
    return path


def invoke(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the public installed-style CLI with ordinary process isolation."""
    return subprocess.run([sys.executable, str(HELPER), *arguments], text=True, capture_output=True, check=False)


@pytest.fixture
def profile_module() -> ModuleType:
    """Load the profile helper as a module for checks of its in-process functions."""
    return load_module("session_profile", HELPER)


@pytest.fixture(scope="module")
def handoff_fixtures() -> ModuleType:
    """Load the review suite's final-handoff fixtures once; they only build files and never hold state."""
    return load_module("profile_handoff_fixture", HANDOFF_FIXTURES)


@pytest.mark.installed_plugin
@pytest.mark.integration
def test_profile_keeps_latest_cumulative_usage_and_inclusive_timing(tmp_path: Path) -> None:
    """Multiple turns and duplicate events cannot inflate tokens or pretend timing is exclusive.

    A log with two cumulative token records, a duplicated completed tool event, a second tool and an unknown future
    event must report the latest totals once, each tool's own inclusive duration, no cost, and the hash of untouched
    bytes.
    """
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
            token_row(thread_token_usage=usage(100)),
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
            token_row(thread_token_usage=usage(200)),
            {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": usage(200)}}},
            {"type": "future_event", "payload": {"private": "PRIVATE-SENTINEL"}},
        ],
    )
    before = path.read_bytes()

    result = invoke("--input", str(path))

    data = json.loads(result.stdout)
    session = data["sessions"][0]
    assert result.returncode == 0, result.stderr
    assert "PRIVATE-SENTINEL" not in result.stdout + result.stderr
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
    """Absent counters stay unavailable, and a reset never becomes a misleading smaller total.

    One log with no usage and one whose cumulative counter falls from 200 to 100 are profiled together; both must say
    why tokens are unavailable and neither may carry a cost estimate.
    """
    empty = rollout(tmp_path / "empty.jsonl", [])
    reset = rollout(
        tmp_path / "reset.jsonl",
        [
            {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": usage(n)}}}
            for n in (200, 100)
        ],
    )

    result = invoke("--input", str(empty), "--input", str(reset))

    first, second = json.loads(result.stdout)["sessions"]
    assert result.returncode == 0, result.stderr
    assert first["tokens"] == {"usage": None, "basis": "unavailable", "reason": "token-usage-not-observed"}
    assert second["tokens"] == {"usage": None, "basis": "unavailable", "reason": "cumulative-counter-decreased"}
    assert first["cost"]["estimate_usd"] is None
    assert second["cost"]["estimate_usd"] is None


@pytest.fixture
def priced_source(tmp_path: Path) -> tuple[Path, Path]:
    """Provide a one-million-token log and the rate sheet that prices its model."""
    source = rollout(tmp_path / "priced.jsonl", [token_row(thread_token_usage=usage(1000000), usage=usage(1000000))])
    return source, write_rates(tmp_path / "rates.json")


@pytest.mark.installed_plugin
def test_profile_estimate_prices_cached_and_output_subsets_once(priced_source: tuple[Path, Path]) -> None:
    """Public-rate estimates charge cached input separately and never add reasoning output twice.

    A million tokens at the example rates (2 input, 0.5 cached, 8 output per million) must cost exactly 2.9 dollars and
    record the basis and the rate sheet's hash.
    """
    source, rates = priced_source

    result = invoke("--input", str(source), "--rates", str(rates))

    cost = json.loads(result.stdout)["sessions"][0]["cost"]
    assert result.returncode == 0, result.stderr
    assert cost["estimate_usd"] == "2.9"
    assert cost["basis"] == "supplied-rates-estimate-not-billing"
    assert cost["rates_sha256"] == hashlib.sha256(rates.read_bytes()).hexdigest()


@pytest.mark.installed_plugin
def test_profile_estimate_is_withheld_when_a_second_model_appears(priced_source: tuple[Path, Path]) -> None:
    """A log that later switches to another model cannot be priced against one model's rates."""
    source, rates = priced_source
    with source.open("ab") as handle:
        handle.write(b'{"type":"turn_context","payload":{"model":"another-model"}}\n')

    result = invoke("--input", str(source), "--rates", str(rates))

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["sessions"][0]["cost"]["reason"] == "model-attribution-unavailable"


@pytest.mark.parametrize("counter", [-1, True, "10"])
@pytest.mark.installed_plugin
def test_profile_rejects_malformed_recognized_usage(tmp_path: Path, counter: Any) -> None:
    """Invalid native counters fail without echoing transcript content.

    A negative, boolean or string input-token counter in an otherwise valid usage record is a malformed measurement; the
    run fails naming the problem and never repeats the private text sitting beside it.
    """
    values: dict[str, Any] = usage(100)
    values["input_tokens"] = counter
    path = rollout(tmp_path / "bad.jsonl", [token_row(thread_token_usage=values, private="PRIVATE-SENTINEL")])

    result = invoke("--input", str(path))

    assert result.returncode != 0
    assert "invalid-token-counter" in result.stderr
    assert "PRIVATE-SENTINEL" not in result.stdout + result.stderr


@pytest.fixture
def live_capture(tmp_path: Path) -> Path:
    """Provide a log still being written: valid rows followed by an unterminated final row."""
    path = usage_source(tmp_path, name="live.jsonl")
    with path.open("ab") as handle:
        handle.write(b'{"private":"PRIVATE-SENTINEL')
    return path


@pytest.mark.installed_plugin
def test_profile_degrades_for_an_incomplete_final_row(live_capture: Path) -> None:
    """Only an incomplete final row may degrade a live capture instead of failing.

    A writer is mid-row when the log is read; the profile still succeeds, records the limit, and leaves the private
    partial row out of its output.
    """
    result = invoke("--input", str(live_capture))

    assert result.returncode == 0, result.stderr
    assert "incomplete-final-row" in json.loads(result.stdout)["sessions"][0]["limits"]
    assert "PRIVATE-SENTINEL" not in result.stdout


@pytest.mark.installed_plugin
def test_profile_rejects_a_malformed_complete_row(live_capture: Path) -> None:
    """A malformed row that is newline-terminated is corruption, not a live tail, and fails the run.

    The same partial row as above becomes complete once its newline lands; it must now be refused without echoing it.
    """
    with live_capture.open("ab") as handle:
        handle.write(b"\n")

    result = invoke("--input", str(live_capture))

    assert result.returncode != 0
    assert "invalid-json-row" in result.stderr
    assert "PRIVATE-SENTINEL" not in result.stderr


class TestVerifyCapture:
    """Retained-prefix verification of a growing log, checked in process."""

    @pytest.fixture
    def retained(self, tmp_path: Path) -> tuple[Path, bytes, dict[str, Any]]:
        """Provide a log with a recorded capture of its current bytes (length and digest)."""
        path = rollout(tmp_path / "capture.jsonl", [])
        before = path.read_bytes()
        return path, before, {"bytes": len(before), "sha256": hashlib.sha256(before).hexdigest()}

    @pytest.mark.installed_plugin
    def test_accepts_appended_growth(self, profile_module: ModuleType, retained: tuple[Path, bytes, dict]) -> None:
        """A retained live prefix stays valid after more rows are appended, and the growth length is returned."""
        path, _, capture = retained
        with path.open("ab") as handle:
            handle.write(b'{"type":"future_event"}\n')

        assert profile_module.verify_capture(path, capture) == len(b'{"type":"future_event"}\n')

    @pytest.mark.installed_plugin
    def test_rejects_a_changed_prefix(self, profile_module: ModuleType, retained: tuple[Path, bytes, dict]) -> None:
        """Replacing bytes inside the retained prefix invalidates the capture even when the file is not shorter."""
        path, before, capture = retained
        path.write_bytes(before.replace(b"example-model", b"another-model"))

        with pytest.raises(ValueError, match="capture-prefix-changed"):
            profile_module.verify_capture(path, capture)

    @pytest.mark.installed_plugin
    def test_rejects_a_truncated_prefix(self, profile_module: ModuleType, retained: tuple[Path, bytes, dict]) -> None:
        """A file cut shorter than the retained prefix can no longer prove the capture."""
        path, before, capture = retained
        path.write_bytes(before[:10])

        with pytest.raises(ValueError, match="capture-prefix-truncated"):
            profile_module.verify_capture(path, capture)


@pytest.mark.parametrize(
    "rate", [-1, True, float("nan"), float("inf"), pytest.param(10**400, id="overflowing-integer")]
)
@pytest.mark.installed_plugin
def test_profile_rejects_invalid_rates_without_raw_diagnostics(tmp_path: Path, rate: Any) -> None:
    """Malformed or nonfinite pricing cannot produce an apparently valid dollar estimate.

    The sheet's private source text must not leak into the one-line diagnostic whichever invalid input rate is supplied.
    """
    path = rollout(tmp_path / "rates-source.jsonl", [])
    rates = write_rates(
        tmp_path / "invalid-rates.json",
        source="PRIVATE-SENTINEL",
        input_per_million=rate,
        cached_input_per_million=1,
        output_per_million=1,
    )

    result = invoke("--input", str(path), "--rates", str(rates))

    assert result.returncode == 1
    assert result.stderr == "profile: invalid-rate-sheet\n"
    assert "PRIVATE-SENTINEL" not in result.stdout + result.stderr


@pytest.mark.installed_plugin
def test_profile_legacy_usage_and_missing_duration_remain_distinct(tmp_path: Path) -> None:
    """Legacy counters are supported, while a completed call lacking timing is not zero milliseconds.

    A log written with only the older total-usage events and a completed command carrying no duration must profile with
    the legacy basis and an unavailable (not zero) inclusive time.
    """
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

    session = json.loads(result.stdout)["sessions"][0]
    assert result.returncode == 0, result.stderr
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
    """Untrusted retained metadata must fail with a bounded diagnostic rather than a parser traceback.

    A retained report whose token basis is a list holding private text is checked; the one-line diagnostic must not echo
    the list.
    """
    report = json.loads(invoke("--input", str(usage_source(tmp_path))).stdout)
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
    """A retained report remains verifiable across hosts without reading its external source coordinate.

    The recorded source path is a Windows or POSIX string from another machine; checking the report must treat it as
    data (the original log is deleted) and must not rewrite the retained file.
    """
    source = usage_source(tmp_path)
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


@pytest.mark.parametrize(
    ("leading_rows", "first_payload"),
    [
        pytest.param(
            [{"type": "turn_context", "payload": {}}],
            {"thread_token_usage": usage(100), "usage": usage(100)},
            id="unknown-model",
        ),
        pytest.param([], {"thread_token_usage": usage(100), "usage": usage(20)}, id="partial-prefix"),
        pytest.param([], {"thread_token_usage": usage(100)}, id="missing-increment"),
    ],
)
@pytest.mark.installed_plugin
def test_profile_never_prices_incompletely_attributed_counters(
    tmp_path: Path, leading_rows: list[dict[str, Any]], first_payload: dict[str, Any]
) -> None:
    """A later known model cannot supply attribution for unknown or uncaptured earlier usage.

    The first usage record has no known model, covers only part of its total, or lacks its increment; a second record
    under a known model follows. The total stays reported but no dollar estimate may be produced.
    """
    rows = [
        *leading_rows,
        token_row(**first_payload),
        {"type": "turn_context", "payload": {"model": "example-model"}},
        token_row(thread_token_usage=usage(200), usage=usage(100)),
    ]
    source = rollout(tmp_path / "incomplete.jsonl", rows)
    rates = write_rates(tmp_path / "rates.json")

    result = invoke("--input", str(source), "--rates", str(rates))

    session = json.loads(result.stdout)["sessions"][0]
    assert result.returncode == 0, result.stderr
    assert session["tokens"]["usage"] == usage(200)
    assert session["cost"]["estimate_usd"] is None
    assert session["cost"]["reason"] == "model-attribution-unavailable"


#: Fields replacing a standard tabular handoff with a caller-requested JSON-only reply.
CALLER_CONTRACT = {
    "branch": "caller-contract",
    "tables": [],
    "source_records": [],
    "source_coverage": {
        "source_records_total": 0,
        "represented_source_records_total": 0,
        "omitted_source_records_total": 0,
    },
    "caller_contract": {
        "format": "application/json",
        "evidence": "User explicitly requested JSON only.",
        "output": '{"status":"ok"}\n',
    },
}
BRANCHES = [
    pytest.param("standard", {}, id="standard"),
    pytest.param("caller-contract", CALLER_CONTRACT, id="exact"),
]


def _promote_profile(
    tmp_path: Path, fixtures: ModuleType, branch: str, handoff_changes: dict[str, Any]
) -> tuple[ModuleType, Path]:
    """Render, bind, validate and promote a profile result through the ordinary shared consumers.

    Returns the shared validator and the canonical result path, with `profile.json` and `final.md` left in `tmp_path`.
    """
    source = usage_source(tmp_path, 200, "selected.jsonl")
    (tmp_path / "profile.json").write_text(invoke("--input", str(source)).stdout, encoding="utf-8")
    (tmp_path / "profile-report.md").write_text(
        "# Scope\nSelected log.\n# Observed usage\n200 total tokens.\n# Inclusive timing\nUnavailable.\n"
        "# Cost basis\nNo rates.\n# Coverage and limits\nSelected prefix only.\n",
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
    payload.update(handoff_changes)
    gap = "(-0.05) Selected prefix does not establish omitted agents."
    rationale = "Only the selected native source was read."
    payload["confidence"]["limits"] = [gap]
    payload["confidence"]["gaps"][0].update(gap=gap, rationale=rationale)
    handoff_path.write_text(json.dumps(payload), encoding="utf-8")
    binding = fixtures._load_finalizer().render_files(
        handoff_path, tmp_path / "final.md", tmp_path / "final-handoff.validation.json"
    )
    metadata = json.loads(candidate.read_text())["metadata"]
    metadata["confidence_gaps"] = [gap]
    metadata["confidence_gap_closures"][0].update(gap=gap, rationale=rationale)
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
            str(SHARED / "write-result.py"),
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
    return validator, canonical


def _exit_messages(validator: ModuleType, tmp_path: Path, results: list[tuple[str, dict[str, Any]]]) -> list[str]:
    """Validate each result written under its file name and return every refusal message.

    An accepted result contributes the text `accepted`, so a missing refusal shows up in the comparison.
    """
    messages = []
    for name, result in results:
        target = tmp_path / name
        target.write_text(json.dumps(result), encoding="utf-8")
        try:
            validator.validate("profile", tmp_path, target)
        except SystemExit as refusal:
            messages.append(str(refusal))
        else:
            messages.append("accepted")
    return messages


def _different_session_report(tmp_path: Path, original: bytes) -> bytes:
    """Return the profile of a different session, which contradicts the published one."""
    return invoke("--input", str(usage_source(tmp_path, 300, "other.jsonl"))).stdout.encode()


def _oversized_cache_write_report(tmp_path: Path, original: bytes) -> bytes:
    """Return the published report with cache writes exceeding input tokens, a contradictory subset."""
    report = json.loads(original)
    counters = report["sessions"][0]["tokens"]["usage"]
    counters["cache_write_input_tokens"] = counters["input_tokens"] + 1
    return json.dumps(report).encode()


REPLACEMENTS = {
    "empty": lambda tmp_path, original: b"{}",
    "malformed": lambda tmp_path, original: b"not JSON",
    "different-session": _different_session_report,
    "unavailable": lambda tmp_path, original: UNAVAILABLE_REPORT,
    "invalid-cache-write": _oversized_cache_write_report,
}
#: Branch and replacement pairs; a caller-contract reply publishes no session for a different one to contradict.
REPLACED_REPORTS = [
    pytest.param(branch, changes, replacement, id=f"{label}-{name}")
    for label, branch, changes in (("standard", "standard", {}), ("exact", "caller-contract", CALLER_CONTRACT))
    for name, replacement in REPLACEMENTS.items()
    if (label, name) != ("exact", "different-session")
]


@pytest.mark.installed_plugin
@pytest.mark.integration
class TestProfilePromotion:
    """A profile report promotes through ordinary rendering, binding, validation and promotion."""

    def test_standard_report_renders_observed_totals(self, tmp_path: Path, handoff_fixtures: ModuleType) -> None:
        """A tabular profile handoff is bound, validated, promoted, and renders the observed total.

        The reader report plus a standard handoff must pass the shared validator both as a candidate and as the promoted
        result, and the rendered reply must carry the session's total tokens.
        """
        _promote_profile(tmp_path, handoff_fixtures, "standard", {})

        assert "200 total" in (tmp_path / "final.md").read_text()

    def test_caller_contract_reply_is_rendered_verbatim(self, tmp_path: Path, handoff_fixtures: ModuleType) -> None:
        """A caller-requested JSON-only reply is promoted and rendered exactly as the caller specified."""
        _promote_profile(tmp_path, handoff_fixtures, "caller-contract", CALLER_CONTRACT)

        assert (tmp_path / "final.md").read_text() == '{"status":"ok"}\n'

    @pytest.mark.parametrize(("branch", "handoff_changes", "replacement"), REPLACED_REPORTS)
    def test_replaced_report_is_refused(
        self, tmp_path: Path, handoff_fixtures: ModuleType, branch: str, handoff_changes: dict, replacement: Callable
    ) -> None:
        """Replacing the bound profile report with an empty, malformed, contradictory or unavailable one is refused.

        After promotion the retained `profile.json` no longer matches its recorded hash and, for the contradictory
        variants, cannot describe the published session either.
        """
        validator, canonical = _promote_profile(tmp_path, handoff_fixtures, branch, handoff_changes)
        original = (tmp_path / "profile.json").read_bytes()
        (tmp_path / "profile.json").write_bytes(replacement(tmp_path, original))

        with pytest.raises(SystemExit, match="profile-"):
            validator.validate("profile", tmp_path, canonical)

    @pytest.mark.parametrize(("branch", "handoff_changes", "replacement"), REPLACED_REPORTS)
    def test_rebinding_a_replaced_report_cannot_make_it_valid(
        self, tmp_path: Path, handoff_fixtures: ModuleType, branch: str, handoff_changes: dict, replacement: Callable
    ) -> None:
        """Rebinding changed bytes cannot make a malformed report or contradictory published session valid.

        The recorded hash is updated to match the replacement, so only the report's own content and agreement with the
        published session can reject it, under both the candidate and the canonical result name.
        """
        validator, canonical = _promote_profile(tmp_path, handoff_fixtures, branch, handoff_changes)
        changed = replacement(tmp_path, (tmp_path / "profile.json").read_bytes())
        (tmp_path / "profile.json").write_bytes(changed)
        result = json.loads(canonical.read_text())
        result["metadata"]["profile"]["profile_sha256"] = hashlib.sha256(changed).hexdigest()

        messages = _exit_messages(validator, tmp_path, [("result.candidate.json", result), ("result.json", result)])

        assert len(messages) == 2
        assert all("profile-" in message for message in messages)

    @pytest.mark.parametrize(("branch", "handoff_changes"), BRANCHES)
    def test_missing_report_is_refused(
        self, tmp_path: Path, handoff_fixtures: ModuleType, branch: str, handoff_changes: dict
    ) -> None:
        """Deleting the retained report after promotion is refused as a missing artifact naming `profile.json`."""
        validator, canonical = _promote_profile(tmp_path, handoff_fixtures, branch, handoff_changes)
        (tmp_path / "profile.json").unlink()

        with pytest.raises(SystemExit, match=r"missing-artifact:.*[/\\]profile\.json$"):
            validator.validate("profile", tmp_path, canonical)

    @pytest.mark.parametrize(("branch", "handoff_changes"), BRANCHES)
    def test_result_without_a_version_2_schema_is_refused(
        self, tmp_path: Path, handoff_fixtures: ModuleType, branch: str, handoff_changes: dict
    ) -> None:
        """A result whose schema version is absent, 1, boolean or a float 2.0 is refused under either name.

        The unbound reply and the missing handoff metadata make schema the only thing left to reject; `True` and `2.0`
        guard against Python's numeric equality with the valid integer.
        """
        validator, canonical = _promote_profile(tmp_path, handoff_fixtures, branch, handoff_changes)
        original = json.loads(canonical.read_text())
        (tmp_path / "final.md").write_text("Unbound response\n", encoding="utf-8")
        (tmp_path / "final-handoff.validation.json").unlink()
        bare = {
            **original,
            "metadata": {key: value for key, value in original["metadata"].items() if key != "final_handoff"},
        }
        variants = [
            {key: value for key, value in bare.items() if key != "schema_version"},
            *({**bare, "schema_version": schema} for schema in (1, True, 2.0)),
        ]
        results = [(name, variant) for variant in variants for name in ("result.candidate.json", "result.json")]

        messages = _exit_messages(validator, tmp_path, results)

        assert len(messages) == 8
        assert all("profile-result-schema-version" in message for message in messages)


@pytest.mark.installed_plugin
@pytest.mark.parametrize("cache_write", [None, 0, 80])
def test_profile_cache_write_subset_accepts_observed_and_absent_values(tmp_path: Path, cache_write: int | None) -> None:
    """Observed cache writes fit input tokens; absent writes remain unavailable rather than zero."""
    counters: dict[str, Any] = {**usage(100), "cache_write_input_tokens": cache_write}
    source = rollout(tmp_path / "cache.jsonl", [token_row(thread_token_usage=counters)])
    before = source.read_bytes()

    result = invoke("--input", str(source))

    assert source.read_bytes() == before
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["sessions"][0]["tokens"]["usage"]["cache_write_input_tokens"] == cache_write


@pytest.mark.installed_plugin
def test_profile_cache_write_subset_rejects_more_than_input_tokens(tmp_path: Path) -> None:
    """Cache writes of 81 against 80 input tokens exceed the total they are a subset of and fail the run."""
    counters = {**usage(100), "cache_write_input_tokens": 81}
    source = rollout(tmp_path / "cache.jsonl", [token_row(thread_token_usage=counters)])
    before = source.read_bytes()

    result = invoke("--input", str(source))

    assert source.read_bytes() == before
    assert result.returncode == 1
    assert result.stdout == ""
    assert "token-subset-exceeds-total" in result.stderr


def _deep_telemetry_arguments(tmp_path: Path, source: Path) -> tuple[str, ...]:
    """Point the profile at the deeply nested file as its native telemetry input."""
    return ("--input", str(source))


def _deep_rates_arguments(tmp_path: Path, source: Path) -> tuple[str, ...]:
    """Point the profile at a valid log plus the deeply nested file as its rate sheet."""
    return ("--input", str(rollout(tmp_path / "valid.jsonl", [])), "--rates", str(source))


def _deep_report_arguments(tmp_path: Path, source: Path) -> tuple[str, ...]:
    """Point the profile at the deeply nested file as a retained report to check."""
    return ("--check-report", str(source))


@pytest.mark.installed_plugin
@pytest.mark.parametrize(
    ("raw", "arguments_for"),
    [
        pytest.param(
            '{"type":"token_usage_record","payload":{"thread_token_usage":' + NESTED_JSON + "}}\n",
            _deep_telemetry_arguments,
            id="telemetry",
        ),
        pytest.param(
            '{"type":"token_usage_record","payload":{"thread_token_usage":' + NESTED_JSON + "}}",
            _deep_telemetry_arguments,
            id="telemetry-no-newline",
        ),
        pytest.param(
            '{"as_of":"2026-10-20","source":"fixture","models":' + NESTED_JSON + "}\n",
            _deep_rates_arguments,
            id="rates",
        ),
        pytest.param(
            '{"schema_version":1,"aggregation":"separate-sessions-no-grand-total","sessions":' + NESTED_JSON + "}\n",
            _deep_report_arguments,
            id="report",
        ),
    ],
)
def test_profile_deep_json_has_bounded_diagnostics(
    tmp_path: Path, raw: str, arguments_for: Callable[[Path, Path], tuple[str, ...]]
) -> None:
    """Decoder depth failures on older supported Python versions cannot escape as tracebacks.

    A 2000-level nested JSON value in telemetry (with or without a final newline), a rate sheet or a retained report
    must fail with one short profile diagnostic that names no private text and leaves the file untouched.
    """
    source = tmp_path / "deep.json"
    source.write_bytes(raw.encode())
    before = source.read_bytes()

    result = invoke(*arguments_for(tmp_path, source))

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
    retained.write_bytes(UNAVAILABLE_REPORT)

    checked = invoke("--check-report", str(retained))

    assert checked.returncode == 0, checked.stderr
    assert json.loads(checked.stdout) == {
        "observed": False,
        "rows": [["Unavailable", "Unavailable", "Unavailable", "Unavailable", "No selected telemetry"]],
    }

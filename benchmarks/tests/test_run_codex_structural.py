"""No-model acceptance tests for the Codex provider-parity adapter."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

BENCHMARKS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BENCHMARKS_DIR))

from _bench_codex import runtime as codex_runtime  # noqa: E402
from _bench_common.presentation import LEGEND_CLOSE_RULE, LEGEND_OPEN_RULE  # noqa: E402
from benchmarks._bench_common import provider_parity_contracts as core  # noqa: E402


SCRIPT_PATH = BENCHMARKS_DIR / "run-codex-structural.py"
SUITE_PATH = BENCHMARKS_DIR / "suites" / "tasks-bench.json"
MANIFEST_PATH = BENCHMARKS_DIR / "manifests" / "codex-integration.json"
POSIX_SECURITY = pytest.mark.skipif(os.name == "nt", reason="requires POSIX private-mode and ownership semantics")


def test_public_runner_stays_below_the_250_kilobyte_maintenance_limit() -> None:
    """The public runner must keep stage detail in focused private modules."""
    assert SCRIPT_PATH.stat().st_size < 250_000


def _make_direct_runtime_bundle(root: Path) -> Path:
    """Create the minimum source-shaped direct CLI runtime used by isolation tests.

    Example:
        >>> from tempfile import TemporaryDirectory
        >>> with TemporaryDirectory() as directory:
        ...     launcher = _make_direct_runtime_bundle(Path(directory))
        ...     launcher.name, (launcher.parents[1] / "src/codemap_py/__init__.py").is_file()
        ('codemap-py', True)
    """
    runtime = root / "codemap-runtime"
    launcher = runtime / "bin" / "codemap-py"
    exclusions = runtime / "bin" / "_exclusions.py"
    entrypoint = runtime / "scripts" / "codemap_py_entry.py"
    package = runtime / "src" / "codemap_py"
    launcher.parent.mkdir(parents=True)
    entrypoint.parent.mkdir(parents=True)
    package.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)
    exclusions.write_text("EXCLUSION_PATTERNS = ()\n", encoding="utf-8")
    entrypoint.write_text("raise SystemExit(0)\n", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    return launcher


def _successful_plain_profile_command(command: list[str], **_kwargs: Any) -> SimpleNamespace:
    """Emulate a valid plain profile without executing Codex or exposing auth.

    Example:
        >>> _successful_plain_profile_command(["codex", "sandbox", "-c", "pass"]).returncode
        0
        >>> result = _successful_plain_profile_command(["codex", "sandbox", "-c", "write()"])
        >>> result.returncode, result.stderr
        (1, 'Operation not permitted')
    """
    if command == ["codex", "--version"]:
        return SimpleNamespace(returncode=0, stdout="codex-cli 0.145.0", stderr="")
    if command[:2] == ["codex", "sandbox"]:
        script = command[command.index("-c") + 1]
        if script == "pass":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="Operation not permitted")
    if command == ["codex", "login", "status"]:
        return SimpleNamespace(returncode=0, stdout="Logged in using fixture", stderr="")
    return SimpleNamespace(returncode=0, stdout='{"installed":[],"available":[]}', stderr="")


def test_parse_codex_jsonl_preserves_native_events_and_normalizes_usage(
    script_run_codex: Any,
) -> None:
    """Official JSONL events must yield output, usage, calls, and raw audit evidence."""
    events = [
        {"type": "thread.started", "thread_id": "thread-fixture"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {
                "id": "command-1",
                "type": "command_execution",
                "command": '"$CODEMAP_BIN" query --compact rdeps lightning.fabric',
                "status": "completed",
                "exit_code": 0,
                "aggregated_output": '{"index":{"query_complete":true,"compact":true}}',
                "duration_ms": 1250,
            },
        },
        {
            "type": "item.completed",
            "item": {"id": "answer-1", "type": "agent_message", "text": "Final fixture answer."},
        },
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 120,
                "cached_input_tokens": 80,
                "output_tokens": 24,
                "reasoning_output_tokens": 7,
            },
        },
    ]
    stream = "\n".join(json.dumps(event) for event in events)

    parsed = codex_runtime.parse_codex_jsonl(stream)

    assert parsed.thread_id == "thread-fixture"
    assert parsed.output_text == "Final fixture answer."
    assert parsed.input_tokens == 120
    assert parsed.cached_input_tokens == 80
    assert parsed.output_tokens == 24
    assert parsed.reasoning_output_tokens == 7
    assert parsed.command_calls == 1
    assert parsed.codemap_calls == 1
    assert parsed.completed is True
    assert parsed.incomplete is False
    assert parsed.raw_events == events
    assert parsed.item_counts == {"command_execution": 1, "agent_message": 1}
    assert parsed.tool_elapsed_s == pytest.approx(1.25)
    assert parsed.tool_result_tokens is None


def test_parse_codex_jsonl_preserves_agent_message_boundaries(script_run_codex: Any) -> None:
    """Separate progress and answer events must not fuse a Markdown heading."""
    events = [
        {
            "type": "item.completed",
            "item": {"id": "progress", "type": "agent_message", "text": "Checked the repository."},
        },
        {
            "type": "item.completed",
            "item": {"id": "answer", "type": "agent_message", "text": "## Callers\npkg.mod::caller"},
        },
        {"type": "turn.completed", "status": "completed"},
    ]

    parsed = codex_runtime.parse_codex_jsonl("\n".join(json.dumps(event) for event in events))

    assert parsed.output_text == "Checked the repository.\n## Callers\npkg.mod::caller"


def test_parse_codex_jsonl_records_text_boundary_after_last_command(script_run_codex: Any) -> None:
    """Agentic report scoring can exclude exploratory prose before the last tool."""
    events = [
        {
            "type": "item.completed",
            "item": {"id": "progress", "type": "agent_message", "text": "Candidate: pkg.first."},
        },
        {
            "type": "item.completed",
            "item": {
                "id": "query",
                "type": "command_execution",
                "command": '"$CODEMAP_BIN" query --compact rdeps pkg.target',
                "status": "completed",
                "exit_code": 0,
                "aggregated_output": '{"index":{"query_complete":true,"compact":true}}',
            },
        },
        {
            "type": "item.completed",
            "item": {"id": "answer", "type": "agent_message", "text": "Final: pkg.second."},
        },
        {"type": "turn.completed", "status": "completed"},
    ]

    parsed = codex_runtime.parse_codex_jsonl("\n".join(json.dumps(event) for event in events))

    assert parsed.output_text[parsed.last_tool_text_offset :].lstrip("\n") == "Final: pkg.second."


def _completed_stream(
    *,
    output: str = "fixture answer",
    input_tokens: int = 10,
    cached_input_tokens: int = 0,
    output_tokens: int = 2,
    commands: list[dict[str, Any]] | None = None,
) -> str:
    """Build one official-shape completed Codex event stream.

    Example:
        >>> events = [json.loads(line) for line in _completed_stream(output="example", input_tokens=0).splitlines()]
        >>> [event["type"] for event in events]
        ['thread.started', 'item.completed', 'turn.completed']
        >>> events[1]["item"]["text"], events[-1]["usage"]["input_tokens"]
        ('example', 0)
    """
    events: list[dict[str, Any]] = [{"type": "thread.started", "thread_id": "fixture-thread"}]
    events.extend(
        {"type": "item.completed", "item": {"id": f"command-{index}", **command}}
        for index, command in enumerate(commands or [], start=1)
    )
    events.extend(
        [
            {
                "type": "item.completed",
                "item": {"id": "answer", "type": "agent_message", "text": output},
            },
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": input_tokens,
                    "cached_input_tokens": cached_input_tokens,
                    "output_tokens": output_tokens,
                    "reasoning_output_tokens": 0,
                },
            },
        ]
    )
    return "\n".join(json.dumps(event) for event in events)


def test_loaded_task_keeps_canonical_identity_and_shared_evaluator_input(script_run_codex: Any, tmp_path: Path) -> None:
    """Adapter provenance must not enter task hashing or evaluator input."""
    raw_task = next(task for task in core.load_task_suite(SUITE_PATH) if task["id"] == "FN-02")
    loaded_task = next(
        task for task in script_run_codex.load_tasks_with_provenance(SUITE_PATH, MANIFEST_PATH) if task["id"] == "FN-02"
    )
    evaluated: list[tuple[dict[str, Any], str]] = []

    def _evaluator(task: dict[str, Any], output_text: str) -> core.EvaluationResult:
        """Evaluate synthetic provider output using the scenario's controlled score."""
        evaluated.append((task, output_text))
        return core.EvaluationResult(scored=True, correct=True, quality_score=0.75)

    runner = script_run_codex.CodexRunner(
        "fixture-model",
        tmp_path,
        transport=lambda *_args, **_kwargs: _completed_stream(),
        evaluator=_evaluator,
    )

    result = runner.run(loaded_task, "B_auto")

    assert result.task_hash == core.canonical_task_hash(raw_task)
    assert result.prompt_hash == core.prompt_hash(raw_task)
    assert result.suite_hash == core.semantic_suite_hash(core.load_task_suite(SUITE_PATH))
    assert result.oracle_class == "independent"
    assert result.headline_eligible_v1 is True
    assert result.quality_score == pytest.approx(0.75)
    assert evaluated == [(raw_task, "fixture answer")]


@pytest.mark.parametrize(
    ("arm", "commands", "expected_compliance", "expected_contamination", "expected_success"),
    [
        pytest.param("A_plain", [], None, False, True, id="plain-clean"),
        pytest.param(
            "A_plain",
            [
                {
                    "type": "command_execution",
                    "command": '"$CODEMAP_BIN" query --compact rdeps pkg.core',
                    "status": "completed",
                    "exit_code": 0,
                    "aggregated_output": '{"index":{"query_complete":true,"compact":true}}',
                }
            ],
            None,
            True,
            False,
            id="plain-contaminated",
        ),
        pytest.param("B_auto", [], False, False, True, id="direct-no-call-separate"),
        pytest.param("C_strict", [], False, False, True, id="skill-no-call-separate"),
        pytest.param(
            "C_strict",
            [
                {
                    "type": "command_execution",
                    "command": '"$CODEMAP_BIN" query --compact rdeps pkg.core',
                    "status": "completed",
                    "exit_code": 0,
                    "aggregated_output": '{"index":{"query_complete":true,"compact":true}}',
                }
            ],
            False,
            False,
            True,
            id="skill-query-without-read-not-compliant",
        ),
    ],
)
def test_arm_call_semantics_are_separate_from_quality(
    script_run_codex: Any,
    tmp_path: Path,
    arm: str,
    commands: list[dict[str, Any]],
    expected_compliance: bool | None,
    expected_contamination: bool,
    expected_success: bool,
) -> None:
    """A contamination and C compliance cannot silently change correctness."""
    task = {"id": "fixture", "prompt": "unchanged prompt", "type": "demo", "scoreable": True}
    runner = script_run_codex.CodexRunner(
        "fixture-model",
        tmp_path,
        transport=lambda *_args, **_kwargs: _completed_stream(commands=commands),
        evaluator=lambda *_args: core.EvaluationResult(scored=True, correct=True, quality_score=1.0),
    )

    result = runner.run(task, arm)

    assert result.compliance is expected_compliance
    assert result.contaminated is expected_contamination
    assert result.success is expected_success
    assert result.correct is True
    assert result.quality_score == pytest.approx(1.0)


def test_runner_persists_cache_over_gross_as_explicit_unscoreable_token_accounting(
    script_run_codex: Any, tmp_path: Path
) -> None:
    """Provider-native gross/cache evidence remains raw when fresh-token derivation is impossible."""
    runner = script_run_codex.CodexRunner(
        "fixture-model",
        tmp_path,
        transport=lambda *_args, **_kwargs: _completed_stream(input_tokens=25, cached_input_tokens=80),
        evaluator=lambda *_args: core.EvaluationResult(scored=True, correct=True, quality_score=1.0),
    )

    result = runner.run({"id": "fixture", "prompt": "prompt", "type": "demo"}, "A_plain")
    output_path = tmp_path / "telemetry.jsonl"
    script_run_codex._append_run(output_path, result, execution_index=0)
    persisted = json.loads(output_path.read_text(encoding="utf-8"))

    assert (result.input_tokens, result.cached_input_tokens, result.fresh_input_tokens) == (25, 80, None)
    assert result.token_accounting_inconsistent is True
    assert persisted["token_accounting_inconsistent"] is True
    assert persisted["fresh_input_tokens"] is None


def test_result_rows_show_gross_input_while_telemetry_retains_cache_detail(
    script_run_codex: Any, tmp_path: Path
) -> None:
    """Console output avoids cache-derived claims while JSONL retains provider evidence."""
    runner = script_run_codex.CodexRunner(
        "fixture-model",
        tmp_path,
        transport=lambda *_args, **_kwargs: _completed_stream(input_tokens=120, cached_input_tokens=80),
        evaluator=lambda *_args: core.EvaluationResult(scored=True, correct=True, quality_score=1.0),
    )

    result = runner.run({"id": "fixture", "prompt": "prompt", "type": "demo"}, "A_plain")
    output_path = tmp_path / "telemetry.jsonl"
    script_run_codex._append_run(output_path, result, execution_index=0)
    persisted = json.loads(output_path.read_text(encoding="utf-8"))
    row = codex_runtime.format_structural_result_row(
        status="✓",
        task_id=result.task_id,
        repetition=1,
        arm=result.arm,
        input_tokens=result.input_tokens,
        cached_input_tokens=result.cached_input_tokens,
        fresh_tokens=result.fresh_input_tokens,
        output_tokens=result.output_tokens,
        elapsed_s=1.0,
        quality="1.000",
        adherence=True,
        codemap_used=False,
    )

    assert "in=   120  out=" in row
    assert "/" not in row
    assert persisted["input_tokens"] == 120
    assert persisted["cached_input_tokens"] == 80
    assert persisted["fresh_input_tokens"] == 40


def test_result_rows_expose_query_conformance_and_cohort(script_run_codex: Any) -> None:
    """Console progress must not disguise a diagnostic query mismatch as a clean treatment."""
    row = codex_runtime.format_structural_result_row(
        status="✓",
        task_id="SE-01",
        repetition=1,
        arm="C_strict",
        input_tokens=120,
        cached_input_tokens=80,
        fresh_tokens=40,
        output_tokens=1,
        elapsed_s=1.0,
        quality="1.000",
        adherence=True,
        codemap_used=True,
        query_conformance=False,
        headline_eligible=False,
    )

    assert "treatment:✓" in row
    assert "query:✗" in row
    assert "cohort:D" in row


def test_parser_marks_malformed_and_missing_terminal_streams_incomplete(script_run_codex: Any) -> None:
    """Invalid or unterminated JSONL cannot become a complete benchmark cell."""
    malformed = codex_runtime.parse_codex_jsonl('{"type":"turn.completed"}\nnot-json')
    unterminated = codex_runtime.parse_codex_jsonl(
        json.dumps(
            {
                "type": "item.completed",
                "item": {"id": "answer", "type": "agent_message", "text": "partial"},
            }
        )
    )

    assert malformed.incomplete is True
    assert malformed.error
    assert malformed.malformed_lines == 1
    assert unterminated.incomplete is True
    assert unterminated.error


def test_codemap_failure_then_ordinary_command_is_fallback(script_run_codex: Any) -> None:
    """Fallback is counted only after a failed Codemap command."""
    stream = _completed_stream(
        commands=[
            {
                "type": "command_execution",
                "command": '"$CODEMAP_BIN" query --compact rdeps pkg.core',
                "status": "failed",
                "exit_code": 1,
            },
            {
                "type": "command_execution",
                "command": "rg 'pkg.core' src",
                "status": "completed",
                "exit_code": 0,
            },
        ]
    )

    parsed = codex_runtime.parse_codex_jsonl(stream)

    assert parsed.codemap_calls == 1
    assert parsed.codemap_errors == 1
    assert parsed.fallback_calls == 1


@pytest.mark.parametrize(
    ("first_stream", "expected_calls", "expected_retries"),
    [
        pytest.param(
            json.dumps({"type": "error", "error": "transport unavailable"}),
            2,
            1,
            id="zero-token-error-retried",
        ),
        pytest.param(_completed_stream(input_tokens=0, output_tokens=0), 1, 0, id="successful-zero-token-not-retried"),
        pytest.param(
            "\n".join(
                [
                    json.dumps({"type": "error", "error": "substantive failure"}),
                    json.dumps(
                        {
                            "type": "turn.failed",
                            "usage": {"input_tokens": 5, "output_tokens": 0},
                            "error": "substantive failure",
                        }
                    ),
                ]
            ),
            1,
            0,
            id="substantive-failure-not-retried",
        ),
    ],
)
def test_retry_policy_only_retries_zero_token_transport_failures(
    script_run_codex: Any,
    tmp_path: Path,
    first_stream: str,
    expected_calls: int,
    expected_retries: int,
) -> None:
    """The locked two-retry allowance cannot repeat a substantive task result."""
    streams = iter([first_stream, _completed_stream()])
    calls = 0

    def _transport(*_args: Any, **_kwargs: Any) -> str:
        """Return fixture transport evidence; no provider call."""
        nonlocal calls
        calls += 1
        return next(streams)

    runner = script_run_codex.CodexRunner(
        "fixture-model",
        tmp_path,
        transport=_transport,
        evaluator=lambda *_args: core.EvaluationResult(scored=True, correct=True, quality_score=1.0),
    )

    result = runner.run({"id": "fixture", "prompt": "prompt", "type": "demo"}, "A_plain")

    assert calls == expected_calls
    assert result.retry_count == expected_retries
    assert runner.timeout == pytest.approx(600.0)


@pytest.mark.parametrize(
    "message",
    ["HTTP 401 Unauthorized: refresh token expired", "HTTP 401 Unauthorized: refresh token has already been used"],
)
def test_retry_policy_does_not_repeat_non_retryable_authentication_failures(
    script_run_codex: Any,
    tmp_path: Path,
    message: str,
) -> None:
    """Stop expired or consumed refresh tokens after one attempt.

    Zero-token permanent 401 failures must not be retried as transient errors.
    """
    streams = iter(
        [
            json.dumps({"type": "error", "error": message, "error_type": "non_zero_exit"}),
            _completed_stream(output="must not replace the 401"),
        ]
    )
    calls = 0

    def _transport(*_args: Any, **_kwargs: Any) -> str:
        """Return fixture transport evidence; no provider call."""
        nonlocal calls
        calls += 1
        return next(streams)

    runner = script_run_codex.CodexRunner(
        "fixture-model",
        tmp_path,
        transport=_transport,
        evaluator=lambda *_args: core.EvaluationResult(scored=True, correct=True, quality_score=1.0),
    )

    result = runner.run({"id": "fixture", "prompt": "prompt", "type": "demo"}, "A_plain")

    assert calls == 1
    assert result.retry_count == 0
    assert result.incomplete is True
    assert result.error == message


def test_parser_keeps_refresh_authentication_classification_after_later_generic_401(script_run_codex: Any) -> None:
    """Preserve permanent auth classification across refresh-token reuse, turn.failed, then generic 401.

    The later error must not enable retries of invalid credentials.
    """
    stream = "\n".join(
        json.dumps(event)
        for event in (
            {
                "type": "error",
                "error": "HTTP 401 Unauthorized: refresh token has already been used",
                "error_type": "non_zero_exit",
            },
            {"type": "turn.failed", "error": "turn failed", "error_type": "turn_failed"},
            {
                "type": "error",
                "error": "HTTP 401 Unauthorized: command exited non-zero",
                "error_type": "non_zero_exit",
            },
        )
    )

    parsed = codex_runtime.parse_codex_jsonl(stream)

    assert parsed.incomplete is True
    assert parsed.error_type == "authentication_failed"
    assert parsed.retryable is False


@pytest.mark.parametrize(
    ("header", "secret"),
    [
        pytest.param("Cookie: session=fixture-cookie", "fixture-cookie", id="cookie"),
        pytest.param("Set-Cookie: session=fixture-set-cookie; HttpOnly", "fixture-set-cookie", id="set-cookie"),
        pytest.param("Authorization: Basic fixture-authorization", "fixture-authorization", id="authorization"),
    ],
)
def test_parser_redacts_textual_credential_headers_from_provider_and_telemetry(
    script_run_codex: Any, header: str, secret: str
) -> None:
    """Credential-bearing textual errors must be safe in every persisted projection.

    Prevents provider error strings from retaining Cookie, Set-Cookie, or non-Bearer Authorization values in either the
    result error or raw events.
    """
    parsed = codex_runtime.parse_codex_jsonl(json.dumps({"type": "error", "error": f"provider failure: {header}"}))
    persisted_projection = json.dumps({"error": parsed.error, "raw_events": parsed.raw_events})

    assert secret not in persisted_projection
    assert "<redacted>" in persisted_projection


def test_retry_policy_preserves_partial_response_when_usage_is_absent(
    script_run_codex: Any,
    tmp_path: Path,
) -> None:
    """Preserve partial output received before transport failure without usage metadata.

    A zero-token retry must not overwrite that auditable response.
    """
    first_stream = "\n".join(
        [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"id": "answer", "type": "agent_message", "text": "partial answer"},
                }
            ),
            json.dumps({"type": "error", "error": "connection dropped", "error_type": "transport_error"}),
        ]
    )
    streams = iter([first_stream, _completed_stream(output="replacement answer")])
    calls = 0

    def _transport(*_args: Any, **_kwargs: Any) -> str:
        """Return fixture transport evidence; no provider call."""
        nonlocal calls
        calls += 1
        return next(streams)

    runner = script_run_codex.CodexRunner(
        "fixture-model",
        tmp_path,
        transport=_transport,
        evaluator=lambda *_args: core.EvaluationResult(scored=True, correct=True, quality_score=1.0),
    )

    result = runner.run({"id": "fixture", "prompt": "prompt", "type": "demo"}, "A_plain")

    assert calls == 1
    assert result.retry_count == 0
    assert result.incomplete is True
    assert result.output_text == "partial answer"
    assert result.input_tokens == 0
    assert result.output_tokens == 0


def test_retry_attempts_share_one_coordinate_wall_clock_budget(
    script_run_codex: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transport timeout cannot unlock another full coordinate budget."""
    calls = 0

    def _transport(*_args: Any, **_kwargs: Any) -> str:
        """Return fixture transport evidence; no provider call."""
        nonlocal calls
        calls += 1
        return json.dumps({"type": "error", "error": "transport unavailable"})

    clock = iter([0.0, 0.0, 600.0, 600.0])
    monkeypatch.setattr(script_run_codex.time, "monotonic", lambda: next(clock))
    runner = script_run_codex.CodexRunner(
        "fixture-model",
        tmp_path,
        timeout=600.0,
        transport=_transport,
        evaluator=lambda *_args: core.EvaluationResult(scored=True, correct=True, quality_score=1.0),
    )

    result = runner.run({"id": "fixture", "prompt": "prompt", "type": "demo"}, "A_plain")

    assert calls == 1
    assert result.retry_count == 0
    assert result.error_type == "cell_timeout"
    assert result.cell_wall_clock_limit_s == pytest.approx(600.0)


def test_command_lifecycle_uses_completed_status_once(script_run_codex: Any) -> None:
    """A started command cannot hide its later failed completion status."""
    item = {
        "id": "command-1",
        "type": "command_execution",
        "command": '"$CODEMAP_BIN" query --compact rdeps pkg.core',
    }
    events = [
        {"type": "item.started", "item": {**item, "status": "in_progress"}},
        {"type": "item.completed", "item": {**item, "status": "failed", "exit_code": 1}},
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 5, "output_tokens": 1},
        },
    ]

    parsed = codex_runtime.parse_codex_jsonl("\n".join(json.dumps(event) for event in events))

    assert parsed.command_calls == 1
    assert parsed.codemap_calls == 1
    assert parsed.codemap_successful_calls == 0
    assert parsed.codemap_errors == 1


def test_compound_shell_probe_is_not_codemap_delivery_evidence(script_run_codex: Any) -> None:
    """A compound shell command cannot become Codemap delivery evidence."""
    stream = _completed_stream(
        commands=[
            {
                "type": "command_execution",
                "command": "/bin/zsh -lc '/tmp/plugin/bin/codemap-py query central --top 1; echo $?'",
                "status": "completed",
                "exit_code": 0,
                "aggregated_output": "127\n",
            }
        ]
    )

    parsed = codex_runtime.parse_codex_jsonl(stream)

    assert parsed.codemap_calls == 0
    assert parsed.codemap_successful_calls == 0
    assert parsed.codemap_errors == 0


def test_terminal_event_with_pending_command_is_incomplete(script_run_codex: Any) -> None:
    """A terminal turn cannot make an unfinished command item scoreable."""
    events = [
        {
            "type": "item.started",
            "item": {
                "id": "command-1",
                "type": "command_execution",
                "command": "rg fixture src",
                "status": "in_progress",
            },
        },
        {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 1}},
    ]

    parsed = codex_runtime.parse_codex_jsonl("\n".join(json.dumps(event) for event in events))

    assert parsed.completed is False
    assert parsed.incomplete is True
    assert parsed.error_type == "pending_item"


def test_mentioning_codemap_in_an_ordinary_search_is_not_adoption(script_run_codex: Any) -> None:
    """A grep query about Codemap text is not a Codemap executable invocation."""
    parsed = codex_runtime.parse_codex_jsonl(
        _completed_stream(
            commands=[
                {
                    "type": "command_execution",
                    "command": "rg 'codemap-py' src",
                    "status": "completed",
                    "exit_code": 0,
                }
            ]
        )
    )

    assert parsed.command_calls == 1
    assert parsed.codemap_calls == 0


def test_loader_rejects_reordered_known_tasks(script_run_codex: Any, tmp_path: Path) -> None:
    """Known task IDs cannot be rearranged into an unregistered suite."""
    tasks = core.load_task_suite(SUITE_PATH)
    reordered_path = tmp_path / "tasks-bench-reordered.json"
    reordered_path.write_text(json.dumps(list(reversed(tasks))), encoding="utf-8")

    with pytest.raises(ValueError, match="order|membership|suite"):
        script_run_codex.load_tasks_with_provenance(reordered_path, MANIFEST_PATH)


def test_runner_rejects_tampered_nested_provenance(script_run_codex: Any, tmp_path: Path) -> None:
    """Supplied provenance cannot override the canonical task-byte hash."""
    loaded = next(
        task for task in script_run_codex.load_tasks_with_provenance(SUITE_PATH, MANIFEST_PATH) if task["id"] == "FN-02"
    )
    tampered = dict(loaded)
    tampered[script_run_codex._PROVENANCE_KEY] = {
        **loaded[script_run_codex._PROVENANCE_KEY],
        "task_hash": "0" * 64,
    }
    runner = script_run_codex.CodexRunner(
        "fixture-model",
        tmp_path,
        transport=lambda *_args, **_kwargs: _completed_stream(),
        evaluator=lambda *_args: core.EvaluationResult(scored=True, correct=True, quality_score=1.0),
    )

    with pytest.raises(ValueError, match="task hash"):
        runner.run(tampered, "B_auto")


def test_skill_arm_installs_and_locks_rig_and_codemap_plugins(script_run_codex: Any, tmp_path: Path) -> None:
    """C installs both plugins and records their installed identities before model use."""
    marketplace_root = tmp_path / "marketplace"
    manifest = marketplace_root / ".agents" / "plugins" / "marketplace.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"name":"borda-ai-rig","plugins":[]}', encoding="utf-8")
    calls: list[list[str]] = []

    with script_run_codex.prepare_arm_home("C_strict", root=tmp_path) as home:
        rig_path = home.path / "plugins" / "cache" / "borda-ai-rig" / "codex-rig" / "0.4.0"
        rig_manifest = rig_path / ".codex-plugin" / "plugin.json"
        rig_manifest.parent.mkdir(parents=True)
        rig_manifest.write_text('{"name":"codex-rig","version":"0.4.0"}', encoding="utf-8")
        adapter = rig_path / "shared" / "codemap_adapter.py"
        adapter.parent.mkdir(parents=True)
        adapter.write_text("print('fixture adapter')\n", encoding="utf-8")
        installed_path = home.path / "plugins" / "cache" / "borda-ai-rig" / "codemap-py" / "0.27.0"
        launcher = installed_path / "bin" / "codemap-py"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        launcher.chmod(0o755)
        plugin_manifest = installed_path / ".codex-plugin" / "plugin.json"
        plugin_manifest.parent.mkdir()
        plugin_manifest.write_text('{"name":"codemap-py","version":"0.27.0"}', encoding="utf-8")
        query_skill = installed_path / "codex-skills" / "query-code" / "SKILL.md"
        query_skill.parent.mkdir(parents=True)
        query_skill.write_text("# query-code\n", encoding="utf-8")

        def _command_runner(command: list[str], **_kwargs: Any) -> SimpleNamespace:
            """Record commands and return fixture responses."""
            calls.append(command)
            if len(command) > 2 and command[1:3] == ["plugin", "add"]:
                install_path = rig_path if command[3] == "codex-rig@borda-ai-rig" else installed_path
                stdout = json.dumps({"installedPath": str(install_path)})
            elif len(command) > 2 and command[1:3] == ["plugin", "list"]:
                stdout = (
                    '{"installed":[{"name":"codex-rig","enabled":true},'
                    '{"name":"codemap-py","enabled":true}],"available":[]}'
                )
            elif len(command) > 2 and command[1] == str(adapter):
                assert "SECRET" not in _kwargs["env"]
                assert _kwargs["env"]["CODEMAP_BIN"] == str(launcher.resolve())
                assert _kwargs["cwd"] == tmp_path.resolve()
                context_path = Path(command[command.index("--out") + 1])
                index_path = tmp_path / "locked-index.json"
                context_path.write_text(
                    json.dumps(
                        {
                            "protocol_version": "codemap-py.integration.v1",
                            "target": "lightning.pytorch.trainer.call",
                            "status": "degraded",
                            "probe": {
                                "status": "available",
                                "launcher": str(launcher.resolve()),
                                "doctor": {
                                    "plugin_root": str(installed_path.resolve()),
                                    "index_path": str(index_path.resolve()),
                                },
                            },
                            "queries": [{"exit_code": 0, "error": None, "query_complete": True}],
                        }
                    ),
                    encoding="utf-8",
                )
                stdout = ""
            else:
                stdout = ""
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

        installed = script_run_codex._install_codemap_plugin(
            home,
            marketplace_root,
            command_runner=_command_runner,
        )

        assert installed is True
        assert home.codex_rig_path == rig_path.resolve()
        assert home.codex_rig_manifest_sha256 == hashlib.sha256(rig_manifest.read_bytes()).hexdigest()
        assert home.codex_rig_adapter_path == adapter.resolve()
        assert home.codex_rig_adapter_sha256 == hashlib.sha256(adapter.read_bytes()).hexdigest()
        assert home.env["CODEMAP_BIN"] == str(launcher.resolve())
        assert home.env["CODEMAP_SKILL_FILE"] == str(query_skill.resolve())
        assert home.codemap_launcher_path == launcher.resolve()
        assert home.codemap_launcher_sha256 == hashlib.sha256(launcher.read_bytes()).hexdigest()
        assert home.codemap_skill_path == query_skill.resolve()
        assert home.codemap_skill_sha256 == hashlib.sha256(query_skill.read_bytes()).hexdigest()
        assert script_run_codex._shell_environment(home)["CODEMAP_SKILL_FILE"] == str(query_skill.resolve())
        home.codemap_available = True
        home.codemap_verified = True
        home.env["CODEMAP_PYTHON"] = "/usr/bin/python3"
        home.env["SECRET"] = "must-not-reach-adapter"
        index_path = tmp_path / "locked-index.json"
        index_path.write_text("{}", encoding="utf-8")
        lock_path = tmp_path / "locks.json"
        lock_path.write_text(
            json.dumps(
                {
                    "artifact_sha256": {
                        "codemap_runtime_cli": home.codemap_launcher_sha256,
                        "codemap_candidate_manifest": home.codemap_plugin_manifest_sha256,
                        "codemap_query_skill": home.codemap_skill_sha256,
                        "codex_rig_plugin_manifest": home.codex_rig_manifest_sha256,
                        "codex_rig_adapter": home.codex_rig_adapter_sha256,
                    },
                    "codemap_candidate": {"version": "0.27.0"},
                    "codex_rig_candidate": {"version": "0.4.0"},
                    "codex_rig_integration_admission": {
                        "probe_category": "analysis",
                        "probe_target": "lightning.pytorch.trainer.call",
                    },
                }
            ),
            encoding="utf-8",
        )
        script_run_codex._verify_treatment_artifact_locks(home, lock_path)
        stale_lock = json.loads(lock_path.read_text(encoding="utf-8"))
        stale_lock["artifact_sha256"]["codex_rig_adapter"] = "0" * 64
        lock_path.write_text(json.dumps(stale_lock), encoding="utf-8")
        with pytest.raises(script_run_codex.TreatmentArtifactLockError, match="no paid model call was started"):
            script_run_codex._verify_treatment_artifact_locks(home, lock_path)
        stale_lock["artifact_sha256"]["codex_rig_adapter"] = home.codex_rig_adapter_sha256
        lock_path.write_text(json.dumps(stale_lock), encoding="utf-8")
        script_run_codex._admit_installed_skill_pair(
            home,
            tmp_path,
            index_path,
            manifest_path=lock_path,
            command_runner=_command_runner,
        )
        config = home.path / "config.toml"
        config.write_text("", encoding="utf-8")
        config.chmod(0o600)
        evidence = script_run_codex.probe_arm_home(home)
        assert evidence["codemap_launcher_path"] == str(launcher.resolve())
        assert evidence["codemap_launcher_sha256"] == home.codemap_launcher_sha256
        assert evidence["codemap_skill_path"] == str(query_skill.resolve())
        assert evidence["codemap_skill_sha256"] == home.codemap_skill_sha256
        assert evidence["codemap_skill_file"] == str(query_skill.resolve())
        assert evidence["codex_rig_path"] == str(rig_path.resolve())
        assert evidence["codex_rig_manifest_sha256"] == home.codex_rig_manifest_sha256
        assert evidence["codemap_context_path"] == str(home.codemap_context_path)
        assert evidence["codemap_context_sha256"] == home.codemap_context_sha256
        assert calls == [
            ["codex", "plugin", "marketplace", "add", str(marketplace_root)],
            ["codex", "plugin", "add", "codemap-py@borda-ai-rig", "--json"],
            ["codex", "plugin", "add", "codex-rig@borda-ai-rig", "--json"],
            ["codex", "plugin", "list", "--json"],
            [
                "/usr/bin/python3",
                str(adapter.resolve()),
                "context",
                "--category",
                "analysis",
                "--target",
                "lightning.pytorch.trainer.call",
                "--root",
                str(tmp_path.resolve()),
                "--out",
                str(home.codemap_context_path),
            ],
        ]


def _write_snapshot_plugin_tree(root: Path, name: str, version: str) -> Path:
    """Create one minimal byte-locked local plugin source for runtime tests.

    Example:
        >>> from tempfile import TemporaryDirectory
        >>> with TemporaryDirectory() as directory:
        ...     plugin = _write_snapshot_plugin_tree(Path(directory), "example", "1.2.3")
        ...     json.loads((plugin / ".codex-plugin/plugin.json").read_text())
        {'name': 'example', 'version': '1.2.3'}
    """
    plugin = root / name
    manifest = plugin / ".codex-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"name": name, "version": version}), encoding="utf-8")
    if name == "codemap-py":
        launcher = plugin / "bin" / "codemap-py"
        launcher.parent.mkdir()
        launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        launcher.chmod(0o755)
        skill = plugin / "codex-skills" / "query-code" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("# query-code\n", encoding="utf-8")
    else:
        adapter = plugin / "shared" / "codemap_adapter.py"
        adapter.parent.mkdir()
        adapter.write_text("print('fixture adapter')\n", encoding="utf-8")
    return plugin


def _write_frozen_snapshot_marketplace(root: Path) -> Path:
    """Write the fixed local marketplace beside the archived C plugin pair.

    Example:
        >>> from tempfile import TemporaryDirectory
        >>> with TemporaryDirectory() as directory:
        ...     path = _write_frozen_snapshot_marketplace(Path(directory))
        ...     [entry["source"]["source"] for entry in json.loads(path.read_text())["plugins"]]
        ['local', 'local']
    """
    marketplace_manifest = root / ".agents" / "plugins" / "marketplace.json"
    marketplace_manifest.parent.mkdir(parents=True)
    marketplace_manifest.write_text(
        json.dumps(
            {
                "name": "borda-ai-rig-frozen",
                "plugins": [
                    {"name": "codemap-py", "source": {"source": "local", "path": "./codemap-py"}},
                    {"name": "codex-rig", "source": {"source": "local", "path": "./codex-rig"}},
                ],
            }
        ),
        encoding="utf-8",
    )
    marketplace_manifest.chmod(0o600)
    return marketplace_manifest


def _write_runtime_snapshot_metadata(
    snapshot_root: Path,
    sources: dict[str, Path],
    *,
    locked_files: dict[str, Path] | None = None,
) -> None:
    """Write the minimal input identity ledger required by snapshot binding tests.

    Example:
        >>> from tempfile import TemporaryDirectory
        >>> with TemporaryDirectory() as directory:
        ...     root = Path(directory)
        ...     _write_runtime_snapshot_metadata(root, {})
        ...     json.loads((root / "input-snapshot.json").read_text())
        {'files': []}
    """
    files = []
    for role, source in sources.items():
        for path in sorted(source.rglob("*")):
            if path.is_file():
                archived_mode = 0o700 if stat.S_IMODE(path.stat().st_mode) & 0o111 else 0o600
                path.chmod(archived_mode)
                files.append(
                    {
                        "role": role,
                        "archived_path": path.relative_to(snapshot_root).as_posix(),
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "bytes": path.stat().st_size,
                        "mode": archived_mode,
                    }
                )
    for role, path in (locked_files or {}).items():
        archived_mode = 0o700 if stat.S_IMODE(path.stat().st_mode) & 0o111 else 0o600
        path.chmod(archived_mode)
        files.append(
            {
                "role": role,
                "archived_path": path.relative_to(snapshot_root).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "bytes": path.stat().st_size,
                "mode": archived_mode,
            }
        )
    (snapshot_root / "input-snapshot.json").write_text(json.dumps({"files": files}), encoding="utf-8")


def test_runtime_snapshot_excludes_private_evidence_cache_plan_and_test_trees(
    script_run_codex: Any, tmp_path: Path
) -> None:
    """Archived C plugins must not carry private evidence or test inputs into a model-visible home."""
    source = tmp_path / "plugin"
    archive = tmp_path / "inputs" / "C_strict" / "plugin"
    for relative in ("runtime.py", ".reports/oracle.json", ".plans/notes.md", ".cache/state.json", "tests/test.py"):
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("private" if relative != "runtime.py" else "print('runtime')\n", encoding="utf-8")
    entries: list[dict[str, Any]] = []

    script_run_codex._archive_snapshot_tree(source, archive, role="C_strict:plugin", entries=entries)

    assert (archive / "runtime.py").is_file()
    assert not any((archive / relative).exists() for relative in (".reports", ".plans", ".cache", "tests"))
    assert all(
        not any(part in {".reports", ".plans", ".cache", "tests"} for part in entry["archived_path"].split("/"))
        for entry in entries
    )


@POSIX_SECURITY
def test_paid_skill_home_installs_only_from_bound_run_snapshot(
    script_run_codex: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Install both archived plugins through a run-owned snapshot marketplace, insulating later cells from marketplace
    drift.

    Codex CLI 0.146 rejects direct plugin paths.
    """
    snapshot_root = tmp_path / "run" / "inputs"
    snapshot_root.mkdir(parents=True)
    codemap_source = _write_snapshot_plugin_tree(snapshot_root / "C_strict", "codemap-py", "0.27.0")
    rig_source = _write_snapshot_plugin_tree(snapshot_root / "C_strict", "codex-rig", "0.4.0")
    marketplace_manifest = _write_frozen_snapshot_marketplace(snapshot_root / "C_strict")
    _write_runtime_snapshot_metadata(
        snapshot_root,
        {
            "C_strict:codemap-py": codemap_source,
            "C_strict:codex-rig": rig_source,
        },
        locked_files={"C_strict:marketplace": marketplace_manifest},
    )
    frozen_name = "borda-ai-rig-frozen"
    snapshot_manifest = json.loads(marketplace_manifest.read_text(encoding="utf-8"))
    snapshot_ledger = json.loads((snapshot_root / "input-snapshot.json").read_text(encoding="utf-8"))
    assert snapshot_manifest == {
        "name": frozen_name,
        "plugins": [
            {"name": "codemap-py", "source": {"source": "local", "path": "./codemap-py"}},
            {"name": "codex-rig", "source": {"source": "local", "path": "./codex-rig"}},
        ],
    }
    assert marketplace_manifest.stat().st_mode & 0o777 == 0o600
    assert {
        "role": "C_strict:marketplace",
        "archived_path": "C_strict/.agents/plugins/marketplace.json",
        "sha256": hashlib.sha256(marketplace_manifest.read_bytes()).hexdigest(),
        "bytes": marketplace_manifest.stat().st_size,
        "mode": 0o600,
    } in snapshot_ledger["files"]
    marketplace_root = tmp_path / "mutable-marketplace"
    marketplace_root.mkdir()
    index_path = tmp_path / "locked-index.json"
    index_path.write_text("{}", encoding="utf-8")
    created_homes: list[Any] = []
    prepare_arm_home = script_run_codex.prepare_arm_home

    def _prepare(arm: str, **_kwargs: Any) -> Any:
        """Prepare and record an isolated arm home."""
        home = prepare_arm_home(arm, root=tmp_path)
        created_homes.append(home)
        return home

    commands: list[list[str]] = []
    frozen_marketplace: Path | None = None
    sources_by_selector: dict[str, Path] = {}
    installed_paths: dict[str, Path] = {}

    def _command_runner(command: list[str], **kwargs: Any) -> SimpleNamespace:
        """Record commands and return fixture responses."""
        nonlocal frozen_marketplace
        commands.append(command)
        if command[1:4] == ["plugin", "marketplace", "add"]:
            frozen_marketplace = Path(command[4]).resolve()
            assert frozen_marketplace != marketplace_root.resolve()
            assert frozen_marketplace.is_relative_to(snapshot_root.resolve())
            manifest = json.loads(
                (frozen_marketplace / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8")
            )
            assert manifest["name"] == frozen_name
            sources_by_selector.update(
                {
                    f"{entry['name']}@{frozen_name}": (frozen_marketplace / entry["source"]["path"]).resolve()
                    for entry in manifest["plugins"]
                }
            )
            assert sources_by_selector == {
                f"codemap-py@{frozen_name}": codemap_source.resolve(),
                f"codex-rig@{frozen_name}": rig_source.resolve(),
            }
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[1:3] == ["plugin", "add"]:
            assert frozen_marketplace is not None
            assert command[3] in sources_by_selector
            source = sources_by_selector[command[3]]
            home = Path(kwargs["env"]["CODEX_HOME"])
            installed = (
                home
                / "plugins"
                / "cache"
                / "fixture"
                / source.name
                / ("0.27.0" if source.name == "codemap-py" else "0.4.0")
            )
            shutil.copytree(source, installed)
            installed_paths[source.name] = installed
            return SimpleNamespace(returncode=0, stdout=json.dumps({"installedPath": str(installed)}), stderr="")
        if command[1:3] == ["plugin", "list"]:
            return SimpleNamespace(
                returncode=0,
                stdout='{"installed":[{"name":"codemap-py"},{"name":"codex-rig"}]}',
                stderr="",
            )
        raise AssertionError(command)

    # The measured workspace is a sibling of the disposable homes, as in a real run: a cell's gate is prepared outside
    # the workspace, so a fixture that nests the two would exercise a layout the runner refuses.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = script_run_codex.CodexRunner(
        "fixture-model",
        workspace,
        index_path=index_path,
        marketplace_root=marketplace_root,
        command_runner=_command_runner,
    )
    runner._bind_runtime_snapshot(
        snapshot_root,
        {"C_strict": {"codemap-py": codemap_source, "codex-rig": rig_source}},
    )
    monkeypatch.setattr(script_run_codex, "prepare_arm_home", _prepare)
    monkeypatch.setattr(script_run_codex, "_validate_locked_runtime", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(script_run_codex, "_verify_locked_codemap_python", lambda **_kwargs: "/usr/bin/python3")
    monkeypatch.setattr(script_run_codex, "_verify_treatment_artifact_locks", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(script_run_codex, "_admit_installed_skill_pair", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(script_run_codex, "_write_permission_config", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(script_run_codex, "_verify_installed_plugin_pair", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(script_run_codex, "_verify_permission_profile", lambda *_args, **_kwargs: None)

    home = runner._prepare_verified_home("C_strict")
    try:
        assert frozen_marketplace is not None
        assert home.codemap_plugin_path == installed_paths["codemap-py"]
        assert home.codex_rig_path == installed_paths["codex-rig"]
        assert (home.codemap_plugin_path / ".codex-plugin" / "plugin.json").read_bytes() == (
            codemap_source / ".codex-plugin" / "plugin.json"
        ).read_bytes()
        assert (home.codex_rig_path / ".codex-plugin" / "plugin.json").read_bytes() == (
            rig_source / ".codex-plugin" / "plugin.json"
        ).read_bytes()
    finally:
        assert home.coordination_path is not None
        script_run_codex._cleanup_coordination_root(home.coordination_path)
        home.cleanup()
        runner.close()
    assert len(created_homes) == 1
    assert commands[:3] == [
        ["codex", "plugin", "marketplace", "add", str(frozen_marketplace)],
        ["codex", "plugin", "add", f"codemap-py@{frozen_name}", "--json"],
        ["codex", "plugin", "add", f"codex-rig@{frozen_name}", "--json"],
    ]
    assert all(str(marketplace_root.resolve()) not in command for command in commands)


def test_bound_runtime_snapshot_rejects_byte_drift_and_records_observed_identity(
    script_run_codex: Any,
    tmp_path: Path,
) -> None:
    """A changed snapshot source must fail with expected/observed identity evidence.

    Prevents a mutable local archive from silently becoming the next cell's plugin source and preserves the identity
    that caused an admission failure.
    """
    snapshot_root = tmp_path / "run" / "inputs"
    snapshot_root.mkdir(parents=True)
    source = _write_snapshot_plugin_tree(snapshot_root / "C_strict", "codemap-py", "0.27.0")
    rig_source = _write_snapshot_plugin_tree(snapshot_root / "C_strict", "codex-rig", "0.4.0")
    _write_runtime_snapshot_metadata(
        snapshot_root,
        {"C_strict:codemap-py": source, "C_strict:codex-rig": rig_source},
    )
    runner = script_run_codex.CodexRunner("fixture-model", tmp_path)
    runner._bind_runtime_snapshot(
        snapshot_root,
        {"C_strict": {"codemap-py": source, "codex-rig": rig_source}},
    )
    manifest = source / ".codex-plugin" / "plugin.json"
    manifest.write_text('{"name":"codemap-py","version":"drifted"}', encoding="utf-8")

    with pytest.raises(ValueError, match=r"codemap-py.*expected=.*observed="):
        runner._runtime_plugin_sources("C_strict")

    evidence_path = snapshot_root.parent / "runtime-isolation.jsonl"
    runner._record_runtime_failure("C_strict", ValueError("snapshot byte drift"), source_paths=[source])
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["arm"] == "C_strict"
    assert evidence["error"] == "snapshot byte drift"
    assert evidence["observed_plugin_identities"]["codemap-py"]["version"] == "drifted"


def test_verified_runtime_identity_is_recorded_before_home_cleanup(script_run_codex: Any, tmp_path: Path) -> None:
    """A successful first C admission preserves the exact identities that later cells reuse."""
    codemap = _write_snapshot_plugin_tree(tmp_path / "plugins", "codemap-py", "0.28.6")
    codex_rig = _write_snapshot_plugin_tree(tmp_path / "plugins", "codex-rig", "0.4.3")
    codemap_manifest = codemap / ".codex-plugin" / "plugin.json"
    codex_rig_manifest = codex_rig / ".codex-plugin" / "plugin.json"
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "artifact_sha256": {
                    "codemap_candidate_manifest": hashlib.sha256(codemap_manifest.read_bytes()).hexdigest(),
                    "codex_rig_plugin_manifest": hashlib.sha256(codex_rig_manifest.read_bytes()).hexdigest(),
                },
                "codemap_candidate": {"version": "0.28.6"},
                "codex_rig_candidate": {"version": "0.4.3"},
            }
        ),
        encoding="utf-8",
    )
    runner = script_run_codex.CodexRunner("fixture-model", tmp_path, manifest_path=manifest_path)
    runner._runtime_evidence_path = tmp_path / "runtime-isolation.jsonl"
    home = SimpleNamespace(codemap_plugin_path=codemap, codex_rig_path=codex_rig)

    runner._record_runtime_success("C_strict", home)

    evidence = json.loads(runner._runtime_evidence_path.read_text(encoding="utf-8"))
    assert evidence["status"] == "verified"
    assert evidence["error"] is None
    assert evidence["expected_plugin_identities"] == evidence["observed_plugin_identities"]


def test_treatment_version_drift_message_names_versions_and_recovery(script_run_codex: Any) -> None:
    """Version admission failures must provide a safe manifest-and-scope recovery path."""
    message = script_run_codex._treatment_artifact_version_mismatch_message({"codex-rig": ("0.5.0", "0.5.1")})

    assert "codex-rig: manifest=0.5.0, installed=0.5.1" in message
    assert "No paid model call was started." in message
    assert "build-codex-integration-manifest.py" in message
    assert "Do not reuse the previous --paid-approval value." in message


@POSIX_SECURITY
def test_initial_skill_admission_failure_keeps_identity_evidence_after_cleanup(
    script_run_codex: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first C admission failure persists identities before its home is removed."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "artifact_sha256": {
                    "codemap_candidate_manifest": "codemap-locked",
                    "codex_rig_plugin_manifest": "rig-locked",
                },
                "codemap_candidate": {"version": "0.27.0"},
                "codex_rig_candidate": {"version": "0.4.0"},
            }
        ),
        encoding="utf-8",
    )
    tasks_path = tmp_path / "tasks.json"
    tasks_path.write_text("{}", encoding="utf-8")
    marketplace_root = tmp_path / "marketplace"
    marketplace_root.mkdir()
    runner = script_run_codex.CodexRunner(
        "fixture-model",
        tmp_path,
        manifest_path=manifest_path,
        marketplace_root=marketplace_root,
    )

    def _fail_after_staging(home: Any, *_args: Any, **_kwargs: Any) -> bool:
        """Create staged plugin identities before simulating an installation failure."""
        for name, version in (("codemap-py", "0.27.0"), ("codex-rig", "0.4.0")):
            plugin = home.path / "plugins" / name
            (plugin / ".codex-plugin").mkdir(parents=True)
            (plugin / ".codex-plugin" / "plugin.json").write_text(
                json.dumps({"name": name, "version": version}), encoding="utf-8"
            )
            setattr(home, "codemap_plugin_path" if name == "codemap-py" else "codex_rig_path", plugin)
        raise RuntimeError("fixture plugin identity mismatch")

    monkeypatch.setattr(script_run_codex, "_validate_locked_runtime", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(script_run_codex, "_install_codemap_plugin", _fail_after_staging)

    with pytest.raises(RuntimeError, match="fixture plugin identity mismatch"):
        runner.create_input_snapshot(
            run_dir,
            tasks_path=tasks_path,
            manifest_path=manifest_path,
            tasks=[],
            arms=["C_strict"],
        )

    evidence_path = run_dir / "runtime-isolation.jsonl"
    assert evidence_path.stat().st_mode & 0o777 == 0o600
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["arm"] == "C_strict"
    assert evidence["error"] == "fixture plugin identity mismatch"
    assert evidence["expected_plugin_identities"]["codemap-py"]["version"] == "0.27.0"
    assert evidence["observed_plugin_identities"]["codex-rig"]["version"] == "0.4.0"
    assert not any(tmp_path.rglob("plugin.json"))


@pytest.mark.parametrize(
    "installed",
    [
        pytest.param([{"name": "codemap-py", "enabled": True}], id="missing-rig"),
        pytest.param(
            [{"name": "codemap-py", "enabled": True}, {"name": "codex-rig", "enabled": False}],
            id="disabled-rig",
        ),
        pytest.param(
            [
                {"name": "codemap-py", "enabled": True},
                {"name": "codex-rig", "enabled": True},
                {"name": "unrelated", "enabled": True},
            ],
            id="extra-plugin",
        ),
    ],
)
def test_final_skill_plugin_roster_rejects_missing_disabled_or_extra_entries(
    script_run_codex: Any,
    tmp_path: Path,
    installed: list[dict[str, object]],
) -> None:
    """Final C admission requires exactly the two enabled treatment plugins."""
    home_path = tmp_path / "codex-home"
    home_path.mkdir()
    home = script_run_codex.ArmHome("C_strict", home_path, {}, True, True)

    def _command_runner(_command: list[str], **_kwargs: Any) -> SimpleNamespace:
        """Record commands and return fixture responses."""
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"installed": installed, "available": []}),
            stderr="",
        )

    with pytest.raises(RuntimeError, match="plugin registration"):
        script_run_codex._verify_installed_plugin_pair(home, command_runner=_command_runner)


@POSIX_SECURITY
def test_auth_source_is_copied_with_private_modes_and_removed_with_home(script_run_codex: Any, tmp_path: Path) -> None:
    """One explicit auth source must remain private and disappear with its arm home."""
    auth_source = tmp_path / "source-auth.json"
    auth_source.write_text('{"fixture_token":"do-not-report"}', encoding="utf-8")
    auth_source.chmod(0o600)

    with script_run_codex.prepare_arm_home(
        "A_plain",
        root=tmp_path,
        auth_source=auth_source,
    ) as home:
        home_path = home.path
        copied_auth = home.path / "auth.json"
        assert home.path.stat().st_mode & 0o777 == 0o700
        assert copied_auth.stat().st_mode & 0o777 == 0o600
        assert copied_auth.read_bytes() == auth_source.read_bytes()
        assert "do-not-report" not in json.dumps(script_run_codex.probe_arm_home(home))

    assert not home_path.exists()
    assert auth_source.read_text(encoding="utf-8") == '{"fixture_token":"do-not-report"}'


def test_arm_home_drops_batch_controls_from_codex_process_environment(
    script_run_codex: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local approval, auth-source, and result values must not reach Codex."""
    control_names = (
        "CODEX_PAID_APPROVAL",
        "CODEX_AUTH_SOURCE",
        "CODEX_RUN_DIR",
    )
    for name in control_names:
        monkeypatch.setenv(name, f"private-{name.lower()}")

    with script_run_codex.prepare_arm_home("A_plain", root=tmp_path) as home:
        assert all(name not in home.env for name in control_names)


@POSIX_SECURITY
def test_auth_source_rejects_insecure_permissions_and_symlinks(script_run_codex: Any, tmp_path: Path) -> None:
    """Credential propagation must fail closed on readable or indirect sources."""
    auth_source = tmp_path / "source-auth.json"
    auth_source.write_text("fixture", encoding="utf-8")
    auth_source.chmod(0o644)

    with pytest.raises(ValueError, match="permissions"):
        script_run_codex.prepare_arm_home(
            "A_plain",
            root=tmp_path,
            auth_source=auth_source,
        )

    auth_source.chmod(0o600)
    auth_link = tmp_path / "auth-link.json"
    auth_link.symlink_to(auth_source)
    with pytest.raises(ValueError, match="path is unsafe"):
        script_run_codex.prepare_arm_home(
            "A_plain",
            root=tmp_path,
            auth_source=auth_link,
        )


@POSIX_SECURITY
@pytest.mark.parametrize("violation", ["permissions", "owner"])
def test_arm_home_cleanup_rejects_non_private_credential_directory(
    script_run_codex: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    violation: str,
) -> None:
    """Preserve credential homes whose ownership or private permissions changed after creation.

    Blind recursive cleanup must fail the preservation assertion.
    """
    home_path = tmp_path / violation
    home_path.mkdir()
    home_path.chmod(0o700 if violation == "owner" else 0o755)
    if violation == "owner":
        current_uid = os.getuid()
        monkeypatch.setattr(script_run_codex.os, "getuid", lambda: current_uid + 1)

    home = script_run_codex.ArmHome("A_plain", home_path, {}, False)

    with pytest.raises(RuntimeError, match="owned by the current user|permissions must be exactly 0700"):
        home.cleanup()

    assert home_path.is_dir()


@POSIX_SECURITY
def test_prepare_verified_home_cleans_credential_home_after_keyboard_interrupt(
    script_run_codex: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interrupting authentication cannot retain a credential-bearing cell home.

    Prevents the former ``except Exception`` cleanup path from missing ``KeyboardInterrupt`` after the run auth state
    has seeded ``auth.json``.
    """
    auth_source = tmp_path / "source-auth.json"
    auth_source.write_text('{"refresh_token":"fixture"}', encoding="utf-8")
    auth_source.chmod(0o600)
    created_homes: list[Any] = []
    prepare_original = script_run_codex.prepare_arm_home

    def _prepare(arm: str, **kwargs: Any) -> Any:
        """Prepare and record an isolated arm home."""
        home = prepare_original(arm, root=tmp_path, **kwargs)
        created_homes.append(home)
        return home

    def _interrupt_authentication(*_args: Any, **_kwargs: Any) -> None:
        """Interrupt authentication setup before provider execution."""
        raise KeyboardInterrupt("fixture interrupt")

    runner = script_run_codex.CodexRunner("fixture-model", tmp_path, auth_source=auth_source)
    monkeypatch.setattr(script_run_codex, "_validate_locked_runtime", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(script_run_codex, "prepare_arm_home", _prepare)
    monkeypatch.setattr(script_run_codex, "_write_permission_config", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(script_run_codex, "_verify_permission_profile", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(script_run_codex, "_verify_authentication", _interrupt_authentication)

    try:
        with pytest.raises(KeyboardInterrupt, match="fixture interrupt"):
            runner._prepare_verified_home("A_plain")
    finally:
        runner.close()

    assert len(created_homes) == 1
    assert not created_homes[0].path.exists()


@POSIX_SECURITY
def test_auth_source_path_validation_error_is_generic_and_does_not_expose_source_path(
    script_run_codex: Any, tmp_path: Path
) -> None:
    """Unsafe credential paths fail before a provider result can expose their location.

    Prevents a symlink-component validation error from embedding the approved auth-source path in terminal output or any
    later persisted provider row.
    """
    source_root = tmp_path / "source-root"
    source_root.mkdir()
    real_source = source_root / "auth.json"
    real_source.write_text('{"refresh_token":"fixture"}', encoding="utf-8")
    real_source.chmod(0o600)
    alias = tmp_path / "source-alias"
    alias.symlink_to(source_root, target_is_directory=True)
    auth_source = alias / "auth.json"
    runner = script_run_codex.CodexRunner("fixture-model", tmp_path, auth_source=auth_source)

    with pytest.raises(ValueError) as raised:
        runner._ensure_auth_state()

    assert "path is unsafe" in str(raised.value)
    assert raised.value.__cause__ is None
    rendered = "".join(traceback.format_exception(raised.value))
    for private_path in (auth_source, alias, real_source):
        assert str(private_path) not in rendered


@POSIX_SECURITY
def test_probe_verifies_authentication_without_disclosing_auth_source(
    script_run_codex: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A no-model probe must prove login while returning no credential material.

    The probe rejects an A_plain home that can reach a Codemap binary, so the host's own PATH — which carries the
    installed plugin on a development machine — has to be replaced with an empty directory for the assertion under test
    to be about authentication rather than the developer's shell.
    """
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    auth_source = tmp_path / "source-auth.json"
    auth_source.write_text('{"fixture_token":"do-not-report"}', encoding="utf-8")
    auth_source.chmod(0o600)
    calls: list[list[str]] = []

    def _command_runner(command: list[str], **kwargs: Any) -> SimpleNamespace:
        """Record commands and return fixture responses."""
        calls.append(command)
        return _successful_plain_profile_command(command, **kwargs)

    monkeypatch.setattr(script_run_codex, "_validate_locked_runtime", lambda *_args, **_kwargs: None)
    fixture_index = tmp_path / "fixture-index.json"
    fixture_index.write_text("{}", encoding="utf-8")
    runner = script_run_codex.CodexRunner(
        "fixture-model",
        tmp_path,
        index_path=fixture_index,
        auth_source=auth_source,
        command_runner=_command_runner,
    )

    evidence = runner.probe_arm("A_plain")

    assert evidence["authenticated"] is True
    assert calls[0] == ["codex", "--version"]
    assert ["codex", "login", "status"] in calls
    assert "do-not-report" not in json.dumps(evidence)
    assert str(auth_source) not in json.dumps(evidence)


@POSIX_SECURITY
def test_runner_cleans_auth_home_when_transport_raises(
    script_run_codex: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unexpected execution failures cannot leave a copied credential on disk."""
    auth_source = tmp_path / "source-auth.json"
    auth_source.write_text('{"fixture_token":"do-not-report"}', encoding="utf-8")
    auth_source.chmod(0o600)
    homes: list[Path] = []
    original_prepare_arm_home = script_run_codex.prepare_arm_home

    def _prepare_home(arm: str, **kwargs: Any) -> Any:
        """Prepare and record an isolated home using the original helper."""
        home = original_prepare_arm_home(arm, root=tmp_path, **kwargs)
        homes.append(home.path)
        return home

    monkeypatch.setattr(script_run_codex, "_validate_locked_runtime", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(script_run_codex, "_verify_plain_plugin_absent", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(script_run_codex, "prepare_arm_home", _prepare_home)
    fixture_index = tmp_path / "fixture-index.json"
    fixture_index.write_text("{}", encoding="utf-8")
    runner = script_run_codex.CodexRunner(
        "fixture-model",
        tmp_path,
        index_path=fixture_index,
        auth_source=auth_source,
        command_runner=_successful_plain_profile_command,
        evaluator=lambda *_args: core.EvaluationResult(scored=True, correct=True, quality_score=1.0),
    )
    monkeypatch.setattr(
        runner,
        "_subprocess",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("fixture transport failure")),
    )

    with pytest.raises(RuntimeError, match="fixture transport failure"):
        runner.run({"id": "fixture", "prompt": "prompt", "type": "demo"}, "A_plain")

    assert homes
    assert all(not home.exists() for home in homes)


@POSIX_SECURITY
def test_runner_reuses_rotated_auth_state_without_mutating_immutable_source(
    script_run_codex: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seed each cell from the refreshed private credential chain.

    Reject source write-back, broad directory permissions, and a retained second link.
    """
    source = tmp_path / "immutable-auth.json"
    source_bytes = b'{"state":"seed"}'
    rotated_bytes = b'{"state":"rotated"}'
    source.write_bytes(source_bytes)
    source.chmod(0o600)
    seen: list[bytes] = []
    index_path = tmp_path / "fixture-index.json"
    index_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(script_run_codex, "_validate_locked_runtime", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(script_run_codex, "_verify_plain_plugin_absent", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(script_run_codex, "_verify_permission_profile", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        script_run_codex, "_verify_authentication", lambda home, **_kwargs: setattr(home, "authenticated", True)
    )
    runner = script_run_codex.CodexRunner(
        "fixture-model",
        tmp_path,
        index_path=index_path,
        auth_source=source,
        evaluator=lambda *_args: core.EvaluationResult(scored=True, correct=True, quality_score=1.0),
    )

    def _rotating_subprocess(_command: list[str], env: dict[str, str], **_kwargs: Any) -> str:
        """Record copied authentication bytes, rotate the fixture copy, and return success."""
        copied_auth = Path(env["CODEX_HOME"]) / "auth.json"
        seen.append(copied_auth.read_bytes())
        copied_auth.write_bytes(rotated_bytes)
        copied_auth.chmod(0o600)
        return _completed_stream()

    monkeypatch.setattr(runner, "_subprocess", _rotating_subprocess)
    try:
        runner.run({"id": "first", "prompt": "prompt", "type": "demo"}, "A_plain")
        runner.run({"id": "second", "prompt": "prompt", "type": "demo"}, "A_plain")

        assert seen == [source_bytes, rotated_bytes]
        assert source.read_bytes() == source_bytes
        state_dir = runner._auth_state_dir
        assert state_dir is not None
        assert state_dir.stat().st_mode & 0o777 == 0o700
        state_auth = state_dir / "auth.json"
        assert state_auth.read_bytes() == rotated_bytes
        assert state_auth.stat().st_mode & 0o777 == 0o600
        assert state_auth.stat().st_nlink == 1
        assert not state_dir.is_relative_to(tmp_path / "results")
    finally:
        closer = getattr(runner, "close", None)
        if callable(closer):
            closer()

    assert state_dir is not None
    assert not state_dir.exists()
    runner.close()


@POSIX_SECURITY
def test_runner_rejects_auth_source_drift_before_the_next_model_call(
    script_run_codex: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The immutable source cannot be swapped between authenticated cells.

    Prevents a caller from silently replacing the reviewed source after the run starts.  The transport call count proves
    detection occurs before the next model process is launched.
    """
    source = tmp_path / "immutable-auth.json"
    source.write_bytes(b'{"state":"seed"}')
    source.chmod(0o600)
    calls = 0
    index_path = tmp_path / "fixture-index.json"
    index_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(script_run_codex, "_validate_locked_runtime", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(script_run_codex, "_verify_plain_plugin_absent", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(script_run_codex, "_verify_permission_profile", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        script_run_codex, "_verify_authentication", lambda home, **_kwargs: setattr(home, "authenticated", True)
    )
    runner = script_run_codex.CodexRunner(
        "fixture-model",
        tmp_path,
        index_path=index_path,
        auth_source=source,
        evaluator=lambda *_args: core.EvaluationResult(scored=True, correct=True, quality_score=1.0),
    )

    def _subprocess_fixture(*_args: Any, **_kwargs: Any) -> str:
        """Count invocations and return a successful native stream."""
        nonlocal calls
        calls += 1
        return _completed_stream()

    monkeypatch.setattr(runner, "_subprocess", _subprocess_fixture)
    try:
        runner.run({"id": "first", "prompt": "prompt", "type": "demo"}, "A_plain")
        source.write_bytes(b'{"state":"changed"}')
        source.chmod(0o600)

        with pytest.raises(ValueError, match="auth source.*changed|changed.*auth source"):
            runner.run({"id": "second", "prompt": "prompt", "type": "demo"}, "A_plain")
    finally:
        closer = getattr(runner, "close", None)
        if callable(closer):
            closer()

    assert calls == 1


def test_probe_requires_verified_treatment_home(script_run_codex: Any, tmp_path: Path) -> None:
    """A copied or merely declared treatment home is not installation evidence."""
    with script_run_codex.prepare_arm_home("C_strict", root=tmp_path) as home:
        with pytest.raises(ValueError, match="verified"):
            script_run_codex.probe_arm_home(home)
        home.codemap_available = True
        home.codemap_verified = True
        with pytest.raises(ValueError, match="exact installed Skill binding"):
            script_run_codex.probe_arm_home(home)
        skill_path = home.path / "query-code" / "SKILL.md"
        skill_path.parent.mkdir()
        skill_path.write_text("# query-code\n", encoding="utf-8")
        home.codemap_skill_path = skill_path.resolve()
        home.env["CODEMAP_SKILL_FILE"] = "/wrong/SKILL.md"
        with pytest.raises(ValueError, match="exact installed Skill binding"):
            script_run_codex.probe_arm_home(home)
        home.env["CODEMAP_SKILL_FILE"] = str(skill_path.resolve())
        evidence = script_run_codex.probe_arm_home(home)

    assert evidence["codemap_available"] is True
    assert evidence["codemap_verified"] is True


def test_locked_runtime_requires_one_shared_locked_index_for_every_arm(
    script_run_codex: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A/B/C share exact index identity while A's profile denies model access."""
    repo_path = tmp_path / "codemap-target"
    repo_path.mkdir()
    index_path = repo_path / ".cache" / "codemap" / "codemap-target.json"
    index_path.parent.mkdir(parents=True)
    index_path.write_text('{"git_sha":"fixture-commit","scan_version":11}', encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "target_source": {"commit": "fixture-commit"},
                "index": {
                    "raw_sha256": hashlib.sha256(index_path.read_bytes()).hexdigest(),
                    "git_sha": "fixture-commit",
                    "scan_version": 11,
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(script_run_codex, "PARITY_MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(script_run_codex, "_repo_sha", lambda _path: "fixture-commit")
    monkeypatch.setattr(
        script_run_codex.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    script_run_codex._validate_locked_runtime(repo_path, index_path, "A_plain", manifest_path)
    script_run_codex._validate_locked_runtime(repo_path, index_path, "B_auto", manifest_path)

    with pytest.raises(ValueError, match="requires the locked index"):
        script_run_codex._validate_locked_runtime(repo_path, None, "A_plain", manifest_path)


def test_locked_runtime_admits_only_a_provenance_bound_worktree_index(
    script_run_codex: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Executable cells may relocate scan_root without weakening graph identity."""
    source_root = tmp_path / "source"
    worktree_root = tmp_path / "worktree"
    index_path = worktree_root / ".cache" / "codemap" / "worktree.json"
    index_path.parent.mkdir(parents=True)
    frozen_bytes = json.dumps(
        {
            "git_sha": "fixture-commit",
            "modules": {"pkg": ["symbol"]},
            "scan_root": str(source_root),
            "scan_version": 13,
        },
        sort_keys=True,
    ).encode("utf-8")
    derived_bytes, relocation = script_run_codex.relocate_frozen_index_for_worktree(
        frozen_bytes,
        source_root=source_root,
        worktree_root=worktree_root,
    )
    index_path.write_bytes(derived_bytes)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "target_source": {"commit": "fixture-commit"},
                "index": {
                    "raw_sha256": hashlib.sha256(frozen_bytes).hexdigest(),
                    "git_sha": "fixture-commit",
                    "scan_version": 13,
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(script_run_codex, "_repo_sha", lambda _path: "fixture-commit")
    monkeypatch.setattr(script_run_codex, "_git_porcelain_status", lambda _path: {})

    script_run_codex._validate_locked_runtime(
        worktree_root,
        index_path,
        "C_strict",
        manifest_path,
        index_relocation=relocation,
    )

    tampered = json.loads(index_path.read_text(encoding="utf-8"))
    tampered["modules"] = {"pkg": ["changed"]}
    index_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="changed after relocation"):
        script_run_codex._validate_locked_runtime(
            worktree_root,
            index_path,
            "C_strict",
            manifest_path,
            index_relocation=relocation,
        )


def test_historical_runtime_coordinate_uses_patch_baseline_not_main_manifest(
    script_run_codex: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Patch worktrees validate their reviewed coordinate without weakening manifest stages."""
    source_root = tmp_path / "source"
    worktree_root = tmp_path / "historical-worktree"
    baseline_commit = "a" * 40
    index_path = worktree_root / ".cache" / "codemap" / "historical-worktree.json"
    index_path.parent.mkdir(parents=True)
    frozen_bytes = json.dumps(
        {
            "git_sha": baseline_commit,
            "modules": {"pkg": ["symbol"]},
            "scan_root": str(source_root),
            "scan_version": 13,
        },
        sort_keys=True,
    ).encode("utf-8")
    derived_bytes, relocation = script_run_codex.relocate_frozen_index_for_worktree(
        frozen_bytes,
        source_root=source_root,
        worktree_root=worktree_root,
    )
    index_path.write_bytes(derived_bytes)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "target_source": {"commit": "b" * 40},
                "index": {"raw_sha256": "c" * 64, "git_sha": "b" * 40, "scan_version": 13},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(script_run_codex, "_repo_sha", lambda _path: baseline_commit)
    monkeypatch.setattr(script_run_codex, "_git_porcelain_status", lambda _path: {})
    coordinate = {
        "baseline_commit": baseline_commit,
        "raw_index_sha256": hashlib.sha256(frozen_bytes).hexdigest(),
        "scan_version": "13",
    }

    script_run_codex._validate_locked_runtime(
        worktree_root,
        index_path,
        "C_strict",
        manifest_path,
        index_relocation=relocation,
        historical_runtime_coordinate=coordinate,
    )

    with pytest.raises(ValueError, match="historical Patch run requires target commit"):
        script_run_codex._validate_locked_runtime(
            worktree_root,
            index_path,
            "C_strict",
            manifest_path,
            index_relocation=relocation,
            historical_runtime_coordinate={**coordinate, "baseline_commit": "d" * 40},
        )


@pytest.mark.parametrize("line_ending", [b"\n", b"\r\n"], ids=["lf", "crlf"])
def test_fixture_runtime_coordinate_is_distinct_from_graph_admission(
    script_run_codex: Any, tmp_path: Path, line_ending: bytes
) -> None:
    """A copied fixture bypasses neither the PL lock nor graph relocation admission."""
    source_root = tmp_path / "repo"
    source_root.mkdir()
    source = b"def quota(value: int) -> int:\n    return value\n".replace(b"\n", line_ending)
    (source_root / "impact.py").write_bytes(source)
    index_path = source_root / ".cache" / "codemap" / "repo.json"
    index_path.parent.mkdir(parents=True)
    index_path.write_text(json.dumps({"scan_version": 13, "scan_root": str(source_root), "modules": {}}))
    coordinate = {
        "source_fingerprint": hashlib.sha256(b"impact.py\x00" + source + b"\x00").hexdigest(),
        "raw_index_sha256": hashlib.sha256(index_path.read_bytes()).hexdigest(),
        "scan_version": 13,
        "index_scan_root": str(source_root),
    }

    script_run_codex._validate_locked_runtime(source_root, index_path, "A_plain", fixture_runtime_coordinate=coordinate)

    with pytest.raises(ValueError, match="source fingerprint drift"):
        script_run_codex._validate_locked_runtime(
            source_root,
            index_path,
            "A_plain",
            fixture_runtime_coordinate={**coordinate, "source_fingerprint": "0" * 64},
        )

    with pytest.raises(ValueError, match="cannot combine"):
        script_run_codex._validate_locked_runtime(
            source_root,
            index_path,
            "A_plain",
            index_relocation={},
            fixture_runtime_coordinate=coordinate,
        )


def test_result_exposes_native_telemetry_and_turn_limit_capability(script_run_codex: Any, tmp_path: Path) -> None:
    """Every result keeps measurable Codex-native fields and the turn-limit gap."""
    runner = script_run_codex.CodexRunner(
        "fixture-model",
        tmp_path,
        transport=lambda *_args, **_kwargs: _completed_stream(),
        evaluator=lambda *_args: core.EvaluationResult(scored=True, correct=True, quality_score=1.0),
    )

    result = runner.run({"id": "fixture", "prompt": "prompt", "type": "demo"}, "B_auto")

    assert result.elapsed_s >= 0.0
    assert result.native_item_counts == {"agent_message": 1}
    assert result.native_attempt_events == [result.raw_events]
    assert result.telemetry_contract_id == "installed-skill-binding-locked-query-components-v3"
    assert result.tool_elapsed_s is None
    assert result.tool_result_tokens is None
    assert result.error_type == ""
    assert result.turn_budget_enforced is False
    assert result.reasoning_effort == "high"


def test_default_evaluator_score_and_identity_match_claude_reference(
    script_run_codex: Any, script_run_bench: Any
) -> None:
    """The Codex adapter must call the exact Claude evaluator and provenance path."""
    task = next(task for task in core.load_task_suite(SUITE_PATH) if task["id"] == "FN-02")
    output_text = "## Callers\n" + "\n".join(task["ground_truth"]["fn_callers"])

    claude = script_run_bench._evaluate_shared_task(task, output_text)
    codex = script_run_codex._default_evaluator(task, output_text)

    assert codex.scored is claude.scored
    assert codex.correct is claude.correct
    assert codex.quality_score == pytest.approx(claude.recall)
    assert codex.components["recall"] == pytest.approx(claude.recall)
    assert script_run_codex._evaluator_identity(
        task, script_run_codex._default_evaluator
    ) == script_run_bench._evaluator_provenance(task)


def test_runner_default_timeout_matches_shared_parity_contract(script_run_codex: Any, tmp_path: Path) -> None:
    """The Codex adapter inherits the same provider-neutral wall-clock budget."""
    runner = script_run_codex.CodexRunner("fixture-model", tmp_path)

    assert runner.timeout == core.PARITY_TIMEOUT_SECONDS == 600


def test_subprocess_timeout_and_nonzero_exit_keep_distinct_error_types(
    script_run_codex: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Transport timeout and nonzero exit remain separately diagnosable."""
    runner = script_run_codex.CodexRunner("fixture-model", tmp_path)

    monkeypatch.setattr(script_run_codex.subprocess, "Popen", _FakePopen.factory(timeout_after_streaming=""))
    monkeypatch.setattr(script_run_codex, "terminate_process_group", lambda _process: None)
    timed_out = codex_runtime.parse_codex_jsonl(runner._subprocess(["codex"], {}))

    assert timed_out.incomplete is True
    assert timed_out.error_type == "timeout"

    monkeypatch.setattr(
        script_run_codex.subprocess,
        "Popen",
        _FakePopen.factory(returncode=7, stdout=_completed_stream(output="partial answer"), stderr="CLI failed"),
    )
    nonzero = codex_runtime.parse_codex_jsonl(runner._subprocess(["codex"], {}))

    assert nonzero.output_text == "partial answer"
    assert nonzero.incomplete is True
    assert nonzero.error_type == "non_zero_exit"


class _FakePopen:
    """Minimal ``subprocess.Popen`` stand-in for transport tests.

    Reproduces the one behavior that matters here: on a timeout the pipes still hold
    whatever the child streamed before the kill, so the second ``communicate()`` after
    the kill returns it.
    """

    def __init__(
        self,
        *_args: Any,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        timeout_after_streaming: str | None = None,
        **_kwargs: Any,
    ) -> None:
        """Initialize fixture state."""
        self.pid = 4321
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._timeout_after_streaming = timeout_after_streaming
        self._timed_out = False
        self.killed = False

    @classmethod
    def factory(cls, **config: Any) -> Any:
        """Return a Popen-shaped callable bound to one fixed outcome."""
        return lambda *args, **kwargs: cls(*args, **{**kwargs, **config})

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        """Return buffered output, raising once when the fixture models a timeout."""
        if self._timeout_after_streaming is not None and not self._timed_out:
            self._timed_out = True
            raise subprocess.TimeoutExpired(["codex"], timeout or 600)
        if self._timed_out:
            return self._timeout_after_streaming or "", self._stderr
        return self._stdout, self._stderr

    def kill(self) -> None:
        """Record that the direct child was killed."""
        self.killed = True


def test_failed_coordination_cleanup_is_recorded_not_silently_dropped(
    script_run_codex: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same cleanup used to be suppressed at some sites and escalated at others.

    A suppressed failure left the coordination root behind with no trace anywhere, so a leak was invisible until
    something later tripped over it.
    """
    runner = script_run_codex.CodexRunner("fixture-model", tmp_path)
    coordination = tmp_path / "coordination"

    def _refuse(_path: Path) -> None:
        """Reject cleanup as though its target directory were not empty."""
        raise ValueError("directory not empty")

    monkeypatch.setattr(script_run_codex, "_cleanup_coordination_root", _refuse)

    message = runner._cleanup_coordination(coordination)

    assert message is not None
    assert runner.coordination_cleanup_errors == [message]
    assert "directory not empty" in message


def test_successful_coordination_cleanup_records_nothing(
    script_run_codex: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordinary path stays silent."""
    runner = script_run_codex.CodexRunner("fixture-model", tmp_path)
    monkeypatch.setattr(script_run_codex, "_cleanup_coordination_root", lambda _path: None)

    assert runner._cleanup_coordination(tmp_path / "coordination") is None
    assert runner.coordination_cleanup_errors == []


def test_coordination_cleanup_never_raises_from_a_finally_block(
    script_run_codex: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Raising from cleanup would mask the exception carrying the real cause."""
    runner = script_run_codex.CodexRunner("fixture-model", tmp_path)

    def _refuse(_path: Path) -> None:
        """Reject cleanup as though its target directory were not empty."""
        raise ValueError("directory not empty")

    monkeypatch.setattr(script_run_codex, "_cleanup_coordination_root", _refuse)

    try:
        raise RuntimeError("original cause")
    except RuntimeError as exc:
        runner._cleanup_coordination(tmp_path / "coordination")
        assert str(exc) == "original cause"


def test_timed_out_transport_preserves_streamed_usage_events(
    script_run_codex: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retain streamed usage after a timeout kill.

    Returning only an error envelope previously discarded billed tokens and persisted zero usage.
    """
    runner = script_run_codex.CodexRunner("fixture-model", tmp_path)
    streamed = _completed_stream(output="partial answer")
    monkeypatch.setattr(script_run_codex.subprocess, "Popen", _FakePopen.factory(timeout_after_streaming=streamed))
    monkeypatch.setattr(script_run_codex, "terminate_process_group", lambda _process: None)

    parsed = codex_runtime.parse_codex_jsonl(runner._subprocess(["codex"], {}))

    assert parsed.error_type == "timeout"
    assert parsed.output_text == "partial answer"


def test_timed_out_transport_kills_the_whole_process_group(
    script_run_codex: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Killing only the direct child leaves descendants burning paid budget."""
    runner = script_run_codex.CodexRunner("fixture-model", tmp_path)
    terminated: list[Any] = []
    monkeypatch.setattr(script_run_codex.subprocess, "Popen", _FakePopen.factory(timeout_after_streaming=""))
    monkeypatch.setattr(script_run_codex, "terminate_process_group", terminated.append)

    runner._subprocess(["codex"], {})

    assert len(terminated) == 1


def test_transport_child_starts_in_its_own_process_group(script_run_codex: Any) -> None:
    """The Popen keywords must actually detach the child."""
    group = script_run_codex.NEW_PROCESS_GROUP

    assert group.get("start_new_session") is True or "creationflags" in group


def test_transport_decodes_undecodable_bytes_instead_of_raising(
    script_run_codex: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Text mode without an error policy raises on malformed provider bytes."""
    runner = script_run_codex.CodexRunner("fixture-model", tmp_path)
    captured: dict[str, Any] = {}

    def _popen(*args: Any, **kwargs: Any) -> Any:
        """Capture process-launch options and return an empty-stream process double."""
        captured.update(kwargs)
        return _FakePopen(*args, **{**kwargs, "stdout": "", "stderr": ""})

    monkeypatch.setattr(script_run_codex.subprocess, "Popen", _popen)
    runner._subprocess(["codex"], {})

    assert captured["errors"] == "replace"


def test_main_dry_run_never_requires_or_writes_output(
    script_run_codex: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No-model planning performs probes without reserving a result artifact."""
    task = {"id": "fixture", "prompt": "prompt", "type": "demo"}
    monkeypatch.setattr(script_run_codex, "load_tasks_with_provenance", lambda _path, *_args: [task])
    monkeypatch.setattr(script_run_codex, "_read_manifest_revision", lambda *_args: "fixture-revision")
    monkeypatch.setattr(
        script_run_codex,
        "deterministic_arm_order",
        lambda *_args, **_kwargs: script_run_codex.CODEX_STRUCTURAL_ARMS,
    )

    class FixtureRunner:
        """Supply deterministic no-model arm probes."""

        timeout = 600.0

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            """Initialize fixture state."""
            pass

        def probe_arm(self, _arm: str) -> dict[str, bool]:
            """Report the arm's fixture availability."""
            return {"codemap_available": False}

    monkeypatch.setattr(script_run_codex, "CodexRunner", FixtureRunner)

    script_run_codex.main(
        repo_path=tmp_path,
        model=script_run_codex.PARITY_CODEX_MODEL,
        tasks_path=tmp_path / "tasks.json",
        dry_run=True,
    )

    assert list(tmp_path.iterdir()) == []


def test_dry_run_prints_the_manifest_driven_per_cell_timeout_without_global_deadline(
    script_run_codex: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reviewable plan exposes only the retry-inclusive per-cell timeout."""
    task = {"id": "fixture", "prompt": "prompt", "type": "demo"}
    planned: list[str] = []

    class FixtureRunner:
        """Supply deterministic no-model probe evidence."""

        timeout = 600.0

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            """Initialize fixture state."""
            pass

        def probe_arm(self, _arm: str) -> dict[str, bool]:
            """Report the arm's fixture availability."""
            return {"codemap_available": False}

    monkeypatch.setattr(script_run_codex, "load_tasks_with_provenance", lambda _path, *_args: [task])
    monkeypatch.setattr(script_run_codex, "_read_manifest_revision", lambda *_args: "fixture-revision")
    monkeypatch.setattr(script_run_codex, "CodexRunner", FixtureRunner)
    monkeypatch.setattr(script_run_codex, "print", planned.append, raising=False)
    monkeypatch.setattr(codex_runtime, "print_plan_row", planned.append)

    script_run_codex.main(
        repo_path=tmp_path,
        model=script_run_codex.PARITY_CODEX_MODEL,
        tasks_path=tmp_path / "tasks.json",
        arm="A_plain",
        dry_run=True,
    )

    assert "CONTROL\tcell_wall_clock_seconds=600" in planned
    assert all("max_wall_clock_seconds" not in row for row in planned)


def test_main_rejects_missing_or_existing_output_before_model_execution(
    script_run_codex: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Paid execution must have a fresh durable destination before constructing a runner."""
    task = {"id": "fixture", "prompt": "prompt", "type": "demo"}
    monkeypatch.setattr(script_run_codex, "load_tasks_with_provenance", lambda _path, *_args: [task])
    monkeypatch.setattr(script_run_codex, "_read_manifest_revision", lambda *_args: "fixture-revision")
    monkeypatch.setattr(
        script_run_codex,
        "CodexRunner",
        lambda *_args, **_kwargs: pytest.fail("invalid output reached runner construction"),
    )

    with pytest.raises(ValueError, match="output-path"):
        script_run_codex.main(
            repo_path=tmp_path,
            model=script_run_codex.PARITY_CODEX_MODEL,
            tasks_path=tmp_path / "tasks.json",
        )

    output_path = tmp_path / "existing.jsonl"
    output_path.write_text("preserve\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        script_run_codex.main(
            repo_path=tmp_path,
            model=script_run_codex.PARITY_CODEX_MODEL,
            tasks_path=tmp_path / "tasks.json",
            output_path=output_path,
        )
    assert output_path.read_text(encoding="utf-8") == "preserve\n"


def test_main_rejects_existing_canonical_telemetry_before_model_execution_or_mutation(
    script_run_codex: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A derived canonical sidecar cannot be replaced by a new paid run."""
    task = {"id": "fixture", "prompt": "prompt", "type": "demo"}
    output_path = tmp_path / "fresh.jsonl"
    metadata_path = tmp_path / "fresh-metadata.json"
    canonical_path = script_run_codex._canonical_telemetry_path(output_path)
    canonical_path.write_text('{"preserve": true}\n', encoding="utf-8")
    monkeypatch.setattr(script_run_codex, "load_tasks_with_provenance", lambda _path, *_args: [task])
    monkeypatch.setattr(
        script_run_codex,
        "CodexRunner",
        lambda *_args, **_kwargs: pytest.fail("existing sidecar reached runner construction"),
    )

    with pytest.raises(FileExistsError, match="telemetry-canonical\\.jsonl"):
        script_run_codex.main(
            repo_path=tmp_path,
            model=script_run_codex.PARITY_CODEX_MODEL,
            tasks_path=tmp_path / "tasks.json",
            output_path=output_path,
            metadata_path=metadata_path,
        )

    assert canonical_path.read_text(encoding="utf-8") == '{"preserve": true}\n'
    assert not output_path.exists()
    assert not metadata_path.exists()


def test_paid_main_uses_manifest_timeout_without_a_global_deadline(
    script_run_codex: Any,
) -> None:
    """The public runner exposes no process-wide wall-clock control surface."""
    assert "max_wall_clock_seconds" not in script_run_codex.main.__annotations__


def test_main_rejects_unreviewed_implementation_revision_before_reserving_output(
    script_run_codex: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Paid execution cannot use a manifest that does not hash the active runner."""
    task = {"id": "fixture", "prompt": "prompt", "type": "demo"}
    manifest_path = tmp_path / "manifest.json"
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest["experiment_revision"] = "prior-fixture-revision"
    manifest["implementation_contract"]["artifact_sha256"]["run_codex_structural"] = "0" * 64
    manifest_path.write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    monkeypatch.setattr(script_run_codex, "load_tasks_with_provenance", lambda _path, *_args: [task])
    monkeypatch.setattr(
        script_run_codex,
        "CodexRunner",
        lambda *_args, **_kwargs: pytest.fail("unreviewed manifest reached runner construction"),
    )
    output_path = tmp_path / "unreviewed.jsonl"

    with pytest.raises(ValueError, match="manifest locked to this runner"):
        script_run_codex.main(
            repo_path=tmp_path,
            model=script_run_codex.PARITY_CODEX_MODEL,
            tasks_path=tmp_path / "tasks.json",
            manifest_path=manifest_path,
            output_path=output_path,
        )

    assert not output_path.exists()


def test_main_persists_each_completed_cell_in_task_then_arm_order(
    script_run_codex: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial JSONL artifact retains every completed cell in deterministic plan order."""
    tasks = [{"id": "first", "prompt": "one", "type": "demo"}, {"id": "second", "prompt": "two", "type": "demo"}]
    monkeypatch.setattr(script_run_codex, "load_tasks_with_provenance", lambda _path, *_args: tasks)
    monkeypatch.setattr(script_run_codex, "_read_manifest_revision", lambda *_args: "fixture-revision")

    class FixtureRunner:
        """Return one minimal serializable result per planned cell."""

        timeout = 600.0

        def __init__(self, model: str, *_args: Any, **_kwargs: Any) -> None:
            """Initialize fixture state."""
            self.model = model

        def run(
            self,
            task: dict[str, Any],
            arm: str,
            *,
            repetition: int = 1,
        ) -> Any:
            """Return a fixture result without provider calls."""
            return script_run_codex.CodexRun(
                arm=arm,
                task_id=task["id"],
                task_type=task["type"],
                model=self.model,
                parity_arm=arm,
                repetition=repetition,
                success=True,
                scoreable=True,
                input_tokens=1,
                output_tokens=1,
                compliance=True,
            )

    monkeypatch.setattr(script_run_codex, "CodexRunner", FixtureRunner)
    monkeypatch.setattr(script_run_codex, "_validate_execution_manifest", lambda _path: None)
    monkeypatch.setattr(
        script_run_codex,
        "deterministic_arm_order",
        lambda *_args, **_kwargs: script_run_codex.CODEX_STRUCTURAL_ARMS,
    )
    output_path = tmp_path / "smoke.jsonl"

    script_run_codex.main(
        repo_path=tmp_path,
        model=script_run_codex.PARITY_CODEX_MODEL,
        tasks_path=tmp_path / "tasks.json",
        output_path=output_path,
        repetitions=3,
    )

    rows = [json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines()]
    assert [(row["provider"], row["task_id"], row["repetition"], row["arm"]) for row in rows] == [
        ("codex", task["id"], repetition, arm)
        for task in tasks
        for repetition in range(1, 4)
        for arm in script_run_codex.CODEX_STRUCTURAL_ARMS
    ]
    metadata = json.loads((tmp_path / "smoke-metadata.json").read_text(encoding="utf-8"))
    assert metadata["status"] == "completed"
    assert metadata["persisted_cells"] == 18
    assert metadata["execution"]["planned_cells"] == 18
    assert metadata["auth_source_recorded"] is False
    assert metadata["artifacts"]["canonical_telemetry_pooling_eligible"] is True
    assert metadata["artifacts"]["canonical_telemetry_pooling_ineligibility_reasons"] == []


def test_main_records_cell_failures_and_continues_after_smoke(
    script_run_codex: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The full run reports treatment failures without optional cell-level stopping."""
    tasks = [{"id": "first", "prompt": "one", "type": "demo"}, {"id": "second", "prompt": "two", "type": "demo"}]

    class FixtureRunner:
        """Return one non-compliant cell followed by one compliant cell."""

        timeout = 600.0

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            """Initialize fixture state."""
            pass

        def run(self, task: dict[str, Any], arm: str, **_kwargs: Any) -> Any:
            """Return a fixture result without provider calls."""
            return script_run_codex.CodexRun(
                arm=arm,
                task_id=task["id"],
                task_type="demo",
                model=script_run_codex.PARITY_CODEX_MODEL,
                success=True,
                scoreable=True,
                input_tokens=1,
                output_tokens=1,
                compliance=task["id"] == "second",
                locked_query_conformance=task["id"] == "second",
            )

    monkeypatch.setattr(script_run_codex, "load_tasks_with_provenance", lambda _path, *_args: tasks)
    monkeypatch.setattr(script_run_codex, "_read_manifest_revision", lambda *_args: "fixture-revision")
    monkeypatch.setattr(script_run_codex, "_validate_execution_manifest", lambda _path: None)
    monkeypatch.setattr(script_run_codex, "CodexRunner", FixtureRunner)
    output_path = tmp_path / "admission.jsonl"

    script_run_codex.main(
        repo_path=tmp_path,
        model=script_run_codex.PARITY_CODEX_MODEL,
        tasks_path=tmp_path / "tasks.json",
        output_path=output_path,
        arm="B_auto",
    )

    assert len(output_path.read_text(encoding="utf-8").splitlines()) == 2
    metadata = json.loads((tmp_path / "admission-metadata.json").read_text(encoding="utf-8"))
    assert metadata["schema_version"] == "codex-structural-run-metadata-v2"
    assert metadata["status"] == "completed"
    assert metadata["persisted_cells"] == 2
    assert metadata["cell_outcomes"]["compliance_failed"] == 1
    assert metadata["cell_outcomes"]["locked_query_nonconforming"] == 1
    assert "semantic_query_failed" not in metadata["cell_outcomes"]
    # B is an optional-use canary, so a no-query B cell keeps its pooling
    # eligibility. Its non-compliance is still observed and reported above as
    # `compliance_failed`; only the survivorship-inducing exclusion is gone.
    assert metadata["artifacts"]["canonical_telemetry_pooling_eligible"] is True
    assert metadata["artifacts"]["canonical_telemetry_pooling_ineligibility_reasons"] == []
    stdout = capsys.readouterr().out
    assert stdout.count("quality=    ?") == 2
    assert sum(line == LEGEND_OPEN_RULE for line in stdout.splitlines()) == 1
    assert sum(line == LEGEND_CLOSE_RULE for line in stdout.splitlines()) == 1
    assert all(not line.startswith("LEGEND  ") for line in stdout.splitlines())
    assert stdout.count("ARTIFACTS:") == 1
    assert f" - telemetry={output_path}" in stdout
    assert f" - metadata={tmp_path / 'admission-metadata.json'}" in stdout
    assert stdout.count(str(tmp_path / "admission-metadata.json")) == 1
    result_rows = [line for line in stdout.splitlines() if line.startswith("(")]
    assert len(result_rows) == 2
    assert str(output_path) not in "\n".join(result_rows)


def test_pooling_still_excludes_a_non_compliant_strict_cell(script_run_codex: Any) -> None:
    """Relaxing B must not relax C's required-use contract.

    Pins the other side of the same predicate: the strict arm keeps
    ``required_use_missing`` when it makes no successful query.
    """
    run = script_run_codex.CodexRun(
        arm="C_strict",
        task_id="first",
        task_type="demo",
        model=script_run_codex.PARITY_CODEX_MODEL,
        success=True,
        scoreable=True,
        input_tokens=1,
        output_tokens=1,
        compliance=False,
    )

    assert "required_use_missing" in script_run_codex._pooling_ineligibility_reasons(run)


def test_pooling_admits_a_non_compliant_optional_use_cell(script_run_codex: Any) -> None:
    """A zero-query B cell carries no required-use exclusion."""
    run = script_run_codex.CodexRun(
        arm="B_auto",
        task_id="first",
        task_type="demo",
        model=script_run_codex.PARITY_CODEX_MODEL,
        success=True,
        scoreable=True,
        input_tokens=1,
        output_tokens=1,
        compliance=False,
    )

    assert script_run_codex._pooling_ineligibility_reasons(run) == ()


def test_main_stops_after_three_equivalent_unknown_infrastructure_failures(
    script_run_codex: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop after three matching unknown pre-response infrastructure failures.

    Deterministic auth errors stop immediately; semantic failures continue.
    """
    tasks = [{"id": f"task-{index}", "prompt": "prompt", "type": "demo"} for index in range(1, 5)]
    calls: list[tuple[str, str]] = []

    class FixtureRunner:
        """Return the same provider failure for every coordinate."""

        timeout = 600.0

        def __init__(self, model: str, *_args: Any, **_kwargs: Any) -> None:
            """Initialize fixture state."""
            self.model = model

        def run(self, task: dict[str, Any], arm: str, **_kwargs: Any) -> Any:
            """Return a fixture result without provider calls."""
            calls.append((task["id"], arm))
            return script_run_codex.CodexRun(
                arm=arm,
                task_id=task["id"],
                task_type="demo",
                model=self.model,
                parity_arm=arm,
                success=False,
                scoreable=False,
                incomplete=True,
                error="provider temporarily unavailable",
                error_type="transport_error",
            )

    monkeypatch.setattr(script_run_codex, "load_tasks_with_provenance", lambda _path, *_args: tasks)
    monkeypatch.setattr(script_run_codex, "_read_manifest_revision", lambda *_args: "fixture-revision")
    monkeypatch.setattr(script_run_codex, "_validate_execution_manifest", lambda _path: None)
    monkeypatch.setattr(script_run_codex, "CodexRunner", FixtureRunner)
    output_path = tmp_path / "infrastructure.jsonl"

    with pytest.raises(RuntimeError, match="infrastructure failure"):
        script_run_codex.main(
            repo_path=tmp_path,
            model=script_run_codex.PARITY_CODEX_MODEL,
            tasks_path=tmp_path / "tasks.json",
            output_path=output_path,
            arm="A_plain",
        )

    rows = [json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines()]
    assert calls == [("task-1", "A_plain"), ("task-2", "A_plain"), ("task-3", "A_plain")]
    assert [(row["task_id"], row["error_type"]) for row in rows] == [
        ("task-1", "transport_error"),
        ("task-2", "transport_error"),
        ("task-3", "transport_error"),
    ]
    metadata = json.loads((tmp_path / "infrastructure-metadata.json").read_text(encoding="utf-8"))
    assert metadata["status"] == "failed"
    assert metadata["persisted_cells"] == 3
    assert metadata["error"]["type"] == "RuntimeError"


def test_main_stops_immediately_after_a_deterministic_authentication_failure(
    script_run_codex: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A known expired or consumed refresh token requires no recurrence wait.

    Prevents a deterministic credential failure from paying for two additional cells merely to satisfy the generic
    unknown-infrastructure threshold.
    """
    tasks = [{"id": f"task-{index}", "prompt": "prompt", "type": "demo"} for index in range(1, 3)]
    calls: list[tuple[str, str]] = []

    class FixtureRunner:
        """Return a recognized authentication failure for every coordinate."""

        timeout = 600.0

        def __init__(self, model: str, *_args: Any, **_kwargs: Any) -> None:
            """Initialize fixture state."""
            self.model = model

        def run(self, task: dict[str, Any], arm: str, **_kwargs: Any) -> Any:
            """Return a fixture result without provider calls."""
            calls.append((task["id"], arm))
            return script_run_codex.CodexRun(
                arm=arm,
                task_id=task["id"],
                task_type="demo",
                model=self.model,
                parity_arm=arm,
                success=False,
                scoreable=False,
                incomplete=True,
                error="HTTP 401 Unauthorized: refresh token has already been used",
                error_type="authentication_failed",
            )

    monkeypatch.setattr(script_run_codex, "load_tasks_with_provenance", lambda _path, *_args: tasks)
    monkeypatch.setattr(script_run_codex, "_read_manifest_revision", lambda *_args: "fixture-revision")
    monkeypatch.setattr(script_run_codex, "_validate_execution_manifest", lambda _path: None)
    monkeypatch.setattr(script_run_codex, "CodexRunner", FixtureRunner)
    output_path = tmp_path / "authentication.jsonl"

    with pytest.raises(RuntimeError, match="authentication failed"):
        script_run_codex.main(
            repo_path=tmp_path,
            model=script_run_codex.PARITY_CODEX_MODEL,
            tasks_path=tmp_path / "tasks.json",
            output_path=output_path,
            arm="A_plain",
        )

    rows = [json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines()]
    assert calls == [("task-1", "A_plain")]
    assert [(row["task_id"], row["error_type"]) for row in rows] == [("task-1", "authentication_failed")]
    metadata = json.loads((tmp_path / "authentication-metadata.json").read_text(encoding="utf-8"))
    assert metadata["status"] == "failed"
    assert metadata["persisted_cells"] == 1


def test_main_continues_after_semantic_or_model_quality_failures(
    script_run_codex: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Continue the study after low-quality or semantically wrong answers.

    These are scored evidence, not infrastructure failures for the recurrence guard.
    """
    tasks = [{"id": f"task-{index}", "prompt": "prompt", "type": "demo"} for index in range(1, 5)]
    calls: list[tuple[str, str]] = []

    class FixtureRunner:
        """Return independent answer-quality failures with provider usage."""

        timeout = 600.0

        def __init__(self, model: str, *_args: Any, **_kwargs: Any) -> None:
            """Initialize fixture state."""
            self.model = model

        def run(self, task: dict[str, Any], arm: str, **_kwargs: Any) -> Any:
            """Return a fixture result without provider calls."""
            calls.append((task["id"], arm))
            return script_run_codex.CodexRun(
                arm=arm,
                task_id=task["id"],
                task_type="demo",
                model=self.model,
                parity_arm=arm,
                success=False,
                scoreable=True,
                input_tokens=21,
                output_tokens=3,
                quality_score=0.0,
                error="answer did not satisfy the task oracle",
                error_type="semantic_quality_failure",
            )

    monkeypatch.setattr(script_run_codex, "load_tasks_with_provenance", lambda _path, *_args: tasks)
    monkeypatch.setattr(script_run_codex, "_read_manifest_revision", lambda *_args: "fixture-revision")
    monkeypatch.setattr(script_run_codex, "_validate_execution_manifest", lambda _path: None)
    monkeypatch.setattr(script_run_codex, "CodexRunner", FixtureRunner)
    output_path = tmp_path / "semantic.jsonl"

    script_run_codex.main(
        repo_path=tmp_path,
        model=script_run_codex.PARITY_CODEX_MODEL,
        tasks_path=tmp_path / "tasks.json",
        output_path=output_path,
        arm="A_plain",
    )

    assert calls == [(f"task-{index}", "A_plain") for index in range(1, 5)]
    metadata = json.loads((tmp_path / "semantic-metadata.json").read_text(encoding="utf-8"))
    assert metadata["status"] == "completed"
    assert metadata["persisted_cells"] == 4


@pytest.mark.parametrize("raise_from_run", [False, True])
def test_main_closes_runner_auth_state_on_all_study_exits(
    script_run_codex: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raise_from_run: bool,
) -> None:
    """Close private auth state on normal completion and exceptions.

    Runner-level auth tests separately verify concrete directory permissions and deletion.
    """
    task = {"id": "fixture", "prompt": "prompt", "type": "demo"}
    closed = 0

    class FixtureRunner:
        """Expose a close seam without creating an authenticated process."""

        timeout = 600.0

        def __init__(self, model: str, *_args: Any, **_kwargs: Any) -> None:
            """Initialize fixture state."""
            self.model = model

        def run(self, selected_task: dict[str, Any], arm: str, **_kwargs: Any) -> Any:
            """Return a fixture result without provider calls."""
            if raise_from_run:
                raise RuntimeError("fixture run failure")
            return script_run_codex.CodexRun(
                arm=arm,
                task_id=selected_task["id"],
                task_type="demo",
                model=self.model,
                parity_arm=arm,
                success=True,
                scoreable=True,
                input_tokens=1,
                output_tokens=1,
            )

        def close(self) -> None:
            """Clean up the fixture adapter."""
            nonlocal closed
            closed += 1

    monkeypatch.setattr(script_run_codex, "load_tasks_with_provenance", lambda _path, *_args: [task])
    monkeypatch.setattr(script_run_codex, "_read_manifest_revision", lambda *_args: "fixture-revision")
    monkeypatch.setattr(script_run_codex, "_validate_execution_manifest", lambda _path: None)
    monkeypatch.setattr(script_run_codex, "CodexRunner", FixtureRunner)
    output_path = tmp_path / f"runner-close-{raise_from_run}.jsonl"

    if raise_from_run:
        with pytest.raises(RuntimeError, match="fixture run failure"):
            script_run_codex.main(
                repo_path=tmp_path,
                model=script_run_codex.PARITY_CODEX_MODEL,
                tasks_path=tmp_path / "tasks.json",
                output_path=output_path,
                arm="A_plain",
            )
    else:
        script_run_codex.main(
            repo_path=tmp_path,
            model=script_run_codex.PARITY_CODEX_MODEL,
            tasks_path=tmp_path / "tasks.json",
            output_path=output_path,
            arm="A_plain",
        )

    assert closed == 1


@pytest.mark.parametrize("failure_site", ["snapshot", "metadata", "metadata-write"])
def test_main_closes_runner_when_setup_raises_before_the_first_cell(
    script_run_codex: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_site: str,
) -> None:
    """Close credentials after interrupted snapshot generation, metadata construction, or first persistence.

    Snapshot-only cleanup would leak at later setup boundaries.
    """
    task = {"id": "fixture", "prompt": "prompt", "type": "demo"}
    closed = 0

    class FixtureRunner:
        """Expose the run-lifecycle seams without preparing a real credential."""

        timeout = 600.0

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            """Initialize fixture state."""
            pass

        def create_input_snapshot(self, *_args: Any, **_kwargs: Any) -> dict[str, str]:
            """Supply snapshot evidence or interrupt at the configured snapshot boundary."""
            if failure_site == "snapshot":
                raise KeyboardInterrupt("snapshot interrupted")
            return {"path": "fixture", "sha256": "fixture"}

        def close(self) -> None:
            """Clean up the fixture adapter."""
            nonlocal closed
            closed += 1

    def _initial_metadata(**_kwargs: Any) -> dict[str, int]:
        """Supply initial progress metadata or interrupt at the configured failure site."""
        if failure_site == "metadata":
            raise KeyboardInterrupt("metadata interrupted")
        return {"persisted_cells": 0}

    def _write_metadata(*_args: Any, **_kwargs: Any) -> None:
        """Interrupt only when metadata writing is the configured failure site."""
        if failure_site == "metadata-write":
            raise KeyboardInterrupt("metadata write interrupted")

    monkeypatch.setattr(script_run_codex, "load_tasks_with_provenance", lambda _path, *_args: [task])
    monkeypatch.setattr(script_run_codex, "_read_manifest_revision", lambda *_args: "fixture-revision")
    monkeypatch.setattr(script_run_codex, "_validate_execution_manifest", lambda _path: None)
    monkeypatch.setattr(script_run_codex, "CodexRunner", FixtureRunner)
    monkeypatch.setattr(script_run_codex, "_initial_run_metadata", _initial_metadata)
    monkeypatch.setattr(script_run_codex, "_write_run_metadata", _write_metadata)

    with pytest.raises(KeyboardInterrupt, match="interrupted"):
        script_run_codex.main(
            repo_path=tmp_path,
            model=script_run_codex.PARITY_CODEX_MODEL,
            tasks_path=tmp_path / "tasks.json",
            output_path=tmp_path / f"{failure_site}.jsonl",
            arm="A_plain",
        )

    assert closed == 1


def test_main_emits_plans_only_for_dry_runs_and_paths_only_in_artifact_announcement(
    script_run_codex: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Dry-run exposes its plan while a paid run exposes only results and one path announcement."""
    task = {"id": "fixture", "prompt": "prompt", "type": "demo"}

    class FixtureRunner:
        """Provide deterministic probe evidence and one successful paid cell."""

        timeout = 600.0

        def __init__(self, model: str, *_args: Any, **_kwargs: Any) -> None:
            """Initialize fixture state."""
            self.model = model

        def probe_arm(self, _arm: str) -> dict[str, bool]:
            """Report the arm's fixture availability."""
            return {"codemap_available": False}

        def run(self, task: dict[str, Any], arm: str, **_kwargs: Any) -> Any:
            """Return a fixture result without provider calls."""
            return script_run_codex.CodexRun(
                arm=arm,
                task_id=task["id"],
                task_type=task["type"],
                model=self.model,
                success=True,
                scoreable=True,
                input_tokens=1,
                output_tokens=1,
                compliance=None,
            )

    monkeypatch.setattr(script_run_codex, "load_tasks_with_provenance", lambda _path, *_args: [task])
    monkeypatch.setattr(script_run_codex, "_read_manifest_revision", lambda *_args: "fixture-revision")
    monkeypatch.setattr(script_run_codex, "_validate_execution_manifest", lambda _path: None)
    monkeypatch.setattr(script_run_codex, "CodexRunner", FixtureRunner)

    script_run_codex.main(
        repo_path=tmp_path,
        model=script_run_codex.PARITY_CODEX_MODEL,
        tasks_path=tmp_path / "tasks.json",
        arm="A_plain",
        dry_run=True,
    )
    dry_stdout = capsys.readouterr().out
    assert [line for line in dry_stdout.splitlines() if line.startswith("PLAN")] == ["PLAN    fixture  rep=1  A_plain"]

    output_path = tmp_path / "paid.jsonl"
    metadata_path = tmp_path / "paid-metadata.json"
    script_run_codex.main(
        repo_path=tmp_path,
        model=script_run_codex.PARITY_CODEX_MODEL,
        tasks_path=tmp_path / "tasks.json",
        output_path=output_path,
        arm="A_plain",
    )
    paid_stdout = capsys.readouterr().out
    assert not any(line.startswith("PLAN") for line in paid_stdout.splitlines())
    assert paid_stdout.count(str(output_path)) == 1
    assert paid_stdout.count(str(metadata_path)) == 1
    assert all(
        str(path) not in line
        for path in (output_path, metadata_path)
        for line in paid_stdout.splitlines()
        if line.startswith(("(", "SUMMARY"))
    )


def test_main_dry_run_routes_plan_through_shared_plan_renderer(
    script_run_codex: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dry-run probe and plan rows reach the shared arm renderer instead of a direct print.

    An operator reading a 219-cell plan scrolls past hundreds of near-identical rows, so each coordinate carries its arm
    color on a terminal. Routing through the shared renderer is what keeps that color out of redirected logs, so a
    direct ``print`` path would silently reintroduce the ANSI contamination the redirected-output contract forbids.
    """
    task = {"id": "fixture", "prompt": "prompt", "type": "demo"}
    printed: list[str] = []

    class FixtureRunner:
        """Provide deterministic no-model probe evidence."""

        timeout = 600.0

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            """Initialize fixture state."""
            pass

        def probe_arm(self, _arm: str) -> dict[str, bool]:
            """Report the arm's fixture availability."""
            return {"codemap_available": False}

    monkeypatch.setattr(script_run_codex, "load_tasks_with_provenance", lambda _path, *_args: [task])
    monkeypatch.setattr(script_run_codex, "_read_manifest_revision", lambda *_args: "fixture-revision")
    monkeypatch.setattr(script_run_codex, "CodexRunner", FixtureRunner)
    plain: list[str] = []
    monkeypatch.setattr(script_run_codex, "print", plain.append, raising=False)
    monkeypatch.setattr(codex_runtime, "print_plan_row", printed.append)

    script_run_codex.main(
        repo_path=tmp_path,
        model=script_run_codex.PARITY_CODEX_MODEL,
        tasks_path=tmp_path / "tasks.json",
        arm="A_plain",
        dry_run=True,
        show_legend=False,
    )

    assert "PLAN    fixture  rep=1  A_plain" in printed
    assert "PROBE   A_plain   codemap=false  use=forbidden  codemap_python=absent" in printed
    assert not any(row.startswith(("PLAN", "PROBE")) for row in plain)


@pytest.mark.parametrize(
    ("arm", "expected_prefixes"),
    [
        pytest.param("all", ["(1/3)", "(2/3)", "(3/3)"], id="task-subset-all-arms"),
        pytest.param("B_auto", ["(1/1)"], id="task-subset-single-arm"),
    ],
)
def test_main_progress_denominator_matches_selected_cells(
    script_run_codex: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arm: str,
    expected_prefixes: list[str],
) -> None:
    """Subset and single-arm runs use their exact selected-cell totals."""
    tasks = [{"id": "first", "prompt": "one", "type": "demo"}, {"id": "second", "prompt": "two", "type": "demo"}]

    class FixtureRunner:
        """Return a minimal completed result for each selected cell."""

        timeout = 600.0

        def __init__(self, model: str, *_args: Any, **_kwargs: Any) -> None:
            """Initialize fixture state."""
            self.model = model

        def run(self, task: dict[str, Any], selected_arm: str, **_kwargs: Any) -> Any:
            """Return a fixture result without provider calls."""
            return script_run_codex.CodexRun(
                arm=selected_arm,
                task_id=task["id"],
                task_type=task["type"],
                model=self.model,
                success=True,
                scoreable=True,
                input_tokens=1,
                output_tokens=1,
                compliance=selected_arm != "A_plain",
            )

    monkeypatch.setattr(script_run_codex, "load_tasks_with_provenance", lambda _path, *_args: tasks)
    monkeypatch.setattr(script_run_codex, "_read_manifest_revision", lambda *_args: "fixture-revision")
    monkeypatch.setattr(script_run_codex, "_validate_execution_manifest", lambda _path: None)
    monkeypatch.setattr(script_run_codex, "_validate_unscoped_paid_task_ids", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        script_run_codex,
        "deterministic_arm_order",
        lambda *_args, **_kwargs: ("C_strict", "B_auto", "A_plain"),
    )
    monkeypatch.setattr(script_run_codex, "CodexRunner", FixtureRunner)

    script_run_codex.main(
        repo_path=tmp_path,
        model=script_run_codex.PARITY_CODEX_MODEL,
        tasks_path=tmp_path / "tasks.json",
        output_path=tmp_path / "selected.jsonl",
        task_ids=["second"],
        arm=arm,
    )

    result_rows = [line for line in capsys.readouterr().out.splitlines() if line.startswith("(")]
    assert [row.split()[0] for row in result_rows] == expected_prefixes
    assert all("RESULT" not in row for row in result_rows)
    if arm == "all":
        assert [row.split()[4] for row in result_rows] == ["A_plain", "B_auto", "C_strict"]


def test_main_prints_interrupted_partial_block_with_planned_denominator(
    script_run_codex: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failure before one arm preserves display-order progress for persisted peers."""
    task = {"id": "fixture", "prompt": "prompt", "type": "demo"}

    class FixtureRunner:
        """Persist C/B, then interrupt before A can produce a result."""

        timeout = 600.0

        def __init__(self, model: str, *_args: Any, **_kwargs: Any) -> None:
            """Initialize fixture state."""
            self.model = model

        def run(self, selected_task: dict[str, Any], selected_arm: str, **_kwargs: Any) -> Any:
            """Return a fixture result without provider calls."""
            if selected_arm == "A_plain":
                raise RuntimeError("fixture interruption")
            return script_run_codex.CodexRun(
                arm=selected_arm,
                task_id=selected_task["id"],
                task_type=selected_task["type"],
                model=self.model,
                success=True,
                scoreable=True,
                input_tokens=1,
                output_tokens=1,
                compliance=True,
            )

    monkeypatch.setattr(script_run_codex, "load_tasks_with_provenance", lambda _path, *_args: [task])
    monkeypatch.setattr(script_run_codex, "_read_manifest_revision", lambda *_args: "fixture-revision")
    monkeypatch.setattr(script_run_codex, "_validate_execution_manifest", lambda _path: None)
    monkeypatch.setattr(
        script_run_codex,
        "deterministic_arm_order",
        lambda *_args, **_kwargs: ("C_strict", "B_auto", "A_plain"),
    )
    monkeypatch.setattr(script_run_codex, "CodexRunner", FixtureRunner)

    with pytest.raises(RuntimeError, match="fixture interruption"):
        script_run_codex.main(
            repo_path=tmp_path,
            model=script_run_codex.PARITY_CODEX_MODEL,
            tasks_path=tmp_path / "tasks.json",
            output_path=tmp_path / "interrupted.jsonl",
        )

    result_rows = [line for line in capsys.readouterr().out.splitlines() if line.startswith("(")]
    assert [row.split()[0] for row in result_rows] == ["(1/3)", "(2/3)"]
    assert [row.split()[4] for row in result_rows] == ["B_auto", "C_strict"]


def test_output_legend_defines_treatments_tasks_and_measurement_marks(script_run_codex: Any) -> None:
    """The upfront legend makes every compact terminal field independently interpretable."""
    legend = script_run_codex.runtime.STRUCTURAL_OUTPUT_LEGEND
    lines = legend.splitlines()

    assert legend.startswith(f"{LEGEND_OPEN_RULE}\n")
    assert legend.endswith(LEGEND_CLOSE_RULE)
    assert all(not line.startswith("LEGEND  ") for line in lines)
    for field in ("treatments", "status", "quality", "progress", "treatment", "codemap-used", "input tokens"):
        assert sum(line.startswith(f"  {field}:") for line in lines) == 1
    assert sum(line == "  tasks:" for line in lines) == 1
    assert (
        "treatments: A_plain=no Codemap, B_auto=direct Codemap available and optional, "
        "C_strict=Codemap Skill required" in legend
    )
    assert "B_direct" not in legend
    assert "C_skill" not in legend
    assert "status: ✓ completed, ✗ failed" in legend
    assert "quality: continuous [0,1], ? unscoreable (higher is better)" in legend
    assert "progress: N completed cells / M planned cells" in legend
    assert "treatment: ✓ assigned arm followed, ✗ assigned arm not followed" in legend
    assert "codemap-used: ✓ Codemap call observed; ✗ no Codemap call (expected for A_plain)" in legend
    assert "or required use missed (B/C)" in legend
    assert "query: ✓ exact expected query; ✗ mismatch; — not applicable" in legend
    assert "cohort: H headline; D diagnostic" in legend
    assert (
        "input tokens: gross total; cached and fresh details remain in telemetry only (lower is better at equal quality)"
        in legend
    )
    assert " | " not in legend
    assert "tokens: k=1,000, M=1,000,000" not in legend


@pytest.mark.parametrize(
    ("canonical_arm", "display_arm"),
    [
        pytest.param("A_plain", "A_plain", id="plain"),
        pytest.param("B_auto", "B_auto", id="direct"),
        pytest.param("C_strict", "C_strict", id="skill"),
    ],
)
def test_human_arm_labels_shorten_only_presentation_names(
    script_run_codex: Any,
    canonical_arm: str,
    display_arm: str,
) -> None:
    """PLAN/progress rows use short labels while machine arm IDs remain canonical."""
    plan = codex_runtime.format_plan_row("FN-02", 1, canonical_arm)
    result = codex_runtime.format_structural_result_row(
        status="✓",
        task_id="FN-02",
        repetition=1,
        arm=canonical_arm,
        input_tokens=100,
        cached_input_tokens=20,
        fresh_tokens=80,
        output_tokens=1,
        elapsed_s=1.0,
        quality="1.000",
        adherence=True,
        codemap_used=canonical_arm != "A_plain",
    )

    assert display_arm in plan
    assert display_arm in result
    if canonical_arm != display_arm:
        assert canonical_arm not in plan
        assert canonical_arm not in result


@pytest.mark.parametrize(
    ("code", "meaning"),
    [
        pytest.param("SE", "symbol extraction", id="symbol-extraction"),
        pytest.param("FN", "function-call graph", id="function-call-graph"),
        pytest.param("RV", "review assistance", id="review-assistance"),
        pytest.param("CQ", "code quality", id="code-quality"),
        pytest.param("BR", "blast radius", id="blast-radius"),
        pytest.param("DG", "debug from trace", id="debug-from-trace"),
        pytest.param("FT", "feature scaffolding", id="feature-scaffolding"),
        pytest.param("RI", "real issue", id="real-issue"),
        pytest.param("DI", "diff impact", id="diff-impact"),
        pytest.param("GR", "graph reasoning", id="graph-reasoning"),
        pytest.param("MB", "module blast radius", id="module-blast-radius"),
    ],
)
def test_output_legend_defines_each_task_code(script_run_codex: Any, code: str, meaning: str) -> None:
    """Every task code in compact output expands to its benchmark meaning."""
    assert f"      {code}: {meaning}" in script_run_codex.runtime.STRUCTURAL_OUTPUT_LEGEND.splitlines()


#: Arm names are printed unabbreviated, so the expected padding tracks the widest canonical name.
_ARM_COLUMN_WIDTH = codex_runtime._DISPLAY_ARM_COLUMN_WIDTH


@pytest.mark.parametrize(
    (
        "arm",
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "elapsed_s",
        "quality",
        "adherence",
        "codemap_used",
        "expected",
    ),
    [
        pytest.param(
            "A_plain",
            44_441,
            2_000,
            658,
            17.2,
            "1.000",
            True,
            False,
            f"✓  SE-01  rep=1  {'A_plain':<{_ARM_COLUMN_WIDTH}}  in= 44.4k  out=  658  time=  17s  "
            "quality=1.000  treatment:✓  codemap-used:✗",
            id="plain-small-output",
        ),
        pytest.param(
            "C_strict",
            74_530,
            10_000,
            995,
            24.0,
            "1.000",
            True,
            True,
            f"✓  SE-01  rep=1  {'C_strict':<{_ARM_COLUMN_WIDTH}}  in= 74.5k  out=  995  time=  24s  "
            "quality=1.000  treatment:✓  codemap-used:✓",
            id="skill-required",
        ),
        pytest.param(
            "B_auto",
            1_230_920,
            230_920,
            1_475,
            97.6,
            "?",
            False,
            True,
            f"✗  SE-01  rep=1  {'B_auto':<{_ARM_COLUMN_WIDTH}}  in=  1.2M  out= 1.5k  time=1m38s  "
            "quality=    ?  treatment:✗  codemap-used:✓",
            id="direct-million-and-failure",
        ),
    ],
)
def test_format_result_row_uses_shared_human_units_and_fixed_columns(
    script_run_codex: Any,
    arm: str,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    elapsed_s: float,
    quality: str,
    adherence: bool,
    codemap_used: bool,
    expected: str,
) -> None:
    """Terminal rows remain compact and visually comparable across all Codex arms."""
    status = "✓" if adherence else "✗"

    assert (
        codex_runtime.format_structural_result_row(
            status=status,
            task_id="SE-01",
            repetition=1,
            arm=arm,
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            fresh_tokens=input_tokens - cached_input_tokens,
            output_tokens=output_tokens,
            elapsed_s=elapsed_s,
            quality=quality,
            adherence=adherence,
            codemap_used=codemap_used,
        )
        == expected
    )


def test_format_result_row_hides_inconsistent_cache_detail(script_run_codex: Any) -> None:
    """The terminal display must remain gross-only even for inconsistent cache telemetry."""
    row = codex_runtime.format_structural_result_row(
        status="✓",
        task_id="SE-01",
        repetition=1,
        arm="A_plain",
        input_tokens=25,
        cached_input_tokens=80,
        fresh_tokens=None,
        output_tokens=1,
        elapsed_s=1.0,
        quality="1.000",
        adherence=True,
        codemap_used=False,
    )

    assert "in=    25  out=" in row
    assert "/" not in row


def test_format_result_row_separates_treatment_from_observed_codemap_use(
    script_run_codex: Any,
) -> None:
    """A contaminated plain arm reports use even though its treatment failed."""
    row = codex_runtime.format_structural_result_row(
        status="✗",
        task_id="FN-02",
        repetition=1,
        arm="A_plain",
        input_tokens=86_600,
        cached_input_tokens=77_300,
        fresh_tokens=9_300,
        output_tokens=1,
        elapsed_s=1.0,
        quality="1.000",
        adherence=False,
        codemap_used=True,
    )

    assert "treatment:✗  codemap-used:✓" in row
    assert "in= 86.6k  out=" in row
    assert "/" not in row


@pytest.mark.parametrize(
    ("is_terminal", "expected_rich_calls", "expected_plain_calls"),
    [pytest.param(True, 1, 0, id="interactive-rich-color"), pytest.param(False, 0, 1, id="redirected-plain-text")],
)
def test_print_arm_row_colors_only_interactive_output(
    monkeypatch: pytest.MonkeyPatch,
    is_terminal: bool,
    expected_rich_calls: int,
    expected_plain_calls: int,
) -> None:
    """Color aids terminal navigation without contaminating redirected benchmark logs."""
    rich_calls: list[tuple[str, dict[str, Any]]] = []
    plain_calls: list[str] = []

    class FixtureConsole:
        """Record Rich calls behind a configurable terminal boundary."""

        def __init__(self) -> None:
            """Initialize fixture state."""
            self.is_terminal = is_terminal

        def print(self, row: str, **kwargs: Any) -> None:
            """Handle console output through the test double instead of a live terminal."""
            rich_calls.append((row, kwargs))

    monkeypatch.setattr(codex_runtime, "_CONSOLE", FixtureConsole())
    monkeypatch.setattr(codex_runtime.presentation, "print", plain_calls.append, raising=False)

    codex_runtime.print_arm_row("progress fixture", "B_auto")

    assert len(rich_calls) == expected_rich_calls
    assert len(plain_calls) == expected_plain_calls
    if rich_calls:
        assert rich_calls == [
            (
                "progress fixture",
                {"style": "cyan", "markup": False, "highlight": False, "soft_wrap": True},
            )
        ]
    if plain_calls:
        assert plain_calls == ["progress fixture"]


@pytest.mark.parametrize(
    ("row", "expected_style"),
    [
        pytest.param("PLAN    FN-02  rep=1  A_plain", "yellow", id="plan-a-plain"),
        pytest.param("PLAN    FN-02  rep=1  B_auto", "cyan", id="plan-b-direct"),
        pytest.param("PLAN    RC-01  rep=1  C_strict", "magenta", id="plan-c-strict"),
        pytest.param("PROBE   B_auto       codemap=true", "cyan", id="probe-b-direct-required"),
    ],
)
def test_print_plan_row_colors_each_arm_distinctly(
    monkeypatch: pytest.MonkeyPatch,
    row: str,
    expected_style: str,
) -> None:
    """Every no-model plan and probe row carries its own arm color on a terminal.

    The three arms are visually indistinguishable in a long plan listing without this, which is where an operator checks
    that the coordinate set they are about to pay for is the one they intended. A row naming no arm must still print
    unchanged.
    """
    rich_calls: list[tuple[str, dict[str, Any]]] = []

    class FixtureConsole:
        """Record Rich calls behind an interactive terminal boundary."""

        is_terminal = True

        def print(self, printed_row: str, **kwargs: Any) -> None:
            """Handle console output through the test double instead of a live terminal."""
            rich_calls.append((printed_row, kwargs))

    monkeypatch.setattr(codex_runtime, "_CONSOLE", FixtureConsole())

    codex_runtime.print_plan_row(row)

    assert rich_calls == [(row, {"style": expected_style, "markup": False, "highlight": False, "soft_wrap": True})]


def test_print_plan_row_leaves_armless_banner_uncolored(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stage preflight banner names no arm and must not acquire one arm's color.

    Stage headers such as the ReadCrop banner share the plan stream with coordinate rows; coloring them would attribute
    the whole section to whichever arm the regex happened to match.
    """
    plain_calls: list[str] = []

    class FixtureConsole:
        """Fail the test if an armless row reaches Rich."""

        is_terminal = True

        def print(self, *_args: Any, **_kwargs: Any) -> None:
            """Reject console output for a row that names no arm."""
            pytest.fail("an armless banner reached the arm-color path")

    monkeypatch.setattr(codex_runtime, "_CONSOLE", FixtureConsole())
    monkeypatch.setattr(codex_runtime.presentation, "print", plain_calls.append, raising=False)

    codex_runtime.print_plan_row("READCROP PREFLIGHT (no model)")

    assert plain_calls == ["READCROP PREFLIGHT (no model)"]


def test_print_result_block_numbers_rows_in_fixed_display_order(
    script_run_codex: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Progress follows A/B/C display order instead of completion order."""
    printed: list[tuple[str, str]] = []
    monkeypatch.setattr(codex_runtime, "print_arm_row", lambda row, arm: printed.append((row, arm)))

    next_progress = script_run_codex._print_result_block(
        [
            ("C_strict", "✓  task  rep=1  C_strict"),
            ("A_plain", "✓  task  rep=1  A_plain"),
            ("B_auto", "✓  task  rep=1  B_auto"),
        ],
        printed_cells=4,
        planned_cells=9,
    )

    assert printed == [
        ("(5/9) ✓  task  rep=1  A_plain", "A_plain"),
        ("(6/9) ✓  task  rep=1  B_auto", "B_auto"),
        ("(7/9) ✓  task  rep=1  C_strict", "C_strict"),
    ]
    assert next_progress == 7
    assert all("RESULT" not in row for row, _arm in printed)


def test_print_result_block_keeps_partial_progress_relative_to_full_plan(
    script_run_codex: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interrupted block retains the selected plan's denominator."""
    printed: list[tuple[str, str]] = []
    monkeypatch.setattr(codex_runtime, "print_arm_row", lambda row, arm: printed.append((row, arm)))

    next_progress = script_run_codex._print_result_block(
        [("C_strict", "✓  task  rep=1  C_strict"), ("B_auto", "✓  task  rep=1  B_auto")],
        printed_cells=0,
        planned_cells=3,
    )

    assert printed == [("(1/3) ✓  task  rep=1  B_auto", "B_auto"), ("(2/3) ✓  task  rep=1  C_strict", "C_strict")]
    assert next_progress == 2


def test_main_filters_locked_tasks_in_suite_order_and_rejects_invalid_ids(
    script_run_codex: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Smoke selection keeps canonical suite order without accepting unknown or duplicate IDs."""
    tasks = [{"id": "first", "prompt": "one", "type": "demo"}, {"id": "second", "prompt": "two", "type": "demo"}]
    monkeypatch.setattr(script_run_codex, "load_tasks_with_provenance", lambda _path, *_args: tasks)
    monkeypatch.setattr(script_run_codex, "_read_manifest_revision", lambda *_args: "fixture-revision")
    planned: list[str] = []

    class FixtureRunner:
        """Record selected dry-run tasks without model execution."""

        timeout = 600.0

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            """Initialize fixture state."""
            pass

        def probe_arm(self, _arm: str) -> dict[str, bool]:
            """Report the arm's fixture availability."""
            return {"codemap_available": False}

    monkeypatch.setattr(script_run_codex, "CodexRunner", FixtureRunner)
    monkeypatch.setattr(
        script_run_codex,
        "print",
        lambda text: planned.append(text),
        raising=False,
    )
    monkeypatch.setattr(codex_runtime, "print_plan_row", planned.append)

    script_run_codex.main(
        repo_path=tmp_path,
        model=script_run_codex.PARITY_CODEX_MODEL,
        tasks_path=tmp_path / "tasks.json",
        task_ids=["second"],
        arm="A_plain",
        dry_run=True,
    )

    assert any(row == "PLAN    second  rep=1  A_plain" for row in planned)
    assert not any("first" in row for row in planned)
    with pytest.raises(ValueError, match="unique"):
        script_run_codex.main(
            repo_path=tmp_path,
            model=script_run_codex.PARITY_CODEX_MODEL,
            tasks_path=tmp_path / "tasks.json",
            task_ids=["first", "first"],
            dry_run=True,
        )
    with pytest.raises(ValueError, match="unknown"):
        script_run_codex.main(
            repo_path=tmp_path,
            model=script_run_codex.PARITY_CODEX_MODEL,
            tasks_path=tmp_path / "tasks.json",
            task_ids=["missing"],
            dry_run=True,
        )
    with pytest.raises(ValueError, match="positive"):
        script_run_codex.main(
            repo_path=tmp_path,
            model=script_run_codex.PARITY_CODEX_MODEL,
            tasks_path=tmp_path / "tasks.json",
            repetitions=0,
            dry_run=True,
        )


def test_main_plans_every_preregistered_pilot_coordinate_once(
    script_run_codex: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lock the six-task, three-repetition pilot to exactly 54 ordered cells."""
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    pilot_ids = manifest["preregistered_cells"]["structural_pilot_task_ids"]
    repetitions = manifest["preregistered_cells"]["pilot_repetitions"]
    tasks = [{"id": task_id, "prompt": task_id, "type": "demo"} for task_id in pilot_ids]
    monkeypatch.setattr(script_run_codex, "load_tasks_with_provenance", lambda _path, *_args: tasks)
    monkeypatch.setattr(script_run_codex, "_read_manifest_revision", lambda *_args: "fixture-revision")
    planned: list[str] = []

    class FixtureRunner:
        """Provide no-model probe evidence while the plan is constructed."""

        timeout = 600.0

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            """Initialize fixture state."""
            pass

        def probe_arm(self, arm: str) -> dict[str, bool]:
            """Report the arm's fixture availability."""
            return {"codemap_available": arm != "A_plain"}

    monkeypatch.setattr(script_run_codex, "CodexRunner", FixtureRunner)
    monkeypatch.setattr(script_run_codex, "print", planned.append, raising=False)
    monkeypatch.setattr(codex_runtime, "print_plan_row", planned.append)
    monkeypatch.setattr(
        script_run_codex,
        "deterministic_arm_order",
        lambda *_args, **_kwargs: script_run_codex.CODEX_STRUCTURAL_ARMS,
    )

    script_run_codex.main(
        repo_path=tmp_path,
        model="gpt-5.6-luna",
        tasks_path=tmp_path / "tasks.json",
        task_ids=pilot_ids,
        repetitions=repetitions,
        dry_run=True,
    )

    plan_rows = [line.split()[1:] for line in planned if line.startswith("PLAN ")]
    expected = [
        [task_id, f"rep={repetition}", arm]
        for task_id in pilot_ids
        for repetition in range(1, repetitions + 1)
        for arm in script_run_codex.CODEX_STRUCTURAL_ARMS
    ]
    assert plan_rows == expected
    assert len(plan_rows) == len({tuple(row) for row in plan_rows}) == 54


def test_codex_arm_envelopes_define_plain_cli_and_skill_treatments(script_run_codex: Any) -> None:
    """The new Codex arms must make delivery mode and required use unambiguous."""
    assert script_run_codex.CODEX_STRUCTURAL_ARMS == (
        "A_plain",
        "B_auto",
        "C_strict",
    )
    assert "Codemap is absent" in script_run_codex._arm_envelope("A_plain")
    assert '"$CODEMAP_BIN" query --compact' in script_run_codex._arm_envelope("B_auto")
    assert "dedicated native command item" in script_run_codex._arm_envelope("B_auto")
    assert "Do not use batch" in script_run_codex._arm_envelope("B_auto")
    assert '"$CODEMAP_BIN" query --help' in script_run_codex._arm_envelope("B_auto")
    assert "$codemap-py:query-code" in script_run_codex._arm_envelope("C_strict")
    assert "installed $codemap-py:query-code Skill is available" in script_run_codex._arm_envelope("C_strict")
    assert 'cat "$CODEMAP_SKILL_FILE"' not in script_run_codex._arm_envelope("C_strict")
    assert "Do not use batch" in script_run_codex._arm_envelope("C_strict")
    assert '"$CODEMAP_BIN" query --help' in script_run_codex._arm_envelope("C_strict")
    assert "sed -n" not in script_run_codex._arm_envelope("C_strict")


@pytest.mark.parametrize(
    ("command", "status", "exit_code", "output", "expected_credit"),
    [
        pytest.param(
            '"$CODEMAP_BIN" query --compact fn-rdeps "pkg.core::target"',
            "completed",
            0,
            '{"index":{"query_complete":true,"compact":true}}',
            True,
            id="canonical-native-item",
        ),
        pytest.param(
            "\"$CODEMAP_BIN\" query --compact find-symbol '^is_overridden$' --exclude-tests",
            "completed",
            0,
            '{"index":{"query_complete":true,"compact":true}}',
            True,
            id="single-quoted-literal-dollar-anchor",
        ),
        pytest.param(
            '"$CODEMAP_BIN" query --compact find-symbol "$PATTERN" --exclude-tests',
            "completed",
            0,
            '{"index":{"query_complete":true,"compact":true}}',
            False,
            id="double-quoted-query-expansion",
        ),
        pytest.param(
            "$CODEMAP_BIN query --compact fn-rdeps pkg.core",
            "completed",
            0,
            '{"index":{"query_complete":true,"compact":true}}',
            True,
            id="unquoted-launcher",
        ),
        pytest.param(
            "${CODEMAP_BIN} query --compact fn-rdeps pkg.core",
            "completed",
            0,
            '{"index":{"query_complete":true,"compact":true}}',
            True,
            id="unquoted-braced-launcher",
        ),
        pytest.param(
            '"$CODEMAP_LAUNCHER" query --compact fn-rdeps pkg.core',
            "completed",
            0,
            '{"index":{"query_complete":true,"compact":true}}',
            False,
            id="alias",
        ),
        pytest.param(
            'CODEMAP_BIN=/wrong/codemap-py; "$CODEMAP_BIN" query --compact rdeps pkg.core',
            "completed",
            0,
            '{"index":{"query_complete":true,"compact":true}}',
            False,
            id="assignment",
        ),
        pytest.param(
            '"$CODEMAP_BIN" query --compact rdeps pkg.core; printf done',
            "completed",
            0,
            '{"index":{"query_complete":true,"compact":true}}',
            False,
            id="compound-command",
        ),
        pytest.param(
            '"$CODEMAP_BIN" query --compact rdeps pkg.core && printf done',
            "completed",
            0,
            '{"index":{"query_complete":true,"compact":true}}',
            False,
            id="control-operator",
        ),
        pytest.param(
            '"$CODEMAP_BIN" query --compact rdeps pkg.core > result.json',
            "completed",
            0,
            '{"index":{"query_complete":true,"compact":true}}',
            False,
            id="redirection",
        ),
        pytest.param(
            "/bin/zsh -lc '\"$CODEMAP_BIN\" query --compact rdeps pkg.core'",
            "completed",
            0,
            '{"index":{"query_complete":true,"compact":true}}',
            True,
            id="one-outer-transport-wrapper",
        ),
        pytest.param(
            '"$CODEMAP_BIN" query --compact rdeps pkg.core',
            "completed",
            "0",
            '{"index":{"query_complete":true,"compact":true}}',
            False,
            id="noninteger-exit-code",
        ),
        pytest.param(
            '"$CODEMAP_BIN" query --compact rdeps pkg.core',
            "completed",
            1,
            '{"index":{"query_complete":true,"compact":true}}',
            False,
            id="nonzero-exit-code",
        ),
        pytest.param(
            '"$CODEMAP_BIN" query --compact rdeps pkg.core',
            "completed",
            0,
            '{"index":{"query_complete":true}}',
            False,
            id="missing-compact-output",
        ),
        pytest.param(
            '"$CODEMAP_BIN" query --compact rdeps pkg.core',
            "completed",
            0,
            'diagnostic\n{"index":{"query_complete":true,"compact":true}}',
            False,
            id="json-prefix",
        ),
        pytest.param(
            '"$CODEMAP_BIN" query --compact rdeps pkg.core',
            "completed",
            0,
            '{"index":{"query_complete":true,"compact":true}}\ndiagnostic',
            False,
            id="json-suffix",
        ),
        pytest.param(
            '"$CODEMAP_BIN" query --compact rdeps pkg.core',
            "completed",
            0,
            '{"index":{"query_complete":true,"compact":true}}\n{}',
            False,
            id="multiple-json-documents",
        ),
    ],
)
def test_canonical_native_item_query_contract(
    script_run_codex: Any,
    command: str,
    status: str,
    exit_code: object,
    output: str,
    expected_credit: bool,
) -> None:
    """Credit B only for the standalone canonical native command item.

    Prevents telemetry from treating shell interpretation or loosely embedded JSON as equivalent to the future
    benchmark's explicit native-item proof.
    """
    parsed = codex_runtime.parse_codex_jsonl(
        _completed_stream(
            commands=[
                {
                    "type": "command_execution",
                    "command": command,
                    "status": status,
                    "exit_code": exit_code,
                    "aggregated_output": output,
                }
            ]
        )
    )

    assert parsed.codemap_direct_compact_successful_calls == int(expected_credit)
    assert script_run_codex._arm_compliance("B_auto", parsed) is expected_credit


def test_canonical_native_query_allows_auxiliary_separate_events(script_run_codex: Any) -> None:
    """Separate native command items cannot contaminate a canonical query item."""
    parsed = codex_runtime.parse_codex_jsonl(
        _completed_stream(
            commands=[
                {
                    "type": "command_execution",
                    "command": "pwd",
                    "status": "completed",
                    "exit_code": 0,
                    "aggregated_output": "/fixture\n",
                },
                {
                    "type": "command_execution",
                    "command": '"$CODEMAP_BIN" query --compact fn-rdeps "pkg.core::target"',
                    "status": "completed",
                    "exit_code": 0,
                    "aggregated_output": '{"index":{"query_complete":true,"compact":true}}',
                },
                {
                    "type": "command_execution",
                    "command": "printf complete",
                    "status": "completed",
                    "exit_code": 0,
                    "aggregated_output": "complete",
                },
            ]
        )
    )

    assert parsed.codemap_direct_compact_successful_calls == 1
    assert script_run_codex._arm_compliance("B_auto", parsed) is True


@pytest.mark.parametrize(
    ("expected_queries", "expected_query_policy", "actual", "expected"),
    [
        pytest.param(
            [{"cmd": "central", "args": ["--top", "5"]}],
            None,
            [["central", "--top", "5"]],
            True,
            id="matching-central",
        ),
        pytest.param(
            [{"cmd": "fn-rdeps", "args": ["qname", "--exclude-tests"]}],
            None,
            [["fn-rdeps", "--exclude-tests", "qname"]],
            True,
            id="equivalent-option-order",
        ),
        pytest.param(
            [{"cmd": "central", "args": ["--top", "5", "--exclude-tests"]}],
            None,
            [["central", "--exclude-tests", "--top", "5"]],
            True,
            id="equivalent-boolean-and-value-order",
        ),
        pytest.param([{"cmd": "central", "args": ["--top", "5"]}], None, [["coupled"]], False, id="wrong-endpoint"),
        pytest.param(
            [{"cmd": "central", "args": ["--top", "5"]}],
            None,
            [["central", "--top", "3"]],
            False,
            id="wrong-target",
        ),
        pytest.param(
            [{"cmd": "central", "args": ["--top", "5"]}],
            None,
            [["central", "--unknown", "5"]],
            False,
            id="unknown-option",
        ),
        pytest.param(
            [{"cmd": "central", "args": ["--top", "5"]}],
            None,
            [["central", "--top"]],
            False,
            id="missing-value",
        ),
        pytest.param(
            [{"cmd": "central", "args": ["--exclude-tests"]}],
            None,
            [["central", "--exclude-tests", "--exclude-tests"]],
            False,
            id="duplicate-boolean",
        ),
        pytest.param(
            [{"cmd": "central", "args": ["--top", "5"]}],
            None,
            [["central", "--top", "5", "extra"]],
            False,
            id="extra-positional",
        ),
        pytest.param(
            [{"cmd": "coupled", "args": []}, {"cmd": "central", "args": ["--top", "5"]}],
            None,
            [["coupled"], ["central", "--top", "5"]],
            True,
            id="extra-query-with-match",
        ),
        pytest.param(
            [
                {"cmd": "fn-rdeps", "args": ["pkg.core::target", "--exclude-tests"]},
                {"cmd": "rdeps", "args": ["pkg.core"]},
            ],
            "all_required",
            [["fn-rdeps", "pkg.core::target", "--exclude-tests"]],
            False,
            id="all-required-rejects-one-of-two-queries",
        ),
        pytest.param(
            [
                {"cmd": "fn-rdeps", "args": ["pkg.core::target", "--exclude-tests"]},
                {"cmd": "rdeps", "args": ["pkg.core"]},
            ],
            "all_required",
            [["fn-rdeps", "--exclude-tests", "pkg.core::target"], ["rdeps", "pkg.core"]],
            True,
            id="all-required-accepts-every-query",
        ),
    ],
)
def test_locked_query_conformance_requires_expected_endpoint_and_target(
    script_run_codex: Any,
    expected_queries: list[dict[str, Any]],
    expected_query_policy: str | None,
    actual: list[list[str]],
    expected: bool,
) -> None:
    """Generic Codemap use cannot satisfy the exact locked query contract."""
    task = {"id": "GR-01", "expected_queries": expected_queries}
    if expected_query_policy is not None:
        task["expected_query_policy"] = expected_query_policy
    run = script_run_codex.CodexRun(
        arm="B_auto",
        task_id="GR-01",
        task_type="graph_reasoning",
        model=script_run_codex.PARITY_CODEX_MODEL,
        successful_query_arguments=actual,
    )
    assert script_run_codex._locked_query_conformance(task, run.arm, run) is expected


@pytest.mark.parametrize(
    ("expected_query", "actual", "expected"),
    [
        pytest.param(
            {"cmd": "fn-rdeps", "args": ["qname", "--exclude-tests"]},
            [["fn-rdeps", "--exclude-tests", "qname"]],
            (1.0, 1.0, 1.0, 1.0),
            id="exact",
        ),
        pytest.param(
            {"cmd": "fn-rdeps", "args": ["qname", "--exclude-tests"]},
            [["fn-blast", "qname", "--exclude-tests"]],
            (0.5, 0.0, 1.0, 1.0),
            id="alternate-endpoint",
        ),
        pytest.param(
            {"cmd": "symbol", "args": ["Timer.start"]},
            [["symbol", "Timer.stop"]],
            (1 / 3, 1.0, 0.0, 1.0),
            id="target-mismatch",
        ),
        pytest.param(
            {"cmd": "fn-rdeps", "args": ["qname", "--exclude-tests"]},
            [["fn-rdeps", "qname"]],
            (2 / 3, 1.0, 1.0, 0.0),
            id="option-filter-mismatch",
        ),
        pytest.param(
            {"cmd": "symbol", "args": ["Timer.start", "--limit", "0"]},
            [["symbol", "--limit", "0", "Timer.start"]],
            (1.0, 1.0, 1.0, 1.0),
            id="limit-value-option",
        ),
    ],
)
def test_locked_query_fitness_reports_mismatch_components(
    script_run_codex: Any,
    expected_query: dict[str, Any],
    actual: list[list[str]],
    expected: tuple[float, float, float, float],
) -> None:
    """Query fitness identifies endpoint, target, and option/filter mismatches."""
    task = {"id": "GR-01", "expected_queries": [expected_query]}
    run = script_run_codex.CodexRun(
        arm="B_auto",
        task_id="GR-01",
        task_type="graph_reasoning",
        model=script_run_codex.PARITY_CODEX_MODEL,
        successful_query_arguments=actual,
    )

    fitness = script_run_codex._locked_query_fitness(task, run.arm, run)

    assert fitness is not None
    assert (fitness.overall, fitness.endpoint, fitness.target, fitness.options) == pytest.approx(expected)


def test_locked_query_fitness_averages_all_required_queries(script_run_codex: Any) -> None:
    """Missing one required query reduces every component without cross-query mixing."""
    task = {
        "id": "DI-01",
        "expected_query_policy": "all_required",
        "expected_queries": [
            {"cmd": "fn-rdeps", "args": ["pkg.core::target", "--exclude-tests"]},
            {"cmd": "rdeps", "args": ["pkg.core"]},
        ],
    }
    run = script_run_codex.CodexRun(
        arm="B_auto",
        task_id="DI-01",
        task_type="diff_impact",
        model=script_run_codex.PARITY_CODEX_MODEL,
        successful_query_arguments=[["fn-rdeps", "pkg.core::target", "--exclude-tests"]],
    )

    fitness = script_run_codex._locked_query_fitness(task, run.arm, run)

    assert fitness is not None
    assert (fitness.overall, fitness.endpoint, fitness.target, fitness.options) == pytest.approx((0.5, 0.5, 0.5, 0.5))


def test_query_mismatch_does_not_reclassify_successful_transport_or_pooling(
    script_run_codex: Any,
) -> None:
    """A required Codemap call remains treatment-adherent when query fitness is imperfect."""
    run = script_run_codex.CodexRun(
        arm="C_strict",
        task_id="GR-01",
        task_type="graph_reasoning",
        model=script_run_codex.PARITY_CODEX_MODEL,
        success=True,
        compliance=True,
        treatment_adherence=True,
        locked_query_conformance=False,
        locked_query_fitness=0.25,
        locked_query_endpoint_fitness=0.0,
        locked_query_target_fitness=1.0,
        locked_query_option_fitness=1.0,
    )

    assert run.treatment_adherence is True
    assert script_run_codex._pooling_ineligibility_reasons(run) == ()


def test_all_locked_execution_queries_accept_strict_option_permutations(script_run_codex: Any) -> None:
    """Admit all 68 expected queries from the locked 55-task execution set under strict B/C conformance.

    Derive cases from tasks, not the normalizer's option vocabulary.
    """
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    execution_ids = manifest["preregistered_cells"]["structural_execution_task_ids"]
    tasks_by_id = {task["id"]: task for task in core.load_task_suite(SUITE_PATH)}
    tasks = [tasks_by_id[task_id] for task_id in execution_ids]
    assert len(tasks) == 55
    assert sum(len(task.get("expected_queries", [])) for task in tasks) == 68

    boolean_options = {"--broken", "--exclude-tests", "--with-imports"}

    def _permute_options(arguments: list[str]) -> list[str]:
        """Move valid option groups while retaining positional order."""
        groups: list[list[str]] = []
        positionals: list[str] = []
        index = 0
        while index < len(arguments):
            argument = arguments[index]
            if argument in boolean_options:
                groups.append([argument])
            elif argument == "--top":
                groups.append([argument, arguments[index + 1]])
                index += 1
            else:
                positionals.append(argument)
            index += 1
        if not groups:
            return arguments[:]
        if len(groups) > 1:
            groups.reverse()
        return [token for group in groups for token in group] + positionals

    for task in tasks:
        task_id = task["id"]
        queries = task.get("expected_queries", [])
        actual: list[list[str]] = []
        for query_index, query in enumerate(queries):
            assert isinstance(query, dict), (task_id, query_index)
            command = query.get("cmd")
            arguments = query.get("args")
            assert isinstance(command, str) and isinstance(arguments, list), (task_id, query_index, query)
            assert all(isinstance(argument, str) for argument in arguments), (task_id, query_index, query)
            actual.append([command, *_permute_options(arguments)])
        for arm in ("B_auto", "C_strict"):
            run = script_run_codex.CodexRun(
                arm=arm,
                task_id=task_id,
                task_type="contract-test",
                model=script_run_codex.PARITY_CODEX_MODEL,
                successful_query_arguments=actual,
            )
            assert script_run_codex._locked_query_conformance(tasks_by_id[task_id], arm, run) is True, (
                task_id,
                query_index,
                arm,
                actual,
            )


def test_input_snapshot_archives_hashes_but_never_credential_bytes(script_run_codex: Any, tmp_path: Path) -> None:
    """Paid provenance stores exact launch inputs while excluding auth contents."""
    source = tmp_path / "source"
    source.mkdir()
    manifest = source / "manifest.json"
    tasks = source / "tasks.json"
    runner = source / "runner.py"
    contract = source / "contract.py"
    launcher = source / "run-all.sh"
    index = source / "index.json"
    for path, value in (
        (manifest, "manifest"),
        (tasks, "tasks"),
        (runner, "runner"),
        (launcher, "launcher"),
        (index, "index"),
        (contract, "contract"),
    ):
        path.write_text(value, encoding="utf-8")
    package = source / "package"
    package.mkdir()
    (package / "module.py").write_text("module", encoding="utf-8")
    auth = source / "auth.json"
    auth.write_text("secret-token", encoding="utf-8")
    auth.chmod(0o600)
    snapshot = script_run_codex._write_input_snapshot(
        tmp_path / "inputs",
        manifest_path=manifest,
        tasks_path=tasks,
        runner_path=runner,
        invocation_launcher_path=launcher,
        index_path=index,
        auth_source=auth,
        arm_archives={"B_auto": {"direct-cli": package}},
        additional_shared_files={"readcrop-contracts.py": contract},
    )
    payload = json.loads((tmp_path / "inputs" / "input-snapshot.json").read_text(encoding="utf-8"))
    assert payload["auth_source"] == {"archived": False, "supplied": True}
    assert not any("auth.json" in entry["archived_path"] for entry in payload["files"])
    assert "secret-token" not in json.dumps(payload)
    assert str(auth) not in json.dumps(payload)
    assert payload["files"] == sorted(payload["files"], key=lambda item: (item["role"], item["archived_path"]))
    launcher_entry = next(entry for entry in payload["files"] if entry["role"] == "invocation_launcher")
    assert launcher_entry["archived_path"] == "shared/run-all.sh"
    assert launcher_entry["sha256"] == hashlib.sha256(launcher.read_bytes()).hexdigest()
    contract_entry = next(entry for entry in payload["files"] if entry["role"] == "shared:readcrop-contracts.py")
    assert contract_entry["archived_path"] == "shared/readcrop-contracts.py"
    assert contract_entry["sha256"] == hashlib.sha256(contract.read_bytes()).hexdigest()
    assert snapshot["sha256"] == hashlib.sha256((tmp_path / "inputs" / "input-snapshot.json").read_bytes()).hexdigest()


def test_input_snapshot_uses_source_names_for_nonstructural_launch_inputs(
    script_run_codex: Any, tmp_path: Path
) -> None:
    """A stage-specific adapter keeps its task and runner provenance legible."""
    source = tmp_path / "source"
    source.mkdir()
    manifest = source / "manifest.json"
    tasks = source / "tasks-readcrop.json"
    runner = source / "run-codex-structural.py"
    for path in (manifest, tasks, runner):
        path.write_text(path.name, encoding="utf-8")

    script_run_codex._write_input_snapshot(
        tmp_path / "inputs",
        manifest_path=manifest,
        tasks_path=tasks,
        runner_path=runner,
        invocation_launcher_path=runner,
        index_path=None,
        auth_source=None,
        arm_archives={},
    )
    payload = json.loads((tmp_path / "inputs" / "input-snapshot.json").read_text(encoding="utf-8"))
    archived = {entry["archived_path"] for entry in payload["files"]}

    assert {"shared/tasks-readcrop.json", "shared/run-codex-structural.py"}.issubset(archived)


@POSIX_SECURITY
def test_input_snapshot_keeps_private_executable_launcher_for_later_b_home(
    script_run_codex: Any,
    tmp_path: Path,
) -> None:
    """B must remain runnable from its immutable snapshot after the admission home is removed."""
    shared = tmp_path / "shared"
    shared.mkdir()
    manifest = shared / "manifest.json"
    tasks = shared / "tasks.json"
    runner_path = shared / "runner.py"
    for path in (manifest, tasks, runner_path):
        path.write_text(path.name, encoding="utf-8")

    runtime = tmp_path / "admission-home" / "direct-cli"
    launcher = runtime / "bin" / "codemap-py"
    exclusions = runtime / "bin" / "_exclusions.py"
    entrypoint = runtime / "scripts" / "codemap_py_entry.py"
    package = runtime / "src" / "codemap_py" / "__init__.py"
    for path in (launcher, exclusions, entrypoint, package):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"fixture:{path.name}\n", encoding="utf-8")
    launcher.chmod(0o755)
    launcher_bytes = launcher.read_bytes()

    snapshot_root = tmp_path / "inputs"
    script_run_codex._write_input_snapshot(
        snapshot_root,
        manifest_path=manifest,
        tasks_path=tasks,
        runner_path=runner_path,
        index_path=None,
        auth_source=None,
        arm_archives={"B_auto": {"direct-cli": runtime}},
    )
    archived_root = snapshot_root / "B_auto" / "direct-cli"
    archived_launcher = archived_root / "bin" / "codemap-py"
    archived_metadata = archived_launcher.lstat()
    assert stat.S_ISREG(archived_metadata.st_mode)
    assert archived_metadata.st_nlink == 1
    assert stat.S_IMODE(archived_metadata.st_mode) == 0o700
    assert archived_launcher.read_bytes() == launcher_bytes

    shutil.rmtree(runtime.parent)
    adapter = script_run_codex.CodexRunner("fixture-model", tmp_path)
    adapter._bind_runtime_snapshot(
        snapshot_root,
        {"B_auto": {"direct-cli": archived_root}},
    )
    frozen_launcher = adapter._runtime_direct_launcher("B_auto")
    home = script_run_codex.prepare_arm_home(
        "B_auto",
        root=tmp_path,
        codemap_bin=frozen_launcher,
    )
    try:
        assert home.codemap_verified is True
        assert home.codemap_launcher_path is not None
        assert home.codemap_launcher_path.read_bytes() == launcher_bytes
    finally:
        home.cleanup()


def test_invocation_launcher_validation_rejects_drift(script_run_codex: Any, tmp_path: Path) -> None:
    """A paid run cannot continue after its executing shell snapshot changes."""
    launcher = tmp_path / "run-all.sh"
    launcher.write_text("original", encoding="utf-8")
    expected = hashlib.sha256(launcher.read_bytes()).hexdigest()

    script_run_codex._validate_invocation_launcher(launcher, expected)
    launcher.write_text("mutated", encoding="utf-8")

    with pytest.raises(ValueError, match="invocation launcher changed"):
        script_run_codex._validate_invocation_launcher(launcher, expected)


def test_input_snapshot_includes_configuration_for_every_arm(script_run_codex: Any, tmp_path: Path) -> None:
    """Plain-arm provenance must not disappear when only treatments have package trees."""
    shared = tmp_path / "shared"
    shared.mkdir()
    manifest = shared / "manifest.json"
    tasks = shared / "tasks.json"
    runner = shared / "runner.py"
    for path in (manifest, tasks, runner):
        path.write_text(path.name, encoding="utf-8")

    arm_files: dict[str, dict[str, Path]] = {}
    arm_archives: dict[str, dict[str, Path]] = {}
    for arm in ("A_plain", "B_auto", "C_strict"):
        arm_root = tmp_path / arm
        arm_root.mkdir()
        config = arm_root / "config.toml"
        config.write_text(arm, encoding="utf-8")
        arm_files[arm] = {"config.toml": config}
        if arm != "A_plain":
            package = arm_root / "package"
            package.mkdir()
            (package / "payload.txt").write_text(arm, encoding="utf-8")
            arm_archives[arm] = {"package": package}

    script_run_codex._write_input_snapshot(
        tmp_path / "inputs",
        manifest_path=manifest,
        tasks_path=tasks,
        runner_path=runner,
        index_path=None,
        auth_source=None,
        arm_archives=arm_archives,
        arm_files=arm_files,
    )

    payload = json.loads((tmp_path / "inputs" / "input-snapshot.json").read_text(encoding="utf-8"))
    archived = {entry["archived_path"] for entry in payload["files"]}
    assert {f"{arm}/config.toml" for arm in arm_files}.issubset(archived)


def test_snapshot_copy_rejects_source_replacement_between_validation_and_open(
    script_run_codex: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Snapshot bytes must come from the exact inode that passed validation."""
    source = tmp_path / "source.json"
    source.write_text("validated", encoding="utf-8")
    replacement = tmp_path / "replacement.json"
    replacement.write_text("substituted", encoding="utf-8")
    destination = tmp_path / "snapshot" / "source.json"
    entries: list[dict[str, Any]] = []
    original_open = os.open
    replaced = False

    def _replace_before_open(path: str | bytes | os.PathLike[str] | os.PathLike[bytes], flags: int, *args: Any) -> int:
        """Swap the authentication source for a symlink before its first matching open."""
        nonlocal replaced
        if Path(path) == source and not replaced:
            replaced = True
            source.unlink()
            source.symlink_to(replacement)
        return original_open(path, flags, *args)

    monkeypatch.setattr(script_run_codex.os, "open", _replace_before_open)

    with pytest.raises(ValueError, match="changed|copied securely|symlink"):
        script_run_codex._archive_snapshot_file(
            source,
            destination,
            role="fixture",
            archive_root=destination.parent,
            entries=entries,
        )

    assert not destination.exists()
    assert entries == []


@pytest.mark.parametrize("source_kind", ["leaf-symlink", "hardlink", "escaping-parent"])
def test_snapshot_source_indirection_fails_closed(
    script_run_codex: Any,
    tmp_path: Path,
    source_kind: str,
) -> None:
    """Snapshot admission rejects direct, linked, and escaping source aliases."""
    source_root = tmp_path / "source-root"
    source_root.mkdir()
    real = source_root / "real.json"
    real.write_text("fixture", encoding="utf-8")
    source = source_root / "source.json"
    if source_kind == "leaf-symlink":
        source.symlink_to(real)
    elif source_kind == "hardlink":
        os.link(real, source)
    else:
        outside = tmp_path / "outside"
        outside.mkdir()
        outside_file = outside / "source.json"
        outside_file.write_text("outside", encoding="utf-8")
        escaping = source_root / "escaping"
        escaping.symlink_to(outside, target_is_directory=True)
        source = escaping / "source.json"
    destination = tmp_path / "snapshot" / "source.json"
    entries: list[dict[str, Any]] = []

    with pytest.raises(ValueError, match="symlink|single-link|escaped"):
        script_run_codex._archive_snapshot_file(
            source,
            destination,
            role="fixture",
            archive_root=destination.parent,
            source_root=source_root,
            entries=entries,
        )

    assert not destination.exists()
    assert entries == []


def test_staged_direct_cli_admission_rejects_malformed_output_and_index_mutation(
    script_run_codex: Any, tmp_path: Path
) -> None:
    """B admission rejects malformed probes and any locked-index mutation."""
    repo = tmp_path / "repo"
    repo.mkdir()
    index = repo / "index.json"
    index.write_text("locked", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "direct_cli_admission": {
                    "probe_subcommand": "fn-rdeps",
                    "probe_target": "package.module::target",
                }
            }
        ),
        encoding="utf-8",
    )
    home_path = tmp_path / "home"
    home_path.mkdir()
    launcher = home_path / "codemap-py"
    launcher.write_text("launcher", encoding="utf-8")
    home = script_run_codex.ArmHome(
        "B_auto",
        home_path,
        {"PATH": "/fixture"},
        True,
        codemap_verified=True,
        permission_profile="provider-parity-codemap",
        codemap_launcher_path=launcher,
    )

    with pytest.raises(RuntimeError, match="admission query failed"):
        script_run_codex._admit_staged_direct_cli(
            home,
            repo,
            index,
            manifest_path=manifest,
            command_runner=lambda *_args, **_kwargs: (0, "not JSON", ""),
        )

    def _mutate_index(*_args: Any, **_kwargs: Any) -> tuple[int, str, str]:
        """Modify the locked index before returning otherwise successful query evidence."""
        index.write_text("mutated", encoding="utf-8")
        return 0, '{"index":{"query_complete":true,"compact":true}}', ""

    with pytest.raises(RuntimeError, match="mutated the locked index"):
        script_run_codex._admit_staged_direct_cli(
            home,
            repo,
            index,
            manifest_path=manifest,
            command_runner=_mutate_index,
        )


@pytest.mark.parametrize(
    "selected_arms",
    [pytest.param(("B_auto", "C_strict"), id="both-treatments"), pytest.param(("C_strict",), id="skill-only")],
)
def test_expected_query_preflight_runs_unique_b_queries_once_and_never_replays_c(
    script_run_codex: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    selected_arms: tuple[str, ...],
) -> None:
    """Any treatment study deduplicates queries through B and skips C replay."""
    repo = tmp_path / "repo"
    repo.mkdir()
    index = repo / "index.json"
    index.write_text("locked", encoding="utf-8")
    home_path = tmp_path / "home"
    home_path.mkdir()
    launcher = home_path / "codemap-py"
    launcher.write_text("launcher", encoding="utf-8")
    runner = object.__new__(script_run_codex.CodexRunner)
    runner.repo_path = repo
    runner.index_path = index
    prepared_arms: list[str] = []
    commands: list[list[str]] = []

    class Home:
        """Supply the B-only runtime fields required by query preflight."""

        arm = "B_auto"
        env = {"PATH": "/fixture"}
        permission_profile = "provider-parity-codemap"
        codemap_launcher_path = launcher
        coordination_path = None

        def cleanup(self) -> None:
            """Provide fixture cleanup."""
            return None

    def _prepare(arm: str) -> Home:
        """Prepare and record an isolated arm home."""
        prepared_arms.append(arm)
        return Home()

    def _command_runner(command: list[str], **_kwargs: Any) -> tuple[int, str, str]:
        """Record commands and return fixture responses."""
        commands.append(command)
        return 0, '{"index":{"query_complete":true,"compact":true}}', ""

    runner.command_runner = _command_runner
    monkeypatch.setattr(runner, "_prepare_verified_home", _prepare)
    runner.preflight_expected_queries(
        [
            {"id": "first", "expected_queries": [{"cmd": "central", "args": ["package"]}]},
            {
                "id": "second",
                "expected_queries": [
                    {"cmd": "central", "args": ["package"]},
                    {"cmd": "fn-rdeps", "args": ["package.module::target"]},
                ],
            },
        ],
        selected_arms,
    )

    assert prepared_arms == ["B_auto"]
    assert [command[-2:] for command in commands] == [["central", "package"], ["fn-rdeps", "package.module::target"]]


def test_expected_query_preflight_rejects_malformed_or_failed_b_queries(
    script_run_codex: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Malformed contracts and failed B commands stop the study before snapshotting."""
    repo = tmp_path / "repo"
    repo.mkdir()
    index = repo / "index.json"
    index.write_text("locked", encoding="utf-8")
    home_path = tmp_path / "home"
    home_path.mkdir()
    launcher = home_path / "codemap-py"
    launcher.write_text("launcher", encoding="utf-8")
    runner = object.__new__(script_run_codex.CodexRunner)
    runner.repo_path = repo
    runner.index_path = index
    runner.command_runner = lambda *_args, **_kwargs: (1, "", "fixture failure")

    class Home:
        """Supply the B-only runtime fields required by rejecting preflight."""

        env = {"PATH": "/fixture"}
        permission_profile = "provider-parity-codemap"
        codemap_launcher_path = launcher
        coordination_path = None

        def cleanup(self) -> None:
            """Provide fixture cleanup."""
            return None

    monkeypatch.setattr(runner, "_prepare_verified_home", lambda _arm: Home())

    with pytest.raises(RuntimeError, match="malformed expected query args"):
        runner.preflight_expected_queries(
            [{"id": "malformed", "expected_queries": [{"cmd": "central", "args": "package"}]}],
            ("B_auto",),
        )
    with pytest.raises(RuntimeError, match="expected query failed.*fixture failure"):
        runner.preflight_expected_queries(
            [{"id": "failed", "expected_queries": [{"cmd": "central", "args": ["package"]}]}],
            ("B_auto",),
        )


def test_targeted_cells_are_explicitly_non_poolable(script_run_codex: Any) -> None:
    """Explicitly selected rows stay visible but cannot enter headline pooling."""
    run = script_run_codex.CodexRun(
        arm="A_plain",
        task_id="CQ-05",
        task_type="code_quality",
        model=script_run_codex.PARITY_CODEX_MODEL,
        success=True,
        targeted=True,
    )
    assert script_run_codex._pooling_ineligibility_reasons(run) == ("targeted",)


@pytest.mark.parametrize(
    ("selectors", "expected_ids"),
    [
        pytest.param("DI", ["DI-01", "DI-02", "DI-03", "DI-04", "DI-05", "DI-06"], id="di"),
        pytest.param(
            "DI,GR",
            ["DI-01", "DI-02", "DI-03", "DI-04", "DI-05", "DI-06", "GR-01", "GR-02", "GR-03", "GR-04"],
            id="di-gr",
        ),
        pytest.param(
            "GR-03,DI-01,DI", ["DI-01", "DI-02", "DI-03", "DI-04", "DI-05", "DI-06", "GR-03"], id="gr-03-di-01-di"
        ),
        pytest.param("DI,DI-01,DI", ["DI-01", "DI-02", "DI-03", "DI-04", "DI-05", "DI-06"], id="di-di-01-di"),
    ],
)
def test_resolve_task_selection_is_manifest_ordered_and_deduplicated(
    script_run_codex: Any, selectors: str, expected_ids: list[str]
) -> None:
    """Selectors preserve the manifest order rather than caller token order."""
    scope = script_run_codex.resolve_task_selection(MANIFEST_PATH, selectors)
    assert scope["task_ids"] == expected_ids
    assert scope["selection_mode"] == "selected"
    assert scope["stages"] == [
        {
            "stage_id": "structural",
            "task_ids": expected_ids,
            "repetitions": 3,
            "arms": list(script_run_codex.CODEX_STRUCTURAL_ARMS),
            "total_cells": len(expected_ids) * 9,
        }
    ]
    assert "complete_run_max_wall_clock_seconds" not in scope
    assert len(scope["scope_sha256"]) == 64


@pytest.mark.parametrize(
    ("selectors", "error"),
    [
        pytest.param("", "empty tokens", id="empty"),
        pytest.param("DI,,GR", "empty tokens", id="di-gr"),
        pytest.param("ZZ", "unknown task selector", id="zz"),
        pytest.param("RI", "unknown task selector", id="ri"),
        pytest.param("RI-01", "unknown task selector", id="ri-01"),
    ],
)
def test_resolve_task_selection_rejects_empty_and_unknown_selectors(
    script_run_codex: Any, selectors: str, error: str
) -> None:
    """The public selector surface fails closed before any task loading occurs."""
    with pytest.raises(ValueError, match=error):
        script_run_codex.resolve_task_selection(MANIFEST_PATH, selectors)


@pytest.mark.parametrize(
    ("repetitions", "arm", "scope_sha256", "error"),
    [
        pytest.param(2, "all", "match", "repetition", id="2"),
        pytest.param(3, "A_plain", "match", "arm all", id="3-a_plain"),
        pytest.param(3, "all", "0" * 64, "SHA-256", id="3-all-0-64"),
        pytest.param(3, "all", None, "requires --scope-sha256", id="3-all-none"),
    ],
)
def test_paid_targeted_scope_rejects_control_or_hash_tampering(
    script_run_codex: Any,
    repetitions: int,
    arm: str,
    scope_sha256: str | None,
    error: str,
) -> None:
    """A paid subset cannot alter reviewed coordinates, controls, or scope identity."""
    scope = script_run_codex._resolve_structural_task_selection(MANIFEST_PATH, "DI")
    if scope_sha256 == "match":
        scope_sha256 = scope["scope_sha256"]
    with pytest.raises(ValueError, match=error):
        script_run_codex._validate_targeted_scope_request(
            scope,
            repetitions=repetitions,
            arm=arm,
            scope_sha256=scope_sha256,
            dry_run=False,
        )


def test_unscoped_paid_task_ids_allow_only_exact_confirmatory_sequence(script_run_codex: Any, tmp_path: Path) -> None:
    """Direct paid subsets cannot bypass the public scope-bound selector."""
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({"preregistered_cells": {"structural_execution_task_ids": ["DI-01", "GR-01"]}}),
        encoding="utf-8",
    )

    script_run_codex._validate_unscoped_paid_task_ids(
        manifest_path,
        ["DI-01", "GR-01"],
        targeted=False,
        dry_run=False,
    )
    with pytest.raises(ValueError, match="paid task subsets require --tasks"):
        script_run_codex._validate_unscoped_paid_task_ids(
            manifest_path,
            ["DI-01"],
            targeted=False,
            dry_run=False,
        )
    script_run_codex._validate_unscoped_paid_task_ids(
        manifest_path,
        ["DI-01"],
        targeted=False,
        dry_run=True,
    )


def test_targeted_scope_is_persisted_separately_from_confirmatory_metadata(
    script_run_codex: Any, tmp_path: Path
) -> None:
    """Targeted rows are durable nonpoolable evidence rather than confirmatory data."""
    scope = script_run_codex._resolve_structural_task_selection(MANIFEST_PATH, "DI")
    task_arms = {
        (task_id, repetition): script_run_codex.CODEX_STRUCTURAL_ARMS
        for task_id in scope["task_ids"]
        for repetition in range(1, scope["repetitions"] + 1)
    }
    metadata = script_run_codex._initial_run_metadata(
        manifest_path=MANIFEST_PATH,
        repo_path=tmp_path,
        index_path=None,
        output_path=tmp_path / "telemetry.jsonl",
        metadata_path=tmp_path / "metadata.json",
        model=script_run_codex.PARITY_CODEX_MODEL,
        reasoning_effort=script_run_codex.PARITY_CODEX_REASONING_EFFORT,
        repetitions=scope["repetitions"],
        task_arms=task_arms,
        cell_wall_clock_seconds=float(scope["coordinate_timeout_seconds"]),
        auth_provisioned=False,
        study_mode="targeted",
        targeted_scope=scope,
    )
    execution = metadata["execution"]
    assert execution["study_mode"] == "targeted"
    assert execution["targeted_scope"] == scope
    assert execution["targeted_scope_sha256"] == scope["scope_sha256"]
    assert execution["planned_cells"] == 54


def test_historical_headline_exclusion_is_not_diagnostic_mode(script_run_codex: Any) -> None:
    """Normal study rows can be headline-ineligible without diagnostic rerun status."""
    run = script_run_codex.CodexRun(
        arm="A_plain",
        task_id="CQ-05",
        task_type="code_quality",
        model=script_run_codex.PARITY_CODEX_MODEL,
        success=True,
        headline_eligible_v1=False,
        diagnostic_only=False,
    )
    assert script_run_codex._pooling_ineligibility_reasons(run) == ()


def test_diff_impact_stager_wraps_all_arms_and_restores_on_success_or_failure(
    script_run_codex: Any, tmp_path: Path
) -> None:
    """The shared Claude stager exposes identical staged bytes to every arm and always reverts."""
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "module.py"
    target.write_text("BASE\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "add", "module.py"], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "fixture",
        ],
        check=True,
        capture_output=True,
    )
    task = {"id": "DI-01", "type": "diff_impact", "stage": [{"file": "module.py", "append": "STAGED\n"}]}
    stager = script_run_codex._diff_impact_stager(repo, task)
    assert stager is not None
    seen: list[str] = []
    with stager:
        for _arm in script_run_codex.CODEX_STRUCTURAL_ARMS:
            seen.append(target.read_text(encoding="utf-8"))
    assert seen == ["BASE\nSTAGED\n"] * 3
    assert target.read_text(encoding="utf-8") == "BASE\n"

    failing = script_run_codex._diff_impact_stager(repo, task)
    assert failing is not None
    with pytest.raises(RuntimeError):
        with failing:
            assert target.read_text(encoding="utf-8") == "BASE\nSTAGED\n"
            raise RuntimeError("arm failure")
    assert target.read_text(encoding="utf-8") == "BASE\n"


def _make_locked_diff_impact_repo(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, Any]]:
    """Create one clean tracked target plus its resolver-path locked index."""
    repo = tmp_path / "target"
    repo.mkdir()
    target = repo / "module.py"
    target.write_text("BASE\n", encoding="utf-8")
    (repo / ".gitignore").write_text("/.cache/\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "add", "module.py", ".gitignore"], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "fixture",
        ],
        check=True,
        capture_output=True,
    )
    commit = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    index = repo / ".cache" / "codemap" / f"{repo.name}.json"
    index.parent.mkdir(parents=True)
    index.write_text(json.dumps({"git_sha": commit, "scan_version": 11}), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "target_source": {"commit": commit},
                "index": {
                    "raw_sha256": hashlib.sha256(index.read_bytes()).hexdigest(),
                    "git_sha": commit,
                    "scan_version": 11,
                },
            }
        ),
        encoding="utf-8",
    )
    task: dict[str, Any] = {
        "id": "DI-fixture",
        "type": "diff_impact",
        "stage": [{"file": "module.py", "append": "STAGED\n"}],
    }
    return repo, index, manifest, task


def test_diff_impact_admission_requires_exact_status_and_post_stage_bytes(
    script_run_codex: Any, tmp_path: Path
) -> None:
    """DI may admit only its declared modified tracked file at captured bytes."""
    repo, index, manifest, task = _make_locked_diff_impact_repo(tmp_path)
    script_run_codex._validate_locked_runtime(repo, index, "A_plain", manifest)
    stager = script_run_codex._diff_impact_stager(repo, task)
    assert stager is not None
    with stager:
        admission = script_run_codex._capture_diff_impact_stage(repo, task)
        script_run_codex._validate_locked_runtime(repo, index, "A_plain", manifest, admission)

        (repo / "module.py").write_text("BASE\nTAMPERED\n", encoding="utf-8")
        with pytest.raises(ValueError, match="bytes changed after admission"):
            script_run_codex._validate_locked_runtime(repo, index, "A_plain", manifest, admission)

    script_run_codex._validate_locked_runtime(repo, index, "A_plain", manifest)


def test_diff_impact_contamination_persists_stage_and_worktree_evidence(script_run_codex: Any, tmp_path: Path) -> None:
    """A DI rejection retains enough evidence to diagnose the mutated stage."""
    repo, index, manifest, task = _make_locked_diff_impact_repo(tmp_path)
    task["prompt"] = "Inspect the staged change."
    stager = script_run_codex._diff_impact_stager(repo, task)
    assert stager is not None

    def _transport(_command: list[str], **_kwargs: Any) -> str:
        """Return fixture transport evidence; no provider call."""
        (repo / "module.py").write_text("BASE\nSTAGED\nTAMPERED\n", encoding="utf-8")
        return _completed_stream()

    with stager:
        admission = script_run_codex._capture_diff_impact_stage(repo, task)
        runner = script_run_codex.CodexRunner(
            "fixture",
            repo,
            index_path=index,
            manifest_path=manifest,
            transport=_transport,
        )
        result = runner.run(task, "A_plain", diff_impact_stage=admission)

    assert result.contaminated is True
    assert result.stage_evidence == {
        "stage": task["stage"],
        "changed_paths": ["module.py"],
        "expected_status": {"module.py": " M"},
        "observed_status": {"module.py": " M"},
    }


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        pytest.param("extra-tracked", "unexpected worktree status", id="extra-tracked"),
        pytest.param("extra-untracked", "unexpected worktree status", id="extra-untracked"),
        pytest.param("delete-stage", "unexpected worktree status", id="delete-stage"),
        pytest.param("symlink-stage", "unexpected worktree status", id="symlink-stage"),
        pytest.param("hardlink-stage", "unlinked regular tracked files", id="hardlink-stage"),
    ],
)
def test_diff_impact_admission_rejects_worktree_mutations(
    script_run_codex: Any, tmp_path: Path, mutation: str, error: str
) -> None:
    """Extra paths and destructive type changes cannot hide behind DI admission."""
    repo, index, manifest, task = _make_locked_diff_impact_repo(tmp_path)
    extra = repo / "extra.py"
    extra.write_text("BASE\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "extra.py"], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "extra",
        ],
        check=True,
        capture_output=True,
    )
    commit = script_run_codex._repo_sha(repo)
    index.write_text(json.dumps({"git_sha": commit, "scan_version": 11}), encoding="utf-8")
    manifest.write_text(
        json.dumps(
            {
                "target_source": {"commit": commit},
                "index": {
                    "raw_sha256": hashlib.sha256(index.read_bytes()).hexdigest(),
                    "git_sha": commit,
                    "scan_version": 11,
                },
            }
        ),
        encoding="utf-8",
    )
    stager = script_run_codex._diff_impact_stager(repo, task)
    assert stager is not None
    with stager:
        admission = script_run_codex._capture_diff_impact_stage(repo, task)
        if mutation == "extra-tracked":
            extra.write_text("MUTATED\n", encoding="utf-8")
        elif mutation == "extra-untracked":
            (repo / "untracked.py").write_text("UNTRACKED\n", encoding="utf-8")
        elif mutation == "delete-stage":
            (repo / "module.py").unlink()
        elif mutation == "symlink-stage":
            (repo / "module.py").unlink()
            (repo / "module.py").symlink_to(extra)
        else:
            (repo / "module.py").unlink()
            staged_copy = tmp_path / "staged-copy.py"
            staged_copy.write_text("BASE\nSTAGED\n", encoding="utf-8")
            os.link(staged_copy, repo / "module.py")
        with pytest.raises(ValueError, match=error):
            script_run_codex._validate_locked_runtime(repo, index, "A_plain", manifest, admission)

    subprocess.run(["git", "-C", str(repo), "checkout", "--", "module.py", "extra.py"], check=True, capture_output=True)
    (repo / "untracked.py").unlink(missing_ok=True)
    script_run_codex._validate_locked_runtime(repo, index, "A_plain", manifest)


def test_git_status_always_includes_untracked_files(
    script_run_codex: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A user Git configuration cannot hide an untracked contaminant from admission."""
    commands: list[list[str]] = []

    def _run(command: list[str], **_kwargs: Any) -> Any:
        """Record argv and return an empty successful subprocess result."""
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(script_run_codex.subprocess, "run", _run)
    assert script_run_codex._git_porcelain_status(tmp_path) == {}
    assert commands == [["git", "-C", str(tmp_path), "status", "--porcelain=v1", "-z", "--untracked-files=all"]]


def test_git_status_failure_rejects_admission(
    script_run_codex: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Git-status failures cannot be treated as a clean worktree."""
    monkeypatch.setattr(
        script_run_codex.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout="", stderr="fixture failure"),
    )

    with pytest.raises(ValueError, match="could not verify worktree cleanliness"):
        script_run_codex._git_porcelain_status(tmp_path)


@POSIX_SECURITY
def test_diff_impact_preflight_exercises_stage_admission_and_strict_revert(
    script_run_codex: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No-model DI preflight proves enter, exact admission, and clean restoration together."""
    repo, index, manifest, task = _make_locked_diff_impact_repo(tmp_path)
    runner = script_run_codex.CodexRunner("fixture", repo, index_path=index, manifest_path=manifest)
    admitted: list[str] = []

    def _prepare(arm: str, *, diff_impact_stage: Any = None) -> Any:
        """Prepare and record an isolated arm home."""
        assert diff_impact_stage is not None
        script_run_codex._validate_locked_runtime(repo, index, arm, manifest, diff_impact_stage)
        admitted.append(arm)
        home_path = tmp_path / f"home-{arm}"
        home_path.mkdir()
        home_path.chmod(0o700)
        return script_run_codex.ArmHome(arm, home_path, {}, False)

    monkeypatch.setattr(runner, "_prepare_verified_home", _prepare)
    runner.preflight_diff_impact_stages([task], script_run_codex.CODEX_STRUCTURAL_ARMS)

    assert admitted == list(script_run_codex.CODEX_STRUCTURAL_ARMS)
    script_run_codex._validate_locked_runtime(repo, index, "A_plain", manifest)


def test_main_dry_run_calls_diff_impact_preflight_and_can_suppress_legend(
    script_run_codex: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The paid DI lifecycle is preflighted without a model and legend suppression keeps wrapper output bounded."""
    task = {"id": "DI-fixture", "prompt": "prompt", "type": "diff_impact", "stage": [{"file": "x", "append": "y"}]}
    calls: list[tuple[str, ...]] = []

    class FixtureRunner:
        """Expose only no-model probe seams for main's dry-run contract."""

        timeout = 600.0

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            """Initialize fixture state."""
            pass

        def probe_arm(self, _arm: str) -> dict[str, bool]:
            """Report the arm's fixture availability."""
            return {"codemap_available": False}

        def preflight_diff_impact_stages(self, _tasks: list[dict[str, Any]], arms: tuple[str, ...]) -> None:
            """Record the treatments supplied for diff-impact preflight."""
            calls.append(arms)

    monkeypatch.setattr(script_run_codex, "load_tasks_with_provenance", lambda _path, *_args: [task])
    monkeypatch.setattr(script_run_codex, "_read_manifest_revision", lambda *_args: "fixture-revision")
    monkeypatch.setattr(script_run_codex, "_locked_task_ordinal", lambda *_args: 0)
    monkeypatch.setattr(script_run_codex, "_validate_diff_impact_stage", lambda *_args: None)
    monkeypatch.setattr(script_run_codex, "CodexRunner", FixtureRunner)

    script_run_codex.main(
        repo_path=tmp_path,
        model=script_run_codex.PARITY_CODEX_MODEL,
        tasks_path=tmp_path / "tasks.json",
        dry_run=True,
        show_legend=False,
    )

    output = capsys.readouterr().out
    assert calls == [script_run_codex.CODEX_STRUCTURAL_ARMS]
    assert "LEGEND" not in output
    assert "PROBE   A_plain " in output
    assert "PLAN    DI-fixture" in output


@pytest.mark.parametrize(
    ("command", "expected_credit"),
    [
        pytest.param('"$CODEMAP_BIN" query --compact coupled', True, id="quoted-public-zero-argument-query"),
        pytest.param('"${CODEMAP_BIN}" query --compact coupled', True, id="braced-public-zero-argument-query"),
        pytest.param('"$CODEMAP_BIN" query --help', False, id="help-only"),
    ],
)
def test_public_query_forms_credit_completed_queries_but_not_help(
    script_run_codex: Any,
    command: str,
    expected_credit: bool,
) -> None:
    """A public compact query may omit target arguments; a help call is not evidence."""
    parsed = codex_runtime.parse_codex_jsonl(
        _completed_stream(
            commands=[
                {
                    "type": "command_execution",
                    "command": command,
                    "status": "completed",
                    "exit_code": 0,
                    "aggregated_output": '{"index":{"query_complete":true,"compact":true}}',
                }
            ]
        )
    )

    assert parsed.codemap_direct_compact_successful_calls == int(expected_credit)
    assert script_run_codex._arm_compliance("B_auto", parsed) is expected_credit


def test_skill_activation_then_public_coupled_query_satisfies_compliance_and_adherence(
    script_run_codex: Any, tmp_path: Path
) -> None:
    """C requires its exact Skill activation plus one completed public compact query."""
    skill_path = tmp_path / "query-code" / "SKILL.md"
    skill_path.parent.mkdir()
    skill_bytes = b"# query-code\nUse the compact query.\n"
    skill_path.write_bytes(skill_bytes)
    events = [
        {
            "type": "item.completed",
            "item": {
                "id": "skill-read",
                "type": "command_execution",
                "command": 'cat "$CODEMAP_SKILL_FILE"',
                "status": "completed",
                "exit_code": 0,
                "aggregated_output": skill_bytes.decode(),
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "query",
                "type": "command_execution",
                "command": '"${CODEMAP_BIN}" query --compact coupled',
                "status": "completed",
                "exit_code": 0,
                "aggregated_output": '{"index":{"query_complete":true,"compact":true}}',
            },
        },
        {"type": "turn.completed"},
    ]

    parsed = codex_runtime.parse_codex_jsonl(
        "\n".join(json.dumps(event) for event in events),
        skill_path=skill_path,
        skill_sha256=hashlib.sha256(skill_bytes).hexdigest(),
    )
    compliance = script_run_codex._arm_compliance("C_strict", parsed)

    assert parsed.skill_delivery_observed is True
    assert parsed.codemap_skill_compact_successful_calls == 1
    assert compliance is True
    assert core.treatment_adherence("C_strict", codemap_use_compliance=compliance, contaminated=False) is True


@pytest.mark.parametrize(
    ("read_command", "read_output", "query_first", "expected_manual_read"),
    [
        pytest.param('cat "$CODEMAP_SKILL_FILE"', "locked", False, True, id="canonical-skill-file"),
        pytest.param(
            "/bin/zsh -lc 'cat \"$CODEMAP_SKILL_FILE\"'",
            "locked",
            False,
            True,
            id="canonical-transport-wrapper",
        ),
        pytest.param("cat {skill}", "locked", False, False, id="literal-path"),
        pytest.param("sed -n 1,260p {skill}", "locked", False, False, id="static-sed-reader"),
        pytest.param("sed -n '1,$p' {skill}", "locked", False, False, id="dynamic-sed-reader"),
        pytest.param("cat $CODEMAP_SKILL_FILE", "locked", False, False, id="unquoted-skill-file"),
        pytest.param('cat "$OTHER_SKILL_FILE"', "locked", False, False, id="wrong-variable"),
        pytest.param("skill_path={skill}; cat $skill_path", "locked", False, False, id="bound-reader"),
        pytest.param("cat {skill}; printf activated", "lockedactivated", False, False, id="compound-reader"),
        pytest.param('cat "$CODEMAP_SKILL_FILE"', "changed", False, False, id="wrong-reader-bytes"),
        pytest.param('cat "$CODEMAP_SKILL_FILE"', "locked", True, False, id="read-after-query"),
    ],
)
def test_C_strict_binding_credits_a_valid_query_while_retaining_manual_read_diagnostics(
    script_run_codex: Any,
    tmp_path: Path,
    read_command: str,
    read_output: str,
    query_first: bool,
    expected_manual_read: bool,
) -> None:
    """Separate installed-Skill query credit from optional manual-read telemetry."""
    skill_path = tmp_path / "query-code" / "SKILL.md"
    skill_path.parent.mkdir()
    skill_bytes = b"locked"
    skill_path.write_bytes(skill_bytes)
    reader = {
        "type": "item.completed",
        "item": {
            "id": "skill-read",
            "type": "command_execution",
            "command": read_command.format(skill=skill_path),
            "status": "completed",
            "exit_code": 0,
            "aggregated_output": skill_bytes.decode() if read_output == "locked" else read_output,
        },
    }
    query = {
        "type": "item.completed",
        "item": {
            "id": "query",
            "type": "command_execution",
            "command": '"$CODEMAP_BIN" query --compact fn-rdeps "pkg.core::target"',
            "status": "completed",
            "exit_code": 0,
            "aggregated_output": '{"index":{"query_complete":true,"compact":true}}',
        },
    }
    ordered_events = [query, reader] if query_first else [reader, query]
    parsed = codex_runtime.parse_codex_jsonl(
        "\n".join(json.dumps(event) for event in [*ordered_events, {"type": "turn.completed"}]),
        skill_path=skill_path,
        skill_sha256=hashlib.sha256(skill_bytes).hexdigest(),
    )

    assert parsed.codemap_skill_compact_successful_calls == 1
    assert parsed.skill_delivery_observed is expected_manual_read
    assert script_run_codex._arm_compliance("C_strict", parsed) is True


def test_cli_exposes_only_task_selection_not_study_or_paid_switches(script_run_codex: Any) -> None:
    """The single runner infers internal stages from task IDs.

    Prevents deprecated public routing and confirmation switches from surviving after task selection becomes the
    complete execution interface.
    """
    parameters = inspect.signature(script_run_codex.cli).parameters

    assert "study" not in parameters
    assert "paid" not in parameters
    assert "task_id" not in parameters


@pytest.mark.parametrize(
    ("legacy_flag", "value"),
    [
        pytest.param("--study", "structural", id="study"),
        pytest.param("--paid", "true", id="paid"),
        pytest.param("--task-id", "FN-02", id="task-id"),
    ],
)
def test_removed_cli_flags_fail_before_task_resolution_or_plan_output(legacy_flag: str, value: str) -> None:
    """Deprecated routing flags must not invoke the unified runner before Fire rejects them."""
    completed = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), legacy_flag, value],
        cwd=BENCHMARKS_DIR.parent,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "PLAN" not in completed.stdout
    assert "SCOPE" not in completed.stdout
    assert "PAID_COMMAND" not in completed.stdout


def test_unified_paid_command_preserves_the_supplied_absolute_manifest_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Dry-run handoff must replay a non-default manifest, not silently substitute the default."""
    custom_manifest = tmp_path / "custom manifest.json"

    codex_runtime.print_unified_paid_command(
        repo_path=tmp_path,
        manifest_path=custom_manifest,
        index_path=tmp_path / "locked-index.json",
        marketplace_root=tmp_path / "marketplace root",
        codemap_bin=tmp_path / "codemap-py",
        model="gpt-5.6-luna",
        selectors=("RC", "FS-03"),
        scope_sha256="a" * 64,
        patch_pytest="/opt/bench runtime/bin/pytest",
    )

    command = capsys.readouterr().out
    assert f"--manifest-path '{custom_manifest.resolve()}'" in command
    # The label and its rule precede the copyable command, so the assignment opens the third line.
    assert command.splitlines()[2].startswith("CODEMAP_BENCH_PATCH_PYTEST='/opt/bench runtime/bin/pytest' python3")
    assert "--tasks RC,FS-03" in command
    assert "--study" not in command
    assert "--paid " not in command
    assert "--task-id" not in command
    # The command is framed, so its last flag sits above the closing rule rather than at the end.
    assert command.splitlines()[-2] == "  --paid-approval aaaaaaaaaaaaaaaa"
    assert command.splitlines()[-1] == "-" * 78


def test_resolve_task_selection_without_selectors_plans_all_stage_cells(script_run_codex: Any) -> None:
    """Omitting ``--tasks`` must plan the complete 73-task, 219-cell benchmark."""
    scope = script_run_codex.resolve_task_selection(MANIFEST_PATH, None)
    stage_task_ids = {stage["stage_id"]: stage["task_ids"] for stage in scope["stages"]}

    assert len(scope["task_ids"]) == 73
    assert scope["total_tasks"] == 73
    assert scope["total_cells"] == 219
    assert {task_id[:2] for task_id in scope["task_ids"]} >= {"RC", "FS", "FM", "PT"}
    assert stage_task_ids == {
        "structural": scope["task_ids"][:55],
        "readcrop": [f"RC-{number:02d}" for number in range(1, 7)],
        "fix-single": [f"FS-{number:02d}" for number in range(1, 5)],
        "fix-multi": [f"FM-{number:02d}" for number in range(1, 4)],
        "patch": [f"PT-{number:02d}" for number in range(1, 6)],
    }


def test_resolve_task_selection_partitions_mixed_families_and_ids_once(script_run_codex: Any) -> None:
    """Mixed selectors deduplicate before dispatching every task to its native scorer."""
    scope = script_run_codex.resolve_task_selection(MANIFEST_PATH, "FM-02,RC,PT-03,FS-03,RC-01,FM,PT")
    stage_task_ids = {stage["stage_id"]: stage["task_ids"] for stage in scope["stages"]}

    assert scope["task_ids"] == [
        *[f"RC-{number:02d}" for number in range(1, 7)],
        "FS-03",
        "FM-01",
        "FM-02",
        "FM-03",
        "PT-01",
        "PT-02",
        "PT-03",
        "PT-04",
        "PT-05",
    ]
    assert scope["total_cells"] == 45
    assert stage_task_ids == {
        "readcrop": [f"RC-{number:02d}" for number in range(1, 7)],
        "fix-single": ["FS-03"],
        "fix-multi": ["FM-01", "FM-02", "FM-03"],
        "patch": [f"PT-{number:02d}" for number in range(1, 6)],
    }


def test_resolve_task_selection_routes_one_exact_patch_task_to_its_native_stage(script_run_codex: Any) -> None:
    """An exact PT selector must not expand to the Patch family or structural loop."""
    scope = script_run_codex.resolve_task_selection(MANIFEST_PATH, "PT-01")

    assert scope["task_ids"] == ["PT-01"]
    assert scope["total_tasks"] == 1
    assert scope["total_cells"] == 3
    assert scope["stages"] == [
        {
            "stage_id": "patch",
            "task_ids": ["PT-01"],
            "repetitions": 1,
            "arms": ["A_plain", "B_auto", "C_strict"],
            "total_cells": 3,
        }
    ]


def test_unified_scope_digest_changes_with_each_stage_partition(script_run_codex: Any) -> None:
    """One approval must bind all selected stage scopes, not only structural rows."""
    first = script_run_codex.resolve_task_selection(MANIFEST_PATH, "RC-01,FS-03")
    second = script_run_codex.resolve_task_selection(MANIFEST_PATH, "RC-01,FM-03")

    assert first["scope_sha256"] != second["scope_sha256"]
    assert [stage["stage_id"] for stage in first["stages"]] == ["readcrop", "fix-single"]
    assert [stage["stage_id"] for stage in second["stages"]] == ["readcrop", "fix-multi"]


def test_unified_execution_rejects_stale_aggregate_approval_before_creating_run_dir(
    script_run_codex: Any, tmp_path: Path
) -> None:
    """A stale multi-stage approval cannot create partial artifacts or reach a model."""
    run_dir = tmp_path / "unified-run"
    auth_source = tmp_path / "auth.json"
    auth_source.write_text('{"refresh_token":"fixture"}', encoding="utf-8")

    with pytest.raises(ValueError, match="approval"):
        script_run_codex.cli(
            repo_path=str(tmp_path),
            model=script_run_codex.PARITY_CODEX_MODEL,
            tasks="RC-01,FS-03",
            auth_source=str(auth_source),
            run_dir=str(run_dir),
            paid_approval="stale-aggregate-scope",
        )

    assert not run_dir.exists()


def test_unified_paid_execution_uses_one_counter_across_native_stage_rows(
    script_run_codex: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Paid output must count every native stage row against the complete scope."""
    import _bench_codex.stage_fix as fix_stage
    import _bench_codex.stage_readcrop as readcrop_stage

    stages = [
        {"stage_id": "structural", "task_ids": ["SE-01"], "total_cells": 3, "repetitions": 1},
        {"stage_id": "readcrop", "task_ids": ["RC-01"], "total_cells": 3, "repetitions": 1},
        {"stage_id": "fix-single", "task_ids": ["FS-01"], "total_cells": 3, "repetitions": 1},
        {"stage_id": "fix-multi", "task_ids": ["FM-01"], "total_cells": 3, "repetitions": 1},
    ]
    for number, stage in enumerate(stages, start=1):
        stage["scope_sha256"] = str(number) * 64
    selection = {
        "selection_mode": "all",
        "selectors": [],
        "task_ids": [task_id for stage in stages for task_id in stage["task_ids"]],
        "total_tasks": 4,
        "total_cells": 12,
        "stages": stages,
    }
    scope = {**selection, "scope_sha256": "1234567890123456" + "a" * 48}

    def _emit_stage_rows(stage_id: str) -> None:
        """Emit one native three-arm block through the production renderer."""
        for completed, arm in enumerate(("A_plain", "B_auto", "C_strict"), start=1):
            script_run_codex.runtime.print_arm_row(f"({completed}/3) ✓ {stage_id} {arm}", arm)

    monkeypatch.setattr(script_run_codex, "resolve_task_selection", lambda *_args: selection)
    monkeypatch.setattr(script_run_codex, "_resolve_execution_scope", lambda **_kwargs: scope)
    monkeypatch.setattr(script_run_codex, "main", lambda **_kwargs: _emit_stage_rows("structural"))
    monkeypatch.setattr(readcrop_stage, "run_stage", lambda **_kwargs: _emit_stage_rows("readcrop"))
    monkeypatch.setattr(fix_stage, "run_fix_stage", lambda study, **_kwargs: _emit_stage_rows(study))
    monkeypatch.setattr(script_run_codex, "write_checksums", lambda _path: None)

    script_run_codex._run_unified_execution(
        repo_path=tmp_path,
        model=script_run_codex.PARITY_CODEX_MODEL,
        reasoning_effort=script_run_codex.PARITY_CODEX_REASONING_EFFORT,
        tasks=None,
        manifest_path=MANIFEST_PATH,
        index_path=tmp_path / "index.json",
        marketplace_root=tmp_path,
        codemap_bin=tmp_path / "codemap-py",
        auth_source=tmp_path / "auth.json",
        invocation_launcher_path=None,
        run_dir=tmp_path / "run",
        paid_approval=1234567890123456,
        dry_run=False,
        show_legend=True,
    )

    output = capsys.readouterr().out
    expected_order = [
        "→ aggregate: 4 tasks, 12 cells",
        "== STAGE 1/4: structural (1 tasks, 3 cells) ==",
        "(1/12) ✓ structural A_plain",
        "(3/12) ✓ structural C_strict",
        "== STAGE 2/4: readcrop (1 tasks, 3 cells) ==",
        "(4/12) ✓ readcrop A_plain",
        "(6/12) ✓ readcrop C_strict",
        "== STAGE 3/4: fix-single (1 tasks, 3 cells) ==",
        "(7/12) ✓ fix-single A_plain",
        "(9/12) ✓ fix-single C_strict",
        "== STAGE 4/4: fix-multi (1 tasks, 3 cells) ==",
        "(10/12) ✓ fix-multi A_plain",
        "(12/12) ✓ fix-multi C_strict",
    ]
    positions = [output.index(fragment) for fragment in expected_order]
    assert positions == sorted(positions)
    assert "→ sequential stages: structural=1 tasks/3 cells, readcrop=1 tasks/3 cells" in output
    progress = [re.match(r"^\((\d+)/(\d+)\)", row).groups() for row in output.splitlines() if row.startswith("(")]
    assert progress == [(str(completed), "12") for completed in range(1, 13)]


def test_run_level_relocation_reaches_every_admission_inside_the_runner(
    script_run_codex: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runner built with relocation provenance admits every arm against it.

    Scenario: a run in its own worktree proves its index once, at construction. Admission happens at
    many points inside the runner, and one of them reaching the gate without the provenance would
    demand the byte identity a worktree index cannot have — the failure the flag exists to prevent.
    """
    captured: list[Any] = []
    monkeypatch.setattr(
        script_run_codex,
        "_validate_locked_runtime",
        lambda *args, **kwargs: captured.append(args[5] if len(args) > 5 else kwargs.get("index_relocation")),
    )
    relocation = {"frozen_index_sha256": "a" * 64, "worktree_scan_root": str(tmp_path)}
    index_path = tmp_path / ".cache" / "codemap" / f"{tmp_path.name}.json"
    index_path.parent.mkdir(parents=True)
    index_path.write_text(json.dumps({"scan_root": str(tmp_path), "modules": []}), encoding="utf-8")
    runner = script_run_codex.CodexRunner(
        "gpt-5.6-luna",
        tmp_path,
        index_path=index_path,
        manifest_path=MANIFEST_PATH,
        index_relocation=relocation,
    )

    runner._admit_runtime("A_plain")

    assert captured == [relocation]


def test_index_relocation_provenance_loads_without_its_operator_facing_path(
    script_run_codex: Any,
    tmp_path: Path,
) -> None:
    """The loader hands admission the digests and drops the path written for the operator.

    Scenario: the relocation writer records where it installed the index so a person can find it,
    but admission compares an exact key set. Passing the extra key through would fail every
    relocated run, and accepting a non-object file would silently admit an unverified index.
    """
    provenance = tmp_path / "index-relocation.json"
    provenance.write_text(
        json.dumps({"frozen_index_sha256": "a" * 64, "derived_index_path": "/somewhere/index.json"}),
        encoding="utf-8",
    )

    loaded = script_run_codex.load_index_relocation(provenance)

    assert loaded == {"frozen_index_sha256": "a" * 64}
    assert script_run_codex.load_index_relocation(None) is None
    malformed = tmp_path / "malformed.json"
    malformed.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object of strings"):
        script_run_codex.load_index_relocation(malformed)

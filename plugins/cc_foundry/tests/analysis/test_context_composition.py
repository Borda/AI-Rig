"""Check Claude and Codex context composition against hand-written transcripts."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

import _codex_composition as codex
import _context_composition as composition
import cost_analyzer as analyzer
import pytest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture(autouse=True)
def _isolate_compactions_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep in-process CLI tests away from the user's real compactions log."""
    build_parser = analyzer._build_parser

    def isolated_parser() -> argparse.ArgumentParser:
        """Use a missing temporary log unless a test explicitly supplies one."""
        parser = build_parser()
        parser.set_defaults(compactions_log=str(tmp_path / "missing-compactions.jsonl"))
        return parser

    monkeypatch.setattr(analyzer, "_build_parser", isolated_parser)


def _write_rows(path: Path, rows: list[dict]) -> Path:
    """Write portable JSONL test inputs without platform-dependent newline bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(("\n".join(json.dumps(row) for row in rows) + "\n").encode("utf-8"))
    return path


def _read_fixture_rows(path: Path) -> list[dict]:
    """Load JSONL fixture rows for a scenario that edits them before analysis."""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _call(message_id: str, tokens: int = 100, **message: object) -> dict:
    """Construct one assistant call while keeping scenario-specific content visible."""
    return {
        "type": "assistant",
        "message": {"id": message_id, "model": "claude-opus-5", "usage": {"input_tokens": tokens}, **message},
    }


def test_duplicate_usage_preserves_later_tool_blocks() -> None:
    """Usage counts once while a Read appearing only on the second row remains observable."""
    windows = composition.analyze_transcript(FIXTURES / "composition_duplicate_usage.jsonl", analyzer)
    assert len(windows) == 1
    window = windows[0]
    assert len(window.calls) == 1
    assert window.calls[0].context == 1120
    assert window.calls[0].call.cache_write == 100
    assert len(window.reads) == 2
    assert window.reads[1].change == "unchanged"
    assert window.tool_bytes == 8
    assert window.identical_tool_bytes == 4
    assert window.tool_tokens == 2
    assert "81.8%" in "\n".join(composition._render_window(window, 1, analyzer))


def test_explicit_compaction_overrides_context_drop() -> None:
    """An earlier drop stays in the same window when native boundary evidence exists."""
    windows = composition.analyze_transcript(FIXTURES / "composition_compaction.jsonl", analyzer)
    assert [len(window.calls) for window in windows] == [2, 1]
    assert windows[0].calls[0].context == 100000
    assert windows[0].calls[-1].context == 41000
    assert windows[1].boundary == "compaction record"
    assert windows[1].large_writes == [(3, 60000, "compaction")]


@pytest.mark.parametrize(
    "marker",
    [
        pytest.param({"type": "compact-boundary"}, id="boundary-row"),
        pytest.param({"type": "compact-summary"}, id="summary-row"),
        pytest.param({"type": "system", "subtype": "compact_boundary"}, id="native-boundary"),
        pytest.param({"type": "user", "isCompactSummary": True}, id="native-summary"),
    ],
)
def test_compaction_marker_shapes(tmp_path: Path, marker: dict) -> None:
    """All supported explicit markers split even when context does not shrink."""
    path = _write_rows(tmp_path / "transcript.jsonl", [_call("before"), marker, _call("after", 200)])
    assert [len(window.calls) for window in composition.analyze_transcript(path, analyzer)] == [1, 1]


def test_drop_boundary_is_strictly_below_seventy_percent() -> None:
    """Exactly 70% is not a compaction; the next call just below 70% starts a window."""
    windows = composition.analyze_transcript(FIXTURES / "composition_context_drop.jsonl", analyzer)
    assert [len(window.calls) for window in windows] == [2, 1]
    assert windows[1].calls[0].context == 48999
    assert windows[1].boundary == "70% context drop"


def test_idle_gap_respects_previous_cache_duration() -> None:
    """Five-minute expiry, a warm one-hour gap, and one-hour expiry get distinct labels."""
    windows = composition.analyze_transcript(FIXTURES / "composition_idle_write.jsonl", analyzer)
    triggers = [trigger for _, _, trigger in windows[0].large_writes]
    assert triggers == [
        "other",
        "idle gap beyond the cache TTL (300s)",
        "other",
        "idle gap beyond the cache TTL (3600s)",
    ]
    report = "\n".join(composition._render_session(FIXTURES / "composition_idle_write.jsonl", analyzer))
    assert "5-minute 60,000 tokens; 1-hour 186,000 tokens" in report


def test_attachment_utf8_bytes_and_hook_event_sizes() -> None:
    """Payload bytes exclude wrappers, and tool results do not advance the user prompt."""
    window = composition.analyze_transcript(FIXTURES / "composition_attachments.jsonl", analyzer)[0]
    assert window.attachments == {"instructions": 9, "nested_memory": 6, "hook_additional_context": 15}
    assert window.hooks == {(1, "UserPromptSubmit"): [3, 6], (1, "PreToolUse"): [2], (2, "UserPromptSubmit"): [4]}
    assert window.prompts == {1, 2}
    report = "\n".join(composition._render_window(window, 1, analyzer))
    assert "| **instructions** | 9 |" in report
    assert "| 1 | **all events** | 3 | 11 | - |" in report
    assert "| 1 | UserPromptSubmit | 2 | 9 | 3, 6 |" in report
    assert "| 2 | **all events** | 1 | 4 | - |" in report


def test_native_instruction_and_file_attachment_payloads(tmp_path: Path) -> None:
    """Native file containers count injected content once, excluding paths and raw copies."""
    attachments = [
        {"type": "instructions", "files": [{"path": "guide.md", "type": "project", "content": "café"}]},
        {"type": "nested_memory", "content": {"path": "notes.md", "content": "note", "rawContent": "note"}},
        {"type": "file", "content": {"type": "text", "file": {"filePath": "sample.txt", "content": "abc"}}},
        {"type": "total_tokens_reminder", "text": "reminder"},
    ]
    path = _write_rows(
        tmp_path / "transcript.jsonl",
        [*[{"type": "attachment", "attachment": attachment} for attachment in attachments], _call("request")],
    )
    window = composition.analyze_transcript(path, analyzer)[0]
    assert window.attachments == {"instructions": 5, "nested_memory": 4, "file": 3, "total_tokens_reminder": 8}


@pytest.mark.parametrize("recorded_path", ["folder/example.txt", "C:\\folder\\example.txt"])
def test_read_hashes_changed_unchanged_and_unknown(tmp_path: Path, recorded_path: str) -> None:
    """Read comparison preserves foreign path spelling and reports missing content as unknown."""
    rows = []
    for number, content in enumerate(["alpha", "alpha", "beta", None]):
        tool = f"read-{number}"
        rows.append(
            _call(
                f"call-{number}",
                content=[{"type": "tool_use", "id": tool, "name": "Read", "input": {"file_path": recorded_path}}],
            )
        )
        rows.append(
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tool, "content": content}]}}
        )
    path = _write_rows(tmp_path / "transcript.jsonl", rows)
    window = composition.analyze_transcript(path, analyzer)[0]
    assert [read.change for read in window.reads[1:]] == ["unchanged", "changed", "unknown"]
    assert [read.path for read in window.reads] == [recorded_path] * 4
    assert window.identical_tool_bytes == 5


def test_identical_tool_bytes_reset_after_compaction(tmp_path: Path) -> None:
    """A result seen before compaction cannot inflate duplicate bytes in the next window."""
    result = {"type": "user", "message": {"content": [{"type": "tool_result", "content": "same"}]}}
    path = _write_rows(
        tmp_path / "transcript.jsonl", [_call("before"), result, {"type": "compact-boundary"}, _call("after"), result]
    )
    windows = composition.analyze_transcript(path, analyzer)
    assert [window.tool_bytes for window in windows] == [4, 4]
    assert [window.identical_tool_bytes for window in windows] == [0, 0]


def test_model_change_and_large_write_threshold(tmp_path: Path) -> None:
    """A model change labels a large write, while exactly 50,000 tokens is excluded."""
    rows = [
        _call("initial", usage={"cache_creation_input_tokens": 50000}),
        _call("changed", model="claude-sonnet-5", usage={"cache_creation_input_tokens": 50001}),
    ]
    path = _write_rows(tmp_path / "transcript.jsonl", rows)
    windows = composition.analyze_transcript(path, analyzer)
    assert windows[0].large_writes == [(2, 50001, "model change")]


def test_malformed_rows_warn_and_keep_later_usage(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """A corrupt row is visible on stderr without suppressing a later valid call."""
    path = tmp_path / "transcript.jsonl"
    path.write_bytes(("broken\n[]\n" + json.dumps(_call("valid")) + "\n").encode("utf-8"))
    assert len(composition.analyze_transcript(path, analyzer)[0].calls) == 1
    assert "malformed JSON" in capsys.readouterr().err


@pytest.mark.parametrize(
    "runtime_flags",
    [pytest.param([], id="default-claude"), pytest.param(["--runtime", "claude"], id="explicit-claude")],
)
def test_cli_composition_reuses_prefix_selection_and_output(tmp_path: Path, runtime_flags: list[str]) -> None:
    """The CLI writes composition Markdown through the existing output path and prefix selector."""
    root = tmp_path / "projects"
    transcript = root / "project" / "composition-example.jsonl"
    transcript.parent.mkdir(parents=True)
    shutil.copyfile(FIXTURES / "composition_duplicate_usage.jsonl", transcript)
    output = tmp_path / "reports" / "context.md"
    result = analyzer.main(
        [
            "--composition",
            *runtime_flags,
            "--projects-root",
            str(root),
            "--session-id",
            transcript.stem[:8],
            "--output",
            str(output),
        ]
    )
    assert result == 0
    report = output.read_text(encoding="utf-8")
    assert report.startswith("## Context composition\n")
    assert "| 1,120 | 1,120 | 1 | 81.8% |" in report
    assert "5-minute 100 tokens; 1-hour 0 tokens" in report
    assert report.endswith("\n")


def test_cli_since_mode_keeps_subagent_contexts_separate(tmp_path: Path) -> None:
    """Window discovery includes recent activity without blending agent context trajectories."""
    root = tmp_path / "projects"
    main = _write_rows(
        root / "project" / "composition-example.jsonl", [{**_call("main", 100), "timestamp": "2030-01-01T00:00:00Z"}]
    )
    _write_rows(
        main.parent / main.stem / "subagents" / "agent-example.jsonl",
        [{**_call("agent", 10), "timestamp": "2030-01-01T00:00:00Z"}],
    )
    output = tmp_path / "context.md"
    assert (
        analyzer.main(["--composition", "--projects-root", str(root), "--since", "24h", "--output", str(output)]) == 0
    )
    report = output.read_text(encoding="utf-8")
    assert report.count("#### Context window 1") == 2
    assert "#### Transcript: main" in report
    assert "#### Transcript: subagent agent-example" in report


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        pytest.param(["--composition"], "#### Transcript: main", id="composition-mode"),
        pytest.param([], "Window: last 24h, 1 session(s) with usage.", id="cost-mode"),
    ],
)
@pytest.mark.parametrize(
    "bad",
    [
        pytest.param([1, 2], id="list-row"),
        pytest.param({"type": "assistant", "message": "wrong"}, id="string-message"),
        pytest.param({"type": "assistant", "message": {"id": "bad", "usage": [1]}}, id="list-usage"),
        pytest.param({"type": "assistant", "timestamp": 5, "message": {"id": "bad", "content": 7}}, id="int-fields"),
        pytest.param({"type": "assistant", "message": {"id": ["bad"], "usage": {"input_tokens": 1}}}, id="list-id"),
        pytest.param({"type": "assistant", "message": {"usage": {"input_tokens": "1"}}}, id="string-counter"),
    ],
)
def test_cli_since_selection_skips_malformed_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture, flags: list[str], expected: str, bad: object
) -> None:
    """Window discovery skips malformed rows and still reports the session's valid usage.

    Session selection parses every recent transcript before composition analysis runs, so one valid-JSON row with the
    wrong shape must not abort the whole --since report in either report mode.
    """
    root = tmp_path / "projects"
    rows = [bad, {**_call("main", 100), "timestamp": "2030-01-01T00:00:00Z"}]
    _write_rows(root / "project" / "composition-example.jsonl", rows)
    output = tmp_path / "report.md"
    assert analyzer.main([*flags, "--projects-root", str(root), "--since", "24h", "--output", str(output)]) == 0
    assert expected in output.read_text(encoding="utf-8")
    assert "composition-example.jsonl:1; skipped" in capsys.readouterr().err


@pytest.mark.parametrize(
    "flags", [pytest.param(["--composition"], id="composition-mode"), pytest.param([], id="cost-mode")]
)
def test_codex_runtime_entrypoint_writes_tokens(tmp_path: Path, flags: list[str]) -> None:
    """Codex selects token-only composition through the actual script entrypoint."""
    output = tmp_path / "context.md"
    result = subprocess.run(
        [
            sys.executable,
            str(Path(analyzer.__file__)),
            *flags,
            "--runtime",
            "codex",
            "--projects-root",
            str(FIXTURES / "composition_codex_parent.jsonl"),
            "--output",
            str(output),
        ],
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = output.read_text(encoding="utf-8")
    assert "Runtime: Codex (tokens only)" in report
    assert "| 100,010 | 40,000 | 2 | 71.4% |" in report
    assert "Cache-write" not in report
    assert "$" not in report


def test_actual_claude_entrypoint_imports_and_writes(tmp_path: Path) -> None:
    """The private-module import works when cost_analyzer runs as __main__."""
    output = tmp_path / "context.md"
    result = subprocess.run(
        [
            sys.executable,
            str(Path(analyzer.__file__)),
            "--composition",
            "--runtime",
            "claude",
            "--compactions-log",
            str(tmp_path / "missing-compactions.jsonl"),
            "--projects-root",
            str(FIXTURES / "composition_duplicate_usage.jsonl"),
            "--output",
            str(output),
        ],
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8").startswith("## Context composition")


def test_missing_compactions_log_preserves_session_report(tmp_path: Path) -> None:
    """A missing log adds only a note to the existing Claude session measurements."""
    path = FIXTURES / "composition_compactions_example.jsonl"
    output = tmp_path / "context.md"
    assert analyzer.main(["--composition", "--projects-root", str(path), "--output", str(output)]) == 0
    report = output.read_text(encoding="utf-8")
    assert report.count("no logged compaction events") == 1
    assert "Logged compaction events" not in report
    windows = composition.analyze_transcript(path, analyzer)
    expected = [
        f"### Session `{path.stem}`",
        "",
        "Cache-write split (main + subagents): 5-minute 0 tokens; 1-hour 0 tokens.",
        "",
        "no logged compaction events.",
        "",
        "#### Transcript: main",
        "",
    ]
    for number, window in enumerate(windows, 1):
        expected += composition._render_window(window, number, analyzer)
    assert "\n".join(expected) in report


def test_logged_compactions_render_and_compare_fixture_windows(tmp_path: Path) -> None:
    """Session-matched events sort by instants and compare each gate with its next compaction."""
    output = tmp_path / "context.md"
    assert (
        analyzer.main(
            [
                "--composition",
                "--projects-root",
                str(FIXTURES / "composition_compactions_example.jsonl"),
                "--compactions-log",
                str(FIXTURES / "composition_compactions_events.jsonl"),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    report = output.read_text(encoding="utf-8")
    assert "Logged compaction events" in report
    expected = [
        "| 2030-01-01T01:00:01+01:00 | gate |  | compact | saving exceeds rebuild cost | 200000 | 4 | 240001 |  |  |",
        "| 2030-01-01T00:00:02.000Z | precompact | manual |  |  |  |  |  |  | true |",
        "| 2030-01-01T00:00:04.000Z | postcompact | manual |  |  |  |  |  | 3200 |  |",
        "| 2030-01-01T00:00:07+00:00 | gate |  | continue | saving does not exceed rebuild cost | 126000 | 3 | 240000 |  |  |",
        "| 2030-01-01T00:00:08.000Z | precompact | auto |  |  |  |  |  |  | false |",
        "| 2030-01-01T00:00:12+00:00 | gate |  | continue | no contract |  |  |  |  |  |",
    ]
    positions = [report.index(row) for row in expected]
    assert positions == sorted(positions)
    assert "another-session" not in report
    assert "future-event" not in report
    assert "calls 4 vs 2" in report
    assert "calls 3 vs 1" in report
    assert "120,000 tokens (gate floor assumption) vs 125,000 tokens (post-call)" in report
    assert "120,000 tokens (gate floor assumption) vs 118,000 tokens (post-call)" in report
    assert report.count("estimate vs realised") == 3
    assert "no compaction followed" in report


@pytest.mark.parametrize("boundary", ["compaction record", "70% context drop"])
def test_gate_matches_logged_compaction_without_blending_subagents(tmp_path: Path, boundary: str) -> None:
    """Multiple gates can precede one compaction, whose realised calls belong only to the main window."""
    path = tmp_path / "composition_compactions_example.jsonl"
    shutil.copyfile(FIXTURES / path.name, path)
    if boundary == "70% context drop":
        rows = _read_fixture_rows(path)
        rows[-1]["message"]["usage"]["input_tokens"] = 79000
        _write_rows(path, [row for row in rows if row["type"] == "assistant"])
    _write_rows(path.parent / path.stem / "subagents" / "agent-example.jsonl", [_call("agent", 500)])
    events = composition._read_compactions(FIXTURES / "composition_compactions_events.jsonl", analyzer)[path.stem]
    events.insert(1, {**events[0], "ts": "2030-01-01T00:00:01.500Z", "calls_ahead": 0})
    report = "\n".join(composition._render_session(path, analyzer, events))
    assert "calls 4 vs 2" in report
    assert "calls 0 vs 2" in report
    assert "main window 2" in report
    assert f"Context window 2 ({boundary})" in report
    assert "#### Transcript: subagent agent-example" in report


def test_logged_compaction_with_missing_call_timing_reports_unavailable(tmp_path: Path) -> None:
    """Compaction logs alone cannot assign a realised window when transcript timestamps are absent."""
    path = _write_rows(
        tmp_path / "composition_compactions_example.jsonl",
        [_call("before"), {"type": "compact-boundary"}, _call("after")],
    )
    events = composition._read_compactions(FIXTURES / "composition_compactions_events.jsonl", analyzer)[path.stem]
    report = "\n".join(composition._render_session(path, analyzer, events))
    assert report.count("realised window unavailable") == 2
    assert report.count("no compaction followed") == 1
    assert "calls 4 vs 1" not in report


def test_codex_composition_ignores_claude_compactions_log(tmp_path: Path) -> None:
    """Codex output stays identical even when the Claude log path cannot be read as a file."""
    output = tmp_path / "context.md"
    flags = [
        "--runtime",
        "codex",
        "--projects-root",
        str(FIXTURES / "composition_codex_parent.jsonl"),
        "--output",
        str(output),
    ]
    assert analyzer.main(flags) == 0
    expected = output.read_bytes()
    assert analyzer.main([*flags, "--compactions-log", str(tmp_path)]) == 0
    assert output.read_bytes() == expected
    assert b"Logged compaction events" not in expected
    assert b"no logged compaction events" not in expected


def test_malformed_compaction_events_warn_and_keep_valid_rows(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """Malformed JSON, non-objects and invalid timestamps cannot hide valid later events."""
    log = tmp_path / "compactions.jsonl"
    valid = {
        "event": "gate",
        "session_id": "composition_compactions_example",
        "ts": "2030-01-01T00:00:12Z",
        "decision": "continue",
        "reason": "literal | reason\nnext line",
    }
    log.write_text(
        "broken JSON\n[]\n" + json.dumps({**valid, "ts": 42}) + "\n" + json.dumps(valid) + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "context.md"
    assert (
        analyzer.main(
            [
                "--composition",
                "--projects-root",
                str(FIXTURES / "composition_compactions_example.jsonl"),
                "--compactions-log",
                str(log),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    warning = capsys.readouterr().err
    assert warning.count("warning:") == 3
    assert f"{log}:1" in warning
    assert f"{log}:2" in warning
    assert f"{log}:3" in warning
    report = output.read_text(encoding="utf-8")
    assert "literal &#124; reason next line" in report
    assert "no compaction followed" in report


def test_missing_and_empty_sessions_do_not_write(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """Missing selectors and transcripts without usage preserve existing failure semantics."""
    root = tmp_path / "projects"
    output = tmp_path / "context.md"
    flags = ["--composition", "--projects-root", str(root), "--output", str(output)]
    assert analyzer.main([*flags, "--session-id", "absent"]) == 1
    assert "no session matching" in capsys.readouterr().err
    transcript = _write_rows(root / "project" / "empty.jsonl", [{"type": "user"}])
    assert analyzer.main([*flags, "--session-id", transcript.stem]) == 1
    assert "no sessions with usage" in capsys.readouterr().err
    assert not output.exists()


def test_codex_response_dedupe_windows_and_injections() -> None:
    """Response ids exclude mirrored usage while explicit compaction preserves the earlier drop."""
    thread = codex.analyze_transcript(FIXTURES / "composition_codex_parent.jsonl", analyzer)
    assert thread.session_id == "example-parent"
    assert thread.edited_code
    assert [len(window.calls) for window in thread.windows] == [2, 2]
    first, second = thread.windows
    assert [item.context for item in first.calls] == [100010, 40000]
    assert first.attachments == {
        "instructions": 5,
        "skills_instructions": 5,
        "model_switch": 5,
        "multi_agent_role": 4,
        "multi_agent_mode": 4,
        "hook_developer": 4,
        "other": 4,
    }
    assert (first.tool_bytes, first.tool_tokens, first.identical_tool_bytes) == (8, 2, 4)
    assert (second.tool_bytes, second.identical_tool_bytes) == (4, 0)
    assert second.boundary == "compaction record"
    assert second.large_writes == [(3, 60000, "compaction"), (4, 60001, "model change")]
    assert first.calls[0].call.cache_read == 80000
    assert analyzer.prompt_tokens(first.calls[0].call.usage) == 100000


def test_codex_drop_and_legacy_event_dedupe() -> None:
    """The strict drop fallback and adjacent cumulative keys preserve calls after a reset."""
    thread = codex.analyze_transcript(FIXTURES / "composition_codex_drop.jsonl", analyzer)
    assert [len(window.calls) for window in thread.windows] == [2, 1]
    assert thread.windows[1].boundary == "70% context drop"
    legacy = codex.analyze_transcript(FIXTURES / "composition_codex_legacy.jsonl", analyzer)
    assert sum(len(window.calls) for window in legacy.windows) == 3
    assert [item.context for item in legacy.windows[0].calls] == [110, 110, 110]


def test_codex_cli_family_prefix_and_subagent_share(tmp_path: Path) -> None:
    """Metadata prefixes select linked rollouts without merging parent and child windows."""
    root = tmp_path / "sessions"
    for name in ("parent", "child"):
        destination = root / "2030" / "01" / "01" / f"rollout-{name}.jsonl"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FIXTURES / f"composition_codex_{name}.jsonl", destination)
    output = tmp_path / "context.md"
    assert (
        analyzer.main(
            [
                "--composition",
                "--runtime",
                "codex",
                "--projects-root",
                str(root),
                "--session-id",
                "example-par",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    report = output.read_text(encoding="utf-8")
    assert report.count("#### Context window") == 3
    assert "Subagent share: 1/5 calls (20.0%); 10,010/270,041 tokens (3.7%)" in report
    assert "AGENTS.md bytes × calls" in report
    assert "| main | yes (attempt observed) | 5 | 4 | 20 |" in report
    assert "| subagent example-child | no edit observed | 4 | 1 | 4 |" in report
    assert (
        analyzer.main(
            [
                "--composition",
                "--runtime",
                "codex",
                "--projects-root",
                str(root),
                "--session-id",
                "example-child",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert output.read_text(encoding="utf-8") == report


def test_codex_default_home_since_and_help(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CODEX_HOME supplies discovery while recorded timestamps control recent-family selection."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    rollout = tmp_path / "sessions" / "2030" / "01" / "01" / "rollout-example.jsonl"
    rollout.parent.mkdir(parents=True)
    shutil.copyfile(FIXTURES / "composition_codex_drop.jsonl", rollout)
    output = tmp_path / "context.md"
    assert analyzer.main(["--composition", "--runtime", "codex", "--since", "2d", "--output", str(output)]) == 0
    assert output.read_text(encoding="utf-8").count("#### Context window") == 2
    help_text = analyzer._build_parser().format_help()
    assert "token-only" in help_text
    assert "Claude foundry plugin" in help_text


def test_codex_empty_usage_and_missing_selector(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """Missing Codex selectors and cumulative-only events cannot create misleading reports."""
    root = tmp_path / "sessions"
    _write_rows(
        root / "rollout-empty.jsonl",
        [
            {"type": "session_meta", "payload": {"id": "empty-example"}},
            {
                "type": "event_msg",
                "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 100}}},
            },
        ],
    )
    output = tmp_path / "context.md"
    flags = ["--runtime", "codex", "--composition", "--projects-root", str(root), "--output", str(output)]
    assert analyzer.main([*flags, "--session-id", "absent"]) == 1
    assert "no session matching" in capsys.readouterr().err
    assert analyzer.main(flags) == 1
    assert "no sessions with usage" in capsys.readouterr().err
    assert not output.exists()


@pytest.mark.parametrize("root_flag", ["--projects-root", "--projects-ro", "--projects-root=", "--projects-ro="])
def test_codex_explicit_root_overrides_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_flag: str) -> None:
    """Accepted explicit-root spellings retain argparse behavior and override CODEX_HOME."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "absent"))
    root = FIXTURES / "composition_codex_drop.jsonl"
    root_args = [root_flag + str(root)] if root_flag.endswith("=") else [root_flag, str(root)]
    output = tmp_path / "context.md"
    assert analyzer.main(["--composition", "--runtime", "codex", *root_args, "--output", str(output)]) == 0
    assert output.read_text(encoding="utf-8").count("#### Context window") == 2


@pytest.mark.parametrize(
    ("payload", "edited"),
    [
        pytest.param({"name": "apply_patch", "input": "patch"}, True, id="custom-patch"),
        pytest.param({"name": "exec", "input": 'await tools.apply_patch("patch")'}, True, id="wrapped-patch"),
        pytest.param(
            {"name": "exec_command", "arguments": '{"cmd": "echo value > sample.py"}'}, True, id="shell-code-write"
        ),
        pytest.param(
            {"name": "exec_command", "arguments": '{"cmd": "Set-Content sample.py value"}'},
            True,
            id="powershell-code-write",
        ),
        pytest.param({"name": "write_file", "arguments": '{"path": "sample.ts"}'}, True, id="native-file-write"),
        pytest.param({"name": "exec_command", "arguments": '{"cmd": "cat sample.py"}'}, False, id="read-code"),
        pytest.param(
            {"name": "exec_command", "arguments": '{"cmd": "echo value > notes.md"}'}, False, id="write-prose"
        ),
        pytest.param(
            {"name": "spawn_agent", "arguments": '{"message": "echo value > sample.py"}'},
            False,
            id="delegation-is-not-write",
        ),
    ],
)
def test_codex_code_edit_evidence(tmp_path: Path, payload: dict, edited: bool) -> None:
    """Edit flags follow actual patch or code-write call evidence rather than filename references."""
    rows = _read_fixture_rows(FIXTURES / "composition_codex_drop.jsonl")
    rows.insert(1, {"type": "response_item", "payload": {"type": "function_call", **payload}})
    path = _write_rows(tmp_path / "rollout-example.jsonl", rows)
    assert codex.analyze_transcript(path, analyzer).edited_code is edited


@pytest.mark.parametrize(
    "invalid",
    [
        pytest.param({"input_tokens": -1, "output_tokens": 0}, id="negative-input"),
        pytest.param({"input_tokens": True, "output_tokens": 0}, id="boolean-counter"),
        pytest.param({"input_tokens": 5, "cached_input_tokens": 6, "output_tokens": 0}, id="cache-exceeds-input"),
        pytest.param(
            {"input_tokens": 5, "output_tokens": 0, "reasoning_output_tokens": 1}, id="reasoning-exceeds-output"
        ),
        pytest.param({"input_tokens": 10**309, "output_tokens": 0}, id="oversized-counter"),
    ],
)
def test_codex_invalid_usage_warns_and_keeps_valid_calls(
    tmp_path: Path, invalid: dict, capsys: pytest.CaptureFixture
) -> None:
    """Corrupt counters cannot affect context totals or suppress later valid responses."""
    rows = _read_fixture_rows(FIXTURES / "composition_codex_drop.jsonl")
    rows.insert(2, {"type": "token_usage_record", "payload": {"response_id": "invalid-example", "usage": invalid}})
    thread = codex.analyze_transcript(_write_rows(tmp_path / "rollout-example.jsonl", rows), analyzer)
    assert len(thread.calls) == 3
    assert "valid last-response counters" in capsys.readouterr().err


def test_codex_missing_parent_preserves_sibling_family(tmp_path: Path) -> None:
    """Children linked to an absent parent still form one family, selected through either child."""
    root = tmp_path / "sessions"
    for name in ("alpha-child", "beta-child"):
        rows = _read_fixture_rows(FIXTURES / "composition_codex_child.jsonl")
        rows[0]["payload"]["id"] = name
        _write_rows(root / f"rollout-{name}.jsonl", rows)
    output = tmp_path / "context.md"
    assert (
        analyzer.main(
            [
                "--composition",
                "--runtime",
                "codex",
                "--projects-root",
                str(root),
                "--session-id",
                "alpha",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    report = output.read_text(encoding="utf-8")
    assert "Sessions found: 1." in report
    assert report.count("#### Context window") == 2
    assert "Subagent share: 2/2 calls (100.0%); 20,020/20,020 tokens (100.0%)" in report


def test_codex_ambiguous_prefix_and_duplicate_identity_fail(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """Unrelated prefix matches and duplicate thread identities never silently double-count usage."""
    root = tmp_path / "sessions"
    rows = _read_fixture_rows(FIXTURES / "composition_codex_drop.jsonl")
    _write_rows(root / "rollout-alpha.jsonl", rows)
    rows[0]["payload"]["id"] = "drop-other"
    _write_rows(root / "rollout-beta.jsonl", rows)
    output = tmp_path / "context.md"
    flags = ["--composition", "--runtime", "codex", "--projects-root", str(root), "--output", str(output)]
    assert analyzer.main([*flags, "--session-id", "drop-"]) == 1
    assert "ambiguous Codex session prefix" in capsys.readouterr().err
    _write_rows(root / "rollout-duplicate.jsonl", rows)
    assert analyzer.main(flags) == 1
    assert "duplicate Codex thread identity" in capsys.readouterr().err
    assert not output.exists()


@pytest.mark.parametrize("runtime", ["claude", "codex"])
@pytest.mark.parametrize(
    "bad",
    [
        pytest.param({"type": ["assistant"]}, id="list-type"),
        pytest.param({"type": "system", "subtype": ["compact_boundary"]}, id="list-subtype"),
        pytest.param({"type": "assistant", "message": "wrong"}, id="string-message"),
        pytest.param({"type": "assistant", "message": None}, id="null-message"),
        pytest.param({"type": "turn_context", "payload": "wrong"}, id="string-payload"),
        pytest.param({"type": "turn_context", "payload": None}, id="null-payload"),
        pytest.param({"type": "event_msg", "payload": {"type": "token_count", "info": "wrong"}}, id="string-info"),
        pytest.param({"type": "session_meta", "payload": {"source": {"subagent": 7}}}, id="int-subagent"),
        pytest.param(
            {"type": "session_meta", "payload": {"source": {"subagent": {"thread_spawn": None}}}}, id="null-spawn"
        ),
        pytest.param({"type": "assistant", "message": {"usage": "wrong"}}, id="string-usage"),
        pytest.param({"type": "assistant", "message": {"id": ["x"], "usage": {"input_tokens": 1}}}, id="list-id"),
        pytest.param({"type": "assistant", "message": {"usage": {"input_tokens": "1"}}}, id="string-counter"),
        pytest.param(
            {"type": "assistant", "message": {"usage": {"input_tokens": 1, "cache_creation": []}}}, id="list-split"
        ),
        pytest.param({"type": "assistant", "timestamp": 5}, id="int-timestamp"),
        pytest.param(
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": ["x"], "content": "x"}]}},
            id="list-tool-result-id",
        ),
        pytest.param({"type": "attachment", "attachment": {"type": None, "content": "x"}}, id="null-attachment-type"),
        pytest.param(
            {
                "type": "session_meta",
                "payload": {"source": {"subagent": {"thread_spawn": {"parent_thread_id": ["x"]}}}},
            },
            id="list-parent-id",
        ),
        pytest.param({"type": "response_item", "payload": {"type": "message", "role": ["user"]}}, id="list-role"),
        pytest.param({"type": "turn_context", "payload": {"model": ["gpt"]}}, id="list-model"),
        pytest.param({"type": "assistant", "message": {"usage": {"input_tokens": 10**309}}}, id="oversized-counter"),
        pytest.param(
            {
                "type": "user",
                "message": {"content": [{"type": "tool_use", "id": "bad", "name": "Read", "input": "wrong"}]},
            },
            id="string-tool-input",
        ),
    ],
)
def test_malformed_row_shapes_warn_and_preserve_later_usage(tmp_path: Path, capsys, runtime: str, bad: dict) -> None:
    """Valid JSON with invalid nested shapes warns at its original line and cannot hide later usage."""
    valid = (
        _call("valid")
        if runtime == "claude"
        else {
            "type": "token_usage_record",
            "payload": {"response_id": "valid", "usage": {"input_tokens": 100, "output_tokens": 0}},
        }
    )
    path = _write_rows(tmp_path / "transcript.jsonl", [bad, valid])
    result = (
        composition.analyze_transcript(path, analyzer)
        if runtime == "claude"
        else codex.analyze_transcript(path, analyzer).windows
    )
    assert len(result) == 1
    assert len(result[0].calls) == 1
    assert result[0].calls[0].context == 100
    warning = capsys.readouterr().err
    assert "warning:" in warning
    assert f"{path}:1" in warning
    assert "skipped" in warning


@pytest.mark.parametrize("kind", ["review", "compact", "memory_consolidation"])
def test_codex_string_subagent_keeps_identity(tmp_path: Path, kind: str) -> None:
    """Plain subagent kinds recorded as strings keep the native session identity.

    Codex serializes thread spawns as objects but other subagent kinds as bare strings; rejecting the string form
    dropped the session_meta row and fell back to the rollout filename, breaking selection by session id.
    """
    rows = [
        {"type": "session_meta", "payload": {"id": "native-session", "source": {"subagent": kind}}},
        {
            "type": "token_usage_record",
            "payload": {"response_id": "r1", "usage": {"input_tokens": 9, "output_tokens": 1}},
        },
    ]
    thread = codex.analyze_transcript(_write_rows(tmp_path / "rollout-example.jsonl", rows), analyzer)
    assert (thread.session_id, thread.parent_id, len(thread.calls)) == ("native-session", None, 1)


def test_null_cache_counters_keep_call(tmp_path: Path) -> None:
    """Null cache counters, valid in the Messages API schema, count as zero instead of discarding the call."""
    usage = {
        "input_tokens": 100,
        "output_tokens": 10,
        "cache_read_input_tokens": None,
        "cache_creation_input_tokens": None,
    }
    row = {"type": "assistant", "message": {"id": "nullable", "model": "claude-opus-5-5", "usage": usage}}
    path = _write_rows(tmp_path / "transcript.jsonl", [row])
    windows = composition.analyze_transcript(path, analyzer)
    calls, _, _, _ = analyzer._parse_rows(path)
    assert (windows[0].calls[0].context, list(calls)) == (110, ["nullable"])


def test_deeply_nested_payload_is_measured() -> None:
    """Payload extraction walks arbitrarily deep nesting without exhausting the interpreter stack.

    The JSON decoder's own depth limit differs by Python version, so the walk is exercised on a decoded value.
    """
    content: object = "x"
    for _ in range(5_000):
        content = [content]
    assert composition._payload_bytes({"type": "instructions", "content": content}) == b"x"


@pytest.mark.parametrize(
    "line",
    [
        pytest.param("[" * 200_000 + "]" * 200_000, id="nesting-past-recursion-limit"),
        pytest.param(
            '{"type": "assistant", "message": {"usage": {"input_tokens": ' + "9" * 5_000 + "}}}", id="huge-integer"
        ),
    ],
)
def test_undecodable_json_line_is_skipped(tmp_path: Path, capsys: pytest.CaptureFixture, line: str) -> None:
    """A row the decoder rejects past its limits is skipped with a warning; later usage survives.

    Deep nesting raises RecursionError and an integer past the digit limit raises a plain ValueError, neither of which
    is a JSONDecodeError; on interpreters without the digit limit the counter bound rejects the row instead.
    """
    path = tmp_path / "transcript.jsonl"
    path.write_bytes((line + "\n" + json.dumps(_call("main")) + "\n").encode("utf-8"))
    windows = composition.analyze_transcript(path, analyzer)
    calls, _, _, _ = analyzer._parse_rows(path)
    assert (len(windows[0].calls), list(calls)) == (1, ["main"])
    assert f"{path}:1; skipped" in capsys.readouterr().err


@pytest.mark.parametrize(
    "line",
    [
        pytest.param("[" * 200_000 + "]" * 200_000, id="nesting-past-recursion-limit"),
        pytest.param('{"event": "gate", "ctx": ' + "9" * 5_000 + "}", id="huge-integer"),
        pytest.param(
            '{"event": "gate", "ts": "2030-01-01T00:00:00Z", "session_id": "other", "K": ' + "9" * 400 + "}",
            id="overflowing-k",
        ),
    ],
)
def test_undecodable_compaction_event_is_skipped(tmp_path: Path, capsys: pytest.CaptureFixture, line: str) -> None:
    """An undecodable compaction-log line warns and leaves later valid events grouped by session."""
    event = {"event": "gate", "ts": "2030-01-01T00:00:00Z", "session_id": "sample-session"}
    path = tmp_path / "compactions.jsonl"
    path.write_bytes((line + "\n" + json.dumps(event) + "\n").encode("utf-8"))
    assert list(composition._read_compactions(path, analyzer)) == ["sample-session"]
    assert "malformed compaction event" in capsys.readouterr().err


def test_codex_undecodable_tool_arguments_do_not_abort() -> None:
    """Tool arguments nested past the decoder limit fall back to text matching instead of raising."""
    payload = {"type": "function_call", "name": "exec_command", "arguments": "[" * 200_000 + "]" * 200_000}
    assert codex._is_code_edit(payload) is False


def test_skipped_row_leaves_no_partial_state(tmp_path: Path) -> None:
    """A rejected row contributes neither its call id nor its tool bytes to later accounting.

    A row that failed midway once kept its message id, so a later valid row with that id was dropped as a duplicate, and
    kept tool-result bytes despite its skipped warning.
    """
    rows = [
        {**_call("repeat", 500), "timestamp": 5},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": ["x"], "content": "lost"}]}},
        _call("repeat", 500),
        _call("other", 500),
    ]
    windows = composition.analyze_transcript(_write_rows(tmp_path / "transcript.jsonl", rows), analyzer)
    assert [item.call.message_id for item in windows[0].calls] == ["repeat", "other"]
    assert windows[0].tool_bytes == 0


def test_surrogate_attachment_label_still_writes_report(tmp_path: Path) -> None:
    """Transcript-controlled labels with lone surrogates cannot abort report writing.

    JSON escapes admit unpaired surrogates; the label lands in a table cell and once raised UnicodeEncodeError.
    """
    root = tmp_path / "projects"
    rows = [{"type": "attachment", "attachment": {"type": "bad\ud800", "content": "text"}}, _call("main", 100)]
    _write_rows(root / "project" / "composition-example.jsonl", rows)
    output = tmp_path / "report.md"
    assert (
        analyzer.main(
            [
                "--composition",
                "--projects-root",
                str(root),
                "--session-id",
                "composition-example",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert "| bad? |" in output.read_text(encoding="utf-8")


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_lone_surrogates_preserve_valid_multibyte_accounting(tmp_path: Path, runtime: str) -> None:
    """Escaped invalid Unicode is replaced deterministically while genuine Unicode keeps UTF-8 byte counts."""
    text = "café🦊\ud800"
    if runtime == "claude":
        rows = [{"type": "attachment", "attachment": {"type": "instructions", "content": text}}, _call("valid")]
    else:
        rows = [
            {"type": "response_item", "payload": {"type": "message", "role": "developer", "content": text}},
            {
                "type": "token_usage_record",
                "payload": {"response_id": "valid", "usage": {"input_tokens": 100, "output_tokens": 0}},
            },
        ]
    path = _write_rows(tmp_path / "transcript.jsonl", rows)
    result = (
        composition.analyze_transcript(path, analyzer)
        if runtime == "claude"
        else codex.analyze_transcript(path, analyzer).windows
    )
    assert len(result) == 1
    assert result[0].attachments == {"instructions" if runtime == "claude" else "other": 10}
    assert result[0].calls[0].context == 100

"""Context-composition reports for Claude and Codex transcripts, checked against small hand-written sessions.

Covers window splitting (explicit compaction records versus the 70% context drop), attachment and tool-byte accounting,
the logged-compaction comparison, Codex session selection, and how malformed or undecodable rows are skipped without
hiding later usage. The two script-entrypoint tests run ``cost_analyzer.py`` in a real subprocess because the entrypoint
import path is the contract there; every other test calls ``cost_analyzer.main`` in-process.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import _codex_composition as codex
import _context_composition as composition
import cost_analyzer as analyzer
import pytest

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
_COMPACTIONS_TRANSCRIPT = FIXTURES / "composition_compactions_example.jsonl"
_COMPACTIONS_EVENTS = FIXTURES / "composition_compactions_events.jsonl"
_CODEX_FLAGS = ("--composition", "--runtime", "codex")
#: A row nested past the JSON decoder's recursion limit; it raises ``RecursionError`` rather than ``JSONDecodeError``.
_TOO_DEEP = "[" * 200_000 + "]" * 200_000
#: Rows shown to ``--composition`` in every rendered ``Logged compaction events`` table, in expected order.
_LOGGED_EVENT_ROWS = (
    "| 2030-01-01T01:00:01+01:00 | gate |  | compact | saving exceeds rebuild cost | 200000 | 4 | 240001 |  |  |",
    "| 2030-01-01T00:00:02.000Z | precompact | manual |  |  |  |  |  |  | true |",
    "| 2030-01-01T00:00:04.000Z | postcompact | manual |  |  |  |  |  | 3200 |  |",
    "| 2030-01-01T00:00:07+00:00 | gate |  | continue | saving does not exceed rebuild cost | 126000 | 3 | 240000 |  |  |",
    "| 2030-01-01T00:00:08.000Z | precompact | auto |  |  |  |  |  |  | false |",
    "| 2030-01-01T00:00:12+00:00 | gate |  | continue | no contract |  |  |  |  |  |",
)


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


@pytest.fixture
def output(tmp_path: Path) -> Path:
    """Report path every in-process CLI test writes to."""
    return tmp_path / "context.md"


@pytest.fixture
def run_report(output: Path) -> Callable[..., str]:
    """Run ``cost_analyzer.main`` with ``--output`` set, require exit 0, and return the report text."""

    def run(*args: str) -> str:
        """Run the CLI with ``args`` and return the written report."""
        assert analyzer.main([*args, "--output", str(output)]) == 0
        return output.read_text(encoding="utf-8")

    return run


@pytest.fixture
def logged_report(run_report: Callable[..., str]) -> str:
    """Report for the fixture session joined with its logged compaction events."""
    return run_report(
        "--composition",
        *("--session-id", "composition_compactions_example"),
        *("--projects-root", str(_COMPACTIONS_TRANSCRIPT)),
        *("--compactions-log", str(_COMPACTIONS_EVENTS)),
    )


@pytest.fixture
def codex_family_root(tmp_path: Path) -> Path:
    """Sessions tree holding the parent and child rollouts of one Codex thread family."""
    root = tmp_path / "sessions"
    for name in ("parent", "child"):
        destination = root / "2030" / "01" / "01" / f"rollout-{name}.jsonl"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FIXTURES / f"composition_codex_{name}.jsonl", destination)
    return root


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


def _dated_call(message_id: str, tokens: int = 100) -> dict:
    """Construct an assistant call stamped inside any ``--since`` window."""
    return {**_call(message_id, tokens), "timestamp": "2030-01-01T00:00:00Z"}


def _session_root(tmp_path: Path, rows: list[dict]) -> Path:
    """Write ``rows`` as one Claude session under a projects root and return that root."""
    root = tmp_path / "projects"
    _write_rows(root / "project" / "composition-example.jsonl", rows)
    return root


def _attachment_rows(attachments: list[dict], *rest: dict) -> list[dict]:
    """Wrap each attachment payload in a transcript row and append ``rest``."""
    return [*({"type": "attachment", "attachment": attachment} for attachment in attachments), *rest]


def _read_rows(recorded_path: str, contents: list[str | None]) -> list[dict]:
    """Build one Read call plus its result per entry of ``contents``; ``None`` means the result carries no content."""
    rows = []
    for number, content in enumerate(contents):
        tool = f"read-{number}"
        use = {"type": "tool_use", "id": tool, "name": "Read", "input": {"file_path": recorded_path}}
        result = {"type": "tool_result", "tool_use_id": tool, "content": content}
        rows += [_call(f"call-{number}", content=[use]), {"type": "user", "message": {"content": [result]}}]
    return rows


def _nest(depth: int) -> object:
    """Return the string ``"x"`` wrapped in ``depth`` single-element lists."""
    value: object = "x"
    for _ in range(depth):
        value = [value]
    return value


def _in_order(report: str, rows: tuple[str, ...]) -> bool:
    """Report whether every row occurs in ``report`` and the rows appear in the order given."""
    positions = [report.index(row) for row in rows]
    return positions == sorted(positions)


def _render_windows(path: Path) -> list[str]:
    """Render every window of a Claude transcript the way the session report does."""
    lines: list[str] = []
    for number, window in enumerate(composition.analyze_transcript(path, analyzer), 1):
        lines += composition._render_window(window, number, analyzer)
    return lines


def _write_rollouts(root: Path, template: str, thread_ids: dict[str, str]) -> None:
    """Write one ``rollout-<name>.jsonl`` per entry from the ``template`` fixture, with that entry's thread id."""
    for name, thread_id in thread_ids.items():
        rows = _read_fixture_rows(FIXTURES / template)
        rows[0]["payload"]["id"] = thread_id
        _write_rows(root / f"rollout-{name}.jsonl", rows)


def _run_script(*args: str) -> subprocess.CompletedProcess[str]:
    """Run ``cost_analyzer.py`` as a script in a fresh interpreter."""
    return subprocess.run(
        [sys.executable, str(Path(analyzer.__file__)), *args], capture_output=True, encoding="utf-8", check=False
    )


def _claude_windows(path: Path) -> list:
    """Return the context windows of a Claude transcript."""
    return composition.analyze_transcript(path, analyzer)


def _codex_windows(path: Path) -> list:
    """Return the context windows of a Codex rollout."""
    return codex.analyze_transcript(path, analyzer).windows


def _keep_rows(rows: list[dict]) -> list[dict]:
    """Leave the fixture rows untouched, so the explicit compaction record splits the window."""
    return rows


def _force_context_drop(rows: list[dict]) -> list[dict]:
    """Drop the compaction record and shrink the last call so only the context drop splits the window."""
    rows[-1]["message"]["usage"]["input_tokens"] = 79000
    return [row for row in rows if row["type"] == "assistant"]


_CODEX_VALID_ROW = {
    "type": "token_usage_record",
    "payload": {"response_id": "valid", "usage": {"input_tokens": 100, "output_tokens": 0}},
}


class TestClaudeWindows:
    """Splitting a Claude transcript into context windows and accounting each window's content."""

    def test_duplicate_usage_counts_once_and_keeps_later_tool_blocks(self) -> None:
        """Usage counts once while a Read appearing only on the second row remains observable.

        A message streamed over two rows repeats its usage; the second row also carries a Read that must still be
        measured, and the repeated tool bytes must be reported as identical.
        """
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

    def test_explicit_compaction_overrides_context_drop(self) -> None:
        """An earlier drop stays in the same window when native boundary evidence exists.

        The fixture shrinks the context before its compaction record; the record, not the drop, must define the split.
        """
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
    def test_compaction_marker_splits_window(self, tmp_path: Path, marker: dict) -> None:
        """Every supported explicit marker starts a new window even when the context does not shrink.

        The calls before and after the marker use growing context, so only the marker can explain the split.
        """
        path = _write_rows(tmp_path / "transcript.jsonl", [_call("before"), marker, _call("after", 200)])
        assert [len(window.calls) for window in composition.analyze_transcript(path, analyzer)] == [1, 1]

    def test_drop_boundary_is_strictly_below_seventy_percent(self) -> None:
        """Exactly 70% is not a compaction; the next call just below 70% starts a window.

        Pins the inclusive edge of the context-drop heuristic when no compaction record exists.
        """
        windows = composition.analyze_transcript(FIXTURES / "composition_context_drop.jsonl", analyzer)
        assert [len(window.calls) for window in windows] == [2, 1]
        assert windows[1].calls[0].context == 48999
        assert windows[1].boundary == "70% context drop"

    def test_idle_gap_respects_previous_cache_duration(self) -> None:
        """Five-minute expiry, a warm one-hour gap, and one-hour expiry get distinct labels.

        The same wall-clock gap is a cache miss after a 5-minute write and a hit after a 1-hour write, so the label must
        follow the cache duration of the previous call.
        """
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

    def test_attachment_utf8_bytes_and_hook_event_sizes(self) -> None:
        """Payload bytes exclude wrappers, and tool results do not advance the user prompt.

        Hook events are grouped per user prompt, so a tool result between two prompts must not open a new group.
        """
        window = composition.analyze_transcript(FIXTURES / "composition_attachments.jsonl", analyzer)[0]
        assert window.attachments == {"instructions": 9, "nested_memory": 6, "hook_additional_context": 15}
        assert window.hooks == {(1, "UserPromptSubmit"): [3, 6], (1, "PreToolUse"): [2], (2, "UserPromptSubmit"): [4]}
        assert window.prompts == {1, 2}
        report = "\n".join(composition._render_window(window, 1, analyzer))
        assert "| **instructions** | 9 |" in report
        assert "| 1 | **all events** | 3 | 11 | - |" in report
        assert "| 1 | UserPromptSubmit | 2 | 9 | 3, 6 |" in report
        assert "| 2 | **all events** | 1 | 4 | - |" in report

    def test_native_attachment_containers_count_injected_content_once(self, tmp_path: Path) -> None:
        """Native file containers count injected content once, excluding paths and raw copies.

        Each container nests the text differently (file list, nested memory, file body, plain text); the byte count must
        be the visible content only, in UTF-8.
        """
        attachments = [
            {"type": "instructions", "files": [{"path": "guide.md", "type": "project", "content": "café"}]},
            {"type": "nested_memory", "content": {"path": "notes.md", "content": "note", "rawContent": "note"}},
            {"type": "file", "content": {"type": "text", "file": {"filePath": "sample.txt", "content": "abc"}}},
            {"type": "total_tokens_reminder", "text": "reminder"},
        ]
        path = _write_rows(tmp_path / "transcript.jsonl", _attachment_rows(attachments, _call("request")))
        window = composition.analyze_transcript(path, analyzer)[0]
        assert window.attachments == {"instructions": 5, "nested_memory": 4, "file": 3, "total_tokens_reminder": 8}

    @pytest.mark.parametrize("recorded_path", ["folder/example.txt", "C:\\folder\\example.txt"])
    def test_repeated_reads_classified_changed_unchanged_unknown(self, tmp_path: Path, recorded_path: str) -> None:
        """Read comparison preserves foreign path spelling and reports missing content as unknown.

        The same file is read four times with identical, changed, and absent results; the POSIX and Windows spellings
        must both survive verbatim in the report.
        """
        rows = _read_rows(recorded_path, ["alpha", "alpha", "beta", None])
        window = composition.analyze_transcript(_write_rows(tmp_path / "transcript.jsonl", rows), analyzer)[0]
        assert [read.change for read in window.reads[1:]] == ["unchanged", "changed", "unknown"]
        assert [read.path for read in window.reads] == [recorded_path] * 4
        assert window.identical_tool_bytes == 5

    def test_identical_tool_bytes_reset_after_compaction(self, tmp_path: Path) -> None:
        """A result seen before compaction cannot inflate duplicate bytes in the next window.

        The same tool result appears on both sides of a compaction record; after the reset each window sees it once.
        """
        result = {"type": "user", "message": {"content": [{"type": "tool_result", "content": "same"}]}}
        rows = [_call("before"), result, {"type": "compact-boundary"}, _call("after"), result]
        windows = composition.analyze_transcript(_write_rows(tmp_path / "transcript.jsonl", rows), analyzer)
        assert [window.tool_bytes for window in windows] == [4, 4]
        assert [window.identical_tool_bytes for window in windows] == [0, 0]

    def test_model_change_labels_write_above_threshold(self, tmp_path: Path) -> None:
        """A model change labels a large write, while exactly 50,000 tokens is excluded.

        Two consecutive writes straddle the 50,000-token threshold; only the second, on a different model, is listed.
        """
        rows = [
            _call("initial", usage={"cache_creation_input_tokens": 50000}),
            _call("changed", model="claude-sonnet-5", usage={"cache_creation_input_tokens": 50001}),
        ]
        windows = composition.analyze_transcript(_write_rows(tmp_path / "transcript.jsonl", rows), analyzer)
        assert windows[0].large_writes == [(2, 50001, "model change")]

    def test_null_cache_counters_count_as_zero(self, tmp_path: Path) -> None:
        """Null cache counters, valid in the Messages API schema, count as zero instead of discarding the call.

        Both the composition analysis and the cost parser must keep the call, so the two reports cannot disagree.
        """
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

    def test_deeply_nested_payload_is_measured(self) -> None:
        """Payload extraction walks arbitrarily deep nesting without exhausting the interpreter stack.

        The JSON decoder's own depth limit differs by Python version, so the walk is exercised on a decoded value.
        """
        assert composition._payload_bytes({"type": "instructions", "content": _nest(5_000)}) == b"x"


class TestMalformedInput:
    """Rows that parse as JSON but not as transcript rows, and rows the decoder rejects, for both runtimes."""

    def test_corrupt_row_warns_and_keeps_later_usage(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """A corrupt row is visible on stderr without suppressing a later valid call.

        Broken JSON and a bare list precede one valid call; the call must still be counted.
        """
        path = tmp_path / "transcript.jsonl"
        path.write_bytes(("broken\n[]\n" + json.dumps(_call("valid")) + "\n").encode("utf-8"))
        assert len(composition.analyze_transcript(path, analyzer)[0].calls) == 1
        assert "malformed JSON" in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("valid", "windows_of"),
        [
            pytest.param(_call("valid"), _claude_windows, id="claude"),
            pytest.param(_CODEX_VALID_ROW, _codex_windows, id="codex"),
        ],
    )
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
                {
                    "type": "user",
                    "message": {"content": [{"type": "tool_result", "tool_use_id": ["x"], "content": "x"}]},
                },
                id="list-tool-result-id",
            ),
            pytest.param(
                {"type": "attachment", "attachment": {"type": None, "content": "x"}}, id="null-attachment-type"
            ),
            pytest.param(
                {
                    "type": "session_meta",
                    "payload": {"source": {"subagent": {"thread_spawn": {"parent_thread_id": ["x"]}}}},
                },
                id="list-parent-id",
            ),
            pytest.param({"type": "response_item", "payload": {"type": "message", "role": ["user"]}}, id="list-role"),
            pytest.param({"type": "turn_context", "payload": {"model": ["gpt"]}}, id="list-model"),
            pytest.param(
                {"type": "assistant", "message": {"usage": {"input_tokens": 10**309}}}, id="oversized-counter"
            ),
            pytest.param(
                {
                    "type": "user",
                    "message": {"content": [{"type": "tool_use", "id": "bad", "name": "Read", "input": "wrong"}]},
                },
                id="string-tool-input",
            ),
        ],
    )
    def test_invalid_row_shape_warns_and_keeps_later_usage(
        self, tmp_path: Path, capsys: pytest.CaptureFixture, valid: dict, windows_of: Callable[[Path], list], bad: dict
    ) -> None:
        """Valid JSON with invalid nested shapes warns at its original line and cannot hide later usage.

        Every row shape is tried against both runtimes' analyzers, ahead of one valid call that must still be counted.
        """
        path = _write_rows(tmp_path / "transcript.jsonl", [bad, valid])
        windows = windows_of(path)
        assert len(windows) == 1
        assert len(windows[0].calls) == 1
        assert windows[0].calls[0].context == 100
        warning = capsys.readouterr().err
        assert "warning:" in warning
        assert f"{path}:1" in warning
        assert "skipped" in warning

    @pytest.mark.parametrize(
        "line",
        [
            pytest.param(_TOO_DEEP, id="nesting-past-recursion-limit"),
            pytest.param(
                '{"type": "assistant", "message": {"usage": {"input_tokens": ' + "9" * 5_000 + "}}}", id="huge-integer"
            ),
        ],
    )
    def test_undecodable_json_line_is_skipped(self, tmp_path: Path, capsys: pytest.CaptureFixture, line: str) -> None:
        """A row the decoder rejects past its limits is skipped with a warning; later usage survives.

        Deep nesting raises RecursionError and an integer past the digit limit raises a plain ValueError, neither of
        which is a JSONDecodeError; on interpreters without the digit limit the counter bound rejects the row instead.
        """
        path = tmp_path / "transcript.jsonl"
        path.write_bytes((line + "\n" + json.dumps(_call("main")) + "\n").encode("utf-8"))
        windows = composition.analyze_transcript(path, analyzer)
        calls, _, _, _ = analyzer._parse_rows(path)
        assert (len(windows[0].calls), list(calls)) == (1, ["main"])
        assert f"{path}:1; skipped" in capsys.readouterr().err

    def test_skipped_row_leaves_no_partial_state(self, tmp_path: Path) -> None:
        """A rejected row contributes neither its call id nor its tool bytes to later accounting.

        A row that failed midway once kept its message id, so a later valid row with that id was dropped as a duplicate,
        and kept tool-result bytes despite its skipped warning.
        """
        rows = [
            {**_call("repeat", 500), "timestamp": 5},
            {
                "type": "user",
                "message": {"content": [{"type": "tool_result", "tool_use_id": ["x"], "content": "lost"}]},
            },
            _call("repeat", 500),
            _call("other", 500),
        ]
        windows = composition.analyze_transcript(_write_rows(tmp_path / "transcript.jsonl", rows), analyzer)
        assert [item.call.message_id for item in windows[0].calls] == ["repeat", "other"]
        assert windows[0].tool_bytes == 0

    def test_surrogate_attachment_label_still_writes_report(
        self, tmp_path: Path, run_report: Callable[..., str]
    ) -> None:
        """Transcript-controlled labels with lone surrogates cannot abort report writing.

        JSON escapes admit unpaired surrogates; the label lands in a table cell and once raised UnicodeEncodeError.
        """
        attachment = {"type": "bad\ud800", "content": "text"}
        root = _session_root(tmp_path, _attachment_rows([attachment], _call("main", 100)))
        report = run_report("--composition", "--projects-root", str(root), "--session-id", "composition-example")
        assert "| bad? |" in report

    @pytest.mark.parametrize(
        ("rows", "windows_of", "kind"),
        [
            pytest.param(
                _attachment_rows([{"type": "instructions", "content": "café🦊\ud800"}], _call("valid")),
                _claude_windows,
                "instructions",
                id="claude",
            ),
            pytest.param(
                [
                    {
                        "type": "response_item",
                        "payload": {"type": "message", "role": "developer", "content": "café🦊\ud800"},
                    },
                    _CODEX_VALID_ROW,
                ],
                _codex_windows,
                "other",
                id="codex",
            ),
        ],
    )
    def test_lone_surrogates_keep_multibyte_accounting(
        self, tmp_path: Path, rows: list[dict], windows_of: Callable[[Path], list], kind: str
    ) -> None:
        """Escaped invalid Unicode is replaced deterministically while genuine Unicode keeps UTF-8 byte counts.

        The text mixes a two-byte, a four-byte and an unpaired character, so a wrong replacement width changes the total
        of 10 bytes.
        """
        windows = windows_of(_write_rows(tmp_path / "transcript.jsonl", rows))
        assert len(windows) == 1
        assert windows[0].attachments == {kind: 10}


class TestClaudeCli:
    """The Claude composition report through ``cost_analyzer`` selection, output and window discovery."""

    @pytest.mark.parametrize(
        "runtime_flags",
        [pytest.param([], id="default-claude"), pytest.param(["--runtime", "claude"], id="explicit-claude")],
    )
    def test_composition_reuses_prefix_selection_and_output(self, tmp_path: Path, runtime_flags: list[str]) -> None:
        """The CLI writes composition Markdown through the existing output path and prefix selector.

        The session is picked by an eight-character id prefix and written into a report directory that does not exist
        yet, as the cost report does.
        """
        root = tmp_path / "projects"
        transcript = root / "project" / "composition-example.jsonl"
        transcript.parent.mkdir(parents=True)
        shutil.copyfile(FIXTURES / "composition_duplicate_usage.jsonl", transcript)
        output = tmp_path / "reports" / "context.md"
        args = ["--composition", *runtime_flags, "--projects-root", str(root), "--session-id", transcript.stem[:8]]
        assert analyzer.main([*args, "--output", str(output)]) == 0
        report = output.read_text(encoding="utf-8")
        assert report.startswith("## Context composition\n")
        assert "| 1,120 | 1,120 | 1 | 81.8% |" in report
        assert "5-minute 100 tokens; 1-hour 0 tokens" in report
        assert report.endswith("\n")

    def test_since_mode_keeps_subagent_contexts_separate(self, tmp_path: Path, run_report: Callable[..., str]) -> None:
        """Window discovery includes recent activity without blending agent context trajectories.

        A main transcript and one subagent transcript each get their own first window rather than one merged series.
        """
        root = tmp_path / "projects"
        main = _write_rows(root / "project" / "composition-example.jsonl", [_dated_call("main", 100)])
        _write_rows(main.parent / main.stem / "subagents" / "agent-example.jsonl", [_dated_call("agent", 10)])
        report = run_report("--composition", "--projects-root", str(root), "--since", "24h")
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
            pytest.param(
                {"type": "assistant", "timestamp": 5, "message": {"id": "bad", "content": 7}}, id="int-fields"
            ),
            pytest.param({"type": "assistant", "message": {"id": ["bad"], "usage": {"input_tokens": 1}}}, id="list-id"),
            pytest.param({"type": "assistant", "message": {"usage": {"input_tokens": "1"}}}, id="string-counter"),
        ],
    )
    def test_since_selection_skips_malformed_rows(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
        run_report: Callable[..., str],
        flags: list[str],
        expected: str,
        bad: object,
    ) -> None:
        """Window discovery skips malformed rows and still reports the session's valid usage.

        Session selection parses every recent transcript before composition analysis runs, so one valid-JSON row with
        the wrong shape must not abort the whole --since report in either report mode.
        """
        root = _session_root(tmp_path, [bad, _dated_call("main", 100)])
        report = run_report(*flags, "--projects-root", str(root), "--since", "24h")
        assert expected in report
        assert "composition-example.jsonl:1; skipped" in capsys.readouterr().err

    def test_missing_selector_exits_one_without_writing(
        self, tmp_path: Path, output: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """A session id that matches nothing fails with a message and leaves no report behind.

        The projects root does not exist at all, which is what a mistyped path looks like.
        """
        args = ["--composition", "--projects-root", str(tmp_path / "projects"), "--session-id", "absent"]
        assert analyzer.main([*args, "--output", str(output)]) == 1
        assert "no session matching" in capsys.readouterr().err
        assert not output.exists()

    def test_transcript_without_usage_exits_one_without_writing(
        self, tmp_path: Path, output: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """A selected transcript that holds no usage fails with a message and leaves no report behind.

        A session with only a user row has nothing to compose; an empty report would be misleading.
        """
        root = _session_root(tmp_path, [{"type": "user"}])
        args = ["--composition", "--projects-root", str(root), "--session-id", "composition-example"]
        assert analyzer.main([*args, "--output", str(output)]) == 1
        assert "no sessions with usage" in capsys.readouterr().err
        assert not output.exists()

    def test_script_entrypoint_imports_private_module(self, tmp_path: Path, output: Path) -> None:
        """The private-module import works when cost_analyzer runs as __main__.

        Run as a real subprocess because the import path of ``_context_composition`` differs between ``-m``-less script
        execution and an in-process import.
        """
        result = _run_script(
            "--composition",
            *("--session-id", "composition_duplicate_usage"),
            *("--runtime", "claude"),
            *("--compactions-log", str(tmp_path / "missing-compactions.jsonl")),
            *("--projects-root", str(FIXTURES / "composition_duplicate_usage.jsonl")),
            *("--output", str(output)),
        )
        assert result.returncode == 0, result.stderr
        assert output.read_text(encoding="utf-8").startswith("## Context composition")


class TestCompactionLog:
    """Joining the logged compaction events of a session to its measured windows."""

    def test_missing_log_adds_note_only(self, run_report: Callable[..., str]) -> None:
        """A missing log adds only a note to the existing Claude session measurements.

        The isolated default log path does not exist, so the report must equal the plain session report plus one note.
        """
        path = _COMPACTIONS_TRANSCRIPT
        report = run_report("--composition", "--session-id", path.stem, "--projects-root", str(path))
        expected = [
            f"### Session `{path.stem}`",
            "",
            "Cache-write split (main + subagents): 5-minute 0 tokens; 1-hour 0 tokens.",
            "",
            "no logged compaction events.",
            "",
            "#### Transcript: main",
            "",
            *_render_windows(path),
        ]
        assert "\n".join(expected) in report
        assert report.count("no logged compaction events") == 1
        assert "Logged compaction events" not in report

    def test_events_sorted_by_instant_across_offsets(self, logged_report: str) -> None:
        """Session-matched events sort by their instants, whatever timestamp spelling or offset they use.

        The fixture mixes ``Z``, ``+00:00``, ``+01:00`` and fractional-second stamps, so file order and instant order
        differ.
        """
        assert _in_order(logged_report, _LOGGED_EVENT_ROWS)

    def test_other_sessions_and_future_events_excluded(self, logged_report: str) -> None:
        """Events from another session, and events after the last window, are not rendered.

        The log is shared across sessions, so the join must filter by session id and drop events with no window.
        """
        assert "Logged compaction events" in logged_report
        assert "another-session" not in logged_report
        assert "future-event" not in logged_report

    def test_each_gate_compared_with_next_compaction(self, logged_report: str) -> None:
        """Each gate's estimate is compared with the realised window of the compaction that follows it.

        The first two gates precede compactions and show estimate versus post-call tokens; the last gate has no
        compaction after it and says so.
        """
        assert "calls 4 vs 2" in logged_report
        assert "calls 3 vs 1" in logged_report
        assert "120,000 tokens (gate floor assumption) vs 125,000 tokens (post-call)" in logged_report
        assert "120,000 tokens (gate floor assumption) vs 118,000 tokens (post-call)" in logged_report
        assert logged_report.count("estimate vs realised") == 3
        assert "no compaction followed" in logged_report

    @pytest.mark.parametrize(
        ("boundary", "rewrite"),
        [
            pytest.param("compaction record", _keep_rows, id="compaction-record"),
            pytest.param("70% context drop", _force_context_drop, id="context-drop"),
        ],
    )
    def test_gate_matches_logged_compaction_without_blending_subagents(
        self, tmp_path: Path, boundary: str, rewrite: Callable[[list[dict]], list[dict]]
    ) -> None:
        """Multiple gates can precede one compaction, whose realised calls belong only to the main window.

        A subagent transcript sits beside the main one with its own calls; the realised window must count the main
        transcript only, whichever boundary evidence split it.
        """
        path = _write_rows(
            tmp_path / _COMPACTIONS_TRANSCRIPT.name, rewrite(_read_fixture_rows(_COMPACTIONS_TRANSCRIPT))
        )
        _write_rows(path.parent / path.stem / "subagents" / "agent-example.jsonl", [_call("agent", 500)])
        events = composition._read_compactions(_COMPACTIONS_EVENTS, analyzer)[path.stem]
        events.insert(1, {**events[0], "ts": "2030-01-01T00:00:01.500Z", "calls_ahead": 0})
        report = "\n".join(composition._render_session(path, analyzer, events))
        assert "calls 4 vs 2" in report
        assert "calls 0 vs 2" in report
        assert "main window 2" in report
        assert f"Context window 2 ({boundary})" in report
        assert "#### Transcript: subagent agent-example" in report

    def test_missing_call_timing_reports_unavailable(self, tmp_path: Path) -> None:
        """Compaction logs alone cannot assign a realised window when transcript timestamps are absent.

        The transcript has a compaction record but no call timestamps, so events cannot be placed in any window.
        """
        path = _write_rows(
            tmp_path / _COMPACTIONS_TRANSCRIPT.name, [_call("before"), {"type": "compact-boundary"}, _call("after")]
        )
        events = composition._read_compactions(_COMPACTIONS_EVENTS, analyzer)[path.stem]
        report = "\n".join(composition._render_session(path, analyzer, events))
        assert report.count("realised window unavailable") == 2
        assert report.count("no compaction followed") == 1
        assert "calls 4 vs 1" not in report

    def test_malformed_events_warn_and_keep_valid_rows(
        self, tmp_path: Path, capsys: pytest.CaptureFixture, run_report: Callable[..., str]
    ) -> None:
        """Malformed JSON, non-objects and invalid timestamps cannot hide valid later events.

        Three bad lines precede one valid gate event; each must warn with its own line number, and the pipe and newline
        in the valid reason must be escaped so the table row stays on one line.
        """
        log = tmp_path / "compactions.jsonl"
        valid = {
            "event": "gate",
            "session_id": "composition_compactions_example",
            "ts": "2030-01-01T00:00:12Z",
            "decision": "continue",
            "reason": "literal | reason\nnext line",
        }
        lines = ["broken JSON", "[]", json.dumps({**valid, "ts": 42}), json.dumps(valid)]
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        report = run_report(
            "--composition",
            *("--session-id", "composition_compactions_example"),
            *("--projects-root", str(_COMPACTIONS_TRANSCRIPT)),
            *("--compactions-log", str(log)),
        )
        warning = capsys.readouterr().err
        assert warning.count("warning:") == 3
        assert f"{log}:1" in warning
        assert f"{log}:2" in warning
        assert f"{log}:3" in warning
        assert "literal &#124; reason next line" in report
        assert "no compaction followed" in report

    @pytest.mark.parametrize(
        "line",
        [
            pytest.param(_TOO_DEEP, id="nesting-past-recursion-limit"),
            pytest.param('{"event": "gate", "ctx": ' + "9" * 5_000 + "}", id="huge-integer"),
            pytest.param(
                '{"event": "gate", "ts": "2030-01-01T00:00:00Z", "session_id": "other", "K": ' + "9" * 400 + "}",
                id="overflowing-k",
            ),
        ],
    )
    def test_undecodable_event_is_skipped(self, tmp_path: Path, capsys: pytest.CaptureFixture, line: str) -> None:
        """An undecodable compaction-log line warns and leaves later valid events grouped by session.

        The bad line comes first; the valid event after it must still be read and keyed by its session id.
        """
        event = {"event": "gate", "ts": "2030-01-01T00:00:00Z", "session_id": "sample-session"}
        path = tmp_path / "compactions.jsonl"
        path.write_bytes((line + "\n" + json.dumps(event) + "\n").encode("utf-8"))
        assert list(composition._read_compactions(path, analyzer)) == ["sample-session"]
        assert "malformed compaction event" in capsys.readouterr().err


class TestCodexTranscript:
    """Codex rollouts: response dedupe, window splitting, injections and code-edit evidence."""

    def test_response_dedupe_windows_and_injections(self) -> None:
        """Response ids exclude mirrored usage while explicit compaction preserves the earlier drop.

        The parent rollout mirrors usage under response ids, injects several developer messages, and compacts once; the
        windows, attachment bytes, tool bytes and large-write labels must all follow from that single record.
        """
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

    def test_context_drop_starts_window_without_boundary_record(self) -> None:
        """The strict drop fallback splits a rollout that has no compaction record.

        Mirrors the Claude 70% rule for rollouts, which usually carry no explicit boundary.
        """
        thread = codex.analyze_transcript(FIXTURES / "composition_codex_drop.jsonl", analyzer)
        assert [len(window.calls) for window in thread.windows] == [2, 1]
        assert thread.windows[1].boundary == "70% context drop"

    def test_legacy_cumulative_events_dedupe(self) -> None:
        """Adjacent cumulative token-count keys preserve calls after a reset without double counting.

        The legacy format repeats cumulative totals on every event; three distinct calls must remain, each seen once.
        """
        thread = codex.analyze_transcript(FIXTURES / "composition_codex_legacy.jsonl", analyzer)
        assert sum(len(window.calls) for window in thread.windows) == 3
        assert [item.context for item in thread.windows[0].calls] == [110, 110, 110]

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
    def test_code_edit_follows_call_evidence(self, tmp_path: Path, payload: dict, edited: bool) -> None:
        """Edit flags follow actual patch or code-write call evidence rather than filename references.

        One function call is inserted into an otherwise edit-free rollout; only calls that write a code file count.
        """
        rows = _read_fixture_rows(FIXTURES / "composition_codex_drop.jsonl")
        rows.insert(1, {"type": "response_item", "payload": {"type": "function_call", **payload}})
        path = _write_rows(tmp_path / "rollout-example.jsonl", rows)
        assert codex.analyze_transcript(path, analyzer).edited_code is edited

    def test_undecodable_tool_arguments_do_not_abort(self) -> None:
        """Tool arguments nested past the decoder limit fall back to text matching instead of raising.

        Arguments are decoded as JSON to look for a code write; a pathological string must degrade, not crash.
        """
        payload = {"type": "function_call", "name": "exec_command", "arguments": _TOO_DEEP}
        assert codex._is_code_edit(payload) is False

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
    def test_invalid_usage_warns_and_keeps_valid_calls(
        self, tmp_path: Path, invalid: dict, capsys: pytest.CaptureFixture
    ) -> None:
        """Corrupt counters cannot affect context totals or suppress later valid responses.

        One response with impossible counters is inserted between two valid ones; only the valid three are counted.
        """
        rows = _read_fixture_rows(FIXTURES / "composition_codex_drop.jsonl")
        rows.insert(2, {"type": "token_usage_record", "payload": {"response_id": "invalid-example", "usage": invalid}})
        thread = codex.analyze_transcript(_write_rows(tmp_path / "rollout-example.jsonl", rows), analyzer)
        assert len(thread.calls) == 3
        assert "valid last-response counters" in capsys.readouterr().err

    @pytest.mark.parametrize("kind", ["review", "compact", "memory_consolidation"])
    def test_string_subagent_keeps_identity(self, tmp_path: Path, kind: str) -> None:
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


class TestCodexCli:
    """The Codex composition report through ``cost_analyzer`` selection, discovery and output."""

    @pytest.mark.parametrize(
        "flags", [pytest.param(["--composition"], id="composition-mode"), pytest.param([], id="cost-mode")]
    )
    def test_script_entrypoint_writes_tokens_only(self, output: Path, flags: list[str]) -> None:
        """Codex selects token-only composition through the actual script entrypoint.

        Run as a real subprocess in both report modes; neither may print cache-write splits or dollar amounts, which a
        Codex rollout cannot supply.
        """
        result = _run_script(
            *flags,
            *("--runtime", "codex"),
            *("--projects-root", str(FIXTURES / "composition_codex_parent.jsonl")),
            *("--output", str(output)),
        )
        assert result.returncode == 0, result.stderr
        report = output.read_text(encoding="utf-8")
        assert "Runtime: Codex (tokens only)" in report
        assert "| 100,010 | 40,000 | 2 | 71.4% |" in report
        assert "Cache-write" not in report
        assert "$" not in report

    def test_family_prefix_reports_subagent_share(
        self, codex_family_root: Path, run_report: Callable[..., str]
    ) -> None:
        """Metadata prefixes select linked rollouts without merging parent and child windows.

        The parent prefix selects the parent and child rollouts together; their windows stay separate and the child's
        share of calls and tokens is reported.
        """
        report = run_report(*_CODEX_FLAGS, "--projects-root", str(codex_family_root), "--session-id", "example-par")
        assert report.count("#### Context window") == 3
        assert "Subagent share: 1/5 calls (20.0%); 10,010/270,041 tokens (3.7%)" in report
        assert "AGENTS.md bytes × calls" in report
        assert "| main | yes (attempt observed) | 5 | 4 | 20 |" in report
        assert "| subagent example-child | no edit observed | 4 | 1 | 4 |" in report

    def test_child_prefix_selects_whole_family(self, codex_family_root: Path, run_report: Callable[..., str]) -> None:
        """Selecting by the child's prefix produces the same report as selecting by the parent's.

        Both ids name one family, so the report must not depend on which member the caller happened to type.
        """
        args = [*_CODEX_FLAGS, "--projects-root", str(codex_family_root), "--session-id"]
        assert run_report(*args, "example-child") == run_report(*args, "example-par")

    def test_missing_parent_keeps_sibling_family(self, tmp_path: Path, run_report: Callable[..., str]) -> None:
        """Children linked to an absent parent still form one family, selected through either child.

        Two child rollouts point at a parent that was never written; selecting one by prefix must still find both.
        """
        root = tmp_path / "sessions"
        _write_rollouts(
            root, "composition_codex_child.jsonl", {"alpha-child": "alpha-child", "beta-child": "beta-child"}
        )
        report = run_report(*_CODEX_FLAGS, "--projects-root", str(root), "--session-id", "alpha")
        assert "Sessions found: 1." in report
        assert report.count("#### Context window") == 2
        assert "Subagent share: 2/2 calls (100.0%); 20,020/20,020 tokens (100.0%)" in report

    @pytest.mark.parametrize(
        ("thread_ids", "extra_args", "message"),
        [
            pytest.param(
                {"alpha": "drop-example", "beta": "drop-other"},
                ["--session-id", "drop-"],
                "ambiguous Codex session prefix",
                id="ambiguous-prefix",
            ),
            pytest.param(
                {"alpha": "drop-example", "beta": "drop-other", "duplicate": "drop-other"},
                [],
                "duplicate Codex thread identity",
                id="duplicate-identity",
            ),
        ],
    )
    def test_unsafe_selection_fails_without_writing(
        self,
        tmp_path: Path,
        output: Path,
        capsys: pytest.CaptureFixture,
        thread_ids: dict[str, str],
        extra_args: list[str],
        message: str,
    ) -> None:
        """Unrelated prefix matches and duplicate thread identities never silently double-count usage.

        Two unrelated threads share the prefix ``drop-``, or two rollouts claim one thread id; both must exit 1.
        """
        root = tmp_path / "sessions"
        _write_rollouts(root, "composition_codex_drop.jsonl", thread_ids)
        args = [*_CODEX_FLAGS, "--projects-root", str(root), *extra_args, "--output", str(output)]
        assert analyzer.main(args) == 1
        assert message in capsys.readouterr().err
        assert not output.exists()

    @pytest.mark.parametrize(
        ("extra_args", "message"),
        [
            pytest.param(["--session-id", "absent"], "no session matching", id="absent-selector"),
            pytest.param([], "no sessions with usage", id="cumulative-only-events"),
        ],
    )
    def test_unusable_sessions_exit_one_without_writing(
        self, tmp_path: Path, output: Path, capsys: pytest.CaptureFixture, extra_args: list[str], message: str
    ) -> None:
        """Missing Codex selectors and cumulative-only events cannot create misleading reports.

        The only rollout carries a cumulative total but no per-response usage, so there is nothing to compose.
        """
        info = {"total_token_usage": {"input_tokens": 100}}
        rows = [
            {"type": "session_meta", "payload": {"id": "empty-example"}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": info}},
        ]
        root = tmp_path / "sessions"
        _write_rows(root / "rollout-empty.jsonl", rows)
        args = [*_CODEX_FLAGS, "--projects-root", str(root), *extra_args, "--output", str(output)]
        assert analyzer.main(args) == 1
        assert message in capsys.readouterr().err
        assert not output.exists()

    def test_default_home_with_since_selects_recent_rollouts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_report: Callable[..., str]
    ) -> None:
        """CODEX_HOME supplies discovery while recorded timestamps control recent-family selection.

        No ``--projects-root`` is given; the rollout lives under ``$CODEX_HOME/sessions`` and must be found by
        ``--since``.
        """
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        rollout = tmp_path / "sessions" / "2030" / "01" / "01" / "rollout-example.jsonl"
        rollout.parent.mkdir(parents=True)
        shutil.copyfile(FIXTURES / "composition_codex_drop.jsonl", rollout)
        report = run_report(*_CODEX_FLAGS, "--since", "2d")
        assert report.count("#### Context window") == 2

    def test_help_describes_token_only_runtime(self) -> None:
        """The help text says Codex reports are token-only and names the Claude plugin that owns cost.

        Callers choose the runtime from the help, so the limitation must be stated there.
        """
        help_text = analyzer._build_parser().format_help()
        assert "token-only" in help_text
        assert "Claude foundry plugin" in help_text

    @pytest.mark.parametrize(
        "root_args",
        [
            pytest.param(lambda root: ["--projects-root", str(root)], id="full-name"),
            pytest.param(lambda root: ["--projects-ro", str(root)], id="abbreviated"),
            pytest.param(lambda root: [f"--projects-root={root}"], id="full-name-equals"),
            pytest.param(lambda root: [f"--projects-ro={root}"], id="abbreviated-equals"),
        ],
    )
    def test_explicit_root_overrides_home(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        run_report: Callable[..., str],
        root_args: Callable[[Path], list[str]],
    ) -> None:
        """Accepted explicit-root spellings retain argparse behavior and override CODEX_HOME.

        CODEX_HOME points at a missing directory, so only the explicit root can supply the rollout.
        """
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "absent"))
        report = run_report(*_CODEX_FLAGS, *root_args(FIXTURES / "composition_codex_drop.jsonl"))
        assert report.count("#### Context window") == 2

    def test_ignores_claude_compactions_log(self, tmp_path: Path, output: Path) -> None:
        """Codex output stays identical even when the Claude log path cannot be read as a file.

        The second run points ``--compactions-log`` at a directory; the Claude-only log must not be opened for Codex.
        """
        flags = [*_CODEX_FLAGS, "--projects-root", str(FIXTURES / "composition_codex_parent.jsonl")]
        flags += ["--output", str(output)]
        assert analyzer.main(flags) == 0
        expected = output.read_bytes()
        assert analyzer.main([*flags, "--compactions-log", str(tmp_path)]) == 0
        assert output.read_bytes() == expected
        assert b"Logged compaction events" not in expected
        assert b"no logged compaction events" not in expected

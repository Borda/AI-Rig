"""Exercise the resolve picker's question-text contract through the real Node hook."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import ClassVar

import pytest

HOOK = Path(__file__).resolve().parents[2] / "hooks/enforce-resolve-table.js"
CSID = "test-session-5678"
_skip_node_unavailable = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
_OPTIONS = [
    {"label": "+All [req]", "description": "Queue the required items"},
    {"label": "+All [suggest]", "description": "Queue the suggested items"},
    {"label": "ALL (req + suggest)", "description": "Queue all pending items"},
    {"label": "Skip all", "description": "Skip the items and exit"},
]
_PENDING = (
    {"id": 1, "type": "[report]", "summary": "guard keeps view", "severity": "high"},
    {"id": 2, "type": "[gh][suggest]", "summary": "add test", "severity": "low"},
    {"id": 3, "type": "[done]"},
)
_TABLE = (
    "| # | Type | Summary |\n| -- | -- | -- |\n| 1 | [report] | guard keeps view |\n| 2 | [gh][suggest] | add test |"
)


def _items(tmp_path: Path, *rows: dict) -> Path:
    """Write a real items file, its saved table with every selectable id, and the session-scoped sentinel."""
    impl = tmp_path / "impl"
    impl.mkdir()
    (impl / "action-items.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8", newline="\n"
    )
    table = "| # | Type | Summary |\n| -- | -- | -- |\n" + "".join(
        f"| {r['id']} | {r['type']} | {r.get('summary', 'x')} |\n"
        for r in rows
        if "[done]" not in r["type"] and "[info]" not in r["type"]
    )
    (impl / "action-items-table.md").write_text(table, encoding="utf-8", newline="\n")
    (tmp_path / f"resolve-impl-dir-{CSID}").write_text(f"{impl}\n", encoding="utf-8", newline="\n")
    return impl


def _bulk(impl: Path, notice: str = "") -> dict:
    """Bulk question whose text holds only counts, notices and the saved table path, never item rows."""
    text = f"{notice}→ Full item table: {impl / 'action-items-table.md'}"
    return {"question": text, "header": "Bulk action", "multiSelect": False, "options": _OPTIONS}


def _run(tmp_path: Path, questions: list[dict], **extra: object) -> str | None:
    """Invoke PreToolUse, preserving host environment requirements on every OS."""
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "AskUserQuestion",
        "tool_input": {"questions": questions},
        **extra,
    }
    proc = subprocess.run(
        ["node", str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        encoding="utf-8",
        timeout=10,
        env={**os.environ, "TMPDIR": str(tmp_path), "CLAUDE_CODE_SESSION_ID": CSID},
    )
    assert proc.returncode == 0, proc.stderr
    decision = json.loads(proc.stdout)["hookSpecificOutput"] if proc.stdout.strip() else {}
    return decision.get("permissionDecisionReason") if decision.get("permissionDecision") == "deny" else None


def _previews(question: dict, *previews: str) -> dict:
    """Assign previews to options without mutating the shared bulk fixture."""
    return {
        **question,
        "options": [
            {**option, "preview": preview} if preview is not None else option
            for option, preview in zip(question["options"], previews, strict=True)
        ],
    }


def _large(tmp_path: Path, rows: int = 30) -> tuple[Path, dict]:
    """Create a large real case: every id in the saved table, only counts and the path in Q1."""
    items = [
        {"id": n, "type": "[gh][suggest]", "severity": "high" if n == 1 else "low", "summary": "x" * 50}
        for n in range(1, rows + 1)
    ]
    impl = _items(tmp_path, *reversed(items))
    return impl, _bulk(impl, f"→ {rows} pending items\n")


@_skip_node_unavailable
class TestSelectionGate:
    def test_counts_and_table_path_pass_without_item_rows_or_previews(self, tmp_path: Path) -> None:
        """The printed table and item options show the items, so Q1 needs only notices and the saved table path."""
        impl = _items(tmp_path, *_PENDING)
        assert _run(tmp_path, [_bulk(impl, "→ 2 pending items\n")]) is None

    def test_missing_table_path_denial_and_question_text_retry(self, tmp_path: Path) -> None:
        """The remedy names the saved table file and accepts the corrected first question."""
        impl = _items(tmp_path, *_PENDING)
        reason = _run(tmp_path, [{**_bulk(impl), "question": "Choose items"}])
        assert reason is not None
        assert str(impl / "action-items-table.md") in reason
        assert "1500" in reason
        assert "never item rows" in reason
        assert _run(tmp_path, [_bulk(impl)]) is None

    @pytest.mark.parametrize("field", ["description", "preview", "later-question", "question-description"])
    def test_table_path_in_other_fields_never_supplies_q1(self, tmp_path: Path, field: str) -> None:
        """Descriptions, previews and later questions cannot stand in for Q1's own table path."""
        impl = _items(tmp_path, *_PENDING)
        path_text = _bulk(impl)["question"]
        question = {**_bulk(impl), "question": "Choose items"}
        questions = [question]
        if field in ("description", "preview"):
            question["options"] = [{**o, field: path_text} for o in question["options"]]
        elif field == "later-question":
            questions.append(_bulk(impl))
        else:
            question["description"] = path_text
        assert _run(tmp_path, questions) is not None

    @pytest.mark.parametrize("kind", ["text", "thinking"])
    def test_transcript_table_does_not_supply_the_question(self, tmp_path: Path, kind: str) -> None:
        """Earlier reply text and thinking never replace Q1's saved-table path."""
        impl = _items(tmp_path, *_PENDING)
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            json.dumps({"type": "assistant", "message": {"content": [{"type": kind, kind: _TABLE}]}}), encoding="utf-8"
        )
        assert _run(tmp_path, [{**_bulk(impl), "question": "Choose"}], transcript_path=str(transcript)) is not None

    def test_denial_does_not_wait_for_transcript(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A flush wait cannot delay a gate that only reads the call and items file."""
        impl = _items(tmp_path, *_PENDING)
        monkeypatch.setenv("CLAUDE_GATE_FLUSH_WAIT_MS", "10000")
        start = time.monotonic()
        assert _run(tmp_path, [{**_bulk(impl), "question": "Choose"}]) is not None
        assert time.monotonic() - start < 3

    def test_multiselect_bulk_is_denied(self, tmp_path: Path) -> None:
        """The bulk page remains the first, single-select question."""
        impl = _items(tmp_path, *_PENDING)
        reason = _run(tmp_path, [{**_bulk(impl), "multiSelect": True}])
        assert reason is not None
        assert "single-select" in reason

    @pytest.mark.parametrize("wrapper", ["<pre>{}</pre>", "<div>{}</div>", "<table>{}</table>"])
    def test_html_question_text_is_denied(self, tmp_path: Path, wrapper: str) -> None:
        """HTML cannot substitute for plain question text."""
        impl = _items(tmp_path, *_PENDING)
        assert _run(tmp_path, [{**_bulk(impl), "question": wrapper.format(_bulk(impl)["question"])}]) is not None


@_skip_node_unavailable
class TestLimits:
    @pytest.mark.parametrize(
        ("length", "allowed"), [pytest.param(1500, True, id="at-cap"), pytest.param(1501, False, id="over-cap")]
    )
    def test_question_character_budget(self, tmp_path: Path, length: int, allowed: bool) -> None:
        """The entire question, including hints, has a hard 1500-character cap."""
        impl = _items(tmp_path, *_PENDING)
        base = _bulk(impl)["question"]
        text = base + "\n" + "x" * (length - len(base) - 1)
        reason = _run(tmp_path, [{**_bulk(impl), "question": text}])
        assert (reason is None) is allowed
        if not allowed:
            assert "1500" in reason

    @pytest.mark.parametrize(
        "preview", [pytest.param("Queue these items", id="compact-summary"), pytest.param(_TABLE, id="legacy-table")]
    )
    def test_identical_previews_are_denied(self, tmp_path: Path, preview: str) -> None:
        """Four identical compact summaries repeat the reported regression."""
        impl = _items(tmp_path, *_PENDING)
        reason = _run(tmp_path, [_previews(_bulk(impl), *([preview] * 4))])
        assert reason is not None
        assert "identical previews" in reason

    def test_identical_previews_in_separate_questions_are_denied(self, tmp_path: Path) -> None:
        """A later question cannot repeat another option's comparison."""
        impl = _items(tmp_path, *_PENDING)
        bulk = _previews(_bulk(impl), "Required", "Suggestions", "All", "Skip")
        reason = _run(tmp_path, [bulk, {"question": "Commit mode?", "options": [{"preview": "Required"}]}])
        assert reason is not None
        assert "identical previews" in reason

    def test_short_distinct_plain_comparisons_pass(self, tmp_path: Path) -> None:
        """Optional distinct comparisons stay within the host cap."""
        impl = _items(tmp_path, *_PENDING)
        assert (
            _run(tmp_path, [_previews(_bulk(impl), "Required only", "Suggestions only", "All pending", "Skip")]) is None
        )

    @pytest.mark.parametrize(
        "preview",
        [
            pytest.param("x" * 2001, id="chars"),
            pytest.param("\n".join(["x"] * 13), id="lines"),
            pytest.param("\n".join(["x" * 87] * 7), id="wrapped-lines"),
            pytest.param(_TABLE, id="table"),
            pytest.param("<pre>Compare</pre>", id="html"),
        ],
    )
    def test_preview_over_cap_or_rich_content_is_denied(self, tmp_path: Path, preview: str) -> None:
        """A valid Q1 cannot excuse a withheld, clipped, table or HTML preview."""
        impl = _items(tmp_path, *_PENDING)
        assert _run(tmp_path, [_previews(_bulk(impl), preview, "Suggestions", "All", "Skip")]) is not None

    @pytest.mark.parametrize(
        ("lines", "allowed"), [pytest.param(12, True, id="line-cap"), pytest.param(13, False, id="line-over")]
    )
    def test_optional_comparison_line_boundary(self, tmp_path: Path, lines: int, allowed: bool) -> None:
        """The host counts twelve short rendered lines, including wrapping."""
        impl = _items(tmp_path, *_PENDING)
        reason = _run(tmp_path, [_previews(_bulk(impl), "\n".join(["Compare"] * lines), "Suggestions", "All", "Skip")])
        assert (reason is None) is allowed

    def test_later_option_preview_is_checked(self, tmp_path: Path) -> None:
        """Limits apply to the whole selection call, including checkbox questions."""
        impl = _items(tmp_path, *_PENDING)
        assert _run(tmp_path, [_bulk(impl), {"question": "Items", "options": [{"preview": "x" * 2001}]}]) is not None


@_skip_node_unavailable
class TestSavedTable:
    def test_large_list_needs_only_counts_and_the_complete_table_file(self, tmp_path: Path) -> None:
        """Thirty pending items add nothing to Q1 beyond the count and the real complete table path."""
        _, q = _large(tmp_path)
        assert _run(tmp_path, [q]) is None

    def test_forward_slash_table_path_passes(self, tmp_path: Path) -> None:
        """Skill-rendered paths can use forward slashes even on native Windows."""
        impl, q = _large(tmp_path)
        file = impl / "action-items-table.md"
        q = {**q, "question": q["question"].replace(str(file), file.as_posix())}
        assert _run(tmp_path, [q]) is None

    @pytest.mark.parametrize(
        "fault",
        [
            pytest.param("missing-file", id="missing-file"),
            pytest.param("directory", id="directory"),
            pytest.param("missing-id", id="missing-id"),
            pytest.param("no-path", id="no-path"),
            pytest.param("oversize-file", id="oversize-file"),
        ],
    )
    def test_incomplete_saved_table_is_denied(self, tmp_path: Path, fault: str) -> None:
        """Q1 must name a saved table that exists, stays bounded and holds every id as its own cell."""
        impl, q = _large(tmp_path)
        file = impl / "action-items-table.md"
        if fault == "missing-file":
            file.unlink()
        elif fault == "directory":
            file.unlink()
            file.mkdir()
        elif fault == "missing-id":
            file.write_text(file.read_text(encoding="utf-8").replace("| 30 |", "| 31 |"), encoding="utf-8")
        elif fault == "oversize-file":
            file.write_text("x" * (2 * 1024 * 1024 + 1), encoding="utf-8")
        else:
            q = {**q, "question": q["question"].replace(str(file), "elsewhere.md")}
        assert _run(tmp_path, [q]) is not None

    def test_composite_row_hiding_an_id_is_denied(self, tmp_path: Path) -> None:
        """A table row that merges two ids hides one from the selection record."""
        impl, q = _large(tmp_path, rows=3)
        file = impl / "action-items-table.md"
        file.write_text(file.read_text(encoding="utf-8").replace("| 3 |", "| 2, 3 |"), encoding="utf-8")
        assert _run(tmp_path, [q]) is not None


@_skip_node_unavailable
class TestClosedAndPassThrough:
    _CLOSED: ClassVar[dict[str, int | str]] = {"id": 4, "type": "[gh][req]", "status": "resolved"}
    _NOTICE = "→ {} resolved/addressed items not in bulk options — type their ids to include\n"

    def test_wrong_closed_count_is_denied(self, tmp_path: Path) -> None:
        """A count with the correct number as a suffix must not hide incorrect closed-item context."""
        impl = _items(tmp_path, *_PENDING, self._CLOSED)
        reason = _run(tmp_path, [_bulk(impl, self._NOTICE.format(11))])
        assert reason is not None
        assert "1 resolved/addressed items" in reason

    def test_missing_closed_count_is_denied(self, tmp_path: Path) -> None:
        """Closed rows remain selectable by typed id, so their count cannot disappear."""
        impl = _items(tmp_path, *_PENDING, self._CLOSED)
        reason = _run(tmp_path, [_bulk(impl)])
        assert reason is not None
        assert "1 resolved/addressed items" in reason

    def test_bulk_page_after_a_different_question_is_denied(self, tmp_path: Path) -> None:
        """A valid Q1 is insufficient if the bulk action is on a later tab."""
        impl = _items(tmp_path, *_PENDING)
        first = {"question": _bulk(impl)["question"], "options": [{"label": "Each"}, {"label": "All"}]}
        reason = _run(tmp_path, [first, _bulk(impl)])
        assert reason is not None
        assert "bulk-action question first" in reason

    def test_closed_items_need_only_a_count(self, tmp_path: Path) -> None:
        """Closed rows stay in the table and are named by count only."""
        impl = _items(tmp_path, *_PENDING, self._CLOSED)
        assert _run(tmp_path, [_bulk(impl, self._NOTICE.format(1))]) is None

    def test_closed_only_count_passes(self, tmp_path: Path) -> None:
        """A closed-only picker names the count and the saved table, never the closed rows."""
        impl = _items(tmp_path, {"id": 4, "type": "[gh][req]", "status": "addressed"})
        assert _run(tmp_path, [_bulk(impl, self._NOTICE.format(1))]) is None

    @pytest.mark.parametrize(
        "case", ["no-sentinel", "no-items", "done-info", "malformed", "non-selection", "other-event", "other-tool"]
    )
    def test_out_of_scope_or_unreadable_evidence_fails_open(self, tmp_path: Path, case: str) -> None:
        """Existing scoping and fail-open conventions keep recovery calls usable."""
        q = {**_bulk(tmp_path), "question": "Choose"}
        extra = {}
        if case != "no-sentinel":
            impl = _items(tmp_path, *_PENDING)
            if case == "no-items":
                (impl / "action-items.jsonl").unlink()
            elif case == "done-info":
                (impl / "action-items.jsonl").write_text(
                    '{"id":3,"type":"[done]"}\n{"id":4,"type":"[info]"}\n', encoding="utf-8"
                )
            elif case == "malformed":
                (impl / "action-items.jsonl").write_text("not json", encoding="utf-8")
            elif case == "non-selection":
                q = {"question": "Recover?", "options": [{"label": "Retry"}, {"label": "Stop"}]}
            elif case == "other-event":
                extra = {"hook_event_name": "PostToolUse"}
            elif case == "other-tool":
                extra = {"tool_name": "Bash"}
        assert _run(tmp_path, [q], **extra) is None

    def test_required_shared_exports_exist(self) -> None:
        """A renamed shared helper must not silently fail open at runtime."""
        source = HOOK.read_text(encoding="utf-8")
        match = re.search(r'const \{([^}]*)\} = require\("\./report-header-table\.js"\)', source)
        assert match is not None
        names = [n.strip() for n in match.group(1).split(",") if n.strip()]
        assert set(names) == {"previewCapProblem"}
        proc = subprocess.run(
            [
                "node",
                "-e",
                "const m=require(process.argv[1]);process.stdout.write(JSON.stringify(process.argv.slice(2).map(n=>typeof m[n])));",
                str(HOOK.parent / "report-header-table.js"),
                *names,
            ],
            capture_output=True,
            encoding="utf-8",
            timeout=10,
        )
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout) == ["function"] * len(names)

"""Exercise the resolve picker's question-text contract through the real Node hook."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[2] / "hooks/enforce-resolve-table.js"
CSID = "test-session-5678"
_skip_node_unavailable = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
_BULK = {
    "question": "#1 [report] guard keeps view\n#2 [gh][suggest] add test",
    "header": "Bulk action",
    "multiSelect": False,
    "options": [
        {"label": "+All [req]", "description": "Queue the required items"},
        {"label": "+All [suggest]", "description": "Queue the suggested items"},
        {"label": "ALL (req + suggest)", "description": "Queue all pending items"},
        {"label": "Skip all", "description": "Skip the items and exit"},
    ],
}
_PENDING = (
    {"id": 1, "type": "[report]", "summary": "guard keeps view", "severity": "high"},
    {"id": 2, "type": "[gh][suggest]", "summary": "add test", "severity": "low"},
    {"id": 3, "type": "[done]"},
)
_TABLE = (
    "| # | Type | Summary |\n| -- | -- | -- |\n| 1 | [report] | guard keeps view |\n| 2 | [gh][suggest] | add test |"
)


def _items(tmp_path: Path, *rows: dict) -> Path:
    """Write a real items file and the session-scoped sentinel."""
    impl = tmp_path / "impl"
    impl.mkdir()
    (impl / "action-items.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8", newline="\n"
    )
    (tmp_path / f"resolve-impl-dir-{CSID}").write_text(f"{impl}\n", encoding="utf-8", newline="\n")
    return impl


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
    """Create an over-budget real case with every id in the saved table and top ids in Q1."""
    items = [
        {"id": n, "type": "[gh][suggest]", "severity": "high" if n == 1 else "low", "summary": "x" * 50}
        for n in range(1, rows + 1)
    ]
    impl = _items(tmp_path, *reversed(items))
    file = impl / "action-items-table.md"
    file.write_text(
        "| # | Type | Summary |\n| -- | -- | -- |\n"
        + "".join(f"| {i['id']} | {i['type']} | {i['summary']} |\n" for i in items),
        encoding="utf-8",
        newline="\n",
    )
    text = f"#1 [gh][suggest] {'x' * 50}\nPending: [gh][suggest] {rows}\n→ Full item table: {file}"
    return impl, {**_BULK, "question": text}


@_skip_node_unavailable
class TestSelectionGate:
    def test_all_pending_ids_in_first_question_pass_without_previews(self, tmp_path: Path) -> None:
        """The user sees every pending item before selecting a bulk option."""
        _items(tmp_path, *_PENDING)
        assert _run(tmp_path, [_BULK]) is None

    def test_missing_id_denial_and_question_text_retry(self, tmp_path: Path) -> None:
        """The remedy names the missing id and accepts the corrected first question."""
        _items(tmp_path, *_PENDING)
        reason = _run(tmp_path, [{**_BULK, "question": "#1 [report] guard keeps view"}])
        assert reason is not None
        assert "#2" in reason
        assert "question text" in reason
        assert "1500" in reason
        assert "`preview` of every option" not in reason
        assert _run(tmp_path, [_BULK]) is None

    @pytest.mark.parametrize("field", ["description", "preview", "later-question", "question-description"])
    def test_ids_in_other_fields_never_supply_q1_items(self, tmp_path: Path, field: str) -> None:
        """Descriptions, previews and later questions cannot hide the selection list."""
        _items(tmp_path, *_PENDING)
        question = {**_BULK, "question": "Choose items"}
        questions = [question]
        if field in ("description", "preview"):
            question["options"] = [{**o, field: _BULK["question"]} for o in question["options"]]
        elif field == "later-question":
            questions.append(_BULK)
        else:
            question["description"] = _BULK["question"]
        assert _run(tmp_path, questions) is not None

    @pytest.mark.parametrize("kind", ["text", "thinking"])
    def test_transcript_items_do_not_supply_the_question(self, tmp_path: Path, kind: str) -> None:
        """Earlier reply text and thinking never replace the Q1 item list."""
        _items(tmp_path, *_PENDING)
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            json.dumps({"type": "assistant", "message": {"content": [{"type": kind, kind: _TABLE}]}}), encoding="utf-8"
        )
        assert _run(tmp_path, [{**_BULK, "question": "Choose"}], transcript_path=str(transcript)) is not None

    def test_denial_does_not_wait_for_transcript(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A flush wait cannot delay a gate that only reads the call and items file."""
        _items(tmp_path, *_PENDING)
        monkeypatch.setenv("CLAUDE_GATE_FLUSH_WAIT_MS", "10000")
        start = time.monotonic()
        assert _run(tmp_path, [{**_BULK, "question": "Choose"}]) is not None
        assert time.monotonic() - start < 3

    @pytest.mark.parametrize("text", ["#10 [report] wrong id\n#2 [gh][suggest] add test", "Pending: 2", _TABLE])
    def test_prefix_ids_counts_and_tables_do_not_supply_item_lines(self, tmp_path: Path, text: str) -> None:
        """A numeric prefix, bare count or Markdown table must not authorize missing item lines."""
        _items(tmp_path, *_PENDING)
        assert _run(tmp_path, [{**_BULK, "question": text}]) is not None

    def test_split_ids_across_fields_do_not_add_up(self, tmp_path: Path) -> None:
        """Each missing Q1 item remains missing even if an option mentions it."""
        _items(tmp_path, *_PENDING)
        q = {**_BULK, "question": "#1 [report] guard keeps view", "description": "#2 [gh][suggest] add test"}
        assert _run(tmp_path, [q]) is not None

    def test_multiselect_bulk_is_denied(self, tmp_path: Path) -> None:
        """The bulk page remains the first, single-select question."""
        _items(tmp_path, *_PENDING)
        reason = _run(tmp_path, [{**_BULK, "multiSelect": True}])
        assert reason is not None
        assert "single-select" in reason

    @pytest.mark.parametrize("wrapper", ["<pre>{}</pre>", "<div>{}</div>", "<table>{}</table>"])
    def test_html_item_list_is_denied(self, tmp_path: Path, wrapper: str) -> None:
        """HTML cannot substitute for plain question text."""
        _items(tmp_path, *_PENDING)
        assert _run(tmp_path, [{**_BULK, "question": wrapper.format(_BULK["question"])}]) is not None


@_skip_node_unavailable
class TestLimits:
    @pytest.mark.parametrize(
        ("length", "allowed"), [pytest.param(1500, True, id="at-cap"), pytest.param(1501, False, id="over-cap")]
    )
    def test_question_character_budget(self, tmp_path: Path, length: int, allowed: bool) -> None:
        """The entire question, including hints, has a hard 1500-character cap."""
        _items(tmp_path, *_PENDING)
        text = _BULK["question"] + "\n" + "x" * (length - len(_BULK["question"]) - 1)
        reason = _run(tmp_path, [{**_BULK, "question": text}])
        assert (reason is None) is allowed
        if not allowed:
            assert "1500" in reason

    @pytest.mark.parametrize(
        "preview", [pytest.param("Queue these items", id="compact-summary"), pytest.param(_TABLE, id="legacy-table")]
    )
    def test_identical_previews_are_denied_even_with_every_id_in_q1(self, tmp_path: Path, preview: str) -> None:
        """Four identical compact summaries repeat the reported regression."""
        _items(tmp_path, *_PENDING)
        reason = _run(tmp_path, [_previews(_BULK, *([preview] * 4))])
        assert reason is not None
        assert "identical previews" in reason

    def test_identical_previews_in_separate_questions_are_denied(self, tmp_path: Path) -> None:
        """A later question cannot repeat another option's comparison."""
        _items(tmp_path, *_PENDING)
        bulk = _previews(_BULK, "Required", "Suggestions", "All", "Skip")
        reason = _run(tmp_path, [bulk, {"question": "Commit mode?", "options": [{"preview": "Required"}]}])
        assert reason is not None
        assert "identical previews" in reason

    def test_short_distinct_plain_comparisons_pass(self, tmp_path: Path) -> None:
        """Optional distinct comparisons do not deliver items and stay within the host cap."""
        _items(tmp_path, *_PENDING)
        assert _run(tmp_path, [_previews(_BULK, "Required only", "Suggestions only", "All pending", "Skip")]) is None

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
        """A valid item list cannot excuse a withheld, clipped, table or HTML preview."""
        _items(tmp_path, *_PENDING)
        assert _run(tmp_path, [_previews(_BULK, preview, "Suggestions", "All", "Skip")]) is not None

    @pytest.mark.parametrize(
        ("lines", "allowed"), [pytest.param(12, True, id="line-cap"), pytest.param(13, False, id="line-over")]
    )
    def test_optional_comparison_line_boundary(self, tmp_path: Path, lines: int, allowed: bool) -> None:
        """The host counts twelve short rendered lines, including wrapping."""
        _items(tmp_path, *_PENDING)
        reason = _run(tmp_path, [_previews(_BULK, "\n".join(["Compare"] * lines), "Suggestions", "All", "Skip")])
        assert (reason is None) is allowed

    def test_later_option_preview_is_checked(self, tmp_path: Path) -> None:
        """Limits apply to the whole selection call, including checkbox questions."""
        _items(tmp_path, *_PENDING)
        assert _run(tmp_path, [_BULK, {"question": "Items", "options": [{"preview": "x" * 2001}]}]) is not None


@_skip_node_unavailable
class TestOverBudget:
    def test_top_items_counts_and_file_pass_without_previews(self, tmp_path: Path) -> None:
        """A large list names the real complete table, type counts and highest-severity item."""
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
            "missing-file",
            "directory",
            "missing-id",
            "no-path",
            "wrong-count",
            "no-count",
            "no-top",
            "wrong-top",
            "oversize-file",
        ],
    )
    def test_incomplete_over_budget_form_is_denied(self, tmp_path: Path, fault: str) -> None:
        """A file fallback still supplies concrete selection context and the complete saved table."""
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
            replacements = {
                "no-path": (str(file), "elsewhere.md"),
                "wrong-count": ("suggest] 30", "suggest] 29"),
                "no-count": ("Pending: [gh][suggest] 30\n", ""),
                "no-top": (q["question"].splitlines()[0] + "\n", ""),
                "wrong-top": ("#1 ", "#2 "),
            }
            old, new = replacements[fault]
            q = {**q, "question": q["question"].replace(old, new)}
        assert _run(tmp_path, [q]) is not None

    def test_small_list_cannot_use_file_to_hide_an_item(self, tmp_path: Path) -> None:
        """Fallback is allowed by the whole-question budget, not merely by a table's length."""
        impl, q = _large(tmp_path, rows=2)
        reason = _run(tmp_path, [q])
        assert reason is not None
        assert "#2" in reason
        assert str(impl / "action-items-table.md") in reason

    def test_counts_cover_each_type(self, tmp_path: Path) -> None:
        """An over-budget summary cannot omit a pending type's count."""
        impl, q = _large(tmp_path)
        with (impl / "action-items.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"id": 31, "type": "[report]", "severity": "low", "summary": "more"}) + "\n")
        with (impl / "action-items-table.md").open("a", encoding="utf-8") as stream:
            stream.write("| 31 | [report] | more |\n")
        assert _run(tmp_path, [q]) is not None
        q = {**q, "question": q["question"].replace("suggest] 30", "suggest] 30 · [report] 1")}
        assert _run(tmp_path, [q]) is None


@_skip_node_unavailable
class TestClosedAndPassThrough:
    def test_wrong_closed_count_is_denied(self, tmp_path: Path) -> None:
        """A count with the correct number as a suffix must not hide incorrect closed-item context."""
        _items(tmp_path, *_PENDING, {"id": 4, "type": "[gh][req]", "status": "resolved"})
        text = _BULK["question"] + "\n→ 11 resolved/addressed items not in bulk options — type their ids to include"
        reason = _run(tmp_path, [{**_BULK, "question": text}])
        assert reason is not None
        assert "1 resolved/addressed items" in reason

    def test_missing_closed_count_is_denied(self, tmp_path: Path) -> None:
        """Closed rows remain selectable by typed id, so their count cannot disappear."""
        _items(tmp_path, *_PENDING, {"id": 4, "type": "[gh][req]", "status": "resolved"})
        reason = _run(tmp_path, [_BULK])
        assert reason is not None
        assert "1 resolved/addressed items" in reason

    def test_bulk_page_after_a_different_question_is_denied(self, tmp_path: Path) -> None:
        """A valid item list is insufficient if the bulk action is on a later tab."""
        _items(tmp_path, *_PENDING)
        first = {"question": _BULK["question"], "options": [{"label": "Each"}, {"label": "All"}]}
        reason = _run(tmp_path, [first, _BULK])
        assert reason is not None
        assert "bulk-action question first" in reason

    def test_closed_items_need_only_a_count(self, tmp_path: Path) -> None:
        """Closed rows no longer consume the pending-list budget."""
        _items(tmp_path, *_PENDING, {"id": 4, "type": "[gh][req]", "status": "resolved"})
        assert (
            _run(
                tmp_path,
                [
                    {
                        **_BULK,
                        "question": _BULK["question"]
                        + "\n→ 1 resolved/addressed items not in bulk options — type their ids to include",
                    }
                ],
            )
            is None
        )

    def test_closed_only_count_passes(self, tmp_path: Path) -> None:
        """A closed-only picker can name the count without listing closed ids."""
        _items(tmp_path, {"id": 4, "type": "[gh][req]", "status": "addressed"})
        assert (
            _run(
                tmp_path,
                [{**_BULK, "question": "→ 1 resolved/addressed items not in bulk options — type their ids to include"}],
            )
            is None
        )

    @pytest.mark.parametrize(
        "case", ["no-sentinel", "no-items", "done-info", "malformed", "non-selection", "other-event", "other-tool"]
    )
    def test_out_of_scope_or_unreadable_evidence_fails_open(self, tmp_path: Path, case: str) -> None:
        """Existing scoping and fail-open conventions keep recovery calls usable."""
        q = {**_BULK, "question": "Choose"}
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

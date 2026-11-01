"""Exercise the resolve picker's question-text contract through the real Node hook.

Every test spawns ``hooks/enforce-resolve-table.js`` under Node with a real items file and session sentinel; nothing is
replaced, because the hook's PreToolUse verdict (deny reason or silent pass) is the contract.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
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
_CLOSED_ROW = {"id": 4, "type": "[gh][req]", "status": "resolved"}
_CLOSED_NOTE = "→ 1 resolved/addressed items not in bulk options — type their ids to include"
_CHOOSE = {**_BULK, "question": "Choose"}


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


def _large_with_report_row(tmp_path: Path) -> tuple[Path, dict]:
    """Extend the over-budget case with one pending ``[report]`` item in both the items file and the saved table."""
    impl, q = _large(tmp_path)
    with (impl / "action-items.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"id": 31, "type": "[report]", "severity": "low", "summary": "more"}) + "\n")
    with (impl / "action-items-table.md").open("a", encoding="utf-8") as stream:
        stream.write("| 31 | [report] | more |\n")
    return impl, q


def _question_of_length(length: int) -> str:
    """Pad the valid bulk question with filler so the whole text is exactly ``length`` characters."""
    return _BULK["question"] + "\n" + "x" * (length - len(_BULK["question"]) - 1)


def _ids_in_option_field(field: str) -> list[dict]:
    """Return a bulk question whose item list sits only in each option's ``field``."""
    options = [{**o, field: _BULK["question"]} for o in _BULK["options"]]
    return [{**_BULK, "question": "Choose items", "options": options}]


def _ids_in_later_question() -> list[dict]:
    """Return a vague first question followed by the valid bulk question."""
    return [{**_BULK, "question": "Choose items"}, _BULK]


def _ids_in_question_description() -> list[dict]:
    """Return a vague bulk question whose item list sits only in its ``description``."""
    return [{**_BULK, "question": "Choose items", "description": _BULK["question"]}]


@_skip_node_unavailable
class TestSelectionGate:
    """The first question must carry every pending id as plain question text."""

    def test_all_pending_ids_in_first_question_pass_without_previews(self, tmp_path: Path) -> None:
        """The user sees every pending item before selecting a bulk option.

        With each pending id listed in the first question's text, no option needs a preview and the hook stays silent.
        """
        _items(tmp_path, *_PENDING)
        assert _run(tmp_path, [_BULK]) is None

    def test_missing_id_denial_names_the_remedy(self, tmp_path: Path) -> None:
        """A first question missing an id is denied with a reason naming that id and the question-text remedy.

        The reason must point at the question text and its 1500-character cap, not at option previews, so the retry
        moves the list into the text.
        """
        _items(tmp_path, *_PENDING)
        reason = _run(tmp_path, [{**_BULK, "question": "#1 [report] guard keeps view"}])
        assert reason is not None
        assert "#2" in reason
        assert "question text" in reason
        assert "1500" in reason
        assert "`preview` of every option" not in reason

    @pytest.mark.parametrize(
        "questions",
        [
            pytest.param(lambda: _ids_in_option_field("description"), id="description"),
            pytest.param(lambda: _ids_in_option_field("preview"), id="preview"),
            pytest.param(_ids_in_later_question, id="later-question"),
            pytest.param(_ids_in_question_description, id="question-description"),
        ],
    )
    def test_ids_in_other_fields_never_supply_q1_items(
        self, tmp_path: Path, questions: Callable[[], list[dict]]
    ) -> None:
        """Descriptions, previews and later questions cannot hide the selection list.

        The item list is present in the call, but not in the first question's own text, so it must still be denied.
        """
        _items(tmp_path, *_PENDING)
        assert _run(tmp_path, questions()) is not None

    @pytest.mark.parametrize("kind", ["text", "thinking"])
    def test_transcript_items_do_not_supply_the_question(self, tmp_path: Path, kind: str) -> None:
        """Earlier reply text and thinking never replace the Q1 item list.

        A transcript holding the full table is supplied, yet the vague first question is still denied.
        """
        _items(tmp_path, *_PENDING)
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            json.dumps({"type": "assistant", "message": {"content": [{"type": kind, kind: _TABLE}]}}), encoding="utf-8"
        )
        assert _run(tmp_path, [{**_BULK, "question": "Choose"}], transcript_path=str(transcript)) is not None

    def test_denial_does_not_wait_for_transcript(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A flush wait cannot delay a gate that only reads the call and items file.

        The wait is set far beyond ``_run``'s 10 s subprocess timeout, so a hook that waited would raise
        ``TimeoutExpired`` here instead of returning its denial; no wall-clock threshold is needed.
        """
        _items(tmp_path, *_PENDING)
        monkeypatch.setenv("CLAUDE_GATE_FLUSH_WAIT_MS", "600000")
        assert _run(tmp_path, [_CHOOSE]) is not None

    @pytest.mark.parametrize("text", ["#10 [report] wrong id\n#2 [gh][suggest] add test", "Pending: 2", _TABLE])
    def test_prefix_ids_counts_and_tables_do_not_supply_item_lines(self, tmp_path: Path, text: str) -> None:
        """A numeric prefix, bare count or Markdown table must not authorize missing item lines."""
        _items(tmp_path, *_PENDING)
        assert _run(tmp_path, [{**_BULK, "question": text}]) is not None

    def test_split_ids_across_fields_do_not_add_up(self, tmp_path: Path) -> None:
        """Each missing Q1 item remains missing even if an option mentions it.

        Id 1 is in the question and id 2 only in the question's description; neither field alone is complete.
        """
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
    """Whole-call limits: question length, preview size, and identical or rich previews."""

    def test_question_at_character_cap_passes(self, tmp_path: Path) -> None:
        """A question of exactly 1500 characters, hints included, is accepted."""
        _items(tmp_path, *_PENDING)
        assert _run(tmp_path, [{**_BULK, "question": _question_of_length(1500)}]) is None

    def test_question_over_character_cap_is_denied(self, tmp_path: Path) -> None:
        """A question of 1501 characters is denied and the reason names the 1500-character cap."""
        _items(tmp_path, *_PENDING)
        reason = _run(tmp_path, [{**_BULK, "question": _question_of_length(1501)}])
        assert reason is not None
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


def _fault_missing_file(file: Path, q: dict) -> dict:
    """Delete the saved table the question points at."""
    file.unlink()
    return q


def _fault_directory(file: Path, q: dict) -> dict:
    """Replace the saved table with a directory of the same name."""
    file.unlink()
    file.mkdir()
    return q


def _fault_missing_id(file: Path, q: dict) -> dict:
    """Drop id 30 from the saved table so it no longer lists every pending item."""
    file.write_text(file.read_text(encoding="utf-8").replace("| 30 |", "| 31 |"), encoding="utf-8")
    return q


def _fault_oversize_file(file: Path, q: dict) -> dict:
    """Grow the saved table past the 2 MiB read cap."""
    file.write_text("x" * (2 * 1024 * 1024 + 1), encoding="utf-8")
    return q


def _swap_in_question(old: str, new: str) -> Callable[[Path, dict], dict]:
    """Return a fault that rewrites ``old`` to ``new`` in the question text."""

    def fault(_file: Path, q: dict) -> dict:
        """Rewrite the question text."""
        return {**q, "question": q["question"].replace(old, new)}

    return fault


@_skip_node_unavailable
class TestOverBudget:
    """A pending list too large for the question falls back to top items, counts and a saved full table."""

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
            pytest.param(_fault_missing_file, id="missing-file"),
            pytest.param(_fault_directory, id="directory"),
            pytest.param(_fault_missing_id, id="missing-id"),
            pytest.param(
                lambda file, q: {**q, "question": q["question"].replace(str(file), "elsewhere.md")}, id="no-path"
            ),
            pytest.param(_swap_in_question("suggest] 30", "suggest] 29"), id="wrong-count"),
            pytest.param(_swap_in_question("Pending: [gh][suggest] 30\n", ""), id="no-count"),
            pytest.param(
                lambda _file, q: {**q, "question": q["question"].replace(q["question"].splitlines()[0] + "\n", "")},
                id="no-top",
            ),
            pytest.param(_swap_in_question("#1 ", "#2 "), id="wrong-top"),
            pytest.param(_fault_oversize_file, id="oversize-file"),
        ],
    )
    def test_incomplete_over_budget_form_is_denied(self, tmp_path: Path, fault: Callable[[Path, dict], dict]) -> None:
        """A file fallback still supplies concrete selection context and the complete saved table.

        Each fault breaks one required part: the saved table (absent, a directory, missing an id, over the size cap),
        or the question's path, per-type count, or top item.
        """
        impl, q = _large(tmp_path)
        q = fault(impl / "action-items-table.md", q)
        assert _run(tmp_path, [q]) is not None

    def test_small_list_cannot_use_file_to_hide_an_item(self, tmp_path: Path) -> None:
        """Fallback is allowed by the whole-question budget, not merely by a table's length.

        Only two items exist, so they fit in the question; pointing at the file instead is denied and names the item.
        """
        impl, q = _large(tmp_path, rows=2)
        reason = _run(tmp_path, [q])
        assert reason is not None
        assert "#2" in reason
        assert str(impl / "action-items-table.md") in reason

    def test_summary_omitting_a_pending_type_count_is_denied(self, tmp_path: Path) -> None:
        """An over-budget summary cannot omit a pending type's count.

        A ``[report]`` item exists besides the 30 ``[gh][suggest]`` ones, but the count line mentions only the latter.
        """
        _, q = _large_with_report_row(tmp_path)
        assert _run(tmp_path, [q]) is not None

    def test_summary_counting_every_pending_type_passes(self, tmp_path: Path) -> None:
        """An over-budget summary that counts each pending type is accepted."""
        _, q = _large_with_report_row(tmp_path)
        q = {**q, "question": q["question"].replace("suggest] 30", "suggest] 30 · [report] 1")}
        assert _run(tmp_path, [q]) is None


def _setup_pending(tmp_path: Path) -> None:
    """Publish the standard pending items and sentinel."""
    _items(tmp_path, *_PENDING)


def _setup_no_items_file(tmp_path: Path) -> None:
    """Publish the sentinel but delete the items file."""
    (_items(tmp_path, *_PENDING) / "action-items.jsonl").unlink()


def _setup_items_text(text: str) -> Callable[[Path], None]:
    """Return a setup that publishes the sentinel with an items file holding exactly ``text``."""

    def setup(tmp_path: Path) -> None:
        """Write the items file content."""
        (_items(tmp_path, *_PENDING) / "action-items.jsonl").write_text(text, encoding="utf-8")

    return setup


@_skip_node_unavailable
class TestClosedAndPassThrough:
    """Closed-item counts, bulk-page position, and fail-open scoping."""

    def test_wrong_closed_count_is_denied(self, tmp_path: Path) -> None:
        """A count with the correct number as a suffix must not hide incorrect closed-item context."""
        _items(tmp_path, *_PENDING, _CLOSED_ROW)
        text = _BULK["question"] + "\n→ 11 resolved/addressed items not in bulk options — type their ids to include"
        reason = _run(tmp_path, [{**_BULK, "question": text}])
        assert reason is not None
        assert "1 resolved/addressed items" in reason

    def test_missing_closed_count_is_denied(self, tmp_path: Path) -> None:
        """Closed rows remain selectable by typed id, so their count cannot disappear."""
        _items(tmp_path, *_PENDING, _CLOSED_ROW)
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
        _items(tmp_path, *_PENDING, _CLOSED_ROW)
        assert _run(tmp_path, [{**_BULK, "question": _BULK["question"] + "\n" + _CLOSED_NOTE}]) is None

    def test_closed_only_count_passes(self, tmp_path: Path) -> None:
        """A closed-only picker can name the count without listing closed ids."""
        _items(tmp_path, {"id": 4, "type": "[gh][req]", "status": "addressed"})
        assert _run(tmp_path, [{**_BULK, "question": _CLOSED_NOTE}]) is None

    @pytest.mark.parametrize(
        ("setup", "questions", "extra"),
        [
            pytest.param(lambda _tmp: None, [_CHOOSE], {}, id="no-sentinel"),
            pytest.param(_setup_no_items_file, [_CHOOSE], {}, id="no-items"),
            pytest.param(
                _setup_items_text('{"id":3,"type":"[done]"}\n{"id":4,"type":"[info]"}\n'),
                [_CHOOSE],
                {},
                id="done-info",
            ),
            pytest.param(_setup_items_text("not json"), [_CHOOSE], {}, id="malformed"),
            pytest.param(
                _setup_pending,
                [{"question": "Recover?", "options": [{"label": "Retry"}, {"label": "Stop"}]}],
                {},
                id="non-selection",
            ),
            pytest.param(_setup_pending, [_CHOOSE], {"hook_event_name": "PostToolUse"}, id="other-event"),
            pytest.param(_setup_pending, [_CHOOSE], {"tool_name": "Bash"}, id="other-tool"),
        ],
    )
    def test_out_of_scope_or_unreadable_evidence_fails_open(
        self, tmp_path: Path, setup: Callable[[Path], object], questions: list[dict], extra: dict
    ) -> None:
        """Existing scoping and fail-open conventions keep recovery calls usable.

        With no sentinel, a missing, empty-of-pending or malformed items file, a non-selection question, or an event or
        tool the hook does not own, the hook must not block.
        """
        setup(tmp_path)
        assert _run(tmp_path, questions, **extra) is None


class TestSharedHelperContract:
    """The hook's imports from its shared helper module resolve at runtime."""

    @_skip_node_unavailable
    def test_required_shared_exports_exist(self) -> None:
        """A renamed shared helper must not silently fail open at runtime.

        The hook destructures ``previewCapProblem`` from ``report-header-table.js``; if that export disappears the
        destructured value is ``undefined`` and the check quietly stops applying. The source is JavaScript, so the
        single-line ``require`` destructuring is matched with a regex and each name is then checked under real Node.
        """
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

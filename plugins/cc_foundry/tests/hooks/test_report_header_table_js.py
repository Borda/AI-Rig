"""Unit tests for ``hooks/report-header-table.js``, the table-format detector shared by all six ``enforce-*-header.js``
hooks (see propagate_shared.py MANIFEST — canonical here, byte-identical copies in cc_oss, cc_develop, cc_research).

Covers the exports in isolation, independent of any single hook's
sentinel/report-dir wiring:

* ``hasHeaderTable`` — pipe-table detection (header + separator + >= MIN_TABLE_ROWS
  data rows) and the documented ``·``-separated one-line fallback.
* ``finalReplyText`` — bounded tail-read of a JSONL transcript returning only the
  turn's final reply: assistant text after the last tool call, skipping non-turn
  rows and sidechain (subagent) output.
* question-time delivery (``questionDeliveryProblem``, ``questionDelivers``,
  ``misplacementNote``) — first-question placement, html previews, inline-markdown
  tolerant cells, and the near-match remedy that names which values differ.
* the preview cap (``previewCapProblem``, ``cappedQuestionDelivers``, ``fileDelivery``) —
  a preview over 2000 chars or 12 lines never delivers; over-cap content passes as a named
  file holding it plus a compact summary naming that path in every option preview.
* Stop-time delivery (``deliveryProblem``, ``stopBlockReason``) — judged on the final
  reply only, at most once per report version.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parent.parent.parent / "hooks" / "report-header-table.js"

NODE_UNAVAILABLE = shutil.which("node") is None


def _call(name: str, *args: object) -> object:
    """Call one export of report-header-table.js in a separate Node process."""
    proc = subprocess.run(
        [
            "node",
            "-e",
            "const mod = require(process.argv[1]); process.stdout.write(JSON.stringify(mod[process.argv[2]](...JSON.parse(process.argv[3]))));",
            str(MODULE),
            name,
            json.dumps(args),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return json.loads(proc.stdout)


# ── hasHeaderTable ─────────────────────────────────────────────────────────


_skip_node_unavailable = pytest.mark.skipif(
    NODE_UNAVAILABLE,
    reason="requires node to execute the module",
)


@_skip_node_unavailable
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            "| Field | Value |\n| --- | --- |\n| Title | x |\n| PR | #1 |\n| Date | 2026-08-08 |\n",
            True,
            id="pipe-table-with-enough-rows",
        ),
        pytest.param("Title: oss-review\nPR: #1303\nDate: 2026-08-08\n", False, id="raw-yaml-fields-without-pipes"),
        pytest.param("| Field | Value |\n| --- | --- |\n| Title | x |\n", False, id="table-below-min-rows"),
        pytest.param("verdict: APPROVE · findings: 3 · file: review-report.md", True, id="fallback-dot-separated-line"),
        pytest.param("", False, id="empty-text"),
    ],
)
def test_has_header_table_detection(text: str, expected: bool) -> None:
    """A rendered header table, or the documented one-line fallback, is detected; anything else is not.

    Scenario: a `| Field | Value |` table with >= MIN_TABLE_ROWS data rows counts; SKILL.md's `·`-separated one-line
    fallback (used when the report read fails) also counts; raw fields printed one per line with no table (the exact
    failure this module guards against), fewer than MIN_TABLE_ROWS data rows (stray prose pipes) and empty text (an
    unreadable transcript) are never mistaken for a printed table.
    """
    assert _call("hasHeaderTable", text) is expected


@_skip_node_unavailable
@pytest.mark.parametrize(
    ("report_text", "text"),
    [
        pytest.param(
            "---\nTitle: Current review\nOutcome: PASS\nSummary: Verified result\n---\n", "", id="absent-table"
        ),
        pytest.param(
            "---\nTitle: Current review\nOutcome: PASS\nSummary: Verified result\n---\n",
            "| Field | Value |\n| --- | --- |\n| Title | unrelated |\n| Outcome | PASS |\n| Summary | old |",
            id="unrelated-table",
        ),
        pytest.param(
            "---\nTitle: Current\nOutcome: PASS\nSummary: Verified\n---\n",
            "Title Current Outcome PASS Summary Verified\n| Field | Value |\n| --- | --- |\n| Title | Other |\n| Outcome | PASS |\n| Summary | Stale |",
            id="raw-fields-plus-unrelated-table",
        ),
    ],
)
def test_delivery_rejects_absent_or_unrelated_table(tmp_path: Path, report_text: str, text: str) -> None:
    """File presence and another report's table cannot establish this report's delivery.

    Scenario: an absent table, a table of another report, and header fields that occur as prose beside an unrelated
    table all fail, because header fields must occur as rows in one matching table.
    """
    report = tmp_path / "report.md"
    report.write_text(report_text, encoding="utf-8")
    transcript = _write_transcript(
        tmp_path, [{"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}]
    )
    assert _call("deliveryProblem", str(report), str(transcript)) is not None


@_skip_node_unavailable
def test_delivery_problem_reports_zero_visible_chars_for_thinking_only_report(tmp_path: Path) -> None:
    """A header table written only in thinking is rejected, and the reason reports 0 visible reply chars.

    Observed in a resolve run: the model "printed" its table inside reasoning, got denied four times, and blamed the
    hook. The char count plus the thinking note tells it the problem is its own reply, not the gate.
    """
    report = tmp_path / "report.md"
    report.write_text("---\nTitle: Current review\nOutcome: PASS\nSummary: Verified result\n---\n", encoding="utf-8")
    table = "| Field | Value |\n| --- | --- |\n| Title | Current review |\n| Outcome | PASS |\n| Summary | Verified result |"
    transcript = _write_transcript(
        tmp_path, [{"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": table}]}}]
    )
    problem = _call("deliveryProblem", str(report), str(transcript))
    assert isinstance(problem, str)
    assert "0 chars" in problem
    assert "thinking" in problem


_HEADER_REPORT = "---\nTitle: Current review\nOutcome: PASS\nSummary: Verified result\n---\n"
_HEADER_TABLE = (
    "| Field | Value |\n| --- | --- |\n| Title | Current review |\n| Outcome | PASS |\n| Summary | Verified result |"
)


@_skip_node_unavailable
def test_delivery_accepts_bound_header(tmp_path: Path) -> None:
    """The current complete header table permits the follow-up transition."""
    report = tmp_path / "report.md"
    report.write_text("---\nTitle: Current review\nOutcome: PASS\nSummary: Verified result\n---\n", encoding="utf-8")
    text = "| Field | Value |\n| --- | --- |\n| Title | Current review |\n| Outcome | PASS |\n| Summary | Verified result |"
    transcript = _write_transcript(
        tmp_path, [{"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}]
    )
    assert _call("deliveryProblem", str(report), str(transcript)) is None


def _question(preview: str | None, *, multi_select: bool = False, description: str = "fix it") -> dict:
    """Build a one-question AskUserQuestion input whose two options both carry `preview` (when given)."""
    options = [{"label": label, "description": description} for label in ("/oss:resolve 1", "skip")]
    if preview is not None:
        for option in options:
            option["preview"] = preview
    return {
        "questions": [
            {"question": "What next?", "header": "oss-review", "multiSelect": multi_select, "options": options}
        ]
    }


class TestQuestionTexts:
    """``questionTexts`` lists what the follow-up question itself shows the user."""

    @_skip_node_unavailable
    def test_collects_question_descriptions_and_previews(self) -> None:
        """Question text, every option description and every single-select preview are returned in order."""
        texts = _call("questionTexts", _question("TABLE"))

        assert texts == ["What next?", "fix it", "TABLE", "fix it", "TABLE"]

    @_skip_node_unavailable
    def test_collects_the_question_level_description(self) -> None:
        """A form-style question's own `description` helper line is shown, so it is listed right after its text."""
        tool_input = _question(None)
        tool_input["questions"][0]["description"] = "HELPER"

        assert _call("questionTexts", tool_input) == ["What next?", "HELPER", "fix it", "fix it"]

    @_skip_node_unavailable
    def test_drops_previews_of_multi_select_questions(self) -> None:
        """A multi-select question never renders previews, so its preview text is no delivery evidence."""
        assert "TABLE" not in _call("questionTexts", _question("TABLE", multi_select=True))

    @_skip_node_unavailable
    @pytest.mark.parametrize("tool_input", [None, {}, {"questions": "x"}, {"questions": [None, {"options": [None]}]}])
    def test_malformed_input_yields_no_text(self, tool_input: object) -> None:
        """Any unexpected AskUserQuestion shape reads as "nothing shown", never a crash."""
        assert _call("questionTexts", tool_input) == []


class TestDeliveryViaQuestion:
    """At question time only the call's own fields deliver the report; a reply-text table never does.

    On models that return text written before a tool call as an empty progress-update thinking block, the question's own
    preview is the only copy of the table the user is sure to see, so the gate judges the call and never the transcript.
    """

    @_skip_node_unavailable
    def test_preview_table_allows(self, tmp_path: Path) -> None:
        """The current header as every option's preview passes with no reply text at all."""
        report = tmp_path / "report.md"
        report.write_text(_HEADER_REPORT, encoding="utf-8")

        assert _call("questionDeliveryProblem", str(report), _question(_HEADER_TABLE)) is None

    @_skip_node_unavailable
    def test_stale_preview_is_denied_and_names_the_preview_fix(self, tmp_path: Path) -> None:
        """A preview of another report version is no delivery; the reason names the differing value and the preview fix.

        Saying "no field holds the table" here was wrong — the previews hold a table, just not this report's — and sent
        the model back with the same table.
        """
        report = tmp_path / "report.md"
        report.write_text(_HEADER_REPORT, encoding="utf-8")

        problem = _call("questionDeliveryProblem", str(report), _question(_HEADER_TABLE.replace("PASS", "FAIL")))

        assert isinstance(problem, str)
        assert problem.startswith("current report header was not delivered by this question")
        assert "`Outcome` reads `FAIL` but the file has `PASS`" in problem
        assert "do not hold it either" not in problem
        assert "`preview` of every option" in problem
        assert "Re-issue" not in problem

    @_skip_node_unavailable
    def test_call_without_any_table_says_no_field_holds_it(self, tmp_path: Path) -> None:
        """With no table anywhere in the call, the reason says so instead of naming a near-match."""
        report = tmp_path / "report.md"
        report.write_text(_HEADER_REPORT, encoding="utf-8")

        problem = _call("questionDeliveryProblem", str(report), _question(None))

        assert isinstance(problem, str)
        assert "previews do not hold it either" in problem
        assert "does not match the report file" not in problem

    @_skip_node_unavailable
    def test_reply_text_table_is_denied_and_the_preview_fix_passes(
        self, tmp_path: Path, follow_up_run: tuple[Path, Path]
    ) -> None:
        """A complete reply-text table before the question does not satisfy the gate; the named preview fix does.

        The table can sit in the transcript and still never have reached the user (an empty progress update), so the
        follow-up is denied with the reason that reply text does not count and the one accepted placement; the call so
        corrected passes with no reply text at all — denial then retry is one lockout-freedom contract.
        """
        sentinel, report = follow_up_run
        transcript = _write_transcript(
            tmp_path, [{"type": "assistant", "message": {"content": [{"type": "text", "text": _HEADER_TABLE}]}}]
        )
        replied = {"transcript_path": str(transcript), "tool_input": _question(None)}
        corrected = {"transcript_path": None, "tool_input": _question(_HEADER_TABLE)}

        denied = _call("followUpProblem", str(sentinel), str(report), replied)
        retried = _call("followUpProblem", str(sentinel), str(report), corrected)

        assert isinstance(denied, str)
        assert "reply text before the call does not count" in denied
        assert "`preview` of every option of the call's first question (single-select)" in denied
        assert retried is None

    @_skip_node_unavailable
    def test_partial_tables_in_separate_fields_never_combine(self, tmp_path: Path) -> None:
        """Half the rows in the question text and half in the previews is not one complete table."""
        report = tmp_path / "report.md"
        report.write_text(_HEADER_REPORT, encoding="utf-8")
        head = "| Field | Value |\n| --- | --- |\n"
        preview = head + "| Outcome | PASS |\n| Summary | Verified result |"
        split = {"questions": [{**_question(preview)["questions"][0], "question": head + "| Title | Current review |"}]}

        assert _call("questionDeliveryProblem", str(report), split) is not None

    @_skip_node_unavailable
    def test_incomplete_report_wins_over_matching_preview(self, tmp_path: Path) -> None:
        """A preview cannot authorize a follow-up on a report whose own header is unfinished."""
        report = tmp_path / "report.md"
        report.write_text("---\nTitle: Current review\nOutcome: PASS\n---\n", encoding="utf-8")

        problem = _call("questionDeliveryProblem", str(report), _question(_HEADER_TABLE))

        assert problem == "report header is incomplete; finish the report before following up"

    @_skip_node_unavailable
    def test_audit_findings_in_preview_allow(self, tmp_path: Path) -> None:
        """Audit's Step 7 findings report carried as a preview satisfies its aggregate-bound check."""
        report = tmp_path / "summary.jsonl"
        report.write_text(json.dumps({"sev": "high", "one_line": "broken ref"}) + "\n", encoding="utf-8")

        assert _call("questionDeliveryProblem", str(report), _question("## Audit Report\nTotal: 1\nbroken ref")) is None


def _follow_up(previews: list[str | None], *, multi_select: bool = False) -> dict:
    """One follow-up question whose n-th option carries ``previews[n]`` as its `preview` (no preview when None)."""
    options = [{"label": f"option {index}", "description": "next step"} for index in range(1, len(previews) + 1)]
    for option, preview in zip(options, previews):
        if preview is not None:
            option["preview"] = preview
    return {"question": "What next?", "header": "oss-review", "multiSelect": multi_select, "options": options}


#: A question asked beside the follow-up that carries no part of the report.
_UNRELATED = {
    "question": "Which branch?",
    "header": "branch",
    "options": [{"label": "main", "description": "default"}, {"label": "dev", "description": "feature work"}],
}


#: Phrase naming a copy in an option `description`, which Claude Code renders as one line.
_DESCRIPTION_SPOT = "It sits in an option `description`, which Claude Code renders as one line"


class TestFirstQuestionPlacement:
    """The call delivers only through its first question: its question text, or every option's preview.

    The picker opens on question 1 and renders only the focused option's preview, so a table in some previews, in a
    later question, or in a multiSelect question's (never rendered) previews lets the user answer before seeing it.
    Claude Code renders every option description as one line, its line breaks replaced, so a table there is never
    readable. The deny reason names where the misplaced copy sits, so applying its remedy once passes — no denial loop.
    """

    @_skip_node_unavailable
    @pytest.mark.parametrize(
        ("misplaced", "spot", "remedied"),
        [
            pytest.param(
                [_follow_up([None, None, _HEADER_TABLE])],
                "It sits in 1 of 3 option previews",
                [_follow_up([_HEADER_TABLE] * 3)],
                id="last-option-preview-only",
            ),
            pytest.param(
                [_UNRELATED, _follow_up([_HEADER_TABLE] * 2)],
                "It sits in question 2",
                [_follow_up([_HEADER_TABLE] * 2), _UNRELATED],
                id="previews-of-second-question",
            ),
            pytest.param(
                [_UNRELATED, {**_follow_up([None, None]), "question": _HEADER_TABLE}],
                "It sits in question 2",
                [{**_follow_up([None, None]), "question": _HEADER_TABLE}, _UNRELATED],
                id="text-of-second-question",
            ),
            pytest.param(
                [_follow_up([_HEADER_TABLE] * 2, multi_select=True)],
                "multiSelect question's previews, which are never rendered — move it to the previews of a single-select",
                [_follow_up([_HEADER_TABLE] * 2)],
                id="multi-select-previews",
            ),
            pytest.param(
                [_follow_up([f"<pre>{_HEADER_TABLE}</pre>", None])],
                "It sits in 1 of 2 option previews",
                [_follow_up([f"<pre>{_HEADER_TABLE}</pre>"] * 2)],
                id="html-first-option-preview-only",
            ),
            pytest.param(
                [{**_follow_up([None, None]), "description": _HEADER_TABLE}],
                "It sits in the question-level `description`, a single helper line",
                [_follow_up([_HEADER_TABLE] * 2)],
                id="form-question-level-description",
            ),
            pytest.param(
                [{**_follow_up([None]), "options": [{"label": "a", "description": _HEADER_TABLE}, {"label": "b"}]}],
                _DESCRIPTION_SPOT,
                [_follow_up([_HEADER_TABLE] * 2)],
                id="one-option-description",
            ),
            pytest.param(
                [{**_follow_up([None], multi_select=True), "options": [{"label": "a", "description": _HEADER_TABLE}]}],
                _DESCRIPTION_SPOT,
                [_follow_up([_HEADER_TABLE] * 2)],
                id="multi-select-description",
            ),
            pytest.param(
                [_UNRELATED, {**_follow_up([None]), "options": [{"label": "a", "description": _HEADER_TABLE}]}],
                _DESCRIPTION_SPOT,
                [_follow_up([_HEADER_TABLE] * 2), _UNRELATED],
                id="description-of-second-question",
            ),
        ],
    )
    def test_misplaced_table_is_denied_and_the_named_fix_passes(
        self, tmp_path: Path, misplaced: list[dict], spot: str, remedied: list[dict]
    ) -> None:
        """A misplaced table is denied with its spot and the exact placement named; the call so corrected passes.

        Denial followed by the remedied retry is one contract — the gate must never lock out a model that follows it.
        """
        report = tmp_path / "report.md"
        report.write_text(_HEADER_REPORT, encoding="utf-8")

        problem = _call("questionDeliveryProblem", str(report), {"questions": misplaced})
        retried = _call("questionDeliveryProblem", str(report), {"questions": remedied})

        assert isinstance(problem, str)
        assert spot in problem
        assert "`preview` of every option of the call's first question (single-select)" in problem
        assert retried is None

    @_skip_node_unavailable
    def test_first_question_text_delivers_on_its_own(self, tmp_path: Path) -> None:
        """Question text renders without focus and keeps its line breaks, so on question 1 it delivers alone."""
        report = tmp_path / "report.md"
        report.write_text(_HEADER_REPORT, encoding="utf-8")
        first = {**_follow_up([None, None]), "question": _HEADER_TABLE}

        assert _call("questionDeliveryProblem", str(report), {"questions": [first, _UNRELATED]}) is None


#: Three-field header with a path value, the shape models most often decorate with bold or backticks.
_PATH_REPORT = "---\nTitle: Current review\nOutcome: PASS\nPath: .reports/x/report.md\n---\n"


def _path_table(title: str = "Current review", outcome: str = "PASS", path: str = ".reports/x/report.md") -> str:
    """Render `_PATH_REPORT`'s header table with each cell exactly as given."""
    return f"| Field | Value |\n| --- | --- |\n| Title | {title} |\n| Outcome | {outcome} |\n| Path | {path} |"


class TestInlineMarkdownCells:
    """A value cell matches verbatim or with its inline markdown removed; the saved value is never unwrapped.

    Bolding a verdict or backticking a path is common table styling. Before, it was denied with a remedy saying no field
    held the table, so the model re-sent the same shape and was denied again.
    """

    @_skip_node_unavailable
    @pytest.mark.parametrize(
        "table",
        [
            pytest.param(_path_table(outcome="**PASS**"), id="bold-value"),
            pytest.param(_path_table(path="`.reports/x/report.md`"), id="backticked-path"),
            pytest.param(_path_table(outcome="_PASS_"), id="underscore-italic-value"),
            pytest.param(_path_table(outcome="*PASS*"), id="star-italic-value"),
            pytest.param(_path_table(path="``.reports/x/report.md``"), id="double-backtick-span"),
            pytest.param(_path_table(outcome="**`PASS`**"), id="nested-bold-code"),
            pytest.param(_path_table().replace("| Title |", "| **Title** |"), id="bold-field-name"),
            pytest.param(f"<pre>{_path_table(outcome='**PASS**')}</pre>", id="html-preview-bold-value"),
        ],
    )
    def test_formatted_table_passes(self, tmp_path: Path, table: str) -> None:
        """The current header with inline formatting in its cells passes as every option's preview."""
        report = tmp_path / "report.md"
        report.write_text(_PATH_REPORT, encoding="utf-8")

        assert _call("questionDeliveryProblem", str(report), _question(table)) is None

    @_skip_node_unavailable
    @pytest.mark.parametrize(
        ("saved", "delivered"),
        [
            pytest.param("__init__", "init", id="dunder-not-stripped-from-saved"),
            pytest.param("a_b_c", "abc", id="snake-case-kept"),
            pytest.param("**PASS**", "PASS", id="saved-markup-required"),
        ],
    )
    def test_cell_dropping_saved_characters_is_denied(self, tmp_path: Path, saved: str, delivered: str) -> None:
        """Only the cell is unwrapped, so a cell missing characters the file holds is still a mismatch."""
        report = tmp_path / "report.md"
        report.write_text(_PATH_REPORT.replace("PASS", saved), encoding="utf-8")

        assert _call("questionDeliveryProblem", str(report), _question(_path_table(outcome=delivered))) is not None

    @_skip_node_unavailable
    @pytest.mark.parametrize(
        ("mismatched", "named"),
        [
            pytest.param(
                _path_table(outcome="**FAIL**"), "`Outcome` reads `**FAIL**` but the file has `PASS`", id="value"
            ),
            pytest.param(
                "| Field | Value |\n| --- | --- |\n| Title | Current review |\n| Outcome | PASS |",
                "no `Path` row",
                id="row",
            ),
            pytest.param(_path_table(path=".reports/x|report.md"), "the `Path` row splits into 3 cells", id="pipe"),
        ],
    )
    def test_near_match_names_the_difference_and_the_corrected_retry_passes(
        self, tmp_path: Path, mismatched: str, named: str
    ) -> None:
        """A table that differs from the file is denied with the differing row named; the corrected call passes.

        Denial followed by the corrected retry is one contract — the remedy must say exactly what to change.
        """
        report = tmp_path / "report.md"
        report.write_text(_PATH_REPORT, encoding="utf-8")

        denied = _call("questionDeliveryProblem", str(report), _question(mismatched))
        retried = _call("questionDeliveryProblem", str(report), _question(_path_table(path="`.reports/x/report.md`")))

        assert isinstance(denied, str)
        assert "table is present but does not match the report file" in denied
        assert named in denied
        assert "copy every field name and value verbatim" in denied
        assert retried is None

    @_skip_node_unavailable
    def test_stop_reason_names_the_near_match_difference(self, tmp_path: Path) -> None:
        """At Stop a mismatched final-reply table is named too, after the unchanged reason prefix."""
        report = tmp_path / "report.md"
        report.write_text(_PATH_REPORT, encoding="utf-8")

        problem = _call("deliveryProblem", str(report), None, _path_table(outcome="FAIL"))

        assert isinstance(problem, str)
        assert problem.startswith("current report header was not delivered; print every header field as a table")
        assert "`Outcome` reads `FAIL` but the file has `PASS`" in problem

    @_skip_node_unavailable
    def test_audit_near_match_names_the_missing_lines(self, tmp_path: Path) -> None:
        """An Audit Report with a wrong total is named, instead of "no field holds it"."""
        report = tmp_path / "summary.jsonl"
        report.write_text(json.dumps({"sev": "high", "one_line": "broken ref"}) + "\n", encoding="utf-8")

        problem = _call("questionDeliveryProblem", str(report), _question("## Audit Report\nTotal: 2\nbroken ref"))

        assert isinstance(problem, str)
        assert "An Audit Report is present but does not match the findings aggregate: no `Total: 1` line" in problem

    @_skip_node_unavailable
    @pytest.mark.parametrize(
        ("saved", "cell"),
        [
            pytest.param("plugins/*/hooks/*.js", "**plugins/*/hooks/*.js**", id="bold-glob-keeps-literal-stars"),
            pytest.param("`a` b", "`` `a` b ``", id="code-span-around-literal-backticks"),
        ],
    )
    def test_whole_cell_wrapper_keeps_literal_markup_inside(self, tmp_path: Path, saved: str, cell: str) -> None:
        """A value holding literal stars or backticks matches once only its one outer wrapper is removed.

        Removing markup anywhere first paired the glob's two literal stars as italics (`plugins//hooks/.js`), so a
        correctly bolded glob was denied; the outer wrapper alone must be enough.
        """
        report = tmp_path / "report.md"
        report.write_text(f"---\nTitle: Current review\nOutcome: PASS\nPath: {saved}\n---\n", encoding="utf-8")

        assert _call("questionDeliveryProblem", str(report), _question(_path_table(path=cell))) is None


#: A 14-field report header: its smallest table is 16 lines, over the 12-line preview cap.
_LONG_FIELDS = [("Title", "Current review"), ("Outcome", "PASS"), *((f"Field{n}", f"value {n}") for n in range(1, 13))]
_LONG_REPORT = "---\n" + "".join(f"{name}: {value}\n" for name, value in _LONG_FIELDS) + "---\n"
_LONG_TABLE = "| Field | Value |\n| --- | --- |\n" + "\n".join(f"| {name} | {value} |" for name, value in _LONG_FIELDS)


@pytest.fixture(name="long_report")
def _long_report(tmp_path: Path) -> Path:
    """Write `_LONG_REPORT` to `run/report.md` under `tmp_path`, beside a workflow sentinel."""
    report = tmp_path / "run" / "report.md"
    report.parent.mkdir()
    report.write_text(_LONG_REPORT, encoding="utf-8")
    (tmp_path / "wf-report-dir-test-session-1234").write_text("x\n", encoding="utf-8")
    return report


def _asked(question: str, preview: str, *, multi_select: bool = False) -> dict:
    """One follow-up call whose question text is `question` and whose two options both preview `preview`."""
    call = _question(preview, multi_select=multi_select)
    call["questions"][0]["question"] = question
    return call


def _summary(report: Path) -> str:
    """Return a compact preview summary of `_LONG_REPORT` that names its file."""
    return f"Title: Current review · Outcome: PASS\n→ full header and report: {report}"


def _html_padded(text: str, total: int) -> str:
    """Return `text` in `<pre>`, padded with a wrapper-tag attribute to exactly `total` raw chars.

    Wrapper tags are removed before lines are measured but count toward the raw char cap, so the padding moves a preview
    across the 2000-char boundary without changing the lines it renders. At the 86-column wrap width, plain text within
    12 lines holds at most 1043 chars, so only markup reaches the char cap.
    """
    shell = f'<pre>{text}</pre><span title=""></span>'
    return shell.replace('title=""', f'title="{"z" * (total - len(shell))}"')


class TestPreviewCap:
    """An option preview over 2000 chars or 12 lines never delivers: the host withholds or clips it, with no scroll.

    Claude Code 2.1.294 shows a placeholder instead of a preview longer than 2000 chars and a box of terminal rows − 26
    lines, terminal columns − 34 wide; a line wider than the box wraps onto extra rows. A gate passing such a call
    recorded a report as delivered that the user never saw.
    """

    @_skip_node_unavailable
    @pytest.mark.parametrize(
        ("preview", "expected"),
        [
            pytest.param(_html_padded("row", 2000), None, id="2000-chars-fit"),
            pytest.param(_html_padded("row", 2001), "2001 chars", id="2001-chars-over"),
            pytest.param("x" * 1000, None, id="1000-char-line-wraps-to-12-rows"),
            pytest.param("x" * 1100, "13 lines", id="1100-char-line-wraps-to-13-rows"),
            pytest.param("\n".join(["row"] * 12), None, id="12-lines-fit"),
            pytest.param("\n".join(["row"] * 12) + "\n\n", None, id="trailing-newlines-not-counted"),
            pytest.param("\n".join(["row"] * 13), "13 lines", id="13-lines-over"),
            pytest.param("\n\n".join(["row"] * 7), "13 lines", id="blank-lines-count-one"),
            pytest.param("<pre>" + "<br>".join(["row"] * 13) + "</pre>", "13 lines", id="html-line-breaks-count"),
            pytest.param("\n".join(["x" * 86] * 12), None, id="12-lines-at-wrap-width-fit"),
            pytest.param("\n".join(["x" * 86 + "  "] * 12), None, id="trailing-spaces-not-measured"),
            pytest.param("\n".join(["x" * 87, *["row"] * 11]), "13 lines", id="line-past-wrap-width-wraps"),
            pytest.param("\n".join(["y" * 164] * 12), "24 lines", id="12-wide-lines-wrap-past-the-cap"),
            pytest.param("\n".join(["y" * 200] * 13), "2612 chars, 39 lines", id="both-over"),
            pytest.param(None, None, id="no-preview"),
        ],
    )
    def test_preview_cap_problem(self, preview: str | None, expected: str | None) -> None:
        """Chars are the raw length, lines the rows they span at the 86-column wrap width; the cap itself still fits.

        Counting physical lines let 12 lines of 164 chars pass while the host wrapped them to 24 rows and clipped half.
        """
        assert _call("previewCapProblem", preview) == expected

    @_skip_node_unavailable
    def test_size_note_counts_wrapped_rows_and_the_wrapped_table_still_fits(self, tmp_path: Path) -> None:
        """The deny note sizes a header table in wrapped rows, the same measure the cap applies to its preview.

        A note counting physical lines while the cap counts wrapped ones could call a rejected preview "within the cap";
        a 6-row table with one wide value stays within it, so the preview carrying it still passes.
        """
        report = tmp_path / "report.md"
        report.write_text(_HEADER_REPORT.replace("Verified result", "s" * 120), encoding="utf-8")
        table = _HEADER_TABLE.replace("Verified result", "s" * 120)

        denied = _call("questionDeliveryProblem", str(report), _question(None))
        delivered = _call("questionDeliveryProblem", str(report), _question(table))

        assert isinstance(denied, str)
        assert "this header table takes at least 6 lines" in denied
        assert "within the cap" in denied
        assert delivered is None

    @_skip_node_unavailable
    @pytest.mark.parametrize(
        ("over", "at_cap", "named"),
        [
            pytest.param(
                _html_padded(_HEADER_TABLE, 2001),
                _html_padded(_HEADER_TABLE, 2000),
                "option 1: 2001 chars;",
                id="2001-chars",
            ),
            pytest.param(
                _HEADER_TABLE + "\n\n" + "\n".join(["note"] * 7),
                _HEADER_TABLE + "\n\n" + "\n".join(["note"] * 6),
                "option 1: 13 lines",
                id="13-lines",
            ),
        ],
    )
    def test_over_cap_preview_is_denied_and_the_capped_retry_passes(
        self, tmp_path: Path, over: str, at_cap: str, named: str
    ) -> None:
        """A complete table in an over-cap preview is denied with the cap named; the same table at the cap passes.

        Denial and one corrected retry are a single lockout-freedom contract: the remedy states the cap and the size.
        """
        report = tmp_path / "report.md"
        report.write_text(_HEADER_REPORT, encoding="utf-8")

        denied = _call("questionDeliveryProblem", str(report), _question(over))
        retried = _call("questionDeliveryProblem", str(report), _question(at_cap))

        assert isinstance(denied, str)
        assert f"exceed the preview cap ({named}" in denied
        assert "Preview cap: at most 2000 chars and 12 lines per preview" in denied
        assert "this header table takes at least 5 lines" in denied
        assert retried is None

    @_skip_node_unavailable
    def test_over_cap_preview_records_nothing_for_stop(self, follow_up_run: tuple[Path, Path]) -> None:
        """A shown follow-up whose only table sits in an over-cap preview never settles Stop's check."""
        sentinel, report = follow_up_run
        payload = {"tool_input": _question(_HEADER_TABLE + "\n\n" + "x" * 2000)}

        assert _call("recordFollowUpShown", str(sentinel), str(report), payload) is False

    @_skip_node_unavailable
    def test_over_cap_question_text_counts_only_its_shown_part(self, tmp_path: Path) -> None:
        """Question text is cut after 2000 chars, so a table pushed past that point is no delivery."""
        report = tmp_path / "report.md"
        report.write_text(_HEADER_REPORT, encoding="utf-8")
        call = _asked("x" * 2000 + "\n" + _HEADER_TABLE, "next step")

        assert _call("questionDeliveryProblem", str(report), call) is not None


class TestFileShapeDelivery:
    """Content over the preview cap passes as a named file plus a compact summary naming it in every option preview.

    The question text names a file that exists and holds the full content (a header report holds its own header), and
    every option preview is a summary within the cap that carries that path; anything less is denied.
    """

    @_skip_node_unavailable
    def test_over_cap_table_is_denied_and_the_file_shape_passes(self, long_report: Path) -> None:
        """A 16-line table preview is denied with the file shape named; naming the report with summaries passes."""
        denied = _call("questionDeliveryProblem", str(long_report), _question(_LONG_TABLE))
        retried = _call(
            "questionDeliveryProblem",
            str(long_report),
            _asked(f"What next? Report: {long_report}", _summary(long_report)),
        )

        assert isinstance(denied, str)
        assert "option 1: 16 lines" in denied
        assert "this header table takes at least 16 lines" in denied
        assert "over the cap" in denied
        assert f"the report file `{long_report}` already holds the full header" in denied
        assert retried is None

    @_skip_node_unavailable
    @pytest.mark.parametrize(
        ("named", "session"),
        [
            pytest.param(
                lambda report: "run/report.md", lambda report: str(report.parent.parent), id="relative-to-cwd"
            ),
            pytest.param(lambda report: "run/report.md", lambda report: None, id="relative-suffix-of-report"),
            pytest.param(str, lambda report: None, id="absolute-path"),
        ],
    )
    def test_named_report_passes(self, long_report: Path, named: object, session: object) -> None:
        """The report may be named absolute or relative — resolved from the session cwd or as a suffix of the report."""
        path = named(long_report)
        call = _asked(f"What next? (report saved at {path}.)", f"Outcome: PASS\n→ saved to {path}")

        assert _call("questionDeliveryProblem", str(long_report), call, session(long_report)) is None

    @_skip_node_unavailable
    def test_summary_may_name_the_report_in_another_form(self, long_report: Path) -> None:
        """The question names the report absolute while each summary names it relative — both resolve to one file.

        Real review gates name the absolute report path in the question's `/compact` hint; a summary ending with the
        report's relative `Path` value names the same file and must not be read as a missing path.
        """
        call = _asked(f"What next? (report saved at {long_report}.)", "Outcome: PASS\n→ saved to run/report.md")

        assert _call("questionDeliveryProblem", str(long_report), call, str(long_report.parent.parent)) is None

    @_skip_node_unavailable
    @pytest.mark.parametrize(
        ("question", "preview", "fragment"),
        [
            pytest.param("What next?", "{summary}", "previews do not hold it either", id="path-only-in-previews"),
            pytest.param(
                "Report: {report}", "Outcome: PASS", "previews do not hold it either", id="path-only-in-question"
            ),
            pytest.param(
                "Report: {missing}",
                "Outcome: PASS → {missing}",
                "previews do not hold it either",
                id="named-file-missing",
            ),
            pytest.param(
                "Report: {unrelated}",
                "Outcome: PASS → {unrelated}",
                "does not hold the full report header",
                id="named-file-unrelated",
            ),
            pytest.param(
                "Report: {report}",
                "Outcome: PASS\n→ run/report.md\n" + "\n".join(["more"] * 11),
                "exceed the preview cap (option 1: 13 lines",
                id="summary-over-cap",
            ),
            pytest.param(
                "Report: {report}",
                "| Field | Value |\n| --- | --- |\n| Outcome | FAIL |\n\n→ {report}",
                "The preview summary contradicts the report file: `Outcome` reads `FAIL` but the file has `PASS`",
                id="summary-contradicts-file",
            ),
            pytest.param(
                "Report: {report}",
                "{report}",
                "2 of 2 option previews are not a compact summary",
                id="path-without-summary",
            ),
        ],
    )
    def test_incomplete_file_shape_is_denied(
        self, long_report: Path, question: str, preview: str, fragment: str
    ) -> None:
        """Each file-shape condition is required: named in the question, held, summarized within the cap, unchanged.

        Dropping any one of them would let a call pass that names no readable full copy or shows a stale summary.
        """
        unrelated = long_report.parent / "unrelated.md"
        unrelated.write_text("hello\n", encoding="utf-8")
        paths = {
            "summary": _summary(long_report),
            "report": long_report,
            "missing": long_report.parent / "missing.md",
            "unrelated": unrelated,
        }

        problem = _call(
            "questionDeliveryProblem", str(long_report), _asked(question.format(**paths), preview.format(**paths))
        )

        assert isinstance(problem, str)
        assert problem.startswith("current report header was not delivered by this question")
        assert fragment in problem

    @_skip_node_unavailable
    def test_file_shape_is_denied_when_the_table_fits(self, tmp_path: Path) -> None:
        """A header table within the preview cap must be in the previews; naming its file with summaries is denied.

        A five-row header passed with every preview a path plus one value, so the gate recorded a delivery while the
        user never saw the table. The file shape exists only for content the previews cannot hold.
        """
        report = tmp_path / "report.md"
        report.write_text(_HEADER_REPORT, encoding="utf-8")
        call = _asked(f"What next? Report: {report}", f"Outcome: PASS\n→ {report}")

        problem = _call("questionDeliveryProblem", str(report), call)
        shown = _call("recordFollowUpShown", str(tmp_path / "wf-report-dir-s"), str(report), {"tool_input": call})

        assert isinstance(problem, str)
        assert "fits the preview cap, so a file named in the question does not deliver it" in problem
        assert "this header table takes at least 5 lines" in problem
        assert shown is False

    @_skip_node_unavailable
    def test_audit_file_shape_is_denied_when_the_report_fits(self, tmp_path: Path) -> None:
        """A three-finding Audit Report fits the cap, so naming its rendered copy instead of showing it is denied."""
        aggregate = tmp_path / "summary.jsonl"
        lines = [f"broken ref {n}" for n in range(3)]
        aggregate.write_text(
            "".join(json.dumps({"sev": "high", "one_line": x}) + "\n" for x in lines), encoding="utf-8"
        )
        rendered = tmp_path / "audit-report.md"
        rendered.write_text("## Audit Report\n\nTotal: 3\n" + "\n".join(lines) + "\n", encoding="utf-8")

        problem = _call(
            "questionDeliveryProblem", str(aggregate), _asked(f"Findings: {rendered}", f"3 high\n→ {rendered}")
        )

        assert isinstance(problem, str)
        assert "fits the preview cap" in problem

    @_skip_node_unavailable
    def test_multi_select_question_never_delivers_a_file(self, long_report: Path) -> None:
        """A multiSelect question renders no previews, so its summaries cannot carry the file path."""
        call = _asked(f"Report: {long_report}", _summary(long_report), multi_select=True)

        assert _call("questionDeliveryProblem", str(long_report), call) is not None

    @_skip_node_unavailable
    def test_shown_file_shape_settles_stop(self, long_report: Path) -> None:
        """A shown follow-up delivering the report as a named file records the report version for Stop."""
        sentinel = long_report.parent.parent / "wf-report-dir-test-session-1234"
        payload = {"tool_input": _asked(f"Report: {long_report}", _summary(long_report)), "cwd": None}

        assert _call("recordFollowUpShown", str(sentinel), str(long_report), payload) is True

    @_skip_node_unavailable
    def test_audit_file_shape_passes_with_the_rendered_report(self, audit_run: tuple[Path, Path]) -> None:
        """Audit over the cap passes by naming the rendered Audit Report beside its aggregate."""
        aggregate, rendered = audit_run
        call = _asked(f"Findings: {rendered}", f"15 high\n→ {rendered}")

        assert _call("questionDeliveryProblem", str(aggregate), call) is None

    @_skip_node_unavailable
    def test_audit_aggregate_is_no_readable_copy(self, audit_run: tuple[Path, Path]) -> None:
        """Naming the JSONL aggregate is denied, and the remedy names where the rendered report goes."""
        aggregate, rendered = audit_run
        call = _asked(f"Findings: {aggregate}", f"15 high\n→ {aggregate}")

        problem = _call("questionDeliveryProblem", str(aggregate), call)

        assert isinstance(problem, str)
        assert "does not hold the full Audit Report" in problem
        assert f"to `{rendered}` first" in problem


#: Fifteen audit finding lines — with the heading and `Total:` line, 17 lines, over the 12-line preview cap.
_AUDIT_LINES = [f"broken ref {n}" for n in range(15)]


@pytest.fixture(name="audit_run")
def _audit_run(tmp_path: Path) -> tuple[Path, Path]:
    """Write a 15-finding `summary.jsonl` aggregate and its rendered `audit-report.md` beside it."""
    aggregate = tmp_path / "summary.jsonl"
    aggregate.write_text(
        "".join(json.dumps({"sev": "high", "one_line": line}) + "\n" for line in _AUDIT_LINES), encoding="utf-8"
    )
    rendered = tmp_path / "audit-report.md"
    rendered.write_text("## Audit Report\n\nTotal: 15\n" + "\n".join(_AUDIT_LINES) + "\n", encoding="utf-8")
    return aggregate, rendered


#: Every row of a three-field header whose last field (`Path: A|B`) the html preview tests append themselves.
_PIPE_VALUE_ROWS = "| Field | Value |\n| --- | --- |\n| Title | Current review |\n| Outcome | PASS |\n"

_HTML_TABLE = (
    "<table><tr><th>Field</th><th>Value</th></tr><tr><td>Title</td><td>Current review</td></tr>"
    "<tr><td>Outcome</td><td>PASS</td></tr><tr><td>Summary</td><td>Verified result</td></tr></table>"
)


class TestHtmlPreviewText:
    """``htmlPreviewText`` turns an html-format preview back into the text a Markdown table check can read."""

    @_skip_node_unavailable
    @pytest.mark.parametrize(
        ("preview", "expected"),
        [
            pytest.param("| a | b |", "| a | b |", id="no-tag-unchanged"),
            pytest.param("a &#124; b", "a &#124; b", id="no-tag-entities-kept"),
            pytest.param("<pre>| a | b |</pre>", "| a | b |", id="inline-pre"),
            pytest.param('<pre class="t">\n| a |\n</pre>', "\n| a |\n", id="pre-with-attributes"),
            pytest.param("<div><pre><code>| a |</code></pre></div>", "| a |", id="nested-wrappers"),
            pytest.param("<div>| a |<br>| b |<br/>| c |</div>", "| a |\n| b |\n| c |", id="br-line-breaks"),
            pytest.param("<pre>a &#124; b &vert; c &#X7C; d</pre>", "a | b | c | d", id="pipe-entities"),
            pytest.param("<pre>| a &#124; b | c |</pre>", "| a \\| b | c |", id="in-cell-pipe-entity-kept-in-cell"),
            pytest.param("<pre>| a \\&#124; b |</pre>", "| a \\| b |", id="escaped-in-cell-pipe-entity"),
            pytest.param("<pre>&#124; a \\&#124; b &#124;</pre>", "| a \\| b |", id="escaped-entity-in-encoded-row"),
            pytest.param("<pre>&amp;#124; &lt;x&gt;</pre>", "&#124; <x>", id="entities-decoded-once"),
            pytest.param(
                "<pre>| Path | .reports/<skill>/x |</pre>", "| Path | .reports/<skill>/x |", id="literal-angle-text"
            ),
        ],
    )
    def test_unwraps_html_preview(self, preview: str, expected: str) -> None:
        """Wrapper tags go, `<br>` becomes a line break, entities decode once; non-wrapper `<...>` text stays.

        A `<placeholder>`-style value inside a `<pre>` table is report content, not markup, so removing it would turn a
        correct preview into a mismatch; a preview with no tag at all is a Markdown-mode preview and is left as written.
        """
        assert _call("htmlPreviewText", preview) == expected


class TestDeliveryViaHtmlPreview:
    """A host with ``previewFormat: "html"`` rejects a preview holding no html tag, so the table arrives wrapped.

    Before this, a `<pre>`-wrapped inline table was denied with a remedy the tool itself rejects (a bare Markdown
    preview), so the follow-up question looped on denials.
    """

    @_skip_node_unavailable
    @pytest.mark.parametrize(
        "preview",
        [
            pytest.param(f"<pre>{_HEADER_TABLE}</pre>", id="inline-pre"),
            pytest.param(f'<pre style="font-size:12px">\n{_HEADER_TABLE}\n</pre>', id="pre-own-lines-attributes"),
            pytest.param(f"<div><pre><code>{_HEADER_TABLE}</code></pre></div>", id="div-pre-code"),
            pytest.param("<div>" + _HEADER_TABLE.replace("\n", "<br>") + "</div>", id="br-separated-rows"),
            pytest.param("<pre>" + _HEADER_TABLE.replace("|", "&#124;") + "</pre>", id="numeric-pipe-entities"),
            pytest.param("<pre>" + _HEADER_TABLE.replace("|", "&vert;") + "</pre>", id="named-pipe-entities"),
        ],
    )
    def test_wrapped_header_table_allows(self, tmp_path: Path, preview: str) -> None:
        """The current header table wrapped for an html preview host passes like the Markdown preview does."""
        report = tmp_path / "report.md"
        report.write_text(_HEADER_REPORT, encoding="utf-8")

        assert _call("questionDeliveryProblem", str(report), _question(preview)) is None

    @_skip_node_unavailable
    def test_html_table_markup_is_denied_with_the_pre_remedy(self, tmp_path: Path) -> None:
        """A `<table>` element is not converted; the reason tells an html host to wrap the Markdown table in `<pre>`."""
        report = tmp_path / "report.md"
        report.write_text(_HEADER_REPORT, encoding="utf-8")

        problem = _call("questionDeliveryProblem", str(report), _question(_HTML_TABLE))

        assert isinstance(problem, str)
        assert "html preview hosts: wrap the table in `<pre>` with the table on its own lines" in problem

    @_skip_node_unavailable
    @pytest.mark.parametrize(
        "preview",
        [
            pytest.param(f"<pre>{_PIPE_VALUE_ROWS}| Path | A&#124;B |</pre>", id="numeric-entity"),
            pytest.param(f"<pre>{_PIPE_VALUE_ROWS}| Path | A&vert;B |</pre>", id="named-entity"),
            pytest.param(f"<pre>{_PIPE_VALUE_ROWS}| Path | A\\|B |</pre>", id="backslash-escape"),
            pytest.param(
                "<pre>&#124; Field &#124; Value &#124;\n&#124; --- &#124; --- &#124;\n"
                "&#124; Title &#124; Current review &#124;\n&#124; Outcome &#124; PASS &#124;\n"
                "&#124; Path &#124; A\\&#124;B &#124;</pre>",
                id="fully-encoded-table-escaped-entity",
            ),
        ],
    )
    def test_escaped_in_cell_pipe_stays_cell_content(self, tmp_path: Path, preview: str) -> None:
        """A header value holding a pipe matches when the `<pre>` preview escapes that pipe inside its cell.

        Decoding `&#124;` to a bare `|` split the value cell into two, so a correct table was denied.
        """
        report = tmp_path / "report.md"
        report.write_text("---\nTitle: Current review\nOutcome: PASS\nPath: A|B\n---\n", encoding="utf-8")

        assert _call("questionDeliveryProblem", str(report), _question(preview)) is None

    @_skip_node_unavailable
    def test_audit_findings_in_html_preview_allow(self, tmp_path: Path) -> None:
        """An audit finding whose text holds a pipe matches when an html preview escapes that pipe as an entity."""
        report = tmp_path / "summary.jsonl"
        report.write_text(json.dumps({"sev": "high", "one_line": "a | b broken"}) + "\n", encoding="utf-8")
        preview = "<pre>## Audit Report\nTotal: 1\na &#124; b broken</pre>"

        assert _call("questionDeliveryProblem", str(report), _question(preview)) is None


@pytest.fixture(name="follow_up_run")
def _follow_up_run(tmp_path: Path) -> tuple[Path, Path]:
    """Stage a workflow sentinel beside its written `---` header report."""
    sentinel = tmp_path / "wf-report-dir-test-session-1234"
    sentinel.write_text("x\n", encoding="utf-8")
    report = tmp_path / "report.md"
    report.write_text(_HEADER_REPORT, encoding="utf-8")
    return sentinel, report


class TestFollowUpProblem:
    """``followUpProblem`` is the PreToolUse entry point: question text plus transcript, no Stop bookkeeping."""

    @_skip_node_unavailable
    def test_preview_pass_leaves_stop_armed(self, follow_up_run: tuple[Path, Path]) -> None:
        """An allowed follow-up records nothing yet: another PreToolUse hook or a permission rule may still deny it.

        Recording at this point let a question the user never saw settle Stop, so the report went undelivered.
        """
        sentinel, report = follow_up_run
        payload = {"transcript_path": None, "tool_input": _question(_HEADER_TABLE)}

        assert _call("followUpProblem", str(sentinel), str(report), payload) is None
        assert isinstance(_call("stopBlockReason", str(sentinel), str(report), _stop_payload(), "oss:review"), str)

    @_skip_node_unavailable
    def test_denied_follow_up_leaves_stop_armed(self, follow_up_run: tuple[Path, Path]) -> None:
        """A denied follow-up records nothing, so Stop still enforces delivery once."""
        sentinel, report = follow_up_run
        payload = {"transcript_path": None, "tool_input": _question(None)}

        assert isinstance(_call("followUpProblem", str(sentinel), str(report), payload), str)
        assert isinstance(_call("stopBlockReason", str(sentinel), str(report), _stop_payload(), "oss:review"), str)


class TestRecordFollowUpShown:
    """``recordFollowUpShown`` is the PostToolUse step: a shown follow-up that delivered the report settles Stop."""

    @_skip_node_unavailable
    def test_shown_preview_settles_the_stop_check(self, follow_up_run: tuple[Path, Path]) -> None:
        """A shown question whose preview carried the table records the version, so Stop does not demand it again.

        The preview never becomes a transcript text block; without the record, every run delivered that way would get
        one forced Stop continuation re-printing a table the user already saw.
        """
        sentinel, report = follow_up_run
        payload = {"transcript_path": None, "tool_input": _question(_HEADER_TABLE)}

        assert _call("recordFollowUpShown", str(sentinel), str(report), payload) is True
        assert _call("stopBlockReason", str(sentinel), str(report), _stop_payload(), "oss:review") is None

    @_skip_node_unavailable
    def test_shown_question_without_the_report_records_nothing(self, follow_up_run: tuple[Path, Path]) -> None:
        """A shown question that did not carry the report never vouches for delivery; Stop stays armed."""
        sentinel, report = follow_up_run
        payload = {"transcript_path": None, "tool_input": _question(None)}

        assert _call("recordFollowUpShown", str(sentinel), str(report), payload) is False
        assert isinstance(_call("stopBlockReason", str(sentinel), str(report), _stop_payload(), "oss:review"), str)

    @_skip_node_unavailable
    def test_table_in_some_previews_records_nothing(self, follow_up_run: tuple[Path, Path]) -> None:
        """A shown question carrying the table in only some option previews never vouches for delivery.

        The user may answer with another option focused and never see it, so Stop must still demand the table once.
        """
        sentinel, report = follow_up_run
        payload = {"transcript_path": None, "tool_input": {"questions": [_follow_up([_HEADER_TABLE, None])]}}

        assert _call("recordFollowUpShown", str(sentinel), str(report), payload) is False
        assert isinstance(_call("stopBlockReason", str(sentinel), str(report), _stop_payload(), "oss:review"), str)

    @_skip_node_unavailable
    def test_reply_text_table_records_nothing(self, tmp_path: Path, follow_up_run: tuple[Path, Path]) -> None:
        """A reply-text table before a shown question that does not carry it never vouches for delivery.

        That text may have come back as an empty progress update, so Stop must still demand the table once.
        """
        sentinel, report = follow_up_run
        transcript = _write_transcript(
            tmp_path, [{"type": "assistant", "message": {"content": [{"type": "text", "text": _HEADER_TABLE}]}}]
        )
        payload = {"transcript_path": str(transcript), "tool_input": _question(None)}

        assert _call("recordFollowUpShown", str(sentinel), str(report), payload) is False

    @_skip_node_unavailable
    def test_missing_sentinel_records_nothing(self, follow_up_run: tuple[Path, Path]) -> None:
        """Without an active workflow sentinel there is no marker to write."""
        _, report = follow_up_run
        payload = {"transcript_path": None, "tool_input": _question(_HEADER_TABLE)}

        assert _call("recordFollowUpShown", None, str(report), payload) is False


@_skip_node_unavailable
@pytest.mark.parametrize(
    ("saved", "delivered", "matches"),
    [
        pytest.param(r"C:\reports\q", "C:reportsq", False, id="path-separators-missing"),
        pytest.param("A|B", "AB", False, id="literal-pipe-missing"),
        pytest.param("A*B", "AB", False, id="literal-star-missing"),
        pytest.param("A`B", "AB", False, id="literal-backtick-missing"),
        pytest.param(r"C:\reports\q", r"C:\reports\q", True, id="literal-path-delivered"),
        pytest.param("A|B", r"A\|B", True, id="table-pipe-escaped"),
    ],
)
def test_delivery_preserves_literal_header_values(tmp_path: Path, saved: str, delivered: str, matches: bool) -> None:
    """Formatting tolerance cannot equate different paths or erase literal header characters."""
    report = tmp_path / "report.md"
    report.write_text(f"---\nTitle: Current\nOutcome: PASS\nPath: {saved}\n---\n", encoding="utf-8")
    text = f"| Field | Value |\n| --- | --- |\n| Title | Current |\n| Outcome | PASS |\n| Path | {delivered} |"
    transcript = _write_transcript(
        tmp_path, [{"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}]
    )
    problem = _call("deliveryProblem", str(report), str(transcript))
    if matches:
        assert problem is None
    else:
        assert isinstance(problem, str)
        assert problem.startswith(
            "current report header was not delivered; print every header field as a table before following up"
        )


@_skip_node_unavailable
@pytest.mark.parametrize(
    ("delivered", "matches"),
    [
        pytest.param("AB", False, id="audit-pipe-missing"),
        pytest.param("A|B", True, id="audit-literal-pipe"),
        pytest.param(r"A\|B", True, id="audit-table-pipe-escaped"),
    ],
)
def test_audit_delivery_preserves_literal_finding(tmp_path: Path, delivered: str, matches: bool) -> None:
    """Audit findings retain literal punctuation rather than accepting a different description."""
    report = tmp_path / "summary.jsonl"
    report.write_text(json.dumps({"sev": "high", "one_line": "A|B"}) + "\n", encoding="utf-8")
    text = f"## Audit Report\nTotal: 1\n{delivered}"
    transcript = _write_transcript(
        tmp_path, [{"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}]
    )
    problem = _call("deliveryProblem", str(report), str(transcript))
    if matches:
        assert problem is None
    else:
        assert isinstance(problem, str)
        assert problem.startswith("current audit findings were not delivered; emit Step 7 before following up")


@_skip_node_unavailable
@pytest.mark.parametrize(
    "skill", ["oss:review", "oss:analyse", "develop:review", "research:topic", "foundry:profile", "foundry:audit"]
)
@pytest.mark.parametrize(
    "question",
    [
        pytest.param(
            {
                "questions": [
                    {
                        "question": "The producer failed. Retry or stop?",
                        "options": [{"label": "Retry"}, {"label": "Stop"}],
                    }
                ]
            },
            id="recovery-question",
        ),
        pytest.param(
            {
                "questions": [
                    {"question": "What next?", "header": "Recovery", "options": [{"label": "Retry"}, {"label": "Stop"}]}
                ]
            },
            id="unrelated-what-next",
        ),
    ],
)
def test_question_is_not_a_workflow_follow_up(question: dict, skill: str) -> None:
    """Neither a recovery question nor a generic one activates a workflow's report gate.

    Scenario: missing report delivery must not prevent asking how to recover the producer, and a generic "What next?"
    cannot activate a different workflow's report gate.
    """
    assert _call("isWorkflowFollowUp", question, skill) is False


# ── finalReplyText ─────────────────────────────────────────────────────────


def _write_transcript(tmp_path: Path, rows: list[dict]) -> Path:
    """Write transcript fixture rows as JSONL and return their path.

    Examples:
        >>> from tempfile import TemporaryDirectory
        >>> with TemporaryDirectory() as directory:
        ...     _write_transcript(Path(directory), [{"type": "user"}]).read_text().endswith("\\n")
        True
    """
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return transcript


@_skip_node_unavailable
@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        pytest.param(
            [
                {"type": "user", "message": {"content": [{"type": "text", "text": "go"}]}},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}},
            ],
            "hello",
            id="current-turn-assistant-text",
        ),
        pytest.param(
            [
                {"type": "user", "message": {"content": [{"type": "text", "text": "go"}]}},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "before"}]}},
                {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read"}]}},
                {"type": "user", "message": {"content": [{"type": "tool_result", "content": "file contents"}]}},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "after"}]}},
            ],
            "after",
            id="text-before-the-last-tool-call-excluded",
        ),
        pytest.param(
            [
                {"type": "user", "message": {"content": [{"type": "text", "text": "go"}]}},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "a"}]}},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "b"}]}},
            ],
            "a\nb",
            id="final-reply-split-across-rows",
        ),
        pytest.param(
            [
                {"type": "user", "message": {"content": [{"type": "text", "text": "go"}]}},
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "before"}, {"type": "tool_use", "name": "Read"}]},
                },
                {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}},
            ],
            "",
            id="final-reply-not-written-yet",
        ),
        pytest.param(
            [
                {"type": "user", "message": {"content": [{"type": "text", "text": "go"}]}},
                {"type": "queue-operation", "operation": "noop"},
                {"type": "attachment", "attachment": {}},
                {"type": "mode"},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}},
            ],
            "hello",
            id="non-turn-rows-are-skipped",
        ),
        pytest.param(
            [
                {"type": "user", "message": {"content": [{"type": "text", "text": "go"}]}},
                {
                    "type": "assistant",
                    "isSidechain": True,
                    "message": {"content": [{"type": "text", "text": "sub-agent text"}]},
                },
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "orchestrator text"}]}},
            ],
            "orchestrator text",
            id="sidechain-assistant-rows-are-excluded",
        ),
    ],
)
def test_final_reply_text(tmp_path: Path, rows: list[dict], expected: str) -> None:
    """Only the orchestrator's own text after its last tool call — the turn's final reply — is returned.

    Scenario: text written before a tool call may come back as an empty progress update and focus mode shows only the
    final message, so it never counts; a final reply split over several rows is joined; a transcript still ending on a
    tool_result (final reply not yet flushed) yields "" instead of an earlier table; queue-operation / attachment /
    mode rows are skipped; subagent output (isSidechain: true) is not the orchestrator's reply.
    """
    transcript = _write_transcript(tmp_path, rows)

    assert _call("finalReplyText", str(transcript)) == expected


@_skip_node_unavailable
def test_missing_transcript_path_returns_empty_string() -> None:
    """No transcript_path at all — caller treats this exactly like 'no table found', never a crash."""
    assert _call("finalReplyText", None) == ""


@_skip_node_unavailable
def test_unreadable_transcript_path_returns_empty_string(tmp_path: Path) -> None:
    """A path that doesn't resolve to a file fails open to an empty string."""
    assert _call("finalReplyText", str(tmp_path / "does-not-exist.jsonl")) == ""


# ── stopBlockReason ────────────────────────────────────────────────────────

_HEADER = "---\nTitle: Current review\nOutcome: PASS\nPath: .reports/x/report.md\n---\n"
_TABLE = (
    "| Field | Value |\n| --- | --- |\n| Title | Current review |\n| Outcome | PASS |\n| Path | .reports/x/report.md |"
)


def _stop_payload(**overrides: object) -> dict:
    """Build a Stop payload with no transcript and an empty final message, applying `overrides`."""
    payload: dict = {"hook_event_name": "Stop", "stop_hook_active": False, "last_assistant_message": ""}
    payload.update(overrides)
    return payload


@pytest.fixture(name="stop_run")
def _stop_run(tmp_path: Path) -> tuple[Path, Path]:
    """Stage a workflow sentinel and its written report."""
    sentinel = tmp_path / "wf-report-dir-test-session-1234"
    sentinel.write_text("x\n", encoding="utf-8")
    report = tmp_path / "report.md"
    report.write_text(_HEADER, encoding="utf-8")
    return sentinel, report


@_skip_node_unavailable
def test_stop_blocks_undelivered_report_once(stop_run: tuple[Path, Path]) -> None:
    """A turn ending without the header is kept going once; the same report never re-blocks."""
    sentinel, report = stop_run

    reason = _call("stopBlockReason", str(sentinel), str(report), _stop_payload(), "oss:review")

    assert isinstance(reason, str)
    assert "oss:review" in reason
    assert "Path" in reason
    assert _call("stopBlockReason", str(sentinel), str(report), _stop_payload(), "oss:review") is None


@_skip_node_unavailable
def test_stop_rechecks_rewritten_report(stop_run: tuple[Path, Path]) -> None:
    """A delivered report that is later rewritten must be delivered again."""
    sentinel, report = stop_run
    assert (
        _call("stopBlockReason", str(sentinel), str(report), _stop_payload(last_assistant_message=_TABLE), "x") is None
    )

    report.write_text(_HEADER.replace("PASS", "NEEDS_WORK"), encoding="utf-8")

    assert _call("stopBlockReason", str(sentinel), str(report), _stop_payload(), "x") is not None


@_skip_node_unavailable
@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"last_assistant_message": _TABLE}, id="table-in-final-message-without-transcript"),
        pytest.param({"stop_hook_active": True}, id="forced-continuation"),
    ],
)
def test_stop_does_not_block(stop_run: tuple[Path, Path], overrides: dict) -> None:
    """A turn that delivered the table, or that a Stop hook already forced on, is never blocked.

    Scenario: the Stop payload's final message counts as delivery even when the transcript lags; `stop_hook_active`
    means a Stop hook already forced this turn on, so never loop.
    """
    sentinel, report = stop_run

    assert _call("stopBlockReason", str(sentinel), str(report), _stop_payload(**overrides), "x") is None


def _mid_turn_table_transcript(tmp_path: Path, final_text: str) -> Path:
    """Write a transcript whose turn printed `_TABLE`, ran a tool, then ended its reply with `final_text`."""
    return _write_transcript(
        tmp_path,
        [
            {"type": "user", "message": {"content": [{"type": "text", "text": "go"}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": _TABLE}]}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": final_text}]}},
        ],
    )


class TestStopJudgesTheFinalReply:
    """Stop checks only the turn's final reply: `last_assistant_message`, else the transcript text after the last tool.

    A table printed mid-turn and followed by a tool call may have come back as an empty progress update, and focus mode
    shows only the final message, so a turn ending "Done." after such a table has not delivered it.
    """

    @_skip_node_unavailable
    @pytest.mark.parametrize(
        "final_message", [pytest.param("Done.", id="final-message"), pytest.param("", id="no-lam")]
    )
    def test_mid_turn_table_with_final_reply_without_it_blocks_once(
        self, tmp_path: Path, stop_run: tuple[Path, Path], final_message: str
    ) -> None:
        """Table mid-turn, final reply "Done." — blocked once, judged on the payload's message or the transcript."""
        sentinel, report = stop_run
        payload = _stop_payload(
            transcript_path=str(_mid_turn_table_transcript(tmp_path, "Done.")), last_assistant_message=final_message
        )

        reason = _call("stopBlockReason", str(sentinel), str(report), payload, "oss:review")

        assert isinstance(reason, str)
        assert "final reply text found" in reason
        assert _call("stopBlockReason", str(sentinel), str(report), payload, "oss:review") is None

    @_skip_node_unavailable
    def test_table_in_transcript_final_reply_passes_without_last_message(
        self, tmp_path: Path, stop_run: tuple[Path, Path]
    ) -> None:
        """Without `last_assistant_message`, the transcript's final reply holding the table is delivery."""
        sentinel, report = stop_run
        payload = _stop_payload(transcript_path=str(_mid_turn_table_transcript(tmp_path, f"Done.\n\n{_TABLE}")))

        assert _call("stopBlockReason", str(sentinel), str(report), payload, "oss:review") is None


@_skip_node_unavailable
@pytest.mark.parametrize("content", [None, ""])
def test_stop_ignores_unwritten_report(stop_run: tuple[Path, Path], content: str | None) -> None:
    """Missing or empty report means the producer has not finished — nothing to deliver yet."""
    sentinel, report = stop_run
    report.unlink()
    if content is not None:
        report.write_text(content, encoding="utf-8")

    assert _call("stopBlockReason", str(sentinel), str(report), _stop_payload(), "x") is None


@_skip_node_unavailable
def test_stop_audit_reason_names_findings_not_header(tmp_path: Path) -> None:
    """Audit delivers a findings aggregate, so its Stop reason must not ask for a header table."""
    sentinel = tmp_path / "run-dir"
    sentinel.write_text("x\n", encoding="utf-8")
    summary = tmp_path / "summary.jsonl"
    summary.write_text(json.dumps({"sev": "high", "one_line": "broken ref"}) + "\n", encoding="utf-8")

    reason = _call("stopBlockReason", str(sentinel), str(summary), _stop_payload(), "foundry:audit")

    assert "Audit Report" in reason
    assert "Field | Value" not in reason


@_skip_node_unavailable
def test_delivered_marker_keeps_session_token_terminal(tmp_path: Path) -> None:
    """The marker name ends with the sentinel's own CSID-suffixed name, beside it."""
    marker = Path(_call("deliveredMarkerPath", str(tmp_path / "oss-review-report-dir-abc")))

    assert marker.parent == tmp_path
    assert marker.name == "report-delivered-oss-review-report-dir-abc"

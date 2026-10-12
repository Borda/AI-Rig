"""Unit tests for ``hooks/report-header-table.js``, the table-format detector shared by all six ``enforce-*-header.js``
hooks (see propagate_shared.py MANIFEST — canonical here, byte-identical copies in cc_oss, cc_develop, cc_research).

Covers the exports in isolation, independent of any single hook's
sentinel/report-dir wiring:

* ``hasHeaderTable`` — pipe-table detection (header + separator + >= MIN_TABLE_ROWS
  data rows) and the documented ``·``-separated one-line fallback.
* ``finalReplyText`` — bounded tail-read of a JSONL transcript returning only the
  turn's final reply: assistant text after the last tool call, skipping non-turn
  rows and sidechain (subagent) output.
* question-time delivery — first-question plain summary, resolved report path,
  headline values, and the 1500-character cap.
* optional previews retain host caps; legacy HTML helpers remain for the resolve picker.
* inline Markdown tolerance and mismatch remedies remain at final-table delivery.
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
        encoding="utf-8",
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


def _summary(report: Path, title: str = "Current review", outcome: str = "PASS") -> str:
    """Build the report's plain headline summary and file line."""
    return f"Title: {title}\nOutcome: {outcome}\n→ {report}"


def _asked(question: str, preview: str | None = None, *, multi_select: bool = False) -> dict:
    """Build a follow-up whose first question text is explicit, with optional previews."""
    call = _question(preview, multi_select=multi_select)
    call["questions"][0]["question"] = question
    return call


class TestQuestionSummaryFields:
    """Only first-question text supplies summary evidence."""

    @_skip_node_unavailable
    @pytest.mark.parametrize(
        "placement",
        ["option-preview", "question-description", "option-description", "multi-select-preview"],
    )
    def test_other_fields_cannot_supply_summary(self, tmp_path: Path, placement: str) -> None:
        """Neither descriptions nor previews substitute for the question's own summary."""
        report = tmp_path / "report.md"
        report.write_text(_HEADER_REPORT, encoding="utf-8")
        summary = _summary(report)
        call = _question(None, multi_select=placement == "multi-select-preview")
        first = call["questions"][0]
        if placement == "question-description":
            first["description"] = summary
        else:
            key = "description" if placement == "option-description" else "preview"
            for option in first["options"]:
                option[key] = summary
        assert _call("questionDeliveryProblem", str(report), call) is not None
        first["question"] = summary
        assert _call("questionDeliveryProblem", str(report), call) is None

    @_skip_node_unavailable
    @pytest.mark.parametrize(
        "tool_input",
        [
            pytest.param(None, id="null"),
            pytest.param({}, id="empty"),
            pytest.param({"questions": "x"}, id="not-array"),
            pytest.param({"questions": [None, {"options": [None]}]}, id="malformed-entries"),
        ],
    )
    def test_malformed_input_yields_no_summary(self, tmp_path: Path, tool_input: object) -> None:
        """Malformed input denies the transition without crashing the library."""
        report = tmp_path / "report.md"
        report.write_text(_HEADER_REPORT, encoding="utf-8")
        assert isinstance(_call("questionDeliveryProblem", str(report), tool_input), str)


class TestDeliveryViaQuestion:
    """The question summary is independent of full-table delivery at Stop."""

    @_skip_node_unavailable
    def test_plain_summary_allows(self, tmp_path: Path) -> None:
        """Headline values and a report path pass without previews or a header table."""
        report = tmp_path / "report.md"
        report.write_text(_HEADER_REPORT, encoding="utf-8")
        assert _call("questionDeliveryProblem", str(report), _asked(_summary(report))) is None

    @_skip_node_unavailable
    def test_stale_summary_is_denied_and_names_the_summary_fix(self, tmp_path: Path) -> None:
        """A stale outcome is denied with the required value and first-question remedy."""
        report = tmp_path / "report.md"
        report.write_text(_HEADER_REPORT, encoding="utf-8")
        problem = _call("questionDeliveryProblem", str(report), _asked(_summary(report, outcome="FAIL")))
        assert "Outcome: PASS" in problem
        assert "first question's `question` text" in problem
        assert _call("questionDeliveryProblem", str(report), _asked(_summary(report))) is None

    @_skip_node_unavailable
    def test_call_without_summary_names_fixed_lines(self, tmp_path: Path) -> None:
        """Missing summary evidence yields a bounded, concrete remedy."""
        report = tmp_path / "report.md"
        report.write_text(_HEADER_REPORT, encoding="utf-8")
        problem = _call("questionDeliveryProblem", str(report), _question(None))
        assert "Title: Current review" in problem
        assert "Outcome: PASS" in problem
        assert str(report) in problem
        assert "fixed lines" in problem

    @_skip_node_unavailable
    def test_reply_text_table_is_denied_and_the_summary_fix_passes(
        self, tmp_path: Path, follow_up_run: tuple[Path, Path]
    ) -> None:
        """Earlier reply text cannot replace the first question's summary."""
        sentinel, report = follow_up_run
        transcript = _write_transcript(
            tmp_path, [{"type": "assistant", "message": {"content": [{"type": "text", "text": _HEADER_TABLE}]}}]
        )
        denied = _call(
            "followUpProblem",
            str(sentinel),
            str(report),
            {"transcript_path": str(transcript), "tool_input": _question(None)},
        )
        assert "reply text before the call does not count" in denied
        assert _call("followUpProblem", str(sentinel), str(report), {"tool_input": _asked(_summary(report))}) is None

    @_skip_node_unavailable
    def test_summary_split_across_fields_never_combines(self, tmp_path: Path) -> None:
        """A title in question text cannot combine with the outcome and path in previews."""
        report = tmp_path / "report.md"
        report.write_text(_HEADER_REPORT, encoding="utf-8")
        call = _asked("Title: Current review", f"Outcome: PASS\n→ {report}")
        assert _call("questionDeliveryProblem", str(report), call) is not None

    @_skip_node_unavailable
    def test_incomplete_report_wins_over_matching_summary(self, tmp_path: Path) -> None:
        """Matching text cannot authorize a follow-up whose report header is unfinished."""
        report = tmp_path / "report.md"
        report.write_text("---\nTitle: Current review\nOutcome: PASS\n---\n", encoding="utf-8")
        assert (
            _call("questionDeliveryProblem", str(report), _asked(_summary(report)))
            == "report header is incomplete; finish the report before following up"
        )

    @_skip_node_unavailable
    def test_audit_headline_and_aggregate_path_allow(self, tmp_path: Path) -> None:
        """Audit uses the aggregate path and existing headline without inventing header fields."""
        report = tmp_path / "summary.jsonl"
        report.write_text(json.dumps({"sev": "high", "one_line": "broken ref"}) + "\n", encoding="utf-8")
        assert _call("questionDeliveryProblem", str(report), _asked(f"Audit Report\n→ {report}")) is None


class TestFirstQuestionPlacement:
    """Every misplaced summary can be repaired by moving it to the first question's text."""

    @_skip_node_unavailable
    @pytest.mark.parametrize(
        "placement",
        [
            "last-option-preview-only",
            "previews-of-second-question",
            "text-of-second-question",
            "multi-select-previews",
            "html-first-option-preview-only",
            "form-question-level-description",
            "one-option-description",
            "multi-select-description",
            "description-of-second-question",
            "every-option-preview",
        ],
    )
    def test_misplaced_summary_is_denied_and_the_named_fix_passes(self, tmp_path: Path, placement: str) -> None:
        """Later questions, previews and helper lines cannot provide first-question evidence."""
        report = tmp_path / "report.md"
        report.write_text(_HEADER_REPORT, encoding="utf-8")
        summary = _summary(report)
        question = _question(None, multi_select=placement.startswith("multi-select"))["questions"][0]
        if placement == "text-of-second-question":
            question["question"] = summary
        elif placement == "form-question-level-description":
            question["description"] = summary
        elif "description" in placement:
            question["options"][0]["description"] = summary
        else:
            for option in question["options"] if placement == "every-option-preview" else question["options"][-1:]:
                option["preview"] = f"<pre>{summary}</pre>" if placement.startswith("html") else summary
        unrelated = {"question": "Which branch?", "options": [{"label": "main"}]}
        call = {"questions": [unrelated, question] if "second-question" in placement else [question]}
        problem = _call("questionDeliveryProblem", str(report), call)
        assert "first question's `question` text" in problem
        call["questions"][0]["question"] = summary
        assert _call("questionDeliveryProblem", str(report), call) is None

    @_skip_node_unavailable
    def test_first_question_text_delivers_on_its_own(self, tmp_path: Path) -> None:
        """A later unrelated question does not invalidate a complete first summary."""
        report = tmp_path / "report.md"
        report.write_text(_HEADER_REPORT, encoding="utf-8")
        call = _asked(_summary(report))
        call["questions"].append({"question": "Which branch?"})
        assert _call("questionDeliveryProblem", str(report), call) is None


_PATH_REPORT = "---\nTitle: Current review\nOutcome: PASS\nPath: .reports/x/report.md\n---\n"


def _path_table(title: str = "Current review", outcome: str = "PASS", path: str = ".reports/x/report.md") -> str:
    """Render the saved header's cells for final-reply matching."""
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
        """Markdown cells pass in the final reply; legacy HTML must first be rendered as plain Markdown."""
        report = tmp_path / "report.md"
        report.write_text(_PATH_REPORT, encoding="utf-8")

        if table.startswith("<pre>"):
            assert _call("deliveryProblem", str(report), None, table) is not None
            table = table.removeprefix("<pre>").removesuffix("</pre>")
        assert _call("deliveryProblem", str(report), None, table) is None

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

        assert _call("deliveryProblem", str(report), None, _path_table(outcome=delivered)) is not None

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
        """A table that differs from the file is denied with the differing row named; the corrected final reply passes.

        Denial followed by the corrected final reply is one contract — the remedy must say exactly what to change.
        """
        report = tmp_path / "report.md"
        report.write_text(_PATH_REPORT, encoding="utf-8")

        denied = _call("deliveryProblem", str(report), None, mismatched)
        retried = _call("deliveryProblem", str(report), None, _path_table(path="`.reports/x/report.md`"))

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

        problem = _call("deliveryProblem", str(report), None, "## Audit Report\nTotal: 2\nbroken ref")

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

        assert _call("deliveryProblem", str(report), None, _path_table(path=cell)) is None


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
    def test_large_nonheadline_field_does_not_expand_summary(self, tmp_path: Path) -> None:
        """Only fixed headline fields are required even when other header values are wide."""
        report = tmp_path / "report.md"
        report.write_text(_HEADER_REPORT.replace("Verified result", "s" * 120), encoding="utf-8")
        assert _call("questionDeliveryProblem", str(report), _question(None)) is not None
        assert _call("questionDeliveryProblem", str(report), _asked(_summary(report))) is None

    @_skip_node_unavailable
    @pytest.mark.parametrize(
        ("over", "at_cap", "named"),
        [
            pytest.param(
                _html_padded(_HEADER_TABLE, 2001), _html_padded(_HEADER_TABLE, 2000), "2001 chars", id="chars"
            ),
            pytest.param(
                _HEADER_TABLE + "\n\n" + "\n".join(["note"] * 7),
                _HEADER_TABLE + "\n\n" + "\n".join(["note"] * 6),
                "13 lines",
                id="lines",
            ),
        ],
    )
    def test_over_cap_optional_preview_is_denied_and_the_capped_retry_passes(
        self, tmp_path: Path, over: str, at_cap: str, named: str
    ) -> None:
        """Optional previews remain capped even when the question summary is already complete."""
        report = tmp_path / "report.md"
        report.write_text(_HEADER_REPORT, encoding="utf-8")
        problem = _call("questionDeliveryProblem", str(report), _asked(_summary(report), over))
        assert "exceed the preview cap" in problem
        assert named in problem
        assert _call("questionDeliveryProblem", str(report), _asked(_summary(report), at_cap)) is None

    @_skip_node_unavailable
    def test_over_cap_preview_records_nothing_for_stop(self, follow_up_run: tuple[Path, Path]) -> None:
        """A shown question with an over-cap preview cannot settle final report delivery."""
        sentinel, report = follow_up_run
        payload = {"tool_input": _asked(_summary(report), "x" * 2001)}
        assert _call("recordFollowUpShown", str(sentinel), str(report), payload) is False
        assert _call("stopBlockReason", str(sentinel), str(report), _stop_payload(), "oss:review") is not None

    @_skip_node_unavailable
    def test_over_cap_question_text_is_denied(self, tmp_path: Path) -> None:
        """A summary beyond the host cut cannot be rescued by preview evidence."""
        report = tmp_path / "report.md"
        report.write_text(_HEADER_REPORT, encoding="utf-8")
        problem = _call(
            "questionDeliveryProblem", str(report), _asked("x" * 2000 + "\n" + _summary(report), _HEADER_TABLE)
        )
        assert "question text exceeds 1500 characters" in problem


_LONG_FIELDS = [("Title", "Current review"), ("Outcome", "PASS"), *((f"Field{n}", f"value {n}") for n in range(1, 13))]
_LONG_REPORT = "---\n" + "".join(f"{name}: {value}\n" for name, value in _LONG_FIELDS) + "---\n"
_LONG_TABLE = "| Field | Value |\n| --- | --- |\n" + "\n".join(f"| {name} | {value} |" for name, value in _LONG_FIELDS)


@pytest.fixture(name="long_report")
def _long_report(tmp_path: Path) -> Path:
    """Write a large complete report beside a workflow sentinel."""
    report = tmp_path / "run" / "report.md"
    report.parent.mkdir()
    report.write_text(_LONG_REPORT, encoding="utf-8")
    (tmp_path / "wf-report-dir-test-session-1234").write_text("x\n", encoding="utf-8")
    return report


class TestNamedSummaryDelivery:
    """Named summaries use one contract regardless of the report's table size."""

    @_skip_node_unavailable
    def test_over_cap_table_is_denied_and_plain_summary_passes(self, long_report: Path) -> None:
        """A large table preview is denied; the fixed question lines pass without previews."""
        denied = _call("questionDeliveryProblem", str(long_report), _question(_LONG_TABLE))
        assert "option 1: 16 lines" in denied
        assert "fixed lines" in denied
        assert _call("questionDeliveryProblem", str(long_report), _asked(_summary(long_report))) is None

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
        """Existing report path matching accepts native absolute paths and relative suffixes."""
        call = _asked(f"Title: Current review\nOutcome: PASS\n→ {named(long_report)}")
        assert _call("questionDeliveryProblem", str(long_report), call, session(long_report)) is None

    @_skip_node_unavailable
    def test_preview_may_name_report_in_another_form(self, long_report: Path) -> None:
        """A relative path in an optional preview does not affect a complete question summary."""
        call = _asked(_summary(long_report), "Outcome: PASS\n→ run/report.md")
        assert _call("questionDeliveryProblem", str(long_report), call, str(long_report.parent.parent)) is None

    @_skip_node_unavailable
    @pytest.mark.parametrize(
        ("question", "preview", "fragment"),
        [
            pytest.param("What next?", "{summary}", "name the report file", id="path-only-in-previews"),
            pytest.param("Report: {report}", "Outcome: PASS", "Title: Current review", id="path-only-in-question"),
            pytest.param(
                "Title: Current review\nOutcome: PASS\nReport: {missing}",
                "Outcome: PASS → {missing}",
                "name the report file",
                id="named-file-missing",
            ),
            pytest.param(
                "Title: Current review\nOutcome: PASS\nReport: {unrelated}",
                "Outcome: PASS → {unrelated}",
                "name the report file",
                id="named-file-unrelated",
            ),
            pytest.param(
                "{summary}",
                "Outcome: PASS\n→ run/report.md\n" + "\n".join(["more"] * 11),
                "exceed the preview cap",
                id="preview-over-cap",
            ),
            pytest.param(
                "Title: Current review\nOutcome: FAIL\n→ {report}",
                "{summary}",
                "Outcome: PASS",
                id="summary-contradicts-file",
            ),
            pytest.param("{report}", "{report}", "Title: Current review", id="path-without-summary"),
        ],
    )
    def test_incomplete_summary_is_denied(self, long_report: Path, question: str, preview: str, fragment: str) -> None:
        """The summary must name the correct existing report and present headline values."""
        unrelated = long_report.parent / "unrelated.md"
        unrelated.write_text("hello\n", encoding="utf-8")
        values = {
            "summary": _summary(long_report),
            "report": long_report,
            "missing": long_report.parent / "missing.md",
            "unrelated": unrelated,
        }
        problem = _call(
            "questionDeliveryProblem", str(long_report), _asked(question.format(**values), preview.format(**values))
        )
        assert isinstance(problem, str)
        assert fragment in problem

    @_skip_node_unavailable
    def test_small_report_summary_passes_without_settling_stop(self, follow_up_run: tuple[Path, Path]) -> None:
        """Small reports use the same question contract, but the table still belongs in the final reply."""
        sentinel, report = follow_up_run
        call = _asked(_summary(report))
        assert _call("questionDeliveryProblem", str(report), call) is None
        assert _call("recordFollowUpShown", str(sentinel), str(report), {"tool_input": call}) is False

    @_skip_node_unavailable
    def test_small_audit_summary_names_aggregate(self, tmp_path: Path) -> None:
        """Small audits need the aggregate path, rather than a separate rendered copy."""
        aggregate = tmp_path / "summary.jsonl"
        aggregate.write_text(json.dumps({"sev": "high", "one_line": "broken ref"}) + "\n", encoding="utf-8")
        assert _call("questionDeliveryProblem", str(aggregate), _asked(f"Audit Report\n→ {aggregate}")) is None

    @_skip_node_unavailable
    def test_multi_select_summary_delivers(self, long_report: Path) -> None:
        """Question text renders for multi-select questions too, so previews are unnecessary."""
        assert (
            _call("questionDeliveryProblem", str(long_report), _asked(_summary(long_report), multi_select=True)) is None
        )

    @_skip_node_unavailable
    def test_shown_named_summary_leaves_stop_armed(self, long_report: Path) -> None:
        """A large report's shown summary cannot settle full-table delivery."""
        sentinel = long_report.parent.parent / "wf-report-dir-test-session-1234"
        payload = {"tool_input": _asked(_summary(long_report))}
        assert _call("recordFollowUpShown", str(sentinel), str(long_report), payload) is False
        assert _call("stopBlockReason", str(sentinel), str(long_report), _stop_payload(), "oss:review") is not None

    @_skip_node_unavailable
    def test_audit_rendered_report_cannot_replace_aggregate_path(self, audit_run: tuple[Path, Path]) -> None:
        """The summary is bound to the aggregate the audit hook resolved."""
        aggregate, rendered = audit_run
        problem = _call("questionDeliveryProblem", str(aggregate), _asked(f"Audit Report\n→ {rendered}"))
        assert str(aggregate) in problem
        assert _call("questionDeliveryProblem", str(aggregate), _asked(f"Audit Report\n→ {aggregate}")) is None

    @_skip_node_unavailable
    def test_large_audit_aggregate_is_summary_evidence(self, audit_run: tuple[Path, Path]) -> None:
        """The JSONL aggregate path and existing headline suffice even for many findings."""
        aggregate, _ = audit_run
        assert _call("questionDeliveryProblem", str(aggregate), _asked(f"Audit Report\n→ {aggregate}")) is None


_AUDIT_LINES = [f"broken ref {n}" for n in range(15)]


@pytest.fixture(name="audit_run")
def _audit_run(tmp_path: Path) -> tuple[Path, Path]:
    """Write a many-finding aggregate and its rendered report."""
    aggregate = tmp_path / "summary.jsonl"
    aggregate.write_text(
        "".join(json.dumps({"sev": "high", "one_line": line}) + "\n" for line in _AUDIT_LINES), encoding="utf-8"
    )
    rendered = tmp_path / "audit-report.md"
    rendered.write_text("## Audit Report\nTotal: 15\n" + "\n".join(_AUDIT_LINES), encoding="utf-8")
    return aggregate, rendered


#: Every row of a three-field header whose last field (`Path: A|B`) the html preview tests append themselves.
_PIPE_VALUE_ROWS = "| Field | Value |\n| --- | --- |\n| Title | Current review |\n| Outcome | PASS |\n"

_HTML_TABLE = (
    "<table><tr><th>Field</th><th>Value</th></tr><tr><td>Title</td><td>Current review</td></tr>"
    "<tr><td>Outcome</td><td>PASS</td></tr><tr><td>Summary</td><td>Verified result</td></tr></table>"
)


class TestOptionalPreviewHeight:
    """Optional previews retain host height checks after removing legacy HTML table delivery."""

    @_skip_node_unavailable
    @pytest.mark.parametrize(
        ("preview", "expected"),
        [
            pytest.param("| a | b |", 1, id="plain-text"),
            pytest.param("a &#124; b", 1, id="literal-entity"),
            pytest.param("<pre>| a | b |</pre>", 1, id="inline-pre"),
            pytest.param('<pre class="t">\n| a |\n</pre>', 2, id="pre-with-attributes"),
            pytest.param("<div><pre><code>| a |</code></pre></div>", 1, id="nested-wrappers"),
            pytest.param("<div>| a |<br>| b |<br/>| c |</div>", 3, id="br-line-breaks"),
            pytest.param("<pre>a &#124; b &vert; c &#X7C; d</pre>", 1, id="pipe-entities"),
            pytest.param("<pre>| a &#124; b | c |</pre>", 1, id="in-cell-entity"),
            pytest.param("<pre>| a \\&#124; b |</pre>", 1, id="escaped-entity"),
            pytest.param("<pre>&#124; a \\&#124; b &#124;</pre>", 1, id="encoded-row"),
            pytest.param("<pre>&amp;#124; &lt;x&gt;</pre>", 1, id="entities"),
            pytest.param("<pre>| Path | artifact/<skill>/x |</pre>", 1, id="literal-angle-text"),
        ],
    )
    def test_optional_preview_rendered_height(self, preview: str, expected: int) -> None:
        """Rendered height stays capped; HTML entity tables are never decoded for item delivery."""
        assert _call("previewLineCount", preview) == expected


class TestOptionalHtmlPreviews:
    """Optional HTML previews cannot substitute for plain question-text evidence."""

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
    def test_wrapped_preview_requires_plain_question_summary(self, tmp_path: Path, preview: str) -> None:
        """Optional HTML previews cannot substitute for plain question-text evidence."""
        report = tmp_path / "report.md"
        report.write_text(_HEADER_REPORT, encoding="utf-8")

        assert _call("questionDeliveryProblem", str(report), _question(preview)) is not None
        assert _call("questionDeliveryProblem", str(report), _asked(_summary(report), preview)) is None

    @_skip_node_unavailable
    def test_html_table_preview_cannot_replace_summary(self, tmp_path: Path) -> None:
        """Optional HTML previews cannot substitute for plain question-text evidence."""
        report = tmp_path / "report.md"
        report.write_text(_HEADER_REPORT, encoding="utf-8")

        problem = _call("questionDeliveryProblem", str(report), _question(_HTML_TABLE))

        assert isinstance(problem, str)
        assert "first question's `question` text" in problem
        assert _call("questionDeliveryProblem", str(report), _asked(_summary(report), _HTML_TABLE)) is None

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
        """Optional HTML previews cannot substitute for plain question-text evidence."""
        report = tmp_path / "report.md"
        report.write_text("---\nTitle: Current review\nOutcome: PASS\nPath: A|B\n---\n", encoding="utf-8")

        assert _call("questionDeliveryProblem", str(report), _question(preview)) is not None
        assert _call("questionDeliveryProblem", str(report), _asked(_summary(report), preview)) is None

    @_skip_node_unavailable
    def test_audit_html_preview_requires_aggregate_summary(self, tmp_path: Path) -> None:
        """Optional HTML previews cannot substitute for plain question-text evidence."""
        report = tmp_path / "summary.jsonl"
        report.write_text(json.dumps({"sev": "high", "one_line": "a | b broken"}) + "\n", encoding="utf-8")
        preview = "<pre>## Audit Report\nTotal: 1\na &#124; b broken</pre>"

        assert _call("questionDeliveryProblem", str(report), _question(preview)) is not None
        assert _call("questionDeliveryProblem", str(report), _asked(f"Audit Report\n→ {report}", preview)) is None


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
    def test_summary_pass_leaves_stop_armed(self, follow_up_run: tuple[Path, Path]) -> None:
        """An allowed follow-up records nothing yet: another PreToolUse hook or a permission rule may still deny it.

        Recording at this point let a question the user never saw settle Stop, so the report went undelivered.
        """
        sentinel, report = follow_up_run
        payload = {"transcript_path": None, "tool_input": _asked(_summary(report))}

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
    """PostToolUse summaries never settle the separate final-reply delivery check."""

    @_skip_node_unavailable
    def test_shown_summary_does_not_settle_stop(self, follow_up_run: tuple[Path, Path]) -> None:
        """Showing the summary leaves Stop armed for the full table."""
        sentinel, report = follow_up_run
        payload = {"transcript_path": None, "tool_input": _asked(_summary(report))}

        assert _call("recordFollowUpShown", str(sentinel), str(report), payload) is False
        assert _call("stopBlockReason", str(sentinel), str(report), _stop_payload(), "oss:review") is not None

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
        payload = {
            "transcript_path": None,
            "tool_input": {
                "questions": [{"question": "What next?", "options": [{"preview": _HEADER_TABLE}, {"label": "skip"}]}]
            },
        }

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
        payload = {"transcript_path": None, "tool_input": _asked(_summary(report))}

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


@_skip_node_unavailable
def test_plain_question_summary_without_previews_passes(follow_up_run: tuple[Path, Path]) -> None:
    """The first question names the resolved report and its headline values without option previews."""
    _, report = follow_up_run
    call = _question(None)
    call["questions"][0]["question"] = f"Title: Current review\nOutcome: PASS\n→ {report}\nWhat next?"

    assert _call("questionDeliveryProblem", str(report), call) is None


@_skip_node_unavailable
@pytest.mark.parametrize(
    ("length", "allowed"), [pytest.param(1500, True, id="at-cap"), pytest.param(1501, False, id="over-cap")]
)
def test_question_summary_length_cap(follow_up_run: tuple[Path, Path], length: int, allowed: bool) -> None:
    """Even a complete headline summary must leave headroom below the host's question-text cut."""
    _, report = follow_up_run
    summary = f"Title: Current review\nOutcome: PASS\n→ {report}\n"
    call = _question(_HEADER_TABLE)
    call["questions"][0]["question"] = summary + "x" * (length - len(summary))
    problem = _call("questionDeliveryProblem", str(report), call)

    if allowed:
        assert problem is None
    else:
        assert isinstance(problem, str)
        assert "1500" in problem
        assert "fixed lines" in problem
        assert str(report) in problem


@_skip_node_unavailable
@pytest.mark.parametrize("final_table", [False, True])
def test_shown_summary_keeps_final_table_delivery_armed(follow_up_run: tuple[Path, Path], final_table: bool) -> None:
    """A shown summary leaves Stop armed; final delivery passes and each report gets at most one nudge."""
    sentinel, report = follow_up_run
    call = _question(None)
    call["questions"][0]["question"] = f"Title: Current review\nOutcome: PASS\n→ {report}"
    assert _call("followUpProblem", str(sentinel), str(report), {"tool_input": call}) is None
    assert _call("recordFollowUpShown", str(sentinel), str(report), {"tool_input": call}) is False
    assert not Path(_call("deliveredMarkerPath", str(sentinel))).exists()
    payload = _stop_payload(last_assistant_message=_HEADER_TABLE if final_table else "Done.")

    reason = _call("stopBlockReason", str(sentinel), str(report), payload, "oss:review")

    assert (reason is None) is final_table
    if not final_table:
        assert "| Field | Value |" in reason
    assert _call("stopBlockReason", str(sentinel), str(report), _stop_payload(), "oss:review") is None


@_skip_node_unavailable
@pytest.mark.parametrize(
    ("header", "summary", "allowed"),
    [
        pytest.param(
            "Title: Current review\nVerdict: APPROVE\nDate: today",
            "Current review\nAPPROVE",
            True,
            id="verdict-fallback",
        ),
        pytest.param(
            "Title: Current review\nOutcome: PASS\nVerdict: REJECT", "Current review\nPASS", True, id="outcome-wins"
        ),
        pytest.param(
            "Title: Current review\nOutcome: PASS\nVerdict: REJECT",
            "Current review\nREJECT",
            False,
            id="verdict-cannot-replace-outcome",
        ),
        pytest.param("Title: Current review\nDate: today\nScope: hooks", "Current review", True, id="no-outcome"),
        pytest.param("Date: today\nScope: hooks\nSummary: ready", "", True, id="path-only-without-headlines"),
        pytest.param("Outcome: PASS\nDate: today\nScope: hooks", "PASS", True, id="outcome-without-title"),
        pytest.param("Title: Current review\nOutcome: PASS\nDate: today", "PASS", False, id="missing-title"),
    ],
)
def test_question_requires_only_present_headline_fields(
    tmp_path: Path, header: str, summary: str, allowed: bool
) -> None:
    """The question contract selects existing headline fields without manufacturing missing ones."""
    report = tmp_path / "report.md"
    report.write_text(f"---\n{header}\n---\n", encoding="utf-8")
    problem = _call("questionDeliveryProblem", str(report), _asked(f"{summary}\n→ {report}"))
    assert (problem is None) is allowed


@_skip_node_unavailable
@pytest.mark.parametrize(
    ("saved", "shown", "allowed"),
    [
        pytest.param("PASS", "**PASS**", True, id="bold-value"),
        pytest.param("PASS", "_PASS_", True, id="italic-value"),
        pytest.param("PASS", "**`PASS`**", True, id="nested-wrappers"),
        pytest.param("NEEDS WORK", "NEEDS **WORK**", True, id="inline-markdown"),
        pytest.param("__init__", "init", False, id="literal-dunder-required"),
        pytest.param("a_b_c", "abc", False, id="literal-underscores-required"),
        pytest.param("**PASS**", "PASS", False, id="saved-markup-required"),
        pytest.param("plugins/*/hooks/*.js", "**plugins/*/hooks/*.js**", True, id="literal-glob-under-wrapper"),
        pytest.param("`a` b", "`` `a` b ``", True, id="literal-code-under-wrapper"),
    ],
)
def test_question_summary_preserves_saved_values(tmp_path: Path, saved: str, shown: str, allowed: bool) -> None:
    """Summary matching shares table-cell Markdown tolerance while preserving saved literal characters."""
    report = tmp_path / "report.md"
    report.write_text(f"---\nTitle: Current review\nOutcome: {saved}\nDate: today\n---\n", encoding="utf-8")
    problem = _call("questionDeliveryProblem", str(report), _asked(_summary(report, outcome=shown)))
    assert (problem is None) is allowed


@_skip_node_unavailable
@pytest.mark.parametrize("shape", ["table", "html"])
def test_question_summary_requires_plain_text(tmp_path: Path, shape: str) -> None:
    """Putting all facts in a question's table or HTML wrapper is denied with a plain-text remedy."""
    report = tmp_path / "report.md"
    report.write_text(_HEADER_REPORT, encoding="utf-8")
    summary = _summary(report)
    text = _HEADER_TABLE + f"\n→ {report}" if shape == "table" else f"<pre>{summary}</pre>"
    problem = _call("questionDeliveryProblem", str(report), _asked(text))
    assert "use plain text" in problem
    assert _call("questionDeliveryProblem", str(report), _asked(summary)) is None


@_skip_node_unavailable
@pytest.mark.parametrize("placement", ["multi-select", "second-question"])
def test_optional_preview_caps_apply_wherever_present(tmp_path: Path, placement: str) -> None:
    """An optional preview exceeding the host cap is denied regardless of its question placement."""
    report = tmp_path / "report.md"
    report.write_text(_HEADER_REPORT, encoding="utf-8")
    call = _asked(_summary(report), multi_select=placement == "multi-select")
    target = call["questions"][0]
    if placement == "second-question":
        target = {"question": "Which branch?", "options": [{"label": "main"}]}
        call["questions"].append(target)
    target["options"][0]["preview"] = "x" * 2001
    assert "exceed the preview cap" in _call("questionDeliveryProblem", str(report), call)
    del target["options"][0]["preview"]
    assert _call("questionDeliveryProblem", str(report), call) is None


@_skip_node_unavailable
def test_question_can_name_report_relative_to_cwd_without_slash(tmp_path: Path) -> None:
    """A report in the session cwd can be named by its filename, without an artificial path prefix."""
    report = tmp_path / "report.md"
    report.write_text(_HEADER_REPORT, encoding="utf-8")
    call = _asked("Title: Current review\nOutcome: PASS\n→ report.md")
    assert _call("questionDeliveryProblem", str(report), call, str(tmp_path)) is None
    other_cwd = tmp_path / "other"
    other_cwd.mkdir()
    (other_cwd / "report.md").write_text(_HEADER_REPORT, encoding="utf-8")
    assert _call("questionDeliveryProblem", str(report), call, str(other_cwd)) is not None

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

Every test drives the real module through a Node subprocess: the JavaScript export is the contract, nothing is mocked.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from functools import partial
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parent.parent.parent / "hooks" / "report-header-table.js"

_skip_node_unavailable = pytest.mark.skipif(shutil.which("node") is None, reason="requires node to execute the module")

#: Calls ``require(<module>)[<export>](...JSON.parse(<args>))`` and prints the result as JSON.
_CALL_SCRIPT = (
    "const mod = require(process.argv[1]);"
    "process.stdout.write(JSON.stringify(mod[process.argv[2]](...JSON.parse(process.argv[3]))));"
)

_HEADER_REPORT = "---\nTitle: Current review\nOutcome: PASS\nSummary: Verified result\n---\n"
_HEADER_TABLE = (
    "| Field | Value |\n| --- | --- |\n| Title | Current review |\n| Outcome | PASS |\n| Summary | Verified result |"
)
_PATH_REPORT = "---\nTitle: Current review\nOutcome: PASS\nPath: .reports/x/report.md\n---\n"


def _path_table(title: str = "Current review", outcome: str = "PASS", path: str = ".reports/x/report.md") -> str:
    """Render the saved header's cells for final-reply matching."""
    return f"| Field | Value |\n| --- | --- |\n| Title | {title} |\n| Outcome | {outcome} |\n| Path | {path} |"


_PATH_TABLE = _path_table()

_UNRELATED_QUESTION = {"question": "Which branch?", "options": [{"label": "main"}]}


def _call(name: str, *args: object) -> object:
    """Call one export of report-header-table.js in a separate Node process."""
    proc = subprocess.run(
        ["node", "-e", _CALL_SCRIPT, str(MODULE), name, json.dumps(args)],
        capture_output=True,
        encoding="utf-8",
        timeout=10,
        check=True,
    )
    return json.loads(proc.stdout)


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


def _reply_transcript(tmp_path: Path, text: str, block: str = "text") -> Path:
    """Write a transcript whose only row is one assistant block of kind `block` (``text`` or ``thinking``)."""
    row = {"type": "assistant", "message": {"content": [{"type": block, block: text}]}}
    return _write_transcript(tmp_path, [row])


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


def _stop_payload(**overrides: object) -> dict:
    """Build a Stop payload with no transcript and an empty final message, applying `overrides`."""
    payload: dict = {"hook_event_name": "Stop", "stop_hook_active": False, "last_assistant_message": ""}
    payload.update(overrides)
    return payload


def _mid_turn_table_transcript(tmp_path: Path, final_text: str) -> Path:
    """Write a transcript whose turn printed `_PATH_TABLE`, ran a tool, then ended its reply with `final_text`."""
    return _write_transcript(
        tmp_path,
        [
            {"type": "user", "message": {"content": [{"type": "text", "text": "go"}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": _PATH_TABLE}]}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": final_text}]}},
        ],
    )


def _html_padded(text: str, total: int) -> str:
    """Return `text` in `<pre>`, padded with a wrapper-tag attribute to exactly `total` raw chars.

    Wrapper tags are removed before lines are measured but count toward the raw char cap, so the padding moves a preview
    across the 2000-char boundary without changing the lines it renders. At the 86-column wrap width, plain text within
    12 lines holds at most 1043 chars, so only markup reaches the char cap.
    """
    shell = f'<pre>{text}</pre><span title=""></span>'
    return shell.replace('title=""', f'title="{"z" * (total - len(shell))}"')


def _set_question(call: dict, summary: str, *, key: str, index: int = 0) -> None:
    """Put `summary` in field `key` of question `index` itself."""
    call["questions"][index][key] = summary


def _set_options(
    call: dict, summary: str, *, key: str, index: int = 0, which: slice = slice(None), wrap: str = "{}"
) -> None:
    """Put `summary` (inside `wrap`) in field `key` of the selected options of question `index`."""
    for option in call["questions"][index]["options"][which]:
        option[key] = wrap.format(summary)


def _after_unrelated(place: Callable[[dict, str], None]) -> Callable[[dict, str], None]:
    """Return a placer that first puts an unrelated question ahead of the real one, then runs `place`."""

    def run(call: dict, summary: str) -> None:
        """Prepend the unrelated question so the real one becomes the second, then place the summary."""
        call["questions"].insert(0, dict(_UNRELATED_QUESTION))
        place(call, summary)

    return run


def _then_unrelated(place: Callable[[dict, str], None]) -> Callable[[dict, str], None]:
    """Return a placer that appends an unrelated question after the real one, then runs `place` on that one."""

    def run(call: dict, summary: str) -> None:
        """Append the unrelated question as the second one, then place the summary on it."""
        call["questions"].append(dict(_UNRELATED_QUESTION))
        place(call, summary)

    return run


@pytest.fixture(name="header_report")
def _header_report(tmp_path: Path) -> Path:
    """Write the three-field ``---`` header report (Title, Outcome, Summary) and return its path."""
    report = tmp_path / "report.md"
    report.write_text(_HEADER_REPORT, encoding="utf-8")
    return report


@pytest.fixture(name="path_report")
def _path_report(tmp_path: Path) -> Path:
    """Write the header report whose last field is a ``Path`` and return its path."""
    report = tmp_path / "report.md"
    report.write_text(_PATH_REPORT, encoding="utf-8")
    return report


@pytest.fixture(name="follow_up_run")
def _follow_up_run(tmp_path: Path, header_report: Path) -> tuple[Path, Path]:
    """Stage a workflow sentinel beside its written `---` header report."""
    sentinel = tmp_path / "wf-report-dir-test-session-1234"
    sentinel.write_text("x\n", encoding="utf-8")
    return sentinel, header_report


@pytest.fixture(name="stop_run")
def _stop_run(tmp_path: Path, path_report: Path) -> tuple[Path, Path]:
    """Stage a workflow sentinel and its written report."""
    sentinel = tmp_path / "wf-report-dir-test-session-1234"
    sentinel.write_text("x\n", encoding="utf-8")
    return sentinel, path_report


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


@pytest.fixture(name="audit_single")
def _audit_single(tmp_path: Path) -> Path:
    """Write a one-finding audit aggregate and return its path."""
    aggregate = tmp_path / "summary.jsonl"
    aggregate.write_text(json.dumps({"sev": "high", "one_line": "broken ref"}) + "\n", encoding="utf-8")
    return aggregate


@_skip_node_unavailable
class TestHasHeaderTable:
    """``hasHeaderTable`` recognises a rendered header table or its documented one-line fallback."""

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
            pytest.param(
                "verdict: APPROVE · findings: 3 · file: review-report.md", True, id="fallback-dot-separated-line"
            ),
            pytest.param("", False, id="empty-text"),
        ],
    )
    def test_detection(self, text: str, expected: bool) -> None:
        """A rendered header table, or the documented one-line fallback, is detected; anything else is not.

        Scenario: a `| Field | Value |` table with >= MIN_TABLE_ROWS data rows counts; SKILL.md's `·`-separated one-line
        fallback (used when the report read fails) also counts; raw fields printed one per line with no table (the
        exact failure this module guards against), fewer than MIN_TABLE_ROWS data rows (stray prose pipes) and empty
        text (an unreadable transcript) are never mistaken for a printed table.
        """
        assert _call("hasHeaderTable", text) is expected


@_skip_node_unavailable
class TestDeliveryProblem:
    """``deliveryProblem`` judges whether the report's header reached the user in the final reply."""

    @pytest.mark.parametrize(
        ("report_text", "text"),
        [
            pytest.param(_HEADER_REPORT, "", id="absent-table"),
            pytest.param(
                _HEADER_REPORT,
                "| Field | Value |\n| --- | --- |\n| Title | unrelated |\n| Outcome | PASS |\n| Summary | old |",
                id="unrelated-table",
            ),
            pytest.param(
                "---\nTitle: Current\nOutcome: PASS\nSummary: Verified\n---\n",
                "Title Current Outcome PASS Summary Verified\n| Field | Value |\n| --- | --- |\n| Title | Other |\n"
                "| Outcome | PASS |\n| Summary | Stale |",
                id="raw-fields-plus-unrelated-table",
            ),
        ],
    )
    def test_rejects_absent_or_unrelated_table(self, tmp_path: Path, report_text: str, text: str) -> None:
        """File presence and another report's table cannot establish this report's delivery.

        Scenario: an absent table, a table of another report, and header fields that occur as prose beside an
        unrelated table all fail, because header fields must occur as rows in one matching table.
        """
        report = tmp_path / "report.md"
        report.write_text(report_text, encoding="utf-8")
        transcript = _reply_transcript(tmp_path, text)
        assert _call("deliveryProblem", str(report), str(transcript)) is not None

    def test_reports_zero_visible_chars_for_thinking_only_reply(self, tmp_path: Path, header_report: Path) -> None:
        """A header table written only in thinking is rejected, and the reason reports 0 visible reply chars.

        Observed in a resolve run: the model "printed" its table inside reasoning, got denied four times, and blamed
        the hook. The char count plus the thinking note tells it the problem is its own reply, not the gate.
        """
        transcript = _reply_transcript(tmp_path, _HEADER_TABLE, block="thinking")
        problem = _call("deliveryProblem", str(header_report), str(transcript))
        assert isinstance(problem, str)
        assert ("0 chars" in problem, "thinking" in problem) == (True, True)

    def test_accepts_bound_header(self, tmp_path: Path, header_report: Path) -> None:
        """The current complete header table permits the follow-up transition.

        The final reply holds a table whose every row equals the saved header, so no delivery problem is reported.
        """
        transcript = _reply_transcript(tmp_path, _HEADER_TABLE)
        assert _call("deliveryProblem", str(header_report), str(transcript)) is None

    def _deliver_path(self, tmp_path: Path, saved: str, delivered: str) -> object:
        """Judge a reply whose `Path` cell is `delivered` against a report whose `Path` field is `saved`."""
        report = tmp_path / "report.md"
        report.write_text(f"---\nTitle: Current\nOutcome: PASS\nPath: {saved}\n---\n", encoding="utf-8")
        text = f"| Field | Value |\n| --- | --- |\n| Title | Current |\n| Outcome | PASS |\n| Path | {delivered} |"
        return _call("deliveryProblem", str(report), str(_reply_transcript(tmp_path, text)))

    @pytest.mark.parametrize(
        ("saved", "delivered"),
        [
            pytest.param(r"C:\reports\q", r"C:\reports\q", id="literal-path-delivered"),
            pytest.param("A|B", r"A\|B", id="table-pipe-escaped"),
        ],
    )
    def test_accepts_literal_header_values(self, tmp_path: Path, saved: str, delivered: str) -> None:
        """A literal path or a table-escaped pipe delivers the saved value.

        Formatting tolerance (escaping a pipe so it survives inside a table cell) must still count as the same value.
        """
        assert self._deliver_path(tmp_path, saved, delivered) is None

    @pytest.mark.parametrize(
        ("saved", "delivered"),
        [
            pytest.param(r"C:\reports\q", "C:reportsq", id="path-separators-missing"),
            pytest.param("A|B", "AB", id="literal-pipe-missing"),
            pytest.param("A*B", "AB", id="literal-star-missing"),
            pytest.param("A`B", "AB", id="literal-backtick-missing"),
        ],
    )
    def test_rejects_header_value_missing_literal_characters(self, tmp_path: Path, saved: str, delivered: str) -> None:
        """Formatting tolerance cannot equate different paths or erase literal header characters.

        A cell that drops backslashes, pipes, stars or backticks the saved file holds is a different value, so the
        header counts as undelivered and the standard remedy prefix is reported.
        """
        problem = self._deliver_path(tmp_path, saved, delivered)
        assert isinstance(problem, str)
        assert problem.startswith(
            "current report header was not delivered; print every header field as a table before following up"
        )

    def _deliver_finding(self, tmp_path: Path, delivered: str) -> object:
        """Judge an Audit Report whose finding line is `delivered` against an aggregate holding the finding `A|B`."""
        report = tmp_path / "summary.jsonl"
        report.write_text(json.dumps({"sev": "high", "one_line": "A|B"}) + "\n", encoding="utf-8")
        text = f"## Audit Report\nTotal: 1\n{delivered}"
        return _call("deliveryProblem", str(report), str(_reply_transcript(tmp_path, text)))

    @pytest.mark.parametrize(
        "delivered",
        [pytest.param("A|B", id="audit-literal-pipe"), pytest.param(r"A\|B", id="audit-table-pipe-escaped")],
    )
    def test_accepts_audit_finding_with_literal_punctuation(self, tmp_path: Path, delivered: str) -> None:
        """An audit finding delivered with its literal pipe, or a table-escaped one, is accepted.

        Audit findings retain their punctuation; escaping for a table cell is the only tolerated difference.
        """
        assert self._deliver_finding(tmp_path, delivered) is None

    def test_rejects_audit_finding_missing_punctuation(self, tmp_path: Path) -> None:
        """Audit findings retain literal punctuation rather than accepting a different description.

        A finding printed as ``AB`` for the saved ``A|B`` is a different description, so the audit counts as undelivered
        and the Step 7 remedy is named.
        """
        problem = self._deliver_finding(tmp_path, "AB")
        assert isinstance(problem, str)
        assert problem.startswith("current audit findings were not delivered; emit Step 7 before following up")


@_skip_node_unavailable
class TestQuestionSummaryFields:
    """Only first-question text supplies summary evidence."""

    @pytest.mark.parametrize(
        "tool_input",
        [
            pytest.param(None, id="null"),
            pytest.param({}, id="empty"),
            pytest.param({"questions": "x"}, id="not-array"),
            pytest.param({"questions": [None, {"options": [None]}]}, id="malformed-entries"),
        ],
    )
    def test_malformed_input_yields_no_summary(self, header_report: Path, tool_input: object) -> None:
        """Malformed input denies the transition without crashing the library.

        A null, empty, non-array or entry-malformed AskUserQuestion input carries no summary, so the report is reported
        undelivered with a string remedy instead of an exception.
        """
        assert isinstance(_call("questionDeliveryProblem", str(header_report), tool_input), str)


@_skip_node_unavailable
class TestDeliveryViaQuestion:
    """The question summary is independent of full-table delivery at Stop."""

    @pytest.mark.parametrize(
        "suffix", [pytest.param("", id="summary-only"), pytest.param("\nWhat next?", id="summary-then-question")]
    )
    def test_plain_summary_allows(self, header_report: Path, suffix: str) -> None:
        """Headline values and a report path pass without previews or a header table.

        The first question names the resolved report and its headline values as plain text, optionally followed by the
        actual question; no option previews are needed.
        """
        assert _call("questionDeliveryProblem", str(header_report), _asked(_summary(header_report) + suffix)) is None

    def test_stale_summary_is_denied_and_names_the_summary_fix(self, header_report: Path) -> None:
        """A stale outcome is denied with the required value and first-question remedy.

        The summary claims FAIL while the file says PASS; the remedy names the expected value and where it belongs, and
        the corrected summary then passes.
        """
        problem = _call("questionDeliveryProblem", str(header_report), _asked(_summary(header_report, outcome="FAIL")))
        assert "Outcome: PASS" in problem
        assert "first question's `question` text" in problem
        assert _call("questionDeliveryProblem", str(header_report), _asked(_summary(header_report))) is None

    def test_call_without_summary_names_fixed_lines(self, header_report: Path) -> None:
        """Missing summary evidence yields a bounded, concrete remedy.

        A question carrying no summary at all is told the exact fixed lines to add: the headline values and the path.
        """
        problem = _call("questionDeliveryProblem", str(header_report), _question(None))
        assert all(
            fragment in problem
            for fragment in ("Title: Current review", "Outcome: PASS", str(header_report), "fixed lines")
        )

    def test_reply_text_table_is_denied_and_the_summary_fix_passes(
        self, tmp_path: Path, follow_up_run: tuple[Path, Path]
    ) -> None:
        """Earlier reply text cannot replace the first question's summary.

        A complete table printed before the call does not count (that text may never reach the user); only the first
        question's own text does, and the corrected call then passes.
        """
        sentinel, report = follow_up_run
        transcript = _reply_transcript(tmp_path, _HEADER_TABLE)
        denied = _call(
            "followUpProblem",
            str(sentinel),
            str(report),
            {"transcript_path": str(transcript), "tool_input": _question(None)},
        )
        assert "reply text before the call does not count" in denied
        assert _call("followUpProblem", str(sentinel), str(report), {"tool_input": _asked(_summary(report))}) is None

    def test_summary_split_across_fields_never_combines(self, header_report: Path) -> None:
        """A title in question text cannot combine with the outcome and path in previews.

        Evidence is read from the first question's text alone; fragments spread over text and previews are not joined.
        """
        call = _asked("Title: Current review", f"Outcome: PASS\n→ {header_report}")
        assert _call("questionDeliveryProblem", str(header_report), call) is not None

    def test_incomplete_report_wins_over_matching_summary(self, tmp_path: Path) -> None:
        """Matching text cannot authorize a follow-up whose report header is unfinished.

        The report lacks its ``Summary`` field, so even a summary matching every present field is refused until the
        producer finishes.
        """
        report = tmp_path / "report.md"
        report.write_text("---\nTitle: Current review\nOutcome: PASS\n---\n", encoding="utf-8")
        assert (
            _call("questionDeliveryProblem", str(report), _asked(_summary(report)))
            == "report header is incomplete; finish the report before following up"
        )

    def test_audit_headline_and_aggregate_path_allow(self, audit_single: Path) -> None:
        """Audit uses the aggregate path and existing headline without inventing header fields.

        An audit has no header fields; ``Audit Report`` plus the aggregate's path is the whole summary.
        """
        assert _call("questionDeliveryProblem", str(audit_single), _asked(f"Audit Report\n→ {audit_single}")) is None


@_skip_node_unavailable
class TestFirstQuestionPlacement:
    """Every misplaced summary can be repaired by moving it to the first question's text."""

    @pytest.mark.parametrize(
        ("place", "multi_select"),
        [
            pytest.param(partial(_set_options, key="preview"), False, id="every-option-preview"),
            pytest.param(
                partial(_set_options, key="preview", which=slice(-1, None)), False, id="last-option-preview-only"
            ),
            pytest.param(
                partial(_set_options, key="preview", which=slice(-1, None), wrap="<pre>{}</pre>"),
                False,
                id="html-first-option-preview-only",
            ),
            pytest.param(partial(_set_options, key="preview"), True, id="multi-select-preview"),
            pytest.param(partial(_set_options, key="preview", which=slice(-1, None)), True, id="multi-select-previews"),
            pytest.param(partial(_set_options, key="description"), False, id="option-description"),
            pytest.param(
                partial(_set_options, key="description", which=slice(0, 1)), False, id="one-option-description"
            ),
            pytest.param(
                partial(_set_options, key="description", which=slice(0, 1)), True, id="multi-select-description"
            ),
            pytest.param(partial(_set_question, key="description"), False, id="form-question-level-description"),
            pytest.param(
                _after_unrelated(partial(_set_options, key="preview", index=1, which=slice(-1, None))),
                False,
                id="previews-of-second-question",
            ),
            pytest.param(
                _after_unrelated(partial(_set_question, key="question", index=1)), False, id="text-of-second-question"
            ),
            pytest.param(
                _after_unrelated(partial(_set_options, key="description", index=1, which=slice(0, 1))),
                False,
                id="description-of-second-question",
            ),
        ],
    )
    def test_misplaced_summary_is_denied_and_the_named_fix_passes(
        self, header_report: Path, place: Callable[[dict, str], None], multi_select: bool
    ) -> None:
        """Later questions, previews, descriptions and helper lines cannot provide first-question evidence.

        The summary is put anywhere except the first question's own text (previews, option or question descriptions, a
        later question); the denial names the first question's `question` text as the fix, and moving the summary there
        passes.
        """
        summary = _summary(header_report)
        call = _question(None, multi_select=multi_select)
        place(call, summary)
        problem = _call("questionDeliveryProblem", str(header_report), call)
        assert "first question's `question` text" in problem
        call["questions"][0]["question"] = summary
        assert _call("questionDeliveryProblem", str(header_report), call) is None

    def test_first_question_text_delivers_on_its_own(self, header_report: Path) -> None:
        """A later unrelated question does not invalidate a complete first summary.

        The first question carries the whole summary; a second question without options is simply ignored.
        """
        call = _asked(_summary(header_report))
        call["questions"].append({"question": "Which branch?"})
        assert _call("questionDeliveryProblem", str(header_report), call) is None


@_skip_node_unavailable
class TestInlineMarkdownCells:
    """A value cell matches verbatim or with its inline markdown removed; the saved value is never unwrapped.

    Bolding a verdict or backticking a path is common table styling. Before, it was denied with a remedy saying no field
    held the table, so the model re-sent the same shape and was denied again.
    """

    @pytest.mark.parametrize(
        "table",
        [
            pytest.param(_path_table(outcome="**PASS**"), id="bold-value"),
            pytest.param(_path_table(path="`.reports/x/report.md`"), id="backticked-path"),
            pytest.param(_path_table(outcome="_PASS_"), id="underscore-italic-value"),
            pytest.param(_path_table(outcome="*PASS*"), id="star-italic-value"),
            pytest.param(_path_table(path="``.reports/x/report.md``"), id="double-backtick-span"),
            pytest.param(_path_table(outcome="**`PASS`**"), id="nested-bold-code"),
            pytest.param(_PATH_TABLE.replace("| Title |", "| **Title** |"), id="bold-field-name"),
        ],
    )
    def test_formatted_table_passes(self, path_report: Path, table: str) -> None:
        """Markdown cells (bold, italic, code spans, a bold field name) pass in the final reply.

        Stripping the inline markup from a delivered cell must give back the saved value.
        """
        assert _call("deliveryProblem", str(path_report), None, table) is None

    def test_html_preview_table_is_denied_until_rendered_as_markdown(self, path_report: Path) -> None:
        """A legacy ``<pre>``-wrapped table is denied; the same table as plain Markdown passes.

        HTML is not a delivery format for the final reply, so the wrapper must be removed before the cells count.
        """
        wrapped = f"<pre>{_path_table(outcome='**PASS**')}</pre>"
        unwrapped = wrapped.removeprefix("<pre>").removesuffix("</pre>")
        assert (
            _call("deliveryProblem", str(path_report), None, wrapped) is not None,
            _call("deliveryProblem", str(path_report), None, unwrapped),
        ) == (True, None)

    @pytest.mark.parametrize(
        ("saved", "delivered"),
        [
            pytest.param("__init__", "init", id="dunder-not-stripped-from-saved"),
            pytest.param("a_b_c", "abc", id="snake-case-kept"),
            pytest.param("**PASS**", "PASS", id="saved-markup-required"),
        ],
    )
    def test_cell_dropping_saved_characters_is_denied(self, tmp_path: Path, saved: str, delivered: str) -> None:
        """Only the cell is unwrapped, so a cell missing characters the file holds is still a mismatch.

        The saved Outcome contains underscores or asterisks that markup stripping must not be allowed to erase.
        """
        report = tmp_path / "report.md"
        report.write_text(_PATH_REPORT.replace("PASS", saved), encoding="utf-8")

        assert _call("deliveryProblem", str(report), None, _path_table(outcome=delivered)) is not None

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
        self, path_report: Path, mismatched: str, named: str
    ) -> None:
        """A table that differs from the file is denied with the differing row named; the corrected final reply passes.

        Denial followed by the corrected final reply is one contract — the remedy must say exactly what to change.
        """
        denied = _call("deliveryProblem", str(path_report), None, mismatched)
        retried = _call("deliveryProblem", str(path_report), None, _path_table(path="`.reports/x/report.md`"))

        assert isinstance(denied, str)
        assert [
            "table is present but does not match the report file" in denied,
            named in denied,
            "copy every field name and value verbatim" in denied,
            retried is None,
        ] == [True] * 4

    def test_stop_reason_names_the_near_match_difference(self, path_report: Path) -> None:
        """At Stop a mismatched final-reply table is named too, after the unchanged reason prefix.

        The reply table says FAIL for a saved PASS; the Stop reason keeps its stable prefix and appends the difference.
        """
        problem = _call("deliveryProblem", str(path_report), None, _path_table(outcome="FAIL"))

        assert isinstance(problem, str)
        assert problem.startswith("current report header was not delivered; print every header field as a table")
        assert "`Outcome` reads `FAIL` but the file has `PASS`" in problem

    def test_audit_near_match_names_the_missing_lines(self, audit_single: Path) -> None:
        """An Audit Report with a wrong total is named, instead of "no field holds it".

        The saved aggregate holds one finding but the printed report claims two; the remedy names the missing line.
        """
        problem = _call("deliveryProblem", str(audit_single), None, "## Audit Report\nTotal: 2\nbroken ref")

        assert isinstance(problem, str)
        assert "An Audit Report is present but does not match the findings aggregate: no `Total: 1` line" in problem

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


@_skip_node_unavailable
class TestPreviewCap:
    """An option preview over 2000 chars or 12 lines never delivers: the host withholds or clips it, with no scroll.

    Claude Code 2.1.294 shows a placeholder instead of a preview longer than 2000 chars and a box of terminal rows − 26
    lines, terminal columns − 34 wide; a line wider than the box wraps onto extra rows. A gate passing such a call
    recorded a report as delivered that the user never saw.
    """

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

    def test_large_nonheadline_field_does_not_expand_summary(self, tmp_path: Path) -> None:
        """Only fixed headline fields are required even when other header values are wide.

        A 120-char ``Summary`` value is not part of the headline, so the plain summary still fits and passes.
        """
        report = tmp_path / "report.md"
        report.write_text(_HEADER_REPORT.replace("Verified result", "s" * 120), encoding="utf-8")
        assert _call("questionDeliveryProblem", str(report), _question(None)) is not None
        assert _call("questionDeliveryProblem", str(report), _asked(_summary(report))) is None

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
        self, header_report: Path, over: str, at_cap: str, named: str
    ) -> None:
        """Optional previews remain capped even when the question summary is already complete.

        The over-cap preview is denied naming the exceeded measure; the same call at exactly the cap passes.
        """
        problem = _call("questionDeliveryProblem", str(header_report), _asked(_summary(header_report), over))
        assert "exceed the preview cap" in problem
        assert named in problem
        assert _call("questionDeliveryProblem", str(header_report), _asked(_summary(header_report), at_cap)) is None

    def test_over_cap_preview_records_nothing_for_stop(self, follow_up_run: tuple[Path, Path]) -> None:
        """A shown question with an over-cap preview cannot settle final report delivery.

        Stop must still demand the table afterwards, because the user never saw the clipped preview.
        """
        sentinel, report = follow_up_run
        payload = {"tool_input": _asked(_summary(report), "x" * 2001)}
        assert _call("recordFollowUpShown", str(sentinel), str(report), payload) is False
        assert _call("stopBlockReason", str(sentinel), str(report), _stop_payload(), "oss:review") is not None

    def test_over_cap_question_text_is_denied(self, header_report: Path) -> None:
        """A summary beyond the host cut cannot be rescued by preview evidence.

        The question text is padded past 1500 characters while a complete table sits in the previews.
        """
        call = _asked("x" * 2000 + "\n" + _summary(header_report), _HEADER_TABLE)
        assert "question text exceeds 1500 characters" in _call("questionDeliveryProblem", str(header_report), call)

    @pytest.mark.parametrize(
        ("place", "multi_select"),
        [
            pytest.param(partial(_set_options, key="preview", which=slice(0, 1)), True, id="multi-select"),
            pytest.param(
                _then_unrelated(partial(_set_options, key="preview", index=1, which=slice(0, 1))),
                False,
                id="second-question",
            ),
        ],
    )
    def test_optional_preview_caps_apply_wherever_present(
        self, header_report: Path, place: Callable[[dict, str], None], multi_select: bool
    ) -> None:
        """An optional preview exceeding the host cap is denied regardless of its question placement.

        The same placement with a short preview passes, so the cap alone decides.
        """
        call = _asked(_summary(header_report), multi_select=multi_select)
        place(call, "x" * 2001)
        assert "exceed the preview cap" in _call("questionDeliveryProblem", str(header_report), call)
        place(call, "x")
        assert _call("questionDeliveryProblem", str(header_report), call) is None


@_skip_node_unavailable
class TestNamedSummaryDelivery:
    """Named summaries use one contract regardless of the report's table size."""

    def test_over_cap_table_is_denied_and_plain_summary_passes(self, long_report: Path) -> None:
        """A large table preview is denied; the fixed question lines pass without previews.

        A 16-row table cannot fit a preview, so the plain summary is the only delivery route.
        """
        denied = _call("questionDeliveryProblem", str(long_report), _question(_LONG_TABLE))
        assert "option 1: 16 lines" in denied
        assert "fixed lines" in denied
        assert _call("questionDeliveryProblem", str(long_report), _asked(_summary(long_report))) is None

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
        """Existing report path matching accepts native absolute paths and relative suffixes.

        The summary may name the report by absolute path, by a path relative to the session cwd, or by a suffix of the
        absolute path.
        """
        call = _asked(f"Title: Current review\nOutcome: PASS\n→ {named(long_report)}")
        assert _call("questionDeliveryProblem", str(long_report), call, session(long_report)) is None

    def test_preview_may_name_report_in_another_form(self, long_report: Path) -> None:
        """A relative path in an optional preview does not affect a complete question summary.

        Only the question text names the report; a differently spelled path in a preview is ignored.
        """
        call = _asked(_summary(long_report), "Outcome: PASS\n→ run/report.md")
        assert _call("questionDeliveryProblem", str(long_report), call, str(long_report.parent.parent)) is None

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
        """The summary must name the correct existing report and present headline values.

        Each case omits or contradicts one requirement (path in the wrong field, a missing or unrelated file, a stale
        outcome, an over-cap preview); the remedy fragment pins which requirement failed.
        """
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

    def test_small_report_summary_passes_without_settling_stop(self, follow_up_run: tuple[Path, Path]) -> None:
        """Small reports use the same question contract, but the table still belongs in the final reply.

        The summary passes the question gate, yet recording the shown question settles nothing for Stop.
        """
        sentinel, report = follow_up_run
        call = _asked(_summary(report))
        assert _call("questionDeliveryProblem", str(report), call) is None
        assert _call("recordFollowUpShown", str(sentinel), str(report), {"tool_input": call}) is False

    def test_multi_select_summary_delivers(self, long_report: Path) -> None:
        """Question text renders for multi-select questions too, so previews are unnecessary.

        A complete plain summary on a multi-select question passes with no previews at all.
        """
        call = _asked(_summary(long_report), multi_select=True)
        assert _call("questionDeliveryProblem", str(long_report), call) is None

    def test_shown_named_summary_leaves_stop_armed(self, long_report: Path) -> None:
        """A large report's shown summary cannot settle full-table delivery.

        After the question is recorded as shown, Stop still blocks once to demand the full table.
        """
        sentinel = long_report.parent.parent / "wf-report-dir-test-session-1234"
        payload = {"tool_input": _asked(_summary(long_report))}
        assert _call("recordFollowUpShown", str(sentinel), str(long_report), payload) is False
        assert _call("stopBlockReason", str(sentinel), str(long_report), _stop_payload(), "oss:review") is not None

    def test_audit_rendered_report_cannot_replace_aggregate_path(self, audit_run: tuple[Path, Path]) -> None:
        """The summary is bound to the aggregate the audit hook resolved.

        Naming the rendered Markdown copy instead of the JSONL aggregate is denied with the aggregate path in the
        remedy; naming the aggregate passes.
        """
        aggregate, rendered = audit_run
        problem = _call("questionDeliveryProblem", str(aggregate), _asked(f"Audit Report\n→ {rendered}"))
        assert str(aggregate) in problem
        assert _call("questionDeliveryProblem", str(aggregate), _asked(f"Audit Report\n→ {aggregate}")) is None

    def test_large_audit_aggregate_is_summary_evidence(self, audit_run: tuple[Path, Path]) -> None:
        """The JSONL aggregate path and existing headline suffice even for many findings.

        Fifteen findings do not need a rendered table in the question; the aggregate path is the evidence.
        """
        aggregate, _ = audit_run
        assert _call("questionDeliveryProblem", str(aggregate), _asked(f"Audit Report\n→ {aggregate}")) is None


@_skip_node_unavailable
class TestOptionalPreviewHeight:
    """Optional previews retain host height checks after removing legacy HTML table delivery."""

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
    def test_rendered_height(self, preview: str, expected: int) -> None:
        """Rendered height stays capped; HTML entity tables are never decoded for item delivery.

        Wrapper tags are removed and ``<br>`` counts as a line break, but entities such as ``&#124;`` stay literal text.
        """
        assert _call("previewLineCount", preview) == expected


#: Every row of a three-field header whose last field (`Path: A|B`) the html preview tests append themselves.
_PIPE_VALUE_ROWS = "| Field | Value |\n| --- | --- |\n| Title | Current review |\n| Outcome | PASS |\n"

_HTML_TABLE = (
    "<table><tr><th>Field</th><th>Value</th></tr><tr><td>Title</td><td>Current review</td></tr>"
    "<tr><td>Outcome</td><td>PASS</td></tr><tr><td>Summary</td><td>Verified result</td></tr></table>"
)


@_skip_node_unavailable
class TestOptionalHtmlPreviews:
    """Optional HTML previews cannot substitute for plain question-text evidence."""

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
    def test_wrapped_preview_requires_plain_question_summary(self, header_report: Path, preview: str) -> None:
        """A table in an HTML preview is denied alone; with a plain question summary beside it, the call passes.

        However the table is wrapped (pre, div, br rows, pipe entities, a real HTML table), the preview is optional
        decoration and never replaces the first question's plain-text summary.
        """
        assert _call("questionDeliveryProblem", str(header_report), _question(preview)) is not None
        assert _call("questionDeliveryProblem", str(header_report), _asked(_summary(header_report), preview)) is None

    def test_html_table_preview_cannot_replace_summary(self, header_report: Path) -> None:
        """An HTML ``<table>`` preview is denied alone with the first-question remedy; a plain summary makes it pass.

        A real HTML table is the legacy delivery shape; it is tolerated as decoration but never counts as evidence.
        """
        problem = _call("questionDeliveryProblem", str(header_report), _question(_HTML_TABLE))

        assert isinstance(problem, str)
        assert "first question's `question` text" in problem
        assert (
            _call("questionDeliveryProblem", str(header_report), _asked(_summary(header_report), _HTML_TABLE)) is None
        )

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
        """A pipe escaped inside a preview cell stays cell content, and the preview still needs a plain summary.

        The saved ``Path`` is ``A|B``; every escape spelling of that pipe is denied alone and accepted beside a plain
        question summary.
        """
        report = tmp_path / "report.md"
        report.write_text("---\nTitle: Current review\nOutcome: PASS\nPath: A|B\n---\n", encoding="utf-8")

        assert _call("questionDeliveryProblem", str(report), _question(preview)) is not None
        assert _call("questionDeliveryProblem", str(report), _asked(_summary(report), preview)) is None

    def test_audit_html_preview_requires_aggregate_summary(self, tmp_path: Path) -> None:
        """An audit's HTML preview cannot replace the plain aggregate summary either.

        The finding contains a pipe; its entity-encoded rendering in a preview is denied alone and accepted beside the
        ``Audit Report`` + aggregate-path question text.
        """
        report = tmp_path / "summary.jsonl"
        report.write_text(json.dumps({"sev": "high", "one_line": "a | b broken"}) + "\n", encoding="utf-8")
        preview = "<pre>## Audit Report\nTotal: 1\na &#124; b broken</pre>"

        assert _call("questionDeliveryProblem", str(report), _question(preview)) is not None
        assert _call("questionDeliveryProblem", str(report), _asked(f"Audit Report\n→ {report}", preview)) is None


@_skip_node_unavailable
class TestFollowUpProblem:
    """``followUpProblem`` is the PreToolUse entry point: question text plus transcript, no Stop bookkeeping."""

    def test_summary_pass_leaves_stop_armed(self, follow_up_run: tuple[Path, Path]) -> None:
        """An allowed follow-up records nothing yet: another PreToolUse hook or a permission rule may still deny it.

        Recording at this point let a question the user never saw settle Stop, so the report went undelivered.
        """
        sentinel, report = follow_up_run
        payload = {"transcript_path": None, "tool_input": _asked(_summary(report))}

        assert _call("followUpProblem", str(sentinel), str(report), payload) is None
        assert isinstance(_call("stopBlockReason", str(sentinel), str(report), _stop_payload(), "oss:review"), str)

    def test_denied_follow_up_leaves_stop_armed(self, follow_up_run: tuple[Path, Path]) -> None:
        """A denied follow-up records nothing, so Stop still enforces delivery once.

        The question carries no summary and is denied; the later Stop check must still block for the table.
        """
        sentinel, report = follow_up_run
        payload = {"transcript_path": None, "tool_input": _question(None)}

        assert isinstance(_call("followUpProblem", str(sentinel), str(report), payload), str)
        assert isinstance(_call("stopBlockReason", str(sentinel), str(report), _stop_payload(), "oss:review"), str)


@_skip_node_unavailable
class TestRecordFollowUpShown:
    """PostToolUse summaries never settle the separate final-reply delivery check."""

    def test_shown_summary_does_not_settle_stop(self, follow_up_run: tuple[Path, Path]) -> None:
        """Showing the summary leaves Stop armed for the full table.

        The shown question carries a complete summary; recording it returns False and Stop still blocks.
        """
        sentinel, report = follow_up_run
        payload = {"transcript_path": None, "tool_input": _asked(_summary(report))}

        assert _call("recordFollowUpShown", str(sentinel), str(report), payload) is False
        assert _call("stopBlockReason", str(sentinel), str(report), _stop_payload(), "oss:review") is not None

    def test_shown_question_without_the_report_records_nothing(self, follow_up_run: tuple[Path, Path]) -> None:
        """A shown question that did not carry the report never vouches for delivery; Stop stays armed.

        The question has no summary, so nothing is recorded and the Stop reason is still produced.
        """
        sentinel, report = follow_up_run
        payload = {"transcript_path": None, "tool_input": _question(None)}

        assert _call("recordFollowUpShown", str(sentinel), str(report), payload) is False
        assert isinstance(_call("stopBlockReason", str(sentinel), str(report), _stop_payload(), "oss:review"), str)

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

    def test_reply_text_table_records_nothing(self, tmp_path: Path, follow_up_run: tuple[Path, Path]) -> None:
        """A reply-text table before a shown question that does not carry it never vouches for delivery.

        That text may have come back as an empty progress update, so Stop must still demand the table once.
        """
        sentinel, report = follow_up_run
        transcript = _reply_transcript(tmp_path, _HEADER_TABLE)
        payload = {"transcript_path": str(transcript), "tool_input": _question(None)}

        assert _call("recordFollowUpShown", str(sentinel), str(report), payload) is False

    def test_missing_sentinel_records_nothing(self, follow_up_run: tuple[Path, Path]) -> None:
        """Without an active workflow sentinel there is no marker to write.

        A null sentinel path (no workflow running) makes the recorder a no-op even for a complete summary.
        """
        _, report = follow_up_run
        payload = {"transcript_path": None, "tool_input": _asked(_summary(report))}

        assert _call("recordFollowUpShown", None, str(report), payload) is False


@_skip_node_unavailable
class TestIsWorkflowFollowUp:
    """``isWorkflowFollowUp`` decides whether a question is the follow-up that the report gate must hold."""

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
                        {
                            "question": "What next?",
                            "header": "Recovery",
                            "options": [{"label": "Retry"}, {"label": "Stop"}],
                        }
                    ]
                },
                id="unrelated-what-next",
            ),
        ],
    )
    def test_unrelated_question_is_not_a_follow_up(self, question: dict, skill: str) -> None:
        """Neither a recovery question nor a generic one activates a workflow's report gate.

        Scenario: missing report delivery must not prevent asking how to recover the producer, and a generic "What
        next?" cannot activate a different workflow's report gate.
        """
        assert _call("isWorkflowFollowUp", question, skill) is False

    @pytest.mark.parametrize(
        "label",
        [
            pytest.param("/research:topic plan", id="topic-plan-option"),
            pytest.param("/research:plan", id="legacy-plan-option"),
        ],
    )
    def test_headerless_topic_follow_up_is_recognized_by_its_plan_option(self, label: str) -> None:
        """A topic follow-up whose option is the plan continuation activates the report gate even with no header.

        Scenario: the topic skill's option (a) is `/research:topic plan`; matching only the older `/research:plan`
        spelling let that question slip past the gate, so a follow-up could be asked before the header was delivered.
        """
        question = {"questions": [{"question": "What next?", "options": [{"label": label}, {"label": "Skip"}]}]}
        assert _call("isWorkflowFollowUp", question, "research:topic") is True


@_skip_node_unavailable
class TestFinalReplyText:
    """``finalReplyText`` returns only the orchestrator's own text after its last tool call."""

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
                        "message": {
                            "content": [{"type": "text", "text": "before"}, {"type": "tool_use", "name": "Read"}]
                        },
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
    def test_returns_only_the_turns_final_reply(self, tmp_path: Path, rows: list[dict], expected: str) -> None:
        """Only the orchestrator's own text after its last tool call — the turn's final reply — is returned.

        Scenario: text written before a tool call may come back as an empty progress update and focus mode shows only
        the final message, so it never counts; a final reply split over several rows is joined; a transcript still
        ending on a tool_result (final reply not yet flushed) yields "" instead of an earlier table; queue-operation /
        attachment / mode rows are skipped; subagent output (isSidechain: true) is not the orchestrator's reply.
        """
        transcript = _write_transcript(tmp_path, rows)

        assert _call("finalReplyText", str(transcript)) == expected

    def test_missing_transcript_path_returns_empty_string(self) -> None:
        """No transcript_path at all — caller treats this exactly like 'no table found', never a crash.

        The hook payload may omit the path; a null argument must yield an empty reply, not an exception.
        """
        assert _call("finalReplyText", None) == ""

    def test_unreadable_transcript_path_returns_empty_string(self, tmp_path: Path) -> None:
        """A path that doesn't resolve to a file fails open to an empty string.

        A transcript that was rotated away or never written must not break the hook.
        """
        assert _call("finalReplyText", str(tmp_path / "does-not-exist.jsonl")) == ""


@_skip_node_unavailable
class TestStopBlockReason:
    """``stopBlockReason`` keeps a turn going once when the report header was never delivered."""

    def test_blocks_undelivered_report_once(self, stop_run: tuple[Path, Path]) -> None:
        """A turn ending without the header is kept going once; the same report never re-blocks.

        The first Stop names the workflow and the missing ``Path`` field; the second Stop for the same report version
        passes so the session cannot loop.
        """
        sentinel, report = stop_run

        reason = _call("stopBlockReason", str(sentinel), str(report), _stop_payload(), "oss:review")

        assert isinstance(reason, str)
        assert ("oss:review" in reason, "Path" in reason) == (True, True)
        assert _call("stopBlockReason", str(sentinel), str(report), _stop_payload(), "oss:review") is None

    def test_rechecks_rewritten_report(self, stop_run: tuple[Path, Path]) -> None:
        """A delivered report that is later rewritten must be delivered again.

        Delivery is bound to the report version: after the file changes (PASS to NEEDS_WORK) the old delivery no longer
        counts and Stop blocks again.
        """
        sentinel, report = stop_run
        delivered = _stop_payload(last_assistant_message=_PATH_TABLE)
        assert _call("stopBlockReason", str(sentinel), str(report), delivered, "x") is None

        report.write_text(_PATH_REPORT.replace("PASS", "NEEDS_WORK"), encoding="utf-8")

        assert _call("stopBlockReason", str(sentinel), str(report), _stop_payload(), "x") is not None

    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param({"last_assistant_message": _PATH_TABLE}, id="table-in-final-message-without-transcript"),
            pytest.param({"stop_hook_active": True}, id="forced-continuation"),
        ],
    )
    def test_does_not_block(self, stop_run: tuple[Path, Path], overrides: dict) -> None:
        """A turn that delivered the table, or that a Stop hook already forced on, is never blocked.

        Scenario: the Stop payload's final message counts as delivery even when the transcript lags; `stop_hook_active`
        means a Stop hook already forced this turn on, so never loop.
        """
        sentinel, report = stop_run

        assert _call("stopBlockReason", str(sentinel), str(report), _stop_payload(**overrides), "x") is None

    @pytest.mark.parametrize(
        "write_report",
        [
            pytest.param(Path.unlink, id="missing"),
            pytest.param(partial(Path.write_text, data="", encoding="utf-8"), id="empty"),
        ],
    )
    def test_ignores_unwritten_report(self, stop_run: tuple[Path, Path], write_report: object) -> None:
        """Missing or empty report means the producer has not finished — nothing to deliver yet.

        The staged report is deleted or truncated before Stop runs, and no block is produced for either.
        """
        sentinel, report = stop_run
        write_report(report)

        assert _call("stopBlockReason", str(sentinel), str(report), _stop_payload(), "x") is None

    def test_audit_reason_names_findings_not_header(self, tmp_path: Path, audit_single: Path) -> None:
        """Audit delivers a findings aggregate, so its Stop reason must not ask for a header table.

        The remedy asks for the Audit Report; mentioning a ``Field | Value`` table would send the model the wrong way.
        """
        sentinel = tmp_path / "run-dir"
        sentinel.write_text("x\n", encoding="utf-8")

        reason = _call("stopBlockReason", str(sentinel), str(audit_single), _stop_payload(), "foundry:audit")

        assert ("Audit Report" in reason, "Field | Value" in reason) == (True, False)

    def test_delivered_marker_keeps_session_token_terminal(self, tmp_path: Path) -> None:
        """The marker name ends with the sentinel's own CSID-suffixed name, beside it.

        Keeping the session token terminal in the marker name stops two concurrent sessions sharing one marker.
        """
        marker = Path(_call("deliveredMarkerPath", str(tmp_path / "oss-review-report-dir-abc")))

        assert (marker.parent, marker.name) == (tmp_path, "report-delivered-oss-review-report-dir-abc")

    def test_shown_summary_is_settled_by_a_final_table(self, follow_up_run: tuple[Path, Path]) -> None:
        """A shown summary leaves Stop armed; a final-reply table then passes and Stop never nudges again.

        Showing the question writes no marker; the final reply carrying the full table is accepted, and a later Stop for
        the same report is silent.
        """
        sentinel, report = follow_up_run
        call = _asked(_summary(report))
        assert _call("followUpProblem", str(sentinel), str(report), {"tool_input": call}) is None
        assert _call("recordFollowUpShown", str(sentinel), str(report), {"tool_input": call}) is False
        assert not Path(_call("deliveredMarkerPath", str(sentinel))).exists()

        reason = _call(
            "stopBlockReason",
            str(sentinel),
            str(report),
            _stop_payload(last_assistant_message=_HEADER_TABLE),
            "oss:review",
        )

        assert reason is None
        assert _call("stopBlockReason", str(sentinel), str(report), _stop_payload(), "oss:review") is None

    def test_shown_summary_without_final_table_blocks_once(self, follow_up_run: tuple[Path, Path]) -> None:
        """A shown summary does not excuse a final reply without the table; each report gets at most one nudge.

        The first Stop quotes the table header in its remedy; the second Stop for the same report is silent.
        """
        sentinel, report = follow_up_run
        call = _asked(_summary(report))
        assert _call("followUpProblem", str(sentinel), str(report), {"tool_input": call}) is None
        assert _call("recordFollowUpShown", str(sentinel), str(report), {"tool_input": call}) is False
        assert not Path(_call("deliveredMarkerPath", str(sentinel))).exists()

        reason = _call(
            "stopBlockReason", str(sentinel), str(report), _stop_payload(last_assistant_message="Done."), "oss:review"
        )

        assert "| Field | Value |" in reason
        assert _call("stopBlockReason", str(sentinel), str(report), _stop_payload(), "oss:review") is None


@_skip_node_unavailable
class TestStopJudgesTheFinalReply:
    """Stop checks only the turn's final reply: `last_assistant_message`, else the transcript text after the last tool.

    A table printed mid-turn and followed by a tool call may have come back as an empty progress update, and focus mode
    shows only the final message, so a turn ending "Done." after such a table has not delivered it.
    """

    @pytest.mark.parametrize(
        "final_message", [pytest.param("Done.", id="final-message"), pytest.param("", id="no-lam")]
    )
    def test_mid_turn_table_with_final_reply_without_it_blocks_once(
        self, tmp_path: Path, stop_run: tuple[Path, Path], final_message: str
    ) -> None:
        """Table mid-turn, final reply "Done." — blocked once, judged on the payload's message or the transcript.

        The payload's ``last_assistant_message`` is either "Done." or empty (falling back to the transcript); both judge
        the same final reply and block exactly once.
        """
        sentinel, report = stop_run
        payload = _stop_payload(
            transcript_path=str(_mid_turn_table_transcript(tmp_path, "Done.")), last_assistant_message=final_message
        )

        reason = _call("stopBlockReason", str(sentinel), str(report), payload, "oss:review")

        assert isinstance(reason, str)
        assert "final reply text found" in reason
        assert _call("stopBlockReason", str(sentinel), str(report), payload, "oss:review") is None

    def test_table_in_transcript_final_reply_passes_without_last_message(
        self, tmp_path: Path, stop_run: tuple[Path, Path]
    ) -> None:
        """Without `last_assistant_message`, the transcript's final reply holding the table is delivery.

        The final reply after the last tool call contains the table, so Stop does not block.
        """
        sentinel, report = stop_run
        payload = _stop_payload(transcript_path=str(_mid_turn_table_transcript(tmp_path, f"Done.\n\n{_PATH_TABLE}")))

        assert _call("stopBlockReason", str(sentinel), str(report), payload, "oss:review") is None


@_skip_node_unavailable
class TestQuestionSummaryContract:
    """Headline selection, saved-value fidelity, plain-text and length limits of the first question's summary."""

    @pytest.mark.parametrize(
        ("length", "allowed"), [pytest.param(1500, True, id="at-cap"), pytest.param(1501, False, id="over-cap")]
    )
    def test_length_cap(self, follow_up_run: tuple[Path, Path], length: int, allowed: bool) -> None:
        """Even a complete headline summary must leave headroom below the host's question-text cut.

        The question text is padded to exactly the cap (accepted) or one char over (denied, with the cap, the fixed
        lines and the report path named in the remedy).
        """
        _, report = follow_up_run
        summary = f"Title: Current review\nOutcome: PASS\n→ {report}\n"
        call = _question(_HEADER_TABLE)
        call["questions"][0]["question"] = summary + "x" * (length - len(summary))
        problem = _call("questionDeliveryProblem", str(report), call)

        assert (problem is None) is allowed

    def test_over_cap_remedy_names_cap_fixed_lines_and_path(self, follow_up_run: tuple[Path, Path]) -> None:
        """An over-cap summary is denied with a remedy naming the cap, the fixed lines and the report path.

        The model needs all three to shorten the question text without re-reading the hook.
        """
        _, report = follow_up_run
        summary = f"Title: Current review\nOutcome: PASS\n→ {report}\n"
        call = _question(_HEADER_TABLE)
        call["questions"][0]["question"] = summary + "x" * (1501 - len(summary))
        problem = _call("questionDeliveryProblem", str(report), call)

        assert isinstance(problem, str)
        assert all(fragment in problem for fragment in ("1500", "fixed lines", str(report)))

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
                "Title: Current review\nOutcome: PASS\nVerdict: REJECT",
                "Current review\nPASS",
                True,
                id="outcome-wins",
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
    def test_requires_only_present_headline_fields(
        self, tmp_path: Path, header: str, summary: str, allowed: bool
    ) -> None:
        """The question contract selects existing headline fields without manufacturing missing ones.

        Outcome wins over Verdict when both exist, Verdict stands in when Outcome is absent, and a headline field the
        report lacks is never demanded; a field the report has must be present.
        """
        report = tmp_path / "report.md"
        report.write_text(f"---\n{header}\n---\n", encoding="utf-8")
        problem = _call("questionDeliveryProblem", str(report), _asked(f"{summary}\n→ {report}"))
        assert (problem is None) is allowed

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
    def test_preserves_saved_values(self, tmp_path: Path, saved: str, shown: str, allowed: bool) -> None:
        """Summary matching shares table-cell Markdown tolerance while preserving saved literal characters.

        Styling around the Outcome (bold, italic, nested wrappers) is tolerated; characters the saved value holds
        (underscores, stars, backticks) can never be dropped.
        """
        report = tmp_path / "report.md"
        report.write_text(f"---\nTitle: Current review\nOutcome: {saved}\nDate: today\n---\n", encoding="utf-8")
        problem = _call("questionDeliveryProblem", str(report), _asked(_summary(report, outcome=shown)))
        assert (problem is None) is allowed

    @pytest.mark.parametrize(
        "build_text",
        [
            pytest.param(lambda report, summary: _HEADER_TABLE + f"\n→ {report}", id="table"),
            pytest.param(lambda report, summary: f"<pre>{summary}</pre>", id="html"),
        ],
    )
    def test_requires_plain_text(self, header_report: Path, build_text: object) -> None:
        """Putting all facts in a question's table or HTML wrapper is denied with a plain-text remedy.

        The same facts as plain lines then pass.
        """
        summary = _summary(header_report)
        problem = _call("questionDeliveryProblem", str(header_report), _asked(build_text(header_report, summary)))
        assert "use plain text" in problem
        assert _call("questionDeliveryProblem", str(header_report), _asked(summary)) is None

    def test_report_in_session_cwd_can_be_named_by_filename(self, tmp_path: Path, header_report: Path) -> None:
        """A report in the session cwd can be named by its filename, without an artificial path prefix.

        The bare name resolves against the session cwd; the same name in a cwd that holds a different ``report.md`` is
        not accepted for this report.
        """
        call = _asked("Title: Current review\nOutcome: PASS\n→ report.md")
        other_cwd = tmp_path / "other"
        other_cwd.mkdir()
        (other_cwd / "report.md").write_text(_HEADER_REPORT, encoding="utf-8")
        assert (
            _call("questionDeliveryProblem", str(header_report), call, str(tmp_path)),
            _call("questionDeliveryProblem", str(header_report), call, str(other_cwd)) is not None,
        ) == (None, True)

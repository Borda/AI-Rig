"""Keep path-bearing evidence outside terminal tables without losing facts."""

import copy
import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from _final_handoff_support import handoff_payload, load_finalizer
from test_challenge_history import _history, _render
from test_finding_presentation import _selection

_FAILED_ACTION_CELLS = ("Failed: corrupt input", "Needs action")


def _row_payload(finalizer: object, cells: list[str], evidence: str, skill: str = "implement") -> dict[str, object]:
    """Build a current-presentation handoff with one result row backed by one evidence record."""
    payload = handoff_payload()
    payload.update(skill=skill, presentation_version=8, remaining=[], next_steps=[])
    payload["tables"] = [
        {
            "heading": "Results",
            "columns": list(finalizer.STANDARD_COLUMNS[skill]),
            "rows": [{"id": "A", "cells": cells, "source_ids": ["source"]}],
        }
    ]
    payload["source_records"] = [{"id": "source", "evidence": evidence}]
    payload["source_coverage"] = {
        "source_records_total": 1,
        "represented_source_records_total": 1,
        "omitted_source_records_total": 0,
    }
    return payload


def _table(rendered: str) -> str:
    """Return only the Markdown table lines of a rendered terminal view."""
    return "\n".join(line for line in rendered.splitlines() if line.startswith("|"))


def _bullets(rendered: str) -> list[str]:
    """Return only the externalized `- ` list lines of a rendered terminal view."""
    return [line for line in rendered.splitlines() if line.startswith("- ")]


def _externalize(columns: str, value: str, header: str = "| Item | {} |\n| --- | --- |") -> str:
    """Externalize the paths of a one-row `Item | <column>` table whose cell holds `value`."""
    table = header.format(columns) + f"\n| Parser | {value} |\n"
    return load_finalizer()._externalize_table_paths(table)


@dataclass
class _Published:
    """Outcome of writing, rendering and checking a handoff through the public file writer."""

    rendered: str
    statuses: tuple[str, str]
    unchanged: bool


def _publish(tmp_path: Path, payload: dict[str, object]) -> _Published:
    """Render and check a handoff file with the public writer, noting whether the input bytes survived."""
    finalizer = load_finalizer()
    handoff = tmp_path / "final-handoff.json"
    final = tmp_path / "final.md"
    validation = tmp_path / "final-handoff.validation.json"
    handoff.write_text(json.dumps(payload), encoding="utf-8")
    original = handoff.read_bytes()
    statuses = (
        finalizer.render_files(handoff, final, validation)["status"],
        finalizer.check_files(handoff, final, validation)["status"],
    )
    return _Published(final.read_text(encoding="utf-8"), statuses, handoff.read_bytes() == original)


class TestHandoffRendering:
    """Rendered handoff and selection views keep paths out of tables while retaining every fact."""

    @pytest.mark.parametrize(
        "path",
        [
            "/work/project/.reports/audit/run/result.json",
            r"C:\Work\Project\.reports\audit\run\result.json",
            "reports/review with spaces/result.json",
        ],
    )
    def test_audit_moves_evidence_links_below_table(self, path: str) -> None:
        """The exact full destination moves below the table while the table cells stay readable.

        An audit row whose Evidence cell links a POSIX, Windows or space-containing path must lose the Evidence column
        from the table, keep the path verbatim in the item list, and leave the input payload unmodified.
        """
        finalizer = load_finalizer()
        cells = ["Parser", "Medium", "Fix", f"[Evidence](<{path}>)", "Add regression"]
        payload = _row_payload(finalizer, cells, path, skill="audit")
        before = copy.deepcopy(payload)

        rendered = finalizer.render_handoff(payload)

        table = _table(rendered)
        assert "| Evidence |" not in table
        assert path not in table
        assert f"- Parser: {path}" in rendered
        assert "Add regression" in table
        assert payload == before

    def test_surface_and_mixed_action_keep_text_and_externalize_paths(self) -> None:
        """Paths in non-evidence columns cannot bypass compact terminal rendering.

        An implement row names a source file in Surface and links a trace in the Outcome sentence. Both paths move below
        the table, and the sentence stays readable with the link label in place of its destination.
        """
        finalizer = load_finalizer()
        outcome = "Fixed; inspect [trace](<reports/run with spaces/trace.json>) before retry"
        payload = _row_payload(
            finalizer, ["src/package/core.py", outcome, "Passed", "None"], "reports/run with spaces/trace.json"
        )

        rendered = finalizer.render_handoff(payload)

        table = _table(rendered)
        assert "src/package/" not in table
        assert "reports/" not in table
        assert "Fixed; inspect trace before retry" in table
        assert "- core.py: src/package/core.py" in rendered
        assert "- core.py: reports/run with spaces/trace.json" in rendered

    def test_selection_externalizes_proposal_path(self) -> None:
        """The intermediate selection view uses the same item-to-path presentation.

        Every selectable item proposes a resolution that links the parser source; the rendered selection keeps the
        sentence in the table and lists the path below it.
        """
        payload = _selection()
        payload["presentation_version"] = 5
        proposal = "Correct [parser](<src/package/parser.py>) and retain compatibility."
        payload["items"] = [{**item, "resolution_proposal": proposal} for item in payload["items"]]

        rendered = load_finalizer().render_selection(payload)

        assert "src/package/parser.py" not in _table(rendered)
        assert "Correct parser and retain compatibility." in _table(rendered)
        assert ": src/package/parser.py" in rendered

    def test_historical_rendering_and_exact_caller_output_remain_unchanged(self) -> None:
        """New terminal views do not rewrite historical evidence or exact caller bytes.

        A pre-externalization handoff keeps its path inside the table, and a literal caller contract is returned byte
        for byte even when its table holds a path.
        """
        finalizer = load_finalizer()
        payload = handoff_payload()
        payload["tables"][0]["rows"][0]["cells"][-1] = "reports/old/result.json"
        historical = finalizer.render_handoff(payload)
        payload.update(
            branch="caller-contract", presentation_version=8, tables=[], source_records=[], remaining=[], next_steps=[]
        )
        payload["source_coverage"] = {
            "source_records_total": 0,
            "represented_source_records_total": 0,
            "omitted_source_records_total": 0,
        }
        payload["caller_contract"] = {
            "format": "literal",
            "evidence": "Explicit caller bytes.",
            "output": "| exact | reports/old/result.json |\n",
        }

        caller = finalizer.render_handoff(payload)

        assert "| reports/old/result.json |" in historical
        assert caller == payload["caller_contract"]["output"]

    def test_challenge_history_uses_item_path_list(self, tmp_path: Path) -> None:
        """Admission and counts are retained while challenge evidence paths move outside the table.

        A challenge history with advisory and checkpoint rounds is rendered at the current presentation; no table line
        may mention a Markdown file, while both rounds appear in the item list.
        """
        payload = _history(tmp_path)
        payload["presentation_version"] = 8

        rendered = _render(tmp_path, payload)

        assert "- Advisory 1: " in rendered
        assert "- Checkpoint 2: " in rendered
        assert "first%20review.md" in rendered
        assert "No validated rounds" in rendered
        assert ".md" not in _table(rendered)

    def test_public_handoff_keeps_path_leading_surface_context(self) -> None:
        """A complete user instruction survives validation and terminal rendering.

        A Surface cell that starts with a path but continues with an instruction is moved below the table in full under
        a neutral item name, rather than being cut at the path.
        """
        payload = handoff_payload()
        payload.update(skill="implement", presentation_version=8)
        payload["tables"][0]["columns"] = ["Surface", "Outcome", "Verification", "Remaining limit"]
        payload["tables"][0]["rows"][0]["cells"] = ["src/core.py then inspect src/other.py", "Fixed", "Passed", "None"]
        payload["tables"][0]["rows"][1]["cells"] = ["External CI", "Pending", "Unavailable", "External runner"]
        before = copy.deepcopy(payload)

        rendered = load_finalizer().render_handoff(payload)

        assert "| Item 1 | Fixed | Passed | None |" in rendered
        assert "- Item 1 (Surface): src/core.py then inspect src/other.py" in rendered
        assert payload == before
        assert "src/" not in _table(rendered)

    @pytest.mark.parametrize("surface", ["Report", "Parser"])
    def test_ordinary_report_named_row_keeps_actions_and_failures(self, surface: str) -> None:
        """A normal work row cannot be mistaken for the generated review-report pointer.

        A row literally named `Report` carries a rebuild action and a failure; both must stay in the table exactly like
        the same row named `Parser`, with only the link destination moved below.
        """
        finalizer = load_finalizer()
        cells = [surface, "Rebuild [report](<reports/result.json>) before retry", *_FAILED_ACTION_CELLS]
        payload = _row_payload(finalizer, cells, "reports/result.json")

        rendered = finalizer.render_handoff(payload)

        assert f"| {surface} | Rebuild report before retry | Failed: corrupt input | Needs action |" in rendered
        assert f"- {surface}: reports/result.json" in rendered

    @pytest.mark.parametrize(
        "destination", ["reports/a(b).json", r"reports/a\(b\).json", "https://example.org/a(b).json", "a(b).json"]
    )
    def test_complex_inline_destination_keeps_complete_context(self, destination: str) -> None:
        """Unsupported inline target boundaries stay literal rather than becoming invented partial paths.

        Destinations containing parentheses cannot be split reliably, so the whole action sentence is kept below the
        table and no truncated path such as `reports/a(b` may be invented.
        """
        finalizer = load_finalizer()
        action = f"Read [proof]({destination}) before retry"
        payload = _row_payload(finalizer, ["Parser", action, *_FAILED_ACTION_CELLS], destination)

        rendered = finalizer.render_handoff(payload)

        table = _table(rendered)
        assert f"- Parser (Outcome): {action}" in rendered
        assert destination not in table
        assert "Failed: corrupt input" in table
        assert "Needs action" in table
        assert "- Parser: reports/a(b" not in rendered


class TestExternalizeTablePaths:
    """Table cells are split into compact text and verbatim detail lines without guessing path grammar."""

    def test_preserves_slash_prose_and_extracts_bare_evidence(self) -> None:
        """A path view must not reinterpret status prose as a file or omit bare report names.

        `pass/fail` and `owner/status` inside an Action sentence stay prose, while a bare `result.json` in the Evidence
        column is listed as a path and its column leaves the table.
        """
        table = "| Item | Evidence | Action |\n| --- | --- | --- |\n"
        table += "| Parser | result.json | Keep pass/fail and owner/status facts |\n"

        rendered = load_finalizer()._externalize_table_paths(table)

        assert "- Parser (Action): Keep pass/fail and owner/status facts" in rendered
        assert "- Parser: pass/fail" not in rendered
        assert "- Parser: owner/status" not in rendered
        assert "- Parser: result.json" in rendered
        assert "| Evidence |" not in rendered

    @pytest.mark.parametrize(
        "destination",
        [
            pytest.param("/work/my project/reports/result.json", id="absolute-spaces"),
            pytest.param("reports/my run/result.json", id="relative-spaces"),
        ],
    )
    def test_bare_evidence_paths_preserve_spaces(self, destination: str) -> None:
        """A complete evidence cell is one path even when its directories contain spaces."""
        rendered = _externalize("Evidence", destination)

        assert f"- Parser (Evidence): {destination}" in rendered
        assert destination not in rendered.splitlines()[2]

    @pytest.mark.parametrize(
        ("value", "compact", "resources"),
        [
            pytest.param(
                "Read src/core.py, then retry",
                "See detail",
                ["- Parser (Surface): Read src/core.py, then retry"],
                id="punctuated-context",
            ),
            pytest.param(
                "Fix core.py then retry",
                "See detail",
                ["- Parser (Surface): Fix core.py then retry"],
                id="bare-file-context",
            ),
            pytest.param(
                "[src/core.py](<reports/actual.txt>)",
                "See detail",
                ["- Parser (Surface): [src/core.py](<reports/actual.txt>)"],
                id="path-label-not-resource",
            ),
            pytest.param(
                "Read `src/core.py:42`; `src/other.py`.",
                "Read core.py:42; other.py.",
                ["- Parser: src/core.py:42", "- Parser: src/other.py"],
                id="explicit-coordinate-paths",
            ),
        ],
    )
    def test_extraction_retains_facts(self, value: str, compact: str, resources: list[str]) -> None:
        """Explicit references are extracted; ambiguous plain context stays verbatim outside the table."""
        rendered = _externalize("Surface", value)

        assert f"| Parser | {compact} |" in rendered
        assert _bullets(rendered) == resources

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("src/core.py then inspect src/other.py", id="path-leading-instruction"),
            pytest.param("src/core.py and src/other.py", id="path-leading-conjunction"),
            pytest.param("src/core.py,src/other.py", id="comma-boundary-ambiguity"),
            pytest.param("src/core.py;src/other.py", id="semicolon-boundary-ambiguity"),
            pytest.param("my reports/run/result.json", id="leading-directory-spaces"),
            pytest.param("Read src/core.py", id="instruction-or-directory-ambiguity"),
        ],
    )
    @pytest.mark.parametrize("column", ["Surface", "Evidence"])
    def test_ambiguous_plain_cells_move_verbatim_without_invented_paths(self, value: str, column: str) -> None:
        """Every original byte moves below the table instead of guessing prose versus path spaces.

        Instructions that mix paths with words, joined paths and directories with spaces cannot be classified, so the
        cell is kept whole in either column and the table never shows a `src/` or `reports/` fragment.
        """
        rendered = _externalize(column, value)

        assert "| Parser | See detail |" in rendered
        assert _bullets(rendered) == [f"- Parser ({column}): {value}"]
        assert "src/" not in _table(rendered)
        assert "reports/" not in _table(rendered)

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("src/.gitignore", id="relative-dotfile"),
            pytest.param("bin/launcher", id="extensionless-path"),
            pytest.param("pass/fail", id="slash-prose"),
            pytest.param("owner/status", id="ownership-prose"),
        ],
    )
    def test_unmarked_slash_cells_remain_literal_context(self, value: str) -> None:
        """Do not guess whether an unmarked slash expression is a path or prose."""
        rendered = _externalize("Surface", value)

        assert "| Parser | See detail |" in rendered
        assert _bullets(rendered) == [f"- Parser (Surface): {value}"]

    def test_unclassified_code_span_remains_complete_context(self) -> None:
        """The safe quote fallback preserves ordinary commands without claiming they name files."""
        action = "Run `python --version` before retry"

        rendered = _externalize("Action", action)

        assert "| Parser | See detail |" in rendered
        assert _bullets(rendered) == [f"- Parser (Action): {action}"]


@pytest.mark.integration
class TestPublicWriter:
    """The public file writer keeps real destinations and full actions outside tables."""

    @pytest.mark.parametrize(
        ("filename", "quote"),
        [
            pytest.param("release notes.md", "`", id="spaced-basename"),
            pytest.param("[report].md", "`", id="bracketed-basename"),
            pytest.param("foo.longextensionhere", "`", id="long-extension"),
            pytest.param("evidence.資料", "`", id="unicode-extension"),
            pytest.param("LICENSE", "`", id="extensionless-basename"),
            pytest.param(".env", "`", id="dotfile"),
            pytest.param("report (final).md", "`", id="parenthesized-basename"),
            pytest.param("report`draft.md", "``", id="embedded-backtick"),
            pytest.param("foo.longextensionhere", "", id="plain-long-extension"),
            pytest.param("evidence.資料", "", id="plain-unicode-extension"),
            pytest.param("[report].md", "", id="plain-bracketed-basename"),
            pytest.param("report.json-final", "", id="plain-punctuated-extension"),
            pytest.param(".env", "", id="plain-dotfile"),
        ],
    )
    def test_keeps_unclassified_references_outside_tables(self, tmp_path: Path, filename: str, quote: str) -> None:
        """Real destinations and their full action stay outside tables without guessing filename grammar.

        Each file exists on disk and is cited either in a code span or as the bare cell text. Unusual names such as
        spaces, brackets, dotfiles or non-ASCII extensions must not be cut into partial paths or left in the table.
        """
        (tmp_path / filename).write_text("Evidence retained.\n", encoding="utf-8")
        action = f"Inspect {quote}{filename}{quote} before retry" if quote else filename
        payload = _row_payload(load_finalizer(), ["Parser", action, *_FAILED_ACTION_CELLS], filename)

        published = _publish(tmp_path, payload)

        assert published.statuses == ("pass", "pass")
        assert filename not in _table(published.rendered)
        assert "| Parser | See detail | Failed: corrupt input | Needs action |" in _table(published.rendered)
        assert f"- Parser (Outcome): {action}" in published.rendered
        assert "- Parser: foo.longextensio\n" not in published.rendered
        assert "- Parser: report.json\n" not in published.rendered
        assert published.unchanged

    @pytest.mark.parametrize(
        ("label", "destination"),
        [
            pytest.param("docs/references/README", "docs/references/README", id="extensionless-label"),
            pytest.param("docs/references/guide.md", "docs/references/guide.md", id="filename-label"),
            pytest.param("docs/references/README", "reports/current.json", id="different-label-and-target"),
            pytest.param("`release notes.md`", "reports/current.json", id="quoted-label"),
        ],
    )
    def test_preserves_path_labels_and_actions_outside_tables(
        self, tmp_path: Path, label: str, destination: str
    ) -> None:
        """Distinct visible labels, actual destinations and row facts survive the public writer.

        A link whose label itself looks like a path is not trusted to name the destination: the full action sentence
        moves below the table verbatim, and neither the label nor the target stays in the table.
        """
        (tmp_path / destination).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / destination).write_text("Evidence retained.\n", encoding="utf-8")
        action = f"Inspect [{label}](<{destination}>) before retry"
        payload = _row_payload(load_finalizer(), ["Parser", action, *_FAILED_ACTION_CELLS], destination)

        published = _publish(tmp_path, payload)

        table = _table(published.rendered)
        assert published.statuses == ("pass", "pass")
        assert destination not in table
        assert label not in table
        assert "Failed: corrupt input" in table
        assert "Needs action" in table
        assert f"- Parser (Outcome): {action}" in published.rendered
        assert published.unchanged

    def test_human_label_link_extracts_destination(self, tmp_path: Path) -> None:
        """A link with a plain human label shows that label in the table and lists the destination below.

        Unlike path-looking labels, `Reference guide` cannot be mistaken for a file, so the destination is extracted as
        its own item while the readable sentence stays in the row.
        """
        destination = "docs/references/README"
        (tmp_path / destination).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / destination).write_text("Evidence retained.\n", encoding="utf-8")
        action = f"Inspect [Reference guide](<{destination}>) before retry"
        payload = _row_payload(load_finalizer(), ["Parser", action, *_FAILED_ACTION_CELLS], destination)

        published = _publish(tmp_path, payload)

        table = _table(published.rendered)
        assert published.statuses == ("pass", "pass")
        assert destination not in table
        assert "Failed: corrupt input" in table
        assert "Needs action" in table
        assert "Inspect Reference guide before retry" in table
        assert f"- Parser: {destination}" in published.rendered
        assert published.unchanged

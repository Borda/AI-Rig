"""Keep path-bearing evidence outside terminal tables without losing facts."""

import copy
import json
from pathlib import Path

import pytest
from test_final_handoff import _handoff_payload, _load_finalizer
from test_finding_presentation import _selection


@pytest.mark.parametrize(
    "path",
    [
        "/work/project/.reports/audit/run/result.json",
        r"C:\Work\Project\.reports\audit\run\result.json",
        "reports/review with spaces/result.json",
    ],
)
def test_current_audit_moves_evidence_links_below_table(path: str) -> None:
    """Retain the exact full destination while keeping table cells readable."""
    finalizer = _load_finalizer()
    payload = _handoff_payload()
    payload.update(skill="audit", presentation_version=8, remaining=[], next_steps=[])
    payload["tables"] = [
        {
            "heading": "Results",
            "columns": list(finalizer.STANDARD_COLUMNS["audit"]),
            "rows": [
                {
                    "id": "A",
                    "cells": ["Parser", "Medium", "Fix", f"[Evidence](<{path}>)", "Add regression"],
                    "source_ids": ["source"],
                }
            ],
        }
    ]
    payload["source_records"] = [{"id": "source", "evidence": path}]
    payload["source_coverage"] = {
        "source_records_total": 1,
        "represented_source_records_total": 1,
        "omitted_source_records_total": 0,
    }
    before = copy.deepcopy(payload)

    rendered = finalizer.render_handoff(payload)
    table = "\n".join(line for line in rendered.splitlines() if line.startswith("|"))
    assert "| Evidence |" not in table
    assert path not in table
    assert f"- Parser: {path}" in rendered
    assert "Add regression" in table
    assert payload == before


def test_current_surface_and_mixed_action_keep_text_and_externalize_paths() -> None:
    """Paths in non-evidence columns cannot bypass compact terminal rendering."""
    finalizer = _load_finalizer()
    payload = _handoff_payload()
    payload.update(skill="implement", presentation_version=8, remaining=[], next_steps=[])
    payload["tables"] = [
        {
            "heading": "Results",
            "columns": list(finalizer.STANDARD_COLUMNS["implement"]),
            "rows": [
                {
                    "id": "A",
                    "cells": [
                        "src/package/core.py",
                        "Fixed; inspect [trace](<reports/run with spaces/trace.json>) before retry",
                        "Passed",
                        "None",
                    ],
                    "source_ids": ["source"],
                }
            ],
        }
    ]
    payload["source_records"] = [{"id": "source", "evidence": "reports/run with spaces/trace.json"}]
    payload["source_coverage"] = {
        "source_records_total": 1,
        "represented_source_records_total": 1,
        "omitted_source_records_total": 0,
    }
    rendered = finalizer.render_handoff(payload)
    table = "\n".join(line for line in rendered.splitlines() if line.startswith("|"))
    assert "src/package/" not in table
    assert "reports/" not in table
    assert "Fixed; inspect trace before retry" in table
    assert "- core.py: src/package/core.py" in rendered
    assert "- core.py: reports/run with spaces/trace.json" in rendered


def test_current_selection_externalizes_proposal_path() -> None:
    """The intermediate selection view uses the same item-to-path presentation."""
    payload = _selection()
    payload["presentation_version"] = 5
    for item in payload["items"]:
        item["resolution_proposal"] = "Correct [parser](<src/package/parser.py>) and retain compatibility."
    rendered = _load_finalizer().render_selection(payload)
    table = "\n".join(line for line in rendered.splitlines() if line.startswith("|"))
    assert "src/package/parser.py" not in table
    assert "Correct parser and retain compatibility." in table
    assert ": src/package/parser.py" in rendered


def test_historical_rendering_and_exact_caller_output_remain_unchanged() -> None:
    """New terminal views do not rewrite historical evidence or exact caller bytes."""
    finalizer = _load_finalizer()
    payload = _handoff_payload()
    payload["tables"][0]["rows"][0]["cells"][-1] = "reports/old/result.json"
    rendered = finalizer.render_handoff(payload)
    assert "| reports/old/result.json |" in rendered
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
    assert finalizer.render_handoff(payload) == payload["caller_contract"]["output"]


def test_current_table_preserves_slash_prose_and_extracts_bare_evidence() -> None:
    """A path view must not reinterpret status prose as a file or omit bare report names."""
    rendered = _load_finalizer()._externalize_table_paths(
        "| Item | Evidence | Action |\n| --- | --- | --- |\n| Parser | result.json | Keep pass/fail and owner/status facts |\n"
    )
    assert "- Parser (Action): Keep pass/fail and owner/status facts" in rendered
    assert "- Parser: pass/fail" not in rendered
    assert "- Parser: owner/status" not in rendered
    assert "- Parser: result.json" in rendered
    assert "| Evidence |" not in rendered


def test_current_challenge_history_uses_item_path_list(tmp_path) -> None:
    """Retain admission and counts while moving challenge evidence outside the table."""
    from test_challenge_history import _history, _render

    payload = _history(tmp_path)
    payload["presentation_version"] = 8
    rendered = _render(tmp_path, payload)
    assert "- Advisory 1: " in rendered
    assert "- Checkpoint 2: " in rendered
    assert "first%20review.md" in rendered
    assert "No validated rounds" in rendered
    assert all(".md" not in line for line in rendered.splitlines() if line.startswith("|"))


@pytest.mark.parametrize(
    "destination",
    [
        pytest.param("/work/my project/reports/result.json", id="absolute-spaces"),
        pytest.param("reports/my run/result.json", id="relative-spaces"),
    ],
)
def test_bare_evidence_paths_preserve_spaces(destination: str) -> None:
    """A complete evidence cell is one path even when its directories contain spaces."""
    rendered = _load_finalizer()._externalize_table_paths(
        f"| Item | Evidence |\n| --- | --- |\n| Parser | {destination} |\n"
    )
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
        pytest.param("pass/fail", "See detail", ["- Parser (Surface): pass/fail"], id="slash-prose"),
        pytest.param(
            "Read src/core.py", "See detail", ["- Parser (Surface): Read src/core.py"], id="unpunctuated-context"
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
def test_current_table_extraction_retains_facts(value: str, compact: str, resources: list[str]) -> None:
    """Extract explicit references; retain ambiguous plain context verbatim outside the table."""
    rendered = _load_finalizer()._externalize_table_paths(f"| Item | Surface |\n| --- | --- |\n| Parser | {value} |\n")
    assert f"| Parser | {compact} |" in rendered
    assert [line for line in rendered.splitlines() if line.startswith("- ")] == resources


def test_mixed_evidence_column_retains_action_words() -> None:
    """Do not remove an Evidence column that contains a required instruction with its path."""
    rendered = _load_finalizer()._externalize_table_paths(
        "| Item | Evidence |\n| --- | --- |\n| Parser | Read src/core.py |\n"
    )
    assert "| Parser | See detail |" in rendered
    assert "- Parser (Evidence): Read src/core.py" in rendered


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
def test_ambiguous_plain_cells_move_verbatim_without_invented_paths(value: str, column: str) -> None:
    """Keep every original byte outside the table instead of guessing prose versus path spaces."""
    rendered = _load_finalizer()._externalize_table_paths(f"| Item | {column} |\n| --- | --- |\n| Parser | {value} |\n")
    assert "| Parser | See detail |" in rendered
    assert [line for line in rendered.splitlines() if line.startswith("- ")] == [f"- Parser ({column}): {value}"]
    assert all("src/" not in line and "reports/" not in line for line in rendered.splitlines() if line.startswith("|"))


def test_current_public_handoff_keeps_path_leading_surface_context() -> None:
    """Preserve a complete user instruction through validation and terminal rendering."""
    payload = _handoff_payload()
    payload.update(skill="implement", presentation_version=8)
    payload["tables"][0]["columns"] = ["Surface", "Outcome", "Verification", "Remaining limit"]
    payload["tables"][0]["rows"][0]["cells"] = ["src/core.py then inspect src/other.py", "Fixed", "Passed", "None"]
    payload["tables"][0]["rows"][1]["cells"] = ["External CI", "Pending", "Unavailable", "External runner"]
    before = copy.deepcopy(payload)
    rendered = _load_finalizer().render_handoff(payload)
    assert "| Item 1 | Fixed | Passed | None |" in rendered
    assert "- Item 1 (Surface): src/core.py then inspect src/other.py" in rendered
    assert payload == before
    assert all("src/" not in line for line in rendered.splitlines() if line.startswith("|"))


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("src/.gitignore", id="relative-dotfile"),
        pytest.param("bin/launcher", id="extensionless-path"),
        pytest.param("pass/fail", id="slash-prose"),
        pytest.param("owner/status", id="ownership-prose"),
    ],
)
def test_unmarked_slash_cells_remain_literal_context(value: str) -> None:
    """Do not guess whether an unmarked slash expression is a path or prose."""
    rendered = _load_finalizer()._externalize_table_paths(f"| Item | Surface |\n| --- | --- |\n| Parser | {value} |\n")
    assert "| Parser | See detail |" in rendered
    assert [line for line in rendered.splitlines() if line.startswith("- ")] == [f"- Parser (Surface): {value}"]


@pytest.mark.parametrize("surface", ["Report", "Parser"])
def test_ordinary_report_named_row_preserves_actions_and_failures(surface: str) -> None:
    """A normal work row cannot be mistaken for the generated review-report pointer."""
    finalizer = _load_finalizer()
    payload = _handoff_payload()
    payload.update(skill="implement", presentation_version=8, remaining=[], next_steps=[])
    payload["tables"] = [
        {
            "heading": "Results",
            "columns": list(finalizer.STANDARD_COLUMNS["implement"]),
            "rows": [
                {
                    "id": "A",
                    "cells": [
                        surface,
                        "Rebuild [report](<reports/result.json>) before retry",
                        "Failed: corrupt input",
                        "Needs action",
                    ],
                    "source_ids": ["source"],
                }
            ],
        }
    ]
    payload["source_records"] = [{"id": "source", "evidence": "reports/result.json"}]
    payload["source_coverage"] = {
        "source_records_total": 1,
        "represented_source_records_total": 1,
        "omitted_source_records_total": 0,
    }
    rendered = finalizer.render_handoff(payload)
    assert f"| {surface} | Rebuild report before retry | Failed: corrupt input | Needs action |" in rendered
    assert f"- {surface}: reports/result.json" in rendered


@pytest.mark.parametrize(
    "destination", ["reports/a(b).json", r"reports/a\(b\).json", "https://example.org/a(b).json", "a(b).json"]
)
def test_complex_inline_destination_keeps_complete_context(destination: str) -> None:
    """Unsupported inline target boundaries stay literal rather than becoming invented partial paths."""
    finalizer = _load_finalizer()
    payload = _handoff_payload()
    payload.update(skill="implement", presentation_version=8, remaining=[], next_steps=[])
    action = f"Read [proof]({destination}) before retry"
    payload["tables"] = [
        {
            "heading": "Results",
            "columns": list(finalizer.STANDARD_COLUMNS["implement"]),
            "rows": [
                {
                    "id": "A",
                    "cells": ["Parser", action, "Failed: corrupt input", "Needs action"],
                    "source_ids": ["source"],
                }
            ],
        }
    ]
    payload["source_records"] = [{"id": "source", "evidence": destination}]
    payload["source_coverage"] = {
        "source_records_total": 1,
        "represented_source_records_total": 1,
        "omitted_source_records_total": 0,
    }
    rendered = finalizer.render_handoff(payload)
    assert f"- Parser (Outcome): {action}" in rendered
    table = "\n".join(line for line in rendered.splitlines() if line.startswith("|"))
    assert destination not in table
    assert "Failed: corrupt input" in table
    assert "Needs action" in table
    assert "- Parser: reports/a(b" not in rendered


@pytest.mark.integration
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
def test_public_file_writer_keeps_unclassified_references_outside_tables(
    tmp_path: Path, filename: str, quote: str
) -> None:
    """Keep real destinations and their full action outside tables without guessing filename grammar."""
    (tmp_path / filename).write_text("Evidence retained.\n", encoding="utf-8")
    finalizer = _load_finalizer()
    payload = _handoff_payload()
    payload.update(skill="implement", presentation_version=8, remaining=[], next_steps=[])
    action = f"Inspect {quote}{filename}{quote} before retry" if quote else filename
    payload["tables"] = [
        {
            "heading": "Results",
            "columns": list(finalizer.STANDARD_COLUMNS["implement"]),
            "rows": [
                {
                    "id": "A",
                    "cells": ["Parser", action, "Failed: corrupt input", "Needs action"],
                    "source_ids": ["source"],
                }
            ],
        }
    ]
    payload["source_records"] = [{"id": "source", "evidence": filename}]
    payload["source_coverage"] = {
        "source_records_total": 1,
        "represented_source_records_total": 1,
        "omitted_source_records_total": 0,
    }
    handoff = tmp_path / "final-handoff.json"
    final = tmp_path / "final.md"
    validation = tmp_path / "final-handoff.validation.json"
    handoff.write_text(json.dumps(payload), encoding="utf-8")
    original = handoff.read_bytes()
    assert finalizer.render_files(handoff, final, validation)["status"] == "pass"
    assert finalizer.check_files(handoff, final, validation)["status"] == "pass"
    rendered = final.read_text(encoding="utf-8")
    table = "\n".join(line for line in rendered.splitlines() if line.startswith("|"))
    assert filename not in table
    assert "| Parser | See detail | Failed: corrupt input | Needs action |" in table
    assert f"- Parser (Outcome): {action}" in rendered
    assert "- Parser: foo.longextensio\n" not in rendered
    assert "- Parser: report.json\n" not in rendered
    assert handoff.read_bytes() == original


def test_unclassified_code_span_remains_complete_context_not_invented_path() -> None:
    """The safe quote fallback preserves ordinary commands without claiming they name files."""
    action = "Run `python --version` before retry"
    rendered = _load_finalizer()._externalize_table_paths(f"| Item | Action |\n| --- | --- |\n| Parser | {action} |\n")
    assert "| Parser | See detail |" in rendered
    assert [line for line in rendered.splitlines() if line.startswith("- ")] == [f"- Parser (Action): {action}"]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("label", "destination", "detail"),
    [
        pytest.param("docs/references/README", "docs/references/README", True, id="extensionless-label"),
        pytest.param("docs/references/guide.md", "docs/references/guide.md", True, id="filename-label"),
        pytest.param("docs/references/README", "reports/current.json", True, id="different-label-and-target"),
        pytest.param("`release notes.md`", "reports/current.json", True, id="quoted-label"),
        pytest.param("Reference guide", "docs/references/README", False, id="human-label"),
    ],
)
def test_public_writer_preserves_path_labels_and_actions_outside_tables(
    tmp_path: Path, label: str, destination: str, detail: bool
) -> None:
    """Preserve distinct visible labels, actual destinations and row facts through the public writer."""
    target = tmp_path / destination
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("Evidence retained.\n", encoding="utf-8")
    finalizer = _load_finalizer()
    payload = _handoff_payload()
    payload.update(skill="implement", presentation_version=8, remaining=[], next_steps=[])
    action = f"Inspect [{label}](<{destination}>) before retry"
    payload["tables"] = [
        {
            "heading": "Results",
            "columns": list(finalizer.STANDARD_COLUMNS["implement"]),
            "rows": [
                {
                    "id": "A",
                    "cells": ["Parser", action, "Failed: corrupt input", "Needs action"],
                    "source_ids": ["source"],
                }
            ],
        }
    ]
    payload["source_records"] = [{"id": "source", "evidence": destination}]
    payload["source_coverage"] = {
        "source_records_total": 1,
        "represented_source_records_total": 1,
        "omitted_source_records_total": 0,
    }
    handoff = tmp_path / "final-handoff.json"
    final = tmp_path / "final.md"
    validation = tmp_path / "final-handoff.validation.json"
    handoff.write_text(json.dumps(payload), encoding="utf-8")
    original = handoff.read_bytes()
    assert finalizer.render_files(handoff, final, validation)["status"] == "pass"
    assert finalizer.check_files(handoff, final, validation)["status"] == "pass"
    rendered = final.read_text(encoding="utf-8")
    table = "\n".join(line for line in rendered.splitlines() if line.startswith("|"))
    assert destination not in table
    assert "Failed: corrupt input" in table
    assert "Needs action" in table
    if detail:
        assert label not in table
        assert f"- Parser (Outcome): {action}" in rendered
    else:
        assert "Inspect Reference guide before retry" in table
        assert f"- Parser: {destination}" in rendered
    assert handoff.read_bytes() == original

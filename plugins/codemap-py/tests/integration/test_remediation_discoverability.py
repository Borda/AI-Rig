"""Black-box regression contracts for the compact Codemap discovery surface."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from codemap_py import cli, query


_PLUGIN_ROOT = Path(__file__).resolve().parents[2]


def test_top_level_help_succeeds_and_teaches_the_compact_query_form(capsys: pytest.CaptureFixture[str]) -> None:
    """Prevent first-use discovery from failing before users learn the public query form."""
    assert cli.main(["--help"]) == 0

    captured = capsys.readouterr()
    help_text = captured.out + captured.err
    assert "query" in help_text
    assert "codemap-py query --compact rdeps" in help_text
    assert "codemap-py query --compact fn-rdeps" in help_text
    assert "codemap-py query --compact undocumented" in help_text
    assert "codemap-py query --compact uncovered" in help_text


def test_top_level_no_arguments_remain_a_syntax_error(capsys: pytest.CaptureFixture[str]) -> None:
    """Keep missing command handling distinct from the successful help path."""
    assert cli.main([]) == 2
    assert "usage: codemap-py" in capsys.readouterr().err


# Byte ratchet for the Codex query skill, which is re-sent on every invocation.  The bound is
# re-baselined only when mandatory content lands, never to make a red test green:
#   2500 -> 3600: four required disclosures the previous bound predates — the resolved
#   `PLUGIN_ROOT/bin/codemap-py query` command (the file previously named an undefined
#   `$CODEMAP_BIN`), a Runtime note, the 20-item result cap with `--limit 0`/`index.confidence`,
#   the partial-routing-table pointer to `--help`, and the test-impact subcommand-vs-skill split.
# The bound was already exceeded at 2518 bytes before that work, so it is not a fresh regression.
# 3600 -> 4100: mandatory independent-read/no-self-heal execution, retained launcher
# identity, targeted-help routing, distinct-fact settlement, and bounded correction
# retries. Preserve prior index-root/symbol/runtime safety details; never trim them
# merely to fit the old bound. Live token savings still require matched measurements.
_CODEX_QUERY_SKILL_MAX_BYTES = 4100


def test_codex_query_skill_is_compact_required_and_oriented_to_the_smallest_complete_query_set() -> None:
    """Prevent avoidable discovery overhead without dropping independently required facts."""
    skill_text = (_PLUGIN_ROOT / "codex-skills/query-code/SKILL.md").read_text(encoding="utf-8")

    assert len(skill_text.encode("utf-8")) <= _CODEX_QUERY_SKILL_MAX_BYTES
    assert all(
        phrase in skill_text
        for phrase in ("smallest complete query set", "query --compact", "Run each compact query alone")
    )
    assert "make one query" not in skill_text
    assert "Maximum: three Codemap calls" not in skill_text
    assert all(
        phrase in skill_text
        for phrase in (
            "shell persistence",
            "retain its literal",
            "SCAN_NO_AUTOBUILD=1",
            "--index <emitted-index-path> --root <same-root>",
            "symbol <name> --with-imports",
            "source-body implementation/runtime detail",
        )
    )


@pytest.mark.parametrize(
    ("command", "arguments", "result_key", "semantics_fragment"),
    [
        pytest.param(
            "undocumented",
            ("pkg.module", False),
            "undocumented",
            "declaration",
            id="undocumented-declarations",
        ),
        pytest.param(
            "uncovered",
            (argparse.Namespace(module="pkg.module", all_modules=False, sort="loc", top=20),),
            "uncovered",
            "symbol",
            id="uncovered-symbols",
        ),
    ],
)
def test_quality_query_output_labels_the_meaning_of_its_total(
    capsys: pytest.CaptureFixture[str],
    command: str,
    arguments: tuple[object, ...],
    result_key: str,
    semantics_fragment: str,
) -> None:
    """Prevent total counts from being mistaken for a different unique-name oracle."""
    index = {
        "scan_version": 11,
        "modules": [
            {
                "name": "pkg.module",
                "status": "ok",
                "is_test": False,
                "symbols": [
                    {
                        "name": "missing_docs",
                        "qualified_name": "pkg.module::missing_docs",
                        "type": "function",
                        "start_line": 1,
                        "end_line": 3,
                        "has_docstring": False,
                        "fn_rdep_test_count": 0,
                        "mock_rdep_count": 0,
                    },
                    {
                        "name": "same_name_second_declaration",
                        "qualified_name": "pkg.module::missing_docs",
                        "type": "function",
                        "start_line": 4,
                        "end_line": 6,
                        "has_docstring": False,
                        "fn_rdep_test_count": 0,
                        "mock_rdep_count": 0,
                    },
                ],
            }
        ],
    }

    if command == "undocumented":
        query.cmd_undocumented(index, *arguments)
    else:
        query.cmd_uncovered(index, *arguments)
    payload = json.loads(capsys.readouterr().out)

    assert payload["total"] == len(payload[result_key]) == 2
    assert payload["unique_total"] == 1
    assert payload["unique_qualified_names"] == ["pkg.module::missing_docs"]
    assert isinstance(payload["count_semantics"], dict)
    assert semantics_fragment in payload["count_semantics"]["total"].lower()


def test_uncovered_keeps_unique_semantics_when_top_hides_findings(capsys: pytest.CaptureFixture[str]) -> None:
    """Keep the full static count distinct from the capped display list."""
    index = {
        "scan_version": 11,
        "modules": [
            {
                "name": "pkg.module",
                "status": "ok",
                "is_test": False,
                "symbols": [
                    {
                        "name": "first",
                        "qualified_name": "pkg.module::first",
                        "type": "function",
                        "start_line": 1,
                        "end_line": 3,
                        "fn_rdep_test_count": 0,
                        "mock_rdep_count": 0,
                    },
                    {
                        "name": "second",
                        "qualified_name": "pkg.module::second",
                        "type": "function",
                        "start_line": 4,
                        "end_line": 6,
                        "fn_rdep_test_count": 0,
                        "mock_rdep_count": 0,
                    },
                ],
            }
        ],
    }
    query.cmd_uncovered(index, argparse.Namespace(module="pkg.module", all_modules=False, sort="name", top=1))

    payload = json.loads(capsys.readouterr().out)
    assert payload["total"] == payload["unique_total"] == 2
    assert payload["showing"] == len(payload["uncovered"]) == 1
    assert payload["unique_qualified_names"] == ["pkg.module::first", "pkg.module::second"]

"""The structural-context contract document and the code agree on every closed vocabulary consumers rely on.

Consumers on both runtimes classify missing structural context only by the slugs the contract lists, so a cause, a
status, a fallback rule or the record version present on one side and not the other would let a report carry a reason no
consumer recognises. The tables are read line by line, cell by cell.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from codemap_py import cli, structural_context
from codemap_py.structural_context import (
    CONSUMER_DETECTED_CAUSES,
    FALLBACK_CAUSES,
    SCHEMA,
    SCHEMA_VERSION,
    ContextCause,
    ContextStatus,
)

_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_CONTRACT = _PLUGIN_ROOT / "shared" / "structural-context-contract.md"
_CLAUDE_CONTEXT = _PLUGIN_ROOT / "claude-skills" / "_shared" / "codemap-context.md"


def _section_rows(heading: str) -> list[list[str]]:
    """Return the body rows of the first table under *heading*, each split into stripped cells."""
    lines = _CONTRACT.read_text(encoding="utf-8").splitlines()
    start = lines.index(heading) + 1
    rows: list[list[str]] = []
    for line in lines[start:]:
        if line.startswith("## "):
            break
        if line.startswith("|") and not line.startswith("| --") and not line.startswith("| Cause |"):
            rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
    return rows[1:] if rows and rows[0][0] in {"Status", "Field"} else rows


def _slug(cell: str) -> str:
    """Return the backticked slug of a table cell."""
    return cell.strip("`")


class TestCauseTable:
    """The cause table is the closed set the code emits, with the same detector and fallback split."""

    def test_lists_exactly_the_code_causes(self) -> None:
        """Every ContextCause has one row, and no row names a slug the code cannot emit."""
        slugs = [_slug(row[0]) for row in _section_rows("## Causes")]

        assert sorted(slugs) == sorted(cause.value for cause in ContextCause)
        assert len(slugs) == len(set(slugs))

    def test_consumer_detected_rows_match_code(self) -> None:
        """Rows detected by the consumer are exactly the causes codemap-py cannot report itself."""
        consumer = {_slug(row[0]) for row in _section_rows("## Causes") if row[2] == "consumer"}

        assert consumer == {cause.value for cause in CONSUMER_DETECTED_CAUSES}

    def test_fallback_rows_match_code(self) -> None:
        """Rows marked with a fallback are exactly the causes after which the text search runs."""
        with_fallback = {_slug(row[0]) for row in _section_rows("## Causes") if row[3] == "yes"}

        assert with_fallback == {cause.value for cause in FALLBACK_CAUSES}


class TestRecordVocabulary:
    """Statuses, record identity and the dispatcher entry match the document."""

    def test_status_table_matches_code(self) -> None:
        """The status table lists every ContextStatus value and nothing else."""
        statuses = {_slug(row[0]) for row in _section_rows("## Status")}

        assert statuses == {status.value for status in ContextStatus}

    def test_record_identity_is_documented(self) -> None:
        """The document names the schema identifier and version the code writes."""
        record_lines = _CONTRACT.read_text(encoding="utf-8").split("## Record", 1)[1].splitlines()

        assert any(f"`{SCHEMA}`" in line and f"`{SCHEMA_VERSION}`" in line for line in record_lines)

    @pytest.mark.parametrize(
        "flag",
        [
            pytest.param(flag, id=flag)
            for flag in ("--root", "--diff-file", "--expect-head", "--index-dir", "--no-build", "--timeout", "--out")
        ],
    )
    def test_documented_flags_exist(self, flag: str) -> None:
        """Every flag the call block documents is accepted by the parser."""
        call = _CONTRACT.read_text(encoding="utf-8").split("## Call", 1)[1].split("## Record", 1)[0]

        assert flag in call
        assert flag in structural_context._parser().format_help()

    def test_dispatcher_lists_context(self) -> None:
        """The top-level usage names the context command the contract documents."""
        assert "context" in cli._USAGE.split("{", 1)[1].split("}", 1)[0].split(",")


class TestClaudePointer:
    """The Claude consumer contract points review consumers at this contract."""

    def test_codemap_context_names_contract_and_command(self) -> None:
        """The Claude context contract names the command and the contract file in its own section."""
        lines = _CLAUDE_CONTEXT.read_text(encoding="utf-8").splitlines()
        section = lines[lines.index("## Change set at a reviewed head") + 1 :]

        assert any("codemap-py context" in line and "structural-context-contract.md" in line for line in section)

"""Keep plugin instructions from telling the model to grow a ledger through the Write or Edit tool.

A growing ledger (``.jsonl`` log, diary, journal, results file) only grows, and ``plugins/CLAUDE.md`` §Growing Ledgers
fixes how a record reaches it: stage one record in ``<ledger>.rec`` with the Write tool, then run the plugin's
``bin/append_ledger.py``. An instruction that says "append … with the Write tool" or "append via the Edit tool" instead
makes the model Read the ledger and Write it back, re-emitting every earlier record — one dropped or reworded line
silently rewrites history.

That shape shipped: ``calibrate`` read two logs, concatenated them and wrote the result back on every run, duplicating
legacy history each time; ``fortify`` recorded each ablation "with the Write/Edit tool" and later rewrote the whole
results file to add deltas.

The detector works per sentence: a sentence pairing an ``append`` verb with the Write or Edit tool fails unless it names
the ``.rec`` staging. ``append a counter suffix`` (output-file naming) is not a ledger append and is ignored. Known hits
that are correct for their context — an in-place edit of the artifact being refined, or a sentence where the Write tool
persists a separate file while the append is done by a ``jq`` block — are allow-listed by ``(path, excerpt)`` with the
reason, never by line number, since other work edits these files concurrently.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_DIR = REPO_ROOT / "plugins"

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_APPEND_VERB = re.compile(r"(?<![\w$-])[Aa]ppend(?:s|ed|ing)?\b(?!_)")
_COUNTER_SUFFIX = re.compile(r"\bappend(?:ing)? (?:a |the )?counter suffix", re.IGNORECASE)
_TOOL = re.compile(r"\b(?:Write|Edit)(?:/(?:Write|Edit))?\s+(?:tool|combined|back)\b|\bvia (?:the )?(?:Write|Edit)\b")
_STAGED = ".rec"
#: Prose that documents the policy itself, not instructions a skill executes.
_SKIPPED_NAMES = frozenset({"README.md", "CHANGELOG.md", "AUTHORING.md"})

#: (path relative to plugins/, excerpt of the flagged sentence) → why the hit is acceptable.
_ALLOWED: dict[tuple[str, str], str] = {
    (
        "cc_develop/skills/feature/SKILL.md",
        "lead appends the one-line entry to `CHANGELOG.md` under `Unreleased`",
    ): "exempt — inserts into a CHANGELOG section, the artifact being refined, not an end-of-file ledger",
    (
        "cc_oss/skills/release/modes/release-draft-template.md",
        "same Read+Edit tool mechanism as Append merge above",
    ): "exempt — release draft merge edits the refined artifact in place",
    (
        "cc_oss/skills/release/modes/release-draft-template.md",
        'Candidate missing (first append) → `cp "<executive-summary-draft>"',
    ): "exempt — SUMMARY.md increment is a mid-document edit of the refined artifact",
    (
        "cc_oss/skills/release/modes/release-draft-template.md",
        'Candidate missing (first append) → `cp "<migration-guide-draft>"',
    ): "exempt — MIGRATION.md blocks are inserted and struck in place in the refined artifact",
    (
        "cc_oss/skills/resolve/SKILL.md",
        "then write the whole map in one Write tool call",
    ): "not a ledger append — one Write of a fresh map file; 'append' names a rejected alternative",
    (
        "cc_oss/skills/resolve/modes/action-item-dispatch.md",
        "persist its raw public JSON object verbatim via the Write tool",
    ): "not a ledger append — Write persists a reply file; the append is a jq block",
    (
        "cc_oss/skills/resolve/modes/action-item-dispatch.md",
        "challenge-verdicts-<domain>-retry-<id>.json` with the Write tool as",
    ): "not a ledger append — Write persists a verdict file; the append is a jq block",
    (
        "cc_oss/skills/resolve/modes/action-item-dispatch.md",
        "persist its raw JSON reply verbatim via the Write tool",
    ): "not a ledger append — Write persists a verdict file; the append is a jq block",
    (
        "cc_oss/skills/resolve/modes/action-item-dispatch.md",
        "persist that reply via the Write tool to a **separate** file",
    ): "not a ledger append — Write persists a retry file; the append is a jq block",
}


def _instruction_files() -> list[Path]:
    """Return every plugin Markdown file that carries skill, agent, mode, or rule instructions."""
    return sorted(
        path
        for path in PLUGINS_DIR.rglob("*.md")
        if "tests" not in path.parts and path.name not in _SKIPPED_NAMES and path.parent != PLUGINS_DIR
    )


def ledger_tool_appends(text: str) -> list[tuple[int, str]]:
    """Return ``(line_number, sentence)`` for each sentence that appends via the Write or Edit tool.

    Args:
        text: Full Markdown file contents.

    Returns:
        Offending sentences in file order; a sentence naming the ``.rec`` staging is not offending.

    Examples:
        >>> ledger_tool_appends("Append one line to `results.jsonl` with the Write tool.\\n")
        [(1, 'Append one line to `results.jsonl` with the Write tool.')]
        >>> ledger_tool_appends("Write the line to `log.jsonl.rec` with the Write tool, then append it.\\n")
        []
        >>> ledger_tool_appends("Call **Write tool** to create the report (append a counter suffix if it exists).\\n")
        []
        >>> ledger_tool_appends("Set `$APPEND_STAGE` and Edit tool the draft.\\n")
        []
    """
    hits = []
    for number, line in enumerate(text.splitlines(), 1):
        for sentence in _SENTENCE_SPLIT.split(line):
            probe = _COUNTER_SUFFIX.sub("", sentence)
            if _STAGED not in probe and _APPEND_VERB.search(probe) and _TOOL.search(probe):
                hits.append((number, sentence.strip()))
    return hits


def _is_allowed(relative: str, sentence: str) -> bool:
    """Report whether a flagged sentence matches a documented allow-list entry for its file."""
    return any(path == relative and excerpt in sentence for path, excerpt in _ALLOWED)


@pytest.mark.parametrize(
    "path", [pytest.param(path, id=path.relative_to(PLUGINS_DIR).as_posix()) for path in _instruction_files()]
)
def test_ledgers_are_appended_by_staging_not_by_write_or_edit(path: Path) -> None:
    """Fail the exact file whose instruction grows a ledger through the Write or Edit tool."""
    relative = path.relative_to(PLUGINS_DIR).as_posix()
    hits = ledger_tool_appends(path.read_text(encoding="utf-8"))
    assert [f"{relative}:{n}: {s[:160]}" for n, s in hits if not _is_allowed(relative, s)] == []


@pytest.mark.parametrize(
    "sentence",
    [
        pytest.param(
            "Append new lines and Write combined content back to `calibrations.jsonl`.", id="read-concat-write"
        ),
        pytest.param("Update the report's section (append via Edit tool) with the table.", id="append-via-edit"),
        pytest.param(
            "Written with the Write/Edit tool, not a Bash block — a bash append would prompt.", id="write-edit"
        ),
    ],
)
def test_guard_fires_on_shapes_that_shipped(sentence: str) -> None:
    """Each sentence shape the converted skills carried before the append-only policy is detected."""
    assert ledger_tool_appends(sentence + "\n") == [(1, sentence)]

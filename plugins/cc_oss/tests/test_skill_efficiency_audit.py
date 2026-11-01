"""Plugin-wide audit: no oss skill instructs polling or bookkeeping-only turns, and every spawn has a wait rule.

Measured runs across the oss skills spent several calls per run on improvised ``ScheduleWakeup``/``ListAgents`` waits,
periodic ``find -newer`` probes and standalone task updates — each a full-context turn. This check walks every skill
file, so a new skill or mode that reintroduces either pattern fails here instead of in a live run. Prose checks run on
whitespace-flattened text (or single lines), so reflowing a paragraph never changes a verdict.
"""

from __future__ import annotations

import functools
import re
from pathlib import Path

import pytest
from _text_io import flat

_SKILLS = Path(__file__).resolve().parents[1] / "skills"

#: Files propagated byte-identical from cc_foundry; their wording is owned and audited there.
_PROPAGATED = {
    "codex-delegation.md",
    "compaction-contract.md",
    "file-handoff-protocol.md",
    "foundry--cross-validation-protocol.md",
    "notebook-style.md",
    "semver-rules.md",
    "terminal-summaries.md",
}

#: Instruction shapes that make the model poll instead of resuming on a completion notification.
_POLLING = [
    re.compile(r"poll every", re.I),
    re.compile(r"every \d+ ?min(ute)?s? while waiting", re.I),
    re.compile(r"polling interval", re.I),
    re.compile(r"liveness probe", re.I),
    re.compile(r"\bfind\b[^\n`]*-newer"),
    re.compile(r"\bHARD_CUTOFF\b"),
]
#: A line naming a waiting tool is fine only when it forbids it.
_WAIT_TOOLS = re.compile(r"\b(ScheduleWakeup|ListAgents)\b")
_FORBIDS = re.compile(
    r"\bnever\b|\bdo not\b|\bno (?:`?find|periodic|polling|checkpoint|`?sleep|`?ScheduleWakeup)", re.I
)
_WAIT_RULE = re.compile(r"agent-watch|agent_watch|Agent wait discipline|§Health monitoring|Agent waits")


@functools.cache
def _text(path: Path) -> str:
    """Return a skill file's text, read once per run."""
    return path.read_text(encoding="utf-8")


def _path_params(paths: list[Path]) -> list[object]:
    """Wrap skill files as ``pytest.param`` cases identified by their path under ``skills/``."""
    return [pytest.param(path, id=path.relative_to(_SKILLS).as_posix()) for path in paths]


def _unforbidden_match(line: str) -> bool:
    """Tell whether a line instructs polling: a match not preceded, within 120 chars, by a prohibition."""
    matches = [m for pattern in (_WAIT_TOOLS, *_POLLING) for m in pattern.finditer(line)]
    return any(not _FORBIDS.search(line[max(0, m.start() - 120) : m.start()]) for m in matches)


def _skill_files() -> list[Path]:
    """Every instruction file a skill loads — ARCH diagrams and propagated foundry files excluded."""
    return sorted(path for path in _SKILLS.rglob("*.md") if path.name != "ARCH.md" and path.name not in _PROPAGATED)


@pytest.mark.parametrize("path", _path_params(_skill_files()))
def test_no_polling_instruction(path: Path) -> None:
    """No skill file tells the model to poll, probe for liveness, or wait with a scheduling tool.

    Each line is judged alone: a waiting tool or polling phrase counts only when no prohibition ("never", "do not")
    precedes it within 120 characters on that line.
    """
    hits = [line.strip()[:80] for line in _text(path).splitlines() if _unforbidden_match(line)]
    assert hits == []


@pytest.mark.parametrize(
    "path", _path_params([path for path in _skill_files() if "Agent(" in path.read_text(encoding="utf-8")])
)
def test_every_spawn_file_points_at_a_wait_rule(path: Path) -> None:
    """A file that spawns an agent names the deadline-based wait rule, so the wait is never improvised.

    Scenario: any skill or mode file containing an ``Agent(`` spawn must also reference the agent-watch deadline rule.
    """
    assert _WAIT_RULE.search(flat(_text(path)))


def test_setup_keeps_its_conflict_question() -> None:
    """Oss:setup (light pass, untouched) still asks before overwriting a conflicting destination.

    Efficiency edits elsewhere must not remove the one question that protects a user's existing files.
    """
    text = flat(_text(_SKILLS / "setup" / "SKILL.md"))
    assert "Otherwise invoke `AskUserQuestion`, listing each conflicting destination and its current state:" in text

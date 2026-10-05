"""Plugin-wide audit: no oss skill instructs polling or bookkeeping-only turns, and every spawn has a wait rule.

Measured runs across the oss skills spent several calls per run on improvised ``ScheduleWakeup``/``ListAgents`` waits,
periodic ``find -newer`` probes and standalone task updates — each a full-context turn. This check walks every skill
file, so a new skill or mode that reintroduces either pattern fails here instead of in a live run.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

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
    re.compile(r"poll every", re.IGNORECASE),
    re.compile(r"every \d+ ?min(ute)?s? while waiting", re.IGNORECASE),
    re.compile(r"polling interval", re.IGNORECASE),
    re.compile(r"liveness probe", re.IGNORECASE),
    re.compile(r"\bfind\b[^\n`]*-newer"),
    re.compile(r"\bHARD_CUTOFF\b"),
]
#: A line naming a waiting tool is fine only when it forbids it.
_WAIT_TOOLS = re.compile(r"\b(ScheduleWakeup|ListAgents)\b")
_FORBIDS = re.compile(
    r"\bnever\b|\bdo not\b|\bno (?:`?find|periodic|polling|checkpoint|`?sleep|`?ScheduleWakeup)", re.IGNORECASE
)
_WAIT_RULE = re.compile(r"agent-watch|agent_watch|Agent wait discipline|§Health monitoring|Agent waits")
_RIDE_RULE = re.compile(r"bookkeeping-only|bookkeeping alone|rides? (in|with) the (response|next)", re.IGNORECASE)


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
    """No skill file tells the model to poll, probe for liveness, or wait with a scheduling tool."""
    hits = [line.strip()[:80] for line in path.read_text(encoding="utf-8").splitlines() if _unforbidden_match(line)]
    assert hits == []


@pytest.mark.parametrize(
    "path", _path_params([path for path in _skill_files() if "Agent(" in path.read_text(encoding="utf-8")])
)
def test_every_spawn_file_points_at_a_wait_rule(path: Path) -> None:
    """A file that spawns an agent names the deadline-based wait rule, so the wait is never improvised."""
    assert _WAIT_RULE.search(path.read_text(encoding="utf-8"))


def _task_skills() -> list[object]:
    """Every skill directory whose files drive tasks, as ``pytest.param`` cases named after the skill."""
    uses_tasks = re.compile(r"TaskCreate\(|TaskUpdate\(")
    return [
        pytest.param(directory, id=directory.name)
        for directory in sorted(_SKILLS.iterdir())
        if directory.is_dir() and any(uses_tasks.search(f.read_text(encoding="utf-8")) for f in directory.rglob("*.md"))
    ]


@pytest.mark.parametrize("skill", _task_skills())
def test_task_using_skill_forbids_bookkeeping_only_turns(skill: Path) -> None:
    """Every skill that drives tasks states that task calls ride with real work."""
    assert any(_RIDE_RULE.search(f.read_text(encoding="utf-8")) for f in skill.rglob("*.md"))


def test_setup_keeps_its_conflict_question() -> None:
    """Oss:setup (light pass, untouched) still asks before overwriting a conflicting destination."""
    text = (_SKILLS / "setup" / "SKILL.md").read_text(encoding="utf-8")
    assert "Otherwise invoke `AskUserQuestion`, listing each conflicting destination and its current state:" in text


_TASK_ONLY_BLOCK = re.compile(r"```text\n(.*?)```", re.DOTALL)
_TASK_CALL = re.compile(r"\s*(TaskUpdate|TaskCreate)\(|\s*for each .*TaskUpdate\(")
_RIDES_OR_EXCEPTION = re.compile(
    r"riding|rides (in|with)|same response|one response|standalone|before (the )?long output|before printing",
    re.IGNORECASE,
)


def _task_only_blocks(path: Path) -> list[tuple[str, str]]:
    """Return ``(preceding text, block body)`` for every fenced text block holding nothing but task calls."""
    text = path.read_text(encoding="utf-8")
    blocks = []
    for match in _TASK_ONLY_BLOCK.finditer(text):
        body = [line for line in match.group(1).splitlines() if line.strip()]
        if body and all(_TASK_CALL.match(line) for line in body):
            blocks.append((text[max(0, match.start() - 400) : match.start()], match.group(1)))
    return blocks


@pytest.mark.parametrize("path", _path_params(_skill_files()))
def test_no_standalone_in_progress_task_blocks(path: Path) -> None:
    """No step opens with a task-only ``in_progress`` block — the per-step pair that cost a turn of its own.

    The baseline resolve skill had seven such blocks; each step's start is now carried by the call that does its work.
    """
    assert [body for _, body in _task_only_blocks(path) if "in_progress" in body] == []


@pytest.mark.parametrize("path", _path_params(_skill_files()))
def test_every_task_only_block_states_how_it_rides(path: Path) -> None:
    """Every remaining task-only block says it rides with real work, or is the one completion before long output."""
    unexplained = [
        body.strip()[:70] for before, body in _task_only_blocks(path) if not _RIDES_OR_EXCEPTION.search(before + body)
    ]
    assert unexplained == []

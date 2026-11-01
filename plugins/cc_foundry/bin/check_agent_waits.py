#!/usr/bin/env python
"""check_agent_waits.py — flag skill/agent text that polls for agents, spends a turn on bookkeeping, or hides timeouts.

Every ``Agent()`` spawn runs in the background and the harness wakes the orchestrator with a completion notification,
so waiting on an agent is never a tool call. Measured in real runs: orchestrators that read an allowance for "one
``Monitor`` call per turn" or a ``find -newer`` liveness probe improvised ``ScheduleWakeup`` / ``ListAgents`` loops
(399 ``ScheduleWakeup`` calls in one month of ``/oss:resolve`` runs) while users typed "check on the agents". A task
update sent as its own response costs a full re-read of the live context for one bookkeeping record. The canonical rule
is ``rules/task-lifecycle.md``; this checker keeps every plugin's skill and agent text from prescribing either habit.

Findings, one per offending line:

``wait-tool``
    A line names ``ScheduleWakeup``, ``ListAgents`` or ``Monitor`` without forbidding it.
``fixed-poll``
    A line prescribes a fixed-interval poll (``poll every``, ``every N min`` while waiting or checking,
    ``MONITOR_INTERVAL``), the retired ``health_sentinel`` helper, or a ``find -newer … wc -l`` liveness probe.
``bookkeeping-turn``
    A line puts ``TaskCreate`` / ``TaskUpdate`` / ``TaskList`` in a turn or response of its own. The one sanctioned
    exception — ``TaskUpdate(completed)`` immediately before a long output block — is not a finding.
``standalone-in-progress``
    A fenced ``text`` block holds nothing but task calls and one of them sets ``in_progress`` — the per-step
    start-of-step update that costs a full-context turn of its own.
``unexplained-task-block``
    A fenced ``text`` block holding nothing but task calls neither rides with real work nor is the completion before
    long output: none of the 400 characters before it, nor its body, says so (``riding``, ``rides with``,
    ``same response``, ``standalone``, ``before the long output``…).
``missing-ride-rule``
    A skill directory drives tasks (``TaskCreate(`` / ``TaskUpdate(``) yet none of its Markdown states that task calls
    ride with the next call (``bookkeeping-only``, ``bookkeeping alone``, ``rides with the next…``).
``silent-timeout``
    A skill directory spawns agents (``Agent(subagent_type=…)``) yet none of its Markdown ever surfaces a timed-out
    agent (``⏱`` / ``timed_out``). Replacing polling with notifications must not make a stalled agent invisible: the
    user has to see every agent that never delivered. Reported once per skill directory, line ``0``.

A line that negates the pattern ("never", "no", "not", "forbid", "ban", "instead of", "retired"…) is a prohibition,
not an instruction, and is skipped. Detector source that merely contains the patterns (a ``grep`` or ``re.compile``
line) is skipped too. A line carrying ``wait-check: allow`` is an explicit, reviewed exemption.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/check_agent_waits.py" --scan-dir plugins
    python "${CLAUDE_PLUGIN_ROOT}/bin/check_agent_waits.py" path/to/SKILL.md [...]

Exit codes:
    0 — no findings
    1 — one or more findings (printed as ``<kind>: <path>:<line>: <text>``)
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

#: Words showing a line forbids or describes a wait pattern rather than prescribing it.
_NEGATION = re.compile(
    r"(?i)\b(?:never|no|not|nor|don't|do not|forbid\w*|ban\w*|without|instead of|replac\w*|retired|stale|zero)\b"
)
#: Matches lines that are themselves grep or regex detector code, which must not be flagged.
_DETECTOR_SOURCE = re.compile(r"\bgrep\b|re\.compile|\brg\b")
#: Marker text that exempts a line from the wait check.
_ALLOW = "wait-check: allow"
#: Matches tools that must not be used to wait on spawned agents (ScheduleWakeup, ListAgents, Monitor).
_WAIT_TOOL = re.compile(r"\b(?:ScheduleWakeup|ListAgents)\b|\bMonitor\(|`Monitor`")
#: Matches fixed-interval polling wording, such as ``poll every`` or ``MONITOR_INTERVAL``.
_FIXED_POLL = re.compile(
    r"(?i)poll every|\bevery \d+\s*(?:s|sec|secs|seconds|min|mins|minutes)\b.*\b(?:wait|waiting|poll|probe|check)"
    r"|MONITOR_INTERVAL|health_sentinel|-newer\b.*\bwc -l"
)
#: Matches mentions of the TaskCreate, TaskUpdate and TaskList tools.
_TASK_TOOL = re.compile(r"\bTask(?:Create|Update|List)\b")
#: Matches wording that gives a task update its own separate turn or response.
_OWN_TURN = re.compile(
    r"(?i)\b(?:own turn|separate turn|own response|separate response|standalone turn|turn of its own"
    r"|response of its own|bookkeeping-only (?:turn|response))\b"
)
#: Matches the ``long output`` wording of the one sanctioned standalone task-update exception.
_LONG_OUTPUT = re.compile(r"(?i)long output")
#: Glob patterns selecting the skill and agent Markdown files that are scanned.
_SCAN_GLOBS = ("**/skills/**/*.md", "**/agents/*.md")
#: Matches an ``Agent(subagent_type`` call, marking a skill as one that spawns agents.
_SPAWN = re.compile(r"\bAgent\(subagent_type")
#: Matches wording showing a timed-out agent is surfaced to the user (the clock symbol or ``timed_out``).
_TIMEOUT_VISIBLE = re.compile(r"⏱|timed_out|timed out")

#: Matches a task call as the first token of a line, including the ``for each … TaskUpdate(`` loop form.
_TASK_CALL = re.compile(r"\s*(?:TaskUpdate|TaskCreate)\(|\s*for each .*TaskUpdate\(")
#: Matches the statement that task calls ride with the next call instead of taking a bookkeeping-only turn.
_RIDE_RULE = re.compile(r"(?i)bookkeeping-only|bookkeeping alone|rides? (?:in|with) the (?:response|next)")
#: Matches wording that explains why a task-only block does not cost a turn of its own.
_RIDES_OR_EXCEPTION = re.compile(
    r"(?i)riding|rides (?:in|with)|same response|one response|standalone|before (?:the )?long output|before printing"
)
#: Matches a call that opens a task-driving skill: ``TaskCreate(`` or ``TaskUpdate(``.
_USES_TASKS = re.compile(r"TaskCreate\(|TaskUpdate\(")
#: Opening fence of a ``text`` block, the form skills use to show task calls.
_TEXT_FENCE = "```text"
#: How many characters before a task-only block may carry its explanation.
_CONTEXT_CHARS = 400


@dataclass(frozen=True)
class Finding:
    """One offending instruction line."""

    kind: str
    path: str
    line: int
    text: str


def classify(line: str) -> str | None:
    """Return the finding kind an instruction line represents, or None when it is clean or a prohibition.

    Args:
        line: One line of skill or agent Markdown.

    Returns:
        ``wait-tool``, ``fixed-poll``, ``bookkeeping-turn``, or None.

    Examples:
        >>> classify("Call ScheduleWakeup(300) and check again.")
        'wait-tool'
        >>> classify("Never call ScheduleWakeup to wait on an agent.") is None
        True
        >>> classify("Every 5 min while waiting: probe the run dir.")
        'fixed-poll'
        >>> classify("Send TaskUpdate in a separate response.")
        'bookkeeping-turn'
        >>> classify("TaskUpdate(completed) as its own response immediately before the long output block.") is None
        True
    """
    if _ALLOW in line or _DETECTOR_SOURCE.search(line) or _NEGATION.search(line):
        return None
    if _WAIT_TOOL.search(line):
        return "wait-tool"
    if _FIXED_POLL.search(line):
        return "fixed-poll"
    if _TASK_TOOL.search(line) and _OWN_TURN.search(line) and not _LONG_OUTPUT.search(line):
        return "bookkeeping-turn"
    return None


def scan_file(path: Path) -> list[Finding]:
    """Classify every line of one Markdown file.

    Args:
        path: Skill, mode, template or agent Markdown file.

    Returns:
        One finding per offending line; an unreadable file yields none.
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [
        Finding(kind, path.as_posix(), number, text.strip()[:160])
        for number, text in enumerate(lines, start=1)
        if (kind := classify(text))
    ]


def task_only_blocks(text: str) -> list[tuple[int, str, str]]:
    """Return ``(line, preceding text, body)`` for every ``text`` fence that holds nothing but task calls.

    The scan is line by line, so reflowing a paragraph never changes a verdict. The preceding text is the
    whitespace-flattened ``_CONTEXT_CHARS`` characters before the opening fence.

    Args:
        text: Full Markdown text of one skill file.

    Returns:
        One tuple per task-only block: its 1-based fence line, the flattened text before it, and its body.

    Examples:
        >>> task_only_blocks("Rides with the next call.\\n```text\\nTaskUpdate(1, completed)\\n```\\n")[0][0]
        2
        >>> task_only_blocks("```text\\nsome output\\n```\\n")
        []
    """
    lines = text.splitlines()
    blocks = []
    index = 0
    while index < len(lines):
        if lines[index].strip() != _TEXT_FENCE:
            index += 1
            continue
        end = index + 1
        while end < len(lines) and lines[end].strip() != "```":
            end += 1
        body = [line for line in lines[index + 1 : end] if line.strip()]
        if body and all(_TASK_CALL.match(line) for line in body):
            before = "\n".join(lines[:index])[-_CONTEXT_CHARS:]
            blocks.append((index + 1, " ".join(before.split()), "\n".join(lines[index + 1 : end])))
        index = end + 1
    return blocks


def task_block_findings(path: Path) -> list[Finding]:
    """Flag task-only blocks that open a step with ``in_progress`` or never say how they avoid a turn of their own.

    Args:
        path: Skill, mode or template Markdown file.

    Returns:
        ``standalone-in-progress`` and ``unexplained-task-block`` findings; an unreadable file yields none.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    findings = []
    for line, before, body in task_only_blocks(text):
        first = body.strip().splitlines()[0].strip()[:160]
        if "in_progress" in body:
            findings.append(Finding("standalone-in-progress", path.as_posix(), line, first))
        elif not _RIDES_OR_EXCEPTION.search(" ".join((before + " " + body).split())):
            findings.append(Finding("unexplained-task-block", path.as_posix(), line, first))
    return findings


def missing_ride_rules(paths: list[Path]) -> list[Finding]:
    """Report each skill directory that drives tasks but never states that task calls ride with real work.

    Args:
        paths: Markdown files already collected for scanning.

    Returns:
        One ``missing-ride-rule`` finding per offending skill directory.
    """
    uses: dict[Path, bool] = {}
    states: dict[Path, bool] = {}
    for path in paths:
        skill = _skill_dir(path)
        if skill is None or skill.name == "_shared":
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        uses[skill] = uses.get(skill, False) or bool(_USES_TASKS.search(text))
        states[skill] = states.get(skill, False) or bool(_RIDE_RULE.search(" ".join(text.split())))
    return [
        Finding("missing-ride-rule", skill.as_posix(), 0, "drives tasks but never says task calls ride with real work")
        for skill in sorted(uses)
        if uses[skill] and not states[skill]
    ]


def _skill_dir(path: Path) -> Path | None:
    """Return the ``skills/<name>`` directory a file belongs to, or None outside a skill.

    Examples:
        >>> _skill_dir(Path("p/skills/run/modes/a.md")).as_posix()
        'p/skills/run'
        >>> _skill_dir(Path("p/agents/a.md")) is None
        True
    """
    parts = path.parts
    for index in range(len(parts) - 2, -1, -1):
        if parts[index] in {"skills", "claude-skills"} and index + 1 < len(parts) - 1:
            return Path(*parts[: index + 2])
    return None


def silent_timeouts(paths: list[Path]) -> list[Finding]:
    """Report each skill directory that spawns agents but never shows the user a timed-out agent.

    Args:
        paths: Markdown files already collected for scanning.

    Returns:
        One ``silent-timeout`` finding per offending skill directory.
    """
    spawns: dict[Path, bool] = {}
    visible: dict[Path, bool] = {}
    for path in paths:
        skill = _skill_dir(path)
        if skill is None or skill.name == "_shared":
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        spawns[skill] = spawns.get(skill, False) or bool(_SPAWN.search(text))
        visible[skill] = visible.get(skill, False) or bool(_TIMEOUT_VISIBLE.search(text))
    return [
        Finding(
            "silent-timeout", skill.as_posix(), 0, "spawns agents but never surfaces a timed-out agent (⏱ / timed_out)"
        )
        for skill in sorted(spawns)
        if spawns[skill] and not visible[skill]
    ]


def collect(scan_dir: Path | None, files: list[str]) -> list[Path]:
    """Return the Markdown files to check: explicit paths, or every skill/agent file under ``scan_dir``.

    Args:
        scan_dir: Directory whose ``skills/**`` and ``agents/*`` Markdown is scanned, or None.
        files: Explicit file paths.

    Returns:
        Sorted, de-duplicated file paths.
    """
    found = {Path(f) for f in files if f.endswith(".md")}
    if scan_dir is not None:
        for pattern in _SCAN_GLOBS:
            found.update(p for p in scan_dir.glob(pattern) if p.is_file())
    return sorted(found)


def main(argv: list[str] | None = None) -> int:
    """Print every finding and return the exit code.

    Args:
        argv: Command-line arguments; ``None`` reads ``sys.argv``.

    Returns:
        0 when clean, 1 when any finding exists.
    """
    parser = argparse.ArgumentParser(description="Flag agent polling and bookkeeping-only turns in skill/agent text.")
    parser.add_argument("--scan-dir", type=Path, help="directory whose skills/ and agents/ Markdown is scanned")
    parser.add_argument("files", nargs="*", help="explicit Markdown files")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]
    paths = collect(args.scan_dir, args.files)
    findings = (
        [finding for path in paths for finding in scan_file(path) + task_block_findings(path)]
        + silent_timeouts(paths)
        + missing_ride_rules(paths)
    )
    for finding in findings:
        print(f"{finding.kind}: {finding.path}:{finding.line}: {finding.text}")
    if not findings:
        print("✓ agent-waits: no polling, bookkeeping-only turn, or silent-timeout instructions")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())

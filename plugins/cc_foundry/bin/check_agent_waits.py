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

_NEGATION = re.compile(
    r"(?i)\b(?:never|no|not|nor|don't|do not|forbid\w*|ban\w*|without|instead of|replac\w*|retired|stale|zero)\b"
)
_DETECTOR_SOURCE = re.compile(r"\bgrep\b|re\.compile|\brg\b")
_ALLOW = "wait-check: allow"
_WAIT_TOOL = re.compile(r"\b(?:ScheduleWakeup|ListAgents)\b|\bMonitor\(|`Monitor`")
_FIXED_POLL = re.compile(
    r"(?i)poll every|\bevery \d+\s*(?:s|sec|secs|seconds|min|mins|minutes)\b.*\b(?:wait|waiting|poll|probe|check)"
    r"|MONITOR_INTERVAL|health_sentinel|-newer\b.*\bwc -l"
)
_TASK_TOOL = re.compile(r"\bTask(?:Create|Update|List)\b")
_OWN_TURN = re.compile(
    r"(?i)\b(?:own turn|separate turn|own response|separate response|standalone turn|turn of its own"
    r"|response of its own|bookkeeping-only (?:turn|response))\b"
)
_LONG_OUTPUT = re.compile(r"(?i)long output")
_SCAN_GLOBS = ("**/skills/**/*.md", "**/agents/*.md")
_SPAWN = re.compile(r"\bAgent\(subagent_type")
_TIMEOUT_VISIBLE = re.compile(r"⏱|timed_out|timed out")


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
    findings = [finding for path in paths for finding in scan_file(path)] + silent_timeouts(paths)
    for finding in findings:
        print(f"{finding.kind}: {finding.path}:{finding.line}: {finding.text}")
    if not findings:
        print("✓ agent-waits: no polling, bookkeeping-only turn, or silent-timeout instructions")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())

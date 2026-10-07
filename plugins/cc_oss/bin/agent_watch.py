#!/usr/bin/env python
"""agent_watch.py — report every spawned agent's deliverable and deadline in one call, so nothing polls.

``oss:resolve`` spawns background agents and resumes on their completion notifications. Without a deadline check, a
stalled or idle agent is noticed only when the user types "check on the agents", and the orchestrator was observed
improvising ``ScheduleWakeup`` / ``ListAgents`` polling instead. This script replaces both: at every wake-up the
orchestrator runs it once and acts on the verdicts it prints.

State files: ``<state-dir>/agent-watch-<batch>.tsv``, written with the Write tool in the same response as the spawn
batch. One row per agent: ``<name>\\t<deliverable path or ->\\t<deadline seconds>``. The file's modification time is
the batch's spawn time, so no clock value ever has to be typed into a command. ``-`` marks an envelope-only agent
whose deliverable is its final message rather than a file.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/agent_watch.py" --state-dir "$IMPL_DIR"

Per agent: batch, name, deliverable, status, elapsed seconds since spawn, seconds left to the deadline.
Statuses printed per agent:
    done             — deliverable file exists and is non-empty
    pending          — no deliverable yet, deadline not reached
    awaiting-envelope — envelope-only agent, deadline not reached
    timed_out        — no deliverable and the deadline has passed

Exit codes:
    0 — report printed (including when no state file exists: an empty agent list)
    1 — the state directory does not exist
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

#: Deliverable placeholder meaning the agent writes no file and is judged only by its returned envelope.
_ENVELOPE_ONLY = "-"


@dataclass(frozen=True)
class AgentVerdict:
    """One spawned agent's current standing against its deliverable and deadline."""

    batch: str
    name: str
    deliverable: str
    status: str
    elapsed_s: int
    seconds_left: int


def classify(deliverable: str, deadline_epoch: float, now: float) -> str:
    """Decide an agent's status from its deliverable and deadline.

    Args:
        deliverable: Path of the agent's output file, or ``-`` for an envelope-only agent.
        deadline_epoch: Absolute deadline in epoch seconds.
        now: Current time in epoch seconds.

    Returns:
        ``done``, ``pending``, ``awaiting-envelope`` or ``timed_out``.

    Examples:
        >>> classify("-", 100.0, 50.0), classify("-", 100.0, 150.0)
        ('awaiting-envelope', 'timed_out')
    """
    if deliverable != _ENVELOPE_ONLY and Path(deliverable).is_file() and Path(deliverable).stat().st_size > 0:
        return "done"
    if now >= deadline_epoch:
        return "timed_out"
    return "awaiting-envelope" if deliverable == _ENVELOPE_ONLY else "pending"


def read_batch(state_file: Path, now: float) -> list[AgentVerdict]:
    """Read one batch file and classify each agent row in it.

    Args:
        state_file: ``agent-watch-<batch>.tsv`` file; its modification time is the spawn time.
        now: Current time in epoch seconds.

    Returns:
        One verdict per well-formed row; a malformed row is reported as ``timed_out`` so it is never silently lost.
    """
    batch = state_file.stem.removeprefix("agent-watch-")
    spawned = state_file.stat().st_mtime
    verdicts = []
    for line in state_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 3 or not parts[2].strip().isdigit():
            verdicts.append(
                AgentVerdict(batch, parts[0].strip() or "?", "", "timed_out", max(0, int(now - spawned)), 0)
            )
            continue
        name, deliverable, seconds = (part.strip() for part in parts)
        deadline = spawned + int(seconds)
        status = classify(deliverable, deadline, now)
        verdicts.append(
            AgentVerdict(batch, name, deliverable, status, max(0, int(now - spawned)), max(0, int(deadline - now)))
        )
    return verdicts


def watch(state_dir: Path, now: float) -> dict[str, object]:
    """Classify every agent across every batch file in ``state_dir``.

    Args:
        state_dir: Directory holding ``agent-watch-*.tsv`` files.
        now: Current time in epoch seconds.

    Returns:
        ``agents`` (all verdicts, each with ``elapsed_s`` since spawn and ``seconds_left`` to its deadline), ``open``
        (count still pending or awaiting an envelope), ``pending`` (their names) and ``timed_out`` (names).
    """
    verdicts = [verdict for path in sorted(state_dir.glob("agent-watch-*.tsv")) for verdict in read_batch(path, now)]
    return {
        "agents": [asdict(verdict) for verdict in verdicts],
        "open": sum(verdict.status in {"pending", "awaiting-envelope"} for verdict in verdicts),
        "pending": [verdict.name for verdict in verdicts if verdict.status in {"pending", "awaiting-envelope"}],
        "timed_out": [verdict.name for verdict in verdicts if verdict.status == "timed_out"],
    }


def main(argv: list[str] | None = None) -> int:
    """Print the watch report as one JSON line.

    Args:
        argv: Command-line arguments; ``None`` reads ``sys.argv``.

    Returns:
        Process exit code (see module docstring).
    """
    parser = argparse.ArgumentParser(description="Report spawned agents' deliverables and deadlines.")
    parser.add_argument("--state-dir", required=True, help="directory holding agent-watch-*.tsv batch files")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]
    state_dir = Path(args.state_dir)
    if not state_dir.is_dir():
        print(json.dumps({"error": f"state dir not found: {args.state_dir}"}))
        return 1
    print(json.dumps(watch(state_dir, time.time())))
    return 0


if __name__ == "__main__":
    sys.exit(main())

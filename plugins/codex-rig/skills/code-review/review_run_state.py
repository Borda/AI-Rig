#!/usr/bin/env python3
"""Record and classify how far a Code Review run got before it stopped.

## Purpose

A review run that stops before finalization leaves no ``result.json``, so any count built only from results misses it
and cannot say why it stopped. This helper writes a small ``run-state.json`` stub at the routing and dispatch
checkpoints and classifies every Code Review run directory, finished or not, into one terminal state with the last
checkpoint it reached and a cause. It does not classify remediation run directories: the gap roll-up reads those stubs
itself.

## Scope

``result.json`` keeps its meaning: only a promoted, validated review result is named that, so report lookup, retries,
cleanup and remediation intake are unaffected by the stub. The stub is written next to the other run artifacts and is
never read as review evidence. Classification only reads file names and the two small JSON files; it never validates a
review, changes artifacts or contacts any service.

## Usage

``mark(run_dir, checkpoint)`` is called by ``review_routing.py`` after routing and by ``review_prepare.py`` after
preparing a dispatch and after assembling the reviewer manifest. ``python review_run_state.py classify <run-dir>...``
prints one JSON object per run directory for counts and audits.

## Used by

``review_routing.py`` and ``review_prepare.py`` record checkpoints; ``classify`` is run through the command line above
for Code Review run counts and audits. The gap roll-up does not call it.

## Outputs

``run-state.json`` holds ``schema_version`` 1, ``review_status`` ``incomplete``, the ``checkpoint`` and a ``cause``.
``classify`` returns ``review_status`` (``assessed``, ``unavailable``, ``closed`` or ``incomplete``), ``terminal``,
``checkpoint`` and ``cause``.

## Failure

``mark`` raises ``ValueError`` for an unknown checkpoint or a redirected run directory. ``classify`` reports an
unreadable or malformed ``result.json`` as ``review_status`` ``invalid`` and reads a malformed stub, whatever JSON its
fields hold, as no stub instead of raising. The CLI classifies each directory separately, so one damaged run cannot hide
the others from a count.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

#: File name of the non-terminal run stub; deliberately distinct from the promoted ``result.json``.
RUN_STATE_NAME = "run-state.json"
#: Version family of the ``run-state.json`` stub.
RUN_STATE_SCHEMA_VERSION = 1
#: Checkpoints in workflow order, each with the run artifacts whose presence proves it was reached.
CHECKPOINT_ARTIFACTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("allocated", ("review-entry.json", "pr-target.txt")),
    ("source-collected", ("pr.json", "diff.patch", "files.txt")),
    ("routed", ("review-routing.json",)),
    (
        "dispatch-prepared",
        ("dispatch.json", "direct-plan.json", "inspection-plan.json", "batch-dispatch.json", "batch-inventory.json"),
    ),
    ("reviewers-joined", ("specialist-manifest.json", "inspection-summary.json")),
    ("notes-written", ("review-notes.md",)),
    ("gates-run", ("gates.json",)),
    ("candidate-written", ("result.candidate.json",)),
)
#: Checkpoint names in workflow order.
CHECKPOINTS: tuple[str, ...] = tuple(name for name, _ in CHECKPOINT_ARTIFACTS)
#: Checkpoints a workflow helper may record in the stub.
MARKABLE_CHECKPOINTS = frozenset({"routed", "dispatch-prepared", "reviewers-joined"})


def stub_cause(checkpoint: str) -> str:
    """Describe why a run with this last checkpoint has no terminal result.

    Examples:
        >>> stub_cause("dispatch-prepared")
        'The run reached dispatch-prepared and no promoted result.json was written afterwards.'
        >>> stub_cause("candidate-written")
        'result.candidate.json was written but never promoted; finalization validation failed or did not finish.'
    """
    if checkpoint == "candidate-written":
        return "result.candidate.json was written but never promoted; finalization validation failed or did not finish."
    return f"The run reached {checkpoint} and no promoted result.json was written afterwards."


def _later(first: str | None, second: str | None) -> str | None:
    """Return the later of two checkpoints in workflow order, ignoring unknown names.

    Examples:
        >>> _later("routed", "gates-run")
        'gates-run'
        >>> _later(None, "routed")
        'routed'
    """
    known = [name for name in (first, second) if name in CHECKPOINTS]
    return max(known, key=CHECKPOINTS.index) if known else None


def _artifact_checkpoint(run_dir: Path) -> str | None:
    """Return the latest checkpoint whose proving artifact exists in the run directory."""
    reached = None
    for name, artifacts in CHECKPOINT_ARTIFACTS:
        if any(os.path.lexists(run_dir / artifact) for artifact in artifacts):
            reached = name
    return reached


def _read_object(path: Path) -> dict[str, Any] | None:
    """Read a JSON object without following a redirected path, or ``None`` when unusable."""
    if path.is_symlink() or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    return payload if isinstance(payload, dict) else None


def _named(value: object, names: frozenset[str] | tuple[str, ...]) -> bool:
    """Report whether a JSON value is one of the given names; a list, object or number never is, nor raises.

    Examples:
        >>> _named("routed", MARKABLE_CHECKPOINTS)
        True
        >>> _named(["routed"], MARKABLE_CHECKPOINTS)
        False
    """
    return isinstance(value, str) and value in names


def read_stub(run_dir: Path) -> dict[str, Any] | None:
    """Return a well-formed run stub, or ``None`` when absent or malformed, without raising on any JSON content."""
    stub = _read_object(run_dir / RUN_STATE_NAME)
    if (
        stub is None
        or stub.get("schema_version") != RUN_STATE_SCHEMA_VERSION
        or stub.get("review_status") != "incomplete"
        or not _named(stub.get("checkpoint"), MARKABLE_CHECKPOINTS)
        or not isinstance(stub.get("cause"), str)
    ):
        return None
    return stub


def mark(run_dir: Path, checkpoint: str) -> dict[str, Any] | None:
    """Record that the run reached ``checkpoint`` unless it already has a promoted result.

    The stub only moves forward: an earlier checkpoint never replaces a later one. The write is atomic so a run killed
    mid-write keeps its previous stub.
    """
    if checkpoint not in MARKABLE_CHECKPOINTS:
        raise ValueError(f"run-state-checkpoint-invalid:{checkpoint}")
    if run_dir.is_symlink() or not run_dir.is_dir():
        raise ValueError(f"run-state-directory-invalid:{run_dir}")
    if os.path.lexists(run_dir / "result.json"):
        return None
    previous = read_stub(run_dir)
    reached = _later(previous.get("checkpoint") if previous else None, checkpoint) or checkpoint
    stub = {
        "schema_version": RUN_STATE_SCHEMA_VERSION,
        "review_status": "incomplete",
        "checkpoint": reached,
        "cause": stub_cause(reached),
    }
    if previous == stub:
        return stub
    target = run_dir / RUN_STATE_NAME
    if target.is_symlink():
        raise ValueError(f"run-state-redirected:{target}")
    descriptor, temporary = tempfile.mkstemp(prefix=".run-state.", suffix=".tmp", dir=run_dir)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(stub, indent=2, sort_keys=True) + "\n")
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return stub


def _terminal_state(result: dict[str, Any] | None) -> dict[str, Any]:
    """Classify a promoted result by its review axis."""
    metadata = result.get("metadata") if result is not None else None
    if not isinstance(metadata, dict):
        return {"review_status": "invalid", "terminal": True, "cause": "result.json is unreadable or malformed."}
    review_status = metadata.get("review_status")
    if _named(review_status, ("unavailable", "closed")):
        return {"review_status": review_status, "terminal": True, "cause": None}
    if review_status is None and isinstance(metadata.get("review_decision"), dict):
        return {"review_status": "assessed", "terminal": True, "cause": None}
    return {"review_status": "invalid", "terminal": True, "cause": "result.json has no recognized review disposition."}


def classify(run_dir: Path) -> dict[str, Any]:
    """Classify one run directory into a single terminal or incomplete state.

    A promoted ``result.json`` decides the state. Otherwise the run is ``incomplete`` at the later of its recorded stub
    checkpoint and the latest checkpoint its artifacts prove.
    """
    checkpoint = _artifact_checkpoint(run_dir)
    if os.path.lexists(run_dir / "result.json"):
        state = _terminal_state(_read_object(run_dir / "result.json"))
        return {"run": str(run_dir), "checkpoint": "promoted", "stub": None, **state}
    stub = read_stub(run_dir)
    reached = _later(stub.get("checkpoint") if stub else None, checkpoint)
    return {
        "run": str(run_dir),
        "review_status": "incomplete",
        "terminal": False,
        "checkpoint": reached,
        "stub": stub.get("checkpoint") if stub else None,
        "cause": stub_cause(reached) if reached else "The run directory holds no recognized workflow artifact.",
    }


def main(argv: list[str] | None = None) -> int:
    """Mark one run checkpoint or classify run directories as JSON lines."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    mark_parser = commands.add_parser("mark", help="Record a reached checkpoint in run-state.json.")
    mark_parser.add_argument("--run", required=True, type=Path, help="Code Review run directory.")
    mark_parser.add_argument("--checkpoint", required=True, choices=sorted(MARKABLE_CHECKPOINTS))
    classify_parser = commands.add_parser("classify", help="Print one terminal-state record per run directory.")
    classify_parser.add_argument("runs", nargs="+", type=Path, help="Run directories to classify.")
    args = parser.parse_args(argv)
    if args.command == "mark":
        try:
            print(json.dumps(mark(args.run, args.checkpoint)))
        except (OSError, ValueError) as error:
            print(f"run-state-failed:{error}", file=sys.stderr)
            return 2
        return 0
    status = 0
    for run in args.runs:
        # Classify each run on its own, so one unreadable directory cannot hide the runs after it from a count.
        try:
            print(json.dumps(classify(run), sort_keys=True))
        except (OSError, ValueError) as error:
            print(f"run-state-failed:{run}:{error}", file=sys.stderr)
            status = 2
    return status


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Create or promote one portable artifact directory for a Codex Rig workflow.

## Purpose

Give each workflow invocation an isolated, predictable evidence location without requiring shell-specific timestamp
handling. The generated directory is the handoff point that later helpers use for all run-scoped evidence and results. A
collected PR review can subsequently promote that complete directory into a stable PR-specific sequence.

## Scope

It validates a canonical skill identifier and either creates one local directory or promotes one direct child produced
by ``code-review``. Promotion reads the authoritative PR number from the run's ``pr.json`` and moves the whole run
atomically; it neither changes evidence files nor starts a workflow.

## Usage

Run ``python create_run.py --skill <canonical-skill-id>`` once and use the printed path literally in later commands.
After PR collection, run it again with ``--skill code-review --promote-pr-run <created-path>``. Pass ``--root``
consistently when a workflow owns a different local artifact root.

## Used by

Every artifact-producing workflow skill and portable workflow-helper tests use this creator. Downstream commands depend
on its stdout path rather than recomputing timestamps or assuming a shell-specific date utility.

## Outputs

Creation prints a UTC timestamp path beneath ``.reports/codex/<skill>/`` unless an explicit root is supplied. Promotion
prints ``<root>/code-review/pr-<number>/run-<NNN>`` using the next positive numeric index with at least three digits.
The directory is moved as one filesystem entry, retaining every collected artifact.

## Failure

Invalid skill IDs and unwritable paths exit non-zero. Promotion also fails closed for an unsupported skill, an unsafe or
misplaced source, missing or invalid PR identity, or malformed sequence siblings. Atomic destination collisions are
rescanned and retried without overwriting an accepted run.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

#: Pattern for a valid skill id: lowercase words joined by single hyphens.
SKILL_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
#: Pattern for a timestamp-named run directory, which is the only kind eligible for promotion.
TIMESTAMP_RUN_ID = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{6}Z\Z")
#: Pattern for a promoted `run-NNN` directory name; the capture group is its sequence number.
PROMOTED_RUN_ID = re.compile(r"run-(\d{3,})\Z")
#: Number of attempts to claim the next `run-NNN` name before promotion gives up.
PROMOTION_RETRIES = 100


class RunPromotionError(RuntimeError):
    """Report a fail-closed PR run-promotion contract violation."""


def parse_args() -> argparse.Namespace:
    """Parse the run-directory creation contract."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill", required=True, help="Canonical lowercase skill identifier")
    parser.add_argument("--root", type=Path, default=Path(".reports") / "codex", help="Artifact root")
    parser.add_argument(
        "--promote-pr-run",
        type=Path,
        metavar="PATH",
        help="Atomically move one newly created code-review run into its PR-specific sequence",
    )
    arguments = parser.parse_args()
    if SKILL_ID.fullmatch(arguments.skill) is None:
        parser.error("invalid skill id")
    return arguments


def _read_pr_number(source: Path) -> int:
    """Read the positive integer PR identity collected inside a review run."""
    identity_path = source / "pr.json"
    if identity_path.is_symlink() or not identity_path.is_file():
        raise RunPromotionError("promotion source has no authoritative pr.json")
    try:
        payload = json.loads(identity_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RunPromotionError("promotion source has invalid authoritative pr.json") from error
    number = payload.get("number") if isinstance(payload, dict) else None
    if type(number) is not int or number <= 0:
        raise RunPromotionError("pr.json number must be a positive integer")
    return number


def _next_run_index(pr_directory: Path) -> int:
    """Validate existing promoted runs and return their numeric maximum plus one."""
    indexes: list[int] = []
    for sibling in pr_directory.iterdir():
        if not sibling.name.startswith("run-"):
            continue
        match = PROMOTED_RUN_ID.fullmatch(sibling.name)
        if match is None or int(match.group(1)) < 1:
            raise RunPromotionError(f"malformed promoted run sibling: {sibling.name}")
        if sibling.is_symlink() or not sibling.is_dir():
            raise RunPromotionError(f"promoted run sibling is not a directory: {sibling.name}")
        indexes.append(int(match.group(1)))
    return max(indexes, default=0) + 1


def promote_pr_run(root: Path, skill: str, source: Path) -> Path:
    """Move a collected review run into its authoritative PR-specific sequence."""
    if skill != "code-review":
        raise RunPromotionError("PR run promotion is supported only for code-review")

    skill_directory = root / skill
    normalized_skill = Path(os.path.abspath(skill_directory))
    normalized_source = Path(os.path.abspath(source))
    if normalized_source.parent != normalized_skill:
        raise RunPromotionError("promotion source must be a direct child of the skill root")
    if TIMESTAMP_RUN_ID.fullmatch(source.name) is None:
        raise RunPromotionError("promotion source is not a newly created timestamp run")
    if source.is_symlink() or not source.is_dir():
        raise RunPromotionError("promotion source must be a safe directory")

    pr_number = _read_pr_number(source)
    pr_directory = skill_directory / f"pr-{pr_number}"
    pr_directory.mkdir(parents=True, exist_ok=True)
    if pr_directory.is_symlink() or not pr_directory.is_dir():
        raise RunPromotionError("PR promotion target must be a safe directory")

    for _ in range(PROMOTION_RETRIES):
        index = _next_run_index(pr_directory)
        destination = pr_directory / f"run-{index:03d}"
        try:
            source.rename(destination)
        except OSError as error:
            if isinstance(error, FileExistsError) or error.errno in {errno.EEXIST, errno.ENOTEMPTY}:
                continue
            raise
        return destination
    raise RunPromotionError("PR run destination remained contested")


def main() -> int:
    """Create or promote the directory and print its path for later arguments."""
    arguments = parse_args()
    if arguments.promote_pr_run is not None:
        try:
            run_directory = promote_pr_run(arguments.root, arguments.skill, arguments.promote_pr_run)
        except RunPromotionError as error:
            print(error, file=sys.stderr)
            return 1
        print(run_directory)
        return 0

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-%fZ")
    run_directory = arguments.root / arguments.skill / timestamp
    run_directory.mkdir(parents=True, exist_ok=False)
    print(run_directory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

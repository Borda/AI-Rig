#!/usr/bin/env python3
"""Create or promote one portable artifact directory for a Codex Rig workflow.

## Purpose

Give each workflow invocation an isolated, predictable evidence location without requiring shell-specific timestamp
handling. The generated directory is the handoff point that later helpers use for all run-scoped evidence and results. A
collected PR review or remediation can subsequently promote that complete directory into a stable PR-specific sequence.

## Scope

It validates a canonical skill identifier and either creates one local directory or promotes one direct child produced
by ``code-review`` or ``code-remediate``. Promotion reads the authoritative PR number from ``pr.json`` for review or
``pr/pr.json`` for remediation and moves the whole run atomically. It neither changes evidence files nor starts a
workflow.

## Usage

Run ``python create_run.py --skill <canonical-skill-id> [--pr-number <number> | --branch <name>]`` once.
Use the printed path literally in later commands.
After PR collection, run it again with the same skill and ``--promote-pr-run <created-path>``. Pass ``--root``
consistently when a workflow owns a different local artifact root; resumed runs retain their existing paths.

## Used by

Every artifact-producing workflow skill and portable workflow-helper tests use this creator. Downstream commands depend
on its stdout path rather than recomputing timestamps or assuming a shell-specific date utility.

## Outputs

Creation prints a UTC timestamp with optional PR or portable branch identity and a randomized suffix beneath
``.reports/codex/<skill>/`` unless an explicit root is supplied. Promotion prints
``<root>/<skill>/pr-<number>/run-<NNN>`` using the next positive numeric index with at least three digits.
The directory is moved as one filesystem entry, retaining every collected artifact.

## Failure

Invalid skill IDs and unwritable paths exit non-zero. Promotion also fails closed for an unsupported skill, an unsafe or
misplaced source, missing or invalid PR identity, or malformed sequence siblings. Atomic destination collisions are
rescanned and retried without overwriting an accepted run. Hidden claim directories coordinate concurrent promoters; an
interrupted claim reserves its index permanently rather than risking another invocation's ownership.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

#: Pattern for a valid skill id: lowercase words joined by single hyphens.
SKILL_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
#: Pattern for a timestamp-named run directory, which is the only kind eligible for promotion.
TIMESTAMP_RUN_ID = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{6}Z"
    r"(?:-(?:pr-(?P<pr_number>[1-9]\d*)|branch-[a-z0-9]+(?:-[a-z0-9]+)*-[a-f0-9]{12}))?"
    r"(?:-[a-z0-9_]+)?\Z"
)
#: Pattern for a promoted `run-NNN` directory name; the capture group is its sequence number.
PROMOTED_RUN_ID = re.compile(r"run-(\d{3,})\Z")
#: Hidden exclusive claims reserve sequence numbers until an atomic promotion completes.
PROMOTION_CLAIM_ID = re.compile(r"\.run-(\d{3,})\.claim\Z")
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
        help="Atomically move one newly created code-review or code-remediate run into its PR-specific sequence",
    )
    identity = parser.add_mutually_exclusive_group()
    identity.add_argument("--pr-number", type=int, help="Known positive PR number for the initial run name")
    identity.add_argument("--branch", help="Local branch identity, encoded as a bounded portable slug and digest")
    arguments = parser.parse_args()
    if arguments.pr_number is not None and arguments.pr_number <= 0:
        parser.error("PR number must be a positive integer")
    if arguments.branch is not None and not arguments.branch.strip():
        parser.error("branch must be nonempty")
    if arguments.promote_pr_run is not None and (arguments.pr_number is not None or arguments.branch is not None):
        parser.error("creation identity cannot be supplied during promotion")
    if SKILL_ID.fullmatch(arguments.skill) is None:
        parser.error("invalid skill id")
    return arguments


def _read_pr_number(source: Path) -> int:
    """Read the positive integer PR identity from its owning collection directory."""
    identity_path = source / "pr.json"
    if source.is_symlink() or identity_path.is_symlink() or not identity_path.is_file():
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
        claim_match = PROMOTION_CLAIM_ID.fullmatch(sibling.name)
        if not sibling.name.startswith("run-") and claim_match is None:
            continue
        match = claim_match or PROMOTED_RUN_ID.fullmatch(sibling.name)
        if match is None or int(match.group(1)) < 1:
            raise RunPromotionError(f"malformed promoted run sibling: {sibling.name}")
        if sibling.is_symlink() or not sibling.is_dir():
            # Another promoter can release a claim after this directory listing was captured.
            if claim_match is not None and not sibling.exists():
                continue
            raise RunPromotionError(f"promoted run sibling is not a directory: {sibling.name}")
        indexes.append(int(match.group(1)))
    return max(indexes, default=0) + 1


def promote_pr_run(root: Path, skill: str, source: Path) -> Path:
    """Move a collected review or remediation run into its authoritative PR-specific sequence."""
    if skill not in {"code-review", "code-remediate"}:
        raise RunPromotionError("PR run promotion is supported only for code-review and code-remediate")

    skill_directory = root / skill
    normalized_skill = Path(os.path.abspath(skill_directory))
    normalized_source = Path(os.path.abspath(source))
    if normalized_source.parent != normalized_skill:
        raise RunPromotionError("promotion source must be a direct child of the skill root")
    source_identity = TIMESTAMP_RUN_ID.fullmatch(source.name)
    if source_identity is None:
        raise RunPromotionError("promotion source is not a newly created timestamp run")
    if source.is_symlink() or not source.is_dir():
        raise RunPromotionError("promotion source must be a safe directory")

    pr_number = _read_pr_number(source / "pr" if skill == "code-remediate" else source)
    named_number = source_identity.group("pr_number")
    if named_number is not None and int(named_number) != pr_number:
        raise RunPromotionError("initial PR run identity does not match authoritative pr.json")
    pr_directory = skill_directory / f"pr-{pr_number}"
    pr_directory.mkdir(parents=True, exist_ok=True)
    if pr_directory.is_symlink() or not pr_directory.is_dir():
        raise RunPromotionError("PR promotion target must be a safe directory")

    for _ in range(PROMOTION_RETRIES):
        index = _next_run_index(pr_directory)
        destination = pr_directory / f"run-{index:03d}"
        claim = pr_directory / f".run-{index:03d}.claim"
        try:
            claim.mkdir()
        except FileExistsError:
            continue
        try:
            # Reserve beside the destination: Windows cannot rename over even an empty directory.
            # A stale claim keeps its index occupied; never remove another invocation's claim.
            if destination.exists() or destination.is_symlink():
                continue
            try:
                source.rename(destination)
            except OSError as error:
                if isinstance(error, FileExistsError) or error.errno in {errno.EEXIST, errno.ENOTEMPTY}:
                    continue
                raise
            return destination
        finally:
            claim.rmdir()
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
    skill_directory = arguments.root / arguments.skill
    skill_directory.mkdir(parents=True, exist_ok=True)
    identity = ""
    if arguments.pr_number is not None:
        identity = f"-pr-{arguments.pr_number}"
    elif arguments.branch is not None:
        # Hash original UTF-8 text: distinct branches may normalize to the same portable slug.
        slug = re.sub(r"[^a-z0-9]+", "-", arguments.branch.lower()).strip("-")[:40].rstrip("-") or "branch"
        digest = hashlib.sha256(arguments.branch.encode("utf-8")).hexdigest()[:12]
        identity = f"-branch-{slug}-{digest}"
    run_directory = Path(tempfile.mkdtemp(prefix=f"{timestamp}{identity}-", dir=skill_directory))
    print(run_directory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

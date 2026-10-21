#!/usr/bin/env python
"""revert_iteration_commit.py — revert HEAD only when it is this iteration's experiment commit.

The research:run optimisation loop can try to revert the same iteration twice: Phase 5 reverts after a metric
timeout, and Phase 7 may then revert the same iteration again (rework exhausted, no improvement, or a tiny gain on a
large change). The second ``git revert HEAD`` would undo the first revert and silently re-apply the rejected change.
This script is the single guard both phases call.

The match is anchored at the start of the commit subject, never a substring search. A revert commit's subject embeds
the original (``Revert "experiment(optimize/i3): ..."``), so an unanchored search would see a reverted commit as still
being the iteration's experiment commit and revert the revert. Iteration ``1`` must not match ``i10``: the closing
``):`` of the prefix is part of the literal compared against.

A ``HEAD`` that is an experiment commit of a *different* iteration is refused, not skipped. Phase 4 types the
iteration number into the commit subject while ``--iteration`` comes from the Phase 0 sentinel; when the two disagree,
the commit under test sits at ``HEAD`` and a silent skip would keep a change the loop decided to revert. Every path
that reaches this guard runs right after this iteration's Phase 4 commit (a failed or empty commit skips Phases 5–7),
so the mismatch can only be that typo — stopping is the only safe answer.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/revert_iteration_commit.py" --iteration "$I" --phase 5|7

Exit codes:
    0   reverted, or HEAD is no experiment commit at all (already reverted — message printed, nothing changed)
    1   ``--iteration`` empty, or HEAD is another iteration's experiment commit (``! BLOCKED`` message printed,
        nothing changed), or ``git revert`` failed (its own status is returned)
    2   argument error (missing or invalid ``--phase``, ``git`` not on PATH)

The phase only selects the wording of the skip message, which names what the caller expects to have happened
already: Phase 7 expects Phase 5 to have reverted; Phase 5 expects an earlier revert this iteration.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys

#: Subject prefix of an iteration's experiment commit; ``{iteration}`` is substituted before comparison.
_SUBJECT_PREFIX = "experiment(optimize/i{iteration}):"
#: Matches the subject of any iteration's experiment commit and captures its iteration number.
_ANY_EXPERIMENT = re.compile(r"^experiment\(optimize/i(\d+)\):")
#: Seconds allowed for the read-only ``git log`` probe; ``git revert`` itself is bounded by the caller's Bash timeout.
_LOG_TIMEOUT_S = 10
#: Per-phase tail of the skip message, naming what the caller expects an earlier step to have done.
_SKIP_TAIL = {
    "5": "already reverted; skipping double-revert.",
    "7": "Phase 5 already reverted; skipping double-revert.",
}


def head_subject(git: str) -> str | None:
    """Return the subject line of ``HEAD``, or ``None`` when the history cannot be read.

    Any failure to read ``HEAD`` (not a repository, no commits, ``git`` timing out) yields ``None``: the guard's job is
    to avoid a wrong revert, so an unreadable history never authorises one.

    Args:
        git: Absolute path of the ``git`` executable.

    Returns:
        The subject, decoded with replacement characters for non-UTF-8 bytes, or ``None``.
    """
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, git resolved via shutil.which
            [git, "log", "-1", "--format=%s"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",  # a non-UTF-8 subject must not raise; the compared prefix is ASCII
            check=False,
            timeout=_LOG_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def head_is_iteration_commit(git: str, iteration: str) -> bool:
    """Return whether the subject of ``HEAD`` starts with this iteration's experiment-commit prefix.

    An unreadable history counts as "not this commit" (see :func:`head_subject`).

    Args:
        git: Absolute path of the ``git`` executable.
        iteration: Iteration number as the loop spells it.

    Returns:
        ``True`` only when ``HEAD``'s subject starts with ``experiment(optimize/i<iteration>):``.
    """
    subject = head_subject(git)
    return subject is not None and subject.startswith(_SUBJECT_PREFIX.format(iteration=iteration))


def other_iteration(subject: str | None, iteration: str) -> str | None:
    """Return the iteration number of an experiment commit subject that belongs to a different iteration.

    Args:
        subject: ``HEAD``'s subject, or ``None`` when unreadable.
        iteration: The iteration the caller is reverting.

    Returns:
        The other iteration's number as written in the subject, or ``None`` when the subject is unreadable, is no
        experiment commit (a revert, a baseline), or is this iteration's own commit.

    Examples:
        >>> other_iteration("experiment(optimize/i5): try x", "6")
        '5'
        >>> other_iteration("experiment(optimize/i6): try x", "6") is None
        True
        >>> other_iteration('Revert "experiment(optimize/i6): try x"', "6") is None
        True
    """
    found = _ANY_EXPERIMENT.match(subject or "")
    if found is None or found.group(1) == iteration:
        return None
    return found.group(1)


def main(argv: list[str] | None = None) -> int:
    """Revert ``HEAD`` when it is this iteration's experiment commit, else report the skip.

    Args:
        argv: Optional argument list (tests); defaults to ``sys.argv[1:]``.

    Returns:
        Process exit status; see the module docstring for the codes.
    """
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    parser = argparse.ArgumentParser(
        prog="revert_iteration_commit",
        description="Revert HEAD only when it is this iteration's experiment commit.",
    )
    parser.add_argument("--iteration", required=True, help="Iteration number; empty is a BLOCKED error, not a skip.")
    parser.add_argument("--phase", required=True, choices=sorted(_SKIP_TAIL), help="Calling phase (5 or 7).")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    if not args.iteration:
        print(f"! BLOCKED — Phase {args.phase}: iteration number unset, cannot scope revert guard")
        return 1
    git = shutil.which("git")
    if git is None:
        print("revert_iteration_commit: git not on PATH", file=sys.stderr)
        return 2
    subject = head_subject(git)
    if subject is not None and subject.startswith(_SUBJECT_PREFIX.format(iteration=args.iteration)):
        return subprocess.run([git, "revert", "HEAD", "--no-edit"], check=False).returncode  # noqa: S603
    stray = other_iteration(subject, args.iteration)
    if stray is not None:
        print(
            f"! BLOCKED — Phase {args.phase}: HEAD is iteration {stray}'s experiment commit, but this is iteration "
            f"{args.iteration} — the Phase 4 subject and the Phase 0 iteration sentinel disagree; nothing reverted. "
            f"Check `git log -1` first: if HEAD is this iteration's change, amend its subject to "
            f"`experiment(optimize/i{args.iteration}): …` (git commit --amend) and re-run this step; if it is an earlier "
            f"kept commit, Phase 4 never committed — do not amend."
        )
        return 1
    print(f"Phase {args.phase}: HEAD is not iteration {args.iteration}'s experiment commit — {_SKIP_TAIL[args.phase]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

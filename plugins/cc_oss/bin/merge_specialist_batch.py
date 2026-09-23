#!/usr/bin/env python
"""merge_specialist_batch.py — cherry-pick per-item commits from parallel specialist worktrees onto the current branch,
in original item-priority order.

oss:resolve Step 8 Phase 2 dispatches one implementation agent per specialist
(sw-engineer/qa-specialist/doc-scribe/linting-expert), each working in its own
``git worktree`` so concurrent edits never race on a shared working tree. Each
specialist commits its assigned action items individually inside its own
worktree branch. This script brings those commits back onto the real PR branch
one at a time, in the caller-supplied priority order (interleaved across
specialists, not grouped by specialist), so history order matches the
review's severity ranking regardless of which specialist finished first.

For ``--commit-mode`` other than ``each``, every entry still lands as a real
commit during the loop (an in-progress soft-reset would leave the index
non-clean, and Git refuses the *next* cherry-pick against a non-clean index
even when the two entries touch disjoint files — confirmed empirically,
``git cherry-pick`` exits 128 with "your local changes would be overwritten"
before it even attempts the merge). Once every entry in the plan has applied
cleanly, one combined ``git reset --soft HEAD~<n>`` (``n`` = entries applied)
collapses them into a single staged diff — the "stage first, commit-mode-
specific commit later" contract that action-item-dispatch.md's post-loop
COMMIT_MODE=grouped/all/stage sections already assume. A conflict mid-plan
leaves every already-applied entry as a real, uncollapsed commit — never
reset — so the repository state matches ``git cherry-pick``'s ordinary
partial-progress state exactly, with no reset in flight to reason about.

Usage:
    merge_specialist_batch.py --plan <plan.json> --commit-mode <each|grouped|all|stage>
        [--centrality-file <map.json>]

Plan file: JSON array of objects containing ``item_id``, ``sha``, and optional
``group`` and ``module`` fields, in the exact order to apply.

With ``--centrality-file`` (a ``{module: score}`` JSON map) the plan is first
reordered so the most foundational worktree groups land first — see
``order_plan``. Groups touch disjoint files (oss:resolve's file-ownership
tiebreak guarantees it), so reordering whole groups never adds a textual
conflict; commit order *within* a group is always preserved.

Exit codes:
    0 — all entries applied cleanly
    1 — cherry-pick conflict on an entry; partial progress reported as JSON
        on stdout, repo left in the conflicted cherry-pick state for the
        caller to resolve (mirrors Step 5 merge-conflict handling), then
        re-invoke with the remaining (unapplied) entries once resolved
    2 — bad/missing required argument (argparse default), or a plan entry's
        ``sha`` fails ``_SHA_RE`` (argument-injection guard: JSON error on
        stdout, nothing cherry-picked)

Security:
    ``--plan``/``--centrality-file`` are trusted to come from ``oss:resolve`` Step 8
    (``skills/resolve/modes/action-item-dispatch.md``, the ``PLAN_FILE``/``CENTRALITY_FILE``
    variables). Both are read-only JSON reads and are not validated here — a future caller passing
    externally-influenced paths must validate upstream. Contrast with each plan entry's ``sha``,
    which *is* validated: it reaches ``git cherry-pick`` argv and is checked against ``_SHA_RE``
    before use, exiting 2 on a mismatch (see :func:`parse_plan`).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from shutil import which


class CommitMode(str, Enum):
    """How ``oss:resolve`` turns the cherry-picked action items into commits.

    Subclasses ``str`` (not ``enum.StrEnum``) because ``requires-python`` is ``>=3.10``. Only ``EACH`` keeps one commit
    per action item; every other mode stages the diff and lets a later step decide the commit shape.
    """

    EACH = "each"
    GROUPED = "grouped"
    ALL = "all"
    STAGE = "stage"


# git argv guard: a sha reaches `git cherry-pick <sha>` unquoted, so a value
# starting with '-' would be parsed as an option (e.g. --strategy=evil can
# execute an arbitrary git-<name> from PATH). {7,64} covers both sha1 (7-40)
# and sha256 (up to 64) short/full forms.
_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")


@dataclass(frozen=True)
class PlanEntry:
    """One cherry-pick unit — a single action item's commit from a specialist worktree.

    Attributes:
        item_id: Review action-item id (matches ``SELECTED_ITEMS`` entries).
        sha: Commit SHA to cherry-pick, reachable via the specialist worktree's branch.
        group: Worktree-group tag this commit came from (one linear commit chain per
            group). Empty when the caller supplies no grouping — the whole plan is then
            treated as a single group and left in the order given.
        module: Dotted module path this item edits, used to look up a centrality score
            for group ordering. Empty when unknown (non-Python item, or no index).
    """

    item_id: str
    sha: str
    group: str = ""
    module: str = ""


def _resolve(cmd: str) -> str:
    """Resolve a CLI tool to its absolute path.

    Args:
        cmd: Bare executable name (e.g. ``"git"``).

    Returns:
        Absolute path to the executable.

    Raises:
        FileNotFoundError: If ``cmd`` is not present on ``PATH``.

    Examples:
        >>> import shutil
        >>> _resolve("git") == shutil.which("git")
        True
    """
    p = which(cmd)
    if p is None:
        raise FileNotFoundError(f"executable not found on PATH: {cmd}")
    return p


def parse_plan(raw: str) -> list[PlanEntry]:
    """Parse a plan JSON string into ordered ``PlanEntry`` objects.

    Args:
        raw: JSON array text, e.g. ``'[{"item_id":"6","sha":"abc1234"}]'``.

    Returns:
        Ordered list of ``PlanEntry``, preserving input order.

    Raises:
        ValueError: When an entry's ``sha`` fails ``_SHA_RE`` — the value later
            reaches ``git cherry-pick`` argv, so a malformed sha is hard-failed
            here rather than silently skipped (skipping would silently drop a
            specialist's committed work).

    Examples:
        >>> parse_plan('[{"item_id": "3", "sha": "abc1234"}]')
        [PlanEntry(item_id='3', sha='abc1234', group='', module='')]
        >>> parse_plan('[{"item_id": "3", "sha": "abc1234", "group": "sw", "module": "pkg.a"}]')
        [PlanEntry(item_id='3', sha='abc1234', group='sw', module='pkg.a')]
    """
    entries = []
    for e in json.loads(raw):
        sha = str(e["sha"]).strip()
        if not _SHA_RE.match(sha):
            raise ValueError(f"invalid sha for item {e.get('item_id')!r}: {sha!r}")
        entries.append(
            PlanEntry(
                item_id=str(e["item_id"]),
                sha=sha,
                group=str(e.get("group", "")),
                module=str(e.get("module", "")),
            )
        )
    return entries


def order_plan(entries: list[PlanEntry], centrality: dict[str, float]) -> list[PlanEntry]:
    """Reorder the cherry-pick plan so the most foundational worktree chains land first.

    Ordering is done at **group** granularity, never at the individual-commit level: a
    specialist worktree is a linear commit chain whose commits may build on one another,
    so reordering commits *within* a group risks a cherry-pick conflict or a broken
    intermediate state. Whole groups are safe to reorder relative to each other because
    oss:resolve's file-ownership tiebreak guarantees any single file's items all live in
    one group — distinct groups therefore touch disjoint files and can never textually
    conflict across the reordering.

    Each group's sort weight is the maximum centrality of any module it edits (its most
    depended-upon change). Groups are sorted by that weight descending; ties and unscored
    groups keep first-seen order (stable). Within every group the original commit order is
    preserved untouched.

    Args:
        entries: The plan in caller-supplied (priority) order.
        centrality: Map of dotted module path to centrality score. Missing modules score
            ``0.0``. An empty map leaves the plan order unchanged (every group scores 0).

    Returns:
        The reordered plan. Same ``PlanEntry`` objects, regrouped; never mutated.

    Examples:
        >>> plan = [
        ...     PlanEntry("1", "aa", group="docs", module="pkg.readme"),
        ...     PlanEntry("2", "bb", group="sw", module="pkg.core"),
        ...     PlanEntry("3", "cc", group="sw", module="pkg.util"),
        ... ]
        >>> [e.item_id for e in order_plan(plan, {"pkg.core": 9.0, "pkg.readme": 1.0})]
        ['2', '3', '1']
        >>> [e.item_id for e in order_plan(plan, {})]
        ['1', '2', '3']
    """
    groups: dict[str, list[PlanEntry]] = {}
    for e in entries:
        groups.setdefault(e.group, []).append(e)
    ordered_groups = sorted(
        groups,
        key=lambda g: -max((centrality.get(e.module, 0.0) for e in groups[g]), default=0.0),
    )
    return [e for g in ordered_groups for e in groups[g]]


def _conflicted_files(git: str) -> list[str]:
    """List files left with unmerged conflict markers by the current git operation.

    Args:
        git: Absolute path to the ``git`` executable.

    Returns:
        Conflicted file paths (``git diff --diff-filter=U``), empty if none.
    """
    proc = subprocess.run(  # noqa: S603
        [git, "diff", "--name-only", "--diff-filter=U"],
        capture_output=True,
        text=True,
        check=False,
        timeout=3,
    )
    return [f for f in proc.stdout.splitlines() if f.strip()]


def run_plan(entries: list[PlanEntry], commit_mode: CommitMode, base_sha: str | None = None) -> dict[str, object]:
    """Cherry-pick each plan entry in order, then collapse the whole plan with one combined reset in non-``each`` mode.

    Stops at the first cherry-pick conflict and leaves the repository in that
    conflicted state (matching how Step 5's merge-conflict handling expects to
    find in-progress state) so the caller can resolve it and re-invoke with
    the remaining entries.

    Args:
        entries: Ordered cherry-pick plan.
        commit_mode: A ``CommitMode`` member. Any mode other than
            ``CommitMode.EACH`` collapses every applied entry into one
            staged diff via a single combined soft-reset, once the whole
            plan has landed cleanly — never per entry (see module docstring
            for why a per-entry reset breaks the next cherry-pick).
        base_sha: When given, the combined reset targets this fixed commit
            instead of ``HEAD~<entries applied this call>``, and fires even when
            THIS call's own ``entries`` is empty (a resumed call whose last
            remaining entry conflicted on its first attempt applies nothing —
            gating on ``applied`` would leave the run's earlier commits
            permanently uncollapsed, the one call that could ever fix that
            refusing to). Required for a correct **resumed** call after a
            conflict: a resumed call's ``entries`` only holds the *remaining*
            plan, so ``HEAD~len(applied)`` would collapse only the entries
            picked in *this* call and leave every entry applied before the
            conflict (plus the one resolved via ``--continue``) as permanent
            real commits — the caller's original branch tip before *any* pass
            is the one fixed point every pass (first or resumed, whether or
            not it applies anything) can reset to correctly.

    Returns:
        A mapping with ``applied``, ``conflict``, and ``remaining`` fields.

    Examples:
        No doctest — requires live git subprocess; covered by pytest with monkeypatch.
    """
    git = _resolve("git")
    applied: list[str] = []
    for i, entry in enumerate(entries):
        pick = subprocess.run(  # noqa: S603
            [git, "cherry-pick", "--end-of-options", entry.sha], check=False, timeout=30
        )
        if pick.returncode != 0:
            return {
                "applied": applied,
                "conflict": {
                    "item_id": entry.item_id,
                    "sha": entry.sha,
                    "files": _conflicted_files(git),
                },
                "remaining": [e.item_id for e in entries[i + 1 :]],
            }
        applied.append(entry.item_id)
    if commit_mode != CommitMode.EACH:
        if base_sha:
            # Unconditional on `applied` here — a call that cherry-picks ZERO entries (the very last
            # plan entry conflicted on its first attempt, so `remaining` comes back empty and this
            # call's own `applied` is []) still needs to collapse whatever earlier calls in the SAME
            # run left as real commits on top of `base_sha`. Gating on `applied` (as the HEAD~n
            # fallback below still must) left that one call the only one that could ever collapse the
            # run refusing to, stranding real commits in `stage` mode with no recovery route.
            subprocess.run([git, "reset", "--soft", "--end-of-options", base_sha], check=False, timeout=3)  # noqa: S603
        elif applied:
            subprocess.run(  # noqa: S603
                [git, "reset", "--soft", "--end-of-options", f"HEAD~{len(applied)}"], check=False, timeout=3
            )
    return {"applied": applied, "conflict": None, "remaining": []}


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Exit code: 0 — all entries applied; 1 — conflict, partial JSON on stdout;
        2 — a plan entry's sha fails validation, JSON error on stdout.

    Examples:
        No doctest — requires live git; covered by pytest with monkeypatch.
    """
    parser = argparse.ArgumentParser(
        prog="merge_specialist_batch.py",
        description="Cherry-pick per-item commits from parallel specialist worktrees, in priority order.",
    )
    parser.add_argument("--plan", required=True, help="Path to plan JSON file: [{item_id, sha, group?, module?}, ...]")
    parser.add_argument("--commit-mode", required=True, choices=[m.value for m in CommitMode])
    parser.add_argument(
        "--centrality-file",
        default=None,
        help="Optional JSON map {module: score}; reorders whole worktree groups most-central-first.",
    )
    parser.add_argument(
        "--base-sha",
        default=None,
        help=(
            "Branch tip before the FIRST call of this merge session (first pass or resumed). "
            "Required for a correct combined reset on a resumed call after a conflict — see "
            "run_plan's base_sha docstring. Same validation as a plan entry's sha."
        ),
    )
    args = parser.parse_args(argv)

    sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]
    with open(args.plan, encoding="utf-8") as f:
        raw_plan = f.read()
    try:
        entries = parse_plan(raw_plan)
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}))
        return 2

    if args.base_sha is not None and not _SHA_RE.match(args.base_sha):
        print(json.dumps({"error": f"invalid --base-sha: {args.base_sha!r}"}))
        return 2

    if args.centrality_file:
        with open(args.centrality_file, encoding="utf-8") as f:
            centrality = {str(k): float(v) for k, v in json.load(f).items()}
        entries = order_plan(entries, centrality)

    result = run_plan(entries, CommitMode(args.commit_mode), base_sha=args.base_sha)
    print(json.dumps(result))
    return 0 if result["conflict"] is None else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python
"""build_merge_plan.py — assemble the ``merge_specialist_batch.py --plan`` file for oss:resolve Step 8 Phase 3.

Phase 2 dispatches one implementation agent per specialist worktree; each returns a JSON envelope
(``{"commits":[{"item_id":N,"sha":"..."}], "skipped":[...]}``). The orchestrator appends each group's
``commits`` entries — tagged with that group's worktree tag — to ``$IMPL_DIR/phase2-commits.jsonl`` as
it parses each envelope. This script turns that append-only ledger into the single ordered plan file
``merge_specialist_batch.py`` expects: one entry per landed item, in original review-priority order
(interleaved across specialist groups by item id, never grouped by specialist), each carrying its
worktree ``group`` tag and its canonical codemap ``module`` name.

Priority order and codemap module resolution both come from files Step 8 already writes
(``selected-items.txt``, ``action-items.jsonl``, ``codemap-maps.json``) — this script never re-derives
either; it only joins them. An item present in ``priority-order`` but absent from the commits ledger
(rejected in Phase 1, or skipped by its Phase 2 agent) is silently omitted — that is its correct
terminal state, tracked elsewhere (``challenge-log.txt`` / ``skipped-items.txt``), not a plan entry.

Usage:
    build_merge_plan.py --commits phase2-commits.jsonl --action-items action-items.jsonl \\
        --priority-order "3 1 2" --out merge-plan.json [--codemap-maps codemap-maps.json]

Exit codes:
    0 — plan written (possibly empty array, when no item in priority-order ever committed)
    1 — a required input file is missing or fails to parse as JSON/JSONL
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _read_jsonl(path: str) -> list[dict]:
    """Read a JSON-Lines file into a list of decoded objects, skipping blank lines.

    Args:
        path: Path to a ``.jsonl`` file, one JSON object per line.

    Returns:
        Decoded objects in file order.

    Examples:
        >>> import tempfile, os
        >>> fd, p = tempfile.mkstemp()
        >>> _ = os.write(fd, b'{"a": 1}\\n\\n{"a": 2}\\n')
        >>> os.close(fd)
        >>> _read_jsonl(p) == [{"a": 1}, {"a": 2}]
        True
        >>> os.unlink(p)
    """
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def build_plan(
    commits: list[dict],
    action_items: list[dict],
    file_module: dict[str, str],
    priority_order: list[str],
) -> list[dict]:
    """Assemble the ordered cherry-pick plan from Phase 2's commit ledger.

    Args:
        commits: Decoded ``phase2-commits.jsonl`` rows — each ``{"item_id", "sha", "group"}``.
        action_items: Decoded ``action-items.jsonl`` rows — each carries ``"id"`` and ``"file"``,
            used to resolve an item's codemap module.
        file_module: ``{file: module}`` map from ``codemap-maps.json``'s ``file_module`` — empty
            when no codemap index was available (every item's module then resolves to ``""``).
        priority_order: Item ids in original review-priority order (``SELECTED_ITEMS``), as strings.

    Returns:
        One entry per priority-order id that has a commit, in original priority order with any
        repeated id de-duplicated to its first occurrence — each ``{"item_id", "sha", "group",
        "module"}``. An id with no matching commit (rejected or skipped) is omitted, never emitted
        with an empty ``sha``.

    Raises:
        ValueError: A ``commits`` row is missing ``item_id``/``sha``, an ``action_items`` row is
            missing ``id``, or two ``commits`` rows share the same ``item_id`` — the last case would
            otherwise silently drop one group's commit from the plan while the item still reads as
            "landed" everywhere else (the all-mode close-out flips its task regardless).

    Examples:
        >>> commits = [{"item_id": 2, "sha": "bb", "group": "sw"}, {"item_id": 1, "sha": "aa", "group": "docs"}]
        >>> items = [{"id": 1, "file": "readme.md"}, {"id": 2, "file": "core.py"}]
        >>> plan = build_plan(commits, items, {"core.py": "pkg.core"}, ["1", "2"])
        >>> [e["item_id"] for e in plan]
        ['1', '2']
        >>> plan[1]["module"]
        'pkg.core'
        >>> build_plan(commits, items, {}, ["1", "3"])  # id 3 never committed — omitted
        [{'item_id': '1', 'sha': 'aa', 'group': 'docs', 'module': ''}]
        >>> dupe_plan = build_plan(commits, items, {}, ["1", "1", "2"])  # repeated priority-order id
        >>> [e["item_id"] for e in dupe_plan]  # de-duplicated, not emitted twice
        ['1', '2']
        >>> build_plan([{"item_id": 1, "sha": "aa"}, {"item_id": 1, "sha": "bb"}], items, {}, ["1"])
        Traceback (most recent call last):
            ...
        ValueError: duplicate item_id in commits ledger: '1'
    """
    by_id: dict[str, dict] = {}
    for c in commits:
        if "item_id" not in c or "sha" not in c:
            raise ValueError(f"commits row missing item_id/sha: {c!r}")
        cid = str(c["item_id"])
        if cid in by_id:
            raise ValueError(f"duplicate item_id in commits ledger: {cid!r}")
        by_id[cid] = c
    item_file: dict[str, str] = {}
    for a in action_items:
        if "id" not in a:
            raise ValueError(f"action-items row missing id: {a!r}")
        item_file[str(a["id"])] = a.get("file", "")
    plan = []
    seen: set[str] = set()
    for item_id in priority_order:
        if item_id in seen:
            continue
        seen.add(item_id)
        commit = by_id.get(item_id)
        if commit is None:
            continue
        module = file_module.get(item_file.get(item_id, ""), "")
        plan.append({"item_id": item_id, "sha": commit["sha"], "group": commit.get("group", ""), "module": module})
    return plan


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Exit code: 0 on success (plan written, possibly empty); 1 on missing/malformed input, or a
        malformed ledger/action-items row (duplicate item_id, missing sha/id).

    Examples:
        No doctest — filesystem-dependent; covered by pytest.
    """
    parser = argparse.ArgumentParser(
        prog="build_merge_plan.py",
        description="Assemble the merge_specialist_batch.py --plan file from Phase 2's commit ledger.",
    )
    parser.add_argument("--commits", required=True, help="Path to phase2-commits.jsonl")
    parser.add_argument("--action-items", required=True, help="Path to action-items.jsonl")
    parser.add_argument("--priority-order", required=True, help="Space-separated item ids, priority order")
    parser.add_argument("--out", required=True, help="Path to write the assembled plan JSON array")
    parser.add_argument("--codemap-maps", default=None, help="Optional path to codemap-maps.json")
    args = parser.parse_args(argv)

    try:
        commits = _read_jsonl(args.commits) if Path(args.commits).is_file() else []
        action_items = _read_jsonl(args.action_items) if Path(args.action_items).is_file() else []
    except (OSError, json.JSONDecodeError) as exc:
        print(f"build_merge_plan: failed to read input: {exc}", file=sys.stderr)
        return 1

    # --codemap-maps is optional and best-effort: Structural prep creates it unconditionally and
    # re-empties it to 0 bytes on any codemap-py query failure, so an empty-but-present file is the
    # NORMAL state for any run without codemap-py installed — not a malformed-input error. Degrading
    # to an empty map (every module resolves to "") on ANY read/parse problem, rather than folding
    # this into the hard-fail block above, is what keeps Phase 3 runnable for that majority case.
    file_module: dict[str, str] = {}
    if args.codemap_maps and Path(args.codemap_maps).is_file():
        try:
            raw = json.loads(Path(args.codemap_maps).read_text(encoding="utf-8"))
            fm = raw.get("file_module", {}) if isinstance(raw, dict) else {}
            file_module = fm if isinstance(fm, dict) else {}
        except (OSError, json.JSONDecodeError):
            file_module = {}

    priority_order = args.priority_order.split()
    try:
        plan = build_plan(commits, action_items, file_module, priority_order)
    except ValueError as exc:
        print(f"build_merge_plan: {exc}", file=sys.stderr)
        return 1
    # A commits-ledger row whose item_id never appears in priority_order (a stale row from an
    # earlier, differently-scoped run reusing the same IMPL_DIR, or an id typo) has no other path
    # to the plan — build_plan only ever iterates priority_order, so such a row is silently dropped
    # with no trace. Surface it rather than let it vanish unremarked.
    committed_ids = {str(c["item_id"]) for c in commits if "item_id" in c}
    dropped = sorted(committed_ids - set(priority_order))
    if dropped:
        print(f"build_merge_plan: commit ledger row(s) outside priority-order, dropped: {dropped}", file=sys.stderr)
    Path(args.out).write_text(json.dumps(plan), encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

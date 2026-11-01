#!/usr/bin/env python3
"""Hold always-loaded instruction text to a pinned byte budget.

Claude Code re-reads every always-loaded rule file on every turn, the Codex global template on every Codex turn, and a
skill file on every invocation, so each added byte is a recurring token cost across all sessions. Nothing stops working
when a budget is exceeded, which is why this is a hook and not a pytest: it only keeps growth deliberate. Raise a limit
in ``BUDGETS`` only in the same change that justifies the added always-on text, and say why a lazy home does not fit;
lower it when text moves out, so the next addition is judged against the real total rather than slack.

Two budget kinds:

``file``
    One file's byte size must stay at or below its limit.
``eager-rules``
    The summed size of every top-level ``*.md`` rule in a directory that Claude Code loads at session start (no
    ``paths`` frontmatter, or a glob that matches every file) must stay at or below its limit. Rules behind a narrower
    ``paths`` glob load on first touch of a matching file and do not count. The directory must hold at least
    ``MIN_RULE_FILES`` rules, or the scan resolved to the wrong place and would pass vacuously.

Usage: ``check_instruction_budget.py [--root DIR]``. Exit 1 on any violation, one ``path: message`` line each.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

#: Globs in a rule's ``paths`` frontmatter that match every file, which makes the rule load at session start.
EAGER_GLOBS = frozenset({"**", "**/*"})
#: A rules directory holding fewer top-level rule files than this was mis-resolved.
MIN_RULE_FILES = 10
_FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)


@dataclass(frozen=True)
class Budget:
    """One byte budget: a file, or the eager rule set of a directory."""

    path: str
    max_bytes: int
    kind: str = "file"


#: Budgets keyed by repository-relative path. Each history comment states why the limit moved; add yours beside it.
BUDGETS = (
    # Total bytes of the eager foundry rule files.
    # Raised from 58597: commit-message format bullets stay eager in claude-config.md because
    # rule-inject.js context arrives beside the first git call's result, after that message
    # was drafted; stub/detail pairs gained policy-sibling markers. Raised again from 59315:
    # default-branch and history-safety bullets stay eager because `reset --hard` never
    # triggers injection and `rebase` injects only after it ran. Raised again from 59455: the eager
    # commit-authority ban names the local Git grant as its third source, or it would contradict the
    # injected git-commit.md before that rule arrives. Raised again from 59464: the gh-write approval
    # question (public-github.md) must be seen before the first gh write, which no hook injects a
    # rule for, and untrusted-content.md states that free gh reads stay data.
    # Raised from 59990 by 995 bytes: the gh-write question now lists approval scope and expiry,
    # with the body summary and exact command visible before approval. It must remain eager
    # because no hook injects this rule before the first write; a lazy home misses that decision.
    # Raised from 60985 by 161 bytes: the adversarial-loop progress table format (exact columns,
    # `old + new` cells) now sits in the quality-gates stub. Defined only in the lazily read full
    # rule, it was replaced by a reduced ad-hoc table in a live loop report.
    # Raised from 61146 by 66 bytes: a one-line pointer to `_full/quality-gates.md` §Challenger Rounds,
    # which holds the procedure itself. The challenger brief is written before the first Agent() call,
    # and rule-inject.js injects agent-spawn.md only after that call ran, so a lazy home misses it.
    # Raised from 61212 by 11 bytes: the progress table header in the quality-gates stub gains its
    # `Security` column; the stub states the exact columns, so leaving it out reintroduces the
    # seven-column table in live loop reports.
    # Raised from 61223 by 44 bytes: the stub's recurrence clause names "a repeated invariant (same
    # signature or same invariant evidence)". Already eager, it said "repeated signature" alone, which
    # contradicts the full rule's per-invariant count; dropping it would leave no eager recurrence trigger.
    # Raised by 22 bytes: the existing eager subject rule must permit omitting a redundant scope;
    # the injected full rule arrives only after the first Git call, potentially after drafting.
    # Raised by 707 bytes: confidence and its gate must assess the delivered result in every workflow,
    # including early handoffs, so a lazily loaded rule cannot prevent status-score substitution.
    # Raised by 581 net bytes: proportional implementation and controlled-first validation must be known
    # before planning, editing, or external dispatch; activity-triggered rules arrive after those decisions.
    Budget("plugins/cc_foundry/rules", 62577, kind="eager-rules"),
    # Byte ratchet for the Codex query skill, which is re-sent on every invocation. The bound is
    # re-baselined only when mandatory content lands, never to make a red check green:
    #   2500 -> 3600: four required disclosures the previous bound predates — the resolved
    #   `PLUGIN_ROOT/bin/codemap-py query` command (the file previously named an undefined
    #   `$CODEMAP_BIN`), a Runtime note, the 20-item result cap with `--limit 0`/`index.confidence`,
    #   the partial-routing-table pointer to `--help`, and the test-impact subcommand-vs-skill split.
    # The bound was already exceeded at 2518 bytes before that work, so it is not a fresh regression.
    # 3600 -> 4100: mandatory independent-read/no-self-heal execution, retained launcher
    # identity, targeted-help routing, distinct-fact settlement, and bounded correction
    # retries. Preserve prior index-root/symbol/runtime safety details; never trim them
    # merely to fit the old bound. Live token savings still require matched measurements.
    # 4100 -> 4600: the scoped-ranking route (`central --among`) and the rule that counts and
    # scoped rankings come from the query rather than from hand work on its output. Every
    # measured wrong answer this skill's arm produced in the agentic benchmark was assembly
    # over correct tool output — a list counted by eye, one call subtracted from another, a
    # repository-wide ranking intersected against a candidate set — so the routing row and the
    # prohibition are the correction, not commentary. The prose was compressed first; the
    # addition is 475 bytes and the bound moves by the smallest round step that holds it.
    # 4600 -> 5000: required exact-module and measurement-availability disclosure. Retain
    # the established routing contract; compressing its mandatory literals broke parity.
    # 5000 -> 5200: one routing row for gap-finding and inventory queries (`coverage-gap`,
    # `undocumented`, `packages`, `list`). Five days of telemetry on two active repositories
    # showed these four at 1-8 calls against 379 total while agents answered the same
    # questions by hand; the Claude table already carried the row, the Codex one did not.
    # 5200 -> 5250: a protocol (dunder) method's zero-caller `hint` names no search, because Python
    # calls it implicitly; read as "the method hint's grep came back empty", that licensed deleting a
    # method in active use. The clause was compressed to 42 bytes against 5 bytes of headroom.
    # 5250 -> 5711: mandatory automatic preparation, frozen missing-index handling and canonical/custom root selection
    # add exactly 461 bytes over the committed 5237-byte skill; preserve the existing 13-byte headroom.
    Budget("plugins/codemap-py/codex-skills/query-code/SKILL.md", 5711),
    # Codex reads the whole global template on every turn, so each byte costs context on all of them. Activity-specific
    # detail belongs in ``shared/global-baseline-details.md``, loaded only when the template's trigger line names it.
    # Raise this number deliberately, in the same change that justifies the added always-loaded text, never to make a
    # failing run green. Raised by 217 bytes for the one ``PLUGIN_ROOT`` line the installer renders with the absolute
    # installed root: without it no ``shared/<file>`` pointer resolves from a session outside this repository. Raised
    # by 966 bytes for the local Git approval grant lines in Commit Authorization: commit authority is one of the rules
    # that must stay always-loaded, since a session that never reads the shared contract would otherwise commit or
    # self-grant without knowing the grant exists.
    # Raised by 324 bytes for the after-wait notification checkpoint: ordinary sessions also wait for agents without
    # loading a skill contract, so pending roles and empty-return evidence limits must stay visible in the global rule.
    # Raised by 464 bytes for the user-required table/path display rule: it applies to all terminal messages, including
    # sessions that load no workflow skill, so a skill-local pointer cannot establish the requested default.
    # Raised by 420 bytes for the dependency-preparation trigger: skills and roles can report blocked checks before
    # loading the shared workflow contract, so the parent-owned preparation rule must remain always-loaded.
    # Raised by 14 bytes for root-cause and model-neutral dispatch principles that apply even without a skill.
    # Raised by 534 bytes: confidence must describe the delivered result and stay separate from completion even before a
    # workflow skill loads, so a lazy skill-local rule cannot govern early status handoffs.
    # Raised by 391 bytes for the user-requested proportional execution, controlled-first validation and accurate
    # confidence reporting instructions. These govern ordinary sessions before any skill loads; an independent
    # followability audit rejected moving the dependency and resume gates out of the template.
    # Raised by 570 bytes for the user-required universal lifecycle: ordinary sessions without a skill must plan and
    # challenge, accept within existing authority, assign execution, and consolidate verified delivery without creating
    # an automatic user approval gate. These pre-skill obligations must remain always-loaded.
    Budget("plugins/codex-rig/assets/AGENTS.md", 40951),
)


def _frontmatter(text: str) -> str | None:
    """Return the frontmatter body of ``text``, or ``None`` when the file has none.

    Examples:
        >>> _frontmatter("---\\npaths: ['**']\\n---\\nbody")
        "paths: ['**']"
        >>> _frontmatter("no frontmatter") is None
        True
    """
    match = _FRONTMATTER.match(text)
    return match.group(1) if match else None


def _paths(front: str) -> list[str] | None:
    """Parse the ``paths`` key of a rule frontmatter block: a block list of ``- 'glob'`` items or an inline list.

    Returns:
        The globs, or ``None`` when the key is absent.

    Examples:
        >>> _paths("description: x\\npaths:\\n  - '**'\\n  - docs/**")
        ['**', 'docs/**']
        >>> _paths("paths: ['**/*.py']")
        ['**/*.py']
        >>> _paths("description: x") is None
        True
    """
    lines = front.splitlines()
    for index, line in enumerate(lines):
        if not line.startswith("paths:"):
            continue
        inline = line.split(":", 1)[1].strip()
        if inline.startswith("["):
            items = inline.strip("[]").split(",")
        else:
            items = []
            for follow in lines[index + 1 :]:
                stripped = follow.strip()
                if not stripped.startswith("- "):
                    break
                items.append(stripped[2:])
        return [item.strip().strip("'\"") for item in items if item.strip()]
    return None


def is_eager(text: str) -> bool:
    """Report whether a rule loads at session start: no ``paths``, or any glob that matches every file.

    Examples:
        >>> is_eager("---\\ndescription: x\\n---\\nbody")
        True
        >>> is_eager("---\\npaths:\\n  - '**/*.md'\\n---\\nbody")
        False
        >>> is_eager("---\\npaths: ['**']\\n---\\nbody")
        True
    """
    front = _frontmatter(text)
    globs = _paths(front) if front is not None else None
    return globs is None or any(glob in EAGER_GLOBS for glob in globs)


def _check_file(root: Path, budget: Budget) -> list[str]:
    """Return a violation line when the file is missing or over its limit."""
    path = root / budget.path
    if not path.is_file():
        return [f"{budget.path}: budgeted file not found; update BUDGETS in scripts/check_instruction_budget.py"]
    size = path.stat().st_size
    if size <= budget.max_bytes:
        return []
    return [
        f"{budget.path}: {size} bytes, over the {budget.max_bytes}-byte budget; move activity-specific text into a "
        "lazily loaded file, or raise the limit in scripts/check_instruction_budget.py with a stated reason"
    ]


def _check_eager_rules(root: Path, budget: Budget) -> list[str]:
    """Return violation lines for a rules directory that is missing, too small to trust, or over its eager total."""
    directory = root / budget.path
    rules = sorted(directory.glob("*.md")) if directory.is_dir() else []
    if len(rules) < MIN_RULE_FILES:
        return [
            f"{budget.path}: only {len(rules)} rule files found, expected at least {MIN_RULE_FILES}; "
            "the budget scan resolved to the wrong directory"
        ]
    sizes = {rule.name: rule.stat().st_size for rule in rules if is_eager(rule.read_text(encoding="utf-8"))}
    total = sum(sizes.values())
    if total <= budget.max_bytes:
        return []
    largest = ", ".join(f"{name} {size}" for name, size in sorted(sizes.items(), key=lambda item: -item[1])[:5])
    return [
        f"{budget.path}: always-loaded rules total {total} bytes, over the {budget.max_bytes}-byte budget "
        f"(largest: {largest}); move the new text behind a narrower `paths` glob or into `_full/` with a trigger "
        "line, or raise the limit in scripts/check_instruction_budget.py with a stated reason"
    ]


def check(root: Path, budgets: tuple[Budget, ...] = BUDGETS) -> list[str]:
    """Return one ``path: message`` line per budget violation under ``root``.

    Args:
        root: Repository root the budget paths are relative to.
        budgets: Budgets to enforce; defaults to the repository table.

    Returns:
        Violation lines; empty when every budget holds.
    """
    findings: list[str] = []
    for budget in budgets:
        checker = _check_eager_rules if budget.kind == "eager-rules" else _check_file
        findings.extend(checker(root, budget))
    return findings


def main(argv: list[str] | None = None) -> int:
    """Print every violation and return 1 when any exists."""
    parser = argparse.ArgumentParser(description="Hold always-loaded instruction text to a pinned byte budget.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1], help="repository root")
    args = parser.parse_args(argv)
    findings = check(args.root)
    for finding in findings:
        print(finding)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())

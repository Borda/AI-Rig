#!/usr/bin/env python3
"""Hold hand-synced policy text identical across the files that restate it.

Several policy paragraphs and rule sets are restated by hand in more than one shipped file, with no propagation entry
because the surrounding files differ wholly: the reviewer-attribution block in the two review plugins, the rating-legend
sentence, the four ``quality-gates.md`` rule sets, the local Git grant-scope paragraph across the Codex copies, the
routing content-scan patterns of the two overlay hosts, the shared vitality scoring rules, and the five delivery
principles every host surface states. A change to one copy leaves the others stating the old policy with nothing to
notice.

This is a hook and not a test because a failure only means a copy drifted: no behaviour a user or hook depends on is at
risk, so a pytest failure would block fix rounds without protecting anything. Phrase pins that guard what an instruction
actually says stay in pytest.

The groups live in :data:`IDENTICAL` (every file yields the same text) plus two set-comparison checks. Usage:
``check_policy_parity.py [--root DIR]``. Output is one ``path[:line]: message`` line per violation; exit 1 on any.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

REVIEW_PLUGINS = ("cc_oss", "cc_develop")
SKILL_MARKER = "> Review presentation is additive:"
CONSOLIDATOR_MARKER = "**Reviewer attribution (additive):**"

#: The legend is quoted in Markdown, Python and JSON, so the sentence is matched up to its terminating period.
LEGEND_PATTERN = re.compile(r"Legend: 1 = Approve[^\n]*?(?:Reject|Block)\.")
HISTORICAL_LEGEND = (
    "Legend: 1 = Approve · 2 = Minor changes · 3 = Changes required · 4 = Insufficient evidence · 5 = Block / Reject."
)
CURRENT_LEGEND = "Legend: 1 = Approve · 2 = Minor changes · 3 = Major revision · 4 = Insufficient, but a good initiative · 5 = Block."
LEGEND_SUFFIXES = frozenset({".md", ".py", ".json"})
#: Directories never scanned for the legend: dependency trees and caches hold no policy of ours.
_SKIPPED_DIRS = frozenset({"node_modules", "__pycache__"})

GRANT_START = "Task-scoped local Git work other than a plain commit runs without an authorization question"
GRANT_END = "it does not require inventing Git work or committing unrelated changes."

#: Anchors of the five delivery principles every shipped host surface states verbatim. Both occur exactly once in
#: each of the three surfaces, because ``span()`` resolves the first occurrence and a duplicated anchor would
#: silently extract the wrong region. The canonical text is ``plugins/codex-rig/shared/delivery-principles.md``.
DELIVERY_PRINCIPLES_START = "- **Consistent, simple delivery.**"
DELIVERY_PRINCIPLES_END = "Unowned dirty working-tree changes belong to someone else."

QUALITY_PLUGINS = ("cc_foundry", "cc_develop", "cc_oss", "cc_research")
QUALITY_RULE = "quality-gates.md"
QUALITY_DELTA = "quality-gates-delta.md"
QUALITY_SATELLITES = tuple(plugin for plugin in QUALITY_PLUGINS if plugin != "cc_foundry")
#: ``Output Routing`` is one prose paragraph in foundry and nested labelled bullets in the siblings, so the names
#: differ without any rule differing. Re-examine this exemption if that section is restructured to a common shape.
QUALITY_EXEMPT_SECTIONS = frozenset({"output routing"})
#: Fewer shared sections than this means heading detection broke and the comparison went vacuous.
QUALITY_MIN_SHARED_SECTIONS = 5
#: Sentence in foundry's stub that makes the full Proportional Execution section bind before planning.
PROPORTIONAL_TRIGGER = (
    "Before planning or implementation, including small parent-owned tasks, resolve + Read "
    "`_full/quality-gates.md` §Proportional Execution; its full policy binds as if inline."
)
_BOUND_QUALITY_SECTION = re.compile(
    r"^>\s*Before applying(?: this section)?,? (?:resolve \+ )?Read `_full/quality-gates\.md` §(.+?)"
    r"(?:\. Every instruction there is binding as if inline here\.|; binding inline\.)"
)
_NAMED_RULE = re.compile(r"^\s*(?:[-*]|\d+\.)\s+\*\*(.+?)\*\*")
_LIST_ITEM = re.compile(r"^(?:>\s*)?-\s")
_ROUTING_COMMAND = re.compile(r"`(git grep -n -I -i -E [^`]+)`")
_ROUTING_PATTERN = re.compile(r"-e '([^']+)'")

VITALITY_FILES = (
    "plugins/cc_oss/skills/_shared/vitality-scoring.md",
    "plugins/cc_oss/skills/_shared/vitality-scoring-group-a.md",
    "plugins/cc_oss/skills/_shared/vitality-scoring-group-b.md",
    "plugins/cc_oss/skills/_shared/vitality-scoring-group-c.md",
    "plugins/cc_oss/agents/repo-warden.md",
)


class ParityError(Exception):
    """A file lacks the text a group compares, so the group cannot be checked."""


@dataclass(frozen=True)
class Found:
    """The comparable text one file contributes to a group, with the 1-based line it starts on."""

    text: str
    line: int


Extractor = Callable[[str], Found]


def block(marker: str) -> Extractor:
    """Build an extractor for the lead-in line starting with ``marker`` plus the bullet list that follows it.

    The policy is a lead-in line followed by a bullet list, optionally inside a blockquote, so comparing only the lead-
    in would leave every bullet unguarded.
    """

    def extract(text: str) -> Found:
        """Return the block of ``text`` opened by the marker; the marker line must occur exactly once."""
        lines = [line.rstrip() for line in text.splitlines()]
        starts = [index for index, line in enumerate(lines) if line.startswith(marker)]
        if len(starts) != 1:
            raise ParityError(f"expected exactly one line starting {marker!r}, found {len(starts)}")
        collected = [lines[starts[0]]]
        rest = lines[starts[0] + 1 :]
        while rest and rest[0] in {"", ">"}:
            rest = rest[1:]
        while rest and _LIST_ITEM.match(rest[0]):
            collected.append(rest.pop(0))
        return Found("\n".join(collected), starts[0] + 1)

    return extract


def span(start: str, end: str) -> Extractor:
    """Build an extractor for the text from the first ``start`` through the first ``end`` after it, anchors included."""

    def extract(text: str) -> Found:
        """Return the anchored span of ``text``; both anchors must be present."""
        begin = text.find(start)
        if begin < 0:
            raise ParityError(f"anchor not found: {start!r}")
        finish = text.find(end, begin)
        if finish < 0:
            raise ParityError(f"end anchor not found after the start anchor: {end!r}")
        return Found(text[begin : finish + len(end)], text.count("\n", 0, begin) + 1)

    return extract


def rule_line(marker: str) -> Extractor:
    """Build an extractor for the single line stating the ``**marker**`` rule, cut from the marker to end of line."""
    token = f"**{marker}**"

    def extract(text: str) -> Found:
        """Return the rule line of ``text``; exactly one line may state the rule."""
        hits = [(number, line) for number, line in enumerate(text.splitlines(), 1) if token in line]
        if len(hits) != 1:
            raise ParityError(f"expected the {token} rule stated on exactly one line, found {len(hits)}")
        number, line = hits[0]
        return Found(line[line.index(token) :], number)

    return extract


def routing_patterns() -> Extractor:
    """Build an extractor for the ``-e`` patterns of the routing content-scan ``git grep`` command a host prescribes."""

    def extract(text: str) -> Found:
        """Return the scan patterns, one per line; the file must embed the command."""
        command = _ROUTING_COMMAND.search(text)
        if command is None:
            raise ParityError("no routing content-scan `git grep -n -I -i -E ...` command found")
        patterns = _ROUTING_PATTERN.findall(command.group(1))
        return Found("\n".join(patterns), text.count("\n", 0, command.start()) + 1)

    return extract


@dataclass(frozen=True)
class Identical:
    """A group whose files must all yield the same text from ``extract``."""

    name: str
    files: tuple[str, ...]
    extract: Extractor
    fix: str


#: Declarative roster: add a row to hold another restated paragraph identical.
IDENTICAL = (
    Identical(
        "review attribution (skill)",
        tuple(f"plugins/{plugin}/skills/review/SKILL.md" for plugin in REVIEW_PLUGINS),
        block(SKILL_MARKER),
        "make the reviewer-attribution block of each review SKILL.md read the same",
    ),
    Identical(
        "review attribution (consolidator)",
        tuple(f"plugins/{plugin}/skills/review/templates/consolidator-prompt.md" for plugin in REVIEW_PLUGINS),
        block(CONSOLIDATOR_MARKER),
        "make the reviewer-attribution block of each consolidator prompt read the same",
    ),
    Identical(
        "local Git grant-scope paragraph",
        (
            ".codex/config.toml",
            ".codex/README.md",
            "plugins/codex-rig/README.md",
            "plugins/codex-rig/shared/native-skill-contract.md",
        ),
        span(GRANT_START, GRANT_END),
        "copy the paragraph byte-for-byte from plugins/codex-rig/shared/native-skill-contract.md",
    ),
    Identical(
        "overlay routing scan",
        (
            "plugins/cc_oss/skills/review/templates/agent-prompts.md",
            "plugins/codex-rig/shared/native-skill-contract.md",
        ),
        routing_patterns(),
        "give both hosts the same `git grep` -e patterns",
    ),
    Identical(
        "delivery principles block",
        (
            "plugins/codex-rig/assets/AGENTS.md",
            "plugins/cc_foundry/CLAUDE.src.md",
            "plugins/opencode-rig/src/resources/references/delivery-principles.md",
        ),
        span(DELIVERY_PRINCIPLES_START, DELIVERY_PRINCIPLES_END),
        "copy the principles block byte-for-byte from plugins/codex-rig/shared/delivery-principles.md",
    ),
    *(
        Identical(
            f"vitality rule: {marker}",
            VITALITY_FILES,
            rule_line(marker),
            "copy the rule line from plugins/cc_oss/skills/_shared/vitality-scoring.md",
        )
        for marker in ("Confidence formula", "Band rule", "Copy rule")
    ),
)


def _read(root: Path, relative: str) -> str:
    """Return the UTF-8 text of ``relative`` under ``root``, or raise :class:`ParityError` when it is missing."""
    path = root / relative
    if not path.is_file():
        raise ParityError("file is missing, so the group it belongs to cannot be compared")
    return path.read_text(encoding="utf-8")


def _first_difference(reference: str, other: str) -> str:
    """Describe the first line where ``other`` departs from ``reference``, each side cut to 80 characters."""
    ours, theirs = reference.splitlines(), other.splitlines()
    for index in range(max(len(ours), len(theirs))):
        left = ours[index] if index < len(ours) else "<missing>"
        right = theirs[index] if index < len(theirs) else "<missing>"
        if left != right:
            return f"first difference at text line {index + 1}: {left[:80]!r} vs {right[:80]!r}"
    return "texts differ"


def check_identical(root: Path, group: Identical) -> list[str]:
    """Return one violation per file of ``group`` whose text differs from the first file's, or lacks the text."""
    found: dict[str, Found] = {}
    violations = []
    for relative in group.files:
        try:
            found[relative] = group.extract(_read(root, relative))
        except ParityError as error:
            violations.append(f"{relative}: [{group.name}] {error}")
    if not found:
        return violations
    reference_path = next(iter(found))
    reference = found[reference_path]
    violations.extend(
        f"{relative}:{item.line}: [{group.name}] differs from {reference_path}:{reference.line} "
        f"({_first_difference(reference.text, item.text)}); {group.fix}"
        for relative, item in found.items()
        if item.text != reference.text
    )
    return violations


def _legend_sentences(root: Path) -> dict[str, list[str]]:
    """Return every rating-legend sentence under ``plugins/`` mapped to the ``path:line`` places stating it."""
    found: dict[str, list[str]] = {}
    for directory, subdirs, names in os.walk(root / "plugins"):
        subdirs[:] = sorted(name for name in subdirs if name not in _SKIPPED_DIRS)
        for name in sorted(names):
            path = Path(directory) / name
            if path.suffix not in LEGEND_SUFFIXES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for match in LEGEND_PATTERN.finditer(text):
                place = f"{path.relative_to(root).as_posix()}:{text.count(chr(10), 0, match.start()) + 1}"
                found.setdefault(match.group(0), []).append(place)
    return found


def check_legend(root: Path) -> list[str]:
    """Return a violation per rating-legend wording that is neither the historical nor the current sentence.

    Archived reviews keep the historical wording and every active template uses the current one, so exactly those two
    sentences may exist anywhere under ``plugins/``; a legend stated nowhere means the feature was renamed or removed.
    """
    found = _legend_sentences(root)
    allowed = {HISTORICAL_LEGEND, CURRENT_LEGEND}
    if not found:
        return ["plugins: [rating legend] stated nowhere; the feature was removed or renamed, update LEGEND_PATTERN"]
    violations = [
        f"{place}: [rating legend] unknown wording {sentence!r}; use the historical or the current legend sentence"
        for sentence, places in sorted(found.items())
        if sentence not in allowed
        for place in places
    ]
    violations.extend(
        f"plugins: [rating legend] the {label} wording is stated nowhere; restore it or update the allowed pair"
        for label, sentence in (("historical", HISTORICAL_LEGEND), ("current", CURRENT_LEGEND))
        if sentence not in found
    )
    return violations


def sections(text: str) -> dict[str, str]:
    """Split a rule file into its ``## `` sections, ignoring headings inside fenced blocks.

    A heading that recurs has its bodies concatenated rather than overwritten: a delta repeats its full file's
    review-loop heading, and dropping the earlier body would exempt every rule stated there from the comparison.
    """
    chunks: list[tuple[str, list[str]]] = []
    fenced = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        if not fenced and line.startswith("## "):
            chunks.append((line[3:].strip(), []))
        elif chunks:
            chunks[-1][1].append(line)
    merged: dict[str, str] = {}
    for name, lines in chunks:
        body = "\n".join(lines).strip()
        merged[name] = f"{merged[name]}\n\n{body}" if name in merged else body
    return merged


def section_key(heading: str) -> str:
    """Normalise a heading so one section matches across copies: text before the first parenthesis, lowercased."""
    return heading.split("(")[0].strip().lower()


def named_rules(body: str) -> set[str]:
    """Return the lowercased bold labels of the obligations a section states, one per list item."""
    matches = (_NAMED_RULE.match(line) for line in body.splitlines())
    return {match.group(1).strip().rstrip(":").lower() for match in matches if match}


def _delivered_text(root: Path, plugin: str) -> str:
    """Return one plugin's rule text with foundry's bound full sections and any delta appended.

    Raise :class:`ParityError` when a bound full section is missing.
    """
    rule = f"plugins/{plugin}/rules/{QUALITY_RULE}"
    text = _read(root, rule)
    if plugin == "cc_foundry":
        stub = sections(text)
        bound = {
            match.group(1)
            for body in stub.values()
            for line in body.splitlines()
            if (match := _BOUND_QUALITY_SECTION.match(line))
        }
        if PROPORTIONAL_TRIGGER in stub.get("Proportional Execution", ""):
            bound.add("Proportional Execution")
        if bound:
            full_rule = f"plugins/{plugin}/rules/_full/{QUALITY_RULE}"
            full = sections(_read(root, full_rule))
            for heading in sorted(bound):
                if heading not in full:
                    raise ParityError(f"bound section §{heading} not found in {full_rule}")
                text = f"{text}\n## {heading}\n\n{full[heading]}\n"
    delta = root / f"plugins/{plugin}/rules/{QUALITY_DELTA}"
    return f"{text}\n{delta.read_text(encoding='utf-8')}" if delta.is_file() else text


def check_quality_gates(root: Path) -> list[str]:
    """Return violations where the four ``quality-gates.md`` copies state different named rules, or a delta adds one.

    The comparison is by rule *name* (the bold list-item label), not bytes: the satellites are compressed rewrites that
    reshape prose into lists, so what must not differ is the set of rules. A rule added to one copy alone changes that
    set. A delta abbreviates delivery only, so it may not state a rule its own full file lacks.
    """
    violations = []
    delivered: dict[str, str] = {}
    for plugin in QUALITY_PLUGINS:
        try:
            delivered[plugin] = _delivered_text(root, plugin)
        except ParityError as error:
            violations.append(f"plugins/{plugin}/rules/{QUALITY_RULE}: [quality gates] {error}")
    shared: dict[str, dict[str, set[str]]] = {}
    for plugin, text in delivered.items():
        for heading, body in sections(text).items():
            shared.setdefault(section_key(heading), {})[plugin] = named_rules(body)
    shared = {key: copies for key, copies in shared.items() if len(copies) > 1}
    for section in sorted(set(shared) - QUALITY_EXEMPT_SECTIONS):
        copies = shared[section]
        everywhere = set.intersection(*copies.values())
        for rule in sorted(set.union(*copies.values()) - everywhere):
            holders = sorted(plugin for plugin, rules in copies.items() if rule in rules)
            violations.append(
                f"plugins/{holders[0]}/rules/{QUALITY_RULE}: [quality gates] §{section} states {rule!r} only in "
                f"{holders}; propagate it to every plugin in {sorted(copies)}, or exempt the section with a reason"
            )
    violations.extend(
        f"plugins: [quality gates] exempt section {section!r} is no longer shared between copies; drop the exemption"
        for section in sorted(QUALITY_EXEMPT_SECTIONS - set(shared))
    )
    if len(set(shared) - QUALITY_EXEMPT_SECTIONS) < QUALITY_MIN_SHARED_SECTIONS:
        violations.append(
            f"plugins: [quality gates] only {len(set(shared) - QUALITY_EXEMPT_SECTIONS)} shared sections parsed "
            f"(need {QUALITY_MIN_SHARED_SECTIONS}); heading detection regressed and the comparison went vacuous"
        )
    for plugin in QUALITY_SATELLITES:
        violations.extend(_delta_violations(root, plugin))
    return violations


def _delta_violations(root: Path, plugin: str) -> list[str]:
    """Return a violation per named rule the plugin's delta states that its full rule file lacks."""
    rule_path = f"plugins/{plugin}/rules/{QUALITY_RULE}"
    delta_path = f"plugins/{plugin}/rules/{QUALITY_DELTA}"
    try:
        full = named_rules(_read(root, rule_path))
    except ParityError as error:
        return [f"{rule_path}: [quality gates] {error}"]
    try:
        delta = named_rules(_read(root, delta_path))
    except ParityError as error:
        return [f"{delta_path}: [quality gates] {error}"]
    return [
        f"{delta_path}: [quality gates] states {rule!r} absent from {QUALITY_RULE}; add it to the full file first, "
        "the delta is only delivered when foundry's copy is installed"
        for rule in sorted(delta - full)
    ]


CHECKS: tuple[Callable[[Path], list[str]], ...] = (
    *(lambda root, group=group: check_identical(root, group) for group in IDENTICAL),
    check_legend,
    check_quality_gates,
)


def main(argv: list[str] | None = None) -> int:
    """Run every group and print violations; exit 1 when any exist."""
    parser = argparse.ArgumentParser(description="Hold hand-synced policy text identical across its copies.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1], help="repository root")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    violations = [violation for check in CHECKS for violation in check(args.root)]
    for violation in violations:
        print(violation)
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())

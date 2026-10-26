#!/usr/bin/env python
"""size_importance.py — flag instruction text whose size outruns its importance, for /foundry:audit --efficiency (E11).

Instruction text costs tokens on every load. The spine (a workflow step, a gate, a deliverable's contract) is
complete first and short second; the complement (rationale, examples, measurement detail, rare-path caveats) should
be small, and moving it behind a load boundary beats shrinking it. This script reads the section inventory of
``consolidate_inventory.py`` (rules, skills, agents; the load class of each section is already there), splits every
section into blocks, and judges each block on two independent axes. Zero model tokens.

Importance, decided by evidence in the text. Doubtful means spine, so a block is complement only on positive proof:

1. High-stakes words (force push, credential, security, injection, untrusted, settings.json, delete) → spine.
2. The block's opening clause (up to the first ``;`` or sentence end) states an opt-in gate as its own condition and
   neither negates it before it ("not only", "never skipped, only with") nor reverses it right after the flag
   ("only if `--x` is not set", "only with `--x` it rewrites", "each run still writes") → complement, *gated*: the
   unit it sits in loads whole, so the block is paid for on runs that never use it. The phrase must sit inside the
   block's label or heading, or within 80 characters after it with no negation and no second bold span between.
   Accepted shapes, nothing looser: ``(opt-in)``, ``(opt-in `--x`)``, ``(only with|when|if `--x`)``, ``is opt-in``,
   ``— opt-in``, and ``only with|when|if `--x``` after a comma, colon, dash or the label's closing bold
   (``**Phase B3**: three steps, only with `--measured```, ``**Phase B3.** Runs only with `--measured```). A bare
   mention ("list every opt-in flag") and a prerequisite ("requires `--repo`") are not a gate. A gated block counts
   only what its gate scopes. A heading-led block counts its section (conditional as a whole) up to a level-5 or 6
   heading, without the unit that loads a mode file. Otherwise: the lead paragraph up to a sentence that says it
   applies on every run ("on every run", "regardless of flags"), and the paragraphs that open with the lead label's
   step id (``B3a`` under ``Phase B3``) with the fences that follow them. A fence that follows the lead directly (the
   loader or guard that stays behind) and every other paragraph are spine that happens to share the block; their
   tokens are not counted.
3. The lead opens with a rule (never, always, must, only, do not), also after a bold label → spine.
4. Structural or deliverable-contract words (envelope, ``Return ONLY``, deadline, AskUserQuestion, gate, a
   ``summary.jsonl``-style file, an inline JSON object) → spine.
5. A fenced block, which is an executable step → spine.
6. The label is, as a whole, a rationale term (rationale, why, example, worked example, precedent, incident,
   background, history, known limits, catalogue, appendix) — a bold label, a heading, or the text before the first
   colon or dash — and the block holds no obligation, no sentence that opens with an instruction verb and is not an
   ordered-list item → complement.
7. Anything else → spine.

Action by quadrant (load class from the inventory: ``always`` / ``scoped`` / ``lazy``):

``move``
    A complement block in always or scoped text, or the proven part of a gated block anywhere, of at least
    ``--min-tokens``: put it behind a load boundary (a flag-loaded mode file, ``_full/``) and leave a one-line
    pointer.
``trim``
    A complement block in lazy text, ungated, whose tokens per obligation exceed ``--factor`` times the median of
    complement blocks of its load class.

Doubt means spine, and spine is not flagged for size; that is a property of the keyword proofs above, not a guarantee
about every wording. Whether a spine block is too thin is not computable here; the ``--consolidate`` challenger pass
owns that. A mode or template file of one skill, or a file under ``skills/_shared``, made mostly of gated blocks
(counted whole) is itself the gated unit and is not flagged; a rule, skill body or agent never is.

Obligations are a proxy: sentences with a normative word (must, never, always, only, shall, required, forbidden, do
not, exactly, at most, stop, skip, refuse) plus one per fenced block, at least one per block. Token estimate is
``bytes / 3`` (``plugins/CLAUDE.md`` §Length Unit Convention). Lines and fences follow the inventory's own reader.

Known limits: blocks are bold-lead paragraphs and top-level list items, so a long unbolded paragraph is one block; a
block's role comes from keywords, not meaning, so a gate written in another shape is missed and an instruction phrased
without an imperative opener under a rationale label can still read as rationale; byte-identical blocks in shared
copies are reported once with every location listed; a section whose file is unreadable or lies outside ``--root``
is counted and skipped.

Usage:
    size_importance.py [--root <repo>] [--inventory <file>] [--min-tokens N] [--factor X] [--json <file>]

Exit codes:
    0   a completed run — findings are in the output, not the status
    1   the inventory file is missing, not valid JSON, or an item lacks its file, line range or load class
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import sys
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from itertools import takewhile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# The inventory's own line and fence readers (private by name, same plugin): one reader keeps line numbers in step.
from consolidate_inventory import (
    _FENCE,
    ALL_KINDS,
    _closes_fence,
    _lines,
    build_inventory,
    estimate_tokens,
)

#: Output schema version.
SCHEMA_VERSION = 1

#: Smallest block worth moving, in tokens; below it the saving is noise.
MIN_TOKENS = 400

#: A lazy complement block is trimmed when its tokens per obligation exceed this multiple of its class median.
FACTOR = 2.0

#: Share of a file's tokens in gated blocks above which the file is itself the gated unit and is not flagged.
OWN_GATE_SHARE = 0.7

#: Skill subfolders whose files load one at a time, behind the flag or step that names them (``skills/_shared`` too).
OWN_GATE_DIRS = ("modes", "templates")

#: Characters of a block's opening line taken as its lead.
LEAD_CHARS = 400

#: Words that make a block spine whatever else it says: a miss costs data, secrets or trust.
HIGH_STAKES = re.compile(
    r"force[- ]push|\bcredential|\bsecret\b|\bpassword|\bapi[ _-]?key|\birreversible|\bdestructive|\bsecurity\b|\bforbidden\b"
    r"|\binjection\b|\buntrusted\b|\bdelete\b|rm -rf|settings\.json",
    re.IGNORECASE,
)

#: A command-line flag in a gate phrase, with an optional value ("--top=N"); backticks optional.
_FLAG = r"`?--[a-z][a-z-]*(?:=[^`\s)]*)?`?"

_VIA = rf"(?: (?:via |with |by )?{_FLAG})?"

#: How an opt-in is named: "(opt-in `--x`)", "(only when `--x`)", "is opt-in", "— opt-in", "only with `--x`" after a
#: comma, colon, dash or the bold label. "opt-in" must close the phrase or be followed by its flag, so "opt-in flags
#: are parsed" is a mention; a hyphen inside a word ("read-only with") is no dash; "(requires `--x`)" is left out
#: because it names a prerequisite as often as an opt-in.
GATED = re.compile(
    rf"\(\s*(?:an? )?opt-in{_VIA}\s*\)"
    rf"|\(\s*only (?:with|when|if) {_FLAG}[^)\n]{{0,40}}\)"
    rf"|(?:[,:—]|(?<!\w)-|\*\*)\s+(?:(?:runs|applies|executes) )?only (?:with|when|if) {_FLAG}"
    rf"|(?:\bis\s+|[:—]\s*|(?<!\w)-\s*)opt-in{_VIA}(?=\s*(?:[.;:,)]|$))",
    re.IGNORECASE,
)

#: A negation just before a gate phrase turns "only with --x" into its opposite.
NEGATION = re.compile(r"\b(not|never|without|always)\b|n't", re.IGNORECASE)

#: Phrases that say a step applies on every run, whatever the flags.
RUN_SCOPE = (
    r"\bon (?:every|each) (?:run|call|invocation)\b|\b(?:every|each) (?:run|invocation) (?:still|also|regardless)\b"
    r"|\balways (?:runs?|applies|executes)\b|\bregardless of (?:the )?(?:flags?|options?)\b|\bunconditional(?:ly)?\b"
    r"|\bwhatever the flags\b"
)

#: Phrases right after a gate that reverse it ("only if `--x` is not set"), say the flag only changes what a default
#: step does ("only with `--fix` it rewrites"), or keep the step on every run.
AFTER_NEGATION = re.compile(
    r"\bnot\s+(?:set|passed|given|provided|present|supplied|used|specified)\b|\b(?:absent|unset|omitted)\b"
    r"|\bwithout it\b|n't\s+(?:set|passed|given|provided)\b|\b(?:it|they)\b|" + RUN_SCOPE,
    re.IGNORECASE,
)

#: A unit that loads another file on demand: a ``> loads:`` note, a loader script call, a ``cat`` of a mode file.
LOADER = re.compile(
    r"^\s*>\s*loads:|load_mode\.py|load_shared_doc\.py|dev_load_shared\.py|\bcat\s+\"\$[^\"\n]*(?:modes|_shared|templates)/",
    re.MULTILINE,
)

#: Characters after a gate phrase searched for ``AFTER_NEGATION``.
AFTER_WINDOW = 40

#: Longest plain descriptor between a block's label and its gate phrase ("three steps after the scan block, only with").
DESCRIPTOR_CHARS = 80

#: A sentence in a gated lead paragraph that says the rest applies on every run: it ends the span the gate scopes.
UNCONDITIONAL = re.compile(RUN_SCOPE, re.IGNORECASE)

#: A lead that states a rule before any rationale, also after a bold label: the rationale belongs to the rule.
DIRECTIVE = re.compile(
    r"^[\s>*\-\d.]*(?:\*\*[^*\n]+\*\*[\s:—-]*)?(never|always|must|do not|only)\b|\*\*(never|always|must)\b",
    re.IGNORECASE,
)

#: Structural protocol or deliverable contract words, and an inline JSON object (a deliverable shape).
STRUCTURAL = re.compile(
    r"\benvelope\b|Return ONLY|\bdeadline\b|AskUserQuestion|NON_AUTO_FIXABLE|## Confidence|\bconvergence\b"
    r"|\bgate\b|\bsummary\.jsonl\b|\baggregate\.md\b|\bapproval\b|\bschema\b|`\{\s*\"[a-z_]+\"\s*:",
    re.IGNORECASE,
)

#: Rationale and example vocabulary: accepted only when it is the whole label ("Rationale", "Worked example"), so
#: "Background agents", "History safety" or "Example output check" stay steps. "Note" is left out: it routinely
#: introduces an instruction.
COMPLEMENT = re.compile(
    r"(?:worked |more |further )?(?:examples?|rationale|why(?: this| it works)?|precedents?|incidents?|background"
    r"|history|known limits?|catalogue|appendix)",
    re.IGNORECASE,
)

#: A sentence that opens with one of these verbs tells the reader to do something: not an explanation. A softener
#: ("you", "please", "first,", "then") before the verb does not change that.
IMPERATIVE = re.compile(
    r"^(?:(?:you|please|first|then|now|also|just),?\s+)*(?:re-?run|re-?read|re-?check|run|add|pass|spawn|use|set|write|read|check|call|create|update|keep|ensure|make|send|print|load|fix|pick"
    r"|apply|remove|avoid|prefer|install|replace|invoke|execute|start|open|fetch|resolve|verify|confirm|choose|name"
    r"|list|return|wait|end|copy|move|put|ask|report|record|compare|scan|parse|dispatch|launch|stage|commit|push"
    r"|merge|compose|include|mark|treat|follow|take|give|try|test|build|edit|rename|drop|skip|stop|never|always|do(?:n't)?)\b",
    re.IGNORECASE,
)

#: An ordered-list item: a step or a ranked principle, never an aside.
ORDERED = re.compile(r"^\s*\d+\.\s")

#: Obligations at which a block with a rationale label is a rule, not an explanation: any.
RULE_OBLIGATIONS = 1

#: A block's label: a heading, a bold lead, or plain text up to the first colon or dash (at most 80 characters).
LABEL = re.compile(
    r"^[\s>\-\d.]*(?:#+\s*(?P<head>.{1,80})|\*\*(?P<bold>[^*\n]{1,80})\*\*|(?P<plain>[^:—\n]{1,80}?)(?::|\s—))"
)

#: A sentence that states an obligation.
NORMATIVE = re.compile(
    r"\b(must|never|always|only|shall|required|forbidden|do not|don't|exactly|at most|stop|skip|refuse[sd]?)\b",
    re.IGNORECASE,
)

#: A line that starts a new block: a bold-lead paragraph or a top-level list item.
BLOCK_START = re.compile(r"^(\*\*[^*\n]+\*\*|- |\d+\. )")

#: Load classes whose text is read on every matching session.
LOADED = ("always", "scoped")

#: Severity order, highest first.
SEVERITY_RANK = {"medium": 2, "low": 1}

#: Load order, most-read first: of two equal copies the row names the one paid for most often.
LOAD_RANK = {"always": 3, "scoped": 2, "lazy": 1}


@dataclass
class Block:
    """One scored span of an instruction file."""

    file: str
    start: int
    end: int
    title: str
    load: str
    text: str


@dataclass
class Scored:
    """A block with its judgment: ``tokens`` are the proven part of a gated block, else the whole block."""

    block: Block
    role: str
    reason: str
    gated: bool
    count: int
    tokens: int

    @property
    def ratio(self) -> float:
        """Tokens per obligation."""
        return self.tokens / self.count


@dataclass
class Finding:
    """One block judged out of proportion to its importance."""

    file: str
    lines: str
    title: str
    load: str
    role: str
    reason: str
    tokens: int
    obligations: int
    per_obligation: float
    median: float
    action: str
    severity: str
    saving: int
    copies: int
    locations: list[str] = field(default_factory=list)


def _fence_after(line: str, fence: str) -> str:
    """Return the fence state after ``line``: the opening marker while inside a fence, else an empty string."""
    if fence:
        return "" if _closes_fence(line, fence) else fence
    match = _FENCE.match(line)
    return match.group(1) if match else ""


def split_blocks(lines: list[str], first: int) -> list[tuple[int, int, str]]:
    """Split a section into ``(start, end, text)`` blocks, 1-based line numbers, fenced code kept whole.

    >>> body = ["## T", "intro", "**Lead** one", "more", "- item", "```", "- not a start", "```", "- two"]
    >>> [(s, e) for s, e, _ in split_blocks(body, 1)]
    [(1, 2), (3, 4), (5, 8), (9, 9)]
    >>> split_blocks([], 5)
    []
    """
    if not lines:
        return []
    starts: list[int] = [0]
    fence = ""
    for index, line in enumerate(lines):
        was_open = bool(fence)
        fence = _fence_after(line, fence)
        if not was_open and not fence and index and BLOCK_START.match(line):
            starts.append(index)
    ends = [*starts[1:], len(lines)]
    return [(first + a, first + b - 1, "\n".join(lines[a:b])) for a, b in zip(starts, ends, strict=True)]


def split_units(text: str) -> list[tuple[str, bool]]:
    """Split a block into ``(text, is_fence)`` units: blank-line paragraphs and whole fenced blocks.

    >>> split_units("lead\\n\\nmore\\n```\\ncode\\n```\\n\\ntail")
    [('lead', False), ('more', False), ('```\\ncode\\n```', True), ('tail', False)]
    """
    units: list[tuple[str, bool]] = []
    buf: list[str] = []
    fence = ""
    for line in text.split("\n"):
        was_open = bool(fence)
        fence = _fence_after(line, fence)
        if fence and not was_open and buf:
            units.append(("\n".join(buf), False))
            buf = []
        if was_open or fence or line.strip():
            buf.append(line)
        elif buf:
            units.append(("\n".join(buf), False))
            buf = []
        if was_open and not fence:
            units.append(("\n".join(buf), True))
            buf = []
    if buf:
        units.append(("\n".join(buf), bool(fence)))
    return units


def obligations(text: str) -> int:
    """Count the obligations in a block (at least one): see ``_rules``.

    >>> obligations("Never push. Run it.\\n```bash\\nls\\n```")
    2
    >>> obligations("Plain description.")
    1
    """
    return max(1, _rules(text))


def _rules(text: str) -> int:
    """Count normative sentences outside code, plus one per fenced block; zero for a block with no rule in it."""
    prose: list[str] = []
    fences = 0
    fence = ""
    for line in text.split("\n"):
        was_open = bool(fence)
        fence = _fence_after(line, fence)
        if not was_open and fence:
            fences += 1
        elif not was_open and not fence:
            prose.append(line)
    sentences = re.split(r"(?<=[.;:!?])\s+|\n", " ".join(prose))
    return fences + sum(1 for s in sentences if NORMATIVE.search(s))


def _lead(text: str) -> str:
    """Return the first non-empty line of a block, at most ``LEAD_CHARS`` characters."""
    return next((line for line in text.split("\n") if line.strip()), "")[:LEAD_CHARS]


def _label(lead: str) -> str:
    """Return the label of a lead line (heading, bold lead, or text before a colon or dash), or an empty string."""
    match = LABEL.match(lead)
    return next((g for g in match.group("head", "bold", "plain") if g), "") if match else ""


def _gated(lead: str) -> bool:
    """Report whether a lead line's opening clause makes the block apply only behind an opt-in flag.

    The opening clause ends at the first semicolon or sentence end, so a later sub-clause ("...; the copy is written
    only with `--json`") never gates the whole block. The phrase must sit in the block's label or heading, or after a
    short plain descriptor; a negation before it, a second bold span, or a reversal right after its flag ("only if
    `--quiet` is not set") cancels it.

    >>> _gated("**Phase B3**: three steps, only with `--measured` (zero model tokens; more).")
    True
    >>> _gated("**Step 2**: run the scan; the copy is written only with `--json`.")
    False
    >>> _gated("**Step 3**: list every opt-in flag the user passed.")
    False
    >>> _gated("**Step 4**: print the summary, only if `--quiet` is not set.")
    False
    """
    clause = re.split(r";|(?<=[.!?])\s", lead, maxsplit=1)[0]
    label = LABEL.match(clause)
    label_end = label.end() if label else 0
    return any(_anchored(clause, m, label_end) for m in GATED.finditer(clause))


def _anchored(clause: str, found: re.Match[str], label_end: int) -> bool:
    """Report whether a gate phrase is the block's own condition: inside its label or heading, or a short plain
    descriptor after it that holds no negation and no other bold span, and nothing after it reverses it."""
    before = clause[: found.start()]
    if NEGATION.search(before) or UNCONDITIONAL.search(before):
        return False
    if found.start() >= label_end:
        descriptor = clause[label_end : found.start()]
        if len(descriptor) > DESCRIPTOR_CHARS or "**" in descriptor:
            return False
    return not AFTER_NEGATION.search(clause[found.start() : found.end() + AFTER_WINDOW])


def _imperative(text: str) -> bool:
    """Report whether any sentence after the block's label opens with a verb that tells the reader to act.

    >>> _imperative("**Note**: run `make sync` after every push.")
    True
    >>> _imperative("**Rationale**: a long explanation of why.")
    False
    """
    stripped = text.lstrip()
    match = LABEL.match(stripped)
    body = stripped[match.end() :] if match else stripped
    sentences = re.split(r"(?<=[.;:!?])\s+|\n", body.lstrip(" \t:—-.*"))
    return any(IMPERATIVE.match(s.lstrip(" \t>*-0123456789.")) for s in sentences)


def classify(text: str) -> tuple[str, str, bool]:
    """Return ``(role, reason, gated)`` for a block; ``role`` is ``spine`` unless complement is proven.

    >>> classify("**Phase B3**: three steps, only with `--measured` (zero model tokens).")[:2]
    ('complement', 'gated by an opt-in flag')
    >>> classify("**Phase B**: runs always, not only with `--measured`.")[0]
    'spine'
    >>> classify("Never force push; ask first.")[0]
    'spine'
    >>> classify("**Worked example**: a long case.")[0]
    'complement'
    >>> classify("**Step 4**: run the example command.")[0]
    'spine'
    >>> classify("Do the thing.")
    ('spine', 'no complement evidence', False)
    """
    lead = _lead(text)
    if HIGH_STAKES.search(text):
        return "spine", "high-stakes words", False
    if _gated(lead):
        return "complement", "gated by an opt-in flag", True
    if DIRECTIVE.search(lead):
        return "spine", "opens with a directive", False
    if STRUCTURAL.search(text):
        return "spine", "structural or deliverable contract", False
    if any(_FENCE.match(line) for line in text.split("\n")):
        return "spine", "carries an executable step", False
    rationale = COMPLEMENT.fullmatch(_label(lead).strip(" ."))
    if rationale and _rules(text) < RULE_OBLIGATIONS and not ORDERED.match(lead) and not _imperative(text):
        return "complement", "rationale or example label", False
    return "spine", "no complement evidence", False


def _section_units(units: list[tuple[str, bool]]) -> Iterator[str]:
    """Yield the units of a gated heading section: it ends at a level-5 or 6 heading (a default-path sub-section) and
    skips a unit that loads a mode or shared file, which is the pointer a move leaves behind."""
    for unit, _is_fence in units:
        if re.match(r"\s*#{5,6}\s", unit):
            return
        if not LOADER.search(unit):
            yield unit


def gated_tokens(text: str) -> int:
    """Return the tokens of a gated block that its gate scopes.

    That is the lead paragraph up to the first sentence that turns unconditional ("on every run", "regardless of
    flags"), plus every paragraph that opens with the lead label's step id (``B3a`` under ``Phase B3``) and the fences
    that follow such a paragraph. A fence straight after the lead (the loader or guard that stays behind) and any other
    paragraph are spine sharing the block.

    >>> gated_tokens("**S** (opt-in `--x`) run.\\n\\nWhen skipping, drop both tasks.\\n\\n```\\nls\\n```") == (
    ...     estimate_tokens("**S** (opt-in `--x`) run.")
    ... )
    True
    """
    lead = _lead(text)
    step = re.search(r"\b[A-Z]{1,3}\d+\b", _label(lead))
    opener = re.compile(rf"[\s>*]*{re.escape(step.group(0))}[a-z]\b") if step else None
    units = split_units(text)
    if lead.lstrip().startswith("#"):
        return sum(estimate_tokens(unit) for unit in _section_units(units))
    sentences = re.split(r"(?<=[.!?])\s", units[0][0] if units else lead)
    kept = takewhile(lambda s: s is sentences[0] or not UNCONDITIONAL.search(s), sentences)
    total = sum(estimate_tokens(s) for s in kept)
    scoped = False
    for unit, is_fence in units[1:]:
        scoped = scoped if is_fence else bool(opener and opener.match(unit))
        total += estimate_tokens(unit) if scoped else 0
    return total


def _title(text: str) -> str:
    """Return the first non-empty line of a block, stripped of markup, at most 70 characters."""
    for line in text.split("\n"):
        plain = re.sub(r"[#*>`]", "", line).strip()
        if plain:
            return plain[:70]
    return ""


def blocks_of(item: dict[str, object], root: Path) -> list[Block] | None:
    """Read one inventory section from disk and split it into blocks; None when the file is unreadable or outside
    root."""
    path = (root / str(item["file"])).resolve()
    if not path.is_relative_to(root.resolve()):
        return None
    try:
        lines = _lines(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    start, end = int(item["start_line"]), int(item["end_line"])  # type: ignore[call-overload]
    load = str(item["load"]["hierarchy"])  # type: ignore[index]
    return [
        Block(str(item["file"]), s, e, _title(t), load, t) for s, e, t in split_blocks(lines[start - 1 : end], start)
    ]


def _medians(scored: list[Scored]) -> dict[str, float]:
    """Return the median tokens-per-obligation of ungated complement blocks (the trim population), per load class."""
    by_load: dict[str, list[float]] = {}
    for item in scored:
        if item.role == "complement" and not item.gated:
            by_load.setdefault(item.block.load, []).append(item.ratio)
    return {load: statistics.median(values) for load, values in by_load.items()}


def _is_gated_unit_path(file: str) -> bool:
    """Report whether a path is a mode or template file of one skill, or a shared file under ``skills/``: the files that
    load one at a time behind the flag or step naming them. A ``SKILL.md`` is never one.

    >>> _is_gated_unit_path("plugins/cc_x/skills/audit/modes/m.md")
    True
    >>> _is_gated_unit_path("plugins/cc_x/skills/modes/SKILL.md")
    False
    """
    parts = Path(file).parts
    if len(parts) < 3 or parts[-1] == "SKILL.md":
        return False
    shared = parts[-2] == "_shared" and parts[-3] == "skills"
    return shared or (parts[-2] in OWN_GATE_DIRS and len(parts) >= 4 and parts[-4] == "skills")


def _own_gate_files(scored: list[Scored]) -> set[str]:
    """Return the mode, template and shared files, read only when loaded, that are mostly whole gated blocks: the file
    is itself the gated unit.

    A rule, a skill body or an agent is never one, whatever its share.
    """
    total: dict[str, int] = {}
    gated_tokens_by_file: dict[str, int] = {}
    loaded_always: set[str] = set()
    for item in scored:
        file = item.block.file
        total[file] = total.get(file, 0) + estimate_tokens(item.block.text)
        if item.block.load != "lazy":
            loaded_always.add(file)
        if item.gated:
            gated_tokens_by_file[file] = gated_tokens_by_file.get(file, 0) + estimate_tokens(item.block.text)
    return {
        file
        for file, tokens in gated_tokens_by_file.items()
        if tokens >= OWN_GATE_SHARE * total[file] and file not in loaded_always and _is_gated_unit_path(file)
    }


def _decide(item: Scored, median: float, min_tokens: int, factor: float) -> tuple[str, str, int] | None:
    """Return ``(action, severity, saving)`` for a complement block, or None when it is in proportion."""
    if item.tokens < min_tokens:
        return None
    if item.gated or item.block.load in LOADED:
        return "move", ("medium" if item.block.load in LOADED else "low"), item.tokens
    if item.ratio > factor * median:
        return "trim", "low", max(0, item.tokens - int(median * item.count))
    return None


def _merge(found: Finding, block: Block, verdict: tuple[str, str, int], median: float) -> None:
    """Fold a byte-identical copy into its finding: the worst severity, then a move over a trim, then the load read most
    often names the row; every location is listed."""
    found.copies += 1
    found.locations.append(f"{block.file}:{block.start}-{block.end}")
    action, severity, saving = verdict
    if (SEVERITY_RANK[severity], action == "move", LOAD_RANK.get(block.load, 0)) > (
        SEVERITY_RANK[found.severity],
        found.action == "move",
        LOAD_RANK.get(found.load, 0),
    ):
        found.action, found.severity, found.saving = action, severity, saving
        found.file, found.lines, found.load, found.median = (
            block.file,
            f"{block.start}-{block.end}",
            block.load,
            round(median, 1),
        )


def score(
    inventory: dict[str, object], root: Path, min_tokens: int = MIN_TOKENS, factor: float = FACTOR
) -> dict[str, object]:
    """Judge every block of an inventory and return the report document."""
    scored: list[Scored] = []
    unreadable = 0
    for entry in inventory["items"]:  # type: ignore[attr-defined]
        blocks = blocks_of(entry, root)
        unreadable += blocks is None
        for block in blocks or []:
            role, reason, gated = classify(block.text)
            tokens = gated_tokens(block.text) if gated else estimate_tokens(block.text)
            scored.append(Scored(block, role, reason, gated, obligations(block.text), tokens))
    medians = _medians(scored)
    own_gate = _own_gate_files(scored)
    found: dict[str, Finding] = {}
    for item in scored:
        block = item.block
        if item.role != "complement" or (item.gated and block.file in own_gate):
            continue
        median = medians.get(block.load, 0.0)
        verdict = _decide(item, median, min_tokens, factor)
        if verdict is None:
            continue
        digest = hashlib.sha256(block.text.encode()).hexdigest()
        if digest in found:
            _merge(found[digest], block, verdict, median)
            continue
        action, severity, saving = verdict
        found[digest] = Finding(
            block.file, f"{block.start}-{block.end}", block.title, block.load, item.role, item.reason,
            item.tokens, item.count, round(item.ratio, 1), round(median, 1), action, severity,
            saving, 1, [f"{block.file}:{block.start}-{block.end}"],
        )  # fmt: skip
    findings = sorted(found.values(), key=lambda f: -f.saving * f.copies)
    return {
        "schema_version": SCHEMA_VERSION,
        "blocks": len(scored),
        "spine": sum(1 for s in scored if s.role == "spine"),
        "complement": sum(1 for s in scored if s.role == "complement"),
        "unreadable": unreadable,
        "medians": {k: round(v, 1) for k, v in medians.items()},
        "findings": [asdict(f) for f in findings],
    }


def render(report: dict[str, object], top: int = 15) -> str:
    """Render the report as the markdown section the efficiency report embeds."""
    findings: list[dict[str, object]] = report["findings"]  # type: ignore[assignment]
    lines = [
        "## Size to importance (E11)",
        "",
        f"{report['blocks']} blocks scored: {report['spine']} spine (not flagged for size), "
        f"{report['complement']} complement, {len(findings)} flagged. "
        "Tokens are bytes/3, and for a gated block only the part proven to sit behind its gate; obligations are a "
        "keyword proxy; a thin spine block is not detected here.",
        "",
    ]
    if report.get("unreadable"):
        lines += [f"{report['unreadable']} section(s) skipped: file unreadable or outside the root.", ""]
    if not findings:
        return "\n".join([*lines, "No complement block is out of proportion."])
    lines += [
        "| action | severity | file:lines | block | load | tokens | obligations | copies | why |",
        "| -- " * 9 + "|",
    ]
    for f in findings[:top]:
        title = str(f["title"]).replace("|", "\\|")
        primary = f"{f['file']}:{f['lines']}"
        others = [loc for loc in f.get("locations", []) if loc != primary]
        copies = f"{f['copies']}" + (
            f" (also {', '.join(others[:3])}{', …' if len(others) > 3 else ''})" if others else ""
        )
        lines.append(
            f"| {f['action']} | {f['severity']} | `{primary}` | {title} | {f['load']} "
            f"| {f['tokens']} | {f['obligations']} | {copies} | {f['reason']} |"
        )
    if len(findings) > top:
        lines.append(f"\n{len(findings) - top} more in the JSON output.")
    return "\n".join(lines)


def inventory_problem(inventory: object) -> str:
    """Return why an inventory cannot be scored, or an empty string when every item has file, lines and load class."""
    items = inventory.get("items") if isinstance(inventory, dict) else None
    if not isinstance(items, list) or not items:
        return "no items"
    for index, item in enumerate(items):
        try:
            ok = (
                isinstance(item["file"], str)
                and isinstance(item["start_line"], int)
                and isinstance(item["end_line"], int)
                and isinstance(item["load"]["hierarchy"], str)
            )
        except (KeyError, TypeError):
            ok = False
        if not ok:
            return f"item {index} lacks file, start_line, end_line or load.hierarchy"
    return ""


def main(argv: list[str] | None = None) -> int:
    """Run the scorer and print the markdown section."""
    parser = argparse.ArgumentParser(description="Flag instruction text whose size outruns its importance.")
    parser.add_argument("--root", default=".", help="repository root holding plugins/")
    parser.add_argument("--inventory", help="inventory JSON from consolidate_inventory.py; built in memory when absent")
    parser.add_argument("--min-tokens", type=int, default=MIN_TOKENS)
    parser.add_argument("--factor", type=float, default=FACTOR)
    parser.add_argument("--json", help="also write the full report here")
    args = parser.parse_args(argv)
    root = Path(args.root)
    if args.inventory:
        try:
            inventory = json.loads(Path(args.inventory).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"! BLOCKED — unreadable inventory: {exc}", file=sys.stderr)
            return 1
    else:
        inventory = build_inventory(root, ALL_KINDS)
    problem = inventory_problem(inventory)
    if problem:
        print(f"! BLOCKED — unusable inventory: {problem}", file=sys.stderr)
        return 1
    report = score(inventory, root, args.min_tokens, args.factor)
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())

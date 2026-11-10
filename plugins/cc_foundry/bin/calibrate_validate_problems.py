#!/usr/bin/env python
"""calibrate_validate_problems.py — validate generated calibration problems so a bad answer key never reaches scoring.

Phase 1b of the calibrate pipeline. Each generator writes ONE problem per file into the run dir as
``problems-<source>-<problem_id>.json`` (source ``claude``, ``codex`` or ``real``). This script checks every such file
deterministically, merges the valid problems into ``problems.json`` and lists every rejected file, with all of its
reasons, in ``problems-rejected.json``. Other files in the run dir (including the merged outputs themselves and any
legacy ``problems-<source>.json`` array) are not inputs and are left alone.

A failing check rejects the WHOLE problem, never a single ground-truth item: dropping one item would leave its defect in
``input``, and a reviewer that correctly reports it would then be scored a false positive.

Problem schema v2, one JSON object per file:
    problem_id    kebab-case string
    difficulty    trivial | low | medium | high | extreme | scope
    source        claude | codex | real — optional; taken from the file name when absent or null, rejected when it
                  disagrees
    task_prompt   non-empty string
    input         non-empty string
    ground_truth  list of {claim, decisive, role, location, severity}; claim/decisive/role non-empty strings,
                  location a string, severity critical | high | medium | low | null (absent counts as null);
                  ``[]`` for a scope problem, non-empty otherwise
    near_misses   optional list of {ref, wrong_role}, both non-empty strings; written as ``[]`` when absent
Any other key (for example ``provenance``) passes through unchanged. A ``decisive`` of ``none`` (any case) marks a
no-answer item such as "no candidate is a duplicate".

Checks — every text comparison is case-insensitive on stripped text:
    1. schema and types above; the same ``decisive`` + ``role`` pair listed twice
    2. every ``decisive`` except ``none``, and every ``near_misses[].ref``, occurs in ``input`` — skipped under
       ``--derived-gt``, for domains whose answer a deterministic tool computes from the fixture (for example a band
       computed from a JSONL data file), so the answer token never appears verbatim in ``input``
    3. free-recall leak: a ``decisive`` (``none`` included) equal to the value of a labelled input line such as
       ``Type: pull_request``
    4. size floor of ``input`` for ``high`` and ``extreme`` problems from synthetic sources (``real`` is exempt)
    5. a near miss whose ``ref`` is a ground-truth ``decisive`` under that item's own role (contradictory key)
    6. at most one scope problem in the merged set; the first in (source, problem_id) order is kept
    7. ``--open-shape`` (thread runs): an open candidate pool — see :func:`open_shape_reasons`
    8. ``--boundary`` (vitality runs, with ``--derived-gt``): enough axes near a band line — see
       :func:`boundary_reasons`

Merge: valid problems sorted by source then problem_id, each carrying its ``source``. A problem_id used by more than
one source is prefixed ``<source>-`` on every problem carrying it; a problem_id repeated within one source keeps only
the first file in name order.

Usage:
    python calibrate_validate_problems.py --run-dir <dir> [--floors high=6000,extreme=12000] [--derived-gt]
        [--open-shape] [--boundary]

Prints one compact JSON summary line, counts over valid problems:
    {"valid":N,"rejected":N,"by_source":{...},"by_difficulty":{...},"rejected_ids":[...]}

Exit codes:
    0 — at least one valid problem
    1 — no valid problem, or the run dir is unreadable, or the outputs cannot be written
    2 — argument error, including a malformed ``--floors`` value
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class Source(str, Enum):
    """Generator that produced a problem; ``real`` problems come from frozen real-world threads."""

    CLAUDE = "claude"
    CODEX = "codex"
    REAL = "real"


class Difficulty(str, Enum):
    """Difficulty tier of a problem; ``scope`` marks the out-of-scope problem that must yield no findings."""

    TRIVIAL = "trivial"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    EXTREME = "extreme"
    SCOPE = "scope"


class Severity(str, Enum):
    """Severity of one ground-truth item."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


#: Sources whose inputs are generated, so a short input means an under-built hard problem.
_SYNTHETIC_SOURCES = frozenset({Source.CLAUDE.value, Source.CODEX.value})
#: Name of one generated problem file; the source segment is checked here, the id segment is only a fallback label.
_PROBLEM_FILE = re.compile(r"^problems-(" + "|".join(source.value for source in Source) + r")-(.+)\.json$")
#: Kebab-case problem id: lowercase alphanumeric words joined by single hyphens.
_KEBAB = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
#: Labelled input line (``Type: pull_request``); its value is free to read, so a decisive equal to it is leaked.
_LABELLED_LINE = re.compile(r"^\s*[A-Za-z][A-Za-z _-]{0,30}:\s*(.+?)\s*$")
#: Decisive marking a no-answer item; it has no text to find in ``input``.
_NO_ANSWER = "none"
#: Ground-truth fields that must be non-empty strings; an empty ``decisive`` would occur in every input.
_GT_REQUIRED_TEXT = ("claim", "decisive", "role")
#: Near-miss fields that must be non-empty strings.
_NEAR_MISS_REQUIRED_TEXT = ("ref", "wrong_role")
#: Merged output with every valid problem.
MERGED_NAME = "problems.json"
#: Output listing every rejected problem file with its reasons.
REJECTED_NAME = "problems-rejected.json"

#: One candidate of a rendered pool, the row format real thread fixtures use:
#: ``#<N> | state: <s> | title: <t> | body: <b>``. The title is non-greedy so the first ``| body:`` ends it; the body
#: runs to the end of the line, since real bodies may contain ``|``.
_CANDIDATE_ROW = re.compile(r"^\s*#(\d+)\s*\|\s*state:\s*(.*?)\s*\|\s*title:\s*(.*?)\s*\|\s*body:\s*(.*?)\s*$")
#: Fewest candidate rows an open pool must render.
_OPEN_SHAPE_MIN_ROWS = 5
#: Longest candidate body, in characters; a longer body tends to restate the item and give the link away.
_OPEN_SHAPE_MAX_BODY = 300
#: Fence marker; a code block inside the pool turns candidate matching into code comparison.
_FENCE = "```"
#: Length of a shared word run, in tokens, that counts as a phrase copied from the item into its canonical candidate.
_COPIED_RUN = 4
#: Word token of the copied-phrase check, matched on lowercased text.
_WORD = re.compile(r"[a-z0-9_]+")
#: Ground-truth role naming the duplicate's canonical candidate.
_DUPLICATE_ROLE = "duplicate"
#: A ``decisive`` naming a candidate row: ``#<N>``, the ``#`` optional.
_CANDIDATE_REF = re.compile(r"#?(\d+)")

#: Difficulties the boundary check applies to.
_BOUNDARY_DIFFICULTIES = frozenset({Difficulty.HIGH.value, Difficulty.EXTREME.value})
#: Axes that must sit one step from a different band for a boundary problem to pass.
_BOUNDARY_MIN_AXES = 3
#: Extractor groups saved per problem, as ``derived-<problem_id>-<group>.json``.
_DERIVED_GROUPS = ("A", "B", "C")
#: Band glyphs the extractor prints; ``⚪`` (unscorable) is deliberately absent.
_RED, _YELLOW, _GREEN = "🔴", "🟡", "🟢"
_SCORED_BANDS = frozenset({_RED, _YELLOW, _GREEN})
#: Checkpoint axes → (🟢 minimum met, 🟡 minimum met). Mirrors ``score_axis5``/``6``/``7`` of the oss plugin's
#: ``vitality_extract.py``; a disagreement with a printed band is reported, never silently scored.
_CHECKPOINT_CUTOFFS: dict[str, tuple[int, int]] = {"5": (4, 2), "6": (7, 4), "7": (5, 3)}
#: Clause axes → number of 🟢 clauses ``band_placement`` requires. Mirrors ``score_axis1``/``2``/``3``/``4``/``8``.
_CLAUSE_TOTALS: dict[str, int] = {"1": 3, "2": 2, "3": 3, "4": 4, "8": 2}
#: Trajectory axis: band from the mean of its available sub-signal scores. Mirrors ``score_axis9``.
_TRAJECTORY_AXIS = "9"
#: Mean at or above which the trajectory band is 🟢, then 🟡; below the second it is 🔴.
_TRAJECTORY_CUTOFFS = (7.5, 3.75)
#: Levels a trajectory sub-signal scores (🔴 0 · 🟡 5 · 🟢 10); one step moves one sub-signal to an adjacent level.
_SUB_SCORE_LEVELS = (0, 5, 10)
_SUB_SCORE_STEP = 5
#: The "one step" definition, quoted in every boundary rejection so the generator sees the rule it missed.
_ONE_STEP_RULE = (
    "one step = a checkpoint axis (5, 6, 7) whose met count +-1 crosses a band cutoff; trajectory axis 9 where one "
    "sub-signal moved one level (+-5) moves the mean across 7.5 or 3.75; a clause axis (1, 2, 3, 4, 8) that is 🔴 on "
    "exactly one red condition or 🟡 with exactly one 🟢 clause missing. 🟢 clause axes, ⚪ axes, the axis 2 "
    "abandonment override and axis 8 partial scoring never count"
)


@dataclass(frozen=True)
class Floors:
    """Minimum ``input`` length, in characters, for synthetic problems at the two hardest difficulties."""

    high: int = 6000
    extreme: int = 12000

    def minimum(self, difficulty: object) -> int:
        """Return the input-length floor for ``difficulty``, or 0 for a difficulty without one.

        Examples:
            >>> Floors().minimum("extreme"), Floors().minimum("medium")
            (12000, 0)
        """
        return {Difficulty.HIGH.value: self.high, Difficulty.EXTREME.value: self.extreme}.get(str(difficulty), 0)


@dataclass(frozen=True)
class Problem:
    """One problem file that passed every per-problem check."""

    file: str
    source: str
    problem_id: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class Rejection:
    """One rejected problem file with every reason it failed."""

    file: str
    problem_id: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class DerivedOutputs:
    """The saved ``vitality_extract.py`` outputs of one problem, every group's axes merged by axis number.

    Attributes:
        axes: Axis number → that axis's printed object, from every group file that could be read.
        missing: One description per group file that is absent, unreadable, or not that group's extractor output.
    """

    axes: dict[str, object]
    missing: tuple[str, ...] = ()


@dataclass(frozen=True)
class _CandidateRow:
    """One parsed candidate row of a thread problem's pool."""

    line: int
    number: int
    title: str
    body: str


class _Step(str, Enum):
    """Whether one derived axis sits one step from a different band."""

    ONE = "one step"
    OTHER = "not one step"
    INCONSISTENT = "band disagrees with the mirrored extractor rule"


def parse_floors(text: str) -> Floors:
    """Read a ``--floors`` value into input-length floors, keeping the default for any difficulty it omits.

    Args:
        text: Comma-separated ``<difficulty>=<chars>`` pairs, difficulty ``high`` or ``extreme``.

    Returns:
        The floors with the given values applied over the defaults.

    Raises:
        ValueError: A pair is malformed, names another difficulty, or its value is not a non-negative integer.

    Examples:
        >>> parse_floors("high=7000")
        Floors(high=7000, extreme=12000)
        >>> parse_floors("high=1,extreme=2")
        Floors(high=1, extreme=2)
    """
    known = {Difficulty.HIGH.value, Difficulty.EXTREME.value}
    values: dict[str, int] = {}
    for pair in text.split(","):
        key, separator, raw = (part.strip() for part in pair.partition("="))
        if not separator or key not in known:
            raise ValueError(f"floor pair {pair.strip()!r} must be high=<chars> or extreme=<chars>")
        if not re.fullmatch(r"[0-9]+", raw):
            raise ValueError(f"floor {key}={raw!r} must be a non-negative integer")
        values[key] = int(raw)
    return Floors(**values)


def _norm(value: object) -> str:
    """Return ``value`` stripped and case-folded when it is a string, else an empty string."""
    return value.strip().casefold() if isinstance(value, str) else ""


def _is_text(value: object) -> bool:
    """Report whether ``value`` is a string holding more than whitespace."""
    return isinstance(value, str) and bool(value.strip())


def _dict_items(problem: dict[str, Any], key: str) -> list[tuple[int, dict[str, Any]]]:
    """Return the indexed object entries of the list field ``key``; anything else yields no entries."""
    value = problem.get(key)
    return (
        [(index, item) for index, item in enumerate(value) if isinstance(item, dict)] if isinstance(value, list) else []
    )


def _top_level_reasons(problem: dict[str, Any], file_source: str) -> list[str]:
    """Check the problem's top-level fields (check 1)."""
    reasons = []
    if not isinstance(problem.get("problem_id"), str) or not _KEBAB.match(problem["problem_id"]):
        reasons.append("problem_id must be a kebab-case string")
    difficulty = problem.get("difficulty")
    # isinstance first: a list or object value is unhashable and would raise on the set lookup.
    if not isinstance(difficulty, str) or difficulty not in {tier.value for tier in Difficulty}:
        reasons.append(f"difficulty {difficulty!r} must be one of {', '.join(tier.value for tier in Difficulty)}")
    if problem.get("source") is not None and problem["source"] != file_source:
        reasons.append(f"source field {problem['source']!r} disagrees with file name source {file_source!r}")
    reasons += [
        f"{key} must be a non-empty string" for key in ("task_prompt", "input") if not _is_text(problem.get(key))
    ]
    ground_truth = problem.get("ground_truth")
    if not isinstance(ground_truth, list):
        reasons.append("ground_truth must be a list")
    elif difficulty == Difficulty.SCOPE.value and ground_truth:
        reasons.append("scope problem must have an empty ground_truth")
    elif difficulty != Difficulty.SCOPE.value and not ground_truth:
        reasons.append("ground_truth must be non-empty for a non-scope problem")
    if problem.get("near_misses") is not None and not isinstance(problem["near_misses"], list):
        reasons.append("near_misses must be a list when present")
    return reasons


def _ground_truth_reasons(problem: dict[str, Any]) -> list[str]:
    """Check every ground-truth item's fields and reject a repeated ``decisive`` + ``role`` pair (check 1)."""
    ground_truth = problem.get("ground_truth")
    entries = ground_truth if isinstance(ground_truth, list) else []
    reasons = [
        f"ground_truth[{index}] must be an object" for index, item in enumerate(entries) if not isinstance(item, dict)
    ]
    seen: set[tuple[str, str]] = set()
    for index, item in _dict_items(problem, "ground_truth"):
        reasons += [
            f"ground_truth[{index}].{key} must be a non-empty string"
            for key in _GT_REQUIRED_TEXT
            if not _is_text(item.get(key))
        ]
        if not isinstance(item.get("location"), str):
            reasons.append(f"ground_truth[{index}].location must be a string")
        severity = item.get("severity")
        if severity is not None and (not isinstance(severity, str) or severity not in {s.value for s in Severity}):
            reasons.append(f"ground_truth[{index}].severity {severity!r} must be critical, high, medium, low or null")
        pair = (_norm(item.get("decisive")), _norm(item.get("role")))
        if all(pair) and pair in seen:
            reasons.append(f"ground_truth[{index}] repeats the decisive+role pair {pair!r} of an earlier item")
        seen.add(pair)
    return reasons


def _near_miss_reasons(problem: dict[str, Any]) -> list[str]:
    """Check every near miss's fields (check 1)."""
    near_misses = problem.get("near_misses")
    entries = near_misses if isinstance(near_misses, list) else []
    reasons = [
        f"near_misses[{index}] must be an object" for index, item in enumerate(entries) if not isinstance(item, dict)
    ]
    for index, item in _dict_items(problem, "near_misses"):
        reasons += [
            f"near_misses[{index}].{key} must be a non-empty string"
            for key in _NEAR_MISS_REQUIRED_TEXT
            if not _is_text(item.get(key))
        ]
    return reasons


def _occurrence_reasons(problem: dict[str, Any]) -> list[str]:
    """Require every answer-bearing ``decisive`` and every near-miss ``ref`` to occur in ``input`` (check 2)."""
    haystack = _norm(problem.get("input"))
    if not haystack:
        return []
    reasons = []
    for index, item in _dict_items(problem, "ground_truth"):
        decisive = _norm(item.get("decisive"))
        if decisive and decisive != _NO_ANSWER and decisive not in haystack:
            reasons.append(f"ground_truth[{index}].decisive {item['decisive']!r} does not occur in input")
    for index, item in _dict_items(problem, "near_misses"):
        ref = _norm(item.get("ref"))
        if ref and ref not in haystack:
            reasons.append(f"near_misses[{index}].ref {item['ref']!r} does not occur in input")
    return reasons


def _leak_reasons(problem: dict[str, Any]) -> list[str]:
    """Reject a ``decisive`` readable verbatim as the value of a labelled input line (check 3)."""
    text = problem.get("input")
    if not isinstance(text, str):
        return []
    labelled = {match.group(1).casefold() for line in text.splitlines() if (match := _LABELLED_LINE.match(line))}
    return [
        f"ground_truth[{index}].decisive {item['decisive']!r} is the value of a labelled input line (free-recall leak)"
        for index, item in _dict_items(problem, "ground_truth")
        if _norm(item.get("decisive")) and _norm(item.get("decisive")) in labelled
    ]


def _floor_reasons(problem: dict[str, Any], source: str, floors: Floors) -> list[str]:
    """Reject a synthetic high or extreme problem whose ``input`` is shorter than its floor (check 4)."""
    text = problem.get("input")
    floor = floors.minimum(problem.get("difficulty"))
    if source not in _SYNTHETIC_SOURCES or not isinstance(text, str) or len(text) >= floor:
        return []
    return [f"input has {len(text)} chars, below the {problem['difficulty']} floor of {floor}"]


def _near_miss_conflict_reasons(problem: dict[str, Any]) -> list[str]:
    """Reject a near miss that names a ground-truth answer under that answer's own role (check 5)."""
    answers = {
        (_norm(item.get("decisive")), _norm(item.get("role"))) for _, item in _dict_items(problem, "ground_truth")
    }
    reasons = []
    for index, item in _dict_items(problem, "near_misses"):
        claim = (_norm(item.get("ref")), _norm(item.get("wrong_role")))
        # Both halves must be present: a missing field on each side would otherwise pair ("x", "") with ("x", "").
        if all(claim) and claim in answers:
            reasons.append(
                f"near_misses[{index}] ref {item['ref']!r} as {item['wrong_role']!r} contradicts a ground-truth item"
            )
    return reasons


def _candidate_rows(lines: list[str]) -> list[_CandidateRow]:
    """Parse every line of ``input`` that is a candidate row in the open-pool format."""
    rows = []
    for index, line in enumerate(lines):
        match = _CANDIDATE_ROW.match(line)
        if match is not None:
            rows.append(_CandidateRow(index, int(match.group(1)), match.group(3), match.group(4)))
    return rows


def _word_runs(text: str) -> list[tuple[str, ...]]:
    """Return every run of ``_COPIED_RUN`` consecutive word tokens of ``text``, in order."""
    tokens = _WORD.findall(text.lower())
    return [tuple(tokens[start : start + _COPIED_RUN]) for start in range(len(tokens) - _COPIED_RUN + 1)]


def _copied_phrase_reasons(problem: dict[str, Any], rows: list[_CandidateRow], item_text: str) -> list[str]:
    """Reject a duplicate whose canonical row is missing from the pool or repeats a phrase of the item text.

    The row's own text is its title followed by its body; the ``#N``, the state and the field labels are left out, since
    every row and the item header carry them and they would match without anything being copied.
    """
    item_runs = set(_word_runs(item_text))
    by_number = {row.number: row for row in rows}
    reasons = []
    for index, item in _dict_items(problem, "ground_truth"):
        decisive = _norm(item.get("decisive"))
        if _norm(item.get("role")) != _DUPLICATE_ROLE or not decisive or decisive == _NO_ANSWER:
            continue
        reference = _CANDIDATE_REF.fullmatch(decisive)
        row = by_number.get(int(reference.group(1))) if reference else None
        if row is None:
            reasons.append(f"open shape: ground_truth[{index}] duplicate {item['decisive']!r} names no candidate row")
            continue
        shared = next((run for run in _word_runs(f"{row.title} {row.body}") if run in item_runs), None)
        if shared is not None:
            reasons.append(
                f"open shape: canonical candidate #{row.number} shares the {_COPIED_RUN}-word phrase "
                f"{' '.join(shared)!r} with the item text, so the duplicate link is a string match"
            )
    return reasons


def open_shape_reasons(problem: dict[str, Any], source: str) -> list[str]:
    """Reject a synthetic thread problem whose candidate pool is not rendered as an open pool (check 7).

    The pool must use the real fixtures' row format, ``#<N> | state: <s> | title: <t> | body: <b>``, one candidate per
    line. A problem is rejected when fewer than 5 rows parse, when a row's body exceeds 300 characters, when the
    candidate section (first row to last row) holds a triple-backtick fence, or when the canonical row of a
    ``duplicate`` ground-truth item (``decisive`` ``#<N>``, not ``none``) is absent from the pool or shares a run of 4
    consecutive word tokens (lowercased ``[a-z0-9_]+``) with the item text before the first row. ``real`` problems are
    exempt, like the size floors, and so is the ``scope`` problem: it is off-domain by design and has no pool.

    Args:
        problem: One problem object.
        source: Source named by the problem's file name.

    Returns:
        Rejection reasons, each prefixed ``open shape:``; empty when the problem passes or is exempt.

    Examples:
        >>> open_shape_reasons({"difficulty": "low", "input": "Title: x\\n#1 | issue | open | y | z"}, "claude")
        ['open shape: only 0 candidate rows in the #<N> | state: <s> | title: <t> | body: <b> format (need 5)']
        >>> open_shape_reasons({"difficulty": "low", "input": "no pool"}, "real")
        []
    """
    text = problem.get("input")
    if source not in _SYNTHETIC_SOURCES or problem.get("difficulty") == Difficulty.SCOPE.value:
        return []
    if not isinstance(text, str):
        return []
    lines = text.splitlines()
    rows = _candidate_rows(lines)
    reasons = []
    if len(rows) < _OPEN_SHAPE_MIN_ROWS:
        reasons.append(
            f"open shape: only {len(rows)} candidate rows in the #<N> | state: <s> | title: <t> | body: <b> format "
            f"(need {_OPEN_SHAPE_MIN_ROWS})"
        )
    long_bodies = [f"#{row.number} ({len(row.body)})" for row in rows if len(row.body) > _OPEN_SHAPE_MAX_BODY]
    if long_bodies:
        reasons.append(f"open shape: candidate bodies over {_OPEN_SHAPE_MAX_BODY} chars: {', '.join(long_bodies)}")
    if not rows:
        return reasons
    if any(_FENCE in line for line in lines[rows[0].line : rows[-1].line + 1]):
        reasons.append(
            f"open shape: the candidate section (#{rows[0].number} to #{rows[-1].number}) holds a {_FENCE} fence"
        )
    return reasons + _copied_phrase_reasons(problem, rows, "\n".join(lines[: rows[0].line]))


def read_derived_outputs(run_dir: Path, problem_id: str) -> DerivedOutputs:
    """Read the three saved extractor outputs of one problem, ``derived-<problem_id>-<A|B|C>.json`` in ``run_dir``.

    Args:
        run_dir: Directory the pipeline saved the outputs into.
        problem_id: The problem's declared id.

    Returns:
        Every readable group's axes, plus one entry in ``missing`` per file that is absent, unreadable, not a JSON
        object with an ``axes`` object, or printed for another group.
    """
    axes: dict[str, object] = {}
    missing: list[str] = []
    for group in _DERIVED_GROUPS:
        name = f"derived-{problem_id}-{group}.json"
        try:
            payload = json.loads((run_dir / name).read_text(encoding="utf-8"))
        except FileNotFoundError:
            missing.append(f"{name} not found")
            continue
        except (OSError, ValueError) as error:
            missing.append(f"{name} unreadable: {error}")
            continue
        group_axes = payload.get("axes") if isinstance(payload, dict) else None
        if not isinstance(group_axes, dict) or payload.get("group") != group:
            missing.append(f"{name} is not a group {group} extractor output")
            continue
        axes.update(group_axes)
    return DerivedOutputs(axes, tuple(missing))


def _is_count(value: object) -> bool:
    """Report whether ``value`` is a non-negative integer (``bool`` excluded)."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _met_band(met: int, cutoffs: tuple[int, int]) -> str:
    """Return a checkpoint axis's band for ``met`` under (🟢 minimum, 🟡 minimum) ``cutoffs``."""
    green, yellow = cutoffs
    if met >= green:
        return _GREEN
    return _YELLOW if met >= yellow else _RED


def _checkpoint_step(axis: dict[str, Any], cutoffs: tuple[int, int]) -> _Step:
    """Report whether one more or one fewer met checkpoint (within ``0..applicable``) changes the band."""
    met, applicable, band = axis.get("met"), axis.get("applicable"), axis.get("band")
    if not _is_count(met) or not _is_count(applicable) or band not in _SCORED_BANDS:
        return _Step.OTHER
    if _met_band(met, cutoffs) != band:
        return _Step.INCONSISTENT
    neighbours = [count for count in (met - 1, met + 1) if 0 <= count <= applicable]
    return _Step.ONE if any(_met_band(count, cutoffs) != band for count in neighbours) else _Step.OTHER


def _clause_step(axis: dict[str, Any], total: int) -> _Step:
    """Report whether a band-placement axis rests on a single clause: 🔴 on one red condition or 🟡 one 🟢 clause short.

    A 🟢 axis never counts: it needs every 🟢 clause, so losing any one demotes it whatever the data, and that says
    nothing about how close the data sits to a threshold. The abandonment override decides axis 2 regardless of clauses,
    and axis 8 partial scoring prints no clauses, so neither counts.
    """
    held, red, band = axis.get("clauses_held"), axis.get("red_held"), axis.get("band")
    if "override" in axis or not isinstance(held, list) or not isinstance(red, list) or band not in _SCORED_BANDS:
        return _Step.OTHER
    expected = _RED if red else _GREEN if len(held) == total else _YELLOW
    if expected != band:
        return _Step.INCONSISTENT
    if band == _RED:
        return _Step.ONE if len(red) == 1 else _Step.OTHER
    return _Step.ONE if band == _YELLOW and len(held) == total - 1 else _Step.OTHER


def _trajectory_band(scores: list[float]) -> str:
    """Return the trajectory band of the mean of ``scores``."""
    mean = sum(scores) / len(scores)
    green, yellow = _TRAJECTORY_CUTOFFS
    if mean >= green:
        return _GREEN
    return _YELLOW if mean >= yellow else _RED


def _trajectory_step(axis: dict[str, Any]) -> _Step:
    """Report whether moving one available sub-signal to an adjacent level changes the trajectory band."""
    subs, band = axis.get("sub_scores"), axis.get("band")
    if not isinstance(subs, dict) or band not in _SCORED_BANDS:
        return _Step.OTHER
    scores = [score for score in subs.values() if score is not None]
    if not scores or not all(_is_count(score) and score in _SUB_SCORE_LEVELS for score in scores):
        return _Step.OTHER
    if _trajectory_band(scores) != band:
        return _Step.INCONSISTENT
    variants = (
        [*scores[:index], level, *scores[index + 1 :]]
        for index, score in enumerate(scores)
        for level in _SUB_SCORE_LEVELS
        if abs(level - score) == _SUB_SCORE_STEP
    )
    return _Step.ONE if any(_trajectory_band(variant) != band for variant in variants) else _Step.OTHER


def _axis_step(number: str, axis: object) -> _Step:
    """Classify one derived axis by its kind; an unknown axis or a non-object value never counts."""
    if not isinstance(axis, dict):
        return _Step.OTHER
    if number in _CHECKPOINT_CUTOFFS:
        return _checkpoint_step(axis, _CHECKPOINT_CUTOFFS[number])
    if number in _CLAUSE_TOTALS:
        return _clause_step(axis, _CLAUSE_TOTALS[number])
    return _trajectory_step(axis) if number == _TRAJECTORY_AXIS else _Step.OTHER


def boundary_reasons(problem: dict[str, Any], derived: DerivedOutputs) -> list[str]:
    """Reject a high or extreme vitality problem with fewer than 3 axes one step from a different band (check 8).

    Ground truth is the band per axis that ``vitality_extract.py`` prints; a hard problem must put several axes next
    to a band line, or a reviewer reading the data loosely still lands every band. "One step" is defined per axis
    kind from what the extractor prints, with its cutoffs mirrored here (the extractor is never imported or run):

    - checkpoint axes 5, 6, 7 (band from ``met``; 🟢/🟡 minimum 4/2, 7/4, 5/3): ``met`` + 1 or ``met`` - 1, kept
      within ``0..applicable``, gives another band — ``met`` exactly at, or one below, a cutoff
    - trajectory axis 9 (band from the mean of the non-null ``sub_scores``; 🟢 ≥ 7.5, 🟡 ≥ 3.75): moving one
      sub-signal one level (0, 5, 10) gives another band
    - clause axes 1, 2, 3, 4, 8 (``band_placement``; 3, 2, 3, 4, 2 🟢 clauses): 🔴 with exactly one ``red_held``
      condition, or 🟡 with no red condition and exactly one 🟢 clause missing from ``clauses_held``

    Counted as undecidable, never one step: a 🟢 clause axis (it needs every clause, so it is always one clause from
    🟡 and that carries no proximity), a ⚪ axis, axis 2 under the abandonment ``override``, and axis 8 partial
    scoring (it prints no clauses; its points do not move in single units). The printed metrics behind each clause
    (medians, day counts, rates) have no common unit of one step, so clause axes are judged on clauses alone. An axis
    whose printed band disagrees with the mirrored rule is reported, so a changed extractor cutoff fails loudly.

    Args:
        problem: One problem object; only ``high`` and ``extreme`` problems are checked.
        derived: The problem's saved extractor outputs.

    Returns:
        ``derived output missing`` when any group file could not be used, else rejection reasons prefixed
        ``boundary:``; empty when the problem passes or is not checked.

    Examples:
        >>> boundary_reasons({"difficulty": "high"}, DerivedOutputs({}, ("derived-x-A.json not found",)))
        ['derived output missing: derived-x-A.json not found']
        >>> boundary_reasons({"difficulty": "medium"}, DerivedOutputs({}))
        []
    """
    if str(problem.get("difficulty")) not in _BOUNDARY_DIFFICULTIES:
        return []
    if derived.missing:
        return [f"derived output missing: {'; '.join(derived.missing)}"]
    steps = {number: _axis_step(number, axis) for number, axis in sorted(derived.axes.items())}
    inconsistent = [number for number, step in steps.items() if step is _Step.INCONSISTENT]
    one_step = [number for number, step in steps.items() if step is _Step.ONE]
    reasons = []
    if inconsistent:
        reasons.append(
            f"boundary: axes {', '.join(inconsistent)} print a band the mirrored vitality_extract.py rule does not "
            "give; update the boundary cutoffs in calibrate_validate_problems.py"
        )
    if len(one_step) < _BOUNDARY_MIN_AXES:
        reasons.append(
            f"boundary: only {len(one_step)} of {len(steps)} axes sit one step from a different band "
            f"(need {_BOUNDARY_MIN_AXES}; one step: {', '.join(one_step) or 'none'}); {_ONE_STEP_RULE}"
        )
    return reasons


def validate_problem(
    problem: object,
    source: str,
    floors: Floors,
    *,
    derived_gt: bool = False,
    open_shape: bool = False,
    derived: DerivedOutputs | None = None,
) -> list[str]:
    """List every reason one problem must be rejected; an empty list means the problem is valid.

    Covers the per-problem checks 1–5, 7 and 8 of the module docstring. Each check reads only the fields it can trust,
    so a schema failure does not hide an independent content failure in the same problem.

    Args:
        problem: The parsed content of one problem file.
        source: Source named by the problem's file name.
        floors: Input-length floors for synthetic ``high`` and ``extreme`` problems.
        derived_gt: Skip only the occurrence check (check 2), for ground truth a deterministic tool computes from the
            fixture rather than quotes from it. The free-recall leak check and every other check still apply.
        open_shape: Apply the open candidate-pool check (check 7) for thread problems; see
            :func:`open_shape_reasons`.
        derived: The problem's saved extractor outputs, which turn on the boundary check (check 8) for ``high`` and
            ``extreme`` problems; ``None`` leaves it off. See :func:`boundary_reasons`.

    Returns:
        Human-readable rejection reasons, in check order.

    Examples:
        >>> validate_problem([], "claude", Floors())
        ['problem file must hold one JSON object']
    """
    if not isinstance(problem, dict):
        return ["problem file must hold one JSON object"]
    return [
        *_top_level_reasons(problem, source),
        *_ground_truth_reasons(problem),
        *_near_miss_reasons(problem),
        *([] if derived_gt else _occurrence_reasons(problem)),
        *_leak_reasons(problem),
        *_floor_reasons(problem, source, floors),
        *_near_miss_conflict_reasons(problem),
        *(open_shape_reasons(problem, source) if open_shape else []),
        *([] if derived is None else boundary_reasons(problem, derived)),
    ]


def _load(
    path: Path, source: str, fallback_id: str, floors: Floors, *, derived_gt: bool, open_shape: bool, boundary: bool
) -> Problem | Rejection:
    """Read and validate one problem file, reading its saved extractor outputs when the boundary check applies."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return Rejection(path.name, fallback_id, (f"unreadable problem file: {error}",))
    declared_id = payload.get("problem_id") if isinstance(payload, dict) else None
    problem_id = declared_id if isinstance(declared_id, str) and declared_id else fallback_id
    derived = None
    if boundary and isinstance(payload, dict) and str(payload.get("difficulty")) in _BOUNDARY_DIFFICULTIES:
        # Only a kebab-case id names a file; anything else (``../x``) could reach outside the run dir.
        safe_id = declared_id if isinstance(declared_id, str) and _KEBAB.match(declared_id) else fallback_id
        derived = read_derived_outputs(path.parent, safe_id)
    reasons = validate_problem(payload, source, floors, derived_gt=derived_gt, open_shape=open_shape, derived=derived)
    if reasons:
        return Rejection(path.name, problem_id, tuple(reasons))
    return Problem(path.name, source, problem_id, payload)


def _cap_scope(problems: list[Problem]) -> tuple[list[Problem], list[Rejection]]:
    """Keep the first scope problem and reject every later one (check 6)."""
    scope = [problem for problem in problems if problem.payload["difficulty"] == Difficulty.SCOPE.value]
    extra = {problem.file for problem in scope[1:]}
    rejected = [
        Rejection(problem.file, problem.problem_id, (f"only one scope problem is allowed; {scope[0].file} is kept",))
        for problem in scope[1:]
    ]
    return [problem for problem in problems if problem.file not in extra], rejected


def _drop_repeated_ids(problems: list[Problem]) -> tuple[list[Problem], list[Rejection]]:
    """Reject a problem whose id an earlier file of the same source already uses."""
    first_file: dict[tuple[str, str], str] = {}
    kept: list[Problem] = []
    rejected: list[Rejection] = []
    for problem in problems:
        key = (problem.source, problem.problem_id)
        if key in first_file:
            reason = f"problem_id {problem.problem_id!r} is already used by {first_file[key]} from the same source"
            rejected.append(Rejection(problem.file, problem.problem_id, (reason,)))
        else:
            first_file[key] = problem.file
            kept.append(problem)
    return kept, rejected


def _assign_ids(problems: list[Problem]) -> tuple[list[dict[str, Any]], list[Rejection]]:
    """Build output records, prefixing ``<source>-`` onto every problem_id shared across sources."""
    sources_by_id: defaultdict[str, set[str]] = defaultdict(set)
    for problem in problems:
        sources_by_id[problem.problem_id].add(problem.source)
    first_file: dict[str, str] = {}
    records: list[dict[str, Any]] = []
    rejected: list[Rejection] = []
    for problem in problems:
        shared = len(sources_by_id[problem.problem_id]) > 1
        final_id = f"{problem.source}-{problem.problem_id}" if shared else problem.problem_id
        if final_id in first_file:
            reason = f"problem_id {final_id!r} after collision prefixing clashes with {first_file[final_id]}"
            rejected.append(Rejection(problem.file, problem.problem_id, (reason,)))
            continue
        first_file[final_id] = problem.file
        near_misses = problem.payload.get("near_misses") or []
        records.append(
            {**problem.payload, "problem_id": final_id, "source": problem.source, "near_misses": near_misses}
        )
    records.sort(key=lambda record: (record["source"], record["problem_id"]))
    return records, rejected


def _merge(problems: list[Problem]) -> tuple[list[dict[str, Any]], list[Rejection]]:
    """Apply the cross-problem checks to valid problems and build the sorted merged records."""
    ordered = sorted(problems, key=lambda problem: (problem.source, problem.problem_id, problem.file))
    capped, scope_rejected = _cap_scope(ordered)
    unique, repeat_rejected = _drop_repeated_ids(capped)
    records, collision_rejected = _assign_ids(unique)
    return records, [*scope_rejected, *repeat_rejected, *collision_rejected]


def _summary(records: list[dict[str, Any]], rejections: list[Rejection]) -> dict[str, Any]:
    """Count valid problems by source and difficulty and list the rejected ids."""
    by_source = Counter(record["source"] for record in records)
    by_difficulty = Counter(record["difficulty"] for record in records)
    return {
        "valid": len(records),
        "rejected": len(rejections),
        "by_source": {source.value: by_source[source.value] for source in Source},
        "by_difficulty": {tier.value: by_difficulty[tier.value] for tier in Difficulty},
        "rejected_ids": [rejection.problem_id for rejection in rejections],
    }


def _write_json(path: Path, data: object) -> None:
    """Write ``data`` as indented UTF-8 JSON with LF line endings and a trailing newline."""
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")


def run(
    run_dir: Path, floors: Floors, *, derived_gt: bool = False, open_shape: bool = False, boundary: bool = False
) -> int:
    """Validate every problem file in ``run_dir``, write both outputs, and print the summary line.

    Args:
        run_dir: Directory holding the ``problems-<source>-<problem_id>.json`` files.
        floors: Input-length floors for synthetic ``high`` and ``extreme`` problems.
        derived_gt: Skip only the occurrence check (check 2); see :func:`validate_problem`.
        open_shape: Apply the open candidate-pool check (check 7); see :func:`open_shape_reasons`.
        boundary: Apply the boundary check (check 8) to ``high`` and ``extreme`` problems, reading each one's
            ``derived-<problem_id>-<A|B|C>.json`` from ``run_dir``; see :func:`boundary_reasons`.

    Returns:
        Process exit code (see module docstring).

    Examples:
        >>> run(Path("no-such-run-dir"), Floors())  # doctest: +ELLIPSIS
        {"error": "run dir unreadable: ..."}
        1
    """
    try:
        names = sorted(entry.name for entry in run_dir.iterdir() if entry.is_file())
    except OSError as error:
        print(json.dumps({"error": f"run dir unreadable: {error}"}))
        return 1
    valid: list[Problem] = []
    rejections: list[Rejection] = []
    for name in names:
        match = _PROBLEM_FILE.match(name)
        if match is None:
            continue
        loaded = _load(
            run_dir / name,
            match.group(1),
            match.group(2),
            floors,
            derived_gt=derived_gt,
            open_shape=open_shape,
            boundary=boundary,
        )
        if isinstance(loaded, Problem):
            valid.append(loaded)
        else:
            rejections.append(loaded)
    records, merge_rejections = _merge(valid)
    rejections = sorted([*rejections, *merge_rejections], key=lambda rejection: rejection.file)
    try:
        _write_json(run_dir / MERGED_NAME, records)
        _write_json(run_dir / REJECTED_NAME, [asdict(rejection) for rejection in rejections])
    except OSError as error:
        print(json.dumps({"error": f"cannot write outputs: {error}"}))
        return 1
    print(json.dumps(_summary(records, rejections), separators=(",", ":")))
    return 0 if records else 1


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and validate the run dir's problems.

    Args:
        argv: Command-line arguments; ``None`` reads ``sys.argv``.

    Returns:
        Process exit code (see module docstring); argument errors exit 2 through ``argparse``.

    Examples:
        >>> main(["--run-dir", "no-such-run-dir"])  # doctest: +ELLIPSIS
        {"error": "run dir unreadable: ..."}
        1
    """
    parser = argparse.ArgumentParser(
        prog="calibrate_validate_problems.py",
        description="Validate generated calibration problems and merge the valid ones into problems.json.",
    )
    parser.add_argument("--run-dir", required=True, help="directory holding problems-<source>-<problem_id>.json files")
    parser.add_argument(
        "--floors",
        default=None,
        help="input-length floors for synthetic problems, e.g. high=6000,extreme=12000 (defaults shown)",
    )
    parser.add_argument(
        "--derived-gt",
        action="store_true",
        help="skip only the occurrence check: ground truth is computed from the fixture by a deterministic tool",
    )
    parser.add_argument(
        "--open-shape",
        action="store_true",
        help="thread runs: require synthetic problems to render an open candidate pool in the real-fixture row format",
    )
    parser.add_argument(
        "--boundary",
        action="store_true",
        help="vitality runs: require high/extreme problems to put 3 axes one step from another band, read from "
        "derived-<problem_id>-<A|B|C>.json in the run dir",
    )
    args = parser.parse_args(argv)
    try:
        floors = Floors() if args.floors is None else parse_floors(args.floors)
    except ValueError as error:
        parser.error(str(error))
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]
    return run(
        Path(args.run_dir), floors, derived_gt=args.derived_gt, open_shape=args.open_shape, boundary=args.boundary
    )


if __name__ == "__main__":
    sys.exit(main())

"""Score the evolution feature x fix matrix and build the release-level aggregates the report charts read.

Purpose:
    Turn stored blame/overlap evidence into one scored row per fix: a soft share per column (Σ soft = 1 per row) and
    a binary association per real column. Columns are changelog "Added"/"Changed" entries (``feature``/``change``)
    plus pseudo-columns created on demand: ``pre-window@<release>``, ``non-feature@<release>`` and the five residue
    buckets. Scoring is pure: no git, no GitHub, no LLM calls; a rescore only reads stored evidence.

Usage:
    python evolution_score.py score --entries ENTRIES.json --evidence EVIDENCE.jsonl --index INDEX.json \
        [--adjudications ADJUDICATIONS.jsonl] [--overrides OVERRIDES.csv] \
        --out-rows ROWS.jsonl --out-columns COLUMNS.jsonl --out-aggregate AGGREGATE.json

Outputs:
    ``--out-rows`` (one row record per fix, JSONL), ``--out-columns`` (real and used pseudo-column records, JSONL)
    and ``--out-aggregate`` (release-level aggregate, JSON). Stdout carries one summary line.

Exit codes:
    0 success; 2 invalid arguments or unreadable/malformed input files.

Computation order (every run, plan "Scores and views"):

1. Deterministic raw score per real column: a noisy-OR ``raw = 1 - Π(1 - w_i * s_i)`` over the signals
   ``blame`` (unsplit share of blamed lines whose commit is in the column footprint), ``blame-context`` (the same
   for context lines around add-only hunks, which count as ``context-mass`` of a direct line), ``inherited``
   (step 3), ``symbol`` (share of the fix's symbols in the footprint), ``text`` (0/1 validated text reference) and
   ``file`` (share of the fix's files in the footprint). The cell ``method`` is the signal with the largest
   ``w_i * s_i``; ties go to the higher-precedence method in ``METHODS``.
2. Pseudo-columns: a blamed commit in no column footprint and not an earlier fix's commit goes to
   ``non-feature@<R>`` when its global first release ``R`` is at or after the oldest real column release (inside
   the window that has columns), else to ``pre-window@<R>``. A commit on ``HEAD`` but in no release goes to
   ``non-feature@unreleased`` (same-cycle); a commit reachable from no tag and not on ``HEAD`` goes to
   ``pre-window@untagged``. With no real columns every released commit is ``pre-window``. Their raw score is their
   blame share. Residue buckets are decided last (see below).
3. Inheritance: fixes are processed in order of first release, then their order in the entry list. Lines blamed on
   an earlier fix's commit move that mass over the earlier fix's final non-residue soft shares; the inherited raw
   support of a column is ``line share * parent raw``. Method ``inherited``; confidence capped at
   ``0.8 * parent confidence``. A parent with only residue mass sends the lines to ``non-feature@<parent release>``.
   A commit owned by several fixes belongs to the first fix in processing order.
4. LLM verdicts: ``{"columns": {id: confidence}}`` sets each chosen real column's raw score to the confidence
   (method ``llm``) and zeroes the unchosen real columns, keeping pseudo-column mass. ``{"unknown": true}`` zeroes
   every cell, so the row lands on the ``unknown`` residue; ``{"unadjudicated": true}`` does the same for the
   ``unadjudicated`` residue (a row over the per-run LLM cap).
5. Overrides: ``assert`` sets raw 1.0 and ``deny`` raw 0.0 (method ``manual``). Precedence is therefore
   manual > llm > inherited > deterministic. Overrides naming a residue bucket are ignored (residue is derived).
6. Soft view: each cell weighs ``max(1/k-split blame mass, non-blame raw)``; LLM and manual cells weigh
   ``max(blame mass, raw)``; a zero raw score weighs nothing. Weights are normalised per row, so Σ soft equals the
   fix count. Residue: when no other cell carries weight, the row's whole mass goes to one residue bucket: the LLM
   verdict's bucket, else the evidence status (``unlinked``, ``unattributable``, ``timed_out``), else ``unknown``
   (including a fix with no evidence record).
7. Binary view: a real column counts when its final raw score is at or above the threshold and its cell method (the
   strongest signal) is one of ``LINK_METHODS``: blame, blame-context, text, llm or manual. A cell led by symbol
   overlap, file overlap or an inherited share keeps its soft weight but is not a binary link: on labelled
   repositories those methods produced most false links (symbol 7 of 73 correct, inherited 1 of 38, file 0 of 25,
   against blame 16 of 23). Pseudo-columns always carry ``binary: false``.

Evidence use: blamed and context line counts are used only when the evidence status is ``ok``; a fix's own commits
are dropped from both the numerator and the denominator (a later commit of a multi-commit fix blaming an earlier
one is not evidence of what it repairs). Symbol and file overlap are also used only for ``ok`` evidence, so an
``unattributable`` fix touching only tests never associates through file overlap. Text references need no
evidence. The ``heuristic`` method is never assigned to an association cell: deterministic residue cells carry it,
and the fix row's ``source`` records heuristic classification.

Cell confidence: ``min(raw, method cap)`` (caps: blame 1.0, symbol 0.8, text 0.7, blame-context 0.6, file 0.3),
multiplied by the fix's ``link_confidence`` for commit-derived methods; LLM cells carry the verdict confidence,
manual cells 1.0.

Weights and threshold are PROVISIONAL until the Phase 0 real-repo evaluation (``evolution_eval.py``) tunes them;
the manifest and the report must flag them as provisional while that gate is open.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import io
import json
import math
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from _evolution_common import (
    ALGO_VERSIONS,
    KIND_CHANGE,
    KIND_FEATURE,
    KIND_NON_FEATURE,
    KIND_PRE_WINDOW,
    KIND_RESIDUE,
    LINK_METHODS,
    METHODS,
    RESIDUE_COLUMNS,
    SCHEMA_VERSION,
    atomic_write_json,
    canonical_json,
    column_kind,
    digest_lines,
    fingerprint,
    last_by_key,
    non_feature_id,
    pre_window_id,
    read_bounded_bytes,
    read_bounded_text,
    read_jsonl,
)

#: Provisional signal weights, each in [0, 1]; Phase 0 tuning on a labelled repo replaces them.
#: ``context-mass`` is how much one context line counts relative to one directly blamed line.
#: Chosen so the plan's worked example (60% of blamed lines in feature A) links A, while file overlap alone
#: (max 0.2) never reaches the threshold.
DEFAULT_WEIGHTS: Final[dict[str, float]] = {
    "blame": 1.0,
    "blame-context": 0.8,
    "inherited": 1.0,
    "symbol": 0.6,
    "text": 0.7,
    "file": 0.2,
    "context-mass": 0.5,
    # Binary-support factor for sweeping columns (see SWEEPING_FILES); soft mass is not damped.
    "sweeping": 0.25,
}

#: A column whose footprint touches more files than this is a sweeping change (mass refactor, version drop, lint
#: sweep). Blame lands on such commits for almost any later fix (a known SZZ false-positive source), so their blame
#: and file-overlap support is scaled by ``DEFAULT_WEIGHTS["sweeping"]``. Provisional, like the weights.
SWEEPING_FILES: Final = 50

#: Provisional binary threshold on the final raw score: a fix spread evenly over three features (1/3 each) still
#: links all three, and file overlap alone (weight 0.2) never links.
DEFAULT_THRESHOLD: Final = 0.3

#: Most-fixed columns listed in the aggregate (the report's matrix shows the same top 30).
TOP_FEATURES_LIMIT: Final = 30

#: Binary links whose confidence is below this count as low-confidence in the aggregate totals.
LOW_CONFIDENCE: Final = 0.5

#: Release key of commits and entries on HEAD that no release contains yet.
UNRELEASED: Final = "unreleased"

#: Age buckets for shipped associations (per-line chronology).
BUCKET_PREVIOUS: Final = "previous"
BUCKET_RECENT: Final = "2-5"
BUCKET_LEGACY: Final = "legacy"
_RECENT_MAX_BACK: Final = 5

_SIGNALS: Final = ("blame", "blame-context", "inherited", "symbol", "text", "file")
_NON_BLAME: Final = ("symbol", "text", "file")
_COMMIT_METHODS: Final = frozenset({"blame", "blame-context", "symbol", "file"})
_CONFIDENCE_CAP: Final = {"blame": 1.0, "blame-context": 0.6, "symbol": 0.8, "text": 0.7, "file": 0.3}
_INHERIT_FACTOR: Final = 0.8
_METHOD_RANK: Final = {method: rank for rank, method in enumerate(METHODS)}
_VERDICT_METHODS: Final = frozenset({"llm", "manual"})
_RESIDUE_FLAGS: Final = ("unknown", "unadjudicated")
_ACTIONS: Final = ("assert", "deny")
_REAL: Final = "real"

_PSEUDO_TITLES: Final = {
    KIND_PRE_WINDOW: "Code first released in {release}, older than every tracked column",
    KIND_NON_FEATURE: "Non-feature code (refactors, chores) first released in {release}",
    "unlinked": "Fix entries with no reachable commit",
    "unattributable": "Fixes that touch no source lines",
    "timed_out": "Fixes whose blame hit the time budget",
    "unadjudicated": "Ambiguous fixes over the per-run LLM cap",
    "unknown": "Fixes no column evidence reached",
}


# --------------------------------------------------------------------------------------------------------------------
# Release lookups
# --------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _ReleaseBook:
    """Release order, lines and commit-to-release lookups derived from one release index."""

    orders: dict[str, int]
    release_line: dict[str, str]
    prereleases: frozenset[str]
    lines: dict[str, list[str]]
    commit_releases: dict[str, dict[str, str]]
    unreleased: frozenset[str]

    @classmethod
    def from_index(cls, index: dict[str, Any]) -> _ReleaseBook:
        """Build the lookups from an index record; missing sections read as empty."""
        releases = index.get("releases") or []
        head = index.get("head") or {}
        return cls(
            orders={str(rel["version"]): int(rel["order"]) for rel in releases},
            release_line={str(rel["version"]): str(rel.get("line", "")) for rel in releases},
            prereleases=frozenset(str(rel["version"]) for rel in releases if rel.get("is_prerelease")),
            lines={str(line): [str(v) for v in versions] for line, versions in (index.get("lines") or {}).items()},
            commit_releases=dict(index.get("commit_releases") or {}),
            unreleased=frozenset(head.get("unreleased") or ()),
        )

    def order(self, version: str | None) -> float:
        """Return the global order of a release; unreleased and unknown versions sort after every release."""
        if version is None:
            return math.inf
        return self.orders.get(version, math.inf)

    def sort_key(self, version: str | None) -> tuple[float, str]:
        """Return a total sort key: release order, then the version text for unindexed versions."""
        return (self.order(version), version or "")

    def earliest(self, versions: list[str]) -> str | None:
        """Return the version with the smallest release order, or None for an empty list."""
        return min(versions, key=self.sort_key) if versions else None

    def first_release(self, sha: str) -> str | None:
        """Return the global first release containing a commit, or None when no release contains it."""
        return self.earliest(list((self.commit_releases.get(sha) or {}).values()))

    def releases_back(self, origin: str, target: str, line: str) -> int | None:
        """Count releases on ``line`` from ``origin`` (inclusive) up to ``target`` (exclusive) by global order.

        Equals the position difference when both releases lie on the line; a cross-line origin counts the line's
        releases published since it. Returns None when ``origin`` is not an indexed release.
        """
        low = self.order(origin)
        if math.isinf(low):
            return None
        high = self.order(target)
        versions = self.lines.get(line) or list(self.orders)
        return sum(1 for version in versions if low <= self.order(version) < high)


def _age_bucket(book: _ReleaseBook, origin: str, target: str, line: str) -> str:
    """Return the age bucket of a shipped association: previous, 2-5 releases back, or legacy."""
    if origin == KIND_PRE_WINDOW:
        return BUCKET_LEGACY
    back = book.releases_back(origin, target, line)
    if back is None or back > _RECENT_MAX_BACK:
        return BUCKET_LEGACY
    return BUCKET_PREVIOUS if back <= 1 else BUCKET_RECENT


# --------------------------------------------------------------------------------------------------------------------
# Columns and the SHA map
# --------------------------------------------------------------------------------------------------------------------


def _column_entries(entries: dict[str, Any] | list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the feature/change entries from a ``build_entries`` result or a plain entry list."""
    items = entries.get("columns", []) if isinstance(entries, dict) else entries
    return [entry for entry in items if entry.get("kind") in (KIND_FEATURE, KIND_CHANGE)]


def _line_introductions(entry: dict[str, Any], commits: list[str], book: _ReleaseBook) -> dict[str, str]:
    """Return the first release per line containing any footprint commit, merged with the entry's own release."""
    intro: dict[str, str] = {}
    candidates = [(line, version) for sha in commits for line, version in (book.commit_releases.get(sha) or {}).items()]
    release = entry.get("release")
    if release in book.orders:
        candidates.append((entry.get("line") or book.release_line[release], release))
    for line, version in candidates:
        current = intro.get(line)
        if current is None or book.sort_key(version) < book.sort_key(current):
            intro[line] = version
    return dict(sorted(intro.items()))


def build_columns(entries: dict[str, Any] | list[dict[str, Any]], index: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the real column records (features and behaviour changes) with their per-line chronology.

    The global ``release`` is the earliest release (by index order) among the per-line introductions; a column with
    no released footprint and an unreleased or unindexed entry keeps the entry's release text. Pseudo-columns are not
    built here: scoring creates them on demand and :func:`pseudo_columns` describes the ones a run used.

    Args:
        entries: The ``build_entries`` result (its ``columns`` list is used) or a list of entries; fix entries are
            ignored.
        index: Release index from ``evolution_releases.build_release_index``.

    Returns:
        Column records sorted by release order, then column ID; a repeated column ID keeps its first entry.

    Raises:
        ValueError: an entry has no ``entry_id``.

    Examples:
        >>> index = {"releases": [{"version": "1.0", "order": 0, "line": "1"}], "commit_releases": {"a": {"1": "1.0"}}}
        >>> entry = {"entry_id": "1.0/x", "kind": "feature", "release": "1.0", "line": "1", "commits": ["a"]}
        >>> column = build_columns([entry], index)[0]
        >>> column["release"], column["line_introductions"], column["footprint"]["commits"]
        ('1.0', {'1': '1.0'}, ['a'])
    """
    book = _ReleaseBook.from_index(index)
    columns: dict[str, dict[str, Any]] = {}
    for entry in _column_entries(entries):
        column_id = entry.get("entry_id")
        if not column_id:
            raise ValueError(f"column entry without entry_id: {entry.get('text', '')[:80]!r}")
        if column_id not in columns:
            columns[column_id] = _column_record(entry, book)
    return sorted(columns.values(), key=lambda col: (book.sort_key(col["release"]), col["column_id"]))


def _column_record(entry: dict[str, Any], book: _ReleaseBook) -> dict[str, Any]:
    """Assemble one real column record from its entry."""
    commits = list(dict.fromkeys(entry.get("commits") or []))
    intro = _line_introductions(entry, commits, book)
    release = book.earliest(list(intro.values())) or str(entry.get("release") or UNRELEASED)
    footprint = {
        "commits": commits,
        "files": sorted(set(entry.get("files") or [])),
        "symbols": sorted(set(entry.get("symbols") or [])),
    }
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "column_id": entry["entry_id"],
        "kind": entry["kind"],
        "release": release,
        "line": entry.get("line") or book.release_line.get(release, ""),
        "line_introductions": intro,
        "title": str(entry.get("text", "")),
        "footprint": footprint,
        "footprint_size": len(commits),
        "aliases": sorted(set(entry.get("aliases") or [])),
        "source": entry.get("source"),
        "link_method": entry.get("link_method"),
        "link_confidence": entry.get("link_confidence"),
        "entry_release": entry.get("release"),
    }
    record["fingerprint"] = fingerprint("column", ALGO_VERSIONS["column_map"], record)
    return record


def _fix_list(fix_entries: dict[str, Any] | list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the fix entries from a ``build_entries`` result or a plain entry list."""
    return list(fix_entries.get("fixes", [])) if isinstance(fix_entries, dict) else list(fix_entries)


def _ordered_fixes(fix_entries: dict[str, Any] | list[dict[str, Any]], book: _ReleaseBook) -> list[dict[str, Any]]:
    """Return fix entries in processing order (first release, then list order), dropping repeated fix IDs.

    Raises:
        ValueError: an entry has no ``entry_id``.
    """
    keyed: list[tuple[tuple[float, str], int, dict[str, Any]]] = []
    seen: set[str] = set()
    for position, entry in enumerate(_fix_list(fix_entries)):
        fix_id = entry.get("entry_id")
        if not fix_id:
            raise ValueError(f"fix entry without entry_id: {str(entry.get('text', ''))[:80]!r}")
        if fix_id in seen:
            continue
        seen.add(fix_id)
        keyed.append((book.sort_key(entry.get("release")), position, entry))
    return [entry for _, _, entry in sorted(keyed, key=lambda item: (item[0], item[1]))]


def _fix_owners(ordered_fixes: list[dict[str, Any]]) -> dict[str, str]:
    """Map each fix commit to the first fix (in processing order) that lists it."""
    owners: dict[str, str] = {}
    for entry in ordered_fixes:
        for sha in entry.get("commits") or []:
            owners.setdefault(sha, entry["entry_id"])
    return owners


def build_sha_map(
    columns: list[dict[str, Any]], index: dict[str, Any], fix_entries: dict[str, Any] | list[dict[str, Any]]
) -> dict[str, Any]:
    """Build the derived SHA maps: footprint commit to columns, and fix commit to its owning fix.

    The map is rebuilt on every run from the column records, so remapping never needs blame again.

    Args:
        columns: Column records; only real columns contribute footprint commits.
        index: Release index (its fingerprint and release order are used).
        fix_entries: Fix entries; a commit shared by several fixes belongs to the first in processing order.

    Returns:
        ``{"columns": {sha: [column_id, ...]}, "fixes": {sha: fix_id}, "fingerprint": str}``.

    Examples:
        >>> cols = [{"column_id": "1.0/a", "footprint": {"commits": ["c1"]}}]
        >>> fixes = [{"entry_id": "1.1/f", "release": "1.1", "commits": ["c2"]}]
        >>> sha_map = build_sha_map(cols, {"releases": []}, fixes)
        >>> sha_map["columns"], sha_map["fixes"]
        ({'c1': ['1.0/a']}, {'c2': '1.1/f'})
    """
    book = _ReleaseBook.from_index(index)
    by_sha: dict[str, set[str]] = {}
    for column_id, column in _real_columns(columns).items():
        for sha in (column.get("footprint") or {}).get("commits") or []:
            by_sha.setdefault(sha, set()).add(column_id)
    columns_out = {sha: sorted(ids) for sha, ids in sorted(by_sha.items())}
    fixes_out = dict(sorted(_fix_owners(_ordered_fixes(fix_entries, book)).items()))
    digest = fingerprint("column_map", ALGO_VERSIONS["column_map"], index.get("fingerprint"), columns_out, fixes_out)
    return {"columns": columns_out, "fixes": fixes_out, "fingerprint": digest}


def pseudo_columns(rows: list[dict[str, Any]], index: dict[str, Any]) -> list[dict[str, Any]]:
    """Describe the pseudo-columns (pre-window, non-feature, residue) that scored rows use, as column records.

    Examples:
        >>> rows = [{"cells": [{"column_id": "unknown"}, {"column_id": "pre-window@1.0"}]}]
        >>> [(col["column_id"], col["kind"]) for col in pseudo_columns(rows, {"releases": []})]
        [('pre-window@1.0', 'pre-window'), ('unknown', 'residue')]
    """
    book = _ReleaseBook.from_index(index)
    used = sorted({cell["column_id"] for row in rows for cell in row.get("cells", [])})
    records = []
    for column_id in used:
        kind = column_kind(column_id)
        if kind == _REAL:
            continue
        release = None if kind == KIND_RESIDUE else column_id.split("@", 1)[1]
        title_key = column_id if kind == KIND_RESIDUE else kind
        line = book.release_line.get(release or "")
        record: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "column_id": column_id,
            "kind": kind,
            "release": release,
            "line": line or "",
            "line_introductions": {line: release} if line and release else {},
            "title": _PSEUDO_TITLES[title_key].format(release=release),
            "footprint": {"commits": [], "files": [], "symbols": []},
            "footprint_size": 0,
            "aliases": [],
        }
        record["fingerprint"] = fingerprint("column", ALGO_VERSIONS["column_map"], record)
        records.append(record)
    return records


# --------------------------------------------------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------------------------------------------------


@dataclass
class _Draft:
    """Working state of one cell while its fix row is scored."""

    column_id: str
    signals: dict[str, float] = field(default_factory=dict)
    mass: float = 0.0
    inherited_mass: float = 0.0
    inherit_cap: float = 0.0
    raw: float = 0.0
    nonblame: float = 0.0
    method: str = "blame"
    confidence: float = 0.0

    def add(self, signal: str, value: float) -> None:
        """Accumulate support for one signal."""
        self.signals[signal] = self.signals.get(signal, 0.0) + value

    def set_final(self, raw: float, method: str, confidence: float) -> None:
        """Replace the cell's score with an LLM or manual decision."""
        self.raw, self.method, self.confidence = raw, method, confidence


@dataclass(frozen=True)
class _ParentView:
    """Final non-residue view of a scored fix, read by later fixes that blame its commits."""

    fix_release: str
    cells: dict[str, tuple[float, float, float]]  # column_id -> (soft share, raw score, confidence)
    score_fp: str


def _draft(drafts: dict[str, _Draft], column_id: str) -> _Draft:
    """Return the draft for a column, creating it on first use."""
    if column_id not in drafts:
        drafts[column_id] = _Draft(column_id)
    return drafts[column_id]


def _clamp(value: float) -> float:
    """Clamp a number into [0, 1]."""
    return min(max(float(value), 0.0), 1.0)


def _noisy_or(terms: list[float]) -> float:
    """Combine independent supports in [0, 1] into one bounded, monotone score."""
    product = 1.0
    for term in terms:
        product *= 1.0 - _clamp(term)
    return 1.0 - product


def _confidence(method: str, raw: float, link_confidence: float, inherit_cap: float) -> float:
    """Return a cell's confidence from its method cap, raw score and the fix's linkage confidence."""
    cap = inherit_cap if method == "inherited" else _CONFIDENCE_CAP[method]
    value = min(raw, cap)
    return value * link_confidence if method in _COMMIT_METHODS else value


def _finish_real(draft: _Draft, weights: dict[str, float], link_confidence: float) -> None:
    """Compute a real column's deterministic raw score, method and confidence from its signals."""
    contributions = {
        signal: weights[signal] * _clamp(draft.signals[signal]) for signal in _SIGNALS if signal in draft.signals
    }
    contributions = {signal: value for signal, value in contributions.items() if value > 0}
    if not contributions:
        return
    draft.raw = _noisy_or(list(contributions.values()))
    draft.nonblame = _noisy_or([contributions[signal] for signal in _NON_BLAME if signal in contributions])
    draft.method = max(contributions, key=lambda signal: (contributions[signal], _METHOD_RANK[signal]))
    draft.confidence = _confidence(draft.method, draft.raw, link_confidence, draft.inherit_cap)


def _finish_pseudo(draft: _Draft, link_confidence: float) -> None:
    """Compute a pseudo-column's raw score (its blame share), method and confidence."""
    draft.raw = _clamp(draft.mass)
    own = draft.mass - draft.inherited_mass
    if draft.inherited_mass > own:
        draft.method = "inherited"
    elif draft.signals.get("blame", 0.0) >= draft.signals.get("blame-context", 0.0):
        draft.method = "blame"
    else:
        draft.method = "blame-context"
    draft.confidence = _confidence(draft.method, draft.raw, link_confidence, draft.inherit_cap)


def _soft_weight(draft: _Draft) -> float:
    """Return a cell's un-normalised soft weight (step 6)."""
    if draft.raw <= 0:
        return 0.0
    if draft.method in _VERDICT_METHODS:
        return max(draft.mass, draft.raw)
    if column_kind(draft.column_id) == _REAL:
        return max(draft.mass, draft.nonblame)
    return draft.mass


def _line_counts(blamed: dict[str, int] | None, own: set[str], scale: float) -> dict[str, float]:
    """Return scaled positive line counts per blamed commit, skipping the fix's own commits."""
    return {sha: scale * float(count) for sha, count in (blamed or {}).items() if sha not in own and count > 0}


def _blame_shares(
    evidence: dict[str, Any] | None, own: set[str], context_mass: float
) -> dict[str, tuple[float, float]]:
    """Return ``{sha: (direct share, context share)}`` of a fix's blamed lines, own commits excluded.

    Shares are relative to ``direct lines + context_mass * context lines``; empty unless the evidence status is ok.
    """
    if not evidence or evidence.get("status") != "ok":
        return {}
    direct = _line_counts(evidence.get("blamed"), own, 1.0)
    context = _line_counts(evidence.get("context_blamed"), own, context_mass)
    total = sum(direct.values()) + sum(context.values())
    if total <= 0:
        return {}
    shas = list(dict.fromkeys([*direct, *context]))
    return {sha: (direct.get(sha, 0.0) / total, context.get(sha, 0.0) / total) for sha in shas}


def blame_only_on_pseudo(
    entry: dict[str, Any],
    evidence: dict[str, Any] | None,
    sha_map: dict[str, Any],
    weights: dict[str, float] | None = None,
) -> bool:
    """Report whether a fix's blamed lines all land on pseudo-columns, so no feature column can explain them.

    Such a row has a deterministic answer already: its lines predate the window or belong to a commit no feature
    owns. Overlap signals (file, symbol) can still give a real column a weak score, but an adjudicator asked to pick
    among those columns can only answer ``unknown``, which would move the row from its pseudo bucket to the
    ``unknown`` residue. Callers use this to keep such rows out of adjudication.

    Args:
        entry: The fix entry; its own ``commits`` are excluded from the blamed lines.
        evidence: The fix's blame evidence record, or ``None``.
        sha_map: The :func:`build_sha_map` result (``columns`` and ``fixes`` maps).
        weights: Signal weights merged over :data:`DEFAULT_WEIGHTS`; only ``context-mass`` is read.

    Returns:
        ``True`` when at least one line is blamed and no blamed commit is in a column footprint or owned by another
        fix (whose inherited shares could reach a real column); ``False`` otherwise, including with no blamed lines.

    Examples:
        >>> sha_map = {"columns": {"a1": ["1.1/A"]}, "fixes": {}}
        >>> fix = {"entry_id": "1.3/F", "commits": ["f"]}
        >>> blame_only_on_pseudo(fix, {"status": "ok", "blamed": {"p1": 4}, "context_blamed": {}}, sha_map)
        True
        >>> blame_only_on_pseudo(fix, {"status": "ok", "blamed": {"p1": 4, "a1": 1}, "context_blamed": {}}, sha_map)
        False
    """
    own = set(entry.get("commits") or [])
    blamed = _blame_shares(evidence, own, _resolve_weights(weights)["context-mass"])
    columns, fixes = sha_map.get("columns") or {}, sha_map.get("fixes") or {}
    return bool(blamed) and not any(sha in columns or sha in fixes for sha in blamed)


def _verdict_of(adjudication: dict[str, Any] | None) -> tuple[dict[str, Any] | None, str | None]:
    """Split an adjudication (a stored record or a bare verdict) into ``(verdict, reuse key)``."""
    if not adjudication:
        return None, None
    if isinstance(adjudication.get("verdict"), dict):
        verdict = adjudication["verdict"]
        return verdict, str(adjudication.get("key") or fingerprint(verdict))
    return adjudication, fingerprint(adjudication)


def _residue_bucket(evidence: dict[str, Any] | None) -> str:
    """Return the residue bucket a row with no other evidence falls into, from its evidence status."""
    status = (evidence or {}).get("status")
    return status if status in RESIDUE_COLUMNS else "unknown"


def _entry_digest(entry: dict[str, Any]) -> str:
    """Fingerprint the fix-entry fields scoring reads."""
    keys = ("release", "line", "source", "commits", "text_refs", "link_confidence", "secondary_releases")
    return fingerprint({key: entry.get(key) for key in keys})


@dataclass
class _Scorer:
    """Scores fixes one at a time in processing order, keeping each finished fix's view for inheritance."""

    book: _ReleaseBook
    columns: dict[str, dict[str, Any]]
    sha_columns: dict[str, list[str]]
    sha_fix: dict[str, str]
    aliases: dict[str, str]
    file_index: dict[str, list[str]]
    symbol_index: dict[str, list[str]]
    window_start: float
    weights: dict[str, float]
    threshold: float
    map_fp: str
    sweeping: frozenset[str] = frozenset()
    finished: dict[str, _ParentView] = field(default_factory=dict)

    def score(
        self,
        entry: dict[str, Any],
        evidence: dict[str, Any] | None,
        adjudication: dict[str, Any] | None,
        overrides: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Score one fix through steps 1-7 and return its row record."""
        fix_id = str(entry["entry_id"])
        fix_release = str(entry.get("release") or UNRELEASED)
        line = str(entry.get("line") or self.book.release_line.get(fix_release, ""))
        drafts: dict[str, _Draft] = {}
        parents = self._add_blame(drafts, entry, evidence)
        self._add_inherited(drafts, parents)
        self._add_overlap(drafts, entry, evidence)
        link_confidence = 1.0 if entry.get("link_confidence") is None else _clamp(entry["link_confidence"])
        for draft in drafts.values():
            if column_kind(draft.column_id) == _REAL:
                _finish_real(draft, self.weights, link_confidence)
            else:
                _finish_pseudo(draft, link_confidence)
        verdict, adjudication_key = _verdict_of(adjudication)
        hint = self._apply_verdict(drafts, verdict)
        self._apply_overrides(drafts, overrides)
        cells, view = self._views(drafts, hint or _residue_bucket(evidence), hint is not None, fix_release, line)
        score_fp = fingerprint(
            "scores",
            ALGO_VERSIONS["scores"],
            (evidence or {}).get("fingerprint", ""),
            digest_lines([canonical_json(evidence)] if evidence else []),
            self.map_fp,
            _entry_digest(entry),
            [[parent, self.finished[parent].score_fp] for parent in sorted(parents)],
            self.weights,
            self.threshold,
            [[item["column_id"], item["action"]] for item in overrides],
            adjudication_key,
        )
        self.finished[fix_id] = _ParentView(fix_release, view, score_fp)
        return _row_record(entry, evidence, fix_release, line, cells, score_fp)

    def _pseudo_for(self, sha: str) -> str:
        """Return the pseudo-column of a blamed commit that is in no column footprint and owned by no earlier fix."""
        version = self.book.first_release(sha)
        if version is None:
            return non_feature_id(UNRELEASED) if sha in self.book.unreleased else pre_window_id(None)
        if self.book.order(version) < self.window_start:
            return pre_window_id(version)
        return non_feature_id(version)

    def _add_blame(
        self, drafts: dict[str, _Draft], entry: dict[str, Any], evidence: dict[str, Any] | None
    ) -> dict[str, float]:
        """Route blamed-line shares to columns, pseudo-columns or earlier fixes; return the per-parent line share."""
        own = set(entry.get("commits") or [])
        parents: dict[str, float] = {}
        for sha, (direct, context) in _blame_shares(evidence, own, self.weights["context-mass"]).items():
            targets = self.sha_columns.get(sha)
            parent = self.sha_fix.get(sha)
            if targets:
                for column_id in targets:
                    factor = self.weights.get("sweeping", 1.0) if column_id in self.sweeping else 1.0
                    draft = _draft(drafts, column_id)
                    draft.add("blame", direct * factor)
                    draft.add("blame-context", context * factor)
                    draft.mass += (direct + context) / len(targets)
            elif parent is not None and parent in self.finished:
                parents[parent] = parents.get(parent, 0.0) + direct + context
            else:
                draft = _draft(drafts, self._pseudo_for(sha))
                draft.add("blame", direct)
                draft.add("blame-context", context)
                draft.mass += direct + context
        return parents

    def _add_inherited(self, drafts: dict[str, _Draft], parents: dict[str, float]) -> None:
        """Redistribute lines blamed on earlier fixes over those fixes' final soft shares (step 3)."""
        for parent_id in sorted(parents):
            share = parents[parent_id]
            view = self.finished[parent_id]
            total = sum(soft for soft, _, _ in view.cells.values())
            if total <= 0:
                draft = _draft(drafts, non_feature_id(view.fix_release))
                draft.add("inherited", share)
                draft.mass += share
                draft.inherited_mass += share
                continue
            for column_id in sorted(view.cells):
                soft, raw, confidence = view.cells[column_id]
                draft = _draft(drafts, column_id)
                moved = share * soft / total
                draft.mass += moved
                draft.inherited_mass += moved
                draft.add("inherited", share * raw)
                draft.inherit_cap = max(draft.inherit_cap, confidence * _INHERIT_FACTOR)

    def _add_overlap(self, drafts: dict[str, _Draft], entry: dict[str, Any], evidence: dict[str, Any] | None) -> None:
        """Add text-link, symbol-overlap and file-overlap signals to real columns."""
        for ref in entry.get("text_refs") or []:
            column_id = self.aliases.get(ref, ref)
            if column_id in self.columns:
                _draft(drafts, column_id).signals["text"] = 1.0
        if evidence and evidence.get("status") == "ok":
            self._add_share(drafts, "symbol", evidence.get("symbols"), self.symbol_index)
            self._add_share(drafts, "file", evidence.get("files"), self.file_index)
            factor = self.weights.get("sweeping", 1.0)
            for column_id in self.sweeping & drafts.keys():
                if "file" in drafts[column_id].signals:
                    drafts[column_id].signals["file"] *= factor

    @staticmethod
    def _add_share(
        drafts: dict[str, _Draft], signal: str, items: list[str] | None, lookup: dict[str, list[str]]
    ) -> None:
        """Set ``signal`` to the share of the fix's items that each column's footprint contains."""
        unique = sorted(set(items or []))
        counts: dict[str, int] = {}
        for item in unique:
            for column_id in lookup.get(item, ()):
                counts[column_id] = counts.get(column_id, 0) + 1
        for column_id in sorted(counts):
            _draft(drafts, column_id).signals[signal] = counts[column_id] / len(unique)

    def _apply_verdict(self, drafts: dict[str, _Draft], verdict: dict[str, Any] | None) -> str | None:
        """Apply an LLM verdict (step 4); return the residue bucket it forces, if any."""
        if verdict is None:
            return None
        for flag in _RESIDUE_FLAGS:
            if verdict.get(flag):
                for draft in drafts.values():
                    draft.set_final(0.0, "llm", 0.0)
                return flag
        chosen: dict[str, float] = {}
        for column_id, confidence in (verdict.get("columns") or {}).items():
            canonical = self.aliases.get(column_id, column_id)
            if canonical in self.columns:
                chosen[canonical] = _clamp(confidence)
        for draft in drafts.values():
            if column_kind(draft.column_id) == _REAL and draft.column_id not in chosen:
                draft.set_final(0.0, "llm", 0.0)
        for column_id in sorted(chosen):
            _draft(drafts, column_id).set_final(chosen[column_id], "llm", chosen[column_id])
        return None

    def _apply_overrides(self, drafts: dict[str, _Draft], overrides: list[dict[str, Any]]) -> None:
        """Apply user overrides (step 5): ``assert`` sets raw 1.0, ``deny`` sets raw 0.0; the last one per cell wins."""
        for override in overrides:
            column_id = self.aliases.get(override["column_id"], override["column_id"])
            kind = column_kind(column_id)
            if kind == KIND_RESIDUE or (kind == _REAL and column_id not in self.columns):
                continue
            _draft(drafts, column_id).set_final(1.0 if override["action"] == "assert" else 0.0, "manual", 1.0)

    def _views(
        self, drafts: dict[str, _Draft], residue: str, by_verdict: bool, fix_release: str, line: str
    ) -> tuple[list[dict[str, Any]], dict[str, tuple[float, float, float]]]:
        """Derive the soft and binary views (steps 6-7); return the cells and the view later fixes inherit."""
        weights = {column_id: _soft_weight(draft) for column_id, draft in drafts.items()}
        total = sum(weights.values())
        cells: list[dict[str, Any]] = []
        view: dict[str, tuple[float, float, float]] = {}
        if total <= 0:
            method = "llm" if by_verdict else "heuristic"
            cells.append(
                _cell(_Draft(residue, raw=1.0, method=method, confidence=1.0), (fix_release, False), 1.0, False)
            )
        for column_id in sorted(drafts):
            draft = drafts[column_id]
            soft = weights[column_id] / total if total > 0 else 0.0
            real = column_kind(column_id) == _REAL
            if soft <= 0 and not (real and draft.method in _VERDICT_METHODS):
                continue
            origin = self._cell_release(column_id, line, fix_release)
            linked = real and draft.method in LINK_METHODS and draft.raw >= self.threshold
            cells.append(_cell(draft, origin, soft, linked))
            if soft > 0:
                view[column_id] = (soft, draft.raw, draft.confidence)
        return sorted(cells, key=lambda cell: cell["column_id"]), view

    def _cell_release(self, column_id: str, line: str, fix_release: str) -> tuple[str | None, bool]:
        """Return a cell's origin release on the fix's line and whether it fell back to the global release."""
        kind = column_kind(column_id)
        if kind == KIND_RESIDUE:
            return fix_release, False
        if kind != _REAL:
            release = column_id.split("@", 1)[1]
            release_line = self.book.release_line.get(release)
            return release, release_line is not None and release_line != line
        column = self.columns[column_id]
        intro = column.get("line_introductions") or {}
        if line in intro:
            return intro[line], False
        return column.get("release"), bool(intro)


def _cell(draft: _Draft, origin: tuple[str | None, bool], soft: float, binary: bool) -> dict[str, Any]:
    """Build one cell record from a finished draft and its ``(release, cross_line)`` origin."""
    release, cross_line = origin
    return {
        "column_id": draft.column_id,
        "release": release,
        "raw_score": draft.raw,
        "soft_share": soft,
        "binary": binary,
        "method": draft.method,
        "confidence": draft.confidence,
        "cross_line": cross_line,
    }


def _row_record(
    entry: dict[str, Any],
    evidence: dict[str, Any] | None,
    fix_release: str,
    line: str,
    cells: list[dict[str, Any]],
    score_fp: str,
) -> dict[str, Any]:
    """Build one row record; the evidence count and digest bind the row to its single evidence line."""
    evidence_lines = [canonical_json(evidence)] if evidence else []
    return {
        "schema_version": SCHEMA_VERSION,
        "fix_id": entry["entry_id"],
        "fix_release": fix_release,
        "line": line,
        "source": entry.get("source"),
        "secondary_releases": list(entry.get("secondary_releases") or []),
        "cells": cells,
        "evidence_count": len(evidence_lines),
        "evidence_digest": digest_lines(evidence_lines),
        "fingerprints": {"evidence": (evidence or {}).get("fingerprint", ""), "scores": score_fp},
        "status": (evidence or {}).get("status", "missing"),
    }


def _resolve_weights(weights: dict[str, float] | None) -> dict[str, float]:
    """Merge caller weights over the defaults, validating names and ranges.

    Raises:
        ValueError: an unknown weight name, or a value outside [0, 1].
    """
    merged = dict(DEFAULT_WEIGHTS)
    for name, value in (weights or {}).items():
        if name not in DEFAULT_WEIGHTS:
            raise ValueError(f"unknown weight {name!r}; expected one of {sorted(DEFAULT_WEIGHTS)}")
        number = float(value)
        if not 0.0 <= number <= 1.0:
            raise ValueError(f"weight {name!r}={number} is outside [0, 1]")
        merged[name] = number
    return merged


def _resolve_threshold(threshold: float | None) -> float:
    """Return the binary threshold, validating it lies in (0, 1].

    Raises:
        ValueError: the threshold is not in (0, 1].
    """
    value = DEFAULT_THRESHOLD if threshold is None else float(threshold)
    if not 0.0 < value <= 1.0:
        raise ValueError(f"threshold {value} is outside (0, 1]")
    return value


def _overrides_by_fix(overrides: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group overrides by fix ID, keeping file order.

    Raises:
        ValueError: an override has an action other than ``assert`` or ``deny``.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for override in overrides:
        if override.get("action") not in _ACTIONS:
            raise ValueError(f"override action {override.get('action')!r} is not one of {list(_ACTIONS)}")
        grouped.setdefault(str(override["fix_id"]), []).append(override)
    return grouped


def _real_columns(columns: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index the real (feature/change) column records by ID, skipping pseudo-column records."""
    return {column["column_id"]: column for column in columns if column_kind(column["column_id"]) == _REAL}


def _alias_map(columns: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Map every backport alias ID to its canonical column ID."""
    return {alias: column_id for column_id, column in columns.items() for alias in column.get("aliases") or []}


def _index_footprints(columns: dict[str, dict[str, Any]], part: str) -> dict[str, list[str]]:
    """Map each footprint file or symbol to the sorted column IDs containing it."""
    lookup: dict[str, set[str]] = {}
    for column_id, column in columns.items():
        for item in (column.get("footprint") or {}).get(part) or []:
            lookup.setdefault(item, set()).add(column_id)
    return {item: sorted(ids) for item, ids in lookup.items()}


def _make_scorer(
    columns: list[dict[str, Any]],
    sha_map: dict[str, Any],
    fixes: list[dict[str, Any]],
    index: dict[str, Any],
    weights: dict[str, float],
    threshold: float,
) -> _Scorer:
    """Build the scorer state: real columns, SHA maps, alias and overlap lookups, and the column-map fingerprint."""
    book = _ReleaseBook.from_index(index)
    real = _real_columns(columns)
    structured = isinstance(sha_map.get("columns"), dict)
    column_map = sha_map["columns"] if structured else sha_map
    fix_map = sha_map.get("fixes") if structured else None
    if fix_map is None:
        fix_map = _fix_owners(fixes)
    map_fp = fingerprint(
        sha_map.get("fingerprint") if structured else fingerprint(column_map, fix_map),
        sorted([column_id, column.get("fingerprint", "")] for column_id, column in real.items()),
        index.get("fingerprint"),
    )
    return _Scorer(
        book=book,
        columns=real,
        sha_columns={sha: [cid for cid in ids if cid in real] for sha, ids in column_map.items()},
        sha_fix=dict(fix_map),
        aliases=_alias_map(real),
        file_index=_index_footprints(real, "files"),
        symbol_index=_index_footprints(real, "symbols"),
        window_start=min((book.order(column.get("release")) for column in real.values()), default=math.inf),
        weights=weights,
        threshold=threshold,
        map_fp=map_fp,
        sweeping=frozenset(
            column_id
            for column_id, column in real.items()
            if len((column.get("footprint") or {}).get("files") or []) > SWEEPING_FILES
        ),
    )


def score_rows(
    fix_entries: dict[str, Any] | list[dict[str, Any]],
    evidence_by_fix: dict[str, dict[str, Any]],
    columns: list[dict[str, Any]],
    sha_map: dict[str, Any],
    index: dict[str, Any],
    adjudications: dict[str, dict[str, Any]] | None = None,
    overrides: list[dict[str, Any]] | None = None,
    weights: dict[str, float] | None = None,
    threshold: float | None = None,
) -> list[dict[str, Any]]:
    """Score every fix row through the plan's seven-step computation order (see the module docstring).

    Args:
        fix_entries: Fix entries (or the ``build_entries`` result, whose ``fixes`` list is used).
        evidence_by_fix: Evidence record per fix ID; a missing record means no evidence (``unknown`` residue unless a
            text reference links a column).
        columns: Column records from :func:`build_columns` (pseudo-column records, if present, are ignored).
        sha_map: The :func:`build_sha_map` result; a bare ``{sha: [column_id]}`` map is accepted too, in which case
            fix ownership is derived from ``fix_entries``.
        index: Release index.
        adjudications: Per fix ID, a stored adjudication record (its ``verdict`` and ``key`` are used) or a bare
            verdict; the caller has already checked reuse-key equality.
        overrides: Parsed override rows ``{fix_id, column_id, action, note}``; later rows win per cell.
        weights: Signal weights merged over :data:`DEFAULT_WEIGHTS`.
        threshold: Binary threshold on the final raw score (default :data:`DEFAULT_THRESHOLD`).

    Returns:
        Row records in processing order (first release, then entry order).

    Raises:
        ValueError: invalid weights, threshold or override action, or an entry without ``entry_id``.

    Examples:
        >>> releases = [{"version": v, "order": i, "line": "1"} for i, v in enumerate(["1.0", "1.1"])]
        >>> index = {"releases": releases, "lines": {"1": ["1.0", "1.1"]}, "commit_releases": {"a": {"1": "1.0"}}}
        >>> cols = build_columns([{"entry_id": "1.0/a", "kind": "feature", "release": "1.0", "commits": ["a"]}], index)
        >>> fix = {"entry_id": "1.1/f", "release": "1.1", "line": "1", "commits": ["f"]}
        >>> evidence = {"1.1/f": {"fix_id": "1.1/f", "status": "ok", "blamed": {"a": 3}, "context_blamed": {}}}
        >>> row = score_rows([fix], evidence, cols, build_sha_map(cols, index, [fix]), index)[0]
        >>> [(c["column_id"], c["soft_share"], c["binary"]) for c in row["cells"]]
        [('1.0/a', 1.0, True)]
    """
    book = _ReleaseBook.from_index(index)
    ordered = _ordered_fixes(fix_entries, book)
    scorer = _make_scorer(columns, sha_map, ordered, index, _resolve_weights(weights), _resolve_threshold(threshold))
    verdicts = adjudications or {}
    by_fix = _overrides_by_fix(overrides or [])
    return [
        scorer.score(
            entry,
            evidence_by_fix.get(entry["entry_id"]),
            verdicts.get(entry["entry_id"]),
            by_fix.get(entry["entry_id"], []),
        )
        for entry in ordered
    ]


# --------------------------------------------------------------------------------------------------------------------
# Aggregates
# --------------------------------------------------------------------------------------------------------------------


@dataclass
class _Aggregator:
    """Accumulates release-level views from scored rows."""

    book: _ReleaseBook
    columns: dict[str, dict[str, Any]]
    flow: dict[tuple[str, str, str], float] = field(default_factory=dict)
    residue: dict[str, dict[str, float]] = field(default_factory=dict)
    same_cycle: list[dict[str, Any]] = field(default_factory=list)
    fix_counts: dict[str, int] = field(default_factory=dict)
    soft_sum: float = 0.0
    residue_sum: float = 0.0
    binary_links: int = 0
    low_confidence: int = 0

    def add_row(self, row: dict[str, Any]) -> None:
        """Fold one row's cells into the views."""
        for cell in row.get("cells", []):
            self._add_cell(row, cell)

    def _add_cell(self, row: dict[str, Any], cell: dict[str, Any]) -> None:
        """Route one cell to the binary counts, the residue table, the same-cycle panel or the flow."""
        column_id = cell["column_id"]
        kind = column_kind(column_id)
        soft = float(cell.get("soft_share") or 0.0)
        target = str(row.get("fix_release") or UNRELEASED)
        self.soft_sum += soft
        if cell.get("binary") and kind == _REAL:
            self.fix_counts[column_id] = self.fix_counts.get(column_id, 0) + 1
            self.binary_links += 1
            self.low_confidence += int(float(cell.get("confidence") or 0.0) < LOW_CONFIDENCE)
        if soft <= 0:
            return
        if kind == KIND_RESIDUE:
            bucket = self.residue.setdefault(target, {})
            bucket[column_id] = bucket.get(column_id, 0.0) + soft
            self.residue_sum += soft
            return
        origin, first = self._origin(column_id, kind, cell)
        if first is not None and first == target:
            self.same_cycle.append(
                {
                    "fix_id": row["fix_id"],
                    "column_id": column_id,
                    "release": target,
                    "weight": soft,
                    "binary": bool(cell.get("binary")),
                }
            )
            return
        key = (origin, target, _age_bucket(self.book, origin, target, str(row.get("line", ""))))
        self.flow[key] = self.flow.get(key, 0.0) + soft

    def _origin(self, column_id: str, kind: str, cell: dict[str, Any]) -> tuple[str, str | None]:
        """Return ``(flow origin, global first release)``; pre-window code has no global release to compare."""
        if kind == KIND_PRE_WINDOW:
            return KIND_PRE_WINDOW, None
        if kind == KIND_NON_FEATURE:
            release = column_id.split("@", 1)[1]
            return release, release
        release = str(cell.get("release"))
        return release, str((self.columns.get(column_id) or {}).get("release", release))

    def _referenced(self, rows: list[dict[str, Any]]) -> set[str]:
        """Return every release a fix, a real column or a flow origin names."""
        referenced = {str(row.get("fix_release") or UNRELEASED) for row in rows}
        referenced |= {str(column["release"]) for column in self.columns.values()}
        return referenced | {origin for origin, _, _ in self.flow if origin != KIND_PRE_WINDOW}

    def releases(self, rows: list[dict[str, Any]]) -> list[str]:
        """Return the chart axis: stable releases spanning the referenced range, plus referenced others, in order."""
        referenced = self._referenced(rows)
        known = sorted((v for v in referenced if v in self.book.orders), key=self.book.sort_key)
        unknown = sorted((v for v in referenced if v not in self.book.orders), key=lambda v: (v == UNRELEASED, v))
        if not known:
            return unknown
        low, high = self.book.order(known[0]), self.book.order(known[-1])
        span = [
            version
            for version in sorted(self.book.orders, key=self.book.sort_key)
            if low <= self.book.order(version) <= high
            and (version not in self.book.prereleases or version in referenced)
        ]
        return span + unknown

    def flow_records(self) -> list[dict[str, Any]]:
        """Return the flow bands sorted by target release, then origin release."""

        def key(item: tuple[tuple[str, str, str], float]) -> tuple[float, float, str, str]:
            """Order bands by target, then origin (pre-window first)."""
            (origin, target, bucket), _ = item
            origin_order = -1.0 if origin == KIND_PRE_WINDOW else self.book.order(origin)
            return (self.book.order(target), origin_order, origin, bucket)

        return [
            {"from": origin, "to": target, "weight": weight, "bucket": bucket}
            for (origin, target, bucket), weight in sorted(self.flow.items(), key=key)
        ]

    def top_features(self) -> list[dict[str, Any]]:
        """Return the most-fixed real columns by binary fix count."""
        ranked = sorted(
            (cid for cid, count in self.fix_counts.items() if count > 0),
            key=lambda cid: (-self.fix_counts[cid], self.book.order((self.columns.get(cid) or {}).get("release")), cid),
        )
        return [
            {
                "column_id": cid,
                "title": (self.columns.get(cid) or {}).get("title", ""),
                "release": (self.columns.get(cid) or {}).get("release"),
                "kind": (self.columns.get(cid) or {}).get("kind"),
                "fix_count": self.fix_counts[cid],
            }
            for cid in ranked[:TOP_FEATURES_LIMIT]
        ]

    def burnup(self, releases: list[str], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return cumulative feature-column and fix counts per release."""
        features = sorted(
            self.book.order(col.get("release")) for col in self.columns.values() if col.get("kind") == KIND_FEATURE
        )
        fixes = sorted(self.book.order(str(row.get("fix_release") or UNRELEASED)) for row in rows)
        return [
            {
                "release": release,
                "features": bisect.bisect_right(features, self.book.order(release)),
                "fixes": bisect.bisect_right(fixes, self.book.order(release)),
            }
            for release in releases
        ]


def aggregate(rows: list[dict[str, Any]], columns: list[dict[str, Any]], index: dict[str, Any]) -> dict[str, Any]:
    """Build the release-level aggregate the report renders (soft view for flow/heatmap, binary for rankings).

    Flow runs from a column's introduction on the fix's own line (or its global release when the column is absent
    from that line) to the fix release, bucketed ``previous`` (1 release back), ``2-5`` or ``legacy`` (pre-window code
    and anything more than 5 releases back on the fix's line). Associations whose column's global first release
    equals the fix release (including ``non-feature@<fix release>``) go to ``same_cycle`` and never into ``flow`` or
    ``heatmap``. Residue soft mass is reported per fix release.

    Args:
        rows: Scored row records.
        columns: Column records (real columns are used for titles, releases and burn-up; pseudo records are ignored).
        index: Release index.

    Returns:
        ``{releases, flow, heatmap, burnup, top_features, residue, same_cycle, totals}``; ``totals`` carries
        ``fixes``, ``soft_sum``, ``binary_links``, ``low_confidence_links`` and ``residue_share``.

    Examples:
        >>> releases = [{"version": v, "order": i, "line": "1"} for i, v in enumerate(["1.0", "1.1"])]
        >>> index = {"releases": releases, "lines": {"1": ["1.0", "1.1"]}}
        >>> cols = [{"column_id": "1.0/a", "kind": "feature", "release": "1.0", "title": "A"}]
        >>> cell = {"column_id": "1.0/a", "release": "1.0", "soft_share": 1.0, "binary": True, "confidence": 1.0}
        >>> agg = aggregate([{"fix_id": "1.1/f", "fix_release": "1.1", "line": "1", "cells": [cell]}], cols, index)
        >>> agg["flow"], agg["totals"]["binary_links"]
        ([{'from': '1.0', 'to': '1.1', 'weight': 1.0, 'bucket': 'previous'}], 1)
    """
    book = _ReleaseBook.from_index(index)
    acc = _Aggregator(book, _real_columns(columns))
    for row in rows:
        acc.add_row(row)
    releases = acc.releases(rows)
    flow = acc.flow_records()
    heatmap: dict[str, dict[str, float]] = {}
    for band in flow:
        cells = heatmap.setdefault(band["to"], {})
        cells[band["from"]] = cells.get(band["from"], 0.0) + band["weight"]
    fix_count = len(rows)
    return {
        "releases": releases,
        "flow": flow,
        "heatmap": heatmap,
        "burnup": acc.burnup(releases, rows),
        "top_features": acc.top_features(),
        "residue": {
            release: dict(sorted(acc.residue[release].items())) for release in sorted(acc.residue, key=book.sort_key)
        },
        "same_cycle": sorted(
            acc.same_cycle, key=lambda item: (book.sort_key(item["release"]), item["fix_id"], item["column_id"])
        ),
        "totals": {
            "fixes": fix_count,
            "soft_sum": acc.soft_sum,
            "binary_links": acc.binary_links,
            "low_confidence_links": acc.low_confidence,
            "residue_share": acc.residue_sum / fix_count if fix_count else 0.0,
        },
    }


# --------------------------------------------------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------------------------------------------------


def _read_json(path: Path) -> Any:
    """Read one JSON document."""
    return json.loads(read_bounded_text(path))


def _read_keyed(path: Path, key: str) -> dict[str, dict[str, Any]]:
    """Read records keyed by ``key``: JSONL (last record per key wins), a JSON list, or a JSON object keyed already.

    Raises:
        ValueError: the file holds neither records nor a keyed object.
    """
    if path.suffix == ".jsonl":
        records, _ = read_jsonl(path)
        return last_by_key(records, key)
    data = _read_json(path)
    if isinstance(data, list):
        return last_by_key([item for item in data if isinstance(item, dict)], key)
    if isinstance(data, dict):
        return {str(name): value for name, value in data.items() if isinstance(value, dict)}
    raise ValueError(f"{path}: expected JSON records keyed by {key!r}")


def read_overrides_file(path: Path) -> list[dict[str, Any]]:
    """Read overrides from a JSON list or from ``overrides.csv`` lines ``fix_id,column_id,assert|deny,note``.

    A header row starting with ``fix_id``, blank lines and lines starting with ``#`` are skipped.

    Raises:
        ValueError: a line has fewer than three fields or an unknown action.
    """
    if path.suffix == ".json":
        data = _read_json(path)
        if not isinstance(data, list):
            raise ValueError(f"{path}: expected a JSON list of overrides")
        overrides = [dict(item) for item in data]
    else:
        overrides = []
        with io.StringIO(read_bounded_bytes(path).decode("utf-8"), newline="") as handle:
            for number, fields in enumerate(csv.reader(handle), start=1):
                cells = [part.strip() for part in fields]
                if not cells or not cells[0] or cells[0].startswith("#") or cells[0] == "fix_id":
                    continue
                if len(cells) < 3:
                    raise ValueError(f"{path}:{number}: expected fix_id,column_id,action[,note]")
                overrides.append(
                    {"fix_id": cells[0], "column_id": cells[1], "action": cells[2], "note": ",".join(cells[3:])}
                )
    _overrides_by_fix(overrides)
    return overrides


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    """Write records as canonical JSON lines through a temp file and an atomic rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("".join(f"{canonical_json(record)}\n" for record in records))
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _run_score(args: argparse.Namespace) -> str:
    """Run the ``score`` subcommand and return its summary line."""
    entries = _read_json(args.entries)
    index = _read_json(args.index)
    if not isinstance(entries, (dict, list)) or not isinstance(index, dict):
        raise ValueError("--entries must hold a build_entries object or entry list, --index a release index object")
    evidence = _read_keyed(args.evidence, "fix_id")
    adjudications = _read_keyed(args.adjudications, "fix_id") if args.adjudications else {}
    overrides = read_overrides_file(args.overrides) if args.overrides else []
    columns = build_columns(entries, index)
    fixes = _fix_list(entries)
    sha_map = build_sha_map(columns, index, fixes)
    rows = score_rows(fixes, evidence, columns, sha_map, index, adjudications, overrides)
    summary = aggregate(rows, columns, index)
    _write_jsonl(args.out_rows, rows)
    _write_jsonl(args.out_columns, columns + pseudo_columns(rows, index))
    atomic_write_json(args.out_aggregate, summary)
    totals = summary["totals"]
    return (
        f"scored {totals['fixes']} fixes against {len(columns)} columns: {totals['binary_links']} binary links, "
        f"residue {totals['residue_share']:.0%} (provisional weights) -> {args.out_rows}"
    )


def _build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        prog="evolution_score.py",
        description="Score the evolution feature x fix matrix and build its release aggregates.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    score = sub.add_parser("score", help="score fix rows from entries, evidence and the release index")
    score.add_argument("--entries", type=Path, required=True, help="build_entries JSON (fixes + columns)")
    score.add_argument("--evidence", type=Path, required=True, help="evidence JSONL (last record per fix wins)")
    score.add_argument("--index", type=Path, required=True, help="release index JSON")
    score.add_argument("--adjudications", type=Path, help="adjudications JSONL (already reuse-checked)")
    score.add_argument("--overrides", type=Path, help="overrides CSV (fix_id,column_id,assert|deny,note) or JSON list")
    score.add_argument("--out-rows", type=Path, required=True, help="row records JSONL to write")
    score.add_argument("--out-columns", type=Path, required=True, help="column records JSONL to write")
    score.add_argument("--out-aggregate", type=Path, required=True, help="aggregate JSON to write")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the command line; return 0 on success and 2 on unreadable or malformed input."""
    args = _build_parser().parse_args(argv)
    try:
        print(_run_score(args))
    except (OSError, ValueError) as exc:
        print(f"evolution_score.py: error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Evaluate evolution association scores against human labels, following the Phase 0 real-repo protocol.

Purpose:
    Compare scored fix rows with a labelled sample and report the declared estimands: micro-averaged precision and
    recall over (fix, real column) binary associations, the residue metric (Σ soft over the five residue
    pseudo-columns divided by the labelled fix count), and 95% intervals from a cluster bootstrap over fixes
    stratified by release. Also checks the Phase 0 bars and tunes the binary threshold on the tuning repo.

Labels:
    CSV with header ``repo,fix_id,column_id``; one line per (fix, repaired column). ``column_id`` ``none`` marks a fix
    that repairs nothing tracked. Labels naming a pseudo-column count as ``none``; alias IDs resolve to the canonical
    column when column records are given. A labelled fix with no scored row predicts nothing and counts as fully
    residue.

Usage:
    python evolution_eval.py evaluate --labels LABELS.csv --rows ROWS.jsonl [--columns COLUMNS.jsonl] [--repo R] \
        [--seed N] [--resamples N]
    python evolution_eval.py pooled --labels LABELS.csv --rows REPO=ROWS.jsonl --rows REPO=ROWS.jsonl [--seed N]
    python evolution_eval.py tune --labels LABELS.csv --rows ROWS.jsonl --candidates 0.2,0.3,0.4 [--repo R]

Outputs:
    One line of JSON on stdout: the evaluation result with its ``gate`` flags (``evaluate``, ``pooled``), or the chosen
    threshold with the per-candidate table (``tune``).

Exit codes:
    0 success; 2 invalid arguments, unreadable input, or no feasible threshold.

Protocol bars (declared before any run): precision >= 0.70, recall >= 0.50, residue <= 0.35. The third-repo rule
fires when repo B's precision interval has a lower bound below 0.55; the pooled B+C evaluation then passes only when
all bars hold and its own lower bound is >= 0.55.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import random
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from _evolution_common import LINK_METHODS, RESIDUE_COLUMNS, column_kind, last_by_key, read_bounded_text, read_jsonl

#: Phase 0 bars, fixed before any real-repo run.
PRECISION_BAR: Final = 0.70
RECALL_BAR: Final = 0.50
RESIDUE_BAR: Final = 0.35
CI_LOWER_BAR: Final = 0.55

#: A repo is gated only with at least this many labelled (fix, column) pairs; below it the bars are not evidence.
GOLD_FLOOR: Final = 15

#: Links kept per fix, best raw score first. ``None`` keeps every link that passes the threshold. Measured on the
#: labelled repos: any cap of 1-3 cost the best-scoring repo a true link (recall 0.50 to 0.46) for no precision gain
#: that reaches the bar, so the default stays uncapped and ``--top-k`` is an experiment switch.
DEFAULT_TOP_K: Final[int | None] = None

#: Bootstrap defaults from the evaluation protocol.
DEFAULT_RESAMPLES: Final = 2000
MAX_RESAMPLES: Final = 10000
DEFAULT_SEED: Final = 0

#: Label value for a fix that repairs nothing tracked.
NONE_LABEL: Final = "none"

_LABEL_FIELDS: Final = ("repo", "fix_id", "column_id")
_CI_LEVEL: Final = 0.95
_MISSING_RELEASE: Final = "missing"

Labels = list[dict[str, str]]


@dataclass(frozen=True, slots=True)
class _FixStat:
    """Per-fix counts for micro-averaged metrics; one bootstrap cluster."""

    cluster: tuple[str, str]
    stratum: tuple[str, str]
    true_positive: int
    predicted: int
    gold: int
    residue: float


# --------------------------------------------------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------------------------------------------------


def parse_labels(text: str) -> Labels:
    """Parse labels CSV text with header ``repo,fix_id,column_id`` into label records.

    Raises:
        ValueError: the header lacks a required column, or a line has an empty ``fix_id`` or ``column_id``.

    Examples:
        >>> parse_labels("repo,fix_id,column_id\\nB,1.1/f,none\\n")
        [{'repo': 'B', 'fix_id': '1.1/f', 'column_id': 'none'}]
    """
    reader = csv.reader(io.StringIO(text))
    header = [name.strip().lower() for name in next(reader, [])]
    missing = [name for name in _LABEL_FIELDS if name not in header]
    if missing:
        raise ValueError(f"labels CSV header lacks {missing}; expected repo,fix_id,column_id")
    positions = {name: header.index(name) for name in _LABEL_FIELDS}
    labels: Labels = []
    for number, fields in enumerate(reader, start=2):
        values = _label_values(fields, positions)
        if not any(values.values()):
            continue
        if not values["fix_id"] or not values["column_id"]:
            raise ValueError(f"labels line {number}: fix_id and column_id are required")
        labels.append(values)
    return labels


def _label_values(fields: list[str], positions: dict[str, int]) -> dict[str, str]:
    """Pick the label columns from one CSV line, stripped; a short line yields empty values."""
    return {name: fields[pos].strip() if pos < len(fields) else "" for name, pos in positions.items()}


def read_labels(path: Path) -> Labels:
    """Read a labels CSV file (see :func:`parse_labels`)."""
    return parse_labels(read_bounded_text(path))


def _labels_of(labels: Labels | Path | str) -> Labels:
    """Return label records from records or from a CSV path."""
    return read_labels(Path(labels)) if isinstance(labels, (str, Path)) else list(labels)


def _rows_by_fix(rows: list[dict[str, Any]] | dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index rows by fix ID; with a list, the last record per fix wins (store semantics)."""
    return dict(rows) if isinstance(rows, dict) else last_by_key(rows, "fix_id")


def _aliases(columns: list[dict[str, Any]] | None) -> dict[str, str]:
    """Map every alias ID to its canonical column ID."""
    return {alias: col["column_id"] for col in columns or [] for alias in col.get("aliases") or []}


def _select_repo(labels: Labels, repo: str | None) -> tuple[str, Labels]:
    """Return the repo name and its labels; require ``repo`` when labels span several repos.

    Raises:
        ValueError: no labels, no labels for ``repo``, or several repos without ``repo``.
    """
    repos = sorted({label["repo"] for label in labels})
    if repo is not None:
        selected = [label for label in labels if label["repo"] == repo]
        if not selected:
            raise ValueError(f"no labels for repo {repo!r}; labelled repos: {repos}")
        return repo, selected
    if not repos:
        raise ValueError("no labels to evaluate")
    if len(repos) > 1:
        raise ValueError(f"labels span repos {repos}; pass repo= to pick one or use pooled()")
    return repos[0], labels


# --------------------------------------------------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------------------------------------------------


def _gold_by_fix(labels: Labels, aliases: dict[str, str]) -> dict[str, set[str]]:
    """Return the labelled real columns per fix; ``none`` and pseudo-column labels add a fix with no columns."""
    gold: dict[str, set[str]] = {}
    for label in labels:
        columns = gold.setdefault(label["fix_id"], set())
        column_id = aliases.get(label["column_id"], label["column_id"])
        if column_id != NONE_LABEL and column_kind(column_id) == "real":
            columns.add(column_id)
    return gold


def _predicted(row: dict[str, Any], threshold: float | None, top_k: int | None = None) -> set[str]:
    """Return the real columns a row associates: its binary view, or ``raw >= threshold`` for link methods when given.

    A re-decided threshold applies the scorer's rule, so only cells whose strongest signal is in ``LINK_METHODS`` link.
    ``top_k`` then keeps only the k links with the highest raw score (ties by column ID), because one fix repairs few
    features and every further link is far more likely wrong than right.
    """
    linked = [
        cell
        for cell in row.get("cells", [])
        if column_kind(cell["column_id"]) == "real"
        and (
            cell.get("binary")
            if threshold is None
            else cell.get("method") in LINK_METHODS and float(cell.get("raw_score") or 0.0) >= threshold
        )
    ]
    if top_k is not None:
        linked.sort(key=lambda cell: (-float(cell.get("raw_score") or 0.0), cell["column_id"]))
        linked = linked[:top_k]
    return {cell["column_id"] for cell in linked}


def _residue(row: dict[str, Any]) -> float:
    """Return a row's soft mass on the residue pseudo-columns."""
    return sum(
        float(cell.get("soft_share") or 0.0) for cell in row.get("cells", []) if cell["column_id"] in RESIDUE_COLUMNS
    )


def _fix_stats(
    labels: Labels,
    rows_by_fix: dict[str, dict[str, Any]],
    repo: str,
    aliases: dict[str, str],
    cut: tuple[float | None, int | None],
) -> list[_FixStat]:
    """Return one stat per labelled fix, clustered by ``(repo, fix)`` and stratified by ``(repo, fix release)``.

    ``cut`` is ``(threshold, top_k)``: the re-decided raw threshold (None keeps the stored binary view) and the cap on
    links per fix (None keeps all).
    """
    threshold, top_k = cut
    stats = []
    for fix_id, gold in sorted(_gold_by_fix(labels, aliases).items()):
        row = rows_by_fix.get(fix_id)
        predicted = _predicted(row, threshold, top_k) if row else set()
        release = str(row.get("fix_release")) if row else _MISSING_RELEASE
        residue = _residue(row) if row else 1.0
        stats.append(
            _FixStat((repo, fix_id), (repo, release), len(predicted & gold), len(predicted), len(gold), residue)
        )
    return stats


def _metrics(stats: list[_FixStat]) -> dict[str, Any]:
    """Return micro precision, recall, F1 and the residue metric; undefined ratios are None."""
    true_positive = sum(stat.true_positive for stat in stats)
    predicted = sum(stat.predicted for stat in stats)
    gold = sum(stat.gold for stat in stats)
    precision = true_positive / predicted if predicted else None
    recall = true_positive / gold if gold else None
    f1 = None
    if precision is not None and recall is not None:
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "fixes": len(stats),
        "true_positive": true_positive,
        "predicted": predicted,
        "gold": gold,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "residue": sum(stat.residue for stat in stats) / len(stats) if stats else None,
    }


def _percentile(ordered: list[float], fraction: float) -> float:
    """Return the linearly interpolated percentile of sorted values."""
    position = fraction * (len(ordered) - 1)
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _interval(values: list[float]) -> list[float | None]:
    """Return the two-sided percentile interval at the protocol level, or ``[None, None]`` with no values."""
    if not values:
        return [None, None]
    ordered = sorted(values)
    tail = (1.0 - _CI_LEVEL) / 2
    return [_percentile(ordered, tail), _percentile(ordered, 1.0 - tail)]


def _bootstrap(stats: list[_FixStat], seed: int, resamples: int) -> dict[str, list[float | None]]:
    """Return percentile intervals from a cluster bootstrap over fixes, resampling within each stratum."""
    rng = random.Random(seed)  # noqa: S311 - seeded statistical resampling must be reproducible, not secret
    strata: dict[tuple[str, str], list[_FixStat]] = {}
    for stat in stats:
        strata.setdefault(stat.stratum, []).append(stat)
    groups = [strata[key] for key in sorted(strata)]
    samples: dict[str, list[float]] = {"precision": [], "recall": [], "residue": []}
    for _ in range(resamples):
        drawn = [stat for group in groups for stat in rng.choices(group, k=len(group))]
        metrics = _metrics(drawn)
        for name, values in samples.items():
            if metrics[name] is not None:
                values.append(metrics[name])
    return {name: _interval(values) for name, values in samples.items()}


def _result(
    stats: list[_FixStat], repos: list[str], seed: int, resamples: int, top_k: int | None = None
) -> dict[str, Any]:
    """Assemble one evaluation result: point estimates, intervals and bootstrap settings."""
    if not 1 <= resamples <= MAX_RESAMPLES:
        raise ValueError(f"resamples must be between 1 and {MAX_RESAMPLES}, got {resamples}")
    result = _metrics(stats)
    result.update(
        {
            "repos": repos,
            "top_k": top_k,
            "ci": _bootstrap(stats, seed, resamples),
            "ci_level": _CI_LEVEL,
            "seed": seed,
            "resamples": resamples,
            "missing_rows": sorted(stat.cluster[1] for stat in stats if stat.stratum[1] == _MISSING_RELEASE),
        }
    )
    return result


def evaluate(
    labels: Labels | Path | str,
    rows: list[dict[str, Any]] | dict[str, dict[str, Any]],
    columns: list[dict[str, Any]] | None = None,
    *,
    seed: int = DEFAULT_SEED,
    resamples: int = DEFAULT_RESAMPLES,
    repo: str | None = None,
    top_k: int | None = DEFAULT_TOP_K,
) -> dict[str, Any]:
    """Evaluate one repo's scored rows against its labels.

    Args:
        labels: Label records or a labels CSV path.
        rows: Row records (last record per fix wins) or rows keyed by fix ID.
        columns: Column records, used only to resolve alias IDs in labels.
        seed: Bootstrap seed; the same seed reproduces the same intervals.
        resamples: Bootstrap resamples.
        repo: Repo whose labels to use; required when labels span several repos.
        top_k: Links kept per fix, best raw score first; None keeps all (see :func:`_predicted`).

    Returns:
        ``{fixes, true_positive, predicted, gold, precision, recall, f1, residue, repos, top_k, ci, ci_level, seed,
        resamples, missing_rows}``; ``ci`` holds ``[low, high]`` for precision, recall and residue.

    Raises:
        ValueError: no usable labels, several repos without ``repo``, or ``resamples`` outside 1–10000.

    Examples:
        >>> labels = [{"repo": "B", "fix_id": "f", "column_id": "a"}]
        >>> rows = [{"fix_id": "f", "fix_release": "1.0", "cells": [{"column_id": "a", "binary": True}]}]
        >>> result = evaluate(labels, rows, resamples=10)
        >>> result["precision"], result["recall"], result["ci"]["precision"]
        (1.0, 1.0, [1.0, 1.0])
    """
    repo_name, selected = _select_repo(_labels_of(labels), repo)
    stats = _fix_stats(selected, _rows_by_fix(rows), repo_name, _aliases(columns), (None, top_k))
    return _result(stats, [repo_name], seed, resamples, top_k)


def pooled(
    labels: Labels | Path | str,
    rows_by_repo: dict[str, list[dict[str, Any]] | dict[str, dict[str, Any]]],
    columns_by_repo: dict[str, list[dict[str, Any]]] | None = None,
    *,
    seed: int = DEFAULT_SEED,
    resamples: int = DEFAULT_RESAMPLES,
    top_k: int | None = DEFAULT_TOP_K,
) -> dict[str, Any]:
    """Evaluate several repos together (third-repo rule), clustering the bootstrap by ``(repo, fix)``.

    Labels of repos absent from ``rows_by_repo`` (for example the tuning repo) are ignored and listed under
    ``ignored_repos``.

    Raises:
        ValueError: ``rows_by_repo`` is empty, a repo in it has no labels, or ``resamples`` outside 1–10000.

    Examples:
        >>> labels = [{"repo": r, "fix_id": "f", "column_id": "a"} for r in ("B", "C")]
        >>> row = {"fix_id": "f", "fix_release": "1.0", "cells": [{"column_id": "a", "binary": True}]}
        >>> pooled(labels, {"B": [row], "C": [row]}, resamples=5)["fixes"]
        2
    """
    label_list = _labels_of(labels)
    if not rows_by_repo:
        raise ValueError("pooled evaluation needs rows for at least one repo")
    stats: list[_FixStat] = []
    for repo in sorted(rows_by_repo):
        _, selected = _select_repo(label_list, repo)
        aliases = _aliases((columns_by_repo or {}).get(repo))
        stats += _fix_stats(selected, _rows_by_fix(rows_by_repo[repo]), repo, aliases, (None, top_k))
    result = _result(stats, sorted(rows_by_repo), seed, resamples, top_k)
    result["ignored_repos"] = sorted({label["repo"] for label in label_list} - set(rows_by_repo))
    return result


def gate(result: dict[str, Any], *, pooled_run: bool = False) -> dict[str, Any]:
    """Check an evaluation result against the Phase 0 bars.

    ``bars_met`` covers precision, recall and residue. ``ci_lower`` is the precision interval's lower bound against
    0.55. ``pass`` needs both. For a single repo, a failing ``ci_lower`` sets ``third_repo_required`` (label repo C,
    then run :func:`pooled`); for a pooled run there is no further repo, so ``pass`` is final.

    A result with fewer than :data:`GOLD_FLOOR` labelled pairs is not gated: ``gated`` is False and ``pass`` is None,
    because a handful of pairs moves precision by a whole fix and says nothing about the scorer.

    Examples:
        >>> flags = gate({"precision": 0.8, "recall": 0.6, "residue": 0.2, "ci": {"precision": [0.5, 0.9]}})
        >>> flags["bars_met"], flags["ci_lower"], flags["third_repo_required"], flags["pass"]
        (True, False, True, False)
        >>> gate({"gold": 9, "precision": 1.0, "recall": 1.0, "residue": 0.0})["pass"] is None
        True
    """
    precision, recall, residue = result.get("precision"), result.get("recall"), result.get("residue")
    lower = (result.get("ci") or {}).get("precision", [None, None])[0]
    flags = {
        "precision": precision is not None and precision >= PRECISION_BAR,
        "recall": recall is not None and recall >= RECALL_BAR,
        "residue": residue is not None and residue <= RESIDUE_BAR,
        "ci_lower": lower is not None and lower >= CI_LOWER_BAR,
    }
    bars_met = flags["precision"] and flags["recall"] and flags["residue"]
    gold = result.get("gold")
    gated = gold is None or gold >= GOLD_FLOOR
    return {
        **flags,
        "bars_met": bars_met,
        "gated": gated,
        "pass": bars_met and flags["ci_lower"] if gated else None,
        "third_repo_required": not pooled_run and not flags["ci_lower"],
        "pooled": pooled_run,
        "bars": {
            "precision": PRECISION_BAR,
            "recall": RECALL_BAR,
            "residue": RESIDUE_BAR,
            "ci_lower": CI_LOWER_BAR,
            "gold_floor": GOLD_FLOOR,
        },
    }


def threshold_table(
    labels: Labels | Path | str,
    rows_raw: list[dict[str, Any]] | dict[str, dict[str, Any]],
    candidates: list[float],
    *,
    repo: str | None = None,
    columns: list[dict[str, Any]] | None = None,
    top_k: int | None = DEFAULT_TOP_K,
) -> list[dict[str, Any]]:
    """Return precision, recall and F1 per candidate threshold, re-deciding binary links from raw scores.

    Raises:
        ValueError: no candidates, a candidate outside (0, 1], or unusable labels.
    """
    values = sorted({float(candidate) for candidate in candidates})
    if not values or not all(0.0 < value <= 1.0 for value in values):
        raise ValueError(f"candidate thresholds must be a non-empty set in (0, 1], got {candidates}")
    repo_name, selected = _select_repo(_labels_of(labels), repo)
    rows_by_fix, aliases = _rows_by_fix(rows_raw), _aliases(columns)
    table = []
    for value in values:
        metrics = _metrics(_fix_stats(selected, rows_by_fix, repo_name, aliases, (value, top_k)))
        table.append({"threshold": value, **{key: metrics[key] for key in ("precision", "recall", "f1")}})
    return table


def tune_threshold(
    labels: Labels | Path | str,
    rows_raw: list[dict[str, Any]] | dict[str, dict[str, Any]],
    candidates: list[float],
    *,
    repo: str | None = None,
    columns: list[dict[str, Any]] | None = None,
    top_k: int | None = DEFAULT_TOP_K,
) -> float:
    """Choose the threshold maximising binary F1 on the tuning repo, subject to precision >= 0.70.

    Ties prefer higher precision, then the lower threshold.

    Raises:
        ValueError: no candidate reaches the precision bar (or invalid candidates/labels); the message lists the
            best precision seen so the caller can widen the candidate grid or revisit the weights.

    Examples:
        >>> labels = [{"repo": "A", "fix_id": "f", "column_id": "a"}]
        >>> cells = [{"column_id": "a", "raw_score": 0.6, "method": "blame"},
        ...          {"column_id": "b", "raw_score": 0.3, "method": "blame"}]
        >>> tune_threshold(labels, [{"fix_id": "f", "fix_release": "1.0", "cells": cells}], [0.2, 0.5])
        0.5
    """
    table = threshold_table(labels, rows_raw, candidates, repo=repo, columns=columns, top_k=top_k)
    feasible = [entry for entry in table if entry["precision"] is not None and entry["precision"] >= PRECISION_BAR]
    if not feasible:
        best = max((entry["precision"] or 0.0 for entry in table), default=0.0)
        raise ValueError(f"no candidate threshold reaches precision {PRECISION_BAR} (best {best:.2f}); widen the grid")
    chosen = max(feasible, key=lambda entry: (entry["f1"] or 0.0, entry["precision"], -entry["threshold"]))
    return float(chosen["threshold"])


# --------------------------------------------------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------------------------------------------------


def _read_records(path: Path) -> list[dict[str, Any]]:
    """Read records from JSONL (``.jsonl``) or a JSON list.

    Raises:
        ValueError: a non-JSONL file does not hold a JSON list.
    """
    if path.suffix == ".jsonl":
        records, _ = read_jsonl(path)
        return records
    data = json.loads(read_bounded_text(path))
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a JSON list of records")
    return [item for item in data if isinstance(item, dict)]


def _repo_rows(specs: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Parse repeated ``REPO=FILE`` arguments into rows per repo.

    Raises:
        ValueError: a spec lacks ``=`` or repeats a repo.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for spec in specs:
        repo, sep, path = spec.partition("=")
        if not sep or not repo or not path:
            raise ValueError(f"--rows expects REPO=FILE, got {spec!r}")
        if repo in out:
            raise ValueError(f"--rows names repo {repo!r} twice")
        out[repo] = _read_records(Path(path))
    return out


def _run(args: argparse.Namespace) -> dict[str, Any]:
    """Run the selected subcommand and return its JSON payload."""
    if not 1 <= args.resamples <= MAX_RESAMPLES:
        raise ValueError(f"resamples must be between 1 and {MAX_RESAMPLES}, got {args.resamples}")
    if args.top_k is not None and args.top_k < 1:
        raise ValueError(f"top-k must be at least 1, got {args.top_k}")
    top_k = args.top_k if args.top_k is not None else DEFAULT_TOP_K
    labels = read_labels(args.labels)
    if args.command == "pooled":
        result = pooled(labels, _repo_rows(args.rows), seed=args.seed, resamples=args.resamples, top_k=top_k)
        return {**result, "gate": gate(result, pooled_run=True)}
    rows = _read_records(args.rows)
    columns = _read_records(args.columns) if args.columns else None
    if args.command == "tune":
        candidates = [float(part) for part in args.candidates.split(",") if part.strip()]
        table = threshold_table(labels, rows, candidates, repo=args.repo, columns=columns, top_k=top_k)
        chosen = tune_threshold(labels, rows, candidates, repo=args.repo, columns=columns, top_k=top_k)
        return {"threshold": chosen, "table": table}
    result = evaluate(labels, rows, columns, seed=args.seed, resamples=args.resamples, repo=args.repo, top_k=top_k)
    return {**result, "gate": gate(result)}


def _build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        prog="evolution_eval.py", description="Evaluate evolution association scores against human labels."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    single = sub.add_parser("evaluate", help="precision, recall, residue and bootstrap intervals for one repo")
    tune = sub.add_parser("tune", help="pick the F1-maximising threshold with precision >= 0.70 (tuning repo)")
    for command in (single, tune):
        command.add_argument("--rows", type=Path, required=True, help="rows JSONL (last record per fix wins)")
        command.add_argument("--columns", type=Path, help="column records JSONL, for label alias resolution")
        command.add_argument("--repo", help="repo whose labels to use when the CSV holds several")
    tune.add_argument("--candidates", required=True, help="comma-separated thresholds in (0, 1]")
    group = sub.add_parser("pooled", help="pooled evaluation for the third-repo rule, clustered by (repo, fix)")
    group.add_argument("--rows", action="append", required=True, help="REPO=ROWS.jsonl, repeated per repo")
    for command in (single, tune, group):
        command.add_argument("--labels", type=Path, required=True, help="labels CSV: repo,fix_id,column_id")
        command.add_argument("--seed", type=int, default=DEFAULT_SEED, help="bootstrap seed")
        command.add_argument("--resamples", type=int, default=DEFAULT_RESAMPLES, help="bootstrap resamples (1-10000)")
        command.add_argument("--top-k", type=int, help="links kept per fix, best raw score first (default: all)")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the command line; return 0 on success and 2 on invalid input."""
    args = _build_parser().parse_args(argv)
    try:
        payload = _run(args)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, csv.Error) as exc:
        print(f"evolution_eval.py: error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())

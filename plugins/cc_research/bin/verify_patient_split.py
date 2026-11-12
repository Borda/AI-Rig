#!/usr/bin/env python
"""verify_patient_split.py — detect patient_id overlap between train/test CSV splits.

Reproduces the inline ``python -c`` block from
``plugins/cc_research/references/data-steward/ml-pipeline-patterns.md`` (Patient-Level Split
section). Loads two CSV files via pandas, computes the set intersection on the
``patient_id`` column, and prints a human-readable verdict identical to the original
block:

    "No patient overlap" when the intersection is empty.
    "Overlap: <N> patients" otherwise (where ``<N>`` is the intersection cardinality).

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/verify_patient_split.py" \
        --train splits/train.csv --test splits/test.csv

Exit codes:
    0   no patient overlap between two splits with usable identifiers.
    1   patient overlap detected.
    2   input error (unreadable CSV, missing column, no usable identifiers, pandas import failure).

Note:
    Pandas is the canonical reader used by the original inline block; this script
    requires it. Install via ``pip install pandas``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _validate_csv_path(raw: str) -> Path | None:
    """Resolve and validate that ``raw`` stays within a safe base directory.

    The current working directory is the project root. Resolved inputs must remain within it, including inputs in
    ``.experiments``; symlinks pointing outside the project are rejected.

    Args:
        raw: Raw value from ``--train`` or ``--test``.

    Returns:
        Resolved ``Path`` when the file exists and is within an allowed root;
        ``None`` when the file is missing or outside every allowed root.
    """
    if not raw:
        return None
    candidate = Path(raw).expanduser().resolve()
    if not candidate.is_file():
        return None
    project_root = Path.cwd().resolve()
    return candidate if candidate.is_relative_to(project_root) else None


def format_verdict(overlap_count: int) -> str:
    """Format the human-readable verdict line.

    Mirrors the inline block's ternary: empty overlap prints "No patient overlap",
    otherwise prints ``"Overlap: <N> patients"``. ``<N>`` is printed verbatim — no
    pluralization adjustment for ``N == 1`` (matches original block exactly).

    Args:
        overlap_count: Number of patient_ids present in both splits (must be ``>= 0``).

    Returns:
        The verdict line to print to stdout.

    Raises:
        ValueError: if ``overlap_count`` is negative.

    Examples:
        >>> format_verdict(0)
        'No patient overlap'
        >>> format_verdict(3)
        'Overlap: 3 patients'
        >>> format_verdict(1)
        'Overlap: 1 patients'
    """
    if overlap_count < 0:
        raise ValueError(f"overlap_count must be non-negative, got: {overlap_count}")
    if overlap_count == 0:
        return "No patient overlap"
    return f"Overlap: {overlap_count} patients"


def compute_overlap(train_csv: Path, test_csv: Path, column: str = "patient_id") -> int:
    """Read two CSVs and return the size of the ``column`` value intersection.

    Args:
        train_csv: Path to the train-split CSV — must contain ``column``.
        test_csv: Path to the test-split CSV — must contain ``column``.
        column: Column name to compute the intersection on (default ``"patient_id"``).

    Returns:
        Number of distinct identifiers shared after stripping whitespace and case folding. Null and blank identifiers
        are dropped, with counts reported on stderr.

    Raises:
        FileNotFoundError: if either CSV path does not exist.
        KeyError: if ``column`` is absent from either CSV.
        ImportError: if pandas is not installed.
        ValueError: if a CSV is malformed or either split has no usable identifiers.
    """
    try:
        import pandas as pd
    except ImportError as exc:
        raise ImportError("pandas required — install via 'pip install pandas'") from exc

    for label, path in (("train", train_csv), ("test", test_csv)):
        if not path.exists():
            raise FileNotFoundError(f"{label} CSV not found: {path}")

    for label, path in (("train", train_csv), ("test", test_csv)):
        header = list(pd.read_csv(path, nrows=0).columns)
        if column not in header:
            raise KeyError(f"{label} CSV missing required column '{column}': {sorted(header)}")

    # Load only the identifier column: a split CSV is often a full feature table, and reading every column of it just
    # to intersect one would hold the whole dataset in memory.
    splits: list[set[str]] = []
    for label, path in (("train", train_csv), ("test", test_csv)):
        raw = pd.read_csv(path, usecols=[column], dtype=str)[column]
        ids = raw.dropna().str.strip().str.casefold()
        ids = ids[ids.ne("")]
        dropped = len(raw) - len(ids)
        if dropped:
            print(f"{label}: dropped {dropped} null or blank patient identifiers", file=sys.stderr)
        if ids.empty:
            raise ValueError(f"{label} CSV has no usable patient identifiers")
        splits.append(set(ids))
    return len(splits[0] & splits[1])


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Build CLI arg parser.

    Separate for testability.
    """
    parser = argparse.ArgumentParser(
        description="Detect patient_id overlap between train/test split CSVs.",
    )
    parser.add_argument("--train", type=Path, required=True, help="Path to train split CSV.")
    parser.add_argument("--test", type=Path, required=True, help="Path to test split CSV.")
    parser.add_argument(
        "--column",
        default="patient_id",
        help="Column name to check for overlap (default: patient_id).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Check train/test CSVs for patient overlap and return the process status.

    Reads CSVs and prints the verdict to stdout.

    Args:
        argv: Optional argv list (for testing); defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code (0 for disjoint splits, 1 for overlap, 2 on input error).
    """
    args = _parse_args(argv)
    train_path = _validate_csv_path(str(args.train))
    test_path = _validate_csv_path(str(args.test))
    for label, raw, validated in (("train", args.train, train_path), ("test", args.test, test_path)):
        if validated is None:
            print(
                f"error: {label} CSV not found or outside allowed roots (project root): {raw}",
                file=sys.stderr,
            )
            return 2
    try:
        overlap = compute_overlap(train_path, test_path, column=args.column)
    except (OSError, ValueError, KeyError, ImportError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(format_verdict(overlap))
    return 1 if overlap else 0


if __name__ == "__main__":
    sys.exit(main())

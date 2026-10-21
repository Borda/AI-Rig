#!/usr/bin/env python
"""Print commit SHAs from a resolve ledger or merge plan.

Purpose: Supply the two SHA streams used by resolve's stranded-pick and overlap guards. Scope: Read a JSONL commits
ledger or a JSON-array merge plan without modifying either. Usage: ``python extract_commit_shas.py {ledger,plan} FILE``.
Outputs: Print one SHA per line in input order. Ledger mode skips blank rows, strips SHA whitespace and refuses empty
SHAs; plan mode prints the stored SHA values unchanged. Failure: Read, JSON and missing-field failures propagate with
exit 1, as in the inline implementations. Ledger output may be partial on failure, so callers must check the exit code.
Invalid CLI arguments exit 2; successful extraction exits 0. Used by: ``skills/resolve/modes/dispatch-phase3.md`` before
patch-id and overlap checks.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    """Emit the source's SHA stream while preserving its format-specific validation.

    Args:
        argv: Command-line arguments, or None to read the process arguments.

    Returns:
        Zero after reading and printing every SHA.

    Raises:
        SystemExit: A ledger row contains an empty SHA.
        OSError: The source cannot be read.
        ValueError: The source contains invalid JSON.
        KeyError: A source row lacks its SHA field.
    """
    parser = argparse.ArgumentParser(description="Print SHAs from a resolve commits ledger or merge plan.")
    parser.add_argument("mode", choices=("ledger", "plan"))
    parser.add_argument("source", type=Path)
    args = parser.parse_args(argv)
    with args.source.open(encoding="utf-8") as source:
        if args.mode == "ledger":
            for line in source:
                line = line.strip()
                if line:
                    sha = json.loads(line)["sha"]
                    if not sha.strip():
                        raise SystemExit(f"empty sha in ledger row: {line!r}")
                    print(sha.strip())
        else:
            for entry in json.load(source):
                print(entry["sha"])
    return 0


if __name__ == "__main__":
    sys.exit(main())

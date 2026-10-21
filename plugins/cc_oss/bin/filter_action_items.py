#!/usr/bin/env python
"""Print the ``action-items.jsonl`` records whose ``id`` is listed in an id file, one compact JSON object per line.

``oss:resolve`` reads per-item details on demand: Step 8 takes the ids from the persisted ``selected-items.txt``, Step
3b's pr-intelligence flow from a Write-tool ``item-ids-now.txt``. Both are a space-separated list of numeric item ids.
This script validates that list and prints the matching records in item-file order, so the block that calls it has
fixed text (no ids typed into the command line, which keeps it matchable by the blueprint manifest) and needs no
``jq``.

The id file is read whole and split on spaces, tabs and line breaks. A file with no digit at all is "missing or
empty"; any other character than digits and those separators is a "non-numeric item id". Records print with
``ensure_ascii=False`` and no spaces after separators, the way ``jq -c`` prints them, and always end in ``\\n`` (never
``\\r\\n``, which a text-mode stdout would produce on native Windows).

consumers: plugins/cc_oss/skills/resolve/modes/dispatch-prelude.md (Step 8 per-item details),
plugins/cc_oss/skills/resolve/modes/pr-intelligence.md (Step 3b on-demand item records)

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/filter_action_items.py" --items "$IMPL_DIR/action-items.jsonl" \\
        --ids-file "$IMPL_DIR/selected-items.txt" [--hint "run the Step 8 prelude first"]

Exit codes:
    0 — matching records printed (none when no listed id is in the item file)
    1 — id file missing, empty or holding a non-numeric id (``! BLOCKED`` line on stdout), or the item file unreadable
        or holding a line that is not a JSON object
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_SEPARATORS = re.compile(r"[ \r\n\t]+")
_NOT_ID_CHARS = re.compile(r"[^0-9 \r\n\t]")


def read_ids(path: Path) -> list[str] | str:
    """Read a space-separated list of numeric item ids.

    Args:
        path: File holding the ids.

    Returns:
        The ids as strings, or ``"missing"`` / ``"non-numeric"`` naming why the file cannot be used.

    Examples:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as tmp:
        ...     ids = Path(tmp) / "ids.txt"
        ...     _ = ids.write_text("3 1\\r\\n", encoding="utf-8")
        ...     read_ids(ids)
        ['3', '1']
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "missing"
    if not re.search(r"[0-9]", text):
        return "missing"
    if _NOT_ID_CHARS.search(text):
        return "non-numeric"
    return [token for token in _SEPARATORS.split(text) if token]


def _id_text(record: dict) -> str:
    """Return a record's ``id`` the way ``jq`` ``tostring`` does: strings as is, anything else as JSON."""
    value = record.get("id")
    return value if isinstance(value, str) else json.dumps(value)


def filter_records(lines: list[str], ids: list[str]) -> list[str]:
    """Return the compact JSON text of every record whose id is listed, in input order.

    Args:
        lines: Lines of an ``action-items.jsonl`` file; blank lines are skipped.
        ids: Wanted item ids as strings.

    Returns:
        One compact JSON line per matching record.

    Raises:
        ValueError: A non-blank line is not a JSON object.

    Examples:
        >>> filter_records(['{"id": 1, "x": "é"}', "", '{"id": 2}'], ["1"])
        ['{"id":1,"x":"é"}']
    """
    wanted = set(ids)
    matched: list[str] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"line {number} is not valid JSON ({exc.msg})") from exc
        if not isinstance(record, dict):
            raise ValueError(f"line {number} is not a JSON object")
        if _id_text(record) in wanted:
            matched.append(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
    return matched


def main(argv: list[str] | None = None) -> int:
    """Validate the id file and print the matching item records."""
    parser = argparse.ArgumentParser(description="Print the action-items.jsonl records named in an id file.")
    parser.add_argument("--items", required=True, help="$IMPL_DIR/action-items.jsonl")
    parser.add_argument("--ids-file", required=True, help="file of space-separated numeric item ids")
    parser.add_argument("--hint", default="", help="how to create the id file, appended to the missing-or-empty line")
    args = parser.parse_args(argv)
    ids_path = Path(args.ids_file)
    ids = read_ids(ids_path)
    if ids == "missing":
        tail = f"; {args.hint}" if args.hint else ""
        print(f"! BLOCKED — {ids_path.name} missing or empty{tail}")
        return 1
    if ids == "non-numeric":
        print(f"! BLOCKED — {ids_path.name} holds a non-numeric item id")
        return 1
    try:
        lines = Path(args.items).read_text(encoding="utf-8").splitlines()
        matched = filter_records(lines, ids)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        print(f"! BLOCKED — cannot read {args.items}: {exc}")
        return 1
    sys.stdout.buffer.write("".join(f"{line}\n" for line in matched).encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

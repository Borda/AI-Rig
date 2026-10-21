#!/usr/bin/env python
"""Give every oss:review finding a stable ID that survives line drift between review runs.

The review consolidator writes one JSON record per finding to ``findings.jsonl`` beside ``review-report.md``, then runs
this script over that file before it renders the report, so the report overview, the detailed sections, ``oss:resolve``
and a later re-review all name a finding the same way.

ID rule: ``<section-slug>-<sha1(normalized file path + "\\n" + normalized title)[:8]>``. The line number is deliberately
left out: an unrelated push that shifts the finding a few lines must not change its identity. Two findings that still
collide in one file get ``-2``, ``-3``, ... ranked by a hash of their full text, so neither reordering the report
nor shifting lines swaps them. The ID still depends on the title the consolidator wrote, so a later run that words
a finding differently mints a different ID; consumers matching across runs compare file and claim, not the ID alone.

Required record fields: ``section``, ``severity`` (one of ``critical``, ``high``, ``medium``, ``low``, ``cosmetic``)
and ``title``. ``file`` may be empty for findings with no single location. Any existing ``id`` is recomputed, so
re-running the script is idempotent.

``--carry-from <prior findings.jsonl>`` serves an ``oss:review --fill-gaps`` run, which must publish every carried
prior row unchanged. A row equal to a prior row in every field except ``id`` keeps that prior row's id; every other
row is minted as usual but skips any id a kept row holds. Without it, a new row colliding with a carried one could
outrank it and re-suffix the carried row's id. Without the flag the output is byte-identical to before.

consumers: plugins/cc_oss/skills/review/templates/consolidator-prompt.md (mints ids before rendering);
plugins/cc_oss/bin/merge_action_items.py (imports the normalizers so resolve matches findings the same way)

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/mint_finding_ids.py" "$REPORT_DIR/findings.jsonl"
    python "${CLAUDE_PLUGIN_ROOT}/bin/mint_finding_ids.py" "$REPORT_DIR/findings.jsonl" --carry-from "$PRIOR_FINDINGS"

Exit codes:
    0 — ids written
    1 — a findings file missing or unreadable, or a record that is not an object, misses a required field or carries an
        unknown severity
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Iterator
from itertools import count
from pathlib import Path, PurePosixPath
from typing import Final

#: Accepted finding severity words, ordered from most to least severe.
SEVERITIES: Final = ("critical", "high", "medium", "low", "cosmetic")
#: Fields a finding record must carry, non-empty, before an ID can be minted for it.
REQUIRED_FIELDS: Final = ("section", "severity", "title")
#: Matches a leading "low confidence" tag, stripped from titles before slugging.
_LOW_CONFIDENCE_PREFIX_RE: Final = re.compile(r"^\W*low confidence\W*", re.IGNORECASE)
#: Matches the ``[blocking]`` tag, stripped from titles before slugging.
_BLOCKING_RE: Final = re.compile(r"\[blocking\]", re.IGNORECASE)
#: Matches each run of characters outside ``[a-z0-9]``, which slugification replaces with a hyphen.
_NON_SLUG_RE: Final = re.compile(r"[^a-z0-9]+")
#: Matches whitespace runs, collapsed to a single space when normalizing titles and text.
_WHITESPACE_RE: Final = re.compile(r"\s+")


def section_slug(section: str) -> str:
    """Turn a report section header into the short slug that leads a finding ID.

    Markdown heading marks, the ``⚠ LOW CONFIDENCE —`` prefix the consolidator may add, the ``[blocking]`` tag and the
    trailing ``(if applicable)``/``(must fix ...)`` qualifiers are dropped, so the same section always yields the same
    slug.

    >>> section_slug("### [blocking] Critical (must fix before merge)")
    'critical'
    >>> section_slug("### ⚠ LOW CONFIDENCE — API Design (if applicable)")
    'api-design'
    >>> section_slug("Architecture & Quality")
    'architecture-quality'
    """
    text = section.strip().lstrip("#").strip()
    text = _LOW_CONFIDENCE_PREFIX_RE.sub("", text)
    text = _BLOCKING_RE.sub("", text)
    text = re.sub(r"\(.*?\)", "", text)
    return _NON_SLUG_RE.sub("-", text.lower()).strip("-") or "finding"


def normalize_path(path: str) -> str:
    """Normalize a finding's file path so Windows and POSIX spellings hash the same.

    >>> normalize_path(".\\\\src\\\\pkg\\\\mod.py")
    'src/pkg/mod.py'
    >>> normalize_path("./src/pkg/mod.py")
    'src/pkg/mod.py'
    >>> normalize_path("")
    ''
    """
    text = path.strip().replace("\\", "/")
    if not text:
        return ""
    posix = PurePosixPath(text).as_posix()
    while posix.startswith("./"):
        posix = posix[2:]
    return posix


def normalize_title(title: str) -> str:
    """Normalize a finding title: lowercase, single spaces, no surrounding punctuation.

    >>> normalize_title("  Missing  None check. ")
    'missing none check'
    """
    return _WHITESPACE_RE.sub(" ", title.strip().lower()).strip(" .,:;!-—")


def base_id(record: dict) -> str:
    """Return the collision-free-by-construction part of a finding ID (before any ``-N`` suffix).

    >>> base_id({"section": "Test Coverage Gaps", "file": "a.py", "title": "No test"}).startswith("test-coverage-gaps-")
    True
    """
    key = f"{normalize_path(str(record.get('file') or ''))}\n{normalize_title(str(record['title']))}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]  # noqa: S324 - identity hash, not security
    return f"{section_slug(str(record['section']))}-{digest}"


def validate(record: object, index: int) -> str | None:
    """Return a readable problem description for an invalid record, or ``None`` when it is usable."""
    if not isinstance(record, dict):
        return f"record {index}: not a JSON object"
    missing = [name for name in REQUIRED_FIELDS if not str(record.get(name) or "").strip()]
    if missing:
        return f"record {index}: missing {', '.join(missing)}"
    if str(record["severity"]).strip().lower() not in SEVERITIES:
        return f"record {index}: severity {record['severity']!r} not one of {', '.join(SEVERITIES)}"
    return None


def _collision_rank(record: dict) -> str:
    """Order findings that share a base ID by a hash of their full text, never by line or report position.

    Line numbers are left out for the same reason as in ``base_id``: a push that shifts one of two colliding findings
    must not swap their suffixes.
    """
    text = _WHITESPACE_RE.sub(" ", str(record.get("full_text") or record.get("title") or "").strip().lower())
    return hashlib.sha1(text.encode("utf-8")).hexdigest()  # noqa: S324 - ordering key, not security


def _body(record: dict) -> dict:
    """Return a record without its ``id``, the part a carried row must keep verbatim."""
    return {key: value for key, value in record.items() if key != "id"}


def carried_ids(records: list[dict], prior: list) -> dict[int, str]:
    """Match each record equal to a prior row in every field except ``id`` to that prior row's id.

    Each prior row and each prior id is matched at most once, so two identical new rows never share one id.

    Args:
        records: The rows about to be minted, before any normalization.
        prior: The prior run's ``findings.jsonl`` rows; entries that are not objects or lack a string id are ignored.

    Returns:
        Record index to the prior id it keeps.

    Examples:
        >>> prior = [{"id": "x-1", "title": "a"}, {"id": "x-2", "title": "b"}, "junk"]
        >>> carried_ids([{"title": "b"}, {"title": "c"}, {"id": "stale", "title": "b"}], prior)
        {0: 'x-2'}
    """
    pool = [row for row in prior if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"]]
    kept: dict[int, str] = {}
    for index, record in enumerate(records):
        body = _body(record)
        match = next((row for row in pool if row["id"] not in kept.values() and _body(row) == body), None)
        if match is not None:
            kept[index] = match["id"]
            pool.remove(match)
    return kept


def mint(records: list[dict], kept: dict[int, str] | None = None) -> list[dict]:
    """Assign stable IDs to every record and return the records in their original order.

    Findings that share a base ID get ``-2``, ``-3``, ... ranked by a hash of their full text, so the same findings
    reported in a different order, or at shifted lines, keep the same IDs. A record listed in ``kept`` keeps its id
    and stays verbatim (it is a carried prior row); the others never take an id a kept record holds.

    Args:
        records: Finding records; each gains an ``id`` and a normalized ``severity`` unless kept.
        kept: Record index to the id it keeps, from ``carried_ids``; ``None`` mints every record.

    Returns:
        ``records``, updated in place.

    Examples:
        >>> a = {"section": "Performance Concerns", "severity": "high", "title": "Slow loop", "file": "a.py"}
        >>> b = {"section": "Performance Concerns", "severity": "low", "title": "slow loop.", "file": "a.py"}
        >>> sorted(f["id"][-2:] for f in mint([a, b]))[0] == "-2", mint([a, b])[0]["id"] == mint([b, a])[1]["id"]
        (True, True)
        >>> taken = mint([dict(a)])[0]["id"]
        >>> [f["id"] == taken for f in mint([dict(b), dict(a, id=taken)], {1: taken})]
        [False, True]
    """
    kept = kept or {}
    reserved = set(kept.values())
    groups: dict[str, list[dict]] = {}
    for index, record in enumerate(records):
        if index in kept:
            record["id"] = kept[index]
            continue
        groups.setdefault(base_id(record), []).append(record)
        record["severity"] = str(record["severity"]).strip().lower()
    for root, group in groups.items():
        free = _suffixed(root, reserved)
        for record in sorted(group, key=_collision_rank):
            record["id"] = next(free)
    return records


def _suffixed(root: str, reserved: set[str]) -> Iterator[str]:
    """Yield the root id and then its numbered suffixes in rank order, skipping every id in ``reserved``.

    >>> ids = _suffixed("api-1a2b3c4d", {"api-1a2b3c4d-2"})
    >>> [next(ids) for _ in range(3)]
    ['api-1a2b3c4d', 'api-1a2b3c4d-3', 'api-1a2b3c4d-4']
    """
    for rank in count(1):
        ident = root if rank == 1 else f"{root}-{rank}"
        if ident not in reserved:
            yield ident


#: Largest findings ledger read whole into memory.
_MAX_JSONL_BYTES: Final = 10 * 1024 * 1024


def read_jsonl(path: Path) -> list[dict]:
    """Read one JSON object per non-blank line.

    Raises:
        ValueError: The file is larger than ``_MAX_JSONL_BYTES``; a findings ledger is a few hundred rows, and the file
            is read whole.
    """
    size = path.stat().st_size
    if size > _MAX_JSONL_BYTES:
        raise ValueError(f"{path} is {size} bytes, over the {_MAX_JSONL_BYTES}-byte cap")
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def write_jsonl_atomic(path: Path, records: list[dict]) -> None:
    """Replace ``path`` with ``records`` in one rename so a reader never sees a half-written file."""
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    """Mint ids in place for the findings file named on the command line."""
    parser = argparse.ArgumentParser(description="Assign stable finding IDs in an oss:review findings.jsonl.")
    parser.add_argument("findings", help="path to findings.jsonl written by the review consolidator")
    parser.add_argument(
        "--carry-from",
        default="",
        help="prior run's findings.jsonl (fill-gaps runs): a row equal to a prior row except its id keeps that id",
    )
    args = parser.parse_args(argv)
    path = Path(args.findings)
    try:
        records = read_jsonl(path)
    except (OSError, ValueError, RecursionError) as exc:
        print(f"! BLOCKED — cannot read {path}: {exc}", file=sys.stderr)
        return 1
    try:
        prior = read_jsonl(Path(args.carry_from)) if args.carry_from else []
    except (OSError, ValueError, RecursionError) as exc:
        print(f"! BLOCKED — cannot read --carry-from {args.carry_from}: {exc}", file=sys.stderr)
        return 1
    problems = [problem for i, rec in enumerate(records, 1) if (problem := validate(rec, i))]
    if problems:
        print("! BLOCKED — invalid findings: " + "; ".join(problems), file=sys.stderr)
        return 1
    write_jsonl_atomic(path, mint(records, carried_ids(records, prior) if args.carry_from else None))
    print(f"minted {len(records)} finding id(s) in {path.as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

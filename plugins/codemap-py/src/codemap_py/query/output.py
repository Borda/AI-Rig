"""Emit one command's result as JSON or TSV, honouring batch capture."""

from __future__ import annotations
import csv
import io
import json
import sys
from pathlib import Path
from codemap_py import query_state as state

# Transitional seam: exclusion rules live in codemap_py.scanner, but this
# module still reaches them through the old bare-name ``_exclusions`` import
# (bin/_exclusions.py, itself a shim onto codemap_py.scanner) via a
# bin/-relative sys.path insert, the same route bin/scan-index used to take.
# Every other import below is a direct package-internal import.
# parents[3] not [2]: this file sits one level deeper than the pre-split query.py
_BIN = Path(__file__).resolve().parents[3] / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))
from .errors import _builtin_print, _die_json  # noqa: E402


def _print(*args: object, **kwargs: object) -> None:
    """Render output and retain the result for the invocation's terminal telemetry.

    In batch mode (:data:`_capture` set) a stdout write is diverted into the capture buffer and neither printed nor
    logged — the batch driver owns the single real stdout write and one telemetry record for the whole batch. stderr
    writes (``file=`` kwarg) always go straight through regardless of mode.
    """
    if not kwargs.get("file") and state._capture is not None:
        state._capture.append(str(args[0]) if args else "")
        return
    if not kwargs.get("file") and state._FORMAT == "tsv":
        # Formatting lives here rather than at each emitter: there are ~34 stdout writers,
        # and converting them individually left most commands silently answering in JSON
        # while the caller had asked for TSV.
        _emit_tsv(str(args[0]) if args else "")
        return
    _builtin_print(*args, **kwargs)
    if kwargs.get("file"):
        return
    raw = str(args[0]) if args else ""
    try:
        result = json.loads(raw)
    except Exception:  # noqa: BLE001 — non-JSON stdout still logs an empty result
        result = {}
    # Direct imported cmd_* calls are not CLI invocations and must not pollute telemetry
    # with the host process's argv or import-age timing.
    if state._invocation is not None:
        state._invocation.result = result


def _tabular_key(payload: dict) -> str | None:
    """Return the payload key holding a table of flat scalar rows, or None.

    A payload qualifies when exactly one of its keys holds a non-empty list of dicts that
    share one key order and carry only scalar values. Repeating those keys once per row is
    what makes JSON expensive for this shape: measured on a 100-row ``central`` result,
    tab-separated rows cost 2130 tokens against 3499 for the same rows as JSON.

    Returns None when the shape does not qualify, so the caller can refuse rather than
    flatten a nested value into an unparsable cell.
    """
    found = None
    for key, value in payload.items():
        if not isinstance(value, list) or not value or not all(isinstance(r, dict) for r in value):
            continue
        cols = list(value[0])
        # A sibling list that does not qualify is not a rival table — skip it rather than
        # veto. Refusing on its account would reject a payload that does carry exactly one
        # renderable table, which is the case the flag exists for.
        if not cols or any(list(r) != cols for r in value):
            continue
        if any(not isinstance(v, (str, int, float, bool)) and v is not None for r in value for v in r.values()):
            continue
        if found is not None:
            return None
        found = key
    return found


def _empty_table_key(payload: dict) -> str | None:
    """Return the key of a lone empty result list, or None.

    A query that matched nothing returns ``{"central": [], ...}``. That is an empty table,
    not an unrenderable one: there are no rows and therefore no column order, but refusing
    it would make the same command exit 0 or 1 depending on the data.

    Returns None when more than one list is present, since then the empty one is not
    unambiguously the result.
    """
    lists = [k for k, v in payload.items() if isinstance(v, list)]
    if len(lists) == 1 and not payload[lists[0]]:
        return lists[0]
    return None


def _to_tsv(rows: list[dict]) -> str:
    """Render *rows* as a header line plus one tab-separated line per row.

    ``csv`` with ``QUOTE_MINIMAL`` quotes any field containing a tab, newline, or quote, so a module name or path
    carrying one round-trips instead of silently splitting a column.
    """
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter="\t", lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    cols = list(rows[0])
    writer.writerow(cols)
    for row in rows:
        writer.writerow(["" if row[c] is None else row[c] for c in cols])
    return buf.getvalue().rstrip("\n")


def _emit_tsv(raw: str) -> None:
    """Render one already-serialised JSON result as TSV on stdout, or refuse.

    Called from :func:`_print` for stdout writes when ``--format tsv`` is active, so every
    emitter is covered by one seam instead of each having to opt in.

    The metadata envelope each payload carries (``index``, coverage flags) has no tabular
    shape, so it goes to stderr rather than being dropped: a caller reading rows on stdout
    still sees staleness and completeness warnings. Bytes are written UTF-8 with explicit
    ``\n`` through ``sys.stdout.buffer``, because Windows text-mode stdout would rewrite an
    embedded newline inside a quoted cell to CRLF and a legacy console encoding would raise
    on a non-ASCII path — JSON escapes both, TSV does not.

    Args:
        raw: the JSON text the emitter produced.
    """
    try:
        payload = json.loads(raw)
    except ValueError:
        _builtin_print(raw)
        return
    empty = _empty_table_key(payload) if isinstance(payload, dict) else None
    if empty is not None:
        # An empty result is data, not a format error. Refusing it would make the same
        # command succeed or fail depending on how many rows the index happens to hold.
        envelope = {k: v for k, v in payload.items() if k != empty}
        if envelope:
            sys.stderr.write(json.dumps(envelope) + "\n")
        if state._invocation is not None:
            state._invocation.result = payload
        return
    key = _tabular_key(payload) if isinstance(payload, dict) else None
    if key is None:
        # The error emitter bypasses format conversion; errors remain JSON.
        _die_json(
            {
                "error": "format_not_tabular",
                "detail": "--format tsv needs exactly one list of flat, uniform records; this result has none",
            }
        )
    envelope = {k: v for k, v in payload.items() if k != key}
    if envelope:
        sys.stderr.write(json.dumps(envelope) + "\n")
    sys.stdout.buffer.write(_to_tsv(payload[key]).encode("utf-8") + b"\n")
    sys.stdout.flush()
    if state._invocation is not None:
        state._invocation.result = payload

#!/usr/bin/env python3
"""Classify a ``codemap-py query test-impact`` outcome, persist its fields and print what the report needs.

The test-impact skill reads this helper's stdout to render its report, so every value the report shows — the test
file list, ``via_call``/``via_mock`` counts, the pytest command and the completeness signals — is printed here and
never reconstructed by the model.

Failure routing depends on why the query failed. Every engine error prints a JSON object on stdout. A target that does
not resolve (typo, ambiguous name, module absent from the index) carries the ``rejected_target`` key, together with
``candidates`` or ``suggestions`` when the engine has them: the fix is a different target, so no rebuild advice is
given. Exit 2 without that key is a rejected argument. Anything else (missing, oversized or corrupt index, self-check
failure) is a provider failure and gets the rebuild advice. Branching on the key, not the exit code, matters because
exit 3 covers both "module not indexed" and "index is not valid JSON".

Usage:
    printf '%s' "$RESULT" | python summarize_test_impact.py --rc "$_TI_RC" --stderr-file "$_TI_ERR" \
        --output-prefix "codemap-${_CM_PROJ}"

Sentinels (``${TMPDIR}/<prefix>-ti-<name>-<CSID>``): ``not-covered``, ``hint``, ``total``, ``pytest-cmd``,
``query-complete``, ``stale``, ``completeness-reason``, ``test-files``, ``via-call``, ``via-mock``.

Exit codes:
    0 — query succeeded; summary printed, sentinels written
    1 — query failed or returned no parsable JSON, or a sentinel could not be written safely; the reason and the next
        step are printed
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

#: This ``bin/`` directory, put on ``sys.path`` so the sibling ``resolve_index_env`` import also resolves when
#: ``pytest --doctest-modules`` imports this file by path.
_BIN_DIR = str(Path(__file__).resolve().parent)
if _BIN_DIR not in sys.path:
    sys.path.insert(0, _BIN_DIR)

from resolve_index_env import (  # noqa: E402  (needs the sys.path insert above)
    _resolve_csid,
    _resolve_tmpdir,
    _validate_output_prefix,
    _write_sentinel_file,
)

#: Engine payload key present on every target-resolution failure (``codemap_py.query.errors.REJECTED_TARGET_KEY``).
REJECTED_TARGET_KEY = "rejected_target"

#: Engine exit status for a malformed or rejected caller argument.
EXIT_BAD_INPUT = 2

#: Stderr lines echoed under a provider failure.
STDERR_HEAD_LINES = 5

#: Advice printed only for provider failures — a target typo is never fixed by rebuilding.
REBUILD_ADVICE = "Rebuild with /codemap-py:scan-codebase, then re-run."


def extract_payload(text: str) -> dict | None:
    """Return the JSON object in the query's stdout, tolerating log lines around it.

    Args:
        text: Raw stdout of the query.

    Returns:
        The parsed object, or ``None`` when no line holds a JSON object.

    Examples:
        >>> extract_payload('{"total": 0}')
        {'total': 0}
        >>> extract_payload('warning: stale\\n{"total": 1}\\n')
        {'total': 1}
        >>> extract_payload("not json") is None
        True
    """
    candidates = [text.strip()] + [line.strip() for line in reversed(text.splitlines())]
    for candidate in candidates:
        if not candidate.startswith("{"):
            continue
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def describe_failure(rc: int, payload: dict | None, stderr_text: str) -> list[str]:
    """Build the lines explaining a failed query and the matching next step.

    Args:
        rc: Query exit status (non-zero).
        payload: Parsed stdout error object, if any.
        stderr_text: Captured query stderr.

    Returns:
        Lines to print, ending with the next step for this failure class.
    """
    payload = payload or {}
    error = str(payload.get("error") or "").strip()
    if REJECTED_TARGET_KEY in payload:
        lines = [f"! target did not resolve: {error or payload[REJECTED_TARGET_KEY]}"]
        options = payload.get("candidates") or payload.get("suggestions") or []
        lines.extend(f"  candidate: {option}" for option in options)
        lines.append("Index is not at fault — do not rebuild. Re-run with one exact candidate or a corrected target.")
        return lines
    head = stderr_text.strip().splitlines()[:STDERR_HEAD_LINES]
    lines = [f"! {error}"] if error else []
    lines.extend(f"  {line}" for line in head)
    if rc == EXIT_BAD_INPUT:
        lines.append("Arguments were rejected — fix them and re-run; this is not an index problem.")
    else:
        lines.append(REBUILD_ADVICE)
    return lines


def result_fields(payload: dict) -> dict[str, str]:
    """Map a successful payload onto the sentinel values the skill persists.

    Args:
        payload: Parsed test-impact JSON.

    Returns:
        Sentinel name to single-line value.

    Examples:
        >>> result_fields({"test_files": ["tests/t.py"], "via_call": 1, "via_mock": 0})["total"]
        '1'
    """
    index = payload.get("index") or {}
    test_files = payload.get("test_files") or []
    return {
        "not-covered": json.dumps(index.get("not_covered") or []),
        "hint": str(index.get("hint") or ""),
        "total": str(len(test_files)),
        "pytest-cmd": str(payload.get("pytest_cmd") or ""),
        # null means the engine omitted the signal: never coerce it to false/true
        "query-complete": json.dumps(index.get("query_complete")),
        "stale": json.dumps(index.get("stale")),
        # absent by design on a compact complete answer — never defaulted to a fake reason
        "completeness-reason": str(index.get("completeness_reason") or ""),
        "test-files": json.dumps(test_files),
        "via-call": json.dumps(payload.get("via_call")),
        "via-mock": json.dumps(payload.get("via_mock")),
    }


def summary_lines(payload: dict, fields: dict[str, str]) -> list[str]:
    """Render the compact summary the skill's report is built from.

    Args:
        payload: Parsed test-impact JSON.
        fields: Output of :func:`result_fields` for the same payload.

    Returns:
        Lines to print, test files one per line.
    """
    test_files = payload.get("test_files") or []
    lines = [
        f"qname: {payload.get('qname', '')}",
        f"total: {fields['total']} (via_call: {fields['via-call']}, via_mock: {fields['via-mock']})",
        "test_files:",
    ]
    lines.extend(f"- {path}" for path in test_files)
    lines.extend(
        [
            f"pytest_cmd: {fields['pytest-cmd']}",
            f"not_covered: {fields['not-covered']}",
            f"hint: {fields['hint']}",
            f"query_complete: {fields['query-complete']} · stale: {fields['stale']}"
            f" · completeness_reason: {fields['completeness-reason'] or '-'}",
        ]
    )
    return lines


def _write_sentinels(prefix: str, fields: dict[str, str]) -> None:
    """Persist each field to its session-scoped sentinel file.

    ``TMPDIR`` and ``CSID`` go through the shared resolvers, and each write refuses a planted symlink or a file another
    user owns (see ``_write_sentinel_file``).

    Raises:
        OSError: When a sentinel path is a symlink or a file owned by another user.
        ValueError: When ``prefix`` is not a bare filename prefix.
    """
    tmp_dir, csid, safe_prefix = Path(_resolve_tmpdir()), _resolve_csid(), _validate_output_prefix(prefix)
    for name, value in fields.items():
        _write_sentinel_file(tmp_dir / f"{safe_prefix}-ti-{name}-{csid}", value + "\n")


def _build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser for this helper."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rc", type=int, required=True, help="Exit status of the test-impact query")
    parser.add_argument("--stderr-file", default=None, help="File holding the query's captured stderr")
    parser.add_argument("--output-prefix", required=True, help="Sentinel name prefix, e.g. codemap-<project>")
    return parser


def _read_stderr(path: str | None) -> str:
    """Return the captured stderr text, or ``""`` when the file is absent or unreadable."""
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def main(argv: list[str] | None = None) -> int:
    """Read the query stdout from stdin, then print either the failure routing or the result summary.

    Args:
        argv: Override ``sys.argv[1:]`` (mainly for testing).

    Returns:
        ``0`` for a parsed successful result, ``1`` otherwise.
    """
    args = _build_parser().parse_args(argv)
    payload = extract_payload(sys.stdin.read())
    if args.rc != 0:
        print("\n".join(describe_failure(args.rc, payload, _read_stderr(args.stderr_file))), file=sys.stderr)
        return 1
    if payload is None:
        print(
            "! test-impact returned non-JSON output — NOT an empty result; " + REBUILD_ADVICE,
            file=sys.stderr,
        )
        return 1
    fields = result_fields(payload)
    try:
        _write_sentinels(args.output_prefix, fields)
    except (OSError, ValueError) as exc:
        print(f"! test-impact sentinels not written: {exc}", file=sys.stderr)
        return 1
    print("\n".join(summary_lines(payload, fields)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

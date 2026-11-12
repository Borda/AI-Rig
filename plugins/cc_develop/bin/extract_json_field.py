#!/usr/bin/env python
"""Recover the rightmost complete JSON object from text and print a requested field.

Replaces the inline ``python -c`` fallback used by ``develop:plan`` when ``jq``
is unavailable or the agent response wraps JSON in prose. Two operations,
selected by the ``<field>`` argument:

* ``<field> == "."`` or ``<field> == "_object"`` — print the recovered JSON
  object as a compact one-line string (matches the prior inline-code contract).
* Any other ``<field>`` — extract that top-level key's value. Strings print raw
  (no surrounding quotes); non-strings print as JSON (e.g. ``true``, ``42``,
  ``[1, 2]``).

Recovery scans candidate object starts, discards objects nested within another
successfully parsed object, and returns the rightmost remaining object. This
tolerates surrounding prose and stray opening braces before the answer.

Usage:
    python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/extract_json_field.py" \\
        <field> [<json-or-text>]

When the second argument is omitted (or is ``-``), the text is read from stdin.

The positionals are captured opaquely (the ``<json-or-text>`` blob may begin with ``--``);
argparse is present only to supply ``-h/--help``.

Exit codes:
    0  Success — value printed to stdout.
    1  No balanced JSON object could be recovered from the input, or the input is over the size cap.
    2  Object recovered but the requested field is absent.
    3  Usage error (no field argument).
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

#: Field names that request the entire recovered JSON object instead of one field of it.
_WHOLE_OBJECT_ALIASES = frozenset({".", "_object", ""})
#: Most characters of input accepted; an agent reply is far smaller, and recovery cost grows with input length.
_MAX_INPUT_CHARS = 1_000_000
#: Brace positions that may fail to parse before the scan gives up; each failure can cost a pass over the input.
_MAX_FAILED_STARTS = 1000


def recover_json_object(text: str) -> dict[str, Any] | None:
    """Return the last (rightmost) top-level balanced JSON object in ``text``.

    Decodes one object at each candidate ``{`` and skips starts inside any
    recovered object. Only the last accepted object is retained, so sibling
    objects need no pairwise containment scan. Input length and failed
    candidate attempts are bounded. Stray opening braces and prose before or
    after an answer remain supported; the rightmost accepted outer object
    supplies the final answer envelope.

    Pure function — no I/O, deterministic.

    Args:
        text: Raw text possibly containing a JSON object plus surrounding
            prose, reasoning, or whitespace.

    Returns:
        The parsed object as a ``dict``, or ``None`` if no balanced JSON
        object could be recovered or the input exceeds the size limit.

    Examples:
        >>> recover_json_object('{"a":"sw","ok":true}')
        {'a': 'sw', 'ok': True}
        >>> recover_json_object('thinking... here it is: {"verdict":"PASS"}')
        {'verdict': 'PASS'}
        >>> recover_json_object('prose with { stray brace and {"ok":false}')
        {'ok': False}
        >>> recover_json_object('no json here at all') is None
        True
        >>> recover_json_object('') is None
        True
        >>> recover_json_object('{"nested":{"k":1}} trailing prose')
        {'nested': {'k': 1}}
        >>> recover_json_object('  {"ok": true}\\n\\nthen extra prose')
        {'ok': True}
        >>> recover_json_object('{"a":1} and then {"b":2}')
        {'b': 2}
    """
    if len(text) > _MAX_INPUT_CHARS:
        return None
    decoder = json.JSONDecoder()
    last_object: dict[str, Any] | None = None
    covered_until = 0  # starts inside an already-recovered object cannot be outermost
    failed_starts = 0
    for start, ch in enumerate(text):
        if ch != "{" or start < covered_until:
            continue
        # raw_decode reads one value from `start` and ignores what follows, so prose after the object
        # needs no truncation loop and each start costs one pass instead of one per closing brace.
        try:
            parsed, end = decoder.raw_decode(text, start)
        except (json.JSONDecodeError, RecursionError):
            failed_starts += 1
            if failed_starts >= _MAX_FAILED_STARTS:
                break
            continue
        last_object = parsed
        covered_until = end
    return last_object


def format_field(value: Any) -> str:
    """Format a JSON value for stdout.

    Strings print raw (no surrounding quotes) so shells can capture them
    cleanly; every other type uses ``json.dumps`` with its default spacing.

    Args:
        value: Any JSON-decoded value.

    Returns:
        The stdout representation (no trailing newline).

    Examples:
        >>> format_field("PASS")
        'PASS'
        >>> format_field(True)
        'true'
        >>> format_field(42)
        '42'
        >>> format_field([1, 2, 3])
        '[1, 2, 3]'
        >>> format_field({"k": "v"})
        '{"k": "v"}'
        >>> format_field(None)
        'null'
    """
    if isinstance(value, str):
        return value
    return json.dumps(value)


def main(argv: list[str] | None = None) -> int:
    """Extract a requested field from a JSON object supplied through the command line.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``).

    Returns:
        0 on success, 1 if no object was recovered, 2 if the field is absent, or 3 on usage error.
    """
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]
    raw = list(sys.argv[1:] if argv is None else argv)

    # Handle ``-h/--help`` via argparse, then treat positionals opaquely. The <json-or-text>
    # blob may begin with ``--``, which argparse would reject as an unknown option — so the
    # positionals are NOT fed through parse_args, and the missing-field case keeps exit 3
    # (argparse's own missing-required exit is 2).
    if raw and raw[0] in {"-h", "--help"}:
        parser = argparse.ArgumentParser(
            prog="extract_json_field.py",
            description="Recover a JSON object from text and print a field.",
        )
        parser.add_argument("field", nargs="?", help="Top-level field name, or '.'/'_object' for the whole object.")
        parser.add_argument("text", nargs="?", help="JSON-or-text blob; omit or pass '-' to read stdin.")
        parser.parse_args(raw)  # exits 0 after printing help

    args = raw
    if not args:
        print(
            "Usage: extract_json_field.py <field> [<json-or-text>]",
            file=sys.stderr,
        )
        return 3

    field = args[0]
    if len(args) >= 2 and args[1] != "-":
        text = args[1]
    else:
        text = sys.stdin.read(_MAX_INPUT_CHARS + 1)
    if len(text) > _MAX_INPUT_CHARS:
        print(f"! extract_json_field: input exceeds {_MAX_INPUT_CHARS} characters", file=sys.stderr)
        return 1

    obj = recover_json_object(text)
    if obj is None:
        print("! extract_json_field: no balanced JSON object found", file=sys.stderr)
        return 1

    if field in _WHOLE_OBJECT_ALIASES:
        sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
        return 0

    if field not in obj:
        print(f"! extract_json_field: field {field!r} not present in recovered object", file=sys.stderr)
        return 2

    sys.stdout.write(format_field(obj[field]) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

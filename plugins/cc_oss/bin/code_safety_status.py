#!/usr/bin/env python3
"""Record scan evidence for one PR head and decide when review or resolve may skip the PR-code question.

``oss:review`` checks its own scan and writes ``code-safety.json`` beside ``head-sha.txt`` in the report directory.
``oss:resolve`` uses that record and its own fresh scan. Each workflow skips the question only when its required scans
of the exact head found nothing, and they differ in what then runs:

- review binds an exact-head approval and runs the scanned head without the sandbox; the guard refuses any tree that
  differs from that head;
- resolve also needs a usable sandbox on this host and keeps every run in it, because it merges the base and edits
  files before it tests, so what runs is never exactly the scanned head.

The scan is a static heuristic and the record stores evidence only.

Usage:
    code_safety_status.py write --scan SCAN_JSON --out FILE [--run-decision auto]
    code_safety_status.py write --same-repo --head SHA --out FILE
    code_safety_status.py write --unscanned --head SHA --out FILE
    code_safety_status.py check --status FILE --scan SCAN_JSON --head SHA
    code_safety_status.py check-scan --scan SCAN_JSON --head SHA

``write`` prints nothing and exits 0; it exits 2 when ``--scan`` is unreadable or not a guard report.

``check-scan`` and ``check`` print one line and exit 0: ``auto`` when every condition holds, otherwise
``ask reason=<token>``. Both fail closed on missing, unreadable or mismatched input. ``check-scan`` needs a guard
report with risk level ``none``, a digest and a named scope (``whole_tree`` or a full ``base`` commit id), whose full
head (40 or 64 lowercase hex digits) equals ``--head``. ``check`` needs all of:

- the status file is schema 1 (an integer, never ``true`` or ``1.0``), origin ``fork`` and ``scanned`` true;
- the status head, the fresh scan head and ``--head`` are the same full commit id (40 or 64 lowercase hex digits);
- the review's risk level and the fresh scan's risk level are both ``none``;
- the fresh scan's ``isolation_available`` is exactly ``sandbox`` (this host can start the sandbox); anything else,
  ``host-sandbox`` and an absent value included, gives ``ask reason=no-sandbox``.

Consumers: ``skills/resolve/modes/exec-approval.md`` (PR-code decision) and
``skills/review/templates/agent-prompts.md`` (PR-code decision and report-directory sidecar).

Examples:
    >>> decide_scan({"head": "a" * 40, "risk": {"level": "none"}, "whole_tree": True, "digest": "d"}, "a" * 40)
    'auto'
    >>> decide_scan({"head": "a" * 40, "risk": {"level": "none"}}, "a" * 40)
    'ask reason=no-scan'
    >>> decide_scan({"head": "a" * 40, "risk": {"level": "borderline"}, "whole_tree": True, "digest": "d"}, "a" * 40)
    'ask reason=findings-present'
    >>> clean = {"head": "a" * 40, "risk": {"level": "none"}, "whole_tree": True, "digest": "d"}
    >>> status = {"schema": 1, "origin": "fork", "scanned": True, "head": "a" * 40, "risk_level": "none"}
    >>> decide(status, {**clean, "isolation_available": "sandbox"}, "a" * 40)
    'auto'
    >>> decide(status, {**clean, "isolation_available": "env-only"}, "a" * 40)
    'ask reason=no-sandbox'
    >>> decide(status, {**clean, "head": "b" * 40, "isolation_available": "sandbox"}, "b" * 40)
    'ask reason=head-mismatch'
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SCHEMA = 1
CLEAN_LEVEL = "none"
#: The guard's probed tier for a host that can start its sandbox; resolve keeps every run in it.
SANDBOX_TIER = "sandbox"
#: A full commit id: SHA-1 (40) or SHA-256 (64) lowercase hex; an abbreviation never counts as the same head.
_FULL_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


def _is_full_sha(value: object) -> bool:
    """Tell whether ``value`` is a full-length lowercase commit id."""
    return isinstance(value, str) and _FULL_SHA.fullmatch(value) is not None


def _load(path: str) -> dict[str, object] | None:
    """Return the JSON object at ``path``, or ``None`` when it is missing, unreadable or not an object."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, RecursionError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _risk_level(scan: dict[str, object]) -> str:
    """Read ``risk.level`` from a guard report, or an empty string when it is absent."""
    risk = scan.get("risk")
    level = risk.get("level") if isinstance(risk, dict) else None
    return level if isinstance(level, str) else ""


def from_scan(scan: dict[str, object]) -> dict[str, object]:
    """Build the sidecar record for a fork PR from a guard ``scan`` report."""
    return {
        "schema": SCHEMA,
        "origin": "fork",
        "scanned": True,
        "head": scan.get("head"),
        "base": scan.get("base"),
        "verdict": scan.get("verdict"),
        "risk_level": _risk_level(scan),
        "scan_digest": scan.get("digest"),
    }


def _scan_scope_ok(scan: dict[str, object]) -> bool:
    """Tell whether a guard report names its scope: a whole-tree scan, or a full merge-base commit id, and a digest."""
    whole = scan.get("whole_tree")
    if not isinstance(whole, bool) or not isinstance(scan.get("digest"), str) or not scan["digest"]:
        return False
    return whole or _is_full_sha(scan.get("base"))


def decide_scan(scan: dict[str, object] | None, head: str) -> str:
    """Return whether review can skip the PR-code question for this scan."""
    if scan is None or not isinstance(scan.get("head"), str) or not _scan_scope_ok(scan):
        return "ask reason=no-scan"
    if not _is_full_sha(head) or scan["head"] != head:
        return "ask reason=head-mismatch"
    if _risk_level(scan) != CLEAN_LEVEL:
        return "ask reason=findings-present"
    return "auto"


def decide(status: dict[str, object] | None, scan: dict[str, object] | None, head: str) -> str:
    """Return whether resolve can skip the PR-code question: ``auto`` or ``ask reason=<token>``.

    Anything not proven clean asks, and so does a host without a usable sandbox: resolve binds the sandbox tier for the
    skipped question, so without one every later run would be refused.
    """
    schema = None if status is None else status.get("schema")
    if status is None or type(schema) is not int or schema != SCHEMA:
        return "ask reason=no-review-status"
    if status.get("origin") != "fork" or status.get("scanned") is not True:
        return "ask reason=review-did-not-scan"
    if scan is None or not isinstance(scan.get("head"), str) or not _scan_scope_ok(scan):
        return "ask reason=no-fresh-scan"
    if not _is_full_sha(head) or status.get("head") != head or scan.get("head") != head:
        return "ask reason=head-mismatch"
    if status.get("risk_level") != CLEAN_LEVEL or _risk_level(scan) != CLEAN_LEVEL:
        return "ask reason=findings-present"
    if scan.get("isolation_available") != SANDBOX_TIER:
        return "ask reason=no-sandbox"
    return "auto"


def _write(args: argparse.Namespace) -> int:
    """Write the sidecar record for the chosen origin."""
    if args.scan:
        scan = _load(args.scan)
        if scan is None or "risk" not in scan or "head" not in scan:
            print(f"code-safety: {args.scan} is not a guard scan report", file=sys.stderr)
            return 2
        record = from_scan(scan)
        if args.run_decision:
            record["run_decision"] = args.run_decision
    elif args.head:
        record = {"schema": SCHEMA, "origin": "same-repo" if args.same_repo else "fork", "scanned": False,
                  "head": args.head}  # fmt: skip
    else:
        print("code-safety: write needs --scan, or --head with --same-repo or --unscanned", file=sys.stderr)
        return 2
    Path(args.out).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Write a scan record or print the review or resolve decision."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    write = sub.add_parser("write")
    write.add_argument("--scan")
    write.add_argument("--head")
    write.add_argument("--same-repo", action="store_true")
    write.add_argument("--unscanned", action="store_true")
    write.add_argument("--run-decision", choices=("auto",))
    write.add_argument("--out", required=True)
    check = sub.add_parser("check")
    check.add_argument("--status", required=True)
    for parser_mode in (check, sub.add_parser("check-scan")):
        parser_mode.add_argument("--scan", required=True)
        parser_mode.add_argument("--head", required=True)
    args = parser.parse_args(argv)
    if args.mode == "write":
        return _write(args)
    scan = _load(args.scan)
    if args.mode == "check-scan":
        print(decide_scan(scan, args.head))
    else:
        print(decide(_load(args.status), scan, args.head))
    return 0


if __name__ == "__main__":
    sys.exit(main())

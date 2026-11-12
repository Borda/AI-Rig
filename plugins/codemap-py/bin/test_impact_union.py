#!/usr/bin/env python3
"""Produce a mock-excluding test union for a complete working-tree change request.

Purpose:
    Build one batch from changed modules and symbols instead of issuing per-target shell calls.

Scope:
    Consume a captured diff-impact response and invoke the PATH-resolved Codemap launcher once.

Usage:
    python test_impact_union.py <diff-impact-json-path>

Outputs:
    JSON containing the sorted test union, host-quoted pytest command and completeness caveats.

Failure:
    Exit 1 with a diagnostic for unreadable input, an unavailable launcher or any failed batch item.

Used by:
    The test-impact skill's whole-change route when the user requests mock exclusion.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from summarize_test_impact import describe_failure, diff_summary, extract_payload

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from codemap_py.query.output import _pytest_command  # noqa: E402


def batch_targets(payload: dict) -> list[str]:
    """Collect each distinct changed module and symbol in input order.

    Args:
        payload: Full diff-impact response, not the compact printed summary.

    Returns:
        Targets to query with mock-only matches excluded.

    Raises:
        ValueError: When a module or its symbol list is malformed or no mapped target exists.
    """
    diff_summary(payload)
    targets: list[str] = []
    for module in payload["changed_modules"]:
        symbols = module.get("changed_symbols")
        if not isinstance(symbols, list) or any(not isinstance(symbol, str) or not symbol for symbol in symbols):
            raise ValueError("diff-impact response is missing changed-symbol targets")
        targets.extend([module["module"], *symbols])
    if not targets:
        raise ValueError("no mapped targets for mock-excluding analysis; unmapped files remain coverage gaps")
    return list(dict.fromkeys(targets))


def run_batch(targets: list[str]) -> dict:
    """Run all mock-excluding queries through a single resolved launcher invocation.

    Args:
        targets: Distinct module and symbol names from the captured diff.

    Returns:
        Decoded batch response; individual item failures are checked by the consumer.

    Raises:
        OSError: When the launcher is absent or cannot execute.
        subprocess.TimeoutExpired: When the batch exceeds its bounded query time.
        ValueError: When the query fails or produces no JSON object.
    """
    launcher = shutil.which("codemap-py")
    if launcher is None:
        raise FileNotFoundError("codemap-py not on PATH — install the codemap-py plugin")
    request = [{"cmd": "test-impact", "args": [target, "--no-mocks"]} for target in targets]
    result = subprocess.run(
        [launcher, "query", "batch", "-"],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        timeout=110,
    )
    payload = extract_payload(result.stdout)
    if result.returncode != 0:
        raise ValueError("\n".join(describe_failure(result.returncode, payload, result.stderr)))
    if payload is None:
        raise ValueError(f"batch returned non-JSON output: {result.stderr[:1000]}")
    return payload


def union_summary(diff: dict, targets: list[str], batch: dict) -> dict:
    """Union successful test lists while retaining each target's coverage caveats.

    Args:
        diff: Original response containing unmapped paths and module errors.
        targets: Ordered batch targets used to associate per-query caveats.
        batch: Decoded response from the one batch invocation.

    Returns:
        Compact whole-change report with a host-quoted command and target caveats.

    Raises:
        ValueError: When any expected result is missing, failed or malformed.
    """
    entries = batch.get("batch")
    if not isinstance(entries, list) or len(entries) != len(targets):
        raise ValueError(f"batch returned missing results: {batch.get('error', 'item count mismatch')}")
    files: set[str] = set()
    caveats: list[dict] = []
    for target, entry in zip(targets, entries):
        result = entry.get("result") or {}
        if entry.get("ok") is not True or "error" in result:
            raise ValueError(f"test-impact {target} failed: {entry.get('error') or result.get('error')}")
        test_files = result.get("test_files")
        if not isinstance(test_files, list) or any(not isinstance(path, str) for path in test_files):
            raise ValueError(f"test-impact {target} returned no valid test file list")
        files.update(test_files)
        index = result.get("index") or {}
        if (
            index.get("query_complete") is not True
            or index.get("stale") is not False
            or any(index.get(key) for key in ("not_covered", "hint", "completeness_reason"))
        ):
            caveats.append({"target": target, "index": index})
    ordered = sorted(files)
    summary = diff_summary(diff)
    summary["test_impact"] = {"test_files": ordered, "total": len(ordered), "pytest_cmd": _pytest_command(ordered)}
    summary["diff_index"] = summary["index"]
    summary["index"] = batch.get("index") or {}
    summary["target_caveats"] = caveats
    return summary


def main(argv: list[str] | None = None) -> int:
    """Read the captured diff and print a union only after every batch item succeeds.

    Args:
        argv: Optional CLI argument override.

    Returns:
        Zero for a validated union, one for a failed input or query.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("diff_impact", type=Path, help="Path containing the captured diff-impact stdout")
    args = parser.parse_args(argv)
    try:
        diff = extract_payload(args.diff_impact.read_text(encoding="utf-8"))
        if diff is None:
            raise ValueError("diff-impact input is not JSON")
        targets = batch_targets(diff)
        summary = union_summary(diff, targets, run_batch(targets))
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"! whole-change test-impact failed: {exc} — NOT an empty result", file=sys.stderr)
        return 1
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python
"""Report what oss:resolve did with each review finding back to the review that raised it.

``oss:resolve`` consumes a review report from ``.reports/review/pr-<N>/run-<NNN>/``. When it finishes, this script
appends one JSON line per selected item that carries a review ``finding_id`` to ``resolution.jsonl`` in that same
review directory. The next ``oss:review`` of the PR reads the file, so it can confirm earlier fixes at the new head and
avoid re-reporting findings resolve already rejected with evidence.

Each record: ``finding_id``; the finding's ``title``, ``section``, ``file`` and ``line`` (so a later review can match
on location and claim, since a reworded title mints a different id); ``item_id``; ``shared_with_github`` (true when
the finding was folded into a GitHub item, so the verdict covers the combined item, not the finding alone);
``verdict`` (``fixed``, ``self-resolved``, ``rejected``, ``skipped`` or ``pending``); ``sha`` (short commit hash when
one carries the item, else ``""``); ``why`` (the challenge or skip reason); and ``run`` (the resolve run directory
name, so re-running this script for the same run appends nothing new).

``fixed`` and ``self-resolved`` need implementation evidence, not only a challenge that accepted the item: a Phase 2
commit, a Codex-direct item record, or a ``[resolve No.<id>]`` commit after ``--base-sha``. An item a merge conflict
left unapplied, or one accepted but never implemented, is reported ``pending``. A ``rejected`` verdict records that
resolve's challenger rejected the finding, which includes findings it could not verify; the next review re-checks them
at the new head.

Inputs are the run's own durable records, never retyped by the orchestrator: ``action-items.jsonl``,
``selected-items.txt``, ``challenge-log.txt``, ``skipped-items.txt``, ``phase2-commits.jsonl``,
``c1-item-summary.tsv`` and ``merge-result.json`` in ``$IMPL_DIR``, plus the review's ``findings.jsonl`` (or resolve's
parsed ``report-findings.jsonl`` for an older report) for titles and locations.

consumers: plugins/cc_oss/skills/resolve/SKILL.md (Step 11); read back by plugins/cc_oss/skills/review/SKILL.md
(existing-report guard)

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/append_resolution.py" --impl-dir "$IMPL_DIR" \\
        --review-dir "$(dirname "$REPORT_FILE")" --base-sha "$BASE_SHA"

Exit codes:
    0 — appended (or nothing to append)
    1 — unreadable inputs or a missing review directory
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Final

#: challenge-log ``resolution=`` value → verdict reported back to the review.
VERDICT_BY_RESOLUTION: Final = {
    "as-suggested": "fixed",
    "codex-direct": "fixed",
    "self-resolved": "self-resolved",
    "rejected": "rejected",
}
#: challenge-log.txt producer format (action-item-dispatch.md append block): fixed key order, free text only in the
#: last four fields — anchoring on the known keys keeps a reviewer's quoted ``x=1`` from splitting a field.
_LOG_LINE_RE: Final = re.compile(
    r"^id=(?P<id>\d+) resolution=(?P<resolution>\S+) evidence=(?P<evidence>\S+) suggestion=(?P<suggestion>\S+)"
    r" finding=(?P<finding>.*?) evidence_why=(?P<evidence_why>.*?) suggestion_why=(?P<suggestion_why>.*?)"
    r" detail=(?P<detail>.*)$"
)


def parse_challenge_log(text: str) -> dict[int, dict[str, str]]:
    """Return the last challenge-log record per item id (later lines win, matching how resolve appends).

    >>> line = ("id=3 resolution=rejected evidence=REJECT suggestion=— finding=a=1"
    ...         " evidence_why=gone now suggestion_why=— detail=x")
    >>> parse_challenge_log(line)[3]["evidence_why"]
    'gone now'
    """
    records: dict[int, dict[str, str]] = {}
    for line in text.splitlines():
        match = _LOG_LINE_RE.match(line.strip())
        if match:
            records[int(match["id"])] = {key: value.strip() for key, value in match.groupdict().items()}
    return records


def parse_skipped(text: str) -> dict[int, str]:
    """Return skip reasons per item id from the tab-separated ``skipped-items.txt``."""
    skipped: dict[int, str] = {}
    for line in text.splitlines():
        item_id, _, reason = line.partition("\t")
        if item_id.strip().isdigit():
            skipped[int(item_id)] = reason.strip()
    return skipped


def commit_for(item_id: int, cwd: Path, base_sha: str = "") -> str:
    """Return the short hash of the newest commit tagged ``[resolve No.<id>]``, or ``""`` when none carries it.

    Item ids restart at 1 every resolve run, so the search is limited to commits after ``base_sha`` (the run's starting
    head) whenever it is known; otherwise a commit from an earlier run on the same branch would match.
    """
    rev_range = [f"{base_sha}..HEAD"] if base_sha else []
    try:
        out = subprocess.run(  # noqa: S603 - argv list, no shell
            ["git", "log", "-1", "--format=%h", "--fixed-strings", f"--grep=[resolve No.{item_id}]", *rev_range],  # noqa: S607 - git resolved via PATH on purpose
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _read_jsonl(path: Path) -> list[dict]:
    """Read one JSON object per non-blank line; a missing file reads as empty."""
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def implemented_ids(impl_dir: Path) -> set[int]:
    """Return item ids this run actually implemented: Phase 2 commits plus Codex-direct items, minus unmerged ones."""
    done = {int(row["item_id"]) for row in _read_jsonl(impl_dir / "phase2-commits.jsonl") if row.get("sha")}
    c1_path = impl_dir / "c1-item-summary.tsv"
    if c1_path.exists():
        done |= set(parse_skipped(c1_path.read_text(encoding="utf-8")))
    result_path = impl_dir / "merge-result.json"
    if result_path.exists():
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if result.get("conflict"):
            done -= {int(result["conflict"]["item_id"]), *map(int, result.get("remaining") or [])}
    return done


def _verdict(entry: dict[str, str], implemented: bool, sha: str) -> tuple[str, str]:
    """Combine the challenge verdict with implementation evidence into the reported ``(verdict, why)``."""
    verdict = VERDICT_BY_RESOLUTION.get(entry.get("resolution", ""), "")
    if verdict == "rejected":
        return verdict, entry.get("evidence_why", "")
    why = entry.get("suggestion_why", "")
    if verdict in ("fixed", "self-resolved") and not (implemented or sha):
        return "pending", "accepted at challenge; no implementation record for this item"
    if not verdict:
        verdict = "fixed" if (implemented or sha) else "pending"
    return verdict, why


def build_records(
    impl_dir: Path, run: str, cwd: Path, *, findings: Path | None = None, base_sha: str = ""
) -> list[dict]:
    """Build one resolution record per selected item that came from a review finding."""
    items = {int(record["id"]): record for record in _read_jsonl(impl_dir / "action-items.jsonl")}
    by_finding = {str(f.get("id")): f for f in _read_jsonl(findings)} if findings else {}
    selected_path = impl_dir / "selected-items.txt"
    selected_text = selected_path.read_text(encoding="utf-8") if selected_path.exists() else ""
    selected = [int(tok) for tok in selected_text.split() if tok.isdigit()]
    log_path, skip_path = impl_dir / "challenge-log.txt", impl_dir / "skipped-items.txt"
    log = parse_challenge_log(log_path.read_text(encoding="utf-8")) if log_path.exists() else {}
    skipped = parse_skipped(skip_path.read_text(encoding="utf-8")) if skip_path.exists() else {}
    implemented = implemented_ids(impl_dir)
    records = []
    for item_id in selected:
        item = items.get(item_id)
        if not item or not item.get("finding_id"):
            continue
        entry = log.get(item_id, {})
        closed = item_id in skipped or VERDICT_BY_RESOLUTION.get(entry.get("resolution", "")) == "rejected"
        sha = "" if closed else commit_for(item_id, cwd, base_sha)
        verdict, why = _verdict(entry, item_id in implemented, sha)
        if item_id in skipped:
            verdict, why = "skipped", skipped[item_id]
        finding = by_finding.get(str(item["finding_id"]), {})
        records.append(
            {
                "finding_id": item["finding_id"],
                "title": finding.get("title") or item.get("summary", ""),
                "section": finding.get("section", ""),
                "file": finding.get("file") or item.get("file", ""),
                "line": finding.get("line") or item.get("line", ""),
                "item_id": item_id,
                "shared_with_github": item.get("location") != "report",
                "verdict": verdict,
                "sha": sha,
                "why": why,
                "run": run,
            }
        )
    return records


def main(argv: list[str] | None = None) -> int:
    """Append this resolve run's per-finding outcomes to the review directory's resolution ledger."""
    parser = argparse.ArgumentParser(description="Append oss:resolve outcomes to the review's resolution.jsonl.")
    parser.add_argument("--impl-dir", required=True, help="resolve $IMPL_DIR holding the run's records")
    parser.add_argument("--review-dir", required=True, help="review report directory (.reports/review/pr-N/run-NNN)")
    parser.add_argument("--repo", default=".", help="git checkout used to look up [resolve No.<id>] commits")
    parser.add_argument("--base-sha", default="", help="head when this resolve run started; scopes the commit lookup")
    args = parser.parse_args(argv)
    impl_dir, review_dir = Path(args.impl_dir), Path(args.review_dir)
    if not review_dir.is_dir():
        print(f"! BLOCKED — review directory {review_dir.as_posix()} not found", file=sys.stderr)
        return 1
    try:
        candidates = (review_dir / "findings.jsonl", impl_dir / "report-findings.jsonl")
        findings = next((p for p in candidates if p.is_file() and p.stat().st_size), None)
        records = build_records(impl_dir, impl_dir.name, Path(args.repo), findings=findings, base_sha=args.base_sha)
    except (OSError, ValueError, KeyError) as exc:
        print(f"! BLOCKED — cannot read resolve records: {exc}", file=sys.stderr)
        return 1
    ledger = review_dir / "resolution.jsonl"
    done = set()
    if ledger.exists():
        for line in ledger.read_text(encoding="utf-8").splitlines():
            if line.strip():
                prior = json.loads(line)
                done.add((prior.get("run"), prior.get("finding_id")))
    fresh = [rec for rec in records if (rec["run"], rec["finding_id"]) not in done]
    if fresh:
        with ledger.open("a", encoding="utf-8", newline="\n") as handle:
            handle.writelines(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n" for rec in fresh)
    print(f"resolution.jsonl: +{len(fresh)} record(s) → {ledger.as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

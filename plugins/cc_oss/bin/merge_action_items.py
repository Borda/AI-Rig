#!/usr/bin/env python
"""Merge oss:review findings into oss:resolve's action-item list without renumbering existing items.

``oss:resolve`` keeps one numbered item list, ``$IMPL_DIR/action-items.jsonl``. GitHub items from the PR thread are
written first (ids ``1..G``). This script folds the review's ``findings.jsonl`` (stable finding ids, see
``mint_finding_ids.py``) into that list deterministically, so the orchestrator never retypes or renumbers records:

- A finding at the same file and line as a pending GitHub item is the same target. The GitHub item keeps its id and
  inherits the review's detail: ``finding_id``, the full finding text and the reviewer's evidence paths. GitHub
  comments are often terse, so this is where the local detail matters most. The independent verifier's verdict and
  the ``codex_eligible`` tag are never inherited: both describe the review's claim, not whatever the GitHub comment
  asks for on that line, so the challenge phase still tests the GitHub item from scratch.
- A finding the orchestrator judged to be the same target at a different line (``--links``) gets the same detail.
- A GitHub item absorbs at most one finding. A second finding on the same line, a link to an item that already
  absorbed one, a link to a closed item, or a second link for one finding is not folded; that finding is appended as
  its own item instead, so no finding is lost and no item mixes two findings' evidence.
- Every other finding is appended as a ``[report]`` item with the next free id (``G+1``, ``G+2``, ...). It keeps the
  verifier's ``verify_verdict`` only when the value is exactly ``CONFIRMED``, names a ``verify_file``, and came from
  the review's own ``findings.jsonl``. Findings that resolve parsed itself from an older report (a findings file in the
  same directory as the item list) never carry a verdict.
- A pending GitHub item that matched nothing and has only a short comment is marked ``thin: true`` so the challenge
  phase reassesses it from scratch. Open questions are never thin.
- Findings whose id already appears in the item list are skipped, so re-running the merge changes nothing.

When no GitHub row needs an annotation the file is only appended to; otherwise it is replaced in one atomic rename.
Ids already in the file never change, so table numbers, task ids and commit tags stay aligned.

``--candidates`` prints same-file finding/item pairs that did not match exactly, for the orchestrator's semantic-match
judgment, and writes nothing.

``--recheck-verdicts`` runs right before the challenge phase. It drops every ``verify_verdict`` whose evidence no
longer holds: the review's recorded head (``head-sha.txt`` in ``--review-dir``) differs from ``--current-head``, either
is unknown, or the ``verify_file`` is gone from disk. Those items then get the full existence check again.

consumers: plugins/cc_oss/skills/resolve/modes/report-intelligence.md (Step 3a), plugins/cc_oss/skills/resolve/SKILL.md
(Step 3c), plugins/cc_oss/skills/resolve/modes/action-item-dispatch.md (Phase 1 verdict re-check)

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/merge_action_items.py" --items "$IMPL_DIR/action-items.jsonl" \\
        --findings "$FINDINGS" [--links "$IMPL_DIR/report-links.jsonl"] [--candidates]
    python "${CLAUDE_PLUGIN_ROOT}/bin/merge_action_items.py" --items "$IMPL_DIR/action-items.jsonl" \\
        --recheck-verdicts --review-dir "$(dirname "$REPORT_FILE")" --current-head "$PR_HEAD_OID"

Exit codes:
    0 — merged, candidates printed, or verdicts re-checked
    1 — unreadable input, a finding missing a required field, a link naming an unknown id, or a duplicate item id
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Final

# Sibling import: both scripts ship in the same bin/ directory, which is not a package. Putting it on the path
# keeps one normalizer for minting and matching, whether run as a script or loaded by the test conftest.
_BIN_DIR = str(Path(__file__).resolve().parent)
if _BIN_DIR not in sys.path:
    sys.path.insert(0, _BIN_DIR)

from mint_finding_ids import normalize_path, read_jsonl, section_slug  # noqa: E402

#: Numeric resolve severity per review severity word.
SEVERITY_SCORE: Final = {"critical": 5, "high": 4, "medium": 3, "low": 2, "cosmetic": 1}
#: Sections whose MEDIUM findings are ``[req]`` (code-related) per review-section-taxonomy.md "Severity → Resolve Type".
REQ_MEDIUM_SECTIONS: Final = frozenset({"critical", "architecture-quality", "performance-concerns", "api-design"})
FINDING_FIELDS: Final = ("id", "section", "severity", "title", "change", "author")
PENDING_TYPE_MARKERS: Final = ("[req]", "[suggest]", "[question]")
#: Item ``status`` values that keep an item open; a missing status counts as open.
OPEN_STATUSES: Final = ("", "pending")
CONFIRMED: Final = "CONFIRMED"
THIN_TEXT_CHARS: Final = 80
SUMMARY_CHARS: Final = 60


def resolve_type(finding: dict) -> str:
    """Return the resolve ``type`` for a review finding, following the taxonomy's severity table.

    >>> resolve_type({"severity": "medium", "section": "### Performance Concerns"})
    '[report][req]'
    >>> resolve_type({"severity": "medium", "section": "### Codex Co-Review"})
    '[report][suggest]'
    """
    severity = finding["severity"]
    if severity in ("critical", "high"):
        return "[report][req]"
    if severity == "medium" and section_slug(finding["section"]) in REQ_MEDIUM_SECTIONS:
        return "[report][req]"
    return "[report][suggest]"


def short_summary(text: str, limit: int = SUMMARY_CHARS) -> str:
    """Truncate ``text`` at a word boundary with an ellipsis.

    >>> short_summary("rename param x to count", 60)
    'rename param x to count'
    >>> short_summary("one two three four", 9)
    'one two…'
    """
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0]
    return f"{cut}…"


def _line_key(value: object) -> str:
    """Compare lines as plain integers so ``12`` and ``"12"`` match and blanks never do."""
    text = str(value).strip() if value is not None else ""
    return text if text.isdigit() else ""


def _is_pending(item: dict) -> bool:
    """Only open GitHub items can absorb a finding; resolved, addressed, and ``[info]`` rows are history.

    The ``status`` field closes an item whatever its type: a resolved GitHub thread keeps its
    ``[gh][req]``/``[gh][suggest]`` type and carries ``status: resolved`` instead. Older items files marked closed items
    with ``[done]`` or ``[info]``; those markers close an item even when pending markers remain in its type.
    """
    if str(item.get("status") or "").strip() not in OPEN_STATUSES:
        return False
    item_type = str(item.get("type", ""))
    if "[done]" in item_type or "[info]" in item_type:
        return False
    return any(marker in item_type for marker in PENDING_TYPE_MARKERS)


def _location(record: dict) -> tuple[str, str]:
    """Return the normalized ``(file, line)`` pair used for exact matching."""
    return normalize_path(str(record.get("file") or "")), _line_key(record.get("line"))


def _by_severity(findings: list[dict]) -> list[dict]:
    """Order findings most severe first, keeping report order within a severity, so the worst one claims an item."""
    return sorted(findings, key=lambda f: -SEVERITY_SCORE.get(f["severity"], 0))


def exact_matches(items: list[dict], findings: list[dict]) -> dict[str, int]:
    """Map finding id → item id for pairs at the same file and line; each free pending item takes one finding."""
    index: dict[tuple[str, str], int] = {}
    for item in items:
        key = _location(item)
        if key[0] and key[1] and _is_pending(item) and not item.get("finding_id") and key not in index:
            index[key] = int(item["id"])
    matches: dict[str, int] = {}
    for finding in _by_severity(findings):
        item_id = index.pop(_location(finding), None)
        if item_id is not None:
            matches[finding["id"]] = item_id
    return matches


def candidates(items: list[dict], findings: list[dict], exact: dict[str, int]) -> list[dict]:
    """List same-file pairs that did not match exactly, for the orchestrator's semantic judgment."""
    taken = set(exact.values())
    out = []
    for finding in findings:
        if finding["id"] in exact:
            continue
        path = normalize_path(str(finding.get("file") or ""))
        for item in items:
            free = _is_pending(item) and not item.get("finding_id") and int(item["id"]) not in taken
            if path and free and normalize_path(str(item.get("file") or "")) == path:
                out.append(
                    {
                        "finding_id": finding["id"],
                        "finding_title": finding["title"],
                        "finding_line": finding.get("line"),
                        "item_id": item["id"],
                        "item_summary": item.get("summary", ""),
                        "item_line": item.get("line"),
                    }
                )
    return out


def accepted_links(links: list[dict], by_id: dict[int, dict], known: set[str], exact: dict[str, int]) -> dict[str, int]:
    """Keep each semantic link whose finding and item are both still free; the rest fall back to appending."""
    accepted: dict[str, int] = {}
    taken = set(exact.values())
    seen_findings: set[str] = set()
    for link in links:
        finding_id, item_id = link["finding_id"], int(link["item_id"])
        if finding_id not in known or item_id not in by_id:
            raise ValueError(f"link names an unknown finding or item: {link}")
        repeated = finding_id in seen_findings
        seen_findings.add(finding_id)
        item = by_id[item_id]
        if repeated:
            taken.discard(accepted.pop(finding_id, 0))
            continue
        if finding_id in exact or item_id in taken or not _is_pending(item) or item.get("finding_id"):
            continue
        accepted[finding_id] = item_id
        taken.add(item_id)
    return accepted


def annotate(item: dict, finding: dict) -> None:
    """Fold one review finding's detail into the GitHub item that targets the same code; never its verdict."""
    owner = str(finding["author"])
    item["finding_id"] = finding["id"]
    if owner and owner not in str(item.get("author", "")):
        item["author"] = f"{item.get('author', '')} + {owner}".strip(" +")
        item["summary"] = f"{item.get('summary', '')} (also flagged by /review — {owner})"
    detail = str(finding.get("full_text") or finding["title"])
    item["full_comment_text"] = (
        f"{item.get('full_comment_text', '')}\n\n[review finding {finding['id']}] {detail}".strip()
    )
    item["severity"] = max(int(item.get("severity") or 0), SEVERITY_SCORE[finding["severity"]])
    for key in ("source_file", "verify_file"):
        if finding.get(key):
            item[key] = finding[key]
    item.pop("thin", None)


def as_report_item(finding: dict, item_id: int, *, keep_verdict: bool) -> dict:
    """Build a new ``[report]`` action item from a review finding that no GitHub item covers."""
    item_type = resolve_type(finding)
    severity = SEVERITY_SCORE[finding["severity"]]
    if item_type.endswith("[req]"):
        severity = max(severity, 3)
    item = {
        "id": item_id,
        "type": item_type,
        "change": finding["change"],
        "severity": severity,
        "author": finding["author"],
        "summary": short_summary(str(finding["title"])),
        "file": finding.get("file") or "",
        "line": finding.get("line") if _line_key(finding.get("line")) else "",
        "url": "",
        "full_comment_text": str(finding.get("full_text") or finding["title"]),
        "location": "report",
        "origin": "posted",
        "finding_id": finding["id"],
    }
    for key in ("source_file", "verify_file", "required_change", "codex_eligible"):
        if finding.get(key):
            item[key] = finding[key]
    if keep_verdict and finding.get("verify_verdict") == CONFIRMED and finding.get("verify_file"):
        item["verify_verdict"] = CONFIRMED
    return item


def mark_thin(items: list[dict]) -> int:
    """Flag pending GitHub items with a short comment and no review match; return the count."""
    count = 0
    for item in items:
        skip = item.get("location") == "report" or not _is_pending(item) or item.get("finding_id")
        if skip or "[question]" in str(item.get("type", "")) or item.get("thin"):
            continue
        if len(str(item.get("full_comment_text") or "").strip()) < THIN_TEXT_CHARS:
            item["thin"] = True
            count += 1
    return count


def _validate(items: list[dict], findings: list[dict]) -> str | None:
    """Return the first blocking input problem, or ``None``."""
    ids = [item.get("id") for item in items]
    if any(not isinstance(i, int) or i < 1 for i in ids):
        return "action-items.jsonl holds a non-positive or non-integer id"
    if len(ids) != len(set(ids)):
        return "action-items.jsonl holds a duplicate id"
    for n, finding in enumerate(findings, 1):
        missing = [name for name in FINDING_FIELDS if not str(finding.get(name) or "").strip()]
        if missing:
            return f"finding {n} missing {', '.join(missing)} — run mint_finding_ids.py and complete the record"
        if finding["severity"] not in SEVERITY_SCORE:
            return f"finding {finding['id']} has unknown severity {finding['severity']!r}"
    return None


def _dump(record: dict) -> str:
    """Serialize one record as a compact JSONL line."""
    return json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"


def _write(path: Path, items: list[dict], appended: list[dict], rewrite: bool) -> None:
    """Append new rows, or replace the file atomically when existing rows were annotated."""
    if not rewrite:
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.writelines(_dump(rec) for rec in appended)
        return
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.writelines(_dump(rec) for rec in [*items, *appended])
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def merge(
    items: list[dict], findings: list[dict], links: list[dict], *, keep_verdicts: bool = True
) -> tuple[list[dict], dict]:
    """Annotate matched GitHub items in place and return ``(appended report items, summary counts)``."""
    by_id = {int(item["id"]): item for item in items}
    merged_before = {str(item["finding_id"]) for item in items if item.get("finding_id")}
    known = {finding["id"] for finding in findings}
    fresh = [finding for finding in findings if finding["id"] not in merged_before]
    by_finding = {finding["id"]: finding for finding in fresh}
    exact = exact_matches(items, fresh)
    links = [link for link in links if link["finding_id"] not in merged_before]
    semantic = accepted_links(links, by_id, known, exact)
    for finding_id, item_id in [*exact.items(), *semantic.items()]:
        annotate(by_id[item_id], by_finding[finding_id])
    next_id = max(by_id, default=0) + 1
    appended = []
    for finding in fresh:
        if finding["id"] not in exact and finding["id"] not in semantic:
            appended.append(as_report_item(finding, next_id, keep_verdict=keep_verdicts))
            next_id += 1
    thin = mark_thin(items)
    summary = {
        "findings": len(findings),
        "already_merged": len(findings) - len(fresh),
        "deduped_exact": len(exact),
        "deduped_semantic": len(semantic),
        "links_ignored": len({link["finding_id"] for link in links}) - len(semantic),
        "appended": len(appended),
        "thin": thin,
        "rewrite": bool(exact or semantic or thin),
    }
    return appended, summary


def _git_head(cwd: Path) -> str:
    """Return the checkout's current commit, or ``""`` when git cannot tell."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True, check=False, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _same_commit(left: str, right: str) -> bool:
    """Treat a short and a full hash of the same commit as equal; an unknown side never matches.

    >>> _same_commit("abc1234", "abc1234def"), _same_commit("", "abc1234"), _same_commit("abc1234", "abf1234")
    (True, False, False)
    """
    left, right = left.strip().lower(), right.strip().lower()
    if min(len(left), len(right)) < 7:
        return False
    return left.startswith(right) or right.startswith(left)


def recheck_verdicts(items: list[dict], review_head: str, current_head: str) -> int:
    """Drop verdicts that a moved head or a missing verifier file no longer supports; return how many were dropped."""
    fresh_head = _same_commit(review_head, current_head)
    dropped = 0
    for item in items:
        if not item.get("verify_verdict"):
            continue
        verify_file = str(item.get("verify_file") or "")
        if fresh_head and item["verify_verdict"] == CONFIRMED and verify_file and Path(verify_file).is_file():
            continue
        item.pop("verify_verdict")
        dropped += 1
    return dropped


def _run_recheck(items_path: Path, review_dir: str, current_head: str) -> int:
    """Apply ``recheck_verdicts`` to the item file and print what changed."""
    items = read_jsonl(items_path) if items_path.exists() else []
    head_file = Path(review_dir) / "head-sha.txt" if review_dir else None
    review_head = head_file.read_text(encoding="utf-8").strip() if head_file and head_file.is_file() else ""
    current_head = current_head or _git_head(Path.cwd())
    dropped = recheck_verdicts(items, review_head, current_head)
    if dropped:
        _write(items_path, items, [], rewrite=True)
    kept = sum(1 for item in items if item.get("verify_verdict"))
    same = _same_commit(review_head, current_head)
    print(json.dumps({"confirmed_kept": kept, "confirmed_dropped": dropped, "head_matches_review": same}))
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse the command line for the merge, candidates and re-check modes."""
    parser = argparse.ArgumentParser(description="Fold oss:review findings into oss:resolve action-items.jsonl.")
    parser.add_argument("--items", required=True, help="$IMPL_DIR/action-items.jsonl (created when absent)")
    parser.add_argument("--findings", help="review findings.jsonl with minted ids")
    parser.add_argument("--links", help="orchestrator-judged semantic matches, one {finding_id, item_id} per line")
    parser.add_argument("--candidates", action="store_true", help="print same-file non-exact pairs; write nothing")
    parser.add_argument("--recheck-verdicts", action="store_true", help="drop verdicts a moved head no longer backs")
    parser.add_argument("--review-dir", default="", help="review report directory holding head-sha.txt")
    parser.add_argument("--current-head", default="", help="PR head now; empty means the checkout's HEAD")
    args = parser.parse_args(argv)
    if not args.recheck_verdicts and not args.findings:
        parser.error("--findings is required unless --recheck-verdicts is given")
    return args


def main(argv: list[str] | None = None) -> int:
    """Merge review findings into the resolve item list, print match candidates, or re-check verdicts."""
    args = _parse_args(argv)
    items_path = Path(args.items)
    try:
        if args.recheck_verdicts:
            return _run_recheck(items_path, args.review_dir, args.current_head)
        items = read_jsonl(items_path) if items_path.exists() else []
        findings_path = Path(args.findings)
        findings = read_jsonl(findings_path)
        links = read_jsonl(Path(args.links)) if args.links and Path(args.links).exists() else []
    except (OSError, ValueError) as exc:
        print(f"! BLOCKED — cannot read merge inputs: {exc}", file=sys.stderr)
        return 1
    for finding in findings:
        finding["severity"] = str(finding.get("severity") or "").strip().lower()
    problem = _validate(items, findings)
    if problem:
        print(f"! BLOCKED — {problem}", file=sys.stderr)
        return 1
    if args.candidates:
        print(json.dumps(candidates(items, findings, exact_matches(items, findings)), ensure_ascii=False))
        return 0
    # A findings file beside the item list was parsed by resolve itself from an older report; no verifier backs it.
    keep_verdicts = findings_path.resolve().parent != items_path.resolve().parent
    try:
        appended, summary = merge(items, findings, links, keep_verdicts=keep_verdicts)
    except (KeyError, ValueError) as exc:
        print(f"! BLOCKED — {exc}", file=sys.stderr)
        return 1
    items_path.parent.mkdir(parents=True, exist_ok=True)
    _write(items_path, items, appended, summary["rewrite"])
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())

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

Version-one findings are merged only from a publication the strict intake gate accepts. ``--accept-degraded`` also
accepts one whose every gap is degraded (``find_review_report.py --assess`` finds no critical gap): its confirmed rows
are intact, only a readiness label, a reviewer's coverage or open-concern bookkeeping is wrong. ``oss:resolve`` passes
the flag only after the user chose to proceed with that exact report at its gap triage. The verdict re-check never
takes the flag, so parent candidate proof from a degraded publication is never reused as a shortcut.

``--recheck-verdicts`` runs right before the challenge phase. It drops every ``verify_verdict`` whose evidence no
longer holds: the review's recorded head (``head-sha.txt`` in ``--review-dir``) differs from ``--current-head``, either
is unknown, or the ``verify_file`` is gone from disk. Native Windows resolves serialized Git Bash evidence paths
with ``cygpath`` before checking the file, preserving the recorded pointer. A failed conversion blocks the recheck
without rewriting the item ledger. Unsupported evidence then gets the full existence check again.
Parent candidate proof is carried separately with a derived
``parent_validation_file`` and its SHA-256. Re-checking disables its shortcut when the publication, source head,
candidate mapping or evidence bytes change, retaining the original pointer for bounded freshness investigation.
A matched GitHub item receives evidence scoped only to its linked review finding, never to distinct requests.

consumers: plugins/cc_oss/skills/resolve/modes/report-intelligence.md (Step 3a), plugins/cc_oss/skills/resolve/SKILL.md
(Step 3c), plugins/cc_oss/skills/resolve/modes/action-item-dispatch.md (Phase 1 verdict re-check)

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/merge_action_items.py" --items "$IMPL_DIR/action-items.jsonl" \\
        --findings "$FINDINGS" [--links "$IMPL_DIR/report-links.jsonl"] [--candidates] [--accept-degraded]
    python "${CLAUDE_PLUGIN_ROOT}/bin/merge_action_items.py" --items "$IMPL_DIR/action-items.jsonl" \\
        --recheck-verdicts --review-dir "$(dirname "$REPORT_FILE")" --current-head "$PR_HEAD_OID"

Exit codes:
    0 — merged, candidates printed, or verdicts re-checked
    1 — unreadable input, a finding missing a required field or carrying an unknown severity, two findings sharing an
        id, a row or link that is not an object of the expected field types (text fields, a numeric line), a link
        naming an unknown id, a duplicate item id, version-one findings whose publication is neither valid nor (with
        ``--accept-degraded``) free of critical gaps, or (with ``--recheck-verdicts``) a validated publication holding
        findings rows the merge could not use
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Final

#: Sibling import: both scripts ship in the same bin/ directory, which is not a package. Putting it on the path
#: keeps one normalizer for minting and matching, whether run as a script or loaded by the test conftest.
_BIN_DIR = str(Path(__file__).resolve().parent)
if _BIN_DIR not in sys.path:
    sys.path.insert(0, _BIN_DIR)

#: ``FINDING_FIELDS`` (the fields every finding record must provide to be merged into an action item), the severity
#: vocabulary ``FINDING_SEVERITIES`` and the row-set check ``findings_problem`` live in ``find_review_report``, so
#: ``oss:review``'s producer gate refuses exactly the rows this merge would refuse. ``FINDING_FIELDS`` stays importable
#: from this module, where it used to be defined.
from find_review_report import (  # noqa: E402
    FINDING_FIELDS,  # noqa: F401 - re-exported: the merge's required-field list, now owned by find_review_report
    FINDING_SEVERITIES,
    _parse_header_fields,
    assess_report,
    finding_row_problem,
    findings_problem,
    gate_line,
)
from mint_finding_ids import normalize_path, read_jsonl, section_slug  # noqa: E402

#: Numeric resolve severity per review severity word. Keyed by the shared vocabulary (a strict zip fails at import if
#: its length changes), so every word the producer gate accepts has a score here.
SEVERITY_SCORE: Final = dict(zip(FINDING_SEVERITIES, (5, 4, 3, 2, 1), strict=True))
#: Sections whose MEDIUM findings are ``[req]`` (code-related) per review-section-taxonomy.md "Severity → Resolve Type".
REQ_MEDIUM_SECTIONS: Final = frozenset({"critical", "architecture-quality", "performance-concerns", "api-design"})
#: Item-type markers that denote an unresolved review comment still needing action.
PENDING_TYPE_MARKERS: Final = ("[req]", "[suggest]", "[question]")
#: Item ``status`` values that keep an item open; a missing status counts as open.
OPEN_STATUSES: Final = ("", "pending")
#: Verifier verdict value under which a finding's ``verify_verdict`` and ``verify_file`` are carried onto its item.
CONFIRMED: Final = "CONFIRMED"
#: Minimum length in characters of an item's full comment text before it counts as substantive.
THIN_TEXT_CHARS: Final = 80
#: Default maximum length in characters of a finding's short summary.
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
    for key in (
        "source_file",
        "verify_file",
        "candidate_id",
        "contract_version",
        "validation_status",
        "parent_validation_file",
        "parent_validation_sha256",
    ):
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
    for key in (
        "source_file",
        "verify_file",
        "required_change",
        "codex_eligible",
        "candidate_id",
        "contract_version",
        "validation_status",
        "parent_validation_file",
        "parent_validation_sha256",
    ):
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


def _links_problem(links: list) -> str | None:
    """Name the first semantic link that is not a ``{finding_id: text, item_id: number}`` object, or ``None``.

    >>> _links_problem([{"finding_id": "a", "item_id": 2}, {"finding_id": ["a"], "item_id": 2}])
    'link 2 is not a {finding_id: text, item_id: number} object'
    """
    for n, link in enumerate(links, 1):
        if not (
            isinstance(link, dict)
            and isinstance(link.get("finding_id"), str)
            and (
                type(link.get("item_id")) is int or (isinstance(link.get("item_id"), str) and link["item_id"].isdigit())
            )
        ):
            return f"link {n} is not a {{finding_id: text, item_id: number}} object"
    return None


def _finding_problem(finding: object, n: int) -> str | None:
    """Return why one finding row cannot become an action item, or ``None``.

    The field, type and severity rules are ``find_review_report.finding_row_problem``, shared with ``oss:review``'s
    producer gate; the contract and validation-status rules below are the merge's own.
    """
    row_problem = finding_row_problem(finding, n)
    if row_problem:
        return row_problem
    if "contract_version" in finding and (
        finding["contract_version"] != 1 or finding.get("validation_status") != "CONFIRMED"
    ):
        return f"finding {finding['id']} is not confirmed under a supported review contract — upgrade or revalidate the review"
    if finding.get("validation_status", "CONFIRMED") != "CONFIRMED":
        return f"finding {finding['id']} is an open concern, not an actionable finding"
    return None


def _validate(items: list[dict], findings: list[dict]) -> str | None:
    """Return the first blocking input problem, or ``None``.

    Two findings sharing an id block too: items are keyed by finding id, so the later row would be folded into the
    earlier row's item and its confirmed finding silently lost.
    """
    if not all(isinstance(item, dict) for item in items):
        return "action-items.jsonl holds a line that is not a JSON object"
    ids = [item.get("id") for item in items]
    if any(not isinstance(i, int) or i < 1 for i in ids):
        return "action-items.jsonl holds a non-positive or non-integer id"
    if len(ids) != len(set(ids)):
        return "action-items.jsonl holds a duplicate id"
    return findings_problem(findings, _finding_problem)


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
            ["git", "rev-parse", "HEAD"],  # noqa: S607 - git resolved via PATH on purpose
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
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
        if fresh_head and item["verify_verdict"] == CONFIRMED and verify_file:
            verify_path = Path(verify_file)
            # Git Bash converts argv paths for native Python, but paths stored inside JSON bypass that conversion.
            if (
                sys.platform == "win32"
                and PurePosixPath(verify_file).is_absolute()
                and not PureWindowsPath(verify_file).is_absolute()
            ):
                converted = subprocess.run(  # noqa: S603 - argv list, no shell evaluation of the evidence pointer
                    ["cygpath", "-w", verify_file],  # noqa: S607 - Git Bash's converter is resolved via PATH
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=5,
                )
                verify_path = Path(converted.stdout.strip())
            if verify_path.is_file():
                continue
        item.pop("verify_verdict")
        dropped += 1
    return dropped


def _run_recheck(items_path: Path, review_dir: str, current_head: str) -> int:
    """Apply ``recheck_verdicts`` to the item file and print what changed.

    A findings row of the validated publication that the merge could not use (no ``id``, a wrong JSON type, an unknown
    severity, an id another row repeats) blocks the re-check before anything is written: its parent proof cannot be
    matched to an item, and guessing would either keep or drop a shortcut on no evidence.

    Returns:
        0 after the re-check, or 1 when the publication's findings rows are unusable (reason on stderr).
    """
    items = read_jsonl(items_path) if items_path.exists() else []
    head_file = Path(review_dir) / "head-sha.txt" if review_dir else None
    review_head = head_file.read_text(encoding="utf-8").strip() if head_file and head_file.is_file() else ""
    current_head = current_head or _git_head(Path.cwd())
    dropped = recheck_verdicts(items, review_head, current_head)
    parent_dropped = 0
    validation_file = Path(review_dir) / "review-validation.json"
    report = Path(review_dir) / "review-report.md"
    fields = _parse_header_fields(report.read_text(encoding="utf-8").splitlines()) if report.is_file() else None
    publication_valid = (
        _same_commit(review_head, current_head)
        and fields is not None
        and fields.get("Review contract") == "1"
        and bool(gate_line(report))
    )
    digest = hashlib.sha256(validation_file.read_bytes()).hexdigest() if publication_valid else ""
    findings = read_jsonl(Path(review_dir) / "findings.jsonl") if publication_valid else []
    row_problem = findings_problem(findings)
    if row_problem:
        print(f"! BLOCKED — {row_problem}", file=sys.stderr)
        return 1
    candidate_by_finding = {finding["id"]: finding.get("candidate_id") for finding in findings}
    for item in items:
        if not item.get("parent_validation_file") or not item.get("validation_status"):
            continue
        if (
            publication_valid
            and item["parent_validation_file"] == validation_file.resolve().as_posix()
            and item.get("parent_validation_sha256") == digest
            and item["validation_status"] == CONFIRMED
            and item.get("candidate_id") == candidate_by_finding.get(item.get("finding_id"))
        ):
            continue
        # Preserve the original evidence pointer for diagnosis, while disabling automatic proof reuse.
        item.pop("validation_status")
        parent_dropped += 1
    if dropped or parent_dropped:
        _write(items_path, items, [], rewrite=True)
    kept = sum(1 for item in items if item.get("verify_verdict"))
    same = _same_commit(review_head, current_head)
    print(
        json.dumps(
            {
                "confirmed_kept": kept,
                "confirmed_dropped": dropped,
                "head_matches_review": same,
                "parent_confirmed_dropped": parent_dropped,
            }
        )
    )
    return 0


def _publication_accepted(report: Path, accept_degraded: bool) -> bool:
    """Decide whether version-one findings may be merged from the publication beside them.

    A publication the strict intake gate accepts always qualifies. With ``accept_degraded`` one that fails that gate
    still qualifies when gap assessment finds no critical gap, so its confirmed findings rows are intact; the caller
    owns the decision that the user accepted exactly this degraded report.

    Args:
        report: The ``review-report.md`` beside the findings file.
        accept_degraded: Whether the caller's intake accepted a degraded publication.

    Returns:
        True when the findings may become action items.
    """
    if gate_line(report):
        return True
    return accept_degraded and not assess_report(report).critical


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
    parser.add_argument(
        "--accept-degraded",
        action="store_true",
        help="also merge from a publication with only degraded gaps (no critical one); pass only after the user "
        "accepted that exact report at resolve's gap triage; never applies to --recheck-verdicts",
    )
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
    except (OSError, ValueError, RecursionError, subprocess.SubprocessError) as exc:
        print(f"! BLOCKED — cannot read merge inputs: {exc}", file=sys.stderr)
        return 1
    for finding in findings:
        if isinstance(finding, dict) and isinstance(finding.get("severity"), str):
            finding["severity"] = finding["severity"].strip().lower()
    problem = _validate(items, findings) or _links_problem(links)
    if problem:
        print(f"! BLOCKED — {problem}", file=sys.stderr)
        return 1
    current_findings = [finding for finding in findings if "contract_version" in finding]
    if current_findings:
        report = findings_path.with_name("review-report.md")
        fields = _parse_header_fields(report.read_text(encoding="utf-8").splitlines()) if report.is_file() else None
        if (
            not fields
            or fields.get("Review contract") != "1"
            or not _publication_accepted(report, args.accept_degraded)
        ):
            print("! BLOCKED — current findings require their validated review publication", file=sys.stderr)
            return 1
        validation_file = findings_path.with_name("review-validation.json")
        digest = hashlib.sha256(validation_file.read_bytes()).hexdigest()
        for finding in current_findings:
            finding["parent_validation_file"] = validation_file.resolve().as_posix()
            finding["parent_validation_sha256"] = digest
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

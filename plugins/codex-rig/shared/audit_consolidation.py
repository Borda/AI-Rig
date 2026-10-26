"""Validate advisory instruction-consolidation evidence without modifying its sources.

## Purpose

Bind proposed instruction edits and independent review to the exact source set and derive conservative advisory
outcomes. Structural checks prevent malformed ranges, overlapping ownership and stale evidence from being reported as
reviewed proposals; they do not establish semantic equivalence or measured optimization benefit.

## Scope

Read a schema-one report containing source identities, section inventory, coverage and candidates. Sources include
citing documents. The workflow owner supplies loading evidence and protected heading lines; the independent reviewer
checks their completeness, actual activation, rendered structure and every preserved obligation.

## Usage

Run the packaged launcher with this file and --report <run-local JSON>. The JSON stdout contains settlement and review
inputs. The shared artifact validator calls settle_consolidation on the same digest-bound report.

## Outputs

Return deterministic outcomes and fresh review inputs, including each input's SHA-256 digest. No files are written, no
edits applied, no commands executed and no model or network requests made. Reviewed remains advisory.

## Failure

Malformed records, invalid source paths, changed sources and inconsistent coverage raise ValueError; the CLI exits
nonzero with a concise diagnostic. Unsupported structure or missing/stale review remains unvalidated.

## Used by

The audit skill's consolidation branch, shared artifact validation and focused integration regressions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


def _records(value: object, label: str) -> list[dict[str, Any]]:
    """Require object lists before inspecting untrusted artifact fields."""
    if not isinstance(value, list) or any(not isinstance(entry, dict) for entry in value):
        raise ValueError(f"consolidation-invalid-{label}")
    return value


def _text(value: object) -> bool:
    """Recognize populated evidence strings without coercing arbitrary JSON values."""
    return isinstance(value, str) and bool(value.strip())


def _strings(value: object) -> bool:
    """Recognize a list of populated evidence strings, including an empty list."""
    return isinstance(value, list) and all(_text(entry) for entry in value)


def _indexed(value: object, label: str) -> dict[str, dict[str, Any]]:
    """Index records while refusing duplicate or missing identities."""
    result = {}
    for entry in _records(value, label):
        identity = entry.get("id")
        if not _text(identity) or identity in result:
            raise ValueError(f"consolidation-duplicate-or-missing-{label}-id")
        result[identity] = entry
    return result


def _inventory(report: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Verify current source bytes and disjoint section inventory before settlement."""
    sources = _indexed(report.get("sources"), "sources")
    texts, paths = {}, set()
    for identity, source in sources.items():
        if set(source) != {"id", "path", "sha256"} or not _text(source.get("path")) or not _text(source.get("sha256")):
            raise ValueError("consolidation-invalid-source")
        path = Path(source["path"])
        if not path.is_absolute() or path.resolve() != path or not path.is_file() or path in paths:
            raise ValueError("consolidation-invalid-source-path")
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
            raise ValueError("consolidation-linked-source")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != source["sha256"]:
            raise ValueError("consolidation-source-drift")
        texts[identity] = content.decode("utf-8")
        paths.add(path)
    items = _indexed(report.get("items"), "items")
    ranges: dict[str, list[tuple[int, int]]] = {}
    for item in items.values():
        if set(item) != {
            "id",
            "source",
            "start_line",
            "end_line",
            "load",
            "group",
            "load_evidence",
            "blocked",
            "protected_headings",
            "syntax_supported",
        }:
            raise ValueError("consolidation-invalid-item-fields")
        source, start, end = item.get("source"), item.get("start_line"), item.get("end_line")
        if not isinstance(source, str) or source not in sources:
            raise ValueError("consolidation-unknown-source")
        if type(start) is not int or type(end) is not int or not 1 <= start <= end <= len(texts[source].splitlines()):
            raise ValueError("consolidation-invalid-range")
        if any(start <= other_end and end >= other_start for other_start, other_end in ranges.get(source, [])):
            raise ValueError("consolidation-overlapping-items")
        ranges.setdefault(source, []).append((start, end))
        if (
            item.get("load") not in ("always", "scoped", "lazy", "unknown")
            or not _text(item.get("group"))
            or not _text(item.get("load_evidence"))
            or not _strings(item.get("blocked"))
            or not _strings(item.get("protected_headings"))
            or type(item.get("syntax_supported")) is not bool
        ):
            raise ValueError("consolidation-invalid-load-evidence")
        lines = texts[source].splitlines()
        if any(heading not in lines for heading in item["protected_headings"]):
            raise ValueError("consolidation-heading-not-in-source")
    return items, texts


def _candidate(
    candidate: dict[str, Any], report: dict[str, Any], items: dict[str, dict[str, Any]], texts: dict[str, str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Derive one advisory decision from typed edits, obligation evidence and bound review."""
    members = candidate.get("members")
    decision = candidate.get("decision")
    if (
        set(candidate) != {"id", "members", "decision", "reason", "edits", "obligations", "review"}
        or not _strings(members)
        or len(members) < 2
        or len(set(members)) != len(members)
        or any(member not in items for member in members)
        or decision not in ("keep", "sharpen", "merge-prune")
        or not _text(candidate.get("reason"))
    ):
        raise ValueError("consolidation-invalid-candidate")
    selected = [items[member] for member in members]
    if len({item["source"] for item in selected}) < 2:
        raise ValueError("consolidation-single-file-cluster")
    edits = _records(candidate.get("edits"), "edits")
    obligations = _records(candidate.get("obligations"), "obligations")
    replacements = {}
    for edit in edits:
        member, replacement = edit.get("item"), edit.get("replacement")
        if not isinstance(member, str) or member not in members or member in replacements:
            raise ValueError("consolidation-invalid-edit-owner")
        if not isinstance(replacement, str) or set(edit) != {"item", "replacement"}:
            raise ValueError("consolidation-invalid-replacement")
        replacements[member] = replacement
    if decision == "keep" and (edits or obligations):
        raise ValueError("consolidation-keep-has-edits")
    if decision != "keep" and not edits:
        raise ValueError("consolidation-missing-edits")
    originals = {
        item["id"]: "\n".join(texts[item["source"]].splitlines()[item["start_line"] - 1 : item["end_line"]])
        for item in selected
    }
    final_text = originals | replacements
    mapped = set()
    for obligation in obligations:
        member, target = obligation.get("from"), obligation.get("to_item")
        if (
            set(obligation) != {"from", "original", "to_item", "to", "preserved"}
            or not isinstance(member, str)
            or member not in originals
            or not isinstance(target, str)
            or target not in final_text
            or not _text(obligation.get("original"))
            or obligation["original"] not in originals[member]
            or not _text(obligation.get("to"))
            or obligation["to"] not in final_text[target]
            or obligation.get("preserved") is not True
        ):
            raise ValueError("consolidation-invalid-obligation")
        if decision == "sharpen" and member != target:
            raise ValueError("consolidation-sharpen-moves-obligation")
        mapped.add(member)
    if decision != "keep" and mapped != set(members):
        raise ValueError("consolidation-incomplete-obligation-map")
    # Bind citing files too; changing a consumer invalidates every review that depended on its old text.
    review_input = {
        "sources": report["sources"],
        "items": selected,
        "id": candidate["id"],
        "decision": decision,
        "edits": edits,
        "obligations": obligations,
    }
    digest = hashlib.sha256(json.dumps(review_input, sort_keys=True, ensure_ascii=True).encode("utf-8")).hexdigest()
    review = candidate.get("review")
    if review is not None and (
        not isinstance(review, dict)
        or set(review) != {"digest", "verdict", "evidence"}
        or review.get("verdict") not in ("holds", "weakens", "loses")
        or not _text(review.get("digest"))
        or not _text(review.get("evidence"))
    ):
        raise ValueError("consolidation-invalid-review")
    reason = ""
    if any(item["blocked"] or not item["syntax_supported"] or item["load"] == "unknown" for item in selected):
        reason = "blocked or unsupported inventory"
    if decision == "merge-prune" and (
        len({item["group"] for item in selected}) != 1 or len({item["load"] for item in selected}) != 1
    ):
        reason = "different loading scopes"
    if decision == "merge-prune" and any(item["load"] not in ("always", "scoped") for item in selected):
        reason = "independently loaded content cannot merge"
    if decision == "merge-prune" and set(replacements) != set(members):
        raise ValueError("consolidation-merge-missing-member-edit")
    for source in {item["source"] for item in selected}:
        before = texts[source].splitlines()
        after = before[:]
        for item in sorted(selected, key=lambda entry: entry["start_line"], reverse=True):
            if item["source"] == source and item["id"] in replacements:
                after[item["start_line"] - 1 : item["end_line"]] = replacements[item["id"]].splitlines()
        protected = [
            heading for item in items.values() if item["source"] == source for heading in item["protected_headings"]
        ]
        if any(after.count(heading) < before.count(heading) for heading in protected):
            reason = "protected heading removed"
    # No Markdown parser is claimed. Edits touching container, fence, HTML or setext syntax need manual proposals.
    for member, replacement in replacements.items():
        for text in (originals[member], replacement):
            if re.search(r"(?m)^(?:[ \t]+|>|.*```|.*~~~|.*[<>]|(?:-+|=+)[ \t]*$)", text):
                reason = "unsupported structural edit"
    delta = sum(len(originals[member].encode()) - len(text.encode()) for member, text in replacements.items())
    if decision == "merge-prune" and delta <= 0:
        reason = "merge has no static byte reduction"
    status = "unvalidated"
    if decision == "keep":
        status, reason = "keep", candidate["reason"]
    elif not reason and review is not None and review["digest"] == digest:
        status = "reviewed" if review["verdict"] == "holds" else "rejected"
        reason = review["evidence"]
    elif not reason:
        reason = "missing or stale independent review"
    return (
        {"id": candidate["id"], "decision": decision, "status": status, "reason": reason, "byte_delta": delta},
        {"id": candidate["id"], "digest": digest, "input": review_input},
    )


def settle_consolidation(report: object) -> dict[str, Any]:
    """Validate an inventory and derive outcomes without accepting author-declared validation states."""
    if not isinstance(report, dict) or type(report.get("schema_version")) is not int or report["schema_version"] != 1:
        raise ValueError("consolidation-invalid-schema")
    if set(report) != {"schema_version", "sources", "items", "coverage", "candidates", "excluded"}:
        raise ValueError("consolidation-invalid-report-fields")
    items, texts = _inventory(report)
    candidates = _indexed(report.get("candidates"), "candidates")
    coverage = _indexed(report.get("coverage"), "coverage")
    if set(coverage) != set(items):
        raise ValueError("consolidation-incomplete-coverage")
    for entry in _records(report.get("excluded"), "excluded"):
        if set(entry) != {"path", "reason"} or not _text(entry.get("path")) or not _text(entry.get("reason")):
            raise ValueError("consolidation-invalid-exclusion")
    outcomes, inputs, claimed = [], [], set()
    for candidate in candidates.values():
        outcome, review_input = _candidate(candidate, report, items, texts)
        members = set(candidate["members"])
        if claimed & members:
            raise ValueError("consolidation-member-reused")
        claimed.update(members)
        outcomes.append(outcome)
        inputs.append(review_input)
    examined = set()
    for identity, entry in coverage.items():
        disposition = entry.get("disposition")
        if (
            set(entry) != {"id", "disposition", "reason"}
            or disposition not in ("candidate", "keep", "blocked", "elsewhere", "not-examined")
            or not _text(entry.get("reason"))
        ):
            raise ValueError("consolidation-invalid-coverage")
        if (disposition == "candidate") != (identity in claimed):
            raise ValueError("consolidation-candidate-coverage-mismatch")
        if disposition not in ("not-examined", "blocked"):
            examined.add(items[identity]["group"])
    if len(candidates) > 20 or len(examined) > 12:
        raise ValueError("consolidation-discovery-budget-exceeded")
    return {"outcomes": outcomes, "review_inputs": inputs}


def main() -> int:
    """Print structural settlement and digest-bound inputs for a retained advisory report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = settle_consolidation(json.loads(args.report.read_text(encoding="utf-8")))
    except (OSError, ValueError) as error:
        parser.exit(1, f"consolidation: {error}\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

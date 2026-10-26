"""Exercise consolidation advice at its source, review and artifact-consumer boundaries."""

from __future__ import annotations

import copy
import hashlib
import json
import runpy
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

HELPER = Path(__file__).resolve().parents[1] / "shared" / "audit_consolidation.py"


@pytest.fixture(scope="module", name="settle")
def _settle() -> Any:
    """Load the same entrypoint used by the audit artifact consumer."""
    return runpy.run_path(str(HELPER))["settle_consolidation"]


@pytest.fixture(name="report")
def _report(tmp_path: Path) -> dict[str, Any]:
    """Supply a concrete report shared by the helper's behavioral tests."""
    return make_report(tmp_path)


def make_report(tmp_path: Path) -> dict[str, Any]:
    """Create real source files and a smaller proposal preserving both obligations."""
    sources, items = [], []
    for identity, text in (
        ("first", "# Approval\nBefore publishing any release, the owner must approve publication.\n"),
        ("second", "# Release\nBefore publishing a release, the owner must approve the release.\n"),
        ("caller", "Read first.md §Approval before release.\n"),
    ):
        path = tmp_path / f"{identity}.md"
        path.write_text(text, encoding="utf-8")
        sources.append(
            {"id": identity, "path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        )
        if identity == "caller":
            continue
        items.append(
            {
                "id": identity,
                "source": identity,
                "start_line": 2,
                "end_line": 2,
                "load": "always",
                "group": "project-policy",
                "load_evidence": "Both files apply to this project in the same instruction layer.",
                "blocked": [],
                "protected_headings": [text.splitlines()[0]],
                "syntax_supported": True,
            }
        )
    return {
        "schema_version": 1,
        "sources": sources,
        "items": items,
        "coverage": [
            {"id": item["id"], "disposition": "candidate", "reason": "Equivalent approval intent."} for item in items
        ],
        "excluded": [],
        "candidates": [
            {
                "id": "publication",
                "members": ["first", "second"],
                "decision": "merge-prune",
                "reason": "Both require owner approval before publishing a release.",
                "edits": [
                    {
                        "item": "first",
                        "replacement": "Before publishing a release, the owner must approve publication.",
                    },
                    {"item": "second", "replacement": ""},
                ],
                "obligations": [
                    {
                        "from": item["id"],
                        "original": "the owner must approve",
                        "to_item": "first",
                        "to": "Before publishing a release, the owner must approve publication.",
                        "preserved": True,
                    }
                    for item in items
                ],
                "review": None,
            }
        ],
    }


def _review(report: dict[str, Any], settle: Any, verdict: str = "holds") -> None:
    """Attach a review of exactly the derived input, never a self-declared validation flag."""
    report["candidates"][0]["review"] = {
        "digest": settle(report)["review_inputs"][0]["digest"],
        "verdict": verdict,
        "evidence": "Compared both original obligations, order, actor, loading and unchanged citing heading.",
    }


@pytest.mark.integration
def test_cli_settles_reviewed_advice_without_writing_sources(
    report: dict[str, Any], settle: Any, tmp_path: Path
) -> None:
    """The ordinary CLI consumer gets reviewed advice while instruction bytes remain untouched."""
    _review(report, settle)
    before = {source["path"]: Path(source["path"]).read_bytes() for source in report["sources"]}
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(HELPER), "--report", str(path)], capture_output=True, text=True, check=True
    )
    outcome = json.loads(result.stdout)["outcomes"][0]
    assert outcome["status"] == "reviewed"
    assert outcome["decision"] == "merge-prune"
    assert outcome["byte_delta"] > 0
    assert {name: Path(name).read_bytes() for name in before} == before


@pytest.mark.parametrize("verdict", ["weakens", "loses"])
def test_negative_review_rejects_proposal(report: dict[str, Any], settle: Any, verdict: str) -> None:
    _review(report, settle, verdict)
    assert settle(report)["outcomes"][0]["status"] == "rejected"


def test_missing_then_stale_review_never_becomes_reviewed(report: dict[str, Any], settle: Any) -> None:
    assert settle(report)["outcomes"][0]["status"] == "unvalidated"
    _review(report, settle)
    report["candidates"][0]["edits"][0]["replacement"] += " Additional explanation."
    assert settle(report)["outcomes"][0]["status"] == "unvalidated"


@pytest.mark.parametrize("underline", ["-", "--", "=", "==", "---", "==="])
def test_every_setext_underline_remains_unsupported(report: dict[str, Any], settle: Any, underline: str) -> None:
    """A short underline is still heading syntax and cannot bypass the structural ceiling."""
    report["candidates"][0]["edits"][0]["replacement"] += "\n" + underline
    _review(report, settle)
    assert settle(report)["outcomes"][0]["status"] == "unvalidated"


@pytest.mark.parametrize("identity", ["first", "caller"])
def test_source_and_citing_drift_invalidate_review(report: dict[str, Any], settle: Any, identity: str) -> None:
    _review(report, settle)
    source = next(source for source in report["sources"] if source["id"] == identity)
    Path(source["path"]).write_text("Changed source.\n", encoding="utf-8")
    with pytest.raises(ValueError, match="source-drift"):
        settle(report)


def test_refreshing_citation_hash_does_not_reuse_old_review(report: dict[str, Any], settle: Any) -> None:
    _review(report, settle)
    source = report["sources"][-1]
    Path(source["path"]).write_text("Read first.md §Approval and retain its actor.\n", encoding="utf-8")
    source["sha256"] = hashlib.sha256(Path(source["path"]).read_bytes()).hexdigest()
    assert settle(report)["outcomes"][0]["status"] == "unvalidated"


@pytest.mark.parametrize(
    "damage", ["lazy", "unknown", "mixed-load", "different-group", "blocked", "syntax", "heading", "structure"]
)
def test_review_cannot_override_structural_ineligibility(report: dict[str, Any], settle: Any, damage: str) -> None:
    item = report["items"][1]
    if damage in {"lazy", "unknown"}:
        for entry in report["items"]:
            entry["load"] = damage
    elif damage == "mixed-load":
        item["load"] = "scoped"
    elif damage == "different-group":
        item["group"] = "another-project-layer"
    elif damage == "blocked":
        item["blocked"] = ["policy-sibling needs coordinated edit"]
    elif damage == "syntax":
        item["syntax_supported"] = False
    elif damage == "heading":
        item["start_line"] = 1
    else:
        report["candidates"][0]["edits"][0]["replacement"] += "\n<!--"
    _review(report, settle)
    assert settle(report)["outcomes"][0]["status"] == "unvalidated"


@pytest.mark.parametrize(
    "damage",
    ["source-id", "item-id", "candidate-id", "overlap", "coverage", "mapping", "quote", "edit", "member", "review"],
)
def test_malformed_or_incomplete_evidence_cannot_promote(report: dict[str, Any], settle: Any, damage: str) -> None:
    candidate = report["candidates"][0]
    if damage == "source-id":
        report["sources"].append(copy.deepcopy(report["sources"][0]))
    elif damage == "item-id":
        report["items"].append(copy.deepcopy(report["items"][0]))
    elif damage == "candidate-id":
        report["candidates"].append(copy.deepcopy(candidate))
    elif damage == "overlap":
        duplicate = dict(report["items"][0], id="overlap")
        report["items"].append(duplicate)
    elif damage == "coverage":
        report["coverage"].pop()
    elif damage == "mapping":
        candidate["obligations"].pop()
    elif damage == "quote":
        candidate["obligations"][0]["original"] = "An obligation absent from the source."
    elif damage == "edit":
        candidate["edits"].append(copy.deepcopy(candidate["edits"][0]))
    elif damage == "member":
        candidate["members"][1] = "first"
    else:
        candidate["review"] = {"digest": "a" * 64, "verdict": "holds", "evidence": None}
    with pytest.raises(ValueError, match="consolidation"):
        settle(report)


def test_lazy_sharpen_stays_local_and_keep_needs_no_review(report: dict[str, Any], settle: Any) -> None:
    candidate = report["candidates"][0]
    candidate["decision"] = "sharpen"
    for item, edit, obligation in zip(report["items"], candidate["edits"], candidate["obligations"], strict=True):
        item["load"] = "lazy"
        item["group"] = f"independent-skill-{item['id']}"
        edit["replacement"] = "Before publishing a release, the owner must approve publication."
        obligation["to_item"] = item["id"]
    _review(report, settle)
    assert settle(report)["outcomes"][0]["status"] == "reviewed"
    candidate["obligations"][1]["to_item"] = "first"
    with pytest.raises(ValueError, match="sharpen-moves-obligation"):
        settle(report)
    candidate.update(decision="keep", edits=[], obligations=[], review=None)
    assert settle(report)["outcomes"][0]["status"] == "keep"


def test_empty_scope_has_explicit_successful_advice(settle: Any) -> None:
    report = {"schema_version": 1, "sources": [], "items": [], "coverage": [], "candidates": [], "excluded": []}
    assert settle(report) == {"outcomes": [], "review_inputs": []}

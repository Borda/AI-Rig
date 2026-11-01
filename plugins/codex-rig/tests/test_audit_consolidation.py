"""Exercise consolidation advice at its source, review and artifact-consumer boundaries."""

from __future__ import annotations

import copy
import hashlib
import json
import runpy
import subprocess
import sys
from collections.abc import Callable
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


def empty_report() -> dict[str, Any]:
    """Return a schema-valid report with nothing to consolidate."""
    return {"schema_version": 1, "sources": [], "items": [], "coverage": [], "candidates": [], "excluded": []}


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
    """A review that finds the proposal weaker than the sources rejects it."""
    _review(report, settle, verdict)

    assert settle(report)["outcomes"][0]["status"] == "rejected"


def test_missing_review_stays_unvalidated(report: dict[str, Any], settle: Any) -> None:
    """A proposal with no review is advice that has not been validated.

    A fresh report carries `review: None`; only an attached review of the derived input may promote it.
    """
    assert settle(report)["outcomes"][0]["status"] == "unvalidated"


def test_stale_review_stays_unvalidated(report: dict[str, Any], settle: Any) -> None:
    """Editing the proposal after its review invalidates that review instead of leaving it reviewed.

    The review digest binds the exact derived input, so an appended sentence must drop the outcome back.
    """
    _review(report, settle)
    report["candidates"][0]["edits"][0]["replacement"] += " Additional explanation."

    assert settle(report)["outcomes"][0]["status"] == "unvalidated"


@pytest.mark.parametrize(
    "suffix", ["-", "--", "=", "==", "---", "===", pytest.param("<!--", id="unterminated-comment")]
)
def test_unsupported_markup_in_replacement_stays_unvalidated(report: dict[str, Any], settle: Any, suffix: str) -> None:
    """A replacement ending in markup the validator cannot model cannot be reviewed into acceptance.

    Every short setext underline is still heading syntax and an open comment hides text, so both stay under the
    structural ceiling even when a review of the derived input is attached.
    """
    report["candidates"][0]["edits"][0]["replacement"] += "\n" + suffix
    _review(report, settle)

    assert settle(report)["outcomes"][0]["status"] == "unvalidated"


@pytest.mark.parametrize("identity", ["first", "caller"])
def test_source_and_citing_drift_invalidate_review(report: dict[str, Any], settle: Any, identity: str) -> None:
    """Changing a member source or the citing file after review is reported as source drift.

    The hashes recorded in the report no longer match disk, so settling must raise instead of reusing the review.
    """
    _review(report, settle)
    source = next(source for source in report["sources"] if source["id"] == identity)
    Path(source["path"]).write_text("Changed source.\n", encoding="utf-8")

    with pytest.raises(ValueError, match="source-drift"):
        settle(report)


def test_refreshing_citation_hash_does_not_reuse_old_review(report: dict[str, Any], settle: Any) -> None:
    """Rewriting the citing file and refreshing its hash still invalidates an earlier review.

    The citing text is part of the reviewed input, so a consistent new hash must not carry the old verdict over.
    """
    _review(report, settle)
    source = report["sources"][-1]
    Path(source["path"]).write_text("Read first.md §Approval and retain its actor.\n", encoding="utf-8")
    source["sha256"] = hashlib.sha256(Path(source["path"]).read_bytes()).hexdigest()

    assert settle(report)["outcomes"][0]["status"] == "unvalidated"


def _patch_items(report: dict[str, Any], indexes: tuple[int, ...], patch: dict[str, Any]) -> None:
    """Apply one field patch to the selected report items."""
    for index in indexes:
        report["items"][index].update(patch)


@pytest.mark.parametrize(
    ("indexes", "patch"),
    [
        pytest.param((0, 1), {"load": "lazy"}, id="lazy"),
        pytest.param((0, 1), {"load": "unknown"}, id="unknown"),
        pytest.param((1,), {"load": "scoped"}, id="mixed-load"),
        pytest.param((1,), {"group": "another-project-layer"}, id="different-group"),
        pytest.param((1,), {"blocked": ["policy-sibling needs coordinated edit"]}, id="blocked"),
        pytest.param((1,), {"syntax_supported": False}, id="syntax"),
        pytest.param((1,), {"start_line": 1}, id="heading"),
    ],
)
def test_review_cannot_override_structural_ineligibility(
    report: dict[str, Any], settle: Any, indexes: tuple[int, ...], patch: dict[str, Any]
) -> None:
    """A holding review never promotes a merge whose items are structurally ineligible to merge.

    Lazy or unknown loading, mixed load scopes, different groups, blocked items, unsupported syntax and a protected
    heading line each cap the outcome at unvalidated whatever the reviewer concluded.
    """
    _patch_items(report, indexes, patch)
    _review(report, settle)

    assert settle(report)["outcomes"][0]["status"] == "unvalidated"


@pytest.mark.parametrize(
    "corrupt",
    [
        pytest.param(lambda r: r["sources"].append(copy.deepcopy(r["sources"][0])), id="source-id"),
        pytest.param(lambda r: r["items"].append(copy.deepcopy(r["items"][0])), id="item-id"),
        pytest.param(lambda r: r["candidates"].append(copy.deepcopy(r["candidates"][0])), id="candidate-id"),
        pytest.param(lambda r: r["items"].append(dict(r["items"][0], id="overlap")), id="overlap"),
        pytest.param(lambda r: r["coverage"].pop(), id="coverage"),
        pytest.param(lambda r: r["candidates"][0]["obligations"].pop(), id="mapping"),
        pytest.param(
            lambda r: r["candidates"][0]["obligations"][0].update(original="An obligation absent from the source."),
            id="quote",
        ),
        pytest.param(
            lambda r: r["candidates"][0]["edits"].append(copy.deepcopy(r["candidates"][0]["edits"][0])), id="edit"
        ),
        pytest.param(lambda r: r["candidates"][0].update(members=["first", "first"]), id="member"),
        pytest.param(
            lambda r: r["candidates"][0].update(review={"digest": "a" * 64, "verdict": "holds", "evidence": None}),
            id="review",
        ),
    ],
)
def test_malformed_or_incomplete_evidence_cannot_promote(
    report: dict[str, Any], settle: Any, corrupt: Callable[[dict[str, Any]], object]
) -> None:
    """Duplicate ids, uncovered items, unmapped or misquoted obligations and empty reviews are refused outright.

    Each corruption breaks one structural invariant of the report, so settling raises a consolidation error rather than
    producing any outcome.
    """
    corrupt(report)

    with pytest.raises(ValueError, match="consolidation"):
        settle(report)


@pytest.fixture(name="sharpened")
def _sharpened(report: dict[str, Any]) -> dict[str, Any]:
    """Turn the report into a `sharpen` candidate whose two lazy items each keep their own obligation."""
    candidate = report["candidates"][0]
    candidate["decision"] = "sharpen"
    for item, edit, obligation in zip(report["items"], candidate["edits"], candidate["obligations"], strict=True):
        item["load"] = "lazy"
        item["group"] = f"independent-skill-{item['id']}"
        edit["replacement"] = "Before publishing a release, the owner must approve publication."
        obligation["to_item"] = item["id"]
    return report


def test_lazy_sharpen_stays_local(sharpened: dict[str, Any], settle: Any) -> None:
    """Sharpening lazy independent items can be reviewed because each obligation stays in its own item."""
    _review(sharpened, settle)

    assert settle(sharpened)["outcomes"][0]["status"] == "reviewed"


def test_sharpen_rejects_moving_an_obligation(sharpened: dict[str, Any], settle: Any) -> None:
    """Sharpening must not relocate an obligation into another lazy item, which would change what loads when."""
    sharpened["candidates"][0]["obligations"][1]["to_item"] = "first"

    with pytest.raises(ValueError, match="sharpen-moves-obligation"):
        settle(sharpened)


def test_keep_needs_no_review(sharpened: dict[str, Any], settle: Any) -> None:
    """A `keep` decision with no edits settles as keep without any attached review."""
    sharpened["candidates"][0].update(decision="keep", edits=[], obligations=[], review=None)

    assert settle(sharpened)["outcomes"][0]["status"] == "keep"


def test_empty_scope_has_explicit_successful_advice(settle: Any) -> None:
    """A report with no sources or candidates settles to empty outcomes instead of failing."""
    assert settle(empty_report()) == {"outcomes": [], "review_inputs": []}

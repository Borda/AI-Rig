"""Tests for ``bin/mint_finding_ids.py`` and ``bin/merge_action_items.py``.

Both scripts implement the review → resolve handoff: the review mints stable finding ids, resolve folds those findings
into its numbered item list without renumbering. Files are real JSONL under ``tmp_path``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import merge_action_items as mai
import mint_finding_ids as mfi
import pytest

_TAXONOMY = Path(__file__).parents[2] / "skills" / "_shared" / "review-section-taxonomy.md"


def _write_jsonl(path: Path, records: list[dict]) -> Path:
    """Write ``records`` one per line and return ``path``."""
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8", newline="\n")
    return path


def _finding(**overrides: object) -> dict:
    """Return a complete review finding, overridable per test."""
    record = {
        "section": "### Architecture & Quality",
        "severity": "high",
        "title": "Missing None check",
        "full_text": "[high] `load()` dereferences `cfg` without a None check (src/app.py:12)",
        "file": "src/app.py",
        "line": 12,
        "change": "code",
        "author": "foundry:sw-engineer",
        "source_file": "run/foundry--sw-engineer.md",
        "verify_file": "run/verify-1.md",
        "verify_verdict": "CONFIRMED",
    }
    record.update(overrides)
    return record


def _findings_file(tmp_path: Path, records: list[dict]) -> Path:
    """Write minted findings where the review keeps them: a report directory, not the resolve item directory."""
    review_dir = tmp_path / "review"
    review_dir.mkdir(exist_ok=True)
    return _write_jsonl(review_dir / "findings.jsonl", mfi.mint(records))


def _gh(item_id: int, **overrides: object) -> dict:
    """Return a pending GitHub action item, overridable per test."""
    record = {
        "id": item_id,
        "type": "[gh][req]",
        "change": "code",
        "severity": 3,
        "author": "@reviewer",
        "summary": "guard cfg",
        "file": "src/app.py",
        "line": 12,
        "url": f"https://example.invalid/c/{item_id}",
        "full_comment_text": "cfg can be None here",
        "location": "inline",
        "origin": "posted",
    }
    record.update(overrides)
    return record


@pytest.mark.parametrize(
    ("section", "slug"),
    [
        pytest.param("### [blocking] Critical (must fix before merge)", "critical", id="blocking-critical"),
        pytest.param("### ⚠ LOW CONFIDENCE — API Design (if applicable)", "api-design", id="low-confidence-prefix"),
        pytest.param("### Architecture & Quality", "architecture-quality", id="ampersand"),
    ],
)
def test_section_slug_drops_decoration(section: str, slug: str) -> None:
    """Heading marks, confidence prefix, blocking tag and qualifiers never change the slug."""
    assert mfi.section_slug(section) == slug


def test_mint_is_stable_under_line_drift_and_path_spelling() -> None:
    """The same finding at another line, spelled with Windows separators, keeps its id."""
    first = mfi.mint([_finding()])[0]["id"]
    moved = mfi.mint([_finding(line=40, file=".\\src\\app.py", title="  missing none CHECK. ")])[0]["id"]
    assert first == moved
    assert re.fullmatch(r"architecture-quality-[0-9a-f]{8}", first)


def test_mint_suffixes_collisions_in_file_order() -> None:
    """Two findings that hash alike get deterministic ``-2`` suffixes."""
    out = mfi.mint([_finding(), _finding(line=99)])
    assert out[1]["id"] == out[0]["id"] + "-2"


def test_mint_collision_suffix_ignores_report_order() -> None:
    """Colliding findings keep their ids when the report lists them in another order.

    A resolution ledger names findings by id; if suffixes followed report order, a re-run that reordered two colliding
    findings would attach one finding's outcome to the other.
    """
    first, second = _finding(full_text="cfg may be None"), _finding(line=99, full_text="cfg reloaded twice")
    forward = mfi.mint([dict(first), dict(second)])
    backward = mfi.mint([dict(second, line=140), dict(first, line=3)])
    assert {f["full_text"]: f["id"] for f in forward} == {f["full_text"]: f["id"] for f in backward}


def test_mint_cli_rejects_incomplete_record(tmp_path: Path) -> None:
    """A record without a title blocks instead of minting a weak id."""
    path = _write_jsonl(tmp_path / "findings.jsonl", [_finding(title="")])
    assert mfi.main([str(path)]) == 1


def test_report_mode_assigns_ids_from_one(tmp_path: Path) -> None:
    """With no GitHub items, findings become report items 1..N in findings order."""
    findings = _findings_file(tmp_path, [_finding(), _finding(title="Other", line=3)])
    items = tmp_path / "action-items.jsonl"
    assert mai.main(["--items", str(items), "--findings", str(findings)]) == 0
    rows = mfi.read_jsonl(items)
    assert [r["id"] for r in rows] == [1, 2]
    assert rows[0]["type"] == "[report][req]" and rows[0]["location"] == "report"
    assert rows[0]["finding_id"].startswith("architecture-quality-")
    assert rows[0]["verify_verdict"] == "CONFIRMED"


def test_exact_match_annotates_github_item_without_verdict(tmp_path: Path) -> None:
    """A finding at the same file:line enriches the terse GitHub item; its id never changes.

    The verifier confirmed the review's claim, not the GitHub comment's, so the item must not inherit the verdict.
    """
    items = _write_jsonl(tmp_path / "action-items.jsonl", [_gh(1), _gh(2, file="src/other.py", line=5)])
    findings = _findings_file(tmp_path, [_finding()])
    assert mai.main(["--items", str(items), "--findings", str(findings)]) == 0
    rows = {r["id"]: r for r in mfi.read_jsonl(items)}
    assert set(rows) == {1, 2}
    assert rows[1]["author"] == "@reviewer + foundry:sw-engineer"
    assert "(also flagged by /review — foundry:sw-engineer)" in rows[1]["summary"]
    assert "[review finding architecture-quality-" in rows[1]["full_comment_text"]
    assert "verify_verdict" not in rows[1] and rows[1]["source_file"] == "run/foundry--sw-engineer.md"
    assert rows[1]["severity"] == 4


def test_semantic_link_inherits_detail_but_not_verdict(tmp_path: Path) -> None:
    """A judged same-target pair at another line must still be re-checked by the challenge phase."""
    items = _write_jsonl(tmp_path / "action-items.jsonl", [_gh(1, line=30)])
    minted = mfi.mint([_finding()])
    findings = _write_jsonl(tmp_path / "f.jsonl", minted)
    links = _write_jsonl(tmp_path / "links.jsonl", [{"finding_id": minted[0]["id"], "item_id": 1}])
    assert mai.main(["--items", str(items), "--findings", str(findings), "--links", str(links)]) == 0
    (row,) = mfi.read_jsonl(items)
    assert row["finding_id"] == minted[0]["id"]
    assert row["verify_file"] == "run/verify-1.md"
    assert "verify_verdict" not in row


def test_unmatched_findings_append_after_highest_github_id(tmp_path: Path) -> None:
    """Report items take ids above the existing maximum, gaps included; the file is only appended to."""
    items = _write_jsonl(
        tmp_path / "action-items.jsonl", [_gh(1, file="x.py", full_comment_text="x" * 100), _gh(4, type="[done]")]
    )
    before = items.read_text(encoding="utf-8")
    findings = _write_jsonl(tmp_path / "f.jsonl", mfi.mint([_finding()]))
    assert mai.main(["--items", str(items), "--findings", str(findings)]) == 0
    after = items.read_text(encoding="utf-8")
    assert after.startswith(before)
    assert [r["id"] for r in mfi.read_jsonl(items)] == [1, 4, 5]


def test_unmatched_terse_github_item_is_marked_thin(tmp_path: Path) -> None:
    """A short GitHub comment no finding covers gets reassessed from scratch."""
    items = _write_jsonl(tmp_path / "action-items.jsonl", [_gh(1, file="z.py", full_comment_text="nit")])
    findings = _write_jsonl(tmp_path / "f.jsonl", mfi.mint([_finding()]))
    assert mai.main(["--items", str(items), "--findings", str(findings)]) == 0
    assert mfi.read_jsonl(items)[0]["thin"] is True


def test_candidates_lists_same_file_pairs_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Same-file, different-line pairs are offered for judgment without touching the item file."""
    items = _write_jsonl(tmp_path / "action-items.jsonl", [_gh(1, line=30)])
    before = items.read_text(encoding="utf-8")
    findings = _write_jsonl(tmp_path / "f.jsonl", mfi.mint([_finding()]))
    assert mai.main(["--items", str(items), "--findings", str(findings), "--candidates"]) == 0
    pairs = json.loads(capsys.readouterr().out)
    assert [(p["item_id"], p["finding_line"]) for p in pairs] == [(1, 12)]
    assert items.read_text(encoding="utf-8") == before


@pytest.mark.parametrize(
    ("items", "message"),
    [
        pytest.param([_gh(1), _gh(1)], "duplicate", id="duplicate-id"),
        pytest.param([_gh(1, id="1")], "non-positive or non-integer", id="string-id"),
    ],
)
def test_bad_item_ids_block(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], items: list[dict], message: str
) -> None:
    """Step 8 looks items up by unique integer id, so anything else stops the merge."""
    path = _write_jsonl(tmp_path / "action-items.jsonl", items)
    findings = _write_jsonl(tmp_path / "f.jsonl", mfi.mint([_finding()]))
    assert mai.main(["--items", str(path), "--findings", str(findings)]) == 1
    assert message in capsys.readouterr().err


def test_unknown_link_blocks(tmp_path: Path) -> None:
    """A semantic link to an item that does not exist is an orchestrator error, not a silent drop."""
    items = _write_jsonl(tmp_path / "action-items.jsonl", [_gh(1)])
    minted = mfi.mint([_finding(line=5)])
    findings = _write_jsonl(tmp_path / "f.jsonl", minted)
    links = _write_jsonl(tmp_path / "links.jsonl", [{"finding_id": minted[0]["id"], "item_id": 9}])
    assert mai.main(["--items", str(items), "--findings", str(findings), "--links", str(links)]) == 1


def test_two_findings_on_one_line_never_share_an_item(tmp_path: Path) -> None:
    """The worst finding claims the GitHub item; the other becomes its own item with its own evidence.

    Folding both into one item would let one finding's verifier file vouch for the other's claim and would drop the
    first finding's id from the resolution ledger.
    """
    items = _write_jsonl(tmp_path / "action-items.jsonl", [_gh(1)])
    unverified = _finding(
        section="### Performance Concerns", severity="medium", title="Slow lookup", verify_file="", verify_verdict=""
    )
    findings = _findings_file(tmp_path, [unverified, _finding()])
    assert mai.main(["--items", str(items), "--findings", str(findings)]) == 0
    rows = {r["id"]: r for r in mfi.read_jsonl(items)}
    assert rows[1]["finding_id"].startswith("architecture-quality-")
    assert rows[2]["finding_id"].startswith("performance-concerns-")
    assert "verify_verdict" not in rows[2] and "verify_file" not in rows[2]


def test_github_item_does_not_inherit_codex_eligibility(tmp_path: Path) -> None:
    """Codex eligibility describes the review's claim; a GitHub request on the same line may need a specialist."""
    items = _write_jsonl(tmp_path / "action-items.jsonl", [_gh(1)])
    findings = _findings_file(tmp_path, [_finding(codex_eligible=True)])
    assert mai.main(["--items", str(items), "--findings", str(findings)]) == 0
    assert "codex_eligible" not in mfi.read_jsonl(items)[0]


def test_withdrawn_duplicate_link_frees_its_item(tmp_path: Path) -> None:
    """A finding linked twice is ignored, and the item it first claimed stays open for another finding's link."""
    path = _write_jsonl(tmp_path / "action-items.jsonl", [_gh(1, line=30), _gh(2, line=31)])
    findings = _findings_file(tmp_path, [_finding(line=5), _finding(title="Other", line=6)])
    first, other = (f["id"] for f in mfi.read_jsonl(findings))
    rows = [
        {"finding_id": first, "item_id": 1},
        {"finding_id": first, "item_id": 2},
        {"finding_id": other, "item_id": 1},
    ]
    links = _write_jsonl(tmp_path / "links.jsonl", rows)
    assert mai.main(["--items", str(path), "--findings", str(findings), "--links", str(links)]) == 0
    assert mfi.read_jsonl(path)[0]["finding_id"] == other


def test_rerun_merge_changes_nothing(tmp_path: Path) -> None:
    """A retried merge block (e.g. after compaction) must not duplicate items or annotations."""
    items = _write_jsonl(tmp_path / "action-items.jsonl", [_gh(1)])
    findings = _findings_file(tmp_path, [_finding(), _finding(title="Other", line=40)])
    assert mai.main(["--items", str(items), "--findings", str(findings)]) == 0
    first = items.read_text(encoding="utf-8")
    assert mai.main(["--items", str(items), "--findings", str(findings)]) == 0
    assert items.read_text(encoding="utf-8") == first


def test_findings_parsed_by_resolve_carry_no_verdict(tmp_path: Path) -> None:
    """A findings file beside the item list was written by resolve from an old report, so no verifier backs it."""
    items = tmp_path / "action-items.jsonl"
    findings = _write_jsonl(tmp_path / "report-findings.jsonl", mfi.mint([_finding()]))
    assert mai.main(["--items", str(items), "--findings", str(findings)]) == 0
    assert "verify_verdict" not in mfi.read_jsonl(items)[0]


@pytest.mark.parametrize(
    ("items", "link_to"),
    [
        pytest.param([_gh(1, line=30, type="[done]")], 1, id="closed-item"),
        pytest.param([_gh(1, line=30, finding_id="api-design-00000000")], 1, id="item-already-holds-a-finding"),
    ],
)
def test_link_to_unavailable_item_appends_the_finding(tmp_path: Path, items: list[dict], link_to: int) -> None:
    """A semantic link the item cannot take falls back to appending, so the finding stays selectable."""
    path = _write_jsonl(tmp_path / "action-items.jsonl", items)
    findings = _findings_file(tmp_path, [_finding()])
    finding_id = mfi.read_jsonl(findings)[0]["id"]
    links = _write_jsonl(tmp_path / "links.jsonl", [{"finding_id": finding_id, "item_id": link_to}])
    assert mai.main(["--items", str(path), "--findings", str(findings), "--links", str(links)]) == 0
    assert [r.get("finding_id") for r in mfi.read_jsonl(path)][-1] == finding_id
    assert len(mfi.read_jsonl(path)) == 2


@pytest.mark.parametrize(
    ("overrides", "thin"),
    [
        pytest.param(
            {"file": "", "full_comment_text": "Please add a CHANGELOG entry for the new option. " * 2},
            False,
            id="long-request-without-file",
        ),
        pytest.param({"full_comment_text": "why?", "type": "[gh][question]"}, False, id="question"),
        pytest.param({"file": "", "full_comment_text": "fix this"}, True, id="short-without-file"),
    ],
)
def test_thin_flag_marks_only_short_requests(tmp_path: Path, overrides: dict, thin: bool) -> None:
    """Only a short comment lacks the detail to act on; a clear request without a file location does not."""
    items = _write_jsonl(tmp_path / "action-items.jsonl", [_gh(1, line=77, **overrides)])
    findings = _findings_file(tmp_path, [_finding()])
    assert mai.main(["--items", str(items), "--findings", str(findings)]) == 0
    assert bool(mfi.read_jsonl(items)[0].get("thin")) is thin


@pytest.mark.parametrize(
    ("review_head", "current_head", "verify_exists", "kept"),
    [
        pytest.param("abc1234", "abc1234def", True, True, id="same-head"),
        pytest.param("abc1234", "fff9999", True, False, id="head-moved"),
        pytest.param("", "abc1234", True, False, id="review-head-unknown"),
        pytest.param("abc1234", "abc1234", False, False, id="verifier-file-swept"),
    ],
)
def test_recheck_keeps_verdict_only_when_evidence_still_holds(
    tmp_path: Path, review_head: str, current_head: str, verify_exists: bool, kept: bool
) -> None:
    """A confirmed verdict skips the existence check only while the head and the verifier's file are unchanged."""
    review_dir = tmp_path / "review"
    review_dir.mkdir()
    if review_head:
        (review_dir / "head-sha.txt").write_text(review_head + "\n", encoding="utf-8")
    verify = tmp_path / "verify-1.md"
    if verify_exists:
        verify.write_text("CONFIRMED", encoding="utf-8")
    items = _write_jsonl(tmp_path / "action-items.jsonl", [_gh(1, verify_verdict="CONFIRMED", verify_file=str(verify))])
    argv = ["--items", str(items), "--recheck-verdicts", "--review-dir", str(review_dir)]
    assert mai.main([*argv, "--current-head", current_head]) == 0
    assert ("verify_verdict" in mfi.read_jsonl(items)[0]) is kept


def test_medium_req_sections_match_taxonomy() -> None:
    """The script's MEDIUM → [req] sections are exactly the taxonomy's code-related MEDIUM rows plus Critical."""
    text = _TAXONOMY.read_text(encoding="utf-8")
    row = next(line for line in text.splitlines() if line.startswith("| MEDIUM |") and "`[req]`" in line)
    names = [part.strip() for part in row.split("|")[2].replace("(code-related)", "").split(",")]
    headers = {
        "Architecture": "### Architecture & Quality",
        "Performance": "### Performance Concerns",
        "API Design": "### API Design (if applicable)",
    }
    expected = {mfi.section_slug(headers[name]) for name in names} | {"critical"}
    assert expected == set(mai.REQ_MEDIUM_SECTIONS)

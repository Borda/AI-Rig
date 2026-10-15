"""Tests for ``bin/mint_finding_ids.py`` and ``bin/merge_action_items.py``.

Both scripts implement the review → resolve handoff: the review mints stable finding ids, resolve folds those findings
into its numbered item list without renumbering. Files are real JSONL under ``tmp_path``.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import find_review_report as frr
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
    assert rows[0]["type"] == "[report][req]"
    assert rows[0]["location"] == "report"
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
    assert "verify_verdict" not in rows[1]
    assert rows[1]["source_file"] == "run/foundry--sw-engineer.md"
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


@pytest.mark.parametrize(
    ("closed_type", "status"),
    [
        pytest.param("[done][gh][req]", None, id="done-without-status"),
        pytest.param("[done][gh][req]", "pending", id="done-with-pending-status"),
        pytest.param("[info][gh][req]", None, id="info-without-status"),
        pytest.param("[info][gh][req]", "pending", id="info-with-pending-status"),
    ],
)
@pytest.mark.parametrize("route", ["exact", "semantic"])
def test_closed_type_preserves_history_and_appends_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], closed_type: str, status: str | None, route: str
) -> None:
    """Closed type markers protect history even when request markers and an open status remain."""
    history = _gh(1, type=closed_type, line=12 if route == "exact" else 30)
    if status is not None:
        history["status"] = status
    items = _write_jsonl(tmp_path / "action-items.jsonl", [history])
    before = items.read_bytes()
    findings = _findings_file(tmp_path, [_finding()])
    finding_id = mfi.read_jsonl(findings)[0]["id"]
    argv = ["--items", str(items), "--findings", str(findings)]
    if route == "semantic":
        assert mai.main([*argv, "--candidates"]) == 0
        assert json.loads(capsys.readouterr().out) == []
        assert items.read_bytes() == before
        links = _write_jsonl(tmp_path / "links.jsonl", [{"finding_id": finding_id, "item_id": 1}])
        argv.extend(["--links", str(links)])
    assert mai.main(argv) == 0
    history_row, finding_row = mfi.read_jsonl(items)
    assert history_row == history
    assert items.read_bytes().startswith(before)
    assert finding_row["id"] == 2
    assert finding_row["type"] == "[report][req]"
    assert finding_row["finding_id"] == finding_id
    assert finding_row["full_comment_text"] == _finding()["full_text"]


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


@pytest.mark.parametrize(
    ("finding", "link", "message"),
    [
        pytest.param(
            dict(_finding(), id="architecture-quality-1", section=5), None, "field section is int", id="int-section"
        ),
        pytest.param(dict(_finding(), id=["architecture-quality-1"]), None, "field id is list", id="list-id"),
        pytest.param(dict(_finding(), id="architecture-quality-1", file=3), None, "field file is int", id="int-file"),
        pytest.param(
            dict(_finding(), id="architecture-quality-1", line=[12]), None, "field line is list", id="list-line"
        ),
        pytest.param(7, None, "finding 1 is not a JSON object", id="row-not-object"),
        pytest.param(
            dict(_finding(), id="architecture-quality-1"),
            {"finding_id": ["architecture-quality-1"], "item_id": 1},
            "link 1 is not a {finding_id: text, item_id: number} object",
            id="list-link-finding-id",
        ),
    ],
)
def test_wrongly_typed_rows_block_instead_of_crashing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], finding: object, link: dict | None, message: str
) -> None:
    """A finding row or link whose fields are not the expected JSON types refuses with ``! BLOCKED``.

    A numeric section used to reach the section slug's string operations and a list id the merge's id sets, raising
    ``AttributeError``/``TypeError`` mid-merge (found by the resolve intake fuzz under ``--accept-degraded``).
    """
    items = _write_jsonl(tmp_path / "action-items.jsonl", [_gh(1)])
    findings = _write_jsonl(tmp_path / "f.jsonl", [finding])
    links = _write_jsonl(tmp_path / "links.jsonl", [link] if link else [])
    assert mai.main(["--items", str(items), "--findings", str(findings), "--links", str(links)]) == 1
    error = capsys.readouterr().err
    assert error.startswith("! BLOCKED — ")
    assert message in error


@pytest.mark.parametrize(
    ("finding", "message"),
    [
        pytest.param(
            {key: value for key, value in _finding(id="architecture-quality-1").items() if key != "author"},
            "! BLOCKED — finding 1 missing author — run mint_finding_ids.py and complete the record\n",
            id="missing-author",
        ),
        pytest.param(
            dict(_finding(), id="architecture-quality-1", section=5),
            "! BLOCKED — finding 1 field section is int, not text — the review must write it as one JSON object of "
            "text fields\n",
            id="int-section",
        ),
        pytest.param(
            dict(_finding(), id="architecture-quality-1", severity="major"),
            "! BLOCKED — finding 1 has unknown severity 'major' — use one of critical, high, medium, low, cosmetic\n",
            id="unknown-severity",
        ),
    ],
)
def test_merge_and_producer_gate_share_one_row_rule(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], finding: dict, message: str
) -> None:
    """The merge refuses a row with exactly the words ``oss:review``'s producer gate uses for that row.

    The required-field list, the type check and the severity vocabulary live in ``find_review_report`` so the review can
    refuse, before delivery, any row this merge would refuse; an unknown severity used to pass the producer gate and
    block only here.
    """
    items = _write_jsonl(tmp_path / "action-items.jsonl", [])
    findings = _write_jsonl(tmp_path / "f.jsonl", [finding])
    assert mai.main(["--items", str(items), "--findings", str(findings)]) == 1
    assert capsys.readouterr().err == message
    assert frr.finding_row_problem(finding, 1) == message.removeprefix("! BLOCKED — ").rstrip("\n")


def test_severity_vocabulary_is_shared_by_mint_gate_and_merge() -> None:
    """Mint, the producer gate and the merge accept the same severity words, scored most severe first."""
    expected_scores = {"critical": 5, "high": 4, "medium": 3, "low": 2, "cosmetic": 1}
    assert (mfi.SEVERITIES, mai.SEVERITY_SCORE) == (frr.FINDING_SEVERITIES, expected_scores)


def test_findings_sharing_an_id_block_instead_of_collapsing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Two findings with one id stop the merge before anything is written; neither is folded into the other.

    Regression: the merge keyed findings by id, so the second row's confirmed finding was silently lost while its text
    replaced the first row's detail on the GitHub item at the first row's line.
    """
    items = _write_jsonl(tmp_path / "action-items.jsonl", [_gh(1)])
    before = items.read_bytes()
    rows = [_finding(id="dup-1"), _finding(id="dup-1", file="src/other.py", line=9, title="Other")]
    findings = _write_jsonl(tmp_path / "f.jsonl", rows)
    assert mai.main(["--items", str(items), "--findings", str(findings)]) == 1
    assert capsys.readouterr().err.startswith("! BLOCKED — duplicate finding id dup-1 on findings 1 and 2")
    assert items.read_bytes() == before


def test_mint_carry_from_keeps_a_carried_row_id_against_an_outranking_collision(tmp_path: Path) -> None:
    """A row equal to a prior row except its id keeps that id; a colliding new row takes the next free suffix.

    Without ``--carry-from`` the new row's text outranks the carried one and takes the base id, re-suffixing a row a
    fill-gaps run must publish verbatim.
    """
    prior_path = _write_jsonl(tmp_path / "prior.jsonl", mfi.mint([_finding(full_text="alpha")]))
    carried = mfi.read_jsonl(prior_path)[0]
    new_path = _write_jsonl(tmp_path / "findings.jsonl", [carried, _finding(full_text="beta", line=40)])
    assert mfi.main([str(new_path), "--carry-from", str(prior_path)]) == 0
    minted = mfi.read_jsonl(new_path)
    assert (minted[0], minted[1]["id"]) == (carried, carried["id"] + "-2")


def test_mint_without_carry_from_is_unchanged(tmp_path: Path) -> None:
    """Without ``--carry-from`` minting ignores prior ids exactly as before, outranking collisions included."""
    carried = mfi.mint([_finding(full_text="alpha")])[0]
    path = _write_jsonl(tmp_path / "findings.jsonl", [carried, _finding(full_text="beta", line=40)])
    assert mfi.main([str(path)]) == 0
    assert [row["id"] for row in mfi.read_jsonl(path)] == [carried["id"] + "-2", carried["id"]]


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
    assert "verify_verdict" not in rows[2]
    assert "verify_file" not in rows[2]


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
        pytest.param([_gh(1, line=30, type="[done]")], 1, id="legacy-done-item"),
        pytest.param([_gh(1, line=30, status="resolved")], 1, id="resolved-item"),
        pytest.param([_gh(1, line=30, status="addressed")], 1, id="addressed-item"),
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


@pytest.mark.parametrize("verify_exists", [True, False])
def test_recheck_resolves_serialized_git_bash_verifier_path_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verify_exists: bool
) -> None:
    """Resolve a JSON evidence pointer before testing the real verifier file, preserving its recorded spelling."""
    review = tmp_path / "review"
    review.mkdir()
    (review / "head-sha.txt").write_text("abc1234\n", encoding="utf-8", newline="\n")
    verifier = review / "verify result.md"
    if verify_exists:
        verifier.write_text("Independent confirmation", encoding="utf-8")
    pointer = "/c/review dir/verify $(touch injected) `payload`.md"
    items = _write_jsonl(tmp_path / "action-items.jsonl", [_gh(1, verify_verdict="CONFIRMED", verify_file=pointer)])
    calls = []

    def convert(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Model only the external Git Bash converter; the evidence check reads a real file."""
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout=str(verifier) + "\n")

    monkeypatch.setattr(mai.sys, "platform", "win32")
    monkeypatch.setattr(mai.subprocess, "run", convert)
    assert (
        mai.main(
            ["--items", str(items), "--recheck-verdicts", "--review-dir", str(review), "--current-head", "abc1234"]
        )
        == 0
    )
    row = mfi.read_jsonl(items)[0]
    assert row.get("verify_verdict") == ("CONFIRMED" if verify_exists else None)
    assert row["verify_file"] == pointer
    assert calls == [(["cygpath", "-w", pointer], {"capture_output": True, "text": True, "check": True, "timeout": 5})]
    assert not (tmp_path / "injected").exists()


def test_recheck_blocks_on_verifier_path_conversion_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed converter must leave the evidence ledger intact and return a blocking failure."""
    review = tmp_path / "review"
    review.mkdir()
    (review / "head-sha.txt").write_text("abc1234\n", encoding="utf-8", newline="\n")
    items = _write_jsonl(
        tmp_path / "action-items.jsonl", [_gh(1, verify_verdict="CONFIRMED", verify_file="/c/review/verify.md")]
    )
    before = items.read_bytes()

    def fail_conversion(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Model an external converter failure without changing filesystem state."""
        raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(mai.sys, "platform", "win32")
    monkeypatch.setattr(mai.subprocess, "run", fail_conversion)
    assert (
        mai.main(
            ["--items", str(items), "--recheck-verdicts", "--review-dir", str(review), "--current-head", "abc1234"]
        )
        == 1
    )
    assert items.read_bytes() == before


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

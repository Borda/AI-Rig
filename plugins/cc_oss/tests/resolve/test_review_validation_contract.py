"""Exercise versioned review publication and resolver uncertainty through real artifacts."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import append_resolution as ar
import find_review_report as frr
import merge_action_items as mai
import pytest

_BASH = shutil.which("bash")


def _publication(tmp_path: Path, outcome: str | None = "APPROVE", readiness: str = "complete") -> Path:
    """Write a complete empty version-one review with an explicitly returned scoped assessment."""
    report = tmp_path / "review-report.md"
    gate = "REVIEW_V1_REJECT_SCOPE @abcdef1234567" if outcome == "REJECT" else "REVIEW_V1_PASS"
    report.write_text(
        f"---\nTitle: Review\nPR: #7\nReview contract: 1\nRating scale: source-5-v1\n"
        f"Readiness: {readiness}\nGate: {gate}\nOutcome: {outcome or 'null'}\nSummary: Scoped judgment.\n---\n",
        encoding="utf-8",
        newline="\n",
    )
    rating = {"APPROVE": 1, "MINOR_EDITS": 2, "NEEDS_WORK": 3, "INSUFFICIENT_BUT_PROMISING": 4, "REJECT": 5}.get(
        outcome
    )
    envelope = {
        "contract_version": 1,
        "rating_scale": "source-5-v1",
        "source_outcome": outcome,
        "source_rating": rating,
        "readiness": readiness,
        "readiness_reason": "backend unavailable" if readiness == "blocked" else "checked",
        "assessments": [
            {
                "role": "QA",
                "presence": "completed",
                "source_outcome": outcome,
                "source_rating": rating,
                "readiness": readiness,
                "coverage": "src/backend.py",
                "rationale": "inspected exact source",
                "evidence": ["src/backend.py:12"],
                "limits": [],
            }
        ],
        "candidates": [],
        "open_concerns": [],
    }
    (tmp_path / "review-validation.json").write_text(json.dumps(envelope), encoding="utf-8", newline="\n")
    (tmp_path / "findings.jsonl").write_text("", encoding="utf-8", newline="\n")
    return report


@pytest.mark.parametrize(
    ("outcome", "readiness", "expected"),
    [
        pytest.param("MINOR_EDITS", "complete", "Gate: PASS", id="minor-edits"),
        pytest.param(
            "INSUFFICIENT_BUT_PROMISING", "complete", "Gate: PASS", id="good-initiative-substantially-incomplete-source"
        ),
        pytest.param("REJECT", "complete", "Gate: REJECT_SCOPE @abcdef1234567", id="new-five-is-block"),
        pytest.param(None, "blocked", "Gate: BLOCK", id="completed-undecidable-not-rejected"),
    ],
)
def test_new_publication_has_separate_source_and_readiness(
    tmp_path: Path, outcome: str | None, readiness: str, expected: str
) -> None:
    """The normal report reader accepts new meanings only with their versioned assessment evidence."""
    report = _publication(tmp_path, outcome, readiness)
    assert frr.gate_line(report) == expected


def test_missing_reviewer_is_not_completed_null(tmp_path: Path) -> None:
    """A missing response cannot satisfy a completed-but-undecidable assessment."""
    report = _publication(tmp_path, None, "blocked")
    sidecar = tmp_path / "review-validation.json"
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    data["assessments"][0]["presence"] = "missing"
    sidecar.write_text(json.dumps(data), encoding="utf-8", newline="\n")
    assert frr.gate_line(report) == ""


def test_unknown_contract_and_mismatched_header_fail_closed(tmp_path: Path) -> None:
    """Neither unsupported versions nor a favorable header hiding a different judgment can enter resolve."""
    report = _publication(tmp_path)
    sidecar = tmp_path / "review-validation.json"
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    data["contract_version"] = 2
    sidecar.write_text(json.dumps(data), encoding="utf-8", newline="\n")
    assert frr.main(["--report", str(report)]) == 1
    data["contract_version"] = 1
    data["source_outcome"] = "NEEDS_WORK"
    sidecar.write_text(json.dumps(data), encoding="utf-8", newline="\n")
    assert frr.gate_line(report) == ""


def test_unknown_finding_disposition_cannot_become_action(tmp_path: Path) -> None:
    """The actionable finding consumer rejects uncertainty even when the record has all legacy fields."""
    finding = {
        "id": "architecture-quality-abcd",
        "section": "Architecture & Quality",
        "severity": "high",
        "title": "backend question",
        "change": "code",
        "author": "QA",
        "contract_version": 1,
        "validation_status": "UNVERIFIED",
    }
    findings = tmp_path / "findings.jsonl"
    findings.write_text(json.dumps(finding) + "\n", encoding="utf-8", newline="\n")
    items = tmp_path / "items.jsonl"
    assert mai.main(["--items", str(items), "--findings", str(findings)]) == 1
    assert not items.exists()


@pytest.mark.parametrize("resolution", ["unverified", "already-fixed"])
def test_resolution_consumer_preserves_non_refutation(tmp_path: Path, resolution: str) -> None:
    """A resolver's blocked evidence or later fix stays distinct from rejected and creates no commit lookup."""
    (tmp_path / "action-items.jsonl").write_text(
        json.dumps({"id": 1, "finding_id": "finding-one"}) + "\n", encoding="utf-8", newline="\n"
    )
    (tmp_path / "selected-items.txt").write_text("1\n", encoding="utf-8", newline="\n")
    evidence = "UNVERIFIED" if resolution == "unverified" else "ALREADY_FIXED"
    (tmp_path / "challenge-log.txt").write_text(
        f"id=1 resolution={resolution} evidence={evidence} suggestion=— finding=f evidence_why=current evidence unavailable suggestion_why=— detail=owner=maintainer; next_check=backend CI; closure=exact assertion passes\n",
        encoding="utf-8",
        newline="\n",
    )
    records = ar.build_records(tmp_path, "run", tmp_path)
    assert records[0]["verdict"] == resolution
    assert records[0]["sha"] == ""
    assert "current evidence unavailable" in records[0]["why"]
    if resolution == "unverified":
        assert "next_check=backend CI" in records[0]["follow_up"]


def _candidate(disposition: str | None) -> dict:
    """Describe a source-bound backend claim without pretending simulation covers real hardware."""
    return {
        "id": "backend-one",
        "claim": "backend drops the last value",
        "revision": "abcdef1234567",
        "contract": "all input values preserved",
        "trigger": "two values on native backend",
        "counterevidence": ["generic CI does not assert native outputs"],
        "limits": ["native backend unavailable"],
        "checks": [
            {
                "method": "source inspection and faithful CPU simulation",
                "result": "represented control flow preserves values",
                "revision": "abcdef1234567",
                "environment": "CPU; native backend not represented",
                "limits": ["native allocation"],
            }
        ],
        "disposition": disposition,
        "reason": "hardware-specific assertion still required",
        "feasible_checks_complete": disposition is not None,
        "missing_capability": "native backend hardware",
    }


def test_open_concern_survives_intake_without_actionable_finding(tmp_path: Path) -> None:
    """Bound simulation can leave only backend uncertainty open; its closure handoff survives discovery."""
    report = _publication(tmp_path, None, "blocked")
    sidecar = tmp_path / "review-validation.json"
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    data["candidates"] = [_candidate("UNVERIFIED")]
    data["open_concerns"] = [
        {
            "candidate_id": "backend-one",
            "owner": "backend maintainer",
            "next_check": "run exact native assertion",
            "closure": "all values preserved on reviewed head",
        }
    ]
    sidecar.write_text(json.dumps(data), encoding="utf-8", newline="\n")
    path_out = tmp_path / "selected-report.txt"
    assert frr.main(["--report", str(report), "--path-out", str(path_out)]) == 0
    assert path_out.read_text(encoding="utf-8").strip() == report.as_posix()
    assert json.loads(sidecar.read_text(encoding="utf-8"))["open_concerns"][0]["owner"] == "backend maintainer"
    data["candidates"][0]["feasible_checks_complete"] = False
    sidecar.write_text(json.dumps(data), encoding="utf-8", newline="\n")
    assert frr.gate_line(report) == ""


def test_confirmed_candidate_cannot_disappear_or_publish_twice(tmp_path: Path) -> None:
    """Confirmed obligations and actionable records have one-to-one correspondence."""
    report = _publication(tmp_path, "NEEDS_WORK")
    sidecar = tmp_path / "review-validation.json"
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    data["candidates"] = [_candidate("CONFIRMED")]
    sidecar.write_text(json.dumps(data), encoding="utf-8", newline="\n")
    assert frr.gate_line(report) == ""
    finding = {
        "id": "finding-one",
        "candidate_id": "backend-one",
        "contract_version": 1,
        "validation_status": "CONFIRMED",
    }
    (tmp_path / "findings.jsonl").write_text(json.dumps(finding) + "\n", encoding="utf-8", newline="\n")
    assert frr.gate_line(report) == "Gate: PASS"
    (tmp_path / "findings.jsonl").write_text((json.dumps(finding) + "\n") * 2, encoding="utf-8", newline="\n")
    assert frr.gate_line(report) == ""


def test_legacy_four_keeps_insufficient_evidence_meaning(tmp_path: Path) -> None:
    """Historical four and REQUEST_CHANGES remain readable without new rejection semantics."""
    report = tmp_path / "review-report.md"
    report.write_text(
        "---\nTitle: Old\nPR: #7\nGate: BLOCK\nOutcome: REQUEST_CHANGES\nSummary: evidence incomplete\nReviewers: QA (4)\n---\nLegend: 4 = Insufficient evidence · 5 = Block / Reject.\n",
        encoding="utf-8",
        newline="\n",
    )
    assert frr.gate_line(report) == "Gate: BLOCK"
    assert "4 = Insufficient evidence" in report.read_text(encoding="utf-8")


def test_candidate_evidence_must_match_recorded_review_head(tmp_path: Path) -> None:
    """A report's frozen head cannot silently reuse candidate/check evidence from another revision."""
    report = _publication(tmp_path, "NEEDS_WORK")
    sidecar = tmp_path / "review-validation.json"
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    data["candidates"] = [_candidate("CONFIRMED")]
    sidecar.write_text(json.dumps(data), encoding="utf-8", newline="\n")
    finding = {
        "id": "finding-one",
        "candidate_id": "backend-one",
        "contract_version": 1,
        "validation_status": "CONFIRMED",
    }
    (tmp_path / "findings.jsonl").write_text(json.dumps(finding) + "\n", encoding="utf-8", newline="\n")
    (tmp_path / "head-sha.txt").write_text("different-reviewed-head\n", encoding="utf-8", newline="\n")
    assert frr.gate_line(report) == ""
    (tmp_path / "head-sha.txt").write_text("abcdef1234567\n", encoding="utf-8", newline="\n")
    assert frr.gate_line(report) == "Gate: PASS"
    data["candidates"][0]["checks"][0]["revision"] = "another-source-head"
    sidecar.write_text(json.dumps(data), encoding="utf-8", newline="\n")
    assert frr.gate_line(report) == ""


@pytest.mark.skipif(_BASH is None, reason="bash unavailable")
@pytest.mark.parametrize("pr_mode", [False, True])
def test_resolve_shell_consumers_accept_empty_confirmed_sidecar(tmp_path: Path, pr_mode: bool) -> None:
    """Validated uncertainty reaches report/PR handoff without legacy reparsing or dropping online items."""
    report = _publication(tmp_path, None, "blocked")
    validation = tmp_path / "review-validation.json"
    data = json.loads(validation.read_text(encoding="utf-8"))
    data["candidates"] = [_candidate("UNVERIFIED")]
    concern = {
        "candidate_id": "backend-one",
        "owner": "backend maintainer",
        "next_check": "native CI assertion",
        "closure": "current backend preserves all values",
    }
    data["open_concerns"] = [concern]
    validation.write_text(json.dumps(data), encoding="utf-8", newline="\n")
    assert frr.main(["--report", str(report)]) == 0
    before = validation.read_bytes()
    impl = tmp_path / "impl"
    impl.mkdir()
    online_item = {"id": 1, "type": "[gh][req]", "summary": "independent online obligation"}
    if pr_mode:
        (impl / "action-items.jsonl").write_text(json.dumps(online_item) + "\n", encoding="utf-8", newline="\n")
    session = "empty-findings"
    (tmp_path / f"resolve-impl-dir-{session}").write_text(impl.as_posix() + "\n", encoding="utf-8", newline="\n")
    (tmp_path / f"resolve-report-file-{session}").write_text(report.as_posix() + "\n", encoding="utf-8", newline="\n")
    plugin = Path(__file__).resolve().parents[2]
    source = plugin / "skills/resolve" / ("SKILL.md" if pr_mode else "modes/report-intelligence.md")
    blocks = re.findall(r"```bash\n(.*?)```", source.read_text(encoding="utf-8"), re.DOTALL)
    block = next(block for block in blocks if 'FINDINGS="$(dirname "$REPORT_FILE")/findings.jsonl"' in block)
    env = {
        **os.environ,
        "CLAUDE_PLUGIN_ROOT": plugin.as_posix(),
        "CLAUDE_CODE_SESSION_ID": session,
        "TMPDIR": tmp_path.as_posix(),
    }
    result = subprocess.run(
        [_BASH, "-c", block], env=env, encoding="utf-8", capture_output=True, check=False, timeout=15
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / f"resolve-findings-file-{session}").read_text(encoding="utf-8").strip() == (
        tmp_path / "findings.jsonl"
    ).as_posix()
    assert not (impl / "report-findings.jsonl").exists()
    if pr_mode:
        assert json.loads((impl / "action-items.jsonl").read_text(encoding="utf-8")) == online_item
    else:
        assert (impl / "action-items.jsonl").read_text(encoding="utf-8") == ""
    assert validation.read_bytes() == before
    assert json.loads(validation.read_text(encoding="utf-8"))["open_concerns"] == [concern]
    # Losing the sidecar remains a boundary failure, not permission to invent an empty collection.
    (tmp_path / "findings.jsonl").unlink()
    missing = subprocess.run(
        [_BASH, "-c", block], env=env, encoding="utf-8", capture_output=True, check=False, timeout=15
    )
    assert missing.returncode != 0
    assert "no findings.jsonl" in missing.stdout


@pytest.mark.parametrize("marker", ["Rating scale: source-5-v1", "Readiness: complete"])
def test_partial_current_header_cannot_downgrade_to_legacy(tmp_path: Path, marker: str) -> None:
    """Any current-family marker requires the supported complete publication envelope."""
    report = _publication(tmp_path)
    report.write_text(
        f"---\nTitle: Review\nPR: #7\n{marker}\nGate: PASS\nOutcome: APPROVE\nSummary: partial migration\n---\n",
        encoding="utf-8",
        newline="\n",
    )
    assert frr.main(["--report", str(report)]) == 1


@pytest.mark.parametrize("matched_online", [False, True])
def test_parent_candidate_proof_survives_normal_merge_and_recheck(tmp_path: Path, matched_online: bool) -> None:
    """Derive proof from validated publication and retain it independently of critical verification."""
    review = tmp_path / "review"
    review.mkdir()
    report = _publication(review, "NEEDS_WORK")
    sidecar = review / "review-validation.json"
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    data["candidates"] = [_candidate("CONFIRMED")]
    sidecar.write_text(json.dumps(data), encoding="utf-8", newline="\n")
    (review / "head-sha.txt").write_text("abcdef1234567\n", encoding="utf-8", newline="\n")
    finding = {
        "id": "finding-one",
        "candidate_id": "backend-one",
        "contract_version": 1,
        "validation_status": "CONFIRMED",
        "section": "Architecture & Quality",
        "severity": "medium",
        "title": "backend drops value",
        "change": "code",
        "author": "QA",
        "file": "src/backend.py",
        "line": 12,
    }
    findings = review / "findings.jsonl"
    findings.write_text(json.dumps(finding) + "\n", encoding="utf-8", newline="\n")
    items = tmp_path / "items.jsonl"
    if matched_online:
        items.write_text(
            json.dumps({"id": 1, "file": "src/backend.py", "line": 12, "type": "[gh][req]"}) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    assert frr.gate_line(report) == "Gate: PASS"
    assert mai.main(["--items", str(items), "--findings", str(findings)]) == 0
    item = json.loads(items.read_text(encoding="utf-8"))
    assert item["parent_validation_file"] == sidecar.resolve().as_posix()
    assert item["candidate_id"] == "backend-one"
    assert item["validation_status"] == "CONFIRMED"
    assert "verify_verdict" not in item
    argv = ["--items", str(items), "--recheck-verdicts", "--review-dir", str(review), "--current-head"]
    assert mai.main([*argv, "abcdef1234567"]) == 0
    assert json.loads(items.read_text(encoding="utf-8"))["validation_status"] == "CONFIRMED"
    assert mai.main([*argv, "bbbbbbb1234567"]) == 0
    stale = json.loads(items.read_text(encoding="utf-8"))
    assert "validation_status" not in stale
    assert stale["parent_validation_file"] == item["parent_validation_file"]
    assert stale["candidate_id"] == "backend-one"


@pytest.mark.parametrize(
    "damage", ["missing-sidecar", "tampered-check", "missing-candidate", "wrong-item-candidate", "legacy-header"]
)
def test_parent_proof_recheck_disables_shortcut_but_retains_pointer(tmp_path: Path, damage: str) -> None:
    """A changed proof cannot remain reusable merely because the source head still matches."""
    report = _publication(tmp_path, "NEEDS_WORK")
    sidecar = tmp_path / "review-validation.json"
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    data["candidates"] = [_candidate("CONFIRMED")]
    sidecar.write_text(json.dumps(data), encoding="utf-8", newline="\n")
    (tmp_path / "head-sha.txt").write_text("abcdef1234567\n", encoding="utf-8", newline="\n")
    findings = tmp_path / "findings.jsonl"
    findings.write_text(
        json.dumps(
            {
                "id": "finding-one",
                "candidate_id": "backend-one",
                "contract_version": 1,
                "validation_status": "CONFIRMED",
                "section": "Architecture & Quality",
                "severity": "medium",
                "title": "backend drops value",
                "change": "code",
                "author": "QA",
            }
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    items = tmp_path / "impl" / "items.jsonl"
    assert mai.main(["--items", str(items), "--findings", str(findings)]) == 0
    original = json.loads(items.read_text(encoding="utf-8"))
    if damage == "missing-sidecar":
        sidecar.unlink()
    elif damage == "tampered-check":
        data["candidates"][0]["checks"][0]["result"] = "different evidence"
        sidecar.write_text(json.dumps(data), encoding="utf-8", newline="\n")
    elif damage == "missing-candidate":
        data["candidates"] = []
        sidecar.write_text(json.dumps(data), encoding="utf-8", newline="\n")
    elif damage == "wrong-item-candidate":
        original["candidate_id"] = "different-candidate"
        items.write_text(json.dumps(original) + "\n", encoding="utf-8", newline="\n")
    else:
        report.write_text(
            "---\nTitle: Archive\nPR: #7\nGate: PASS\nOutcome: APPROVE\nSummary: historical\n---\n",
            encoding="utf-8",
            newline="\n",
        )
    assert (
        mai.main(
            [
                "--items",
                str(items),
                "--recheck-verdicts",
                "--review-dir",
                tmp_path.as_posix(),
                "--current-head",
                "abcdef1234567",
            ]
        )
        == 0
    )
    stale = json.loads(items.read_text(encoding="utf-8"))
    assert "validation_status" not in stale
    assert stale["parent_validation_file"] == original["parent_validation_file"]
    assert "verify_verdict" not in stale


def test_late_rejection_routes_to_existing_ground_publisher() -> None:
    """The two normal consumers agree that late rejection cannot publish with literal PASS/BLOCK."""
    plugin = Path(__file__).resolve().parents[2]
    skill = (plugin / "skills/review/SKILL.md").read_text(encoding="utf-8")
    template = (plugin / "skills/review/templates/consolidator-prompt.md").read_text(encoding="utf-8")
    route = "terminal-ground=<GROUND> | evidence=<path>"
    assert route in template
    assert route in skill
    assert "publish nothing" in template
    assert "Stage 1's existing confirmed-ground block and terminal publisher" in skill
    assert "Retain the original reviewed SHA, exact ground/reason and provenance" in skill
    assert "including its conduct/license confirmation safeguards" in skill
    dispatch = (plugin / "skills/resolve/modes/action-item-dispatch.md").read_text(encoding="utf-8")
    assert "reuse applicable proved checks without rerunning them" in dispatch
    assert "This parent proof never supplies the independent [confirmed] verifier marker" in dispatch
    assert "Part 2 — for EVERY item whose problem exists" in dispatch


def test_string_assessment_evidence_is_equivalent_to_a_one_item_list(tmp_path: Path) -> None:
    """A reviewer that wrote evidence and limits as plain strings still publishes a readable gate.

    The type differs, not the meaning, so the intake gate must not stop an otherwise valid envelope on it; the producer
    contract still asks for arrays.
    """
    report = _publication(tmp_path, "NEEDS_WORK")
    sidecar = tmp_path / "review-validation.json"
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    data["assessments"][0]["evidence"] = "src/backend.py:12"
    data["assessments"][0]["limits"] = "none"
    sidecar.write_text(json.dumps(data), encoding="utf-8", newline="\n")
    assert frr.gate_line(report) == "Gate: PASS"


def test_open_concern_for_a_confirmed_candidate_is_rejected(tmp_path: Path) -> None:
    """open_concerns lists only UNVERIFIED or pending candidates, never one already confirmed."""
    report = _publication(tmp_path, "NEEDS_WORK")
    sidecar = tmp_path / "review-validation.json"
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    data["candidates"] = [_candidate("CONFIRMED")]
    data["open_concerns"] = [
        {"candidate_id": "backend-one", "owner": "maintainer", "next_check": "CI", "closure": "CI passes"}
    ]
    sidecar.write_text(json.dumps(data), encoding="utf-8", newline="\n")
    finding = {"id": "f", "candidate_id": "backend-one", "contract_version": 1, "validation_status": "CONFIRMED"}
    (tmp_path / "findings.jsonl").write_text(json.dumps(finding) + "\n", encoding="utf-8", newline="\n")
    assert frr.gate_line(report) == ""


def test_complete_readiness_with_an_unverified_candidate_is_rejected(tmp_path: Path) -> None:
    """An open UNVERIFIED candidate forbids aggregate readiness complete."""
    report = _publication(tmp_path, "NEEDS_WORK")
    sidecar = tmp_path / "review-validation.json"
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    data["candidates"] = [_candidate("UNVERIFIED")]
    data["open_concerns"] = [
        {"candidate_id": "backend-one", "owner": "maintainer", "next_check": "CI", "closure": "CI passes"}
    ]
    sidecar.write_text(json.dumps(data), encoding="utf-8", newline="\n")
    assert frr.gate_line(report) == ""

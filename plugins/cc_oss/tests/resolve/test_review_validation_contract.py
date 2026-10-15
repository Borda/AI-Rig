"""Exercise versioned review publication and resolver uncertainty through real artifacts."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
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


def _open_unverified_under_complete(data: dict) -> None:
    """Leave one UNVERIFIED candidate open while the aggregate still claims completion."""
    data["candidates"] = [_candidate("UNVERIFIED")]
    data["open_concerns"] = [
        {"candidate_id": "backend-one", "owner": "maintainer", "next_check": "native run", "closure": "assert"}
    ]


def _blocked_assessment_under_complete(data: dict) -> None:
    """Report one reviewer as blocked while the aggregate still claims completion."""
    data["assessments"][0]["readiness"] = "blocked"


def _confirmed_without_finding_row(data: dict) -> None:
    """Confirm a candidate without publishing its actionable findings.jsonl row."""
    data["candidates"] = [_candidate("CONFIRMED")]


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        pytest.param(
            _open_unverified_under_complete,
            "open candidate 'backend-one' (UNVERIFIED) under readiness=complete",
            id="open-concern-under-complete",
        ),
        pytest.param(
            _blocked_assessment_under_complete,
            "assessment 'QA' readiness=blocked under aggregate readiness=complete",
            id="blocked-assessment-under-complete",
        ),
        pytest.param(
            _confirmed_without_finding_row,
            "CONFIRMED candidate(s) without findings.jsonl row: backend-one",
            id="confirmed-without-finding-row",
        ),
    ],
)
def test_rejection_names_the_broken_rule(tmp_path: Path, capsys: pytest.CaptureFixture, mutate, reason: str) -> None:
    """An inconsistent envelope is refused with the exact rule it breaks, not a bare incomplete verdict.

    Each case is one of the producer slips seen in a real rejected publication: open concerns, a blocked reviewer,
    or a confirmed candidate missing its finding row, all under ``Readiness: complete``. Without the reason, the
    producer's retry and the user both have to re-derive the cause by calling the private checks by hand.
    """
    report = _publication(tmp_path, "NEEDS_WORK")
    sidecar = tmp_path / "review-validation.json"
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    mutate(data)
    sidecar.write_text(json.dumps(data), encoding="utf-8", newline="\n")
    assert frr.main(["--report", str(report)]) == 1
    assert f"[gate] reason: {reason}" in capsys.readouterr().out


def _review_publication_gate() -> str:
    """Return review Step 5's publication-gate recipe, which must run before the Step 5b header print."""
    skill = Path(__file__).resolve().parents[2] / "skills/review/SKILL.md"
    step5 = skill.read_text(encoding="utf-8").split("## Step 5: Consolidate findings", 1)[1]
    step5 = step5.split('TaskUpdate "Step 5b: Print report header" → `in_progress`', 1)[0]
    blocks = re.findall(r"```bash\n(.*?)```", step5, re.DOTALL)
    return next(block for block in blocks if '--report "$_REPORT_DIR/review-report.md"' in block)


def _run_review_publication_gate(report_dir: Path) -> subprocess.CompletedProcess:
    """Run the shipped Step 5 publication-gate block on the report in ``report_dir``, which is also the TMPDIR."""
    session = "publication-gate"
    (report_dir / f"oss-review-report-dir-{session}").write_text(
        report_dir.as_posix() + "\n", encoding="utf-8", newline="\n"
    )
    env = {
        **os.environ,
        "CLAUDE_PLUGIN_ROOT": Path(__file__).resolve().parents[2].as_posix(),
        "CLAUDE_CODE_SESSION_ID": session,
        "TMPDIR": report_dir.as_posix(),
    }
    return subprocess.run(
        [_BASH, "-c", _review_publication_gate()],
        env=env,
        encoding="utf-8",
        capture_output=True,
        check=False,
        timeout=15,
    )


@pytest.mark.skipif(_BASH is None, reason="bash unavailable")
@pytest.mark.parametrize(
    ("mutate", "returncode", "expected"),
    [
        pytest.param(None, 0, "[gate] Gate: PASS", id="consistent-publication-ships"),
        pytest.param(
            _open_unverified_under_complete, 1, "[gate] reason: open candidate", id="resolve-rejectable-stops"
        ),
    ],
)
def test_review_gates_its_own_publication(tmp_path: Path, mutate, returncode: int, expected: str) -> None:
    """Oss:review runs resolve's intake check on the consolidator's output before printing it as finished.

    Regression: the producer published ``Readiness: complete`` with open UNVERIFIED concerns, and only
    ``/oss:resolve`` noticed. The recipe here is the shipped Step 5 block, run against the real validator.
    """
    report = _publication(tmp_path, "NEEDS_WORK")
    sidecar = tmp_path / "review-validation.json"
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    (mutate or (lambda _data: None))(data)
    sidecar.write_text(json.dumps(data), encoding="utf-8", newline="\n")
    result = _run_review_publication_gate(tmp_path)
    assert result.returncode == returncode, result.stdout + result.stderr
    assert expected in result.stdout
    assert report.exists()


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


def _named_candidate(identifier: str, disposition: str | None) -> dict:
    """Clone the evidence-bound candidate under another id."""
    return {**_candidate(disposition), "id": identifier}


def _assessment(role: str, readiness: str) -> dict:
    """Describe one completed reviewer with a NEEDS_WORK judgment and the given readiness."""
    return {
        "role": role,
        "presence": "completed",
        "source_outcome": "NEEDS_WORK",
        "source_rating": 3,
        "readiness": readiness,
        "coverage": "src/",
        "rationale": "inspected",
        "evidence": ["src/a.py:1"],
        "limits": [],
    }


def _write_envelope(tmp_path: Path, data: dict, findings: list[dict]) -> None:
    """Persist a mutated envelope and its actionable rows beside the report."""
    (tmp_path / "review-validation.json").write_text(json.dumps(data), encoding="utf-8", newline="\n")
    rows = "".join(json.dumps(row) + "\n" for row in findings)
    (tmp_path / "findings.jsonl").write_text(rows, encoding="utf-8", newline="\n")


def _finding_row(candidate_id: str) -> dict:
    """Publish one confirmed candidate as an actionable row carrying every field resolve's merge requires."""
    return {
        "id": f"f-{candidate_id}",
        "candidate_id": candidate_id,
        "contract_version": 1,
        "validation_status": "CONFIRMED",
        "section": "Architecture & Quality",
        "severity": "medium",
        "title": "backend drops value",
        "change": "code",
        "author": "QA",
    }


def _concern(candidate_id: str) -> dict:
    """Carry one open candidate as a durable concern."""
    return {"candidate_id": candidate_id, "owner": "maintainer", "next_check": "native run", "closure": "assert passes"}


@pytest.fixture(name="pr8_like")
def _pr8_like(tmp_path: Path) -> Path:
    """Reproduce the rejected PR 8 publication shape under a claimed-complete header.

    Three UNVERIFIED concerns stay open, four reviewers are blocked and one is incomplete, and one CONFIRMED candidate
    (V1b) never got its findings.jsonl row while another (V2) did.
    """
    report = _publication(tmp_path, "NEEDS_WORK")
    data = json.loads((tmp_path / "review-validation.json").read_text(encoding="utf-8"))
    data["assessments"] = [
        _assessment("Software engineer", "blocked"),
        _assessment("QA specialist", "blocked"),
        _assessment("Performance engineer", "blocked"),
        _assessment("Challenger", "blocked"),
        _assessment("Codex", "incomplete"),
    ]
    open_ids = ["V13", "V20", "V21"]
    data["candidates"] = [
        *(_named_candidate(cid, "UNVERIFIED") for cid in open_ids),
        _named_candidate("V1b", "CONFIRMED"),
        _named_candidate("V2", "CONFIRMED"),
    ]
    data["open_concerns"] = [_concern(cid) for cid in open_ids]
    _write_envelope(tmp_path, data, [_finding_row("V2")])
    return report


def test_assessment_grades_every_pr8_gap(pr8_like: Path) -> None:
    """All gaps are reported at once, the lost obligation and blocked QA as critical, the rest as degraded.

    The strict gate stops at the first broken rule; triage needs the whole picture to offer a rerun scope covering
    exactly the blocked reviewers and open candidates.
    """
    gaps = {(gap.rule, gap.severity.value, gap.subject) for gap in frr.assess_report(pr8_like).gaps}
    assert gaps == {
        ("assessment-not-complete", "degraded", "Software engineer"),
        ("assessment-not-complete", "critical", "QA specialist"),
        ("assessment-not-complete", "degraded", "Performance engineer"),
        ("assessment-not-complete", "degraded", "Challenger"),
        ("assessment-not-complete", "degraded", "Codex"),
        ("readiness-mislabel", "degraded", "V13"),
        ("readiness-mislabel", "degraded", "V20"),
        ("readiness-mislabel", "degraded", "V21"),
        ("finding-missing", "critical", "V1b"),
    }


def test_degraded_only_report_stays_consumable_under_block(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """A wrong readiness label alone leaves the confirmed findings usable, under a BLOCK gate.

    The open concern is intact and the QA reviewer finished, so nothing actionable is lost; resolve may offer to proceed
    with the valid parts. The path is published so that choice needs no second lookup.
    """
    report = _publication(tmp_path, "NEEDS_WORK")
    data = json.loads((tmp_path / "review-validation.json").read_text(encoding="utf-8"))
    data["candidates"] = [_candidate("UNVERIFIED")]
    data["open_concerns"] = [_concern("backend-one")]
    _write_envelope(tmp_path, data, [])
    path_out = tmp_path / "selected.txt"
    assert frr.main(["--report", str(report), "--assess", "--path-out", str(path_out)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert (payload["gate"], payload["critical"], payload["degraded"]) == ("Gate: BLOCK", 0, 1)
    assert path_out.read_text(encoding="utf-8").strip() == report.as_posix()


def test_critical_report_is_not_consumable(pr8_like: Path, capsys: pytest.CaptureFixture) -> None:
    """A lost obligation or blocked security review forbids consuming any part, and publishes no path.

    Exit 2 is distinct from 1 so the resolve recipe can tell "assessed, not consumable" from a broken lookup.
    """
    path_out = pr8_like.with_name("selected.txt")
    assert frr.main(["--report", str(pr8_like), "--assess", "--path-out", str(path_out)]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert (payload["gate"], payload["critical"]) == ("", 2)
    assert path_out.read_text(encoding="utf-8") == ""


@pytest.mark.parametrize(
    ("outcome", "readiness", "contract", "gaps"),
    [
        pytest.param("NEEDS_WORK", "complete", "v1", [], id="valid-v1-no-gaps"),
        pytest.param(None, "blocked", "v1", [], id="valid-undecidable-no-gaps"),
    ],
)
def test_valid_publication_has_no_gaps(
    tmp_path: Path, outcome: str | None, readiness: str, contract: str, gaps: list
) -> None:
    """A report the strict gate accepts yields no gaps and keeps its normal gate line."""
    report = _publication(tmp_path, outcome, readiness)
    assessment = frr.assess_report(report)
    assert (assessment.contract, assessment.gaps, assessment.gate) == (contract, gaps, frr.gate_line(report))


def test_legacy_report_is_one_degraded_gap(tmp_path: Path) -> None:
    """A pre-contract report is readable but carries no candidate, coverage or concern data to consume."""
    report = tmp_path / "review-report.md"
    report.write_text(
        "---\nTitle: Old\nPR: #7\nGate: BLOCK\nOutcome: REQUEST_CHANGES\nSummary: old\n---\n",
        encoding="utf-8",
        newline="\n",
    )
    assessment = frr.assess_report(report)
    assert (assessment.contract, assessment.gate) == ("legacy", "Gate: BLOCK")
    assert [(gap.rule, gap.severity.value) for gap in assessment.gaps] == [("legacy-contract", "degraded")]


@pytest.mark.parametrize("gate", ["REJECTED", "reject_scope"])
def test_unrecognized_legacy_gate_is_critical(tmp_path: Path, capsys: pytest.CaptureFixture, gate: str) -> None:
    """A legacy gate line resolve cannot act on is an ambiguous verdict, never a consumable legacy report.

    Intake reaches ``--assess`` exactly when the reject gate refused that line, so grading it as the usual degraded
    legacy gap would offer Proceed on a misspelled rejection.
    """
    report = tmp_path / "review-report.md"
    report.write_text(
        f"---\nTitle: Old\nPR: #7\nGate: {gate}\nOutcome: NEEDS_WORK\nSummary: old\n---\n",
        encoding="utf-8",
        newline="\n",
    )
    assert frr.main(["--report", str(report), "--assess"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert (payload["gate"], [(gap["rule"], gap["severity"]) for gap in payload["gaps"]]) == (
        "",
        [("legacy-gate-ambiguous", "critical")],
    )


Mutation = Callable[[dict, list], None]


def _set(path: tuple, value: object) -> Mutation:
    """Return a mutation that overwrites one nested envelope field, the way the mutation fuzz damages a sidecar."""

    def mutate(data: dict, _rows: list) -> None:
        target = data
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return mutate


def _blocked_int_role(data: dict, _rows: list) -> None:
    """Rename the only reviewer to a number while it reports blocked under a claimed-complete review."""
    data["assessments"][0].update(role=7, readiness="blocked")


def _missing_with_judgment(data: dict, _rows: list) -> None:
    """Record a reviewer whose output is missing yet who still carries a source judgment."""
    data["assessments"].append({**_assessment("Performance engineer", "complete"), "presence": "missing"})


def _list_finding_candidate(_data: dict, rows: list) -> None:
    """Publish an extra actionable row whose candidate_id is a list."""
    rows.append({**_finding_row("V9"), "candidate_id": ["V9"]})


def _open_concern_under_complete(data: dict, _rows: list) -> None:
    """Leave one UNVERIFIED candidate open, with its concern, while the aggregate claims completion."""
    data["candidates"].append(_named_candidate("open-one", "UNVERIFIED"))
    data["open_concerns"].append(_concern("open-one"))


def _label_slip_row_without_change(data: dict, rows: list) -> None:
    """Add a label slip and drop the ``change`` field resolve's merge needs from the only findings row."""
    _open_concern_under_complete(data, rows)
    del rows[0]["change"]


def _label_slip_row_unknown_severity(data: dict, rows: list) -> None:
    """Add a label slip and give the only findings row a severity outside the merge's vocabulary."""
    _open_concern_under_complete(data, rows)
    rows[0]["severity"] = "major"


def _label_slip_rows_sharing_an_id(data: dict, rows: list) -> None:
    """Add a label slip and a second confirmed candidate whose findings row reuses the first row's id."""
    _open_concern_under_complete(data, rows)
    data["candidates"].append(_named_candidate("second-one", "CONFIRMED"))
    rows.append({**_finding_row("second-one"), "id": rows[0]["id"]})


def _label_slip_unattributed_row_without_change(data: dict, rows: list) -> None:
    """Add a label slip and a row lacking ``change`` whose candidate_id names no recorded candidate."""
    _open_concern_under_complete(data, rows)
    rows.append({key: value for key, value in _finding_row("ghost").items() if key != "change"})


@pytest.fixture(name="graded_publication")
def _graded_publication(tmp_path: Path) -> Path:
    """Publish a strictly valid review with one confirmed (published) and one refuted candidate to damage."""
    report = _publication(tmp_path, "NEEDS_WORK")
    data = json.loads((tmp_path / "review-validation.json").read_text(encoding="utf-8"))
    data["candidates"] = [_named_candidate("backend-one", "CONFIRMED"), _named_candidate("refuted-one", "REFUTED")]
    _write_envelope(tmp_path, data, [_finding_row("backend-one")])
    assert frr.gate_line(report) == "Gate: PASS"
    return report


@pytest.mark.parametrize(
    ("mutate", "gap", "returncode"),
    [
        pytest.param(
            _set(("assessments", 0, "evidence"), []), ("assessment-unbacked", "critical", "QA"), 2, id="qa-no-evidence"
        ),
        pytest.param(
            _set(("assessments", 0, "coverage"), ""), ("assessment-unbacked", "critical", "QA"), 2, id="qa-no-coverage"
        ),
        pytest.param(
            _set(("assessments", 0, "limits"), 5), ("assessment-unbacked", "critical", "QA"), 2, id="limits-not-list"
        ),
        pytest.param(
            _set(("assessments", 0, "source_rating"), 4),
            ("assessment-malformed", "critical", "QA"),
            2,
            id="bad-rating-keeps-role-subject",
        ),
        pytest.param(
            _missing_with_judgment,
            ("assessment-malformed", "critical", "Performance engineer"),
            2,
            id="missing-with-judgment",
        ),
        pytest.param(_blocked_int_role, ("assessment-malformed", "critical", "-"), 2, id="int-role-blocked"),
        pytest.param(
            _set(("assessments", 0, "role"), {"name": "QA"}),
            ("assessment-malformed", "critical", "-"),
            2,
            id="mapping-role-otherwise-valid",
        ),
        pytest.param(
            _set(("candidates", 1, "id"), 5), ("candidate-invalid", "critical", "-"), 2, id="int-candidate-id"
        ),
        pytest.param(
            _set(("candidates", 1, "id"), True), ("candidate-invalid", "critical", "-"), 2, id="bool-candidate-id"
        ),
        pytest.param(
            _set(("candidates", 1, "id"), {"a": 1}), ("candidate-invalid", "critical", "-"), 2, id="dict-candidate-id"
        ),
        pytest.param(
            _set(("candidates", 1, "disposition"), ["X"]),
            ("sidecar-malformed", "critical", "-"),
            2,
            id="unhashable-disposition",
        ),
        pytest.param(
            _set(("source_outcome",), ["X"]), ("sidecar-malformed", "critical", "-"), 2, id="list-aggregate-outcome"
        ),
        pytest.param(
            _set(("open_concerns",), [{**_concern("backend-one"), "candidate_id": ["backend-one"]}]),
            ("concern-unknown-candidate", "critical", "['backend-one']"),
            2,
            id="list-concern-candidate-id",
        ),
        pytest.param(
            _list_finding_candidate, ("finding-unbacked", "critical", "['V9']"), 2, id="list-finding-candidate-id"
        ),
        pytest.param(
            _open_concern_under_complete,
            ("readiness-mislabel", "degraded", "open-one"),
            0,
            id="label-only-stays-consumable",
        ),
        pytest.param(
            _label_slip_row_without_change,
            ("finding-unmergeable", "critical", "backend-one"),
            2,
            id="label-slip-row-without-change",
        ),
        pytest.param(
            _label_slip_row_unknown_severity,
            ("finding-unmergeable", "critical", "backend-one"),
            2,
            id="label-slip-unknown-severity",
        ),
        pytest.param(
            _label_slip_rows_sharing_an_id,
            ("finding-unmergeable", "critical", "second-one"),
            2,
            id="label-slip-repeated-finding-id",
        ),
        pytest.param(
            _label_slip_unattributed_row_without_change,
            ("finding-unmergeable", "critical", "-"),
            2,
            id="unattributed-unmergeable-row",
        ),
    ],
)
def test_every_strict_rejection_grades_at_least_one_gap(
    graded_publication: Path, capsys: pytest.CaptureFixture, mutate: Mutation, gap: tuple, returncode: int
) -> None:
    """Each damage the strict gate refuses is graded as a named gap through ``--assess``, never zero gaps or a crash.

    Regression for the mutation fuzz: unbacked QA sign-off and non-string candidate ids graded zero gaps and became
    consumable, and unhashable or non-string values crashed the grader with a traceback instead of an exit-2 JSON line.
    A record whose role is still a name keeps that role as its subject, so triage can name it in a rerun.
    """
    run = graded_publication.parent
    data = json.loads((run / "review-validation.json").read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in (run / "findings.jsonl").read_text(encoding="utf-8").splitlines()]
    mutate(data, rows)
    _write_envelope(run, data, rows)
    assert frr.gate_line(graded_publication) == ""
    assert frr.main(["--report", str(graded_publication), "--assess"]) == returncode
    payload = json.loads(capsys.readouterr().out)
    assert gap in [(entry["rule"], entry["severity"], entry["subject"]) for entry in payload["gaps"]]


def test_unmergeable_row_still_backs_its_candidate(graded_publication: Path) -> None:
    """A confirmed row the merge would refuse grades one critical gap naming its candidate, and no missing-row gap.

    Regression: a label-slip report whose confirmed row lacked ``change`` graded degraded-only, so resolve offered
    Proceed and its merge then blocked on the row. Naming the candidate lets a targeted rerun re-validate it, which
    replaces the row; a second ``finding-missing`` gap would instead route the candidate to a verbatim row publish.
    """
    run = graded_publication.parent
    data = json.loads((run / "review-validation.json").read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in (run / "findings.jsonl").read_text(encoding="utf-8").splitlines()]
    _label_slip_row_without_change(data, rows)
    _write_envelope(run, data, rows)
    gaps = [(gap.rule, gap.severity.value, gap.subject) for gap in frr.assess_report(graded_publication).gaps]
    assert gaps == [("readiness-mislabel", "degraded", "open-one"), ("finding-unmergeable", "critical", "backend-one")]


def _nest(depth: int) -> object:
    """Return a string wrapped in ``depth`` single-item lists."""
    value: object = "deep"
    for _ in range(depth):
        value = [value]
    return value


def test_deeply_nested_evidence_grades_without_a_traceback(
    graded_publication: Path, capsys: pytest.CaptureFixture
) -> None:
    """A label-slip report whose evidence nests 600 lists deep is graded like any label slip, not crashed on.

    The residual check used to deep-copy the envelope and overflow the interpreter stack at this depth, printing a
    traceback where resolve expected one JSON line; it now copies only the levels it corrects.
    """
    run = graded_publication.parent
    data = json.loads((run / "review-validation.json").read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in (run / "findings.jsonl").read_text(encoding="utf-8").splitlines()]
    _open_concern_under_complete(data, rows)
    data["candidates"][0]["counterevidence"] = [_nest(600)]
    _write_envelope(run, data, rows)
    assert frr.main(["--report", str(graded_publication), "--assess"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [(gap["rule"], gap["subject"]) for gap in payload["gaps"]] == [("readiness-mislabel", "open-one")]


def test_label_correction_leaves_the_envelope_untouched() -> None:
    """The residual check corrects labels on a copy; the graded envelope keeps every original label and record."""
    data = {
        "readiness": "complete",
        "assessments": [{"role": "QA", "presence": "completed", "source_outcome": None, "readiness": "complete"}],
        "candidates": [{"id": "c1", "disposition": "UNVERIFIED", "counterevidence": [["kept"]]}],
        "open_concerns": [],
    }
    original = json.loads(json.dumps(data))
    corrected = frr._label_corrected(data, "blocked")
    assert data == original
    assert (corrected["readiness"], corrected["assessments"][0]["readiness"]) == ("blocked", "blocked")
    assert corrected["candidates"][0]["missing_capability"]
    assert corrected["candidates"][0]["counterevidence"] is data["candidates"][0]["counterevidence"]


@pytest.mark.parametrize(
    ("outcome", "recorded"),
    [
        pytest.param("REJECT", "REVIEW_V1_REJECT_SCOPE @abcdef1234567", id="rejection-under-critical-gap"),
        pytest.param("NEEDS_WORK", "", id="no-rejection"),
    ],
)
def test_assess_echoes_a_recorded_rejection_whatever_the_gaps(
    tmp_path: Path, capsys: pytest.CaptureFixture, outcome: str, recorded: str
) -> None:
    """``recorded_rejection`` carries the header's ``REJECT_`` gate even when a critical gap empties ``gate``.

    Resolve detected a rejection only through ``gate``, which stays empty under any critical gap (here the only
    reviewer, QA, blocked under a claimed-complete review), so a rejected PR was offered reruns without ever being told
    it was rejected. The exit code stays that of the graded gaps.
    """
    report = _publication(tmp_path, outcome)
    sidecar = tmp_path / "review-validation.json"
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    data["assessments"][0]["readiness"] = "blocked"
    sidecar.write_text(json.dumps(data), encoding="utf-8", newline="\n")
    assert frr.main(["--report", str(report), "--assess"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert (payload["gate"], payload["recorded_rejection"]) == ("", recorded)


def _judged_reviewer(data: dict) -> None:
    """Let the only reviewer judge NEEDS_WORK while the aggregate outcome stays null."""
    data["assessments"][0].update(source_outcome="NEEDS_WORK", source_rating=3)


def _missing_reviewer(data: dict) -> None:
    """Record the only reviewer's output as missing, so no reviewer supports any aggregate outcome."""
    data["assessments"][0].update(presence="missing", readiness="incomplete")


@pytest.mark.parametrize(
    ("readiness", "mutate", "gaps", "gate"),
    [
        pytest.param(
            "blocked",
            _judged_reviewer,
            [("assessment-not-complete", "degraded", "QA"), ("readiness-mislabel", "degraded", "-")],
            "Gate: BLOCK",
            id="aggregate-outcome-slip-is-a-label",
        ),
        pytest.param(
            "incomplete",
            _missing_reviewer,
            [
                ("assessment-missing", "degraded", "QA"),
                ("readiness-mislabel", "degraded", "-"),
                ("ungraded-rule", "critical", "-"),
            ],
            "",
            id="no-reviewer-supports-any-outcome",
        ),
    ],
)
def test_null_aggregate_outcome_grades_by_what_the_reviewers_support(
    tmp_path: Path, readiness: str, mutate: Callable[[dict], None], gaps: list, gate: str
) -> None:
    """A null aggregate outcome a reviewer's judgment can correct is a label; one no reviewer supports stays critical.

    Regression: a null aggregate over a reviewer who judged graded a degraded ``readiness-mislabel`` plus a critical
    ``ungraded-rule`` twin for the same rule, because the residual check never relabelled the aggregate outcome, so
    resolve refused to proceed with intact findings. With every reviewer missing, no outcome is supportable and no label
    correction clears the refusal, so the report must never become consumable.
    """
    report = _publication(tmp_path, None, readiness)
    data = json.loads((tmp_path / "review-validation.json").read_text(encoding="utf-8"))
    mutate(data)
    _write_envelope(tmp_path, data, [])
    assessment = frr.assess_report(report)
    assert frr.gate_line(report) == ""
    assert ([(g.rule, g.severity.value, g.subject) for g in assessment.gaps], assessment.gate) == (gaps, gate)


@pytest.mark.parametrize("role", ["Security auditor", "Test engineer", "QA specialist"])
def test_blocked_security_reviewer_is_critical_under_any_qa_name(tmp_path: Path, role: str) -> None:
    """Every role name fill-gaps routes to the QA unit is the security reviewer the grader holds critical.

    Role names are free text; a blocked "Security auditor" under a claimed-complete review used to grade degraded and
    offer Proceed, while fill-gaps already re-ran it as the QA specialist.
    """
    report = _publication(tmp_path, "NEEDS_WORK")
    data = json.loads((tmp_path / "review-validation.json").read_text(encoding="utf-8"))
    data["assessments"] = [_assessment(role, "blocked")]
    _write_envelope(tmp_path, data, [])
    gaps = [(gap.rule, gap.severity.value, gap.subject) for gap in frr.assess_report(report).gaps]
    assert gaps == [("assessment-not-complete", "critical", role)]


@pytest.mark.parametrize(
    ("head_file", "recorded", "current", "head_moved", "calls"),
    [
        pytest.param(
            "head-sha.txt", "abcdef1234567", "abcdef1234567" + "0" * 27, False, ["7"], id="same-head-abbreviated"
        ),
        pytest.param("head-sha.txt", "abcdef1234567", "1234567abcdef" + "0" * 27, True, ["7"], id="head-moved"),
        pytest.param("head-sha.txt", "abcdef1234567", "", None, ["7"], id="head-unfetchable"),
        pytest.param("unrelated.txt", "abcdef1234567", "abcdef1234567", None, [], id="no-head-file-no-gh-call"),
        pytest.param("head-sha.txt", "not-a-sha", "abcdef1234567", None, [], id="garbage-head-file-no-gh-call"),
    ],
)
def test_assess_reports_whether_the_head_moved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    head_file: str,
    recorded: str,
    current: str,
    head_moved: bool | None,
    calls: list[str],
) -> None:
    """``--assess`` adds ``head_moved`` so triage never recommends a fill-gaps rerun that would refuse a moved head.

    The PR number comes from the report header when ``--pr`` is absent; ``gh`` is asked only when the run recorded a
    commit id, and a prefix match tolerates the abbreviated id. The exit code stays that of the graded gaps.
    """
    report = _publication(tmp_path, "NEEDS_WORK")
    (tmp_path / head_file).write_text(f"{recorded}\n", encoding="utf-8", newline="\n")
    asked: list[str] = []
    monkeypatch.setattr(frr, "current_head_sha", lambda pr, _timeout: asked.append(pr) or current)
    assert frr.main(["--report", str(report), "--assess"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert (payload["head_moved"], payload["critical"], asked) == (head_moved, 0, calls)


#: A completed reviewer whose output never arrived, as the protocol records it.
_MISSING_REVIEWER: dict = {
    "role": "Performance engineer",
    "presence": "missing",
    "source_outcome": None,
    "source_rating": None,
    "readiness": "incomplete",
    "coverage": "owed: hot path",
    "rationale": "",
    "evidence": [],
    "limits": ["no output"],
}


@pytest.mark.parametrize(
    ("readiness", "extra", "gate", "reason"),
    [
        pytest.param("incomplete", [_MISSING_REVIEWER], "Gate: BLOCK", "", id="missing-reviewer-makes-null-incomplete"),
        pytest.param("blocked", [], "Gate: BLOCK", "", id="completed-undecidable-stays-blocked"),
        pytest.param(
            "blocked",
            [_MISSING_REVIEWER],
            "",
            "missing assessment 'Performance engineer' requires null judgment and incomplete readiness",
            id="missing-reviewer-under-blocked",
        ),
        pytest.param(
            "incomplete",
            [],
            "",
            "null aggregate outcome requires a completed null assessment in every case",
            id="incomplete-without-missing-reviewer",
        ),
    ],
)
def test_missing_reviewer_outranks_blocked_under_a_null_outcome(
    tmp_path: Path, readiness: str, extra: list[dict], gate: str, reason: str
) -> None:
    """A null aggregate with a missing reviewer publishes as ``incomplete``, and the grader agrees with the gate.

    A null aggregate needs ``blocked`` readiness and a missing reviewer needs ``incomplete``; with both present no label
    was valid, so the review always stopped BLOCKED after its one re-dispatch. A missing required reviewer now outranks
    ``blocked``. The grader mirrors the rule, so a refused envelope still grades at least one gap and an accepted one
    none.
    """
    report = _publication(tmp_path, None, readiness)
    data = json.loads((tmp_path / "review-validation.json").read_text(encoding="utf-8"))
    data["assessments"][0]["readiness"] = "blocked"
    data["assessments"] += extra
    _write_envelope(tmp_path, data, [])
    line, why = frr.gate_verdict(report)
    assert (line, reason in why) == (gate, True)
    assert bool(frr.assess_report(report).gaps) is not bool(gate)


@pytest.mark.parametrize(
    ("readiness", "gate", "reason"),
    [
        pytest.param("incomplete", "Gate: BLOCK", "", id="pending-candidate-makes-null-incomplete"),
        pytest.param(
            "blocked",
            "",
            "otherwise readiness=blocked (pending candidate 'backend-one')",
            id="pending-candidate-under-blocked-is-named",
        ),
    ],
)
def test_pending_candidate_outranks_blocked_under_a_null_outcome(
    tmp_path: Path, readiness: str, gate: str, reason: str
) -> None:
    """A null aggregate beside a pending (null-disposition) candidate publishes as ``incomplete``, never ``blocked``.

    The completed undecidable reviewer requires ``blocked`` and the pending candidate requires ``incomplete``; with both
    present no label was valid, so the review always stopped BLOCKED after its one re-dispatch. Anything still owed now
    outranks ``blocked``, ``blocked`` fails naming the candidate, and the grader agrees with the gate: a refused
    envelope grades at least one gap, never a critical one, and an accepted one none.
    """
    report = _publication(tmp_path, None, readiness)
    data = json.loads((tmp_path / "review-validation.json").read_text(encoding="utf-8"))
    data["assessments"][0]["readiness"] = "blocked"
    data["candidates"] = [_candidate(None)]
    data["open_concerns"] = [_concern("backend-one")]
    _write_envelope(tmp_path, data, [])
    line, why = frr.gate_verdict(report)
    assessment = frr.assess_report(report)
    assert (line, reason in why) == (gate, True)
    assert (bool(assessment.gaps), assessment.critical) == (not gate, False)


@pytest.fixture(name="producer_publication")
def _producer_publication(tmp_path: Path) -> Path:
    """Publish a strictly valid version-one review whose one confirmed candidate has a merge-ready row."""
    report = _publication(tmp_path, "NEEDS_WORK")
    data = json.loads((tmp_path / "review-validation.json").read_text(encoding="utf-8"))
    data["candidates"] = [_candidate("CONFIRMED")]
    _write_envelope(tmp_path, data, [_finding_row("backend-one")])
    return report


def _row_without(field: str) -> Callable[[Path], None]:
    """Return a damage that drops one merge-required field from the only findings row."""

    def damage(report: Path) -> None:
        row = {key: value for key, value in _finding_row("backend-one").items() if key != field}
        report.with_name("findings.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8", newline="\n")

    return damage


def _row_severity(severity: str) -> Callable[[Path], None]:
    """Return a damage that gives the only findings row another severity word."""

    def damage(report: Path) -> None:
        row = {**_finding_row("backend-one"), "severity": severity}
        report.with_name("findings.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8", newline="\n")

    return damage


def _second_row_reusing_id(report: Path) -> None:
    """Confirm a second candidate whose findings row reuses the first row's id, which intake does not check."""
    data = json.loads(report.with_name("review-validation.json").read_text(encoding="utf-8"))
    data["candidates"].append(_named_candidate("second-one", "CONFIRMED"))
    rows = [_finding_row("backend-one"), {**_finding_row("second-one"), "id": "f-backend-one"}]
    _write_envelope(report.parent, data, rows)


def _legacy_shaped_header(report: Path) -> None:
    """Rewrite the header without any version-one marker, which intake reads as a legacy archive."""
    report.write_text(
        "---\nTitle: Review\nPR: #7\nGate: BLOCK\nOutcome: NEEDS_WORK\nSummary: s\n---\n",
        encoding="utf-8",
        newline="\n",
    )


@pytest.mark.parametrize(
    ("damage", "returncode", "expected"),
    [
        pytest.param(None, 0, "[gate] Gate: PASS", id="merge-ready-publication-ships"),
        pytest.param(
            _row_without("author"),
            1,
            "[gate] reason: findings.jsonl finding 1 missing author — run mint_finding_ids.py",
            id="row-without-author",
        ),
        pytest.param(
            _row_without("change"),
            1,
            "[gate] reason: findings.jsonl finding 1 missing change — run mint_finding_ids.py",
            id="row-without-change",
        ),
        pytest.param(
            _row_without("id"),
            1,
            "[gate] reason: findings.jsonl finding 1 missing id — run mint_finding_ids.py",
            id="row-without-id",
        ),
        pytest.param(
            _row_severity("major"),
            1,
            "[gate] reason: findings.jsonl finding 1 has unknown severity 'major' — use one of critical, high, medium",
            id="row-with-unknown-severity",
        ),
        pytest.param(_row_severity(" HIGH "), 0, "[gate] Gate: PASS", id="severity-case-and-spaces-as-merge-reads"),
        pytest.param(
            _second_row_reusing_id,
            1,
            "[gate] reason: findings.jsonl duplicate finding id f-backend-one on findings 1 and 2 — run mint_finding_ids",
            id="rows-sharing-an-id",
        ),
        pytest.param(
            _legacy_shaped_header,
            1,
            "[gate] reason: header lacks Review contract: 1 — the current producer writes the version-one contract",
            id="legacy-shaped-header",
        ),
    ],
)
def test_producer_gate_refuses_what_resolve_cannot_consume(
    producer_publication: Path, capsys: pytest.CaptureFixture, damage, returncode: int, expected: str
) -> None:
    """``--producer`` refuses a report resolve would accept at intake but then fail to merge, or read as an archive.

    Regression: a version-one publication whose row lacked ``author``, ``change`` or ``id``, carried a severity word
    the merge does not score, or reused another row's id passed the review's own gate, was delivered as finished, and
    resolve's merge then blocked (or, for the repeated id, silently dropped a confirmed finding); a header without the
    version-one markers bypassed every sidecar rule as a legacy report. Severity is compared the way the merge reads
    it, case and surrounding spaces ignored. The failure keeps the intake gate's marker and reason line, so the
    review's gate-failure routing and its one consolidator re-spawn work unchanged.
    """
    (damage or (lambda _report: None))(producer_publication)
    assert frr.main(["--report", str(producer_publication), "--producer"]) == returncode
    output = capsys.readouterr().out
    assert expected in output
    assert ("incomplete-review-report" in output) is bool(returncode)


@pytest.mark.parametrize(
    "damage",
    [
        pytest.param(_row_without("author"), id="row-without-author"),
        pytest.param(_legacy_shaped_header, id="legacy-shaped-header"),
    ],
)
def test_intake_without_producer_still_accepts_archives_and_bare_rows(producer_publication: Path, damage) -> None:
    """Resolve's intake keeps reading legacy reports and minimal rows; only the producer gate holds the extra rules."""
    damage(producer_publication)
    assert frr.main(["--report", str(producer_publication)]) == 0


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["--pr", "7", "--producer"], id="pr-lookup"),
        pytest.param(["--report", "r.md", "--assess", "--producer"], id="with-assess"),
    ],
)
def test_producer_needs_an_explicit_report_to_gate(argv: list[str]) -> None:
    """``--producer`` gates the report the review just wrote; a PR lookup or a gap assessment is a usage error."""
    with pytest.raises(SystemExit) as stop:
        frr.main(argv)
    assert stop.value.code == 2


@pytest.mark.skipif(_BASH is None, reason="bash unavailable")
def test_review_publication_gate_refuses_a_row_resolve_cannot_merge(producer_publication: Path) -> None:
    """The shipped Step 5 gate block runs the producer checks, so a row lacking ``author`` never ships as finished."""
    _row_without("author")(producer_publication)
    result = _run_review_publication_gate(producer_publication.parent)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "[gate] reason: findings.jsonl finding 1 missing author" in result.stdout


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        pytest.param(
            _row_without("id"), "! BLOCKED — finding 1 missing id — run mint_finding_ids.py", id="row-without-id"
        ),
        pytest.param(
            _second_row_reusing_id,
            "! BLOCKED — duplicate finding id f-backend-one on findings 1 and 2",
            id="rows-sharing-an-id",
        ),
    ],
)
def test_recheck_blocks_on_a_publication_row_the_merge_refuses(
    producer_publication: Path, capsys: pytest.CaptureFixture, damage: Callable[[Path], None], message: str
) -> None:
    """The verdict re-check refuses a validated publication holding rows the merge could not use, writing nothing.

    The intake gate checks neither row ids nor their uniqueness, so such a publication counts as valid; the re-check
    keys parent proof by finding id, so a missing id used to stop with a ``KeyError`` traceback and a repeated id would
    map one item's proof through whichever row came last. It now blocks before writing anything.
    """
    damage(producer_publication)
    review = producer_publication.parent
    (review / "head-sha.txt").write_text("abcdef1234567\n", encoding="utf-8", newline="\n")
    items = review / "items.jsonl"
    item = {"id": 1, "type": "[report][req]", "finding_id": "f-backend-one", "validation_status": "CONFIRMED"}
    items.write_text(json.dumps(item) + "\n", encoding="utf-8", newline="\n")
    before = items.read_bytes()
    argv = ["--items", str(items), "--recheck-verdicts", "--review-dir", str(review), "--current-head", "abcdef1234567"]
    assert mai.main(argv) == 1
    assert capsys.readouterr().err.startswith(message)
    assert items.read_bytes() == before


@pytest.mark.parametrize(
    "damage",
    [
        pytest.param(None, id="merge-ready-row"),
        pytest.param(_row_without("author"), id="row-without-author"),
    ],
)
def test_producer_gate_keeps_the_reject_route_whatever_the_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture, damage
) -> None:
    """A consolidator-published rejection exits 1 on the reject route under ``--producer``, never as incomplete.

    Regression: a ``REJECT_`` publication whose row lacked ``author`` printed the ``incomplete-review-report`` marker,
    so the review spent its one consolidator re-spawn repairing the row, republished the same rejection and only then
    reached its rejection handling. The producer never publishes a rejection as finished work, so its row rules do not
    apply to one.
    """
    report = _publication(tmp_path, "REJECT")
    data = json.loads((tmp_path / "review-validation.json").read_text(encoding="utf-8"))
    data["candidates"] = [_candidate("CONFIRMED")]
    _write_envelope(tmp_path, data, [_finding_row("backend-one")])
    (damage or (lambda _report: None))(report)
    assert frr.main(["--report", str(report), "--producer"]) == 1
    output = capsys.readouterr().out
    assert ("rejected (Gate: REJECT_SCOPE @abcdef1234567)" in output, "incomplete-review-report" in output) == (
        True,
        False,
    )

"""Exercise publication, assessment and reuse against real review artifacts."""

from __future__ import annotations

import json
from pathlib import Path

import find_review_report as frr
import pytest
from _verdict_publication import Publication, confirmed, judged, missing

_HEAD = "a" * 40
_ROLE = "QA specialist"
_QA = f"qa-specialist | spawned | roles: {_ROLE}\n"
_DISCLOSURE = "- Unreviewed dimension: docs+lint — dropped by FANOUT_MAX. Rerun with `/oss:review 7 --full`."


def _write_publication(directory: Path, **overrides: object) -> Path:
    """Write a minimal completed review and its frozen head."""
    directory.mkdir(parents=True, exist_ok=True)
    defaults = {"assessments": [judged(_ROLE, "APPROVE")], "lineup": _QA}
    report = Publication("REVIEW_V1_PASS", "APPROVE", "complete", **(defaults | overrides)).write(directory)
    report.with_name("head-sha.txt").write_text(_HEAD + "\n", encoding="utf-8")
    return report


def _run_dir(root: Path, index: int) -> Path:
    """Return the directory a numbered attempt for PR 7 uses."""
    return root / ".reports" / "review" / "pr-7" / f"run-{index:03}"


@pytest.mark.parametrize("suffix", ["z", "Z", "0" * 34])
def test_reject_gate_does_not_extract_a_prefix_of_an_invalid_commit_id(suffix: str) -> None:
    """A malformed recorded revision cannot authorize lifting a rejection."""
    assert frr.reject_sha(f"Gate: REJECT_SCOPE @deadbeef{suffix}") == ""


def test_reject_gate_reads_the_entire_sha256_commit_id() -> None:
    """Head movement compares all digits of a recorded full SHA-256 id."""
    revision = "abcdef01" * 8
    assert frr.reject_sha(f"Gate: REJECT_SCOPE @{revision}") == revision


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"author": ""}, id="empty-required-field"),
        pytest.param({"change": []}, id="wrong-required-field-type"),
        pytest.param({"severity": "unknown"}, id="unknown-severity"),
        pytest.param({"merged_into": ["other"]}, id="wrong-merge-target-type"),
    ],
)
def test_assessment_refuses_confirmed_rows_resolve_cannot_merge(tmp_path: Path, overrides: dict) -> None:
    """Passing envelope checks still leave a critical gap when a confirmed row fails merge validation."""
    candidate, row = confirmed("candidate", "medium")
    report = Publication(
        "REVIEW_V1_PASS",
        "MINOR_EDITS",
        "complete",
        [judged(_ROLE, "MINOR_EDITS")],
        candidates=[candidate],
        findings=[row | overrides],
    ).write(tmp_path)
    result = frr.assess_report(report)
    assert result.gate == ""
    assert [(gap.rule, gap.subject) for gap in result.gaps] == [("finding-unmergeable", "candidate")]
    assert result.critical


def test_assessment_refuses_duplicate_finding_ids(tmp_path: Path) -> None:
    """Distinct confirmed candidates cannot publish rows with the same finding identifier."""
    first, first_row = confirmed("first", "medium")
    second, second_row = confirmed("second", "medium")
    report = Publication(
        "REVIEW_V1_PASS",
        "MINOR_EDITS",
        "complete",
        [judged(_ROLE, "MINOR_EDITS")],
        candidates=[first, second],
        findings=[first_row, second_row | {"id": first_row["id"]}],
    ).write(tmp_path)
    result = frr.assess_report(report)
    assert result.critical
    assert [(gap.rule, gap.subject) for gap in result.gaps] == [("finding-unmergeable", "second")]


@pytest.mark.parametrize("head", [None, "", "not-a-commit"])
def test_producer_requires_a_valid_frozen_head(tmp_path: Path, head: str | None) -> None:
    """Producer mode refuses absent or invalid head evidence before any report can be released."""
    report = Publication(
        "REVIEW_V1_PASS", "APPROVE", "complete", [judged(_ROLE, "APPROVE")], lineup="qa-specialist | spawned\n"
    ).write(tmp_path)
    if head is not None:
        report.with_name("head-sha.txt").write_text(head, encoding="utf-8")
    assert frr.publication_verdict(report, producer=True)[1].startswith("head-sha.txt ")


@pytest.mark.parametrize(
    ("lineup", "roles"),
    [
        pytest.param("qa-specialist | spawned\n", [_ROLE], id="roles-omitted-from-launch"),
        pytest.param(_QA, ["Software engineer"], id="required-role-omitted-from-report"),
        pytest.param(_QA, [_ROLE, _ROLE], id="required-role-published-twice"),
        pytest.param(
            "docs+lint | spawned | roles: Documentation reviewer, Linting expert\n",
            ["Documentation reviewer"],
            id="second-merged-role-omitted",
        ),
        pytest.param(
            "doc-scribe | spawned | promoted: migration | roles: Documentation reviewer\n",
            ["Software engineer"],
            id="promoted-role-omitted",
        ),
    ],
)
def test_producer_refuses_unreconciled_launch_roles(tmp_path: Path, lineup: str, roles: list[str]) -> None:
    """Every spawned unit names its required assessments, each recorded exactly once."""
    report = _write_publication(tmp_path, lineup=lineup, assessments=[judged(role, "APPROVE") for role in roles])
    assert frr.publication_verdict(report, producer=True)[0] == ""


def test_producer_accepts_one_completed_and_one_missing_spawned_role(tmp_path: Path) -> None:
    """A timed-out merged reviewer keeps the unit accounted for and the publication incomplete."""
    report = Publication(
        "REVIEW_V1_PASS",
        "APPROVE",
        "incomplete",
        [judged("Documentation reviewer", "APPROVE"), missing("Linting expert")],
        lineup="docs+lint | spawned | promoted: migration | roles: Documentation reviewer, Linting expert\n",
    ).write(tmp_path)
    report.with_name("head-sha.txt").write_text(_HEAD, encoding="utf-8")
    assert frr.publication_verdict(report, producer=True) == ("Gate: BLOCK", "")


@pytest.mark.parametrize(
    ("body", "accepted"),
    [
        pytest.param(f"### Review Confidence\n\n{_DISCLOSURE}", True, id="generated-bullet"),
        pytest.param("### Review Confidence\n\n- Unreviewed dimension: docs+lint", False, id="missing-command"),
        pytest.param(f"### Findings\n\n{_DISCLOSURE}", False, id="outside-review-confidence"),
        pytest.param(f"### Review Confidence\n\n<!--\n{_DISCLOSURE}\n-->", False, id="html-comment"),
        pytest.param(f"### Review Confidence\n\n<div hidden>\n\n{_DISCLOSURE}\n\n</div>", False, id="hidden-html"),
        pytest.param(f"### Review Confidence\n\n```markdown\n{_DISCLOSURE}\n```", False, id="fenced-code"),
        pytest.param(f"### Review Confidence\n\n    {_DISCLOSURE}", False, id="indented-code"),
        pytest.param(f"### Review Confidence\n\n> {_DISCLOSURE}", False, id="quoted-example"),
        pytest.param(f"### Review Confidence\n\n{_DISCLOSURE.replace('--full', '--other')}", False, id="wrong-command"),
    ],
)
def test_producer_requires_visible_generated_rerun_disclosure(tmp_path: Path, body: str, accepted: bool) -> None:
    """Only the complete parent-rendered line in Review Confidence satisfies a dropped unit."""
    report = _write_publication(_run_dir(tmp_path, 1), lineup="docs+lint | dropped\n", body=body)
    assert bool(frr.publication_verdict(report, producer=True)[0]) is accepted


def test_markerless_versioned_run_is_withheld_from_all_intake_modes(tmp_path: Path) -> None:
    """Absence of a pending marker does not establish that a versioned run was ever released."""
    report = _write_publication(_run_dir(tmp_path, 1))
    assert frr.main(["--report", str(report), "--publication-state"]) == 1
    assert frr.gate_verdict(report)[0] == ""
    assert frr.assess_report(report).critical


@pytest.mark.parametrize("frozen", [_HEAD, "b" * 40])
def test_fresh_release_requires_the_allocation_head(
    tmp_path: Path, frozen: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a successful fresh gate may replace the pending marker with a release record."""
    report = _write_publication(_run_dir(tmp_path, 1))
    run = tmp_path / "launch"
    run.mkdir()
    (run / "report-dir.txt").write_text(report.parent.as_posix(), encoding="utf-8")
    (run / "head-sha.txt").write_text(frozen + "\n", encoding="utf-8")
    (run / "review-mode.txt").write_text("fresh\n", encoding="utf-8")
    (tmp_path / "oss-review-run-dir-publication-test").write_text(run.as_posix(), encoding="utf-8")
    monkeypatch.setenv("CSID", "publication-test")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    pending = report.with_name("review-publication.pending")
    pending.touch()
    report.with_name("lineup.launch.md").write_bytes(report.with_name("lineup.md").read_bytes())

    accepted = frozen == _HEAD
    assert frr.main(["--report", str(report), "--producer", "--publish"]) == (0 if accepted else 1)
    assert pending.is_file() is not accepted
    assert report.with_name("review-publication.published").is_file() is accepted
    assert bool(frr.gate_line(report)) is accepted


@pytest.mark.parametrize("draft", ["empty", "pending", "markerless", "parked"])
def test_reuse_skips_newer_unpublished_run_and_surfaces_its_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, draft: str
) -> None:
    """Reuse finds the last usable result while keeping the newer interrupted run visible."""
    old = _write_publication(_run_dir(tmp_path, 1))
    old.with_name("review-publication.published").touch()
    new_dir = _run_dir(tmp_path, 2)
    if draft == "empty":
        new_dir.mkdir()
    else:
        new = _write_publication(new_dir)
        if draft == "pending":
            new.with_name("review-publication.pending").touch()
        if draft == "parked":
            new.rename(new.with_name("review-report.refused.md"))
    selected = tmp_path / "selected"
    monkeypatch.chdir(tmp_path)

    assert frr.main(["--pr", "7", "--reuse", "--path-out", str(selected)]) == 0
    assert selected.read_text(encoding="utf-8").strip() == old.as_posix()
    assert f"PENDING_RUN={new_dir.as_posix()}" in capsys.readouterr().out


def test_fill_gaps_producer_requires_the_plan_head(tmp_path: Path) -> None:
    """A valid-looking plan for another revision cannot qualify the frozen review."""
    report = _write_publication(tmp_path, lineup=None)
    plan = {"head_sha": "b" * 40, "carried": {"candidates": []}}
    report.with_name("fill-gaps.json").write_text(json.dumps(plan), encoding="utf-8")
    assert "head_sha" in frr.publication_verdict(report, producer=True, fill_gaps=True)[1]

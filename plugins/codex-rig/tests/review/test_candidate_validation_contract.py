"""Exercise evidence admission and versioned source judgments at the decision boundary."""

from __future__ import annotations

import ast
import copy
import hashlib
import importlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from test_code_review_decision_validation import _load_validator, _metadata, _result
from test_final_handoff import _load_finalizer, _load_shared_validator
from test_review_batches import _batch_command, _batch_finding, _completed_batches
from test_review_completion_gate import FINDER, PLUGIN_ROOT, _assessed_pr, _module


def _candidate(disposition: str | None = "confirmed") -> dict[str, object]:
    """Return one atomic, revision-bound candidate with an explicit performed check."""
    return {
        "id": "candidate-a",
        "severity": "low",
        "claim": "The documented option is misspelled.",
        "source_revision": "frozen-source",
        "contract": "The documented option must exist.",
        "trigger": "Use the documented invocation.",
        "confirmation_evidence": "Compare the option declaration.",
        "falsification_evidence": "An alias would settle this claim.",
        "counterevidence": [],
        "check": "Inspected declaration and aliases.",
        "result": "The option is absent.",
        "environment": "frozen source inspection",
        "limits": [],
        "disposition": disposition,
        "reason": "The published invocation cannot be used.",
        "feasible_checks_complete": True,
        "missing_prerequisite": "",
        "owner": "review owner",
        "next_check": "Inspect option aliases.",
        "closure_condition": "The documented option resolves.",
    }


def _new_metadata(disposition: str | None = "confirmed") -> dict[str, object]:
    """Build the new family independently of the historical five-value rating family."""
    metadata = _metadata("minor-changes")
    metadata["finding_records_version"] = 2
    metadata["review_validation"] = {
        "schema_version": 1,
        "source_rating_version": 1,
        "source_verdict": "minor-changes",
        "verification_readiness": "complete",
        "candidates": [_candidate(disposition)],
    }
    metadata["review_findings"] = [
        {
            "id": "candidate-a",
            "severity": "low",
            "title": "Misspelled option",
            "summary": "The option is absent.",
            "required_change": "Correct the option.",
            "evidence": ["guide.md:1"],
            "closure_evidence": "The option resolves.",
        }
    ]
    return metadata


def test_unverified_candidate_cannot_enter_confirmed_finding_collection() -> None:
    """Prevent unavailable evidence from becoming an automatic defect edit request."""
    metadata = _new_metadata("unverified")
    metadata["review_validation"]["verification_readiness"] = "blocked"
    metadata["review_validation"]["candidates"][0]["missing_prerequisite"] = "Native backend unavailable."
    with pytest.raises(SystemExit, match="review-candidate-confirmed-inventory-mismatch"):
        _load_validator()._validate_review_decision(metadata, _result(low=1))


def test_feasible_unperformed_check_is_incomplete_not_unverified() -> None:
    """Reject environmental labels when a bounded available check remains unperformed."""
    metadata = _new_metadata("unverified")
    metadata["review_validation"]["candidates"][0]["feasible_checks_complete"] = False
    with pytest.raises(SystemExit, match="review-candidate-unverified-feasible-checks"):
        _load_validator()._validate_review_decision(metadata, _result(low=1))


def test_new_minor_judgment_accepts_confirmed_evidence() -> None:
    """Keep confirmed minor source changes distinct from substantive defects."""
    _load_validator()._validate_review_decision(_new_metadata(), _result(low=1))


def test_new_contract_requires_old_reader_rejection_marker() -> None:
    """An existing records marker must reject new semantics in old consumers."""
    metadata = _new_metadata()
    metadata["finding_records_version"] = 1
    with pytest.raises(SystemExit, match="review-validation-finding-records-version"):
        _load_validator()._validate_review_decision(metadata, _result(low=1))


def test_unverified_requires_unavailable_prerequisite() -> None:
    """Require the exact missing prerequisite before accepting an open concern."""
    metadata = _new_metadata("unverified")
    with pytest.raises(SystemExit, match="review-candidate-missing_prerequisite"):
        _load_validator()._validate_review_decision(metadata, _result(low=1))


def test_incomplete_feasible_work_cannot_pass() -> None:
    """Prevent a terminal passing result while available verification remains undone."""
    metadata = _new_metadata(None)
    metadata["review_validation"]["candidates"][0]["feasible_checks_complete"] = False
    metadata["review_validation"]["verification_readiness"] = "incomplete"
    metadata["review_findings"] = []
    with pytest.raises(SystemExit, match="review-incomplete-cannot-pass"):
        _load_validator()._validate_review_decision(metadata, _result())


def test_legacy_rating_four_remains_readable() -> None:
    """Historical raw four is accepted without claiming new rejection semantics."""
    metadata = _metadata("needs-more-work")
    metadata["reviewer_assessments"] = [{"role": "Main reviewer", "rating": 4, "evidence": "review-notes.md"}]
    _load_validator()._validate_review_decision(copy.deepcopy(metadata), _result())


def test_returned_null_assessment_is_distinct_from_missing_response(tmp_path: Path) -> None:
    """Bind null to an actual completed response rather than a fabricated coverage result."""
    validator = _load_validator()
    response = tmp_path / "reviewer.md"
    response.write_text(
        "## Reviewer Assessment\nSource rating version: 1\nRating: null\nVerification readiness: blocked\n"
        "Rationale: Native platform unavailable; platform owner must run the retained check.\n",
        encoding="utf-8",
    )
    assert (
        validator._retained_reviewer_rating(
            response, local_reviewer_wave=False, main=False, role="qa-specialist", source_rating_version=1
        )
        is None
    )
    response.unlink()
    with pytest.raises(SystemExit, match="review-assessment-content-invalid"):
        validator._retained_reviewer_rating(
            response, local_reviewer_wave=False, main=False, role="qa-specialist", source_rating_version=1
        )


def test_historical_four_cannot_be_relabelled_as_source_reject(tmp_path: Path) -> None:
    """Require a self-described new response before applying the versioned five-stage legend."""
    response = tmp_path / "reviewer.md"
    response.write_text("## Reviewer Assessment\nRating: 4\nRationale: Insufficient evidence.\n", encoding="utf-8")
    validator = _load_validator()
    assert (
        validator._retained_reviewer_rating(response, local_reviewer_wave=False, main=False, role="qa-specialist") == 4
    )
    with pytest.raises(SystemExit, match="review-assessment-rating-version-mismatch"):
        validator._retained_reviewer_rating(
            response, local_reviewer_wave=False, main=False, role="qa-specialist", source_rating_version=1
        )


@pytest.mark.parametrize("readiness", ["blocked", "incomplete"])
@pytest.mark.parametrize("structured", [False, True])
def test_retained_readiness_prevents_complete_aggregate(tmp_path: Path, readiness: str, structured: bool) -> None:
    """Neither native text nor optional structured reviews can silently lose unfinished verification."""
    validator = _load_validator()
    metadata = _new_metadata()
    metadata["reviewer_assessments"] = [{"role": "Main reviewer", "rating": 2, "evidence": "review-notes.md"}]
    path = tmp_path / "review-notes.md"
    path.write_text(
        "## Main Reviewer Assessment\nSource rating version: 1\nRating: 2\n"
        f"Verification readiness: {readiness}\nRationale: Verification remains open.\n",
        encoding="utf-8",
    )
    if structured:
        path = tmp_path / "qa.json"
        path.write_text(
            json.dumps(
                {
                    "assessment": {
                        "source_rating_version": 1,
                        "rating": 2,
                        "verification_readiness": readiness,
                        "rationale": "Verification remains open.",
                    }
                }
            ),
            encoding="utf-8",
        )
        metadata["reviewer_assessments"] = [{"role": "QA specialist", "rating": 2, "evidence": "qa.json"}]
    passes = {"qa-specialist": {"mode": "app-server", "output_path": "qa.json"}} if structured else {}
    with pytest.raises(SystemExit, match="review-assessment-readiness-mismatch"):
        validator._validate_reviewer_assessments(tmp_path, metadata, passes)
    metadata["review_validation"]["verification_readiness"] = readiness
    validator._validate_reviewer_assessments(tmp_path, metadata, passes)


@pytest.mark.parametrize("stage", ["source-001", "interactions", "consolidation-001"])
@pytest.mark.parametrize("mutation", ["blocked", "unversioned"])
def test_all_batch_constituents_bind_family_and_readiness(tmp_path: Path, stage: str, mutation: str) -> None:
    """Final selection cannot hide an earlier incompatible or verification-blocked response."""
    validator = _load_validator()
    metadata = _new_metadata()
    metadata["reviewer_assessments"] = [{"role": "Main reviewer", "rating": 2, "evidence": "review-notes.md"}]
    (tmp_path / "review-notes.md").write_text(
        "## Main Reviewer Assessment\nSource rating version: 1\nRating: 2\n"
        "Verification readiness: complete\nRationale: Main source assessment.\n",
        encoding="utf-8",
    )
    wave = tmp_path / "batches" / stage
    wave.mkdir(parents=True)
    response = "## Reviewer Assessment\nSource rating version: 1\nRating: 1\nVerification readiness: blocked\nRationale: Native check unavailable.\n"
    if mutation == "unversioned":
        response = "## Reviewer Assessment\nRating: 1\nRationale: Historical response.\n"
    (wave / "qa.md").write_text(response, encoding="utf-8")
    (wave / "specialist-manifest.json").write_text(
        json.dumps({"passes": [{"role": "qa-specialist", "output_path": "qa.md"}]}), encoding="utf-8"
    )
    (tmp_path / "specialist-manifest.json").write_text(
        json.dumps({"batch_execution": {"waves": [{"manifest_path": f"batches/{stage}/specialist-manifest.json"}]}}),
        encoding="utf-8",
    )
    failure = (
        "review-assessment-readiness-mismatch" if mutation == "blocked" else "review-assessment-rating-version-mismatch"
    )
    with pytest.raises(SystemExit, match=failure):
        validator._validate_reviewer_assessments(tmp_path, metadata, {}, batch_response=True)


def test_optional_reviewer_recipe_requests_current_assessment_family() -> None:
    """Keep the executable optional recipe aligned with its helper's current JSON response schema."""
    recipe = (PLUGIN_ROOT / "skills/code-review/local-reviewer-wave.md").read_text(encoding="utf-8")
    assert '`consumer_id="code-review"`, `source_rating_version=1`' in recipe
    assert "`assessment` has exactly `source_rating_version=1`, `rating`" in recipe
    assert "`verification_readiness` (`complete|blocked|incomplete`)" in recipe
    assert (
        "Legend: 1 = Approve · 2 = Minor changes · 3 = Major revision · 4 = Insufficient, but a good initiative · 5 = Block."
        in recipe
    )


@pytest.mark.parametrize(
    ("known_rating", "expected"),
    [pytest.param(3, 3, id="known-fixable-defect"), pytest.param(1, None, id="unknown-prevents-approval")],
)
def test_batch_aggregation_preserves_nullable_scope(tmp_path: Path, known_rating: int, expected: int | None) -> None:
    """A completed undecidable constituent cannot crash or silently become approval."""
    validator = _load_validator()
    batches = importlib.import_module("review_batches")
    directories = []
    for index, rating in enumerate([None, known_rating]):
        directory = tmp_path / f"part-{index}"
        directory.mkdir()
        response = (
            "## Reviewer Findings\n```json\n[]\n```\n"
            "## Reviewer Confidence\n```json\n" + json.dumps({"score": 0.95}) + "\n```\n"
            "## Reviewer Assessment\nSource rating version: 1\nRating: "
            + (str(rating) if rating is not None else "null")
            + "\nVerification readiness: "
            + ("blocked" if rating is None else "complete")
            + "\nRationale: Scoped evidence.\n"
        )
        (directory / "qa.md").write_text(response, encoding="utf-8")
        (directory / "specialist-manifest.json").write_text(
            json.dumps(
                {
                    "passes": [
                        {
                            "role": "qa-specialist",
                            "output_path": "qa.md",
                            "attempts": [],
                            "confidence": 0.95,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        directories.append(directory)
    passes = batches._role_passes(tmp_path, directories, [], multipart=True)
    assert [part["rating"] for part in passes[0]["final_parts"]] == [None, known_rating]
    assert [part["verification_readiness"] for part in passes[0]["final_parts"]] == ["blocked", "complete"]
    assert (
        validator._retained_reviewer_rating(
            tmp_path / passes[0]["output_path"],
            local_reviewer_wave=False,
            main=False,
            role="qa-specialist",
            source_rating_version=1,
        )
        == expected
    )


def test_renderer_selects_legend_by_explicit_family() -> None:
    """Keep raw historical four separate from new promising-incomplete judgment and completed null assessment."""
    finalizer = _load_finalizer()
    table = {
        "heading": "Review Snapshot",
        "columns": ["Field", "Value"],
        "rows": [],
        "reviewers": [{"role": "QA specialist", "rating": 4, "evidence": "qa.md"}],
    }
    historical = "\n".join(finalizer._render_table(table))
    assert "4 = Insufficient evidence" in historical
    table.update(source_rating_version=1, verification_readiness="blocked")
    current = "\n".join(finalizer._render_table(table))
    assert "4 = Insufficient, but a good initiative · 5 = Block." in current
    assert "Insufficient evidence" not in current
    table["reviewers"][0]["rating"] = None
    assert "QA specialist (no source judgment)" in "\n".join(finalizer._render_table(table))


def test_null_source_judgment_is_discoverable_as_returned_assessment(tmp_path: Path) -> None:
    """Discovery classifies an explicit undecidable assessment without treating it as unavailable output."""
    path = tmp_path / "result.json"
    metadata = _new_metadata("unverified")
    metadata["scope"] = "pr"
    metadata["review_decision"]["recommendation"] = None
    metadata["review_validation"]["source_verdict"] = None
    path.write_text(json.dumps({"metadata": metadata}), encoding="utf-8")
    assert _module(FINDER).review_result_kind(path) == "assessed"


def test_open_concern_intake_preserves_prerequisite_owner_and_next_check() -> None:
    """Remediation gets an evidence obligation, never an implicit confirmed-defect request."""
    metadata = _new_metadata("unverified")
    candidate = metadata["review_validation"]["candidates"][0]
    candidate["missing_prerequisite"] = "Native backend unavailable."
    metadata["confidence_recovery"] = {}
    sources = _load_shared_validator().report_obligation_sources({"metadata": metadata}, "review/result.json")
    assert len(sources) == 1
    assert sources[0]["source_id"] == "review/result.json#/metadata/review_validation/candidates/0"
    value = {"metadata": metadata}
    for token in sources[0]["source_id"].split("#", 1)[1].strip("/").split("/"):
        value = value[int(token)] if isinstance(value, list) else value[token]
    assert value == candidate
    for field in ("id", "missing_prerequisite", "owner", "next_check", "closure_condition"):
        assert candidate[field] in sources[0]["body"]
    assert "not a confirmed defect or automatic edit request" in sources[0]["body"]


@pytest.mark.integration
def test_batch_candidate_origins_survive_validation_and_remediation_intake(
    tmp_path: Path, subtests: pytest.Subtests
) -> None:
    """Keep real admitted originals linked through every parent disposition and ordinary obligation intake."""
    run, home = _completed_batches(
        tmp_path,
        blocker=(1, "challenger"),
        source_records=[_batch_finding("F_ONE", "high", "Preserve the caller contract.")],
        interaction_finding=(1, "qa-specialist", 3),
        interaction_records=[_batch_finding("F_TWO", "high", "Preserve the independent empty-input contract.")],
    )
    assembled = _batch_command(run, "assemble-batches", home)
    assert assembled.returncode == 0, assembled.stderr
    manifest = json.loads((run / "specialist-manifest.json").read_bytes())
    originals = [item for item in manifest["source_findings"] if item["disposition"] == "unresolved"]
    assert len(originals) == 2
    validator = _load_validator()
    for disposition in ("confirmed", "refuted", "already-fixed", "optional-out-of-scope", "unverified", None):
        metadata = _new_metadata(disposition)
        metadata["review_findings"] = []
        metadata["source_findings"] = originals
        metadata["source_finding_mapping"] = {}
        candidates = []
        for index, origin in enumerate(originals):
            original = origin["original"]
            candidate = _candidate(disposition)
            candidate.update(
                id=f"candidate-{index}",
                severity=original["severity"],
                claim=original["summary"],
                closure_condition=original["closure_evidence"],
                source_revision="sha256:" + hashlib.sha256((run / "batch-inventory.json").read_bytes()).hexdigest(),
                feasible_checks_complete=disposition is not None,
                missing_prerequisite="Native backend unavailable." if disposition == "unverified" else "",
            )
            candidates.append(candidate)
            metadata["source_finding_mapping"][origin["finding_id"]] = [candidate["id"]]
            if disposition == "confirmed":
                metadata["review_findings"].append(
                    {
                        **original,
                        "id": candidate["id"],
                        "authors": [validator._readable_review_role(origin["pass"]["role"])],
                        "evidence": [origin["manifest_path"], "widget.py:1-1"],
                    }
                )
        metadata["review_validation"].update(
            candidates=candidates,
            verification_readiness="incomplete"
            if disposition is None
            else "blocked"
            if disposition == "unverified"
            else "complete",
            source_verdict="needs-more-work",
        )
        metadata["review_decision"]["recommendation"] = "needs-more-work"
        result = {**_result(high=2 if disposition == "confirmed" else 0), "status": "fail", "metadata": metadata}
        with subtests.test(disposition=disposition, case="retained"):
            validator._validate_review_decision(metadata, result)
            validator._validate_candidate_source(run, metadata)
            validator._validate_batch_source_findings(run, result, manifest)
            sources = _load_shared_validator().report_obligation_sources(result, "review/result.json")
            assert len(sources) == (2 if disposition in {None, "unverified"} else 0)
            if disposition in {None, "unverified"}:
                for index, origin in enumerate(originals):
                    source = sources[index]
                    assert source["source_id"] == f"review/result.json#/metadata/review_validation/candidates/{index}"
                    assert json.dumps(origin, sort_keys=True) in source["body"]
                    assert f"/metadata/source_findings/{index}" in source["body"]
                    assert json.dumps(candidates[index], sort_keys=True) in source["body"]
                    assert "not a confirmed defect or automatic edit request" in source["body"]
        if disposition == "confirmed":
            continue
        for field, wrong in (
            ("claim", "Unrelated spelling concern."),
            ("severity", "low"),
            ("closure_condition", "Unrelated closure."),
        ):
            with subtests.test(disposition=disposition, case=field):
                broken = copy.deepcopy(result)
                broken["metadata"]["review_validation"]["candidates"][0][field] = wrong
                with pytest.raises(SystemExit, match="review-batch-result-source-finding-inventory-dropped"):
                    validator._validate_batch_source_findings(run, broken, manifest)
        with subtests.test(disposition=disposition, case="distinct-collapse"):
            broken = copy.deepcopy(result)
            broken["metadata"]["source_finding_mapping"][originals[1]["finding_id"]] = [candidates[0]["id"]]
            with pytest.raises(
                SystemExit, match=r"review-batch-(result-source-finding-inventory-dropped|distinct-findings-collapsed)"
            ):
                validator._validate_batch_source_findings(run, broken, manifest)
        reconciled = copy.deepcopy(result)
        reconciled_metadata = reconciled["metadata"]
        reconciled_metadata["source_finding_mapping"] = {
            origin["finding_id"]: [candidates[0]["id"]] for origin in originals
        }
        reconciled_metadata["review_validation"]["candidates"] = [candidates[0]]
        group = {
            "invariant": "Both caller contracts must remain explicit.",
            "rationale": "One combined contract check covers both retained original obligations.",
            "origins": {
                origin["finding_id"]: "The combined check retains this caller contract." for origin in originals
            },
            "evidence": [originals[0]["original"]["evidence"][0].copy()],
        }
        reconciled_metadata["source_finding_reconciliation"] = {candidates[0]["id"]: group}
        with subtests.test(disposition=disposition, case="reconciled"):
            validator._validate_batch_source_findings(run, reconciled, manifest)
            sources = _load_shared_validator().report_obligation_sources(reconciled, "review/result.json")
            assert len(sources) == (1 if disposition in {None, "unverified"} else 0)
            if sources:
                assert all(json.dumps(origin, sort_keys=True) in sources[0]["body"] for origin in originals)
        for damage in ("missing-origin", "weak-severity", "wrong-source"):
            with subtests.test(disposition=disposition, case=damage):
                broken = copy.deepcopy(reconciled)
                broken_group = broken["metadata"]["source_finding_reconciliation"][candidates[0]["id"]]
                if damage == "missing-origin":
                    broken_group["origins"].pop(originals[0]["finding_id"])
                elif damage == "weak-severity":
                    broken["metadata"]["review_validation"]["candidates"][0]["severity"] = "low"
                else:
                    broken_group["evidence"][0]["source_sha256"] = "0" * 64
                with pytest.raises(SystemExit, match="review-batch-source-reconciliation-invalid"):
                    validator._validate_batch_source_findings(run, broken, manifest)
        duplicate_manifest = copy.deepcopy(manifest)
        duplicate_manifest["source_findings"][1]["original"] = copy.deepcopy(originals[0]["original"])
        duplicated = copy.deepcopy(reconciled)
        duplicated["metadata"].pop("source_finding_reconciliation")
        duplicated["metadata"]["source_findings"] = duplicate_manifest["source_findings"]
        ordered_ids = sorted(origin["finding_id"] for origin in originals)
        duplicated["metadata"]["source_finding_duplicates"] = {ordered_ids[1]: ordered_ids[0]}
        with subtests.test(disposition=disposition, case="exact-duplicate"):
            validator._validate_batch_source_findings(run, duplicated, duplicate_manifest)
            duplicated["metadata"]["source_finding_duplicates"] = {}
            with pytest.raises(SystemExit, match="review-batch-duplicate-accounting-mismatch"):
                validator._validate_batch_source_findings(run, duplicated, duplicate_manifest)


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("damage", ["missing-table", "altered-evidence", "missing-action"])
def test_local_minor_notes_require_canonical_actions(tmp_path: Path, version: int, damage: str) -> None:
    """Current and historical record markers preserve the same notes-table evidence gate."""
    metadata = _new_metadata()
    metadata["finding_records_version"] = version
    notes = tmp_path / "review-notes.md"
    body = (
        "## Review Findings and Merge Blocks\n\n"
        "| Finding / area | Required change | Evidence | Status |\n"
        "| --- | --- | --- | --- |\n"
        "| candidate-a | Correct the option. | guide.md:1 | Required |\n"
    )
    if damage == "missing-table":
        body = "## Source assessment\nMinor changes required.\n"
    elif damage == "altered-evidence":
        body = body.replace("guide.md:1", "unrelated.md:99")
    else:
        body = body.replace("candidate-a", "unrelated")
    notes.write_text(body, encoding="utf-8")
    with pytest.raises(SystemExit, match=r"review-.*findings-action-table"):
        _load_validator()._validate_action_table(
            notes, {**_result(low=1), "schema_version": 3}, metadata, "working-tree"
        )


@pytest.mark.parametrize(
    ("verdict", "rating"),
    [
        pytest.param("insufficient-but-promising", 4, id="promising-incomplete-source"),
        pytest.param("reject", 5, id="documented-binding-conflict"),
    ],
)
def test_current_source_stages_survive_decision_renderer_and_discovery(
    tmp_path: Path, verdict: str, rating: int
) -> None:
    """Keep source implementation judgment separate from historical evidence insufficiency."""
    metadata = _new_metadata()
    metadata["review_decision"]["recommendation"] = verdict
    metadata["review_validation"]["source_verdict"] = verdict
    _load_validator()._validate_review_decision(metadata, _result(low=1))
    table = {
        "heading": "Review Snapshot",
        "columns": ["Field", "Value"],
        "rows": [],
        "source_rating_version": 1,
        "verification_readiness": "complete",
        "reviewers": [{"role": "Main reviewer", "rating": rating, "evidence": "review-notes.md"}],
    }
    rendered = "\n".join(_load_finalizer()._render_table(table))
    assert f"Main reviewer ({rating})" in rendered
    assert "4 = Insufficient, but a good initiative · 5 = Block." in rendered
    metadata["scope"] = "pr"
    result = tmp_path / "result.json"
    result.write_text(json.dumps({"metadata": metadata}), encoding="utf-8")
    assert _module(FINDER).review_result_kind(result) == "assessed"


def test_old_ordinary_completion_rejects_new_records_version(tmp_path: Path) -> None:
    """Prove the semantic cutover fails at the old records gate, not unrelated missing provenance."""
    run = _assessed_pr.__wrapped__(tmp_path)
    current = subprocess.run(
        [sys.executable, str(FINDER), "--complete-run", str(run), "--parent-thread-id", "thread"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert current.returncode == 0, current.stdout + current.stderr
    archived = tmp_path / "old-consumer"
    for directory in ("shared", "skills/code-review", "roles"):
        shutil.copytree(PLUGIN_ROOT / directory, archived / directory)
    validator_path = archived / "skills/code-review/validate_artifacts.py"
    source = validator_path.read_text(encoding="utf-8")
    declaration = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name == "_review_finding_identities"
    )
    legacy = (Path(__file__).parent / "fixtures/legacy-finding-records-reader.txt").read_text(encoding="utf-8")
    lines = source.splitlines(keepends=True)
    validator_path.write_text(
        "".join(lines[: declaration.lineno - 1]) + legacy + "".join(lines[declaration.end_lineno :]), encoding="utf-8"
    )
    old = subprocess.run(
        [
            sys.executable,
            str(archived / "shared/find-review-report.py"),
            "--complete-run",
            str(run),
            "--parent-thread-id",
            "thread",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert old.returncode == 1
    assert "review-validation-failed:review-finding-records-version-invalid" in old.stderr


def test_ordinary_completion_rejects_dropped_reviewer_readiness(tmp_path: Path) -> None:
    """Passing process gates cannot override a retained reviewer's blocked verification."""
    run = _assessed_pr.__wrapped__(tmp_path)
    command = [sys.executable, str(FINDER), "--complete-run", str(run), "--parent-thread-id", "thread"]
    good = subprocess.run(command, capture_output=True, text=True, check=False)
    assert good.returncode == 0, good.stdout + good.stderr
    notes = run / "review-notes.md"
    notes.write_text(
        notes.read_text(encoding="utf-8").replace(
            "Verification readiness: complete", "Verification readiness: blocked"
        ),
        encoding="utf-8",
    )
    blocked = subprocess.run(command, capture_output=True, text=True, check=False)
    assert blocked.returncode == 1
    assert "review-validation-failed:review-assessment-readiness-mismatch" in blocked.stderr


def test_qualified_minor_source_judgment_does_not_clear_blocked_verification() -> None:
    """A known minor source issue remains publishable while actual required gates stay failed."""
    metadata = _new_metadata()
    metadata["review_validation"]["verification_readiness"] = "blocked"
    result = _result(low=1)
    result.update(status="fail", checks_failed=["native-backend-unavailable"])
    _load_validator()._validate_review_decision(metadata, result)
    assert result["status"] == "fail"


def test_unverified_concern_survives_bound_final_handoff(tmp_path: Path) -> None:
    """A backend uncertainty remains visible with exact ownership and closure, outside defect actions."""
    run = _assessed_pr.__wrapped__(tmp_path)
    result = json.loads((run / "result.json").read_text(encoding="utf-8"))
    handoff = json.loads((run / "final-handoff.json").read_text(encoding="utf-8"))
    concern = _candidate("unverified")
    concern["missing_prerequisite"] = "Native platform unavailable."
    metadata = result["metadata"]
    metadata["review_validation"].update(source_verdict=None, verification_readiness="blocked", candidates=[concern])
    metadata["review_decision"].update(recommendation=None, summary="No source judgment; verification blocked.")
    metadata["reviewer_assessments"][0]["rating"] = None
    result["status"] = "fail"
    handoff["outcome"]["summary"] = "Recommendation: None."
    snapshot = handoff["tables"][0]
    snapshot.update(
        verification_readiness="blocked",
        reviewers=metadata["reviewer_assessments"],
        summary=metadata["review_decision"]["summary"],
    )
    snapshot["rows"][-1]["cells"][1] = "no source judgment"
    handoff["remaining"] = [
        {
            "row_id": concern["id"],
            "item": concern["claim"] + " " + concern["missing_prerequisite"],
            "owner": concern["owner"],
            "next_action": concern["next_check"] + " " + concern["closure_condition"],
        }
    ]
    handoff["next_steps"] = [concern["id"]]
    validator = _load_shared_validator()
    validator._validate_code_review_final_handoff(result, handoff, out_dir=run)
    rendered = _load_finalizer().render_handoff(handoff, report_root=run)
    for field in ("id", "missing_prerequisite", "owner", "next_check", "closure_condition"):
        assert concern[field] in rendered
    handoff["remaining"] = []
    with pytest.raises(SystemExit, match="code-review-final-handoff-open-concern-missing:candidate-a"):
        validator._validate_code_review_final_handoff(result, handoff, out_dir=run)


def test_candidate_source_revision_binds_actual_retained_head(tmp_path: Path) -> None:
    """An unrelated revision string cannot certify a candidate against this run's source."""
    run = _assessed_pr.__wrapped__(tmp_path)
    metadata = _new_metadata()
    validator = _load_validator()
    with pytest.raises(SystemExit, match="review-candidate-source-revision-mismatch"):
        validator._validate_candidate_source(run, metadata)
    routing = json.loads((run / "pr-routing.json").read_text(encoding="utf-8"))
    metadata["review_validation"]["candidates"][0]["source_revision"] = "head:" + routing["head_oid"]
    validator._validate_candidate_source(run, metadata)

"""Regression checks for selecting assessed code-review reports for remediation."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from test_review_completion_gate import _assessed_pr, _module

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
FINDER_PATH = PLUGIN_ROOT / "shared" / "find-review-report.py"
CREATE_RUN_PATH = PLUGIN_ROOT / "shared" / "create_run.py"


@pytest.mark.parametrize(
    "broken_result",
    [
        pytest.param(
            json.dumps(
                {"metadata": {"scope": "pr", "review_decision": {"recommendation": None}, "review_validation": None}}
            ).encode(),
            id="null-review-validation",
        ),
        pytest.param(
            json.dumps(
                {"metadata": {"scope": "pr", "review_decision": {"recommendation": None}, "review_validation": []}}
            ).encode(),
            id="list-review-validation",
        ),
        pytest.param(
            json.dumps({"metadata": {"scope": "pr", "review_decision": {"recommendation": []}}}).encode(),
            id="unhashable-recommendation",
        ),
        pytest.param(b"\xff", id="non-utf8-result"),
        pytest.param(b"[" * 1500 + b"]" * 1500, id="excessively-nested-result"),
    ],
)
def test_cli_classifies_broken_review_and_ignores_unrelated_pr(tmp_path: Path, broken_result: bytes) -> None:
    """Broken review artifacts retain their recovery anchor without blocking other PRs."""
    older = _write_nested_report(tmp_path, "run-001")
    newer = _write_nested_report(tmp_path, "run-002")
    newer.write_bytes(broken_result)

    matching = subprocess.run(
        [sys.executable, str(FINDER_PATH), "--reports-dir", str(tmp_path), "--target", "123"],
        capture_output=True,
        text=True,
        check=False,
    )
    unrelated = subprocess.run(
        [sys.executable, str(FINDER_PATH), "--reports-dir", str(tmp_path), "--target", "456"],
        capture_output=True,
        text=True,
        check=False,
    )
    explicit = subprocess.run(
        [sys.executable, str(FINDER_PATH), "--result", str(newer)], capture_output=True, text=True, check=False
    )

    assert older.is_file()
    assert matching.returncode == 1
    assert matching.stderr.strip() == f"invalid-review-report-rerun-code-review:{newer}"
    assert unrelated.returncode == 1
    assert unrelated.stderr.strip() == "missing-matching-review-report"
    assert explicit.returncode == 1
    assert explicit.stderr.strip() == f"invalid-review-report-rerun-code-review:{newer}"


def test_cli_damaged_pr_identity_and_target_do_not_block_other_pr(tmp_path: Path) -> None:
    """A non-UTF-8 identity is invalid for its directory but does not poison unrelated lookups."""
    newer = _write_nested_report(tmp_path, "run-002")
    (newer.parent / "pr.json").write_bytes(b"\xff")
    (newer.parent / "pr-target.txt").write_bytes(b"\xff")
    matching = subprocess.run(
        [sys.executable, str(FINDER_PATH), "--reports-dir", str(tmp_path), "--target", "123"],
        capture_output=True,
        text=True,
        check=False,
    )
    unrelated = subprocess.run(
        [sys.executable, str(FINDER_PATH), "--reports-dir", str(tmp_path), "--target", "456"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert matching.returncode == 1
    assert matching.stderr.strip() == f"invalid-review-report-rerun-code-review:{newer}"
    assert unrelated.returncode == 1
    assert unrelated.stderr.strip() == "missing-matching-review-report"


def test_broken_review_does_not_hide_another_pr_review(tmp_path: Path) -> None:
    """A malformed result for one PR cannot prevent discovery of another PR's result."""
    finder = _load_finder()
    broken = _write_nested_report(tmp_path, "run-002")
    broken.write_text(
        json.dumps(
            {"metadata": {"scope": "pr", "review_decision": {"recommendation": None}, "review_validation": None}}
        ),
        encoding="utf-8",
    )
    other = _write_nested_report(tmp_path, "run-001", pull_number=456)

    assert finder.find_latest_review_report("456", [tmp_path]) == other


@pytest.mark.parametrize("target", ["123", "https://github.com/acme/widgets/pull/123"])
@pytest.mark.parametrize("url", ["not-a-pr-url", "https://github.com/acme/widgets/pull/456"])
def test_malformed_newer_pr_url_cannot_revive_older_review(tmp_path: Path, target: str, url: str) -> None:
    """A newer run with a damaged URL remains the recovery anchor for its PR."""
    finder = _load_finder()
    _write_nested_report(tmp_path, "run-001")
    newer = _write_nested_report(tmp_path, "run-002")
    (newer.parent / "pr.json").write_text(json.dumps({"number": 123, "url": url}), encoding="utf-8")

    with pytest.raises(LookupError, match="invalid-review-report-rerun-code-review"):
        finder.find_latest_review_report(target, [tmp_path])
    result = subprocess.run(
        [sys.executable, str(FINDER_PATH), "--reports-dir", str(tmp_path), "--target", target],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert result.stderr.strip() == f"invalid-review-report-rerun-code-review:{newer}"


def test_different_repository_url_does_not_block_matching_pr(tmp_path: Path) -> None:
    """A valid different repository identity is not a damaged identity for the requested PR."""
    finder = _load_finder()
    expected = _write_nested_report(tmp_path, "run-001")
    other = _write_nested_report(tmp_path, "run-002")
    (other.parent / "pr.json").write_text(
        json.dumps({"number": 123, "url": "https://github.com/other/widgets/pull/123"}), encoding="utf-8"
    )

    assert finder.find_latest_review_report("https://github.com/acme/widgets/pull/123", [tmp_path]) == expected


@pytest.mark.parametrize("target", ["123", "https://github.com/acme/widgets/pull/123"])
@pytest.mark.parametrize("url", ["not-a-pr-url", "https://github.com/acme/widgets/pull/456"])
def test_flat_damaged_identity_blocks_older_review(tmp_path: Path, target: str, url: str) -> None:
    """A flat report's retained number identifies a damaged newer review as the recovery anchor."""
    finder = _load_finder()
    for date, stored_url in [("2026-01-01", "https://github.com/acme/widgets/pull/123"), ("2026-02-01", url)]:
        run = tmp_path / date
        run.mkdir()
        (run / "pr.json").write_text(json.dumps({"number": 123, "url": stored_url}), encoding="utf-8")
        (run / "result.json").write_text(
            json.dumps({"metadata": {"scope": "pr", "review_decision": {"recommendation": "needs-more-work"}}}),
            encoding="utf-8",
        )

    with pytest.raises(LookupError, match="invalid-review-report-rerun-code-review"):
        finder.find_latest_review_report(target, [tmp_path])


@pytest.mark.parametrize("target", ["123", "https://github.com/acme/widgets/pull/123"])
def test_flat_conflicting_identity_uses_recorded_target_for_fresh_review(tmp_path: Path, target: str) -> None:
    """A damaged newer identity cannot hide its recorded PR target and revive older findings."""
    finder = _load_finder()
    for date, number, url in [
        ("2026-01-01", 123, "https://github.com/acme/widgets/pull/123"),
        ("2026-02-01", 456, "invalid-url"),
    ]:
        run = tmp_path / date
        run.mkdir()
        (run / "pr.json").write_text(json.dumps({"number": number, "url": url}), encoding="utf-8")
        (run / "result.json").write_text(
            json.dumps({"metadata": {"scope": "pr", "review_decision": {"recommendation": "needs-more-work"}}}),
            encoding="utf-8",
        )
    newer = tmp_path / "2026-02-01"
    (newer / "pr-target.txt").write_text("https://github.com/acme/widgets/pull/123\n", encoding="utf-8")

    with pytest.raises(LookupError, match="invalid-review-report-rerun-code-review") as error:
        finder.find_latest_review_report(target, [tmp_path])
    assert str(error.value) == f"invalid-review-report-rerun-code-review:{newer / 'result.json'}"


@pytest.fixture(name="assessed_local")
def _assessed_local(tmp_path: Path) -> Path:
    """Build a complete local artifact using the existing real producer fixture."""
    run = _assessed_pr.__wrapped__(tmp_path)
    result_path = run / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["metadata"]["scope"] = "working-tree"
    commit = result["metadata"]["reviewed_commit_sha"]
    (run / "reviewed-commit.txt").write_text(commit + "\n", encoding="utf-8", newline="\n")
    handoff_path = run / "final-handoff.json"
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    snapshot = handoff["tables"][0]
    snapshot["heading"] = "Review Snapshot"
    snapshot["rows"][0]["cells"] = ["Scope", "working-tree"]
    snapshot["rows"][1]["cells"] = ["Revision", f"diff sha256:{result['metadata']['review_input_sha256']}"]
    snapshot["rows"][2]["cells"] = ["CI", "unavailable"]
    snapshot["rows"][3]["cells"] = ["Type", "docs"]
    snapshot["rows"][-2]["cells"] = ["Commit", commit + " (working-tree base)"]
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
    validation = _module(PLUGIN_ROOT / "shared/final_handoff.py").render_files(
        handoff_path, run / "final.md", run / "final-handoff.validation.json"
    )
    for field in ("handoff_sha256", "rendered_sha256"):
        result["metadata"]["final_handoff"][field] = validation[field]
    result_path.write_text(json.dumps(result), encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(FINDER_PATH), "--complete-run", str(run), "--parent-thread-id", "thread"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return result_path


def _load_finder() -> object:
    """Load the standalone report finder from its shipped plugin path."""
    specification = importlib.util.spec_from_file_location("find_review_report", FINDER_PATH)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


@pytest.mark.integration
def test_explicit_local_archive_schema_two_is_readable_but_unverified(assessed_local: Path) -> None:
    """Keep archive access without admitting an old result as remediation evidence."""
    result = json.loads(assessed_local.read_text(encoding="utf-8"))
    result["schema_version"] = 2
    del result["metadata"]["reviewer_assessments"]
    handoff_path = assessed_local.parent / "final-handoff.json"
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    handoff["presentation_version"] = 2
    del handoff["tables"][0]["reviewers"]
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
    validation = _module(PLUGIN_ROOT / "shared/final_handoff.py").render_files(
        handoff_path, assessed_local.parent / "final.md", assessed_local.parent / "final-handoff.validation.json"
    )
    for field in ("handoff_sha256", "rendered_sha256"):
        result["metadata"]["final_handoff"][field] = validation[field]
    assessed_local.write_text(json.dumps(result), encoding="utf-8")
    finder = _load_finder()

    with pytest.raises(LookupError, match="historical-review-unverified-archive"):
        finder.require_assessed_review_result(assessed_local, parent_thread_id="thread")
    archive = subprocess.run(
        [sys.executable, str(FINDER_PATH), "--archive-result", str(assessed_local)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert archive.returncode == 0, archive.stderr
    assert json.loads(archive.stdout) == {
        "status": "unverified-archive",
        "path": str(assessed_local),
        "schema_version": 2,
        "scope": "working-tree",
    }
    with pytest.raises(LookupError, match="review-completion-requires-bound-schema-v3"):
        finder.complete_review_run(assessed_local.parent, parent_thread_id="thread")


def test_schema_three_without_retained_role_proof_is_readable_only_as_archive(tmp_path: Path) -> None:
    """Older current-format reports remain inspectable without certifying their provenance."""
    result = tmp_path / "result.json"
    result.write_text(json.dumps({"schema_version": 3, "metadata": {"scope": "pr"}}), encoding="utf-8")

    archive = subprocess.run(
        [sys.executable, str(FINDER_PATH), "--archive-result", str(result)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert archive.returncode == 0, archive.stderr
    assert json.loads(archive.stdout) == {
        "status": "unverified-archive",
        "path": str(result),
        "schema_version": 3,
        "scope": "pr",
    }


@pytest.mark.integration
def test_pr_target_lookup_rejects_unverified_archive(tmp_path: Path) -> None:
    """Prevent automatic PR remediation from consuming a schema-two result."""
    run = _assessed_pr.__wrapped__(tmp_path)
    result_path = run / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["schema_version"] = 2
    result_path.write_text(json.dumps(result), encoding="utf-8")

    targeted = subprocess.run(
        [sys.executable, str(FINDER_PATH), "--target", "123", "--reports-dir", str(run.parent.parent)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert targeted.returncode == 1
    assert targeted.stdout == ""
    assert "historical-review-unverified-archive" in targeted.stderr


@pytest.mark.integration
def test_rewritten_review_notes_rating_mismatch_is_rejected(assessed_local: Path) -> None:
    """A schema-v3 result whose notes disagree with the recorded rating is rejected."""
    notes_path = assessed_local.parent / "review-notes.md"
    notes = notes_path.read_text(encoding="utf-8")
    assert notes.count("Rating: 1") == 1
    notes_path.write_text(notes.replace("Rating: 1", "Rating: 2"), encoding="utf-8")

    command = [sys.executable, str(FINDER_PATH), "--result", str(assessed_local), "--parent-thread-id", "thread"]
    current = subprocess.run(command, capture_output=True, text=True, check=False)

    assert current.returncode == 1
    assert current.stdout == ""
    assert "review-assessment-rating-mismatch:main reviewer" in current.stderr


@pytest.mark.integration
def test_schema_downgrade_cannot_admit_a_rewritten_review_for_remediation(assessed_local: Path) -> None:
    """Reject a rewritten current result even after its assessment fields are stripped."""
    command = [sys.executable, str(FINDER_PATH), "--result", str(assessed_local), "--parent-thread-id", "thread"]
    result = json.loads(assessed_local.read_text(encoding="utf-8"))
    assert result["schema_version"] == 3
    result["schema_version"] = 2
    del result["metadata"]["reviewer_assessments"]
    handoff_path = assessed_local.parent / "final-handoff.json"
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    handoff["presentation_version"] = 2
    del handoff["tables"][0]["reviewers"]
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
    validation = _module(PLUGIN_ROOT / "shared/final_handoff.py").render_files(
        handoff_path, assessed_local.parent / "final.md", assessed_local.parent / "final-handoff.validation.json"
    )
    for field in ("handoff_sha256", "rendered_sha256"):
        result["metadata"]["final_handoff"][field] = validation[field]
    assessed_local.write_text(json.dumps(result), encoding="utf-8")

    downgraded = subprocess.run(command, capture_output=True, text=True, check=False)

    assert downgraded.returncode == 1
    assert downgraded.stdout == ""
    assert "historical-review-unverified-archive" in downgraded.stderr


def _write_report(root: Path, timestamp: str, *, unavailable: bool) -> Path:
    """Create a minimal PR-identified report with an optional terminal collection failure."""
    report_dir = root / timestamp
    report_dir.mkdir()
    if unavailable:
        (report_dir / "pr-target.txt").write_text("https://github.com/acme/widgets/pull/123\n", encoding="utf-8")
    else:
        (report_dir / "pr.json").write_text(
            json.dumps({"number": 123, "url": "https://github.com/acme/widgets/pull/123"}), encoding="utf-8"
        )
    metadata: dict[str, object] = {"scope": "pr", "review_decision": {"recommendation": "needs-more-work"}}
    if unavailable:
        metadata["review_status"] = "unavailable"
    result_path = report_dir / "result.json"
    result_path.write_text(json.dumps({"metadata": metadata}), encoding="utf-8")
    return result_path


def _write_closed_report(root: Path, timestamp: str) -> Path:
    """Create a PR-identified terminal close result that remediation must not consume."""
    report_dir = root / timestamp
    report_dir.mkdir()
    (report_dir / "pr.json").write_text(
        json.dumps({"number": 123, "url": "https://github.com/acme/widgets/pull/123"}), encoding="utf-8"
    )
    result_path = report_dir / "result.json"
    result_path.write_text(
        json.dumps({"metadata": {"scope": "pr", "review_status": "closed", "close_decision": {"code": "DUPLICATE"}}}),
        encoding="utf-8",
    )
    return result_path


def _write_candidate_report(root: Path, timestamp: str, *, pull_number: int = 123) -> Path:
    """Create an assessed PR candidate that has not passed artifact validation."""
    report_dir = root / timestamp
    report_dir.mkdir()
    (report_dir / "pr.json").write_text(
        json.dumps({"number": pull_number, "url": f"https://github.com/acme/widgets/pull/{pull_number}"}),
        encoding="utf-8",
    )
    candidate_path = report_dir / "result.candidate.json"
    candidate_path.write_text(
        json.dumps({"metadata": {"scope": "pr", "review_decision": {"recommendation": "needs-more-work"}}}),
        encoding="utf-8",
    )
    return candidate_path


def _write_nested_report(
    root: Path,
    run_name: str,
    *,
    pull_number: int = 123,
    result_name: str = "result.json",
    review_status: str | None = None,
) -> Path:
    """Create one report in the PR-scoped, numerically ordered run topology."""
    report_dir = root / f"pr-{pull_number}" / run_name
    report_dir.mkdir(parents=True)
    (report_dir / "pr.json").write_text(
        json.dumps({"number": pull_number, "url": f"https://github.com/acme/widgets/pull/{pull_number}"}),
        encoding="utf-8",
    )
    metadata: dict[str, object] = {"scope": "pr", "review_decision": {"recommendation": "needs-more-work"}}
    if review_status is not None:
        metadata["review_status"] = review_status
    result_path = report_dir / result_name
    result_path.write_text(json.dumps({"metadata": metadata}), encoding="utf-8")
    return result_path


class TestPrScopedReviewRuns:
    """Protect discovery and terminal ordering for PR-scoped review runs."""

    @pytest.mark.parametrize(
        ("stored_url", "target"),
        [
            pytest.param(
                "https://github.com/Acme/Widgets/pull/123",
                "https://github.com/acme/widgets/pull/123",
                id="collected-name-casing",
            ),
            pytest.param(
                "https://github.com/acme/widgets/pull/123",
                "HTTPS://GITHUB.COM/ACME/WIDGETS/pull/123",
                id="requested-url-casing",
            ),
        ],
    )
    def test_url_casing_selects_newest_same_pr(self, tmp_path: Path, stored_url: str, target: str) -> None:
        """GitHub identity casing cannot hide the latest assessed review."""
        finder = _load_finder()
        _write_nested_report(tmp_path, "run-001")
        newest = _write_nested_report(tmp_path, "run-002")
        (newest.parent / "pr.json").write_text(json.dumps({"number": 123, "url": stored_url}), encoding="utf-8")

        assert finder.find_latest_review_report(target, [tmp_path]) == newest

    @pytest.mark.parametrize("pending_kind", ["identity", "partial-identity", "candidate", "flat-target"])
    def test_url_casing_keeps_newer_pending_run_blocking(self, tmp_path: Path, pending_kind: str) -> None:
        """Equivalent URL spelling cannot revive findings after a newer review starts."""
        finder = _load_finder()
        target = "https://GITHUB.COM/ACME/WIDGETS/pull/123"
        if pending_kind == "flat-target":
            _write_report(tmp_path, "2026-08-10T10-00-00Z", unavailable=False)
            pending = tmp_path / "2026-08-10T11-00-00Z"
            pending.mkdir()
            (pending / "pr-target.txt").write_text("https://github.com/acme/widgets/pull/123", encoding="utf-8")
        else:
            _write_nested_report(tmp_path, "run-001")
            pending = tmp_path / "pr-123" / "run-002"
            pending.mkdir()
            identity = {"number": 123}
            if pending_kind != "partial-identity":
                identity["url"] = "https://github.com/Acme/Widgets/pull/123"
            (pending / "pr.json").write_text(json.dumps(identity), encoding="utf-8")
            if pending_kind == "candidate":
                (pending / "result.candidate.json").write_text("{}", encoding="utf-8")
        diagnostic = (
            "matching-review-candidate-unpromoted:" if pending_kind == "candidate" else "matching-review-incomplete:"
        )

        with pytest.raises(LookupError, match=diagnostic) as error:
            finder.find_latest_review_report(target, [tmp_path])

        assert str(pending) in str(error.value)

    @pytest.mark.parametrize(
        "target",
        [
            "https://example.com/acme/widgets/pull/123",
            "https://github.com/other/widgets/pull/123",
            "https://github.com/acme/other/pull/123",
            "https://github.com/acme/widgets/pull/456",
            "https://github.com/acme/widgets/PULL/123",
            "https://github.com/acme/widgets/pull/0123",
            "https://github.com/acme/widgets/pull/123?query=1",
            "https://github.com/acme/widgets/pull/123#fragment",
            "https://github.com:443/acme/widgets/pull/123",
            "https://user@github.com/acme/widgets/pull/123",
            "https://github.com/ac%6de/widgets/pull/123",
            "https://github.com/acme/widgets/pull/123/extra",
        ],
    )
    def test_url_identity_rejects_different_or_noncanonical_target(self, tmp_path: Path, target: str) -> None:
        """Casing tolerance preserves canonical URL structure and repository identity."""
        finder = _load_finder()
        _write_nested_report(tmp_path, "run-001")

        with pytest.raises(LookupError, match="missing-matching-review-report"):
            finder.find_latest_review_report(target, [tmp_path])

    def test_selects_nested_assessed_result(self, tmp_path: Path) -> None:
        """Discover assessed output below the explicit PR and run directories."""
        finder = _load_finder()
        assessed = _write_nested_report(tmp_path, "run-001")

        selected = finder.find_latest_review_report("#123", [tmp_path])

        assert selected == assessed

    def test_newer_notes_without_result_block_older_review(self, tmp_path: Path) -> None:
        """Retained preliminary review evidence must not disappear behind an older verdict."""
        finder = _load_finder()
        _write_nested_report(tmp_path, "run-001")
        incomplete = tmp_path / "pr-123" / "run-002"
        incomplete.mkdir()
        (incomplete / "pr.json").write_text(json.dumps({"number": 123}), encoding="utf-8")
        (incomplete / "review-notes.md").write_text("Preliminary findings; promotion failed.", encoding="utf-8")

        with pytest.raises(LookupError, match="matching-review-incomplete:"):
            finder.find_latest_review_report("123", [tmp_path])

    def test_newer_promoted_run_before_notes_blocks_older_review(self, tmp_path: Path) -> None:
        """A collected and promoted PR remains current before notes or result are written."""
        finder = _load_finder()
        _write_nested_report(tmp_path, "run-001")
        pending = tmp_path / "pr-123" / "run-002"
        pending.mkdir()
        (pending / "pr.json").write_text(
            json.dumps({"number": 123, "url": "https://github.com/acme/widgets/pull/123"}), encoding="utf-8"
        )

        with pytest.raises(LookupError, match="matching-review-incomplete:") as error:
            finder.find_latest_review_report("123", [tmp_path])

        assert str(pending) in str(error.value)

    def test_newer_partial_identity_blocks_url_lookup(self, tmp_path: Path) -> None:
        """A run with a PR number but no URL cannot revive an older URL-matched result."""
        finder = _load_finder()
        _write_nested_report(tmp_path, "run-001")
        pending = tmp_path / "pr-123" / "run-002"
        pending.mkdir()
        (pending / "pr.json").write_text(json.dumps({"number": 123}), encoding="utf-8")

        with pytest.raises(LookupError, match="matching-review-incomplete:"):
            finder.find_latest_review_report("https://github.com/acme/widgets/pull/123", [tmp_path])

    @pytest.mark.parametrize(
        ("artifact_name", "identity"),
        [
            pytest.param("result.candidate.json", None, id="candidate-missing-identity"),
            pytest.param("result.candidate.json", "{", id="candidate-malformed-identity"),
            pytest.param("result.candidate.json", '{"number": 456}', id="candidate-conflicting-identity"),
            pytest.param("result.json", "{", id="result-malformed-identity"),
        ],
    )
    def test_newer_identity_damaged_run_blocks_older_review(
        self, tmp_path: Path, artifact_name: str, identity: str | None
    ) -> None:
        """Identity damage in a newer PR run cannot revive stale assessed findings."""
        finder = _load_finder()
        _write_nested_report(tmp_path, "run-001")
        pending = _write_nested_report(tmp_path, "run-002", result_name=artifact_name)
        identity_path = pending.parent / "pr.json"
        if identity is None:
            identity_path.unlink()
        else:
            identity_path.write_text(identity, encoding="utf-8")

        with pytest.raises(
            LookupError, match=r"matching-review-candidate-unpromoted|invalid-review-report-rerun-code-review"
        ):
            finder.find_latest_review_report("123", [tmp_path])

    @pytest.mark.parametrize("incomplete_kind", ["notes", "candidate"])
    def test_later_collection_failure_does_not_clear_incomplete_review(
        self, tmp_path: Path, incomplete_kind: str
    ) -> None:
        """A failed collection cannot make an older assessment current again."""
        finder = _load_finder()
        _write_nested_report(tmp_path, "run-001")
        if incomplete_kind == "notes":
            pending = _write_nested_report(tmp_path, "run-002", result_name="review-notes.md")
            diagnostic = "matching-review-incomplete:"
        else:
            pending = _write_nested_report(tmp_path, "run-002", result_name="result.candidate.json")
            diagnostic = "matching-review-candidate-unpromoted:"
        _write_nested_report(tmp_path, "run-003", review_status="unavailable")

        with pytest.raises(LookupError, match=diagnostic) as error:
            finder.find_latest_review_report("123", [tmp_path])

        assert str(pending.parent) in str(error.value)

    def test_completed_result_supersedes_earlier_incomplete_notes(self, tmp_path: Path) -> None:
        """Recovery needs a newer completed review, not deletion of retained evidence."""
        finder = _load_finder()
        incomplete = tmp_path / "pr-123" / "run-001"
        incomplete.mkdir(parents=True)
        (incomplete / "pr.json").write_text(json.dumps({"number": 123}), encoding="utf-8")
        (incomplete / "review-notes.md").write_text("Preliminary findings.", encoding="utf-8")
        assessed = _write_nested_report(tmp_path, "run-002")

        assert finder.find_latest_review_report("123", [tmp_path]) == assessed

    def test_newer_malformed_result_does_not_resurrect_older_verdict(self, tmp_path: Path) -> None:
        """A failed current handoff cannot silently fall back to a stale assessed review."""
        finder = _load_finder()
        _write_nested_report(tmp_path, "run-001")
        broken = _write_nested_report(tmp_path, "run-002")
        broken.write_text("{}", encoding="utf-8")
        with pytest.raises(LookupError, match="invalid-review-report-rerun-code-review"):
            finder.find_latest_review_report("123", [tmp_path])

    @pytest.mark.parametrize("earlier_kind", ["notes", "candidate", "closed"])
    @pytest.mark.parametrize("damage", ["missing-identity", "malformed-identity", "malformed-result"])
    def test_newest_damaged_result_owns_recovery_over_older_unfinished_or_closed_run(
        self, tmp_path: Path, earlier_kind: str, damage: str
    ) -> None:
        """Prevent recovery of an older run from hiding the newest damaged review and its evidence."""
        finder = _load_finder()
        if earlier_kind == "notes":
            earlier = _write_nested_report(tmp_path, "run-001", result_name="review-notes.md")
        elif earlier_kind == "candidate":
            earlier = _write_nested_report(tmp_path, "run-001", result_name="result.candidate.json")
        else:
            earlier = _write_nested_report(tmp_path, "run-001", review_status="closed")
        newest = _write_nested_report(tmp_path, "run-002")
        if damage == "missing-identity":
            (newest.parent / "pr.json").unlink()
        elif damage == "malformed-identity":
            (newest.parent / "pr.json").write_text("{", encoding="utf-8")
        else:
            newest.write_text("{", encoding="utf-8")
        before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

        with pytest.raises(LookupError, match="invalid-review-report-rerun-code-review") as error:
            finder.find_latest_review_report("https://github.com/acme/widgets/pull/123", [tmp_path])

        assert str(newest) in str(error.value)
        assert str(earlier) not in str(error.value)
        assert {path: path.read_bytes() for path in before} == before

    def test_newer_closed_result_supersedes_older_damaged_result(self, tmp_path: Path) -> None:
        """Keep a newer terminal close authoritative while identifying the actual retained close report."""
        finder = _load_finder()
        earlier = _write_nested_report(tmp_path, "run-001")
        (earlier.parent / "pr.json").unlink()
        newest = _write_nested_report(tmp_path, "run-002", review_status="closed")

        with pytest.raises(LookupError, match="matching-review-closed-not-remediable") as error:
            finder.find_latest_review_report("123", [tmp_path])

        assert str(newest) in str(error.value)
        assert str(earlier) not in str(error.value)

    @pytest.mark.parametrize("disposition", ["invalid", "unavailable", "closed"])
    def test_rejected_lookup_names_the_selected_report(self, tmp_path: Path, disposition: str) -> None:
        """Give recovery the rejected artifact path instead of requiring it to guess another run."""
        finder = _load_finder()
        older = _write_nested_report(tmp_path, "run-001", review_status="unavailable")
        newest = _write_nested_report(
            tmp_path, "run-002", review_status=None if disposition == "invalid" else disposition
        )
        if disposition == "invalid":
            newest.write_text("{}", encoding="utf-8")
        diagnostic = {
            "invalid": "invalid-review-report-rerun-code-review",
            "unavailable": "matching-review-unavailable-rerun-code-review",
            "closed": "matching-review-closed-not-remediable",
        }[disposition]

        with pytest.raises(LookupError, match=diagnostic) as error:
            finder.find_latest_review_report("123", [tmp_path])

        assert str(newest) in str(error.value)
        assert str(older) not in str(error.value)

    def test_notes_only_report_is_incomplete_not_missing(self, tmp_path: Path) -> None:
        """Diagnose an identified failed handoff independently of session continuity."""
        finder = _load_finder()
        run = tmp_path / "pr-123" / "run-001"
        run.mkdir(parents=True)
        (run / "pr.json").write_text(json.dumps({"number": 123}), encoding="utf-8")
        (run / "review-notes.md").write_text("Retained findings.", encoding="utf-8")

        with pytest.raises(LookupError, match="matching-review-incomplete:"):
            finder.find_latest_review_report("123", [tmp_path])

    def test_orders_run_indexes_numerically(self, tmp_path: Path) -> None:
        """Prevent lexical ordering from making run 099 newer than run 100."""
        finder = _load_finder()
        _write_nested_report(tmp_path, "run-099")
        newest = _write_nested_report(tmp_path, "run-100")

        selected = finder.find_latest_review_report("123", [tmp_path])

        assert selected == newest

    def test_nested_run_takes_precedence_over_legacy_timestamp(self, tmp_path: Path) -> None:
        """Treat any matching PR-scoped run as newer than flat timestamp reports."""
        finder = _load_finder()
        _write_report(tmp_path, "9999-12-31T23-59-59Z", unavailable=False)
        nested = _write_nested_report(tmp_path, "run-001")

        selected = finder.find_latest_review_report("123", [tmp_path])

        assert selected == nested

    def test_newer_nested_candidate_blocks_assessed_run(self, tmp_path: Path) -> None:
        """Require promotion of the highest numbered run before remediation reuse."""
        finder = _load_finder()
        _write_nested_report(tmp_path, "run-009")
        candidate = _write_nested_report(tmp_path, "run-010", result_name="result.candidate.json")

        with pytest.raises(LookupError, match="matching-review-candidate-unpromoted") as error:
            finder.find_latest_review_report("123", [tmp_path])

        assert str(candidate) in str(error.value)

    @pytest.mark.parametrize(
        "earlier_run",
        [
            pytest.param({"result_name": "result.candidate.json"}, id="candidate"),
            pytest.param({"review_status": "closed"}, id="closed"),
        ],
    )
    def test_older_nested_run_does_not_block_assessed_run(self, tmp_path: Path, earlier_run: dict[str, str]) -> None:
        """Prefer a promoted later run over an abandoned earlier candidate or an earlier terminal close."""
        finder = _load_finder()
        _write_nested_report(tmp_path, "run-009", **earlier_run)
        assessed = _write_nested_report(tmp_path, "run-010")

        selected = finder.find_latest_review_report("123", [tmp_path])

        assert selected == assessed

    def test_newer_nested_closed_run_blocks_assessed_run(self, tmp_path: Path) -> None:
        """Prevent an earlier assessment from surviving a later terminal close."""
        finder = _load_finder()
        _write_nested_report(tmp_path, "run-009")
        _write_nested_report(tmp_path, "run-010", review_status="closed")

        with pytest.raises(LookupError, match="matching-review-closed-not-remediable"):
            finder.find_latest_review_report("123", [tmp_path])

    def test_rejects_invalid_nested_result(self, tmp_path: Path) -> None:
        """Retain explicit result validation inside the new directory topology."""
        finder = _load_finder()
        invalid = _write_nested_report(tmp_path, "run-001")
        invalid.write_text(json.dumps({"metadata": {"scope": "pr"}}), encoding="utf-8")

        with pytest.raises(LookupError, match="invalid-review-report-rerun-code-review"):
            finder.find_latest_review_report("123", [tmp_path])

    def test_nested_result_requires_explicit_pr_identity(self, tmp_path: Path) -> None:
        """Do not treat the directory name alone as validated result identity."""
        finder = _load_finder()
        result = _write_nested_report(tmp_path, "run-001")
        (result.parent / "pr.json").unlink()

        with pytest.raises(LookupError, match="invalid-review-report-rerun-code-review"):
            finder.find_latest_review_report("123", [tmp_path])

    def test_nested_result_rejects_directory_identity_disagreement(self, tmp_path: Path) -> None:
        """Require the PR directory and collected identity to name the same pull request."""
        finder = _load_finder()
        result = _write_nested_report(tmp_path, "run-001")
        (result.parent / "pr.json").write_text(
            json.dumps({"number": 456, "url": "https://github.com/acme/widgets/pull/456"}), encoding="utf-8"
        )

        with pytest.raises(LookupError, match="missing-matching-review-report"):
            finder.find_latest_review_report("456", [tmp_path])

    @pytest.mark.parametrize(
        ("pr_name", "run_name"),
        [
            pytest.param("pr-x", "run-001", id="pr-x"),
            pytest.param("pr-123", "run-x", id="pr-123-run-x"),
            pytest.param("pr-123", "run-01", id="pr-123-run-01"),
            pytest.param("pr-123", "run-000", id="pr-123-run-000"),
        ],
    )
    def test_ignores_malformed_nested_directory_names(self, tmp_path: Path, pr_name: str, run_name: str) -> None:
        """Ignore directories outside the canonical PR and zero-padded run grammar."""
        finder = _load_finder()
        report_dir = tmp_path / pr_name / run_name
        report_dir.mkdir(parents=True)
        (report_dir / "pr.json").write_text(
            json.dumps({"number": 123, "url": "https://github.com/acme/widgets/pull/123"}), encoding="utf-8"
        )
        (report_dir / "result.json").write_text(
            json.dumps({"metadata": {"scope": "pr", "review_decision": {"recommendation": "accept-as-is"}}}),
            encoding="utf-8",
        )

        with pytest.raises(LookupError, match="missing-matching-review-report"):
            finder.find_latest_review_report("123", [tmp_path])


class TestPromotedRunIntegration:
    """Join the allocator's printed topology to the remediation finder."""

    def test_finder_selects_the_promoted_run(self, tmp_path: Path) -> None:
        """Prevent producer and consumer path grammars from drifting apart."""
        created = subprocess.run(
            [sys.executable, str(CREATE_RUN_PATH), "--skill", "code-review", "--root", str(tmp_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert created.returncode == 0, created.stderr
        staging = Path(created.stdout.strip())
        (staging / "pr.json").write_text(
            json.dumps({"number": 123, "url": "https://github.com/acme/widgets/pull/123"}), encoding="utf-8"
        )
        (staging / "result.json").write_text(
            json.dumps({"metadata": {"scope": "pr", "review_decision": {"recommendation": "needs-more-work"}}}),
            encoding="utf-8",
        )

        promoted = subprocess.run(
            [
                sys.executable,
                str(CREATE_RUN_PATH),
                "--skill",
                "code-review",
                "--root",
                str(tmp_path),
                "--promote-pr-run",
                str(staging),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        expected = tmp_path / "code-review" / "pr-123" / "run-001" / "result.json"
        assert promoted.returncode == 0, promoted.stderr
        assert Path(promoted.stdout.strip()) == expected.parent
        assert _load_finder().find_latest_review_report("#123", [tmp_path / "code-review"]) == expected


def test_newer_unavailable_report_does_not_shadow_older_assessed_review(tmp_path: Path) -> None:
    """Keep automatic remediation bound to findings that were actually assessed."""
    finder = _load_finder()
    assessed = _write_report(tmp_path, "2026-08-10T10-00-00Z", unavailable=False)
    _write_report(tmp_path, "2026-08-10T11-00-00Z", unavailable=True)

    selected = finder.find_latest_review_report("123", [tmp_path])

    assert selected == assessed


@pytest.mark.parametrize("target", ["https://github.com/acme/widgets/pull/123", "123", "#123"])
def test_only_unavailable_reports_require_a_new_code_review(tmp_path: Path, target: str) -> None:
    """Do not let remediation consume an operational diagnostic as source findings.

    A pre-identity collection failure keeps its unavailable diagnosis for the URL form and for numeric lookup.
    """
    finder = _load_finder()
    _write_report(tmp_path, "2026-08-10T11-00-00Z", unavailable=True)

    with pytest.raises(LookupError, match="matching-review-unavailable-rerun-code-review"):
        finder.find_latest_review_report(target, [tmp_path])


@pytest.mark.parametrize(
    ("newer_result", "expected_error"),
    [
        pytest.param("{", "invalid-review-report-rerun-code-review", id="malformed-result"),
        pytest.param(
            json.dumps({"metadata": {"scope": "pr", "review_status": "closed"}}),
            "invalid-review-report-rerun-code-review",
            id="closed-result",
        ),
    ],
)
def test_newer_flat_target_only_result_blocks_stale_assessment(
    tmp_path: Path, newer_result: str, expected_error: str
) -> None:
    """A flat run identified by its recorded PR target cannot revive old findings."""
    finder = _load_finder()
    _write_report(tmp_path, "2026-08-10T10-00-00Z", unavailable=False)
    newer = tmp_path / "2026-08-10T11-00-00Z"
    newer.mkdir()
    (newer / "pr-target.txt").write_text("https://github.com/acme/widgets/pull/123\n", encoding="utf-8")
    (newer / "result.json").write_text(newer_result, encoding="utf-8")

    with pytest.raises(LookupError, match=expected_error):
        finder.find_latest_review_report("123", [tmp_path])

    with pytest.raises(LookupError, match=expected_error):
        finder.find_latest_review_report("https://github.com/acme/widgets/pull/123", [tmp_path])


@pytest.mark.parametrize(
    ("pending_file", "target"),
    [
        pytest.param("pr-target.txt", "123", id="target-only-numeric"),
        pytest.param("pr-target.txt", "https://github.com/acme/widgets/pull/123", id="target-only-url"),
        pytest.param("pr.json", "123", id="identity-only-numeric"),
        pytest.param("pr.json", "https://github.com/acme/widgets/pull/123", id="identity-only-url"),
    ],
)
def test_newer_flat_collection_before_result_blocks_stale_assessment(
    tmp_path: Path, pending_file: str, target: str
) -> None:
    """A started flat collection stays pending before notes or result exist."""
    finder = _load_finder()
    _write_report(tmp_path, "2026-08-10T10-00-00Z", unavailable=False)
    pending = tmp_path / "2026-08-10T11-00-00Z"
    pending.mkdir()
    payload = (
        "https://github.com/acme/widgets/pull/123\n"
        if pending_file == "pr-target.txt"
        else json.dumps({"number": 123, "url": "https://github.com/acme/widgets/pull/123"})
    )
    (pending / pending_file).write_text(payload, encoding="utf-8")

    with pytest.raises(LookupError, match="matching-review-incomplete:") as error:
        finder.find_latest_review_report(target, [tmp_path])

    assert str(pending) in str(error.value)


@pytest.mark.parametrize("pending_file", ["pr-target.txt", "pr.json"])
@pytest.mark.parametrize(
    "target",
    ["malformed-target", "https://example.com/acme/widgets/pull/123", "https://github.com/acme/widgets/PULL/123"],
)
def test_malformed_target_cannot_match_unrelated_flat_pending_run(
    tmp_path: Path, pending_file: str, target: str
) -> None:
    """Missing parsed identity cannot equal a flat report's missing directory identity."""
    finder = _load_finder()
    pending = tmp_path / "2026-08-10T11-00-00Z"
    pending.mkdir()
    payload = "https://github.com/acme/widgets/pull/456" if pending_file == "pr-target.txt" else '{"number": 456}'
    (pending / pending_file).write_text(payload, encoding="utf-8")

    with pytest.raises(LookupError, match="missing-matching-review-report"):
        finder.find_latest_review_report(target, [tmp_path])


@pytest.mark.parametrize(
    "target",
    ["malformed-target", "https://example.com/acme/widgets/pull/123", "https://github.com/acme/widgets/PULL/123"],
)
def test_matching_malformed_flat_target_is_not_pr_identity(tmp_path: Path, target: str) -> None:
    """An identical retained malformed string cannot bypass canonical URL validation."""
    finder = _load_finder()
    pending = tmp_path / "2026-08-10T11-00-00Z"
    pending.mkdir()
    (pending / "pr-target.txt").write_text(target, encoding="utf-8")

    with pytest.raises(LookupError, match="missing-matching-review-report"):
        finder.find_latest_review_report(target, [tmp_path])


@pytest.mark.parametrize("target", ["0", "#0"])
@pytest.mark.parametrize("stored_target", ["0", "malformed-target"])
def test_zero_pr_target_cannot_match_flat_pending_run(tmp_path: Path, target: str, stored_target: str) -> None:
    """Neither numeric text nor two missing parsed numbers can admit a zero PR identity."""
    finder = _load_finder()
    pending = tmp_path / "2026-08-10T11-00-00Z"
    pending.mkdir()
    (pending / "pr-target.txt").write_text(stored_target, encoding="utf-8")

    with pytest.raises(LookupError, match="missing-matching-review-report"):
        finder.find_latest_review_report(target, [tmp_path])


@pytest.mark.parametrize("intake", ["explicit", "target"])
def test_pr_metadata_only_report_is_rejected(tmp_path: Path, intake: str) -> None:
    """PR intake must not accept a disposition without the producer's required evidence."""
    report_dir = tmp_path / "run"
    report_dir.mkdir()
    (report_dir / "pr.json").write_text(json.dumps({"number": 123}), encoding="utf-8")
    result = report_dir / "result.json"
    result.write_text(
        json.dumps(
            {"schema_version": 3, "metadata": {"scope": "pr", "review_decision": {"recommendation": "needs-more-work"}}}
        ),
        encoding="utf-8",
    )

    args = ["--result", str(result)] if intake == "explicit" else ["--target", "123", "--reports-dir", str(tmp_path)]
    completed = subprocess.run([sys.executable, str(FINDER_PATH), *args], capture_output=True, text=True, check=False)

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert "review-validation-failed:" in completed.stderr


def test_explicit_invalid_report_diagnostic_retains_requested_path(tmp_path: Path) -> None:
    """Keep the supplied recovery target available when explicit admission rejects its report bytes."""
    path = _write_nested_report(tmp_path, "run-001")
    path.write_text("{}", encoding="utf-8")

    with pytest.raises(LookupError, match="invalid-review-report-rerun-code-review") as error:
        _load_finder().require_assessed_review_result(path)

    assert str(path) in str(error.value)


@pytest.mark.integration
def test_pr_intake_revalidates_recorded_producer_across_sessions(tmp_path: Path) -> None:
    """Validate current PR intake against its producer rather than the consumer thread."""
    run = _assessed_pr.__wrapped__(tmp_path)
    path = run / "result.json"
    for args in (["--result", str(path)], ["--target", "123", "--reports-dir", str(run.parent.parent)]):
        completed = subprocess.run(
            [sys.executable, str(FINDER_PATH), *args], capture_output=True, text=True, check=False
        )
        assert completed.returncode == 0, completed.stderr
        assert Path(completed.stdout.strip()) == path
    (run / "diff.patch").write_text("Unreviewed replacement diff.\n", encoding="utf-8")
    damaged = subprocess.run(
        [sys.executable, str(FINDER_PATH), "--result", str(path)], capture_output=True, text=True, check=False
    )
    assert damaged.returncode == 1
    assert damaged.stdout == ""
    assert "review-validation-failed:" in damaged.stderr


@pytest.mark.integration
@pytest.mark.parametrize(
    ("run_name", "result_name", "review_status", "expected_error"),
    [
        pytest.param(
            "run-002", "result.candidate.json", None, "matching-review-candidate-unpromoted", id="higher-candidate"
        ),
        pytest.param(
            "run-001", "result.candidate.json", None, "matching-review-candidate-unpromoted", id="equal-candidate"
        ),
        pytest.param("run-001", "result.json", "closed", "matching-review-closed-not-remediable", id="equal-closed"),
        pytest.param("run-001", "result.json", None, "review-completion-not-current", id="equal-assessed"),
    ],
)
def test_explicit_legacy_pr_result_is_superseded_by_canonical_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_name: str,
    result_name: str,
    review_status: str | None,
    expected_error: str,
) -> None:
    """An equally numbered current run supersedes a legacy PR result."""
    legacy_run = _assessed_pr.__wrapped__(tmp_path / "legacy")
    canonical_root = tmp_path / "canonical" / "code-review"
    newer = _write_nested_report(canonical_root, run_name, result_name=result_name, review_status=review_status)
    finder = _load_finder()
    monkeypatch.setattr(finder, "CURRENT_REPORTS_DIR", canonical_root)
    monkeypatch.setattr(finder, "LEGACY_REPORTS_DIR", legacy_run.parent.parent)

    assert finder.complete_review_run(legacy_run, parent_thread_id="thread") == (legacy_run / "final.md").read_bytes()
    with pytest.raises(LookupError, match=expected_error) as error:
        finder.require_assessed_review_result(legacy_run / "result.json", parent_thread_id="thread")

    if result_name == "result.candidate.json":
        assert str(newer) in str(error.value)


def test_canonical_root_supersedes_larger_legacy_run_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A legacy run number is never a cross-root recency claim."""
    legacy_root = tmp_path / "legacy"
    canonical_root = tmp_path / "canonical"
    _write_nested_report(legacy_root, "run-009")
    current = _write_nested_report(canonical_root, "run-001")
    finder = _load_finder()
    monkeypatch.setattr(finder, "CURRENT_REPORTS_DIR", canonical_root)
    monkeypatch.setattr(finder, "LEGACY_REPORTS_DIR", legacy_root)

    assert finder.find_latest_review_report("123", [legacy_root, canonical_root]) == current


@pytest.mark.integration
def test_candidate_beside_promoted_result_blocks_pr_intake(tmp_path: Path) -> None:
    """A pending candidate in the same run cannot hide behind its promoted sibling."""
    run = _assessed_pr.__wrapped__(tmp_path)
    promoted = run / "result.json"
    candidate = run / "result.candidate.json"
    candidate.write_bytes(promoted.read_bytes())
    finder = _load_finder()

    with pytest.raises(LookupError, match="matching-review-candidate-unpromoted") as error:
        finder.require_assessed_review_result(promoted, parent_thread_id="thread")

    assert str(candidate) in str(error.value)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("pending_file", "expected_error"),
    [
        pytest.param("pr.json", "matching-review-incomplete", id="collected-only"),
        pytest.param("result.candidate.json", "matching-review-candidate-unpromoted", id="candidate-identity-lost"),
    ],
)
def test_explicit_pr_intake_rejects_newer_pending_run(tmp_path: Path, pending_file: str, expected_error: str) -> None:
    """A valid older result cannot pass explicit intake while a newer PR run is unfinished."""
    run = _assessed_pr.__wrapped__(tmp_path)
    pending = run.parent / "run-002"
    pending.mkdir()
    if pending_file == "pr.json":
        (pending / "pr.json").write_text(
            json.dumps({"number": 123, "url": "https://github.com/acme/widgets/pull/123"}), encoding="utf-8"
        )
    else:
        (pending / "result.candidate.json").write_text("{}", encoding="utf-8")

    with pytest.raises(LookupError, match=expected_error):
        _load_finder().require_assessed_review_result(run / "result.json", parent_thread_id="thread")


@pytest.mark.integration
@pytest.mark.parametrize("scope", ["working-tree", "path", "commit"])
def test_explicit_local_report_requires_complete_validation(assessed_local: Path, scope: str) -> None:
    """Accept real local artifacts through both producer validators before intake."""
    result = json.loads(assessed_local.read_text(encoding="utf-8"))
    result["metadata"]["scope"] = scope
    handoff_path = assessed_local.parent / "final-handoff.json"
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    handoff["tables"][0]["rows"][0]["cells"] = ["Scope", scope]
    commit = result["metadata"]["reviewed_commit_sha"]
    handoff["tables"][0]["rows"][-2]["cells"] = [
        "Commit",
        commit if scope == "commit" else commit + " (working-tree base)",
    ]
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
    validation = _module(PLUGIN_ROOT / "shared/final_handoff.py").render_files(
        handoff_path, assessed_local.parent / "final.md", assessed_local.parent / "final-handoff.validation.json"
    )
    for field in ("handoff_sha256", "rendered_sha256"):
        result["metadata"]["final_handoff"][field] = validation[field]
    assessed_local.write_text(json.dumps(result), encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, str(FINDER_PATH), "--result", str(assessed_local), "--parent-thread-id", "thread"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == str(assessed_local)
    assert completed.stderr == ""


@pytest.mark.parametrize("scope", ["working-tree", "path", "commit"])
def test_explicit_local_metadata_only_report_is_rejected(tmp_path: Path, scope: str) -> None:
    """A canonical filename and plausible disposition do not establish producer validation."""
    result = tmp_path / "result.json"
    result.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "metadata": {"scope": scope, "review_decision": {"recommendation": "needs-more-work"}},
            }
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [sys.executable, str(FINDER_PATH), "--result", str(result), "--parent-thread-id", "thread"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert "review-validation-failed:" in completed.stderr


@pytest.mark.integration
@pytest.mark.parametrize("damage", ["draft", "missing-handoff", "final-text", "diff", "manifest-parent"])
def test_explicit_local_report_rejects_invalid_producer_evidence(assessed_local: Path, damage: str) -> None:
    """Exercise real validation failures instead of mocking the intake's proof boundary."""
    result = json.loads(assessed_local.read_text(encoding="utf-8"))
    run = assessed_local.parent
    if damage == "draft":
        result["metadata"]["review_status"] = "draft"
        assessed_local.write_text(json.dumps(result), encoding="utf-8")
    elif damage == "missing-handoff":
        del result["metadata"]["final_handoff"]
        assessed_local.write_text(json.dumps(result), encoding="utf-8")
    elif damage == "final-text":
        (run / "final.md").write_text("Unvalidated replacement final.\n", encoding="utf-8")
    elif damage == "diff":
        (run / "diff.patch").write_text("Unreviewed replacement diff.\n", encoding="utf-8")
    else:
        manifest_path = run / "specialist-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["parent_thread_id"] = "different-producer"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, str(FINDER_PATH), "--result", str(assessed_local), "--parent-thread-id", "thread"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert "review-validation-failed:" in completed.stderr


@pytest.mark.integration
def test_explicit_local_alias_cannot_validate_its_sibling_instead(assessed_local: Path) -> None:
    """Never validate result.json while returning a different, unvalidated local file."""
    alias = assessed_local.with_name("other.json")
    alias.write_text(
        json.dumps({"metadata": {"scope": "working-tree", "review_decision": {"recommendation": "needs-more-work"}}}),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [sys.executable, str(FINDER_PATH), "--result", str(alias), "--parent-thread-id", "thread"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert "invalid-review-report-rerun-code-review" in completed.stderr


@pytest.mark.parametrize("scope", ["working-tree", "path", "commit"])
def test_pr_lookup_rejects_local_report_with_leftover_pr_identity(tmp_path: Path, scope: str) -> None:
    """Explicit local intake must not widen PR discovery to unrelated local assessments."""
    result = _write_report(tmp_path, "2026-08-10T11-00-00Z", unavailable=False)
    result.write_text(
        json.dumps({"metadata": {"scope": scope, "review_decision": {"recommendation": "needs-more-work"}}}),
        encoding="utf-8",
    )

    with pytest.raises(LookupError) as error:
        _load_finder().find_latest_review_report("123", [tmp_path])
    assert str(error.value) == f"invalid-review-report-rerun-code-review:{result}"


@pytest.mark.parametrize(
    ("metadata", "diagnostic"),
    [
        pytest.param(
            {"scope": "working-tree", "review_status": "unavailable"},
            "matching-review-unavailable-rerun-code-review",
            id="local-unavailable",
        ),
        pytest.param(
            {"scope": "path", "review_status": "closed"},
            "matching-review-closed-not-remediable",
            id="local-closed",
        ),
        pytest.param(
            {"scope": "unknown", "review_decision": {"recommendation": "needs-more-work"}},
            "invalid-review-report-rerun-code-review",
            id="unknown-scope",
        ),
        pytest.param(
            {"scope": [], "review_decision": {"recommendation": "needs-more-work"}},
            "invalid-review-report-rerun-code-review",
            id="malformed-scope",
        ),
        pytest.param(
            {"scope": "commit", "review_decision": {"recommendation": "unknown"}},
            "invalid-review-report-rerun-code-review",
            id="unknown-decision",
        ),
    ],
)
def test_explicit_local_intake_retains_rejection_boundaries(
    tmp_path: Path, metadata: dict[str, object], diagnostic: str
) -> None:
    """Preserve rejection categories and identify the exact recoverable report without admitting it."""
    result = tmp_path / "result.json"
    result.write_text(json.dumps({"metadata": metadata}), encoding="utf-8")

    with pytest.raises(LookupError) as error:
        _load_finder().require_assessed_review_result(result)
    assert str(error.value) == f"{diagnostic}:{result}"


@pytest.mark.parametrize("scope", ["working-tree", "path", "commit"])
def test_explicit_local_candidate_still_requires_promotion(tmp_path: Path, scope: str) -> None:
    """Local scope support must not make a draft result consumable."""
    candidate = tmp_path / "result.candidate.json"
    candidate.write_text(
        json.dumps({"metadata": {"scope": scope, "review_decision": {"recommendation": "needs-more-work"}}}),
        encoding="utf-8",
    )

    with pytest.raises(LookupError, match=r"^matching-review-candidate-unpromoted:"):
        _load_finder().require_assessed_review_result(candidate)


def test_explicit_unavailable_report_is_rejected_as_remediation_input(tmp_path: Path) -> None:
    """Apply the same assessed-review guard when the report path is user supplied."""
    finder = _load_finder()
    unavailable = _write_report(tmp_path, "2026-08-10T11-00-00Z", unavailable=True)

    with pytest.raises(LookupError, match="matching-review-unavailable-rerun-code-review"):
        finder.require_assessed_review_result(unavailable)


def test_malformed_result_is_rejected_as_remediation_input(tmp_path: Path) -> None:
    """Fail closed instead of treating an unreadable diagnostic as an assessed review."""
    finder = _load_finder()
    malformed = tmp_path / "result.json"
    malformed.write_text("not-json", encoding="utf-8")

    with pytest.raises(LookupError, match="invalid-review-report-rerun-code-review"):
        finder.require_assessed_review_result(malformed)


@pytest.mark.parametrize(
    ("write_report", "error"),
    [
        pytest.param(_write_closed_report, "matching-review-closed-not-remediable", id="closed"),
        pytest.param(_write_candidate_report, "matching-review-candidate-unpromoted", id="candidate"),
    ],
)
def test_explicit_nonassessed_report_is_rejected_as_remediation_input(
    tmp_path: Path, write_report: Callable[[Path, str], Path], error: str
) -> None:
    """Never treat an explicit close decision or unpromoted candidate as validated source findings.

    A terminal proposal-level close is not source findings, and an explicit candidate path must not bypass full artifact
    validation.
    """
    finder = _load_finder()
    report = write_report(tmp_path, "2026-08-10T11-00-00Z")

    with pytest.raises(LookupError, match=error):
        finder.require_assessed_review_result(report)


def test_newer_closed_report_blocks_older_assessed_review(tmp_path: Path) -> None:
    """Prevent remediation from reviving stale findings after a newer close decision."""
    finder = _load_finder()
    _write_report(tmp_path, "2026-08-10T10-00-00Z", unavailable=False)
    _write_closed_report(tmp_path, "2026-08-10T11-00-00Z")

    with pytest.raises(LookupError, match="matching-review-closed-not-remediable"):
        finder.find_latest_review_report("123", [tmp_path])


def test_candidate_only_report_requires_validation_and_promotion(tmp_path: Path) -> None:
    """Expose recoverable candidate state instead of reporting no prior review."""
    finder = _load_finder()
    candidate = _write_candidate_report(tmp_path, "2026-08-10T11-00-00Z")

    with pytest.raises(LookupError, match="matching-review-candidate-unpromoted") as error:
        finder.find_latest_review_report("123", [tmp_path])

    assert str(candidate) in str(error.value)


def test_newer_candidate_blocks_fallback_to_stale_assessed_review(tmp_path: Path) -> None:
    """Require recovery of the newest same-PR review before reusing stale findings."""
    finder = _load_finder()
    _write_report(tmp_path, "2026-08-10T10-00-00Z", unavailable=False)
    candidate = _write_candidate_report(tmp_path, "2026-08-10T11-00-00Z")

    with pytest.raises(LookupError, match="matching-review-candidate-unpromoted") as error:
        finder.find_latest_review_report("123", [tmp_path])

    assert str(candidate) in str(error.value)


def test_candidate_for_other_pull_request_does_not_block_assessed_review(tmp_path: Path) -> None:
    """Scope candidate recovery to the requested pull request rather than the report root."""
    finder = _load_finder()
    assessed = _write_report(tmp_path, "2026-08-10T10-00-00Z", unavailable=False)
    _write_candidate_report(tmp_path, "2026-08-10T11-00-00Z", pull_number=456)

    selected = finder.find_latest_review_report("123", [tmp_path])

    assert selected == assessed


@pytest.fixture
def finding_with_direct_sources(tmp_path: Path) -> tuple[Path, list[dict]]:
    """Retain two distinct online origins and ordinary reviewer evidence for one confirmed finding."""
    origins = [
        {
            "id": f"review-comment-{index}",
            "parent_id": f"review-thread-{index}",
            "path": "widget.py",
            "line": index + 6,
            "body": body,
            "artifact": "review-threads.json",
            "candidate_id": "F1",
            "disposition": "confirmed",
        }
        for index, body in enumerate(("Guard the empty input.", "Guard the repeated empty input."), 1)
    ]
    specialist = tmp_path / "specialists" / "engineer.md"
    specialist.parent.mkdir()
    specialist.write_text("Both input paths require the same guard.\n", encoding="utf-8", newline="\n")
    (tmp_path / "review-notes.md").write_text("Confirmed input guard.\n", encoding="utf-8", newline="\n")
    report = {
        "metadata": {
            "review_findings": [
                {
                    "id": "F1",
                    "title": "Missing input guard",
                    "authors": ["Software engineer"],
                    "evidence": ["specialists/engineer.md", "widget.py:7"],
                }
            ],
            "reviewer_assessments": [{"role": "Software engineer", "evidence": "specialists/engineer.md"}],
            "source_findings": origins,
            "source_finding_mapping": [{"source_id": origin["id"], "candidate_id": "F1"} for origin in origins],
        }
    }
    path = tmp_path / "result.json"
    path.write_text(json.dumps(report), encoding="utf-8", newline="\n")
    return path, origins


def test_finding_evidence_preserves_every_direct_origin(finding_with_direct_sources: tuple[Path, list[dict]]) -> None:
    """Keep distinct online IDs and complete original records when one action reconciles multiple reports."""
    path, origins = finding_with_direct_sources
    original = path.read_bytes()

    evidence = _load_finder().finding_evidence(path, "F1")

    assert evidence["source_origins"] == origins
    assert evidence["author_evidence"] == [
        {"author": "Software engineer", "path": str(path.parent / "specialists/engineer.md")}
    ]
    # Retain the report's portable evidence label beside its resolved native filesystem path.
    assert evidence["artifact_evidence"] == [  # path-sep: native
        {"evidence": "specialists/engineer.md", "path": str(path.parent / "specialists/engineer.md")}
    ]
    assert evidence["source_evidence"] == ["widget.py:7"]
    assert evidence["review_notes"] == str(path.parent / "review-notes.md")
    assert path.read_bytes() == original


@pytest.mark.parametrize("supplemental", ["absent", "empty"])
def test_finding_evidence_allows_no_supplemental_origins(
    finding_with_direct_sources: tuple[Path, list[dict]], supplemental: str
) -> None:
    """Do not require an optional online-origin ledger to read a finding's ordinary source evidence."""
    path, _origins = finding_with_direct_sources
    report = json.loads(path.read_bytes())
    for field in ("source_findings", "source_finding_mapping"):
        if supplemental == "absent":
            del report["metadata"][field]
        else:
            report["metadata"][field] = []
    path.write_text(json.dumps(report), encoding="utf-8", newline="\n")

    evidence = _load_finder().finding_evidence(path, "F1")

    assert evidence["source_origins"] == []
    assert evidence["source_evidence"] == ["widget.py:7"]


@pytest.mark.parametrize("damage", ["missing-relation", "conflicting-relation", "missing-origin"])
def test_finding_evidence_rejects_incomplete_direct_origin_relations(
    finding_with_direct_sources: tuple[Path, list[dict]], damage: str
) -> None:
    """Reject broken relevant associations instead of silently losing one of a finding's source obligations."""
    path, _origins = finding_with_direct_sources
    report = json.loads(path.read_bytes())
    metadata = report["metadata"]
    if damage == "missing-relation":
        metadata["source_finding_mapping"].pop()
    elif damage == "conflicting-relation":
        metadata["source_finding_mapping"].append({"source_id": "review-comment-1", "candidate_id": "F2"})
    else:
        metadata["source_findings"].pop()
    path.write_text(json.dumps(report), encoding="utf-8", newline="\n")

    with pytest.raises(LookupError):
        _load_finder().finding_evidence(path, "F1")


def test_finding_evidence_keeps_batched_origin_relations(
    finding_with_direct_sources: tuple[Path, list[dict]],
) -> None:
    """Preserve existing batched original records and reconciliation while fixing direct-review intake."""
    path, direct_origins = finding_with_direct_sources
    report = json.loads(path.read_bytes())
    origins = [
        {
            "finding_id": f"source.software-engineer.origin-{index}",
            "original": {"id": f"origin-{index}", "summary": origin["body"]},
            "original_text": json.dumps(origin),
            "manifest_path": "source/specialist-manifest.json",
            "manifest_sha256": "a" * 64,
            "output_sha256": "b" * 64,
            "pass": {"role": "sw-engineer", "output_path": "specialists/engineer.md"},
            "disposition": "unresolved",
        }
        for index, origin in enumerate(direct_origins, 1)
    ]
    reconciliation = {"invariant": "Reject empty input.", "rationale": "One guard covers both input paths."}
    report["metadata"].update(
        source_findings=origins,
        source_finding_mapping={origin["finding_id"]: ["F1"] for origin in origins},
        source_finding_reconciliation={"F1": reconciliation},
    )
    path.write_text(json.dumps(report), encoding="utf-8", newline="\n")

    evidence = _load_finder().finding_evidence(path, "F1")

    assert evidence["source_origins"] == origins
    assert evidence["source_reconciliation"] == reconciliation

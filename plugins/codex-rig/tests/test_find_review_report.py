"""Regression checks for selecting assessed code-review reports for remediation."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from test_review_completion_gate import _assessed_pr, _module


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
FINDER_PATH = PLUGIN_ROOT / "shared" / "find-review-report.py"
CREATE_RUN_PATH = PLUGIN_ROOT / "shared" / "create_run.py"


@pytest.fixture(name="assessed_local")
def _assessed_local(tmp_path: Path) -> Path:
    """Build a complete local artifact using the existing real producer fixture."""
    run = _assessed_pr.__wrapped__(tmp_path)
    result_path = run / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["metadata"]["scope"] = "working-tree"
    handoff_path = run / "final-handoff.json"
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    handoff["tables"] = []
    handoff["source_records"] = []
    handoff["source_coverage"] = {
        "source_records_total": 0,
        "represented_source_records_total": 0,
        "omitted_source_records_total": 0,
    }
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
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


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

    def test_older_nested_candidate_does_not_block_assessed_run(self, tmp_path: Path) -> None:
        """Prefer a promoted later run over an abandoned earlier candidate."""
        finder = _load_finder()
        _write_nested_report(tmp_path, "run-009", result_name="result.candidate.json")
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

    def test_older_nested_closed_run_does_not_block_assessed_run(self, tmp_path: Path) -> None:
        """Allow a later assessment to supersede an earlier terminal close."""
        finder = _load_finder()
        _write_nested_report(tmp_path, "run-009", review_status="closed")
        assessed = _write_nested_report(tmp_path, "run-010")

        selected = finder.find_latest_review_report("123", [tmp_path])

        assert selected == assessed

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

        with pytest.raises(LookupError, match="missing-matching-review-report"):
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
        "pr_name,run_name",
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


def test_only_unavailable_reports_require_a_new_code_review(tmp_path: Path) -> None:
    """Do not let remediation consume an operational diagnostic as source findings."""
    finder = _load_finder()
    _write_report(tmp_path, "2026-08-10T11-00-00Z", unavailable=True)

    with pytest.raises(LookupError, match="matching-review-unavailable-rerun-code-review"):
        finder.find_latest_review_report("https://github.com/acme/widgets/pull/123", [tmp_path])


@pytest.mark.parametrize("intake", ["explicit", "target"])
def test_pr_metadata_only_report_is_rejected(tmp_path: Path, intake: str) -> None:
    """PR intake must not accept a disposition without the producer's required evidence."""
    report_dir = tmp_path / "run"
    report_dir.mkdir()
    (report_dir / "pr.json").write_text(json.dumps({"number": 123}), encoding="utf-8")
    result = report_dir / "result.json"
    result.write_text(
        json.dumps({"metadata": {"scope": "pr", "review_decision": {"recommendation": "needs-more-work"}}}),
        encoding="utf-8",
    )

    args = ["--result", str(result)] if intake == "explicit" else ["--target", "123", "--reports-dir", str(tmp_path)]
    completed = subprocess.run([sys.executable, str(FINDER_PATH), *args], capture_output=True, text=True, check=False)

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert "review-validation-failed:" in completed.stderr


@pytest.mark.integration
@pytest.mark.parametrize("schema", [1, 2])
def test_pr_intake_revalidates_recorded_producer_across_sessions(tmp_path: Path, schema: int) -> None:
    """Retain validated historical PR intake without confusing the current thread with the producer."""
    run = _assessed_pr.__wrapped__(tmp_path)
    path = run / "result.json"
    if schema == 1:
        result = json.loads(path.read_text(encoding="utf-8"))
        result.pop("schema_version")
        path.write_text(json.dumps(result), encoding="utf-8")
        routing_path = run / "pr-routing.json"
        routing = json.loads(routing_path.read_text(encoding="utf-8"))
        routing["local_checkout_command"] = "gh pr checkout 123"
        routing_path.write_text(json.dumps(routing), encoding="utf-8")
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
@pytest.mark.parametrize("scope", ["working-tree", "path", "commit"])
def test_explicit_local_report_requires_complete_validation(assessed_local: Path, scope: str) -> None:
    """Accept real local artifacts through both producer validators before intake."""
    result = json.loads(assessed_local.read_text(encoding="utf-8"))
    result["metadata"]["scope"] = scope
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
        json.dumps({"metadata": {"scope": scope, "review_decision": {"recommendation": "needs-more-work"}}}),
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

    with pytest.raises(LookupError, match="^invalid-review-report-rerun-code-review$"):
        _load_finder().find_latest_review_report("123", [tmp_path])


@pytest.mark.parametrize(
    "metadata,diagnostic",
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
    """Reject unassessed or malformed local reports with their existing diagnostic."""
    result = tmp_path / "result.json"
    result.write_text(json.dumps({"metadata": metadata}), encoding="utf-8")

    with pytest.raises(LookupError, match=f"^{diagnostic}$"):
        _load_finder().require_assessed_review_result(result)


@pytest.mark.parametrize("scope", ["working-tree", "path", "commit"])
def test_explicit_local_candidate_still_requires_promotion(tmp_path: Path, scope: str) -> None:
    """Local scope support must not make a draft result consumable."""
    candidate = tmp_path / "result.candidate.json"
    candidate.write_text(
        json.dumps({"metadata": {"scope": scope, "review_decision": {"recommendation": "needs-more-work"}}}),
        encoding="utf-8",
    )

    with pytest.raises(LookupError, match="^matching-review-candidate-unpromoted:"):
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


def test_explicit_closed_report_is_rejected_as_remediation_input(tmp_path: Path) -> None:
    """Do not treat a terminal proposal-level close decision as source findings."""
    finder = _load_finder()
    closed = _write_closed_report(tmp_path, "2026-08-10T11-00-00Z")

    with pytest.raises(LookupError, match="matching-review-closed-not-remediable"):
        finder.require_assessed_review_result(closed)


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


def test_explicit_candidate_is_not_accepted_as_validated_review(tmp_path: Path) -> None:
    """Never let an explicit candidate path bypass full artifact validation."""
    finder = _load_finder()
    candidate = _write_candidate_report(tmp_path, "2026-08-10T11-00-00Z")

    with pytest.raises(LookupError, match="matching-review-candidate-unpromoted"):
        finder.require_assessed_review_result(candidate)

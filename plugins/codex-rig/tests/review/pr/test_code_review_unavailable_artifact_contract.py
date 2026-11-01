"""End-to-end contract checks for unavailable PR-review artifacts.

An unavailable review is a terminal process failure, not a source verdict. These tests build the smallest accepted
artifact on disk, then exercise both standalone validators and the real finalizer to prove that recovery text, checkout
state, confidence accounting and result paths stay bounded and that no assessed-review content can leak in.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from _modules import load_module

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
REVIEW_VALIDATOR_PATH = PLUGIN_ROOT / "skills" / "code-review" / "validate_artifacts.py"
SHARED_VALIDATOR_PATH = PLUGIN_ROOT / "shared" / "validate-artifacts.py"
GATE_IDS = ("lint", "format", "types", "tests", "review")
GH_CHECKOUT_FAILURE = {
    "command": "gh pr checkout https://github.com/Borda/AI-Rig/pull/123",
    "code": "github-network:local-pr-checkout",
    "diagnostics": {
        "exit_code": 1,
        "failure_class": "github-network",
        "failure_reason": "connection-reset",
        "label": "local-pr-checkout",
    },
}
ACCESS_RECOVERY = "Check effective runtime access; if required access is missing or unknown and requests are allowed, request runtime approval for the complete collector. Respect an explicit denial or non-overridable restriction; retry only after approval or an evidenced state change."
DIRTY_CODE = "collector:dirty-pr-worktree-before-pr-checkout"
DIRTY_ACTION = "Preserve or move the local changes that overlap PR files, then start a fresh collector run."
CURRENT_HEAD, EXPECTED_HEAD = "a" * 40, "b" * 40
HEAD_DIAGNOSTIC = f"Worktree preflight: local head `{CURRENT_HEAD}`; expected PR head `{EXPECTED_HEAD}`."
CLAIM = "The PR was reviewed and approved for merging."
DEFAULT_OVERLAP = ("pkg/item.py",)
NO_DEDUCTION_NOTE = " No additional deduction; the canonical source-verification gap accounts for this limitation."


def _load_module(path: Path, name: str) -> ModuleType:
    """Load one standalone validator module from its shipped path."""
    return load_module(name, path)


@pytest.fixture
def review_validator() -> ModuleType:
    """Load the standalone code-review artifact validator."""
    return _load_module(REVIEW_VALIDATOR_PATH, "code_review_validator")


@pytest.fixture
def shared_validator() -> ModuleType:
    """Load the shared artifact validator that also checks code-review results."""
    return _load_module(SHARED_VALIDATOR_PATH, "shared_artifact_validator")


def _write_unavailable_artifact(
    out_dir: Path,
    *,
    decision: bool = False,
    finding_table: bool = False,
    extra_result: dict[str, object] | None = None,
    extra_notes: str = "",
    checkout_state: dict[str, object] | None = None,
) -> Path:
    """Write the smallest terminal PR-collection artifact accepted by both validators."""
    for gate_id in GATE_IDS:
        for suffix in ("command.txt", "stdout.txt", "stderr.txt"):
            (out_dir / f"{gate_id}.{suffix}").write_text("", encoding="utf-8")
    checks = [
        {
            "id": gate_id,
            "status": "not-applicable",
            "exit_code": 0,
            "duration_seconds": 0.0,
            "command_path": f"{gate_id}.command.txt",
            "stdout": f"{gate_id}.stdout.txt",
            "stderr": f"{gate_id}.stderr.txt",
            "reason": "PR evidence collection stopped before review gates.",
        }
        for gate_id in GATE_IDS
    ]
    (out_dir / "gates.json").write_text(
        json.dumps({"status": "pass", "checks_failed": [], "checks": checks}), encoding="utf-8"
    )
    (out_dir / "pr-error.txt").write_text("github-network:gh-pr-view\n", encoding="utf-8")
    (out_dir / "pr-target.txt").write_text("123\n", encoding="utf-8")
    if checkout_state is not None:
        (out_dir / "checkout-state.json").write_text(json.dumps(checkout_state), encoding="utf-8")
    recovery_action = ACCESS_RECOVERY
    if checkout_state is not None:
        recovery_action += " Inspect the local checkout state before retrying."
    notes = (
        "# PR Review Availability: unavailable\n\n"
        "Source findings: not assessed\n\n"
        "Merge decision: not made\n\n"
        "Process diagnostic: `github-network:gh-pr-view`. This is a workflow/integration failure, not a PR finding or "
        "merge block.\n\n"
        f"Recovery: {recovery_action}\n\n"
        "Evidence: `pr-error.txt`.\n"
    )
    if finding_table:
        notes += (
            "\n## Review Findings and Merge Blocks\n\n"
            "| Finding / area | Required change | Evidence | Status |\n"
            "| --- | --- | --- | --- |\n"
            "| Wrong | Remove this table. | Fixture. | Required |\n"
        )
    notes += extra_notes
    (out_dir / "review-notes.md").write_text(notes, encoding="utf-8")
    metadata: dict[str, object] = {
        "scope": "pr",
        "risk_tier": "HIGH_RISK",
        "review_status": "unavailable",
        "collection_failure": {"code": "github-network:gh-pr-view", "artifact": "pr-error.txt"},
        "confidence_gaps": [
            "Core PR source verification did not complete; no source review or merge decision was made."
        ],
        "confidence_gap_closures": [
            {
                "gap": "Core PR source verification did not complete; no source review or merge decision was made.",
                "status": "unresolved",
                "rationale": (
                    "A local checkout command may have changed state, but no verified source bundle was produced."
                    if checkout_state is not None
                    else "Core source verification did not complete; retained collection artifacts may be partial and were not assessed."
                ),
            }
        ],
        "confidence_recovery": {
            "initial_confidence": 0.9,
            "final_confidence": 0.9,
            "status": "fair",
            "evidence": [
                "The classified collection failure and conservative checkout-state evidence were retained."
                if checkout_state is not None
                else "The classified collection failure and any current-attempt collector artifacts were retained."
            ],
            "recovery_actions": ["Stopped before source review."],
            "remaining_limits": [
                "PR correctness was not assessed; inspect local checkout state before retrying."
                if checkout_state is not None
                else "PR correctness was not assessed."
            ],
        },
    }
    if decision:
        metadata["review_decision"] = {"recommendation": "needs-more-work", "summary": "Wrong.", "rationale": "Wrong."}
    result_path = out_dir / "result.json"
    result = {
        "status": "fail",
        "checks_run": list(GATE_IDS),
        "checks_failed": [],
        "findings": {"critical": 0, "high": 0, "medium": 0, "low": 0},
        "confidence": 0.9,
        "artifact_path": str(result_path),
        "metadata": metadata,
    }
    if extra_result:
        result.update(extra_result)
    result_path.write_text(json.dumps(result), encoding="utf-8")
    return result_path


def _judged_profile(metadata: dict, checkout_started: bool = False) -> None:
    """Prepare explicit report-correctness judgment for current unavailable handoffs."""
    gap = "(-0.10) Residual uncertainty in the failure report and its production and checks."
    metadata["confidence_gaps"] = [gap]
    metadata["confidence_gap_closures"] = [
        {
            "gap": gap,
            "status": "unresolved",
            "rationale": "The retained records do not establish every aspect of report correctness; this is the operator's subjective judgment, not a substitute for a known gap.",
        }
    ]
    metadata["confidence_recovery"] = {
        "initial_confidence": 0.9,
        "final_confidence": 0.9,
        "status": "fair",
        "evidence": [
            "Confidence assesses the correctness of this failure report and its production and checks; the score is a subjective judgment.",
            "The classified collection failure and conservative checkout-state evidence were retained."
            if checkout_started
            else "The classified collection failure and any current-attempt collector artifacts were retained.",
        ],
        "recovery_actions": ["Stopped before source review."],
        "remaining_limits": [
            "(-0.00) PR correctness was not assessed; inspect local checkout state before retrying."
            if checkout_started
            else "(-0.00) PR correctness was not assessed.",
            "(-0.00) Confidence is a subjective judgment of report correctness and its production and checks.",
        ],
    }


def _account_deductions(metadata: dict) -> None:
    """Rewrite historical confidence text into the complete visible-deduction profile."""
    closure = metadata["confidence_gap_closures"][0]
    closure["rationale"] = "(-0.10) " + closure["rationale"]
    previous_limit = metadata["confidence_recovery"]["remaining_limits"][0]
    metadata["confidence_recovery"]["remaining_limits"] = [f"(-0.00) {previous_limit}{NO_DEDUCTION_NOTE}"]


def _keep_historical(metadata: dict) -> None:
    """Leave the historical no-deduction confidence text untouched."""


@dataclass
class UnavailableRun:
    """A blocked-preflight unavailable run on disk plus the metadata and handoff drafts to finalize it."""

    run_dir: Path
    result_path: Path
    metadata: dict[str, Any]
    handoff: dict[str, Any]
    preflight_path: Path
    notes_path: Path
    dirty_paths: Sequence[str]


def _build_dirty_preflight_run(
    run_dir: Path,
    *,
    status: str = "blocked-pr-dirty-paths",
    dirty: Sequence[str] = DEFAULT_OVERLAP,
    changed: Sequence[str] = DEFAULT_OVERLAP,
    recorded: Sequence[str] = DEFAULT_OVERLAP,
) -> UnavailableRun:
    """Write an unavailable run whose collector stopped on a dirty-worktree preflight, with unfinalized drafts."""
    result_path = _write_unavailable_artifact(run_dir)
    result = json.loads(result_path.read_bytes())
    result_path.unlink()
    metadata = result["metadata"]
    _judged_profile(metadata)
    metadata["collection_failure"]["code"] = DIRTY_CODE
    (run_dir / "pr-error.txt").write_text(DIRTY_CODE + "\n", encoding="utf-8", newline="\n")
    notes_path = run_dir / "review-notes.md"
    notes = notes_path.read_text(encoding="utf-8").replace("github-network:gh-pr-view", DIRTY_CODE)
    notes_path.write_text(notes.replace(ACCESS_RECOVERY, DIRTY_ACTION), encoding="utf-8", newline="\n")
    preflight_path = run_dir / "worktree-preflight.json"
    preflight = {
        "status": status,
        "current_head": CURRENT_HEAD,
        "expected_head": EXPECTED_HEAD,
        "dirty_paths": list(dirty),
        "checkout_paths": list(changed),
        "overlapping_paths": list(recorded),
        "pr_paths": list(changed),
        "overlapping_pr_paths": list(recorded),
        "unmerged_paths": [],
        "phase": "before-checkout",
    }
    preflight_path.write_text(json.dumps(preflight), encoding="utf-8", newline="\n")
    handoff = {
        "schema_version": 1,
        "presentation_version": 3,
        "skill": "code-review",
        "branch": "unavailable",
        "outcome": {
            "title": "PR Review Availability",
            "summary": (
                "I stopped before checkout because local changes overlap PR files, so the review has not started. "
                f"Reason: `{DIRTY_CODE}`. {HEAD_DIAGNOSTIC} The collector did not retain a more specific cause."
            ),
        },
        "tables": [],
        "source_records": [],
        "source_coverage": {
            "source_records_total": 0,
            "represented_source_records_total": 0,
            "omitted_source_records_total": 0,
        },
        "remaining": [
            {
                "row_id": "collection-recovery",
                "owner": "code-review",
                "item": f"PR collection stopped at `{DIRTY_CODE}`.",
                "next_action": f"Inspect the classified `dirty-pr-worktree-before-pr-checkout` collector failure. {DIRTY_ACTION} Resume only after a fresh collector run produces and validates the PR source bundle.",
            }
        ],
        "next_steps": ["collection-recovery"],
        "artifacts": [
            {"label": "Collection failure", "path": "pr-error.txt"},
            {"label": "Worktree preflight", "path": "worktree-preflight.json"},
        ],
        "caller_contract": None,
    }
    return UnavailableRun(run_dir, result_path, metadata, handoff, preflight_path, notes_path, dirty)


def _retained_bytes(unavailable: UnavailableRun) -> dict[Path, bytes]:
    """Snapshot the collector evidence that finalization must never rewrite."""
    paths = (unavailable.preflight_path, unavailable.run_dir / "pr-error.txt", unavailable.notes_path)
    return {path: path.read_bytes() for path in paths}


def _finalize(unavailable: UnavailableRun, artifact_path: str) -> tuple[int, dict[str, Any]]:
    """Run the real remediation finalizer with `--promote`; return its exit code and parsed JSON outcome."""
    run_dir = unavailable.run_dir
    metadata_path, handoff_path = run_dir / "metadata-draft.json", run_dir / "handoff-draft.json"
    metadata_path.write_text(json.dumps(unavailable.metadata), encoding="utf-8", newline="\n")
    handoff_path.write_text(json.dumps(unavailable.handoff), encoding="utf-8", newline="\n")
    finalized = subprocess.run(
        [
            sys.executable,
            str(PLUGIN_ROOT / "shared/remediation_finalize.py"),
            "finalize",
            "--skill",
            "code-review",
            "--parent-thread-id",
            "thread",
            "--run",
            str(run_dir),
            "--metadata",
            str(metadata_path),
            "--handoff",
            str(handoff_path),
            "--status",
            "fail",
            "--confidence",
            "0.9",
            "--artifact-path",
            artifact_path,
            "--promote",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return finalized.returncode, json.loads(finalized.stdout)


def _assert_promoted_unavailable(unavailable: UnavailableRun, returncode: int, outcome: dict[str, Any]) -> bytes:
    """Assert finalization promoted a bounded unavailable result and the complete-run lookup returns its report."""
    assert returncode == 0, outcome
    assert outcome["promoted"] is True, outcome
    promoted = json.loads(unavailable.result_path.read_bytes())
    assert promoted["status"] == "fail"
    assert promoted["metadata"]["review_status"] == "unavailable"
    assert promoted["findings"] == dict.fromkeys(("critical", "high", "medium", "low"), 0)
    assert "review_decision" not in promoted["metadata"]
    assert "Source findings: not assessed" in unavailable.notes_path.read_text(encoding="utf-8")
    final_bytes = (unavailable.run_dir / "final.md").read_bytes()
    assert HEAD_DIAGNOSTIC.encode("utf-8") in final_bytes
    assert DIRTY_ACTION.encode("utf-8") in final_bytes
    assert all(path.encode("utf-8") not in final_bytes for path in unavailable.dirty_paths)
    completed = subprocess.run(
        [
            sys.executable,
            str(PLUGIN_ROOT / "shared/find-review-report.py"),
            "--complete-run",
            str(unavailable.run_dir),
            "--parent-thread-id",
            "thread",
        ],
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode()
    assert completed.stdout == final_bytes
    return final_bytes


def _keep_relative_diagnostics(handoff: dict[str, Any], run_dir: Path) -> None:
    """Leave handoff artifact paths relative to the run directory."""


def _make_absolute_diagnostics(handoff: dict[str, Any], run_dir: Path) -> None:
    """Point every handoff artifact path at its absolute location inside the run directory."""
    handoff["artifacts"] = [{**artifact, "path": str(run_dir / artifact["path"])} for artifact in handoff["artifacts"]]


DIAGNOSTIC_PATHS = [
    pytest.param(_keep_relative_diagnostics, id="relative-diagnostics"),
    pytest.param(_make_absolute_diagnostics, id="absolute-diagnostics"),
]


class TestRecovery:
    """Recovery text for an unavailable review stays actionable and tied to the classified cause."""

    def test_network_failure_requests_missing_access_instead_of_blind_retry(self, review_validator: ModuleType) -> None:
        """Network recovery asks for missing access without authorizing a bypass of a denial.

        A collector that failed on the network must tell the operator to request runtime approval, honor an explicit
        denial or non-overridable restriction, and never advise retrying the unchanged collector.
        """
        recovery = review_validator._unavailable_recovery_action("github-network:gh-pr-view", False)

        assert "request runtime approval for the complete collector" in recovery
        assert "missing or unknown" in recovery
        assert "denial" in recovery
        assert "non-overridable" in recovery
        assert "Retry the unchanged collector later" not in recovery

    @pytest.mark.parametrize(
        ("code", "reason", "expected_action"),
        [
            pytest.param(
                "command-failed:pr-head-fetch",
                "repository-unavailable",
                "Confirm the canonical repository identity and availability after a state change, then start a fresh collector run.",
                id="repository-unavailable-no-auth-guess",
            ),
            pytest.param("collector:dirty-pr-worktree-before-pr-checkout", None, DIRTY_ACTION, id="dirty-pr-paths"),
            pytest.param(
                "collector:unresolved-index-before-pr-checkout",
                None,
                "Resolve the Git index entries, then start a fresh collector run.",
                id="unresolved-index",
            ),
        ],
    )
    def test_is_actionable_without_auth_guess(
        self, review_validator: ModuleType, code: str, reason: str | None, expected_action: str
    ) -> None:
        """Fetch and worktree failures map to a recovery tied to the classified safe cause.

        The recovery names the concrete next step (confirm the repository, move overlapping changes, resolve the index)
        instead of guessing that authentication is at fault.
        """
        assert review_validator._unavailable_recovery_action(code, False, reason) == expected_action


class TestArtifactValidation:
    """Both validators accept a terminal unavailable artifact and reject any assessed-review content in it."""

    def test_accepts_artifact_without_assessed_review_evidence(
        self, tmp_path: Path, review_validator: ModuleType, shared_validator: ModuleType
    ) -> None:
        """A terminal collector failure passes both validators and keeps a canonical result artifact.

        The smallest artifact has no source findings, decision, or gate evidence; the review and shared validators must
        still accept it as the final result of a collection that never reached review.
        """
        result_path = _write_unavailable_artifact(tmp_path)

        review_validator._validate_result(tmp_path, result_path, tmp_path, "thread", tmp_path)
        shared_validator.validate("code-review", tmp_path, result_path)

    def test_retains_current_attempt_evidence_without_assessing_it(
        self, tmp_path: Path, review_validator: ModuleType
    ) -> None:
        """Diagnostic files from the failed attempt may stay in the run without inventing source findings.

        A partial `pr.json` and `diff.patch` from the same attempt are evidence of what failed, not an assessment, so
        the validator accepts them beside the unavailable result.
        """
        result_path = _write_unavailable_artifact(tmp_path)
        (tmp_path / "pr.json").write_text(json.dumps({"number": 123, "body": "Contributor intent"}), encoding="utf-8")
        (tmp_path / "diff.patch").write_text("partial current-attempt evidence\n", encoding="utf-8")

        review_validator._validate_result(tmp_path, result_path, tmp_path, "thread", tmp_path)

    @pytest.mark.parametrize(
        ("decision", "finding_table", "extra_result", "extra_notes"),
        [
            pytest.param(True, False, None, "", id="review-decision"),
            pytest.param(False, True, None, "", id="findings-table"),
            pytest.param(False, False, {"recommendations": ["needs-more-work"]}, "", id="recommendations"),
            pytest.param(False, False, {"follow_up": ["merge must be blocked"]}, "", id="follow-up"),
            pytest.param(
                False,
                False,
                {"findings": {"critical": 0, "high": 0, "medium": 0, "low": 0, "source_review": {}}},
                "",
                id="source-review-findings",
            ),
            pytest.param(
                False, False, None, "\n## Decision Summary\n\nRecommendation: needs-more-work\n", id="decision-summary"
            ),
            pytest.param(
                False, False, None, "\nSource assessment: implementation requires changes.\n", id="source-assessment"
            ),
        ],
    )
    def test_rejects_assessed_review_content(
        self,
        tmp_path: Path,
        review_validator: ModuleType,
        decision: bool,
        finding_table: bool,
        extra_result: dict[str, object] | None,
        extra_notes: str,
    ) -> None:
        """Process-failure output stays distinct from a source-review outcome.

        A decision, findings table, recommendation, follow-up, source-review findings or assessment prose in an
        unavailable artifact would read as a verdict on code that was never reviewed.
        """
        result_path = _write_unavailable_artifact(
            tmp_path,
            decision=decision,
            finding_table=finding_table,
            extra_result=extra_result,
            extra_notes=extra_notes,
        )

        with pytest.raises(SystemExit, match="unavailable-review"):
            review_validator._validate_result(tmp_path, result_path, tmp_path, "thread", tmp_path)

    def test_rejects_process_diagnostic_table(self, tmp_path: Path, review_validator: ModuleType) -> None:
        """Workflow failures stay out of the PR findings and action-table visual language.

        A Markdown table in the notes would be read as a findings table, so the validator forbids tables in the process
        diagnostic.
        """
        result_path = _write_unavailable_artifact(tmp_path, extra_notes="\n| Area | Recovery |\n| --- | --- |\n")

        with pytest.raises(SystemExit, match="unavailable-review-process-table-forbidden"):
            review_validator._validate_result(tmp_path, result_path, tmp_path, "thread", tmp_path)


class TestCheckoutState:
    """The collector's checkout-state record is accepted only in its bounded schema."""

    @pytest.mark.parametrize(
        "checkout_state",
        [
            pytest.param(
                {"status": "checkout-command-started", "local_state": "changed-or-unknown"},
                id="historical-checkout-started",
            ),
            pytest.param(
                {"status": "checkout-command-succeeded-unverified", "local_state": "changed-or-unknown"},
                id="historical-succeeded-unverified",
            ),
            pytest.param(
                {
                    "status": "checkout-command-succeeded-unverified",
                    "local_state": "changed-or-unknown",
                    "gh_checkout_failure": None,
                },
                id="collector-succeeded-unverified",
            ),
            pytest.param(
                {
                    "status": "gh-checkout-failed-recovery-assessment-started",
                    "local_state": "changed-or-unknown",
                    "gh_checkout_failure": GH_CHECKOUT_FAILURE,
                },
                id="collector-failed-gh-recovery",
            ),
            pytest.param(
                {
                    "status": "checkout-command-succeeded-unverified",
                    "local_state": "changed-or-unknown",
                    "gh_checkout_failure": GH_CHECKOUT_FAILURE,
                },
                id="collector-fallback-remains-unverified",
            ),
        ],
    )
    def test_accepts_legacy_and_current_records(
        self, tmp_path: Path, review_validator: ModuleType, checkout_state: dict[str, object]
    ) -> None:
        """Bounded legacy and current collector checkout-state records are accepted.

        Runs written before and after the collector gained `gh_checkout_failure` must both validate.
        """
        result_path = _write_unavailable_artifact(tmp_path, checkout_state=checkout_state)

        review_validator._validate_result(tmp_path, result_path, tmp_path, "thread", tmp_path)

    @pytest.mark.parametrize(
        "checkout_state",
        [
            pytest.param({"status": "unknown", "local_state": "changed-or-unknown"}, id="unknown-status"),
            pytest.param(
                {
                    "status": "checkout-command-succeeded-unverified",
                    "local_state": "changed-or-unknown",
                    "gh_checkout_failure": {**GH_CHECKOUT_FAILURE, "stderr": "credential=secret"},
                },
                id="failure-diagnostics-raw-stderr",
            ),
            pytest.param(
                {
                    "status": "gh-checkout-failed-recovery-assessment-started",
                    "local_state": "changed-or-unknown",
                    "gh_checkout_failure": {**GH_CHECKOUT_FAILURE, "diagnostics": {"label": "wrong"}},
                },
                id="failure-diagnostics-wrong-label",
            ),
            pytest.param(
                {
                    "status": "gh-checkout-failed-recovery-assessment-started",
                    "local_state": "changed-or-unknown",
                    "gh_checkout_failure": {
                        **GH_CHECKOUT_FAILURE,
                        "diagnostics": {
                            "failure_class": "github-network",
                            "label": "local-pr-checkout",
                            "stderr": "credential=secret",
                        },
                    },
                },
                id="failure-diagnostics-extra-field",
            ),
            pytest.param(
                {
                    "status": "gh-checkout-failed-recovery-assessment-started",
                    "local_state": "changed-or-unknown",
                    "gh_checkout_failure": {
                        **GH_CHECKOUT_FAILURE,
                        "command": "gh pr checkout https://token@github.com/Borda/AI-Rig/pull/123",
                    },
                },
                id="failure-command-credential-url",
            ),
            pytest.param(
                {
                    "status": "checkout-command-succeeded-unverified",
                    "local_state": "changed-or-unknown",
                    "gh_checkout_failure": None,
                    "unexpected": True,
                },
                id="state-extra-field",
            ),
        ],
    )
    def test_rejects_malformed_records(
        self, tmp_path: Path, review_validator: ModuleType, checkout_state: dict[str, object]
    ) -> None:
        """Checkout-state fields outside the bounded collector schema are rejected.

        An unknown status, raw stderr, a mislabelled or padded diagnostics block, a credential-bearing URL or an extra
        field could smuggle unbounded or secret text into the artifact.
        """
        result_path = _write_unavailable_artifact(tmp_path, checkout_state=checkout_state)

        with pytest.raises(SystemExit, match="unavailable-review-invalid-checkout-state"):
            review_validator._validate_result(tmp_path, result_path, tmp_path, "thread", tmp_path)

    def test_diagnostic_omits_collector_command_and_diagnostics(
        self, tmp_path: Path, review_validator: ModuleType
    ) -> None:
        """The rendered checkout diagnostic carries only the fixed status, never command or diagnostic payloads.

        The collector record holds the failed `gh pr checkout` command and error code; the user-facing line must name
        just the status so neither can leak into the report.
        """
        state = {
            "status": "gh-checkout-failed-recovery-assessment-started",
            "local_state": "changed-or-unknown",
            "gh_checkout_failure": GH_CHECKOUT_FAILURE,
        }
        _write_unavailable_artifact(tmp_path, checkout_state=state)

        diagnostic = review_validator._unavailable_checkout_diagnostic(tmp_path)

        assert diagnostic == (
            "Checkout diagnostic: local worktree state is changed or unknown after "
            "`gh-checkout-failed-recovery-assessment-started`."
        )
        assert GH_CHECKOUT_FAILURE["command"] not in diagnostic
        assert GH_CHECKOUT_FAILURE["code"] not in diagnostic


class TestConfidenceProfile:
    """Unavailable-review confidence is either a complete historical profile or a complete deduction profile."""

    @pytest.mark.integration
    @pytest.mark.parametrize(
        "profile",
        [
            pytest.param(_keep_historical, id="historical-profile"),
            pytest.param(_account_deductions, id="deduction-profile"),
        ],
    )
    @pytest.mark.parametrize(
        "checkout_state",
        [
            None,
            pytest.param(
                {"status": "checkout-command-started", "local_state": "changed-or-unknown"},
                id="checkout-state-retained",
            ),
        ],
    )
    def test_accepts_complete_historical_or_deduction_profile(
        self,
        tmp_path: Path,
        review_validator: ModuleType,
        shared_validator: ModuleType,
        checkout_state: dict[str, object] | None,
        profile: Callable[[dict], None],
    ) -> None:
        """Historical metadata stays readable and complete transparent accounting is accepted.

        Both validators must pass either the unchanged historical text or the rewritten `(-0.10)` / `(-0.00)` profile,
        with and without a retained checkout state.
        """
        result_path = _write_unavailable_artifact(tmp_path, checkout_state=checkout_state)
        result = json.loads(result_path.read_text(encoding="utf-8"))
        profile(result["metadata"])
        result_path.write_text(json.dumps(result), encoding="utf-8")

        review_validator._validate_result(tmp_path, result_path, tmp_path, "thread", tmp_path)
        shared_validator.validate("code-review", tmp_path, result_path)

    @pytest.mark.parametrize(
        ("rationale_prefix", "remaining_limits", "error"),
        [
            pytest.param(
                "(-0.10) ", ["PR correctness was not assessed."], "confidence-recovery", id="new-rationale-old-limit"
            ),
            pytest.param(
                "",
                [f"(-0.00) PR correctness was not assessed.{NO_DEDUCTION_NOTE}"],
                "confidence-recovery",
                id="old-rationale-new-limit",
            ),
            pytest.param(
                "(-0.09) ",
                [f"(-0.00) PR correctness was not assessed.{NO_DEDUCTION_NOTE}"],
                "confidence-closures",
                id="shortfall-under-counted",
            ),
            pytest.param(
                "(-0.11) ",
                [f"(-0.00) PR correctness was not assessed.{NO_DEDUCTION_NOTE}"],
                "confidence-closures",
                id="shortfall-over-counted",
            ),
            pytest.param(
                "(−0.10) ",
                [f"(-0.00) PR correctness was not assessed.{NO_DEDUCTION_NOTE}"],
                "confidence-closures",
                id="non-ascii-minus",
            ),
            pytest.param(
                "(-0.1) ",
                [f"(-0.00) PR correctness was not assessed.{NO_DEDUCTION_NOTE}"],
                "confidence-closures",
                id="wrong-decimal-precision",
            ),
            pytest.param(
                "(-0.10) ",
                [f"(-0.01) PR correctness was not assessed.{NO_DEDUCTION_NOTE}"],
                "confidence-recovery",
                id="overlap-double-counted",
            ),
        ],
    )
    def test_rejects_mixed_or_misaccounted_profiles(
        self,
        tmp_path: Path,
        review_validator: ModuleType,
        rationale_prefix: str,
        remaining_limits: list[str],
        error: str,
    ) -> None:
        """Partial migrations and deductions inconsistent with the canonical 0.90 are rejected.

        Mixing a new-style rationale with an old-style limit (or the reverse), or a shortfall that is under-counted,
        over-counted, written with a non-ASCII minus, wrong precision, or double-counted, must fail as non-canonical.
        """
        result_path = _write_unavailable_artifact(tmp_path)
        result = json.loads(result_path.read_text(encoding="utf-8"))
        metadata = result["metadata"]
        closure = metadata["confidence_gap_closures"][0]
        closure["rationale"] = rationale_prefix + closure["rationale"]
        metadata["confidence_recovery"]["remaining_limits"] = remaining_limits
        result_path.write_text(json.dumps(result), encoding="utf-8")

        with pytest.raises(SystemExit, match=f"unavailable-review-{error}-must-be-canonical"):
            review_validator._validate_result(tmp_path, result_path, tmp_path, "thread", tmp_path)

    @pytest.mark.parametrize("candidate_name", ["result.candidate.json", "renamed-candidate.json"])
    def test_fresh_candidate_requires_visible_deductions(
        self, tmp_path: Path, review_validator: ModuleType, candidate_name: str
    ) -> None:
        """New candidates cannot use the historical no-deduction compatibility, while the old result still passes.

        A schema-3 candidate with historical confidence text is refused under any file name, in both the raising and the
        collected-errors paths, and validating it leaves the historical result file untouched.
        """
        historical_path = _write_unavailable_artifact(tmp_path)
        historical_bytes = historical_path.read_bytes()
        candidate_path = tmp_path / candidate_name
        candidate_result = json.loads(historical_bytes)
        candidate_result["schema_version"] = 3
        candidate_path.write_text(json.dumps(candidate_result), encoding="utf-8")

        with pytest.raises(SystemExit, match="unavailable-review-candidate-confidence-deductions-required"):
            review_validator._validate_result(tmp_path, candidate_path, tmp_path, "thread", tmp_path)
        report = review_validator.collect_result_errors(tmp_path, candidate_path, tmp_path, "thread", tmp_path)
        assert {"step": "unavailable", "code": "unavailable-review-candidate-confidence-deductions-required"} in report[
            "errors"
        ]
        review_validator._validate_result(tmp_path, historical_path, tmp_path, "thread", tmp_path)
        assert historical_path.read_bytes() == historical_bytes


@pytest.mark.integration
class TestDirtyPreflightFinalization:
    """The finalizer consumes bounded collector overlap evidence into an actionable unavailable result."""

    @pytest.mark.parametrize(
        ("dirty", "changed", "recorded"),
        [
            pytest.param(["pkg/item.py"], ["pkg/item.py"], ["pkg/item.py"], id="exact"),
            pytest.param(["pkg"], ["pkg/item.py"], ["pkg"], id="dirty-ancestor"),
            pytest.param(["pkg/item.py"], ["pkg"], ["pkg/item.py"], id="dirty-descendant"),
            pytest.param(["Widget.py"], ["widget.py"], ["Widget.py"], id="collector-case-alias"),
        ],
    )
    def test_promotes_proven_overlap(
        self, tmp_path: Path, dirty: list[str], changed: list[str], recorded: list[str]
    ) -> None:
        """An overlap between dirty and PR paths is promoted without reassessing source or reading original paths.

        Exact, ancestor, descendant and case-aliased overlaps recorded by the collector yield a promoted unavailable
        result whose report omits the dirty paths and whose collector evidence stays byte-identical.
        """
        unavailable = _build_dirty_preflight_run(tmp_path, dirty=dirty, changed=changed, recorded=recorded)
        before = _retained_bytes(unavailable)

        returncode, outcome = _finalize(unavailable, str(unavailable.result_path))

        assert _retained_bytes(unavailable) == before
        _assert_promoted_unavailable(unavailable, returncode, outcome)

    @pytest.mark.parametrize(
        ("dirty", "changed", "recorded", "status"),
        [
            pytest.param(
                ["pkg/item.py"],
                ["pkg/item.py"],
                ["pkg/item.py", "unknown.py"],
                "blocked-pr-dirty-paths",
                id="overlap-not-dirty",
            ),
            pytest.param(
                ["pkg/item.py", "other.py"],
                ["pkg/item.py", "other.py"],
                ["other.py"],
                "blocked-pr-dirty-paths",
                id="missing-exact-collision",
            ),
            pytest.param(
                ["pkg", "other.py"],
                ["pkg/item.py", "other.py"],
                ["other.py"],
                "blocked-pr-dirty-paths",
                id="missing-ancestor-collision",
            ),
            pytest.param(
                ["pkg/item.py"],
                ["pkg/item.py"],
                ["pkg/item.py"],
                "safe-unrelated-dirty-paths",
                id="safe-status-hides-overlap",
            ),
        ],
    )
    def test_rejects_unproven_overlap(
        self, tmp_path: Path, dirty: list[str], changed: list[str], recorded: list[str], status: str
    ) -> None:
        """A recorded overlap that does not match the dirty and PR paths is refused and nothing is promoted.

        An overlap path that is not dirty, a collision the record misses, or a safe status that hides an overlap makes
        the preflight invalid; the result file is never created and collector evidence stays untouched.
        """
        unavailable = _build_dirty_preflight_run(
            tmp_path, status=status, dirty=dirty, changed=changed, recorded=recorded
        )
        before = _retained_bytes(unavailable)

        returncode, outcome = _finalize(unavailable, str(unavailable.result_path))

        assert _retained_bytes(unavailable) == before
        assert returncode == 1, outcome
        assert outcome["promoted"] is False, outcome
        assert "unavailable-review-worktree-preflight-invalid" in json.dumps(outcome), outcome
        assert not unavailable.result_path.exists()


def _append_claim(entry: dict[str, Any], key: str) -> None:
    """Append the unsupported PR claim to one existing handoff field."""
    entry[key] = f"{entry[key]} {CLAIM}"


#: Handoff edits that put a free-text PR claim into a field the finalizer must bound; each one must be refused.
CLAIM_INJECTIONS = [
    pytest.param(lambda handoff: _append_claim(handoff["remaining"][0], "next_action"), id="action"),
    pytest.param(lambda handoff: handoff["artifacts"][0].update(label=CLAIM), id="artifact-label"),
    pytest.param(lambda handoff: handoff["artifacts"][0].update(path=CLAIM), id="artifact-path"),
    pytest.param(
        lambda handoff: handoff["artifacts"][0].update(path=f"{CLAIM}/../pr-error.txt"), id="artifact-path-traversal"
    ),
    pytest.param(
        lambda handoff: handoff["artifacts"][0].update(path=f"{CLAIM}\\..\\pr-error.txt"),
        id="artifact-path-windows-traversal",
    ),
    pytest.param(lambda handoff: _append_claim(handoff["outcome"], "summary"), id="summary"),
    pytest.param(lambda handoff: handoff["outcome"].update(title=CLAIM), id="title"),
    pytest.param(lambda handoff: _append_claim(handoff["remaining"][0], "item"), id="item"),
    pytest.param(lambda handoff: handoff["remaining"][0].update(owner=CLAIM), id="owner"),
    pytest.param(
        lambda handoff: (handoff["remaining"][0].update(row_id=CLAIM), handoff.update(next_steps=[CLAIM])),
        id="row-id",
    ),
    pytest.param(
        lambda handoff: handoff.update(commit_disposition={"status": "none", "reason": CLAIM, "evidence": "x"}),
        id="commit",
    ),
]
#: Handoff edits whose claim lands in a field that is dropped or ignored for unavailable results, so promotion succeeds.
DROPPED_CLAIMS = [
    pytest.param(lambda handoff: None, id="no-claim"),
    pytest.param(
        lambda handoff: handoff.update(confidence={"score": 0.9, "band": "fair", "limits": [CLAIM], "gaps": []}),
        id="confidence-limit",
    ),
    pytest.param(
        lambda handoff: handoff.update(verification=[{"check": "lint", "status": "pass", "evidence": CLAIM}]),
        id="verification-evidence",
    ),
]


class TestBoundedTextFinalizer:
    """Fresh unavailable results keep classified recovery and reject free-text PR claims."""

    @pytest.mark.parametrize("inject", CLAIM_INJECTIONS)
    def test_rejects_free_text_claim(self, tmp_path: Path, inject: Callable[[dict[str, Any]], object]) -> None:
        """A free-text claim that the PR was reviewed is refused in every bounded handoff field.

        Action, labels, paths (including traversal), summary, title, item, owner, row id and commit disposition are all
        fields where text could assert a review that never ran; finalization must fail and promote nothing.
        """
        unavailable = _build_dirty_preflight_run(tmp_path)
        inject(unavailable.handoff)

        returncode, outcome = _finalize(unavailable, str(unavailable.result_path))

        assert returncode != 0, outcome
        assert outcome["promoted"] is False

    @pytest.mark.parametrize("inject", DROPPED_CLAIMS)
    def test_drops_claim_from_unrendered_fields(
        self, tmp_path: Path, inject: Callable[[dict[str, Any]], object]
    ) -> None:
        """Claims placed in fields that unavailable results ignore never reach the rendered report.

        With no claim, or with the claim in confidence limits or verification evidence, finalization still promotes the
        bounded result and the final report never contains the claim.
        """
        unavailable = _build_dirty_preflight_run(tmp_path)
        inject(unavailable.handoff)
        before = _retained_bytes(unavailable)

        returncode, outcome = _finalize(unavailable, str(unavailable.result_path))

        assert _retained_bytes(unavailable) == before
        final_bytes = _assert_promoted_unavailable(unavailable, returncode, outcome)
        assert CLAIM.encode() not in final_bytes


class TestResultPathAnchors:
    """The finalizer resolves equivalent spellings of the result path and refuses paths outside the run."""

    @pytest.mark.parametrize("diagnostics", DIAGNOSTIC_PATHS)
    @pytest.mark.parametrize(
        "result_arg",
        [
            pytest.param(lambda result_path: str(result_path), id="absolute-result"),
            pytest.param(lambda result_path: "result.json", id="relative-result"),
        ],
    )
    def test_equivalent_artifact_paths_promote(
        self,
        tmp_path: Path,
        diagnostics: Callable[[dict[str, Any], Path], None],
        result_arg: Callable[[Path], str],
    ) -> None:
        """Absolute or run-relative result and diagnostic paths all promote the same unavailable result.

        The run directory is the working directory here, so a bare `result.json` and its absolute spelling must be
        treated as the same artifact whether the handoff lists diagnostics relatively or absolutely.
        """
        unavailable = _build_dirty_preflight_run(tmp_path)
        diagnostics(unavailable.handoff, tmp_path)

        returncode, outcome = _finalize(unavailable, result_arg(unavailable.result_path))

        _assert_promoted_unavailable(unavailable, returncode, outcome)

    @pytest.mark.parametrize("diagnostics", DIAGNOSTIC_PATHS)
    @pytest.mark.parametrize(
        "anchor",
        [
            pytest.param(lambda repo, result_path: str(result_path), id="absolute"),
            pytest.param(lambda repo, result_path: "result.json", id="run-relative"),
            pytest.param(lambda repo, result_path: str(result_path.relative_to(repo)), id="repo-relative"),
        ],
    )
    def test_run_anchored_paths_promote_in_nested_report_layout(
        self,
        tmp_path: Path,
        diagnostics: Callable[[dict[str, Any], Path], None],
        anchor: Callable[[Path, Path], str],
    ) -> None:
        """Absolute, run-relative and repo-relative result paths promote inside `.reports/codex/code-review/run-NNN`.

        The reports directory sits below a repository root, so a path relative to that root must resolve to the same run
        result as the absolute and run-relative spellings.
        """
        run_dir = tmp_path / ".reports" / "codex" / "code-review" / "run-001"
        run_dir.mkdir(parents=True)
        unavailable = _build_dirty_preflight_run(run_dir)
        diagnostics(unavailable.handoff, run_dir)

        returncode, outcome = _finalize(unavailable, anchor(tmp_path, unavailable.result_path))

        _assert_promoted_unavailable(unavailable, returncode, outcome)

    @pytest.mark.parametrize("diagnostics", DIAGNOSTIC_PATHS)
    @pytest.mark.parametrize(
        "anchor",
        [
            pytest.param(lambda repo, result_path: str(repo / "other" / "result.json"), id="foreign"),
            pytest.param(lambda repo, result_path: "../../foreign/result.json", id="traversal"),
        ],
    )
    def test_paths_outside_run_are_refused(
        self,
        tmp_path: Path,
        diagnostics: Callable[[dict[str, Any], Path], None],
        anchor: Callable[[Path, Path], str],
    ) -> None:
        """A result path outside the run directory is refused and no result is written in the run.

        A sibling directory path and a `../..` traversal must fail finalization so the finalizer never promotes a result
        outside the run it was asked to finalize.
        """
        run_dir = tmp_path / ".reports" / "codex" / "code-review" / "run-001"
        run_dir.mkdir(parents=True)
        unavailable = _build_dirty_preflight_run(run_dir)
        diagnostics(unavailable.handoff, run_dir)

        returncode, outcome = _finalize(unavailable, anchor(tmp_path, unavailable.result_path))

        assert returncode != 0, outcome
        assert not unavailable.result_path.exists()

"""A ``ci-covered`` tests-gate skip is accepted only against retained CI evidence for the verified head."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
HEAD = "a" * 40
SHORT = HEAD[:12]
BASE = "b" * 40
JOB_URL = "https://ci.example/job/tests"
CHECK_RUN = {
    "name": "pytest (ubuntu, 3.12)",
    "status": "COMPLETED",
    "conclusion": "SUCCESS",
    "detailsUrl": JOB_URL,
}


def _validator() -> Any:
    """Load the code-review validator by path, as the installed skill does."""
    path = PLUGIN_ROOT / "skills" / "code-review" / "validate_artifacts.py"
    specification = importlib.util.spec_from_file_location("codex_rig_ci_covered_validator", path)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _check(reason: str) -> list[dict[str, Any]]:
    """Return a gates check list whose tests row is skipped with ``reason``."""
    return [{"id": "tests", "status": "not-applicable", "reason": reason}, {"id": "lint", "status": "pass"}]


def _run(
    tmp_path: Path,
    reason: str,
    *,
    head: str = HEAD,
    rollup: object = (CHECK_RUN,),
    readiness: str = "complete",
    evidence: dict[str, Any] | None = None,
    patch: str | None = None,
    checks: list[dict[str, Any]] | None = None,
) -> None:
    """Write the retained ``pr.json`` and run the CI-covered binding against ``reason``."""
    (tmp_path / "pr.json").write_text(
        json.dumps({"headRefOid": head, "statusCheckRollup": None if rollup is None else list(rollup)}),
        encoding="utf-8",
    )
    observations = [
        {
            "check": entry.get("name") or entry.get("context"),
            "job_url": entry.get("detailsUrl") or entry.get("targetUrl") or JOB_URL,
            "checkout_oid": head,
            "selection": "pytest",
            "matrix": "Linux, Python 3.12",
            "passing_test": "test_widget",
            **(evidence or {}),
        }
        for entry in (rollup or ())
    ]
    (tmp_path / "review-notes.md").write_text(
        "\n".join(
            "CI test evidence: " + json.dumps(row) for row in {row["check"]: row for row in observations}.values()
        ),
        encoding="utf-8",
    )
    (tmp_path / "diff.patch").write_text(patch or "diff --git a/src/a.py b/src/a.py\n", encoding="utf-8")
    _validator()._validate_ci_tests_skip(tmp_path, checks or _check(reason), HEAD, readiness=readiness, base_oid=BASE)


def test_a_skip_covered_by_a_successful_check_at_the_verified_head_is_accepted(tmp_path: Path) -> None:
    """The cited check completed with SUCCESS for the verified head, so the skip stands."""
    _run(tmp_path, f"ci-covered: {CHECK_RUN['name']} at {SHORT}")


def test_a_status_context_with_a_successful_state_counts(tmp_path: Path) -> None:
    """A legacy commit status reports ``state`` instead of a check-run conclusion."""
    _run(
        tmp_path,
        f"ci-covered: ci/tests at {SHORT}",
        rollup=({"context": "ci/tests", "state": "SUCCESS", "targetUrl": JOB_URL},),
    )


def test_several_cited_checks_separated_by_a_bare_pipe_are_each_verified(tmp_path: Path) -> None:
    """The documented ``|`` separator splits the names, so a matrix of successful jobs is accepted."""
    second = {**CHECK_RUN, "name": "pytest (macos, 3.13)"}
    _run(tmp_path, f"ci-covered: {CHECK_RUN['name']}|{second['name']} at {SHORT}", rollup=(CHECK_RUN, second))


def test_failure_of_an_optional_check_allows_coverage_from_the_required_job(tmp_path: Path) -> None:
    """The local skip cites only the successful job that ran the required selection."""
    optional = {**CHECK_RUN, "name": "optional", "conclusion": "FAILURE"}
    _run(tmp_path, f"ci-covered: {CHECK_RUN['name']} at {SHORT}", rollup=(CHECK_RUN, optional))


@pytest.mark.parametrize(
    ("reason", "conclusion", "readiness", "error"),
    [
        pytest.param(f"ci-failed: tests at {SHORT}", "FAILURE", "blocked", "", id="required-job-failure"),
        pytest.param(f"ci-failed: tests at {SHORT}", "SUCCESS", "blocked", "check-not-failed", id="passing-job"),
        pytest.param(f"ci-failed: tests at {SHORT}", "CANCELLED", "blocked", "check-not-failed", id="cancelled-job"),
        pytest.param(f"ci-failed: tests at {SHORT}", "FAILURE", "complete", "readiness", id="unresolved-failure"),
        pytest.param("required CI tests failed", "FAILURE", "complete", "readiness", id="historical-failure"),
        pytest.param("CI failed: tests", "FAILURE", "blocked", "", id="historical-blocked-receipt"),
    ],
)
def test_required_failure_disposition_binds_the_job_and_readiness(
    tmp_path: Path, reason: str, conclusion: str, readiness: str, error: str
) -> None:
    """A failing required test job supplies evidence of failure and prevents complete verification."""
    rollup = [{**CHECK_RUN, "name": "tests", "conclusion": conclusion}]
    if error:
        with pytest.raises(SystemExit, match=error):
            _run(tmp_path, reason, rollup=rollup, readiness=readiness)
    else:
        _run(tmp_path, reason, rollup=rollup, readiness=readiness)


def test_a_reason_without_the_ci_covered_prefix_is_not_judged(tmp_path: Path) -> None:
    """Other skip reasons keep their own contracts, so this binding leaves them alone."""
    _validator()._validate_ci_tests_skip(tmp_path, _check("documentation-only change"), HEAD)


@pytest.mark.parametrize(
    ("reason", "kwargs", "code"),
    [
        pytest.param("ci-covered: pytest", {}, "pr-source-review-ci-covered-reason-invalid", id="no-head"),
        pytest.param(
            f"CI covered: pytest at {SHORT}", {}, "pr-source-review-ci-covered-reason-invalid", id="misspelled-claim"
        ),
        pytest.param(
            f" ci-covered: pytest at {SHORT}", {}, "pr-source-review-ci-covered-reason-invalid", id="leading-space"
        ),
        pytest.param(f"ci-covered:  at {SHORT}", {}, "pr-source-review-ci-covered-reason-invalid", id="no-check-names"),
        pytest.param(
            f"ci-covered: {CHECK_RUN['name']} at {'b' * 12}",
            {},
            "pr-source-review-ci-covered-head-mismatch",
            id="head-prefix-differs",
        ),
        pytest.param(
            f"ci-covered: {CHECK_RUN['name']} at {SHORT}",
            {"head": "c" * 40},
            "pr-source-review-ci-covered-rollup-unavailable",
            id="rollup-for-another-head",
        ),
        pytest.param(
            f"ci-covered: {CHECK_RUN['name']} at {SHORT}",
            {"rollup": None},
            "pr-source-review-ci-covered-rollup-unavailable",
            id="rollup-absent",
        ),
        pytest.param(
            f"ci-covered: other-job at {SHORT}",
            {},
            "pr-source-review-ci-covered-check-not-successful",
            id="unknown-check-name",
        ),
        pytest.param(
            f"ci-covered: {CHECK_RUN['name']} at {SHORT}",
            {"rollup": ({**CHECK_RUN, "conclusion": "SKIPPED"},)},
            "pr-source-review-ci-covered-check-not-successful",
            id="skipped-check",
        ),
        pytest.param(
            f"ci-covered: {CHECK_RUN['name']} at {SHORT}",
            {"rollup": ({**CHECK_RUN, "conclusion": "NEUTRAL"},)},
            "pr-source-review-ci-covered-check-not-successful",
            id="neutral-check",
        ),
        pytest.param(
            f"ci-covered: {CHECK_RUN['name']} at {SHORT}",
            {"rollup": ({**CHECK_RUN, "status": "IN_PROGRESS", "conclusion": ""},)},
            "pr-source-review-ci-covered-check-not-successful",
            id="pending-check",
        ),
        pytest.param(
            f"ci-covered: {CHECK_RUN['name']} at {SHORT}",
            {"rollup": (CHECK_RUN, {**CHECK_RUN, "conclusion": "FAILURE"})},
            "pr-source-review-ci-covered-check-not-successful",
            id="same-name-rerun-failed",
        ),
    ],
)
def test_a_skip_the_rollup_does_not_support_is_refused(
    tmp_path: Path, reason: str, kwargs: dict[str, Any], code: str
) -> None:
    """Every way the cited coverage can be false or unverifiable fails with its own code."""
    with pytest.raises(SystemExit, match=code):
        _run(tmp_path, reason, **kwargs)


@pytest.mark.parametrize(
    "observation",
    [
        pytest.param(None, id="rollup-only"),
        pytest.param({"checkout_oid": "d" * 40}, id="synthetic-merge"),
        pytest.param({"passed": 0}, id="zero-tests"),
        pytest.param({"job_url": "https://ci.example/job/elsewhere"}, id="different-job"),
    ],
)
def test_green_check_requires_observed_test_execution(tmp_path: Path, observation: dict[str, Any] | None) -> None:
    """A green check cannot replace tests when job-level evidence describes another revision, job or no tests."""
    name = CHECK_RUN["name"]
    job_url = "https://ci.example/job/tests"
    (tmp_path / "pr.json").write_text(
        json.dumps({"headRefOid": HEAD, "statusCheckRollup": [{**CHECK_RUN, "detailsUrl": job_url}]}), encoding="utf-8"
    )
    row = {
        "check": name,
        "job_url": job_url,
        "checkout_oid": HEAD,
        "selection": "pytest",
        "matrix": "linux",
        "passed": 7,
    }
    notes = "" if observation is None else "CI test evidence: " + json.dumps({**row, **observation})
    (tmp_path / "review-notes.md").write_text(notes, encoding="utf-8")
    with pytest.raises(SystemExit, match="pr-source-review-ci-test-evidence-invalid"):
        _validator()._validate_ci_tests_skip(tmp_path, _check(f"ci-covered: {name} at {SHORT}"), HEAD)


@pytest.mark.parametrize("reason", ["covered by CI: pytest", "CI green", "ran in GitHub Actions"])
def test_a_skip_citing_no_verified_evidence_cannot_claim_complete_verification(tmp_path: Path, reason: str) -> None:
    """Free-form wording is judged by its effect: no CI binding, no approval, no complete readiness."""
    with pytest.raises(SystemExit, match="tests-skip-unverified-readiness"):
        _run(tmp_path, reason)
    _run(tmp_path, reason, readiness="incomplete")


def test_documentation_only_and_approved_skips_keep_complete_readiness(tmp_path: Path) -> None:
    """The two other admitted dispositions need no CI evidence."""
    _run(tmp_path, "documentation-only change")
    approved = [{"id": "tests", "status": "not-applicable", "reason": "user waived", "approval": {"path": "x"}}]
    _run(tmp_path, "user waived", checks=approved)


@pytest.mark.parametrize(
    "evidence",
    [
        pytest.param({"job_url": "http://ci.example/job/tests"}, id="plain-http-url"),
        pytest.param({"job_url": "https://evil.example/x"}, id="url-not-in-the-rollup"),
    ],
)
def test_a_job_url_must_be_an_https_link_from_the_rollup(tmp_path: Path, evidence: dict[str, Any]) -> None:
    """The cited job link is the rollup's own, never a free-text address."""
    with pytest.raises(SystemExit, match="test-evidence-invalid"):
        _run(tmp_path, f"ci-covered: {CHECK_RUN['name']} at {SHORT}", evidence=evidence)


def test_a_rollup_entry_without_a_link_cannot_be_cited(tmp_path: Path) -> None:
    """With no recorded link there is nothing to bind the evidence row to, so any address is refused."""
    bare = {key: value for key, value in CHECK_RUN.items() if key != "detailsUrl"}
    with pytest.raises(SystemExit, match="test-evidence-invalid"):
        _run(tmp_path, f"ci-covered: {CHECK_RUN['name']} at {SHORT}", rollup=(bare,), evidence={"job_url": JOB_URL})


WORKFLOW_PATCH = "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n"


def test_a_named_test_without_a_count_is_refused_when_the_pr_changes_the_workflow(tmp_path: Path) -> None:
    """A green job may no longer run the cited tests once a workflow changed, so counts are required."""
    reason = f"ci-covered: {CHECK_RUN['name']} at {SHORT}"
    with pytest.raises(SystemExit, match="test-evidence-invalid"):
        _run(tmp_path, reason, patch=WORKFLOW_PATCH)
    _run(tmp_path, reason, patch=WORKFLOW_PATCH, evidence={"passed": 120, "passing_test": None})
    _run(tmp_path, reason, patch="diff --git a/src/a.py b/src/a.py\n")


MERGE = "c" * 40


@pytest.mark.parametrize(
    ("evidence", "accepted"),
    [
        pytest.param({"checkout_oid": MERGE, "merge_parents": [BASE, HEAD]}, True, id="merge-of-base-and-head"),
        pytest.param({"checkout_oid": MERGE, "merge_parents": [HEAD, BASE]}, True, id="parents-in-either-order"),
        pytest.param({"checkout_oid": MERGE, "merge_parents": [BASE, "d" * 40]}, False, id="merge-of-another-head"),
        pytest.param({"checkout_oid": MERGE}, False, id="merge-without-recorded-parents"),
    ],
)
def test_a_merge_checkout_counts_only_when_its_parents_are_the_verified_base_and_head(
    tmp_path: Path, evidence: dict[str, Any], accepted: bool
) -> None:
    """CI that tested base plus head merged covers the reviewed bytes; any other merge does not."""
    reason = f"ci-covered: {CHECK_RUN['name']} at {SHORT}"
    if accepted:
        _run(tmp_path, reason, evidence=evidence)
    else:
        with pytest.raises(SystemExit, match="test-evidence-invalid"):
            _run(tmp_path, reason, evidence=evidence)


@pytest.mark.parametrize(
    ("reason", "refused"),
    [
        pytest.param(f"ci-covered: tests at {SHORT}", True, id="ci-covered-claim"),
        pytest.param("CI covered", True, id="free-form-claim"),
        pytest.param("documentation-only change", False, id="documentation-only"),
    ],
)
def test_a_local_review_cannot_claim_ci_coverage(reason: str, refused: bool) -> None:
    """A local working-tree review has no PR rollup, so a CI claim on its tests row is refused."""
    check = {"id": "tests", "status": "not-applicable", "reason": reason}
    if refused:
        with pytest.raises(SystemExit, match="ci-claim-unsupported"):
            _validator()._reject_local_ci_claim(check)
    else:
        _validator()._reject_local_ci_claim(check)

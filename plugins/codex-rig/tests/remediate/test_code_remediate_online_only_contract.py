"""Regression checks for bare-PR online-only remediation intake."""

from __future__ import annotations

from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[2]
CODE_REMEDIATE_SKILL = PLUGIN_ROOT / "skills" / "code-remediate" / "SKILL.md"


@pytest.mark.parametrize("skill_name", ["code-review", "code-remediate"])
def test_embedded_review_findings_are_individually_inventoried(skill_name: str) -> None:
    """Prevent a collected bot review from masking its independent nested findings."""
    skill = (PLUGIN_ROOT / "skills" / skill_name / "SKILL.md").read_text(encoding="utf-8")

    assert "### Embedded review findings" in skill
    assert "<parent-id>#finding-<ordinal>" in skill
    assert "before deduplication" in skill
    assert "Comments generated" in skill
    assert "same file or line alone" in skill
    assert "complete parent body" in skill
    assert "every nested finding" in skill


def test_bare_pr_targets_collect_online_evidence_without_review_artifact() -> None:
    """Keep bare PR remediation usable when no assessed report exists."""
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8")

    assert "bare number, `#number`, PR URL, and natural-language bare PR targets" in skill
    assert "collect current online items and verified local checkout" in skill
    assert "without a prior review report" in skill
    assert "explicit `+review`, `+report`, report aliases, and report paths retain report-plus-online behavior" in skill


def test_missing_findings_source_does_not_fail_bare_pr_route() -> None:
    """Keep the report-source fail-fast rule scoped to report aliases."""
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8")
    fail_fast = skill.split("## Fail-fast Rules", maxsplit=1)[1].split("## Quality Gates", maxsplit=1)[0]

    assert "Missing findings source in report mode, an explicit report path, or a report alias" in fail_fast
    assert "A bare PR has current online PR evidence as its findings source" in fail_fast
    assert "must not fail or request `code-review` merely because no assessed review artifact exists" in fail_fast
    assert "A bare PR target must not run this helper, scan prior review reports" in skill
    assert "For bare PR online-only intake, do not create `<run-directory>/findings-input.txt`" in skill
    assert (
        "When `REQUESTED_REPORT=true`, no matching code-review report means the requested assessed findings are missing"
        in skill
    )
    assert "Explain that first" in skill
    assert "Inspect that run's classified error and retained checkout diagnostics" in skill


def test_requested_review_does_not_replace_primary_remediation() -> None:
    """Require the user's review decision before replacing requested PR evidence."""
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8")
    intake = skill.split("### 02: Normalize input", maxsplit=1)[1].split("### 03:", maxsplit=1)[0]

    assert "`+review` requests existing review evidence; it does not authorize a fresh code review" in intake
    assert "Missing requested PR review decision" in intake
    assert "before online/user/preliminary finding intake, selection, or edits" in intake
    assert "Do not silently continue remediation with the requested review obligation open" in intake
    assert "Remediation resumes only after the producer completes" not in intake
    assert "or switch to online-only intake" not in intake
    assert "complete its ordered artifact closure before intake" not in intake


def test_preliminary_findings_keep_source_and_completion_boundaries() -> None:
    """Permit source-confirmed hypotheses without certifying an unfinished review."""
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8")

    assert "### Preliminary finding intake" in skill
    assert "matching exact PR identity, head, scope, and source evidence" in skill
    assert "triage each claim against the freshly verified current source" in skill
    assert "never present preliminary records as a validated review result" in skill
    assert "Selection, source checkout, target integration, and merge authorization gates still apply" in skill
    assert "do not certify unreviewed fixes as clean" in skill
    assert "Do not manufacture an assessed JSON report" in skill


def test_missing_report_blocks_until_the_fresh_review_decision() -> None:
    """Prevent available online comments from bypassing the requested-review decision."""
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8")
    fail_fast = skill.split("## Fail-fast Rules", maxsplit=1)[1].split("## Quality Gates", maxsplit=1)[0]

    assert "ask only for the missing finding evidence or selection" in skill
    assert "run `$code-review <target>` first or provide a report path" not in fail_fast
    assert "Missing requested PR review decision" in fail_fast
    assert "before any available-finding continuation" in fail_fast


def test_missing_requested_pr_review_has_explicit_yes_no_and_pending_routes() -> None:
    """Keep review authorization, declined review and unanswered review distinct."""
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8")
    decision = skill.split("### Missing requested PR review decision", maxsplit=1)[1].split(
        "### Preliminary finding intake", maxsplit=1
    )[0]

    assert "`mode=pr` and `REQUESTED_REPORT=true`" in decision
    assert "explicit PR report paths" in decision
    assert "Run a fresh code review now before remediation?" in decision
    assert "Run fresh code review (Recommended)" in decision
    assert "Continue with available findings" in decision
    assert "single question/menu" in decision
    assert "do not duplicate them in context prose" in decision
    assert "normal full `code-review` workflow" in decision
    assert "both artifact validators, promotion and `--complete-run`" in decision
    assert "resume this same remediation run" in decision
    assert "only after an eligible completed result is admitted" in decision
    assert "`requested_report=true`" in decision
    assert "missing/cancelled answer leaves this decision pending" in decision
    assert "Tool acceptance, silence or an unrelated answer does not authorize continuation" in decision
    assert "Bare PR online-only intake does not ask this question" in decision


def test_requested_review_sibling_routes_cannot_bypass_decision() -> None:
    """Make incomplete, unavailable and candidate evidence pass the same checkpoint."""
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8")
    incomplete = skill.split("- `matching-review-incomplete:", maxsplit=1)[1].split("\n- ", maxsplit=1)[0]
    missing = skill.split("- When `REQUESTED_REPORT=true`", maxsplit=1)[1].split("\n- ", maxsplit=1)[0]
    candidate = skill.split("For `matching-review-candidate-unpromoted:", maxsplit=1)[1].split(
        "### Missing requested PR review decision", maxsplit=1
    )[0]

    for route in (incomplete, missing, candidate):
        assert "Missing requested PR review decision" in route
    assert "then continue available current-online/user finding intake" not in missing

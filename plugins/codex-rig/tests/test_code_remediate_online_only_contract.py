"""Regression checks for bare-PR online-only remediation intake."""

from __future__ import annotations

from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CODE_REMEDIATE_SKILL = PLUGIN_ROOT / "skills" / "code-remediate" / "SKILL.md"


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
    assert "Inspect that run's classified error and retained checkout diagnostics, perform permitted recovery" in skill

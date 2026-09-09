"""Regression checks for ownership-scoped remediation commit choices."""

from __future__ import annotations

from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CODE_REMEDIATE_SKILL = PLUGIN_ROOT / "skills" / "code-remediate" / "SKILL.md"


def test_remediation_offers_post_gate_commit_modes() -> None:
    """Keep the commit decision after validated remediation work."""
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8")
    commit_section = skill.split("### 12: Offer An Opt-In Commit After Verified Remediation", maxsplit=1)[1]

    assert "shared quality gates, and result validation finish" in commit_section
    assert "- all at once" in commit_section
    assert "- group findings by topic" in commit_section
    assert "- each finding as a separate commit" in commit_section
    assert "- leave unstaged" in commit_section


def test_remediation_commit_stages_only_proven_owned_paths() -> None:
    """Reject commits that could absorb user or overlapping worktree changes."""
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8")
    commit_section = skill.split("### 12: Offer An Opt-In Commit After Verified Remediation", maxsplit=1)[1]
    fail_fast = skill.split("## Fail-fast Rules", maxsplit=1)[1].split("## Quality Gates", maxsplit=1)[0]

    assert "commit-baseline.json" in skill
    assert "including a lockfile such as `uv.lock`" in skill
    assert "git diff --cached --quiet" in commit_section
    assert "git add -- <paths>" in commit_section
    assert "Never use `git add .`, `git add -A`, a glob" in commit_section
    assert "partial-hunk staging" in commit_section
    assert "code-remediate-commit-scope-unsafe" in fail_fast
    assert "code-remediate-commit-grouping-unsafe" in fail_fast

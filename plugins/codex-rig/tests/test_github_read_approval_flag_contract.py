"""Check GitHub reader workflows use runtime permission without invocation approval flags."""

from __future__ import annotations

from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_CONTRACT = PLUGIN_ROOT / "shared" / "native-skill-contract.md"
ASSESS_SKILL = PLUGIN_ROOT / "skills" / "assess" / "SKILL.md"
RELEASE_SKILL = PLUGIN_ROOT / "skills" / "release" / "SKILL.md"


@pytest.mark.installed_plugin
@pytest.mark.parametrize(
    "skill_path", [pytest.param(ASSESS_SKILL, id="assess"), pytest.param(RELEASE_SKILL, id="release")]
)
def test_reader_workflow_has_no_approval_flag_or_consent_prompt(skill_path: Path) -> None:
    """Keep GitHub evidence reads in the selected workflow without a second consent gate."""
    skill = skill_path.read_text(encoding="utf-8")
    schema = skill.split("## Input Schema\n", 1)[1].split("\n## ", 1)[0]

    assert "approve_gh" not in schema
    assert "--approve-gh" not in skill
    assert "active opted-in `github-read` profile or request runtime approval for the complete owning command" in skill
    assert "native-skill-contract.md#github-read-execution" in skill
    assert "native-skill-contract.md#github-reader-runtime-boundary" in skill
    assert "Local-only" in skill
    assert "denial stops the current attempt" in skill


@pytest.mark.installed_plugin
def test_reader_runtime_boundary_preserves_narrow_command_and_remote_write_ban() -> None:
    """Keep host permission bound to the reader while preserving denied-read behavior."""
    contract = SHARED_CONTRACT.read_text(encoding="utf-8")
    boundary = contract.split("## GitHub Reader Runtime Boundary\n", 1)[1].split("\n## ", 1)[0]

    assert "actual Python executable, absolute installed `github_read.py` path" in boundary
    assert "`--out` location" in boundary
    assert "across repositories" in boundary
    assert "allowlisted local PR checkout" in boundary
    assert (
        'otherwise give the required brief and request external access for the complete reader with `sandbox_permissions="require_escalated"`'
        in boundary
    )
    assert "An unexpected restriction or denial stops" in boundary
    assert "remote publication or other remote mutation" in boundary
    assert "Do not wrap this command in `rtk`" in boundary
    assert "--approve-gh" not in boundary


@pytest.mark.installed_plugin
def test_pr_analysis_uses_collector_boundary() -> None:
    """Prevent the reader rule from being mistaken for collector permission."""
    skill = ASSESS_SKILL.read_text(encoding="utf-8")

    assert "PR collection uses `collect_pr.py` only" in skill
    assert "reader helper does not replace its outer collector" in skill
    assert "select-git-remote.py --canonical-pr-url" in skill

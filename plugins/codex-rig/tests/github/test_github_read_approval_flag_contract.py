"""Check GitHub reader workflows use runtime permission without invocation approval flags.

The skill and contract files are prose, so assertions read them by heading and whitespace-normalized text: moving a
paragraph break or reflowing a line must not break a test, while a changed rule still does.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _skill_text import flat

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
SHARED_CONTRACT = PLUGIN_ROOT / "shared" / "native-skill-contract.md"
ASSESS_SKILL = PLUGIN_ROOT / "skills" / "assess" / "SKILL.md"
RELEASE_SKILL = PLUGIN_ROOT / "skills" / "release" / "SKILL.md"


def _section(path: Path, heading: str) -> str:
    """Return the whitespace-normalized body of a level-2 section, up to the next level-2 heading."""
    body: list[str] = []
    inside = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            if inside:
                break
            inside = line.strip() == f"## {heading}"
        elif inside:
            body.append(line)
    assert body, f"section {heading!r} missing or empty in {path}"
    return flat(" ".join(body))


@pytest.mark.installed_plugin
class TestReaderWorkflows:
    """GitHub evidence reads in the assess and release workflows."""

    @pytest.mark.parametrize(
        "skill_path", [pytest.param(ASSESS_SKILL, id="assess"), pytest.param(RELEASE_SKILL, id="release")]
    )
    def test_has_no_approval_flag_or_consent_prompt(self, skill_path: Path) -> None:
        """Evidence reads stay in the selected workflow without a second consent gate.

        The skills defer to runtime permission and the shared contract; an `--approve-gh` flag or schema field would add
        an invocation-level approval the contract deliberately removed.
        """
        skill = flat(skill_path.read_text(encoding="utf-8"))

        assert "approve_gh" not in _section(skill_path, "Input Schema")
        assert "--approve-gh" not in skill
        assert "current effective" in skill
        assert "native-skill-contract.md#github-read-execution" in skill
        assert "native-skill-contract.md#github-reader-runtime-boundary" in skill
        assert "Local-only" in skill
        assert "denial stops the current attempt" in skill

    def test_pr_analysis_uses_collector_boundary(self) -> None:
        """The reader rule is not mistaken for collector permission.

        Pull-request analysis goes through the collector only, which resolves a bare number through the remote selector
        first.
        """
        skill = flat(ASSESS_SKILL.read_text(encoding="utf-8"))

        assert "PR collection uses `collect_pr.py` only" in skill
        assert "reader helper does not replace its outer collector" in skill
        assert "select-git-remote.py --canonical-pr-url" in skill


@pytest.mark.installed_plugin
class TestSharedContract:
    """The shared native-skill contract sections that bind reader permission."""

    def test_runtime_boundary_preserves_narrow_command_and_remote_write_ban(self) -> None:
        """Host permission stays bound to the reader command while denied-read behavior is preserved.

        The grant covers one exact executable, script path and output location, never a broader shell, and a denial or
        non-overridable restriction ends the attempt rather than being retried through another route.
        """
        boundary = _section(SHARED_CONTRACT, "GitHub Reader Runtime Boundary")

        assert "actual Python executable, absolute installed `github_read.py` path" in boundary
        assert "`--out` location" in boundary
        assert "across repositories" in boundary
        assert "allowlisted local PR checkout" in boundary
        assert (
            "When GitHub Read Execution establishes network and path grants, use the default sandboxed tool call"
            in boundary
        )
        assert (
            "Only when required capability is unavailable and runtime policy permits approval, give the required brief "
            'and request external access for the complete reader with `sandbox_permissions="require_escalated"`'
        ) in boundary
        assert "An explicit denial or non-overridable restriction stops" in boundary
        assert "remote publication or other remote mutation" in boundary
        assert "Do not wrap this command in `rtk`" in boundary
        assert "--approve-gh" not in boundary

    def test_checkout_checks_git_and_destination_write_grants(self) -> None:
        """A permitted reader output path cannot mask the write access a local checkout needs."""
        execution = _section(SHARED_CONTRACT, "GitHub Read Execution")

        assert (
            "For `github_read.py` commands that perform local checkout, also verify the repository `.git` and checkout "
            "destination are writable."
        ) in execution

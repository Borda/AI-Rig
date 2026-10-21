"""Pin develop:review's PR#/filename gate and the severity checklist develop now ships itself.

The gate used to test the raw argument text in shell (``TOKEN="$ARGUMENTS"`` plus ``[[ =~ ]]``) before flag parsing: a
quote or newline in the text broke the block, ``42 --full`` missed PR mode, and oss availability came from a cache glob
that also matched orphaned versions. It now reads the ``dev-review-pr-number`` sentinel ``dev_parse_args.py`` classifies
(``tests/test_dev_parse_args.py::TestReviewPrNumberSentinel``) and checks oss through the install record. The severity
checklist used to be read out of the oss plugin; develop now ships its own copy under ``templates/``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parents[1]
_SKILL = _PLUGIN / "skills/review/SKILL.md"
_CHECKLIST = _PLUGIN / "skills/review/templates/review-checklist.md"
_BASH = shutil.which("bash")
_CSID = "pr-gate-test"
_skip_no_bash = pytest.mark.skipif(_BASH is None, reason="bash unavailable")


def _skill_text() -> str:
    """Return the review skill text."""
    return _SKILL.read_text(encoding="utf-8")


def _gate_block() -> str:
    """Return the bash block that reads the classified PR-number sentinel."""
    blocks = re.findall(r"```bash\n(.*?)```", _skill_text(), re.DOTALL)
    return next(block for block in blocks if "dev-review-pr-number-" in block and "OSS_AVAILABLE=" in block)


def _write_registry(home: Path, plugins: dict[str, list[dict[str, str]]]) -> None:
    """Write a Claude Code plugin registry under a temp home."""
    registry = home / ".claude" / "plugins" / "installed_plugins.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(json.dumps({"plugins": plugins}), encoding="utf-8")


def _oss_entry(install: Path) -> dict[str, list[dict[str, str]]]:
    """Return a registry mapping that records oss as installed at ``install``."""
    return {"oss@borda-ai-rig": [{"installedAt": "2026-10-01T00:00:00Z", "installPath": install.as_posix()}]}


def _run_gate(tmp_path: Path, pr_number: str) -> subprocess.CompletedProcess[str]:
    """Run the gate block with a temp sentinel dir, the temp home's registry, and this plugin's bin/."""
    (tmp_path / f"dev-review-pr-number-{_CSID}").write_text(f"{pr_number}\n", encoding="utf-8", newline="\n")
    home = tmp_path / "home"
    env = {
        **os.environ,
        "HOME": home.as_posix(),
        "USERPROFILE": str(home),
        "TMPDIR": tmp_path.as_posix(),
        "CLAUDE_CODE_SESSION_ID": _CSID,
        "CLAUDE_PLUGIN_ROOT": _PLUGIN.as_posix(),
        "PATH": os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", ""),
    }
    return subprocess.run([_BASH, "-c", _gate_block()], capture_output=True, encoding="utf-8", env=env, check=False)


class TestPrGateBlock:
    """The gate reads only the classified sentinel and the install record."""

    @_skip_no_bash
    def test_local_target_skips_the_oss_question(self, tmp_path: Path) -> None:
        """An empty sentinel (path, directory or diff review) prints ``skip`` and never probes oss.

        This is the common path; it must not print the PR-detected line or touch the registry.
        """
        _write_registry(tmp_path / "home", {})
        result = _run_gate(tmp_path, "")
        assert (result.returncode, result.stdout) == (0, "OSS_AVAILABLE=skip PR_NUMBER=\n")

    @_skip_no_bash
    def test_installed_oss_offers_the_pr_review(self, tmp_path: Path) -> None:
        """A PR number with oss recorded as installed, review skill on disk, offers ``/oss:review``."""
        install = tmp_path / "cache" / "oss" / "1.0.0"
        (install / "skills" / "review").mkdir(parents=True)
        (install / "skills" / "review" / "SKILL.md").write_text("---\nname: review\n---\n", encoding="utf-8")
        _write_registry(tmp_path / "home", _oss_entry(install))
        result = _run_gate(tmp_path, "42")
        assert result.stdout.splitlines() == [
            "PR number detected — checking oss plugin availability",
            "OSS_AVAILABLE=true PR_NUMBER=42",
        ]

    @_skip_no_bash
    def test_unregistered_oss_reports_unavailable(self, tmp_path: Path) -> None:
        """No install record means oss is absent, even when an old version dir still sits in the cache.

        The retired cache glob matched any version dir, including orphaned ones Claude Code no longer dispatches to.
        """
        orphan = tmp_path / "home" / ".claude" / "plugins" / "cache" / "borda-ai-rig" / "oss" / "0.1.0"
        (orphan / "skills" / "review").mkdir(parents=True)
        (orphan / "skills" / "review" / "SKILL.md").write_text("---\nname: review\n---\n", encoding="utf-8")
        _write_registry(tmp_path / "home", {})
        result = _run_gate(tmp_path, "42")
        assert result.stdout.splitlines()[-1] == "OSS_AVAILABLE=false PR_NUMBER=42"

    @_skip_no_bash
    def test_install_record_without_review_skill_reports_unavailable(self, tmp_path: Path) -> None:
        """A recorded install path that lacks ``skills/review/SKILL.md`` cannot serve ``/oss:review``."""
        install = tmp_path / "cache" / "oss" / "1.0.0"
        install.mkdir(parents=True)
        _write_registry(tmp_path / "home", _oss_entry(install))
        result = _run_gate(tmp_path, "42")
        assert result.stdout.splitlines()[-1] == "OSS_AVAILABLE=false PR_NUMBER=42"

    def test_block_never_reads_raw_arguments_or_the_cache(self) -> None:
        """No argument text, shell regex test or cache glob reaches the gate, so a quote cannot break it."""
        block = _gate_block()
        assert ("$ARGUMENTS" in block, "[[" in block, "plugins/cache" in block) == (False, False, False)

    def test_runs_after_flag_parsing_and_before_worktree_isolation(self) -> None:
        """The sentinel exists only once Flag parsing ran, and the gate must still precede Step 1."""
        text = _skill_text()
        gate = text.index(_gate_block())
        assert text.index("## Flag parsing") < gate < text.index("## Worktree isolation")


class TestPrGateAnswers:
    """The question answers route through the parsed values, not the raw arguments."""

    def test_oss_review_receives_the_bare_number(self) -> None:
        """``/oss:review`` gets the classified number, never the develop flags passed beside it."""
        assert 'Skill(skill="oss:review", args="<PR_NUMBER>")' in _skill_text()

    def test_local_path_answer_replaces_the_clean_args(self) -> None:
        """The Q2 path lands in the sentinel Step 1 re-reads; the gate runs after Flag parsing, so no read-back."""
        text = _skill_text()
        assert 'echo "$Q2_PATH" > "${TMPDIR:-/tmp}/dev-review-clean-args-${CSID}"' in text
        assert "dev-review-q2-path" not in text

    def test_missing_clean_args_never_falls_back_to_raw_arguments(self) -> None:
        """A missing sentinel reviews the diff instead of pasting the raw argument text into a shell variable."""
        assert 'REVIEW_ARGS="$ARGUMENTS"' not in _skill_text()


class TestVendoredChecklist:
    """Develop:review applies its own severity checklist; it never reads one out of the oss plugin."""

    def test_checklist_resolves_inside_develop(self) -> None:
        """The handoff block points ``REVIEW_CHECKLIST`` at develop's template, and no oss checklist path remains."""
        text = _skill_text()
        assert 'REVIEW_CHECKLIST="$_DIMENSION_BRIEFS/review-checklist.md"' in text
        assert ("/skills/review/checklist.md" in text, _CHECKLIST.is_file()) == (False, True)

    @pytest.mark.parametrize(
        "marker",
        [
            pytest.param("## CRITICAL Patterns (must block merge)", id="critical-anchors"),
            pytest.param("## HIGH Patterns", id="high-anchors"),
            pytest.param("## Consolidation Rules", id="consolidation-rules"),
            pytest.param("## Suppressions (DO NOT flag these)", id="suppressions"),
        ],
    )
    def test_checklist_keeps_the_sections_reviewers_apply(self, marker: str) -> None:
        """Agent 1 reads the severity anchors and suppressions; the consolidator applies the consolidation rules."""
        assert marker in _CHECKLIST.read_text(encoding="utf-8")

    @pytest.mark.parametrize(
        "absent",
        [
            pytest.param("PR linked to issue", id="no-issue-linkage-pattern"),
            pytest.param("PR description diverges", id="no-pr-description-pattern"),
            pytest.param("review-section-taxonomy.md", id="no-oss-only-file-reference"),
        ],
    )
    def test_checklist_drops_what_a_local_review_cannot_honour(self, absent: str) -> None:
        """A local diff has no linked issue or PR description, and develop ships no section-taxonomy file."""
        assert absent not in _CHECKLIST.read_text(encoding="utf-8")

"""Protect audit orchestration, read-only preflight, and per-invocation state isolation."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from _hook_env import _bash_runs_posix_script

_SKILL = Path(__file__).resolve().parents[2] / "skills" / "audit" / "SKILL.md"
_BASH = pytest.mark.skipif(not _bash_runs_posix_script(), reason="POSIX bash unavailable")


def _section(heading: str) -> str:
    """Read one audit workflow section without depending on paragraph wrapping."""
    lines = _SKILL.read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith(heading))
    end = next((index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")), len(lines))
    return "\n".join(lines[start:end])


def _bash_block(section: str) -> str:
    """Extract the first executable bash block from a workflow section."""
    return section.split("```bash\n", 1)[1].split("```", 1)[0]


@_BASH
@pytest.mark.parametrize("hook_exit", [0, 1])
def test_precommit_only_validates_config_without_rewriting_sources(tmp_path: Path, hook_exit: int) -> None:
    """Preflight cannot run auto-fixing hooks or cache a failed check as success."""
    (tmp_path / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")
    source = tmp_path / "user.md"
    source.write_text("user's uncommitted text\n", encoding="utf-8")
    state = tmp_path / "audit-state-test"
    state.mkdir()
    validation = state / "precommit-validate.txt"
    validation.write_text("stale output\n", encoding="utf-8")
    script = (
        'pre-commit() { printf "%s\\n" "$*" >> calls; '
        'printf "validation output\\n"; printf "validation diagnostic\\n" >&2; return "$HOOK_EXIT"; }\n'
    )
    result = subprocess.run(
        ["bash"],
        input=script + _bash_block(_section("## Step 1:")),
        text=True,
        capture_output=True,
        cwd=tmp_path,
        env={
            **os.environ,
            "HOOK_EXIT": str(hook_exit),
            "TMPDIR": str(tmp_path),
            "CLAUDE_CODE_SESSION_ID": "test",
        },
        check=False,
    )
    assert (tmp_path / "calls").read_text().splitlines() == ["validate-config .pre-commit-config.yaml"]
    assert source.read_text() == "user's uncommitted text\n"
    assert not (tmp_path / ".temp/state/preflight/pre-commit.ok").exists()
    assert result.returncode == hook_exit
    assert validation.read_text() == "validation output\nvalidation diagnostic\n"


@_BASH
def test_no_precommit_config_clears_prior_validation_output(tmp_path: Path) -> None:
    """A run without pre-commit cannot feed stale validation findings to the consolidator."""
    state = tmp_path / "audit-state-test"
    state.mkdir()
    validation = state / "precommit-validate.txt"
    validation.write_text("stale failure\n", encoding="utf-8")
    result = subprocess.run(
        ["bash"],
        input=_bash_block(_section("## Step 1:")),
        text=True,
        capture_output=True,
        cwd=tmp_path,
        env={**os.environ, "TMPDIR": str(tmp_path), "CLAUDE_CODE_SESSION_ID": "test"},
        check=False,
    )
    assert result.returncode == 0
    assert validation.read_text() == ""


@_BASH
@pytest.mark.parametrize("preflight_exit", [0, 1])
def test_new_invocation_resets_stale_state_only_after_success(tmp_path: Path, preflight_exit: int) -> None:
    """A fresh run cannot inherit coverage or reports, and a failed preflight cannot clear prior evidence."""
    state = tmp_path / "audit-state-test"
    state.mkdir()
    (state / "unscanned-plugins").write_text("stale plugin\n", encoding="utf-8")
    (state / "run-dir").write_text("old-run\n", encoding="utf-8")
    result = subprocess.run(
        ["bash"],
        input='python() { return "$PREFLIGHT_EXIT"; }\n' + _bash_block(_section("## Pre-flight checks")),
        text=True,
        capture_output=True,
        cwd=tmp_path,
        env={
            **os.environ,
            "TMPDIR": str(tmp_path),
            "CLAUDE_CODE_SESSION_ID": "test",
            "PREFLIGHT_EXIT": str(preflight_exit),
        },
        check=False,
    )
    assert result.returncode == preflight_exit
    assert (state / "run-dir").read_text() == ("old-run\n" if preflight_exit else "")
    assert (state / "unscanned-plugins").read_text() == ("stale plugin\n" if preflight_exit else "")


def test_breaking_acknowledgment_has_an_automation_exception() -> None:
    """Automation records awareness without waiting for an interactive answer."""
    lines = _SKILL.read_text(encoding="utf-8").splitlines()
    exception = next(line for line in lines if "acknowledged-by-automation" in line)
    assert "--skip-gate" in exception
    assert "AskUserQuestion" in exception
    assert "each" in exception
    assert "report" in exception
    assert "`ack`" in exception
    assert "Step 11" in exception
    assert "only" in exception
    assert "in its finding file" not in exception


def test_clean_report_has_no_fix_menu_and_recommendation_is_second() -> None:
    """Zero findings finish without a question, and every nonempty menu places its recommendation second."""
    gate = _section("## Follow-up gate")
    assert "Total: 0" in gate
    assert "no fix options" in gate
    assert "required decision or authorization" in gate
    layouts = [
        line.split(": ", 1)[1].split(" = ", 1)[0] for line in gate.splitlines() if " = " in line and " → " in line
    ]
    assert layouts == ["(b) → (a) → (c) → (d)", "(c) → (a) → (d)"]
    recommendation = next(line for line in gate.splitlines() if line.startswith("- Every option:"))
    assert "second in both layouts" in recommendation


def test_standard_curators_share_a_wave_ceiling() -> None:
    """Step 4's five group curators cannot bypass Step 3's opus pool ceiling."""
    lines = _SKILL.read_text(encoding="utf-8").splitlines()
    cap = next(line for line in lines if "**Parallel execution rule**" in line)
    assert "Step 3 first" in cap
    assert "Step 4 group curators" in cap
    assert "CAP_OPUS" in cap
    assert "return" in cap


def test_existing_fix_authority_reaches_dispatch_without_a_new_question() -> None:
    """Conditional follow-up cannot silently drop an already-authorized fix scope."""
    dispatch = _section("## Steps 8–10:")
    assert "or already authorized a concrete fix scope" in dispatch
    assert "use that granted scope as the dispatch input" in dispatch
    assert "no fix option was picked and no fix scope was already authorized" in dispatch
    assert "do not broaden it" in dispatch


def test_fix_authority_requires_an_explicit_session_instruction() -> None:
    """Findings and invocation alone cannot authorize a new fix scope or systemic decisions."""
    gate = _section("## Follow-up gate")
    authority = next(line for line in gate.splitlines() if "explicit user instruction in this session" in line)
    assert "naming a fix level" in authority
    assert "never inferred from findings or the invocation" in authority
    assert "NON_AUTO_FIXABLE" in authority
    assert "still ask" in authority
    missing = next(line for line in gate.splitlines() if "Total > 0" in line and "no such instruction" in line)
    assert "no `--skip-gate`" in missing
    assert "ask" in missing


def test_communication_reads_are_allowed_during_a_default_sweep() -> None:
    """The orchestrator can read its declared communication inputs even without the narrow scope."""
    lines = _SKILL.read_text(encoding="utf-8").splitlines()
    orchestration = next(line for line in lines if line.startswith("**Orchestration contract**"))
    assert "the communication files listed in Step 2" in orchestration
    assert "when `communication` scope is active" not in orchestration


def test_precommit_diagnostics_reach_the_consolidator() -> None:
    """Persisted validation diagnostics remain an explicit input to Step 5's findings aggregation."""
    validation = _section("## Step 1:")
    collection = _section("## Steps 4–5b:")
    sentinel = "${TMPDIR:-/tmp}/audit-state-${CSID}/precommit-validate.txt"
    assert sentinel in collection
    assert "consolidator" in collection
    command = next(line for line in validation.splitlines() if "pre-commit validate-config" in line)
    assert "# timeout: 15000" in command


def test_report_task_completes_only_after_table_delivery() -> None:
    """The report-print task cannot claim completion before its table is visible."""
    lines = _SKILL.read_text(encoding="utf-8").splitlines()
    phase = next(line for line in lines if "Phase 6: print report header" in line)
    assert "after" in phase
    assert "table" in phase
    assert "BEFORE" not in phase


def test_preflight_receives_original_arguments_for_plugin_resolution() -> None:
    """The deterministic helper receives raw scope tokens instead of a model-rewritten command."""
    preflight = _section("## Pre-flight checks")
    assert '--arguments "$ARGUMENTS"' in _bash_block(preflight)
    assert "normalize each tier-2 scope" not in preflight
    inputs = _SKILL.read_text(encoding="utf-8").split("</inputs>", 1)[0]
    tier2 = next(line for line in inputs.splitlines() if "`plugins <name>` —" in line)
    assert "`plugins/<name>/` first, then `plugins/cc_<name>/`" in tier2
    assert "codemap-py" in inputs
    assert "manifest `name`" in tier2


def test_named_skills_use_declared_layout_and_keep_multiple_matches() -> None:
    """A named skill in a custom directory or several plugins cannot silently disappear."""
    collect = _section("## Step 2:")
    tier3 = next(line for line in collect.splitlines() if "`<skill-name>` (tier 3) scope" in line)
    assert "<skills-dir>/<name>/SKILL.md" in tier3
    assert "PLUGIN_LAYOUT:" in tier3
    assert "union every plugin" in tier3
    assert "retain all same-named skills" in tier3
    resolution = next(
        line for line in _SKILL.read_text(encoding="utf-8").splitlines() if "**Scope token resolution**" in line
    )
    assert "`LOCAL_MODE=false`" in resolution
    assert "`.claude/skills/` only" in resolution
    assert "`.claude/agents/`" in resolution


def test_mode_exclusions_agree_at_all_three_entry_points() -> None:
    """Flag descriptions and the combination summary cannot send the same invocation down conflicting routes."""
    lines = _SKILL.read_text(encoding="utf-8").splitlines()
    upgrade = next(line for line in lines if line.startswith("  - `--upgrade`"))
    adversarial = next(line for line in lines if line.startswith("  - `--adversarial`"))
    combinations = next(line for line in lines if "**Valid combinations**" in line)
    expected = (
        "Among `--upgrade`, `--adversarial`, `--efficiency`, `--consolidate`: "
        "`--upgrade` and `--consolidate` each exclude all others; "
        "`--adversarial` and `--efficiency` may combine; error on all other mode combinations."
    )
    assert expected in upgrade
    assert expected in adversarial
    assert expected in combinations

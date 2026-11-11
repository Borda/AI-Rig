"""Guard audit mode handoffs, fix authority, and measurement provenance."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from _hook_env import _bash_runs_posix_script
from _hook_io import CSID, run_hook_json
from audit_preflight import write_state

_AUDIT = Path(__file__).resolve().parents[2] / "skills" / "audit"
_BASH = pytest.mark.skipif(not _bash_runs_posix_script(), reason="POSIX bash unavailable")


def _lines(relative: str) -> list[str]:
    """Read a mode or template as independent instruction lines."""
    return (_AUDIT / relative).read_text(encoding="utf-8").splitlines()


def test_inventory_review_collects_the_read_only_bridge_reply() -> None:
    """Inventory reviews include unchanged sources and never depend on peer writes."""
    lines = _lines("modes/adversarial.md")
    dispatch = next(line for line in lines if 'Skill(skill="bridge:review"' in line)
    assert "unchanged" in dispatch
    capture = next(line for line in lines if "orchestrator" in line and "returned" in line)
    assert "Write" in capture
    assert "codex-adversarial-" in capture


def test_adversarial_only_run_produces_the_gate_inputs() -> None:
    """Standalone adversarial findings reach the existing follow-up and fix gates."""
    lines = _lines("modes/adversarial.md")
    assert any("summary.jsonl.adversarial.rec" in line for line in lines)
    assert any("append_ledger.py" in line for line in lines)
    assert any("aggregate.md" in line and "canonical" in line for line in lines)
    assert any("Step 5" in line and "sole writer" in line for line in lines)


@_BASH
@pytest.mark.skipif(shutil.which("node") is None, reason="requires node to execute the hook")
@pytest.mark.integration
@pytest.mark.parametrize("relative", ["modes/adversarial.md", "modes/steps-4-5-7.md"])
@pytest.mark.parametrize("finding_count", [0, 1])
def test_adversarial_staging_reaches_the_real_follow_up_gate(tmp_path: Path, finding_count: int, relative: str) -> None:
    """The mode's actual append command produces usable gate inputs, including a clean audit."""
    stage = "adversarial" if relative == "modes/adversarial.md" else "standard"
    instruction = next(line for line in _lines(relative) if f"summary.jsonl.{stage}.rec" in line)
    assert "empty" in instruction
    assert "Zero" in instruction
    staged_name = next(token for token in instruction.split("`") if token.endswith(f"/summary.jsonl.{stage}.rec"))
    command = next(
        token for token in instruction.split("`") if token.startswith("python ") and "append_ledger" in token
    )
    run_dir = tmp_path / "repo/.reports/audit/current"
    run_dir.mkdir(parents=True)
    staged = Path(staged_name.replace("<RUN_DIR>", str(run_dir)))
    row = {"file": "agents/helper.md", "sev": "high", "one_line": "Missing reference", "auto_fixable": True}
    staged.write_text((json.dumps(row) + "\n") * finding_count, encoding="utf-8")
    result = subprocess.run(
        [
            "bash",
            "-c",
            command.replace('\\"', '"')
            .replace("<RUN_DIR>", str(run_dir))
            .replace("python ", shlex.quote(sys.executable) + " ", 1),
        ],
        text=True,
        capture_output=True,
        env={**os.environ, "CLAUDE_PLUGIN_ROOT": str(_AUDIT.parents[1])},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (run_dir / "summary.jsonl").read_text(encoding="utf-8") == (json.dumps(row) + "\n") * finding_count
    assert not staged.exists()
    (run_dir / "aggregate.md").write_text("Consolidated findings.\n", encoding="utf-8")
    state = tmp_path / f"audit-state-{CSID}"
    state.mkdir()
    (state / "run-dir").write_text(str(run_dir), encoding="utf-8")
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "AskUserQuestion",
        "cwd": str(tmp_path / "repo"),
        "tool_input": {
            "questions": [
                {
                    "question": f"Audit Report\nTotal: {finding_count}\n→ {run_dir / 'summary.jsonl'}",
                    "options": [{"label": "Fix auto-fixable (Recommended)", "description": "Apply eligible fixes"}],
                }
            ]
        },
    }
    assert run_hook_json(_AUDIT.parents[1] / "hooks/enforce-audit-header.js", tmp_path, payload) == {}


@pytest.mark.parametrize("relative", ["modes/fix.md", "templates/audit-fix-prompt.md"])
def test_fix_dispatch_preserves_the_selected_authority(relative: str) -> None:
    """The delegated route cannot turn a limited selection into permission to fix everything."""
    lines = _lines(relative)
    assert any("SELECTED_OPTION" in line and "SEVERITY_SET" in line for line in lines)
    assert any("NON_AUTO_FIXABLE_IDS" in line and "RESOLUTIONS" in line for line in lines)
    assert any("auto_fixable" in line and "false" in line for line in lines)
    assert any("before" in line and "dispatch" in line and "decisions" in line for line in lines)


@pytest.mark.parametrize("relative", ["modes/fix.md", "templates/audit-fix-prompt.md"])
def test_new_findings_cannot_bypass_systemic_exclusions(relative: str) -> None:
    """A newly discovered settings or tier finding is classified before using an old exclusion-id snapshot."""
    assert any(
        "new findings" in line and "authoritative" in line and "NON_AUTO_FIXABLE" in line for line in _lines(relative)
    )


@pytest.mark.parametrize("relative", ["modes/fix.md", "templates/audit-fix-prompt.md"])
def test_dirty_tree_fast_path_uses_a_per_file_agent(relative: str) -> None:
    """An available bridge does not make its clean-tree implementation route usable."""
    lines = _lines(relative)
    assert any("dirty" in line and "haiku" in line and "bridge:implement" in line for line in lines)


def test_fix_subagent_enforces_its_own_convergence_bound() -> None:
    """Skipping the parent re-audit step cannot skip the counter or plateau guard."""
    lines = _lines("templates/audit-fix-prompt.md")
    assert any("fix-passes.txt" in line and "5" in line for line in lines)
    assert any("re_audit_clean: false" in line and "repeat" in line for line in lines)
    assert any("strictly decrease" in line and "NOT CONVERGING" in line for line in lines)


def test_residual_criticals_carry_fix_selection_metadata() -> None:
    """A fixable critical discovered by the residual scanner can enter the next authorized pass."""
    residual = next(line for line in _lines("templates/audit-fix-prompt.md") if line.startswith("**Step R2"))
    assert '"sev":"critical"' in residual
    assert '"auto_fixable":true|false' in residual
    assert '"category"' in residual
    assert '"one_line"' in residual


def test_upgrade_snapshots_have_a_fresh_persisted_run_directory() -> None:
    """Upgrade-only dispatch cannot snapshot into a missing or prior audit run directory."""
    lines = _lines("modes/upgrade.md")
    assert any("make_run_dir.py" in line for line in lines)
    assert any("upgrade-run-dir" in line for line in lines)


def test_summary_keys_and_fix_artifacts_distinguish_same_named_skills() -> None:
    """Different plugins' SKILL.md findings and gate actors retain distinct identities."""
    summary = next(line for line in _lines("modes/steps-4-5-7.md") if '"file":"' in line)
    assert '"file":"<repo-relative path>"' in summary
    assert '"auto_fixable":true|false' in summary
    assert "<basename>" not in summary
    lines = _lines("templates/audit-fix-prompt.md")
    assert any("gate-<file-slug>-<actor>.md" in line for line in lines)
    assert any("residual-<file-slug>.md" in line for line in lines)


def test_large_low_confidence_batch_has_a_consumer_and_staged_records() -> None:
    """The fan-out ceiling still merges its batch output into the canonical summary."""
    lines = _lines("modes/steps-4-5-7.md")
    consumer = next(line for line in lines if line.startswith("**Batch consumer**"))
    assert "lowconf-batch-rerun.md" in consumer
    assert "summary.jsonl.lowconf-batch.rec" in consumer
    assert "aggregate.md" in consumer


@_BASH
@pytest.mark.parametrize("relative", ["modes/adversarial.md", "modes/fix.md", "modes/steps-4-5-7.md"])
@pytest.mark.parametrize("probe_exit", [0, 1])
def test_bridge_probe_reports_one_persisted_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str, probe_exit: int
) -> None:
    """Partial stdout on a failed probe cannot concatenate into an invalid availability value."""
    lines = _lines(relative)
    probe = next(line for line in lines if "check_bridge.py" in line)
    status_read = next(line for line in lines if "IFS= read -r CODEX_STATUS" in line)
    visible = next(line for line in lines if "CODEX_AVAILABLE=%s" in line)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "test")
    prepared = write_state({"local-mode": "false"})
    assert prepared == tmp_path / "audit-state-test"
    result = subprocess.run(
        ["bash"],
        input='python() { printf "available\\n"; return "$PROBE_EXIT"; }\n'
        + "CSID=test\n"
        + "\n".join([probe, status_read, visible]),
        text=True,
        capture_output=True,
        env={**os.environ, "TMPDIR": str(tmp_path), "PROBE_EXIT": str(probe_exit)},
        check=False,
    )
    assert result.returncode == 0
    assert (tmp_path / "audit-state-test/codex-status").read_text().strip() == ("absent" if probe_exit else "available")
    assert result.stdout.strip() == ("CODEX_AVAILABLE=false" if probe_exit else "CODEX_AVAILABLE=true")


def test_upgrade_calibration_binds_source_and_distinct_runs() -> None:
    """Before and after measure the edited source without overwriting baseline evidence."""
    lines = _lines("modes/upgrade.md")
    baseline = next(line for line in lines if "N=3" in line and "Spawn" in line)
    assert "LOCAL_MODE=true" in baseline
    assert "TIMESTAMP" in baseline
    assert "RUN_SLUG" in baseline
    assert "before" in baseline
    after = next(line for line in lines if "**Step c" in line)
    assert "after" in after
    assert "RUN_SLUG" in after
    assert any("TARGET_FILE" in line and "unresolved" in line and "reject" in line for line in lines)


def test_upgrade_config_validation_uses_a_recoverable_target_snapshot() -> None:
    """A missing unrelated settings file or jq cannot cause a spurious rollback."""
    lines = _lines("modes/upgrade.md")
    assert any("snapshot" in line and "before" in line for line in lines)
    assert any("json.tool" in line for line in lines)
    assert not any("jq empty" in line for line in lines)
    assert any("summary.jsonl" in line and "4 hours" in line for line in lines)


def test_mechanical_handoff_is_not_a_deep_reasoning_signal() -> None:
    """A single fetch delegation does not incorrectly upgrade a focused agent's required tier."""
    signal = next(line for line in _lines("templates/checks-agents.md") if "**Orchestration signal**" in line)
    assert "2+" in signal
    assert "judgment" in signal
    assert "mechanical" in signal


def test_local_only_install_reference_is_advisory() -> None:
    """The rubric agrees with R1's warning for legitimate unreleased source additions."""
    lines = _lines("templates/checks-install.md")
    local_only = next(line for line in lines if line.startswith("| R1-") and "local-only" in line)
    cells = [cell.strip() for cell in local_only.split("|")[1:-1]]
    assert cells[0].startswith("R1-WARN")
    assert cells[2] == "medium"
    assert "newly added" in cells[3]
    assert not any("foundry:setup link" in line or "lacks `--check-tests`" in line for line in lines)


def test_inventory_and_shell_state_classes_have_canonical_severity() -> None:
    """The scoring rubric does not claim roster drift proves a runtime failure."""
    lines = _lines("severity-table.md")
    roster = next(line for line in lines if line.startswith("| MEMORY.md inventory drift"))
    shell = next(line for line in lines if line.startswith("| Cross-call shell state"))
    assert [cell.strip() for cell in roster.split("|")[1:-1]][1] == "medium"
    assert [cell.strip() for cell in shell.split("|")[1:-1]][1] == "medium"
    assert not any("stale roster fails at runtime" in line for line in lines)


@pytest.mark.parametrize("relative", ["modes/steps-4-5-7.md", "modes/fix.md", "templates/audit-fix-prompt.md"])
def test_nested_spawn_sites_are_included_in_deadline_rows(relative: str) -> None:
    """Consolidation and nested gate agents cannot evade the notification deadline protocol."""
    lines = _lines(relative)
    deadline = next(line for line in lines if "agent-watch-" in line and "every" in line)
    assert "row per" in deadline
    assert "900" in deadline


def _bash_block(relative: str, needle: str) -> str:
    """Extract a runnable fence containing the requested operation without reflowing prose."""
    block: list[str] | None = None
    for line in _lines(relative):
        if line.strip() == "```bash":
            block = []
        elif line.strip() == "```" and block is not None:
            if any(needle in item for item in block):
                return "\n".join(block) + "\n"
            block = None
        elif block is not None:
            block.append(line)
    raise AssertionError(f"No runnable block for {needle}")


@pytest.mark.parametrize("relative", ["modes/fix.md", "templates/audit-fix-prompt.md"])
def test_scoped_authority_intersects_eligibility_without_collecting_decisions(relative: str) -> None:
    """A prior path or severity authorization reaches dispatch without granting systemic edits."""
    scoped = next(line for line in _lines(relative) if "SELECTED_OPTION=scoped" in line)
    assert "SCOPE_FILTER" in scoped
    assert "paths" in scoped
    assert "severities" in scoped
    assert "auto_fixable:true" in scoped
    assert "NON_AUTO_FIXABLE_IDS" in scoped
    assert "never" in scoped
    assert "(c)" in scoped
    assert "fix-selection.json" in scoped


def test_scoped_selection_has_an_explicit_dispatch_route_without_a_new_question() -> None:
    """Even a one-finding authorized scope reaches the fix sub-agent without another follow-up gate."""
    lines = _lines("modes/fix.md")
    dispatch = next(line for line in lines if "finding count > 10" in line)
    assert "SELECTED_OPTION=scoped" in dispatch
    gate = next(line for line in lines if line.startswith("**Pre-apply validation gate authority**"))
    assert "scoped" in gate
    assert "already granted authority" in gate


def test_every_excluded_category_is_presented_and_reported() -> None:
    """Systemic and advisory exclusions cannot silently disappear at non-low severities."""
    lines = _lines("modes/fix.md")
    decision = next(line for line in lines if "**Upfront decision collection**" in line)
    assert "every `auto_fixable:false` row" in decision
    assert "group" in decision
    assert "category" in decision
    assert "max 4 categories" not in decision
    assert "overflow findings" not in decision
    report = next(line for line in lines if "every ineligible row" in line)
    assert "Step 11" in report
    assert "any severity" in report
    assert "reason" in report
    advisory = next(line for line in _lines("severity-table.md") if "closed list" in line)
    assert "report-only" in advisory
    assert "Check 47" in advisory


@pytest.mark.parametrize("relative", ["templates/audit-fix-prompt.md", "modes/steps-4-5-7.md"])
def test_classifiers_receive_all_five_systemic_exclusion_classes(relative: str) -> None:
    """New findings can be classified without guessing an unavailable mode file's contents."""
    classifier = next(line for line in _lines(relative) if "Check 3 (settings.json missing permission)" in line)
    assert "Check 5 critical/high (permission safety)" in classifier
    assert "Check 19 (model tier mismatch)" in classifier
    assert "CLAUDE.md contradiction" in classifier
    assert "dead loop in follow-up chains" in classifier
    assert any("policy-sibling:" in line and "modes/fix.md" in line for line in _lines(relative))


def test_combined_modes_have_a_reachable_single_writer() -> None:
    """The combined route actually dispatches the writer after efficiency has produced findings."""
    lines = _lines("modes/adversarial.md")
    dispatch = next(line for line in lines if "defer" in line and "efficiency Phase C" in line)
    assert "Spawn" in dispatch
    assert "foundry:curator" in dispatch
    producer = next(line for line in lines if "summary.jsonl.efficiency.rec" in line)
    assert "efficiency-report.md" in producer
    assert "stage" in producer
    assert "root" in producer
    assert "aggregate.md" in producer
    assert "append_ledger.py" in producer
    assert "P-HOLD" in producer
    assert "E11" in producer
    assert "E8/E9/E10" in producer


@_BASH
@pytest.mark.integration
def test_remediation_merge_keeps_existing_report_and_source_files(tmp_path: Path) -> None:
    """The shipped append recipe preserves findings without loading them into orchestration context."""
    block = _bash_block("modes/steps-4-5-7.md", '--from "$_RUN_DIR"/lowconf-')
    run_dir = tmp_path / ".reports/audit/current"
    run_dir.mkdir(parents=True)
    aggregate = run_dir / "aggregate.md"
    aggregate.write_bytes(b"Findings\n## Confidence\nScore: 0.93")
    source = run_dir / "lowconf-helper-remediation.md"
    source.write_bytes(b"\n### Remediation\nConfirmed\n")
    prepared = tmp_path / "audit-state-test"
    prepared.mkdir()
    (prepared / "run-dir").write_text(str(run_dir) + "\n", encoding="utf-8")
    result = subprocess.run(
        ["bash"],
        input=block.replace("python ", shlex.quote(sys.executable) + " "),
        cwd=tmp_path,
        env={
            **os.environ,
            "TMPDIR": str(tmp_path),
            "CLAUDE_CODE_SESSION_ID": "test",
            "CLAUDE_PLUGIN_ROOT": str(_AUDIT.parents[1]),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert aggregate.read_bytes() == b"Findings\n## Confidence\nScore: 0.93\n### Remediation\nConfirmed\n"
    assert source.read_bytes() == b"\n### Remediation\nConfirmed\n"


@_BASH
@pytest.mark.integration
def test_nested_watch_does_not_timeout_the_parents_children(tmp_path: Path) -> None:
    """Only the fix sub-agent consumes its nested envelope deadlines."""
    lines = _lines("templates/audit-fix-prompt.md")
    watch = next(line for line in lines if "Write `<RUN_DIR>/fix-pass-<N>/agent-watch-" in line)
    watch_name = next(token for token in watch.split("`") if token.endswith(".tsv"))
    run_dir = tmp_path / ".reports/audit/current"
    nested = Path(
        watch_name.replace("<RUN_DIR>", str(run_dir))
        .replace("<N>", "1")
        .replace("<phase>", "p1")
        .replace("<wave>", "1")
    )
    nested.parent.mkdir(parents=True)
    nested.write_text("child\t-\t900\n", encoding="utf-8")
    os.utime(nested, (1, 1))
    deliverable = run_dir / "fix-summary.md"
    deliverable.write_text("Completed\n", encoding="utf-8")
    (run_dir / "fix-passes.txt").write_text("1\n", encoding="utf-8")
    (run_dir / "agent-watch-parent.tsv").write_text(f"audit-fix\t{deliverable}\t900\n", encoding="utf-8")
    prepared = tmp_path / "audit-state-test"
    prepared.mkdir()
    (prepared / "run-dir").write_text(str(run_dir) + "\n", encoding="utf-8")
    command = _bash_block("templates/audit-fix-prompt.md", "agent_watch.py")
    child_result = subprocess.run(
        ["bash"],
        input=command.replace("python ", shlex.quote(sys.executable) + " "),
        env={
            **os.environ,
            "CLAUDE_PLUGIN_ROOT": str(_AUDIT.parents[1]),
            "TMPDIR": str(tmp_path),
            "CLAUDE_CODE_SESSION_ID": "test",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    parent_result = subprocess.run(
        [sys.executable, str(_AUDIT.parents[1] / "bin/agent_watch.py"), "--state-dir", str(run_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert child_result.returncode == parent_result.returncode == 0
    assert json.loads(child_result.stdout)["timed_out"] == ["child"]
    assert json.loads(parent_result.stdout)["timed_out"] == []
    assert [row["name"] for row in json.loads(parent_result.stdout)["agents"]] == ["audit-fix"]


@_BASH
@pytest.mark.parametrize("initial", [None, 4, 5])
def test_subagent_counter_persists_and_stops_at_the_shared_limit(tmp_path: Path, initial: int | None) -> None:
    """A delegated fix cannot lose its pass count across fresh shells or dispatch a sixth pass."""
    counter = tmp_path / "fix-passes.txt"
    counter.write_text("" if initial is None else str(initial) + "\n", encoding="utf-8")
    prepared = tmp_path / "audit-state-test"
    prepared.mkdir()
    (prepared / "run-dir").write_text(str(tmp_path) + "\n", encoding="utf-8")
    block = _bash_block("templates/audit-fix-prompt.md", "2>/dev/null || PASS_COUNT=0")
    result = subprocess.run(
        ["bash"],
        input=block + '\nprintf "dispatch=%s\\n" "$PASS_COUNT"\n',
        env={**os.environ, "TMPDIR": str(tmp_path), "CLAUDE_CODE_SESSION_ID": "test"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert counter.read_text(encoding="utf-8").strip() == str(min((initial or 0) + 1, 5))
    assert ("dispatch=" in result.stdout) == (initial != 5)


@pytest.mark.parametrize("relative", ["modes/fix.md", "templates/audit-fix-prompt.md"])
def test_non_inventory_findings_have_collision_free_artifact_names(relative: str) -> None:
    """Settings and hook findings get stable artifacts even when Step 3 did not assign a slug."""
    fallback = next(line for line in _lines(relative) if "non-inventory" in line)
    assert "repo-relative" in fallback
    assert "UTF-8" in fallback
    assert "hex" in fallback
    assert "path-" in fallback
    assert "basename" in fallback


def test_adversarial_inventory_assigns_slugs_without_step_three() -> None:
    """Standalone adversarial runs compute agent and skill identities before dispatch."""
    rule = next(line for line in _lines("modes/adversarial.md") if "Before Phase A" in line)
    assert "Step 2" in rule
    assert "Step 3" in rule
    assert "agent" in rule
    assert "skill" in rule
    assert "local" in rule
    assert "substitute" in rule
    dispatch = next(line for line in _lines("modes/adversarial.md") if line.startswith("> Write full findings"))
    assert "assigned above" in dispatch


def test_parallel_watch_names_include_the_wave_component() -> None:
    """Nested parallel fixes cannot overwrite a watch file from another wave."""
    launch = next(line for line in _lines("templates/audit-fix-prompt.md") if "**Phase 1 — Parallel basket**" in line)
    assert "fix-pass-<N>/agent-watch-fix-<N>-p1-<wave>.tsv" in launch


def _upgrade_block(tmp_path: Path, needle: str) -> subprocess.CompletedProcess[str]:
    """Run a shipped upgrade recipe with session state isolated from the repository."""
    prepared = tmp_path / "audit-state-test"
    prepared.mkdir(exist_ok=True)
    (prepared / "upgrade-run-dir").write_text(str(tmp_path) + "\n", encoding="utf-8")
    block = _bash_block("modes/upgrade.md", needle)
    return subprocess.run(
        ["bash"],
        input=block.replace("python ", shlex.quote(sys.executable) + " "),
        cwd=tmp_path,
        env={**os.environ, "TMPDIR": str(tmp_path), "CLAUDE_CODE_SESSION_ID": "test"},
        capture_output=True,
        text=True,
        check=False,
    )


@_BASH
def test_upgrade_after_arm_copies_exact_problems_and_skips_generation(tmp_path: Path) -> None:
    """Both calibration arms score the same problem and ground-truth bytes."""
    baseline = tmp_path / "calibrate/before"
    baseline.mkdir(parents=True)
    problems = b'[{"input":"fixed case", "ground_truth":["known answer"]}]\r\n'
    (baseline / "problems.json").write_bytes(problems)
    after = tmp_path / "calibrate/after"
    upgrade = tmp_path / "upgrade"
    upgrade.mkdir()
    (upgrade / "calibration-state.json").write_text(
        json.dumps({"before_dir": str(baseline), "after_dir": str(after)}), encoding="utf-8"
    )
    result = _upgrade_block(tmp_path, "calibration-state.json")
    assert result.returncode == 0, result.stderr
    assert (after / "problems.json").read_bytes() == problems
    assert (baseline / "problems.json").read_bytes() == problems
    after_instruction = next(line for line in _lines("modes/upgrade.md") if "**Step c" in line)
    assert "Phase 2" in after_instruction
    assert "1a–1c" in after_instruction


@_BASH
@pytest.mark.parametrize("original", [None, b"", b"line one\r\n\x00\xff\n"])
def test_upgrade_snapshot_restores_exact_bytes_or_absence(tmp_path: Path, original: bytes | None) -> None:
    """A rejected proposal restores binary bytes, empty files and newly created target absence."""
    upgrade = tmp_path / "upgrade"
    upgrade.mkdir()
    target = tmp_path / "settings.json"
    initial_path = target if original is not None else tmp_path / "unrelated.json"
    initial_path.write_bytes(original or b"")
    state = {
        "kind": "config",
        "proposal": "syntax",
        "action": "snapshot",
        "targets": [{"path": str(target), "slug": "settings"}],
    }
    state_path = upgrade / "proposal-state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    snapshot = _upgrade_block(tmp_path, "proposal-state.json")
    assert snapshot.returncode == 0, snapshot.stderr
    target.write_bytes(b"proposal's edited bytes\r\n")
    state_path.write_text(json.dumps({**state, "action": "record-edited"}), encoding="utf-8")
    recorded = _upgrade_block(tmp_path, "proposal-state.json")
    assert recorded.returncode == 0, recorded.stderr
    state_path.write_text(json.dumps({**state, "action": "restore"}), encoding="utf-8")
    restored = _upgrade_block(tmp_path, "proposal-state.json")
    assert restored.returncode == 0, restored.stderr
    assert (target.read_bytes() if target.exists() else None) == original


@_BASH
def test_upgrade_restore_refuses_concurrent_changes(tmp_path: Path) -> None:
    """A rollback cannot overwrite edits made after a proposal's post-edit snapshot."""
    upgrade = tmp_path / "upgrade"
    upgrade.mkdir()
    target = tmp_path / "agent.md"
    target.write_bytes(b"before")
    state = {
        "kind": "capability",
        "proposal": "routing",
        "action": "snapshot",
        "targets": [{"path": str(target), "slug": "agent"}],
    }
    state_path = upgrade / "proposal-state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    assert _upgrade_block(tmp_path, "proposal-state.json").returncode == 0
    target.write_bytes(b"proposal")
    state_path.write_text(json.dumps({**state, "action": "record-edited"}), encoding="utf-8")
    assert _upgrade_block(tmp_path, "proposal-state.json").returncode == 0
    target.write_bytes(b"other owner's edit")
    state_path.write_text(json.dumps({**state, "action": "restore"}), encoding="utf-8")
    result = _upgrade_block(tmp_path, "proposal-state.json")
    assert result.returncode != 0
    assert "concurrent" in result.stderr.lower()
    assert target.read_bytes() == b"other owner's edit"


@_BASH
def test_upgrade_gate_selects_latest_completed_audit_not_upgrade(tmp_path: Path) -> None:
    """An incomplete or newer upgrade run cannot displace the latest completed project audit."""
    root = tmp_path / ".reports/audit"
    older = root / "older"
    latest = root / "latest"
    incomplete = root / "incomplete"
    upgrade = root / "upgrade"
    older.mkdir(parents=True)
    latest.mkdir()
    incomplete.mkdir()
    (upgrade / "upgrade").mkdir(parents=True)
    (older / "summary.jsonl").write_bytes(b"")
    (latest / "summary.jsonl").write_bytes(b"")
    (incomplete / "summary.jsonl").write_bytes(b"")
    (upgrade / "summary.jsonl").write_bytes(b"")
    (older / "aggregate.md").write_text("No findings", encoding="utf-8")
    (latest / "aggregate.md").write_text("No findings", encoding="utf-8")
    (upgrade / "aggregate.md").write_text("No findings", encoding="utf-8")
    (older / "result.jsonl").write_text('{"scope":"agents"}\n', encoding="utf-8")
    (latest / "result.jsonl").write_text('{"scope":"agents"}\n', encoding="utf-8")
    (upgrade / "result.jsonl").write_text('{"mode":"upgrade"}\n', encoding="utf-8")
    (older / "report.md").write_text("Completed", encoding="utf-8")
    (latest / "report.md").write_text("Completed", encoding="utf-8")
    (upgrade / "report.md").write_text("Completed", encoding="utf-8")
    latest_stamp = (latest / "result.jsonl").stat().st_mtime
    os.utime(older / "result.jsonl", (latest_stamp - 60, latest_stamp - 60))
    result = _upgrade_block(tmp_path, "BASELINE_AUDIT_DIR")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"BASELINE_AUDIT_DIR={latest}"


@_BASH
def test_upgrade_gate_requests_fresh_audit_when_evidence_is_stale(tmp_path: Path) -> None:
    """Old completion evidence never authorizes an upgrade."""
    stale = tmp_path / ".reports/audit/stale"
    stale.mkdir(parents=True)
    (stale / "summary.jsonl").write_bytes(b"")
    (stale / "aggregate.md").write_text("No findings", encoding="utf-8")
    (stale / "report.md").write_text("Completed", encoding="utf-8")
    completion = stale / "result.jsonl"
    completion.write_text('{"scope":"agents"}\n', encoding="utf-8")
    os.utime(completion, (1, 1))
    result = _upgrade_block(tmp_path, "BASELINE_AUDIT_DIR")
    assert result.returncode != 0
    assert "fresh" in (result.stdout + result.stderr).lower()


@_BASH
def test_upgrade_validates_the_persisted_target_without_placeholders(tmp_path: Path) -> None:
    """A real changed JSON target is checked even when unrelated settings are absent."""
    prepared = tmp_path / "audit-state-test"
    prepared.mkdir()
    target = tmp_path / "changed target.json"
    target.write_text('{"valid":true}', encoding="utf-8")
    (prepared / "upgrade-target").write_text(str(target) + "\n", encoding="utf-8")
    result = _upgrade_block(tmp_path, "python -m json.tool")
    assert result.returncode == 0, result.stderr
    assert "✓ valid JSON" in result.stdout
    assert "<changed-" not in _bash_block("modes/upgrade.md", "python -m json.tool")


def test_upgrade_records_a_terminal_result_for_artifact_cleanup() -> None:
    """Upgrade-only runs finish with the same durable cleanup signal as standard audits."""
    lines = _lines("modes/upgrade.md")
    result = next(line for line in lines if "result.jsonl" in line and "completion" in line)
    assert "mode" in result
    assert "upgrade" in result
    assert "append_ledger.py" in result

"""Exercise calibration shell recipes against isolated run histories and source trees."""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from _hook_env import _bash_runs_posix_script, _symlinks_supported

_PLUGIN = Path(__file__).resolve().parents[2]
_CALIBRATE = _PLUGIN / "skills/calibrate"
_BASH = pytest.mark.skipif(not _bash_runs_posix_script(), reason="POSIX Bash execution unavailable")
_SHELLS = ["bash", pytest.param("zsh", marks=pytest.mark.skipif(not shutil.which("zsh"), reason="zsh unavailable"))]


def _block(relative: str, prefix: str) -> str:
    """Extract a runnable Bash fence by one command prefix."""
    lines = (_CALIBRATE / relative).read_text(encoding="utf-8").splitlines()
    for start, line in enumerate(lines):
        if line.strip() != "```bash":
            continue
        end = next(index for index in range(start + 1, len(lines)) if lines[index].strip() == "```")
        block = lines[start + 1 : end]
        if any(item.strip().startswith(prefix) for item in block):
            return "\n".join(block)
    raise AssertionError(f"Missing shell recipe {prefix!r} in {relative}")


@pytest.fixture
def shell_env(tmp_path: Path) -> dict[str, str]:
    """Keep recipes inside a private home with the project interpreter available."""
    return {
        **os.environ,
        "HOME": str(tmp_path),
        "USERPROFILE": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "CLAUDE_PLUGIN_ROOT": str(_PLUGIN),
        "CLAUDE_CODE_SESSION_ID": "calibrate-safety",
        "PATH": os.pathsep.join([str(Path(sys.executable).parent), os.environ.get("PATH", "")]),
    }


@_BASH
@pytest.mark.parametrize(
    ("target", "expected"),
    [
        pytest.param("/oss:analyse vitality", "oss-analyse-vitality", id="skill-mode"),
        pytest.param("foundry:sw-engineer", "foundry-sw-engineer", id="agent"),
        pytest.param("/foundry:audit", "foundry-audit", id="skill"),
    ],
)
def test_run_slug_preserves_label_without_windows_reserved_separators(
    tmp_path: Path, target: str, expected: str
) -> None:
    """Namespace and mode labels remain intact while their directories are portable."""
    script = _block("SKILL.md", "TARGET_LABEL=").replace("<TARGET_LABEL>", target)
    result = subprocess.run(["bash", "-c", script], cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [f"target-label={target}", f"run-slug={expected}"]
    assert not set('<>:"/\\|?*') & set(expected)


@_BASH
@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize(
    ("target", "relative"),
    [
        pytest.param("/example:inspect", "skills/inspect/SKILL.md", id="skill"),
        pytest.param("/example:inspect", "claude-skills/inspect/SKILL.md", id="claude-skill"),
        pytest.param("example:inspect", "agents/inspect.md", id="agent"),
    ],
)
def test_local_resolution_loads_each_source_layout(
    tmp_path: Path, shell_env: dict[str, str], shell: str, target: str, relative: str
) -> None:
    """Both supported shells resolve real fixture files instead of treating a list as one path."""
    source = tmp_path / "plugins/cc_example" / relative
    source.parent.mkdir(parents=True)
    source.write_text("fixture\n", encoding="utf-8")
    script = _block("templates/pipeline-prompt.md", "LOCAL_MODE=")
    script = script.replace("<LOCAL_MODE>", "true").replace("<TARGET>", target)
    result = subprocess.run(
        [shell, "-c", script], cwd=tmp_path, env=shell_env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"local mode: using plugins/cc_example/{relative}"


@_BASH
@pytest.mark.parametrize("orphan", [True, False])
def test_availability_ignores_orphaned_plugin_versions(tmp_path: Path, shell_env: dict[str, str], orphan: bool) -> None:
    """An orphaned cache directory cannot enable an unavailable cross-plugin calibration."""
    root = tmp_path / ".claude/plugins/cache/borda-ai-rig/oss/1.0.0"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin/plugin.json").write_text('{"name":"oss"}', encoding="utf-8")
    (root / (".orphaned_at" if orphan else ".live")).write_text("old\n", encoding="utf-8")
    script = _block("SKILL.md", 'if [ "$LOCAL_MODE"')
    result = subprocess.run(
        ["bash", "-c", script], cwd=tmp_path, env=shell_env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert f"OSS_AVAILABLE={'' if orphan else root}" in result.stdout.splitlines()


@_BASH
def test_benchmark_apply_refuses_a_missing_session_timestamp(tmp_path: Path, shell_env: dict[str, str]) -> None:
    """Losing session state must not authorize another session's latest proposal directory."""
    foreign = tmp_path / ".reports/calibrate/2030-01-01T00-00-00Z/foundry-curator"
    foreign.mkdir(parents=True)
    (foreign / "proposal.md").write_text("foreign\n", encoding="utf-8")
    script = _block("modes/apply.md", "export CSID=")
    result = subprocess.run(
        ["bash", "-c", script], cwd=tmp_path, env=shell_env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode != 0
    assert "TIMESTAMP state lost" in result.stdout
    assert "falling back" not in result.stdout


@_BASH
@pytest.mark.parametrize("shell", _SHELLS)
def test_pure_apply_selects_latest_run_holding_each_target(
    tmp_path: Path, shell_env: dict[str, str], shell: str
) -> None:
    """A newer unrelated run cannot hide a requested target's saved proposal."""
    old = tmp_path / ".reports/calibrate/2026-01-01T00-00-00Z/foundry-curator"
    old.mkdir(parents=True)
    (old / "proposal.md").write_text("older\n", encoding="utf-8")
    selected = tmp_path / ".reports/calibrate/2026-02-01T00-00-00Z/foundry-curator"
    selected.mkdir(parents=True)
    (selected / "proposal.md").write_text("selected\n", encoding="utf-8")
    unrelated = tmp_path / ".reports/calibrate/2026-03-01T00-00-00Z/foundry-sw-engineer"
    unrelated.mkdir(parents=True)
    (unrelated / "proposal.md").write_text("other\n", encoding="utf-8")
    script = _block("modes/apply.md", "RUN_SLUG=").replace("<RUN_SLUG>", "foundry-curator")
    result = subprocess.run(
        [shell, "-c", script], cwd=tmp_path, env=shell_env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert "timestamp=2026-02-01T00-00-00Z" in result.stdout.splitlines()
    assert "proposal-path=.reports/calibrate/2026-02-01T00-00-00Z/foundry-curator/proposal.md" in result.stdout
    assert not (tmp_path / "calibrate-state-calibrate-safety/timestamp").exists()


@_BASH
@pytest.mark.parametrize("shell", _SHELLS)
def test_pure_apply_reports_a_missing_target_without_creating_a_run(
    tmp_path: Path, shell_env: dict[str, str], shell: str
) -> None:
    """An empty history stays empty and produces an explicit missing-target result."""
    script = _block("modes/apply.md", "RUN_SLUG=").replace("<RUN_SLUG>", "foundry-curator")
    result = subprocess.run(
        [shell, "-c", script], cwd=tmp_path, env=shell_env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert "missing=foundry-curator" in result.stdout.splitlines()
    assert not (tmp_path / ".reports").exists()


@_BASH
@pytest.mark.parametrize("shell", _SHELLS)
def test_logging_collects_agent_and_nested_rule_results(tmp_path: Path, shell_env: dict[str, str], shell: str) -> None:
    """The history receives every target result, including rule pipelines' extra directory level."""
    state = tmp_path / "calibrate-state-calibrate-safety"
    state.mkdir()
    (state / "timestamp").write_text("2026-01-01T00-00-00Z\n", encoding="utf-8")
    agent = tmp_path / ".reports/calibrate/2026-01-01T00-00-00Z/foundry-curator/result.jsonl"
    agent.parent.mkdir(parents=True)
    agent.write_text('{"target":"foundry:curator"}\n', encoding="utf-8")
    rule = tmp_path / ".reports/calibrate/2026-01-01T00-00-00Z/rules/project-quality/result.jsonl"
    rule.parent.mkdir(parents=True)
    rule.write_text('{"target":"rules/project-quality"}\n', encoding="utf-8")
    (agent.parent.parent / "ledger-sources.txt").write_text(
        ".reports/calibrate/2026-01-01T00-00-00Z/foundry-curator/result.jsonl\n"
        ".reports/calibrate/2026-01-01T00-00-00Z/rules/project-quality/result.jsonl\n",
        encoding="utf-8",
    )
    script = _block("SKILL.md", "mkdir -p .notes/logs")
    result = subprocess.run(
        [shell, "-c", script], cwd=tmp_path, env=shell_env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    records = (tmp_path / ".notes/logs/calibrations.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["target"] for line in records] == ["foundry:curator", "rules/project-quality"]


@_BASH
@pytest.mark.parametrize("shell", _SHELLS)
def test_empty_result_set_does_not_abort_logging(tmp_path: Path, shell_env: dict[str, str], shell: str) -> None:
    """No-match discovery reports missing results without zsh aborting before the helper runs."""
    state = tmp_path / "calibrate-state-calibrate-safety"
    state.mkdir()
    (state / "timestamp").write_text("2026-01-01T00-00-00Z\n", encoding="utf-8")
    (tmp_path / ".reports/calibrate/2026-01-01T00-00-00Z").mkdir(parents=True)
    (tmp_path / ".reports/calibrate/2026-01-01T00-00-00Z/ledger-sources.txt").write_text("", encoding="utf-8")
    script = _block("SKILL.md", "mkdir -p .notes/logs")
    result = subprocess.run(
        [shell, "-c", script], cwd=tmp_path, env=shell_env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert "no result.jsonl" in result.stdout
    assert not (tmp_path / ".notes/logs/calibrations.jsonl").exists()


@_BASH
def test_failed_legacy_append_keeps_migration_retryable(tmp_path: Path, shell_env: dict[str, str]) -> None:
    """A failed append must leave the migration marker absent so legacy records are not lost."""
    state = tmp_path / "calibrate-state-calibrate-safety"
    state.mkdir()
    (state / "timestamp").write_text("2026-01-01T00-00-00Z\n", encoding="utf-8")
    legacy = tmp_path / ".claude/logs/calibrations.jsonl"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"target":"legacy"}\n', encoding="utf-8")
    (tmp_path / ".reports/calibrate/2026-01-01T00-00-00Z").mkdir(parents=True)
    script = "python() { return 23; }\n" + _block("SKILL.md", "mkdir -p .notes/logs")
    result = subprocess.run(
        ["bash", "-c", script], cwd=tmp_path, env=shell_env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode != 0
    assert not (tmp_path / ".notes/logs/.calibrations-legacy-migrated").exists()


@_BASH
def test_logging_keeps_selected_timeout_outcome_when_a_pipeline_writes_late(
    tmp_path: Path, shell_env: dict[str, str]
) -> None:
    """A late pipeline write cannot replace the incomplete outcome already chosen for the report."""
    state = tmp_path / "calibrate-state-calibrate-safety"
    state.mkdir()
    (state / "timestamp").write_text("2026-01-01T00-00-00Z\n", encoding="utf-8")
    target = tmp_path / ".reports/calibrate/2026-01-01T00-00-00Z/foundry-curator"
    target.mkdir(parents=True)
    (target / "result.jsonl").write_text('{"verdict":"calibrated"}\n', encoding="utf-8")
    (target / "result-orchestrator.jsonl").write_text('{"verdict":"incomplete"}\n', encoding="utf-8")
    (target.parent / "ledger-sources.txt").write_text(
        ".reports/calibrate/2026-01-01T00-00-00Z/foundry-curator/result-orchestrator.jsonl\n", encoding="utf-8"
    )
    script = _block("SKILL.md", "mkdir -p .notes/logs")
    result = subprocess.run(
        ["bash", "-c", script], cwd=tmp_path, env=shell_env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / ".notes/logs/calibrations.jsonl").read_text(encoding="utf-8") == '{"verdict":"incomplete"}\n'
    assert (target / "result.jsonl").read_text(encoding="utf-8") == '{"verdict":"calibrated"}\n'


@_BASH
def test_apply_deadline_does_not_require_a_benchmark_timestamp(tmp_path: Path, shell_env: dict[str, str]) -> None:
    """Pure apply can check its own agents after selecting targets from different historical runs."""
    state = tmp_path / "calibrate-state-calibrate-safety/apply"
    state.mkdir(parents=True)
    (state / "agent-watch-apply-1.tsv").write_text("foundry:curator\t-\t600\n", encoding="utf-8")
    script = _block("modes/apply.md", "APPLY_RUN_DIR=")
    result = subprocess.run(
        ["bash", "-c", script], cwd=tmp_path, env=shell_env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert not (state.parent / "timestamp").exists()


def test_humanizer_domain_names_the_installed_skill_namespace() -> None:
    """The prose-review domain dispatches a real plugin skill rather than a missing user skill."""
    lines = (_CALIBRATE / "modes/skills.md").read_text(encoding="utf-8").splitlines()
    domain = next(line for line in lines if line.startswith("- `/foundry:humanizer` →"))
    assert "check <file>" in domain
    assert (_PLUGIN / "skills/humanizer/SKILL.md").is_file()
    assert not any(line.startswith("- `/humanizer`") for line in lines)


@pytest.mark.parametrize("target", ["/develop:debug", "/foundry:investigate", "/codemap-py:test-impact"])
def test_added_domains_bound_execution_to_fixture_evidence(target: str) -> None:
    """Partial diagnostic and test-impact domains declare inspectable answers and avoid live execution."""
    lines = (_CALIBRATE / "modes/skills.md").read_text(encoding="utf-8").splitlines()
    domain = next(line for line in lines if line.startswith(f"- `{target}` →"))
    assert "ground truth =" in domain.lower()
    assert "synthetic" in domain
    assert "no " in domain or "never execute tests" in domain


def test_apply_requires_approval_of_filtered_concrete_edits() -> None:
    """Generated proposals cannot authorize new execution or bypass review through apply flags."""
    lines = (_CALIBRATE / "modes/apply.md").read_text(encoding="utf-8").splitlines()
    gate = next(line for line in lines if line.startswith("**Review and approval before dispatch**:"))
    assert "AskUserQuestion" in gate
    assert "exact remaining edits" in gate
    assert "reject any edit introducing commands, permission grants, tool access or URLs" in gate
    assert "pure apply, benchmark + `--apply`, and `--skip-gate`" in gate
    assert "On Cancel or no answer: no writes or curator dispatch" in gate
    assert not any("without second-guessing" in line for line in lines)


def test_pipeline_children_share_reserved_tier_slots_across_all_phases() -> None:
    """Concurrent pipelines cannot each consume the session's entire model-tier pool."""
    lines = (_CALIBRATE / "templates/pipeline-prompt.md").read_text(encoding="utf-8").splitlines()
    budget = next(line for line in lines if line.startswith("**Shared spawn budget**:"))
    assert "max(1, floor((tier ceiling - coordinators in that tier) / coordinators))" in budget
    assert "haiku 4 / sonnet 1 / opus 1" in budget
    assert "weak probes, probe scorers, target runs, baselines, Claude scorers and curator" in budget
    assert "counting all pipeline coordinators and children" in budget
    assert "haiku 20 / sonnet 8 / opus 5" in budget
    assert not any("Issue ALL spawns" in line or "all scorers in a single response" in line for line in lines)


@_BASH
@pytest.mark.parametrize("shell", _SHELLS)
def test_logging_includes_an_unterminated_final_source(tmp_path: Path, shell_env: dict[str, str], shell: str) -> None:
    """The Write tool's missing final newline must not silently discard the only selected outcome."""
    state = tmp_path / "calibrate-state-calibrate-safety"
    state.mkdir()
    (state / "timestamp").write_text("2030-01-01T00-00-00Z\n", encoding="utf-8")
    run = tmp_path / ".reports/calibrate/2030-01-01T00-00-00Z"
    run.mkdir(parents=True)
    source = run / "result.jsonl"
    source.write_text('{"target":"example:checker"}', encoding="utf-8")
    (run / "ledger-sources.txt").write_text(source.as_posix(), encoding="utf-8")
    result = subprocess.run(
        [shell, "-c", _block("SKILL.md", "mkdir -p .notes/logs")],
        cwd=tmp_path,
        env=shell_env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / ".notes/logs/calibrations.jsonl").read_text(encoding="utf-8") == source.read_text() + "\n"


@_BASH
@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize(
    "failure",
    [
        "missing",
        pytest.param("symlink", marks=pytest.mark.skipif(not _symlinks_supported(), reason="symlinks unavailable")),
    ],
)
def test_failed_later_source_never_appends_an_earlier_prefix(
    tmp_path: Path, shell_env: dict[str, str], shell: str, failure: str
) -> None:
    """A missing or rejected later source leaves history unchanged and a repaired retry writes each record once."""
    state = tmp_path / "calibrate-state-calibrate-safety"
    state.mkdir()
    (state / "timestamp").write_text("2030-01-01T00-00-00Z\n", encoding="utf-8")
    run = tmp_path / ".reports/calibrate/2030-01-01T00-00-00Z"
    run.mkdir(parents=True)
    first, last = run / "first.jsonl", run / "last.jsonl"
    first.write_text('{"target":"first"}\n', encoding="utf-8")
    last.symlink_to(first) if failure == "symlink" else None
    (run / "ledger-sources.txt").write_text(f"{first.as_posix()}\n{last.as_posix()}\n", encoding="utf-8")
    logs = tmp_path / ".notes/logs"
    logs.mkdir(parents=True)
    ledger = logs / "calibrations.jsonl"
    ledger.write_text('{"target":"existing"}\n', encoding="utf-8")
    script = _block("SKILL.md", "mkdir -p .notes/logs")
    failed = subprocess.run(
        [shell, "-c", script], cwd=tmp_path, env=shell_env, capture_output=True, text=True, timeout=10
    )
    assert failed.returncode != 0
    assert ledger.read_text(encoding="utf-8") == '{"target":"existing"}\n'
    last.unlink(missing_ok=True)
    last.write_text('{"target":"last"}\n', encoding="utf-8")
    retried = subprocess.run(
        [shell, "-c", script], cwd=tmp_path, env=shell_env, capture_output=True, text=True, timeout=10
    )
    assert retried.returncode == 0, retried.stderr
    assert ledger.read_text(encoding="utf-8").splitlines() == [
        '{"target":"existing"}',
        '{"target":"first"}',
        '{"target":"last"}',
    ]


@_BASH
@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize("legacy", ["example:checker", "checker", "example:inspect-graph"])
def test_pure_apply_reads_and_labels_historical_directories(
    tmp_path: Path, shell_env: dict[str, str], shell: str, legacy: str
) -> None:
    """Previously saved proposals remain reviewable while their canonical destination identity stays portable."""
    run = tmp_path / ".reports/calibrate/2030-01-01T00-00-00Z" / legacy
    run.mkdir(parents=True)
    (run / "proposal.md").write_text("saved proposal\n", encoding="utf-8")
    target, label = (
        ("/example:inspect", "/example:inspect graph")
        if "inspect" in legacy
        else ("example:checker", "example:checker")
    )
    slug = label.lstrip("/").replace(":", "-").replace(" ", "-")
    script = _block("modes/apply.md", "RUN_SLUG=")
    script = script.replace("<RUN_SLUG>", slug).replace("<TARGET_LABEL>", label).replace("<TARGET>", target)
    result = subprocess.run(
        [shell, "-c", script], cwd=tmp_path, env=shell_env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert f"legacy-directory={legacy}" in result.stdout.splitlines()
    assert f"run-slug={slug}" in result.stdout.splitlines()
    assert f"proposal-path=.reports/calibrate/2030-01-01T00-00-00Z/{legacy}/proposal.md" in result.stdout


@_BASH
@pytest.mark.parametrize(
    ("status", "label"),
    [pytest.param(1, "absent", id="absent"), pytest.param(2, "security or argument failure", id="refused")],
)
def test_availability_preserves_diagnostics_and_exit_reason(
    tmp_path: Path, shell_env: dict[str, str], status: int, label: str
) -> None:
    """Resolver refusal is visible rather than being silently collapsed into an uninstalled plugin."""
    script = f"python() {{ printf 'resolver diagnostic\\n' >&2; return {status}; }}\n"
    script += _block("SKILL.md", 'if [ "$LOCAL_MODE"')
    result = subprocess.run(
        ["bash", "-c", script], cwd=tmp_path, env=shell_env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr.count("resolver diagnostic") == 4
    assert f"oss: {label} (exit {status})" in result.stdout


def test_agent_and_skill_domain_identities_stay_distinct() -> None:
    """Every registered domain has its own label and agent substitutions cannot reintroduce colon directories."""
    lines = (_CALIBRATE / "modes/skills.md").read_text(encoding="utf-8").splitlines()
    table = lines[lines.index("### Domain table") : lines.index("### Step 2: Spawn skill pipeline subagents")]
    labels = [line.split("`", 2)[1] for line in table if line.startswith("- `/") and " → " in line]
    assert len(labels) == len(set(labels))
    assert "/codemap-py:query-code fn-blast" in labels
    agent_lines = (_CALIBRATE / "modes/agents.md").read_text(encoding="utf-8").splitlines()
    identity = next(line for line in agent_lines if line.startswith("- `<TARGET_LABEL>`"))
    assert "Step 1" in identity
    assert "foundry-sw-engineer" in identity
    assert not any("5 when this category" in line for line in agent_lines)


def test_shared_apply_destination_gets_one_reviewed_artifact_and_curator() -> None:
    """Domains editing one source share its snapshot and dispatch, with explicit handling of overlapping pairs."""
    lines = (_CALIBRATE / "modes/apply.md").read_text(encoding="utf-8").splitlines()
    handoff = next(line for line in lines if line.startswith("**Approved edit handoff**:"))
    assert "one frozen" in handoff
    assert "per resolved destination" in handoff
    assert "all approved pairs" in handoff
    assert "every target" in handoff
    assert "review sequence number" in handoff
    assert any("one curator per resolved destination" in line for line in lines)
    assert any("earlier pair" in line and "warning" in line for line in lines)
    gate = next(line for line in lines if line.startswith("**Review and approval before dispatch**:"))
    assert "refresh" in gate
    assert "contract" in gate
    assert "6 plain lines" in gate
    assert "`/compact`" in gate


def test_communication_uses_a_distinct_identity_and_complete_substitutions() -> None:
    """Communication cannot share curator's deliverable or omit local-mode substitution."""
    lines = (_CALIBRATE / "modes/communication.md").read_text(encoding="utf-8").splitlines()
    substitutions = next(line for line in lines if line.startswith("Use the standard pipeline template"))
    assert "<LOCAL_MODE>" in substitutions
    assert "<TARGET_LABEL>` = `<RUN_SLUG>` = `communication`" in substitutions
    assert "`foundry:curator`" in substitutions
    assert "Domain keys" in substitutions
    assert any("<TIMESTAMP>/communication/" in line for line in lines)


def test_manage_domain_avoids_unanswerable_rename_confirmation() -> None:
    """Only unambiguous quoted content edits enter calibration of the management workflow."""
    lines = (_CALIBRATE / "modes/skills.md").read_text(encoding="utf-8").splitlines()
    domain = next(line for line in lines if line.startswith("- `/foundry:manage update` →"))
    assert "quoted content-edit directives only" in domain
    assert "never rename, create, delete or permission operations" in domain
    assert "unambiguous type" in domain


def test_curator_receives_only_frozen_approved_pairs_with_source_identity() -> None:
    """Mixed proposals cannot let rejected or subsequently changed edits reach the applying child."""
    lines = (_CALIBRATE / "modes/apply.md").read_text(encoding="utf-8").splitlines()
    handoff = next(line for line in lines if line.startswith("**Approved edit handoff**:"))
    prompt = next(line for line in lines if line.startswith("Apply only the displayed and approved"))
    assert "only approved **Current**/**Proposed** pairs" in handoff
    assert "full reviewed source text" in handoff
    assert "rejected edits must not appear" in handoff
    assert "<APPROVED_EDITS_PATH>" in prompt
    assert "compare the actual source with the full reviewed source text" in prompt
    assert "mismatch: reject the target without writing" in prompt
    assert "Do not read the mutable original proposal" in prompt


def test_rule_category_completion_stays_with_the_collecting_parent() -> None:
    """Rules reporting does not repeat the category completion already performed when results were collected."""
    lines = (_CALIBRATE / "modes/rules.md").read_text(encoding="utf-8").splitlines()
    report_start = lines.index("### Report format (Step 3 output)")
    followup_start = lines.index("### Follow-up chain")
    assert not any('Mark "Calibrate rules" completed' in line for line in lines[report_start:followup_start])


@_BASH
@pytest.mark.parametrize("shell", _SHELLS)
def test_canonical_apply_history_takes_precedence_over_a_newer_legacy_run(
    tmp_path: Path, shell_env: dict[str, str], shell: str
) -> None:
    """Historical readers never replace an existing canonical proposal merely because the legacy run is newer."""
    canonical = tmp_path / ".reports/calibrate/2030-01-01T00-00-00Z/example-checker"
    legacy = tmp_path / ".reports/calibrate/2031-01-01T00-00-00Z/example:checker"
    canonical.mkdir(parents=True)
    legacy.mkdir(parents=True)
    (canonical / "proposal.md").write_text("canonical\n", encoding="utf-8")
    (legacy / "proposal.md").write_text("historical\n", encoding="utf-8")
    script = _block("modes/apply.md", "RUN_SLUG=")
    script = script.replace("<RUN_SLUG>", "example-checker").replace("<TARGET_LABEL>", "example:checker")
    script = script.replace("<TARGET>", "example:checker")
    result = subprocess.run(
        [shell, "-c", script], cwd=tmp_path, env=shell_env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert "timestamp=2030-01-01T00-00-00Z" in result.stdout.splitlines()
    assert "legacy-directory=" not in result.stdout


@_BASH
@pytest.mark.parametrize("shell", _SHELLS)
def test_historical_mode_lookup_never_adopts_a_base_domain_proposal(
    tmp_path: Path, shell_env: dict[str, str], shell: str
) -> None:
    """A saved generic query benchmark cannot stand in for the separately requested function-blast domain."""
    base = tmp_path / ".reports/calibrate/2030-01-01T00-00-00Z/example:inspect"
    base.mkdir(parents=True)
    (base / "proposal.md").write_text("different domain\n", encoding="utf-8")
    script = _block("modes/apply.md", "RUN_SLUG=")
    script = script.replace("<RUN_SLUG>", "example-inspect-callers")
    script = script.replace("<TARGET_LABEL>", "/example:inspect callers").replace("<TARGET>", "/example:inspect")
    result = subprocess.run(
        [shell, "-c", script], cwd=tmp_path, env=shell_env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["missing=example-inspect-callers"]


def test_fast_deadlines_cover_serial_retries_and_distinct_bridge_timeouts() -> None:
    """Armed deadlines cover serial probe retries plus generation and scoring's different bridge allowances."""
    lines = (_CALIBRATE / "SKILL.md").read_text(encoding="utf-8").splitlines()
    block = lines[lines.index("<constants>") + 1 : lines.index("</constants>")]
    constants = dict(line[2:].split(": ", 1) for line in block if line.startswith("- "))
    child = int(constants["CHILD_ALLOWANCE_S"].split()[0])
    overhead = int(constants["PIPELINE_OVERHEAD_S"].split()[0])
    fast = int(constants["PIPELINE_TIMEOUT_MIN"].split()[0])
    dual = int(constants["PIPELINE_TIMEOUT_MIN_DUAL"].split()[0])
    serial = 2 * 6 + 4 * 2 + 1
    assert fast == math.ceil((overhead + child * serial) / 60)
    assert dual == math.ceil((overhead + child * serial + 3 * 576 + 2 * 4 * 432) / 60)
    assert "576 × GENERATION_CALLS + 432 × 2 × TOTAL_PROBLEMS × ARMS" in constants["CODEX_ALLOWANCE_S"]
    assert "SERIAL_CHILDREN + CODEX_ALLOWANCE_S" in constants["PIPELINE_TIMEOUT_MIN_FULL"]

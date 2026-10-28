"""Keep each packaged specialist's task cue bound to its intended role."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from dataclasses import replace
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("role", sorted(path.parent.name for path in (PLUGIN_ROOT / "roles").glob("*/ROLE.md")))
def test_standalone_role_requires_confidence_deductions(role: str) -> None:
    """Prevent standalone cards from reporting an unexplained score or hiding zero-impact limits."""
    card = (PLUGIN_ROOT / "roles" / role / "ROLE.md").read_text(encoding="utf-8")
    confidence = card.split("## Confidence contract\n", 1)[1]

    assert "(-0.NN)" in confidence
    assert "ASCII minus and two decimals" in confidence
    assert "(-0.00)" in confidence
    assert "count overlapping causes once" in confidence
    assert "unique deductions sum exactly to `1.00 - score`" in confidence
    assert "score-setting caps/floors/bands and their contribution" in confidence
    assert "unexplained shortfall is one explicit residual" in confidence
    assert "evidence-backed judgment accounting" in confidence
    assert "not an empirically calibrated probability" in confidence


def _load_runner() -> object:
    """Load the shipped calibration runner for its role-routing check."""
    path = PLUGIN_ROOT / "runtime" / "calibration" / "run.py"
    spec = importlib.util.spec_from_file_location("codex_rig_role_task_runner", path)
    assert spec is not None
    assert spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = runner
    spec.loader.exec_module(runner)
    return runner


def test_every_role_owns_one_related_task_cue() -> None:
    """Catch a missing or ambiguous declared route across the complete role roster."""
    runner = _load_runner()
    cards = {role: (PLUGIN_ROOT / "roles" / role / "ROLE.md").read_text(encoding="utf-8") for role in runner.AGENTS}

    assert runner.validate_role_task_routing(cards) == []


def test_required_calibration_roster_covers_packaged_workflows() -> None:
    """Prevent a new artifact skill from silently escaping all required calibration."""
    runner = _load_runner()
    packaged = {path.parent.name for path in (PLUGIN_ROOT / "skills").glob("*/SKILL.md")}

    assert set(runner.SKILLS) == packaged - {"agent-shims"}


@pytest.mark.parametrize("missing", ["task", "behavior"])
def test_missing_kaggle_calibration_fails_roster_gate(tmp_path: Path, missing: str) -> None:
    """Require the ordinary roster consumer to reject either missing Kaggle input."""
    runner = _load_runner()
    paths = runner.Paths.create("plugin", tmp_path)
    source = paths.tasks if missing == "task" else paths.behavioral_cases
    payload = json.loads(source.read_text(encoding="utf-8"))
    key, target = ("tasks", "skill") if missing == "task" else ("cases", "target")
    payload[key] = [item for item in payload[key] if item[target] != "kaggle"]
    broken = tmp_path / source.name
    broken.write_text(json.dumps(payload), encoding="utf-8")
    paths = replace(paths, **{("tasks" if missing == "task" else "behavioral_cases"): broken})
    run = runner.CalibrationRun(paths=paths)

    runner.check_fixed_task_and_behavioral_rosters(run)

    assert run.checks_failed == ["fixed-task-set"]
    assert "kaggle" in run.paths.leaks.read_text(encoding="utf-8")


def test_complete_workflow_calibration_roster_passes(tmp_path: Path) -> None:
    """Keep the valid task-and-case producer accepted by the ordinary consumer."""
    runner = _load_runner()
    run = runner.CalibrationRun(paths=runner.Paths.create("plugin", tmp_path))

    runner.check_fixed_task_and_behavioral_rosters(run)

    assert run.checks_failed == []
    assert "fixed-task-roster=ok" in run.paths.checks.read_text(encoding="utf-8")


def test_kaggle_cases_cover_grounding_modes_runtime_and_authority() -> None:
    """Keep Kaggle's required negative scenarios and valid-mode controls in the scored case set."""
    payload = json.loads((PLUGIN_ROOT / "runtime/calibration/behavioral-cases.json").read_text(encoding="utf-8"))
    cases = [case for case in payload["cases"] if case["target"] == "kaggle"]
    findings = {finding for case in cases for finding in case["expected_findings"]}

    assert findings == {
        "kaggle-invented-schema",
        "kaggle-submission-unvalidated",
        "kaggle-eda-mode-leak",
        "kaggle-inference-retraining",
        "kaggle-unverified-runtime",
        "kaggle-unapproved-download",
    }
    assert {case["id"] for case in cases if not case["expected_findings"]} == {
        "kaggle-grounded-full",
        "kaggle-eda-only-valid",
        "kaggle-inference-only-valid",
    }


def test_plugin_calibration_records_the_role_task_gate(tmp_path: Path) -> None:
    """Prevent the runner from silently omitting the routing check."""
    runner = _load_runner()
    run = runner.CalibrationRun(paths=runner.Paths.create("plugin", tmp_path))

    runner.check_agents(run)

    assert run.checks_failed == []
    assert "agent-task-routing=ok:roles=15:declared-only" in run.paths.checks.read_text(encoding="utf-8")


def test_python_source_review_cue_cannot_disappear_or_move_to_qa() -> None:
    """Catch recurrence of the missing software reviewer and an ambiguous QA route."""
    runner = _load_runner()
    cards = {role: (PLUGIN_ROOT / "roles" / role / "ROLE.md").read_text(encoding="utf-8") for role in runner.AGENTS}
    cue = runner.ROLE_TASK_CUES["sw-engineer"]
    cards["sw-engineer"] = cards["sw-engineer"].replace(cue, "generic code review")
    assert runner.validate_role_task_routing(cards) == ["role-task-route:sw-engineer:missing"]

    cards["sw-engineer"] = (PLUGIN_ROOT / "roles" / "sw-engineer" / "ROLE.md").read_text(encoding="utf-8")
    cards["qa-specialist"] = cards["qa-specialist"].replace("- Trigger:", f"- Trigger: {cue};", 1)
    assert runner.validate_role_task_routing(cards) == [
        "role-task-route:sw-engineer:ambiguous:qa-specialist,sw-engineer"
    ]

    cards["sw-engineer"] = cards["sw-engineer"].replace(cue, "generic code review")
    assert runner.validate_role_task_routing(cards) == ["role-task-route:sw-engineer:wrong-role:qa-specialist"]


@pytest.mark.parametrize("role", sorted(path.parent.name for path in (PLUGIN_ROOT / "roles").glob("*/ROLE.md")))
def test_calibration_rejects_a_copied_card_without_deduction_accounting(tmp_path: Path, role: str) -> None:
    """Detect loss of standalone accounting in any role without changing shipped cards."""
    runner = _load_runner()
    copied_roles = tmp_path / "copied-roles"
    shutil.copytree(PLUGIN_ROOT / "roles", copied_roles)
    card = copied_roles / role / "ROLE.md"
    original = card.read_text(encoding="utf-8")
    deduction_line = next(
        line for line in original.splitlines() if line.startswith("For every reported gap or limitation")
    )
    card.write_text(original.replace(deduction_line, "", 1), encoding="utf-8", newline="\n")
    run = runner.CalibrationRun(paths=runner.Paths.create("plugin", tmp_path))
    run.paths.roles_dir = copied_roles

    runner.check_agents(run)

    assert run.checks_failed == ["confidence-policy"]
    assert run.fails == 1
    assert f"role-confidence-deductions-missing:{role}" in run.paths.leaks.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "tokens",
    [
        pytest.param(("result-template.json",), id="result-template"),
        pytest.param(("helper-cli-contract",), id="helper-contract"),
        pytest.param((".reports/codex/code-remediate/", "create_run.py --skill code-remediate"), id="run-location"),
    ],
)
def test_skill_schema_checks_relocated_phase_obligations(tmp_path: Path, tokens: tuple[str, ...]) -> None:
    """Accept a phase-loaded contract while still rejecting an obligation removed from its references."""
    runner = _load_runner()
    copied_skills = tmp_path / "skills"
    shutil.copytree(PLUGIN_ROOT / "skills", copied_skills)
    paths = runner.Paths.create("plugin", tmp_path)
    paths.skills_dir = copied_skills
    complete = runner.CalibrationRun(paths=paths)

    runner.check_core_configs(complete)

    assert "skill-schema-all" not in complete.checks_failed
    for reference in (copied_skills / "code-remediate" / "references").glob("*.md"):
        text = reference.read_text(encoding="utf-8")
        for token in tokens:
            text = text.replace(token, "removed-obligation")
        reference.write_text(text, encoding="utf-8", newline="\n")
    broken = runner.CalibrationRun(paths=runner.Paths.create("plugin", tmp_path))
    broken.paths.skills_dir = copied_skills

    runner.check_core_configs(broken)

    assert "skill-schema-all" in broken.checks_failed
    leaks = broken.paths.leaks.read_text(encoding="utf-8")
    assert "missing:" in leaks
    assert "code-remediate/SKILL.md" in leaks.replace("\\", "/")

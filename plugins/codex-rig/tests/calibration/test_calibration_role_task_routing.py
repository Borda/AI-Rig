"""Keep each packaged specialist's task cue bound to its intended role.

Also pins the role cards' standalone confidence contract and the calibration runner checks that guard the role, skill
and Kaggle rosters, so a dropped cue, card clause or roster entry fails calibration instead of passing silently.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest
from _modules import load_module_scoped

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
ROLES = sorted(path.parent.name for path in (PLUGIN_ROOT / "roles").glob("*/ROLE.md"))
CONFIDENCE_MARKERS = (
    "(-0.NN)",
    "ASCII minus and two decimals",
    "(-0.00)",
    "count overlapping causes once",
    "unique deductions sum exactly to `1.00 - score`",
    "score-setting caps/floors/bands and their contribution",
    "unexplained shortfall is one explicit residual",
    "evidence-backed judgment accounting",
    "not an empirically calibrated probability",
)
CONFIDENCE_HEADING = "## Confidence contract"


@pytest.fixture(scope="module", name="runner")
def _runner() -> Iterator[ModuleType]:
    """Load the shipped calibration runner once for its role-routing checks."""
    with pytest.MonkeyPatch.context() as patch:
        yield load_module_scoped(
            "codex_rig_role_task_runner", PLUGIN_ROOT / "runtime" / "calibration" / "run.py", patch
        )


@pytest.fixture(name="cards")
def _cards(runner: ModuleType) -> dict[str, str]:
    """Read every role card in the calibration roster."""
    return {role: (PLUGIN_ROOT / "roles" / role / "ROLE.md").read_text(encoding="utf-8") for role in runner.AGENTS}


def _section_text(card: str, heading: str) -> str:
    """Return the section body under `heading` up to the next level-2 heading, with whitespace normalized."""
    lines = card.splitlines()
    start = lines.index(heading) + 1
    body = []
    for line in lines[start:]:
        if line.startswith("## "):
            break
        body.append(line)
    return " ".join(" ".join(body).split())


@pytest.mark.parametrize("role", ROLES)
def test_standalone_role_requires_confidence_deductions(role: str) -> None:
    """A standalone role card states the full deduction accounting for its confidence score.

    Without it a card could report an unexplained score or hide zero-impact limits, so every marker of the contract must
    appear in the card's confidence section whatever the prose wrapping.
    """
    card = (PLUGIN_ROOT / "roles" / role / "ROLE.md").read_text(encoding="utf-8")
    confidence = _section_text(card, CONFIDENCE_HEADING)

    assert [marker for marker in CONFIDENCE_MARKERS if marker not in confidence] == []


@pytest.mark.parametrize(
    ("source_field", "items_key", "target_key"),
    [
        pytest.param("tasks", "tasks", "skill", id="task"),
        pytest.param("behavioral_cases", "cases", "target", id="behavior"),
    ],
)
def test_missing_kaggle_calibration_fails_roster_gate(
    runner: ModuleType, tmp_path: Path, source_field: str, items_key: str, target_key: str
) -> None:
    """The roster gate rejects a fixed task set or behavioral case set that lacks its Kaggle entries.

    Kaggle rows are stripped from a copy of the task file or the case file in turn, and the ordinary roster consumer
    must fail `fixed-task-set` and name kaggle in the leak log.
    """
    paths = runner.Paths.create("plugin", tmp_path)
    source = getattr(paths, source_field)
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload[items_key] = [item for item in payload[items_key] if item[target_key] != "kaggle"]
    broken = tmp_path / source.name
    broken.write_text(json.dumps(payload), encoding="utf-8")
    run = runner.CalibrationRun(paths=replace(paths, **{source_field: broken}))

    runner.check_fixed_task_and_behavioral_rosters(run)

    assert run.checks_failed == ["fixed-task-set"]
    assert "kaggle" in run.paths.leaks.read_text(encoding="utf-8")


def test_complete_workflow_calibration_roster_passes(runner: ModuleType, tmp_path: Path) -> None:
    """The shipped task and case rosters are accepted by the ordinary roster consumer."""
    run = runner.CalibrationRun(paths=runner.Paths.create("plugin", tmp_path))

    runner.check_fixed_task_and_behavioral_rosters(run)

    assert run.checks_failed == []
    assert "fixed-task-roster=ok" in run.paths.checks.read_text(encoding="utf-8")


def test_kaggle_cases_cover_grounding_modes_runtime_and_authority() -> None:
    """The scored case set keeps Kaggle's required negative scenarios and its valid-mode controls.

    Six distinct failure findings (schema, submission, mode leak, retraining, runtime, download) and three clean
    controls must stay present, so Kaggle behavior cannot lose a scenario without calibration noticing.
    """
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


def test_plugin_calibration_records_the_role_task_gate(runner: ModuleType, tmp_path: Path) -> None:
    """The agent check records the role-task routing gate in its check log instead of silently omitting it."""
    run = runner.CalibrationRun(paths=runner.Paths.create("plugin", tmp_path))

    runner.check_agents(run)

    assert run.checks_failed == []
    assert "agent-task-routing=ok:roles=15:declared-only" in run.paths.checks.read_text(encoding="utf-8")


class TestPythonSourceReviewCue:
    """The software reviewer keeps its own Python source review cue, which no other role may take over."""

    GENERIC = "generic code review"

    def test_missing_cue_is_reported(self, runner: ModuleType, cards: dict[str, str]) -> None:
        """Removing the cue from the software reviewer card reports the route as missing."""
        cue = runner.ROLE_TASK_CUES["sw-engineer"]
        cards["sw-engineer"] = cards["sw-engineer"].replace(cue, self.GENERIC)

        assert runner.validate_role_task_routing(cards) == ["role-task-route:sw-engineer:missing"]

    def test_cue_on_qa_makes_the_route_ambiguous(self, runner: ModuleType, cards: dict[str, str]) -> None:
        """The cue appearing on the QA card too reports the route as ambiguous between both roles."""
        cue = runner.ROLE_TASK_CUES["sw-engineer"]
        cards["qa-specialist"] = cards["qa-specialist"].replace("- Trigger:", f"- Trigger: {cue};", 1)

        assert runner.validate_role_task_routing(cards) == [
            "role-task-route:sw-engineer:ambiguous:qa-specialist,sw-engineer"
        ]

    def test_cue_moved_to_qa_is_a_wrong_role(self, runner: ModuleType, cards: dict[str, str]) -> None:
        """The cue present only on the QA card reports the software reviewer route as the wrong role."""
        cue = runner.ROLE_TASK_CUES["sw-engineer"]
        cards["qa-specialist"] = cards["qa-specialist"].replace("- Trigger:", f"- Trigger: {cue};", 1)
        cards["sw-engineer"] = cards["sw-engineer"].replace(cue, self.GENERIC)

        assert runner.validate_role_task_routing(cards) == ["role-task-route:sw-engineer:wrong-role:qa-specialist"]


@pytest.mark.parametrize("role", ROLES)
def test_calibration_rejects_a_copied_card_without_deduction_accounting(
    runner: ModuleType, tmp_path: Path, role: str
) -> None:
    """Calibration fails when any one role card loses its deduction-accounting clause.

    The cards are copied into a temporary roles directory so the shipped files stay untouched; removing the clause from
    a single role must fail `confidence-policy` and name that role in the leak log.
    """
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


def _copied_skills_run(runner: ModuleType, tmp_path: Path) -> object:
    """Return a calibration run whose skills directory is a private copy of the shipped skills."""
    copied_skills = tmp_path / "skills"
    shutil.copytree(PLUGIN_ROOT / "skills", copied_skills)
    run = runner.CalibrationRun(paths=runner.Paths.create("plugin", tmp_path))
    run.paths.skills_dir = copied_skills
    return run


def _remove_tokens_from_references(skills: Path, tokens: tuple[str, ...]) -> None:
    """Replace every listed token in the code-remediate reference files with a placeholder."""
    for reference in (skills / "code-remediate" / "references").glob("*.md"):
        text = reference.read_text(encoding="utf-8")
        for token in tokens:
            text = text.replace(token, "removed-obligation")
        reference.write_text(text, encoding="utf-8", newline="\n")


def test_skill_schema_accepts_complete_phase_obligations(runner: ModuleType, tmp_path: Path) -> None:
    """An unmodified copy of the skills passes the skill-schema check, so obligations loaded by phase count."""
    run = _copied_skills_run(runner, tmp_path)

    runner.check_core_configs(run)

    assert "skill-schema-all" not in run.checks_failed


@pytest.mark.parametrize(
    "tokens",
    [
        pytest.param(("result-template.json",), id="result-template"),
        pytest.param(("helper-cli-contract",), id="helper-contract"),
        pytest.param((".reports/codex/code-remediate/", "create_run.py --skill code-remediate"), id="run-location"),
    ],
)
def test_skill_schema_rejects_obligation_removed_from_references(
    runner: ModuleType, tmp_path: Path, tokens: tuple[str, ...]
) -> None:
    """The skill-schema check fails when a phase-loaded obligation disappears from the skill's references.

    The obligations now live in the code-remediate reference files; replacing the result template, helper contract or
    run-location tokens there must still be reported as a missing obligation for that skill.
    """
    run = _copied_skills_run(runner, tmp_path)
    _remove_tokens_from_references(run.paths.skills_dir, tokens)

    runner.check_core_configs(run)

    leaks = run.paths.leaks.read_text(encoding="utf-8")
    assert "skill-schema-all" in run.checks_failed
    assert "missing:" in leaks
    assert "code-remediate/SKILL.md" in leaks.replace("\\", "/")

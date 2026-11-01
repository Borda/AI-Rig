"""Keep every output the efficiency rollout touched as visible as it was before the rollout.

The rollout folded helper calls, narrowed loop test runs, and replaced polling waits. None of that may hide an artifact,
a validation output, a per-stage outcome, a pending or timed-out agent, or the reason a targeted test set was chosen.
``head_observable_artifacts.json`` snapshots the run artifacts each touched skill named before the rollout.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from _modules import load_module
from _skill_text import missing_phrases, read_with_references

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
#: Skills that load part of their instructions from a trigger-loaded reference rather than SKILL.md.
_MOVED_REFERENCES = {
    "challenge-resolve": ("chunk-path.md",),
    "code-review": (
        "source-and-scope.md",
        "routing.md",
        "legacy-dispatch.md",
        "findings-and-completion.md",
        "batched-review.md",
        "collection-failure.md",
        "historical-routes.md",
        "report-recovery.md",
        "validation-recovery.md",
    ),
    "code-remediate": (
        "intake.md",
        "selection.md",
        "implementation.md",
        "completion.md",
        "existing-conflict.md",
        "missing-review-decision.md",
        "source-recovery.md",
    ),
}
HEAD_ARTIFACTS = json.loads(Path(__file__).with_name("head_observable_artifacts.json").read_text(encoding="utf-8"))


def _text(relative: str) -> str:
    """Return one contract file with wrapping collapsed so assertions survive reflow."""
    return " ".join((PLUGIN_ROOT / relative).read_text(encoding="utf-8").split())


def _load(name: str, relative: str) -> ModuleType:
    """Load one shipped helper by file path."""
    return load_module(name, PLUGIN_ROOT / relative)


@pytest.mark.parametrize(("skill", "artifacts"), sorted(HEAD_ARTIFACTS.items()))
def test_pre_rollout_artifacts_are_still_named(skill: str, artifacts: list[str]) -> None:
    """Keep every run artifact a touched skill named before the rollout in its current contract.

    ``head_observable_artifacts.json`` lists the artifacts per skill; an artifact that disappeared from the skill text
    (and from its trigger-loaded references) would silently stop being produced or reported.
    """
    text = read_with_references(PLUGIN_ROOT / "skills" / skill / "SKILL.md", *_MOVED_REFERENCES.get(skill, ()))

    assert [artifact for artifact in artifacts if artifact not in text] == []


def test_finalize_writes_stage_artifacts_and_reports_each_stage(tmp_path: Path) -> None:
    """Write the same handoff, rendered output, digest record, and candidate the separate calls wrote, per stage.

    One summary line is not enough: each stage keeps its own status and detail so tooling sees what the separate render,
    write-result, and validator calls showed before. The real finalize CLI runs against a disposable remediation run.
    """
    from test_finding_presentation import _write_remediation_candidate
    from test_remediation_finalize import _draft_handoff, _stale_metadata

    result_path = _write_remediation_candidate(tmp_path, "code", ("implemented", "Added the guard."))
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["metadata"]["unresolved_summary"].update(selected_items_total=1, selected_items_resolved=1)
    drafts = tmp_path / "drafts"
    drafts.mkdir()
    (drafts / "metadata.json").write_text(json.dumps(_stale_metadata(result)), encoding="utf-8")
    (drafts / "handoff.json").write_text(json.dumps(_draft_handoff(tmp_path)), encoding="utf-8")
    command = [sys.executable, str(PLUGIN_ROOT / "shared" / "remediation_finalize.py"), "finalize", "--run"]
    command += [str(tmp_path), "--metadata", str(drafts / "metadata.json"), "--handoff", str(drafts / "handoff.json")]
    command += ["--status", "pass", "--confidence", str(result["confidence"]), "--artifact-path"]
    command += [result["artifact_path"]]

    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    summary = json.loads(completed.stdout)

    assert completed.returncode == 0, completed.stdout
    artifacts = ("final-handoff.json", "final.md", "final-handoff.validation.json", "result.candidate.json")
    assert [name for name in artifacts if not (tmp_path / name).is_file()] == []
    steps = {step["step"]: step for step in summary["steps"]}
    assert set(steps) == {"derive", "render", "write-result", "validate"}
    assert steps["render"]["detail"]["rendered_sha256"]
    assert steps["write-result"]["detail"]["candidate"].endswith("result.candidate.json")
    assert steps["validate"]["detail"] == {"errors": 0, "not_run": 0}


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(["shared/validate-artifacts.py", "--skill", "code-remediate"], id="shared-validator"),
        pytest.param(
            ["skills/code-review/validate_artifacts.py", "--parent-thread-id", "thread"], id="review-validator"
        ),
    ],
)
def test_default_validator_output_stays_fail_fast_without_all_errors(tmp_path: Path, command: list[str]) -> None:
    """Keep the default fail-fast stderr code and empty stdout; the JSON report appears only on request.

    A malformed candidate is fed to each real validator CLI without ``--all-errors``; scripts that parse stdout rely on
    it staying empty and on stderr carrying a plain code rather than the JSON report.
    """
    (tmp_path / "result.candidate.json").write_text(json.dumps({"status": "pass"}), encoding="utf-8")
    argv = [sys.executable, str(PLUGIN_ROOT / command[0]), *command[1:], "--out", str(tmp_path)]
    argv += ["--result", str(tmp_path / "result.candidate.json")]

    completed = subprocess.run(argv, capture_output=True, text=True, check=False)

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr.strip()
    assert not completed.stderr.strip().startswith("{")


def test_review_validator_all_errors_marks_dependent_checks_not_run(tmp_path: Path) -> None:
    """Report a failed shape check and list the review checks it blocked instead of passing them.

    A candidate with only a status fails the result-shape step; the review checks depend on that shape, so the report
    must name them as not run rather than omit them or count them as passed.
    """
    (tmp_path / "result.candidate.json").write_text(json.dumps({"status": "pass"}), encoding="utf-8")
    validator = _load("codex_rig_review_validator", "skills/code-review/validate_artifacts.py")

    report = validator.collect_result_errors(tmp_path, tmp_path / "result.candidate.json", tmp_path, "thread", tmp_path)

    assert report["status"] == "fail"
    assert report["errors"] == [{"step": "result-shape", "code": "result-missing-metadata"}]
    assert report["not_run"] == [{"step": "review-checks", "blocked_by": "result-shape"}]


def test_targeted_selection_is_recorded_in_the_run_directory(tmp_path: Path) -> None:
    """Persist which tests a targeted run selected and why, so it stays auditable against a full run.

    The real target helper runs against a tiny project with one changed module; its JSON record under the run directory
    must map the chosen test file to the heuristic that picked it.
    """
    (tmp_path / "tests").mkdir()
    (tmp_path / "mod.py").write_text("X = 1\n", encoding="utf-8")
    (tmp_path / "tests" / "test_mod.py").write_text("import mod\n", encoding="utf-8")
    out = tmp_path / "run" / "test-targets-01.json"
    command = [sys.executable, str(PLUGIN_ROOT / "shared" / "test_targets.py"), "--root", str(tmp_path)]
    command += ["--changed", "mod.py", "--no-codemap", "--out", str(out)]

    completed = subprocess.run(command, capture_output=True, text=True, check=False)

    assert completed.returncode == 0, completed.stderr
    assert json.loads(out.read_text(encoding="utf-8"))["sources"] == {"tests/test_mod.py": "heuristic"}


@pytest.mark.parametrize(
    ("relative", "phrase"),
    [
        pytest.param(
            "shared/native-skill-contract.md",
            "--out <run-directory>/test-targets-<NN>.json",
            id="native-contract-names-the-record-path",
        ),
        pytest.param(
            "skills/code-review/references/findings-and-completion.md",
            "record the exact pytest arguments in `review-notes.md`",
            id="review-records-the-pytest-arguments",
        ),
    ],
)
def test_targeted_selection_record_is_documented(relative: str, phrase: str) -> None:
    """Tell agents to persist the targeted selection so a loop run stays auditable against a full run."""
    assert phrase in _text(relative)


def test_waits_still_report_pending_and_timed_out_agents() -> None:
    """Keep pending and timed-out children visible after polling waits were replaced.

    The shared native contract is the single place the wait rule lives; its progress-update wording must still name
    every pending child and every child that timed out.
    """
    native = _text("shared/native-skill-contract.md")

    phrases = (
        "the progress update names every child still pending and every child that timed out",
        "keep each child's terminal state",
    )
    assert missing_phrases(native, phrases) == []


def test_empty_wait_requires_evidence_based_status_before_another_wait() -> None:
    """Prevent silent empty-wait loops and unsupported claims that no children are running.

    An empty wait return proves nothing about the children, so the contract must demand a dispatch to wait on and an
    evidence-based status update before any further wait or tool call.
    """
    native = _text("shared/native-skill-contract.md")

    phrases = (
        "Before waiting, identify at least one accepted dispatch still awaiting completion or join",
        "If none remains, do not wait",
        "before another wait or other tool call",
        "role/task, elapsed time, last observed event",
        "no new progress evidence",
        "does not prove that a child is progressing, stalled, absent or complete",
        "Tool-generated waiting banners do not satisfy this update",
    )
    assert missing_phrases(native, phrases) == []


@pytest.mark.parametrize(
    "relative",
    [
        "assets/AGENTS.md",
        "shared/specialist-orchestration.md",
        "skills/code-review/references/legacy-dispatch.md",
        "skills/challenge-resolve/SKILL.md",
    ],
)
def test_wait_call_sites_keep_the_status_checkpoint_visible(relative: str) -> None:
    """Keep notification mandatory where the workflow tells the agent to wait."""
    assert (
        "After every wait, including an empty return, report completed/expected counts, pending roles and elapsed time "
        "before any next tool call" in _text(relative)
    )


@pytest.mark.parametrize("skill", sorted(path.parent.name for path in (PLUGIN_ROOT / "skills").glob("*/SKILL.md")))
def test_every_skill_references_the_shared_execution_rules(skill: str) -> None:
    """Route every codex-rig skill through the shared native contract that holds the wait, plan, and test rules."""
    assert "native-skill-contract.md" in _text(f"skills/{skill}/SKILL.md")


def test_shared_contract_holds_the_wait_plan_and_test_sections() -> None:
    """Keep the sections every skill points at: agent waits, plan updates and sandboxed test runs.

    Each skill only references the shared contract by file name, so this single check proves the referenced sections
    still exist instead of re-checking them once per skill.
    """
    native = _text("shared/native-skill-contract.md")

    assert missing_phrases(native, ("## Agent Waits", "## Plan Updates", "## Sandboxed Test Runs")) == []

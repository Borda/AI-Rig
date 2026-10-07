"""Keep every output the efficiency rollout touched as visible as it was before the rollout.

The rollout folded helper calls, narrowed loop test runs, and replaced polling waits. None of that may hide an artifact,
a validation output, a per-stage outcome, a pending or timed-out agent, or the reason a targeted test set was chosen.
``head_observable_artifacts.json`` snapshots the run artifacts each touched skill named before the rollout.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
HEAD_ARTIFACTS = json.loads(Path(__file__).with_name("head_observable_artifacts.json").read_text(encoding="utf-8"))


def _text(relative: str) -> str:
    """Return one contract file with wrapping collapsed so assertions survive reflow."""
    return " ".join((PLUGIN_ROOT / relative).read_text(encoding="utf-8").split())


def _load(name: str, relative: str) -> ModuleType:
    """Load one shipped helper by file path."""
    spec = importlib.util.spec_from_file_location(name, PLUGIN_ROOT / relative)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(("skill", "artifacts"), sorted(HEAD_ARTIFACTS.items()))
def test_every_pre_rollout_artifact_is_still_named(skill: str, artifacts: list[str]) -> None:
    """Keep every run artifact a touched skill named before the rollout in its current contract."""
    text = (PLUGIN_ROOT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")

    assert [artifact for artifact in artifacts if artifact not in text] == []


def test_finalize_writes_every_stage_artifact_and_reports_each_stage(tmp_path: Path) -> None:
    """Write the same handoff, rendered output, digest record, and candidate the separate calls wrote, per stage.

    One summary line is not enough: each stage keeps its own status and detail so tooling sees what the separate render,
    write-result, and validator calls showed before.
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
    for name in ("final-handoff.json", "final.md", "final-handoff.validation.json", "result.candidate.json"):
        assert (tmp_path / name).is_file()
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
def test_default_validator_output_is_unchanged_without_all_errors(tmp_path: Path, command: list[str]) -> None:
    """Keep the default fail-fast stderr code and empty stdout; the JSON report appears only on request."""
    (tmp_path / "result.candidate.json").write_text(json.dumps({"status": "pass"}), encoding="utf-8")
    argv = [sys.executable, str(PLUGIN_ROOT / command[0]), *command[1:], "--out", str(tmp_path)]
    argv += ["--result", str(tmp_path / "result.candidate.json")]

    completed = subprocess.run(argv, capture_output=True, text=True, check=False)

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr.strip()
    assert not completed.stderr.strip().startswith("{")


def test_review_validator_all_errors_marks_dependent_checks_not_run(tmp_path: Path) -> None:
    """Report a failed shape check and list the review checks it blocked instead of passing them."""
    (tmp_path / "result.candidate.json").write_text(json.dumps({"status": "pass"}), encoding="utf-8")
    validator = _load("codex_rig_review_validator", "skills/code-review/validate_artifacts.py")

    report = validator.collect_result_errors(tmp_path, tmp_path / "result.candidate.json", tmp_path, "thread", tmp_path)

    assert report["status"] == "fail"
    assert report["errors"] == [{"step": "result-shape", "code": "result-missing-metadata"}]
    assert report["not_run"] == [{"step": "review-checks", "blocked_by": "result-shape"}]


def test_targeted_selection_is_recorded_in_the_run_directory(tmp_path: Path) -> None:
    """Persist which tests a targeted run selected and why, so it stays auditable against a full run."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "mod.py").write_text("X = 1\n", encoding="utf-8")
    (tmp_path / "tests" / "test_mod.py").write_text("import mod\n", encoding="utf-8")
    out = tmp_path / "run" / "test-targets-01.json"
    command = [sys.executable, str(PLUGIN_ROOT / "shared" / "test_targets.py"), "--root", str(tmp_path)]
    command += ["--changed", "mod.py", "--no-codemap", "--out", str(out)]

    completed = subprocess.run(command, capture_output=True, text=True, check=False)

    assert completed.returncode == 0, completed.stderr
    assert json.loads(out.read_text(encoding="utf-8"))["sources"] == {"tests/test_mod.py": "heuristic"}
    assert "--out <run-directory>/test-targets-<NN>.json" in _text("shared/native-skill-contract.md")
    assert "record the exact pytest arguments in `review-notes.md`" in _text("skills/code-review/SKILL.md")


def test_waits_still_report_pending_and_timed_out_agents() -> None:
    """Keep pending and timed-out children visible after polling waits were replaced."""
    native = _text("shared/native-skill-contract.md")

    assert "the progress update names every child still pending and every child that timed out" in native
    assert "keep each child's terminal state" in native


@pytest.mark.parametrize("skill", sorted(path.parent.name for path in (PLUGIN_ROOT / "skills").glob("*/SKILL.md")))
def test_every_skill_references_the_shared_execution_rules(skill: str) -> None:
    """Route every codex-rig skill through the shared native contract that holds the wait, plan, and test rules."""
    text = _text(f"skills/{skill}/SKILL.md")
    native = _text("shared/native-skill-contract.md")

    assert "native-skill-contract.md" in text
    for heading in ("## Agent Waits", "## Plan Updates", "## Sandboxed Test Runs"):
        assert heading in native

"""Keep inspection availability and actionable pauses explicit in shipped review instructions."""

import json
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.installed_plugin
def test_every_skill_preserves_its_closing_gate_after_reentry() -> None:
    """Keep user intervention from turning resumed skills into informal final summaries."""
    contract = (PLUGIN_ROOT / "shared/helper-cli-contract.md").read_text(encoding="utf-8")
    assert "## Resume And Re-entry" in contract
    section = contract.split("## Resume And Re-entry", 1)[1].split("\n## ", 1)[0]
    for requirement in (
        "every skill",
        "user intervention",
        "repeated skill invocation",
        "first unmet checkpoint",
        "normal closing gate",
        "not an exact-output override",
        "never replay completed work",
        "agent-shims",
    ):
        assert requirement in section


@pytest.mark.installed_plugin
@pytest.mark.parametrize(
    "skill_path",
    [pytest.param(path, id=path.parent.name) for path in sorted((PLUGIN_ROOT / "skills").glob("*/SKILL.md"))],
)
def test_each_skill_references_the_shared_helper_contract(skill_path: Path) -> None:
    """Require each packaged skill document to retain its closing helper contract."""
    text = skill_path.read_text(encoding="utf-8")

    assert "helper-cli-contract.md" in text, skill_path.parent.name


@pytest.mark.installed_plugin
def test_calibration_covers_plain_explanations_and_owned_freshness() -> None:
    """Keep vague checkout handoffs and technical-first communication in the behavioral probes."""
    payload = json.loads((PLUGIN_ROOT / "runtime/calibration/behavioral-cases.json").read_text(encoding="utf-8"))
    cases = {case["id"]: case for case in payload["cases"]}
    assert set(cases["workflow-plain-explanation-diagnostics"]["expected_findings"]) == {
        "technical-first-user-message",
        "generic-recovery-without-diagnosis",
        "empty-or-duplicate-report-sections",
        "historical-report-render-drift",
    }
    assert set(cases["code-review-agent-owned-fork-freshness"]["expected_findings"]) == {
        "fork-head-compared-before-fetch",
        "routine-source-preparation-delegated-to-user",
        "checkout-cause-invented",
        "unverified-upstream-pull",
    }


@pytest.mark.installed_plugin
def test_public_api_alone_does_not_force_high_risk() -> None:
    """Prevent ordinary compatibility review from reintroducing an automatic risk barrier."""
    skill = (PLUGIN_ROOT / "skills/code-review/SKILL.md").read_text(encoding="utf-8")
    classification = skill.split("Classify diff;", 1)[1].split("For `scope=pr`", 1)[0]
    high_risk = next(line for line in classification.splitlines() if line.startswith("- `HIGH_RISK`:"))
    assert "public API" not in high_risk
    assert "public API" in classification
    assert "compatibility" in classification


@pytest.mark.installed_plugin
def test_pauses_explain_cause_rule_and_recovery() -> None:
    """Reject a shared pause contract that strands users without a concrete recovery decision."""
    contract = (PLUGIN_ROOT / "shared/native-skill-contract.md").read_text(encoding="utf-8")
    assert "## Actionable Pauses" in contract
    section = contract.split("## Actionable Pauses", 1)[1].split("\n## ", 1)[0]
    for obligation in ("Stopped action", "Cause", "Rule", "Continue", "Next step", "Resume condition"):
        assert obligation in section
    assert "approval" in section.lower()
    assert "existing authorization" in section.lower()


@pytest.mark.installed_plugin
def test_calibration_covers_inspection_and_pause_boundaries() -> None:
    """Retain behavioral probes for false isolation, execution bypass, and dead-end pauses."""
    payload = json.loads((PLUGIN_ROOT / "runtime/calibration/behavioral-cases.json").read_text(encoding="utf-8"))
    cases = {case["id"]: case for case in payload["cases"]}
    assert set(cases["code-review-inspection-execution-separation"]["expected_findings"]) == {
        "public-api-automatically-high-risk",
        "inspection-blocked-by-missing-host-controls",
        "inspection-authorizes-repository-execution",
        "instruction-boundary-labelled-enforced-isolation",
        "serial-review-labelled-independent",
    }
    assert set(cases["workflow-actionable-pause"]["expected_findings"]) == {
        "pause-cause-or-rule-missing",
        "pause-recovery-owner-missing",
        "pause-resume-condition-missing",
        "pause-blocks-unaffected-work",
        "pause-repeats-existing-approval",
    }

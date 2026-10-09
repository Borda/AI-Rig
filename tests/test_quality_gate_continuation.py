"""Keep shipped review and output summaries aligned with authorized continuation."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGINS = ["cc_foundry", "cc_develop", "cc_oss", "cc_research"]


@pytest.mark.parametrize("plugin", PLUGINS)
def test_review_summary_keeps_feasible_remediation_and_any_score_decrease(plugin: str) -> None:
    """Prevent summary rules from stopping authorized fixes for labels or slow improvement."""
    text = (ROOT / "plugins" / plugin / "rules" / "quality-gates.md").read_text(encoding="utf-8")
    section = text.split("## Adversarial Convergence Loop", 1)[1].split("\n## ", 1)[0]
    assert "Resolve every feasible authorized finding" in section
    assert "a structural flag alone is not a stop" in section
    assert "a repeated signature requires shared-root-cause investigation before another fix" in section
    assert "Any score decrease converges (`0 < r_n < 1`)" in section
    assert "equal scores plateau" in section
    assert "Open `security` or `critical` findings forbid completion and commit" in section
    assert "unresolved `high` findings require escalation rather than deferral" in section
    assert "only for the concrete missing decision or authorization" in section
    assert "open structural finding" not in section
    assert "same open signature in consecutive reviews" not in section


@pytest.mark.parametrize(
    "relative",
    [
        "cc_foundry/rules/quality-gates.md",
        "cc_develop/rules/quality-gates.md",
        "cc_oss/rules/quality-gates.md",
        "cc_research/rules/quality-gates.md",
    ],
)
def test_output_length_cannot_force_redundant_completion_question(relative: str) -> None:
    """Let authorized next steps continue while retaining concrete permission decisions."""
    text = (ROOT / "plugins" / relative).read_text(encoding="utf-8")
    section = text.split("## Output Routing", 1)[1].split("\n## ", 1)[0]
    assert "only when a required decision or authorization is missing" in section
    assert "output length never triggers a question" in section
    assert "Continue an already-authorized next action in the same turn" in section
    assert "overrides generic skill completion prompts" in section
    assert "`NEVER SKIP` or `Always fires`" in section
    assert "Preserve concrete gates for a new fix scope, paid execution" in section
    assert "sensitive or destructive actions, remote changes" in section
    assert "existing authorization applies only within its granted scope" in section
    assert "when in doubt, invoke" not in section
    assert "**Follow-up gate** — invoke" not in section


def test_distill_trend_matches_canonical_any_decrease_contract() -> None:
    """Prevent executable extraction from calling a decreasing review score a plateau."""
    text = (ROOT / "plugins/cc_foundry/skills/distill/modes/executables.md").read_text(encoding="utf-8")
    assert "Any decrease (`0 < r_n < 1`) converges" in text
    assert "0.5 < r_n < 1.0" not in text
    assert "a structural flag alone does not stop remediation" in text
    assert "A clean loop does not authorize a commit" in text


def test_new_fix_scope_and_paid_execution_still_require_actual_user_choice() -> None:
    """Preserve real authorization gates while generic completion prompts become conditional."""
    audit = (ROOT / "plugins/cc_foundry/skills/audit/SKILL.md").read_text(encoding="utf-8")
    assert "When user picks fix option (a–c): run Steps 8–10" in audit
    assert "collect all NON_AUTO_FIXABLE decisions upfront" in audit
    calibrate = (ROOT / "plugins/cc_foundry/skills/calibrate/SKILL.md").read_text(encoding="utf-8")
    gate = calibrate.split("**Large fan-out gate**", 1)[1].split("On Abort:", 1)[0]
    assert "before any task creation or pipeline spawn" in gate
    assert "Call `AskUserQuestion`" in gate
    assert "label: `Proceed`" in gate
    assert "label: `Abort`" in gate
    loop = (ROOT / "plugins/cc_foundry/rules/_full/adversarial-loop.md").read_text(encoding="utf-8")
    assert "launch another paid/rejected route without required approval or changed evidence" in loop

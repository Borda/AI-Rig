"""Keep shipped review and output summaries aligned with authorized continuation."""

from pathlib import Path

import pytest
from _repo_phrases import missing_phrases

ROOT = Path(__file__).resolve().parents[2]
PLUGINS = ["cc_foundry", "cc_develop", "cc_oss", "cc_research"]


def _read(relative: str) -> str:
    """Return the text of a repository file given its path relative to the repository root."""
    return (ROOT / relative).read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """Return the body of the first ``## `` section whose heading starts with ``heading``, up to the next ``## ``.

    Examples:
        >>> _section("## A\\nbody a\\n## B\\nbody b\\n", "## A")
        'body a'
    """
    lines = text.splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith(heading))
    end = next((index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")), len(lines))
    return "\n".join(lines[start + 1 : end])


def _present(text: str, phrases: tuple[str, ...]) -> list[str]:
    """Return the phrases that ``text`` still contains, in the order given."""
    return [phrase for phrase in phrases if phrase in text]


@pytest.mark.parametrize("plugin", PLUGINS)
def test_review_loop_keeps_feasible_remediation_and_any_score_decrease(plugin: str) -> None:
    """The review-loop summary never stops authorized fixes for labels or slow improvement.

    Each plugin restates the Adversarial Convergence Loop in its own quality-gates rule. A summary that halts on a
    structural flag, or treats a shrinking score as a plateau, would block remediation the user already authorized.
    """
    section = _section(_read(f"plugins/{plugin}/rules/quality-gates.md"), "## Adversarial Convergence Loop")
    required = (
        "Resolve every feasible authorized finding",
        "a structural flag alone is not a stop",
        # recurrence is per invariant: a new route under a new signature but the same invariant evidence still repeats
        "a repeated invariant (same signature or same invariant evidence) requires shared-root-cause investigation "
        "before another fix",
        "Any score decrease converges (`0 < r_n < 1`)",
        "equal scores plateau",
        "Open `security` or `critical` findings forbid completion and commit",
        "unresolved `high` findings require escalation rather than deferral",
        "only for the concrete missing decision or authorization",
    )
    retired = ("open structural finding", "same open signature in consecutive reviews")

    assert (missing_phrases(section, required), _present(section, retired)) == ([], [])


@pytest.mark.parametrize("plugin", PLUGINS)
def test_output_routing_continues_authorized_steps_without_redundant_question(plugin: str) -> None:
    """Output length never forces a completion question, yet concrete permission decisions are retained.

    Each plugin's Output Routing section must let an authorized next step continue in the same turn while keeping the
    gates for new fix scope, paid execution, and remote or destructive actions.
    """
    section = _section(_read(f"plugins/{plugin}/rules/quality-gates.md"), "## Output Routing")
    required = (
        "only when a required decision or authorization is missing",
        "output length never triggers a question",
        "Continue an already-authorized next action in the same turn",
        "overrides generic skill completion prompts",
        "`NEVER SKIP` or `Always fires`",
        "Preserve concrete gates for a new fix scope, paid execution",
        "sensitive or destructive actions, remote changes",
        "existing authorization applies only within its granted scope",
    )
    retired = ("when in doubt, invoke", "**Follow-up gate** — invoke")

    assert (missing_phrases(section, required), _present(section, retired)) == ([], [])


def test_distill_trend_matches_any_decrease_contract() -> None:
    """The distill executables mode calls a decreasing review score converging, never a plateau.

    Executable extraction restates the trend rule from the canonical loop; a stale `0.5 < r_n < 1.0` band would let a
    shrinking score stall remediation.
    """
    text = _read("plugins/cc_foundry/skills/distill/modes/executables.md")
    required = (
        "Any decrease (`0 < r_n < 1`) converges",
        "a structural flag alone does not stop remediation",
        "A clean loop does not authorize a commit",
    )

    assert (missing_phrases(text, required), _present(text, ("0.5 < r_n < 1.0",))) == ([], [])


def test_new_fix_scope_and_paid_execution_still_require_user_choice() -> None:
    """Audit fixes and the calibrate fan-out keep their real user-choice gates while completion prompts go conditional.

    The generic follow-up question became conditional, so the places that must still ask (a new fix scope, a large paid
    fan-out, relaunching a rejected route) are pinned here.
    """
    audit = _read("plugins/cc_foundry/skills/audit/SKILL.md")
    calibrate = _read("plugins/cc_foundry/skills/calibrate/SKILL.md")
    gate = calibrate.split("**Large fan-out gate**", 1)[1].split("On Abort:", 1)[0]
    loop = _read("plugins/cc_foundry/rules/_full/adversarial-loop.md")

    assert [
        missing_phrases(
            audit,
            ("When user picks fix option (a–c): run Steps 8–10", "collect all NON_AUTO_FIXABLE decisions upfront"),
        ),
        missing_phrases(
            gate,
            (
                "before any task creation or pipeline spawn",
                "Call `AskUserQuestion`",
                "label: `Proceed`",
                "label: `Abort`",
            ),
        ),
        missing_phrases(loop, ("launch another paid/rejected route without required approval or changed evidence",)),
    ] == [[], [], []]

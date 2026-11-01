"""Keep decision context visible without hiding rich content in option previews.

Each test reads the communication and debugging rules as text and checks single-line phrases, so reflowing a paragraph
never breaks an assertion.
"""

from pathlib import Path

import pytest
from _markdown_text import found, missing

RULES = Path(__file__).resolve().parents[2] / "rules"
_COMMUNICATION = pytest.mark.parametrize("relative", ["communication.md", "_full/communication.md"])


@_COMMUNICATION
def test_question_summary_and_rich_content_delivery(relative: str) -> None:
    """Both rule layers require a short summary and a visible, durable copy of rich content.

    The stub and the full rule must agree, and neither may still tell the model to put rich content in every option's
    preview: the user reads the question first, so the content is printed once before it.
    """
    text = (RULES / relative).read_text(encoding="utf-8")
    required = [
        "6 plain lines",
        "target 600",
        "hard cap 1,500",
        "no table, pipe layout or fence",
        "counts, headline",
        "`→ <file path>`",
        "named file first",
        "printed once as last text before the call",
        "never table/tree in option `preview`",
        "one-line effect",
        "no preview by default",
        "distinct plain comparisons",
    ]
    retired = ["every option's `preview`", "`preview` of **every**", "`preview` of every single-select option"]
    assert missing(text, required) == []
    assert found(text, retired) == []


@_COMMUNICATION
def test_approval_questions_keep_scope_and_owner(relative: str) -> None:
    """Short-question guidance cannot shorten approval scope or take ownership of its wording.

    The approval questions are exempt from the six-line target and their wording lives in the git and GitHub rules.
    """
    text = (RULES / relative).read_text(encoding="utf-8")
    assert "`git-approve`, `git-push`, `gh-write` are exempt from the 6-line target and carry their scope" in text
    assert "`git-commit.md` / `public-github.md`" in text


def test_report_follow_ups_defer_full_content_until_final_reply() -> None:
    """The six report gates show a first-question summary and retain final-reply Stop delivery.

    The full header or findings are printed in the final reply after the answer, never before the question.
    """
    text = (RULES / "_full/communication.md").read_text(encoding="utf-8")
    gates = [
        f"`{gate}`"
        for gate in (
            "oss:review",
            "develop:review",
            "oss:analyse",
            "foundry:audit",
            "foundry:profile",
            "research:topic",
        )
    ]
    markers = [
        "first question's text",
        "`Title`",
        "`Outcome`/`Verdict`",
        "`summary.jsonl`",
        "Audit Report headline",
        "full header table",
        "final reply after the answer",
        "Stop still requires it there",
        "Do not print this content before the question or put it in option previews",
    ]
    assert missing(text, [*gates, *markers]) == []


def test_command_and_confidence_markers_keep_their_visible_fields() -> None:
    """Commands stay in question text; confidence keeps its axes and recommendation placement.

    The stub carries the placement rule and marker format, the full rule carries the tie-break and command placement.
    """
    stub = (RULES / "communication.md").read_text(encoding="utf-8")
    full = (RULES / "_full/communication.md").read_text(encoding="utf-8")
    stub_markers = [
        "recommended option **second**",
        "inside each option's own `description` field",
        "fit: N% · conf: N% ← recommended",
        "pure user-taste",
        "forced/single-path choices",
    ]
    full_markers = [
        "inside each option's own `description` field",
        "Near-tie on `fit` → higher `conf` breaks it",
        "pure user-taste",
        "A command the user must approve stays in the question text as an inline code span",
    ]
    assert missing(stub, stub_markers) == []
    assert missing(full, full_markers) == []


def test_debugging_escalation_delivers_attempts_before_question_with_summary() -> None:
    """Repeated-failure escalation preserves both attempts and the signature outside previews.

    The early-stop paragraph must print the attempts once, before the question, and keep the question text short.
    """
    text = (RULES / "_full/debugging.md").read_text(encoding="utf-8")
    paragraph = next(
        line for line in text.splitlines() if line.startswith("**Early-stop on repeated failure signature**")
    )
    required = [
        "both attempts and the unchanged signature",
        "named file first",
        "printed once as last text before the call",
        "question text",
        "counts, headline, `→ <file path>`",
        "never table/tree in option `preview`",
    ]
    assert missing(paragraph, required) == []
    assert "every option's `preview`" not in paragraph

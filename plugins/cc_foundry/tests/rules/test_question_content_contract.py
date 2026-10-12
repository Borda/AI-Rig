"""Keep decision context visible without hiding rich content in option previews."""

from pathlib import Path

import pytest

RULES = Path(__file__).resolve().parents[2] / "rules"


@pytest.mark.parametrize("relative", ["communication.md", "_full/communication.md"])
def test_question_summary_and_rich_content_delivery(relative: str) -> None:
    """Both rule layers require a short summary and a visible, durable copy of rich content."""
    text = (RULES / relative).read_text(encoding="utf-8")
    for marker in (
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
    ):
        assert marker in text, f"{relative} lacks {marker!r}"
    assert "every option's `preview`" not in text
    assert "`preview` of **every**" not in text
    assert "`preview` of every single-select option" not in text


@pytest.mark.parametrize("relative", ["communication.md", "_full/communication.md"])
def test_approval_questions_keep_scope_and_owner(relative: str) -> None:
    """Short-question guidance cannot shorten approval scope or take ownership of its wording."""
    text = (RULES / relative).read_text(encoding="utf-8")
    assert "`git-approve`, `git-push`, `gh-write` are exempt from the 6-line target and carry their scope" in text
    assert "`git-commit.md` / `public-github.md`" in text


def test_report_follow_ups_defer_full_content_until_final_reply() -> None:
    """The six report gates show a first-question summary and retain final-reply Stop delivery."""
    text = (RULES / "_full/communication.md").read_text(encoding="utf-8")
    for gate in ("oss:review", "develop:review", "oss:analyse", "foundry:audit", "foundry:profile", "research:topic"):
        assert f"`{gate}`" in text
    for marker in (
        "first question's text",
        "`Title`",
        "`Outcome`/`Verdict`",
        "`summary.jsonl`",
        "Audit Report headline",
        "full header table",
        "final reply after the answer",
        "Stop still requires it there",
        "Do not print this content before the question or put it in option previews",
    ):
        assert marker in text


def test_command_and_confidence_markers_keep_their_visible_fields() -> None:
    """Commands stay in question text; confidence keeps its axes and recommendation placement."""
    stub = (RULES / "communication.md").read_text(encoding="utf-8")
    full = (RULES / "_full/communication.md").read_text(encoding="utf-8")
    assert "recommended option **second**" in stub
    assert "inside each option's own `description` field" in stub
    assert "inside each option's own `description` field" in full
    assert "fit: N% · conf: N% ← recommended" in stub
    assert "Near-tie on `fit` → higher `conf` breaks it" in full
    assert "pure user-taste" in stub
    assert "pure user-taste" in full
    assert "forced/single-path choices" in stub
    assert "A command the user must approve stays in the question text as an inline code span" in full


def test_debugging_escalation_delivers_attempts_before_question_with_summary() -> None:
    """Repeated-failure escalation preserves both attempts and the signature outside previews."""
    text = (RULES / "_full/debugging.md").read_text(encoding="utf-8")
    paragraph = next(
        line for line in text.splitlines() if line.startswith("**Early-stop on repeated failure signature**")
    )
    assert "both attempts and the unchanged signature" in paragraph
    assert "named file first" in paragraph
    assert "printed once as last text before the call" in paragraph
    assert "question text" in paragraph
    assert "counts, headline, `→ <file path>`" in paragraph
    assert "never table/tree in option `preview`" in paragraph
    assert "every option's `preview`" not in paragraph

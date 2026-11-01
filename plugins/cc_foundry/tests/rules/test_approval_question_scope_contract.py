"""Pin the ask, decision context and approval scope of the three approval questions.

The Claude rules (``git-commit.md`` and ``public-github.md``, each in its stub and ``_full/`` form) and the Codex native
skill contract all describe the same approval questions. Each test reads the Markdown line by line, so reflowing a
paragraph never breaks an assertion.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from _markdown_text import missing

_PLUGINS = Path(__file__).resolve().parents[3]
_RULES = _PLUGINS / "cc_foundry" / "rules"

#: Fragments every approval question's instruction paragraph must carry.
_INSTRUCTION_FRAGMENTS = (
    "multi-line plain text",
    "at most 1,500 characters",
    "exempt from the 6-line target",
    "no table, no fence",
    "one blank line after the first line, which states the ask",
    "Option descriptions carry what each answer does, one line each",
)
_VARIANTS = pytest.mark.parametrize("variant", ["", "_full/"])
_QUESTION_CASES = [
    pytest.param(
        "git-commit.md",
        "git-approve",
        [
            "Commit these changes to {branch}?",
            "Subject: {subject}",
            "Diff: {file count}, {insertions} insertions, {deletions} deletions; files: {file names}",
            "Scope: one plain local commit; never push.",
        ],
        [
            "`Approve` = this commit only",
            "`Approve always` = the grant description required above, which must name "
            "`claude-git-approval.json` and its revocation command",
            "`Deny` = skip, leave the changes",
        ],
        id="local-commit",
    ),
    pytest.param(
        "git-commit.md",
        "git-push",
        [
            "Push {branch} to {configured remote name}?",
            "HEAD: {HEAD}",
            "Commits: {count}; subjects (up to 5): {subjects}",
            "Command: {exact command}",
            "Not covered: force, delete, tags, other refs, other remotes, URLs.",
        ],
        [
            "`Approve` = one push of this branch at this HEAD to this remote; token valid 15 minutes",
            "`Deny` = skip this push",
        ],
        id="push",
    ),
    pytest.param(
        "public-github.md",
        "gh-write",
        [
            "Run this write on {owner/repo}?",
            "Writes: {action, such as create, comment, edit or close; target}",
            "Body summary: {one-line summary}",
            "Command: `gh {exact arguments}`",
        ],
        [
            "`Approve` = run exactly this command once; token valid 15 minutes; read files re-hashed",
            "`Deny` = skip this write",
        ],
        id="github-write",
    ),
]


def _question(variant: str, rule: str, header: str) -> tuple[str, str, list[str]]:
    """Return a rule's full text, the instruction paragraph after its question marker, and the question's lines.

    The question block is the bullet list that follows the instruction paragraph; its bullet markers are removed.
    """
    text = (_RULES / f"{variant}{rule}").read_text(encoding="utf-8")
    marker = f"**{header} question text**"
    assert marker in text
    instructions, _, following = text.split(marker, 1)[1].partition("\n\n")
    block = following.split("\n\n", 1)[0].splitlines()
    assert all(line.startswith("- ") for line in block)
    return text, instructions, [line.removeprefix("- ") for line in block]


@_VARIANTS
@pytest.mark.parametrize(("rule", "header", "expected", "descriptions"), _QUESTION_CASES)
def test_approval_question_opens_with_ask_and_carries_scope(
    variant: str, rule: str, header: str, expected: list[str], descriptions: list[str]
) -> None:
    """Both rule variants open with the ask, keep scope in option descriptions and preserve rendering limits.

    The question reads as one sentence stating the ask followed by short fact lines; what each answer does lives in the
    one-line option descriptions instead of repeating in the question body.
    """
    _, instructions, lines = _question(variant, rule, header)
    question = "\n".join(lines)
    assert missing(instructions, [*_INSTRUCTION_FRAGMENTS, *descriptions]) == []
    assert lines == expected
    assert lines[0].endswith("?")
    assert len(question) <= 1500
    assert "|" not in question
    assert "```" not in question


@_VARIANTS
def test_github_write_question_has_one_command_span(variant: str) -> None:
    """The gh-write question carries exactly one inline code span, its command, and forbids invisible characters.

    A second code span would offer two commands to approve; the span is how the guard recognises what was approved.
    """
    text, instructions, lines = _question(variant, "public-github.md", "gh-write")
    assert re.findall(r"`([^`]+)`", "\n".join(lines)) == ["gh {exact arguments}"]
    assert "no invisible or control characters" in text
    assert "with no second command span" in instructions


@_VARIANTS
def test_push_question_names_the_current_branch(variant: str) -> None:
    """The git-push question puts the current branch, verbatim, in its first line.

    The user approves one specific branch push, so the branch must be visible in the ask itself.
    """
    _, instructions, lines = _question(variant, "git-commit.md", "git-push")
    assert "the current branch must appear verbatim in it" in instructions
    assert "{branch}" in lines[0]


@_VARIANTS
def test_local_commit_question_offers_a_named_grant(variant: str) -> None:
    """The local-commit question offers ``Approve always`` and names the grant file and its revocation command.

    A standing grant is only acceptable when the user can see where it lives and how to revoke it.
    """
    text, instructions, _ = _question(variant, "git-commit.md", "git-approve")
    assert "Approve always" in text
    assert (
        missing(
            instructions,
            ["claude-git-approval.json", "must name `claude-git-approval.json` and its revocation command"],
        )
        == []
    )


def test_codex_local_commit_question_keeps_host_specific_grant() -> None:
    """Codex opens with the same ask but keeps scope lines in the body because its disclosure precedes choices.

    The Codex contract differs on purpose from the Claude rules: scope sits in the question body and the grant file is
    ``codex-git-approval.json``, revocable with a shell command.
    """
    text = (_PLUGINS / "codex-rig" / "shared" / "native-skill-contract.md").read_text(encoding="utf-8")
    section = text.split("### Local Git approval grant\n", 1)[1].split("\n### ", 1)[0]
    assert (
        missing(
            section,
            [
                "**Local-commit question text**",
                "- Commit these changes to {branch}?",
                "one blank line after the first line, which states the ask",
                "Subject: {subject}",
                "Diff: {file count}, {insertions} insertions, {deletions} deletions; files: {file names}",
                "Scope: one plain local commit; never push.",
                "Approve = this commit only.",
                "every worktree of this project and in later sessions",
                'revoke with rm "$(git rev-parse --git-common-dir)/codex-git-approval.json".',
                "Deny = skip, leave the changes.",
                "at most 1,500 characters",
            ],
        )
        == []
    )

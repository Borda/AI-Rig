"""Pin the ask, decision context and approval scope of the three approval questions."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_PLUGINS = Path(__file__).resolve().parents[3]
_RULES = _PLUGINS / "cc_foundry" / "rules"


@pytest.mark.parametrize("variant", ["", "_full/"])
@pytest.mark.parametrize(
    ("rule", "header", "expected", "descriptions"),
    [
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
    ],
)
def test_approval_question_opens_with_ask_and_carries_scope(
    variant: str, rule: str, header: str, expected: list[str], descriptions: list[str]
) -> None:
    """Both rule variants open with the ask, keep scope in option descriptions and preserve rendering limits.

    The question reads as one sentence stating the ask followed by short fact lines; what each answer does lives in the
    one-line option descriptions instead of repeating in the question body.
    """
    text = (_RULES / f"{variant}{rule}").read_text(encoding="utf-8")
    marker = f"**{header} question text**"
    assert marker in text
    instructions, _, following = text.split(marker, 1)[1].partition("\n\n")
    assert "multi-line plain text" in instructions
    assert "at most 1,500 characters" in instructions
    assert "exempt from the 6-line target" in instructions
    assert "no table, no fence" in instructions
    assert "one blank line after the first line, which states the ask" in instructions
    assert "Option descriptions carry what each answer does, one line each" in instructions
    for description in descriptions:
        assert description in instructions
    block = following.split("\n\n", 1)[0]
    assert all(line.startswith("- ") for line in block.splitlines())
    lines = [line.removeprefix("- ") for line in block.splitlines()]
    assert lines == expected
    assert lines[0].endswith("?")
    question = "\n".join(lines)
    assert len(question) <= 1500
    assert "|" not in question
    assert "```" not in question
    if header == "gh-write":
        assert re.findall(r"`([^`]+)`", question) == ["gh {exact arguments}"]
        assert "no invisible or control characters" in text
        assert "with no second command span" in instructions
    if header == "git-push":
        assert "the current branch must appear verbatim in it" in instructions
        assert "{branch}" in lines[0]
    if header == "git-approve":
        assert "Approve always" in text
        assert "claude-git-approval.json" in instructions
        assert "must name `claude-git-approval.json` and its revocation command" in instructions


def test_codex_local_commit_question_keeps_host_specific_grant() -> None:
    """Codex opens with the same ask but keeps scope lines in the body because its disclosure precedes choices."""
    text = (_PLUGINS / "codex-rig" / "shared" / "native-skill-contract.md").read_text(encoding="utf-8")
    section = text.split("### Local Git approval grant\n", 1)[1].split("\n### ", 1)[0]
    assert "**Local-commit question text**" in section
    assert "- Commit these changes to {branch}?" in section
    assert "one blank line after the first line, which states the ask" in section
    assert "Subject: {subject}" in section
    assert "Diff: {file count}, {insertions} insertions, {deletions} deletions; files: {file names}" in section
    assert "Scope: one plain local commit; never push." in section
    assert "Approve = this commit only." in section
    assert "every worktree of this project and in later sessions" in section
    assert 'revoke with rm "$(git rev-parse --git-common-dir)/codex-git-approval.json".' in section
    assert "Deny = skip, leave the changes." in section
    assert "at most 1,500 characters" in section

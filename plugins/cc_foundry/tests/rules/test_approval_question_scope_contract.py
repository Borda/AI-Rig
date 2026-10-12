"""Pin the decision context and approval scope of the three approval questions."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_PLUGINS = Path(__file__).resolve().parents[3]
_RULES = _PLUGINS / "cc_foundry" / "rules"


@pytest.mark.parametrize("variant", ["", "_full/"])
@pytest.mark.parametrize(
    ("rule", "header", "expected"),
    [
        pytest.param(
            "git-commit.md",
            "git-approve",
            [
                "Branch: {branch}",
                "Operation: plain local commit",
                "Diff: {file count}, {insertions} insertions, {deletions} deletions; files: {file names}",
                "Draft subject: {subject}",
                "Approve = this commit only.",
                "Approve always = standing grant for verified, owned plain local commits in every worktree of this "
                "project and in later sessions; never push, force, history rewrite, discarding work, deleting unique "
                'history or auth/security changes; revoke with rm "$(git rev-parse --git-common-dir)/'
                'claude-git-approval.json".',
                "Deny = skip, leave the changes.",
            ],
            id="local-commit",
        ),
        pytest.param(
            "git-commit.md",
            "git-push",
            [
                "Branch: {branch}; HEAD: {HEAD}",
                "Target remote: {configured remote name}",
                "Commits: {count}; subjects (up to 5): {subjects}",
                "Command: {exact command}",
                "Approve = one push of this branch at this HEAD to this remote; token valid 15 minutes.",
                "Not covered: force, delete, tags, other refs, other remotes, URLs.",
                "Deny = skip this push.",
            ],
            id="push",
        ),
        pytest.param(
            "public-github.md",
            "gh-write",
            [
                "Target repo: {owner/repo}",
                "Writes: {action, such as create, comment, edit or close; target}",
                "Body summary: {one-line summary}",
                "Command: `gh {exact arguments}`",
                "Approve = run exactly this command once; token valid 15 minutes; files it reads are re-hashed.",
                "Anything else needs a new question.",
                "Deny = skip this write.",
            ],
            id="github-write",
        ),
    ],
)
def test_approval_question_lists_context_and_scope(variant: str, rule: str, header: str, expected: list[str]) -> None:
    """Both rule variants put the scope in the question and preserve its rendering constraints."""
    text = (_RULES / f"{variant}{rule}").read_text(encoding="utf-8")
    marker = f"**{header} question text**"
    assert marker in text
    instructions, _, following = text.split(marker, 1)[1].partition("\n\n")
    assert "multi-line plain text" in instructions
    assert "at most 1,500 characters" in instructions
    assert "exempt from the 6-line target" in instructions
    assert "no table, no fence" in instructions
    assert "Option descriptions repeat their scope in one line" in instructions
    block = following.split("\n\n", 1)[0]
    assert all(line.startswith("- ") for line in block.splitlines())
    lines = [line.removeprefix("- ") for line in block.splitlines()]
    assert lines == expected
    question = "\n".join(lines)
    assert len(question) <= 1500
    assert "|" not in question
    assert "```" not in question
    if header == "gh-write":
        assert re.findall(r"`([^`]+)`", question) == ["gh {exact arguments}"]
        assert "no invisible or control characters" in text
    if header == "git-push":
        assert "branch must appear verbatim" in instructions
    if header == "git-approve":
        assert "Approve always" in text
        assert "description" in instructions
        assert "claude-git-approval.json" in instructions


def test_codex_local_commit_question_keeps_host_specific_grant() -> None:
    """Codex uses the same local-commit context with its own record and native choices."""
    text = (_PLUGINS / "codex-rig" / "shared" / "native-skill-contract.md").read_text(encoding="utf-8")
    section = text.split("### Local Git approval grant\n", 1)[1].split("\n### ", 1)[0]
    assert "**Local-commit question text**" in section
    assert "Operation: plain local commit" in section
    assert "Diff: {file count}, {insertions} insertions, {deletions} deletions; files: {file names}" in section
    assert "Draft subject: {subject}" in section
    assert "Approve = this commit only." in section
    assert "every worktree of this project and in later sessions" in section
    assert 'revoke with rm "$(git rev-parse --git-common-dir)/codex-git-approval.json".' in section
    assert "Deny = skip, leave the changes." in section
    assert "at most 1,500 characters" in section

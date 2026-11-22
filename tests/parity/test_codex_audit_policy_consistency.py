"""Prevent audited cross-file Codex instructions from contradicting host contracts."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RIG = ROOT / "plugins" / "codex-rig"


@pytest.mark.parametrize("relative", ["shared/native-skill-contract.md", "README.md"])
def test_codex_remote_write_policy_limits_publication_to_worktree_gate(relative: str) -> None:
    """Keep the narrow PR push route and its own approvals independent of Claude's stored tokens."""
    text = (RIG / relative).read_text(encoding="utf-8")

    assert "Codex remote writes are prohibited outside the optional PR worktree publication gate" in text
    assert "Force operations are forbidden." in text
    assert "asks every time on Codex" not in text
    assert "every push asks the user" not in text
    assert (
        "GitHub writes ask the user and need explicit approval of the exact command every time, on both hosts"
        not in text
    )
    assert "claude-push-approval.json" in text


#: Wording that states where Codex may write to a remote. Scoped statements stay out on purpose: a profile or reader
#: that "does not authorize remote mutation", a commit grant whose scope says "never push", and the gate's own
#: helper doing "no ... other remote writes" are true whether or not the publication gate exists.
REMOTE_BOUNDARY = re.compile(
    r"never pushes|Remote updates remain human-owned|Remote boundary|remote writes are prohibited|remote write route"
    r"|forbidden remote|remote (?:mutation|publication)\b[^.]*\b(?:forbidden|prohibited)",
    re.IGNORECASE,
)


def _remote_boundary_lines(path: Path) -> list[str]:
    """Return every line of ``path`` that states the remote-write boundary."""
    return [line for line in path.read_text(encoding="utf-8").splitlines() if REMOTE_BOUNDARY.search(line)]


@pytest.mark.parametrize(
    "path",
    [
        pytest.param(RIG / "README.md", id="plugin-readme"),
        pytest.param(RIG / "DEVELOPMENT.md", id="development-guide"),
        pytest.param(ROOT / ".codex" / "README.md", id="codex-readme"),
        pytest.param(RIG / "shared" / "native-skill-contract.md", id="native-contract"),
        pytest.param(RIG / "assets" / "AGENTS.md", id="global-template"),
    ],
)
def test_remote_boundary_statements_name_the_publication_gate(path: Path) -> None:
    """A statement that remote updates stay with the human, or that Codex never pushes, names the one gated push."""
    lines = path.read_text(encoding="utf-8").splitlines()
    boundary = _remote_boundary_lines(path)
    assert boundary, "no remote-boundary statement found in this document: the check would pass on any text"
    assert not any("never pushes" in line for line in boundary)
    for line in boundary:
        assert "publication gate" in line, line
    table = [line for line in lines if "Authentication recovery, network approval" in line]
    for line in table:
        assert "publication push" in line, line


def test_skill_remote_boundary_statements_name_the_publication_gate() -> None:
    """Every skill document that states the remote-write boundary names the gate; most skill documents state none."""
    boundary = {
        f"{path.relative_to(RIG).as_posix()}: {line}"
        for path in sorted((RIG / "skills").rglob("*.md"))
        for line in _remote_boundary_lines(path)
    }
    assert boundary, "no skill states a remote-write boundary: the check would pass on any text"
    unnamed = sorted(entry for entry in boundary if "publication gate" not in entry or "never pushes" in entry)
    assert not unnamed, unnamed


def test_helper_documents_worker_receipts_without_certifying_subprocesses() -> None:
    """Stop obsolete guidance from denying the tested worker-source guarantee.

    The helper contract loads the owning PR contract before PR preparation. That contract states that per-worker
    receipts exist while unobserved test subprocesses stay uncertified, without treating xdist workers as inconclusive.
    """
    helper_text = (RIG / "shared" / "helper-cli-contract.md").read_text(encoding="utf-8")
    pr_reference = next(
        line for line in helper_text.splitlines() if "[PR Helper Contract](helper-pr-contract.md)" in line
    )
    assert "Before any PR review or PR remediation preparation, read and apply" in pr_reference
    assert "it binds as if inline here" in pr_reference

    text = (RIG / "shared" / "helper-pr-contract.md").read_text(encoding="utf-8")

    assert "per-worker receipts" in text
    assert "unobserved test subprocesses" in text
    assert "or xdist workers, which remain inconclusive" not in helper_text + text


def test_plugin_wait_instructions_preserve_host_boundaries() -> None:
    """Prevent Claude notification rules from overriding native Codex waits.

    The Codex plugin instructions keep their own ``Agent Waits`` section and never claim the Claude-only background
    spawn rule, while the Claude instructions keep the completion-notification wording.
    """
    codex = (ROOT / "plugins" / "AGENTS.md").read_text(encoding="utf-8")
    claude = (ROOT / "plugins" / "CLAUDE.md").read_text(encoding="utf-8")

    assert "Every agent spawn runs in the background" not in codex
    assert "Codex" in codex
    assert "Agent Waits" in codex
    assert "completion notification" in claude


def test_explicit_loop_has_no_implicit_three_round_cap() -> None:
    """Allow a still-decreasing fourth review without waiving explicit caller budgets.

    The specialist-orchestration contract must drop the fixed three-round cap in favour of a strictly decreasing score,
    while an explicit caller budget still bounds the loop.
    """
    text = (RIG / "shared" / "specialist-orchestration.md").read_text(encoding="utf-8")

    assert "up to three serial independent review rounds" not in text
    assert "strictly decreasing" in text
    assert "explicit caller" in text

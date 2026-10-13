"""Prevent audited cross-file Codex instructions from contradicting host contracts."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RIG = ROOT / "plugins" / "codex-rig"


@pytest.mark.parametrize("relative", ["shared/native-skill-contract.md", "README.md"])
def test_codex_remote_write_policy_has_no_approval_escape(relative: str) -> None:
    """Keep the Codex remote prohibition distinct from Claude's approval tokens."""
    text = (RIG / relative).read_text(encoding="utf-8")

    assert "Codex never performs remote mutations" in text
    assert "asks every time on Codex" not in text
    assert "every push asks the user" not in text
    assert (
        "GitHub writes ask the user and need explicit approval of the exact command every time, on both hosts"
        not in text
    )
    assert "claude-push-approval.json" in text


def test_helper_documents_worker_receipts_without_certifying_subprocesses() -> None:
    """Stop obsolete guidance from denying the tested worker-source guarantee."""
    text = (RIG / "shared" / "helper-cli-contract.md").read_text(encoding="utf-8")

    assert "per-worker receipts" in text
    assert "unobserved test subprocesses" in text
    assert "or xdist workers, which remain inconclusive" not in text


def test_plugin_wait_instructions_preserve_host_boundaries() -> None:
    """Prevent Claude notification rules from overriding native Codex waits."""
    codex = (ROOT / "plugins" / "AGENTS.md").read_text(encoding="utf-8")
    claude = (ROOT / "plugins" / "CLAUDE.md").read_text(encoding="utf-8")

    assert "Every agent spawn runs in the background" not in codex
    assert "Codex" in codex
    assert "Agent Waits" in codex
    assert "completion notification" in claude


def test_explicit_loop_has_no_implicit_three_round_cap() -> None:
    """Allow a still-decreasing fourth review without waiving explicit caller budgets."""
    text = (RIG / "shared" / "specialist-orchestration.md").read_text(encoding="utf-8")

    assert "up to three serial independent review rounds" not in text
    assert "strictly decreasing" in text
    assert "explicit caller" in text

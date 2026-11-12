"""Protect agent handoff formats and installable instruction references."""

from pathlib import Path

import pytest

_AGENTS = Path(__file__).resolve().parents[2] / "agents"


@pytest.mark.parametrize("agent", ["web-explorer", "perf-optimizer"])
def test_decline_preserves_required_file_and_envelope(agent: str) -> None:
    """An out-of-scope child must return the caller's required delivery format."""
    lines = (_AGENTS / f"{agent}.md").read_text(encoding="utf-8").splitlines()
    delivery = next(line for line in lines if "Decline delivery:" in line)
    assert '"status": "unsupported"' in delivery
    assert '"reason"' in delivery
    assert '"owner"' in delivery
    assert '"file"' in delivery
    assert "fixed closing line" in delivery
    assert "no Confidence block" in delivery
    assert not any("genuinely critical perf issue visible in out-of-scope" in line for line in lines)


@pytest.mark.parametrize("agent", ["doc-scribe", "perf-optimizer"])
def test_targeted_codemap_bindings_survive_tool_call_boundaries(agent: str) -> None:
    """A standalone targeted preflight must bind indexed names within its own Bash call."""
    lines = (_AGENTS / f"{agent}.md").read_text(encoding="utf-8").splitlines()
    binding = next(line for line in lines if "Before running the block" in line)
    assert "TARGET_MODULE" in binding
    assert "TARGET_FN" in binding
    assert "same Bash call" in binding
    assert "empty strings" in binding
    assert "unindexed" in binding


@pytest.mark.parametrize(
    "agent",
    [
        "creator",
        "curator",
        "doc-scribe",
        "linting-expert",
        "perf-optimizer",
        "qa-specialist",
        "solution-architect",
        "sw-engineer",
        "web-explorer",
    ],
)
def test_quality_gate_reference_matches_global_install(agent: str) -> None:
    """Agents must load the quality rule at the location where setup installs it."""
    lines = (_AGENTS / f"{agent}.md").read_text(encoding="utf-8").splitlines()
    assert any("~/.claude/rules/foundry-quality-gates.md" in line for line in lines)
    assert not any("`.claude/rules/foundry-quality-gates.md`" in line for line in lines)


def test_generic_configuration_is_not_redirected_to_python_lint_owner() -> None:
    """The implementation boundary must route prose and generic configuration by domain."""
    lines = (_AGENTS / "sw-engineer.md").read_text(encoding="utf-8").splitlines()
    redirect = next(line for line in lines if "Analysing non-Python inputs" in line)
    assert "Python lint/type configuration" in redirect
    assert "Markdown" in redirect
    assert "foundry:doc-scribe" in redirect
    assert "domain owner" in redirect


def test_pytorch_tracking_load_names_an_executable_read() -> None:
    """The conditional ecosystem guide must be loaded through a concrete Bash read."""
    lines = (_AGENTS / "web-explorer.md").read_text(encoding="utf-8").splitlines()
    loading = next(line for line in lines if "pytorch-tracking.md" in line)
    assert 'cat "${CLAUDE_PLUGIN_ROOT}/skills/_shared/pytorch-tracking.md"' in loading
    assert "Bash" in loading
    assert "CI-maintainer mode only" in loading

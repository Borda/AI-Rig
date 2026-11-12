"""Exercise untrusted MCP task forwarding and the bridge's caller recovery instructions."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / "bin"))

import bridge_call  # noqa: E402
import bridge_mcp  # noqa: E402


@pytest.mark.parametrize("verb", ["advise", "review", "implement"])
@pytest.mark.parametrize("untrusted", [True, False])
def test_mcp_forwards_the_untrusted_task_designation(tmp_path: Path, verb: str, untrusted: bool) -> None:
    """The MCP request reaches the existing peer prompt fence without changing trusted tasks."""
    (tmp_path / ".git").mkdir()
    request = bridge_mcp._request_from_arguments(
        verb, {"task": "External source text", "untrusted_task": untrusted}, tmp_path
    )
    prompt = bridge_call._prompt_with_budget(request, host="claude")
    assert request.untrusted_task is untrusted
    assert ("<untrusted-" in prompt) is untrusted
    assert "External source text" in prompt


def test_mcp_omitted_untrusted_designation_keeps_the_trusted_default(tmp_path: Path) -> None:
    """Existing MCP clients retain the Request default when they omit the new argument."""
    request = bridge_mcp._request_from_arguments("advise", {"task": "Authored request"}, tmp_path)
    assert request.untrusted_task is False
    assert "<untrusted-" not in bridge_call._prompt_with_budget(request, host="claude")


@pytest.mark.parametrize("value", [None, 0, 1, "true", pytest.param([], id="array")])
def test_mcp_refuses_nonboolean_untrusted_designations(tmp_path: Path, value: object) -> None:
    """Values such as the string false cannot accidentally mark a task trusted or untrusted."""
    with pytest.raises(ValueError, match="untrusted_task must be a boolean"):
        bridge_mcp._request_from_arguments("advise", {"task": "External text", "untrusted_task": value}, tmp_path)


@pytest.mark.parametrize("verb", ["advise", "review", "implement"])
def test_mcp_advertises_an_optional_strict_boolean(verb: str) -> None:
    """Tool discovery accepts the designation while retaining rejection of unknown fields."""
    tool = next(item for item in bridge_mcp.tool_definitions() if item["name"] == f"bridge_{verb}")
    schema = tool["inputSchema"]
    assert schema["properties"]["untrusted_task"] == {"type": "boolean"}
    assert "untrusted_task" not in schema["required"]
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize("host", ["claude", "codex"])
@pytest.mark.parametrize("verb", ["advise", "review", "implement"])
def test_skills_designate_all_external_task_sources(host: str, verb: str) -> None:
    """External content must use the data fence regardless of its task transport."""
    lines = (PLUGIN / f"{host}-skills" / verb / "SKILL.md").read_text(encoding="utf-8").splitlines()
    instruction = next(line for line in lines if "whenever the task text" in line)
    assert "PR comments, issue bodies, web pages or any other external source" in instruction
    assert "scratch file or `--task-file`" in instruction
    assert ("`--untrusted-task`" if host == "claude" else "`untrusted_task=true`") in instruction


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_implement_requires_peer_exit_after_a_stalled_supervisor(host: str) -> None:
    """A stalled supervisor alone cannot release the caller's workspace edit exclusion."""
    lines = (PLUGIN / f"{host}-skills/implement/SKILL.md").read_text(encoding="utf-8").splitlines()
    instruction = next(line for line in lines if "After a `stalled` status" in line)
    assert "edit the workspace only after" in instruction
    assert "peer is confirmed terminated" in instruction
    assert "terminal peer status or confirmed cancellation" in instruction
    assert "original hard cutoff has passed" in instruction
    assert "`stalled` and `cancel_requested` do not confirm peer termination" in instruction
    assert "a dead supervisor cannot enforce the hard cutoff" in instruction


def test_setup_documents_the_pty_authentication_limit() -> None:
    """Setup's terminal prerequisite must not be presented as proof of human approval."""
    source = " ".join((PLUGIN / "bin/bridge_setup.py").read_text(encoding="utf-8").split())
    assert "not human authentication: a model-controlled process can allocate a PTY" in source
    assert "Run this helper outside model-controlled tools; the host must enforce that boundary." in source

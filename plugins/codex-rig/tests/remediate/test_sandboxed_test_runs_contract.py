"""Regression checks for sandbox-safe pytest runs and the narrow reusable pytest approval."""

from __future__ import annotations

from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
NATIVE_CONTRACT = PLUGIN_ROOT / "shared" / "native-skill-contract.md"
CODE_REMEDIATE_SKILL = PLUGIN_ROOT / "skills" / "code-remediate" / "SKILL.md"
SECTION_LINK = "native-skill-contract.md#sandboxed-test-runs"


def _section(path: Path, start: str, end: str) -> str:
    """Return one Markdown span with wrapping collapsed so assertions survive reflow."""
    text = path.read_text(encoding="utf-8").split(start, maxsplit=1)[1].split(end, maxsplit=1)[0]
    return " ".join(text.split())


def test_targeted_runs_disable_xdist_only_when_parallelism_is_absent() -> None:
    """Keep targeted loop runs inside the sandbox without changing parallel or xdist-dependent runs.

    A merely installed xdist makes pytest-rerunfailures open a localhost socket that the sandbox blocks; the flag is
    safe only when neither the command nor any pytest configuration source requests parallelism.
    """
    section = _section(NATIVE_CONTRACT, "## Sandboxed Test Runs", "## Actionable Pauses")
    for required in (
        "outside the canonical `run_gates.py` test gate",
        "add `-p no:xdist` to a targeted pytest command that needs no parallelism",
        "even with `-n 0`",
        "The command has no `-n`, `--numprocesses`, `--dist`, or `-p xdist` option.",
        "`pyproject.toml` `[tool.pytest.ini_options]`, `pytest.ini`, `setup.cfg` `[tool:pytest]`, `tox.ini` `[pytest]`",
        "`PYTEST_ADDOPTS`",
        "the `worker_id` fixture",
        "Add the flag only when every condition below holds; otherwise keep the command unchanged:",
    ):
        assert required in section


def test_reusable_pytest_approval_is_narrow_upfront_and_disclosed() -> None:
    """Allow one pinned test-runner prefix only with an explicit trigger, timing, and unsandboxed disclosure.

    The exception must never widen into a bare interpreter or gate-runner prefix, and a denial of this optional approval
    must not stop the workflow or prompt again.
    """
    section = _section(NATIVE_CONTRACT, "## Sandboxed Test Runs", "## Actionable Pauses")
    for required in (
        "verified required capability unavailable under the active sandbox",
        "Never request it when only sandbox-safe targeted runs are needed.",
        "immediately after the selected scope is accepted and before the first edit",
        "Never repeat it in the same session after a grant or denial.",
        "set `prefix_rule` to exactly the repository's pinned test-runner prefix",
        '`["<repo>/.venv/bin/python", "-m", "pytest"]`',
        '`["uv", "run", "--no-sync", "pytest"]`',
        "Never use a bare interpreter, `python -c`",
        "or the `run_gates.py` gate runner",
        "start every escalated pytest command with exactly that prefix, in one form for the whole session",
        "a command beginning with `env VAR=value`, a shell wrapper, or a different runner spelling",
        "through the execution tool's environment field",
        "never widen the prefix to fit the wrapper",
        "approved pytest commands run outside the sandbox for the rest of this session",
        "repository `conftest.py`, plugin, and test code executes unsandboxed",
        "may still need its own one-time approval",
        "this approval is optional",
        "`environment-blocked`",
        "request a one-time escalation for the complete `run_gates.py` command and omit `prefix_rule`",
    ):
        assert required in section
    networked = _section(NATIVE_CONTRACT, "## Networked CLI Approval", "## GitHub Read Execution")
    assert "request broad interpreter prefix" in networked
    assert "The only interpreter-family reusable prefix permitted is the pinned test-runner prefix" in networked


def test_code_remediate_applies_sandboxed_test_runs_at_each_checkpoint() -> None:
    """Request the pytest approval with the scope answer, sandbox loop runs, and leave final gates unchanged."""
    scope = _section(CODE_REMEDIATE_SKILL, "### 05: Ask For Resolution Scope Before Editing", "### 06:")
    fixes = _section(CODE_REMEDIATE_SKILL, "### 07: Apply Fixes In Selected Scope", "### 08:")
    gates = _section(CODE_REMEDIATE_SKILL, "### 09: Run Shared Quality Gates", "### 10:")

    assert SECTION_LINK in scope
    assert (
        "request the single reusable pinned test-runner approval when its trigger holds, otherwise request none"
        in scope
    )
    assert "A denial does not stop remediation." in scope
    assert SECTION_LINK in fixes
    assert "add `-p no:xdist` when its conditions hold" in fixes
    assert "Keep the repository's configured test command unchanged" in gates
    assert "complete `run_gates.py` command without `prefix_rule`" in gates


@pytest.mark.parametrize(
    "relative",
    ["skills/implement/SKILL.md", "skills/investigate/SKILL.md", "skills/challenge-resolve/SKILL.md"],
)
def test_loop_skills_reference_sandboxed_test_runs(relative: str) -> None:
    """Keep every skill with a direct pytest loop on the shared sandbox-safe run contract."""
    assert SECTION_LINK in (PLUGIN_ROOT / relative).read_text(encoding="utf-8")


def test_agent_contract_mirrors_sandboxed_test_runs() -> None:
    """Keep the shipped agent instructions aligned with the pinned-prefix exception and commit command."""
    agents = " ".join((PLUGIN_ROOT / "assets" / "AGENTS.md").read_text(encoding="utf-8").split())
    assert "§Sandboxed Test Runs" in agents
    assert "add `-p no:xdist`" in agents
    assert "never bare interpreter, `python -c`, or gate runner" in agents
    assert "`git add -- <paths> && git commit --cleanup=verbatim -m <message>`" in agents


def test_local_test_authorization_does_not_invent_a_sandbox_restriction() -> None:
    """Require observed restrictions, avoiding speculative approval and duplicate wrapper runs."""
    section = _section(NATIVE_CONTRACT, "## Sandboxed Test Runs", "## Actionable Pauses")
    assert "verified required capability unavailable under the active sandbox" in section
    assert "Installed plugins alone do not establish a sandbox restriction." in section
    assert "reuse valid unchanged canonical evidence" in section
    assert "avoid a redundant wrapper run for a lightweight path" in section

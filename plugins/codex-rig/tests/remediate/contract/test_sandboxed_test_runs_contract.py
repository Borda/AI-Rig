"""Contract text for sandbox-safe pytest runs and the narrow reusable pytest approval.

The checks read the shipped Markdown line by line (heading-anchored spans, whitespace collapsed), so reflowing a
paragraph never breaks an assertion.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _contract_text import absent_phrases
from _skill_text import read_with_references

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
NATIVE_CONTRACT = PLUGIN_ROOT / "shared" / "native-skill-contract.md"
CODE_REMEDIATE_SKILL = PLUGIN_ROOT / "skills" / "code-remediate" / "SKILL.md"
AGENTS = PLUGIN_ROOT / "assets" / "AGENTS.md"
SECTION_LINK = "native-skill-contract.md#sandboxed-test-runs"


def _collapse(text: str) -> str:
    """Join wrapped lines into single-spaced prose so phrase checks ignore line breaks."""
    return " ".join(text.split())


def _span(text: str, start: str, end: str) -> str:
    """Return the collapsed lines after the first ``start`` heading line up to the next ``end`` heading line."""
    lines = text.splitlines()
    first = next((index for index, line in enumerate(lines) if line.startswith(start)), None)
    assert first is not None, f"heading {start!r} not found"
    last = next((index for index in range(first + 1, len(lines)) if lines[index].startswith(end)), None)
    assert last is not None, f"heading {end!r} not found after {start!r}"
    return _collapse("\n".join(lines[first + 1 : last]))


@pytest.fixture(scope="module")
def sandbox_section() -> str:
    """Return the native contract's Sandboxed Test Runs section."""
    return _span(NATIVE_CONTRACT.read_text(encoding="utf-8"), "## Sandboxed Test Runs", "## Actionable Pauses")


@pytest.fixture(scope="module")
def remediate_skill() -> str:
    """Return the code-remediate SKILL.md joined with its trigger-loaded reference files."""
    return read_with_references(CODE_REMEDIATE_SKILL, "intake.md", "selection.md", "implementation.md", "completion.md")


class TestSandboxedTestRunsContract:
    """The Sandboxed Test Runs contract and the skills and agent instructions that point at it."""

    def test_targeted_runs_disable_xdist_only_without_parallelism(self, sandbox_section: str):
        """Targeted loop runs add `-p no:xdist` only when neither the command nor pytest configuration asks for xdist.

        A merely installed xdist makes pytest-rerunfailures open a localhost socket that the sandbox blocks; the flag is
        safe only when nothing requests parallelism, so parallel and xdist-dependent runs must stay unchanged.
        """
        assert not absent_phrases(
            sandbox_section,
            "outside the canonical `run_gates.py` test gate",
            "add `-p no:xdist` to a targeted pytest command that needs no parallelism",
            "even with `-n 0`",
            "The command has no `-n`, `--numprocesses`, `--dist`, or `-p xdist` option.",
            "`pyproject.toml` `[tool.pytest.ini_options]`, `pytest.ini`, `setup.cfg` `[tool:pytest]`, `tox.ini` `[pytest]`",
            "`PYTEST_ADDOPTS`",
            "the `worker_id` fixture",
            "Add the flag only when every condition below holds; otherwise keep the command unchanged:",
        )

    def test_reusable_approval_is_narrow_upfront_and_disclosed(self, sandbox_section: str):
        """One pinned test-runner prefix is requested only with an explicit trigger, timing, and unsandboxed disclosure.

        The exception must never widen into a bare interpreter or gate-runner prefix, and a denial of this optional
        approval must neither stop the workflow nor prompt again.
        """
        assert not absent_phrases(
            sandbox_section,
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
        )

    def test_networked_approval_permits_only_pinned_runner_prefix(self):
        """The networked-CLI approval section refuses broad interpreter prefixes and allows only audited ones.

        The reusable pytest prefix must be the single interpreter-family exception, next to the audited GitHub helper
        prefixes, so the pytest approval cannot become a general interpreter grant.
        """
        networked = _span(NATIVE_CONTRACT.read_text(encoding="utf-8"), "## Networked CLI Approval", "## GitHub Read")

        assert not absent_phrases(
            networked,
            "request broad interpreter prefix",
            "Permitted interpreter-family reusable prefixes are the pinned test-runner prefix",
            "and the audited helper prefixes in [Reusable GitHub approval](#reusable-github-approval)",
        )

    def test_code_remediate_applies_contract_at_each_checkpoint(self, remediate_skill: str):
        """Code-remediate requests the pytest approval with the scope answer and leaves the final gates unchanged.

        The scope step asks for the approval, the fix step runs sandbox loop runs, and the shared quality gates keep the
        repository's configured test command, so the exception never reaches `run_gates.py`.
        """
        scope = _span(remediate_skill, "### 05: Ask For Resolution Scope Before Editing", "### 06:")
        fixes = _span(remediate_skill, "### 07: Apply Fixes In Selected Scope", "### 08:")
        gates = _span(remediate_skill, "### 09: Run Shared Quality Gates", "### 10:")

        assert not (
            absent_phrases(
                scope,
                SECTION_LINK,
                "request the single reusable pinned test-runner approval when its trigger holds, otherwise request none",
                "A denial does not stop remediation.",
            )
            + absent_phrases(fixes, SECTION_LINK, "add `-p no:xdist` when its conditions hold")
            + absent_phrases(
                gates,
                "Keep the repository's configured test command unchanged",
                "complete `run_gates.py` command without `prefix_rule`",
            )
        )

    @pytest.mark.parametrize("relative", ["skills/implement", "skills/investigate", "skills/challenge-resolve"])
    def test_loop_skill_references_contract(self, relative: str):
        """Every skill with a direct pytest loop links the shared sandbox-safe run contract.

        Skipping the link would let one loop skill run pytest without the xdist guard the other loop skills apply.
        """
        skill = (PLUGIN_ROOT / relative / "SKILL.md").read_text(encoding="utf-8")

        assert SECTION_LINK in skill

    def test_agent_instructions_mirror_contract(self):
        """The shipped agent instructions mirror the pinned-prefix exception and the commit command.

        AGENTS.md is the Codex-side copy of the policy; if it drifts from the native contract the agent follows a stale
        rule even while the skills are correct.
        """
        agents = _collapse(AGENTS.read_text(encoding="utf-8"))

        assert not absent_phrases(
            agents,
            "§Sandboxed Test Runs",
            "add `-p no:xdist`",
            "never bare interpreter, `python -c`, or gate runner",
            "`git add -- <paths> && git commit --cleanup=verbatim -m <message>`",
        )

    def test_local_test_authorization_requires_observed_restriction(self, sandbox_section: str):
        """Approval is requested only for an observed sandbox restriction, never speculatively.

        Installed plugins alone do not prove a restriction, and valid canonical evidence is reused instead of repeating
        a wrapper run for a lightweight path.
        """
        assert not absent_phrases(
            sandbox_section,
            "verified required capability unavailable under the active sandbox",
            "Installed plugins alone do not establish a sandbox restriction.",
            "reuse valid unchanged canonical evidence",
            "avoid a redundant wrapper run for a lightweight path",
        )

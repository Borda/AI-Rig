"""Preserve executable helper recipes and reachable specialist branches in shipped workflows."""

from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[2]


def test_research_dispatch_is_independent_of_implementation_mapping() -> None:
    """Paper-only research reaches its planned specialist wave before synthesis."""
    lines = (PLUGIN_ROOT / "skills/research/SKILL.md").read_text(encoding="utf-8").splitlines()
    mapping = next(index for index, line in enumerate(lines) if line.startswith("### 04:"))
    dispatch = lines.index("### 04b: Dispatch planned specialists")
    synthesis = next(index for index, line in enumerate(lines) if line.startswith("### 05:"))
    action = next(line for line in lines[dispatch:synthesis] if line.startswith("When step 03 planned specialists"))

    assert mapping < dispatch < synthesis
    assert "even when step 04 is inapplicable" in action
    assert "join all handoffs before writing `research.md`" in action
    assert any("dispatch is step 04b" in line for line in lines[:mapping])


@pytest.mark.parametrize("flag", ["--changelog", "--summary", "--migration"])
def test_prepare_rejects_notes_only_artifact_flags(flag: str) -> None:
    """Prepare cannot silently reinterpret an artifact flag documented as notes-only."""
    lines = (PLUGIN_ROOT / "skills/release/SKILL.md").read_text(encoding="utf-8").splitlines()
    rule = next(line for line in lines if line.startswith("Normalize `base->head`"))

    assert "other artifact flags apply to notes only" in rule
    assert "Reject" in rule
    assert f"`prepare {flag}`" in rule
    assert "as incompatible; prepare already includes those artifacts" in rule


def test_ambiguous_notes_impact_has_a_specialist_plan_route() -> None:
    """Notes whose release impact is ambiguous use the same bounded plan route as readiness modes."""
    lines = (PLUGIN_ROOT / "skills/release/SKILL.md").read_text(encoding="utf-8").splitlines()
    route = next(line for line in lines if line.startswith("For `prepare`/`audit`"))

    assert "or `notes` when SemVer/migration impact is ambiguous" in route
    assert "read and apply `../../shared/specialist-orchestration.md` only for" in route
    assert "specialist-release-plan.md" in route


def test_sync_instruction_health_uses_the_installed_python_launcher() -> None:
    """The instruction-health check cannot bypass the selected package's runtime validation."""
    lines = (PLUGIN_ROOT / "skills/sync/SKILL.md").read_text(encoding="utf-8").splitlines()
    health = next(line for line in lines if line.startswith("At explicit check"))

    assert "`PLUGIN_ROOT/bin/python PLUGIN_ROOT/scripts/install_global_agents.py --check" in health
    assert "`PLUGIN_ROOT` and `<installed-package>` are the selected cache path from step 02" in health
    assert "On native Windows use `PLUGIN_ROOT/bin/python.cmd`" in health


def test_sync_profile_recipe_runs_as_one_command_in_each_shell() -> None:
    """Profile setup uses quoted launcher paths and a native PowerShell invocation without Bash continuations."""
    lines = (PLUGIN_ROOT / "skills/sync/SKILL.md").read_text(encoding="utf-8").splitlines()
    start = lines.index(
        "After successful managed-plugin installation, resolve active installed Codex Rig cache root "
        "and invoke the packaged profile helper:"
    )
    bash = lines.index("```bash", start)
    powershell = lines.index("```powershell", start)
    command = lines[powershell + 1]

    assert lines[bash + 1].startswith('"<installed-codex-rig-cache-root>/bin/python" ')
    assert lines[bash + 2] == "```"
    assert command.startswith('& "<installed-codex-rig-cache-root>/bin/python.cmd" ')
    assert '"<installed-codex-rig-cache-root>/scripts/install_github_read_rules.py"' in command
    assert '--plugin-root "<installed-codex-rig-cache-root>" --codex-home "<CODEX_HOME>"' in command
    assert not command.endswith("\\")
    assert lines[powershell + 2] == "```"

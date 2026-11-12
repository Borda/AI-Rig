"""Keep extracted rule obligations binding before their original activity begins."""

from pathlib import Path

import pytest

RULES = Path(__file__).resolve().parents[2] / "rules"

_DETAIL_CASES = [
    pytest.param("agent-spawn.md", "Spawn slots — three fields, no repeats", "Pre-spawn check", id="spawn-labels"),
    pytest.param("agent-spawn.md", "Parallel Spawn Ceilings — by Model Tier", "effective", id="spawn-pools"),
    pytest.param(
        "claude-config.md", "Turn Batching — one response, every independent call", "221,527 tok", id="turn-batching"
    ),
    pytest.param("claude-config.md", "Directory Navigation Commands", "cd /path &&", id="navigation-examples"),
    pytest.param("claude-config.md", "Agent/Skill Spawn Discipline", "~73", id="spawn-discipline"),
    pytest.param("communication.md", "Re: Anchor", "╔", id="reply-template"),
    pytest.param("communication.md", "Execution Failure Signaling", "**! BLOCKED", id="failure-template"),
    pytest.param("python-code.md", "Docstring Style", "_FRAME_LAYOUT_HINTS", id="docstring-example"),
    pytest.param("python-code.md", "Library API Awareness", "__version__", id="api-probe"),
    pytest.param(
        "python-code.md", "Multi-OS Executables — a POSIX Assumption is a Defect", "PureWindowsPath", id="portable-code"
    ),
    pytest.param("python-code.md", "Complexity Thresholds", "PLR0913", id="complexity-table"),
    pytest.param("python-testing.md", "Test Selection — Markers", "pytestmark", id="test-markers"),
    pytest.param("python-testing.md", "Helpers in Tests", "self-contained", id="test-helpers"),
    pytest.param("python-testing.md", "Doctests", "--doctest-modules", id="doctest-command"),
    pytest.param("python-testing.md", "Test-Softening Anti-patterns", "raises=", id="test-softening"),
    pytest.param("python-testing.md", "Mechanical Enforcement — Ruff", "D100", id="test-enforcement"),
    pytest.param("quality-gates.md", "Output Routing", "new file — never overwrite", id="output-protocol"),
    pytest.param("quality-gates.md", "Report File Format", "Universal terminal-print rule", id="report-protocol"),
]


def _section(path: Path, heading: str) -> list[str]:
    """Read one rule section without matching across prose line breaks."""
    lines = path.read_text(encoding="utf-8").splitlines()
    start = lines.index(f"## {heading}")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return lines[start + 1 : end]


@pytest.mark.parametrize(("name", "heading", "obligation"), _DETAIL_CASES)
def test_extracted_section_loads_before_activity(name: str, heading: str, obligation: str) -> None:
    """Every moved contract has an unconditional before-activity load and its original operative detail."""
    stub = _section(RULES / name, heading)
    full = (RULES / "_full" / name).read_text(encoding="utf-8").splitlines()

    assert any(f"_full/{name}" in line and "Before" in line and "binding" in line for line in stub)
    assert any("cannot be read, stop" in line for line in stub)
    assert any(obligation in line for line in full)


@pytest.mark.parametrize("name", ["agent-spawn.md", "claude-config.md", "communication.md", "quality-gates.md"])
def test_rule_detail_recipe_reads_payload(name: str) -> None:
    """Resolving an extracted file is followed by reading it rather than only assigning its pathname."""
    lines = (RULES / name).read_text(encoding="utf-8").splitlines()

    assert any('cat "$RULE_FULL"' in line for line in lines)


@pytest.mark.parametrize("name", ["agent-spawn.md", "claude-config.md", "communication.md", "quality-gates.md"])
def test_rule_detail_uses_active_install_provenance(name: str) -> None:
    """Mandatory detail loading follows the selected install instead of a more recently touched cache copy."""
    lines = (RULES / name).read_text(encoding="utf-8").splitlines()

    assert any(
        "resolve_plugin_root.py --plugin-name foundry" in line and "registry preferred" in line for line in lines
    )
    assert any("Never select a different installed version" in line and "mtime" in line for line in lines)
    assert any("repository maintenance only" in line and "source-tree" in line for line in lines)
    assert not any("ls -td" in line for line in lines)
    assert any(f'RULE_FULL="$FOUNDRY_ROOT/rules/_full/{name}"' in line for line in lines)


@pytest.mark.parametrize(
    ("name", "target", "heading"),
    [
        pytest.param("python-testing.md", "python-testing.md", "Uncollected Tests", id="uncollected-parent-section"),
        pytest.param(
            "python-testing.md", "python-testing.md", "Behaviour vs Bookkeeping Checks", id="bookkeeping-parent-section"
        ),
        pytest.param("communication.md", "communication.md", "Reply Visibility", id="visibility-parent-section"),
    ],
)
def test_detail_cross_reference_names_its_parent_rule(name: str, target: str, heading: str) -> None:
    """A moved shorthand section reference explicitly identifies the parent file that still owns it."""
    lines = (RULES / "_full" / name).read_text(encoding="utf-8").splitlines()
    parent = (RULES / target).read_text(encoding="utf-8").splitlines()

    assert any(f"`{target}` §{heading}" in line for line in lines)
    assert f"## {heading}" in parent

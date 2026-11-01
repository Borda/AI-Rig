"""Guard the compaction boundary and Check 27's optional-provider exception.

The shared compaction contract carries one bash gate that resolves the foundry provider through a sibling resolver. The
audit's Check 27 flags sibling reach-ins, but must let this one guarded shape through; both documents are read live and
parsed by heading and fence lines, so reflowing their prose cannot break these tests.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from _hook_env import _bash_runs_posix_script

PLUGIN = Path(__file__).resolve().parents[2]
_FENCE = "```"

_skip_check27_tools_unavailable = pytest.mark.skipif(
    not _bash_runs_posix_script() or any(shutil.which(tool) is None for tool in ("grep", "awk")),
    reason="Check 27's executable audit requires Bash executing POSIX scripts, grep and awk",
)


def _bash_blocks(lines: list[str]) -> list[str]:
    """Return the body of every ``bash`` fenced block found in ``lines``."""
    blocks: list[str] = []
    body: list[str] | None = None
    for line in lines:
        if body is None and line.startswith(f"{_FENCE}bash"):
            body = []
        elif body is not None and line.startswith(_FENCE):
            blocks.append("\n".join(body))
            body = None
        elif body is not None:
            body.append(line)
    return blocks


def _heading_body(text: str, heading: str) -> list[str]:
    """Return the lines between the ``## `` heading starting with ``heading`` and the next ``## `` heading."""
    lines = text.splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith(heading)) + 1
    end = next((index for index in range(start, len(lines)) if lines[index].startswith("## ")), len(lines))
    return lines[start:end]


def _boundary_gate() -> str:
    """Return the single bash block of the shared compaction contract's Boundary step."""
    contract = (PLUGIN / "skills" / "_shared" / "compaction-contract.md").read_text(encoding="utf-8")
    (gate,) = _bash_blocks(_heading_body(contract, "## Boundary step"))
    return gate


def _check27_sibling_reach_in_command() -> str:
    """Return the bash command Check 27 Step 2 runs to find sibling reach-ins."""
    checks = (PLUGIN / "skills" / "audit" / "templates" / "checks-skills-crossplugin.md").read_text(encoding="utf-8")
    section = _heading_body(checks, "## Check 27 —")
    first = next(index for index, line in enumerate(section) if line.startswith("**Step 2 — Sibling reach-in**:"))
    last = next((index for index in range(first, len(section)) if section[index].startswith("**Step 3")), len(section))
    (command,) = _bash_blocks(section[first:last])
    return command


def test_compaction_boundary_gate_has_no_placeholder() -> None:
    """Keep the gate isolated, guarded, and executable without model substitutions.

    The boundary step is a fixed block the orchestrator runs verbatim, so it must hold no ``<placeholder>`` the model
    would have to fill in and must keep its provenance guard and fallback echo exactly.
    """
    gate = _boundary_gate()
    assert re.search(r"<[^>\n]+>", gate) is None
    assert gate == (
        '_RS="${CLAUDE_PLUGIN_ROOT:+$CLAUDE_PLUGIN_ROOT/bin/resolve_shared_path.py}"\n'
        '[ -n "$_RS" ] || _RS=$(ls plugins/cc_*/bin/resolve_shared_path.py 2>/dev/null | head -1)\n'
        '[ -f "$_RS" ] && _GD=$(python "$_RS" foundry bin 2>/dev/null) && [ -f "$_GD/compact_gate.py" ] '
        '&& python "$_GD/compact_gate.py" || echo "compact-gate: continue (foundry gate unavailable)"'
    )


@pytest.mark.integration
@_skip_check27_tools_unavailable
@pytest.mark.parametrize(
    ("mutate", "flagged_line"),
    [
        pytest.param(lambda gate: gate, None, id="guarded-gate-allowed"),
        pytest.param(
            lambda gate: gate.replace('[ -f "$_GD/compact_gate.py" ] && ', ""), 3, id="unguarded-gate-flagged"
        ),
        pytest.param(
            lambda gate: gate.replace('python "$_GD/compact_gate.py"', 'python "$_GD/other.py"'),
            3,
            id="guard-with-different-script-flagged",
        ),
        pytest.param(lambda gate: gate.split(' || echo "compact-gate:', 1)[0], 3, id="guard-without-fallback-flagged"),
        pytest.param(
            lambda _gate: 'python "plugins/cc_oss/bin/resolve_shared_path.py" foundry bin',
            1,
            id="literal-ungated-resolver-flagged",
        ),
    ],
)
def test_check27_optional_foundry_gate(tmp_path: Path, mutate: Callable[[str], str], flagged_line: int | None) -> None:
    """Execute the shipped audit against guarded and ungated sibling resolver calls.

    The real Check 27 command (taken from the audit template) runs in Bash over a sibling skill holding the compaction
    gate in one mutated form: only the intact guarded gate is allowed; an unguarded call, a guard for a different
    script, a missing fallback or a literal sibling path must each be reported at the offending line.
    """
    gate = mutate(_boundary_gate())
    skill_path = Path("plugins/cc_oss/skills/provider/SKILL.md")
    skill = tmp_path / skill_path
    skill.parent.mkdir(parents=True)
    skill.write_text(gate + "\n", encoding="utf-8")

    result = subprocess.run(
        ["bash", "-c", _check27_sibling_reach_in_command()], cwd=tmp_path, capture_output=True, text=True, check=False
    )

    expected = (
        []
        if flagged_line is None
        else [f"{skill_path.as_posix()}:{flagged_line}:{gate.splitlines()[flagged_line - 1]}"]
    )
    assert (result.returncode, result.stderr, result.stdout.splitlines()) == (0, "", expected)

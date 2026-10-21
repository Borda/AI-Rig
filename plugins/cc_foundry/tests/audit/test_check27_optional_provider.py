"""Guard the compaction boundary and Check 27's optional-provider exception."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from _hook_env import _bash_runs_posix_script


def test_compaction_boundary_gate_has_no_placeholder() -> None:
    """Keep the gate isolated, guarded, and executable without model substitutions."""
    contract = Path(__file__).resolve().parents[2] / "skills" / "_shared" / "compaction-contract.md"
    boundary = contract.read_text(encoding="utf-8").split("## Boundary step\n", 1)[1].split("\n## ", 1)[0]
    blocks = re.findall(r"```bash\n(.*?)\n```", boundary, re.DOTALL)
    assert len(blocks) == 1
    assert re.search(r"<[^>\n]+>", blocks[0]) is None
    assert blocks[0] == (
        '_RS="${CLAUDE_PLUGIN_ROOT:+$CLAUDE_PLUGIN_ROOT/bin/resolve_shared_path.py}"\n'
        '[ -n "$_RS" ] || _RS=$(ls plugins/cc_*/bin/resolve_shared_path.py 2>/dev/null | head -1)\n'
        '[ -f "$_RS" ] && _GD=$(python "$_RS" foundry bin 2>/dev/null) && [ -f "$_GD/compact_gate.py" ] '
        '&& python "$_GD/compact_gate.py" || echo "compact-gate: continue (foundry gate unavailable)"'
    )


@pytest.mark.integration
@pytest.mark.skipif(
    not _bash_runs_posix_script() or any(shutil.which(tool) is None for tool in ("grep", "awk")),
    reason="Check 27's executable audit requires Bash executing POSIX scripts, grep and awk",
)
@pytest.mark.parametrize(
    ("case", "flagged_line"),
    [
        pytest.param("guarded-gate", None, id="guarded-gate-allowed"),
        pytest.param("unguarded-gate", 3, id="unguarded-gate-flagged"),
        pytest.param("different-script", 3, id="guard-with-different-script-flagged"),
        pytest.param("missing-fallback", 3, id="guard-without-fallback-flagged"),
        pytest.param("literal-resolver", 1, id="literal-ungated-resolver-flagged"),
    ],
)
def test_check27_optional_foundry_gate(tmp_path: Path, case: str, flagged_line: int | None) -> None:
    """Execute the shipped audit against guarded and ungated sibling resolver calls."""
    plugin = Path(__file__).resolve().parents[2]
    contract = (plugin / "skills" / "_shared" / "compaction-contract.md").read_text(encoding="utf-8")
    boundary = contract.split("## Boundary step\n", 1)[1].split("\n## ", 1)[0]
    gate = re.findall(r"```bash\n(.*?)\n```", boundary, re.DOTALL)[0]
    if case == "unguarded-gate":
        gate = gate.replace('[ -f "$_GD/compact_gate.py" ] && ', "")
    elif case == "different-script":
        gate = gate.replace('python "$_GD/compact_gate.py"', 'python "$_GD/other.py"')
    elif case == "missing-fallback":
        gate = gate.split(' || echo "compact-gate:', 1)[0]
    elif case == "literal-resolver":
        gate = 'python "plugins/cc_oss/bin/resolve_shared_path.py" foundry bin'

    skill_path = Path("plugins/cc_oss/skills/provider/SKILL.md")
    skill = tmp_path / skill_path
    skill.parent.mkdir(parents=True)
    skill.write_text(gate + "\n", encoding="utf-8")
    checks = (plugin / "skills" / "audit" / "templates" / "checks-skills-crossplugin.md").read_text(encoding="utf-8")
    check27 = checks.split("## Check 27 —", 1)[1].split("\n## ", 1)[0]
    step2 = check27.split("**Step 2 — Sibling reach-in**:", 1)[1].split("**Step 3", 1)[0]
    command = re.findall(r"```bash\n(.*?)\n```", step2, re.DOTALL)
    assert len(command) == 1
    result = subprocess.run(["bash", "-c", command[0]], cwd=tmp_path, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    expected = (
        []
        if flagged_line is None
        else [f"{skill_path.as_posix()}:{flagged_line}:{gate.splitlines()[flagged_line - 1]}"]
    )
    assert result.stdout.splitlines() == expected

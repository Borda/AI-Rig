"""Exercise debug's shipped shell recipes and the diagnosis information passed to fix."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from _develop_text import bash_fences

_PLUGIN = Path(__file__).resolve().parents[2]
_SKILL = _PLUGIN / "skills" / "debug" / "SKILL.md"
_BASH = shutil.which("bash")
_CSID = "debug-workflow-test"
_skip_no_bash = pytest.mark.skipif(_BASH is None, reason="bash unavailable")


def _block(marker: str) -> str:
    """Find the executable fence containing a unique command marker."""
    return next(block for block in bash_fences(_SKILL.read_text(encoding="utf-8")) if marker in block)


@pytest.fixture
def debug_env(tmp_path: Path) -> dict[str, str]:
    """Prepare isolated sentinels and an installed plugin copy without an optional gate provider."""
    plugin = tmp_path / "plugin"
    scripts = plugin / "bin"
    scripts.mkdir(parents=True)
    for name in ("append_ledger.py", "write_skill_contract.py", "compaction_boundary.py", "get_plugin_install_path.py"):
        shutil.copy2(_PLUGIN / "bin" / name, scripts / name)
    shared = plugin / "skills" / "_shared"
    shared.mkdir(parents=True)
    shutil.copy2(_PLUGIN / "skills" / "_shared" / "compaction-contract.md", shared)
    return {
        **os.environ,
        "CLAUDE_PLUGIN_ROOT": str(plugin),
        "CLAUDE_CODE_SESSION_ID": _CSID,
        "CLAUDE_CONFIG_DIR": str(tmp_path / "config"),
        "TMPDIR": str(tmp_path),
        "PATH": os.pathsep.join((str(Path(sys.executable).parent), os.environ.get("PATH", ""))),
    }


def _run(block: str, tmp_path: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Execute a shipped recipe in a private directory with bounded runtime."""
    return subprocess.run(
        [_BASH or "bash", "-c", block], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=20, check=False
    )


@_skip_no_bash
def test_flaky_recipe_does_not_substitute_arguments_into_comments(tmp_path: Path, debug_env: dict[str, str]) -> None:
    """A newline in an argument blob cannot turn a commented example into a shell command."""
    payload = "\nprintf injected > injected\n#"
    block = _block("find-polluter.py").replace("$ARGUMENTS", payload)
    result = _run(block, tmp_path, debug_env)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "injected").exists()
    assert "not resolved" in result.stdout


@_skip_no_bash
@pytest.mark.parametrize("verdict", ["open", "refuted (challenger)", "ruled-out (probe)"])
def test_hypothesis_records_remain_literal_data(tmp_path: Path, debug_env: dict[str, str], verdict: str) -> None:
    """Shell metacharacters in candidate names are appended without execution or lost prior events."""
    ledger = tmp_path / f"dev-debug-hypotheses-{_CSID}"
    ledger.write_text("earlier :: open\n", encoding="utf-8")
    record = f'$(touch dollar) `touch backtick` "quoted" :: {verdict}'
    staged = ledger.with_name(ledger.name + ".rec")
    staged.write_text(record, encoding="utf-8")
    block = _block("# ledger survives compaction")
    assert "bin/append_ledger.py" in block
    result = _run(block, tmp_path, debug_env)
    assert result.returncode == 0, result.stderr
    assert ledger.read_text(encoding="utf-8") == f"earlier :: open\n{record}\n"
    assert not staged.exists()
    assert not (tmp_path / "dollar").exists()
    assert not (tmp_path / "backtick").exists()


@_skip_no_bash
@pytest.mark.parametrize("later_candidates", [1, 12])
def test_boundary_preserves_last_verdict_for_every_candidate(
    tmp_path: Path, debug_env: dict[str, str], later_candidates: int
) -> None:
    """Repeated events and later distinct causes cannot erase an early ruled-out cause after compaction."""
    ledger = tmp_path / f"dev-debug-hypotheses-{_CSID}"
    events = "early :: open\nearly :: ruled-out (probe)\n"
    events += "".join(f"later-{index} :: open\n" for index in range(later_candidates))
    events += "latest :: open\n" * 9
    ledger.write_text(events, encoding="utf-8")
    result = _run(_block("_TRIED="), tmp_path, debug_env)
    assert result.returncode == 0, result.stderr
    contract = (tmp_path / ".temp" / "state" / "skill-contract.md").read_text(encoding="utf-8")
    assert "early :: ruled-out (probe)" in contract
    assert "early :: open" not in contract
    assert contract.count("latest :: open") == 1
    assert f"later-{later_candidates - 1} :: open" in contract


@_skip_no_bash
def test_boundary_emits_compaction_metadata_and_provider_fallback(tmp_path: Path, debug_env: dict[str, str]) -> None:
    """The hypothesis transition reaches the compaction advice even without a shared-path sentinel."""
    result = _run(_block("_TRIED="), tmp_path, debug_env)
    assert result.returncode == 0, result.stderr
    contract = (tmp_path / ".temp" / "state" / "skill-contract.md").read_text(encoding="utf-8")
    metadata = next(index for index, line in enumerate(contract.splitlines()) if line == "- gate-metadata:")
    assert any("calls_ahead:" in line for line in contract.splitlines()[metadata + 1 :])
    assert any("drop:" in line for line in contract.splitlines()[metadata + 1 :])
    assert "## Boundary step" in result.stdout
    assert "compact-gate: continue (foundry gate unavailable)" in result.stdout


@_skip_no_bash
def test_diagnosis_path_preserves_existing_handoffs(tmp_path: Path, debug_env: dict[str, str]) -> None:
    """A repeated symptom selects the first free counter suffix while old diagnosis bytes remain intact."""
    (tmp_path / f"dev-debug-slug-{_CSID}").write_text("sample-symptom\n", encoding="utf-8")
    active = tmp_path / ".plans" / "active"
    active.mkdir(parents=True)
    first = active / "debug_sample-symptom.md"
    second = active / "debug_sample-symptom-2.md"
    first.write_text("earlier diagnosis", encoding="utf-8")
    second.write_text("second diagnosis", encoding="utf-8")
    result = _run(_block('DIAG_FILE="$_DIAG_BASE/'), tmp_path, debug_env)
    assert result.returncode == 0, result.stderr
    chosen = Path((tmp_path / f"dev-debug-diag-file-{_CSID}").read_text(encoding="utf-8").strip())
    assert chosen == active / "debug_sample-symptom-3.md"
    assert not chosen.exists()
    assert first.read_text(encoding="utf-8") == "earlier diagnosis"
    assert second.read_text(encoding="utf-8") == "second diagnosis"


@_skip_no_bash
def test_diagnosis_path_uses_unsuffixed_name_when_free(tmp_path: Path, debug_env: dict[str, str]) -> None:
    """The first diagnosis keeps its familiar unsuffixed handoff path."""
    (tmp_path / f"dev-debug-slug-{_CSID}").write_text("sample-symptom\n", encoding="utf-8")
    result = _run(_block('DIAG_FILE="$_DIAG_BASE/'), tmp_path, debug_env)
    assert result.returncode == 0, result.stderr
    chosen = Path((tmp_path / f"dev-debug-diag-file-{_CSID}").read_text(encoding="utf-8").strip())
    assert chosen == tmp_path / ".plans" / "active" / "debug_sample-symptom.md"
    assert not chosen.exists()


@_skip_no_bash
@pytest.mark.parametrize("installed", [False, True])
def test_research_gate_checks_the_active_install_registry(
    tmp_path: Path, debug_env: dict[str, str], installed: bool
) -> None:
    """Domain validation uses the research agent only when its optional plugin resolves."""
    config = tmp_path / "config" / "plugins"
    config.mkdir(parents=True)
    entries = [{"installPath": str(tmp_path / "research"), "installedAt": "2026-01-01T00:00:00Z"}]
    registry = {"plugins": {"research@borda-ai-rig": entries if installed else []}}
    (config / "installed_plugins.json").write_text(json.dumps(registry), encoding="utf-8")
    result = _run(_block("get_plugin_install_path.py"), tmp_path, debug_env)
    assert result.returncode == 0, result.stderr
    expected = "research:scientist available" if installed else "Expected value unverified — treating as assumption"
    assert expected in result.stdout


def test_diagnosis_carries_reproduction_context() -> None:
    """The saved handoff identifies the original reproduction rather than forcing another investigation."""
    lines = _SKILL.read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line == "## Reproduction")
    end = next(index for index in range(start + 1, len(lines)) if lines[index].startswith("## "))
    fields = [line.split(":", 1)[0] for line in lines[start + 1 : end] if line.startswith("- ")]
    assert fields == ["- Test node ID / path", "- Runner exit", "- Issue ref", "- --repo", "- Mode"]
    assert any("dev-gate-exit-${CSID}" in line for line in lines[end:])
    assert any("only in issue mode; in symptom mode record `none`" in line for line in lines[end:])


def test_challenger_preserves_surviving_candidates() -> None:
    """A candidate that survives the challenge remains eligible for the next hypothesis gate."""
    lines = _SKILL.read_text(encoding="utf-8").splitlines()
    rule = next(line for line in lines if line.startswith("Parse result — stage each candidate"))
    assert "<candidate cause> :: <verdict>" in rule
    assert "only for candidates the challenger actually refuted" in rule
    assert "surviving candidates remain `open`" in rule


def test_scope_analysis_uses_one_agent_named_deliverable() -> None:
    """The scope agent, deadline watch and user gate refer to the same permitted handoff filename."""
    lines = _SKILL.read_text(encoding="utf-8").splitlines()
    spawn = next(line for line in lines if line.startswith("Spawn **foundry:sw-engineer**"))
    handoff = next(line for line in lines if line.startswith("Carry the analysis summary"))
    assert ".temp/debug/<slug>-sw-engineer.md" in spawn
    assert handoff.count(".temp/debug/<slug>-sw-engineer.md") == 2
    assert any("Agents this skill uses:" in line and "research:scientist" in line for line in lines)

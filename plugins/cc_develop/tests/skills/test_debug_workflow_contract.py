"""Exercise debug's shipped shell recipes and the diagnosis information passed to fix."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from _develop_shell_plugin import render_shell, shell_path

_PLUGIN = Path(__file__).resolve().parents[2]
_SKILL = _PLUGIN / "skills" / "debug" / "SKILL.md"
_BASH = shutil.which("bash")
_CSID = "debug-workflow-test"
_skip_no_bash = pytest.mark.skipif(_BASH is None, reason="bash unavailable")


def _block(marker: str) -> str:
    """Find the executable fence containing a unique command marker."""
    blocks: list[str] = []
    current: list[str] | None = None
    for line in _SKILL.read_text(encoding="utf-8").splitlines():
        if line == "```bash":
            current = []
        elif line == "```" and current is not None:
            blocks.append("\n".join(current))
            current = None
        elif current is not None:
            current.append(line)
    return next(block for block in blocks if marker in block)


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
    (tmp_path / f"dev-debug-slug-{_CSID}").write_text("sample-symptom\n", encoding="utf-8", newline="\n")
    evidence = tmp_path / ".temp" / "debug" / "sample-symptom-evidence.md"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("Reproduction: failing test\nTask IDs: 1\n", encoding="utf-8")
    return {
        **os.environ,
        "CLAUDE_PLUGIN_ROOT": str(plugin),
        "CLAUDE_CODE_SESSION_ID": _CSID,
        "CLAUDE_CONFIG_DIR": str(tmp_path / "config"),
        "TMPDIR": str(tmp_path),
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PATH": os.pathsep.join((str(Path(sys.executable).parent), os.environ.get("PATH", ""))),
    }


def _run(block: str, tmp_path: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Execute a shipped recipe in a private directory with bounded runtime."""
    return subprocess.run(
        [_BASH, "-c", render_shell(block, Path(env.get("CLAUDE_PLUGIN_ROOT", str(_PLUGIN))), "develop")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        check=False,
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
    ledger.write_text("earlier :: open\n", encoding="utf-8", newline="\n")
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
@pytest.mark.integration
def test_hypothesis_recipe_authorizes_temp_ledger(tmp_path: Path, debug_env: dict[str, str]) -> None:
    """The documented caller appends session-scoped records with the canonical helper's confinement."""
    helper = _PLUGIN.parent / "cc_foundry" / "bin" / "append_ledger.py"
    shutil.copy2(helper, Path(debug_env["CLAUDE_PLUGIN_ROOT"]) / "bin" / "append_ledger.py")
    ledger = tmp_path / f"dev-debug-hypotheses-{_CSID}"
    ledger.write_bytes(b"earlier :: open\n")
    staged = ledger.with_name(ledger.name + ".rec")
    staged.write_bytes(b"current :: ruled-out (probe)")
    result = _run(_block("# ledger survives compaction"), tmp_path, debug_env)
    assert result.returncode == 0, result.stderr
    assert ledger.read_bytes() == b"earlier :: open\ncurrent :: ruled-out (probe)\n"
    assert not staged.exists()


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
    ledger.write_text(events, encoding="utf-8", newline="\n")
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
    (tmp_path / f"dev-debug-slug-{_CSID}").write_text("sample-symptom\n", encoding="utf-8", newline="\n")
    active = tmp_path / ".plans" / "active"
    active.mkdir(parents=True)
    first = active / "debug_sample-symptom.md"
    second = active / "debug_sample-symptom-2.md"
    first.write_text("earlier diagnosis", encoding="utf-8")
    second.write_text("second diagnosis", encoding="utf-8")
    result = _run(_block('DIAG_FILE="$_DIAG_BASE/'), tmp_path, debug_env)
    assert result.returncode == 0, result.stderr
    chosen = shell_path((tmp_path / f"dev-debug-diag-file-{_CSID}").read_text(encoding="utf-8").strip())
    assert chosen == active / "debug_sample-symptom-3.md"
    assert not chosen.exists()
    assert first.read_text(encoding="utf-8") == "earlier diagnosis"
    assert second.read_text(encoding="utf-8") == "second diagnosis"


@_skip_no_bash
def test_diagnosis_path_uses_unsuffixed_name_when_free(tmp_path: Path, debug_env: dict[str, str]) -> None:
    """The first diagnosis keeps its familiar unsuffixed handoff path."""
    (tmp_path / f"dev-debug-slug-{_CSID}").write_text("sample-symptom\n", encoding="utf-8", newline="\n")
    result = _run(_block('DIAG_FILE="$_DIAG_BASE/'), tmp_path, debug_env)
    assert result.returncode == 0, result.stderr
    chosen = shell_path((tmp_path / f"dev-debug-diag-file-{_CSID}").read_text(encoding="utf-8").strip())
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


@_skip_no_bash
def test_hypothesis_append_stops_when_the_staged_record_is_missing(tmp_path: Path, debug_env: dict[str, str]) -> None:
    """A misplaced Write record cannot silently produce an empty loop guard."""
    result = _run(_block("# ledger survives compaction"), tmp_path, debug_env)
    assert result.returncode != 0
    assert "staged hypothesis record missing" in result.stdout
    assert not (tmp_path / f"dev-debug-hypotheses-{_CSID}").exists()


@_skip_no_bash
def test_boundary_rejects_evidence_that_was_not_persisted(tmp_path: Path, debug_env: dict[str, str]) -> None:
    """Compaction cannot drop reproduction and analysis while its report is absent."""
    (tmp_path / ".temp/debug/sample-symptom-evidence.md").unlink()
    result = _run(_block("_TRIED="), tmp_path, debug_env)
    assert result.returncode != 0
    assert "evidence note missing" in result.stdout
    assert not (tmp_path / ".temp/state/skill-contract.md").exists()


@_skip_no_bash
def test_boundary_preserves_evidence_and_analysis_paths(tmp_path: Path, debug_env: dict[str, str]) -> None:
    """The resume contract names the durable evidence and analysis needed by the hypothesis gate."""
    result = _run(_block("_TRIED="), tmp_path, debug_env)
    assert result.returncode == 0, result.stderr
    lines = (tmp_path / ".temp/state/skill-contract.md").read_text(encoding="utf-8").splitlines()
    assert any("phase-report=.temp/debug/sample-symptom-evidence.md" in line for line in lines)
    assert any("analysis=.temp/debug/sample-symptom-sw-engineer.md" in line for line in lines)


@_skip_no_bash
def test_boundary_keeps_distinct_candidates_with_embedded_delimiters(tmp_path: Path, debug_env: dict[str, str]) -> None:
    """Candidate text containing the delimiter cannot merge unrelated last-verdict records."""
    ledger = tmp_path / f"dev-debug-hypotheses-{_CSID}"
    ledger.write_text(
        "cache :: stale :: open\ncache :: empty :: open\ncache :: stale :: ruled-out (probe)\n", newline="\n"
    )
    result = _run(_block("_TRIED="), tmp_path, debug_env)
    assert result.returncode == 0, result.stderr
    contract = (tmp_path / ".temp/state/skill-contract.md").read_text(encoding="utf-8")
    assert "cache :: stale :: ruled-out (probe)" in contract
    assert "cache :: empty :: open" in contract
    assert "cache :: stale :: open" not in contract


@_skip_no_bash
def test_diagnosis_path_is_stable_when_the_recipe_is_resumed(tmp_path: Path, debug_env: dict[str, str]) -> None:
    """A resumed Step 4 keeps the completed diagnosis instead of allocating an incomplete second handoff."""
    first = _run(_block('DIAG_FILE="$_DIAG_BASE/'), tmp_path, debug_env)
    assert first.returncode == 0, first.stderr
    sentinel = tmp_path / f"dev-debug-diag-file-{_CSID}"
    diagnosis = shell_path(sentinel.read_text(encoding="utf-8").strip())
    diagnosis.write_text("## Root Cause\nconfirmed cause\n", encoding="utf-8")
    resumed = _run(_block('DIAG_FILE="$_DIAG_BASE/'), tmp_path, debug_env)
    assert resumed.returncode == 0, resumed.stderr
    assert shell_path(sentinel.read_text(encoding="utf-8").strip()) == diagnosis
    assert diagnosis.read_text(encoding="utf-8") == "## Root Cause\nconfirmed cause\n"
    assert not diagnosis.with_name("debug_sample-symptom-2.md").exists()


@_skip_no_bash
def test_completion_removes_unconsumed_hypothesis_records(tmp_path: Path, debug_env: dict[str, str]) -> None:
    """An aborted or completed run cannot leave staged verdicts to be consumed by a later invocation."""
    staged = tmp_path / f"dev-debug-hypotheses-{_CSID}.rec"
    staged.write_text("old symptom :: refuted (challenger)\n", encoding="utf-8", newline="\n")
    result = _run(_block("# clear contract + ledger"), tmp_path, debug_env)
    assert result.returncode == 0, result.stderr
    assert not staged.exists()

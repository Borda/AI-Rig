"""Exercise safe investigation recipes and prose-only humanizer boundaries."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from _hook_env import _bash_runs_posix_script
from _plugin_payload import install_foundry

_SKILLS = Path(__file__).resolve().parents[2] / "skills"
_BASH = pytest.mark.skipif(not _bash_runs_posix_script(), reason="POSIX Bash execution unavailable")
_JQ = pytest.mark.skipif(shutil.which("jq") is None, reason="jq unavailable")


def _section(skill: str, heading: str) -> list[str]:
    """Read one skill section as independent instruction lines."""
    lines = (_SKILLS / skill / "SKILL.md").read_text(encoding="utf-8").splitlines()
    start = lines.index(heading)
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return lines[start:end]


@pytest.fixture
def probe_env(tmp_path: Path) -> dict[str, str]:
    """Provide a private temporary directory without inheriting credentials."""
    env = {
        key: os.environ[key]
        for key in ("PATH", "SystemRoot", "SYSTEMROOT", "COMSPEC", "PATHEXT", "TEMP", "TMP")
        if key in os.environ
    }
    env.update(TMPDIR=str(tmp_path), HOME=str(tmp_path), USERPROFILE=str(tmp_path), CLAUDE_CODE_SESSION_ID="probe-test")
    return env


@_BASH
@pytest.mark.parametrize(
    ("name", "value"),
    [
        pytest.param("UV_INDEX_URL", "https://user:private-pass@example.invalid/simple", id="url-userinfo"),
        pytest.param("NODE_AUTH", "private-node-auth", id="node-auth"),
        pytest.param("CLAUDE_X_CREDENTIALS", "private-claude-credentials", id="claude-credentials"),
        pytest.param("UV_EXTRA", "first-line\nprivate-multiline", id="multiline-value"),
    ],
)
def test_environment_recipe_prints_names_without_values(probe_env: dict[str, str], name: str, value: str) -> None:
    """Secret values cannot escape through diagnostic name filters or multiline output."""
    lines = _section("investigate", "## Step 2: Gather signals")
    command = next(line for line in lines if "env |" in line or "sorted(os.environ)" in line)
    command = command.replace("python ", shlex.quote(sys.executable) + " ", 1)
    result = subprocess.run(
        ["bash", "-c", command], env={**probe_env, name: value}, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert name in result.stdout.splitlines()
    assert value not in result.stdout
    assert "private-" not in result.stdout


@_BASH
@_JQ
@pytest.mark.parametrize("empty_env", [False, True])
def test_settings_projection_excludes_credentials_and_redacts_url_userinfo(
    tmp_path: Path, probe_env: dict[str, str], empty_env: bool
) -> None:
    """Settings diagnostics retain registrations while excluding env and MCP secrets."""
    settings = tmp_path / ".claude/settings.json"
    settings.parent.mkdir()
    content = {
        "permissions": {"allow": ["Read", "Bash(curl -H 'Authorization: Bearer private-bearer')"]},
        "hooks": {"Stop": [{"command": "curl --token private-flag https://user:private-url@example.invalid/hook"}]},
        "enabledPlugins": {"foundry@example": True},
        "mcpServers": {"remote": {"token": "private-mcp-token"}},
    }
    content.update({} if empty_env else {"env": {"NODE_AUTH": "private-env-token"}})
    settings.write_text(json.dumps(content), encoding="utf-8")
    lines = _section("investigate", "## Step 2: Gather signals")
    command = "\n".join(line for line in lines if line.startswith(("_CONFIG_FILTER=", "jq ")))
    result = subprocess.run(["bash", "-c", command], env=probe_env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    projected = json.loads(result.stdout)
    assert set(projected) == {"permissions", "hooks", "enabledPlugins", "env"}
    assert projected["permissions"] == {"allow": ["[REDACTED]", "[REDACTED]"]}
    assert projected["enabledPlugins"] == {"foundry@example": True}
    assert projected["env"] == ([] if empty_env else ["NODE_AUTH"])
    assert projected["hooks"] == {"Stop": [{"command": "[REDACTED]"}]}
    assert "private-" not in result.stdout


@_BASH
@_JQ
def test_settings_probes_compare_real_entries_without_printing_them(tmp_path: Path, probe_env: dict[str, str]) -> None:
    """Permission presence and credential-only sync drift remain observable without secret output."""
    global_settings = tmp_path / ".claude/settings.json"
    global_settings.parent.mkdir()
    global_settings.write_text(
        json.dumps({"permissions": {"allow": ["Read"]}, "env": {"NODE_AUTH": "private-user"}}),
        encoding="utf-8",
    )
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude/settings.json").write_text(
        json.dumps({"permissions": {"allow": ["Read"]}, "env": {"NODE_AUTH": "private-project"}}),
        encoding="utf-8",
    )
    lines = _section("investigate", "## Step 5: Probe top hypotheses")
    command = "\n".join(line for line in lines if line.startswith(("_CONFIG_FILTER=", "jq ", "diff ")))
    result = subprocess.run(
        ["bash", "-c", command], cwd=project, env=probe_env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["true", "false"]
    assert "private-" not in result.stdout


def _ledger_script() -> str:
    """Extract the actual ledger block through its append, excluding contract refresh."""
    lines = _section("investigate", "## Step 5: Probe top hypotheses")
    start = next(i for i, line in enumerate(lines) if line.startswith("export CSID="))
    end = next(i for i in range(start, len(lines)) if lines[i].startswith("_VERDICTS="))
    return "\n".join(lines[start:end])


def test_signal_collection_keeps_raw_logs_and_credentials_out_of_handoffs() -> None:
    """Log discovery and persisted symptoms cannot bypass credential sanitization."""
    lines = _section("investigate", "## Step 2: Gather signals")
    logs = next(line for line in lines if line.startswith("Use Grep"))
    assert "file paths only" in logs
    assert "before emitting" in logs
    storage = next(line for line in lines if line.startswith("Capture all sanitized output"))
    assert "Never write credentials" in storage
    assert "signals.md" in storage
    assert "Before Step 4" in storage
    symptom = next(line for line in lines if line.startswith("- `SYMPTOM_DESCRIPTION`"))
    assert "[REDACTED]" in symptom


@_BASH
def test_probe_ledger_rejects_unsubstituted_placeholders(tmp_path: Path, probe_env: dict[str, str]) -> None:
    """A copied template cannot append a placeholder verdict."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (tmp_path / "investigate-run-path-probe-test").write_text(str(run_dir) + "\n", encoding="utf-8")
    result = subprocess.run(["bash", "-c", _ledger_script()], env=probe_env, capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert "BLOCKED" in result.stdout
    assert not (tmp_path / "investigate-verdicts-probe-test").exists()


@_BASH
def test_probe_ledger_preserves_shell_metacharacters(tmp_path: Path, probe_env: dict[str, str]) -> None:
    """Hypothesis text stays literal and earlier ledger rows survive a new verdict."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (tmp_path / "investigate-run-path-probe-test").write_text(str(run_dir) + "\n", encoding="utf-8")
    ledger = tmp_path / "investigate-verdicts-probe-test"
    ledger.write_text("Prior hypothesis :: Ruled-out\n", encoding="utf-8")
    verdict = "Config's $NODE_AUTH $(touch injected-dollar) `touch injected-backtick` :: Confirmed"
    script = _ledger_script().replace("'<hypothesis> :: <Confirmed|Ruled-out|Inconclusive>'", shlex.quote(verdict))
    result = subprocess.run(
        ["bash", "-c", script], cwd=tmp_path, env=probe_env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert ledger.read_text(encoding="utf-8") == "Prior hypothesis :: Ruled-out\n" + verdict + "\n"
    assert not (tmp_path / "injected-dollar").exists()
    assert not (tmp_path / "injected-backtick").exists()


@_BASH
def test_probe_ledger_requires_run_directory(tmp_path: Path, probe_env: dict[str, str]) -> None:
    """A missing run sentinel stops the batch before a valid verdict is appended."""
    script = _ledger_script().replace(
        "'<hypothesis> :: <Confirmed|Ruled-out|Inconclusive>'", "'Missing hook :: Confirmed'"
    )
    result = subprocess.run(["bash", "-c", script], env=probe_env, capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert "BLOCKED — investigate run directory missing" in result.stdout
    assert not (tmp_path / "investigate-verdicts-probe-test").exists()


def test_humanizer_preserves_content_frontmatter_without_blocking_prose() -> None:
    """Report metadata stays protected while publishing and slide metadata permit body edits."""
    lines = _section("humanizer", "## 1. Load the target text")
    guard = next(line for line in lines if "**Destination guard**" in line)
    assert "any file whose first line" not in guard
    assert "report-schema frontmatter" in guard
    assert ".reports/" in guard
    assert ".temp/" in guard
    preserve = next(line for line in lines if "Marp" in line)
    assert "Jekyll/Hugo" in preserve
    assert "MkDocs" in preserve
    assert "preserve" in preserve
    assert "body" in preserve


def test_humanizer_check_mode_closes_with_confidence() -> None:
    """Read-only findings cannot return before their required confidence assessment."""
    apply = _section("humanizer", "## 3. Apply fixes")
    check = next(line for line in apply if "`check` mode:" in line)
    assert "Step 4" in check
    assert "Confidence" in check
    report = _section("humanizer", "## 4. Report")
    assert any("check" in line and "## Confidence" in line for line in report)
    assert any("**Score**" in line and "**Gaps**" in line for line in report)


@_BASH
def test_hook_probe_reads_installed_plugin_hooks(tmp_path: Path, probe_env: dict[str, str]) -> None:
    """The hook diagnostic lists the active plugin rather than a retired user directory."""
    plugin = install_foundry(tmp_path)
    (plugin / "hooks").mkdir(parents=True)
    (plugin / "hooks/registered.js").write_text("", encoding="utf-8")
    section = _section("investigate", "## Step 5: Probe top hypotheses")
    prologue = next(line for line in section if line.startswith("PLUGIN_ROOT=$(") and "resolve_plugin_root.py" in line)
    command = prologue + "\n" + next(line for line in section if "ls -la" in line)
    result = subprocess.run(
        ["bash", "-c", command],
        env={**probe_env, "CLAUDE_PLUGIN_ROOT": str(plugin)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "registered.js" in result.stdout

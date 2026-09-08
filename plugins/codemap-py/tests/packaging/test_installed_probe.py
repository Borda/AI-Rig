"""Real-install proof for the built package (collected, unlike the probe scripts).

Runs the Claude and Codex install probes as subprocesses against a fresh build. Each test skips with a named reason ONLY
when its runtime CLI is absent (CI runners without ``claude``/``codex``); when the CLI is present it asserts the probe
exits 0, reports ``verification.ok``, exposes the exact six-skill rosters and Codex hook config, installs to a path
OUTSIDE the repo checkout (source-hidden), and — via the probe's source-independent runtime proof — builds from a
DISPOSABLE source copy, DELETES it (source_checkout_unavailable), then executes ``doctor``/``index``/``query`` from the
installed bytes under a scrubbed env (no temp-checkout or repo path in env/argv, no such bytes in the installed cache)
with ``plugin_root`` inside the installed cache.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_REPO_ROOT = _PLUGIN_ROOT.parents[1]
_SCRIPTS = _PLUGIN_ROOT / "scripts"
_CLAUDE_CLI_AVAILABLE = shutil.which("claude") is not None
_CODEX_CLI_AVAILABLE = shutil.which("codex") is not None

_EXPECTED_CLAUDE_SKILLS = {
    "scan-codebase",
    "query-code",
    "test-impact",
    "rename-refs",
    "integration",
    "debrief-coding",
}


def _run_probe(script_name: str) -> dict:
    """Run a probe script and return its parsed JSON result plus the exit code."""
    proc = subprocess.run(
        [sys.executable, str(_SCRIPTS / script_name)],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    result = json.loads(proc.stdout)
    result["_returncode"] = proc.returncode
    return result


def _assert_source_hidden(installed_path: str) -> None:
    """Assert the installed plugin root lives outside the repository checkout."""
    installed = Path(installed_path).resolve()
    repo = _REPO_ROOT.resolve()
    assert installed != repo and repo not in installed.parents, f"installed under repo checkout: {installed}"


@pytest.mark.skipif(not _CLAUDE_CLI_AVAILABLE, reason="claude CLI not present on this runner")
@pytest.mark.integration
@pytest.mark.packaging
def test_claude_probe_installs_and_verifies_exact_roster() -> None:
    """The Claude probe installs the built package and verifies the exact 6-skill roster."""
    result = _run_probe("probe_claude_install.py")
    assert result["status"] == "ok", result
    assert result["_returncode"] == 0
    verification = result["verification"]
    assert verification["ok"] is True, verification["issues"]
    assert verification["roster"] == verification["skill_dirs"]
    assert set(verification["skill_dirs"]) == _EXPECTED_CLAUDE_SKILLS
    _assert_source_hidden(result["installed_path"])
    _assert_runtime_proof(result)


@pytest.mark.skipif(not _CODEX_CLI_AVAILABLE, reason="codex CLI not present on this runner")
@pytest.mark.integration
@pytest.mark.packaging
def test_codex_probe_installs_and_verifies_exact_roster() -> None:
    """The Codex probe installs the built package and verifies the exact six-skill roster (Phase 4)."""
    result = _run_probe("probe_codex_install.py")
    assert result["status"] == "ok", result
    assert result["_returncode"] == 0
    verification = result["verification"]
    assert verification["ok"] is True, verification["issues"]
    assert set(verification["skill_dirs"]) == _EXPECTED_CLAUDE_SKILLS
    assert verification["checks"]["hooks_config_present"] is True
    _assert_source_hidden(result["installed_path"])
    _assert_runtime_proof(result)


def _assert_runtime_proof(result: dict) -> None:
    """Assert the probe deleted the install source and executed via the shipped launcher."""
    assert result["verification"]["runtime_ok"] is True, result.get("runtime")
    runtime = result["runtime"]
    assert runtime["ok"] is True, runtime["detail"]
    assert runtime["execution_path"] == "launcher"
    checks = runtime["checks"]
    # Source-unavailable channel closure: disposable source copy deleted, no env/argv/byte leak.
    assert checks["source_checkout_unavailable"] is True
    assert checks["source_deleted"] is True
    assert checks["env_clean"] is True
    assert checks["argv_clean"] is True
    assert checks["no_source_refs"] is True
    # Execution through the SHIPPED launcher (not a Python-entry fallback) from installed bytes.
    assert checks["launcher_executable"] is True
    assert checks["plugin_root_installed"] is True
    assert checks["doctor_ok"] is True
    assert checks["index_ok"] is True
    assert checks["query_ok"] is True


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX executable-bit falsification")
@pytest.mark.packaging
def test_runtime_proof_fails_when_launcher_mode_stripped(tmp_path: Path) -> None:
    """Falsification: a non-executable installed launcher makes the runtime proof fail (no fallback)."""
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    import _probe_runtime  # noqa: PLC0415  (path insert must precede import)

    installed = tmp_path / "installed"
    (installed / "bin").mkdir(parents=True)
    launcher = installed / "bin" / "codemap-py"
    launcher.write_text("#!/bin/sh\necho should-not-run\n")
    launcher.chmod(0o644)  # stripped executable bit — the bug this proof must catch
    source = tmp_path / "src"
    source.mkdir()

    result = _probe_runtime.runtime_proof(installed, tmp_path / "work", [source], [_REPO_ROOT, source])

    assert result["checks"]["launcher_executable"] is False
    assert result["checks"]["doctor_ok"] is False
    assert result["ok"] is False

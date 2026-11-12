"""Exercise calibration bridge probes without relying on a persistent tool shell."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from _hook_env import _bash_runs_posix_script
from _plugin_payload import install_foundry

_CALIBRATE = Path(__file__).resolve().parents[2] / "skills/calibrate"


def _probe_block() -> str:
    """Read the bridge preflight recipe from its Markdown fence."""
    lines = (_CALIBRATE / "templates/pipeline-prompt.md").read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line == "### Pre-flight — Codex availability")
    first = next(index for index in range(start + 1, len(lines)) if lines[index] == "```bash")
    last = next(index for index in range(first + 1, len(lines)) if lines[index] == "```")
    return "\n".join(lines[first + 1 : last])


@pytest.mark.skipif(not _bash_runs_posix_script(), reason="POSIX Bash execution unavailable")
@pytest.mark.parametrize(
    ("helper_result", "status", "available"),
    [
        pytest.param("printf 'available\\npartial'; return 9", "absent", "false", id="partial-failure"),
        pytest.param("printf 'available\\n'", "available", "true", id="available"),
        pytest.param("printf 'disabled\\n'", "disabled", "false", id="disabled"),
    ],
)
def test_bridge_preflight_persists_and_exposes_a_single_status(
    tmp_path: Path, helper_result: str, status: str, available: str
) -> None:
    """Failed probes replace partial output, and successful probes expose reusable status."""
    script = _probe_block().replace("<TIMESTAMP>", "run").replace("<RUN_SLUG>", "example-inspect")
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'python() {{ case "$1" in */check_bridge.py) {helper_result};; *) "$TEST_PYTHON" "$@";; esac; }}\n'
            + script,
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "USERPROFILE": str(tmp_path),
            "CLAUDE_PLUGIN_ROOT": str(install_foundry(tmp_path)),
            "TEST_PYTHON": sys.executable,
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / ".reports/calibrate/run/example-inspect/bridge-status").read_text() == status + "\n"
    assert f"CODEX_AVAILABLE={available}" in result.stdout.splitlines()


@pytest.mark.skipif(not _bash_runs_posix_script(), reason="POSIX Bash execution unavailable")
def test_local_pipeline_uses_the_scope_resolved_manifest_layout(tmp_path: Path) -> None:
    """The selected skill file survives dispatch when its manifest uses a custom directory."""
    source = tmp_path / "plugins/cc_example/procedures/inspect/SKILL.md"
    source.parent.mkdir(parents=True)
    source.write_text("custom skill\n", encoding="utf-8")
    lines = (_CALIBRATE / "templates/pipeline-prompt.md").read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line == "### Pre-flight — local file resolution")
    first = next(index for index in range(start + 1, len(lines)) if lines[index] == "```bash")
    last = next(index for index in range(first + 1, len(lines)) if lines[index] == "```")
    script = "\n".join(lines[first + 1 : last])
    script = script.replace("<LOCAL_MODE>", "true").replace("<TARGET>", "/example:inspect")
    script = script.replace("<TARGET_SOURCE_PATH>", "plugins/cc_example/procedures/inspect/SKILL.md")
    result = subprocess.run(["bash", "-c", script], cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "local mode: using plugins/cc_example/procedures/inspect/SKILL.md"

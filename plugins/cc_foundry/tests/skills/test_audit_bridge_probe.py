"""Exercise the setup audit's bridge-status probe with partial helper output."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from _hook_env import _bash_runs_posix_script
from _plugin_payload import install_foundry

_CHECKS = Path(__file__).resolve().parents[2] / "skills/audit/templates/checks-setup.md"


def _bridge_block() -> str:
    """Read the executable bridge integration check from its Markdown section."""
    lines = _CHECKS.read_text(encoding="utf-8").splitlines()
    section = next(index for index, line in enumerate(lines) if line.startswith("## Check 7 —"))
    start = next(index for index in range(section + 1, len(lines)) if lines[index] == "```bash")
    end = next(index for index in range(start + 1, len(lines)) if lines[index] == "```")
    return "\n".join(lines[start + 1 : end])


@pytest.mark.skipif(not _bash_runs_posix_script(), reason="POSIX Bash execution unavailable")
@pytest.mark.parametrize(
    ("probe", "status", "message"),
    [
        pytest.param("printf 'available\\npartial'; return 9", "absent", "SKIPPED", id="partial-failure"),
        pytest.param("printf 'available\\n'", "available", "OK", id="available"),
        pytest.param("printf 'disabled\\n'", "disabled", "WARN", id="disabled"),
        pytest.param("printf 'absent\\n'", "absent", "SKIPPED", id="absent"),
    ],
)
def test_setup_bridge_check_persists_one_status(tmp_path: Path, probe: str, status: str, message: str) -> None:
    """A failed helper cannot concatenate partial stdout into a false availability verdict."""
    state = tmp_path / "audit-state-probe"
    state.mkdir()
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'python() {{ case "$1" in */check_bridge.py) {probe};; *) "$TEST_PYTHON" "$@";; esac; }}\n'
            + _bridge_block(),
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "TMPDIR": str(tmp_path),
            "HOME": str(tmp_path),
            "USERPROFILE": str(tmp_path),
            "CLAUDE_PLUGIN_ROOT": str(install_foundry(tmp_path)),
            "TEST_PYTHON": sys.executable,
            "CLAUDE_CODE_SESSION_ID": "probe",
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert f"{message}: Check 7" in result.stdout
    assert (state / "codex-status").read_text(encoding="utf-8") == status + "\n"

"""Execute audit mode ledger recipes under the explicit run-root contract."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from _hook_env import _bash_runs_posix_script

_PLUGIN = Path(__file__).resolve().parents[2]


def _append_command(relative: str) -> str:
    """Read the ledger append command from a mode's inline recipe."""
    lines = (_PLUGIN / "skills/audit" / relative).read_text(encoding="utf-8").splitlines()
    commands = (
        token.replace('\\"', '"')
        for line in lines
        for token in line.split("`")
        if token.startswith("python ") and "append_ledger.py" in token
    )
    return next(commands)


@pytest.mark.integration
@pytest.mark.skipif(not _bash_runs_posix_script(), reason="POSIX Bash execution unavailable")
@pytest.mark.parametrize(
    "relative", ["modes/adversarial.md", "modes/efficiency.md", "modes/upgrade.md", "templates/audit-fix-prompt.md"]
)
@pytest.mark.parametrize("record", ["", '{"status":"complete"}\n'])
def test_audit_mode_append_authorizes_only_its_run(tmp_path: Path, relative: str, record: str) -> None:
    """Each shipped recipe produces its ledger without relying on implicit project-wide authorization."""
    run_dir = tmp_path / ".reports/audit/current"
    run_dir.mkdir(parents=True)
    command = _append_command(relative).replace("<RUN_DIR>", str(run_dir))
    arguments = shlex.split(command)
    ledger = Path(arguments[2])
    staged = Path(str(ledger) + ".rec")
    staged.write_text(record, encoding="utf-8")
    result = subprocess.run(
        ["bash", "-c", command.replace("python ", shlex.quote(sys.executable) + " ", 1)],
        cwd=tmp_path,
        env={**os.environ, "CLAUDE_PLUGIN_ROOT": str(_PLUGIN)},
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert arguments[3:] == ["--run-root", str(run_dir)]
    assert ledger.read_text(encoding="utf-8") == record
    assert not staged.exists()


@pytest.mark.integration
@pytest.mark.skipif(not _bash_runs_posix_script(), reason="POSIX Bash execution unavailable")
def test_error_history_append_names_its_run_and_history_root(tmp_path: Path) -> None:
    """Preflight diagnostics can enter history without authorizing reads from another run."""
    run_dir = tmp_path / "audit-state-error"
    run_dir.mkdir()
    history = tmp_path / ".notes/logs"
    history.mkdir(parents=True)
    staged = run_dir / "audit-errors.jsonl.rec"
    record = '{"check":"preflight","error":"failed"}\n'
    staged.write_text(record, encoding="utf-8")
    command = _append_command("SKILL.md").replace("<ERROR_ROOT>", str(run_dir))
    result = subprocess.run(
        ["bash", "-c", command.replace("python ", shlex.quote(sys.executable) + " ", 1)],
        cwd=tmp_path,
        env={**os.environ, "CLAUDE_PLUGIN_ROOT": str(_PLUGIN)},
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (history / "audit-errors.jsonl").read_text(encoding="utf-8") == record
    assert staged.read_text(encoding="utf-8") == record
    assert shlex.split(command)[3:] == [
        "--run-root",
        str(run_dir),
        "--history-root",
        ".notes/logs",
        "--from",
        str(staged),
    ]

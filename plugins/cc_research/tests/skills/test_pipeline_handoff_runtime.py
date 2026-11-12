"""Exercise invocation-bound handoffs and remote metric execution from shipped skill blocks."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from _research_shell_plugin import render_shell

_PLUGIN = Path(__file__).resolve().parents[2]
_BASH = shutil.which("bash")
_JQ = shutil.which("jq")


def _block(relative: str, language: str, marker: str) -> str:
    """Read the fenced block containing an executable marker."""
    current = None
    for line in (_PLUGIN / relative).read_text(encoding="utf-8").splitlines():
        if line == f"```{language}":
            current = []
        elif line == "```" and current is not None:
            body = "\n".join(current)
            if marker in body:
                return body
            current = None
        elif current is not None:
            current.append(line)
    raise AssertionError(f"No block contains {marker}")


@pytest.mark.skipif(not _BASH, reason="Bash is required for the handoff contract")
@pytest.mark.parametrize(
    ("active", "substitution", "expected"),
    [
        pytest.param("stale", None, "direct.md|", id="standalone-ignores-stale-files"),
        pytest.param("current", "current", "swept.md|--inlined", id="active-sweep-handoff"),
        pytest.param("stale", "current", "direct.md|", id="mismatching-token"),
    ],
)
def test_keep_arguments_require_active_invocation_token(
    tmp_path: Path, active: str, substitution: str | None, expected: str
) -> None:
    """Leftover sweep files cannot override a standalone invocation's arguments."""
    (tmp_path / "sweep-active-token-session").write_text(active + "\n", encoding="utf-8", newline="\n")
    (tmp_path / "sweep-run-args-session").write_text("swept.md\n", encoding="utf-8", newline="\n")
    body = _block("skills/run/SKILL.md", "bash", "bin/extract-keep-flag.py")
    prefix = body.split('python "$PLUGIN_ROOT/bin/extract-keep-flag.py', 1)[0]
    prefix = prefix.replace("'<active-sweep-token>'", repr(substitution)) if substitution else prefix
    env = {**os.environ, "TMPDIR": tmp_path.as_posix(), "CLAUDE_CODE_SESSION_ID": "session", "ARGUMENTS": "direct.md"}
    result = subprocess.run(
        [
            _BASH,
            "-c",
            render_shell(
                prefix + '\nprintf "%s|%s" "$_RUN_ARGS" "$_INL"',
                Path(env.get("CLAUDE_PLUGIN_ROOT", str(_PLUGIN))),
                "research",
            ),
        ],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected


@pytest.mark.skipif(not _BASH, reason="Bash is required for the commit placeholder guard")
def test_unsubstituted_commit_template_never_invokes_git(tmp_path: Path) -> None:
    """An unresolved path, description or iteration must stop before staging."""
    body = _block("skills/run/SKILL.md", "bash", "git commit -m")
    prefix = 'git() { printf "UNEXPECTED_GIT_CALL\\n"; };\n'
    result = subprocess.run(
        [_BASH, "-c", render_shell(prefix + body, _PLUGIN, "research")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    assert result.returncode != 0
    assert "! BLOCKED" in result.stdout
    assert "UNEXPECTED_GIT_CALL" not in result.stdout


@pytest.mark.skipif(not _BASH, reason="The remote cell executes Bash")
@pytest.mark.parametrize("exit_code", [0, 7])
def test_colab_cell_reports_actual_command_exit(tmp_path: Path, exit_code: int) -> None:
    """A printed float cannot hide a nonzero remote command status."""
    body = _block("skills/run/modes/phase5-metric.md", "python", "command =")
    command = (
        f"{shlex.quote(Path(sys.executable).as_posix())} -c {shlex.quote(f'print(1.5); raise SystemExit({exit_code})')}"
    )
    body = body.replace('["bash",', f"[{_BASH!r},")
    body = body.replace("<JSON string literal for config.metric_cmd>", json.dumps(command))
    body = body.replace("<positive integer config.metric_timeout_sec>", "5")
    result = subprocess.run(
        [sys.executable, "-c", body],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "1.5" in result.stdout
    assert f"METRIC_EXIT={exit_code}" in result.stdout
    assert "METRIC_TIMEOUT=1" not in result.stdout


@pytest.mark.skipif(not _BASH or not hasattr(os, "killpg"), reason="Remote deadline requires POSIX process groups")
def test_colab_cell_enforces_metric_timeout(tmp_path: Path) -> None:
    """A hanging metric produces a timeout marker and exits promptly."""
    body = _block("skills/run/modes/phase5-metric.md", "python", "command =")
    command = f"{shlex.quote(Path(sys.executable).as_posix())} -c {shlex.quote('import time; time.sleep(20)')}"
    body = body.replace('["bash",', f"[{_BASH!r},")
    body = body.replace("<JSON string literal for config.metric_cmd>", json.dumps(command))
    body = body.replace("<positive integer config.metric_timeout_sec>", "1")
    result = subprocess.run(
        [sys.executable, "-c", body],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "METRIC_TIMEOUT=1" in result.stdout
    assert "METRIC_EXIT=0" not in result.stdout


@pytest.mark.skipif(not _BASH or not _JQ, reason="Timeout preparation requires Bash and jq")
def test_program_metric_limit_preserves_guard_budget(tmp_path: Path) -> None:
    """A configured slow metric cannot silently expand the guard execution budget."""
    state_dir = tmp_path / ".experiments/state/configured"
    state_dir.mkdir(parents=True)
    state = {"config": {"compute": "local", "metric_timeout_sec": 250}}
    (state_dir / "state.json").write_text(json.dumps(state), encoding="utf-8", newline="\n")
    (tmp_path / "research-run-id-timeouts").write_text("configured\n", encoding="utf-8", newline="\n")
    env = {key: value for key, value in os.environ.items() if key != "VERIFY_TIMEOUT_SEC"}
    env.update(TMPDIR=tmp_path.as_posix(), CLAUDE_CODE_SESSION_ID="timeouts")
    body = _block("skills/run/SKILL.md", "bash", "VERIFY_TIMEOUT_MS=$((")
    result = subprocess.run(
        [_BASH, "-c", render_shell(body, Path(env.get("CLAUDE_PLUGIN_ROOT", str(_PLUGIN))), "research")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "research-run-verify-timeout-ms-timeouts").read_text().strip() == "250000"
    assert (tmp_path / "research-run-guard-timeout-ms-timeouts").read_text().strip() == "120000"

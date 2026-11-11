"""Execute guard, sandbox-script and rework blocks in isolated fresh shells."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

_RUN = Path(__file__).resolve().parents[2] / "skills/run"
_BASH = shutil.which("bash")
_JQ = shutil.which("jq")
_shell_available = pytest.mark.skipif(not _BASH or not _JQ, reason="run shell blocks require Bash and jq")


def _block(relative: str, start: str, end: str, marker: str) -> str:
    """Read the executable fence containing a marker inside the selected heading section."""
    lines = (_RUN / relative).read_text(encoding="utf-8").splitlines()
    first = next(i for i, line in enumerate(lines) if line.startswith(start))
    last = next((i for i in range(first + 1, len(lines)) if lines[i].startswith(end)), len(lines))
    current: list[str] | None = None
    for line in lines[first:last]:
        if line == "```bash":
            current = []
        elif line == "```" and current is not None:
            if any(marker in item for item in current):
                return "\n".join(current)
            current = None
        elif current is not None:
            current.append(line)
    raise AssertionError(f"No Bash block contains {marker!r}")


@pytest.fixture
def shell_env(tmp_path: Path) -> dict[str, str]:
    """Prepare isolated sentinels and an environment without values from a previous shell."""
    (tmp_path / "research-run-id-fresh-guard").write_text("guard-run\n", encoding="utf-8")
    (tmp_path / "research-run-verify-timeout-ms-fresh-guard").write_text("120000\n", encoding="utf-8")
    (tmp_path / "research-run-sandbox-network-fresh-guard").write_text("bridge\n", encoding="utf-8")
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"GUARD_CMD", "RUN_ID", "CSID", "SCRIPT", "SANDBOX_NETWORK", "REWORK_SHA"}
    }
    env.update(
        TMPDIR=tmp_path.as_posix(), CLAUDE_CODE_SESSION_ID="fresh-guard", CAPTURE=(tmp_path / "wrapper-args").as_posix()
    )
    return env


@pytest.mark.integration
@_shell_available
@pytest.mark.parametrize(
    ("start", "end"),
    [
        pytest.param("### Step R2:", "### Step R3:", id="precondition"),
        pytest.param("#### Phase 6", "#### Phase 7", id="iteration"),
    ],
)
@pytest.mark.parametrize("guard", ["python guard.py --label 'validation guard'", "", None])
def test_docker_guard_reloads_config_in_a_fresh_shell(
    tmp_path: Path, shell_env: dict[str, str], start: str, end: str, guard: str | None
) -> None:
    """Both guard entry points pass the saved command intact and block absent commands."""
    state_dir = tmp_path / ".experiments/state/guard-run"
    state_dir.mkdir(parents=True)
    config = {} if guard is None else {"guard_cmd": guard}
    (state_dir / "state.json").write_text(json.dumps({"config": config}), encoding="utf-8")
    block = _block("SKILL.md", start, end, ".config.guard_cmd")
    script = 'python() { printf "%s\\n" "$SANDBOX_NETWORK" "$@" > "$CAPTURE"; }\n' + block

    result = subprocess.run(
        [_BASH, "-c", script], cwd=tmp_path, env=shell_env, capture_output=True, text=True, check=False
    )

    captured = tmp_path / "wrapper-args"
    assert result.returncode == (0 if guard else 1), (result.stdout, result.stderr)
    assert captured.exists() == bool(guard)
    assert (captured.read_text(encoding="utf-8").splitlines()[-3:] if guard else "! BLOCKED" in result.stdout) == (
        ["--mode", "verify", guard] if guard else True
    )
    assert (captured.read_text(encoding="utf-8").splitlines()[0] if guard else "config.guard_cmd" in result.stdout) == (
        "bridge" if guard else True
    )


@pytest.mark.integration
@_shell_available
@pytest.mark.parametrize(
    ("start", "end"),
    [
        pytest.param("### Step R2:", "### Step R3:", id="precondition"),
        pytest.param("#### Phase 6", "#### Phase 7", id="iteration"),
    ],
)
def test_docker_guard_checks_jq_before_reading_state(
    tmp_path: Path, shell_env: dict[str, str], start: str, end: str
) -> None:
    """An absent jq stops before either config lookup or the sandbox wrapper."""
    block = _block("SKILL.md", start, end, ".config.guard_cmd")
    script = (
        "command() { return 1; }\n"
        'jq() { printf "jq-called\\n" >> "$CAPTURE"; }\n'
        'python() { printf "wrapper-called\\n" >> "$CAPTURE"; }\n' + block
    )

    result = subprocess.run(
        [_BASH, "-c", script], cwd=tmp_path, env=shell_env, capture_output=True, text=True, check=False
    )

    assert result.returncode == 1
    assert "jq not on PATH" in result.stdout
    assert not (tmp_path / "wrapper-args").exists()


@pytest.mark.integration
@_shell_available
@pytest.mark.parametrize("script_name", ["explore validation.py", "", "<script>", "explore>.py"])
def test_docker_exploration_assigns_and_validates_the_returned_script(
    tmp_path: Path, shell_env: dict[str, str], script_name: str
) -> None:
    """A supplied script filename is quoted intact; empty or unresolved values never execute."""
    block = _block("modes/compute-docker.md", "## Phase 2a", "## Phase 2b", "docker_sandbox_run.py")
    assignment = next(line for line in block.splitlines() if line.startswith('SCRIPT="<'))
    block = block.replace(assignment, "SCRIPT=" + shlex.quote(script_name), 1)
    script = 'python() { printf "%s\\n" "$@" > "$CAPTURE"; }\n' + block

    result = subprocess.run(
        [_BASH, "-c", script], cwd=tmp_path, env=shell_env, capture_output=True, text=True, check=False
    )

    valid = script_name == "explore validation.py"
    captured = tmp_path / "wrapper-args"
    assert result.returncode == (0 if valid else 1), (result.stdout, result.stderr)
    assert captured.exists() == valid
    assert (captured.read_text(encoding="utf-8").splitlines()[-3:] if valid else "! BLOCKED" in result.stdout) == (
        ["--mode", "explore", f".experiments/state/guard-run/scripts/{script_name}"] if valid else True
    )


@pytest.mark.integration
@_shell_available
def test_rework_sha_survives_a_fresh_shell_before_scope_check(tmp_path: Path, shell_env: dict[str, str]) -> None:
    """Fold agent commits into the saved experiment before invoking the scope helper."""
    save = _block("SKILL.md", "#### Phase 7 —", "#### Phase 7a", "git rev-parse HEAD >")
    saved = subprocess.run(
        [_BASH, "-c", 'git() { printf "experiment-sha\\n"; }\n' + save],
        cwd=tmp_path,
        env=shell_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert saved.returncode == 0, saved.stderr
    assert (tmp_path / "research-run-rework-sha-fresh-guard").read_text(encoding="utf-8") == "experiment-sha\n"
    fold = _block("SKILL.md", "#### Phase 7 —", "#### Phase 7a", "check_iteration_scope.py")
    script = (
        'git() { case "$1" in rev-parse) printf "agent-sha\\n" ;; '
        'reset) printf "%s\\n" "$*" > "$CAPTURE" ;; esac; }\n'
        'python() { printf "%s\\n" "$*" >> "$CAPTURE"; }\n' + fold
    )

    result = subprocess.run(
        [_BASH, "-c", script], cwd=tmp_path, env=shell_env, capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    calls = (tmp_path / "wrapper-args").read_text(encoding="utf-8").splitlines()
    assert calls[0] == "reset --soft experiment-sha"
    assert "check_iteration_scope.py --state-json .experiments/state/guard-run/state.json" in calls[1]


@pytest.mark.integration
@_shell_available
def test_rework_without_a_saved_sha_stops_before_folding(tmp_path: Path, shell_env: dict[str, str]) -> None:
    """Missing rework state cannot reset to an improvised SHA or invoke an unchecked amend path."""
    block = _block("SKILL.md", "#### Phase 7 —", "#### Phase 7a", "check_iteration_scope.py")
    script = (
        'git() { printf "git-called\\n" >> "$CAPTURE"; }\n'
        'python() { printf "scope-called\\n" >> "$CAPTURE"; }\n' + block
    )

    result = subprocess.run(
        [_BASH, "-c", script], cwd=tmp_path, env=shell_env, capture_output=True, text=True, check=False
    )

    assert result.returncode == 1
    assert "! BLOCKED" in result.stdout
    assert not (tmp_path / "wrapper-args").exists()


@pytest.mark.integration
@_shell_available
def test_rework_fold_failure_stops_before_scope_check(tmp_path: Path, shell_env: dict[str, str]) -> None:
    """A failed fold cannot hide committed agent edits from the porcelain scope check."""
    (tmp_path / "research-run-rework-sha-fresh-guard").write_text("experiment-sha\n", encoding="utf-8")
    block = _block("SKILL.md", "#### Phase 7 —", "#### Phase 7a", "check_iteration_scope.py")
    script = (
        'git() { case "$1" in rev-parse) printf "agent-sha\\n" ;; reset) return 1 ;; esac; }\n'
        'python() { printf "scope-called\\n" > "$CAPTURE"; }\n' + block
    )

    result = subprocess.run(
        [_BASH, "-c", script], cwd=tmp_path, env=shell_env, capture_output=True, text=True, check=False
    )

    assert result.returncode == 1
    assert not (tmp_path / "wrapper-args").exists()

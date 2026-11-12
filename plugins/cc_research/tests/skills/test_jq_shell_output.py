"""Exercise captured research state values with native jq's Windows newline behavior."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parents[2]
_BASH = shutil.which("bash")
_JQ = shutil.which("jq")
_shell_available = pytest.mark.skipif(not _BASH or not _JQ, reason="shell recipes require Bash and jq")


def _recipe(relative: str, marker: str) -> list[str]:
    """Extract the executable Bash fence containing a state lookup."""
    current: list[str] | None = None
    for line in (_PLUGIN / "skills" / relative).read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped == "```bash":
            current = []
        elif stripped == "```" and current is not None:
            if any(marker in item for item in current):
                return current
            current = None
        elif current is not None:
            current.append(line)
    raise AssertionError(f"No Bash recipe contains {marker!r}")


@pytest.fixture
def native_jq(tmp_path: Path) -> tuple[str, dict[str, str]]:
    """Wrap real jq and emulate native Windows stdout translation unless binary mode is requested."""
    wrapper = tmp_path / "native_jq.py"
    wrapper.write_text(
        "import os, subprocess, sys\n"
        "args = sys.argv[1:]\n"
        "result = subprocess.run([os.environ['REAL_JQ'], *args], capture_output=True)\n"
        "output = result.stdout\n"
        "if '-b' not in args and '--binary' not in args:\n"
        "    output = output.replace(b'\\r\\n', b'\\n').replace(b'\\n', b'\\r\\n')\n"
        "sys.stdout.buffer.write(output)\n"
        "sys.stderr.buffer.write(result.stderr)\n"
        "sys.exit(result.returncode)\n",
        encoding="utf-8",
        newline="\n",
    )
    env = os.environ.copy()
    env.update(REAL_JQ=str(_JQ), TMPDIR=tmp_path.as_posix(), CLAUDE_CODE_SESSION_ID="jq-output")
    prefix = f'jq() {{ {shlex.quote(Path(sys.executable).as_posix())} {shlex.quote(wrapper.as_posix())} "$@"; }}\n'
    return prefix, env


@pytest.mark.integration
@_shell_available
@pytest.mark.parametrize(
    ("compute", "timeout", "expected"),
    [
        pytest.param("colab", None, b"colab|300|300000", id="colab-default"),
        pytest.param("local", 123, b"local|123|120000", id="local-saved-timeout"),
    ],
)
def test_timeout_initialization_accepts_native_jq_values(
    tmp_path: Path, native_jq: tuple[str, dict[str, str]], compute: str, timeout: int | None, expected: bytes
) -> None:
    """LF sentinels plus native jq output preserve routing and positive timeout validation."""
    state_dir = tmp_path / ".experiments/state/native-output"
    state_dir.mkdir(parents=True)
    (state_dir / "state.json").write_text(
        json.dumps({"config": {"compute": compute, "metric_timeout_sec": timeout}}), encoding="utf-8", newline="\n"
    )
    (tmp_path / "research-run-id-jq-output").write_bytes(b"native-output\n")
    prefix, env = native_jq
    env.pop("VERIFY_TIMEOUT_SEC", None)
    lines = _recipe("run/SKILL.md", "_METRIC_TIMEOUT=$(jq")
    script = prefix + "\n".join(lines) + '\nprintf "%s|%s|%s" "$compute" "$VERIFY_TIMEOUT_SEC" "$GUARD_TIMEOUT_MS"'
    result = subprocess.run([_BASH, "-c", script], cwd=tmp_path, env=env, capture_output=True, check=False)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert result.stdout.splitlines()[-1] == expected


@pytest.mark.integration
@_shell_available
@pytest.mark.parametrize(
    ("relative", "variable", "key"),
    [
        pytest.param("run/SKILL.md", "GUARD_CMD", "guard_cmd", id="precondition-guard"),
        pytest.param("run/modes/phase5-metric.md", "METRIC_CMD", "metric_cmd", id="docker-metric"),
        pytest.param("fortify/SKILL.md", "METRIC_CMD", "metric_cmd", id="fortify-metric"),
        pytest.param("fortify/SKILL.md", "GUARD_CMD", "guard_cmd", id="fortify-guard"),
    ],
)
@pytest.mark.parametrize("value", ["python check.py --label 'native output'", "", None, "<missing>"])
def test_captured_commands_preserve_native_jq_output(
    tmp_path: Path, native_jq: tuple[str, dict[str, str]], relative: str, variable: str, key: str, value: str | None
) -> None:
    """Commands retain exact bytes while empty and missing JSON values stay empty."""
    state_dir = tmp_path / ".experiments/state/native-output"
    state_dir.mkdir(parents=True)
    state = state_dir / "state.json"
    config = {} if value == "<missing>" else {key: value}
    state.write_text(json.dumps({"config": config}), encoding="utf-8", newline="\n")
    line = next(item for item in _recipe(relative, f".config.{key} // empty") if item.startswith(f"{variable}="))
    prefix, env = native_jq
    env.pop(variable, None)
    script = prefix + f"RUN_ID=native-output\nSTATE_JSON={shlex.quote(state.as_posix())}\n" + line
    script += f'\nprintf "%s" "${variable}"'
    result = subprocess.run([_BASH, "-c", script], cwd=tmp_path, env=env, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ("" if value == "<missing>" else value or "").encode()


@pytest.mark.integration
@_shell_available
def test_unreadable_state_still_stops_command_capture(tmp_path: Path, native_jq: tuple[str, dict[str, str]]) -> None:
    """The binary-output option preserves jq failure propagation before a command can run."""
    line = next(item for item in _recipe("run/SKILL.md", ".config.guard_cmd // empty") if item.startswith("GUARD_CMD="))
    prefix, env = native_jq
    result = subprocess.run(
        [_BASH, "-c", prefix + "RUN_ID=absent\n" + line + "\nprintf reached"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
    assert b"state.json unreadable" in result.stdout
    assert b"reached" not in result.stdout

"""Tests for ``docker_sandbox_run.py``.

Covers:
    - ``build_explore_command`` / ``build_verify_command`` argv shape (pure functions).
    - ``main()``: mode dispatch, ``SANDBOX_NETWORK`` override, bad-arg exit 2, return-code forwarding,
      missing-docker handling, ``SANDBOX_TIMEOUT_SEC`` cap and the post-timeout container kill.
    - Resource quotas: default ``--cpus``/``--memory``/``--pids-limit`` in both modes, their
      ``SANDBOX_*`` overrides, and the fallback that keeps a malformed override from disabling them.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

import docker_sandbox_run as ds


# ---------- Pure builders ----------


def test_build_explore_command_strips_leading_dotslash() -> None:
    cmd = ds.build_explore_command("./scripts/x.py", "none", "/proj")
    assert cmd[-1] == "/workspace/scripts/x.py"
    assert cmd[-2] == "python"
    assert cmd[0:3] == ["docker", "run", "--rm"]
    assert "--network" in cmd
    assert "none" in cmd
    assert "/proj:/workspace:ro" in cmd
    assert "--tmpfs" in cmd


def test_build_explore_command_preserves_path_without_dotslash() -> None:
    cmd = ds.build_explore_command("scripts/x.py", "none", "/proj")
    assert cmd[-1] == "/workspace/scripts/x.py"


def test_build_verify_command_mounts_experiments_rw() -> None:
    cmd = ds.build_verify_command("pytest -q", "bridge", "/proj")
    # Must contain both read-only project mount and read-write .experiments mount.
    assert "/proj:/workspace:ro" in cmd
    assert "/proj/.experiments:/workspace/.experiments:rw" in cmd
    # Command suffix: sh -c <metric-cmd>
    assert cmd[-3:] == ["sh", "-c", "pytest -q"]


def test_build_verify_command_network_override() -> None:
    cmd = ds.build_verify_command("echo hi", "host", "/proj")
    idx = cmd.index("--network")
    assert cmd[idx + 1] == "host"


# ---------- Resource quotas ----------


@pytest.mark.parametrize(
    "builder",
    [pytest.param(ds.build_explore_command, id="explore"), pytest.param(ds.build_verify_command, id="verify")],
)
def test_builders_carry_default_resource_quotas(builder: Any) -> None:
    """Both modes cap CPU, memory and process count, not just wall-clock time.

    The wall-clock backstop only reaps a container at the deadline; without these quotas a runaway command may saturate
    the host for the whole timeout window. Explore mode is not exempt — an exploratory script can wedge a laptop exactly
    like a metric command.
    """
    cmd = builder("x.py", "none", "/proj")
    assert cmd[cmd.index("--cpus") + 1] == "2"
    assert cmd[cmd.index("--memory") + 1] == "2g"
    assert cmd[cmd.index("--pids-limit") + 1] == "512"


@pytest.mark.parametrize(
    "builder",
    [pytest.param(ds.build_explore_command, id="explore"), pytest.param(ds.build_verify_command, id="verify")],
)
def test_builders_render_supplied_limits(builder: Any) -> None:
    """Explicit ``SandboxLimits`` reach the argv of both builders instead of the defaults."""
    limits = ds.SandboxLimits(cpus=0.5, memory="256m", pids=64)
    cmd = builder("x.py", "none", "/proj", limits=limits)
    assert cmd[cmd.index("--cpus") + 1] == "0.5"
    assert cmd[cmd.index("--memory") + 1] == "256m"
    assert cmd[cmd.index("--pids-limit") + 1] == "64"


@pytest.mark.parametrize(
    "env,expected",
    [
        pytest.param({}, ds.SandboxLimits(), id="unset-uses-defaults"),
        pytest.param({"SANDBOX_CPUS": ""}, ds.SandboxLimits(), id="empty-uses-defaults"),
        pytest.param({"SANDBOX_CPUS": "1.5"}, ds.SandboxLimits(cpus=1.5), id="cpus-override"),
        pytest.param({"SANDBOX_MEMORY": "512m"}, ds.SandboxLimits(memory="512m"), id="memory-override"),
        pytest.param({"SANDBOX_PIDS_LIMIT": "64"}, ds.SandboxLimits(pids=64), id="pids-override"),
        pytest.param({"SANDBOX_CPUS": "bogus"}, ds.SandboxLimits(), id="cpus-malformed-falls-back"),
        pytest.param({"SANDBOX_CPUS": "-1"}, ds.SandboxLimits(), id="cpus-negative-falls-back"),
        pytest.param({"SANDBOX_MEMORY": "2 gigs"}, ds.SandboxLimits(), id="memory-malformed-falls-back"),
        pytest.param({"SANDBOX_MEMORY": "0g"}, ds.SandboxLimits(), id="memory-zero-falls-back"),
        pytest.param({"SANDBOX_PIDS_LIMIT": "0"}, ds.SandboxLimits(), id="pids-zero-falls-back"),
        pytest.param({"SANDBOX_PIDS_LIMIT": "0.5"}, ds.SandboxLimits(), id="pids-truncating-to-zero-falls-back"),
    ],
)
def test_resolve_limits_falls_back_instead_of_raising(env: dict[str, str], expected: ds.SandboxLimits) -> None:
    """A malformed or non-positive override falls back to the default rather than raising.

    These quotas are containment backstops: a typo in the environment must not be able to
    disable one, and must not abort the run either — the same contract ``_resolve_timeout`` has.
    """
    assert ds._resolve_limits(env) == expected


# ---------- Destructive-token guard ----------


@pytest.mark.parametrize(
    "arg",
    ["pytest -q metric.py", "python -m eval", "echo done", "./run_metric.sh", "pytest_rm_module -q"],
)
def test_find_destructive_tokens_allows_benign_commands(arg: str) -> None:
    """Legitimate metric commands contain no destructive whole-word tokens."""
    assert ds.find_destructive_tokens(arg) == []


@pytest.mark.parametrize(
    "arg,expected",
    [
        pytest.param("rm -rf /workspace/.experiments/state", ["rm"], id="rm"),
        pytest.param("dd of=/workspace/.experiments/x", ["dd"], id="dd"),
        pytest.param("truncate -s 0 log", ["truncate"], id="truncate"),
        pytest.param("shred file", ["shred"], id="shred"),
        pytest.param("mv a b", ["mv"], id="mv"),
    ],
)
def test_find_destructive_tokens_flags_destructive_commands(arg: str, expected: list[str]) -> None:
    """Bare destructive binaries are detected even without shell metacharacters."""
    assert ds.find_destructive_tokens(arg) == expected


# ---------- main() ----------


class _FakeCompleted:
    """Minimal completed-process stand-in for Docker command tests."""

    def __init__(self, returncode: int = 0) -> None:
        """Store the Docker command status exposed to the wrapper."""
        self.returncode = returncode


def _split_cidfile(argv: list[str]) -> tuple[list[str], str]:
    """Split a docker argv into its command arguments and cidfile path.

    Examples:
        >>> _split_cidfile(["docker", "run", "--cidfile", "/tmp/job.cid", "image"])
        (['docker', 'run', 'image'], '/tmp/job.cid')
    """
    i = argv.index("--cidfile")
    return argv[:i] + argv[i + 2 :], argv[i + 1]


@pytest.fixture(name="captured_run")
def _captured_run(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Capture ``subprocess.run`` invocations; return a list filled on each call."""
    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], **_kw: Any) -> _FakeCompleted:
        """Record Docker argv and return a successful process result."""
        calls.append(cmd)
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(ds.subprocess, "run", _fake_run)
    return calls


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_exits_0_without_docker(monkeypatch: pytest.MonkeyPatch, flag: str) -> None:
    """Print help without starting Docker or another subprocess."""
    called: list[Any] = []
    monkeypatch.setattr(ds.subprocess, "run", lambda *a, **k: called.append(a))
    with pytest.raises(SystemExit) as exc:
        ds.main([flag])
    assert exc.value.code == 0
    assert called == []


def test_golden_explore_invocation_constructs_expected_docker_argv(captured_run: list[list[str]]) -> None:
    """Exact compute-docker.md explore call-site argv → pre-argparse baseline plus the timeout kill handle."""
    rc = ds.main(
        ["--mode", "explore", ".experiments/state/RID/scripts/probe.py"],
        env={},
        cwd="/proj",
    )
    assert rc == 0
    assert len(captured_run) == 1
    argv, cidfile = _split_cidfile(captured_run[0])
    assert cidfile.endswith(".cid")
    assert argv == [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--read-only",
        "--cpus",
        "2",
        "--memory",
        "2g",
        "--pids-limit",
        "512",
        "-v",
        "/proj:/workspace:ro",
        "--tmpfs",
        ds.TMPFS_MOUNT,
        "-w",
        "/workspace",
        ds.IMAGE,
        "python",
        "/workspace/.experiments/state/RID/scripts/probe.py",
    ]


def test_golden_verify_invocation_constructs_expected_docker_argv(captured_run: list[list[str]]) -> None:
    """Exact phase5-metric.md verify call-site argv → pre-argparse baseline plus the timeout kill handle."""
    rc = ds.main(["--mode", "verify", "pytest -q metric.py"], env={}, cwd="/proj")
    assert rc == 0
    assert len(captured_run) == 1
    argv, cidfile = _split_cidfile(captured_run[0])
    assert cidfile.endswith(".cid")
    assert argv == [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--read-only",
        "--cpus",
        "2",
        "--memory",
        "2g",
        "--pids-limit",
        "512",
        "-v",
        "/proj:/workspace:ro",
        "-v",
        "/proj/.experiments:/workspace/.experiments:rw",
        "--tmpfs",
        ds.TMPFS_MOUNT,
        "-w",
        "/workspace",
        ds.IMAGE,
        "sh",
        "-c",
        "pytest -q metric.py",
    ]


def test_network_host_guard_rejects_before_docker(
    captured_run: list[list[str]], capsys: pytest.CaptureFixture[str]
) -> None:
    """Reject host networking before starting a container."""
    rc = ds.main(["--mode", "explore", "x.py"], env={"SANDBOX_NETWORK": "host"}, cwd="/proj")
    assert rc == 2
    assert "SANDBOX_NETWORK" in capsys.readouterr().err
    assert captured_run == []


@pytest.mark.parametrize("network", ["none", "bridge", "internal"])
def test_network_host_guard_still_allows_isolated_modes(captured_run: list[list[str]], network: str) -> None:
    """The allowlisted isolated network modes still reach Docker argv unchanged."""
    rc = ds.main(["--mode", "explore", "x.py"], env={"SANDBOX_NETWORK": network}, cwd="/proj")
    assert rc == 0
    cmd = captured_run[0]
    assert cmd[cmd.index("--network") + 1] == network


def test_main_missing_mode_returns_2(capsys: pytest.CaptureFixture[str]) -> None:
    rc = ds.main(["script.py"])
    assert rc == 2
    assert "usage:" in capsys.readouterr().err


def test_main_missing_arg_returns_2(capsys: pytest.CaptureFixture[str]) -> None:
    rc = ds.main(["--mode", "explore"])
    assert rc == 2
    assert "usage:" in capsys.readouterr().err


def test_main_explore_mode_dispatches_correctly(captured_run: list[list[str]]) -> None:
    rc = ds.main(["--mode", "explore", "./scripts/x.py"], env={}, cwd="/proj")
    assert rc == 0
    assert len(captured_run) == 1
    cmd = captured_run[0]
    assert cmd[0:3] == ["docker", "run", "--rm"]
    assert cmd[-2:] == ["python", "/workspace/scripts/x.py"]
    # Default network = "none"
    assert "none" in cmd


def test_main_explore_mode_equals_form(captured_run: list[list[str]]) -> None:
    rc = ds.main(["--mode=explore", "./scripts/x.py"], env={}, cwd="/proj")
    assert rc == 0
    assert captured_run[0][-1] == "/workspace/scripts/x.py"


def test_main_verify_mode_dispatches_correctly(captured_run: list[list[str]]) -> None:
    rc = ds.main(["--mode", "verify", "pytest -q"], env={}, cwd="/proj")
    assert rc == 0
    cmd = captured_run[0]
    assert cmd[-3:] == ["sh", "-c", "pytest -q"]
    assert "/proj/.experiments:/workspace/.experiments:rw" in cmd


def test_main_verify_rejects_destructive_command_before_docker(
    captured_run: list[list[str]], capsys: pytest.CaptureFixture[str]
) -> None:
    """A bare ``rm`` in verify mode wipes the rw .experiments mount → exit 2, no docker."""
    rc = ds.main(["--mode", "verify", "rm -rf /workspace/.experiments/state"], env={}, cwd="/proj")
    assert rc == 2
    assert "destructive binaries" in capsys.readouterr().err
    assert captured_run == []


@pytest.mark.parametrize("network", ["host", "container:abc", "service:name", "unknown", " host "])
def test_main_sandbox_network_rejected(
    captured_run: list[list[str]], capsys: pytest.CaptureFixture[str], network: str
) -> None:
    """Unsafe or unknown ``SANDBOX_NETWORK`` values must be rejected before docker runs."""
    rc = ds.main(["--mode", "explore", "x.py"], env={"SANDBOX_NETWORK": network}, cwd="/proj")
    assert rc == 2
    assert "SANDBOX_NETWORK" in capsys.readouterr().err
    assert captured_run == []


@pytest.mark.parametrize("network", ["none", "bridge", "internal"])
def test_main_sandbox_network_allowlist(captured_run: list[list[str]], network: str) -> None:
    """Only isolated network modes in the allowlist reach docker argv."""
    rc = ds.main(["--mode", "explore", "x.py"], env={"SANDBOX_NETWORK": network}, cwd="/proj")
    assert rc == 0
    cmd = captured_run[0]
    idx = cmd.index("--network")
    assert cmd[idx + 1] == network


def test_main_sandbox_network_empty_falls_back_to_none(captured_run: list[list[str]]) -> None:
    rc = ds.main(["--mode", "explore", "x.py"], env={"SANDBOX_NETWORK": ""}, cwd="/proj")
    assert rc == 0
    cmd = captured_run[0]
    idx = cmd.index("--network")
    assert cmd[idx + 1] == "none"


def test_main_forwards_docker_return_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ds.subprocess, "run", lambda cmd, **_kw: _FakeCompleted(returncode=42))
    rc = ds.main(["--mode", "explore", "x.py"], env={}, cwd="/proj")
    assert rc == 42


def test_main_docker_not_in_path(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def _raise_fnf(*_a: Any, **_kw: Any) -> Any:
        """Model a host where Docker is unavailable on ``PATH``."""
        raise FileNotFoundError("docker")

    monkeypatch.setattr(ds.subprocess, "run", _raise_fnf)
    rc = ds.main(["--mode", "explore", "x.py"], env={}, cwd="/proj")
    assert rc == 127
    assert "'docker' binary not found" in capsys.readouterr().err


# ---------- Timeout backstop ----------


@pytest.mark.parametrize(
    "env,expected",
    [
        pytest.param({}, 600.0, id="default-backstop"),
        pytest.param({"SANDBOX_TIMEOUT_SEC": "45"}, 45.0, id="env-override"),
        pytest.param({"SANDBOX_TIMEOUT_SEC": "bogus"}, 600.0, id="malformed-falls-back"),
    ],
)
def test_main_caps_docker_run_with_resolved_timeout(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], expected: float
) -> None:
    """Every docker run carries a wall-clock cap; ``SANDBOX_TIMEOUT_SEC`` overrides the 600s default."""
    seen: dict[str, Any] = {}

    def _fake_run(_cmd: list[str], **kw: Any) -> _FakeCompleted:
        """Record timeout kwargs while returning a completed Docker process."""
        seen.update(kw)
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(ds.subprocess, "run", _fake_run)
    rc = ds.main(["--mode", "explore", "x.py"], env=env, cwd="/proj")
    assert rc == 0
    assert seen["timeout"] == expected


def test_main_timeout_kills_container_and_returns_124(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A hung container is killed via its cidfile, the cidfile removed, and exit 124 returned."""
    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], **kw: Any) -> _FakeCompleted:
        """Write a container id before raising the timeout used by cleanup."""
        calls.append(cmd)
        if cmd[1] == "run":
            # Mirror docker: the id lands in the cidfile before the client is killed off.
            Path(cmd[cmd.index("--cidfile") + 1]).write_text("c0ffee\n", encoding="utf-8")
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(ds.subprocess, "run", _fake_run)
    rc = ds.main(["--mode", "verify", "pytest -q"], env={"SANDBOX_TIMEOUT_SEC": "1"}, cwd="/proj")
    assert rc == 124
    assert calls[1] == ["docker", "kill", "c0ffee"]
    assert not Path(calls[0][calls[0].index("--cidfile") + 1]).exists()
    assert "exceeded 1s timeout" in capsys.readouterr().err


def test_main_timeout_without_cidfile_content_still_returns_124(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Timing out before docker wrote any container id must not crash the kill path."""
    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], **kw: Any) -> _FakeCompleted:
        """Raise Docker's timeout exception for the cleanup failure path."""
        calls.append(cmd)
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    monkeypatch.setattr(ds.subprocess, "run", _fake_run)
    rc = ds.main(["--mode", "explore", "x.py"], env={"SANDBOX_TIMEOUT_SEC": "2"}, cwd="/proj")
    assert rc == 124
    assert len(calls) == 1
    assert "killing container" in capsys.readouterr().err


@pytest.mark.parametrize("mode", ["explore", "verify"])
def test_main_applies_quota_env_overrides(captured_run: list[list[str]], mode: str) -> None:
    """The ``SANDBOX_*`` quota overrides reach the docker argv in both modes.

    A caller running several sandboxes at once needs to shrink each one's share; this is the path that lets them, and it
    must work identically for explore and verify.
    """
    env = {"SANDBOX_CPUS": "1", "SANDBOX_MEMORY": "512m", "SANDBOX_PIDS_LIMIT": "128"}
    rc = ds.main(["--mode", mode, "x.py"], env=env, cwd="/proj")
    assert rc == 0
    cmd = captured_run[0]
    assert cmd[cmd.index("--cpus") + 1] == "1"
    assert cmd[cmd.index("--memory") + 1] == "512m"
    assert cmd[cmd.index("--pids-limit") + 1] == "128"

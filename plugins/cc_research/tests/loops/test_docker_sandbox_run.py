"""Tests for ``docker_sandbox_run.py``.

Covers:
    - ``build_explore_command`` / ``build_verify_command`` argv shape (pure functions).
    - ``main()``: mode dispatch, ``SANDBOX_NETWORK`` override, bad-arg exit 2, return-code forwarding,
      missing-docker handling, ``SANDBOX_TIMEOUT_SEC`` cap and the post-timeout container kill.
    - Resource quotas: default ``--cpus``/``--memory``/``--pids-limit`` in both modes, their
      ``SANDBOX_*`` overrides, and the fallback that keeps a malformed override from disabling them.

Docker is never started: ``subprocess.run`` is mocked at the boundary the wrapper calls it through.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

import docker_sandbox_run as ds
import pytest

_RUN = "docker_sandbox_run.subprocess.run"
_BUILDERS = [pytest.param(ds.build_explore_command, id="explore"), pytest.param(ds.build_verify_command, id="verify")]


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


def _hardening_prefix() -> list[str]:
    """Return the argv every sandbox run starts with: isolation flags, default quotas and the host user mapping."""
    user = ["--user", f"{os.getuid()}:{os.getgid()}"] if hasattr(os, "getuid") else []
    return [
        *["docker", "run", "--rm", "--network", "none", "--cap-drop=ALL", "--security-opt=no-new-privileges"],
        *["--read-only", "--cpus", "2", "--memory", "2g", "--pids-limit", "512", *user],
    ]


def _timeout_after_writing_cid(cmd: list[str], **kwargs: Any) -> _FakeCompleted:
    """Mirror docker: the container id lands in the cidfile before the client is killed off by the timeout."""
    if cmd[1] == "run":
        Path(cmd[cmd.index("--cidfile") + 1]).write_text("c0ffee\n", encoding="utf-8")
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
    return _FakeCompleted(returncode=0)


def _timeout_without_cid(cmd: list[str], **kwargs: Any) -> _FakeCompleted:
    """Raise the wall-clock timeout before docker wrote any container id."""
    raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])


@pytest.fixture(name="captured_run")
def _captured_run() -> Iterator[list[list[str]]]:
    """Mock ``subprocess.run`` as the wrapper sees it; yield the argv list recorded for each call."""
    calls: list[list[str]] = []
    with mock.patch(_RUN, autospec=True) as mock_run:
        mock_run.side_effect = lambda cmd, **_kw: calls.append(cmd) or _FakeCompleted(returncode=0)
        yield calls


class TestBuilders:
    """Pure argv builders for the two sandbox modes."""

    def test_explore_argv_shape(self) -> None:
        """Explore mode runs the script with ``python`` under a read-only project mount, tmpfs and the chosen network.

        The script is the final argument and the project is bind-mounted read-only at ``/workspace``.
        """
        cmd = ds.build_explore_command("./scripts/x.py", "none", "/proj")

        assert cmd[-2] == "python"
        assert cmd[0:3] == ["docker", "run", "--rm"]
        assert "--network" in cmd
        assert "none" in cmd
        assert f"type=bind,source={Path('/proj').resolve()},target=/workspace,readonly" in cmd
        assert "--tmpfs" in cmd

    @pytest.mark.parametrize(
        "script", [pytest.param("./scripts/x.py", id="leading-dotslash"), pytest.param("scripts/x.py", id="plain")]
    )
    def test_explore_script_path_is_workspace_relative(self, script: str) -> None:
        """A leading ``./`` is stripped so both spellings resolve to the same path inside the container."""
        assert ds.build_explore_command(script, "none", "/proj")[-1] == "/workspace/scripts/x.py"

    def test_verify_mounts_experiments_read_write(self) -> None:
        """Verify mode keeps the project read-only but mounts ``.experiments`` writable and ends with ``sh -c <cmd>``.

        The metric command may write run artifacts, so only that one directory is read-write.
        """
        cmd = ds.build_verify_command("pytest -q", "bridge", "/proj")

        assert f"type=bind,source={Path('/proj').resolve()},target=/workspace,readonly" in cmd
        assert f"type=bind,source={Path('/proj').resolve() / '.experiments'},target=/workspace/.experiments" in cmd
        assert cmd[-3:] == ["sh", "-c", "pytest -q"]

    def test_verify_network_override(self) -> None:
        """The network argument reaches ``--network`` in the verify argv."""
        cmd = ds.build_verify_command("echo hi", "host", "/proj")

        assert cmd[cmd.index("--network") + 1] == "host"


class TestResourceQuotas:
    """CPU, memory and process-count caps carried by every container."""

    @pytest.mark.parametrize("builder", _BUILDERS)
    def test_builders_carry_default_quotas(self, builder: Any) -> None:
        """Both modes cap CPU, memory and process count, not just wall-clock time.

        The wall-clock backstop only reaps a container at the deadline; without these quotas a runaway command may
        saturate the host for the whole timeout window. Explore mode is not exempt — an exploratory script can wedge a
        laptop exactly like a metric command.
        """
        cmd = builder("x.py", "none", "/proj")

        assert cmd[cmd.index("--cpus") + 1] == "2"
        assert cmd[cmd.index("--memory") + 1] == "2g"
        assert cmd[cmd.index("--pids-limit") + 1] == "512"

    @pytest.mark.parametrize("builder", _BUILDERS)
    def test_builders_render_supplied_limits(self, builder: Any) -> None:
        """Explicit ``SandboxLimits`` reach the argv of both builders instead of the defaults."""
        cmd = builder("x.py", "none", "/proj", limits=ds.SandboxLimits(cpus=0.5, memory="256m", pids=64))

        assert cmd[cmd.index("--cpus") + 1] == "0.5"
        assert cmd[cmd.index("--memory") + 1] == "256m"
        assert cmd[cmd.index("--pids-limit") + 1] == "64"

    @pytest.mark.parametrize(
        ("env", "expected"),
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
    def test_resolve_limits_falls_back_instead_of_raising(
        self, env: dict[str, str], expected: ds.SandboxLimits
    ) -> None:
        """A malformed or non-positive override falls back to the default rather than raising.

        These quotas are containment backstops: a typo in the environment must not be able to disable one, and must not
        abort the run either — the same contract ``_resolve_timeout`` has.
        """
        assert ds._resolve_limits(env) == expected

    @pytest.mark.parametrize("mode", ["explore", "verify"])
    def test_main_applies_env_overrides(self, captured_run: list[list[str]], mode: str, tmp_path: Path) -> None:
        """The ``SANDBOX_*`` quota overrides reach the docker argv in both modes.

        A caller running several sandboxes at once needs to shrink each one's share; this is the path that lets them,
        and it must work identically for explore and verify.
        """
        env = {"SANDBOX_CPUS": "1", "SANDBOX_MEMORY": "512m", "SANDBOX_PIDS_LIMIT": "128"}

        assert ds.main(["--mode", mode, "x.py"], env=env, cwd=str(tmp_path)) == 0

        cmd = captured_run[0]
        assert cmd[cmd.index("--cpus") + 1] == "1"
        assert cmd[cmd.index("--memory") + 1] == "512m"
        assert cmd[cmd.index("--pids-limit") + 1] == "128"


class TestDestructiveTokens:
    """Whole-word guard against destructive binaries in a verify command."""

    @pytest.mark.parametrize(
        "arg", ["pytest -q metric.py", "python -m eval", "echo done", "./run_metric.sh", "pytest_rm_module -q"]
    )
    def test_allows_benign_commands(self, arg: str) -> None:
        """Legitimate metric commands contain no destructive whole-word tokens.

        Names that merely contain a destructive word (``pytest_rm_module``) must not be flagged.
        """
        assert ds.find_destructive_tokens(arg) == []

    @pytest.mark.parametrize(
        ("arg", "expected"),
        [
            pytest.param("rm -rf /workspace/.experiments/state", ["rm"], id="rm"),
            pytest.param("dd of=/workspace/.experiments/x", ["dd"], id="dd"),
            pytest.param("truncate -s 0 log", ["truncate"], id="truncate"),
            pytest.param("shred file", ["shred"], id="shred"),
            pytest.param("mv a b", ["mv"], id="mv"),
        ],
    )
    def test_flags_destructive_commands(self, arg: str, expected: list[str]) -> None:
        """Bare destructive binaries are detected even without shell metacharacters."""
        assert ds.find_destructive_tokens(arg) == expected


class TestMain:
    """``main()`` dispatch, validation, return codes and the wall-clock backstop."""

    @pytest.mark.parametrize("flag", ["-h", "--help"])
    @mock.patch(_RUN, autospec=True)
    def test_help_exits_zero_without_docker(self, mock_run: mock.MagicMock, flag: str) -> None:
        """Print help without starting Docker or another subprocess."""
        with pytest.raises(SystemExit) as exc:
            ds.main([flag])

        assert exc.value.code == 0
        mock_run.assert_not_called()

    def test_golden_explore_argv(self, captured_run: list[list[str]]) -> None:
        """Exact compute-docker.md explore call-site argv → pre-argparse baseline plus the timeout kill handle."""
        rc = ds.main(["--mode", "explore", ".experiments/state/RID/scripts/probe.py"], env={}, cwd="/proj")

        assert rc == 0
        assert len(captured_run) == 1
        argv, cidfile = _split_cidfile(captured_run[0])
        assert cidfile.endswith(".cid")
        assert argv == [
            *_hardening_prefix(),
            *["--mount", f"type=bind,source={Path('/proj').resolve()},target=/workspace,readonly"],
            *["--tmpfs", ds.TMPFS_MOUNT, "-w", "/workspace", ds.IMAGE],
            *["python", "/workspace/.experiments/state/RID/scripts/probe.py"],
        ]

    def test_golden_verify_argv(self, captured_run: list[list[str]], tmp_path: Path) -> None:
        """Exact phase5-metric.md verify call-site argv → pre-argparse baseline plus the timeout kill handle."""
        rc = ds.main(["--mode", "verify", "pytest -q metric.py"], env={}, cwd=str(tmp_path))

        assert rc == 0
        assert len(captured_run) == 1
        argv, cidfile = _split_cidfile(captured_run[0])
        assert cidfile.endswith(".cid")
        assert argv == [
            *_hardening_prefix(),
            *["--mount", f"type=bind,source={tmp_path.resolve()},target=/workspace,readonly"],
            *["--mount", f"type=bind,source={tmp_path.resolve() / '.experiments'},target=/workspace/.experiments"],
            *["--tmpfs", ds.TMPFS_MOUNT, "-w", "/workspace", ds.IMAGE],
            *["sh", "-c", "pytest -q metric.py"],
        ]

    @pytest.mark.parametrize(
        "argv",
        [pytest.param(["script.py"], id="missing-mode"), pytest.param(["--mode", "explore"], id="missing-script-arg")],
    )
    def test_missing_argument_returns_2(self, argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
        """A missing ``--mode`` or a missing script/command argument prints usage and returns 2.

        A bare script path without ``--mode``, and ``--mode explore`` without the script to run, are both incomplete
        invocations: the wrapper refuses before starting any container.
        """
        assert ds.main(argv) == 2
        assert "usage:" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param(["--mode", "explore", "./scripts/x.py"], id="spaced-mode"),
            pytest.param(["--mode=explore", "./scripts/x.py"], id="equals-mode"),
        ],
    )
    def test_explore_mode_dispatches(self, captured_run: list[list[str]], argv: list[str]) -> None:
        """Explore mode runs the script under ``python`` with the default ``none`` network, in either flag spelling."""
        assert ds.main(argv, env={}, cwd="/proj") == 0

        assert len(captured_run) == 1
        cmd = captured_run[0]
        assert cmd[0:3] == ["docker", "run", "--rm"]
        assert cmd[-2:] == ["python", "/workspace/scripts/x.py"]
        assert "none" in cmd

    def test_verify_mode_dispatches_and_creates_experiments_dir(
        self, captured_run: list[list[str]], tmp_path: Path
    ) -> None:
        """Verify mode binds ``.experiments`` read-write and creates it first.

        ``--mount type=bind`` refuses a missing source, unlike the ``-v`` form it replaced, so a fresh project without
        ``.experiments`` would fail every verify run.
        """
        assert ds.main(["--mode", "verify", "pytest -q"], env={}, cwd=str(tmp_path)) == 0

        cmd = captured_run[0]
        assert cmd[-3:] == ["sh", "-c", "pytest -q"]
        assert f"type=bind,source={tmp_path.resolve() / '.experiments'},target=/workspace/.experiments" in cmd
        assert (tmp_path / ".experiments").is_dir()

    def test_verify_rejects_destructive_command_before_docker(
        self, captured_run: list[list[str]], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A bare ``rm`` in verify mode wipes the rw .experiments mount → exit 2, no docker."""
        assert ds.main(["--mode", "verify", "rm -rf /workspace/.experiments/state"], env={}, cwd="/proj") == 2

        assert "destructive binaries" in capsys.readouterr().err
        assert captured_run == []

    @pytest.mark.parametrize("network", ["host", "container:abc", "service:name", "unknown", " host "])
    def test_unsafe_network_rejected_before_docker(
        self, captured_run: list[list[str]], capsys: pytest.CaptureFixture[str], network: str
    ) -> None:
        """Unsafe or unknown ``SANDBOX_NETWORK`` values must be rejected before docker runs."""
        assert ds.main(["--mode", "explore", "x.py"], env={"SANDBOX_NETWORK": network}, cwd="/proj") == 2

        assert "SANDBOX_NETWORK" in capsys.readouterr().err
        assert captured_run == []

    @pytest.mark.parametrize(
        ("network", "expected"),
        [
            pytest.param("none", "none", id="none"),
            pytest.param("bridge", "bridge", id="bridge"),
            pytest.param("internal", "internal", id="internal"),
            pytest.param("", "none", id="empty-falls-back-to-none"),
        ],
    )
    def test_network_allowlist(self, captured_run: list[list[str]], network: str, expected: str) -> None:
        """Only isolated network modes in the allowlist reach docker argv; an empty value falls back to ``none``."""
        assert ds.main(["--mode", "explore", "x.py"], env={"SANDBOX_NETWORK": network}, cwd="/proj") == 0

        cmd = captured_run[0]
        assert cmd[cmd.index("--network") + 1] == expected

    @mock.patch(_RUN, autospec=True, return_value=_FakeCompleted(returncode=42))
    def test_forwards_docker_return_code(self, mock_run: mock.MagicMock) -> None:
        """The wrapper exits with the container command's own status."""
        assert ds.main(["--mode", "explore", "x.py"], env={}, cwd="/proj") == 42

    @mock.patch(_RUN, autospec=True, side_effect=FileNotFoundError("docker"))
    def test_docker_not_in_path(self, mock_run: mock.MagicMock, capsys: pytest.CaptureFixture[str]) -> None:
        """A host without Docker on ``PATH`` gets exit 127 and a message naming the missing binary."""
        assert ds.main(["--mode", "explore", "x.py"], env={}, cwd="/proj") == 127
        assert "'docker' binary not found" in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("env", "expected"),
        [
            pytest.param({}, 600.0, id="default-backstop"),
            pytest.param({"SANDBOX_TIMEOUT_SEC": "45"}, 45.0, id="env-override"),
            pytest.param({"SANDBOX_TIMEOUT_SEC": "bogus"}, 600.0, id="malformed-falls-back"),
        ],
    )
    @mock.patch(_RUN, autospec=True, return_value=_FakeCompleted(returncode=0))
    def test_caps_docker_run_with_resolved_timeout(
        self, mock_run: mock.MagicMock, env: dict[str, str], expected: float
    ) -> None:
        """Every docker run carries a wall-clock cap; ``SANDBOX_TIMEOUT_SEC`` overrides the 600s default."""
        assert ds.main(["--mode", "explore", "x.py"], env=env, cwd="/proj") == 0

        assert mock_run.call_args.kwargs["timeout"] == expected

    @mock.patch(_RUN, autospec=True, side_effect=_timeout_after_writing_cid)
    def test_timeout_kills_container_and_returns_124(
        self, mock_run: mock.MagicMock, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        """A hung container is killed via its cidfile, the cidfile removed, and exit 124 returned."""
        rc = ds.main(["--mode", "verify", "pytest -q"], env={"SANDBOX_TIMEOUT_SEC": "1"}, cwd=str(tmp_path))

        run_cmd, kill_cmd = (call.args[0] for call in mock_run.call_args_list)
        assert rc == 124
        assert kill_cmd == ["docker", "kill", "c0ffee"]
        assert not Path(run_cmd[run_cmd.index("--cidfile") + 1]).exists()
        assert "exceeded 1s timeout" in capsys.readouterr().err

    @mock.patch(_RUN, autospec=True, side_effect=_timeout_without_cid)
    def test_timeout_without_cidfile_content_still_returns_124(
        self, mock_run: mock.MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Timing out before docker wrote any container id must not crash the kill path."""
        rc = ds.main(["--mode", "explore", "x.py"], env={"SANDBOX_TIMEOUT_SEC": "2"}, cwd="/proj")

        assert rc == 124
        assert mock_run.call_count == 1
        assert "killing container" in capsys.readouterr().err

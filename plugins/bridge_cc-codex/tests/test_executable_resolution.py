"""Exercise executable lookup without Windows current-directory search on every host.

The Windows cases simulate `sys.platform == "win32"` and a hostile PATH on any runner, using only temporary directories.
Subprocess launches are replaced so no peer binary is ever started.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from unittest import mock

import pytest

BIN_ROOT = Path(__file__).resolve().parents[1] / "bin"
if str(BIN_ROOT) not in sys.path:
    sys.path.insert(0, str(BIN_ROOT))

import bridge_call  # noqa: E402
import bridge_diagnose  # noqa: E402
import bridge_setup  # noqa: E402


def _windows_path(path: Path) -> str:
    """Map a real fixture path onto a fully absolute synthetic Windows drive."""
    return str(PureWindowsPath("C:/", *path.parts[1:]))


@pytest.fixture
def windows_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Simulate a Windows host whose working directory is `tmp_path`; the caller sets PATH and PATHEXT."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bridge_call.sys, "platform", "win32")
    real_isfile = os.path.isfile

    def isfile(candidate: str) -> bool:
        """Translate synthetic Windows candidates at the real filesystem boundary."""
        return real_isfile(Path(tmp_path.anchor, *PureWindowsPath(candidate).parts[1:]))

    monkeypatch.setattr(bridge_call.os.path, "isfile", isfile)
    return tmp_path


@pytest.fixture
def trusted(tmp_path: Path) -> Path:
    """Create the one PATH directory the resolver may trust, next to the working directory."""
    directory = tmp_path / "trusted"
    directory.mkdir()
    return directory


@pytest.mark.parametrize("name", ["claude", "codex", "git", "taskkill", "tasklist"])
def test_windows_lookup_ignores_planted_current_directory_executable(
    windows_host: Path, trusted: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """Lookup uses only PATH directories even when cwd and PATH contain implicit cwd entries.

    A planted `<name>.EXE` in the working directory must lose to the executable in the trusted PATH directory, for every
    tool the bridge launches.
    """
    (windows_host / f"{name}.EXE").touch()
    expected = trusted / f"{name}.EXE"
    expected.touch()
    monkeypatch.setenv("PATH", f';.;.\\;"{_windows_path(trusted)}";;')
    monkeypatch.setenv("PATHEXT", ".EXE;.CMD")

    assert bridge_call._resolve_executable(name) == _windows_path(expected)
    assert bridge_call._resolved_command([name, "--version"]) == [_windows_path(expected), "--version"]


@pytest.mark.parametrize("name", ["codex", "codex.CMD"])
def test_windows_lookup_applies_pathext_in_order(
    windows_host: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """PATHEXT order picks npm shims, and an already supplied executable extension is preserved.

    With both `codex.EXE` and `codex.CMD` present and `.CMD` listed first, a bare name resolves to the shim.
    """
    (windows_host / "codex.EXE").touch()
    expected = windows_host / "codex.CMD"
    expected.touch()
    monkeypatch.setenv("PATH", _windows_path(windows_host))
    monkeypatch.setenv("PATHEXT", ".CMD;.EXE")

    assert bridge_call._resolve_executable(name) == _windows_path(expected)
    with pytest.raises(OSError, match="batch"):
        bridge_call._resolved_command([name, "--version"])


class TestMissingWindowsTool:
    """A peer present only in the working directory stays missing and never reaches a subprocess."""

    @pytest.fixture(autouse=True)
    def cwd_only_peer(self, windows_host: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Plant `claude.EXE` in cwd with a PATH made only of implicit cwd entries."""
        (windows_host / "claude.EXE").touch()
        monkeypatch.setenv("PATH", ";.;.\\;")
        monkeypatch.setenv("PATHEXT", ".EXE")

    @mock.patch("subprocess.run", autospec=True)
    def test_resolver_reports_missing_and_command_raises_os_diagnostic(self, mock_run: mock.MagicMock) -> None:
        """Resolution returns None and building a command raises the OS missing-file error for the bare name.

        Falling back to the bare name would let the OS search the working directory and launch the planted binary.
        """
        assert bridge_call._resolve_executable("claude") is None
        with pytest.raises(FileNotFoundError) as error:
            bridge_call._resolved_command(["claude", "--version"])
        assert str(error.value) == str(FileNotFoundError(2, os.strerror(2), "claude"))
        mock_run.assert_not_called()

    @mock.patch("subprocess.run", autospec=True)
    def test_setup_inspection_reports_missing_prerequisite(self, mock_run: mock.MagicMock) -> None:
        """Setup inspection treats the cwd-only peer as a missing prerequisite instead of probing it."""
        assert bridge_setup._inspect("codex", "claude", "user") == (None, "missing-prerequisite")
        mock_run.assert_not_called()

    @mock.patch("subprocess.run", autospec=True)
    def test_setup_capture_reports_probe_failure(self, mock_run: mock.MagicMock) -> None:
        """A setup probe of the cwd-only peer fails as `probe-failed` without spawning anything."""
        assert bridge_setup._capture(["claude", "--version"]) == (False, "", "probe-failed")
        mock_run.assert_not_called()

    @mock.patch("subprocess.run", autospec=True)
    def test_static_diagnosis_reports_os_missing_file_error(self, mock_run: mock.MagicMock) -> None:
        """The static doctor fails with the OS missing-file text for the cwd-only peer."""
        result = bridge_diagnose._static_result("claude", {"required": ["--help"]})
        assert result["ok"] is False
        assert result["error"] == str(FileNotFoundError(2, os.strerror(2), "claude"))
        mock_run.assert_not_called()


@mock.patch(
    "subprocess.run",
    autospec=True,
    side_effect=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, stdout='{"loggedIn": true}', stderr=""),
)
@pytest.mark.parametrize(
    "launch",
    [
        pytest.param(lambda: bridge_setup._capture(["claude", "--version"])[0] is True, id="setup-probe"),
        pytest.param(lambda: bridge_setup._authenticated("claude") == (True, True), id="authentication-check"),
        pytest.param(
            lambda: bridge_setup._run_configuration([{"argv": ["claude", "plugin", "list"]}]) is True,
            id="configuration-mutation",
        ),
        pytest.param(lambda: bridge_setup._launch_authentication("claude").ok is True, id="login"),
        pytest.param(
            lambda: bridge_diagnose._static_result("claude", {"required": []})["ok"] is True, id="static-doctor"
        ),
    ],
)
def test_windows_setup_and_diagnosis_launch_only_the_path_peer(
    mock_run: mock.MagicMock,
    launch: Callable[[], bool],
    windows_host: Path,
    trusted: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every setup probe, mutation, login and static doctor spawns the absolute PATH peer, never the cwd one.

    A planted `claude.EXE` sits in cwd while another `claude.EXE` lives in the trusted PATH directory; each launch path
    must pass the trusted absolute path as argv[0].
    """
    (windows_host / "claude.EXE").touch()
    peer = trusted / "claude.EXE"
    peer.touch()
    monkeypatch.setenv("PATH", f";.;{_windows_path(trusted)}")
    monkeypatch.setenv("PATHEXT", ".CMD;.EXE")

    assert launch()
    assert [call.args[0][0] for call in mock_run.call_args_list] == [_windows_path(peer)]


@mock.patch(
    "subprocess.Popen",
    autospec=True,
    return_value=SimpleNamespace(returncode=0, communicate=lambda timeout: ("", "")),
)
def test_windows_peer_spawn_uses_the_path_peer_with_child_credentials(
    mock_popen: mock.MagicMock, windows_host: Path, trusted: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolution happens before Popen receives provider credentials and an untrusted working directory.

    The spawned argv must start with the trusted PATH peer while the child still gets the caller's cwd and the provider
    API key in its environment.
    """
    (windows_host / "claude.EXE").touch()
    peer = trusted / "claude.EXE"
    peer.touch()
    monkeypatch.setenv("PATH", f";.;{_windows_path(trusted)}")
    monkeypatch.setenv("PATHEXT", ".EXE")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-provider-key")

    result = bridge_call._spawn_child(["claude", "--version"], windows_host, 1.0)

    assert result.returncode == 0
    mock_popen.assert_called_once()
    assert mock_popen.call_args.args[0] == [_windows_path(peer), "--version"]
    assert mock_popen.call_args.kwargs["cwd"] == windows_host
    assert mock_popen.call_args.kwargs["env"]["ANTHROPIC_API_KEY"] == "test-provider-key"


class TestPosixLookup:
    """POSIX lookup uses absolute PATH entries and checks executable permissions."""

    @pytest.fixture(autouse=True)
    def posix_host(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Simulate a POSIX host with ignored cwd entries and one absolute trusted PATH directory."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(bridge_call.sys, "platform", "linux")
        monkeypatch.setenv("PATH", os.pathsep.join(("", ".", "relative", str(tmp_path / "trusted"))))
        (tmp_path / "trusted").mkdir()

    def test_returns_absolute_executable_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """An absolute PATH entry returns its executable without searching cwd entries."""
        executable = tmp_path / "trusted" / "peer"
        executable.touch()
        executable.chmod(0o755)
        monkeypatch.setattr(bridge_call.os, "name", "posix")

        assert bridge_call._resolve_executable("peer") == str(executable)

    @mock.patch("bridge_call.os.access", autospec=True, return_value=False)
    def test_ignores_non_executable_file(
        self, mock_access: mock.MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A file the OS reports as not executable is not returned as the peer."""
        (tmp_path / "trusted" / "peer").touch()
        monkeypatch.setattr(bridge_call.os, "name", "posix")

        assert bridge_call._resolve_executable("peer") is None
        mock_access.assert_called()

"""Tests for ``bin/resolve_preflight.py``.

``subprocess.run`` and ``which`` monkeypatched — no real tools invoked. ``monkeypatch.chdir`` places the
``.temp/state/preflight/`` TTL cache under ``tmp_path`` (the script uses a relative path).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import resolve_preflight as rp


class _FakeCompleted:
    """Minimal stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        """Store the status and streams consumed by the preflight runner."""
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _dispatch(responses: dict[str, tuple[int, str]]) -> Any:
    """Build a subprocess.run fake dispatching on the binary name + first subcommand."""

    def _fake_run(cmd: list[str], **_kwargs: Any) -> _FakeCompleted:
        """Dispatch a fake response by executable and first subcommand."""
        binary = Path(cmd[0]).name
        subcmd = cmd[1] if len(cmd) > 1 else ""
        key = f"{binary} {subcmd}".strip()
        for pattern, (rc, out) in responses.items():
            if pattern in key:
                return _FakeCompleted(returncode=rc, stdout=out)
        return _FakeCompleted(returncode=0, stdout="")

    return _fake_run


def test_gh_not_found_exits_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No gh on PATH → exit 1 with 'gh not found' on stderr."""
    monkeypatch.setattr(rp, "which", lambda cmd: None if cmd == "gh" else "/fake/" + cmd)
    monkeypatch.setattr(rp.subprocess, "run", _dispatch({}))
    monkeypatch.chdir(tmp_path)
    rc = rp.main([])
    assert rc == 1
    assert "gh not found" in capsys.readouterr().err


def test_gh_unauthenticated_exits_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Gh found but auth fails → exit 1 with 'gh found but not authenticated'."""
    monkeypatch.setattr(rp, "which", lambda cmd: "/fake/" + cmd)
    monkeypatch.setattr(
        rp.subprocess,
        "run",
        _dispatch(
            {
                "gh auth": (1, ""),
                "claude plugin": (0, ""),
                "git remote": (0, ""),
                "git rev-parse": (1, ""),
            }
        ),
    )
    monkeypatch.chdir(tmp_path)
    rc = rp.main([])
    assert rc == 1
    assert "gh found but not authenticated" in capsys.readouterr().err


def test_gh_ok_codex_absent_exits_0(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Authenticated gh, codex absent → exit 0, CODEX_AVAILABLE file=false, GH_OK file=true."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(rp, "which", lambda cmd: "/fake/" + cmd)
    monkeypatch.setattr(rp.Path, "home", classmethod(lambda _cls: tmp_path / "home"))
    monkeypatch.setattr(
        rp.subprocess,
        "run",
        _dispatch(
            {
                "gh auth": (0, ""),
                "claude plugin": (0, "some-other-plugin\n"),
                "git remote": (0, ""),
                "git rev-parse": (1, ""),
            }
        ),
    )
    monkeypatch.chdir(tmp_path)
    rc = rp.main([])
    assert rc == 0
    assert (tmp_path / "resolve-preflight-CODEX_AVAILABLE-shared").read_text() == "false\n"
    assert (tmp_path / "resolve-preflight-GH_OK-shared").read_text() == "true\n"


def test_windows_preflight_uses_native_tempdir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Preflight output avoids a POSIX TMPDIR inherited by Windows CI."""
    monkeypatch.setenv("TMPDIR", "/tmp")
    monkeypatch.setattr(rp.sys, "platform", "win32")
    monkeypatch.setattr(rp.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(rp, "which", lambda cmd: "/fake/" + cmd)
    monkeypatch.setattr(rp.Path, "home", classmethod(lambda _cls: tmp_path / "home"))
    monkeypatch.setattr(
        rp.subprocess,
        "run",
        _dispatch({"gh auth": (0, ""), "git remote": (0, ""), "git rev-parse": (1, "")}),
    )
    monkeypatch.chdir(tmp_path)

    assert rp.main([]) == 0
    assert (tmp_path / "resolve-preflight-CODEX_AVAILABLE-shared").read_text() == "false\n"


def _install_bridge(home: Path, *, enabled: bool = True) -> None:
    """Write a plugin registry (and optional opt-out) describing an installed bridge.

    Examples:
        >>> home = getfixture("tmp_path")
        >>> _install_bridge(home, enabled=False)
        >>> settings = json.loads((home / ".claude" / "settings.json").read_text())
        >>> settings["enabledPlugins"][rp.check_bridge.TARGET_SELECTOR]
        False
    """
    registry = home / ".claude" / "plugins" / "installed_plugins.json"
    registry.parent.mkdir(parents=True)
    entry = {"scope": "user", "installPath": str(home / "cache" / "bridge" / "0.2.0"), "version": "0.2.0"}
    registry.write_text(json.dumps({"plugins": {rp.check_bridge.TARGET_SELECTOR: [entry]}}), encoding="utf-8")
    if not enabled:
        settings = home / ".claude" / "settings.json"
        settings.write_text(json.dumps({"enabledPlugins": {rp.check_bridge.TARGET_SELECTOR: False}}), encoding="utf-8")


def _preflight_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, home: Path) -> None:
    """Point the preflight at a fake home and stub every subprocess it shells out to."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(rp, "which", lambda cmd: "/fake/" + cmd)
    monkeypatch.setattr(rp.Path, "home", classmethod(lambda _cls: home))
    monkeypatch.setattr(
        rp.subprocess,
        "run",
        _dispatch({"gh auth": (0, ""), "git remote": (0, ""), "git rev-parse": (1, "")}),
    )


@pytest.mark.parametrize(
    ("enabled", "expected"),
    [
        pytest.param(True, "true\n", id="bridge-installed-and-enabled"),
        pytest.param(False, "false\n", id="bridge-user-opted-out"),
    ],
)
def test_gh_ok_codex_availability_follows_bridge_enablement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, enabled: bool, expected: str
) -> None:
    """Authenticated gh with the bridge installed → CODEX_AVAILABLE is true only while the bridge is enabled.

    Availability is resolved from the plugin registry through the shared detector rather than from
    ``claude plugin list`` text, so the fixture is a registry, not a listing. A bridge the user opted out of must not
    be handed to callers as available: the registry lists it as installed, so a presence-only check would report it
    usable and every downstream dispatch would fail against a plugin the host refuses to load.
    """
    home = tmp_path / "home"
    _install_bridge(home, enabled=enabled)
    _preflight_env(monkeypatch, tmp_path, home)
    monkeypatch.chdir(tmp_path)
    rc = rp.main([])
    assert rc == 0
    assert (tmp_path / "resolve-preflight-CODEX_AVAILABLE-shared").read_text() == expected


def test_sentinel_writes_end_with_trailing_newline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Both preflight sentinels end with a trailing newline the consuming ``read`` line needs.

    ``IFS= read -r VAR < "$sentinel" || VAR=default`` (the shared sentinel-read idiom every mode file in oss:resolve
    uses) returns non-zero on a file with no trailing newline, so the ``||`` silently overwrites a real value with the
    default — confirmed live: a bare ``"true"`` sentinel made ``CODEX_AVAILABLE`` read back as ``false`` under both zsh
    and bash. Byte-exact, not ``.read_text()`` equality, so a regression back to the bare form is caught even if some
    other read path tolerated it.
    """
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(rp, "which", lambda cmd: "/fake/" + cmd)
    monkeypatch.setattr(rp.Path, "home", classmethod(lambda _cls: tmp_path / "home"))
    monkeypatch.setattr(
        rp.subprocess,
        "run",
        _dispatch({"gh auth": (0, ""), "git remote": (0, ""), "git rev-parse": (1, "")}),
    )
    monkeypatch.chdir(tmp_path)
    assert rp.main([]) == 0
    assert (tmp_path / "resolve-preflight-CODEX_AVAILABLE-shared").read_bytes().endswith(b"\n")
    assert (tmp_path / "resolve-preflight-GH_OK-shared").read_bytes().endswith(b"\n")


def test_gh_cache_hit_skips_auth(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Valid gh cache entry → auth subprocess not called."""
    monkeypatch.setattr(rp, "which", lambda cmd: "/fake/" + cmd)
    calls: list[str] = []

    def _tracking_run(cmd: list[str], **_: Any) -> _FakeCompleted:
        """Record executable/subcommand pairs while returning success."""
        calls.append(Path(cmd[0]).name + " " + (cmd[1] if len(cmd) > 1 else ""))
        return _FakeCompleted(returncode=0, stdout="")

    monkeypatch.setattr(rp.subprocess, "run", _tracking_run)
    monkeypatch.chdir(tmp_path)
    rp._preflight_pass("gh")
    rp.main([])
    assert not any("gh auth" in c for c in calls)


@pytest.mark.parametrize(
    ("remote_stdout", "log_stdout", "pull_rc", "expected_rc", "expected_message"),
    [
        pytest.param(
            "origin\t... (fetch)\n",
            "abc123 commit1\ndef456 commit2\n",
            0,
            0,
            "git pull: merged",
            id="remote-ahead-by-2-pulls-and-merges",
        ),
        pytest.param("", "abc123 commit\n", 1, 1, "git pull had conflicts", id="pull-conflict-exits-1"),
    ],
)
def test_remote_ahead_triggers_pull_and_reports_outcome(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    remote_stdout: str,
    log_stdout: str,
    pull_rc: int,
    expected_rc: int,
    expected_message: str,
) -> None:
    """Remote ahead → git pull invoked; the exit code and stderr message follow the pull outcome.

    A clean pull exits 0 with 'merged'; a non-zero pull exits 1 with a conflict message.
    """
    monkeypatch.setattr(rp, "which", lambda cmd: "/fake/" + cmd)

    def _seq_run(cmd: list[str], **_: Any) -> _FakeCompleted:
        """Return a successful bridge check, the parametrized remote/log output and the parametrized pull status."""
        binary = Path(cmd[0]).name
        subcmd = cmd[1] if len(cmd) > 1 else ""
        if binary == "claude":
            return _FakeCompleted(returncode=0, stdout="")
        if binary == "gh" and subcmd == "auth":
            return _FakeCompleted(returncode=0, stdout="")
        if binary == "git" and subcmd == "remote":
            return _FakeCompleted(returncode=0, stdout=remote_stdout)
        if binary == "git" and subcmd == "rev-parse":
            return _FakeCompleted(returncode=0, stdout="origin/main")
        if binary == "git" and subcmd == "fetch":
            return _FakeCompleted(returncode=0)
        if binary == "git" and subcmd == "log":
            return _FakeCompleted(returncode=0, stdout=log_stdout)
        if binary == "git" and subcmd == "pull":
            return _FakeCompleted(returncode=pull_rc)
        return _FakeCompleted(returncode=0, stdout="")

    monkeypatch.setattr(rp.subprocess, "run", _seq_run)
    monkeypatch.chdir(tmp_path)
    rc = rp.main([])
    assert rc == expected_rc
    assert expected_message in capsys.readouterr().err


def test_preflight_ok_expired_returns_false(tmp_path: Path) -> None:
    """Cache file with timestamp >4h old → ``_preflight_ok`` returns False."""
    state_dir = tmp_path / "preflight"
    state_dir.mkdir()
    old_ts = 0
    (state_dir / "gh.ok").write_text(str(old_ts))
    assert rp._preflight_ok("gh", state_dir) is False


def test_preflight_ok_fresh_returns_true(tmp_path: Path) -> None:
    """Cache file written by ``_preflight_pass`` → ``_preflight_ok`` returns True."""
    state_dir = tmp_path / "preflight"
    rp._preflight_pass("gh", state_dir)
    assert rp._preflight_ok("gh", state_dir) is True


def test_help_exits_0_no_subprocess(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Print usage and exit 0 WITHOUT running gh auth / git pull.

    Regression guard for the pre-argparse hazard where the script executed its full preflight (network + working-tree
    mutation) even when given ``--help``.
    """

    def _boom(*_a: Any, **_k: Any) -> None:
        """Fail the test if help handling performs any external operation."""
        raise AssertionError("subprocess.run must not be called on --help")

    monkeypatch.setattr(rp.subprocess, "run", _boom)
    monkeypatch.setattr(rp, "which", lambda _cmd: (_ for _ in ()).throw(AssertionError("which must not run on --help")))
    with pytest.raises(SystemExit) as exc:
        rp.main(["--help"])
    assert exc.value.code == 0
    assert "usage: resolve_preflight.py" in capsys.readouterr().out

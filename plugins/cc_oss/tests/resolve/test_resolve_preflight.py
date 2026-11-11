"""Tests for ``bin/resolve_preflight.py``.

``subprocess.run`` and ``which`` are replaced — no real tool is invoked. The ``preflight_env`` fixture moves ``TMPDIR``,
the working directory (where the script keeps its relative ``.temp/state/preflight/`` TTL cache) and the home directory
under ``tmp_path``, so no run writes a sentinel or marker outside the test sandbox.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import resolve_preflight as rp

#: Responses for an authenticated ``gh`` in a repository whose remote needs no pull.
_GH_OK = {"gh auth": (0, ""), "git remote": (0, ""), "git rev-parse": (1, "")}
_CODEX_SENTINEL = "resolve-preflight-CODEX_AVAILABLE-shared"
_GH_SENTINEL = "resolve-preflight-GH_OK-shared"


def _dispatch(responses: dict[str, tuple[int, str]]) -> Any:
    """Build a subprocess.run fake dispatching on the binary name + first subcommand; unknown commands succeed."""

    def _fake_run(cmd: list[str], **_kwargs: Any) -> SimpleNamespace:
        """Dispatch a fake response by executable and first subcommand."""
        key = f"{Path(cmd[0]).name} {cmd[1] if len(cmd) > 1 else ''}".strip()
        for pattern, (rc, out) in responses.items():
            if pattern in key:
                return SimpleNamespace(returncode=rc, stdout=out, stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return _fake_run


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


@pytest.fixture
def preflight_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Sandbox the preflight under ``tmp_path`` (TMPDIR, cwd, home) with every tool found on PATH; return the home."""
    home = tmp_path / "home"
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(rp, "which", lambda cmd: "/fake/" + cmd)
    monkeypatch.setattr(rp.Path, "home", classmethod(lambda _cls: home))
    monkeypatch.chdir(tmp_path)
    return home


@pytest.fixture
def stub_tools(monkeypatch: pytest.MonkeyPatch) -> Callable[[dict[str, tuple[int, str]]], None]:
    """Return an installer that replaces ``subprocess.run`` with a responses table (``_dispatch``)."""

    def install(responses: dict[str, tuple[int, str]]) -> None:
        """Install the fake runner for the given responses."""
        monkeypatch.setattr(rp.subprocess, "run", _dispatch(responses))

    return install


class TestToolChecks:
    """``main`` — the gh presence and authentication gate."""

    def test_missing_gh_exits_1(
        self, preflight_env: Path, monkeypatch: pytest.MonkeyPatch, stub_tools: Callable, capsys: pytest.CaptureFixture
    ) -> None:
        """No gh on PATH → exit 1 with 'gh not found' on stderr.

        Without gh nothing in the resolve flow can run, so the failure is reported up front rather than mid-run.
        """
        monkeypatch.setattr(rp, "which", lambda cmd: None if cmd == "gh" else "/fake/" + cmd)
        stub_tools({})
        assert rp.main([]) == 1
        assert "gh not found" in capsys.readouterr().err

    def test_unauthenticated_gh_exits_1(
        self, preflight_env: Path, stub_tools: Callable, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Gh found but auth fails → exit 1 with 'gh found but not authenticated'."""
        stub_tools({**_GH_OK, "gh auth": (1, "")})
        assert rp.main([]) == 1
        assert "gh found but not authenticated" in capsys.readouterr().err

    def test_help_exits_0_without_running_tools(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Print usage and exit 0 WITHOUT running gh auth / git pull.

        Regression guard for the pre-argparse hazard where the script executed its full preflight (network + working-
        tree mutation) even when given ``--help``.
        """

        def _forbidden(*_a: Any, **_k: Any) -> None:
            """Fail the test if help handling performs any external operation."""
            raise AssertionError("no external operation may run on --help")

        monkeypatch.setattr(rp.subprocess, "run", _forbidden)
        monkeypatch.setattr(rp, "which", _forbidden)
        with pytest.raises(SystemExit) as exc:
            rp.main(["--help"])
        assert exc.value.code == 0
        assert "usage: resolve_preflight.py" in capsys.readouterr().out


class TestSentinels:
    """``main`` — the output sentinels the resolve modes read back."""

    def test_codex_absent_writes_newline_terminated_sentinels(
        self, preflight_env: Path, tmp_path: Path, stub_tools: Callable
    ) -> None:
        """Authenticated gh, no bridge → exit 0, CODEX_AVAILABLE ``false`` and GH_OK ``true``, each ending in a newline.

        ``IFS= read -r VAR < "$sentinel" || VAR=default`` (the sentinel-read idiom every oss:resolve mode file uses)
        returns non-zero on a file with no trailing newline, so the ``||`` silently replaces a real value with the
        default — confirmed live: a bare ``"true"`` sentinel read back as ``false`` under both zsh and bash. The check
        is byte-exact so a regression back to the bare form is caught even if another read path tolerated it.
        """
        stub_tools(_GH_OK)
        assert rp.main([]) == 0
        assert (tmp_path / _CODEX_SENTINEL).read_bytes() == b"false\n"
        assert (tmp_path / _GH_SENTINEL).read_bytes() == b"true\n"

    def test_windows_uses_native_tempdir(
        self, preflight_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_tools: Callable
    ) -> None:
        """Preflight output avoids a POSIX TMPDIR inherited by Windows CI.

        Scenario: on win32 a leftover ``TMPDIR=/tmp`` must be ignored in favour of the native temp directory, or the
        sentinels land where the Windows shell does not look.
        """
        monkeypatch.setenv("TMPDIR", "/tmp")
        monkeypatch.setattr(rp.sys, "platform", "win32")
        monkeypatch.setattr(rp.tempfile, "gettempdir", lambda: str(tmp_path))
        stub_tools(_GH_OK)
        assert rp.main([]) == 0
        assert (tmp_path / _CODEX_SENTINEL).read_text() == "false\n"

    @pytest.mark.parametrize(
        ("enabled", "expected"),
        [
            pytest.param(True, "true\n", id="bridge-installed-and-enabled"),
            pytest.param(False, "false\n", id="bridge-user-opted-out"),
        ],
    )
    def test_codex_availability_follows_bridge_enablement(
        self, preflight_env: Path, tmp_path: Path, stub_tools: Callable, enabled: bool, expected: str
    ) -> None:
        """Authenticated gh with the bridge installed → CODEX_AVAILABLE is true only while the bridge is enabled.

        Availability is resolved from the plugin registry through the shared detector rather than from
        ``claude plugin list`` text, so the fixture is a registry, not a listing. A bridge the user opted out of must
        not be handed to callers as available: the registry lists it as installed, so a presence-only check would
        report it usable and every downstream dispatch would fail against a plugin the host refuses to load.
        """
        _install_bridge(preflight_env, enabled=enabled)
        stub_tools(_GH_OK)
        assert rp.main([]) == 0
        assert (tmp_path / _CODEX_SENTINEL).read_text() == expected


class TestGhAuthCache:
    """``main`` and the TTL marker helpers around the gh authentication check."""

    def test_cache_hit_skips_auth(self, preflight_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Valid gh cache entry → auth subprocess not called.

        Scenario: an earlier run recorded a passing gh check; the next run within the TTL must trust it instead of
        spending another network round trip on ``gh auth``.
        """
        calls: list[str] = []

        def _tracking_run(cmd: list[str], **_: Any) -> SimpleNamespace:
            """Record executable/subcommand pairs while returning success."""
            calls.append(Path(cmd[0]).name + " " + (cmd[1] if len(cmd) > 1 else ""))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(rp.subprocess, "run", _tracking_run)
        rp._preflight_pass("gh")
        rp.main([])
        assert not any("gh auth" in c for c in calls)

    def test_fresh_marker_is_ok(self, tmp_path: Path) -> None:
        """Cache file written by ``_preflight_pass`` → ``_preflight_ok`` returns True.

        An expired marker is covered in the cache-trust tests; this is the write-then-read round trip.
        """
        state_dir = tmp_path / "preflight"
        rp._preflight_pass("gh", state_dir)
        assert rp._preflight_ok("gh", state_dir) is True


class TestRemoteAhead:
    """The preflight CLI refreshes origin without pulling the caller's branch."""

    @pytest.mark.parametrize(
        ("remote_stdout", "log_stdout", "fetch_rc", "expected_rc", "expected_message"),
        [
            pytest.param(
                "origin\t... (fetch)\n",
                "abc123 commit1\ndef456 commit2\n",
                0,
                0,
                "fetched origin (current branch unchanged)",
                id="remote-ahead-fetches-without-merging",
            ),
            pytest.param("", "abc123 commit\n", 1, 1, "cannot fetch origin", id="fetch-failure-exits-1"),
        ],
    )
    def test_fetch_outcome_sets_exit_code_and_message(
        self,
        preflight_env: Path,
        stub_tools: Callable,
        capsys: pytest.CaptureFixture[str],
        remote_stdout: str,
        log_stdout: str,
        fetch_rc: int,
        expected_rc: int,
        expected_message: str,
    ) -> None:
        """Fetch errors block the CLI; a successful fetch preserves the current branch."""
        stub_tools(
            {
                "gh auth": (0, ""),
                "git remote": (0, remote_stdout),
                "git rev-parse": (0, "origin/main"),
                "git log": (0, log_stdout),
                "git fetch": (fetch_rc, ""),
            }
        )
        assert rp.main([]) == expected_rc
        assert expected_message in capsys.readouterr().err

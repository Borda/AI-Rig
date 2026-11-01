"""Public-contract tests for Bridge's credential-free setup planner and executor.

Every native host CLI call is replaced by a deterministic ledger-recording fake at the ``subprocess.run`` boundary, so
the suite exercises planning, signed single-use approvals, locks and journals without touching a real host.
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import json
import os
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from _bridge_link_probes import file_symlinks_supported

BIN_ROOT = Path(__file__).resolve().parents[1] / "bin"
SETUP_PATH = BIN_ROOT / "bridge_setup.py"
if str(BIN_ROOT) not in sys.path:
    sys.path.insert(0, str(BIN_ROOT))

INSTALL_ARGV = ["claude", "plugin", "install", "bridge@borda-ai-rig", "--scope", "user"]
LOGIN_ARGV = ["claude", "auth", "login"]
CONFIGURED_PEER = {"claude_installed": True, "claude_authenticated": True}

Calls = list[tuple[list[str], dict[str, object]]]


FILE_SYMLINKS_SUPPORTED = file_symlinks_supported()


def _setup_module() -> Any:
    """Load the setup module only after reporting a normal test failure when absent."""
    assert SETUP_PATH.is_file(), "Bridge setup needs a deterministic bridge_setup.py entry point"
    return importlib.import_module("bridge_setup")


def _completed(
    argv: list[str], stdout: str = "", returncode: int = 0, stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    """Build one native-command result without executing a host command.

    Examples:
        >>> _completed(["claude", "--version"], returncode=7).returncode
        7
    """
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def _install_native_boundary(
    setup: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    claude_installed: bool = False,
    claude_authenticated: bool = False,
    malformed_claude_inventory: bool = False,
    configuration_returncode: int = 0,
    authentication_returncode: int = 0,
    inventory_after_configuration: bool = False,
    authentication_status_returncode: int = 0,
    live_success: bool | None = None,
) -> Calls:
    """Mock exact installed-CLI probes while retaining a visible command ledger."""
    calls: Calls = []
    state = {"claude_installed": claude_installed}

    def _fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Return deterministic CLI probe responses and record every invocation."""
        command = list(argv)
        calls.append((command, kwargs))
        if command == ["codex", "--version"]:
            return _completed(command, "codex-cli 0.148.0\n")
        if command == ["claude", "--version"]:
            return _completed(command, "2.1.227\n")
        if command == ["codex", "plugin", "list", "--json"]:
            return _completed(command, '{"installed": []}\n')
        if command == ["claude", "plugin", "list", "--json"]:
            if malformed_claude_inventory:
                return _completed(command, "not-json\n")
            installed = []
            if state["claude_installed"]:
                installed.append({"id": "bridge@borda-ai-rig", "version": "0.3.1", "enabled": True})
            return _completed(command, json.dumps(installed) + "\n")
        if command == ["codex", "login", "status"]:
            return _completed(command, "Logged in\n")
        if command == ["claude", "auth", "status", "--json"]:
            return _completed(
                command,
                json.dumps({"loggedIn": claude_authenticated}) + "\n" if authentication_status_returncode == 0 else "",
                returncode=authentication_status_returncode,
            )
        if command in (["codex", "plugin", "--help"], ["claude", "plugin", "--help"]):
            return _completed(command, "supported native operation\n")
        if command in (LOGIN_ARGV, ["codex", "login"]):
            return _completed(command, returncode=authentication_returncode)
        if command == INSTALL_ARGV:
            if configuration_returncode == 0 and inventory_after_configuration:
                state["claude_installed"] = True
            return _completed(command, returncode=configuration_returncode)
        if command == ["codex", "plugin", "add", "bridge@borda-ai-rig", "--json"]:
            return _completed(command, returncode=configuration_returncode)
        if live_success is not None and len(command) >= 2 and Path(command[1]).name == "bridge_diagnose.py":
            if live_success:
                direction = command[command.index("--direction") + 1]
                return _completed(command, json.dumps({"ok": True, "live": True, "direction": direction}) + "\n")
            return _completed(command, returncode=1)
        return _completed(command, stderr="unsupported mocked command", returncode=2)

    monkeypatch.setattr(setup.subprocess, "run", _fake_run)
    monkeypatch.setattr(setup, "_resolve_executable", lambda executable: executable)
    monkeypatch.setattr(setup, "_resolved_command", lambda command: command)
    # Tests play the operator at a terminal; the refusal test overrides this.
    monkeypatch.setattr(setup, "_operator_terminal", lambda: True)
    return calls


def _redirect_user_state(setup: Any, monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Keep approval, lock, and journal tests inside a disposable user-state root."""
    monkeypatch.setattr(setup, "_user_state_root", lambda: root)


def _commands(calls: Calls) -> list[list[str]]:
    """Return only the argv of every recorded native call, in order."""
    return [command for command, _ in calls]


def _installs(calls: Calls) -> list[list[str]]:
    """Return the recorded native plugin-install commands."""
    return [command for command in _commands(calls) if command[:3] == ["claude", "plugin", "install"]]


def _bridge_commands(calls: Calls) -> list[list[str]]:
    """Return the recorded native commands that name the Bridge plugin."""
    return [command for command in _commands(calls) if "bridge@borda-ai-rig" in command]


def _state_files(root: Path) -> list[Path]:
    """List every file below ``root`` relative to it, sorted, for before/after state comparison."""
    return sorted(path.relative_to(root) for path in root.rglob("*") if path.is_file())


def _lock_path(user_root: Path) -> Path:
    """Path of the user-scoped mutation lock for the Claude user-scope target."""
    return user_root / "bridge-setup" / "locks" / "claude-user.lock"


def _journal_path(user_root: Path) -> Path:
    """Path of the append-only setup journal for the Claude user-scope target."""
    return user_root / "bridge-setup" / "records" / "claude-user.jsonl"


def _reencode_untrusted_approval(payload: dict[str, object]) -> str:
    """Model an attacker who can recompute a public checksum but lacks the host signing key.

    Examples:
        >>> token = _reencode_untrusted_approval({"action": "configure"})
        >>> len(token.rsplit(".", 1)[-1])
        64
    """
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{encoded}.{hashlib.sha256(raw).hexdigest()}"


def _age_journal_records(journal: Path, seconds: int) -> None:
    """Shift every journal record's timestamp into the past by ``seconds``.

    Examples:
        >>> journal = getfixture("tmp_path") / "journal.jsonl"
        >>> _ = journal.write_text('{"timestamp": 10}\\n')
        >>> _age_journal_records(journal, 3)
        >>> json.loads(journal.read_text())["timestamp"]
        7
    """
    aged = []
    for line in journal.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        record["timestamp"] = int(record["timestamp"]) - seconds
        aged.append(json.dumps(record, sort_keys=True, separators=(",", ":")))
    journal.write_text("\n".join(aged) + "\n", encoding="utf-8", newline="\n")


def _issued_in_the_future(payload: dict[str, object]) -> None:
    """Move an approval's issue time to its former expiry so it claims to be issued in the future."""
    payload["issued_at"] = int(payload["expires_at"])
    payload["expires_at"] = int(payload["expires_at"]) + 300


def _with_excessive_lifetime(payload: dict[str, object]) -> None:
    """Stretch an approval's lifetime one second past the five-minute bound."""
    payload["expires_at"] = int(payload["issued_at"]) + 301


def _gate_lock_creation(setup: Any, monkeypatch: pytest.MonkeyPatch, lock_path: Path) -> None:
    """Make the first two exclusive opens of ``lock_path`` wait for each other so both callers race for the lock."""
    real_open = setup.os.open
    lock_barrier = threading.Barrier(2)
    open_count_lock = threading.Lock()
    lock_open_count = 0

    def _gated_open(path: str, flags: int, mode: int = 0o777) -> int:
        """Synchronize competing lock opens to exercise exclusive creation."""
        nonlocal lock_open_count
        should_wait = False
        if Path(path) == lock_path and flags & setup.os.O_EXCL:
            with open_count_lock:
                if lock_open_count < 2:
                    lock_open_count += 1
                    should_wait = True
            if should_wait:
                lock_barrier.wait(timeout=5)
        return real_open(path, flags, mode)

    monkeypatch.setattr(setup.os, "open", _gated_open)


def _run_concurrently(setup: Any, argv: list[str], workers: int) -> tuple[list[int], list[BaseException], list[bool]]:
    """Run ``setup.main(argv)`` in ``workers`` threads; return exit codes, raised errors and still-alive flags."""
    exit_codes: list[int] = []
    errors: list[BaseException] = []

    def _apply() -> None:
        """Run one setup invocation, keeping any error visible to the caller."""
        try:
            exit_codes.append(setup.main(argv))
        except BaseException as error:  # pragma: no cover - asserted by the caller
            errors.append(error)

    threads = [threading.Thread(target=_apply) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    return exit_codes, errors, [thread.is_alive() for thread in threads]


@pytest.fixture
def setup() -> Any:
    """Provide the imported ``bridge_setup`` module under test."""
    return _setup_module()


@pytest.fixture
def user_root(setup: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Disposable user-state root that setup uses for keys, locks and journals."""
    root = tmp_path / "user-state"
    _redirect_user_state(setup, monkeypatch, root)
    return root


@pytest.fixture
def native(setup: Any, monkeypatch: pytest.MonkeyPatch) -> Callable[..., Calls]:
    """Install the native-CLI fake; call with host options and keep the returned command ledger."""

    def _install(**options: Any) -> Calls:
        """Install the fake with ``options`` and return its ledger."""
        return _install_native_boundary(setup, monkeypatch, **options)

    return _install


@pytest.fixture
def invoke(
    setup: Any, user_root: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> Callable[..., dict[str, object]]:
    """Run one public setup invocation for the Codex host and return its sole JSON result."""

    def _invoke(*extra: str, workspace: Path | None = None) -> dict[str, object]:
        """Run ``main`` against ``workspace`` (default ``tmp_path``) with ``extra`` arguments."""
        exit_code = setup.main(["--current-host", "codex", "--workspace", str(workspace or tmp_path), *extra])
        captured = capsys.readouterr()
        assert exit_code in {0, 2}
        assert captured.err == ""
        result = json.loads(captured.out)
        assert isinstance(result, dict)
        return result

    return _invoke


@pytest.fixture
def approve(invoke: Callable[..., dict[str, object]]) -> Callable[..., dict[str, object]]:
    """Run a setup invocation that presents ``digest`` as the operator's approval."""

    def _approve(digest: object, *extra: str, workspace: Path | None = None) -> dict[str, object]:
        """Invoke setup with ``extra`` arguments followed by ``--approve <digest>``."""
        return invoke(*extra, "--approve", str(digest), workspace=workspace)

    return _approve


@pytest.fixture
def live_calls(setup: Any, monkeypatch: pytest.MonkeyPatch) -> list[tuple[tuple[object, ...], dict[str, object]]]:
    """Record live-verification attempts without contacting a provider."""
    recorded: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def _record(*args: object, **kwargs: object) -> bool:
        """Remember the attempt and report success."""
        recorded.append((args, kwargs))
        return True

    monkeypatch.setattr(setup, "_verify_live", _record)
    return recorded


class TestParser:
    """Argument parsing fails or succeeds before any native setup operation runs."""

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param(["--help"], id="ordinary-help"),
            pytest.param(["--help", "--approve"], id="help-before-empty-approval"),
        ],
    )
    def test_help_exits_zero_with_usage_and_no_native_calls(
        self,
        setup: Any,
        native: Callable[..., Calls],
        capsys: pytest.CaptureFixture[str],
        argv: list[str],
    ) -> None:
        """Help prints usage on stdout and succeeds, even when a bare ``--approve`` follows it.

        A trailing empty approval must not turn a help request into a denial or reach the host CLIs.
        """
        calls = native()

        assert setup.main(argv) == 0

        captured = capsys.readouterr()
        assert calls == []
        assert "usage:" in captured.out
        assert "--current-host" in captured.out
        assert captured.err == ""

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param(["--unknown-option"], id="invalid-arguments"),
            pytest.param(["--unknown-option", "--approve"], id="invalid-before-empty-approval"),
        ],
    )
    def test_syntax_error_exits_two_on_stderr_with_no_native_calls(
        self,
        setup: Any,
        native: Callable[..., Calls],
        capsys: pytest.CaptureFixture[str],
        argv: list[str],
    ) -> None:
        """An unknown option exits 2 with the error on stderr only, before any native operation.

        The bare ``--approve`` case guards the denial fallback from masking the parser's own syntax error.
        """
        calls = native()

        assert setup.main(argv) == 2

        captured = capsys.readouterr()
        assert calls == []
        assert captured.out == ""
        assert "error:" in captured.err

    @pytest.mark.parametrize("sensitive_option", ["--token", "--access-token", "--device-code"])
    def test_rejects_sensitive_values_without_echo_or_subprocess(
        self,
        invoke: Callable[..., dict[str, object]],
        native: Callable[..., Calls],
        sensitive_option: str,
    ) -> None:
        """Credential-shaped options are blocked, never echoed, and never reach a subprocess.

        The rejection is built before arguments are parsed, so its placeholder ``requested`` values must also stay
        inside the published result-schema enums.
        """
        calls = native()
        secret = "sensitive-value-must-not-escape"  # noqa: S105 - test sentinel string, not a credential
        schema_path = SETUP_PATH.parents[1] / "schemas" / "setup-result.schema.json"
        requested_properties = json.loads(schema_path.read_text(encoding="utf-8"))["properties"]["requested"][
            "properties"
        ]

        result = invoke(sensitive_option, secret)

        assert result["status"] == "blocked"
        assert secret not in json.dumps(result, sort_keys=True)
        assert calls == []
        outside_schema = {
            field: value
            for field, value in result["requested"].items()
            if value not in requested_properties[field]["enum"]
        }
        assert outside_schema == {}

    def test_empty_approval_denial_reports_the_actual_request(
        self, invoke: Callable[..., dict[str, object]], native: Callable[..., Calls], tmp_path: Path
    ) -> None:
        """A trailing bare ``--approve`` denial echoes the real action, policy, scope and workspace.

        The old fallback hardcoded action=all live=prompt and hand-parsed only space-separated flags, so a denied
        live=required run was reported under the softer prompt policy and equals-form flags described another run.
        """
        calls = native()

        result = invoke("--action", "verify-live", "--live", "required", "--scope=project", "--approve")

        assert result["status"] == "denied"
        assert result["requested"] == {
            "action": "verify-live",
            "target": "peer",
            "scope": "project",
            "live": "required",
        }
        assert result["canonical_workspace"] == tmp_path.resolve().as_posix()
        assert calls == []


class TestHostProbing:
    """Native CLI capability, version, inventory and authentication probes fail closed."""

    def test_capability_matrix_holds_only_version_gated_native_operations(self, setup: Any) -> None:
        """The capability matrix lists exactly the supported login, plugin and scope commands.

        A guessed login, plugin-enable or scope command added here would become an executable setup path.
        """
        assert setup.CAPABILITY_MATRIX == {
            "codex": {
                "minimum_version": "0.148.0",
                "plugin_list_argv": ["codex", "plugin", "list", "--json"],
                "configure_argv": ["codex", "plugin", "add", "bridge@borda-ai-rig", "--json"],
                "authentication_status_argv": ["codex", "login", "status"],
                "authentication_argv": ["codex", "login"],
                "scopes": [],
            },
            "claude": {
                "minimum_version": "2.1.227",
                "plugin_list_argv": ["claude", "plugin", "list", "--json"],
                "configure_argv": ["claude", "plugin", "install", "bridge@borda-ai-rig", "--scope", "{scope}"],
                "enable_argv": ["claude", "plugin", "enable", "bridge@borda-ai-rig", "--scope", "{scope}"],
                "authentication_status_argv": ["claude", "auth", "status", "--json"],
                "authentication_argv": LOGIN_ARGV,
                "scopes": ["user", "project", "local"],
            },
        }

    @pytest.mark.parametrize(
        ("reported", "accepted"),
        [
            pytest.param("2.1.227\n", True, id="exact-minimum"),
            pytest.param("2.1.228 (Claude Code)\n", True, id="newer-patch"),
            pytest.param("3.0.0\n", True, id="newer-major"),
            pytest.param("2.1.226\n", False, id="older-patch"),
            pytest.param("no numeric release here\n", False, id="unparsable"),
        ],
    )
    def test_version_gate_is_a_minimum_floor_not_an_exact_pin(
        self, setup: Any, monkeypatch: pytest.MonkeyPatch, reported: str, accepted: bool
    ) -> None:
        """Host CLI releases at or above the pinned floor stay supported; older or unreadable ones fail closed.

        Host CLIs self-update between bridge releases; an exact-match gate made every setup action fail unsupported-
        version the day after either CLI shipped an update.
        """
        monkeypatch.setattr(setup, "_capture", lambda argv: (True, reported, ""))

        available, version = setup._version("claude")

        assert available is accepted
        assert (version is not None) is accepted

    def test_codex_not_logged_in_status_is_not_authenticated(self, setup: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """A zero-exit "Not logged in" status does not satisfy the authentication gate.

        ``codex login status`` can exit 0 while printing "Not logged in"; a bare substring match on "logged in" would
        claim host authentication and let a paid live verification proceed against an unauthenticated peer.
        """
        monkeypatch.setattr(setup.subprocess, "run", lambda argv, **kwargs: _completed(list(argv), "Not logged in\n"))
        monkeypatch.setattr(setup, "_resolved_command", lambda command: command)

        supported, authenticated = setup._authenticated("codex")

        assert supported is True
        assert authenticated is False

    def test_malformed_inventory_fails_closed_without_configuration(
        self, invoke: Callable[..., dict[str, object]], native: Callable[..., Calls]
    ) -> None:
        """A malformed plugin inventory is unsupported, never an empty inventory repaired blindly.

        Treating unparsable host output as "nothing installed" would run an install against unknown host state.
        """
        calls = native(malformed_claude_inventory=True)

        result = invoke()

        assert result["status"] == "unsupported"
        assert result["ready_to_use"] is False
        assert result["state_changed"] is False
        assert _bridge_commands(calls) == []

    def test_missing_cli_fails_closed_without_guessing_an_install_command(
        self,
        setup: Any,
        invoke: Callable[..., dict[str, object]],
        native: Callable[..., Calls],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A missing peer executable is a missing prerequisite, not a repairable empty inventory.

        Codex stays discoverable while Claude is hidden, so the planner must stop before any probe runs.
        """
        calls = native()
        monkeypatch.setattr(
            setup, "_resolve_executable", lambda executable: None if executable == "claude" else executable
        )

        result = invoke()

        assert result["status"] == "blocked"
        assert result["classification"] == "missing-prerequisite"
        assert result["ready_to_use"] is False
        assert result["state_changed"] is False
        assert calls == []

    def test_claude_auth_status_exit_one_with_no_output_is_unauthenticated(
        self, invoke: Callable[..., dict[str, object]], native: Callable[..., Calls]
    ) -> None:
        """Claude's documented unauthenticated exit is a clean pending state, not unsupported CLI drift.

        ``claude auth status`` exits 1 with no output when logged out; the plan must still offer configuration and list
        authentication as remaining work.
        """
        native(authentication_status_returncode=1)

        result = invoke()

        assert result["status"] == "partial"
        assert result["classification"] == "configuration-pending"
        assert result["authentication"] == "not-checked"
        assert "authentication" in result["remaining"]

    def test_installed_authenticated_peer_is_host_authenticated_not_session_ready(
        self, invoke: Callable[..., dict[str, object]], native: Callable[..., Calls]
    ) -> None:
        """An installed, logged-in peer reports host authentication without claiming session or live readiness.

        Host-level evidence must not be hidden, and must not be overstated as proof that a session or inference works.
        """
        native(**CONFIGURED_PEER)

        result = invoke()

        assert result["status"] == "partial"
        assert result["authentication"] == "host-authenticated"
        assert result["verification_level"] == "host-authenticated"
        assert result["ready_to_use"] is False
        assert result["remaining"] == ["session-workspace-verification", "live-verification"]


class TestPlanning:
    """Planning resolves the peer host and binds exact native operations to a workspace without executing them."""

    def test_default_resolves_peer_and_binds_exact_native_operation_to_workspace(
        self, invoke: Callable[..., dict[str, object]], native: Callable[..., Calls]
    ) -> None:
        """A default plan selects the peer host, user scope and workspace and lists one exact install command.

        Nothing may be inferred silently: host, scope, workspace and the native argv are all visible in the plan, and
        only read-only probes run while planning.
        """
        calls = native()

        result = invoke()

        assert result["status"] == "partial"
        assert result["current_host"] == "codex"
        assert result["target"] == "claude"
        assert result["direction"] == "codex_to_claude"
        assert result["requested"] == {"action": "all", "target": "peer", "scope": "auto", "live": "prompt"}
        assert result["resolved_scope"] == "user"
        assert result["approval_digest"]
        assert result["ready_to_use"] is False
        assert result["provider_call"] is False
        assert result["authentication"] == "not-checked"
        assert result["operations"] == [
            {
                "action": "configure",
                "argv": INSTALL_ARGV,
                "credential_behavior": "none",
                "external_capability": "configured-marketplace-snapshot",
            }
        ]
        assert _commands(calls) == [
            ["codex", "--version"],
            ["claude", "--version"],
            ["claude", "plugin", "list", "--json"],
            ["claude", "auth", "status", "--json"],
        ]

    @pytest.mark.parametrize(
        ("current_host", "target", "direction"),
        [
            pytest.param("codex", "claude", "codex_to_claude", id="codex-to-claude"),
            pytest.param("claude", "codex", "claude_to_codex", id="claude-to-codex"),
        ],
    )
    def test_peer_target_is_explicit_for_both_loaded_hosts(
        self,
        setup: Any,
        user_root: Path,
        native: Callable[..., Calls],
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        current_host: str,
        target: str,
        direction: str,
    ) -> None:
        """Setup targets the other host, never the one whose invocation surface is already loaded.

        Configuring the loaded host would be a no-op bootstrap loop, so each loaded host maps to its opposite peer.
        """
        native()

        exit_code = setup.main(["--current-host", current_host, "--workspace", str(tmp_path)])

        result = json.loads(capsys.readouterr().out)
        assert exit_code in {0, 2}
        assert result["current_host"] == current_host
        assert result["target"] == target
        assert result["direction"] == direction

    @pytest.mark.parametrize(
        ("action", "refused"),
        [pytest.param("all", True, id="plan-refused"), pytest.param("check", False, id="read-only-check-allowed")],
    )
    def test_home_workspace_is_refused_except_for_the_read_only_check(
        self,
        setup: Any,
        user_root: Path,
        native: Callable[..., Calls],
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        action: str,
        refused: bool,
    ) -> None:
        """Setup refuses the folders the MCP server refuses, so a plan never binds approvals to the home.

        The live probe writes Bridge artifacts into the workspace; ``check`` writes nothing and stays available.
        """
        calls = native()
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(importlib.import_module("bridge_mcp"), "_user_home", lambda: home.resolve())

        exit_code = setup.main(["--current-host", "codex", "--workspace", str(home), "--action", action])

        result = json.loads(capsys.readouterr().out)
        assert (result["classification"] == "workspace-refused") is refused
        assert (exit_code == 2 and result["status"] == "blocked" and calls == []) is refused

    def test_actionable_plan_creates_only_a_safe_key_and_check_creates_no_state(
        self,
        setup: Any,
        user_root: Path,
        invoke: Callable[..., dict[str, object]],
        native: Callable[..., Calls],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Planning writes only the approval signing key; ``--action check`` writes nothing at all.

        The key must be one regular, non-symlink file of the documented size with owner-only permissions where the OS
        has them; no journal or lock may be created while planning.
        """
        native()
        check_root = tmp_path / "check-state"

        plan = invoke()
        key_files = [path for path in user_root.rglob("*") if path.is_file()]
        _redirect_user_state(setup, monkeypatch, check_root)
        check = invoke("--action", "check")

        assert plan["status"] == check["status"] == "partial"
        assert len(key_files) == 1
        assert not key_files[0].is_symlink()
        assert key_files[0].stat().st_size == setup.APPROVAL_KEY_BYTES
        assert os.name == "nt" or stat.S_IMODE(key_files[0].stat().st_mode) == 0o600
        assert not (user_root / "bridge-setup" / "locks").exists()
        assert not (user_root / "bridge-setup" / "records").exists()
        assert not check_root.exists()

    def test_authentication_plan_holds_only_the_provider_owned_login(
        self, invoke: Callable[..., dict[str, object]], native: Callable[..., Calls]
    ) -> None:
        """An authentication plan is one interactive login argv and carries no plugin mutation.

        The login must stay provider-owned with no credential capture, so it can never share a digest with an install.
        """
        native()

        result = invoke("--action", "authenticate")

        assert result["operations"] == [
            {
                "action": "authenticate",
                "argv": LOGIN_ARGV,
                "credential_behavior": "provider-owned-interactive-no-capture",
                "external_capability": "provider-authentication",
            }
        ]

    def test_live_plan_holds_only_the_separately_approved_peer_inference(
        self, invoke: Callable[..., dict[str, object]], native: Callable[..., Calls], tmp_path: Path
    ) -> None:
        """A live-verification plan is one diagnose command and never bundles plugin configuration.

        The paid provider request needs its own approval, so the plan names only the diagnose script for this workspace
        and direction.
        """
        native()

        result = invoke("--action", "verify-live")

        assert result["operations"] == [
            {
                "action": "verify-live",
                "argv": [
                    sys.executable,
                    str(SETUP_PATH.with_name("bridge_diagnose.py").resolve()),
                    "--direction",
                    "claude",
                    "--workspace",
                    tmp_path.resolve().as_posix(),
                    "--live",
                ],
                "credential_behavior": "separately-approved-provider-request",
                "external_capability": "paid-peer-inference",
            }
        ]

    def test_explicit_current_host_target_requires_bootstrap_without_mutation_or_login(
        self, invoke: Callable[..., dict[str, object]], native: Callable[..., Calls]
    ) -> None:
        """Targeting the already-loaded host is a manual bootstrap case and never mutates or logs in.

        A loaded setup skill cannot install its own unavailable invocation surface.
        """
        calls = native()

        result = invoke("--target", "codex")

        assert result["status"] == "manual"
        assert result["classification"] == "bootstrap-required"
        assert result["state_changed"] is False
        assert result["provider_call"] is False
        assert _bridge_commands(calls) == []
        assert ["codex", "login"] not in _commands(calls)

    def test_live_skip_is_terminal_inference_unverified_without_approval_or_provider_call(
        self, invoke: Callable[..., dict[str, object]], native: Callable[..., Calls]
    ) -> None:
        """``--live skip`` ends as inference-unverified with no approval digest and no diagnose call.

        Skipping live verification must not create a second approval or imply a provider result.
        """
        calls = native(**CONFIGURED_PEER, live_success=True)

        plan = invoke("--live", "skip")

        assert plan["operations"] == []
        assert plan["approval_digest"] is None
        assert plan["status"] == "partial"
        assert plan["classification"] == "inference-unverified"
        assert plan["authentication"] == "inference-unverified"
        assert plan["provider_call"] is False
        assert plan["ready_to_use"] is False
        assert plan["remaining"] == ["session-workspace-verification"]
        assert not any(
            len(command) >= 2 and Path(command[1]).name == "bridge_diagnose.py" for command in _commands(calls)
        )

    def test_verify_live_with_live_skip_plans_no_operation_even_when_unconfigured(
        self, invoke: Callable[..., dict[str, object]], native: Callable[..., Calls]
    ) -> None:
        """A live-verification request under ``--live skip`` plans no operation, not a configure fallback.

        On an unconfigured host the configure fallback used to fill the plan, so the operator was asked to approve a
        plugin install under a plan labeled live verification.
        """
        native()

        result = invoke("--action", "verify-live", "--live", "skip")

        assert result["operations"] == []
        assert result["status"] == "blocked"
        assert result["classification"] == "inference-unverified"


class TestApprovals:
    """Approvals are signed, action-bound, time-bound, workspace-bound, single-use and need an operator terminal."""

    @pytest.mark.parametrize(
        "extra",
        [pytest.param(["--approve"], id="missing"), pytest.param(["--approve", "wrong-digest"], id="wrong-digest")],
    )
    def test_missing_or_wrong_approval_is_denied_before_any_native_subprocess(
        self,
        invoke: Callable[..., dict[str, object]],
        native: Callable[..., Calls],
        extra: list[str],
    ) -> None:
        """An unbound approval is denied before even a host-state inspection or mutation.

        A bare ``--approve`` and a made-up digest both fail at the first gate with nothing recorded as run.
        """
        calls = native()

        result = invoke(*extra)

        assert result["status"] == "denied"
        assert result["state_changed"] is False
        assert result["provider_call"] is False
        assert calls == []

    @pytest.mark.parametrize(
        "extra",
        [
            pytest.param([], id="configure"),
            pytest.param(["--action", "authenticate"], id="authenticate"),
            pytest.param(["--action", "verify-live"], id="verify-live"),
        ],
    )
    def test_valid_digest_is_refused_without_the_operator_terminal(
        self,
        setup: Any,
        invoke: Callable[..., dict[str, object]],
        approve: Callable[..., dict[str, object]],
        native: Callable[..., Calls],
        monkeypatch: pytest.MonkeyPatch,
        extra: list[str],
    ) -> None:
        """A digest the model itself planned cannot execute from piped streams, whatever the action.

        The plan is real and signed, so the only missing element is operator proof: an agent that replays its own digest
        through a captured shell must be stopped before inspection, locking, approval consumption or mutation.
        """
        calls = native()
        plan = invoke(*extra)
        calls.clear()
        monkeypatch.setattr(setup, "_operator_terminal", lambda: False)

        result = approve(plan["approval_digest"], *extra)

        assert result["status"] == "denied"
        assert result["classification"] == "operator-terminal-required"
        assert result["state_changed"] is False
        assert result["provider_call"] is False
        assert calls == []

    def test_digest_from_another_workspace_is_denied_before_any_native_subprocess(
        self,
        invoke: Callable[..., dict[str, object]],
        approve: Callable[..., dict[str, object]],
        native: Callable[..., Calls],
        tmp_path: Path,
    ) -> None:
        """A copied approval cannot authorize configuration in a different workspace.

        The digest binds the canonical workspace it was planned for; replaying it against a sibling folder is denied.
        """
        calls = native()
        first_workspace = tmp_path / "first"
        second_workspace = tmp_path / "second"
        first_workspace.mkdir()
        second_workspace.mkdir()
        plan = invoke(workspace=first_workspace)
        calls.clear()

        result = approve(plan["approval_digest"], workspace=second_workspace)

        assert result["status"] == "denied"
        assert result["state_changed"] is False
        assert calls == []

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(_issued_in_the_future, id="future-issued"),
            pytest.param(_with_excessive_lifetime, id="excessive-ttl"),
        ],
    )
    def test_future_or_overlong_approval_time_is_denied_before_probes_or_user_state(
        self,
        setup: Any,
        user_root: Path,
        invoke: Callable[..., dict[str, object]],
        approve: Callable[..., dict[str, object]],
        native: Callable[..., Calls],
        mutate: Callable[[dict[str, object]], None],
    ) -> None:
        """An approval with an impossible issue time or unbounded lifetime never reaches an execution gate.

        The payload is re-signed by the host key path, so only the time fields are invalid; nothing may run and no user
        state may appear beyond what planning already created.
        """
        calls = native()
        plan = invoke("--action", "authenticate")
        payload = setup._decode_approval(str(plan["approval_digest"]))
        assert payload is not None
        mutate(payload)
        invalid_approval = setup._encode_approval(payload)
        planned_state = _state_files(user_root)
        calls.clear()

        result = approve(invalid_approval, "--action", "authenticate")

        assert result["status"] == "denied"
        assert result["classification"] == "approval-time-invalid"
        assert result["provider_call"] is False
        assert result["state_changed"] is False
        assert calls == []
        assert _state_files(user_root) == planned_state

    def test_expired_approval_is_rejected_before_configuration_or_live_execution(
        self,
        setup: Any,
        invoke: Callable[..., dict[str, object]],
        approve: Callable[..., dict[str, object]],
        native: Callable[..., Calls],
        live_calls: list[tuple[tuple[object, ...], dict[str, object]]],
    ) -> None:
        """A syntactically valid but expired approval cannot replay a state-changing plan.

        The payload is re-encoded with ``expires_at`` in 1970; neither a native command nor a live probe may run.
        """
        calls = native(**CONFIGURED_PEER)
        plan = invoke("--action", "verify-live")
        payload = setup._decode_approval(str(plan["approval_digest"]))
        assert payload is not None
        assert isinstance(payload.get("expires_at"), int)
        payload["expires_at"] = 0
        expired = setup._encode_approval(payload)
        calls.clear()

        result = approve(expired, "--action", "verify-live")

        assert result["status"] == "denied"
        assert result["classification"] == "approval-expired"
        assert result["state_changed"] is False
        assert result["provider_call"] is False
        assert calls == []
        assert live_calls == []

    def test_reencoded_tampered_approval_without_host_key_is_denied_before_host_probes(
        self,
        setup: Any,
        invoke: Callable[..., dict[str, object]],
        approve: Callable[..., dict[str, object]],
        native: Callable[..., Calls],
    ) -> None:
        """A public checksum is not an approval signature once an attacker changes a bound field.

        The tampered payload gets a freshly computed checksum but no host-key signature, so it is denied before any host
        probe.
        """
        calls = native()
        plan = invoke("--action", "authenticate")
        payload = setup._decode_approval(str(plan["approval_digest"]))
        assert payload is not None
        payload["nonce"] = "attacker-reencoded-nonce"
        tampered = _reencode_untrusted_approval(payload)
        calls.clear()

        result = approve(tampered, "--action", "authenticate")

        assert result["status"] == "denied"
        assert result["provider_call"] is False
        assert result["state_changed"] is False
        assert calls == []

    def test_each_action_gets_its_own_digest(
        self,
        invoke: Callable[..., dict[str, object]],
        native: Callable[..., Calls],
        live_calls: list[tuple[tuple[object, ...], dict[str, object]]],
    ) -> None:
        """Configuration, authentication and live verification plans carry three different digests.

        Action names must be part of what the signature covers, otherwise one approval could be reused across them.
        """
        native(**CONFIGURED_PEER)

        digests = {
            str(invoke(*extra)["approval_digest"])
            for extra in ([], ["--action", "authenticate"], ["--action", "verify-live"])
        }

        assert len(digests) == 3
        assert live_calls == []

    @pytest.mark.parametrize("action", ["authenticate", "verify-live"])
    @pytest.mark.parametrize(
        "peer",
        [pytest.param({}, id="unconfigured-peer"), pytest.param(CONFIGURED_PEER, id="configured-peer")],
    )
    def test_configuration_digest_cannot_authorize_login_or_live(
        self,
        invoke: Callable[..., dict[str, object]],
        approve: Callable[..., dict[str, object]],
        native: Callable[..., Calls],
        live_calls: list[tuple[tuple[object, ...], dict[str, object]]],
        peer: dict[str, bool],
        action: str,
    ) -> None:
        """A non-sensitive configuration approval is denied for login and for paid inference.

        Neither the provider login nor the live probe may run when only the configuration plan was approved, whether or
        not the peer plugin is already configured.
        """
        calls = native(**peer)
        configuration_plan = invoke()
        calls.clear()

        result = approve(configuration_plan["approval_digest"], "--action", action)

        assert result["status"] == "denied"
        assert LOGIN_ARGV not in _commands(calls)
        assert live_calls == []

    @pytest.mark.parametrize("configuration_returncode", [0, 1])
    def test_configuration_approval_is_one_use_and_replay_stops_before_native_command(
        self,
        invoke: Callable[..., dict[str, object]],
        approve: Callable[..., dict[str, object]],
        native: Callable[..., Calls],
        configuration_returncode: int,
    ) -> None:
        """No approval, including one whose operation failed, can be replayed against host state.

        The first use consumes the digest whether the install succeeded or not; the replay must be blocked or denied
        before the native install runs again.
        """
        calls = native(configuration_returncode=configuration_returncode)
        plan = invoke()
        approve(plan["approval_digest"])
        calls.clear()

        replay = approve(plan["approval_digest"])

        assert replay["status"] in {"blocked", "denied"}
        assert replay["classification"] in {"approval-replayed", "mutation-locked-or-repeat-fault"}
        assert replay["state_changed"] is False
        assert _installs(calls) == []

    def test_concurrent_replay_check_cannot_allow_two_native_configurations(
        self,
        setup: Any,
        user_root: Path,
        invoke: Callable[..., dict[str, object]],
        native: Callable[..., Calls],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Two callers that both read an unused approval cannot each execute the same configuration.

        A barrier holds both threads at the exclusive lock creation so they truly race; exactly one may install and the
        other must be turned away with a non-zero exit.
        """
        calls = native()
        plan = invoke()
        argv = ["--current-host", "codex", "--workspace", str(tmp_path), "--approve", str(plan["approval_digest"])]
        calls.clear()
        _gate_lock_creation(setup, monkeypatch, _lock_path(user_root))

        exit_codes, errors, alive = _run_concurrently(setup, argv, workers=2)

        assert not any(alive)
        assert errors == []
        assert sorted(exit_codes) == [0, 2]
        assert _bridge_commands(calls) == [INSTALL_ARGV]


class TestExecution:
    """Approved plans run only their planned native command and report outcomes without overclaiming."""

    def test_approved_configuration_runs_only_the_planned_argv_and_never_authenticates(
        self,
        invoke: Callable[..., dict[str, object]],
        approve: Callable[..., dict[str, object]],
        native: Callable[..., Calls],
    ) -> None:
        """Approving configuration cannot expand into login or any unplanned host command.

        Only the planned install touches the Bridge plugin, and the result lists authentication and verification as
        remaining work.
        """
        calls = native()
        plan = invoke()
        calls.clear()

        result = approve(plan["approval_digest"])

        assert _bridge_commands(calls) == [INSTALL_ARGV]
        assert LOGIN_ARGV not in _commands(calls)
        assert result["state_changed"] is True
        assert result["authentication"] == "not-checked"
        assert result["status"] == "partial"
        assert result["remaining"] == ["authentication", "session-workspace-verification", "live-verification"]

    def test_native_configuration_failure_is_terminal_and_claims_no_state_change(
        self,
        invoke: Callable[..., dict[str, object]],
        approve: Callable[..., dict[str, object]],
        native: Callable[..., Calls],
    ) -> None:
        """A failed install command reports failure, no state change, and does not fall through to login.

        Reporting success or continuing to authentication after a failed host command would hide a broken install.
        """
        calls = native(configuration_returncode=1)
        plan = invoke()
        calls.clear()

        result = approve(plan["approval_digest"])

        assert result["status"] == "failed"
        assert result["classification"] == "configuration-failed"
        assert result["state_changed"] is False
        assert LOGIN_ARGV not in _commands(calls)

    @pytest.mark.parametrize(
        "peer",
        [
            pytest.param({}, id="unconfigured-peer"),
            pytest.param(CONFIGURED_PEER, id="configured-peer"),
        ],
    )
    def test_approved_authentication_launches_login_with_inherited_streams_and_no_live_probe(
        self,
        invoke: Callable[..., dict[str, object]],
        approve: Callable[..., dict[str, object]],
        native: Callable[..., Calls],
        live_calls: list[tuple[tuple[object, ...], dict[str, object]]],
        peer: dict[str, bool],
    ) -> None:
        """The authentication digest launches exactly one login on the operator's own streams and nothing else.

        The login runs with no captured stdin/stdout and no extra kwargs, so credentials stay with the provider; it is
        reported as a launched flow, never as authentication or session evidence, and no live probe starts.
        """
        calls = native(**peer)
        plan = invoke("--action", "authenticate")
        calls.clear()

        result = approve(plan["approval_digest"], "--action", "authenticate")

        assert [(command, kwargs) for command, kwargs in calls if command == LOGIN_ARGV] == [(LOGIN_ARGV, {})]
        assert result["status"] == "partial"
        assert result["authentication"] == "auth-flow-launched"
        assert result["verification_level"] in {"static", "host-authenticated"}
        assert result["provider_call"] is False
        assert result["ready_to_use"] is False
        assert live_calls == []

    def test_failed_authentication_process_is_not_reported_as_a_launched_flow(
        self,
        invoke: Callable[..., dict[str, object]],
        approve: Callable[..., dict[str, object]],
        native: Callable[..., Calls],
    ) -> None:
        """A rejected native login process is a failure, not a usable authentication flow.

        The login argv exits non-zero, so the result must be ``failed`` with authentication still unchecked.
        """
        native(authentication_returncode=1)
        plan = invoke("--action", "authenticate")

        result = approve(plan["approval_digest"], "--action", "authenticate")

        assert result["status"] == "failed"
        assert result["classification"] == "authentication-launch-failed"
        assert result["authentication"] == "not-checked"
        assert result["state_changed"] is False

    def test_approved_live_verification_runs_only_the_live_phase(
        self,
        invoke: Callable[..., dict[str, object]],
        approve: Callable[..., dict[str, object]],
        native: Callable[..., Calls],
        live_calls: list[tuple[tuple[object, ...], dict[str, object]]],
    ) -> None:
        """The live-verification digest runs the live probe and not the provider login.

        Action names must bind the executed phase, so approving paid inference never re-runs authentication.
        """
        calls = native(**CONFIGURED_PEER)
        plan = invoke("--action", "verify-live")
        calls.clear()

        result = approve(plan["approval_digest"], "--action", "verify-live")

        assert live_calls
        assert LOGIN_ARGV not in _commands(calls)
        assert result["provider_call"] is True

    @pytest.mark.parametrize(
        ("peer", "classification"),
        [
            pytest.param(
                {"claude_installed": False, "claude_authenticated": True},
                "configuration-needed",
                id="plugin-not-configured",
            ),
            pytest.param(
                {"claude_installed": True, "claude_authenticated": False},
                "authentication-needed",
                id="host-not-authenticated",
            ),
        ],
    )
    def test_live_approval_stops_before_provider_when_prerequisites_are_unproven(
        self,
        user_root: Path,
        invoke: Callable[..., dict[str, object]],
        approve: Callable[..., dict[str, object]],
        native: Callable[..., Calls],
        live_calls: list[tuple[tuple[object, ...], dict[str, object]]],
        peer: dict[str, bool],
        classification: str,
    ) -> None:
        """No paid live call starts when static plugin or authentication evidence is insufficient.

        The approval is valid, but the peer is unconfigured or logged out; the run must stop blocked with no native
        command, no live probe and no new state.
        """
        calls = native(**peer)
        plan = invoke("--action", "verify-live")
        planned_state = _state_files(user_root)
        calls.clear()

        result = approve(plan["approval_digest"], "--action", "verify-live")

        assert result["status"] == "blocked"
        assert result["classification"] == classification
        assert result["provider_call"] is False
        assert result["state_changed"] is False
        assert live_calls == []
        assert calls == []
        assert _state_files(user_root) == planned_state

    @pytest.mark.parametrize(
        ("inventory_after_configuration", "classification"),
        [
            pytest.param(False, "fresh-session-required", id="host-inventory-unchanged"),
            pytest.param(True, "configuration-verified", id="host-inventory-updated"),
        ],
    )
    def test_configuration_reinspects_host_inventory_before_reporting_outcome(
        self,
        invoke: Callable[..., dict[str, object]],
        approve: Callable[..., dict[str, object]],
        native: Callable[..., Calls],
        inventory_after_configuration: bool,
        classification: str,
    ) -> None:
        """Configuration re-reads the host plugin inventory after installing and reports what it found.

        The plan, drift check, lock-authority check and post-install probe each list the inventory (four calls); an
        unchanged inventory asks for a fresh session, an updated one verifies the configuration.
        """
        calls = native(inventory_after_configuration=inventory_after_configuration)
        plan = invoke()

        result = approve(plan["approval_digest"])

        assert result["status"] == "partial"
        assert result["classification"] == classification
        assert result["state_changed"] is True
        assert _commands(calls).count(["claude", "plugin", "list", "--json"]) == 4

    def test_approved_configuration_uses_the_network_sized_timeout(
        self,
        invoke: Callable[..., dict[str, object]],
        approve: Callable[..., dict[str, object]],
        native: Callable[..., Calls],
        setup: Any,
    ) -> None:
        """The approved marketplace install runs with the configure budget, not the 20 s probe timeout.

        A marketplace install downloads plugin payloads; under the shared probe timeout a slow link journaled a failure
        while the child could still complete, and the failed record then blocked the repeat of that install.
        """
        calls = native()
        plan = invoke()
        calls.clear()

        approve(plan["approval_digest"])

        install_kwargs = [kwargs for command, kwargs in calls if command == INSTALL_ARGV]
        assert [kwargs.get("timeout") for kwargs in install_kwargs] == [setup.CONFIGURE_TIMEOUT_SECONDS]

    def test_successful_live_verification_is_point_in_time_evidence_not_readiness(
        self,
        invoke: Callable[..., dict[str, object]],
        approve: Callable[..., dict[str, object]],
        native: Callable[..., Calls],
    ) -> None:
        """One successful paid probe is reported as live-verified but not as session or workspace readiness.

        The diagnose fake answers ok; the session-workspace check is still outstanding and ``ready_to_use`` stays off.
        """
        native(**CONFIGURED_PEER, live_success=True)
        plan = invoke("--action", "verify-live")

        result = approve(plan["approval_digest"], "--action", "verify-live")

        assert result["status"] == "partial"
        assert result["classification"] == "live-verified"
        assert result["authentication"] == "live-verified"
        assert result["verification_level"] == "live-verified"
        assert result["provider_call"] is True
        assert result["ready_to_use"] is False
        assert result["remaining"] == ["session-workspace-verification"]

    def test_required_live_verification_reports_failure_and_stays_non_ready(
        self,
        invoke: Callable[..., dict[str, object]],
        approve: Callable[..., dict[str, object]],
        native: Callable[..., Calls],
    ) -> None:
        """``--live required`` keeps live verification outstanding, and a failed probe fails the run.

        Required live evidence must not become optional once configuration and authentication pass.
        """
        native(**CONFIGURED_PEER, live_success=False)

        required_plan = invoke("--live", "required")
        live_plan = invoke("--action", "verify-live", "--live", "required")
        failed = approve(live_plan["approval_digest"], "--action", "verify-live", "--live", "required")

        assert required_plan["ready_to_use"] is False
        assert "live-verification" in required_plan["remaining"]
        assert failed["status"] == "failed"
        assert failed["provider_call"] is True
        assert failed["ready_to_use"] is False
        assert "live-verification" in failed["remaining"]


class TestStateAndLocks:
    """Journals and mutation locks keep repeated or concurrent setup from corrupting host state."""

    def test_failed_configuration_is_journaled_without_raw_command_and_not_retried_within_ttl(
        self,
        user_root: Path,
        invoke: Callable[..., dict[str, object]],
        approve: Callable[..., dict[str, object]],
        native: Callable[..., Calls],
    ) -> None:
        """A failed install is journaled by fingerprint only and the identical retry is blocked until the TTL passes.

        The journal must hold no argv (secret-capable command material), and a second approved attempt must not run the
        native install again.
        """
        calls = native(configuration_returncode=1)
        plan = invoke()
        approve(plan["approval_digest"])
        records = [json.loads(line) for line in _journal_path(user_root).read_text(encoding="utf-8").splitlines()]
        calls.clear()
        repeated_plan = invoke()

        repeated = approve(repeated_plan["approval_digest"])

        assert set(records[0]) == {
            "exit_classification",
            "fault_fingerprint",
            "operation_fingerprint",
            "recurrence_count",
            "result",
            "rollback",
            "state_fingerprint",
            "target",
            "timestamp",
            "verification_outcome",
        }
        assert records[0]["result"] == "failed"
        assert "argv" not in json.dumps(records)
        assert repeated["status"] == "blocked"
        assert repeated["classification"] == "mutation-locked-or-repeat-fault"
        assert _installs(calls) == []

    def test_expired_failure_record_allows_one_fresh_approved_attempt(
        self,
        user_root: Path,
        setup: Any,
        invoke: Callable[..., dict[str, object]],
        approve: Callable[..., dict[str, object]],
        native: Callable[..., Calls],
    ) -> None:
        """A failure record older than the retry TTL no longer blocks a newly approved identical operation.

        The permanent form of this block contradicted its own "wait and retry" guidance: with no expiry and no cleanup
        path the exact operation stayed locked forever while host state was unchanged.
        """
        calls = native(configuration_returncode=1)
        plan = invoke()
        approve(plan["approval_digest"])
        _age_journal_records(_journal_path(user_root), setup.FAILED_OPERATION_TTL_SECONDS + 1)
        retry_plan = invoke()
        calls.clear()

        retry = approve(retry_plan["approval_digest"])

        assert retry["classification"] == "configuration-failed"
        assert _installs(calls) == [INSTALL_ARGV]

    def test_concurrent_mutation_lock_stops_before_native_configuration(
        self,
        user_root: Path,
        invoke: Callable[..., dict[str, object]],
        approve: Callable[..., dict[str, object]],
        native: Callable[..., Calls],
    ) -> None:
        """A held mutation lock blocks a second setup from mutating the same selected host.

        The lock appears after planning, so the approval is valid and only the lock stands in the way.
        """
        calls = native()
        plan = invoke()
        lock = _lock_path(user_root)
        lock.parent.mkdir(parents=True)
        lock.write_text("active\n", encoding="utf-8", newline="\n")
        calls.clear()

        result = approve(plan["approval_digest"])

        assert result["status"] == "blocked"
        assert result["classification"] == "mutation-locked-or-repeat-fault"
        assert _installs(calls) == []

    def test_user_scoped_lock_blocks_the_same_target_from_another_workspace(
        self,
        user_root: Path,
        invoke: Callable[..., dict[str, object]],
        approve: Callable[..., dict[str, object]],
        native: Callable[..., Calls],
        tmp_path: Path,
    ) -> None:
        """The lock is per user-scoped target, so another workspace cannot mutate the same host setting.

        A workspace-local lock would let two projects configure one user-scoped plugin concurrently.
        """
        calls = native()
        first_workspace = tmp_path / "first"
        second_workspace = tmp_path / "second"
        first_workspace.mkdir()
        second_workspace.mkdir()
        first_plan = invoke(workspace=first_workspace)
        lock = _lock_path(user_root)
        lock.parent.mkdir(parents=True)
        lock.write_text("active\n", encoding="utf-8", newline="\n")
        calls.clear()
        second_plan = invoke(workspace=second_workspace)

        result = approve(second_plan["approval_digest"], workspace=second_workspace)

        assert first_plan["target"] == second_plan["target"] == "claude"
        assert result["status"] == "blocked"
        assert result["classification"] == "mutation-locked-or-repeat-fault"
        assert _installs(calls) == []

    def test_abandoned_lock_from_a_crashed_setup_no_longer_blocks(
        self,
        setup: Any,
        user_root: Path,
        invoke: Callable[..., dict[str, object]],
        approve: Callable[..., dict[str, object]],
        native: Callable[..., Calls],
    ) -> None:
        """An approved setup proceeds once a crashed process's unreleased lock has gone stale.

        The lock's mtime is moved past the stale bound; setup must reclaim it, run the install and leave no lock or
        ``.stale`` leftover behind.
        """
        calls = native()
        plan = invoke()
        lock = _lock_path(user_root)
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("active\n", encoding="utf-8", newline="\n")
        abandoned_at = time.time() - setup.MUTATION_LOCK_STALE_SECONDS - 60
        os.utime(lock, (abandoned_at, abandoned_at))
        calls.clear()

        result = approve(plan["approval_digest"])

        assert result.get("classification") != "mutation-locked-or-repeat-fault"
        assert _installs(calls) == [INSTALL_ARGV]
        assert not lock.exists()
        assert not list(lock.parent.glob("*.stale"))

    def test_oversized_journal_reads_its_newest_records(
        self, setup: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An append-only journal past the read bound still yields its newest records instead of failing forever.

        Replay protection must keep working: the newest approval reads as replayed and a fresh one is consumed.
        """
        monkeypatch.setattr(setup, "MAX_STATE_RECORD_BYTES", 4096)
        journal = tmp_path / "claude-user.jsonl"
        old = [{"approval_fingerprint": f"old-{index}", "timestamp": index} for index in range(200)]
        new = [{"approval_fingerprint": "newest", "timestamp": 10_000}]
        journal.write_text("".join(json.dumps(record) + "\n" for record in old + new), encoding="utf-8", newline="\n")
        assert journal.stat().st_size > setup.MAX_STATE_RECORD_BYTES

        records = setup._read_records(journal)

        assert records is not None
        assert records[-1] == new[0]
        assert {"approval_fingerprint": "old-0", "timestamp": 0} not in records
        assert setup._consume_approval_once(journal, "newest") == "replayed"
        assert setup._consume_approval_once(journal, "fresh") == "consumed"

    def test_oversized_journal_without_a_record_boundary_fails_closed(
        self, setup: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A tampered journal whose tail holds no complete record reads as unreadable, not as empty state.

        Reading it as empty would forget every consumed approval and re-open replay.
        """
        monkeypatch.setattr(setup, "MAX_STATE_RECORD_BYTES", 64)
        journal = tmp_path / "claude-user.jsonl"
        journal.write_text("x" * 200, encoding="utf-8", newline="\n")

        assert setup._read_records(journal) is None

    def test_records_are_regular_files_inside_the_user_state_root(
        self,
        user_root: Path,
        invoke: Callable[..., dict[str, object]],
        approve: Callable[..., dict[str, object]],
        native: Callable[..., Calls],
    ) -> None:
        """Journals are plain files under the user-state root, never symlinks or workspace-local state.

        Replay protection stored in the workspace could be reset by whoever controls it.
        """
        native()
        plan = invoke()

        approve(plan["approval_digest"])

        records = list(user_root.rglob("*.jsonl"))
        assert records
        assert all(path.is_file() and not path.is_symlink() for path in records)
        assert all(path.parent.is_relative_to(user_root) for path in records)

    @pytest.mark.skipif(not FILE_SYMLINKS_SUPPORTED, reason="requires file symlink creation capability")
    def test_symlinked_journal_is_rejected_before_record_or_native_mutation(
        self,
        user_root: Path,
        invoke: Callable[..., dict[str, object]],
        approve: Callable[..., dict[str, object]],
        native: Callable[..., Calls],
        tmp_path: Path,
    ) -> None:
        """A journal replaced by a symlink blocks setup before any state write or native mutation.

        Following the link would write setup records to an attacker-chosen file outside the state root.
        """
        calls = native()
        journal = _journal_path(user_root)
        outside = tmp_path / "outside.jsonl"
        journal.parent.mkdir(parents=True)
        outside.write_text("outside\n", encoding="utf-8", newline="\n")
        journal.symlink_to(outside)
        plan = invoke()
        calls.clear()

        result = approve(plan["approval_digest"])

        assert result["status"] == "blocked"
        assert result["state_changed"] is False
        assert outside.read_text(encoding="utf-8") == "outside\n"
        assert _installs(calls) == []

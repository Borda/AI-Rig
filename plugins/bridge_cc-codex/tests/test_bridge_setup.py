"""Public-contract tests for Bridge's credential-free setup planner and executor."""

from __future__ import annotations

import importlib
import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
from typing import Any

import pytest


BIN_ROOT = Path(__file__).resolve().parents[1] / "bin"
SETUP_PATH = BIN_ROOT / "bridge_setup.py"
if str(BIN_ROOT) not in sys.path:
    sys.path.insert(0, str(BIN_ROOT))


def _supports_file_symlinks() -> bool:
    """Probe file-symlink capability once so hostile-link coverage is portable."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        target = root / "target"
        link = root / "link"
        target.write_text("target\n", encoding="utf-8", newline="\n")
        try:
            link.symlink_to(target)
        except OSError:
            return False
        return link.is_symlink()


FILE_SYMLINKS_SUPPORTED = _supports_file_symlinks()


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
) -> list[tuple[list[str], dict[str, object]]]:
    """Mock exact installed-CLI probes while retaining a visible command ledger."""
    calls: list[tuple[list[str], dict[str, object]]] = []
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
        if command in (["claude", "auth", "login"], ["codex", "login"]):
            return _completed(command, returncode=authentication_returncode)
        if command == ["claude", "plugin", "install", "bridge@borda-ai-rig", "--scope", "user"]:
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
    monkeypatch.setattr(setup.shutil, "which", lambda executable: executable)
    return calls


def _invoke(
    setup: Any,
    capsys: pytest.CaptureFixture[str],
    workspace: Path,
    *extra: str,
) -> dict[str, object]:
    """Run one public setup invocation and return its sole JSON result."""
    exit_code = setup.main(["--current-host", "codex", "--workspace", str(workspace), *extra])
    captured = capsys.readouterr()
    assert exit_code in {0, 2}
    assert captured.err == ""
    result = json.loads(captured.out)
    assert isinstance(result, dict)
    return result


def _redirect_user_state(setup: Any, monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Keep approval, lock, and journal tests inside a disposable user-state root."""
    monkeypatch.setattr(setup, "_user_state_root", lambda: root)


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


def test_default_plan_resolves_peer_and_binds_exact_native_operation_to_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Prevent a default setup from silently selecting a host, scope, workspace, or native command."""
    setup = _setup_module()
    _redirect_user_state(setup, monkeypatch, tmp_path / "user-state")
    calls = _install_native_boundary(setup, monkeypatch)

    result = _invoke(setup, capsys, tmp_path)

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
            "argv": ["claude", "plugin", "install", "bridge@borda-ai-rig", "--scope", "user"],
            "credential_behavior": "none",
            "external_capability": "configured-marketplace-snapshot",
        }
    ]
    assert [command for command, _ in calls] == [
        ["codex", "--version"],
        ["claude", "--version"],
        ["claude", "plugin", "list", "--json"],
        ["claude", "auth", "status", "--json"],
    ]


def test_actionable_plan_creates_only_safe_key_state_and_check_creates_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Prevent an approval signer from creating journals or locks during planning or any state during check."""
    setup = _setup_module()
    user_root = tmp_path / "user-state"
    _redirect_user_state(setup, monkeypatch, user_root)
    _install_native_boundary(setup, monkeypatch)

    plan = _invoke(setup, capsys, tmp_path)
    key_files = [path for path in user_root.rglob("*") if path.is_file()]
    check_root = tmp_path / "check-state"
    _redirect_user_state(setup, monkeypatch, check_root)
    check = _invoke(setup, capsys, tmp_path, "--action", "check")

    assert plan["status"] == check["status"] == "partial"
    assert len(key_files) == 1
    assert key_files[0].is_file() and not key_files[0].is_symlink()
    assert key_files[0].stat().st_size == setup.APPROVAL_KEY_BYTES
    if os.name != "nt":
        assert stat.S_IMODE(key_files[0].stat().st_mode) == 0o600
    assert not (user_root / "bridge-setup" / "locks").exists()
    assert not (user_root / "bridge-setup" / "records").exists()
    assert not check_root.exists()


@pytest.mark.parametrize(
    ("current_host", "target", "direction"),
    (
        pytest.param("codex", "claude", "codex_to_claude", id="codex-to-claude"),
        pytest.param("claude", "codex", "claude_to_codex", id="claude-to-codex"),
    ),
)
def test_peer_target_resolution_is_explicit_for_both_loaded_hosts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    current_host: str,
    target: str,
    direction: str,
) -> None:
    """Prevent peer setup from accidentally configuring the loaded invocation surface."""
    setup = _setup_module()
    _redirect_user_state(setup, monkeypatch, tmp_path / "user-state")
    _install_native_boundary(setup, monkeypatch)

    exit_code = setup.main(["--current-host", current_host, "--workspace", str(tmp_path)])
    result = json.loads(capsys.readouterr().out)

    assert exit_code in {0, 2}
    assert result["current_host"] == current_host
    assert result["target"] == target
    assert result["direction"] == direction


def test_capability_matrix_contains_only_the_version_gated_native_operations() -> None:
    """Prevent guessed login, plugin-enable, or scope commands from becoming executable setup paths."""
    setup = _setup_module()

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
            "authentication_argv": ["claude", "auth", "login"],
            "scopes": ["user", "project", "local"],
        },
    }


@pytest.mark.parametrize("approval", (pytest.param(None, id="missing"), "wrong-digest"))
def test_apply_rejects_missing_or_wrong_approval_before_any_native_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    approval: str | None,
) -> None:
    """Prevent an unbound approval from triggering even a host-state inspection or mutation."""
    setup = _setup_module()
    _redirect_user_state(setup, monkeypatch, tmp_path / "user-state")
    calls = _install_native_boundary(setup, monkeypatch)

    extra = ["--approve"] if approval is None else ["--approve", approval]
    result = _invoke(setup, capsys, tmp_path, *extra)

    assert result["status"] == "denied"
    assert result["state_changed"] is False
    assert result["provider_call"] is False
    assert calls == []


def test_apply_rejects_a_digest_from_another_workspace_before_any_native_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Prevent a copied approval from authorizing configuration in a different workspace."""
    setup = _setup_module()
    _redirect_user_state(setup, monkeypatch, tmp_path / "user-state")
    calls = _install_native_boundary(setup, monkeypatch)
    first_workspace = tmp_path / "first"
    second_workspace = tmp_path / "second"
    first_workspace.mkdir()
    second_workspace.mkdir()

    plan = _invoke(setup, capsys, first_workspace)
    calls.clear()
    result = _invoke(setup, capsys, second_workspace, "--approve", str(plan["approval_digest"]))

    assert result["status"] == "denied"
    assert result["state_changed"] is False
    assert calls == []


def test_approved_configuration_executes_only_the_planned_native_argv_and_never_authenticates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Prevent an approval for configuration from expanding into login or an unplanned host command."""
    setup = _setup_module()
    _redirect_user_state(setup, monkeypatch, tmp_path / "user-state")
    calls = _install_native_boundary(setup, monkeypatch)

    plan = _invoke(setup, capsys, tmp_path)
    calls.clear()
    result = _invoke(setup, capsys, tmp_path, "--approve", str(plan["approval_digest"]))

    executed = [command for command, _ in calls if "bridge@borda-ai-rig" in command]
    assert executed == [["claude", "plugin", "install", "bridge@borda-ai-rig", "--scope", "user"]]
    assert ["claude", "auth", "login"] not in [command for command, _ in calls]
    assert result["state_changed"] is True
    assert result["authentication"] == "not-checked"
    assert result["status"] == "partial"
    assert result["remaining"] == ["authentication", "session-workspace-verification", "live-verification"]


def test_native_configuration_failure_is_terminal_and_does_not_claim_state_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Prevent a failed host command from reporting configuration success or falling through to login."""
    setup = _setup_module()
    _redirect_user_state(setup, monkeypatch, tmp_path / "user-state")
    calls = _install_native_boundary(setup, monkeypatch, configuration_returncode=1)

    plan = _invoke(setup, capsys, tmp_path)
    calls.clear()
    result = _invoke(setup, capsys, tmp_path, "--approve", str(plan["approval_digest"]))

    assert result["status"] == "failed"
    assert result["classification"] == "configuration-failed"
    assert result["state_changed"] is False
    assert ["claude", "auth", "login"] not in [command for command, _ in calls]


def test_authentication_action_needs_its_own_digest_and_inherits_operator_streams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Prevent a configuration approval from authorizing login output or credential handling."""
    setup = _setup_module()
    _redirect_user_state(setup, monkeypatch, tmp_path / "user-state")
    calls = _install_native_boundary(setup, monkeypatch)

    configuration_plan = _invoke(setup, capsys, tmp_path)
    authentication_plan = _invoke(setup, capsys, tmp_path, "--action", "authenticate")
    calls.clear()
    denied = _invoke(
        setup,
        capsys,
        tmp_path,
        "--action",
        "authenticate",
        "--approve",
        str(configuration_plan["approval_digest"]),
    )

    assert denied["status"] == "denied"
    assert ["claude", "auth", "login"] not in [command for command, _ in calls]
    calls.clear()
    result = _invoke(
        setup,
        capsys,
        tmp_path,
        "--action",
        "authenticate",
        "--approve",
        str(authentication_plan["approval_digest"]),
    )

    login_calls = [(command, kwargs) for command, kwargs in calls if command == ["claude", "auth", "login"]]
    assert login_calls == [(["claude", "auth", "login"], {})]
    assert result["status"] == "partial"
    assert result["authentication"] == "auth-flow-launched"
    assert result["provider_call"] is False
    assert result["ready_to_use"] is False


def test_failed_authentication_process_does_not_claim_that_the_flow_launched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Prevent a rejected native login process from being reported as a usable authentication flow."""
    setup = _setup_module()
    _redirect_user_state(setup, monkeypatch, tmp_path / "user-state")
    _install_native_boundary(setup, monkeypatch, authentication_returncode=1)

    plan = _invoke(setup, capsys, tmp_path, "--action", "authenticate")
    result = _invoke(
        setup,
        capsys,
        tmp_path,
        "--action",
        "authenticate",
        "--approve",
        str(plan["approval_digest"]),
    )

    assert result["status"] == "failed"
    assert result["classification"] == "authentication-launch-failed"
    assert result["authentication"] == "not-checked"
    assert result["state_changed"] is False


def test_failed_configuration_is_journaled_without_raw_command_and_not_retried_within_backoff_ttl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Prevent repeated repair attempts and secret-capable command material from entering setup state."""
    setup = _setup_module()
    user_root = tmp_path / "user-state"
    _redirect_user_state(setup, monkeypatch, user_root)
    calls = _install_native_boundary(setup, monkeypatch, configuration_returncode=1)

    plan = _invoke(setup, capsys, tmp_path)
    _invoke(setup, capsys, tmp_path, "--approve", str(plan["approval_digest"]))
    journal = user_root / "bridge-setup" / "records" / "claude-user.jsonl"
    records = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    calls.clear()
    repeated_plan = _invoke(setup, capsys, tmp_path)
    repeated = _invoke(setup, capsys, tmp_path, "--approve", str(repeated_plan["approval_digest"]))

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
    assert not any(command[:3] == ["claude", "plugin", "install"] for command, _ in calls)


def test_concurrent_mutation_lock_stops_before_native_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Prevent two setup processes from mutating the same selected host concurrently."""
    setup = _setup_module()
    user_root = tmp_path / "user-state"
    _redirect_user_state(setup, monkeypatch, user_root)
    calls = _install_native_boundary(setup, monkeypatch)
    plan = _invoke(setup, capsys, tmp_path)
    lock = user_root / "bridge-setup" / "locks" / "claude-user.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("active\n", encoding="utf-8", newline="\n")
    calls.clear()

    result = _invoke(setup, capsys, tmp_path, "--approve", str(plan["approval_digest"]))

    assert result["status"] == "blocked"
    assert result["classification"] == "mutation-locked-or-repeat-fault"
    assert not any(command[:3] == ["claude", "plugin", "install"] for command, _ in calls)


@pytest.mark.parametrize("sensitive_option", ("--token", "--access-token", "--device-code"))
def test_cli_rejects_sensitive_values_without_echo_or_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    sensitive_option: str,
) -> None:
    """Prevent credentials or browser codes from being accepted or reflected by setup parsing."""
    setup = _setup_module()
    _redirect_user_state(setup, monkeypatch, tmp_path / "user-state")
    calls = _install_native_boundary(setup, monkeypatch)
    secret = "sensitive-value-must-not-escape"

    result = _invoke(setup, capsys, tmp_path, sensitive_option, secret)
    rendered = json.dumps(result, sort_keys=True)

    assert result["status"] == "blocked"
    assert secret not in rendered
    assert calls == []
    # This pre-parse rejection is the one result built before arguments are
    # known; its placeholder values must stay inside the published contract.
    schema = json.loads((SETUP_PATH.parents[1] / "schemas" / "setup-result.schema.json").read_text(encoding="utf-8"))
    requested_properties = schema["properties"]["requested"]["properties"]
    for field, value in result["requested"].items():
        assert value in requested_properties[field]["enum"], field


def test_malformed_inventory_or_failed_probe_fails_closed_without_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Prevent malformed host output from being treated as an empty inventory and repaired blindly."""
    setup = _setup_module()
    _redirect_user_state(setup, monkeypatch, tmp_path / "user-state")
    calls = _install_native_boundary(setup, monkeypatch, malformed_claude_inventory=True)

    result = _invoke(setup, capsys, tmp_path)

    assert result["status"] == "unsupported"
    assert result["ready_to_use"] is False
    assert result["state_changed"] is False
    assert not any("bridge@borda-ai-rig" in command for command, _ in calls)


def test_missing_cli_fails_closed_without_guessing_an_install_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Prevent a missing peer executable from being misclassified as a repairable empty inventory."""
    setup = _setup_module()
    _redirect_user_state(setup, monkeypatch, tmp_path / "user-state")
    calls = _install_native_boundary(setup, monkeypatch)

    def _missing_claude(executable: str) -> str | None:
        """Hide Claude while leaving Codex discoverable on the fake PATH."""
        return None if executable == "claude" else executable

    monkeypatch.setattr(setup.shutil, "which", _missing_claude)
    result = _invoke(setup, capsys, tmp_path)

    assert result["status"] == "blocked"
    assert result["classification"] == "missing-prerequisite"
    assert result["ready_to_use"] is False
    assert result["state_changed"] is False
    assert calls == []


def test_explicit_current_host_target_is_bootstrap_required_without_mutation_or_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Prevent a loaded setup skill from attempting to bootstrap its own unavailable invocation surface."""
    setup = _setup_module()
    _redirect_user_state(setup, monkeypatch, tmp_path / "user-state")
    calls = _install_native_boundary(setup, monkeypatch)

    result = _invoke(setup, capsys, tmp_path, "--target", "codex")

    assert result["status"] == "manual"
    assert result["classification"] == "bootstrap-required"
    assert result["state_changed"] is False
    assert result["provider_call"] is False
    assert not any("bridge@borda-ai-rig" in command for command, _ in calls)
    assert ["codex", "login"] not in [command for command, _ in calls]


def test_installed_authenticated_peer_reports_host_authentication_but_not_session_or_inference_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Prevent host-authenticated evidence from being hidden or overstated as session/live readiness."""
    setup = _setup_module()
    _redirect_user_state(setup, monkeypatch, tmp_path / "user-state")
    _install_native_boundary(setup, monkeypatch, claude_installed=True, claude_authenticated=True)

    result = _invoke(setup, capsys, tmp_path)

    assert result["status"] == "partial"
    assert result["authentication"] == "host-authenticated"
    assert result["verification_level"] == "host-authenticated"
    assert result["ready_to_use"] is False
    assert "authentication" not in result["remaining"]
    assert result["remaining"] == ["session-workspace-verification", "live-verification"]


def test_authentication_plan_contains_only_the_exact_provider_owned_login_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Prevent an authentication plan from carrying a plugin mutation under the same approval digest."""
    setup = _setup_module()
    _redirect_user_state(setup, monkeypatch, tmp_path / "user-state")
    _install_native_boundary(setup, monkeypatch)

    result = _invoke(setup, capsys, tmp_path, "--action", "authenticate")

    assert result["operations"] == [
        {
            "action": "authenticate",
            "argv": ["claude", "auth", "login"],
            "credential_behavior": "provider-owned-interactive-no-capture",
            "external_capability": "provider-authentication",
        }
    ]
    assert not any("bridge@borda-ai-rig" in operation["argv"] for operation in result["operations"])


def test_live_plan_contains_only_the_separately_approved_peer_inference_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Prevent a live-verification plan from bundling plugin configuration with a paid provider request."""
    setup = _setup_module()
    _redirect_user_state(setup, monkeypatch, tmp_path / "user-state")
    _install_native_boundary(setup, monkeypatch)

    result = _invoke(setup, capsys, tmp_path, "--action", "verify-live")

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
    assert not any("bridge@borda-ai-rig" in operation["argv"] for operation in result["operations"])


@pytest.mark.parametrize(
    ("claude_installed", "claude_authenticated", "classification"),
    (
        pytest.param(False, True, "configuration-needed", id="plugin-not-configured"),
        pytest.param(True, False, "authentication-needed", id="host-not-authenticated"),
    ),
)
def test_live_approval_stops_before_provider_when_prerequisites_are_unproven(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    claude_installed: bool,
    claude_authenticated: bool,
    classification: str,
) -> None:
    """Prevent a paid live call when static plugin or authentication evidence is insufficient."""
    setup = _setup_module()
    user_root = tmp_path / "user-state"
    _redirect_user_state(setup, monkeypatch, user_root)
    calls = _install_native_boundary(
        setup,
        monkeypatch,
        claude_installed=claude_installed,
        claude_authenticated=claude_authenticated,
    )
    live_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(
        setup,
        "_verify_live",
        lambda *args, **kwargs: live_calls.append((args, kwargs)) or True,
    )
    plan = _invoke(setup, capsys, tmp_path, "--action", "verify-live")
    planned_state = sorted(path.relative_to(user_root) for path in user_root.rglob("*") if path.is_file())
    calls.clear()

    result = _invoke(setup, capsys, tmp_path, "--action", "verify-live", "--approve", str(plan["approval_digest"]))

    assert result["status"] == "blocked"
    assert result["classification"] == classification
    assert result["provider_call"] is False
    assert result["state_changed"] is False
    assert live_calls == []
    assert calls == []
    assert sorted(path.relative_to(user_root) for path in user_root.rglob("*") if path.is_file()) == planned_state


def test_authentication_launch_never_claims_a_non_static_verification_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Prevent process launch from being represented as authentication or session verification evidence."""
    setup = _setup_module()
    _redirect_user_state(setup, monkeypatch, tmp_path / "user-state")
    _install_native_boundary(setup, monkeypatch)
    plan = _invoke(setup, capsys, tmp_path, "--action", "authenticate")

    result = _invoke(
        setup,
        capsys,
        tmp_path,
        "--action",
        "authenticate",
        "--approve",
        str(plan["approval_digest"]),
    )

    assert result["authentication"] == "auth-flow-launched"
    assert result["verification_level"] in {"static", "host-authenticated"}


@pytest.mark.parametrize("invalid_time", ("future-issued", "excessive-ttl"))
def test_future_or_overlong_approval_time_is_denied_before_probes_or_user_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    invalid_time: str,
) -> None:
    """Prevent an approval with impossible issue time or unbounded lifetime from reaching any execution gate."""
    setup = _setup_module()
    user_root = tmp_path / "user-state"
    _redirect_user_state(setup, monkeypatch, user_root)
    calls = _install_native_boundary(setup, monkeypatch)
    plan = _invoke(setup, capsys, tmp_path, "--action", "authenticate")
    payload = setup._decode_approval(str(plan["approval_digest"]))
    assert payload is not None
    issued_at = payload["issued_at"]
    assert isinstance(issued_at, int)
    if invalid_time == "future-issued":
        payload["issued_at"] = int(payload["expires_at"])
        payload["expires_at"] = int(payload["expires_at"]) + 300
    else:
        payload["expires_at"] = issued_at + 301
    invalid_approval = setup._encode_approval(payload)
    planned_state = sorted(path.relative_to(user_root) for path in user_root.rglob("*") if path.is_file())
    calls.clear()

    result = _invoke(setup, capsys, tmp_path, "--action", "authenticate", "--approve", invalid_approval)

    assert result["status"] == "denied"
    assert result["classification"] == "approval-time-invalid"
    assert result["provider_call"] is False
    assert result["state_changed"] is False
    assert calls == []
    assert sorted(path.relative_to(user_root) for path in user_root.rglob("*") if path.is_file()) == planned_state


def test_action_bound_approvals_cannot_cross_from_configuration_to_authentication_or_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Prevent a non-sensitive configuration approval from authorizing login or paid inference."""
    setup = _setup_module()
    _redirect_user_state(setup, monkeypatch, tmp_path / "user-state")
    calls = _install_native_boundary(setup, monkeypatch, claude_installed=True, claude_authenticated=True)
    live_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def _fake_live(*args: object, **kwargs: object) -> bool:
        """Record live-verification attempts without contacting a provider."""
        live_calls.append((args, kwargs))
        return True

    monkeypatch.setattr(setup, "_verify_live", _fake_live)
    configuration_plan = _invoke(setup, capsys, tmp_path)
    authentication_plan = _invoke(setup, capsys, tmp_path, "--action", "authenticate")
    live_plan = _invoke(setup, capsys, tmp_path, "--action", "verify-live")

    assert (
        len(
            {
                str(configuration_plan["approval_digest"]),
                str(authentication_plan["approval_digest"]),
                str(live_plan["approval_digest"]),
            }
        )
        == 3
    )
    calls.clear()
    rejected_auth = _invoke(
        setup,
        capsys,
        tmp_path,
        "--action",
        "authenticate",
        "--approve",
        str(configuration_plan["approval_digest"]),
    )
    rejected_live = _invoke(
        setup,
        capsys,
        tmp_path,
        "--action",
        "verify-live",
        "--approve",
        str(configuration_plan["approval_digest"]),
    )

    assert rejected_auth["status"] == rejected_live["status"] == "denied"
    assert ["claude", "auth", "login"] not in [command for command, _ in calls]
    assert live_calls == []


def test_action_specific_authentication_and_live_approvals_execute_only_their_exact_bound_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Prevent action names from becoming decorative while execution expands beyond the approved phase."""
    setup = _setup_module()
    _redirect_user_state(setup, monkeypatch, tmp_path / "user-state")
    calls = _install_native_boundary(setup, monkeypatch, claude_installed=True, claude_authenticated=True)
    live_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def _fake_live(*args: object, **kwargs: object) -> bool:
        """Record live-verification attempts without contacting a provider."""
        live_calls.append((args, kwargs))
        return True

    monkeypatch.setattr(setup, "_verify_live", _fake_live)
    authentication_plan = _invoke(setup, capsys, tmp_path, "--action", "authenticate")
    calls.clear()
    authentication = _invoke(
        setup,
        capsys,
        tmp_path,
        "--action",
        "authenticate",
        "--approve",
        str(authentication_plan["approval_digest"]),
    )

    assert [(command, kwargs) for command, kwargs in calls if command == ["claude", "auth", "login"]] == [
        (["claude", "auth", "login"], {})
    ]
    assert authentication["authentication"] == "auth-flow-launched"
    assert live_calls == []
    live_plan = _invoke(setup, capsys, tmp_path, "--action", "verify-live")
    calls.clear()
    live = _invoke(
        setup,
        capsys,
        tmp_path,
        "--action",
        "verify-live",
        "--approve",
        str(live_plan["approval_digest"]),
    )

    assert live_calls
    assert ["claude", "auth", "login"] not in [command for command, _ in calls]
    assert live["provider_call"] is True


def test_expired_approval_is_rejected_before_configuration_or_live_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Prevent a syntactically valid but expired approval from replaying a state-changing plan."""
    setup = _setup_module()
    _redirect_user_state(setup, monkeypatch, tmp_path / "user-state")
    calls = _install_native_boundary(setup, monkeypatch, claude_installed=True, claude_authenticated=True)
    live_calls: list[object] = []
    monkeypatch.setattr(setup, "_verify_live", lambda *args, **kwargs: live_calls.append(args) or True)
    plan = _invoke(setup, capsys, tmp_path, "--action", "verify-live")
    payload = setup._decode_approval(str(plan["approval_digest"]))
    assert payload is not None
    assert isinstance(payload.get("expires_at"), int)
    payload["expires_at"] = 0
    expired = setup._encode_approval(payload)
    calls.clear()

    result = _invoke(setup, capsys, tmp_path, "--action", "verify-live", "--approve", expired)

    assert result["status"] == "denied"
    assert result["classification"] == "approval-expired"
    assert result["state_changed"] is False
    assert result["provider_call"] is False
    assert calls == []
    assert live_calls == []


@pytest.mark.parametrize("configuration_returncode", (0, 1))
def test_configuration_approval_is_one_use_and_replay_stops_before_native_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    configuration_returncode: int,
) -> None:
    """Prevent any approval, including a failed operation, from being replayed against host state."""
    setup = _setup_module()
    _redirect_user_state(setup, monkeypatch, tmp_path / "user-state")
    calls = _install_native_boundary(setup, monkeypatch, configuration_returncode=configuration_returncode)
    plan = _invoke(setup, capsys, tmp_path)

    _invoke(setup, capsys, tmp_path, "--approve", str(plan["approval_digest"]))
    calls.clear()
    replay = _invoke(setup, capsys, tmp_path, "--approve", str(plan["approval_digest"]))

    assert replay["status"] in {"blocked", "denied"}
    assert replay["classification"] in {"approval-replayed", "mutation-locked-or-repeat-fault"}
    assert replay["state_changed"] is False
    assert not any(command[:3] == ["claude", "plugin", "install"] for command, _ in calls)


@pytest.mark.parametrize(
    ("inventory_after_configuration", "classification"),
    (
        pytest.param(False, "fresh-session-required", id="host-inventory-unchanged"),
        pytest.param(True, "configuration-verified", id="host-inventory-updated"),
    ),
)
def test_configuration_reinspects_host_inventory_before_reporting_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    inventory_after_configuration: bool,
    classification: str,
) -> None:
    """Prevent configuration from skipping the plan, drift, lock-authority, or post-operation inventory proof."""
    setup = _setup_module()
    _redirect_user_state(setup, monkeypatch, tmp_path / "user-state")
    calls = _install_native_boundary(
        setup,
        monkeypatch,
        inventory_after_configuration=inventory_after_configuration,
    )
    plan = _invoke(setup, capsys, tmp_path)
    result = _invoke(setup, capsys, tmp_path, "--approve", str(plan["approval_digest"]))

    assert result["status"] == "partial"
    assert result["classification"] == classification
    assert result["state_changed"] is True
    assert [command for command, _ in calls].count(["claude", "plugin", "list", "--json"]) == 4


def test_user_scoped_lock_blocks_the_same_target_and_scope_from_another_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Prevent workspace-local locks from allowing concurrent mutation of one user-scoped host setting."""
    setup = _setup_module()
    user_root = tmp_path / "user-state"
    _redirect_user_state(setup, monkeypatch, user_root)
    calls = _install_native_boundary(setup, monkeypatch)
    first_workspace = tmp_path / "first"
    second_workspace = tmp_path / "second"
    first_workspace.mkdir()
    second_workspace.mkdir()
    first_plan = _invoke(setup, capsys, first_workspace)
    lock = user_root / "bridge-setup" / "locks" / "claude-user.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("active\n", encoding="utf-8", newline="\n")
    calls.clear()
    second_plan = _invoke(setup, capsys, second_workspace)

    result = _invoke(setup, capsys, second_workspace, "--approve", str(second_plan["approval_digest"]))

    assert first_plan["target"] == second_plan["target"] == "claude"
    assert result["status"] == "blocked"
    assert result["classification"] == "mutation-locked-or-repeat-fault"
    assert not any(command[:3] == ["claude", "plugin", "install"] for command, _ in calls)


def test_user_state_records_are_regular_files_and_replay_protection_is_not_workspace_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Prevent journals from becoming implicit symlink-capable files or workspace-local approval state."""
    setup = _setup_module()
    user_root = tmp_path / "user-state"
    _redirect_user_state(setup, monkeypatch, user_root)
    _install_native_boundary(setup, monkeypatch)
    plan = _invoke(setup, capsys, tmp_path)

    _invoke(setup, capsys, tmp_path, "--approve", str(plan["approval_digest"]))

    records = list(user_root.rglob("*.jsonl"))
    assert records
    assert all(path.is_file() and not path.is_symlink() for path in records)
    assert all(path.parent.is_relative_to(user_root) for path in records)


@pytest.mark.skipif(not FILE_SYMLINKS_SUPPORTED, reason="requires file symlink creation capability")
def test_symlinked_user_journal_is_rejected_before_record_or_native_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Prevent journal redirection from writing setup state or performing a mutation through a hostile link."""
    setup = _setup_module()
    user_root = tmp_path / "user-state"
    _redirect_user_state(setup, monkeypatch, user_root)
    calls = _install_native_boundary(setup, monkeypatch)
    journal = user_root / "bridge-setup" / "records" / "claude-user.jsonl"
    outside = tmp_path / "outside.jsonl"
    journal.parent.mkdir(parents=True)
    outside.write_text("outside\n", encoding="utf-8", newline="\n")
    journal.symlink_to(outside)
    plan = _invoke(setup, capsys, tmp_path)
    calls.clear()

    result = _invoke(setup, capsys, tmp_path, "--approve", str(plan["approval_digest"]))

    assert result["status"] == "blocked"
    assert result["state_changed"] is False
    assert outside.read_text(encoding="utf-8") == "outside\n"
    assert not any(command[:3] == ["claude", "plugin", "install"] for command, _ in calls)


def test_claude_auth_status_exit_one_with_no_output_is_clean_unauthenticated_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Prevent a documented unauthenticated exit from being misclassified as unsupported CLI drift."""
    setup = _setup_module()
    _redirect_user_state(setup, monkeypatch, tmp_path / "user-state")
    _install_native_boundary(setup, monkeypatch, authentication_status_returncode=1)

    result = _invoke(setup, capsys, tmp_path)

    assert result["status"] == "partial"
    assert result["classification"] == "configuration-pending"
    assert result["authentication"] == "not-checked"
    assert "authentication" in result["remaining"]


def test_successful_live_verification_is_point_in_time_evidence_not_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Prevent one successful paid probe from claiming unverified session/workspace readiness."""
    setup = _setup_module()
    _redirect_user_state(setup, monkeypatch, tmp_path / "user-state")
    _install_native_boundary(
        setup,
        monkeypatch,
        claude_installed=True,
        claude_authenticated=True,
        live_success=True,
    )

    plan = _invoke(setup, capsys, tmp_path, "--action", "verify-live")
    result = _invoke(
        setup,
        capsys,
        tmp_path,
        "--action",
        "verify-live",
        "--approve",
        str(plan["approval_digest"]),
    )

    assert result["status"] == "partial"
    assert result["classification"] == "live-verified"
    assert result["authentication"] == "live-verified"
    assert result["verification_level"] == "live-verified"
    assert result["provider_call"] is True
    assert result["ready_to_use"] is False
    assert result["remaining"] == ["session-workspace-verification"]


def test_all_live_skip_is_terminal_inference_unverified_without_an_approval_or_provider_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Prevent skipped live verification from creating a second approval or implying a provider result."""
    setup = _setup_module()
    _redirect_user_state(setup, monkeypatch, tmp_path / "user-state")
    calls = _install_native_boundary(
        setup,
        monkeypatch,
        claude_installed=True,
        claude_authenticated=True,
        live_success=True,
    )

    plan = _invoke(setup, capsys, tmp_path, "--live", "skip")

    assert plan["operations"] == []
    assert plan["approval_digest"] is None
    assert plan["status"] == "partial"
    assert plan["classification"] == "inference-unverified"
    assert plan["authentication"] == "inference-unverified"
    assert plan["provider_call"] is False
    assert plan["ready_to_use"] is False
    assert plan["remaining"] == ["session-workspace-verification"]
    assert not any(len(command) >= 2 and Path(command[1]).name == "bridge_diagnose.py" for command, _ in calls)


def test_live_required_remains_non_ready_until_approved_live_verification_and_reports_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Prevent required live evidence from becoming optional after configuration and authentication pass."""
    setup = _setup_module()
    _redirect_user_state(setup, monkeypatch, tmp_path / "user-state")
    _install_native_boundary(
        setup,
        monkeypatch,
        claude_installed=True,
        claude_authenticated=True,
        live_success=False,
    )

    required_plan = _invoke(setup, capsys, tmp_path, "--live", "required")
    live_plan = _invoke(setup, capsys, tmp_path, "--action", "verify-live", "--live", "required")
    failed = _invoke(
        setup,
        capsys,
        tmp_path,
        "--action",
        "verify-live",
        "--live",
        "required",
        "--approve",
        str(live_plan["approval_digest"]),
    )

    assert required_plan["ready_to_use"] is False
    assert "live-verification" in required_plan["remaining"]
    assert failed["status"] == "failed"
    assert failed["provider_call"] is True
    assert failed["ready_to_use"] is False
    assert "live-verification" in failed["remaining"]


def test_reencoded_tampered_approval_without_host_key_is_denied_before_host_probes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Prevent a public checksum from acting as an approval signature after an attacker changes a bound field."""
    setup = _setup_module()
    _redirect_user_state(setup, monkeypatch, tmp_path / "user-state")
    calls = _install_native_boundary(setup, monkeypatch)

    plan = _invoke(setup, capsys, tmp_path, "--action", "authenticate")
    payload = setup._decode_approval(str(plan["approval_digest"]))
    assert payload is not None
    payload["nonce"] = "attacker-reencoded-nonce"
    tampered = _reencode_untrusted_approval(payload)
    calls.clear()

    result = _invoke(setup, capsys, tmp_path, "--action", "authenticate", "--approve", tampered)

    assert result["status"] == "denied"
    assert result["provider_call"] is False
    assert result["state_changed"] is False
    assert calls == []


def test_concurrent_approval_replay_check_cannot_allow_two_native_configurations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Prevent callers that both read an unused approval from each executing the same configuration."""
    setup = _setup_module()
    user_root = tmp_path / "user-state"
    _redirect_user_state(setup, monkeypatch, user_root)
    calls = _install_native_boundary(setup, monkeypatch)
    plan = _invoke(setup, capsys, tmp_path)
    approval = str(plan["approval_digest"])
    calls.clear()

    real_open = setup.os.open
    lock_barrier = threading.Barrier(2)
    open_count_lock = threading.Lock()
    lock_open_count = 0
    lock_path = user_root / "bridge-setup" / "locks" / "claude-user.lock"

    def _gate_lock_creation(path: str, flags: int, mode: int = 0o777) -> int:
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

    monkeypatch.setattr(setup.os, "open", _gate_lock_creation)
    exit_codes: list[int] = []
    errors: list[BaseException] = []

    def _apply_same_approval() -> None:
        """Run one approved setup action from a competing worker thread."""
        try:
            exit_codes.append(
                setup.main(["--current-host", "codex", "--workspace", str(tmp_path), "--approve", approval])
            )
        except BaseException as error:  # pragma: no cover - asserted below; keeps concurrent errors visible.
            errors.append(error)

    first = threading.Thread(target=_apply_same_approval)
    second = threading.Thread(target=_apply_same_approval)
    first.start()
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)

    configured = [command for command, _ in calls if "bridge@borda-ai-rig" in command]
    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert sorted(exit_codes) == [0, 2]
    assert configured == [["claude", "plugin", "install", "bridge@borda-ai-rig", "--scope", "user"]]


@pytest.mark.parametrize(
    ("reported", "accepted"),
    (
        pytest.param("2.1.227\n", True, id="exact-minimum"),
        pytest.param("2.1.228 (Claude Code)\n", True, id="newer-patch"),
        pytest.param("3.0.0\n", True, id="newer-major"),
        pytest.param("2.1.226\n", False, id="older-patch"),
        pytest.param("no numeric release here\n", False, id="unparsable"),
    ),
)
def test_version_gate_is_a_minimum_floor_not_an_exact_pin(
    monkeypatch: pytest.MonkeyPatch, reported: str, accepted: bool
) -> None:
    """Host CLI releases at or above the pinned floor stay supported; older or unreadable ones fail closed.

    Host CLIs self-update between bridge releases; an exact-match gate made every setup action fail unsupported-version
    the day after either CLI shipped an update.
    """
    setup = _setup_module()
    monkeypatch.setattr(setup, "_capture", lambda argv: (True, reported, ""))

    available, version = setup._version("claude")

    assert available is accepted
    assert (version is not None) is accepted


def test_codex_not_logged_in_status_is_never_treated_as_authenticated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A zero-exit "Not logged in" status must not satisfy the authentication gate.

    ``codex login status`` can exit 0 while printing "Not logged in"; a bare substring match on "logged in" would claim
    host authentication and let a paid live verification proceed against an unauthenticated peer.
    """
    setup = _setup_module()
    monkeypatch.setattr(setup.subprocess, "run", lambda argv, **kwargs: _completed(list(argv), "Not logged in\n"))

    supported, authenticated = setup._authenticated("codex")

    assert supported is True
    assert authenticated is False


def test_verify_live_with_live_skip_plans_no_operation_even_when_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A live-verification request under live=skip must not smuggle a configure argv into its plan.

    On an unconfigured host the configure fallback used to fill the plan, so the operator was asked to approve a plugin
    install under a plan labeled live verification.
    """
    setup = _setup_module()
    _redirect_user_state(setup, monkeypatch, tmp_path / "user-state")
    _install_native_boundary(setup, monkeypatch)

    result = _invoke(setup, capsys, tmp_path, "--action", "verify-live", "--live", "skip")

    assert result["operations"] == []
    assert result["status"] == "blocked"
    assert result["classification"] == "inference-unverified"


def test_expired_failure_record_allows_one_fresh_approved_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failure record older than the retry TTL no longer blocks a newly approved identical operation.

    The permanent form of this block contradicted its own "wait and retry" guidance: with no expiry and no cleanup path
    the exact operation stayed locked forever while host state was unchanged.
    """
    setup = _setup_module()
    user_root = tmp_path / "user-state"
    _redirect_user_state(setup, monkeypatch, user_root)
    calls = _install_native_boundary(setup, monkeypatch, configuration_returncode=1)
    plan = _invoke(setup, capsys, tmp_path)
    _invoke(setup, capsys, tmp_path, "--approve", str(plan["approval_digest"]))
    journal = user_root / "bridge-setup" / "records" / "claude-user.jsonl"
    _age_journal_records(journal, setup.FAILED_OPERATION_TTL_SECONDS + 1)
    retry_plan = _invoke(setup, capsys, tmp_path)
    calls.clear()

    retry = _invoke(setup, capsys, tmp_path, "--approve", str(retry_plan["approval_digest"]))

    assert retry["classification"] == "configuration-failed"
    assert any(command[:3] == ["claude", "plugin", "install"] for command, _ in calls)


def test_approved_configuration_uses_the_network_sized_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The approved marketplace install runs with the configure budget, not the 20 s probe timeout.

    A marketplace install downloads plugin payloads; under the shared probe timeout a slow link journaled a failure
    while the child could still complete, and the failed record then blocked the repeat of that same install.
    """
    setup = _setup_module()
    _redirect_user_state(setup, monkeypatch, tmp_path / "user-state")
    calls = _install_native_boundary(setup, monkeypatch)
    plan = _invoke(setup, capsys, tmp_path)
    calls.clear()

    _invoke(setup, capsys, tmp_path, "--approve", str(plan["approval_digest"]))

    install_kwargs = [kwargs for command, kwargs in calls if command[:3] == ["claude", "plugin", "install"]]
    assert [kwargs.get("timeout") for kwargs in install_kwargs] == [setup.CONFIGURE_TIMEOUT_SECONDS]


def test_empty_approval_denial_reports_the_actual_request_instead_of_fabricated_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A trailing bare --approve denial must echo the real action, policy, scope, and workspace.

    The old fallback hardcoded action=all live=prompt and hand-parsed only space-separated flags, so a denied
    live=required run was reported under the softer prompt policy and equals-form flags silently described a different
    invocation.
    """
    setup = _setup_module()
    _redirect_user_state(setup, monkeypatch, tmp_path / "user-state")
    calls = _install_native_boundary(setup, monkeypatch)

    result = _invoke(
        setup, capsys, tmp_path, "--action", "verify-live", "--live", "required", "--scope=project", "--approve"
    )

    assert result["status"] == "denied"
    assert result["requested"] == {"action": "verify-live", "target": "peer", "scope": "project", "live": "required"}
    assert result["canonical_workspace"] == tmp_path.resolve().as_posix()
    assert calls == []

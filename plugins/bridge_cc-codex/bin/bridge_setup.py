"""Plan and execute credential-free Bridge host setup operations.

Purpose: Provide the narrow, deterministic process boundary behind the Claude and Codex ``bridge:setup`` skills. Scope:
This module detects a supported installed host CLI, checks that its version meets the supported minimum, parses its
plugin inventory and login-status probe, and plans one native plugin operation for a single peer host. It can execute
that already-bound operation only after a digest approval and a repeated state probe. Provider authentication is
deliberately separate: it starts only the native login command with the operator's streams inherited. Usage: Run
``bridge_setup.py --current-host codex --workspace <path>`` to emit a credential-free plan, then repeat its exact
arguments with ``--approve <digest>`` to configure. ``--action authenticate`` and ``--action verify-live`` each emit a
distinct plan and require their own action-bound approval; authentication launches only the native login command and
live verification calls the existing bounded Bridge supervisor. Outputs: Stdout receives exactly one sanitized JSON
result that contains the requested/resolved scope, operations, bound digest, evidence level, and remaining work; no raw
CLI output is retained. Failure: Missing executables, unknown versions, malformed inventories, approval mismatch, state
drift, or failed native commands fail closed without guessed repair or credential capture. Used by: The shipped Claude
and Codex setup skills, their contract tests, and package validation. The code is Python 3.10+ standard library only and
intentionally never accepts, reads, logs, pipes, stores, or validates tokens, access keys, browser/device codes, or
transcripts. Approved mutations and approval consumption are recorded as opaque fingerprints under the platform-native
per-user ``bridge-setup`` state root, rather than the workspace, so locking and replay protection cover every workspace
for the same target and scope. Those records contain no raw output, provider content, or secret-bearing command
material. On POSIX the signing key and its state directory are owner-only (0600/0700); on Windows the equivalent
boundary is the current user's LOCALAPPDATA ACL. The HMAC binds a plan to state but does not itself prove human consent:
the host skill or terminal approval remains the authority for an action.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePath

#: Marketplace identifier of the bridge plugin that host CLI commands install and look up.
PLUGIN_ID = "bridge@borda-ai-rig"
#: Seconds allowed for each short host CLI probe, such as listing plugins or checking login status.
CAPTURE_TIMEOUT_SECONDS = 20
# Marketplace installs download plugin payloads, so the approved configure
# command gets a network-sized budget instead of the local probe timeout.
#: Seconds allowed for the approved install or enable command, sized for a network download.
CONFIGURE_TIMEOUT_SECONDS = 300
# A recorded failed operation blocks identical retries only for this long;
# afterwards one fresh approved attempt is allowed again, so a transient host
# fault (network drop, interrupted install) cannot lock the operation forever.
#: Seconds a recorded failed operation blocks identical retries before one fresh approved attempt is allowed.
FAILED_OPERATION_TTL_SECONDS = 3600
#: Largest stdout or stderr slice, in bytes, kept from a host CLI command.
MAX_CAPTURE_BYTES = 64 * 1024
#: Largest setup state record, in bytes, that is read back or accepted.
MAX_STATE_RECORD_BYTES = 8 * 1024 * 1024
#: Seconds an issued approval stays valid before the configure step must be re-approved.
APPROVAL_TTL_SECONDS = 300
#: Length in bytes of the secret key used to sign approval records.
APPROVAL_KEY_BYTES = 32
#: Command-line option names that look like credentials; any argv containing one is rejected before parsing.
SENSITIVE_OPTIONS = ("--token", "--access-token", "--api-key", "--device-code", "--code", "--secret")
#: Seconds allowed for the live end-to-end check that calls a real host CLI.
LIVE_TIMEOUT_SECONDS = 180
#: Per-host table of minimum CLI version, fixed argv templates, and supported install scopes; no other commands are run.
CAPABILITY_MATRIX = {
    "codex": {
        "minimum_version": "0.148.0",
        "plugin_list_argv": ["codex", "plugin", "list", "--json"],
        "configure_argv": ["codex", "plugin", "add", PLUGIN_ID, "--json"],
        "authentication_status_argv": ["codex", "login", "status"],
        "authentication_argv": ["codex", "login"],
        "scopes": [],
    },
    "claude": {
        "minimum_version": "2.1.227",
        "plugin_list_argv": ["claude", "plugin", "list", "--json"],
        "configure_argv": ["claude", "plugin", "install", PLUGIN_ID, "--scope", "{scope}"],
        "enable_argv": ["claude", "plugin", "enable", PLUGIN_ID, "--scope", "{scope}"],
        "authentication_status_argv": ["claude", "auth", "status", "--json"],
        "authentication_argv": ["claude", "auth", "login"],
        "scopes": ["user", "project", "local"],
    },
}


def _canonical_workspace(value: str) -> Path:
    """Resolve the host-selected workspace before binding an approval."""
    return Path(value).resolve()


def _stable_fingerprint(value: object) -> str:
    """Return a portable SHA-256 fingerprint for JSON-serializable evidence."""
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _workspace_fingerprint(workspace: Path) -> str:
    """Hash a normalized workspace path without serializing host separators."""
    return hashlib.sha256(PurePath(workspace).as_posix().encode("utf-8")).hexdigest()


def _approval_signature(key: bytes, raw: bytes) -> str:
    """Return the domain-separated signature for one serialized approval payload."""
    return hmac.new(key, b"bridge-setup-approval-v1\0" + raw, hashlib.sha256).hexdigest()


def _encode_approval(payload: dict[str, object], key: bytes | None = None) -> str:
    """Encode one approval with the host-held key that signs its exact payload."""
    if key is None:
        key = _approval_key(create=True)
    if key is None:
        raise ValueError("approval signing key is unavailable")
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{encoded}.{_approval_signature(key, raw)}"


def _decode_approval(value: str, key: bytes | None = None) -> dict[str, object] | None:
    """Decode an approval only when its keyed signature and shape are valid."""
    encoded, separator, signature = value.partition(".")
    if not encoded or not separator or not signature or len(value) > MAX_STATE_RECORD_BYTES:
        return None
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except (UnicodeDecodeError, ValueError):
        return None
    if key is None:
        try:
            key = _approval_key(create=False)
        except (OSError, ValueError):
            return None
    if key is None or not hmac.compare_digest(_approval_signature(key, raw), signature):
        return None
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, dict):
        return None
    return decoded


def _approval_time_is_valid(approval: dict[str, object], now: int) -> bool:
    """Require a fresh, fixed-lifetime approval with a nonempty nonce."""
    issued_at = approval.get("issued_at")
    expires_at = approval.get("expires_at")
    nonce = approval.get("nonce")
    return (
        type(issued_at) is int
        and type(expires_at) is int
        and isinstance(nonce, str)
        and bool(nonce)
        and issued_at <= now
        and expires_at == issued_at + APPROVAL_TTL_SECONDS
        and expires_at >= now
        and expires_at <= now + APPROVAL_TTL_SECONDS
    )


def _contains_sensitive_option(argv: list[str]) -> bool:
    """Reject credential-like switches before argparse can echo their values."""
    return any(token == option or token.startswith(f"{option}=") for token in argv for option in SENSITIVE_OPTIONS)


def _direction(current_host: str, target: str) -> str | None:
    """Return a normal traffic direction for a distinct target host."""
    if current_host == target:
        return None
    return f"{current_host}_to_{target}"


def _capture(argv: list[str]) -> tuple[bool, str, str]:
    """Run one status probe with bounded capture and discard raw output after parsing."""
    try:
        result = subprocess.run(  # noqa: S603 - argv list, no shell
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=CAPTURE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, "", "probe-failed"
    stdout = result.stdout[:MAX_CAPTURE_BYTES]
    stderr = result.stderr[:MAX_CAPTURE_BYTES]
    if result.returncode != 0:
        return False, "", "probe-failed"
    return True, stdout, "" if not stderr else "stderr-discarded"


def _version_tuple(version: str) -> tuple[int, ...]:
    """Parse a dotted numeric version into an orderable tuple."""
    return tuple(int(part) for part in version.split("."))


def _version(host: str) -> tuple[bool, str | None]:
    """Verify that a host reports at least the minimum supported CLI version.

    Host CLIs self-update between bridge releases, so an exact pin would fail every setup action the day after either
    CLI ships. The floor keeps the known-incompatible past releases out while the flag-level baseline in
    ``bridge_diagnose`` guards against forward help-surface drift.
    """
    ok, stdout, _ = _capture([host, "--version"])
    if not ok:
        return False, None
    reported = re.search(r"\b\d+\.\d+\.\d+\b", stdout)
    minimum = str(CAPABILITY_MATRIX[host]["minimum_version"])
    if reported is None or _version_tuple(reported.group()) < _version_tuple(minimum):
        return False, None
    return True, reported.group()


def _inventory(host: str) -> tuple[bool, dict[str, object] | None]:
    """Parse only supported plugin-list JSON into a compact plugin record."""
    ok, stdout, _ = _capture(list(CAPABILITY_MATRIX[host]["plugin_list_argv"]))
    if not ok:
        return False, None
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError:
        return False, None
    entries = value.get("installed") if isinstance(value, dict) else value
    if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
        return False, None
    matches: list[dict[str, object]] = []
    for entry in entries:
        identifier = entry.get("id", entry.get("pluginId"))
        if "id" in entry and "pluginId" in entry and entry["id"] != entry["pluginId"]:
            return False, None
        if identifier == PLUGIN_ID:
            matches.append(entry)
    if len(matches) > 1:
        return False, None
    if not matches:
        return True, {"installed": False, "enabled": False, "version": None}
    entry = matches[0]
    enabled = entry.get("enabled", True)
    version = entry.get("version")
    if not isinstance(enabled, bool) or (version is not None and not isinstance(version, str)):
        return False, None
    return True, {"installed": True, "enabled": enabled, "version": version}


def _authenticated(host: str) -> tuple[bool, bool]:
    """Probe host-owned login state without retaining the provider response."""
    try:
        result = subprocess.run(  # noqa: S603 - argv list, no shell
            list(CAPABILITY_MATRIX[host]["authentication_status_argv"]),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=CAPTURE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, False
    stdout = result.stdout[:MAX_CAPTURE_BYTES]
    if result.returncode not in {0, 1}:
        return False, False
    if host == "claude":
        if result.returncode == 1 and not stdout.strip():
            return True, False
        try:
            value = json.loads(stdout)
        except json.JSONDecodeError:
            return False, False
        logged_in = value.get("loggedIn") if isinstance(value, dict) else None
        return isinstance(logged_in, bool), bool(logged_in)
    # ``codex login status`` prints "Logged in ..." when authenticated and
    # "Not logged in" otherwise, sometimes both with exit code 0, so the
    # negative phrase must veto the substring match before it can pass.
    lowered = stdout.lower()
    return True, result.returncode == 0 and "logged in" in lowered and "not logged in" not in lowered


def _resolved_scope(host: str, requested_scope: str) -> str | None:
    """Choose a verified host scope, preferring user for portable peer setup."""
    supported = CAPABILITY_MATRIX[host]["scopes"]
    if not supported:
        return "user" if requested_scope == "auto" else None
    if requested_scope == "auto":
        return "user"
    return requested_scope if requested_scope in supported else None


def _operation(host: str, scope: str, inventory: dict[str, object]) -> list[dict[str, object]]:
    """Return the one closed-table operation implied by a verified inventory."""
    if not inventory["installed"]:
        argv = [str(value).format(scope=scope) for value in CAPABILITY_MATRIX[host]["configure_argv"]]
        return [_operation_entry("configure", argv)]
    if host == "claude" and not inventory["enabled"]:
        argv = [str(value).format(scope=scope) for value in CAPABILITY_MATRIX[host]["enable_argv"]]
        return [_operation_entry("repair", argv)]
    return []


def _operation_entry(
    action: str,
    argv: list[str],
    credential_behavior: str = "none",
    external_capability: str = "configured-marketplace-snapshot",
) -> dict[str, object]:
    """Describe one exact approved operation without executing it."""
    return {
        "action": action,
        "argv": argv,
        "credential_behavior": credential_behavior,
        "external_capability": external_capability,
    }


def _live_operation_argv(target: str, workspace: Path) -> list[str]:
    """Return the exact installed doctor command used for one live verification."""
    doctor = Path(__file__).resolve().with_name("bridge_diagnose.py")
    return [sys.executable, str(doctor), "--direction", target, "--workspace", PurePath(workspace).as_posix(), "--live"]


def _planned_operations(
    action: str, host: str, scope: str, inventory: dict[str, object], workspace: Path, live: str
) -> list[dict[str, object]]:
    """Return the isolated operation matching the requested lifecycle action."""
    if action == "authenticate":
        return [
            _operation_entry(
                "authenticate",
                list(CAPABILITY_MATRIX[host]["authentication_argv"]),
                "provider-owned-interactive-no-capture",
                "provider-authentication",
            )
        ]
    if action == "verify-live":
        # ``live=skip`` disables the provider call entirely; falling through to
        # the configure branch would put a plugin-install argv under a plan
        # labeled live verification, so the plan must stay empty instead.
        if live == "skip":
            return []
        return [
            _operation_entry(
                "verify-live",
                _live_operation_argv(host, workspace),
                "separately-approved-provider-request",
                "paid-peer-inference",
            )
        ]
    if action == "check":
        return []
    return _operation(host, scope, inventory)


def _inspect(current_host: str, target: str, scope: str) -> tuple[dict[str, object] | None, str | None]:
    """Collect bounded, non-secret state for a single possible peer target."""
    hosts = [current_host] if current_host == target else [current_host, target]
    # Presence is a prerequisite for this whole branch. Check all executables
    # before probing either one so a missing peer cannot leak into a partial
    # or misleading current-host inspection.
    if any(shutil.which(host) is None for host in hosts):
        return None, "missing-prerequisite"
    versions: dict[str, str] = {}
    for host in hosts:
        available, version = _version(host)
        if not available or version is None:
            return None, "unsupported-version"
        versions[host] = version
    inventory_ok, inventory = _inventory(target)
    authentication_ok, authenticated = _authenticated(target)
    if not inventory_ok or not authentication_ok or inventory is None:
        return None, "unsupported-capability"
    return {
        "versions": versions,
        "inventory": inventory,
        "authenticated": authenticated,
        "resolved_scope": scope,
    }, None


def _approval_payload(
    workspace: Path,
    requested: dict[str, str],
    current_host: str,
    target: str,
    direction: str | None,
    scope: str,
    operations: list[dict[str, object]],
    state: dict[str, object],
) -> dict[str, object]:
    """Bind one plan to its workspace, exact operation, request, and probe state."""
    state_fingerprint = _stable_fingerprint(state)
    execution_argv = operations[0]["argv"] if operations else []
    issued_at = int(time.time())
    return {
        "workspace": PurePath(workspace).as_posix(),
        "workspace_fingerprint": _workspace_fingerprint(workspace),
        "requested": requested,
        "current_host": current_host,
        "target": target,
        "direction": direction,
        "resolved_scope": scope,
        "operations": operations,
        "action": requested["action"],
        "execution_argv": execution_argv,
        "prerequisites": {
            "configuration_ready": bool(state["inventory"]["installed"] and state["inventory"]["enabled"]),
            "host_authenticated": state["authenticated"] is True,
        },
        "state_fingerprint": state_fingerprint,
        "issued_at": issued_at,
        "expires_at": issued_at + APPROVAL_TTL_SECONDS,
        "nonce": secrets.token_urlsafe(18),
    }


def _reinspect_approved_operation(
    token: str,
    key: bytes,
    workspace: Path,
    requested: dict[str, str],
    current_host: str,
    target: str,
    scope: str,
) -> tuple[dict[str, object], list[dict[str, object]], dict[str, object]] | None:
    """Rebind an approval after locking so external state cannot race execution."""
    approved = _decode_approval(token, key)
    if approved is None or not _approval_time_is_valid(approved, int(time.time())):
        return None
    state, error = _inspect(current_host, target, scope)
    if state is None or error is not None:
        return None
    operations = _planned_operations(
        requested["action"], target, scope, state["inventory"], workspace, requested["live"]
    )
    payload = _approval_payload(
        workspace,
        requested,
        current_host,
        target,
        _direction(current_host, target),
        scope,
        operations,
        state,
    )
    payload["issued_at"] = approved["issued_at"]
    payload["expires_at"] = approved["expires_at"]
    payload["nonce"] = approved["nonce"]
    return (state, operations, payload) if hmac.compare_digest(_encode_approval(payload, key), token) else None


def _base_result(
    *,
    status: str,
    current_host: str,
    target: str,
    direction: str | None,
    requested: dict[str, str],
    scope: str | None,
    workspace: Path,
    approval_digest: str | None = None,
    operations: list[dict[str, object]] | None = None,
    classification: str = "inspection-complete",
    authentication: str = "not-checked",
    verification_level: str = "static",
    state_changed: bool = False,
    provider_call: bool = False,
    ready_to_use: bool = False,
    remaining: list[str] | None = None,
    manual_next_action: str | None = None,
    limits: list[str] | None = None,
    state_fingerprint: str | None = None,
) -> dict[str, object]:
    """Build the single sanitized result shape shared by every terminal path."""
    return {
        "status": status,
        "current_host": current_host,
        "target": target,
        "direction": direction,
        "requested": requested,
        "canonical_workspace": PurePath(workspace).as_posix(),
        "workspace_fingerprint": _workspace_fingerprint(workspace),
        "resolved_scope": scope,
        "approval_digest": approval_digest,
        "state_fingerprint": state_fingerprint,
        "operations": operations or [],
        "classification": classification,
        "authentication": authentication,
        "verification_level": verification_level,
        "state_changed": state_changed,
        "provider_call": provider_call,
        "ready_to_use": ready_to_use,
        "remaining": remaining or [],
        "manual_next_action": manual_next_action,
        "confidence": "high" if status not in {"failed", "unsupported"} else "limited",
        "limits": limits or [],
    }


def _denied_result(current_host: str, target: str, requested: dict[str, str], workspace: Path) -> dict[str, object]:
    """Return a no-subprocess approval denial safe for untrusted CLI input."""
    return _base_result(
        status="denied",
        current_host=current_host,
        target=target,
        direction=_direction(current_host, target),
        requested=requested,
        scope=None,
        workspace=workspace,
        classification="approval-mismatch",
        remaining=["approved-configuration"],
        manual_next_action="Re-run the inspected plan with its exact approval digest.",
        limits=["No host command ran because approval was absent, malformed, or for different input."],
    )


def _run_configuration(operations: list[dict[str, object]]) -> bool:
    """Execute one previously verified marketplace command with output discarded."""
    if len(operations) != 1:
        return False
    argv = operations[0]["argv"]
    if not isinstance(argv, list) or not all(isinstance(value, str) for value in argv):
        return False
    try:
        result = subprocess.run(  # noqa: S603 - argv list, no shell
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=CONFIGURE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _user_state_root() -> Path:
    """Return the platform-native per-user state base without creating it."""
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")


def _state_paths(target: str, scope: str) -> tuple[Path, Path]:
    """Return regular-file-guarded lock and journal paths for a host setting."""
    root = _user_state_root() / "bridge-setup"
    locks = root / "locks"
    records = root / "records"
    for directory in (root, locks, records):
        if directory.is_symlink():
            raise ValueError("user setup state path must not be a symlink")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = directory.stat()
        if not stat.S_ISDIR(metadata.st_mode) or directory.is_symlink():
            raise ValueError("user setup state path is not a regular directory")
        if os.name != "nt":
            if metadata.st_uid != os.getuid():
                raise ValueError("user setup state directory has an unexpected owner")
            if stat.S_IMODE(metadata.st_mode) != 0o700:
                directory.chmod(0o700)
    name = f"{target}-{scope}"
    lock, journal = locks / f"{name}.lock", records / f"{name}.jsonl"
    for path in (lock, journal):
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError("user setup state record must be a regular file")
    return lock, journal


def _approval_journal(target: str, scope: str) -> Path:
    """Return the separate regular-file journal used only for one-use approvals."""
    _, journal = _state_paths(target, scope)
    path = journal.with_name(f"{target}-{scope}.approvals.jsonl")
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("approval record must be a regular file")
    return path


def _open_regular_record(path: Path, flags: int) -> int:
    """Open one user-state record without accepting a symlink or non-regular file."""
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("user setup record must be a regular file")
    descriptor = os.open(str(path), flags | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode) or path.is_symlink():
            raise ValueError("user setup record changed while opening")
    except (OSError, ValueError):
        os.close(descriptor)
        raise
    return descriptor


def _approval_key_path() -> Path:
    """Return the host-held approval-key path without creating user state."""
    return _user_state_root() / "bridge-setup" / "approval.key"


def _read_approval_key(path: Path) -> bytes | None:
    """Read exactly one private approval key through the regular-file guard."""
    try:
        descriptor = _open_regular_record(path, os.O_RDONLY)
    except FileNotFoundError:
        return None
    try:
        metadata = os.fstat(descriptor)
        if metadata.st_size != APPROVAL_KEY_BYTES or (
            os.name != "nt" and (metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o600)
        ):
            os.close(descriptor)
            return None
        with os.fdopen(descriptor, "rb") as stream:
            key = stream.read(APPROVAL_KEY_BYTES + 1)
    except OSError:
        return None
    return key if len(key) == APPROVAL_KEY_BYTES else None


def _approval_key(create: bool) -> bytes | None:
    """Load or atomically create the 0600 per-user HMAC key for actionable plans."""
    path = _approval_key_path()
    key = _read_approval_key(path)
    if key is not None or not create:
        return key
    # Only actionable planning reaches this branch; ``check`` remains free of
    # user-state creation while races converge on one opaque random key.
    root = path.parent
    if root.is_symlink():
        raise ValueError("approval key directory must not be a symlink")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = root.stat()
    if not stat.S_ISDIR(metadata.st_mode) or root.is_symlink():
        raise ValueError("approval key directory is invalid")
    if os.name != "nt":
        if metadata.st_uid != os.getuid():
            raise ValueError("approval key directory has an unexpected owner")
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            root.chmod(0o700)
            metadata = root.stat()
            if stat.S_IMODE(metadata.st_mode) != 0o700:
                raise ValueError("approval key directory is not private")
    try:
        descriptor = _open_regular_record(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        return _read_approval_key(path)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        key = secrets.token_bytes(APPROVAL_KEY_BYTES)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(key)
            stream.flush()
            os.fsync(stream.fileno())
        return key
    except OSError:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _read_records(path: Path) -> deque[dict[str, object]] | None:
    """Return a bounded regular-file journal tail, or ``None`` on unsafe state."""
    try:
        descriptor = _open_regular_record(path, os.O_RDONLY)
    except FileNotFoundError:
        return deque()
    except (OSError, ValueError):
        return None
    try:
        if os.fstat(descriptor).st_size > MAX_STATE_RECORD_BYTES:
            os.close(descriptor)
            return None
        with os.fdopen(descriptor, encoding="utf-8") as stream:
            lines = deque(stream)
    except OSError:
        return None
    records: deque[dict[str, object]] = deque()
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(record, dict):
            return None
        records.append(record)
    return records


def _append_record(path: Path, record: dict[str, object]) -> None:
    """Append one newline-stable credential-free record through a safe descriptor."""
    descriptor = _open_regular_record(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _journal_has_failed_operation(
    journal: Path, target: str, operation_fingerprint: str, state_fingerprint: str
) -> bool:
    """Reject a recent repeat of the same recorded failed operation without raw-log retention.

    The block expires after ``FAILED_OPERATION_TTL_SECONDS`` so the documented "wait and retry" recovery path actually
    exists: a transient host fault would otherwise lock the exact operation for the state's lifetime with no cleanup
    command. A record without a readable timestamp blocks indefinitely, so tampered or truncated journals fail closed.
    """
    records = _read_records(journal)
    if records is None:
        return True
    threshold = int(time.time()) - FAILED_OPERATION_TTL_SECONDS
    for record in records:
        if (
            record.get("target") == target
            and record.get("operation_fingerprint") == operation_fingerprint
            and record.get("state_fingerprint") == state_fingerprint
            and record.get("result") == "failed"
        ):
            timestamp = record.get("timestamp")
            if type(timestamp) is not int or timestamp >= threshold:
                return True
    return False


def _consume_approval_once(journal: Path, approval_fingerprint: str) -> str:
    """Recheck and consume one approval while the caller holds the target lock."""
    records = _read_records(journal)
    if records is None:
        return "invalid"
    if any(record.get("approval_fingerprint") == approval_fingerprint for record in records):
        return "replayed"
    try:
        _append_record(journal, {"approval_fingerprint": approval_fingerprint, "timestamp": int(time.time())})
    except (OSError, ValueError):
        return "invalid"
    return "consumed"


def _acquire_mutation_lock(
    target: str, scope: str, operation_fingerprint: str, state_fingerprint: str
) -> tuple[Path, Path] | None:
    """Acquire one cross-process setup lock after rejecting repeated failed work."""
    try:
        lock, journal = _state_paths(target, scope)
        if _journal_has_failed_operation(journal, target, operation_fingerprint, state_fingerprint):
            return None
        descriptor = _open_regular_record(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except (OSError, ValueError):
        return None
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps({"target": target, "operation_fingerprint": operation_fingerprint}) + "\n")
    return lock, journal


def _release_mutation_lock(lock: Path) -> None:
    """Release only a regular lock path after the mutation terminal state is recorded."""
    try:
        if lock.exists() and not lock.is_symlink() and lock.is_file():
            lock.unlink()
    except OSError:
        pass


def _record_mutation(
    lock: Path,
    journal: Path,
    target: str,
    state_fingerprint: str,
    operation_fingerprint: str,
    result: str,
    verification_outcome: str,
) -> bool:
    """Append a credential-free result record and always release the operation lock."""
    try:
        record = {
            "timestamp": int(time.time()),
            "target": target,
            "state_fingerprint": state_fingerprint,
            "operation_fingerprint": operation_fingerprint,
            "fault_fingerprint": _stable_fingerprint(
                {"target": target, "state": state_fingerprint, "operation": operation_fingerprint}
            ),
            "result": result,
            "exit_classification": result,
            "verification_outcome": verification_outcome,
            "recurrence_count": 1,
            "rollback": {"mode": "manual-native-host", "credentials": "unchanged"},
        }
        _append_record(journal, record)
        return True
    except (OSError, ValueError):
        return False
    finally:
        _release_mutation_lock(lock)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse the closed setup CLI after sensitive switches have been screened."""
    parser = argparse.ArgumentParser(description="Plan or apply credential-free Bridge setup.")
    parser.add_argument("--current-host", choices=("codex", "claude"), required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument(
        "--action", choices=("all", "check", "configure", "authenticate", "repair", "verify-live"), default="all"
    )
    parser.add_argument("--target", choices=("peer", "codex", "claude"), default="peer")
    parser.add_argument("--scope", choices=("auto", "user", "project", "local"), default="auto")
    parser.add_argument("--live", choices=("prompt", "skip", "required"), default="prompt")
    parser.add_argument("--approve")
    return parser.parse_args(argv)


def _requested(args: argparse.Namespace) -> dict[str, str]:
    """Return stable requested fields without exposing operational flags."""
    return {"action": args.action, "target": args.target, "scope": args.scope, "live": args.live}


def _target(current_host: str, requested_target: str) -> str:
    """Resolve the one peer or explicit host owned by this invocation."""
    if requested_target == "peer":
        return "claude" if current_host == "codex" else "codex"
    return requested_target


def _authentication_label(state: dict[str, object]) -> str:
    """Report positive host-authentication evidence without exposing its probe."""
    return "host-authenticated" if state["authenticated"] is True else "not-checked"


def _verification_level(state: dict[str, object]) -> str:
    """Return the strongest static readiness evidence actually observed."""
    return "host-authenticated" if state["authenticated"] is True else "static"


def _remaining(operations: list[dict[str, object]], state: dict[str, object], live: str = "prompt") -> list[str]:
    """List only lifecycle levels that remain after static host evidence."""
    result: list[str] = []
    if operations:
        result.append("configuration")
    if state["authenticated"] is not True:
        result.append("authentication")
    result.append("session-workspace-verification")
    if live != "skip":
        result.append("live-verification")
    return result


def _verify_live(target: str, workspace: Path) -> bool:
    """Run the exact approved live doctor and retain only its boolean outcome."""
    try:
        result = subprocess.run(  # noqa: S603 - argv list, no shell
            _live_operation_argv(target, workspace),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=LIVE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0 or len(result.stdout.encode("utf-8")) > MAX_CAPTURE_BYTES:
        return False
    try:
        output = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False
    return (
        isinstance(output, dict)
        and output.get("ok") is True
        and output.get("live") is True
        and output.get("direction") == target
    )


@dataclass(frozen=True)
class _Resolved:
    """Bundle the invocation identity shared by every result built after scope resolution.

    The six fields repeat unchanged in almost every ``_base_result`` call once the target and scope are known, so
    carrying them as one value keeps each terminal path down to the fields that actually differ between paths.
    """

    current_host: str
    target: str
    direction: str | None
    requested: dict[str, str]
    scope: str | None
    workspace: Path

    def result(self, **fields: object) -> dict[str, object]:
        """Build one sanitized result that carries this invocation's resolved identity."""
        return _base_result(**vars(self), **fields)


@dataclass(frozen=True)
class _Terminal:
    """Pair one sanitized result with the exit code its path reports."""

    result: dict[str, object]
    exit_code: int


def _emit(terminal: _Terminal) -> int:
    """Write the single sanitized JSON result for one terminal path and return its exit code."""
    sys.stdout.write(json.dumps(terminal.result, sort_keys=True) + "\n")
    return terminal.exit_code


@dataclass(frozen=True)
class _MutationOutcome:
    """Report what one branch's native execution step produced, before its result is built."""

    ok: bool
    verification_outcome: str


@dataclass(frozen=True)
class _MutationCompletion:
    """Carry the outcome of an executed mutation together with the approval state rebound under the lock."""

    ok: bool
    verification_outcome: str
    state: dict[str, object]
    operations: list[dict[str, object]]
    payload: dict[str, object]


def _launch_authentication(target: str) -> _MutationOutcome:
    """Start only the native provider login, with the operator's streams inherited and nothing captured."""
    try:
        authentication_process = subprocess.run(list(CAPABILITY_MATRIX[target]["authentication_argv"]))  # noqa: S603 - argv list from fixed capability matrix, no shell
    except OSError:
        authentication_process = None
    authentication_ok = authentication_process is not None and authentication_process.returncode == 0
    return _MutationOutcome(authentication_ok, "auth-flow-launched" if authentication_ok else "failed")


def _apply_configuration(target: str, operations: list[dict[str, object]]) -> _MutationOutcome:
    """Run the approved marketplace operation and re-probe the inventory it was expected to change."""
    configured = _run_configuration(operations)
    verified = False
    if configured:
        inventory_ok, inventory = _inventory(target)
        verified = bool(inventory_ok and inventory and inventory["installed"] and inventory["enabled"])
    return _MutationOutcome(configured, "configuration-verified" if verified else "fresh-session-required")


def _run_live_verification(target: str, workspace: Path) -> _MutationOutcome:
    """Run the approved live doctor and retain only its boolean outcome."""
    live_ok = _verify_live(target, workspace)
    return _MutationOutcome(live_ok, "live-verified" if live_ok else "failed")


def _execute_approved_mutation(
    *,
    resolved: _Resolved,
    args: argparse.Namespace,
    approval_key: bytes | None,
    approval_fingerprint: str | None,
    lock_pair: tuple[Path, Path],
    operation_fingerprint: str,
    execute: Callable[[list[dict[str, object]]], _MutationOutcome],
) -> _Terminal | _MutationCompletion:
    """Run the lock-held part every approved mutation shares and report its outcome or its denial.

    The caller acquires the lock and owns its failure result, because that result is not the same shape in all three
    branches. From there the order is fixed: ``_reinspect_approved_operation`` re-verifies the signature against state
    read under the lock, ``_consume_approval_once`` spends the single-use approval, the branch's own native step runs,
    and ``_record_mutation`` journals the result and releases the lock. Both failure exits before execution release the
    lock explicitly. The caller must only reach this function from inside the region already gated by the approval
    digest and state-fingerprint drift check.
    """
    lock, journal = lock_pair
    bound = _reinspect_approved_operation(
        args.approve,
        approval_key or b"",
        resolved.workspace,
        resolved.requested,
        resolved.current_host,
        resolved.target,
        str(resolved.scope),
    )
    if bound is None:
        _release_mutation_lock(lock)
        return _Terminal(
            resolved.result(
                status="denied",
                classification="state-drift",
                remaining=["fresh-inspection"],
                manual_next_action="Inspect again and approve the current action before mutation.",
            ),
            2,
        )
    state, operations, payload = bound
    consumption = _consume_approval_once(
        journal.with_name(f"{resolved.target}-{resolved.scope}.approvals.jsonl"), approval_fingerprint or ""
    )
    if consumption != "consumed":
        _release_mutation_lock(lock)
        return _Terminal(
            resolved.result(
                status="denied" if consumption == "replayed" else "blocked",
                classification=("approval-replayed" if consumption == "replayed" else "approval-consumption-failed"),
                remaining=["fresh-approval"],
                manual_next_action="Inspect again; approvals are single use."
                if consumption == "replayed"
                else "Repair the regular user state path and inspect again.",
            ),
            2,
        )
    outcome = execute(operations)
    record_ok = _record_mutation(
        lock,
        journal,
        resolved.target,
        str(payload["state_fingerprint"]),
        operation_fingerprint,
        "complete" if outcome.ok else "failed",
        outcome.verification_outcome,
    )
    return _MutationCompletion(outcome.ok and record_ok, outcome.verification_outcome, state, operations, payload)


def main(argv: list[str] | None = None) -> int:
    """Emit one sanitized plan or approved result and never print native output."""
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if _contains_sensitive_option(raw_argv):
        # The sensitive value is deliberately discarded before parser errors can echo it.
        return _emit(
            _Terminal(
                _base_result(
                    status="blocked",
                    current_host="unknown",
                    target="unknown",
                    direction=None,
                    requested={"action": "unknown", "target": "unknown", "scope": "unknown", "live": "unknown"},
                    scope=None,
                    workspace=Path.cwd(),
                    classification="sensitive-input-rejected",
                    remaining=["credential-free-input"],
                    manual_next_action="Use the provider-owned interactive login; do not pass credentials to Bridge.",
                    limits=["Sensitive option rejected before argument parsing."],
                ),
                2,
            )
        )
    if raw_argv and raw_argv[-1] == "--approve":
        # argparse would print usage to stderr for the missing approval value;
        # an empty approval is a normal denied state and must preserve the
        # one-JSON-result contract instead. The remaining arguments still go
        # through the real parser so the denial reports the actual requested
        # action, policy, and workspace rather than fabricated defaults.
        try:
            args = _parse_args(raw_argv[:-1])
        except SystemExit as error:
            return 0 if error.code == 0 else 2
        workspace = _canonical_workspace(args.workspace)
        return _emit(
            _Terminal(
                _denied_result(args.current_host, _target(args.current_host, args.target), _requested(args), workspace),
                2,
            )
        )
    try:
        args = _parse_args(raw_argv)
    except SystemExit as error:
        # Help is successful; malformed arguments remain a nonzero usage error.
        return 0 if error.code == 0 else 2
    workspace = _canonical_workspace(args.workspace)
    requested = _requested(args)
    action = requested["action"]
    target = _target(args.current_host, args.target)
    if target == args.current_host:
        return _emit(
            _Terminal(
                _base_result(
                    status="manual",
                    current_host=args.current_host,
                    target=target,
                    direction=None,
                    requested=requested,
                    scope=None,
                    workspace=workspace,
                    classification="bootstrap-required",
                    remaining=["current-host-bootstrap", "fresh-session"],
                    manual_next_action=(
                        "Install or enable the current host externally, then start a fresh host session."
                    ),
                    limits=["A loaded setup skill never mutates the surface that loaded it."],
                ),
                2,
            )
        )
    scope = _resolved_scope(target, args.scope)
    if scope is None:
        return _emit(
            _Terminal(
                _base_result(
                    status="unsupported",
                    current_host=args.current_host,
                    target=target,
                    direction=_direction(args.current_host, target),
                    requested=requested,
                    scope=None,
                    workspace=workspace,
                    classification="unsupported-scope",
                    remaining=["supported-scope"],
                    manual_next_action="Select a scope supported by the target host.",
                ),
                2,
            )
        )
    resolved = _Resolved(
        current_host=args.current_host,
        target=target,
        direction=_direction(args.current_host, target),
        requested=requested,
        scope=scope,
        workspace=workspace,
    )
    approved: dict[str, object] | None = None
    approval_fingerprint: str | None = None
    if args.approve is not None:
        approved = _decode_approval(args.approve)
        expected_workspace = PurePath(workspace).as_posix()
        if (
            approved is None
            or approved.get("workspace") != expected_workspace
            or approved.get("workspace_fingerprint") != _workspace_fingerprint(workspace)
            or approved.get("requested") != requested
            or approved.get("current_host") != args.current_host
            or approved.get("target") != target
            or approved.get("resolved_scope") != scope
            or approved.get("action") != requested["action"]
        ):
            return _emit(_Terminal(_denied_result(args.current_host, target, requested, workspace), 2))
        now = int(time.time())
        expires_at = approved.get("expires_at")
        if type(expires_at) is int and expires_at < now:
            return _emit(
                _Terminal(
                    resolved.result(
                        status="denied",
                        classification="approval-expired",
                        remaining=["fresh-approval"],
                        manual_next_action="Inspect again and approve a current setup plan.",
                    ),
                    2,
                )
            )
        if not _approval_time_is_valid(approved, now):
            return _emit(
                _Terminal(
                    resolved.result(
                        status="denied",
                        classification="approval-time-invalid",
                        remaining=["fresh-approval"],
                        manual_next_action="Inspect again and approve a fixed-lifetime current setup plan.",
                    ),
                    2,
                )
            )
        approval_fingerprint = _stable_fingerprint(args.approve)
    elif "--approve" in raw_argv:
        return _emit(_Terminal(_denied_result(args.current_host, target, requested, workspace), 2))
    if approved is not None and requested["action"] == "verify-live":
        prerequisites = approved.get("prerequisites")
        if (
            not isinstance(prerequisites, dict)
            or not isinstance(prerequisites.get("configuration_ready"), bool)
            or not isinstance(prerequisites.get("host_authenticated"), bool)
        ):
            return _emit(_Terminal(_denied_result(args.current_host, target, requested, workspace), 2))
        if not prerequisites["configuration_ready"]:
            return _emit(
                _Terminal(
                    resolved.result(
                        status="blocked",
                        classification="configuration-needed",
                        remaining=["configuration"],
                        manual_next_action=(
                            "Configure the target host, start a fresh session if required, then inspect again."
                        ),
                        limits=[
                            "Live verification did not invoke a host or provider because the approved plan lacks configuration evidence."
                        ],
                    ),
                    2,
                )
            )
        if not prerequisites["host_authenticated"]:
            return _emit(
                _Terminal(
                    resolved.result(
                        status="blocked",
                        classification="authentication-needed",
                        remaining=["authentication"],
                        manual_next_action="Run separately approved native authentication, then inspect again.",
                        limits=[
                            "Live verification did not invoke a host or provider because the approved plan lacks authentication evidence."
                        ],
                    ),
                    2,
                )
            )
    state, inspection_error = _inspect(args.current_host, target, scope)
    if state is None:
        missing = inspection_error == "missing-prerequisite"
        return _emit(
            _Terminal(
                resolved.result(
                    status="blocked" if missing else "unsupported",
                    classification=inspection_error or "unsupported-capability",
                    remaining=["host-cli" if missing else "supported-host-capability"],
                    manual_next_action="Install or update the target host through its documented external workflow.",
                    limits=["Malformed or unsupported host output is never treated as an empty inventory."],
                ),
                2,
            )
        )
    operations = _planned_operations(
        requested["action"], target, scope, state["inventory"], workspace, requested["live"]
    )
    if requested["live"] == "skip" and not operations:
        return _emit(
            _Terminal(
                resolved.result(
                    status="blocked" if action == "verify-live" else "partial",
                    classification="inference-unverified",
                    authentication="inference-unverified",
                    verification_level=_verification_level(state),
                    remaining=["session-workspace-verification"],
                    manual_next_action=(
                        "Live verification is disabled by live=skip; remove that option before requesting it."
                        if action == "verify-live"
                        else "Live verification was skipped; use bridge status to verify this session and workspace."
                    ),
                    limits=["No provider call was requested because live=skip."],
                    state_fingerprint=_stable_fingerprint(state),
                ),
                2 if action == "verify-live" else 0,
            )
        )
    payload = _approval_payload(
        workspace,
        requested,
        args.current_host,
        target,
        _direction(args.current_host, target),
        scope,
        operations,
        state,
    )
    if approved is not None:
        payload["issued_at"] = approved["issued_at"]
        payload["expires_at"] = approved["expires_at"]
        payload["nonce"] = approved.get("nonce")
    try:
        approval_key = _approval_key(create=action != "check")
    except (OSError, ValueError):
        approval_key = None
    if action != "check" and approval_key is None:
        return _emit(
            _Terminal(
                resolved.result(
                    status="blocked",
                    classification="approval-key-unavailable",
                    remaining=["safe-user-state"],
                    manual_next_action="Repair the regular per-user setup state before retrying.",
                ),
                2,
            )
        )
    digest = _encode_approval(payload, approval_key) if approval_key is not None else None
    if args.approve is not None and action != "check":
        if _stable_fingerprint(state) != payload["state_fingerprint"] or args.approve != digest:
            return _emit(
                _Terminal(
                    resolved.result(
                        status="denied",
                        classification="state-drift",
                        remaining=["fresh-inspection"],
                        manual_next_action="Inspect again and approve the new digest before mutation.",
                        limits=["Host state changed or the operation no longer matches the approved plan."],
                    ),
                    2,
                )
            )
    if action == "verify-live" and args.approve is not None:
        if not state["inventory"]["installed"] or not state["inventory"]["enabled"]:
            return _emit(
                _Terminal(
                    resolved.result(
                        status="blocked",
                        approval_digest=digest,
                        operations=operations,
                        classification="configuration-needed",
                        authentication=_authentication_label(state),
                        verification_level=_verification_level(state),
                        remaining=["configuration"],
                        manual_next_action=(
                            "Configure the target host, start a fresh session if required, then inspect again."
                        ),
                        limits=[
                            "Live verification did not invoke the provider because target configuration is not verified."
                        ],
                        state_fingerprint=str(payload["state_fingerprint"]),
                    ),
                    2,
                )
            )
        if state["authenticated"] is not True:
            return _emit(
                _Terminal(
                    resolved.result(
                        status="blocked",
                        approval_digest=digest,
                        operations=operations,
                        classification="authentication-needed",
                        authentication="not-checked",
                        verification_level=_verification_level(state),
                        remaining=["authentication"],
                        manual_next_action="Run separately approved native authentication, then inspect again.",
                        limits=[
                            "Live verification did not invoke the provider because target authentication is not verified."
                        ],
                        state_fingerprint=str(payload["state_fingerprint"]),
                    ),
                    2,
                )
            )
    if action == "authenticate" and args.approve is not None:
        operation_fingerprint = _stable_fingerprint(operations)
        lock_pair = _acquire_mutation_lock(target, scope, operation_fingerprint, str(payload["state_fingerprint"]))
        if lock_pair is None:
            # Deliberately plainer than the configure branch's lock-failure result; the asymmetry is
            # deliberate pending a maintainer decision. Do not normalize it.
            return _emit(
                _Terminal(
                    resolved.result(
                        status="blocked",
                        classification="mutation-locked-or-repeat-fault",
                        remaining=["fresh-inspection"],
                        manual_next_action="Wait for the active setup operation before retrying.",
                    ),
                    2,
                )
            )
        mutation = _execute_approved_mutation(
            resolved=resolved,
            args=args,
            approval_key=approval_key,
            approval_fingerprint=approval_fingerprint,
            lock_pair=lock_pair,
            operation_fingerprint=operation_fingerprint,
            execute=lambda _operations: _launch_authentication(target),
        )
        if isinstance(mutation, _Terminal):
            return _emit(mutation)
        state, operations, payload = mutation.state, mutation.operations, mutation.payload
        if not mutation.ok:
            return _emit(
                _Terminal(
                    resolved.result(
                        status="failed",
                        approval_digest=digest,
                        operations=operations,
                        classification="authentication-launch-failed",
                        remaining=["authentication"],
                        manual_next_action="Run the native provider login directly and retry static inspection.",
                        limits=["Bridge did not capture login output."],
                        state_fingerprint=str(payload["state_fingerprint"]),
                    ),
                    2,
                )
            )
        return _emit(
            _Terminal(
                resolved.result(
                    status="partial",
                    approval_digest=digest,
                    operations=operations,
                    authentication="auth-flow-launched",
                    verification_level=_verification_level(state),
                    remaining=(
                        ["static-authentication-check"] + ([] if requested["live"] == "skip" else ["live-verification"])
                    ),
                    manual_next_action="Complete the provider-owned login, then re-run setup inspection.",
                    limits=["Login output and provider credentials are not captured."],
                    state_fingerprint=str(payload["state_fingerprint"]),
                ),
                0,
            )
        )
    if args.approve is not None and operations and action in {"all", "configure", "repair"}:
        operation_fingerprint = _stable_fingerprint(operations)
        lock_pair = _acquire_mutation_lock(target, scope, operation_fingerprint, str(payload["state_fingerprint"]))
        if lock_pair is None:
            # Richer than the authenticate and verify-live lock-failure results: it also reports the digest,
            # operations, authentication, verification level, and limits. The asymmetry is deliberate pending a
            # maintainer decision. Do not normalize it.
            return _emit(
                _Terminal(
                    resolved.result(
                        status="blocked",
                        approval_digest=digest,
                        operations=operations,
                        classification="mutation-locked-or-repeat-fault",
                        authentication=_authentication_label(state),
                        verification_level=_verification_level(state),
                        remaining=["fresh-inspection"],
                        manual_next_action=(
                            "Wait for the active setup operation or inspect changed state before retrying."
                        ),
                        limits=["Per-user setup locking prevents concurrent or repeated failed mutation."],
                        state_fingerprint=str(payload["state_fingerprint"]),
                    ),
                    2,
                )
            )
        mutation = _execute_approved_mutation(
            resolved=resolved,
            args=args,
            approval_key=approval_key,
            approval_fingerprint=approval_fingerprint,
            lock_pair=lock_pair,
            operation_fingerprint=operation_fingerprint,
            execute=lambda rebound_operations: _apply_configuration(target, rebound_operations),
        )
        if isinstance(mutation, _Terminal):
            return _emit(mutation)
        state, operations, payload = mutation.state, mutation.operations, mutation.payload
        if not mutation.ok:
            return _emit(
                _Terminal(
                    resolved.result(
                        status="failed",
                        approval_digest=digest,
                        operations=operations,
                        classification="configuration-failed",
                        authentication=_authentication_label(state),
                        verification_level=_verification_level(state),
                        remaining=["configuration"],
                        manual_next_action="Use the documented native host workflow; do not retry blindly.",
                        limits=["Native command output was discarded; the credential-free journal is per-user state."],
                        state_fingerprint=str(payload["state_fingerprint"]),
                    ),
                    2,
                )
            )
        return _emit(
            _Terminal(
                resolved.result(
                    status="partial",
                    approval_digest=digest,
                    operations=operations,
                    classification=mutation.verification_outcome,
                    authentication=_authentication_label(state),
                    verification_level=_verification_level(state),
                    state_changed=True,
                    remaining=_remaining([], state, requested["live"]),
                    manual_next_action="Start provider-owned authentication with a separately approved setup run.",
                    limits=[
                        "A fresh host session may be required before inventory becomes visible.",
                        "Credential-free setup journal is deliberately stored in platform-native per-user state.",
                    ],
                    state_fingerprint=str(payload["state_fingerprint"]),
                ),
                0,
            )
        )
    if action == "verify-live" and args.approve is not None:
        operation_fingerprint = _stable_fingerprint(operations)
        lock_pair = _acquire_mutation_lock(target, scope, operation_fingerprint, str(payload["state_fingerprint"]))
        if lock_pair is None:
            # Deliberately plainer than the configure branch's lock-failure result; the asymmetry is
            # deliberate pending a maintainer decision. Do not normalize it.
            return _emit(
                _Terminal(
                    resolved.result(
                        status="blocked",
                        classification="mutation-locked-or-repeat-fault",
                        remaining=["fresh-inspection"],
                        manual_next_action="Wait for the active setup operation before retrying.",
                    ),
                    2,
                )
            )
        mutation = _execute_approved_mutation(
            resolved=resolved,
            args=args,
            approval_key=approval_key,
            approval_fingerprint=approval_fingerprint,
            lock_pair=lock_pair,
            operation_fingerprint=operation_fingerprint,
            execute=lambda _operations: _run_live_verification(target, workspace),
        )
        if isinstance(mutation, _Terminal):
            return _emit(mutation)
        state, operations, payload = mutation.state, mutation.operations, mutation.payload
        live_ok = mutation.ok
        return _emit(
            _Terminal(
                resolved.result(
                    status="partial" if live_ok else "failed",
                    approval_digest=digest,
                    operations=operations,
                    classification="live-verified" if live_ok else "live-verification-failed",
                    authentication="live-verified" if live_ok else _authentication_label(state),
                    verification_level="live-verified" if live_ok else _verification_level(state),
                    provider_call=True,
                    ready_to_use=False,
                    remaining=["session-workspace-verification"] if live_ok else ["live-verification"],
                    manual_next_action=(
                        "Run bridge status to verify the active session and workspace before treating Bridge as ready."
                        if live_ok
                        else "Inspect and obtain a fresh live-verification approval."
                    ),
                    state_fingerprint=str(payload["state_fingerprint"]),
                ),
                0 if live_ok else 2,
            )
        )
    if action == "verify-live":
        status, classification, remaining, next_action = (
            "manual",
            "live-verification-requires-separate-approval",
            ["live-verification"],
            "Approve the exact listed live-verification operation after configuration and authentication are verified.",
        )
    elif action == "authenticate":
        status, classification, remaining, next_action = (
            "partial",
            "authentication-pending",
            ["authentication", "session-workspace-verification", "live-verification"],
            "Approve this exact native login operation; it does not configure the target host.",
        )
    else:
        status, classification, remaining, next_action = (
            "partial",
            "configuration-pending" if operations else "static-ready",
            _remaining(operations, state, requested["live"]),
            "Approve this exact digest to apply the listed native operation."
            if operations
            else (
                "Run separately approved --action verify-live, then verify the session and workspace with bridge status."
                if state["authenticated"] is True and requested["live"] != "skip"
                else "Launch authentication separately."
            ),
        )
    return _emit(
        _Terminal(
            resolved.result(
                status=status,
                approval_digest=digest,
                operations=operations,
                classification=classification,
                authentication="inference-unverified" if requested["live"] == "skip" else _authentication_label(state),
                verification_level=_verification_level(state),
                remaining=remaining,
                manual_next_action=next_action,
                limits=[
                    "Static inspection cannot prove a fresh host session, MCP workspace binding, or live inference."
                ],
                state_fingerprint=str(payload["state_fingerprint"]),
            ),
            0 if status == "partial" else 2,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())

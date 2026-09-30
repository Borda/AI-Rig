#!/usr/bin/env python3
"""Verify and emit one role card from the currently enabled Codex Rig package.

## Purpose

Let a thin user-agent shim resolve only a verified active package and role-card payload. The executable rechecks package
and executable identity at the point of use so a generated link cannot blindly trust stale paths.

## Scope

Reads local Codex/plugin state with bounded descriptor checks; it never installs a plugin, mutates shims, or contacts
GitHub. Its output contract is limited to one selected role card and the identity data needed by the calling shim.

## Usage

Generated role shims invoke this executable with their role identity; maintainers normally use ``agent-shims`` instead.
The role argument must come from the generated allowlist, and the executable must be selected through the local
discovery checks before the card is emitted.

## Used by

Generated thin shims, the shim manager, and installed-package link tests invoke this verifier. A shim uses the result to
continue with the verified role-card payload, while the manager and tests use the same protocol to detect package drift.

## Outputs

Prints the selected verified role card and enough stable identity data for the calling shim to continue safely. Output
is bounded and separated by a fixed protocol marker so callers can reject truncation or unexpected extra data.

## Failure

Absent/changed executable, invalid role ID, package identity mismatch, oversized output, or failed local Codex discovery
raises ``RoleUnavailable``. The failure protocol gives the shim a safe unavailable result instead of returning a card
whose provenance was not established.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import selectors
import stat
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath
from typing import NoReturn


PLUGIN_ID = "codex-rig@borda-ai-rig"
PLUGIN_NAME = "codex-rig"
MARKETPLACE = "borda-ai-rig"
PROTOCOL = 1
CARD_SEPARATOR = "--- codex-rig-role-card ---\n"
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
SCRIPT_PATH = Path(__file__).absolute()
PLUGIN_ROOT = SCRIPT_PATH.parent.parent
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_BINARY_BYTES = 512 * 1024 * 1024
MAX_CLI_OUTPUT_BYTES = 64 * 1024
VALID_ROLE_IDS = frozenset(
    {
        "challenger",
        "cicd-steward",
        "curator",
        "data-steward",
        "delegation-lead",
        "doc-scribe",
        "linting-expert",
        "oss-shepherd",
        "qa-specialist",
        "scientist",
        "security-auditor",
        "solution-architect",
        "squeezer",
        "sw-engineer",
        "web-explorer",
    }
)


class RoleUnavailable(RuntimeError):
    """Signal that linked role validation did not establish a safe active card."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class BootstrapArgumentParser(argparse.ArgumentParser):
    """Convert argument errors into the bootstrap failure protocol."""

    def error(self, message: str) -> NoReturn:
        """Reject malformed fixed arguments without argparse stderr output."""
        raise RoleUnavailable("invalid-arguments")


def require_digest(value: object) -> str:
    """Validate one exact lowercase SHA-256 value."""
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise RoleUnavailable("invalid-digest")
    return value


def digest_bytes(payload: bytes) -> str:
    """Return the lowercase SHA-256 digest for exact bytes."""
    return hashlib.sha256(payload).hexdigest()


def read_fd(fd: int, maximum: int) -> bytes:
    """Read bounded bytes from one already-open regular file descriptor."""
    metadata = os.fstat(fd)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
        raise RoleUnavailable("unsafe-file")
    chunks: list[bytes] = []
    remaining = maximum + 1
    while remaining:
        chunk = os.read(fd, min(remaining, 64 * 1024))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    payload = b"".join(chunks)
    if len(payload) > maximum:
        raise RoleUnavailable("oversized-file")
    return payload


def validate_relative_path(value: object, expected: str) -> tuple[str, ...]:
    """Require one exact normalized relative package path."""
    if not isinstance(value, str) or value != expected or any(ord(character) < 32 for character in value):
        raise RoleUnavailable("invalid-package-path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise RoleUnavailable("invalid-package-path")
    return path.parts


def read_relative(root_fd: int, value: object, expected: str, maximum: int = MAX_TEXT_BYTES) -> bytes:
    """Read one exact package file without following any path-component symlink."""
    parts = validate_relative_path(value, expected)
    directory_fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory_fd)
        try:
            return read_fd(file_fd, maximum)
        finally:
            os.close(file_fd)
    finally:
        os.close(directory_fd)


def digest_fd(fd: int, maximum: int) -> str:
    """Hash an already-open file from its first byte without closing it."""
    os.lseek(fd, 0, os.SEEK_SET)
    return digest_bytes(read_fd(fd, maximum))


def open_absolute_executable(value: object) -> tuple[Path, int, str]:
    """Open and hash one absolute non-symlink executable selected by doctor."""
    if not isinstance(value, str) or not os.path.isabs(value) or "\x00" in value:
        raise RoleUnavailable("invalid-codex-binary")
    path = Path(value)
    file_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        metadata = os.fstat(file_fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o111 == 0:
            raise RoleUnavailable("invalid-codex-binary")
        return path, file_fd, digest_fd(file_fd, MAX_BINARY_BYTES)
    except Exception:
        os.close(file_fd)
        raise


def executable_is_unchanged(path: Path, original_fd: int, expected_digest: str) -> bool:
    """Detect an in-place or pathname replacement of a bound executable."""
    current_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        original = os.fstat(original_fd)
        current = os.fstat(current_fd)
        identity_fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns")
        same_identity = all(getattr(original, field) == getattr(current, field) for field in identity_fields)
        return (
            same_identity
            and digest_fd(original_fd, MAX_BINARY_BYTES) == expected_digest
            and digest_fd(current_fd, MAX_BINARY_BYTES) == expected_digest
        )
    finally:
        os.close(current_fd)


def run_bounded(command: list[str], timeout: float = 10.0) -> tuple[int, bytes, bytes]:
    """Run a child while enforcing stdout and stderr limits during capture."""
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdout is not None
    assert process.stderr is not None
    streams = {process.stdout.fileno(): bytearray(), process.stderr.fileno(): bytearray()}
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    selector.register(process.stderr, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            events = selector.select(remaining)
            if not events:
                raise subprocess.TimeoutExpired(command, timeout)
            for key, _ in events:
                chunk = os.read(key.fd, 64 * 1024)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                streams[key.fd].extend(chunk)
                if len(streams[key.fd]) > MAX_CLI_OUTPUT_BYTES:
                    raise RoleUnavailable("active-package-oracle-oversized")
        return (
            process.wait(timeout=max(0.0, deadline - time.monotonic())),
            bytes(streams[process.stdout.fileno()]),
            bytes(streams[process.stderr.fileno()]),
        )
    except Exception:
        process.kill()
        process.wait()
        raise
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()


def active_plugin_root(codex_binary: Path, codex_home: Path, version: str) -> Path:
    """Derive the enabled package root from the observed Codex cache contract."""
    returncode, stdout, _ = run_bounded([str(codex_binary), "plugin", "list", "--marketplace", MARKETPLACE, "--json"])
    if returncode != 0:
        raise RoleUnavailable("active-package-oracle-failed")
    payload = json.loads(stdout)
    if not isinstance(payload, dict) or not isinstance(payload.get("installed"), list):
        raise RoleUnavailable("active-package-oracle-invalid")
    matches = []
    for item in payload["installed"]:
        if not isinstance(item, dict):
            raise RoleUnavailable("active-package-oracle-invalid")
        if (
            item.get("pluginId") == PLUGIN_ID
            and item.get("name") == PLUGIN_NAME
            and item.get("marketplaceName") == MARKETPLACE
            and item.get("installed") is True
            and item.get("enabled") is True
            and item.get("version") == version
        ):
            matches.append(item)
    if len(matches) != 1:
        raise RoleUnavailable("active-package-mismatch")
    return codex_home / "plugins" / "cache" / MARKETPLACE / PLUGIN_NAME / version


def active_root_matches(root_fd: int, active_root: Path) -> bool:
    """Compare the selected cache directory with the held package snapshot."""
    try:
        active_fd = os.open(active_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        expected = os.fstat(root_fd)
        active = os.fstat(active_fd)
        return (expected.st_dev, expected.st_ino) == (active.st_dev, active.st_ino)
    finally:
        os.close(active_fd)


def validate_role_id(value: object) -> str:
    """Accept only a bounded canonical role identifier safe for envelopes."""
    if not isinstance(value, str) or value not in VALID_ROLE_IDS or len(value.encode("utf-8")) > 64:
        raise RoleUnavailable("role-not-allowlisted")
    return value


def indexed_files(manifest: dict[str, object]) -> dict[str, dict[str, object]]:
    """Index the unique package file records by exact relative path."""
    files = manifest.get("files")
    if not isinstance(files, list):
        raise RoleUnavailable("manifest-invalid")
    index: dict[str, dict[str, object]] = {}
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or item["path"] in index:
            raise RoleUnavailable("manifest-invalid")
        index[item["path"]] = item
    return index


def has_compatible_profile(manifest: dict[str, object]) -> bool:
    """Accept only declared plugin-only, injected-role, or shim-enabled feature tuples."""
    features = manifest.get("features")
    if not isinstance(features, dict) or set(features) != {"manager", "hooks", "mcp", "generated_shims"}:
        return False
    identity = (
        manifest.get("release_profile"),
        features.get("manager"),
        features.get("hooks"),
        features.get("mcp"),
        features.get("generated_shims"),
    )
    return identity in {
        ("plugin-only", False, False, False, False),
        ("plugin-only+manager", True, False, False, True),
        ("plugin-only+manager", True, True, False, True),
        ("role-card-injected", True, False, False, False),
        ("role-card-injected", True, True, False, False),
        ("role-card-injected", True, True, True, False),
        ("shim-enabled", True, False, False, True),
        ("shim-enabled", True, True, False, True),
    }


def load_verified_role(args: argparse.Namespace) -> tuple[bytes, str]:
    """Validate one identity chain and return its exact role bytes and digest."""
    role_id = validate_role_id(args.role)
    expected_root = Path(args.plugin_root).absolute()
    if expected_root != PLUGIN_ROOT:
        raise RoleUnavailable("plugin-root-mismatch")

    root_fd = os.open(PLUGIN_ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        helper_bytes = read_relative(root_fd, "scripts/verify_role_link.py", "scripts/verify_role_link.py")
        helper_digest = require_digest(args.helper_sha256)
        if digest_bytes(helper_bytes) != helper_digest:
            raise RoleUnavailable("helper-hash-mismatch")

        manifest_bytes = read_relative(root_fd, "package-manifest.json", "package-manifest.json")
        if digest_bytes(manifest_bytes) != require_digest(args.manifest_sha256):
            raise RoleUnavailable("manifest-hash-mismatch")
        manifest = json.loads(manifest_bytes)
        if not isinstance(manifest, dict) or manifest.get("schema") != PROTOCOL:
            raise RoleUnavailable("manifest-invalid")

        plugin_bytes = read_relative(root_fd, ".codex-plugin/plugin.json", ".codex-plugin/plugin.json")
        plugin_manifest = json.loads(plugin_bytes)
        version = manifest.get("version")
        files = indexed_files(manifest)
        plugin_record = files.get(".codex-plugin/plugin.json", {})
        if (
            manifest.get("plugin") != PLUGIN_NAME
            or not has_compatible_profile(manifest)
            or not isinstance(plugin_manifest, dict)
            or plugin_manifest.get("name") != PLUGIN_NAME
            or plugin_manifest.get("version") != version
            or plugin_record.get("sha256") != digest_bytes(plugin_bytes)
            or plugin_record.get("mode") != "0644"
        ):
            raise RoleUnavailable("package-identity-mismatch")

        bootstrap = manifest.get("bootstrap")
        if not isinstance(bootstrap, dict) or bootstrap.get("protocol") != PROTOCOL:
            raise RoleUnavailable("bootstrap-manifest-mismatch")
        helper_path = "scripts/verify_role_link.py"
        if (
            bootstrap.get("helper") != helper_path
            or bootstrap.get("sha256") != helper_digest
            or files.get(helper_path, {}).get("sha256") != helper_digest
        ):
            raise RoleUnavailable("bootstrap-manifest-mismatch")

        codex_binary, codex_fd, codex_digest = open_absolute_executable(args.codex_binary)
        try:
            if codex_digest != require_digest(args.codex_sha256):
                raise RoleUnavailable("codex-binary-mismatch")
            codex_home_raw = os.environ.get("CODEX_HOME")
            if not codex_home_raw or not os.path.isabs(codex_home_raw):
                raise RoleUnavailable("codex-home-invalid")
            codex_home = Path(codex_home_raw).absolute()
            active_root = active_plugin_root(codex_binary, codex_home, version)
            if not active_root_matches(root_fd, active_root):
                raise RoleUnavailable("active-package-mismatch")

            roles = manifest.get("roles")
            if not isinstance(roles, list):
                raise RoleUnavailable("manifest-invalid")
            role_entries = [item for item in roles if isinstance(item, dict) and item.get("id") == role_id]
            if len(role_entries) != 1:
                raise RoleUnavailable("role-not-allowlisted")
            role_path = f"roles/{role_id}/ROLE.md"
            role_entry = role_entries[0]
            validate_relative_path(role_entry.get("path"), role_path)
            role_digest = require_digest(args.role_sha256)
            if role_entry.get("sha256") != role_digest or files.get(role_path, {}).get("sha256") != role_digest:
                raise RoleUnavailable("role-manifest-mismatch")
            role_bytes = read_relative(root_fd, role_entry["path"], role_path)
            if digest_bytes(role_bytes) != role_digest:
                raise RoleUnavailable("role-hash-mismatch")
            role_bytes.decode("utf-8")

            # Refuse output if a normal concurrent runtime or cache update changed
            # either identity after the initial active-package decision.
            active_root = active_plugin_root(codex_binary, codex_home, version)
            if not active_root_matches(root_fd, active_root):
                raise RoleUnavailable("active-package-transition")
            if not executable_is_unchanged(codex_binary, codex_fd, codex_digest):
                raise RoleUnavailable("codex-binary-transition")
            return role_bytes, role_digest
        finally:
            os.close(codex_fd)
    finally:
        os.close(root_fd)


def parse_args() -> argparse.Namespace:
    """Parse the fixed linked-bootstrap verification arguments."""
    parser = BootstrapArgumentParser(description=__doc__, add_help=False)
    parser.add_argument("--plugin-root", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--role-sha256", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--helper-sha256", required=True)
    parser.add_argument("--codex-binary", required=True)
    parser.add_argument("--codex-sha256", required=True)
    return parser.parse_args()


def encode_envelope(status: str, role_id: str, **fields: str | int) -> str:
    """Encode one canonical single-line bootstrap protocol envelope."""
    payload: dict[str, str | int] = {"protocol": PROTOCOL, "role_id": role_id, "status": status, **fields}
    return json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"


def unavailable(role_id: str, reason: str) -> NoReturn:
    """Emit the only permitted failure envelope and terminate."""
    sys.stdout.buffer.write(encode_envelope("codex-rig-role-unavailable", role_id, reason=reason).encode("utf-8"))
    raise SystemExit(4)


def main() -> None:
    """Emit a verified role envelope and card or an unavailable envelope."""
    role_id = "unknown"
    try:
        args = parse_args()
        role_id = validate_role_id(args.role)
        role, role_digest = load_verified_role(args)
    except RoleUnavailable as error:
        unavailable(role_id, error.reason)
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError, subprocess.SubprocessError):
        unavailable(role_id, "verification-error")
    success = encode_envelope("ok", role_id, role_sha256=role_digest).encode("utf-8")
    sys.stdout.buffer.write(success + CARD_SEPARATOR.encode("utf-8") + role)


if __name__ == "__main__":
    main()

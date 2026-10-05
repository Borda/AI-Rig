#!/usr/bin/env python3
"""Generate deterministic thin role-shim bytes from the installed Codex Rig package.

## Purpose

Derive role configuration that points at verified installed role cards without duplicating their behavioral contract.
Deterministic generation gives the manager and link verifier identical bytes and hashes for the same package inputs.

## Scope

Validates package inputs and generates roster/shim values; mutation of a user's agent directory is delegated to the
manager transaction flow. It reads package assets and emits in-memory records, so it cannot install, repair, or remove a
user's files by itself.

## Usage

Invoke through ``manage_role_agents.py`` or import generation helpers in reproducibility tests; it is not a general
installation command. Call generation only after package identity and role-card inputs have been resolved to the active
package root.

## Used by

Shim planning, role-link verification, and generated-role acceptance tests call these generation helpers. The planner
consumes the roster for operation selection, while the link verifier uses the generated payload contract to validate one
role at startup.

## Outputs

Returns immutable generated role/roster records, content hashes, and canonical shim payload bytes for a verified package
version. The records include the package and role identities needed to bind a shim to the exact card it will request
later.

## Failure

Bad identifiers, invalid frontmatter, malformed manifests, path escape, or oversized input raises a local validation
error without writing user files. Callers must treat this as a generation failure and avoid presenting the incomplete
roster for approval.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn

ROLE_IDS = (
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
)
RUNTIME_KEYS = ("model", "model_reasoning_effort", "approval_policy", "sandbox_mode")
FRONTMATTER_KEYS = frozenset({"role_id", "name", *RUNTIME_KEYS, "fallback_modes"})
PLUGIN_FIELDS = frozenset(
    {"name", "version", "description", "author", "homepage", "repository", "license", "skills", "interface"}
)
PLUGIN_AUTHOR_FIELDS = frozenset({"name", "url"})
PLUGIN_INTERFACE_FIELDS = frozenset(
    {
        "displayName",
        "shortDescription",
        "longDescription",
        "developerName",
        "category",
        "capabilities",
        "defaultPrompt",
    }
)
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
SEMVER_IDENTIFIER = r"(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
SEMVER_PATTERN = re.compile(
    rf"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    rf"(?:-{SEMVER_IDENTIFIER}(?:\.{SEMVER_IDENTIFIER})*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
MAX_PATH_BYTES = 4096
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_ROLE_BYTES = 2 * 1024 * 1024
MAX_BINARY_BYTES = 512 * 1024 * 1024
MAX_SHIM_BYTES = 262_144


@dataclass(frozen=True)
class GeneratedRole:
    """Bind one generated shim to its canonical role and content identities."""

    role_id: str
    target_name: str
    card_path: str
    role_hash: str
    shim_bytes: bytes
    file_hash: str


@dataclass(frozen=True)
class GeneratedRoster:
    """Expose one immutable manager-ready generated roster."""

    plugin_version: str
    package_hash: str
    bootstrap_hash: str
    generator_version: int
    roles: tuple[GeneratedRole, ...]


def digest_bytes(payload: bytes) -> str:
    """Return the lowercase SHA-256 digest for exact bytes."""
    return hashlib.sha256(payload).hexdigest()


def _reject_constant(value: str) -> NoReturn:
    """Reject non-finite JSON numbers from package metadata."""
    raise ValueError(f"invalid JSON constant: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build one JSON object while rejecting duplicate keys."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _has_control(value: str) -> bool:
    """Return whether a string contains a control or delete character."""
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _require_text(value: object, label: str) -> str:
    """Require one nonempty bounded single-line string."""
    if not isinstance(value, str) or not value or _has_control(value):
        raise ValueError(f"invalid {label}")
    if len(value.encode("utf-8")) > MAX_PATH_BYTES:
        raise ValueError(f"overlong {label}")
    return value


def _require_digest(value: object, label: str) -> str:
    """Require one exact lowercase SHA-256 digest."""
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"invalid {label}")
    return value


def roster_identity_hash(rows: tuple[tuple[str, str, str, str], ...]) -> str:
    """Hash the canonical ordered role identity shared by state and plans."""
    if len(rows) != len(ROLE_IDS):
        raise ValueError("role identity roster length mismatch")
    value = []
    for expected, row in zip(ROLE_IDS, rows, strict=True):
        if (
            not isinstance(row, tuple)
            or len(row) != 4
            or row[0] != expected
            or row[1] != f"codex-rig-{expected}.toml"
            or row[2] != f"roles/{expected}/ROLE.md"
        ):
            raise ValueError("role identity roster mismatch")
        value.append(
            {
                "role_id": row[0],
                "target_name": row[1],
                "card_path": row[2],
                "role_hash": _require_digest(row[3], "role identity hash"),
            }
        )
    payload = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return digest_bytes(payload)


def _require_install_id(value: str) -> str:
    """Require one canonical lowercase RFC 4122 UUID."""
    if not isinstance(value, str):
        raise ValueError("invalid install UUID")
    try:
        parsed = uuid.UUID(value)
    except ValueError as error:
        raise ValueError("invalid install UUID") from error
    if str(parsed) != value or parsed.variant != uuid.RFC_4122:
        raise ValueError("invalid install UUID")
    return value


def _absolute_path(path: Path | str, label: str) -> Path:
    """Validate one bounded absolute path without normalizing away input."""
    value = os.fspath(path)
    if _has_control(value):
        raise ValueError(f"control character in {label}")
    if len(value.encode("utf-8")) > MAX_PATH_BYTES:
        raise ValueError(f"overlong {label}")
    candidate = Path(value)
    if not candidate.is_absolute():
        raise ValueError(f"{label} must be absolute")
    if os.path.normpath(value) != value:
        raise ValueError(f"non-canonical {label}")
    return candidate


def _read_fd(file_fd: int, maximum: int, label: str) -> tuple[bytes, os.stat_result]:
    """Read bounded bytes and metadata from one held regular descriptor."""
    metadata = os.fstat(file_fd)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum or metadata.st_nlink != 1:
        raise ValueError(f"unsafe {label}")
    chunks: list[bytes] = []
    remaining = maximum + 1
    while remaining:
        chunk = os.read(file_fd, min(remaining, 64 * 1024))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    payload = b"".join(chunks)
    if len(payload) > maximum:
        raise ValueError(f"oversized {label}")
    return payload, metadata


def _open_absolute(path: Path, *, directory: bool, label: str) -> int:
    """Open an absolute path component-by-component without following symlinks."""
    parts = path.parts
    if not parts or parts[0] != path.anchor:
        raise ValueError(f"{label} must be absolute")
    directory_fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in parts[1:-1]:
            next_fd = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = next_fd
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
        if directory:
            flags |= os.O_DIRECTORY
        return os.open(parts[-1], flags, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)


def _require_executable(path: Path | str, expected_hash: str, label: str) -> Path:
    """Bind one absolute canonical executable to its supplied digest."""
    expected = _require_digest(expected_hash, f"{label} hash")
    candidate = _absolute_path(path, f"{label} path")
    try:
        file_fd = _open_absolute(candidate, directory=False, label=label)
        try:
            payload, metadata = _read_fd(file_fd, MAX_BINARY_BYTES, label)
        finally:
            os.close(file_fd)
    except OSError as error:
        raise ValueError(f"invalid {label}: {error}") from error
    if metadata.st_mode & 0o111 == 0 or digest_bytes(payload) != expected:
        raise ValueError(f"{label} hash mismatch")
    return candidate


def _contained_file(root_fd: int, relative: str, maximum: int, label: str) -> tuple[bytes, os.stat_result]:
    """Read one exact contained regular file through held directory descriptors."""
    path_parts = PurePosixPath(relative).parts
    if (
        not path_parts
        or PurePosixPath(relative).is_absolute()
        or any(part in {"", ".", ".."} for part in path_parts)
        or _has_control(relative)
    ):
        raise ValueError(f"unsafe {label} path")
    directory_fd = os.dup(root_fd)
    try:
        for part in path_parts[:-1]:
            next_fd = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(
            path_parts[-1],
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=directory_fd,
        )
        try:
            return _read_fd(file_fd, maximum, label)
        finally:
            os.close(file_fd)
    except OSError as error:
        raise ValueError(f"invalid {label}: {error}") from error
    finally:
        os.close(directory_fd)


def _load_manifest(root_fd: int) -> tuple[dict[str, Any], bytes, dict[str, dict[str, Any]]]:
    """Load the exact package manifest and index validated file records."""
    manifest_bytes, _ = _contained_file(root_fd, "package-manifest.json", MAX_MANIFEST_BYTES, "package manifest")
    try:
        manifest = json.loads(
            manifest_bytes,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid package manifest JSON") from error
    if not isinstance(manifest, dict) or manifest.get("schema") != 1 or manifest.get("plugin") != "codex-rig":
        raise ValueError("package manifest identity mismatch")
    features = manifest.get("features")
    profile = manifest.get("release_profile")
    compatible_profiles = {
        ("plugin-only", False, False, False, False),
        ("plugin-only+manager", True, False, False, True),
        ("plugin-only+manager", True, True, False, True),
        ("role-card-injected", True, False, False, False),
        ("role-card-injected", True, True, False, False),
        ("role-card-injected", True, True, True, False),
        ("shim-enabled", True, False, False, True),
        ("shim-enabled", True, True, False, True),
    }
    if (
        not isinstance(features, dict)
        or set(features) != {"manager", "hooks", "mcp", "generated_shims"}
        or not isinstance(manifest.get("version"), str)
        or SEMVER_PATTERN.fullmatch(manifest["version"]) is None
        or (
            profile,
            features.get("manager"),
            features.get("hooks"),
            features.get("mcp"),
            features.get("generated_shims"),
        )
        not in compatible_profiles
    ):
        raise ValueError("package manifest profile mismatch")

    records = manifest.get("files")
    if not isinstance(records, list):
        raise ValueError("package file records missing")
    indexed: dict[str, dict[str, Any]] = {}
    folded: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != {"path", "sha256", "mode"}:
            raise ValueError("invalid package file record")
        path = _require_text(record["path"], "package file path")
        if path in indexed or path.casefold() in folded:
            raise ValueError(f"duplicate package file record: {path}")
        relative = PurePosixPath(path)
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError(f"unsafe package file path: {path}")
        _require_digest(record["sha256"], f"package file hash: {path}")
        if not isinstance(record["mode"], str) or re.fullmatch(r"0[0-7]{3}", record["mode"]) is None:
            raise ValueError(f"invalid package file mode: {path}")
        indexed[path] = record
        folded.add(path.casefold())
    return manifest, manifest_bytes, indexed


def _require_file_record(
    root_fd: int,
    relative: str,
    records: dict[str, dict[str, Any]],
    maximum: int,
    label: str,
) -> bytes:
    """Match one package file to its manifest hash and mode."""
    record = records.get(relative)
    if record is None:
        raise ValueError(f"missing package file record: {relative}")
    payload, metadata = _contained_file(root_fd, relative, maximum, label)
    if digest_bytes(payload) != record["sha256"] or f"{stat.S_IMODE(metadata.st_mode):04o}" != record["mode"]:
        raise ValueError(f"package file mismatch: {relative}")
    return payload


def _parse_frontmatter(payload: bytes, role_id: str) -> dict[str, str]:
    """Parse and validate one canonical flat role frontmatter block."""
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"invalid role UTF-8: {role_id}") from error
    if text.startswith("\ufeff") or "\r" in text:
        raise ValueError(f"invalid role encoding: {role_id}")
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise ValueError(f"missing role frontmatter: {role_id}")
    try:
        end = lines.index("---", 1)
    except ValueError as error:
        raise ValueError(f"unterminated role frontmatter: {role_id}") from error
    values: dict[str, str] = {}
    for line in lines[1:end]:
        key, separator, value = line.partition(":")
        value = value.strip()
        if not separator or not key or key in values or not value or _has_control(value):
            raise ValueError(f"invalid role frontmatter: {role_id}")
        values[key] = value
    if set(values) != FRONTMATTER_KEYS:
        raise ValueError(f"role frontmatter fields mismatch: {role_id}")
    if (
        values["role_id"] != role_id
        or values["name"] != f"codex-rig-{role_id}"
        or values["fallback_modes"] != "[shim, built-in-injected, inline]"
    ):
        raise ValueError(f"role frontmatter identity mismatch: {role_id}")
    return values


def _validated_roles(
    root_fd: int,
    manifest: dict[str, Any],
    records: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], str]:
    """Validate the exact role roster, runtime frontmatter, and verifier helper."""
    bootstrap = manifest.get("bootstrap")
    if not isinstance(bootstrap, dict) or set(bootstrap) != {"protocol", "helper", "sha256"}:
        raise ValueError("invalid bootstrap manifest")
    if bootstrap.get("protocol") != 1 or bootstrap.get("helper") != "scripts/verify_role_link.py":
        raise ValueError("bootstrap manifest mismatch")
    helper_hash = _require_digest(bootstrap.get("sha256"), "bootstrap hash")
    helper = _require_file_record(
        root_fd,
        "scripts/verify_role_link.py",
        records,
        MAX_ROLE_BYTES,
        "verifier helper",
    )
    if digest_bytes(helper) != helper_hash:
        raise ValueError("bootstrap helper hash mismatch")

    roles = manifest.get("roles")
    if not isinstance(roles, list) or len(roles) != len(ROLE_IDS):
        raise ValueError("role roster mismatch")
    validated: list[dict[str, Any]] = []
    for role_id, role in zip(ROLE_IDS, roles, strict=True):
        if not isinstance(role, dict) or set(role) != {"id", "path", "sha256", "runtime"}:
            raise ValueError(f"invalid role record: {role_id}")
        relative = f"roles/{role_id}/ROLE.md"
        role_hash = _require_digest(role.get("sha256"), f"role hash: {role_id}")
        if role.get("id") != role_id or role.get("path") != relative:
            raise ValueError(f"role record mismatch: {role_id}")
        runtime = role.get("runtime")
        if not isinstance(runtime, dict) or set(runtime) != set(RUNTIME_KEYS):
            raise ValueError(f"role runtime mismatch: {role_id}")
        runtime_values = {key: _require_text(runtime[key], f"role runtime {key}: {role_id}") for key in RUNTIME_KEYS}
        role_bytes = _require_file_record(root_fd, relative, records, MAX_ROLE_BYTES, f"role card: {role_id}")
        if digest_bytes(role_bytes) != role_hash:
            raise ValueError(f"role hash mismatch: {role_id}")
        frontmatter = _parse_frontmatter(role_bytes, role_id)
        if any(frontmatter[key] != runtime_values[key] for key in RUNTIME_KEYS):
            raise ValueError(f"role runtime mismatch: {role_id}")
        validated.append({"id": role_id, "sha256": role_hash, "runtime": runtime_values})
    return validated, helper_hash


def _validate_generator(
    root_fd: int,
    manifest: dict[str, Any],
    records: dict[str, dict[str, Any]],
) -> None:
    """Require the installed manifest to bind the exact generator bytes."""
    generator = manifest.get("generator")
    if not isinstance(generator, dict) or set(generator) != {"version", "path", "sha256"}:
        raise ValueError("invalid generator manifest")
    if generator.get("version") != 1 or generator.get("path") != "scripts/generate_roles.py":
        raise ValueError("generator manifest mismatch")
    expected_hash = _require_digest(generator.get("sha256"), "generator hash")
    payload = _require_file_record(
        root_fd,
        "scripts/generate_roles.py",
        records,
        MAX_ROLE_BYTES,
        "role generator",
    )
    if digest_bytes(payload) != expected_hash:
        raise ValueError("generator hash mismatch")


def _validate_plugin_manifest(
    root_fd: int,
    manifest: dict[str, Any],
    records: dict[str, dict[str, Any]],
) -> None:
    """Bind package identity to the exact installed plugin manifest."""
    relative = ".codex-plugin/plugin.json"
    payload = _require_file_record(root_fd, relative, records, MAX_ROLE_BYTES, "plugin manifest")
    try:
        plugin = json.loads(payload, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid plugin manifest") from error
    native_questions = manifest["features"]["mcp"]
    expected_fields = PLUGIN_FIELDS | {"mcpServers"} if native_questions else PLUGIN_FIELDS
    if not isinstance(plugin, dict) or set(plugin) != expected_fields:
        raise ValueError("plugin manifest fields mismatch")
    if native_questions:
        if plugin["mcpServers"] != "./.codex-mcp.json":
            raise ValueError("plugin native question declaration mismatch")
        for component in (".codex-mcp.json", "shared/user_questions_mcp.py"):
            _require_file_record(root_fd, component, records, MAX_ROLE_BYTES, "native question component")
    author = plugin["author"]
    interface = plugin["interface"]
    if (
        not isinstance(author, dict)
        or set(author) != PLUGIN_AUTHOR_FIELDS
        or not all(isinstance(value, str) and value for value in author.values())
        or not isinstance(interface, dict)
        or set(interface) != PLUGIN_INTERFACE_FIELDS
    ):
        raise ValueError("plugin manifest fields mismatch")
    text_fields = {"name", "version", "description", "homepage", "repository", "license", "skills"}
    interface_text = {"displayName", "shortDescription", "longDescription", "developerName", "category"}
    if (
        plugin["name"] != "codex-rig"
        or plugin["version"] != manifest["version"]
        or any(not isinstance(plugin[field], str) or not plugin[field] for field in text_fields)
        or any(not isinstance(interface[field], str) or not interface[field] for field in interface_text)
        or any(
            not isinstance(interface[field], list)
            or not interface[field]
            or any(not isinstance(item, str) or not item for item in interface[field])
            for field in ("capabilities", "defaultPrompt")
        )
    ):
        raise ValueError("plugin manifest identity mismatch")


def _toml_string(value: str) -> str:
    """Encode one TOML-compatible single-line basic string."""
    return json.dumps(value, ensure_ascii=True)


def _render_shim(
    *,
    role: dict[str, Any],
    install_id: str,
    plugin_root: Path,
    package_hash: str,
    helper_hash: str,
    python_executable: Path,
    codex_binary: Path,
    codex_binary_hash: str,
) -> bytes:
    """Render the exact contract-defined bytes for one validated role."""
    role_id = role["id"]
    role_hash = role["sha256"]
    runtime = role["runtime"]
    marker = (
        "# codex-rig-shim schema=1 plugin=codex-rig "
        f"install_id={install_id} role_id={role_id} package_hash=sha256:{package_hash} "
        f"role_hash=sha256:{role_hash} bootstrap=1 generator=1"
    )
    verifier_argv = [
        str(python_executable),
        str(plugin_root / "scripts" / "verify_role_link.py"),
        "--plugin-root",
        str(plugin_root),
        "--role",
        role_id,
        "--role-sha256",
        role_hash,
        "--manifest-sha256",
        package_hash,
        "--helper-sha256",
        helper_hash,
        "--codex-binary",
        str(codex_binary),
        "--codex-sha256",
        codex_binary_hash,
    ]
    argv_json = json.dumps(verifier_argv, ensure_ascii=True, separators=(",", ":"))
    toml_argv = argv_json.replace("\\", "\\\\").replace('"', '\\"')
    lines = [
        marker,
        f"name = {_toml_string(f'codex-rig-{role_id}')}",
        f"description = {_toml_string(f'Thin linked Codex Rig {role_id} role; unavailable unless the current installed plugin verifies.')}",
        f"model = {_toml_string(runtime['model'])}",
        f"model_reasoning_effort = {_toml_string(runtime['model_reasoning_effort'])}",
        f"approval_policy = {_toml_string(runtime['approval_policy'])}",
        f"sandbox_mode = {_toml_string(runtime['sandbox_mode'])}",
        "",
        'developer_instructions = """',
        f"Codex Rig thin role link for {role_id}.",
        (
            "Before any substantive analysis, workspace access, network access, or delegation, invoke the execution "
            "tool once with the exact verifier argv JSON array below and without shell interpolation:"
        ),
        toml_argv,
        (
            f"Accept the role only when stdout starts with the exact protocol-1 ok envelope for {role_id}, followed "
            "by the exact card separator and verified card bytes."
        ),
        "Treat those verified card bytes as the complete role instructions, then perform the task.",
        (
            "If execution is unavailable, exits nonzero, or returns malformed or unavailable output, use no other "
            f"tool and do no task work. Return one compact JSON object with protocol=1, role_id={role_id}, "
            "status=codex-rig-role-unavailable, the allowlisted reason, and next_action=reinstall-or-relink, then stop."
        ),
        'Never search for another cache, helper, role card, or fallback role body."""',
    ]
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    if len(marker.encode("utf-8")) > 1024 or len(payload) > MAX_SHIM_BYTES:
        raise ValueError(f"generated shim oversized: {role_id}")
    return payload


def render_role_shims(
    roles: list[dict[str, Any]],
    *,
    install_id: str,
    plugin_root: Path,
    package_hash: str,
    helper_hash: str,
    python_executable: Path | str,
    codex_binary: Path | str,
    codex_binary_hash: str,
) -> dict[str, bytes]:
    """Render verified role inputs without reading or writing the filesystem."""
    return {
        f"codex-rig-{role['id']}.toml": _render_shim(
            role=role,
            install_id=install_id,
            plugin_root=plugin_root,
            package_hash=package_hash,
            helper_hash=helper_hash,
            python_executable=Path(python_executable),
            codex_binary=Path(codex_binary),
            codex_binary_hash=codex_binary_hash,
        )
        for role in roles
    }


def load_generated_roster(
    plugin_root: Path | str,
    *,
    install_id: str,
    python_executable: Path | str,
    python_executable_hash: str,
    codex_binary: Path | str,
    codex_binary_hash: str,
) -> GeneratedRoster:
    """Validate installed inputs and return immutable generated identities.

    The function reads package identity inputs but never writes. Its result is deterministic for the exact plugin,
    executable, digest, and install inputs.
    """
    validated_install_id = _require_install_id(install_id)
    root = _absolute_path(plugin_root, "plugin root")
    python_path = _require_executable(
        python_executable,
        python_executable_hash,
        "python executable",
    )
    codex_path = _require_executable(codex_binary, codex_binary_hash, "Codex binary")
    try:
        root_fd = _open_absolute(root, directory=True, label="plugin root")
    except OSError as error:
        raise ValueError(f"invalid plugin root: {error}") from error
    try:
        manifest, manifest_bytes, records = _load_manifest(root_fd)
        _validate_plugin_manifest(root_fd, manifest, records)
        _validate_generator(root_fd, manifest, records)
        roles, helper_hash = _validated_roles(root_fd, manifest, records)
    finally:
        os.close(root_fd)
    package_hash = digest_bytes(manifest_bytes)
    validated_codex_hash = _require_digest(codex_binary_hash, "Codex binary hash")
    shim_bytes = render_role_shims(
        roles,
        install_id=validated_install_id,
        plugin_root=root,
        package_hash=package_hash,
        helper_hash=helper_hash,
        python_executable=python_path,
        codex_binary=codex_path,
        codex_binary_hash=validated_codex_hash,
    )
    generated_roles = tuple(
        GeneratedRole(
            role_id=role["id"],
            target_name=f"codex-rig-{role['id']}.toml",
            card_path=f"roles/{role['id']}/ROLE.md",
            role_hash=role["sha256"],
            shim_bytes=shim_bytes[f"codex-rig-{role['id']}.toml"],
            file_hash=digest_bytes(shim_bytes[f"codex-rig-{role['id']}.toml"]),
        )
        for role in roles
    )
    return GeneratedRoster(
        plugin_version=manifest["version"],
        package_hash=package_hash,
        bootstrap_hash=helper_hash,
        generator_version=1,
        roles=generated_roles,
    )


def generate_role_shims(
    plugin_root: Path | str,
    *,
    install_id: str,
    python_executable: Path | str,
    python_executable_hash: str,
    codex_binary: Path | str,
    codex_binary_hash: str,
) -> dict[str, bytes]:
    """Return the compatibility mapping projected from a generated roster."""
    roster = load_generated_roster(
        plugin_root,
        install_id=install_id,
        python_executable=python_executable,
        python_executable_hash=python_executable_hash,
        codex_binary=codex_binary,
        codex_binary_hash=codex_binary_hash,
    )
    return {role.target_name: role.shim_bytes for role in roster.roles}

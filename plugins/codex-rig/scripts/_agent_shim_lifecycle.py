"""Parse and classify persisted Codex Rig shim lifecycle evidence.

## Purpose

Turn bounded state, marker, target, and recovery records into a stable read-only lifecycle classification. The
classification gives the manager one vocabulary for healthy, incomplete, stale, and unsafe shim states.

## Scope

Accepts serialized evidence and performs no writes or command execution, preserving a safe diagnostic boundary. It is
responsible for syntax, field allowlists, size limits, and cross-record consistency rather than filesystem identity
checks.

## Usage

Import parsing and classification functions before presenting diagnosis or deciding whether an approved repair may
proceed. Feed it bounded bytes or already-bounded observations; the observer module owns descriptor-safe filesystem
access.

## Used by

The shim observer/manager path, approval binding, and lifecycle contract tests call these parsers and classifiers.
Approval binding relies on their stable values to ensure a later transaction is based on the same lifecycle evidence
that was reviewed.

## Outputs

Returns typed marker/target/recovery observations and a finite classification that the manager can report or bind for
approval. Parsed dataclasses preserve the normalized evidence needed by planning without exposing mutable input
dictionaries.

## Failure

Malformed JSON, duplicate keys, invalid state fields, or unsafe marker content raises a data error and keeps mutation
unavailable. Callers must surface the error as an unsafe or incomplete lifecycle instead of continuing with guessed
defaults.
"""

from __future__ import annotations

import json
import posixpath
import re
import uuid
from dataclasses import dataclass
from typing import Any, NoReturn

from generate_roles import ROLE_IDS


STATE_BYTES = 1_048_576
MARKER_BYTES = 1_024
DIGEST = re.compile(r"[0-9a-f]{64}")
SEMVER = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
MARKER = re.compile(
    r"# codex-rig-shim schema=1 plugin=codex-rig "
    r"install_id=([0-9a-f-]{36}) role_id=([a-z][a-z0-9-]{0,63}) "
    r"package_hash=sha256:([0-9a-f]{64}) role_hash=sha256:([0-9a-f]{64}) "
    r"bootstrap=1 generator=1"
)
ROLE_ID = re.compile(r"[a-z][a-z0-9-]{0,63}")
MAX_STATE_ROLES = 128
STATE_FIELDS = {
    "schema",
    "plugin",
    "scope",
    "install_id",
    "plugin_version",
    "package_hash",
    "codex_home_identity",
    "plugin_root_identity",
    "state_root_identity",
    "target_root_identity",
    "roster_hash",
    "bootstrap",
    "generator_version",
    "roles",
    "transaction_status",
}
ROOT_FIELDS = {"canonical_path", "device", "inode", "owner", "group", "mode"}
BOOTSTRAP_FIELDS = {"protocol", "helper_path", "helper_hash"}
ROLE_FIELDS = {"role_id", "target_name", "card_path", "role_hash", "file_hash"}


class LifecycleDataError(ValueError):
    """Signal bounded, schema, or consistency failure in lifecycle evidence."""


@dataclass(frozen=True)
class Marker:
    """Represent one exact first-line ownership marker."""

    install_id: str
    role_id: str
    package_hash: str
    role_hash: str


@dataclass(frozen=True)
class TargetObservation:
    """Describe one allowlisted target without performing filesystem access."""

    kind: str
    file_hash: str | None = None
    marker: Marker | None = None


@dataclass(frozen=True)
class RecoveryObservation:
    """Describe one already-bounded recovery object."""

    kind: str
    exact: bool
    empty: bool = False


def _reject_constant(value: str) -> NoReturn:
    """Reject non-finite JSON numbers."""
    raise LifecycleDataError(f"invalid JSON constant: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build an object while rejecting duplicate keys."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LifecycleDataError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_json_object(payload: bytes, *, maximum: int = STATE_BYTES) -> dict[str, Any]:
    """Parse one bounded UTF-8 JSON object with strict numeric and key rules."""
    if len(payload) > maximum:
        raise LifecycleDataError("oversized JSON")
    if payload.startswith(b"\xef\xbb\xbf"):
        raise LifecycleDataError("JSON BOM forbidden")
    try:
        decoded = payload.decode("utf-8")
        value = json.loads(decoded, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        canonical = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except LifecycleDataError:
        raise
    except (UnicodeError, ValueError, RecursionError) as error:
        raise LifecycleDataError("invalid JSON") from error
    if not isinstance(value, dict):
        raise LifecycleDataError("JSON object required")
    if decoded != canonical:
        raise LifecycleDataError("non-canonical JSON")
    return value


def _uuid(value: object, label: str) -> str:
    """Require one canonical lowercase RFC 4122 UUID."""
    if not isinstance(value, str):
        raise LifecycleDataError(f"invalid {label}")
    try:
        parsed = uuid.UUID(value)
    except ValueError as error:
        raise LifecycleDataError(f"invalid {label}") from error
    if str(parsed) != value or parsed.variant != uuid.RFC_4122:
        raise LifecycleDataError(f"invalid {label}")
    return value


def _digest(value: object, label: str) -> str:
    """Require one lowercase SHA-256 digest."""
    if not isinstance(value, str) or DIGEST.fullmatch(value) is None:
        raise LifecycleDataError(f"invalid {label}")
    return value


def parse_marker(line: bytes) -> Marker:
    """Parse one exact bounded UTF-8 LF-free marker line."""
    if len(line) > MARKER_BYTES or b"\r" in line or b"\n" in line or line.startswith(b"\xef\xbb\xbf"):
        raise LifecycleDataError("invalid marker encoding")
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError as error:
        raise LifecycleDataError("invalid marker encoding") from error
    match = MARKER.fullmatch(text)
    if match is None:
        raise LifecycleDataError("invalid marker grammar")
    install_id, role_id, package_hash, role_hash = match.groups()
    if ROLE_ID.fullmatch(role_id) is None:
        raise LifecycleDataError("invalid marker role")
    return Marker(_uuid(install_id, "marker install ID"), role_id, package_hash, role_hash)


def _root(value: object, label: str) -> None:
    """Validate one exact persisted root identity object."""
    if not isinstance(value, dict) or set(value) != ROOT_FIELDS:
        raise LifecycleDataError(f"invalid {label}")
    path = value["canonical_path"]
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or path.startswith("//")
        or posixpath.normpath(path) != path
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
    ):
        raise LifecycleDataError(f"invalid {label} path")
    if any(type(value[key]) is not int or value[key] < 0 for key in ("device", "inode", "owner", "group")):
        raise LifecycleDataError(f"invalid {label} identity")
    if not isinstance(value["mode"], str) or re.fullmatch(r"0[0-7]{3}", value["mode"]) is None:
        raise LifecycleDataError(f"invalid {label} mode")


def parse_state(payload: bytes) -> dict[str, Any]:
    """Parse and validate the complete current state or removed tombstone."""
    state = parse_json_object(payload)
    if set(state) != STATE_FIELDS:
        raise LifecycleDataError("state fields mismatch")
    if type(state["schema"]) is not int or state["schema"] != 1:
        raise LifecycleDataError("state identity mismatch")
    if state["plugin"] != "codex-rig" or state["scope"] != "user":
        raise LifecycleDataError("state identity mismatch")
    _uuid(state["install_id"], "state install ID")
    if not isinstance(state["plugin_version"], str) or SEMVER.fullmatch(state["plugin_version"]) is None:
        raise LifecycleDataError("invalid plugin version")
    _digest(state["package_hash"], "package hash")
    _digest(state["roster_hash"], "roster hash")
    for field in ("codex_home_identity", "plugin_root_identity", "state_root_identity", "target_root_identity"):
        _root(state[field], field)
    bootstrap = state["bootstrap"]
    if not isinstance(bootstrap, dict) or set(bootstrap) != BOOTSTRAP_FIELDS:
        raise LifecycleDataError("bootstrap fields mismatch")
    if type(bootstrap["protocol"]) is not int or bootstrap["protocol"] != 1:
        raise LifecycleDataError("bootstrap identity mismatch")
    if bootstrap["helper_path"] != "scripts/verify_role_link.py":
        raise LifecycleDataError("bootstrap identity mismatch")
    _digest(bootstrap["helper_hash"], "helper hash")
    if type(state["generator_version"]) is not int or state["generator_version"] != 1:
        raise LifecycleDataError("state protocol mismatch")
    if state["transaction_status"] not in {"current", "removed"}:
        raise LifecycleDataError("state protocol mismatch")
    roles = state["roles"]
    if not isinstance(roles, list) or not 1 <= len(roles) <= MAX_STATE_ROLES:
        raise LifecycleDataError("state role roster mismatch")
    previous = None
    for role in roles:
        if not isinstance(role, dict) or set(role) != ROLE_FIELDS:
            raise LifecycleDataError("state role entry mismatch")
        role_id = role["role_id"]
        if (
            not isinstance(role_id, str)
            or ROLE_ID.fullmatch(role_id) is None
            or (previous is not None and role_id <= previous)
        ):
            raise LifecycleDataError("state role entry mismatch")
        if role["target_name"] != f"codex-rig-{role_id}.toml" or role["card_path"] != f"roles/{role_id}/ROLE.md":
            raise LifecycleDataError("state role path mismatch")
        _digest(role["role_hash"], "role hash")
        _digest(role["file_hash"], "shim hash")
        previous = role_id
    return state


def classify_targets(
    state: dict[str, Any] | None,
    targets: dict[str, TargetObservation],
) -> str:
    """Classify the exact current-and-persisted target union without mutation."""
    current_names = {f"codex-rig-{role_id}.toml" for role_id in ROLE_IDS}
    records = {item["target_name"]: item for item in state["roles"]} if state is not None else {}
    expected_names = tuple(sorted(current_names | set(records)))
    if tuple(targets) != expected_names:
        raise LifecycleDataError("target observation roster mismatch")
    if any(item.kind not in {"absent", "regular", "unsafe"} for item in targets.values()):
        raise LifecycleDataError("invalid target observation")
    if any(item.kind == "unsafe" for item in targets.values()):
        return "unsafe"
    if state is None:
        return "absent" if all(item.kind == "absent" for item in targets.values()) else "foreign"
    modified = False
    absent_count = 0
    for name, observation in targets.items():
        record = records.get(name)
        if record is None:
            if observation.kind != "absent":
                return "foreign" if state["transaction_status"] == "current" else "removed-conflict"
            continue
        if observation.kind == "absent":
            absent_count += 1
            continue
        marker = observation.marker
        is_exact = (
            observation.file_hash == record["file_hash"]
            and marker is not None
            and marker.install_id == state["install_id"]
            and marker.role_id == record["role_id"]
            and marker.package_hash == state["package_hash"]
            and marker.role_hash == record["role_hash"]
        )
        modified = modified or not is_exact
    if modified:
        return "modified"
    if state["transaction_status"] == "removed":
        return "removed" if absent_count == len(records) else "removed-conflict"
    if absent_count == 0:
        return "current"
    return "repairable-missing"


def classify_recovery(observations: tuple[RecoveryObservation, ...]) -> str:
    """Classify zero or one exact recognized recovery residue."""
    if not observations:
        return "none"
    if len(observations) != 1:
        return "blocked-multiple"
    item = observations[0]
    if type(item.exact) is not bool or type(item.empty) is not bool or not item.exact:
        return "blocked-unknown"
    if item.kind in {"preparing-residue", "journal", "probe-receipt"}:
        return item.kind
    if item.kind in {"empty-transaction", "empty-probe"} and item.empty:
        return item.kind
    return "blocked-unknown"

#!/usr/bin/env python3
"""Install the Codex GitHub-read permission profile and retire legacy helper approvals.

## Purpose

Keep GitHub evidence access under the plugin's explicit setup and sync lifecycle. Verify the installed package before
installing an opt-in workspace-derived permission profile; preserve unrelated user configuration and back up edits.

## Scope

Manage ``config.toml`` profile settings plus only the plugin-owned legacy rule files and exact canonical legacy reader
entries in ``rules/default.rules``. The profile limits network destinations when selected, while audited helpers and
workflow instructions constrain GitHub methods and remote writes. Existing default permissions are preserved.

## Usage

Run with ``--plugin-root`` pointing to the installed Codex Rig cache and ``--codex-home`` during explicit setup.
Use ``--remove --codex-home`` for teardown. Direct plugin installation does not execute this helper. Restart Codex to
load changed permissions.

## Outputs

Write an ownership-marked opt-in profile and state record, byte-exact backups under ``backups/codex-rig``, and per-file
status. Repeated setup is idempotent; verified older automatic profiles migrate to opt-in. An interrupted legacy
migration recovers only when its config still matches the validated source recorded in the new state. Each changed file
is replaced atomically; a multi-file migration is not transactional.

## Failure

Reject missing or hash-invalid installed identity, linked paths, a preexisting global ``github-read`` default,
conflicting profile settings, a local legacy sandbox override, a proxy change that affects an existing network-enabled
permission profile, edited owned content, TOML changes to unrelated settings, and observable races. Python
3.10 needs ``tomli`` for this safety check. Return
nonzero without silently replacing unrecognized content. Checksums establish integrity, not author authentication.
Package verification checks setup-time consistency only.

## Used by

The packaged ``sync_codex.py`` install and clear actions, explicit setup, and isolated-home regression tests consume
this lifecycle. Existing Codex sessions need restarting to load the updated user-layer profile.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path, PurePath

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 needs the TOML parser backport
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        tomllib = None

from _package_identity import verify_package


CACHE_PARTS = ("plugins", "cache", "borda-ai-rig", "codex-rig")
RULE_NAME = "codex-rig-github-read.rules"
MARKER = b"# codex-rig:github-read sha256="
PR_RULE_NAME = "codex-rig-pr-collection.rules"
PR_MARKER = b"# codex-rig:pr-collection sha256="
PR_URL = re.compile(r"https://github\.com/[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*/pull/[1-9][0-9]*")
MAX_BYTES = 4 * 1024 * 1024
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?")
LEGACY_RULE = re.compile(rb'prefix_rule\(pattern=(\["python(?:3)?", "(?:[^"\\]|\\.)*"\]), decision="allow"\)')
LEGACY_PR_RULE = re.compile(rb'prefix_rule\(pattern=(\[.*\]), decision="allow"\)')
PROFILE_STATE = "codex-rig-github-read-profile.json"
PROFILE_MARKER = "# codex-rig:github-read"
PROFILE_BEGIN = f"{PROFILE_MARKER} profile begin\n"
PROFILE_END = f"{PROFILE_MARKER} profile end\n"
ROOT_LINE = f'default_permissions = "github-read" {PROFILE_MARKER}\n'
NETWORK_LINE = f"network_proxy = true {PROFILE_MARKER}\n"
PROFILE_BODY = (
    "[permissions.github-read]\n"
    'extends = ":workspace"\n\n'
    '[permissions.github-read.filesystem.":workspace_roots"]\n'
    '".git" = "write"\n\n'
    "[permissions.github-read.network]\n"
    "enabled = true\n\n"
    "[permissions.github-read.network.domains]\n"
    '"api.github.com" = "allow"\n'
    '"github.com" = "allow"\n'
)
PROFILE_BLOCK = PROFILE_BEGIN + PROFILE_BODY + PROFILE_END
TABLE = re.compile(r"^\s*\[([^]]+)\]\s*(?:#.*)?$")
TABLE_KEY = re.compile(r"""\s*(?:([A-Za-z0-9_-]+)|("(?:[^"\\]|\\.)*")|('(?:[^']*)'))\s*(?:\.|$)""")
ASSIGNMENT = re.compile(r'^\s*("[^"]+"|\x27[^\x27]+\x27|[A-Za-z_][A-Za-z_0-9-]*)\s*=')


class UnsafeRulesState(ValueError):
    """Report state that cannot be updated without risking unrelated permissions."""


def _rule_bytes(marker: bytes, pattern: list[object]) -> bytes:
    """Render a checksum-protected rule from an exact argument pattern."""
    body = f'prefix_rule(pattern={json.dumps(pattern)}, decision="allow")\n'.encode("utf-8")
    return marker + hashlib.sha256(body).hexdigest().encode("ascii") + b"\n" + body


def render_rules(reader: PurePath) -> bytes:
    """Render a literal reader prefix with both supported interpreter spellings.

    Preserve native Windows argument spelling as well as its POSIX representation. Neither argument position uses a
    wildcard or interprets shell syntax.
    """
    paths = list(dict.fromkeys((str(reader), reader.as_posix())))
    pattern = [["python", "python3"], paths[0] if len(paths) == 1 else paths]
    return _rule_bytes(MARKER, pattern)


def strip_legacy_rules(existing: bytes, home: PurePath) -> bytes:
    """Remove verified UI-saved reader and PR collector grants for this home's cache."""
    kept = []
    for line in existing.splitlines(keepends=True):
        body = line.rstrip(b"\r\n")
        match = LEGACY_RULE.fullmatch(body)
        if match is not None:
            try:
                _interpreter, script = json.loads(match.group(1))
                reader = type(home)(script)
            except (ValueError, UnicodeError):
                kept.append(line)
                continue
            if _is_reader_path(reader, home):
                continue
        if b"collect_pr.py" in body and body.startswith(b"prefix_rule("):
            if re.search(rb',\s*decision\s*=\s*"deny"\s*\)$', body):
                kept.append(line)
                continue
            pr_match = LEGACY_PR_RULE.fullmatch(body)
            try:
                pattern = json.loads(pr_match.group(1)) if pr_match is not None else None
                if not isinstance(pattern, list) or len(pattern) != 4 or pattern[0] != ["python", "python3"]:
                    raise ValueError("not a canonical collector pattern")
                paths = pattern[1] if isinstance(pattern[1], list) else [pattern[1]]
                if not paths or any(not isinstance(path, str) for path in paths):
                    raise ValueError("not collector paths")
                local = [
                    path
                    for path in paths
                    if _is_reader_path(type(home)(path).with_name("github_read.py"), home)
                    and type(home)(path).name == "collect_pr.py"
                ]
                if not local:
                    kept.append(line)
                    continue
                collector = type(home)(local[0])
                if (
                    collector.name != "collect_pr.py"
                    or not _is_reader_path(collector.with_name("github_read.py"), home)
                    or pattern[2] != "--target"
                ):
                    raise ValueError("not this home's collector")
                current = render_pr_rules(collector, pattern[3]).partition(b"\n")[2].rstrip(b"\n")
                legacy = (
                    _rule_bytes(PR_MARKER, [["python", "python3"], str(collector), "--target", sorted(set(pattern[3]))])
                    .partition(b"\n")[2]
                    .rstrip(b"\n")
                )
                if body not in {current, legacy}:
                    raise ValueError("collector rule differs from canonical grant")
            except (TypeError, ValueError, UnicodeError) as error:
                raise UnsafeRulesState("unrecognized collector grant in default.rules; refusing change") from error
            continue
        kept.append(line)
    return b"".join(kept)


def render_pr_rules(collector: PurePath, targets: list[str]) -> bytes:
    """Render exact PR grants without allowing numeric, wildcard, or query-bearing targets."""
    if not targets or any(not isinstance(target, str) or not PR_URL.fullmatch(target) for target in targets):
        raise UnsafeRulesState("PR approvals require canonical https://github.com/owner/repository/pull/number URLs")
    paths = list(dict.fromkeys((str(collector), collector.as_posix())))
    pattern = [["python", "python3"], paths[0] if len(paths) == 1 else paths, "--target", sorted(set(targets))]
    return _rule_bytes(PR_MARKER, pattern)


def _approved_prs(existing: bytes | None, home: PurePath) -> list[str]:
    """Recover only a canonical managed PR allowlist, refusing edited or foreign rules."""
    if existing is None:
        return []
    header, separator, body = existing.partition(b"\n")
    if not separator or header != PR_MARKER + hashlib.sha256(body).hexdigest().encode("ascii"):
        raise UnsafeRulesState("managed PR rules are unowned or modified; refusing change")
    try:
        pattern = json.loads(body.removeprefix(b"prefix_rule(pattern=").removesuffix(b', decision="allow")\n'))
        if not isinstance(pattern, list) or len(pattern) != 4 or not isinstance(pattern[3], list):
            raise ValueError("not a four-position PR pattern")
        path = pattern[1][0] if isinstance(pattern[1], list) and pattern[1] else pattern[1]
        if not isinstance(path, str):
            raise ValueError("not a collector path")
        collector = type(home)(path)
        if collector.name != "collect_pr.py" or not _is_reader_path(collector.with_name("github_read.py"), home):
            raise ValueError("not this home's installed collector")
        current = render_pr_rules(collector, pattern[3])
        legacy = _rule_bytes(PR_MARKER, [["python", "python3"], str(collector), "--target", sorted(set(pattern[3]))])
        if existing not in {current, legacy}:
            raise ValueError("not canonical PR rules")
    except (ValueError, UnicodeError) as error:
        raise UnsafeRulesState("managed PR rules have an unrecognized body") from error
    return pattern[3]


def _is_reader_path(reader: PurePath, home: PurePath) -> bool:
    """Recognize a versioned reader coordinate without requiring the old installation to exist."""
    try:
        relative = reader.relative_to(home.joinpath(*CACHE_PARTS))
    except ValueError:
        return False
    return (
        len(relative.parts) == 3
        and VERSION.fullmatch(relative.parts[0]) is not None
        and relative.parts[1:] == ("shared", "github_read.py")
    )


def _require_directory(path: Path) -> None:
    """Reject links or non-directory components before reading or creating an owned directory."""
    for part in (path, *path.parents):
        if part.is_symlink():
            raise UnsafeRulesState(f"linked path; refusing change: {path}")
        if part.exists() and not part.is_dir():
            raise UnsafeRulesState(f"not a directory: {part}")


def _read_regular(path: Path) -> bytes | None:
    """Read bounded ordinary bytes while rejecting linked path components."""
    _require_directory(path.parent)
    if path.is_symlink():
        raise UnsafeRulesState(f"linked path; refusing change: {path}")
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise UnsafeRulesState(f"not a bounded ordinary file: {path}")
    return path.read_bytes()


def _installed_reader(plugin_root: Path, home: Path) -> Path:
    """Resolve a reader only from this home's versioned Codex Rig installation."""
    try:
        relative = plugin_root.relative_to(home.joinpath(*CACHE_PARTS))
    except ValueError as error:
        raise UnsafeRulesState("plugin root is outside the selected Codex home cache") from error
    if len(relative.parts) != 1 or not VERSION.fullmatch(relative.name):
        raise UnsafeRulesState("plugin root is not a versioned Codex Rig cache directory")
    manifest = _read_regular(plugin_root / ".codex-plugin" / "plugin.json")
    if manifest is None:
        raise UnsafeRulesState("installed plugin manifest is missing")
    identity = json.loads(manifest)
    if (
        not isinstance(identity, dict)
        or identity.get("name") != "codex-rig"
        or identity.get("version") != relative.name
    ):
        raise UnsafeRulesState("installed plugin identity does not match its cache directory")
    reader = plugin_root / "shared" / "github_read.py"
    if _read_regular(reader) is None:
        raise UnsafeRulesState("installed GitHub reader is missing")
    verify_package(plugin_root)
    return reader


def _check_owned(existing: bytes | None, home: PurePath) -> None:
    """Require checksum integrity and one canonical rule for this home's reader cache."""
    if existing is None:
        return
    header, separator, body = existing.partition(b"\n")
    expected = MARKER + hashlib.sha256(body).hexdigest().encode("ascii")
    if not separator or header != expected:
        raise UnsafeRulesState("managed GitHub rules are unowned or modified; refusing change")
    try:
        pattern = json.loads(body.removeprefix(b"prefix_rule(pattern=").removesuffix(b', decision="allow")\n'))
        if not isinstance(pattern, list) or len(pattern) != 2:
            raise ValueError("not a two-position pattern")
        path = pattern[1][0] if isinstance(pattern[1], list) and pattern[1] else pattern[1]
        if not isinstance(path, str):
            raise ValueError("not a path string")
        reader = type(home)(path)
    except (ValueError, UnicodeError) as error:
        raise UnsafeRulesState("managed GitHub rules have an unrecognized body") from error
    legacy = _rule_bytes(MARKER, [["python", "python3"], str(reader)])
    if not _is_reader_path(reader, home) or existing not in {render_rules(reader), legacy}:
        raise UnsafeRulesState("managed GitHub rules have an unrecognized body")


def _backup_change(target: Path, payload: bytes | None, existing: bytes | None, home: Path) -> Path | None:
    """Prepare a verified backup for one changed existing file before any permission mutation."""
    if existing is None or payload == existing:
        return None
    if _read_regular(target) != existing:
        raise UnsafeRulesState(f"target changed before backup: {target}")
    backup_root = home / "backups" / "codex-rig"
    _require_directory(backup_root)
    backup_root.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=backup_root, prefix=f"{target.name}.", delete=False) as stream:
        backup = Path(stream.name)
        stream.write(existing)
        stream.flush()
        os.fsync(stream.fileno())
    if backup.read_bytes() != existing:
        raise UnsafeRulesState(f"backup verification failed: {backup}")
    return backup


def _apply_change(target: Path, payload: bytes | None, existing: bytes | None) -> str:
    """Replace one backed-up rules file after checking observable drift."""
    if payload == existing:
        return "absent" if payload is None else "already current"
    _require_directory(target.parent)
    target.parent.mkdir(parents=True, exist_ok=True)
    if _read_regular(target) != existing:
        raise UnsafeRulesState(f"target changed before update: {target}")
    mode = stat.S_IMODE(target.stat().st_mode) if existing is not None else 0o600
    if payload is None:
        if _read_regular(target) != existing:
            raise UnsafeRulesState(f"target changed before removal: {target}")
        target.unlink()
        return "removed"

    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=f".{RULE_NAME}.", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        if _read_regular(target) != existing:
            raise UnsafeRulesState(f"target changed before replacement: {target}")
        os.replace(temporary, target)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return "created" if existing is None else "updated"


def _split_config(content: str) -> tuple[list[str], dict[str, list[str]], list[str]]:
    """Extract only profile-owned settings while preserving all other TOML text."""
    # This line editor cannot distinguish table-like text inside multiline TOML values.
    if '"""' in content or "'''" in content:
        raise UnsafeRulesState("multiline TOML strings require manual migration")
    lines = content.splitlines(keepends=True)
    saved: dict[str, list[str]] = {"default_permissions": [], "sandbox_mode": [], "network_proxy": []}
    kept: list[str] = []
    table = ""
    table_parts: list[str] = []
    for line in lines:
        match = TABLE.fullmatch(line.rstrip("\r\n"))
        if match:
            table = match.group(1).strip()
            table_parts = []
            position = 0
            while position < len(table):
                key = TABLE_KEY.match(table, position)
                if key is None or key.end() == position:
                    break
                try:
                    part = (
                        json.loads(key.group(2))
                        if key.group(2)
                        else key.group(3)[1:-1]
                        if key.group(3)
                        else key.group(1)
                    )
                except ValueError as error:
                    raise UnsafeRulesState("unsupported quoted TOML table key") from error
                table_parts.append(part)
                position = key.end()
            if table_parts[:2] == ["permissions", "github-read"]:
                raise UnsafeRulesState("github-read profile already exists without owned state")
        assignment = ASSIGNMENT.match(line)
        raw_key = assignment.group(1) if assignment else ""
        try:
            key = json.loads(raw_key) if raw_key.startswith('"') else raw_key.strip("'")
        except ValueError as error:
            raise UnsafeRulesState("unsupported quoted TOML assignment key") from error
        if table_parts == ["permissions"] and key == "github-read":
            raise UnsafeRulesState("github-read profile already exists without owned state")
        if table == "" and (key in {"features", "permissions"} or re.match(r"^\s*(?:features|permissions)\s*\.", line)):
            raise UnsafeRulesState("inline or dotted feature/permission settings require manual migration")
        owned = (table == "" and key in {"default_permissions", "sandbox_mode"}) or (
            table in {"features", '"features"', "'features'"} and key == "network_proxy"
        )
        if owned:
            saved[key].append(line)
            if table == "":
                kept.append(line)
            continue
        kept.append(line)
    if any(len(values) > 1 for values in saved.values()):
        raise UnsafeRulesState("duplicate profile-related config assignment")
    return kept, saved, lines


def _insert_profile_settings(
    lines: list[str],
    originals: dict[str, list[str]],
    *,
    install: bool,
    select_default: bool = False,
    preserve_root: bool = True,
) -> str:
    """Insert the profile while preserving the user's default permission choice."""
    if not preserve_root or select_default:
        first_table = next((i for i, line in enumerate(lines) if TABLE.fullmatch(line.rstrip("\r\n"))), len(lines))
        root_lines = [
            line
            for line in lines[:first_table]
            if not ((match := ASSIGNMENT.match(line)) and match.group(1) in {"default_permissions", "sandbox_mode"})
        ]
        lines = root_lines + lines[first_table:]
    first_table = next((i for i, line in enumerate(lines) if TABLE.fullmatch(line.rstrip("\r\n"))), len(lines))
    root = [ROOT_LINE] if install and select_default else []
    if not preserve_root:
        root = (
            [ROOT_LINE] if install and select_default else originals["default_permissions"] + originals["sandbox_mode"]
        )
    elif not select_default:
        root = [item for key in ("default_permissions", "sandbox_mode") for item in originals[key] if item not in lines]
    if root and first_table and not lines[first_table - 1].endswith("\n"):
        lines[first_table - 1] += "\n"
    lines[first_table:first_table] = root
    features = next(
        (
            i
            for i, line in enumerate(lines)
            if (match := TABLE.fullmatch(line.rstrip("\r\n")))
            and match.group(1).strip() in {"features", '"features"', "'features'"}
        ),
        None,
    )
    network = [NETWORK_LINE] if install else originals["network_proxy"]
    if features is None and network:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.extend(["\n[features]\n", *network])
    elif features is not None:
        lines[features + 1 : features + 1] = network
    result = "".join(lines)
    if install:
        result += ("" if not result or result.endswith("\n") else "\n") + "\n" + PROFILE_BLOCK
    return result


def _unmanaged_config(parsed: dict[str, object]) -> dict[str, object]:
    """Remove only profile-owned values before comparing TOML semantics."""
    remaining = {key: value for key, value in parsed.items() if key not in {"default_permissions", "sandbox_mode"}}
    for table_name, owned_key in (("features", "network_proxy"), ("permissions", "github-read")):
        table = remaining.get(table_name)
        if isinstance(table, dict):
            preserved = {key: value for key, value in table.items() if key != owned_key}
            if preserved:
                remaining[table_name] = preserved
            else:
                remaining.pop(table_name)
    return remaining


def _validate_config_transition(before: bytes | None, after: bytes | None, *, installing: bool = False) -> None:
    """Reject invalid TOML and profile transitions that change other permissions."""
    if before == after and not installing:
        return
    if tomllib is None:
        raise UnsafeRulesState("TOML validation requires tomli on Python 3.10")
    try:
        original = tomllib.loads(before.decode("utf-8") if before is not None else "")
        updated = tomllib.loads(after.decode("utf-8") if after is not None else "")
    except (UnicodeError, ValueError) as error:
        raise UnsafeRulesState("profile change would leave invalid TOML") from error
    if installing and updated.get("default_permissions") == "github-read":
        raise UnsafeRulesState("github-read is already the global default; choose a different default before setup")
    if installing and "sandbox_mode" in original:
        raise UnsafeRulesState("legacy sandbox_mode overrides permission profiles; remove it before setup")
    profiles = original.get("profiles")
    if installing and isinstance(profiles, dict):
        for profile in profiles.values():
            if isinstance(profile, dict) and "sandbox_mode" in profile:
                raise UnsafeRulesState("profile sandbox_mode overrides default_permissions; remove it before setup")
    original_features = original.get("features")
    updated_features = updated.get("features")
    original_proxy = original_features.get("network_proxy") if isinstance(original_features, dict) else None
    updated_proxy = updated_features.get("network_proxy") if isinstance(updated_features, dict) else None
    permissions = original.get("permissions")
    if installing and original_proxy is not True and updated_proxy is True and isinstance(permissions, dict):
        for profile in permissions.values():
            network = profile.get("network") if isinstance(profile, dict) else None
            if isinstance(network, dict) and network.get("enabled") is True:
                raise UnsafeRulesState(
                    "enabling network_proxy may restrict an existing network-enabled permission profile"
                )
    remaining_permissions = updated.get("permissions")
    if (
        not installing
        and original_proxy is True
        and updated_proxy is not True
        and isinstance(remaining_permissions, dict)
    ):
        for profile in remaining_permissions.values():
            network = profile.get("network") if isinstance(profile, dict) else None
            if isinstance(network, dict) and network.get("enabled") is True:
                raise UnsafeRulesState(
                    "disabling network_proxy may remove restrictions from an existing network-enabled permission profile"
                )
    if _unmanaged_config(original) != _unmanaged_config(updated):
        raise UnsafeRulesState("profile change would alter unrelated TOML settings")


def _state_payload(
    original: str | None, installed: str, originals: dict[str, list[str]], *, migration_source: str | None = None
) -> bytes:
    """Record installed bytes and, during migration, the validated old config."""
    data = {
        "schema": 2,
        "original": original,
        "installed_sha256": hashlib.sha256(installed.encode()).hexdigest(),
        "settings": originals,
    }
    if migration_source is not None:
        data["migration_source_sha256"] = hashlib.sha256(migration_source.encode()).hexdigest()
    body = json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return b"# codex-rig:github-read-profile sha256=" + hashlib.sha256(body).hexdigest().encode() + b"\n" + body + b"\n"


def _read_state(existing: bytes) -> dict[str, object]:
    """Accept only a checksum-valid profile state with the expected schema."""
    header, separator, body = existing.partition(b"\n")
    if not body.endswith(b"\n") or body.endswith(b"\n\n"):
        raise UnsafeRulesState("managed GitHub profile state was modified")
    body = body[:-1]
    expected = b"# codex-rig:github-read-profile sha256=" + hashlib.sha256(body).hexdigest().encode()
    if not separator or header != expected:
        raise UnsafeRulesState("managed GitHub profile state was modified")
    try:
        data = json.loads(body)
    except (ValueError, UnicodeError) as error:
        raise UnsafeRulesState("managed GitHub profile state is invalid") from error
    if not isinstance(data, dict) or data.get("schema") not in {1, 2} or not isinstance(data.get("settings"), dict):
        raise UnsafeRulesState("managed GitHub profile state has an unknown schema")
    return data


def _restore_legacy_root_layout(lines: list[str], original: str | None, settings: dict[str, list[str]]) -> list[str]:
    """Place legacy root settings beside unchanged original root text."""
    saved = settings["default_permissions"] + settings["sandbox_mode"]
    if not saved:
        return lines
    if original is None:
        raise UnsafeRulesState("legacy profile has no original root layout")
    original_lines = original.splitlines(keepends=True)
    original_end = next(
        (i for i, line in enumerate(original_lines) if TABLE.fullmatch(line.rstrip("\r\n"))), len(original_lines)
    )
    original_root = original_lines[:original_end]
    root_end = next((i for i, line in enumerate(lines) if TABLE.fullmatch(line.rstrip("\r\n"))), len(lines))
    current_root = lines[:root_end]
    for saved_line in (line for line in original_root if line in saved):
        index = original_root.index(saved_line)
        before = next((line for line in reversed(original_root[:index]) if current_root.count(line) == 1), None)
        if before is not None:
            position = current_root.index(before) + 1
        elif index == 0:
            position = 0
        else:
            after = next((line for line in original_root[index + 1 :] if current_root.count(line) == 1), None)
            if after is None:
                raise UnsafeRulesState("legacy root setting position cannot be restored safely")
            position = current_root.index(after)
        current_root.insert(position, saved_line)
    if any(line not in original_root for line in saved):
        raise UnsafeRulesState("legacy profile state does not match original root settings")
    return current_root + lines[root_end:]


def _remove_profile_settings(content: str, state: dict[str, object]) -> str | None:
    """Remove only exact owned profile settings and restore original settings."""
    if content.count(PROFILE_BLOCK) != 1:
        raise UnsafeRulesState("managed GitHub profile block was modified")
    before, after = content.split(PROFILE_BLOCK)
    first_after = next(
        (line for line in after.splitlines() if line.strip() and not line.lstrip().startswith("#")), None
    )
    if first_after is not None and TABLE.fullmatch(first_after) is None:
        raise UnsafeRulesState("managed GitHub profile block was extended without a new TOML table")
    remaining = before + after
    legacy_default = state["schema"] == 1
    if remaining.count(ROOT_LINE) != int(legacy_default) or remaining.count(NETWORK_LINE) != 1:
        raise UnsafeRulesState("managed GitHub profile settings were modified")
    original = state.get("original")
    if original is not None and not isinstance(original, str):
        raise UnsafeRulesState("managed GitHub profile state has an invalid original")
    if hashlib.sha256(content.encode()).hexdigest() == state.get("installed_sha256"):
        return original
    lines = remaining.splitlines(keepends=True)
    if legacy_default:
        lines.remove(ROOT_LINE)
    lines.remove(NETWORK_LINE)
    original_features = original is not None and any(
        (match := TABLE.fullmatch(line.rstrip("\r\n")))
        and match.group(1).strip() in {"features", '"features"', "'features'"}
        for line in original.splitlines(keepends=True)
    )
    if not original_features:
        features = next((i for i, line in enumerate(lines) if line.strip() == "[features]"), None)
        if features is not None:
            next_table = next(
                (i for i in range(features + 1, len(lines)) if TABLE.fullmatch(lines[i].rstrip("\r\n"))),
                len(lines),
            )
            if not "".join(lines[features + 1 : next_table]).strip():
                del lines[features:next_table]
                if features and not lines[features - 1].strip():
                    del lines[features - 1]
    kept, added_settings, _original_lines = _split_config("".join(lines))
    settings = state["settings"]
    if not isinstance(settings, dict) or any(
        not isinstance(settings.get(key), list) or any(not isinstance(item, str) for item in settings[key])
        for key in ("default_permissions", "sandbox_mode", "network_proxy")
    ):
        raise UnsafeRulesState("managed GitHub profile state has invalid settings")
    if added_settings["network_proxy"] or (legacy_default and added_settings["default_permissions"]):
        raise UnsafeRulesState("unowned profile-related config assignment appeared after setup")
    if added_settings["default_permissions"]:
        if tomllib is None:
            raise UnsafeRulesState("TOML validation requires tomli on Python 3.10")
        selected = tomllib.loads("".join(added_settings["default_permissions"]))["default_permissions"]
        if selected == "github-read":
            raise UnsafeRulesState("the managed GitHub profile is selected as default")
    restored_settings = dict(settings)
    if legacy_default:
        kept = _restore_legacy_root_layout(kept, original, settings)
        restored_settings["default_permissions"] = []
        restored_settings["sandbox_mode"] = []
    else:
        restored_settings["default_permissions"] = added_settings["default_permissions"]
        restored_settings["sandbox_mode"] = added_settings["sandbox_mode"]
    restored = _insert_profile_settings(kept, restored_settings, install=False)
    return restored if original is not None or restored.strip() else None


def _interrupted_legacy_migration(content: str, state: dict[str, object]) -> bool:
    """Recognize only the exact old profile left after a schema-2 state write."""
    if state["schema"] != 2 or ROOT_LINE not in content:
        return False
    original = state.get("original")
    if original is not None and not isinstance(original, str):
        raise UnsafeRulesState("managed GitHub profile state has an invalid original")
    kept, settings, _lines = _split_config(original or "")
    if state.get("settings") != settings:
        raise UnsafeRulesState("managed GitHub profile state does not match original settings")
    installed = _insert_profile_settings(kept.copy(), settings, install=True)
    older_installed = _insert_profile_settings(kept.copy(), settings, install=True, preserve_root=False)
    if state.get("installed_sha256") not in {
        hashlib.sha256(installed.encode()).hexdigest(),
        hashlib.sha256(older_installed.encode()).hexdigest(),
    }:
        raise UnsafeRulesState("managed GitHub profile state does not match installed profile")
    source_hash = state.get("migration_source_sha256")
    if source_hash is not None:
        if not isinstance(source_hash, str) or re.fullmatch(r"[0-9a-f]{64}", source_hash) is None:
            raise UnsafeRulesState("managed GitHub profile state has an invalid migration source")
        return hashlib.sha256(content.encode()).hexdigest() == source_hash
    legacy = _insert_profile_settings(kept, settings, install=True, select_default=True)
    return content == legacy


def sync_github_read_profile(home: Path, plugin_root: Path | None = None) -> Iterator[tuple[str, Path, Path | None]]:
    """Install or clear the opt-in profile and retire only validated legacy approvals."""
    home = Path(os.path.abspath(home))
    if home == Path(home.anchor):
        raise UnsafeRulesState("a filesystem root cannot be used as Codex home")
    _require_directory(home)
    if plugin_root is not None:
        _installed_reader(Path(os.path.abspath(plugin_root)), home)

    config = home / "config.toml"
    state_path = home / PROFILE_STATE
    existing_config = _read_regular(config)
    existing_state = _read_regular(state_path)
    current = existing_config.decode("utf-8") if existing_config is not None else None
    state = _read_state(existing_state) if existing_state is not None else None
    if state is None and current is not None and PROFILE_MARKER in current:
        raise UnsafeRulesState("GitHub profile marker exists without owned state")
    interrupted_migration = state is not None and current is not None and _interrupted_legacy_migration(current, state)

    if plugin_root is not None:
        if state is None:
            kept, originals, _lines = _split_config(current or "")
            desired_text = _insert_profile_settings(kept, originals, install=True)
            desired_state = _state_payload(current, desired_text, originals)
        else:
            original = state.get("original")
            if original is not None and not isinstance(original, str):
                raise UnsafeRulesState("managed GitHub profile state has an invalid original")
            kept, originals, _lines = _split_config(original or "")
            if state.get("settings") != originals:
                raise UnsafeRulesState("managed GitHub profile state does not match original settings")
            legacy_default = state["schema"] == 1
            expected_text = _insert_profile_settings(kept, originals, install=True, select_default=legacy_default)
            older_expected_text = _insert_profile_settings(
                kept, originals, install=True, select_default=legacy_default, preserve_root=False
            )
            if state.get("installed_sha256") == hashlib.sha256(older_expected_text.encode()).hexdigest():
                expected_text = older_expected_text
            elif hashlib.sha256(expected_text.encode()).hexdigest() != state.get("installed_sha256"):
                raise UnsafeRulesState("managed GitHub profile state does not match installed profile")
            # A first install can leave only state behind; accept exactly its canonical empty-origin record.
            state_only_retry = (
                current is None
                and not legacy_default
                and original is None
                and existing_state == _state_payload(None, expected_text, originals)
            )
            if current is None and not state_only_retry:
                raise UnsafeRulesState("managed GitHub profile config disappeared")
            restored = (
                original if interrupted_migration or current == original else _remove_profile_settings(current, state)
            )
            if legacy_default:
                kept, originals, _lines = _split_config(restored or "")
                desired_text = _insert_profile_settings(kept, originals, install=True)
                desired_state = _state_payload(restored, desired_text, originals, migration_source=current)
            else:
                desired_text = expected_text if interrupted_migration or current == original else current
                desired_state = existing_state
        if desired_state is not None and len(desired_state) > MAX_BYTES:
            raise UnsafeRulesState("managed GitHub profile state would exceed the readable size limit")
        desired_config = desired_text.encode("utf-8")
        if len(desired_config) > MAX_BYTES:
            raise UnsafeRulesState("managed GitHub profile config would exceed the readable size limit")
    else:
        desired_state = None
        if state is None:
            desired_config = existing_config
        elif current == state.get("original"):
            desired_config = existing_config
        else:
            if current is None:
                raise UnsafeRulesState("managed GitHub profile config disappeared")
            restored = state.get("original") if interrupted_migration else _remove_profile_settings(current, state)
            desired_config = None if restored is None else restored.encode("utf-8")

    _validate_config_transition(existing_config, desired_config, installing=plugin_root is not None)

    managed = home / "rules" / RULE_NAME
    old_reader = _read_regular(managed)
    _check_owned(old_reader, home)
    pr_rules = home / "rules" / PR_RULE_NAME
    old_pr = _read_regular(pr_rules)
    _approved_prs(old_pr, home)
    default = home / "rules" / "default.rules"
    old_default = _read_regular(default)
    cleaned = None if old_default is None else strip_legacy_rules(old_default, home)

    config_change = (config, desired_config, existing_config)
    state_change = (state_path, desired_state, existing_state)
    changes = [state_change, config_change] if plugin_root is not None else [config_change, state_change]
    if old_reader is not None:
        changes.append((managed, None, old_reader))
    if old_pr is not None:
        changes.append((pr_rules, None, old_pr))
    if cleaned != old_default:
        changes.append((default, cleaned, old_default))
    backups = [_backup_change(target, payload, original, home) for target, payload, original in changes]
    for (target, payload, original), backup in zip(changes, backups):
        action = _apply_change(target, payload, original)
        yield action, target, backup


def main(argv: list[str] | None = None) -> int:
    """Run the explicit profile lifecycle and report each resulting local file action."""
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--plugin-root", type=Path, help="verified installed Codex Rig cache root")
    action.add_argument("--remove", action="store_true", help="remove owned profile and legacy approvals")
    parser.add_argument("--codex-home", type=Path, required=True, help="Codex home receiving managed profile")
    args = parser.parse_args(argv)
    completed_changes = 0
    try:
        for status, target, backup in sync_github_read_profile(args.codex_home, args.plugin_root):
            print(f"  [ok] GitHub profile {status}: {target}", flush=True)
            if backup is not None:
                print(f"    backup: {backup}", flush=True)
            completed_changes += status not in {"absent", "already current"}
    except (OSError, ValueError) as error:
        print(f"github-read-profile-error: {error}", file=sys.stderr)
        if completed_changes:
            print(
                f"github-read-profile-partial: {completed_changes} completed update(s); no rollback performed",
                file=sys.stderr,
            )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

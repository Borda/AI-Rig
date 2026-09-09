#!/usr/bin/env python3
"""Install, refresh, or remove managed Codex plugins through native platform commands.

## Purpose

Provide portable local Codex plugin synchronization while preserving explicit action selection and bounded command
output. It refreshes the canonical Git marketplace registration and clean-installs the managed plugin set so local Codex
state can be restored predictably. Setup also projects reusable GitHub-reader approval from the newly installed version;
teardown removes that approval before removing the plugin providing its lifecycle helper.

## Scope

Manages the configured local plugin set only; it neither edits GitHub state nor substitutes for a marketplace
publication workflow. The script may invoke native local Codex commands, but it does not decide release contents or
alter remote repositories. Explicit install includes the reader's existing GitHub reads, local PR checkout, and output
writes through a dedicated managed user rule; it never grants arbitrary Python or direct GitHub CLI execution.

## Usage

Invoke from the ``sync`` skill with one selected action, or call ``sync_codex`` in its focused tests. The default action
removes and reinstalls the managed set after marketplace refresh; ``--no-clean`` retains installed plugins but never
suppresses marketplace refresh. The clear action removes local managed state according to the command contract.

## Used by

Codex Rig's sync workflow and cross-platform synchronization tests call this synchronizer. The workflow consumes the
structured result to report marketplace/ref state and to distinguish optional cleanup from required command failures.

## Outputs

Writes bounded human-readable status describing the resolved marketplace/package state and local Codex command results.
The output retains command outcomes and configured references so callers can explain what changed without exposing
unbounded subprocess output.

## Failure

Malformed registry data, unavailable executable, unsafe Windows command shape, or failed required subprocess returns a
typed sync failure. Required failures stop the action and are not converted into partial success, while explicitly
optional cleanup may be represented as a bounded non-success result.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from enum import Enum
from pathlib import Path
from typing import Any, TextIO

from _package_identity import PackageIdentityError, verify_package


class SyncAction(str, Enum):
    """The two restore verbs this script accepts.

    Subclasses ``str`` (not ``enum.StrEnum``) because ``requires-python`` is ``>=3.10``.
    """

    INSTALL = "install"
    CLEAR = "clear"


MARKETPLACE = "borda-ai-rig"
MARKETPLACE_SOURCE = "Borda/AI-Rig"
MANAGED_PLUGINS = (
    ("Codex Rig", f"codex-rig@{MARKETPLACE}"),
    ("Codemap", f"codemap-py@{MARKETPLACE}"),
    ("Claude Code and Codex Bridge", f"bridge@{MARKETPLACE}"),
)
MAX_JSON_BYTES = 1_048_576
MINIMUM_BRIDGE_PYTHON = (3, 10)
WINDOWS_BATCH_METACHARACTERS = frozenset('&|<>^()%!"')
RunCommand = Callable[..., subprocess.CompletedProcess[str]]


class SyncError(RuntimeError):
    """Report one bounded restore failure without a partial-success claim."""


def _resolve_system_command(command: list[str], *, windows: bool) -> tuple[list[str] | str, bool]:
    """Resolve one command and return its argv plus required shell mode."""
    executable = shutil.which(command[0])
    if executable is None:
        raise FileNotFoundError(command[0])
    resolved = [executable, *command[1:]]
    if windows and Path(executable).suffix.lower() in {".bat", ".cmd"}:
        # Batch files require cmd.exe. Quote the resolved launcher and reject
        # arguments that could become shell syntax before enabling the shell.
        if any(character in '\r\n"%!' for character in executable) or any(
            not argument
            or any(character.isspace() or character in WINDOWS_BATCH_METACHARACTERS for character in argument)
            for argument in command[1:]
        ):
            raise OSError("unsafe Windows batch command")
        command_line = f'"{executable}"'
        if command[1:]:
            command_line = f"{command_line} {' '.join(command[1:])}"
        return command_line, True
    return resolved, False


def _system_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Run one resolved native command, including Windows batch launchers."""
    resolved, shell = _resolve_system_command(command, windows=os.name == "nt")
    return subprocess.run(resolved, shell=shell, **kwargs)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the native Codex restore command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", choices=[a.value for a in SyncAction], default=SyncAction.INSTALL.value)
    parser.add_argument("--codex-ref", help="Git ref to pin; default follows the marketplace default branch")
    parser.add_argument(
        "--no-clean",
        action="store_true",
        help="skip managed-plugin removal before reinstalling",
    )
    parser.add_argument(
        "--no-codex-global-agents",
        action="store_true",
        help="leave CODEX_HOME/AGENTS.md unchanged",
    )
    return parser.parse_args(argv)


def _run(run: RunCommand, command: list[str], *, required: bool = True) -> subprocess.CompletedProcess[str]:
    """Run one argv-only child command and normalize execution failures."""
    try:
        result = run(
            command,
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )
    except OSError as error:
        if not required:
            return subprocess.CompletedProcess(command, 127, "", str(error))
        raise SyncError(f"command unavailable: {command[0]}: {error}") from error
    if required and result.returncode != 0:
        detail = (result.stderr or result.stdout or "command failed").strip()[:512]
        raise SyncError(f"command failed ({result.returncode}): {' '.join(command)}: {detail}")
    return result


def _run_github_rule_installer(run: RunCommand, command: list[str], stdout: TextIO) -> subprocess.CompletedProcess[str]:
    """Run the local rule helper without hiding its completed-status output."""
    result = _run(run, command, required=False)
    if result.stdout:
        print(result.stdout.rstrip(), file=stdout)
    if result.returncode != 0:
        detail = (result.stderr or "command failed").strip()[:512]
        raise SyncError(f"GitHub-rule installer failed ({result.returncode}): {detail}")
    return result


def _validate_selected_payload(root: Path) -> Path:
    """Verify the selected Codex Rig package and required rule helper before cleanup."""
    plugin_root = root / "plugins" / "codex-rig"
    try:
        verify_package(plugin_root)
    except (OSError, PackageIdentityError) as error:
        raise SyncError(f"selected Codex Rig package failed verification: {error}") from error
    rules_installer = plugin_root / "scripts" / "install_github_read_rules.py"
    if rules_installer.is_symlink() or not rules_installer.is_file():
        raise SyncError("installed GitHub-rule installer is incomplete or linked")
    return rules_installer


def _json_output(result: subprocess.CompletedProcess[str], label: str) -> dict[str, object]:
    """Decode one bounded command JSON object."""
    encoded = result.stdout.encode("utf-8")
    if len(encoded) > MAX_JSON_BYTES:
        raise SyncError(f"{label} output is oversized")
    try:
        payload = json.loads(result.stdout)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise SyncError(f"{label} returned invalid JSON") from error
    if not isinstance(payload, dict):
        raise SyncError(f"{label} returned a non-object")
    return payload


def _marketplace_state(run: RunCommand) -> tuple[Path | None, str | None]:
    """Return the configured marketplace root and source type, if present."""
    result = _run(run, ["codex", "plugin", "marketplace", "list", "--json"])
    payload = _json_output(result, "marketplace list")
    entries = payload.get("marketplaces")
    if not isinstance(entries, list):
        raise SyncError("marketplace list is missing entries")
    matches = [item for item in entries if isinstance(item, dict) and item.get("name") == MARKETPLACE]
    if len(matches) > 1:
        raise SyncError(f"multiple configured marketplaces named {MARKETPLACE}")
    if not matches:
        return None, None
    raw_root = matches[0].get("root")
    if not isinstance(raw_root, str) or not raw_root or "\x00" in raw_root:
        raise SyncError("configured marketplace root is invalid")
    try:
        root = Path(raw_root).resolve(strict=True)
    except OSError as error:
        raise SyncError(f"configured marketplace root is unavailable: {error}") from error
    if not root.is_dir():
        raise SyncError("configured marketplace root is not a directory")
    source = matches[0].get("marketplaceSource")
    source_type = source.get("sourceType") if isinstance(source, Mapping) else None
    return root, source_type if isinstance(source_type, str) else None


def _configured_ref(root: Path) -> str | None:
    """Read the bounded marketplace ref identity when metadata exists."""
    metadata = root / ".codex-marketplace-install.json"
    if not metadata.exists():
        return None
    if metadata.is_symlink() or not metadata.is_file() or metadata.stat().st_size > MAX_JSON_BYTES:
        raise SyncError("existing marketplace ref metadata is unsafe")
    try:
        payload = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SyncError("existing marketplace ref metadata is invalid") from error
    value = payload.get("ref_name") if isinstance(payload, dict) else None
    if value is None:
        return ""
    if not isinstance(value, str) or "\x00" in value or "\n" in value:
        raise SyncError("existing marketplace ref metadata is invalid")
    return value


def _installed_versions(run: RunCommand) -> dict[str, str]:
    """Return the enabled version of every managed Codex plugin."""
    result = _run(run, ["codex", "plugin", "list", "--marketplace", MARKETPLACE, "--json"])
    payload = _json_output(result, "plugin list")
    installed = payload.get("installed")
    if not isinstance(installed, list):
        raise SyncError("plugin list is missing installed entries")
    versions: dict[str, str] = {}
    for display_name, plugin_id in MANAGED_PLUGINS:
        matches = [
            item
            for item in installed
            if isinstance(item, dict)
            and item.get("pluginId") == plugin_id
            and item.get("enabled") is True
            and isinstance(item.get("version"), str)
            and item["version"]
        ]
        if len(matches) != 1:
            raise SyncError(f"{display_name} is not uniquely enabled after installation")
        versions[plugin_id] = str(matches[0]["version"])
    return versions


def _codex_home(environ: Mapping[str, str]) -> Path:
    """Resolve the configured Codex home without shell parameter expansion."""
    configured = environ.get("CODEX_HOME")
    return Path(configured) if configured else Path.home() / ".codex"


def _run_bridge_static_diagnosis(root: Path, run: RunCommand, stdout: TextIO) -> None:
    """Check the installed Bridge runtime without starting either provider."""
    doctor = root / "plugins" / "bridge_cc-codex" / "bin" / "bridge_diagnose.py"
    if doctor.is_symlink() or not doctor.is_file():
        raise SyncError("installed Bridge static doctor is incomplete or linked")

    version_result = _run(run, ["python", "--version"], required=False)
    version_text = f"{version_result.stdout}\n{version_result.stderr}".strip()
    match = re.search(r"\bPython\s+(\d+)\.(\d+)(?:\.\d+)?\b", version_text)
    if version_result.returncode != 0 or match is None:
        raise SyncError("Bridge requires a `python` command reporting Python 3.10 or newer")
    if (int(match.group(1)), int(match.group(2))) < MINIMUM_BRIDGE_PYTHON:
        raise SyncError(f"Bridge requires Python 3.10 or newer; found {match.group(0)}")

    diagnosis = _run(run, ["python", str(doctor), "--direction", "claude"], required=False)
    payload = _json_output(diagnosis, "Bridge static diagnosis")
    if diagnosis.returncode != 0 or payload.get("ok") is not True or payload.get("live") is not False:
        detail = json.dumps(payload, sort_keys=True)[:512]
        raise SyncError(f"Bridge static diagnosis failed: {detail}")
    print("  [ok] Bridge static diagnosis passed; no provider call made", file=stdout)


def _remove_managed_plugins(run: RunCommand, stdout: TextIO) -> None:
    """Remove managed plugins while tolerating entries that are already absent."""
    for _display_name, plugin_id in MANAGED_PLUGINS:
        removed = _run(run, ["codex", "plugin", "remove", plugin_id], required=False)
        state = "removed" if removed.returncode == 0 else "not installed"
        print(f"  [ok] {plugin_id}: {state}", file=stdout)


def _clear(run: RunCommand, environ: Mapping[str, str], stdout: TextIO) -> int:
    """Remove owned permissions and instructions before deleting their installed helpers."""
    for filename in ("install_github_read_rules.py", "install_global_agents.py"):
        installer = Path(__file__).resolve().with_name(filename)
        command = [sys.executable, str(installer), "--remove", "--codex-home", str(_codex_home(environ))]
        if filename == "install_github_read_rules.py":
            _run_github_rule_installer(run, command, stdout)
            continue
        result = _run(run, command)
        if result.stdout:
            print(result.stdout.rstrip(), file=stdout)
    _remove_managed_plugins(run, stdout)
    return 0


def sync_codex(
    args: argparse.Namespace,
    *,
    run: RunCommand = _system_run,
    environ: Mapping[str, str] = os.environ,
    stdout: TextIO = sys.stdout,
) -> int:
    """Execute one Codex-only restore or teardown with argv-safe subprocesses."""
    if args.action == SyncAction.CLEAR:
        return _clear(run, environ, stdout)

    requested_ref = args.codex_ref or ""
    root, source_type = _marketplace_state(run)
    marketplace_add = ["codex", "plugin", "marketplace", "add", MARKETPLACE_SOURCE]
    if requested_ref:
        marketplace_add.extend(("--ref", requested_ref))

    if root is not None:
        configured_ref = _configured_ref(root)
        if configured_ref is None and requested_ref:
            raise SyncError("existing marketplace ref cannot be verified")
        if configured_ref is not None and configured_ref != requested_ref:
            raise SyncError(
                f"marketplace tracks {configured_ref or 'default branch'}, requested {requested_ref or 'default branch'}"
            )
        if requested_ref:
            _validate_selected_payload(root)

    if root is not None:
        if source_type == "git":
            _run(run, ["codex", "plugin", "marketplace", "upgrade", MARKETPLACE])
            print("  [ok] marketplace refreshed", file=stdout)
        else:
            _run(run, ["codex", "plugin", "marketplace", "remove", MARKETPLACE])
            _run(run, marketplace_add)
            print("  [ok] marketplace re-registered from Git source", file=stdout)
    else:
        _run(run, marketplace_add)
        print("  [ok] marketplace registered", file=stdout)

    root, _source_type = _marketplace_state(run)
    if root is None:
        raise SyncError("marketplace root is unavailable after refresh")
    rules_installer = _validate_selected_payload(root)
    revision = _run(run, ["git", "-C", str(root), "rev-parse", "HEAD"], required=False)
    revision_text = revision.stdout.strip()
    if revision.returncode == 0 and revision_text:
        print(f"  [ok] marketplace source: {requested_ref or 'default branch'} @ {revision_text[:12]}", file=stdout)
    else:
        print(f"  [warn] marketplace source: {requested_ref or 'default branch'}; revision unavailable", file=stdout)

    if not args.no_clean:
        _remove_managed_plugins(run, stdout)

    for _display_name, plugin_id in MANAGED_PLUGINS:
        _run(run, ["codex", "plugin", "add", plugin_id])
    versions = _installed_versions(run)
    for display_name, plugin_id in MANAGED_PLUGINS:
        print(f"  [ok] {display_name} {versions[plugin_id]} installed", file=stdout)

    _run_bridge_static_diagnosis(root, run, stdout)

    version = versions[f"codex-rig@{MARKETPLACE}"]
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?", version) is None:
        raise SyncError("installed Codex Rig version is invalid")
    home = _codex_home(environ)
    installed_root = home / "plugins" / "cache" / MARKETPLACE / "codex-rig" / version
    _run_github_rule_installer(
        run,
        [sys.executable, str(rules_installer), "--plugin-root", str(installed_root), "--codex-home", str(home)],
        stdout,
    )

    if args.no_codex_global_agents:
        print("  [skip] global instructions unchanged", file=stdout)
    else:
        plugin_root = root / "plugins" / "codex-rig"
        installer = plugin_root / "scripts" / "install_global_agents.py"
        template = plugin_root / "assets" / "AGENTS.md"
        if installer.is_symlink() or template.is_symlink() or not installer.is_file() or not template.is_file():
            raise SyncError("installed global-instruction payload is incomplete or linked")
        result = _run(
            run,
            [
                sys.executable,
                str(installer),
                "--source",
                str(template),
                "--codex-home",
                str(_codex_home(environ)),
            ],
        )
        if result.stdout:
            print(result.stdout.rstrip(), file=stdout)
    print(
        "  Start a fresh Codex session. Legacy files copied by older sync versions are not deleted automatically.",
        file=stdout,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the command and emit one concise error on failure."""
    try:
        return sync_codex(parse_args(argv))
    except SyncError as error:
        print(f"codex-sync-error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

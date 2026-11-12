#!/usr/bin/env python
"""Perform the JSON operations used by foundry setup without external executables.

Purpose:
    Read, probe and atomically update the specific settings keys owned by setup.
Scope:
    Selective foundry hook removal, statusLine, enabledPlugins, env defaults and advisorModel only;
    this is not an expression evaluator. Writes retain unrelated settings, mode
    bits and symlinks, using the same write roots as the former setup writer.
Usage:
    settings_json.py COMMAND SETTINGS [--root ROOT | --command CMD | --name NAME |
    --defaults FILE | --model MODEL]. See --help for the explicit subcommands.
Outputs:
    Probes exit 0 when matched, 1 otherwise. advisor-model prints the project
    value; merge-env prints the added/preserved counts. filter-foundry-hooks
    requires --root and --dry-run or --apply, and reports foundry/user or
    removed/kept counts. statusline-owned --root proves a shipped hook path in
    the current root, its installed-cache lineage or the legacy hooks folder.
    Other writes are silent; the obsolete delete-hooks operation is not accepted.
Failure:
    Invalid input, refused paths and write errors exit 2 with a diagnostic and
    leave settings unchanged. A private sibling temporary file is removed after
    failures; replacement happens only after serialization and close succeed.
Used by:
    /foundry:setup Steps 3–8, through Bash without Write-tool prompts.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import stat
import sys
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

from merge_settings_permissions import _settings_lock

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def _path(raw: str) -> Path:
    """Expand HOME consistently with setup's Bash, including on native Windows."""
    path = Path(raw)
    if path.parts and path.parts[0] == "~":
        return Path(os.environ.get("HOME") or Path.home()).joinpath(*path.parts[1:])
    return path.expanduser()


def _validate_path(path: Path, *, source: bool = False) -> None:
    """Preserve the former writer's cwd, temporary and user-settings boundaries."""
    if source:
        if not path.resolve().is_relative_to(_PLUGIN_ROOT.resolve()):
            raise ValueError(f"defaults must be inside the plugin root: {path}")
        return
    home = Path(os.environ.get("HOME") or Path.home())
    claude = Path(os.path.abspath(home / ".claude"))
    lexical = Path(os.path.abspath(path))
    if not source and lexical.parent == claude and lexical.suffix == ".json":
        return
    tmp_fallback = tempfile.gettempdir() if os.name == "nt" else "/tmp"  # noqa: S108 - shell default of ${TMPDIR:-/tmp}
    # Both temp spellings stay roots: the shell's ${TMPDIR:-/tmp} and the platform dir that TEMP/TMP may name.
    roots = [
        Path.cwd().resolve(),
        Path(os.environ.get("TMPDIR") or tmp_fallback).resolve(),
        Path(os.environ.get("TMPDIR") or tempfile.gettempdir()).resolve(),
    ]
    resolved = path.resolve()
    if not any(resolved == root or root in resolved.parents for root in roots):
        raise ValueError(f"path outside allowed {'read' if source else 'write'} roots: {path}")


def _reject_constant(value: str) -> None:
    """Reject non-JSON numeric constants accepted by Python's default decoder."""
    raise ValueError(f"invalid JSON constant: {value}")


def _load(path: Path) -> dict[str, Any] | None:
    """Read an object or null, rejecting invalid JSON and incompatible shapes."""
    value = json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_constant)
    if value is not None and not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _atomic_write(target: Path, settings: dict[str, Any]) -> None:
    """Replace a complete settings document through its symlink, retaining file mode."""
    destination = target.resolve()
    mode = stat.S_IMODE(destination.stat().st_mode)
    text = json.dumps(settings, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    temporary: Path | None = None
    try:
        # Close before replace for Windows; a private sibling avoids collisions and cross-device renames.
        with tempfile.NamedTemporaryFile(
            dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(text.encode("utf-8"))
        os.chmod(temporary, mode)
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


def _object(settings: dict[str, Any], key: str) -> dict[str, Any]:
    """Create a missing or null nested object, refusing to discard incompatible values."""
    value = settings.get(key)
    if value is None:
        value = settings[key] = {}
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value


def _foundry_root(raw: str) -> Path:
    """Validate the supplied foundry installation before using it as an ownership boundary."""
    if not raw.strip():
        raise ValueError("foundry root must not be empty")
    root = _path(raw).resolve()
    marker = _load(root / ".claude-plugin" / "plugin.json")
    if not marker or marker.get("name") != "foundry" or not (root / "hooks").is_dir():
        raise ValueError(f"not a foundry plugin root: {root}")
    return root


def _owned_hook(command: Any, root: Path, names: set[str]) -> bool:
    """Recognize a standalone executed shipped hook, preserving compound and custom commands."""
    if not isinstance(command, str):
        return False
    try:
        tokens = shlex.split(command.replace("\\", "/"))
    except ValueError:
        return False
    if tokens[:1] == ["exec"]:
        tokens = tokens[1:]
    if len(tokens) != 2 or tokens[0].rsplit("/", 1)[-1] not in {"node", "node.exe", "python", "python3"}:
        return False
    raw = tokens[1]
    raw = raw.replace("${CLAUDE_PLUGIN_ROOT}/", root.as_posix() + "/")
    raw = raw.replace("$CLAUDE_PLUGIN_ROOT/", root.as_posix() + "/")
    script = _path(os.path.expandvars(raw))
    if not script.is_absolute() or script.name not in names:
        return False
    script = Path(os.path.abspath(script))
    home = Path(os.environ.get("HOME") or Path.home()).resolve()
    owned = {root / "hooks", home / ".claude" / "hooks"}
    cache = home / ".claude" / "plugins" / "cache"
    lineage = root.parent
    if lineage.name == "foundry" and lineage.parent.parent == cache:
        owned.add(lineage / script.parent.parent.name / "hooks")
    return script.parent in owned


def _shipped_commands(root: Path) -> set[str]:
    """Read exact trusted launcher commands so shell wrappers need no permissive shell matching."""
    manifest = _load(root / "hooks" / "hooks.json") or {}
    events = manifest.get("hooks")
    if not isinstance(events, dict):
        raise ValueError("foundry hooks manifest must contain a hooks object")
    commands = set()
    for groups in events.values():
        if not isinstance(groups, list):
            raise ValueError("foundry hook events must contain group arrays")
        for group in groups:
            hooks = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(hooks, list):
                raise ValueError("foundry hook groups must contain hook arrays")
            commands.update(
                hook["command"] for hook in hooks if isinstance(hook, dict) and isinstance(hook.get("command"), str)
            )
    return commands


def _filter_group(group: Any, root: Path, names: set[str], commands: set[str]) -> tuple[Any, int, int]:
    """Remove owned entries from one group while preserving unknown values and matcher metadata."""
    if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
        return group, 0, 1
    hooks = group["hooks"]
    kept = []
    for hook in hooks:
        command = hook.get("command") if isinstance(hook, dict) else None
        shipped = isinstance(command, str) and command in commands
        if not shipped and not _owned_hook(command, root, names):
            kept.append(hook)
    removed = len(hooks) - len(kept)
    return ({**group, "hooks": kept} if kept or not hooks else None), removed, len(kept)


def _filter_hooks(settings: dict[str, Any], root: Path) -> tuple[int, int]:
    """Filter only proven foundry hooks in memory and count preserved unrecognized settings shapes."""
    events = settings.get("hooks", {})
    if not isinstance(events, dict):
        return 0, 1
    names = {script.name for script in (root / "hooks").glob("*.js")}
    commands = _shipped_commands(root)
    removed = user = 0
    for event, groups in list(events.items()):
        if not isinstance(groups, list):
            user += 1
            continue
        kept = []
        for group in groups:
            filtered, foundry_count, user_count = _filter_group(group, root, names, commands)
            removed += foundry_count
            user += user_count
            if filtered is not None or not foundry_count:
                kept.append(filtered)
        if kept:
            events[event] = kept
        elif groups:
            del events[event]
    if removed and not events:
        settings.pop("hooks", None)
    return removed, user


def _run_hook_filter(target: Path, args: argparse.Namespace) -> str:
    """Classify or atomically filter hooks while sharing the lexical settings lock with all setup writers."""
    root = _foundry_root(args.root)
    _validate_path(target)
    with _settings_lock(target):
        settings = _load(target) or {}
        if "hooks" in settings and not isinstance(settings["hooks"], dict):
            print("hooks: skipped (expected an object; kept user-owned value)")
        removed, user = _filter_hooks(settings, root)
        if args.apply and removed:
            _atomic_write(target, settings)
    return f"removed: {removed}, kept: {user}" if args.apply else f"foundry: {removed}, user: {user}"


def _probe(operation: str, settings: dict[str, Any], args: argparse.Namespace) -> int:
    """Answer one read-only probe: exit status 0 when it matches, 1 otherwise; ``advisor-model`` prints and returns
    0."""
    if operation == "statusline-current":
        status = settings.get("statusLine")
        command = status.get("command") if isinstance(status, dict) else None
        return 0 if isinstance(command, str) and "statusline.js" in command and args.root in command else 1
    if operation == "statusline-owned":
        root = _foundry_root(args.root)
        status = settings.get("statusLine")
        command = status.get("command") if isinstance(status, dict) else None
        names = {"statusline.js"} if (root / "hooks" / "statusline.js").is_file() else set()
        return 0 if _owned_hook(command, root, names) else 1
    if operation == "plugin-enabled":
        plugins = settings.get("enabledPlugins")
        return 0 if isinstance(plugins, dict) and plugins.get(args.name) is True else 1
    if operation == "advisor-model":
        model = settings.get("advisorModel")
        if model is not None and model is not False:
            print(model if isinstance(model, str) else json.dumps(model, indent=2, ensure_ascii=False))
        return 0
    return 0 if settings.get("advisorModel") == args.model else 1


def _apply(operation: str, settings: dict[str, Any], args: argparse.Namespace) -> str:
    """Apply one write operation to ``settings`` in memory and return its report line (empty when none)."""
    if operation == "set-statusline":
        settings["statusLine"] = {"async": True, "command": args.command, "type": "command"}
    elif operation == "enable-plugin":
        _object(settings, "enabledPlugins")[args.name] = True
    elif operation == "set-advisor":
        if (
            re.fullmatch(
                r"(?:best|fable|opusplan|(?:opus|sonnet)(?:\[1m\])?|haiku"
                r"|claude-(?:fable|opus|sonnet|haiku)-[0-9]{1,2}(?:-[0-9]{1,8}){0,2}"
                r"|claude-[0-9]{1,2}(?:-[0-9]{1,2})?-(?:opus|sonnet|haiku)(?:-[0-9]{8})?)",
                args.model,
            )
            is None
        ):
            raise ValueError("advisor model must be a supported Claude alias or family model ID")
        settings["advisorModel"] = args.model
    elif operation == "merge-env":
        source = _path(args.defaults)
        _validate_path(source, source=True)
        defaults = _load(source)
        if defaults is None:
            raise ValueError("env defaults must be an object")
        # Existing key presence is decisive: even deliberate "0", false or null stays intact.
        if settings.get("env") is False:
            settings["env"] = {}
        env = _object(settings, "env")
        added = sum(key not in env for key in defaults)
        for key, value in defaults.items():
            env.setdefault(key, value)
        return f"  env: {added} default(s) added ({len(defaults) - added} already set)"
    return ""


_PROBES = frozenset({"statusline-current", "statusline-owned", "plugin-enabled", "advisor-model", "advisor-matches"})


def main(argv: list[str] | None = None) -> int:
    """Execute one bounded setup operation and return its process status."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="operation", required=True)
    parsers = {}
    for name in (
        "has-hooks",
        "filter-foundry-hooks",
        "statusline-current",
        "statusline-owned",
        "set-statusline",
        "plugin-enabled",
        "enable-plugin",
        "merge-env",
        "advisor-model",
        "advisor-matches",
        "set-advisor",
    ):
        command = commands.add_parser(name)
        command.add_argument("settings")
        parsers[name] = command
    parsers["statusline-current"].add_argument("--root", required=True)
    parsers["statusline-owned"].add_argument("--root", required=True)
    parsers["filter-foundry-hooks"].add_argument("--root", required=True)
    filter_mode = parsers["filter-foundry-hooks"].add_mutually_exclusive_group(required=True)
    filter_mode.add_argument("--dry-run", action="store_true")
    filter_mode.add_argument("--apply", action="store_true")
    parsers["set-statusline"].add_argument("--command", required=True)
    parsers["plugin-enabled"].add_argument("--name", required=True)
    parsers["enable-plugin"].add_argument("--name", required=True)
    parsers["merge-env"].add_argument("--defaults", required=True)
    parsers["advisor-matches"].add_argument("--model", required=True)
    parsers["set-advisor"].add_argument("--model", required=True)
    args = parser.parse_args(argv)
    try:
        target = _path(args.settings)
        operation = args.operation
        if operation == "filter-foundry-hooks":
            print(_run_hook_filter(target, args))
            return 0
        if operation == "has-hooks" or operation in _PROBES:
            settings = _load(target)
            if operation == "has-hooks":
                return 0 if settings is not None and "hooks" in settings else 1
            return _probe(operation, settings or {}, args)
        _validate_path(target)
        with _settings_lock(target):
            settings = _load(target) or {}
            report = _apply(operation, settings, args)
            _atomic_write(target, settings)
        if report:
            print(report)
        return 0
    except (OSError, ValueError, UnicodeError) as exc:
        print(f"! settings_json: {exc} — settings.json unchanged", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

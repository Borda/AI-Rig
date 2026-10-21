#!/usr/bin/env python
"""Perform the JSON operations used by foundry setup without external executables.

Purpose:
    Read, probe and atomically update the specific settings keys owned by setup.
Scope:
    Hooks removal, statusLine, enabledPlugins, env defaults and advisorModel only;
    this is not an expression evaluator. Writes retain unrelated settings, mode
    bits and symlinks, using the same write roots as the former setup writer.
Usage:
    settings_json.py COMMAND SETTINGS [--root ROOT | --command CMD | --name NAME |
    --defaults FILE | --model MODEL]. See --help for the explicit subcommands.
Outputs:
    Probes exit 0 when matched, 1 otherwise. advisor-model prints the project
    value; merge-env prints the added/preserved counts. Other writes are silent.
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
import stat
import sys
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any


def _path(raw: str) -> Path:
    """Expand HOME consistently with setup's Bash, including on native Windows."""
    path = Path(raw)
    if path.parts and path.parts[0] == "~":
        return Path(os.environ.get("HOME") or Path.home()).joinpath(*path.parts[1:])
    return path.expanduser()


def _validate_path(path: Path, *, source: bool = False) -> None:
    """Preserve the former writer's cwd, temporary and user-settings boundaries."""
    home = Path(os.environ.get("HOME") or Path.home())
    claude = Path(os.path.abspath(home / ".claude"))
    lexical = Path(os.path.abspath(path))
    if not source and lexical.parent == claude and lexical.suffix == ".json":
        return
    roots = [Path.cwd().resolve(), Path(os.environ.get("TMPDIR") or tempfile.gettempdir()).resolve()]
    if source:
        roots.append(claude.resolve())
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


def _probe(operation: str, settings: dict[str, Any], args: argparse.Namespace) -> int:
    """Answer one read-only probe: exit status 0 when it matches, 1 otherwise; ``advisor-model`` prints and returns
    0."""
    if operation == "statusline-current":
        status = settings.get("statusLine")
        command = status.get("command") if isinstance(status, dict) else None
        return 0 if isinstance(command, str) and "statusline.js" in command and args.root in command else 1
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
    if operation == "delete-hooks":
        settings.pop("hooks", None)
    elif operation == "set-statusline":
        settings["statusLine"] = {"async": True, "command": args.command, "type": "command"}
    elif operation == "enable-plugin":
        _object(settings, "enabledPlugins")[args.name] = True
    elif operation == "set-advisor":
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


_PROBES = frozenset({"statusline-current", "plugin-enabled", "advisor-model", "advisor-matches"})


def main(argv: list[str] | None = None) -> int:
    """Execute one bounded setup operation and return its process status."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="operation", required=True)
    parsers = {}
    for name in (
        "has-hooks",
        "delete-hooks",
        "statusline-current",
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
    parsers["set-statusline"].add_argument("--command", required=True)
    parsers["plugin-enabled"].add_argument("--name", required=True)
    parsers["enable-plugin"].add_argument("--name", required=True)
    parsers["merge-env"].add_argument("--defaults", required=True)
    parsers["advisor-matches"].add_argument("--model", required=True)
    parsers["set-advisor"].add_argument("--model", required=True)
    args = parser.parse_args(argv)
    try:
        target = _path(args.settings)
        settings = _load(target)
        operation = args.operation
        if operation == "has-hooks":
            return 0 if settings is not None and "hooks" in settings else 1
        settings = settings if settings is not None else {}
        if operation in _PROBES:
            return _probe(operation, settings, args)
        _validate_path(target)
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

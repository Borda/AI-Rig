#!/usr/bin/env python
"""Run a configured research metric or guard with its enforced wall-clock timeout.

Purpose:
    Keep commands read from run state inside one execution boundary instead of expanding
    them into a caller's shell command.
Scope:
    Read config.metric_cmd or config.guard_cmd from state.json; an explicit command
    override supports the current ablation variant and legacy state without config.
    Bash preserves existing pipelines.
Usage:
    run_config_command.py --state-json PATH --kind metric|guard [--command-override TEXT]
Outputs:
    Forward command stdout, stderr and exit status without parsing metric values.
Failure:
    Invalid input or unavailable Bash exits 2. A timeout exits 124. POSIX timeout cleanup
    kills the command process group; on Windows only the direct child can be killed.
Used by:
    research:fortify metric and guard execution in each variant working directory.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

MAX_STATE_BYTES = 64 * 1024 * 1024


def read_config(state_json: Path, *, allow_missing: bool = False) -> dict:
    """Read bounded configuration, admitting absent legacy config only for an explicit command override."""
    with state_json.open("rb") as handle:
        raw = handle.read(MAX_STATE_BYTES + 1)
    if len(raw) > MAX_STATE_BYTES:
        raise ValueError(f"state exceeds {MAX_STATE_BYTES} bytes")
    state = json.loads(raw)
    if isinstance(state, dict) and "config" not in state and allow_missing:
        return {}
    config = state.get("config") if isinstance(state, dict) else None
    if not isinstance(config, dict):
        raise ValueError("state.config must be an object")
    return config


def command_timeout(config: dict, kind: str) -> int:
    """Resolve the shared positive-integer metric limit or the established guard limit."""
    if kind == "guard":
        return 360
    default = 300 if config.get("compute") == "colab" else 120
    value = config.get("metric_timeout_sec", default)
    override = os.environ.get("VERIFY_TIMEOUT_SEC")
    if override is not None:
        if not override.isascii() or not override.isdecimal():
            raise ValueError("VERIFY_TIMEOUT_SEC must be a positive integer")
        value = int(override)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("config.metric_timeout_sec must be a positive integer")
    return value


def execute(command: str, timeout: int) -> int:
    """Run Bash in the inherited working directory and terminate timed-out command groups."""
    bash = shutil.which("bash")
    if bash is None:
        raise OSError("bash not found on PATH")
    process = subprocess.Popen(  # noqa: S603 - execute the reviewed program command through resolved Bash
        [bash, "-c", command], start_new_session=os.name != "nt"
    )
    try:
        status = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            print("run_config_command: cleanup did not confirm termination within 5 seconds", file=sys.stderr)
        print(f"run_config_command: timed out after {timeout} seconds", file=sys.stderr)
        return 124
    return status if status >= 0 else 128 - status


def main(argv: list[str] | None = None) -> int:
    """Validate the selected state command and forward its bounded execution result."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-json", type=Path, required=True)
    parser.add_argument("--kind", choices=("metric", "guard"), required=True)
    parser.add_argument("--command-override")
    args = parser.parse_args(argv)
    try:
        config = read_config(args.state_json, allow_missing=args.command_override is not None)
        command = args.command_override if args.command_override is not None else config.get(f"{args.kind}_cmd")
        if not isinstance(command, str) or not command.strip():
            raise ValueError(f"config.{args.kind}_cmd must be a non-empty command")
        return execute(command, command_timeout(config, args.kind))
    except (OSError, ValueError, ArithmeticError, RecursionError) as exc:
        print(f"run_config_command: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

"""Portable fake ``uv`` and ``pre-commit`` executables for gate-environment provisioning tests.

Each fake logs its arguments and environment to ``calls.jsonl`` beside it. ``uv venv`` builds a real virtual
environment, ``pre-commit`` keeps a copy of the configuration file it was given as ``hook-config-seen.yaml``, and every
other call prints the configured diagnostic and exits with the configured status. The launchers are a POSIX shell script
or a native Windows ``.cmd`` file, so ``shutil.which`` finds them on every supported OS.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

#: Body shared by every fake executable; its file stem names the tool it plays.
_FAKE_TOOL = """\
import json, os, subprocess, sys
from pathlib import Path
here = Path(__file__).resolve().parent
name = Path(__file__).stem
args = sys.argv[1:]
config = json.loads((here / "config.json").read_text(encoding="utf-8"))
with (here / "calls.jsonl").open("a", encoding="utf-8") as log:
    log.write(json.dumps({"tool": name, "argv": args, "env": dict(os.environ)}) + "\\n")
if name == "uv" and args[:1] == ["venv"]:
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", args[-1]], check=True)
    raise SystemExit(0)
if name == "pre-commit":
    (here / "hook-config-seen.yaml").write_bytes(Path(args[args.index("--config") + 1]).read_bytes())
sys.stderr.write(config.get("output", ""))
raise SystemExit(config.get("exit", 0))
"""


def fake_bin(directory: Path, *tools: str, output: str = "", exit_code: int = 0) -> Path:
    """Create fake executables named ``tools`` in ``directory`` and return the directory.

    Args:
        directory: Directory to create and fill; put it first on ``PATH`` to shadow the real tools.
        tools: Executable names, such as ``uv`` and ``pre-commit``.
        output: Diagnostic every call other than ``uv venv`` writes to standard error.
        exit_code: Status every call other than ``uv venv`` exits with.

    Returns:
        ``directory``.
    """
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.json").write_text(json.dumps({"output": output, "exit": exit_code}), encoding="utf-8")
    for name in tools:
        script = directory / f"{name}.py"
        script.write_text(_FAKE_TOOL, encoding="utf-8")
        if os.name == "nt":
            (directory / f"{name}.cmd").write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        else:
            launcher = directory / name
            launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
            launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
    return directory


def tool_calls(directory: Path) -> list[dict]:
    """Return the logged calls of the fake executables in ``directory``, oldest first."""
    log = directory / "calls.jsonl"
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.is_file() else []

"""Ensure the canonical Python bootstrap ignores current-directory PATH entries."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_SHELL = shutil.which("sh")
_LAUNCHER = Path(__file__).resolve().parents[1] / "plugins/cc_foundry/bin/python"


@pytest.mark.skipif(_SHELL is None, reason="requires a POSIX shell")
@pytest.mark.parametrize("entry", ["", ".", "relative"])
def test_launcher_skips_nonabsolute_runtime_directory(tmp_path: Path, entry: str) -> None:
    """A planted Python cannot run even for the version probe through an unsafe PATH entry."""
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    runtime = trusted / "python3"
    runtime.write_text(f'#!/bin/sh\nexec "{Path(sys.executable).as_posix()}" "$@"\n', encoding="utf-8", newline="\n")
    runtime.chmod(0o755)
    planted_dir = tmp_path / (entry or ".")
    planted_dir.mkdir(exist_ok=True)
    marker = tmp_path / "planted-ran"
    planted = planted_dir / "python"
    planted.write_text(f'#!/bin/sh\n: > "{marker.as_posix()}"\nexit 0\n', encoding="utf-8", newline="\n")
    planted.chmod(0o755)

    # A script file, not `-c`: Git-for-Windows `sh` re-parses its command line and strips the single quotes of an inline
    # `print('trusted')`, so the probe would run `print(trusted)` and fail for a reason unrelated to the launcher.
    probe = tmp_path / "probe.py"
    probe.write_text("print('trusted')\n", encoding="utf-8", newline="\n")

    result = subprocess.run(
        [_SHELL, str(_LAUNCHER), probe.as_posix()],
        cwd=tmp_path,
        env={**os.environ, "PATH": os.pathsep.join((entry, str(trusted)))},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert not marker.exists()
    assert (result.returncode, result.stdout, result.stderr) == (0, "trusted\n", "")

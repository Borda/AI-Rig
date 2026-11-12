"""Exercise the canonical POSIX bootstrap on hosts with only the Windows Python launcher."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_SHELL = shutil.which("sh")
_LAUNCHER = Path(__file__).resolve().parents[2] / "bin" / "python"


@pytest.mark.skipif(_SHELL is None, reason="requires a POSIX shell")
@pytest.mark.parametrize("name", ["py", "py.exe"])
def test_py_only_host_preserves_arguments_input_and_failure(tmp_path: Path, name: str) -> None:
    """The final fallback validates py -3 and executes a failing workload exactly once."""
    runtime = tmp_path / name
    runtime.write_text(
        f'#!/bin/sh\n[ "$1" = -3 ] || exit 90\nshift\nexec "{Path(sys.executable).as_posix()}" "$@"\n',
        encoding="utf-8",
    )
    runtime.chmod(0o755)
    marker = tmp_path / "executions"
    workload = (
        "import pathlib, sys; "
        "pathlib.Path(sys.argv[2]).open('a').write('once\\n'); "
        "print(sys.argv[1]); print(sys.stdin.read(), end=''); sys.exit(23)"
    )

    result = subprocess.run(
        [_SHELL, str(_LAUNCHER), "-c", workload, "two words", str(marker)],
        cwd=tmp_path,
        env={**os.environ, "PATH": str(tmp_path)},
        input="payload\n",
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert (result.returncode, result.stdout, result.stderr) == (23, "two words\npayload\n", "")
    assert marker.read_text(encoding="utf-8") == "once\n"

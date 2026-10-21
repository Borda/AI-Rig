"""Research sentinel writers never write through a file or symlink planted at the predictable sentinel name.

The writers persist state in ``${TMPDIR}/<name>-${CSID}``. ``TMPDIR`` may be the machine-wide ``/tmp``, so another local
user can plant a symlink at that name before the skill runs; a plain ``Path.write_text`` would follow it and overwrite
the link target. Each writer now goes through ``_sentinel_io.write_private_text``: the planted entry is removed and a
fresh owner-only file is created in its place.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parents[1] / "bin"
_CSID = "l84sess"


def _symlinks_supported() -> bool:
    """Report whether this host lets the test create a symlink (unprivileged Windows may not)."""
    with tempfile.TemporaryDirectory() as tmp:
        try:
            (Path(tmp) / "probe-link").symlink_to(Path(tmp) / "probe-target")
        except OSError:
            return False
    return True


_skip_no_symlink = pytest.mark.skipif(not _symlinks_supported(), reason="host cannot create symlinks")


@pytest.fixture
def planted(tmp_path: Path) -> tuple[Path, Path]:
    """Temp dir holding a victim file, plus the env-free paths the writers resolve against it."""
    victim = tmp_path / "victim.txt"
    victim.write_text("untouched\n", encoding="utf-8")
    return tmp_path, victim


@_skip_no_symlink
@pytest.mark.parametrize(
    ("script", "args", "sentinel_name", "expected"),
    [
        pytest.param(
            "check_output_within_root.py",
            ["--parse-out", "plan.md", "--sentinel", "research-l84"],
            f"research-l84-{_CSID}",
            "program.md\n",
            id="check-output-within-root",
        ),
        pytest.param(
            "parse_kaggle_args.py",
            ['titanic --keep "x"'],
            f"kaggle-keep-items-{_CSID}",
            "x\n",
            id="parse-kaggle-args",
        ),
    ],
)
def test_writer_replaces_planted_symlink(
    planted: tuple[Path, Path], script: str, args: list[str], sentinel_name: str, expected: str
) -> None:
    """A symlink planted at the sentinel name is replaced by a private regular file; its target stays untouched.

    Before the fix the writer followed the link and overwrote ``victim.txt`` with the sentinel value.
    """
    tmp, victim = planted
    sentinel = tmp / sentinel_name
    sentinel.symlink_to(victim)
    env = {**os.environ, "TMPDIR": str(tmp), "CSID": _CSID}

    proc = subprocess.run(
        [sys.executable, str(_BIN / script), *args], cwd=tmp, env=env, capture_output=True, text=True, timeout=30
    )

    assert proc.returncode == 0, proc.stderr
    assert victim.read_text(encoding="utf-8") == "untouched\n"
    assert not sentinel.is_symlink()
    assert sentinel.read_text(encoding="utf-8") == expected
    assert sys.platform == "win32" or stat.S_IMODE(sentinel.stat().st_mode) == 0o600

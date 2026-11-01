"""Check resolve's deferred-file overlap output and visible read failures."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "bin/compare_deferred_files.py"


def _compare(tmp_path: Path, diff: bytes, deferred: str) -> subprocess.CompletedProcess[str]:
    """Run the script on ``diff``/``deferred`` files written under ``tmp_path`` with ``tmp_path`` as the repo root."""
    diff_file = tmp_path / "diff.txt"
    diff_file.write_bytes(diff)
    pending = tmp_path / "deferred.txt"
    pending.write_text(deferred, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(_SCRIPT), str(tmp_path), str(diff_file), str(pending)],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.integration
@pytest.mark.parametrize(
    ("deferred", "expected"),
    [
        pytest.param(
            "./src/my file.py\nother.py\n./src/my file.py\n",
            f"{Path('src/my file.py')} {Path('src/my file.py')}\n",
            id="ordered-duplicates-and-spaces",
        ),
        pytest.param("other.py\n\n", "\n", id="disjoint-files"),
    ],
)
def test_prints_overlap_in_deferred_order(tmp_path: Path, deferred: str, expected: str) -> None:
    """Deferred files that appear in the diff are printed in deferred order, duplicates and spaces preserved.

    The diff lists an absolute path under the repo root while the deferred list uses relative ``./`` paths, so the
    script must match both forms. A deferred list with no overlap prints a single empty line and exits 0.
    """
    result = _compare(tmp_path, f"{tmp_path / 'src/my file.py'}\n\n".encode(), deferred)
    assert (result.returncode, result.stdout, result.stderr) == (0, expected, "")


@pytest.mark.integration
def test_invalid_utf8_fails(tmp_path: Path) -> None:
    """An undecodable diff file fails with the decode error instead of reporting an empty overlap.

    A filename that is not valid UTF-8 must stay visible to the caller: exiting 0 with empty output would let resolve
    treat every deferred file as safe to touch.
    """
    result = _compare(tmp_path, b"\xff\n", "file.py\n")
    assert result.returncode == 1
    assert result.stdout == ""
    assert "UnicodeDecodeError" in result.stderr

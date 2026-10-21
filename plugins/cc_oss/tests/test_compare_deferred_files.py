"""Check resolve's deferred-file overlap output and visible read failures."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "bin/compare_deferred_files.py"


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
def test_compare_files(tmp_path: Path, deferred: str, expected: str) -> None:
    """Match absolute and relative paths while preserving deferred order and duplicates."""
    diff = tmp_path / "diff.txt"
    diff.write_text(f"{tmp_path / 'src/my file.py'}\n\n", encoding="utf-8")
    pending = tmp_path / "deferred.txt"
    pending.write_text(deferred, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), str(tmp_path), str(diff), str(pending)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, expected, "")


@pytest.mark.integration
def test_invalid_utf8_fails(tmp_path: Path) -> None:
    """Reject unreadable filenames rather than report a successful empty overlap."""
    diff = tmp_path / "diff.txt"
    diff.write_bytes(b"\xff\n")
    pending = tmp_path / "deferred.txt"
    pending.write_text("file.py\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), str(tmp_path), str(diff), str(pending)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert "UnicodeDecodeError" in result.stderr

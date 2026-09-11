"""Tests for check_output_within_root.py."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

BIN = os.path.join(os.path.dirname(__file__), "..", "bin", "check_output_within_root.py")


def test_within_root(tmp_path: Path):
    sub = tmp_path / "sub" / "dir"
    result = subprocess.run([sys.executable, BIN, str(sub), str(tmp_path)])
    assert result.returncode == 0


def test_equal_to_root(tmp_path: Path):
    result = subprocess.run([sys.executable, BIN, str(tmp_path), str(tmp_path)])
    assert result.returncode == 0


@pytest.mark.parametrize("case", ["absolute_tmp", "sibling_prefix", "relative_parent"])
def test_outside_root(case: str, tmp_path: Path):
    if case == "absolute_tmp":
        candidate = "/tmp/evil"
    elif case == "sibling_prefix":
        candidate = f"{tmp_path}-evil"
    else:
        sibling = tmp_path.parent / "sibling"
        candidate = tmp_path / ".." / sibling.name
    result = subprocess.run([sys.executable, BIN, str(candidate), str(tmp_path)])
    assert result.returncode == 1


def test_path_traversal_blocked(tmp_path: Path):
    traversal = tmp_path / ".." / ".." / "etc"
    result = subprocess.run([sys.executable, BIN, str(traversal), str(tmp_path)])
    assert result.returncode == 1


def test_help_exits_zero():
    """Print usage and exit 0 (argparse contract)."""
    result = subprocess.run([sys.executable, BIN, "--help"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "usage" in result.stdout.lower()


def test_golden_invocation_within_root(tmp_path: Path):
    """Protect the documented behavior against regression: the SKILL call shape ``<candidate> <root>`` (2 positional)
    still exits 0 within root."""
    sub = tmp_path / "sub"
    sub.mkdir()
    result = subprocess.run([sys.executable, BIN, str(sub), str(tmp_path)])
    assert result.returncode == 0


def test_one_positional_is_an_argument_error(tmp_path: Path):
    result = subprocess.run([sys.executable, BIN, str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 2


def _parse_out(args: str, tmp_path: Path, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run the ``--parse-out`` mode with an isolated sentinel dir and session token."""
    env = {**os.environ, "TMPDIR": str(tmp_path / "sentinels"), "CSID": "testsess"}
    (tmp_path / "sentinels").mkdir(exist_ok=True)
    return subprocess.run(
        [sys.executable, BIN, f"--parse-out={args}", "--sentinel", "sweep-out-path"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(cwd or tmp_path),
    )


def _sentinel_text(tmp_path: Path) -> str:
    return (tmp_path / "sentinels" / "sweep-out-path-testsess").read_text(encoding="utf-8")


def test_parse_out_persists_relative_path(tmp_path: Path):
    result = _parse_out("goal text --out docs/program.md --team", tmp_path)
    assert result.returncode == 0, result.stderr
    assert _sentinel_text(tmp_path) == "docs/program.md\n"


def test_parse_out_absent_writes_default(tmp_path: Path):
    result = _parse_out("some optimization goal", tmp_path)
    assert result.returncode == 0, result.stderr
    assert _sentinel_text(tmp_path) == "program.md\n"


def test_parse_out_leading_dash_payload(tmp_path: Path):
    """``$ARGUMENTS`` often starts with a flag; the attached ``--parse-out=`` form must absorb it."""
    result = _parse_out("--team --out out.md", tmp_path)
    assert result.returncode == 0, result.stderr
    assert _sentinel_text(tmp_path) == "out.md\n"


@pytest.mark.parametrize("candidate", ["../evil.md", "sub/../../evil.md"])
def test_parse_out_rejects_traversal(candidate: str, tmp_path: Path):
    result = _parse_out(f"goal --out {candidate}", tmp_path)
    assert result.returncode == 2
    assert "invalid --out path (path traversal not allowed)" in result.stderr
    assert result.stderr.startswith("sweep:")


def test_parse_out_rejects_escape_without_traversal(tmp_path: Path):
    """An absolute path outside the project root escapes without containing ``..``."""
    outside = tmp_path.parent / "outside-root.md"
    result = _parse_out(f"goal --out {outside}", tmp_path)
    assert result.returncode == 2
    assert "--out path escapes project root" in result.stderr


def test_parse_out_requires_sentinel(tmp_path: Path):
    result = subprocess.run(
        [sys.executable, BIN, "--parse-out=goal --out x.md"], capture_output=True, text=True, cwd=str(tmp_path)
    )
    assert result.returncode == 2
    assert "--sentinel" in result.stderr

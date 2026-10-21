"""Reject unsafe sentinel identities before filesystem or resolver work."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parents[1] / "bin"


@pytest.mark.parametrize(
    ("script", "args"),
    [
        pytest.param("find_judge_verdict.py", [], id="judge-verdict"),
        pytest.param("fortify_next_variant.py", [], id="fortify-cursor"),
        pytest.param("parse_kaggle_args.py", ["titanic"], id="kaggle"),
        pytest.param("codemap-flag.py", ["research", "--no-codemap"], id="codemap"),
        pytest.param("check_output_within_root.py", ["--parse-out=", "--sentinel", "research"], id="output"),
        pytest.param("resolve_shared.py", ["--persist", "research"], id="shared-path"),
    ],
)
@pytest.mark.parametrize("identity", ["../escape", "bad..name", "bad\\name", "bad:name", "bad\nname", "café"])
def test_rejects_unsafe_session_identity(tmp_path: Path, script: str, args: list[str], identity: str) -> None:
    """Malformed session suffixes fail with one diagnostic before any files are touched."""
    proc = subprocess.run(
        [sys.executable, str(_BIN / script), *args],
        cwd=tmp_path,
        env={**os.environ, "TMPDIR": str(tmp_path), "CSID": identity},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 2, (proc.stdout, proc.stderr)
    assert len(proc.stderr.splitlines()) == 1
    assert "invalid sentinel" in proc.stderr
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("script", "args"),
    [
        pytest.param("codemap-flag.py", ["{slug}", "--no-codemap"], id="codemap"),
        pytest.param("check_output_within_root.py", ["--parse-out=", "--sentinel", "{slug}"], id="output"),
        pytest.param("resolve_shared.py", ["--persist", "{slug}"], id="shared-path"),
    ],
)
@pytest.mark.parametrize("slug", ["", "../escape", "bad..name", "bad\\name", "bad:name", "bad\nname", "café"])
def test_rejects_unsafe_sentinel_slug(tmp_path: Path, script: str, args: list[str], slug: str) -> None:
    """Slug inputs cannot introduce traversal or malformed basenames."""
    proc = subprocess.run(
        [sys.executable, str(_BIN / script), *(arg.replace("{slug}", slug) for arg in args)],
        cwd=tmp_path,
        env={**os.environ, "TMPDIR": str(tmp_path), "CSID": "valid-session"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 2, (proc.stdout, proc.stderr)
    assert len(proc.stderr.splitlines()) == 1
    assert "invalid sentinel" in proc.stderr
    assert list(tmp_path.iterdir()) == []

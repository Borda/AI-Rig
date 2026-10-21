"""Tests for ``scan-codebase`` argument validation in ``parse_scan_args`` and its ``setup_scan_env`` exit code."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from parse_scan_args import parse_scan_args, validate_scan_args

_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_SUPPORTED = "Supported: --root <path>, --incremental"


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param("", id="empty"),
        pytest.param("--incremental", id="incremental"),
        pytest.param("--root /abs/path --incremental", id="root-and-incremental"),
        pytest.param('--root "a --b"', id="quoted-root-containing-flag-text"),
        pytest.param("--root '/p with space'", id="quoted-root-with-space"),
    ],
)
def test_valid_arguments_pass(arguments: str) -> None:
    """Quoted roots stay one token, so text inside them is never flagged."""
    assert validate_scan_args(arguments) is None


@pytest.mark.parametrize(
    ("arguments", "first_line"),
    [
        pytest.param("--full", "! Unknown flag(s): --full", id="unknown"),
        pytest.param("--root=/x", "! Unknown flag(s): --root=/x", id="equals-root-is-not-parsed"),
        pytest.param("--incremental-foo", "! Unknown flag(s): --incremental-foo", id="lookalike"),
        pytest.param("--incremental --root", "! --root requires a value", id="trailing-bare-root"),
        pytest.param("--root --incremental", "! --root requires a value", id="root-value-is-a-flag"),
    ],
)
def test_invalid_arguments_get_the_pinned_wording(arguments: str, first_line: str) -> None:
    """Rejections use the exact two-line wording scan-codebase prints unchanged."""
    assert validate_scan_args(arguments) == f"{first_line}\n{_SUPPORTED}"


def test_incremental_is_a_token_not_a_substring() -> None:
    """A root path containing the flag text never enables incremental mode."""
    assert parse_scan_args("--root /a--incremental") == ["--root", "/a--incremental"]


def test_setup_exits_5_with_message_verbatim(tmp_path: Path) -> None:
    """setup_scan_env.py surfaces the rejection on stderr under its own exit code, before any state write."""
    env = {k: v for k, v in os.environ.items() if k != "CSID"}
    env.update({"CLAUDE_PLUGIN_ROOT": str(_PLUGIN_ROOT), "TMPDIR": str(tmp_path)})
    result = subprocess.run(
        [sys.executable, str(_PLUGIN_ROOT / "bin" / "setup_scan_env.py"), "--arguments", "--root '/x y' --bogus"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
        timeout=60,
        check=False,
    )

    assert result.returncode == 5
    assert result.stderr == f"! Unknown flag(s): --bogus\n{_SUPPORTED}\n"
    assert result.stdout == ""
    assert not list(tmp_path.glob("codemap-scan-state-*"))

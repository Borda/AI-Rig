"""Exercise lossless resolve argument transport into Bash."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from _skill_source import read_skill_source

_PARSER = Path(__file__).resolve().parents[2] / "bin" / "parse-resolve-args.py"
_FLAGS = _PARSER.with_name("parse-skill-flags.py")
_BASH = shutil.which("bash")


@pytest.mark.parametrize("comment", ["it's broken", "first line\nsecond line", "$(touch should-not-exist)"])
def test_null_stream_preserves_comment(comment: str) -> None:
    """Comments reach the consumer as data without shell quoting or line splitting."""
    result = subprocess.run(
        [sys.executable, str(_PARSER), "--null", comment], capture_output=True, check=False, timeout=5
    )
    assert result.returncode == 0
    assert result.stdout.decode().split("\0") == [
        "PR_NUMBER",
        "",
        "PR_URL",
        "",
        "MODE",
        "comment-dispatch",
        "ARGUMENTS",
        comment,
        "",
    ]


def test_flag_preprocessing_preserves_multiline_comment(tmp_path: Path) -> None:
    """Flag removal retains newlines and indentation before resolve's argument transport."""
    comment = "it's wrong\nkeep this paragraph\n    indented example"
    result = subprocess.run(
        [
            sys.executable,
            str(_FLAGS),
            "--flags",
            "no-challenge",
            "--preserve-whitespace",
            "--keep-state",
            "resolve",
            comment,
        ],
        env=os.environ | {"TMPDIR": str(tmp_path), "CSID": "multiline"},
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 0
    assert (tmp_path / "resolve-clean-args-multiline").read_text() == comment + "\n"


@pytest.mark.skipif(_BASH is None, reason="Resolve intake requires Bash")
def test_shipped_intake_preserves_multiline_comment(tmp_path: Path) -> None:
    """The real flag preprocessing and NUL-reading Bash consumer retain the entire comment."""
    plugin = _PARSER.parent.parent
    source = read_skill_source(plugin / "skills/resolve/SKILL.md")
    block = next(block for block in re.findall(r"```bash\n(.*?)```", source, re.S) if "tmpenv=$(mktemp)" in block)
    comment = "it's wrong\n    preserve indentation"
    result = subprocess.run(
        [_BASH, "-c", block + '\nprintf "\\0%s\\0" "$ARGUMENTS"'],
        env=os.environ
        | {
            "ARGUMENTS": comment,
            "CLAUDE_PLUGIN_ROOT": str(plugin),
            "TMPDIR": str(tmp_path),
            "CLAUDE_CODE_SESSION_ID": "intake",
            "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
        },
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split("\0")[-2] == comment

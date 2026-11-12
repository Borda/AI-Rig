"""Exercise raw argument isolation in Foundry's shipped parser invocations."""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parents[2]
_PARSER = _PLUGIN / "bin/parse-skill-flags.py"


def _caller_arguments(skill: str) -> list[str]:
    """Read the actual parser options and raw placeholder from one skill command."""
    lines = (_PLUGIN / "skills" / skill / "SKILL.md").read_text(encoding="utf-8").splitlines()
    command = next(line for line in lines if '/bin/parse-skill-flags.py"' in line)
    tokens = shlex.split(command, comments=True)
    parser_index = next(index for index, token in enumerate(tokens) if token.endswith("/parse-skill-flags.py"))
    return tokens[parser_index + 1 : tokens.index("$ARGUMENTS")]


@pytest.mark.integration
@pytest.mark.parametrize("skill", ["brainstorm", "calibrate", "distill", "investigate", "manage", "profile"])
@pytest.mark.parametrize("blob", ["--inlined", "--keep-state=foreign", "--out-file=foreign"])
def test_raw_blob_cannot_configure_the_helper(
    skill: str, blob: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Helper-shaped user text must survive in the caller's own clean-argument sentinel."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "caller-boundary")
    options = [
        argument.replace("${TMPDIR:-/tmp}", str(tmp_path)).replace("${CSID}", "caller-boundary")
        for argument in _caller_arguments(skill)
    ]
    prefix = options[options.index("--keep-state") + 1]
    result = subprocess.run(
        [sys.executable, str(_PARSER), *options, blob],
        cwd=tmp_path,
        capture_output=True,
        encoding="utf-8",
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    values = dict(token.split("=", 1) for token in shlex.split(result.stdout))
    assert values["CLEAN_ARGS"] == blob
    assert (tmp_path / f"{prefix}-clean-args-caller-boundary").read_text(encoding="utf-8") == blob + "\n"


@pytest.mark.integration
@pytest.mark.parametrize(
    ("blob", "expected"),
    [
        pytest.param("--approve", "true", id="standalone"),
        pytest.param("--APPROVE", "true", id="case-insensitive"),
        pytest.param('"--approve"', "false", id="quoted-data"),
        pytest.param("-- --approve", "false", id="after-terminator"),
        pytest.param("--keep-state=foreign", "false", id="helper-option-is-data"),
    ],
)
def test_setup_approval_uses_the_shared_flag_parser(
    blob: str, expected: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shipped setup invocation detects only an unquoted standalone approval flag."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "setup-boundary")
    result = subprocess.run(
        [sys.executable, str(_PARSER), *_caller_arguments("setup"), blob],
        cwd=tmp_path,
        capture_output=True,
        encoding="utf-8",
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "foundry-setup-approve-setup-boundary").read_text(encoding="utf-8") == expected + "\n"
    assert not (tmp_path / "foreign-approve-setup-boundary").exists()

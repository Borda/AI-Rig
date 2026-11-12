"""Exercise raw argument boundaries in the shipped skill flag-parser commands."""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_PARSER = _ROOT / "plugins/cc_oss/bin/parse-skill-flags.py"
_CALLERS = (
    "cc_oss/skills/analyse/SKILL.md",
    "cc_oss/skills/release/SKILL.md",
    "cc_oss/skills/review/modes/preflight.md",
    "cc_oss/skills/resolve/modes/preflight.md",
    "cc_develop/skills/debug/SKILL.md",
    "cc_develop/skills/feature/SKILL.md",
    "cc_develop/skills/plan/SKILL.md",
    "cc_develop/skills/_shared/preflight-helpers.md",
)


def _caller_commands() -> list:
    """Extract each real parser invocation through its raw argument placeholder."""
    commands = []
    for caller in _CALLERS:
        for number, line in enumerate((_ROOT / "plugins" / caller).read_text(encoding="utf-8").splitlines(), 1):
            if '/bin/parse-skill-flags.py"' not in line:
                continue
            tokens = shlex.split(line, comments=True)
            parser_index = next(index for index, token in enumerate(tokens) if token.endswith("/parse-skill-flags.py"))
            raw_argument = "$ARGUMENT_BLOB" if caller.startswith("cc_develop/") else "$ARGUMENTS"
            arguments = tokens[parser_index + 1 : tokens.index(raw_argument) + 1]
            commands.append(pytest.param(arguments, id=f"{caller}:{number}"))
    return commands


@pytest.mark.integration
@pytest.mark.parametrize("arguments", _caller_commands())
@pytest.mark.parametrize("blob", ["--inlined", "--keep-state=foreign"])
def test_skill_call_preserves_helper_shaped_raw_text(
    arguments: list[str], blob: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Raw text must survive as clean arguments instead of configuring the helper."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "caller-boundary")
    result = subprocess.run(
        [sys.executable, str(_PARSER), *arguments[:-1], blob],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    values = dict(token.split("=", 1) for token in shlex.split(result.stdout))
    assert values["CLEAN_ARGS"] == blob

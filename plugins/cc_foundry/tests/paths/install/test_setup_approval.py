"""Run the setup approval handoff in a real shell with the canonical flag parser."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parents[3]
_BASH = shutil.which("bash")


def _approval_block() -> str:
    """Extract the approval stanza without executing the unrelated setup mutations."""
    lines = (_PLUGIN / "skills/setup/SKILL.md").read_text(encoding="utf-8").splitlines()
    start = next(
        index
        for index, line in enumerate(lines)
        if line.startswith(("APPROVE_ALL=false", '$PYTHON_CMD "$PLUGIN_ROOT/bin/parse-skill-flags.py"'))
    )
    end = lines.index('echo "APPROVE_ALL=$APPROVE_ALL"', start)
    return "\n".join(lines[start : end + 1])


@pytest.mark.skipif(_BASH is None, reason="setup recipe requires Bash")
@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        pytest.param('"example --approve text"', "false", id="quoted-data"),
        pytest.param("\t--ApPrOvE\n", "true", id="case-insensitive-token"),
        pytest.param("--inlined", "false", id="raw-helper-option"),
    ],
)
def test_setup_reads_parser_approval_sentinel(tmp_path: Path, arguments: str, expected: str) -> None:
    """Setup transports raw text without eval and replaces a previous approval value."""
    plugin = tmp_path / "plugin"
    (plugin / "bin").mkdir(parents=True)
    canonical = _PLUGIN.parent / "cc_oss/bin/parse-skill-flags.py"
    parser = canonical if canonical.is_file() else _PLUGIN / "bin/parse-skill-flags.py"
    shutil.copyfile(parser, plugin / "bin/parse-skill-flags.py")
    sentinel = tmp_path / "foundry-setup-approve-approval"
    sentinel.write_text("true\n", encoding="utf-8")
    result = subprocess.run(
        [_BASH, "-c", _approval_block()],
        cwd=tmp_path,
        env=os.environ
        | {
            "ARGUMENTS": arguments,
            "PLUGIN_ROOT": str(plugin),
            "PYTHON_CMD": sys.executable,
            "CSID": "approval",
            "TMPDIR": str(tmp_path),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"APPROVE_ALL={expected}\n"
    assert sentinel.read_text(encoding="utf-8") == f"{expected}\n"

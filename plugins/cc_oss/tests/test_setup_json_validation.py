"""Execute the setup JSON validators with jq absent from command lookup."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_PLUGINS = Path(__file__).resolve().parents[2]
_BASH = shutil.which("bash")
_CP = shutil.which("cp")


@pytest.mark.integration
@pytest.mark.skipif(not _BASH or not _CP, reason="setup validation requires Bash and cp")
@pytest.mark.parametrize("plugin", ["cc_foundry", "cc_oss"])
@pytest.mark.parametrize(
    ("merged", "expected_rc", "expected_settings"),
    [
        pytest.param('{"merged":true}\n', 0, '{"merged":true}\n', id="valid-merge-kept-without-jq"),
        pytest.param('{"merged":', 1, '{"backup":true}\n', id="invalid-merge-restores-backup"),
    ],
)
def test_setup_validation_without_jq(
    tmp_path: Path, plugin: str, merged: str, expected_rc: int, expected_settings: str
) -> None:
    """Preserve a valid merge without jq and retain the real parse-failure restore path."""
    assert _BASH is not None
    assert _CP is not None
    setup = (_PLUGINS / plugin / "skills/setup/SKILL.md").read_text(encoding="utf-8")
    validation = next(line for line in setup.splitlines() if line.startswith(("jq empty ", "python -m json.tool ")))
    config = tmp_path / ".claude"
    config.mkdir()
    settings = config / "settings.json"
    settings.write_text(merged, encoding="utf-8", newline="\n")
    (config / "settings.json.bak-test").write_text('{"backup":true}\n', encoding="utf-8", newline="\n")
    sentinel_dir = tmp_path / "sentinels"
    sentinel_dir.mkdir()
    (sentinel_dir / "foundry-setup-bak-ts-test").write_text("test\n", encoding="utf-8", newline="\n")
    if plugin == "cc_foundry":
        restore = re.search(r"restore from backup: `([^`]+)`", setup)
        assert restore is not None
        validation = f"if {validation}\nthen :; else {restore.group(1)}; exit 1; fi"
    prelude = (
        f'python() {{ {shlex.quote(Path(sys.executable).as_posix())} "$@"; }}\n'
        f'cp() {{ {shlex.quote(Path(_CP).as_posix())} "$@"; }}\n'
        "export SETUP_BAK_TS=test CLAUDE_CODE_SESSION_ID=test\n"
        "command -v jq && exit 99\n"
    )
    env = {
        **os.environ,
        "HOME": tmp_path.as_posix(),
        "TMPDIR": sentinel_dir.as_posix(),
        "PATH": sentinel_dir.as_posix(),
    }
    result = subprocess.run([_BASH, "-c", prelude + validation], env=env, capture_output=True, text=True, check=False)
    assert result.returncode == expected_rc, result.stderr
    assert settings.read_text(encoding="utf-8") == expected_settings

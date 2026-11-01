"""Execute the setup JSON validators with jq absent from command lookup.

Both setup skills validate the merged ``settings.json`` with a one-line check that must still work on a machine without
``jq``. The foundry skill additionally restores the backup when the check fails; the oss skill leaves that to the user.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

_PLUGINS = Path(__file__).resolve().parents[2]
_BASH = shutil.which("bash")
_CP = shutil.which("cp")


def _as_written(setup: str, validation: str) -> str:
    """Use the validation line exactly as the skill ships it (oss reports failure by exit code only)."""
    return validation


def _with_restore(setup: str, validation: str) -> str:
    """Wrap the validation in the skill's own documented restore-from-backup command (foundry)."""
    restore = re.search(r"restore from backup: `([^`]+)`", setup)
    assert restore is not None
    return f"if {validation}\nthen :; else {restore.group(1)}; exit 1; fi"


@pytest.mark.integration
@pytest.mark.skipif(not _BASH or not _CP, reason="setup validation requires Bash and cp")
@pytest.mark.parametrize(
    ("plugin", "build_script"),
    [
        pytest.param("cc_foundry", _with_restore, id="foundry"),
        pytest.param("cc_oss", _as_written, id="oss"),
    ],
)
@pytest.mark.parametrize(
    ("merged", "expected_rc", "expected_settings"),
    [
        pytest.param('{"merged":true}\n', 0, '{"merged":true}\n', id="valid-merge-kept-without-jq"),
        pytest.param('{"merged":', 1, '{"backup":true}\n', id="invalid-merge-restores-backup"),
    ],
)
def test_setup_validation_without_jq(
    tmp_path: Path,
    plugin: str,
    build_script: Callable[[str, str], str],
    merged: str,
    expected_rc: int,
    expected_settings: str,
) -> None:
    """Preserve a valid merge without jq and retain the real parse-failure restore path.

    Scenario: the setup skill's validation line runs in bash with ``jq`` hidden from lookup (PATH holds only an empty
    sentinel dir). A valid merged file is kept; an invalid one fails the check and the backup is restored, so a broken
    ``settings.json`` is never left in place.
    """
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
    script = prelude + build_script(setup, validation)
    result = subprocess.run([_BASH, "-c", script], env=env, capture_output=True, text=True, check=False)
    assert result.returncode == expected_rc, result.stderr
    assert settings.read_text(encoding="utf-8") == expected_settings

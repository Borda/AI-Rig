"""Tests that an explicit release range survives from Mode Detection into Gather changes.

Every fenced bash block in ``release/SKILL.md`` runs in a fresh shell, so a range parsed in Mode Detection is lost
unless it is written to the session-scoped ``release-range`` sentinel. Losing it made ``/oss:release v1.2->v2.0``
silently build notes and CHANGELOG entries from ``LAST_TAG..HEAD`` instead of the requested commits.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_SKILL = (_PLUGIN_ROOT / "skills" / "release" / "SKILL.md").read_text(encoding="utf-8")
_BLOCKS = re.findall(r"```bash\n(.*?)```", _SKILL, re.DOTALL)
_MODE_BLOCK = next(block for block in _BLOCKS if "release-do-migration-" in block and "parse-skill-flags.py" in block)
_GATHER_RELOAD = next(block for block in _BLOCKS if "# quote RANGE" in block).split('if [ -z "$RANGE" ]', 1)[0]


@pytest.mark.skipif(shutil.which("bash") is None, reason="Release skill blocks run in Bash")
@pytest.mark.parametrize(
    ("arguments", "expected_range"),
    [
        pytest.param("notes v1.2->v2.0", "v1.2..v2.0", id="arrow-range-persists"),
        pytest.param("v1.2..v2.0 --changelog", "v1.2..v2.0", id="bare-two-dot-range-persists"),
        pytest.param("prepare v1.3.0", "", id="prepare-version-is-not-a-range"),
        pytest.param("notes", "", id="no-range-clears-stale-sentinel"),
    ],
)
def test_mode_detection_range_reaches_gather_shell(tmp_path: Path, arguments: str, expected_range: str) -> None:
    """Gather changes, in its own shell, sees exactly the range Mode Detection parsed, or none.

    A stale sentinel from an earlier invocation in the same session is planted first: Mode Detection must overwrite
    it, so a run without an explicit range falls through to the marker/``LAST_TAG`` default instead of reusing it.
    """
    sentinel = tmp_path / "release-range-test"
    sentinel.write_text("stale..range\n", encoding="utf-8", newline="\n")
    environment = os.environ.copy()
    environment.update(
        {
            "TMPDIR": str(tmp_path),
            "CLAUDE_CODE_SESSION_ID": "test",
            "CLAUDE_PLUGIN_ROOT": str(_PLUGIN_ROOT),
            "PATH": os.pathsep.join([str(Path(sys.executable).parent), environment.get("PATH", "")]),
            "ARGUMENTS": arguments,
        }
    )
    bash = shutil.which("bash")
    subprocess.run([bash, "-c", _MODE_BLOCK], cwd=tmp_path, env=environment, check=True, capture_output=True)

    gather = subprocess.run(
        [bash, "-c", _GATHER_RELOAD + 'printf "RANGE=%s\\n" "$RANGE"'],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    assert gather.stdout.strip().splitlines()[-1] == f"RANGE={expected_range}"

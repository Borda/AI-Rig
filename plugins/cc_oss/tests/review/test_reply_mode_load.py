"""Tests that review's Step 8 load block reaches the reply draft whenever the run is in reply mode.

Step 8's body lives in ``modes/reply.md`` and loads through a fenced Bash block in ``SKILL.md``. The direct report fast-
path can switch a run into reply mode after a question, without ``--reply`` on the command line, so the flags sentinel
still says ``REPLY_MODE=false``. A load block that re-read that sentinel and exited would silently skip the reply draft
the user just accepted; these tests run the shipped block with exactly that sentinel and check the draft instructions
are printed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parents[2]
_REVIEW_DIR = _PLUGIN / "skills" / "review"
_SESSION = "reply-load"

_skip_bash_unavailable = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")


def _reply_load_block() -> str:
    """Return the shipped fenced Bash block in review SKILL.md that prints ``modes/reply.md``."""
    text = (_REVIEW_DIR / "SKILL.md").read_text(encoding="utf-8")
    blocks = re.findall(r"^```bash\n(.*?)^```", text, flags=re.MULTILINE | re.DOTALL)
    return next(block for block in blocks if "modes/reply.md" in block)


@pytest.fixture
def stale_flags_tmpdir(tmp_path: Path) -> Path:
    """TMPDIR whose flags sentinel says ``REPLY_MODE=false`` and whose skill-dir sentinel points at the source tree."""
    # Match Bash's LF sentinels; Windows text writes would leave CR in values read by Bash.
    (tmp_path / f"oss-review-flags-{_SESSION}").write_bytes(b"REPLY_MODE=false\nDIRECT_PATH_MODE=true\n")
    (tmp_path / f"review-skill-dir-{_SESSION}").write_bytes((_REVIEW_DIR.as_posix() + "\n").encode("utf-8"))
    return tmp_path


@_skip_bash_unavailable
def test_reply_draft_loads_despite_stale_flags_sentinel(stale_flags_tmpdir: Path) -> None:
    """The Step 8 block prints the reply instructions even when the flags file still records ``REPLY_MODE=false``.

    Scenario: a report path was passed without ``--reply`` and the user accepted reply mode at the fast-path question.
    Only the conversation knows that; the block must not overrule it from the sentinel written at flag parsing.
    """
    environment = os.environ.copy()
    environment.update({"TMPDIR": str(stale_flags_tmpdir), "CLAUDE_CODE_SESSION_ID": _SESSION})

    completed = subprocess.run(
        [shutil.which("bash"), "-c", _reply_load_block()],
        cwd=stale_flags_tmpdir,
        env=environment,
        capture_output=True,
        # cat emits the file's UTF-8 bytes; text=True alone decodes with the locale codec (cp1252 on Windows).
        encoding="utf-8",
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == (_REVIEW_DIR / "modes" / "reply.md").read_text(encoding="utf-8")

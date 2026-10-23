"""Tests that review and resolve read their ``KEY=value`` state files as data, never as shell code.

The flags, gate and mode-flag sentinels and resolve's ``pr-vars.sh`` carry values that can hold outside text: a PR's
failing CI check names, a report path, an agent-written motivation line. These files used to be sourced with ``.``, so a
value with a space ran its second word as a command and a value with ``;`` or ``$(…)`` ran arbitrary code, all inside
blocks the blueprint manifest auto-allows. Each shipped consumer line now parses the file with ``while IFS='=' read``
and a closed ``case`` of known keys; these tests run that exact line in Bash and check that the value comes back
verbatim and that nothing in it executed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parents[2]
_REVIEW_SKILL = _PLUGIN / "skills" / "review" / "SKILL.md"
_PAYLOAD = 'x; touch "$MARK"; $(touch "$MARK") `touch "$MARK"` y'


def _consumer_line(path: Path, marker: str) -> str:
    """Return the first shipped line in ``path`` that contains ``marker``."""
    return next(line for line in path.read_text(encoding="utf-8").splitlines() if marker in line)


def _read_back(tmp_path: Path, line: str, prelude: str, variable: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run one consumer line in Bash and print the variable it loaded; return the process and the marker path."""
    mark = tmp_path / "executed"
    environment = os.environ.copy()
    environment.update({"TMPDIR": str(tmp_path), "CSID": "test", "MARK": str(mark)})
    script = f'{prelude}\n{line}\nprintf "%s" "${variable}"'
    completed = subprocess.run(
        [shutil.which("bash"), "-c", script], cwd=tmp_path, env=environment, capture_output=True, text=True, check=False
    )
    return completed, mark


@pytest.mark.skipif(shutil.which("bash") is None, reason="Review skill blocks run in Bash")
@pytest.mark.parametrize(
    ("source", "marker", "state_file", "content", "prelude", "variable", "expected"),
    [
        pytest.param(
            _REVIEW_SKILL,
            'oss-review-flags-${CSID}" ] && while',
            "oss-review-flags-test",
            f"REPLY_MODE=true\nREVIEW_FILE=report dir/{_PAYLOAD}\n",
            "",
            "REVIEW_FILE",
            f"report dir/{_PAYLOAD}",
            id="review-flags-report-path",
        ),
        pytest.param(
            _REVIEW_SKILL,
            'oss-review-gate-${CSID}" ] && while',
            "oss-review-gate-test",
            f"GATE=BLOCK\nGATE_REASON=ci-red: lint, {_PAYLOAD}\n",
            "",
            "GATE_REASON",
            f"ci-red: lint, {_PAYLOAD}",
            id="review-gate-ci-check-names",
        ),
        pytest.param(
            _REVIEW_SKILL,
            '[ -f "$_REVIEW_MODE_FILE" ] && while',
            "oss-review-mode-flags-7-test",
            f"PR_TYPE=CODE {_PAYLOAD}\n",
            '_REVIEW_MODE_FILE="$TMPDIR/oss-review-mode-flags-7-test"',
            "PR_TYPE",
            f"CODE {_PAYLOAD}",
            id="review-mode-flags",
        ),
        pytest.param(
            _PLUGIN / "skills" / "review" / "modes" / "codemap-context.md",
            'oss-review-mode-flags-${CLEAN_ARGS}-${CSID}" ] && while',
            "oss-review-mode-flags-7-test",
            f"CICD_ONLY_MODE=true\nDOCS_ONLY_MODE={_PAYLOAD}\n",
            "CLEAN_ARGS=7",
            "DOCS_ONLY_MODE",
            _PAYLOAD,
            id="codemap-context-mode-flags",
        ),
        pytest.param(
            _PLUGIN / "skills" / "resolve" / "modes" / "pr-intelligence.md",
            'done < "$IMPL_DIR/pr-vars.sh"',
            "pr-vars.sh",
            "ACTION_ITEMS_TOTAL='3'\nPR_MOTIVATION='fix $(touch \"$MARK\") now'\n",
            'IMPL_DIR="$TMPDIR"',
            "PR_MOTIVATION",
            'fix $(touch "$MARK") now',
            id="resolve-pr-vars",
        ),
        pytest.param(
            _PLUGIN / "skills" / "resolve" / "modes" / "pr-intelligence.md",
            'done < "$IMPL_DIR/pr-vars.sh"',
            "pr-vars.sh",
            "ACTION_ITEMS_TOTAL='3'\nRESOLVED_THREAD_IDS_COUNT='4'",
            'IMPL_DIR="$TMPDIR"',
            "RESOLVED_THREAD_IDS_COUNT",
            "4",
            id="resolve-pr-vars-no-final-newline",
        ),
    ],
)
def test_state_values_read_back_verbatim(
    tmp_path: Path, source: Path, marker: str, state_file: str, content: str, prelude: str, variable: str, expected: str
) -> None:
    """A state value carrying spaces, ``;``, ``$(…)`` and backticks is loaded as text and never executed.

    The state file is written the way its producer writes it (``KEY=value`` lines, later lines winning), then the
    shipped consumer line reads it in a fresh Bash process. Sourcing the same file would have run ``touch`` and created
    the marker; parsing must leave it absent and return the value byte for byte.
    """
    (tmp_path / state_file).write_text(content, encoding="utf-8", newline="\n")
    completed, mark = _read_back(tmp_path, _consumer_line(source, marker), prelude, variable)
    assert (completed.stdout, mark.exists()) == (expected, False), completed.stderr

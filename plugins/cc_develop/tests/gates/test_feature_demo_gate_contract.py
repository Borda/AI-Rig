"""Run develop:feature's example-script demo gate as shipped and pin which statuses count as a red demo.

The example-script branch once read the status of ``python "$DEMO_SCRIPT" | tail``, which is the status of ``tail``, and
treated every non-zero status as "failed as expected". The block sets ``pipefail``, so the pipe reports the demo's own
status, and it accepts only exit 1 as a red demo; a missing script (2), a signal, or an interpreter that cannot start is
a gate error, never a pass of the gate. No log file is written under ``TMPDIR``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from _develop_text import bash_blocks

_SKILL = Path(__file__).resolve().parents[2] / "skills/feature/SKILL.md"
_BASH = shutil.which("bash")
_CSID = "feature-demo-gate-test"

_skip_no_bash = pytest.mark.skipif(_BASH is None, reason="bash unavailable")


def _gate_block(demo_script: str) -> str:
    """Return the feature gate block with ``DEMO_SCRIPT`` set to ``demo_script`` instead of the empty doctest form."""
    blocks = bash_blocks(_SKILL.read_text(encoding="utf-8"))
    block = next(block for block in blocks if 'elif [ -n "$DEMO_SCRIPT" ]' in block)
    return block.replace('DEMO_SCRIPT=""', f'DEMO_SCRIPT="{demo_script}"', 1)


def _run(
    block: str, tmpdir: Path, cwd: Path, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run ``block`` in bash from ``cwd`` with the session state under ``tmpdir``."""
    env = {
        **os.environ,
        "TMPDIR": tmpdir.as_posix(),
        "CLAUDE_CODE_SESSION_ID": _CSID,
        "CLAUDE_PLUGIN_ROOT": _SKILL.parents[2].as_posix(),
        **(extra_env or {}),
    }
    return subprocess.run([_BASH, "-c", block], capture_output=True, encoding="utf-8", env=env, cwd=cwd, check=False)


@pytest.fixture
def demo_script(tmp_path: Path) -> Callable[[str], str]:
    """Return a factory that writes ``demo.py`` with the given source and returns its POSIX path."""

    def write(source: str) -> str:
        """Write one demo script and return its path."""
        script = tmp_path / "demo.py"
        script.write_text(source, encoding="utf-8")
        return script.as_posix()

    return write


@_skip_no_bash
class TestExampleScriptGate:
    """Only an exit 1 from the demo script is a red demo."""

    @pytest.mark.parametrize(
        ("status", "verdict", "blocked"),
        [
            pytest.param(1, "GATE OK", False, id="demo-fails-as-expected"),
            pytest.param(0, "GATE FAIL: demo passed", False, id="demo-passes"),
            pytest.param(2, "GATE ERROR: demo script exit 2", True, id="demo-usage-error"),
            pytest.param(64, "GATE ERROR: demo script exit 64", True, id="demo-exit-matching-no-run-status"),
        ],
    )
    def test_status_decides_verdict(
        self, tmp_path: Path, demo_script: Callable[[str], str], status: int, verdict: str, blocked: bool
    ) -> None:
        """Exit 1 is the red demo, 0 means the feature already exists, any other status stops the gate.

        The demo script just exits with the parametrized status, so the verdict line and the block's own exit code show
        how the shipped bash maps each status.
        """
        result = _run(_gate_block(demo_script(f"raise SystemExit({status})\n")), tmp_path, tmp_path)
        assert (verdict in result.stdout, (result.returncode != 0) is blocked) == (True, True)

    def test_missing_script_is_a_gate_error(self, tmp_path: Path) -> None:
        """A demo path that does not exist makes Python exit 2; that must not read as a red demo.

        Exit 2 is also what a usage error returns, so the gate has to report it as an error rather than a failure.
        """
        result = _run(_gate_block((tmp_path / "absent.py").as_posix()), tmp_path, tmp_path)
        assert ("GATE ERROR: demo script exit 2" in result.stdout, "GATE OK" in result.stdout) == (True, False)

    def test_interpreter_that_cannot_start_is_a_gate_error(
        self, tmp_path: Path, demo_script: Callable[[str], str]
    ) -> None:
        """A Python that aborts during start-up exits 1 before the demo runs; that must not read as a red demo.

        ``PYTHONHOME`` pointing nowhere reproduces the abort; its status 1 equals a genuinely failing demo, so only the
        gate's own interpreter check can tell them apart.
        """
        script = demo_script("raise SystemExit(1)\n")
        result = _run(_gate_block(script), tmp_path, tmp_path, {"PYTHONHOME": "/nonexistent"})
        assert ("python cannot start" in result.stdout, "GATE OK" in result.stdout, result.returncode) == (
            True,
            False,
            1,
        )

    def test_writes_no_file_under_tmpdir(self, tmp_path: Path, demo_script: Callable[[str], str]) -> None:
        """The demo output goes through a pipe, so no predictable log file exists for a planted link to redirect.

        A fixed ``dev-feature-demo-out-<CSID>`` log under a shared ``/tmp`` let a planted symlink truncate another file
        of the caller's; ``pipefail`` keeps the demo's own status without that file.
        """
        script = demo_script("print('red')\nraise SystemExit(1)\n")
        state = tmp_path / "state"
        state.mkdir()
        result = _run(_gate_block(script), state, tmp_path)
        assert ("GATE OK" in result.stdout, sorted(p.name for p in state.glob("dev-feature-demo-out-*"))) == (True, [])

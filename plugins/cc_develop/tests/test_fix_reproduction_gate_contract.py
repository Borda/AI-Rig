"""Run develop:fix's reproduction gates as shipped and pin that only pytest exit 1 counts as a reproduced bug.

Each gate once read any non-zero exit as "failed as expected": a fresh shell without ``PYTEST_CMD`` ran ``--tb=short``
alone (exit 127) and printed ``GATE OK``, so the fix proceeded on a bug nobody reproduced. The blocks now reload the
runner, treat every exit other than 0 and 1 as a gate error, and the stop gate blocks unless both paths wrote 1.

The runner is faked with ``sh -c 'exit N' --`` so each case controls the exit code; the gate text runs unchanged except
for its ``<placeholder>`` targets, which the model substitutes at run time.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parents[1]
_SKILL = _PLUGIN / "skills/fix/SKILL.md"
_BASH = shutil.which("bash")
_CSID = "fix-gate-test"

_skip_no_bash = pytest.mark.skipif(_BASH is None, reason="bash unavailable")


def _block(*markers: str) -> str:
    """Return the fix skill's bash block containing every marker, with placeholders filled by a dummy target."""
    blocks = re.findall(r"```bash\n(.*?)```", _SKILL.read_text(encoding="utf-8"), re.DOTALL)
    block = next(block for block in blocks if all(marker in block for marker in markers))
    return re.sub(r"<[a-z_]+>", "x", block)


def _run(block: str, tmp_path: Path, runner: str | None) -> subprocess.CompletedProcess[str]:
    """Run ``block`` with ``runner`` as the ``dev-pytest-cmd`` sentinel (``None`` = sentinel missing)."""
    sentinel = tmp_path / f"dev-pytest-cmd-{_CSID}"
    sentinel.unlink(missing_ok=True)
    if runner is not None:
        sentinel.write_text(f"{runner}\n", encoding="utf-8", newline="\n")
    env = {
        **os.environ,
        "TMPDIR": tmp_path.as_posix(),
        "CLAUDE_CODE_SESSION_ID": _CSID,
        "CLAUDE_PLUGIN_ROOT": _PLUGIN.as_posix(),
        "PATH": os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", ""),
    }
    return subprocess.run(
        [_BASH, "-c", block], capture_output=True, encoding="utf-8", env=env, cwd=tmp_path, check=False
    )


def _gate_sentinels(tmp_path: Path) -> tuple[str, str]:
    """Return the Path 1 / Path 2 exit codes the reproduction block persisted for the stop gate."""
    return tuple(
        (tmp_path / f"dev-fix-gate-{path}-{_CSID}").read_text(encoding="utf-8").strip() for path in ("p1", "p2")
    )


@_skip_no_bash
class TestPartBReproductionGate:
    """Part B: both new reproduction tests must fail with exit 1 before the stop gate lets the fix proceed."""

    def test_missing_runner_blocks_and_leaves_blocking_sentinels(self, tmp_path: Path) -> None:
        """No ``PYTEST_CMD`` sentinel stops the block before any eval; the stop gate then refuses to continue.

        This is the reported failure: a fresh shell used to run `` --tb=short`` and print ``GATE OK``.
        """
        result = _run(_block("dev-fix-gate-p1", "GATE_P1=$?"), tmp_path, None)
        stop = _run(_block('IFS= read -r GATE_P1 < "${TMPDIR:-/tmp}/dev-fix-gate-p1'), tmp_path, None)
        assert (result.returncode, "GATE OK" in result.stdout, _gate_sentinels(tmp_path)) == (
            1,
            False,
            ("missing", "missing"),
        )
        assert (stop.returncode, "! GATE ERROR" in stop.stdout) == (1, True)

    @pytest.mark.parametrize(
        ("exit_code", "verdict", "stop_code", "stop_line"),
        [
            pytest.param(1, "GATE OK (Path 1): failed as expected (exit 1)", 0, "", id="exit-1-reproduced"),
            pytest.param(0, "GATE FAIL (Path 1): test passed", 1, "! GATE FAIL:", id="exit-0-not-captured"),
            pytest.param(4, "! GATE ERROR (Path 1): pytest could not run (exit 4)", 1, "! GATE ERROR:", id="exit-4"),
            pytest.param(
                127, "! GATE ERROR (Path 1): pytest could not run (exit 127)", 1, "! GATE ERROR:", id="exit-127"
            ),
        ],
    )
    def test_only_exit_one_reaches_the_fix(
        self, exit_code: int, verdict: str, stop_code: int, stop_line: str, tmp_path: Path
    ) -> None:
        """Exit 1 passes the stop gate; 0 is not captured; any other code (usage, collection, missing runner) errors."""
        result = _run(_block("dev-fix-gate-p1", "GATE_P1=$?"), tmp_path, f"sh -c 'exit {exit_code}' --")
        stop = _run(_block('IFS= read -r GATE_P1 < "${TMPDIR:-/tmp}/dev-fix-gate-p1'), tmp_path, None)
        assert (verdict in result.stdout, _gate_sentinels(tmp_path)) == (True, (str(exit_code), str(exit_code)))
        assert (stop.returncode, stop_line in stop.stdout) == (stop_code, True)


@_skip_no_bash
class TestOutcomeBGate:
    """Outcome B: the corrected weak test must now fail with exit 1, or the block stops."""

    @pytest.mark.parametrize(
        ("runner", "returncode", "line"),
        [
            pytest.param("sh -c 'exit 1' --", 0, "GATE OK: fixed test fails as expected (exit 1)", id="exit-1"),
            pytest.param("sh -c 'exit 5' --", 1, "! GATE STOP: Outcome B gate not satisfied (exit 5)", id="exit-5"),
            pytest.param(None, 1, "! BLOCKED — PYTEST_CMD sentinel missing", id="runner-missing"),
        ],
    )
    def test_only_exit_one_satisfies_the_gate(
        self, runner: str | None, returncode: int, line: str, tmp_path: Path
    ) -> None:
        """A missing runner or a non-1 exit stops Outcome B instead of reading as a corrected test."""
        result = _run(_block("GATE_EXIT=$?", "Outcome B gate not satisfied"), tmp_path, runner)
        assert (result.returncode, line in result.stdout) == (returncode, True)


@_skip_no_bash
class TestPartACandidateRun:
    """Part A: a candidate test that could not run is never read as Outcome A ("fails already")."""

    def test_rejected_runner_is_a_gate_error(self, tmp_path: Path) -> None:
        """``pytest_gate.py`` exits 2 for a runner outside its allowlist; the block reports a gate error, not a fail."""
        result = _run(_block("pytest_gate.py", "CANDIDATE_EXIT=$?"), tmp_path, "sh -c 'exit 1' --")
        assert "! GATE ERROR: candidate could not run (exit 2)" in result.stdout

    def test_missing_runner_blocks(self, tmp_path: Path) -> None:
        """No ``PYTEST_CMD`` sentinel stops before ``pytest_gate.py`` runs the default runner on a dummy target."""
        result = _run(_block("pytest_gate.py", "CANDIDATE_EXIT=$?"), tmp_path, None)
        assert (result.returncode, "CANDIDATE" in result.stdout) == (1, False)

"""Run develop:fix's reproduction gates as shipped and pin that only pytest exit 1 counts as a reproduced bug.

Each gate once read any non-zero exit as "failed as expected": a fresh shell without ``PYTEST_CMD`` ran ``--tb=short``
alone (exit 127) and printed ``GATE OK``, so the fix proceeded on a bug nobody reproduced. The blocks now reload the
runner, treat every exit other than 0 and 1 as a gate error, and the stop gate blocks unless both paths wrote 1.

The runner is faked with an allowlisted ``python -m pytest`` module so each case controls the exit code. The gate text
runs unchanged except for its ``<placeholder>`` targets, which the model substitutes at run time.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parents[1]
_SKILL = _PLUGIN / "skills/fix/SKILL.md"
_BASH = shutil.which("bash")
_CSID = "fix-gate-test"

_skip_no_bash = pytest.mark.skipif(_BASH is None, reason="bash unavailable")


def _symlinks_supported() -> bool:
    """Report whether this host lets an unprivileged user create a symlink (Windows may not)."""
    with tempfile.TemporaryDirectory() as directory:
        try:
            os.symlink(directory, os.path.join(directory, "probe"))
        except OSError:
            return False
    return True


_skip_no_symlink = pytest.mark.skipif(not _symlinks_supported(), reason="symlinks unavailable")


def _block(*markers: str) -> str:
    """Return the fix skill's bash block containing every marker, with placeholders filled by a dummy target."""
    blocks = re.findall(r"```bash\n(.*?)```", _SKILL.read_text(encoding="utf-8"), re.DOTALL)
    block = next(block for block in blocks if all(marker in block for marker in markers))
    return re.sub(r"<[a-z_]+>", "x", block)


def _run(
    block: str, tmp_path: Path, runner: str | int | None, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run ``block`` with ``runner`` as the ``dev-pytest-cmd`` sentinel (``None`` = sentinel missing)."""
    sentinel = tmp_path / f"dev-pytest-cmd-{_CSID}"
    sentinel.unlink(missing_ok=True)
    if isinstance(runner, int):
        # real pytest prints a failure summary whenever it exits 1; the wrapper relies on that to tell a failing test
        # from a runner that never started
        summary = 'print("FAILED t.py::t - assert 0")\nprint("1 failed in 0.01s")\n' if runner == 1 else ""
        (tmp_path / "pytest.py").write_text(f"{summary}raise SystemExit({runner})\n", encoding="utf-8")
        runner = "python -m pytest"
    if runner is not None:
        sentinel.write_text(f"{runner}\n", encoding="utf-8", newline="\n")
    env = {
        **os.environ,
        "TMPDIR": tmp_path.as_posix(),
        "CLAUDE_CODE_SESSION_ID": _CSID,
        "CLAUDE_PLUGIN_ROOT": _PLUGIN.as_posix(),
        "PATH": os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", ""),
        **(extra_env or {}),
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
        """No ``PYTEST_CMD`` sentinel stops the block before launching tests; the stop gate then refuses to continue.

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
            pytest.param(
                2, "! GATE ERROR (Path 1): pytest could not run (exit 2)", 1, "! GATE ERROR:", id="exit-2-interrupted"
            ),
        ],
    )
    def test_only_exit_one_reaches_the_fix(
        self, exit_code: int, verdict: str, stop_code: int, stop_line: str, tmp_path: Path
    ) -> None:
        """Exit 1 passes the stop gate; 0 is not captured; any other code (usage, collection, missing runner) errors."""
        result = _run(_block("dev-fix-gate-p1", "GATE_P1=$?"), tmp_path, exit_code)
        stop = _run(_block('IFS= read -r GATE_P1 < "${TMPDIR:-/tmp}/dev-fix-gate-p1'), tmp_path, None)
        assert (verdict in result.stdout, _gate_sentinels(tmp_path)) == (True, (str(exit_code), str(exit_code)))
        assert (stop.returncode, stop_line in stop.stdout) == (stop_code, True)

    @pytest.mark.parametrize("stale", [pytest.param("", id="no-earlier-verdict"), pytest.param("failed", id="stale")])
    def test_interpreter_dying_before_the_wrapper_is_not_a_reproduction(self, stale: str, tmp_path: Path) -> None:
        """A Python that cannot start exits 1 before the wrapper runs; without its ``failed`` verdict that is no red.

        ``PYTHONHOME`` pointing nowhere makes the interpreter abort during start-up with status 1, the same status as a
        failing test. A ``failed`` verdict left by an earlier run must not vouch for it either.
        """
        (tmp_path / f"dev-tests-verdict-{_CSID}").write_text(f"{stale}\n", encoding="utf-8")
        result = _run(_block("dev-fix-gate-p1", "GATE_P1=$?"), tmp_path, 1, {"PYTHONHOME": "/nonexistent"})
        stop = _run(_block('IFS= read -r GATE_P1 < "${TMPDIR:-/tmp}/dev-fix-gate-p1'), tmp_path, None)
        assert ("GATE OK" in result.stdout, _gate_sentinels(tmp_path)) == (False, ("70", "70"))
        assert (stop.returncode, "! GATE ERROR:" in stop.stdout) == (1, True)

    @_skip_no_symlink
    def test_planted_verdict_symlink_target_is_never_written(self, tmp_path: Path) -> None:
        """A symlink planted at the verdict path never lets the gate or the wrapper write through to its target.

        The gate no longer resets the file from the shell, which would truncate whatever a planted link points at; the
        wrapper replaces the link itself, so a genuine failure still reads as reproduced.
        """
        victim = tmp_path / "victim.txt"
        victim.write_text("keep\n", encoding="utf-8")
        (tmp_path / f"dev-tests-verdict-{_CSID}").symlink_to(victim)
        result = _run(_block("dev-fix-gate-p1", "GATE_P1=$?"), tmp_path, 1)
        assert (victim.read_text(encoding="utf-8"), "GATE OK (Path 1)" in result.stdout) == ("keep\n", True)


@_skip_no_bash
class TestOutcomeBGate:
    """Outcome B: the corrected weak test must now fail with exit 1, or the block stops."""

    @pytest.mark.parametrize(
        ("runner", "returncode", "line"),
        [
            pytest.param(1, 0, "GATE OK: fixed test fails as expected (exit 1)", id="exit-1"),
            pytest.param(5, 1, "! GATE STOP: Outcome B gate not satisfied (exit 5)", id="exit-5"),
            pytest.param(None, 1, "! BLOCKED — PYTEST_CMD sentinel missing", id="runner-missing"),
        ],
    )
    def test_only_exit_one_satisfies_the_gate(
        self, runner: int | None, returncode: int, line: str, tmp_path: Path
    ) -> None:
        """A missing runner or a non-1 exit stops Outcome B instead of reading as a corrected test."""
        result = _run(_block("GATE_EXIT=$?", "Outcome B gate not satisfied"), tmp_path, runner)
        assert (result.returncode, line in result.stdout) == (returncode, True)

    def test_interpreter_dying_before_the_wrapper_stops_the_gate(self, tmp_path: Path) -> None:
        """Exit 1 from an interpreter that never reached the wrapper has no ``failed`` verdict, so Outcome B stops."""
        result = _run(
            _block("GATE_EXIT=$?", "Outcome B gate not satisfied"), tmp_path, 1, {"PYTHONHOME": "/nonexistent"}
        )
        assert (result.returncode, "GATE OK" in result.stdout, "(exit 70)" in result.stdout) == (1, False, True)


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

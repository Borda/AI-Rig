"""Tests for ``bin/dev_run_repro.py`` — the pytest repro step of ``/develop:debug``.

Covers:
* Verdict line and ``dev-gate-exit`` sentinel for a failing and a passing run
* The exact runner command line (``--tb=long``, optional target, ``-v``) and the no-target full-suite shape
* Refusal paths: unresolved runner, non-allowlisted runner, target outside the project, stray arguments
* ``run_tail`` against a real subprocess: exit code propagation and bounded tail
"""

from __future__ import annotations

import sys
from pathlib import Path

import dev_run_repro
import pytest
from dev_run_dir import read_sentinel


@pytest.fixture(name="session")
def _session_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate ``$TMPDIR``, the session token and the CWD, and pin the runner lookup to a fake binary path.

    >>> getfixture("session").is_dir()
    True
    """
    base = tmp_path / "sentinels"
    base.mkdir()
    monkeypatch.setenv("TMPDIR", str(base))
    monkeypatch.setenv("CSID", "repro-test")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(dev_run_repro, "which", lambda name: f"/fake/{name}")
    return base


def _seed(session: Path, runner: str | None, test_path: str | None) -> None:
    """Write the two input sentinels the way runner detection and the debug flow do."""
    if runner is not None:
        (session / "dev-pytest-cmd-repro-test").write_text(runner + "\n", encoding="utf-8")
    if test_path is not None:
        (session / "dev-debug-test-path-repro-test").write_text(test_path + "\n", encoding="utf-8")


class TestVerdict:
    """The run's exit code decides the verdict line; the code itself lands in the gate sentinel."""

    @pytest.mark.parametrize(
        ("code", "verdict"),
        [
            pytest.param(1, "Bug reproduced — tests fail. Proceed to fix.", id="failing-run"),
            pytest.param(
                0, "Tests pass — bug may not be reproducible via pytest; check symptom directly.", id="passing-run"
            ),
        ],
    )
    def test_verdict_and_gate_exit(
        self,
        session: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        code: int,
        verdict: str,
    ) -> None:
        """Print the tail, then the verdict; record the runner's exit code for later blocks.

        Only pytest exit 1 (tests ran and failed) reads as "reproduced"; exit 0 is a pass.
        """
        _seed(session, "uv run pytest", "tests/test_a.py::test_x")
        monkeypatch.setattr(dev_run_repro, "run_tail", lambda argv: (code, ["last line"]))
        rc = dev_run_repro.main([])
        assert (rc, capsys.readouterr().out, read_sentinel("dev-gate-exit")) == (
            0,
            f"last line\n{verdict}\n",
            str(code),
        )

    @pytest.mark.parametrize(
        "code",
        [
            pytest.param(2, id="collection-error"),
            pytest.param(4, id="bad-node-id"),
            pytest.param(5, id="no-tests-collected"),
            pytest.param(127, id="runner-missing"),
        ],
    )
    def test_other_codes_are_a_gate_error_not_a_reproduction(
        self, session: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], code: int
    ) -> None:
        """Exit codes other than 0 or 1 fail closed: an error line, rc 1, and no "reproduced" verdict.

        A collection error or an unknown node id in a freshly written test is the common way a repro run goes wrong;
        reading it as a reproduced bug would send the lead into a fix for a bug nobody observed.
        """
        _seed(session, "uv run pytest", "tests/test_a.py::test_x")
        monkeypatch.setattr(dev_run_repro, "run_tail", lambda argv: (code, ["last line"]))
        rc = dev_run_repro.main([])
        out = capsys.readouterr().out
        assert (
            rc,
            "Bug reproduced" in out,
            f"! GATE ERROR: pytest exit {code}" in out,
            read_sentinel("dev-gate-exit"),
        ) == (
            1,
            False,
            True,
            str(code),
        )


class TestCommandLine:
    """The runner is invoked exactly as the inline blocks did, minus the shell."""

    @pytest.mark.parametrize(
        ("runner", "test_path", "expected"),
        [
            pytest.param(
                "uv run pytest",
                "tests/test_a.py::test_x",
                ["/fake/uv", "run", "pytest", "--tb=long", "tests/test_a.py::test_x", "-v"],
                id="node-id-target",
            ),
            pytest.param("pytest", "", ["/fake/pytest", "--tb=long", "-v"], id="empty-path-is-full-suite"),
            pytest.param(
                "python -m pytest", None, ["/fake/python", "-m", "pytest", "--tb=long", "-v"], id="no-path-sentinel"
            ),
        ],
    )
    def test_argv(
        self,
        session: Path,
        monkeypatch: pytest.MonkeyPatch,
        runner: str,
        test_path: str | None,
        expected: list[str],
    ) -> None:
        """An empty or absent target adds no argument at all — never an empty-string one or an explicit ``.``."""
        seen: list[list[str]] = []
        _seed(session, runner, test_path)
        monkeypatch.setattr(dev_run_repro, "run_tail", lambda argv: seen.append(argv) or (0, []))
        dev_run_repro.main([])
        assert seen == [expected]


class TestRefusals:
    """Inputs that must stop the run before pytest starts."""

    def test_unresolved_runner_exits_1_without_gate_sentinel(
        self, session: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An empty runner would exit 127 and look like a reproduced bug, so it is reported and nothing runs."""
        monkeypatch.setattr(dev_run_repro, "run_tail", lambda argv: pytest.fail("must not run"))
        rc = dev_run_repro.main([])
        out = capsys.readouterr().out
        assert (rc, out.startswith("! PYTEST_CMD unresolved"), read_sentinel("dev-gate-exit")) == (1, True, "")

    def test_non_allowlisted_runner_is_rejected(
        self, session: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A sentinel holding an arbitrary command never reaches a subprocess."""
        _seed(session, "rm -rf /", "")
        monkeypatch.setattr(dev_run_repro, "run_tail", lambda argv: pytest.fail("must not run"))
        rc = dev_run_repro.main([])
        assert (rc, "rejected unsafe PYTEST_CMD" in capsys.readouterr().err) == (2, True)

    def test_target_outside_project_is_rejected(self, session: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A test path lifted from an issue body cannot point pytest outside the working directory."""
        _seed(session, "pytest", "../../elsewhere/test_x.py")
        monkeypatch.setattr(dev_run_repro, "run_tail", lambda argv: pytest.fail("must not run"))
        with pytest.raises(SystemExit) as exc:
            dev_run_repro.main([])
        assert exc.value.code == 1

    def test_runner_missing_from_path_exits_2(
        self, session: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An allowlisted runner that is not installed is a clear error, not a traceback."""
        _seed(session, "poetry run pytest", "")
        monkeypatch.setattr(dev_run_repro, "which", lambda name: None)
        rc = dev_run_repro.main([])
        assert (rc, "not found on PATH" in capsys.readouterr().err) == (2, True)

    def test_stray_argument_is_a_usage_error(self, session: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """The script reads sentinels only; a leftover ``--test-path``-style argument fails loudly."""
        rc = dev_run_repro.main(["--test-path", "tests/x.py"])
        assert (rc, capsys.readouterr().out.startswith("usage:")) == (2, True)


class TestRunTail:
    """``run_tail`` against a real subprocess, no mocks."""

    def test_returns_exit_code_and_only_the_last_lines(self) -> None:
        """Ten lines in, three kept, in order; the child's exit code survives."""
        code, lines = dev_run_repro.run_tail(
            [sys.executable, "-c", "print(*range(10), sep='\\n'); raise SystemExit(4)"], tail_lines=3
        )
        assert (code, lines) == (4, ["7", "8", "9"])

    def test_merges_stderr_into_the_tail(self) -> None:
        """Pytest writes failures to stderr in some runners; the tail must show them like ``2>&1 | tail``."""
        code, lines = dev_run_repro.run_tail([sys.executable, "-c", "import sys; sys.stderr.write('boom\\n')"])
        assert (code, lines) == (0, ["boom"])

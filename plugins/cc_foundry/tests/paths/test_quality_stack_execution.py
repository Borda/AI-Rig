"""Exercise quality-stack shell calls with controlled tool exits and session state."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from _hook_env import _bash_runs_posix_script

_STACK = Path(__file__).resolve().parents[2] / "skills/_shared/quality-stack.md"
_SHELLS = [
    pytest.param("bash", marks=pytest.mark.skipif(not _bash_runs_posix_script(), reason="Bash unavailable"), id="bash"),
    pytest.param("zsh", marks=pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh unavailable"), id="zsh"),
]


def _fence(marker: str) -> str:
    """Read the executable fence containing the specified operation."""
    body: list[str] | None = None
    for line in _STACK.read_text(encoding="utf-8").splitlines():
        if body is None and line == "```bash":
            body = []
        elif body is not None and line == "```":
            if any(marker in entry for entry in body):
                return "\n".join(body)
            body = None
        elif body is not None:
            body.append(line)
    raise AssertionError(f"Missing shell operation: {marker}")


@pytest.fixture
def session(tmp_path: Path) -> dict[str, str]:
    """Prepare isolated sentinels and a tool double that records exact argument boundaries."""
    values = {
        "runner": "python -m",
        "skip-ruff": "1",
        "skip-mypy": "0",
        "precommit": "0",
        "test-dir": "test directory",
        "target-modules": "source module.py",
        "changed-files": "changed file.py",
        "xdist-args": "",
        "doctest-merged": "false",
        "module": "package.module",
    }
    for name, value in values.items():
        (tmp_path / f"quality-stack-{name}-execution").write_text(value + "\n", encoding="utf-8")
    fake = tmp_path / "fake.py"
    fake.write_text(
        "import json, os, signal, sys\n"
        "from pathlib import Path\n"
        "p = Path(os.environ['TMPDIR']) / 'calls.jsonl'\n"
        "calls = p.read_text().splitlines() if p.exists() else []\n"
        "with p.open('a') as f: f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if 'mypy' in sys.argv:\n"
        "    signal.signal(signal.SIGPIPE, signal.SIG_DFL)\n"
        "    print('type error\\n' * 20000)\n"
        "    sys.exit(2)\n"
        "code = int(os.environ.get('FAKE_EXITS', '0').split(',')[len(calls)])\n"
        "if code: print(os.environ.get('FAKE_FAILURE', 'FAILED tests/test_sample.py::test_case[with spaces] - AssertionError'))\n"
        "sys.exit(code)\n",
        encoding="utf-8",
    )
    launcher = tmp_path / "python"
    launcher.write_text(
        f'#!/bin/bash\nexec {shlex.quote(sys.executable)} {shlex.quote(str(fake))} "$@"\n', encoding="utf-8"
    )
    launcher.chmod(0o755)
    return {
        **os.environ,
        "TMPDIR": str(tmp_path),
        "CLAUDE_CODE_SESSION_ID": "execution",
        "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
    }


def _run(shell: str, marker: str, session: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run one real shipped fence as a fresh shell invocation."""
    return subprocess.run(
        [shell, "-c", _fence(marker)], env=session, cwd=session["TMPDIR"], text=True, capture_output=True, timeout=15
    )


@pytest.mark.parametrize("shell", _SHELLS)
def test_mypy_preserves_exit_when_output_exceeds_head(shell: str, session: dict[str, str]) -> None:
    """Truncating displayed mypy output must preserve the type-checker's exit code."""
    result = _run(shell, "MYPY_EXIT=skipped", session)
    assert result.returncode == 0, result.stderr
    assert "MYPY_EXIT=2" in result.stdout
    assert result.stdout.count("type error") == 30
    assert json.loads((Path(session["TMPDIR"]) / "calls.jsonl").read_text().splitlines()[0]) == [
        "-m",
        "mypy",
        "changed file.py",
        "--no-error-summary",
    ]


@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize(
    ("marker", "sentinel", "value"),
    [
        pytest.param("MYPY_EXIT=skipped", "changed-files", "<changed_files>", id="lint-placeholder"),
        pytest.param("DOCTEST_MERGED=false", "target-modules", "<target_module>", id="collect-placeholder"),
        pytest.param("SUITE_EXIT=$?", "test-dir", "<test_dir>", id="wide-placeholder"),
        pytest.param(
            'if [ "$DOCTEST_MERGED" != "true" ]; then', "target-modules", "<module>", id="doctest-placeholder"
        ),
        pytest.param("SUITE_EXIT=$?", "test-dir", "", id="missing-test-directory"),
    ],
)
def test_unresolved_targets_block_before_tools(
    shell: str, marker: str, sentinel: str, value: str, session: dict[str, str]
) -> None:
    """Missing or template targets must halt before any tool or misleading failure verdict."""
    root = Path(session["TMPDIR"])
    (root / f"quality-stack-{sentinel}-execution").write_text(value + "\n", encoding="utf-8")
    result = _run(shell, marker, session)
    assert result.returncode == 1, result.stderr
    assert "! BLOCKED" in result.stdout
    assert "GENUINE FAILURE" not in result.stdout
    assert not (root / "calls.jsonl").exists()


@pytest.mark.parametrize("shell", _SHELLS)
def test_codemap_unresolved_module_blocks_before_query(shell: str, session: dict[str, str]) -> None:
    """An available optional codemap check must reject its unresolved module operand."""
    root = Path(session["TMPDIR"])
    (root / "quality-stack-module-execution").write_text("<module>\n", encoding="utf-8")
    shutil.copyfile(root / "python", root / "codemap-py")
    (root / "codemap-py").chmod(0o755)
    index = root / "index"
    index.mkdir()
    (index / f"{root.name}.json").write_text("{}", encoding="utf-8")
    session["CODEMAP_INDEX_DIR"] = str(index)
    result = _run(shell, "codemap-py query rdeps", session)
    assert result.returncode == 1
    assert "! BLOCKED" in result.stdout
    assert not (root / "calls.jsonl").exists()


@pytest.mark.parametrize("shell", _SHELLS)
def test_unavailable_codemap_check_needs_no_module_target(shell: str, session: dict[str, str]) -> None:
    """A missing codemap index must retain the optional check's no-op behavior."""
    root = Path(session["TMPDIR"])
    (root / "quality-stack-module-execution").unlink()
    session["CODEMAP_INDEX_DIR"] = str(root / "missing-index")
    result = _run(shell, "codemap-py query rdeps", session)
    assert result.returncode == 0
    assert "! BLOCKED" not in result.stdout
    assert not (root / "calls.jsonl").exists()


@pytest.mark.parametrize("shell", _SHELLS)
def test_failed_suite_returns_before_retries(shell: str, session: dict[str, str]) -> None:
    """A wide run must finish its shell call before either independently budgeted retry."""
    session["FAKE_EXITS"] = "1,0,0"
    result = _run(shell, "SUITE_EXIT=$?", session)
    assert result.returncode == 0, result.stderr
    root = Path(session["TMPDIR"])
    assert len((root / "calls.jsonl").read_text().splitlines()) == 1
    assert (root / "quality-stack-suite-exit-execution").read_text().strip() == "1"
    assert (root / "quality-stack-failed-ids-execution").read_text().strip() == (
        "tests/test_sample.py::test_case[with spaces]"
    )


@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize("xdist", ["", "-n auto"])
def test_retries_restore_state_across_shell_calls(shell: str, xdist: str, session: dict[str, str]) -> None:
    """Serial retry calls must restore exact IDs and retain the flaky versus isolation decision."""
    root = Path(session["TMPDIR"])
    (root / "quality-stack-xdist-args-execution").write_text(xdist + "\n", encoding="utf-8")
    session["FAKE_EXITS"] = "1,0,0"
    wide = _run(shell, "SUITE_EXIT=$?", session)
    retry_a = _run(shell, "# Retry A: exactly", session)
    retry_b = _run(shell, "# Retry B: full-dir", session)
    assert wide.returncode == retry_a.returncode == 0
    calls = [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()]
    assert len(calls) == 3
    assert calls[0][2] == "test directory"
    assert calls[1] == ["-m", "pytest", "--tb=short", "-v", "tests/test_sample.py::test_case[with spaces]"]
    assert calls[2] == ["-m", "pytest", "test directory", "-v", "--tb=short"]
    assert retry_b.returncode == (1 if xdist else 0)
    assert ("PARALLEL_FAIL_SERIAL_PASS" in retry_b.stdout) == bool(xdist)
    assert (root / "quality-stack-flaky-detected-execution").read_text().strip() == ("false" if xdist else "true")


@pytest.mark.parametrize("shell", _SHELLS)
def test_green_suite_does_not_retry(shell: str, session: dict[str, str]) -> None:
    """A passing suite must skip both retry commands even though each fence is invoked."""
    wide = _run(shell, "SUITE_EXIT=$?", session)
    retry_a = _run(shell, "# Retry A: exactly", session)
    retry_b = _run(shell, "# Retry B: full-dir", session)
    assert wide.returncode == retry_a.returncode == retry_b.returncode == 0
    assert len((Path(session["TMPDIR"]) / "calls.jsonl").read_text().splitlines()) == 1


@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize(
    ("exits", "message", "count"),
    [
        pytest.param("1,1", "failing node-id(s) fail again on isolated re-run", 2, id="isolated-failure"),
        pytest.param("1,0,1", "order-dependent", 3, id="order-dependent-failure"),
    ],
)
def test_retry_failures_halt_without_flaky_verdict(
    shell: str, exits: str, message: str, count: int, session: dict[str, str]
) -> None:
    """Either failing retry must retain a genuine failure and prohibit a flaky verdict."""
    session["FAKE_EXITS"] = exits
    wide = _run(shell, "SUITE_EXIT=$?", session)
    retry_a = _run(shell, "# Retry A: exactly", session)
    retry_b = _run(shell, "# Retry B: full-dir", session)
    assert wide.returncode == 0
    assert retry_b.returncode == 1
    assert f"GENUINE FAILURE: {message}" in (retry_a.stdout + retry_b.stdout)
    root = Path(session["TMPDIR"])
    assert len((root / "calls.jsonl").read_text().splitlines()) == count
    assert (root / "quality-stack-flaky-detected-execution").read_text().strip() == "false"


@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize("marker", ["# Retry A: exactly", "# Retry B: full-dir"])
def test_interrupted_suite_cannot_reuse_prior_failure_ids(shell: str, marker: str, session: dict[str, str]) -> None:
    """An unfinished suite must block retries even with stale IDs from an earlier attempt."""
    root = Path(session["TMPDIR"])
    (root / "quality-stack-suite-exit-execution").write_text("pending\n", encoding="utf-8")
    (root / "quality-stack-failed-ids-execution").write_text("tests/test_old.py::test_old\n", encoding="utf-8")
    result = _run(shell, marker, session)
    assert result.returncode == 1
    assert "! BLOCKED" in result.stdout
    assert not (root / "calls.jsonl").exists()


@pytest.mark.parametrize("shell", _SHELLS)
def test_failed_collection_keeps_separate_doctest_pass(shell: str, session: dict[str, str]) -> None:
    """Collection errors must retain the unmerged flag and the separate doctest execution."""
    session["FAKE_EXITS"] = "2,0"
    collection = _run(shell, "DOCTEST_MERGED=false", session)
    separate = _run(shell, 'if [ "$DOCTEST_MERGED" != "true" ]; then', session)
    root = Path(session["TMPDIR"])
    assert collection.returncode == separate.returncode == 0
    assert (root / "quality-stack-doctest-merged-execution").read_text().strip() == "false"
    calls = [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()]
    assert calls == [
        ["-m", "pytest", "test directory", "--doctest-modules", "source module.py", "--collect-only", "-q"],
        ["-m", "pytest", "--doctest-modules", "source module.py", "-v"],
    ]


@pytest.mark.parametrize("shell", _SHELLS)
def test_doctest_failure_never_authorizes_retry(shell: str, session: dict[str, str]) -> None:
    """Deterministic doctest failures must halt before writing a retry-ready suite state."""
    session["FAKE_EXITS"] = "1"
    session["FAKE_FAILURE"] = "FAILED source_module.py::source_module - AssertionError"
    wide = _run(shell, "SUITE_EXIT=$?", session)
    retry = _run(shell, "# Retry A: exactly", session)
    assert wide.returncode == retry.returncode == 1
    assert "GENUINE FAILURE: doctest assertion failed" in wide.stdout
    assert "! BLOCKED" in retry.stdout
    assert len((Path(session["TMPDIR"]) / "calls.jsonl").read_text().splitlines()) == 1


@pytest.mark.integration
@pytest.mark.parametrize("shell", _SHELLS)
def test_real_pytest_failure_survives_separate_retry_calls(shell: str, session: dict[str, str]) -> None:
    """Real pytest output must carry a transient failure through isolated and directory retries."""
    root = Path(session["TMPDIR"])
    session["PYTEST_ADDOPTS"] = "-p no:xdist"
    (root / "python").write_text(f'#!/bin/bash\nexec {shlex.quote(sys.executable)} "$@"\n', encoding="utf-8")
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_sample.py").write_text(
        "from pathlib import Path\n"
        "def test_once():\n"
        "    seen = Path('seen')\n"
        "    first = not seen.exists()\n"
        "    seen.touch()\n"
        "    assert not first\n",
        encoding="utf-8",
    )
    (root / "quality-stack-test-dir-execution").write_text("tests\n", encoding="utf-8")
    wide = _run(shell, "SUITE_EXIT=$?", session)
    retry_a = _run(shell, "# Retry A: exactly", session)
    retry_b = _run(shell, "# Retry B: full-dir", session)
    assert wide.returncode == retry_a.returncode == retry_b.returncode == 0, (
        wide.stdout + wide.stderr + retry_a.stdout + retry_a.stderr + retry_b.stdout + retry_b.stderr
    )
    assert "SUITE_EXIT=1" in wide.stdout
    assert "1 passed" in retry_a.stdout
    assert "1 passed" in retry_b.stdout
    assert (root / "quality-stack-failed-ids-execution").read_text().strip() == "tests/test_sample.py::test_once"
    assert (root / "quality-stack-flaky-detected-execution").read_text().strip() == "true"

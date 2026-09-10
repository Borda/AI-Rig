"""Regression tests for the isolated real-issue patch-task runtime."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

try:
    from _bench_common.edit_patch_contracts import assess_patch_answer, build_edit_task_contract
    from _bench_common import mutation_isolation
    from _bench_common.mutation_isolation import create_patch_task_agent_workspace, execute_patch_task_answer
except ModuleNotFoundError:
    from benchmarks._bench_common.edit_patch_contracts import assess_patch_answer, build_edit_task_contract
    from benchmarks._bench_common import mutation_isolation
    from benchmarks._bench_common.mutation_isolation import create_patch_task_agent_workspace, execute_patch_task_answer


def _shell_launcher_available() -> bool:
    """Probe whether this host can execute the shell-wrapper fixture directly."""
    shell = shutil.which("sh")
    if shell is None:
        return False
    try:
        with tempfile.TemporaryDirectory() as scratch:
            launcher = Path(scratch) / "launcher"
            launcher.write_bytes(f"#!{shell}\nexit 0\n".encode())
            launcher.chmod(0o755)
            return subprocess.run([str(launcher)], capture_output=True, timeout=10, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


POSIX_SHELL_LAUNCHER_AVAILABLE = _shell_launcher_available()


def _git(repo: Path, *args: str) -> str:
    """Run one local Git command for a disposable test repository."""
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _patch_task(repo: Path) -> dict[str, object]:
    """Return a frozen fixture whose target test is absent from the baseline."""
    return {
        "id": "PT-runtime",
        "type": "patch_task",
        "prompt": "Return a patch.",
        "pre_fix_commit": _git(repo, "rev-parse", "HEAD"),
        "test_fixture_patch": (
            "diff --git a/test_target.py b/test_target.py\n"
            "new file mode 100644\n"
            "index 0000000..2b4ec9d\n"
            "--- /dev/null\n"
            "+++ b/test_target.py\n"
            "@@ -0,0 +1,5 @@\n"
            "+from app import value\n"
            "+\n"
            "+\n"
            "+def test_target() -> None:\n"
            "+    assert value() == 'fixed'\n"
        ),
        "test_command": "pytest test_target.py -q",
        "gt_files_changed": ["src/app.py"],
        "regression_test_commands": ["pytest test_regression.py -q"],
        "scoreable": True,
    }


@pytest.fixture(name="patch_repo")
def _patch_repo(tmp_path: Path) -> Path:
    """Create a clean baseline with a failing staged target and a passing regression."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True, text=True)
    _git(repo, "config", "user.email", "bench@example.invalid")
    _git(repo, "config", "user.name", "Benchmark")
    (repo / "src").mkdir()
    (repo / "src/app.py").write_text("def value() -> str:\n    return 'broken'\n", encoding="utf-8")
    (repo / "test_regression.py").write_text(
        "from app import value\n\n\ndef test_regression() -> None:\n    assert isinstance(value(), str)\n",
        encoding="utf-8",
    )
    _git(repo, "add", "src/app.py", "test_regression.py")
    _git(repo, "commit", "-m", "baseline")
    return repo


def _answer() -> str:
    """Build a fenced candidate patch that changes only the application value, not its oracle.

    >>> lines = _answer().splitlines()
    >>> lines[0], lines[-1], [line for line in lines if line.startswith("+    ")]
    ('```diff', '```', ["+    return 'fixed'"])
    """
    return (
        "```diff\n"
        "diff --git a/src/app.py b/src/app.py\n"
        "index 90731cd..2f13d5a 100644\n"
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def value() -> str:\n"
        "-    return 'broken'\n"
        "+    return 'fixed'\n"
        "```"
    )


def _already_passing_fixture(task: dict[str, object]) -> dict[str, object]:
    """Copy a task and weaken its oracle to accept the broken baseline without mutating the original.

    >>> task = {"test_fixture_patch": "assert value == 'fixed'"}
    >>> _already_passing_fixture(task)
    {'test_fixture_patch': "assert value == 'broken'"}
    >>> task
    {'test_fixture_patch': "assert value == 'fixed'"}
    """
    task = dict(task)
    task["test_fixture_patch"] = str(task["test_fixture_patch"]).replace("'fixed'", "'broken'")
    return task


def test_patch_task_runtime_requires_staged_fixture_and_clean_candidate(patch_repo: Path) -> None:
    """A valid patch passes only after the fixture makes baseline failure observable."""
    contract = build_edit_task_contract(_patch_task(patch_repo))

    execution = execute_patch_task_answer(patch_repo, contract, assess_patch_answer(_answer()))

    assert execution.baseline_target_failed is True, execution.command_evidence
    assert execution.baseline_regressions_passed is True
    assert execution.patch_applied is True, execution.error
    assert execution.targeted_test_passed is True
    assert execution.regression_test_passed is True
    assert execution.changed_paths == ("src/app.py",)
    assert execution.cleanup_verified is True
    assert execution.source_integrity is True


def test_patch_task_runtime_rejects_an_already_passing_baseline(patch_repo: Path) -> None:
    """A target test that does not fail at the frozen baseline is inadmissible evidence."""
    contract = build_edit_task_contract(_already_passing_fixture(_patch_task(patch_repo)))

    execution = execute_patch_task_answer(patch_repo, contract, assess_patch_answer(_answer()))

    assert execution.baseline_target_failed is False
    assert execution.patch_applied is False
    assert execution.cleanup_verified is True
    assert execution.source_integrity is True


def test_patch_agent_workspace_rejects_a_clean_source_head_switch(patch_repo: Path) -> None:
    """Patch cells retain the source identity observed before the agent's writable turn.

    Regression: a clean checkout after an agent changed ``HEAD`` was accepted
    because only porcelain status was checked for historical Patch tasks.
    """
    contract = build_edit_task_contract(_patch_task(patch_repo))
    index = patch_repo / ".cache" / "codemap" / "patch" / "PT-runtime.json"
    index.parent.mkdir(parents=True)
    index.write_text(json.dumps({"scan_root": str(patch_repo)}), encoding="utf-8")
    _git(patch_repo, "add", ".cache/codemap/patch/PT-runtime.json")
    _git(patch_repo, "commit", "-m", "freeze-index")
    workspace = create_patch_task_agent_workspace(patch_repo, index, contract)
    try:
        _git(patch_repo, "commit", "--allow-empty", "-m", "clean-head-switch")

        assert workspace.source_unchanged() is False
    finally:
        workspace.workspace.cleanup()


def _ambient_pytest_launcher() -> str:
    """Prefer the explicit benchmark pytest launcher, falling back to executable discovery on PATH.

    >>> with pytest.MonkeyPatch.context() as patch:
    ...     patch.setenv(mutation_isolation.PATCH_PYTEST_ENV, "fixture-pytest")
    ...     _ambient_pytest_launcher()
    'fixture-pytest'
    """
    launcher = os.environ.get(mutation_isolation.PATCH_PYTEST_ENV) or shutil.which("pytest")
    if launcher is None:
        pytest.fail("the benchmark test environment must provide a pytest launcher on PATH")
    return launcher


def test_patch_test_runtime_binds_the_ambient_launcher_whatever_its_file_format() -> None:
    """The admitted launcher reports its own runtime instead of being parsed for a shebang.

    Regression: the identity was read out of the launcher's first line, which exists
    only for a POSIX text console script. The same entry point installs as a binary
    trampoline on Windows, where that read failed with ``UnicodeDecodeError`` and took
    every Patch scope down with ``Patch task pytest runtime is unavailable``.
    """
    identity = mutation_isolation.patch_test_runtime_identity()

    assert identity["pytest_executable"] == str(Path(_ambient_pytest_launcher()).absolute())
    assert identity["invocation"] == "absolute pytest executable"
    assert Path(identity["python_executable"]).is_file()
    assert identity == mutation_isolation.patch_test_runtime_identity()


@pytest.mark.skipif(not POSIX_SHELL_LAUNCHER_AVAILABLE, reason="a POSIX shell launcher is not executable on this host")
def test_patch_test_runtime_accepts_a_launcher_without_a_python_shebang(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A launcher whose header is not a Python shebang still yields a bound runtime.

    A shell wrapper stands in for a binary trampoline: neither header names a Python
    interpreter. Probe actual execution capability; the ambient-launcher test also
    covers native Windows trampolines. Outer pytest arguments must not contaminate
    the isolated runtime probe.
    """
    shell = shutil.which("sh")
    assert shell is not None
    ambient_identity = mutation_isolation.patch_test_runtime_identity()
    launcher = tmp_path / "pytest-wrapper"
    launcher.write_bytes(f'#!{shell}\nexec "{_ambient_pytest_launcher()}" "$@"\n'.encode())
    launcher.chmod(0o755)
    monkeypatch.setenv(mutation_isolation.PATCH_PYTEST_ENV, str(launcher))
    monkeypatch.setenv("PYTEST_ADDOPTS", "--unrecognized-outer-test-option")

    identity = mutation_isolation.patch_test_runtime_identity()

    assert identity["pytest_executable"] == str(launcher)
    assert identity["python_executable"] == ambient_identity["python_executable"]
    assert identity["pytest_module_sha256"] == ambient_identity["pytest_module_sha256"]


def test_patch_test_command_prioritizes_worktree_without_hiding_environment_dependencies(
    patch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Patch tests use the frozen source while retaining dependencies from the active benchmark environment."""
    captured: dict[str, object] = {}
    pytest_launcher = Path(_ambient_pytest_launcher()).absolute()
    runtime = mutation_isolation.patch_test_runtime_identity()

    def _fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Capture the test environment without starting pytest."""
        captured["argv"] = argv
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "passed", "")

    monkeypatch.setenv("PYTHONPATH", "/host/editable/source")
    monkeypatch.delenv("PYTHONNOUSERSITE", raising=False)
    monkeypatch.setattr(mutation_isolation, "patch_test_runtime_identity", lambda: runtime)
    monkeypatch.setattr(mutation_isolation.subprocess, "run", _fake_run)

    result = mutation_isolation._run_test_command(
        patch_repo,
        "pytest test_target.py -q",
        "target",
        runtime_identity=runtime,
    )

    environment = captured["env"]
    assert isinstance(environment, dict)
    assert environment["PYTHONPATH"] == str(patch_repo / "src")
    assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD" not in environment
    assert "PYTHONNOUSERSITE" not in environment
    assert runtime["pytest_executable"] == str(pytest_launcher)
    assert captured["argv"] == [str(pytest_launcher), "test_target.py", "-q"]
    assert result["returncode"] == 0
    assert result["output_excerpt"] == "stdout:\npassed\nstderr:\n"

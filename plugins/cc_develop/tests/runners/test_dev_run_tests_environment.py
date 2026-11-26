"""Verify receipt-based pytest retries preserve the test runner's validation and verdict."""

import json
import os
import subprocess
import sys
import sysconfig
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import dev_env_provision
import dev_run_dir
import dev_run_tests
import dev_test_targets
import pytest
import run_pytest_short


@pytest.fixture
def ready_environment(tmp_path, monkeypatch):
    """Create a private, provisioner-shaped interpreter and caller-owned receipt."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "environment-runner")
    root = dev_env_provision.environment_root()
    bucket = root / ("a" * 64)
    root.mkdir(mode=0o700)
    bucket.mkdir(mode=0o700)
    python = dev_env_provision._env_python(bucket / "venv")
    python.parent.mkdir(parents=True)
    python.touch()
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps({"status": "partial", "env_python": str(python)}), encoding="utf-8")
    return receipt, python


@pytest.mark.parametrize("command", ["pytest", "python -m pytest"])
def test_receipt_replaces_only_pytest_launcher(ready_environment, monkeypatch, command):
    """Preserve literal target argv and record the original session nonce on a real pytest-style failure."""
    receipt, python = ready_environment
    dev_run_dir.write_sentinel("dev-pytest-cmd", command)
    monkeypatch.setenv("DEV_TESTS_VERDICT_NONCE", "retry-nonce")
    calls = []

    def run(argv):
        """Capture the exact executed argv and return a real failure-summary shape."""
        calls.append(argv)
        return 1, ["============================== 1 failed in 0.01s =============================="]

    monkeypatch.setattr(dev_run_tests, "_run_stream", run)
    target = "tests/test file.py::test_case"
    assert dev_run_tests.main(["pytest", "--env", str(receipt), "--", "--tb=short", target, "-v"]) == 1
    assert calls == [[str(python), "-m", "pytest", "--tb=short", target, "-v"]]
    assert dev_run_dir.read_sentinel("dev-tests-verdict") == "failed retry-nonce"


def test_receipt_refuses_interpreter_outside_root(ready_environment, monkeypatch):
    """A receipt outside the provisioner root cannot launch any interpreter or preserve a stale verdict."""
    receipt, _ = ready_environment
    python = receipt.parent / "bin" / "python"
    python.parent.mkdir()
    python.touch()
    receipt.write_text(json.dumps({"status": "ready", "env_python": str(python)}), encoding="utf-8")
    dev_run_dir.write_sentinel("dev-pytest-cmd", "pytest")
    dev_run_dir.write_sentinel("dev-tests-verdict", "failed stale")
    monkeypatch.setattr(dev_run_tests, "_run_stream", lambda argv: pytest.fail(f"unexpected launch: {argv}"))
    assert dev_run_tests.main(["pytest", "--env", str(receipt)]) == 64
    assert dev_run_dir.read_sentinel("dev-tests-verdict") == "none"


@pytest.mark.parametrize(
    ("mode", "sentinel", "command"),
    [
        pytest.param("suite", "dev-test-cmd", "make test", id="non-pytest-suite"),
        pytest.param("pytest", "dev-pytest-cmd", "uv run pytest", id="managed-launcher"),
    ],
)
def test_receipt_cannot_change_other_allowlisted_commands(ready_environment, monkeypatch, mode, sentinel, command):
    """Refuse to replace an allowlisted command outside the targeted pytest recovery contract."""
    receipt, _ = ready_environment
    dev_run_dir.write_sentinel(sentinel, command)
    monkeypatch.setattr(dev_run_tests, "_run_stream", lambda argv: pytest.fail(f"unexpected launch: {argv}"))
    assert dev_run_tests.main([mode, "--env", str(receipt)]) == 64


@pytest.fixture
def short_launch(monkeypatch):
    """Observe the child argv without installing packages or starting a subprocess."""
    child = SimpleNamespace(stdout=StringIO("pytest output\n"), wait=Mock(return_value=0))
    launch = Mock(return_value=child)
    monkeypatch.setattr(run_pytest_short.subprocess, "Popen", launch)
    return launch


@pytest.mark.parametrize("command", ["pytest", "python -m pytest"])
def test_short_runner_uses_receipt_interpreter(ready_environment, short_launch, monkeypatch, command):
    """Launch the provisioned Python directly and preserve the literal short-runner arguments."""
    receipt, python = ready_environment
    monkeypatch.setattr(run_pytest_short, "which", Mock(side_effect=AssertionError("unexpected PATH lookup")))
    target = "tests/test file.py::test_case"
    assert run_pytest_short.main([command, target, "--env", str(receipt)]) == 0
    assert short_launch.call_args.args[0] == [str(python), "-m", "pytest", "--tb=short", target, "-v"]


@pytest.mark.parametrize(
    "command", ["pytest; other", "uv run pytest", "poetry run pytest", "poetry run python -m pytest"]
)
def test_short_runner_receipt_refuses_indirect_and_unsafe_commands(ready_environment, short_launch, command):
    """Keep the allowlist and refuse substitutions through managed runners."""
    receipt, _ = ready_environment
    assert run_pytest_short.main([command, "tests", "--env", str(receipt)]) == 2
    short_launch.assert_not_called()


def test_short_runner_receipt_refuses_an_external_interpreter(ready_environment, short_launch):
    """Reject even an existing interpreter in a provisioner-shaped bucket outside the private root."""
    receipt, _ = ready_environment
    python = dev_env_provision._env_python(receipt.parent / "outside" / ("b" * 64) / "venv")
    python.parent.mkdir(parents=True)
    python.touch()
    receipt.write_text(json.dumps({"status": "ready", "env_python": str(python)}), encoding="utf-8")
    assert run_pytest_short.main(["pytest", "tests", "--env", str(receipt)]) == 2
    short_launch.assert_not_called()


def test_short_runner_without_receipt_keeps_managed_argv(ready_environment, short_launch, monkeypatch):
    """Keep PATH resolution and the original argv for an ordinary managed runner."""
    executable = str(ready_environment[0].parent / "poetry")
    resolve = Mock(return_value=executable)
    monkeypatch.setattr(run_pytest_short, "which", resolve)
    target = "tests/test file.py::test_case"
    assert run_pytest_short.main(["poetry run python -m pytest", target]) == 0
    resolve.assert_called_once_with("poetry")
    assert short_launch.call_args.args[0] == [executable, "run", "python", "-m", "pytest", "--tb=short", target, "-v"]


def test_selector_refuses_managed_retry_before_selection(ready_environment, monkeypatch):
    """A managed launcher is ineligible even if selection would yield no tests."""
    receipt, _ = ready_environment
    repo_root = Mock()
    monkeypatch.setattr(dev_test_targets, "_repo_root", repo_root)
    assert dev_test_targets.main(["--pytest-cmd", "uv run pytest", "--run", "--env", str(receipt)]) == 2
    repo_root.assert_not_called()


@pytest.mark.parametrize(
    ("child_exit", "output", "expected_exit", "expected_check"),
    [
        pytest.param(0, "1 passed in 0.01s\n", 0, 0, id="green-baseline"),
        pytest.param(1, "1 failed in 0.01s\n", 1, 0, id="red-baseline"),
        pytest.param(1, "python: No module named pytest\n", 2, 2, id="incomplete-startup"),
    ],
)
def test_selector_receipt_records_baseline_evidence(
    ready_environment, short_launch, monkeypatch, child_exit, output, expected_exit, expected_check
):
    """Let the baseline gate accept completed retries and reject an interpreter that never started pytest."""
    receipt, python = ready_environment
    project = receipt.parent
    monkeypatch.setattr(dev_test_targets, "_repo_root", lambda: project)
    clock = Mock(wraps=dev_test_targets.time)
    clock.gmtime.return_value = dev_test_targets.time.gmtime(0)
    monkeypatch.setattr(dev_test_targets, "time", clock)
    default_pytest = str(project / "pytest")
    monkeypatch.setattr(run_pytest_short, "which", Mock(return_value=default_pytest))
    record_dir = project / ".developments"
    target = "tests/test file.py::test_case"
    selection = project / "baseline-tests.json"
    selection.write_text(json.dumps([target]), encoding="utf-8")
    args = ["--skill", "refactor", "--tests-from", str(selection), "--record-dir", str(record_dir), "--run"]
    dev_run_dir.write_sentinel("dev-pytest-cmd", "pytest")

    missing_dependency = "ModuleNotFoundError: No module named 'declared_package'\n"
    short_launch.return_value.stdout = StringIO(missing_dependency)
    short_launch.return_value.wait.return_value = 2
    assert dev_test_targets.main(args) == 2
    assert short_launch.call_args.args[0] == [default_pytest, "--tb=short", target, "-v"]

    short_launch.reset_mock()
    short_launch.return_value.stdout = StringIO(output)
    short_launch.return_value.wait.return_value = child_exit
    assert dev_test_targets.main([*args, "--env", str(receipt)]) == expected_exit
    short_launch.assert_called_once()
    assert short_launch.call_args.args[0] == [str(python), "-m", "pytest", "--tb=short", target, "-v"]
    ledger = record_dir / "test-targets.jsonl"
    original, record = map(json.loads, ledger.read_text(encoding="utf-8").splitlines())
    assert "env_receipt" not in original
    assert Path(original["log"]).read_text(encoding="utf-8") == missing_dependency
    assert record["tests"] == [target]
    assert record["exit"] == expected_exit
    assert record["env_receipt"] == receipt.as_posix()
    assert Path(record["log"]).read_text(encoding="utf-8") == output
    assert dev_test_targets.main(["--check-record", str(ledger), "--expect-exit", str(child_exit)]) == expected_check


@pytest.fixture
def src_layout_retry(tmp_path, monkeypatch):
    """Create a src-layout project and a real provisioned-shaped interpreter that lacks the project itself.

    The receipt interpreter is a real virtual environment without the project installed; ``PYTHONPATH`` names this test
    run's own site-packages so that interpreter can import pytest, as a provisioned environment would.
    """
    project = tmp_path / "project"
    (project / "src" / "demo_pkg").mkdir(parents=True)
    (project / "src" / "demo_pkg" / "__init__.py").write_text("VALUE = 7\n", encoding="utf-8")
    (project / "tests").mkdir()
    (project / "tests" / "test_demo.py").write_text(
        "import demo_pkg\n\n\ndef test_value():\n    assert demo_pkg.VALUE == 7\n", encoding="utf-8"
    )
    monkeypatch.chdir(project)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "environment-runner")
    monkeypatch.setenv("PYTHONPATH", sysconfig.get_paths()["purelib"])
    root = dev_env_provision.environment_root()
    bucket = root / ("c" * 64)
    root.mkdir(mode=0o700)
    bucket.mkdir(mode=0o700)
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", "--copies", str(bucket / "venv")], check=True, timeout=300
    )
    receipt = tmp_path / "receipt.json"
    python = dev_env_provision._env_python(bucket / "venv")
    receipt.write_text(json.dumps({"status": "ready", "env_python": str(python)}), encoding="utf-8")
    return receipt


_COPIED_VENV_NEEDS_PY311 = pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="a copied venv interpreter of a uv-managed Python 3.10 cannot find its standard library (CPython 3.10 getpath)",
)


@_COPIED_VENV_NEEDS_PY311
@pytest.mark.integration
def test_short_runner_retry_imports_a_src_layout_project(src_layout_retry, capsys):
    """The provisioned retry imports the uninstalled project from ``src``, so only the missing package changes."""
    assert run_pytest_short.main(["pytest", "tests/test_demo.py", "--env", str(src_layout_retry)]) == 0
    assert "1 passed" in capsys.readouterr().out


@pytest.mark.integration
def test_short_retry_refuses_unrepresentable_checkout_path(src_layout_retry, tmp_path, monkeypatch, capsys):
    """Refuse to launch when the checkout path would split into multiple Python search roots."""
    original = Path.cwd()
    project = tmp_path / f"project{os.pathsep}checkout"
    monkeypatch.chdir(tmp_path)
    original.rename(project)
    monkeypatch.chdir(project)
    assert run_pytest_short.main(["pytest", "tests/test_demo.py", "--env", str(src_layout_retry)]) == 2
    assert "source path contains the PYTHONPATH separator" in capsys.readouterr().err


@_COPIED_VENV_NEEDS_PY311
@pytest.mark.integration
def test_direct_runner_retry_imports_a_src_layout_project(src_layout_retry, capsys):
    """The direct runner's provisioned retry imports the project from ``src`` and leaves the caller's path as it was."""
    dev_run_dir.write_sentinel("dev-pytest-cmd", "python -m pytest")
    before = os.environ["PYTHONPATH"]

    assert dev_run_tests.main(["pytest", "--env", str(src_layout_retry), "--", "tests/test_demo.py"]) == 0
    assert "1 passed" in capsys.readouterr().out
    assert os.environ["PYTHONPATH"] == before

"""Real-Git acceptance checks for optional gate source binding."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
RUN_GATES = PLUGIN_ROOT / "shared" / "run_gates.py"
GATE_IDS = ("lint", "format", "types", "tests", "review")
CODEX_TRAILER = "Co-authored-by: Codex <codex@openai.com>"


def _git(repository: Path, *arguments: str) -> str:
    """Run one successful local Git command and return its standard output."""
    completed = subprocess.run(["git", *arguments], cwd=repository, capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def _initialize_repository(repository: Path) -> str:
    """Create a clean committed repository whose commits retain Codex attribution."""
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "Codex Rig Test")
    _git(repository, "config", "user.email", "codex-rig@example.invalid")
    (repository / "tracked.txt").write_text("base\n", encoding="utf-8", newline="\n")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "-m", f"fixture\n\n{CODEX_TRAILER}")
    return _git(repository, "rev-parse", "HEAD")


def _python_command(source: str) -> str:
    """Return a native-shell command that executes portable encoded Python source."""
    encoded = base64.b64encode(source.encode("utf-8")).decode("ascii")
    if sys.platform == "win32":
        executable = "& '" + sys.executable.replace("'", "''") + "'"
    else:
        executable = shlex.quote(sys.executable)
    return f"{executable} -c \"import base64;exec(compile(base64.b64decode('{encoded}'), '<gate>', 'exec'))\""


def _run_lint_gate(
    repository: Path, output: Path, expected_head: str | None, command: str, worktree: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Run one executable lint check while declaring the other canonical gates unavailable."""
    arguments = [sys.executable, str(RUN_GATES), "--out", str(output), "--lint", command]
    if expected_head is not None:
        arguments.extend(("--expected-head", expected_head))
    if worktree is not None:
        arguments.extend(("--worktree", str(worktree)))
    for gate_id in GATE_IDS:
        if gate_id != "lint":
            arguments.extend((f"--skip-{gate_id}", "out of scope"))
    return subprocess.run(arguments, cwd=repository, capture_output=True, text=True, check=False)


def _lint_check(output: Path) -> dict[str, object]:
    """Load the generated lint record from a gate run."""
    payload = json.loads((output / "gates.json").read_text(encoding="utf-8"))
    return next(check for check in payload["checks"] if check["id"] == "lint")


def test_gate_uses_explicit_project_environment_in_isolated_worktree(tmp_path: Path) -> None:
    """Run a declared checker from the source environment against the selected review worktree."""
    repository = tmp_path / "repository"
    repository.mkdir()
    head = _initialize_repository(repository)
    environment = tmp_path / "project-env"
    environment.mkdir()
    (environment / "pyvenv.cfg").write_text("home = fixture\n", encoding="utf-8")
    scripts = environment / ("Scripts" if sys.platform == "win32" else "bin")
    scripts.mkdir(parents=True)
    (scripts / ("python.exe" if sys.platform == "win32" else "python")).write_bytes(b"placeholder")
    marker = tmp_path / "checker-cwd.txt"
    command = _python_command(
        f"import os; from pathlib import Path; "
        f"assert os.environ['PATH'].split(os.pathsep)[0] == {str(scripts)!r}; "
        f"Path({str(marker)!r}).write_text(str(Path.cwd()), encoding='utf-8')"
    )
    arguments = [
        sys.executable,
        str(RUN_GATES),
        "--out",
        str(tmp_path / "gates"),
        "--expected-head",
        head,
        "--worktree",
        str(repository),
        "--project-env",
        str(environment),
        "--types",
        command,
    ]
    for gate_id in ("lint", "format", "tests", "review"):
        arguments.extend((f"--skip-{gate_id}", "out of scope"))

    result = subprocess.run(arguments, cwd=tmp_path, capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    assert marker.read_text(encoding="utf-8") == str(repository)
    payload = json.loads((tmp_path / "gates" / "gates.json").read_text(encoding="utf-8"))
    assert next(check for check in payload["checks"] if check["id"] == "types")["status"] == "pass"
    assert payload["source"]["project_env"] == environment.as_posix()


def test_missing_project_environment_is_rejected_before_gates(tmp_path: Path) -> None:
    """Keep a missing checker environment as a prerequisite instead of silently using PATH."""
    repository = tmp_path / "repository"
    repository.mkdir()
    head = _initialize_repository(repository)
    output = tmp_path / "gates"

    result = subprocess.run(
        [
            sys.executable,
            str(RUN_GATES),
            "--out",
            str(output),
            "--expected-head",
            head,
            "--worktree",
            str(repository),
            "--project-env",
            str(tmp_path / "missing"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "invalid-project-env" in result.stderr
    assert not output.exists()


@pytest.mark.parametrize(
    ("executable", "expected_prefix"),
    [
        pytest.param(
            r"C:\Program Files\Python\python.exe",
            "& 'C:\\Program Files\\Python\\python.exe' -c ",
            id="powershell-spaced-executable",
        ),
        pytest.param(
            r"C:\Tools\Python's runtime\python.exe",
            "& 'C:\\Tools\\Python''s runtime\\python.exe' -c ",
            id="powershell-literal-apostrophe",
        ),
    ],
)
def test_python_gate_command_uses_powershell_invocation(
    monkeypatch: pytest.MonkeyPatch, executable: str, expected_prefix: str
) -> None:
    """Keep Windows test commands executable when interpreter paths need PowerShell quoting."""
    with monkeypatch.context() as context:
        context.setattr(sys, "platform", "win32")
        context.setattr(sys, "executable", executable)
        command = _python_command("pass")
    assert command.startswith(expected_prefix)
    assert "cGFzcw==" in command


@pytest.mark.parametrize(
    "expected_head",
    [pytest.param("0" * 40, id="sha1"), pytest.param("0" * 64, id="sha256")],
)
def test_wrong_expected_head_blocks_command_and_records_preblocked_receipt(tmp_path: Path, expected_head: str) -> None:
    """Reject a checkout mismatch before the configured command can create its side effect."""
    repository = tmp_path / "repository"
    repository.mkdir()
    actual_head = _initialize_repository(repository)
    output = tmp_path / "gates"
    marker = repository / "ran.txt"

    completed = _run_lint_gate(
        repository,
        output,
        expected_head,
        _python_command(f"from pathlib import Path; Path({str(marker)!r}).write_text('ran', encoding='utf-8')"),
    )

    assert completed.returncode == 1
    assert not marker.exists()
    check = _lint_check(output)
    assert check["status"] == "fail"
    assert check["source"] == {
        "expected_head": expected_head,
        "before": {"head": actual_head, "status": ""},
        "after": None,
    }
    assert "source-head-mismatch" in (output / str(check["stderr"])).read_text(encoding="utf-8")


@pytest.mark.parametrize("exit_code", [0, 1, 7])
def test_matching_head_runs_command_and_keeps_command_failure(tmp_path: Path, exit_code: int) -> None:
    """Execute on matching source and retain the native command's exact exit status."""
    repository = tmp_path / "repository"
    repository.mkdir()
    expected_head = _initialize_repository(repository)
    output = tmp_path / "gates"

    completed = _run_lint_gate(repository, output, expected_head, _python_command(f"raise SystemExit({exit_code})"))

    assert completed.returncode == (1 if exit_code else 0)
    check = _lint_check(output)
    assert check["status"] == ("fail" if exit_code else "pass")
    assert check["exit_code"] == exit_code
    assert check["source"] == {
        "expected_head": expected_head,
        "before": {"head": expected_head, "status": ""},
        "after": {"head": expected_head, "status": ""},
    }


@pytest.mark.parametrize(
    ("mutation", "expected_status"),
    [
        pytest.param("tracked.txt", " M tracked.txt", id="tracked"),
        pytest.param("untracked.txt", "?? untracked.txt", id="untracked"),
    ],
)
def test_dirty_source_blocks_command_before_execution(tmp_path: Path, mutation: str, expected_status: str) -> None:
    """Block tracked and untracked source changes before an executable gate starts."""
    repository = tmp_path / "repository"
    repository.mkdir()
    expected_head = _initialize_repository(repository)
    (repository / mutation).write_text("changed\n", encoding="utf-8", newline="\n")
    output = tmp_path / "gates"
    marker = repository / "ran.txt"

    completed = _run_lint_gate(
        repository,
        output,
        expected_head,
        _python_command(f"from pathlib import Path; Path({str(marker)!r}).write_text('ran', encoding='utf-8')"),
    )

    assert completed.returncode == 1
    assert not marker.exists()
    check = _lint_check(output)
    assert check["status"] == "fail"
    assert check["source"] == {
        "expected_head": expected_head,
        "before": {"head": expected_head, "status": expected_status},
        "after": None,
    }
    assert "source-dirty" in (output / str(check["stderr"])).read_text(encoding="utf-8")


def test_post_command_source_drift_fails_an_otherwise_passing_gate(tmp_path: Path) -> None:
    """Reject a command that changes HEAD even when it itself exits successfully."""
    repository = tmp_path / "repository"
    repository.mkdir()
    expected_head = _initialize_repository(repository)
    output = tmp_path / "gates"
    command = _python_command(
        "from pathlib import Path\n"
        "import subprocess\n"
        "Path('drift.txt').write_text('drift\\n', encoding='utf-8', newline='\\n')\n"
        "subprocess.run(['git', 'add', 'drift.txt'], check=True)\n"
        f"subprocess.run(['git', 'commit', '-m', 'drift\\n\\n{CODEX_TRAILER}'], check=True)\n"
    )

    completed = _run_lint_gate(repository, output, expected_head, command)

    actual_head = _git(repository, "rev-parse", "HEAD")
    assert completed.returncode == 1
    assert actual_head != expected_head
    check = _lint_check(output)
    assert check["status"] == "fail"
    assert check["exit_code"] == 0
    assert check["source"] == {
        "expected_head": expected_head,
        "before": {"head": expected_head, "status": ""},
        "after": {"head": actual_head, "status": ""},
    }
    assert "source-head-mismatch" in (output / str(check["stderr"])).read_text(encoding="utf-8")


def test_post_command_source_change_fails_an_otherwise_passing_gate(tmp_path: Path) -> None:
    """Reject a successful command that leaves an untracked source change behind."""
    repository = tmp_path / "repository"
    repository.mkdir()
    expected_head = _initialize_repository(repository)
    output = tmp_path / "gates"
    command = _python_command(
        "from pathlib import Path\nPath('during-gate.txt').write_text('changed\\n', encoding='utf-8', newline='\\n')"
    )

    completed = _run_lint_gate(repository, output, expected_head, command)

    assert completed.returncode == 1
    check = _lint_check(output)
    assert check["status"] == "fail"
    assert check["exit_code"] == 0
    assert check["source"] == {
        "expected_head": expected_head,
        "before": {"head": expected_head, "status": ""},
        "after": {"head": expected_head, "status": "?? during-gate.txt"},
    }
    assert "source-dirty" in (output / str(check["stderr"])).read_text(encoding="utf-8")


def test_expected_head_rejects_non_git_directory_without_executing_command(tmp_path: Path) -> None:
    """Treat unavailable Git provenance as a failed guard rather than a clean source."""
    output = tmp_path / "gates"
    marker = tmp_path / "ran.txt"

    completed = _run_lint_gate(
        tmp_path,
        output,
        "a" * 40,
        _python_command(f"from pathlib import Path; Path({str(marker)!r}).write_text('ran', encoding='utf-8')"),
    )

    assert completed.returncode == 1
    assert not marker.exists()
    check = _lint_check(output)
    source = check["source"]
    assert source["expected_head"] == "a" * 40
    assert source["before"]["head"] == ""
    assert source["before"]["status"] == ""
    assert source["before"]["error"]
    assert source["after"] is None
    assert "source-inspection-failed" in (output / str(check["stderr"])).read_text(encoding="utf-8")


def test_unflagged_gate_behavior_omits_source_receipts(tmp_path: Path) -> None:
    """Leave existing runs unchanged when callers do not request source binding."""
    output = tmp_path / "gates"

    completed = _run_lint_gate(tmp_path, output, None, _python_command("pass"))

    assert completed.returncode == 0
    check = _lint_check(output)
    assert check["status"] == "pass"
    assert "source" not in check


def test_selected_worktree_with_wrong_head_blocks_execution(tmp_path: Path) -> None:
    """Check the selected checkout, not the invocation checkout, before running commands."""
    original = tmp_path / "original"
    original.mkdir()
    expected_head = _initialize_repository(original)
    selected = tmp_path / "selected"
    selected.mkdir()
    _initialize_repository(selected)
    (selected / "tracked.txt").write_text("different\n", encoding="utf-8", newline="\n")
    _git(selected, "add", "tracked.txt")
    _git(selected, "commit", "-m", f"different\n\n{CODEX_TRAILER}")
    actual_head = _git(selected, "rev-parse", "HEAD")
    assert actual_head != expected_head
    output = tmp_path / "gates"
    marker = tmp_path / "ran.txt"

    completed = _run_lint_gate(
        original,
        output,
        expected_head,
        _python_command(f"from pathlib import Path; Path({str(marker)!r}).write_text('ran', encoding='utf-8')"),
        worktree=selected,
    )

    assert completed.returncode == 1
    assert not marker.exists()
    check = _lint_check(output)
    assert check["source"] == {
        "expected_head": expected_head,
        "worktree": selected.resolve().as_posix(),
        "before": {"head": actual_head, "status": ""},
        "after": None,
    }
    assert "source-head-mismatch" in (output / str(check["stderr"])).read_text(encoding="utf-8")


def test_same_head_different_worktree_retains_checkout_identity(tmp_path: Path) -> None:
    """Record the selected path even when another checkout has the same Git head."""
    original = tmp_path / "original"
    original.mkdir()
    expected_head = _initialize_repository(original)
    selected = tmp_path / "selected"
    _git(original, "worktree", "add", "--detach", str(selected), expected_head)
    output = tmp_path / "gates"

    completed = _run_lint_gate(original, output, expected_head, _python_command("pass"), worktree=selected)

    assert completed.returncode == 0
    payload = json.loads((output / "gates.json").read_text(encoding="utf-8"))
    assert payload["source"] == {"expected_head": expected_head, "worktree": selected.resolve().as_posix()}
    check = _lint_check(output)
    assert check["source"]["worktree"] == selected.resolve().as_posix()
    assert check["source"]["before"] == {"head": expected_head, "status": ""}
    assert check["source"]["after"] == {"head": expected_head, "status": ""}
    assert original.resolve().as_posix() != check["source"]["worktree"]


def test_selected_worktree_is_command_cwd(tmp_path: Path) -> None:
    """Launch commands inside the selected checkout without changing the parent process cwd."""
    original = tmp_path / "original"
    original.mkdir()
    expected_head = _initialize_repository(original)
    selected = tmp_path / "selected"
    _git(original, "worktree", "add", "--detach", str(selected), expected_head)
    output = tmp_path / "gates"
    command = _python_command("from pathlib import Path; print(Path.cwd().resolve().as_posix())")

    completed = _run_lint_gate(original, output, expected_head, command, worktree=selected)

    assert completed.returncode == 0
    check = _lint_check(output)
    assert (output / str(check["stdout"])).read_text(encoding="utf-8").strip() == selected.resolve().as_posix()
    assert not (original / "gates").exists()


def test_worktree_requires_expected_head_before_writing_receipts(tmp_path: Path) -> None:
    """Do not permit checkout selection without an exact source guard."""
    repository = tmp_path / "repository"
    repository.mkdir()
    _initialize_repository(repository)
    output = tmp_path / "gates"

    completed = _run_lint_gate(repository, output, None, _python_command("pass"), worktree=repository)

    assert completed.returncode == 2
    assert "invalid-worktree: --expected-head is required" in completed.stderr
    assert not output.exists()


def test_selected_worktree_must_be_checkout_root(tmp_path: Path) -> None:
    """Reject a repository subdirectory as a misleading checkout identity."""
    repository = tmp_path / "repository"
    repository.mkdir()
    expected_head = _initialize_repository(repository)
    nested = repository / "nested"
    nested.mkdir()
    output = tmp_path / "gates"
    marker = tmp_path / "ran.txt"

    completed = _run_lint_gate(
        repository,
        output,
        expected_head,
        _python_command(f"from pathlib import Path; Path({str(marker)!r}).write_text('ran', encoding='utf-8')"),
        worktree=nested,
    )

    assert completed.returncode == 1
    assert not marker.exists()
    check = _lint_check(output)
    assert check["source"]["worktree"] == nested.resolve().as_posix()
    assert check["source"]["before"]["error"] == "selected-worktree-is-not-checkout-root"


@dataclass
class _EditableCheckout:
    """A committed checkout paired with a detached worktree, wired for editable-install provenance checks."""

    original: Path
    selected: Path
    expected_head: str
    editable_path: Path
    environment: dict[str, str]

    def run_tests(
        self,
        output: Path,
        imported_module: str = "demo_package",
        pytest_arguments: list[str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run the structured test gate from the original checkout."""
        arguments = [
            sys.executable,
            str(RUN_GATES),
            "--out",
            str(output),
            "--worktree",
            str(self.selected),
            "--expected-head",
            self.expected_head,
            "--pytest-python",
            sys.executable,
            "--pytest-import",
            imported_module,
            "--pytest-args-json",
            json.dumps(pytest_arguments or ["-q", "-o", "addopts=", "tests/test_demo.py"]),
        ]
        for gate_id in GATE_IDS:
            if gate_id != "tests":
                arguments.extend((f"--skip-{gate_id}", "out of scope"))
        return subprocess.run(
            arguments, cwd=self.original, env=self.environment, capture_output=True, text=True, check=False
        )


def _gate_check(output: Path, gate_id: str) -> dict[str, object]:
    """Load one generated gate record by id from a run_gates.py output directory."""
    payload = json.loads((output / "gates.json").read_text(encoding="utf-8"))
    return next(check for check in payload["checks"] if check["id"] == gate_id)


@pytest.fixture
def editable_checkout(tmp_path: Path, request: pytest.FixtureRequest) -> _EditableCheckout:
    """Build a committed checkout with a demo package plus a detached worktree of the same commit."""
    original = tmp_path / "original"
    original.mkdir()
    _initialize_repository(original)
    package = original / "src" / "demo_package"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("VALUE = 'same in both checkouts'\n", encoding="utf-8", newline="\n")
    tests = original / "tests"
    tests.mkdir()
    (tests / "test_demo.py").write_text(
        "from demo_package import VALUE\n"
        "\ndef test_value():\n"
        "    import os\n"
        "    from pathlib import Path\n"
        "    if marker := os.environ.get('SOURCE_BOUND_MARKER'):\n"
        "        Path(marker).write_text('executed', encoding='utf-8')\n"
        "    assert VALUE == 'same in both checkouts'\n",
        encoding="utf-8",
        newline="\n",
    )
    project_addopts = getattr(request, "param", None)
    if project_addopts == "drop-proof":
        (tests / "conftest.py").write_text(
            "import pytest\n\n"
            "@pytest.hookimpl(hookwrapper=True, trylast=True)\n"
            "def pytest_sessionfinish(session, exitstatus):\n"
            "    yield\n"
            "    if hasattr(session.config, 'workeroutput'):\n"
            "        session.config.workeroutput.pop('codex_rig_source_proof', None)\n",
            encoding="utf-8",
            newline="\n",
        )
    elif project_addopts is not None:
        (original / "pytest.ini").write_text(f"[pytest]\naddopts = {project_addopts}\n", encoding="utf-8", newline="\n")
    (original / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8", newline="\n")
    _git(original, "add", "src", "tests", ".gitignore")
    if project_addopts not in (None, "drop-proof"):
        _git(original, "add", "pytest.ini")
    _git(original, "commit", "-m", f"test package\n\n{CODEX_TRAILER}")
    expected_head = _git(original, "rev-parse", "HEAD")
    selected = tmp_path / "selected"
    _git(original, "worktree", "add", "--detach", str(selected), expected_head)

    # A .pth file is the import-path effect of a conventional editable install.
    bootstrap = tmp_path / "bootstrap"
    bootstrap.mkdir()
    editable_site = tmp_path / "editable-site"
    editable_site.mkdir()
    (bootstrap / "sitecustomize.py").write_text(
        f"import site\nsite.addsitedir({str(editable_site)!r})\n", encoding="utf-8", newline="\n"
    )
    editable_path = editable_site / "editable.pth"
    environment = {**os.environ, "PYTHONPATH": str(bootstrap)}

    return _EditableCheckout(original, selected, expected_head, editable_path, environment)


def test_editable_source_from_invoking_checkout_fails_gate(
    editable_checkout: _EditableCheckout, tmp_path: Path
) -> None:
    """A passing test must not certify an editable package imported from the invoking checkout."""
    editable_checkout.editable_path.write_text(
        f"{(editable_checkout.original / 'src').as_posix()}\n", encoding="utf-8", newline="\n"
    )
    output = tmp_path / "wrong-gates"

    completed = editable_checkout.run_tests(output)

    assert completed.returncode == 1
    check = _gate_check(output, "tests")
    assert check["status"] == "fail"
    assert check["python_imports"]["status"] == "fail"
    assert (
        check["python_imports"]["modules"]["demo_package"]["origin"]
        == (editable_checkout.original / "src" / "demo_package" / "__init__.py").resolve().as_posix()
    )
    assert "1 passed" in (output / str(check["stdout"])).read_text(encoding="utf-8")


def test_editable_source_from_selected_worktree_passes_gate(
    editable_checkout: _EditableCheckout, tmp_path: Path
) -> None:
    """Certify an editable package whose import resolves to the selected worktree checkout."""
    editable_checkout.editable_path.write_text(
        f"{(editable_checkout.selected / 'src').as_posix()}\n", encoding="utf-8", newline="\n"
    )
    output = tmp_path / "right-gates"

    completed = editable_checkout.run_tests(output)

    assert completed.returncode == 0, completed.stderr
    check = _gate_check(output, "tests")
    assert check["status"] == "pass"
    assert check["python_imports"]["status"] == "pass"
    assert check["python_imports"]["modules"]["demo_package"] == {
        "origin": (editable_checkout.selected / "src" / "demo_package" / "__init__.py").resolve().as_posix(),
        "tracked": True,
        "status": "pass",
        "reason": None,
    }
    selected_test = (editable_checkout.selected / "tests" / "test_demo.py").resolve().as_posix()
    assert check["python_imports"]["tests"][selected_test] == {
        "origin": selected_test,
        "tracked": True,
        "status": "pass",
        "reason": None,
    }


@pytest.mark.parametrize("editable_checkout", ["-n 2"], indirect=True)
@pytest.mark.parametrize("parallel_source", ["cli", "project", "environment"])
def test_parallel_source_bound_pytest_proves_worker_sources(
    editable_checkout: _EditableCheckout, tmp_path: Path, parallel_source: str
) -> None:
    """Certify selected tests and imports from actual xdist workers for each option source."""
    editable_checkout.editable_path.write_text(
        f"{(editable_checkout.selected / 'src').as_posix()}\n", encoding="utf-8", newline="\n"
    )
    marker = tmp_path / "selected-test-ran.txt"
    editable_checkout.environment["SOURCE_BOUND_MARKER"] = str(marker)
    if parallel_source == "environment":
        editable_checkout.environment["PYTEST_ADDOPTS"] = "-n 2"
    pytest_arguments = ["-q", "tests/test_demo.py"]
    if parallel_source != "project":
        pytest_arguments.extend(["-o", "addopts="])
    if parallel_source == "cli":
        pytest_arguments.extend(["-n", "2"])
    output = tmp_path / "parallel-gates"

    completed = editable_checkout.run_tests(output, pytest_arguments=pytest_arguments)

    assert completed.returncode == 0, completed.stderr
    assert marker.read_text(encoding="utf-8") == "executed"
    check = _gate_check(output, "tests")
    assert check["status"] == "pass"
    proof = check["python_imports"]
    assert proof["status"] == "pass"
    assert len(proof["workers"]) == 2
    assert all(worker["status"] != "fail" for worker in proof["workers"].values())
    selected_test = (editable_checkout.selected / "tests" / "test_demo.py").resolve().as_posix()
    assert proof["tests"][selected_test]["status"] == "pass"
    assert (
        proof["modules"]["demo_package"]["origin"]
        == (editable_checkout.selected / "src" / "demo_package" / "__init__.py").resolve().as_posix()
    )
    assert any(selected_test in worker["tests"] for worker in proof["workers"].values())
    assert any(worker["modules"]["demo_package"]["status"] == "pass" for worker in proof["workers"].values())


@pytest.mark.parametrize(
    "pytest_arguments", [["-q", "-o", "addopts=", "tests/test_demo.py"], ["-q", "-n", "0", "tests/test_demo.py"]]
)
def test_serial_source_bound_pytest_retains_source_proof(
    editable_checkout: _EditableCheckout, tmp_path: Path, pytest_arguments: list[str]
) -> None:
    """Keep serial and explicit zero-worker pytest valid with selected test execution."""
    editable_checkout.editable_path.write_text(
        f"{(editable_checkout.selected / 'src').as_posix()}\n", encoding="utf-8", newline="\n"
    )
    marker = tmp_path / "selected-test-ran.txt"
    editable_checkout.environment["SOURCE_BOUND_MARKER"] = str(marker)
    output = tmp_path / "serial-gates"

    completed = editable_checkout.run_tests(output, pytest_arguments=pytest_arguments)

    assert completed.returncode == 0, completed.stderr
    assert marker.read_text(encoding="utf-8") == "executed"
    assert _gate_check(output, "tests")["python_imports"]["status"] == "pass"


def test_parallel_worker_wrong_editable_origin_fails_source_proof(
    editable_checkout: _EditableCheckout, tmp_path: Path
) -> None:
    """A worker import from the invoking checkout cannot certify reviewed source."""
    editable_checkout.editable_path.write_text(
        f"{(editable_checkout.original / 'src').as_posix()}\n", encoding="utf-8", newline="\n"
    )
    output = tmp_path / "wrong-worker-gates"

    completed = editable_checkout.run_tests(output, pytest_arguments=["-q", "-n", "2", "tests/test_demo.py"])

    assert completed.returncode == 1
    check = _gate_check(output, "tests")
    assert check["status"] == "fail"
    proof = check["python_imports"]
    assert len(proof["workers"]) == 2
    assert (
        proof["modules"]["demo_package"]["origin"]
        == (editable_checkout.original / "src" / "demo_package" / "__init__.py").resolve().as_posix()
    )
    assert proof["modules"]["demo_package"]["status"] == "fail"


def test_parallel_external_selected_test_fails_source_proof(
    editable_checkout: _EditableCheckout, tmp_path: Path
) -> None:
    """Worker collection of an outside test file must fail selected-test provenance."""
    editable_checkout.editable_path.write_text(
        f"{(editable_checkout.selected / 'src').as_posix()}\n", encoding="utf-8", newline="\n"
    )
    external_test = editable_checkout.original / "tests" / "test_demo.py"
    output = tmp_path / "external-worker-gates"

    completed = editable_checkout.run_tests(output, pytest_arguments=["-q", "-n", "2", str(external_test)])

    assert completed.returncode == 1
    check = _gate_check(output, "tests")
    assert check["status"] == "fail"
    proof = check["python_imports"]
    assert len(proof["workers"]) == 2
    assert proof["tests"][external_test.resolve().as_posix()]["reason"] == "test-outside-worktree"


@pytest.mark.parametrize("editable_checkout", ["drop-proof"], indirect=True)
def test_parallel_missing_worker_receipts_fail_closed(editable_checkout: _EditableCheckout, tmp_path: Path) -> None:
    """Reject a passing worker test whose source receipt is removed before delivery."""
    editable_checkout.editable_path.write_text(
        f"{(editable_checkout.selected / 'src').as_posix()}\n", encoding="utf-8", newline="\n"
    )
    output = tmp_path / "missing-worker-proof-gates"

    completed = editable_checkout.run_tests(output, pytest_arguments=["-q", "-n", "2", "tests/test_demo.py"])

    assert completed.returncode == 1
    check = _gate_check(output, "tests")
    assert check["status"] == "fail"
    assert check["python_imports"]["status"] == "fail"
    assert any(error.startswith("worker-proof-missing:") for error in check["python_imports"]["errors"])


def test_external_test_file_cannot_certify_selected_worktree(
    editable_checkout: _EditableCheckout, tmp_path: Path
) -> None:
    """Fail source proof when a passing test comes from outside the reviewed checkout."""
    editable_checkout.editable_path.write_text(
        f"{(editable_checkout.selected / 'src').as_posix()}\n", encoding="utf-8", newline="\n"
    )
    output = tmp_path / "external-test-gates"
    external_test = editable_checkout.original / "tests" / "test_demo.py"

    completed = editable_checkout.run_tests(output, pytest_arguments=["-q", "-o", "addopts=", str(external_test)])

    assert completed.returncode == 1
    check = _gate_check(output, "tests")
    assert check["status"] == "fail"
    assert check["python_imports"]["status"] == "fail"
    assert check["python_imports"]["tests"][external_test.resolve().as_posix()] == {
        "origin": external_test.resolve().as_posix(),
        "tracked": False,
        "status": "fail",
        "reason": "test-outside-worktree",
    }
    assert "1 passed" in (output / str(check["stdout"])).read_text(encoding="utf-8")


def test_unimported_declared_module_reports_inconclusive_provenance(
    editable_checkout: _EditableCheckout, tmp_path: Path
) -> None:
    """Report inconclusive provenance for a declared import module that the test run never loads."""
    output = tmp_path / "missing-gates"

    completed = editable_checkout.run_tests(output, "unloaded_package")

    assert completed.returncode == 1
    check = _gate_check(output, "tests")
    assert check["status"] == "fail"
    assert check["python_imports"]["status"] == "inconclusive"
    assert check["python_imports"]["modules"]["unloaded_package"]["reason"] == "module-not-imported"


@pytest.mark.parametrize("mutation", ["tracked", "untracked", "commit"])
def test_ignored_submodule_changes_block_guarded_execution(tmp_path: Path, mutation: str) -> None:
    """Reject changed submodule source even when the repository hides it from ordinary status."""
    repository = tmp_path / "repository"
    repository.mkdir()
    _initialize_repository(repository)
    dependency = repository / "dependency"
    dependency.mkdir()
    _initialize_repository(dependency)
    (repository / ".gitmodules").write_text(
        '[submodule "dependency"]\n\tpath = dependency\n\turl = ../dependency\n\tignore = all\n',
        encoding="utf-8",
        newline="\n",
    )
    _git(repository, "add", ".gitmodules", "dependency")
    _git(repository, "commit", "-m", f"dependency\n\n{CODEX_TRAILER}")
    head = _git(repository, "rev-parse", "HEAD")
    changed_file = dependency / ("extra.txt" if mutation == "untracked" else "tracked.txt")
    changed_file.write_text("changed\n", encoding="utf-8", newline="\n")
    if mutation == "commit":
        _git(dependency, "add", "tracked.txt")
        _git(dependency, "commit", "-m", f"new dependency state\n\n{CODEX_TRAILER}")
    assert _git(repository, "status", "--porcelain=v1", "--untracked-files=all") == ""
    assert _git(repository, "status", "--porcelain=v1", "--ignore-submodules=none")
    marker = tmp_path / "ran.txt"
    output = tmp_path / "gates"

    completed = _run_lint_gate(
        repository,
        output,
        head,
        _python_command(f"from pathlib import Path; Path({str(marker)!r}).write_text('ran', encoding='utf-8')"),
    )

    assert completed.returncode == 1
    assert not marker.exists()
    check = _lint_check(output)
    assert check["status"] == "fail"
    assert check["source"]["before"]["head"] == head
    assert "dependency" in check["source"]["before"]["status"]
    assert check["source"]["after"] is None
    assert "source-dirty" in (output / str(check["stderr"])).read_text(encoding="utf-8")


def _local_gate_inputs(tmp_path: Path, variant: str) -> tuple[Path, Path, Path, list[str]]:
    """Build a real collector mirror and its explicit import-bound gate invocation."""
    repository = tmp_path / "repository"
    repository.mkdir()
    head = _initialize_repository(repository)
    (repository / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    _git(repository, "add", ".gitignore")
    _git(repository, "commit", "-m", f"test caches\n\n{CODEX_TRAILER}")
    module = repository / "localmod.py"
    test = repository / "test_localmod.py"
    module.write_text("value = 1\n", encoding="utf-8")
    test.write_text(
        "import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).parent))\n"
        "import localmod\ndef test_value():\n    assert localmod.value == 2\n",
        encoding="utf-8",
    )
    if variant != "added":
        _git(repository, "add", "localmod.py", "test_localmod.py")
        if variant == "gitlink":
            _git(repository, "update-index", "--add", "--cacheinfo", f"160000,{head},dependency")
            (repository / "dependency").mkdir()
        _git(repository, "commit", "-m", f"runtime files\n\n{CODEX_TRAILER}")
    module.write_text("value = 2\n", encoding="utf-8")
    output = tmp_path / "review"
    collected = subprocess.run(
        [
            sys.executable,
            str(PLUGIN_ROOT / "shared/collect_diff.py"),
            "--review-worktree",
            "--repository",
            str(repository),
            "--out",
            str(output / "local-source"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert collected.returncode == 0, collected.stderr
    mirror = Path(json.loads((output / "local-source/review-worktree.json").read_bytes())["review_worktree"])
    if variant == "gitlink":
        (mirror / "dependency").mkdir(exist_ok=True)
    args = [
        sys.executable,
        str(RUN_GATES),
        "--out",
        str(output),
        "--worktree",
        str(mirror),
        "--pytest-python",
        sys.executable,
        "--pytest-import",
        "localmod",
        "--pytest-args-json",
        json.dumps(["-q", "-p", "no:xdist", "-o", "addopts=", "test_localmod.py"]),
    ]
    for gate in GATE_IDS:
        if gate != "tests":
            args.extend((f"--skip-{gate}", "out of scope"))
    return repository, output, mirror, args


@pytest.mark.parametrize("variant", ["tracked", "added", "gitlink"])
def test_local_mirror_import_bound_gate_preserves_admitted_source(tmp_path: Path, variant: str) -> None:
    """Import exact dirty mirror bytes including added files while excluding unchanged gitlinks."""
    _repository, output, mirror, args = _local_gate_inputs(tmp_path, variant)
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    check = _gate_check(output, "tests")
    assert check["source"]["mode"] == "local-review-mirror"
    assert check["source"]["before"] == check["source"]["after"]
    proof = check["python_imports"]
    sidecar = (output / "checks/tests.python-imports.json").read_bytes()
    assert json.loads(sidecar) == proof
    assert check["source"]["artifacts"]["checks/tests.python-imports.json"] == hashlib.sha256(sidecar).hexdigest()
    assert proof["modules"]["localmod"]["tracked"] is (variant != "added")
    assert proof["modules"]["localmod"]["snapshot_member"]["path"] == "localmod.py"
    assert proof["tests"][(mirror / "test_localmod.py").as_posix()]["tracked"] is (variant != "added")
    snapshot = json.loads((output / "checks/local-source-snapshot.json").read_bytes())
    assert all(record["path"] != "dependency" for record in snapshot["files"])
    assert bool(snapshot["gitlinks"]) is (variant == "gitlink")


@pytest.mark.parametrize("mutation", ["during-run", "gitlink-pointer", "unreceipted-root"])
def test_local_gate_rejects_source_binding_changes(tmp_path: Path, mutation: str) -> None:
    """Local source execution never accepts drift, altered gitlink identity or another checkout."""
    repository, output, mirror, _args = _local_gate_inputs(
        tmp_path, "gitlink" if mutation == "gitlink-pointer" else "added"
    )
    marker = tmp_path / "executed.txt"
    command = _python_command(
        f"from pathlib import Path; Path({str(marker)!r}).write_text('ran'); "
        + (f"Path({str(mirror / 'localmod.py')!r}).write_text('value = 99\\n')" if mutation == "during-run" else "pass")
    )
    if mutation == "gitlink-pointer":
        _git(mirror, "update-index", "--cacheinfo", f"160000,{_git(mirror, 'rev-parse', 'HEAD')},dependency")
    elif mutation == "unreceipted-root":
        mirror = repository
    result = _run_lint_gate(repository, output, None, command, mirror)
    assert result.returncode != 0
    assert marker.exists() is (mutation == "during-run")
    if mutation == "during-run":
        assert _lint_check(output)["status"] == "fail"


def test_parallel_local_added_source_proof_binds_every_worker(tmp_path: Path) -> None:
    """Actual pytest workers retain the same admitted untracked module and test byte membership."""
    _repository, output, _mirror, args = _local_gate_inputs(tmp_path, "added")
    index = args.index("--pytest-args-json") + 1
    args[index] = json.dumps(["-q", "-n", "2", "-p", "no:cacheprovider", "-o", "addopts=", "test_localmod.py"])
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    proof = _gate_check(output, "tests")["python_imports"]
    assert set(proof["workers"]) == {"gw0", "gw1"}
    assert all(worker["local_source"] == proof["local_source"] for worker in proof["workers"].values())
    assert proof["modules"]["localmod"]["tracked"] is False
    assert all(record["tracked"] is False for record in proof["tests"].values())


@pytest.mark.parametrize("mutation", ["changed", "deleted"])
def test_local_gate_rejects_child_sidecar_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str) -> None:
    """Reject child proof changes during real post-run source capture instead of hashing mixed evidence."""
    _repository, output, mirror, _args = _local_gate_inputs(tmp_path, "added")
    monkeypatch.syspath_prepend(str(PLUGIN_ROOT / "shared"))
    spec = importlib.util.spec_from_file_location("sidecar_gate_runner", RUN_GATES)
    gates = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = gates
    spec.loader.exec_module(gates)
    (output / "checks").mkdir()
    snapshot = gates.validate_local_gate_source(output, mirror)
    content = gates._local_snapshot_bytes(snapshot)
    (output / "checks/local-source-snapshot.json").write_bytes(content)
    local_source = {
        "mode": "local-review-mirror",
        "worktree": mirror.as_posix(),
        "receipt_path": "local-source/review-worktree.json",
        "receipt_sha256": hashlib.sha256((output / "local-source/review-worktree.json").read_bytes()).hexdigest(),
        "snapshot_path": "checks/local-source-snapshot.json",
        "snapshot_sha256": hashlib.sha256(content).hexdigest(),
    }
    sidecar = output / "checks/tests.python-imports.json"
    read_bytes = Path.read_bytes
    observations = 0

    def read_and_change_proof(path: Path) -> bytes:
        """Read actual bytes, then synchronize an exact-path sidecar mutation after initial consumption."""
        nonlocal observations
        content = read_bytes(path)
        if path == sidecar:
            observations += 1
            if observations == 1:
                assert json.loads(content)["status"] == "pass"
                if mutation == "deleted":
                    path.unlink()
                else:
                    path.write_bytes(content + b" ")
        return content

    read_text = Path.read_text

    def read_text_and_change_proof(path: Path, *arguments, **keywords) -> str:
        """Apply the same exact-path timing control to the historical text reader."""
        nonlocal observations
        content = read_text(path, *arguments, **keywords)
        if path == sidecar:
            observations += 1
            assert json.loads(content)["status"] == "pass"
            if mutation == "deleted":
                path.unlink()
            else:
                path.write_bytes(content.encode("utf-8") + b" ")
        return content

    monkeypatch.setattr(Path, "read_text", read_text_and_change_proof)
    monkeypatch.setattr(Path, "read_bytes", read_and_change_proof)
    result = gates.run_check(
        "tests",
        "",
        "",
        60,
        output,
        None,
        mirror,
        gates.PythonTestSpec(
            Path(sys.executable), ("localmod",), ("-q", "-p", "no:xdist", "-o", "addopts=", "test_localmod.py")
        ),
        {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        local_source,
    )
    assert result["python_imports"]["status"] == "pass"
    assert result["source"]["before"] == result["source"]["after"]
    assert result["status"] == "fail"
    assert observations == (1 if mutation == "deleted" else 2)
    assert "local-gate-proof-artifact-changed" in (output / "checks/tests.stderr.txt").read_text()

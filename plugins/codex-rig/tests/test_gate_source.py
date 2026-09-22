"""Real-Git acceptance checks for optional gate source binding."""

from __future__ import annotations

import base64
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
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


def test_in_process_pytest_rejects_editable_source_from_another_checkout(tmp_path: Path) -> None:
    """A passing test must not certify an editable package imported from the invoking checkout."""
    original = tmp_path / "original"
    original.mkdir()
    _initialize_repository(original)
    package = original / "src" / "demo_package"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("VALUE = 'same in both checkouts'\n", encoding="utf-8", newline="\n")
    tests = original / "tests"
    tests.mkdir()
    (tests / "test_demo.py").write_text(
        "from demo_package import VALUE\n\ndef test_value():\n    assert VALUE == 'same in both checkouts'\n",
        encoding="utf-8",
        newline="\n",
    )
    (original / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8", newline="\n")
    _git(original, "add", "src", "tests", ".gitignore")
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

    def run_tests(output: Path, imported_module: str = "demo_package") -> subprocess.CompletedProcess[str]:
        """Run the structured test gate from the original checkout."""
        arguments = [
            sys.executable,
            str(RUN_GATES),
            "--out",
            str(output),
            "--worktree",
            str(selected),
            "--expected-head",
            expected_head,
            "--pytest-python",
            sys.executable,
            "--pytest-import",
            imported_module,
            "--pytest-args-json",
            '["-q", "-o", "addopts=", "tests/test_demo.py"]',
        ]
        for gate_id in GATE_IDS:
            if gate_id != "tests":
                arguments.extend((f"--skip-{gate_id}", "out of scope"))
        return subprocess.run(arguments, cwd=original, env=environment, capture_output=True, text=True, check=False)

    editable_path.write_text(f"{(original / 'src').as_posix()}\n", encoding="utf-8", newline="\n")
    wrong_output = tmp_path / "wrong-gates"
    wrong = run_tests(wrong_output)

    assert wrong.returncode == 1
    wrong_check = next(
        check for check in json.loads((wrong_output / "gates.json").read_text())["checks"] if check["id"] == "tests"
    )
    assert wrong_check["status"] == "fail"
    assert wrong_check["python_imports"]["status"] == "fail"
    assert (
        wrong_check["python_imports"]["modules"]["demo_package"]["origin"]
        == (original / "src" / "demo_package" / "__init__.py").resolve().as_posix()
    )
    assert "1 passed" in (wrong_output / str(wrong_check["stdout"])).read_text(encoding="utf-8")

    editable_path.write_text(f"{(selected / 'src').as_posix()}\n", encoding="utf-8", newline="\n")
    right_output = tmp_path / "right-gates"
    right = run_tests(right_output)

    assert right.returncode == 0, right.stderr
    right_check = next(
        check for check in json.loads((right_output / "gates.json").read_text())["checks"] if check["id"] == "tests"
    )
    assert right_check["status"] == "pass"
    assert right_check["python_imports"]["status"] == "pass"
    assert right_check["python_imports"]["modules"]["demo_package"] == {
        "origin": (selected / "src" / "demo_package" / "__init__.py").resolve().as_posix(),
        "tracked": True,
        "status": "pass",
        "reason": None,
    }

    missing_output = tmp_path / "missing-gates"
    missing = run_tests(missing_output, "unloaded_package")

    assert missing.returncode == 1
    missing_check = next(
        check for check in json.loads((missing_output / "gates.json").read_text())["checks"] if check["id"] == "tests"
    )
    assert missing_check["status"] == "fail"
    assert missing_check["python_imports"]["status"] == "inconclusive"
    assert missing_check["python_imports"]["modules"]["unloaded_package"]["reason"] == "module-not-imported"


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

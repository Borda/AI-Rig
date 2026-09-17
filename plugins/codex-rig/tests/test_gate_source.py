"""Real-Git acceptance checks for optional gate source binding."""

from __future__ import annotations

import base64
import json
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
    repository: Path, output: Path, expected_head: str | None, command: str
) -> subprocess.CompletedProcess[str]:
    """Run one executable lint check while declaring the other canonical gates unavailable."""
    arguments = [sys.executable, str(RUN_GATES), "--out", str(output), "--lint", command]
    if expected_head is not None:
        arguments.extend(("--expected-head", expected_head))
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


def test_matching_head_runs_command_and_keeps_command_failure(tmp_path: Path) -> None:
    """Execute on a clean matching source but retain the command's own nonzero failure."""
    repository = tmp_path / "repository"
    repository.mkdir()
    expected_head = _initialize_repository(repository)
    output = tmp_path / "gates"

    completed = _run_lint_gate(repository, output, expected_head, _python_command("raise SystemExit(7)"))

    assert completed.returncode == 1
    check = _lint_check(output)
    assert check["status"] == "fail"
    assert check["exit_code"] == 7
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

"""Real-Git acceptance checks for optional gate source binding.

Every test drives ``shared/run_gates.py`` as a subprocess against a real temporary Git repository, because the contract
is the observable receipt, exit status and side effects of that runner, not any internal function. Nothing here is
mocked: the Git state, the checked-out worktrees and the child processes are the behaviour under test.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from _modules import load_module

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
RUN_GATES = PLUGIN_ROOT / "shared" / "run_gates.py"
GATE_IDS = ("lint", "format", "types", "tests", "review")
CODEX_TRAILER = "Co-authored-by: Codex <codex@openai.com>"
SOURCE_DIRTY_STATUS = "source-dirty"
HEAD_MISMATCH = "source-head-mismatch"
INVALID_FAIL_EXIT_CODE = "gate-check-invalid-fail-exit-code"


def _git(repository: Path, *arguments: str) -> str:
    """Run one successful local Git command and return its standard output."""
    completed = subprocess.run(["git", *arguments], cwd=repository, capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def _git_steps(repository: Path, steps: list[tuple[str, ...]]) -> None:
    """Run each listed Git command in order inside one repository."""
    for step in steps:
        _git(repository, *step)


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


def _marker_command(marker: Path) -> str:
    """Return a gate command whose only effect is to create ``marker``, proving the command ran."""
    return _python_command(f"from pathlib import Path; Path({str(marker)!r}).write_text('ran', encoding='utf-8')")


def _skip_gates(keep: str) -> list[str]:
    """Return the options that declare every canonical gate except ``keep`` out of scope."""
    return [part for gate in GATE_IDS if gate != keep for part in (f"--skip-{gate}", "out of scope")]


def _run(
    arguments: list[str], cwd: Path | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run one command to completion, capturing text output and never raising on a failing exit status."""
    return subprocess.run(arguments, cwd=cwd, env=env, capture_output=True, text=True, check=False)


def _run_lint_gate(
    repository: Path, output: Path, expected_head: str | None, command: str, worktree: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Run one executable lint check while declaring the other canonical gates unavailable."""
    arguments = [sys.executable, str(RUN_GATES), "--out", str(output), "--lint", command]
    if expected_head is not None:
        arguments.extend(("--expected-head", expected_head))
    if worktree is not None:
        arguments.extend(("--worktree", str(worktree)))
    return _run([*arguments, *_skip_gates("lint")], cwd=repository)


def _gate_check(output: Path, gate_id: str) -> dict[str, Any]:
    """Load one generated gate record by id from a ``run_gates.py`` output directory."""
    payload = json.loads((output / "gates.json").read_text(encoding="utf-8"))
    return next(check for check in payload["checks"] if check["id"] == gate_id)


def _receipt_text(output: Path, check: dict[str, Any], stream: str = "stderr") -> str:
    """Read the stdout or stderr receipt file that a gate record points at."""
    return (output / str(check[stream])).read_text(encoding="utf-8")


def _load_validator() -> Any:
    """Load the shared artifact validator without requiring a package import."""
    return load_module("gate_artifact_validator", PLUGIN_ROOT / "shared/validate-artifacts.py")


def _edit_gates_json(output: Path, edit: Callable[[dict[str, Any]], None]) -> None:
    """Rewrite ``gates.json`` after applying ``edit`` to its parsed payload."""
    path = output / "gates.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    edit(payload)
    path.write_text(json.dumps(payload), encoding="utf-8", newline="\n")


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """Create a clean committed repository at ``tmp_path/repository``."""
    path = tmp_path / "repository"
    path.mkdir()
    _initialize_repository(path)
    return path


@pytest.fixture
def head(repository: Path) -> str:
    """Return the commit id at the tip of the ``repository`` fixture."""
    return _git(repository, "rev-parse", "HEAD")


@pytest.fixture
def output(tmp_path: Path) -> Path:
    """Return where a gate run writes its receipts; the runner creates the directory."""
    return tmp_path / "gates"


@pytest.fixture
def linked_worktree(tmp_path: Path) -> tuple[Path, Path, str]:
    """Return a committed ``original`` checkout, a detached ``selected`` worktree of the same commit, and that id."""
    original = tmp_path / "original"
    original.mkdir()
    expected_head = _initialize_repository(original)
    selected = tmp_path / "selected"
    _git(original, "worktree", "add", "--detach", str(selected), expected_head)
    return original, selected, expected_head


class TestPytestGateRecovery:
    """A source-bound test gate that cannot run pytest keeps its failure and says how review should continue."""

    @staticmethod
    def _tests_gate_command(
        output: Path, repository: Path, expected_head: str, interpreter: Path | str, *extra: str
    ) -> list[str]:
        """Build a source-bound ``--pytest-python`` invocation with every other gate declared out of scope."""
        return [
            sys.executable,
            str(RUN_GATES),
            "--out",
            str(output),
            "--expected-head",
            expected_head,
            "--worktree",
            str(repository),
            "--pytest-python",
            str(interpreter),
            "--pytest-import",
            "example",
            *extra,
            *_skip_gates("tests"),
        ]

    @staticmethod
    def _assert_failure_with_recovery(result: subprocess.CompletedProcess[str], output: Path, stderr_text: str) -> None:
        """Assert the tests gate failed, kept ``stderr_text`` and printed the dependency recovery guidance."""
        assert result.returncode == 1
        assert stderr_text in (output / "checks/tests.stderr.txt").read_text(encoding="utf-8")
        assert json.loads((output / "gates.json").read_text(encoding="utf-8"))["checks_failed"] == ["tests"]
        assert "dependency preparation" in result.stderr
        assert "continue independent review" in result.stderr

    def test_missing_pytest_keeps_failure_and_directs_recovery(
        self, tmp_path: Path, repository: Path, head: str, output: Path
    ) -> None:
        """A gate interpreter without pytest fails the tests gate and tells review work to continue.

        The runner is pointed at a fresh virtual environment that has no pytest installed; the import error must stay in
        the tests receipt instead of being masked by the recovery message.
        """
        environment = tmp_path / "environment"
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(environment)], check=True, capture_output=True
        )
        interpreter = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")

        result = _run(self._tests_gate_command(output, repository, head, interpreter))

        self._assert_failure_with_recovery(result, output, "ModuleNotFoundError: No module named 'pytest'")

    def test_collection_import_failure_keeps_failure_and_directs_recovery(self, repository: Path, output: Path) -> None:
        """An import failure while pytest collects also keeps its failure and the recovery guidance.

        The committed ``conftest.py`` imports a module that does not exist, so pytest itself starts but cannot collect.
        """
        (repository / "conftest.py").write_text("import nonexistent_review_fixture_dependency\n", encoding="utf-8")
        _git(repository, "add", "conftest.py")
        _git(repository, "commit", "-m", f"collection fixture\n\n{CODEX_TRAILER}")
        head = _git(repository, "rev-parse", "HEAD")

        result = _run(
            self._tests_gate_command(
                output, repository, head, sys.executable, "--pytest-args-json", '["-p", "no:xdist"]'
            )
        )

        self._assert_failure_with_recovery(result, output, "nonexistent_review_fixture_dependency")


class TestProjectEnvironment:
    """The optional ``--project-env`` selects the checker environment without falling back to ``PATH``."""

    def test_checker_runs_with_project_env_first_on_path_in_selected_worktree(
        self, tmp_path: Path, repository: Path, head: str, output: Path
    ) -> None:
        """A declared checker sees the project environment first on ``PATH`` and runs in the selected worktree.

        The environment is a minimal fake with ``pyvenv.cfg`` and a placeholder interpreter; the checker asserts its own
        ``PATH`` and records its working directory so the receipt can be compared against the worktree.
        """
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
            str(output),
            "--expected-head",
            head,
            "--worktree",
            str(repository),
            "--project-env",
            str(environment),
            "--types",
            command,
            *_skip_gates("types"),
        ]

        result = _run(arguments, cwd=tmp_path)

        assert result.returncode == 0, result.stderr
        assert marker.read_text(encoding="utf-8") == str(repository)
        payload = json.loads((output / "gates.json").read_text(encoding="utf-8"))
        assert _gate_check(output, "types")["status"] == "pass"
        assert payload["source"]["project_env"] == environment.as_posix()

    def test_missing_environment_is_rejected_before_gates(
        self, tmp_path: Path, repository: Path, head: str, output: Path
    ) -> None:
        """A ``--project-env`` that does not exist is a prerequisite error, not a silent ``PATH`` fallback.

        The runner must exit with the usage status and write no receipts at all.
        """
        arguments = [
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
        ]

        result = _run(arguments, cwd=tmp_path)

        assert result.returncode == 2
        assert "invalid-project-env" in result.stderr
        assert not output.exists()


class TestPythonCommand:
    """The test-local ``_python_command`` helper builds commands that run under every supported shell."""

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
    def test_windows_interpreter_path_uses_powershell_invocation(
        self, monkeypatch: pytest.MonkeyPatch, executable: str, expected_prefix: str
    ) -> None:
        """Interpreter paths with spaces or apostrophes are quoted for PowerShell on Windows.

        The command is built on any host by faking the platform and executable, so the Windows quoting stays covered on
        POSIX runners too.
        """
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(sys, "executable", executable)

        command = _python_command("pass")

        assert command.startswith(expected_prefix)
        assert "cGFzcw==" in command


class TestSourceGuard:
    """``--expected-head`` binds a gate command to the exact committed source before and after it runs."""

    @pytest.mark.parametrize("expected_head", [pytest.param("0" * 40, id="sha1"), pytest.param("0" * 64, id="sha256")])
    def test_wrong_expected_head_blocks_command_and_records_preblocked_receipt(
        self, tmp_path: Path, repository: Path, head: str, output: Path, expected_head: str
    ) -> None:
        """A checkout whose head differs from the expected one never runs the command and is recorded as pre-blocked.

        Both SHA-1 and SHA-256 length ids are accepted; the runner must fail the gate and keep the actual head in the
        receipt instead of executing a command against unreviewed source.
        """
        marker = repository / "ran.txt"

        result = _run_lint_gate(repository, output, expected_head, _marker_command(marker))

        check = _gate_check(output, "lint")
        assert result.returncode == 1
        assert not marker.exists()
        assert check["status"] == "fail"
        assert check["source"] == {
            "expected_head": expected_head,
            "before": {"head": head, "status": ""},
            "after": None,
        }
        assert HEAD_MISMATCH in _receipt_text(output, check)

    @pytest.mark.parametrize("exit_code", [0, 1, 7])
    def test_matching_head_runs_command_and_keeps_its_exit_status(
        self, repository: Path, head: str, output: Path, exit_code: int
    ) -> None:
        """On matching source the command runs and its native exit status is recorded unchanged.

        A zero exit passes the gate; any nonzero exit fails it while the exact code, and clean before and after source
        receipts, are retained.
        """
        result = _run_lint_gate(repository, output, head, _python_command(f"raise SystemExit({exit_code})"))

        check = _gate_check(output, "lint")
        assert result.returncode == (1 if exit_code else 0)
        assert check["status"] == ("fail" if exit_code else "pass")
        assert check["exit_code"] == exit_code
        assert check["source"] == {
            "expected_head": head,
            "before": {"head": head, "status": ""},
            "after": {"head": head, "status": ""},
        }

    @pytest.mark.parametrize(
        ("mutation", "expected_status"),
        [
            pytest.param("tracked.txt", " M tracked.txt", id="tracked"),
            pytest.param("untracked.txt", "?? untracked.txt", id="untracked"),
        ],
    )
    def test_dirty_source_blocks_command_before_execution(
        self, repository: Path, head: str, output: Path, mutation: str, expected_status: str
    ) -> None:
        """A tracked edit or an untracked file blocks the gate before its command starts.

        The porcelain status of the dirty checkout is recorded as the ``before`` receipt and there is no ``after``.
        """
        (repository / mutation).write_text("changed\n", encoding="utf-8", newline="\n")
        marker = repository / "ran.txt"

        result = _run_lint_gate(repository, output, head, _marker_command(marker))

        check = _gate_check(output, "lint")
        assert result.returncode == 1
        assert not marker.exists()
        assert check["status"] == "fail"
        assert check["source"] == {
            "expected_head": head,
            "before": {"head": head, "status": expected_status},
            "after": None,
        }
        assert SOURCE_DIRTY_STATUS in _receipt_text(output, check)

    def test_command_that_moves_head_fails_an_otherwise_passing_gate(
        self, repository: Path, head: str, output: Path
    ) -> None:
        """A command that commits during the run fails the gate even though it exits zero.

        The after-run receipt records the new head, so a gate cannot certify source it changed itself.
        """
        command = _python_command(
            "from pathlib import Path\n"
            "import subprocess\n"
            "Path('drift.txt').write_text('drift\\n', encoding='utf-8', newline='\\n')\n"
            "subprocess.run(['git', 'add', 'drift.txt'], check=True)\n"
            f"subprocess.run(['git', 'commit', '-m', 'drift\\n\\n{CODEX_TRAILER}'], check=True)\n"
        )

        result = _run_lint_gate(repository, output, head, command)

        actual_head = _git(repository, "rev-parse", "HEAD")
        check = _gate_check(output, "lint")
        assert result.returncode == 1
        assert actual_head != head
        assert check["status"] == "fail"
        assert check["exit_code"] == 0
        assert check["source"] == {
            "expected_head": head,
            "before": {"head": head, "status": ""},
            "after": {"head": actual_head, "status": ""},
        }
        assert HEAD_MISMATCH in _receipt_text(output, check)

    def test_command_that_leaves_untracked_file_fails_gate_and_validator_rejects_edit(
        self, repository: Path, head: str, output: Path
    ) -> None:
        """A successful command that leaves an untracked file fails the gate, and the receipt cannot be repaired.

        The artifact validator accepts the failed record, then rejects the same record once its ``after`` source receipt
        is rewritten to look clean, because a failed gate with exit code zero needs the dirty evidence.
        """
        command = _python_command(
            "from pathlib import Path\nPath('during-gate.txt').write_text('changed\\n', encoding='utf-8', newline='\\n')"
        )

        result = _run_lint_gate(repository, output, head, command)

        check = _gate_check(output, "lint")
        assert result.returncode == 1
        assert check["status"] == "fail"
        assert check["exit_code"] == 0
        assert check["source"] == {
            "expected_head": head,
            "before": {"head": head, "status": ""},
            "after": {"head": head, "status": "?? during-gate.txt"},
        }
        assert SOURCE_DIRTY_STATUS in _receipt_text(output, check)
        validator = _load_validator()
        assert validator._validate_gates(output)["status"] == "fail"
        _edit_gates_json(
            output,
            lambda payload: payload["checks"][0]["source"].update(after=payload["checks"][0]["source"]["before"]),
        )
        with pytest.raises(SystemExit, match=INVALID_FAIL_EXIT_CODE):
            validator._validate_gates(output)

    def test_non_git_directory_fails_guard_without_running_command(self, tmp_path: Path, output: Path) -> None:
        """Unavailable Git provenance fails the guard instead of being treated as a clean source.

        The invocation directory is not a repository, so the head cannot be read; the command must not run and the
        receipt must carry the inspection error.
        """
        marker = tmp_path / "ran.txt"

        result = _run_lint_gate(tmp_path, output, "a" * 40, _marker_command(marker))

        check = _gate_check(output, "lint")
        source = check["source"]
        assert result.returncode == 1
        assert not marker.exists()
        assert source["expected_head"] == "a" * 40
        assert source["before"]["head"] == ""
        assert source["before"]["status"] == ""
        assert source["before"]["error"]
        assert source["after"] is None
        assert "source-inspection-failed" in _receipt_text(output, check)

    def test_unflagged_run_omits_source_receipts(self, tmp_path: Path, output: Path) -> None:
        """Without ``--expected-head`` a gate run is unchanged and records no source receipt.

        Callers that do not ask for source binding must keep the pre-existing receipt shape.
        """
        result = _run_lint_gate(tmp_path, output, None, _python_command("pass"))

        check = _gate_check(output, "lint")
        assert result.returncode == 0
        assert check["status"] == "pass"
        assert "source" not in check

    @pytest.mark.parametrize(
        ("changed_name", "dependency_git_steps"),
        [
            pytest.param("tracked.txt", [], id="tracked"),
            pytest.param("extra.txt", [], id="untracked"),
            pytest.param(
                "tracked.txt",
                [("add", "tracked.txt"), ("commit", "-m", f"new dependency state\n\n{CODEX_TRAILER}")],
                id="commit",
            ),
        ],
    )
    def test_ignored_submodule_change_blocks_guarded_execution(
        self,
        tmp_path: Path,
        repository: Path,
        output: Path,
        changed_name: str,
        dependency_git_steps: list[tuple[str, ...]],
    ) -> None:
        """A submodule edit, new file or new commit blocks the gate even when ordinary Git status hides it.

        The submodule is configured with ``ignore = all``, so plain ``git status`` is clean; the runner must still see
        the change through ``--ignore-submodules=none`` and refuse to execute the command.
        """
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
        (dependency / changed_name).write_text("changed\n", encoding="utf-8", newline="\n")
        _git_steps(dependency, dependency_git_steps)
        assert _git(repository, "status", "--porcelain=v1", "--untracked-files=all") == ""
        assert _git(repository, "status", "--porcelain=v1", "--ignore-submodules=none")
        marker = tmp_path / "ran.txt"

        result = _run_lint_gate(repository, output, head, _marker_command(marker))

        check = _gate_check(output, "lint")
        assert result.returncode == 1
        assert not marker.exists()
        assert check["status"] == "fail"
        assert check["source"]["before"]["head"] == head
        assert "dependency" in check["source"]["before"]["status"]
        assert check["source"]["after"] is None
        assert SOURCE_DIRTY_STATUS in _receipt_text(output, check)


class TestSelectedWorktree:
    """``--worktree`` selects the checkout that is guarded and used as the command's working directory."""

    def test_wrong_head_in_selected_worktree_blocks_command(self, tmp_path: Path, output: Path) -> None:
        """The selected checkout is checked, not the invocation checkout, before any command runs.

        The invocation repository matches the expected head, but the selected one has a later commit; the gate must fail
        and record the selected path next to the head it actually found.
        """
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
        marker = tmp_path / "ran.txt"

        result = _run_lint_gate(original, output, expected_head, _marker_command(marker), worktree=selected)

        check = _gate_check(output, "lint")
        assert result.returncode == 1
        assert not marker.exists()
        assert check["source"] == {
            "expected_head": expected_head,
            "worktree": selected.resolve().as_posix(),
            "before": {"head": actual_head, "status": ""},
            "after": None,
        }
        assert HEAD_MISMATCH in _receipt_text(output, check)

    def test_same_head_in_other_worktree_retains_checkout_identity(
        self, linked_worktree: tuple[Path, Path, str], output: Path
    ) -> None:
        """The receipt names the selected path even when another checkout has the same Git head.

        Two linked checkouts at one commit are indistinguishable by head alone, so the worktree path is what ties the
        receipt to the reviewed source.
        """
        original, selected, expected_head = linked_worktree

        result = _run_lint_gate(original, output, expected_head, _python_command("pass"), worktree=selected)

        payload = json.loads((output / "gates.json").read_text(encoding="utf-8"))
        source = _gate_check(output, "lint")["source"]
        assert result.returncode == 0
        assert payload["source"] == {"expected_head": expected_head, "worktree": selected.resolve().as_posix()}
        assert source["worktree"] == selected.resolve().as_posix()
        assert source["before"] == {"head": expected_head, "status": ""}
        assert source["after"] == {"head": expected_head, "status": ""}
        assert original.resolve().as_posix() != source["worktree"]

    def test_command_runs_in_selected_worktree(self, linked_worktree: tuple[Path, Path, str], output: Path) -> None:
        """The gate command starts in the selected checkout while the runner keeps its own working directory.

        The command prints its cwd; the stdout receipt must equal the selected path, and no ``gates`` directory may
        appear inside the invocation checkout.
        """
        original, selected, expected_head = linked_worktree
        command = _python_command("from pathlib import Path; print(Path.cwd().resolve().as_posix())")

        result = _run_lint_gate(original, output, expected_head, command, worktree=selected)

        check = _gate_check(output, "lint")
        assert result.returncode == 0
        assert _receipt_text(output, check, "stdout").strip() == selected.resolve().as_posix()
        assert not (original / "gates").exists()

    def test_requires_expected_head_before_writing_receipts(self, repository: Path, output: Path) -> None:
        """Selecting a worktree without ``--expected-head`` is a usage error that writes no receipts.

        A checkout selection without an exact source guard would give a false sense of binding.
        """
        result = _run_lint_gate(repository, output, None, _python_command("pass"), worktree=repository)

        assert result.returncode == 2
        assert "invalid-worktree: --expected-head is required" in result.stderr
        assert not output.exists()

    def test_subdirectory_is_rejected_as_checkout_root(self, repository: Path, head: str, output: Path) -> None:
        """A repository subdirectory is rejected as the selected checkout because it is a misleading identity.

        The gate fails with a dedicated error in the ``before`` receipt and the command never runs.
        """
        nested = repository / "nested"
        nested.mkdir()
        marker = repository.parent / "ran.txt"

        result = _run_lint_gate(repository, output, head, _marker_command(marker), worktree=nested)

        check = _gate_check(output, "lint")
        assert result.returncode == 1
        assert not marker.exists()
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

    def link_editable(self, root: Path) -> None:
        """Point the simulated editable install at the ``src`` directory of one checkout."""
        self.editable_path.write_text(f"{(root / 'src').as_posix()}\n", encoding="utf-8", newline="\n")

    def run_tests(
        self, output: Path, imported_module: str = "demo_package", pytest_arguments: list[str] | None = None
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
            *_skip_gates("tests"),
        ]
        return _run(arguments, cwd=self.original, env=self.environment)


def _package_origin(root: Path) -> str:
    """Return the resolved ``demo_package`` module path a given checkout provides."""
    return (root / "src" / "demo_package" / "__init__.py").resolve().as_posix()


def _demo_test_path(root: Path) -> str:
    """Return the resolved path of the demo test file inside a given checkout."""
    return (root / "tests" / "test_demo.py").resolve().as_posix()


@pytest.fixture
def editable_checkout(tmp_path: Path, request: pytest.FixtureRequest) -> _EditableCheckout:
    """Build a committed checkout with a demo package plus a detached worktree of the same commit.

    Indirect parametrization may set a project ``addopts`` string, or ``"drop-proof"`` to install a conftest that
    removes the worker source receipt before it is delivered.
    """
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


class TestEditableSourceProvenance:
    """The tests gate proves, per import and per worker, that executed source comes from the selected worktree."""

    def test_editable_package_from_invoking_checkout_fails_gate(
        self, editable_checkout: _EditableCheckout, output: Path
    ) -> None:
        """A passing test must not certify an editable package that is imported from the invoking checkout.

        The simulated editable install points at ``original/src`` while the gate reviews ``selected``; the test still
        passes, but the import proof must fail and name the wrong origin.
        """
        editable_checkout.link_editable(editable_checkout.original)

        result = editable_checkout.run_tests(output)

        check = _gate_check(output, "tests")
        assert result.returncode == 1
        assert check["status"] == "fail"
        assert check["python_imports"]["status"] == "fail"
        assert check["python_imports"]["modules"]["demo_package"]["origin"] == _package_origin(
            editable_checkout.original
        )
        assert "1 passed" in _receipt_text(output, check, "stdout")

    def test_editable_package_from_selected_worktree_passes_gate(
        self, editable_checkout: _EditableCheckout, output: Path
    ) -> None:
        """A package and test file that resolve inside the selected worktree certify the gate.

        Both the imported module and the executed test file are tracked and carry a passing proof with no reason.
        """
        editable_checkout.link_editable(editable_checkout.selected)

        result = editable_checkout.run_tests(output)

        check = _gate_check(output, "tests")
        selected_test = _demo_test_path(editable_checkout.selected)
        assert result.returncode == 0, result.stderr
        assert check["status"] == "pass"
        assert check["python_imports"]["status"] == "pass"
        assert check["python_imports"]["modules"]["demo_package"] == {
            "origin": _package_origin(editable_checkout.selected),
            "tracked": True,
            "status": "pass",
            "reason": None,
        }
        assert check["python_imports"]["tests"][selected_test] == {
            "origin": selected_test,
            "tracked": True,
            "status": "pass",
            "reason": None,
        }

    def test_bytecode_is_disabled_so_overlay_recovery_leaves_no_pyc(
        self, editable_checkout: _EditableCheckout, tmp_path: Path, output: Path
    ) -> None:
        """A failed source-bound gate can be recovered with an overlay while its failed attempt is preserved.

        The first run fails because ``PYTEST_ADDOPTS`` names a plugin that is not importable. After the plugin is added
        on an overlay path the same output directory passes, runs Python with ``-B`` and writes no ``.pyc`` into the
        overlay or the reviewed worktree, and the first attempt stays archived.
        """
        editable_checkout.link_editable(editable_checkout.selected)
        editable_checkout.environment["PYTEST_ADDOPTS"] = "-p overlay_probe"
        editable_checkout.environment["PYTHONDONTWRITEBYTECODE"] = "1"
        failed = editable_checkout.run_tests(output)
        assert failed.returncode == 1
        assert "overlay_probe" in (output / "checks/tests.stderr.txt").read_text(encoding="utf-8")
        overlay = tmp_path / "overlay"
        overlay.mkdir()
        (overlay / "overlay_probe.py").write_text("VALUE = 1\n", encoding="utf-8")
        bootstrap = editable_checkout.environment["PYTHONPATH"]
        editable_checkout.environment["PYTHONPATH"] = os.pathsep.join((str(overlay), bootstrap))

        completed = editable_checkout.run_tests(output)

        invocation = (output / "checks/tests.command.txt").read_text(encoding="utf-8").removeprefix("argv-json:")
        prior = json.loads((output / "gate-attempts/001/gates.json").read_text(encoding="utf-8"))
        assert completed.returncode == 0, completed.stderr
        assert _gate_check(output, "tests")["python_imports"]["status"] == "pass"
        assert json.loads(invocation)[1] == "-B"
        assert not list(overlay.rglob("*.pyc"))
        assert not list(editable_checkout.selected.rglob("*.pyc"))
        assert prior["checks_failed"] == ["tests"]

    @pytest.mark.parametrize("editable_checkout", ["-n 2"], indirect=True)
    @pytest.mark.parametrize(
        ("extra_environment", "pytest_arguments"),
        [
            pytest.param({}, ["-q", "tests/test_demo.py", "-o", "addopts=", "-n", "2"], id="cli"),
            pytest.param({}, ["-q", "tests/test_demo.py"], id="project"),
            pytest.param({"PYTEST_ADDOPTS": "-n 2"}, ["-q", "tests/test_demo.py", "-o", "addopts="], id="environment"),
        ],
    )
    def test_parallel_run_proves_worker_sources(
        self,
        editable_checkout: _EditableCheckout,
        tmp_path: Path,
        output: Path,
        extra_environment: dict[str, str],
        pytest_arguments: list[str],
    ) -> None:
        """Selected tests and imports are certified from real xdist workers whichever source enables ``-n 2``.

        The worker count comes from the command line, the project ``pytest.ini`` or ``PYTEST_ADDOPTS``; in each case
        both workers must report a passing source proof and the test must really execute in the selected worktree.
        """
        editable_checkout.link_editable(editable_checkout.selected)
        marker = tmp_path / "selected-test-ran.txt"
        editable_checkout.environment["SOURCE_BOUND_MARKER"] = str(marker)
        editable_checkout.environment.update(extra_environment)

        result = editable_checkout.run_tests(output, pytest_arguments=pytest_arguments)

        proof = _gate_check(output, "tests")["python_imports"]
        selected_test = _demo_test_path(editable_checkout.selected)
        assert result.returncode == 0, result.stderr
        assert marker.read_text(encoding="utf-8") == "executed"
        assert _gate_check(output, "tests")["status"] == "pass"
        assert proof["status"] == "pass"
        assert len(proof["workers"]) == 2
        assert all(worker["status"] != "fail" for worker in proof["workers"].values())
        assert proof["tests"][selected_test]["status"] == "pass"
        assert proof["modules"]["demo_package"]["origin"] == _package_origin(editable_checkout.selected)
        assert any(selected_test in worker["tests"] for worker in proof["workers"].values())
        assert any(worker["modules"]["demo_package"]["status"] == "pass" for worker in proof["workers"].values())

    @pytest.mark.parametrize(
        "pytest_arguments",
        [
            pytest.param(["-q", "-o", "addopts=", "tests/test_demo.py"], id="no-addopts"),
            pytest.param(["-q", "-n", "0", "tests/test_demo.py"], id="explicit-zero-workers"),
        ],
    )
    def test_serial_run_retains_source_proof(
        self, editable_checkout: _EditableCheckout, tmp_path: Path, output: Path, pytest_arguments: list[str]
    ) -> None:
        """Serial pytest, with or without an explicit ``-n 0``, still yields a valid proof and runs the selected test.

        Without xdist workers the proof comes from the single process, so it must pass without a ``workers`` section.
        """
        editable_checkout.link_editable(editable_checkout.selected)
        marker = tmp_path / "selected-test-ran.txt"
        editable_checkout.environment["SOURCE_BOUND_MARKER"] = str(marker)

        result = editable_checkout.run_tests(output, pytest_arguments=pytest_arguments)

        assert result.returncode == 0, result.stderr
        assert marker.read_text(encoding="utf-8") == "executed"
        assert _gate_check(output, "tests")["python_imports"]["status"] == "pass"

    def test_parallel_worker_with_wrong_editable_origin_fails_proof(
        self, editable_checkout: _EditableCheckout, output: Path
    ) -> None:
        """A worker that imports the package from the invoking checkout cannot certify the reviewed source.

        Two xdist workers run against an editable install pointing at ``original/src``; the gate and the module proof
        must both fail and record the wrong origin.
        """
        editable_checkout.link_editable(editable_checkout.original)

        result = editable_checkout.run_tests(output, pytest_arguments=["-q", "-n", "2", "tests/test_demo.py"])

        check = _gate_check(output, "tests")
        proof = check["python_imports"]
        assert result.returncode == 1
        assert check["status"] == "fail"
        assert len(proof["workers"]) == 2
        assert proof["modules"]["demo_package"]["origin"] == _package_origin(editable_checkout.original)
        assert proof["modules"]["demo_package"]["status"] == "fail"

    def test_parallel_external_test_file_fails_proof(self, editable_checkout: _EditableCheckout, output: Path) -> None:
        """Workers that collect a test file outside the selected worktree fail the selected-test provenance.

        The package resolves correctly, but the test path is the invoking checkout's copy, so its proof reason must be
        ``test-outside-worktree``.
        """
        editable_checkout.link_editable(editable_checkout.selected)
        external_test = editable_checkout.original / "tests" / "test_demo.py"

        result = editable_checkout.run_tests(output, pytest_arguments=["-q", "-n", "2", str(external_test)])

        check = _gate_check(output, "tests")
        proof = check["python_imports"]
        assert result.returncode == 1
        assert check["status"] == "fail"
        assert len(proof["workers"]) == 2
        assert proof["tests"][external_test.resolve().as_posix()]["reason"] == "test-outside-worktree"

    @pytest.mark.parametrize("editable_checkout", ["drop-proof"], indirect=True)
    def test_parallel_missing_worker_receipts_fail_closed(
        self, editable_checkout: _EditableCheckout, output: Path
    ) -> None:
        """A passing worker test whose source receipt is removed before delivery fails the gate.

        A project conftest strips the proof from each worker's output, so the runner must report ``worker-proof-
        missing`` rather than assume the sources were fine.
        """
        editable_checkout.link_editable(editable_checkout.selected)

        result = editable_checkout.run_tests(output, pytest_arguments=["-q", "-n", "2", "tests/test_demo.py"])

        check = _gate_check(output, "tests")
        assert result.returncode == 1
        assert check["status"] == "fail"
        assert check["python_imports"]["status"] == "fail"
        assert any(error.startswith("worker-proof-missing:") for error in check["python_imports"]["errors"])

    def test_serial_external_test_file_cannot_certify_selected_worktree(
        self, editable_checkout: _EditableCheckout, output: Path
    ) -> None:
        """A passing serial test taken from outside the reviewed checkout fails the source proof.

        The test file is recorded as untracked with the reason ``test-outside-worktree`` even though pytest reports it
        as passed.
        """
        editable_checkout.link_editable(editable_checkout.selected)
        external_test = editable_checkout.original / "tests" / "test_demo.py"

        result = editable_checkout.run_tests(output, pytest_arguments=["-q", "-o", "addopts=", str(external_test)])

        check = _gate_check(output, "tests")
        assert result.returncode == 1
        assert check["status"] == "fail"
        assert check["python_imports"]["status"] == "fail"
        assert check["python_imports"]["tests"][external_test.resolve().as_posix()] == {
            "origin": external_test.resolve().as_posix(),
            "tracked": False,
            "status": "fail",
            "reason": "test-outside-worktree",
        }
        assert "1 passed" in _receipt_text(output, check, "stdout")

    def test_unimported_declared_module_is_inconclusive(
        self, editable_checkout: _EditableCheckout, output: Path
    ) -> None:
        """A declared import module that the test run never loads yields inconclusive provenance.

        The gate asks about ``unloaded_package``, which no test imports, so no origin can be proven and the gate fails.
        """
        result = editable_checkout.run_tests(output, "unloaded_package")

        check = _gate_check(output, "tests")
        assert result.returncode == 1
        assert check["status"] == "fail"
        assert check["python_imports"]["status"] == "inconclusive"
        assert check["python_imports"]["modules"]["unloaded_package"]["reason"] == "module-not-imported"


@dataclass
class _LocalGate:
    """A dirty local repository, its reviewed mirror, the receipts directory and an import-bound gate invocation."""

    repository: Path
    output: Path
    mirror: Path
    args: list[str]


def _local_gate_inputs(
    tmp_path: Path,
    *,
    committed: bool = True,
    gitlink: bool = False,
    conftest: str = "",
    pytest_arguments: list[str] | None = None,
) -> _LocalGate:
    """Build a real collector mirror and its explicit import-bound gate invocation.

    ``committed`` selects whether ``localmod.py`` and its test were committed before the working-tree edit or stay
    untracked, ``gitlink`` adds a committed gitlink entry named ``dependency``, and ``conftest`` is written uncommitted.
    """
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
    if committed:
        _git(repository, "add", "localmod.py", "test_localmod.py")
        if gitlink:
            _git(repository, "update-index", "--add", "--cacheinfo", f"160000,{head},dependency")
            (repository / "dependency").mkdir()
        _git(repository, "commit", "-m", f"runtime files\n\n{CODEX_TRAILER}")
    module.write_text("value = 2\n", encoding="utf-8")
    if conftest:
        (repository / "conftest.py").write_text(conftest, encoding="utf-8", newline="\n")
    output = tmp_path / "review"
    collected = _run(
        [
            sys.executable,
            str(PLUGIN_ROOT / "shared/collect_diff.py"),
            "--review-worktree",
            "--repository",
            str(repository),
            "--out",
            str(output / "local-source"),
        ]
    )
    assert collected.returncode == 0, collected.stderr
    mirror = Path(json.loads((output / "local-source/review-worktree.json").read_bytes())["review_worktree"])
    if gitlink:
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
        json.dumps(pytest_arguments or ["-q", "-p", "no:xdist", "-o", "addopts=", "test_localmod.py"]),
        *_skip_gates("tests"),
    ]
    return _LocalGate(repository, output, mirror, args)


def _move_gitlink_pointer(gate: _LocalGate) -> Path:
    """Alter the mirror's recorded gitlink identity and return the mirror as the worktree to run in."""
    _git(gate.mirror, "update-index", "--cacheinfo", f"160000,{_git(gate.mirror, 'rev-parse', 'HEAD')},dependency")
    return gate.mirror


def _use_unreceipted_checkout(gate: _LocalGate) -> Path:
    """Return the original repository, which has no collector receipt, as the worktree to run in."""
    return gate.repository


def _expect_validator_rejects_stripped_guard_error(
    validator: Any, output: Path, check: dict[str, Any], stderr: bytes, guard_error: str
) -> None:
    """Remove the guard error from the stderr receipt and require the validator to reject the inconsistent record."""
    (output / str(check["stderr"])).write_bytes(stderr.replace(guard_error.encode("utf-8"), b""))
    with pytest.raises(SystemExit, match=INVALID_FAIL_EXIT_CODE):
        validator._validate_gates(output)


def _accept_validator_state(
    validator: Any, output: Path, check: dict[str, Any], stderr: bytes, guard_error: str
) -> None:
    """No extra check: a nonzero command exit already explains the failed gate."""


@dataclass(frozen=True)
class _GuardCase:
    """One child behaviour whose raw output and failure disposition must survive the post-run source guard."""

    body: str
    raw_prefix: bytes
    gate_status: str
    exit_code: int
    runner_exit: int
    validator_status: str = "fail"
    timeout_seconds: str = "20"
    reason_pattern: str = ".*"
    tamper_check: Callable[..., None] = _accept_validator_state


class TestLocalMirror:
    """Gates run against a local review mirror bind their results to the exact mirror bytes and receipts."""

    @pytest.mark.parametrize(
        ("committed", "gitlink"),
        [
            pytest.param(True, False, id="tracked"),
            pytest.param(False, False, id="added"),
            pytest.param(True, True, id="gitlink"),
        ],
    )
    def test_import_bound_gate_preserves_admitted_source(self, tmp_path: Path, committed: bool, gitlink: bool) -> None:
        """The mirror's dirty bytes, added files included, are imported and unchanged gitlinks are excluded.

        The test imports a module edited in the working tree, so passing proves the gate ran the mirror and not the
        committed copy; the sidecar proof must match ``gates.json`` and its hash must be recorded.
        """
        gate = _local_gate_inputs(tmp_path, committed=committed, gitlink=gitlink)

        result = _run(gate.args)

        check = _gate_check(gate.output, "tests")
        proof = check["python_imports"]
        sidecar = (gate.output / "checks/tests.python-imports.json").read_bytes()
        snapshot = json.loads((gate.output / "checks/local-source-snapshot.json").read_bytes())
        assert result.returncode == 0, result.stderr
        assert check["source"]["mode"] == "local-review-mirror"
        assert check["source"]["before"] == check["source"]["after"]
        assert json.loads(sidecar) == proof
        assert check["source"]["artifacts"]["checks/tests.python-imports.json"] == hashlib.sha256(sidecar).hexdigest()
        assert proof["modules"]["localmod"]["tracked"] is committed
        assert proof["modules"]["localmod"]["snapshot_member"]["path"] == "localmod.py"
        assert proof["tests"][(gate.mirror / "test_localmod.py").as_posix()]["tracked"] is committed
        assert all(record["path"] != "dependency" for record in snapshot["files"])
        assert bool(snapshot["gitlinks"]) is gitlink

    def test_rejects_source_changed_during_run(self, tmp_path: Path) -> None:
        """A command that edits the mirror while it runs fails the gate and the failure cannot be edited away.

        The command does run (its marker exists) but the post-run guard fails the gate; deleting ``guard_error`` from
        the record afterwards makes the validator reject it, so the evidence cannot be silently removed.
        """
        gate = _local_gate_inputs(tmp_path, committed=False)
        marker = tmp_path / "executed.txt"
        command = _python_command(
            f"from pathlib import Path; Path({str(marker)!r}).write_text('ran'); "
            f"Path({str(gate.mirror / 'localmod.py')!r}).write_text('value = 99\\n')"
        )

        result = _run_lint_gate(gate.repository, gate.output, None, command, gate.mirror)

        validator = _load_validator()
        assert result.returncode != 0
        assert marker.exists()
        assert _gate_check(gate.output, "lint")["status"] == "fail"
        assert validator._validate_gates(gate.output)["status"] == "fail"
        _edit_gates_json(gate.output, lambda payload: payload["checks"][0]["source"].pop("guard_error"))
        with pytest.raises(SystemExit, match=INVALID_FAIL_EXIT_CODE):
            validator._validate_gates(gate.output)

    @pytest.mark.parametrize(
        ("gitlink", "choose_worktree"),
        [
            pytest.param(True, _move_gitlink_pointer, id="gitlink-pointer"),
            pytest.param(False, _use_unreceipted_checkout, id="unreceipted-root"),
        ],
    )
    def test_rejects_unadmitted_source_before_running(
        self, tmp_path: Path, gitlink: bool, choose_worktree: Callable[[_LocalGate], Path]
    ) -> None:
        """A mirror with an altered gitlink identity, or a checkout with no collector receipt, never runs the command.

        Local source execution only trusts the mirror that the collector produced; any other worktree must fail closed
        with a nonzero status before the command can leave its marker.
        """
        gate = _local_gate_inputs(tmp_path, committed=gitlink, gitlink=gitlink)
        marker = tmp_path / "executed.txt"
        worktree = choose_worktree(gate)

        result = _run_lint_gate(gate.repository, gate.output, None, _marker_command(marker), worktree)

        assert result.returncode != 0
        assert not marker.exists()

    @pytest.mark.parametrize(
        "case",
        [
            pytest.param(
                _GuardCase(
                    "sys.stderr.buffer.write(b'\\xff')",
                    b"\xff",
                    "fail",
                    0,
                    1,
                    tamper_check=_expect_validator_rejects_stripped_guard_error,
                ),
                id="invalid-utf8",
            ),
            pytest.param(
                _GuardCase(
                    "sys.stderr.buffer.write(b'x' * (1048576 - 1) + b'\\xe2\\x82\\xac')",
                    b"x" * (1048576 - 1) + b"\xe2\n[output truncated after 1048576 bytes]\n",
                    "fail",
                    0,
                    1,
                    tamper_check=_expect_validator_rejects_stripped_guard_error,
                ),
                id="truncated-utf8",
            ),
            pytest.param(_GuardCase("sys.exit(1)", b"", "fail", 1, 1), id="command-failed"),
            pytest.param(
                _GuardCase("sys.exit(127)", b"", "missing-command", 127, 1, reason_pattern=".+"),
                id="missing-command",
            ),
            pytest.param(
                _GuardCase(
                    "time.sleep(20)",
                    b"",
                    "timeout",
                    124,
                    124,
                    validator_status="timeout",
                    timeout_seconds="1",
                    reason_pattern="timeout after 1 seconds",
                ),
                id="timeout",
            ),
        ],
    )
    def test_source_guard_preserves_external_output_and_failure(self, tmp_path: Path, case: _GuardCase) -> None:
        """Raw child bytes and the command's failure disposition survive alongside the post-run source guard.

        Each child edits the mirror and then misbehaves in one way (invalid or truncated UTF-8, nonzero exit, missing
        command, timeout). The stderr receipt must keep the child's bytes verbatim before appending the guard error, its
        hash must be recorded, and the validator must agree with the runner on the overall status.
        """
        gate = _local_gate_inputs(tmp_path, committed=False)
        command = _python_command(
            "import sys,time;from pathlib import Path;"
            f"Path({str(gate.mirror / 'localmod.py')!r}).write_bytes(b'value = 99\\n');{case.body}"
        )
        arguments = [
            sys.executable,
            str(RUN_GATES),
            "--out",
            str(gate.output),
            "--worktree",
            str(gate.mirror),
            "--timeout-seconds",
            case.timeout_seconds,
            "--lint",
            command,
            *_skip_gates("lint"),
        ]

        completed = _run(arguments, cwd=gate.repository)

        check = _gate_check(gate.output, "lint")
        guard_error = check["source"]["guard_error"]
        stderr = (gate.output / str(check["stderr"])).read_bytes()
        validator = _load_validator()
        gates = validator._validate_gates(gate.output)
        assert completed.returncode == case.runner_exit, completed.stderr
        assert check["status"] == case.gate_status
        assert check["exit_code"] == case.exit_code
        assert guard_error
        assert stderr.startswith(case.raw_prefix)
        assert guard_error.encode("utf-8") in stderr
        assert check["source"]["artifacts"][check["stderr"]] == hashlib.sha256(stderr).hexdigest()
        assert re.fullmatch(case.reason_pattern, str(check.get("reason") or ""))
        assert gates["status"] == case.validator_status
        assert gates["checks_failed"] == ["lint"]
        case.tamper_check(validator, gate.output, check, stderr, guard_error)

    def test_parallel_workers_share_admitted_source_proof(self, tmp_path: Path) -> None:
        """Real xdist workers each retain the same admitted untracked module and test byte membership.

        Both workers must report the same ``local_source`` receipt as the aggregate, and the untracked files must be
        recorded as untracked for every worker.
        """
        gate = _local_gate_inputs(
            tmp_path,
            committed=False,
            pytest_arguments=["-q", "-n", "2", "-p", "no:cacheprovider", "-o", "addopts=", "test_localmod.py"],
        )

        result = _run(gate.args)

        proof = _gate_check(gate.output, "tests")["python_imports"]
        assert result.returncode == 0, result.stderr
        assert set(proof["workers"]) == {"gw0", "gw1"}
        assert all(worker["local_source"] == proof["local_source"] for worker in proof["workers"].values())
        assert proof["modules"]["localmod"]["tracked"] is False
        assert all(record["tracked"] is False for record in proof["tests"].values())

    @pytest.mark.parametrize(
        ("mutate", "expected_reads"),
        [
            pytest.param(lambda path, content: path.unlink(), 1, id="deleted"),
            pytest.param(lambda path, content: path.write_bytes(content + b" "), 2, id="changed"),
        ],
    )
    @pytest.mark.parametrize(
        ("conftest", "expected_status", "expected_exit", "timeout_seconds", "reason_pattern"),
        [
            pytest.param("", "fail", 0, 60, ".*", id="pass"),
            pytest.param(
                "def pytest_sessionfinish(session):\n    session.exitstatus = 127\n",
                "missing-command",
                127,
                60,
                ".+",
                id="missing-command",
            ),
            pytest.param(
                "import time\ndef pytest_unconfigure(config):\n    time.sleep(20)\n",
                "timeout",
                124,
                3,
                ".+",
                id="timeout",
            ),
        ],
    )
    def test_rejects_child_sidecar_drift(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        mutate: Callable[[Path, bytes], None],
        expected_reads: int,
        conftest: str,
        expected_status: str,
        expected_exit: int,
        timeout_seconds: int,
        reason_pattern: str,
    ) -> None:
        """A child proof file that is changed or deleted during real post-run capture is rejected, not hashed.

        The import-proof sidecar is mutated right after the runner first reads it, for a passing, a missing-command and
        a timed-out child. The proof the runner already consumed stays valid, but the gate must fail with the ``local-
        gate-proof-artifact-changed`` error instead of recording a hash of mixed evidence.
        """
        gate = _local_gate_inputs(tmp_path, committed=False, conftest=conftest)
        gates = _load_run_gates(monkeypatch)
        local_source = _local_source_receipt(gates, gate.output, gate.mirror)
        reads = _mutate_sidecar_after_first_read(monkeypatch, gate.output / "checks/tests.python-imports.json", mutate)

        result = gates.run_check(
            "tests",
            "",
            "",
            timeout_seconds,
            gate.output,
            None,
            gate.mirror,
            gates.PythonTestSpec(
                Path(sys.executable), ("localmod",), ("-q", "-p", "no:xdist", "-o", "addopts=", "test_localmod.py")
            ),
            {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            local_source,
        )

        assert result["python_imports"]["status"] == "pass"
        assert result["source"]["before"] == result["source"]["after"]
        assert result["status"] == expected_status
        assert result["exit_code"] == expected_exit
        assert re.fullmatch(reason_pattern, str(result.get("reason") or ""))
        assert reads[0] == expected_reads
        assert "local-gate-proof-artifact-changed" in (gate.output / "checks/tests.stderr.txt").read_text(
            encoding="utf-8"
        )


def _load_run_gates(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Import ``run_gates.py`` as a module whose registration in ``sys.modules`` is undone after the test."""
    monkeypatch.syspath_prepend(str(PLUGIN_ROOT / "shared"))
    specification = importlib.util.spec_from_file_location("sidecar_gate_runner", RUN_GATES)
    module = importlib.util.module_from_spec(specification)
    monkeypatch.setitem(sys.modules, specification.name, module)
    specification.loader.exec_module(module)
    return module


def _local_source_receipt(gates: Any, output: Path, mirror: Path) -> dict[str, str]:
    """Write the local source snapshot for ``mirror`` and return the receipt describing it for ``run_check``."""
    (output / "checks").mkdir()
    content = gates._local_snapshot_bytes(gates.validate_local_gate_source(output, mirror))
    (output / "checks/local-source-snapshot.json").write_bytes(content)
    return {
        "mode": "local-review-mirror",
        "worktree": mirror.as_posix(),
        "receipt_path": "local-source/review-worktree.json",
        "receipt_sha256": hashlib.sha256((output / "local-source/review-worktree.json").read_bytes()).hexdigest(),
        "snapshot_path": "checks/local-source-snapshot.json",
        "snapshot_sha256": hashlib.sha256(content).hexdigest(),
    }


def _mutate_sidecar_after_first_read(
    monkeypatch: pytest.MonkeyPatch, sidecar: Path, mutate: Callable[[Path, bytes], None]
) -> list[int]:
    """Make ``mutate(path, bytes)`` run when ``sidecar`` is read, and return a one-item list counting its reads.

    Both ``Path.read_bytes`` and ``Path.read_text`` are wrapped for exactly that path so the change lands after the
    runner has consumed the proof once, whichever reader it uses. The bytes reader mutates only on its first read.
    """
    reads = [0]
    read_bytes = Path.read_bytes
    read_text = Path.read_text

    def read_and_change_proof(path: Path) -> bytes:
        """Read actual bytes, then synchronize an exact-path sidecar mutation after initial consumption."""
        content = read_bytes(path)
        if path == sidecar:
            reads[0] += 1
            if reads[0] == 1:
                assert json.loads(content)["status"] == "pass"
                mutate(path, content)
        return content

    def read_text_and_change_proof(path: Path, *arguments: Any, **keywords: Any) -> str:
        """Apply the same exact-path timing control to the historical text reader."""
        content = read_text(path, *arguments, **keywords)
        if path == sidecar:
            reads[0] += 1
            assert json.loads(content)["status"] == "pass"
            mutate(path, content.encode("utf-8"))
        return content

    monkeypatch.setattr(Path, "read_text", read_text_and_change_proof)
    monkeypatch.setattr(Path, "read_bytes", read_and_change_proof)
    return reads

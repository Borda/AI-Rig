"""Exercise gate child isolation and clean-source evidence against real subprocesses."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType

import pytest
from _modules import load_module_scoped

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
RUNNER = PLUGIN_ROOT / "shared/run_gates.py"


def _symlinks_supported() -> bool:
    """Probe whether the host can create the links needed by Git fixture tests."""
    with tempfile.TemporaryDirectory() as directory:
        link = Path(directory) / "link"
        try:
            link.symlink_to("target")
        except OSError:
            return False
        return link.is_symlink()


_skip_without_symlinks = pytest.mark.skipif(not _symlinks_supported(), reason="symlink capability unavailable")


@pytest.fixture
def gates(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Load the gate runner with its installed sibling imports available."""
    monkeypatch.syspath_prepend(str(PLUGIN_ROOT / "shared"))
    return load_module_scoped("input_safety_gates", RUNNER, monkeypatch)


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """Create a committed source fixture with one ignored runtime filename."""
    repository = tmp_path / "source"
    repository.mkdir()
    (repository / "tracked.py").write_text("value = 1\n", encoding="utf-8")
    (repository / "test_tracked.py").write_text(
        "import tracked\ndef test_value():\n    assert tracked.value == 1\n", encoding="utf-8"
    )
    (repository / ".gitignore").write_text("conftest.py\n.pytest_cache/\n__pycache__/\n", encoding="utf-8")
    commands = (
        ("init", "-q"),
        ("config", "user.name", "Gate Fixture"),
        ("config", "user.email", "fixture@example.invalid"),
        ("add", "tracked.py", "test_tracked.py", ".gitignore"),
        ("commit", "-qm", "fixture\n\nCo-authored-by: Codex <codex@openai.com>"),
    )
    for arguments in commands:
        subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True)
    return repository


def test_review_requires_explicit_scope_or_absence_reason(tmp_path: Path) -> None:
    """An absent review command records missing scope without inspecting unrelated whitespace."""
    arguments = [sys.executable, str(RUNNER), "--out", str(tmp_path / "out")]
    skips = [part for gate in ("lint", "format", "types", "tests") for part in (f"--skip-{gate}", "out of scope")]
    environment = {key: value for key, value in os.environ.items() if key != "REVIEW_CMD"}
    completed = subprocess.run(arguments + skips, cwd=tmp_path, env=environment, capture_output=True, check=False)
    payload = json.loads((tmp_path / "out/gates.json").read_text(encoding="utf-8"))
    review = next(check for check in payload["checks"] if check["id"] == "review")

    assert completed.returncode == 1
    assert review["status"] == "missing-command"
    assert (tmp_path / "out/checks/review.command.txt").read_text(encoding="utf-8") == ""


@pytest.mark.parametrize(
    "explicit_environment", [pytest.param(False, id="inherited"), pytest.param(True, id="explicit")]
)
def test_gate_child_cannot_read_parent_credentials(
    gates: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit_environment: bool
) -> None:
    """Real gate children cannot print credential sentinels through either environment route."""
    monkeypatch.setenv("GH_TOKEN", "credential-sentinel")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "credential-sentinel")
    monkeypatch.setenv("UNLISTED_CREDENTIAL", "credential-sentinel")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    command = [
        sys.executable,
        "-c",
        "import json, os; print(json.dumps({k: os.environ[k] for k in "
        "('GH_TOKEN', 'AWS_SECRET_ACCESS_KEY', 'UNLISTED_CREDENTIAL', 'TMPDIR', 'PATH') if k in os.environ}))",
    ]
    output, error = tmp_path / "stdout.txt", tmp_path / "stderr.txt"
    code, _ = gates.execute_command(
        command, 10, output, error, environment=dict(os.environ) if explicit_environment else None
    )
    observed = json.loads(output.read_text(encoding="utf-8"))

    assert code == 0
    assert "credential-sentinel" not in output.read_text(encoding="utf-8")
    assert "credential-sentinel" not in error.read_text(encoding="utf-8")
    assert observed["TMPDIR"] == str(tmp_path)
    assert observed["PATH"] == os.environ["PATH"]


def test_posix_gate_shell_does_not_load_login_profiles(gates: ModuleType) -> None:
    """POSIX command execution selects Bash without login startup files."""
    assert gates.command_argv("printf ok", platform="linux") == ["bash", "-c", "printf ok"]


def test_ignored_runtime_file_fails_clean_source_guard(gates: ModuleType, repository: Path) -> None:
    """A Git-ignored conftest is retained as dirtiness in the source receipt."""
    (repository / "conftest.py").write_text("raise RuntimeError('injected')\n", encoding="utf-8")
    snapshot = gates.inspect_source(repository)

    assert snapshot["status"] == ""
    assert "conftest.py" in json.loads(snapshot["ignored"])
    assert gates.source_guard_error(snapshot, snapshot["head"]).startswith("source-dirty:")


def test_import_bound_pytest_keeps_cache_outside_clean_source(
    gates: ModuleType, repository: Path, tmp_path: Path
) -> None:
    """A passing import-bound test retains its cache under checks without dirtying the reviewed source."""
    output = tmp_path / "out"
    skipped = [part for gate in ("lint", "format", "types", "review") for part in (f"--skip-{gate}", "out of scope")]
    arguments = [
        sys.executable,
        str(RUNNER),
        "--out",
        str(output),
        "--worktree",
        str(repository),
        "--expected-head",
        gates.inspect_source(repository)["head"],
        "--pytest-python",
        sys.executable,
        "--pytest-import",
        "tracked",
        "--pytest-args-json",
        json.dumps(["-q", "-p", "no:xdist", "test_tracked.py"]),
        *skipped,
    ]
    completed = subprocess.run(arguments, cwd=repository, capture_output=True, check=False)

    assert completed.returncode == 0, completed.stderr
    assert (output / "checks/pytest-cache").is_dir()
    assert not (repository / ".pytest_cache").exists()
    assert gates.inspect_source(repository)["status"] == ""


@pytest.mark.parametrize(
    "flag",
    [pytest.param("--assume-unchanged", id="assume-unchanged"), pytest.param("--skip-worktree", id="skip-worktree")],
)
def test_concealed_index_entry_fails_clean_source_guard(gates: ModuleType, repository: Path, flag: str) -> None:
    """Index flags cannot conceal modified runtime bytes from a clean-source receipt."""
    subprocess.run(["git", "update-index", flag, "--", "tracked.py"], cwd=repository, check=True, capture_output=True)
    (repository / "tracked.py").write_text("value = 'injected'\n", encoding="utf-8")
    snapshot = gates.inspect_source(repository)

    assert gates.source_guard_error(snapshot, snapshot["head"]) is not None
    assert "tracked.py" in snapshot.get("error", "") + snapshot["status"]


def test_gate_environment_preserves_configuration_without_credentials(gates: ModuleType) -> None:
    """Operational configuration survives while tokens and authenticated URLs are removed."""
    environment = {
        "CI": "true",
        "SSL_CERT_FILE": "cert.pem",
        "HTTPS_PROXY": "https://proxy.invalid:8080",
        "http_proxy": "http://proxy.invalid:8080",
        "UV_OFFLINE": "1",
        "PIP_INDEX_URL": "https://user:password@index.invalid/simple",
        "UV_INDEX_PRIVATE_PASSWORD": "secret",
        "GH_TOKEN": "secret",
    }

    assert gates.gate_environment(environment) == {
        "CI": "true",
        "SSL_CERT_FILE": "cert.pem",
        "HTTPS_PROXY": "https://proxy.invalid:8080",
        "http_proxy": "http://proxy.invalid:8080",
        "UV_OFFLINE": "1",
    }


@pytest.mark.integration
def test_free_form_pytest_passes_with_expected_head(gates: ModuleType, repository: Path, tmp_path: Path) -> None:
    """A clean source remains acceptable when free-form pytest writes ignored tool caches."""
    output = tmp_path / "out"
    skipped = [part for gate in ("lint", "format", "types", "review") for part in (f"--skip-{gate}", "out of scope")]
    executable = (
        "& '" + sys.executable.replace("'", "''") + "'" if sys.platform == "win32" else shlex.quote(sys.executable)
    )
    arguments = [
        sys.executable,
        str(RUNNER),
        "--tests",
        f"{executable} -m pytest -q -p no:xdist",
        "--out",
        str(output),
        "--worktree",
        str(repository),
        "--expected-head",
        gates.inspect_source(repository)["head"],
        *skipped,
    ]
    completed = subprocess.run(arguments, cwd=repository, capture_output=True, check=False)
    payload = json.loads((output / "gates.json").read_text(encoding="utf-8"))
    result = next(check for check in payload["checks"] if check["id"] == "tests")

    assert completed.returncode == 0, completed.stderr
    assert result["status"] == "pass", (output / result["stderr"]).read_text(encoding="utf-8")
    assert (repository / ".pytest_cache").is_dir()
    assert gates.inspect_source(repository)["status"] == ""


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_unchanged_flagged_file_passes_source_guard(gates: ModuleType, repository: Path, flag: str) -> None:
    """Index flags alone cannot make identical tracked bytes fail the source guard."""
    subprocess.run(["git", "update-index", flag, "--", "tracked.py"], cwd=repository, check=True, capture_output=True)
    snapshot = gates.inspect_source(repository)

    assert gates.source_guard_error(snapshot, snapshot["head"]) is None


@pytest.mark.integration
def test_sparse_checkout_passes_source_guard(gates: ModuleType, repository: Path) -> None:
    """Excluded skip-worktree entries in a real sparse checkout remain clean."""
    subprocess.run(
        ["git", "sparse-checkout", "set", "--no-cone", "/test_tracked.py"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    snapshot = gates.inspect_source(repository)

    assert not (repository / "tracked.py").exists()
    assert gates.source_guard_error(snapshot, snapshot["head"]) is None


@pytest.mark.parametrize(
    "filename", ["module.py", "site.pth", "conftest.py", "pyproject.toml", "setup.cfg", "pytest.ini"]
)
def test_ignored_import_or_config_file_fails_source_guard(gates: ModuleType, repository: Path, filename: str) -> None:
    """An ignored import or tool configuration cannot silently affect a guarded gate."""
    (repository / ".git/info/exclude").write_text("runtime/\n", encoding="utf-8")
    (repository / "runtime").mkdir()
    (repository / "runtime" / filename).write_text("injected", encoding="utf-8")
    snapshot = gates.inspect_source(repository)

    assert snapshot["status"] == ""
    assert json.loads(snapshot["ignored"]) == [f"runtime/{filename}"]
    assert gates.source_guard_error(snapshot, snapshot["head"]).startswith("source-dirty:ignored-inputs:")


@pytest.mark.parametrize("cache", [".venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"])
def test_ignored_tool_cache_cannot_fail_source_guard(gates: ModuleType, repository: Path, cache: str) -> None:
    """Even importable-looking files in ignored tool caches remain outside the source guard."""
    (repository / ".git/info/exclude").write_text(f"{cache}/\n", encoding="utf-8")
    (repository / cache).mkdir()
    (repository / cache / "runtime.py").write_text("cached", encoding="utf-8")
    snapshot = gates.inspect_source(repository)

    assert gates.source_guard_error(snapshot, snapshot["head"]) is None


def test_ignored_non_input_is_recorded_without_failing(gates: ModuleType, repository: Path) -> None:
    """Ignored report artifacts are inventoried separately and do not dirty reviewed source."""
    (repository / ".git/info/exclude").write_text("report.txt\n", encoding="utf-8")
    (repository / "report.txt").write_text("output", encoding="utf-8")
    snapshot = gates.inspect_source(repository)

    assert json.loads(snapshot["ignored"]) == ["report.txt"]
    assert gates.source_guard_error(snapshot, snapshot["head"]) is None


@pytest.mark.integration
def test_gate_creating_ignored_input_fails_after_execution(gates: ModuleType, repository: Path, tmp_path: Path) -> None:
    """An ignored import created by a passing command invalidates its after-run source receipt."""
    output = tmp_path / "out"
    (output / "checks").mkdir(parents=True)
    executable = (
        "& '" + sys.executable.replace("'", "''") + "'" if sys.platform == "win32" else shlex.quote(sys.executable)
    )
    command = f"{executable} -c \"from pathlib import Path; Path('conftest.py').write_text('injected')\""
    result = gates.run_check("tests", command, "", 30, output, gates.inspect_source(repository)["head"], repository)

    assert result["exit_code"] == 0
    assert result["status"] == "fail"
    assert json.loads(result["source"]["before"]["ignored"]) == []
    assert json.loads(result["source"]["after"]["ignored"]) == ["conftest.py"]
    assert "source-dirty:ignored-inputs:" in (output / result["stderr"]).read_text(encoding="utf-8")


@pytest.fixture
def flagged_symlink_repository(repository: Path) -> Path:
    """Commit and flag a symlink whose index blob stores its literal destination."""
    (repository / "link.py").symlink_to("tracked.py")
    subprocess.run(["git", "add", "--", "link.py"], cwd=repository, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-qm", "link fixture\n\nCo-authored-by: Codex <codex@openai.com>"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "update-index", "--assume-unchanged", "--", "link.py"], cwd=repository, check=True, capture_output=True
    )
    return repository


@_skip_without_symlinks
@pytest.mark.integration
@pytest.mark.parametrize(
    ("destination", "clean"),
    [pytest.param("tracked.py", True, id="unchanged-link"), pytest.param("test_tracked.py", False, id="changed-link")],
)
def test_flagged_symlink_compares_literal_destination(
    gates: ModuleType, flagged_symlink_repository: Path, destination: str, clean: bool
) -> None:
    """Flagged symlinks compare link bytes with the index instead of following their targets."""
    repository = flagged_symlink_repository
    link = repository / "link.py"
    link.unlink()
    link.symlink_to(destination)
    snapshot = gates.inspect_source(repository)

    assert (gates.source_guard_error(snapshot, snapshot["head"]) is None) is clean


@_skip_without_symlinks
@pytest.mark.integration
@pytest.mark.parametrize("symlinks", [True, False])
def test_flagged_symlink_file_kind_respects_git_configuration(
    gates: ModuleType, flagged_symlink_repository: Path, symlinks: bool
) -> None:
    """Identical regular-file bytes replace a symlink only when Git emulates symlinks."""
    repository = flagged_symlink_repository
    subprocess.run(
        ["git", "config", "core.symlinks", str(symlinks).lower()], cwd=repository, check=True, capture_output=True
    )
    link = repository / "link.py"
    link.unlink()
    link.write_bytes(b"tracked.py")
    snapshot = gates.inspect_source(repository)

    assert snapshot["status"] == ""
    assert (gates.source_guard_error(snapshot, snapshot["head"]) is None) is (not symlinks)


@pytest.mark.integration
@pytest.mark.parametrize("filemode", [True, False])
def test_flagged_executable_mode_respects_git_configuration(
    gates: ModuleType, repository: Path, filemode: bool
) -> None:
    """A concealed executable-bit change is rejected when Git tracks executable semantics."""
    subprocess.run(
        ["git", "config", "core.filemode", str(filemode).lower()], cwd=repository, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "update-index", "--chmod=+x", "--", "tracked.py"], cwd=repository, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-qm", "executable fixture\n\nCo-authored-by: Codex <codex@openai.com>"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "update-index", "--assume-unchanged", "--", "tracked.py"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    # Keeping the physical file non-executable also exercises hosts that cannot toggle executable bits.
    (repository / "tracked.py").chmod(0o644)
    snapshot = gates.inspect_source(repository)

    assert snapshot["status"] == ""
    assert (gates.source_guard_error(snapshot, snapshot["head"]) is None) is (not filemode)

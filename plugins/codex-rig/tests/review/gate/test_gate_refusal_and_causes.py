"""One source refusal per attempt, environment-aware failure causes, recorded timeouts and a writable tool cache.

The gate runner observes a source-bound checkout before its first executable gate. A refusal there is recorded once at
the top of ``gates.json`` and no gate command runs. Ignored files that a gate cannot read as source, such as the
workflows' own report evidence or a virtual environment marked by ``pyvenv.cfg``, never cause that refusal. Every gate
that did not pass names a cause, every executable gate keeps its timeout, and pre-commit and uv receive a writable cache
outside the checkout when one is requested.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import ModuleType

import pytest
from _gate_runner_support import (
    RUN_GATES,
    _gate_check,
    _load_run_gates,
    _load_validator,
    _marker_command,
    _python_command,
    _run,
    _skip_gates,
)


@pytest.fixture
def gates(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Load the gate runner with its installed sibling imports available."""
    return _load_run_gates(monkeypatch)


def _ignore(repository: Path, *patterns: str) -> None:
    """Ignore ``patterns`` through the repository's local exclude file, leaving tracked content unchanged."""
    (repository / ".git/info/exclude").write_text("".join(f"{pattern}\n" for pattern in patterns), encoding="utf-8")


def _write(path: Path, content: str = "value = 1\n") -> None:
    """Create ``path`` and its parent directories with ``content``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _two_gate_run(repository: Path, output: Path, head: str, first: Path, second: Path) -> tuple[int, dict]:
    """Run lint and tests as marker commands against ``head`` and return the exit status and ``gates.json``."""
    skipped = [part for gate in ("format", "types", "review") for part in (f"--skip-{gate}", "out of scope")]
    completed = _run(
        [
            sys.executable,
            str(RUN_GATES),
            "--out",
            str(output),
            "--worktree",
            str(repository),
            "--expected-head",
            head,
            "--timeout-seconds",
            "321",
            "--lint",
            _marker_command(first),
            "--tests",
            _marker_command(second),
            *skipped,
        ],
        cwd=repository,
    )
    return completed.returncode, json.loads((output / "gates.json").read_text(encoding="utf-8"))


class TestIgnoredInputReachability:
    """Only ignored files a gate can read as project source count as ignored inputs."""

    @pytest.mark.parametrize("name", ["pytest.toml", ".pytest.toml"])
    def test_ignored_pytest_configuration_refuses(self, gates, repository, name):
        """Pytest configuration that can deselect tests must be part of the guarded source."""
        _ignore(repository, name)
        _write(repository / name, '[pytest]\naddopts = ["-k", "passing"]\n')
        snapshot = gates.inspect_source(repository)

        assert snapshot["status"] == ""
        assert gates.source_guard_error(snapshot, snapshot["head"]).startswith("source-dirty:ignored-inputs:")

    @pytest.mark.parametrize(
        "relative",
        [
            pytest.param(".reports/codex/calibration/run/fixtures/calc.py", id="workflow-report-evidence"),
            pytest.param(".temp/review/probe.py", id="workflow-scratch"),
            pytest.param(".experiments/apple-venv/lib/python3.12/site-packages/pkg/__init__.py", id="hidden-venv"),
            pytest.param("env/lib/python3.12/site-packages/conftest.py", id="named-venv"),
        ],
    )
    def test_non_source_ignored_file_never_refuses(self, gates: ModuleType, repository: Path, relative: str) -> None:
        """Report evidence and virtual-environment contents stay outside the counted ignored inputs."""
        _ignore(repository, ".reports/", ".temp/", ".experiments/", "env/")
        _write(repository / relative)
        for root in (".experiments/apple-venv", "env"):
            _write(repository / root / "pyvenv.cfg", "home = /usr/bin\n")
        snapshot = gates.inspect_source(repository)

        assert snapshot["status"] == ""
        assert gates.source_guard_error(snapshot, snapshot["head"]) is None

    def test_ignored_source_beside_report_evidence_still_refuses(self, gates: ModuleType, repository: Path) -> None:
        """Excluding report evidence does not hide an ignored module in an ordinary project directory."""
        _ignore(repository, ".reports/", "runtime/")
        _write(repository / ".reports/codex/run/probe.py")
        _write(repository / "runtime/module.py")
        snapshot = gates.inspect_source(repository)

        assert json.loads(snapshot["ignored"]) == ["runtime/module.py"]
        assert gates.source_guard_error(snapshot, snapshot["head"]).startswith("source-dirty:ignored-inputs:count=1:")

    def test_untracked_desktop_metadata_never_refuses(self, gates: ModuleType, repository: Path) -> None:
        """A file manager's ``.DS_Store`` is not source; any other untracked file still makes the checkout dirty."""
        _write(repository / ".DS_Store", "metadata")
        snapshot = gates.inspect_source(repository)
        assert gates.source_guard_error(snapshot, snapshot["head"]) is None

        _write(repository / "stray.txt", "x")
        snapshot = gates.inspect_source(repository)
        assert gates.source_guard_error(snapshot, snapshot["head"]).startswith("source-dirty:git status")

    def test_checkout_root_marker_excludes_nothing(self, gates: ModuleType, repository: Path) -> None:
        """A ``pyvenv.cfg`` at the checkout root cannot turn the whole checkout into an environment."""
        _ignore(repository, "pyvenv.cfg", "runtime/")
        _write(repository / "pyvenv.cfg", "home = /usr/bin\n")
        _write(repository / "runtime/module.py")
        snapshot = gates.inspect_source(repository)

        assert gates.source_guard_error(snapshot, snapshot["head"]).startswith("source-dirty:ignored-inputs:count=1:")


class TestSingleRefusal:
    """A checkout refused before the first gate is recorded once and runs no command."""

    @pytest.mark.parametrize(
        ("prepare", "cause"),
        [
            pytest.param(lambda repository: _write(repository / "untracked.py"), "source-dirty", id="untracked-file"),
            pytest.param(
                lambda repository: (_ignore(repository, "runtime/"), _write(repository / "runtime/module.py")),
                "source-ignored-inputs",
                id="ignored-module",
            ),
        ],
    )
    def test_refusal_is_recorded_once_and_no_command_runs(
        self, repository: Path, head: str, output: Path, tmp_path: Path, prepare, cause: str
    ) -> None:
        """One refusal record, refused rows instead of failed ones, no command run, and the validator accepts it."""
        prepare(repository)
        first, second = tmp_path / "lint-ran", tmp_path / "tests-ran"

        returncode, payload = _two_gate_run(repository, output, head, first, second)

        assert returncode == 1
        assert not first.exists()
        assert not second.exists()
        assert payload["refusal"]["cause"] == cause
        reason = payload["refusal"]["reason"]
        assert reason.startswith("source-dirty:")
        for gate_id in ("lint", "tests"):
            check = _gate_check(output, gate_id)
            assert (check["status"], check["exit_code"], check["cause"]) == ("refused", 0, "source-refused")
            assert "timeout_seconds" not in check
            assert reason in (output / check["stderr"]).read_text(encoding="utf-8")
        assert (payload["status"], payload["checks_failed"], payload["checks_refused"]) == (
            "fail",
            [],
            ["lint", "tests"],
        )
        assert "timeout" not in payload
        assert (output / "failed.txt").read_text(encoding="utf-8") == ""
        assert _load_validator()._validate_gates(output)["status"] == "fail"

    def test_unconfigured_gate_keeps_its_own_cause_under_a_refusal(
        self, repository: Path, head: str, output: Path
    ) -> None:
        """A gate with no command is reported as missing its command, not as refused."""
        _write(repository / "untracked.py")
        skipped = [part for gate in ("format", "types", "tests") for part in (f"--skip-{gate}", "out of scope")]
        argv = [sys.executable, str(RUN_GATES), "--out", str(output), "--worktree", str(repository)]
        argv += ["--expected-head", head, "--lint", _python_command("print(1)"), *skipped]

        assert _run(argv, cwd=repository).returncode == 1
        payload = json.loads((output / "gates.json").read_text(encoding="utf-8"))
        assert (_gate_check(output, "lint")["status"], _gate_check(output, "lint")["cause"]) == (
            "refused",
            "source-refused",
        )
        assert _gate_check(output, "review")["cause"] == "missing-command"
        assert (payload["checks_refused"], payload["checks_failed"]) == (["lint"], ["review"])
        assert _load_validator()._validate_gates(output)["status"] == "fail"

    def test_repaired_checkout_reruns_into_the_refused_output(
        self, repository: Path, head: str, output: Path, tmp_path: Path
    ) -> None:
        """A refused attempt is a complete prior receipt: repairing its reason and rerunning archives it and passes."""
        _write(repository / "untracked.py")
        refused_code, refused = _two_gate_run(repository, output, head, tmp_path / "lint-0", tmp_path / "tests-0")
        assert (refused_code, refused["checks_refused"]) == (1, ["lint", "tests"])
        (repository / "untracked.py").unlink()
        first, second = tmp_path / "lint-ran", tmp_path / "tests-ran"

        returncode, payload = _two_gate_run(repository, output, head, first, second)

        assert (returncode, payload["status"]) == (0, "pass")
        assert first.exists()
        assert second.exists()
        archive = output / "gate-attempts" / "001"
        archived = _load_validator()._validate_gates(archive)
        assert (archived["status"], archived["checks_refused"], archived["refusal"]) == (
            "fail",
            ["lint", "tests"],
            refused["refusal"],
        )
        for gate_id in ("lint", "tests"):
            check = next(row for row in archived["checks"] if row["id"] == gate_id)
            assert refused["refusal"]["reason"] in (archive / check["stderr"]).read_text(encoding="utf-8")

    def test_refused_gate_cannot_be_reclassified_as_skipped(
        self, repository: Path, head: str, output: Path, tmp_path: Path
    ) -> None:
        """A gate that never ran may run again, but a retry cannot declare it out of scope instead."""
        _write(repository / "untracked.py")
        assert _two_gate_run(repository, output, head, tmp_path / "lint-0", tmp_path / "tests-0")[0] == 1
        refused = (output / "gates.json").read_bytes()
        (repository / "untracked.py").unlink()
        skipped = [
            part for gate in ("lint", "format", "types", "review") for part in (f"--skip-{gate}", "out of scope")
        ]
        argv = [sys.executable, str(RUN_GATES), "--out", str(output), "--worktree", str(repository)]
        argv += ["--expected-head", head, "--tests", _marker_command(tmp_path / "tests-ran"), *skipped]

        completed = _run(argv, cwd=repository)

        assert completed.returncode == 2
        assert "cannot-skip-failed-gate:lint" in completed.stderr
        assert not (tmp_path / "tests-ran").exists()
        assert (output / "gates.json").read_bytes() == refused

    def test_clean_checkout_records_no_refusal(self, repository: Path, head: str, output: Path, tmp_path: Path) -> None:
        """A clean checkout runs every executable gate and records neither a refusal nor a cause."""
        first, second = tmp_path / "lint-ran", tmp_path / "tests-ran"

        returncode, payload = _two_gate_run(repository, output, head, first, second)

        assert returncode == 0
        assert first.exists()
        assert second.exists()
        assert "refusal" not in payload
        assert all("cause" not in check for check in payload["checks"])

    def test_change_made_by_a_gate_stays_with_that_gate(self, repository: Path, head: str, output: Path) -> None:
        """A checkout dirtied by an executed gate is a per-gate failure, never an attempt-wide refusal."""
        skipped = [part for gate in ("format", "types", "review") for part in (f"--skip-{gate}", "out of scope")]
        untracked = repository / "left-behind.py"
        completed = _run(
            [
                sys.executable,
                str(RUN_GATES),
                "--out",
                str(output),
                "--worktree",
                str(repository),
                "--expected-head",
                head,
                "--lint",
                _marker_command(untracked),
                "--tests",
                _python_command("print('tests')"),
                *skipped,
            ],
            cwd=repository,
        )
        payload = json.loads((output / "gates.json").read_text(encoding="utf-8"))

        assert completed.returncode == 1
        assert "refusal" not in payload
        assert _gate_check(output, "lint")["status"] == "fail"


class TestRecordedTimeoutAndCauses:
    """Executable gates keep their timeout, and gates that did not pass name an environment-aware cause."""

    def test_timeout_value_and_origin_are_recorded(
        self, repository: Path, head: str, output: Path, tmp_path: Path
    ) -> None:
        """Each executable gate keeps the applied timeout and the attempt names where it came from."""
        _, payload = _two_gate_run(repository, output, head, tmp_path / "lint-ran", tmp_path / "tests-ran")

        assert payload["timeout"] == {"seconds": 321, "origin": "flag"}
        assert {check["id"]: check.get("timeout_seconds") for check in payload["checks"]} == {
            "lint": 321,
            "format": None,
            "types": None,
            "tests": 321,
            "review": None,
        }

    def test_environment_timeout_origin(self, repository: Path, output: Path) -> None:
        """A timeout taken from ``GATE_TIMEOUT_SECONDS`` is recorded with that origin."""
        completed = _run(
            [
                sys.executable,
                str(RUN_GATES),
                "--out",
                str(output),
                "--lint",
                _python_command("print(1)"),
                *_skip_gates("lint"),
            ],
            cwd=repository,
            env={**os.environ, "GATE_TIMEOUT_SECONDS": "900"},
        )
        payload = json.loads((output / "gates.json").read_text(encoding="utf-8"))

        assert completed.returncode == 0, completed.stderr
        assert payload["timeout"] == {"seconds": 900, "origin": "environment"}

    @pytest.mark.parametrize(
        ("source", "cause"),
        [
            pytest.param(
                "import ai_rig_absent_gate_dependency",
                "missing-python-package",
                id="missing-package",
            ),
            pytest.param(
                "import sys; sys.stderr.write('fatal: Could not resolve host: github.com\\n'); raise SystemExit(1)",
                "network-unavailable",
                id="network",
            ),
            pytest.param(
                "import sys; sys.stderr.write('error: Failed to initialize cache at `/h/.cache/uv`\\n'); "
                "raise SystemExit(2)",
                "hook-cache-unwritable",
                id="hook-cache",
            ),
            pytest.param("print('1 failed, 3 passed'); raise SystemExit(1)", "command-failed", id="ordinary"),
        ],
    )
    def test_failed_gate_names_its_cause(self, repository: Path, output: Path, source: str, cause: str) -> None:
        """The cause comes from the tool's own diagnostic and the validator still accepts the record."""
        completed = _run(
            [
                sys.executable,
                str(RUN_GATES),
                "--out",
                str(output),
                "--lint",
                _python_command(source),
                *_skip_gates("lint"),
            ],
            cwd=repository,
        )

        assert completed.returncode == 1
        assert _gate_check(output, "lint")["cause"] == cause
        assert _load_validator()._validate_gates(output)["checks_failed"] == ["lint"]

    @pytest.mark.parametrize(
        ("missing", "cause"),
        [
            pytest.param("ai_rig_absent_gate_dependency", "missing-python-package", id="third-party"),
            pytest.param("tracked_pkg.removed", "command-failed", id="own-package-submodule"),
        ],
    )
    def test_missing_module_names_are_recorded(self, repository: Path, output: Path, missing: str, cause: str) -> None:
        """Missing modules are listed, and a removed submodule of the checkout's own package is a source failure."""
        _write(repository / "src/tracked_pkg/__init__.py", "")
        source = f"import sys; sys.path.insert(0, 'src'); import {missing}"
        argv = [sys.executable, str(RUN_GATES), "--out", str(output), "--lint", _python_command(source)]

        assert _run([*argv, *_skip_gates("lint")], cwd=repository).returncode == 1
        check = _gate_check(output, "lint")
        assert (check["cause"], check["missing_modules"]) == (cause, [missing])

    @pytest.mark.parametrize(
        ("body", "modules"),
        [
            pytest.param(
                'sys.stderr.write("=== ERRORS ===\\nTraceback (most recent call last):\\n'
                '  File \\"fake.py\\", line 1, in <module>\\nModuleNotFoundError: No module named \'printed\'\\n"); assert False',
                [],
                id="uncaptured-printed-traceback",
            ),
            pytest.param("assert False, 'fatal: Could not resolve host'", [], id="quoted-network-diagnostic"),
            pytest.param("assert False, 'bind() failed: Operation not permitted'", [], id="quoted-bind-diagnostic"),
            pytest.param(
                "assert False, \"ModuleNotFoundError: No module named 'asserted'\"",
                [],
                id="assertion-message",
            ),
            pytest.param(
                "assert False, \"first line\\nModuleNotFoundError: No module named 'asserted'\"",
                [],
                id="multiline-assertion-message",
            ),
            pytest.param(
                "print(\"ModuleNotFoundError: No module named 'printed'\"); assert False",
                [],
                id="captured-output",
            ),
            pytest.param(
                "sys.stderr.write(\"E   ImportError: No module named 'printed'\\n\"); assert False",
                [],
                id="captured-pytest-lookalike",
            ),
            pytest.param(
                "try:\n        import ai_rig_caught_dependency\n"
                "    except ImportError:\n        traceback.print_exc()\n    assert False",
                [],
                id="captured-traceback",
            ),
            pytest.param(
                "print(\"ModuleNotFoundError: No module named 'printed'\"); import ai_rig_absent_gate_dependency",
                ["ai_rig_absent_gate_dependency"],
                id="actual-import-with-captured-output",
            ),
            pytest.param(
                "raise ImportError(\"No module named 'ai_rig_legacy_import'\")",
                [],
                id="import-error-without-module-evidence",
            ),
        ],
    )
    def test_pytest_failure_diagnoses_only_import_exceptions(self, repository, output, body, modules):
        """Actual pytest tracebacks drive provisioning while assertion text and captured output do not."""
        _write(repository / "test_case.py", f"import sys\nimport traceback\n\ndef test_case():\n    {body}\n")
        command = _python_command("import pytest; raise SystemExit(pytest.main(['-q', '-s', 'test_case.py']))")
        argv = [sys.executable, str(RUN_GATES), "--out", str(output), "--tests", command, *_skip_gates("tests")]
        env = {**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": ""}
        completed = _run(argv, cwd=repository, env=env)

        assert completed.returncode == 1, completed.stderr
        check = _gate_check(output, "tests")
        assert check["cause"] == ("missing-python-package" if modules else "command-failed")
        assert check.get("missing_modules", []) == modules

    def test_pytest_conftest_failure_reports_actual_missing_module(self, repository, output):
        """The recorded exception survives failure before the pytest session can start."""
        _write(repository / "conftest.py", "import absent_gate_conftest_dependency\n")
        command = _python_command("import pytest; raise SystemExit(pytest.main(['-q']))")
        argv = [sys.executable, str(RUN_GATES), "--out", str(output), "--tests", command, *_skip_gates("tests")]
        env = {**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": ""}

        assert _run(argv, cwd=repository, env=env).returncode == 1
        check = _gate_check(output, "tests")
        assert check["cause"] == "missing-python-package"
        assert check["missing_modules"] == ["absent_gate_conftest_dependency"]

    def test_shell_pytest_collect_only_does_not_pass(self, repository, output):
        """The shell route also requires call reports when its command invokes pytest."""
        _write(repository / "test_case.py", "def test_case():\n    pass\n")
        command = _python_command("import pytest; raise SystemExit(pytest.main(['--collect-only']))")
        argv = [sys.executable, str(RUN_GATES), "--out", str(output), "--tests", command, *_skip_gates("tests")]
        env = {**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": ""}

        assert _run(argv, cwd=repository, env=env).returncode == 1
        assert _gate_check(output, "tests")["status"] == "fail"


class TestToolCache:
    """Pre-commit and uv receive a writable cache outside the checkout, and only there."""

    def test_gate_commands_see_the_cache(self, repository: Path, output: Path, tmp_path: Path) -> None:
        """Both cache variables point inside the requested directory, which the attempt records."""
        cache = tmp_path / "tool-cache"
        command = _python_command("import os; print(os.environ['PRE_COMMIT_HOME']); print(os.environ['UV_CACHE_DIR'])")
        completed = _run(
            [
                sys.executable,
                str(RUN_GATES),
                "--out",
                str(output),
                "--tool-cache",
                str(cache),
                "--lint",
                command,
                *_skip_gates("lint"),
            ],
            cwd=repository,
        )
        payload = json.loads((output / "gates.json").read_text(encoding="utf-8"))
        printed = (output / _gate_check(output, "lint")["stdout"]).read_text(encoding="utf-8").splitlines()

        assert completed.returncode == 0, completed.stderr
        assert payload["tool_cache"] == cache.resolve().as_posix()
        assert [Path(line) for line in printed] == [cache.resolve() / "pre-commit", cache.resolve() / "uv"]

    def test_cache_inside_the_checkout_is_refused(self, repository: Path, output: Path) -> None:
        """A cache among the reviewed inputs is invalid input, and no gate runs."""
        completed = _run(
            [
                sys.executable,
                str(RUN_GATES),
                "--out",
                str(output),
                "--tool-cache",
                str(repository / "cache"),
                "--lint",
                _python_command("print(1)"),
                *_skip_gates("lint"),
            ],
            cwd=repository,
        )

        assert completed.returncode == 2
        assert "invalid-tool-cache" in completed.stderr
        assert not (output / "gates.json").exists()

    def test_gate_environment_keeps_pre_commit_home(self, gates: ModuleType) -> None:
        """A caller-provided pre-commit cache location reaches gate commands."""
        assert gates.gate_environment({"PRE_COMMIT_HOME": "/cache/pre-commit"}) == {
            "PRE_COMMIT_HOME": "/cache/pre-commit"
        }

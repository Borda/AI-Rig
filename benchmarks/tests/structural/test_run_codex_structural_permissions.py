"""No-model permission, sandbox-profile, and arm-home tests for the Codex structural adapter.

Split out of ``test_run_codex_structural.py`` so neither file crosses the 250 KB maintenance limit the suite enforces on
its own runners; the tests are unchanged.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from _launcher_capability import requires_private_filesystem
from _runtime_bundle import make_direct_runtime_bundle, write_runtime_snapshot_metadata

BENCHMARKS_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BENCHMARKS_DIR))

# Patch seams live in the package modules the runner shim re-exports from: patching the shim
# would leave each package module's own global binding untouched. Inside the package every seam
# below is reached through its defining module, so this is the single place to patch it.
from _bench_codex.structural import diff_impact as codex_diff_impact  # noqa: E402
from _bench_codex.structural import provisioning as codex_provisioning  # noqa: E402
from _bench_common import mutation_isolation  # noqa: E402

SCRIPT_PATH = BENCHMARKS_DIR / "run-codex-structural.py"
MANIFEST_PATH = BENCHMARKS_DIR / "manifests" / "codex-integration.json"
SUITE_PATH = BENCHMARKS_DIR / "suites" / "tasks-bench.json"
TREATMENT_PYTHON = "/opt/homebrew/bin/python3.11"
CODEX_CLI_REQUIRED = pytest.mark.skipif(shutil.which("codex") is None, reason="Codex CLI is unavailable")
SANDBOX_INTEGRATION_OPT_IN = pytest.mark.skipif(
    os.environ.get("RUN_CODEX_SANDBOX_INTEGRATION") != "1",
    reason="set RUN_CODEX_SANDBOX_INTEGRATION=1 to exercise the installed Codex sandbox",
)


def _oracle_leaking_runner(oracle: Path) -> Callable[..., SimpleNamespace]:
    """Build a command runner emulating a profile that leaks only the evaluator oracle fixture.

    Example:
        >>> runner = _oracle_leaking_runner(Path("oracle"))
        >>> runner(["codex", "--version"]).returncode, runner(["sandbox", "pass"]).returncode
        (0, 0)
    """

    def _run(command: list[str], **_kwargs: Any) -> SimpleNamespace:
        """Answer the version probe, the control read, and the oracle read the way a leaking sandbox would."""
        if command == ["codex", "--version"]:
            return SimpleNamespace(returncode=0, stdout="codex-cli fixture", stderr="")
        if command[-1] == "pass":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if str(oracle) in command:
            return SimpleNamespace(returncode=0, stdout="oracle", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="Operation not permitted")

    return _run


def _link_coordination_root(coordination_root: Path, outside: Path) -> None:
    """Make the coordination root itself a symlink to an outside directory."""
    coordination_root.symlink_to(outside, target_is_directory=True)


def _link_readers_directory(coordination_root: Path, outside: Path) -> None:
    """Create a real coordination root whose ``readers`` directory is a symlink to an outside directory."""
    coordination_root.mkdir()
    (coordination_root / "readers").symlink_to(outside, target_is_directory=True)


@pytest.fixture(name="repo_path")
def _repo_path(tmp_path: Path) -> Path:
    """Empty target repository directory under the test's temporary root."""
    path = tmp_path / "target"
    path.mkdir()
    return path


@pytest.fixture(name="index_path")
def _index_path(repo_path: Path) -> Path:
    """Create an empty-graph locked index under the target repository's Codemap cache directory."""
    path = repo_path / ".cache" / "codemap" / "locked-index.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}", encoding="utf-8")
    return path


@pytest.fixture(name="home_path")
def _home_path(tmp_path: Path) -> Path:
    """Empty disposable Codex home directory under the test's temporary root."""
    path = tmp_path / "codex-home"
    path.mkdir()
    return path


@pytest.fixture(name="auth_path")
def _auth_path(home_path: Path) -> Path:
    """Credential file placed inside the Codex home so tests can assert the profile denies it."""
    path = home_path / "auth.json"
    path.write_text("fixture-auth", encoding="utf-8")
    return path


@pytest.fixture(name="evidence_root")
def _evidence_root(tmp_path: Path) -> Path:
    """Directory standing in for the frozen evaluator checkout."""
    path = tmp_path / "evaluator"
    path.mkdir()
    return path


@pytest.fixture(name="evaluator_oracle")
def _evaluator_oracle(evidence_root: Path) -> Path:
    """Oracle source file inside the evaluator evidence root that a model must never read."""
    oracle = evidence_root / "benchmarks" / "run-codex-structural.py"
    oracle.parent.mkdir(parents=True)
    oracle.write_text("oracle", encoding="utf-8")
    return oracle


@pytest.fixture(name="stub_home_admission")
def _stub_home_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the host-dependent admission probes that run while a verified arm home is prepared."""
    monkeypatch.setattr(codex_diff_impact, "_validate_locked_runtime", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(codex_provisioning, "_verify_permission_profile", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(codex_provisioning, "_verify_locked_codemap_python", lambda **_kwargs: TREATMENT_PYTHON)
    monkeypatch.setattr(codex_provisioning, "_verify_treatment_artifact_locks", lambda *_args, **_kwargs: None)


@pytest.fixture(name="coordination_root")
def _coordination_root(script_run_codex: Any, index_path: Path) -> Iterator[Path]:
    """Prepare the index-local coordination root and release it on teardown unless the test already removed it."""
    root = script_run_codex._prepare_coordination_root(index_path)
    yield root
    with suppress(ValueError):
        script_run_codex._cleanup_coordination_root(root)


@pytest.fixture(name="live_reader")
def _live_reader(coordination_root: Path, index_path: Path) -> Iterator[subprocess.Popen[str]]:
    """Child process holding a live Codemap reader lease on the index until teardown.

    The real ``codemap_py.rwgate`` lock is the contract here, so the child is a genuine process rather than a stub.
    """
    source_root = BENCHMARKS_DIR.parent / "plugins" / "codemap-py" / "src"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import sys\n"
                "from codemap_py.rwgate import read_index\n"
                "with read_index(sys.argv[1]):\n"
                "    print('ready', flush=True)\n"
                "    sys.stdin.readline()\n"
            ),
            str(index_path),
        ],
        env={**os.environ, "PYTHONPATH": str(source_root)},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None
    assert child.stdout.readline().strip() == "ready"
    yield child
    child.communicate("\n", timeout=10)
    assert child.returncode == 0


class TestBuildCodexCommand:
    """Transport plan built for one ``codex exec`` invocation."""

    def test_is_ephemeral_json_profile_backed_and_keeps_prompt_exact(
        self, script_run_codex: Any, tmp_path: Path
    ) -> None:
        """The command uses isolated profiles, never a CLI sandbox flag, and preserves prompt bytes.

        Prevents a legacy command-line sandbox setting from overriding the disposable home's permission profile; the
        same guarantee covers executable cells, which rely on the verified profile instead of a sandbox override.
        """
        prompt = "Return the callers exactly.\nSecond line stays unchanged."

        command = script_run_codex.build_codex_command(
            repo_path=tmp_path, model="gpt-6.1-sol", reasoning_effort="high", prompt=prompt
        )

        assert command[:2] == ["codex", "exec"]
        assert "--json" in command
        assert "--ephemeral" in command
        assert "--strict-config" in command
        assert "--sandbox" not in command
        assert command[command.index("--config") + 1] == 'model_reasoning_effort="high"'
        assert command[command.index("--cd") + 1] == str(tmp_path)
        assert command[command.index("--model") + 1] == "gpt-6.1-sol"
        assert command[-1] == prompt


class TestRelocateFrozenIndex:
    """Re-rooting a frozen Codemap index at an executable worktree."""

    def test_changes_only_scan_root(self, script_run_codex: Any, tmp_path: Path) -> None:
        """The derived graph carries the worktree scan root and otherwise identical content.

        An executable worktree needs a valid graph; any other edit would silently change what the model can query.
        """
        source_root = tmp_path / "source"
        worktree_root = tmp_path / "worktree"
        frozen_payload = {
            "scan_root": str(source_root.resolve()),
            "scan_version": 13,
            "modules": {"pkg.module": {"symbols": ["target"]}},
        }
        frozen_bytes = json.dumps(frozen_payload, indent=2, sort_keys=True).encode("utf-8")

        derived_bytes, relocation = script_run_codex.relocate_frozen_index_for_worktree(
            frozen_bytes, source_root=source_root, worktree_root=worktree_root
        )

        assert json.loads(frozen_bytes) == frozen_payload
        assert json.loads(derived_bytes) == {**frozen_payload, "scan_root": str(worktree_root.resolve())}
        assert relocation["frozen_index_sha256"] == hashlib.sha256(frozen_bytes).hexdigest()
        assert relocation["derived_index_sha256"] == hashlib.sha256(derived_bytes).hexdigest()
        assert relocation["source_scan_root"] == str(source_root.resolve())
        assert relocation["worktree_scan_root"] == str(worktree_root.resolve())
        # The root-stripped digest belongs to the shared relocation helper, which the runner imports rather
        # than redefines, so the expectation is read from its owner.
        assert relocation["non_root_content_sha256"] == mutation_isolation._non_root_index_sha256(frozen_payload)

    def test_rejects_an_unrelated_source_root(self, script_run_codex: Any, tmp_path: Path) -> None:
        """A graph whose recorded scan root differs from the frozen repository is refused.

        A relocated graph must originate from the frozen benchmark repository, not from an arbitrary checkout.
        """
        frozen_bytes = json.dumps({"scan_root": str(tmp_path / "other")}).encode("utf-8")

        with pytest.raises(ValueError, match="scan_root"):
            script_run_codex.relocate_frozen_index_for_worktree(
                frozen_bytes, source_root=tmp_path / "source", worktree_root=tmp_path / "worktree"
            )


class TestCodexStratum:
    """Model and reasoning-effort pair locked by the active manifest."""

    def test_accepts_the_manifest_model_and_effort(self, script_run_codex: Any) -> None:
        """The accepted model/effort pair is consumed from the active manifest without error."""
        script_run_codex._validate_codex_stratum("gpt-6.1-sol", "high", MANIFEST_PATH)

    @pytest.mark.parametrize(
        ("model", "effort", "message"),
        [
            pytest.param("gpt-5.3-codex", "high", r"gpt-6.1-sol", id="other-model"),
            pytest.param("gpt-6.1-sol", "medium", "reasoning effort", id="other-effort"),
        ],
    )
    def test_rejects_other_model_or_effort(self, script_run_codex: Any, model: str, effort: str, message: str) -> None:
        """A model or effort outside the locked pair is refused before any paid run.

        Guards the experiment coordinate: a drifted model or effort would make arms incomparable.
        """
        with pytest.raises(ValueError, match=message):
            script_run_codex._validate_codex_stratum(model, effort, MANIFEST_PATH)


class TestManifestArmOrder:
    """Deterministic, counterbalanced ordering of the three treatment arms."""

    def test_is_deterministic_and_uses_only_current_plain_cli_skill_arms(self, script_run_codex: Any) -> None:
        """Repeated calls agree and only the current A/B/C arms appear.

        Prevents the historical auto/required arm registry from leaking into the new experiment.
        """
        args = ("codex-integration-v1", "gpt-6.1-sol", "FN-02", 1, "high")

        first = script_run_codex._manifest_arm_order(*args)
        second = script_run_codex._manifest_arm_order(*args)

        assert first == second
        assert set(first) == {"A_plain", "B_auto", "C_strict"}

    def test_counterbalances_ordinals_across_the_exact_suite(self, script_run_codex: Any) -> None:
        """Each treatment occupies every ordinal 18 or 19 times across all 55 tasks at one repetition.

        Prevents a deterministic hash order from making prompt-cache exposure a systematic arm confound while preserving
        repeatable execution planning.
        """
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        task_ids = manifest["preregistered_cells"]["structural_execution_task_ids"]
        orders = [
            script_run_codex._manifest_arm_order(
                "codex-integration-v1",
                script_run_codex.PARITY_CODEX_MODEL,
                task_id,
                1,
                script_run_codex.PARITY_CODEX_REASONING_EFFORT,
            )
            for task_id in task_ids
        ]
        ordinals_by_arm = {
            arm: [sum(order[ordinal] == arm for order in orders) for ordinal in range(3)]
            for arm in script_run_codex.CODEX_STRUCTURAL_ARMS
        }

        assert len(task_ids) == 55
        assert all(count in {18, 19} for counts in ordinals_by_arm.values() for count in counts), ordinals_by_arm


class TestWritePermissionConfig:
    """Permission profile written into each arm's disposable Codex home."""

    @requires_private_filesystem
    def test_plain_profile_denies_private_roots_and_grants_no_write(
        self, script_run_codex: Any, home_path: Path, index_path: Path, auth_path: Path
    ) -> None:
        """Plain profile extends read-only, denies credentials and the index, and grants no write access.

        Exact config assertions prevent implicit or overly broad permissions, and the host environment must never leak
        into the shell policy.
        """
        env = {"PATH": "/fixture/bin", "API_TOKEN": "must-not-leak", "SSH_AUTH_SOCK": "/fixture/socket"}
        home = script_run_codex.ArmHome("A_plain", home_path, env, False)

        config = script_run_codex._write_permission_config(home, "A_plain", index_path)

        text = config.read_text(encoding="utf-8")
        denied_roots = script_run_codex._untrusted_host_agent_roots(home, "A_plain")
        assert config == home_path / "config.toml"
        assert config.stat().st_mode & 0o777 == 0o600
        assert 'default_permissions = "provider-parity-plain"' in text
        assert "\n[permissions]\n" in text
        assert "[permissions.provider-parity-plain]" in text
        assert 'extends = ":read-only"' in text
        assert f'"{auth_path.resolve()}" = "deny"' in text
        assert f'"{index_path.parent.resolve()}" = "deny"' in text
        assert "[permissions.provider-parity-plain.network]" in text
        assert "enabled = false" in text
        assert '"write"' not in text
        assert "[shell_environment_policy]" in text
        assert 'inherit = "none"' in text
        assert "API_TOKEN" not in text
        assert "SSH_AUTH_SOCK" not in text
        assert all(f'"{root}" = "deny"' in text for root in denied_roots)

    @requires_private_filesystem
    @pytest.mark.parametrize("arm", ["B_auto", "C_strict"])
    def test_codemap_profiles_grant_only_coordination_write(
        self, script_run_codex: Any, tmp_path: Path, home_path: Path, index_path: Path, auth_path: Path, arm: str
    ) -> None:
        """Codemap treatment profiles deny private roots while granting write only to the coordination lock root."""
        home = script_run_codex.ArmHome(arm, home_path, {"PATH": "/fixture/bin"}, True, True)
        marketplace_root = tmp_path / "marketplace"

        config = script_run_codex._write_permission_config(home, arm, index_path, marketplace_root=marketplace_root)

        text = config.read_text(encoding="utf-8")
        coordination_root = index_path.parent / ".index-rw"
        denied_roots = script_run_codex._untrusted_host_agent_roots(home, arm, marketplace_root)
        assert config == home_path / "config.toml"
        assert 'default_permissions = "provider-parity-codemap"' in text
        assert "[permissions.provider-parity-codemap]" in text
        assert 'extends = ":read-only"' in text
        assert f'"{auth_path.resolve()}" = "deny"' in text
        assert f'"{coordination_root.resolve()}" = "write"' in text
        assert "[permissions.provider-parity-codemap.network]" in text
        assert "enabled = false" in text
        assert "sandbox_mode" not in text
        assert "sandbox_workspace_write" not in text
        assert all(f'"{root}" = "deny"' in text for root in denied_roots)

    def test_executable_workspace_grants_only_the_disposable_worktree(
        self, script_run_codex: Any, tmp_path: Path, home_path: Path, index_path: Path
    ) -> None:
        """Executable cells receive a write grant for the disposable worktree while the source stays denied."""
        source_path = tmp_path / "source"
        source_path.mkdir()
        workspace_path = tmp_path / "workspace"
        workspace_path.mkdir()
        home = script_run_codex.ArmHome("A_plain", home_path, {"PATH": "/fixture/bin"}, False)

        config = script_run_codex._write_permission_config(
            home, "A_plain", index_path, writable_workspace=workspace_path, denied_workspace=source_path
        )

        text = config.read_text(encoding="utf-8")
        assert 'extends = ":read-only"' in text
        # Paths enter the TOML as basic strings, so the expectation is the escaped rendering
        # rather than the raw path: a Windows separator is a TOML escape character.
        assert f'{json.dumps(str(workspace_path.resolve()))} = "write"' in text
        assert f'{json.dumps(str(source_path.resolve()))} = "deny"' in text

    def test_denies_evaluator_evidence_and_records_an_oracle_probe(
        self, script_run_codex: Any, home_path: Path, index_path: Path, evidence_root: Path, evaluator_oracle: Path
    ) -> None:
        """A model must not read the frozen evaluator source used to score its answer.

        The profile denies the evidence root and records the actual oracle file as a probe target so verification checks
        a real read rather than a directory listing.
        """
        home = script_run_codex.ArmHome("A_plain", home_path, {"PATH": "/fixture/bin"}, False)

        config = script_run_codex._write_permission_config(home, "A_plain", index_path, evidence_roots=(evidence_root,))

        assert f'{json.dumps(str(evidence_root.resolve()))} = "deny"' in config.read_text(encoding="utf-8")
        assert home.evidence_probe_paths == (evaluator_oracle.resolve(),)

    def test_bound_snapshot_is_denied_while_staged_arm_runtime_stays_accessible(
        self, script_run_codex: Any, tmp_path: Path, home_path: Path, index_path: Path
    ) -> None:
        """B may use its home copy of the runtime without read access to the archived runtime inputs.

        The archived snapshot root is denied as evidence; the copy staged inside the arm home is not listed at all.
        """
        snapshot_root = tmp_path / "results" / "inputs"
        launcher = make_direct_runtime_bundle(snapshot_root / "B_auto")
        source_root = launcher.parent.parent
        write_runtime_snapshot_metadata(snapshot_root, {"B_auto:direct-cli": source_root})
        runner = script_run_codex.CodexRunner("gpt-6.1-sol", tmp_path)
        runner._bind_runtime_snapshot(snapshot_root, {"B_auto": {"direct-cli": source_root}})
        staged_runtime = home_path / "direct-cli"
        shutil.copytree(source_root, staged_runtime)
        home = script_run_codex.ArmHome("B_auto", home_path, {"PATH": "/fixture/bin"}, True, True)

        config = script_run_codex._write_permission_config(
            home, "B_auto", index_path, evidence_roots=runner.evidence_roots
        )
        runner._record_runtime_failure("B_auto", ValueError("fixture runtime failure"))

        text = config.read_text(encoding="utf-8")
        evidence = json.loads((snapshot_root.parent / "runtime-isolation.jsonl").read_text(encoding="utf-8"))
        assert f'{json.dumps(str(snapshot_root.parent.resolve()))} = "deny"' in text
        assert str(staged_runtime) not in text
        assert launcher.is_file()
        assert str(snapshot_root.parent.resolve()) in evidence["evidence_roots"]


class TestBenchmarkEvidenceRoots:
    """Evidence-boundary coordinates read from ``BENCHMARK_EVIDENCE_ROOTS``."""

    def test_defaults_to_the_repository_root(self, script_run_codex: Any) -> None:
        """Without the variable, the repository root is the single evidence root."""
        assert script_run_codex._benchmark_evidence_roots({}) == (BENCHMARKS_DIR.parent.resolve(),)

    @pytest.mark.parametrize(
        "entry",
        [pytest.param("evaluator", id="existing-directory"), pytest.param("future-results", id="not-yet-created")],
    )
    def test_resolves_absolute_paths(
        self, script_run_codex: Any, tmp_path: Path, evidence_root: Path, entry: str
    ) -> None:
        """Absolute entries resolve whether or not the directory exists yet, so results dirs can be denied early."""
        root = tmp_path / entry

        roots = script_run_codex._benchmark_evidence_roots({"BENCHMARK_EVIDENCE_ROOTS": json.dumps([str(root)])})

        assert roots == (root.resolve(),)

    @pytest.mark.parametrize(
        ("raw", "message"),
        [
            pytest.param('["relative"]', r"absolute|evidence", id="relative-path"),
            pytest.param("[]", r"path strings|evidence", id="empty-list"),
        ],
    )
    def test_rejects_coordinates_the_sandbox_cannot_enforce(
        self, script_run_codex: Any, raw: str, message: str
    ) -> None:
        """Relative paths and empty lists are refused rather than silently weakening the evidence boundary."""
        with pytest.raises(ValueError, match=message):
            script_run_codex._benchmark_evidence_roots({"BENCHMARK_EVIDENCE_ROOTS": raw})

    def test_rejects_a_path_that_is_not_a_directory(self, script_run_codex: Any, tmp_path: Path) -> None:
        """An existing regular file cannot serve as an evidence root."""
        non_directory = tmp_path / "not-a-directory"
        non_directory.write_text("fixture", encoding="utf-8")

        with pytest.raises(ValueError, match=r"directory|evidence"):
            script_run_codex._benchmark_evidence_roots({"BENCHMARK_EVIDENCE_ROOTS": json.dumps([str(non_directory)])})

    def test_runner_reads_parent_environment_for_its_roots(
        self, script_run_codex: Any, tmp_path: Path, evidence_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Direct stage adapters inherit the parent evaluator boundary without a constructor argument."""
        monkeypatch.setenv("BENCHMARK_EVIDENCE_ROOTS", json.dumps([str(evidence_root)]))

        runner = script_run_codex.CodexRunner("gpt-6.1-sol", tmp_path)

        assert runner.evidence_roots == (evidence_root.resolve(),)


class TestVerifyPermissionProfile:
    """Fail-closed verification of the written permission profile against the Codex binary."""

    def test_fails_when_an_evaluator_oracle_is_readable(
        self,
        script_run_codex: Any,
        repo_path: Path,
        home_path: Path,
        index_path: Path,
        evidence_root: Path,
        evaluator_oracle: Path,
    ) -> None:
        """A denied root enumeration cannot substitute for an oracle-file read check.

        The simulated sandbox leaks only the oracle file; verification must notice by reading that file.
        """
        home = script_run_codex.ArmHome("A_plain", home_path, {"PATH": "/fixture/bin"}, False)
        script_run_codex._write_permission_config(home, "A_plain", index_path, evidence_roots=(evidence_root,))

        with pytest.raises(ValueError, match="evaluator evidence"):
            script_run_codex._verify_permission_profile(
                home, repo_path, index_path, command_runner=_oracle_leaking_runner(evaluator_oracle)
            )

    def test_fails_closed_when_codex_rejects_the_profile(
        self, script_run_codex: Any, repo_path: Path, home_path: Path
    ) -> None:
        """An unsupported permission profile cannot silently run under Codex defaults.

        Prevents the profile probe from treating an unknown profile as a successful setup. A check that only validates
        the Codex binary version would fail to raise here.
        """
        home = script_run_codex.ArmHome("B_auto", home_path, {}, True, True)

        def _reject_profile(command: list[str], **_kwargs: Any) -> SimpleNamespace:
            """Accept the version probe and reject the selected profile at the validation boundary."""
            if "--version" in command:
                return SimpleNamespace(returncode=0, stdout="codex-cli 0.138.0", stderr="")
            return SimpleNamespace(returncode=2, stdout="", stderr="unknown permission profile provider-parity-codemap")

        with pytest.raises(ValueError, match=r"profile|permission|unsupported"):
            script_run_codex._verify_permission_profile(home, repo_path, command_runner=_reject_profile)

    def test_resolves_workspace_python_symlink_before_sandbox(
        self, script_run_codex: Any, tmp_path: Path, repo_path: Path, home_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A project virtualenv launcher is resolved to its real interpreter before the sandbox command is built.

        Otherwise the launcher would be denied as an executable inside the source tree.
        """
        python_dir = repo_path / ".venv" / "bin"
        python_dir.mkdir(parents=True)
        external_python = tmp_path / "runtime" / "python3"
        external_python.parent.mkdir()
        external_python.write_text("fixture", encoding="utf-8")
        external_python.chmod(0o755)
        workspace_python = python_dir / "python3"
        workspace_python.symlink_to(external_python)
        home = script_run_codex.ArmHome("A_plain", home_path, {}, False)
        commands: list[list[str]] = []

        def _reject_after_capture(command: list[str], **_kwargs: Any) -> SimpleNamespace:
            """Capture validation input before rejecting the fixture runtime."""
            commands.append(command)
            if command == ["codex", "--version"]:
                return SimpleNamespace(returncode=0, stdout="codex-cli 0.146.1", stderr="")
            return SimpleNamespace(returncode=2, stdout="", stderr="fixture stop")

        monkeypatch.setattr(script_run_codex.sys, "executable", str(workspace_python))

        with pytest.raises(ValueError, match=r"profile|permission|unsupported"):
            script_run_codex._verify_permission_profile(home, repo_path, command_runner=_reject_after_capture)

        sandbox_command = commands[1]
        assert sandbox_command[sandbox_command.index("--") + 1] == str(external_python.resolve())


class TestArmHomePreparation:
    """Disposable arm homes created by ``prepare_arm_home`` and ``CodexRunner._prepare_verified_home``."""

    @pytest.mark.parametrize(
        ("arm", "home_kwargs"),
        [
            pytest.param("A_plain", lambda launcher: {}, id="plain"),
            pytest.param("B_auto", lambda launcher: {"codemap_bin": launcher}, id="direct-cli"),
        ],
    )
    def test_drops_host_skill_file_binding(
        self,
        script_run_codex: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        arm: str,
        home_kwargs: Callable[[Path], dict[str, Path]],
    ) -> None:
        """A/B homes must not inherit a host Skill path or evidence-root list that could contaminate treatment."""
        monkeypatch.setenv("CODEMAP_SKILL_FILE", "/host/untrusted/SKILL.md")
        monkeypatch.setenv("BENCHMARK_EVIDENCE_ROOTS", '["/host/evaluator"]')
        launcher = make_direct_runtime_bundle(tmp_path / "direct-runtime")

        with script_run_codex.prepare_arm_home(arm, root=tmp_path, **home_kwargs(launcher)) as home:
            assert "CODEMAP_SKILL_FILE" not in home.env
            assert "BENCHMARK_EVIDENCE_ROOTS" not in home.env

    def test_default_root_is_the_canonical_temp_directory(
        self, script_run_codex: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OS temp aliases must not become rejected snapshot path components."""
        canonical_root = tmp_path / "canonical-temp"
        canonical_root.mkdir()
        temp_alias = tmp_path / "temp-alias"
        temp_alias.symlink_to(canonical_root, target_is_directory=True)
        monkeypatch.setattr(script_run_codex.tempfile, "gettempdir", lambda: str(temp_alias))

        with script_run_codex.prepare_arm_home("A_plain") as home:
            assert home.path.parent == canonical_root.resolve(strict=True)
            script_run_codex._assert_safe_path_components(home.path / "config.toml")

    def test_explicit_root_rejects_symlink_components(self, script_run_codex: Any, tmp_path: Path) -> None:
        """Caller-supplied roots remain strict even when the symlink target is a directory."""
        canonical_root = tmp_path / "canonical-temp"
        canonical_root.mkdir()
        temp_alias = tmp_path / "temp-alias"
        temp_alias.symlink_to(canonical_root, target_is_directory=True)

        with pytest.raises(ValueError, match=re.escape(f"permission path contains a symlink: {temp_alias}")):
            script_run_codex.prepare_arm_home("A_plain", root=temp_alias)

    def test_verified_home_passes_writable_workspace_to_permission_verifier(
        self, script_run_codex: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Executable homes verify the workspace-write permission instead of source denial."""
        workspace = tmp_path / "workspace"
        source = tmp_path / "source"
        workspace.mkdir()
        source.mkdir()
        runner = script_run_codex.CodexRunner("gpt-6.1-sol", tmp_path)
        prepare_original = script_run_codex.prepare_arm_home
        observed: dict[str, Any] = {}

        def _prepare(arm: str, **kwargs: Any) -> Any:
            """Prepare an isolated arm home under the test's temporary root."""
            return prepare_original(arm, root=tmp_path, **kwargs)

        def _verify_permission(*_args: Any, **kwargs: Any) -> None:
            """Record permission-verification arguments without running the real host probe."""
            observed.update(kwargs)

        monkeypatch.setattr(codex_diff_impact, "_validate_locked_runtime", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(codex_provisioning, "prepare_arm_home", _prepare)
        monkeypatch.setattr(codex_provisioning, "_write_permission_config", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(codex_provisioning, "_verify_permission_profile", _verify_permission)
        monkeypatch.setattr(codex_provisioning, "_verify_plain_plugin_absent", lambda *_args, **_kwargs: None)

        with runner._prepare_verified_home("A_plain", writable_workspace=workspace, denied_workspace=source):
            assert observed["writable_workspace"] == workspace

    @pytest.mark.usefixtures("stub_home_admission")
    def test_skill_home_keeps_plugin_registration_after_permissions_are_applied(
        self, script_run_codex: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """C retains its enabled plugin tables after the permission profile is applied.

        Prevents the runner from verifying plugin installation and then deleting its registration by replacing
        ``config.toml`` before ``codex exec``.
        """
        repo_path = tmp_path / "target"
        index_path = repo_path / ".cache" / "codemap" / "target.json"
        index_path.parent.mkdir(parents=True)
        index_path.write_text("{}", encoding="utf-8")
        marketplace_root = tmp_path / "marketplace"
        marketplace_root.mkdir()

        def _install_plugins(home: Any, *_args: Any, **_kwargs: Any) -> bool:
            """Supply fixture plugin installation state without contacting a marketplace."""
            config_path = home.path / "config.toml"
            config_text = config_path.read_text(encoding="utf-8")
            config_path.write_text(
                config_text
                + '\n[plugins."codemap-py@borda-ai-rig"]\nenabled = true\n'
                + '\n[plugins."codex-rig@borda-ai-rig"]\nenabled = true\n',
                encoding="utf-8",
            )
            home.codemap_skill_path = config_path
            home.codemap_skill_sha256 = "skill-sha"
            home.env["CODEMAP_SKILL_FILE"] = str(config_path.resolve())
            home.codex_rig_path = home.path
            home.codex_rig_manifest_sha256 = "rig-sha"
            return True

        monkeypatch.setattr(codex_provisioning, "_install_codemap_plugin", _install_plugins)
        monkeypatch.setattr(codex_provisioning, "_admit_installed_skill_pair", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(codex_provisioning, "_verify_installed_plugin_pair", lambda *_args, **_kwargs: None)
        runner = script_run_codex.CodexRunner(
            "gpt-6.1-sol", repo_path, index_path=index_path, marketplace_root=marketplace_root
        )

        with runner._prepare_verified_home("C_strict") as home:
            config_text = (home.path / "config.toml").read_text(encoding="utf-8")
            coordination_path = home.coordination_path

        assert "[permissions.provider-parity-codemap]" in config_text
        assert '[plugins."codemap-py@borda-ai-rig"]' in config_text
        assert '[plugins."codex-rig@borda-ai-rig"]' in config_text
        assert coordination_path is not None
        script_run_codex._cleanup_coordination_root(coordination_path)

    @pytest.mark.usefixtures("stub_home_admission")
    def test_verified_home_overrides_treatment_python_and_removes_it_from_plain(
        self, script_run_codex: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A caller's ``CODEMAP_PYTHON`` cannot select the B/C Python or leak into A.

        The treatment runtime comes from the locked manifest; the plain arm must carry no Python selection at all.
        """
        repo_path = tmp_path / "target"
        index_path = repo_path / ".cache" / "codemap" / "target.json"
        index_path.parent.mkdir(parents=True)
        index_path.write_text("{}", encoding="utf-8")
        monkeypatch.setenv("CODEMAP_PYTHON", "/caller/selected/python")
        monkeypatch.setattr(codex_provisioning, "_verify_plain_plugin_absent", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(codex_provisioning, "_admit_staged_direct_cli", lambda *_args, **_kwargs: None)
        launcher = make_direct_runtime_bundle(tmp_path)
        runner = script_run_codex.CodexRunner("gpt-6.1-sol", repo_path, index_path=index_path, codemap_bin=launcher)

        with runner._prepare_verified_home("A_plain") as plain:
            plain_evidence = script_run_codex.probe_arm_home(plain)
            plain_has_runtime = "CODEMAP_PYTHON" in plain.env
        with runner._prepare_verified_home("B_auto") as treatment:
            treatment_evidence = script_run_codex.probe_arm_home(treatment)
            coordination_path = treatment.coordination_path

        assert plain_evidence["codemap_python"] is None
        assert plain_has_runtime is False
        assert treatment_evidence["codemap_python"] == TREATMENT_PYTHON
        assert coordination_path is not None
        script_run_codex._cleanup_coordination_root(coordination_path)

    def test_structural_snapshot_cleans_a_shared_treatment_coordination_root_once(
        self, script_run_codex: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """B/C snapshot homes release their common index-local root only once.

        A second cleanup of the shared root raises, so cleaning it per home would fail the snapshot.
        """
        events: list[str] = []
        shared_root = tmp_path / ".index-rw"
        live_roots = {shared_root}

        class Home:
            """Minimal snapshot home with the B/C shared coordination path."""

            codemap_plugin_path = tmp_path / "codemap"
            codex_rig_path = tmp_path / "codex-rig"
            codemap_context_path = None

            def __init__(self, arm: str) -> None:
                """Initialize fixture state."""
                self.arm = arm
                self.path = tmp_path / arm
                self.coordination_path = shared_root if arm != "A_plain" else None

            def cleanup(self) -> None:
                """Record that the home was released."""
                events.append(f"home:{self.arm}")

        def _cleanup(path: Path) -> None:
            """Record cleanup at the scenario's isolated-home boundary."""
            events.append(f"coordination:{path.name}")
            if path not in live_roots:
                raise ValueError("Codemap coordination root is unavailable")
            live_roots.remove(path)

        runner = object.__new__(script_run_codex.CodexRunner)
        runner.index_path = tmp_path / "index.json"
        runner.auth_source = None
        monkeypatch.setattr(runner, "_prepare_verified_home", lambda arm: Home(arm))
        monkeypatch.setattr(codex_provisioning, "_write_input_snapshot", lambda *_args, **_kwargs: {"ok": True})
        monkeypatch.setattr(codex_provisioning, "_cleanup_coordination_root", _cleanup)

        snapshot = runner.create_input_snapshot(
            tmp_path / "run",
            tasks_path=SUITE_PATH,
            manifest_path=MANIFEST_PATH,
            tasks=[],
            arms=["A_plain", "B_auto", "C_strict"],
        )

        assert snapshot == {"ok": True}
        assert events == ["home:A_plain", "coordination:.index-rw", "home:B_auto", "home:C_strict"]


class TestLockedTreatmentPython:
    """Treatment Python resolved from the locked manifest's reviewed runtime."""

    @pytest.fixture(name="locked_runtime")
    def _locked_runtime(self, tmp_path: Path) -> tuple[Path, Path]:
        """Manifest requiring Python 3.11, with the executable stand-in it names; returns (manifest, python)."""
        python_path = tmp_path / "python3.11"
        python_path.write_text("#!/bin/sh\n", encoding="utf-8")
        python_path.chmod(0o755)
        manifest_path = tmp_path / "manifest.json"
        runtime = {
            "environment": {"CODEMAP_PYTHON": str(python_path)},
            "required_major_minor": [3, 11],
            "scope": ["B_auto", "C_strict"],
        }
        manifest_path.write_text(
            json.dumps({"codex_permission_profiles": {"treatment_runtime": runtime}}), encoding="utf-8"
        )
        return manifest_path, python_path

    def test_manifest_requires_a_portable_resolver(self) -> None:
        """Treatments resolve Python 3.11 from reviewed candidate paths, never from a host-specific path."""
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        runtime = manifest["codex_permission_profiles"]["treatment_runtime"]

        assert manifest["experiment_revision"]
        assert runtime == {
            "required_major_minor": [3, 11],
            "scope": ["B_auto", "C_strict"],
            "resolution": "first executable Python reporting the required major/minor from the reviewed runtime path candidates",
        }

    def test_returns_the_executable_whose_version_matches(
        self, script_run_codex: Any, locked_runtime: tuple[Path, Path]
    ) -> None:
        """The locked interpreter is version-probed once and returned when it reports the required major/minor."""
        manifest_path, python_path = locked_runtime
        commands: list[list[str]] = []

        def _matching_runtime(command: list[str], **_kwargs: Any) -> SimpleNamespace:
            """Return runtime identity matching the fixture's locked contract."""
            commands.append(command)
            return SimpleNamespace(returncode=0, stdout="3.11.15\n", stderr="")

        resolved = script_run_codex._verify_locked_codemap_python(
            manifest_path=manifest_path, command_runner=_matching_runtime
        )

        assert resolved == str(python_path)
        assert commands == [[str(python_path), "--version"]]

    def test_rejects_a_wrong_version_runtime(self, script_run_codex: Any, locked_runtime: tuple[Path, Path]) -> None:
        """A runtime reporting another minor version is refused before model execution."""
        manifest_path, _python_path = locked_runtime

        def _wrong_runtime(_command: list[str], **_kwargs: Any) -> SimpleNamespace:
            """Return a mismatched runtime identity for admission rejection."""
            return SimpleNamespace(returncode=0, stdout="Python 3.13.5\n", stderr="")

        with pytest.raises(ValueError, match=r"3.11"):
            script_run_codex._verify_locked_codemap_python(manifest_path=manifest_path, command_runner=_wrong_runtime)


class TestCoordinationRoot:
    """Index-local Codemap lock root shared between benchmark readers."""

    def test_prepare_creates_the_exact_safe_lock_root(self, script_run_codex: Any, index_path: Path) -> None:
        """Preparing the root creates the initialized ``.index-rw`` layout next to the index and nothing broader."""
        root = script_run_codex._prepare_coordination_root(index_path)

        assert root == index_path.parent / ".index-rw"
        assert root.is_dir()
        assert (root / "readers").is_dir()
        assert (root / "registry.lock").is_file()
        script_run_codex._validate_coordination_root(root)

    def test_cleanup_removes_the_root_and_keeps_the_locked_index(
        self, script_run_codex: Any, index_path: Path, coordination_root: Path
    ) -> None:
        """Cleaning disposable coordination state leaves the locked index untouched."""
        script_run_codex._cleanup_coordination_root(coordination_root)

        assert not coordination_root.exists()
        assert index_path.read_text(encoding="utf-8") == "{}"

    def test_cleanup_rejects_an_already_removed_root(self, script_run_codex: Any, coordination_root: Path) -> None:
        """A missing coordination root remains an explicit lifecycle error, not a silent no-op."""
        script_run_codex._cleanup_coordination_root(coordination_root)

        with pytest.raises(ValueError, match="coordination root is unavailable"):
            script_run_codex._cleanup_coordination_root(coordination_root)

    def test_prepare_reclaims_an_unlocked_stale_reader_token(
        self, script_run_codex: Any, index_path: Path, coordination_root: Path
    ) -> None:
        """An interrupted reader must not permanently block later benchmark admission."""
        stale_token = coordination_root / "readers" / f"{'0' * 32}.json"
        stale_token.write_text('{"kind":"reader","pid":1}', encoding="utf-8")

        reclaimed = script_run_codex._prepare_coordination_root(index_path)

        assert reclaimed == coordination_root
        assert not stale_token.exists()

    @pytest.mark.usefixtures("live_reader")
    def test_prepare_rejects_a_locked_live_reader_token(self, script_run_codex: Any, index_path: Path) -> None:
        """A genuinely live reader lease remains a fail-closed admission error.

        A real child process holds the Codemap read lock, so only a lock-aware check can tell it from a stale token.
        """
        with pytest.raises(ValueError, match="live reader tokens"):
            script_run_codex._prepare_coordination_root(index_path)

    @pytest.mark.parametrize(
        "link",
        [
            pytest.param(_link_coordination_root, id="root-symlink"),
            pytest.param(_link_readers_directory, id="readers-symlink"),
        ],
    )
    def test_prepare_rejects_symlinks_that_escape_the_index_directory(
        self, script_run_codex: Any, tmp_path: Path, index_path: Path, link: Callable[[Path, Path], None]
    ) -> None:
        """Symlinked lock or readers directories are refused before any write escapes the index directory.

        Concurrent hostile replacement requires separate process-level fault tests.
        """
        escaped_path = tmp_path / "outside"
        escaped_path.mkdir()
        link(index_path.parent / ".index-rw", escaped_path)

        with pytest.raises(ValueError, match=r"symlink|escape|safe|coordination"):
            script_run_codex._prepare_coordination_root(index_path)

        assert not (escaped_path / "registry.lock").exists()
        assert not (escaped_path / "readers").exists()


class TestRealCodexSandbox:
    """Installed Codex sandbox exercised end to end; opt-in because it needs the CLI and a real sandbox."""

    @pytest.mark.integration
    @CODEX_CLI_REQUIRED
    @SANDBOX_INTEGRATION_OPT_IN
    def test_denies_source_and_auth_but_allows_coordination(self, script_run_codex: Any, tmp_path: Path) -> None:
        """The installed sandbox must deny evidence while executing the staged B runtime.

        Runs the real permission-profile verification with an evaluator oracle and credential sentinel present.
        """
        repo_path = tmp_path / "target"
        index_path = repo_path / ".cache" / "codemap" / "target.json"
        index_path.parent.mkdir(parents=True)
        index_path.write_text("{}", encoding="utf-8")
        source_launcher = BENCHMARKS_DIR.parent / "plugins" / "codemap-py" / "bin" / "codemap-py"
        home = script_run_codex.prepare_arm_home("B_auto", root=tmp_path, codemap_bin=source_launcher)
        home.env["CODEMAP_PYTHON"] = str(Path(sys.executable).resolve())
        auth_path = home.path / "auth.json"
        auth_path.write_text('{"fixture":"credential-sentinel"}', encoding="utf-8")
        auth_path.chmod(0o600)
        evidence_root = tmp_path / "evaluator"
        oracle = evidence_root / "benchmarks" / "run-codex-structural.py"
        oracle.parent.mkdir(parents=True)
        oracle.write_text("oracle-sentinel", encoding="utf-8")
        home.coordination_path = script_run_codex._prepare_coordination_root(index_path)
        script_run_codex._write_permission_config(
            home, "B_auto", index_path, evidence_roots=(BENCHMARKS_DIR.parent, evidence_root)
        )

        try:
            script_run_codex._verify_permission_profile(home, repo_path, index_path)
        finally:
            script_run_codex._cleanup_coordination_root(home.coordination_path)
            home.cleanup()

        assert not any(repo_path.glob(".codex-parity-deny-*"))
        assert index_path.read_text(encoding="utf-8") == "{}"

    @pytest.mark.integration
    @CODEX_CLI_REQUIRED
    @SANDBOX_INTEGRATION_OPT_IN
    def test_plain_profile_cannot_read_the_locked_index(self, script_run_codex: Any, tmp_path: Path) -> None:
        """A shares the locked target while the installed sandbox denies its index.

        Runs the real permission-profile verification with a readable-looking sentinel in the index.
        """
        repo_path = tmp_path / "target"
        index_path = repo_path / ".cache" / "codemap" / "target.json"
        index_path.parent.mkdir(parents=True)
        index_path.write_text('{"sentinel":"must-not-be-readable"}', encoding="utf-8")
        home = script_run_codex.prepare_arm_home("A_plain", root=tmp_path)
        auth_path = home.path / "auth.json"
        auth_path.write_text('{"fixture":"credential-sentinel"}', encoding="utf-8")
        auth_path.chmod(0o600)
        script_run_codex._write_permission_config(home, "A_plain", index_path)

        try:
            script_run_codex._verify_permission_profile(home, repo_path, index_path)
        finally:
            home.cleanup()

        assert index_path.read_text(encoding="utf-8") == '{"sentinel":"must-not-be-readable"}'

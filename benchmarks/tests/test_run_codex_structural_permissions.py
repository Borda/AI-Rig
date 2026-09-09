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
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

BENCHMARKS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BENCHMARKS_DIR))

from _bench_common import mutation_isolation  # noqa: E402

SCRIPT_PATH = BENCHMARKS_DIR / "run-codex-structural.py"
MANIFEST_PATH = BENCHMARKS_DIR / "manifests" / "codex-integration.json"
SUITE_PATH = BENCHMARKS_DIR / "suites" / "tasks-bench.json"
POSIX_SECURITY = pytest.mark.skipif(os.name == "nt", reason="requires POSIX private-mode and ownership semantics")


def _write_runtime_snapshot_metadata(
    snapshot_root: Path,
    sources: dict[str, Path],
    *,
    locked_files: dict[str, Path] | None = None,
) -> None:
    """Write the minimal input identity ledger required by snapshot binding tests.

    Example:
        >>> from tempfile import TemporaryDirectory
        >>> with TemporaryDirectory() as directory:
        ...     root = Path(directory)
        ...     _write_runtime_snapshot_metadata(root, {})
        ...     json.loads((root / "input-snapshot.json").read_text())
        {'files': []}
    """
    files = []
    for role, source in sources.items():
        for path in sorted(source.rglob("*")):
            if path.is_file():
                archived_mode = 0o700 if stat.S_IMODE(path.stat().st_mode) & 0o111 else 0o600
                path.chmod(archived_mode)
                files.append(
                    {
                        "role": role,
                        "archived_path": path.relative_to(snapshot_root).as_posix(),
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "bytes": path.stat().st_size,
                        "mode": archived_mode,
                    }
                )
    for role, path in (locked_files or {}).items():
        archived_mode = 0o700 if stat.S_IMODE(path.stat().st_mode) & 0o111 else 0o600
        path.chmod(archived_mode)
        files.append(
            {
                "role": role,
                "archived_path": path.relative_to(snapshot_root).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "bytes": path.stat().st_size,
                "mode": archived_mode,
            }
        )
    (snapshot_root / "input-snapshot.json").write_text(json.dumps({"files": files}), encoding="utf-8")


def _make_direct_runtime_bundle(root: Path) -> Path:
    """Create the minimum source-shaped direct CLI runtime used by isolation tests.

    Example:
        >>> from tempfile import TemporaryDirectory
        >>> with TemporaryDirectory() as directory:
        ...     launcher = _make_direct_runtime_bundle(Path(directory))
        ...     launcher.name, (launcher.parents[1] / "src/codemap_py/__init__.py").is_file()
        ('codemap-py', True)
    """
    runtime = root / "codemap-runtime"
    launcher = runtime / "bin" / "codemap-py"
    exclusions = runtime / "bin" / "_exclusions.py"
    entrypoint = runtime / "scripts" / "codemap_py_entry.py"
    package = runtime / "src" / "codemap_py"
    launcher.parent.mkdir(parents=True)
    entrypoint.parent.mkdir(parents=True)
    package.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)
    exclusions.write_text("EXCLUSION_PATTERNS = ()\n", encoding="utf-8")
    entrypoint.write_text("raise SystemExit(0)\n", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    return launcher


def test_codex_command_is_ephemeral_json_profile_backed_and_keeps_prompt_exact(
    script_run_codex: Any, tmp_path: Path
) -> None:
    """The transport plan must use isolated profiles and preserve prompt bytes.

    Prevents a legacy command-line sandbox setting from overriding the disposable home's permission profile.
    """
    prompt = "Return the callers exactly.\nSecond line stays unchanged."

    command = script_run_codex.build_codex_command(
        repo_path=tmp_path,
        model="fixture-model",
        reasoning_effort="high",
        prompt=prompt,
    )

    assert command[:2] == ["codex", "exec"]
    assert "--json" in command
    assert "--ephemeral" in command
    assert "--strict-config" in command
    assert "--sandbox" not in command
    assert command[command.index("--config") + 1] == 'model_reasoning_effort="high"'
    assert command[command.index("--cd") + 1] == str(tmp_path)
    assert command[command.index("--model") + 1] == "fixture-model"
    assert command[-1] == prompt


def test_executable_command_uses_profile_permissions_without_legacy_sandbox(
    script_run_codex: Any, tmp_path: Path
) -> None:
    """Executable cells rely on the verified profile rather than a CLI sandbox override."""
    command = script_run_codex.build_codex_command(
        repo_path=tmp_path,
        model="fixture-model",
        reasoning_effort="high",
        prompt="Edit the disposable worktree.",
    )

    assert "--sandbox" not in command


def test_worktree_index_relocation_changes_only_scan_root(script_run_codex: Any, tmp_path: Path) -> None:
    """An executable worktree gets a valid graph without mutating graph content."""
    source_root = tmp_path / "source"
    worktree_root = tmp_path / "worktree"
    frozen_payload = {
        "scan_root": str(source_root.resolve()),
        "scan_version": 13,
        "modules": {"pkg.module": {"symbols": ["target"]}},
    }
    frozen_bytes = json.dumps(frozen_payload, indent=2, sort_keys=True).encode("utf-8")

    derived_bytes, relocation = script_run_codex.relocate_frozen_index_for_worktree(
        frozen_bytes,
        source_root=source_root,
        worktree_root=worktree_root,
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


def test_worktree_index_relocation_rejects_an_unrelated_source_root(script_run_codex: Any, tmp_path: Path) -> None:
    """A relocated graph must originate from the frozen benchmark repository."""
    frozen_bytes = json.dumps({"scan_root": str(tmp_path / "other")}).encode("utf-8")

    with pytest.raises(ValueError, match="scan_root"):
        script_run_codex.relocate_frozen_index_for_worktree(
            frozen_bytes,
            source_root=tmp_path / "source",
            worktree_root=tmp_path / "worktree",
        )


def test_codex_stratum_locks_luna_and_high_effort(script_run_codex: Any) -> None:
    """The accepted model/effort pair is consumed from the active manifest."""
    script_run_codex._validate_codex_stratum("gpt-5.6-luna", "high", MANIFEST_PATH)

    with pytest.raises(ValueError, match="gpt-5.6-luna"):
        script_run_codex._validate_codex_stratum("gpt-5.3-codex", "high", MANIFEST_PATH)
    with pytest.raises(ValueError, match="reasoning effort"):
        script_run_codex._validate_codex_stratum("gpt-5.6-luna", "medium", MANIFEST_PATH)


def test_deterministic_order_uses_only_current_plain_cli_skill_arms(script_run_codex: Any) -> None:
    """Prevent the historical auto/required arm registry leaking into the new experiment."""
    first = script_run_codex._manifest_arm_order(
        "codex-integration-v1",
        "gpt-5.6-luna",
        "FN-02",
        1,
        "high",
    )
    second = script_run_codex._manifest_arm_order(
        "codex-integration-v1",
        "gpt-5.6-luna",
        "FN-02",
        1,
        "high",
    )

    assert first == second
    assert set(first) == {"A_plain", "B_auto", "C_strict"}


def test_exact_suite_counterbalances_arm_ordinals_at_one_repetition(script_run_codex: Any) -> None:
    """Each treatment must occupy every ordinal 18 or 19 times across all 55 tasks.

    Prevents a deterministic hash order from making prompt-cache exposure a systematic arm confound while preserving
    repeatable execution planning.
    """
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    task_ids = manifest["preregistered_cells"]["structural_execution_task_ids"]
    assert len(task_ids) == 55
    ordinals_by_arm = {arm: [0, 0, 0] for arm in script_run_codex.CODEX_STRUCTURAL_ARMS}

    for task_id in task_ids:
        first = script_run_codex._manifest_arm_order(
            "codex-integration-v1",
            script_run_codex.PARITY_CODEX_MODEL,
            task_id,
            1,
            script_run_codex.PARITY_CODEX_REASONING_EFFORT,
        )
        second = script_run_codex._manifest_arm_order(
            "codex-integration-v1",
            script_run_codex.PARITY_CODEX_MODEL,
            task_id,
            1,
            script_run_codex.PARITY_CODEX_REASONING_EFFORT,
        )

        assert first == second
        for ordinal, arm in enumerate(first):
            ordinals_by_arm[arm][ordinal] += 1

    assert all(count in {18, 19} for counts in ordinals_by_arm.values() for count in counts), ordinals_by_arm


@POSIX_SECURITY
def test_permission_profiles_replace_legacy_sandbox_and_grant_only_coordination_write(
    script_run_codex: Any, tmp_path: Path
) -> None:
    """Reject legacy sandbox flags, missing profiles, and writes outside Codemap's lock root.

    Exact config assertions prevent implicit or overly broad permissions.
    """
    repo_path = tmp_path / "target"
    index_path = repo_path / ".cache" / "codemap" / "locked-index.json"
    index_path.parent.mkdir(parents=True)
    index_path.write_text("{}", encoding="utf-8")
    home_path = tmp_path / "codex-home"
    home_path.mkdir()
    auth_path = home_path / "auth.json"
    auth_path.write_text("fixture-auth", encoding="utf-8")
    home = script_run_codex.ArmHome(
        "A_plain",
        home_path,
        {"PATH": "/fixture/bin", "API_TOKEN": "must-not-leak", "SSH_AUTH_SOCK": "/fixture/socket"},
        False,
    )

    plain_config = script_run_codex._write_permission_config(home, "A_plain", index_path)
    plain_text = plain_config.read_text(encoding="utf-8")
    assert plain_config == home_path / "config.toml"
    assert plain_config.stat().st_mode & 0o777 == 0o600
    assert 'default_permissions = "provider-parity-plain"' in plain_text
    assert "\n[permissions]\n" in plain_text
    assert "[permissions.provider-parity-plain]" in plain_text
    assert 'extends = ":read-only"' in plain_text
    assert f'"{auth_path.resolve()}" = "deny"' in plain_text
    assert f'"{index_path.parent.resolve()}" = "deny"' in plain_text
    assert "[permissions.provider-parity-plain.network]" in plain_text
    assert "enabled = false" in plain_text
    assert '"write"' not in plain_text
    assert "[shell_environment_policy]" in plain_text
    assert 'inherit = "none"' in plain_text
    assert "API_TOKEN" not in plain_text
    assert "SSH_AUTH_SOCK" not in plain_text
    for denied_root in script_run_codex._untrusted_host_agent_roots(home, "A_plain"):
        assert f'"{denied_root}" = "deny"' in plain_text

    for arm in ("B_auto", "C_strict"):
        treatment_home_path = tmp_path / f"codex-home-{arm}"
        treatment_home_path.mkdir()
        treatment_auth_path = treatment_home_path / "auth.json"
        treatment_auth_path.write_text("fixture-auth", encoding="utf-8")
        treatment_home = script_run_codex.ArmHome(
            arm,
            treatment_home_path,
            {"PATH": "/fixture/bin"},
            True,
            True,
        )
        marketplace_root = tmp_path / "marketplace"
        treatment_config = script_run_codex._write_permission_config(
            treatment_home,
            arm,
            index_path,
            marketplace_root=marketplace_root,
        )
        treatment_text = treatment_config.read_text(encoding="utf-8")
        coordination_root = index_path.parent / ".index-rw"

        assert treatment_config == treatment_home_path / "config.toml"
        assert 'default_permissions = "provider-parity-codemap"' in treatment_text
        assert "[permissions.provider-parity-codemap]" in treatment_text
        assert 'extends = ":read-only"' in treatment_text
        assert f'"{treatment_auth_path.resolve()}" = "deny"' in treatment_text
        assert f'"{coordination_root.resolve()}" = "write"' in treatment_text
        assert "[permissions.provider-parity-codemap.network]" in treatment_text
        assert "enabled = false" in treatment_text
        assert "sandbox_mode" not in treatment_text
        assert "sandbox_workspace_write" not in treatment_text
        for denied_root in script_run_codex._untrusted_host_agent_roots(treatment_home, arm, marketplace_root):
            assert f'"{denied_root}" = "deny"' in treatment_text


def test_executable_workspace_permission_grants_only_the_disposable_worktree(
    script_run_codex: Any, tmp_path: Path
) -> None:
    """Executable cells receive a bounded worktree grant while the source stays denied."""
    home_path = tmp_path / "codex-home"
    home_path.mkdir()
    source_path = tmp_path / "source"
    source_path.mkdir()
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    index_path = workspace_path / ".cache" / "codemap" / "locked-index.json"
    index_path.parent.mkdir(parents=True)
    index_path.write_text("{}", encoding="utf-8")
    home = script_run_codex.ArmHome("A_plain", home_path, {"PATH": "/fixture/bin"}, False)

    config = script_run_codex._write_permission_config(
        home,
        "A_plain",
        index_path,
        writable_workspace=workspace_path,
        denied_workspace=source_path,
    )

    text = config.read_text(encoding="utf-8")
    assert 'extends = ":read-only"' in text
    # Paths enter the TOML as basic strings, so the expectation is the escaped rendering
    # rather than the raw path: a Windows separator is a TOML escape character.
    assert f'{json.dumps(str(workspace_path.resolve()))} = "write"' in text
    assert f'{json.dumps(str(source_path.resolve()))} = "deny"' in text


def test_benchmark_evidence_roots_require_absolute_directories(script_run_codex: Any, tmp_path: Path) -> None:
    """Reject evidence-boundary coordinates that cannot be enforced by the sandbox."""
    evidence_root = tmp_path / "evaluator"
    evidence_root.mkdir()

    assert script_run_codex._benchmark_evidence_roots({}) == (BENCHMARKS_DIR.parent.resolve(),)
    assert script_run_codex._benchmark_evidence_roots(
        {"BENCHMARK_EVIDENCE_ROOTS": json.dumps([str(evidence_root)])}
    ) == (evidence_root.resolve(),)

    with pytest.raises(ValueError, match="absolute|evidence"):
        script_run_codex._benchmark_evidence_roots({"BENCHMARK_EVIDENCE_ROOTS": '["relative"]'})
    with pytest.raises(ValueError, match="path strings|evidence"):
        script_run_codex._benchmark_evidence_roots({"BENCHMARK_EVIDENCE_ROOTS": "[]"})

    missing_root = tmp_path / "future-results"
    assert script_run_codex._benchmark_evidence_roots(
        {"BENCHMARK_EVIDENCE_ROOTS": json.dumps([str(missing_root)])}
    ) == (missing_root.resolve(),)

    non_directory = tmp_path / "not-a-directory"
    non_directory.write_text("fixture", encoding="utf-8")
    with pytest.raises(ValueError, match="directory|evidence"):
        script_run_codex._benchmark_evidence_roots({"BENCHMARK_EVIDENCE_ROOTS": json.dumps([str(non_directory)])})


def test_runner_reads_parent_evidence_roots_without_forwarding_them_to_arm_homes(
    script_run_codex: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Direct stage adapters inherit the parent evaluator boundary without a constructor argument."""
    evidence_root = tmp_path / "evaluator"
    evidence_root.mkdir()
    monkeypatch.setenv("BENCHMARK_EVIDENCE_ROOTS", json.dumps([str(evidence_root)]))

    runner = script_run_codex.CodexRunner("fixture-model", tmp_path)

    assert runner.evidence_roots == (evidence_root.resolve(),)


def test_permission_config_denies_evaluator_evidence_and_records_an_actual_oracle_probe(
    script_run_codex: Any, tmp_path: Path
) -> None:
    """A model must not read the frozen evaluator source used to score its answer."""
    repo_path = tmp_path / "target"
    index_path = repo_path / ".cache" / "codemap" / "locked-index.json"
    index_path.parent.mkdir(parents=True)
    index_path.write_text("{}", encoding="utf-8")
    evidence_root = tmp_path / "evaluator"
    oracle = evidence_root / "benchmarks" / "run-codex-structural.py"
    oracle.parent.mkdir(parents=True)
    oracle.write_text("oracle", encoding="utf-8")
    home_path = tmp_path / "codex-home"
    home_path.mkdir()
    home = script_run_codex.ArmHome("A_plain", home_path, {"PATH": "/fixture/bin"}, False)

    config = script_run_codex._write_permission_config(
        home,
        "A_plain",
        index_path,
        evidence_roots=(evidence_root,),
    )

    assert f'{json.dumps(str(evidence_root.resolve()))} = "deny"' in config.read_text(encoding="utf-8")
    assert home.evidence_probe_paths == (oracle.resolve(),)


def test_permission_profile_fails_when_an_actual_evaluator_oracle_is_readable(
    script_run_codex: Any, tmp_path: Path
) -> None:
    """A denied root enumeration cannot substitute for an oracle-file read check."""
    repo_path = tmp_path / "target"
    index_path = repo_path / ".cache" / "codemap" / "locked-index.json"
    index_path.parent.mkdir(parents=True)
    index_path.write_text("{}", encoding="utf-8")
    evidence_root = tmp_path / "evaluator"
    oracle = evidence_root / "benchmarks" / "run-codex-structural.py"
    oracle.parent.mkdir(parents=True)
    oracle.write_text("oracle", encoding="utf-8")
    home_path = tmp_path / "codex-home"
    home_path.mkdir()
    home = script_run_codex.ArmHome("A_plain", home_path, {"PATH": "/fixture/bin"}, False)
    script_run_codex._write_permission_config(home, "A_plain", index_path, evidence_roots=(evidence_root,))

    def _oracle_leak(command: list[str], **_kwargs: Any) -> SimpleNamespace:
        """Emulate a profile that leaks only the evaluator oracle fixture."""
        if command == ["codex", "--version"]:
            return SimpleNamespace(returncode=0, stdout="codex-cli fixture", stderr="")
        if command[-1] == "pass":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if str(oracle) in command:
            return SimpleNamespace(returncode=0, stdout="oracle", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="Operation not permitted")

    with pytest.raises(ValueError, match="evaluator evidence"):
        script_run_codex._verify_permission_profile(home, repo_path, index_path, command_runner=_oracle_leak)


def test_bound_snapshot_is_denied_as_evidence_while_staged_arm_runtime_remains_accessible(
    script_run_codex: Any, tmp_path: Path
) -> None:
    """B may use its home copy without retaining read access to archived runtime inputs."""
    snapshot_root = tmp_path / "results" / "inputs"
    launcher = _make_direct_runtime_bundle(snapshot_root / "B_auto")
    source_root = launcher.parent.parent
    _write_runtime_snapshot_metadata(snapshot_root, {"B_auto:direct-cli": source_root})
    runner = script_run_codex.CodexRunner("fixture-model", tmp_path)
    runner._bind_runtime_snapshot(snapshot_root, {"B_auto": {"direct-cli": source_root}})
    home_path = tmp_path / "codex-home"
    home_path.mkdir()
    staged_runtime = home_path / "direct-cli"
    shutil.copytree(source_root, staged_runtime)
    home = script_run_codex.ArmHome("B_auto", home_path, {"PATH": "/fixture/bin"}, True, True)
    index_path = tmp_path / "target" / ".cache" / "codemap" / "locked-index.json"
    index_path.parent.mkdir(parents=True)
    index_path.write_text("{}", encoding="utf-8")

    config = script_run_codex._write_permission_config(
        home,
        "B_auto",
        index_path,
        evidence_roots=runner.evidence_roots,
    )

    text = config.read_text(encoding="utf-8")
    assert f'{json.dumps(str(snapshot_root.parent.resolve()))} = "deny"' in text
    assert str(staged_runtime) not in text
    assert launcher.is_file()

    runner._record_runtime_failure("B_auto", ValueError("fixture runtime failure"))

    evidence = json.loads((snapshot_root.parent / "runtime-isolation.jsonl").read_text(encoding="utf-8"))
    assert str(snapshot_root.parent.resolve()) in evidence["evidence_roots"]


def test_prepare_verified_home_passes_writable_workspace_to_permission_verifier(
    script_run_codex: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Executable homes verify the workspace-write permission instead of source denial."""
    workspace = tmp_path / "workspace"
    source = tmp_path / "source"
    workspace.mkdir()
    source.mkdir()
    runner = script_run_codex.CodexRunner("fixture-model", tmp_path)
    prepare_original = script_run_codex.prepare_arm_home
    observed: dict[str, Any] = {}

    def _prepare(arm: str, **kwargs: Any) -> Any:
        """Prepare and record an isolated arm home."""
        return prepare_original(arm, root=tmp_path, **kwargs)

    def _verify_permission(*_args: Any, **kwargs: Any) -> None:
        """Record permission-verification arguments without running the real host probe."""
        observed.update(kwargs)

    monkeypatch.setattr(script_run_codex, "_validate_locked_runtime", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(script_run_codex, "prepare_arm_home", _prepare)
    monkeypatch.setattr(script_run_codex, "_write_permission_config", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(script_run_codex, "_verify_permission_profile", _verify_permission)
    monkeypatch.setattr(script_run_codex, "_verify_plain_plugin_absent", lambda *_args, **_kwargs: None)

    home = runner._prepare_verified_home("A_plain", writable_workspace=workspace, denied_workspace=source)
    try:
        assert observed["writable_workspace"] == workspace
    finally:
        home.cleanup()


def test_plain_and_direct_homes_drop_host_skill_file_binding(
    script_run_codex: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A/B must not inherit a host Skill path that could contaminate treatment."""
    monkeypatch.setenv("CODEMAP_SKILL_FILE", "/host/untrusted/SKILL.md")
    monkeypatch.setenv("BENCHMARK_EVIDENCE_ROOTS", '["/host/evaluator"]')
    launcher = _make_direct_runtime_bundle(tmp_path / "direct-runtime")

    with script_run_codex.prepare_arm_home("A_plain", root=tmp_path) as plain_home:
        assert "CODEMAP_SKILL_FILE" not in plain_home.env
        assert "BENCHMARK_EVIDENCE_ROOTS" not in plain_home.env
    with script_run_codex.prepare_arm_home(
        "B_auto",
        root=tmp_path,
        codemap_bin=launcher,
    ) as direct_home:
        assert "CODEMAP_SKILL_FILE" not in direct_home.env
        assert "BENCHMARK_EVIDENCE_ROOTS" not in direct_home.env


def test_default_arm_home_uses_canonical_temp_root(
    script_run_codex: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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


def test_explicit_arm_home_root_rejects_symlink_components(script_run_codex: Any, tmp_path: Path) -> None:
    """Caller-supplied roots remain strict even when their target is a directory."""
    canonical_root = tmp_path / "canonical-temp"
    canonical_root.mkdir()
    temp_alias = tmp_path / "temp-alias"
    temp_alias.symlink_to(canonical_root, target_is_directory=True)

    with pytest.raises(ValueError, match=re.escape(f"permission path contains a symlink: {temp_alias}")):
        script_run_codex.prepare_arm_home("A_plain", root=temp_alias)


def test_skill_home_preserves_plugin_registration_when_permissions_are_applied(
    script_run_codex: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C must retain enabled plugin tables after applying its permission profile.

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

    monkeypatch.setattr(script_run_codex, "_validate_locked_runtime", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        script_run_codex,
        "_verify_locked_codemap_python",
        lambda **_kwargs: "/opt/homebrew/bin/python3.11",
    )
    monkeypatch.setattr(script_run_codex, "_install_codemap_plugin", _install_plugins)
    monkeypatch.setattr(script_run_codex, "_verify_treatment_artifact_locks", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(script_run_codex, "_admit_installed_skill_pair", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(script_run_codex, "_verify_installed_plugin_pair", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(script_run_codex, "_verify_permission_profile", lambda *_args, **_kwargs: None)
    runner = script_run_codex.CodexRunner(
        "fixture-model",
        repo_path,
        index_path=index_path,
        marketplace_root=marketplace_root,
    )

    with runner._prepare_verified_home("C_strict") as home:
        config_text = (home.path / "config.toml").read_text(encoding="utf-8")
        coordination_path = home.coordination_path

    assert "[permissions.provider-parity-codemap]" in config_text
    assert '[plugins."codemap-py@borda-ai-rig"]' in config_text
    assert '[plugins."codex-rig@borda-ai-rig"]' in config_text
    assert coordination_path is not None
    script_run_codex._cleanup_coordination_root(coordination_path)


def test_active_manifest_requires_a_portable_treatment_python_resolver() -> None:
    """Treatments must resolve Python 3.11 from reviewed candidate paths, not a host path."""
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    runtime = manifest["codex_permission_profiles"]["treatment_runtime"]

    assert manifest["experiment_revision"]
    assert runtime == {
        "required_major_minor": [3, 11],
        "scope": ["B_auto", "C_strict"],
        "resolution": "first executable Python reporting the required major/minor from the reviewed runtime path candidates",
    }


def test_locked_treatment_python_is_executable_and_version_checked(
    script_run_codex: Any,
    tmp_path: Path,
) -> None:
    """Reject missing or wrong-version treatment runtimes before model execution."""
    python_path = tmp_path / "python3.11"
    python_path.write_text("#!/bin/sh\n", encoding="utf-8")
    python_path.chmod(0o755)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "codex_permission_profiles": {
                    "treatment_runtime": {
                        "environment": {"CODEMAP_PYTHON": str(python_path)},
                        "required_major_minor": [3, 11],
                        "scope": ["B_auto", "C_strict"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    commands: list[list[str]] = []

    def _matching_runtime(command: list[str], **_kwargs: Any) -> SimpleNamespace:
        """Return runtime identity matching the fixture's locked contract."""
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="3.11.15\n", stderr="")

    assert script_run_codex._verify_locked_codemap_python(
        manifest_path=manifest_path,
        command_runner=_matching_runtime,
    ) == str(python_path)
    assert commands == [[str(python_path), "--version"]]

    def _wrong_runtime(_command: list[str], **_kwargs: Any) -> SimpleNamespace:
        """Return a mismatched runtime identity for admission rejection."""
        return SimpleNamespace(returncode=0, stdout="Python 3.13.5\n", stderr="")

    with pytest.raises(ValueError, match="3.11"):
        script_run_codex._verify_locked_codemap_python(
            manifest_path=manifest_path,
            command_runner=_wrong_runtime,
        )


def test_verified_home_overrides_treatment_python_and_removes_it_from_plain(
    script_run_codex: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove caller environment cannot select B/C Python or leak it into A."""
    repo_path = tmp_path / "target"
    index_path = repo_path / ".cache" / "codemap" / "target.json"
    index_path.parent.mkdir(parents=True)
    index_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CODEMAP_PYTHON", "/caller/selected/python")
    monkeypatch.setattr(script_run_codex, "_validate_locked_runtime", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(script_run_codex, "_verify_permission_profile", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(script_run_codex, "_verify_plain_plugin_absent", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        script_run_codex,
        "_verify_locked_codemap_python",
        lambda **_kwargs: "/opt/homebrew/bin/python3.11",
    )
    monkeypatch.setattr(script_run_codex, "_verify_treatment_artifact_locks", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(script_run_codex, "_admit_staged_direct_cli", lambda *_args, **_kwargs: None)
    launcher = _make_direct_runtime_bundle(tmp_path)
    runner = script_run_codex.CodexRunner(
        "fixture-model",
        repo_path,
        index_path=index_path,
        codemap_bin=launcher,
    )

    with runner._prepare_verified_home("A_plain") as plain:
        plain_evidence = script_run_codex.probe_arm_home(plain)
        plain_has_runtime = "CODEMAP_PYTHON" in plain.env
    with runner._prepare_verified_home("B_auto") as treatment:
        treatment_evidence = script_run_codex.probe_arm_home(treatment)
        coordination_path = treatment.coordination_path

    assert plain_evidence["codemap_python"] is None
    assert plain_has_runtime is False
    assert treatment_evidence["codemap_python"] == "/opt/homebrew/bin/python3.11"
    assert coordination_path is not None
    script_run_codex._cleanup_coordination_root(coordination_path)


def test_coordination_root_is_exact_safe_and_cleanup_keeps_the_locked_index(
    script_run_codex: Any, tmp_path: Path
) -> None:
    """Require the exact initialized index-local lock root.

    Reject broader write access and preserve the locked index when cleaning disposable coordination state.
    """
    index_path = tmp_path / "target" / ".cache" / "codemap" / "locked-index.json"
    index_path.parent.mkdir(parents=True)
    index_path.write_text("locked", encoding="utf-8")

    coordination_root = script_run_codex._prepare_coordination_root(index_path)

    assert coordination_root == index_path.parent / ".index-rw"
    assert coordination_root.is_dir()
    assert (coordination_root / "readers").is_dir()
    assert (coordination_root / "registry.lock").is_file()
    script_run_codex._validate_coordination_root(coordination_root)

    script_run_codex._cleanup_coordination_root(coordination_root)

    assert not coordination_root.exists()
    assert index_path.read_text(encoding="utf-8") == "locked"


def test_coordination_root_reclaims_an_unlocked_stale_reader_token(script_run_codex: Any, tmp_path: Path) -> None:
    """An interrupted reader must not permanently block later benchmark admission."""
    index_path = tmp_path / "target" / ".cache" / "codemap" / "locked-index.json"
    index_path.parent.mkdir(parents=True)
    index_path.write_text("locked", encoding="utf-8")
    coordination_root = script_run_codex._prepare_coordination_root(index_path)
    stale_token = coordination_root / "readers" / f"{'0' * 32}.json"
    stale_token.write_text('{"kind":"reader","pid":1}', encoding="utf-8")

    assert script_run_codex._prepare_coordination_root(index_path) == coordination_root
    assert not stale_token.exists()

    script_run_codex._cleanup_coordination_root(coordination_root)


def test_coordination_root_rejects_a_locked_live_reader_token(script_run_codex: Any, tmp_path: Path) -> None:
    """A genuinely live reader lease must remain a fail-closed admission error."""
    index_path = tmp_path / "target" / ".cache" / "codemap" / "locked-index.json"
    index_path.parent.mkdir(parents=True)
    index_path.write_text("{}", encoding="utf-8")
    coordination_root = script_run_codex._prepare_coordination_root(index_path)
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
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "ready"
        with pytest.raises(ValueError, match="live reader tokens"):
            script_run_codex._prepare_coordination_root(index_path)
    finally:
        child.communicate("\n", timeout=10)

    assert child.returncode == 0
    script_run_codex._cleanup_coordination_root(coordination_root)


def test_coordination_root_cleanup_rejects_an_already_removed_root(script_run_codex: Any, tmp_path: Path) -> None:
    """A missing coordination root remains an explicit lifecycle error."""
    index_path = tmp_path / "target" / ".cache" / "codemap" / "locked-index.json"
    index_path.parent.mkdir(parents=True)
    index_path.write_text("locked", encoding="utf-8")
    coordination_root = script_run_codex._prepare_coordination_root(index_path)

    script_run_codex._cleanup_coordination_root(coordination_root)

    with pytest.raises(ValueError, match="coordination root is unavailable"):
        script_run_codex._cleanup_coordination_root(coordination_root)


def test_structural_snapshot_cleans_a_shared_treatment_coordination_root_once(
    script_run_codex: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """B/C snapshot homes release their common index-local root only once."""
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
            """Provide fixture cleanup."""
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
    monkeypatch.setattr(script_run_codex, "_write_input_snapshot", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr(script_run_codex, "_cleanup_coordination_root", _cleanup)

    assert runner.create_input_snapshot(
        tmp_path / "run",
        tasks_path=SUITE_PATH,
        manifest_path=MANIFEST_PATH,
        tasks=[],
        arms=["A_plain", "B_auto", "C_strict"],
    ) == {"ok": True}
    assert events == ["home:A_plain", "coordination:.index-rw", "home:B_auto", "home:C_strict"]


@pytest.mark.parametrize("unsafe_entry", ["coord-symlink", "readers-symlink"])
def test_coordination_root_rejects_symlinks_and_cannot_escape_its_index_directory(
    script_run_codex: Any, tmp_path: Path, unsafe_entry: str
) -> None:
    """Reject symlinked lock/readers directories before writes escape the index.

    Concurrent hostile replacement requires separate process-level fault tests.
    """
    index_path = tmp_path / "target" / ".cache" / "codemap" / "locked-index.json"
    index_path.parent.mkdir(parents=True)
    index_path.write_text("locked", encoding="utf-8")
    escaped_path = tmp_path / "outside"
    escaped_path.mkdir()
    coordination_root = index_path.parent / ".index-rw"

    if unsafe_entry == "coord-symlink":
        coordination_root.symlink_to(escaped_path, target_is_directory=True)
    else:
        coordination_root.mkdir()
        (coordination_root / "readers").symlink_to(escaped_path, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink|escape|safe|coordination"):
        script_run_codex._prepare_coordination_root(index_path)

    assert not (escaped_path / "registry.lock").exists()
    assert not (escaped_path / "readers").exists()


def test_permission_profile_verification_fails_closed_when_codex_rejects_the_profile(
    script_run_codex: Any, tmp_path: Path
) -> None:
    """An unsupported permission profile cannot silently run under Codex defaults.

    Prevents the profile probe from treating an unknown profile as a successful setup.  A check that only validates the
    Codex binary version would fail to raise here.
    """
    repo_path = tmp_path / "target"
    repo_path.mkdir()
    home_path = tmp_path / "codex-home"
    home_path.mkdir()
    home = script_run_codex.ArmHome("B_auto", home_path, {}, True, True)

    def _reject_profile(command: list[str], **_kwargs: Any) -> SimpleNamespace:
        """Reject the selected profile at the fixture validation boundary."""
        if "--version" in command:
            return SimpleNamespace(returncode=0, stdout="codex-cli 0.138.0", stderr="")
        return SimpleNamespace(returncode=2, stdout="", stderr="unknown permission profile provider-parity-codemap")

    with pytest.raises(ValueError, match="profile|permission|unsupported"):
        script_run_codex._verify_permission_profile(home, repo_path, command_runner=_reject_profile)


def test_permission_profile_resolves_workspace_python_symlink_before_sandbox(
    script_run_codex: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A project virtualenv launcher must not be denied as a source-tree executable."""
    repo_path = tmp_path / "target"
    python_dir = repo_path / ".venv" / "bin"
    python_dir.mkdir(parents=True)
    external_python = tmp_path / "runtime" / "python3"
    external_python.parent.mkdir()
    external_python.write_text("fixture", encoding="utf-8")
    external_python.chmod(0o755)
    workspace_python = python_dir / "python3"
    workspace_python.symlink_to(external_python)
    home_path = tmp_path / "codex-home"
    home_path.mkdir()
    home = script_run_codex.ArmHome("A_plain", home_path, {}, False)
    commands: list[list[str]] = []

    def _reject_after_capture(command: list[str], **_kwargs: Any) -> SimpleNamespace:
        """Capture validation input before rejecting the fixture runtime."""
        commands.append(command)
        if command == ["codex", "--version"]:
            return SimpleNamespace(returncode=0, stdout="codex-cli 0.146.1", stderr="")
        return SimpleNamespace(returncode=2, stdout="", stderr="fixture stop")

    monkeypatch.setattr(script_run_codex.sys, "executable", str(workspace_python))

    with pytest.raises(ValueError, match="profile|permission|unsupported"):
        script_run_codex._verify_permission_profile(home, repo_path, command_runner=_reject_after_capture)

    sandbox_command = commands[1]
    interpreter = sandbox_command[sandbox_command.index("--") + 1]
    assert interpreter == str(external_python.resolve())


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("codex") is None, reason="Codex CLI is unavailable")
@pytest.mark.skipif(
    os.environ.get("RUN_CODEX_SANDBOX_INTEGRATION") != "1",
    reason="set RUN_CODEX_SANDBOX_INTEGRATION=1 to exercise the installed Codex sandbox",
)
def test_real_codex_profile_denies_source_and_auth_but_allows_coordination(
    script_run_codex: Any, tmp_path: Path
) -> None:
    """The installed sandbox must deny evidence while executing the staged B runtime."""
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
        home,
        "B_auto",
        index_path,
        evidence_roots=(BENCHMARKS_DIR.parent, evidence_root),
    )

    try:
        script_run_codex._verify_permission_profile(home, repo_path, index_path)
    finally:
        script_run_codex._cleanup_coordination_root(home.coordination_path)
        home.cleanup()

    assert not any(repo_path.glob(".codex-parity-deny-*"))
    assert index_path.read_text(encoding="utf-8") == "{}"


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("codex") is None, reason="Codex CLI is unavailable")
@pytest.mark.skipif(
    os.environ.get("RUN_CODEX_SANDBOX_INTEGRATION") != "1",
    reason="set RUN_CODEX_SANDBOX_INTEGRATION=1 to exercise the installed Codex sandbox",
)
def test_real_plain_profile_cannot_read_locked_index(script_run_codex: Any, tmp_path: Path) -> None:
    """A must share the locked target while the installed sandbox denies its index."""
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

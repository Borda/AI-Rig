"""The per-task, per-arm Codex execution engine and its telemetry writers."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from _bench_common.coordination_gate import (
    assert_coordination_root_idle as _assert_coordination_root_idle,
)
from _bench_common.process_group import NEW_PROCESS_GROUP, terminate_process_group
from _bench_common.provider_parity_contracts import (
    PARITY_TIMEOUT_SECONDS,
    EvaluationResult,
    canonical_result_rows,
    capability_strata,
    fresh_input_tokens,
    materialize_task_prompt,
    prompt_hash,
    token_accounting_inconsistent,
    treatment_adherence,
)

from _bench_codex import runtime
from _bench_codex.structural import diff_impact, provenance, provisioning
from _bench_codex.structural.arms import (
    _arm_contract_hash,
    _arm_envelope,
    _is_known_codex_arm,
    _raw_task,
    _raw_task_hash,
)
from _bench_codex.structural.config import (
    _CODEMAP_PERMISSION_PROFILE,
    _CODEX_BIN,
    _FROZEN_MARKETPLACE_NAME,
    _PROVENANCE_KEY,
    CODEX_STRUCTURAL_ARMS,
    PARITY_CODEX_REASONING_EFFORT,
    PARITY_MANIFEST_PATH,
    RUNNER_PATH,
)
from _bench_codex.structural.diff_impact import (
    DiffImpactStageAdmission,
    _capture_diff_impact_stage,
    _diff_impact_stage_evidence,
    build_codex_command,
)
from _bench_codex.structural.models import CodexRun
from _bench_codex.structural.provenance import _index_sha
from _bench_codex.structural.provisioning import (
    ArmHome,
    _aggregate_file_hashes,
    _benchmark_evidence_roots,
    _canonical_index_path,
    _invoke_plugin_command,
    _prepare_coordination_root,
    _RunAuthState,
)
from _bench_codex.structural.scoring import (
    _arm_compliance,
    _default_evaluator,
    _diff_impact_stager,
    _evaluator_identity,
    _locked_query_conformance,
    _locked_query_fitness,
    _normalize_locked_query,
)


class CodexRunner:
    """Run one canonical Codex cell with injectable process/evaluator seams."""

    def __init__(
        self,
        model: str,
        repo_path: Path,
        *,
        reasoning_effort: str = PARITY_CODEX_REASONING_EFFORT,
        index_path: Path | None = None,
        timeout: float = PARITY_TIMEOUT_SECONDS,
        marketplace_root: Path | None = None,
        codemap_bin: Path | None = None,
        manifest_path: Path = PARITY_MANIFEST_PATH,
        index_relocation: Mapping[str, str] | None = None,
        auth_source: Path | None = None,
        plugin_installer: Callable[[Path], bool | None] | None = None,
        plugin_probe: Callable[[Path], bool] | None = None,
        targeted: bool = False,
        command_runner: Callable[..., Any] | None = None,
        transport: Callable[..., str | bytes | Iterable[str | bytes]] | None = None,
        evaluator: Callable[[Mapping[str, Any], str], EvaluationResult] | None = None,
        evidence_roots: Iterable[Path] = (),
    ) -> None:
        if model not in runtime.SUPPORTED_CODEX_MODELS:
            raise ValueError(f"supported Codex benchmark model required: {', '.join(runtime.SUPPORTED_CODEX_MODELS)}")
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.repo_path = Path(repo_path).resolve()
        self.index_path = _canonical_index_path(Path(index_path)) if index_path else None
        self.timeout = timeout
        self.marketplace_root = marketplace_root.resolve() if marketplace_root else None
        self.codemap_bin = Path(codemap_bin) if codemap_bin else None
        self.manifest_path = Path(manifest_path)
        # A relocated index carries run-owned provenance.
        self.index_relocation = dict(index_relocation) if index_relocation is not None else None
        # Preserve this path so auth-copy rejects symlinks.
        self.auth_source = Path(auth_source) if auth_source else None
        self.plugin_installer = plugin_installer
        self.plugin_probe = plugin_probe
        self.targeted = targeted
        self.command_runner = command_runner
        self.transport = transport
        self.evaluator = evaluator or _default_evaluator
        supplied_evidence_roots = tuple(evidence_roots)
        self._evidence_roots = (
            tuple(Path(root).resolve(strict=False) for root in supplied_evidence_roots)
            if supplied_evidence_roots
            else _benchmark_evidence_roots()
        )
        self._auth_state: _RunAuthState | None = None
        self._auth_state_dir: Path | None = None
        self._runtime_snapshot_sources: dict[str, dict[str, Path]] = {}
        self._runtime_snapshot_marketplaces: dict[str, Path] = {}
        self._runtime_snapshot_hashes: dict[Path, str] = {}
        self._runtime_snapshot_modes: dict[Path, int] = {}
        self._runtime_evidence_path: Path | None = None
        # Record every coordination-root cleanup failure.
        self.coordination_cleanup_errors: list[str] = []

    @property
    def evidence_roots(self) -> tuple[Path, ...]:
        """Return denied roots."""
        return self._evidence_roots

    def _cleanup_coordination(self, coordination_path: Path | None) -> str | None:
        """Remove one coordination root, recording rather than discarding a failure.

        Never raises: every call site is inside a ``finally`` block, where raising would
        mask the exception that carries the real cause. The returned message lets a
        cell-scoped caller additionally promote the failure to contamination.
        """
        if coordination_path is None:
            return None
        try:
            provisioning._cleanup_coordination_root(coordination_path)
        except ValueError as exc:
            message = f"coordination cleanup failed for {coordination_path}: {exc}"
            self.coordination_cleanup_errors.append(message)
            return message
        return None

    def _bind_runtime_snapshot(
        self,
        snapshot_root: Path,
        arm_archives: Mapping[str, Mapping[str, Path]],
    ) -> None:
        """Bind later B/C cells to the exact package bytes archived for this run."""
        snapshot_root = Path(snapshot_root).resolve(strict=True)
        snapshot_path = snapshot_root / "input-snapshot.json"
        try:
            payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
            entries = payload["files"]
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("runtime input snapshot is unavailable or malformed") from exc
        if not isinstance(entries, list):
            raise ValueError("runtime input snapshot does not contain file identities")
        expected_hashes: dict[Path, str] = {}
        expected_modes: dict[Path, int] = {}
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise ValueError("runtime input snapshot contains malformed file identities")
            archived_path = entry.get("archived_path")
            sha256 = entry.get("sha256")
            mode = entry.get("mode")
            if (
                not isinstance(archived_path, str)
                or not isinstance(sha256, str)
                or not isinstance(mode, int)
                or mode not in {0o600, 0o700}
            ):
                raise ValueError("runtime input snapshot contains malformed file identities")
            relative_path = Path(archived_path)
            if relative_path.is_absolute():
                raise ValueError("runtime input snapshot contains an unsafe archived path")
            path = snapshot_root / relative_path
            if not path.resolve(strict=False).is_relative_to(snapshot_root):
                raise ValueError("runtime input snapshot contains an unsafe archived path")
            expected_hashes[path] = sha256
            expected_modes[path] = mode
        bound_sources: dict[str, dict[str, Path]] = {}
        bound_marketplaces: dict[str, Path] = {}
        for arm, archives in arm_archives.items():
            if arm not in {"B_auto", "C_strict"}:
                raise ValueError(f"runtime snapshot cannot bind unsupported arm {arm!r}")
            bound_sources[arm] = {}
            for role, source in archives.items():
                if role == "marketplace":
                    continue
                source_path = Path(source).resolve(strict=True)
                if not source_path.is_relative_to(snapshot_root):
                    raise ValueError("runtime snapshot source escaped the run-owned inputs directory")
                if not any(path.is_relative_to(source_path) for path in expected_hashes):
                    raise ValueError(f"runtime snapshot lacks identities for {arm}:{role}")
                bound_sources[arm][role] = source_path
            marketplace = archives.get("marketplace")
            if marketplace is None and arm == "C_strict":
                plugin_root = bound_sources[arm].get("codemap-py")
                if (
                    plugin_root is not None
                    and (plugin_root.parent / ".agents" / "plugins" / "marketplace.json").is_file()
                ):
                    marketplace = plugin_root.parent
            if marketplace is not None:
                if arm != "C_strict":
                    raise ValueError("only the C runtime snapshot can bind a frozen marketplace")
                marketplace_path = Path(marketplace).resolve(strict=True)
                if not marketplace_path.is_relative_to(snapshot_root):
                    raise ValueError("runtime marketplace escaped the run-owned inputs directory")
                self._validate_runtime_snapshot_tree(marketplace_path, expected_hashes, expected_modes)
                manifest_path = marketplace_path / ".agents" / "plugins" / "marketplace.json"
                expected_manifest = expected_hashes.get(manifest_path)
                if expected_manifest is None or expected_modes.get(manifest_path) != 0o600:
                    raise ValueError("runtime marketplace lacks a locked private manifest")
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise ValueError("runtime marketplace manifest is unavailable or malformed") from exc
                expected_plugins = [
                    {"name": "codemap-py", "source": {"source": "local", "path": "./codemap-py"}},
                    {"name": "codex-rig", "source": {"source": "local", "path": "./codex-rig"}},
                ]
                if not isinstance(manifest, Mapping) or manifest.get("name") != _FROZEN_MARKETPLACE_NAME:
                    raise ValueError("runtime marketplace name drifted")
                if manifest.get("plugins") != expected_plugins:
                    raise ValueError("runtime marketplace schema drifted")
                bound_marketplaces[arm] = marketplace_path
        self._runtime_snapshot_sources = bound_sources
        self._runtime_snapshot_marketplaces = bound_marketplaces
        self._runtime_snapshot_hashes = expected_hashes
        self._runtime_snapshot_modes = expected_modes
        self._runtime_evidence_path = snapshot_root.parent / "runtime-isolation.jsonl"
        runtime_history_root = snapshot_root.parent
        if runtime_history_root not in self._evidence_roots:
            self._evidence_roots = (*self._evidence_roots, runtime_history_root)

    def _validate_runtime_snapshot_tree(
        self,
        source: Path,
        expected_hashes: Mapping[Path, str] | None = None,
        expected_modes: Mapping[Path, int] | None = None,
    ) -> None:
        """Fail closed when a run-owned runtime tree no longer matches its input ledger."""
        source = Path(source).resolve(strict=True)
        expected_hashes = self._runtime_snapshot_hashes if expected_hashes is None else expected_hashes
        expected_modes = self._runtime_snapshot_modes if expected_modes is None else expected_modes
        expected = {path: sha256 for path, sha256 in expected_hashes.items() if path.is_relative_to(source)}
        if not expected:
            raise ValueError(f"runtime snapshot has no locked identities for {source}")
        observed: dict[Path, str] = {}
        for path in source.rglob("*"):
            if path.is_symlink():
                raise ValueError(f"runtime snapshot source contains symlink: {source}")
            if path.is_file():
                observed[path] = hashlib.sha256(path.read_bytes()).hexdigest()
        if observed != expected:
            expected_identity = _aggregate_file_hashes(
                {path.relative_to(source).as_posix(): sha256 for path, sha256 in expected.items()}
            )
            observed_identity = _aggregate_file_hashes(
                {path.relative_to(source).as_posix(): sha256 for path, sha256 in observed.items()}
            )
            raise ValueError(
                f"runtime snapshot byte drift for {source.name}: expected={expected_identity} observed={observed_identity}"
            )
        expected_modes = {path: mode for path, mode in expected_modes.items() if path.is_relative_to(source)}
        if os.name != "nt":
            observed_modes = {path: stat.S_IMODE(path.lstat().st_mode) for path in observed}
            if observed_modes != expected_modes:
                raise ValueError(f"runtime snapshot mode drift for {source.name}")

    def _runtime_plugin_sources(self, arm: str) -> dict[str, Path] | None:
        """Return validated C plugin sources when this run already owns a snapshot."""
        sources = self._runtime_snapshot_sources.get(arm)
        if sources is None:
            return None
        if set(sources) != {"codemap-py", "codex-rig"}:
            raise ValueError("C runtime snapshot lacks the locked plugin pair")
        marketplace = self._runtime_snapshot_marketplaces.get(arm)
        if marketplace is not None:
            self._validate_runtime_snapshot_tree(marketplace)
        for source in sources.values():
            self._validate_runtime_snapshot_tree(source)
        return dict(sources)

    def _runtime_direct_launcher(self, arm: str) -> Path | None:
        """Return the validated B launcher from this run's snapshot when available."""
        sources = self._runtime_snapshot_sources.get(arm)
        if sources is None:
            return self.codemap_bin
        direct_root = sources.get("direct-cli")
        if direct_root is None or set(sources) != {"direct-cli"}:
            raise ValueError("B runtime snapshot lacks the locked direct CLI")
        self._validate_runtime_snapshot_tree(direct_root)
        return direct_root / "bin" / "codemap-py"

    def _record_runtime_failure(
        self,
        arm: str,
        error: BaseException,
        *,
        home: ArmHome | None = None,
        source_paths: Iterable[Path] = (),
    ) -> None:
        """Persist non-secret expected and observed runtime identities before home cleanup."""
        if self._runtime_evidence_path is None:
            return
        expected, expected_plugins = self._expected_runtime_identities()
        payload = {
            "arm": arm,
            "error": str(error),
            "expected_artifact_sha256": expected,
            "expected_plugin_identities": expected_plugins,
            "observed_plugin_identities": self._observed_plugin_identities(home, source_paths),
            "status": "failed",
        }
        self._append_runtime_evidence(payload)

    def _record_runtime_success(self, arm: str, home: ArmHome) -> None:
        """Persist the verified plugin identities before the disposable home is removed."""
        if self._runtime_evidence_path is None:
            return
        expected, expected_plugins = self._expected_runtime_identities()
        observed = self._observed_plugin_identities(home, ())
        if observed != expected_plugins:
            raise ValueError("verified runtime plugin identities differ from the locked manifest")
        self._append_runtime_evidence(
            {
                "arm": arm,
                "error": None,
                "expected_artifact_sha256": expected,
                "expected_plugin_identities": expected_plugins,
                "observed_plugin_identities": observed,
                "status": "verified",
            }
        )

    def _observed_plugin_identities(
        self,
        home: ArmHome | None,
        source_paths: Iterable[Path],
    ) -> dict[str, dict[str, str]]:
        """Return public plugin identity fields from sources that still exist."""
        observed: dict[str, dict[str, str]] = {}
        for source in source_paths:
            manifest_path = Path(source) / ".codex-plugin" / "plugin.json"
            try:
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, TypeError, json.JSONDecodeError):
                continue
            if isinstance(payload, Mapping) and isinstance(payload.get("name"), str):
                observed[payload["name"]] = {
                    "version": str(payload.get("version", "")),
                    "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                }
        if home is not None:
            for name, path in (("codemap-py", home.codemap_plugin_path), ("codex-rig", home.codex_rig_path)):
                if path is not None:
                    manifest_path = path / ".codex-plugin" / "plugin.json"
                    try:
                        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                    except (OSError, TypeError, json.JSONDecodeError):
                        continue
                    observed[name] = {
                        "version": str(payload.get("version", "")) if isinstance(payload, Mapping) else "",
                        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                    }
        return observed

    def _expected_runtime_identities(self) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
        """Return locked public artifact and plugin identities from the manifest."""
        expected: Mapping[str, Any] = {}
        expected_plugins: dict[str, dict[str, str]] = {}
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            candidate = manifest.get("artifact_sha256", {})
            if isinstance(candidate, Mapping):
                expected = {str(name): str(value) for name, value in candidate.items()}
            for name, candidate_key, manifest_key in (
                ("codemap-py", "codemap_candidate", "codemap_candidate_manifest"),
                ("codex-rig", "codex_rig_candidate", "codex_rig_plugin_manifest"),
            ):
                version = manifest.get(candidate_key, {})
                expected_plugins[name] = {
                    "version": str(version.get("version", "")) if isinstance(version, Mapping) else "",
                    "manifest_sha256": expected.get(manifest_key, ""),
                }
        except (OSError, TypeError, json.JSONDecodeError):
            pass
        return dict(expected), expected_plugins

    def _append_runtime_evidence(self, payload: Mapping[str, Any]) -> None:
        """Append one private runtime-identity and evidence-boundary record."""
        if self._runtime_evidence_path is None:
            return
        record = {**payload, "evidence_roots": [str(root) for root in self._evidence_roots]}
        self._runtime_evidence_path.parent.mkdir(parents=True, exist_ok=True)
        with self._runtime_evidence_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        self._runtime_evidence_path.chmod(0o600)

    def _ensure_auth_state(self) -> _RunAuthState | None:
        """Lazily seed the private credential chain for this runner."""
        if self.auth_source is None:
            return None
        if self._auth_state is None:
            self._auth_state = _RunAuthState(self.auth_source)
            self._auth_state_dir = self._auth_state.directory
        return self._auth_state

    def close(self) -> None:
        """Remove the runner-owned private credential chain."""
        if self._auth_state is not None:
            self._auth_state.close()

    def build_command(self, prompt: str, *, working_directory: Path | None = None) -> list[str]:
        """Build this runner's canonical Codex command."""
        return build_codex_command(
            working_directory or self.repo_path,
            self.model,
            prompt,
            reasoning_effort=self.reasoning_effort,
        )

    # Declared stage surface (see module-level note): delegating replacements for
    # stage-module reach-ins. `run_stream` avoids the name `subprocess` so it
    # cannot be confused with the stdlib module.
    def prepare_verified_home(self, arm: str, **kwargs: Any) -> ArmHome:
        """Create and verify one arm home."""
        return self._prepare_verified_home(arm, **kwargs)

    def run_stream(self, command: list[str], env: Mapping[str, str], **kwargs: Any) -> str:
        """Run one Codex attempt and return its raw stream."""
        return self._subprocess(command, env, **kwargs)

    def _admit_runtime(self, arm: str, diff_impact_stage: DiffImpactStageAdmission | None = None) -> None:
        """Admit this run's target and index for one arm, carrying any relocation provenance.

        Every admission routes through here, so a relocated index is proven the same way at each; reaching the module-
        level check directly would demand byte identity a worktree cannot have.
        """
        diff_impact._validate_locked_runtime(
            self.repo_path,
            self.index_path,
            arm,
            self.manifest_path,
            diff_impact_stage,
            self.index_relocation,
        )

    def _prepare_verified_home(
        self,
        arm: str,
        *,
        diff_impact_stage: DiffImpactStageAdmission | None = None,
        writable_workspace: Path | None = None,
        denied_workspace: Path | None = None,
        index_relocation: Mapping[str, str] | None = None,
        historical_runtime_coordinate: Mapping[str, str] | None = None,
        fixture_runtime_coordinate: Mapping[str, Any] | None = None,
    ) -> ArmHome:
        """Create and verify one arm home without invoking a model.

        Historical Patch and copied-fixture coordinates are explicit opt-ins; other callers use the active manifest.
        """
        # Historical coordinates do not inherit the active-run relocation.
        run_relocation = None if historical_runtime_coordinate is not None else self.index_relocation
        diff_impact._validate_locked_runtime(
            self.repo_path,
            self.index_path,
            arm,
            self.manifest_path,
            diff_impact_stage,
            index_relocation if index_relocation is not None else run_relocation,
            historical_runtime_coordinate,
            fixture_runtime_coordinate,
        )
        auth_state = self._ensure_auth_state()
        if auth_state is not None:
            auth_state.assert_source_unchanged()
        runtime_plugin_sources: dict[str, Path] | None = None
        runtime_marketplace: Path | None = None
        bound_sources = self._runtime_snapshot_sources.get(arm, {})
        home: ArmHome | None = None
        try:
            if arm == "C_strict":
                runtime_plugin_sources = self._runtime_plugin_sources(arm)
                runtime_marketplace = self._runtime_snapshot_marketplaces.get(arm)
            runtime_codemap_bin = self._runtime_direct_launcher(arm) if arm == "B_auto" else self.codemap_bin
            home = provisioning.prepare_arm_home(
                arm,
                auth_source=None,
                codemap_bin=runtime_codemap_bin,
                plugin_installer=None if runtime_plugin_sources is not None else self.plugin_installer,
            )
            if auth_state is not None:
                auth_state.seed_home(home.path)
                home.auth_provisioned = True
            if arm != "A_plain" and self.index_path is not None:
                home.env["CODEMAP_PYTHON"] = provisioning._verify_locked_codemap_python(
                    manifest_path=self.manifest_path,
                    command_runner=self.command_runner,
                )
                home.env["SCAN_NO_AUTOBUILD"] = "1"
                home.env["CODEMAP_LOGGING"] = "false"
                home.env["CODEX_CODEMAP_AVAILABLE"] = "1"
                # Sole writable path of a measured cell: outside the workspace, discarded with this cell's home.
                gate = home.path.parent / f"{home.path.name}-gate"
                if gate.resolve().is_relative_to(self.repo_path.resolve()):
                    raise ValueError("Codemap coordination root must be outside the measured workspace")
                home.coordination_path = _prepare_coordination_root(self.index_path, gate)
                home.env["CODEMAP_COORDINATION_DIR"] = str(home.coordination_path)
            else:
                for variable in (
                    "CODEMAP_BIN",
                    "CODEMAP_COORDINATION_DIR",
                    "CODEMAP_INDEX",
                    "CODEMAP_INDEX_DIR",
                    "CODEMAP_PYTHON",
                    "CODEMAP_SKILL_FILE",
                    "SCAN_NO_AUTOBUILD",
                    "CODEMAP_LOGGING",
                ):
                    home.env.pop(variable, None)
            if arm == "C_strict":
                if runtime_plugin_sources is not None:
                    home.codemap_verified = provisioning._install_codemap_plugin(
                        home,
                        runtime_marketplace,
                        plugin_sources=runtime_plugin_sources,
                        command_runner=self.command_runner,
                    )
                elif self.plugin_probe is not None:
                    home.codemap_verified = bool(self.plugin_probe(home.path))
                elif not home.codemap_verified:
                    home.codemap_verified = provisioning._install_codemap_plugin(
                        home,
                        self.marketplace_root,
                        command_runner=self.command_runner,
                    )
            if arm != "A_plain":
                if not home.codemap_verified:
                    raise RuntimeError("Codemap delivery is not verified")
                home.codemap_available = True
                provisioning._verify_treatment_artifact_locks(home, self.manifest_path)
            if arm == "C_strict" and (
                home.codemap_skill_path is None
                or not home.codemap_skill_sha256
                or home.codex_rig_path is None
                or not home.codex_rig_manifest_sha256
            ):
                raise RuntimeError("installed Codemap skill and Codex Rig are not verified")
            if arm == "C_strict" and fixture_runtime_coordinate is None:
                if self.index_path is None:
                    raise ValueError("C_strict admission requires the locked index")
                provisioning._admit_installed_skill_pair(
                    home,
                    self.repo_path,
                    self.index_path,
                    manifest_path=self.manifest_path,
                    command_runner=self.command_runner,
                )
            provisioning._write_permission_config(
                home,
                arm,
                self.index_path,
                marketplace_root=self.marketplace_root,
                writable_workspace=writable_workspace,
                denied_workspace=denied_workspace,
                evidence_roots=self._evidence_roots,
            )
            if arm == "C_strict":
                provisioning._verify_installed_plugin_pair(home, command_runner=self.command_runner)
            provisioning._verify_permission_profile(
                home,
                self.repo_path,
                self.index_path,
                command_runner=self.command_runner,
                writable_workspace=writable_workspace,
            )
            if arm == "B_auto" and fixture_runtime_coordinate is None:
                if self.index_path is None:
                    raise ValueError("B_auto admission requires the locked index")
                provisioning._admit_staged_direct_cli(
                    home,
                    self.repo_path,
                    self.index_path,
                    manifest_path=self.manifest_path,
                    command_runner=self.command_runner,
                )
            if home.auth_provisioned:
                provisioning._verify_authentication(home, command_runner=self.command_runner)
                if auth_state is not None:
                    auth_state.refresh_from_home(home.path)
            if arm == "A_plain":
                provisioning._verify_plain_plugin_absent(home, command_runner=self.command_runner)
        except BaseException as exc:
            self._record_runtime_failure(
                arm,
                exc,
                home=home,
                source_paths=(runtime_plugin_sources or bound_sources).values(),
            )
            if home is not None:
                self._cleanup_coordination(home.coordination_path)
                home.cleanup()
            raise
        assert home is not None
        return home

    def preflight_expected_queries(self, tasks: Iterable[Mapping[str, Any]], arms: Iterable[str]) -> None:
        """Validate each unique locked query through B once before study setup."""
        if not {"B_auto", "C_strict"}.intersection(arms):
            return
        if self.index_path is None:
            raise RuntimeError("B_auto query preflight lacks a locked index")
        home = self._prepare_verified_home("B_auto")
        try:
            if home.codemap_launcher_path is None:
                raise RuntimeError("B_auto query preflight lacks a locked launcher")
            index_sha256 = hashlib.sha256(self.index_path.read_bytes()).hexdigest()
            profile = home.permission_profile or _CODEMAP_PERMISSION_PROFILE
            seen_queries: set[tuple[str, ...]] = set()
            for task in tasks:
                task_id = str(task.get("id", "unknown"))
                expected = task.get("expected_queries")
                if not isinstance(expected, list) or not expected:
                    raise RuntimeError(f"B_auto task {task_id} has no structured expected_queries")
                for query in expected:
                    if not isinstance(query, Mapping) or not isinstance(query.get("cmd"), str):
                        raise RuntimeError(f"B_auto task {task_id} has malformed expected query")
                    arguments = query.get("args", [])
                    if not isinstance(arguments, list) or not all(isinstance(value, str) for value in arguments):
                        raise RuntimeError(f"B_auto task {task_id} has malformed expected query args")
                    normalized = _normalize_locked_query(str(query["cmd"]), arguments)
                    if normalized is None:
                        raise RuntimeError(f"B_auto task {task_id} has malformed expected query")
                    if normalized in seen_queries:
                        continue
                    seen_queries.add(normalized)
                    command = [
                        _CODEX_BIN,
                        "sandbox",
                        "-P",
                        profile,
                        "--include-managed-config",
                        "-C",
                        str(self.repo_path),
                        "--",
                        str(home.codemap_launcher_path),
                        "query",
                        "--compact",
                        *normalized,
                    ]
                    code, stdout, stderr = _invoke_plugin_command(
                        command,
                        home.env,
                        self.command_runner,
                        cwd=self.repo_path,
                    )
                    if code != 0 or not runtime._canonical_query_output({"aggregated_output": stdout}):
                        detail = stderr.strip() or stdout.strip()
                        raise RuntimeError(f"B_auto expected query failed for {task_id}: {detail[:300]}")
                    if hashlib.sha256(self.index_path.read_bytes()).hexdigest() != index_sha256:
                        raise RuntimeError(f"B_auto expected query mutated the locked index for {task_id}")
        finally:
            try:
                self._cleanup_coordination(home.coordination_path)
            finally:
                home.cleanup()

    def create_input_snapshot(
        self,
        run_dir: Path,
        *,
        tasks_path: Path,
        manifest_path: Path,
        invocation_launcher_path: Path | None = None,
        tasks: Iterable[Mapping[str, Any]],
        arms: Iterable[str],
        runner_path: Path | None = None,
        additional_shared_files: Mapping[str, Path] | None = None,
    ) -> dict[str, Any]:
        """Archive launch inputs and verified B/C package bytes before paid calls."""
        snapshot_root = Path(run_dir) / "inputs"
        if snapshot_root.exists():
            raise FileExistsError(snapshot_root)
        # The first admission home is created before the immutable input snapshot
        # exists.  Reserve this non-secret evidence file now so an install failure
        # can persist expected/observed identities before the disposable home is
        # cleaned up.
        self._runtime_evidence_path = Path(run_dir) / "runtime-isolation.jsonl"
        self._runtime_evidence_path.parent.mkdir(parents=True, exist_ok=True)
        self._runtime_evidence_path.touch(exist_ok=False)
        self._runtime_evidence_path.chmod(0o600)
        homes: list[ArmHome] = []
        arm_archives: dict[str, dict[str, Path]] = {}
        arm_files: dict[str, dict[str, Path]] = {}
        try:
            for arm in arms:
                home = self._prepare_verified_home(arm)
                homes.append(home)
                arm_files[arm] = {"config.toml": home.path / "config.toml"}
                if arm == "B_auto":
                    arm_archives[arm] = {"direct-cli": home.path / "direct-cli"}
                elif arm == "C_strict":
                    if home.codemap_plugin_path is None or home.codex_rig_path is None:
                        raise RuntimeError("C_strict package roots are not verified")
                    arm_archives[arm] = {
                        "codemap-py": home.codemap_plugin_path,
                        "codex-rig": home.codex_rig_path,
                    }
                    if home.codemap_context_path is not None:
                        arm_files[arm]["codemap-context.json"] = home.codemap_context_path
            snapshot = provisioning._write_input_snapshot(
                snapshot_root,
                manifest_path=manifest_path,
                tasks_path=tasks_path,
                runner_path=runner_path or RUNNER_PATH,
                invocation_launcher_path=invocation_launcher_path,
                index_path=self.index_path,
                auth_source=self.auth_source,
                arm_archives=arm_archives,
                arm_files=arm_files,
                additional_shared_files=additional_shared_files,
            )
            if isinstance(snapshot.get("path"), str):
                runtime_archives = {
                    arm: {
                        **{role: snapshot_root / arm / role for role in archives},
                        **({"marketplace": snapshot_root / arm} if arm == "C_strict" else {}),
                    }
                    for arm, archives in arm_archives.items()
                }
                self._bind_runtime_snapshot(snapshot_root, runtime_archives)
            return snapshot
        finally:
            cleaned_coordination_paths: set[Path] = set()
            for home in homes:
                coordination_path = home.coordination_path
                if coordination_path is not None and coordination_path not in cleaned_coordination_paths:
                    cleaned_coordination_paths.add(coordination_path)
                    self._cleanup_coordination(coordination_path)
                home.cleanup()

    def probe_arm(
        self,
        arm: str,
        *,
        diff_impact_stage: DiffImpactStageAdmission | None = None,
    ) -> dict[str, Any]:
        """Return no-model runtime and plugin-isolation evidence for one arm."""
        if not _is_known_codex_arm(arm):
            raise ValueError(f"unknown benchmark arm {arm!r}")
        if diff_impact_stage is None:
            home = self._prepare_verified_home(arm)
        else:
            home = self._prepare_verified_home(arm, diff_impact_stage=diff_impact_stage)
        try:
            return provisioning.probe_arm_home(home)
        finally:
            try:
                self._cleanup_coordination(home.coordination_path)
            finally:
                home.cleanup()

    def preflight_diff_impact_stages(self, tasks: Iterable[Mapping[str, Any]], arms: Iterable[str]) -> None:
        """Exercise DI staging, exact admission, and strict restoration without a model."""
        selected_arms = tuple(arms)
        for task in tasks:
            stager = _diff_impact_stager(self.repo_path, task)
            if stager is None:
                continue
            self._admit_runtime("A_plain")
            entered = False
            try:
                stager.__enter__()
                entered = True
                admission = _capture_diff_impact_stage(self.repo_path, task)
                for arm in selected_arms:
                    home = self._prepare_verified_home(arm, diff_impact_stage=admission)
                    try:
                        self._admit_runtime(arm, admission)
                    finally:
                        try:
                            self._cleanup_coordination(home.coordination_path)
                        finally:
                            home.cleanup()
            finally:
                try:
                    if entered:
                        stager.__exit__(*sys.exc_info())
                finally:
                    self._admit_runtime("A_plain")

    def run(
        self,
        task: Mapping[str, Any],
        arm: str,
        *,
        repetition: int = 1,
        diff_impact_stage: DiffImpactStageAdmission | None = None,
    ) -> CodexRun:
        """Execute one task cell within a retry-inclusive coordinate deadline."""
        if not _is_known_codex_arm(arm):
            raise ValueError(f"unknown benchmark arm {arm!r}")
        if repetition < 1:
            raise ValueError("repetition must be a positive integer")
        task_id = task.get("id")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("task requires a non-empty id")
        is_diff_impact = task.get("type") == "diff_impact"
        if is_diff_impact and diff_impact_stage is None:
            raise ValueError("canonical Codex DI run requires an admitted staged worktree")
        if not is_diff_impact and diff_impact_stage is not None:
            raise ValueError("canonical Codex non-DI run cannot admit a staged worktree")
        prompt = materialize_task_prompt(_raw_task(task))
        envelope = _arm_envelope(arm)
        command_prompt = envelope + "\n\n" + prompt
        metadata = task.get(_PROVENANCE_KEY, {})
        if not isinstance(metadata, Mapping):
            metadata = {}
        run = CodexRun(
            arm,
            task_id,
            str(task.get("type", "unknown")),
            self.model,
            reasoning_effort=self.reasoning_effort,
            repetition=repetition,
            targeted=self.targeted,
            study_mode="targeted" if self.targeted else "confirmatory",
        )
        run.parity_arm = arm
        run.cell_wall_clock_limit_s = self.timeout
        if diff_impact_stage is not None:
            run.stage_evidence = _diff_impact_stage_evidence(self.repo_path, task, diff_impact_stage)
        run.capability_strata = capability_strata(_raw_task(task))
        run.arm_contract_hash = _arm_contract_hash(arm)
        raw_hash = _raw_task_hash(task)
        expected_hash = metadata.get("task_hash", task.get("task_hash"))
        raw_prompt_hash = prompt_hash(_raw_task(task))
        expected_prompt_hash = metadata.get("prompt_hash", task.get("prompt_hash"))
        if metadata and metadata.get("task_hash") != raw_hash:
            raise ValueError(f"task hash mismatch for {task_id!r}")
        if metadata and metadata.get("prompt_hash") != raw_prompt_hash:
            raise ValueError(f"prompt hash mismatch for {task_id!r}")
        # ``load_tasks_with_provenance`` already verifies these values against
        # the locked manifest.  Trusting the precomputed fields here avoids
        # hashing an enriched projection (which is not canonical task bytes).
        run.task_hash = str(expected_hash or raw_hash)
        run.prompt_hash = str(expected_prompt_hash or raw_prompt_hash)
        run.suite_hash = str(metadata.get("suite_hash", task.get("suite_hash", "")))
        run.suite_raw_hash = str(metadata.get("suite_raw_hash", task.get("suite_raw_hash", "")))
        run.experiment_revision = str(metadata.get("experiment_revision", task.get("experiment_revision", "")))
        run.oracle_class = str(metadata.get("oracle_class", task.get("oracle_class", "unknown")))
        run.headline_eligible_v1 = bool(metadata.get("headline_eligible_v1", task.get("headline_eligible_v1", False)))
        run.repo_sha = provenance._repo_sha(self.repo_path)
        run.index_sha = _index_sha(self.index_path)
        explicit_evaluator_id = metadata.get("evaluator_id", task.get("evaluator_id"))
        explicit_evaluator_hash = metadata.get("evaluator_hash", task.get("evaluator_hash"))
        if explicit_evaluator_id and explicit_evaluator_hash:
            run.evaluator_id = str(explicit_evaluator_id)
            run.evaluator_hash = str(explicit_evaluator_hash)
        else:
            run.evaluator_id, run.evaluator_hash = _evaluator_identity(_raw_task(task), self.evaluator)
        run.envelope_hash = hashlib.sha256(envelope.encode()).hexdigest()
        run.scoreable = metadata.get("scoreable", task.get("scoreable", True)) is not False
        home: ArmHome | None = None
        if diff_impact_stage is not None:
            self._admit_runtime(arm, diff_impact_stage)
        if self.transport is None:
            if diff_impact_stage is None:
                home = self._prepare_verified_home(arm)
            else:
                home = self._prepare_verified_home(arm, diff_impact_stage=diff_impact_stage)
        started_at = time.monotonic()
        coordinate_deadline = started_at + self.timeout
        attempt_events: list[list[dict[str, Any]]] = []
        parsed = runtime.CodexParseResult()
        postflight_error = ""
        auth_state_error = ""
        command = self.build_command(command_prompt)
        try:
            for attempt in range(3):
                remaining_s = coordinate_deadline - time.monotonic()
                if remaining_s <= 0:
                    parsed = runtime.CodexParseResult(
                        incomplete=True,
                        error=f"cell wall-clock budget exhausted ({self.timeout}s total)",
                        error_type="cell_timeout",
                    )
                    break
                run.retry_count = attempt
                if self.transport is None:
                    assert home is not None
                    stream = self._subprocess(command, home.env, timeout=remaining_s)
                else:
                    stream = self.transport(command, arm=arm)
                parsed = runtime.parse_codex_jsonl(
                    stream,
                    launcher_path=home.codemap_launcher_path if home is not None else None,
                    skill_path=home.codemap_skill_path if home is not None else None,
                    skill_sha256=home.codemap_skill_sha256 if home is not None else "",
                )
                attempt_events.append(parsed.raw_events)
                if home is not None or diff_impact_stage is not None:
                    try:
                        self._admit_runtime(arm, diff_impact_stage)
                        if home is not None and home.coordination_path is not None:
                            # Liveness and path safety only; gate scratch is permitted output.
                            _assert_coordination_root_idle(home.coordination_path)
                    except ValueError as exc:
                        postflight_error = str(exc)
                        if diff_impact_stage is not None:
                            run.stage_evidence = _diff_impact_stage_evidence(self.repo_path, task, diff_impact_stage)
                        parsed.completed = False
                        parsed.incomplete = True
                        parsed.error = f"runtime contamination: {postflight_error}"
                        parsed.error_type = "runtime_contamination"
                        parsed.retryable = False
                        break
                zero_token_transport_failure = (
                    parsed.input_tokens == 0
                    and parsed.output_tokens == 0
                    and not parsed.output_text.strip()
                    and parsed.retryable
                )
                if not zero_token_transport_failure or attempt == 2:
                    break
        finally:
            run.elapsed_s = time.monotonic() - started_at
            if home is not None:
                if self._auth_state is not None and home.auth_provisioned:
                    try:
                        self._auth_state.refresh_from_home(home.path)
                    except (RuntimeError, ValueError) as exc:
                        auth_state_error = str(exc)
                # Cell-scoped escalation on top of the shared recording policy: a leak
                # here contaminates this cell's measurement, not just the run's hygiene.
                cleanup_error = self._cleanup_coordination(home.coordination_path)
                postflight_error = postflight_error or cleanup_error
                home.cleanup()
        run.thread_id = parsed.thread_id
        run.output_text = parsed.output_text
        run.raw_events = parsed.raw_events
        run.native_attempt_events = attempt_events
        run.native_item_counts = parsed.item_counts
        run.tool_elapsed_s = parsed.tool_elapsed_s
        run.tool_result_tokens = parsed.tool_result_tokens
        for field_name in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
            "command_calls",
            "codemap_observed_calls",
            "codemap_calls",
            "codemap_successful_calls",
            "codemap_compact_successful_calls",
            "codemap_direct_calls",
            "codemap_direct_successful_calls",
            "codemap_direct_compact_successful_calls",
            "codemap_skill_calls",
            "codemap_skill_successful_calls",
            "codemap_skill_compact_successful_calls",
            "skill_delivery_observed",
            "codemap_errors",
            "fallback_calls",
            "malformed_usage",
            "successful_query_arguments",
        ):
            setattr(run, field_name, getattr(parsed, field_name))
        run.success = parsed.success
        run.incomplete = parsed.incomplete
        run.error = parsed.error
        run.error_type = parsed.error_type
        run.compliance = _arm_compliance(arm, run)
        run.locked_query_conformance = _locked_query_conformance(task, arm, run)
        locked_query_fitness = _locked_query_fitness(task, arm, run)
        if locked_query_fitness is not None:
            run.locked_query_fitness = locked_query_fitness.overall
            run.locked_query_endpoint_fitness = locked_query_fitness.endpoint
            run.locked_query_target_fitness = locked_query_fitness.target
            run.locked_query_option_fitness = locked_query_fitness.options
        if arm == "B_auto" and run.compliance:
            run.codemap_delivery = "direct_cli"
        elif arm == "C_strict" and run.compliance:
            run.codemap_delivery = "installed_skill"
        run.contaminated = bool(postflight_error) or (arm == "A_plain" and run.codemap_observed_calls > 0)
        run.treatment_adherence = treatment_adherence(
            arm,
            codemap_use_compliance=run.compliance,
            contaminated=run.contaminated,
        )
        run.token_accounting_inconsistent = token_accounting_inconsistent(run.input_tokens, run.cached_input_tokens)
        run.fresh_input_tokens = fresh_input_tokens(run.input_tokens, run.cached_input_tokens)
        if postflight_error:
            run.incomplete = True
            run.error = f"runtime contamination: {postflight_error}"
            run.error_type = "runtime_contamination"
            run.success = False
        if auth_state_error:
            run.incomplete = True
            run.error = "run auth state could not be refreshed"
            run.error_type = "authentication_state_failed"
            run.success = False
        if run.contaminated and not run.error:
            run.error = "contaminated"
            run.success = False
        if run.scoreable and not run.incomplete:
            evaluation = self.evaluator(_raw_task(task), run.output_text)
            run.quality_score = evaluation.quality_score
            run.quality_components = evaluation.components
            run.correct = evaluation.correct
            run.extraction_failed = evaluation.extraction_failed
        return run

    def _subprocess(
        self,
        command: list[str],
        env: Mapping[str, str],
        *,
        timeout: float | None = None,
        working_directory: Path | None = None,
    ) -> str:
        """Run one Codex attempt within the coordinate's remaining budget.

        The child runs in its own process group so a timeout kills the descendants too. Killing only the direct child
        left grandchildren alive, still consuming paid budget outside the measured window.
        """
        attempt_timeout = self.timeout if timeout is None else timeout
        try:
            process = subprocess.Popen(
                command,
                cwd=working_directory or self.repo_path,
                env=dict(env),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                **NEW_PROCESS_GROUP,
            )
        except OSError as exc:
            return json.dumps(
                {
                    "type": "error",
                    "error": f"Codex launch failed: {exc.strerror or type(exc).__name__}",
                    "error_type": "launch_os_error",
                }
            )
        try:
            stdout, stderr = process.communicate(timeout=attempt_timeout)
        except subprocess.TimeoutExpired:
            terminate_process_group(process)
            # Whatever the agent streamed before the kill is real evidence: the usage
            # events it already emitted are what it already billed. Discarding them
            # persisted a timed-out cell as 0 tokens despite genuine spend.
            stdout, stderr = process.communicate()
            terminal = json.dumps(
                {
                    "type": "error",
                    "error": f"timeout ({attempt_timeout}s)",
                    "error_type": "timeout",
                }
            )
            return ((stdout or "") + "\n" + terminal).lstrip()
        if process.returncode != 0:
            terminal = json.dumps(
                {
                    "type": "error",
                    "error": (stderr or "").strip()[:300] or f"non-zero exit {process.returncode}",
                    "error_type": "non_zero_exit",
                }
            )
            return ((stdout or "") + "\n" + terminal).lstrip()
        return stdout or ""


def _append_run(output_path: Path, run: CodexRun, *, execution_index: int) -> None:
    """Append one completed cell so later failures cannot erase smoke evidence."""
    if execution_index < 0:
        raise ValueError("execution_index must be non-negative")
    run.execution_index = execution_index
    with output_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(run), sort_keys=True) + "\n")


def _canonical_telemetry_path(output_path: Path) -> Path:
    """Return the derived canonical-order sidecar path for raw telemetry."""
    return output_path.with_name("telemetry-canonical.jsonl")


def _write_canonical_telemetry(
    output_path: Path,
    canonical_path: Path,
    *,
    task_order: tuple[str, ...],
) -> str:
    """Atomically publish a canonical sidecar without rewriting raw execution evidence."""
    rows = [json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines() if line]
    canonical_rows = canonical_result_rows(
        rows,
        task_order=task_order,
        arm_order=CODEX_STRUCTURAL_ARMS,
    )
    serialized = "".join(json.dumps(row, sort_keys=True) + "\n" for row in canonical_rows).encode("utf-8")
    with tempfile.NamedTemporaryFile(
        dir=canonical_path.parent, prefix=f".{canonical_path.name}.", delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(serialized)
    try:
        os.replace(temporary, canonical_path)
    finally:
        temporary.unlink(missing_ok=True)
    return hashlib.sha256(serialized).hexdigest()


def _utc_now() -> str:
    """Return one stable UTC timestamp for run-level evidence."""
    return datetime.now(timezone.utc).isoformat()


def _write_run_metadata(metadata_path: Path, payload: Mapping[str, Any]) -> None:
    """Atomically persist run provenance so interruptions retain the last completed cell."""
    serialized = (json.dumps(dict(payload), indent=2, sort_keys=True) + "\n").encode("utf-8")
    with tempfile.NamedTemporaryFile(
        dir=metadata_path.parent, prefix=f".{metadata_path.name}.", delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(serialized)
    try:
        os.replace(temporary, metadata_path)
    finally:
        temporary.unlink(missing_ok=True)


def _close_runner(runner: Any) -> None:
    """Close optional private runner state without constraining fixture runners."""
    close = getattr(runner, "close", None)
    if callable(close):
        close()

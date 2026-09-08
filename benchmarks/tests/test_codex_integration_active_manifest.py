"""Regression checks for the Codex plain/CLI/Skill integration relock."""

from __future__ import annotations

import hashlib
import json
import re
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
BENCHMARKS = ROOT / "benchmarks"
GENERATOR = BENCHMARKS / "build-codex-integration-manifest.py"
SOURCE_MANIFEST = BENCHMARKS / "manifests" / "provider-parity-methodology.json"
MANIFEST = BENCHMARKS / "manifests" / "codex-integration.json"
HUMAN_MANIFEST = BENCHMARKS / "manifests" / "codex-integration.md"
SHORTHAND = re.compile(r"(?<![A-Za-z0-9_-])r[0-9]+(?![A-Za-z0-9_-])")


def _sha256(path: Path) -> str:
    """Hash exact relock artifact bytes without text or newline normalization.

    >>> from tempfile import TemporaryDirectory
    >>> with TemporaryDirectory() as directory:
    ...     path = Path(directory) / "artifact"
    ...     _ = path.write_bytes(b"abc")
    ...     _sha256(path)
    'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path) -> dict[str, object]:
    """Read a UTF-8 benchmark manifest and assert that its root is a JSON object.

    >>> from tempfile import TemporaryDirectory
    >>> with TemporaryDirectory() as directory:
    ...     path = Path(directory) / "manifest.json"
    ...     _ = path.write_text('{"schema": 1}', encoding="utf-8")
    ...     _load(path)
    {'schema': 1}
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _run_generator(*args: str) -> subprocess.CompletedProcess[str]:
    """Execute the relock utility without invoking a model or authentication."""
    return subprocess.run(
        [sys.executable, str(GENERATOR), *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_builder_locks_optional_query_arguments_ordering_and_cache_policy() -> None:
    """Builder output must replace inherited query, ordering, and token-reporting drift."""
    builder = runpy.run_path(str(GENERATOR))
    source = _load(SOURCE_MANIFEST)
    arm_order = (
        "deterministic six-permutation counterbalancing by frozen structural task ordinal; "
        "across the 55-task single-repetition execution suite, every arm occupies every ordinal 18 or 19 times"
    )
    token_prompt_cache_policy = (
        "Console and primary efficiency reports use gross provider input tokens only. "
        "Cached and fresh input counts are retained as raw telemetry diagnostics. "
        "The Codex CLI exposes no supported per-cell provider prompt-cache reset or disable control. "
        "Deterministic arm-order counterbalancing mitigates order exposure without claiming cache elimination."
    )

    arms = builder["_arms"]()
    controls = builder["_execution_controls"](source)
    cells = builder["_preregistered_cells"](source)
    task_selection = builder["_task_selection_contract"](source)
    telemetry = builder["_telemetry_admission"]()
    assert builder["EXPERIMENT_REVISION"] == "codex-integration-unified-task-cli-2026-08-11"
    assert arms["B_auto"]["requirement"] == (
        "Run at least one successful compact direct query in its own native command item containing exactly "
        '"$CODEMAP_BIN" query --compact <subcommand> [arguments]; '
        "additional reads and shell work are allowed as separate items."
    )
    assert telemetry["query"]["accepted_form"] == '"$CODEMAP_BIN" query --compact <subcommand> [arguments]'
    assert controls["arm_order"] == arm_order
    assert cells["arm_order"] == arm_order
    assert controls["token_prompt_cache_policy"] == token_prompt_cache_policy
    assert task_selection["coordinate_timeout_seconds"] == 600
    assert task_selection["nonpoolable"] is True
    assert task_selection["default_total_cells"] == 219
    assert task_selection["stages"]["structural"]["allowed_task_ids"] == cells["structural_execution_task_ids"]
    assert task_selection["stages"]["structural"]["default_repetitions"] == 1
    assert task_selection["stages"]["structural"]["selected_repetitions"] == 3
    assert all(
        task_selection["stages"][stage]["default_repetitions"] == 1
        for stage in ("readcrop", "fix-single", "fix-multi", "patch")
    )
    assert all(
        task_selection["stages"][stage]["selected_repetitions"] == 1
        for stage in ("readcrop", "fix-single", "fix-multi", "patch")
    )
    assert "repetitions" not in task_selection
    assert "study_mode" not in task_selection
    assert task_selection["resolution_policy"]["exact_id_first"] is True
    assert task_selection["resolution_policy"]["deduplicate"].startswith("selector tokens")
    assert task_selection["scope_digest"]["runtime_derived"] is True
    assert task_selection["scope_digest"]["stored_in_manifest"] is False
    assert task_selection["aggregate_execution_scope"]["approval_option"] == "--paid-approval"
    assert task_selection["aggregate_execution_scope"]["dry_run_marker"] == "SCOPE"


def test_generator_is_current_and_never_rewrites_methodology_source() -> None:
    """The integration relock must be deterministic and preserve methodology."""
    before = _sha256(SOURCE_MANIFEST)
    result = _run_generator("--check")

    assert result.returncode == 0, result.stderr
    assert _sha256(SOURCE_MANIFEST) == before


def test_package_freeze_reuses_live_mode_map_inside_gitless_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Paid source snapshots preserve the live package payload and executable modes."""
    generator = runpy.run_path(str(GENERATOR))
    freeze_package = generator["_codemap_package_manifest_sha256"]
    live_sha256 = freeze_package()
    snapshot_root = tmp_path / "source"
    snapshot_plugin = snapshot_root / "plugins" / "codemap-py"
    shutil.copytree(ROOT / "plugins" / "codemap-py", snapshot_plugin)
    mode_map_path = snapshot_root / "benchmarks" / "manifests" / "codemap-package-mode-map.json"
    mode_map_path.parent.mkdir(parents=True)
    package_builder = runpy.run_path(str(ROOT / "plugins" / "codemap-py" / "scripts" / "build_package.py"))
    live_mode_map = package_builder["_git_exec_modes"](ROOT / "plugins" / "codemap-py")
    mode_map_path.write_text(json.dumps(live_mode_map, sort_keys=True), encoding="utf-8")
    freeze_package.__globals__["ROOT"] = snapshot_root
    monkeypatch.setenv("CODEX_LAUNCHER_SNAPSHOT_ACTIVE", "1")

    assert freeze_package() == live_sha256


def test_generated_manifest_uses_environment_neutral_cli_and_posix_paths() -> None:
    """Generated records cannot contain host paths or Windows separators."""
    manifest = _load(MANIFEST)
    assert manifest["codex_cli"] == {
        "available": True,
        "path": "<codex-cli>",
        "reviewed_version": "codex-cli 0.146.1",
    }
    assert manifest["source_manifest"]["path"] == "benchmarks/manifests/provider-parity-methodology.json"
    assert "\\" not in manifest["source_manifest"]["path"]


def test_cli_identity_is_host_neutral_reviewed_provenance() -> None:
    """The manifest records the reviewed identity without imposing an admission pin."""
    builder = runpy.run_path(str(GENERATOR))
    assert builder["REVIEWED_CODEX_CLI_VERSION"] == "codex-cli 0.146.1"
    assert builder["_codex_cli_identity"]() == {
        "available": True,
        "path": "<codex-cli>",
        "reviewed_version": "codex-cli 0.146.1",
    }


def test_repository_marketplace_remains_the_installable_source_root() -> None:
    """The live marketplace must not be overwritten by a frozen snapshot manifest."""
    marketplace = _load(ROOT / ".agents" / "plugins" / "marketplace.json")
    sources = {entry["name"]: entry["source"] for entry in marketplace["plugins"]}

    assert marketplace["name"] == "borda-ai-rig"
    assert sources == {
        "bridge": {"path": "./plugins/bridge_cc-codex", "source": "local"},
        "codemap-py": {"path": "./plugins/codemap-py", "source": "local"},
        "codex-rig": {"path": "./plugins/codex-rig", "source": "local"},
    }


def test_generator_stale_error_names_exact_rebuild_command(tmp_path: Path) -> None:
    """Internal check mode must identify the command that repairs generated drift."""
    generator = runpy.run_path(str(GENERATOR))

    try:
        generator["_write_or_check"](tmp_path / "stale.json", b"expected\n", True)
    except ValueError as exc:
        assert str(exc).endswith("run: uv run python benchmarks/build-codex-integration-manifest.py")
    else:
        raise AssertionError("stale integration output was accepted")


def test_integration_manifest_reuses_every_canonical_suite_identity() -> None:
    """Task, suite, prompt, oracle, evaluator, target, and index identities cannot drift."""
    source = _load(SOURCE_MANIFEST)
    manifest = _load(MANIFEST)

    assert manifest["source_manifest"] == {
        "path": SOURCE_MANIFEST.relative_to(ROOT).as_posix(),
        "sha256": _sha256(SOURCE_MANIFEST),
    }
    for key in (
        "evaluation_contract",
        "headline_structural_v1",
        "index",
        "oracle_remediation",
        "suite_integrity",
        "suites",
        "target_source",
        "validation",
    ):
        assert manifest[key] == source[key]


def test_integration_manifest_locks_plain_cli_and_skill_arms_and_artifacts() -> None:
    """Only the three planned Codex arms and their exact package bytes are eligible."""
    manifest = _load(MANIFEST)

    assert list(manifest["arms"]) == ["A_plain", "B_auto", "C_strict"]
    assert manifest["model"] == {
        "name": "gpt-5.6-luna",
        "additional_strata": ["gpt-5.6-terra", "gpt-5.6-sol"],
        "reasoning_effort": "high",
        "strict_config": True,
    }
    assert manifest["estimands"] == {
        "C_strict-A_plain": "product effect",
        "B_auto-A_plain": "direct CLI availability effect",
        "C_strict-B_auto": "integration effect",
    }
    assert manifest["package_roster"] == ["codemap-py", "codex-rig"]
    assert manifest["experiment_id"] == "codex-integration-v1"
    assert manifest["schema_version"] == "codex-integration-manifest-v3"
    assert manifest["experiment_revision"] == "codex-integration-unified-task-cli-2026-08-11"
    assert manifest["preregistered_cells"]["arms"] == ["A_plain", "B_auto", "C_strict"]
    assert manifest["preregistered_cells"]["providers"] == ["codex"]
    assert manifest["preregistered_cells"]["confirmatory_repetitions"] == 1
    assert manifest["preregistered_cells"]["arm_order"] == (
        "deterministic six-permutation counterbalancing by frozen structural task ordinal; "
        "across the 55-task single-repetition execution suite, every arm occupies every ordinal 18 or 19 times"
    )
    assert len(manifest["preregistered_cells"]["structural_confirmatory_task_ids"]) == 45
    assert len(manifest["preregistered_cells"]["structural_execution_task_ids"]) == 55
    assert len(manifest["preregistered_cells"]["structural_diagnostic_task_ids"]) == 10
    assert set(manifest["preregistered_cells"]["structural_execution_task_ids"]) == (
        set(manifest["preregistered_cells"]["structural_confirmatory_task_ids"])
        | set(manifest["preregistered_cells"]["structural_diagnostic_task_ids"])
    )
    assert not set(manifest["preregistered_cells"]["structural_confirmatory_task_ids"]) & set(
        manifest["preregistered_cells"]["structural_diagnostic_task_ids"]
    )
    assert manifest["codex_permission_profiles"]["treatment"]["arms"] == ["B_auto", "C_strict"]
    assert manifest["codex_permission_profiles"]["treatment_runtime"]["scope"] == ["B_auto", "C_strict"]
    assert manifest["codex_permission_profiles"]["host_tooling_isolation"] == {
        "access": "deny",
        "arms": ["A_plain", "B_auto", "C_strict"],
        "roots": ["<host-home>/.agents", "<host-home>/.claude", "<host-home>/.codex"],
        "verification": "no-model directory enumeration must fail without emitting entries",
    }
    assert manifest["codex_permission_profiles"]["marketplace_source_access"] == {
        "A_plain": "deny",
        "B_auto": "deny after the locked direct runtime is staged inside its disposable home",
        "C_strict": "deny",
    }
    assert manifest["execution_controls"]["parity_timeout_seconds"] == 600
    assert manifest["execution_controls"]["coordinate_timeout_scope"] == (
        "one total 600-second budget shared by the initial attempt and at most two eligible retries"
    )
    assert "complete_run_wall_clock" not in manifest["execution_controls"]
    assert "confirmatory_max_wall_clock_seconds" not in manifest["execution_controls"]
    assert manifest["execution_controls"]["arm_order"] == (
        "deterministic six-permutation counterbalancing by frozen structural task ordinal; "
        "across the 55-task single-repetition execution suite, every arm occupies every ordinal 18 or 19 times"
    )
    assert manifest["execution_controls"]["token_prompt_cache_policy"] == (
        "Console and primary efficiency reports use gross provider input tokens only. "
        "Cached and fresh input counts are retained as raw telemetry diagnostics. "
        "The Codex CLI exposes no supported per-cell provider prompt-cache reset or disable control. "
        "Deterministic arm-order counterbalancing mitigates order exposure without claiming cache elimination."
    )
    assert manifest["codex_rig_integration_admission"]["required_before_paid_smoke"] == [
        "C installs exactly the locked codemap-py then codex-rig package roster.",
        "The Codex Rig adapter resolves the public CODEMAP_BIN launcher before PATH.",
        "One persisted compact-query context is available without Codemap cache or source reads.",
    ]
    assert re.fullmatch(r"[0-9a-f]{64}", manifest["codemap_candidate"]["package_manifest_sha256"])
    assert set(manifest["artifact_sha256"]) == {
        "codemap_candidate_manifest",
        "codemap_query_skill",
        "codemap_runtime_cli",
        "codemap_runtime_entrypoint",
        "codemap_runtime_graph",
        "codemap_runtime_integration",
        "codemap_runtime_query",
        "codex_rig_adapter",
        "codex_rig_contract",
        "codex_rig_integration_host",
        "codex_rig_package_manifest",
        "codex_rig_plugin_manifest",
        "codex_stage_fix",
        "codex_stage_readcrop",
        "codex_stage_runtime",
        "paid_lifecycle",
        "presentation",
        "prepare_codex_index",
        "run_all",
        "run_codex_structural",
    }
    assert manifest["implementation_contract"]["artifact_sha256"] == manifest["artifact_sha256"]
    direct_runtime = manifest["direct_cli_runtime"]
    assert direct_runtime["staged_root"] == "<disposable-CODEX_HOME>/direct-cli"
    assert set(direct_runtime["files"]) >= {
        "bin/codemap-py",
        "bin/_exclusions.py",
        "scripts/codemap_py_entry.py",
        "src/codemap_py/query.py",
    }
    assert re.fullmatch(r"[0-9a-f]{64}", direct_runtime["aggregate_sha256"])
    assert manifest["direct_cli_admission"] == {
        "probe_subcommand": "fn-rdeps",
        "probe_target": "lightning.pytorch.trainer.call::_call_lightning_module_hook",
        "required_before_paid_execution": (
            "Execute this compact query through B's staged launcher under the treatment permission profile; "
            "require exit 0, index.query_complete=true, and unchanged locked-index bytes."
        ),
    }
    validation = manifest["no_model_validation"]
    assert validation["model_or_auth_used"] is False
    assert validation["status"] == "runtime_smoke_required_before_paid_execution"
    assert set(validation["checks"].values()) == {"required"}
    assert len(validation["evidence"]) == 2
    telemetry = manifest["telemetry_admission"]
    assert telemetry["telemetry_contract_id"] == "installed-skill-binding-locked-query-components-v3"
    assert telemetry["skill_binding"] == {
        "arm": "C_strict",
        "environment_binding": "runner-owned immutable exact installed query Skill path; absent from A and B",
        "manual_read_telemetry": "skill_delivery_observed is diagnostic-only and never a query-credit prerequisite",
    }
    assert telemetry["query"] == {
        "accepted_form": '"$CODEMAP_BIN" query --compact <subcommand> [arguments]',
        "item_scope": "dedicated native command item",
        "required_exit_code": 0,
        "required_output": "one JSON document with index.query_complete=true and index.compact=true",
    }
    assert telemetry["treatment_attribution"] == {
        "B_auto": "direct CLI reachable; the model decides whether to query",
        "C_strict": "at least one successful canonical compact query in the immutable installed-Skill treatment",
    }
    assert all("Skill read" not in item for item in telemetry["rejected_evidence"])


def test_integration_manifest_locks_unified_nonpoolable_task_selection() -> None:
    """Unified selections preserve stage order, native repetitions, and aggregate approval."""
    manifest = _load(MANIFEST)
    scope = manifest["task_selection"]

    stage_order = ["structural", "readcrop", "fix-single", "fix-multi", "patch"]
    expected_stage_sizes = {"structural": 55, "readcrop": 6, "fix-single": 4, "fix-multi": 3, "patch": 5}
    flattened_ids = [task_id for stage in stage_order for task_id in scope["stages"][stage]["allowed_task_ids"]]
    assert scope["selector_option"] == "--tasks"
    assert scope["separator"] == ","
    assert scope["stage_order"] == stage_order
    assert {stage: len(scope["stages"][stage]["allowed_task_ids"]) for stage in stage_order} == expected_stage_sizes
    assert scope["allowed_task_ids"] == flattened_ids
    assert len(scope["allowed_task_ids"]) == 73
    assert scope["default_total_cells"] == 219
    assert scope["allowed_families"] == list(dict.fromkeys(task_id.split("-", 1)[0] for task_id in flattened_ids))
    assert scope["resolution_policy"] == {
        "exact_id_first": True,
        "family_match": "a family token selects every matching allowed ID",
        "order": "resolved IDs preserve structural, ReadCrop, Fix-Single, Fix-Multi, then Patch stage order",
        "deduplicate": "selector tokens and overlapping expanded IDs are evaluated once",
        "reject": ["empty tokens", "unknown task IDs", "unknown families"],
    }
    assert scope["stages"]["structural"]["arms"] == ["A_plain", "B_auto", "C_strict"]
    assert scope["stages"]["structural"]["default_repetitions"] == 1
    assert scope["stages"]["structural"]["selected_repetitions"] == 3
    for stage in ("readcrop", "fix-single", "fix-multi", "patch"):
        assert scope["stages"][stage]["arms"] == ["A_plain", "B_auto", "C_strict"]
        assert scope["stages"][stage]["default_repetitions"] == 1
        assert scope["stages"][stage]["selected_repetitions"] == 1
    assert "arms" not in scope
    assert "repetitions" not in scope
    assert "study_mode" not in scope
    assert scope["coordinate_timeout_seconds"] == 600
    assert "complete_run_max_wall_clock_seconds" not in scope
    assert scope["nonpoolable"] is True
    assert scope["pooling_eligibility"] == "ineligible"
    assert scope["confirmatory_product_acceptance"] == "ineligible"
    assert scope["scope_digest"] == {
        "canonical_fields": [
            "active manifest SHA-256",
            "selection mode",
            "resolved ordered task IDs",
            "ordered stage partitions with repetitions, arms, and cell counts",
            "total tasks and cells",
        ],
        "runtime_derived": True,
        "stored_in_manifest": False,
    }
    assert scope["aggregate_execution_scope"] == {
        "canonical_fields": [
            "schema version",
            "active manifest SHA-256",
            "model and reasoning effort",
            "selection mode and ordered task IDs",
            "ordered stage partitions and their native scope SHA-256 values",
            "total tasks and cells",
        ],
        "approval_option": "--paid-approval",
        "dry_run_marker": "SCOPE",
    }

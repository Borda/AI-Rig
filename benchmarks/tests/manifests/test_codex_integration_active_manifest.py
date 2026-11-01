"""Regression checks for the Codex plain/CLI/Skill integration relock.

The integration manifest locks the three-arm study (plain, direct CLI, installed Skill) that every paid Codex stage is
admitted against. These tests pin what that lock must contain, that its builder reproduces it without touching the
methodology source, and that the runtime package freeze stays stable inside a gitless snapshot. No model or
authentication is used.
"""

from __future__ import annotations

import json
import re
import runpy
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from _manifest_helpers import load_manifest, sha256_file

ROOT = Path(__file__).resolve().parents[3]
BENCHMARKS = ROOT / "benchmarks"
GENERATOR = BENCHMARKS / "build-codex-integration-manifest.py"
SOURCE_MANIFEST = BENCHMARKS / "manifests" / "provider-parity-methodology.json"
MANIFEST = BENCHMARKS / "manifests" / "codex-integration.json"
HUMAN_MANIFEST = BENCHMARKS / "manifests" / "codex-integration.md"
ARMS = ["A_plain", "B_auto", "C_strict"]
ARM_ORDER = (
    "deterministic six-permutation counterbalancing by frozen structural task ordinal; "
    "across the 55-task single-repetition execution suite, every arm occupies every ordinal 18 or 19 times"
)
TOKEN_PROMPT_CACHE_POLICY = (
    "Console and primary efficiency reports use gross provider input tokens only. "  # noqa: S105 - policy text about LLM tokens, not a credential
    "Cached and fresh input counts are retained as raw telemetry diagnostics. "
    "The Codex CLI exposes no supported per-cell provider prompt-cache reset or disable control. "
    "Deterministic arm-order counterbalancing mitigates order exposure without claiming cache elimination."
)
COMPACT_QUERY_FORM = '"$CODEMAP_BIN" query --compact <subcommand> [arguments]'
NON_STRUCTURAL_STAGES = ("readcrop", "fix-single", "fix-multi", "patch")


def _run_generator(*args: str) -> subprocess.CompletedProcess[str]:
    """Execute the relock utility without invoking a model or authentication."""
    return subprocess.run(
        [sys.executable, str(GENERATOR), *args], cwd=ROOT, check=False, capture_output=True, text=True
    )


@pytest.fixture(name="manifest")
def _manifest() -> dict[str, Any]:
    """Load the shipped integration manifest fresh for each test."""
    return load_manifest(MANIFEST)


@pytest.fixture(name="builder", scope="module")
def _builder() -> dict[str, Any]:
    """Namespace of the manifest generator script, loaded once because its functions are pure."""
    return runpy.run_path(str(GENERATOR))


class TestGenerator:
    """The generator reproduces the manifest, owns its query and ordering policy, and leaves the source alone."""

    def test_pins_query_form_and_optional_arm_requirement(self, builder: dict[str, Any]) -> None:
        """Builder output replaces inherited query-argument drift with the compact direct-query form.

        B_auto must require exactly one successful compact query item while still allowing other shell work, and the
        telemetry admission must accept only that command shape.
        """
        arms = builder["_arms"]()
        telemetry = builder["_telemetry_admission"]()

        assert builder["EXPERIMENT_REVISION"] == "codex-integration-unified-task-cli-2026-08-11"
        assert arms["B_auto"]["requirement"] == (
            "Run at least one successful compact direct query in its own native command item containing exactly "
            f"{COMPACT_QUERY_FORM}; "
            "additional reads and shell work are allowed as separate items."
        )
        assert telemetry["query"]["accepted_form"] == COMPACT_QUERY_FORM

    def test_pins_arm_order_and_token_cache_policy(self, builder: dict[str, Any]) -> None:
        """Builder output carries the counterbalanced arm order and the gross-input-token cache policy.

        Order exposure cannot be eliminated for Codex (no cache reset), so the documented mitigation and the reporting
        convention must both survive regeneration.
        """
        source = load_manifest(SOURCE_MANIFEST)

        controls = builder["_execution_controls"](source)
        cells = builder["_preregistered_cells"](source)

        assert controls["arm_order"] == ARM_ORDER
        assert cells["arm_order"] == ARM_ORDER
        assert controls["token_prompt_cache_policy"] == TOKEN_PROMPT_CACHE_POLICY

    def test_task_selection_contract_is_nonpoolable_with_runtime_scope(self, builder: dict[str, Any]) -> None:
        """Builder task selection allows 219 default cells, one default repetition, and a runtime-derived scope digest.

        Structural runs three repetitions by selection while every other stage runs one; the manifest must neither store
        the digest nor carry a global repetition or study-mode field.
        """
        source = load_manifest(SOURCE_MANIFEST)

        cells = builder["_preregistered_cells"](source)
        selection = builder["_task_selection_contract"](source)
        stages = selection["stages"]

        assert selection["coordinate_timeout_seconds"] == 600
        assert selection["nonpoolable"] is True
        assert selection["default_total_cells"] == 219
        assert stages["structural"]["allowed_task_ids"] == cells["structural_execution_task_ids"]
        assert stages["structural"]["default_repetitions"] == 1
        assert stages["structural"]["selected_repetitions"] == 3
        assert all(stages[stage]["default_repetitions"] == 1 for stage in NON_STRUCTURAL_STAGES)
        assert all(stages[stage]["selected_repetitions"] == 1 for stage in NON_STRUCTURAL_STAGES)
        assert "repetitions" not in selection
        assert "study_mode" not in selection
        assert selection["resolution_policy"]["exact_id_first"] is True
        assert selection["resolution_policy"]["deduplicate"].startswith("selector tokens")
        assert selection["scope_digest"]["runtime_derived"] is True
        assert selection["scope_digest"]["stored_in_manifest"] is False
        assert selection["aggregate_execution_scope"]["approval_option"] == "--paid-approval"
        assert selection["aggregate_execution_scope"]["dry_run_marker"] == "SCOPE"

    def test_check_is_current_and_never_rewrites_methodology_source(self) -> None:
        """The integration relock is deterministic and leaves the methodology source byte-identical.

        ``--check`` is run through the real command line, since that is the gate contributors use, and the source
        manifest digest is compared before and after to prove the generator only reads it.
        """
        before = sha256_file(SOURCE_MANIFEST)

        result = _run_generator("--check")

        assert result.returncode == 0, result.stderr
        assert sha256_file(SOURCE_MANIFEST) == before

    def test_stale_error_names_exact_rebuild_command(self, builder: dict[str, Any], tmp_path: Path) -> None:
        """Internal check mode must identify the command that repairs generated drift.

        A stale file checked against different expected bytes has to end its error with the copy-pasteable rebuild
        command, so nobody has to guess how to regenerate.
        """
        with pytest.raises(ValueError, match=r"stale") as raised:
            builder["_write_or_check"](tmp_path / "stale.json", b"expected\n", True)
        assert str(raised.value).endswith("run: uv run python benchmarks/build-codex-integration-manifest.py")

    def test_cli_identity_is_host_neutral_reviewed_provenance(self, builder: dict[str, Any]) -> None:
        """The manifest records the reviewed Codex CLI identity without imposing an admission pin.

        The recorded path is a placeholder rather than the generating host's path, so regenerating on another machine
        cannot change the manifest.
        """
        assert builder["REVIEWED_CODEX_CLI_VERSION"] == "codex-cli 0.146.1"
        assert builder["_codex_cli_identity"]() == {
            "available": True,
            "path": "<codex-cli>",
            "reviewed_version": "codex-cli 0.146.1",
        }

    def test_package_freeze_reuses_live_mode_map_inside_gitless_snapshot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Paid source snapshots preserve the live package payload and executable modes.

        A snapshot has no git history, so the freeze must read the executable-mode map copied next to it and still hash
        the same package as the live checkout. The real plugin tree is copied because the package payload is the
        contract under test.
        """
        generator = runpy.run_path(str(GENERATOR))
        freeze_package = generator["_codemap_package_manifest_sha256"]
        live_sha256 = freeze_package()
        snapshot_root = tmp_path / "source"
        shutil.copytree(ROOT / "plugins" / "codemap-py", snapshot_root / "plugins" / "codemap-py")
        mode_map_path = snapshot_root / "benchmarks" / "manifests" / "codemap-package-mode-map.json"
        mode_map_path.parent.mkdir(parents=True)
        package_builder = runpy.run_path(str(ROOT / "plugins" / "codemap-py" / "scripts" / "build_package.py"))
        live_mode_map = package_builder["_git_exec_modes"](ROOT / "plugins" / "codemap-py")
        mode_map_path.write_text(json.dumps(live_mode_map, sort_keys=True), encoding="utf-8")
        monkeypatch.setitem(freeze_package.__globals__, "ROOT", snapshot_root)
        monkeypatch.setenv("CODEX_LAUNCHER_SNAPSHOT_ACTIVE", "1")

        assert freeze_package() == live_sha256


class TestManifestIdentity:
    """The manifest reuses the canonical suite identities and records host-neutral provenance."""

    def test_uses_environment_neutral_cli_and_posix_paths(self, manifest: dict[str, Any]) -> None:
        """Generated records cannot contain host paths or Windows separators.

        The Codex CLI is recorded as a placeholder and the source manifest path is repository-relative POSIX, so a
        regeneration on Windows or another home directory yields identical bytes.
        """
        assert manifest["codex_cli"] == {
            "available": True,
            "path": "<codex-cli>",
            "reviewed_version": "codex-cli 0.146.1",
        }
        assert manifest["source_manifest"]["path"] == "benchmarks/manifests/provider-parity-methodology.json"
        assert "\\" not in manifest["source_manifest"]["path"]

    def test_source_manifest_digest_is_recorded(self, manifest: dict[str, Any]) -> None:
        """The manifest names its source methodology and that file's exact SHA-256.

        The digest ties the relock to the methodology bytes it was built from; a methodology edit without a relock must
        therefore be visible.
        """
        assert manifest["source_manifest"] == {
            "path": SOURCE_MANIFEST.relative_to(ROOT).as_posix(),
            "sha256": sha256_file(SOURCE_MANIFEST),
        }

    @pytest.mark.parametrize(
        "key",
        [
            "evaluation_contract",
            "headline_structural_v1",
            "index",
            "oracle_remediation",
            "suite_integrity",
            "suites",
            "target_source",
            "validation",
        ],
    )
    def test_reuses_canonical_suite_identity(self, manifest: dict[str, Any], key: str) -> None:
        """Task, suite, prompt, oracle, evaluator, target, and index identities cannot drift from the methodology.

        Each section is compared whole with the provider-parity methodology it is derived from, so any divergence in a
        suite identity fails under that section's name.
        """
        assert manifest[key] == load_manifest(SOURCE_MANIFEST)[key]

    def test_repository_marketplace_remains_the_installable_source_root(self) -> None:
        """The live marketplace must not be overwritten by a frozen snapshot manifest.

        Snapshot freezing swaps source roots for a disposable copy; the repository's own marketplace file has to keep
        pointing at the three local plugins users install from.
        """
        marketplace = load_manifest(ROOT / ".agents" / "plugins" / "marketplace.json")
        sources = {entry["name"]: entry["source"] for entry in marketplace["plugins"]}

        assert marketplace["name"] == "borda-ai-rig"
        assert sources == {
            "bridge": {"path": "./plugins/bridge_cc-codex", "source": "local"},
            "codemap-py": {"path": "./plugins/codemap-py", "source": "local"},
            "codex-rig": {"path": "./plugins/codex-rig", "source": "local"},
        }


class TestManifestArmsAndCells:
    """The three planned Codex arms, their estimands, cells and permission profiles."""

    def test_locks_arms_model_and_estimands(self, manifest: dict[str, Any]) -> None:
        """Only the three planned Codex arms, the locked model and the contrasts they estimate are eligible.

        The estimands name what each arm pair measures (product, availability, integration effect), so a renamed arm or
        contrast would change what the study claims.
        """
        assert list(manifest["arms"]) == ARMS
        assert manifest["model"] == {
            "name": "gpt-6.1-sol",
            "additional_strata": ["gpt-6-luna", "gpt-6-sol", "gpt-6-astra"],
            "reasoning_effort": "high",
            "strict_config": True,
        }
        assert manifest["estimands"] == {
            "C_strict-A_plain": "product effect",
            "B_auto-A_plain": "direct CLI availability effect",
            "C_strict-B_auto": "integration effect",
        }

    def test_locks_experiment_identity_and_package_roster(self, manifest: dict[str, Any]) -> None:
        """The experiment id, schema, revision and the exact package roster are pinned.

        The C arm installs precisely this roster, so a changed roster or revision must be a deliberate relock.
        """
        assert manifest["package_roster"] == ["codemap-py", "codex-rig"]
        assert manifest["experiment_id"] == "codex-integration-v1"
        assert manifest["schema_version"] == "codex-integration-manifest-v3"
        assert manifest["experiment_revision"] == "codex-integration-unified-task-cli-2026-08-11"

    def test_preregisters_confirmatory_and_diagnostic_task_partition(self, manifest: dict[str, Any]) -> None:
        """Execution tasks are the disjoint union of 45 confirmatory and 10 diagnostic structural tasks.

        Confirmatory inference must never share a task with a diagnostic one, and the counterbalanced arm order is
        defined over the combined 55-task execution suite.
        """
        cells = manifest["preregistered_cells"]
        confirmatory = set(cells["structural_confirmatory_task_ids"])
        diagnostic = set(cells["structural_diagnostic_task_ids"])

        assert cells["arms"] == ARMS
        assert cells["providers"] == ["codex"]
        assert cells["confirmatory_repetitions"] == 1
        assert cells["arm_order"] == ARM_ORDER
        assert len(cells["structural_confirmatory_task_ids"]) == 45
        assert len(cells["structural_execution_task_ids"]) == 55
        assert len(cells["structural_diagnostic_task_ids"]) == 10
        assert set(cells["structural_execution_task_ids"]) == confirmatory | diagnostic
        assert not confirmatory & diagnostic

    def test_locks_permission_profiles_and_host_tooling_isolation(self, manifest: dict[str, Any]) -> None:
        """Treatment arms get the Codemap profile, and host tooling roots are denied in every arm.

        Denying the host's agent directories keeps A, B and C on equal footing, and the marketplace source is denied
        except where B stages its own direct runtime.
        """
        profiles = manifest["codex_permission_profiles"]
        assert profiles["treatment"]["arms"] == ["B_auto", "C_strict"]
        assert profiles["treatment_runtime"]["scope"] == ["B_auto", "C_strict"]
        assert profiles["host_tooling_isolation"] == {
            "access": "deny",
            "arms": ARMS,
            "roots": ["<host-home>/.agents", "<host-home>/.claude", "<host-home>/.codex"],
            "verification": "no-model directory enumeration must fail without emitting entries",
        }
        assert profiles["marketplace_source_access"] == {
            "A_plain": "deny",
            "B_auto": "deny after the locked direct runtime is staged inside its disposable home",
            "C_strict": "deny",
        }

    def test_locks_execution_controls(self, manifest: dict[str, Any]) -> None:
        """The per-coordinate timeout budget, arm order and cache policy are locked without a run-wide clock.

        The 600-second budget is shared by an attempt and its eligible retries; a complete-run wall clock would make
        results depend on machine speed, so it must stay absent.
        """
        controls = manifest["execution_controls"]
        assert controls["parity_timeout_seconds"] == 600
        assert controls["coordinate_timeout_scope"] == (
            "one total 600-second budget shared by the initial attempt and at most two eligible retries"
        )
        assert "complete_run_wall_clock" not in controls
        assert "confirmatory_max_wall_clock_seconds" not in controls
        assert controls["arm_order"] == ARM_ORDER
        assert controls["token_prompt_cache_policy"] == TOKEN_PROMPT_CACHE_POLICY


class TestManifestAdmission:
    """Artifact identities and the no-model gates that must pass before paid execution."""

    def test_codex_rig_integration_admission_requires_locked_install(self, manifest: dict[str, Any]) -> None:
        """Paid smoke is gated on the locked install order, public launcher resolution and a persisted query context.

        These three prerequisites are what make the C arm a real integration rather than a stub, so they are listed
        verbatim.
        """
        assert manifest["codex_rig_integration_admission"]["required_before_paid_smoke"] == [
            "C installs exactly the locked codemap-py then codex-rig package roster.",
            "The Codex Rig adapter resolves the public CODEMAP_BIN launcher before PATH.",
            "One persisted compact-query context is available without Codemap cache or source reads.",
        ]

    def test_artifact_hashes_cover_every_implementation_file(self, manifest: dict[str, Any]) -> None:
        """The candidate package digest is SHA-256 shaped and the artifact roster is exact and mirrored.

        The hashed roster defines the code identity of the study; an added or dropped artifact must be an explicit
        relock, and the implementation contract has to repeat the same map.
        """
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

    def test_direct_cli_runtime_and_admission_probe(self, manifest: dict[str, Any]) -> None:
        """B's staged direct runtime lists its launcher files and is admitted by a locked fn-rdeps probe.

        The probe must exit 0 with a complete query and unchanged index bytes under the treatment permission profile,
        otherwise the optional arm could be admitted with a launcher that cannot actually query.
        """
        direct_runtime = manifest["direct_cli_runtime"]
        assert direct_runtime["staged_root"] == "<disposable-CODEX_HOME>/direct-cli"
        assert set(direct_runtime["files"]) >= {
            "bin/codemap-py",
            "bin/_exclusions.py",
            "scripts/codemap_py_entry.py",
            "src/codemap_py/query/__init__.py",
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

    def test_no_model_validation_requires_runtime_smoke_before_paid_execution(self, manifest: dict[str, Any]) -> None:
        """The no-model validation block used no model and leaves every check marked as still required.

        The manifest must not claim a runtime smoke already happened; it only records the evidence and states the gate.
        """
        validation = manifest["no_model_validation"]
        assert validation["model_or_auth_used"] is False
        assert validation["status"] == "runtime_smoke_required_before_paid_execution"
        assert set(validation["checks"].values()) == {"required"}
        assert len(validation["evidence"]) == 2

    def test_telemetry_admission_credits_only_canonical_compact_queries(self, manifest: dict[str, Any]) -> None:
        """Telemetry credit needs the installed-Skill binding and a dedicated, successful, canonical compact query.

        A manual Skill read is diagnostic only; query credit comes from the command item, which keeps B's optional use
        and C's required use attributable.
        """
        telemetry = manifest["telemetry_admission"]
        assert telemetry["telemetry_contract_id"] == "installed-skill-binding-locked-query-components-v3"
        assert telemetry["skill_binding"] == {
            "arm": "C_strict",
            "environment_binding": "runner-owned immutable exact installed query Skill path; absent from A and B",
            "manual_read_telemetry": "skill_delivery_observed is diagnostic-only and never a query-credit prerequisite",
        }
        assert telemetry["query"] == {
            "accepted_form": COMPACT_QUERY_FORM,
            "item_scope": "dedicated native command item",
            "required_exit_code": 0,
            "required_output": "one JSON document with index.query_complete=true and index.compact=true",
        }
        assert telemetry["treatment_attribution"] == {
            "B_auto": "direct CLI reachable; the model decides whether to query",
            "C_strict": "at least one successful canonical compact query in the immutable installed-Skill treatment",
        }
        assert all("Skill read" not in item for item in telemetry["rejected_evidence"])


class TestManifestTaskSelection:
    """The unified, nonpoolable task selection across structural, ReadCrop, Fix and Patch stages."""

    STAGE_ORDER = ("structural", "readcrop", "fix-single", "fix-multi", "patch")

    def test_orders_stages_and_flattens_allowed_ids(self, manifest: dict[str, Any]) -> None:
        """Selections preserve stage order and sizes, and the flat id list is the stages concatenated.

        73 tasks over five stages give the 219 default cells; a reordered or resized stage would change what ``--tasks``
        resolves to and the approved scope.
        """
        scope = manifest["task_selection"]
        stage_order = list(self.STAGE_ORDER)
        flattened_ids = [task_id for stage in stage_order for task_id in scope["stages"][stage]["allowed_task_ids"]]

        assert scope["selector_option"] == "--tasks"
        assert scope["separator"] == ","
        assert scope["stage_order"] == stage_order
        assert {stage: len(scope["stages"][stage]["allowed_task_ids"]) for stage in stage_order} == {
            "structural": 55,
            "readcrop": 6,
            "fix-single": 4,
            "fix-multi": 3,
            "patch": 5,
        }
        assert scope["allowed_task_ids"] == flattened_ids
        assert len(scope["allowed_task_ids"]) == 73
        assert scope["default_total_cells"] == 219
        assert scope["allowed_families"] == list(dict.fromkeys(task_id.split("-", 1)[0] for task_id in flattened_ids))

    def test_resolution_policy_prefers_exact_ids_and_rejects_unknowns(self, manifest: dict[str, Any]) -> None:
        """Selector tokens resolve exact ids first, expand families, keep stage order and reject unknowns.

        The policy text is what the user reads when approving a ``--tasks`` selection, so each rule is pinned verbatim.
        """
        assert manifest["task_selection"]["resolution_policy"] == {
            "exact_id_first": True,
            "family_match": "a family token selects every matching allowed ID",
            "order": "resolved IDs preserve structural, ReadCrop, Fix-Single, Fix-Multi, then Patch stage order",
            "deduplicate": "selector tokens and overlapping expanded IDs are evaluated once",
            "reject": ["empty tokens", "unknown task IDs", "unknown families"],
        }

    @pytest.mark.parametrize(
        ("stage", "selected_repetitions"),
        [
            pytest.param("structural", 3, id="structural-three-repetitions"),
            pytest.param("readcrop", 1, id="readcrop-single"),
            pytest.param("fix-single", 1, id="fix-single-single"),
            pytest.param("fix-multi", 1, id="fix-multi-single"),
            pytest.param("patch", 1, id="patch-single"),
        ],
    )
    def test_stage_runs_every_arm_with_native_repetitions(
        self, manifest: dict[str, Any], stage: str, selected_repetitions: int
    ) -> None:
        """Each stage runs all three arms, defaults to one repetition, and structural selects three.

        Structural gets its repetitions from the selection while every other stage keeps its native single repetition.
        """
        entry = manifest["task_selection"]["stages"][stage]

        assert entry["arms"] == ARMS
        assert entry["default_repetitions"] == 1
        assert entry["selected_repetitions"] == selected_repetitions

    def test_is_nonpoolable_with_runtime_derived_scope_digests(self, manifest: dict[str, Any]) -> None:
        """Selection is ineligible for pooling and its scope digests are computed at run time, not stored.

        Global arms, repetitions and study-mode fields must not exist beside the per-stage ones, and the aggregate
        approval scope lists every field the paid approval binds to.
        """
        scope = manifest["task_selection"]
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

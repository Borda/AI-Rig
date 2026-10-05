"""Regression checks for bounded, approved code-remediation work buckets."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest
from _platform import FILE_SYMLINKS_AVAILABLE

PLUGIN_ROOT = Path(__file__).resolve().parents[2]


def _load_validator() -> ModuleType:
    """Load the hyphenated artifact-validator module for focused contract tests."""
    path = PLUGIN_ROOT / "shared" / "validate-artifacts.py"
    spec = importlib.util.spec_from_file_location("codex_rig_validate_artifacts", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VALIDATOR = _load_validator()


def _write_workplan(metadata: dict[str, object], out_dir: Path) -> None:
    """Write plan, approval, context, and human-readable binding evidence."""
    workplan = metadata["resolution_workplan"]
    assert isinstance(workplan, dict)
    buckets = workplan["work_buckets"]
    assert isinstance(buckets, list)

    specialists_dir = out_dir / "specialists"
    specialists_dir.mkdir()
    for bucket in buckets:
        assert isinstance(bucket, dict)
        if bucket["owner"] != "parent":
            context_path = out_dir / str(bucket["context_pack_path"])
            context_path.write_text("# Context\n", encoding="utf-8")
            bucket["context_sha256"] = hashlib.sha256(context_path.read_bytes()).hexdigest()

    plan_path = out_dir / "work-bucket-plan.json"
    plan_path.write_text(
        json.dumps({"schema_version": 1, "work_buckets": buckets}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    plan_sha256 = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    approval_status = workplan["parallel_approval_status"]
    response = {"approved": "approve", "parent-only": "parent-only", "not-required": "not-required"}[
        str(approval_status)
    ]
    workplan.update(
        {
            "approved_plan_sha256": plan_sha256 if response == "approve" else None,
            "bucket_plan_path": "work-bucket-plan.json",
            "bucket_plan_sha256": plan_sha256,
            "parallel_approval_path": "parallel-approval.json",
            "parallel_approval_response": response,
        }
    )
    (out_dir / "parallel-approval.json").write_text(
        json.dumps(
            {
                "plan_sha256": plan_sha256,
                "prompt_presented": workplan["parallel_prompt_presented"],
                "response": response,
                "source": workplan["parallel_approval_source"],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    bucket_rows = "\n".join(
        f"| {bucket['bucket_id']} | {bucket['owner']} | {bucket['verifier']} | {bucket['context_pack_path']} | tests |"
        for bucket in buckets
    )
    (out_dir / "resolution-workplan.md").write_text(
        f"""# Resolution Workplan

## Work Bucket Plan

| Bucket | Owner | Verifier | Context | Closure |
| --- | --- | --- | --- | --- |
{bucket_rows}

## Parallel Approval

Approval source: {workplan["parallel_approval_source"]}.
Approval response: {response}.
Plan SHA-256: {plan_sha256}.

## Execution Order

Parallel after approval.

## Ungrouped Items

None.
""",
        encoding="utf-8",
    )


def _parallel_metadata() -> dict[str, object]:
    """Return a valid six-item, two-bucket parallel plan.

    Example:
        >>> len(_parallel_metadata()["resolution_workplan"]["work_buckets"])
        2
    """
    return {
        "resolution_scope": {"selected_indexes": [1, 2, 3, 4, 5, 6]},
        "resolution_workplan": {
            "execution_mode": "parallel-specialists",
            "groups_total": 2,
            "max_items_per_bucket": 5,
            "parallel_approval_required": True,
            "parallel_approval_source": "explicit-input",
            "parallel_approval_status": "approved",
            "parallel_eligible": True,
            "parallel_prompt_presented": False,
            "parent_owned_groups": 0,
            "specialist_owned_groups": 2,
            "unassigned_selected_items": 0,
            "verifier_groups": 2,
            "work_buckets": [
                {
                    "bucket_id": "B1",
                    "selected_indexes": [1, 2, 3],
                    "owner": "sw-engineer",
                    "verifier": "qa-specialist",
                    "context_pack_path": "specialists/b1-context.md",
                    "context_sha256": "a" * 64,
                    "owned_paths": ["src/feature.py"],
                    "resource_locks": [],
                    "output": "b1.patch",
                    "execution_mode": "parallel",
                },
                {
                    "bucket_id": "B2",
                    "selected_indexes": [4, 5, 6],
                    "owner": "doc-scribe",
                    "verifier": "parent",
                    "context_pack_path": "specialists/b2-context.md",
                    "context_sha256": "b" * 64,
                    "owned_paths": ["docs/feature.md"],
                    "resource_locks": [],
                    "output": "b2.patch",
                    "execution_mode": "parallel",
                },
            ],
            "workplan_path": "resolution-workplan.md",
        },
    }


def _write_completed_production_lifecycle(metadata: dict[str, object], out_dir: Path) -> Path:
    """Write one truthful schema-v2 completed lifecycle projection for approved buckets."""
    _write_workplan(metadata, out_dir)
    workplan = metadata["resolution_workplan"]
    assert isinstance(workplan, dict)
    buckets = workplan["work_buckets"]
    assert isinstance(buckets, list)
    plan_path = out_dir / "work-bucket-plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "consumer": "code-remediate",
                "write_parallel_promoted": False,
                "source_repository": "authoritative-repository",
                "worktree_root": ".codex-rig-worktrees/example",
                "baseline_head": "a" * 40,
                "baseline_tree": "b" * 40,
                "rollback_policy": "approved-paths-if-preapply-baseline-matches",
                "cleanup_policy": "non-force-after-durable-source-application",
                "state_path": "production-lifecycle.json",
                "verification_gate": "code-remediate-shared-quality-gates",
                "work_buckets": buckets,
                "status": "frozen-awaiting-explicit-approval",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    plan_sha256 = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    workplan["bucket_plan_sha256"] = plan_sha256
    workplan["approved_plan_sha256"] = plan_sha256
    approval_path = out_dir / "parallel-approval.json"
    approval_path.write_text(
        json.dumps(
            {
                "plan_sha256": plan_sha256,
                "prompt_presented": workplan["parallel_prompt_presented"],
                "response": "approve",
                "source": workplan["parallel_approval_source"],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    workplan_path = out_dir / "resolution-workplan.md"
    old_digest = next(
        digest for digest in workplan_path.read_text(encoding="utf-8").split() if len(digest.removesuffix(".")) == 64
    ).removesuffix(".")
    workplan_path.write_text(
        workplan_path.read_text(encoding="utf-8").replace(old_digest, plan_sha256), encoding="utf-8"
    )
    evidence_root = ".reports/codex/code-remediate/fixture"
    nodes: list[dict[str, object]] = []
    for position, bucket in enumerate(buckets):
        assert isinstance(bucket, dict)
        patch_path = out_dir / str(bucket["output"])
        patch_path.write_text(
            f"diff --git a/{bucket['output']} b/{bucket['output']}\n",
            encoding="utf-8",
        )
        nodes.append(
            {
                "node_id": bucket["bucket_id"],
                "owned_paths": bucket["owned_paths"],
                "patch_path": f"{evidence_root}/{bucket['output']}",
                "patch_sha256": hashlib.sha256(patch_path.read_bytes()).hexdigest(),
            }
        )
    source_patch_path = out_dir / "source-application.patch"
    source_patch_path.write_text("diff --git a/src/feature.py b/src/feature.py\n", encoding="utf-8")
    rollback_path = out_dir / "rollback.patch"
    rollback_path.write_text("diff --git a/src/feature.py b/src/feature.py\n", encoding="utf-8")
    lifecycle_path = out_dir / "production-lifecycle.json"
    lifecycle_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "consumer": "code-remediate",
                "status": "completed",
                "plan_sha256": plan_sha256,
                "state_path": f"{evidence_root}/production-lifecycle.json",
                "verification_gate": "code-remediate-shared-quality-gates",
                "source": {
                    "baseline_head": "a" * 40,
                    "baseline_tree": "b" * 40,
                    "applied_head": "a" * 40,
                    "preimage_sha256": {"docs/feature.md": "c" * 64, "src/feature.py": "d" * 64},
                    "postimage_sha256": {"docs/feature.md": "e" * 64, "src/feature.py": "f" * 64},
                },
                "evidence_root": evidence_root,
                "nodes": nodes,
                "joined_nodes": [
                    {
                        "node_id": node["node_id"],
                        "owned_paths": node["owned_paths"],
                        "patch_sha256": node["patch_sha256"],
                    }
                    for node in nodes
                ],
                "integration": {
                    "status": "structurally-verified",
                    "order": [bucket["bucket_id"] for bucket in buckets],
                    "paths": ["docs/feature.md", "src/feature.py"],
                },
                "source_application": {
                    "status": "applied",
                    "applied_paths": ["docs/feature.md", "src/feature.py"],
                    "patch_path": source_patch_path.name,
                    "patch_sha256": hashlib.sha256(source_patch_path.read_bytes()).hexdigest(),
                    "rollback_patch_path": "rollback.patch",
                    "rollback_patch_sha256": hashlib.sha256(rollback_path.read_bytes()).hexdigest(),
                },
                "cleanup": {"status": "removed", "force": False},
                "containment": {
                    "mode": "parent-authoritative-worktrees",
                    "capability_sandbox_verified": False,
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    workplan["production_lifecycle"] = {
        "path": lifecycle_path.name,
        "sha256": hashlib.sha256(lifecycle_path.read_bytes()).hexdigest(),
        "status": "completed",
    }
    return lifecycle_path


def _refresh_production_lifecycle_digest(metadata: dict[str, object], lifecycle_path: Path) -> None:
    """Rebind fixture metadata after one deliberate lifecycle mutation."""
    workplan = metadata["resolution_workplan"]
    assert isinstance(workplan, dict)
    reference = workplan["production_lifecycle"]
    assert isinstance(reference, dict)
    reference["sha256"] = hashlib.sha256(lifecycle_path.read_bytes()).hexdigest()


def test_parallel_work_buckets_reject_planning_only_approval_without_runtime_lifecycle(tmp_path: Path) -> None:
    """Prevent a schema-v1 approved bucket proposal from masquerading as completed production execution."""
    metadata = _parallel_metadata()
    _write_workplan(metadata, tmp_path)

    with pytest.raises(SystemExit, match="code-remediate-production-lifecycle-required"):
        VALIDATOR._validate_code_remediate_workplan(metadata, tmp_path)


def test_low_volume_selection_stays_in_one_agent_scope(tmp_path: Path) -> None:
    """Keep one coherent parent bucket valid for historical low-volume work."""
    metadata = _parallel_metadata()
    metadata["resolution_scope"] = {"selected_indexes": [1, 2]}
    metadata["resolution_workplan"] = {
        "execution_mode": "parent-owned",
        "groups_total": 1,
        "max_items_per_bucket": 5,
        "parallel_approval_required": False,
        "parallel_approval_source": "not-required",
        "parallel_approval_status": "not-required",
        "parallel_eligible": False,
        "parallel_prompt_presented": False,
        "parent_owned_groups": 1,
        "specialist_owned_groups": 0,
        "unassigned_selected_items": 0,
        "verifier_groups": 1,
        "work_buckets": [
            {
                "bucket_id": "B1",
                "selected_indexes": [1, 2],
                "owner": "parent",
                "verifier": "qa-specialist",
                "context_pack_path": "resolution-workplan.md",
                "owned_paths": ["src/feature.py"],
                "execution_mode": "parent",
            }
        ],
        "workplan_path": "resolution-workplan.md",
    }
    _write_workplan(metadata, tmp_path)

    VALIDATOR._validate_code_remediate_workplan(metadata, tmp_path, current_contract=False)


def test_ineligible_parallel_plan_accepts_user_approved_fallback(tmp_path: Path) -> None:
    """Accept a fallback only when the recorded plan is actually parallel-ineligible."""
    metadata = _parallel_metadata()
    metadata["resolution_scope"] = {"selected_indexes": [1, 2]}
    workplan = metadata["resolution_workplan"]
    assert isinstance(workplan, dict)
    workplan.update(
        execution_mode="parent-owned",
        groups_total=1,
        parent_owned_groups=1,
        specialist_owned_groups=0,
        verifier_groups=1,
        parallel_eligible=False,
        parallel_approval_source="explicit-input",
        parallel_approval_status="parent-only",
    )
    workplan["work_buckets"] = [
        {
            "bucket_id": "B1",
            "selected_indexes": [1, 2],
            "owner": "parent",
            "verifier": "qa-specialist",
            "context_pack_path": "resolution-workplan.md",
            "owned_paths": ["src/feature.py"],
            "execution_mode": "parent",
        }
    ]
    _write_workplan(metadata, tmp_path)

    VALIDATOR._validate_code_remediate_workplan(metadata, tmp_path)
    workplan["parallel_eligible"] = True
    with pytest.raises(SystemExit, match="code-remediate-eligible-fanout-fallback-forbidden"):
        VALIDATOR._validate_code_remediate_workplan(metadata, tmp_path)


@pytest.mark.parametrize("execution_mode", ["parent-owned", "sequential-specialists"])
def test_ineligible_plan_accepts_workflow_default_continuation(tmp_path: Path, execution_mode: str) -> None:
    """Permit a truthful parent or sequential route when parallel writing is infeasible."""
    metadata = _parallel_metadata()
    workplan = metadata["resolution_workplan"]
    assert isinstance(workplan, dict)
    workplan.update(
        execution_mode=execution_mode,
        parallel_approval_required=False,
        parallel_approval_source="workflow-default",
        parallel_approval_status="parent-only",
        parallel_eligible=False,
        parallel_prompt_presented=False,
    )
    buckets = workplan["work_buckets"]
    assert isinstance(buckets, list)
    if execution_mode == "parent-owned":
        workplan.update(groups_total=2, parent_owned_groups=2, specialist_owned_groups=0)
        for bucket in buckets:
            bucket["owner"] = "parent"
            bucket["execution_mode"] = "parent"
    else:
        for bucket in buckets:
            bucket["execution_mode"] = "sequential"
    _write_workplan(metadata, tmp_path)

    document = tmp_path / "resolution-workplan.md"
    document.write_text(
        document.read_text(encoding="utf-8").replace(
            "## Parallel Approval\n",
            "## Parallel Approval\n\nIneligibility reason: Runtime exposes one writer slot for these buckets.\n",
        ),
        encoding="utf-8",
    )
    VALIDATOR._validate_code_remediate_workplan(metadata, tmp_path)

    workplan["parallel_eligible"] = True
    with pytest.raises(SystemExit, match="code-remediate-eligible-fanout-fallback-forbidden"):
        VALIDATOR._validate_code_remediate_workplan(metadata, tmp_path)


@pytest.mark.parametrize("reason", [None, "", "   ", "none", "TBD", "<concrete reason>"])
def test_default_fallback_requires_concrete_ineligibility_reason(tmp_path: Path, reason: str | None) -> None:
    """Reject a reasonless or placeholder serial choice for otherwise independent buckets."""
    metadata = _parallel_metadata()
    workplan = metadata["resolution_workplan"]
    assert isinstance(workplan, dict)
    workplan.update(
        execution_mode="sequential-specialists",
        parallel_approval_required=False,
        parallel_approval_source="workflow-default",
        parallel_approval_status="parent-only",
        parallel_eligible=False,
        parallel_prompt_presented=False,
    )
    for bucket in workplan["work_buckets"]:
        bucket["execution_mode"] = "sequential"
    _write_workplan(metadata, tmp_path)
    document = tmp_path / "resolution-workplan.md"
    if reason is not None:
        document.write_text(
            document.read_text(encoding="utf-8").replace(
                "## Parallel Approval\n", f"## Parallel Approval\n\nIneligibility reason: {reason}\n"
            ),
            encoding="utf-8",
        )
    with pytest.raises(SystemExit, match="code-remediate-ineligibility-reason-missing"):
        VALIDATOR._validate_code_remediate_workplan(metadata, tmp_path)


@pytest.mark.parametrize("execution_mode", ["parent-owned", "sequential-specialists"])
def test_current_fallback_rejects_historical_not_required_route(tmp_path: Path, execution_mode: str) -> None:
    """Prevent new selected-scope work from avoiding its reason through legacy dispatch fields."""
    metadata = _parallel_metadata()
    workplan = metadata["resolution_workplan"]
    workplan.update(
        execution_mode=execution_mode,
        parallel_approval_required=False,
        parallel_approval_source="not-required",
        parallel_approval_status="not-required",
        parallel_eligible=False,
        parallel_prompt_presented=False,
    )
    if execution_mode == "parent-owned":
        workplan.update(parent_owned_groups=2, specialist_owned_groups=0)
    for bucket in workplan["work_buckets"]:
        bucket["execution_mode"] = "parent" if execution_mode == "parent-owned" else "sequential"
        if execution_mode == "parent-owned":
            bucket["owner"] = "parent"
    _write_workplan(metadata, tmp_path)

    with pytest.raises(SystemExit, match="code-remediate-current-fallback-dispatch-required"):
        VALIDATOR._validate_code_remediate_workplan(metadata, tmp_path)

    VALIDATOR._validate_code_remediate_workplan(metadata, tmp_path, current_contract=False)


def test_workflow_default_fallback_rejects_presented_prompt(tmp_path: Path) -> None:
    """Keep an actual user prompt distinct from a workflow-owned continuation."""
    metadata = _parallel_metadata()
    workplan = metadata["resolution_workplan"]
    assert isinstance(workplan, dict)
    workplan.update(
        execution_mode="sequential-specialists",
        parallel_approval_required=False,
        parallel_approval_source="workflow-default",
        parallel_approval_status="parent-only",
        parallel_eligible=False,
        parallel_prompt_presented=True,
    )
    for bucket in workplan["work_buckets"]:
        bucket["execution_mode"] = "sequential"
    _write_workplan(metadata, tmp_path)

    with pytest.raises(SystemExit, match="code-remediate-default-fallback-prompt-invalid"):
        VALIDATOR._validate_code_remediate_workplan(metadata, tmp_path)


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        pytest.param("too-large", "code-remediate-work-bucket-too-large", id="too-large"),
        pytest.param("duplicate-index", "code-remediate-work-bucket-coverage-mismatch", id="duplicate-index"),
        pytest.param("low-volume-fanout", "code-remediate-production-lifecycle-required", id="low-volume-fanout"),
        pytest.param("missing-approval", "code-remediate-parallel-approval-missing", id="missing-approval"),
        pytest.param(
            "declined-fanout-unrecorded",
            "code-remediate-eligible-fanout-approval-not-recorded",
            id="declined-fanout-unrecorded",
        ),
        pytest.param("aliased-path", "code-remediate-parallel-ownership-overlap", id="aliased-path"),
        pytest.param("ancestor-path-overlap", "code-remediate-parallel-ownership-overlap", id="ancestor-path-overlap"),
        pytest.param("unsupported-owner", "code-remediate-work-bucket-owner-unsupported", id="unsupported-owner"),
        pytest.param(
            "parent-parallel-mode", "code-remediate-parent-work-bucket-mode-invalid", id="parent-parallel-mode"
        ),
        pytest.param("missing-context", "code-remediate-specialist-context-pack-missing", id="missing-context"),
        pytest.param(
            "one-specialist-per-finding", "code-remediate-one-specialist-per-finding", id="one-specialist-per-finding"
        ),
    ],
)
def test_invalid_work_bucket_plans_fail_closed(tmp_path: Path, mutation: str, error: str) -> None:
    """Reject plans that create excess fan-out, overlap, or unapproved parallel work."""
    metadata = copy.deepcopy(_parallel_metadata())
    scope = metadata["resolution_scope"]
    workplan = metadata["resolution_workplan"]
    assert isinstance(scope, dict) and isinstance(workplan, dict)
    buckets = workplan["work_buckets"]
    assert isinstance(buckets, list)

    if mutation == "too-large":
        buckets[0]["selected_indexes"] = [1, 2, 3, 4, 5, 6]
        buckets.pop()
        workplan["groups_total"] = 1
        workplan["specialist_owned_groups"] = 1
        workplan["verifier_groups"] = 1
    elif mutation == "duplicate-index":
        buckets[1]["selected_indexes"] = [3, 4, 5, 6]
    elif mutation == "low-volume-fanout":
        scope["selected_indexes"] = [1, 2]
        buckets[0]["selected_indexes"] = [1]
        buckets[0]["singleton_rationale"] = "Distinct runtime boundary."
        buckets[1]["selected_indexes"] = [2]
        buckets[1]["singleton_rationale"] = "Distinct documentation boundary."
    elif mutation == "missing-approval":
        workplan["parallel_approval_status"] = "not-required"
    elif mutation == "declined-fanout-unrecorded":
        workplan["execution_mode"] = "sequential-specialists"
        workplan["parallel_approval_required"] = False
        workplan["parallel_approval_source"] = "not-required"
        workplan["parallel_approval_status"] = "parent-only"
        for bucket in buckets:
            bucket["execution_mode"] = "sequential"
    elif mutation == "aliased-path":
        buckets[1]["owned_paths"] = ["SRC/feature.py"]
    elif mutation == "ancestor-path-overlap":
        buckets[0]["owned_paths"] = ["src"]
        buckets[1]["owned_paths"] = ["src/feature.py"]
    elif mutation == "unsupported-owner":
        buckets[0]["owner"] = "invented-specialist"
    elif mutation == "parent-parallel-mode":
        buckets[0]["owner"] = "parent"
        workplan["parent_owned_groups"] = 1
        workplan["specialist_owned_groups"] = 1
    elif mutation == "one-specialist-per-finding":
        workplan["groups_total"] = 6
        workplan["specialist_owned_groups"] = 6
        workplan["verifier_groups"] = 6
        workplan["work_buckets"] = [
            {
                "bucket_id": f"B{index}",
                "selected_indexes": [index],
                "owner": "sw-engineer",
                "verifier": "qa-specialist",
                "context_pack_path": f"specialists/b{index}-context.md",
                "owned_paths": [f"src/feature_{index}.py"],
                "execution_mode": "parallel",
                "singleton_rationale": "Distinct file.",
            }
            for index in range(1, 7)
        ]

    _write_workplan(metadata, tmp_path)
    if mutation == "missing-context":
        (tmp_path / "specialists" / "b1-context.md").unlink()

    with pytest.raises(SystemExit, match=error):
        VALIDATOR._validate_code_remediate_workplan(metadata, tmp_path)


def test_parallel_approval_is_bound_to_the_approved_plan_digest(tmp_path: Path) -> None:
    """Reject an approval record copied from a different bucket proposal."""
    metadata = _parallel_metadata()
    _write_workplan(metadata, tmp_path)
    workplan = metadata["resolution_workplan"]
    assert isinstance(workplan, dict)
    workplan["approved_plan_sha256"] = "0" * 64

    with pytest.raises(SystemExit, match="code-remediate-parallel-approved-plan-not-bound"):
        VALIDATOR._validate_code_remediate_workplan(metadata, tmp_path)


def test_default_parallel_route_still_binds_exact_plan(tmp_path: Path) -> None:
    """Accept the workflow default without pretending it received plan-specific approval."""
    metadata = _parallel_metadata()
    workplan = metadata["resolution_workplan"]
    assert isinstance(workplan, dict)
    workplan["parallel_approval_source"] = "workflow-default"
    _write_completed_production_lifecycle(metadata, tmp_path)

    VALIDATOR._validate_code_remediate_workplan(metadata, tmp_path)


def test_parallel_domain_buckets_may_share_an_exact_file(tmp_path: Path) -> None:
    """Let isolated writers share one file while requiring the parent to bind final source evidence."""
    metadata = _parallel_metadata()
    workplan = metadata["resolution_workplan"]
    assert isinstance(workplan, dict)
    buckets = workplan["work_buckets"]
    assert isinstance(buckets, list)
    buckets[1]["owned_paths"] = ["src/feature.py"]
    lifecycle_path = _write_completed_production_lifecycle(metadata, tmp_path)
    lifecycle = json.loads(lifecycle_path.read_text(encoding="utf-8"))
    lifecycle["integration"]["paths"] = ["src/feature.py"]
    lifecycle["source"]["preimage_sha256"] = {"src/feature.py": "d" * 64}
    lifecycle["source"]["postimage_sha256"] = {"src/feature.py": "f" * 64}
    lifecycle["source_application"]["applied_paths"] = ["src/feature.py"]
    lifecycle_path.write_text(json.dumps(lifecycle), encoding="utf-8")
    _refresh_production_lifecycle_digest(metadata, lifecycle_path)

    VALIDATOR._validate_code_remediate_workplan(metadata, tmp_path)


@pytest.mark.parametrize(
    "mutation",
    ["valid", "record-drift", "postimage-drift", "missing-record", "failed-node-drift"],
)
def test_completed_parallel_reconciliation_binds_record_to_lifecycle(tmp_path: Path, mutation: str) -> None:
    """Reject a collision claim unless its retained record binds the joined patches and final bytes."""
    metadata = _parallel_metadata()
    lifecycle_path = _write_completed_production_lifecycle(metadata, tmp_path)
    lifecycle = json.loads(lifecycle_path.read_text(encoding="utf-8"))
    nodes = lifecycle["nodes"]
    paths = lifecycle["integration"]["paths"]
    postimages = {path: "e" * 64 for path in paths}
    record = {
        "baseline_head": lifecycle["source"]["baseline_head"],
        "failed_node": nodes[1]["node_id"],
        "integration_order": [node["node_id"] for node in nodes],
        "paths": paths,
        "patch_sha256": {node["node_id"]: node["patch_sha256"] for node in nodes},
        "postimage_sha256": postimages,
        "summary": "The parent merged both child edits in the integration worktree.",
    }
    if mutation == "record-drift":
        record["patch_sha256"][nodes[0]["node_id"]] = "0" * 64
    record_path = tmp_path / "reconciliation.json"
    if mutation != "missing-record":
        record_path.write_text(json.dumps(record), encoding="utf-8")
    lifecycle["integration_failed_node"] = nodes[1]["node_id"]
    lifecycle["integration_conflict_paths"] = [paths[-1]]
    lifecycle["integration_final_sha256"] = postimages
    lifecycle["reconciliation"] = {
        "status": "verified",
        "record_path": record_path.name,
        "record_sha256": hashlib.sha256(record_path.read_bytes()).hexdigest() if record_path.exists() else "0" * 64,
        "changed_paths": paths,
        "failed_node": nodes[1]["node_id"],
        "postimage_sha256": postimages.copy(),
    }
    if mutation == "postimage-drift":
        lifecycle["reconciliation"]["postimage_sha256"][paths[-1]] = "0" * 64
    elif mutation == "failed-node-drift":
        lifecycle["reconciliation"]["failed_node"] = nodes[0]["node_id"]
    lifecycle_path.write_text(json.dumps(lifecycle), encoding="utf-8")
    _refresh_production_lifecycle_digest(metadata, lifecycle_path)

    if mutation == "valid":
        VALIDATOR._validate_code_remediate_workplan(metadata, tmp_path)
    else:
        with pytest.raises(SystemExit, match="code-remediate-production-lifecycle-reconciliation"):
            VALIDATOR._validate_code_remediate_workplan(metadata, tmp_path)


def test_two_selected_domain_items_can_run_in_parallel(tmp_path: Path) -> None:
    """Allow two genuine role scopes without the former five-item fan-out threshold."""
    metadata = _parallel_metadata()
    metadata["resolution_scope"] = {"selected_indexes": [1, 2]}
    workplan = metadata["resolution_workplan"]
    assert isinstance(workplan, dict)
    buckets = workplan["work_buckets"]
    assert isinstance(buckets, list)
    buckets[0]["selected_indexes"] = [1]
    buckets[0]["singleton_rationale"] = "Code behavior is an independent role scope."
    buckets[1]["selected_indexes"] = [2]
    buckets[1]["singleton_rationale"] = "Documentation is an independent role scope."
    _write_completed_production_lifecycle(metadata, tmp_path)

    VALIDATOR._validate_code_remediate_workplan(metadata, tmp_path)


def test_completed_parallel_remediation_accepts_matching_production_lifecycle(tmp_path: Path) -> None:
    """Accept completion only when schema-v2 lifecycle evidence reconciles approval, apply, rollback, and cleanup."""
    metadata = _parallel_metadata()
    _write_completed_production_lifecycle(metadata, tmp_path)

    VALIDATOR._validate_code_remediate_workplan(metadata, tmp_path)


def test_completed_parallel_remediation_rejects_missing_production_lifecycle(tmp_path: Path) -> None:
    """Prevent a completed fan-out result from bypassing durable source-application evidence."""
    metadata = _parallel_metadata()
    lifecycle_path = _write_completed_production_lifecycle(metadata, tmp_path)
    lifecycle_path.unlink()

    with pytest.raises(SystemExit, match="code-remediate-production-lifecycle-evidence-missing"):
        VALIDATOR._validate_code_remediate_workplan(metadata, tmp_path)


def test_completed_parallel_remediation_rejects_lifecycle_bound_to_other_plan(tmp_path: Path) -> None:
    """Reject a truthful-looking completed lifecycle copied from a different approved bucket plan."""
    metadata = _parallel_metadata()
    lifecycle_path = _write_completed_production_lifecycle(metadata, tmp_path)
    lifecycle = json.loads(lifecycle_path.read_text(encoding="utf-8"))
    lifecycle["plan_sha256"] = "0" * 64
    lifecycle_path.write_text(json.dumps(lifecycle, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    workplan = metadata["resolution_workplan"]
    assert isinstance(workplan, dict)
    production_lifecycle = workplan["production_lifecycle"]
    assert isinstance(production_lifecycle, dict)
    production_lifecycle["sha256"] = hashlib.sha256(lifecycle_path.read_bytes()).hexdigest()

    with pytest.raises(SystemExit, match="code-remediate-production-lifecycle-plan-mismatch"):
        VALIDATOR._validate_code_remediate_workplan(metadata, tmp_path)


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        pytest.param(
            "missing-child-path",
            "code-remediate-production-lifecycle-child-patch-path-invalid",
            id="missing-child-path",
        ),
        pytest.param(
            "escaped-child-path",
            "code-remediate-production-lifecycle-child-patch-path-invalid",
            id="escaped-child-path",
        ),
        pytest.param(
            "mismatched-child-patch",
            "code-remediate-production-lifecycle-child-patch-mismatch",
            id="mismatched-child-patch",
        ),
        pytest.param(
            "missing-source-path",
            "code-remediate-production-lifecycle-source-patch-path-invalid",
            id="missing-source-path",
        ),
        pytest.param(
            "escaped-source-path",
            "code-remediate-production-lifecycle-source-patch-path-invalid",
            id="escaped-source-path",
        ),
        pytest.param(
            "mismatched-source-patch",
            "code-remediate-production-lifecycle-source-patch-mismatch",
            id="mismatched-source-patch",
        ),
        pytest.param(
            "mismatched-rollback-patch",
            "code-remediate-production-lifecycle-rollback-mismatch",
            id="mismatched-rollback-patch",
        ),
    ],
)
def test_completed_parallel_remediation_rejects_unbound_patch_evidence(
    tmp_path: Path, mutation: str, error: str
) -> None:
    """Reject absent, escaping, or rehashed child and source patch evidence."""
    metadata = _parallel_metadata()
    lifecycle_path = _write_completed_production_lifecycle(metadata, tmp_path)
    lifecycle = json.loads(lifecycle_path.read_text(encoding="utf-8"))
    nodes = lifecycle["nodes"]
    assert isinstance(nodes, list) and isinstance(nodes[0], dict)
    application = lifecycle["source_application"]
    assert isinstance(application, dict)

    if mutation == "missing-child-path":
        nodes[0].pop("patch_path")
    elif mutation == "escaped-child-path":
        nodes[0]["patch_path"] = "../outside.patch"
    elif mutation == "mismatched-child-patch":
        (tmp_path / "b1.patch").write_text("tampered\n", encoding="utf-8")
    elif mutation == "missing-source-path":
        application.pop("patch_path")
    elif mutation == "escaped-source-path":
        application["patch_path"] = "../outside.patch"
    else:
        patch_name = "rollback.patch" if mutation == "mismatched-rollback-patch" else "source-application.patch"
        (tmp_path / patch_name).write_text("tampered\n", encoding="utf-8")

    lifecycle_path.write_text(json.dumps(lifecycle, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _refresh_production_lifecycle_digest(metadata, lifecycle_path)

    with pytest.raises(SystemExit, match=error):
        VALIDATOR._validate_code_remediate_workplan(metadata, tmp_path)


@pytest.mark.skipif(not FILE_SYMLINKS_AVAILABLE, reason="file symlink capability unavailable")
@pytest.mark.parametrize(
    ("source_patch_kind", "rollback_patch_kind", "error"),
    [
        pytest.param(
            "symlink",
            "regular",
            "code-remediate-production-lifecycle-source-patch-path-invalid",
            id="source-symlink",
        ),
        pytest.param(
            "regular",
            "symlink",
            "code-remediate-production-lifecycle-rollback-path-invalid",
            id="rollback-symlink",
        ),
        pytest.param(
            "symlink",
            "symlink",
            "code-remediate-production-lifecycle-source-patch-path-invalid",
            id="source-precedes-rollback",
        ),
    ],
)
def test_completed_parallel_remediation_rejects_symlinked_source_and_rollback_patch_evidence(
    tmp_path: Path, source_patch_kind: str, rollback_patch_kind: str, error: str
) -> None:
    """Reject symlinked source evidence and preserve source-before-rollback error precedence."""
    metadata = _parallel_metadata()
    _write_completed_production_lifecycle(metadata, tmp_path)

    for patch_name, patch_kind in (
        ("source-application.patch", source_patch_kind),
        ("rollback.patch", rollback_patch_kind),
    ):
        if patch_kind == "symlink":
            patch_path = tmp_path / patch_name
            target_path = tmp_path / f"contained-{patch_name}"
            target_path.write_bytes(patch_path.read_bytes())
            patch_path.unlink()
            patch_path.symlink_to(target_path)

    with pytest.raises(SystemExit, match=error):
        VALIDATOR._validate_code_remediate_workplan(metadata, tmp_path)


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        pytest.param("context-tamper", "code-remediate-production-lifecycle-context-mismatch", id="context-tamper"),
        pytest.param(
            "state-path-drift", "code-remediate-production-lifecycle-state-path-mismatch", id="state-path-drift"
        ),
        pytest.param("gate-drift", "code-remediate-production-lifecycle-verification-gate-mismatch", id="gate-drift"),
    ],
)
def test_completed_parallel_remediation_reconciles_context_state_and_gate(
    tmp_path: Path, mutation: str, error: str
) -> None:
    """Reject completed evidence detached from context bytes, state location, or parent gate semantics."""
    metadata = _parallel_metadata()
    lifecycle_path = _write_completed_production_lifecycle(metadata, tmp_path)
    lifecycle = json.loads(lifecycle_path.read_text(encoding="utf-8"))

    if mutation == "context-tamper":
        (tmp_path / "specialists" / "b1-context.md").write_text("tampered\n", encoding="utf-8")
    elif mutation == "state-path-drift":
        lifecycle["state_path"] = ".reports/codex/code-remediate/fixture/other.json"
    else:
        lifecycle["verification_gate"] = "unverified-plan-command"
    lifecycle_path.write_text(json.dumps(lifecycle, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _refresh_production_lifecycle_digest(metadata, lifecycle_path)

    with pytest.raises(SystemExit, match=error):
        VALIDATOR._validate_code_remediate_workplan(metadata, tmp_path)

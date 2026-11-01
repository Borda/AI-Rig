"""Regression checks for bounded, approved code-remediation work buckets.

The artifact validator must accept a parallel work-bucket plan only when its sizes, ownership, approval and (for a
completed run) production lifecycle evidence are mutually consistent. Each test writes one valid plan or lifecycle to
``tmp_path`` and breaks or varies exactly one fact about it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from _modules import load_module
from _platform import FILE_SYMLINKS_AVAILABLE

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
PLAN_ERROR = "code-remediate-"
LIFECYCLE_ERROR = "code-remediate-production-lifecycle-"
DEFAULT_EVIDENCE_ROOT = ".reports/codex/code-remediate/fixture"
ROOT_INVALID = r"^code-remediate-production-lifecycle-evidence-root-invalid$"
FALLBACK_FORBIDDEN = "code-remediate-eligible-fanout-fallback-forbidden"
GOOD_REASON = "Runtime exposes one writer slot for these buckets."
#: Per fallback route: the work-plan counters and the per-bucket fields that make the route truthful.
FALLBACK_ROUTES = {
    "parent-owned": (
        {"parent_owned_groups": 2, "specialist_owned_groups": 0},
        {"owner": "parent", "execution_mode": "parent"},
    ),
    "sequential-specialists": ({}, {"execution_mode": "sequential"}),
}


def _load_validator() -> ModuleType:
    """Load the hyphenated artifact-validator module for focused contract tests."""
    return load_module("codex_rig_validate_artifacts", PLUGIN_ROOT / "shared" / "validate-artifacts.py")


VALIDATOR = _load_validator()


def _validate(metadata: dict[str, Any], out_dir: Path, **kwargs: Any) -> None:
    """Run the work-plan validator the way the artifact gate does."""
    return VALIDATOR._validate_code_remediate_workplan(metadata, out_dir, **kwargs)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write one JSON evidence file in the canonical sorted, indented form."""
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    """Return the SHA-256 digest of one file's bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_workplan(metadata: dict[str, Any], out_dir: Path) -> None:
    """Write plan, approval, context, and human-readable binding evidence."""
    workplan = metadata["resolution_workplan"]
    buckets = workplan["work_buckets"]

    (out_dir / "specialists").mkdir()
    for bucket in buckets:
        if bucket["owner"] != "parent":
            context_path = out_dir / bucket["context_pack_path"]
            context_path.write_text("# Context\n", encoding="utf-8")
            bucket["context_sha256"] = _sha256(context_path)

    plan_path = out_dir / "work-bucket-plan.json"
    _write_json(plan_path, {"schema_version": 1, "work_buckets": buckets})
    plan_sha256 = _sha256(plan_path)
    response = {"approved": "approve", "parent-only": "parent-only", "not-required": "not-required"}[
        workplan["parallel_approval_status"]
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
    _write_json(
        out_dir / "parallel-approval.json",
        {
            "plan_sha256": plan_sha256,
            "prompt_presented": workplan["parallel_prompt_presented"],
            "response": response,
            "source": workplan["parallel_approval_source"],
        },
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


def _parallel_metadata() -> dict[str, Any]:
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


def _parent_bucket(indexes: list[int]) -> dict[str, Any]:
    """Return one parent-owned bucket covering the given selected indexes."""
    return {
        "bucket_id": "B1",
        "selected_indexes": indexes,
        "owner": "parent",
        "verifier": "qa-specialist",
        "context_pack_path": "resolution-workplan.md",
        "owned_paths": ["src/feature.py"],
        "execution_mode": "parent",
    }


def _write_completed_production_lifecycle(
    metadata: dict[str, Any], out_dir: Path, evidence_root: str = DEFAULT_EVIDENCE_ROOT
) -> Path:
    """Write one truthful schema-v2 completed lifecycle projection for approved buckets."""
    _write_workplan(metadata, out_dir)
    workplan = metadata["resolution_workplan"]
    buckets = workplan["work_buckets"]
    plan_path = out_dir / "work-bucket-plan.json"
    _write_json(
        plan_path,
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
    )
    plan_sha256 = _sha256(plan_path)
    workplan["bucket_plan_sha256"] = plan_sha256
    workplan["approved_plan_sha256"] = plan_sha256
    _write_json(
        out_dir / "parallel-approval.json",
        {
            "plan_sha256": plan_sha256,
            "prompt_presented": workplan["parallel_prompt_presented"],
            "response": "approve",
            "source": workplan["parallel_approval_source"],
        },
    )
    workplan_path = out_dir / "resolution-workplan.md"
    old_digest = next(
        digest for digest in workplan_path.read_text(encoding="utf-8").split() if len(digest.removesuffix(".")) == 64
    ).removesuffix(".")
    workplan_path.write_text(
        workplan_path.read_text(encoding="utf-8").replace(old_digest, plan_sha256), encoding="utf-8"
    )
    nodes: list[dict[str, Any]] = []
    for bucket in buckets:
        patch_path = out_dir / bucket["output"]
        patch_path.write_text(f"diff --git a/{bucket['output']} b/{bucket['output']}\n", encoding="utf-8")
        nodes.append(
            {
                "node_id": bucket["bucket_id"],
                "owned_paths": bucket["owned_paths"],
                "patch_path": f"{evidence_root}/{bucket['output']}",
                "patch_sha256": _sha256(patch_path),
            }
        )
    source_patch_path = out_dir / "source-application.patch"
    source_patch_path.write_text("diff --git a/src/feature.py b/src/feature.py\n", encoding="utf-8")
    rollback_path = out_dir / "rollback.patch"
    rollback_path.write_text("diff --git a/src/feature.py b/src/feature.py\n", encoding="utf-8")
    lifecycle_path = out_dir / "production-lifecycle.json"
    _write_json(
        lifecycle_path,
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
                {"node_id": node["node_id"], "owned_paths": node["owned_paths"], "patch_sha256": node["patch_sha256"]}
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
                "patch_sha256": _sha256(source_patch_path),
                "rollback_patch_path": "rollback.patch",
                "rollback_patch_sha256": _sha256(rollback_path),
            },
            "cleanup": {"status": "removed", "force": False},
            "containment": {"mode": "parent-authoritative-worktrees", "capability_sandbox_verified": False},
        },
    )
    workplan["production_lifecycle"] = {
        "path": lifecycle_path.name,
        "sha256": _sha256(lifecycle_path),
        "status": "completed",
    }
    return lifecycle_path


def _read_lifecycle(lifecycle_path: Path) -> dict[str, Any]:
    """Load the written lifecycle projection so a test can break one fact of it."""
    return json.loads(lifecycle_path.read_text(encoding="utf-8"))


def _rewrite_lifecycle(metadata: dict[str, Any], lifecycle_path: Path, lifecycle: dict[str, Any]) -> None:
    """Persist a deliberately mutated lifecycle and rebind the fixture metadata to its new digest."""
    _write_json(lifecycle_path, lifecycle)
    metadata["resolution_workplan"]["production_lifecycle"]["sha256"] = _sha256(lifecycle_path)


def _insert_after_approval_heading(out_dir: Path, text: str) -> None:
    """Insert text directly below the `## Parallel Approval` heading of the written human-readable plan."""
    document = out_dir / "resolution-workplan.md"
    heading = "## Parallel Approval\n"
    document.write_text(document.read_text(encoding="utf-8").replace(heading, heading + text), encoding="utf-8")


def _reason(text: str) -> str:
    """Return the ineligibility-reason line, with its leading blank line, as the plan document carries it."""
    return f"\nIneligibility reason: {text}\n"


def _fallback_plan(
    route: str, *, source: str = "workflow-default", status: str = "parent-only", prompt_presented: bool = False
) -> dict[str, Any]:
    """Return a parallel-ineligible plan continuing on a parent-owned or sequential route.

    Example:
        >>> _fallback_plan("sequential-specialists")["resolution_workplan"]["parallel_eligible"]
        False
    """
    metadata = _parallel_metadata()
    workplan = metadata["resolution_workplan"]
    plan_fields, bucket_fields = FALLBACK_ROUTES[route]
    workplan.update(
        execution_mode=route,
        parallel_approval_required=False,
        parallel_approval_source=source,
        parallel_approval_status=status,
        parallel_eligible=False,
        parallel_prompt_presented=prompt_presented,
        **plan_fields,
    )
    for bucket in workplan["work_buckets"]:
        bucket.update(bucket_fields)
    return metadata


def _user_approved_fallback() -> dict[str, Any]:
    """Return a two-item, single parent bucket plan whose serial route the user explicitly approved."""
    metadata = _parallel_metadata()
    metadata["resolution_scope"] = {"selected_indexes": [1, 2]}
    metadata["resolution_workplan"].update(
        execution_mode="parent-owned",
        groups_total=1,
        parent_owned_groups=1,
        specialist_owned_groups=0,
        verifier_groups=1,
        parallel_eligible=False,
        parallel_approval_source="explicit-input",
        parallel_approval_status="parent-only",
        work_buckets=[_parent_bucket([1, 2])],
    )
    return metadata


def _low_volume_parent_plan() -> dict[str, Any]:
    """Return the historical low-volume plan: two items in one parent-owned bucket, no approval needed."""
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
        "work_buckets": [_parent_bucket([1, 2])],
        "workplan_path": "resolution-workplan.md",
    }
    return metadata


def _buckets(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the work buckets of a plan."""
    return metadata["resolution_workplan"]["work_buckets"]


def _singleton_buckets(metadata: dict[str, Any], rationales: tuple[str, str]) -> None:
    """Narrow a two-bucket plan to one selected item per bucket, each with its singleton rationale."""
    metadata["resolution_scope"] = {"selected_indexes": [1, 2]}
    first, second = _buckets(metadata)
    first.update(selected_indexes=[1], singleton_rationale=rationales[0])
    second.update(selected_indexes=[2], singleton_rationale=rationales[1])


@pytest.fixture
def parallel_metadata() -> dict[str, Any]:
    """Return a fresh valid two-bucket parallel plan each test may edit freely."""
    return _parallel_metadata()


@pytest.fixture
def lifecycle_path(parallel_metadata: dict[str, Any], tmp_path: Path) -> Path:
    """Write a completed, truthful production lifecycle for the default plan and return its path."""
    return _write_completed_production_lifecycle(parallel_metadata, tmp_path)


# Mutators for invalid plans: each breaks exactly one rule of the valid six-item, two-bucket plan.
def _too_large(metadata: dict[str, Any]) -> None:
    """Collapse all six items into one oversized bucket."""
    buckets = _buckets(metadata)
    buckets[0]["selected_indexes"] = [1, 2, 3, 4, 5, 6]
    buckets.pop()
    metadata["resolution_workplan"].update(groups_total=1, specialist_owned_groups=1, verifier_groups=1)


def _duplicate_index(metadata: dict[str, Any]) -> None:
    """Assign item 3 to two buckets."""
    _buckets(metadata)[1]["selected_indexes"] = [3, 4, 5, 6]


def _low_volume_fanout(metadata: dict[str, Any]) -> None:
    """Fan two selected items out to specialists, which the low-volume rule forbids."""
    _singleton_buckets(metadata, ("Distinct runtime boundary.", "Distinct documentation boundary."))


def _missing_approval(metadata: dict[str, Any]) -> None:
    """Mark the required fan-out approval as not required."""
    metadata["resolution_workplan"]["parallel_approval_status"] = "not-required"


def _declined_fanout_unrecorded(metadata: dict[str, Any]) -> None:
    """Continue sequentially on an eligible plan without recording why fan-out was declined."""
    metadata["resolution_workplan"].update(
        execution_mode="sequential-specialists",
        parallel_approval_required=False,
        parallel_approval_source="not-required",
        parallel_approval_status="parent-only",
    )
    for bucket in _buckets(metadata):
        bucket["execution_mode"] = "sequential"


def _aliased_path(metadata: dict[str, Any]) -> None:
    """Own the same file under a differently cased path in two buckets."""
    _buckets(metadata)[1]["owned_paths"] = ["SRC/feature.py"]


def _ancestor_path_overlap(metadata: dict[str, Any]) -> None:
    """Let one bucket own a directory that contains another bucket's file."""
    first, second = _buckets(metadata)
    first["owned_paths"] = ["src"]
    second["owned_paths"] = ["src/feature.py"]


def _unsupported_owner(metadata: dict[str, Any]) -> None:
    """Assign a bucket to an owner outside the supported specialist roster."""
    _buckets(metadata)[0]["owner"] = "invented-specialist"


def _parent_parallel_mode(metadata: dict[str, Any]) -> None:
    """Mix a parent-owned bucket into a parallel fan-out."""
    _buckets(metadata)[0]["owner"] = "parent"
    metadata["resolution_workplan"].update(parent_owned_groups=1, specialist_owned_groups=1)


def _one_specialist_per_finding(metadata: dict[str, Any]) -> None:
    """Give each of six findings its own specialist bucket."""
    workplan = metadata["resolution_workplan"]
    workplan.update(groups_total=6, specialist_owned_groups=6, verifier_groups=6)
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


class TestWorkBucketPlan:
    """A work-bucket plan is accepted only when its sizes, ownership and approval are consistent."""

    def test_planning_only_approval_requires_runtime_lifecycle(
        self, parallel_metadata: dict[str, Any], tmp_path: Path
    ) -> None:
        """A schema-v1 approved bucket proposal cannot masquerade as completed production execution."""
        _write_workplan(parallel_metadata, tmp_path)

        with pytest.raises(SystemExit, match=f"{LIFECYCLE_ERROR}required"):
            _validate(parallel_metadata, tmp_path)

    @pytest.mark.parametrize(
        ("mutate", "error"),
        [
            pytest.param(_too_large, f"{PLAN_ERROR}work-bucket-too-large", id="too-large"),
            pytest.param(_duplicate_index, f"{PLAN_ERROR}work-bucket-coverage-mismatch", id="duplicate-index"),
            pytest.param(_low_volume_fanout, f"{LIFECYCLE_ERROR}required", id="low-volume-fanout"),
            pytest.param(_missing_approval, f"{PLAN_ERROR}parallel-approval-missing", id="missing-approval"),
            pytest.param(
                _declined_fanout_unrecorded,
                f"{PLAN_ERROR}eligible-fanout-approval-not-recorded",
                id="declined-fanout-unrecorded",
            ),
            pytest.param(_aliased_path, f"{PLAN_ERROR}parallel-ownership-overlap", id="aliased-path"),
            pytest.param(_ancestor_path_overlap, f"{PLAN_ERROR}parallel-ownership-overlap", id="ancestor-path-overlap"),
            pytest.param(_unsupported_owner, f"{PLAN_ERROR}work-bucket-owner-unsupported", id="unsupported-owner"),
            pytest.param(
                _parent_parallel_mode, f"{PLAN_ERROR}parent-work-bucket-mode-invalid", id="parent-parallel-mode"
            ),
            pytest.param(
                _one_specialist_per_finding, f"{PLAN_ERROR}one-specialist-per-finding", id="one-specialist-per-finding"
            ),
        ],
    )
    def test_invalid_plans_fail_closed(
        self, parallel_metadata: dict[str, Any], tmp_path: Path, mutate: Callable[[dict[str, Any]], None], error: str
    ) -> None:
        """Plans that create excess fan-out, overlap, or unapproved parallel work are rejected.

        Each mutator breaks exactly one rule of an otherwise valid plan, so the named error identifies the rule.
        """
        mutate(parallel_metadata)
        _write_workplan(parallel_metadata, tmp_path)

        with pytest.raises(SystemExit, match=error):
            _validate(parallel_metadata, tmp_path)

    def test_missing_context_pack_fails_closed(self, parallel_metadata: dict[str, Any], tmp_path: Path) -> None:
        """A specialist bucket whose context pack file vanished after planning is rejected."""
        _write_workplan(parallel_metadata, tmp_path)
        (tmp_path / "specialists" / "b1-context.md").unlink()

        with pytest.raises(SystemExit, match=f"{PLAN_ERROR}specialist-context-pack-missing"):
            _validate(parallel_metadata, tmp_path)

    def test_approval_is_bound_to_the_approved_plan_digest(
        self, parallel_metadata: dict[str, Any], tmp_path: Path
    ) -> None:
        """An approval record copied from a different bucket proposal is rejected."""
        _write_workplan(parallel_metadata, tmp_path)
        parallel_metadata["resolution_workplan"]["approved_plan_sha256"] = "0" * 64

        with pytest.raises(SystemExit, match=f"{PLAN_ERROR}parallel-approved-plan-not-bound"):
            _validate(parallel_metadata, tmp_path)

    def test_low_volume_selection_stays_in_one_agent_scope(self, tmp_path: Path) -> None:
        """One coherent parent bucket stays valid for historical low-volume work under the legacy contract."""
        metadata = _low_volume_parent_plan()
        _write_workplan(metadata, tmp_path)

        assert _validate(metadata, tmp_path, current_contract=False) is None


class TestSerialFallback:
    """A parent-owned or sequential route is valid only for a plan that is truthfully parallel-ineligible."""

    def test_user_approved_fallback_is_accepted_when_ineligible(self, tmp_path: Path) -> None:
        """A fallback the user approved is accepted when the recorded plan is actually parallel-ineligible."""
        metadata = _user_approved_fallback()
        _write_workplan(metadata, tmp_path)

        assert _validate(metadata, tmp_path) is None

    @pytest.mark.parametrize("route", ["parent-owned", "sequential-specialists"])
    def test_workflow_default_continuation_is_accepted_when_ineligible(self, tmp_path: Path, route: str) -> None:
        """A parent or sequential route with a concrete reason is permitted when parallel writing is infeasible."""
        metadata = _fallback_plan(route)
        _write_workplan(metadata, tmp_path)
        _insert_after_approval_heading(tmp_path, _reason(GOOD_REASON))

        assert _validate(metadata, tmp_path) is None

    @pytest.mark.parametrize(
        ("build", "reason_text"),
        [
            pytest.param(_user_approved_fallback, "", id="user-approved"),
            pytest.param(
                lambda: _fallback_plan("parent-owned"), _reason(GOOD_REASON), id="workflow-default-parent-owned"
            ),
            pytest.param(
                lambda: _fallback_plan("sequential-specialists"), _reason(GOOD_REASON), id="workflow-default-sequential"
            ),
        ],
    )
    def test_eligible_plan_refuses_fallback(
        self, tmp_path: Path, build: Callable[[], dict[str, Any]], reason_text: str
    ) -> None:
        """A fallback is refused once the recorded plan is parallel-eligible after all.

        A workflow-default plan carries a concrete reason line, so only the eligibility flag, not a missing reason,
        can trigger the error; a user-approved plan needs no reason line.
        """
        metadata = build()
        metadata["resolution_workplan"]["parallel_eligible"] = True
        _write_workplan(metadata, tmp_path)
        _insert_after_approval_heading(tmp_path, reason_text)

        with pytest.raises(SystemExit, match=FALLBACK_FORBIDDEN):
            _validate(metadata, tmp_path)

    @pytest.mark.parametrize(
        "reason_text",
        [
            pytest.param("", id="no-reason-line"),
            pytest.param(_reason(""), id="empty"),
            pytest.param(_reason("   "), id="blank"),
            pytest.param(_reason("none"), id="none-placeholder"),
            pytest.param(_reason("TBD"), id="tbd-placeholder"),
            pytest.param(_reason("<concrete reason>"), id="template-placeholder"),
        ],
    )
    def test_default_fallback_requires_concrete_ineligibility_reason(self, tmp_path: Path, reason_text: str) -> None:
        """A reasonless or placeholder serial choice for otherwise independent buckets is rejected."""
        metadata = _fallback_plan("sequential-specialists")
        _write_workplan(metadata, tmp_path)
        _insert_after_approval_heading(tmp_path, reason_text)

        with pytest.raises(SystemExit, match=f"{PLAN_ERROR}ineligibility-reason-missing"):
            _validate(metadata, tmp_path)

    @pytest.mark.parametrize("route", ["parent-owned", "sequential-specialists"])
    def test_current_contract_rejects_historical_not_required_route(self, tmp_path: Path, route: str) -> None:
        """New selected-scope work cannot avoid its reason through legacy dispatch fields."""
        metadata = _fallback_plan(route, source="not-required", status="not-required")
        _write_workplan(metadata, tmp_path)

        with pytest.raises(SystemExit, match=f"{PLAN_ERROR}current-fallback-dispatch-required"):
            _validate(metadata, tmp_path)

    @pytest.mark.parametrize("route", ["parent-owned", "sequential-specialists"])
    def test_legacy_contract_accepts_historical_not_required_route(self, tmp_path: Path, route: str) -> None:
        """The same legacy dispatch fields stay valid when a historical result is read under the legacy contract."""
        metadata = _fallback_plan(route, source="not-required", status="not-required")
        _write_workplan(metadata, tmp_path)

        assert _validate(metadata, tmp_path, current_contract=False) is None

    def test_workflow_default_rejects_presented_prompt(self, tmp_path: Path) -> None:
        """An actual user prompt stays distinct from a workflow-owned continuation."""
        metadata = _fallback_plan("sequential-specialists", prompt_presented=True)
        _write_workplan(metadata, tmp_path)

        with pytest.raises(SystemExit, match=f"{PLAN_ERROR}default-fallback-prompt-invalid"):
            _validate(metadata, tmp_path)


def _share_one_file(lifecycle: dict[str, Any]) -> None:
    """Make both buckets' work land in the single file `src/feature.py`."""
    lifecycle["integration"]["paths"] = ["src/feature.py"]
    lifecycle["source"]["preimage_sha256"] = {"src/feature.py": "d" * 64}
    lifecycle["source"]["postimage_sha256"] = {"src/feature.py": "f" * 64}
    lifecycle["source_application"]["applied_paths"] = ["src/feature.py"]


def _workflow_default_source(metadata: dict[str, Any]) -> None:
    """Record the workflow default, not an explicit user answer, as the approval source."""
    metadata["resolution_workplan"]["parallel_approval_source"] = "workflow-default"


def _two_domain_items(metadata: dict[str, Any]) -> None:
    """Select two items that each form a genuine singleton role scope."""
    _singleton_buckets(
        metadata, ("Code behavior is an independent role scope.", "Documentation is an independent role scope.")
    )


def _unchanged(metadata: dict[str, Any]) -> None:
    """Leave the default plan untouched."""


class TestCompletedLifecycle:
    """A completed parallel remediation needs schema-v2 lifecycle evidence that reconciles with its approved plan."""

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(_unchanged, id="matching-lifecycle"),
            pytest.param(_workflow_default_source, id="workflow-default-binds-exact-plan"),
            pytest.param(_two_domain_items, id="two-domain-items-in-parallel"),
        ],
    )
    def test_matching_lifecycle_is_accepted(
        self, parallel_metadata: dict[str, Any], tmp_path: Path, mutate: Callable[[dict[str, Any]], None]
    ) -> None:
        """Completion is accepted when lifecycle evidence reconciles approval, apply, rollback, and cleanup.

        The variants cover the plain plan, a workflow-default approval that must still bind the exact plan digest
        without claiming plan-specific approval, and two genuine role scopes below the former five-item threshold.
        """
        mutate(parallel_metadata)
        _write_completed_production_lifecycle(parallel_metadata, tmp_path)

        assert _validate(parallel_metadata, tmp_path) is None

    def test_domain_buckets_may_share_an_exact_file(self, parallel_metadata: dict[str, Any], tmp_path: Path) -> None:
        """Isolated writers may share one file while the parent binds the final source evidence."""
        _buckets(parallel_metadata)[1]["owned_paths"] = ["src/feature.py"]
        lifecycle_path = _write_completed_production_lifecycle(parallel_metadata, tmp_path)
        lifecycle = _read_lifecycle(lifecycle_path)
        _share_one_file(lifecycle)
        _rewrite_lifecycle(parallel_metadata, lifecycle_path, lifecycle)

        assert _validate(parallel_metadata, tmp_path) is None

    def test_missing_lifecycle_file_is_rejected(
        self, parallel_metadata: dict[str, Any], lifecycle_path: Path, tmp_path: Path
    ) -> None:
        """A completed fan-out result cannot bypass durable source-application evidence."""
        lifecycle_path.unlink()

        with pytest.raises(SystemExit, match=f"{LIFECYCLE_ERROR}evidence-missing"):
            _validate(parallel_metadata, tmp_path)

    @pytest.mark.parametrize(
        "evidence_root",
        [
            ".reports/codex/code-remediate/pr-0/run-001",
            ".reports/codex/code-remediate/pr-01/run-001",
            ".reports/codex/code-remediate/pr-41/run-000",
            ".reports/codex/code-remediate/pr-41/run-1",
            ".reports/codex/code-remediate/other/run-001",
            ".reports/codex/code-remediate/pr-41/run-001/extra",
            ".reports/codex/code-remediate/pr-41/../run-001",
            ".reports/codex/code-remediate/pr-41//run-001",
            "/.reports/codex/code-remediate/pr-41/run-001",
            "C:/.reports/codex/code-remediate/pr-41/run-001",
            ".reports\\codex\\code-remediate\\pr-41\\run-001",
        ],
    )
    def test_invalid_promoted_root_is_rejected(
        self, parallel_metadata: dict[str, Any], tmp_path: Path, evidence_root: str
    ) -> None:
        """Malformed identities and unsafe paths are rejected even when their receipt digest matches."""
        _write_completed_production_lifecycle(parallel_metadata, tmp_path, evidence_root)

        with pytest.raises(SystemExit, match=ROOT_INVALID):
            _validate(parallel_metadata, tmp_path)

    def test_foreign_promoted_root_is_rejected(self, parallel_metadata: dict[str, Any], tmp_path: Path) -> None:
        """Another canonical run's identity cannot certify the current run."""
        out_dir = tmp_path / ".reports/codex/code-remediate/pr-41/run-001"
        out_dir.mkdir(parents=True)
        _write_completed_production_lifecycle(parallel_metadata, out_dir, ".reports/codex/code-remediate/pr-42/run-001")

        with pytest.raises(SystemExit, match=ROOT_INVALID):
            _validate(parallel_metadata, out_dir)


# Mutators for detached lifecycle evidence: each receives the parsed lifecycle and the run directory.
def _write_tampered(out_dir: Path, name: str) -> None:
    """Overwrite one evidence file so its bytes no longer match the recorded digest."""
    (out_dir / name).write_text("tampered\n", encoding="utf-8")


def _other_plan(lifecycle: dict[str, Any], out_dir: Path) -> None:
    """Bind the lifecycle to a different approved bucket plan."""
    lifecycle["plan_sha256"] = "0" * 64


def _missing_child_path(lifecycle: dict[str, Any], out_dir: Path) -> None:
    """Drop the first child patch path."""
    lifecycle["nodes"][0].pop("patch_path")


def _escaped_child_path(lifecycle: dict[str, Any], out_dir: Path) -> None:
    """Point the first child patch outside the evidence root."""
    lifecycle["nodes"][0]["patch_path"] = "../outside.patch"


def _mismatched_child_patch(lifecycle: dict[str, Any], out_dir: Path) -> None:
    """Rewrite the first child patch after its digest was recorded."""
    _write_tampered(out_dir, "b1.patch")


def _missing_source_path(lifecycle: dict[str, Any], out_dir: Path) -> None:
    """Drop the source-application patch path."""
    lifecycle["source_application"].pop("patch_path")


def _escaped_source_path(lifecycle: dict[str, Any], out_dir: Path) -> None:
    """Point the source-application patch outside the evidence root."""
    lifecycle["source_application"]["patch_path"] = "../outside.patch"


def _mismatched_source_patch(lifecycle: dict[str, Any], out_dir: Path) -> None:
    """Rewrite the source-application patch after its digest was recorded."""
    _write_tampered(out_dir, "source-application.patch")


def _mismatched_rollback_patch(lifecycle: dict[str, Any], out_dir: Path) -> None:
    """Rewrite the rollback patch after its digest was recorded."""
    _write_tampered(out_dir, "rollback.patch")


def _context_tamper(lifecycle: dict[str, Any], out_dir: Path) -> None:
    """Rewrite a specialist context pack after the plan was approved."""
    _write_tampered(out_dir / "specialists", "b1-context.md")


def _state_path_drift(lifecycle: dict[str, Any], out_dir: Path) -> None:
    """Move the recorded state path away from the canonical evidence location."""
    lifecycle["state_path"] = f"{DEFAULT_EVIDENCE_ROOT}/other.json"


def _gate_drift(lifecycle: dict[str, Any], out_dir: Path) -> None:
    """Replace the shared quality gate with an unverified plan command."""
    lifecycle["verification_gate"] = "unverified-plan-command"


def _replace_with_symlinks(out_dir: Path, *names: str) -> None:
    """Replace each named evidence file with a symlink to a contained copy of itself."""
    for name in names:
        path = out_dir / name
        target = out_dir / f"contained-{name}"
        target.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(target)


class TestLifecycleEvidenceBinding:
    """Lifecycle evidence detached from its plan, patches, context, state location or gate is rejected."""

    @pytest.mark.parametrize(
        ("mutate", "error"),
        [
            pytest.param(_other_plan, f"{LIFECYCLE_ERROR}plan-mismatch", id="other-plan"),
            pytest.param(_missing_child_path, f"{LIFECYCLE_ERROR}child-patch-path-invalid", id="missing-child-path"),
            pytest.param(_escaped_child_path, f"{LIFECYCLE_ERROR}child-patch-path-invalid", id="escaped-child-path"),
            pytest.param(
                _mismatched_child_patch, f"{LIFECYCLE_ERROR}child-patch-mismatch", id="mismatched-child-patch"
            ),
            pytest.param(_missing_source_path, f"{LIFECYCLE_ERROR}source-patch-path-invalid", id="missing-source-path"),
            pytest.param(_escaped_source_path, f"{LIFECYCLE_ERROR}source-patch-path-invalid", id="escaped-source-path"),
            pytest.param(
                _mismatched_source_patch, f"{LIFECYCLE_ERROR}source-patch-mismatch", id="mismatched-source-patch"
            ),
            pytest.param(
                _mismatched_rollback_patch, f"{LIFECYCLE_ERROR}rollback-mismatch", id="mismatched-rollback-patch"
            ),
            pytest.param(_context_tamper, f"{LIFECYCLE_ERROR}context-mismatch", id="context-tamper"),
            pytest.param(_state_path_drift, f"{LIFECYCLE_ERROR}state-path-mismatch", id="state-path-drift"),
            pytest.param(_gate_drift, f"{LIFECYCLE_ERROR}verification-gate-mismatch", id="gate-drift"),
        ],
    )
    def test_detached_evidence_is_rejected(
        self,
        parallel_metadata: dict[str, Any],
        lifecycle_path: Path,
        tmp_path: Path,
        mutate: Callable[[dict[str, Any], Path], None],
        error: str,
    ) -> None:
        """Absent, escaping, rehashed or re-bound lifecycle evidence is rejected with its named error.

        A truthful completed lifecycle is written, then one fact is broken: a copied plan digest, a missing or
        escaping child or source patch path, rewritten patch or context bytes, a drifted state path, or a drifted gate.
        """
        lifecycle = _read_lifecycle(lifecycle_path)
        mutate(lifecycle, tmp_path)
        _rewrite_lifecycle(parallel_metadata, lifecycle_path, lifecycle)

        with pytest.raises(SystemExit, match=error):
            _validate(parallel_metadata, tmp_path)

    @pytest.mark.skipif(not FILE_SYMLINKS_AVAILABLE, reason="file symlink capability unavailable")
    @pytest.mark.parametrize(
        ("symlinked", "error"),
        [
            pytest.param(
                ("source-application.patch",), f"{LIFECYCLE_ERROR}source-patch-path-invalid", id="source-symlink"
            ),
            pytest.param(("rollback.patch",), f"{LIFECYCLE_ERROR}rollback-path-invalid", id="rollback-symlink"),
            pytest.param(
                ("source-application.patch", "rollback.patch"),
                f"{LIFECYCLE_ERROR}source-patch-path-invalid",
                id="source-precedes-rollback",
            ),
        ],
    )
    def test_symlinked_source_or_rollback_patch_is_rejected(
        self,
        parallel_metadata: dict[str, Any],
        lifecycle_path: Path,
        tmp_path: Path,
        symlinked: tuple[str, ...],
        error: str,
    ) -> None:
        """Symlinked source evidence is rejected, with the source error taking precedence over the rollback error."""
        _replace_with_symlinks(tmp_path, *symlinked)

        with pytest.raises(SystemExit, match=error):
            _validate(parallel_metadata, tmp_path)


def _record_drift(record: dict[str, Any], claim: dict[str, Any]) -> None:
    """Make the retained record disagree with the joined patch digest of the first node."""
    record["patch_sha256"]["B1"] = "0" * 64


def _postimage_drift(record: dict[str, Any], claim: dict[str, Any]) -> None:
    """Make the claimed final bytes of the last changed path differ from the record."""
    claim["postimage_sha256"][claim["changed_paths"][-1]] = "0" * 64


def _failed_node_drift(record: dict[str, Any], claim: dict[str, Any]) -> None:
    """Make the claim name a different failed node than the record."""
    claim["failed_node"] = "B1"


def _reconciliation_parts(
    metadata: dict[str, Any], out_dir: Path
) -> tuple[Path, dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Write a completed lifecycle and return it with a collision-reconciliation record and claim for B2 failing."""
    lifecycle_path = _write_completed_production_lifecycle(metadata, out_dir)
    lifecycle = _read_lifecycle(lifecycle_path)
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
    claim = {
        "status": "verified",
        "record_path": "reconciliation.json",
        "changed_paths": paths,
        "failed_node": nodes[1]["node_id"],
        "postimage_sha256": postimages.copy(),
    }
    lifecycle |= {
        "integration_failed_node": nodes[1]["node_id"],
        "integration_conflict_paths": [paths[-1]],
        "integration_final_sha256": postimages,
    }
    return lifecycle_path, lifecycle, record, claim


class TestReconciliationRecord:
    """A collision claim is accepted only when its retained record binds the joined patches and final bytes."""

    def test_matching_record_is_accepted(self, parallel_metadata: dict[str, Any], tmp_path: Path) -> None:
        """A retained record that matches the lifecycle's patches, order and final bytes is accepted."""
        lifecycle_path, lifecycle, record, claim = _reconciliation_parts(parallel_metadata, tmp_path)
        record_path = tmp_path / "reconciliation.json"
        record_path.write_text(json.dumps(record), encoding="utf-8")
        lifecycle["reconciliation"] = claim | {"record_sha256": _sha256(record_path)}
        _rewrite_lifecycle(parallel_metadata, lifecycle_path, lifecycle)

        assert _validate(parallel_metadata, tmp_path) is None

    @pytest.mark.parametrize(
        "tamper",
        [
            pytest.param(_record_drift, id="record-drift"),
            pytest.param(_postimage_drift, id="postimage-drift"),
            pytest.param(_failed_node_drift, id="failed-node-drift"),
        ],
    )
    def test_drifted_record_or_claim_is_rejected(
        self,
        parallel_metadata: dict[str, Any],
        tmp_path: Path,
        tamper: Callable[[dict[str, Any], dict[str, Any]], None],
    ) -> None:
        """A collision claim whose record or claimed bytes drift from the lifecycle is rejected.

        The record file's own digest stays consistent with the claim, so only the cross-check against the lifecycle's
        joined patch digests, final bytes and failed node can notice the drift.
        """
        lifecycle_path, lifecycle, record, claim = _reconciliation_parts(parallel_metadata, tmp_path)
        tamper(record, claim)
        record_path = tmp_path / "reconciliation.json"
        record_path.write_text(json.dumps(record), encoding="utf-8")
        lifecycle["reconciliation"] = claim | {"record_sha256": _sha256(record_path)}
        _rewrite_lifecycle(parallel_metadata, lifecycle_path, lifecycle)

        with pytest.raises(SystemExit, match=f"{LIFECYCLE_ERROR}reconciliation"):
            _validate(parallel_metadata, tmp_path)

    def test_missing_record_is_rejected(self, parallel_metadata: dict[str, Any], tmp_path: Path) -> None:
        """A collision claim that points at a record file which was never retained is rejected."""
        lifecycle_path, lifecycle, _, claim = _reconciliation_parts(parallel_metadata, tmp_path)
        lifecycle["reconciliation"] = claim | {"record_sha256": "0" * 64}
        _rewrite_lifecycle(parallel_metadata, lifecycle_path, lifecycle)

        with pytest.raises(SystemExit, match=f"{LIFECYCLE_ERROR}reconciliation"):
            _validate(parallel_metadata, tmp_path)

"""Acceptance checks for generated-fixture parallel worktree lifecycle evidence."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import tempfile
from pathlib import Path
from types import ModuleType

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
LIFECYCLE_PATH = PLUGIN_ROOT / "shared" / "parallel_worktrees.py"
VALIDATOR_PATH = PLUGIN_ROOT / "shared" / "validate-artifacts.py"


def _supports_symlinks() -> bool:
    """Return whether this test host can create directory symlinks."""
    if not hasattr(os, "symlink"):
        return False
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        target = root / "target"
        target.mkdir()
        try:
            (root / "alias").symlink_to(target, target_is_directory=True)
        except OSError:
            return False
    return True


SYMLINKS_SUPPORTED = _supports_symlinks()


def _load_lifecycle() -> ModuleType:
    """Load the installed-package-safe lifecycle module by file path."""
    specification = importlib.util.spec_from_file_location("codex_rig_parallel_worktrees", LIFECYCLE_PATH)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _load_validator() -> ModuleType:
    """Load the artifact validator for producer-to-consumer lifecycle checks."""
    specification = importlib.util.spec_from_file_location("codex_rig_validate_artifacts", VALIDATOR_PATH)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _git(repository: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run one local Git fixture command with stable captured output."""
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=check,
        text=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _sha256(path: Path) -> str:
    """Return the SHA-256 of exact file bytes.

    Example:
        >>> len(_sha256(PLUGIN_ROOT / "package-manifest.json"))
        64
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _child_handover(workspace: Path, node: dict[str, object]) -> dict[str, object]:
    """Build the five-field child result from the exact detached worktree diff."""
    worktree = workspace / str(node["worktree_path"])
    owned_paths = [str(path) for path in node["owned_paths"]]
    patch = subprocess.run(
        ["git", "-C", str(worktree), "diff", "--binary", "--full-index", "HEAD", "--", *owned_paths],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout
    return {
        "node_id": node["node_id"],
        "status": "completed",
        "summary": f"Completed {node['node_id']}.",
        "changed_paths": owned_paths,
        "patch_sha256": hashlib.sha256(patch).hexdigest(),
    }


def _fixture_repository(workspace: Path) -> Path:
    """Create a committed generated repository with two tracked write buckets."""
    repository = workspace / ".reports" / "codex" / "develop" / "fixture" / "repository"
    repository.mkdir(parents=True)
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "Codex Test")
    _git(repository, "config", "user.email", "codex-test@example.invalid")
    (repository / ".gitignore").write_text(".reports/\n", encoding="utf-8", newline="\n")
    (repository / "bucket-a.txt").write_text("baseline-a\n", encoding="utf-8", newline="\n")
    (repository / "bucket-b.txt").write_text("baseline-b\n", encoding="utf-8", newline="\n")
    _git(repository, "add", ".gitignore", "bucket-a.txt", "bucket-b.txt")
    _git(
        repository,
        "commit",
        "-q",
        "-m",
        "fixture baseline\n\nCo-authored-by: Codex <codex@openai.com>",
    )
    return repository


def _approved_plan(workspace: Path) -> tuple[Path, Path, Path]:
    """Write one exact approved two-node generated-fixture plan."""
    repository = _fixture_repository(workspace)
    plan_path = workspace / "fixture-plan.json"
    approval_path = workspace / "approval.json"
    plan = {
        "schema_version": 1,
        "plan_id": "generated-fixture",
        "requested_authority": {
            "serial_repository_edits": True,
            "one_local_parallel_write_pilot": True,
            "pilot_scope": "generated-fixture-repository-only",
            "network": "denied-by-plan",
            "external_or_paid_parent_process": False,
            "remote_mutation": False,
            "user_data_deletion": False,
            "automatic_retry_count": 0,
            "general_parallel_write_enablement": False,
        },
        "stages": [
            {
                "stage_id": "S3",
                "mode": "parallel-write",
                "configured_limit": 2,
                "fixture_repository": repository.relative_to(workspace).as_posix(),
                "worktree_root": (workspace / ".reports" / "codex" / "develop" / "fixture" / "worktrees")
                .relative_to(workspace)
                .as_posix(),
                "nodes": [
                    {
                        "node_id": "FIXTURE-WRITE-A",
                        "owned_paths": ["bucket-a.txt"],
                        "resource_locks": [],
                        "output": "patch-a.diff",
                    },
                    {
                        "node_id": "FIXTURE-WRITE-B",
                        "owned_paths": ["bucket-b.txt"],
                        "resource_locks": [],
                        "output": "patch-b.diff",
                    },
                ],
            }
        ],
        "status": "frozen-awaiting-explicit-approval",
    }
    plan_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8", newline="\n")
    approval_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "plan_sha256": _sha256(plan_path),
                "response": "approve",
                "source": "user-prompt",
                "scope": {
                    "serial_repository_edits": True,
                    "one_local_parallel_write_pilot": True,
                    "pilot_scope": "generated-fixture-repository-only",
                    "network": False,
                    "external_or_paid_parent_process": False,
                    "remote_mutation": False,
                    "user_data_deletion": False,
                    "automatic_retry_count": 0,
                    "general_parallel_write_enablement": False,
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return plan_path, approval_path, repository


def _approved_code_remediate_plan(workspace: Path, *, pin_lf: bool = False) -> tuple[Path, Path, Path]:
    """Write one lifecycle plan, optionally pinning tracked text to LF."""
    repository = workspace / "authoritative-repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "Codex Test")
    _git(repository, "config", "user.email", "codex-test@example.invalid")
    text_attributes = "*.txt text eol=lf\n" if pin_lf else "*.txt text\n"
    (repository / ".gitattributes").write_text(text_attributes, encoding="utf-8", newline="\n")
    (repository / "bucket-a.txt").write_text("baseline-a\n", encoding="utf-8", newline="\n")
    (repository / "bucket-b.txt").write_text("baseline-b\n", encoding="utf-8", newline="\n")
    _git(repository, "add", ".gitattributes", "bucket-a.txt", "bucket-b.txt")
    _git(
        repository,
        "commit",
        "-q",
        "-m",
        "production fixture baseline\n\nCo-authored-by: Codex <codex@openai.com>",
    )
    evidence = repository / ".reports" / "codex" / "code-remediate" / "fixture"
    evidence.mkdir(parents=True)
    plan_path = evidence / "work-bucket-plan.json"
    approval_path = evidence / "parallel-approval.json"
    context_paths = []
    for bucket_id, content in (("WRITE-A", "Context A\n"), ("WRITE-B", "Context B\n")):
        context_path = evidence / f"{bucket_id}-context.md"
        context_path.write_text(content, encoding="utf-8", newline="\n")
        context_paths.append(context_path)
    plan = {
        "schema_version": 2,
        "consumer": "code-remediate",
        "write_parallel_promoted": False,
        "source_repository": "authoritative-repository",
        "worktree_root": ".codex-rig-worktrees/fixture",
        "baseline_head": _git(repository, "rev-parse", "HEAD").stdout.strip(),
        "baseline_tree": _git(repository, "rev-parse", "HEAD^{tree}").stdout.strip(),
        "rollback_policy": "approved-paths-if-preapply-baseline-matches",
        "cleanup_policy": "non-force-after-durable-source-application",
        "state_path": "production-lifecycle.json",
        "verification_gate": "code-remediate-shared-quality-gates",
        "work_buckets": [
            {
                "bucket_id": "WRITE-A",
                "selected_indexes": [1, 2, 3],
                "owner": "sw-engineer",
                "verifier": "qa-specialist",
                "owned_paths": ["bucket-a.txt"],
                "resource_locks": [],
                "context_pack_path": context_paths[0].relative_to(evidence).as_posix(),
                "context_sha256": _sha256(context_paths[0]),
                "output": "patch-a.diff",
                "execution_mode": "parallel",
            },
            {
                "bucket_id": "WRITE-B",
                "selected_indexes": [4, 5, 6],
                "owner": "doc-scribe",
                "verifier": "parent",
                "owned_paths": ["bucket-b.txt"],
                "resource_locks": [],
                "context_pack_path": context_paths[1].relative_to(evidence).as_posix(),
                "context_sha256": _sha256(context_paths[1]),
                "output": "patch-b.diff",
                "execution_mode": "parallel",
            },
        ],
        "status": "frozen-awaiting-explicit-approval",
    }
    plan_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8", newline="\n")
    approval_path.write_text(
        json.dumps(
            {
                "plan_sha256": _sha256(plan_path),
                "prompt_presented": True,
                "response": "approve",
                "source": "user-prompt",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return plan_path, approval_path, repository


def _rewrite_code_remediate_plan(plan_path: Path, approval_path: Path, plan: dict[str, object]) -> None:
    """Rewrite one intentional test-plan mutation with its matching approval digest."""
    plan_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8", newline="\n")
    approval = json.loads(approval_path.read_text(encoding="utf-8"))
    approval["plan_sha256"] = _sha256(plan_path)
    approval_path.write_text(json.dumps(approval, indent=2) + "\n", encoding="utf-8", newline="\n")


def _prepare(workspace: Path) -> tuple[ModuleType, Path, dict[str, object], Path]:
    """Prepare one valid pilot and return its module, state, payload, and repository."""
    lifecycle = _load_lifecycle()
    plan_path, approval_path, repository = _approved_plan(workspace)
    state_path = workspace / ".reports" / "codex" / "develop" / "fixture" / "lifecycle.json"
    state = lifecycle.prepare_write_pilot(
        plan_path=plan_path,
        approval_path=approval_path,
        workspace_root=workspace,
        state_path=state_path,
    )
    return lifecycle, state_path, state, repository


def _prepare_integrated_code_remediate(
    workspace: Path, *, simulate_crlf_default: bool = False
) -> tuple[ModuleType, Path, dict[str, object], Path]:
    """Prepare and integrate a fixture, optionally simulating a CRLF checkout default."""
    lifecycle = _load_lifecycle()
    plan_path, approval_path, repository = _approved_code_remediate_plan(workspace, pin_lf=simulate_crlf_default)
    if simulate_crlf_default:
        _git(repository, "config", "core.autocrlf", "false")
        _git(repository, "config", "core.eol", "crlf")
    state_path = repository / ".reports" / "codex" / "code-remediate" / "fixture" / "production-lifecycle.json"
    state = lifecycle.prepare_code_remediate_pilot(
        plan_path=plan_path,
        approval_path=approval_path,
        workspace_root=workspace,
        state_path=state_path,
    )
    for node, content in zip(state["nodes"], ("child-a\n", "child-b\n"), strict=True):
        worktree = workspace / str(node["worktree_path"])
        (worktree / str(node["owned_paths"][0])).write_text(content, encoding="utf-8", newline="\n")
    handovers = [
        lifecycle.create_completed_child_handover(
            state_path=state_path,
            node_id=str(node["node_id"]),
            summary=f"Completed {node['node_id']}.",
        )
        for node in state["nodes"]
    ]
    lifecycle.join_child_handovers(state_path=state_path, handovers=handovers)
    for node in state["nodes"]:
        lifecycle.collect_write_patch(state_path=state_path, node_id=str(node["node_id"]))
    lifecycle.integrate_write_pilot(state_path=state_path)
    return lifecycle, state_path, state, repository


@pytest.mark.installed_plugin
@pytest.mark.integration
def test_code_remediate_prepare_accepts_clean_digest_bound_production_plan(tmp_path: Path) -> None:
    """Prepare detached remediation worktrees from one clean, approval-bound source baseline."""
    lifecycle = _load_lifecycle()
    plan_path, approval_path, repository = _approved_code_remediate_plan(tmp_path)
    state_path = repository / ".reports" / "codex" / "code-remediate" / "fixture" / "production-lifecycle.json"

    state = lifecycle.prepare_code_remediate_pilot(
        plan_path=plan_path,
        approval_path=approval_path,
        workspace_root=tmp_path,
        state_path=state_path,
    )

    assert state["schema_version"] == 2
    assert state["status"] == "prepared"
    assert state["source_repository"] == "authoritative-repository"
    assert state["plan_sha256"] == _sha256(plan_path)
    assert state["approval_sha256"] == _sha256(approval_path)
    assert state["baseline_head"] == _git(repository, "rev-parse", "HEAD").stdout.strip()
    assert state["baseline_tree"] == _git(repository, "rev-parse", "HEAD^{tree}").stdout.strip()
    assert state_path.is_relative_to(repository)
    assert [node["owned_paths"] for node in state["nodes"]] == [["bucket-a.txt"], ["bucket-b.txt"]]
    assert all(not (tmp_path / str(node["worktree_path"])).is_relative_to(repository) for node in state["nodes"])
    assert all(
        _git(tmp_path / str(node["worktree_path"]), "symbolic-ref", "-q", "HEAD", check=False).returncode == 1
        for node in state["nodes"]
    )
    assert set(_git(repository, "status", "--porcelain=v1", "--untracked-files=all").stdout.splitlines()) == {
        "?? .reports/codex/code-remediate/fixture/WRITE-A-context.md",
        "?? .reports/codex/code-remediate/fixture/WRITE-B-context.md",
        "?? .reports/codex/code-remediate/fixture/parallel-approval.json",
        "?? .reports/codex/code-remediate/fixture/production-lifecycle.json",
        "?? .reports/codex/code-remediate/fixture/work-bucket-plan.json",
    }


@pytest.mark.installed_plugin
@pytest.mark.integration
@pytest.mark.parametrize("mutation", ["missing", "digest-mismatch"])
def test_code_remediate_prepare_binds_each_source_local_context_pack(tmp_path: Path, mutation: str) -> None:
    """Reject dispatch when an approved bucket lacks its exact source-local context bytes."""
    lifecycle = _load_lifecycle()
    plan_path, approval_path, repository = _approved_code_remediate_plan(tmp_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    bucket = plan["work_buckets"][0]
    context_path = plan_path.parent / str(bucket["context_pack_path"])
    if mutation == "missing":
        context_path.unlink()
        expected = "context-pack-missing:WRITE-A"
    else:
        context_path.write_text("changed context\n", encoding="utf-8", newline="\n")
        expected = "context-pack-digest-mismatch:WRITE-A"
    _rewrite_code_remediate_plan(plan_path, approval_path, plan)

    with pytest.raises(lifecycle.PilotError, match=rf"^{expected}$"):
        lifecycle.prepare_code_remediate_pilot(
            plan_path=plan_path,
            approval_path=approval_path,
            workspace_root=tmp_path,
            state_path=repository / ".reports" / "codex" / "code-remediate" / "fixture" / "production-lifecycle.json",
        )

    assert not (tmp_path / ".codex-rig-worktrees" / "fixture").exists()


@pytest.mark.installed_plugin
@pytest.mark.integration
def test_code_remediate_transition_rejects_context_pack_drift(tmp_path: Path) -> None:
    """Rehash frozen context bytes before a child handover can advance the lifecycle."""
    lifecycle = _load_lifecycle()
    plan_path, approval_path, repository = _approved_code_remediate_plan(tmp_path)
    state_path = repository / ".reports" / "codex" / "code-remediate" / "fixture" / "production-lifecycle.json"
    state = lifecycle.prepare_code_remediate_pilot(
        plan_path=plan_path,
        approval_path=approval_path,
        workspace_root=tmp_path,
        state_path=state_path,
    )
    context_path = repository / ".reports" / "codex" / "code-remediate" / "fixture" / "WRITE-A-context.md"
    context_path.write_text("drifted after prepare\n", encoding="utf-8", newline="\n")
    node = state["nodes"][0]
    worktree = tmp_path / str(node["worktree_path"])
    (worktree / str(node["owned_paths"][0])).write_text("child-a\n", encoding="utf-8", newline="\n")

    with pytest.raises(lifecycle.PilotError, match="^context-pack-digest-mismatch:WRITE-A$"):
        lifecycle.create_completed_child_handover(
            state_path=state_path,
            node_id="WRITE-A",
            summary="Context drift must block transition.",
        )


@pytest.mark.installed_plugin
@pytest.mark.integration
@pytest.mark.parametrize(
    "state_case",
    ["different-plan-path", "preexisting-state", "preexisting-state-temporary", "preexisting-output"],
)
def test_code_remediate_prepare_requires_one_plan_bound_new_state_path(tmp_path: Path, state_case: str) -> None:
    """Reject caller-selected state or pre-existing lifecycle output before creating worktrees."""
    lifecycle = _load_lifecycle()
    plan_path, approval_path, repository = _approved_code_remediate_plan(tmp_path)
    state_path = repository / ".reports" / "codex" / "code-remediate" / "fixture" / "production-lifecycle.json"
    if state_case == "different-plan-path":
        state_path = state_path.with_name("other-lifecycle.json")
        expected = "lifecycle-state-plan-mismatch"
    elif state_case == "preexisting-state":
        state_path.write_text('{"untrusted": true}\n', encoding="utf-8", newline="\n")
        expected = "lifecycle-state-exists"
    elif state_case == "preexisting-state-temporary":
        state_path.with_name(f".{state_path.name}.tmp").write_text("untrusted\n", encoding="utf-8", newline="\n")
        expected = "state-temporary-exists"
    else:
        state_path.with_name("patch-a.diff").write_text("untrusted\n", encoding="utf-8", newline="\n")
        expected = "lifecycle-output-exists:patch-a.diff"

    with pytest.raises(lifecycle.PilotError, match=rf"^{expected}$"):
        lifecycle.prepare_code_remediate_pilot(
            plan_path=plan_path,
            approval_path=approval_path,
            workspace_root=tmp_path,
            state_path=state_path,
        )

    assert not (tmp_path / ".codex-rig-worktrees" / "fixture").exists()


@pytest.mark.installed_plugin
@pytest.mark.integration
def test_code_remediate_rollback_rechecks_preimages_after_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retain evidence and block cleanup when an external restore leaves an approved path changed."""
    lifecycle, state_path, state, repository = _prepare_integrated_code_remediate(tmp_path)
    real_git = lifecycle._git

    def _fail_apply_and_skip_restore(target: Path, *arguments: str) -> str:
        """Apply once, inject a source failure, and suppress rollback restore commands."""
        if target == repository and arguments[:1] == ("apply",) and "--check" not in arguments:
            real_git(target, *arguments)
            raise lifecycle.PilotError("simulated-source-apply-failure")
        if target == repository and arguments[:1] == ("restore",):
            return ""
        return real_git(target, *arguments)

    monkeypatch.setattr(lifecycle, "_git", _fail_apply_and_skip_restore)
    with pytest.raises(lifecycle.PilotError, match="^rollback-ambiguous$"):
        lifecycle.apply_code_remediate_source(state_path=state_path)

    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["status"] == "rollback-ambiguous"
    assert persisted["source_application"]["status"] == "rollback-ambiguous"
    assert (repository / "bucket-a.txt").read_text(encoding="utf-8") == "child-a\n"
    assert all((tmp_path / str(node["worktree_path"])).exists() for node in state["nodes"])
    with pytest.raises(lifecycle.PilotError, match="^cleanup-before-source-application-forbidden$"):
        lifecycle.cleanup_code_remediate_pilot(state_path=state_path)


@pytest.mark.installed_plugin
@pytest.mark.integration
def test_code_remediate_uses_fixed_parent_gate_reference_not_bucket_shell_commands(tmp_path: Path) -> None:
    """Keep structural integration distinct from the parent-owned shared quality-gate execution."""
    lifecycle = _load_lifecycle()
    plan_path, approval_path, repository = _approved_code_remediate_plan(tmp_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["verification_gate"] == "code-remediate-shared-quality-gates"
    assert all("verification_commands" not in bucket for bucket in plan["work_buckets"])

    state = lifecycle.prepare_code_remediate_pilot(
        plan_path=plan_path,
        approval_path=approval_path,
        workspace_root=tmp_path,
        state_path=repository / ".reports" / "codex" / "code-remediate" / "fixture" / "production-lifecycle.json",
    )

    assert state["verification_gate"] == "code-remediate-shared-quality-gates"


@pytest.mark.installed_plugin
@pytest.mark.integration
def test_code_remediate_prepare_rejects_plan_changed_after_approval(tmp_path: Path) -> None:
    """Prevent approval of one remediation bucket contract authorizing another."""
    lifecycle = _load_lifecycle()
    plan_path, approval_path, _ = _approved_code_remediate_plan(tmp_path)
    plan_path.write_text(plan_path.read_text(encoding="utf-8") + "\n", encoding="utf-8", newline="\n")

    with pytest.raises(lifecycle.PilotError, match="^plan-approval-digest-mismatch$"):
        lifecycle.prepare_code_remediate_pilot(
            plan_path=plan_path,
            approval_path=approval_path,
            workspace_root=tmp_path,
            state_path=tmp_path
            / "authoritative-repository"
            / ".reports"
            / "codex"
            / "code-remediate"
            / "fixture"
            / "production-lifecycle.json",
        )


@pytest.mark.installed_plugin
@pytest.mark.integration
@pytest.mark.parametrize("source_state", ["dirty", "merging"])
def test_code_remediate_prepare_rejects_nonclean_source_before_dispatch(tmp_path: Path, source_state: str) -> None:
    """Block child dispatch when the authoritative repository has drift or an unresolved merge."""
    lifecycle = _load_lifecycle()
    plan_path, approval_path, repository = _approved_code_remediate_plan(tmp_path)
    if source_state == "dirty":
        (repository / "bucket-a.txt").write_text("dirty\n", encoding="utf-8", newline="\n")
    else:
        base_branch = _git(repository, "branch", "--show-current").stdout.strip()
        _git(repository, "checkout", "-q", "-b", "conflicting-change")
        (repository / "bucket-a.txt").write_text("other branch\n", encoding="utf-8", newline="\n")
        _git(repository, "add", "bucket-a.txt")
        _git(repository, "commit", "-q", "-m", "conflicting change")
        _git(repository, "checkout", "-q", base_branch)
        (repository / "bucket-a.txt").write_text("source branch\n", encoding="utf-8", newline="\n")
        _git(repository, "add", "bucket-a.txt")
        _git(repository, "commit", "-q", "-m", "source change")
        assert _git(repository, "merge", "conflicting-change", check=False).returncode != 0

    with pytest.raises(lifecycle.PilotError, match=rf"^source-repository-{source_state}$"):
        lifecycle.prepare_code_remediate_pilot(
            plan_path=plan_path,
            approval_path=approval_path,
            workspace_root=tmp_path,
            state_path=repository / ".reports" / "codex" / "code-remediate" / "fixture" / "production-lifecycle.json",
        )

    assert not (tmp_path / ".codex-rig-worktrees" / "fixture").exists()


@pytest.mark.installed_plugin
@pytest.mark.integration
@pytest.mark.skipif(not SYMLINKS_SUPPORTED, reason="directory symlink capability unavailable")
def test_code_remediate_prepare_rejects_symlinked_sibling_worktree_root(tmp_path: Path) -> None:
    """Prevent a plan-bound sibling worktree root escaping through a symlinked parent."""
    lifecycle = _load_lifecycle()
    plan_path, approval_path, repository = _approved_code_remediate_plan(tmp_path)
    outside = tmp_path / "outside-worktrees"
    outside.mkdir()
    (tmp_path / ".codex-rig-worktrees").symlink_to(outside, target_is_directory=True)

    with pytest.raises(lifecycle.PilotError, match="^managed-path-symlink-forbidden$"):
        lifecycle.prepare_code_remediate_pilot(
            plan_path=plan_path,
            approval_path=approval_path,
            workspace_root=tmp_path,
            state_path=repository / ".reports" / "codex" / "code-remediate" / "fixture" / "production-lifecycle.json",
        )

    assert not (outside / "fixture").exists()


@pytest.mark.installed_plugin
@pytest.mark.integration
@pytest.mark.skipif(not SYMLINKS_SUPPORTED, reason="directory symlink capability unavailable")
def test_code_remediate_transition_rejects_symlinked_evidence_root(tmp_path: Path) -> None:
    """Reject lifecycle authority redirected after preparation through source-local evidence symlinks."""
    lifecycle = _load_lifecycle()
    plan_path, approval_path, repository = _approved_code_remediate_plan(tmp_path)
    state_path = repository / ".reports" / "codex" / "code-remediate" / "fixture" / "production-lifecycle.json"
    state = lifecycle.prepare_code_remediate_pilot(
        plan_path=plan_path,
        approval_path=approval_path,
        workspace_root=tmp_path,
        state_path=state_path,
    )
    evidence = repository / ".reports"
    outside = tmp_path / "outside-evidence"
    evidence.rename(outside)
    evidence.symlink_to(outside, target_is_directory=True)

    with pytest.raises(lifecycle.PilotError, match="^managed-path-symlink-forbidden$"):
        lifecycle.create_completed_child_handover(
            state_path=state_path,
            node_id=str(state["nodes"][0]["node_id"]),
            summary="Must fail before reading child changes.",
        )


@pytest.mark.installed_plugin
@pytest.mark.integration
@pytest.mark.skipif(not SYMLINKS_SUPPORTED, reason="directory symlink capability unavailable")
def test_code_remediate_prepare_rejects_symlinked_source_parent(tmp_path: Path) -> None:
    """Prevent an authoritative repository path escaping through a symlinked parent."""
    lifecycle = _load_lifecycle()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _, _, repository = _approved_code_remediate_plan(workspace)
    outside_repository = tmp_path / "outside-source-repository"
    repository.rename(outside_repository)
    (workspace / "source-parent").symlink_to(tmp_path, target_is_directory=True)
    evidence = outside_repository / ".reports" / "codex" / "code-remediate" / "fixture"
    plan_path = evidence / "work-bucket-plan.json"
    approval_path = evidence / "parallel-approval.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["source_repository"] = "source-parent/outside-source-repository"
    _rewrite_code_remediate_plan(plan_path, approval_path, plan)

    with pytest.raises(lifecycle.PilotError, match="^managed-path-symlink-forbidden$"):
        lifecycle.prepare_code_remediate_pilot(
            plan_path=plan_path,
            approval_path=approval_path,
            workspace_root=workspace,
            state_path=evidence / "production-lifecycle.json",
        )

    assert not (workspace / ".codex-rig-worktrees" / "fixture").exists()


@pytest.mark.installed_plugin
@pytest.mark.integration
def test_code_remediate_source_application_records_rollback_before_nonforce_cleanup(tmp_path: Path) -> None:
    """Apply only integrated bucket patches and retain rollback bytes before cleanup removes worktrees."""
    lifecycle, state_path, state, repository = _prepare_integrated_code_remediate(tmp_path)

    application = lifecycle.apply_code_remediate_source(state_path=state_path)

    rollback_path = state_path.parent / str(application["rollback_patch_path"])
    assert application["status"] == "applied"
    assert application["applied_paths"] == ["bucket-a.txt", "bucket-b.txt"]
    assert _sha256(rollback_path) == application["rollback_patch_sha256"]
    assert (repository / "bucket-a.txt").read_text(encoding="utf-8") == "child-a\n"
    assert (repository / "bucket-b.txt").read_text(encoding="utf-8") == "child-b\n"

    cleanup = lifecycle.cleanup_code_remediate_pilot(state_path=state_path)

    assert cleanup["cleanup_status"] == "removed"
    assert cleanup["force"] is False
    assert all(not (tmp_path / str(node["worktree_path"])).exists() for node in state["nodes"])


@pytest.mark.installed_plugin
@pytest.mark.integration
def test_code_remediate_source_application_records_actual_crlf_postimage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Record native source bytes after clean-filter-equivalent CRLF application."""
    lifecycle, state_path, _, repository = _prepare_integrated_code_remediate(tmp_path, simulate_crlf_default=True)
    _git(repository, "config", "core.autocrlf", "true")
    real_git = lifecycle._git

    def _apply_with_crlf_source(target: Path, *arguments: str) -> str:
        """Convert applied source files to CRLF before lifecycle postimage checks."""
        result = real_git(target, *arguments)
        if target == repository and arguments[0] == "apply" and "--check" not in arguments:
            for relative in ("bucket-a.txt", "bucket-b.txt"):
                path = repository / relative
                path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        return result

    monkeypatch.setattr(lifecycle, "_git", _apply_with_crlf_source)

    application = lifecycle.apply_code_remediate_source(state_path=state_path)

    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    actual_postimages = {relative: _sha256(repository / relative) for relative in ("bucket-a.txt", "bucket-b.txt")}
    assert application["status"] == "applied"
    assert (repository / "bucket-a.txt").read_bytes() == b"child-a\r\n"
    assert (repository / "bucket-b.txt").read_bytes() == b"child-b\r\n"
    assert persisted["source"]["postimage_sha256"] == actual_postimages
    assert persisted["source"]["postimage_sha256"] != persisted["integration_final_sha256"]
    assert lifecycle.cleanup_code_remediate_pilot(state_path=state_path)["cleanup_status"] == "removed"


@pytest.mark.installed_plugin
@pytest.mark.integration
def test_code_remediate_cli_join_rejects_handover_outside_evidence_root(tmp_path: Path) -> None:
    """Reject an otherwise valid external handover before it can satisfy a parent join."""
    lifecycle = _load_lifecycle()
    plan_path, approval_path, repository = _approved_code_remediate_plan(tmp_path)
    state_path = repository / ".reports" / "codex" / "code-remediate" / "fixture" / "production-lifecycle.json"
    state = lifecycle.prepare_code_remediate_pilot(
        plan_path=plan_path,
        approval_path=approval_path,
        workspace_root=tmp_path,
        state_path=state_path,
    )
    handover_paths: list[Path] = []
    for node, content in zip(state["nodes"], ("child-a\n", "child-b\n"), strict=True):
        worktree = tmp_path / str(node["worktree_path"])
        (worktree / str(node["owned_paths"][0])).write_text(content, encoding="utf-8", newline="\n")
        handover = lifecycle.create_completed_child_handover(
            state_path=state_path,
            node_id=str(node["node_id"]),
            summary=f"Completed {node['node_id']}.",
        )
        handover_path = state_path.parent / f"{node['node_id']}-handover.json"
        if node["node_id"] == "WRITE-A":
            handover_path = tmp_path / handover_path.name
        handover_path.write_text(json.dumps(handover, indent=2) + "\n", encoding="utf-8", newline="\n")
        handover_paths.append(handover_path)

    assert (
        lifecycle.main(
            ["join", "--state", str(state_path), *sum((["--handover", str(path)] for path in handover_paths), [])]
        )
        == 2
    )

    assert json.loads(state_path.read_text(encoding="utf-8"))["status"] == "prepared"


@pytest.mark.installed_plugin
@pytest.mark.integration
@pytest.mark.skipif(not SYMLINKS_SUPPORTED, reason="directory symlink capability unavailable")
@pytest.mark.parametrize("path_kind", ["contained", "symlinked-component"])
def test_code_remediate_cli_handover_output_rejects_intermediate_symlink(tmp_path: Path, path_kind: str) -> None:
    """Allow a contained output but reject a symlinked parent even before its terminal exists."""
    lifecycle = _load_lifecycle()
    plan_path, approval_path, repository = _approved_code_remediate_plan(tmp_path)
    state_path = repository / ".reports" / "codex" / "code-remediate" / "fixture" / "production-lifecycle.json"
    state = lifecycle.prepare_code_remediate_pilot(
        plan_path=plan_path,
        approval_path=approval_path,
        workspace_root=tmp_path,
        state_path=state_path,
    )
    node = state["nodes"][0]
    worktree = tmp_path / str(node["worktree_path"])
    (worktree / str(node["owned_paths"][0])).write_text("child-a\n", encoding="utf-8", newline="\n")
    output_parent = state_path.parent / "handover-output"
    if path_kind == "symlinked-component":
        contained_target = state_path.parent / "contained-target"
        contained_target.mkdir()
        output_parent.symlink_to(contained_target, target_is_directory=True)
    else:
        output_parent.mkdir()
    output_path = output_parent / "WRITE-A.json"

    result = lifecycle.main(
        [
            "create-handover",
            "--state",
            str(state_path),
            "--node",
            "WRITE-A",
            "--summary",
            "One valid child handover.",
            "--output",
            str(output_path),
        ]
    )

    assert result == (0 if path_kind == "contained" else 2)
    assert output_path.exists() is (path_kind == "contained")


@pytest.mark.installed_plugin
@pytest.mark.integration
def test_code_remediate_apply_rejects_tampered_integration_before_source_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revalidate integrated bytes before any authoritative source apply command can run."""
    lifecycle, state_path, state, repository = _prepare_integrated_code_remediate(tmp_path)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    integration = tmp_path / str(state["integration_worktree"])
    (integration / "bucket-a.txt").write_text("tampered integration\n", encoding="utf-8", newline="\n")
    real_git = lifecycle._git

    def _reject_source_apply(target: Path, *arguments: str) -> str:
        """Fail if source application runs after integration-worktree tampering."""
        if target == repository and arguments[:1] == ("apply",):
            raise AssertionError("authoritative source apply ran after integration tamper")
        return real_git(target, *arguments)

    monkeypatch.setattr(lifecycle, "_git", _reject_source_apply)
    with pytest.raises(lifecycle.PilotError, match="^integration-worktree-drift$"):
        lifecycle.apply_code_remediate_source(state_path=state_path)

    assert (repository / "bucket-a.txt").read_text(encoding="utf-8") == "baseline-a\n"
    assert (repository / "bucket-b.txt").read_text(encoding="utf-8") == "baseline-b\n"


@pytest.mark.installed_plugin
@pytest.mark.integration
def test_code_remediate_cli_drives_the_supported_parent_lifecycle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Keep the skill-callable CLI equivalent to the validated function lifecycle."""
    lifecycle = _load_lifecycle()
    plan_path, approval_path, repository = _approved_code_remediate_plan(tmp_path)
    state_path = repository / ".reports" / "codex" / "code-remediate" / "fixture" / "production-lifecycle.json"

    assert (
        lifecycle.main(
            [
                "prepare",
                "--plan",
                str(plan_path),
                "--approval",
                str(approval_path),
                "--workspace",
                str(tmp_path),
                "--state",
                str(state_path),
            ]
        )
        == 0
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    handover_paths: list[Path] = []
    for node, content in zip(state["nodes"], ("child-a\n", "child-b\n"), strict=True):
        worktree = tmp_path / str(node["worktree_path"])
        (worktree / str(node["owned_paths"][0])).write_text(content, encoding="utf-8", newline="\n")
        handover_path = state_path.parent / f"{node['node_id']}-handover.json"
        assert (
            lifecycle.main(
                [
                    "create-handover",
                    "--state",
                    str(state_path),
                    "--node",
                    str(node["node_id"]),
                    "--summary",
                    f"Completed {node['node_id']}.",
                    "--output",
                    str(handover_path),
                ]
            )
            == 0
        )
        handover_paths.append(handover_path)
    join_arguments = ["join", "--state", str(state_path)]
    for handover_path in handover_paths:
        join_arguments.extend(["--handover", str(handover_path)])
    assert lifecycle.main(join_arguments) == 0
    for node in state["nodes"]:
        assert lifecycle.main(["collect", "--state", str(state_path), "--node", str(node["node_id"])]) == 0
    assert lifecycle.main(["integrate", "--state", str(state_path)]) == 0
    assert lifecycle.main(["apply-source", "--state", str(state_path)]) == 0
    assert lifecycle.main(["cleanup", "--state", str(state_path)]) == 0

    capsys.readouterr()
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["status"] == "completed"
    assert persisted["cleanup"] == {
        "force": False,
        "status": "removed",
        "worktrees": [
            ".codex-rig-worktrees/fixture/WRITE-A",
            ".codex-rig-worktrees/fixture/WRITE-B",
            ".codex-rig-worktrees/fixture/integration",
        ],
    }
    _load_validator()._validate_code_remediate_production_lifecycle(
        {
            "production_lifecycle": {
                "path": state_path.name,
                "sha256": _sha256(state_path),
                "status": "completed",
            }
        },
        json.loads(plan_path.read_text(encoding="utf-8")),
        state_path.parent,
        _sha256(plan_path),
    )


@pytest.mark.installed_plugin
@pytest.mark.integration
def test_code_remediate_source_apply_failure_restores_only_known_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restore exact baseline paths when a source apply fails after reaching known postimages."""
    lifecycle, state_path, _, repository = _prepare_integrated_code_remediate(tmp_path)
    real_git = lifecycle._git

    def _fail_after_source_apply(target: Path, *arguments: str) -> str:
        """Apply source changes once, then inject the lifecycle failure."""
        if target == repository and arguments[0] == "apply" and "--check" not in arguments:
            real_git(target, *arguments)
            raise lifecycle.PilotError("simulated-source-apply-failure")
        return real_git(target, *arguments)

    monkeypatch.setattr(lifecycle, "_git", _fail_after_source_apply)
    with pytest.raises(lifecycle.PilotError, match="^simulated-source-apply-failure$"):
        lifecycle.apply_code_remediate_source(state_path=state_path)

    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["status"] == "failed-rolled-back"
    assert persisted["source_application"]["status"] == "failed-rolled-back"
    assert (repository / "bucket-a.txt").read_text(encoding="utf-8") == "baseline-a\n"
    assert (repository / "bucket-b.txt").read_text(encoding="utf-8") == "baseline-b\n"
    assert set(_git(repository, "status", "--porcelain=v1", "--untracked-files=all").stdout.splitlines()) == {
        "?? .reports/codex/code-remediate/fixture/WRITE-A-context.md",
        "?? .reports/codex/code-remediate/fixture/WRITE-B-context.md",
        "?? .reports/codex/code-remediate/fixture/parallel-approval.json",
        "?? .reports/codex/code-remediate/fixture/patch-a.diff",
        "?? .reports/codex/code-remediate/fixture/patch-b.diff",
        "?? .reports/codex/code-remediate/fixture/production-lifecycle.json",
        "?? .reports/codex/code-remediate/fixture/rollback.patch",
        "?? .reports/codex/code-remediate/fixture/source-application.patch",
        "?? .reports/codex/code-remediate/fixture/work-bucket-plan.json",
    }


@pytest.mark.installed_plugin
@pytest.mark.integration
def test_code_remediate_source_apply_rolls_back_filtered_newline_postimage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restore a known clean-filtered postimage whose source bytes use CRLF."""
    lifecycle, state_path, _, repository = _prepare_integrated_code_remediate(tmp_path)
    _git(repository, "config", "core.autocrlf", "true")
    real_git = lifecycle._git

    def _fail_after_crlf_source_apply(target: Path, *arguments: str) -> str:
        """Apply changes, rewrite them as CRLF, and inject the lifecycle failure."""
        if target == repository and arguments[0] == "apply" and "--check" not in arguments:
            real_git(target, *arguments)
            for relative in ("bucket-a.txt", "bucket-b.txt"):
                path = repository / relative
                path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
            raise lifecycle.PilotError("simulated-source-apply-failure")
        return real_git(target, *arguments)

    monkeypatch.setattr(lifecycle, "_git", _fail_after_crlf_source_apply)
    with pytest.raises(lifecycle.PilotError, match="^simulated-source-apply-failure$"):
        lifecycle.apply_code_remediate_source(state_path=state_path)

    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["status"] == "failed-rolled-back"
    assert persisted["source_application"]["status"] == "failed-rolled-back"
    assert (repository / "bucket-a.txt").read_bytes() == b"baseline-a\r\n"
    assert (repository / "bucket-b.txt").read_bytes() == b"baseline-b\r\n"


@pytest.mark.installed_plugin
@pytest.mark.integration
def test_code_remediate_ambiguous_source_failure_retains_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retain diagnostics without restoration when a concurrent write creates an unknown state."""
    lifecycle, state_path, state, repository = _prepare_integrated_code_remediate(tmp_path)
    real_git = lifecycle._git

    def _create_ambiguous_state(target: Path, *arguments: str) -> str:
        """Apply changes, simulate a concurrent writer, and report the resulting ambiguity."""
        if target == repository and arguments[0] == "apply" and "--check" not in arguments:
            real_git(target, *arguments)
            (repository / "bucket-a.txt").write_text("concurrent-writer\n", encoding="utf-8", newline="\n")
            raise lifecycle.PilotError("simulated-concurrent-write")
        return real_git(target, *arguments)

    monkeypatch.setattr(lifecycle, "_git", _create_ambiguous_state)
    with pytest.raises(lifecycle.PilotError, match="^rollback-ambiguous$"):
        lifecycle.apply_code_remediate_source(state_path=state_path)

    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["status"] == "rollback-ambiguous"
    assert persisted["source_application"]["status"] == "rollback-ambiguous"
    assert (repository / "bucket-a.txt").read_text(encoding="utf-8") == "concurrent-writer\n"
    assert (repository / "bucket-b.txt").read_text(encoding="utf-8") == "child-b\n"
    assert all((tmp_path / str(node["worktree_path"])).exists() for node in state["nodes"])
    with pytest.raises(lifecycle.PilotError, match="^cleanup-before-source-application-forbidden$"):
        lifecycle.cleanup_code_remediate_pilot(state_path=state_path)


def _collect(
    lifecycle: ModuleType, state_path: Path, state: dict[str, object], node: dict[str, object]
) -> dict[str, object]:
    """Collect one patch after both child handovers have joined."""
    return lifecycle.collect_write_patch(
        state_path=state_path,
        node_id=str(node["node_id"]),
    )


def _join(lifecycle: ModuleType, state_path: Path, state: dict[str, object], workspace: Path) -> dict[str, object]:
    """Join both valid child handovers for the prepared fixture."""
    return lifecycle.join_child_handovers(
        state_path=state_path,
        handovers=[_child_handover(workspace, node) for node in state["nodes"]],
    )


def _write_other_children(workspace: Path, state: dict[str, object], excluded_node_id: str) -> None:
    """Give every non-target child one valid owned-path edit."""
    for node in state["nodes"]:
        if node["node_id"] == excluded_node_id:
            continue
        worktree = workspace / str(node["worktree_path"])
        (worktree / str(node["owned_paths"][0])).write_text("other-child\n", encoding="utf-8", newline="\n")


@pytest.mark.installed_plugin
@pytest.mark.integration
def test_prepare_rejects_plan_changed_after_approval(tmp_path: Path) -> None:
    """Prevent a stale approval from authorizing changed nodes or scope."""
    lifecycle = _load_lifecycle()
    plan_path, approval_path, _ = _approved_plan(tmp_path)
    plan_path.write_text(plan_path.read_text(encoding="utf-8") + "\n", encoding="utf-8", newline="\n")

    with pytest.raises(lifecycle.PilotError, match="^plan-approval-digest-mismatch$"):
        lifecycle.prepare_write_pilot(
            plan_path=plan_path,
            approval_path=approval_path,
            workspace_root=tmp_path,
            state_path=tmp_path / ".reports" / "codex" / "develop" / "state.json",
        )

    assert not (tmp_path / ".reports" / "codex" / "develop" / "fixture" / "worktrees").exists()


@pytest.mark.installed_plugin
@pytest.mark.integration
@pytest.mark.parametrize(
    ("owned_path", "error"),
    [
        pytest.param("../escape.txt", "owned-path-invalid", id="..-escape.txt"),
        pytest.param("/absolute.txt", "owned-path-invalid", id="absolute.txt"),
        pytest.param("C:\\absolute.txt", "owned-path-invalid", id="c-absolute.txt"),
        pytest.param("C:relative.txt", "owned-path-invalid", id="c-relative.txt"),
        pytest.param("\\\\server\\share.txt", "owned-path-invalid", id="server-share.txt"),
        pytest.param("bucket-a.txt.", "owned-path-nonportable", id="bucket-a.txt."),
        pytest.param("BUCKET-A.TXT", "owned-path-alias", id="bucket-a.txt"),
        pytest.param("bucket-a.txt/child", "owned-path-overlap", id="bucket-a.txt-child"),
    ],
)
def test_prepare_rejects_nonportable_or_aliased_owned_paths(tmp_path: Path, owned_path: str, error: str) -> None:
    """Block traversal, Windows aliases, and nonportable bucket ownership."""
    lifecycle = _load_lifecycle()
    plan_path, approval_path, _ = _approved_plan(tmp_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["stages"][0]["nodes"][1]["owned_paths"] = [owned_path]
    plan_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8", newline="\n")
    approval = json.loads(approval_path.read_text(encoding="utf-8"))
    approval["plan_sha256"] = _sha256(plan_path)
    approval_path.write_text(json.dumps(approval, indent=2) + "\n", encoding="utf-8", newline="\n")

    with pytest.raises(lifecycle.PilotError, match=rf"^{error}"):
        lifecycle.prepare_write_pilot(
            plan_path=plan_path,
            approval_path=approval_path,
            workspace_root=tmp_path,
            state_path=tmp_path / ".reports" / "codex" / "develop" / "state.json",
        )


@pytest.mark.installed_plugin
@pytest.mark.integration
def test_prepare_rejects_overlapping_resource_locks(tmp_path: Path) -> None:
    """Stop two otherwise disjoint packages from sharing one exclusive resource."""
    lifecycle = _load_lifecycle()
    plan_path, approval_path, _ = _approved_plan(tmp_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    for node in plan["stages"][0]["nodes"]:
        node["resource_locks"] = ["git-index"]
    plan_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8", newline="\n")
    approval = json.loads(approval_path.read_text(encoding="utf-8"))
    approval["plan_sha256"] = _sha256(plan_path)
    approval_path.write_text(json.dumps(approval, indent=2) + "\n", encoding="utf-8", newline="\n")

    with pytest.raises(lifecycle.PilotError, match="^resource-lock-overlap:FIXTURE-WRITE-B$"):
        lifecycle.prepare_write_pilot(
            plan_path=plan_path,
            approval_path=approval_path,
            workspace_root=tmp_path,
            state_path=tmp_path / ".reports" / "codex" / "develop" / "state.json",
        )


@pytest.mark.installed_plugin
@pytest.mark.integration
@pytest.mark.parametrize("dirty_kind", ["tracked", "untracked"])
def test_prepare_rejects_dirty_fixture_repository(tmp_path: Path, dirty_kind: str) -> None:
    """Prevent dispatch from a baseline with hidden tracked or untracked drift."""
    lifecycle = _load_lifecycle()
    plan_path, approval_path, repository = _approved_plan(tmp_path)
    target = repository / ("bucket-a.txt" if dirty_kind == "tracked" else "untracked.txt")
    target.write_text("dirty\n", encoding="utf-8", newline="\n")

    with pytest.raises(lifecycle.PilotError, match="^fixture-repository-dirty$"):
        lifecycle.prepare_write_pilot(
            plan_path=plan_path,
            approval_path=approval_path,
            workspace_root=tmp_path,
            state_path=tmp_path / ".reports" / "codex" / "develop" / "state.json",
        )


@pytest.mark.installed_plugin
@pytest.mark.integration
def test_prepare_creates_detached_worktrees_at_exact_frozen_head(tmp_path: Path) -> None:
    """Create isolated children without mutating the generated source checkout."""
    _, _, state, repository = _prepare(tmp_path)
    baseline = str(state["baseline_head"])

    for node in state["nodes"]:
        worktree = tmp_path / str(node["worktree_path"])
        assert _git(worktree, "rev-parse", "HEAD").stdout.strip() == baseline
        assert _git(worktree, "symbolic-ref", "-q", "HEAD", check=False).returncode == 1
    assert _git(repository, "status", "--porcelain=v1", "--untracked-files=all").stdout == ""


@pytest.mark.installed_plugin
@pytest.mark.integration
def test_prepare_canonicalizes_multi_path_package_order(tmp_path: Path) -> None:
    """Freeze multi-path ownership in the same lexical order used by parent verification."""
    lifecycle = _load_lifecycle()
    plan_path, approval_path, repository = _approved_plan(tmp_path)
    (repository / "bucket-c.txt").write_text("baseline-c\n", encoding="utf-8", newline="\n")
    _git(repository, "add", "bucket-c.txt")
    _git(repository, "commit", "-q", "-m", "add third fixture bucket")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["stages"][0]["nodes"][0]["owned_paths"] = ["bucket-c.txt", "bucket-a.txt"]
    plan_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8", newline="\n")
    approval = json.loads(approval_path.read_text(encoding="utf-8"))
    approval["plan_sha256"] = _sha256(plan_path)
    approval_path.write_text(json.dumps(approval, indent=2) + "\n", encoding="utf-8", newline="\n")

    state = lifecycle.prepare_write_pilot(
        plan_path=plan_path,
        approval_path=approval_path,
        workspace_root=tmp_path,
        state_path=tmp_path / ".reports" / "codex" / "develop" / "state.json",
    )

    assert state["nodes"][0]["owned_paths"] == ["bucket-a.txt", "bucket-c.txt"]


@pytest.mark.installed_plugin
@pytest.mark.integration
@pytest.mark.parametrize("state_path_kind", ["path", "string"])
def test_create_completed_child_handover_accepts_canonical_state_path_forms(
    tmp_path: Path, state_path_kind: str
) -> None:
    """Return the canonical report for supported lifecycle-state path forms."""
    lifecycle, state_path, state, _ = _prepare(tmp_path)
    node = state["nodes"][1]
    worktree = tmp_path / str(node["worktree_path"])
    (worktree / "bucket-b.txt").write_text("child-b\n", encoding="utf-8", newline="\n")
    expected = _child_handover(tmp_path, node)
    expected["summary"] = "Updated bucket B."
    state_path_value = state_path if state_path_kind == "path" else str(state_path)

    report = lifecycle.create_completed_child_handover(
        state_path=state_path_value,
        node_id="FIXTURE-WRITE-B",
        summary="Updated bucket B.",
    )

    assert report == expected
    assert json.loads(state_path.read_text(encoding="utf-8"))["status"] == "prepared"


@pytest.mark.installed_plugin
@pytest.mark.integration
def test_create_completed_child_handover_rejects_invalid_state_path_type(tmp_path: Path) -> None:
    """Return the stable pilot error for an unsupported state-path object."""
    lifecycle, _, _, _ = _prepare(tmp_path)

    with pytest.raises(lifecycle.PilotError, match="^lifecycle-state-path-invalid$"):
        lifecycle.create_completed_child_handover(
            state_path=object(),
            node_id="FIXTURE-WRITE-B",
            summary="Updated bucket B.",
        )


@pytest.mark.installed_plugin
@pytest.mark.integration
def test_create_completed_child_handover_rejects_relative_state_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed when a child resolves the parent state relative to its worktree."""
    lifecycle, state_path, state, _ = _prepare(tmp_path)
    worktree = tmp_path / str(state["nodes"][1]["worktree_path"])
    monkeypatch.chdir(worktree)

    with pytest.raises(lifecycle.PilotError, match="^lifecycle-state-invalid$"):
        lifecycle.create_completed_child_handover(
            state_path=state_path.relative_to(tmp_path),
            node_id="FIXTURE-WRITE-B",
            summary="Updated bucket B.",
        )


@pytest.mark.installed_plugin
@pytest.mark.integration
def test_parent_authoritative_handovers_gate_patch_collection_and_integration(tmp_path: Path) -> None:
    """Require both child reports and verify them against worktrees before integration."""
    lifecycle, state_path, state, repository = _prepare(tmp_path)
    for node, content in zip(state["nodes"], ("child-a\n", "child-b\n"), strict=True):
        worktree = tmp_path / str(node["worktree_path"])
        (worktree / str(node["owned_paths"][0])).write_text(content, encoding="utf-8", newline="\n")

    handovers = [
        lifecycle.create_completed_child_handover(
            state_path=state_path,
            node_id=str(node["node_id"]),
            summary=f"Completed {node['node_id']}.",
        )
        for node in state["nodes"]
    ]
    joined = lifecycle.join_child_handovers(state_path=state_path, handovers=handovers)
    patches = [
        lifecycle.collect_write_patch(state_path=state_path, node_id=str(node["node_id"])) for node in state["nodes"]
    ]
    result = lifecycle.integrate_write_pilot(state_path=state_path)
    integration = tmp_path / str(result["integration_worktree"])

    assert joined["status"] == "joined"
    assert [node["handover"]["status"] for node in joined["nodes"]] == ["completed", "completed"]
    assert [node["handover"]["patch_sha256"] for node in joined["nodes"]] == [
        handover["patch_sha256"] for handover in handovers
    ]
    assert [patch["patch_sha256"] for patch in patches] == [
        node["handover"]["patch_sha256"] for node in joined["nodes"]
    ]
    assert (integration / "bucket-a.txt").read_text(encoding="utf-8") == "child-a\n"
    assert (integration / "bucket-b.txt").read_text(encoding="utf-8") == "child-b\n"
    assert (repository / "bucket-a.txt").read_text(encoding="utf-8") == "baseline-a\n"


@pytest.mark.installed_plugin
@pytest.mark.integration
def test_prior_attempt_fingerprint_blocks_retained_worktree_mutation(tmp_path: Path) -> None:
    """Freeze retained diagnostics and reject their mutation before current patch collection."""
    lifecycle = _load_lifecycle()
    plan_path, approval_path, repository = _approved_plan(tmp_path)
    prior_root = tmp_path / ".reports" / "codex" / "develop" / "prior" / "worktrees"
    prior_root.mkdir(parents=True)
    for node_id in ("FIXTURE-WRITE-A", "FIXTURE-WRITE-B"):
        _git(repository, "worktree", "add", "--detach", str(prior_root / node_id), "HEAD")
    runtime = tmp_path / ".reports" / "codex" / "develop" / "prior" / "runtime.json"
    runtime.write_text('{"status":"failed"}\n', encoding="utf-8", newline="\n")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["prior_attempt"] = {
        "runtime_record": runtime.relative_to(tmp_path).as_posix(),
        "status": "failed-retained",
        "retry_count": 0,
        "retained_worktree_root": prior_root.relative_to(tmp_path).as_posix(),
    }
    plan_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8", newline="\n")
    approval = json.loads(approval_path.read_text(encoding="utf-8"))
    approval["plan_sha256"] = _sha256(plan_path)
    approval_path.write_text(json.dumps(approval, indent=2) + "\n", encoding="utf-8", newline="\n")
    state_path = tmp_path / ".reports" / "codex" / "develop" / "fixture" / "lifecycle.json"
    state = lifecycle.prepare_write_pilot(
        plan_path=plan_path,
        approval_path=approval_path,
        workspace_root=tmp_path,
        state_path=state_path,
    )
    (prior_root / "FIXTURE-WRITE-B" / "bucket-b.txt").write_text("mutated\n", encoding="utf-8", newline="\n")
    current = tmp_path / str(state["nodes"][0]["worktree_path"])
    (current / "bucket-a.txt").write_text("child-a\n", encoding="utf-8", newline="\n")
    _write_other_children(tmp_path, state, "FIXTURE-WRITE-A")

    with pytest.raises(lifecycle.PilotError, match="^prior-attempt-fingerprint-mismatch$"):
        _join(lifecycle, state_path, state, tmp_path)

    assert not (state_path.parent / "patch-a.diff").exists()


@pytest.mark.installed_plugin
@pytest.mark.integration
def test_state_authority_tamper_fails_before_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rederive managed roots before executing Git from mutable lifecycle state."""
    lifecycle, state_path, state, _ = _prepare(tmp_path)
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    persisted["repository"] = ".reports/codex/develop/redirected"
    state_path.write_text(json.dumps(persisted, indent=2) + "\n", encoding="utf-8", newline="\n")

    def _unexpected_git(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Fail if Git runs before lifecycle state authority validation."""
        raise AssertionError("Git ran before state authority validation")

    monkeypatch.setattr(lifecycle.subprocess, "run", _unexpected_git)
    with pytest.raises(lifecycle.PilotError, match="^lifecycle-state-root-drift$"):
        lifecycle.collect_write_patch(
            state_path=state_path,
            node_id="FIXTURE-WRITE-A",
        )


@pytest.mark.installed_plugin
@pytest.mark.integration
def test_collect_rejects_child_commit_and_retains_worktree(tmp_path: Path) -> None:
    """Reject a clean-looking child that hid its mutation in a commit."""
    lifecycle, state_path, state, _ = _prepare(tmp_path)
    worktree = tmp_path / str(state["nodes"][0]["worktree_path"])
    (worktree / "bucket-a.txt").write_text("child-a\n", encoding="utf-8", newline="\n")
    _git(worktree, "add", "bucket-a.txt")
    _git(worktree, "commit", "-q", "-m", "forbidden child commit")
    _write_other_children(tmp_path, state, "FIXTURE-WRITE-A")

    with pytest.raises(lifecycle.PilotError, match="^child-commit-forbidden:FIXTURE-WRITE-A$"):
        _join(lifecycle, state_path, state, tmp_path)

    assert worktree.exists()


@pytest.mark.installed_plugin
@pytest.mark.integration
@pytest.mark.parametrize("mutation", ["outside-owned", "untracked"])
def test_collect_rejects_undeclared_or_untracked_changes(tmp_path: Path, mutation: str) -> None:
    """Prevent a child patch from smuggling undeclared or untracked files."""
    lifecycle, state_path, state, _ = _prepare(tmp_path)
    worktree = tmp_path / str(state["nodes"][0]["worktree_path"])
    target = worktree / ("bucket-b.txt" if mutation == "outside-owned" else "new.txt")
    target.write_text("forbidden\n", encoding="utf-8", newline="\n")
    expected = "child-owned-path-mismatch" if mutation == "outside-owned" else "child-untracked-path-forbidden"
    _write_other_children(tmp_path, state, "FIXTURE-WRITE-A")

    with pytest.raises(lifecycle.PilotError, match=rf"^{expected}:FIXTURE-WRITE-A$"):
        _join(lifecycle, state_path, state, tmp_path)


@pytest.mark.installed_plugin
@pytest.mark.integration
def test_collect_rejects_staged_owned_change(tmp_path: Path) -> None:
    """Prevent a valid owned edit from bypassing the patch-only index boundary."""
    lifecycle, state_path, state, _ = _prepare(tmp_path)
    worktree = tmp_path / str(state["nodes"][0]["worktree_path"])
    (worktree / "bucket-a.txt").write_text("staged\n", encoding="utf-8", newline="\n")
    _git(worktree, "add", "bucket-a.txt")
    _write_other_children(tmp_path, state, "FIXTURE-WRITE-A")

    with pytest.raises(lifecycle.PilotError, match="^child-index-change-forbidden:FIXTURE-WRITE-A$"):
        _join(lifecycle, state_path, state, tmp_path)

    assert not (state_path.parent / "patch-a.diff").exists()


@pytest.mark.installed_plugin
@pytest.mark.integration
def test_collect_rejects_deletion_patch_shape(tmp_path: Path) -> None:
    """Reject deletion even when it stays in the owned bucket."""
    lifecycle, state_path, state, _ = _prepare(tmp_path)
    worktree = tmp_path / str(state["nodes"][0]["worktree_path"])
    target = worktree / "bucket-a.txt"
    target.unlink()
    _write_other_children(tmp_path, state, "FIXTURE-WRITE-A")

    with pytest.raises(lifecycle.PilotError, match="^child-patch-shape-forbidden:FIXTURE-WRITE-A$"):
        _join(lifecycle, state_path, state, tmp_path)


@pytest.mark.installed_plugin
@pytest.mark.integration
def test_raw_content_updates_rejects_mode_only_metadata_without_filemode_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject a Git-reported mode-only patch without relying on chmod tracking."""
    lifecycle, _, state, _ = _prepare(tmp_path)
    node = state["nodes"][0]
    worktree = tmp_path / str(node["worktree_path"])
    real_git_bytes = lifecycle._git_bytes

    def _mode_only_raw(target: Path, *arguments: str) -> bytes:
        """Return mode-only raw diff evidence for patch-shape rejection."""
        if target == worktree and arguments[:3] == ("diff", "--raw", "--no-renames"):
            return b":100644 100755 deadbeef deadbeef M\0bucket-a.txt\0"
        return real_git_bytes(target, *arguments)

    monkeypatch.setattr(lifecycle, "_git_bytes", _mode_only_raw)

    with pytest.raises(lifecycle.PilotError, match="^child-patch-shape-forbidden:FIXTURE-WRITE-A$"):
        lifecycle._raw_content_updates(worktree, node)


@pytest.mark.installed_plugin
@pytest.mark.integration
@pytest.mark.parametrize(
    ("mutation", "error"),
    (
        pytest.param("missing", "child-handovers-incomplete", id="missing"),
        pytest.param("duplicate", "child-handover-invalid", id="duplicate"),
        pytest.param("extra-field", "child-handover-invalid", id="extra-field"),
        pytest.param("failed", "child-handover-not-completed:FIXTURE-WRITE-A", id="failed"),
        pytest.param("cancelled", "child-handover-not-completed:FIXTURE-WRITE-A", id="cancelled"),
        pytest.param("empty-summary", "child-handover-summary-invalid:FIXTURE-WRITE-A", id="empty-summary"),
        pytest.param("wrong-path", "child-handover-paths-mismatch:FIXTURE-WRITE-A", id="wrong-path"),
        pytest.param("wrong-hash", "child-handover-patch-mismatch:FIXTURE-WRITE-A", id="wrong-hash"),
    ),
)
def test_join_rejects_incomplete_or_mismatched_child_handover(tmp_path: Path, mutation: str, error: str) -> None:
    """Reject partial, unsuccessful, empty, or Git-inconsistent child reports."""
    lifecycle, state_path, state, _ = _prepare(tmp_path)
    for node in state["nodes"]:
        worktree = tmp_path / str(node["worktree_path"])
        (worktree / str(node["owned_paths"][0])).write_text("child\n", encoding="utf-8", newline="\n")
    handovers = [_child_handover(tmp_path, node) for node in state["nodes"]]
    if mutation == "missing":
        handovers.pop()
    elif mutation == "duplicate":
        handovers[1] = dict(handovers[0])
    elif mutation == "extra-field":
        handovers[0]["unexpected"] = True
    elif mutation in {"failed", "cancelled"}:
        handovers[0]["status"] = mutation
    elif mutation == "empty-summary":
        handovers[0]["summary"] = ""
    elif mutation == "wrong-path":
        handovers[0]["changed_paths"] = ["bucket-b.txt"]
    else:
        handovers[0]["patch_sha256"] = "0" * 64

    with pytest.raises(lifecycle.PilotError, match=rf"^{error}$"):
        lifecycle.join_child_handovers(state_path=state_path, handovers=handovers)

    failed_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert failed_state["status"] == "join-failed"
    assert failed_state["join_failure"]["reason"] == error
    if mutation in {"failed", "cancelled"}:
        assert failed_state["join_failure"]["failed_node"] == "FIXTURE-WRITE-A"
        assert failed_state["join_failure"]["received_reports"][0]["status"] == mutation
        assert failed_state["join_failure"]["received_reports"][0]["summary"] == "Completed FIXTURE-WRITE-A."
    assert not (state_path.parent / "patch-a.diff").exists()
    assert all((tmp_path / str(node["worktree_path"])).exists() for node in state["nodes"])


@pytest.mark.installed_plugin
@pytest.mark.integration
def test_collect_rejects_worktree_drift_after_parent_join(tmp_path: Path) -> None:
    """Recheck the reported patch hash immediately before parent collection."""
    lifecycle, state_path, state, _ = _prepare(tmp_path)
    for node in state["nodes"]:
        worktree = tmp_path / str(node["worktree_path"])
        (worktree / str(node["owned_paths"][0])).write_text("joined\n", encoding="utf-8", newline="\n")
    _join(lifecycle, state_path, state, tmp_path)
    worktree = tmp_path / str(state["nodes"][0]["worktree_path"])
    (worktree / "bucket-a.txt").write_text("drifted-after-join\n", encoding="utf-8", newline="\n")

    with pytest.raises(lifecycle.PilotError, match="^child-handover-drift:FIXTURE-WRITE-A$"):
        lifecycle.collect_write_patch(state_path=state_path, node_id="FIXTURE-WRITE-A")

    assert not (state_path.parent / "patch-a.diff").exists()


@pytest.mark.installed_plugin
@pytest.mark.integration
def test_collect_rejects_fabricated_operation_api(tmp_path: Path) -> None:
    """Keep caller-created operation dictionaries outside the collector API."""
    lifecycle, state_path, state, _ = _prepare(tmp_path)
    worktree = tmp_path / str(state["nodes"][0]["worktree_path"])
    (worktree / "bucket-a.txt").write_text("child-a\n", encoding="utf-8", newline="\n")
    with pytest.raises(TypeError, match="unexpected keyword argument 'operation'"):
        lifecycle.collect_write_patch(
            state_path=state_path,
            node_id="FIXTURE-WRITE-A",
            operation={},
        )


@pytest.mark.installed_plugin
@pytest.mark.integration
def test_integrate_requires_every_completed_joined_patch(tmp_path: Path) -> None:
    """Prevent integration before both child reports and patches are verified."""
    lifecycle, state_path, state, repository = _prepare(tmp_path)

    with pytest.raises(lifecycle.PilotError, match="^pilot-children-not-joined$"):
        lifecycle.integrate_write_pilot(state_path=state_path)

    assert (repository / "bucket-a.txt").read_text(encoding="utf-8") == "baseline-a\n"


@pytest.mark.installed_plugin
@pytest.mark.integration
def test_integrate_applies_parent_derived_patches_in_lexical_order(tmp_path: Path) -> None:
    """Apply both hash-bound patches to an isolated integration worktree in stable order."""
    lifecycle, state_path, state, repository = _prepare(tmp_path)
    for node, content in zip(state["nodes"], ("child-a\n", "child-b\n"), strict=True):
        worktree = tmp_path / str(node["worktree_path"])
        owned_path = str(node["owned_paths"][0])
        (worktree / owned_path).write_text(content, encoding="utf-8", newline="\n")
    _join(lifecycle, state_path, state, tmp_path)
    for node in state["nodes"]:
        patch = _collect(lifecycle, state_path, state, node)
        patch_path = tmp_path / str(patch["patch_path"])
        assert _sha256(patch_path) == patch["patch_sha256"]

    result = lifecycle.integrate_write_pilot(state_path=state_path)
    integration = tmp_path / str(result["integration_worktree"])

    assert result["integration_order"] == ["FIXTURE-WRITE-A", "FIXTURE-WRITE-B"]
    assert (integration / "bucket-a.txt").read_text(encoding="utf-8") == "child-a\n"
    assert (integration / "bucket-b.txt").read_text(encoding="utf-8") == "child-b\n"
    assert (repository / "bucket-a.txt").read_text(encoding="utf-8") == "baseline-a\n"


@pytest.mark.installed_plugin
@pytest.mark.integration
def test_plan_drift_after_join_blocks_integration_and_retains_worktrees(tmp_path: Path) -> None:
    """Rehash immutable authority after joins before applying any child patch."""
    lifecycle, state_path, state, _ = _prepare(tmp_path)
    for node in state["nodes"]:
        worktree = tmp_path / str(node["worktree_path"])
        owned_path = str(node["owned_paths"][0])
        (worktree / owned_path).write_text(f"{node['node_id']}\n", encoding="utf-8", newline="\n")
    _join(lifecycle, state_path, state, tmp_path)
    for node in state["nodes"]:
        _collect(lifecycle, state_path, state, node)
    plan_path = tmp_path / str(state["plan_path"])
    plan_path.write_text(plan_path.read_text(encoding="utf-8") + "\n", encoding="utf-8", newline="\n")

    with pytest.raises(lifecycle.PilotError, match="^plan-approval-digest-mismatch$"):
        lifecycle.integrate_write_pilot(state_path=state_path)

    assert all((tmp_path / str(node["worktree_path"])).exists() for node in state["nodes"])


@pytest.mark.installed_plugin
@pytest.mark.integration
@pytest.mark.parametrize("drift", ["approval", "source"])
def test_authority_or_source_drift_blocks_transition(tmp_path: Path, drift: str) -> None:
    """Reject approval-byte or generated-source drift before another Git transition."""
    lifecycle, state_path, state, repository = _prepare(tmp_path)
    for node in state["nodes"]:
        worktree = tmp_path / str(node["worktree_path"])
        (worktree / str(node["owned_paths"][0])).write_text("child\n", encoding="utf-8", newline="\n")
    _join(lifecycle, state_path, state, tmp_path)
    if drift == "approval":
        approval = tmp_path / str(state["approval_path"])
        approval.write_text(approval.read_text(encoding="utf-8") + "\n", encoding="utf-8", newline="\n")
        expected = "approval-drift-before-transition"
    else:
        (repository / "bucket-a.txt").write_text("source-drift\n", encoding="utf-8", newline="\n")
        expected = "fixture-repository-dirty"

    with pytest.raises(lifecycle.PilotError, match=rf"^{expected}$"):
        _collect(lifecycle, state_path, state, state["nodes"][0])

    assert not (state_path.parent / "patch-a.diff").exists()


@pytest.mark.installed_plugin
@pytest.mark.integration
def test_patch_digest_tamper_blocks_integration(tmp_path: Path) -> None:
    """Reject changed patch bytes before creating an integration worktree."""
    lifecycle, state_path, state, _ = _prepare(tmp_path)
    for node in state["nodes"]:
        worktree = tmp_path / str(node["worktree_path"])
        (worktree / str(node["owned_paths"][0])).write_text("child\n", encoding="utf-8", newline="\n")
    _join(lifecycle, state_path, state, tmp_path)
    for node in state["nodes"]:
        _collect(lifecycle, state_path, state, node)
    patch = state_path.parent / "patch-a.diff"
    patch.write_bytes(patch.read_bytes() + b"\n")

    with pytest.raises(lifecycle.PilotError, match="^patch-digest-mismatch:FIXTURE-WRITE-A$"):
        lifecycle.integrate_write_pilot(state_path=state_path)

    assert not (tmp_path / str(state["worktree_root"]) / "integration").exists()


@pytest.mark.installed_plugin
@pytest.mark.integration
def test_conflicting_patch_retains_failed_integration(tmp_path: Path) -> None:
    """Retain the integration worktree when a hash-bound later patch conflicts."""
    lifecycle, state_path, state, repository = _prepare(tmp_path)
    for node in state["nodes"]:
        worktree = tmp_path / str(node["worktree_path"])
        (worktree / str(node["owned_paths"][0])).write_text("child\n", encoding="utf-8", newline="\n")
    _join(lifecycle, state_path, state, tmp_path)
    for node in state["nodes"]:
        _collect(lifecycle, state_path, state, node)
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    patch_a = state_path.parent / "patch-a.diff"
    patch_b = state_path.parent / "patch-b.diff"
    patch_b.write_bytes(patch_a.read_bytes())
    persisted["nodes"][1]["patch_sha256"] = _sha256(patch_b)
    state_path.write_text(json.dumps(persisted, indent=2) + "\n", encoding="utf-8", newline="\n")

    with pytest.raises(lifecycle.PilotError, match="^patch-integration-failed:FIXTURE-WRITE-B$"):
        lifecycle.integrate_write_pilot(state_path=state_path)

    retained = json.loads(state_path.read_text(encoding="utf-8"))
    assert retained["integration_status"] == "failed"
    assert (tmp_path / str(retained["integration_worktree"])).exists()
    assert all((tmp_path / str(node["worktree_path"])).exists() for node in retained["nodes"])
    assert (repository / "bucket-a.txt").read_text(encoding="utf-8") == "baseline-a\n"


@pytest.mark.installed_plugin
@pytest.mark.integration
def test_cleanup_removes_only_persisted_successful_worktrees(tmp_path: Path) -> None:
    """Remove generated worktrees only after integration evidence is durable."""
    lifecycle, state_path, state, _ = _prepare(tmp_path)
    for node, content in zip(state["nodes"], ("child-a\n", "child-b\n"), strict=True):
        worktree = tmp_path / str(node["worktree_path"])
        (worktree / str(node["owned_paths"][0])).write_text(content, encoding="utf-8", newline="\n")
    _join(lifecycle, state_path, state, tmp_path)
    for node in state["nodes"]:
        _collect(lifecycle, state_path, state, node)
    result = lifecycle.integrate_write_pilot(state_path=state_path)

    cleanup = lifecycle.cleanup_write_pilot(state_path=state_path)

    assert cleanup["cleanup_status"] == "removed"
    paths = [*(tmp_path / str(node["worktree_path"]) for node in state["nodes"])]
    paths.append(tmp_path / str(result["integration_worktree"]))
    assert all(not path.exists() for path in paths)
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["cleanup_status"] == "removed"
    assert all(item["postcondition"] == "absent" for item in persisted["cleanup"])


@pytest.mark.installed_plugin
@pytest.mark.integration
def test_cleanup_failure_is_retained_and_blocks_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Record non-force cleanup failure and retain every remaining diagnostic path."""
    lifecycle, state_path, state, _ = _prepare(tmp_path)
    for node in state["nodes"]:
        worktree = tmp_path / str(node["worktree_path"])
        (worktree / str(node["owned_paths"][0])).write_text("child\n", encoding="utf-8", newline="\n")
    _join(lifecycle, state_path, state, tmp_path)
    for node in state["nodes"]:
        _collect(lifecycle, state_path, state, node)
    lifecycle.integrate_write_pilot(state_path=state_path)
    real_git = lifecycle._git

    def _fail_remove(repository: Path, *arguments: str) -> str:
        """Inject cleanup failure at worktree removal and delegate other Git commands."""
        if arguments[:2] == ("worktree", "remove"):
            raise lifecycle.PilotError("simulated-cleanup-lock")
        return real_git(repository, *arguments)

    monkeypatch.setattr(lifecycle, "_git", _fail_remove)
    with pytest.raises(lifecycle.PilotError, match="^cleanup-failed:FIXTURE-WRITE-A$"):
        lifecycle.cleanup_write_pilot(state_path=state_path)

    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["cleanup_status"] == "failed"
    assert (tmp_path / str(state["nodes"][0]["worktree_path"])).exists()


@pytest.mark.installed_plugin
@pytest.mark.integration
@pytest.mark.skipif(not SYMLINKS_SUPPORTED, reason="directory symlink capability unavailable")
def test_prepare_rejects_symlinked_generated_root_when_supported(tmp_path: Path) -> None:
    """Prevent a managed worktree path from escaping through a symlink component."""
    lifecycle = _load_lifecycle()
    plan_path, approval_path, _ = _approved_plan(tmp_path)
    generated = tmp_path / ".reports" / "codex" / "develop" / "fixture"
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = generated / "worktrees"
    alias.symlink_to(outside, target_is_directory=True)

    with pytest.raises(lifecycle.PilotError, match="^managed-path-symlink-forbidden$"):
        lifecycle.prepare_write_pilot(
            plan_path=plan_path,
            approval_path=approval_path,
            workspace_root=tmp_path,
            state_path=tmp_path / ".reports" / "codex" / "develop" / "state.json",
        )


@pytest.mark.installed_plugin
@pytest.mark.integration
def test_git_runner_sanitizes_git_environment_without_shell(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Remove Git overrides while preserving Windows-required child variables."""
    lifecycle = _load_lifecycle()
    observed: list[dict[str, object]] = []

    def _record_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Capture subprocess environment while returning a successful Git result."""
        observed.append(kwargs)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="head\n", stderr="")

    monkeypatch.setattr(lifecycle.subprocess, "run", _record_run)

    monkeypatch.setenv("GIT_DIR", "redirected")
    monkeypatch.setenv("git_work_tree", "redirected")
    monkeypatch.setenv("SYSTEMROOT", "C:\\Windows")
    monkeypatch.setenv("COMSPEC", "cmd.exe")
    lifecycle._git(tmp_path, "rev-parse", "HEAD")

    assert observed
    environment = observed[0]["env"]
    assert isinstance(environment, dict)
    assert not any(key.upper().startswith("GIT_") for key in environment)
    assert environment["SYSTEMROOT"] == "C:\\Windows"
    assert environment["COMSPEC"] == "cmd.exe"
    assert observed[0]["shell"] is False
    assert os.environ is not environment

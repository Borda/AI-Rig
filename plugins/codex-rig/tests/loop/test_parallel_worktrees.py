"""Acceptance checks for the ``code-remediate`` parallel worktree lifecycle from preparation to crash recovery.

Each test drives the real lifecycle module (``shared/parallel_worktrees.py``) against throwaway Git repositories built
from committed templates, so Git behaviour is part of the contract; only a single failing ``_git`` step or an OS error
is ever replaced. This module covers the ``code-remediate`` production plan (``prepare_code_remediate_pilot``): plan
admission, the worktree root, transition rechecks, overlapping buckets, the guarded source apply in patch and grouped
commit modes, and recovery after a crash around any Git effect. The run-wide apply lock, the CLI lifecycle, the
generated-fixture ``develop`` pilot and the Git helpers are checked in ``test_parallel_worktrees_lock.py``.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from _artifact_files import read_json
from _parallel_worktrees_support import (
    _DELIVERY_MODES,
    CHILD_EDITS,
    PLUGIN_ROOT,
    GatedGroupedRemediation,
    Pilot,
    RepositoryTemplates,
    _apply_statuses,
    _approved_code_remediate_plan,
    _assert_completed_lifecycle,
    _assert_completed_lifecycle_after_cleanup,
    _cleanup_targets,
    _collect_all,
    _crash_after_patch_output,
    _crash_around_git,
    _crash_inside_worktree_removal,
    _create_handovers,
    _delivery_route,
    _DeliveryRoute,
    _edit_owned,
    _git,
    _hook_after_source_apply,
    _is_source_apply,
    _is_source_merge,
    _is_worktree_command,
    _node_worktrees,
    _overlapping_code_remediate_plan,
    _prepare_integrated_code_remediate,
    _prepare_remediation,
    _raise,
    _read,
    _registered_worktrees,
    _rewrite_plan,
    _run_grouped_gates,
    _sha256,
    _SimulatedCrash,
    _skip_no_symlinks,
    _write,
    _write_in_worktrees,
    _write_json,
)


def _tree_snapshot(root: Path) -> dict[str, bytes] | None:
    """Return every file under ``root`` keyed by relative path, or None when ``root`` does not exist."""
    if not root.exists():
        return None
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def _point_worktree_root(plan_path: Path, approval_path: Path, repository: Path, root: Path) -> None:
    """Rebind a remediation plan's worktree root, written relative to the workspace holding the repository."""
    plan = read_json(plan_path)
    plan["worktree_root"] = root.relative_to(repository.parent).as_posix()
    _rewrite_plan(plan_path, approval_path, plan)


def _use_source_local_worktree_root(plan_path: Path, approval_path: Path, repository: Path) -> Path:
    """Bind a remediation plan to an ignored worktree root inside its source repository."""
    root = repository / ".reports" / "codex" / "code-remediate-worktrees" / "fixture"
    with (repository / ".git" / "info" / "exclude").open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(".reports/codex/code-remediate-worktrees/\n")
    _point_worktree_root(plan_path, approval_path, repository, root)
    return root


def _rewrite_owned_as_crlf(repository: Path) -> None:
    """Rewrite both owned source files with CRLF line endings, as a clean-filter checkout would leave them."""
    for relative in ("bucket-a.txt", "bucket-b.txt"):
        path = repository / relative
        path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))


# --- case builders: plan and repository mutations selected by parametrized tests -----------------------------------


def _delete_context(context_path: Path) -> None:
    """Remove a bucket's frozen context pack."""
    context_path.unlink()


def _change_context(context_path: Path) -> None:
    """Change a bucket's frozen context pack bytes."""
    _write(context_path, "changed context\n")


def _other_state_name(state_path: Path) -> Path:
    """Select a state file name the plan does not bind."""
    return state_path.with_name("other-lifecycle.json")


def _precreate_state(state_path: Path) -> Path:
    """Pre-create the lifecycle state file with untrusted content."""
    _write(state_path, '{"untrusted": true}\n')
    return state_path


def _precreate_state_temporary(state_path: Path) -> Path:
    """Pre-create the temporary file the state writer would use."""
    _write(state_path.with_name(f".{state_path.name}.tmp"), "untrusted\n")
    return state_path


def _precreate_output(state_path: Path) -> Path:
    """Pre-create a bucket output file inside the evidence directory."""
    _write(state_path.with_name("patch-a.diff"), "untrusted\n")
    return state_path


def _dirty_owned_worktree(repository: Path) -> None:
    """Leave an unstaged edit on an approved path."""
    _write(repository / "bucket-a.txt", "dirty\n")


def _stage_owned_edit(repository: Path) -> None:
    """Stage an edit on an approved path."""
    _write(repository / "bucket-a.txt", "staged\n")
    _git(repository, "add", "bucket-a.txt")


def _rename_owned_path(repository: Path) -> None:
    """Rename an approved path in the index."""
    _git(repository, "mv", "bucket-a.txt", "renamed.txt")


def _start_conflicting_merge(repository: Path) -> None:
    """Leave the source repository in the middle of a conflicted merge."""
    base_branch = _git(repository, "branch", "--show-current").stdout.strip()
    _git(repository, "checkout", "-q", "-b", "conflicting-change")
    _write(repository / "bucket-a.txt", "other branch\n")
    _git(repository, "add", "bucket-a.txt")
    _git(repository, "commit", "-q", "-m", "conflicting change")
    _git(repository, "checkout", "-q", base_branch)
    _write(repository / "bucket-a.txt", "source branch\n")
    _git(repository, "add", "bucket-a.txt")
    _git(repository, "commit", "-q", "-m", "source change")
    assert _git(repository, "merge", "conflicting-change", check=False).returncode != 0


def _root_not_ignored(plan_path: Path, approval_path: Path, repository: Path) -> Path:
    """Bind the plan to a source-local worktree root that Git does not ignore."""
    root = repository / ".reports" / "codex" / "code-remediate-worktrees" / "fixture"
    _point_worktree_root(plan_path, approval_path, repository, root)
    return root


def _root_outside_managed_prefix(plan_path: Path, approval_path: Path, repository: Path) -> Path:
    """Bind the plan to an ignored-looking root outside the managed ``code-remediate-worktrees`` prefix."""
    _use_source_local_worktree_root(plan_path, approval_path, repository)
    root = repository / ".reports" / "codex" / "develop-worktrees" / "fixture"
    _point_worktree_root(plan_path, approval_path, repository, root)
    return root


def _root_overlapping_evidence(plan_path: Path, approval_path: Path, repository: Path) -> Path:
    """Bind the plan to the directory that already holds its own evidence."""
    _use_source_local_worktree_root(plan_path, approval_path, repository)
    _point_worktree_root(plan_path, approval_path, repository, plan_path.parent)
    return plan_path.parent


def _root_preexisting(plan_path: Path, approval_path: Path, repository: Path) -> Path:
    """Bind the plan to a valid source-local root that already holds a user file."""
    root = _use_source_local_worktree_root(plan_path, approval_path, repository)
    root.mkdir(parents=True)
    (root / "existing.txt").write_bytes(b"must survive\n")
    return root


# --- code-remediate: plan preparation -------------------------------------------------------------------------------


@pytest.mark.installed_plugin
@pytest.mark.integration
class TestCodeRemediatePrepare:
    """A schema-2 remediation plan is admitted only when digest-bound, clean and unambiguous."""

    def test_accepts_clean_digest_bound_plan(
        self, lifecycle: ModuleType, tmp_path: Path, repository_templates: RepositoryTemplates
    ) -> None:
        """Prepare detached remediation worktrees from one clean, approval-bound source baseline.

        A committed source repository holds the approved plan and context packs as untracked evidence. Preparation must
        record the digests and baseline, create children outside the source, and leave the source untouched.
        """
        plan_path, approval_path, repository = _approved_code_remediate_plan(tmp_path, repository_templates)
        state_path = plan_path.parent / "production-lifecycle.json"

        state = _prepare_remediation(lifecycle, tmp_path, plan_path, approval_path)

        assert state["schema_version"] == 2
        assert state["status"] == "prepared"
        assert state["source_repository"] == "authoritative-repository"
        assert state["plan_sha256"] == _sha256(plan_path)
        assert state["approval_sha256"] == _sha256(approval_path)
        assert state["baseline_head"] == _git(repository, "rev-parse", "HEAD").stdout.strip()
        assert state["baseline_tree"] == _git(repository, "rev-parse", "HEAD^{tree}").stdout.strip()
        assert state_path.is_relative_to(repository)
        assert [node["owned_paths"] for node in state["nodes"]] == [["bucket-a.txt"], ["bucket-b.txt"]]
        assert all(not worktree.is_relative_to(repository) for worktree in _node_worktrees(tmp_path, state))
        assert all(
            _git(worktree, "symbolic-ref", "-q", "HEAD", check=False).returncode == 1
            for worktree in _node_worktrees(tmp_path, state)
        )
        assert set(_git(repository, "status", "--porcelain=v1", "--untracked-files=all").stdout.splitlines()) == {
            "?? .reports/codex/code-remediate/fixture/WRITE-A-context.md",
            "?? .reports/codex/code-remediate/fixture/WRITE-B-context.md",
            "?? .reports/codex/code-remediate/fixture/parallel-approval.json",
            "?? .reports/codex/code-remediate/fixture/production-lifecycle.json",
            "?? .reports/codex/code-remediate/fixture/work-bucket-plan.json",
        }

    def test_accepts_digest_bound_workflow_default(
        self, lifecycle: ModuleType, tmp_path: Path, repository_templates: RepositoryTemplates
    ) -> None:
        """Record workflow authority without claiming a per-plan approval prompt.

        An approval whose source is ``workflow-default`` and whose prompt flag is false still binds the plan digest, so
        preparation succeeds and records the approval digest.
        """
        plan_path, approval_path, repository = _approved_code_remediate_plan(tmp_path, repository_templates)
        approval = read_json(approval_path)
        approval.update({"source": "workflow-default", "prompt_presented": False})
        _write_json(approval_path, approval)

        state = _prepare_remediation(lifecycle, tmp_path, plan_path, approval_path)

        assert state["approval_sha256"] == _sha256(approval_path)
        assert state["plan_sha256"] == approval["plan_sha256"]
        assert len(state["nodes"]) == 2
        assert _read(repository / "bucket-a.txt") == "baseline-a\n"

    def test_rejects_workflow_default_claiming_a_prompt(
        self, lifecycle: ModuleType, tmp_path: Path, repository_templates: RepositoryTemplates
    ) -> None:
        """Keep workflow authority distinct from an exact per-plan user prompt.

        An approval that says ``workflow-default`` while still claiming the prompt was presented is self-contradictory
        and must not authorize dispatch.
        """
        plan_path, approval_path, _ = _approved_code_remediate_plan(tmp_path, repository_templates)
        approval = read_json(approval_path)
        approval["source"] = "workflow-default"
        _write_json(approval_path, approval)

        with pytest.raises(lifecycle.PilotError, match=r"^write-approval-invalid$"):
            _prepare_remediation(lifecycle, tmp_path, plan_path, approval_path)

    @pytest.mark.parametrize(
        ("change_context", "expected"),
        [
            pytest.param(_delete_context, "context-pack-missing:WRITE-A", id="missing"),
            pytest.param(_change_context, "context-pack-digest-mismatch:WRITE-A", id="digest-mismatch"),
        ],
    )
    def test_binds_each_context_pack(
        self,
        lifecycle: ModuleType,
        tmp_path: Path,
        repository_templates: RepositoryTemplates,
        change_context: Callable[[Path], None],
        expected: str,
    ) -> None:
        """Reject dispatch when an approved bucket lacks its exact source-local context bytes.

        The plan is re-approved after the context pack is removed or edited, so the failure comes from the per-bucket
        context digest and not from the plan digest.
        """
        plan_path, approval_path, _ = _approved_code_remediate_plan(tmp_path, repository_templates)
        plan = read_json(plan_path)
        change_context(plan_path.parent / str(plan["work_buckets"][0]["context_pack_path"]))
        _rewrite_plan(plan_path, approval_path, plan)

        with pytest.raises(lifecycle.PilotError, match=rf"^{expected}$"):
            _prepare_remediation(lifecycle, tmp_path, plan_path, approval_path)

        assert not (tmp_path / ".codex-rig-worktrees" / "fixture").exists()

    @pytest.mark.parametrize(
        ("choose_state_path", "expected"),
        [
            pytest.param(_other_state_name, "lifecycle-state-plan-mismatch", id="different-plan-path"),
            pytest.param(_precreate_state, "lifecycle-state-exists", id="preexisting-state"),
            pytest.param(_precreate_state_temporary, "state-temporary-exists", id="preexisting-state-temporary"),
            pytest.param(_precreate_output, "lifecycle-output-exists:patch-a.diff", id="preexisting-output"),
        ],
    )
    def test_requires_one_plan_bound_new_state_path(
        self,
        lifecycle: ModuleType,
        tmp_path: Path,
        repository_templates: RepositoryTemplates,
        choose_state_path: Callable[[Path], Path],
        expected: str,
    ) -> None:
        """Reject caller-selected state or pre-existing lifecycle output before creating worktrees.

        Preparation owns exactly one fresh state path named by the plan; a different name, or any file already sitting
        where lifecycle output will be written, could be attacker-controlled and must stop the run.
        """
        plan_path, approval_path, _ = _approved_code_remediate_plan(tmp_path, repository_templates)
        state_path = choose_state_path(plan_path.parent / "production-lifecycle.json")

        with pytest.raises(lifecycle.PilotError, match=rf"^{expected}$"):
            _prepare_remediation(lifecycle, tmp_path, plan_path, approval_path, state_path)

        assert not (tmp_path / ".codex-rig-worktrees" / "fixture").exists()

    def test_uses_fixed_parent_gate_reference(
        self, lifecycle: ModuleType, tmp_path: Path, repository_templates: RepositoryTemplates
    ) -> None:
        """Keep structural integration distinct from the parent-owned shared quality-gate execution.

        Buckets carry no shell verification commands; the plan names one fixed parent gate and preparation must carry
        that reference into the state unchanged.
        """
        plan_path, approval_path, _ = _approved_code_remediate_plan(tmp_path, repository_templates)
        plan = read_json(plan_path)
        assert plan["verification_gate"] == "code-remediate-shared-quality-gates"
        assert all("verification_commands" not in bucket for bucket in plan["work_buckets"])

        state = _prepare_remediation(lifecycle, tmp_path, plan_path, approval_path)

        assert state["verification_gate"] == "code-remediate-shared-quality-gates"

    def test_rejects_plan_changed_after_approval(
        self, lifecycle: ModuleType, tmp_path: Path, repository_templates: RepositoryTemplates
    ) -> None:
        """Prevent approval of one remediation bucket contract authorizing another.

        A single appended newline changes the plan bytes after approval, so the stored digest no longer matches.
        """
        plan_path, approval_path, _ = _approved_code_remediate_plan(tmp_path, repository_templates)
        _write(plan_path, _read(plan_path) + "\n")

        with pytest.raises(lifecycle.PilotError, match=r"^plan-approval-digest-mismatch$"):
            _prepare_remediation(lifecycle, tmp_path, plan_path, approval_path)

    @pytest.mark.parametrize(
        ("damage_source", "expected"),
        [
            pytest.param(_dirty_owned_worktree, "source-repository-dirty", id="owned-worktree"),
            pytest.param(_stage_owned_edit, "source-repository-dirty", id="owned-index"),
            pytest.param(_rename_owned_path, "owned-path-not-regular:WRITE-A", id="owned-rename"),
            pytest.param(_start_conflicting_merge, "source-repository-merging", id="merging"),
        ],
    )
    def test_rejects_unsafe_source_before_dispatch(
        self,
        lifecycle: ModuleType,
        tmp_path: Path,
        repository_templates: RepositoryTemplates,
        damage_source: Callable[[Path], None],
        expected: str,
    ) -> None:
        """Block dispatch for approved-path drift or an unresolved Git operation.

        The source repository is damaged in one way after approval (dirty or staged owned file, renamed owned file,
        conflicted merge); none of these may reach worktree creation.
        """
        plan_path, approval_path, repository = _approved_code_remediate_plan(tmp_path, repository_templates)
        damage_source(repository)

        with pytest.raises(lifecycle.PilotError, match=rf"^{expected}$"):
            _prepare_remediation(lifecycle, tmp_path, plan_path, approval_path)

        assert not (tmp_path / ".codex-rig-worktrees" / "fixture").exists()

    def test_rejects_unrelated_unmerged_index(
        self, lifecycle: ModuleType, tmp_path: Path, repository_templates: RepositoryTemplates
    ) -> None:
        """Block dispatch when Git has unresolved index stages without an operation marker.

        Two stages for an unrelated path are injected straight into the index; with no ``MERGE_HEAD`` present, only the
        unmerged-entry check can catch the unresolved state.
        """
        plan_path, approval_path, repository = _approved_code_remediate_plan(tmp_path, repository_templates)
        blob = _git(repository, "rev-parse", "HEAD:bucket-a.txt").stdout.strip()
        entries = f"100644 {blob} 1\tunrelated.txt\n100644 {blob} 2\tunrelated.txt\n"
        # Binary stdin preserves Git's LF record delimiters on Windows.
        subprocess.run(
            ["git", "-C", str(repository), "update-index", "--index-info"],
            input=entries.encode("ascii"),
            check=True,
            capture_output=True,
        )
        assert _git(repository, "ls-files", "--unmerged").stdout == entries

        with pytest.raises(lifecycle.PilotError, match=r"^source-repository-unmerged$"):
            _prepare_remediation(lifecycle, tmp_path, plan_path, approval_path)

        assert not (tmp_path / ".codex-rig-worktrees" / "fixture").exists()


@pytest.mark.installed_plugin
@pytest.mark.integration
class TestCodeRemediateWorktreeRoot:
    """The worktree root a remediation plan names must be a fresh, managed, non-escaping location."""

    @pytest.mark.parametrize(
        "root_name",
        [
            pytest.param(".codex-rig-worktrees/fixture", id="external-sibling"),
            pytest.param("authoritative-repository/.reports/codex/code-remediate-worktrees/fixture", id="source-local"),
        ],
    )
    def test_protected_interval_detects_same_size_sibling_draft_change(
        self, lifecycle: ModuleType, tmp_path: Path, repository_templates: RepositoryTemplates, root_name: str
    ) -> None:
        """Per-worktree snapshots detect a rewrite of a paused sibling's already dirty tracked file in either layout."""
        plan_path, approval_path, source = _approved_code_remediate_plan(tmp_path, repository_templates)
        _use_source_local_worktree_root(plan_path, approval_path, source)
        _point_worktree_root(plan_path, approval_path, source, tmp_path / root_name)
        state = _prepare_remediation(lifecycle, tmp_path, plan_path, approval_path)
        active, sibling = _node_worktrees(tmp_path, state)
        draft = sibling / "bucket-b.txt"
        draft.write_bytes(b"first draft\n")
        evidence = plan_path.parent / "protected-interval"
        evidence.mkdir()
        guard = [sys.executable, str(PLUGIN_ROOT / "shared" / "contributor_run_integrity.py")]

        def snapshot(checkout: Path) -> list[str]:
            """Take one baseline and retain the verifier invocation anchored to its printed digest."""
            snapshot_file = evidence / f"integrity-{checkout.name}.json"
            run = subprocess.run(
                [
                    *guard,
                    "snapshot",
                    "--repo",
                    checkout,
                    "--watch-surface",
                    "--main-checkout",
                    source,
                    "--out",
                    snapshot_file,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            return [*guard, "verify", "--snapshot", str(snapshot_file), "--expect", json.loads(run.stdout)["sha256"]]

        active_verify, sibling_verify = snapshot(active), snapshot(sibling)
        subprocess.run(active_verify, check=True, capture_output=True)
        subprocess.run(sibling_verify, check=True, capture_output=True)
        draft.write_bytes(b"other draft\n")

        mismatch = subprocess.run(sibling_verify, check=False, capture_output=True, text=True)

        assert mismatch.returncode == 3, mismatch.stderr
        assert "review:diff" in json.loads(mismatch.stdout)["changed"]

    @pytest.mark.parametrize(
        ("bind_root", "expected"),
        [
            pytest.param(_root_not_ignored, "worktree-root-not-ignored", id="not-ignored"),
            pytest.param(
                _root_outside_managed_prefix, "worktree-root-not-managed-sibling", id="outside-managed-prefix"
            ),
            pytest.param(_root_overlapping_evidence, "worktree-root-not-managed-sibling", id="evidence-overlap"),
            pytest.param(_root_preexisting, "worktree-root-exists", id="preexisting"),
        ],
    )
    def test_rejects_unsafe_source_local_root(
        self,
        lifecycle: ModuleType,
        tmp_path: Path,
        repository_templates: RepositoryTemplates,
        bind_root: Callable[[Path, Path, Path], Path],
        expected: str,
    ) -> None:
        """Reject source-local roots that can expose outputs or overwrite existing evidence.

        Each case binds the plan to a root that is unignored, outside the managed prefix, the evidence directory itself,
        or already populated. Rejection must leave that root exactly as it was (absent, or with its files intact).
        """
        plan_path, approval_path, repository = _approved_code_remediate_plan(tmp_path, repository_templates)
        root = bind_root(plan_path, approval_path, repository)
        before = _tree_snapshot(root)

        with pytest.raises(lifecycle.PilotError, match=rf"^{expected}$"):
            _prepare_remediation(lifecycle, tmp_path, plan_path, approval_path)

        assert _tree_snapshot(root) == before
        assert plan_path.exists()

    def test_rejects_casefolded_source_local_root_alias(
        self, lifecycle: ModuleType, tmp_path: Path, repository_templates: RepositoryTemplates
    ) -> None:
        """Reject a case-altered source prefix that can name a sibling on case-sensitive hosts.

        The root repeats the managed path under an upper-cased repository directory name; on a case-insensitive host
        that is the source itself, on a case-sensitive one a different directory, so it is refused either way.
        """
        plan_path, approval_path, repository = _approved_code_remediate_plan(tmp_path, repository_templates)
        _use_source_local_worktree_root(plan_path, approval_path, repository)
        root = f"{repository.name.upper()}/.reports/codex/code-remediate-worktrees/fixture"
        plan = read_json(plan_path)
        plan["worktree_root"] = root
        _rewrite_plan(plan_path, approval_path, plan)

        with pytest.raises(lifecycle.PilotError, match=r"^worktree-root-not-managed-sibling$"):
            _prepare_remediation(lifecycle, tmp_path, plan_path, approval_path)

        assert not (tmp_path / root).exists()

    @_skip_no_symlinks
    def test_rejects_symlinked_source_local_root(
        self, lifecycle: ModuleType, tmp_path: Path, repository_templates: RepositoryTemplates
    ) -> None:
        """Reject a source-local worktree parent redirected outside the source checkout.

        The managed parent directory is a symlink to a directory outside the repository; creating worktrees through it
        would write outside the approved tree.
        """
        plan_path, approval_path, repository = _approved_code_remediate_plan(tmp_path, repository_templates)
        _use_source_local_worktree_root(plan_path, approval_path, repository)
        outside = tmp_path / "outside-worktrees"
        outside.mkdir()
        (repository / ".reports" / "codex" / "code-remediate-worktrees").symlink_to(outside, target_is_directory=True)

        with pytest.raises(lifecycle.PilotError, match=r"^managed-path-symlink-forbidden$"):
            _prepare_remediation(lifecycle, tmp_path, plan_path, approval_path)

        assert not (outside / "fixture").exists()

    @_skip_no_symlinks
    def test_rejects_symlinked_sibling_root(
        self, lifecycle: ModuleType, tmp_path: Path, repository_templates: RepositoryTemplates
    ) -> None:
        """Prevent a plan-bound sibling worktree root escaping through a symlinked parent.

        The default sibling directory ``.codex-rig-worktrees`` is replaced by a symlink to an outside directory.
        """
        plan_path, approval_path, _ = _approved_code_remediate_plan(tmp_path, repository_templates)
        outside = tmp_path / "outside-worktrees"
        outside.mkdir()
        (tmp_path / ".codex-rig-worktrees").symlink_to(outside, target_is_directory=True)

        with pytest.raises(lifecycle.PilotError, match=r"^managed-path-symlink-forbidden$"):
            _prepare_remediation(lifecycle, tmp_path, plan_path, approval_path)

        assert not (outside / "fixture").exists()

    @_skip_no_symlinks
    def test_rejects_symlinked_source_parent(
        self, lifecycle: ModuleType, tmp_path: Path, repository_templates: RepositoryTemplates
    ) -> None:
        """Prevent an authoritative repository path escaping through a symlinked parent.

        The repository is moved out of the workspace and reached again through a symlinked directory component, which
        the plan names as its source; every component of that path must be a real directory.
        """
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        _, _, repository = _approved_code_remediate_plan(workspace, repository_templates)
        outside_repository = tmp_path / "outside-source-repository"
        repository.rename(outside_repository)
        (workspace / "source-parent").symlink_to(tmp_path, target_is_directory=True)
        evidence = outside_repository / ".reports" / "codex" / "code-remediate" / "fixture"
        plan_path = evidence / "work-bucket-plan.json"
        approval_path = evidence / "parallel-approval.json"
        plan = read_json(plan_path)
        plan["source_repository"] = "source-parent/outside-source-repository"
        _rewrite_plan(plan_path, approval_path, plan)

        with pytest.raises(lifecycle.PilotError, match=r"^managed-path-symlink-forbidden$"):
            _prepare_remediation(lifecycle, workspace, plan_path, approval_path)

        assert not (workspace / ".codex-rig-worktrees" / "fixture").exists()


@pytest.mark.installed_plugin
@pytest.mark.integration
class TestCodeRemediateTransition:
    """Evidence is rechecked at each transition after preparation."""

    def test_rejects_context_pack_drift(
        self, lifecycle: ModuleType, tmp_path: Path, repository_templates: RepositoryTemplates
    ) -> None:
        """Rehash frozen context bytes before a child handover can advance the lifecycle.

        A context pack is edited after preparation; creating the first child's handover must notice and stop.
        """
        plan_path, approval_path, _ = _approved_code_remediate_plan(tmp_path, repository_templates)
        state_path = plan_path.parent / "production-lifecycle.json"
        state = _prepare_remediation(lifecycle, tmp_path, plan_path, approval_path)
        _write(plan_path.parent / "WRITE-A-context.md", "drifted after prepare\n")
        _write(_node_worktrees(tmp_path, state)[0] / "bucket-a.txt", "child-a\n")

        with pytest.raises(lifecycle.PilotError, match=r"^context-pack-digest-mismatch:WRITE-A$"):
            lifecycle.create_completed_child_handover(
                state_path=state_path, node_id="WRITE-A", summary="Context drift must block transition."
            )

    @_skip_no_symlinks
    def test_rejects_symlinked_evidence_root(
        self, lifecycle: ModuleType, tmp_path: Path, repository_templates: RepositoryTemplates
    ) -> None:
        """Reject lifecycle authority redirected after preparation through source-local evidence symlinks.

        The evidence tree is moved aside and replaced by a symlink to it; the next transition must refuse to read child
        changes through that redirect.
        """
        plan_path, approval_path, repository = _approved_code_remediate_plan(tmp_path, repository_templates)
        state_path = plan_path.parent / "production-lifecycle.json"
        state = _prepare_remediation(lifecycle, tmp_path, plan_path, approval_path)
        evidence = repository / ".reports"
        outside = tmp_path / "outside-evidence"
        evidence.rename(outside)
        evidence.symlink_to(outside, target_is_directory=True)

        with pytest.raises(lifecycle.PilotError, match=r"^managed-path-symlink-forbidden$"):
            lifecycle.create_completed_child_handover(
                state_path=state_path,
                node_id=str(state["nodes"][0]["node_id"]),
                summary="Must fail before reading child changes.",
            )


@pytest.mark.installed_plugin
@pytest.mark.integration
class TestCodeRemediateOverlap:
    """Two buckets may own the same file when their patches combine; conflicts need a recorded parent resolution."""

    def test_nonconflicting_edits_to_shared_file_integrate_once(
        self, lifecycle: ModuleType, tmp_path: Path, repository_templates: RepositoryTemplates
    ) -> None:
        """Combine distant edits to an approved shared file without duplicate path evidence.

        Both buckets own ``bucket-a.txt`` and edit lines 1 and 28 of a 30-line file; the patches apply cleanly in order,
        the path is reported once, and the source is only changed by the explicit source-apply step.
        """
        baseline_lines = [f"line-{index}\n" for index in range(30)]
        plan_path, approval_path, repository = _overlapping_code_remediate_plan(
            tmp_path, repository_templates, "".join(baseline_lines)
        )
        state_path = plan_path.parent / "production-lifecycle.json"
        state = _prepare_remediation(lifecycle, tmp_path, plan_path, approval_path)
        expected = baseline_lines.copy()
        expected[1] = "from-a\n"
        expected[28] = "from-b\n"
        edit_a = baseline_lines.copy()
        edit_a[1] = "from-a\n"
        edit_b = baseline_lines.copy()
        edit_b[28] = "from-b\n"
        _write_in_worktrees(tmp_path, state, ["bucket-a.txt"] * 2, ["".join(edit_a), "".join(edit_b)])
        handovers = _create_handovers(lifecycle, state_path, state, "Completed shared-file edit.")
        lifecycle.join_child_handovers(state_path=state_path, handovers=handovers)
        _collect_all(lifecycle, state_path, state)

        integrated = lifecycle.integrate_write_pilot(state_path=state_path)

        assert integrated["integration"]["paths"] == ["bucket-a.txt"]
        assert _read(tmp_path / str(integrated["integration_worktree"]) / "bucket-a.txt") == "".join(expected)
        assert _read(repository / "bucket-a.txt") == "".join(baseline_lines)
        application = lifecycle.apply_code_remediate_source(state_path=state_path)
        assert application["applied_paths"] == ["bucket-a.txt"]
        assert _read(repository / "bucket-a.txt") == "".join(expected)

    def test_conflicting_edits_require_recorded_parent_reconciliation(
        self, lifecycle: ModuleType, tmp_path: Path, repository_templates: RepositoryTemplates
    ) -> None:
        """Block source apply until the parent records a verified shared-file resolution.

        Both buckets rewrite the same line, so integration stops with ``needs-reconciliation``. The parent's resolution
        record must name the exact paths, patch digests and postimage; each wrong record is rejected until a correct one
        lets source apply and cleanup finish. This is one ordered contract, so the steps share a single test.
        """
        plan_path, approval_path, repository = _overlapping_code_remediate_plan(
            tmp_path, repository_templates, "baseline-a\n"
        )
        # Windows hosts install Git with core.autocrlf=true, so every worktree checkout of a `text`
        # attribute file holds CRLF while the source fixture keeps the LF bytes the test wrote. Forcing
        # that default on every host keeps the reconciliation cleanup path below honest: a file "restored"
        # by rewriting baseline bytes stays modified under CRLF checkout, and `git worktree remove` then
        # fails with git-command-failed:worktree, which is how this test first broke on Windows only.
        _git(repository, "config", "core.autocrlf", "true")
        state_path = plan_path.parent / "production-lifecycle.json"
        state = _prepare_remediation(lifecycle, tmp_path, plan_path, approval_path)
        _write_in_worktrees(tmp_path, state, ["bucket-a.txt"] * 2, CHILD_EDITS)
        handovers = _create_handovers(lifecycle, state_path, state, "Completed shared-file edit.")
        lifecycle.join_child_handovers(state_path=state_path, handovers=handovers)
        patches = {
            str(node["node_id"]): patch
            for node, patch in zip(state["nodes"], _collect_all(lifecycle, state_path, state), strict=True)
        }

        with pytest.raises(lifecycle.PilotError, match=r"^patch-integration-needs-reconciliation:WRITE-B$"):
            lifecycle.integrate_write_pilot(state_path=state_path)
        failed = read_json(state_path)
        assert failed["integration_status"] == "needs-reconciliation"
        assert failed["integration_failed_node"] == "WRITE-B"
        assert _read(repository / "bucket-a.txt") == "baseline-a\n"
        with pytest.raises(lifecycle.PilotError, match=r"^source-apply-before-integration-forbidden$"):
            lifecycle.apply_code_remediate_source(state_path=state_path)

        integration = tmp_path / str(failed["integration_worktree"])
        resolved = integration / "bucket-a.txt"
        _write(resolved, "resolved-a-and-b\n")
        record_path = plan_path.parent / "reconciliation.json"
        record = {
            "baseline_head": state["baseline_head"],
            "failed_node": "WRITE-B",
            "integration_order": ["WRITE-A", "WRITE-B"],
            "paths": ["bucket-a.txt"],
            "patch_sha256": {node_id: patch["patch_sha256"] for node_id, patch in patches.items()},
            "postimage_sha256": {"bucket-a.txt": _sha256(resolved)},
            "summary": "Parent combined both child edits.",
        }
        _write(integration / "bucket-b.txt", "unapproved\n")
        _write_json(record_path, record)
        with pytest.raises(lifecycle.PilotError, match=r"^integration-reconciliation-path-mismatch$"):
            lifecycle.reconcile_code_remediate_integration(state_path=state_path, record_path=record_path)
        # Restore through Git, never by rewriting baseline bytes: the checkout honours the repository's
        # end-of-line conversion, so only Git can put the file back in the exact state cleanup requires.
        _git(integration, "restore", "--", "bucket-b.txt")
        record["postimage_sha256"]["bucket-a.txt"] = "0" * 64
        _write_json(record_path, record)
        with pytest.raises(lifecycle.PilotError, match=r"^integration-reconciliation-postimage-mismatch$"):
            lifecycle.reconcile_code_remediate_integration(state_path=state_path, record_path=record_path)
        record["postimage_sha256"]["bucket-a.txt"] = _sha256(resolved)
        _write_json(record_path, record)
        assert lifecycle.main(["reconcile", "--state", str(state_path), "--record", str(record_path)]) == 0
        reconciled = read_json(state_path)
        assert reconciled["reconciliation"]["record_sha256"] == _sha256(record_path)
        assert reconciled["reconciliation"]["postimage_sha256"] == record["postimage_sha256"]
        assert reconciled["integration_status"] == "structurally-verified"
        assert _read(repository / "bucket-a.txt") == "baseline-a\n"
        _write(record_path, _read(record_path) + "\n")
        with pytest.raises(lifecycle.PilotError, match=r"^integration-reconciliation-evidence-drift$"):
            lifecycle.apply_code_remediate_source(state_path=state_path)
        _write_json(record_path, record)
        application = lifecycle.apply_code_remediate_source(state_path=state_path)
        assert application["applied_paths"] == ["bucket-a.txt"]
        assert _read(repository / "bucket-a.txt") == "resolved-a-and-b\n"
        assert lifecycle.cleanup_code_remediate_pilot(state_path=state_path)["cleanup_status"] == "removed"
        assert not integration.exists()


# --- code-remediate: applying integrated patches to the source repository -------------------------------------------


@pytest.mark.installed_plugin
@pytest.mark.integration
class TestCodeRemediateSourceApply:
    """Integrated patches reach the authoritative source only through a guarded, rollback-aware apply."""

    def test_source_local_worktrees_preserve_unrelated_source_changes(
        self, lifecycle: ModuleType, tmp_path: Path, repository_templates: RepositoryTemplates
    ) -> None:
        """Apply approved edits without changing unrelated source bytes or staged index entries.

        The source repository holds staged, unstaged, untracked and ignored user changes while children run in source-
        local worktrees, and another unrelated edit lands during apply. Only the two approved files may change.
        """
        plan_path, approval_path, repository = _approved_code_remediate_plan(tmp_path, repository_templates)
        worktree_root = _use_source_local_worktree_root(plan_path, approval_path, repository)
        tracked = repository / ".gitattributes"
        _write(tracked, "*.txt text\n# staged unrelated note\n")
        _git(repository, "add", ".gitattributes")
        staged_entry = _git(repository, "ls-files", "--stage", "--", ".gitattributes").stdout
        _write(tracked, "*.txt text\n# staged unrelated note\n# unstaged unrelated note\n")
        untracked = repository / "scratch.txt"
        untracked.write_bytes(b"untracked user bytes\n")
        ignored = repository / "scratch-cache.tmp"
        with (repository / ".git" / "info" / "exclude").open("a", encoding="utf-8", newline="\n") as stream:
            stream.write("scratch-cache.tmp\n")
        ignored.write_bytes(b"ignored user bytes\n")
        state_path = plan_path.parent / "production-lifecycle.json"
        state = _prepare_remediation(lifecycle, tmp_path, plan_path, approval_path)
        assert worktree_root.is_dir()
        assert all(worktree.is_relative_to(repository) for worktree in _node_worktrees(tmp_path, state))
        assert _git(repository, "check-ignore", str(worktree_root)).returncode == 0
        _edit_owned(tmp_path, state, CHILD_EDITS)
        lifecycle.join_child_handovers(state_path=state_path, handovers=_create_handovers(lifecycle, state_path, state))
        _collect_all(lifecycle, state_path, state)
        lifecycle.integrate_write_pilot(state_path=state_path)
        _write(tracked, _read(tracked) + "# concurrent unrelated note\n")
        concurrent = repository / "concurrent.txt"
        concurrent.write_bytes(b"concurrent user bytes\n")
        lifecycle.apply_code_remediate_source(state_path=state_path)

        cleanup = lifecycle.cleanup_code_remediate_pilot(state_path=state_path)

        assert cleanup["cleanup_status"] == "removed"
        assert cleanup["force"] is False
        assert all(not worktree.exists() for worktree in _node_worktrees(tmp_path, state))
        assert _read(repository / "bucket-a.txt") == "child-a\n"
        assert _read(repository / "bucket-b.txt") == "child-b\n"
        assert _read(tracked) == (
            "*.txt text\n# staged unrelated note\n# unstaged unrelated note\n# concurrent unrelated note\n"
        )
        assert _git(repository, "ls-files", "--stage", "--", ".gitattributes").stdout == staged_entry
        assert untracked.read_bytes() == b"untracked user bytes\n"
        assert ignored.read_bytes() == b"ignored user bytes\n"
        assert concurrent.read_bytes() == b"concurrent user bytes\n"

    def test_records_rollback_before_nonforce_cleanup(self, integrated_remediation: Pilot, tmp_path: Path) -> None:
        """Apply only integrated bucket patches and retain rollback bytes before cleanup removes worktrees.

        After a successful apply the rollback patch must already be on disk with a matching digest, so a later cleanup
        can never leave the source without a recorded way back.
        """
        lifecycle, state_path, state, repository = integrated_remediation

        application = lifecycle.apply_code_remediate_source(state_path=state_path)

        rollback_path = state_path.parent / str(application["rollback_patch_path"])
        assert application["status"] == "applied"
        assert application["applied_paths"] == ["bucket-a.txt", "bucket-b.txt"]
        assert _sha256(rollback_path) == application["rollback_patch_sha256"]
        assert _read(repository / "bucket-a.txt") == "child-a\n"
        assert _read(repository / "bucket-b.txt") == "child-b\n"

        cleanup = lifecycle.cleanup_code_remediate_pilot(state_path=state_path)

        assert cleanup["cleanup_status"] == "removed"
        assert cleanup["force"] is False
        assert all(not worktree.exists() for worktree in _node_worktrees(tmp_path, state))

    def test_resumes_nonforce_cleanup_after_locked_worktree(
        self, integrated_remediation: Pilot, tmp_path: Path
    ) -> None:
        """Resume historical patch delivery cleanup from a positively recorded removal."""
        lifecycle, state_path, state, repository = integrated_remediation
        first, second = _node_worktrees(tmp_path, state)
        integration = tmp_path / read_json(state_path)["integration_worktree"]
        lifecycle.apply_code_remediate_source(state_path=state_path)
        _git(repository, "worktree", "lock", str(second))

        with pytest.raises(lifecycle.PilotError, match=r"^cleanup-failed$"):
            lifecycle.cleanup_code_remediate_pilot(state_path=state_path)

        persisted = read_json(state_path)["cleanup"]
        assert persisted == {"status": "failed", "force": False, "worktrees": [first.relative_to(tmp_path).as_posix()]}
        assert not first.exists()
        assert second.is_dir()
        assert integration.is_dir()
        _git(repository, "worktree", "unlock", str(second))
        _git(repository, "worktree", "add", "--detach", str(first), "HEAD")
        with pytest.raises(lifecycle.PilotError, match=r"^cleanup-removed-worktree-reappeared$"):
            lifecycle.cleanup_code_remediate_pilot(state_path=state_path)
        assert read_json(state_path)["cleanup"] == persisted
        _git(repository, "worktree", "remove", str(first))
        assert lifecycle.cleanup_code_remediate_pilot(state_path=state_path)["cleanup_status"] == "removed"
        assert not second.exists()
        assert not integration.exists()
        assert read_json(state_path)["cleanup"]["worktrees"] == [
            path.relative_to(tmp_path).as_posix() for path in (first, second, integration)
        ]

    def test_records_actual_crlf_postimage(
        self,
        lifecycle: ModuleType,
        tmp_path: Path,
        repository_templates: RepositoryTemplates,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Record native source bytes after clean-filter-equivalent CRLF application.

        The source checkout converts applied files to CRLF while the integrated worktree stays LF; the persisted
        postimage digest must describe the CRLF bytes actually on disk, not the integration bytes.
        """
        _, state_path, _, repository = _prepare_integrated_code_remediate(
            lifecycle, tmp_path, repository_templates, simulate_crlf_default=True
        )
        _git(repository, "config", "core.autocrlf", "true")
        _hook_after_source_apply(
            lifecycle, monkeypatch, repository, functools.partial(_rewrite_owned_as_crlf, repository)
        )

        application = lifecycle.apply_code_remediate_source(state_path=state_path)

        persisted = read_json(state_path)
        actual_postimages = {relative: _sha256(repository / relative) for relative in ("bucket-a.txt", "bucket-b.txt")}
        assert application["status"] == "applied"
        assert (repository / "bucket-a.txt").read_bytes() == b"child-a\r\n"
        assert (repository / "bucket-b.txt").read_bytes() == b"child-b\r\n"
        assert persisted["source"]["postimage_sha256"] == actual_postimages
        assert persisted["source"]["postimage_sha256"] != persisted["integration_final_sha256"]
        assert lifecycle.cleanup_code_remediate_pilot(state_path=state_path)["cleanup_status"] == "removed"

    def test_rejects_tampered_integration_before_source_apply(
        self, integrated_remediation: Pilot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Revalidate integrated bytes before any authoritative source apply command can run.

        The integration worktree is edited after integration finished; apply must detect the drift and never reach ``git
        apply`` on the source, which the wrapped Git runner turns into a hard failure.
        """
        lifecycle, state_path, _, repository = integrated_remediation
        integration = tmp_path / str(read_json(state_path)["integration_worktree"])
        _write(integration / "bucket-a.txt", "tampered integration\n")
        real_git = lifecycle._git

        def _reject_source_apply(target: Path, *arguments: str) -> str:
            """Fail if source application runs after integration-worktree tampering."""
            if target == repository and arguments[:1] == ("apply",):
                raise AssertionError("authoritative source apply ran after integration tamper")
            return real_git(target, *arguments)

        monkeypatch.setattr(lifecycle, "_git", _reject_source_apply)
        with pytest.raises(lifecycle.PilotError, match=r"^integration-worktree-drift$"):
            lifecycle.apply_code_remediate_source(state_path=state_path)

        assert _read(repository / "bucket-a.txt") == "baseline-a\n"
        assert _read(repository / "bucket-b.txt") == "baseline-b\n"

    def test_failure_restores_only_known_states(
        self, integrated_remediation: Pilot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Restore approved paths after apply failure without touching unrelated source changes.

        Apply fails after changing both approved files while the user has a staged and an unstaged edit plus an
        untracked file. Rollback must put back exactly the approved paths and leave every user change as it was.
        """
        lifecycle, state_path, _, repository = integrated_remediation
        tracked = repository / ".gitattributes"
        _write(tracked, "*.txt text\n# staged note\n")
        _git(repository, "add", ".gitattributes")
        staged_entry = _git(repository, "ls-files", "--stage", "--", ".gitattributes").stdout
        _write(tracked, "*.txt text\n# staged note\n# unstaged note\n")
        untracked = repository / "unrelated.txt"
        untracked.write_bytes(b"unrelated user bytes\n")
        failure = functools.partial(_raise, lifecycle.PilotError("simulated-source-apply-failure"))
        _hook_after_source_apply(lifecycle, monkeypatch, repository, failure)

        with pytest.raises(lifecycle.PilotError, match=r"^simulated-source-apply-failure$"):
            lifecycle.apply_code_remediate_source(state_path=state_path)

        assert _apply_statuses(state_path) == ("failed-rolled-back", "failed-rolled-back")
        assert _read(repository / "bucket-a.txt") == "baseline-a\n"
        assert _read(repository / "bucket-b.txt") == "baseline-b\n"
        assert _read(tracked) == "*.txt text\n# staged note\n# unstaged note\n"
        assert _git(repository, "ls-files", "--stage", "--", ".gitattributes").stdout == staged_entry
        assert untracked.read_bytes() == b"unrelated user bytes\n"
        assert set(_git(repository, "status", "--porcelain=v1", "--untracked-files=all").stdout.splitlines()) == {
            "MM .gitattributes",
            "?? .reports/codex/code-remediate/fixture/WRITE-A-context.md",
            "?? .reports/codex/code-remediate/fixture/WRITE-B-context.md",
            "?? .reports/codex/code-remediate/fixture/parallel-approval.json",
            "?? .reports/codex/code-remediate/fixture/patch-a.diff",
            "?? .reports/codex/code-remediate/fixture/patch-b.diff",
            "?? .reports/codex/code-remediate/fixture/production-lifecycle.json",
            "?? .reports/codex/code-remediate/fixture/rollback.patch",
            "?? .reports/codex/code-remediate/fixture/source-application.patch",
            "?? .reports/codex/code-remediate/fixture/work-bucket-plan.json",
            "?? unrelated.txt",
        }

    def test_preflight_preserves_new_staged_owned_edit(
        self, integrated_remediation: Pilot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reject late owned-path index drift before rollback can erase user-staged content.

        A user stages an owned file just after the lifecycle enters ``source-applying``. Preflight must refuse and must
        not run any ``git restore``, which would silently discard the staged edit.
        """
        lifecycle, state_path, _, repository = integrated_remediation
        owned = repository / "bucket-a.txt"
        baseline = owned.read_bytes()
        staged = b"user staged edit\n"
        real_persist = lifecycle._persist
        real_git = lifecycle._git
        restore_calls: list[tuple[str, ...]] = []
        staged_entry: list[str] = []

        def _inject_staged_edit(path: Path, payload: dict[str, object]) -> None:
            """Model a user staging an owned file after lifecycle state becomes applying."""
            real_persist(path, payload)
            if payload["status"] == "source-applying":
                owned.write_bytes(staged)
                _git(repository, "add", "bucket-a.txt")
                staged_entry.append(_git(repository, "ls-files", "--stage", "--", "bucket-a.txt").stdout)
                owned.write_bytes(baseline)

        def _record_restore(target: Path, *arguments: str) -> str:
            """Observe whether a failed preflight invokes a destructive source restore."""
            if target == repository and arguments[:1] == ("restore",):
                restore_calls.append(arguments)
            return real_git(target, *arguments)

        monkeypatch.setattr(lifecycle, "_persist", _inject_staged_edit)
        monkeypatch.setattr(lifecycle, "_git", _record_restore)

        with pytest.raises(lifecycle.PilotError, match=r"^source-repository-dirty$"):
            lifecycle.apply_code_remediate_source(state_path=state_path)

        assert len(staged_entry) == 1
        assert _git(repository, "ls-files", "--stage", "--", "bucket-a.txt").stdout == staged_entry[0], (
            "late user-staged index entry was erased"
        )
        assert owned.read_bytes() == baseline
        assert restore_calls == [], "preflight rejection invoked source restore"
        assert (repository / "bucket-b.txt").read_bytes() == b"baseline-b\n"
        assert _apply_statuses(state_path) == ("integrated", "preflight-failed")

    def test_preflight_check_failure_reopens_integrated_run(
        self, integrated_remediation: Pilot, validator: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Keep the source unchanged and the run retryable when Git rejects patch preflight.

        ``git apply --check`` fails before anything is applied, so the source bytes and index must be untouched and no
        ``apply`` or ``restore`` may run. Nothing reached the source, so the run reopens as ``integrated`` with the
        attempt recorded as ``preflight-failed``, and a later apply delivers once Git accepts the patch.
        """
        lifecycle, state_path, _, repository = integrated_remediation
        baseline = {path: (repository / path).read_bytes() for path in ("bucket-a.txt", "bucket-b.txt")}
        index = _git(repository, "ls-files", "--stage", "--", "bucket-a.txt", "bucket-b.txt").stdout
        real_git = lifecycle._git
        source_mutations: list[tuple[str, ...]] = []

        def _reject_apply_preflight(target: Path, *arguments: str) -> str:
            """Reject only the source patch preflight and record forbidden mutations."""
            if target == repository and arguments[:2] == ("apply", "--check"):
                raise lifecycle.PilotError("simulated-source-preflight-check-failure")
            if target == repository and arguments[:1] in {("apply",), ("restore",)}:
                source_mutations.append(arguments)
            return real_git(target, *arguments)

        monkeypatch.setattr(lifecycle, "_git", _reject_apply_preflight)
        with pytest.raises(lifecycle.PilotError, match=r"^simulated-source-preflight-check-failure$"):
            lifecycle.apply_code_remediate_source(state_path=state_path)

        assert _apply_statuses(state_path) == ("integrated", "preflight-failed")
        assert {path: (repository / path).read_bytes() for path in baseline} == baseline
        assert _git(repository, "ls-files", "--stage", "--", "bucket-a.txt", "bucket-b.txt").stdout == index
        assert source_mutations == []
        monkeypatch.undo()

        assert lifecycle.apply_code_remediate_source(state_path=state_path)["status"] == "applied"
        assert lifecycle.cleanup_code_remediate_pilot(state_path=state_path)["cleanup_status"] == "removed"
        assert _read(repository / "bucket-a.txt") == "child-a\n"
        _assert_completed_lifecycle(validator, state_path)

    def test_rolls_back_filtered_newline_postimage(
        self, integrated_remediation: Pilot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Restore a known clean-filtered postimage whose source bytes use CRLF.

        Apply fails after the source checkout rewrote both files as CRLF; that is a recognised postimage, so rollback
        must restore the CRLF baseline bytes rather than declare the state ambiguous.
        """
        lifecycle, state_path, _, repository = integrated_remediation
        _git(repository, "config", "core.autocrlf", "true")
        crlf = functools.partial(_rewrite_owned_as_crlf, repository)
        failure = functools.partial(_raise, lifecycle.PilotError("simulated-source-apply-failure"))
        _hook_after_source_apply(lifecycle, monkeypatch, repository, crlf, failure)

        with pytest.raises(lifecycle.PilotError, match=r"^simulated-source-apply-failure$"):
            lifecycle.apply_code_remediate_source(state_path=state_path)

        assert _apply_statuses(state_path) == ("failed-rolled-back", "failed-rolled-back")
        assert (repository / "bucket-a.txt").read_bytes() == b"baseline-a\r\n"
        assert (repository / "bucket-b.txt").read_bytes() == b"baseline-b\r\n"

    def test_rollback_rechecks_preimages_after_restore(
        self, integrated_remediation: Pilot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Retain evidence and block cleanup when an external restore leaves an approved path changed.

        Apply fails and the restore step silently does nothing, so an approved path still holds the child's bytes.
        Rollback must recheck, report ``rollback-ambiguous``, keep all worktrees and forbid cleanup.
        """
        lifecycle, state_path, state, repository = integrated_remediation
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
        with pytest.raises(lifecycle.PilotError, match=r"^rollback-ambiguous$"):
            lifecycle.apply_code_remediate_source(state_path=state_path)

        assert _apply_statuses(state_path) == ("rollback-ambiguous", "rollback-ambiguous")
        assert _read(repository / "bucket-a.txt") == "child-a\n"
        assert all(worktree.exists() for worktree in _node_worktrees(tmp_path, state))
        with pytest.raises(lifecycle.PilotError, match=r"^cleanup-before-source-application-forbidden$"):
            lifecycle.cleanup_code_remediate_pilot(state_path=state_path)

    def test_rollback_restores_a_file_the_failed_apply_deleted(
        self, integrated_remediation: Pilot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bring back an approved file that a failed ``git apply`` left missing, instead of stopping as ambiguous.

        ``git apply`` deletes each modified file before it writes the new one, so a write error leaves the file absent
        while its index entry still holds the baseline; Git shows that state, so the rollback restores it.
        """
        lifecycle, state_path, _, repository = integrated_remediation
        delete = (repository / "bucket-a.txt").unlink
        failure = functools.partial(_raise, lifecycle.PilotError("simulated-apply-write-error"))
        _hook_after_source_apply(lifecycle, monkeypatch, repository, delete, failure)

        with pytest.raises(lifecycle.PilotError, match=r"^simulated-apply-write-error$"):
            lifecycle.apply_code_remediate_source(state_path=state_path)

        assert _apply_statuses(state_path) == ("failed-rolled-back", "failed-rolled-back")
        assert _read(repository / "bucket-a.txt") == "baseline-a\n"
        assert _read(repository / "bucket-b.txt") == "baseline-b\n"

    def test_rollback_keeps_an_edit_that_lands_after_the_check(
        self, integrated_remediation: Pilot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Report ambiguity and keep the bytes when a user edit slips in between the image check and the restore."""
        lifecycle, state_path, _, repository = integrated_remediation
        failure = functools.partial(_raise, lifecycle.PilotError("simulated-apply-write-error"))
        _hook_after_source_apply(lifecycle, monkeypatch, repository, failure)
        real_stage_entries = lifecycle._stage_zero_entries

        def _edit_after_the_check(source: Path, paths: list[str]) -> dict[str, Any] | None:
            """Read the index as Git does, then let the user's edit land before the backup is taken."""
            entries = real_stage_entries(source, paths)
            _write(repository / "bucket-a.txt", "late user edit\n")
            return entries

        monkeypatch.setattr(lifecycle, "_stage_zero_entries", _edit_after_the_check)

        with pytest.raises(lifecycle.PilotError, match=r"^rollback-ambiguous$"):
            lifecycle.apply_code_remediate_source(state_path=state_path)

        assert _apply_statuses(state_path) == ("rollback-ambiguous", "rollback-ambiguous")
        assert _read(repository / "bucket-a.txt") == "late user edit\n"

    def test_ambiguous_failure_retains_evidence(
        self, integrated_remediation: Pilot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Retain diagnostics without restoration when a concurrent write creates an unknown state.

        A second writer changes an approved file between apply and failure, so the bytes match no known postimage.
        Rollback must refuse to guess, keep the concurrent bytes and the worktrees, and forbid cleanup.
        """
        lifecycle, state_path, state, repository = integrated_remediation
        concurrent_write = functools.partial(_write, repository / "bucket-a.txt", "concurrent-writer\n")
        failure = functools.partial(_raise, lifecycle.PilotError("simulated-concurrent-write"))
        _hook_after_source_apply(lifecycle, monkeypatch, repository, concurrent_write, failure)

        with pytest.raises(lifecycle.PilotError, match=r"^rollback-ambiguous$"):
            lifecycle.apply_code_remediate_source(state_path=state_path)

        assert _apply_statuses(state_path) == ("rollback-ambiguous", "rollback-ambiguous")
        assert _read(repository / "bucket-a.txt") == "concurrent-writer\n"
        assert _read(repository / "bucket-b.txt") == "child-b\n"
        assert all(worktree.exists() for worktree in _node_worktrees(tmp_path, state))
        with pytest.raises(lifecycle.PilotError, match=r"^cleanup-before-source-application-forbidden$"):
            lifecycle.cleanup_code_remediate_pilot(state_path=state_path)


def _joined_remediation(lifecycle: ModuleType, workspace: Path, templates: RepositoryTemplates) -> Pilot:
    """Prepare, edit and join a patch-mode remediation pilot before collection."""
    plan_path, approval_path, repository = _approved_code_remediate_plan(workspace, templates)
    state_path = plan_path.parent / "production-lifecycle.json"
    state = _prepare_remediation(lifecycle, workspace, plan_path, approval_path)
    _edit_owned(workspace, state, CHILD_EDITS)
    lifecycle.join_child_handovers(state_path=state_path, handovers=_create_handovers(lifecycle, state_path, state))
    return Pilot(lifecycle, state_path, state, repository)


def _collected_remediation(lifecycle: ModuleType, workspace: Path, templates: RepositoryTemplates) -> Pilot:
    """Prepare, edit, join and collect a patch-mode remediation pilot that has not integrated yet."""
    pilot = _joined_remediation(lifecycle, workspace, templates)
    lifecycle, state_path, state, _ = pilot
    _collect_all(lifecycle, state_path, state)
    return pilot


def _crash_during_integration(pilot: Pilot, workspace: Path, monkeypatch: pytest.MonkeyPatch, command: str) -> Path:
    """Interrupt integration just after the first patch application or cherry-pick."""
    lifecycle, state_path, state, _ = pilot
    integration = workspace / f"{state['worktree_root']}/integration"
    with monkeypatch.context() as crash:
        _crash_around_git(
            lifecycle,
            crash,
            lambda target, args: target == integration and args[0] == command and "--check" not in args,
            after_effect=True,
        )
        with pytest.raises(_SimulatedCrash):
            lifecycle.integrate_write_pilot(state_path=state_path)
    assert read_json(state_path)["integration_status"] == "applying"
    assert _read(integration / "bucket-a.txt") == "child-a\n"
    assert _read(integration / "bucket-b.txt") == "baseline-b\n"
    return integration


def _replace_patch_with_symlink(patch: Path) -> None:
    """Replace the patch with a symlink to its original, byte-identical contents."""
    target = patch.with_suffix(".saved")
    patch.rename(target)
    patch.symlink_to(target.name)


def _leave_pending_merge(integration: Path) -> None:
    """Write the per-worktree marker used by Git during a merge."""
    git_dir = Path(_git(integration, "rev-parse", "--absolute-git-dir").stdout.strip())
    _write(git_dir / "MERGE_HEAD", _git(integration, "rev-parse", "HEAD").stdout)


def _stage_unowned_edit(integration: Path) -> None:
    """Stage an unowned edit and restore its worktree bytes to the baseline."""
    attributes = integration / ".gitattributes"
    baseline = attributes.read_bytes()
    attributes.write_bytes(b"*.txt text eol=lf\n")
    _git(integration, "add", "--", ".gitattributes")
    attributes.write_bytes(baseline)


def _leave_ignored_file(integration: Path) -> None:
    """Create an ignored file outside the approved paths."""
    ignored = integration / ".reports" / "unowned.txt"
    ignored.parent.mkdir()
    _write(ignored, "unowned\n")


def _collect_grouped_wave(pilot: Pilot) -> None:
    """Join and collect the recorded worker commits before integrating them."""
    lifecycle, state_path, state, _ = pilot
    lifecycle.join_child_handovers(state_path=state_path, handovers=_create_handovers(lifecycle, state_path, state))
    _collect_all(lifecycle, state_path, state)


def _crash_before_delivery_intent(lifecycle: ModuleType, monkeypatch: pytest.MonkeyPatch, state_path: Path) -> None:
    """Run delivery until it dies just before the ``source-applying`` record reaches disk, then restore saving."""
    real_persist = lifecycle._persist

    def _crash_on_intent(path: Path, payload: dict[str, object]) -> None:
        """Die before the delivery intent is saved; every other save goes through."""
        if payload["status"] == "source-applying":
            raise _SimulatedCrash("source-applying")
        real_persist(path, payload)

    monkeypatch.setattr(lifecycle, "_persist", _crash_on_intent)
    with pytest.raises(_SimulatedCrash):
        lifecycle.apply_code_remediate_source(state_path=state_path)
    monkeypatch.undo()
    assert read_json(state_path)["status"] == "integrated"
    assert (state_path.parent / "source-application.patch").is_file()


def _land_delivery_with_plain_git(route: _DeliveryRoute) -> None:
    """Deliver the integrated change into the source with plain Git, saving no lifecycle state."""
    _, state_path, _, repository = route.pilot
    if route.gates is None:
        _git(repository, "apply", str(state_path.parent / "source-application.patch"))
    else:
        _git(repository, "merge", "--ff-only", "-q", route.delivered_head)


def _finish_other_apply_during_recheck(route: _DeliveryRoute, monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    """Run a second apply of the same run to completion while this apply re-checks the integration worktree.

    The re-check runs after this apply loaded ``integrated`` and passed its source checks, so the other apply lands
    the delivery and records it in between, exactly as a concurrent invocation would.

    Returns:
        A list that receives the state file bytes the other apply left behind.
    """
    lifecycle, state_path, _, _ = route.pilot
    real_git = lifecycle._git
    fired: list[tuple[str, ...]] = []
    recorded: list[bytes] = []

    def _git_after_other_apply(target: Path, *arguments: str) -> str:
        """Let the other apply deliver before the first integration re-check runs."""
        if not fired and target == route.integration and arguments[:2] == ("diff", "--name-only"):
            fired.append(arguments)
            lifecycle.apply_code_remediate_source(state_path=state_path, gates_path=route.gates)
            recorded.append(state_path.read_bytes())
        return real_git(target, *arguments)

    monkeypatch.setattr(lifecycle, "_git", _git_after_other_apply)
    return recorded


def _land_after_delivery_intent(route: _DeliveryRoute, monkeypatch: pytest.MonkeyPatch) -> None:
    """Land the delivery with plain Git right after this apply records ``source-applying``.

    Models another apply of the same run whose delivery reached Git but whose record never reached the state file.
    """
    lifecycle = route.pilot.lifecycle
    real_persist = lifecycle._persist

    def _land_after_intent(path: Path, payload: dict[str, object]) -> None:
        """Persist, then land the delivery once this apply has recorded it as in progress."""
        real_persist(path, payload)
        if payload["status"] == "source-applying":
            _land_delivery_with_plain_git(route)

    monkeypatch.setattr(lifecycle, "_persist", _land_after_intent)


def _edit_owned_after_delivery_intent(pilot: Pilot, monkeypatch: pytest.MonkeyPatch) -> None:
    """Let a user edit an approved source path right after this apply records ``source-applying``."""
    lifecycle, _, _, repository = pilot
    real_persist = lifecycle._persist

    def _edit_after_intent(path: Path, payload: dict[str, object]) -> None:
        """Persist, then edit the first approved path once delivery has been recorded as in progress."""
        real_persist(path, payload)
        if payload["status"] == "source-applying":
            _write(repository / "bucket-a.txt", "user edit during delivery\n")

    monkeypatch.setattr(lifecycle, "_persist", _edit_after_intent)


def _fail_staged_path_probe(pilot: Pilot, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the source read that asks whether anything is staged on the approved paths."""
    lifecycle, _, _, repository = pilot
    real_git_bytes = lifecycle._git_bytes

    def _staged_probe_fails(target: Path, *arguments: str) -> bytes:
        """Raise the error Git's runner raises for a failed command, only for the path-scoped staged-change read."""
        if target == repository and arguments[:5] == ("diff", "--cached", "--name-only", "-z", "--"):
            raise lifecycle.PilotError("git-command-failed:diff")
        return real_git_bytes(target, *arguments)

    monkeypatch.setattr(lifecycle, "_git_bytes", _staged_probe_fails)


def _set_worktree_exec_bit(repository: Path, relative: str) -> None:
    """Make one working file executable for its owner, the bit Git tracks as mode ``100755``."""
    path = repository / relative
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _stage_exec_bit(repository: Path, relative: str) -> None:
    """Stage mode ``100755`` for one path while leaving its working file untouched."""
    _git(repository, "update-index", "--chmod=+x", "--", relative)


#: Skip marker for checks of a working file's executable bit, which only hosts with POSIX permission bits keep.
_skip_no_posix_modes = pytest.mark.skipif(os.name != "posix", reason="host keeps no POSIX permission bits")

_CRASH_POINTS = [
    pytest.param(False, id="before-effect"),
    pytest.param(True, id="after-effect"),
]


@pytest.mark.installed_plugin
@pytest.mark.integration
class TestCodeRemediateCrashRecovery:
    """A rerun after a crash around any Git effect finishes or reconciles from what Git shows; it never gets stuck."""

    def test_collect_resumes_after_patch_write(
        self,
        lifecycle: ModuleType,
        tmp_path: Path,
        repository_templates: RepositoryTemplates,
        validator: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Adopt the exact patch left by a crash and finish remediation."""
        pilot = _joined_remediation(lifecycle, tmp_path, repository_templates)
        _, state_path, state, repository = pilot
        _, patch_path, patch = _crash_after_patch_output(pilot, monkeypatch)
        written_at = patch_path.stat().st_mtime_ns

        collected = _collect_all(lifecycle, state_path, state)

        assert collected[0]["patch_sha256"] == hashlib.sha256(patch).hexdigest()
        assert patch_path.stat().st_mtime_ns == written_at
        lifecycle.integrate_write_pilot(state_path=state_path)
        lifecycle.apply_code_remediate_source(state_path=state_path)
        assert _read(repository / "bucket-b.txt") == "child-b\n"
        _assert_completed_lifecycle_after_cleanup(lifecycle, validator, state_path)

    @pytest.mark.parametrize(
        "replace_output",
        [
            pytest.param(lambda path: path.write_bytes(b"unrelated patch\n"), id="different-bytes"),
            pytest.param(_replace_patch_with_symlink, id="symlink", marks=_skip_no_symlinks),
        ],
    )
    def test_collect_refuses_unproven_patch_output(
        self,
        lifecycle: ModuleType,
        tmp_path: Path,
        repository_templates: RepositoryTemplates,
        monkeypatch: pytest.MonkeyPatch,
        replace_output: Callable[[Path], object],
    ) -> None:
        """Refuse an occupied output unless it is a regular file with the re-derived bytes."""
        pilot = _joined_remediation(lifecycle, tmp_path, repository_templates)
        _, state_path, _, _ = pilot
        node_id, patch_path, _ = _crash_after_patch_output(pilot, monkeypatch)
        replace_output(patch_path)
        before = (patch_path.is_symlink(), patch_path.read_bytes(), state_path.read_bytes())

        with pytest.raises(lifecycle.PilotError, match=rf"^patch-output-exists:{node_id}$"):
            lifecycle.collect_write_patch(state_path=state_path, node_id=node_id)

        assert (patch_path.is_symlink(), patch_path.read_bytes(), state_path.read_bytes()) == before

    def test_resumes_partial_patch_integration(
        self,
        lifecycle: ModuleType,
        tmp_path: Path,
        repository_templates: RepositoryTemplates,
        validator: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Restart patch integration from the baseline after the first patch landed."""
        pilot = _collected_remediation(lifecycle, tmp_path, repository_templates)
        _, state_path, _, repository = pilot
        _crash_during_integration(pilot, tmp_path, monkeypatch, "apply")

        assert lifecycle.integrate_write_pilot(state_path=state_path)["integration_status"] == "structurally-verified"

        assert _read(repository / "bucket-a.txt") == "baseline-a\n"
        lifecycle.apply_code_remediate_source(state_path=state_path)
        assert _read(repository / "bucket-a.txt") == "child-a\n"
        assert _read(repository / "bucket-b.txt") == "child-b\n"
        _assert_completed_lifecycle_after_cleanup(lifecycle, validator, state_path)

    def test_resumes_partial_grouped_integration(
        self,
        grouped_remediation: Pilot,
        draft_commits: tuple[str, str],
        tmp_path: Path,
        validator: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Restart grouped integration from the baseline after the first cherry-pick landed."""
        lifecycle, state_path, state, repository = grouped_remediation
        _collect_grouped_wave(grouped_remediation)
        integration = _crash_during_integration(grouped_remediation, tmp_path, monkeypatch, "cherry-pick")
        first = _git(integration, "rev-parse", "HEAD").stdout.strip()
        assert first != state["baseline_head"]
        assert (
            _git(integration, "show", "-s", "--format=%s", first).stdout
            == _git(repository, "show", "-s", "--format=%s", draft_commits[0]).stdout
        )

        result = lifecycle.integrate_write_pilot(state_path=state_path)

        assert len(result["integrated_commits"]) == 2
        assert _git(repository, "rev-parse", "HEAD").stdout.strip() == state["baseline_head"]
        gates = _run_grouped_gates(state_path.parent, integration, result["integration_head"])
        lifecycle.apply_code_remediate_source(state_path=state_path, gates_path=gates)
        assert _git(repository, "rev-parse", "HEAD").stdout.strip() == result["integration_head"]
        _assert_completed_lifecycle_after_cleanup(lifecycle, validator, state_path)

    @pytest.mark.parametrize(
        "edit_checkout",
        [
            pytest.param(lambda path: _write(path / ".gitattributes", "*.txt text eol=lf\n"), id="unowned-edit"),
            pytest.param(_stage_unowned_edit, id="unowned-index-edit"),
            pytest.param(_leave_ignored_file, id="unowned-ignored-file"),
            pytest.param(_leave_pending_merge, id="pending-merge"),
        ],
    )
    def test_integration_resume_preserves_unproven_patch_checkout(
        self,
        lifecycle: ModuleType,
        tmp_path: Path,
        repository_templates: RepositoryTemplates,
        monkeypatch: pytest.MonkeyPatch,
        edit_checkout: Callable[[Path], None],
    ) -> None:
        """Keep the checkout and its Git metadata when patch recovery cannot prove ownership."""
        pilot = _collected_remediation(lifecycle, tmp_path, repository_templates)
        _, state_path, _, repository = pilot
        integration = _crash_during_integration(pilot, tmp_path, monkeypatch, "apply")
        edit_checkout(integration)
        git_dir = Path(_git(integration, "rev-parse", "--absolute-git-dir").stdout.strip())
        before = (_tree_snapshot(integration), _tree_snapshot(git_dir), state_path.read_bytes())

        with pytest.raises(lifecycle.PilotError, match=r"^integration-worktree-exists$"):
            lifecycle.integrate_write_pilot(state_path=state_path)

        assert (_tree_snapshot(integration), _tree_snapshot(git_dir), state_path.read_bytes()) == before
        assert _read(repository / "bucket-a.txt") == "baseline-a\n"

    def test_grouped_integration_resume_preserves_unproven_head(
        self,
        grouped_remediation: Pilot,
        draft_commits: tuple[str, str],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Keep a linear grouped checkout whose first commit contains a different approved-path patch."""
        lifecycle, state_path, state, _ = grouped_remediation
        _collect_grouped_wave(grouped_remediation)
        integration = _crash_during_integration(grouped_remediation, tmp_path, monkeypatch, "cherry-pick")
        _git(integration, "checkout", "--detach", draft_commits[1])
        assert _git(integration, "rev-parse", "HEAD^").stdout.strip() == state["baseline_head"]
        before = (_tree_snapshot(integration), state_path.read_bytes())

        with pytest.raises(lifecycle.PilotError, match=r"^integration-worktree-exists$"):
            lifecycle.integrate_write_pilot(state_path=state_path)

        assert (_tree_snapshot(integration), state_path.read_bytes()) == before
        assert _git(integration, "rev-parse", "HEAD").stdout.strip() == draft_commits[1]

    def test_grouped_integration_resume_preserves_same_diff_commit_with_other_author(
        self,
        grouped_remediation: Pilot,
        draft_commits: tuple[str, str],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Keep a checkout whose first commit has the recorded diff but is not this run's cherry-pick."""
        lifecycle, state_path, _, _ = grouped_remediation
        _collect_grouped_wave(grouped_remediation)
        integration = _crash_during_integration(grouped_remediation, tmp_path, monkeypatch, "cherry-pick")
        _git(integration, "commit", "--amend", "--no-edit", "--author=Other <other@example.com>")
        before = (_tree_snapshot(integration), state_path.read_bytes())

        with pytest.raises(lifecycle.PilotError, match=r"^integration-worktree-exists$"):
            lifecycle.integrate_write_pilot(state_path=state_path)

        assert (_tree_snapshot(integration), state_path.read_bytes()) == before

    @pytest.mark.parametrize("after_effect", _CRASH_POINTS)
    def test_resumes_integration_after_crash_around_worktree_creation(
        self,
        lifecycle: ModuleType,
        tmp_path: Path,
        repository_templates: RepositoryTemplates,
        validator: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        after_effect: bool,
    ) -> None:
        """Adopt a crash-left integration checkout that Git proves is fresh, or create it when it never appeared."""
        lifecycle, state_path, state, repository = _collected_remediation(lifecycle, tmp_path, repository_templates)
        integration = tmp_path / f"{state['worktree_root']}/integration"
        _crash_around_git(lifecycle, monkeypatch, _is_worktree_command("add", integration), after_effect=after_effect)
        with pytest.raises(_SimulatedCrash):
            lifecycle.integrate_write_pilot(state_path=state_path)
        monkeypatch.undo()
        assert integration.is_dir() is after_effect

        result = lifecycle.integrate_write_pilot(state_path=state_path)

        assert result["integration_status"] == "structurally-verified"
        lifecycle.apply_code_remediate_source(state_path=state_path)
        assert lifecycle.cleanup_code_remediate_pilot(state_path=state_path)["cleanup_status"] == "removed"
        assert _read(repository / "bucket-a.txt") == "child-a\n"
        _assert_completed_lifecycle(validator, state_path)

    def test_integration_resume_refuses_edited_crash_left_checkout(
        self,
        lifecycle: ModuleType,
        tmp_path: Path,
        repository_templates: RepositoryTemplates,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Never adopt a crash-left integration checkout that no longer matches a fresh detached baseline."""
        lifecycle, state_path, state, repository = _collected_remediation(lifecycle, tmp_path, repository_templates)
        integration = tmp_path / f"{state['worktree_root']}/integration"
        _crash_around_git(lifecycle, monkeypatch, _is_worktree_command("add", integration), after_effect=True)
        with pytest.raises(_SimulatedCrash):
            lifecycle.integrate_write_pilot(state_path=state_path)
        monkeypatch.undo()
        _write(integration / "bucket-a.txt", "edited after crash\n")

        with pytest.raises(lifecycle.PilotError, match=r"^integration-worktree-exists$"):
            lifecycle.integrate_write_pilot(state_path=state_path)

        assert read_json(state_path)["status"] == "joined"
        assert _read(repository / "bucket-a.txt") == "baseline-a\n"

    @pytest.mark.parametrize("after_effect", _CRASH_POINTS)
    def test_resumes_patch_application_after_crash(
        self,
        integrated_remediation: Pilot,
        tmp_path: Path,
        validator: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        after_effect: bool,
    ) -> None:
        """Finish a patch delivery whose source write did or did not land before the process died."""
        lifecycle, state_path, state, repository = integrated_remediation
        _crash_around_git(lifecycle, monkeypatch, _is_source_apply(repository), after_effect=after_effect)
        with pytest.raises(_SimulatedCrash):
            lifecycle.apply_code_remediate_source(state_path=state_path)
        monkeypatch.undo()
        assert read_json(state_path)["status"] == "source-applying"
        assert _read(repository / "bucket-a.txt") == ("child-a\n" if after_effect else "baseline-a\n")

        application = lifecycle.apply_code_remediate_source(state_path=state_path)

        assert application["status"] == "applied"
        assert read_json(state_path)["status"] == "source-applied"
        assert _read(repository / "bucket-a.txt") == "child-a\n"
        assert _read(repository / "bucket-b.txt") == "child-b\n"
        assert lifecycle.cleanup_code_remediate_pilot(state_path=state_path)["cleanup_status"] == "removed"
        assert all(not worktree.exists() for worktree in _node_worktrees(tmp_path, state))
        _assert_completed_lifecycle(validator, state_path)

    def test_delivery_rederives_identical_outputs_after_crash_before_intent_save(
        self, integrated_remediation: Pilot, validator: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Keep delivery patches a crash left on disk before the ``source-applying`` record was saved.

        The forward and rollback patches are written before the intent record, so a crash between them leaves the files
        while the state still says ``integrated``. Their bytes equal what a rerun derives again, so the rerun finishes.
        """
        lifecycle, state_path, _, repository = integrated_remediation
        _crash_before_delivery_intent(lifecycle, monkeypatch, state_path)

        assert lifecycle.apply_code_remediate_source(state_path=state_path)["status"] == "applied"

        assert lifecycle.cleanup_code_remediate_pilot(state_path=state_path)["cleanup_status"] == "removed"
        assert _read(repository / "bucket-a.txt") == "child-a\n"
        _assert_completed_lifecycle(validator, state_path)

    def test_delivery_refuses_different_leftover_outputs(
        self, integrated_remediation: Pilot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stop before any source write when a leftover delivery patch differs from the one derived again."""
        lifecycle, state_path, _, repository = integrated_remediation
        _crash_before_delivery_intent(lifecycle, monkeypatch, state_path)
        forward = state_path.parent / "source-application.patch"
        forward.write_bytes(b"unrelated patch bytes\n")

        with pytest.raises(lifecycle.PilotError, match=r"^source-application-output-exists$"):
            lifecycle.apply_code_remediate_source(state_path=state_path)

        assert forward.read_bytes() == b"unrelated patch bytes\n"
        assert _read(repository / "bucket-a.txt") == "baseline-a\n"

    def test_resume_restores_interrupted_patch_write_then_delivers(
        self, integrated_remediation: Pilot, validator: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Finish a patch delivery a crash cut short with one approved path written and the other still at baseline.

        ``git apply`` writes files one by one, so a crash part-way leaves a per-path mix of recorded preimages and
        postimages. Git shows every path holds a known image, so the rerun restores the baseline and delivers again.
        """
        lifecycle, state_path, _, repository = integrated_remediation
        _crash_around_git(lifecycle, monkeypatch, _is_source_apply(repository), after_effect=True)
        with pytest.raises(_SimulatedCrash):
            lifecycle.apply_code_remediate_source(state_path=state_path)
        monkeypatch.undo()
        _write(repository / "bucket-b.txt", "baseline-b\n")

        assert lifecycle.apply_code_remediate_source(state_path=state_path)["status"] == "applied"

        assert _read(repository / "bucket-a.txt") == "child-a\n"
        assert _read(repository / "bucket-b.txt") == "child-b\n"
        assert lifecycle.cleanup_code_remediate_pilot(state_path=state_path)["cleanup_status"] == "removed"
        _assert_completed_lifecycle(validator, state_path)

    @pytest.mark.parametrize(
        "index_written", [pytest.param(False, id="worktree-only"), pytest.param(True, id="index-and-worktree")]
    )
    def test_resume_restores_interrupted_fast_forward_checkout_then_delivers(
        self,
        gated_grouped_remediation: GatedGroupedRemediation,
        validator: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        index_written: bool,
    ) -> None:
        """Finish a grouped delivery whose killed fast-forward checked out one path before moving the branch.

        The fast-forward updates files, then the index, then the branch, so a crash can leave an approved path at its
        postimage (with or without its index entry) while the branch still points at the baseline.
        """
        lifecycle, state_path, _, repository = gated_grouped_remediation.pilot
        gates, head = gated_grouped_remediation.gates, gated_grouped_remediation.head
        _crash_around_git(lifecycle, monkeypatch, _is_source_merge(repository), after_effect=False)
        with pytest.raises(_SimulatedCrash):
            lifecycle.apply_code_remediate_source(state_path=state_path, gates_path=gates)
        monkeypatch.undo()
        if index_written:
            _git(repository, "checkout", head, "--", "bucket-a.txt")
        else:
            _write(repository / "bucket-a.txt", "child-a\n")

        assert lifecycle.apply_code_remediate_source(state_path=state_path, gates_path=gates)["status"] == "applied"

        assert _git(repository, "rev-parse", "HEAD").stdout.strip() == head
        assert _git(repository, "status", "--porcelain").stdout == ""
        assert lifecycle.cleanup_code_remediate_pilot(state_path=state_path)["cleanup_status"] == "removed"
        _assert_completed_lifecycle(validator, state_path)

    def test_resume_restores_a_file_the_interrupted_apply_deleted(
        self, integrated_remediation: Pilot, validator: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Finish a delivery a crash cut short while an approved file was deleted but not yet rewritten."""
        lifecycle, state_path, _, repository = integrated_remediation
        _crash_around_git(lifecycle, monkeypatch, _is_source_apply(repository), after_effect=True)
        with pytest.raises(_SimulatedCrash):
            lifecycle.apply_code_remediate_source(state_path=state_path)
        monkeypatch.undo()
        (repository / "bucket-a.txt").unlink()
        _write(repository / "bucket-b.txt", "baseline-b\n")

        assert lifecycle.apply_code_remediate_source(state_path=state_path)["status"] == "applied"

        assert _read(repository / "bucket-a.txt") == "child-a\n"
        assert _read(repository / "bucket-b.txt") == "child-b\n"
        assert lifecycle.cleanup_code_remediate_pilot(state_path=state_path)["cleanup_status"] == "removed"
        _assert_completed_lifecycle(validator, state_path)

    def test_resume_refuses_mix_with_staged_user_edit(
        self, integrated_remediation: Pilot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stop without restoring when an approved path's index entry matches no recorded image, keeping that stage."""
        lifecycle, state_path, _, repository = integrated_remediation
        _crash_around_git(lifecycle, monkeypatch, _is_source_apply(repository), after_effect=True)
        with pytest.raises(_SimulatedCrash):
            lifecycle.apply_code_remediate_source(state_path=state_path)
        monkeypatch.undo()
        _write(repository / "bucket-b.txt", "user staged edit\n")
        _git(repository, "add", "bucket-b.txt")
        _write(repository / "bucket-b.txt", "baseline-b\n")
        staged = _git(repository, "ls-files", "--stage", "--", "bucket-b.txt").stdout

        with pytest.raises(lifecycle.PilotError, match=r"^source-application-resume-ambiguous$"):
            lifecycle.apply_code_remediate_source(state_path=state_path)

        assert _git(repository, "ls-files", "--stage", "--", "bucket-b.txt").stdout == staged
        assert _read(repository / "bucket-a.txt") == "child-a\n"
        assert _read(repository / "bucket-b.txt") == "baseline-b\n"
        assert read_json(state_path)["status"] == "source-applying"

    def test_resume_waits_for_leftover_index_lock(
        self,
        gated_grouped_remediation: GatedGroupedRemediation,
        validator: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Change nothing while a killed fast-forward's index lock remains, then finish once the lock is cleared."""
        lifecycle, state_path, _, repository = gated_grouped_remediation.pilot
        gates, head = gated_grouped_remediation.gates, gated_grouped_remediation.head
        _crash_around_git(lifecycle, monkeypatch, _is_source_merge(repository), after_effect=False)
        with pytest.raises(_SimulatedCrash):
            lifecycle.apply_code_remediate_source(state_path=state_path, gates_path=gates)
        monkeypatch.undo()
        _git(repository, "checkout", head, "--", "bucket-a.txt")
        lock = repository / ".git" / "index.lock"
        lock.write_bytes(b"")

        with pytest.raises(lifecycle.PilotError, match=r"^source-repository-index-locked$"):
            lifecycle.apply_code_remediate_source(state_path=state_path, gates_path=gates)

        assert read_json(state_path)["status"] == "source-applying"
        assert _read(repository / "bucket-a.txt") == "child-a\n"
        lock.unlink()
        assert lifecycle.apply_code_remediate_source(state_path=state_path, gates_path=gates)["status"] == "applied"
        assert _git(repository, "rev-parse", "HEAD").stdout.strip() == head
        _assert_completed_lifecycle_after_cleanup(lifecycle, validator, state_path)

    def test_saves_over_state_temporary_left_by_crash(
        self, integrated_remediation: Pilot, validator: ModuleType
    ) -> None:
        """Overwrite the half-finished state write a crash left beside the state file instead of refusing every save.

        The leftover is never read: the committed state file stays authoritative and Git decides the rest.
        """
        lifecycle, state_path, _, repository = integrated_remediation
        leftover = state_path.with_name(f".{state_path.name}.tmp")
        _write(leftover, '{"status": "source-applied"}\n')

        assert lifecycle.apply_code_remediate_source(state_path=state_path)["status"] == "applied"

        assert not leftover.exists()
        assert _read(repository / "bucket-a.txt") == "child-a\n"
        _assert_completed_lifecycle_after_cleanup(lifecycle, validator, state_path)

    @_skip_no_symlinks
    def test_still_refuses_symlinked_state_temporary(self, integrated_remediation: Pilot, tmp_path: Path) -> None:
        """Keep refusing to save through a state temporary that is a symlink, which no crash can produce."""
        lifecycle, state_path, _, repository = integrated_remediation
        target = tmp_path / "elsewhere.json"
        _write(target, "{}\n")
        state_path.with_name(f".{state_path.name}.tmp").symlink_to(target)

        with pytest.raises(lifecycle.PilotError, match=r"^state-path-symlink-forbidden$"):
            lifecycle.apply_code_remediate_source(state_path=state_path)

        assert _read(target) == "{}\n"
        assert read_json(state_path)["status"] == "integrated"
        assert _read(repository / "bucket-a.txt") == "baseline-a\n"

    def test_patch_resume_refuses_unrecognized_source_bytes(
        self, integrated_remediation: Pilot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stop without touching the source when an owned file matches neither the baseline nor the delivery."""
        lifecycle, state_path, _, repository = integrated_remediation
        _crash_around_git(lifecycle, monkeypatch, _is_source_apply(repository), after_effect=True)
        with pytest.raises(_SimulatedCrash):
            lifecycle.apply_code_remediate_source(state_path=state_path)
        monkeypatch.undo()
        _write(repository / "bucket-a.txt", "user edit after crash\n")

        with pytest.raises(lifecycle.PilotError, match=r"^source-application-resume-ambiguous$"):
            lifecycle.apply_code_remediate_source(state_path=state_path)

        assert read_json(state_path)["status"] == "source-applying"
        assert _read(repository / "bucket-a.txt") == "user edit after crash\n"
        assert _read(repository / "bucket-b.txt") == "child-b\n"

    @pytest.mark.parametrize("after_effect", _CRASH_POINTS)
    def test_resumes_grouped_fast_forward_after_crash(
        self,
        gated_grouped_remediation: GatedGroupedRemediation,
        tmp_path: Path,
        validator: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        after_effect: bool,
    ) -> None:
        """Finish a grouped delivery whether or not the branch moved before the process died.

        After the move the branch tip must equal the recorded integration tip exactly; before it, the rerun performs the
        guarded fast-forward itself.
        """
        lifecycle, state_path, state, repository = gated_grouped_remediation.pilot
        gates = gated_grouped_remediation.gates
        _crash_around_git(lifecycle, monkeypatch, _is_source_merge(repository), after_effect=after_effect)
        with pytest.raises(_SimulatedCrash):
            lifecycle.apply_code_remediate_source(state_path=state_path, gates_path=gates)
        monkeypatch.undo()
        expected_tip = gated_grouped_remediation.head if after_effect else gated_grouped_remediation.baseline
        assert _git(repository, "rev-parse", "HEAD").stdout.strip() == expected_tip
        assert read_json(state_path)["status"] == "source-applying"

        application = lifecycle.apply_code_remediate_source(state_path=state_path, gates_path=gates)

        assert application["status"] == "applied"
        assert read_json(state_path)["source"]["applied_head"] == gated_grouped_remediation.head
        assert _git(repository, "rev-parse", "HEAD").stdout.strip() == gated_grouped_remediation.head
        assert lifecycle.cleanup_code_remediate_pilot(state_path=state_path)["cleanup_status"] == "removed"
        assert not gated_grouped_remediation.integration.exists()
        assert all(not worktree.exists() for worktree in _node_worktrees(tmp_path, state))
        _assert_completed_lifecycle(validator, state_path)

    def test_grouped_resume_refuses_unrecognized_branch_tip(
        self, gated_grouped_remediation: GatedGroupedRemediation, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stop without moving the branch when its tip after a crash is neither the baseline nor the gated tip."""
        lifecycle, state_path, _, repository = gated_grouped_remediation.pilot
        gates = gated_grouped_remediation.gates
        _crash_around_git(lifecycle, monkeypatch, _is_source_merge(repository), after_effect=True)
        with pytest.raises(_SimulatedCrash):
            lifecycle.apply_code_remediate_source(state_path=state_path, gates_path=gates)
        monkeypatch.undo()
        _git(repository, "commit", "-q", "--allow-empty", "-m", "User commit after crash")
        user_tip = _git(repository, "rev-parse", "HEAD").stdout.strip()

        with pytest.raises(lifecycle.PilotError, match=r"^source-application-resume-ambiguous$"):
            lifecycle.apply_code_remediate_source(state_path=state_path, gates_path=gates)

        assert _git(repository, "rev-parse", "HEAD").stdout.strip() == user_tip
        assert read_json(state_path)["status"] == "source-applying"

    @pytest.mark.parametrize("target_index", [pytest.param(0, id="first-child"), pytest.param(2, id="integration")])
    def test_resumes_cleanup_after_crash_following_removal(
        self,
        integrated_remediation: Pilot,
        tmp_path: Path,
        validator: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        target_index: int,
    ) -> None:
        """Record a removal Git already performed when the process died before saving it, then finish cleanup."""
        lifecycle, state_path, state, _ = integrated_remediation
        lifecycle.apply_code_remediate_source(state_path=state_path)
        targets = _cleanup_targets(tmp_path, state_path, state)
        victim = targets[target_index]
        _crash_around_git(lifecycle, monkeypatch, _is_worktree_command("remove", victim), after_effect=True)
        with pytest.raises(_SimulatedCrash):
            lifecycle.cleanup_code_remediate_pilot(state_path=state_path)
        monkeypatch.undo()
        relatives = [target.relative_to(tmp_path).as_posix() for target in targets]
        assert not victim.exists()
        assert read_json(state_path)["cleanup"]["worktrees"] == relatives[:target_index]

        cleanup = lifecycle.cleanup_code_remediate_pilot(state_path=state_path)

        assert cleanup["cleanup_status"] == "removed"
        assert read_json(state_path)["cleanup"] == {"status": "removed", "force": False, "worktrees": relatives}
        assert all(not target.exists() for target in targets)
        _assert_completed_lifecycle(validator, state_path)

    @pytest.mark.parametrize("target_index", [pytest.param(0, id="first-child"), pytest.param(2, id="integration")])
    def test_resumes_cleanup_after_crash_inside_removal(
        self,
        integrated_remediation: Pilot,
        tmp_path: Path,
        validator: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        target_index: int,
    ) -> None:
        """Unregister and record the pending worktree whose directory a crashed removal deleted, then finish cleanup.

        ``git worktree remove`` deletes the directory before Git's own registration, so a crash in between leaves the
        recorded pending target gone from disk while Git still lists it as prunable.
        """
        lifecycle, state_path, state, repository = integrated_remediation
        lifecycle.apply_code_remediate_source(state_path=state_path)
        targets = _cleanup_targets(tmp_path, state_path, state)
        victim = targets[target_index]
        _crash_inside_worktree_removal(lifecycle, monkeypatch, victim)
        with pytest.raises(_SimulatedCrash):
            lifecycle.cleanup_code_remediate_pilot(state_path=state_path)
        monkeypatch.undo()
        assert not victim.exists()
        assert victim in _registered_worktrees(repository)

        cleanup = lifecycle.cleanup_code_remediate_pilot(state_path=state_path)

        relatives = [target.relative_to(tmp_path).as_posix() for target in targets]
        assert cleanup["cleanup_status"] == "removed"
        assert read_json(state_path)["cleanup"] == {"status": "removed", "force": False, "worktrees": relatives}
        assert victim not in _registered_worktrees(repository)
        _assert_completed_lifecycle(validator, state_path)

    @pytest.mark.parametrize(
        "tamper",
        [
            pytest.param("later-worktree-removed", id="not-the-pending-target"),
            pytest.param("later-directory-deleted", id="another-worktree-also-prunable"),
            pytest.param("unrelated-directory-deleted", id="unrelated-worktree-also-prunable"),
        ],
    )
    def test_cleanup_resume_refuses_removal_git_cannot_prove(
        self, integrated_remediation: Pilot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
    ) -> None:
        """Keep rejecting a missing worktree unless it is the recorded pending target and only it is missing.

        A pending target whose directory is gone is reconciled only while no other worktree is prunable: pruning would
        otherwise also drop a registration this cleanup cannot prove it removed.
        """
        lifecycle, state_path, state, repository = integrated_remediation
        lifecycle.apply_code_remediate_source(state_path=state_path)
        first, second, _ = _cleanup_targets(tmp_path, state_path, state)
        _crash_around_git(lifecycle, monkeypatch, _is_worktree_command("remove", first), after_effect=False)
        with pytest.raises(_SimulatedCrash):
            lifecycle.cleanup_code_remediate_pilot(state_path=state_path)
        monkeypatch.undo()
        # A user's own worktree of the same repository, outside this run, whose directory is also gone.
        unrelated = tmp_path / "user-worktree"
        if tamper == "later-worktree-removed":
            # Patch-mode children still hold their owned edits, so only a forced out-of-band removal succeeds.
            _git(repository, "worktree", "remove", "--force", str(second))
        elif tamper == "later-directory-deleted":
            shutil.rmtree(first)
            shutil.rmtree(second)
        else:
            shutil.rmtree(first)
            _git(repository, "worktree", "add", "-q", "--detach", str(unrelated), "HEAD")
            shutil.rmtree(unrelated)

        with pytest.raises(lifecycle.PilotError, match=r"^cleanup-unrecorded-worktree-removal$"):
            lifecycle.cleanup_code_remediate_pilot(state_path=state_path)

        assert read_json(state_path)["cleanup"]["worktrees"] == []
        if tamper == "later-directory-deleted":
            assert {first, second} <= _registered_worktrees(repository)
        elif tamper == "unrelated-directory-deleted":
            assert {first, unrelated} <= _registered_worktrees(repository)

    @pytest.mark.parametrize("mode", _DELIVERY_MODES)
    def test_concurrent_apply_stops_before_saving_over_a_recorded_delivery(
        self,
        request: pytest.FixtureRequest,
        validator: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        mode: str,
    ) -> None:
        """Stop on the first save when another apply of the same run delivered and recorded it after this one loaded.

        Both applies load ``integrated``. The other one lands the delivery and records ``source-applied`` while this one
        is still checking; saving ``source-applying`` over that record, then reopening the run when preflight sees the
        moved source, would leave a delivered run that no later apply, resume or cleanup accepts.
        """
        route = _delivery_route(request, mode)
        lifecycle, state_path, _, repository = route.pilot
        recorded = _finish_other_apply_during_recheck(route, monkeypatch)

        with pytest.raises(lifecycle.PilotError, match=r"^lifecycle-state-concurrent-update$"):
            lifecycle.apply_code_remediate_source(state_path=state_path, gates_path=route.gates)
        monkeypatch.undo()

        assert state_path.read_bytes() == recorded[0]
        assert _apply_statuses(state_path) == ("source-applied", "applied")
        assert _git(repository, "rev-parse", "HEAD").stdout.strip() == route.delivered_head
        assert _read(repository / "bucket-a.txt") == "child-a\n"
        _assert_completed_lifecycle_after_cleanup(lifecycle, validator, state_path)

    @pytest.mark.parametrize("mode", _DELIVERY_MODES)
    def test_records_a_delivery_git_shows_landed_instead_of_reopening_the_run(
        self,
        request: pytest.FixtureRequest,
        validator: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        mode: str,
    ) -> None:
        """Record the delivery as applied when preflight fails only because Git already shows it landed exactly.

        The delivery reaches the source after this apply saved ``source-applying`` and before its preflight, with no
        record of it saved. Reopening the run as ``integrated`` would strand it: the source no longer matches the
        baseline, so every later apply fails and cleanup stays forbidden.
        """
        route = _delivery_route(request, mode)
        lifecycle, state_path, _, repository = route.pilot
        _land_after_delivery_intent(route, monkeypatch)

        application = lifecycle.apply_code_remediate_source(state_path=state_path, gates_path=route.gates)
        monkeypatch.undo()

        assert application["status"] == "applied"
        assert _apply_statuses(state_path) == ("source-applied", "applied")
        assert read_json(state_path)["source"]["applied_head"] == route.delivered_head
        assert _read(repository / "bucket-b.txt") == "child-b\n"
        _assert_completed_lifecycle_after_cleanup(lifecycle, validator, state_path)

    def test_reopens_the_run_when_the_landed_check_cannot_read_git(
        self, integrated_remediation: Pilot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reopen a run whose preflight failed when Git cannot answer whether the delivery landed.

        A read that fails proves nothing landed, so the preflight failure is recorded and raised as before, keeping the
        run retryable instead of leaving it ``source-applying`` with the probe's error.
        """
        lifecycle, state_path, _, repository = integrated_remediation
        _edit_owned_after_delivery_intent(integrated_remediation, monkeypatch)
        _fail_staged_path_probe(integrated_remediation, monkeypatch)

        with pytest.raises(lifecycle.PilotError, match=r"^source-repository-dirty$"):
            lifecycle.apply_code_remediate_source(state_path=state_path)
        monkeypatch.undo()

        assert _apply_statuses(state_path) == ("integrated", "preflight-failed")
        assert _read(repository / "bucket-a.txt") == "user edit during delivery\n"

    @pytest.mark.parametrize(
        "change_mode",
        [
            pytest.param(_set_worktree_exec_bit, id="worktree-exec-bit", marks=_skip_no_posix_modes),
            pytest.param(_stage_exec_bit, id="staged-exec-bit"),
        ],
    )
    def test_resume_refuses_mix_with_user_mode_change(
        self,
        integrated_remediation: Pilot,
        monkeypatch: pytest.MonkeyPatch,
        change_mode: Callable[[Path, str], None],
    ) -> None:
        """Stop without restoring when an approved path's mode differs from the baseline after a crash.

        Deliveries never change modes, so an executable bit set after the crash is the user's work even though the
        path's bytes still match a recorded image; restoring would silently drop it.
        """
        lifecycle, state_path, _, repository = integrated_remediation
        _crash_around_git(lifecycle, monkeypatch, _is_source_apply(repository), after_effect=True)
        with pytest.raises(_SimulatedCrash):
            lifecycle.apply_code_remediate_source(state_path=state_path)
        monkeypatch.undo()
        _write(repository / "bucket-b.txt", "baseline-b\n")
        change_mode(repository, "bucket-a.txt")
        staged = _git(repository, "ls-files", "--stage", "--", "bucket-a.txt").stdout
        working_mode = (repository / "bucket-a.txt").stat().st_mode

        with pytest.raises(lifecycle.PilotError, match=r"^source-application-resume-ambiguous$"):
            lifecycle.apply_code_remediate_source(state_path=state_path)

        assert _git(repository, "ls-files", "--stage", "--", "bucket-a.txt").stdout == staged
        assert (repository / "bucket-a.txt").stat().st_mode == working_mode
        assert _read(repository / "bucket-a.txt") == "child-a\n"
        assert _read(repository / "bucket-b.txt") == "baseline-b\n"
        assert read_json(state_path)["status"] == "source-applying"

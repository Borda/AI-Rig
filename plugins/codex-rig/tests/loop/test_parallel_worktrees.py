"""Acceptance checks for the parallel worktree lifecycle behind ``develop`` pilots and ``code-remediate`` buckets.

Each test drives the real lifecycle module (``shared/parallel_worktrees.py``) against throwaway Git repositories built
from committed templates, so Git behaviour is part of the contract; only a single failing ``_git`` step or an OS error
is ever replaced. Two plan routes share the lifecycle: the generated-fixture ``develop`` pilot (``prepare_write_pilot``)
and the schema-2 ``code-remediate`` production plan (``prepare_code_remediate_pilot``).
"""

from __future__ import annotations

import errno
import functools
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import NamedTuple
from unittest import mock

import pytest
from _artifact_files import read_json
from _modules import load_module

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
LIFECYCLE_PATH = PLUGIN_ROOT / "shared" / "parallel_worktrees.py"
VALIDATOR_PATH = PLUGIN_ROOT / "shared" / "validate-artifacts.py"

#: One distinct edit per child; the first child owns ``bucket-a.txt`` and the second ``bucket-b.txt``.
CHILD_EDITS = ("child-a\n", "child-b\n")


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
_skip_no_symlinks = pytest.mark.skipif(not SYMLINKS_SUPPORTED, reason="directory symlink capability unavailable")


def _load_lifecycle() -> ModuleType:
    """Load the installed-package-safe lifecycle module by file path."""
    return load_module("codex_rig_parallel_worktrees", LIFECYCLE_PATH)


def _load_validator() -> ModuleType:
    """Load the artifact validator for producer-to-consumer lifecycle checks."""
    return load_module("codex_rig_validate_artifacts", VALIDATOR_PATH)


def _git(repository: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run one local Git fixture command with stable captured output."""
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=check,
        text=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
    )


def _sha256(path: Path) -> str:
    """Return the SHA-256 of exact file bytes.

    Example:
        >>> len(_sha256(PLUGIN_ROOT / "package-manifest.json"))
        64
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, text: str) -> None:
    """Write LF-only UTF-8 text so byte-asserted fixtures match on every host."""
    path.write_text(text, encoding="utf-8", newline="\n")


def _write_json(path: Path, payload: object) -> None:
    """Write an indented JSON document with a trailing newline."""
    _write(path, json.dumps(payload, indent=2) + "\n")


def _read(path: Path) -> str:
    """Read UTF-8 text from disk."""
    return path.read_text(encoding="utf-8")


def _tree_snapshot(root: Path) -> dict[str, bytes] | None:
    """Return every file under ``root`` keyed by relative path, or None when ``root`` does not exist."""
    if not root.exists():
        return None
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def _child_handover(workspace: Path, node: dict[str, object]) -> dict[str, object]:
    """Build the five-field child result from the exact detached worktree diff."""
    worktree = workspace / str(node["worktree_path"])
    owned_paths = [str(path) for path in node["owned_paths"]]
    patch = subprocess.run(
        ["git", "-C", str(worktree), "diff", "--binary", "--full-index", "HEAD", "--", *owned_paths],
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
    ).stdout
    return {
        "node_id": node["node_id"],
        "status": "completed",
        "summary": f"Completed {node['node_id']}.",
        "changed_paths": owned_paths,
        "patch_sha256": hashlib.sha256(patch).hexdigest(),
    }


class RepositoryTemplates:
    """Generated fixture repositories committed once per session and copied into each test.

    Initializing and committing a repository costs several Git processes, which dominates fixture setup on Windows. Each
    distinct file set is committed once; every test receives a private copy, so a committed template is never mutated
    and every test still starts from the same committed bytes.
    """

    def __init__(self, root: Path) -> None:
        """Keep committed templates under one session-owned directory."""
        self._root = root
        self._committed: dict[tuple[str, tuple[tuple[str, str], ...]], tuple[Path, str, str]] = {}

    def copy(self, destination: Path, files: dict[str, str], message: str) -> tuple[str, str]:
        """Copy the repository committing ``files`` to ``destination`` and return its head and tree."""
        key = (message, tuple(files.items()))
        if key not in self._committed:
            self._committed[key] = self._commit(self._root / f"repository-{len(self._committed)}", files, message)
        template, head, tree = self._committed[key]
        shutil.copytree(template, destination, symlinks=True)
        return head, tree

    @staticmethod
    def _commit(repository: Path, files: dict[str, str], message: str) -> tuple[Path, str, str]:
        """Commit LF-only files without background maintenance racing later repository copies."""
        repository.mkdir()
        _git(repository, "init", "-q")
        _git(repository, "config", "maintenance.auto", "false")
        _git(repository, "config", "user.name", "Codex Test")
        _git(repository, "config", "user.email", "codex-test@example.invalid")
        for name, content in files.items():
            (repository / name).write_text(content, encoding="utf-8", newline="\n")
        _git(repository, "add", *files)
        _git(repository, "commit", "-q", "-m", message)
        head = _git(repository, "rev-parse", "HEAD").stdout.strip()
        return repository, head, _git(repository, "rev-parse", "HEAD^{tree}").stdout.strip()


@pytest.fixture(scope="session")
def repository_templates(tmp_path_factory: pytest.TempPathFactory) -> RepositoryTemplates:
    """Share committed fixture repositories across this test process; tests receive private copies."""
    return RepositoryTemplates(tmp_path_factory.mktemp("parallel-worktree-templates"))


@pytest.fixture
def lifecycle() -> ModuleType:
    """Load a fresh lifecycle module per test so patched attributes never leak between tests."""
    return _load_lifecycle()


@pytest.fixture
def validator() -> ModuleType:
    """Load a fresh artifact validator per test."""
    return _load_validator()


def _fixture_repository(workspace: Path, repository_templates: RepositoryTemplates) -> Path:
    """Create a committed generated repository with two tracked write buckets."""
    repository = workspace / ".reports" / "codex" / "develop" / "fixture" / "repository"
    repository_templates.copy(
        repository,
        {".gitignore": ".reports/\n", "bucket-a.txt": "baseline-a\n", "bucket-b.txt": "baseline-b\n"},
        "fixture baseline\n\nCo-authored-by: Codex <codex@openai.com>",
    )
    return repository


def _approved_plan(workspace: Path, repository_templates: RepositoryTemplates) -> tuple[Path, Path, Path]:
    """Write one exact approved two-node generated-fixture plan."""
    repository = _fixture_repository(workspace, repository_templates)
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
    _write_json(plan_path, plan)
    approval = {
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
    }
    _write_json(approval_path, approval)
    return plan_path, approval_path, repository


def _approved_code_remediate_plan(
    workspace: Path,
    repository_templates: RepositoryTemplates,
    *,
    pin_lf: bool = False,
    baseline_a: str = "baseline-a\n",
    authored_owned_paths: list[str] | None = None,
) -> tuple[Path, Path, Path]:
    """Write an approved plan with optional authored spellings for a multi-path bucket."""
    repository = workspace / "authoritative-repository"
    text_attributes = "*.txt text eol=lf\n" if pin_lf else "*.txt text\n"
    files = {".gitattributes": text_attributes, "bucket-a.txt": baseline_a, "bucket-b.txt": "baseline-b\n"}
    if authored_owned_paths is not None:
        files["bucket-c.txt"] = "baseline-c\n"
    baseline_head, baseline_tree = repository_templates.copy(
        repository,
        files,
        "production fixture baseline\n\nCo-authored-by: Codex <codex@openai.com>",
    )
    evidence = repository / ".reports" / "codex" / "code-remediate" / "fixture"
    evidence.mkdir(parents=True)
    plan_path = evidence / "work-bucket-plan.json"
    approval_path = evidence / "parallel-approval.json"
    context_paths = []
    for bucket_id, content in (("WRITE-A", "Context A\n"), ("WRITE-B", "Context B\n")):
        context_path = evidence / f"{bucket_id}-context.md"
        _write(context_path, content)
        context_paths.append(context_path)
    plan = {
        "schema_version": 2,
        "consumer": "code-remediate",
        "write_parallel_promoted": False,
        "source_repository": "authoritative-repository",
        "worktree_root": ".codex-rig-worktrees/fixture",
        "baseline_head": baseline_head,
        "baseline_tree": baseline_tree,
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
                "owned_paths": authored_owned_paths if authored_owned_paths is not None else ["bucket-a.txt"],
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
    _write_json(plan_path, plan)
    approval = {
        "plan_sha256": _sha256(plan_path),
        "prompt_presented": True,
        "response": "approve",
        "source": "user-prompt",
    }
    _write_json(approval_path, approval)
    return plan_path, approval_path, repository


def _rewrite_plan(plan_path: Path, approval_path: Path, plan: dict[str, object]) -> None:
    """Rewrite one intentional test-plan mutation with its matching approval digest."""
    _write_json(plan_path, plan)
    approval = read_json(approval_path)
    approval["plan_sha256"] = _sha256(plan_path)
    _write_json(approval_path, approval)


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


def _overlapping_code_remediate_plan(
    workspace: Path, repository_templates: RepositoryTemplates, baseline: str
) -> tuple[Path, Path, Path]:
    """Approve two isolated remediation buckets that both own one tracked file."""
    plan_path, approval_path, repository = _approved_code_remediate_plan(
        workspace, repository_templates, baseline_a=baseline
    )
    plan = read_json(plan_path)
    plan["work_buckets"][1]["owned_paths"] = ["bucket-a.txt"]
    _rewrite_plan(plan_path, approval_path, plan)
    return plan_path, approval_path, repository


def _prepare_remediation(
    lifecycle: ModuleType, workspace: Path, plan_path: Path, approval_path: Path, state_path: Path | None = None
) -> dict[str, object]:
    """Prepare a remediation pilot, defaulting the state file to the one the plan binds (beside the plan)."""
    return lifecycle.prepare_code_remediate_pilot(
        plan_path=plan_path,
        approval_path=approval_path,
        workspace_root=workspace,
        state_path=state_path or plan_path.parent / "production-lifecycle.json",
    )


def _prepare_generated(
    lifecycle: ModuleType, workspace: Path, plan_path: Path, approval_path: Path
) -> dict[str, object]:
    """Prepare a generated-fixture pilot with the conventional state file."""
    return lifecycle.prepare_write_pilot(
        plan_path=plan_path,
        approval_path=approval_path,
        workspace_root=workspace,
        state_path=workspace / ".reports" / "codex" / "develop" / "state.json",
    )


def _node_worktrees(workspace: Path, state: dict[str, object]) -> list[Path]:
    """Return every child worktree of a prepared pilot, in node order."""
    return [workspace / str(node["worktree_path"]) for node in state["nodes"]]


def _write_in_worktrees(
    workspace: Path, state: dict[str, object], relatives: Sequence[str], contents: Sequence[str]
) -> None:
    """Write one file into each child worktree, pairing node, relative path and content by position."""
    for worktree, relative, content in zip(_node_worktrees(workspace, state), relatives, contents, strict=True):
        _write(worktree / relative, content)


def _edit_owned(workspace: Path, state: dict[str, object], contents: Sequence[str]) -> None:
    """Give each child one valid edit of the first path it owns."""
    owned = [str(node["owned_paths"][0]) for node in state["nodes"]]
    _write_in_worktrees(workspace, state, owned, contents)


def _create_handovers(
    lifecycle: ModuleType, state_path: Path, state: dict[str, object], summary: str = "Completed {node_id}."
) -> list[dict[str, object]]:
    """Create every child's handover from its worktree; ``summary`` may mention ``{node_id}``."""
    return [
        lifecycle.create_completed_child_handover(
            state_path=state_path, node_id=str(node["node_id"]), summary=summary.format(node_id=node["node_id"])
        )
        for node in state["nodes"]
    ]


def _join(lifecycle: ModuleType, state_path: Path, state: dict[str, object], workspace: Path) -> dict[str, object]:
    """Join both valid child handovers for the prepared fixture."""
    return lifecycle.join_child_handovers(
        state_path=state_path, handovers=[_child_handover(workspace, node) for node in state["nodes"]]
    )


def _collect_all(lifecycle: ModuleType, state_path: Path, state: dict[str, object]) -> list[dict[str, object]]:
    """Collect every child's patch after the handovers have joined."""
    return [
        lifecycle.collect_write_patch(state_path=state_path, node_id=str(node["node_id"])) for node in state["nodes"]
    ]


def _write_other_children(workspace: Path, state: dict[str, object], excluded_node_id: str) -> None:
    """Give every non-target child one valid owned-path edit."""
    for node in state["nodes"]:
        if node["node_id"] == excluded_node_id:
            continue
        _write(workspace / str(node["worktree_path"]) / str(node["owned_paths"][0]), "other-child\n")


def _raise(error: Exception) -> None:
    """Raise ``error``; lets a test hand a failure to a hook as a plain callable."""
    raise error


def _rewrite_owned_as_crlf(repository: Path) -> None:
    """Rewrite both owned source files with CRLF line endings, as a clean-filter checkout would leave them."""
    for relative in ("bucket-a.txt", "bucket-b.txt"):
        path = repository / relative
        path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))


def _hook_after_source_apply(
    lifecycle: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    *hooks: Callable[[], None],
) -> None:
    """Run ``hooks`` in order right after the real source ``git apply`` so a test models what happens next."""
    real_git = lifecycle._git

    def _git_then_hooks(target: Path, *arguments: str) -> str:
        """Delegate to Git, then run the hooks once the authoritative source patch has been applied."""
        result = real_git(target, *arguments)
        if target == repository and arguments[0] == "apply" and "--check" not in arguments:
            for hook in hooks:
                hook()
        return result

    monkeypatch.setattr(lifecycle, "_git", _git_then_hooks)


def _record_lifecycle_git(
    lifecycle: ModuleType, monkeypatch: pytest.MonkeyPatch, *, refuse_batches: bool = False
) -> list[tuple[str, ...]]:
    """Record every lifecycle Git invocation, optionally failing any batched ``--git-path`` query."""
    real_git = lifecycle._git
    calls: list[tuple[str, ...]] = []

    def _record(target: Path, *arguments: str) -> str:
        """Run the real Git helper after noting its arguments."""
        calls.append(arguments)
        if refuse_batches and arguments.count("--git-path") > 1:
            raise lifecycle.PilotError("git-command-failed:rev-parse")
        return real_git(target, *arguments)

    monkeypatch.setattr(lifecycle, "_git", _record)
    return calls


def _apply_statuses(state_path: Path) -> tuple[object, object]:
    """Return the persisted lifecycle status and its source-application status."""
    persisted = read_json(state_path)
    return persisted["status"], persisted["source_application"]["status"]


def _join_arguments(state_path: Path, handover_paths: Sequence[Path]) -> list[str]:
    """Build the ``join`` command line for the given handover files."""
    handovers = [argument for path in handover_paths for argument in ("--handover", str(path))]
    return ["join", "--state", str(state_path), *handovers]


class Pilot(NamedTuple):
    """A prepared pilot: the lifecycle module, its state file, the prepared state and the source repository."""

    lifecycle: ModuleType
    state_path: Path
    state: dict[str, object]
    repository: Path


@pytest.fixture
def pilot(lifecycle: ModuleType, tmp_path: Path, repository_templates: RepositoryTemplates) -> Pilot:
    """Prepare a two-node generated pilot: detached child worktrees exist and nothing is edited yet."""
    plan_path, approval_path, repository = _approved_plan(tmp_path, repository_templates)
    state_path = tmp_path / ".reports" / "codex" / "develop" / "fixture" / "lifecycle.json"
    state = lifecycle.prepare_write_pilot(
        plan_path=plan_path, approval_path=approval_path, workspace_root=tmp_path, state_path=state_path
    )
    return Pilot(lifecycle, state_path, state, repository)


@pytest.fixture
def edited_pilot(pilot: Pilot, tmp_path: Path) -> Pilot:
    """Have each child edit the path it owns (``CHILD_EDITS``) in the prepared pilot."""
    _edit_owned(tmp_path, pilot.state, CHILD_EDITS)
    return pilot


@pytest.fixture
def joined_pilot(edited_pilot: Pilot, tmp_path: Path) -> Pilot:
    """Join both verified child handovers of the edited pilot."""
    _join(edited_pilot.lifecycle, edited_pilot.state_path, edited_pilot.state, tmp_path)
    return edited_pilot


@pytest.fixture
def collected_pilot(joined_pilot: Pilot) -> Pilot:
    """Collect both hash-bound patches of the joined pilot."""
    _collect_all(joined_pilot.lifecycle, joined_pilot.state_path, joined_pilot.state)
    return joined_pilot


def _prepare_integrated_code_remediate(
    lifecycle: ModuleType,
    workspace: Path,
    repository_templates: RepositoryTemplates,
    *,
    simulate_crlf_default: bool = False,
) -> Pilot:
    """Prepare and integrate a remediation fixture, optionally simulating a CRLF checkout default."""
    plan_path, approval_path, repository = _approved_code_remediate_plan(
        workspace, repository_templates, pin_lf=simulate_crlf_default
    )
    if simulate_crlf_default:
        _git(repository, "config", "core.autocrlf", "false")
        _git(repository, "config", "core.eol", "crlf")
    state_path = plan_path.parent / "production-lifecycle.json"
    state = _prepare_remediation(lifecycle, workspace, plan_path, approval_path)
    _edit_owned(workspace, state, CHILD_EDITS)
    lifecycle.join_child_handovers(state_path=state_path, handovers=_create_handovers(lifecycle, state_path, state))
    _collect_all(lifecycle, state_path, state)
    lifecycle.integrate_write_pilot(state_path=state_path)
    return Pilot(lifecycle, state_path, state, repository)


@pytest.fixture
def integrated_remediation(lifecycle: ModuleType, tmp_path: Path, repository_templates: RepositoryTemplates) -> Pilot:
    """Integrate the child patches of a remediation pilot without applying them to the source repository."""
    return _prepare_integrated_code_remediate(lifecycle, tmp_path, repository_templates)


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


def _contained_output_parent(evidence: Path) -> Path:
    """Create a real output directory inside the evidence root."""
    parent = evidence / "handover-output"
    parent.mkdir()
    return parent


def _symlinked_output_parent(evidence: Path) -> Path:
    """Create an output directory name that is a symlink to another directory inside the evidence root."""
    target = evidence / "contained-target"
    target.mkdir()
    parent = evidence / "handover-output"
    parent.symlink_to(target, target_is_directory=True)
    return parent


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
        assert _apply_statuses(state_path) == ("failed", "failed")

    def test_preflight_check_failure_records_terminal_state(
        self, integrated_remediation: Pilot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Keep unchanged source and terminal evidence when Git rejects patch preflight.

        ``git apply --check`` fails before anything is applied, so the source bytes and index must be untouched, no
        ``apply`` or ``restore`` may run, and the lifecycle must end in the terminal ``failed`` state.
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

        assert _apply_statuses(state_path) == ("failed", "failed")
        assert {path: (repository / path).read_bytes() for path in baseline} == baseline
        assert _git(repository, "ls-files", "--stage", "--", "bucket-a.txt", "bucket-b.txt").stdout == index
        assert source_mutations == []

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


# --- code-remediate: CLI lifecycle and receipt validation -----------------------------------------------------------


def _create_run(*arguments: str) -> Path:
    """Run the shipped run-directory creator for ``code-remediate`` and return the directory it printed."""
    created = subprocess.run(
        [sys.executable, str(PLUGIN_ROOT / "shared" / "create_run.py"), "--skill", "code-remediate", *arguments],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(created.stdout.strip())


def _keep_plan_location(repository: Path, plan_path: Path, approval_path: Path) -> tuple[Path, Path]:
    """Leave the plan and approval where the fixture wrote them."""
    return plan_path, approval_path


def _promote_to_pr_run(repository: Path, plan_path: Path, approval_path: Path) -> tuple[Path, Path]:
    """Move the plan and approval into a promoted ``pr-41/run-001`` evidence directory."""
    reports_root = repository / ".reports" / "codex"
    initial_run = _create_run("--root", str(reports_root))
    shutil.copytree(plan_path.parent, initial_run, dirs_exist_ok=True)
    pr_directory = initial_run / "pr"
    pr_directory.mkdir()
    (pr_directory / "pr.json").write_text(json.dumps({"number": 41}), encoding="utf-8")
    run = _create_run("--root", str(reports_root), "--promote-pr-run", str(initial_run))
    assert run.relative_to(repository).as_posix() == ".reports/codex/code-remediate/pr-41/run-001"
    return run / plan_path.name, run / approval_path.name


def _drive_cli_lifecycle(
    lifecycle: ModuleType, workspace: Path, plan_path: Path, approval_path: Path, state_path: Path
) -> tuple[list[int], dict[str, object]]:
    """Run every parent step through the CLI entry point; return each exit code and the prepared state."""
    state_arguments = ["--state", str(state_path)]
    prepare = ["prepare", "--plan", str(plan_path), "--approval", str(approval_path), "--workspace", str(workspace)]
    codes = [lifecycle.main([*prepare, *state_arguments])]
    state = read_json(state_path)
    for worktree, node, content in zip(_node_worktrees(workspace, state), state["nodes"], CHILD_EDITS, strict=True):
        for owned_path in node["owned_paths"]:
            _write(worktree / str(owned_path), content)
    handover_paths = [state_path.parent / f"{node['node_id']}-handover.json" for node in state["nodes"]]
    for node, handover_path in zip(state["nodes"], handover_paths, strict=True):
        summary = f"Completed {node['node_id']}."
        create = ["create-handover", *state_arguments, "--node", str(node["node_id"]), "--summary", summary]
        codes.append(lifecycle.main([*create, "--output", str(handover_path)]))
    codes.append(lifecycle.main(_join_arguments(state_path, handover_paths)))
    codes.extend(
        lifecycle.main(["collect", *state_arguments, "--node", str(node["node_id"])]) for node in state["nodes"]
    )
    codes.extend(lifecycle.main([step, *state_arguments]) for step in ("integrate", "apply-source", "cleanup"))
    return codes, state


@dataclass
class CompletedRemediation:
    """A remediation fixture driven through every CLI step to a completed lifecycle."""

    exit_codes: list[int]
    prepared_owned_paths: list[str]
    expected_owned_paths: list[str]
    plan_path: Path
    state_path: Path
    persisted: dict[str, object]
    bucket_plan: dict[str, object]


@pytest.fixture(
    scope="module",
    params=[
        pytest.param(
            (_keep_plan_location, None, ["bucket-a.txt"]),
            id="sorted-plan",
            marks=pytest.mark.xdist_group(name="remediation-sorted-plan"),
        ),
        pytest.param(
            (_promote_to_pr_run, None, ["bucket-a.txt"]),
            id="promoted-sorted-plan",
            marks=pytest.mark.xdist_group(name="remediation-promoted-plan"),
        ),
        pytest.param(
            (_keep_plan_location, ["bucket-c.txt", "bucket-a.txt"], ["bucket-a.txt", "bucket-c.txt"]),
            id="authored-unsorted-paths",
            marks=pytest.mark.xdist_group(name="remediation-unsorted-paths"),
        ),
        pytest.param(
            (_keep_plan_location, [".\\bucket-c.txt", ".//bucket-a.txt"], ["bucket-a.txt", "bucket-c.txt"]),
            id="portable-authored-spellings",
            marks=pytest.mark.xdist_group(name="remediation-portable-paths"),
        ),
    ],
)
def completed_remediation(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory, repository_templates: RepositoryTemplates
) -> CompletedRemediation:
    """Drive one remediation fixture through the whole CLI once per authored-path variant and share the evidence.

    Driving the lifecycle takes several real Git processes, so the receipt-validation tests reuse one completed run per
    variant. Tests that rewrite its state file put the original bytes back (see ``restored_state``).
    """
    relocate, authored_owned_paths, expected_owned_paths = request.param
    lifecycle = _load_lifecycle()
    workspace = tmp_path_factory.mktemp("remediation-cli")
    plan_path, approval_path, repository = _approved_code_remediate_plan(
        workspace, repository_templates, authored_owned_paths=authored_owned_paths
    )
    plan_path, approval_path = relocate(repository, plan_path, approval_path)
    state_path = plan_path.parent / "production-lifecycle.json"
    exit_codes, prepared = _drive_cli_lifecycle(lifecycle, workspace, plan_path, approval_path, state_path)
    return CompletedRemediation(
        exit_codes=exit_codes,
        prepared_owned_paths=prepared["nodes"][0]["owned_paths"],
        expected_owned_paths=expected_owned_paths,
        plan_path=plan_path,
        state_path=state_path,
        persisted=read_json(state_path),
        bucket_plan=read_json(plan_path),
    )


@pytest.fixture
def restored_state(completed_remediation: CompletedRemediation) -> None:
    """Put the completed lifecycle state file back after a test rewrote it."""
    original = completed_remediation.state_path.read_bytes()
    yield
    completed_remediation.state_path.write_bytes(original)


def _lifecycle_reference(state_path: Path) -> dict[str, object]:
    """Build the work-plan reference the validator resolves to the completed lifecycle state."""
    return {"production_lifecycle": {"path": state_path.name, "sha256": _sha256(state_path), "status": "completed"}}


def _unowned_paths(expected: list[str]) -> object:
    """Receipt paths naming a file no bucket owns."""
    return ["unowned.txt"]


def _duplicated_first_path(expected: list[str]) -> object:
    """Receipt paths repeating the first owned path."""
    return [*expected, expected[0]]


def _uppercased_first_path(expected: list[str]) -> object:
    """Receipt paths spelling the first owned path in a different case."""
    return [expected[0].upper(), *expected[1:]]


def _bare_string_path(expected: list[str]) -> object:
    """Receipt paths given as one string instead of a list."""
    return expected[0]


@pytest.mark.installed_plugin
@pytest.mark.integration
class TestCodeRemediateCli:
    """The supported parent lifecycle runs end to end through the CLI and its receipts validate."""

    def test_join_rejects_handover_outside_evidence_root(
        self, lifecycle: ModuleType, tmp_path: Path, repository_templates: RepositoryTemplates
    ) -> None:
        """Reject an otherwise valid external handover before it can satisfy a parent join.

        One of the two valid handover files is written beside the workspace instead of inside the evidence directory;
        the join must exit with status 2 and leave the lifecycle ``prepared``.
        """
        plan_path, approval_path, _ = _approved_code_remediate_plan(tmp_path, repository_templates)
        state_path = plan_path.parent / "production-lifecycle.json"
        state = _prepare_remediation(lifecycle, tmp_path, plan_path, approval_path)
        _edit_owned(tmp_path, state, CHILD_EDITS)
        outside_handover = tmp_path / "WRITE-A-handover.json"
        inside_handover = state_path.parent / "WRITE-B-handover.json"
        first, second = _create_handovers(lifecycle, state_path, state)
        _write_json(outside_handover, first)
        _write_json(inside_handover, second)

        exit_code = lifecycle.main(_join_arguments(state_path, [outside_handover, inside_handover]))

        assert exit_code == 2
        assert read_json(state_path)["status"] == "prepared"

    @_skip_no_symlinks
    @pytest.mark.parametrize(
        ("make_output_parent", "exit_code", "written"),
        [
            pytest.param(_contained_output_parent, 0, True, id="contained"),
            pytest.param(_symlinked_output_parent, 2, False, id="symlinked-component"),
        ],
    )
    def test_handover_output_rejects_intermediate_symlink(
        self,
        lifecycle: ModuleType,
        tmp_path: Path,
        repository_templates: RepositoryTemplates,
        make_output_parent: Callable[[Path], Path],
        exit_code: int,
        written: bool,
    ) -> None:
        """Allow a contained output but reject a symlinked parent even before its terminal exists.

        The handover is written to ``handover-output/WRITE-A.json``; when that directory name is a symlink the CLI must
        refuse with status 2 and write nothing, while a real directory is accepted.
        """
        plan_path, approval_path, _ = _approved_code_remediate_plan(tmp_path, repository_templates)
        state_path = plan_path.parent / "production-lifecycle.json"
        state = _prepare_remediation(lifecycle, tmp_path, plan_path, approval_path)
        _edit_owned(tmp_path, state, CHILD_EDITS)
        output_path = make_output_parent(state_path.parent) / "WRITE-A.json"

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

        assert result == exit_code
        assert output_path.exists() is written

    def test_drives_supported_parent_lifecycle(
        self, completed_remediation: CompletedRemediation, validator: ModuleType
    ) -> None:
        """Accept producer-sorted portable receipts without weakening exact recorded ownership checks.

        Four plan variants (sorted, PR-promoted, authored unsorted paths, portable authored spellings) are driven
        through prepare, handovers, join, collect, integrate, apply and cleanup; every step exits 0, the lifecycle
        completes, and the validator accepts the receipt against the authored plan.
        """
        completed = completed_remediation
        reference = _lifecycle_reference(completed.state_path)

        validator._validate_code_remediate_production_lifecycle(
            reference, completed.bucket_plan, completed.state_path.parent, _sha256(completed.plan_path)
        )

        assert completed.exit_codes == [0] * 9
        assert completed.prepared_owned_paths == completed.expected_owned_paths
        assert completed.persisted["status"] == "completed"
        assert completed.persisted["cleanup"] == {
            "force": False,
            "status": "removed",
            "worktrees": [
                ".codex-rig-worktrees/fixture/WRITE-A",
                ".codex-rig-worktrees/fixture/WRITE-B",
                ".codex-rig-worktrees/fixture/integration",
            ],
        }

    @pytest.mark.usefixtures("restored_state")
    @pytest.mark.parametrize(
        ("surface", "error"),
        [
            pytest.param(
                "nodes", "code-remediate-production-lifecycle-child-patch-path-invalid", id="child-patch-path"
            ),
            pytest.param("joined_nodes", "code-remediate-production-lifecycle-node-mismatch", id="joined-node"),
        ],
    )
    @pytest.mark.parametrize(
        "invalid_paths",
        [
            pytest.param(_unowned_paths, id="unowned"),
            pytest.param(_duplicated_first_path, id="duplicate"),
            pytest.param(_uppercased_first_path, id="case-altered"),
            pytest.param(_bare_string_path, id="bare-string"),
        ],
    )
    def test_validator_rejects_mutated_receipt_paths(
        self,
        completed_remediation: CompletedRemediation,
        validator: ModuleType,
        surface: str,
        error: str,
        invalid_paths: Callable[[list[str]], object],
    ) -> None:
        """Never normalize a mutated receipt into acceptance.

        The completed run's state file is rewritten so one node's owned paths are unowned, duplicated, re-cased or a
        bare string; the validator must reject it with the surface-specific error.
        """
        completed = completed_remediation
        mutated = json.loads(json.dumps(completed.persisted))
        mutated[surface][0]["owned_paths"] = invalid_paths(completed.expected_owned_paths)
        _write_json(completed.state_path, mutated)
        reference = _lifecycle_reference(completed.state_path)

        with pytest.raises(SystemExit, match=rf"^{error}$"):
            validator._validate_code_remediate_production_lifecycle(
                reference, completed.bucket_plan, completed.state_path.parent, _sha256(completed.plan_path)
            )

    @pytest.mark.parametrize(
        ("invalid_path", "error"),
        [
            pytest.param("bucket-a.txt:stream", "owned-path-invalid", id="alternate-data-stream"),
            pytest.param("bucket-a.txt\x00", "owned-path-invalid", id="nul-byte"),
            pytest.param("bucket-a.txt.", "owned-path-nonportable", id="trailing-dot"),
            pytest.param("bucket-a.txt ", "owned-path-nonportable", id="trailing-space"),
        ],
    )
    def test_producer_rejects_invalid_owned_path(
        self,
        lifecycle: ModuleType,
        tmp_path: Path,
        repository_templates: RepositoryTemplates,
        invalid_path: str,
        error: str,
    ) -> None:
        """Refuse to build lifecycle nodes for an owned path that is invalid or nonportable.

        Alternate data streams and NUL bytes are invalid outright; trailing dots and spaces name a different file on
        Windows, so they are nonportable.
        """
        plan_path, _, repository = _approved_code_remediate_plan(tmp_path, repository_templates)
        plan = read_json(plan_path)
        plan["work_buckets"][0]["owned_paths"] = [invalid_path]

        with pytest.raises(lifecycle.PilotError, match=rf"^{error}$"):
            lifecycle._code_remediate_nodes(plan, repository, plan_path.parent.relative_to(repository).as_posix())

    @pytest.mark.parametrize(
        "invalid_path",
        [
            pytest.param("bucket-a.txt:stream", id="alternate-data-stream"),
            pytest.param("bucket-a.txt\x00", id="nul-byte"),
            pytest.param("bucket-a.txt.", id="trailing-dot"),
            pytest.param("bucket-a.txt ", id="trailing-space"),
        ],
    )
    def test_validator_rejects_invalid_owned_path(
        self, completed_remediation: CompletedRemediation, validator: ModuleType, invalid_path: str
    ) -> None:
        """Reject a plan whose owned path the producer would refuse, even against a completed receipt.

        The validator re-derives the producer's portability verdict from the authored plan, so a plan edited after the
        run cannot be reconciled with the completed lifecycle.
        """
        completed = completed_remediation
        invalid_plan = json.loads(json.dumps(completed.bucket_plan))
        invalid_plan["work_buckets"][0]["owned_paths"] = [invalid_path]
        reference = _lifecycle_reference(completed.state_path)

        with pytest.raises(SystemExit, match=r"^code-remediate-production-lifecycle-owned-path-invalid$"):
            validator._validate_code_remediate_production_lifecycle(
                reference, invalid_plan, completed.state_path.parent, _sha256(completed.plan_path)
            )


# --- generated-fixture pilot: preparation ---------------------------------------------------------------------------


@pytest.mark.installed_plugin
@pytest.mark.integration
class TestPrepareWritePilot:
    """A generated-fixture plan is admitted only when approved, portable, disjoint and cleanly based."""

    def test_rejects_plan_changed_after_approval(
        self, lifecycle: ModuleType, tmp_path: Path, repository_templates: RepositoryTemplates
    ) -> None:
        """Prevent a stale approval from authorizing changed nodes or scope.

        One appended newline changes the plan bytes after approval; the stored digest no longer matches and no worktree
        may be created.
        """
        plan_path, approval_path, _ = _approved_plan(tmp_path, repository_templates)
        _write(plan_path, _read(plan_path) + "\n")

        with pytest.raises(lifecycle.PilotError, match=r"^plan-approval-digest-mismatch$"):
            _prepare_generated(lifecycle, tmp_path, plan_path, approval_path)

        assert not (tmp_path / ".reports" / "codex" / "develop" / "fixture" / "worktrees").exists()

    @pytest.mark.parametrize(
        ("owned_path", "error"),
        [
            pytest.param("../escape.txt", "owned-path-invalid", id="parent-escape"),
            pytest.param("/absolute.txt", "owned-path-invalid", id="posix-absolute"),
            pytest.param("C:\\absolute.txt", "owned-path-invalid", id="drive-absolute"),
            pytest.param("C:relative.txt", "owned-path-invalid", id="drive-relative"),
            pytest.param("\\\\server\\share.txt", "owned-path-invalid", id="unc-share"),
            pytest.param("bucket-a.txt.", "owned-path-nonportable", id="trailing-dot"),
            pytest.param("BUCKET-A.TXT", "owned-path-alias", id="case-alias"),
            pytest.param("bucket-a.txt/child", "owned-path-overlap", id="child-of-owned-file"),
        ],
    )
    def test_rejects_nonportable_or_aliased_owned_paths(
        self,
        lifecycle: ModuleType,
        tmp_path: Path,
        repository_templates: RepositoryTemplates,
        owned_path: str,
        error: str,
    ) -> None:
        """Block traversal, Windows aliases, and nonportable bucket ownership.

        The second node claims a path that escapes the repository, is absolute on some OS, differs only in case or
        trailing dot from the first node's file, or sits beneath it; each is refused with its own error code.
        """
        plan_path, approval_path, _ = _approved_plan(tmp_path, repository_templates)
        plan = read_json(plan_path)
        plan["stages"][0]["nodes"][1]["owned_paths"] = [owned_path]
        _rewrite_plan(plan_path, approval_path, plan)

        with pytest.raises(lifecycle.PilotError, match=rf"^{error}"):
            _prepare_generated(lifecycle, tmp_path, plan_path, approval_path)

    def test_rejects_overlapping_resource_locks(
        self, lifecycle: ModuleType, tmp_path: Path, repository_templates: RepositoryTemplates
    ) -> None:
        """Stop two otherwise disjoint packages from sharing one exclusive resource.

        Both nodes own different files but declare the same ``git-index`` lock, so they cannot run in parallel.
        """
        plan_path, approval_path, _ = _approved_plan(tmp_path, repository_templates)
        plan = read_json(plan_path)
        first, second = plan["stages"][0]["nodes"]
        first["resource_locks"] = ["git-index"]
        second["resource_locks"] = ["git-index"]
        _rewrite_plan(plan_path, approval_path, plan)

        with pytest.raises(lifecycle.PilotError, match=r"^resource-lock-overlap:FIXTURE-WRITE-B$"):
            _prepare_generated(lifecycle, tmp_path, plan_path, approval_path)

    @pytest.mark.parametrize(
        "dirty_file",
        [pytest.param("bucket-a.txt", id="tracked"), pytest.param("untracked.txt", id="untracked")],
    )
    def test_rejects_dirty_fixture_repository(
        self, lifecycle: ModuleType, tmp_path: Path, repository_templates: RepositoryTemplates, dirty_file: str
    ) -> None:
        """Prevent dispatch from a baseline with hidden tracked or untracked drift.

        The fixture repository gains one modified tracked file or one new untracked file after approval; either makes
        the approved baseline stale.
        """
        plan_path, approval_path, repository = _approved_plan(tmp_path, repository_templates)
        _write(repository / dirty_file, "dirty\n")

        with pytest.raises(lifecycle.PilotError, match=r"^fixture-repository-dirty$"):
            _prepare_generated(lifecycle, tmp_path, plan_path, approval_path)

    @_skip_no_symlinks
    def test_rejects_symlinked_generated_root(
        self, lifecycle: ModuleType, tmp_path: Path, repository_templates: RepositoryTemplates
    ) -> None:
        """Prevent a managed worktree path from escaping through a symlink component.

        The generated ``worktrees`` directory is a symlink to a directory outside the workspace.
        """
        plan_path, approval_path, _ = _approved_plan(tmp_path, repository_templates)
        outside = tmp_path / "outside"
        outside.mkdir()
        (tmp_path / ".reports" / "codex" / "develop" / "fixture" / "worktrees").symlink_to(
            outside, target_is_directory=True
        )

        with pytest.raises(lifecycle.PilotError, match=r"^managed-path-symlink-forbidden$"):
            _prepare_generated(lifecycle, tmp_path, plan_path, approval_path)

    def test_creates_detached_worktrees_at_exact_frozen_head(self, pilot: Pilot, tmp_path: Path) -> None:
        """Create isolated children without mutating the generated source checkout.

        Each child worktree must sit on the frozen baseline commit with a detached HEAD, and the source checkout must
        stay completely clean.
        """
        _, _, state, repository = pilot
        worktrees = _node_worktrees(tmp_path, state)

        heads = [_git(worktree, "rev-parse", "HEAD").stdout.strip() for worktree in worktrees]
        branch_lookups = [
            _git(worktree, "symbolic-ref", "-q", "HEAD", check=False).returncode for worktree in worktrees
        ]

        assert heads == [str(state["baseline_head"])] * 2
        assert branch_lookups == [1, 1]
        assert _git(repository, "status", "--porcelain=v1", "--untracked-files=all").stdout == ""

    def test_canonicalizes_multi_path_package_order(
        self, lifecycle: ModuleType, tmp_path: Path, repository_templates: RepositoryTemplates
    ) -> None:
        """Freeze multi-path ownership in the same lexical order used by parent verification.

        The first node is authored with ``bucket-c.txt`` before ``bucket-a.txt``; the state must list them sorted so
        parent verification compares like with like.
        """
        plan_path, approval_path, repository = _approved_plan(tmp_path, repository_templates)
        _write(repository / "bucket-c.txt", "baseline-c\n")
        _git(repository, "add", "bucket-c.txt")
        _git(repository, "commit", "-q", "-m", "add third fixture bucket")
        plan = read_json(plan_path)
        plan["stages"][0]["nodes"][0]["owned_paths"] = ["bucket-c.txt", "bucket-a.txt"]
        _rewrite_plan(plan_path, approval_path, plan)

        state = _prepare_generated(lifecycle, tmp_path, plan_path, approval_path)

        assert state["nodes"][0]["owned_paths"] == ["bucket-a.txt", "bucket-c.txt"]


# --- generated-fixture pilot: handovers and join --------------------------------------------------------------------


def _drop_last_handover(handovers: list[dict[str, object]]) -> None:
    """Omit the last child's handover."""
    handovers.pop()


def _duplicate_first_handover(handovers: list[dict[str, object]]) -> None:
    """Report the first child's handover twice instead of the second child's."""
    handovers[1] = dict(handovers[0])


def _set_first_handover_field(key: str, value: object, handovers: list[dict[str, object]]) -> None:
    """Set one field of the first child's handover."""
    handovers[0][key] = value


@pytest.mark.installed_plugin
@pytest.mark.integration
class TestChildHandover:
    """Children report through handovers that the parent verifies against the worktrees before joining."""

    @pytest.mark.parametrize("as_state_path", [pytest.param(Path, id="path"), pytest.param(str, id="string")])
    def test_accepts_canonical_state_path_forms(
        self, pilot: Pilot, tmp_path: Path, as_state_path: Callable[[Path], object]
    ) -> None:
        """Return the canonical report for supported lifecycle-state path forms.

        A ``Path`` and its string form must give the same five-field report that an independent diff of the child
        worktree produces, without advancing the lifecycle past ``prepared``.
        """
        lifecycle, state_path, state, _ = pilot
        node = state["nodes"][1]
        _write(_node_worktrees(tmp_path, state)[1] / "bucket-b.txt", "child-b\n")
        expected = _child_handover(tmp_path, node)
        expected["summary"] = "Updated bucket B."

        report = lifecycle.create_completed_child_handover(
            state_path=as_state_path(state_path), node_id="FIXTURE-WRITE-B", summary="Updated bucket B."
        )

        assert report == expected
        assert read_json(state_path)["status"] == "prepared"

    def test_rejects_invalid_state_path_type(self, pilot: Pilot) -> None:
        """Return the stable pilot error for an unsupported state-path object.

        Anything that is neither a ``Path`` nor a string must fail with the documented error code, not an arbitrary
        ``TypeError`` from deep inside the lifecycle.
        """
        lifecycle, _, _, _ = pilot

        with pytest.raises(lifecycle.PilotError, match=r"^lifecycle-state-path-invalid$"):
            lifecycle.create_completed_child_handover(
                state_path=object(), node_id="FIXTURE-WRITE-B", summary="Updated bucket B."
            )

    def test_rejects_relative_state_path(self, pilot: Pilot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Fail closed when a child resolves the parent state relative to its worktree.

        A child runs from inside its own worktree and passes the state path relative to the workspace; that would
        resolve to a different, child-controlled location, so a relative path is refused.
        """
        lifecycle, state_path, state, _ = pilot
        monkeypatch.chdir(_node_worktrees(tmp_path, state)[1])

        with pytest.raises(lifecycle.PilotError, match=r"^lifecycle-state-invalid$"):
            lifecycle.create_completed_child_handover(
                state_path=state_path.relative_to(tmp_path), node_id="FIXTURE-WRITE-B", summary="Updated bucket B."
            )

    def test_parent_authoritative_handovers_gate_patch_collection_and_integration(
        self, edited_pilot: Pilot, tmp_path: Path
    ) -> None:
        """Require both child reports and verify them against worktrees before integration.

        Both children edit their bucket and report; the parent joins the reports, collects patches whose digests match
        the reported ones, and integrates them into a separate worktree while the source stays at baseline.
        """
        lifecycle, state_path, state, repository = edited_pilot

        handovers = _create_handovers(lifecycle, state_path, state)
        joined = lifecycle.join_child_handovers(state_path=state_path, handovers=handovers)
        patches = _collect_all(lifecycle, state_path, state)
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
        assert _read(integration / "bucket-a.txt") == "child-a\n"
        assert _read(integration / "bucket-b.txt") == "child-b\n"
        assert _read(repository / "bucket-a.txt") == "baseline-a\n"

    @pytest.mark.parametrize(
        ("change_handovers", "error"),
        [
            pytest.param(_drop_last_handover, "child-handovers-incomplete", id="missing"),
            pytest.param(_duplicate_first_handover, "child-handover-invalid", id="duplicate"),
            pytest.param(
                functools.partial(_set_first_handover_field, "unexpected", True),
                "child-handover-invalid",
                id="extra-field",
            ),
            pytest.param(
                functools.partial(_set_first_handover_field, "summary", ""),
                "child-handover-summary-invalid:FIXTURE-WRITE-A",
                id="empty-summary",
            ),
            pytest.param(
                functools.partial(_set_first_handover_field, "changed_paths", ["bucket-b.txt"]),
                "child-handover-paths-mismatch:FIXTURE-WRITE-A",
                id="wrong-path",
            ),
            pytest.param(
                functools.partial(_set_first_handover_field, "patch_sha256", "0" * 64),
                "child-handover-patch-mismatch:FIXTURE-WRITE-A",
                id="wrong-hash",
            ),
        ],
    )
    def test_join_rejects_incomplete_or_mismatched_report(
        self,
        edited_pilot: Pilot,
        tmp_path: Path,
        change_handovers: Callable[[list[dict[str, object]]], None],
        error: str,
    ) -> None:
        """Reject partial, empty, or Git-inconsistent child reports.

        Valid reports are built from the worktrees and then damaged one way (missing, duplicated, extra field, empty
        summary, wrong paths, wrong patch hash). The join must fail with that reason, write no patch and keep the
        worktrees for diagnosis.
        """
        lifecycle, state_path, state, _ = edited_pilot
        handovers = [_child_handover(tmp_path, node) for node in state["nodes"]]
        change_handovers(handovers)

        with pytest.raises(lifecycle.PilotError, match=rf"^{error}$"):
            lifecycle.join_child_handovers(state_path=state_path, handovers=handovers)

        failed_state = read_json(state_path)
        assert failed_state["status"] == "join-failed"
        assert failed_state["join_failure"]["reason"] == error
        assert not (state_path.parent / "patch-a.diff").exists()
        assert all(worktree.exists() for worktree in _node_worktrees(tmp_path, state))

    @pytest.mark.parametrize("status", ["failed", "cancelled"])
    def test_join_records_unsuccessful_child_report(self, edited_pilot: Pilot, tmp_path: Path, status: str) -> None:
        """Reject a child that reports failure or cancellation, and keep what it reported.

        The first child's otherwise valid report says ``failed`` or ``cancelled``; the join stops with ``child-handover-
        not-completed`` and the state records which node and exactly what it sent.
        """
        lifecycle, state_path, state, _ = edited_pilot
        handovers = [_child_handover(tmp_path, node) for node in state["nodes"]]
        handovers[0]["status"] = status
        error = "child-handover-not-completed:FIXTURE-WRITE-A"

        with pytest.raises(lifecycle.PilotError, match=rf"^{error}$"):
            lifecycle.join_child_handovers(state_path=state_path, handovers=handovers)

        failed_state = read_json(state_path)
        assert failed_state["status"] == "join-failed"
        assert failed_state["join_failure"]["reason"] == error
        assert failed_state["join_failure"]["failed_node"] == "FIXTURE-WRITE-A"
        assert failed_state["join_failure"]["received_reports"][0]["status"] == status
        assert failed_state["join_failure"]["received_reports"][0]["summary"] == "Completed FIXTURE-WRITE-A."
        assert not (state_path.parent / "patch-a.diff").exists()
        assert all(worktree.exists() for worktree in _node_worktrees(tmp_path, state))


# --- generated-fixture pilot: collection ----------------------------------------------------------------------------


def _commit_owned_edit(worktree: Path) -> None:
    """Hide an owned edit in a child commit so the worktree looks clean."""
    _write(worktree / "bucket-a.txt", "child-a\n")
    _git(worktree, "add", "bucket-a.txt")
    _git(worktree, "commit", "-q", "-m", "forbidden child commit")


def _edit_unowned_file(worktree: Path) -> None:
    """Edit a tracked file that belongs to the other child."""
    _write(worktree / "bucket-b.txt", "forbidden\n")


def _create_untracked_file(worktree: Path) -> None:
    """Create a file that no bucket owns."""
    _write(worktree / "new.txt", "forbidden\n")


def _stage_child_owned_edit(worktree: Path) -> None:
    """Stage a valid owned edit, bypassing the patch-only index boundary."""
    _write(worktree / "bucket-a.txt", "staged\n")
    _git(worktree, "add", "bucket-a.txt")


def _delete_owned_file(worktree: Path) -> None:
    """Delete an owned file, which a content patch cannot express."""
    (worktree / "bucket-a.txt").unlink()


@pytest.mark.installed_plugin
@pytest.mark.integration
class TestCollect:
    """The parent collects only exact, owned, content-only child changes."""

    @pytest.mark.parametrize(
        ("damage_child", "error"),
        [
            pytest.param(_commit_owned_edit, "child-commit-forbidden", id="child-commit"),
            pytest.param(_edit_unowned_file, "child-owned-path-mismatch", id="outside-owned"),
            pytest.param(_create_untracked_file, "child-untracked-path-forbidden", id="untracked"),
            pytest.param(_stage_child_owned_edit, "child-index-change-forbidden", id="staged-owned"),
            pytest.param(_delete_owned_file, "child-patch-shape-forbidden", id="deletion"),
        ],
    )
    def test_join_rejects_forbidden_child_change(
        self,
        pilot: Pilot,
        tmp_path: Path,
        damage_child: Callable[[Path], None],
        error: str,
    ) -> None:
        """Prevent a child from smuggling commits, undeclared files, staged edits or deletions into a patch.

        The first child alters its worktree in a way the patch-only contract forbids while the second child behaves; the
        join must name the first child in the error, write no patch and retain the worktree.
        """
        lifecycle, state_path, state, _ = pilot
        worktree = _node_worktrees(tmp_path, state)[0]
        damage_child(worktree)
        _write_other_children(tmp_path, state, "FIXTURE-WRITE-A")

        with pytest.raises(lifecycle.PilotError, match=rf"^{error}:FIXTURE-WRITE-A$"):
            _join(lifecycle, state_path, state, tmp_path)

        assert worktree.exists()
        assert not (state_path.parent / "patch-a.diff").exists()

    def test_mode_only_change_rejected_without_filemode_capability(
        self, pilot: Pilot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reject a Git-reported mode-only patch without relying on chmod tracking.

        Git is made to report a pure mode change (100644 to 100755) for an owned file, so the shape check is exercised
        on hosts where chmod is not tracked.
        """
        lifecycle, _, state, _ = pilot
        node = state["nodes"][0]
        worktree = _node_worktrees(tmp_path, state)[0]
        real_git_bytes = lifecycle._git_bytes

        def _mode_only_raw(target: Path, *arguments: str) -> bytes:
            """Return mode-only raw diff evidence for patch-shape rejection."""
            if target == worktree and arguments[:3] == ("diff", "--raw", "--no-renames"):
                return b":100644 100755 deadbeef deadbeef M\0bucket-a.txt\0"
            return real_git_bytes(target, *arguments)

        monkeypatch.setattr(lifecycle, "_git_bytes", _mode_only_raw)

        with pytest.raises(lifecycle.PilotError, match=r"^child-patch-shape-forbidden:FIXTURE-WRITE-A$"):
            lifecycle._raw_content_updates(worktree, node)

    def test_rejects_worktree_drift_after_parent_join(self, joined_pilot: Pilot, tmp_path: Path) -> None:
        """Recheck the reported patch hash immediately before parent collection.

        A child edits its owned file again after the parent joined its handover; collection must notice the patch no
        longer matches the reported hash and write nothing.
        """
        lifecycle, state_path, state, _ = joined_pilot
        _write(_node_worktrees(tmp_path, state)[0] / "bucket-a.txt", "drifted-after-join\n")

        with pytest.raises(lifecycle.PilotError, match=r"^child-handover-drift:FIXTURE-WRITE-A$"):
            lifecycle.collect_write_patch(state_path=state_path, node_id="FIXTURE-WRITE-A")

        assert not (state_path.parent / "patch-a.diff").exists()

    def test_rejects_fabricated_operation_api(self, pilot: Pilot, tmp_path: Path) -> None:
        """Keep caller-created operation dictionaries outside the collector API.

        The collector derives its own Git evidence, so passing a caller-built ``operation`` must be a ``TypeError`` for
        an unknown keyword rather than being trusted.
        """
        lifecycle, state_path, state, _ = pilot
        _write(_node_worktrees(tmp_path, state)[0] / "bucket-a.txt", "child-a\n")

        with pytest.raises(TypeError, match="unexpected keyword argument 'operation'"):
            lifecycle.collect_write_patch(state_path=state_path, node_id="FIXTURE-WRITE-A", operation={})

    def test_prior_attempt_fingerprint_blocks_retained_worktree_mutation(
        self, lifecycle: ModuleType, tmp_path: Path, repository_templates: RepositoryTemplates
    ) -> None:
        """Freeze retained diagnostics and reject their mutation before current patch collection.

        A failed earlier attempt left detached worktrees that the plan records by fingerprint. One of them is edited
        after preparation, so joining the current attempt must stop and write no patch.
        """
        plan_path, approval_path, repository = _approved_plan(tmp_path, repository_templates)
        prior_root = tmp_path / ".reports" / "codex" / "develop" / "prior" / "worktrees"
        prior_root.mkdir(parents=True)
        _git(repository, "worktree", "add", "--detach", str(prior_root / "FIXTURE-WRITE-A"), "HEAD")
        _git(repository, "worktree", "add", "--detach", str(prior_root / "FIXTURE-WRITE-B"), "HEAD")
        runtime = tmp_path / ".reports" / "codex" / "develop" / "prior" / "runtime.json"
        _write(runtime, '{"status":"failed"}\n')
        plan = read_json(plan_path)
        plan["prior_attempt"] = {
            "runtime_record": runtime.relative_to(tmp_path).as_posix(),
            "status": "failed-retained",
            "retry_count": 0,
            "retained_worktree_root": prior_root.relative_to(tmp_path).as_posix(),
        }
        _rewrite_plan(plan_path, approval_path, plan)
        state_path = tmp_path / ".reports" / "codex" / "develop" / "fixture" / "lifecycle.json"
        state = lifecycle.prepare_write_pilot(
            plan_path=plan_path, approval_path=approval_path, workspace_root=tmp_path, state_path=state_path
        )
        _write(prior_root / "FIXTURE-WRITE-B" / "bucket-b.txt", "mutated\n")
        _write(_node_worktrees(tmp_path, state)[0] / "bucket-a.txt", "child-a\n")
        _write_other_children(tmp_path, state, "FIXTURE-WRITE-A")

        with pytest.raises(lifecycle.PilotError, match=r"^prior-attempt-fingerprint-mismatch$"):
            _join(lifecycle, state_path, state, tmp_path)

        assert not (state_path.parent / "patch-a.diff").exists()

    @mock.patch(
        "subprocess.run", autospec=True, side_effect=AssertionError("Git ran before state authority validation")
    )
    def test_state_authority_tamper_fails_before_git(self, mock_run: mock.Mock, pilot: Pilot) -> None:
        """Rederive managed roots before executing Git from mutable lifecycle state.

        The persisted repository path is redirected; collection must raise the root-drift error without starting any
        subprocess, so a tampered state file can never steer a Git command.
        """
        lifecycle, state_path, _, _ = pilot
        persisted = read_json(state_path)
        persisted["repository"] = ".reports/codex/develop/redirected"
        _write_json(state_path, persisted)

        with pytest.raises(lifecycle.PilotError, match=r"^lifecycle-state-root-drift$"):
            lifecycle.collect_write_patch(state_path=state_path, node_id="FIXTURE-WRITE-A")

        mock_run.assert_not_called()


# --- generated-fixture pilot: integration and cleanup ---------------------------------------------------------------


def _drift_approval(workspace: Path, state: dict[str, object], repository: Path) -> None:
    """Change the approval file bytes after the join."""
    approval = workspace / str(state["approval_path"])
    _write(approval, _read(approval) + "\n")


def _drift_source(workspace: Path, state: dict[str, object], repository: Path) -> None:
    """Dirty an approved path of the generated source repository after the join."""
    _write(repository / "bucket-a.txt", "source-drift\n")


@pytest.mark.installed_plugin
@pytest.mark.integration
class TestIntegrateAndCleanup:
    """Integration applies verified patches to an isolated worktree; cleanup removes only durable successes."""

    def test_requires_every_completed_joined_patch(self, pilot: Pilot) -> None:
        """Prevent integration before both child reports and patches are verified.

        A freshly prepared pilot has no joined children, so integration must refuse and leave the source untouched.
        """
        lifecycle, state_path, _, repository = pilot

        with pytest.raises(lifecycle.PilotError, match=r"^pilot-children-not-joined$"):
            lifecycle.integrate_write_pilot(state_path=state_path)

        assert _read(repository / "bucket-a.txt") == "baseline-a\n"

    def test_applies_patches_in_lexical_order(self, joined_pilot: Pilot, tmp_path: Path) -> None:
        """Apply both hash-bound patches to an isolated integration worktree in stable order.

        After collection each patch file must match its recorded digest; integration then applies A before B into its
        own worktree and the source repository keeps its baseline bytes.
        """
        lifecycle, state_path, state, repository = joined_pilot
        patches = _collect_all(lifecycle, state_path, state)

        result = lifecycle.integrate_write_pilot(state_path=state_path)

        integration = tmp_path / str(result["integration_worktree"])
        assert [_sha256(tmp_path / str(patch["patch_path"])) for patch in patches] == [
            patch["patch_sha256"] for patch in patches
        ]
        assert result["integration_order"] == ["FIXTURE-WRITE-A", "FIXTURE-WRITE-B"]
        assert _read(integration / "bucket-a.txt") == "child-a\n"
        assert _read(integration / "bucket-b.txt") == "child-b\n"
        assert _read(repository / "bucket-a.txt") == "baseline-a\n"

    def test_plan_drift_after_join_blocks_integration(self, collected_pilot: Pilot, tmp_path: Path) -> None:
        """Rehash immutable authority after joins before applying any child patch, and retain the worktrees.

        The approved plan gains a byte after patches were collected; integration must fail on the digest and leave all
        child worktrees in place.
        """
        lifecycle, state_path, state, _ = collected_pilot
        plan_path = tmp_path / str(state["plan_path"])
        _write(plan_path, _read(plan_path) + "\n")

        with pytest.raises(lifecycle.PilotError, match=r"^plan-approval-digest-mismatch$"):
            lifecycle.integrate_write_pilot(state_path=state_path)

        assert all(worktree.exists() for worktree in _node_worktrees(tmp_path, state))

    @pytest.mark.parametrize(
        ("drift", "expected"),
        [
            pytest.param(_drift_approval, "approval-drift-before-transition", id="approval"),
            pytest.param(_drift_source, "fixture-repository-dirty", id="source"),
        ],
    )
    def test_authority_or_source_drift_blocks_transition(
        self,
        joined_pilot: Pilot,
        tmp_path: Path,
        drift: Callable[[Path, dict[str, object], Path], None],
        expected: str,
    ) -> None:
        """Reject approval-byte or generated-source drift before another Git transition.

        After the join, either the approval file changes or the generated source repository becomes dirty; collecting
        the next patch must fail with the matching error and write nothing.
        """
        lifecycle, state_path, state, repository = joined_pilot
        drift(tmp_path, state, repository)

        with pytest.raises(lifecycle.PilotError, match=rf"^{expected}$"):
            lifecycle.collect_write_patch(state_path=state_path, node_id="FIXTURE-WRITE-A")

        assert not (state_path.parent / "patch-a.diff").exists()

    def test_patch_digest_tamper_blocks_integration(self, collected_pilot: Pilot, tmp_path: Path) -> None:
        """Reject changed patch bytes before creating an integration worktree.

        A newline is appended to the collected patch for the first child, so its digest no longer matches the state.
        """
        lifecycle, state_path, state, _ = collected_pilot
        patch = state_path.parent / "patch-a.diff"
        patch.write_bytes(patch.read_bytes() + b"\n")

        with pytest.raises(lifecycle.PilotError, match=r"^patch-digest-mismatch:FIXTURE-WRITE-A$"):
            lifecycle.integrate_write_pilot(state_path=state_path)

        assert not (tmp_path / str(state["worktree_root"]) / "integration").exists()

    def test_conflicting_patch_retains_failed_integration(self, collected_pilot: Pilot, tmp_path: Path) -> None:
        """Retain the integration worktree when a hash-bound later patch conflicts.

        The second child's patch is replaced by a copy of the first (with its recorded digest updated), so applying it
        conflicts; the failed integration worktree and all children must stay for inspection.
        """
        lifecycle, state_path, _, repository = collected_pilot
        persisted = read_json(state_path)
        patch_b = state_path.parent / "patch-b.diff"
        patch_b.write_bytes((state_path.parent / "patch-a.diff").read_bytes())
        persisted["nodes"][1]["patch_sha256"] = _sha256(patch_b)
        _write_json(state_path, persisted)

        with pytest.raises(lifecycle.PilotError, match=r"^patch-integration-failed:FIXTURE-WRITE-B$"):
            lifecycle.integrate_write_pilot(state_path=state_path)

        retained = read_json(state_path)
        assert retained["integration_status"] == "failed"
        assert (tmp_path / str(retained["integration_worktree"])).exists()
        assert all(worktree.exists() for worktree in _node_worktrees(tmp_path, retained))
        assert _read(repository / "bucket-a.txt") == "baseline-a\n"

    def test_cleanup_removes_only_persisted_successful_worktrees(self, collected_pilot: Pilot, tmp_path: Path) -> None:
        """Remove generated worktrees only after integration evidence is durable.

        After a successful integration, cleanup removes both child worktrees and the integration worktree and records
        each as absent.
        """
        lifecycle, state_path, state, _ = collected_pilot
        result = lifecycle.integrate_write_pilot(state_path=state_path)

        cleanup = lifecycle.cleanup_write_pilot(state_path=state_path)

        paths = [*_node_worktrees(tmp_path, state), tmp_path / str(result["integration_worktree"])]
        persisted = read_json(state_path)
        assert cleanup["cleanup_status"] == "removed"
        assert all(not path.exists() for path in paths)
        assert persisted["cleanup_status"] == "removed"
        assert all(item["postcondition"] == "absent" for item in persisted["cleanup"])

    def test_cleanup_failure_is_retained_and_blocks_success(
        self, collected_pilot: Pilot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Record non-force cleanup failure and retain every remaining diagnostic path.

        Git refuses to remove the first worktree (a lock, for example); cleanup must report the failing node, persist
        ``failed`` and leave that worktree in place instead of forcing removal.
        """
        lifecycle, state_path, state, _ = collected_pilot
        lifecycle.integrate_write_pilot(state_path=state_path)
        real_git = lifecycle._git

        def _fail_remove(repository: Path, *arguments: str) -> str:
            """Inject cleanup failure at worktree removal and delegate other Git commands."""
            if arguments[:2] == ("worktree", "remove"):
                raise lifecycle.PilotError("simulated-cleanup-lock")
            return real_git(repository, *arguments)

        monkeypatch.setattr(lifecycle, "_git", _fail_remove)
        with pytest.raises(lifecycle.PilotError, match=r"^cleanup-failed:FIXTURE-WRITE-A$"):
            lifecycle.cleanup_write_pilot(state_path=state_path)

        assert read_json(state_path)["cleanup_status"] == "failed"
        assert _node_worktrees(tmp_path, state)[0].exists()


# --- Git helper behaviour -------------------------------------------------------------------------------------------


def _generated_route(
    lifecycle: ModuleType, workspace: Path, repository_templates: RepositoryTemplates
) -> tuple[Path, list[dict[str, object]], Callable[[], list[object]], str]:
    """Build the generated-pilot route: repository, mutable buckets, a node validator and the second bucket's id."""
    plan_path, _, repository = _approved_plan(workspace, repository_templates)
    plan = read_json(plan_path)
    validate = functools.partial(lifecycle._validated_nodes, plan["stages"][0], repository)
    return repository, plan["stages"][0]["nodes"], validate, "FIXTURE-WRITE-B"


def _remediation_route(
    lifecycle: ModuleType, workspace: Path, repository_templates: RepositoryTemplates
) -> tuple[Path, list[dict[str, object]], Callable[[], list[object]], str]:
    """Build the code-remediate route: repository, mutable buckets, a node validator and the second bucket's id."""
    plan_path, _, repository = _approved_code_remediate_plan(workspace, repository_templates)
    plan = read_json(plan_path)
    evidence = plan_path.parent.relative_to(repository).as_posix()
    validate = functools.partial(lifecycle._code_remediate_nodes, plan, repository, evidence)
    return repository, plan["work_buckets"], validate, "WRITE-B"


def _keep_first_path(repository: Path) -> str:
    """Leave the first bucket's file where it is."""
    return "bucket-a.txt"


def _rename_first_path(repository: Path) -> str:
    """Move the first bucket's file to a name with spaces and a non-ASCII letter."""
    _git(repository, "mv", "bucket-a.txt", "owned path é.txt")
    return "owned path é.txt"


def _oversized_posix_batch() -> OSError:
    """Build the error POSIX raises when a command line is too long."""
    return OSError(errno.E2BIG, "probe")


def _oversized_windows_batch() -> OSError:
    """Build the error Windows raises when a command line is too long (``winerror`` 206)."""
    error = OSError(errno.EACCES, "probe")
    error.winerror = 206
    return error


def _fail_batched_launches(
    lifecycle: ModuleType, monkeypatch: pytest.MonkeyPatch, error: OSError
) -> list[tuple[str, ...]]:
    """Raise ``error`` for every multi-path Git launch, run single-path ones for real, and record all calls."""
    real_git = lifecycle._git
    calls: list[tuple[str, ...]] = []

    def _batch_limit(target: Path, *arguments: str) -> str:
        """Simulate only batch launch failure; individual checks execute Git."""
        calls.append(arguments)
        if len(arguments) > 4:
            raise error
        return real_git(target, *arguments)

    monkeypatch.setattr(lifecycle, "_git", _batch_limit)
    return calls


@pytest.mark.installed_plugin
@pytest.mark.integration
class TestGitHelpers:
    """The lifecycle's Git wrappers sanitize their environment and batch their queries without changing answers."""

    @mock.patch(
        "subprocess.run",
        autospec=True,
        return_value=subprocess.CompletedProcess(args=["git"], returncode=0, stdout="head\n", stderr=""),
    )
    def test_runner_sanitizes_git_environment_without_shell(
        self, mock_run: mock.Mock, lifecycle: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Remove Git overrides while preserving Windows-required child variables.

        ``GIT_*`` overrides (in any letter case) are dropped from the child environment, ``SYSTEMROOT`` and ``COMSPEC``
        survive because Windows children need them, and no shell is involved.
        """
        monkeypatch.setenv("GIT_DIR", "redirected")
        monkeypatch.setenv("git_work_tree", "redirected")
        monkeypatch.setenv("SYSTEMROOT", "C:\\Windows")
        monkeypatch.setenv("COMSPEC", "cmd.exe")

        lifecycle._git(tmp_path, "rev-parse", "HEAD")

        assert mock_run.called
        launch = mock_run.call_args_list[0].kwargs
        environment = launch["env"]
        assert isinstance(environment, dict)
        assert not any(key.upper().startswith("GIT_") for key in environment)
        assert environment["SYSTEMROOT"] == "C:\\Windows"
        assert environment["COMSPEC"] == "cmd.exe"
        assert launch["shell"] is False
        assert os.environ is not environment

    @pytest.mark.parametrize(
        ("creator", "name", "expected"),
        [
            pytest.param("touch", "MERGE_HEAD", "merging", id="merge"),
            pytest.param("touch", "CHERRY_PICK_HEAD", "cherry-picking", id="cherry-pick"),
            pytest.param("touch", "REVERT_HEAD", "reverting", id="revert"),
            pytest.param("mkdir", "rebase-merge", "rebasing", id="rebase-merge"),
            pytest.param("mkdir", "rebase-apply", "rebasing", id="rebase-apply"),
            pytest.param("touch", "UNRELATED_MARKER", None, id="idle"),
        ],
    )
    def test_pending_operation_is_read_with_one_git_process(
        self,
        lifecycle: ModuleType,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        creator: str,
        name: str,
        expected: str | None,
    ) -> None:
        """Report each pending operation by its own label from a single marker-path query.

        A repository gains exactly one operation marker; the batched answer must map every line back to the marker it
        was asked for, so swapping two answers would report the wrong operation or none.
        """
        _git(tmp_path, "init", "-q")
        getattr(tmp_path / ".git" / name, creator)()
        calls = _record_lifecycle_git(lifecycle, monkeypatch)

        assert lifecycle._git_operation(tmp_path) == expected
        assert calls == [
            (
                "rev-parse",
                "--git-path",
                "MERGE_HEAD",
                "--git-path",
                "CHERRY_PICK_HEAD",
                "--git-path",
                "REVERT_HEAD",
                "--git-dir",
            )
        ]

    def test_pending_operation_uses_the_linked_worktree_git_directory(
        self,
        lifecycle: ModuleType,
        tmp_path: Path,
        repository_templates: RepositoryTemplates,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Find a marker in a linked worktree's private Git directory and not in the main checkout.

        A linked worktree keeps MERGE_HEAD beside its own HEAD under the shared Git directory, where Git answers with an
        absolute path; the main checkout must stay idle while the linked one reports the merge.
        """
        repository = _fixture_repository(tmp_path, repository_templates)
        linked = tmp_path / "linked"
        _git(repository, "worktree", "add", "--detach", str(linked))
        Path(_git(linked, "rev-parse", "--git-path", "MERGE_HEAD").stdout.strip()).touch()
        calls = _record_lifecycle_git(lifecycle, monkeypatch)

        assert (lifecycle._git_operation(linked), lifecycle._git_operation(repository)) == ("merging", None)
        assert len(calls) == 2

    def test_pending_operation_falls_back_to_one_query_per_marker(
        self, lifecycle: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fall back to the unbatched queries, with the same answer, when the batched query is refused.

        A refused or ambiguous batched answer must never change the result: each marker and the Git directory are asked
        on their own, in reporting order, stopping at the first marker that exists.
        """
        _git(tmp_path, "init", "-q")
        (tmp_path / ".git" / "CHERRY_PICK_HEAD").touch()
        calls = _record_lifecycle_git(lifecycle, monkeypatch, refuse_batches=True)

        assert lifecycle._git_operation(tmp_path) == "cherry-picking"
        assert calls[1:] == [
            ("rev-parse", "--git-path", "MERGE_HEAD"),
            ("rev-parse", "--git-path", "CHERRY_PICK_HEAD"),
        ]

    def test_head_and_tree_are_read_with_one_git_process(
        self,
        lifecycle: ModuleType,
        tmp_path: Path,
        repository_templates: RepositoryTemplates,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Return the commit and tree identifiers a pair of single queries would, from one process.

        The combined query must give exactly the values two separate ``rev-parse`` calls give, using one Git invocation.
        """
        repository = _fixture_repository(tmp_path, repository_templates)
        expected = (
            _git(repository, "rev-parse", "HEAD").stdout.strip(),
            _git(repository, "rev-parse", "HEAD^{tree}").stdout.strip(),
        )
        calls = _record_lifecycle_git(lifecycle, monkeypatch)

        assert lifecycle._git_head_and_tree(repository) == expected
        assert calls == [("rev-parse", "HEAD", "HEAD^{tree}")]

    def test_head_and_tree_of_unborn_branch_keep_the_single_query_error(
        self, lifecycle: ModuleType, tmp_path: Path
    ) -> None:
        """Raise the same git-command-failed code a lone ``rev-parse HEAD`` raises when no commit exists.

        A repository with no commit has no HEAD; batching the two queries must not change the error callers see.
        """
        _git(tmp_path, "init", "-q")

        with pytest.raises(lifecycle.PilotError, match=r"^git-command-failed:rev-parse$"):
            lifecycle._git_head_and_tree(tmp_path)

    @pytest.mark.parametrize(
        "build_route",
        [pytest.param(_generated_route, id="generated"), pytest.param(_remediation_route, id="remediation")],
    )
    @pytest.mark.parametrize(
        "rename_first",
        [pytest.param(_keep_first_path, id="plain-path"), pytest.param(_rename_first_path, id="unicode-path")],
    )
    def test_owned_paths_batch_tracking_checks_and_recheck_index_drift(
        self,
        lifecycle: ModuleType,
        tmp_path: Path,
        repository_templates: RepositoryTemplates,
        monkeypatch: pytest.MonkeyPatch,
        build_route: Callable[..., tuple[Path, list[dict[str, object]], Callable[[], list[object]], str]],
        rename_first: Callable[[Path], str],
    ) -> None:
        """Batch real tracked-path reads without accepting a later untracked owned file.

        Both plan routes check every owned path with one ``ls-files --error-unmatch`` call (also for a path with spaces
        and a non-ASCII letter); removing a path from the index afterwards must make the next validation fail.
        """
        repository, buckets, validate, owner = build_route(lifecycle, tmp_path, repository_templates)
        first_path = rename_first(repository)
        buckets[0]["owned_paths"] = [first_path]
        calls = _record_lifecycle_git(lifecycle, monkeypatch)

        assert len(validate()) == 2
        assert calls == [("ls-files", "--error-unmatch", "--", first_path, "bucket-b.txt")]

        _git(repository, "rm", "--cached", "bucket-b.txt")
        with pytest.raises(lifecycle.PilotError, match=rf"^owned-path-not-tracked:{owner}$"):
            validate()

    @pytest.mark.parametrize(
        "make_error",
        [
            pytest.param(_oversized_posix_batch, id="posix-argv-limit"),
            pytest.param(_oversized_windows_batch, id="windows-argv-limit"),
        ],
    )
    def test_batch_launch_limit_falls_back_to_per_path_checks(
        self,
        lifecycle: ModuleType,
        tmp_path: Path,
        repository_templates: RepositoryTemplates,
        monkeypatch: pytest.MonkeyPatch,
        make_error: Callable[[], OSError],
    ) -> None:
        """Fall back to real per-path Git checks when an oversized batch cannot be launched.

        The batch of both owned paths hits the OS command-line limit (POSIX ``E2BIG`` or Windows error 206), so each
        path is checked on its own and validation still succeeds.
        """
        plan_path, _, repository = _approved_plan(tmp_path, repository_templates)
        stage = read_json(plan_path)["stages"][0]
        calls = _fail_batched_launches(lifecycle, monkeypatch, make_error())

        nodes = lifecycle._validated_nodes(stage, repository)

        assert len(nodes) == 2
        assert calls == [
            ("ls-files", "--error-unmatch", "--", "bucket-a.txt", "bucket-b.txt"),
            ("ls-files", "--error-unmatch", "--", "bucket-a.txt"),
            ("ls-files", "--error-unmatch", "--", "bucket-b.txt"),
        ]

    def test_unrelated_os_error_from_batch_launch_propagates(
        self,
        lifecycle: ModuleType,
        tmp_path: Path,
        repository_templates: RepositoryTemplates,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Let an OS error that is not an argument-limit failure escape instead of being retried.

        A permission error while launching the batch is not recoverable by splitting the batch, so it must reach the
        caller unchanged after exactly one attempt.
        """
        plan_path, _, repository = _approved_plan(tmp_path, repository_templates)
        stage = read_json(plan_path)["stages"][0]
        error = OSError(errno.EACCES, "probe")
        calls = _fail_batched_launches(lifecycle, monkeypatch, error)

        with pytest.raises(OSError, match="probe") as raised:
            lifecycle._validated_nodes(stage, repository)

        assert raised.value is error
        assert len(calls) == 1

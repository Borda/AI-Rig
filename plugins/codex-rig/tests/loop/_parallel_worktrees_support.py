"""Helpers shared by the parallel worktree lifecycle acceptance modules.

Those modules drive the real lifecycle module (``shared/parallel_worktrees.py``) against throwaway Git repositories
built from committed templates. This module holds the repository templates, plan and handover builders, Git and crash-
injection helpers, and record types that more than one of them uses; the pytest fixtures built on them live in the
sibling ``conftest.py``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from types import ModuleType
from typing import NamedTuple

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
    include_fixture_tests: bool = False,
) -> tuple[Path, Path, Path]:
    """Write an approved plan with optional authored spellings for a multi-path bucket."""
    repository = workspace / "authoritative-repository"
    text_attributes = "*.txt text eol=lf\n" if pin_lf else "*.txt text\n"
    files = {".gitattributes": text_attributes, "bucket-a.txt": baseline_a, "bucket-b.txt": "baseline-b\n"}
    if authored_owned_paths is not None:
        files["bucket-c.txt"] = "baseline-c\n"
    if include_fixture_tests:
        files[".gitignore"] = "__pycache__/\n.pytest_cache/\n"
        files["test_fixture.py"] = (
            "from pathlib import Path\n\n\n"
            "def test_integrated_topics():\n"
            '    assert Path("bucket-a.txt").read_text() == "child-a\\n"\n'
            '    assert Path("bucket-b.txt").read_text() == "child-b\\n"\n'
        )
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
            state_path=state_path,
            node_id=str(node["node_id"]),
            summary=summary.format(node_id=node["node_id"]),
            verification_path=(
                _write_worker_verification(
                    state_path.parent, Path(state["workspace_root"]) / node["worktree_path"], node
                )
                if state["schema_version"] == 3
                else None
            ),
        )
        for node in state["nodes"]
    ]


def _write_worker_verification(directory: Path, worktree: Path, node: dict[str, object]) -> Path:
    """Run the frozen targeted fixture command in its own committed worktree."""
    commit = _git(worktree, "rev-parse", "HEAD").stdout.strip()
    output = directory / f"{node['node_id']}-targeted.stdout.txt"
    output.write_text(_git(worktree, "diff", "--check", "HEAD^", "HEAD").stdout, encoding="utf-8")
    receipt = directory / f"{node['node_id']}-verification.json"
    _write_json(
        receipt,
        {
            "node_id": node["node_id"],
            "commit_oid": commit,
            "checks": [
                {
                    "command": "git diff --check HEAD^ HEAD",
                    "exit_code": 0,
                    "stdout": output.name,
                    "stdout_sha256": _sha256(output),
                }
            ],
        },
    )
    return receipt


def _collect_all(lifecycle: ModuleType, state_path: Path, state: dict[str, object]) -> list[dict[str, object]]:
    """Collect every child's patch after the handovers have joined."""
    return [
        lifecycle.collect_write_patch(state_path=state_path, node_id=str(node["node_id"])) for node in state["nodes"]
    ]


def _raise(error: Exception) -> None:
    """Raise ``error``; lets a test hand a failure to a hook as a plain callable."""
    raise error


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


def _apply_statuses(state_path: Path) -> tuple[object, object]:
    """Return the persisted lifecycle status and its source-application status."""
    persisted = read_json(state_path)
    return persisted["status"], persisted["source_application"]["status"]


class Pilot(NamedTuple):
    """A prepared pilot: the lifecycle module, its state file, the prepared state and the source repository."""

    lifecycle: ModuleType
    state_path: Path
    state: dict[str, object]
    repository: Path


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


def _use_grouped_plan(plan_path: Path, approval_path: Path, repository: Path) -> None:
    """Freeze the plan for grouped commits on the currently attached source branch."""
    plan = read_json(plan_path)
    plan.update(
        schema_version=3,
        commit_mode="grouped",
        commit_choice={"mode": "grouped", "source": "explicit-input"},
        source_branch=_git(repository, "symbolic-ref", "HEAD").stdout.strip(),
        tests_command=_fixture_tests_command(),
    )
    for bucket in plan["work_buckets"]:
        bucket["verification_commands"] = ["git diff --check HEAD^ HEAD"]
    (plan_path.parent / "resolution-workplan.md").write_text(
        "## Upfront Decisions\n\nCommit choice: group findings by topic (explicit-input)\n"
        f"Full test command: {_fixture_tests_command()}\n",
        encoding="utf-8",
    )
    _rewrite_plan(plan_path, approval_path, plan)


def _draft_commit(worktree: Path, relative: str, contents: str, subject: str) -> str:
    """Commit one worker fixture edit with the required attribution and return its exact Git object ID."""
    _write(worktree / relative, contents)
    _git(worktree, "add", "--", relative)
    _git(worktree, "commit", "-q", "-m", subject, "-m", "Co-authored-by: Codex <codex@openai.com>")
    return _git(worktree, "rev-parse", "HEAD").stdout.strip()


def _fixture_tests_command() -> str:
    """Return the exact full grouped fixture invocation for the host shell."""
    executable = (
        "& '" + sys.executable.replace("'", "''") + "'" if sys.platform == "win32" else shlex.quote(sys.executable)
    )
    return f"{executable} -m pytest -q -p no:xdist"


def _run_grouped_gates(out: Path, integration: Path, head: str) -> Path:
    """Run the real gate runner against the two integrated fixture commits."""
    command = "git diff --check HEAD~2 HEAD"
    gate_arguments = [argument for gate in ("lint", "format", "types", "review") for argument in (f"--{gate}", command)]
    completed = subprocess.run(
        [
            sys.executable,
            str(PLUGIN_ROOT / "shared" / "run_gates.py"),
            "--out",
            str(out),
            "--worktree",
            str(integration),
            "--expected-head",
            head,
            "--tests",
            _fixture_tests_command(),
            *gate_arguments,
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    return out / "gates.json"


class GatedGroupedRemediation(NamedTuple):
    """A grouped pilot whose integration tip passed the real gates and waits for delivery to the source branch."""

    pilot: Pilot
    integration: Path
    baseline: str
    head: str
    gates: Path


class _SimulatedCrash(Exception):
    """Model the process dying mid-transition; lifecycle handlers catch only ``PilotError`` and ``OSError``."""


def _crash_around_git(
    lifecycle: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    matches: Callable[[Path, tuple[str, ...]], bool],
    *,
    after_effect: bool,
) -> None:
    """Kill the lifecycle at the first matching Git command, either just before it runs or right after it succeeds.

    Crashing after the effect leaves Git changed while the state file still describes the moment before it, which is
    exactly what a killed process leaves behind.
    """
    real_git = lifecycle._git
    fired: list[tuple[str, ...]] = []

    def _git_or_crash(target: Path, *arguments: str) -> str:
        """Run Git normally except for the first matching command, which crashes around its effect."""
        if fired or not matches(target, arguments):
            return real_git(target, *arguments)
        fired.append(arguments)
        if after_effect:
            real_git(target, *arguments)
        raise _SimulatedCrash(" ".join(arguments[:2]))

    monkeypatch.setattr(lifecycle, "_git", _git_or_crash)


def _is_source_apply(repository: Path) -> Callable[[Path, tuple[str, ...]], bool]:
    """Match the real ``git apply`` that writes the integrated patch into the authoritative source."""
    return lambda target, arguments: target == repository and arguments[:1] == ("apply",) and "--check" not in arguments


def _is_source_merge(repository: Path) -> Callable[[Path, tuple[str, ...]], bool]:
    """Match the fast-forward that moves the authoritative branch to the gated integration tip."""
    return lambda target, arguments: target == repository and arguments[:1] == ("merge",)


def _is_worktree_command(command: str, worktree: Path | None = None) -> Callable[[Path, tuple[str, ...]], bool]:
    """Match one ``git worktree`` subcommand, optionally only for the named worktree path."""
    return lambda _, arguments: (
        arguments[:2] == ("worktree", command) and (worktree is None or str(worktree) in arguments)
    )


def _cleanup_targets(workspace: Path, state_path: Path, state: dict[str, object]) -> list[Path]:
    """Return every worktree cleanup removes, in its fixed order: children first, integration last."""
    return [*_node_worktrees(workspace, state), workspace / str(read_json(state_path)["integration_worktree"])]


def _registered_worktrees(repository: Path) -> set[Path]:
    """Return every worktree path Git registers for ``repository``, including ones whose directory is gone."""
    listing = _git(repository, "worktree", "list", "--porcelain").stdout
    return {Path(line.removeprefix("worktree ")) for line in listing.splitlines() if line.startswith("worktree ")}


def _crash_inside_worktree_removal(
    lifecycle: ModuleType, monkeypatch: pytest.MonkeyPatch, victim: Path, deleted: Sequence[str] = ()
) -> None:
    """Die inside ``git worktree remove`` of ``victim`` after Git deleted some or all of it, before it unregisters it.

    Git checks that the worktree is clean, deletes its directory entry by entry and drops its own registration last.
    With ``deleted`` empty the whole directory is gone; otherwise only those worktree-relative entries (``.git``
    included) are, leaving the partly deleted directory a crash in the middle of Git's delete leaves.
    """
    real_git = lifecycle._git

    def _git_or_crash(target: Path, *arguments: str) -> str:
        """Delete what Git deletes first, then crash before the registration is removed."""
        if _is_worktree_command("remove", victim)(target, arguments):
            if not deleted:
                shutil.rmtree(victim)
            for relative in deleted:
                victim.joinpath(*relative.split("/")).unlink()
            raise _SimulatedCrash("worktree remove")
        return real_git(target, *arguments)

    monkeypatch.setattr(lifecycle, "_git", _git_or_crash)


def _crash_after_patch_output(pilot: Pilot, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path, bytes]:
    """Leave the first patch on disk with its collection record still unsaved."""
    lifecycle, state_path, state, _ = pilot
    node = state["nodes"][0]
    node_id = node["node_id"]
    patch_path = state_path.parent / node["output"]
    with monkeypatch.context() as crash:
        crash.setattr(lifecycle, "_persist", lambda *_: _raise(_SimulatedCrash()))
        with pytest.raises(_SimulatedCrash):
            lifecycle.collect_write_patch(state_path=state_path, node_id=node_id)
    assert read_json(state_path)["nodes"][0]["patch_status"] == "not-collected"
    return node_id, patch_path, patch_path.read_bytes()


def _assert_completed_lifecycle(validator: ModuleType, state_path: Path) -> None:
    """Require the finished lifecycle to pass the same validator a published remediation must pass."""
    plan = state_path.parent / "work-bucket-plan.json"
    validator._validate_code_remediate_production_lifecycle(
        _lifecycle_reference(state_path), read_json(plan), state_path.parent, _sha256(plan)
    )


def _assert_completed_lifecycle_after_cleanup(lifecycle: ModuleType, validator: ModuleType, state_path: Path) -> None:
    """Run the non-force cleanup of an applied lifecycle, then require it to pass the published-run validator."""
    assert lifecycle.cleanup_code_remediate_pilot(state_path=state_path)["cleanup_status"] == "removed"
    _assert_completed_lifecycle(validator, state_path)


class _DeliveryRoute(NamedTuple):
    """An integrated run waiting for source delivery, with what one apply of it needs."""

    pilot: Pilot
    integration: Path
    gates: Path | None
    delivered_head: str


def _delivery_route(request: pytest.FixtureRequest, mode: str) -> _DeliveryRoute:
    """Build the patch or grouped run waiting for delivery; a delivered patch run keeps its baseline commit."""
    if mode == "grouped":
        gated: GatedGroupedRemediation = request.getfixturevalue("gated_grouped_remediation")
        return _DeliveryRoute(gated.pilot, gated.integration, gated.gates, gated.head)
    pilot: Pilot = request.getfixturevalue("integrated_remediation")
    workspace: Path = request.getfixturevalue("tmp_path")
    integration = workspace / str(read_json(pilot.state_path)["integration_worktree"])
    return _DeliveryRoute(pilot, integration, None, _git(pilot.repository, "rev-parse", "HEAD").stdout.strip())


_DELIVERY_MODES = [
    pytest.param("patch", id="patch"),
    pytest.param("grouped", id="grouped"),
]


def _lifecycle_reference(state_path: Path) -> dict[str, object]:
    """Build the work-plan reference the validator resolves to the completed lifecycle state."""
    return {"production_lifecycle": {"path": state_path.name, "sha256": _sha256(state_path), "status": "completed"}}

"""Fixtures shared by the parallel worktree lifecycle acceptance modules.

They build the throwaway template repositories, load the lifecycle and artifact validator modules, and prepare the
generated-fixture pilot and the ``code-remediate`` runs those modules exercise. A module-level fixture of the same name
still takes precedence in any other module under this directory.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

import pytest
from _parallel_worktrees_support import (
    GatedGroupedRemediation,
    Pilot,
    RepositoryTemplates,
    _approved_code_remediate_plan,
    _approved_plan,
    _collect_all,
    _create_handovers,
    _draft_commit,
    _git,
    _load_lifecycle,
    _load_validator,
    _node_worktrees,
    _prepare_integrated_code_remediate,
    _prepare_remediation,
    _run_grouped_gates,
    _use_grouped_plan,
)


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
def integrated_remediation(lifecycle: ModuleType, tmp_path: Path, repository_templates: RepositoryTemplates) -> Pilot:
    """Integrate the child patches of a remediation pilot without applying them to the source repository."""
    return _prepare_integrated_code_remediate(lifecycle, tmp_path, repository_templates)


@pytest.fixture
def grouped_remediation(lifecycle: ModuleType, tmp_path: Path, repository_templates: RepositoryTemplates) -> Pilot:
    """Prepare two disjoint grouped commit worktrees while keeping the authoritative branch clean."""
    plan_path, approval_path, repository = _approved_code_remediate_plan(
        tmp_path, repository_templates, include_fixture_tests=True
    )
    _use_grouped_plan(plan_path, approval_path, repository)
    with (repository / ".git" / "info" / "exclude").open("a", encoding="utf-8", newline="\n") as stream:
        stream.write("\n/.reports/\n")
    state_path = plan_path.parent / "production-lifecycle.json"
    state = _prepare_remediation(lifecycle, tmp_path, plan_path, approval_path)
    return Pilot(lifecycle, state_path, state, repository)


@pytest.fixture
def draft_commits(grouped_remediation: Pilot, tmp_path: Path) -> tuple[str, str]:
    """Write distinct commits for the two groups and return their IDs in planned node order."""
    first, second = _node_worktrees(tmp_path, grouped_remediation.state)
    return (
        _draft_commit(first, "bucket-a.txt", "child-a\n", "Edit bucket A"),
        _draft_commit(second, "bucket-b.txt", "child-b\n", "Edit bucket B"),
    )


@pytest.fixture
def gated_grouped_remediation(
    grouped_remediation: Pilot, draft_commits: tuple[str, str], tmp_path: Path
) -> GatedGroupedRemediation:
    """Join, collect and integrate both topic commits, then run the full gates on the integration tip."""
    lifecycle, state_path, state, repository = grouped_remediation
    assert draft_commits
    baseline = _git(repository, "rev-parse", "HEAD").stdout.strip()
    lifecycle.join_child_handovers(state_path=state_path, handovers=_create_handovers(lifecycle, state_path, state))
    _collect_all(lifecycle, state_path, state)
    result = lifecycle.integrate_write_pilot(state_path=state_path)
    integration = tmp_path / str(result["integration_worktree"])
    head = str(result["integration_head"])
    gates = _run_grouped_gates(state_path.parent, integration, head)
    return GatedGroupedRemediation(grouped_remediation, integration, baseline, head, gates)

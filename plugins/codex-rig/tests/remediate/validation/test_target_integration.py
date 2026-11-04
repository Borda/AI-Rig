"""Require actual fetched-target integration before remediation accepts a clean merge receipt."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any, NamedTuple

import pytest
from test_finding_presentation import _selection
from test_remediation_finalize import HELPER
from test_review_completion_gate import _module

PLUGIN_ROOT = Path(__file__).resolve().parents[3]


class IntegrationRepository(NamedTuple):
    """Keep the divergent PR and fetched-target identities with their local checkout receipt."""

    root: Path
    pr_dir: Path
    pre_head: str
    target: str


def _git(root: Path, *arguments: str) -> str:
    """Run real local Git and retain command failures instead of hiding fixture setup errors."""
    result = subprocess.run(["git", *arguments], cwd=root, capture_output=True, text=True, check=True)
    return result.stdout.strip()


@pytest.fixture(scope="module")
def validator() -> ModuleType:
    """Load the production artifact validator for the complete group of integration checks."""
    return _module(PLUGIN_ROOT / "shared" / "validate-artifacts.py")


@pytest.fixture
def integration_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> IntegrationRepository:
    """Create separate target and PR commits without credentials, network, hooks, or host Git settings."""
    for name in tuple(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Integration Fixture")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "fixture@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Integration Fixture")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "fixture@example.invalid")
    root = tmp_path / "source"
    root.mkdir()
    _git(root, "init", "--quiet", "--initial-branch=target")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "config", "core.hooksPath", str(tmp_path / "disabled-hooks"))
    _git(root, "commit", "--quiet", "--allow-empty", "-m", "Common source")
    common = _git(root, "rev-parse", "HEAD")
    (root / "target.txt").write_text("Fetched target behavior\n", encoding="utf-8", newline="\n")
    _git(root, "add", "--", "target.txt")
    _git(root, "commit", "--quiet", "-m", "Target update")
    target = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", "--quiet", "-b", "topic", common)
    (root / "topic.txt").write_text("PR behavior\n", encoding="utf-8", newline="\n")
    _git(root, "add", "--", "topic.txt")
    _git(root, "commit", "--quiet", "-m", "PR update")
    pre_head = _git(root, "rev-parse", "HEAD")
    pr_dir = tmp_path / "run" / "pr"
    pr_dir.mkdir(parents=True)
    (pr_dir / "local-checkout.json").write_text(
        json.dumps({"worktree": root.resolve().as_posix(), "local_branch": "topic"}),
        encoding="utf-8",
        newline="\n",
    )
    return IntegrationRepository(root, pr_dir, pre_head, target)


def _write_receipt(
    source: IntegrationRepository,
    *,
    schema: int = 2,
    status: str = "not-needed",
    pre_head: str | None = None,
    post_head: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Write clean-conflict evidence without using ancestry to decide whether integration is complete."""
    path = source.pr_dir / "merge-resolution.json"
    pre_head = pre_head or source.pre_head
    post_head = post_head or pre_head
    completed = status == "completed"
    authorization = "explicit-input" if completed else "not-required"
    path.write_text(
        json.dumps(
            {
                "schema_version": schema,
                "conflicts_detected": False,
                "status": status,
                "authorization": authorization,
                "base_remote_ref": source.target,
                "target_oid": source.target,
                "pre_merge_head": pre_head,
                "post_merge_head": post_head,
                "merge_commit": post_head if completed else None,
                "resolved_paths": [],
                "unmerged_paths": [],
                "evidence": ["The target and PR change separate files; no conflict resolution was needed."],
            }
        ),
        encoding="utf-8",
        newline="\n",
    )
    metadata = {
        "merge_resolution": {
            "artifact_path": str(path.resolve()),
            "authorization": authorization,
            "conflicts_detected": False,
            "status": status,
        }
    }
    return metadata, {"local_head": source.target}


@pytest.mark.integration
@pytest.mark.installed_plugin
class TestTargetIntegration:
    """Clean conflict evidence cannot substitute for the actual fetched target in remediation source."""

    def test_rejects_clean_not_needed_when_target_is_missing(
        self, validator: ModuleType, integration_repository: IntegrationRepository
    ) -> None:
        """A divergent conflict-free PR still requires target integration before finding edits."""
        source = integration_repository
        metadata, target = _write_receipt(source)

        with pytest.raises(SystemExit, match=r"^code-remediate-target-not-integrated$"):
            validator._validate_code_remediate_merge_resolution(
                metadata, source.pr_dir, target, source.pre_head, require_integrated=True
            )

    def test_accepts_completed_clean_merge_without_resolved_paths(
        self, validator: ModuleType, integration_repository: IntegrationRepository
    ) -> None:
        """A real no-ff merge can complete integration even when Git needed no conflict resolution."""
        source = integration_repository
        _git(source.root, "merge", "--quiet", "--no-ff", "-m", "Integrate fetched target", source.target)
        post_head = _git(source.root, "rev-parse", "HEAD")
        metadata, target = _write_receipt(source, status="completed", post_head=post_head)

        assert (
            validator._validate_code_remediate_merge_resolution(
                metadata, source.pr_dir, target, source.pre_head, require_integrated=True
            )
            is None
        )

    def test_accepts_not_needed_when_target_is_already_ancestor(
        self, validator: ModuleType, integration_repository: IntegrationRepository
    ) -> None:
        """A PR that already contains the fetched target requires no additional merge commit."""
        source = integration_repository
        _git(source.root, "merge", "--quiet", "--no-ff", "-m", "Prior integration", source.target)
        integrated_head = _git(source.root, "rev-parse", "HEAD")
        metadata, target = _write_receipt(source, pre_head=integrated_head)

        assert (
            validator._validate_code_remediate_merge_resolution(
                metadata, source.pr_dir, target, integrated_head, require_integrated=True
            )
            is None
        )

    def test_rejects_completed_receipt_without_fetched_target(
        self, validator: ModuleType, integration_repository: IntegrationRepository
    ) -> None:
        """A genuine merge with another branch does not prove the recorded fetched target was integrated."""
        source = integration_repository
        _git(source.root, "checkout", "--quiet", "-b", "other")
        _git(source.root, "commit", "--quiet", "--allow-empty", "-m", "Unrelated update")
        other = _git(source.root, "rev-parse", "HEAD")
        _git(source.root, "checkout", "--quiet", "topic")
        _git(source.root, "merge", "--quiet", "--no-ff", "-m", "Merge another branch", other)
        post_head = _git(source.root, "rev-parse", "HEAD")
        metadata, target = _write_receipt(source, status="completed", post_head=post_head)

        with pytest.raises(SystemExit, match=r"^code-remediate-target-not-integrated$"):
            validator._validate_code_remediate_merge_resolution(
                metadata, source.pr_dir, target, source.pre_head, require_integrated=True
            )

    def test_retains_historical_schema_one_readability(
        self, validator: ModuleType, integration_repository: IntegrationRepository
    ) -> None:
        """Historical schema-one no-conflict output remains readable without asserting current source readiness."""
        source = integration_repository
        metadata, target = _write_receipt(source, schema=1)

        assert (
            validator._validate_code_remediate_merge_resolution(metadata, source.pr_dir, target, source.pre_head)
            is None
        )

    def test_rejects_historical_not_needed_before_current_edits(
        self, validator: ModuleType, integration_repository: IntegrationRepository
    ) -> None:
        """A legacy no-conflict receipt cannot bypass the resumed run's current integration prerequisite."""
        source = integration_repository
        metadata, target = _write_receipt(source, schema=1)

        with pytest.raises(SystemExit, match=r"^code-remediate-target-not-integrated$"):
            validator._validate_code_remediate_merge_resolution(
                metadata, source.pr_dir, target, source.pre_head, require_integrated=True
            )

    def test_rejects_merged_receipt_when_checkout_is_still_premerge(
        self, validator: ModuleType, integration_repository: IntegrationRepository
    ) -> None:
        """The existence of a merged object does not certify the source actually checked out for finding edits."""
        source = integration_repository
        _git(source.root, "checkout", "--quiet", "-b", "integrated")
        _git(source.root, "merge", "--quiet", "--no-ff", "-m", "Integrate elsewhere", source.target)
        post_head = _git(source.root, "rev-parse", "HEAD")
        _git(source.root, "checkout", "--quiet", "topic")
        metadata, target = _write_receipt(source, status="completed", post_head=post_head)

        with pytest.raises(SystemExit, match=r"^code-remediate-target-integration-checkout-head-mismatch$"):
            validator._validate_code_remediate_merge_resolution(
                metadata, source.pr_dir, target, source.pre_head, require_integrated=True
            )

    def test_rejects_nonmerge_descendant_recorded_as_merge_commit(
        self, validator: ModuleType, integration_repository: IntegrationRepository
    ) -> None:
        """A later ordinary commit containing both histories is not the specifically recorded integration merge."""
        source = integration_repository
        _git(source.root, "merge", "--quiet", "--no-ff", "-m", "Actual integration", source.target)
        _git(source.root, "commit", "--quiet", "--allow-empty", "-m", "Later ordinary commit")
        post_head = _git(source.root, "rev-parse", "HEAD")
        metadata, target = _write_receipt(source, status="completed", post_head=post_head)

        with pytest.raises(SystemExit, match=r"^code-remediate-target-merge-parents-invalid$"):
            validator._validate_code_remediate_merge_resolution(
                metadata, source.pr_dir, target, source.pre_head, require_integrated=True
            )

    @pytest.mark.parametrize("stage", ["selection", "plan"])
    @pytest.mark.parametrize("schema", [1, 2])
    def test_preflight_rejects_behind_pr_before_selection_or_plan(
        self,
        integration_repository: IntegrationRepository,
        capsys: pytest.CaptureFixture[str],
        stage: str,
        schema: int,
    ) -> None:
        """Both public pre-edit stages enforce target ancestry even for a retained legacy clean receipt."""
        source = integration_repository
        run = source.pr_dir.parent
        _write_receipt(source, schema=schema)
        (source.pr_dir / "target-branch.json").write_text(
            json.dumps({"local_head": source.target}), encoding="utf-8", newline="\n"
        )
        (source.pr_dir / "pr-routing.json").write_text(
            json.dumps({"head_oid": source.pre_head}), encoding="utf-8", newline="\n"
        )
        inventory = _selection()
        inventory["pr_relevance"] = {
            "evaluated": True,
            "connected_open_items_total": 1,
            "connected_selectable_items_total": 1,
            "connected_required_followup_total": 0,
            "connected_items_marked_out_of_scope": 0,
        }
        (run / "selection.json").write_text(json.dumps(inventory), encoding="utf-8", newline="\n")
        (run / "action-items.md").write_text("## PR Relevance Summary\n- direct-diff\n", encoding="utf-8", newline="\n")

        assert HELPER.main(["preflight", "--run", str(run), "--stage", stage]) == 1

        assert json.loads(capsys.readouterr().out)["code"] == "code-remediate-target-not-integrated"

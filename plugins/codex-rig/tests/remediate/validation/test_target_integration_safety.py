"""Keep integration validation on the recorded source without executing repository-configured helpers."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from _modules import load_module

VALIDATOR = load_module(
    "integration_safety_validator", Path(__file__).resolve().parents[3] / "shared/validate-artifacts.py"
)


@pytest.fixture
def collected_source(tmp_path):
    """Record the same source in the collector checkout and the gate runner."""
    pr = tmp_path / "pr"
    pr.mkdir()
    source = (tmp_path / "source").as_posix()
    (pr / "local-checkout.json").write_text(json.dumps({"worktree": source}), encoding="utf-8")
    (tmp_path / "gates.json").write_text(json.dumps({"source": {"worktree": source}}), encoding="utf-8")
    return pr, source


def test_integration_git_ignores_fsmonitor_and_pager(collected_source, monkeypatch):
    """Every Git probe disables fsmonitor and paging, and the parent lookup disables text conversion."""
    pr, source = collected_source
    pre, target, post = "a" * 40, "b" * 40, "c" * 40
    execute = Mock(return_value=SimpleNamespace(returncode=0, stdout=f"{pre} {target}"))
    monkeypatch.setattr(VALIDATOR.subprocess, "run", execute)
    VALIDATOR._validate_code_remediate_target_integration(
        pr,
        {"target_oid": target, "pre_merge_head": pre, "post_merge_head": post, "status": "completed"},
        current_checkout=False,
    )
    assert execute.call_count == 3
    assert all(
        call.args[0][:6] == ["git", "--no-pager", "-c", "core.fsmonitor=false", "-C", source]
        for call in execute.call_args_list
    )
    assert execute.call_args.args[0][6:] == ["show", "--no-textconv", "-s", "--format=%P", post]


def test_integration_rejects_another_repository(collected_source, monkeypatch):
    """Reject a forged checkout coordinate before any Git command can enter that directory."""
    pr, source = collected_source
    (pr / "local-checkout.json").write_text(json.dumps({"worktree": source + "-hostile"}), encoding="utf-8")
    execute = Mock()
    monkeypatch.setattr(VALIDATOR.subprocess, "run", execute)
    with pytest.raises(SystemExit, match="target-integration-source-mismatch"):
        VALIDATOR._validate_code_remediate_target_integration(
            pr,
            {"target_oid": "a" * 40, "pre_merge_head": "a" * 40, "post_merge_head": "a" * 40, "status": "not-needed"},
            current_checkout=False,
        )
    execute.assert_not_called()

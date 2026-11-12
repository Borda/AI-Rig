"""Reject trailing line breaks in identities passed to URLs and Git commands."""

from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import evolution_render
import merge_specialist_batch
import parse_analyse_args
import pytest
import release_append_marker


def test_repo_url_rejects_trailing_line_break() -> None:
    """A repository owner must be exactly one safe path component."""
    assert evolution_render.repo_prefix({"owner": "owner\n", "repo": "project"}) is None
    assert evolution_render.repo_prefix({"owner": "owner", "repo": "project"}) == "https://github.com/owner/project/"


@pytest.mark.parametrize("operation", ["commit", "tag"])
def test_marker_commit_rejects_trailing_line_break(monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    """An invalid marker never reaches Git even when Git would otherwise answer positively."""
    run = mock.Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(release_append_marker, "which", lambda _name: "git")
    monkeypatch.setattr(release_append_marker.subprocess, "run", run)
    result = (
        release_append_marker._is_valid_commit("a" * 40 + "\n")
        if operation == "commit"
        else release_append_marker._tag_advanced_past("a" * 40 + "\n", "v1.0.0")
    )
    assert result is False
    run.assert_not_called()


def test_explicit_repo_rejects_trailing_line_break() -> None:
    """A bare repository argument must match the entire slug."""
    with pytest.raises(parse_analyse_args._Abort):
        parse_analyse_args.repo_from_argument("owner/project\n")


def test_merge_base_rejects_trailing_line_break(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An invalid resumed base is refused even when the merge plan has no work."""
    monkeypatch.chdir(tmp_path)
    plan = tmp_path / "plan.json"
    plan.write_text("[]", encoding="utf-8")
    assert (
        merge_specialist_batch.main(["--plan", str(plan), "--commit-mode", "each", "--base-sha", "a" * 40 + "\n"]) == 2
    )

"""Real Git regressions for refreshing the recorded hook approval before a merge commit.

A merge can change hook inputs after the approval was recorded. The refresh keeps the merge commit possible without ever
running hook bytes nobody approved: a helper-made approval is decided again, any other stale one is set aside.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest

_HOOK = "#!/bin/sh\necho ran > ran-hook\nexit 0\n"


@pytest.fixture
def hooks() -> Any:
    """Return the helper module under test."""
    return importlib.import_module("resolve_commit_hooks")


@pytest.fixture
def merge_repo(scratch_repo: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Provide a repository with a ``base`` branch, a shared ``package.json`` and a hook that leaves a marker."""
    (scratch_repo.path / "package.json").write_text('{"version": "1"}\n', encoding="utf-8", newline="\n")
    scratch_repo.git("add", "package.json")
    scratch_repo.git("commit", "-q", "-m", "package")
    scratch_repo.git("branch", "base")
    hook = scratch_repo.path / ".git" / "hooks" / "pre-commit"
    hook.write_text(_HOOK, encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    monkeypatch.delenv("RESOLVE_APPROVED_HOOKS", raising=False)
    return scratch_repo


def _commit_file(repo: Any, name: str, text: str) -> None:
    """Commit one file on the current branch with hooks off, so setup never leaves the marker."""
    (repo.path / name).write_text(text, encoding="utf-8", newline="\n")
    repo.git("add", name)
    no_hooks = repo.path.parent / "no-hooks"
    no_hooks.mkdir(exist_ok=True)
    repo.git("-c", f"core.hooksPath={no_hooks.as_posix()}", "commit", "-q", "-m", f"add {name}")


def _record_then_stage_merge(repo: Any, hooks: Any, tmp_path: Path, *, base_file: str, decided: str) -> Path:
    """Record an approval for the PR checkout, then stage a base merge that moves ``base_file``.

    ``decided`` is the decision word recorded beside the approval: ``run`` for the helper's own, ``ask`` for an approval
    the user gave after the helper asked. Return the implementation directory holding both files.
    """
    repo.git("checkout", "-q", "-b", "pr")
    _commit_file(repo, "feature.txt", "pr work\n")
    repo.git("checkout", "-q", "base")
    _commit_file(repo, base_file, '{"version": "2"}\n')
    repo.git("checkout", "-q", "pr")
    impl = tmp_path / "impl"
    impl.mkdir()
    (impl / "approved-hooks.txt").write_text(str(hooks.inspect_repository_hooks("git")["sha256"]), encoding="utf-8")
    (impl / "hooks-decision.txt").write_text(decided + "\n", encoding="utf-8")
    repo.git("merge", "--no-commit", "--no-ff", "base")
    return impl


def _refresh(hooks: Any, impl: Path, *, base: str | None = "base", head: str | None = "pr") -> Any:
    """Run the refresh on the implementation directory's two files and return its outcome."""
    return hooks.refresh_approval("git", impl / "approved-hooks.txt", impl / "hooks-decision.txt", base, head)


def _commit_merge(hooks: Any, impl: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    """Commit the pending merge with whatever the approval file now holds, as the resolve block does."""
    monkeypatch.setenv("RESOLVE_APPROVED_HOOKS", (impl / "approved-hooks.txt").read_text(encoding="utf-8").strip())
    return hooks.main(["--commit-merge"])


def _merge_pending(repo: Any) -> bool:
    """Tell whether the repository still has a merge in progress."""
    return (repo.path / ".git" / "MERGE_HEAD").exists()


@pytest.mark.integration
def test_a_stale_approval_refuses_the_merge_commit_without_a_refresh(
    merge_repo: Any, hooks: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A base-side ``package.json`` change makes the recorded digest stale, so the merge commit alone is refused."""
    impl = _record_then_stage_merge(merge_repo, hooks, tmp_path, base_file="package.json", decided="run")
    assert _commit_merge(hooks, impl, monkeypatch) == 1
    assert _merge_pending(merge_repo)


@pytest.mark.integration
def test_a_helper_made_approval_is_decided_again_on_the_merged_tree(
    merge_repo: Any, hooks: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The helper's own ``run`` follows a base-side hook-input change, and the merge commits with the hooks."""
    impl = _record_then_stage_merge(merge_repo, hooks, tmp_path, base_file="package.json", decided="run")
    before = (impl / "approved-hooks.txt").read_text(encoding="utf-8")
    outcome, _reason = _refresh(hooks, impl)
    assert outcome is hooks.RefreshOutcome.REFRESHED
    after = (impl / "approved-hooks.txt").read_text(encoding="utf-8")
    assert after != before
    assert after == hooks.inspect_repository_hooks("git")["sha256"]
    assert (impl / "hooks-decision.txt").read_text(encoding="utf-8") == "run\n"
    assert _commit_merge(hooks, impl, monkeypatch) == 0
    assert not _merge_pending(merge_repo)
    assert (merge_repo.path / "ran-hook").exists()


@pytest.mark.integration
def test_a_stale_user_approval_is_set_aside_and_the_merge_commits_without_hooks(
    merge_repo: Any, hooks: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user's answer is never re-decided: once its hooks change, hooks stay disabled and the merge still commits."""
    impl = _record_then_stage_merge(merge_repo, hooks, tmp_path, base_file="package.json", decided="ask")
    outcome, reason = _refresh(hooks, impl)
    assert outcome is hooks.RefreshOutcome.STALE
    assert "changed since they were approved" in reason
    assert (impl / "approved-hooks.txt").read_text(encoding="utf-8") == ""
    assert (impl / "hooks-decision.txt").read_text(encoding="utf-8") == "stale\n"
    assert _commit_merge(hooks, impl, monkeypatch) == 0
    assert not _merge_pending(merge_repo)
    assert not (merge_repo.path / "ran-hook").exists()


@pytest.mark.integration
def test_a_helper_made_approval_is_set_aside_when_the_merge_stages_a_new_hook_input(
    merge_repo: Any, hooks: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hook input staged during the merge that matches neither side asks, so the approval is set aside."""
    impl = _record_then_stage_merge(merge_repo, hooks, tmp_path, base_file="other.txt", decided="run")
    (merge_repo.path / "package.json").write_text('{"version": "blend"}\n', encoding="utf-8", newline="\n")
    merge_repo.git("add", "package.json")
    outcome, reason = _refresh(hooks, impl)
    assert outcome is hooks.RefreshOutcome.STALE
    assert "package.json" in reason
    assert (impl / "approved-hooks.txt").read_text(encoding="utf-8") == ""
    assert _commit_merge(hooks, impl, monkeypatch) == 0
    assert not (merge_repo.path / "ran-hook").exists()


@pytest.mark.integration
@pytest.mark.parametrize("decided", ["run", "ask"])
def test_an_approval_that_still_matches_is_kept(
    merge_repo: Any, hooks: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, decided: str
) -> None:
    """A merge that leaves every hook input alone keeps the approval, the user's included."""
    impl = _record_then_stage_merge(merge_repo, hooks, tmp_path, base_file="other.txt", decided=decided)
    before = (impl / "approved-hooks.txt").read_text(encoding="utf-8")
    outcome, _reason = _refresh(hooks, impl)
    assert outcome is hooks.RefreshOutcome.CURRENT
    assert (impl / "approved-hooks.txt").read_text(encoding="utf-8") == before
    assert (impl / "hooks-decision.txt").read_text(encoding="utf-8") == decided + "\n"
    assert _commit_merge(hooks, impl, monkeypatch) == 0
    assert (merge_repo.path / "ran-hook").exists()


@pytest.mark.integration
def test_a_pending_question_and_disabled_hooks_are_left_alone(merge_repo: Any, hooks: Any, tmp_path: Path) -> None:
    """No approval file stays absent and an empty one stays empty: neither becomes an approval."""
    impl = tmp_path / "impl"
    impl.mkdir()
    assert _refresh(hooks, impl)[0] is hooks.RefreshOutcome.PENDING
    assert not (impl / "approved-hooks.txt").exists()
    assert not (impl / "hooks-decision.txt").exists()
    (impl / "approved-hooks.txt").write_text("", encoding="utf-8")
    assert _refresh(hooks, impl)[0] is hooks.RefreshOutcome.DISABLED
    assert (impl / "approved-hooks.txt").read_text(encoding="utf-8") == ""
    assert not (impl / "hooks-decision.txt").exists()


@pytest.mark.integration
@pytest.mark.parametrize(
    ("decided", "base"),
    [
        pytest.param(None, "base", id="no-decision-file"),
        pytest.param("run", None, id="helper-made-without-a-base"),
        pytest.param("disabled", "base", id="decision-is-not-run"),
    ],
)
def test_a_stale_approval_nobody_can_refresh_is_set_aside(
    merge_repo: Any, hooks: Any, tmp_path: Path, decided: str | None, base: str | None
) -> None:
    """Without proof the helper made it, or without a base to decide again, a stale approval is set aside."""
    impl = _record_then_stage_merge(merge_repo, hooks, tmp_path, base_file="package.json", decided=decided or "run")
    if decided is None:
        (impl / "hooks-decision.txt").unlink()
    assert _refresh(hooks, impl, base=base)[0] is hooks.RefreshOutcome.STALE
    assert (impl / "approved-hooks.txt").read_text(encoding="utf-8") == ""
    assert (impl / "hooks-decision.txt").read_text(encoding="utf-8") == "stale\n"


@pytest.mark.integration
def test_a_failed_inspection_sets_the_approval_aside(
    merge_repo: Any, hooks: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the hooks cannot be inspected, no digest is trusted: the approval is set aside."""
    impl = _record_then_stage_merge(merge_repo, hooks, tmp_path, base_file="other.txt", decided="run")

    def broken(git: str) -> dict[str, object]:
        """Fail like an unreadable hook configuration."""
        raise RuntimeError("cannot inspect core.hooksPath configuration")

    monkeypatch.setattr(hooks, "inspect_repository_hooks", broken)
    outcome, reason = _refresh(hooks, impl)
    assert outcome is hooks.RefreshOutcome.STALE
    assert "hook inspection failed" in reason
    assert (impl / "approved-hooks.txt").read_text(encoding="utf-8") == ""


@pytest.mark.integration
def test_the_cli_prints_one_outcome_line(
    merge_repo: Any, hooks: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The command-line form prints a single ``hooks-refresh=`` line and exits successfully."""
    impl = _record_then_stage_merge(merge_repo, hooks, tmp_path, base_file="package.json", decided="run")
    argv = ["--refresh-approval", "--base", "base", "--head", "pr"]
    files = ["--approved-file", str(impl / "approved-hooks.txt"), "--decision-file", str(impl / "hooks-decision.txt")]
    assert hooks.main([*argv, *files]) == 0
    out = capsys.readouterr().out
    assert out.startswith("hooks-refresh=refreshed — run: ")
    assert out.count("\n") == 1


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param(["--approved-file", "a"], id="without-decision-file"),
        pytest.param(["--decision-file", "d"], id="without-approved-file"),
    ],
)
def test_the_refresh_needs_both_files(hooks: Any, capsys: pytest.CaptureFixture[str], extra: list[str]) -> None:
    """A refresh without both files is a usage error, never a silent no-op."""
    with pytest.raises(SystemExit) as exc:
        hooks.main(["--refresh-approval", *extra])
    assert exc.value.code == 2
    assert "--refresh-approval requires" in capsys.readouterr().err

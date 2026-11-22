"""Real Git regressions for the repository-hook decision: follow Git, ask only when the PR changes hook inputs."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def decide_repo(scratch_repo: Any) -> Any:
    """Provide a repository with a ``base`` branch ref at the initial commit and a locally installed hook."""
    scratch_repo.git("branch", "base")
    hook = scratch_repo.path / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    return scratch_repo


def _decide(capsys: pytest.CaptureFixture[str], *extra: str, base: str = "base") -> dict[str, Any]:
    """Run ``--decide`` through the public entrypoint and return its JSON report."""
    hooks = importlib.import_module("resolve_commit_hooks")
    assert hooks.main(["--decide", "--base", base, *extra]) == 0
    return json.loads(capsys.readouterr().out)


def _commit_file(repo: Any, name: str, text: str) -> None:
    """Commit one file on the current branch, creating parent directories."""
    path = repo.path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    repo.git("add", name)
    repo.git("commit", "-q", "-m", f"add {name}")


@pytest.mark.integration
def test_no_installed_hooks_runs_without_a_question(
    scratch_repo: Any, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A repository holding only Git's inactive ``.sample`` hooks decides ``none`` and records an empty approval."""
    scratch_repo.git("branch", "base")
    approved = tmp_path / "approved-hooks.txt"
    report = _decide(capsys, "--approved-file", str(approved))
    assert report["decision"] == "none"
    assert approved.read_text(encoding="utf-8") == ""


@pytest.mark.integration
def test_installed_hook_with_unchanged_inputs_runs_without_a_question(
    decide_repo: Any, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A local hook the PR cannot influence runs, and the inspected digest is recorded as the approval."""
    _commit_file(decide_repo, "feature.txt", "pr work\n")
    approved = tmp_path / "approved-hooks.txt"
    report = _decide(capsys, "--approved-file", str(approved))
    assert report["decision"] == "run"
    assert report["changed"] == []
    assert approved.read_text(encoding="utf-8") == report["sha256"]


@pytest.mark.integration
@pytest.mark.parametrize(
    "changed",
    [
        pytest.param(".githooks/pre-commit", id="hook-script"),
        pytest.param(".pre-commit-config.yaml", id="pre-commit-config"),
        pytest.param("package.json", id="package-json"),
        pytest.param(".husky/pre-commit", id="husky-hook"),
    ],
)
def test_pr_changing_hook_inputs_asks_and_records_nothing(
    decide_repo: Any, capsys: pytest.CaptureFixture[str], tmp_path: Path, changed: str
) -> None:
    """A PR that adds or edits any hook input makes the decision ``ask`` and leaves the approval file unwritten."""
    _commit_file(decide_repo, changed, "payload\n")
    approved = tmp_path / "approved-hooks.txt"
    report = _decide(capsys, "--approved-file", str(approved))
    assert report["decision"] == "ask"
    assert changed in report["changed"]
    assert not approved.exists()


@pytest.mark.integration
def test_uncommitted_edit_to_a_hook_input_asks(decide_repo: Any, capsys: pytest.CaptureFixture[str]) -> None:
    """A dirty or untracked hook input counts as a PR change even before it is committed."""
    (decide_repo.path / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")
    report = _decide(capsys)
    assert report["decision"] == "ask"
    assert ".pre-commit-config.yaml" in report["changed"]


@pytest.mark.integration
def test_unknown_base_fails_closed_to_asking(decide_repo: Any, capsys: pytest.CaptureFixture[str]) -> None:
    """When the base cannot be compared, installed hooks are never trusted silently."""
    report = _decide(capsys, base="origin/does-not-exist")
    assert report["decision"] == "ask"
    assert "base" in report["reason"]


@pytest.mark.integration
def test_remembered_approval_covers_the_exact_hook_bytes_only(
    decide_repo: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """A saved approval skips the question for the same hook digest and asks again once the hook changes."""
    _commit_file(decide_repo, ".githooks/pre-commit", "#!/bin/sh\nexit 0\n")
    shown = _decide(capsys)
    assert shown["decision"] == "ask"
    hooks = importlib.import_module("resolve_commit_hooks")
    assert hooks.main(["--remember", "approved", "--sha", shown["sha256"]]) == 0
    capsys.readouterr()
    assert _decide(capsys)["decision"] == "run"
    (decide_repo.path / ".githooks/pre-commit").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8", newline="\n")
    assert _decide(capsys)["decision"] == "ask"


@pytest.mark.integration
def test_remembered_approval_binds_the_digest_the_user_was_shown(
    decide_repo: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """Hooks that change between the question and the answer are not covered by the saved approval."""
    _commit_file(decide_repo, ".githooks/pre-commit", "#!/bin/sh\nexit 0\n")
    shown = _decide(capsys)
    (decide_repo.path / ".githooks/pre-commit").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8", newline="\n")
    hooks = importlib.import_module("resolve_commit_hooks")
    assert hooks.main(["--remember", "approved", "--sha", shown["sha256"]]) == 0
    capsys.readouterr()
    assert _decide(capsys)["decision"] == "ask"


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param([], id="no-digest"),
        pytest.param(["--sha", "abc"], id="short-digest"),
        pytest.param(["--sha", "A" * 64], id="uppercase-digest"),
        pytest.param(["--sha", "g" * 64], id="non-hex-digest"),
    ],
)
def test_remembering_an_approval_needs_the_shown_digest(extra: list[str]) -> None:
    """``--remember approved`` without a full lowercase digest is refused instead of inspecting later."""
    hooks = importlib.import_module("resolve_commit_hooks")
    with pytest.raises(SystemExit) as refused:
        hooks.main(["--remember", "approved", *extra])
    assert refused.value.code == 2


@pytest.mark.integration
def test_never_policy_disables_hooks_until_forgotten(
    decide_repo: Any, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A saved never-run policy keeps hooks disabled with no question, and forgetting it restores the default."""
    hooks = importlib.import_module("resolve_commit_hooks")
    assert hooks.main(["--remember", "never"]) == 0
    capsys.readouterr()
    approved = tmp_path / "approved-hooks.txt"
    assert _decide(capsys, "--approved-file", str(approved))["decision"] == "disabled"
    assert approved.read_text(encoding="utf-8") == ""
    assert hooks.main(["--forget"]) == 0
    capsys.readouterr()
    assert _decide(capsys)["decision"] == "run"


@pytest.mark.integration
def test_corrupt_policy_file_is_ignored(decide_repo: Any, capsys: pytest.CaptureFixture[str]) -> None:
    """An unreadable saved policy never grants approval and never blocks the decision."""
    (decide_repo.path / ".git" / "resolve-hooks-policy.json").write_text("{not json", encoding="utf-8")
    assert _decide(capsys)["decision"] == "run"
    _commit_file(decide_repo, ".githooks/pre-commit", "#!/bin/sh\nexit 0\n")
    assert _decide(capsys)["decision"] == "ask"


def _stage_base_merge(repo: Any, *, base_file: str, pr_file: str) -> str:
    """Commit ``pr_file`` on a PR branch, move ``base`` by ``base_file``, stage the merge; return the PR head."""
    repo.git("checkout", "-q", "-b", "pr")
    _commit_file(repo, pr_file, "pr work\n")
    pr_head = repo.git("rev-parse", "HEAD").strip()
    repo.git("checkout", "-q", "base")
    _commit_file(repo, base_file, "base work\n")
    repo.git("checkout", "-q", "pr")
    repo.git("merge", "--no-commit", "--no-ff", "base")
    return pr_head


@pytest.mark.integration
def test_head_ignores_base_side_hook_input_changes_staged_by_a_merge(
    decide_repo: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """A base-only edit to a hook input, already staged by the pending merge, is not something the PR supplies."""
    pr_head = _stage_base_merge(decide_repo, base_file="package.json", pr_file="feature.txt")
    report = _decide(capsys, "--head", pr_head)
    assert report["decision"] == "run"
    assert report["changed"] == []


@pytest.mark.integration
def test_head_still_asks_when_the_pr_commits_a_hook_input(decide_repo: Any, capsys: pytest.CaptureFixture[str]) -> None:
    """A hook input the PR head itself changes asks, even with a base merge staged beside it."""
    pr_head = _stage_base_merge(decide_repo, base_file="other.txt", pr_file="package.json")
    report = _decide(capsys, "--head", pr_head)
    assert report["decision"] == "ask"
    assert report["changed"] == ["package.json"]


@pytest.mark.integration
def test_head_still_flags_an_untracked_hook_input(decide_repo: Any, capsys: pytest.CaptureFixture[str]) -> None:
    """A local untracked hook input is counted with ``--head`` exactly as it is without it."""
    pr_head = decide_repo.git("rev-parse", "HEAD").strip()
    (decide_repo.path / ".husky").mkdir()
    (decide_repo.path / ".husky" / "pre-commit").write_text("exit 0\n", encoding="utf-8", newline="\n")
    report = _decide(capsys, "--head", pr_head)
    assert report["decision"] == "ask"
    assert ".husky/pre-commit" in report["changed"]


@pytest.mark.integration
def test_head_still_flags_an_unstaged_edit_to_a_staged_hook_input(
    decide_repo: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """A worktree edit on top of what the staged merge brought in is a change; the staged base content alone is not."""
    pr_head = _stage_base_merge(decide_repo, base_file="package.json", pr_file="feature.txt")
    assert _decide(capsys, "--head", pr_head)["decision"] == "run"
    (decide_repo.path / "package.json").write_text("edited after the merge\n", encoding="utf-8", newline="\n")
    report = _decide(capsys, "--head", pr_head)
    assert report["decision"] == "ask"
    assert report["changed"] == ["package.json"]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("base_text", "staged_text"),
    [
        pytest.param("base work\n", "blend of both sides\n", id="blend-of-a-base-edit"),
        pytest.param(None, "staged on top\n", id="path-neither-side-has"),
    ],
)
def test_head_flags_a_staged_hook_input_matching_neither_side(
    decide_repo: Any, capsys: pytest.CaptureFixture[str], base_text: str | None, staged_text: str
) -> None:
    """A staged hook input that equals neither the checked-out commit nor the merged base was written during the merge.

    Conflict resolution stages its result, so such content is neither the PR's commits nor the base and must ask.
    """
    pr_head = _stage_base_merge(
        decide_repo, base_file="package.json" if base_text else "other.txt", pr_file="feature.txt"
    )
    assert _decide(capsys, "--head", pr_head)["decision"] == "run"
    (decide_repo.path / "package.json").write_text(staged_text, encoding="utf-8", newline="\n")
    decide_repo.git("add", "package.json")
    report = _decide(capsys, "--head", pr_head)
    assert report["decision"] == "ask"
    assert report["changed"] == ["package.json"]


@pytest.mark.integration
def test_head_flags_a_hook_input_committed_after_the_recorded_head(
    decide_repo: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """A PR commit pulled after the head was recorded is still the PR's change, so its hook-input edit asks."""
    _commit_file(decide_repo, "package.json", "shared\n")
    decide_repo.git("branch", "-f", "base")
    decide_repo.git("checkout", "-q", "-b", "pr")
    _commit_file(decide_repo, "feature.txt", "pr work\n")
    recorded_head = decide_repo.git("rev-parse", "HEAD").strip()
    _commit_file(decide_repo, "package.json", "pulled later\n")
    decide_repo.git("checkout", "-q", "base")
    _commit_file(decide_repo, "other.txt", "base work\n")
    decide_repo.git("checkout", "-q", "pr")
    decide_repo.git("merge", "--no-commit", "--no-ff", "base")
    report = _decide(capsys, "--head", recorded_head)
    assert report["decision"] == "ask"
    assert report["changed"] == ["package.json"]


@pytest.mark.integration
def test_head_flags_a_staged_deletion_of_a_hook_input(decide_repo: Any, capsys: pytest.CaptureFixture[str]) -> None:
    """Removing a hook input both sides keep, staged during the merge, is a change the merge did not bring in."""
    _commit_file(decide_repo, "package.json", "shared\n")
    decide_repo.git("branch", "-f", "base")
    pr_head = _stage_base_merge(decide_repo, base_file="other.txt", pr_file="feature.txt")
    decide_repo.git("rm", "-q", "package.json")
    report = _decide(capsys, "--head", pr_head)
    assert report["decision"] == "ask"
    assert report["changed"] == ["package.json"]


@pytest.mark.integration
def test_head_flags_a_staged_mode_change_of_a_hook_input(decide_repo: Any, capsys: pytest.CaptureFixture[str]) -> None:
    """A staged executable-bit change on a hook script matches neither side's entry, so it asks."""
    _commit_file(decide_repo, ".githooks/pre-commit", "#!/bin/sh\nexit 0\n")
    decide_repo.git("branch", "-f", "base")
    pr_head = _stage_base_merge(decide_repo, base_file="other.txt", pr_file="feature.txt")
    decide_repo.git("update-index", "--chmod=+x", ".githooks/pre-commit")
    report = _decide(capsys, "--head", pr_head)
    assert report["decision"] == "ask"
    assert report["changed"] == [".githooks/pre-commit"]


@pytest.mark.integration
def test_head_keeps_a_staged_hook_input_equal_to_the_checked_out_commit(
    decide_repo: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """Resolving a conflict to the checked-out side stages content the commit already has, which is not a new edit."""
    _commit_file(decide_repo, "package.json", "shared\n")
    decide_repo.git("branch", "-f", "base")
    pr_head = _stage_base_merge(decide_repo, base_file="package.json", pr_file="feature.txt")
    decide_repo.git("checkout", "-q", "HEAD", "--", "package.json")
    report = _decide(capsys, "--head", pr_head)
    assert report["decision"] == "run"
    assert report["changed"] == []


@pytest.mark.integration
def test_head_decision_equals_the_pre_merge_decision(decide_repo: Any, capsys: pytest.CaptureFixture[str]) -> None:
    """With the same hook-input edit on both sides, ``--head`` after the merge stages gives the pre-merge answer."""
    decide_repo.git("checkout", "-q", "-b", "pr")
    _commit_file(decide_repo, "feature.txt", "pr work\n")  # keeps the PR commit distinct from the base commit below
    _commit_file(decide_repo, "package.json", "same\n")
    pr_head = decide_repo.git("rev-parse", "HEAD").strip()
    before = _decide(capsys)
    decide_repo.git("checkout", "-q", "base")
    _commit_file(decide_repo, "package.json", "same\n")
    decide_repo.git("checkout", "-q", "pr")
    decide_repo.git("merge", "--no-commit", "--no-ff", "base")
    after = _decide(capsys, "--head", pr_head)
    assert (after["decision"], after["changed"]) == (before["decision"], before["changed"])


@pytest.mark.integration
def test_head_is_ignored_without_a_pending_merge(decide_repo: Any, capsys: pytest.CaptureFixture[str]) -> None:
    """With no merge pending, a staged hook-input edit counts exactly as it does without ``--head``."""
    pr_head = decide_repo.git("rev-parse", "HEAD").strip()
    (decide_repo.path / "package.json").write_text("staged only\n", encoding="utf-8", newline="\n")
    decide_repo.git("add", "package.json")
    without = _decide(capsys)
    with_head = _decide(capsys, "--head", pr_head)
    assert without["decision"] == "ask"
    assert (with_head["decision"], with_head["changed"]) == (without["decision"], without["changed"])


@pytest.mark.integration
def test_option_like_head_fails_closed_to_asking(decide_repo: Any, capsys: pytest.CaptureFixture[str]) -> None:
    """A head value that looks like an option is never passed to Git; the decision asks."""
    report = _decide(capsys, "--head=--output=x")
    assert report["decision"] == "ask"
    assert "not a reference" in report["reason"]


@pytest.mark.integration
def test_decide_requires_a_base(capsys: pytest.CaptureFixture[str]) -> None:
    """``--decide`` without ``--base`` is a usage error, not a silent default."""
    hooks = importlib.import_module("resolve_commit_hooks")
    with pytest.raises(SystemExit) as exc:
        hooks.main(["--decide"])
    assert exc.value.code == 2
    assert "--base" in capsys.readouterr().err

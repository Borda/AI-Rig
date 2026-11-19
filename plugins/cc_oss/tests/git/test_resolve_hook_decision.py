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
    assert _decide(capsys)["decision"] == "ask"
    hooks = importlib.import_module("resolve_commit_hooks")
    assert hooks.main(["--remember", "approved"]) == 0
    capsys.readouterr()
    assert _decide(capsys)["decision"] == "run"
    (decide_repo.path / ".githooks/pre-commit").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8", newline="\n")
    assert _decide(capsys)["decision"] == "ask"


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


@pytest.mark.integration
def test_decide_requires_a_base(capsys: pytest.CaptureFixture[str]) -> None:
    """``--decide`` without ``--base`` is a usage error, not a silent default."""
    hooks = importlib.import_module("resolve_commit_hooks")
    with pytest.raises(SystemExit) as exc:
        hooks.main(["--decide"])
    assert exc.value.code == 2
    assert "--base" in capsys.readouterr().err

"""Keep local review provenance bound before source inspection and separate from readiness."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parents[1]
_SKILL = _PLUGIN / "skills/review/SKILL.md"
_BASH = shutil.which("bash")


@pytest.mark.skipif(_BASH is None, reason="bash unavailable")
@pytest.mark.parametrize("git_context", ["repository", "non-git", "unborn", "external-target"])
def test_identity_is_captured_before_inspection(tmp_path: Path, git_context: str) -> None:
    """The shipped capture recipe records the actual full base or an explicit unavailable reason."""
    skill = _SKILL.read_text(encoding="utf-8")
    scope = skill.split("## Step 1: Identify scope", 1)[1].split("### Scope pre-check", 1)[0]
    blocks = re.findall(r"```bash\n(.*?)```", scope, re.DOTALL)
    capture = next(block for block in blocks if "dev-review-commit-sha-" in block)
    assert scope.index("dev-review-commit-sha-") < scope.index("resolve_review_target.py")
    cwd = _PLUGIN if git_context in {"repository", "external-target"} else tmp_path
    if git_context == "external-target":
        target = tmp_path / "module.py"
        target.write_text("value = 1\n", encoding="utf-8")
        # Match the shipped Bash writer: serialized paths use forward slashes and LF on every host.
        (tmp_path / "dev-review-clean-args-identity-test").write_bytes((target.as_posix() + "\n").encode("utf-8"))
    if git_context == "unborn":
        subprocess.run(["git", "init", str(cwd)], check=True, capture_output=True)
    result = subprocess.run(
        [_BASH, "-c", capture],
        cwd=cwd,
        env={**os.environ, "TMPDIR": str(tmp_path), "CLAUDE_CODE_SESSION_ID": "identity-test"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    value = (tmp_path / "dev-review-commit-sha-identity-test").read_text().strip()
    if git_context == "repository":
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=cwd, text=True).strip()
        assert value == head
        assert "working-tree base" in result.stdout
    else:
        assert value == "unavailable (non-Git directory or unborn HEAD)"


def test_consolidator_retains_bound_sha_and_supported_source_judgment() -> None:
    """Presentation must retain source evidence even when execution remains blocked."""
    skill = _SKILL.read_text(encoding="utf-8")
    prompt = (_PLUGIN / "skills/review/templates/consolidator-prompt.md").read_text(encoding="utf-8")
    report = (_PLUGIN / "skills/review/templates/review-report.md").read_text(encoding="utf-8")
    assert "REVIEWED_COMMIT_SHA=<" in skill
    assert "REVIEWED_COMMIT_KIND=<" in skill
    assert "Commit SHA:" in report
    assert "`Commit SHA:`" in prompt
    assert "Preserve a supported source judgment" in prompt
    assert "independence" in prompt
    assert "runs no tests" in report


@pytest.mark.integration
@pytest.mark.skipif(_BASH is None, reason="bash unavailable")
@pytest.mark.parametrize("target_location", ["existing-worktree", "outside-worktree"])
def test_worktree_flag_does_not_certify_committed_target(tmp_path: Path, target_location: str) -> None:
    """Requested isolation cannot turn dirty explicit targets into committed-source evidence."""
    repository = tmp_path / "repository"
    repository.mkdir()
    commands = [
        ["git", "init", "-q"],
        ["git", "add", "module.py"],
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture\n\nCo-authored-by: Codex <codex@openai.com>",
        ],
    ]
    (repository / "module.py").write_text("value = 1\n", encoding="utf-8")
    for command in commands:
        subprocess.run(command, cwd=repository, check=True, capture_output=True)
    worktree = tmp_path / "worktree"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(worktree)], cwd=repository, check=True, capture_output=True
    )
    target = (worktree if target_location == "existing-worktree" else repository) / "module.py"
    target.write_text("value = 2\n", encoding="utf-8")
    (tmp_path / "dev-review-clean-args-worktree-identity").write_bytes((target.as_posix() + "\n").encode("utf-8"))
    (tmp_path / "dev-review-worktree-worktree-identity").write_bytes(b"true\n")
    scope = _SKILL.read_text(encoding="utf-8").split("## Step 1: Identify scope", 1)[1]
    capture = next(
        block for block in re.findall(r"```bash\n(.*?)```", scope, re.DOTALL) if "dev-review-commit-sha-" in block
    )
    result = subprocess.run(
        [_BASH, "-c", capture],
        cwd=worktree,
        env={**os.environ, "TMPDIR": str(tmp_path), "CLAUDE_CODE_SESSION_ID": "worktree-identity"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "working-tree base; reviewed files may include uncommitted changes" in result.stdout
    assert "committed worktree source" not in result.stdout
    assert target.read_text(encoding="utf-8") == "value = 2\n"

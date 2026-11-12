"""Protect implementation instructions for failed probes, read-only plans, and completion commits."""

from pathlib import Path

SKILL = Path(__file__).resolve().parents[2] / "skills/implement/SKILL.md"


def _section(heading: str) -> list[str]:
    """Read the named heading through the next heading at the same or a higher level."""
    lines = SKILL.read_text(encoding="utf-8").splitlines()
    start = lines.index(heading) + 1
    level = len(heading.split(" ", 1)[0])
    end = next(
        (
            index
            for index in range(start, len(lines))
            if lines[index].startswith("#") and len(lines[index].split(" ", 1)[0]) <= level
        ),
        len(lines),
    )
    return lines[start:end]


def test_branch_probe_records_failure_instead_of_branch_stdout() -> None:
    """Keep failed Git probes distinguishable from a detached HEAD in the persisted baseline."""
    probe = next(line for line in _section("### 02: Record baseline diff and branch") if "rev-parse" in line)
    assert "exit status" in probe
    assert "only on zero" in probe
    assert "On nonzero" in probe
    assert "stderr" in probe
    assert "unborn branch" in probe
    assert "not a repository" in probe
    assert "branch.txt" in probe


def test_read_only_preflight_omits_write_approval_argument() -> None:
    """Avoid rejecting a read-only plan by passing a write receipt unconditionally."""
    command = next(line for line in _section("### Required barrier") if "parallel_execution.py preflight" in line)
    literal = next(part for part in command.split("`") if "parallel_execution.py preflight" in part)
    assert "--consumer implement" in literal
    assert "--approval" not in literal
    assert "only when `write_policy.parent_writes=planned`" in command
    assert "omit it when `write_policy.parent_writes=none`" in command
    assert "--execution=parallel-read" in command


def test_detailed_commit_requires_passing_candidate_and_acceptance() -> None:
    """Prevent an automatic commit before failed quality or result validation is resolved."""
    lines = _section("### 12: Write and validate the mandatory result artifact")
    guard = next(line for line in lines if line.startswith("Only after"))
    commit = next(line for line in lines if line.startswith("Completion commit:"))
    assert lines.index(guard) < lines.index(commit)
    assert "all project-relevant gates pass" in guard
    assert "no critical findings remain" in guard
    assert "shared confidence policy passes" in guard
    assert "result candidate validation passes" in guard
    assert "otherwise record `commit skipped`" in guard
    assert "do not commit" in guard
    assert "unless the user asked for no commit, leave unstaged or to wait" in commit
    assert any("Revalidate" in line and "commit disposition" in line for line in lines)


def test_lightweight_completion_applies_grant_after_acceptance() -> None:
    """Keep small verified changes subject to the same grant and user override as detailed work."""
    lines = _section("## Lightweight Local Work")
    completion = next(line for line in lines if line.startswith("3."))
    assert "acceptance and configured checks pass" in completion
    assert "no critical findings remain" in completion
    assert "shared confidence policy passes" in completion
    assert "Local Git approval grant" in completion
    assert "completion-commit rules" in completion
    assert "otherwise record `commit skipped`" in completion
    assert "no commit, leave unstaged or to wait takes precedence" in completion

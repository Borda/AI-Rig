"""Exercise review recipes and preserve their target, coverage and child safety contracts."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from _develop_shell_plugin import shell_path
from _develop_text import bash_blocks

_PLUGIN = Path(__file__).resolve().parents[2]
_SKILL = _PLUGIN / "skills/review/SKILL.md"
_BASH = shutil.which("bash")
_skip_no_bash = pytest.mark.skipif(_BASH is None, reason="bash unavailable")


def _block(marker: str) -> str:
    """Find the shipped recipe containing the named operation."""
    return next(block for block in bash_blocks(_SKILL.read_text(encoding="utf-8")) if marker in block)


def _run(block: str, tmp_path: Path, prefix: str = "") -> subprocess.CompletedProcess[str]:
    """Execute a shipped recipe with private sentinels and the declared project interpreter."""
    env = {
        **os.environ,
        "TMPDIR": tmp_path.as_posix(),
        "CLAUDE_CODE_SESSION_ID": "target-contract",
        "CLAUDE_PLUGIN_ROOT": _PLUGIN.as_posix(),
        "TEST_PYTHON": sys.executable,
        "PYTHONIOENCODING": "utf-8",
    }
    return subprocess.run(
        [_BASH, "-c", prefix + block],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


@_skip_no_bash
@pytest.mark.parametrize("target", ["src/selected.py", "src/package"])
@pytest.mark.parametrize("line_ending", ["\n", ""])
def test_path_context_queries_selected_modules_and_keeps_full_tier(
    tmp_path: Path, target: str, line_ending: str
) -> None:
    """Explicit file and directory reviews ignore unrelated diff impact and query only the bound module set."""
    run_dir = tmp_path / ".developments" / "review target"
    run_dir.mkdir(parents=True)
    (tmp_path / "dev-review-run-dir-target-contract").write_text(
        run_dir.as_posix() + "\n", encoding="utf-8", newline="\n"
    )
    (tmp_path / "dev-review-clean-args-target-contract").write_text(target + "\n", encoding="utf-8", newline="\n")
    (tmp_path / "dev-review-target-modules-target-contract").write_text(
        "selected package.child" + line_ending, encoding="utf-8", newline="\n"
    )
    (tmp_path / "dev-review-codemap-enabled-target-contract").write_text("true\n", encoding="utf-8", newline="\n")
    result = _run(
        _block('CODEMAP_CONTEXT_STAGE="'),
        tmp_path,
        'python() { command "$TEST_PYTHON" "$@"; }\n'
        'codemap-py() { printf "%s\\n" "$*" >> "$TMPDIR/queries"; printf \'{"batch":[]}\\n\'; }\n',
    )
    assert result.returncode == 0, result.stderr
    request = json.loads((run_dir / "dev-review-codemap-batch.json-target-contract").read_text(encoding="utf-8"))
    assert [item["args"] for item in request if item["cmd"] == "rdeps"] == [["selected"], ["package.child"]]
    assert "diff-impact" not in (tmp_path / "queries").read_text(encoding="utf-8")
    assert (tmp_path / "dev-review-impact-target-contract").read_text(encoding="utf-8").startswith("FULL")


@_skip_no_bash
@pytest.mark.parametrize("owner", ["standalone", "feature", "fix", "refactor"])
def test_review_run_directory_survives_preflight_and_step_two(tmp_path: Path, owner: str) -> None:
    """Review batches and copied context stay in the same accepted run directory across fresh shells."""
    root = tmp_path / "active run"
    (tmp_path / f"dev-{owner}-dev-dir-target-contract").write_text(
        root.as_posix() + "\n", encoding="utf-8", newline="\n"
    )
    setup = _run(_block('RUN_BASE=""'), tmp_path)
    assert setup.returncode == 0, setup.stderr
    sentinel = tmp_path / "dev-review-run-dir-target-contract"
    run_dir = (tmp_path / sentinel.read_text(encoding="utf-8").strip()).resolve()
    expected_root = tmp_path / ".developments" if owner == "standalone" else root
    assert run_dir.parent == expected_root / "review"
    (tmp_path / "dev-review-target-modules-target-contract").write_text("selected\n", encoding="utf-8", newline="\n")
    (tmp_path / "dev-review-codemap-enabled-target-contract").write_text("true\n", encoding="utf-8", newline="\n")
    prefix = 'python() { command "$TEST_PYTHON" "$@"; }\ncodemap-py() { printf \'{"batch":[]}\\n\'; }\n'
    preflight = _run(_block('CODEMAP_CONTEXT_STAGE="'), tmp_path, prefix)
    assert preflight.returncode == 0, preflight.stderr
    assert (run_dir / "dev-review-codemap-batch.json-target-contract").is_file()
    staged = (tmp_path / "dev-review-codemap-context.md-target-contract").read_bytes()
    step_two = _run(_block("REPORT_DIR_LITERAL="), tmp_path, prefix)
    assert step_two.returncode == 0, step_two.stderr
    assert (tmp_path / sentinel.read_text(encoding="utf-8").strip()).resolve() == run_dir
    assert (run_dir / "codemap-context.md").read_bytes() == staged
    report_dir = Path((tmp_path / "dev-review-report-dir-target-contract").read_text(encoding="utf-8").strip())
    assert report_dir.name == run_dir.name
    assert report_dir.parent == tmp_path / ".reports" / "develop-review"


@_skip_no_bash
@pytest.mark.parametrize("converter_available", [True, False])
def test_report_sentinel_path_conversion_is_capability_guarded(tmp_path: Path, converter_available: bool) -> None:
    """The recipe converts MSYS spelling for native hooks while preserving POSIX output without cygpath."""
    fragment = next(line for line in _block("REPORT_DIR_LITERAL=").splitlines() if "REPORT_DIR=$(cygpath" in line)
    prefix = (
        "REPORT_DIR='/c/work space/.reports/develop-review/run'\n"
        f"command() {{ return {0 if converter_available else 1}; }}\n"
        'cygpath() { [ "$1" = -m ] && [ "$2" = -- ] && '
        '[ "$3" = "$REPORT_DIR" ] || return 2; printf "%s\\n" "C:/work space/.reports/develop-review/run"; }\n'
    )
    result = _run(fragment + '\nprintf "%s\\n" "$REPORT_DIR"', tmp_path, prefix)
    assert result.returncode == 0, result.stderr
    expected = (
        "C:/work space/.reports/develop-review/run"
        if converter_available
        else "/c/work space/.reports/develop-review/run"
    )
    assert result.stdout.strip() == expected


@_skip_no_bash
@pytest.mark.parametrize(
    ("target", "dirty", "exit_code"),
    [
        pytest.param("", " M module.py", 1, id="dirty-diff-blocked"),
        pytest.param("", "?? module.py", 1, id="untracked-diff-blocked"),
        pytest.param("", "", 0, id="clean-diff-allowed"),
        pytest.param("src/module.py", " M other.py", 0, id="explicit-committed-target-allowed"),
    ],
)
def test_worktree_entry_refuses_implicit_retargeting(tmp_path: Path, target: str, dirty: str, exit_code: int) -> None:
    """The entry guard stops dirty implicit targets before loading the worktree entry instructions."""
    (tmp_path / "dev-review-worktree-target-contract").write_text("true\n", encoding="utf-8", newline="\n")
    (tmp_path / "dev-review-clean-args-target-contract").write_text(target + "\n", encoding="utf-8", newline="\n")
    prefix = f"git() {{ printf '%s' '{dirty}'; }}\npython() {{ echo loaded; }}\n"
    result = _run(_block('if [ "$WORKTREE_ENABLED" = "true" ]; then python'), tmp_path, prefix)
    assert result.returncode == exit_code, result.stdout + result.stderr
    assert ("loaded" in result.stdout) == (exit_code == 0)
    assert (tmp_path / "dev-worktree-skill-target-contract").read_text(encoding="utf-8").strip() == "review"


@_skip_no_bash
def test_committed_diff_identity_names_worktree_source(tmp_path: Path) -> None:
    """Only an implicit committed-diff review receives the committed worktree source label."""
    (tmp_path / "dev-review-worktree-target-contract").write_text("true\n", encoding="utf-8", newline="\n")
    result = _run(_block("REVIEWED_COMMIT_SHA=$("), tmp_path, "git() { echo frozen-sha; }\n")
    assert result.returncode == 0, result.stderr
    kind = (tmp_path / "dev-review-commit-kind-target-contract").read_text(encoding="utf-8")
    assert "committed worktree source" in kind
    assert "HEAD~1..HEAD" in kind
    assert "uncommitted" not in kind


def test_intent_reviewer_cannot_be_ranked_out() -> None:
    """Every review retains the intent duty while counting its unit against the existing cap."""
    lines = _SKILL.read_text(encoding="utf-8").splitlines()
    assert any("Agent 1" in line and "never ranked out" in line and "cap" in line for line in lines)


def test_every_child_gets_read_only_and_task_tracking_limits() -> None:
    """Blind solve, dimensions, verifiers and consolidation inherit explicit execution boundaries."""
    lines = _SKILL.read_text(encoding="utf-8").splitlines()
    assert any("Agents 0–7" in line and "verifiers" in line and "consolidator" in line for line in lines)
    assert any("Task tracking: do NOT call TaskCreate or TaskUpdate" in line for line in lines)
    assert any("no fixers" in line and "outside `$RUN_DIR`" in line for line in lines)


def test_plan_claim_uses_affected_paths_and_reaches_intent_reviewer() -> None:
    """Free-text stem matches cannot supply a plan claim to the blind or intent reviewers."""
    lines = _SKILL.read_text(encoding="utf-8").splitlines()
    brief = (_PLUGIN / "skills/review/templates/brief-sw-engineer.md").read_text(encoding="utf-8").splitlines()
    assert any("Affected files" in line and "exact" in line and "paths" in line for line in lines)
    assert not any("grep -qi" in line or line.startswith("STEMS=") for line in lines)
    assert any("PLAN_CLAIM=" in line and "Agent 1" in line for line in lines)
    assert any("Path/directory" in line and "no diff" in line for line in brief)


def test_skip_marker_survives_parent_validation_write() -> None:
    """The final validation artifact retains the missing-protocol signal after the parent's Write."""
    lines = _SKILL.read_text(encoding="utf-8").splitlines()
    write_line = next(index for index, line in enumerate(lines) if "parent's all-severity" in line)
    assert any("Cross-Validation: SKIPPED" in line for line in lines[write_line:])


def test_blocking_handoff_carries_finding_and_location() -> None:
    """The suggested fix command carries a bounded symptom instead of dropping review evidence."""
    lines = _SKILL.read_text(encoding="utf-8").splitlines()
    assert any('/develop:fix "<finding title> (<file:line>)"' in line for line in lines)


def test_dependency_audit_targets_changed_specification() -> None:
    """Dependency review never substitutes the active interpreter for the changed dependency input."""
    lines = (
        (_PLUGIN / "skills/review/templates/brief-security-augmentation.md").read_text(encoding="utf-8").splitlines()
    )
    assert any('pip-audit -r "$REQ_FILE"' in line for line in lines)
    assert not any("<run pip-audit>" in line for line in lines)
    assert any("REQ_FILE" in line and "changed" in line and "requirements" in line for line in lines)


@_skip_no_bash
@pytest.mark.parametrize("line_ending", ["\n", ""])
def test_matched_plan_path_is_preserved_at_end_of_file(tmp_path: Path, line_ending: str) -> None:
    """Write-tool plan claims remain readable even when their sentinel ends without LF."""
    plan = tmp_path / "matched plan.md"
    plan.write_text("# Intended feature\n", encoding="utf-8", newline="\n")
    (tmp_path / "dev-review-plan-target-contract").write_text(
        plan.as_posix() + line_ending, encoding="utf-8", newline="\n"
    )
    (tmp_path / "dev-review-run-dir-target-contract").write_text(
        tmp_path.as_posix() + "\n", encoding="utf-8", newline="\n"
    )
    result = _run(_block('echo "blind-solve input:'), tmp_path)
    assert result.returncode == 0, result.stderr
    assert "# Intended feature" in (tmp_path / "blind-solve-input.md").read_text(encoding="utf-8")


@_skip_no_bash
@pytest.mark.parametrize("audit_exit", [0, 1])
def test_dependency_command_keeps_quoted_spec_and_audit_result(tmp_path: Path, audit_exit: int) -> None:
    """The shipped audit command passes the changed spec and preserves vulnerability exit status."""
    line = (
        (_PLUGIN / "skills/review/templates/brief-security-augmentation.md").read_text(encoding="utf-8").splitlines()[0]
    )
    command = next(piece for piece in line.split("`") if piece.startswith("if ! command -v pip-audit"))
    result = _run(
        command,
        tmp_path,
        'REQ_FILE="requirements changed.txt"\nRUN_DIR="$TMPDIR"\n'
        f'pip-audit() {{ printf "%s\\n" "$@" > "$TMPDIR/audit-args"; return {audit_exit}; }}\n',
    )
    assert result.returncode == audit_exit, result.stderr
    arguments = (tmp_path / "audit-args").read_text(encoding="utf-8").splitlines()
    assert arguments[:-1] == [
        "-r",
        "requirements changed.txt",
        "--no-deps",
        "--disable-pip",
        "--cache-dir",
    ]
    assert shell_path(arguments[-1]) == tmp_path / "pip-audit-cache"

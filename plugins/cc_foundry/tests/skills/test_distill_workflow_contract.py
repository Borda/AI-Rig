"""Exercise distill recipes and guard the approval and handoff paths they instruct."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from _hook_env import _bash_runs_posix_script
from _plugin_payload import install_foundry

_PLUGIN = Path(__file__).resolve().parents[2]
_DISTILL = _PLUGIN / "skills/distill"
_BASH = pytest.mark.skipif(not _bash_runs_posix_script(), reason="POSIX bash unavailable")


def _lines(relative: str) -> list[str]:
    """Read instruction lines without relying on prose wrapping."""
    return (_DISTILL / relative).read_text(encoding="utf-8").splitlines()


def _blocks(relative: str) -> list[str]:
    """Collect executable Bash fences in document order."""
    result = []
    current: list[str] | None = None
    for line in _lines(relative):
        if line == "```bash":
            current = []
        elif line == "```" and current is not None:
            result.append("\n".join(current))
            current = None
        elif current is not None:
            current.append(line)
    return result


def _run(script: str, root: Path) -> subprocess.CompletedProcess[str]:
    """Run a recipe in a fresh shell using real plugin helpers and private session state."""
    return subprocess.run(
        ["bash"],
        input='unset RUN_DIR EXT_RUN_DIR; python() { "$DISTILL_PYTHON" "$@"; }\n' + script,
        cwd=root,
        env={
            **os.environ,
            "DISTILL_PYTHON": sys.executable,
            "CLAUDE_PLUGIN_ROOT": str(install_foundry(root)),
            "HOME": str(root),
            "USERPROFILE": str(root),
            "TMPDIR": str(root),
            "CLAUDE_CODE_SESSION_ID": "distill-test",
        },
        text=True,
        capture_output=True,
        check=False,
    )


@_BASH
def test_flag_cleaning_survives_host_argument_substitution(tmp_path: Path) -> None:
    """Textual host expansion must not undo the parser's removal of known flags."""
    recipe = _blocks("SKILL.md")[0].replace("$ARGUMENTS", "memory --eager --project")
    result = _run(recipe, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "ARGUMENTS_FINAL=memory" in result.stdout.splitlines()
    assert "EAGER=true" in result.stdout.splitlines()
    assert "PROJECT_FLAG=true" in result.stdout.splitlines()


def test_modes_dispatch_before_default_inventory() -> None:
    """Memory and extraction routes avoid the unrelated installed-cache inventory."""
    lines = _lines("SKILL.md")
    dispatch = next(i for i, line in enumerate(lines) if "**If first token equals `external`**" in line)
    inventory = next(i for i, line in enumerate(lines) if 'find "$HOME/.claude/plugins/cache"' in line)
    assert dispatch < inventory
    assert any("rev-parse --is-inside-work-tree" in line for line in lines)


def test_memory_target_verification_does_not_require_skipped_inventory() -> None:
    """Early dispatch still verifies the concrete agent or skill before proposing its edit."""
    line = next(line for line in _lines("modes/memory.md") if line.startswith("**Agent/skill name verification**"))
    assert "roster from Step 1" not in line
    assert "Read" in line
    assert "target" in line


@_BASH
def test_history_scan_reports_absence_outside_a_repository(tmp_path: Path) -> None:
    """The default route degrades to task evidence without invoking unguarded Git history reads."""
    recipe = next(block for block in _blocks("SKILL.md") if "git log --oneline -50" in block)
    result = _run(recipe, tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["git history unavailable"] * 3
    assert result.stderr == ""


def test_feedback_selection_uses_frontmatter_instead_of_filename() -> None:
    """A freely named feedback file is included while a misnamed project memory is excluded."""
    lines = _lines("modes/memory.md")
    assert not any("feedback_*.md" in line for line in lines)
    assert any("frontmatter" in line and "`type: feedback`" in line for line in lines)
    assert any("Glob" in line and "`*.md`" in line and "MEMORY.md" in line for line in lines)


@pytest.mark.parametrize("relative", ["modes/prune.md", "modes/external.md"])
def test_skill_calls_use_the_foundry_namespace(relative: str) -> None:
    """Adoption prompts and pruning next steps must dispatch the installed plugin skill."""
    lines = _lines(relative)
    calls = [line for line in lines if "external mode" in line or "Rule conversions proposed:" in line]
    assert calls
    assert not any("/distill external" in line or "via /manage)" in line for line in calls)
    assert all("/foundry:distill external" in line or "/foundry:manage" in line for line in calls)


@_BASH
def test_memory_inventory_counts_freely_named_topic_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The picker lists every topic file for frontmatter inspection, including unlinked feedback."""
    monkeypatch.setenv("HOME", str(tmp_path))
    memory = tmp_path / ".claude/projects/example/memory"
    memory.mkdir(parents=True)
    (memory / "MEMORY.md").write_text("[Project](feedback_misnamed.md)\n", encoding="utf-8")
    (memory / "preferences.md").write_text("---\ntype: feedback\n---\nUse focused tests.\n", encoding="utf-8")
    (memory / "feedback_misnamed.md").write_text("---\ntype: project\n---\nUse Python.\n", encoding="utf-8")
    result = _run(_blocks("modes/memory.md")[0], tmp_path)
    assert result.returncode == 0, result.stderr
    assert "| 2 memory files |" in result.stdout


def test_prune_judges_memory_bodies_and_keeps_index_consistent() -> None:
    """Removing a pointer alone cannot count as dropping the underlying memory."""
    lines = _lines("modes/prune.md")
    assert any("Glob" in line and "`*.md`" in line and "MEMORY.md" in line for line in lines)
    assert any("Drop:" in line and "file" in line and "index" in line for line in lines)
    assert any("Trim:" in line and "frontmatter" in line for line in lines)
    assert not any("project CLAUDE.md `Memory Policy`" in line for line in lines)
    assert any('subagent_type="general-purpose"' in line and 'name="' in line for line in lines)
    assert any('subagent_type="foundry:curator"' in line and 'name="' in line for line in lines)


def test_memory_review_first_enters_the_snapshot_apply_path() -> None:
    """Per-proposal approval rejoins the same pre-write snapshot and post-write diff gate."""
    lines = _lines("modes/memory.md")
    review = next(line for line in lines if line.startswith("User selects (b)"))
    assert "each proposal" in review
    assert "approval" in review
    assert "same" in review
    assert "snapshot" in review
    assert "diff gate" in review
    assert any("either (a) or (b)" in line and "Snapshot first" in line for line in lines)


def test_external_actions_are_keyed_by_labels() -> None:
    """Removing the install option cannot turn Review first into installation."""
    lines = _lines("modes/external.md")
    start = lines.index("**E14: Apply**")
    end = lines.index("**E15: Verify and report**")
    apply = lines[start:end]
    assert not any("Option (" in line for line in apply)
    assert any(line.startswith("- `Review first`:") and "approval" in line for line in apply)
    assert any(line.startswith("- `Install as standalone plugin`:") for line in apply)
    assert any(line.startswith("- `Skip`:") for line in apply)


def test_external_confidence_follows_quality_review() -> None:
    """The final result incorporates reviewer outcomes and cannot close before dispatch."""
    lines = _lines("modes/external.md")
    review = lines.index("**E16: quality review — by file type**")
    confidence = next(i for i, line in enumerate(lines) if "End with `## Confidence`" in line)
    assert confidence > review
    assert not any("git diff HEAD" in line for line in lines)
    assert any("snapshot" in line and "new" in line for line in lines)


def test_external_reads_executables_before_adoption() -> None:
    """The directory route inventories the executable types it later subjects to review."""
    directory = next(line for line in _lines("modes/external.md") if line.startswith("- Directory"))
    assert all(extension in directory for extension in ("*.py", "*.sh", "*.ts", "*.mjs", "*.ps1"))


def test_inline_scan_handoff_is_file_backed_and_includes_local_config() -> None:
    """A project-only scan has a real unit and agents load the installed efficiency protocol."""
    lines = _lines("modes/executables.md")
    assert not any("attached block JSONL" in line for line in lines)
    assert any(".claude/" in line and "one scan unit" in line for line in lines)
    assert any("blocks-<plugin>.jsonl" in line and "Read" in line for line in lines)
    assert any("load_mode.py" in line and "efficiency.md" in line for line in lines)
    assert any("Substitute" in line and "$RUN_DIR" in line and "prompt" in line for line in lines)


def test_extraction_cannot_commit_and_versioning_requires_plugin_layout() -> None:
    """An extraction agent leaves its pinned HEAD intact and local config needs no plugin manifest."""
    lines = _lines("modes/executables.md")
    assert any("Do not commit" in line for line in lines)
    assert any("Only" in line and ".claude-plugin/plugin.json" in line for line in lines)
    assert any("extract_count == 0" in line for line in lines)
    assert any("extract_count == 0" in line for line in _lines("SKILL.md"))


def test_prose_only_has_a_separate_snapshot_and_diff_path() -> None:
    """Selecting compression cannot silently skip every extraction-only step."""
    lines = _lines("modes/executables.md")
    branch = next(line for line in lines if line.startswith("**`Prose only`"))
    assert "snapshot" in branch
    assert "diff" in branch
    assert "curator" in branch
    assert "script" in branch


@_BASH
@pytest.mark.parametrize(
    ("relative", "sentinel", "variable"),
    [
        pytest.param("modes/memory.md", "memory", "RUN_DIR", id="memory-review"),
        pytest.param("modes/executables.md", "exec", "RUN_DIR", id="extraction-reviews"),
        pytest.param("modes/external.md", "external", "EXT_RUN_DIR", id="external-reviews"),
    ],
)
def test_notification_watch_reloads_state_in_a_fresh_shell(
    tmp_path: Path, relative: str, sentinel: str, variable: str
) -> None:
    """Every documented watcher can find real batch rows without inherited shell variables."""
    run_dir = tmp_path / "run with spaces"
    run_dir.mkdir()
    (run_dir / "agent-watch-test.tsv").write_text("review\t-\t900\n", encoding="utf-8")
    (tmp_path / f"distill-{sentinel}-run-dir-distill-test").write_text(str(run_dir) + "\n", encoding="utf-8")
    commands = [
        span
        for line in _lines(relative)
        for span in line.split("`")
        if "agent_watch.py" in span and f'"${variable}"' in span
    ]
    assert commands
    results = [_run(command, tmp_path) for command in commands]
    assert all(result.returncode == 0 for result in results), [(r.stdout, r.stderr) for r in results]
    assert all("awaiting-envelope" in result.stdout for result in results)


@_BASH
@pytest.mark.parametrize("new_file", [False, True])
def test_external_diff_uses_preapply_bytes_and_includes_untracked_files(tmp_path: Path, new_file: bool) -> None:
    """The actual safety-net recipes expose only this run's delta, even outside a Git repository."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    existing = tmp_path / "existing.md"
    existing.write_bytes(b"user's existing changes\n")
    target = tmp_path / "new.md" if new_file else existing
    (run_dir / "targets.txt").write_text(target.name + "\n", encoding="utf-8")
    (tmp_path / "distill-external-run-dir-distill-test").write_text(str(run_dir) + "\n", encoding="utf-8")
    blocks = _blocks("modes/external.md")
    snapshot = next(block for block in blocks if 'cp -p "$f"' in block)
    result = _run(snapshot, tmp_path)
    assert result.returncode == 0, result.stderr
    target.write_bytes(b"user's existing changes\ndistilled addition\n")
    delta = next(block for block in blocks if "diff -u " in block)
    result = _run(delta, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "distilled addition" in result.stdout
    assert (f"=== new file: {target.name}" in result.stdout) == new_file
    assert "+user's existing changes" not in result.stdout
    assert existing.read_bytes() == (
        b"user's existing changes\n" if new_file else b"user's existing changes\ndistilled addition\n"
    )
    saved = run_dir / "pre" / target.name
    assert (saved.read_bytes() if saved.exists() else None) == (None if new_file else b"user's existing changes\n")
    assert (run_dir / "pre/new-files.txt").read_text(encoding="utf-8") == (target.name + "\n" if new_file else "")

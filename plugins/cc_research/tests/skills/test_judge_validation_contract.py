"""Exercise judge's command boundaries and verdict inputs from the shipped skill."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parents[2]
_SKILL = _PLUGIN / "skills/judge/SKILL.md"
_BASH = shutil.which("bash")


def _section(start: str, end: str) -> list[str]:
    """Read one heading-delimited skill section without depending on prose wrapping."""
    lines = _SKILL.read_text(encoding="utf-8").splitlines()
    first = next(index for index, line in enumerate(lines) if line.startswith(start))
    last = next(index for index in range(first + 1, len(lines)) if lines[index].startswith(end))
    return lines[first:last]


def _bash_blocks(lines: list[str]) -> list[str]:
    """Extract runnable fences using their line delimiters."""
    blocks = []
    current = None
    for line in lines:
        if line == "```bash":
            current = []
        elif line == "```" and current is not None:
            blocks.append("\n".join(current))
            current = None
        elif current is not None:
            current.append(line)
    return blocks


def test_validation_requires_fresh_approval_before_program_execution() -> None:
    """Autodetected programs cannot silently authorize local or container commands."""
    lines = _section("## Step J4:", "## Step J5a:")
    approval = next(index for index, line in enumerate(lines) if "**Execution approval**" in line)
    snapshot = next(index for index, line in enumerate(lines) if "dirty-before.json" in line)
    assert approval < snapshot
    assert any("AskUserQuestion" in line and "each command" in line for line in lines)
    assert any("resolved program path" in line and "exact" in line for line in lines)
    assert any("standalone judge invocation always asks" in line for line in lines)
    assert any("hooks" in line and "approval" in line for line in lines)
    assert any("directly invoked entry-point file" in line for line in lines)
    assert not any("downstream workflows" in line for line in lines)


def test_explicit_validation_skip_is_advisory_and_excluded_from_required_changes() -> None:
    """Sweep can approve a sound program whose user explicitly deferred execution."""
    lines = _section("## Step J4:", "## Step J5a:")
    skip = next(line for line in lines if "Skip if `$SKIP_VALIDATION`" in line)
    assert "`medium` finding" in skip
    assert "Required Changes" in skip
    assert "APPROVED" in skip
    assert "not achievable" not in skip


def test_docker_validation_has_no_host_fallback() -> None:
    """Judge uses run's rejecting Docker executor and declared network isolation."""
    lines = _section("## Step J4:", "## Step J5a:")
    assert any("docker_sandbox_run.py" in line and '--mode verify "$COMMAND"' in line for line in lines)
    assert any("SANDBOX_NETWORK=" in line and "<resolved sandbox_network>" in line for line in lines)
    assert any("never fall back" in line and "host" in line for line in lines)
    assert not any("docker` is still validated on this machine" in line for line in lines)


def test_validation_uses_run_timeout_instead_of_six_minutes() -> None:
    """A slow local metric cannot pass judge under a larger default than run."""
    lines = _section("## Step J4:", "## Step J5a:")
    assert any('VERIFY_TIMEOUT_SEC="${VERIFY_TIMEOUT_SEC:-120}"' in line for line in lines)
    assert any("timeout: $VERIFY_TIMEOUT_MS" in line for line in lines)
    assert not any("timeout: 360000" in line for line in lines)


def test_reviews_require_machine_readable_ratings_and_goodhart_flags() -> None:
    """Reviews supply authoritative verdict data rather than prose keyword matches."""
    lines = (_PLUGIN / "skills/_shared/judge-j3-prompts.md").read_text(encoding="utf-8").splitlines()
    assert any("scientific_rating: <sound|needs-refinement|fundamentally-flawed>" in line for line in lines)
    assert any("methodology_rating: <sound|needs-refinement|fundamentally-flawed>" in line for line in lines)
    assert any("goodhart: <true|false>" in line for line in lines)
    envelopes = [line for line in lines if line.startswith('{"status"') or line.startswith("Return ONLY: {")]
    assert len(envelopes) == 2
    assert all('"goodhart":true|false' in line for line in envelopes)
    merged = next(line for line in lines if line.startswith('[{"status"'))
    assert merged.count('"goodhart":true|false') == 2
    assert sum("## Verdict" in line and "goodhart:" in line for line in lines) >= 2
    dispatch = _section("## Step J3:", "## Step J4:")
    assert not any("append these requirements" in line for line in dispatch)
    assert any("Reviews are static" in line and "do not execute" in line for line in dispatch)
    assert any("Use `methodology_rating`" in line and "J5b" in line and "file" in line for line in dispatch)
    verdict = _section("## Step J6:", "</workflow>")
    assert any("Otherwise" in line and "NEEDS-REVISION" in line for line in verdict)
    assert any("`goodhart == true`" in line for line in verdict)
    assert any("scientific_rating` in {`sound`, `needs-refinement`}" in line for line in verdict)
    assert any('scientific_rating == "unrecognized"' in line and "NEEDS-REVISION" in line for line in verdict)


def test_spawn_notes_reference_deadline_protocol_and_completion_notifications() -> None:
    """Background reviewers retain the shared watch-file and deadline protocol."""
    lines = _section("## Step J3:", "## Step J4:")
    assert any("agent-resolution.md" in line and "Agent waits" in line and "agent-watch" in line for line in lines)
    assert not any("after the merged Agent() returns" in line or "After the spawn returns" in line for line in lines)


def test_bridge_probe_does_not_require_claude_cli() -> None:
    """IDE hosts with an available bridge still receive the adversarial pass."""
    lines = _section("## Step J5a:", "## Step J5b:")
    assert any("check_bridge.py" in line for line in lines)
    assert not any("command -v claude" in line or "cli-missing" in line for line in lines)


def test_reports_disclose_unreviewed_methodology_and_bridge_absence() -> None:
    """Both report headers describe a missing methodology review accurately."""
    lines = _section("## Step J6:", "</workflow>")
    fields = [line for line in lines if line.startswith("Methodology:")]
    assert len(fields) == 2
    assert all("timed-out (unreviewed)" in line for line in fields)
    assert any("Skipped — bridge plugin not installed or disabled" in line for line in lines)


@pytest.fixture
def dirty_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Create an unborn Git repository with an untracked user file and a judge run directory."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git_dir = repo / ".git"
    (git_dir / "objects").mkdir(parents=True)
    (git_dir / "refs/heads").mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="ascii")
    (repo / "notes.txt").write_text("original dirty content\n", encoding="utf-8")
    run_dir = repo / ".experiments/judge-test"
    run_dir.mkdir(parents=True)
    return repo, run_dir


def _write_index(repo: Path, content: bytes) -> None:
    """Build a one-file index fixture directly without staging or committing through Git."""
    blob = b"blob " + str(len(content)).encode("ascii") + b"\0" + content
    object_id = hashlib.sha1(blob, usedforsecurity=False).digest()
    object_path = repo / ".git/objects" / object_id.hex()[:2] / object_id.hex()[2:]
    object_path.parent.mkdir(exist_ok=True)
    object_path.write_bytes(zlib.compress(blob))
    name = b"notes.txt"
    entry = struct.pack(">10I", 0, 0, 0, 0, 0, 0, 0o100644, 0, 0, len(content))
    entry += object_id + struct.pack(">H", len(name)) + name + b"\0"
    entry += b"\0" * (-len(entry) % 8)
    index = b"DIRC" + struct.pack(">II", 2, 1) + entry
    (repo / ".git/index").write_bytes(index + hashlib.sha1(index, usedforsecurity=False).digest())


def _write_head(repo: Path, tree: bytes = b"") -> str:
    """Create valid tree and commit objects without invoking any Git mutation command."""
    tree_object = b"tree " + str(len(tree)).encode("ascii") + b"\0" + tree
    tree_id = hashlib.sha1(tree_object, usedforsecurity=False).hexdigest()
    commit = f"tree {tree_id}\nauthor Test <test@example.com> 0 +0000\ncommitter Test <test@example.com> 0 +0000\n\nbase\n".encode()
    commit_object = b"commit " + str(len(commit)).encode("ascii") + b"\0" + commit
    commit_id = hashlib.sha1(commit_object, usedforsecurity=False).hexdigest()
    for identity, data in ((tree_id, tree_object), (commit_id, commit_object)):
        path = repo / ".git/objects" / identity[:2] / identity[2:]
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(zlib.compress(data))
    (repo / ".git/refs/heads/main").write_text(commit_id + "\n", encoding="ascii")
    return commit_id


def _snapshot(repo: Path, run_dir: Path, phase: str) -> subprocess.CompletedProcess[str]:
    """Execute judge's actual Python snapshot, without relying on a host Bash or Python alias."""
    helper = _PLUGIN / "bin/judge_tree_snapshot.py"
    return subprocess.run(
        [sys.executable, str(helper), str(run_dir), phase],
        cwd=repo,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"},
        capture_output=True,
        text=True,
        timeout=15,
    )


@pytest.mark.integration
def test_snapshot_detects_overwrite_without_status_change(dirty_repo: tuple[Path, Path]) -> None:
    """An already-untracked file remains ?? after overwrite, but its content change is reported."""
    repo, run_dir = dirty_repo
    before = _snapshot(repo, run_dir, "before")
    (repo / "notes.txt").write_text("overwritten content\n", encoding="utf-8")
    after = _snapshot(repo, run_dir, "after")
    assert before.returncode == after.returncode == 0, (before.stderr, after.stderr)
    assert "J4 changed the working tree" in after.stdout
    assert "notes.txt" in after.stdout
    assert (run_dir / "dirty-before.json").is_file()


@pytest.mark.integration
def test_snapshot_ignores_its_own_run_artifacts(dirty_repo: tuple[Path, Path]) -> None:
    """Judge reports and snapshot output do not create false program-side-effect findings."""
    repo, run_dir = dirty_repo
    before = _snapshot(repo, run_dir, "before")
    (run_dir / "metric-output.txt").write_text("42\n", encoding="utf-8")
    after = _snapshot(repo, run_dir, "after")
    assert before.returncode == after.returncode == 0, (before.stderr, after.stderr)
    assert "J4 working tree unchanged" in after.stdout


@pytest.mark.integration
def test_snapshot_detects_deleted_dirty_file(dirty_repo: tuple[Path, Path]) -> None:
    """A command deleting pre-existing untracked content cannot hide by making Git status cleaner."""
    repo, run_dir = dirty_repo
    before = _snapshot(repo, run_dir, "before")
    (repo / "notes.txt").unlink()
    after = _snapshot(repo, run_dir, "after")
    assert before.returncode == after.returncode == 0, (before.stderr, after.stderr)
    assert "notes.txt" in after.stdout
    assert "J4 changed the working tree" in after.stdout


@pytest.mark.integration
def test_snapshot_failure_stops_validation(dirty_repo: tuple[Path, Path]) -> None:
    """Missing baseline evidence fails closed instead of declaring an unchanged tree."""
    repo, run_dir = dirty_repo
    result = _snapshot(repo, run_dir, "after")
    assert result.returncode != 0
    assert "dirty-before.json" in result.stderr


@pytest.mark.integration
def test_snapshot_detects_index_only_changes(dirty_repo: tuple[Path, Path]) -> None:
    """Changed staged bytes remain visible when worktree bytes and AM status are unchanged."""
    repo, run_dir = dirty_repo
    _write_index(repo, b"first staged content\n")
    before = _snapshot(repo, run_dir, "before")
    _write_index(repo, b"replacement staged content\n")
    after = _snapshot(repo, run_dir, "after")
    assert before.returncode == after.returncode == 0, (before.stderr, after.stderr)
    assert "J4 changed the working tree" in after.stdout
    assert "notes.txt" in after.stdout


@pytest.mark.integration
@pytest.mark.skipif(not _BASH, reason="Docker skill route requires Bash")
def test_docker_route_rejects_shell_composition_before_host_execution(tmp_path: Path) -> None:
    """The real verify wrapper rejects pipes without executing the metric on the host."""
    blocks = _bash_blocks(_section("## Step J4:", "## Step J5a:"))
    block = next(block for block in blocks if "docker_sandbox_run.py" in block)
    script = block.replace("<command>", "echo 42 | echo host-executed")
    script = script.replace("<resolved sandbox_network>", "none").replace("<VERIFY_TIMEOUT_SEC>", "120")
    python_path = Path(sys.executable).as_posix()
    script = script.replace(" python ", f' "{python_path}" ')
    result = subprocess.run(
        [_BASH, "-c", script],
        env={**os.environ, "CLAUDE_PLUGIN_ROOT": _PLUGIN.as_posix()},
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "metacharacters" in result.stderr
    assert "host-executed" not in result.stdout


def test_metric_drift_keeps_guard_and_discloses_every_unexecuted_command() -> None:
    """A metric's file writes cannot hide an untested guard behind APPROVED."""
    lines = _section("## Step J4:", "## Step J5a:")
    assert any("metric drift" in line and "separately approved guard" in line for line in lines)
    assert not any("Stop further command execution on drift" in line for line in lines)
    assert any("any reason" in line and "`medium`" in line and "naming that command" in line for line in lines)
    assert any("snapshot failure" in line and "wrapper rejection" in line and "abort" in line for line in lines)
    report = _section("## Step J6:", "</workflow>")
    rows = [line for line in report if line.startswith("| metric_cmd") or line.startswith("| guard_cmd")]
    assert len(rows) == 2
    assert all("not run (<reason>)" in line and "unverified (<reason>)" in line for line in rows)
    terminal = next(line for line in report if line.startswith("Validation:"))
    assert "metric=" in terminal
    assert "guard=" in terminal
    assert "not run (<reason>)" in terminal
    assert "unverified (<reason>)" in terminal


def test_non_program_failures_are_advisory_without_weakening_program_failures() -> None:
    """Judge-side evidence failures cannot demand futile edits to program.md."""
    lines = _section("## Step J4:", "## Step J5a:")
    classification = next(line for line in lines if "**Non-program failures**" in line)
    assert all(term in classification for term in ("snapshot", "rating", "goodhart", "Docker", "127", "`medium`"))
    assert "Required Changes" in classification
    assert "APPROVED" in classification
    assert any("metacharacter" in line and "destructive-token" in line and "`critical`" in line for line in lines)
    assert any("guard exits non-zero" in line and "critical" in line for line in lines)
    assert any("snapshot failure never cancels" in line and "critical" in line for line in lines)
    ratings = _section("## Step J5b:", "## Step J6:")
    assert any("unrecognized" in line and "`medium`" in line for line in ratings)
    assert any("malformed Goodhart" in line and "`medium`" in line and "unreviewed" in line for line in ratings)
    assert any("goodhart=null" in line and "never" in line and "false" in line for line in ratings)
    report = _section("## Step J6:", "</workflow>")
    assert any("Required Changes" in line and "non-program" in line for line in report)
    assert any(line.startswith("Review fields:") and "unreviewed" in line for line in report)


def test_snapshot_phases_are_fixed_calls_with_no_editable_heredoc() -> None:
    """The documented after phase cannot accidentally overwrite the baseline."""
    blocks = _bash_blocks(_section("## Step J4:", "## Step J5a:"))
    calls = [block for block in blocks if "judge_tree_snapshot.py" in block]
    assert len(calls) == 2
    assert '"$RUN_DIR" before' in calls[0]
    assert '"$RUN_DIR" after' in calls[1]
    assert all("<<'PY'" not in block for block in calls)


@pytest.mark.integration
def test_snapshot_refuses_to_replace_existing_baseline(dirty_repo: tuple[Path, Path]) -> None:
    """Verbatim reruns preserve the original content hashes and fail visibly."""
    repo, run_dir = dirty_repo
    before = _snapshot(repo, run_dir, "before")
    original = (run_dir / "dirty-before.json").read_bytes()
    (repo / "notes.txt").write_text("changed\n", encoding="utf-8")
    repeated = _snapshot(repo, run_dir, "before")
    assert before.returncode == 0, before.stderr
    assert repeated.returncode != 0
    assert (run_dir / "dirty-before.json").read_bytes() == original


@pytest.mark.integration
def test_snapshot_accepts_run_directory_outside_repository(dirty_repo: tuple[Path, Path], tmp_path: Path) -> None:
    """An external artifact directory needs no repository exclude pathspec."""
    repo, _ = dirty_repo
    external = tmp_path / "external-run"
    external.mkdir()
    before = _snapshot(repo, external, "before")
    after = _snapshot(repo, external, "after")
    assert before.returncode == after.returncode == 0, (before.stderr, after.stderr)
    assert "J4 working tree unchanged" in after.stdout


@pytest.mark.integration
def test_snapshot_reports_deleted_clean_tracked_filename(dirty_repo: tuple[Path, Path]) -> None:
    """A newly deleted index file must be named even though it was absent from baseline hashes."""
    repo, run_dir = dirty_repo
    content = b"original dirty content\n"
    _write_index(repo, content)
    blob_id = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content, usedforsecurity=False).digest()
    _write_head(repo, b"100644 notes.txt\0" + blob_id)
    before = _snapshot(repo, run_dir, "before")
    (repo / "notes.txt").unlink()
    after = _snapshot(repo, run_dir, "after")
    assert before.returncode == after.returncode == 0, (before.stderr, after.stderr)
    assert "notes.txt" in after.stdout


@pytest.mark.integration
def test_snapshot_hashes_nested_repository_head_and_dirty_content(dirty_repo: tuple[Path, Path]) -> None:
    """A nested repository remains snapshotable and its status changes are visible."""
    repo, run_dir = dirty_repo
    nested = repo / "sub"
    nested.mkdir()
    shutil.copytree(repo / ".git", nested / ".git")
    head = _write_head(nested)
    before = _snapshot(repo, run_dir, "before")
    (nested / "new.txt").write_text("nested change\n", encoding="utf-8")
    after = _snapshot(repo, run_dir, "after")
    assert before.returncode == after.returncode == 0, (before.stderr, after.stderr)
    state = json.loads((run_dir / "dirty-before.json").read_text(encoding="utf-8"))["files"]["sub/"]
    assert state["head"] == head
    assert state["status_sha256"] == hashlib.sha256(b"").hexdigest()
    assert "sub/" in after.stdout
    assert "J4 changed" in after.stdout


@pytest.mark.integration
def test_snapshot_checks_other_ignored_experiment_state(dirty_repo: tuple[Path, Path]) -> None:
    """Ignored prior-run state is protected while this run's evidence stays excluded."""
    repo, run_dir = dirty_repo
    (repo / ".gitignore").write_text(".experiments/\n.env\n", encoding="utf-8")
    state = repo / ".experiments/state/earlier/result.json"
    state.parent.mkdir(parents=True)
    state.write_text("old", encoding="utf-8")
    before = _snapshot(repo, run_dir, "before")
    state.write_text("new", encoding="utf-8")
    (run_dir / "report.txt").write_text("evidence", encoding="utf-8")
    after = _snapshot(repo, run_dir, "after")
    assert before.returncode == after.returncode == 0, (before.stderr, after.stderr)
    assert ".experiments/state/earlier/result.json" in after.stdout
    assert "report.txt" not in after.stdout
    lines = _section("## Step J4:", "## Step J5a:")
    assert any("other ignored files are not checked" in line for line in lines)
    report = _section("## Step J6:", "</workflow>")
    assert any(
        line.startswith("Snapshot coverage:") and "other ignored files are not checked" in line for line in report
    )


@pytest.mark.integration
@pytest.mark.skipif(not _BASH, reason="Docker skill route requires Bash")
@pytest.mark.parametrize("missing", ["<command>", "<resolved sandbox_network>", "<VERIFY_TIMEOUT_SEC>"])
def test_docker_route_rejects_each_unsubstituted_placeholder(tmp_path: Path, missing: str) -> None:
    """A partially filled Docker example must abort before reaching the wrapper."""
    blocks = _bash_blocks(_section("## Step J4:", "## Step J5a:"))
    block = next(block for block in blocks if "docker_sandbox_run.py" in block)
    replacements = {"<command>": "echo 42", "<resolved sandbox_network>": "none", "<VERIFY_TIMEOUT_SEC>": "120"}
    script = block.replace("<command>", replacements["<command>"] if missing != "<command>" else missing)
    script = script.replace(
        "<resolved sandbox_network>",
        replacements["<resolved sandbox_network>"] if missing != "<resolved sandbox_network>" else missing,
    )
    script = script.replace(
        "<VERIFY_TIMEOUT_SEC>", replacements["<VERIFY_TIMEOUT_SEC>"] if missing != "<VERIFY_TIMEOUT_SEC>" else missing
    )
    result = subprocess.run([_BASH, "-c", script], cwd=tmp_path, capture_output=True, text=True, timeout=15)
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "not substituted" in result.stdout
    assert "can't open file" not in result.stderr


@pytest.mark.integration
@pytest.mark.skipif(not _BASH, reason="Timeout preparation requires Bash")
@pytest.mark.parametrize("seconds", ["120", "240"])
def test_docker_tool_timeout_leaves_cleanup_headroom(seconds: str) -> None:
    """The tool timeout leaves the wrapper thirty seconds to stop its container."""
    lines = _section("## Step J4:", "## Step J5a:")
    block = next(block for block in _bash_blocks(lines) if "VERIFY_TIMEOUT_MS=$((" in block)
    result = subprocess.run(
        [_BASH, "-c", block],
        env={**os.environ, "VERIFY_TIMEOUT_SEC": seconds},
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert f"DOCKER_TOOL_TIMEOUT_MS={(int(seconds) + 30) * 1000}" in result.stdout
    assert any("marker is a floor" in line for line in lines)


def test_route_defaults_and_sweep_approval_reuse_are_explicit() -> None:
    """Unknown compute stays local and sweep cannot silently approve changed commands."""
    lines = _section("## Step J4:", "## Step J5a:")
    assert any("local, absent or invalid" in line for line in lines)
    assert any("none|bridge" in line and "unknown" in line and "none" in line for line in lines)
    assert any("SANDBOX_NETWORK" in line and "ambient" in line for line in lines)
    assert any("byte-identical" in line and "sweep" in line for line in lines)
    sweep = (_PLUGIN / "skills/sweep/SKILL.md").read_text(encoding="utf-8").splitlines()
    assert not any("Non-interactive end-to-end" in line or "sweep non-interactive except" in line for line in sweep)
    assert any("J4" in line and "not overridden" in line for line in sweep)
    assert any("byte-identical" in line and "network" in line and "command" in line for line in sweep)


def test_snapshot_streams_large_file_hashes(dirty_repo: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    """Hashing a large untracked file never allocates its complete contents through read_bytes."""
    repo, run_dir = dirty_repo
    payload = b"data" * (1024 * 1024)
    (repo / "large.bin").write_bytes(payload)
    module = importlib.import_module("judge_tree_snapshot")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(Path, "read_bytes", lambda self: pytest.fail("whole-file allocation"))
    assert module.main([str(run_dir), "before"]) == 0
    snapshot = json.loads((run_dir / "dirty-before.json").read_text(encoding="utf-8"))
    assert snapshot["files"]["large.bin"]["sha256"] == hashlib.sha256(payload).hexdigest()

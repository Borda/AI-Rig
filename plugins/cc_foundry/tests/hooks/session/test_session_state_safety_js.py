"""Exercise session hook trust boundaries with real filesystem state and Node processes."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from _hook_env import _symlinks_supported

HOOKS = Path(os.environ.get("FOUNDRY_SAFETY_HOOKS", Path(__file__).resolve().parents[3] / "hooks"))
_node = pytest.mark.skipif(shutil.which("node") is None, reason="requires Node")
_symlinks = pytest.mark.skipif(not _symlinks_supported(), reason="requires symlink capability")


def _filename_supported(name: str) -> bool:
    """Probe whether the host filesystem permits a hostile filename used by a regression."""
    with tempfile.TemporaryDirectory() as directory:
        try:
            (Path(directory) / name).write_text("probe", encoding="utf-8")
        except OSError:
            return False
    return True


_newline_name = pytest.mark.skipif(not _filename_supported("old\nsrc"), reason="filesystem rejects newline names")


def _invoke(hook: str, project: Path, payload: dict, home: Path) -> subprocess.CompletedProcess:
    """Run a hook with isolated home and a deadline that bounds hostile regex input."""
    return subprocess.run(
        ["node", str(HOOKS / hook)],
        input=json.dumps(payload),
        capture_output=True,
        encoding="utf-8",
        cwd=project,
        env={**os.environ, "HOME": str(home), "USERPROFILE": str(home)},
        timeout=3,
        check=True,
    )


@_node
@_symlinks
@pytest.mark.parametrize("surface", ["file", "state"])
def test_precompact_refuses_symlink_writes(tmp_path: Path, sid: str, surface: str) -> None:
    """A linked snapshot or linked state parent never changes an outside target."""
    project = tmp_path / "project"
    state = project / ".claude" / "state"
    state.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "session-context.md"
    victim.write_text("keep me", encoding="utf-8")
    link = {"file": state / "session-context.md", "state": state, "claude": project / ".claude"}[surface]
    target = {"file": victim, "state": outside, "claude": outside}[surface]
    shutil.rmtree(link, ignore_errors=True)
    link.symlink_to(target, target_is_directory=surface != "file")
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text('{"type":"tool_use","name":"Write","file_path":"src/a.py"}', encoding="utf-8")
    _invoke(
        "task-log.js",
        project,
        {"hook_event_name": "PreCompact", "session_id": sid, "transcript_path": str(transcript)},
        tmp_path,
    )
    assert victim.read_text(encoding="utf-8") == "keep me"
    assert not (outside / "state").exists()


@_node
def test_precompact_omits_unverified_contract(tmp_path: Path, sid: str) -> None:
    """Repository text without a matching session owner is absent from restored context."""
    contract_dir = tmp_path / ".temp" / "state"
    contract_dir.mkdir(parents=True)
    (contract_dir / "skill-contract.md").write_text("UNTRUSTED_BODY", encoding="utf-8")
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("{}", encoding="utf-8")
    _invoke(
        "task-log.js",
        tmp_path,
        {"hook_event_name": "PreCompact", "session_id": sid, "transcript_path": str(transcript)},
        tmp_path,
    )
    saved = (tmp_path / ".claude" / "state" / "session-context.md").read_text(encoding="utf-8")
    assert "UNTRUSTED_BODY" not in saved
    assert "Files Modified" in saved


@_node
@_symlinks
@pytest.mark.parametrize("surface", ["file", "state"])
def test_error_breadcrumb_refuses_symlinks(tmp_path: Path, sid: str, surface: str) -> None:
    """A failing handler cannot append or rotate an outside file via a state symlink."""
    state = tmp_path / ".claude" / "state"
    state.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "hook-errors.jsonl"
    original = "keep me\n" * 205
    victim.write_text(original, encoding="utf-8")
    link = state / "hook-errors.jsonl" if surface == "file" else state
    shutil.rmtree(link, ignore_errors=True)
    link.symlink_to(victim if surface == "file" else outside, target_is_directory=surface == "state")
    _invoke("task-log.js", tmp_path, {"hook_event_name": "UserPromptSubmit", "session_id": sid, "prompt": {}}, tmp_path)
    assert victim.read_text(encoding="utf-8") == original


@_node
def test_env_masking_has_bounded_runtime(tmp_path: Path, sid: str, state_dir) -> None:
    """Repeated command substitutions followed by a dangling escape cannot trigger exponential backtracking."""
    command = "X=" + "$()" * 24 + "\\"
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "session_id": sid,
        "tool_use_id": "timed",
        "tool_input": {"command": command},
    }
    _invoke("task-log.js", tmp_path, payload, tmp_path)
    marker = json.loads((state_dir(sid) / "timings" / "timed.json").read_text(encoding="utf-8"))
    assert marker["args"] == "command=" + command


@_node
@pytest.mark.parametrize("field", ["agent_id", "tool_use_id", "agent_type", "tool_name"])
def test_payload_path_components_reject_traversal(tmp_path: Path, sid: str, state_dir, field: str) -> None:
    """IDs and agent definition names cannot read, overwrite or unlink outside their state directory."""
    state = state_dir(sid)
    (state / "agents").mkdir(parents=True)
    victim = state / "victim.json"
    victim.write_text('{"start":0,"tool":"Read"}', encoding="utf-8")
    payload = {
        "hook_event_name": "SubagentStart",
        "session_id": sid,
        "agent_id": "agent",
        "agent_type": "general-purpose",
        "tool_name": "Read",
        "tool_use_id": "call",
    }
    payload[field] = "../victim"
    _invoke("task-log.js", tmp_path, payload, tmp_path)
    assert victim.read_text(encoding="utf-8") == '{"start":0,"tool":"Read"}'
    assert (state / "agents" / "agent.json").exists() is (field != "agent_id")


@_node
@pytest.mark.parametrize("future", [False, True])
def test_restore_rejects_future_and_delimits_context(tmp_path: Path, future: bool) -> None:
    """Fresh handovers identify their prior-session provenance; future handovers remain untouched."""
    store = tmp_path / ".claude" / "state" / "session"
    store.mkdir(parents=True)
    created = datetime.now(timezone.utc) + timedelta(minutes=10 if future else -1)
    doc = store / "example.md"
    doc.write_text(
        f"---\ncreated: {created.isoformat()}\nconsumed: false\nbranch: BRANCH_TEXT\n---\nBODY_TEXT", encoding="utf-8"
    )
    (store / "LATEST").write_text("example", encoding="utf-8")
    result = _invoke(
        "session-restore.js",
        tmp_path,
        {"hook_event_name": "SessionStart", "source": "clear", "cwd": str(tmp_path)},
        tmp_path,
    )
    expected = "" if future else "Handover written by a prior session in this checkout via /foundry:session dump"
    assert expected in result.stdout
    assert bool(result.stdout) is not future
    assert future or "confirm the next step with the user before acting" in result.stdout
    assert "not instructions" not in result.stdout
    assert ("consumed: false" in doc.read_text(encoding="utf-8")) is future
    assert future or re.search(
        r"<untrusted-[a-f0-9]+>.*BRANCH_TEXT.*BODY_TEXT.*</untrusted-[a-f0-9]+>", result.stdout, re.DOTALL
    )


@_node
@_symlinks
def test_statusline_clear_refuses_linked_agents(tmp_path: Path, sid: str, state_dir) -> None:
    """Clearing a session cannot unlink files through a planted agents symlink."""
    state = state_dir(sid)
    state.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "keep.json"
    victim.write_text("keep me", encoding="utf-8")
    (state / "agents").symlink_to(outside, target_is_directory=True)
    _invoke("statusline.js", tmp_path, {"session_id": sid, "context_window": {"remaining_percentage": 0}}, tmp_path)
    assert victim.read_text(encoding="utf-8") == "keep me"


@_node
def test_statusline_strips_controls_from_all_labels(tmp_path: Path, sid: str, state_dir) -> None:
    """Only renderer-owned ANSI escapes survive model, directory, skill, tool and agent labels."""
    state = state_dir(sid)
    (state / "agents").mkdir(parents=True)
    (state / "skills").mkdir()
    (state / "tools").mkdir()
    attack = "label\x1b]52;c;PAYLOAD\x07\nINJECTED"
    (state / "agents" / "a.json").write_text(json.dumps({"type": attack, "model": "inherit"}), encoding="utf-8")
    (state / "skills" / "s.json").write_text(json.dumps({"skill": attack}), encoding="utf-8")
    (state / "tools" / "t.json").write_text(
        json.dumps({"tool": attack, "since": datetime.now(timezone.utc).isoformat()}), encoding="utf-8"
    )
    result = _invoke(
        "statusline.js",
        tmp_path,
        {
            "session_id": sid,
            "model": {"display_name": attack},
            "workspace": {"current_dir": str(tmp_path / attack)},
            "effort": {"level": attack},
        },
        tmp_path,
    )
    assert "\x1b]" not in result.stdout
    assert "\x07" not in result.stdout
    assert result.stdout.count("\n") == 1
    assert "🤖" in result.stdout
    assert "⚡" in result.stdout


def _age_tree(root: Path) -> None:
    """Age a fixture and its descendants without relying on filesystem timestamp resolution."""
    old = time.time() - 40 * 24 * 60 * 60
    for entry in [*root.rglob("*"), root]:
        os.utime(entry, (old, old), follow_symlinks=False)


def _cleanup(
    root: Path, tracked: list[str] | None = None, *, fail_git: bool | str = False, payload: dict | None = None
) -> None:
    """Run the real SessionEnd handler with only the Git service boundary replaced."""
    subprocess.run(
        [
            "node",
            str(Path(__file__).with_name("_session_state_runner.js")),
            str(HOOKS / "task-log.js"),
            str(root),
            json.dumps(tracked or []),
            str(fail_git) if isinstance(fail_git, str) else "yes" if fail_git else "no",
            json.dumps(payload) if payload else "",
        ],
        check=True,
        capture_output=True,
        encoding="utf-8",
        timeout=10,
    )


@_node
@pytest.mark.parametrize("separator", [" ", pytest.param("\n", marks=_newline_name, id="newline")])
def test_cleanup_keeps_filename_boundaries(tmp_path: Path, separator: str) -> None:
    """A run name containing whitespace cannot name an unrelated deletion target."""
    review = tmp_path / ".reports" / "review"
    run = review / ("old" + separator + "src")
    run.mkdir(parents=True)
    source = tmp_path / "src"
    source.mkdir()
    (source / "keep.py").write_text("keep", encoding="utf-8")
    _age_tree(review)
    _cleanup(tmp_path)
    assert source.is_dir()
    assert not run.exists()


@_node
def test_cleanup_never_deletes_artifact_root(tmp_path: Path) -> None:
    """A root-level result file cannot cause deletion of newer experiment runs."""
    experiments = tmp_path / ".experiments"
    active = experiments / "active"
    active.mkdir(parents=True)
    result = experiments / "result.jsonl"
    result.write_text("{}", encoding="utf-8")
    _age_tree(experiments)
    (active / "fresh.txt").write_text("new work", encoding="utf-8")
    _cleanup(tmp_path)
    assert result.is_file()
    assert (active / "fresh.txt").read_text(encoding="utf-8") == "new work"


@_node
@pytest.mark.parametrize("layout", ["review/flat", "review/pr-1/run-001", "audit/run-001"])
def test_cleanup_preserves_fresh_descendants(tmp_path: Path, layout: str) -> None:
    """A run remains live when a deep file changes after its directory mtime ages out."""
    run = tmp_path / ".reports" / layout
    nested = run / "nested"
    nested.mkdir(parents=True)
    (run / "result.jsonl").write_text("{}", encoding="utf-8")
    fresh = nested / "active.txt"
    fresh.write_text("before", encoding="utf-8")
    _age_tree(tmp_path / ".reports")
    fresh.write_text("still working", encoding="utf-8")
    _cleanup(tmp_path)
    assert fresh.read_text(encoding="utf-8") == "still working"


@_node
@pytest.mark.parametrize(
    "layout",
    [
        ".cache/keep file",
        pytest.param(".temp/keep\nfile", marks=_newline_name, id="newline"),
        ".plans/blueprint/keep",
        ".reports/review/run/keep",
    ],
)
def test_cleanup_preserves_tracked_paths(tmp_path: Path, layout: str) -> None:
    """Tracked files and their parent runs survive cleanup, including whitespace in tracked names."""
    tracked = tmp_path / layout
    tracked.parent.mkdir(parents=True)
    tracked.write_text("tracked content", encoding="utf-8")
    _age_tree(tmp_path)
    _cleanup(tmp_path, [layout])
    assert tracked.read_text(encoding="utf-8") == "tracked content"


@_node
def test_cleanup_fails_closed_without_git_inventory(tmp_path: Path) -> None:
    """Cleanup preserves all candidate artifacts when the Git tracked-file query fails."""
    cache = tmp_path / ".cache"
    cache.mkdir()
    keep = cache / "keep"
    keep.write_text("keep", encoding="utf-8")
    _age_tree(cache)
    _cleanup(tmp_path, fail_git=True)
    assert keep.is_file()


@_node
def test_cleanup_preserves_tracked_directory_contents(tmp_path: Path) -> None:
    """A tracked gitlink protects submodule contents absent from the parent's Git inventory."""
    submodule = tmp_path / ".cache" / "module"
    submodule.mkdir(parents=True)
    keep = submodule / "source.py"
    keep.write_text("valuable source", encoding="utf-8")
    _age_tree(submodule)
    _cleanup(tmp_path, [".cache/module"])
    assert keep.read_text(encoding="utf-8") == "valuable source"


@_node
@pytest.mark.parametrize(
    ("tracked", "layout"),
    [
        pytest.param(".experiments", ".experiments/old", id="completed-root"),
        pytest.param(".reports/review", ".reports/review/pr-1/run-001", id="review-root"),
        pytest.param(".reports/review/pr-1", ".reports/review/pr-1/run-001", id="review-group"),
        pytest.param(".reports", ".reports/audit/old", id="artifact-ancestor"),
    ],
)
def test_cleanup_preserves_runs_inside_tracked_directories(tmp_path: Path, tracked: str, layout: str) -> None:
    """A tracked gitlink protects every nested run, even when the parent index omits its contents."""
    run = tmp_path / layout
    run.mkdir(parents=True)
    (run / "result.jsonl").write_text("{}", encoding="utf-8")
    keep = run / "source.py"
    keep.write_text("valuable source", encoding="utf-8")
    _age_tree(tmp_path)
    _cleanup(tmp_path, [tracked])
    assert keep.read_text(encoding="utf-8") == "valuable source"


@_node
@_symlinks
def test_timing_marker_refuses_symlink(tmp_path: Path, sid: str, state_dir) -> None:
    """Predictable marker filenames cannot redirect state writes into outside files."""
    timing_dir = state_dir(sid) / "timings"
    timing_dir.mkdir(parents=True)
    victim = tmp_path / "outside.json"
    victim.write_text("keep me", encoding="utf-8")
    (timing_dir / "call.json").symlink_to(victim)
    _invoke(
        "task-log.js",
        tmp_path,
        {"hook_event_name": "PreToolUse", "session_id": sid, "tool_name": "Read", "tool_use_id": "call"},
        tmp_path,
    )
    assert victim.read_text(encoding="utf-8") == "keep me"


@_node
@_symlinks
def test_cleanup_refuses_linked_artifact_ancestor(tmp_path: Path) -> None:
    """An intermediate artifact-root symlink never exposes an outside review tree to cleanup."""
    outside = tmp_path / "outside"
    run = outside / "review" / "old"
    run.mkdir(parents=True)
    keep = run / "keep"
    keep.write_text("outside work", encoding="utf-8")
    _age_tree(outside)
    (tmp_path / ".reports").symlink_to(outside, target_is_directory=True)
    _cleanup(tmp_path)
    assert keep.read_text(encoding="utf-8") == "outside work"


@_node
def test_cleanup_reaps_empty_temp_runs(tmp_path: Path) -> None:
    """Safe cleanup still removes empty depth-two temp runs without removing their roots."""
    run = tmp_path / ".temp" / "skill" / "run"
    run.mkdir(parents=True)
    _cleanup(tmp_path)
    assert not run.exists()
    assert run.parent.is_dir()


def _large_cache(root: Path) -> Path:
    """Create a subtree larger than the inventory entry budget with an early expired sibling."""
    cache = root / ".cache"
    large = cache / "large"
    large.mkdir(parents=True)
    (cache / "a-expired").write_text("old", encoding="utf-8")
    for index in range(10020):
        (large / f"entry-{index:05d}").touch()
    _age_tree(cache)
    return cache


@_node
def test_cleanup_budget_keeps_partial_progress_and_other_roots(tmp_path: Path) -> None:
    """An oversized cache preserves unscanned entries while completed runs and inventoried files expire."""
    cache = _large_cache(tmp_path)
    run = tmp_path / ".reports" / "audit" / "run"
    run.mkdir(parents=True)
    (run / "result.jsonl").write_text("{}", encoding="utf-8")
    _age_tree(run)
    _cleanup(tmp_path)
    assert not run.exists()
    assert not (cache / "a-expired").exists()
    assert (cache / "large" / "entry-10019").exists()
    errors = (tmp_path / ".claude" / "state" / "hook-errors.jsonl").read_text(encoding="utf-8")
    assert ".cache" in errors
    assert "budget" in errors


@_node
def test_cleanup_non_git_project_expires_completed_runs(tmp_path: Path) -> None:
    """Real Git exit 128 in an unversioned project still allows artifact expiration."""
    run = tmp_path / ".reports" / "audit" / "run"
    run.mkdir(parents=True)
    (run / "result.jsonl").write_text("{}", encoding="utf-8")
    _age_tree(run)
    _cleanup(tmp_path, fail_git="real")
    assert not run.exists()


@_node
@pytest.mark.parametrize("failure", ["timeout", "overflow"])
def test_cleanup_git_resource_failure_is_logged_and_closed(tmp_path: Path, failure: str) -> None:
    """A timed out or oversized inventory cannot authorize deletion, even with exit 128."""
    cache = tmp_path / ".cache"
    cache.mkdir()
    keep = cache / "keep"
    keep.write_text("tracked maybe", encoding="utf-8")
    _age_tree(cache)
    _cleanup(tmp_path, fail_git=failure)
    assert keep.is_file()
    error = json.loads((tmp_path / ".claude" / "state" / "hook-errors.jsonl").read_text(encoding="utf-8"))
    assert error["event"] == "SessionEnd"
    assert "Git unavailable" in error["error"]


@_node
@_symlinks
@pytest.mark.parametrize("tracked_link", [False, True])
def test_cleanup_completed_run_unlinks_only_untracked_links(tmp_path: Path, tracked_link: bool) -> None:
    """A run's untracked symlink does not preserve it or expose the link target to recursive removal."""
    run = tmp_path / ".reports" / "audit" / "run"
    run.mkdir(parents=True)
    (run / "result.jsonl").write_text("{}", encoding="utf-8")
    target = tmp_path / "outside"
    target.mkdir()
    victim = target / "keep"
    victim.write_text("valuable", encoding="utf-8")
    link = run / "link"
    link.symlink_to(target, target_is_directory=True)
    _age_tree(run)
    _cleanup(tmp_path, [".reports/audit/run/link"] if tracked_link else [])
    assert run.exists() is tracked_link
    assert victim.read_text(encoding="utf-8") == "valuable"


@_node
@_symlinks
def test_precompact_accepts_owned_linked_claude_without_chmod(tmp_path: Path, sid: str) -> None:
    """An owned dotfiles directory accepts private hook state without changing its own mode."""
    project = tmp_path / "project"
    project.mkdir()
    config = tmp_path / "config"
    config.mkdir(mode=0o755)
    before = config.stat().st_mode
    (project / ".claude").symlink_to(config, target_is_directory=True)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("{}", encoding="utf-8")
    _invoke(
        "task-log.js",
        project,
        {"hook_event_name": "PreCompact", "session_id": sid, "transcript_path": str(transcript)},
        tmp_path,
    )
    assert (config / "state" / "session-context.md").is_file()
    assert config.stat().st_mode == before


@_node
def test_error_log_preserves_claude_directory_mode(tmp_path: Path, sid: str) -> None:
    """The error path leaves the project's configuration directory permissions intact."""
    config = tmp_path / ".claude"
    config.mkdir(mode=0o755)
    before = config.stat().st_mode
    _invoke("task-log.js", tmp_path, {"hook_event_name": "UserPromptSubmit", "session_id": sid, "prompt": {}}, tmp_path)
    assert (config / "state" / "hook-errors.jsonl").is_file()
    assert config.stat().st_mode == before


@_node
@_symlinks
def test_state_write_rejection_emits_one_diagnostic(tmp_path: Path, sid: str) -> None:
    """A linked state directory produces one stderr diagnostic and leaves its target untouched."""
    config = tmp_path / ".claude"
    config.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (config / "state").symlink_to(outside, target_is_directory=True)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("{}", encoding="utf-8")
    result = _invoke(
        "task-log.js",
        tmp_path,
        {"hook_event_name": "PreCompact", "session_id": sid, "transcript_path": str(transcript)},
        tmp_path,
    )
    assert result.stderr.count("task-log:") == 1
    assert not list(outside.iterdir())


@_node
def test_invalid_identifier_keeps_logging_and_compaction(tmp_path: Path, sid: str) -> None:
    """An invalid path key skips that marker while normal logs and compaction still run."""
    _invoke(
        "task-log.js",
        tmp_path,
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Agent",
            "tool_use_id": "../escape",
            "session_id": sid,
            "tool_input": {"subagent_type": "foundry:worker"},
        },
        tmp_path,
    )
    entry = json.loads((tmp_path / ".claude" / "logs" / "invocations.jsonl").read_text(encoding="utf-8"))
    assert entry["agent"] == "foundry:worker"
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("{}", encoding="utf-8")
    _invoke(
        "task-log.js",
        tmp_path,
        {
            "hook_event_name": "PreCompact",
            "agent_id": "../escape",
            "session_id": sid,
            "transcript_path": str(transcript),
        },
        tmp_path,
    )
    assert (tmp_path / ".claude" / "state" / "session-context.md").is_file()


@_node
def test_session_end_reaps_only_old_regular_state_temporaries(tmp_path: Path) -> None:
    """Teardown removes abandoned atomic-write files but retains fresh files and other state."""
    state = tmp_path / ".claude" / "state"
    state.mkdir(parents=True)
    old = state / ".state-old"
    old.write_text("orphan", encoding="utf-8")
    permanent = state / "session-context.md"
    permanent.write_text("snapshot", encoding="utf-8")
    _age_tree(state)
    fresh = state / ".state-fresh"
    fresh.write_text("in flight", encoding="utf-8")
    directory = state / ".state-directory"
    directory.mkdir()
    _cleanup(tmp_path, payload={"hook_event_name": "SessionEnd", "agent_id": "../escape", "session_id": "cleanup"})
    assert not old.exists()
    assert fresh.is_file()
    assert permanent.read_text(encoding="utf-8") == "snapshot"
    assert directory.is_dir()


@_node
@pytest.mark.parametrize(
    "label", ["👩\u200d💻", "\u0646\u06cc\u0645\u200c\u0641\u0627\u0635\u0644\u0647", "a\u200bb\ufeffc"]
)
def test_statusline_preserves_joiners_and_removes_invisible_separators(tmp_path: Path, sid: str, label: str) -> None:
    """Unicode joiners survive rendering while zero-width space and BOM disappear without added spaces."""
    result = _invoke("statusline.js", tmp_path, {"session_id": sid, "model": {"display_name": label}}, tmp_path)
    assert label.replace("\u200b", "").replace("\ufeff", "") in result.stdout


@_node
def test_statusline_replaces_bidi_and_terminal_controls(tmp_path: Path, sid: str) -> None:
    """Control and bidi override characters become spaces rather than joining labels or reaching the terminal."""
    label = "a\x01b\x85c\u202ad\u202ee\u2066f\u2069g\u061ch"
    result = _invoke("statusline.js", tmp_path, {"session_id": sid, "model": {"display_name": label}}, tmp_path)
    assert "a b c d e f g h" in result.stdout

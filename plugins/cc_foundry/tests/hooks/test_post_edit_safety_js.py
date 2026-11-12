"""Exercise edit hooks against untrusted repository files and shared filesystem state."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from _hook_env import _symlinks_supported

HOOKS = Path(__file__).resolve().parents[2] / "hooks"
_node = pytest.mark.skipif(shutil.which("node") is None, reason="requires Node")
_symlinks = pytest.mark.skipif(not _symlinks_supported(), reason="requires symlink capability")


def _invoke(hook: str, project: Path, payload: dict, home: Path) -> subprocess.CompletedProcess:
    """Run the real hook in a private project and home without inherited lint consent."""
    env = {key: value for key, value in os.environ.items() if key not in {"FOUNDRY_LINT_CONFIG", "CLAUDE_CONFIG_DIR"}}
    return subprocess.run(
        ["node", str(HOOKS / hook)],
        input=json.dumps(payload),
        capture_output=True,
        encoding="utf-8",
        cwd=project,
        env={**env, "HOME": str(home), "USERPROFILE": str(home)},
        timeout=5,
        check=False,
    )


def _lint(
    project: Path, temp: Path, config: Path | None = None, setup: str = "", *, session_id: str | None = "s"
) -> tuple[subprocess.CompletedProcess, list]:
    """Keep hook filesystem behavior real and intercept only its external pre-commit launch."""
    record = temp / "spawn.json"
    script = (
        r"""
const fs = require('fs'), os = require('os'), cp = require('child_process');
const { EventEmitter } = require('events');
const { PassThrough } = require('stream');
os.tmpdir = () => process.argv[2];
cp.spawn = (command, args, options) => {
  fs.writeFileSync(process.argv[3], JSON.stringify([command, args, options.cwd]));
  const child = new EventEmitter();
  child.stdout = new PassThrough(); child.stderr = new PassThrough();
  child.kill = () => true;
  process.nextTick(() => child.emit('close', 0, null));
  return child;
};
"""
        + setup
        + "\nrequire(process.argv[1]);"
    )
    env = {key: value for key, value in os.environ.items() if key != "FOUNDRY_LINT_CONFIG"}
    if config is not None:
        env["FOUNDRY_LINT_CONFIG"] = str(config)
    result = subprocess.run(
        ["node", "-e", script, str(HOOKS / "lint-on-save.js"), str(temp), str(record)],
        input=json.dumps(
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "Edit",
                "session_id": session_id,
                "tool_input": {"file_path": str(project / "file.py")},
            }
        ),
        capture_output=True,
        encoding="utf-8",
        cwd=project,
        env=env,
        timeout=5,
        check=False,
    )
    return result, json.loads(record.read_text(encoding="utf-8")) if record.exists() else []


@pytest.fixture(name="lint_project")
def _lint_project(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Provide a repository config whose mere existence must never authorize execution."""
    project = tmp_path / "project"
    project.mkdir()
    temp = tmp_path / "temp"
    temp.mkdir()
    config = project / ".pre-commit-config.yaml"
    config.write_text("repos: []\n", encoding="utf-8")
    (project / "file.py").write_text("x = 1\n", encoding="utf-8")
    return project, temp, config


@_node
def test_missing_session_does_not_share_lint_deduplication(lint_project: tuple[Path, Path, Path]) -> None:
    """Calls without session identities retain checks rather than sharing a default dedup lock."""
    project, temp, config = lint_project
    first, first_spawn = _lint(project, temp, config, session_id=None)
    assert first.returncode == 0
    assert first_spawn
    (temp / "spawn.json").unlink()
    second, second_spawn = _lint(project, temp, config, session_id=None)
    assert second.returncode == 0
    assert second_spawn


@_node
def test_lint_requires_explicit_process_consent(lint_project: tuple[Path, Path, Path]) -> None:
    """A repository-controlled pre-commit configuration cannot start a child process by default."""
    project, temp, _ = lint_project
    result, spawned = _lint(project, temp)
    assert result.returncode == 0
    assert spawned == []


@_node
def test_lint_explicit_config_runs_with_private_locks(lint_project: tuple[Path, Path, Path]) -> None:
    """Explicit consent enables the selected config and creates private lock state."""
    project, temp, config = lint_project
    result, spawned = _lint(project, temp, config)
    assert result.returncode == 0
    assert spawned == [
        "pre-commit",
        ["run", "--config", str(config.resolve()), "--files", str(project / "file.py")],
        str(project.resolve()),
    ]
    dirs = list(temp.iterdir())
    private = next(p for p in dirs if p.is_dir())
    assert private.name.startswith("claude-precommit-")
    assert os.name == "nt" or stat.S_IMODE(private.stat().st_mode) == 0o700
    assert len(list(private.glob("file-*.lock"))) == 1
    assert not list(private.glob("project-*.lock"))


@_node
@_symlinks
def test_lint_refuses_linked_private_directory(lint_project: tuple[Path, Path, Path]) -> None:
    """A planted private-directory symlink cannot redirect lock writes."""
    project, temp, config = lint_project
    outside = temp / "outside"
    outside.mkdir()
    setup = "const uid = typeof process.getuid === 'function' ? process.getuid() : require('crypto').createHash('sha256').update(os.homedir()).digest('hex'); fs.symlinkSync(process.argv[2] + '/outside', process.argv[2] + '/claude-precommit-' + uid, 'dir');"
    result, spawned = _lint(project, temp, config, setup)
    assert result.returncode == 0
    assert spawned == []
    assert list(outside.iterdir()) == []


@_node
@_symlinks
def test_lint_refuses_stale_lock_symlink(lint_project: tuple[Path, Path, Path]) -> None:
    """Stale recovery never follows or replaces a symlink lock."""
    project, temp, config = lint_project
    victim = temp / "victim"
    victim.write_text("preserve", encoding="utf-8")
    os.utime(victim, (1, 1))
    setup = r"""
const crypto = require('crypto'), path = require('path');
const uid = typeof process.getuid === 'function' ? process.getuid() : crypto.createHash('sha256').update(os.homedir()).digest('hex');
const dir = path.join(os.tmpdir(), 'claude-precommit-' + uid);
fs.mkdirSync(dir, {mode: 0o700});
const name = 'project-' + crypto.createHash('sha256').update(fs.realpathSync(process.cwd())).digest('hex') + '.lock';
fs.symlinkSync(path.join(os.tmpdir(), 'victim'), path.join(dir, name));
"""
    result, spawned = _lint(project, temp, config, setup)
    assert result.returncode == 0
    assert spawned == []
    assert victim.read_text(encoding="utf-8") == "preserve"


@_node
@pytest.mark.parametrize("lock_kind", ["file", "project"])
def test_lint_stale_lock_recovery_uses_exclusive_create(lint_project: tuple[Path, Path, Path], lock_kind: str) -> None:
    """Stale regular locks are removed and reacquired with wx, never overwritten."""
    project, temp, config = lint_project
    digest = hashlib.sha256(str(project.resolve()).encode()).hexdigest()
    setup = r"""
const crypto = require('crypto'), path = require('path');
const uid = typeof process.getuid === 'function' ? process.getuid() : crypto.createHash('sha256').update(os.homedir()).digest('hex');
const dir = path.join(os.tmpdir(), 'claude-precommit-' + uid);
fs.mkdirSync(dir, {mode: 0o700});
const root = fs.realpathSync(process.cwd());
const kind = KIND;
const key = kind === 'project' ? root : root + '\0s\0' + path.join(root, 'file.py');
const lock = path.join(dir, kind + '-' + crypto.createHash('sha256').update(key).digest('hex') + '.lock');
fs.writeFileSync(lock, 'stale', {mode: 0o600}); fs.utimesSync(lock, 1, 1);
const originalOpen = fs.openSync;
fs.openSync = (filename, flags, ...args) => {
  if (filename === lock && flags !== 'wx') throw new Error('nonexclusive recovery');
  return originalOpen(filename, flags, ...args);
};
""".replace("KIND", json.dumps(lock_kind))
    result, spawned = _lint(project, temp, config, setup)
    assert result.returncode == 0
    assert spawned[0] == "pre-commit"
    private = next(p for p in temp.iterdir() if p.is_dir())
    assert not (private / f"project-{digest}.lock").exists()
    assert next(private.glob("file-*.lock")).read_text(encoding="utf-8") != "stale"


@_node
@pytest.mark.parametrize("event", ["PreToolUse", "PostToolUse"])
def test_md_compress_only_changes_completed_edits(tmp_path: Path, event: str) -> None:
    """Normalization follows an authorized edit and emits no permission decision or updated input."""
    source = tmp_path / "report.md"
    original = "| A    | B    |  \n\n\nend  \n"
    source.write_text(original, encoding="utf-8")
    source.chmod(0o640)
    result = _invoke(
        "md-compress.js",
        tmp_path,
        {
            "hook_event_name": event,
            "tool_name": "Edit",
            "tool_input": {"file_path": str(source), "old_string": "| A    | B    |"},
        },
        tmp_path,
    )
    assert result.returncode == 0
    assert result.stdout == ""
    assert source.read_text(encoding="utf-8") == (original if event == "PreToolUse" else "| A | B |\n\nend\n")
    assert os.name == "nt" or stat.S_IMODE(source.stat().st_mode) == 0o640


@_node
@_symlinks
@pytest.mark.parametrize("kind", ["file", "ancestor"])
def test_md_compress_refuses_symlinks(tmp_path: Path, kind: str) -> None:
    """Neither a linked document nor its linked parent changes an outside target or the original symlink."""
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "report.md"
    original = "| A    | B    |  \n"
    victim.write_text(original, encoding="utf-8")
    link = project / ("report.md" if kind == "file" else "linked")
    link.symlink_to(victim if kind == "file" else outside, target_is_directory=kind == "ancestor")
    source = link if kind == "file" else link / "report.md"
    result = _invoke(
        "md-compress.js",
        project,
        {"hook_event_name": "PostToolUse", "tool_name": "Edit", "tool_input": {"file_path": str(source)}},
        tmp_path,
    )
    assert result.returncode == 0
    assert result.stdout == ""
    assert link.is_symlink()
    assert victim.read_text(encoding="utf-8") == original


@_node
def test_md_compress_refuses_outside_project(tmp_path: Path) -> None:
    """Absolute paths outside the project are never normalized by the post-edit hook."""
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "outside.md"
    original = "| A    | B    |  \n"
    source.write_text(original, encoding="utf-8")
    result = _invoke(
        "md-compress.js",
        project,
        {"hook_event_name": "PostToolUse", "tool_name": "Edit", "tool_input": {"file_path": str(source)}},
        tmp_path,
    )
    assert result.returncode == 0
    assert source.read_text(encoding="utf-8") == original


@_node
def test_teammate_reads_documented_home_task_list(tmp_path: Path) -> None:
    """Pending tasks in the user's team directory prevent idle regardless of the current workspace."""
    project = tmp_path / "project"
    project.mkdir()
    tasks = tmp_path / ".claude" / "tasks" / "example"
    tasks.mkdir(parents=True)
    (tasks / "1.json").write_text('{"id":"1","subject":"Finish review","status":"pending"}', encoding="utf-8")
    result = _invoke(
        "teammate-quality.js", project, {"hook_event_name": "TeammateIdle", "team_name": "example"}, tmp_path
    )
    assert result.returncode == 2
    assert "1: Finish review" in result.stderr


@_node
@pytest.mark.parametrize("name", [".", "..", "../escape", "..\\escape", ""])
def test_teammate_rejects_unsafe_team_names(tmp_path: Path, name: str) -> None:
    """Unsafe or missing names produce a diagnostic without traversing the task tree."""
    result = _invoke("teammate-quality.js", tmp_path, {"hook_event_name": "TeammateIdle", "team_name": name}, tmp_path)
    assert result.returncode == 0
    assert "team_name" in result.stderr


@_node
@pytest.mark.parametrize("fault", ["mode", "owner", "create"])
def test_lint_never_runs_without_safe_lock_storage(lint_project: tuple[Path, Path, Path], fault: str) -> None:
    """Unsafe permissions, foreign ownership, and failed exclusive creation all skip pre-commit."""
    project, temp, config = lint_project
    setup = r"""
const fault = FAULT;
Object.defineProperty(process, 'platform', {value: 'linux'});
process.getuid = () => 12345;
const originalStat = fs.lstatSync;
fs.lstatSync = (filename, ...args) => {
  const value = originalStat(filename, ...args);
  value.uid = fault === 'owner' ? 54321 : 12345;
  if (value.isDirectory()) value.mode = fault === 'mode' ? 0o40777 : 0o40700;
  return value;
};
const originalOpen = fs.openSync;
fs.openSync = (filename, ...args) => {
  if (fault === 'create' && String(filename).endsWith('.lock')) {
    const error = new Error('denied'); error.code = 'EACCES'; throw error;
  }
  return originalOpen(filename, ...args);
};
""".replace("FAULT", json.dumps(fault))
    result, spawned = _lint(project, temp, config, setup)
    assert result.returncode == 0
    assert spawned == []


@_node
@pytest.mark.parametrize("selection", ["relative", "directory", "missing"])
def test_lint_rejects_invalid_config_selection(lint_project: tuple[Path, Path, Path], selection: str) -> None:
    """Consent names an existing regular absolute config file, never a directory or relative discovery path."""
    project, temp, _ = lint_project
    selected = {"relative": Path(".pre-commit-config.yaml"), "directory": project, "missing": project / "missing"}[
        selection
    ]
    result, spawned = _lint(project, temp, selected)
    assert result.returncode == 0
    assert spawned == []


@_node
def test_lint_deduplicates_repeated_events(lint_project: tuple[Path, Path, Path]) -> None:
    """A second event in the same session does not spawn while its deduplication lock is fresh."""
    project, temp, config = lint_project
    first, first_spawn = _lint(project, temp, config)
    (temp / "spawn.json").unlink()
    second, second_spawn = _lint(project, temp, config)
    assert first.returncode == second.returncode == 0
    assert first_spawn[0] == "pre-commit"
    assert second_spawn == []


@_node
@pytest.mark.parametrize("tool", ["Read", "Write"])
def test_md_compress_ignores_other_tools(tmp_path: Path, tool: str) -> None:
    """Reading and writing documents never invokes Edit-only normalization."""
    source = tmp_path / "report.md"
    original = "| A    | B    |  \n"
    source.write_text(original, encoding="utf-8")
    result = _invoke(
        "md-compress.js",
        tmp_path,
        {"hook_event_name": "PostToolUse", "tool_name": tool, "tool_input": {"file_path": str(source)}},
        tmp_path,
    )
    assert result.returncode == 0
    assert result.stdout == ""
    assert source.read_text(encoding="utf-8") == original


@_node
def test_md_compress_preserves_fenced_content(tmp_path: Path) -> None:
    """Post-edit compression preserves fenced examples while normalizing surrounding prose."""
    source = tmp_path / "report.md"
    original = "| A    | B |  \n````md\n| C    | D |  \n```\n\n\n````\nend  \n"
    source.write_text(original, encoding="utf-8")
    result = _invoke(
        "md-compress.js",
        tmp_path,
        {"hook_event_name": "PostToolUse", "tool_name": "Edit", "tool_input": {"file_path": str(source)}},
        tmp_path,
    )
    assert result.returncode == 0
    assert source.read_text(encoding="utf-8") == "| A | B |\n````md\n| C    | D |  \n```\n\n\n````\nend\n"


@_node
@pytest.mark.parametrize("event", ["TeammateIdle", "TaskCompleted"])
def test_teammate_diagnoses_only_missing_idle_team(tmp_path: Path, event: str) -> None:
    """Missing deprecated payload fields are observable on idle and never reject completed tasks."""
    result = _invoke("teammate-quality.js", tmp_path, {"hook_event_name": event}, tmp_path)
    assert result.returncode == 0
    assert ("team_name" in result.stderr) is (event == "TeammateIdle")

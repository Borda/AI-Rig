"""Exercise setup commands against disposable homes and preserve user-owned content on failure."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from _hook_env import _bash_runs_posix_script, _symlinks_supported

_PLUGIN = Path(__file__).resolve().parents[2]
_SKILL = _PLUGIN / "skills/setup/SKILL.md"
_SHELL = pytest.mark.skipif(not _bash_runs_posix_script(), reason="requires a working Bash")
_LINK = pytest.mark.skipif(not _symlinks_supported(), reason="host cannot create symlinks")
_SHELLS = [shell for shell in ("bash", "zsh") if shutil.which(shell)]


def _blocks(heading: str) -> list[str]:
    """Read Bash fences from one setup section without depending on prose wrapping."""
    lines = _SKILL.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(heading))
    blocks: list[list[str]] = []
    inside = False
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        if line == "```bash" and not inside:
            blocks.append([])
            inside = True
        elif line == "```" and inside:
            inside = False
        elif inside:
            blocks[-1].append(line)
    return ["\n".join(block) for block in blocks]


@pytest.fixture(params=_SHELLS)
def setup_home(tmp_path: Path, request: pytest.FixtureRequest) -> dict[str, str]:
    """Prepare settings, plugin sources and session files isolated from the real home."""
    (tmp_path / ".claude/rules").mkdir(parents=True)
    (tmp_path / ".claude/settings.json").write_text('{"theme":"dark"}', encoding="utf-8")
    installed = tmp_path / ".claude/plugins/cache/borda-ai-rig/foundry/1.0.0"
    (installed / ".claude-plugin").mkdir(parents=True)
    (installed / ".claude-plugin/plugin.json").write_text('{"name":"foundry"}', encoding="utf-8")
    (installed / "hooks").mkdir()
    (installed / "hooks/statusline.js").write_text("", encoding="utf-8")
    (installed / "bin").mkdir()
    canonical = _PLUGIN.parent / "cc_oss/bin/parse-skill-flags.py"
    parser = canonical if canonical.is_file() else _PLUGIN / "bin/parse-skill-flags.py"
    shutil.copyfile(parser, installed / "bin/parse-skill-flags.py")
    (tmp_path / ".claude/plugins/installed_plugins.json").write_text(
        json.dumps({"plugins": {"foundry@borda-ai-rig": [{"installPath": installed.as_posix()}]}}), encoding="utf-8"
    )
    (tmp_path / "project").mkdir()
    (tmp_path / "setup-plugin-root-preserve").write_text(_PLUGIN.as_posix() + "\n", encoding="utf-8")
    (tmp_path / "foundry-setup-approve-preserve").write_text("true\n", encoding="utf-8")
    return {
        **os.environ,
        "HOME": tmp_path.as_posix(),
        "USERPROFILE": str(tmp_path),
        "TMPDIR": tmp_path.as_posix(),
        "CLAUDE_PLUGIN_ROOT": _PLUGIN.as_posix(),
        "CLAUDE_CODE_SESSION_ID": "preserve",
        "CSID": "preserve",
        "ARGUMENTS": "--approve",
        "SETUP_TEST_SHELL": request.param,
    }


def _run(env: dict[str, str], block: str, prefix: str = "") -> subprocess.CompletedProcess[str]:
    """Run the actual skill command with the project's Python in a fresh shell."""
    python = f'python() {{ "{Path(sys.executable).as_posix()}" "$@"; }}\n'
    return subprocess.run(
        [env["SETUP_TEST_SHELL"], "-c", python + prefix + block],
        env=env,
        cwd=Path(env["HOME"]) / "project",
        encoding="utf-8",
        capture_output=True,
        timeout=30,
        check=False,
    )


def _apply_hook_filter(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Select the shipped apply mode and run the same classifier in a fresh shell."""
    blocks = _blocks("## Step 3:")
    selected = _run(env, blocks[2])
    assert selected.returncode == 0, selected.stderr
    return _run(env, blocks[1])


def test_hook_cleanup_uses_the_scoped_settings_helper() -> None:
    """Setup delegates hook classification to its guarded helper instead of importing private writers."""
    lines = _blocks("## Step 3:")[1].splitlines()
    command = next(line for line in lines if "filter-foundry-hooks" in line)
    assert 'settings_json.py" filter-foundry-hooks ~/.claude/settings.json' in command
    assert '--root "$PLUGIN_ROOT" "$HOOK_FILTER_MODE"' in command
    assert not any("from settings_json import" in line for line in lines)


def test_statusline_merge_uses_the_ownership_probe() -> None:
    """Setup checks executed hook ownership before choosing whether a stale status line can be replaced."""
    blocks = _blocks("## Step 4:")
    command = next(line for block in blocks for line in block.splitlines() if "statusline-owned" in line)
    assert 'settings_json.py" statusline-owned ~/.claude/settings.json --root "$PLUGIN_ROOT"' in command


@pytest.mark.integration
@_SHELL
def test_permissions_guide_creates_project_directory(setup_home: dict[str, str]) -> None:
    """A fresh repository gets the guide even before its .claude directory exists."""
    project = Path(setup_home["HOME"]) / "project"
    (project / ".git").mkdir()
    result = _run(setup_home, _blocks("## Step 6:")[-1])
    assert result.returncode == 0, result.stderr
    assert (project / ".claude/permissions-guide.md").read_bytes() == (_PLUGIN / "permissions-guide.md").read_bytes()
    assert "copied:" in result.stdout


@pytest.mark.integration
@_SHELL
def test_permissions_guide_failure_never_reports_copied(setup_home: dict[str, str]) -> None:
    """A destination blocked by a regular file stops setup without a success line."""
    project = Path(setup_home["HOME"]) / "project"
    (project / ".git").mkdir()
    (project / ".claude").write_text("keep", encoding="utf-8")
    result = _run(setup_home, _blocks("## Step 6:")[-1])
    assert result.returncode != 0
    assert "copied:" not in result.stdout
    assert (project / ".claude").read_text(encoding="utf-8") == "keep"


@pytest.mark.integration
@_SHELL
def test_permissions_guide_outside_repository_is_skipped(setup_home: dict[str, str]) -> None:
    """Global setup skips its project-only guide when invoked outside a repository."""
    result = _run(setup_home, "\n".join(_blocks("## Step 6:")))
    assert result.returncode == 0, result.stderr
    assert "skip" in result.stdout.lower()
    assert not (Path(setup_home["HOME"]) / "project/.claude").exists()


@pytest.mark.integration
@_SHELL
@pytest.mark.parametrize("index", [0, 1, 2, 3])
def test_claude_blocks_require_resolved_root(setup_home: dict[str, str], index: int) -> None:
    """Missing root state stops each CLAUDE.md path before it offers or performs a write."""
    home = Path(setup_home["HOME"])
    (home / "setup-plugin-root-preserve").unlink()
    live = home / ".claude/CLAUDE.md"
    live.write_text("my policy", encoding="utf-8")
    result = _run(setup_home, _blocks("## Step 12:")[index])
    assert result.returncode != 0
    assert "PLUGIN_ROOT" in result.stdout
    assert live.read_text(encoding="utf-8") == "my policy"
    assert not list(live.parent.glob("CLAUDE.md.bak-*"))


@pytest.mark.integration
@_SHELL
def test_claude_replacement_stops_when_backup_fails(setup_home: dict[str, str]) -> None:
    """An unavailable backup leaves the live policy unchanged and never prints wrote."""
    live = Path(setup_home["HOME"]) / ".claude/CLAUDE.md"
    live.write_text("my policy", encoding="utf-8")
    prefix = 'cp() { case "$2" in *.bak-*) return 1 ;; *) command cp "$@" ;; esac; }\n'
    result = _run(setup_home, _blocks("## Step 12:")[2], prefix)
    assert result.returncode != 0
    assert live.read_text(encoding="utf-8") == "my policy"
    assert "wrote:" not in result.stdout


@pytest.mark.integration
@_SHELL
@pytest.mark.parametrize("index", [1, 2, 3])
def test_claude_write_failure_is_reported(setup_home: dict[str, str], index: int) -> None:
    """Every CLAUDE.md copy propagates a failed write without a success message."""
    live = Path(setup_home["HOME"]) / ".claude/CLAUDE.md"
    live.write_text("my policy", encoding="utf-8")
    result = _run(setup_home, _blocks("## Step 12:")[index], "cp() { return 1; }\n")
    assert result.returncode != 0
    assert "wrote:" not in result.stdout
    assert live.read_text(encoding="utf-8") == "my policy"


@pytest.mark.integration
@_SHELL
def test_claude_explicit_replace_keeps_backup(setup_home: dict[str, str]) -> None:
    """An explicitly selected replacement records the old policy before applying the shipped file."""
    live = Path(setup_home["HOME"]) / ".claude/CLAUDE.md"
    live.write_text("my policy", encoding="utf-8")
    result = _run(setup_home, _blocks("## Step 12:")[2])
    assert result.returncode == 0, result.stderr
    backups = list(live.parent.glob("CLAUDE.md.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "my policy"
    assert live.read_bytes() == (_PLUGIN / "CLAUDE.src.md").read_bytes()


@pytest.mark.integration
@_SHELL
@pytest.mark.parametrize("skip", [True, False])
def test_link_conflicts_are_preserved_or_backed_up(setup_home: dict[str, str], skip: bool) -> None:
    """Keeping conflicts preserves content; explicit replace saves a backup before linking."""
    home = Path(setup_home["HOME"])
    live = home / ".claude/TEAM_PROTOCOL.md"
    live.write_text("my protocol", encoding="utf-8")
    blocks = _blocks("## Step 10:")
    scan = _run(setup_home, blocks[2])
    assert scan.returncode == 0, scan.stderr
    (home / "foundry-setup-approve-preserve").write_text("false\n", encoding="utf-8")
    (home / "foundry-setup-replace-all-preserve").write_text(str(not skip).lower() + "\n", encoding="utf-8")
    (home / "foundry-setup-skip-conflicts-preserve").write_text(str(skip).lower() + "\n", encoding="utf-8")
    result = _run(setup_home, blocks[3])
    assert result.returncode == 0, result.stderr
    saved = live if skip else next(live.parent.glob("TEAM_PROTOCOL.md.bak-*"))
    assert saved.read_text(encoding="utf-8") == "my protocol"


@pytest.mark.integration
@_SHELL
@_LINK
def test_skip_conflicts_preserves_foreign_symlink(setup_home: dict[str, str]) -> None:
    """Skip all includes a symlink to the user's own protocol, not just regular files."""
    home = Path(setup_home["HOME"])
    original = home / "my-protocol.md"
    original.write_text("my protocol", encoding="utf-8")
    live = home / ".claude/TEAM_PROTOCOL.md"
    live.symlink_to(original)
    blocks = _blocks("## Step 10:")
    assert _run(setup_home, blocks[2]).returncode == 0
    (home / "foundry-setup-skip-conflicts-preserve").write_text("true\n", encoding="utf-8")
    result = _run(setup_home, blocks[3])
    assert result.returncode == 0, result.stderr
    assert live.resolve() == original


@pytest.mark.integration
@_SHELL
def test_link_backup_failure_keeps_live_file(setup_home: dict[str, str]) -> None:
    """A denied protocol backup must not unlink or overwrite the live file."""
    home = Path(setup_home["HOME"])
    live = home / ".claude/TEAM_PROTOCOL.md"
    live.write_text("my protocol", encoding="utf-8")
    blocks = _blocks("## Step 10:")
    assert _run(setup_home, blocks[2]).returncode == 0
    (home / "foundry-setup-approve-preserve").write_text("false\n", encoding="utf-8")
    (home / "foundry-setup-replace-all-preserve").write_text("true\n", encoding="utf-8")
    result = _run(setup_home, blocks[3], "cp() { return 1; }\n")
    assert result.returncode != 0
    assert live.read_text(encoding="utf-8") == "my protocol"


@pytest.mark.integration
@_SHELL
def test_tracked_copy_replacement_archives_sidecar(setup_home: dict[str, str]) -> None:
    """Explicit replacement of a prior fallback copy archives its provenance before creating a new link."""
    home = Path(setup_home["HOME"])
    live = home / ".claude/TEAM_PROTOCOL.md"
    live.write_text("previous copy", encoding="utf-8")
    sidecar = live.parent / ".TEAM_PROTOCOL.md.sourced_from"
    sidecar.write_text("previous/version/TEAM_PROTOCOL.md\n", encoding="utf-8")
    blocks = _blocks("## Step 10:")
    assert _run(setup_home, blocks[2]).returncode == 0
    (home / "foundry-setup-approve-preserve").write_text("false\n", encoding="utf-8")
    (home / "foundry-setup-replace-all-preserve").write_text("true\n", encoding="utf-8")
    result = _run(setup_home, blocks[3])
    assert result.returncode == 0, result.stderr
    archived = list(live.parent.glob(".TEAM_PROTOCOL.md.sourced_from.bak-*"))
    assert len(archived) == 1
    assert archived[0].read_text(encoding="utf-8") == "previous/version/TEAM_PROTOCOL.md\n"
    assert live.read_bytes() == (_PLUGIN / "TEAM_PROTOCOL.md").read_bytes()


@pytest.mark.integration
@_SHELL
def test_setup_does_not_create_global_python_shim(setup_home: dict[str, str]) -> None:
    """Python3-only bootstrap never overwrites a pre-existing user shim."""
    home = Path(setup_home["HOME"])
    shim = home / ".local/bin/python"
    shim.parent.mkdir(parents=True)
    shim.write_text("my shim", encoding="utf-8")
    (home / "project/.git").mkdir()
    prefix = (
        "python() { return 127; }; py() { return 127; }\n"
        f'python3() {{ "{Path(sys.executable).as_posix()}" "$@"; }}\n'
    )
    result = _run(setup_home, _blocks("## Setup block")[0], prefix)
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "! BLOCKED — later steps call bare python" in result.stdout
    assert shim.read_text(encoding="utf-8") == "my shim"


@pytest.mark.integration
@_SHELL
def test_setup_settings_backup_failure_stops(setup_home: dict[str, str]) -> None:
    """Setup cannot continue after its initial settings backup failed."""
    (Path(setup_home["HOME"]) / "project/.git").mkdir()
    result = _run(setup_home, _blocks("## Setup block")[0], "cp() { return 1; }\n")
    assert result.returncode != 0
    assert not (Path(setup_home["HOME"]) / "foundry-setup-bak-ts-preserve").exists()


@pytest.mark.integration
@_SHELL
def test_global_setup_runs_outside_repository(setup_home: dict[str, str]) -> None:
    """Global setup reaches a real settings backup without any project repository."""
    result = _run(setup_home, _blocks("## Setup block")[0])
    assert result.returncode == 0, (result.stdout, result.stderr)
    backups = list((Path(setup_home["HOME"]) / ".claude").glob("settings.json.bak-*"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8")) == {"theme": "dark"}


@pytest.mark.integration
@_SHELL
def test_hook_cleanup_preserves_mixed_user_hooks(setup_home: dict[str, str]) -> None:
    """Removing stale foundry entries retains user commands and shared matcher metadata."""
    settings = Path(setup_home["HOME"]) / ".claude/settings.json"
    user = {"type": "command", "command": "node /my/hooks/task-log.js"}
    foundry = {"type": "command", "command": f'node "{_PLUGIN.as_posix()}/hooks/task-log.js"'}
    settings.write_text(
        json.dumps({"theme": "dark", "hooks": {"Stop": [{"matcher": "*", "hooks": [foundry, user]}]}}),
        encoding="utf-8",
    )
    result = _apply_hook_filter(setup_home)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "removed: 1, kept: 1"
    assert json.loads(settings.read_text(encoding="utf-8")) == {
        "theme": "dark",
        "hooks": {"Stop": [{"matcher": "*", "hooks": [user]}]},
    }


@pytest.mark.integration
@_SHELL
@pytest.mark.parametrize("executable", ["echo", "echo node"])
def test_hook_cleanup_preserves_hook_path_in_user_argument(setup_home: dict[str, str], executable: str) -> None:
    """A foundry path passed to an unrelated executable does not establish hook ownership."""
    settings = Path(setup_home["HOME"]) / ".claude/settings.json"
    user = {"type": "command", "command": f'{executable} "{_PLUGIN.as_posix()}/hooks/task-log.js"'}
    document = {"hooks": {"Stop": [{"hooks": [user]}]}}
    settings.write_text(json.dumps(document), encoding="utf-8")
    result = _apply_hook_filter(setup_home)
    assert result.returncode == 0, result.stderr
    assert json.loads(settings.read_text(encoding="utf-8")) == document


@pytest.mark.integration
@_SHELL
def test_hook_cleanup_preserves_foreign_cache_like_path(setup_home: dict[str, str]) -> None:
    """A cache-shaped path outside the installed foundry lineage is user-owned."""
    settings = Path(setup_home["HOME"]) / ".claude/settings.json"
    foreign = Path(setup_home["HOME"]) / "dotfiles/plugins/cache/other/foundry/1.0/hooks/task-log.js"
    user = {"type": "command", "command": f'node "{foreign.as_posix()}"'}
    document = {"hooks": {"Stop": [{"hooks": [user]}]}}
    settings.write_text(json.dumps(document), encoding="utf-8")
    result = _apply_hook_filter(setup_home)
    assert result.returncode == 0, result.stderr
    assert json.loads(settings.read_text(encoding="utf-8")) == document


@pytest.mark.integration
@_SHELL
def test_absent_bridge_is_never_enabled(setup_home: dict[str, str]) -> None:
    """No bridge installation means its settings entry is not created even if the writer is invoked."""
    settings = Path(setup_home["HOME"]) / ".claude/settings.json"
    original = settings.read_bytes()
    result = _run(setup_home, _blocks("## Step 7:")[1])
    assert result.returncode == 0, result.stderr
    assert settings.read_bytes() == original


@pytest.mark.parametrize(
    ("heading", "branch", "kept"),
    [
        pytest.param("## Step 12:", "`diverged`:", "option (a)", id="global-policy-kept"),
        pytest.param("## Step 10:", "`APPROVE_ALL=true`:", "option b", id="protocol-conflicts-kept"),
        pytest.param("## Step 4:", "`APPROVE_ALL=true`:", "keep", id="custom-statusline-kept"),
        pytest.param("## Step 7:", "`APPROVE_ALL=true`:", "skip", id="bridge-opt-in-required"),
        pytest.param("## Step 8:", "`APPROVE_ALL=true`:", "keep", id="project-model-opt-in-required"),
    ],
)
def test_noninteractive_setup_preserves_user_choices(heading: str, branch: str, kept: str) -> None:
    """The noninteractive branch must select preservation rather than overwrite or scope promotion."""
    lines = _SKILL.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(heading))
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    policy = next(line for line in lines[start:end] if line.startswith(branch))
    assert kept in policy.lower()


@pytest.mark.integration
@_SHELL
def test_purge_unsubstituted_count_stops_before_shell_redirection(setup_home: dict[str, str]) -> None:
    """An unfilled count is rejected explicitly instead of parsed as an input redirect."""
    result = _run(setup_home, _blocks("## Step 11:")[1])
    assert result.returncode != 0
    assert "placeholder" in result.stdout.lower()
    assert "syntax error" not in result.stderr


@pytest.mark.integration
@_SHELL
def test_setup_blocks_when_only_python3_is_available(setup_home: dict[str, str]) -> None:
    """A successful Python3 probe cannot hide the missing bare interpreter used by later steps."""
    setup_home["PATH"] = os.pathsep.join(
        part for part in setup_home["PATH"].split(os.pathsep) if Path(part).resolve() != _PLUGIN / "bin"
    )
    prefix = (
        "python() { return 127; }; py() { return 127; }\n"
        f'python3() {{ "{Path(sys.executable).as_posix()}" "$@"; }}\n'
    )
    result = _run(setup_home, _blocks("## Setup block")[0], prefix)
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "! BLOCKED — later steps call bare python; python3 works but python does not" in result.stdout
    assert "shipped bin/python launcher" in result.stdout
    assert "pending" not in result.stdout
    assert not list((Path(setup_home["HOME"]) / ".claude").glob("settings.json.bak-*"))


@pytest.mark.integration
@_SHELL
@pytest.mark.parametrize(
    ("approve", "replace"),
    [
        pytest.param(True, False, id="approve-without-choice"),
        pytest.param(True, True, id="approve-overrides-replace"),
        pytest.param(False, False, id="interactive-without-choice"),
    ],
)
def test_link_conflicts_require_explicit_replacement(setup_home: dict[str, str], approve: bool, replace: bool) -> None:
    """Approve mode and an unanswered interactive gate both preserve listed user files without backups."""
    home = Path(setup_home["HOME"])
    live = home / ".claude/TEAM_PROTOCOL.md"
    live.write_text("my protocol", encoding="utf-8")
    rule = home / ".claude/rules/foundry-markdown.md"
    rule.write_text("my rules", encoding="utf-8")
    (home / "foundry-setup-approve-preserve").write_text(str(approve).lower() + "\n", encoding="utf-8")
    replacement = home / "foundry-setup-replace-all-preserve"
    replacement.write_text("true\n", encoding="utf-8")
    blocks = _blocks("## Step 10:")
    assert _run(setup_home, blocks[2]).returncode == 0
    assert replacement.read_text(encoding="utf-8") == "false\n"
    replacement.write_text(str(replace).lower() + "\n", encoding="utf-8")
    (home / "foundry-setup-skip-conflicts-preserve").unlink()
    result = _run(setup_home, blocks[3])
    assert result.returncode == 0, result.stderr
    assert live.read_text(encoding="utf-8") == "my protocol"
    assert rule.read_text(encoding="utf-8") == "my rules"
    assert not list(live.parent.rglob("*.bak-*"))


@pytest.mark.integration
@_SHELL
@pytest.mark.parametrize("command", ["node $HOME/.claude/hooks/task-log.js", "node ~/.claude/hooks/task-log.js"])
def test_hook_cleanup_removes_legacy_hook_paths(setup_home: dict[str, str], command: str) -> None:
    """Legacy hooks with shipped basenames are removed after expanding home syntax."""
    settings = Path(setup_home["HOME"]) / ".claude/settings.json"
    settings.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"command": command}]}]}}), encoding="utf-8")
    result = _apply_hook_filter(setup_home)
    assert result.returncode == 0, result.stderr
    assert json.loads(settings.read_text(encoding="utf-8")) == {}


@pytest.mark.integration
@_SHELL
@pytest.mark.parametrize("command", ['echo "a\\"b"', "echo don't"])
def test_hook_cleanup_keeps_unparsable_user_command(setup_home: dict[str, str], command: str) -> None:
    """An unparsable user command survives while its adjacent stale foundry command is removed."""
    settings = Path(setup_home["HOME"]) / ".claude/settings.json"
    user = {"command": command}
    stale = {"command": f'node "{_PLUGIN.as_posix()}/hooks/task-log.js"'}
    settings.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [stale, user]}]}}), encoding="utf-8")
    result = _apply_hook_filter(setup_home)
    assert result.returncode == 0, result.stderr
    assert json.loads(settings.read_text(encoding="utf-8")) == {"hooks": {"Stop": [{"hooks": [user]}]}}


@pytest.mark.integration
@_SHELL
def test_hook_cleanup_skips_non_mapping_hooks(setup_home: dict[str, str]) -> None:
    """A non-mapping hooks value is reported and preserved without failing setup."""
    settings = Path(setup_home["HOME"]) / ".claude/settings.json"
    settings.write_text('{"hooks": []}', encoding="utf-8")
    result = _apply_hook_filter(setup_home)
    assert result.returncode == 0, result.stderr
    assert settings.read_text(encoding="utf-8") == '{"hooks": []}'
    assert "skipped" in result.stdout


@pytest.mark.integration
@_SHELL
def test_hook_cleanup_preserves_non_mapping_entries(setup_home: dict[str, str]) -> None:
    """Unexpected groups and hook entries survive alongside removal of a known stale command."""
    settings = Path(setup_home["HOME"]) / ".claude/settings.json"
    stale = {"command": f'node "{_PLUGIN.as_posix()}/hooks/task-log.js"'}
    settings.write_text(json.dumps({"hooks": {"Stop": [None, {"hooks": [None, stale]}]}}), encoding="utf-8")
    result = _apply_hook_filter(setup_home)
    assert result.returncode == 0, result.stderr
    assert json.loads(settings.read_text(encoding="utf-8")) == {"hooks": {"Stop": [None, {"hooks": [None]}]}}


@pytest.mark.integration
@_SHELL
def test_hook_cleanup_preserves_groups_without_hooks(setup_home: dict[str, str]) -> None:
    """Opaque groups retain their exact fields and count as user-owned during cleanup."""
    settings = Path(setup_home["HOME"]) / ".claude/settings.json"
    opaque = {"matcher": "*", "custom": 1}
    stale = {"command": f'node "{_PLUGIN.as_posix()}/hooks/task-log.js"'}
    settings.write_text(json.dumps({"hooks": {"Stop": [opaque, {"hooks": [stale]}]}}), encoding="utf-8")
    result = _apply_hook_filter(setup_home)
    assert result.returncode == 0, result.stderr
    assert json.loads(settings.read_text(encoding="utf-8")) == {"hooks": {"Stop": [opaque]}}
    assert result.stdout.strip() == "removed: 1, kept: 1"


@pytest.mark.integration
@_SHELL
def test_hook_filter_dry_run_reports_counts_without_writing(setup_home: dict[str, str]) -> None:
    """Classification produces the counts needed by the approval gate without changing settings."""
    settings = Path(setup_home["HOME"]) / ".claude/settings.json"
    stale = {"command": f'node "{_PLUGIN.as_posix()}/hooks/task-log.js"'}
    user = {"command": "echo keep"}
    settings.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [stale, user]}]}}), encoding="utf-8")
    original = settings.read_bytes()
    (Path(setup_home["HOME"]) / "foundry-setup-hooks-mode-preserve").write_text("--apply\n", encoding="utf-8")
    assert _run(setup_home, _blocks("## Step 3:")[0]).returncode == 0
    result = _run(setup_home, _blocks("## Step 3:")[1])
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "foundry: 1, user: 1"
    assert settings.read_bytes() == original

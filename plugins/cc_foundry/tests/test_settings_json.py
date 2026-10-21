"""Exercise setup's JSON operations and its shipped Bash commands without jq."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import settings_json
from _hook_env import _bash_runs_posix_script

PLUGIN = Path(__file__).resolve().parents[1]


def _can_symlink() -> bool:
    """Probe symlink capability so Windows without link privileges still runs other tests."""
    with tempfile.TemporaryDirectory() as directory:
        try:
            (Path(directory) / "link").symlink_to(Path(directory) / "target")
        except OSError:
            return False
    return True


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Provide an existing settings document inside the permitted working directory."""
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "settings.json"
    path.write_text('{"theme":"dark"}\n', encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("operation", "data", "options", "expected"),
    [
        pytest.param("has-hooks", {"hooks": None}, [], 0, id="hooks-present-even-null"),
        pytest.param("has-hooks", {}, [], 1, id="hooks-absent"),
        pytest.param(
            "statusline-current",
            {"statusLine": {"command": "node cache/v2/statusline.js"}},
            ["--root", "cache/v2"],
            0,
            id="current-statusline",
        ),
        pytest.param(
            "statusline-current",
            {"statusLine": {"command": "node cache/v1/statusline.js"}},
            ["--root", "cache/v2"],
            1,
            id="stale-statusline",
        ),
        pytest.param(
            "statusline-current",
            {"statusLine": {"command": "node cache/v2/other.js"}},
            ["--root", "cache/v2"],
            1,
            id="wrong-script",
        ),
        pytest.param(
            "statusline-current", {"statusLine": {"command": False}}, ["--root", "cache/v2"], 1, id="nonstring-command"
        ),
        pytest.param("plugin-enabled", {"enabledPlugins": {"bridge": True}}, ["--name", "bridge"], 0, id="plugin-true"),
        pytest.param(
            "plugin-enabled", {"enabledPlugins": {"bridge": 1}}, ["--name", "bridge"], 1, id="plugin-number-not-true"
        ),
        pytest.param("plugin-enabled", {}, ["--name", "bridge"], 1, id="plugin-absent"),
        pytest.param("advisor-matches", {"advisorModel": "chosen"}, ["--model", "chosen"], 0, id="advisor-equal"),
        pytest.param("advisor-matches", {"advisorModel": "old"}, ["--model", "chosen"], 1, id="advisor-different"),
        pytest.param("advisor-matches", {}, ["--model", "chosen"], 1, id="advisor-absent"),
    ],
)
def test_settings_probes(settings: Path, operation: str, data: dict, options: list[str], expected: int) -> None:
    """Probes preserve settings and distinguish presence, exact true and current paths."""
    original = json.dumps(data)
    settings.write_text(original, encoding="utf-8")
    assert settings_json.main([operation, str(settings), *options]) == expected
    assert settings.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    ("operation", "options", "data", "expected"),
    [
        pytest.param("delete-hooks", [], {"hooks": {"old": True}}, {}, id="delete-hooks"),
        pytest.param(
            "set-statusline",
            ["--command", 'node "cache v2/statusline.js"'],
            {"statusLine": {"old": True}},
            {"statusLine": {"async": True, "type": "command", "command": 'node "cache v2/statusline.js"'}},
            id="replace-statusline",
        ),
        pytest.param(
            "enable-plugin",
            ["--name", "bridge"],
            {"enabledPlugins": {"other": False}},
            {"enabledPlugins": {"other": False, "bridge": True}},
            id="enable-plugin",
        ),
        pytest.param(
            "enable-plugin",
            ["--name", "bridge"],
            {"enabledPlugins": None},
            {"enabledPlugins": {"bridge": True}},
            id="create-plugins-object",
        ),
        pytest.param(
            "set-advisor", ["--model", "chosen"], {"advisorModel": "old"}, {"advisorModel": "chosen"}, id="set-advisor"
        ),
    ],
)
def test_settings_writes_are_idempotent(
    settings: Path, operation: str, options: list[str], data: dict, expected: dict
) -> None:
    """Each writer changes its owned key only and leaves stable JSON on a rerun."""
    settings.write_text(json.dumps({"theme": "dark", **data}), encoding="utf-8")
    for _ in range(2):
        assert settings_json.main([operation, str(settings), *options]) == 0
        assert json.loads(settings.read_text(encoding="utf-8")) == {"theme": "dark", **expected}


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        pytest.param({}, "", id="missing"),
        pytest.param({"advisorModel": None}, "", id="null"),
        pytest.param({"advisorModel": False}, "", id="false"),
        pytest.param({"advisorModel": ""}, "\n", id="empty"),
        pytest.param({"advisorModel": "chosen"}, "chosen\n", id="pinned"),
        pytest.param({"advisorModel": 0}, "0\n", id="zero"),
    ],
)
def test_settings_advisor_read(settings: Path, capsys: pytest.CaptureFixture[str], data: dict, expected: str) -> None:
    """Read advisorModel with the former raw-read and null/false fallback semantics."""
    settings.write_text(json.dumps(data), encoding="utf-8")
    assert settings_json.main(["advisor-model", str(settings)]) == 0
    assert capsys.readouterr().out == expected


def test_settings_env_merge_keeps_every_existing_value(settings: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Presence, including deliberate zero and false/null values, prevents replacement."""
    existing = {"zero-string": "0", "empty": "", "false": False, "null": None, "zero": 0, "custom": "keep"}
    settings.write_text(json.dumps({"env": existing, "theme": "dark"}), encoding="utf-8")
    defaults = settings.parent / "defaults.json"
    defaults.write_text(json.dumps({**dict.fromkeys(existing, "replace"), "new": "1"}), encoding="utf-8")
    for added, present in ((1, 6), (0, 7)):
        assert settings_json.main(["merge-env", str(settings), "--defaults", str(defaults)]) == 0
        assert capsys.readouterr().out == f"  env: {added} default(s) added ({present} already set)\n"
        assert json.loads(settings.read_text(encoding="utf-8")) == {"env": {**existing, "new": "1"}, "theme": "dark"}


@pytest.mark.parametrize("env", [None, False])
def test_settings_env_creates_object(settings: Path, env: bool | None) -> None:
    """Null and false top-level env containers retain the former fallback to an empty object."""
    settings.write_text(json.dumps({"env": env}), encoding="utf-8")
    defaults = settings.parent / "defaults.json"
    defaults.write_text('{"new":"1"}', encoding="utf-8")
    assert settings_json.main(["merge-env", str(settings), "--defaults", str(defaults)]) == 0
    assert json.loads(settings.read_text(encoding="utf-8")) == {"env": {"new": "1"}}


@pytest.mark.parametrize("defaults_text", ["{", "[]", "null"])
def test_settings_bad_defaults_are_unchanged(settings: Path, defaults_text: str) -> None:
    """Invalid defaults cannot replace even a valid settings document."""
    original = settings.read_bytes()
    defaults = settings.parent / "defaults.json"
    defaults.write_text(defaults_text, encoding="utf-8")
    assert settings_json.main(["merge-env", str(settings), "--defaults", str(defaults)]) == 2
    assert settings.read_bytes() == original


@pytest.mark.skipif(not _can_symlink(), reason="host cannot create symlinks")
def test_settings_home_and_symlink(settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """HOME, rather than USERPROFILE, owns setup's target; a dotfiles symlink survives."""
    home = settings.parent / "home"
    (home / ".claude").mkdir(parents=True)
    link = home / ".claude/settings.json"
    link.symlink_to(settings)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(settings.parent / "other-home"))
    assert settings_json.main(["set-advisor", "~/.claude/settings.json", "--model", "chosen"]) == 0
    assert link.is_symlink()
    assert json.loads(settings.read_text(encoding="utf-8")) == {"theme": "dark", "advisorModel": "chosen"}


def test_settings_rejects_outside_write_root(settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A settings file outside cwd, TMPDIR and the user settings root stays untouched."""
    for key in ("HOME", "TMPDIR"):
        directory = settings.parent / key
        directory.mkdir()
        monkeypatch.setenv(key, str(directory))
    cwd = settings.parent / "project"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    original = settings.read_bytes()
    assert settings_json.main(["set-advisor", str(settings), "--model", "chosen"]) == 2
    assert settings.read_bytes() == original


@pytest.mark.parametrize("fail_replace", [False, True])
def test_settings_atomic_replacement(settings: Path, monkeypatch: pytest.MonkeyPatch, fail_replace: bool) -> None:
    """Replacement sees complete JSON; failures leave exact bytes and no private temp file."""
    original = settings.read_bytes()
    settings.chmod(0o640)
    mode = stat.S_IMODE(settings.stat().st_mode)
    foreign_temp = settings.with_suffix(".json.tmp")
    foreign_temp.write_text("foreign", encoding="utf-8")
    replace = os.replace
    observed = []

    def inspect_replace(source: Path, destination: Path) -> None:
        """Check the filesystem boundary before allowing or rejecting the real rename."""
        assert source.parent == settings.parent
        assert source != foreign_temp
        assert destination == settings.resolve()
        assert settings.read_bytes() == original
        assert json.loads(source.read_text(encoding="utf-8")) == {"theme": "dark", "advisorModel": "chosen"}
        observed.append(source)
        if fail_replace:
            raise OSError("rename refused")
        replace(source, destination)

    monkeypatch.setattr(settings_json.os, "replace", inspect_replace)
    assert settings_json.main(["set-advisor", str(settings), "--model", "chosen"]) == (2 if fail_replace else 0)
    assert len(observed) == 1
    assert not observed[0].exists()
    assert foreign_temp.read_text(encoding="utf-8") == "foreign"
    assert stat.S_IMODE(settings.stat().st_mode) == mode
    if fail_replace:
        assert settings.read_bytes() == original
    else:
        assert json.loads(settings.read_text(encoding="utf-8"))["advisorModel"] == "chosen"


@pytest.mark.parametrize("document", ["{", "[]", '{"env":[]}', '{"env":{"bad":NaN}}'])
def test_settings_bad_input_is_unchanged(settings: Path, document: str) -> None:
    """Malformed settings and incompatible env containers cannot cause partial writes."""
    settings.write_text(document, encoding="utf-8")
    defaults = settings.parent / "defaults.json"
    defaults.write_text('{"new":"1"}', encoding="utf-8")
    assert settings_json.main(["merge-env", str(settings), "--defaults", str(defaults)]) == 2
    assert settings.read_text(encoding="utf-8") == document
    assert not list(settings.parent.glob(".settings.json.*.tmp"))


@pytest.mark.integration
@pytest.mark.skipif(not _bash_runs_posix_script(), reason="setup requires a working Bash")
@pytest.mark.parametrize(
    ("step", "index", "message"),
    [
        pytest.param(3, 1, "stripping hooks", id="hooks-failure"),
        pytest.param(4, 1, "updating statusLine", id="statusline-failure"),
        pytest.param(7, 1, "updating enabledPlugins", id="plugins-failure"),
        pytest.param(7, 2, "merging env defaults", id="env-failure"),
        pytest.param(8, 2, "updating advisorModel", id="advisor-failure"),
    ],
)
def test_setup_settings_failure_messages(tmp_path: Path, step: int, index: int, message: str) -> None:
    """Every shipped writer keeps the established failure line and the original bytes."""
    config = tmp_path / ".claude/settings.json"
    config.parent.mkdir()
    config.write_bytes(b"{invalid")
    (tmp_path / "setup-plugin-root-settings-failure").write_text(PLUGIN.as_posix() + "\n", encoding="utf-8")
    (tmp_path / "foundry-setup-adv-settings-failure").write_text("chosen\n", encoding="utf-8")
    text = (PLUGIN / "skills/setup/SKILL.md").read_text(encoding="utf-8")
    section = text.split(f"## Step {step}:", 1)[1].split("\n## Step ", 1)[0]
    block = re.findall(r"```bash\n(.*?)\n```", section, re.DOTALL)[index]
    prefix = f'python() {{ "{Path(sys.executable).as_posix()}" "$@"; }}\n'
    result = subprocess.run(
        ["bash", "-c", prefix + block],
        env={
            **os.environ,
            "HOME": tmp_path.as_posix(),
            "USERPROFILE": str(tmp_path),
            "TMPDIR": tmp_path.as_posix(),
            "CLAUDE_PLUGIN_ROOT": PLUGIN.as_posix(),
            "CLAUDE_CODE_SESSION_ID": "settings-failure",
        },
        capture_output=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert result.stdout == f"! failed {message} — settings.json unchanged\n"
    assert config.read_bytes() == b"{invalid"


@pytest.mark.integration
@pytest.mark.skipif(not _bash_runs_posix_script(), reason="setup requires a working Bash")
def test_setup_json_blocks_without_jq(tmp_path: Path) -> None:
    """Real setup commands preserve user settings and become skip-ready on the second run."""
    home = tmp_path / "home"
    settings = home / ".claude/settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps({"hooks": {}, "env": {"CLAUDE_CODE_ENABLE_TODO_TOOLS": "0"}, "theme": "dark"}), encoding="utf-8"
    )
    installed = home / ".claude/plugins/cache/foundry"
    shutil.copytree(PLUGIN / ".claude-plugin", installed / ".claude-plugin")
    (installed / "bin").mkdir()
    for script in ("settings_json.py", "merge_settings_permissions.py"):
        shutil.copy2(PLUGIN / "bin" / script, installed / "bin" / script)
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    (project / ".claude").mkdir()
    (project / ".claude/settings.json").write_text('{"advisorModel":"chosen-advisor"}', encoding="utf-8")
    (tmp_path / "setup-plugin-root-settings-test").write_text(installed.as_posix() + "\n", encoding="utf-8")
    env = {
        **os.environ,
        "HOME": home.as_posix(),
        "USERPROFILE": str(home),
        "TMPDIR": tmp_path.as_posix(),
        "CLAUDE_PLUGIN_ROOT": installed.as_posix(),
        "CLAUDE_CODE_SESSION_ID": "settings-test",
    }
    prefix = f"""python() {{ "{Path(sys.executable).as_posix()}" "$@"; }}
jq() {{ printf called >> "$TMPDIR/jq-called"; return 127; }}
"""
    text = (PLUGIN / "skills/setup/SKILL.md").read_text(encoding="utf-8")
    sections = {}
    for step in range(3, 10):
        section = text.split(f"## Step {step}:", 1)[1].split("\n## Step ", 1)[0]
        sections[step] = re.findall(r"```bash\n(.*?)\n```", section, re.DOTALL)

    def run(block: str, expected: int = 0) -> str:
        """Run an unmodified shipped block in a fresh shell, as the host does."""
        result = subprocess.run(
            ["bash", "-c", prefix + block], cwd=project, env=env, capture_output=True, encoding="utf-8", timeout=30
        )
        assert not (tmp_path / "jq-called").exists(), "setup invoked jq"
        assert result.returncode == expected, (block, result.stdout, result.stderr)
        return result.stdout

    first_run = None
    for rerun in (False, True):
        run(sections[3][0], 1 if rerun else 0)
        if not rerun:
            run(sections[3][1])
        run(sections[4][0], 0 if rerun else 1)
        if not rerun:
            run(sections[4][1])
        run(sections[5][0])
        for block in sections[6]:
            run(block)
        run(sections[7][0], 0 if rerun else 1)
        if not rerun:
            run(sections[7][1])
        run(sections[7][2])
        assert "ADV=chosen-advisor" in run(sections[8][0])
        run(sections[8][1], 0 if rerun else 1)
        if not rerun:
            run(sections[8][2])
        run(sections[9][0])
        data = json.loads(settings.read_text(encoding="utf-8"))
        assert "hooks" not in data
        assert data["theme"] == "dark"
        assert data["env"]["CLAUDE_CODE_ENABLE_TODO_TOOLS"] == "0"
        defaults = json.loads((PLUGIN / ".claude-plugin/env-defaults.json").read_text(encoding="utf-8"))
        assert data["env"] == {**defaults, "CLAUDE_CODE_ENABLE_TODO_TOOLS": "0"}
        assert data["enabledPlugins"]["bridge@borda-ai-rig"] is True
        assert data["advisorModel"] == "chosen-advisor"
        assert data["statusLine"] == {
            "async": True,
            "type": "command",
            "command": f'node "{installed.as_posix()}/hooks/statusline.js"',
        }
        if rerun:
            assert data == first_run
        first_run = data

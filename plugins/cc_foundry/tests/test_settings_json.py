"""Exercise setup's JSON operations and its shipped Bash commands without jq."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
import settings_json
from _hook_env import _bash_runs_posix_script, _symlinks_supported

PLUGIN = Path(__file__).resolve().parents[1]
_SETUP_SKILL = PLUGIN / "skills/setup/SKILL.md"


_skip_no_symlink = pytest.mark.skipif(not _symlinks_supported(), reason="host cannot create symlinks")
_skip_no_bash = pytest.mark.skipif(not _bash_runs_posix_script(), reason="setup requires a working Bash")


def _setup_step_blocks(step: int) -> list[str]:
    """Return the bash fences of ``## Step <step>:`` in the setup skill, one string per fence.

    Reads the skill line by line, so wrapping or reflowing prose between the fences cannot change the result.
    """
    blocks: list[list[str]] = []
    lines = _SETUP_SKILL.read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith(f"## Step {step}:"))
    inside = False
    for line in lines[start + 1 :]:
        if line.startswith("## Step "):
            break
        if not inside and line == "```bash":
            inside = True
            blocks.append([])
        elif inside and line == "```":
            inside = False
        elif inside:
            blocks[-1].append(line)
    return ["\n".join(block) for block in blocks]


def _summarize_setup(run: dict) -> dict:
    """Project the settings fields setup owns, plus the advisor probe flag, into one comparable dict."""
    data = run["data"]
    return {
        "advisor_seen": run["advisor_seen"],
        "theme": data["theme"],
        "hooks_present": "hooks" in data,
        "env": data["env"],
        "bridge_enabled": data["enabledPlugins"]["bridge@borda-ai-rig"] is True,
        "advisor": data["advisorModel"],
        "statusline": data["statusLine"],
    }


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Provide an existing settings document inside the permitted working directory."""
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "settings.json"
    path.write_text('{"theme":"dark"}\n', encoding="utf-8")
    return path


@pytest.fixture
def defaults(settings: Path) -> Path:
    """Provide an env-defaults document next to the settings file, holding one new default ``new=1``."""
    path = settings.parent / "defaults.json"
    path.write_text('{"new":"1"}', encoding="utf-8")
    return path


class TestProbes:
    """Read-only probes answer by exit code and never rewrite the settings file."""

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
                "statusline-current",
                {"statusLine": {"command": False}},
                ["--root", "cache/v2"],
                1,
                id="nonstring-command",
            ),
            pytest.param(
                "plugin-enabled", {"enabledPlugins": {"bridge": True}}, ["--name", "bridge"], 0, id="plugin-true"
            ),
            pytest.param(
                "plugin-enabled",
                {"enabledPlugins": {"bridge": 1}},
                ["--name", "bridge"],
                1,
                id="plugin-number-not-true",
            ),
            pytest.param("plugin-enabled", {}, ["--name", "bridge"], 1, id="plugin-absent"),
            pytest.param("advisor-matches", {"advisorModel": "chosen"}, ["--model", "chosen"], 0, id="advisor-equal"),
            pytest.param("advisor-matches", {"advisorModel": "old"}, ["--model", "chosen"], 1, id="advisor-different"),
            pytest.param("advisor-matches", {}, ["--model", "chosen"], 1, id="advisor-absent"),
        ],
    )
    def test_exit_code_reflects_settings(
        self, settings: Path, operation: str, data: dict, options: list[str], expected: int
    ) -> None:
        """Probes preserve settings and distinguish presence, exact true and current paths.

        Setup branches on these exit codes to decide whether a step is already done, so a probe that rewrote the file or
        accepted a truthy non-``true`` value would skip or repeat a step wrongly.
        """
        original = json.dumps(data)
        settings.write_text(original, encoding="utf-8")
        assert settings_json.main([operation, str(settings), *options]) == expected
        assert settings.read_text(encoding="utf-8") == original


class TestWriters:
    """Each writer changes its owned key only and is stable on a rerun."""

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
                "set-advisor",
                ["--model", "chosen"],
                {"advisorModel": "old"},
                {"advisorModel": "chosen"},
                id="set-advisor",
            ),
        ],
    )
    def test_are_idempotent(
        self, settings: Path, operation: str, options: list[str], data: dict, expected: dict
    ) -> None:
        """Each writer changes its owned key only and leaves stable JSON on a rerun.

        Setup may run twice on one machine; the second run must report success and leave the file byte-for-byte as the
        first run wrote it, with unrelated keys such as ``theme`` untouched.
        """
        settings.write_text(json.dumps({"theme": "dark", **data}), encoding="utf-8")
        args = [operation, str(settings), *options]
        first_code = settings_json.main(args)
        first_bytes = settings.read_bytes()
        second_code = settings_json.main(args)
        assert (first_code, second_code, settings.read_bytes()) == (0, 0, first_bytes)
        assert json.loads(first_bytes) == {"theme": "dark", **expected}


class TestAdvisorRead:
    """``advisor-model`` prints the pinned model with the former raw-read semantics."""

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
    def test_prints_pinned_model(
        self, settings: Path, capsys: pytest.CaptureFixture[str], data: dict, expected: str
    ) -> None:
        """Read advisorModel with the former raw-read and null/false fallback semantics.

        Shell callers capture this output as the model name, so a null or false value must print nothing while an
        explicit empty string or zero still prints its line.
        """
        settings.write_text(json.dumps(data), encoding="utf-8")
        assert settings_json.main(["advisor-model", str(settings)]) == 0
        assert capsys.readouterr().out == expected


class TestMergeEnv:
    """``merge-env`` adds missing defaults to ``env`` and never overwrites a present value."""

    def test_keeps_every_existing_value(
        self, settings: Path, defaults: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Presence, including deliberate zero and false/null values, prevents replacement.

        A user who set a flag to ``0``, ``false`` or an empty string made a choice; the merge reports one added default
        on the first run and none on the rerun, and the existing values stay as they were.
        """
        existing = {"zero-string": "0", "empty": "", "false": False, "null": None, "zero": 0, "custom": "keep"}
        settings.write_text(json.dumps({"env": existing, "theme": "dark"}), encoding="utf-8")
        defaults.write_text(json.dumps({**dict.fromkeys(existing, "replace"), "new": "1"}), encoding="utf-8")
        args = ["merge-env", str(settings), "--defaults", str(defaults)]
        first_code = settings_json.main(args)
        first_out = capsys.readouterr().out
        second_code = settings_json.main(args)
        second_out = capsys.readouterr().out
        assert (first_code, first_out, second_code, second_out) == (
            0,
            "  env: 1 default(s) added (6 already set)\n",
            0,
            "  env: 0 default(s) added (7 already set)\n",
        )
        assert json.loads(settings.read_text(encoding="utf-8")) == {"env": {**existing, "new": "1"}, "theme": "dark"}

    @pytest.mark.parametrize("env", [None, False])
    def test_creates_object_for_null_or_false_env(self, settings: Path, defaults: Path, env: bool | None) -> None:
        """Null and false top-level env containers retain the former fallback to an empty object."""
        settings.write_text(json.dumps({"env": env}), encoding="utf-8")
        assert settings_json.main(["merge-env", str(settings), "--defaults", str(defaults)]) == 0
        assert json.loads(settings.read_text(encoding="utf-8")) == {"env": {"new": "1"}}

    @pytest.mark.parametrize("defaults_text", ["{", "[]", "null"])
    def test_bad_defaults_leave_settings_unchanged(self, settings: Path, defaults: Path, defaults_text: str) -> None:
        """Invalid defaults cannot replace even a valid settings document."""
        original = settings.read_bytes()
        defaults.write_text(defaults_text, encoding="utf-8")
        assert settings_json.main(["merge-env", str(settings), "--defaults", str(defaults)]) == 2
        assert settings.read_bytes() == original

    @pytest.mark.parametrize("document", ["{", "[]", '{"env":[]}', '{"env":{"bad":NaN}}'])
    def test_bad_settings_are_unchanged(self, settings: Path, defaults: Path, document: str) -> None:
        """Malformed settings and incompatible env containers cannot cause partial writes."""
        settings.write_text(document, encoding="utf-8")
        assert settings_json.main(["merge-env", str(settings), "--defaults", str(defaults)]) == 2
        assert settings.read_text(encoding="utf-8") == document
        assert not list(settings.parent.glob(".settings.json.*.tmp"))


class TestWriteLocation:
    """Where a write may land: HOME-relative targets follow symlinks, anything outside the permitted roots is
    refused."""

    @_skip_no_symlink
    def test_home_not_userprofile_owns_target_and_symlink_survives(
        self, settings: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HOME, rather than USERPROFILE, owns setup's target; a dotfiles symlink survives.

        Dotfile managers link ``~/.claude/settings.json`` elsewhere; the write must update the link's target and leave
        the link itself in place.
        """
        home = settings.parent / "home"
        (home / ".claude").mkdir(parents=True)
        link = home / ".claude/settings.json"
        link.symlink_to(settings)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(settings.parent / "other-home"))
        assert settings_json.main(["set-advisor", "~/.claude/settings.json", "--model", "chosen"]) == 0
        assert link.is_symlink()
        assert json.loads(settings.read_text(encoding="utf-8")) == {"theme": "dark", "advisorModel": "chosen"}

    def test_rejects_outside_write_root(self, settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A settings file outside cwd, TMPDIR and the user settings root stays untouched.

        The write path is derived from caller input, so it must be confined to the roots setup legitimately edits.
        """
        home = settings.parent / "HOME"
        tmpdir = settings.parent / "TMPDIR"
        project = settings.parent / "project"
        home.mkdir()
        tmpdir.mkdir()
        project.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("TMPDIR", str(tmpdir))
        monkeypatch.chdir(project)
        original = settings.read_bytes()
        assert settings_json.main(["set-advisor", str(settings), "--model", "chosen"]) == 2
        assert settings.read_bytes() == original


class TestAtomicReplacement:
    """The settings file is replaced through a private temp file renamed over it, never edited in place."""

    @pytest.fixture
    def replace_spy(self, settings: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[bool], dict]:
        """Return a factory arming ``os.replace`` with a boundary check, optionally refusing the rename.

        The factory returns the observations: the original bytes, mode, a foreign temp file that must survive, and the
        list of temp sources the library tried to rename.
        """

        def arm(fail_replace: bool) -> dict:
            """Install the spy; ``fail_replace`` makes the rename raise ``OSError``."""
            original = settings.read_bytes()
            settings.chmod(0o640)
            foreign_temp = settings.with_suffix(".json.tmp")
            foreign_temp.write_text("foreign", encoding="utf-8")
            replace = os.replace
            observed: list[Path] = []

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
            return {
                "original": original,
                "mode": stat.S_IMODE(settings.stat().st_mode),
                "foreign_temp": foreign_temp,
                "observed": observed,
            }

        return arm

    @pytest.mark.parametrize(
        ("fail_replace", "exit_code", "advisor", "bytes_unchanged"),
        [
            pytest.param(False, 0, "chosen", False, id="rename-succeeds"),
            pytest.param(True, 2, None, True, id="rename-refused"),
        ],
    )
    def test_replacement_sees_complete_json(
        self,
        settings: Path,
        replace_spy: Callable[[bool], dict],
        fail_replace: bool,
        exit_code: int,
        advisor: str | None,
        bytes_unchanged: bool,
    ) -> None:
        """Replacement sees complete JSON; failures leave exact bytes and no private temp file.

        The rename is the only moment the new document becomes visible, so the temp file must hold the full result
        before it and a refused rename must leave the original bytes, the file mode and an unrelated ``.json.tmp`` file
        exactly as they were.
        """
        probe = replace_spy(fail_replace)
        assert settings_json.main(["set-advisor", str(settings), "--model", "chosen"]) == exit_code
        assert len(probe["observed"]) == 1
        assert not probe["observed"][0].exists()
        assert probe["foreign_temp"].read_text(encoding="utf-8") == "foreign"
        assert stat.S_IMODE(settings.stat().st_mode) == probe["mode"]
        assert json.loads(settings.read_text(encoding="utf-8")).get("advisorModel") == advisor
        assert (settings.read_bytes() == probe["original"]) is bytes_unchanged


@pytest.mark.integration
@_skip_no_bash
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
def test_setup_writer_failure_keeps_message_and_bytes(tmp_path: Path, step: int, index: int, message: str) -> None:
    """Every shipped writer keeps the established failure line and the original bytes.

    Each writer block of the setup skill runs for real against an unparsable settings file; the user must see the one-
    line failure naming the step and the file must stay byte-identical.
    """
    config = tmp_path / ".claude/settings.json"
    config.parent.mkdir()
    config.write_bytes(b"{invalid")
    (tmp_path / "setup-plugin-root-settings-failure").write_text(PLUGIN.as_posix() + "\n", encoding="utf-8")
    (tmp_path / "foundry-setup-adv-settings-failure").write_text("chosen\n", encoding="utf-8")
    block = _setup_step_blocks(step)[index]
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


class _SetupRunner:
    """Runs unmodified shipped setup blocks in fresh shells against an installed copy of the plugin, with jq banned."""

    def __init__(self, tmp_path: Path) -> None:
        """Install a minimal plugin copy, a project and a user settings file under ``tmp_path``."""
        self.tmp_path = tmp_path
        home = tmp_path / "home"
        self.settings = home / ".claude/settings.json"
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text(
            json.dumps({"hooks": {}, "env": {"CLAUDE_CODE_ENABLE_TODO_TOOLS": "0"}, "theme": "dark"}), encoding="utf-8"
        )
        self.installed = home / ".claude/plugins/cache/foundry"
        shutil.copytree(PLUGIN / ".claude-plugin", self.installed / ".claude-plugin")
        (self.installed / "bin").mkdir()
        for script in ("settings_json.py", "merge_settings_permissions.py"):
            shutil.copy2(PLUGIN / "bin" / script, self.installed / "bin" / script)
        self.project = tmp_path / "project"
        (self.project / ".git").mkdir(parents=True)
        (self.project / ".claude").mkdir()
        (self.project / ".claude/settings.json").write_text('{"advisorModel":"chosen-advisor"}', encoding="utf-8")
        (tmp_path / "setup-plugin-root-settings-test").write_text(self.installed.as_posix() + "\n", encoding="utf-8")
        self.env = {
            **os.environ,
            "HOME": home.as_posix(),
            "USERPROFILE": str(home),
            "TMPDIR": tmp_path.as_posix(),
            "CLAUDE_PLUGIN_ROOT": self.installed.as_posix(),
            "CLAUDE_CODE_SESSION_ID": "settings-test",
        }
        self.prefix = (
            f'python() {{ "{Path(sys.executable).as_posix()}" "$@"; }}\n'
            'jq() { printf called >> "$TMPDIR/jq-called"; return 127; }\n'
        )
        self.steps = {step: _setup_step_blocks(step) for step in range(3, 10)}

    def run(self, step: int, index: int, expected: int = 0) -> str:
        """Run block ``index`` of setup step ``step`` and return stdout; fail if jq ran or the exit code differs."""
        block = self.steps[step][index]
        result = subprocess.run(
            ["bash", "-c", self.prefix + block],
            cwd=self.project,
            env=self.env,
            capture_output=True,
            encoding="utf-8",
            timeout=30,
        )
        assert not (self.tmp_path / "jq-called").exists(), "setup invoked jq"
        assert result.returncode == expected, (block, result.stdout, result.stderr)
        return result.stdout

    def run_all(self, step: int) -> None:
        """Run every block of setup step ``step`` expecting success."""
        for index in range(len(self.steps[step])):
            self.run(step, index)

    def first_run(self) -> dict:
        """Run setup on settings that need every change, then return the advisor probe flag and resulting settings."""
        self.run(3, 0)
        self.run(3, 1)
        self.run(4, 0, 1)
        self.run(4, 1)
        self.run(5, 0)
        self.run_all(6)
        self.run(7, 0, 1)
        self.run(7, 1)
        self.run(7, 2)
        advisor_seen = "ADV=chosen-advisor" in self.run(8, 0)
        self.run(8, 1, 1)
        self.run(8, 2)
        self.run(9, 0)
        return {"advisor_seen": advisor_seen, "data": json.loads(self.settings.read_text(encoding="utf-8"))}

    def rerun(self) -> dict:
        """Run setup again on settings it already changed: the probes report skip-ready, so no writer runs."""
        self.run(3, 0, 1)
        self.run(4, 0)
        self.run(5, 0)
        self.run_all(6)
        self.run(7, 0)
        self.run(7, 2)
        advisor_seen = "ADV=chosen-advisor" in self.run(8, 0)
        self.run(8, 1)
        self.run(9, 0)
        return {"advisor_seen": advisor_seen, "data": json.loads(self.settings.read_text(encoding="utf-8"))}


@pytest.mark.integration
@_skip_no_bash
def test_setup_json_blocks_without_jq(tmp_path: Path) -> None:
    """Real setup commands preserve user settings and become skip-ready on the second run.

    The unmodified shipped bash blocks run in order against an installed plugin copy where ``jq`` is a trap: the first
    run must apply every change, keep the user's own keys, and the second run must find nothing left to do and leave the
    same document.
    """
    runner = _SetupRunner(tmp_path)
    first = runner.first_run()
    second = runner.rerun()
    defaults = json.loads((PLUGIN / ".claude-plugin/env-defaults.json").read_text(encoding="utf-8"))
    expected = {
        "advisor_seen": True,
        "theme": "dark",
        "hooks_present": False,
        "env": {**defaults, "CLAUDE_CODE_ENABLE_TODO_TOOLS": "0"},
        "bridge_enabled": True,
        "advisor": "chosen-advisor",
        "statusline": {
            "async": True,
            "type": "command",
            "command": f'node "{runner.installed.as_posix()}/hooks/statusline.js"',
        },
    }
    assert (_summarize_setup(first), _summarize_setup(second), second["data"]) == (expected, expected, first["data"])

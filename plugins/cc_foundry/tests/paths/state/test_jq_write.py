"""Tests for ``bin/jq_write.py``.

Happy path uses real ``jq`` against ``tmp_path`` JSON files; failure paths mock ``subprocess.run`` to assert exit codes
without depending on jq's exact error messages.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

import jq_write
import merge_settings_permissions as msp
import pytest
from _hook_env import _symlinks_supported

_HAS_JQ = shutil.which("jq") is not None
_requires_jq = pytest.mark.skipif(not _HAS_JQ, reason="jq not installed on this host")


def _dir_names(directory: Path) -> list[str]:
    """List every entry name in ``directory``, dotfiles included, so a leftover temp file fails the comparison."""
    return sorted(entry.name for entry in directory.iterdir())


_requires_symlink = pytest.mark.skipif(not _symlinks_supported(), reason="host cannot create symlinks")


def _fake_jq(
    payload: str, temp_names: list[str] | None = None, returncode: int = 0, stderr: str = ""
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Build a ``subprocess.run`` stand-in that writes ``payload`` into the temp file jq was handed.

    Records the temp file's name in ``temp_names`` when given, so a test can inspect where and how it was named, and
    reports ``returncode`` with ``stderr`` the way jq would.
    """

    def _run(*args: Any, stdout: Any, **_kw: Any) -> subprocess.CompletedProcess[str]:
        """Emit the payload on the supplied stdout handle and report the configured result."""
        if temp_names is not None:
            temp_names.append(stdout.name)
        stdout.write(payload)
        stdout.flush()
        return subprocess.CompletedProcess(args=args, returncode=returncode, stdout="", stderr=stderr)

    return _run


def _jq_write_adds_rule(settings: Path, tmp_path: Path) -> int:
    """Append one allow rule to ``settings`` through ``jq_write``."""
    return jq_write.run_jq_write(settings, '.permissions.allow += ["Bash(x)"]', [])


def _merge_adds_rule(settings: Path, tmp_path: Path) -> int:
    """Append the same allow rule to ``settings`` through ``merge_settings_permissions``."""
    allow = tmp_path / "allow.json"
    allow.write_text('["Bash(x)"]', encoding="utf-8")
    return msp.main(["--settings", str(settings), "--allow", str(allow)])


@pytest.fixture(name="roots")
def _roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Separate cwd, TMPDIR, home and outside directories, so each write/read root is exercised alone."""
    dirs = {name: tmp_path / name for name in ("cwd", "tmp", "home", "outside")}
    for path in dirs.values():
        path.mkdir()
    (dirs["home"] / ".claude").mkdir()
    monkeypatch.chdir(dirs["cwd"])
    monkeypatch.setenv("TMPDIR", str(dirs["tmp"]))
    monkeypatch.setenv("HOME", str(dirs["home"]))
    monkeypatch.setenv("USERPROFILE", str(dirs["home"]))
    monkeypatch.setattr(msp, "_PLUGIN_ROOT", tmp_path)
    return dirs


@pytest.fixture
def json_target(tmp_path: Path) -> Callable[[object], Path]:
    """Return a factory writing ``obj.json`` under ``tmp_path`` with the given JSON value and returning its path."""

    def _make(value: object) -> Path:
        target = tmp_path / "obj.json"
        target.write_text(json.dumps(value), encoding="utf-8")
        return target

    return _make


class TestParseJqArgs:
    """``_parse_jq_args`` validates the extra jq arguments: allowed flags with their operand counts."""

    @pytest.mark.parametrize(
        "extras",
        [
            pytest.param([], id="empty"),
            pytest.param(["--arg", "k", "v"], id="one-arg-pair"),
            pytest.param(["--arg", "a", "1", "--arg", "b", "2"], id="two-arg-pairs"),
            pytest.param(["--argjson", "n", "42"], id="argjson-passthrough"),
            pytest.param(["--slurpfile", "envd", "env.json"], id="slurpfile-pair"),
            pytest.param(["--arg", "k", "-f"], id="dash-value"),
            pytest.param(["--argjson", "n", "-1"], id="negative-number"),
            pytest.param(["--arg", "k", "--slurpfile"], id="flag-spelled-value"),
            pytest.param(["--indent", "2"], id="indent"),
        ],
    )
    def test_accepts_vetted_extras_unchanged(self, extras: list[str]) -> None:
        """Allowed flags with their full operands pass through unchanged, even when an operand starts with a dash.

        Only tokens in flag position are vetted; an operand such as ``-f`` or ``-1`` is data, so it must not be mistaken
        for an option. ``--slurpfile`` is a two-operand flag like ``--arg``.
        """
        assert jq_write._parse_jq_args(extras) == extras

    @pytest.mark.parametrize(
        "extras",
        [
            pytest.param(["--arg", "k"], id="arg-missing-value"),
            pytest.param(["--arg"], id="bare-arg"),
            pytest.param(["--argjson", "n"], id="argjson-missing-value"),
            pytest.param(["--indent"], id="indent-missing-operand"),
        ],
    )
    def test_returns_none_for_missing_operand(self, extras: list[str]) -> None:
        """Every allowed flag needs its full operand count; a truncated triplet yields no parsed arguments.

        Callers turn ``None`` into the malformed-argument exit code instead of handing jq a half-formed command line.
        """
        assert jq_write._parse_jq_args(extras) is None

    @pytest.mark.parametrize(
        "extras",
        [
            pytest.param(["-f", "/etc/hosts"], id="program-file"),
            pytest.param(["-L", "/tmp/mods"], id="module-dir"),
            pytest.param(["--arg", "k", "v", "-f", "/etc/hosts"], id="after-valid-pair"),
            pytest.param(["--rawfile", "k", "/etc/hosts"], id="rawfile"),
            pytest.param(["stray"], id="stray-positional"),
        ],
    )
    def test_rejects_unlisted_token_in_flag_position(self, extras: list[str]) -> None:
        """Single-dash options and stray positionals are rejected like unknown ``--`` flags.

        ``-f`` and ``-L`` would let a caller read an arbitrary program file or module directory, so any token outside
        the allowlist raises even when it follows a valid pair.
        """
        with pytest.raises(ValueError, match="disallowed"):
            jq_write._parse_jq_args(extras)


@_requires_jq
class TestRunJqWriteHappyPath:
    """``run_jq_write`` round-trips real jq against ``tmp_path`` JSON."""

    @pytest.mark.parametrize(
        ("initial", "jq_filter", "extras", "expected"),
        [
            pytest.param({"a": 1, "b": [2, 3]}, ".", [], {"a": 1, "b": [2, 3]}, id="identity-filter"),
            pytest.param(
                {"version": "0.1.0"},
                ".version = $v",
                ["--arg", "v", "0.2.0"],
                {"version": "0.2.0"},
                id="field-from-arg",
            ),
        ],
    )
    def test_rewrites_target(
        self,
        json_target: Callable[[object], Path],
        initial: object,
        jq_filter: str,
        extras: list[str],
        expected: object,
    ) -> None:
        """The target file ends up holding the filter's output, with named arguments substituted.

        The identity filter must preserve contents, and ``--arg`` must reach jq as a variable, not be spliced into the
        program text.
        """
        target = json_target(initial)

        rc = jq_write.run_jq_write(target, jq_filter, extras)

        assert rc == 0
        assert json.loads(target.read_text(encoding="utf-8")) == expected

    def test_tmp_file_cleaned_up_on_success(self, json_target: Callable[[object], Path]) -> None:
        """The temporary output is gone after a successful write, leaving only the target in its directory.

        A leftover dotfile beside the target would accumulate with every settings write.
        """
        target = json_target({"x": 1})

        jq_write.run_jq_write(target, ".", [])

        assert _dir_names(target.parent) == ["obj.json"]


class TestRunJqWriteErrorPaths:
    """``run_jq_write`` exit-code contract for failure modes."""

    def test_missing_target_returns_1(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A missing target file exits 1 with a stderr message.

        There is nothing to rewrite, so jq must not even be started.
        """
        rc = jq_write.run_jq_write(tmp_path / "absent.json", ".", [])

        assert rc == 1
        assert "target not found" in capsys.readouterr().err

    def test_outside_allowed_roots_returns_4(self, roots: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
        """A target outside cwd and TMPDIR exits 4, distinct from the missing-target code 1.

        The file exists but lives in a directory that is neither the working tree nor the temp root, so writing it is
        refused.
        """
        target = roots["outside"] / "obj.json"
        target.write_text("{}", encoding="utf-8")

        rc = jq_write.run_jq_write(target, ".", [])

        assert rc == 4
        assert "outside allowed roots" in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("returncode", "partial", "stderr"),
        [
            pytest.param(3, "", "jq: parse error\n", id="parse-error"),
            pytest.param(1, "garbage", "", id="partial-output-left-behind"),
        ],
    )
    def test_jq_nonzero_exit_returns_2_and_cleans_up(
        self,
        json_target: Callable[[object], Path],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        returncode: int,
        partial: str,
        stderr: str,
    ) -> None:
        """A non-zero jq exit gives code 2, removes the temp file jq was writing and leaves the target untouched.

        jq may fail before writing anything or after emitting partial output; either way no stale artifact may remain
        beside the target and the user must see that the filter failed.
        """
        target = json_target({})
        monkeypatch.setattr(jq_write.subprocess, "run", _fake_jq(partial, returncode=returncode, stderr=stderr))

        rc = jq_write.run_jq_write(target, ".bad(", [])

        assert rc == 2
        assert _dir_names(target.parent) == ["obj.json"]
        assert target.read_text(encoding="utf-8") == "{}"
        assert "jq filter failed" in capsys.readouterr().err

    @mock.patch("jq_write.subprocess.run", autospec=True, side_effect=FileNotFoundError("jq"))
    def test_jq_not_installed_returns_2(
        self, mock_run: mock.MagicMock, json_target: Callable[[object], Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A jq binary absent from PATH (``FileNotFoundError``) exits 2 and leaves the directory clean.

        The temp file is created before jq is launched, so the failure to launch must still remove it.
        """
        target = json_target({})

        rc = jq_write.run_jq_write(target, ".", [])

        assert rc == 2
        assert _dir_names(target.parent) == ["obj.json"]
        assert "jq invocation failed" in capsys.readouterr().err


class TestMain:
    """``main`` CLI surface: argv parsing and exit codes."""

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param([], id="no-args"),
            pytest.param(["file.json"], id="only-target"),
        ],
    )
    def test_missing_target_or_filter_returns_3(self, capsys: pytest.CaptureFixture[str], argv: list[str]) -> None:
        """No target/filter, or a target without a filter, exits 3 with the usage line.

        The usage text is the only guidance a caller gets when a skill builds the command line wrongly.
        """
        rc = jq_write.main(argv)

        assert rc == 3
        assert "Usage:" in capsys.readouterr().err

    def test_malformed_arg_returns_3(
        self, json_target: Callable[[object], Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An ``--arg`` option missing its value exits 3 with a ``malformed --arg`` message.

        The name is given but not the value, so jq would otherwise receive a truncated command line.
        """
        target = json_target({})

        rc = jq_write.main([str(target), ".", "--arg", "name-only"])

        assert rc == 3
        assert "malformed --arg" in capsys.readouterr().err

    def test_missing_target_returns_1(self, tmp_path: Path) -> None:
        """A target file that does not exist exits 1 through the CLI as well.

        Mirrors the ``run_jq_write`` contract so callers see one code for "no such file" at either level.
        """
        rc = jq_write.main([str(tmp_path / "absent.json"), "."])

        assert rc == 1

    @_requires_jq
    @pytest.mark.parametrize(
        ("initial", "argv_tail", "expected"),
        [
            pytest.param({"k": 1}, [".k = 2"], {"k": 2}, id="filter-only"),
            pytest.param(
                {"rule": "old"},
                [".rule = $rule", "--arg", "rule", "new-value"],
                {"rule": "new-value"},
                id="arg-passthrough",
            ),
        ],
    )
    def test_end_to_end_with_real_jq(
        self, json_target: Callable[[object], Path], initial: object, argv_tail: list[str], expected: object
    ) -> None:
        """A full ``main()`` round-trip with real jq rewrites the target, including the ``--arg`` call form.

        ``<target> '<filter>' --arg rule '<rule>'`` is how skills invoke the script; the argument must reach jq
        untouched.
        """
        target = json_target(initial)

        rc = jq_write.main([str(target), *argv_tail])

        assert rc == 0
        assert json.loads(target.read_text(encoding="utf-8")) == expected

    def test_help_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        """``--help`` prints usage to stdout and exits 0 (argparse ``SystemExit``).

        Users discover the script through its help, which must not be reported as a usage error.
        """
        with pytest.raises(SystemExit) as exc:
            jq_write.main(["--help"])

        assert exc.value.code == 0
        assert "jq_write.py" in capsys.readouterr().out


class TestSettingsWriteSupport:
    """``/foundry:setup`` support: ``--slurpfile``, the ``~/.claude/*.json`` write root, and the empty-output guard.

    Approved audit decision (P3-9): setup's seven inline ``jq`` → TMPDIR → ``mv`` write-backs move onto this script,
    which needed ``--slurpfile`` and a narrow ``~/.claude`` settings root. The old "slurpfile always rejected" and
    "every ~/.claude path exits 4" behaviours are deliberately narrowed, not silently flipped.
    """

    def test_rawfile_still_rejected(self) -> None:
        """``--rawfile`` reads arbitrary bytes and stays blocked.

        Adding ``--slurpfile`` must not open the sibling flag that reads any file as a raw string.
        """
        with pytest.raises(ValueError, match="disallowed flag"):
            jq_write._parse_jq_args(["--rawfile", "k", "/etc/passwd"])

    def test_slurpfile_outside_read_roots_returns_4(self, roots: dict[str, Path]) -> None:
        """A slurped source outside cwd, TMPDIR and ``~/.claude`` exits 4 before jq runs, target untouched.

        The target is writable, but the file to be slurped lives in an unrelated directory, so reading it is refused.
        """
        target = roots["cwd"] / "obj.json"
        target.write_text("{}", encoding="utf-8")
        source = roots["outside"] / "src.json"
        source.write_text("{}", encoding="utf-8")

        rc = jq_write.run_jq_write(target, ".x = $s[0]", ["--slurpfile", "s", str(source)])

        assert rc == 4
        assert target.read_text(encoding="utf-8") == "{}"

    def test_slurpfile_under_platform_temp_accepted_when_tmpdir_unset(
        self, roots: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A slurped source under the ``TEMP``/``TMP`` directory stays readable when ``TMPDIR`` is unset.

        The sentinel fallback follows the shell's ``${TMPDIR:-/tmp}``, but the read roots must still admit the platform
        temp dir that ``tempfile.gettempdir()`` names, as the write-target check does; narrowing it broke JSON merges
        staged there.
        """
        monkeypatch.delenv("TMPDIR")
        monkeypatch.setenv("TEMP", str(roots["tmp"]))
        monkeypatch.setenv("TMP", str(roots["tmp"]))
        monkeypatch.setattr(tempfile, "tempdir", None)
        source = roots["tmp"] / "src.json"
        source.write_text("{}", encoding="utf-8")

        assert jq_write._validate_slurp_sources(["--slurpfile", "s", str(source)]) is None

    def test_plugin_cache_below_claude_returns_4(self, roots: dict[str, Path]) -> None:
        """Only files directly in ``~/.claude`` are writable; the plugin cache deeper down stays refused.

        The settings root was narrowed to ``~/.claude/*.json`` on purpose, so a nested cache file must still exit 4.
        """
        target = roots["home"] / ".claude" / "plugins" / "cache" / "hooks.json"
        target.parent.mkdir(parents=True)
        target.write_text("{}", encoding="utf-8")

        rc = jq_write.run_jq_write(target, ".", [])

        assert rc == 4

    @_requires_jq
    def test_user_settings_merge_with_slurpfile(self, roots: dict[str, Path]) -> None:
        """Setup's env-defaults merge: ``~/.claude/settings.json`` target, plugin-cache slurp source, user value kept.

        The source lives under ``~/.claude/plugins/cache`` (a readable root) while the target is the user settings file;
        an existing ``"0"`` survives the ``//=`` per-key default.
        """
        target = roots["home"] / ".claude" / "settings.json"
        target.write_text(json.dumps({"env": {"KEEP": "0"}}), encoding="utf-8")
        source = roots["home"] / ".claude" / "plugins" / "cache" / "env-defaults.json"
        source.parent.mkdir(parents=True)
        source.write_text(json.dumps({"KEEP": "1", "NEW": "1"}), encoding="utf-8")
        jq_filter = (
            ".env = ((.env // {}) as $cur | reduce ($envd[0] | to_entries[]) as $e ($cur; .[$e.key] //= $e.value))"
        )

        rc = jq_write.main([str(target), jq_filter, "--slurpfile", "envd", str(source)])

        assert rc == 0
        assert json.loads(target.read_text(encoding="utf-8")) == {"env": {"KEEP": "0", "NEW": "1"}}

    @_requires_jq
    def test_empty_output_returns_2_and_keeps_target(self, roots: dict[str, Path]) -> None:
        """A filter yielding nothing never truncates the target — the guard the shell form had via ``[ -n … ]``.

        jq's ``empty`` exits 0 with no output; installing that would replace the settings file with a zero-byte one.
        """
        target = roots["cwd"] / "obj.json"
        target.write_text('{"a": 1}', encoding="utf-8")

        rc = jq_write.run_jq_write(target, "empty", [])

        assert rc == 2
        assert target.read_text(encoding="utf-8") == '{"a": 1}'
        assert _dir_names(roots["cwd"]) == ["obj.json"]

    @pytest.mark.parametrize(
        "extras",
        [
            pytest.param(["-f", "/etc/hosts"], id="program-file"),
            pytest.param(["-L", "/tmp/mods"], id="module-dir"),
            pytest.param(["--arg", "k"], id="missing-operand"),
        ],
    )
    def test_unvetted_extras_return_3_before_jq_runs(self, roots: dict[str, Path], extras: list[str]) -> None:
        """``run_jq_write`` is public: tokens that skipped ``main`` still never reach jq's argv (exit 3).

        A direct caller bypassing the CLI parser gets the same allowlist, and the target stays untouched.
        """
        target = roots["cwd"] / "obj.json"
        target.write_text("{}", encoding="utf-8")

        rc = jq_write.run_jq_write(target, ".", extras)

        assert rc == 3
        assert target.read_text(encoding="utf-8") == "{}"

    @_requires_jq
    def test_dash_leading_filter_is_a_program_not_an_option(self, roots: dict[str, Path]) -> None:
        """A filter such as ``-.a`` (negation) runs as a program; jq must not parse it as short options.

        The filter is positioned so jq treats it as program text even though it begins with a dash.
        """
        target = roots["cwd"] / "obj.json"
        target.write_text('{"a": 1}', encoding="utf-8")

        rc = jq_write.run_jq_write(target, "-.a", [])

        assert rc == 0
        assert json.loads(target.read_text(encoding="utf-8")) == -1


class TestTempFileIsPrivate:
    """Each call writes its own temp file, so two concurrent writers cannot truncate each other's output."""

    def test_foreign_target_tmp_is_left_alone(
        self, json_target: Callable[[object], Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A file named ``<target>.tmp`` (the old fixed temp name) survives a run untouched.

        Two writers sharing that name was the collision: the second ``open("w")`` truncated the first writer's output,
        which the first then installed half-written. A file under that exact name now belongs to someone else.
        """
        target = json_target({"x": 1})
        foreign = target.parent / "obj.json.tmp"
        foreign.write_text("other writer", encoding="utf-8")
        monkeypatch.setattr(jq_write.subprocess, "run", _fake_jq('{"x": 2}\n'))

        rc = jq_write.run_jq_write(target, ".x = 2", [])

        assert rc == 0
        assert foreign.read_text(encoding="utf-8") == "other writer"
        assert json.loads(target.read_text(encoding="utf-8")) == {"x": 2}

    def test_temp_file_sits_beside_target_under_another_name(
        self, json_target: Callable[[object], Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The temp file shares the target's directory (same filesystem for the rename) but not its fixed ``.tmp`` name.

        Staying on the target's filesystem keeps the final replace atomic.
        """
        target = json_target({})
        names: list[str] = []
        monkeypatch.setattr(jq_write.subprocess, "run", _fake_jq("{}\n", names))

        rc = jq_write.run_jq_write(target, ".", [])

        assert rc == 0
        assert [Path(name).parent for name in names] == [target.parent]
        assert names[0] != str(target.parent / "obj.json.tmp")

    def test_two_calls_use_different_temp_names(
        self, json_target: Callable[[object], Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Back-to-back calls on one target never reuse a temp name, which is what keeps concurrent calls apart.

        Two calls stand in for two concurrent writers: a name collision here is the truncation race.
        """
        target = json_target({})
        names: list[str] = []
        monkeypatch.setattr(jq_write.subprocess, "run", _fake_jq("{}\n", names))

        jq_write.run_jq_write(target, ".", [])
        jq_write.run_jq_write(target, ".", [])

        assert len(set(names)) == 2

    def test_file_mode_is_kept(self, json_target: Callable[[object], Path], monkeypatch: pytest.MonkeyPatch) -> None:
        """The replace keeps the target's permission bits instead of the temp file's owner-only 0600.

        A settings file shared with a group would otherwise silently become unreadable after the next write.
        """
        target = json_target({})
        os.chmod(target, 0o640)
        before = stat.S_IMODE(target.stat().st_mode)
        monkeypatch.setattr(jq_write.subprocess, "run", _fake_jq("{}\n"))

        rc = jq_write.run_jq_write(target, ".", [])

        assert rc == 0
        assert stat.S_IMODE(target.stat().st_mode) == before


@_requires_jq
@_requires_symlink
class TestSymlinkedSettings:
    """Both settings writers write through a symlinked ``~/.claude/settings.json``, one setup run never treats it two
    ways."""

    @pytest.mark.parametrize(
        "apply_rule",
        [
            pytest.param(_jq_write_adds_rule, id="jq_write"),
            pytest.param(_merge_adds_rule, id="merge_settings_permissions"),
        ],
    )
    def test_link_survives_and_destination_is_updated(
        self, roots: dict[str, Path], tmp_path: Path, apply_rule: Callable[[Path, Path], int]
    ) -> None:
        """The link stays a link and the dotfiles copy it points at receives the change.

        The shell ``mv`` both scripts replace swapped the link for a regular file and left the dotfiles copy stale; one
        script following that old behaviour while the other writes through would split a single setup run.
        """
        dotfiles = roots["outside"] / "settings.json"
        dotfiles.write_text(json.dumps({"permissions": {"allow": []}}), encoding="utf-8")
        link = roots["home"] / ".claude" / "settings.json"
        link.symlink_to(dotfiles)

        rc = apply_rule(link, tmp_path)

        assert rc == 0
        assert link.is_symlink()
        assert json.loads(dotfiles.read_text(encoding="utf-8"))["permissions"]["allow"] == ["Bash(x)"]


@_requires_jq
class TestHomeResolution:
    """``~`` means ``$HOME`` when set (as in the setup bash), and only then the platform home directory."""

    @staticmethod
    def _decoy_home(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Make ``Path.home()`` name ``root`` with its own settings file, standing in for Windows' ``USERPROFILE``."""
        (root / ".claude").mkdir()
        settings = root / ".claude" / "settings.json"
        settings.write_text('{"k": 0}', encoding="utf-8")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: root))
        return settings

    def test_home_env_wins_over_path_home(self, roots: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
        """A ``~/.claude/settings.json`` target lands under ``$HOME`` even when ``Path.home()`` says otherwise.

        On macOS and Linux ``Path.home()`` already reads ``HOME``, so the order is only provable by patching it; on
        native Windows it reads ``USERPROFILE``, which can name a different directory than the one setup backed up.
        """
        decoy = self._decoy_home(roots["outside"], monkeypatch)
        real = roots["home"] / ".claude" / "settings.json"
        real.write_text('{"k": 0}', encoding="utf-8")

        rc = jq_write.run_jq_write(Path("~/.claude/settings.json"), ".k = 1", [])

        assert rc == 0
        assert json.loads(real.read_text(encoding="utf-8")) == {"k": 1}
        assert json.loads(decoy.read_text(encoding="utf-8")) == {"k": 0}

    def test_path_home_is_the_fallback_without_home_env(
        self, roots: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With ``HOME`` unset the platform home directory is used, so a bare environment still resolves ``~``.

        The decoy home stands in for the platform default; it is the only place the write can land.
        """
        decoy = self._decoy_home(roots["outside"], monkeypatch)
        monkeypatch.delenv("HOME")

        rc = jq_write.run_jq_write(Path("~/.claude/settings.json"), ".k = 1", [])

        assert rc == 0
        assert json.loads(decoy.read_text(encoding="utf-8")) == {"k": 1}

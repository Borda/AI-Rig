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

import jq_write
import merge_settings_permissions as msp
import pytest

_HAS_JQ = shutil.which("jq") is not None
_requires_jq = pytest.mark.skipif(not _HAS_JQ, reason="jq not installed on this host")


def _dir_names(directory: Path) -> list[str]:
    """List every entry name in ``directory``, dotfiles included, so a leftover temp file fails the comparison."""
    return sorted(entry.name for entry in directory.iterdir())


def _can_symlink() -> bool:
    """Probe whether this host lets an unprivileged process create a file symlink (native Windows often does not)."""
    probe = Path(tempfile.mkdtemp())
    try:
        (probe / "link").symlink_to(probe / "target")
    except OSError:
        return False
    finally:
        shutil.rmtree(probe, ignore_errors=True)
    return True


_requires_symlink = pytest.mark.skipif(not _can_symlink(), reason="host cannot create symlinks")


def _fake_jq(payload: str, temp_names: list[str] | None = None) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Build a ``subprocess.run`` stand-in that writes ``payload`` into the temp file jq was handed.

    Records the temp file's name in ``temp_names`` when given, so a test can inspect where and how it was named.
    """

    def _run(*args: Any, stdout: Any, **_kw: Any) -> subprocess.CompletedProcess[str]:
        """Emit the payload on the supplied stdout handle and report success."""
        if temp_names is not None:
            temp_names.append(stdout.name)
        stdout.write(payload)
        stdout.flush()
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    return _run


def _jq_write_adds_rule(settings: Path, tmp_path: Path) -> int:
    """Append one allow rule to ``settings`` through ``jq_write``."""
    return jq_write.run_jq_write(settings, '.permissions.allow += ["Bash(x)"]', [])


def _merge_adds_rule(settings: Path, tmp_path: Path) -> int:
    """Append the same allow rule to ``settings`` through ``merge_settings_permissions``."""
    allow = tmp_path / "allow.json"
    allow.write_text('["Bash(x)"]', encoding="utf-8")
    return msp.main(["--settings", str(settings), "--allow", str(allow)])


class TestParseJqArgs:
    """_parse_jq_args: triplet validation for --arg pairs."""

    def test_empty(self) -> None:
        """No extras → empty list passes through."""
        assert jq_write._parse_jq_args([]) == []

    def test_one_arg_pair(self) -> None:
        """Single ``--arg k v`` triplet validates."""
        assert jq_write._parse_jq_args(["--arg", "k", "v"]) == ["--arg", "k", "v"]

    def test_two_arg_pairs(self) -> None:
        """Multiple ``--arg`` triplets all validate."""
        extras = ["--arg", "a", "1", "--arg", "b", "2"]
        assert jq_write._parse_jq_args(extras) == extras

    def test_missing_value_returns_none(self) -> None:
        """Return no parsed argument when its value is missing."""
        assert jq_write._parse_jq_args(["--arg", "k"]) is None

    def test_missing_name_and_value_returns_none(self) -> None:
        """Trailing bare ``--arg`` → None."""
        assert jq_write._parse_jq_args(["--arg"]) is None

    def test_non_arg_token_passthrough(self) -> None:
        """Unknown tokens (e.g. ``--argjson``) pass through unchanged."""
        extras = ["--argjson", "n", "42"]
        # --argjson is an allowed flag → passthrough without ValueError.
        assert jq_write._parse_jq_args(extras) == extras

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
    def test_unlisted_token_in_flag_position_rejected(self, extras: list[str]) -> None:
        """Single-dash options and stray positionals are rejected like unknown ``--`` flags."""
        with pytest.raises(ValueError, match="disallowed"):
            jq_write._parse_jq_args(extras)

    @pytest.mark.parametrize(
        "extras",
        [
            pytest.param(["--arg", "k", "-f"], id="dash-value"),
            pytest.param(["--argjson", "n", "-1"], id="negative-number"),
            pytest.param(["--arg", "k", "--slurpfile"], id="flag-spelled-value"),
            pytest.param(["--indent", "2"], id="indent"),
        ],
    )
    def test_dash_operands_are_taken_literally(self, extras: list[str]) -> None:
        """An operand may start with a dash; only tokens in flag position are vetted."""
        assert jq_write._parse_jq_args(extras) == extras

    @pytest.mark.parametrize(
        "extras",
        [
            pytest.param(["--argjson", "n"], id="argjson-missing-value"),
            pytest.param(["--indent"], id="indent-missing-operand"),
        ],
    )
    def test_missing_operand_returns_none(self, extras: list[str]) -> None:
        """Every allowed flag needs its full operand count."""
        assert jq_write._parse_jq_args(extras) is None


@_requires_jq
class TestRunJqWriteHappyPath:
    """run_jq_write: real jq round-trip against tmp_path JSON."""

    def test_identity_filter(self, tmp_path: Path) -> None:
        """Preserve file contents when applying the identity filter."""
        target = tmp_path / "obj.json"
        target.write_text(json.dumps({"a": 1, "b": [2, 3]}), encoding="utf-8")
        rc = jq_write.run_jq_write(target, ".", [])
        assert rc == 0
        assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1, "b": [2, 3]}

    def test_field_update_with_arg(self, tmp_path: Path) -> None:
        """Substitute a named argument value into the filter."""
        target = tmp_path / "obj.json"
        target.write_text(json.dumps({"version": "0.1.0"}), encoding="utf-8")
        rc = jq_write.run_jq_write(target, ".version = $v", ["--arg", "v", "0.2.0"])
        assert rc == 0
        assert json.loads(target.read_text(encoding="utf-8")) == {"version": "0.2.0"}

    def test_tmp_file_cleaned_up_on_success(self, tmp_path: Path) -> None:
        """Remove the temporary output after a successful write."""
        target = tmp_path / "obj.json"
        target.write_text(json.dumps({"x": 1}), encoding="utf-8")
        jq_write.run_jq_write(target, ".", [])
        assert _dir_names(tmp_path) == ["obj.json"]


class TestRunJqWriteErrorPaths:
    """run_jq_write: exit-code contract for failure modes."""

    def test_missing_target_returns_1(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Missing target file → exit 1 with stderr message."""
        rc = jq_write.run_jq_write(tmp_path / "absent.json", ".", [])
        assert rc == 1
        assert "target not found" in capsys.readouterr().err

    def test_outside_allowed_roots_returns_4(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Target outside cwd and TMPDIR → exit 4 (distinct from missing-target 1)."""
        cwd = tmp_path / "cwd"
        tmpdir = tmp_path / "tmp"
        outside = tmp_path / "outside"
        for d in (cwd, tmpdir, outside):
            d.mkdir()
        target = outside / "obj.json"
        target.write_text("{}", encoding="utf-8")
        monkeypatch.chdir(cwd)
        monkeypatch.setenv("TMPDIR", str(tmpdir))
        rc = jq_write.run_jq_write(target, ".", [])
        assert rc == 4
        assert "outside allowed roots" in capsys.readouterr().err

    def test_jq_nonzero_exit_returns_2(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Jq returns non-zero → exit 2, tmp file cleaned up."""
        target = tmp_path / "obj.json"
        target.write_text("{}", encoding="utf-8")

        def _fake_run(*_args: Any, stderr: Any = None, **_kw: Any) -> subprocess.CompletedProcess[str]:
            """Return jq's configured nonzero result for this error path."""
            return subprocess.CompletedProcess(args=_args, returncode=3, stdout="", stderr="jq: parse error\n")

        monkeypatch.setattr(jq_write.subprocess, "run", _fake_run)
        rc = jq_write.run_jq_write(target, ".bad(", [])
        assert rc == 2
        assert _dir_names(tmp_path) == ["obj.json"]
        # The fake stderr from jq plus our own message both surface.
        err = capsys.readouterr().err
        assert "jq filter failed" in err

    def test_jq_not_installed_returns_2(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Jq binary absent → FileNotFoundError → exit 2."""
        target = tmp_path / "obj.json"
        target.write_text("{}", encoding="utf-8")

        def _boom(*_args: Any, **_kw: Any) -> Any:
            """Simulate jq being unavailable on PATH."""
            raise FileNotFoundError("jq")

        monkeypatch.setattr(jq_write.subprocess, "run", _boom)
        rc = jq_write.run_jq_write(target, ".", [])
        assert rc == 2
        assert _dir_names(tmp_path) == ["obj.json"]
        assert "jq invocation failed" in capsys.readouterr().err

    def test_tmp_file_cleaned_up_on_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Failure path removes the temp file jq was writing, leaving no stale artifact beside the target."""
        target = tmp_path / "obj.json"
        target.write_text("{}", encoding="utf-8")

        def _fake_run(*_args: Any, stdout: Any, **_kw: Any) -> subprocess.CompletedProcess[str]:
            """Write the partial result into the temp file jq was handed, then report jq failure."""
            stdout.write("garbage")
            stdout.flush()
            return subprocess.CompletedProcess(args=_args, returncode=1, stdout="", stderr="")

        monkeypatch.setattr(jq_write.subprocess, "run", _fake_run)
        rc = jq_write.run_jq_write(target, ".", [])
        assert rc == 2
        assert _dir_names(tmp_path) == ["obj.json"]


class TestMain:
    """Main: CLI surface — argv parsing + exit codes."""

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param([], id="no-args"),
            pytest.param(["file.json"], id="only-target"),
        ],
    )
    def test_missing_target_or_filter_returns_3(self, capsys: pytest.CaptureFixture[str], argv: list[str]) -> None:
        """No target/filter, or a target without a filter, exits 3 with the usage line."""
        rc = jq_write.main(argv)
        assert rc == 3
        assert "Usage:" in capsys.readouterr().err

    def test_malformed_arg_returns_3(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Reject an argument option missing its name or value."""
        target = tmp_path / "obj.json"
        target.write_text("{}", encoding="utf-8")
        rc = jq_write.main([str(target), ".", "--arg", "name-only"])
        assert rc == 3
        assert "malformed --arg" in capsys.readouterr().err

    def test_missing_target_returns_1(self, tmp_path: Path) -> None:
        """Target file missing → exit 1."""
        rc = jq_write.main([str(tmp_path / "absent.json"), "."])
        assert rc == 1

    @_requires_jq
    def test_happy_path_end_to_end(self, tmp_path: Path) -> None:
        """Full main() round-trip with real jq."""
        target = tmp_path / "obj.json"
        target.write_text(json.dumps({"k": 1}), encoding="utf-8")
        rc = jq_write.main([str(target), ".k = 2"])
        assert rc == 0
        assert json.loads(target.read_text(encoding="utf-8")) == {"k": 2}

    def test_help_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Print usage and exit 0 (argparse SystemExit)."""
        with pytest.raises(SystemExit) as exc:
            jq_write.main(["--help"])
        assert exc.value.code == 0
        assert "jq_write.py" in capsys.readouterr().out

    @_requires_jq
    def test_golden_arg_passthrough_invocation(self, tmp_path: Path) -> None:
        """Manage call form ``<target> '<filter>' --arg rule '<rule>'`` reaches jq untouched."""
        target = tmp_path / "settings.json"
        target.write_text(json.dumps({"rule": "old"}), encoding="utf-8")
        rc = jq_write.main([str(target), ".rule = $rule", "--arg", "rule", "new-value"])
        assert rc == 0
        assert json.loads(target.read_text(encoding="utf-8")) == {"rule": "new-value"}


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
    return dirs


class TestSettingsWriteSupport:
    """``/foundry:setup`` support: ``--slurpfile``, the ``~/.claude/*.json`` write root, and the empty-output guard.

    Approved audit decision (P3-9): setup's seven inline ``jq`` → TMPDIR → ``mv`` write-backs move onto this script,
    which needed ``--slurpfile`` and a narrow ``~/.claude`` settings root. The old "slurpfile always rejected" and
    "every ~/.claude path exits 4" behaviours are deliberately narrowed, not silently flipped.
    """

    def test_slurpfile_pair_validates(self) -> None:
        """``--slurpfile <name> <file>`` is a two-operand flag like ``--arg``."""
        assert jq_write._parse_jq_args(["--slurpfile", "envd", "env.json"]) == ["--slurpfile", "envd", "env.json"]

    def test_rawfile_still_rejected(self) -> None:
        """``--rawfile`` reads arbitrary bytes and stays blocked."""
        with pytest.raises(ValueError, match="disallowed flag"):
            jq_write._parse_jq_args(["--rawfile", "k", "/etc/passwd"])

    def test_slurpfile_outside_read_roots_returns_4(self, roots: dict[str, Path]) -> None:
        """A slurped source outside cwd, TMPDIR and ``~/.claude`` exits 4 before jq runs, target untouched."""
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
        """Only files directly in ``~/.claude`` are writable; the plugin cache deeper down stays refused."""
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
        """A filter yielding nothing never truncates the target — the guard the shell form had via ``[ -n … ]``."""
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
        """``run_jq_write`` is public: tokens that skipped ``main`` still never reach jq's argv (exit 3)."""
        target = roots["cwd"] / "obj.json"
        target.write_text("{}", encoding="utf-8")
        rc = jq_write.run_jq_write(target, ".", extras)
        assert rc == 3
        assert target.read_text(encoding="utf-8") == "{}"

    @_requires_jq
    def test_dash_leading_filter_is_a_program_not_an_option(self, roots: dict[str, Path]) -> None:
        """A filter such as ``-.a`` (negation) runs as a program; jq must not parse it as short options."""
        target = roots["cwd"] / "obj.json"
        target.write_text('{"a": 1}', encoding="utf-8")
        rc = jq_write.run_jq_write(target, "-.a", [])
        assert rc == 0
        assert json.loads(target.read_text(encoding="utf-8")) == -1


class TestTempFileIsPrivate:
    """Each call writes its own temp file, so two concurrent writers cannot truncate each other's output."""

    def test_foreign_target_tmp_is_left_alone(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A file named ``<target>.tmp`` (the old fixed temp name) survives a run untouched.

        Two writers sharing that name was the collision: the second ``open("w")`` truncated the first writer's output,
        which the first then installed half-written. A file under that exact name now belongs to someone else.
        """
        target = tmp_path / "obj.json"
        target.write_text(json.dumps({"x": 1}), encoding="utf-8")
        foreign = tmp_path / "obj.json.tmp"
        foreign.write_text("other writer", encoding="utf-8")
        monkeypatch.setattr(jq_write.subprocess, "run", _fake_jq('{"x": 2}\n'))
        rc = jq_write.run_jq_write(target, ".x = 2", [])
        assert rc == 0
        assert foreign.read_text(encoding="utf-8") == "other writer"
        assert json.loads(target.read_text(encoding="utf-8")) == {"x": 2}

    def test_temp_file_sits_beside_target_under_another_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The temp file shares the target's directory (same filesystem for the rename) but not its fixed ``.tmp``
        name."""
        target = tmp_path / "obj.json"
        target.write_text("{}", encoding="utf-8")
        names: list[str] = []
        monkeypatch.setattr(jq_write.subprocess, "run", _fake_jq("{}\n", names))
        rc = jq_write.run_jq_write(target, ".", [])
        assert rc == 0
        assert [Path(name).parent for name in names] == [tmp_path]
        assert names[0] != str(tmp_path / "obj.json.tmp")

    def test_two_calls_use_different_temp_names(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Back-to-back calls on one target never reuse a temp name, which is what keeps concurrent calls apart."""
        target = tmp_path / "obj.json"
        target.write_text("{}", encoding="utf-8")
        names: list[str] = []
        monkeypatch.setattr(jq_write.subprocess, "run", _fake_jq("{}\n", names))
        jq_write.run_jq_write(target, ".", [])
        jq_write.run_jq_write(target, ".", [])
        assert len(set(names)) == 2

    def test_file_mode_is_kept(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The replace keeps the target's permission bits instead of the temp file's owner-only 0600."""
        target = tmp_path / "obj.json"
        target.write_text("{}", encoding="utf-8")
        os.chmod(target, 0o640)
        before = stat.S_IMODE(target.stat().st_mode)
        monkeypatch.setattr(jq_write.subprocess, "run", _fake_jq("{}\n"))
        rc = jq_write.run_jq_write(target, ".", [])
        assert rc == 0
        assert stat.S_IMODE(target.stat().st_mode) == before


@_requires_jq
@_requires_symlink
class TestSymlinkedSettings:
    """Both settings writers write through a symlinked ``~/.claude/settings.json``, so one setup run never treats it two
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
        """With ``HOME`` unset the platform home directory is used, so a bare environment still resolves ``~``."""
        decoy = self._decoy_home(roots["outside"], monkeypatch)
        monkeypatch.delenv("HOME")
        rc = jq_write.run_jq_write(Path("~/.claude/settings.json"), ".k = 1", [])
        assert rc == 0
        assert json.loads(decoy.read_text(encoding="utf-8")) == {"k": 1}

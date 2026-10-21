"""Tests for ``bin/parse-skill-flags.py`` — generic anchored-token flag + --keep parser.

Covers:
* ``parse_skill_flags`` — flag detection, anchored-token safety, ``--keep``
  extraction, ``CLEAN_ARGS`` stripping
* ``_var_name`` — flag-name → shell-variable-suffix transform
* ``_validate_flags`` — ``--flags`` CLI value validation
* Doctest hookup for embedded examples
* ``_emit`` shell-quoting safety (hostile input round-trip)
* ``main()`` end-to-end via subprocess, including the three real skill call
  shapes (resolve/analyse/review) this script replaces
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import parse_skill_flags as psf  # loaded by conftest.py
import pytest
from parse_skill_flags import _emit, _validate_flags, _var_name, parse_skill_flags  # loaded by conftest.py

_BIN = Path(__file__).resolve().parents[1] / "bin" / "parse-skill-flags.py"


# ---------------------------------------------------------------------------
# _var_name — flag-name → shell-variable-suffix transform
# ---------------------------------------------------------------------------


class TestVarName:
    """_var_name: bare flag name → FLAG_<NAME> suffix."""

    @pytest.mark.parametrize(
        ("flag", "expected"),
        [
            pytest.param("worktree", "WORKTREE", id="single-word"),
            pytest.param("no-challenge", "NO_CHALLENGE", id="hyphenated"),
            pytest.param("codemap", "CODEMAP", id="another-single-word"),
        ],
    )
    def test_transform(self, flag: str, expected: str) -> None:
        """Uppercase + hyphen-to-underscore, no other mutation."""
        assert _var_name(flag) == expected


# ---------------------------------------------------------------------------
# _validate_flags — ``--flags`` CLI value validation
# ---------------------------------------------------------------------------


class TestValidateFlags:
    """_validate_flags: comma-split + per-token name validation."""

    def test_multiple_flags_preserve_order(self) -> None:
        """Comma-separated tokens split, whitespace trimmed, order preserved."""
        assert _validate_flags("reply, no-challenge ,worktree") == ["reply", "no-challenge", "worktree"]

    @pytest.mark.parametrize(
        ("value", "message"),
        [
            pytest.param("", "at least one flag name", id="empty-string"),
            pytest.param("Reply", "invalid flag name", id="uppercase-token"),
            pytest.param("--reply", "invalid flag name", id="leading-dash"),
        ],
    )
    def test_invalid_flags_value_raises(self, value: str, message: str) -> None:
        """An empty ``--flags`` value is rejected; flag names must be lowercase and bare (no leading ``--``)."""
        with pytest.raises(ValueError, match=message):
            _validate_flags(value)


# ---------------------------------------------------------------------------
# parse_skill_flags — flag detection + CLEAN_ARGS stripping
# ---------------------------------------------------------------------------


class TestFlagDetection:
    """parse_skill_flags: anchored-token detection, never bare substring."""

    @pytest.mark.parametrize(
        ("blob", "flags", "variable", "expected"),
        [
            pytest.param("42 --reply", ["reply"], "FLAG_REPLY", "true", id="flag-present"),
            pytest.param("42", ["reply"], "FLAG_REPLY", "false", id="flag-absent"),
            pytest.param("42 --no-challenge", ["no-challenge"], "FLAG_NO_CHALLENGE", "true", id="hyphenated-flag-name"),
            pytest.param(
                "--reply-later fix the bug", ["reply"], "FLAG_REPLY", "false", id="reply-later-not-a-substring-match"
            ),
            pytest.param(
                "42 some-repo--reply-bot", ["reply"], "FLAG_REPLY", "false", id="repo-name-with-reply-bot-not-a-match"
            ),
        ],
    )
    def test_flag_detected_only_as_anchored_token(
        self, blob: str, flags: list[str], variable: str, expected: str
    ) -> None:
        """A requested flag present in the blob → 'true', absent → 'false'; never a bare substring match.

        A hyphenated flag name emits FLAG_NO_CHALLENGE and matches correctly. '--reply-later' must NOT false-fire
        FLAG_REPLY (documented pitfall), and neither may a repo name containing '--reply-bot'.
        """
        result = parse_skill_flags(blob, flags)
        assert result[variable] == expected

    def test_multiple_flags_independent(self) -> None:
        """Each requested flag is detected independently of the others."""
        result = parse_skill_flags("42 --reply --worktree", ["reply", "no-challenge", "worktree"])
        assert result["FLAG_REPLY"] == "true"
        assert result["FLAG_NO_CHALLENGE"] == "false"
        assert result["FLAG_WORKTREE"] == "true"


class TestKeepExtraction:
    """parse_skill_flags: ``--keep <items>`` value extraction."""

    @pytest.mark.parametrize(
        ("blob", "expected"),
        [
            pytest.param('42 --keep "drop the typo fix"', "drop the typo fix", id="quoted-value-captured-verbatim"),
            pytest.param("42", "", id="absent-yields-empty-string-not-missing-key"),
            pytest.param("42 --keep 'a b'", "a b", id="single-quoted-value"),
            pytest.param("plan.md --keep foo bar --reply", "foo bar", id="quote-stripped-words-up-to-next-option"),
            pytest.param("plan.md --keep foo bar", "foo bar", id="quote-stripped-words-to-end"),
            pytest.param('42 --keep="x y"', "x y", id="equals-form"),
        ],
    )
    def test_keep_items_value(self, blob: str, expected: str) -> None:
        """A ``--keep`` value is captured in every spelling; no ``--keep`` flag → KEEP_ITEMS is an empty string.

        The host substitutes ``$ARGUMENTS`` into ``"$ARGUMENTS"`` as text, so the shell consumes the user's double
        quotes and ``--keep "foo bar"`` arrives as bare words — that form must not collapse to ''.
        """
        result = parse_skill_flags(blob, [])
        assert result["KEEP_ITEMS"] == expected

    @pytest.mark.parametrize(
        ("blob", "expected_clean"),
        [
            pytest.param("plan.md --keep foo bar --reply", "plan.md", id="bare-words-and-flag-stripped"),
            pytest.param("plan.md --keep", "plan.md", id="dangling-keep-stripped"),
        ],
    )
    def test_keep_forms_removed_from_clean_args(self, blob: str, expected_clean: str) -> None:
        """Quote-stripped and dangling ``--keep`` spellings never leak into ``CLEAN_ARGS``.

        A leaked ``--keep foo`` would become part of the program path or search topic a skill reads back.
        """
        assert parse_skill_flags(blob, ["reply"])["CLEAN_ARGS"] == expected_clean

    @pytest.mark.parametrize(
        ("blob", "fragment"),
        [
            pytest.param("42 --keep", "empty value", id="dangling-keep-warns"),
            pytest.param('42 --keep ""', "empty value", id="empty-quoted-warns"),
            pytest.param("42 --keep foo bar", "unquoted", id="bare-words-warn"),
        ],
    )
    def test_main_warns_on_degraded_keep(self, blob: str, fragment: str, capsys: pytest.CaptureFixture[str]) -> None:
        """An empty or quote-stripped ``--keep`` prints a stderr warning while still exiting 0.

        Eval callers depend on exit 0 and clean stdout; the warning makes a lost or guessed keep value visible.
        """
        assert psf.main(["--flags", "reply", blob]) == 0
        assert fragment in capsys.readouterr().err

    def test_keep_removed_from_clean_args(self) -> None:
        """Remove retained-item options from the cleaned argument list."""
        result = parse_skill_flags('42 --keep "drop the typo fix"', [])
        assert "--keep" not in result["CLEAN_ARGS"]
        assert "drop the typo fix" not in result["CLEAN_ARGS"]


class TestCleanArgs:
    """parse_skill_flags: CLEAN_ARGS construction (flag stripping, #, whitespace)."""

    @pytest.mark.parametrize(
        ("blob", "flags", "expected"),
        [
            pytest.param(
                "42 --reply --worktree report", ["reply", "worktree"], "42 report", id="requested-flags-stripped"
            ),
            pytest.param("#42", [], "42", id="leading-hash-stripped-once"),
            pytest.param("##42", [], "#42", id="double-hash-stripped-once-not-recursively"),
            pytest.param("42   --reply   report", ["reply"], "42 report", id="whitespace-collapsed-after-flag-removal"),
        ],
    )
    def test_clean_args_value(self, blob: str, flags: list[str], expected: str) -> None:
        """Every requested flag token is removed and exactly one leading '#' is stripped (not recursively).

        Multiple spaces left behind by flag removal collapse to one.
        """
        result = parse_skill_flags(blob, flags)
        assert result["CLEAN_ARGS"] == expected

    def test_unrequested_flag_left_untouched(self) -> None:
        """A flag token not in the requested list is left in CLEAN_ARGS."""
        result = parse_skill_flags("42 --unknown-flag", ["reply"])
        assert "--unknown-flag" in result["CLEAN_ARGS"]

    def test_empty_flags_list_still_handles_keep_and_hash(self) -> None:
        """An empty flags list is valid input to the pure function (CLI rejects it separately)."""
        result = parse_skill_flags('#42 --keep "x"', [])
        assert result["CLEAN_ARGS"] == "42"
        assert result["KEEP_ITEMS"] == "x"


# ---------------------------------------------------------------------------
# _emit — shell-quoting safety
# ---------------------------------------------------------------------------


def test_emit_produces_shell_quoted_assignments() -> None:
    """_emit wraps values with shlex.quote; assignments are VAR=value lines."""
    parsed = {"FLAG_REPLY": "true", "KEEP_ITEMS": "", "CLEAN_ARGS": "42"}
    output = _emit(parsed)
    lines = output.strip().splitlines()
    assert len(lines) == 3
    for line in lines:
        assert "=" in line


@pytest.mark.parametrize(
    "value",
    ["plain text", "'; touch /tmp/pwned; echo 'x", "", "line1\nline2", "$(touch /tmp/pwned)"],
)
def test_emit_shell_round_trip_for_hostile_values(value: str) -> None:
    """A hostile KEEP_ITEMS value round-trips through eval-safe shell quoting."""
    parsed = {"FLAG_REPLY": "false", "KEEP_ITEMS": value, "CLEAN_ARGS": "42"}
    assignments = dict(token.split("=", 1) for token in shlex.split(_emit(parsed)))
    assert assignments["KEEP_ITEMS"] == value


# ---------------------------------------------------------------------------
# Doctest hookup
# ---------------------------------------------------------------------------


def test_module_doctests_pass() -> None:
    """Doctest examples embedded in parse-skill-flags.py must not regress."""
    import doctest

    import parse_skill_flags as _mod

    results = doctest.testmod(_mod, verbose=False)
    assert results.failed == 0, f"{results.failed} doctest(s) failed"


# ---------------------------------------------------------------------------
# main() — subprocess end-to-end, including the three real call shapes
# ---------------------------------------------------------------------------


def test_main_missing_flags_exits_nonzero() -> None:
    """Missing ``--flags`` is an argparse-level error (exit 2)."""
    result = subprocess.run(
        [sys.executable, str(_BIN), "42"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2


def test_main_invalid_flag_name_exits_nonzero() -> None:
    """An invalid flag token in ``--flags`` exits 2 with a stderr message."""
    result = subprocess.run(
        [sys.executable, str(_BIN), "--flags", "Reply", "42"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "invalid flag name" in result.stderr


def test_help_blob_is_argument_text_not_argparse_help() -> None:
    """Treat a blob of exactly ``--help`` as argument text, never as a help request.

    Callers wrap this script in ``eval "$(...)"``, so argparse help printed on stdout would be executed as shell source
    and leave every emitted variable unset.
    """
    result = subprocess.run(
        [sys.executable, str(_BIN), "--flags", "reply", "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.returncode == 0
    assert "usage:" not in result.stdout
    assert "CLEAN_ARGS=--help" in result.stdout


@pytest.mark.parametrize(
    ("flags", "argument", "expected_var", "expected_value"),
    [
        pytest.param("reply", "42 --reply", "FLAG_REPLY", "true", id="basic"),
        pytest.param("worktree", "42 report --worktree", "FLAG_WORKTREE", "true", id="resolve-shape"),
        pytest.param("reply,quick", "vitality --quick", "FLAG_QUICK", "true", id="analyse-shape"),
        pytest.param(
            "reply,no-challenge,no-codemap,codemap,worktree,full",
            "123 --reply --full",
            "FLAG_FULL",
            "true",
            id="review-shape",
        ),
        pytest.param("reply", "-x is broken", "CLEAN_ARGS", "-x is broken", id="dash-leading-prose-not-an-option"),
    ],
)
def test_real_skill_call_shapes_via_subprocess(
    flags: str, argument: str, expected_var: str, expected_value: str
) -> None:
    """The three real SKILL.md call shapes (resolve/analyse/review) produce correct assignments.

    A basic ``--flags reply`` call yields parseable shell assignments. Blob-forward safety: dash-leading blob content
    reaches the parser as CLEAN_ARGS, not as an unknown option.
    """
    result = subprocess.run(
        [sys.executable, str(_BIN), "--flags", flags, argument],
        capture_output=True,
        text=True,
        check=True,
    )
    assignments = dict(line.split("=", 1) for line in result.stdout.strip().splitlines())
    assert assignments[expected_var].strip("'") == expected_value


# ---------------------------------------------------------------------------
# --keep-state — per-session sentinels + stale-contract clear (absorbs extract-keep-flag.py)
# ---------------------------------------------------------------------------


def _probe_symlink() -> bool:
    """Return whether this process may create a symlink (capability probe, not a platform test)."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        try:
            Path(tmp, "link").symlink_to(Path(tmp, "target"))
        except (OSError, NotImplementedError):
            return False
    return True


_skip_no_symlink = pytest.mark.skipif(not _probe_symlink(), reason="symlinks unavailable to this process")
_skip_without_uids = pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX uids unavailable")
_STAMP = Path(".temp/state/skill-contract.owner")
_CONTRACT = Path(".temp/state/skill-contract.md")


@pytest.fixture
def state_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated project dir (cwd) + TMPDIR + exported CSID; returns the TMPDIR."""
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TMPDIR", str(tmp))
    monkeypatch.setenv("CSID", "sess1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess1")
    return tmp


def _plant_contract(owner_session: str | None) -> None:
    """Write a contract, plus an owner stamp naming ``owner_session`` when given."""
    _CONTRACT.parent.mkdir(parents=True, exist_ok=True)
    _CONTRACT.write_text("stale\n", encoding="utf-8")
    if owner_session is not None:
        _STAMP.write_text(json.dumps({"session_id": owner_session}), encoding="utf-8")


class TestKeepState:
    """``--keep-state <prefix>``: every value lands in its own sentinel; stdout stays the eval-mode output."""

    def test_writes_every_sentinel_and_keeps_stdout(
        self, state_session: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Each flag, value flag, CLEAN_ARGS and the keep value get one sentinel; stdout matches eval mode."""
        argv = ["--flags", "reply,quick", "--value-flags", "agent", '42 --reply --agent sw --keep "a; b"']
        assert psf.main(argv) == 0
        eval_stdout = capsys.readouterr().out
        assert psf.main([*argv[:4], "--keep-state", "analyse", argv[4]]) == 0
        assert capsys.readouterr().out == eval_stdout
        contents = {p.name: p.read_text(encoding="utf-8") for p in state_session.iterdir()}
        assert contents == {
            "analyse-reply-sess1": "true\n",
            "analyse-quick-sess1": "false\n",
            "analyse-agent-sess1": "sw\n",
            "analyse-clean-args-sess1": "42\n",
            "analyse-keep-items-sess1": "a; b\n",
        }

    def test_valid_identity_punctuation(self, state_session: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Preserve all allowed filename characters when writing state sentinels."""
        monkeypatch.setenv("CSID", "session.1_2-3")
        assert psf.main(["--flags", "reply", "--keep-state", "skill.1_2-3", "42 --reply"]) == 0
        assert (state_session / "skill.1_2-3-reply-session.1_2-3").read_text(encoding="utf-8") == "true\n"

    def test_rerun_overwrites_previous_values(self, state_session: Path) -> None:
        """A later invocation in the same session rewrites every sentinel, so no earlier value survives."""
        assert psf.main(["--flags", "reply", "--keep-state", "s", '9 --reply --keep "x"']) == 0
        assert psf.main(["--flags", "reply", "--keep-state", "s", "10"]) == 0
        assert (state_session / "s-reply-sess1").read_text(encoding="utf-8") == "false\n"
        assert (state_session / "s-keep-items-sess1").read_text(encoding="utf-8") == "\n"
        assert (state_session / "s-clean-args-sess1").read_text(encoding="utf-8") == "10\n"

    @pytest.mark.parametrize(
        ("stamp", "extra", "cleared"),
        [
            pytest.param(None, [], True, id="no-stamp-clears"),
            pytest.param("sess1", [], True, id="own-stamp-clears"),
            pytest.param("other", [], False, id="foreign-stamp-kept"),
            pytest.param(None, ["--inlined"], False, id="inlined-kept"),
        ],
    )
    def test_contract_clear_respects_owner_and_inlined(
        self, state_session: Path, stamp: str | None, extra: list[str], cleared: bool
    ) -> None:
        """The stale contract goes unless ``--inlined`` or another session's stamp protects it."""
        _plant_contract(stamp)
        assert psf.main(["--flags", "reply", "--keep-state", "s", *extra, "42"]) == 0
        assert _CONTRACT.exists() is not cleared

    def test_eval_mode_never_touches_the_contract(self, state_session: Path) -> None:
        """Without ``--keep-state`` the script stays side-effect free."""
        _plant_contract(None)
        assert psf.main(["--flags", "reply", "42"]) == 0
        assert _CONTRACT.exists()
        assert list(state_session.iterdir()) == []

    @pytest.mark.parametrize(
        ("env", "prefix"),
        [
            pytest.param({"CSID": "", "CLAUDE_CODE_SESSION_ID": ""}, "s", id="missing-session"),
            pytest.param({"CSID": "a..b"}, "s", id="session-with-double-dot"),
            pytest.param({}, "a..b", id="prefix-with-double-dot"),
            pytest.param({}, "", id="empty-prefix"),
            pytest.param({"CSID": "bad\nname"}, "s", id="session-with-newline"),
            pytest.param({"CSID": "../x"}, "s", id="session-with-separator"),
            pytest.param({}, "../escape", id="prefix-with-separator"),
        ],
    )
    def test_identity_failures_return_two_before_any_side_effect(
        self,
        state_session: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        env: dict[str, str],
        prefix: str,
    ) -> None:
        """A bad session ID or prefix exits 2 with nothing printed, written or cleared."""
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        _plant_contract(None)
        assert psf.main(["--flags", "reply", "--keep-state", prefix, "42 --reply --keep unquoted"]) == 2
        output = capsys.readouterr()
        assert output.out == ""
        assert len(output.err.splitlines()) == 1
        assert _CONTRACT.read_text(encoding="utf-8") == "stale\n"
        assert list(state_session.iterdir()) == []

    @pytest.mark.parametrize(
        ("flags", "value_flags"),
        [
            pytest.param("keep-items", "", id="reserved-keep-items"),
            pytest.param("reply", "clean-args", id="reserved-clean-args"),
            pytest.param("agent", "agent", id="boolean-and-value-share-a-name"),
        ],
    )
    def test_sentinel_name_clash_returns_two(self, state_session: Path, flags: str, value_flags: str) -> None:
        """Two values that would share one sentinel path are refused instead of silently overwriting each other."""
        argv = ["--flags", flags, "--keep-state", "s", "42"]
        if value_flags:
            argv[2:2] = ["--value-flags", value_flags]
        assert psf.main(argv) == 2
        assert list(state_session.iterdir()) == []

    def test_state_options_without_keep_state_return_two(self, state_session: Path) -> None:
        """``--out-file``/``--inlined`` alone would be silently ignored; they are refused instead."""
        assert psf.main(["--flags", "reply", "--inlined", "42"]) == 2
        assert psf.main(["--flags", "reply", "--out-file", str(state_session / "k"), "42"]) == 2


class TestKeepStateOutFile:
    """``--out-file`` redirects only the keep value, confined to the temp dir or the working directory."""

    @_skip_without_uids
    def test_redirects_keep_value_into_new_private_dir(self, state_session: Path) -> None:
        """The state directory is created mode 0700; flag sentinels stay at their flat prefix names."""
        target = state_session / "distill-state-sess1" / "keep-items"
        argv = ["--flags", "eager", "--keep-state", "distill-state", "--out-file", str(target), '--keep "k"']
        assert psf.main(argv) == 0
        assert target.read_text(encoding="utf-8") == "k\n"
        assert target.parent.stat().st_mode & 0o777 == 0o700
        assert (state_session / "distill-state-eager-sess1").read_text(encoding="utf-8") == "false\n"
        assert not (state_session / "distill-state-keep-items-sess1").exists()

    @pytest.mark.parametrize(
        "out_file",
        [
            pytest.param("", id="empty"),
            pytest.param("/definitely/not/allowed/keep", id="outside-roots"),
        ],
    )
    def test_unusable_out_file_returns_two_after_flags_are_fresh(self, state_session: Path, out_file: str) -> None:
        """A rejected ``--out-file`` exits 2, but the flag sentinels were already rewritten, never left stale."""
        (state_session / "s-reply-sess1").write_text("true\n", encoding="utf-8")
        assert psf.main(["--flags", "reply", "--keep-state", "s", "--out-file", out_file, "42"]) == 2
        assert (state_session / "s-reply-sess1").read_text(encoding="utf-8") == "false\n"


class TestKeepStateWriteSafety:
    """Sentinels sit at predictable names in a shared temp dir: never write through a planted entry."""

    @_skip_no_symlink
    def test_symlink_at_sentinel_is_replaced_not_followed(self, state_session: Path, tmp_path: Path) -> None:
        """A symlink at the sentinel name ends up a regular file; the link's target is left untouched."""
        victim = tmp_path / "victim"
        victim.write_text("precious\n", encoding="utf-8")
        sentinel = state_session / "s-keep-items-sess1"
        sentinel.symlink_to(victim)
        assert psf.main(["--flags", "reply", "--keep-state", "s", '--keep "x"']) == 0
        assert (victim.read_text(encoding="utf-8"), sentinel.is_symlink(), sentinel.read_text(encoding="utf-8")) == (
            "precious\n",
            False,
            "x\n",
        )
        assert [p.name for p in state_session.iterdir() if p.name.endswith(".tmp")] == []

    @_skip_without_uids
    def test_foreign_owned_sentinel_returns_one(self, state_session: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A sentinel another user owns is refused (exit 1) and left intact."""
        sentinel = state_session / "s-reply-sess1"
        sentinel.write_text("mine\n", encoding="utf-8")
        monkeypatch.setattr(psf.os, "getuid", lambda: sentinel.stat().st_uid + 1, raising=False)
        assert psf.main(["--flags", "reply", "--keep-state", "s", "42"]) == 1
        assert sentinel.read_text(encoding="utf-8") == "mine\n"


@pytest.mark.skipif(shutil.which("bash") is None, reason="read-back idiom runs in Bash")
def test_state_mode_round_trips_through_shell_read(tmp_path: Path) -> None:
    """The skill-side idiom (no eval, ``IFS= read -r``) recovers the exact values, quotes and spaces included."""
    block = (
        'export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"\n'
        f'"{sys.executable}" "{_BIN}" --flags reply --value-flags roles --keep-state t "$ARGUMENTS" >/dev/null\n'
        'IFS= read -r R < "${TMPDIR:-/tmp}/t-reply-${CSID}" 2>/dev/null || R=false\n'
        'IFS= read -r V < "${TMPDIR:-/tmp}/t-roles-${CSID}" 2>/dev/null || V=""\n'
        'IFS= read -r C < "${TMPDIR:-/tmp}/t-clean-args-${CSID}" 2>/dev/null || C=""\n'
        'IFS= read -r K < "${TMPDIR:-/tmp}/t-keep-items-${CSID}" 2>/dev/null || K=""\n'
        'printf "%s|%s|%s|%s\\n" "$R" "$V" "$C" "$K"\n'
    )
    env = {**os.environ, "TMPDIR": str(tmp_path), "CLAUDE_CODE_SESSION_ID": "rt", "ARGUMENTS": ""}
    env["ARGUMENTS"] = '#7 it\'s --reply --roles "a b" --keep "x \\\\ y; \'z\'"'
    result = subprocess.run(
        [shutil.which("bash"), "-c", block], cwd=tmp_path, env=env, capture_output=True, text=True, check=True
    )
    assert result.stdout == "true|a b|7 it's|x \\\\ y; 'z'\n"

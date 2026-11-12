"""Tests for ``bin/parse-skill-flags.py`` — generic anchored-token flag + --keep parser.

Covers:
* ``parse_skill_flags`` — flag detection, anchored-token safety, ``--keep`` extraction, ``CLEAN_ARGS`` stripping
* ``_var_name`` — flag-name to shell-variable-suffix transform
* ``_validate_flags`` — ``--flags`` CLI value validation
* ``_emit`` shell-quoting safety (hostile input round-trip)
* ``main()`` end to end via subprocess, including the three real skill call shapes (resolve/analyse/review)
* ``--keep-state`` per-session sentinels, stale-contract clearing and write safety
* Doctest hookup for embedded examples
"""

from __future__ import annotations

import doctest
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import parse_skill_flags as psf  # loaded by conftest.py
import pytest
from parse_skill_flags import _emit, _validate_flags, _var_name, parse_skill_flags  # loaded by conftest.py

_BIN = Path(__file__).resolve().parents[2] / "bin" / "parse-skill-flags.py"
_BASH = shutil.which("bash")
_STAMP = Path(".temp/state/skill-contract.owner")
_CONTRACT = Path(".temp/state/skill-contract.md")


def _probe_symlink() -> bool:
    """Return whether this process may create a symlink (capability probe, not a platform test)."""
    with tempfile.TemporaryDirectory() as tmp:
        try:
            Path(tmp, "link").symlink_to(Path(tmp, "target"))
        except (OSError, NotImplementedError):
            return False
    return True


_skip_no_symlink = pytest.mark.skipif(not _probe_symlink(), reason="symlinks unavailable to this process")
_skip_without_uids = pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX uids unavailable")


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


def _run_script(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the real script as a child process and return its result without raising on a non-zero exit."""
    return subprocess.run([sys.executable, str(_BIN), *args], capture_output=True, text=True, check=False)


class TestVarName:
    """_var_name: bare flag name to FLAG_<NAME> suffix."""

    @pytest.mark.parametrize(
        ("flag", "expected"),
        [
            pytest.param("worktree", "WORKTREE", id="single-word"),
            pytest.param("no-challenge", "NO_CHALLENGE", id="hyphenated"),
            pytest.param("codemap", "CODEMAP", id="another-single-word"),
        ],
    )
    def test_uppercases_and_underscores(self, flag: str, expected: str) -> None:
        """Uppercase + hyphen-to-underscore, no other mutation.

        The suffix names the shell variable the skill reads back, so any extra change would break the lookup.
        """
        assert _var_name(flag) == expected


class TestValidateFlags:
    """_validate_flags: comma-split + per-token name validation."""

    def test_multiple_flags_preserve_order(self) -> None:
        """Comma-separated tokens split, whitespace trimmed, order preserved.

        Scenario: a hand-written ``--flags`` value with stray spaces around the commas.
        """
        assert _validate_flags("reply, no-challenge ,worktree") == ["reply", "no-challenge", "worktree"]

    @pytest.mark.parametrize(
        ("value", "message"),
        [
            pytest.param("", "at least one flag name", id="empty-string"),
            pytest.param("Reply", "invalid flag name", id="uppercase-token"),
            pytest.param("--reply", "invalid flag name", id="leading-dash"),
        ],
    )
    def test_invalid_value_raises(self, value: str, message: str) -> None:
        """An empty ``--flags`` value is rejected; flag names must be lowercase and bare (no leading ``--``).

        The names become shell variable suffixes, so anything beyond lowercase words and hyphens must be refused.
        """
        with pytest.raises(ValueError, match=message):
            _validate_flags(value)


class TestFlagDetection:
    """parse_skill_flags: anchored-token detection, never bare substring."""

    @pytest.mark.parametrize(
        ("blob", "flags", "variable", "expected"),
        [
            pytest.param("42 --reply", ["reply"], "FLAG_REPLY", "true", id="flag-present"),
            pytest.param("42", ["reply"], "FLAG_REPLY", "false", id="flag-absent"),
            pytest.param("42 --no-challenge", ["no-challenge"], "FLAG_NO_CHALLENGE", "true", id="hyphenated-flag-name"),
            pytest.param("--reply-later fix the bug", ["reply"], "FLAG_REPLY", "false", id="reply-later-not-a-match"),
            pytest.param("42 some-repo--reply-bot", ["reply"], "FLAG_REPLY", "false", id="repo-name-not-a-match"),
        ],
    )
    def test_flag_detected_only_as_anchored_token(
        self, blob: str, flags: list[str], variable: str, expected: str
    ) -> None:
        """A requested flag present in the blob → 'true', absent → 'false'; never a bare substring match.

        A hyphenated flag name emits FLAG_NO_CHALLENGE and matches correctly. '--reply-later' must NOT false-fire
        FLAG_REPLY (documented pitfall), and neither may a repo name containing '--reply-bot'.
        """
        assert parse_skill_flags(blob, flags)[variable] == expected

    def test_multiple_flags_independent(self) -> None:
        """Each requested flag is detected independently of the others.

        Scenario: three requested flags, two present in the blob; the absent one must stay false.
        """
        result = parse_skill_flags("42 --reply --worktree", ["reply", "no-challenge", "worktree"])
        assert {name: result[name] for name in ("FLAG_REPLY", "FLAG_NO_CHALLENGE", "FLAG_WORKTREE")} == {
            "FLAG_REPLY": "true",
            "FLAG_NO_CHALLENGE": "false",
            "FLAG_WORKTREE": "true",
        }


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
        assert parse_skill_flags(blob, [])["KEEP_ITEMS"] == expected

    @pytest.mark.parametrize(
        ("blob", "expected_clean"),
        [
            pytest.param('42 --keep "drop the typo fix"', "42", id="quoted-value-stripped"),
            pytest.param("plan.md --keep foo bar --reply", "plan.md", id="bare-words-and-flag-stripped"),
            pytest.param("plan.md --keep", "plan.md", id="dangling-keep-stripped"),
        ],
    )
    def test_keep_forms_removed_from_clean_args(self, blob: str, expected_clean: str) -> None:
        """Quoted, quote-stripped and dangling ``--keep`` spellings never leak into ``CLEAN_ARGS``.

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
    def test_value(self, blob: str, flags: list[str], expected: str) -> None:
        """Every requested flag token is removed and exactly one leading '#' is stripped (not recursively).

        Multiple spaces left behind by flag removal collapse to one.
        """
        assert parse_skill_flags(blob, flags)["CLEAN_ARGS"] == expected

    def test_unrequested_flag_left_untouched(self) -> None:
        """A flag token not in the requested list is left in CLEAN_ARGS.

        Only the flags a skill asked for are consumed; anything else belongs to the skill's own argument text.
        """
        assert "--unknown-flag" in parse_skill_flags("42 --unknown-flag", ["reply"])["CLEAN_ARGS"]

    def test_empty_flags_list_still_handles_keep_and_hash(self) -> None:
        """An empty flags list is valid input to the pure function (CLI rejects it separately).

        Keep extraction and ``#`` stripping do not depend on any requested flag.
        """
        result = parse_skill_flags('#42 --keep "x"', [])
        assert (result["CLEAN_ARGS"], result["KEEP_ITEMS"]) == ("42", "x")


class TestEmit:
    """_emit: shell-quoted VAR=value lines that are safe to eval."""

    def test_produces_one_assignment_per_value(self) -> None:
        """_emit prints one ``VAR=value`` line per parsed value.

        The caller evals the output, so each value must occupy exactly one assignment line.
        """
        lines = _emit({"FLAG_REPLY": "true", "KEEP_ITEMS": "", "CLEAN_ARGS": "42"}).strip().splitlines()
        assert [line.split("=", 1)[0] for line in lines] == ["FLAG_REPLY", "KEEP_ITEMS", "CLEAN_ARGS"]

    @pytest.mark.parametrize(
        "value",
        ["plain text", "'; touch /tmp/pwned; echo 'x", "", "line1\nline2", "$(touch /tmp/pwned)"],
    )
    def test_hostile_values_round_trip(self, value: str) -> None:
        """A hostile KEEP_ITEMS value round-trips through eval-safe shell quoting.

        Quotes, command substitution and newlines in user text must come back byte for byte, never execute.
        """
        parsed = {"FLAG_REPLY": "false", "KEEP_ITEMS": value, "CLEAN_ARGS": "42"}
        assignments = dict(token.split("=", 1) for token in shlex.split(_emit(parsed)))
        assert assignments["KEEP_ITEMS"] == value


def test_module_doctests_pass() -> None:
    """Doctest examples embedded in parse-skill-flags.py must not regress.

    The script's usage examples double as documentation, so a stale example fails here instead of misleading a reader.
    """
    results = doctest.testmod(psf, verbose=False)
    assert results.failed == 0, f"{results.failed} doctest(s) failed"


class TestCli:
    """Main() through a real child process: exit codes and the eval-facing stdout."""

    @pytest.mark.parametrize("blob", ["--flags=quick", "--out-file=chosen", "--keep-state=chosen", "--inlined"])
    def test_delimited_blob_cannot_replace_parser_options(self, blob: str) -> None:
        """An explicit option terminator preserves even a blob named like a parser option."""
        result = _run_script("--flags", "reply", "--", blob)
        assert result.returncode == 0
        assignments = dict(token.split("=", 1) for token in shlex.split(result.stdout))
        assert assignments == {"FLAG_REPLY": "false", "KEEP_ITEMS": "", "CLEAN_ARGS": blob}

    def test_delimited_out_file_blob_cannot_redirect_state(self, state_session: Path) -> None:
        """A user blob cannot write a chosen file through the parser's out-file option."""
        victim = state_session / "victim"
        victim.write_text("precious", encoding="utf-8")
        blob = f"--out-file={victim}"
        result = _run_script("--flags", "reply", "--keep-state", "safe", "--", blob)
        assert result.returncode == 0
        assert victim.read_text(encoding="utf-8") == "precious"
        assert (state_session / "safe-clean-args-sess1").read_text(encoding="utf-8") == blob + "\n"

    @pytest.mark.parametrize(
        ("argv", "fragment"),
        [
            pytest.param(["42"], "the following arguments are required: --flags", id="missing-flags"),
            pytest.param(["--flags", "Reply", "42"], "invalid flag name", id="invalid-flag-name"),
        ],
    )
    def test_argument_errors_exit_two(self, argv: list[str], fragment: str) -> None:
        """A missing ``--flags`` or an invalid flag token exits 2 with the reason on stderr.

        Exit 2 is the argparse-level failure the calling skill treats as fatal, so it must come from the real CLI.
        """
        result = _run_script(*argv)
        assert result.returncode == 2
        assert fragment in result.stderr

    def test_help_blob_is_argument_text_not_argparse_help(self) -> None:
        """Treat a blob of exactly ``--help`` as argument text, never as a help request.

        Callers wrap this script in ``eval "$(...)"``, so argparse help printed on stdout would be executed as shell
        source and leave every emitted variable unset.
        """
        result = _run_script("--flags", "reply", "--help")
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
    def test_real_skill_call_shapes(self, flags: str, argument: str, expected_var: str, expected_value: str) -> None:
        """The real SKILL.md call shapes (resolve/analyse/review) produce correct assignments.

        A basic ``--flags reply`` call yields parseable shell assignments. Blob-forward safety: dash-leading blob
        content reaches the parser as CLEAN_ARGS, not as an unknown option.
        """
        result = _run_script("--flags", flags, argument)
        assignments = dict(line.split("=", 1) for line in result.stdout.strip().splitlines())
        assert (result.returncode, assignments[expected_var].strip("'")) == (0, expected_value)


class TestKeepState:
    """``--keep-state <prefix>``: every value lands in its own sentinel; stdout stays the eval-mode output."""

    def test_writes_every_sentinel_and_keeps_stdout(
        self, state_session: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Each flag, value flag, CLEAN_ARGS and the keep value get one sentinel; stdout matches eval mode.

        Scenario: the same blob parsed once in eval mode and once with ``--keep-state analyse``. The stdout must be
        identical, and the temp dir must hold exactly the five per-session sentinels.
        """
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

    def test_keeps_identity_punctuation(self, state_session: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Preserve all allowed filename characters when writing state sentinels.

        Dots, underscores and hyphens are legal in both the session ID and the prefix and must reach the file name
        unchanged, or the skill that reads the sentinel back looks for a different path.
        """
        monkeypatch.setenv("CSID", "session.1_2-3")
        assert psf.main(["--flags", "reply", "--keep-state", "skill.1_2-3", "42 --reply"]) == 0
        assert (state_session / "skill.1_2-3-reply-session.1_2-3").read_text(encoding="utf-8") == "true\n"

    def test_rerun_overwrites_previous_values(self, state_session: Path) -> None:
        """A later invocation in the same session rewrites every sentinel, so no earlier value survives.

        Scenario: a first run with a flag and a keep value, then a second run with neither; the second must leave
        ``false`` and empty values, not the first run's.
        """
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
        """The stale contract goes unless ``--inlined`` or another session's stamp protects it.

        A parent skill's live contract (``--inlined``) or one stamped by another session must survive a child's run.
        """
        _plant_contract(stamp)
        assert psf.main(["--flags", "reply", "--keep-state", "s", *extra, "42"]) == 0
        assert _CONTRACT.exists() is not cleared

    def test_eval_mode_never_touches_the_contract(self, state_session: Path) -> None:
        """Without ``--keep-state`` the script stays side-effect free.

        Eval-mode callers expect pure stdout; no contract is cleared and no sentinel is written.
        """
        _plant_contract(None)
        assert psf.main(["--flags", "reply", "42"]) == 0
        assert _CONTRACT.exists()
        assert list(state_session.iterdir()) == []

    @pytest.mark.parametrize(
        ("csid", "claude_session", "prefix"),
        [
            pytest.param("", "", "s", id="missing-session"),
            pytest.param("a..b", "sess1", "s", id="session-with-double-dot"),
            pytest.param("sess1", "sess1", "a..b", id="prefix-with-double-dot"),
            pytest.param("sess1", "sess1", "", id="empty-prefix"),
            pytest.param("bad\nname", "sess1", "s", id="session-with-newline"),
            pytest.param("../x", "sess1", "s", id="session-with-separator"),
            pytest.param("sess1", "sess1", "../escape", id="prefix-with-separator"),
        ],
    )
    def test_identity_failures_return_two_before_any_side_effect(
        self,
        state_session: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        csid: str,
        claude_session: str,
        prefix: str,
    ) -> None:
        """A bad session ID or prefix exits 2 with nothing printed, written or cleared.

        The session ID and prefix become part of a file name in a shared temp dir, so a separator or ``..`` must be
        refused before the script prints, writes a sentinel or clears the planted contract.
        """
        monkeypatch.setenv("CSID", csid)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", claude_session)
        _plant_contract(None)
        assert psf.main(["--flags", "reply", "--keep-state", prefix, "42 --reply --keep unquoted"]) == 2
        output = capsys.readouterr()
        assert output.out == ""
        assert len(output.err.splitlines()) == 1
        assert _CONTRACT.read_text(encoding="utf-8") == "stale\n"
        assert list(state_session.iterdir()) == []

    @pytest.mark.parametrize(
        "flag_args",
        [
            pytest.param(["--flags", "keep-items"], id="reserved-keep-items"),
            pytest.param(["--flags", "reply", "--value-flags", "clean-args"], id="reserved-clean-args"),
            pytest.param(["--flags", "agent", "--value-flags", "agent"], id="boolean-and-value-share-a-name"),
        ],
    )
    def test_sentinel_name_clash_returns_two(self, state_session: Path, flag_args: list[str]) -> None:
        """Two values that would share one sentinel path are refused instead of silently overwriting each other.

        A flag called ``keep-items`` or ``clean-args``, or a name used as both boolean and value flag, would collide
        with a reserved sentinel; nothing may be written in that case.
        """
        assert psf.main([*flag_args, "--keep-state", "s", "42"]) == 2
        assert list(state_session.iterdir()) == []

    @pytest.mark.parametrize(
        "option",
        [
            pytest.param(["--inlined"], id="inlined"),
            pytest.param(["--out-file", "keep"], id="out-file"),
        ],
    )
    def test_state_options_without_keep_state_return_two(self, state_session: Path, option: list[str]) -> None:
        """``--out-file``/``--inlined`` alone would be silently ignored; they are refused instead.

        Scenario: a caller forgets ``--keep-state`` but passes an option that only means something with it.
        """
        assert psf.main(["--flags", "reply", *option, "42"]) == 2


class TestKeepStateOutFile:
    """``--out-file`` redirects only the keep value, confined to the temp dir or the working directory."""

    @_skip_without_uids
    def test_redirects_keep_value_into_new_private_dir(self, state_session: Path) -> None:
        """The state directory is created mode 0700; flag sentinels stay at their flat prefix names.

        Scenario: ``--out-file`` names a file in a not-yet-existing per-session directory under the temp dir.
        """
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
        """A rejected ``--out-file`` exits 2, but the flag sentinels were already rewritten, never left stale.

        A previous run left ``reply`` true; the failing run must not leave that behind for the skill to read.
        """
        (state_session / "s-reply-sess1").write_text("true\n", encoding="utf-8")
        assert psf.main(["--flags", "reply", "--keep-state", "s", "--out-file", out_file, "42"]) == 2
        assert (state_session / "s-reply-sess1").read_text(encoding="utf-8") == "false\n"


class TestKeepStateWriteSafety:
    """Sentinels sit at predictable names in a shared temp dir: never write through a planted entry."""

    @_skip_no_symlink
    def test_symlink_at_sentinel_is_replaced_not_followed(self, state_session: Path, tmp_path: Path) -> None:
        """A symlink at the sentinel name ends up a regular file; the link's target is left untouched.

        Scenario: another local user planted ``s-keep-items-sess1`` as a link to a victim file; writing through it
        would overwrite the victim.
        """
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
        """A sentinel another user owns is refused (exit 1) and left intact.

        The foreign owner is simulated by shifting the process uid, since creating another user's file needs root.
        """
        sentinel = state_session / "s-reply-sess1"
        sentinel.write_text("mine\n", encoding="utf-8")
        monkeypatch.setattr(psf.os, "getuid", lambda: sentinel.stat().st_uid + 1, raising=False)
        assert psf.main(["--flags", "reply", "--keep-state", "s", "42"]) == 1
        assert sentinel.read_text(encoding="utf-8") == "mine\n"


@pytest.mark.skipif(_BASH is None, reason="read-back idiom runs in Bash")
def test_state_mode_round_trips_through_shell_read(tmp_path: Path) -> None:
    """The skill-side idiom (no eval, ``IFS= read -r``) recovers the exact values, quotes and spaces included.

    Scenario: the real script writes the sentinels in a child bash, which reads each back with the exact idiom the
    skills use. Apostrophes, backslashes, semicolons and a leading ``#`` in the blob must all survive.
    """
    block = (
        'export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"\n'
        f'"{sys.executable}" "{_BIN}" --flags reply --value-flags roles --keep-state t "$ARGUMENTS" >/dev/null\n'
        'IFS= read -r R < "${TMPDIR:-/tmp}/t-reply-${CSID}" 2>/dev/null || R=false\n'
        'IFS= read -r V < "${TMPDIR:-/tmp}/t-roles-${CSID}" 2>/dev/null || V=""\n'
        'IFS= read -r C < "${TMPDIR:-/tmp}/t-clean-args-${CSID}" 2>/dev/null || C=""\n'
        'IFS= read -r K < "${TMPDIR:-/tmp}/t-keep-items-${CSID}" 2>/dev/null || K=""\n'
        'printf "%s|%s|%s|%s\\n" "$R" "$V" "$C" "$K"\n'
    )
    arguments = '#7 it\'s --reply --roles "a b" --keep "x \\\\ y; \'z\'"'
    env = {**os.environ, "TMPDIR": str(tmp_path), "CLAUDE_CODE_SESSION_ID": "rt", "ARGUMENTS": arguments}
    result = subprocess.run([_BASH, "-c", block], cwd=tmp_path, env=env, capture_output=True, text=True, check=True)
    assert result.stdout == "true|a b|7 it's|x \\\\ y; 'z'\n"

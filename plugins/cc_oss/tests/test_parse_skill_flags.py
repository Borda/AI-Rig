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

import shlex
import subprocess
import sys
from pathlib import Path

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

    def test_empty_string_raises(self) -> None:
        """An empty ``--flags`` value is rejected."""
        with pytest.raises(ValueError, match="at least one flag name"):
            _validate_flags("")

    def test_uppercase_token_raises(self) -> None:
        """Flag names must be lowercase — uppercase rejected."""
        with pytest.raises(ValueError, match="invalid flag name"):
            _validate_flags("Reply")

    def test_leading_dash_raises(self) -> None:
        """Flag names are bare (no leading ``--``) — a dash-prefixed token is rejected."""
        with pytest.raises(ValueError, match="invalid flag name"):
            _validate_flags("--reply")


# ---------------------------------------------------------------------------
# parse_skill_flags — flag detection + CLEAN_ARGS stripping
# ---------------------------------------------------------------------------


class TestFlagDetection:
    """parse_skill_flags: anchored-token detection, never bare substring."""

    def test_flag_present(self) -> None:
        """A requested flag present in the blob → 'true'."""
        result = parse_skill_flags("42 --reply", ["reply"])
        assert result["FLAG_REPLY"] == "true"

    def test_flag_absent(self) -> None:
        """A requested flag absent from the blob → 'false'."""
        result = parse_skill_flags("42", ["reply"])
        assert result["FLAG_REPLY"] == "false"

    def test_hyphenated_flag_name(self) -> None:
        """Hyphenated flag name emits FLAG_NO_CHALLENGE, matches correctly."""
        result = parse_skill_flags("42 --no-challenge", ["no-challenge"])
        assert result["FLAG_NO_CHALLENGE"] == "true"

    def test_anchored_not_substring_reply_later(self) -> None:
        """'--reply-later' must NOT false-fire FLAG_REPLY (documented pitfall)."""
        result = parse_skill_flags("--reply-later fix the bug", ["reply"])
        assert result["FLAG_REPLY"] == "false"

    def test_anchored_not_substring_reply_bot_repo_name(self) -> None:
        """A repo name containing '--reply-bot' must not false-fire FLAG_REPLY."""
        result = parse_skill_flags("42 some-repo--reply-bot", ["reply"])
        assert result["FLAG_REPLY"] == "false"

    def test_multiple_flags_independent(self) -> None:
        """Each requested flag is detected independently of the others."""
        result = parse_skill_flags("42 --reply --worktree", ["reply", "no-challenge", "worktree"])
        assert result["FLAG_REPLY"] == "true"
        assert result["FLAG_NO_CHALLENGE"] == "false"
        assert result["FLAG_WORKTREE"] == "true"


class TestKeepExtraction:
    """parse_skill_flags: ``--keep <items>`` value extraction."""

    def test_keep_value_extracted(self) -> None:
        """A quoted ``--keep`` value is captured verbatim."""
        result = parse_skill_flags('42 --keep "drop the typo fix"', [])
        assert result["KEEP_ITEMS"] == "drop the typo fix"

    def test_keep_absent_yields_empty_string(self) -> None:
        """No ``--keep`` flag → KEEP_ITEMS is empty string, not absent key."""
        result = parse_skill_flags("42", [])
        assert result["KEEP_ITEMS"] == ""

    def test_keep_removed_from_clean_args(self) -> None:
        """Remove retained-item options from the cleaned argument list."""
        result = parse_skill_flags('42 --keep "drop the typo fix"', [])
        assert "--keep" not in result["CLEAN_ARGS"]
        assert "drop the typo fix" not in result["CLEAN_ARGS"]


class TestCleanArgs:
    """parse_skill_flags: CLEAN_ARGS construction (flag stripping, #, whitespace)."""

    def test_requested_flags_stripped(self) -> None:
        """Every requested flag token is removed from CLEAN_ARGS."""
        result = parse_skill_flags("42 --reply --worktree report", ["reply", "worktree"])
        assert result["CLEAN_ARGS"] == "42 report"

    def test_leading_hash_stripped_once(self) -> None:
        """Exactly one leading '#' is stripped from CLEAN_ARGS."""
        result = parse_skill_flags("#42", [])
        assert result["CLEAN_ARGS"] == "42"

    def test_double_hash_stripped_once_not_recursively(self) -> None:
        """'##42' has exactly one '#' stripped → CLEAN_ARGS='#42'."""
        result = parse_skill_flags("##42", [])
        assert result["CLEAN_ARGS"] == "#42"

    def test_whitespace_collapsed(self) -> None:
        """Multiple spaces left behind by flag removal collapse to one."""
        result = parse_skill_flags("42   --reply   report", ["reply"])
        assert result["CLEAN_ARGS"] == "42 report"

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


def test_main_via_subprocess_basic() -> None:
    """Subprocess invocation produces parseable shell assignments."""
    result = subprocess.run(
        [sys.executable, str(_BIN), "--flags", "reply", "42", "--reply"],
        capture_output=True,
        text=True,
        check=True,
    )
    assignments = dict(line.split("=", 1) for line in result.stdout.strip().splitlines())
    assert assignments["FLAG_REPLY"].strip("'") == "true"


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


def test_dash_leading_prose_forwarded_not_misparsed_as_flag() -> None:
    """Blob-forward safety: dash-leading blob content reaches the parser as CLEAN_ARGS, not an unknown option."""
    result = subprocess.run(
        [sys.executable, str(_BIN), "--flags", "reply", "-x is broken"],
        capture_output=True,
        text=True,
        check=True,
    )
    assignments = dict(line.split("=", 1) for line in result.stdout.strip().splitlines())
    assert "is broken" in assignments["CLEAN_ARGS"]


@pytest.mark.parametrize(
    ("flags", "argument", "expected_var", "expected_value"),
    [
        pytest.param("worktree", "42 report --worktree", "FLAG_WORKTREE", "true", id="resolve-shape"),
        pytest.param("reply,quick", "vitality --quick", "FLAG_QUICK", "true", id="analyse-shape"),
        pytest.param(
            "reply,no-challenge,no-codemap,codemap,semble,worktree",
            "123 --reply --semble",
            "FLAG_SEMBLE",
            "true",
            id="review-shape",
        ),
    ],
)
def test_real_skill_call_shapes_via_subprocess(
    flags: str, argument: str, expected_var: str, expected_value: str
) -> None:
    """The three real SKILL.md call shapes (resolve/analyse/review) produce correct assignments."""
    result = subprocess.run(
        [sys.executable, str(_BIN), "--flags", flags, argument],
        capture_output=True,
        text=True,
        check=True,
    )
    assignments = dict(line.split("=", 1) for line in result.stdout.strip().splitlines())
    assert assignments[expected_var].strip("'") == expected_value

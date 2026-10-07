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
        ],
    )
    def test_keep_items_value(self, blob: str, expected: str) -> None:
        """A quoted ``--keep`` value is captured verbatim; no ``--keep`` flag → KEEP_ITEMS is an empty string."""
        result = parse_skill_flags(blob, [])
        assert result["KEEP_ITEMS"] == expected

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

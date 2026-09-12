"""Tests for derive_codemap_target.py — goal-text codemap target derivation."""

from __future__ import annotations

import pytest

import derive_codemap_target
import parse_target_qname
from derive_codemap_target import derive_target, main


class TestDeriveTarget:
    """derive_target picks the module and function a goal names."""

    @pytest.mark.parametrize(
        ("goal", "expected"),
        [
            pytest.param("extend auth.tokens::refresh for rotation", ("auth.tokens", "refresh"), id="qualified"),
            pytest.param("speed up pkg.mod.submodule", ("pkg.mod.submodule", ""), id="dotted"),
            pytest.param("rework mod::fn", ("mod", "fn"), id="qualified-single-segment-module"),
            pytest.param("fix the login timeout", ("", ""), id="no-target"),
            pytest.param("rewrite this. and that.", ("", ""), id="prose-periods"),
            pytest.param("", ("", ""), id="empty"),
        ],
    )
    def test_recognised_spellings(self, goal, expected):
        """Verify each goal spelling resolves to its documented target pair."""
        assert derive_target(goal) == expected

    def test_qualified_wins_over_earlier_dotted(self):
        """Verify the qualified spelling outranks a dotted name appearing before it.

        The two spellings are searched in a fixed order, not by position, so a dotted module
        mentioned first never shadows an explicit ``module::function`` target.
        """
        assert derive_target("touch a.b then fix pkg.mod::run") == ("pkg.mod", "run")

    def test_leftmost_dotted_wins(self):
        """Verify the first dotted name wins when the goal names several."""
        assert derive_target("move pkg.one into pkg.two") == ("pkg.one", "")

    def test_bare_double_colon_falls_through_to_dotted(self):
        """Verify a bare ``::`` in prose does not suppress the dotted fallback.

        The shell branch this replaced tested for the ``::`` substring before matching, so a
        goal containing both produced no target at all.
        """
        assert derive_target("improve pkg.mod for :: reasons") == ("pkg.mod", "")

    def test_function_half_excludes_trailing_dot(self):
        """Verify a prose period after the function name stays out of the captured name."""
        assert derive_target("fix pkg.mod::run. then rerun") == ("pkg.mod", "run")


class TestMain:
    """Main emits shell assignments for eval."""

    def test_emits_quoted_assignments(self, capsys):
        """Verify both variables are emitted, shell-quoted, in a fixed order."""
        assert main(["extend auth.tokens::refresh"]) == 0
        assert capsys.readouterr().out == "TARGET_MODULE=auth.tokens\nTARGET_FN=refresh\n"

    def test_empty_target_emits_empty_quoted_values(self, capsys):
        """Verify an unrecognised goal still emits both assignments, so eval leaves no stale value."""
        assert main(["fix the login timeout"]) == 0
        assert capsys.readouterr().out == "TARGET_MODULE=''\nTARGET_FN=''\n"

    def test_dash_leading_goal_is_not_an_option(self, capsys):
        """Verify a goal starting with a dash reaches derive_target instead of argparse."""
        assert main(["--issue", "42", "fix", "pkg.mod::run"]) == 0
        assert "TARGET_MODULE=pkg.mod" in capsys.readouterr().out

    def test_help_blob_is_goal_text(self, capsys):
        """Verify ``--help`` is treated as goal text, never as a request for argparse help.

        Callers wrap this script in ``eval "$(...)"``: argparse help on stdout would be executed as shell source and
        leave both variables unset.
        """
        assert main(["--help"]) == 0
        assert capsys.readouterr().out == "TARGET_MODULE=''\nTARGET_FN=''\n"

    def test_emits_quoted_empty_value_beside_a_hostile_goal(self, capsys):
        """Verify the whole emitted line pair stays shell-quoted for a goal carrying metacharacters.

        The assertion is on exact stdout rather than on the module name alone. The regexes cannot carry ``;`` or a space
        into a captured name, so any assertion about the module half holds whether or not ``shlex.quote`` is applied —
        it is the empty ``TARGET_FN`` that proves the quoting actually ran.
        """
        assert main(["fix pkg.mod;rm -rf /"]) == 0
        assert capsys.readouterr().out == "TARGET_MODULE=pkg.mod\nTARGET_FN=''\n"


def test_qualified_regex_matches_sibling():
    """Verify the ``module::function`` pattern stays identical to parse_target_qname.py's.

    Both scripts live in this plugin's ``bin/`` and recognise the same user-facing spelling
    from the same argument blob, with no shared source between them. An edit to one that is
    not mirrored in the other would silently split the spelling in two.
    """
    assert derive_codemap_target._QUALIFIED.pattern == parse_target_qname._QNAME_RE.pattern

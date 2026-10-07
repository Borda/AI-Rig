"""Tests for derive_codemap_target.py — goal-text codemap target derivation."""

from __future__ import annotations

import derive_codemap_target
import parse_target_qname
import pytest
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
            # the two spellings are searched in a fixed order, not by position, so a dotted module mentioned first
            # never shadows an explicit ``module::function`` target
            pytest.param(
                "touch a.b then fix pkg.mod::run", ("pkg.mod", "run"), id="qualified-wins-over-earlier-dotted"
            ),
            pytest.param("move pkg.one into pkg.two", ("pkg.one", ""), id="leftmost-dotted-wins"),
            # the shell branch this replaced tested for the ``::`` substring before matching, so a goal containing both
            # produced no target at all
            pytest.param(
                "improve pkg.mod for :: reasons", ("pkg.mod", ""), id="bare-double-colon-falls-through-to-dotted"
            ),
            pytest.param("fix pkg.mod::run. then rerun", ("pkg.mod", "run"), id="function-half-excludes-trailing-dot"),
        ],
    )
    def test_recognised_spellings(self, goal, expected):
        """Verify each goal spelling resolves to its documented target pair.

        The qualified spelling outranks a dotted name, the leftmost dotted name wins, a bare ``::`` in prose does not
        suppress the dotted fallback, and a prose period after the function name stays out of the captured name.
        """
        assert derive_target(goal) == expected


class TestMain:
    """Main emits shell assignments for eval."""

    @pytest.mark.parametrize(
        ("argv", "expected_out"),
        [
            pytest.param(
                ["extend auth.tokens::refresh"],
                "TARGET_MODULE=auth.tokens\nTARGET_FN=refresh\n",
                id="both-variables-quoted-in-fixed-order",
            ),
            # an unrecognised goal still emits both assignments, so eval leaves no stale value
            pytest.param(["fix the login timeout"], "TARGET_MODULE=''\nTARGET_FN=''\n", id="empty-target-empty-values"),
            # callers wrap this script in ``eval "$(...)"``: argparse help on stdout would be executed as shell source
            # and leave both variables unset
            pytest.param(["--help"], "TARGET_MODULE=''\nTARGET_FN=''\n", id="help-blob-is-goal-text"),
            # the assertion is on exact stdout rather than on the module name alone. The regexes cannot carry ``;`` or a
            # space into a captured name, so any assertion about the module half holds whether or not ``shlex.quote``
            # is applied — it is the empty ``TARGET_FN`` that proves the quoting actually ran
            pytest.param(
                ["fix pkg.mod;rm -rf /"],
                "TARGET_MODULE=pkg.mod\nTARGET_FN=''\n",
                id="quoted-empty-value-beside-a-hostile-goal",
            ),
        ],
    )
    def test_emits_shell_quoted_assignments(self, capsys, argv, expected_out):
        """Verify main emits both variables as shell-quoted assignments in a fixed order and exits 0.

        An unrecognised goal still emits both assignments; ``--help`` is treated as goal text, never as a request for
        argparse help; and a goal carrying shell metacharacters keeps the whole emitted line pair quoted.
        """
        assert main(argv) == 0
        assert capsys.readouterr().out == expected_out

    def test_dash_leading_goal_is_not_an_option(self, capsys):
        """Verify a goal starting with a dash reaches derive_target instead of argparse."""
        assert main(["--issue", "42", "fix", "pkg.mod::run"]) == 0
        assert "TARGET_MODULE=pkg.mod" in capsys.readouterr().out


def test_qualified_regex_matches_sibling():
    """Verify the ``module::function`` pattern stays identical to parse_target_qname.py's.

    Both scripts live in this plugin's ``bin/`` and recognise the same user-facing spelling
    from the same argument blob, with no shared source between them. An edit to one that is
    not mirrored in the other would silently split the spelling in two.
    """
    assert derive_codemap_target._QUALIFIED.pattern == parse_target_qname._QNAME_RE.pattern

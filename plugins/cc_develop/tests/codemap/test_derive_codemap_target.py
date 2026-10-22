"""Tests for derive_codemap_target.py — goal-text codemap target derivation."""

from __future__ import annotations

import shutil
import stat
import subprocess
import sys

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
    """Main preserves the legacy shell-assignment interface alongside session state."""

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
            # Legacy shell-assignment consumers require --help to remain goal text, without a help banner.
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


def test_abbreviated_keep_state_stays_goal_text(tmp_path, monkeypatch, capsys):
    """`--keep` is goal text, not an abbreviation of `--keep-state`, so no session value is written for it.

    A goal that happens to start with those characters must not silently switch the script from printing assignments to
    writing sentinel files under an attacker- or typo-chosen prefix.
    """
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "abbrev-test")
    assert main(["--keep", "x", "pkg.a::b"]) == 0
    assert "TARGET_MODULE=" in capsys.readouterr().out
    assert not list(tmp_path.glob("x-*"))


def test_qualified_regex_matches_sibling():
    """Verify the ``module::function`` pattern stays identical to parse_target_qname.py's.

    Both scripts live in this plugin's ``bin/`` and recognise the same user-facing spelling
    from the same argument blob, with no shared source between them. An edit to one that is
    not mirrored in the other would silently split the spelling in two.
    """
    assert derive_codemap_target._QUALIFIED.pattern == parse_target_qname._QNAME_RE.pattern


@pytest.mark.parametrize(
    ("goal", "expected"),
    [
        pytest.param("extend pkg.mod::run", ("pkg.mod", "run"), id="qualified-state"),
        pytest.param("$(touch pwned)", ("", ""), id="inert-no-target"),
    ],
)
def test_keep_state_writes_private_values(tmp_path, monkeypatch, capsys, goal, expected):
    """State mode writes both values, including empty values that clear stale state, without shell source."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "codemap-test")
    for name in ("target-module", "target-fn"):
        (tmp_path / f"target-{name}-codemap-test").write_text("stale\n", encoding="utf-8")
    assert main(["--keep-state", "target", goal]) == 0
    assert capsys.readouterr().out == ""
    for name, value in zip(("target-module", "target-fn"), expected, strict=True):
        path = tmp_path / f"target-{name}-codemap-test"
        assert path.read_bytes() == (value + "\n").encode()
        if sys.platform != "win32":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not (tmp_path / "pwned").exists()


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required for the documented sentinel reader")
def test_keep_state_bash_read_round_trip(tmp_path, monkeypatch):
    """Real shell reads preserve values and clear an empty function instead of retaining stale state."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "codemap-test")
    assert main(["--keep-state", "target", "pkg.mod $(touch pwned)"]) == 0
    result = subprocess.run(
        [
            shutil.which("bash"),
            "-c",
            'IFS= read -r MOD < "$1" || MOD=""; IFS= read -r FN < "$2" || FN=""; printf "%s\\n%s\\n" "$MOD" "$FN"',
            "reader",
            str(tmp_path / "target-target-module-codemap-test"),
            str(tmp_path / "target-target-fn-codemap-test"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode, result.stdout) == (0, "pkg.mod\n\n")
    assert not (tmp_path / "pwned").exists()

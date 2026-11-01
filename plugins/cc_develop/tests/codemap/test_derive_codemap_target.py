"""Tests for derive_codemap_target.py — goal-text codemap target derivation."""

from __future__ import annotations

import shutil
import stat
import subprocess
import sys
from pathlib import Path

import derive_codemap_target
import parse_target_qname
import pytest
from derive_codemap_target import derive_target, main

_STATE_NAMES = ("module", "fn")


@pytest.fixture(name="session_tmp")
def _session_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``TMPDIR`` at ``tmp_path`` and fix the session token so state files land in a private directory."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "codemap-test")
    return tmp_path


def _state_path(directory: Path, name: str) -> Path:
    """Return the session state file holding the ``target-<name>`` value."""
    return directory / f"target-target-{name}-codemap-test"


def _read_state(directory: Path) -> dict[str, tuple[bytes, int]]:
    """Return each state file's bytes and permission bits (Windows has no POSIX modes, so it reports ``0o600``)."""
    return {
        name: (
            _state_path(directory, name).read_bytes(),
            stat.S_IMODE(_state_path(directory, name).stat().st_mode) if sys.platform != "win32" else 0o600,
        )
        for name in _STATE_NAMES
    }


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
        """Each goal spelling resolves to its documented target pair.

        The qualified spelling outranks a dotted name, the leftmost dotted name wins, a bare ``::`` in prose does not
        suppress the dotted fallback, and a prose period after the function name stays out of the captured name.
        """
        assert derive_target(goal) == expected

    def test_qualified_pattern_matches_sibling_script(self):
        """The ``module::function`` pattern stays identical to ``parse_target_qname.py``'s.

        Both scripts live in this plugin's ``bin/`` and recognise the same user-facing spelling from the same argument
        blob, with no shared source between them. An edit to one that is not mirrored in the other would silently
        split the spelling in two.
        """
        assert derive_codemap_target._QUALIFIED.pattern == parse_target_qname._QNAME_RE.pattern


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
        """Both variables are emitted as shell-quoted assignments in a fixed order, exit 0.

        An unrecognised goal still emits both assignments; ``--help`` is treated as goal text, never as a request for
        argparse help; and a goal carrying shell metacharacters keeps the whole emitted line pair quoted.
        """
        assert main(argv) == 0
        assert capsys.readouterr().out == expected_out

    def test_dash_leading_goal_is_not_an_option(self, capsys):
        """A goal starting with a dash reaches ``derive_target`` instead of argparse."""
        assert main(["--issue", "42", "fix", "pkg.mod::run"]) == 0
        assert "TARGET_MODULE=pkg.mod" in capsys.readouterr().out


class TestKeepState:
    """``--keep-state`` writes private session files instead of printing shell assignments."""

    def test_abbreviated_flag_stays_goal_text(self, session_tmp, capsys):
        """``--keep`` is goal text, not an abbreviation of ``--keep-state``, so no session value is written for it.

        A goal that happens to start with those characters must not silently switch the script from printing assignments
        to writing sentinel files under an attacker- or typo-chosen prefix.
        """
        assert main(["--keep", "x", "pkg.a::b"]) == 0
        assert "TARGET_MODULE=" in capsys.readouterr().out
        assert not list(session_tmp.glob("x-*"))

    @pytest.mark.parametrize(
        ("goal", "module", "fn"),
        [
            pytest.param("extend pkg.mod::run", "pkg.mod", "run", id="qualified-state"),
            pytest.param("$(touch pwned)", "", "", id="inert-no-target"),
        ],
    )
    def test_writes_private_values(self, session_tmp, capsys, goal, module, fn):
        """State mode overwrites stale values with both new ones, empty included, in owner-only files.

        An empty value must still be written so a stale target never survives, and a hostile goal is stored as inert
        data: nothing is executed and nothing reaches stdout.
        """
        for name in _STATE_NAMES:
            _state_path(session_tmp, name).write_text("stale\n", encoding="utf-8")

        assert main(["--keep-state", "target", goal]) == 0

        assert capsys.readouterr().out == ""
        assert _read_state(session_tmp) == {
            "module": (f"{module}\n".encode(), 0o600),
            "fn": (f"{fn}\n".encode(), 0o600),
        }
        assert not (session_tmp / "pwned").exists()

    @pytest.mark.integration
    @pytest.mark.skipif(shutil.which("bash") is None, reason="bash required for the documented sentinel reader")
    def test_bash_read_round_trip(self, session_tmp):
        """A real shell reads the stored values back unchanged and sees an empty function, not stale state.

        The consuming skills read these files with ``IFS= read -r``; a goal carrying ``$(...)`` must come back as plain
        text and must not run.
        """
        assert main(["--keep-state", "target", "pkg.mod $(touch pwned)"]) == 0

        result = subprocess.run(
            [
                shutil.which("bash"),
                "-c",
                'IFS= read -r MOD < "$1" || MOD=""; IFS= read -r FN < "$2" || FN=""; printf "%s\\n%s\\n" "$MOD" "$FN"',
                "reader",
                str(_state_path(session_tmp, "module")),
                str(_state_path(session_tmp, "fn")),
            ],
            cwd=session_tmp,
            capture_output=True,
            text=True,
            check=False,
        )

        assert (result.returncode, result.stdout) == (0, "pkg.mod\n\n")
        assert not (session_tmp / "pwned").exists()

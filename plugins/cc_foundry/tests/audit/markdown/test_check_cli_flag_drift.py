"""Tests for check_cli_flag_drift — documented flag vs argparse drift detector (Check 42)."""

from __future__ import annotations

from pathlib import Path

import pytest
from check_cli_flag_drift import (
    ORIGIN_DOCSTRING,
    DriftFinding,
    command_scope,
    extract_argparse_flags,
    find_drift,
    iter_argparse_scripts,
    main,
    usage_block_lines,
)


def _make_script(base: Path, plugin: str, name: str, body: str) -> Path:
    """Write a bin/ script under ``base/<plugin>/bin/<name>`` and return its path.

    Examples:
        >>> from tempfile import TemporaryDirectory
        >>> with TemporaryDirectory() as directory:
        ...     path = _make_script(Path(directory), "demo", "tool.py", "print('ok')\\n")
        ...     (path.as_posix().endswith("demo/bin/tool.py"), path.read_text() == "print('ok')\\n")
        (True, True)
    """
    bin_dir = base / plugin / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / name
    path.write_text(body)
    return path


def _make_skill(base: Path, plugin: str, skill: str, body: str) -> Path:
    """Write a SKILL.md under ``base/<plugin>/skills/<skill>/SKILL.md`` and return it.

    Examples:
        >>> from tempfile import TemporaryDirectory
        >>> with TemporaryDirectory() as directory:
        ...     path = _make_skill(Path(directory), "demo", "audit", "# Audit\\n")
        ...     (path.as_posix().endswith("demo/skills/audit/SKILL.md"), path.read_text() == "# Audit\\n")
        (True, True)
    """
    skill_dir = base / plugin / "skills" / skill
    skill_dir.mkdir(parents=True, exist_ok=True)
    path = skill_dir / "SKILL.md"
    path.write_text(body)
    return path


def _docstring_script(usage: str, prose: str = "") -> str:
    """Build a bin/ script whose docstring carries ``usage`` inside a Usage: block.

    Examples:
        >>> "Usage:\\npython tool.py --help" in _docstring_script("python tool.py --help")
        True
    """
    return f'"""tool.py — a summary.\n\n{prose}Usage:\n{usage}\n"""\n{_ARGPARSE_BODY}'


_ARGPARSE_BODY = (
    "import argparse\n"
    "def main():\n"
    "    p = argparse.ArgumentParser()\n"
    "    p.add_argument('--real')\n"
    "    p.add_argument('-r', '--recurse')\n"
)
_ARGPARSE_FLAGS = {"--real", "-r", "--recurse", "-h", "--help"}
_GHOST_SKILL = 'python "${ROOT}/bin/tool.py" --ghost\n'
_REAL_SKILL = 'python "${ROOT}/bin/tool.py" --real\n'


class TestExtractArgparseFlags:
    """Covers the static scan of a script's ``add_argument`` calls."""

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            pytest.param(_ARGPARSE_BODY, _ARGPARSE_FLAGS, id="long-short-and-implicit-help"),
            pytest.param(
                "common = argparse.ArgumentParser(add_help=False)\n"
                "common.add_argument('--real')\n"
                "parser = argparse.ArgumentParser(parents=[common])\n",
                {"--real", "-h", "--help"},
                id="help-less-parent-keeps-help-on-main-parser",
            ),
            pytest.param(
                "p = argparse.ArgumentParser(add_help=False)\np.add_argument('--real')\n",
                {"--real"},
                id="add-help-false-drops-help-flags",
            ),
            pytest.param("p.add_argument('target')\n", {"-h", "--help"}, id="positional-only-keeps-implicit-help"),
            pytest.param("x = 1\n", None, id="no-add-argument"),
            pytest.param(
                "import argparse\np.add_argument('extras', nargs=argparse.REMAINDER)\n", None, id="remainder-nargs"
            ),
            pytest.param("p.add_argument('extras', nargs='...')\n", None, id="ellipsis-nargs"),
        ],
    )
    def test_collects_defined_flags(self, source: str, expected: set[str] | None) -> None:
        """The set of flags a script's parser accepts is read from its source, or ``None`` when not enumerable.

        Registered flags plus the ``-h``/``--help`` pair argparse adds by default are defined; a shared
        ``add_help=False`` parent does not take ``--help`` from the child parser, but ``add_help=False`` on the parser
        itself does. A script without ``add_argument`` or whose ``nargs`` swallows the remaining arguments (REMAINDER or
        ``'...'``) reports ``None`` rather than an empty set.
        """
        assert extract_argparse_flags(source) == expected


class TestCommandScope:
    """Covers how far a documented invocation's flags extend across quoting, continuations and separators."""

    @pytest.mark.parametrize(
        ("lines", "kept", "dropped"),
        [
            pytest.param(["x --a --b"], ("--a", "--b"), (), id="single-line-flags"),
            pytest.param(["x --a \\", "  --b"], ("--a", "--b"), (), id="follows-line-continuation"),
            pytest.param(["x --a", "other --b"], ("--a",), ("--b",), id="stops-at-next-uncontinued-line"),
            pytest.param(["x --a | grep --b"], ("--a",), ("--b",), id="truncates-at-pipe"),
            # Flags after a standalone `--` belong to the wrapped command, not the documented script.
            pytest.param(["x --a -- --tb=short -v"], ("--a",), ("--tb",), id="truncates-at-end-of-options-separator"),
            # `tool.py --label "a -- b" --phantom` has no standalone separator; cutting at the quoted one would hide it.
            pytest.param(
                ['x --label "a -- b" --phantom'], ("--phantom",), (), id="separator-inside-quoted-value-keeps-scope"
            ),
            # `--label "a \" -- b" --bogus` is one quoted value then a flag; the escaped quote must not close the value.
            pytest.param(
                ['x --label "a \\" -- b" --bogus'], ("--bogus",), (), id="escaped-quote-inside-quotes-keeps-value-open"
            ),
            # `--label \" -- --child` passes a literal quote then the separator; an opening quote would keep `--child`.
            pytest.param(
                ['x --label \\" -- --child "value"'], (), ("--child",), id="escaped-quote-outside-quotes-opens-nothing"
            ),
            # `$(...)` inside double quotes runs its own command, so its flags are not the script's own.
            pytest.param(
                ['x --label "$(probe --child)" --valid'], (), ("--child",), id="command-substitution-ends-the-scope"
            ),
            pytest.param(["x --label a\\ -- --phantom"], ("--phantom",), (), id="escaped-space-is-not-a-separator"),
            pytest.param(["x --a --b-c"], ("--b-c",), (), id="dashes-inside-flag-are-not-a-separator"),
            # ANSI-C quoting honours backslash escapes, unlike plain single quotes: `$'it\'s -- text'` is one value.
            pytest.param(
                ["x --label $'it\\'s -- text' --phantom"], ("--phantom",), (), id="ansi-c-quoted-escaped-quote"
            ),
            # The shell reads a backslash inside single quotes literally: `'C:\'` closes at its second quote.
            pytest.param(
                ["x --root 'C:\\' --label 'a -- b' --phantom"], ("--phantom",), (), id="backslash-in-single-quotes"
            ),
        ],
    )
    def test_scope_keeps_own_flags_and_drops_foreign_ones(
        self, lines: list[str], kept: tuple[str, ...], dropped: tuple[str, ...]
    ) -> None:
        """Flags that belong to the documented script stay in scope; flags of piped or wrapped commands leave it.

        The drift check only judges flags inside the scope, so an over-long scope reports another tool's flags as drift
        and an over-short one hides a genuinely undefined flag.
        """
        scope = command_scope(lines, 0, 1)

        assert all(flag in scope for flag in kept)
        assert not any(flag in scope for flag in dropped)


class TestIterArgparseScripts:
    """Covers discovery of ``bin/`` scripts that define argparse flags."""

    @pytest.mark.parametrize(
        ("name", "body", "expected"),
        [
            pytest.param("tool.py", _ARGPARSE_BODY, {"tool.py": _ARGPARSE_FLAGS}, id="maps-basename-to-flags"),
            pytest.param("_helper.py", _ARGPARSE_BODY, {}, id="skips-underscore-private"),
            pytest.param("plain.py", "print('hi')\n", {}, id="skips-non-argparse-script"),
        ],
    )
    def test_maps_public_argparse_scripts(
        self, tmp_path: Path, name: str, body: str, expected: dict[str, set[str]]
    ) -> None:
        """Only public scripts with enumerable argparse flags appear, keyed by basename.

        A private ``_``-prefixed helper and a script without any parser are not documentation targets.
        """
        _make_script(tmp_path, "myplugin", name, body)

        assert iter_argparse_scripts(tmp_path) == expected


class TestFindDrift:
    """Covers drift between documented invocations and the script's real flags."""

    def test_documented_flag_missing_from_parser_is_a_finding(self, tmp_path: Path) -> None:
        """A documented flag not in the script's argparse is a finding naming the flag and script.

        A skill documents ``tool.py --ghost`` while the parser defines only ``--real`` and ``--recurse``.
        """
        _make_script(tmp_path, "myplugin", "tool.py", _ARGPARSE_BODY)
        _make_skill(tmp_path, "myplugin", "sk", _GHOST_SKILL)

        findings = find_drift(tmp_path)

        assert [f.flag for f in findings] == ["--ghost"]
        assert findings[0].script == "tool.py"
        assert isinstance(findings[0], DriftFinding)

    @pytest.mark.parametrize(
        ("script_body", "skill_body"),
        [
            pytest.param(_ARGPARSE_BODY, _REAL_SKILL, id="real-flag"),
            pytest.param(_ARGPARSE_BODY, "This skill does things.\n", id="script-flag-never-mentioned"),
            pytest.param(
                _ARGPARSE_BODY, "`tool.py` runs. Then `git log --ghost`.\n", id="prose-mention-anchors-no-flags"
            ),
            pytest.param(_ARGPARSE_BODY, 'python "${ROOT}/bin/tool.py" | grep --color\n', id="piped-command-flags"),
            pytest.param(_ARGPARSE_BODY, 'python "${ROOT}/bin/tool.py" -q\n', id="short-flag-near-invocation"),
            pytest.param(
                _ARGPARSE_BODY,
                'X=$(python "${ROOT}/bin/tool.py" --real) \\\n  && [ -z "$X" ] && [ -f out ]\n',
                id="bash-test-operators",
            ),
            pytest.param(
                "import argparse\np.add_argument('extras', nargs=argparse.REMAINDER)\n",
                'python "${ROOT}/bin/tool.py" --anything\n',
                id="remainder-passthrough-script",
            ),
        ],
    )
    def test_documentation_without_phantom_flag_is_not_flagged(
        self, tmp_path: Path, script_body: str, skill_body: str
    ) -> None:
        """Documentation that names no flag the script lacks is never a finding.

        The scan is about accuracy, not completeness: a real flag, a flag never mentioned, a bare prose reference, flags
        after a pipe (they belong to the piped command), a short option or bash test operator near an invocation (they
        collide with shell syntax) and a REMAINDER passthrough script that accepts arbitrary flags all pass.
        """
        _make_script(tmp_path, "myplugin", "tool.py", script_body)
        _make_skill(tmp_path, "myplugin", "sk", skill_body)

        assert find_drift(tmp_path) == []


class TestUsageBlockLines:
    """Covers extraction of a script docstring's ``Usage:`` block."""

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            pytest.param(
                _docstring_script("    tool.py --real\n\n    tool.py --recurse\n\nNotes:\n    --ghost"),
                ["", "tool.py --real", "", "tool.py --recurse", ""],
                id="block-stops-at-next-section",
            ),
            pytest.param('"""tool.py — a summary, no usage."""\n', [], id="no-usage-section"),
        ],
    )
    def test_returns_usage_block_lines(self, source: str, expected: list[str]) -> None:
        """The block ends where the next docstring section begins, not at the first blank line.

        A docstring without a Usage: section contributes nothing to scan.
        """
        assert [line.strip() for line in usage_block_lines(source)] == expected


class TestDocstringDrift:
    """Covers drift detection on a script's own docstring Usage block."""

    def test_phantom_flag_in_own_usage_block_is_a_finding(self, tmp_path: Path) -> None:
        """Catch a script advertising a flag its parser lacks.

        This is the shape that previously escaped detection and was then copied into four documents:
        the phantom was advertised by the script's own docstring, which nothing validated.
        """
        _make_script(tmp_path, "myplugin", "tool.py", _docstring_script("    tool.py --check"))

        findings = find_drift(tmp_path)

        assert [(f.flag, f.script, f.origin) for f in findings] == [("--check", "tool.py", ORIGIN_DOCSTRING)]
        assert findings[0].document.endswith("myplugin/bin/tool.py")

    @pytest.mark.parametrize(
        "source",
        [
            pytest.param(_docstring_script("    tool.py --real --recurse"), id="real-flags-in-usage-block"),
            pytest.param(
                _docstring_script(
                    "    tool.py --real",
                    prose="It runs ``pytest --tb=short`` and parses ``--root`` out of $ARGUMENTS.\n\n",
                ),
                id="other-tool-flags-in-summary-prose",
            ),
            pytest.param(_docstring_script("    other-cli query --top 100 | tool.py --real"), id="piped-producer-flag"),
            pytest.param(
                '"""tool.py — a summary.\n\nUsage:\n    tool.py --anything\n"""\n'
                "import argparse\np = argparse.ArgumentParser()\np.add_argument('x', nargs=argparse.REMAINDER)\n",
                id="remainder-passthrough-accepts-any-flag",
            ),
        ],
    )
    def test_docstring_naming_no_phantom_flag_is_not_a_finding(self, tmp_path: Path, source: str) -> None:
        """A docstring whose own Usage block names only real flags yields no finding.

        Discrimination against the phantom-flag case: a Usage block of real flags is accepted, prose naming another
        tool's flag is not this script's CLI (the dominant false positive), a Usage line piping another command in owns
        its own flags up to the pipe, and a REMAINDER passthrough accepts arbitrary flags so it cannot drift.
        """
        _make_script(tmp_path, "myplugin", "tool.py", source)

        assert find_drift(tmp_path) == []

    def test_same_basename_in_two_plugins_is_judged_per_copy(self, tmp_path: Path) -> None:
        """One plugin's real flag must not excuse a phantom in another plugin's same-named copy.

        ``--check`` is real in beta and absent from alpha; neither copy documents the other's flags, so both are clean.
        """
        _make_script(tmp_path, "alpha", "tool.py", _docstring_script("    tool.py --real"))
        beta_body = (
            '"""tool.py — a summary.\n\nUsage:\n    tool.py --check\n"""\n'
            "import argparse\np = argparse.ArgumentParser()\np.add_argument('--check')\n"
        )
        _make_script(tmp_path, "beta", "tool.py", beta_body)

        assert find_drift(tmp_path) == []

    def test_cli_exits_1_and_names_the_docstring_origin(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The finding line must say the docstring documented it, so the reader opens the right file."""
        monkeypatch.chdir(tmp_path)
        _make_script(tmp_path, "myplugin", "tool.py", _docstring_script("    tool.py --check"))

        rc = main(["--plugins-dir", str(tmp_path)])

        assert rc == 1
        out = capsys.readouterr().out
        assert ORIGIN_DOCSTRING in out
        assert "--check" in out


class TestNoExecution:
    """Covers the guarantee that audited scripts are parsed, never run."""

    def test_target_script_side_effect_never_triggered(self, tmp_path: Path) -> None:
        """The checker must AST-parse, never import/execute, the target script.

        The audited script writes a sentinel file at import time; the file must stay absent after a full drift scan.
        """
        sentinel = tmp_path / "SIDE_EFFECT"
        body = (
            "import argparse\n"
            f"open({str(sentinel)!r}, 'w').write('x')\n"
            "p = argparse.ArgumentParser()\n"
            "p.add_argument('--real')\n"
        )
        _make_script(tmp_path, "myplugin", "tool.py", body)
        _make_skill(tmp_path, "myplugin", "sk", _GHOST_SKILL)

        find_drift(tmp_path)

        assert not sentinel.exists()


class TestMain:
    """Covers the command-line contract and exit codes."""

    def test_exit_0_when_clean(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Documentation that matches the parser exits 0 with the pass marker."""
        monkeypatch.chdir(tmp_path)
        _make_script(tmp_path, "myplugin", "tool.py", _ARGPARSE_BODY)
        _make_skill(tmp_path, "myplugin", "sk", _REAL_SKILL)

        rc = main(["--plugins-dir", str(tmp_path)])

        assert rc == 0
        assert "✓" in capsys.readouterr().out

    def test_exit_1_when_drift(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A documented flag the parser lacks exits 1 and prints the Check 42 warning with the flag."""
        monkeypatch.chdir(tmp_path)
        _make_script(tmp_path, "myplugin", "tool.py", _ARGPARSE_BODY)
        _make_skill(tmp_path, "myplugin", "sk", _GHOST_SKILL)

        rc = main(["--plugins-dir", str(tmp_path)])

        assert rc == 1
        out = capsys.readouterr().out
        assert "⚠ 42" in out
        assert "--ghost" in out

    def test_exit_2_for_missing_plugins_dir(self, tmp_path: Path) -> None:
        """An explicit ``--plugins-dir`` that does not exist is an argument error."""
        assert main(["--plugins-dir", str(tmp_path / "missing")]) == 2

    def test_exit_2_when_default_dir_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without ``--plugins-dir`` the default ``plugins/`` is resolved from cwd; its absence is an argument error."""
        monkeypatch.chdir(tmp_path)

        assert main([]) == 2

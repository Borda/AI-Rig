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


_ARGPARSE_BODY = (
    "import argparse\n"
    "def main():\n"
    "    p = argparse.ArgumentParser()\n"
    "    p.add_argument('--real')\n"
    "    p.add_argument('-r', '--recurse')\n"
)


# ---------------------------------------------------------------------------
# extract_argparse_flags
# ---------------------------------------------------------------------------


class TestExtractArgparseFlags:
    def test_collects_long_and_short_flags(self) -> None:
        flags = extract_argparse_flags(_ARGPARSE_BODY)
        assert flags == {"--real", "-r", "--recurse"}

    @pytest.mark.parametrize(
        "source",
        [
            pytest.param("x = 1\n", id="no-add-argument"),
            pytest.param("import argparse\np.add_argument('extras', nargs=argparse.REMAINDER)\n", id="remainder-nargs"),
            pytest.param("p.add_argument('extras', nargs='...')\n", id="ellipsis-nargs"),
        ],
    )
    def test_none_when_flags_are_not_enumerable(self, source: str) -> None:
        """No argparse flags can be enumerated without add_argument calls or with a passthrough REMAINDER.

        A script lacking add_argument and one whose ``nargs`` swallows the remaining arguments (REMAINDER or ``'...'``)
        both report ``None`` rather than an empty flag set.
        """
        assert extract_argparse_flags(source) is None

    def test_positional_only_yields_empty_set(self) -> None:
        assert extract_argparse_flags("p.add_argument('target')\n") == set()


# ---------------------------------------------------------------------------
# command_scope
# ---------------------------------------------------------------------------


class TestCommandScope:
    def test_single_line_flags(self) -> None:
        scope = command_scope(["x --a --b"], 0, 1)
        assert "--a" in scope
        assert "--b" in scope

    def test_follows_line_continuation(self) -> None:
        scope = command_scope(["x --a \\", "  --b"], 0, 1)
        assert "--a" in scope
        assert "--b" in scope

    def test_stops_at_next_uncontinued_line(self) -> None:
        scope = command_scope(["x --a", "other --b"], 0, 1)
        assert "--a" in scope
        assert "--b" not in scope

    def test_truncates_at_pipe_boundary(self) -> None:
        scope = command_scope(["x --a | grep --b"], 0, 1)
        assert "--a" in scope
        assert "--b" not in scope


# ---------------------------------------------------------------------------
# iter_argparse_scripts
# ---------------------------------------------------------------------------


class TestIterArgparseScripts:
    def test_maps_basename_to_flags(self, tmp_path: Path) -> None:
        _make_script(tmp_path, "myplugin", "tool.py", _ARGPARSE_BODY)
        scripts = iter_argparse_scripts(tmp_path)
        assert scripts == {"tool.py": {"--real", "-r", "--recurse"}}

    def test_skips_underscore_private(self, tmp_path: Path) -> None:
        _make_script(tmp_path, "myplugin", "_helper.py", _ARGPARSE_BODY)
        assert iter_argparse_scripts(tmp_path) == {}

    def test_skips_non_argparse_script(self, tmp_path: Path) -> None:
        _make_script(tmp_path, "myplugin", "plain.py", "print('hi')\n")
        assert iter_argparse_scripts(tmp_path) == {}


# ---------------------------------------------------------------------------
# find_drift
# ---------------------------------------------------------------------------


class TestFindDrift:
    def test_drift_flag_is_detected(self, tmp_path: Path) -> None:
        """A documented flag not in the script's argparse is a finding."""
        _make_script(tmp_path, "myplugin", "tool.py", _ARGPARSE_BODY)
        _make_skill(tmp_path, "myplugin", "sk", 'python "${ROOT}/bin/tool.py" --ghost\n')
        findings = find_drift(tmp_path)
        assert [f.flag for f in findings] == ["--ghost"]
        assert findings[0].script == "tool.py"
        assert isinstance(findings[0], DriftFinding)

    @pytest.mark.parametrize(
        ("script_body", "skill_body"),
        [
            pytest.param(_ARGPARSE_BODY, 'python "${ROOT}/bin/tool.py" --real\n', id="real-flag"),
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


# ---------------------------------------------------------------------------
# module-docstring Usage: block
# ---------------------------------------------------------------------------


def _docstring_script(usage: str, prose: str = "") -> str:
    """Build a bin/ script whose docstring carries ``usage`` inside a Usage: block.

    Examples:
        >>> "Usage:\\npython tool.py --help" in _docstring_script("python tool.py --help")
        True
    """
    return f'"""tool.py — a summary.\n\n{prose}Usage:\n{usage}\n"""\n{_ARGPARSE_BODY}'


class TestUsageBlockLines:
    def test_block_stops_at_next_section(self) -> None:
        """The block ends where the next docstring section begins, not at the first blank line."""
        source = _docstring_script("    tool.py --real\n\n    tool.py --recurse\n\nNotes:\n    --ghost")
        assert [line.strip() for line in usage_block_lines(source)] == [
            "",
            "tool.py --real",
            "",
            "tool.py --recurse",
            "",
        ]

    def test_no_usage_section_yields_no_lines(self) -> None:
        """A docstring without a Usage: section contributes nothing to scan."""
        assert usage_block_lines('"""tool.py — a summary, no usage."""\n') == []


class TestDocstringDrift:
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
        ],
    )
    def test_docstring_naming_no_phantom_flag_is_not_a_finding(self, tmp_path: Path, source: str) -> None:
        """A docstring whose own Usage block names only real flags yields no finding.

        Discrimination against the phantom-flag case: a Usage block of real flags is accepted, prose naming another
        tool's flag is not this script's CLI (the dominant false positive), and a Usage line piping another command in
        owns its own flags up to the pipe.
        """
        _make_script(tmp_path, "myplugin", "tool.py", source)
        assert find_drift(tmp_path) == []

    def test_passthrough_script_docstring_never_drifts(self, tmp_path: Path) -> None:
        """A REMAINDER passthrough accepts arbitrary flags, so its docstring cannot drift."""
        body = "import argparse\np = argparse.ArgumentParser()\np.add_argument('x', nargs=argparse.REMAINDER)\n"
        source = f'"""tool.py — a summary.\n\nUsage:\n    tool.py --anything\n"""\n{body}'
        _make_script(tmp_path, "myplugin", "tool.py", source)
        assert find_drift(tmp_path) == []

    def test_same_basename_in_two_plugins_is_judged_per_copy(self, tmp_path: Path) -> None:
        """One plugin's real flag must not excuse a phantom in another plugin's same-named copy."""
        _make_script(tmp_path, "alpha", "tool.py", _docstring_script("    tool.py --real"))
        beta_body = (
            '"""tool.py — a summary.\n\nUsage:\n    tool.py --check\n"""\n'
            "import argparse\np = argparse.ArgumentParser()\np.add_argument('--check')\n"
        )
        _make_script(tmp_path, "beta", "tool.py", beta_body)

        # `--check` is real in beta and absent from alpha; neither copy documents the other's.
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


# ---------------------------------------------------------------------------
# no execution / import of target scripts
# ---------------------------------------------------------------------------


class TestNoExecution:
    def test_target_script_side_effect_never_triggered(self, tmp_path: Path) -> None:
        """The checker must AST-parse, never import/execute, the target script."""
        sentinel = tmp_path / "SIDE_EFFECT"
        body = (
            "import argparse\n"
            f"open({str(sentinel)!r}, 'w').write('x')\n"
            "p = argparse.ArgumentParser()\n"
            "p.add_argument('--real')\n"
        )
        _make_script(tmp_path, "myplugin", "tool.py", body)
        _make_skill(tmp_path, "myplugin", "sk", 'python "${ROOT}/bin/tool.py" --ghost\n')
        find_drift(tmp_path)
        assert not sentinel.exists()


# ---------------------------------------------------------------------------
# main (CLI)
# ---------------------------------------------------------------------------


class TestMain:
    def test_exit_0_when_clean(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _make_script(tmp_path, "myplugin", "tool.py", _ARGPARSE_BODY)
        _make_skill(tmp_path, "myplugin", "sk", 'python "${ROOT}/bin/tool.py" --real\n')
        rc = main(["--plugins-dir", str(tmp_path)])
        assert rc == 0
        assert "✓" in capsys.readouterr().out

    def test_exit_1_when_drift(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _make_script(tmp_path, "myplugin", "tool.py", _ARGPARSE_BODY)
        _make_skill(tmp_path, "myplugin", "sk", 'python "${ROOT}/bin/tool.py" --ghost\n')
        rc = main(["--plugins-dir", str(tmp_path)])
        assert rc == 1
        out = capsys.readouterr().out
        assert "⚠ 42" in out
        assert "--ghost" in out

    def test_exit_2_bad_dir(self) -> None:
        rc = main(["--plugins-dir", "/nonexistent/path/does/not/exist"])
        assert rc == 2

    def test_exit_2_default_dir_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        assert main([]) == 2

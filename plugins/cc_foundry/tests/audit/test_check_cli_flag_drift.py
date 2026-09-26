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

    def test_none_when_no_add_argument(self) -> None:
        assert extract_argparse_flags("x = 1\n") is None

    def test_positional_only_yields_empty_set(self) -> None:
        assert extract_argparse_flags("p.add_argument('target')\n") == set()

    def test_remainder_passthrough_returns_none(self) -> None:
        src = "import argparse\np.add_argument('extras', nargs=argparse.REMAINDER)\n"
        assert extract_argparse_flags(src) is None

    def test_ellipsis_nargs_returns_none(self) -> None:
        assert extract_argparse_flags("p.add_argument('extras', nargs='...')\n") is None


# ---------------------------------------------------------------------------
# command_scope
# ---------------------------------------------------------------------------


class TestCommandScope:
    def test_single_line_flags(self) -> None:
        scope = command_scope(["x --a --b"], 0, 1)
        assert "--a" in scope and "--b" in scope

    def test_follows_line_continuation(self) -> None:
        scope = command_scope(["x --a \\", "  --b"], 0, 1)
        assert "--a" in scope and "--b" in scope

    def test_stops_at_next_uncontinued_line(self) -> None:
        scope = command_scope(["x --a", "other --b"], 0, 1)
        assert "--a" in scope and "--b" not in scope

    def test_truncates_at_pipe_boundary(self) -> None:
        scope = command_scope(["x --a | grep --b"], 0, 1)
        assert "--a" in scope and "--b" not in scope


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

    def test_real_flag_not_flagged(self, tmp_path: Path) -> None:
        """A flag the script's argparse actually defines is never a finding."""
        _make_script(tmp_path, "myplugin", "tool.py", _ARGPARSE_BODY)
        _make_skill(tmp_path, "myplugin", "sk", 'python "${ROOT}/bin/tool.py" --real\n')
        assert find_drift(tmp_path) == []

    def test_script_flag_never_mentioned_not_flagged(self, tmp_path: Path) -> None:
        """A real argparse flag absent from all SKILL.md is not a finding (accuracy, not completeness)."""
        _make_script(tmp_path, "myplugin", "tool.py", _ARGPARSE_BODY)
        _make_skill(tmp_path, "myplugin", "sk", "This skill does things.\n")
        assert find_drift(tmp_path) == []

    def test_prose_mention_contributes_no_flags(self, tmp_path: Path) -> None:
        """A bare backtick reference (no invocation) never anchors a flag."""
        _make_script(tmp_path, "myplugin", "tool.py", _ARGPARSE_BODY)
        _make_skill(tmp_path, "myplugin", "sk", "`tool.py` runs. Then `git log --ghost`.\n")
        assert find_drift(tmp_path) == []

    def test_piped_command_flags_not_attributed(self, tmp_path: Path) -> None:
        """Flags after a pipe belong to the piped command, not the script."""
        _make_script(tmp_path, "myplugin", "tool.py", _ARGPARSE_BODY)
        _make_skill(tmp_path, "myplugin", "sk", 'python "${ROOT}/bin/tool.py" | grep --color\n')
        assert find_drift(tmp_path) == []

    def test_short_flag_in_docs_not_flagged(self, tmp_path: Path) -> None:
        """A single-dash short option near an invocation is ignored (collides with shell operators)."""
        _make_script(tmp_path, "myplugin", "tool.py", _ARGPARSE_BODY)
        _make_skill(tmp_path, "myplugin", "sk", 'python "${ROOT}/bin/tool.py" -q\n')
        assert find_drift(tmp_path) == []

    def test_bash_test_operators_not_flagged(self, tmp_path: Path) -> None:
        """Bash test operators (-z, -f, -n) on a continued command line are never findings."""
        _make_script(tmp_path, "myplugin", "tool.py", _ARGPARSE_BODY)
        body = 'X=$(python "${ROOT}/bin/tool.py" --real) \\\n  && [ -z "$X" ] && [ -f out ]\n'
        _make_skill(tmp_path, "myplugin", "sk", body)
        assert find_drift(tmp_path) == []

    def test_remainder_script_never_drifts(self, tmp_path: Path) -> None:
        """A passthrough (nargs=REMAINDER) script accepts arbitrary flags — no drift."""
        body = "import argparse\np.add_argument('extras', nargs=argparse.REMAINDER)\n"
        _make_script(tmp_path, "myplugin", "pass.py", body)
        _make_skill(tmp_path, "myplugin", "sk", 'python "${ROOT}/bin/pass.py" --anything\n')
        assert find_drift(tmp_path) == []

    def test_finding_is_dataclass(self, tmp_path: Path) -> None:
        _make_script(tmp_path, "myplugin", "tool.py", _ARGPARSE_BODY)
        _make_skill(tmp_path, "myplugin", "sk", 'python "${ROOT}/bin/tool.py" --ghost\n')
        assert isinstance(find_drift(tmp_path)[0], DriftFinding)


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

    def test_real_flag_in_own_usage_block_is_not_a_finding(self, tmp_path: Path) -> None:
        """Discrimination: the same scan must accept a Usage block naming only real flags."""
        _make_script(tmp_path, "myplugin", "tool.py", _docstring_script("    tool.py --real --recurse"))
        assert find_drift(tmp_path) == []

    def test_wrapped_tool_flag_in_summary_prose_is_not_a_finding(self, tmp_path: Path) -> None:
        """Prose naming another tool's flag is not this script's CLI — the dominant false positive."""
        source = _docstring_script(
            "    tool.py --real",
            prose="It runs ``pytest --tb=short`` and parses ``--root`` out of $ARGUMENTS.\n\n",
        )
        _make_script(tmp_path, "myplugin", "tool.py", source)
        assert find_drift(tmp_path) == []

    def test_piped_producer_flag_is_not_attributed_to_this_script(self, tmp_path: Path) -> None:
        """A Usage line piping another command in owns its own flags up to the pipe."""
        source = _docstring_script("    other-cli query --top 100 | tool.py --real")
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
        assert "⚠ 42" in out and "--ghost" in out

    def test_exit_2_bad_dir(self) -> None:
        rc = main(["--plugins-dir", "/nonexistent/path/does/not/exist"])
        assert rc == 2

    def test_exit_2_default_dir_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        assert main([]) == 2

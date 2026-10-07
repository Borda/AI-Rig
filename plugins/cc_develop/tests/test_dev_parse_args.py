"""Tests for dev_parse_args.py — develop-skill flag parser."""

from __future__ import annotations

from pathlib import Path

import dev_parse_args
import pytest
from dev_parse_args import SKILL_SPECS, FlagSpec, SpecType, extract_flags, main, parse_specs, run, write_skill_files

# ---------------------------------------------------------------------------
# parse_specs
# ---------------------------------------------------------------------------


class TestParseSpecs:
    """parse_specs converts token list into FlagSpec objects."""

    @pytest.mark.parametrize(
        ("tokens", "expected"),
        [
            pytest.param(
                ["--bool", "team", "TEAM_MODE", "false"],
                FlagSpec(kind=SpecType.BOOL, flag="team", var="TEAM_MODE", default="false"),
                id="bool",
            ),
            pytest.param(
                ["--neg-bool", "no-challenge", "CHALLENGE_ENABLED", "true"],
                FlagSpec(kind=SpecType.NEG_BOOL, flag="no-challenge", var="CHALLENGE_ENABLED", default="true"),
                id="neg-bool",
            ),
            pytest.param(
                ["--codemap", "CODEMAP_RAW", "auto"],
                FlagSpec(kind=SpecType.CODEMAP, flag="", var="CODEMAP_RAW", default="auto"),
                id="codemap-no-flag-token",
            ),
            pytest.param(
                ["--int", "max-depth", "MAX_DEPTH", "3"],
                FlagSpec(kind=SpecType.INT, flag="max-depth", var="MAX_DEPTH", default="3"),
                id="int-default-kept-as-string",
            ),
            pytest.param(
                ["--str", "plan", "PLAN_FILE", ""],
                FlagSpec(kind=SpecType.STR, flag="plan", var="PLAN_FILE", default=""),
                id="str-empty-default",
            ),
        ],
    )
    def test_spec_keyword_produces_flag_spec(self, tokens: list[str], expected: FlagSpec):
        """Each spec keyword produces one FlagSpec with the matching kind, flag, variable and default.

        --codemap takes only VAR + DEFAULT (no FLAG token) and --int keeps its default as a string; every other keyword
        takes KIND FLAG VAR DEFAULT.
        """
        specs = parse_specs(tokens)
        assert specs == [expected]

    def test_multiple_specs(self):
        """Multiple specs parsed in order."""
        specs = parse_specs(["--bool", "team", "TEAM_MODE", "false", "--codemap", "CODEMAP_RAW", "auto"])
        assert len(specs) == 2
        assert specs[0].kind == SpecType.BOOL
        assert specs[1].kind == SpecType.CODEMAP

    @pytest.mark.parametrize(
        "tokens",
        [
            pytest.param(["--unknown", "foo", "BAR", "baz"], id="unknown-keyword"),
            pytest.param(["--bool", "team"], id="insufficient-tokens"),
        ],
    )
    def test_malformed_spec_exits(self, tokens: list[str]):
        """An unknown spec keyword, or too few tokens after a keyword, calls sys.exit(1)."""
        with pytest.raises(SystemExit) as exc:
            parse_specs(tokens)
        assert exc.value.code == 1


# ---------------------------------------------------------------------------
# extract_flags — bool / neg-bool
# ---------------------------------------------------------------------------


class TestBoolFlags:
    """Boolean and negated-boolean flag extraction."""

    @pytest.mark.parametrize(
        ("spec_tokens", "var", "arguments", "expected_value", "expected_clean"),
        [
            pytest.param(
                ["--bool", "team", "S", "false"], "S", "--team fix auth.py", "true", "fix auth.py", id="bool-present"
            ),
            pytest.param(
                ["--bool", "team", "S", "false"], "S", "fix auth.py", "false", "fix auth.py", id="bool-absent"
            ),
            pytest.param(
                ["--neg-bool", "no-challenge", "CHALLENGE", "true"],
                "CHALLENGE",
                "--no-challenge fix auth.py",
                "false",
                "fix auth.py",
                id="neg-bool-present",
            ),
            pytest.param(
                ["--neg-bool", "no-challenge", "CHALLENGE", "true"],
                "CHALLENGE",
                "fix auth.py",
                "true",
                "fix auth.py",
                id="neg-bool-absent",
            ),
            pytest.param(
                ["--bool", "team", "S", "false"],
                "S",
                "--teamx fix auth.py",
                "false",
                "--teamx fix auth.py",
                id="bool-near-miss-leading",
            ),
            pytest.param(
                ["--bool", "team", "S", "false"],
                "S",
                "fix --teamx auth.py",
                "false",
                "fix --teamx auth.py",
                id="bool-near-miss-inline",
            ),
        ],
    )
    def test_bool_flag_extraction(
        self, spec_tokens: list[str], var: str, arguments: str, expected_value: str, expected_clean: str
    ):
        """A boolean flag flips its default only when present as a full token, and is stripped from the clean args.

        --team present gives true and --no-challenge present gives false; an absent flag keeps its default. A substring
        prefix such as --teamx is not the flag: the default holds and the token stays in the clean args.
        """
        specs = parse_specs(spec_tokens)
        vals, clean = extract_flags(arguments, specs)
        assert vals[var] == expected_value
        assert clean == expected_clean


# ---------------------------------------------------------------------------
# extract_flags — codemap
# ---------------------------------------------------------------------------


class TestCodemapFlag:
    """Codemap paired-flag extraction with double-condition guard."""

    @pytest.mark.parametrize(
        ("arguments", "expected_value"),
        [
            pytest.param("fix auth.py", "auto", id="neither-flag-auto"),
            pytest.param("--codemap fix auth.py", "strict", id="codemap-only-strict"),
            pytest.param("--no-codemap fix auth.py", "off", id="no-codemap-off"),
            pytest.param("--codemap --no-codemap fix auth.py", "off", id="both-no-codemap-wins"),
        ],
    )
    def test_codemap_flag_resolution(self, arguments: str, expected_value: str):
        """The --codemap / --no-codemap pair resolves to one mode and both flags are stripped from the clean args.

        Neither flag keeps the auto default, --codemap alone is strict, --no-codemap is off, and with both together
        --no-codemap wins.
        """
        specs = parse_specs(["--codemap", "CODEMAP_RAW", "auto"])
        vals, clean = extract_flags(arguments, specs)
        assert vals["CODEMAP_RAW"] == expected_value
        assert clean == "fix auth.py"


# ---------------------------------------------------------------------------
# extract_flags — int / str
# ---------------------------------------------------------------------------


class TestValueFlags:
    """Integer and string flag extraction."""

    def test_int_non_integer_exits(self):
        """Non-integer value for --int flag exits with code 2."""
        specs = parse_specs(["--int", "max-depth", "MAX_DEPTH", "3"])
        with pytest.raises(SystemExit) as exc:
            extract_flags("--max-depth notanumber", specs)
        assert exc.value.code == 2

    @pytest.mark.parametrize(
        ("arguments", "expected_value", "expected_clean"),
        [
            pytest.param(
                "fix --max-depths 5 auth.py", "3", "fix --max-depths 5 auth.py", id="fix---max-depths-5-auth.py"
            ),
            pytest.param("fix --max-depth 5 auth.py", "5", "fix auth.py", id="fix---max-depth-5-auth.py"),
            pytest.param("fix --max-depth=7 auth.py", "7", "fix auth.py", id="fix---max-depth-7-auth.py"),
            pytest.param("--max-depth 5 fix auth.py", "5", "fix auth.py", id="leading-space-form"),
            pytest.param("--max-depth=5 fix auth.py", "5", "fix auth.py", id="leading-eq-form"),
            pytest.param("fix auth.py", "3", "fix auth.py", id="absent-default"),
        ],
    )
    def test_int_token_boundaries(self, arguments: str, expected_value: str, expected_clean: str):
        """Value flags require exact flag names and preserve near-miss flags.

        An integer flag takes its value in both the space and equals forms, keeps its default when absent, and leaves a
        near-miss such as --max-depths in the clean args.
        """
        specs = parse_specs(["--int", "max-depth", "MAX_DEPTH", "3"])
        vals, clean = extract_flags(arguments, specs)
        assert vals["MAX_DEPTH"] == expected_value
        assert clean == expected_clean

    @pytest.mark.parametrize(
        ("flag", "var", "arguments", "expected_value", "expected_clean"),
        [
            pytest.param(
                "plan",
                "PLAN_FILE",
                "--plan .plans/active/plan.md fix auth.py",
                ".plans/active/plan.md",
                "fix auth.py",
                id="space-form",
            ),
            pytest.param(
                "plan",
                "PLAN_FILE",
                "--plan=.plans/active/plan.md fix auth.py",
                ".plans/active/plan.md",
                "fix auth.py",
                id="eq-form",
            ),
            pytest.param("plan", "PLAN_FILE", "fix auth.py", "", "fix auth.py", id="absent-empty-default"),
            pytest.param(
                "ci-run", "CI_RUN_ID", "--ci-run 12345678 fix auth.py", "12345678", "fix auth.py", id="ci-run-value"
            ),
            pytest.param(
                "plan",
                "PLAN_FILE",
                "--plan --team fix auth.py",
                "",
                "--plan --team fix auth.py",
                id="followed-by-flag-uses-default",
            ),
        ],
    )
    def test_str_flag_extraction(self, flag: str, var: str, arguments: str, expected_value: str, expected_clean: str):
        """A string flag takes its value in the space and equals forms, and falls back to its empty default.

        --plan and --ci-run extract their value and are stripped from the clean args; when absent the empty default
        holds, and a following flag token is never consumed as the value.
        """
        specs = parse_specs(["--str", flag, var, ""])
        vals, clean = extract_flags(arguments, specs)
        assert vals[var] == expected_value
        assert clean == expected_clean


# ---------------------------------------------------------------------------
# run — output format
# ---------------------------------------------------------------------------


class TestRunOutput:
    """Emit shell-eval-safe KEY=VALUE lines."""

    def test_single_quote_wrapping(self):
        """All values wrapped in single quotes."""
        out = run("fix auth.py", ["--bool", "team", "TEAM_MODE", "false"])
        assert "TEAM_MODE='false'" in out
        assert "CLEAN_ARGS='fix auth.py'" in out

    def test_clean_args_last_line(self):
        """CLEAN_ARGS is always the last emitted line."""
        out = run("fix auth.py", ["--bool", "team", "TEAM_MODE", "false"])
        last = out.strip().splitlines()[-1]
        assert last.startswith("CLEAN_ARGS=")

    def test_whitespace_normalised_in_clean_args(self):
        """Multiple spaces in stripped args collapsed to single space."""
        out = run("  --team   fix   auth.py  ", ["--bool", "team", "S", "false"])
        assert "CLEAN_ARGS='fix auth.py'" in out

    @pytest.mark.parametrize(
        ("arguments", "expected"),
        [
            pytest.param("it's a test", "CLEAN_ARGS='it'\\''s a test'", id="it-s-a-test"),
            pytest.param('say "hello"', "CLEAN_ARGS='say \"hello\"'", id="say-hello"),
            pytest.param("semi; colon", "CLEAN_ARGS='semi; colon'", id="semi-colon"),
            pytest.param("", "CLEAN_ARGS=''", id="empty"),
        ],
    )
    def test_shell_quoting_exact(self, arguments: str, expected: str):
        """Shell-sensitive values are emitted with exact single-quote escaping."""
        assert run(arguments, []).strip() == expected

    def test_combined_flags(self):
        """Multiple flags parsed together; clean args contains remainder."""
        out = run(
            "--no-challenge --codemap --team fix auth.py",
            [
                "--neg-bool",
                "no-challenge",
                "CHALLENGE_ENABLED",
                "true",
                "--bool",
                "team",
                "TEAM_MODE",
                "false",
                "--codemap",
                "CODEMAP_RAW",
                "auto",
            ],
        )
        assert "CHALLENGE_ENABLED='false'" in out
        assert "TEAM_MODE='true'" in out
        assert "CODEMAP_RAW='strict'" in out
        assert "CLEAN_ARGS='fix auth.py'" in out

    def test_no_specs_passthrough(self):
        """No specs → only CLEAN_ARGS emitted with original args."""
        out = run("fix auth.py", [])
        assert out.strip() == "CLEAN_ARGS='fix auth.py'"


# ---------------------------------------------------------------------------
# write_skill_files — skill registry and per-flag temp file writes
# ---------------------------------------------------------------------------


class TestWriteSkillFiles:
    """write_skill_files persists per-skill and legacy temp files, suffixed with the session scope."""

    @pytest.fixture(autouse=True)
    def _force_shared_csid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Remove session-id env vars so ``_csid()`` degrades deterministically to ``"shared"``."""
        monkeypatch.delenv("CSID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    def test_unknown_skill_exits(self, tmp_path: Path):
        """Unknown skill name calls sys.exit(1)."""
        with pytest.raises(SystemExit) as exc:
            write_skill_files("nonexistent-skill", "fix auth.py", tmp_dir=tmp_path)
        assert exc.value.code == 1

    @pytest.mark.parametrize("skill", sorted(SKILL_SPECS))
    def test_registered_skills_emit_per_flag_files(self, skill: str, tmp_path: Path):
        """Each registered skill writes one per-skill file per declared flag."""
        write_skill_files(skill, "", tmp_dir=tmp_path)
        for spec, _legacy in SKILL_SPECS[skill]:
            key = spec.flag or "codemap"
            assert (tmp_path / f"dev-{skill}-{key}-shared").exists(), f"missing per-skill file for {skill}/{key}"

    @pytest.mark.parametrize("skill", sorted(SKILL_SPECS))
    def test_registered_skills_emit_legacy_files(self, skill: str, tmp_path: Path):
        """Legacy filenames are written so downstream blocks reading shared paths still work."""
        write_skill_files(skill, "", tmp_dir=tmp_path)
        for _spec, legacy in SKILL_SPECS[skill]:
            if legacy is None:
                continue
            assert (tmp_path / f"{legacy}-shared").exists(), f"missing legacy file {legacy}-shared for skill {skill}"

    def test_feature_flag_values_persisted(self, tmp_path: Path):
        """Feature skill: representative flags persist their parsed values."""
        write_skill_files("feature", "--team --no-challenge --codemap fix auth.py", tmp_dir=tmp_path)
        assert (tmp_path / "dev-feature-team-shared").read_text() == "true\n"
        assert (tmp_path / "dev-feature-no-challenge-shared").read_text() == "false\n"
        assert (tmp_path / "dev-feature-codemap-shared").read_text() == "strict\n"
        # Legacy paths mirror the same values
        assert (tmp_path / "dev-team-mode-shared").read_text() == "true\n"
        assert (tmp_path / "dev-challenge-enabled-shared").read_text() == "false\n"
        assert (tmp_path / "dev-codemap-raw-shared").read_text() == "strict\n"

    def test_debug_codemap_raw_persisted(self, tmp_path: Path):
        """Debug skill: ``--no-codemap`` writes 'off' to both per-skill and legacy CODEMAP_RAW files."""
        write_skill_files("debug", "--no-codemap symptom", tmp_dir=tmp_path)
        assert (tmp_path / "dev-debug-codemap-shared").read_text() == "off\n"
        assert (tmp_path / "dev-codemap-raw-shared").read_text() == "off\n"

    def test_defaults_applied_for_absent_flags(self, tmp_path: Path):
        """Absent flags fall back to declared defaults in both file flavours."""
        write_skill_files("refactor", "tidy module", tmp_dir=tmp_path)
        assert (tmp_path / "dev-refactor-team-shared").read_text() == "false\n"
        assert (tmp_path / "dev-team-mode-shared").read_text() == "false\n"
        assert (tmp_path / "dev-refactor-repo-shared").read_text() == "\n"
        assert (tmp_path / "dev-upstream-shared").read_text() == "\n"

    @pytest.mark.parametrize("skill", sorted(SKILL_SPECS))
    def test_every_written_file_is_newline_terminated(self, skill: str, tmp_path: Path):
        """Every persisted value ends with a newline.

        Consumers read these back with ``IFS= read -r VAR < file || VAR=<default>``. ``read`` exits non-zero on a final
        line with no terminator, so the ``||`` fallback fires and overwrites the value that was just read — a value
        without the trailing newline is silently replaced by the default in every downstream Bash() block.
        """
        write_skill_files(skill, "--codemap do the thing", tmp_dir=tmp_path)
        written = sorted(p for p in tmp_path.iterdir() if p.is_file())
        assert written, f"no files written for skill {skill}"
        for path in written:
            assert path.read_text().endswith("\n"), f"{path.name} is not newline-terminated"

    @pytest.mark.parametrize("skill", ["feature", "fix", "refactor", "debug", "review"])
    @pytest.mark.parametrize(
        ("arguments", "expected"),
        [
            pytest.param("--worktree do the thing", "true\n", id="flag-enabled"),
            pytest.param("do the thing", "false\n", id="flag-absent-defaults-false"),
        ],
    )
    def test_worktree_flag_persisted(self, skill: str, arguments: str, expected: str, tmp_path: Path):
        """Worktree-capable skills: ``--worktree`` persists 'true' to its per-skill sentinel, absent it persists
        'false'.

        The flag has no legacy file (legacy=None), so only the per-skill sentinel is checked.
        """
        write_skill_files(skill, arguments, tmp_dir=tmp_path)
        assert (tmp_path / f"dev-{skill}-worktree-shared").read_text() == expected

    def test_worktree_not_registered_for_plan(self):
        """Plan is analysis-only (never edits) — it must not register ``--worktree``."""
        flags = {spec.flag for spec, _legacy in SKILL_SPECS["plan"]}
        assert "worktree" not in flags

    @pytest.mark.parametrize("skill", ["feature", "refactor"])
    @pytest.mark.parametrize(
        ("arguments", "expected"),
        [
            pytest.param("do the thing", "true\n", id="flag-absent-defaults-true"),
            pytest.param("--no-batch do the thing", "false\n", id="flag-disables"),
        ],
    )
    def test_no_batch_flag_persisted(self, skill: str, arguments: str, expected: str, tmp_path: Path):
        """Batch-capable skills: absent ``--no-batch`` persists 'true' (batch mode ships default-on), ``--no-batch``
        'false'.

        The flag has no legacy file (legacy=None), so only the per-skill sentinel is checked.
        """
        write_skill_files(skill, arguments, tmp_dir=tmp_path)
        assert (tmp_path / f"dev-{skill}-no-batch-shared").read_text() == expected

    @pytest.mark.parametrize("skill", ["fix", "debug", "review", "plan"])
    def test_no_batch_not_registered_outside_feature_and_refactor(self, skill: str):
        """Batch mode is scoped to feature Step 3 / refactor Step 4 only — no other skill registers it."""
        flags = {spec.flag for spec, _legacy in SKILL_SPECS[skill]}
        assert "no-batch" not in flags


# ---------------------------------------------------------------------------
# main() — argparse gate + both call shapes preserved
# ---------------------------------------------------------------------------


class TestMainEntryPoint:
    """Supply -h/--help without letting argparse touch the blob or spec tokens."""

    def test_help_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Print usage to stdout and exit 0 (argparse default)."""
        with pytest.raises(SystemExit) as exc:
            main(["--help"])
        assert exc.value.code == 0
        assert "usage" in capsys.readouterr().out.lower()

    def test_empty_argv_exits_1(self, capsys: pytest.CaptureFixture[str]) -> None:
        """No argv → usage on stderr, exit 1 (legacy contract preserved)."""
        assert main([]) == 1
        assert "usage" in capsys.readouterr().err.lower()

    def test_legacy_blob_with_dash_tokens_reaches_spec_loop(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Legacy form: blob carrying ``--``-shaped tokens is parsed by the spec loop, not argparse.

        argparse would reject the spec tokens (``--bool`` etc.) as unknown options. The script must instead treat
        argv[0] as the blob and argv[1:] as spec tokens — proving the blob and specs bypassed argparse entirely.
        """
        rc = main(["--team --no-challenge fix auth.py", "--bool", "team", "TEAM_MODE", "false"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "TEAM_MODE='true'" in out
        assert "CLEAN_ARGS='--no-challenge fix auth.py'" in out

    def test_skill_mode_blob_dash_tokens_written_to_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Skill form: the ``--write-files`` blob with ``--flag`` tokens is consumed internally.

        ``--skill``/``--write-files`` are the only genuine outer flags; the trailing blob (which contains
        ``--team``/``--no-challenge``) must reach write_skill_files unmangled, not be interpreted as argparse options.
        """
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.delenv("CSID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        rc = main(["--skill", "feature", "--write-files", "--team --no-challenge fix auth.py"])
        assert rc == 0
        assert (tmp_path / "dev-feature-team-shared").read_text() == "true\n"
        assert (tmp_path / "dev-feature-no-challenge-shared").read_text() == "false\n"

    def test_skill_mode_missing_write_files_exits_1(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Reject skill mode when file output is not requested."""
        rc = main(["--skill", "feature", "some args"])
        assert rc == 1
        assert "--write-files" in capsys.readouterr().err

    def test_module_exposes_main(self) -> None:
        """Expose the command entry point from the module namespace."""
        assert callable(dev_parse_args.main)

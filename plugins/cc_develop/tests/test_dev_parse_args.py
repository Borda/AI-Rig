"""Tests for dev_parse_args.py — develop-skill flag parser."""

from __future__ import annotations

from pathlib import Path

import pytest

import dev_parse_args
from dev_parse_args import (
    SKILL_SPECS,
    FlagSpec,
    SpecType,
    extract_flags,
    main,
    parse_specs,
    run,
    write_skill_files,
)


# ---------------------------------------------------------------------------
# parse_specs
# ---------------------------------------------------------------------------


class TestParseSpecs:
    """parse_specs converts token list into FlagSpec objects."""

    def test_bool_spec(self):
        """Verify command-line option behavior.

        --bool produces FlagSpec with correct fields.
        """
        specs = parse_specs(["--bool", "semble", "SEMBLE_ENABLED", "false"])
        assert specs == [FlagSpec(kind=SpecType.BOOL, flag="semble", var="SEMBLE_ENABLED", default="false")]

    def test_neg_bool_spec(self):
        """Verify command-line option behavior.

        --neg-bool produces FlagSpec with neg-bool kind.
        """
        specs = parse_specs(["--neg-bool", "no-challenge", "CHALLENGE_ENABLED", "true"])
        assert specs == [FlagSpec(kind=SpecType.NEG_BOOL, flag="no-challenge", var="CHALLENGE_ENABLED", default="true")]

    def test_codemap_spec(self):
        """Verify command-line option behavior.

        --codemap takes only VAR + DEFAULT (no FLAG token).
        """
        specs = parse_specs(["--codemap", "CODEMAP_RAW", "auto"])
        assert specs == [FlagSpec(kind=SpecType.CODEMAP, flag="", var="CODEMAP_RAW", default="auto")]

    def test_int_spec(self):
        """Verify command-line option behavior.

        --int spec stored with default as string.
        """
        specs = parse_specs(["--int", "max-depth", "MAX_DEPTH", "3"])
        assert specs == [FlagSpec(kind=SpecType.INT, flag="max-depth", var="MAX_DEPTH", default="3")]

    def test_str_spec(self):
        """Verify command-line option behavior.

        --str spec with empty default.
        """
        specs = parse_specs(["--str", "plan", "PLAN_FILE", ""])
        assert specs == [FlagSpec(kind=SpecType.STR, flag="plan", var="PLAN_FILE", default="")]

    def test_multiple_specs(self):
        """Multiple specs parsed in order."""
        specs = parse_specs(["--bool", "team", "TEAM_MODE", "false", "--codemap", "CODEMAP_RAW", "auto"])
        assert len(specs) == 2
        assert specs[0].kind == SpecType.BOOL
        assert specs[1].kind == SpecType.CODEMAP

    def test_unknown_keyword_exits(self):
        """Unknown spec keyword calls sys.exit(1)."""
        with pytest.raises(SystemExit) as exc:
            parse_specs(["--unknown", "foo", "BAR", "baz"])
        assert exc.value.code == 1

    def test_insufficient_tokens_exits(self):
        """Too few tokens after keyword calls sys.exit(1)."""
        with pytest.raises(SystemExit) as exc:
            parse_specs(["--bool", "semble"])
        assert exc.value.code == 1


# ---------------------------------------------------------------------------
# extract_flags — bool / neg-bool
# ---------------------------------------------------------------------------


class TestBoolFlags:
    """Boolean and negated-boolean flag extraction."""

    def test_bool_present(self):
        """Verify command-line option behavior.

        --semble present → true.
        """
        specs = parse_specs(["--bool", "semble", "S", "false"])
        vals, clean = extract_flags("--semble fix auth.py", specs)
        assert vals["S"] == "true"
        assert clean == "fix auth.py"

    def test_bool_absent(self):
        """Verify command-line option behavior.

        --semble absent → default.
        """
        specs = parse_specs(["--bool", "semble", "S", "false"])
        vals, clean = extract_flags("fix auth.py", specs)
        assert vals["S"] == "false"
        assert clean == "fix auth.py"

    def test_neg_bool_present(self):
        """Verify command-line option behavior.

        --no-challenge present → false.
        """
        specs = parse_specs(["--neg-bool", "no-challenge", "CHALLENGE", "true"])
        vals, clean = extract_flags("--no-challenge fix auth.py", specs)
        assert vals["CHALLENGE"] == "false"
        assert clean == "fix auth.py"

    def test_neg_bool_absent(self):
        """Verify command-line option behavior.

        --no-challenge absent → default.
        """
        specs = parse_specs(["--neg-bool", "no-challenge", "CHALLENGE", "true"])
        vals, clean = extract_flags("fix auth.py", specs)
        assert vals["CHALLENGE"] == "true"
        assert clean == "fix auth.py"

    @pytest.mark.parametrize("arguments", ["--sembleton fix auth.py", "fix --sembleton auth.py"])
    def test_bool_near_miss_not_consumed(self, arguments: str):
        """Flag extraction requires a full token, not a substring prefix."""
        specs = parse_specs(["--bool", "semble", "S", "false"])
        vals, clean = extract_flags(arguments, specs)
        assert vals["S"] == "false"
        assert clean == arguments


# ---------------------------------------------------------------------------
# extract_flags — codemap
# ---------------------------------------------------------------------------


class TestCodemapFlag:
    """Codemap paired-flag extraction with double-condition guard."""

    def test_codemap_absent(self):
        """Neither flag → auto."""
        specs = parse_specs(["--codemap", "CODEMAP_RAW", "auto"])
        vals, _ = extract_flags("fix auth.py", specs)
        assert vals["CODEMAP_RAW"] == "auto"

    def test_codemap_strict(self):
        """Verify command-line option behavior.

        --codemap only → strict.
        """
        specs = parse_specs(["--codemap", "CODEMAP_RAW", "auto"])
        vals, clean = extract_flags("--codemap fix auth.py", specs)
        assert vals["CODEMAP_RAW"] == "strict"
        assert clean == "fix auth.py"

    def test_no_codemap_off(self):
        """Verify command-line option behavior.

        --no-codemap → off.
        """
        specs = parse_specs(["--codemap", "CODEMAP_RAW", "auto"])
        vals, clean = extract_flags("--no-codemap fix auth.py", specs)
        assert vals["CODEMAP_RAW"] == "off"
        assert clean == "fix auth.py"

    def test_both_no_codemap_wins(self):
        """Verify command-line option behavior.

        --codemap + --no-codemap together → off (--no-codemap wins).
        """
        specs = parse_specs(["--codemap", "CODEMAP_RAW", "auto"])
        vals, clean = extract_flags("--codemap --no-codemap fix auth.py", specs)
        assert vals["CODEMAP_RAW"] == "off"
        assert clean == "fix auth.py"


# ---------------------------------------------------------------------------
# extract_flags — int / str
# ---------------------------------------------------------------------------


class TestValueFlags:
    """Integer and string flag extraction."""

    def test_int_space_form(self):
        """Verify command-line option behavior.

        --max-depth 5 → 5.
        """
        specs = parse_specs(["--int", "max-depth", "MAX_DEPTH", "3"])
        vals, clean = extract_flags("--max-depth 5 fix auth.py", specs)
        assert vals["MAX_DEPTH"] == "5"
        assert clean == "fix auth.py"

    def test_int_eq_form(self):
        """Verify command-line option behavior.

        --max-depth=5 → 5.
        """
        specs = parse_specs(["--int", "max-depth", "MAX_DEPTH", "3"])
        vals, clean = extract_flags("--max-depth=5 fix auth.py", specs)
        assert vals["MAX_DEPTH"] == "5"
        assert clean == "fix auth.py"

    def test_int_absent_default(self):
        """Verify command-line option behavior.

        --max-depth absent → default.
        """
        specs = parse_specs(["--int", "max-depth", "MAX_DEPTH", "3"])
        vals, _ = extract_flags("fix auth.py", specs)
        assert vals["MAX_DEPTH"] == "3"

    def test_int_non_integer_exits(self):
        """Non-integer value for --int flag exits with code 2."""
        specs = parse_specs(["--int", "max-depth", "MAX_DEPTH", "3"])
        with pytest.raises(SystemExit) as exc:
            extract_flags("--max-depth notanumber", specs)
        assert exc.value.code == 2

    def test_str_space_form(self):
        """Verify command-line option behavior.

        --plan path/to/file.md → value extracted.
        """
        specs = parse_specs(["--str", "plan", "PLAN_FILE", ""])
        vals, clean = extract_flags("--plan .plans/active/plan.md fix auth.py", specs)
        assert vals["PLAN_FILE"] == ".plans/active/plan.md"
        assert clean == "fix auth.py"

    def test_str_eq_form(self):
        """Verify command-line option behavior.

        --plan=path/to/file.md → value extracted.
        """
        specs = parse_specs(["--str", "plan", "PLAN_FILE", ""])
        vals, clean = extract_flags("--plan=.plans/active/plan.md fix auth.py", specs)
        assert vals["PLAN_FILE"] == ".plans/active/plan.md"
        assert clean == "fix auth.py"

    def test_str_absent_empty_default(self):
        """Verify command-line option behavior.

        --str absent → empty string default.
        """
        specs = parse_specs(["--str", "plan", "PLAN_FILE", ""])
        vals, _ = extract_flags("fix auth.py", specs)
        assert vals["PLAN_FILE"] == ""

    def test_str_ci_run(self):
        """Verify command-line option behavior.

        --ci-run value extracted correctly.
        """
        specs = parse_specs(["--str", "ci-run", "CI_RUN_ID", ""])
        vals, clean = extract_flags("--ci-run 12345678 fix auth.py", specs)
        assert vals["CI_RUN_ID"] == "12345678"
        assert clean == "fix auth.py"

    @pytest.mark.parametrize(
        "arguments,expected_value,expected_clean",
        [
            pytest.param(
                "fix --max-depths 5 auth.py", "3", "fix --max-depths 5 auth.py", id="fix---max-depths-5-auth.py"
            ),
            pytest.param("fix --max-depth 5 auth.py", "5", "fix auth.py", id="fix---max-depth-5-auth.py"),
            pytest.param("fix --max-depth=7 auth.py", "7", "fix auth.py", id="fix---max-depth-7-auth.py"),
        ],
    )
    def test_int_token_boundaries(self, arguments: str, expected_value: str, expected_clean: str):
        """Value flags require exact flag names and preserve near-miss flags."""
        specs = parse_specs(["--int", "max-depth", "MAX_DEPTH", "3"])
        vals, clean = extract_flags(arguments, specs)
        assert vals["MAX_DEPTH"] == expected_value
        assert clean == expected_clean

    def test_value_flag_followed_by_another_flag_uses_default(self):
        """A following flag token is not consumed as the value."""
        specs = parse_specs(["--str", "plan", "PLAN_FILE", ""])
        vals, clean = extract_flags("--plan --team fix auth.py", specs)
        assert vals["PLAN_FILE"] == ""
        assert clean == "--plan --team fix auth.py"


# ---------------------------------------------------------------------------
# run — output format
# ---------------------------------------------------------------------------


class TestRunOutput:
    """Emit shell-eval-safe KEY=VALUE lines."""

    def test_single_quote_wrapping(self):
        """All values wrapped in single quotes."""
        out = run("fix auth.py", ["--bool", "semble", "SEMBLE_ENABLED", "false"])
        assert "SEMBLE_ENABLED='false'" in out
        assert "CLEAN_ARGS='fix auth.py'" in out

    def test_clean_args_last_line(self):
        """CLEAN_ARGS is always the last emitted line."""
        out = run("fix auth.py", ["--bool", "semble", "SEMBLE_ENABLED", "false"])
        last = out.strip().splitlines()[-1]
        assert last.startswith("CLEAN_ARGS=")

    def test_whitespace_normalised_in_clean_args(self):
        """Multiple spaces in stripped args collapsed to single space."""
        out = run("  --semble   fix   auth.py  ", ["--bool", "semble", "S", "false"])
        assert "CLEAN_ARGS='fix auth.py'" in out

    @pytest.mark.parametrize(
        "arguments,expected",
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
            "--no-challenge --codemap --semble fix auth.py",
            [
                "--neg-bool",
                "no-challenge",
                "CHALLENGE_ENABLED",
                "true",
                "--bool",
                "semble",
                "SEMBLE_ENABLED",
                "false",
                "--codemap",
                "CODEMAP_RAW",
                "auto",
            ],
        )
        assert "CHALLENGE_ENABLED='false'" in out
        assert "SEMBLE_ENABLED='true'" in out
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
        write_skill_files("feature", "--semble --no-challenge --codemap fix auth.py", tmp_dir=tmp_path)
        assert (tmp_path / "dev-feature-semble-shared").read_text() == "true\n"
        assert (tmp_path / "dev-feature-no-challenge-shared").read_text() == "false\n"
        assert (tmp_path / "dev-feature-codemap-shared").read_text() == "strict\n"
        # Legacy paths mirror the same values
        assert (tmp_path / "dev-semble-enabled-shared").read_text() == "true\n"
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
    def test_worktree_flag_enabled_persisted(self, skill: str, tmp_path: Path):
        """Worktree-capable skills: ``--worktree`` persists 'true' to its per-skill sentinel (legacy=None → no legacy
        file)."""
        write_skill_files(skill, "--worktree do the thing", tmp_dir=tmp_path)
        assert (tmp_path / f"dev-{skill}-worktree-shared").read_text() == "true\n"

    @pytest.mark.parametrize("skill", ["feature", "fix", "refactor", "debug", "review"])
    def test_worktree_flag_absent_defaults_false(self, skill: str, tmp_path: Path):
        """Worktree-capable skills: absent ``--worktree`` defaults the sentinel to 'false'."""
        write_skill_files(skill, "do the thing", tmp_dir=tmp_path)
        assert (tmp_path / f"dev-{skill}-worktree-shared").read_text() == "false\n"

    def test_worktree_not_registered_for_plan(self):
        """Plan is analysis-only (never edits) — it must not register ``--worktree``."""
        flags = {spec.flag for spec, _legacy in SKILL_SPECS["plan"]}
        assert "worktree" not in flags


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
        rc = main(["--semble --no-challenge fix auth.py", "--bool", "semble", "SEMBLE_ENABLED", "false"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "SEMBLE_ENABLED='true'" in out
        assert "CLEAN_ARGS='--no-challenge fix auth.py'" in out

    def test_skill_mode_blob_dash_tokens_written_to_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Skill form: the ``--write-files`` blob with ``--flag`` tokens is consumed internally.

        ``--skill``/``--write-files`` are the only genuine outer flags; the trailing blob (which contains
        ``--semble``/``--no-challenge``) must reach write_skill_files unmangled, not be interpreted as argparse options.
        """
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.delenv("CSID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        rc = main(["--skill", "feature", "--write-files", "--semble --no-challenge fix auth.py"])
        assert rc == 0
        assert (tmp_path / "dev-feature-semble-shared").read_text() == "true\n"
        assert (tmp_path / "dev-feature-no-challenge-shared").read_text() == "false\n"

    def test_skill_mode_missing_write_files_exits_1(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Reject skill mode when file output is not requested."""
        rc = main(["--skill", "feature", "some args"])
        assert rc == 1
        assert "--write-files" in capsys.readouterr().err

    def test_module_exposes_main(self) -> None:
        """Expose the command entry point from the module namespace."""
        assert callable(dev_parse_args.main)

"""Tests for dev_parse_args.py — develop-skill flag parser."""

from __future__ import annotations

from pathlib import Path

import dev_parse_args
import pytest
from dev_parse_args import SKILL_SPECS, FlagSpec, SpecType, extract_flags, main, parse_specs, run, write_skill_files


@pytest.fixture
def shared_csid(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove session-id env vars so ``_csid()`` degrades deterministically to ``"shared"``."""
    monkeypatch.delenv("CSID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


def _read_files(directory: Path, names: list[str]) -> dict[str, str]:
    """Return the text of each named file under ``directory``, keyed by name."""
    return {name: (directory / name).read_text() for name in names}


def _missing_files(directory: Path, names: list[str]) -> list[str]:
    """Return the names from ``names`` that do not exist under ``directory``."""
    return [name for name in names if not (directory / name).exists()]


def _unterminated_files(directory: Path) -> dict[str, bool]:
    """Map every file under ``directory`` to whether its text ends with a newline."""
    return {p.name: p.read_text().endswith("\n") for p in sorted(directory.iterdir()) if p.is_file()}


class TestParseSpecs:
    """parse_specs converts a token list into FlagSpec objects."""

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
    def test_builds_flag_spec(self, tokens: list[str], expected: FlagSpec):
        """Each spec keyword produces one FlagSpec with the matching kind, flag, variable and default.

        --codemap takes only VAR + DEFAULT (no FLAG token) and --int keeps its default as a string; every other keyword
        takes KIND FLAG VAR DEFAULT.
        """
        assert parse_specs(tokens) == [expected]

    def test_keeps_specs_in_order(self):
        """Several specs in one token list come back as separate FlagSpecs in the order given.

        Skills declare all their flags in a single call; a reordered or merged result would shift which variable each
        flag value lands in.
        """
        specs = parse_specs(["--bool", "team", "TEAM_MODE", "false", "--codemap", "CODEMAP_RAW", "auto"])
        assert [spec.kind for spec in specs] == [SpecType.BOOL, SpecType.CODEMAP]

    @pytest.mark.parametrize(
        "tokens",
        [
            pytest.param(["--unknown", "foo", "BAR", "baz"], id="unknown-keyword"),
            pytest.param(["--bool", "team"], id="insufficient-tokens"),
            pytest.param(["--bool", "team", "X;touch pwned", "false"], id="var-command-separator"),
            pytest.param(["--str", "repo", "$(id)", ""], id="var-command-substitution"),
            pytest.param(["--int", "max-depth", "1DEPTH", "3"], id="var-leading-digit"),
            pytest.param(["--codemap", "CODEMAP RAW", "auto"], id="codemap-var-with-space"),
        ],
    )
    def test_exits_on_malformed_spec(self, tokens: list[str]):
        """An unknown spec keyword, too few tokens, or a VAR that is not a shell identifier calls sys.exit(1).

        Spec-driven output is ``VAR=value`` lines a caller feeds to ``eval``; the VAR name is emitted unquoted, so a
        name carrying shell syntax would execute instead of assigning.
        """
        with pytest.raises(SystemExit) as exc:
            parse_specs(tokens)
        assert exc.value.code == 1


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
    def test_extracts_full_token_only(
        self, spec_tokens: list[str], var: str, arguments: str, expected_value: str, expected_clean: str
    ):
        """A boolean flag flips its default only when present as a full token, and is stripped from the clean args.

        --team present gives true and --no-challenge present gives false; an absent flag keeps its default. A substring
        prefix such as --teamx is not the flag: the default holds and the token stays in the clean args.
        """
        vals, clean = extract_flags(arguments, parse_specs(spec_tokens))
        assert (vals[var], clean) == (expected_value, expected_clean)


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
    def test_resolves_flag_pair_to_one_mode(self, arguments: str, expected_value: str):
        """The --codemap / --no-codemap pair resolves to one mode and both flags are stripped from the clean args.

        Neither flag keeps the auto default, --codemap alone is strict, --no-codemap is off, and with both together
        --no-codemap wins.
        """
        vals, clean = extract_flags(arguments, parse_specs(["--codemap", "CODEMAP_RAW", "auto"]))
        assert (vals["CODEMAP_RAW"], clean) == (expected_value, "fix auth.py")


class TestValueFlags:
    """Integer and string flag extraction."""

    def test_int_flag_rejects_non_integer(self):
        """A non-integer value for an --int flag exits with code 2.

        The value feeds numeric limits downstream; passing ``notanumber`` through would fail far from the typo.
        """
        specs = parse_specs(["--int", "max-depth", "MAX_DEPTH", "3"])
        with pytest.raises(SystemExit) as exc:
            extract_flags("--max-depth notanumber", specs)
        assert exc.value.code == 2

    @pytest.mark.parametrize(
        ("arguments", "expected_value", "expected_clean"),
        [
            pytest.param("fix --max-depths 5 auth.py", "3", "fix --max-depths 5 auth.py", id="near-miss-flag-kept"),
            pytest.param("fix --max-depth 5 auth.py", "5", "fix auth.py", id="space-form"),
            pytest.param("fix --max-depth=7 auth.py", "7", "fix auth.py", id="eq-form"),
            pytest.param("--max-depth 5 fix auth.py", "5", "fix auth.py", id="leading-space-form"),
            pytest.param("--max-depth=5 fix auth.py", "5", "fix auth.py", id="leading-eq-form"),
            pytest.param("fix auth.py", "3", "fix auth.py", id="absent-default"),
        ],
    )
    def test_int_flag_token_boundaries(self, arguments: str, expected_value: str, expected_clean: str):
        """Value flags require exact flag names and preserve near-miss flags.

        An integer flag takes its value in both the space and equals forms, keeps its default when absent, and leaves a
        near-miss such as --max-depths in the clean args.
        """
        vals, clean = extract_flags(arguments, parse_specs(["--int", "max-depth", "MAX_DEPTH", "3"]))
        assert (vals["MAX_DEPTH"], clean) == (expected_value, expected_clean)

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
            pytest.param("keep", "KEEP_ITEMS", 'src/x.py --keep "a b"', "a b", "src/x.py", id="quoted-phrase"),
            pytest.param("keep", "KEEP_ITEMS", "src/x.py --keep='a b' go", "a b", "src/x.py go", id="eq-single-quoted"),
        ],
    )
    def test_str_flag_extraction(self, flag: str, var: str, arguments: str, expected_value: str, expected_clean: str):
        """A string flag takes its value in the space and equals forms, and falls back to its empty default.

        --plan and --ci-run extract their value and are stripped from the clean args; when absent the empty default
        holds, and a following flag token is never consumed as the value.
        """
        vals, clean = extract_flags(arguments, parse_specs(["--str", flag, var, ""]))
        assert (vals[var], clean) == (expected_value, expected_clean)


class TestRunOutput:
    """Emit shell-eval-safe KEY=VALUE lines."""

    def test_single_quotes_every_value_and_ends_with_clean_args(self):
        """Every value is wrapped in single quotes and CLEAN_ARGS is always the last emitted line.

        Callers ``eval`` this output; quoting keeps values inert and the fixed final line lets them locate the remainder
        of the arguments.
        """
        out = run("fix auth.py", ["--bool", "team", "TEAM_MODE", "false"])
        assert out.strip().splitlines() == ["TEAM_MODE='false'", "CLEAN_ARGS='fix auth.py'"]

    def test_collapses_whitespace_in_clean_args(self):
        """Multiple spaces left after stripping flags collapse to a single space in CLEAN_ARGS.

        Removing ``--team`` from the middle of the text would otherwise leave runs of spaces for later blocks.
        """
        out = run("  --team   fix   auth.py  ", ["--bool", "team", "S", "false"])
        assert "CLEAN_ARGS='fix auth.py'" in out

    @pytest.mark.parametrize(
        ("arguments", "expected"),
        [
            pytest.param("fix auth.py", "CLEAN_ARGS='fix auth.py'", id="plain-passthrough"),
            pytest.param("it's a test", "CLEAN_ARGS='it'\\''s a test'", id="single-quote"),
            pytest.param('say "hello"', "CLEAN_ARGS='say \"hello\"'", id="double-quotes"),
            pytest.param("semi; colon", "CLEAN_ARGS='semi; colon'", id="semicolon"),
            pytest.param("", "CLEAN_ARGS=''", id="empty"),
        ],
    )
    def test_shell_quoting_exact(self, arguments: str, expected: str):
        """With no specs only CLEAN_ARGS is emitted, with exact single-quote escaping of shell-sensitive text."""
        assert run(arguments, []).strip() == expected

    def test_combined_flags(self):
        """Several flags are parsed together and CLEAN_ARGS holds only the remainder.

        A real skill invocation mixes a negated bool, a codemap pair and a bool; each must resolve independently.
        """
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
        assert out.strip().splitlines() == [
            "CHALLENGE_ENABLED='false'",
            "TEAM_MODE='true'",
            "CODEMAP_RAW='strict'",
            "CLEAN_ARGS='fix auth.py'",
        ]


@pytest.mark.usefixtures("shared_csid")
class TestWriteSkillFiles:
    """write_skill_files persists per-skill and legacy temp files, suffixed with the session scope."""

    def test_rejects_unknown_skill(self, tmp_path: Path):
        """An unregistered skill name calls sys.exit(1) and writes nothing.

        A typo in a skill block would otherwise run with no flag sentinels and silently fall back to defaults.
        """
        with pytest.raises(SystemExit) as exc:
            write_skill_files("nonexistent-skill", "fix auth.py", tmp_dir=tmp_path)
        assert exc.value.code == 1

    @pytest.mark.parametrize("skill", sorted(SKILL_SPECS))
    def test_emits_per_flag_files(self, skill: str, tmp_path: Path):
        """Each registered skill writes one per-skill file per declared flag.

        Later skill blocks read these sentinels by name, so a declared flag with no file reads back as unset.
        """
        write_skill_files(skill, "", tmp_dir=tmp_path)
        names = [f"dev-{skill}-{spec.flag or 'codemap'}-shared" for spec, _legacy in SKILL_SPECS[skill]]
        assert _missing_files(tmp_path, names) == []

    @pytest.mark.parametrize("skill", sorted(SKILL_SPECS))
    def test_emits_legacy_files(self, skill: str, tmp_path: Path):
        """Legacy filenames are written so downstream blocks reading shared paths still work.

        Older blocks still read the un-prefixed sentinel names; a skill that stops writing them breaks those readers.
        """
        write_skill_files(skill, "", tmp_dir=tmp_path)
        names = [f"{legacy}-shared" for _spec, legacy in SKILL_SPECS[skill] if legacy is not None]
        assert _missing_files(tmp_path, names) == []

    def test_persists_feature_flag_values(self, tmp_path: Path):
        """Feature skill: representative flags persist their parsed values under per-skill and legacy names.

        One invocation sets a bool, a negated bool and the codemap pair; every sentinel must mirror the same value in
        both naming schemes.
        """
        write_skill_files("feature", "--team --no-challenge --codemap fix auth.py", tmp_dir=tmp_path)
        names = [
            "dev-feature-team-shared",
            "dev-feature-no-challenge-shared",
            "dev-feature-codemap-shared",
            "dev-team-mode-shared",
            "dev-challenge-enabled-shared",
            "dev-codemap-raw-shared",
        ]
        assert _read_files(tmp_path, names) == {
            "dev-feature-team-shared": "true\n",
            "dev-feature-no-challenge-shared": "false\n",
            "dev-feature-codemap-shared": "strict\n",
            "dev-team-mode-shared": "true\n",
            "dev-challenge-enabled-shared": "false\n",
            "dev-codemap-raw-shared": "strict\n",
        }

    def test_persists_debug_codemap_off(self, tmp_path: Path):
        """Debug skill: ``--no-codemap`` writes 'off' to both the per-skill and the legacy CODEMAP_RAW file."""
        write_skill_files("debug", "--no-codemap symptom", tmp_dir=tmp_path)
        names = ["dev-debug-codemap-shared", "dev-codemap-raw-shared"]
        assert _read_files(tmp_path, names) == {"dev-debug-codemap-shared": "off\n", "dev-codemap-raw-shared": "off\n"}

    def test_applies_defaults_for_absent_flags(self, tmp_path: Path):
        """Absent flags fall back to their declared defaults in both file flavours.

        A plain goal with no flags must still leave readable sentinels (``false`` for bools, empty for strings).
        """
        write_skill_files("refactor", "tidy module", tmp_dir=tmp_path)
        names = ["dev-refactor-team-shared", "dev-team-mode-shared", "dev-refactor-repo-shared", "dev-upstream-shared"]
        assert _read_files(tmp_path, names) == {
            "dev-refactor-team-shared": "false\n",
            "dev-team-mode-shared": "false\n",
            "dev-refactor-repo-shared": "\n",
            "dev-upstream-shared": "\n",
        }

    @pytest.mark.parametrize("skill", sorted(SKILL_SPECS))
    def test_terminates_every_file_with_newline(self, skill: str, tmp_path: Path):
        """Every persisted value ends with a newline.

        Consumers read these back with ``IFS= read -r VAR < file || VAR=<default>``. ``read`` exits non-zero on a final
        line with no terminator, so the ``||`` fallback fires and overwrites the value that was just read — a value
        without the trailing newline is silently replaced by the default in every downstream Bash() block.
        """
        write_skill_files(skill, "--codemap do the thing", tmp_dir=tmp_path)
        terminated = _unterminated_files(tmp_path)
        assert (bool(terminated), all(terminated.values())) == (True, True), terminated

    @pytest.mark.parametrize("skill", ["feature", "fix", "refactor", "debug", "review"])
    @pytest.mark.parametrize(
        ("arguments", "expected"),
        [
            pytest.param("--worktree do the thing", "true\n", id="flag-enabled"),
            pytest.param("do the thing", "false\n", id="flag-absent-defaults-false"),
        ],
    )
    def test_persists_worktree_flag(self, skill: str, arguments: str, expected: str, tmp_path: Path):
        """Worktree-capable skills: ``--worktree`` persists 'true', absent it persists 'false'.

        The flag has no legacy file (legacy=None), so only the per-skill sentinel is checked.
        """
        write_skill_files(skill, arguments, tmp_dir=tmp_path)
        assert (tmp_path / f"dev-{skill}-worktree-shared").read_text() == expected

    @pytest.mark.parametrize("skill", ["feature", "refactor"])
    @pytest.mark.parametrize(
        ("arguments", "expected"),
        [
            pytest.param("do the thing", "true\n", id="flag-absent-defaults-true"),
            pytest.param("--no-batch do the thing", "false\n", id="flag-disables"),
        ],
    )
    def test_persists_no_batch_flag(self, skill: str, arguments: str, expected: str, tmp_path: Path):
        """Batch-capable skills: absent ``--no-batch`` persists 'true' (default-on), ``--no-batch`` persists 'false'.

        The flag has no legacy file (legacy=None), so only the per-skill sentinel is checked.
        """
        write_skill_files(skill, arguments, tmp_dir=tmp_path)
        assert (tmp_path / f"dev-{skill}-no-batch-shared").read_text() == expected

    @pytest.mark.parametrize(
        ("skill", "flag"),
        [
            pytest.param("plan", "worktree", id="plan-no-worktree"),
            pytest.param("fix", "no-batch", id="fix-no-batch"),
            pytest.param("debug", "no-batch", id="debug-no-batch"),
            pytest.param("review", "no-batch", id="review-no-batch"),
            pytest.param("plan", "no-batch", id="plan-no-batch"),
        ],
    )
    def test_leaves_flag_unregistered(self, skill: str, flag: str):
        """A skill outside a flag's scope does not register it.

        Plan is analysis-only (never edits), so it must not register ``--worktree``; batch mode is scoped to feature
        Step 3 and refactor Step 4 only, so no other skill registers ``--no-batch``.
        """
        assert flag not in {spec.flag for spec, _legacy in SKILL_SPECS[skill]}

    @pytest.mark.parametrize("skill", ["feature", "fix", "debug", "refactor", "review"])
    def test_keep_text_never_reaches_clean_args_or_toggles_flags(self, skill: str, tmp_path: Path):
        """A quoted ``--keep`` phrase is cut out whole, so it neither leaks into the clean args nor flips a flag.

        ``review src/x.py --keep "a --team b"`` must leave only the target in the clean args; before the keep spec
        existed, the quoted text stayed in them and the target then failed as a non-Python path.
        """
        values = write_skill_files(skill, 'src/x.py --keep "use --team b"', tmp_dir=tmp_path)
        assert (tmp_path / f"dev-{skill}-clean-args-shared").read_text() == "src/x.py\n"
        assert values.get("TEAM_MODE", "false") == "false"

    def test_mirrors_negated_bool_under_its_variable_name(self, tmp_path: Path):
        """A ``--no-challenge`` file holds the enabled value, so it is also mirrored under ``challenge-enabled``."""
        write_skill_files("review", "--no-challenge src/x.py", tmp_dir=tmp_path)
        names = ["dev-review-no-challenge-shared", "dev-review-challenge-enabled-shared"]
        assert _read_files(tmp_path, names) == {
            "dev-review-no-challenge-shared": "false\n",
            "dev-review-challenge-enabled-shared": "false\n",
        }


@pytest.mark.usefixtures("shared_csid")
class TestReviewPrNumberSentinel:
    """``/develop:review`` reads its PR#/filename gate from a sentinel classified on the parsed arguments."""

    @pytest.mark.parametrize(
        ("arguments", "expected"),
        [
            pytest.param("42", "42\n", id="bare-number"),
            pytest.param("#42", "42\n", id="hash-prefix-dropped"),
            pytest.param("42 --full", "42\n", id="flag-after-number"),
            pytest.param("--no-challenge 42 --codemap", "42\n", id="flags-around-number"),
            pytest.param('42 --keep "a --team b"', "42\n", id="keep-phrase-stripped"),
            pytest.param("42.py", "\n", id="numeric-filename"),
            pytest.param("src/x.py", "\n", id="local-path"),
            pytest.param("42 src", "\n", id="number-plus-path"),
            pytest.param("", "\n", id="no-target-reviews-diff"),
        ],
    )
    def test_classifies_flag_stripped_target(
        self, arguments: str, expected: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Only a lone ``#``-optional digit run is a PR number; flags around it no longer hide it.

        The shell gate this replaces tested the raw argument text, so ``42 --full`` reviewed a nonexistent path and a
        quote in the text broke the block. Run in an empty directory so no file named like the target exists.
        """
        monkeypatch.chdir(tmp_path)
        write_skill_files("review", arguments, tmp_dir=tmp_path / "sentinels")
        assert (tmp_path / "sentinels" / "dev-review-pr-number-shared").read_text() == expected

    @pytest.mark.parametrize("name", ["42", "#42"])
    def test_existing_path_named_like_a_number_stays_local(
        self, name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A file or directory literally named ``42`` (or ``#42``) is reviewed as a path, never sent to oss:review.

        Mirrors the old ``[ ! -e "$TOKEN" ]`` half of the gate: the existence check runs on the exact typed token.
        """
        (tmp_path / name).mkdir()
        monkeypatch.chdir(tmp_path)
        write_skill_files("review", name, tmp_dir=tmp_path / "sentinels")
        assert (tmp_path / "sentinels" / "dev-review-pr-number-shared").read_text() == "\n"

    def test_empty_value_overwrites_a_stale_number(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """A local-target run clears the PR number an earlier run in the same session left behind.

        Without the overwrite, the gate would keep offering ``/oss:review`` for a path review.
        """
        monkeypatch.chdir(tmp_path)
        sentinels = tmp_path / "sentinels"
        write_skill_files("review", "42", tmp_dir=sentinels)
        write_skill_files("review", "src/x.py", tmp_dir=sentinels)
        assert (sentinels / "dev-review-pr-number-shared").read_text() == "\n"

    @pytest.mark.parametrize("skill", ["feature", "fix", "debug", "refactor", "plan"])
    def test_other_skills_write_no_pr_number(self, skill: str, tmp_path: Path):
        """Only review owns the PR#/filename gate; other skills treat a number as an issue or a goal."""
        write_skill_files(skill, "42", tmp_dir=tmp_path)
        assert not any("pr-number" in path.name for path in tmp_path.iterdir())


class TestMainEntryPoint:
    """Supply -h/--help without letting argparse touch the blob or spec tokens."""

    def test_help_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Print usage to stdout and exit 0 (argparse default)."""
        with pytest.raises(SystemExit) as exc:
            main(["--help"])
        assert (exc.value.code, "usage" in capsys.readouterr().out.lower()) == (0, True)

    def test_empty_argv_exits_1(self, capsys: pytest.CaptureFixture[str]) -> None:
        """No argv prints usage on stderr and returns 1 (legacy contract preserved)."""
        assert (main([]), "usage" in capsys.readouterr().err.lower()) == (1, True)

    def test_legacy_blob_with_dash_tokens_reaches_spec_loop(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Legacy form: blob carrying ``--``-shaped tokens is parsed by the spec loop, not argparse.

        argparse would reject the spec tokens (``--bool`` etc.) as unknown options. The script must instead treat
        argv[0] as the blob and argv[1:] as spec tokens — proving the blob and specs bypassed argparse entirely.
        """
        rc = main(["--team --no-challenge fix auth.py", "--bool", "team", "TEAM_MODE", "false"])
        assert (rc, capsys.readouterr().out.splitlines()) == (
            0,
            ["TEAM_MODE='true'", "CLEAN_ARGS='--no-challenge fix auth.py'"],
        )

    @pytest.mark.usefixtures("shared_csid")
    def test_skill_mode_blob_dash_tokens_written_to_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Skill form: the ``--write-files`` blob with ``--flag`` tokens is consumed internally.

        ``--skill``/``--write-files`` are the only genuine outer flags; the trailing blob (which contains
        ``--team``/``--no-challenge``) must reach write_skill_files unmangled, not be interpreted as argparse options.
        """
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        rc = main(["--skill", "feature", "--write-files", "--team --no-challenge fix auth.py"])
        names = ["dev-feature-team-shared", "dev-feature-no-challenge-shared"]
        assert (rc, _read_files(tmp_path, names)) == (
            0,
            {"dev-feature-team-shared": "true\n", "dev-feature-no-challenge-shared": "false\n"},
        )

    def test_skill_mode_missing_write_files_exits_1(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Skill mode without ``--write-files`` returns 1 and names the missing flag on stderr."""
        rc = main(["--skill", "feature", "some args"])
        assert (rc, "--write-files" in capsys.readouterr().err) == (1, True)

    def test_module_exposes_main(self) -> None:
        """Expose the command entry point from the module namespace."""
        assert callable(dev_parse_args.main)

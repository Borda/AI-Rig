"""Tests for check_bash_persistence bin script.

Covers block extraction, assignment/reference detection, cross-block violation detection, env-var filtering, and CLI
integration.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import check_bash_persistence as cbp


def _skill(tmp_path: Path, content: str) -> Path:
    """Write one isolated skill fixture and return its ``SKILL.md`` path.

    Examples:
        >>> from tempfile import TemporaryDirectory
        >>> with TemporaryDirectory() as directory:
        ...     _skill(Path(directory), "# Demo").read_text() == "# Demo"
        True
    """
    skill_dir = tmp_path / "myplugin" / "skills" / "myskill"
    skill_dir.mkdir(parents=True)
    f = skill_dir / "SKILL.md"
    f.write_text(content, encoding="utf-8")
    return f


class TestExtractBashBlocks:
    """Covers extract_bash_blocks() block boundary detection."""

    def test_single_block_returned(self) -> None:
        """Single bash fence yields one block body."""
        assert cbp.extract_bash_blocks("```bash\nFOO=1\n```\n") == ["FOO=1\n"]

    def test_two_blocks_returned_in_order(self) -> None:
        """Two bash fences yield two bodies in document order."""
        text = "```bash\nA=1\n```\nprose\n```bash\nB=2\n```\n"
        blocks = cbp.extract_bash_blocks(text)
        assert len(blocks) == 2
        assert "A=1" in blocks[0]
        assert "B=2" in blocks[1]

    def test_no_fences_returns_empty(self) -> None:
        """File with no bash fences returns empty list."""
        assert cbp.extract_bash_blocks("just prose\n") == []

    def test_non_bash_fence_ignored(self) -> None:
        """Python fenced block is not included."""
        assert cbp.extract_bash_blocks("```python\nFOO=1\n```\n") == []


class TestAssignedVars:
    """Covers assigned_vars() variable assignment detection."""

    def test_simple_assignment(self) -> None:
        """Plain VAR=value is detected."""
        assert "FOO" in cbp.assigned_vars("FOO=bar\n")

    def test_export_prefix(self) -> None:
        """Export VAR=value is detected."""
        assert "FOO" in cbp.assigned_vars("export FOO=bar\n")

    def test_local_prefix(self) -> None:
        """Local VAR=value is detected."""
        assert "FOO" in cbp.assigned_vars("local FOO=bar\n")

    def test_comment_line_skipped(self) -> None:
        """Assignment on a comment line is not detected."""
        assert cbp.assigned_vars("# FOO=bar\n") == frozenset()

    def test_indented_assignment(self) -> None:
        """Indented assignment is detected."""
        assert "FOO" in cbp.assigned_vars("  FOO=bar\n")

    def test_subshell_assignment(self) -> None:
        """Detect assignments made through command substitution."""
        assert "FOO" in cbp.assigned_vars("FOO=$(date)\n")

    @pytest.mark.parametrize("line", ["echo $FOO\n", '[ "$FOO" = "x" ]\n'])
    def test_reference_not_treated_as_assignment(self, line: str) -> None:
        """Variable reference without = at correct position is not an assignment."""
        assert cbp.assigned_vars(line) == frozenset()


class TestReferencedVars:
    """Covers referenced_vars() variable reference detection."""

    def test_dollar_var(self) -> None:
        """$VAR is detected."""
        assert "FOO" in cbp.referenced_vars("echo $FOO\n")

    def test_braced_var(self) -> None:
        """${VAR} is detected."""
        assert "FOO" in cbp.referenced_vars("echo ${FOO}\n")

    def test_env_var_filtered(self) -> None:
        """Known env vars like $HOME are not returned."""
        assert "HOME" not in cbp.referenced_vars("echo $HOME\n")

    def test_single_char_filtered(self) -> None:
        """Single-char vars like $f are not returned."""
        assert "f" not in cbp.referenced_vars("for f in *; do echo $f; done\n")

    def test_multiple_refs_on_one_line(self) -> None:
        """Multiple $VAR references on one line are all detected."""
        refs = cbp.referenced_vars("cp $SRC $DEST\n")
        assert "SRC" in refs
        assert "DEST" in refs

    def test_empty_block_returns_empty(self) -> None:
        """Empty block yields no references."""
        assert cbp.referenced_vars("") == frozenset()


class TestCheckFile:
    """Covers check_file() end-to-end violation detection."""

    def test_clean_single_block(self, tmp_path: Path) -> None:
        """Single bash block with no cross-block refs passes."""
        f = _skill(tmp_path, "```bash\nFOO=1\necho $FOO\n```\n")
        assert cbp.check_file(f) == []

    def test_clean_same_block_assign_and_ref(self, tmp_path: Path) -> None:
        """Assign and reference in same block — not a violation."""
        content = "```bash\nBAR=x\n```\n```bash\nBAZ=y\necho $BAZ\n```\n"
        f = _skill(tmp_path, content)
        assert cbp.check_file(f) == []

    def test_cross_block_violation_detected(self, tmp_path: Path) -> None:
        """Var assigned in block 1, referenced in block 2 is flagged C41."""
        content = "```bash\nFOO=bar\n```\n```bash\necho $FOO\n```\n"
        f = _skill(tmp_path, content)
        findings = cbp.check_file(f)
        assert len(findings) == 1
        assert "C41-CRITICAL" in findings[0]
        assert "FOO" in findings[0]
        assert "block 1" in findings[0]
        assert "block 2" in findings[0]

    def test_skip_var_assigned_env_var_not_flagged(self, tmp_path: Path) -> None:
        """LOCAL_MODE referenced but never assigned — not flagged (env var)."""
        content = '```bash\necho start\n```\n```bash\n[ "$LOCAL_MODE" = "true" ]\n```\n'
        f = _skill(tmp_path, content)
        assert cbp.check_file(f) == []

    def test_var_assigned_in_block2_referenced_in_block3_flagged(self, tmp_path: Path) -> None:
        """Assign in block 2, reference in block 3 is flagged with correct block numbers."""
        content = "```bash\necho a\n```\n```bash\nTS=$(date)\n```\n```bash\necho $TS\n```\n"
        f = _skill(tmp_path, content)
        findings = cbp.check_file(f)
        assert any("block 2" in x and "block 3" in x for x in findings)

    def test_nonexistent_file_returns_empty(self, tmp_path: Path) -> None:
        """Missing file returns no findings instead of raising."""
        assert cbp.check_file(tmp_path / "missing.md") == []

    def test_file_with_one_block_skipped(self, tmp_path: Path) -> None:
        """Single-block files are skipped — no cross-block issue possible."""
        f = _skill(tmp_path, "```bash\nFOO=1\n```\n")
        assert cbp.check_file(f) == []


class TestReferencedVarsComments:
    """Covers comment-line skipping in referenced_vars()."""

    def test_full_comment_reference_ignored(self) -> None:
        """A var named only in a full-line # comment is not a reference."""
        assert "COMMIT_SENTINEL" not in cbp.referenced_vars("# $COMMIT_SENTINEL is gone\n")

    def test_code_reference_still_detected(self) -> None:
        """A real reference on a code line survives alongside comment lines."""
        assert "FOO" in cbp.referenced_vars("# note $BAR\necho $FOO\n")


class TestTemplateBlock:
    """Covers is_template_block() placeholder detection (suppression rule 3)."""

    def test_unassigned_placeholder_flags_template(self) -> None:
        """A never-assigned ${I} loop-counter token marks the block a template."""
        assert cbp.is_template_block("cp x .../ctx-${I}.md\n", frozenset({"RUN_ID"})) is True

    def test_all_tokens_assigned_not_template(self) -> None:
        """Block whose refs are all assigned somewhere is not a template."""
        assert cbp.is_template_block("echo ${RUN_ID}\n", frozenset({"RUN_ID"})) is False

    def test_known_env_var_not_placeholder(self) -> None:
        """A known-safe env var (ARGUMENTS) never assigned is not a placeholder."""
        assert cbp.is_template_block('echo "$ARGUMENTS"\n', frozenset()) is False

    def test_comment_placeholder_ignored(self) -> None:
        """A placeholder token appearing only in a comment does not mark a template."""
        assert cbp.is_template_block("# uses ${I}\necho done\n", frozenset()) is False

    def test_angle_bracket_placeholder_flags_template(self) -> None:
        """An <identifier> angle-bracket placeholder marks a usage-example block."""
        assert cbp.is_template_block('grep "$SQ" rdeps <TARGET_MODULE>\n', frozenset({"SQ"})) is True

    def test_shell_redirection_not_angle_placeholder(self) -> None:
        """Real shell redirection (`< file`, `<(`, `<<`) is not an angle placeholder."""
        assert cbp.is_template_block('sort < "$INFILE"\n', frozenset({"INFILE"})) is False
        assert cbp.is_template_block('diff <(sort "$A") "$B"\n', frozenset({"A", "B"})) is False


class TestReloadsBeforeRef:
    """Covers reloads_before_ref() state-reload detection (suppression rule 1)."""

    def test_eval_reload_before_reference(self) -> None:
        """Eval "$(...)" before the reference re-derives the value."""
        assert cbp.reloads_before_ref('eval "$(git_slugs.sh)"\nrm -f "$SENTINEL"\n', "SENTINEL") is True

    def test_source_reload_before_reference(self) -> None:
        """Source of a state file before the reference re-derives the value."""
        assert cbp.reloads_before_ref('source ./state.sh\necho "$VARX"\n', "VARX") is True

    def test_dot_reload_quoted_path(self) -> None:
        """A quoted dot-source path (.

        "$FILE") is recognized as a reload.
        """
        assert cbp.reloads_before_ref('. "${TMPDIR:-/tmp}/state-${CSID}"\necho "$VARX"\n', "VARX") is True

    def test_reload_after_reference_not_suppressed(self) -> None:
        """A reload appearing after the reference does not rescue it — still lost."""
        assert cbp.reloads_before_ref('echo "$VARX"\neval "$(gen)"\n', "VARX") is False

    def test_no_reload_returns_false(self) -> None:
        """A plain reference with no reload command is not suppressed."""
        assert cbp.reloads_before_ref('echo "$VARX"\n', "VARX") is False


class TestRefsAllDefended:
    """Covers refs_all_defended() empty-var defence detection (suppression rule 2)."""

    def test_strip_assignment_with_guard(self) -> None:
        """Treat a derived variable with an empty-value guard as defended."""
        block = '_SKILLS="${_SHARED%/_shared}"\n[ -z "$_SKILLS" ] && _SKILLS="fallback"\n'
        assert cbp.refs_all_defended(block, "_SHARED") is True

    def test_default_expansion_defended(self) -> None:
        """A ${VAR:-default} parameter expansion defends against empty."""
        assert cbp.refs_all_defended('echo "${OUTDIR:-/tmp}"\n', "OUTDIR") is True

    def test_bare_reference_not_defended(self) -> None:
        """A bare $VAR reference is not defended."""
        assert cbp.refs_all_defended('echo "$OUTDIR"\n', "OUTDIR") is False

    def test_partial_defence_not_all(self) -> None:
        """One defended and one bare reference means not all-defended."""
        block = 'X="${A:-y}"\necho "$A"\n'
        assert cbp.refs_all_defended(block, "A") is False


class TestSuppressionEndToEnd:
    """Covers check_file() suppression of the three FP classes plus real-loss preservation."""

    def test_reload_suppresses_finding(self, tmp_path: Path) -> None:
        """Eval reload of a state file in the referencing block suppresses C41."""
        content = '```bash\nSENTINEL=/tmp/x\n```\n```bash\neval "$(gen_slugs)"\nrm -f "$SENTINEL"\n```\n'
        assert cbp.check_file(_skill(tmp_path, content)) == []

    def test_comment_only_reference_suppressed(self, tmp_path: Path) -> None:
        """A cross-block var named only in a comment is not flagged."""
        content = "```bash\nSENTINEL=/tmp/x\n```\n```bash\n# $SENTINEL gone\necho done\n```\n"
        assert cbp.check_file(_skill(tmp_path, content)) == []

    def test_defended_reference_suppressed(self, tmp_path: Path) -> None:
        """A stripped-and-guarded reference is empty-var-defended, not flagged."""
        content = (
            "```bash\n_SHARED=/a/b/_shared\n```\n"
            '```bash\n_SKILLS="${_SHARED%/_shared}"\n[ -z "$_SKILLS" ] && _SKILLS="x"\n```\n'
        )
        assert cbp.check_file(_skill(tmp_path, content)) == []

    def test_template_placeholder_block_suppressed(self, tmp_path: Path) -> None:
        """A referencing block containing a never-assigned ${I} placeholder is suppressed."""
        content = "```bash\nRUN_ID=$(date -u +%s)\n```\n```bash\ngit log > state/${RUN_ID}/ctx-${I}.md\n```\n"
        assert cbp.check_file(_skill(tmp_path, content)) == []

    def test_real_bare_loss_still_flags(self, tmp_path: Path) -> None:
        """A bare cross-block ref with no re-derivation, guard, or placeholder still flags."""
        content = '```bash\nMEMORY_DIR=/a/b\n```\n```bash\nls "$MEMORY_DIR"/x-*.md\n```\n'
        findings = cbp.check_file(_skill(tmp_path, content))
        assert len(findings) == 1
        assert "MEMORY_DIR" in findings[0]

    def test_real_loss_with_reload_after_ref_still_flags(self, tmp_path: Path) -> None:
        """A reload placed after the reference does not rescue the lost value."""
        content = '```bash\nVARX=1\n```\n```bash\necho "$VARX"\neval "$(gen)"\n```\n'
        findings = cbp.check_file(_skill(tmp_path, content))
        assert any("VARX" in f for f in findings)


class TestMain:
    """Covers main() CLI integration."""

    def test_clean_file_exits_zero(self, tmp_path: Path) -> None:
        """All-clean files produce exit code 0."""
        _skill(tmp_path, "```bash\nFOO=1\necho $FOO\n```\n")
        rc = cbp.main(["--scan-dir", str(tmp_path)])
        assert rc == 0

    def test_violation_exits_one(self, tmp_path: Path) -> None:
        """File with cross-block ref produces exit code 1."""
        _skill(tmp_path, "```bash\nFOO=1\n```\n```bash\necho $FOO\n```\n")
        rc = cbp.main(["--scan-dir", str(tmp_path)])
        assert rc == 1

    def test_explicit_file_arg(self, tmp_path: Path) -> None:
        """Explicit file path argument is checked."""
        f = _skill(tmp_path, "```bash\nFOO=1\n```\n```bash\necho $FOO\n```\n")
        assert cbp.main([str(f)]) == 1

    def test_timeout_arg_accepted(self, tmp_path: Path) -> None:
        """Verify command-line option behavior.

        --timeout flag is accepted without error.
        """
        _skill(tmp_path, "```bash\nFOO=1\necho $FOO\n```\n")
        assert cbp.main(["--scan-dir", str(tmp_path), "--timeout", "15"]) == 0

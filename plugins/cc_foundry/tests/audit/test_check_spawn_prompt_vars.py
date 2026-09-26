"""Tests for check_spawn_prompt_vars bin script.

Covers markdown block detection, $VAR flagging, caller-substituted var filtering, non-markdown $VAR not flagged, and CLI
integration.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import check_spawn_prompt_vars as cspv


def _file(tmp_path: Path, content: str, name: str = "SKILL.md") -> Path:
    """Write a skill or template fixture file and return its path.

    Examples:
        >>> from tempfile import TemporaryDirectory
        >>> with TemporaryDirectory() as directory:
        ...     _file(Path(directory), "body").read_text() == "body"
        True
    """
    if name == "SKILL.md":
        skill_dir = tmp_path / "myplugin" / "skills" / "myskill"
        skill_dir.mkdir(parents=True, exist_ok=True)
        f = skill_dir / name
    else:
        tpl_dir = tmp_path / "myplugin" / "skills" / "myskill" / "templates"
        tpl_dir.mkdir(parents=True, exist_ok=True)
        f = tpl_dir / name
    f.write_text(content, encoding="utf-8")
    return f


class TestCheckFile:
    """Covers check_file() violation detection."""

    def test_clean_no_markdown_blocks(self, tmp_path: Path) -> None:
        """File with no markdown blocks has no violations."""
        f = _file(tmp_path, "```bash\n$_FOUNDRY_SHARED/foo.md\n```\n")
        assert cspv.check_file(f) == []

    def test_dollar_var_in_markdown_block_flagged(self, tmp_path: Path) -> None:
        """$VAR inside markdown block is flagged C42."""
        content = "```markdown\nRead $_FOUNDRY_SHARED/foo.md\n```\n"
        f = _file(tmp_path, content)
        findings = cspv.check_file(f)
        assert len(findings) == 1
        assert "C42-CRITICAL" in findings[0]
        assert "_FOUNDRY_SHARED" in findings[0]
        assert "markdown block 1" in findings[0]

    def test_braced_var_in_markdown_block_flagged(self, tmp_path: Path) -> None:
        """${VAR} inside markdown block is also flagged."""
        content = "```markdown\nRead ${_FOUNDRY_SHARED}/foo.md\n```\n"
        f = _file(tmp_path, content)
        findings = cspv.check_file(f)
        assert any("_FOUNDRY_SHARED" in x for x in findings)

    def test_caller_substituted_var_not_flagged(self, tmp_path: Path) -> None:
        """Known caller-substituted vars like $RUN_DIR are not flagged."""
        content = "```markdown\nWrite to $RUN_DIR/out.md\n```\n"
        f = _file(tmp_path, content)
        assert cspv.check_file(f) == []

    def test_arguments_not_flagged(self, tmp_path: Path) -> None:
        """$ARGUMENTS is a valid runtime-injected var — not flagged."""
        content = "```markdown\nProcess $ARGUMENTS\n```\n"
        f = _file(tmp_path, content)
        assert cspv.check_file(f) == []

    def test_angle_bracket_template_not_flagged(self, tmp_path: Path) -> None:
        """<VAR> templates (no $) are not flagged."""
        content = "```markdown\nWrite to <RUN_DIR>/out.md\n```\n"
        f = _file(tmp_path, content)
        assert cspv.check_file(f) == []

    def test_dollar_var_outside_markdown_block_not_flagged(self, tmp_path: Path) -> None:
        """$VAR in prose or bash blocks outside markdown fences is not flagged."""
        content = "Prose with $_FOUNDRY_SHARED.\n```bash\necho $_FOUNDRY_SHARED\n```\n"
        f = _file(tmp_path, content)
        assert cspv.check_file(f) == []

    def test_same_var_reported_once_per_block(self, tmp_path: Path) -> None:
        """Same var on multiple lines in one block yields one finding."""
        content = "```markdown\n$_SHARED/a.md\n$_SHARED/b.md\n```\n"
        f = _file(tmp_path, content)
        findings = [x for x in cspv.check_file(f) if "_SHARED" in x]
        assert len(findings) == 1

    def test_two_blocks_each_with_var_yield_two_findings(self, tmp_path: Path) -> None:
        """Distinct vars in two separate markdown blocks each produce a finding."""
        content = "```markdown\n$FOO_VAR/a\n```\n```markdown\n$BAR_VAR/b\n```\n"
        f = _file(tmp_path, content)
        findings = cspv.check_file(f)
        assert len(findings) == 2
        assert any("FOO_VAR" in x and "block 1" in x for x in findings)
        assert any("BAR_VAR" in x and "block 2" in x for x in findings)

    def test_nonexistent_file_returns_empty(self, tmp_path: Path) -> None:
        """Missing file returns no findings instead of raising."""
        assert cspv.check_file(tmp_path / "missing.md") == []

    def test_template_md_file_scanned(self, tmp_path: Path) -> None:
        """Non-SKILL.md template file is also checked."""
        content = "```markdown\nRead $_FOUNDRY_SHARED/x.md\n```\n"
        f = _file(tmp_path, content, name="audit-fix-prompt.md")
        findings = cspv.check_file(f)
        assert any("C42" in x for x in findings)

    @pytest.mark.parametrize("var", ["_FOUNDRY_SHARED", "_FS", "CODEX_AVAILABLE", "BATCH_SZ"])
    def test_various_unexpanded_vars_flagged(self, tmp_path: Path, var: str) -> None:
        """Any unrecognised $VAR in markdown block is flagged."""
        content = f"```markdown\necho ${var}\n```\n"
        f = _file(tmp_path, content)
        assert any(var in x for x in cspv.check_file(f))

    def test_genuine_literal_still_flagged(self, tmp_path: Path) -> None:
        """A real literal $FOO — no default, not env var, no bracket, no directive — still flags."""
        content = "```markdown\nRead $FOO_LITERAL/config.md before starting\n```\n"
        f = _file(tmp_path, content)
        findings = cspv.check_file(f)
        assert len(findings) == 1
        assert "FOO_LITERAL" in findings[0]


class TestSuppressionClasses:
    """Covers the four false-positive suppression classes (C42 triage)."""

    def test_param_expansion_with_default_not_flagged(self, tmp_path: Path) -> None:
        """${VAR:-default} idiom (class 1) is a portable shell form — not flagged."""
        content = "```markdown\nwrite to ${TMPDIR:-/tmp}/out and ${CACHE_DIR:-/var}/x\n```\n"  # tmpdir-exempt: synthetic ${VAR:-default}-idiom fixture, not a real sentinel — CSID would itself trip C42
        f = _file(tmp_path, content)
        assert cspv.check_file(f) == []

    def test_default_form_elsewhere_suppresses_bare_occurrence(self, tmp_path: Path) -> None:
        """Bare $VAR is suppressed when the same file uses ${VAR:-default} anywhere (class 1)."""
        content = "```bash\n_IDX=${CODEMAP_INDEX_DIR:-/x}\n```\n```markdown\nread $CODEMAP_INDEX_DIR/y\n```\n"
        f = _file(tmp_path, content)
        assert cspv.check_file(f) == []

    @pytest.mark.parametrize("var", ["TMPDIR", "HOME", "PWD", "CLAUDE_PLUGIN_ROOT"])
    def test_well_known_env_var_not_flagged(self, tmp_path: Path, var: str) -> None:
        """Bare well-known env vars (class 2) resolve in the subagent's own env — not flagged."""
        content = f"```markdown\nread ${var}/thing\n```\n"
        f = _file(tmp_path, content)
        assert cspv.check_file(f) == []

    def test_env_var_phrase_placeholder_not_flagged(self, tmp_path: Path) -> None:
        """A $VAR documented as an env var name (class 2) is not orchestrator payload."""
        content = "```markdown\nRemove; use env var `$MY_KEY` instead\n```\n"
        f = _file(tmp_path, content)
        assert cspv.check_file(f) == []

    def test_substitute_directive_suppresses_var(self, tmp_path: Path) -> None:
        """A substitute/expand/replace directive naming the token (class 3) suppresses it."""
        content = (
            "Block header: expand `${PROGRAM_PATH}` before passing.\n"
            "```markdown\nRead the program at ${PROGRAM_PATH}.\n```\n"
        )
        f = _file(tmp_path, content)
        assert cspv.check_file(f) == []

    def test_substitute_directive_inside_block_suppresses_var(self, tmp_path: Path) -> None:
        """Directive line within the block itself (class 3) also suppresses the var."""
        content = "```markdown\nRead `<MANAGE_TPL>/x.md` (substitute resolved `$MANAGE_TPL`).\n```\n"
        f = _file(tmp_path, content)
        assert cspv.check_file(f) == []

    def test_square_bracket_editorial_placeholder_not_flagged(self, tmp_path: Path) -> None:
        """$VAR inside a [...] editorial span (class 4) is an orchestrator instruction."""
        content = "```markdown\n[Continue with section template from $TEMPLATE_FILE]\n```\n"
        f = _file(tmp_path, content)
        assert cspv.check_file(f) == []

    def test_var_outside_brackets_on_bracket_line_still_flagged(self, tmp_path: Path) -> None:
        """A literal $VAR outside the [...] span on the same line is still flagged."""
        content = "```markdown\nRead $REAL_LIT then [note about $INNER_VAR here]\n```\n"
        f = _file(tmp_path, content)
        findings = cspv.check_file(f)
        assert any("REAL_LIT" in x for x in findings)
        assert not any("INNER_VAR" in x for x in findings)


class TestContextHelpers:
    """Covers scan_file_context() and is_suppressed() directly."""

    def test_scan_file_context_collects_default_and_directive_vars(self) -> None:
        """Default-expansion vars and directive-line vars are gathered separately."""
        text = "x=${TMP:-/t}\nsubstitute `$RUN_DIR` before passing\nplain $UNTOUCHED\n"
        default_vars, directive_vars = cspv.scan_file_context(text)
        assert default_vars == {"TMP"}
        assert directive_vars == {"RUN_DIR"}

    def test_is_suppressed_flags_true_literal(self) -> None:
        """A bare literal with no suppression signal returns False."""
        assert cspv.is_suppressed("FOO", 0, "$FOO/x", set(), set()) is False


class TestMain:
    """Covers main() CLI integration."""

    def test_clean_file_exits_zero(self, tmp_path: Path) -> None:
        """All-clean files produce exit code 0."""
        _file(tmp_path, "```bash\necho hi\n```\n")
        assert cspv.main(["--scan-dir", str(tmp_path)]) == 0

    def test_violation_exits_one(self, tmp_path: Path) -> None:
        """File with unexpanded var produces exit code 1."""
        _file(tmp_path, "```markdown\nRead $_FOUNDRY_SHARED/x.md\n```\n")
        assert cspv.main(["--scan-dir", str(tmp_path)]) == 1

    def test_explicit_file_arg(self, tmp_path: Path) -> None:
        """Explicit file path argument is checked."""
        f = _file(tmp_path, "```markdown\nRead $_FOUNDRY_SHARED/x.md\n```\n")
        assert cspv.main([str(f)]) == 1

    def test_timeout_arg_accepted(self, tmp_path: Path) -> None:
        """Verify command-line option behavior.

        The ``--timeout`` flag is accepted without error.
        """
        _file(tmp_path, "no fences\n")
        assert cspv.main(["--scan-dir", str(tmp_path), "--timeout", "10"]) == 0

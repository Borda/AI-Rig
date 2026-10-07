"""Tests for check_spawn_prompt_vars bin script.

Covers markdown block detection, $VAR flagging, caller-substituted var filtering, non-markdown $VAR not flagged, and CLI
integration.
"""

from __future__ import annotations

from pathlib import Path

import check_spawn_prompt_vars as cspv
import pytest


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

    @pytest.mark.parametrize(
        "content",
        [
            pytest.param("```bash\n$_FOUNDRY_SHARED/foo.md\n```\n", id="no-markdown-block"),
            pytest.param("```markdown\nWrite to $RUN_DIR/out.md\n```\n", id="caller-substituted-var"),
            pytest.param("```markdown\nProcess $ARGUMENTS\n```\n", id="runtime-injected-arguments"),
            pytest.param("```markdown\nWrite to <RUN_DIR>/out.md\n```\n", id="angle-bracket-template"),
            pytest.param(
                "Prose with $_FOUNDRY_SHARED.\n```bash\necho $_FOUNDRY_SHARED\n```\n", id="var-outside-markdown-block"
            ),
            # Class 1: a bare $VAR is suppressed when the same file uses ${VAR:-default} anywhere.
            pytest.param(
                "```bash\n_IDX=${CODEMAP_INDEX_DIR:-/x}\n```\n```markdown\nread $CODEMAP_INDEX_DIR/y\n```\n",
                id="default-form-elsewhere-in-file",
            ),
            # Class 2: a $VAR documented as an env var name is not orchestrator payload.
            pytest.param("```markdown\nRemove; use env var `$MY_KEY` instead\n```\n", id="env-var-phrase"),
            # Class 3: a substitute/expand/replace directive naming the token suppresses it.
            pytest.param(
                "Block header: expand `${PROGRAM_PATH}` before passing.\n"
                "```markdown\nRead the program at ${PROGRAM_PATH}.\n```\n",
                id="directive-before-block",
            ),
            pytest.param(
                "```markdown\nRead `<MANAGE_TPL>/x.md` (substitute resolved `$MANAGE_TPL`).\n```\n",
                id="directive-inside-block",
            ),
            # Class 4: $VAR inside a [...] editorial span is an orchestrator instruction.
            pytest.param(
                "```markdown\n[Continue with section template from $TEMPLATE_FILE]\n```\n",
                id="square-bracket-editorial",
            ),
        ],
    )
    def test_text_without_unexpanded_var_has_no_findings(self, tmp_path: Path, content: str) -> None:
        """Variables that need no caller expansion, and non-markdown text, are never flagged.

        Covers a file with no markdown block, caller-substituted and runtime-injected names, ``<VAR>`` templates, a
        ``$VAR`` outside markdown fences, and the suppression classes: a ``${VAR:-default}`` form elsewhere in the file,
        a documented env var name, a substitute directive (before or inside the block) and a ``[...]`` editorial span.
        """
        assert cspv.check_file(_file(tmp_path, content)) == []

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

    @pytest.mark.parametrize("var", ["TMPDIR", "HOME", "PWD", "CLAUDE_PLUGIN_ROOT"])
    def test_well_known_env_var_not_flagged(self, tmp_path: Path, var: str) -> None:
        """Bare well-known env vars (class 2) resolve in the subagent's own env — not flagged."""
        content = f"```markdown\nread ${var}/thing\n```\n"
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

    @pytest.mark.parametrize(
        ("content", "expected"),
        [
            pytest.param("```bash\necho hi\n```\n", 0, id="clean-file"),
            pytest.param("```markdown\nRead $_FOUNDRY_SHARED/x.md\n```\n", 1, id="unexpanded-var"),
        ],
    )
    def test_scan_dir_exit_code_mirrors_findings(self, tmp_path: Path, content: str, expected: int) -> None:
        """A scanned tree exits 0 when clean and 1 when a markdown block holds an unexpanded variable."""
        _file(tmp_path, content)
        assert cspv.main(["--scan-dir", str(tmp_path)]) == expected

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

"""Tests for check_spawn_prompt_vars bin script.

Covers markdown block detection, $VAR flagging, caller-substituted var filtering, the four false-positive suppression
classes, non-markdown $VAR not flagged, and CLI integration. Every test builds its skill or template file under
``tmp_path``; no test spawns a process.
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
    """Covers check_file() violation detection and its suppression classes."""

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
            # The ${VAR:-default} idiom is a portable shell form and is never flagged itself.
            pytest.param(
                "```markdown\nwrite to ${TMPDIR:-/tmp}/out and ${CACHE_DIR:-/var}/x\n```\n",  # tmpdir-exempt: synthetic ${VAR:-default}-idiom fixture, not a real sentinel — CSID would itself trip C42
                id="default-expansion-idiom",
            ),
            # Class 2: a $VAR documented as an env var name is not orchestrator payload.
            pytest.param("```markdown\nRemove; use env var `$MY_KEY` instead\n```\n", id="env-var-phrase"),
            # Class 2: bare well-known env vars resolve in the subagent's own environment.
            pytest.param("```markdown\nread $TMPDIR/thing\n```\n", id="well-known-env-tmpdir"),
            pytest.param("```markdown\nread $HOME/thing\n```\n", id="well-known-env-home"),
            pytest.param("```markdown\nread $PWD/thing\n```\n", id="well-known-env-pwd"),
            pytest.param("```markdown\nread $CLAUDE_PLUGIN_ROOT/thing\n```\n", id="well-known-env-plugin-root"),
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
        ``$VAR`` outside markdown fences, and the four suppression classes: a ``${VAR:-default}`` form (anywhere in the
        file, or as the idiom itself), a documented or well-known env var name, a substitute directive (before or inside
        the block) and a ``[...]`` editorial span.
        """
        assert cspv.check_file(_file(tmp_path, content)) == []

    @pytest.mark.parametrize(
        ("content", "var", "name"),
        [
            pytest.param("```markdown\nRead $_FOUNDRY_SHARED/foo.md\n```\n", "_FOUNDRY_SHARED", "SKILL.md", id="bare"),
            pytest.param(
                "```markdown\nRead ${_FOUNDRY_SHARED}/foo.md\n```\n", "_FOUNDRY_SHARED", "SKILL.md", id="braced"
            ),
            pytest.param(
                "```markdown\nRead $FOO_LITERAL/config.md before starting\n```\n",
                "FOO_LITERAL",
                "SKILL.md",
                id="genuine-literal-without-suppression",
            ),
            pytest.param(
                "```markdown\nRead $_FOUNDRY_SHARED/x.md\n```\n",
                "_FOUNDRY_SHARED",
                "audit-fix-prompt.md",
                id="template-file",
            ),
            pytest.param(
                "```markdown\necho $_FOUNDRY_SHARED\n```\n", "_FOUNDRY_SHARED", "SKILL.md", id="shared-dir-var"
            ),
            pytest.param("```markdown\necho $_FS\n```\n", "_FS", "SKILL.md", id="short-shared-var"),
            pytest.param("```markdown\necho $CODEX_AVAILABLE\n```\n", "CODEX_AVAILABLE", "SKILL.md", id="flag-var"),
            pytest.param("```markdown\necho $BATCH_SZ\n```\n", "BATCH_SZ", "SKILL.md", id="size-var"),
        ],
    )
    def test_unexpanded_var_in_markdown_block_is_flagged(
        self, tmp_path: Path, content: str, var: str, name: str
    ) -> None:
        """An unrecognised $VAR or ${VAR} inside a markdown block is flagged C42 and names the variable and block.

        Covers the bare and braced forms, a real literal with no default, env-var phrase, bracket or directive to
        suppress it, a non-SKILL.md template file, and assorted variable names.
        """
        findings = cspv.check_file(_file(tmp_path, content, name=name))
        assert len(findings) == 1
        assert "C42-CRITICAL" in findings[0]
        assert var in findings[0]
        assert "markdown block 1" in findings[0]

    def test_same_var_reported_once_per_block(self, tmp_path: Path) -> None:
        """Same var on multiple lines in one block yields one finding.

        Repeating a variable within a block adds no information, so the report stays one line per variable and block.
        """
        f = _file(tmp_path, "```markdown\n$_SHARED/a.md\n$_SHARED/b.md\n```\n")
        assert len([x for x in cspv.check_file(f) if "_SHARED" in x]) == 1

    def test_two_blocks_each_with_var_yield_two_findings(self, tmp_path: Path) -> None:
        """Distinct vars in two separate markdown blocks each produce a finding.

        Each finding carries the number of the block it came from, so the author can find it.
        """
        f = _file(tmp_path, "```markdown\n$FOO_VAR/a\n```\n```markdown\n$BAR_VAR/b\n```\n")
        findings = cspv.check_file(f)
        assert len(findings) == 2
        assert any("FOO_VAR" in x and "block 1" in x for x in findings)
        assert any("BAR_VAR" in x and "block 2" in x for x in findings)

    def test_var_outside_brackets_on_bracket_line_still_flagged(self, tmp_path: Path) -> None:
        """A literal $VAR outside the [...] span on the same line is still flagged.

        The editorial-span suppression covers only the bracketed text, not the rest of its line.
        """
        f = _file(tmp_path, "```markdown\nRead $REAL_LIT then [note about $INNER_VAR here]\n```\n")
        findings = cspv.check_file(f)
        assert any("REAL_LIT" in x for x in findings)
        assert not any("INNER_VAR" in x for x in findings)

    def test_nonexistent_file_returns_empty(self, tmp_path: Path) -> None:
        """Missing file returns no findings instead of raising.

        A path that vanished between listing and scanning, such as a rename mid-commit, must not crash the hook.
        """
        assert cspv.check_file(tmp_path / "missing.md") == []


class TestContextHelpers:
    """Covers scan_file_context() and is_suppressed() directly."""

    def test_scan_file_context_collects_default_and_directive_vars(self) -> None:
        """Default-expansion vars and directive-line vars are gathered separately.

        A variable on a plain line belongs to neither set, so it stays a candidate for flagging.
        """
        text = "x=${TMP:-/t}\nsubstitute `$RUN_DIR` before passing\nplain $UNTOUCHED\n"
        default_vars, directive_vars = cspv.scan_file_context(text)
        assert default_vars == {"TMP"}
        assert directive_vars == {"RUN_DIR"}

    def test_bare_literal_is_not_suppressed(self) -> None:
        """A bare literal with no suppression signal is not suppressed."""
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
        """A scanned tree exits 0 when clean and 1 when a markdown block holds an unexpanded variable.

        pre-commit keys on the exit code alone, so it must track the findings.
        """
        _file(tmp_path, content)
        assert cspv.main(["--scan-dir", str(tmp_path)]) == expected

    def test_explicit_file_arg(self, tmp_path: Path) -> None:
        """An explicit file path argument is checked.

        The pre-commit hook passes the changed files by name rather than a directory to scan.
        """
        f = _file(tmp_path, "```markdown\nRead $_FOUNDRY_SHARED/x.md\n```\n")
        assert cspv.main([str(f)]) == 1

    def test_timeout_arg_accepted(self, tmp_path: Path) -> None:
        """The ``--timeout`` flag is accepted without error.

        Every audit script shares that flag; a clean tree with it set must still exit 0.
        """
        _file(tmp_path, "no fences\n")
        assert cspv.main(["--scan-dir", str(tmp_path), "--timeout", "10"]) == 0
